"""MIDI 2.0 / UMP (Universal MIDI Packet) tool.

STATUS (updated after full live review/testing — see
memories/midi2-python-implementation-unverified-evaluation.md for the
verification pass): this module is fully wired into core/local_tools.py's
tool registry and exposes list_devices / open / close / send / poll,
matching midi1.py's action surface. 'send'/'poll' support BOTH a raw
4-word (128-bit) UMP packet (a plain list of ints) AND a typed 'message'
dict covering all six UMP message groups — native MIDI 2.0 Channel Voice,
classic MIDI-1-in-UMP Channel Voice, System Common/Real-Time, Utility,
SysEx7, and SysEx8/Mixed Data Set. Every encoder/decoder pair below has
been round-tripped live (encode->decode match) and the send/poll path has
been exercised end-to-end over a real ALSA self-loopback, not just unit
tested in isolation. Still-real scope limits, not yet done: no MIDI-CI
discovery, no Property Exchange, no Profiles, and sysex7/sysex8 remain
single-packet building blocks (the caller chunks a longer payload across
multiple 'send' calls themselves — no automatic multi-packet reassembly).
Linux/ALSA-specific; device discovery still shells out to `aconnect -l`
rather than a native client/port-query API call.

Why raw cffi + libasound instead of a Python MIDI library: no mature
Python UMP/MIDI-2.0 library exists (checked live — python-rtmidi/mido are
MIDI-1.0-only; python-alsa-midi has no UMP support in its source). ALSA's
own C sequencer API (libasound.so.2) DOES expose real UMP support
(snd_seq_set_client_midi_version, snd_seq_ump_event_output/_input, etc.) —
confirmed present via `nm -D` and header inspection
(/usr/include/alsa/seq_event.h) before any code was written. cffi is used
in ABI mode (dlopen, no compile step) since it's already available in the
venv backing this whole client.

PROVEN LIVE (self-loopback): opening an ALSA seq client, switching it to
UMP MIDI-2.0 mode, creating two ports on the same client, connecting them
to each other, sending a UMP event, and reading it back — byte-for-byte
round trip confirmed, both for raw words and for typed messages across
all six message groups (most recently re-confirmed via the public
send/poll actions themselves, not just the internal encode/decode
helpers).

ACTION SURFACE: list_devices / open / close / send / poll, same five verbs
as midi1.py.

- list_devices: shells out to `aconnect -l` (already the tool used during
  this project's own live research to confirm PipeWire's UMP-MIDI2
  clients) and parses it into structured client/port entries, flagging
  which clients advertise UMP-MIDI2 support. Chosen over hand-rolling the
  ALSA client/port-info query API (snd_seq_query_next_client/_port, etc.)
  to keep this module's cdef surface small — those bindings can be added
  later if `aconnect` ever proves insufficient.
- open: lazily creates ONE shared ALSA seq client for this whole process
  (module-level, mirrors midi1's _OPEN_PORTS process-lifetime-only
  statefulness caveat), then a new port on it per call. Optional
  'target_client'/'target_port' connect the new port to an existing ALSA
  client/port immediately (snd_seq_connect_to for an output port,
  snd_seq_connect_from for an input port) — omit both to leave the port
  unconnected (e.g. to self-loop-connect two of our own just-opened
  ports by passing our own client id + the other handle's port number).
- send: takes EITHER 'words' (a list of up to 4 unsigned-32-bit ints, the
  raw UMP packet, zero-padded if shorter) OR a typed 'message' dict (see
  the TOOLS description below for the full per-type field list across all
  six message groups) — not both.
- poll: drains ALL pending events on the shared client into a per-port
  buffer (since every handle shares one underlying ALSA client/queue),
  then returns and clears just the buffer for the requested handle's own
  port — so two open handles polling independently won't steal each
  other's events. Returns both the raw 'messages' (hex words) and a
  parallel best-effort typed 'decoded' array covering all six groups.
"""
import asyncio
import itertools
import json
import re
import subprocess
from typing import Any

_MIDI2_INIT_ERROR: Exception | None = None

try:
    from cffi import FFI

    ffi = FFI()

    ffi.cdef("""
typedef struct _snd_seq snd_seq_t;

typedef struct snd_seq_addr {
    unsigned char client;
    unsigned char port;
} snd_seq_addr_t;

typedef struct snd_seq_real_time {
    unsigned int tv_sec;
    unsigned int tv_nsec;
} snd_seq_real_time_t;
typedef unsigned int snd_seq_tick_time_t;
typedef union snd_seq_timestamp {
    snd_seq_tick_time_t tick;
    snd_seq_real_time_t time;
} snd_seq_timestamp_t;
typedef unsigned char snd_seq_event_type_t;

typedef struct snd_seq_ump_event {
    snd_seq_event_type_t type;
    unsigned char flags;
    unsigned char tag;
    unsigned char queue;
    snd_seq_timestamp_t time;
    snd_seq_addr_t source;
    snd_seq_addr_t dest;
    union {
        unsigned char legacy_pad[12];
        unsigned int ump[4];
    } data;
} snd_seq_ump_event_t;

int snd_seq_open(snd_seq_t **handle, const char *name, int streams, int mode);
int snd_seq_close(snd_seq_t *handle);
int snd_seq_set_client_name(snd_seq_t *seq, const char *name);
int snd_seq_set_client_midi_version(snd_seq_t *seq, int midi_version);
int snd_seq_nonblock(snd_seq_t *handle, int nonblock);
int snd_seq_client_id(snd_seq_t *handle);

int snd_seq_create_simple_port(snd_seq_t *seq, const char *name,
                                unsigned int caps, unsigned int type);
int snd_seq_delete_simple_port(snd_seq_t *seq, int port);
int snd_seq_connect_to(snd_seq_t *seq, int my_port, int dest_client, int dest_port);
int snd_seq_connect_from(snd_seq_t *seq, int my_port, int src_client, int src_port);

int snd_seq_ump_event_output(snd_seq_t *seq, snd_seq_ump_event_t *ev);
int snd_seq_drain_output(snd_seq_t *handle);
int snd_seq_ump_event_input(snd_seq_t *seq, snd_seq_ump_event_t **ev);
int snd_seq_event_input_pending(snd_seq_t *seq, int fetch_sequencer);

const char *snd_strerror(int errnum);
""")

    _lib = ffi.dlopen("libasound.so.2")
except (ImportError, OSError) as e:
    # ImportError: cffi itself isn't installed. OSError: cffi IS installed but
    # dlopen("libasound.so.2") failed (e.g. libasound2 missing/not on the
    # loader path). Either way, defer to the single guard in _run() below —
    # every action except list_devices (which only shells out to `aconnect`,
    # no ffi/_lib involved) checks `_lib is None` before touching either name,
    # so a missing dependency degrades to a friendly per-call error instead of
    # crashing `core/local_tools.py`'s module-level `from core import midi2`
    # (and therefore the whole tool registry) at import time.
    ffi = None
    _lib = None
    _MIDI2_INIT_ERROR = e

# --- ALSA sequencer constants (copied from the proven POC, not re-derived) ---
SND_SEQ_OPEN_DUPLEX = 3
SND_SEQ_NONBLOCK = 1
SND_SEQ_PORT_CAP_READ = 1
SND_SEQ_PORT_CAP_WRITE = 2
SND_SEQ_PORT_CAP_SUBS_READ = 32
SND_SEQ_PORT_CAP_SUBS_WRITE = 64
SND_SEQ_PORT_CAP_DUPLEX = 16
SND_SEQ_PORT_TYPE_MIDI_UMP = 1 << 7
SND_SEQ_PORT_TYPE_APPLICATION = 1048576
SND_SEQ_CLIENT_UMP_MIDI_2_0 = 2
SND_SEQ_EVENT_UMP_FLAG = 1 << 5
SND_SEQ_QUEUE_DIRECT = 253


def _check(rc, what):
    """Raise with ALSA's own error string if rc < 0, else return rc."""
    if rc < 0:
        err = ffi.string(_lib.snd_strerror(rc)).decode()
        raise RuntimeError(f"{what} failed: {err} ({rc})")
    return rc


# --- Native MIDI 2.0 Channel Voice (message_type=4) — typed message encode ---
# Wired into _send below (see the 'message' dispatch there). Pure bit-math,
# no ALSA calls, no side effects on its own. Bit layout verified against
# the reviewed reference
# ~/Programs/py-midi2/code/ump.py (MIT-licensed, encode-only, see
# memories/sysex-midi-tool-addition.md's "Reference find" entry) and
# cross-checked against the MIDI 2.0 UMP spec's own Channel Voice message
# group (UMP message_type=4, 2 words/64 bits per message: word0 packs
# message_type(4b)+group(4b)+status(8b)+index(16b); word1 is a plain 32-bit
# data value). Covers all 15 valid opcodes (0x0-0x6, 0x8-0xF; 0x7 is
# reserved/undefined in the spec, not implemented).
_CV_OPCODES = {
    "reg_per_note_controller": 0x0,
    "asn_per_note_controller": 0x1,
    "reg_controller": 0x2,       # RPN
    "asn_controller": 0x3,       # NRPN
    "rel_reg_controller": 0x4,   # relative RPN
    "rel_asn_controller": 0x5,   # relative NRPN
    "per_note_pitch_bend": 0x6,
    "note_off": 0x8,
    "note_on": 0x9,
    "poly_pressure": 0xA,
    "control_change": 0xB,
    "program_change": 0xC,
    "channel_pressure": 0xD,
    "pitch_bend": 0xE,
    "per_note_management": 0xF,
}


def _encode_channel_voice(message: dict):
    """Encode one MIDI 2.0 Channel Voice message dict into (word0, word1).

    `message` must have a 'type' key (one of _CV_OPCODES) plus whatever
    fields that type needs (channel/group default 0). Raises ValueError/
    KeyError on bad input — caller's job to catch and turn into a tool
    error response, same pattern as midi1._build_message.
    """
    msg_type = message.get("type")
    if msg_type not in _CV_OPCODES:
        raise ValueError(
            f"unknown MIDI 2.0 Channel Voice type {msg_type!r} — expected "
            f"one of {sorted(_CV_OPCODES)}"
        )
    group = int(message.get("group", 0)) & 0xF
    channel = int(message.get("channel", 0)) & 0xF
    opcode = _CV_OPCODES[msg_type]
    status = (opcode << 4) | channel

    if msg_type in ("note_off", "note_on"):
        note = int(message["note"]) & 0x7F
        attr_type = int(message.get("attribute_type", 0)) & 0xFF
        velocity = int(message["velocity"]) & 0xFFFF
        attr_data = int(message.get("attribute_data", 0)) & 0xFFFF
        index = (note << 8) | attr_type
        data = (velocity << 16) | attr_data
    elif msg_type == "poly_pressure":
        note = int(message["note"]) & 0x7F
        index = note << 8
        data = int(message["value"]) & 0xFFFFFFFF
    elif msg_type in ("reg_per_note_controller", "asn_per_note_controller"):
        note = int(message["note"]) & 0x7F
        ctrl_index = int(message["index"]) & 0xFF
        index = (note << 8) | ctrl_index
        data = int(message["value"]) & 0xFFFFFFFF
    elif msg_type == "per_note_management":
        note = int(message["note"]) & 0x7F
        detach = 1 if message.get("detach") else 0
        reset = 1 if message.get("reset") else 0
        index = (note << 8) | (detach << 1) | reset
        data = 0
    elif msg_type == "control_change":
        ctrl_index = int(message["index"]) & 0x7F
        index = ctrl_index << 8
        data = int(message["value"]) & 0xFFFFFFFF
    elif msg_type in (
        "reg_controller", "asn_controller",
        "rel_reg_controller", "rel_asn_controller",
    ):
        bank = int(message["bank"]) & 0x7F
        ctrl_index = int(message["index"]) & 0x7F
        index = (bank << 8) | ctrl_index
        data = int(message["value"]) & 0xFFFFFFFF
    elif msg_type == "program_change":
        program = int(message["program"]) & 0x7F
        bank_valid = 1 if message.get("bank_valid") else 0
        bank_msb = int(message.get("bank_msb", 0)) & 0x7F
        bank_lsb = int(message.get("bank_lsb", 0)) & 0x7F
        index = bank_valid
        data = (program << 24) | (bank_msb << 8) | bank_lsb
    elif msg_type == "channel_pressure":
        index = 0
        data = int(message["value"]) & 0xFFFFFFFF
    elif msg_type == "pitch_bend":
        index = 0
        data = int(message.get("value", 0x80000000)) & 0xFFFFFFFF
    elif msg_type == "per_note_pitch_bend":
        note = int(message["note"]) & 0x7F
        index = note << 8
        data = int(message.get("value", 0x80000000)) & 0xFFFFFFFF
    else:  # pragma: no cover - guarded by the membership check above
        raise ValueError(f"unhandled MIDI 2.0 Channel Voice type {msg_type!r}")

    word0 = (4 << 28) | (group << 24) | (status << 16) | index
    word1 = data
    return word0 & 0xFFFFFFFF, word1 & 0xFFFFFFFF


# --- Native MIDI 2.0 Channel Voice (message_type=4) — typed message decode ---
# Wired into _poll's 'decoded' array below. Pure bit-math, no ALSA calls, no
# side effects on its own. This is the exact inverse of _encode_channel_voice
# — no reference
# implementation exists for this direction either (the reviewed
# ~/Programs/py-midi2/code/ump.py is encode-only), so it was hand-derived
# directly from the same word0/word1 layout documented above rather than
# ported from anywhere.
_CV_OPCODES_REV = {v: k for k, v in _CV_OPCODES.items()}


def _decode_channel_voice(word0: int, word1: int) -> dict:
    """Decode one raw 2-word (64-bit) MIDI 2.0 Channel Voice UMP packet into
    a typed message dict — the exact inverse of _encode_channel_voice.

    Raises ValueError if word0's top nibble isn't message_type=4 (Channel
    Voice) or if the status nibble isn't one of the 15 valid opcodes (0x7 is
    spec-reserved). Returned dict always includes 'type', 'channel', 'group'
    plus whatever fields that type carries — using the SAME field names
    _encode_channel_voice expects as input, so encode(decode(x)) round-trips
    cleanly.
    """
    word0 &= 0xFFFFFFFF
    word1 &= 0xFFFFFFFF
    msg_type_nibble = (word0 >> 28) & 0xF
    if msg_type_nibble != 4:
        raise ValueError(
            f"word0 has message_type={msg_type_nibble:#x}, expected 4 "
            f"(Channel Voice) — not decodable by _decode_channel_voice"
        )
    group = (word0 >> 24) & 0xF
    status = (word0 >> 16) & 0xFF
    opcode = (status >> 4) & 0xF
    channel = status & 0xF
    index = word0 & 0xFFFF
    data = word1

    if opcode not in _CV_OPCODES_REV:
        raise ValueError(
            f"status nibble {opcode:#x} is not a valid Channel Voice opcode "
            f"(0x7 is spec-reserved/undefined) — cannot decode"
        )
    msg_type = _CV_OPCODES_REV[opcode]
    out = {"type": msg_type, "channel": channel, "group": group}

    if msg_type in ("note_off", "note_on"):
        out["note"] = (index >> 8) & 0x7F
        out["attribute_type"] = index & 0xFF
        out["velocity"] = (data >> 16) & 0xFFFF
        out["attribute_data"] = data & 0xFFFF
    elif msg_type == "poly_pressure":
        out["note"] = (index >> 8) & 0x7F
        out["value"] = data
    elif msg_type in ("reg_per_note_controller", "asn_per_note_controller"):
        out["note"] = (index >> 8) & 0x7F
        out["index"] = index & 0xFF
        out["value"] = data
    elif msg_type == "per_note_management":
        out["note"] = (index >> 8) & 0x7F
        bits = index & 0xFF
        out["detach"] = bool((bits >> 1) & 1)
        out["reset"] = bool(bits & 1)
    elif msg_type == "control_change":
        out["index"] = (index >> 8) & 0x7F
        out["value"] = data
    elif msg_type in (
        "reg_controller", "asn_controller",
        "rel_reg_controller", "rel_asn_controller",
    ):
        out["bank"] = (index >> 8) & 0x7F
        out["index"] = index & 0x7F
        out["value"] = data
    elif msg_type == "program_change":
        out["bank_valid"] = bool(index & 0x1)
        out["program"] = (data >> 24) & 0x7F
        out["bank_msb"] = (data >> 8) & 0x7F
        out["bank_lsb"] = data & 0x7F
    elif msg_type == "channel_pressure" or msg_type == "pitch_bend":
        out["value"] = data
    elif msg_type == "per_note_pitch_bend":
        out["note"] = (index >> 8) & 0x7F
        out["value"] = data
    else:  # pragma: no cover - guarded by the membership check above
        raise ValueError(f"unhandled MIDI 2.0 Channel Voice type {msg_type!r}")

    return out


# --- MIDI-1-in-UMP Channel Voice (message_type=2) — typed message encode ---
# Wired into _send below (see the 'message' dispatch there, including the
# 'protocol' disambiguator for the 4 type names shared with native Channel
# Voice). Pure bit-math, no ALSA calls, no side effects on its own. This is
# UMP message_type=2 ("MIDI 1.0 Channel Voice
# Messages") — a SINGLE 32-bit word (unlike type=4 Channel Voice's 2
# words), since it's literally the classic 3-byte MIDI 1.0 status+data1+
# data2 wrapped in a UMP header, at 7-bit (0-127) data resolution rather
# than type=4's expanded 16/32-bit resolution. Word layout: bits[31:28]
# message_type=2, bits[27:24] group, bits[23:20] opcode (the MIDI 1.0
# status nibble), bits[19:16] channel, bits[15:8] data1, bits[7:0] data2
# (unused/zero where MIDI 1.0 only has one data byte, e.g. program_change/
# aftertouch). Deliberately reuses the EXACT field names midi1.py's own
# `_build_message` already uses (note/velocity, control/value, program,
# pitch, etc.) rather than inventing new ones — since this message group
# IS classic MIDI 1.0 data, a message dict shaped for midi1's 'send'
# should mean the same thing here (modulo the extra optional 'group'
# field UMP adds). No reference implementation reviewed for this specific
# group (the earlier-reviewed ~/Programs/py-midi2/code/ump.py covers only
# native MIDI 2.0 Channel Voice) — hand-derived directly from the UMP spec's
# well-known "MIDI 1.0 Channel Voice Messages" packet layout.
_M1CV_OPCODES = {
    "note_off": 0x8,
    "note_on": 0x9,
    "polytouch": 0xA,       # poly key pressure — matches midi1.py's naming
    "control_change": 0xB,
    "program_change": 0xC,
    "aftertouch": 0xD,      # channel pressure — matches midi1.py's naming
    "pitchwheel": 0xE,      # matches midi1.py's naming (not 'pitch_bend')
}


def _encode_midi1_channel_voice(message: dict) -> int:
    """Encode one MIDI-1.0-in-UMP Channel Voice message dict into a single
    32-bit word. `message` must have a 'type' key (one of _M1CV_OPCODES)
    plus whatever fields that type needs — SAME field names as midi1.py's
    own `_build_message` (note/velocity, control/value, program, pitch,
    value), plus optional 'channel'/'group' (both default 0). Raises
    ValueError/KeyError on bad input — caller's job to catch, same pattern
    as _encode_channel_voice above.
    """
    msg_type = message.get("type")
    if msg_type not in _M1CV_OPCODES:
        raise ValueError(
            f"unknown MIDI-1-in-UMP Channel Voice type {msg_type!r} — "
            f"expected one of {sorted(_M1CV_OPCODES)}"
        )
    group = int(message.get("group", 0)) & 0xF
    channel = int(message.get("channel", 0)) & 0xF
    opcode = _M1CV_OPCODES[msg_type]

    if msg_type in ("note_on", "note_off"):
        data1 = int(message["note"]) & 0x7F
        data2 = int(message.get("velocity", 0)) & 0x7F
    elif msg_type == "polytouch":
        data1 = int(message["note"]) & 0x7F
        data2 = int(message.get("value", 0)) & 0x7F
    elif msg_type == "control_change":
        data1 = int(message["control"]) & 0x7F
        data2 = int(message.get("value", 0)) & 0x7F
    elif msg_type == "program_change":
        data1 = int(message["program"]) & 0x7F
        data2 = 0
    elif msg_type == "aftertouch":
        data1 = int(message.get("value", 0)) & 0x7F
        data2 = 0
    elif msg_type == "pitchwheel":
        # midi1.py's 'pitch' is signed -8192..8191 (0 = centered), same as
        # mido's own pitchwheel convention. Classic MIDI wire format is
        # unsigned 14-bit (0..16383, 8192 = centered), LSB in data1, MSB in
        # data2 — so re-center then split, same as the raw wire encoding
        # midi1.py's mido dependency does internally for MIDI 1.0 bytes.
        pitch = int(message.get("pitch", 0))
        unsigned14 = (pitch + 8192) & 0x3FFF
        data1 = unsigned14 & 0x7F
        data2 = (unsigned14 >> 7) & 0x7F
    else:  # pragma: no cover - guarded by the membership check above
        raise ValueError(f"unhandled MIDI-1-in-UMP Channel Voice type {msg_type!r}")

    word = (2 << 28) | (group << 24) | (opcode << 20) | (channel << 16) | (data1 << 8) | data2
    return word & 0xFFFFFFFF


# --- MIDI-1-in-UMP Channel Voice (message_type=2) — typed message decode ---
# Wired into _poll's 'decoded' array below, tried right after native
# Channel Voice (this decoder and _decode_channel_voice each reject fast on
# a message_type mismatch, so a raw word is never ambiguous between the
# two — see _poll's try-chain for the full 6-group order). Pure bit-math,
# no ALSA calls, no side effects on its own. Exact
# inverse of _encode_midi1_channel_voice — same word layout comment above
# applies (bits[31:28]=2, [27:24]=group, [23:20]=opcode, [19:16]=channel,
# [15:8]=data1, [7:0]=data2).
_M1CV_OPCODES_REV = {v: k for k, v in _M1CV_OPCODES.items()}


def _decode_midi1_channel_voice(word: int) -> dict:
    """Decode one raw 32-bit MIDI-1-in-UMP Channel Voice packet (UMP
    message_type=2) into a typed message dict — the exact inverse of
    _encode_midi1_channel_voice. Uses the SAME field names that function
    (and midi1.py's own _build_message) expects as input, so
    encode(decode(x)) round-trips cleanly.

    Raises ValueError if the top nibble isn't message_type=2, or if the
    opcode nibble isn't one of the 7 MIDI-1 Channel Voice opcodes this
    group covers (0x8-0xE minus nothing missing here, so any other value
    e.g. 0xF System Common/Real-Time is out of scope for this decoder).
    """
    word &= 0xFFFFFFFF
    msg_type_nibble = (word >> 28) & 0xF
    if msg_type_nibble != 2:
        raise ValueError(
            f"word has message_type={msg_type_nibble:#x}, expected 2 "
            f"(MIDI-1-in-UMP Channel Voice) — not decodable by "
            f"_decode_midi1_channel_voice"
        )
    group = (word >> 24) & 0xF
    opcode = (word >> 20) & 0xF
    channel = (word >> 16) & 0xF
    data1 = (word >> 8) & 0x7F
    data2 = word & 0x7F

    if opcode not in _M1CV_OPCODES_REV:
        raise ValueError(
            f"opcode nibble {opcode:#x} is not a MIDI-1-in-UMP Channel "
            f"Voice opcode this decoder covers (expected one of "
            f"{sorted(_M1CV_OPCODES.values())}) — cannot decode"
        )
    msg_type = _M1CV_OPCODES_REV[opcode]
    out = {"type": msg_type, "channel": channel, "group": group}
    # Stamp 'protocol':'midi1' on the 4 type names shared with native
    # Channel Voice (note_on/note_off/control_change/program_change) so a
    # decoded entry fed straight back into 'send' round-trips to the SAME
    # encoding it came from, rather than silently defaulting to native
    # MIDI 2.0 Channel Voice (the 'protocol' default) for those 4 shared
    # names. The 3 unambiguous names (polytouch/aftertouch/pitchwheel)
    # don't need it — there's no competing interpretation to disambiguate.
    if msg_type in _CV_OPCODES:
        out["protocol"] = "midi1"

    if msg_type in ("note_on", "note_off"):
        out["note"] = data1
        out["velocity"] = data2
    elif msg_type == "polytouch":
        out["note"] = data1
        out["value"] = data2
    elif msg_type == "control_change":
        out["control"] = data1
        out["value"] = data2
    elif msg_type == "program_change":
        out["program"] = data1
    elif msg_type == "aftertouch":
        out["value"] = data1
    elif msg_type == "pitchwheel":
        # Inverse of the encode side: reassemble the unsigned 14-bit wire
        # value (LSB=data1, MSB=data2), then re-center to midi1.py's
        # signed -8192..8191 convention (8192 = centered on the wire).
        unsigned14 = (data2 << 7) | data1
        out["pitch"] = unsigned14 - 8192
    else:  # pragma: no cover - guarded by the membership check above
        raise ValueError(
            f"unhandled MIDI-1-in-UMP Channel Voice type {msg_type!r}"
        )

    return out


# ---------------------------------------------------------------------------
# System Common / System Real Time messages, wrapped in UMP (message_type=1,
# single 32-bit word per message — channel-less, unlike message_type=2's
# Channel Voice group). Word layout confirmed against atsushieno/cmidi2's
# reference C implementation (cmidi2_ump_system_message, MIDI 2.0 spec
# section 7.6): bits[31:28]=1 (message type), bits[27:24]=group,
# bits[23:16]=status (the SAME full MIDI 1.0 status byte 0xF1-0xFF used on
# the wire — no channel nibble to combine, unlike Channel Voice's status+
# channel split), bits[15:8]=data byte 2 (7-bit), bits[7:0]=data byte 3
# (7-bit). Field names/semantics deliberately mirror midi1.py's own system
# message types exactly (quarter_frame's frame_type/frame_value, songpos's
# pos, song_select's song) — confirmed bit-for-bit against real `mido`
# output for each type (quarter_frame byte = frame_type<<4 | frame_value;
# songpos is 14-bit LSB-then-MSB same as MIDI 1.0 wire order) before
# writing this, not assumed from the spec doc alone.
_SYS_STATUS = {
    "quarter_frame": 0xF1,
    "songpos": 0xF2,
    "song_select": 0xF3,
    "tune_request": 0xF6,
    "clock": 0xF8,
    "start": 0xFA,
    "continue": 0xFB,
    "stop": 0xFC,
    "active_sensing": 0xFE,
    "reset": 0xFF,
}
_SYS_STATUS_REV = {v: k for k, v in _SYS_STATUS.items()}
# The 4 types that carry no data bytes at all (byte2/byte3 both 0) — every
# other type in _SYS_STATUS has at least one meaningful data byte.
_SYS_NO_DATA = {"tune_request", "clock", "start", "continue", "stop", "active_sensing", "reset"}


def _encode_system_message(message: dict) -> int:
    """Encode a System Common/System Real Time message dict into ONE UMP
    word (message_type=1). Inverse-paired with _decode_system_message
    below. Raises KeyError/ValueError on bad/missing fields, same
    convention as every other encoder in this module."""
    msg_type = message.get("type")
    if msg_type not in _SYS_STATUS:
        raise ValueError(
            f"unknown System Common/Real-Time type {msg_type!r} — "
            f"supported: {sorted(_SYS_STATUS)}"
        )
    group = message.get("group", 0) & 0xF
    status = _SYS_STATUS[msg_type]

    byte2 = 0
    byte3 = 0
    if msg_type == "quarter_frame":
        frame_type = message["frame_type"]
        frame_value = message["frame_value"]
        if not (0 <= frame_type <= 7):
            raise ValueError(f"frame_type must be 0-7, got {frame_type!r}")
        if not (0 <= frame_value <= 15):
            raise ValueError(f"frame_value must be 0-15, got {frame_value!r}")
        byte2 = ((frame_type & 0x7) << 4) | (frame_value & 0xF)
    elif msg_type == "songpos":
        pos = message.get("pos", 0)
        if not (0 <= pos <= 0x3FFF):
            raise ValueError(f"pos must be 0-16383, got {pos!r}")
        byte2 = pos & 0x7F          # LSB first, matches real MIDI 1.0 wire order
        byte3 = (pos >> 7) & 0x7F  # MSB
    elif msg_type == "song_select":
        song = message["song"]
        if not (0 <= song <= 127):
            raise ValueError(f"song must be 0-127, got {song!r}")
        byte2 = song & 0x7F
    elif msg_type in _SYS_NO_DATA:
        pass
    else:  # pragma: no cover - guarded by the membership check above
        raise ValueError(f"unhandled System Common/Real-Time type {msg_type!r}")

    return (
        (1 << 28)
        | (group << 24)
        | ((status & 0xFF) << 16)
        | ((byte2 & 0x7F) << 8)
        | (byte3 & 0x7F)
    )


def _decode_system_message(word: int) -> dict:
    """Decode ONE UMP word (message_type=1) into a typed message dict —
    exact inverse of _encode_system_message. Raises ValueError if the top
    nibble isn't message_type=1, or if the status byte doesn't match one
    of the 10 System Common/Real-Time types this covers."""
    msg_type_nibble = (word >> 28) & 0xF
    if msg_type_nibble != 1:
        raise ValueError(
            f"word has message_type={msg_type_nibble:#x}, expected 1 "
            "(System Common/Real Time)"
        )
    group = (word >> 24) & 0xF
    status = (word >> 16) & 0xFF
    byte2 = (word >> 8) & 0x7F
    byte3 = word & 0x7F

    msg_type = _SYS_STATUS_REV.get(status)
    if msg_type is None:
        raise ValueError(
            f"word has status={status:#x}, not one of the 10 supported "
            f"System Common/Real-Time statuses {sorted(hex(s) for s in _SYS_STATUS_REV)}"
        )

    out = {"type": msg_type, "group": group}
    if msg_type == "quarter_frame":
        out["frame_type"] = (byte2 >> 4) & 0x7
        out["frame_value"] = byte2 & 0xF
    elif msg_type == "songpos":
        out["pos"] = (byte3 << 7) | byte2
    elif msg_type == "song_select":
        out["song"] = byte2
    # the 6 _SYS_NO_DATA types add no extra fields — status alone (via
    # 'type') is the complete message.

    return out


# --- UMP Utility messages (message_type=0), MIDI 2.0 spec section 7.2 ---
# Bit layout confirmed against atsushieno/cmidi2's reference C implementation
# (cmidi2_ump_noop/jr_clock_direct/jr_timestamp_direct/dctpq/dcs, fetched live
# via curl from raw.githubusercontent.com/atsushieno/cmidi2/main/cmidi2.h,
# same source already used for the System Common group above): single 32-bit
# word, bits[31:28]=0 (message type), bits[27:24]=group (the cmidi2 direct-*
# helpers omit a group parameter entirely, always emitting group=0 — but the
# general UMP word format does reserve this nibble for group same as every
# other message type, so this tool exposes it as the usual optional field,
# default 0, for consistency with every other type here), bits[23:20]=status
# (4-bit nibble, only 5 values defined: 0=NOOP, 1=JR_CLOCK, 2=JR_TIMESTAMP,
# 3=DCTPQ, 4=DELTA_CLOCKSTAMP), then data: for NOOP no data at all; for
# JR_CLOCK/JR_TIMESTAMP/DCTPQ a 16-bit value occupies bits[15:0] with
# bits[19:16] reserved/0; for DELTA_CLOCKSTAMP a 20-bit value occupies
# bits[19:0] (confirmed by hand-computing cmidi2_ump_dcs(0xFFFFF) ->
# 0x4fffff, i.e. nibble at [23:20]=4 and all of [19:0] set, matching the
# "ticks accepts up to 20 bits" comment in the C source). Field names
# deliberately mirror the C helper functions' own parameter names
# (senderClockTime, senderClockTimestamp, dctpq, ticks) translated to
# snake_case, since there's no prior midi1.py precedent for Utility
# messages to mirror instead (midi1.py has no JR Clock/Timestamp concept —
# these are UMP-only, no MIDI-1 wire equivalent).
_UTIL_STATUS = {
    "noop": 0x0,
    "jr_clock": 0x1,
    "jr_timestamp": 0x2,
    "dctpq": 0x3,
    "delta_clockstamp": 0x4,
}
_UTIL_STATUS_REV = {v: k for k, v in _UTIL_STATUS.items()}


def _encode_utility_message(message: dict) -> int:
    """Encode a UMP Utility message dict into ONE UMP word (message_type=0).
    Inverse-paired with _decode_utility_message below. Raises ValueError on
    bad/missing fields, same convention as every other encoder here."""
    msg_type = message.get("type")
    if msg_type not in _UTIL_STATUS:
        raise ValueError(
            f"unknown Utility message type {msg_type!r} — "
            f"supported: {sorted(_UTIL_STATUS)}"
        )
    group = message.get("group", 0) & 0xF
    status = _UTIL_STATUS[msg_type]

    data20 = 0  # the field that actually lands in bits[19:0]
    if msg_type == "noop":
        pass
    elif msg_type == "jr_clock":
        t = message.get("sender_clock_time", 0)
        if not (0 <= t <= 0xFFFF):
            raise ValueError(f"sender_clock_time must be 0-65535, got {t!r}")
        data20 = t
    elif msg_type == "jr_timestamp":
        t = message.get("sender_clock_timestamp", 0)
        if not (0 <= t <= 0xFFFF):
            raise ValueError(f"sender_clock_timestamp must be 0-65535, got {t!r}")
        data20 = t
    elif msg_type == "dctpq":
        t = message.get("ticks_per_quarter_note", 0)
        if not (0 <= t <= 0xFFFF):
            raise ValueError(f"ticks_per_quarter_note must be 0-65535, got {t!r}")
        data20 = t
    elif msg_type == "delta_clockstamp":
        t = message.get("ticks", 0)
        if not (0 <= t <= 0xFFFFF):
            raise ValueError(f"ticks must be 0-1048575 (20-bit), got {t!r}")
        data20 = t
    else:  # pragma: no cover - guarded by the membership check above
        raise ValueError(f"unhandled Utility message type {msg_type!r}")

    return (
        (0 << 28)
        | (group << 24)
        | ((status & 0xF) << 20)
        | (data20 & 0xFFFFF)
    )


def _decode_utility_message(word: int) -> dict:
    """Decode ONE UMP word (message_type=0) into a typed message dict —
    exact inverse of _encode_utility_message. Raises ValueError if the top
    nibble isn't message_type=0, or if the status nibble doesn't match one
    of the 5 defined Utility statuses."""
    msg_type_nibble = (word >> 28) & 0xF
    if msg_type_nibble != 0:
        raise ValueError(
            f"word has message_type={msg_type_nibble:#x}, expected 0 (Utility)"
        )
    group = (word >> 24) & 0xF
    status = (word >> 20) & 0xF
    data20 = word & 0xFFFFF

    msg_type = _UTIL_STATUS_REV.get(status)
    if msg_type is None:
        raise ValueError(
            f"word has status={status:#x}, not one of the 5 supported "
            f"Utility statuses {sorted(hex(s) for s in _UTIL_STATUS_REV)}"
        )

    out = {"type": msg_type, "group": group}
    if msg_type == "jr_clock":
        out["sender_clock_time"] = data20 & 0xFFFF
    elif msg_type == "jr_timestamp":
        out["sender_clock_timestamp"] = data20 & 0xFFFF
    elif msg_type == "dctpq":
        out["ticks_per_quarter_note"] = data20 & 0xFFFF
    elif msg_type == "delta_clockstamp":
        out["ticks"] = data20
    # noop adds no extra fields — status alone (via 'type') is complete.

    return out


# --- System Exclusive 7-Bit-in-UMP (SysEx7, message_type=3) — encode+decode ---
# Wired into _send/_poll below, same as every other group in this file.
# Bit layout confirmed against
# atsushieno/cmidi2 (cmidi2_ump_sysex_get_packet_of + the independent
# cmidi2_ump_sysex7_direct formula, hand-traced to agree bit-for-bit —
# see memories/sysex-midi-tool-addition.md for the full derivation).
# py-midi2's system_exclusive_7bit was reviewed as a second reference and
# DISQUALIFIED — it has two independent, concretely-demonstrated bugs
# (reversed data-byte order, and a mis-sized status field that corrupts
# the group nibble) — so cmidi2 is the sole trusted source here, same as
# every other group in this file.
#
# Deliberate scope choice: this is a single-PACKET encoder/decoder, not a
# full-message auto-chunker. A real SysEx7 payload over 6 bytes needs
# multiple UMP packets (start/continue.../end) — same low-level
# building-block philosophy as every other group here, so the caller is
# responsible for splitting a long payload and calling this once per
# packet, computing 'status' per cmidi2's own chunking rule (complete if
# whole payload <=6 bytes; else start for packet 0, end for the last
# packet, continue for everything between). An auto-chunking convenience
# on top of this is a separate, later design decision — flagged, not
# blocking this step.
_SYSEX7_STATUS = {
    "complete": 0x0,
    "start": 0x1,
    "continue": 0x2,
    "end": 0x3,
}
_SYSEX7_STATUS_REV = {v: k for k, v in _SYSEX7_STATUS.items()}


def _encode_sysex7_message(message: dict):
    """Encode ONE UMP SysEx7 packet dict into (word0, word1),
    message_type=3. 'data' is a list of at most 6 payload bytes (0-127,
    bare — no leading 0xF0/trailing 0xF7, same convention as midi1's own
    'sysex' type). Raises ValueError on bad/missing fields, same
    convention as every other encoder here.
    """
    msg_type = message.get("type")
    if msg_type != "sysex7":
        raise ValueError(f"unknown SysEx7 message type {msg_type!r} — expected 'sysex7'")
    group = int(message.get("group", 0)) & 0xF
    status_name = message.get("status", "complete")
    if status_name not in _SYSEX7_STATUS:
        raise ValueError(
            f"status must be one of {sorted(_SYSEX7_STATUS)}, got {status_name!r}"
        )
    status = _SYSEX7_STATUS[status_name]

    data = message.get("data", [])
    if not isinstance(data, list) or len(data) > 6:
        raise ValueError(f"data must be a list of at most 6 bytes (0-127), got {data!r}")
    for b in data:
        if not (0 <= int(b) <= 0x7F):
            raise ValueError(f"each SysEx7 data byte must be 0-127, got {b!r}")
    num_bytes = len(data)
    padded = [int(b) for b in data] + [0] * (6 - num_bytes)

    byte0 = (3 << 4) | group           # message_type nibble + group nibble
    byte1 = (status << 4) | num_bytes  # status nibble + byte-count nibble
    word0 = (byte0 << 24) | (byte1 << 16) | (padded[0] << 8) | padded[1]
    word1 = (padded[2] << 24) | (padded[3] << 16) | (padded[4] << 8) | padded[5]
    return word0 & 0xFFFFFFFF, word1 & 0xFFFFFFFF


def _decode_sysex7_message(word0: int, word1: int) -> dict:
    """Decode ONE raw 2-word (64-bit) SysEx7 UMP packet into a typed
    message dict — exact inverse of _encode_sysex7_message. Raises
    ValueError if the top nibble isn't message_type=3, the status nibble
    doesn't match one of the 4 defined SysEx7 statuses, or the byte-count
    nibble exceeds the 6-byte-per-packet maximum."""
    msg_type_nibble = (word0 >> 28) & 0xF
    if msg_type_nibble != 3:
        raise ValueError(
            f"word0 has message_type={msg_type_nibble:#x}, expected 3 (SysEx7)"
        )
    group = (word0 >> 24) & 0xF
    status = (word0 >> 20) & 0xF
    num_bytes = (word0 >> 16) & 0xF
    if num_bytes > 6:
        raise ValueError(f"byte-count nibble {num_bytes} exceeds max of 6 for SysEx7")

    status_name = _SYSEX7_STATUS_REV.get(status)
    if status_name is None:
        raise ValueError(
            f"word0 has status={status:#x}, not one of the 4 SysEx7 statuses "
            f"{sorted(hex(s) for s in _SYSEX7_STATUS_REV)}"
        )

    all_bytes = [
        (word0 >> 8) & 0xFF,
        word0 & 0xFF,
        (word1 >> 24) & 0xFF,
        (word1 >> 16) & 0xFF,
        (word1 >> 8) & 0xFF,
        word1 & 0xFF,
    ]
    data = all_bytes[:num_bytes]

    return {"type": "sysex7", "group": group, "status": status_name, "data": data}


# --- System Exclusive 8-Bit/Mixed Data Set-in-UMP (SysEx8, message_type=5)
# --- encode+decode. Wired into _send/_poll below, same as every other
# group in this file. Bit layout confirmed against
# atsushieno/cmidi2's shared cmidi2_ump_sysex_get_packet_of helper (the
# same generic function SysEx7 above already uses, called here with
# radix=13, hasStreamId=true instead of SysEx7's radix=6,
# hasStreamId=false) — see memories/sysex-midi-tool-addition.md for the
# full derivation. py-midi2's system_exclusive_8bit was reviewed as a
# second reference and DISQUALIFIED for the SAME two bugs already found
# in its system_exclusive_7bit sibling (reversed data-byte order, and a
# mis-sized status field that corrupts the group nibble) — cmidi2 is the
# sole trusted source here, same as every other group in this file.
#
# Deliberate scope choice, same as SysEx7: this is a single-PACKET
# encoder/decoder, not a full-message auto-chunker. Caller is
# responsible for splitting a long payload across multiple
# start/continue/.../end packets and calling this once per packet.
#
# Reuses the SAME 4-value status enum as SysEx7 (_SYSEX7_STATUS/_REV) —
# cmidi2's shared helper uses identical status semantics
# (complete/start/continue/end) for both groups, just with a different
# radix and an extra stream_id byte for SysEx8.
_SYSEX8_MAX_DATA_BYTES = 13


def _encode_sysex8_message(message: dict):
    """Encode ONE UMP SysEx8 packet dict into (word0, word1, word2,
    word3), message_type=5. 'data' is a list of at most 13 payload bytes
    (0-255 each — FULL 8-bit range, unlike SysEx7's 7-bit-only payload).
    'stream_id' (0-255) is SysEx8-specific, no SysEx7 equivalent. Raises
    ValueError on bad/missing fields, same convention as every other
    encoder here.
    """
    msg_type = message.get("type")
    if msg_type != "sysex8":
        raise ValueError(f"unknown SysEx8 message type {msg_type!r} — expected 'sysex8'")
    group = int(message.get("group", 0)) & 0xF
    status_name = message.get("status", "complete")
    if status_name not in _SYSEX7_STATUS:
        raise ValueError(
            f"status must be one of {sorted(_SYSEX7_STATUS)}, got {status_name!r}"
        )
    status = _SYSEX7_STATUS[status_name]

    stream_id = int(message.get("stream_id", 0))
    if not (0 <= stream_id <= 0xFF):
        raise ValueError(f"stream_id must be 0-255, got {stream_id!r}")

    data = message.get("data", [])
    if not isinstance(data, list) or len(data) > _SYSEX8_MAX_DATA_BYTES:
        raise ValueError(
            f"data must be a list of at most {_SYSEX8_MAX_DATA_BYTES} bytes (0-255), "
            f"got {data!r}"
        )
    for b in data:
        if not (0 <= int(b) <= 0xFF):
            raise ValueError(f"each SysEx8 data byte must be 0-255, got {b!r}")
    num_data_bytes = len(data)
    padded = [int(b) for b in data] + [0] * (_SYSEX8_MAX_DATA_BYTES - num_data_bytes)

    byte0 = (5 << 4) | group                    # message_type nibble + group nibble
    byte1 = (status << 4) | (num_data_bytes + 1)  # status nibble + (size+1, includes stream_id byte)

    word0 = (byte0 << 24) | (byte1 << 16) | (stream_id << 8) | padded[0]
    word1 = (padded[1] << 24) | (padded[2] << 16) | (padded[3] << 8) | padded[4]
    word2 = (padded[5] << 24) | (padded[6] << 16) | (padded[7] << 8) | padded[8]
    word3 = (padded[9] << 24) | (padded[10] << 16) | (padded[11] << 8) | padded[12]
    return (
        word0 & 0xFFFFFFFF,
        word1 & 0xFFFFFFFF,
        word2 & 0xFFFFFFFF,
        word3 & 0xFFFFFFFF,
    )


def _decode_sysex8_message(word0: int, word1: int, word2: int, word3: int) -> dict:
    """Decode ONE raw 4-word (128-bit) SysEx8 UMP packet into a typed
    message dict — exact inverse of _encode_sysex8_message. Raises
    ValueError if the top nibble isn't message_type=5, the status nibble
    doesn't match one of the 4 defined statuses, or the num-bytes nibble
    is 0 (invalid — must be >=1 to account for the stream_id byte) or
    exceeds the 14-max (13 data bytes + 1 for stream_id)."""
    msg_type_nibble = (word0 >> 28) & 0xF
    if msg_type_nibble != 5:
        raise ValueError(
            f"word0 has message_type={msg_type_nibble:#x}, expected 5 (SysEx8/MDS)"
        )
    group = (word0 >> 24) & 0xF
    status = (word0 >> 20) & 0xF
    num_bytes_field = (word0 >> 16) & 0xF
    if not (1 <= num_bytes_field <= _SYSEX8_MAX_DATA_BYTES + 1):
        raise ValueError(
            f"num-bytes nibble {num_bytes_field} out of range 1-"
            f"{_SYSEX8_MAX_DATA_BYTES + 1} for SysEx8 (must include the stream_id byte)"
        )
    num_data_bytes = num_bytes_field - 1  # wire field includes the stream_id byte

    status_name = _SYSEX7_STATUS_REV.get(status)
    if status_name is None:
        raise ValueError(
            f"word0 has status={status:#x}, not one of the 4 SysEx statuses "
            f"{sorted(hex(s) for s in _SYSEX7_STATUS_REV)}"
        )

    stream_id = (word0 >> 8) & 0xFF
    all_bytes = [
        word0 & 0xFF,
        (word1 >> 24) & 0xFF,
        (word1 >> 16) & 0xFF,
        (word1 >> 8) & 0xFF,
        word1 & 0xFF,
        (word2 >> 24) & 0xFF,
        (word2 >> 16) & 0xFF,
        (word2 >> 8) & 0xFF,
        word2 & 0xFF,
        (word3 >> 24) & 0xFF,
        (word3 >> 16) & 0xFF,
        (word3 >> 8) & 0xFF,
        word3 & 0xFF,
    ]
    data = all_bytes[:num_data_bytes]

    return {
        "type": "sysex8",
        "group": group,
        "status": status_name,
        "stream_id": stream_id,
        "data": data,
    }


TOOLS = [
    {
        "name": "midi2",
        "description": (
            "MIDI 2.0 / UMP (Universal MIDI Packet) device discovery and "
            "I/O, via direct cffi bindings to libasound's ALSA "
            "sequencer API (no mature Python MIDI-2.0 library exists yet — "
            "see memories/sysex-midi-tool-addition.md). 'send'/'poll' "
            "support EITHER a raw 4-word (128-bit) UMP packet (a plain "
            "integer list) OR a typed message dict covering all six UMP "
            "message groups (Channel Voice, MIDI-1-in-UMP, System Common/"
            "Real-Time, Utility, SysEx7, SysEx8 — see below). "
            "Actions: 'list_devices' shells out to `aconnect -l` and "
            "returns structured ALSA sequencer clients/ports, flagging "
            "which ones advertise UMP-MIDI2 support (e.g. PipeWire's own "
            "internal UMP clients). 'open' creates a new port on this "
            "process's single shared ALSA seq client, as either 'direction' "
            "input or output; pass 'target_client'+'target_port' to set its "
            "destination client:port (e.g. another handle's own port, for "
            "a self-loopback test, or a real device once MIDI 2.0 "
            "hardware exists) — REQUIRED on an output handle before "
            "'send' will work (events are addressed directly to this "
            "target; a matching ALSA subscription is also attempted for "
            "visibility in tools like `aconnect -l`, but a failure there "
            "is only a warning, not fatal, since direct addressing doesn't "
            "require it). Returns a handle string. 'send' writes a UMP "
            "packet on an open output handle — EITHER a raw 'words' list "
            "(up to 4 unsigned-32-bit ints, zero-padded if shorter) OR a "
            "typed 'message' dict (pass exactly one, not both). 'message' "
            "supports all six UMP message groups: native MIDI 2.0 "
            "Channel Voice (message_type=4, "
            "wide 16/32-bit data), classic MIDI-1-in-UMP Channel Voice "
            "(message_type=2, 7-bit data, single-word packets), and System "
            "Common/System Real Time-in-UMP (message_type=1, single-word, "
            "channel-less broadcast messages: 'quarter_frame' needs "
            "'frame_type' 0-7 + 'frame_value' 0-15, 'songpos' needs 'pos' "
            "0-16383, 'song_select' needs 'song' 0-127, and 'tune_request'/"
            "'clock'/'start'/'continue'/'stop'/'active_sensing'/'reset' "
            "need no extra fields — field names/semantics match midi1.py's "
            "own system-message types exactly, cross-checked bit-for-bit "
            "against real `mido` output and against the reference "
            "atsushieno/cmidi2 C implementation before shipping). All "
            "message_type=1 types accept optional 'group' (0-15, default "
            "0) only — no 'channel' field, since System Common/Real-Time "
            "messages are channel-less by spec. Fourth group: Utility "
            "messages-in-UMP (message_type=0, single-word, also channel-"
            "less — no MIDI 1.0 wire equivalent exists for these, they're "
            "UMP-native timing/jitter-reduction concepts): 'noop' needs no "
            "extra fields; 'jr_clock' needs 'sender_clock_time' 0-65535; "
            "'jr_timestamp' needs 'sender_clock_timestamp' 0-65535; "
            "'dctpq' needs 'ticks_per_quarter_note' 0-65535; "
            "'delta_clockstamp' needs 'ticks' 0-1048575 (20-bit, wider "
            "than the other three's 16-bit fields) — bit layout confirmed "
            "against the reference atsushieno/cmidi2 C implementation. "
            "All 5 Utility types accept optional 'group' (0-15, default "
            "0) only, same as System Common. Fifth group: System "
            "Exclusive 7-Bit-in-UMP ('sysex7', message_type=3, 2 words/"
            "64 bits) — encodes/decodes ONE UMP packet at a time, NOT a "
            "full multi-packet sysex transfer in one call (a real sysex "
            "dump over 6 bytes needs multiple 'send' calls, one per "
            "packet — caller computes the chunking, same low-level-"
            "building-block philosophy as every other group here). "
            "Needs optional 'status' (one of complete/start/continue/end, "
            "default 'complete' — 'complete' for a whole message that "
            "fits in one packet, 'start' for packet 0 of a longer "
            "payload, 'continue' for every packet after that except the "
            "last, 'end' for the last one) and optional 'data' (list of "
            "at most 6 bytes, 0-127 each, default empty list — bare 7-bit "
            "payload only, same convention as midi1.py's own 'sysex' type: "
            "do NOT include the leading 0xF0 or trailing 0xF7). Accepts "
            "optional 'group' (0-15, default 0) only, same as System "
            "Common/Utility — no 'channel' field, sysex is channel-less. "
            "Bit layout confirmed against the reference atsushieno/cmidi2 "
            "C implementation (cmidi2_ump_sysex_get_packet_of and the "
            "independent cmidi2_ump_sysex7_direct formula, hand-traced to "
            "agree bit-for-bit); a second reference (py-midi2's "
            "system_exclusive_7bit) was reviewed and found to have two "
            "independent bugs (reversed data-byte order, and a mis-sized "
            "status field that corrupts the group nibble), so it was "
            "disqualified as a cross-check — cmidi2 alone is the trusted "
            "source for this group, same as every other group here. "
            "Sixth and final group: System Exclusive 8-Bit/Mixed Data "
            "Set-in-UMP ('sysex8', message_type=5, 4 words/128 bits) — "
            "shares its wire format with SysEx7 via the SAME cmidi2 "
            "shared-helper design (a common header/status-byte shape), "
            "just with a larger radix and one extra field: encodes/"
            "decodes ONE UMP packet at a time, same single-packet-not-"
            "full-transfer scope as sysex7 (caller chunks a longer "
            "payload across multiple 'send' calls). Needs optional "
            "'status' (complete/start/continue/end, same 4 values/"
            "meaning as sysex7's own 'status', default 'complete'), "
            "optional 'stream_id' (0-255, default 0 — SysEx8-specific, "
            "no sysex7 equivalent, identifies which of possibly several "
            "concurrent SysEx8 streams this packet belongs to), and "
            "optional 'data' (list of at most 13 bytes, 0-255 each, "
            "default empty list — FULL 8-bit range, unlike sysex7's "
            "7-bit-only payload, since SysEx8 carries raw 8-bit data "
            "rather than MIDI-1-style 7-bit sysex bytes). Accepts "
            "optional 'group' (0-15, default 0) only, same as sysex7 — "
            "no 'channel' field, sysex is channel-less. Bit layout "
            "confirmed against the same trusted cmidi2 reference used "
            "for every other group (the shared "
            "cmidi2_ump_sysex_get_packet_of helper, called with "
            "radix=13/hasStreamId=true for this group vs. sysex7's "
            "radix=6/hasStreamId=false); py-midi2's "
            "system_exclusive_8bit was reviewed as a second reference "
            "and disqualified for the SAME two bugs already found in "
            "its system_exclusive_7bit sibling (reversed data-byte "
            "order, mis-sized status field), so cmidi2 alone is trusted "
            "here too. Four "
            "type names exist in "
            "BOTH groups with identical spelling — 'note_on', 'note_off', "
            "'control_change', 'program_change' — since neither group "
            "gave them an alternate name; for these four, an optional "
            "'protocol' field ('midi2', the default if omitted, or "
            "'midi1') picks which encoding to use — omitting it preserves "
            "the pre-existing MIDI 2.0 Channel Voice behavior. Three "
            "MIDI-1-only names need no 'protocol' at all since they're "
            "unambiguous: 'polytouch' (poly key pressure), 'aftertouch' "
            "(channel pressure), 'pitchwheel' (pitch bend) — these three "
            "use midi1.py's own field names ('note'/'value', 'value', "
            "'pitch' respectively) since MIDI-1-in-UMP IS classic MIDI 1.0 "
            "data wrapped in a UMP header; NOTE the kernel/ALSA UMP layer "
            "auto-translates sent MIDI-1-in-UMP packets into native "
            "MIDI 2.0 Channel Voice packets in transit for a client "
            "registered in UMP-MIDI-2.0 mode (as this tool's shared "
            "client is) — confirmed spec-faithful (the documented 7-bit-"
            "to-16/32-bit bit-scaling algorithm), so 'poll' will show the "
            "translated wider values, not the original 7-bit ones, when "
            "looped back through this same process. Native Channel Voice "
            "types: 'note_on'/'note_off' (need 'note' 0-127, "
            "'velocity' 0-65535, optional 'attribute_type'/'attribute_data' "
            "0-255/0-65535), 'poly_pressure' (needs 'note', 'value' "
            "0-4294967295), 'control_change' (needs 'index' 0-127, "
            "'value' 0-4294967295), 'program_change' (needs 'program' "
            "0-127, optional 'bank_valid' bool + 'bank_msb'/'bank_lsb' "
            "0-127), 'channel_pressure' (needs 'value'), 'pitch_bend' "
            "(optional 'value' 0-4294967295, default 0x80000000 = "
            "centered), 'per_note_pitch_bend' (needs 'note', optional "
            "'value' as above), 'reg_per_note_controller'/"
            "'asn_per_note_controller' (need 'note', 'index' 0-255, "
            "'value'), 'per_note_management' (needs 'note', optional "
            "'detach'/'reset' bools), 'reg_controller' (RPN)/"
            "'asn_controller' (NRPN)/'rel_reg_controller'/"
            "'rel_asn_controller' (need 'bank' 0-127, 'index' 0-127, "
            "'value'). All types accept optional 'channel' (0-15, default "
            "0) and 'group' (0-15, default 0). 'poll' drains any buffered "
            "raw UMP packets for "
            "an open input handle, returning each as a list of up to 4 "
            "hex-string words in a 'messages' array, PLUS a parallel "
            "'decoded' array (same length/order) with a best-effort typed "
            "decode of each packet — tried in order as native MIDI 2.0 "
            "Channel Voice (message_type=4), classic MIDI-1-in-UMP "
            "Channel Voice (message_type=2), System Common/Real-"
            "Time-in-UMP (message_type=1), Utility-in-UMP "
            "(message_type=0), then SysEx7-in-UMP (message_type=3), "
            "then SysEx8/Mixed-Data-Set-in-UMP (message_type=5) (the "
            "message_type nibble "
            "unambiguously identifies which group produced a packet, so "
            "this is a deterministic try-then-fallback chain, not a "
            "guess) — decoded shape/field-names match whichever group "
            "matched, same as 'send' accepts as its own 'message' dict "
            "(a type=2 decode includes 'protocol':'midi1' so it can be "
            "fed straight back into another 'send' call and still route "
            "to the same encoding), so a decoded entry can be fed "
            "straight back into another 'send' call — an entry is "
            "`null` only for a malformed/reserved status or "
            "message_type nibble, since all six originally-scoped UMP "
            "message groups are now covered. 'close' deletes the underlying ALSA "
            "port and forgets the handle. Handles/ports only live for the "
            "current ResearchMesh process — reopen after a restart."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list_devices", "open", "close", "send", "poll"],
                    "description": "Which MIDI 2.0/UMP operation to perform.",
                },
                "direction": {
                    "type": "string",
                    "enum": ["input", "output"],
                    "description": "Which side to open the port as. Required for 'open'.",
                },
                "target_client": {
                    "type": "integer",
                    "description": (
                        "ALSA sequencer client id to connect the new port "
                        "to immediately on 'open' (as returned by "
                        "'list_devices', or another handle's own client id "
                        "for a self-loopback). Optional; pair with "
                        "'target_port'."
                    ),
                },
                "target_port": {
                    "type": "integer",
                    "description": (
                        "ALSA sequencer port number (within 'target_client') "
                        "to connect to on 'open'. Optional; pair with "
                        "'target_client'."
                    ),
                },
                "handle": {
                    "type": "string",
                    "description": (
                        "Handle returned by a previous 'open' call. "
                        "Required for 'close', 'send', and 'poll'."
                    ),
                },
                "words": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Raw UMP packet for 'send' — up to 4 unsigned-32-bit "
                        "ints (the 32/64/96/128-bit UMP word(s)), zero-padded "
                        "if fewer than 4 given. Use this OR 'message', not "
                        "both."
                    ),
                },
                "message": {
                    "type": "object",
                    "description": (
                        "Typed message for 'send' — use this OR 'words', "
                        "not both. Covers native MIDI 2.0 Channel Voice "
                        "AND classic MIDI-1-in-UMP Channel Voice (see the "
                        "tool description above for the 'protocol' field "
                        "that disambiguates the 4 shared type names). See "
                        "the tool description above for the full field "
                        "list per 'type'."
                    ),
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "note_on", "note_off", "poly_pressure",
                                "control_change", "program_change",
                                "channel_pressure", "pitch_bend",
                                "per_note_pitch_bend",
                                "reg_per_note_controller",
                                "asn_per_note_controller",
                                "per_note_management",
                                "reg_controller", "asn_controller",
                                "rel_reg_controller", "rel_asn_controller",
                                "polytouch", "aftertouch", "pitchwheel",
                                "quarter_frame", "songpos", "song_select",
                                "tune_request", "clock", "start",
                                "continue", "stop", "active_sensing",
                                "reset",
                                "noop", "jr_clock", "jr_timestamp",
                                "dctpq", "delta_clockstamp",
                                "sysex7", "sysex8",
                            ],
                        },
                        "protocol": {
                            "type": "string",
                            "enum": ["midi2", "midi1"],
                            "description": (
                                "Only meaningful for the 4 shared type "
                                "names (note_on/note_off/control_change/"
                                "program_change). Default 'midi2' (or "
                                "omit) keeps the pre-existing native "
                                "Channel Voice encoding; 'midi1' routes to "
                                "the classic MIDI-1-in-UMP encoding "
                                "instead. Ignored for every other type."
                            ),
                        },
                        "channel": {"type": "integer"},
                        "group": {"type": "integer"},
                        "note": {"type": "integer"},
                        "velocity": {"type": "integer"},
                        "attribute_type": {"type": "integer"},
                        "attribute_data": {"type": "integer"},
                        "index": {"type": "integer"},
                        "value": {"type": "integer"},
                        "bank": {"type": "integer"},
                        "program": {"type": "integer"},
                        "bank_valid": {"type": "boolean"},
                        "bank_msb": {"type": "integer"},
                        "bank_lsb": {"type": "integer"},
                        "detach": {"type": "boolean"},
                        "reset": {"type": "boolean"},
                        "control": {
                            "type": "integer",
                            "description": (
                                "MIDI-1-in-UMP 'control_change' only "
                                "(0-127) — the CC controller number. "
                                "(Native Channel Voice control_change "
                                "uses 'index' instead, matching the field "
                                "above.)"
                            ),
                        },
                        "pitch": {
                            "type": "integer",
                            "description": (
                                "MIDI-1-in-UMP 'pitchwheel' only — signed "
                                "-8192..8191, 0 = centered, same convention "
                                "as midi1.py's own 'pitchwheel'. (Native "
                                "Channel Voice pitch_bend uses 'value' "
                                "instead, unsigned 0-4294967295.)"
                            ),
                        },
                        "frame_type": {
                            "type": "integer",
                            "description": (
                                "System Common 'quarter_frame' only — MTC "
                                "message type, 0-7."
                            ),
                        },
                        "frame_value": {
                            "type": "integer",
                            "description": (
                                "System Common 'quarter_frame' only — MTC "
                                "nibble data, 0-15."
                            ),
                        },
                        "pos": {
                            "type": "integer",
                            "description": (
                                "System Common 'songpos' only — 14-bit song "
                                "position, 0-16383."
                            ),
                        },
                        "song": {
                            "type": "integer",
                            "description": (
                                "System Common 'song_select' only — song "
                                "number, 0-127."
                            ),
                        },
                        "sender_clock_time": {
                            "type": "integer",
                            "description": (
                                "Utility 'jr_clock' only — 16-bit sender "
                                "clock time, 0-65535."
                            ),
                        },
                        "sender_clock_timestamp": {
                            "type": "integer",
                            "description": (
                                "Utility 'jr_timestamp' only — 16-bit "
                                "sender clock timestamp, 0-65535."
                            ),
                        },
                        "ticks_per_quarter_note": {
                            "type": "integer",
                            "description": (
                                "Utility 'dctpq' only — 16-bit delta "
                                "clockstamp ticks-per-quarter-note, "
                                "0-65535."
                            ),
                        },
                        "ticks": {
                            "type": "integer",
                            "description": (
                                "Utility 'delta_clockstamp' only — 20-bit "
                                "tick count, 0-1048575 (wider than the "
                                "other 3 Utility types' 16-bit fields)."
                            ),
                        },
                        "status": {
                            "type": "string",
                            "enum": ["complete", "start", "continue", "end"],
                            "description": (
                                "'sysex7' or 'sysex8' only — which role "
                                "this ONE UMP packet plays in a (possibly "
                                "multi-packet) System Exclusive message: "
                                "'complete' for a whole message that fits "
                                "in one packet (<=6 bytes for sysex7, "
                                "<=13 for sysex8), or 'start'/'continue'/"
                                "'end' when chunking a longer payload "
                                "across multiple 'send' calls (max payload "
                                "per packet is 6 bytes for sysex7, 13 for "
                                "sysex8 — caller does the chunking, this "
                                "tool encodes/decodes one packet at a "
                                "time). Default 'complete' if omitted. "
                                "Same 4-value enum/meaning shared by both "
                                "sysex groups."
                            ),
                        },
                        "data": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": (
                                "'sysex7' or 'sysex8' only — payload bytes "
                                "for this ONE packet. For 'sysex7': up to "
                                "6 bytes, 0-127 each (7-bit MIDI sysex "
                                "data). For 'sysex8': up to 13 bytes, "
                                "0-255 each (FULL 8-bit range — SysEx8 "
                                "carries raw 8-bit data, not MIDI-1-style "
                                "7-bit sysex bytes). Bare data only, same "
                                "convention as midi1.py's own 'sysex' type "
                                "— do NOT include the leading 0xF0 or "
                                "trailing 0xF7. Default empty list if "
                                "omitted."
                            ),
                        },
                        "stream_id": {
                            "type": "integer",
                            "description": (
                                "'sysex8' only — 0-255, default 0. "
                                "Identifies which of possibly several "
                                "concurrent SysEx8 streams this packet "
                                "belongs to. No 'sysex7' equivalent — "
                                "sysex7 has no stream concept."
                            ),
                        },
                    },
                },
            },
            "required": ["action"],
        },
    },
]

_TOOL_NAMES = {t["name"] for t in TOOLS}

# Lazily-created single ALSA seq client shared by every 'open' call this
# process makes — same process-lifetime-only statefulness caveat as
# midi1.py's _OPEN_PORTS (does not survive a ResearchMesh restart).
_SEQ = None
_MY_CLIENT_ID = None

# handle -> {"port": int, "direction": "input"/"output"}
_OPEN_PORTS: dict = {}
_HANDLE_COUNTER = itertools.count(1)

# ALSA port number -> list of already-drained-but-not-yet-delivered raw UMP
# word lists, so two independently-polling input handles don't steal each
# other's events out of the one shared client's queue. See module docstring.
_PENDING: dict = {}


def _next_handle() -> str:
    return f"midi2-{next(_HANDLE_COUNTER)}"


def _ensure_seq():
    """Lazily open+configure the one shared ALSA seq client for this process."""
    global _SEQ, _MY_CLIENT_ID
    if _SEQ is not None:
        return _SEQ
    handle_p = ffi.new("snd_seq_t **")
    _check(_lib.snd_seq_open(handle_p, b"default", SND_SEQ_OPEN_DUPLEX, 0), "snd_seq_open")
    seq = handle_p[0]
    _check(_lib.snd_seq_set_client_name(seq, b"researchmesh-midi2"), "set_client_name")
    _check(
        _lib.snd_seq_set_client_midi_version(seq, SND_SEQ_CLIENT_UMP_MIDI_2_0),
        "set_client_midi_version",
    )
    _check(_lib.snd_seq_nonblock(seq, SND_SEQ_NONBLOCK), "nonblock")
    _MY_CLIENT_ID = _check(_lib.snd_seq_client_id(seq), "client_id")
    _SEQ = seq
    return _SEQ


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "midi2":
        return json.dumps({"error": f"unknown midi2 tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


def _err(message: str) -> str:
    return json.dumps({"error": message})


def _run(tool_input: dict) -> str:
    action = tool_input.get("action")
    if action == "list_devices":
        return _list_devices()
    if _lib is None:
        return _err(
            "cffi/libasound initialization failed — `pip install cffi` and/or "
            "ensure libasound2 is installed (needs libasound.so.2 at runtime) "
            f"to use midi2 actions other than list_devices ({_MIDI2_INIT_ERROR})"
        )
    if action == "open":
        return _open(tool_input)
    if action == "close":
        return _close(tool_input)
    if action == "send":
        return _send(tool_input)
    if action == "poll":
        return _poll(tool_input)
    return _err(
        f"unknown action {action!r} — expected one of "
        "list_devices, open, close, send, poll"
    )


# Matches e.g. "client 144: 'PipeWire-System' [type=user,UMP-MIDI2,pid=3777]"
_CLIENT_RE = re.compile(r"^client (\d+): '(.*)' \[(.*)\]$")
# Matches e.g. "    0 'input           '" (port lines are indented, plain int+name)
_PORT_RE = re.compile(r"^\s{4}(\d+) '(.*?)\s*'$")


def _list_devices() -> str:
    try:
        out = subprocess.run(
            ["aconnect", "-l"], capture_output=True, text=True, timeout=5, check=True
        )
    except FileNotFoundError:
        return _err("'aconnect' binary not found on this system")
    except subprocess.CalledProcessError as e:
        return _err(f"aconnect -l failed: {e.stderr.strip() or e}")
    except subprocess.TimeoutExpired:
        return _err("aconnect -l timed out")

    clients: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in out.stdout.splitlines():
        m = _CLIENT_RE.match(line)
        if m:
            client_id, name, flags = m.groups()
            current = {
                "client": int(client_id),
                "name": name,
                "type": flags,
                "ump_midi2": "UMP-MIDI2" in flags,
                "ports": [],
            }
            clients.append(current)
            continue
        m = _PORT_RE.match(line)
        if m and current is not None:
            port_id, port_name = m.groups()
            current["ports"].append({"port": int(port_id), "name": port_name})
    return json.dumps({"status": "ok", "clients": clients})


def _open(tool_input: dict) -> str:
    direction = tool_input.get("direction")
    if direction not in ("input", "output"):
        return _err("'direction' must be 'input' or 'output' for 'open'")
    target_client = tool_input.get("target_client")
    target_port = tool_input.get("target_port")

    try:
        seq = _ensure_seq()
        caps = (
            SND_SEQ_PORT_CAP_READ | SND_SEQ_PORT_CAP_WRITE |
            SND_SEQ_PORT_CAP_SUBS_READ | SND_SEQ_PORT_CAP_SUBS_WRITE |
            SND_SEQ_PORT_CAP_DUPLEX
        )
        port_type = SND_SEQ_PORT_TYPE_MIDI_UMP | SND_SEQ_PORT_TYPE_APPLICATION
        port_name = f"researchmesh-midi2-{direction}".encode()
        port = _check(
            _lib.snd_seq_create_simple_port(seq, port_name, caps, port_type),
            "create_simple_port",
        )

        connect_warning = None
        if target_client is not None and target_port is not None:
            # Best-effort: an explicit subscription is nice for visibility
            # (shows up in `aconnect -l`) and is how PHYSICAL/kernel MIDI
            # ports actually deliver, but is NOT required for our own
            # direct-queue-addressed send() below to work (confirmed live,
            # Phase-9 POC-1 research: subscribing to PipeWire's internal UMP
            # client failed with "Operation not permitted", yet an
            # explicit-dest direct-queue send still succeeded regardless —
            # ALSA delivers direct-queue events straight to the addressed
            # inbox independent of the subscription graph). So a connect
            # failure here is logged, not fatal.
            try:
                if direction == "output":
                    _check(
                        _lib.snd_seq_connect_to(seq, port, int(target_client), int(target_port)),
                        "connect_to",
                    )
                else:
                    _check(
                        _lib.snd_seq_connect_from(seq, port, int(target_client), int(target_port)),
                        "connect_from",
                    )
            except Exception as e:
                connect_warning = f"{type(e).__name__}: {e}"
    except Exception as e:
        return _err(f"failed to open {direction} port: {type(e).__name__}: {e}")

    target = None
    if target_client is not None and target_port is not None:
        target = (int(target_client), int(target_port))

    handle = _next_handle()
    _OPEN_PORTS[handle] = {"port": port, "direction": direction, "target": target}
    _PENDING.setdefault(port, [])
    result = {
        "status": "ok",
        "handle": handle,
        "direction": direction,
        "client": _MY_CLIENT_ID,
        "port": port,
    }
    if connect_warning:
        result["connect_warning"] = connect_warning
    return json.dumps(result)


def _close(tool_input: dict) -> str:
    handle = tool_input.get("handle")
    entry = _OPEN_PORTS.pop(handle, None) if handle else None
    if entry is None:
        return _err(f"no open port for handle {handle!r}")
    port = entry["port"]
    _PENDING.pop(port, None)
    try:
        if _SEQ is not None:
            _check(_lib.snd_seq_delete_simple_port(_SEQ, port), "delete_simple_port")
    except Exception as e:
        return _err(f"error closing handle {handle!r}: {type(e).__name__}: {e}")
    return json.dumps({"status": "ok", "handle": handle, "closed": True})


def _send(tool_input: dict) -> str:
    handle = tool_input.get("handle")
    entry = _OPEN_PORTS.get(handle) if handle else None
    if entry is None:
        return _err(f"no open port for handle {handle!r}")
    if entry["direction"] != "output":
        return _err(f"handle {handle!r} is an input port, cannot send on it")
    target = entry.get("target")
    if target is None:
        return _err(
            f"handle {handle!r} has no destination — pass 'target_client'/"
            "'target_port' when 'open'-ing an output handle"
        )

    message = tool_input.get("message")
    words = tool_input.get("words")
    if message is not None and words is not None:
        return _err("pass either 'message' or 'words' to 'send', not both")
    if message is not None:
        msg_type = message.get("type")
        # Disambiguator for the 4 type names that exist in BOTH message
        # groups (note_on/note_off/control_change/program_change — same
        # names in native MIDI 2.0 Channel Voice AND classic MIDI-1-in-UMP,
        # since neither group invented an alternate name for these 4 the
        # way it did for poly_pressure/channel_pressure/pitch_bend vs.
        # polytouch/aftertouch/pitchwheel). Optional 'protocol' field:
        # 'midi1' explicitly routes an ambiguous name to the MIDI-1-in-UMP
        # encoder; anything else (including omitted) DEFAULTS to the
        # pre-existing Channel Voice behavior for those 4 names, so every
        # already-working call from before this sub-step is unaffected.
        # The 3 MIDI-1-only names (polytouch/aftertouch/pitchwheel) don't
        # need 'protocol' at all — they're unambiguous by name alone.
        protocol = message.get("protocol")
        use_midi1 = (protocol == "midi1") or (
            msg_type in _M1CV_OPCODES and msg_type not in _CV_OPCODES
        )
        if use_midi1 and msg_type in _M1CV_OPCODES:
            try:
                w = _encode_midi1_channel_voice(message)
            except (KeyError, ValueError) as e:
                return _err(f"invalid 'message' for send: {type(e).__name__}: {e}")
            words = [w]
        elif msg_type in _CV_OPCODES:
            try:
                w0, w1 = _encode_channel_voice(message)
            except (KeyError, ValueError) as e:
                return _err(f"invalid 'message' for send: {type(e).__name__}: {e}")
            words = [w0, w1]
        elif msg_type in _SYS_STATUS:
            try:
                w = _encode_system_message(message)
            except (KeyError, ValueError) as e:
                return _err(f"invalid 'message' for send: {type(e).__name__}: {e}")
            words = [w]
        elif msg_type in _UTIL_STATUS:
            try:
                w = _encode_utility_message(message)
            except (KeyError, ValueError) as e:
                return _err(f"invalid 'message' for send: {type(e).__name__}: {e}")
            words = [w]
        elif msg_type == "sysex7":
            try:
                w0, w1 = _encode_sysex7_message(message)
            except (KeyError, ValueError) as e:
                return _err(f"invalid 'message' for send: {type(e).__name__}: {e}")
            words = [w0, w1]
        elif msg_type == "sysex8":
            try:
                w0, w1, w2, w3 = _encode_sysex8_message(message)
            except (KeyError, ValueError) as e:
                return _err(f"invalid 'message' for send: {type(e).__name__}: {e}")
            words = [w0, w1, w2, w3]
        else:
            return _err(
                f"unknown or unsupported 'message' type {msg_type!r} — "
                f"supported typed types: Channel "
                f"Voice {sorted(_CV_OPCODES)}, MIDI-1-in-UMP "
                f"{sorted(_M1CV_OPCODES)} (pass 'protocol':'midi1' to pick "
                f"the MIDI-1-in-UMP encoding for the 4 shared-name types), "
                f"System Common/Real-Time {sorted(_SYS_STATUS)}, "
                f"Utility {sorted(_UTIL_STATUS)}, SysEx7 ('sysex7', one "
                f"packet per call — see tool description for 'status'/"
                f"'data' fields), SysEx8/Mixed Data Set ('sysex8', one "
                f"packet per call, up to 13 8-bit data bytes plus a "
                f"'stream_id' field — see tool description)"
            )
    else:
        words = words or []

    if len(words) > 4:
        return _err("'words' must have at most 4 elements (a full 128-bit UMP packet)")
    padded = list(words) + [0] * (4 - len(words))

    try:
        seq = _ensure_seq()
        ev = ffi.new("snd_seq_ump_event_t *")
        ev.flags = SND_SEQ_EVENT_UMP_FLAG
        ev.queue = SND_SEQ_QUEUE_DIRECT
        ev.source.port = entry["port"]
        ev.dest.client, ev.dest.port = target
        for i, w in enumerate(padded):
            ev.data.ump[i] = w & 0xFFFFFFFF
        _check(_lib.snd_seq_ump_event_output(seq, ev), "ump_event_output")
        _check(_lib.snd_seq_drain_output(seq), "drain_output")
    except Exception as e:
        return _err(f"send failed: {type(e).__name__}: {e}")

    return json.dumps({"status": "ok", "sent_words": [hex(w) for w in padded]})


def _drain_all_pending():
    """Pull every currently-queued event off the shared client into _PENDING,
    keyed by destination port, so each handle's poll() only sees its own."""
    seq = _ensure_seq()
    ev_in_p = ffi.new("snd_seq_ump_event_t **")
    while _lib.snd_seq_event_input_pending(seq, 1) > 0:
        rc = _lib.snd_seq_ump_event_input(seq, ev_in_p)
        if rc < 0:
            break
        got = ev_in_p[0]
        words = [got.data.ump[i] for i in range(4)]
        _PENDING.setdefault(got.dest.port, []).append(words)


def _poll(tool_input: dict) -> str:
    handle = tool_input.get("handle")
    entry = _OPEN_PORTS.get(handle) if handle else None
    if entry is None:
        return _err(f"no open port for handle {handle!r}")
    if entry["direction"] != "input":
        return _err(f"handle {handle!r} is an output port, cannot poll it")

    try:
        _drain_all_pending()
    except Exception as e:
        return _err(f"poll failed: {type(e).__name__}: {e}")

    port = entry["port"]
    pending = _PENDING.get(port, [])
    _PENDING[port] = []
    messages = [[hex(w) for w in words] for words in pending]
    # Best-effort typed decode alongside the raw words, parallel array
    # (same length/order as 'messages'). `None` means "not decodable by
    # any of the 6 supported groups" — only a malformed/reserved
    # message_type nibble or truly corrupt packet should ever hit that
    # case now, every originally-scoped UMP message group is covered.
    # Raw 'messages' is unchanged/backward-compatible; 'decoded' is
    # purely additive.
    #
    # Tries native MIDI 2.0 Channel Voice (message_type=4) first, then
    # MIDI-1-in-UMP Channel Voice (message_type=2), then System Common/
    # Real-Time-in-UMP (message_type=1), then Utility (message_type=0),
    # then SysEx7 (message_type=3), then SysEx8/Mixed Data Set
    # (message_type=5) — extended this sub-step for the 6th and final
    # group. NOT a heuristic/guess: the message_type nibble (word0
    # bits[31:28]) unambiguously identifies which group produced a given
    # packet, and each decoder immediately raises ValueError if that
    # nibble doesn't match what it expects — so a packet from one group
    # can never be mis-decoded as another. The 6 calls are just "try,
    # reject-fast, try the next," not competing interpretations of the
    # same bits.
    decoded: list[dict[str, Any] | None] = []
    for words in pending:
        try:
            decoded.append(_decode_channel_voice(words[0], words[1]))
            continue
        except ValueError:
            pass
        try:
            decoded.append(_decode_midi1_channel_voice(words[0]))
            continue
        except ValueError:
            pass
        try:
            decoded.append(_decode_system_message(words[0]))
            continue
        except ValueError:
            pass
        try:
            decoded.append(_decode_utility_message(words[0]))
            continue
        except ValueError:
            pass
        try:
            decoded.append(_decode_sysex7_message(words[0], words[1]))
            continue
        except ValueError:
            pass
        try:
            decoded.append(_decode_sysex8_message(words[0], words[1], words[2], words[3]))
        except ValueError:
            decoded.append(None)
    return json.dumps({"status": "ok", "messages": messages, "decoded": decoded})
