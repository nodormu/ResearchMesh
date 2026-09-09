"""MIDI 1.0 tool — device discovery, port I/O, and channel/system messages.

Built via `mido[ports-rtmidi]` (python-rtmidi backend). This is the shared
discovery surface both the MIDI 1.0 and (future) MIDI 2.0 tools use — see
memories/sysex-midi-tool-addition.md for the full design/phase history.

PHASE 1 + PHASE 2 + PHASE 3 SCOPE — more actions land in later phases of the
same doc (.mid file I/O in Phase 4, pygame add-on in Phase 5, multi-port/
callback hardening in Phase 6). Current actions:
  - list_devices : enumerate input/output port names
  - open         : open a named port (as "input" or "output"), returns a
                   handle string for later actions
  - close        : close a previously opened handle
  - send         : send a channel or system message on an open output
                   handle. Channel messages: note_on, note_off,
                   control_change, program_change, pitchwheel, aftertouch
                   (channel pressure), polytouch (poly key pressure).
                   System Common: quarter_frame, songpos, song_select,
                   tune_request. System Real-Time: clock, start, stop,
                   continue, active_sensing, reset. System Exclusive: sysex
                   (arbitrary-payload — see Phase 3 note below).
  - poll         : non-blocking check for buffered messages on an open input
                   handle — a bare-bones placeholder for real-time receive,
                   NOT the full non-blocking/callback design promised for
                   Phase 6. Decodes ANY incoming mido message generically
                   (str(msg)), so it already reports message types beyond
                   what `send` explicitly constructs — confirmed live during
                   Phase-2-adjacent hardware testing, where `poll` correctly
                   surfaced `aftertouch` messages from a real keyboard before
                   `send` even had aftertouch support. No change needed here
                   for Phase 2 — this note just records why.

                   EXCEPTION: `active_sensing` will NEVER show up in `poll`
                   results, even though `send` can transmit it fine. mido's
                   rtmidi backend hardcodes
                   `self._rt.ignore_types(False, False, True)` on every input
                   port it opens — the third arg tells RtMidi itself to
                   filter Active Sensing bytes before mido's parser ever
                   sees them. Confirmed live (Phase 2 offline smoke test:
                   sent 14 message types, only 13 came back via poll, the
                   missing one was active_sensing). Not fixable within mido's
                   public API — would need raw rtmidi.MidiIn to change.
  - read_midi_file : read a .mid/.midi or .syx file from disk (Phase 4a —
                   READ side only; write support lands in a later sub-chunk).
                   See PHASE 4a NOTE below for full field details.

PHASE 3 NOTE (SysEx): 'sysex' takes a 'data' array of integers, each 0-127
(7-bit data bytes only — MIDI's own spec forbids status-byte values 0x80+
inside a SysEx payload). Do NOT include the leading 0xF0 or trailing 0xF7 —
mido's mido.Message('sysex', data=...) adds both automatically on send and
strips both automatically when decoding a received one. Confirmed live via
mido.messages.specs.SPEC_BY_TYPE['sysex'] before writing this (status_byte
240 = 0xF0, single value_name 'data', variable length) — same "check mido's
spec before guessing kwarg names" discipline as Phase 2. mido itself raises
a plain ValueError for any out-of-range byte (caught by the same broad
except as every other message type here, no special-casing needed). Like
Phase 2, 'poll' needed ZERO changes for this — str(msg) already decodes an
incoming sysex message generically, confirmed live
(str(mido.Message('sysex', data=(1,2,3))) -> "sysex data=(1,2,3) time=0").

PHASE 4a NOTE (.mid/.syx file READ support — write support is a later,
separate sub-chunk): 'read_midi_file' takes a 'path' (absolute path to a
.mid/.midi or .syx file) and an optional 'max_messages' (default 100, caps
how many message strings are returned per track/file so a huge file can't
flood the response — set 0 to get only metadata/counts with zero message
bodies). For .mid/.midi: uses mido.MidiFile(path), returns file type (0/1/2),
ticks_per_beat, length_seconds, and a per-track summary (index, track name,
message count, first tempo_bpm/time_signature/key_signature/instrument_name
meta values found, decoded messages up to the cap, truncated flag). For
.syx: uses mido.read_syx_file(path), returns message_count + decoded sysex
message strings up to the cap. Field names for all meta message types
(track_name.name, set_tempo.tempo, time_signature.numerator/denominator/
clocks_per_click/notated_32nd_notes_per_beat, key_signature.key,
instrument_name.name, smpte_offset.hours/minutes/seconds/frames/
sub_frames/frame_rate, etc.) verified live against
mido.midifiles.meta._META_SPEC_BY_TYPE before writing any code — same
discipline as Phases 2/3. mido.tempo2bpm() used to convert raw tempo
(microseconds per quarter note) to a human BPM figure for the summary.

State (open ports) lives in this module's process memory (_OPEN_PORTS dict),
same pattern as core/kernel.py's persistent IPython kernel or core/browser.py's
one-page-per-session model — it does NOT persist across a ResearchMesh
restart, and every handle is only valid within one running client process.
"""

import asyncio
import itertools
import json
import os

try:
    import mido
    _MIDO_IMPORT_ERROR: Exception | None = None
except ImportError as e:
    mido = None  # type: ignore[assignment]
    _MIDO_IMPORT_ERROR = e

TOOLS = [
    {
        "name": "midi1",
        "description": (
            "MIDI 1.0 device discovery and I/O (Phases 1-2 of a larger "
            "planned MIDI tool — see memories/sysex-midi-tool-addition.md). "
            "Actions: 'list_devices' enumerates available MIDI input/output "
            "port names. 'open' opens a named port as either 'input' or "
            "'output' and returns a handle string to use in later calls. "
            "'send' sends a channel message (note_on, note_off, "
            "control_change, program_change, pitchwheel, aftertouch, "
            "polytouch), a system message (quarter_frame, songpos, "
            "song_select, tune_request, clock, start, stop, continue, "
            "active_sensing, reset), or an arbitrary-payload System "
            "Exclusive message ('sysex', with a 'data' array of 0-127 "
            "integers, NOT including the leading 0xF0/trailing 0xF7 which "
            "are added automatically) on an open output handle. 'poll' does a "
            "non-blocking check for buffered messages on an open input "
            "handle — decodes any incoming MIDI message generically, not "
            "just the types 'send' explicitly supports. 'close' closes a "
            "previously opened handle. Handles only live for the current "
            "ResearchMesh process — reopen after a restart. "
            "'read_midi_file' reads a .mid/.midi or .syx file from disk "
            "('path' required) and returns a structured summary: for "
            ".mid/.midi, file type/ticks_per_beat/length_seconds plus a "
            "per-track breakdown (name, message count, first tempo/time-"
            "signature/key-signature/instrument-name meta values found, "
            "and decoded messages up to 'max_messages' per track, default "
            "100 — set 0 for metadata/counts only); for .syx, message_count "
            "plus decoded sysex messages up to the same cap. "
            "'write_midi_file' creates a NEW .mid/.midi or .syx file from "
            "scratch on disk ('path' required, format inferred from the "
            "extension) — this is whole-file CREATE only, not an in-place "
            "edit of an existing file. Refuses to overwrite an existing "
            "file unless 'overwrite' is explicitly true. For .mid: "
            "optional 'midi_file_type' (0/1/2, default 1) and "
            "'ticks_per_beat' (default 480), plus required 'tracks' — an "
            "array of {'messages': [...]} objects. Each message reuses the "
            "same type+fields shape as 'send' (note_on, control_change, "
            "sysex, etc.) plus a 'time' field (delta ticks since the "
            "previous message in that track, default 0), PLUS 17 file-only "
            "meta message types not valid for live 'send': track_name, "
            "text, copyright, lyrics, marker, cue_marker, instrument_name, "
            "device_name, set_tempo (takes either raw 'tempo' in "
            "microseconds/quarter-note OR a friendlier 'bpm'), "
            "time_signature, key_signature, smpte_offset, midi_port, "
            "channel_prefix, sequence_number, sequencer_specific, "
            "end_of_track (auto-added if omitted). For .syx: required "
            "'messages' — a flat array of {'type':'sysex','data':[...]} "
            "objects. On success, returns the same structured summary "
            "'read_midi_file' would produce for the file just written, as "
            "a built-in round-trip sanity check."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_devices", "open", "close", "send", "poll",
                        "read_midi_file", "write_midi_file",
                    ],
                    "description": "Which MIDI operation to perform.",
                },
                "port_name": {
                    "type": "string",
                    "description": (
                        "Exact MIDI port name (as returned by 'list_devices'). "
                        "Required for 'open'."
                    ),
                },
                "direction": {
                    "type": "string",
                    "enum": ["input", "output"],
                    "description": "Which side to open the port as. Required for 'open'.",
                },
                "handle": {
                    "type": "string",
                    "description": (
                        "Handle returned by a previous 'open' call. Required "
                        "for 'close', 'send', and 'poll'."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute path to a .mid/.midi or .syx file on disk. "
                        "Required for 'read_midi_file'."
                    ),
                },
                "max_messages": {
                    "type": "integer",
                    "description": (
                        "For 'read_midi_file': max number of decoded message "
                        "strings to return per track (.mid) or total (.syx). "
                        "Default 100. Set 0 to return only metadata/counts "
                        "with no message bodies, useful for a quick look at "
                        "a very large file without flooding the response."
                    ),
                },
                "overwrite": {
                    "type": "boolean",
                    "description": (
                        "For 'write_midi_file': set true to allow "
                        "overwriting a file that already exists at 'path'. "
                        "Default false — the call is refused if the file "
                        "already exists, to avoid accidentally destroying "
                        "it. This only guards against a whole-file "
                        "overwrite; there is no in-place partial-edit "
                        "(e.g. punch-in style re-recording a specific "
                        "region of an existing file) support."
                    ),
                },
                "midi_file_type": {
                    "type": "integer",
                    "enum": [0, 1, 2],
                    "description": (
                        "For 'write_midi_file' on a .mid/.midi path: the "
                        "MIDI file format (0, 1, or 2). Default 1 "
                        "(multi-track). Type 0 requires exactly one track "
                        "in 'tracks' — mido itself raises a clear error if "
                        "violated."
                    ),
                },
                "ticks_per_beat": {
                    "type": "integer",
                    "description": (
                        "For 'write_midi_file' on a .mid/.midi path: the "
                        "file's time division. Default 480."
                    ),
                },
                "tracks": {
                    "type": "array",
                    "description": (
                        "Required for 'write_midi_file' on a .mid/.midi "
                        "path. Array of track objects, each "
                        "{'messages': [...]}. See the tool description for "
                        "the full message-type list (channel/system/sysex "
                        "types matching 'send', plus 17 file-only meta "
                        "types)."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "messages": {
                                "type": "array",
                                "items": {"type": "object"},
                            }
                        },
                    },
                },
                "messages": {
                    "type": "array",
                    "description": (
                        "Required for 'write_midi_file' on a .syx path. "
                        "Flat array of {'type': 'sysex', 'data': [...]} "
                        "objects."
                    ),
                    "items": {"type": "object"},
                },
                "message": {
                    "type": "object",
                    "description": (
                        "Required for 'send'. Object with 'type' plus the "
                        "fields that type needs. Channel messages (need "
                        "'channel', 0-15, default 0): note_on/note_off need "
                        "'note'+'velocity'; control_change needs "
                        "'control'+'value'; program_change needs 'program'; "
                        "pitchwheel needs 'pitch' (-8192..8191, default 0); "
                        "aftertouch (channel pressure) needs 'value'; "
                        "polytouch (poly key pressure) needs 'note'+'value'. "
                        "System Common: quarter_frame needs "
                        "'frame_type'+'frame_value'; songpos needs 'pos'; "
                        "song_select needs 'song'; tune_request needs no "
                        "extra fields. System Real-Time (clock, start, stop, "
                        "continue, active_sensing, reset) need no extra "
                        "fields at all — just 'type'. sysex needs a 'data' "
                        "array of integers, each 0-127 (7-bit data bytes "
                        "only) — do NOT include the leading 0xF0 or trailing "
                        "0xF7, both are added automatically. NOTE: 'active_sensing' "
                        "can be SENT successfully but will NEVER be reported "
                        "by 'poll' even if actually received — mido's rtmidi "
                        "backend hardcodes RtMidi's ignore_types(sysex=False, "
                        "timing=False, active_sense=True), filtering "
                        "incoming Active Sensing at the library level before "
                        "mido's own parser ever sees it. Confirmed live, not "
                        "fixable without bypassing mido for raw rtmidi."
                    ),
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "note_on", "note_off", "control_change",
                                "program_change", "pitchwheel", "aftertouch",
                                "polytouch",
                                "quarter_frame", "songpos", "song_select",
                                "tune_request",
                                "clock", "start", "stop", "continue",
                                "active_sensing", "reset", "sysex",
                            ],
                        },
                        "channel": {"type": "integer"},
                        "note": {"type": "integer"},
                        "velocity": {"type": "integer"},
                        "control": {"type": "integer"},
                        "value": {"type": "integer"},
                        "program": {"type": "integer"},
                        "pitch": {"type": "integer"},
                        "frame_type": {"type": "integer"},
                        "frame_value": {"type": "integer"},
                        "pos": {"type": "integer"},
                        "song": {"type": "integer"},
                        "data": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": (
                                "SysEx payload bytes, each 0-127. Required "
                                "for 'sysex'. Do not include the leading "
                                "0xF0 or trailing 0xF7 — added automatically."
                            ),
                        },
                    },
                },
            },
            "required": ["action"],
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}

# handle -> (direction, mido port object). Process-lifetime only, same
# statefulness caveat as core/kernel.py's persistent IPython kernel.
_OPEN_PORTS: dict = {}
_HANDLE_COUNTER = itertools.count(1)


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "midi1":
        return json.dumps({"error": f"unknown midi1 tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


def _err(message: str) -> str:
    return json.dumps({"error": message})


def _run(tool_input: dict) -> str:
    if mido is None:
        return _err(
            "mido is not installed — `pip install mido[ports-rtmidi]` "
            f"to use the midi1 tool ({_MIDO_IMPORT_ERROR})"
        )
    action = tool_input.get("action")
    if action == "list_devices":
        return _list_devices()
    if action == "open":
        return _open(tool_input)
    if action == "close":
        return _close(tool_input)
    if action == "send":
        return _send(tool_input)
    if action == "poll":
        return _poll(tool_input)
    if action == "read_midi_file":
        return _read_midi_file(tool_input)
    if action == "write_midi_file":
        return _write_midi_file(tool_input)
    return _err(
        f"unknown action {action!r} — expected one of "
        "list_devices, open, close, send, poll, read_midi_file, "
        "write_midi_file"
    )


def _list_devices() -> str:
    try:
        inputs = mido.get_input_names()
        outputs = mido.get_output_names()
    except Exception as e:
        return _err(f"device enumeration failed: {type(e).__name__}: {e}")
    return json.dumps({"status": "ok", "inputs": inputs, "outputs": outputs})


def _open(tool_input: dict) -> str:
    port_name = tool_input.get("port_name")
    direction = tool_input.get("direction")
    if not port_name:
        return _err("'port_name' is required for 'open'")
    if direction not in ("input", "output"):
        return _err("'direction' must be 'input' or 'output' for 'open'")

    try:
        if direction == "input":
            port = mido.open_input(port_name)
        else:
            port = mido.open_output(port_name)
    except Exception as e:
        return _err(
            f"failed to open {direction} port {port_name!r}: "
            f"{type(e).__name__}: {e}"
        )

    handle = f"midi1-{next(_HANDLE_COUNTER)}"
    _OPEN_PORTS[handle] = (direction, port)
    return json.dumps(
        {"status": "ok", "handle": handle, "direction": direction, "port_name": port_name}
    )


def _close(tool_input: dict) -> str:
    handle = tool_input.get("handle")
    entry = _OPEN_PORTS.pop(handle, None) if handle else None
    if entry is None:
        return _err(f"no open port for handle {handle!r}")
    _, port = entry
    try:
        port.close()
    except Exception as e:
        return _err(f"error closing handle {handle!r}: {type(e).__name__}: {e}")
    return json.dumps({"status": "ok", "handle": handle, "closed": True})


def _build_message(message: dict) -> "mido.Message":
    """Build a channel/system/sysex mido.Message from a tool-supplied dict.

    PHASE 4b REFACTOR: extracted verbatim out of `_send` (was previously
    inline there) so this same construction logic can be shared between
    live `send` and the new `write_midi_file` action's per-track messages.
    Behavior/error semantics unchanged from the original `_send` ladder —
    same required-field KeyErrors, same mido-raised ValueErrors for bad
    values, callers still catch both. `time` (delta ticks, meaningful for
    file-writing, harmless/ignored-by-hardware for live send) is read from
    the dict and passed through uniformly — confirmed live that mido.Message
    accepts a `time` kwarg for every message category (channel, system
    common/real-time, and sysex all tested), not just some.
    """
    msg_type = message.get("type")
    channel = message.get("channel", 0)
    time = message.get("time", 0)
    if msg_type == "note_on":
        return mido.Message(
            "note_on", channel=channel, note=message["note"],
            velocity=message.get("velocity", 64), time=time,
        )
    if msg_type == "note_off":
        return mido.Message(
            "note_off", channel=channel, note=message["note"],
            velocity=message.get("velocity", 0), time=time,
        )
    if msg_type == "control_change":
        return mido.Message(
            "control_change", channel=channel, control=message["control"],
            value=message.get("value", 0), time=time,
        )
    if msg_type == "program_change":
        return mido.Message(
            "program_change", channel=channel, program=message["program"],
            time=time,
        )
    if msg_type == "pitchwheel":
        return mido.Message(
            "pitchwheel", channel=channel, pitch=message.get("pitch", 0),
            time=time,
        )
    if msg_type == "aftertouch":
        # Channel Pressure (After-touch) — applies to all currently
        # sounding notes on the channel, single value, no note number.
        return mido.Message(
            "aftertouch", channel=channel, value=message.get("value", 0),
            time=time,
        )
    if msg_type == "polytouch":
        # Polyphonic Key Pressure — per-note aftertouch.
        return mido.Message(
            "polytouch", channel=channel, note=message["note"],
            value=message.get("value", 0), time=time,
        )
    if msg_type == "quarter_frame":
        # MIDI Time Code Quarter Frame (System Common).
        return mido.Message(
            "quarter_frame", frame_type=message["frame_type"],
            frame_value=message["frame_value"], time=time,
        )
    if msg_type == "songpos":
        # Song Position Pointer (System Common), 0-16383.
        return mido.Message("songpos", pos=message.get("pos", 0), time=time)
    if msg_type == "song_select":
        # Song Select (System Common).
        return mido.Message("song_select", song=message["song"], time=time)
    if msg_type == "tune_request":
        # System Common, no data bytes.
        return mido.Message("tune_request", time=time)
    if msg_type in ("clock", "start", "stop", "continue", "active_sensing", "reset"):
        # System Real-Time messages — single status byte, no data at all,
        # no channel. mido accepts the type name directly.
        return mido.Message(msg_type, time=time)
    if msg_type == "sysex":
        # System Exclusive (Phase 3) — arbitrary payload. mido adds the
        # leading 0xF0 / trailing 0xF7 automatically; data bytes must each
        # be 0-127, mido itself raises ValueError otherwise (caught by the
        # broad except in every caller, same as every other message type).
        raw_data = message.get("data")
        if raw_data is None:
            raise KeyError("'data'")
        return mido.Message("sysex", data=tuple(raw_data), time=time)
    raise ValueError(
        f"unknown message type {msg_type!r} — expected one of "
        "note_on, note_off, control_change, program_change, "
        "pitchwheel, aftertouch, polytouch, quarter_frame, songpos, "
        "song_select, tune_request, clock, start, stop, continue, "
        "active_sensing, reset, sysex"
    )


# PHASE 4b: the 17 file-only "meta" message types, verified live against
# mido.midifiles.meta._META_SPEC_BY_TYPE before writing any code (same
# "check mido's real spec, don't guess kwargs" discipline as every prior
# phase). None of these are valid on a live `send` — they only make sense
# inside a .mid file's track data.
_META_TYPES = frozenset({
    "track_name", "text", "copyright", "lyrics", "marker", "cue_marker",
    "instrument_name", "device_name", "set_tempo", "time_signature",
    "key_signature", "smpte_offset", "midi_port", "channel_prefix",
    "sequence_number", "sequencer_specific", "end_of_track",
})


def _build_meta_message(message: dict) -> "mido.MetaMessage":
    """Build a mido.MetaMessage from a tool-supplied dict, for PHASE 4b
    `write_midi_file` track content only.

    Deliberately generic/thin: confirmed live that mido.MetaMessage(type,
    **kwargs) already (a) fills in its OWN sensible defaults for any
    omitted attribute (e.g. time_signature defaults to 4/4, key_signature
    defaults to 'C', smpte_offset defaults to all-zero/24fps — checked live
    for every type in _META_TYPES before writing this), and (b) raises a
    clean ValueError itself for any unrecognized kwarg name (e.g. a typo'd
    field). So this function does NOT hand-duplicate mido's own per-type
    attribute lists/defaults — it just passes whatever fields the caller
    gave straight through and lets mido validate them, same "let mido do
    the validating" lesson learned from the type-0-multitrack research
    during Phase 4b's design pass. Only ONE special case: `set_tempo` gets
    a `bpm` convenience alt-field (user-requested, Phase 4b design
    decision) that converts to mido's real `tempo` (microseconds/quarter)
    via `mido.bpm2tempo()` — mido itself has no `bpm` kwarg, this is purely
    a tool-level convenience layered on top.
    """
    msg_type = message.get("type")
    if msg_type not in _META_TYPES:
        raise ValueError(f"unknown meta message type {msg_type!r}")
    time = message.get("time", 0)
    kwargs = {
        k: v for k, v in message.items()
        if k not in ("type", "time", "bpm")
    }
    if msg_type == "set_tempo" and "bpm" in message:
        kwargs["tempo"] = mido.bpm2tempo(message["bpm"])
    if "data" in kwargs and isinstance(kwargs["data"], list):
        kwargs["data"] = tuple(kwargs["data"])
    return mido.MetaMessage(msg_type, time=time, **kwargs)


def _send(tool_input: dict) -> str:
    handle = tool_input.get("handle")
    message = tool_input.get("message") or {}
    entry = _OPEN_PORTS.get(handle) if handle else None
    if entry is None:
        return _err(f"no open port for handle {handle!r}")
    direction, port = entry
    if direction != "output":
        return _err(f"handle {handle!r} is an input port, cannot send on it")

    try:
        msg = _build_message(message)
        port.send(msg)
    except KeyError as e:
        return _err(f"message missing required field: {e}")
    except Exception as e:
        return _err(f"send failed: {type(e).__name__}: {e}")

    return json.dumps({"status": "ok", "sent": str(msg)})


def _poll(tool_input: dict) -> str:
    handle = tool_input.get("handle")
    entry = _OPEN_PORTS.get(handle) if handle else None
    if entry is None:
        return _err(f"no open port for handle {handle!r}")
    direction, port = entry
    if direction != "input":
        return _err(f"handle {handle!r} is an output port, cannot poll it")

    try:
        messages = [str(msg) for msg in port.iter_pending()]
    except Exception as e:
        return _err(f"poll failed: {type(e).__name__}: {e}")

    return json.dumps({"status": "ok", "messages": messages})


def _read_midi_file(tool_input: dict) -> str:
    path = tool_input.get("path")
    if not path:
        return _err("'path' is required for 'read_midi_file'")

    max_messages = tool_input.get("max_messages", 100)
    if not isinstance(max_messages, int) or max_messages < 0:
        return _err("'max_messages' must be a non-negative integer")

    is_syx = path.lower().endswith(".syx")

    try:
        if is_syx:
            msgs = mido.read_syx_file(path)
        else:
            mf = mido.MidiFile(path)
    except FileNotFoundError:
        return _err(f"file not found: {path!r}")
    except Exception as e:
        return _err(f"failed to read {path!r}: {type(e).__name__}: {e}")

    if is_syx:
        total = len(msgs)
        shown = msgs[:max_messages] if max_messages else []
        return json.dumps(
            {
                "status": "ok",
                "file_type": "syx",
                "message_count": total,
                "messages": [str(m) for m in shown],
                "truncated": max_messages > 0 and total > max_messages,
            }
        )

    tracks_out = []
    for i, track in enumerate(mf.tracks):
        name = None
        tempo_bpm = None
        time_sig = None
        key_sig = None
        instrument_name = None
        for msg in track:
            if not msg.is_meta:
                continue
            if msg.type == "track_name" and name is None:
                name = msg.name
            elif msg.type == "set_tempo" and tempo_bpm is None:
                tempo_bpm = mido.tempo2bpm(msg.tempo)
            elif msg.type == "time_signature" and time_sig is None:
                time_sig = f"{msg.numerator}/{msg.denominator}"
            elif msg.type == "key_signature" and key_sig is None:
                key_sig = msg.key
            elif msg.type == "instrument_name" and instrument_name is None:
                instrument_name = msg.name

        total = len(track)
        shown = list(track)[:max_messages] if max_messages else []
        tracks_out.append(
            {
                "index": i,
                "name": name,
                "message_count": total,
                "tempo_bpm": tempo_bpm,
                "time_signature": time_sig,
                "key_signature": key_sig,
                "instrument_name": instrument_name,
                "messages": [str(m) for m in shown],
                "truncated": max_messages > 0 and total > max_messages,
            }
        )

    return json.dumps(
        {
            "status": "ok",
            "file_type": "mid",
            "type": mf.type,
            "ticks_per_beat": mf.ticks_per_beat,
            "length_seconds": mf.length,
            "track_count": len(mf.tracks),
            "tracks": tracks_out,
        }
    )


def _write_midi_file(tool_input: dict) -> str:
    """PHASE 4b: write a .mid/.midi or .syx file from tool-supplied track/
    message data. See memories/sysex-midi-tool-addition.md's "Phase 4b —
    DESIGN LOCKED IN" entry for the full design discussion this implements.
    """
    path = tool_input.get("path")
    if not path:
        return _err("'path' is required for 'write_midi_file'")

    overwrite = tool_input.get("overwrite", False)
    if os.path.exists(path) and not overwrite:
        return _err(
            f"file already exists: {path!r} — pass overwrite: true to "
            "replace it (refusing by default to avoid accidentally "
            "destroying an existing file)"
        )

    is_syx = path.lower().endswith(".syx")

    if is_syx:
        messages_in = tool_input.get("messages")
        if not messages_in:
            return _err("'messages' is required for writing a .syx file")
        built = []
        for i, m in enumerate(messages_in):
            if not isinstance(m, dict) or m.get("type") != "sysex":
                return _err(
                    f"'.syx' files only support sysex messages, got "
                    f"{m.get('type') if isinstance(m, dict) else m!r} at "
                    f"index {i}"
                )
            try:
                built.append(_build_message(m))
            except KeyError as e:
                return _err(f"message {i} missing required field: {e}")
            except Exception as e:
                return _err(f"message {i} invalid: {type(e).__name__}: {e}")
        try:
            mido.write_syx_file(path, built)
        except Exception as e:
            return _err(f"failed to write {path!r}: {type(e).__name__}: {e}")
    else:
        midi_file_type = tool_input.get("midi_file_type", 1)
        ticks_per_beat = tool_input.get("ticks_per_beat", 480)
        tracks_in = tool_input.get("tracks")
        if not tracks_in:
            return _err("'tracks' is required for writing a .mid file")
        try:
            mf = mido.MidiFile(type=midi_file_type, ticks_per_beat=ticks_per_beat)
        except Exception as e:
            return _err(f"invalid MIDI file parameters: {type(e).__name__}: {e}")
        for ti, track_in in enumerate(tracks_in):
            track = mido.MidiTrack()
            for mi, m in enumerate(track_in.get("messages", [])):
                msg_type = m.get("type") if isinstance(m, dict) else None
                try:
                    if msg_type in _META_TYPES:
                        msg = _build_meta_message(m)
                    else:
                        msg = _build_message(m)
                except KeyError as e:
                    return _err(
                        f"track {ti} message {mi} missing required field: {e}"
                    )
                except Exception as e:
                    return _err(
                        f"track {ti} message {mi} invalid: "
                        f"{type(e).__name__}: {e}"
                    )
                track.append(msg)
            mf.tracks.append(track)
        try:
            mf.save(path)
        except Exception as e:
            return _err(f"failed to write {path!r}: {type(e).__name__}: {e}")

    # Sanity-check the write by immediately re-reading what actually landed
    # on disk via the already-proven Phase 4a reader — this both confirms
    # the write succeeded correctly AND avoids duplicating summary-building
    # logic here (Phase 4b design decision).
    return _read_midi_file({"path": path, "max_messages": 0})
