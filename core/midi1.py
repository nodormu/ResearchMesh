"""MIDI 1.0 tool — device discovery, port I/O, and channel/system messages.

Built via `mido[ports-rtmidi]` (python-rtmidi backend). This started as the
shared discovery surface for both a MIDI 1.0 and a MIDI 2.0 tool; the MIDI
2.0 tool has since been removed from this project and moved to its own
standalone project for further work.

Actions:
  - list_devices : enumerate input/output port names
  - open         : open a named port (as "input" or "output"), returns a
                   handle string for later actions
  - close        : close a previously opened handle
  - send         : send a channel or system message, or a typed sysex
                   convenience message, on an open output handle. Channel
                   messages: note_on, note_off, control_change,
                   program_change, pitchwheel, aftertouch (channel
                   pressure), polytouch (poly key pressure), and
                   channel_mode (a typed wrapper around Control Change's
                   own controller numbers 120-127 — see the inline
                   comment in `_build_message` for the full command list).
                   System Common:
                   quarter_frame, songpos, song_select, tune_request.
                   System Real-Time: clock, start, stop, continue,
                   active_sensing, reset. System Exclusive: sysex
                   (arbitrary-payload — see the SysEx note below), plus
                   six typed convenience messages built on top of that
                   same sysex mechanism: mtc_full (MIDI Time Code Full
                   Message), mmc (MIDI Machine Control transport commands),
                   msc (MIDI Show Control General Category commands),
                   gm_system (General MIDI System On/Off), device_inquiry
                   (Identity Request/Reply), device_control (Master
                   Volume/Balance), midi_tuning (Bulk Tuning Dump Request/
                   Reply, Single Note Tuning Change), notation (Bar
                   Marker, Time Signature Immediate/Delayed), and
                   mtc_cueing (MTC Real-Time Cueing Set-Up messages), and
                   mtc_cueing_nrt (the fuller Non-Real-Time Cueing
                   Set-Up messages, incl. Delete variants and 5 Special
                   sub-types) — see each one's own inline comment in
                   `_build_message` further down in this file for full
                   field details. Two more typed convenience types, rpn
                   and nrpn (Registered/Non-Registered Parameter Numbers),
                   are NOT sysex-based and NOT single messages — selecting
                   and setting one is a short SEQUENCE of Control Change
                   messages on the wire, built by the separate
                   `_build_rpn_or_nrpn_sequence` further down. Because of
                   this, a successful 'send' response's 'sent' field is a
                   single string for every message type EXCEPT rpn/nrpn,
                   where it's a list of strings (one per message actually
                   transmitted) — check whether 'sent' is a str or a list
                   if parsing this programmatically.
  - poll         : non-blocking check for buffered messages on an open input
                   handle. This is a manual, caller-driven check (call it
                   repeatedly to see new messages) — there is currently no
                   event-driven/callback-based receive model, and none is
                   planned unless specifically requested. Decodes ANY
                   incoming mido message generically (str(msg)), so it
                   already reports message types beyond what `send`
                   explicitly constructs — confirmed live during hardware
                   testing, where `poll` correctly surfaced `aftertouch`
                   messages from a real keyboard before `send` even had
                   aftertouch support.

                   EXCEPTION: `active_sensing` will NEVER show up in `poll`
                   results, even though `send` can transmit it fine. mido's
                   rtmidi backend hardcodes
                   `self._rt.ignore_types(False, False, True)` on every input
                   port it opens — the third arg tells RtMidi itself to
                   filter Active Sensing bytes before mido's parser ever
                   sees them. Confirmed live (an offline smoke test sent 14
                   message types, only 13 came back via poll, the missing
                   one was active_sensing). Not fixable within mido's
                   public API — would need raw rtmidi.MidiIn to change.
  - read_midi_file  : read a .mid/.midi or .syx file from disk. See the
                   file-I/O note below for full field details.
  - write_midi_file : create a NEW .mid/.midi or .syx file from disk-
                   supplied track/message data — the mirror-image write
                   path to read_midi_file. See the file-I/O note below.

SysEx note: 'sysex' takes a 'data' array of integers, each 0-127
(7-bit data bytes only — MIDI's own spec forbids status-byte values 0x80+
inside a SysEx payload). Do NOT include the leading 0xF0 or trailing 0xF7 —
mido's mido.Message('sysex', data=...) adds both automatically on send and
strips both automatically when decoding a received one. Confirmed live via
mido.messages.specs.SPEC_BY_TYPE['sysex'] before writing this (status_byte
240 = 0xF0, single value_name 'data', variable length) — same "check mido's
spec before guessing kwarg names" discipline used throughout this file.
mido itself raises a plain ValueError for any out-of-range byte (caught by
the same broad except as every other message type here, no special-casing
needed). 'poll' needed ZERO changes to support this — str(msg) already
decodes an incoming sysex message generically, confirmed live
(str(mido.Message('sysex', data=(1,2,3))) -> "sysex data=(1,2,3) time=0").

File-I/O note (.mid/.syx read AND write support): 'read_midi_file' takes a
'path' (absolute path to a .mid/.midi or .syx file) and an optional
'max_messages' (default 100, caps how many message strings are returned per
track/file so a huge file can't flood the response — set 0 to get only
metadata/counts with zero message bodies). For .mid/.midi: uses
mido.MidiFile(path), returns file type (0/1/2), ticks_per_beat,
length_seconds, and a per-track summary (index, track name, message count,
first tempo_bpm/time_signature/key_signature/instrument_name meta values
found, decoded messages up to the cap, truncated flag). For .syx: uses
mido.read_syx_file(path), returns message_count + decoded sysex message
strings up to the cap. Field names for all meta message types
(track_name.name, set_tempo.tempo, time_signature.numerator/denominator/
clocks_per_click/notated_32nd_notes_per_beat, key_signature.key,
instrument_name.name, smpte_offset.hours/minutes/seconds/frames/
sub_frames/frame_rate, etc.) verified live against
mido.midifiles.meta._META_SPEC_BY_TYPE before writing any code — same
discipline used throughout this file. mido.tempo2bpm() used to convert raw
tempo (microseconds per quarter note) to a human BPM figure for the
summary. 'write_midi_file' builds the corresponding mido.MidiFile/track
objects from caller-supplied data and saves them to disk, then immediately
calls 'read_midi_file' on what it just wrote as a built-in round-trip
sanity check — see `_write_midi_file`'s own docstring further down for
full detail.

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
            "MIDI 1.0 device discovery and I/O. "
            "Actions: 'list_devices' enumerates available MIDI input/output "
            "port names. 'open' opens a named port as either 'input' or "
            "'output' and returns a handle string to use in later calls. "
            "'send' sends a channel message (note_on, note_off, "
            "control_change, program_change, pitchwheel, aftertouch, "
            "polytouch), a system message (quarter_frame, songpos, "
            "song_select, tune_request, clock, start, stop, continue, "
            "active_sensing, reset), an arbitrary-payload System "
            "Exclusive message ('sysex', with a 'data' array of 0-127 "
            "integers, NOT including the leading 0xF0/trailing 0xF7 which "
            "are added automatically), or a typed 'mtc_full' convenience "
            "message (MIDI Time Code Full Message — jumps the timeline to "
            "an exact position in one message, built as a validated sysex "
            "payload under the hood: needs 'hours' 0-23, 'minutes' 0-59, "
            "'seconds' 0-59, 'frames' 0-29, and REQUIRED 'frame_rate' (one "
            "of '24'/'25'/'30drop'/'30nondrop' — no default, since guessing "
            "wrong here changes what the position means downstream; "
            "optional 'device_id' 0-127 defaults to 127/all-devices, which "
            "IS the spec's own stated default), or a typed 'mmc' message "
            "(MIDI Machine Control — transport control, also built as a "
            "validated sysex payload under the hood): needs a required "
            "'command', one of 'stop'/'play'/'deferred_play'/"
            "'fast_forward'/'rewind'/'record_strobe'/'record_exit'/"
            "'record_pause'/'pause'/'eject'/'chase'/'command_error_reset'/"
            "'mmc_reset' (no extra fields needed for any of these), or "
            "'locate' (needs 'hours'/'minutes'/'seconds'/'frames'/"
            "'frame_rate' same as 'mtc_full' above, PLUS 'subframes' "
            "0-99 — moves the receiving device's playhead to that exact "
            "position). Optional 'device_id' 0-127 defaults to 127/all-"
            "devices, same convention as 'mtc_full'. NOTE: Shuttle and "
            "Write commands are deliberately NOT supported (Shuttle's "
            "speed-byte encoding was under-specified in available sources, "
            "Write is a niche multitrack-recorder feature) — use the "
            "generic 'sysex' action directly if either is ever needed. "
            "There is also a typed 'msc' message (MIDI Show Control — "
            "stage/theatrical equipment control, also a validated sysex "
            "payload under the hood): needs 'command_format' (one of "
            "'lighting'/'sound'/'machinery'/'video'/'projection'/"
            "'process_control'/'pyro'/'all_types', the 8 top-level device "
            "categories) OR 'command_format_raw' (0-127, for a narrower "
            "sub-category not in that list — specify exactly one of the "
            "two), and 'command' (one of the 11 'General Category' "
            "commands, which apply to every command_format: 'go'/'stop'/"
            "'resume'/'timed_go'/'load'/'set'/'fire'/'all_off'/'restore'/"
            "'reset'/'go_off' — NOTE this is a shared field name with "
            "'mmc' above but a DIFFERENT set of valid values for 'msc'). "
            "'go'/'stop'/'resume'/'go_off' take optional 'q_number'/"
            "'q_list'/'q_path' (ASCII digit-and-dot strings, e.g. "
            "'q_list' requires 'q_number' too, 'q_path' requires "
            "'q_list' too). 'load' requires 'q_number' (same optional "
            "'q_list'/'q_path' rules). 'timed_go' requires 'hours'/"
            "'minutes'/'seconds'/'frames'/'fractional_frames'/'frame_rate' "
            "(same meaning as 'mtc_full', plus 'fractional_frames' 0-99) "
            "and takes the same optional q_number/q_list/q_path as 'go'. "
            "'set' requires 'control_number' and 'control_value' (each "
            "0-16383) and optionally the SAME 6 time fields as 'timed_go' "
            "— given ALL together or not at all. 'fire' requires "
            "'macro_number' (0-127). 'all_off'/'restore'/'reset' need no "
            "extra fields. Optional 'device_id' 0-127 defaults to 127/all-"
            "devices, same convention as 'mtc_full'/'mmc'. NOTE: the "
            "extended 15-command MSC 'Sound Commands' set (clock/cue-list-"
            "path management) is deliberately NOT supported — use the "
            "generic 'sysex' action directly if ever needed. "
            "All of the above are sent on an open output handle. "
            "'poll' does a "
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
                                "mtc_full", "mmc", "msc",
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
                        "hours": {
                            "type": "integer",
                            "description": "0-23. Required for 'mtc_full'.",
                        },
                        "minutes": {
                            "type": "integer",
                            "description": "0-59. Required for 'mtc_full'.",
                        },
                        "seconds": {
                            "type": "integer",
                            "description": "0-59. Required for 'mtc_full'.",
                        },
                        "frames": {
                            "type": "integer",
                            "description": "0-29. Required for 'mtc_full'.",
                        },
                        "frame_rate": {
                            "type": "string",
                            "enum": ["24", "25", "30drop", "30nondrop"],
                            "description": (
                                "Required for 'mtc_full' — no default, since "
                                "the wrong value changes what the position "
                                "means downstream."
                            ),
                        },
                        "device_id": {
                            "type": "integer",
                            "description": (
                                "0-127. Optional for 'mtc_full'/'mmc', "
                                "defaults to 127 (all devices) — the spec's "
                                "own default."
                            ),
                        },
                        "command": {
                            "type": "string",
                            "enum": [
                                # mmc values
                                "stop", "play", "deferred_play",
                                "fast_forward", "rewind", "record_strobe",
                                "record_exit", "record_pause", "pause",
                                "eject", "chase", "command_error_reset",
                                "mmc_reset", "locate",
                                # msc values (a DIFFERENT meaning of the
                                # same field name, disambiguated by the
                                # message's own 'type' — 'go'/'reset'
                                # deliberately don't clash with any mmc
                                # value above)
                                "go", "resume", "timed_go", "load", "set",
                                "fire", "all_off", "restore", "reset",
                                "go_off",
                            ],
                            "description": (
                                "Required for 'mmc' (14 values) or 'msc' "
                                "(11 DIFFERENT values, sharing this same "
                                "field name for the analogous role) — "
                                "which set applies depends on the "
                                "message's own 'type'."
                            ),
                        },
                        "subframes": {
                            "type": "integer",
                            "description": (
                                "0-99. Required for 'mmc' when 'command' "
                                "is 'locate'."
                            ),
                        },
                        "command_format": {
                            "type": "string",
                            "enum": [
                                "lighting", "sound", "machinery", "video",
                                "projection", "process_control", "pyro",
                                "all_types",
                            ],
                            "description": (
                                "Required for 'msc' (unless "
                                "'command_format_raw' is used instead)."
                            ),
                        },
                        "command_format_raw": {
                            "type": "integer",
                            "description": (
                                "0-127. Alternative to 'command_format' "
                                "for 'msc', for a narrower sub-category "
                                "not in the 8-value enum (e.g. a specific "
                                "type of moving light rather than "
                                "'lighting' in general). Specify exactly "
                                "one of the two, not both."
                            ),
                        },
                        "q_number": {
                            "type": "string",
                            "description": (
                                "'msc' only — ASCII digit/'.' string, e.g. "
                                "'235.6'. Required for 'load', optional "
                                "for 'go'/'stop'/'resume'/'timed_go'/"
                                "'go_off'."
                            ),
                        },
                        "q_list": {
                            "type": "string",
                            "description": (
                                "'msc' only — same ASCII format as "
                                "'q_number'. Requires 'q_number' to also "
                                "be given."
                            ),
                        },
                        "q_path": {
                            "type": "string",
                            "description": (
                                "'msc' only — same ASCII format as "
                                "'q_number'. Requires 'q_list' to also be "
                                "given."
                            ),
                        },
                        "fractional_frames": {
                            "type": "integer",
                            "description": (
                                "0-99. 'msc' only, required for "
                                "'timed_go' and (if any time field is "
                                "given at all) for 'set'."
                            ),
                        },
                        "control_number": {
                            "type": "integer",
                            "description": (
                                "0-16383. Required for 'msc's 'set' "
                                "command."
                            ),
                        },
                        "control_value": {
                            "type": "integer",
                            "description": (
                                "0-16383. Required for 'msc's 'set' "
                                "command."
                            ),
                        },
                        "macro_number": {
                            "type": "integer",
                            "description": (
                                "0-127. Required for 'msc's 'fire' "
                                "command."
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


def _err(message: str) -> str:
    return json.dumps({"error": message})


# Guards `open`/`send`/`close` against a hung ALSA/JACK driver or a
# misbehaving physical device — `_run` calls straight into python-rtmidi's C
# bindings via `asyncio.to_thread`, and without this a stuck call would wait
# forever with zero feedback to the caller. `poll` is the one action that
# doesn't need this (mido's `iter_pending()` is confirmed non-blocking — it
# just loops `poll()` until it returns None), but it's cheap/harmless to
# cover it too rather than special-case it out.
#
# Known limitation, stated plainly rather than hidden: this makes the *tool
# call* return promptly on a timeout, but does NOT kill the underlying OS
# thread — Python cannot forcibly cancel a running thread, so a genuinely
# stuck rtmidi call keeps occupying its slot in the shared default
# asyncio thread-pool executor (the same pool nearly every other local tool
# in this app also uses) until the whole process exits. Fixing that half
# would mean moving off `asyncio.to_thread` onto something killable (e.g. a
# subprocess) — deliberately out of scope here; this timeout only bounds how
# long the *caller* waits, not how long the leak persists.
_DEFAULT_TIMEOUT = 10.0


async def execute(name: str, tool_input: dict) -> str:
    if name != "midi1":
        return json.dumps({"error": f"unknown midi1 tool {name!r}"})
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_run, tool_input), timeout=_DEFAULT_TIMEOUT
        )
    except TimeoutError:
        return _err(
            f"midi1 action {tool_input.get('action')!r} timed out after "
            f"{_DEFAULT_TIMEOUT}s — a MIDI driver or device may be hung "
            "(the underlying blocking call could not be cancelled and may "
            "still be running in the background; see the note above "
            "_DEFAULT_TIMEOUT in core/midi1.py)"
        )


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


def close_all() -> None:
    """Close every still-open port. Called by local_tools.shutdown() on the
    way out — safe to call even if nothing was ever opened (mirrors every
    other tool's cleanup callback registered there, e.g. browser.shutdown/
    kernel.shutdown/data.close, all of which tolerate an idle/never-used
    state the same way).

    Best-effort per handle, same "one failure must not block the rest"
    contract local_tools.shutdown() itself already documents for the whole
    list of registered cleanups — a single stuck/already-dead port here
    must not prevent the others from being released.
    """
    for handle in list(_OPEN_PORTS):
        _, port = _OPEN_PORTS.pop(handle)
        try:
            port.close()
        except Exception as e:
            print(f"[midi1] close_all: failed to close {handle!r} (ignored): {e}")


# Shared by 'mtc_full' and MMC's 'locate' command — both encode an
# SMPTE-style hour byte as 0yyzzzzz (yy = frame-rate type,
# zzzzz = hours), confirmed identical in both specs (somascape.org's MTC
# section and MMC's Locate/Goto section use the exact same bit layout).
# Factored out here rather than duplicated so the two call sites can't
# silently drift apart from each other over time.
_FRAME_RATE_BITS = {"24": 0b00, "25": 0b01, "30drop": 0b10, "30nondrop": 0b11}


def _encode_smpte_hour_byte(hours: int, frame_rate: str) -> int:
    if not (0 <= hours <= 23):
        raise ValueError(f"'hours' must be 0-23, got {hours!r}")
    if frame_rate not in _FRAME_RATE_BITS:
        raise ValueError(
            f"'frame_rate' must be one of {sorted(_FRAME_RATE_BITS)}, "
            f"got {frame_rate!r}"
        )
    return (_FRAME_RATE_BITS[frame_rate] << 5) | hours


def _encode_msc_ascii_field(name: str, value: str) -> tuple:
    """MSC Q_number/Q_list/Q_path are plain ASCII digit strings
    with '.' as the decimal-point delimiter (confirmed against a literal
    worked example in the MSC 1.0 spec itself: cue "235.6" list "36.6" path
    "59" encodes as 32 33 35 2E 36 00 33 36 2E 36 00 35 39). Only validates
    the character set here — the spec's own leniency rules about repeated/
    misplaced dots are a RECEIVER tolerance requirement, not something a
    well-behaved sender needs to enforce on itself.
    """
    if not value or any(c not in "0123456789." for c in value):
        raise ValueError(
            f"{name!r} must be a non-empty string of digits and '.' only, "
            f"got {value!r}"
        )
    return tuple(ord(c) for c in value)


def _encode_msc_cue_data(q_number, q_list, q_path) -> tuple:
    """Shared by the 5 MSC General Category commands that carry
    optional trailing cue-targeting data (GO, STOP, RESUME, TIMED_GO,
    GO_OFF) — factored out once rather than repeated 5 times. Per spec:
    Q_list requires Q_number to also be present, Q_path requires Q_list.
    A single 0x00 delimiter separates each field that's actually present;
    trailing fields are simply omitted, not delimited with nothing after.
    """
    if q_number is None:
        if q_list is not None or q_path is not None:
            raise ValueError("'q_list'/'q_path' require 'q_number' too")
        return ()
    out = list(_encode_msc_ascii_field("q_number", q_number))
    if q_list is None:
        if q_path is not None:
            raise ValueError("'q_path' requires 'q_list' too")
        return tuple(out)
    out += [0x00] + list(_encode_msc_ascii_field("q_list", q_list))
    if q_path is None:
        return tuple(out)
    out += [0x00] + list(_encode_msc_ascii_field("q_path", q_path))
    return tuple(out)


def _encode_msc_time(
    hours: int, minutes: int, seconds: int, frames: int,
    fractional_frames: int, frame_rate: str,
) -> tuple:
    """MSC's own "Standard Time Code" — same 5-byte SHAPE as
    mtc_full/MMC-locate's time fields, but a MORE elaborate bit layout:
    minutes carries an extra "colour frame" flag bit, seconds carries a
    reserved-must-be-zero bit, and frames carries BOTH a sign bit and a
    subframes-vs-status identification bit, per somascape.org's MSC
    section (cross-referenced against the actual MSC 1.0 spec PDF, which
    confirms "MIDI Show Control time code ... specifications are entirely
    consistent with ... MIDI Time Code"). Deliberately narrowed scope:
    this tool always sends colour-frame=0, sign=positive, and the
    subframes (not "status") variant — none of those three flags are
    exposed as separate inputs,
    since they're rare edge cases and hardcoding safe defaults matches the
    same philosophy already used for e.g. mtc_full's device_id default.
    Only the hour byte's encoding is IDENTICAL to mtc_full/MMC's, hence
    still reusing _encode_smpte_hour_byte for that one byte only — minutes/
    seconds/frames/fractional_frames need their own construction here.
    """
    if not (0 <= minutes <= 59):
        raise ValueError(f"'minutes' must be 0-59, got {minutes!r}")
    if not (0 <= seconds <= 59):
        raise ValueError(f"'seconds' must be 0-59, got {seconds!r}")
    if not (0 <= frames <= 29):
        raise ValueError(f"'frames' must be 0-29, got {frames!r}")
    if not (0 <= fractional_frames <= 99):
        raise ValueError(
            f"'fractional_frames' must be 0-99, got {fractional_frames!r}"
        )
    hr_byte = _encode_smpte_hour_byte(hours, frame_rate)
    return (hr_byte, minutes, seconds, frames, fractional_frames)


def _encode_tuning_frequency(entry: dict) -> tuple:
    """Encode ONE MIDI Tuning frequency entry as the 3-byte format shared
    by both the Bulk Tuning Dump and Single Note Tuning Change messages
    (primary spec's "Frequency Data Format" section): byte 1 = nearest
    equal-tempered semitone BELOW the frequency (0-127), bytes 2-3 = a
    14-bit fraction of 100 cents ABOVE that semitone, MSB byte then LSB
    byte (confirmed via the byte-layout diagram `0xxxxxxx 0abcdefg
    0hijklmn` — 'a' is the fraction's most-significant bit, 'n' the
    least — this is MSB-then-LSB wire order, NOT the LSB-first convention
    used elsewhere in this file for e.g. RPN/MSC 14-bit fields; verified
    by hand-computing all 4 of the spec's own worked examples before
    writing this function, not assumed from the other fields' convention).

    Accepts EITHER `{"semitone": int 0-127, "cents": float 0-100
    exclusive}` (the tool computes the 14-bit fraction internally: 100
    cents = the full 14-bit range, per the spec's own stated resolution
    "100 cents / 2**14 = .0061 cents") OR `{"no_change": True}` for the
    spec's reserved (7F,7F,7F) sentinel meaning "make no change to this
    key's stored tuning" — deliberately NOT accepting a target frequency
    in Hz directly, to avoid introducing floating-point equal-temperament
    conversion as a NEW source of rounding/precision bugs on top of an
    already spec-precise encoding; semitone+cents mirrors the spec's own
    conceptual model exactly and is directly hand-verifiable against its
    worked examples (which is exactly how this function was verified).
    """
    if entry.get("no_change"):
        return (0x7F, 0x7F, 0x7F)
    semitone = entry.get("semitone")
    cents = entry.get("cents")
    if semitone is None:
        raise KeyError("'semitone' (or 'no_change': true)")
    if cents is None:
        raise KeyError("'cents' (or 'no_change': true)")
    if not (0 <= semitone <= 127):
        raise ValueError(f"'semitone' must be 0-127, got {semitone!r}")
    if not (0 <= cents < 100):
        raise ValueError(f"'cents' must be 0 <= cents < 100, got {cents!r}")
    frac14 = round(cents / 100.0 * 16384)
    frac14 = min(frac14, 16383)  # guard the round()-to-16384 edge at cents just under 100
    return (semitone, (frac14 >> 7) & 0x7F, frac14 & 0x7F)


def _encode_time_signature_pair(numerator: int, denominator: int) -> tuple:
    """Encode ONE (numerator, denominator) pair as the 2-byte `nn dd`
    format Notation Information's Time Signature messages use — shared by
    the primary pair and each additional compound-signature pair.
    `denominator` is accepted as the ACTUAL value (2, 4, 8, 16, ...), NOT
    the spec's own "negative power of 2" exponent — mirrors mido's own
    EXISTING `time_signature` MetaMessage field (already used elsewhere in
    this file for .mid files), confirmed live by constructing one and
    checking its encoded bytes (`denominator=4` produces byte value `2` =
    log2(4)) before writing this function, so a caller who already knows
    how to build a .mid time_signature meta event recognizes the same
    convention here.
    """
    if not (0 <= numerator <= 127):
        raise ValueError(f"'numerator' must be 0-127, got {numerator!r}")
    if denominator < 1 or (denominator & (denominator - 1)) != 0:
        raise ValueError(
            f"'denominator' must be a positive power of 2 (1, 2, 4, 8, "
            f"...), got {denominator!r}"
        )
    exponent = denominator.bit_length() - 1
    if not (0 <= exponent <= 127):
        raise ValueError(
            f"'denominator' {denominator!r} is out of representable range"
        )
    return (numerator, exponent)


def _encode_mtc_cueing_time(
    hours: int, minutes: int, seconds: int, frames: int,
    fractional_frames: int, frame_rate: str,
) -> tuple:
    """MTC (Non-Real-Time) Cueing's plain 5-byte time field (hr mn sc fr
    ff) — deliberately NOT reusing `_encode_msc_time` despite that
    function numerically producing the same bytes today for equivalent
    inputs: `_encode_msc_time`'s own docstring is explicit that it encodes
    MSC's OWN more-elaborate bit layout (extra colour-frame/sign/status
    flag bits, just hardcoded to safe defaults) — reusing it here would
    be semantically misleading for a future reader even though the output
    happens to coincide. This is MTC Cueing's own plain encoding: only the
    hour byte is shared (via `_encode_smpte_hour_byte`, the same one
    `mtc_full`/MMC-locate/MSC all already use), minutes/seconds/frames/
    fractional_frames have no extra bits at all.
    """
    if not (0 <= minutes <= 59):
        raise ValueError(f"'minutes' must be 0-59, got {minutes!r}")
    if not (0 <= seconds <= 59):
        raise ValueError(f"'seconds' must be 0-59, got {seconds!r}")
    if not (0 <= frames <= 29):
        raise ValueError(f"'frames' must be 0-29, got {frames!r}")
    if not (0 <= fractional_frames <= 99):
        raise ValueError(
            f"'fractional_frames' must be 0-99, got {fractional_frames!r}"
        )
    hr_byte = _encode_smpte_hour_byte(hours, frame_rate)
    return (hr_byte, minutes, seconds, frames, fractional_frames)


def _nibblize(raw_bytes) -> tuple:
    """Nibblize a list of raw 8-bit bytes into MIDI-safe 7-bit values, LS
    nibble first per byte — the encoding MTC Cueing's "additional info"
    field uses to embed an arbitrary MIDI message inside a Cueing Set-Up
    message (per the separate MTC spec's own "Additional Information"
    section). Verified against the spec's own worked example before
    writing any message-building code around it: `0x91 0x46 0x7F` (a real
    Note On status byte with its high bit set, plus 2 data bytes) nibblizes
    to `01 09 06 04 0F 07` — confirmed byte-for-byte via this exact
    function before it was ever wired into `_build_message`.
    """
    out: list = []
    for b in raw_bytes:
        if not (0 <= b <= 255):
            raise ValueError(
                f"additional-info bytes must each be 0-255 (a full "
                f"8-bit MIDI byte, pre-nibblization), got {b!r}"
            )
        out.append(b & 0x0F)
        out.append((b >> 4) & 0x0F)
    return tuple(out)


def _build_message(message: dict) -> "mido.Message":
    """Build a channel/system/sysex mido.Message from a tool-supplied dict.

    Shared by both live `send` and `write_midi_file`'s per-track messages —
    kept as one function rather than duplicated so the two stay in sync.
    Same required-field KeyErrors, same mido-raised ValueErrors for bad
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
        # System Exclusive — arbitrary payload. mido adds the
        # leading 0xF0 / trailing 0xF7 automatically; data bytes must each
        # be 0-127, mido itself raises ValueError otherwise (caught by the
        # broad except in every caller, same as every other message type).
        raw_data = message.get("data")
        if raw_data is None:
            raise KeyError("'data'")
        return mido.Message("sysex", data=tuple(raw_data), time=time)
    if msg_type == "mtc_full":
        # MIDI Time Code Full Message — a Universal Real Time
        # SysEx convenience wrapper (built on the SAME generic sysex path
        # above, not a separate one), for jumping the timeline to an exact
        # position in one message rather than accumulating Quarter Frames.
        # Wire format, confirmed against an independent technical reference
        # (somascape.org/midi/tech/spec.html), consistent with mido's own
        # existing System Common 'quarter_frame' handling already above:
        #   F0 7F <device_id> 01 01 hr mn sc fr F7
        #   hr = 0yyzzzzz : yy = frame-rate type (00=24fps 01=25fps
        #        10=30fps-drop 11=30fps-nondrop), zzzzz = hours (0-23)
        #   mn = minutes (0-59), sc = seconds (0-59), fr = frames (0-29)
        # `frame_rate` is REQUIRED (not defaulted) — it changes what the
        # position actually means downstream, so guessing wrong here would
        # be a real correctness bug, not just a missing convenience default,
        # unlike e.g. `channel` defaulting to 0 elsewhere in this function.
        # `device_id` defaults to 0x7F (all devices) — that IS the spec's
        # own stated default ("id = ID of target device (default = 7F = All
        # devices)"), a genuinely safe default to carry over, unlike
        # frame_rate above.
        hours = message.get("hours")
        minutes = message.get("minutes")
        seconds = message.get("seconds")
        frames = message.get("frames")
        frame_rate = message.get("frame_rate")
        if hours is None:
            raise KeyError("'hours'")
        if minutes is None:
            raise KeyError("'minutes'")
        if seconds is None:
            raise KeyError("'seconds'")
        if frames is None:
            raise KeyError("'frames'")
        if frame_rate is None:
            raise KeyError("'frame_rate'")
        device_id = message.get("device_id", 0x7F)

        if not (0 <= minutes <= 59):
            raise ValueError(f"'minutes' must be 0-59, got {minutes!r}")
        if not (0 <= seconds <= 59):
            raise ValueError(f"'seconds' must be 0-59, got {seconds!r}")
        if not (0 <= frames <= 29):
            raise ValueError(f"'frames' must be 0-29, got {frames!r}")
        if not (0 <= device_id <= 127):
            raise ValueError(f"'device_id' must be 0-127, got {device_id!r}")

        hr_byte = _encode_smpte_hour_byte(hours, frame_rate)

        return mido.Message(
            "sysex",
            data=(0x7F, device_id, 0x01, 0x01, hr_byte, minutes, seconds, frames),
            time=time,
        )
    if msg_type == "mmc":
        # MIDI Machine Control — a Universal Real Time SysEx
        # convenience wrapper (built on the SAME generic sysex path, same
        # rule the user set for 'mtc_full'). ONE typed message covers
        # MULTIPLE commands via a required 'command' sub-field, rather than
        # a dozen separate top-level message types (e.g. 'mmc_stop',
        # 'mmc_play', ...) — mirrors the same "one type, variable
        # data/sub-field" shape 'sysex' itself already has, and keeps the
        # message 'type' enum from ballooning. Wire format, confirmed
        # against somascape.org's MMC section, cross-checked against
        # en.wikipedia.org/wiki/MIDI_Machine_Control (matches exactly):
        #   No-data commands:  F0 7F <device_id> 06 <cc> F7
        #   Locate/Goto:       F0 7F <device_id> 06 44 06 01 hr mn sc fr sf F7
        #     (44 = Sub-ID#2/Locate, 06 = byte count that follows, 01 =
        #     sub-format, hr = SAME 0yyzzzzz encoding as mtc_full above —
        #     hence the shared _encode_smpte_hour_byte helper — sf = SMPTE
        #     sub-frame 0-99, a field mtc_full's own Full Message doesn't
        #     have)
        # NOT IMPLEMENTED (deliberately): Shuttle (0x47 — encoding under-
        # specified in what was sourced) and Write (0x40 — niche,
        # multitrack-specific). Use the generic 'sysex' action directly
        # for either if ever needed.
        # Command Error Reset (0x0C) IS included — cheap to include, one
        # more dict entry, no reason to leave it out just because Wikipedia's
        # table happened to omit it while somascape's didn't contradict it.
        _MMC_COMMANDS = {
            "stop": 0x01, "play": 0x02, "deferred_play": 0x03,
            "fast_forward": 0x04, "rewind": 0x05, "record_strobe": 0x06,
            "record_exit": 0x07, "record_pause": 0x08, "pause": 0x09,
            "eject": 0x0A, "chase": 0x0B, "command_error_reset": 0x0C,
            "mmc_reset": 0x0D,
        }
        command = message.get("command")
        if command is None:
            raise KeyError("'command'")
        device_id = message.get("device_id", 0x7F)
        if not (0 <= device_id <= 127):
            raise ValueError(f"'device_id' must be 0-127, got {device_id!r}")

        if command == "locate":
            hours = message.get("hours")
            minutes = message.get("minutes")
            seconds = message.get("seconds")
            frames = message.get("frames")
            subframes = message.get("subframes")
            frame_rate = message.get("frame_rate")
            if hours is None:
                raise KeyError("'hours'")
            if minutes is None:
                raise KeyError("'minutes'")
            if seconds is None:
                raise KeyError("'seconds'")
            if frames is None:
                raise KeyError("'frames'")
            if subframes is None:
                raise KeyError("'subframes'")
            if frame_rate is None:
                raise KeyError("'frame_rate'")
            if not (0 <= minutes <= 59):
                raise ValueError(f"'minutes' must be 0-59, got {minutes!r}")
            if not (0 <= seconds <= 59):
                raise ValueError(f"'seconds' must be 0-59, got {seconds!r}")
            if not (0 <= frames <= 29):
                raise ValueError(f"'frames' must be 0-29, got {frames!r}")
            if not (0 <= subframes <= 99):
                raise ValueError(
                    f"'subframes' must be 0-99, got {subframes!r}"
                )
            hr_byte = _encode_smpte_hour_byte(hours, frame_rate)
            return mido.Message(
                "sysex",
                data=(
                    0x7F, device_id, 0x06, 0x44, 0x06, 0x01,
                    hr_byte, minutes, seconds, frames, subframes,
                ),
                time=time,
            )

        if command not in _MMC_COMMANDS:
            raise ValueError(
                f"'command' must be one of "
                f"{sorted(_MMC_COMMANDS) + ['locate']}, got {command!r}"
            )
        return mido.Message(
            "sysex",
            data=(0x7F, device_id, 0x06, _MMC_COMMANDS[command]),
            time=time,
        )
    if msg_type == "msc":
        # MIDI Show Control — a Universal Real Time SysEx
        # convenience wrapper, same "layers on top of the generic sysex
        # path" rule as mtc_full/mmc. Wire format, confirmed against the
        # ACTUAL MSC 1.0 spec text (MMA Recommended Practice RP-002,
        # 1991-07-25 — the file's own header states it's "made available
        # here by permission of the MIDI Manufacturers Association"),
        # cross-checked against ETC's and a GitHub MIDIKit discussion's
        # independent descriptions of the same format string:
        #   F0 7F <device_id> 02 <command_format> <command> <data> F7
        # Only the 11 "General Category" commands are implemented (apply
        # to ALL command_formats, "highly recommended" per the spec, and what
        # real commercial gear actually implements — ETC's own docs note
        # their Express/Expression consoles "will only take Lighting GO,
        # STOP, RESUME, and FIRE"). The extended 15-command "Sound
        # Commands" set (clock/cue-list-path management) is deliberately
        # NOT implemented — use raw 'sysex' directly if ever needed.
        _MSC_FORMATS = {
            "lighting": 0x01, "sound": 0x10, "machinery": 0x20,
            "video": 0x30, "projection": 0x40, "process_control": 0x50,
            "pyro": 0x60, "all_types": 0x7F,
        }
        _MSC_COMMANDS = {
            "go": 0x01, "stop": 0x02, "resume": 0x03, "timed_go": 0x04,
            "load": 0x05, "set": 0x06, "fire": 0x07, "all_off": 0x08,
            "restore": 0x09, "reset": 0x0A, "go_off": 0x0B,
        }

        command_format = message.get("command_format")
        command_format_raw = message.get("command_format_raw")
        if command_format is not None and command_format_raw is not None:
            raise ValueError(
                "specify only ONE of 'command_format' or "
                "'command_format_raw', not both"
            )
        if command_format is None and command_format_raw is None:
            raise KeyError("'command_format' (or 'command_format_raw')")
        if command_format_raw is not None:
            if not (0 <= command_format_raw <= 127):
                raise ValueError(
                    f"'command_format_raw' must be 0-127, got "
                    f"{command_format_raw!r}"
                )
            cf_byte = command_format_raw
        else:
            if command_format not in _MSC_FORMATS:
                raise ValueError(
                    f"'command_format' must be one of "
                    f"{sorted(_MSC_FORMATS)} (or use 'command_format_raw' "
                    f"for a narrower sub-category), got {command_format!r}"
                )
            cf_byte = _MSC_FORMATS[command_format]

        command = message.get("command")
        if command is None:
            raise KeyError("'command'")
        if command not in _MSC_COMMANDS:
            raise ValueError(
                f"'command' must be one of {sorted(_MSC_COMMANDS)} for "
                f"'msc' (got {command!r} — note this is a DIFFERENT set "
                f"from 'mmc's own 'command' values)"
            )
        cmd_byte = _MSC_COMMANDS[command]

        device_id = message.get("device_id", 0x7F)
        if not (0 <= device_id <= 127):
            raise ValueError(f"'device_id' must be 0-127, got {device_id!r}")

        if command in ("go", "stop", "resume", "go_off"):
            payload: tuple = _encode_msc_cue_data(
                message.get("q_number"), message.get("q_list"),
                message.get("q_path"),
            )
        elif command == "load":
            q_number = message.get("q_number")
            if q_number is None:
                raise KeyError("'q_number' (required for 'load')")
            payload = _encode_msc_cue_data(
                q_number, message.get("q_list"), message.get("q_path"),
            )
        elif command == "timed_go":
            hours = message.get("hours")
            minutes = message.get("minutes")
            seconds = message.get("seconds")
            frames = message.get("frames")
            fractional_frames = message.get("fractional_frames")
            frame_rate = message.get("frame_rate")
            if hours is None:
                raise KeyError("'hours' (required for 'timed_go')")
            if minutes is None:
                raise KeyError("'minutes' (required for 'timed_go')")
            if seconds is None:
                raise KeyError("'seconds' (required for 'timed_go')")
            if frames is None:
                raise KeyError("'frames' (required for 'timed_go')")
            if fractional_frames is None:
                raise KeyError(
                    "'fractional_frames' (required for 'timed_go')"
                )
            if frame_rate is None:
                raise KeyError("'frame_rate' (required for 'timed_go')")
            payload = _encode_msc_time(
                hours, minutes, seconds, frames, fractional_frames,
                frame_rate,
            ) + _encode_msc_cue_data(
                message.get("q_number"), message.get("q_list"),
                message.get("q_path"),
            )
        elif command == "set":
            control_number = message.get("control_number")
            control_value = message.get("control_value")
            if control_number is None:
                raise KeyError("'control_number' (required for 'set')")
            if control_value is None:
                raise KeyError("'control_value' (required for 'set')")
            if not (0 <= control_number <= 16383):
                raise ValueError(
                    f"'control_number' must be 0-16383, got "
                    f"{control_number!r}"
                )
            if not (0 <= control_value <= 16383):
                raise ValueError(
                    f"'control_value' must be 0-16383, got "
                    f"{control_value!r}"
                )
            payload = (
                control_number & 0x7F, (control_number >> 7) & 0x7F,
                control_value & 0x7F, (control_value >> 7) & 0x7F,
            )
            set_hours = message.get("hours")
            set_minutes = message.get("minutes")
            set_seconds = message.get("seconds")
            set_frames = message.get("frames")
            set_fractional_frames = message.get("fractional_frames")
            set_frame_rate = message.get("frame_rate")
            set_time_provided = [
                v is not None for v in (
                    set_hours, set_minutes, set_seconds, set_frames,
                    set_fractional_frames, set_frame_rate,
                )
            ]
            if any(set_time_provided) and not all(set_time_provided):
                raise ValueError(
                    "SET's time fields (hours/minutes/seconds/frames/"
                    "fractional_frames/frame_rate) must be given ALL "
                    "together or not at all"
                )
            if all(set_time_provided):
                assert set_hours is not None
                assert set_minutes is not None
                assert set_seconds is not None
                assert set_frames is not None
                assert set_fractional_frames is not None
                assert set_frame_rate is not None
                payload += _encode_msc_time(
                    set_hours, set_minutes, set_seconds, set_frames,
                    set_fractional_frames, set_frame_rate,
                )
        elif command == "fire":
            macro_number = message.get("macro_number")
            if macro_number is None:
                raise KeyError("'macro_number' (required for 'fire')")
            if not (0 <= macro_number <= 127):
                raise ValueError(
                    f"'macro_number' must be 0-127, got {macro_number!r}"
                )
            payload = (macro_number,)
        else:
            # all_off, restore, reset — no data bytes at all.
            payload = ()

        return mido.Message(
            "sysex",
            data=(0x7F, device_id, 0x02, cf_byte, cmd_byte) + payload,
            time=time,
        )
    if msg_type == "gm_system":
        # General MIDI System On/Off — a Universal SysEx convenience
        # wrapper, same "layers on top of the generic sysex path, not a
        # separate/duplicate mechanism" rule as mtc_full/mmc/msc above.
        # UNLIKE those three, this one uses Universal ID 7E (Non-Real
        # Time), not 7F (Real Time) — confirmed against the MMA's own
        # MIDI 1.0 Detailed Specification (both the "General MIDI System
        # Messages" section and Table VIIa's "Currently Defined Universal
        # System Exclusive Messages" registry agree: Non-Real Time,
        # sub-ID#1 = 09). Wire format — the simplest of any typed sysex
        # helper in this file, no data bytes at all beyond the fixed
        # sub-ID#2:
        #   F0 7E <device_id> 09 01 F7   (General MIDI System On)
        #   F0 7E <device_id> 09 02 F7   (General MIDI System Off)
        # `device_id` defaults to 0x7F (all devices) — the spec's own
        # suggested default for this specific message ("ID of target
        # device (suggest using 7F 'All Call')"), same default already
        # used for mtc_full/mmc/msc above.
        _GM_SYSTEM_COMMANDS = {"on": 0x01, "off": 0x02}
        command = message.get("command")
        if command is None:
            raise KeyError("'command'")
        if command not in _GM_SYSTEM_COMMANDS:
            raise ValueError(
                f"'command' must be one of {sorted(_GM_SYSTEM_COMMANDS)} "
                f"for 'gm_system', got {command!r}"
            )
        device_id = message.get("device_id", 0x7F)
        if not (0 <= device_id <= 127):
            raise ValueError(f"'device_id' must be 0-127, got {device_id!r}")
        return mido.Message(
            "sysex",
            data=(0x7E, device_id, 0x09, _GM_SYSTEM_COMMANDS[command]),
            time=time,
        )
    if msg_type == "device_inquiry":
        # Device Inquiry — a Universal Non-Real Time SysEx convenience
        # wrapper (same generic-sysex-path rule as gm_system/mtc_full/mmc/
        # msc above), "General Information" sub-ID#1 = 06. Two message
        # shapes under one 'command' sub-field, same "one type, several
        # named commands" pattern already used by mmc/msc:
        #   Identity Request: F0 7E <device_id> 06 01 F7 — no data.
        #   Identity Reply:   F0 7E <device_id> 06 02 mm [..] ff ff dd dd
        #                      ss ss ss ss F7
        # Confirmed against the primary MIDI 1.0 Detailed Specification's
        # "Device Inquiry" section and Table VIIa.
        #   mm = manufacturer ID. Either ONE byte (1-127, direct
        #   assignment) OR the extended 3-byte form (00, then two more ID
        #   bytes) when the direct byte would be 00H — the spec's own text
        #   says so explicitly ("if the manufacturers id code (mm) begins
        #   with 00H then the above message is extended by two bytes").
        #   Accepted here as either a single int (1-127) or a 3-element
        #   list/tuple starting with 0 for the extended form.
        #   ff ff = device family code, 14 bits LSB-first (same split
        #   pattern used elsewhere in this file, e.g. msc's 'set' command).
        #   dd dd = device family member code, 14 bits LSB-first.
        #   ss ss ss ss = software revision level, 4 bytes, "format device
        #   specific" per the spec (no further structure defined) —
        #   accepted here as a raw 4-int list/tuple, each 0-127.
        # Sending an Identity REPLY is a rarer use case for a
        # controller-side tool (normally a physical device sends the
        # reply, not us) but is included for completeness/symmetry with
        # the spec, same "cover the message family fully" precedent as
        # mmc/msc rather than only the "obviously useful" half.
        _DEVICE_INQUIRY_COMMANDS = {"request", "reply"}
        command = message.get("command")
        if command is None:
            raise KeyError("'command'")
        if command not in _DEVICE_INQUIRY_COMMANDS:
            raise ValueError(
                f"'command' must be one of "
                f"{sorted(_DEVICE_INQUIRY_COMMANDS)} for 'device_inquiry', "
                f"got {command!r}"
            )
        device_id = message.get("device_id", 0x7F)
        if not (0 <= device_id <= 127):
            raise ValueError(f"'device_id' must be 0-127, got {device_id!r}")

        if command == "request":
            return mido.Message(
                "sysex", data=(0x7E, device_id, 0x06, 0x01), time=time,
            )

        # command == "reply"
        manufacturer_id = message.get("manufacturer_id")
        device_family_code = message.get("device_family_code")
        device_family_member_code = message.get("device_family_member_code")
        software_revision = message.get("software_revision")
        if manufacturer_id is None:
            raise KeyError("'manufacturer_id' (required for 'reply')")
        if device_family_code is None:
            raise KeyError("'device_family_code' (required for 'reply')")
        if device_family_member_code is None:
            raise KeyError(
                "'device_family_member_code' (required for 'reply')"
            )
        if software_revision is None:
            raise KeyError("'software_revision' (required for 'reply')")

        if isinstance(manufacturer_id, int):
            if not (1 <= manufacturer_id <= 127):
                raise ValueError(
                    "'manufacturer_id' as a single int must be 1-127 (use "
                    "a 3-element list/tuple starting with 0 for the "
                    f"extended form), got {manufacturer_id!r}"
                )
            mfr_bytes: tuple = (manufacturer_id,)
        else:
            mfr_bytes = tuple(manufacturer_id)
            if len(mfr_bytes) != 3 or mfr_bytes[0] != 0:
                raise ValueError(
                    "'manufacturer_id' as a list/tuple must have exactly "
                    f"3 bytes, the first being 0, got {manufacturer_id!r}"
                )
            for b in mfr_bytes:
                if not (0 <= b <= 127):
                    raise ValueError(
                        "'manufacturer_id' bytes must each be 0-127, got "
                        f"{manufacturer_id!r}"
                    )

        if not (0 <= device_family_code <= 16383):
            raise ValueError(
                f"'device_family_code' must be 0-16383, got "
                f"{device_family_code!r}"
            )
        if not (0 <= device_family_member_code <= 16383):
            raise ValueError(
                f"'device_family_member_code' must be 0-16383, got "
                f"{device_family_member_code!r}"
            )
        software_revision_bytes = tuple(software_revision)
        if len(software_revision_bytes) != 4:
            raise ValueError(
                "'software_revision' must be exactly 4 bytes, got "
                f"{software_revision!r}"
            )
        for b in software_revision_bytes:
            if not (0 <= b <= 127):
                raise ValueError(
                    "'software_revision' bytes must each be 0-127, got "
                    f"{software_revision!r}"
                )

        return mido.Message(
            "sysex",
            data=(0x7E, device_id, 0x06, 0x02) + mfr_bytes + (
                device_family_code & 0x7F,
                (device_family_code >> 7) & 0x7F,
                device_family_member_code & 0x7F,
                (device_family_member_code >> 7) & 0x7F,
            ) + software_revision_bytes,
            time=time,
        )
    if msg_type == "device_control":
        # Device Control — Master Volume/Balance — a Universal REAL TIME
        # SysEx convenience wrapper (same generic-sysex-path rule as the
        # others above), sub-ID#1 = 04. Unlike gm_system/device_inquiry
        # (both Non-Real-Time, 0x7E), this one uses Real-Time (0x7F) —
        # same ID as mtc_full/mmc/msc. Wire format, confirmed against the
        # primary MIDI 1.0 Detailed Specification's "Device Control"
        # section and Table VIIa:
        #   Master Volume:  F0 7F <device_id> 04 01 vv vv F7
        #   Master Balance: F0 7F <device_id> 04 02 bb bb F7
        # Both are a single 14-bit value, LSB-first (same split pattern
        # used elsewhere in this file, e.g. msc's 'set' command and
        # device_inquiry's family/member codes above). Per the spec, for
        # Master Volume 00 00 = volume off; for Master Balance 00 00 =
        # hard left and 7F 7F = hard right — these are just data-value
        # conventions at the receiving device, not encoding rules this
        # function needs to special-case.
        # These address the WHOLE DEVICE rather than a channel — the
        # channel-scoped equivalents already exist as ordinary
        # control_change messages (CC7 Channel Volume, CC8 Balance),
        # unrelated code path, no overlap to reconcile here.
        _DEVICE_CONTROL_COMMANDS = {"master_volume": 0x01, "master_balance": 0x02}
        command = message.get("command")
        if command is None:
            raise KeyError("'command'")
        if command not in _DEVICE_CONTROL_COMMANDS:
            raise ValueError(
                f"'command' must be one of "
                f"{sorted(_DEVICE_CONTROL_COMMANDS)} for 'device_control', "
                f"got {command!r}"
            )
        value = message.get("value")
        if value is None:
            raise KeyError("'value'")
        if not (0 <= value <= 16383):
            raise ValueError(f"'value' must be 0-16383, got {value!r}")
        device_id = message.get("device_id", 0x7F)
        if not (0 <= device_id <= 127):
            raise ValueError(f"'device_id' must be 0-127, got {device_id!r}")
        return mido.Message(
            "sysex",
            data=(
                0x7F, device_id, 0x04, _DEVICE_CONTROL_COMMANDS[command],
                value & 0x7F, (value >> 7) & 0x7F,
            ),
            time=time,
        )
    if msg_type == "channel_mode":
        # Channel Mode Messages — reuse the ORDINARY Control Change status
        # byte (BnH), with controller numbers 120-127 reserved for mode/
        # reset semantics instead of tone-shaping (confirmed against the
        # primary spec's "Channel Mode Messages" chapter and Table IV).
        # Technically already sendable today via raw 'control_change'
        # (nothing stops 'control_change' with controller=124) — this
        # typed wrapper adds named commands + validation + the extra
        # sub-fields a couple of these actually need, rather than making
        # the caller remember magic controller numbers and value-byte
        # conventions by hand.
        #   120 All Sound Off            value=0 always
        #   121 Reset All Controllers    value=0 always
        #   122 Local Control            value: 0=Off, 127=On
        #   123 All Notes Off            value=0 always
        #   124 Omni Mode Off            value=0 always
        #   125 Omni Mode On             value=0 always
        #   126 Mono Mode On (Poly Off)  value=M, number of channels
        #                                (0-16; 0 is the spec's own
        #                                special case meaning "voices
        #                                equal the receiver's own channel
        #                                count")
        #   127 Poly Mode On (Mono Off)  value=0 always
        # NOTE (from the spec's own Appendix, "Additional Explanations and
        # Application Notes" — read during this tool's spec survey):
        # commands 125-127 (Omni On/Mono On/Poly On) ALSO trigger an
        # implicit All Notes Off on a compliant receiver — a caller
        # switching modes should not be surprised that notes get cut.
        _CHANNEL_MODE_COMMANDS = {
            "all_sound_off": 120, "reset_all_controllers": 121,
            "local_control": 122, "all_notes_off": 123, "omni_off": 124,
            "omni_on": 125, "mono_on": 126, "poly_on": 127,
        }
        command = message.get("command")
        if command is None:
            raise KeyError("'command'")
        if command not in _CHANNEL_MODE_COMMANDS:
            raise ValueError(
                f"'command' must be one of "
                f"{sorted(_CHANNEL_MODE_COMMANDS)} for 'channel_mode', "
                f"got {command!r}"
            )
        control = _CHANNEL_MODE_COMMANDS[command]

        if command == "local_control":
            on = message.get("on")
            if on is None:
                raise KeyError("'on' (required for 'local_control')")
            mode_value = 127 if on else 0
        elif command == "mono_on":
            channel_count = message.get("channel_count")
            if channel_count is None:
                raise KeyError("'channel_count' (required for 'mono_on')")
            if not (0 <= channel_count <= 16):
                raise ValueError(
                    f"'channel_count' must be 0-16, got {channel_count!r}"
                )
            mode_value = channel_count
        else:
            mode_value = 0

        return mido.Message(
            "control_change", channel=channel, control=control,
            value=mode_value, time=time,
        )
    if msg_type == "midi_tuning":
        # MIDI Tuning — a Universal SysEx family, sub-ID#1 = 08, covering
        # 3 message shapes under one 'command' sub-field (same "one type,
        # several named commands" pattern as mmc/msc/gm_system/
        # device_inquiry/device_control above). Confirmed against the
        # primary spec's "MIDI Tuning" section, WITH the exact byte
        # layouts re-verified via a rendered page image (not just
        # `pdftotext`) before writing this — see the note below about a
        # genuine discrepancy that turned up doing that.
        #   bulk_dump_request (Non-Real Time, 0x7E):
        #     F0 7E <device_id> 08 00 tt F7
        #   bulk_dump_reply   (Non-Real Time, 0x7E):
        #     F0 7E <device_id> 08 01 tt <16 ASCII name bytes>
        #       <3 bytes>x128 (one per MIDI key, note 0 first) chksum F7
        #   note_change       (REAL Time, 0x7F — unlike the other two):
        #     F0 7F <device_id> 08 02 tt ll [kk xx yy zz]x ll F7
        # ⚠️ SPEC DISCREPANCY, worth recording plainly: the primary spec's
        # own printed text for bulk_dump_reply's checksum says "XOR of 7E
        # <device ID> 01 tt <388 bytes>" — TWO separate problems with
        # this, both visually re-verified against a rendered page image to
        # rule out a text-extraction error (it's genuinely printed this
        # way in the original 1996 document):
        #   1. It OMITS the 0x08 sub-ID#1 byte entirely from its own list
        #      (jumps straight from "<device ID>" to "01"), even though
        #      0x08 is unambiguously part of the actual wire message per
        #      the format line right above it ("F0 7E <device ID> 08 01
        #      tt ..."). A caught-live bug during this implementation:
        #      an early draft of this exact function reproduced that
        #      omission in the PAYLOAD itself (not just miscounting a
        #      byte total) — the byte-exact verification pass below is
        #      what caught it.
        #   2. Its own byte-count, <388 bytes>, doesn't match the
        #      message's documented structure either way: 16 (name) + 384
        #      (128 notes x 3 bytes) = 400 data bytes, not 388, and adding
        #      the missing 0x08 byte back in still doesn't reconcile it
        #      (400 vs 388 is a 12-byte gap, not the 1 byte 0x08 alone
        #      would explain).
        # Both point the same direction: this is a transcription slip in
        # the original document, not a deliberate design choice. This
        # implementation computes the checksum as the XOR of EVERY byte
        # actually transmitted between F0 and chksum (7E, device_id, 08,
        # 01, tt, all 16 name bytes, all 384 frequency bytes — 405 bytes
        # total), matching the same "checksum covers everything that came
        # before it" convention Sample Dump's and File Dump's checksums
        # both already use elsewhere in this same document — the
        # internally-consistent interpretation, not a guess.
        _MIDI_TUNING_COMMANDS = {"bulk_dump_request", "bulk_dump_reply", "note_change"}
        command = message.get("command")
        if command is None:
            raise KeyError("'command'")
        if command not in _MIDI_TUNING_COMMANDS:
            raise ValueError(
                f"'command' must be one of "
                f"{sorted(_MIDI_TUNING_COMMANDS)} for 'midi_tuning', got "
                f"{command!r}"
            )
        device_id = message.get("device_id", 0x7F)
        if not (0 <= device_id <= 127):
            raise ValueError(f"'device_id' must be 0-127, got {device_id!r}")
        tuning_program = message.get("tuning_program")
        if tuning_program is None:
            raise KeyError("'tuning_program'")
        if not (0 <= tuning_program <= 127):
            raise ValueError(
                f"'tuning_program' must be 0-127, got {tuning_program!r}"
            )

        if command == "bulk_dump_request":
            return mido.Message(
                "sysex",
                data=(0x7E, device_id, 0x08, 0x00, tuning_program),
                time=time,
            )

        if command == "bulk_dump_reply":
            tuning_name = message.get("tuning_name", "")
            if len(tuning_name) > 16:
                raise ValueError(
                    "'tuning_name' must be at most 16 characters, got "
                    f"{len(tuning_name)} ({tuning_name!r})"
                )
            name_bytes = tuple(ord(c) for c in tuning_name.ljust(16))
            for c, b in zip(tuning_name.ljust(16), name_bytes):
                if not (0 <= b <= 127):
                    raise ValueError(
                        f"'tuning_name' must be 7-bit ASCII, got "
                        f"non-ASCII character {c!r}"
                    )
            notes = message.get("notes")
            if notes is None:
                raise KeyError("'notes' (required for 'bulk_dump_reply')")
            if len(notes) != 128:
                raise ValueError(
                    "'notes' must have exactly 128 entries (one per MIDI "
                    f"key number), got {len(notes)}"
                )
            freq_bytes: tuple = ()
            for i, note_entry in enumerate(notes):
                try:
                    freq_bytes += _encode_tuning_frequency(note_entry)
                except KeyError as e:
                    raise KeyError(f"'notes'[{i}]: {e}") from e
                except ValueError as e:
                    raise ValueError(f"'notes'[{i}]: {e}") from e
            payload_before_checksum = (
                0x7E, device_id, 0x08, 0x01, tuning_program,
            ) + name_bytes + freq_bytes
            checksum = 0
            for b in payload_before_checksum:
                checksum ^= b
            return mido.Message(
                "sysex",
                data=payload_before_checksum + (checksum,),
                time=time,
            )

        # command == "note_change"
        changes = message.get("changes")
        if not changes:
            raise KeyError(
                "'changes' (required, non-empty, for 'note_change')"
            )
        if not (1 <= len(changes) <= 127):
            raise ValueError(
                f"'changes' must have 1-127 entries, got {len(changes)}"
            )
        change_bytes: tuple = ()
        for i, change_entry in enumerate(changes):
            key = change_entry.get("key")
            if key is None:
                raise KeyError(f"'changes'[{i}]: 'key'")
            if not (0 <= key <= 127):
                raise ValueError(
                    f"'changes'[{i}]: 'key' must be 0-127, got {key!r}"
                )
            try:
                freq = _encode_tuning_frequency(change_entry)
            except KeyError as e:
                raise KeyError(f"'changes'[{i}]: {e}") from e
            except ValueError as e:
                raise ValueError(f"'changes'[{i}]: {e}") from e
            change_bytes += (key,) + freq
        return mido.Message(
            "sysex",
            data=(
                0x7F, device_id, 0x08, 0x02, tuning_program, len(changes),
            ) + change_bytes,
            time=time,
        )
    if msg_type == "notation":
        # Notation Information — a Universal Real Time SysEx family,
        # sub-ID#1 = 03, covering 3 message shapes under one 'command'
        # sub-field (same pattern as mmc/msc/gm_system/device_inquiry/
        # device_control/channel_mode/midi_tuning above). Confirmed
        # against the primary spec's "Notation Information" section, with
        # the Time Signature wire format RE-VERIFIED via a rendered page
        # image — see the discrepancy note below.
        #   bar_marker: F0 7F <device_id> 03 01 aa aa F7
        #     aa aa = a SIGNED 14-bit bar number, LSB then MSB, encoded as
        #     ordinary two's-complement (Python's `n & 0x3FFF` produces
        #     the correct raw bit pattern for negative n directly — hand-
        #     verified against all 4 of the spec's own named sentinel
        #     values before writing this: -8192="not running", 0="last
        #     count-in bar", 1..8190="song bar numbers", 8191="running,
        #     bar number unknown").
        #   time_signature_immediate/_delayed:
        #     F0 7F <device_id> 03 <02|42> ln nn dd cc bb [nn dd...] F7
        # ⚠️ SPEC DISCREPANCY, worth recording plainly (a second one found
        # in this same document, after MIDI Tuning's checksum issue): the
        # primary spec's own compact wire-format line for BOTH Time
        # Signature variants prints an extra, spurious 'bb' token
        # ("...ln nn dd bb cc bb [nn dd...]") that does not match the
        # properly-aligned field-by-field definition list directly below
        # it (which lists exactly 5 distinct fields: ln, nn, dd, cc, bb —
        # no duplicate). Visually re-verified against a rendered page
        # image to rule out a text-extraction error — it's genuinely
        # printed that way in the original 1996 document. Independently
        # cross-checked against `mido`'s OWN existing `time_signature`
        # MetaMessage encoding (already used elsewhere in this file for
        # .mid files) by constructing one and inspecting its actual
        # encoded bytes — mido encodes as nn, dd(as a power-of-2
        # exponent), cc, bb, with NO duplicate byte, matching the properly
        # -aligned field list here, not the glitched wire-format line.
        # This implementation uses the doubly-confirmed 5-byte-plus-
        # compound-pairs layout (ln nn dd cc bb [nn dd...]), not literally
        # replicating the spec's own typo'd summary line.
        # `denominator` is accepted as the ACTUAL value (2/4/8/16/...),
        # not the exponent — mirrors mido's own field convention exactly,
        # see `_encode_time_signature_pair`'s own docstring.
        _NOTATION_COMMANDS = {
            "bar_marker", "time_signature_immediate", "time_signature_delayed",
        }
        command = message.get("command")
        if command is None:
            raise KeyError("'command'")
        if command not in _NOTATION_COMMANDS:
            raise ValueError(
                f"'command' must be one of {sorted(_NOTATION_COMMANDS)} "
                f"for 'notation', got {command!r}"
            )
        device_id = message.get("device_id", 0x7F)
        if not (0 <= device_id <= 127):
            raise ValueError(f"'device_id' must be 0-127, got {device_id!r}")

        if command == "bar_marker":
            bar_number = message.get("bar_number")
            if bar_number is None:
                raise KeyError("'bar_number' (required for 'bar_marker')")
            if not (-8192 <= bar_number <= 8191):
                raise ValueError(
                    f"'bar_number' must be -8192 to 8191, got {bar_number!r}"
                )
            raw14 = bar_number & 0x3FFF
            return mido.Message(
                "sysex",
                data=(
                    0x7F, device_id, 0x03, 0x01,
                    raw14 & 0x7F, (raw14 >> 7) & 0x7F,
                ),
                time=time,
            )

        # time_signature_immediate / time_signature_delayed
        numerator = message.get("numerator")
        denominator = message.get("denominator")
        clocks_per_click = message.get("clocks_per_click")
        notated_32nd_notes_per_beat = message.get("notated_32nd_notes_per_beat")
        if numerator is None:
            raise KeyError("'numerator'")
        if denominator is None:
            raise KeyError("'denominator'")
        if clocks_per_click is None:
            raise KeyError("'clocks_per_click'")
        if notated_32nd_notes_per_beat is None:
            raise KeyError("'notated_32nd_notes_per_beat'")
        if not (0 <= clocks_per_click <= 127):
            raise ValueError(
                f"'clocks_per_click' must be 0-127, got {clocks_per_click!r}"
            )
        if not (0 <= notated_32nd_notes_per_beat <= 127):
            raise ValueError(
                "'notated_32nd_notes_per_beat' must be 0-127, got "
                f"{notated_32nd_notes_per_beat!r}"
            )
        try:
            nn, dd = _encode_time_signature_pair(numerator, denominator)
        except ValueError as e:
            raise ValueError(f"primary time signature: {e}") from e

        compound = message.get("compound", [])
        compound_bytes: tuple = ()
        for i, pair in enumerate(compound):
            c_numerator = pair.get("numerator")
            c_denominator = pair.get("denominator")
            if c_numerator is None:
                raise KeyError(f"'compound'[{i}]: 'numerator'")
            if c_denominator is None:
                raise KeyError(f"'compound'[{i}]: 'denominator'")
            try:
                c_nn, c_dd = _encode_time_signature_pair(
                    c_numerator, c_denominator
                )
            except ValueError as e:
                raise ValueError(f"'compound'[{i}]: {e}") from e
            compound_bytes += (c_nn, c_dd)

        sub_id2 = 0x02 if command == "time_signature_immediate" else 0x42
        data_len = 4 + len(compound_bytes)
        return mido.Message(
            "sysex",
            data=(
                0x7F, device_id, 0x03, sub_id2, data_len,
                nn, dd, clocks_per_click, notated_32nd_notes_per_beat,
            ) + compound_bytes,
            time=time,
        )
    if msg_type == "mtc_cueing":
        # MTC (Real Time) Cueing — a Universal Real Time SysEx family,
        # sub-ID#1 = 05, from the SEPARATE MTC Detailed Specification
        # (RP-004/RP-008) rather than the primary MIDI 1.0 spec — this is
        # the smaller Real-Time subset of the fuller Non-Real-Time MTC
        # Cueing Set-Up message family (which also has Delete-variant
        # commands and extra Special sub-types not present here; NOT
        # implemented — see the "Explicitly deferred" notes elsewhere in
        # this project's memory). Wire format:
        #   F0 7F <device_id> 05 <sub-id#2> sl sm <additional info> F7
        # `sl sm` = a 14-bit Event Number, LSB then MSB (spec's own words:
        # "sl is the 7 LS bits, and sm is the 7 MS bits") — same LSB-first
        # convention as RPN/NRPN's parameter numbers elsewhere in this
        # file, unlike MIDI Tuning's MSB-first frequency fields (verified
        # per-field rather than assumed, same discipline throughout this
        # whole tool).
        # `<additional info>` is a NIBBLIZED MIDI data stream (see
        # `_nibblize` above) for every with-info command except
        # `event_name`, where it's nibblized ASCII instead.
        _MTC_CUEING_COMMANDS = {
            "special_system_stop": 0x00, "punch_in": 0x01, "punch_out": 0x02,
            "event_start": 0x05, "event_stop": 0x06,
            "event_start_with_info": 0x07, "event_stop_with_info": 0x08,
            "cue_point": 0x0B, "cue_point_with_info": 0x0C,
            "event_name": 0x0E,
        }
        command = message.get("command")
        if command is None:
            raise KeyError("'command'")
        if command not in _MTC_CUEING_COMMANDS:
            raise ValueError(
                f"'command' must be one of "
                f"{sorted(_MTC_CUEING_COMMANDS)} for 'mtc_cueing', got "
                f"{command!r}"
            )
        sub_id2 = _MTC_CUEING_COMMANDS[command]
        device_id = message.get("device_id", 0x7F)
        if not (0 <= device_id <= 127):
            raise ValueError(f"'device_id' must be 0-127, got {device_id!r}")

        if command == "special_system_stop":
            # Fixed Event Number = 04 00 (sl=0x04, sm=0x00) per the spec —
            # "All others reserved" for the Special sub-type, so there's
            # nothing for the caller to actually supply here.
            return mido.Message(
                "sysex",
                data=(0x7F, device_id, 0x05, sub_id2, 0x04, 0x00),
                time=time,
            )

        event_number = message.get("event_number")
        if event_number is None:
            raise KeyError("'event_number'")
        if not (0 <= event_number <= 16383):
            raise ValueError(
                f"'event_number' must be 0-16383, got {event_number!r}"
            )
        sl, sm = event_number & 0x7F, (event_number >> 7) & 0x7F

        if command == "event_name":
            event_name = message.get("event_name")
            if event_name is None:
                raise KeyError("'event_name' (required for 'event_name')")
            for c in event_name:
                if ord(c) > 127:
                    raise ValueError(
                        f"'event_name' must be 7-bit ASCII, got "
                        f"non-ASCII character {c!r}"
                    )
            info_bytes = _nibblize(ord(c) for c in event_name)
            return mido.Message(
                "sysex",
                data=(0x7F, device_id, 0x05, sub_id2, sl, sm) + info_bytes,
                time=time,
            )

        if command in ("event_start_with_info", "event_stop_with_info", "cue_point_with_info"):
            info_message = message.get("additional_info_message")
            info_raw_bytes = message.get("additional_info_bytes")
            if info_message is not None and info_raw_bytes is not None:
                raise ValueError(
                    "specify only ONE of 'additional_info_message' or "
                    "'additional_info_bytes', not both"
                )
            if info_message is None and info_raw_bytes is None:
                raise KeyError(
                    "'additional_info_message' (or 'additional_info_bytes'"
                    f") — required for {command!r}"
                )
            if info_message is not None:
                embedded = _build_message(info_message)
                raw_bytes = embedded.bytes()
            else:
                raw_bytes = info_raw_bytes
            info_bytes = _nibblize(raw_bytes)
            return mido.Message(
                "sysex",
                data=(0x7F, device_id, 0x05, sub_id2, sl, sm) + info_bytes,
                time=time,
            )

        # punch_in, punch_out, event_start, event_stop, cue_point — plain
        # event-number-only commands, no additional info at all.
        return mido.Message(
            "sysex",
            data=(0x7F, device_id, 0x05, sub_id2, sl, sm),
            time=time,
        )
    if msg_type == "mtc_cueing_nrt":
        # MTC (Non-Real-Time) Cueing — the FULLER Set-Up Message family
        # from the SEPARATE MTC Detailed Specification (RP-004/RP-008),
        # Non-Real Time (0x7E), sub-ID#1=04 — a superset of the Real-Time
        # 'mtc_cueing' type above: adds a full 5-byte time field to every
        # message (Real-Time drops it entirely — "the time would be as
        # soon as you receive this"), 4 Delete-variant commands, and 5
        # distinct "Special" sub-types (Real-Time's Special has only
        # System Stop, "all others reserved").
        # Wire format: F0 7E <device_id> 04 <sub-id#2> hr mn sc fr ff sl sm
        #              <additional info> F7
        # For the 6 'special_*' commands (sub-id#2=0x00), the Event Number
        # field (sl sm) is REPURPOSED to hold a fixed "Special Type"
        # selector (00 00 through 05 00) rather than a real event number —
        # spec's own words: "the Special Type takes the place of the
        # Event Number." 4 of the 6 (enable/disable/clear event list,
        # system stop) have their time field explicitly IGNORED by a
        # receiver per the spec ("types 01 00 through 04 00 ignore the
        # event time field") — sent as zeroed time bytes here rather than
        # exposing meaningless fields the caller would have to fill in for
        # no purpose. The other 2 (time_code_offset, event_list_request)
        # DO use the time field meaningfully, so those still take real
        # hours/minutes/seconds/frames/fractional_frames/frame_rate input.
        # NOTE: 'special_system_stop' is also a command name in the
        # SEPARATE 'mtc_cueing' (Real-Time) type above — no actual
        # conflict since these are two distinct top-level message types
        # (same "shared field name, different meaning per type" pattern
        # mmc/msc's own 'command' field already uses elsewhere).
        _MTC_CUEING_NRT_SPECIAL_TYPES = {
            "special_time_code_offset": 0x00,
            "special_enable_event_list": 0x01,
            "special_disable_event_list": 0x02,
            "special_clear_event_list": 0x03,
            "special_system_stop": 0x04,
            "special_event_list_request": 0x05,
        }
        _MTC_CUEING_NRT_PLAIN_COMMANDS = {
            "punch_in": 0x01, "punch_out": 0x02,
            "delete_punch_in": 0x03, "delete_punch_out": 0x04,
            "event_start": 0x05, "event_stop": 0x06,
            "event_start_with_info": 0x07, "event_stop_with_info": 0x08,
            "delete_event_start": 0x09, "delete_event_stop": 0x0A,
            "cue_point": 0x0B, "cue_point_with_info": 0x0C,
            "delete_cue_point": 0x0D, "event_name": 0x0E,
        }
        _all_nrt_commands = (
            set(_MTC_CUEING_NRT_SPECIAL_TYPES)
            | set(_MTC_CUEING_NRT_PLAIN_COMMANDS)
        )
        command = message.get("command")
        if command is None:
            raise KeyError("'command'")
        if command not in _all_nrt_commands:
            raise ValueError(
                f"'command' must be one of {sorted(_all_nrt_commands)} "
                f"for 'mtc_cueing_nrt', got {command!r}"
            )
        device_id = message.get("device_id", 0x7F)
        if not (0 <= device_id <= 127):
            raise ValueError(f"'device_id' must be 0-127, got {device_id!r}")

        if command in _MTC_CUEING_NRT_SPECIAL_TYPES:
            special_type = _MTC_CUEING_NRT_SPECIAL_TYPES[command]
            if command in (
                "special_time_code_offset", "special_event_list_request",
            ):
                hours = message.get("hours")
                minutes = message.get("minutes")
                seconds = message.get("seconds")
                frames = message.get("frames")
                fractional_frames = message.get("fractional_frames")
                frame_rate = message.get("frame_rate")
                if hours is None:
                    raise KeyError(f"'hours' (required for {command!r})")
                if minutes is None:
                    raise KeyError(f"'minutes' (required for {command!r})")
                if seconds is None:
                    raise KeyError(f"'seconds' (required for {command!r})")
                if frames is None:
                    raise KeyError(f"'frames' (required for {command!r})")
                if fractional_frames is None:
                    raise KeyError(
                        f"'fractional_frames' (required for {command!r})"
                    )
                if frame_rate is None:
                    raise KeyError(
                        f"'frame_rate' (required for {command!r})"
                    )
                time_bytes = _encode_mtc_cueing_time(
                    hours, minutes, seconds, frames, fractional_frames,
                    frame_rate,
                )
            else:
                # enable/disable/clear event list, system stop -- time
                # field explicitly ignored by receivers per the spec.
                time_bytes = (0, 0, 0, 0, 0)
            return mido.Message(
                "sysex",
                data=(0x7E, device_id, 0x04, 0x00) + time_bytes
                + (special_type, 0),
                time=time,
            )

        # Plain (non-Special) commands -- always need the full time field
        # plus a REAL caller-supplied event number.
        sub_id2 = _MTC_CUEING_NRT_PLAIN_COMMANDS[command]
        hours = message.get("hours")
        minutes = message.get("minutes")
        seconds = message.get("seconds")
        frames = message.get("frames")
        fractional_frames = message.get("fractional_frames")
        frame_rate = message.get("frame_rate")
        if hours is None:
            raise KeyError("'hours'")
        if minutes is None:
            raise KeyError("'minutes'")
        if seconds is None:
            raise KeyError("'seconds'")
        if frames is None:
            raise KeyError("'frames'")
        if fractional_frames is None:
            raise KeyError("'fractional_frames'")
        if frame_rate is None:
            raise KeyError("'frame_rate'")
        time_bytes = _encode_mtc_cueing_time(
            hours, minutes, seconds, frames, fractional_frames, frame_rate,
        )
        event_number = message.get("event_number")
        if event_number is None:
            raise KeyError("'event_number'")
        if not (0 <= event_number <= 16383):
            raise ValueError(
                f"'event_number' must be 0-16383, got {event_number!r}"
            )
        sl, sm = event_number & 0x7F, (event_number >> 7) & 0x7F

        if command == "event_name":
            event_name = message.get("event_name")
            if event_name is None:
                raise KeyError("'event_name' (required for 'event_name')")
            for c in event_name:
                if ord(c) > 127:
                    raise ValueError(
                        f"'event_name' must be 7-bit ASCII, got "
                        f"non-ASCII character {c!r}"
                    )
            info_bytes = _nibblize(ord(c) for c in event_name)
            return mido.Message(
                "sysex",
                data=(0x7E, device_id, 0x04, sub_id2) + time_bytes
                + (sl, sm) + info_bytes,
                time=time,
            )

        if command in (
            "event_start_with_info", "event_stop_with_info",
            "cue_point_with_info",
        ):
            info_message = message.get("additional_info_message")
            info_raw_bytes = message.get("additional_info_bytes")
            if info_message is not None and info_raw_bytes is not None:
                raise ValueError(
                    "specify only ONE of 'additional_info_message' or "
                    "'additional_info_bytes', not both"
                )
            if info_message is None and info_raw_bytes is None:
                raise KeyError(
                    "'additional_info_message' (or "
                    f"'additional_info_bytes') — required for {command!r}"
                )
            if info_message is not None:
                embedded = _build_message(info_message)
                raw_bytes = embedded.bytes()
            else:
                raw_bytes = info_raw_bytes
            info_bytes = _nibblize(raw_bytes)
            return mido.Message(
                "sysex",
                data=(0x7E, device_id, 0x04, sub_id2) + time_bytes
                + (sl, sm) + info_bytes,
                time=time,
            )

        # punch_in/out, delete_punch_in/out, event_start/stop,
        # delete_event_start/stop, cue_point, delete_cue_point -- plain,
        # no additional info at all.
        return mido.Message(
            "sysex",
            data=(0x7E, device_id, 0x04, sub_id2) + time_bytes + (sl, sm),
            time=time,
        )
    raise ValueError(
        f"unknown message type {msg_type!r} — expected one of "
        "note_on, note_off, control_change, program_change, "
        "pitchwheel, aftertouch, polytouch, quarter_frame, songpos, "
        "song_select, tune_request, clock, start, stop, continue, "
        "active_sensing, reset, sysex, mtc_full, mmc, msc, gm_system, "
        "device_inquiry, device_control, channel_mode, midi_tuning, "
        "notation, mtc_cueing, mtc_cueing_nrt"
    )


# Registered Parameter Numbers actually defined in the primary MIDI 1.0
# Detailed Specification's Table IIIa — the only 5 that have an
# MMA-assigned universal meaning. Values are LSB-first pairs as printed in
# that table (all 5 happen to share MSB=0x00). Non-Registered Parameter
# Numbers have no such universal names — 'nrpn' below always requires a
# raw 'parameter_number' instead.
_RPN_NAMED_PARAMETERS = {
    "pitch_bend_sensitivity": 0x0000,
    "fine_tuning": 0x0001,
    "coarse_tuning": 0x0002,
    "tuning_program_select": 0x0003,
    "tuning_bank_select": 0x0004,
}


def _build_rpn_or_nrpn_sequence(message: dict, *, registered: bool) -> list:
    """Build the short Control Change SEQUENCE that selects and sets an
    RPN ('registered=True') or NRPN ('registered=False') parameter.

    Unlike every sysex-based type in `_build_message` above (where one
    F0...F7 IS one physical message), RPN/NRPN genuinely require MULTIPLE
    wire messages — there is no single Status byte that encodes
    "select+set" in one shot. Sequence, confirmed against the primary
    spec's "Control Change"/"Registered and Non-Registered Parameter
    Numbers" section (Channel Voice Messages chapter) and the worked RPN
    example in the separate MIDI Tuning spec ("Changing Tuning Programs":
    `Bn 64 03 65 00 06 tt`, i.e. RPN LSB(100)=03, RPN MSB(101)=00, Data
    Entry MSB(6)=tt — confirms LSB-select-first ordering, not just
    byte-packing order):
      1. Parameter Number LSB  — CC100 (RPN) or CC98 (NRPN)
      2. Parameter Number MSB  — CC101 (RPN) or CC99 (NRPN)
      3. Data Entry MSB        — CC6
      4. Data Entry LSB        — CC38 (SKIPPED if 'msb_only' is True — the
         spec explicitly allows sending only the MSB "if seven bits of
         resolution is sufficient", Channel Voice Messages chapter,
         Control Change section)
    Only the 'set' operation (Data Entry) is implemented. Data
    Increment/Decrement (CC96/97) are DELIBERATELY NOT implemented — the
    primary spec names these controllers but never defines what their
    value byte actually means (Table III just lists "Data increment"/
    "Data decrement" with no further detail), the same genuinely-
    underspecified situation that already got MMC Shuttle deferred
    elsewhere in this file. Use raw 'control_change' directly (controller
    96 or 97) if a specific device's increment/decrement behavior is
    already known.
    """
    channel = message.get("channel", 0)
    time = message.get("time", 0)
    type_name = "rpn" if registered else "nrpn"

    if registered:
        parameter = message.get("parameter")
        parameter_number = message.get("parameter_number")
        if parameter is not None and parameter_number is not None:
            raise ValueError(
                "specify only ONE of 'parameter' or 'parameter_number', "
                "not both"
            )
        if parameter is None and parameter_number is None:
            raise KeyError("'parameter' (or 'parameter_number')")
        if parameter is not None:
            if parameter not in _RPN_NAMED_PARAMETERS:
                raise ValueError(
                    f"'parameter' must be one of "
                    f"{sorted(_RPN_NAMED_PARAMETERS)} (or use "
                    f"'parameter_number' for a raw 14-bit value), got "
                    f"{parameter!r}"
                )
            param_num = _RPN_NAMED_PARAMETERS[parameter]
        else:
            # Guaranteed not-None here (both-None already raised above,
            # 'parameter is None' is exactly why we're in this branch) —
            # mypy can't see across the separate if-statements though, same
            # "group-level check, not per-field" gotcha as Standing Rule 5.
            assert parameter_number is not None
            param_num = parameter_number
        select_lsb_cc, select_msb_cc = 100, 101
    else:
        parameter_number = message.get("parameter_number")
        if parameter_number is None:
            raise KeyError("'parameter_number'")
        param_num = parameter_number
        select_lsb_cc, select_msb_cc = 98, 99

    if not (0 <= param_num <= 16383):
        raise ValueError(
            f"parameter number must be 0-16383, got {param_num!r}"
        )

    value = message.get("value")
    if value is None:
        raise KeyError("'value'")
    msb_only = message.get("msb_only", False)

    msgs = [
        mido.Message(
            "control_change", channel=channel, control=select_lsb_cc,
            value=param_num & 0x7F, time=time,
        ),
        mido.Message(
            "control_change", channel=channel, control=select_msb_cc,
            value=(param_num >> 7) & 0x7F, time=0,
        ),
    ]

    if msb_only:
        if not (0 <= value <= 127):
            raise ValueError(
                f"'value' must be 0-127 when 'msb_only' is True for "
                f"'{type_name}', got {value!r}"
            )
        msgs.append(mido.Message(
            "control_change", channel=channel, control=6, value=value,
            time=0,
        ))
    else:
        if not (0 <= value <= 16383):
            raise ValueError(
                f"'value' must be 0-16383 for '{type_name}', got {value!r}"
            )
        msgs.append(mido.Message(
            "control_change", channel=channel, control=6,
            value=(value >> 7) & 0x7F, time=0,
        ))
        msgs.append(mido.Message(
            "control_change", channel=channel, control=38,
            value=value & 0x7F, time=0,
        ))

    return msgs


def _build_message_sequence(message: dict) -> list:
    """Build ONE OR MORE mido.Message objects from a tool-supplied dict —
    a thin wrapper AROUND `_build_message`, not a replacement for it.

    `_build_message` itself is completely UNCHANGED (still returns exactly
    one message, still used directly wherever only sysex messages are
    valid, e.g. the '.syx' file writer below) — every existing message
    type keeps flowing through that same, already-proven single-message
    function exactly as before. This wrapper exists ONLY because 'rpn' and
    'nrpn' are architecturally different: they need a short SEQUENCE of
    Control Change messages on the wire (see `_build_rpn_or_nrpn_sequence`
    above), which no single `mido.Message` can represent. Every other
    message type still produces exactly one message here, just wrapped in
    a 1-element list for a uniform return type.
    """
    msg_type = message.get("type")
    if msg_type == "rpn":
        return _build_rpn_or_nrpn_sequence(message, registered=True)
    if msg_type == "nrpn":
        return _build_rpn_or_nrpn_sequence(message, registered=False)
    return [_build_message(message)]


# The 17 file-only "meta" message types, verified live against
# mido.midifiles.meta._META_SPEC_BY_TYPE before writing any code (same
# "check mido's real spec, don't guess kwargs" discipline used throughout
# this file). None of these are valid on a live `send` — they only make
# sense inside a .mid file's track data.
_META_TYPES = frozenset({
    "track_name", "text", "copyright", "lyrics", "marker", "cue_marker",
    "instrument_name", "device_name", "set_tempo", "time_signature",
    "key_signature", "smpte_offset", "midi_port", "channel_prefix",
    "sequence_number", "sequencer_specific", "end_of_track",
})


def _build_meta_message(message: dict) -> "mido.MetaMessage":
    """Build a mido.MetaMessage from a tool-supplied dict, for
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
    done while designing this. Only ONE special case: `set_tempo` gets
    a `bpm` convenience alt-field (user-requested) that converts to
    mido's real `tempo` (microseconds/quarter)
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
        msgs = _build_message_sequence(message)
        for msg in msgs:
            port.send(msg)
    except KeyError as e:
        return _err(f"message missing required field: {e}")
    except Exception as e:
        return _err(f"send failed: {type(e).__name__}: {e}")

    # Every existing message type still produces exactly one physical
    # message — keep the response shape EXACTLY as before for those
    # ('sent': a single string) rather than changing the contract for
    # everyone just because 'rpn'/'nrpn' need more than one. Only when a
    # type genuinely sent multiple messages (currently just rpn/nrpn) does
    # 'sent' become a list of strings instead.
    if len(msgs) == 1:
        return json.dumps({"status": "ok", "sent": str(msgs[0])})
    return json.dumps({"status": "ok", "sent": [str(m) for m in msgs]})


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
    """Write a .mid/.midi or .syx file from tool-supplied track/message
    data.
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
                        track.append(_build_meta_message(m))
                    else:
                        # Almost every type produces exactly one message
                        # here; 'rpn'/'nrpn' are the only ones that expand
                        # to a short sequence (see
                        # `_build_message_sequence`'s own docstring) — the
                        # sequence's own internal timing (first message
                        # carries the caller's requested delta, the rest
                        # are time=0, i.e. sent back-to-back) already
                        # matches how a real RPN/NRPN select+set is meant
                        # to look in a recorded track.
                        track.extend(_build_message_sequence(m))
                except KeyError as e:
                    return _err(
                        f"track {ti} message {mi} missing required field: {e}"
                    )
                except Exception as e:
                    return _err(
                        f"track {ti} message {mi} invalid: "
                        f"{type(e).__name__}: {e}"
                    )
            mf.tracks.append(track)
        try:
            mf.save(path)
        except Exception as e:
            return _err(f"failed to write {path!r}: {type(e).__name__}: {e}")

    # Sanity-check the write by immediately re-reading what actually landed
    # on disk via the already-proven `_read_midi_file` — this both confirms
    # the write succeeded correctly AND avoids duplicating summary-building
    # logic here.
    return _read_midi_file({"path": path, "max_messages": 0})
