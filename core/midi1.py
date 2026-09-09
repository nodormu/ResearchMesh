"""MIDI 1.0 tool — device discovery, port I/O, and channel/system messages.

Built via `mido[ports-rtmidi]` (python-rtmidi backend). This started as the
shared discovery surface for both a MIDI 1.0 and a MIDI 2.0 tool; the MIDI
2.0 tool has since been removed from this project and moved to its own
standalone project for further work.

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


# Shared by 'mtc_full' (Chunk 3a) and MMC's 'locate' command (Chunk 3b) —
# both encode an SMPTE-style hour byte as 0yyzzzzz (yy = frame-rate type,
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
    """CHUNK 3c: MSC Q_number/Q_list/Q_path are plain ASCII digit strings
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
    """CHUNK 3c: shared by the 5 MSC General Category commands that carry
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
    """CHUNK 3c: MSC's own "Standard Time Code" — same 5-byte SHAPE as
    mtc_full/MMC-locate's time fields, but a MORE elaborate bit layout:
    minutes carries an extra "colour frame" flag bit, seconds carries a
    reserved-must-be-zero bit, and frames carries BOTH a sign bit and a
    subframes-vs-status identification bit, per somascape.org's MSC
    section (cross-referenced against the actual MSC 1.0 spec PDF, which
    confirms "MIDI Show Control time code ... specifications are entirely
    consistent with ... MIDI Time Code"). Deliberately narrowed scope,
    matching the plan written before this chunk started: this tool always
    sends colour-frame=0, sign=positive, and the subframes (not "status")
    variant — none of those three flags are exposed as separate inputs,
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
    if msg_type == "mtc_full":
        # CHUNK 3a: MIDI Time Code Full Message — a Universal Real Time
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
        # CHUNK 3b: MIDI Machine Control — a Universal Real Time SysEx
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
        # DELIBERATELY OUT OF SCOPE for this chunk (per the plan written
        # before starting): Shuttle (0x47 — encoding under-specified in
        # what was sourced) and Write (0x40 — niche, multitrack-specific).
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
        # CHUNK 3c: MIDI Show Control — a Universal Real Time SysEx
        # convenience wrapper, same "layers on top of the generic sysex
        # path" rule as mtc_full/mmc. Wire format, confirmed against the
        # ACTUAL MSC 1.0 spec text (MMA Recommended Practice RP-002,
        # 1991-07-25 — the file's own header states it's "made available
        # here by permission of the MIDI Manufacturers Association"),
        # cross-checked against ETC's and a GitHub MIDIKit discussion's
        # independent descriptions of the same format string:
        #   F0 7F <device_id> 02 <command_format> <command> <data> F7
        # Scope, matching the plan written before this chunk started:
        # only the 11 "General Category" commands (apply to ALL
        # command_formats, "highly recommended" per the spec, and what
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
    raise ValueError(
        f"unknown message type {msg_type!r} — expected one of "
        "note_on, note_off, control_change, program_change, "
        "pitchwheel, aftertouch, polytouch, quarter_frame, songpos, "
        "song_select, tune_request, clock, start, stop, continue, "
        "active_sensing, reset, sysex, mtc_full, mmc, msc"
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
    message data.
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
