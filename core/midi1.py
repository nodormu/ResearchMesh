"""MIDI 1.0 tool: device discovery, port I/O, typed message building, and
.mid/.syx files.

Built on mido with the python-rtmidi backend. If mido isn't installed, the
module still imports and every action returns an install hint.

Actions (dispatched by `_run`):
  - list_devices        input and output port names.
  - open / close        open a named port as "input" or "output"; `open`
                        returns a handle for later actions.
  - send                build a message from a typed dict and send it.
                        Most types are one wire message; rpn, nrpn and
                        mtc_quarter_frame_sequence are several. 'sent' in the
                        response is a string for one message and a list for
                        several.
  - poll                return messages buffered on an input handle: mido's
                        text, the raw hex, the decoded dict (_decode_message),
                        any RPN/NRPN or Quarter Frame change it completes
                        (_StreamDecoder), and a wall-clock 'received_at'
                        taken when the message arrived.
                        'timeout_seconds' (0-60, default 0) waits until a
                        message arrives or the timeout passes.
  - read_midi_file      .mid/.midi (mido.MidiFile) or .syx
                        (mido.read_syx_file): file info plus a per-track
                        summary and message text, capped by 'max_messages'.
  - write_midi_file     build a .mid/.midi or .syx file from typed message
                        dicts, then re-read it with read_midi_file as a check.
  - decode_mmc_response decode an MMC Response SysEx (F0 7F <dev> 07 ... F7,
                        passed with F0/F7 included).
  - run_clock           send MIDI Clock (24 per quarter note) at a set BPM for
                        a fixed duration, with Start/Continue before and Stop
                        after. One 'send' per tool call is too slow and uneven
                        to drive a device's tempo.

Where messages are built: single wire messages in `_build_message`,
multi-message types in `_build_message_sequence`, file-only meta events in
`_build_meta_message`. The 'type' enum in TOOLS lists every message type.

SysEx 'data' excludes F0/F7; mido adds them on send and strips them on
receive. Every data byte must be 0-127.

Limitation: incoming Active Sensing never reaches 'poll'. mido's rtmidi
backend calls ignore_types(False, False, True) on every input port, so RtMidi
drops it before mido sees it. Receiving it would need raw rtmidi.MidiIn.

Open ports and input buffers live in process memory; handles don't survive a
ResearchMesh restart.
"""

import asyncio
import itertools
import json
import os
import threading
import time
from collections import deque

try:
    import mido
    _MIDO_IMPORT_ERROR: Exception | None = None
except ImportError as e:
    mido = None  # type: ignore[assignment]
    _MIDO_IMPORT_ERROR = e

# --- Command tables ------------------------------------------------------
# For each message type with a 'command' field: command name -> the byte that
# selects it on the wire. _build_message validates 'command' against these,
# and the TOOLS 'command' enum is generated from them.

# MMC (RP-013) commands that carry no data: F0 7F <device_id> 06 <opcode> F7.
_MMC_NO_DATA_COMMANDS = {
    "stop": 0x01, "play": 0x02, "deferred_play": 0x03, "fast_forward": 0x04,
    "rewind": 0x05, "record_strobe": 0x06, "record_exit": 0x07,
    "record_pause": 0x08, "pause": 0x09, "eject": 0x0A, "chase": 0x0B,
    "command_error_reset": 0x0C, "mmc_reset": 0x0D,
}

# Non-Real Time MTC Cueing special types, sent where the event number goes.
_MTC_CUEING_NRT_SPECIAL_TYPES = {
    "special_time_code_offset": 0x00,
    "special_enable_event_list": 0x01,
    "special_disable_event_list": 0x02,
    "special_clear_event_list": 0x03,
    "special_system_stop": 0x04,
    "special_event_list_request": 0x05,
}

# File Dump handshakes: each is its own Non-Real Time sub-ID#1.
_FILE_DUMP_HANDSHAKE = {
    "eof": 0x7B, "wait": 0x7C, "cancel": 0x7D, "nak": 0x7E, "ack": 0x7F,
}

_COMMANDS: dict = {
    # MMC opcodes (RP-013).
    "mmc": {
        **_MMC_NO_DATA_COMMANDS,
        "write": 0x40, "masked_write": 0x41, "read": 0x42, "update": 0x43,
        "locate": 0x44, "variable_play": 0x45, "search": 0x46,
        "shuttle": 0x47, "step": 0x48, "assign_system_master": 0x49,
        "generator_command": 0x4A, "midi_time_code_command": 0x4B,
        "move": 0x4C, "add": 0x4D, "subtract": 0x4E,
        "drop_frame_adjust": 0x4F, "procedure": 0x50, "event": 0x51,
        "group": 0x52, "deferred_variable_play": 0x54,
        "record_strobe_variable": 0x55, "wait": 0x7C, "resume": 0x7F,
    },
    # MSC General Category commands (RP-002/014).
    "msc": {
        "go": 0x01, "stop": 0x02, "resume": 0x03, "timed_go": 0x04,
        "load": 0x05, "set": 0x06, "fire": 0x07, "all_off": 0x08,
        "restore": 0x09, "reset": 0x0A, "go_off": 0x0B,
    },
    # Universal Non-Real Time 09 <code>.
    "gm_system": {"on": 0x01, "off": 0x02},
    # Universal Non-Real Time 06 <code>.
    "device_inquiry": {"request": 0x01, "reply": 0x02},
    # Universal Real Time 04 <code>.
    "device_control": {"master_volume": 0x01, "master_balance": 0x02},
    # Control Change controller numbers.
    "channel_mode": {
        "all_sound_off": 120, "reset_all_controllers": 121,
        "local_control": 122, "all_notes_off": 123, "omni_off": 124,
        "omni_on": 125, "mono_on": 126, "poly_on": 127,
    },
    # MIDI Tuning 08 <code>.
    "midi_tuning": {
        "bulk_dump_request": 0x00, "bulk_dump_reply": 0x01,
        "note_change": 0x02,
    },
    # Notation 03 <code>.
    "notation": {
        "bar_marker": 0x01, "time_signature_immediate": 0x02,
        "time_signature_delayed": 0x42,
    },
    # Real Time MTC Cueing 05 <code>.
    "mtc_cueing": {
        "special_system_stop": 0x00, "punch_in": 0x01, "punch_out": 0x02,
        "event_start": 0x05, "event_stop": 0x06,
        "event_start_with_info": 0x07, "event_stop_with_info": 0x08,
        "cue_point": 0x0B, "cue_point_with_info": 0x0C, "event_name": 0x0E,
    },
    # Non-Real Time MTC Cueing 04 <code>; every special uses 00.
    "mtc_cueing_nrt": {
        **{name: 0x00 for name in _MTC_CUEING_NRT_SPECIAL_TYPES},
        "punch_in": 0x01, "punch_out": 0x02,
        "delete_punch_in": 0x03, "delete_punch_out": 0x04,
        "event_start": 0x05, "event_stop": 0x06,
        "event_start_with_info": 0x07, "event_stop_with_info": 0x08,
        "delete_event_start": 0x09, "delete_event_stop": 0x0A,
        "cue_point": 0x0B, "cue_point_with_info": 0x0C,
        "delete_cue_point": 0x0D, "event_name": 0x0E,
    },
    # File Dump 07 <code>, plus the handshakes.
    "file_dump": {
        "header": 0x01, "data_packet": 0x02, "request": 0x03,
        **_FILE_DUMP_HANDSHAKE,
    },
}

# MSC command_format names (RP-002/014).
_MSC_FORMATS = {
    "lighting": 0x01, "sound": 0x10, "machinery": 0x20, "video": 0x30,
    "projection": 0x40, "process_control": 0x50, "pyro": 0x60,
    "all_types": 0x7F,
}


def _command_enum() -> list:
    """Every command name in _COMMANDS, each once, grouped by type."""
    names: list = []
    for table in _COMMANDS.values():
        names += [name for name in table if name not in names]
    return names


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
            "'device_id' 0-127, not required, defaults to 127/all-devices, "
            "which IS the spec's own stated default), or a typed 'mmc' message "
            "(MIDI Machine Control — transport control, also built as a "
            "validated sysex payload under the hood): needs a required "
            "'command', one of 'stop'/'play'/'deferred_play'/"
            "'fast_forward'/'rewind'/'record_strobe'/'record_exit'/"
            "'record_pause'/'pause'/'eject'/'chase'/'command_error_reset'/"
            "'mmc_reset' (no extra fields needed for any of these), or "
            "'locate' (needs 'hours'/'minutes'/'seconds'/'frames'/"
            "'frame_rate' same as 'mtc_full' above, PLUS 'subframes' "
            "0-99 — moves the receiving device's playhead to that exact "
            "position). 'device_id' 0-127, not required, defaults to 127/all-"
            "devices, same convention as 'mtc_full'. NOTE: the 'command' "
            "enum also accepts the later RP-013 extensions, including "
            "'shuttle'/'variable_play'/'search' (need 'speed') and the "
            "Information Field commands 'read'/'write'/'masked_write'/"
            "'update' — see the 'command' field's own enum for the full "
            "list; use the generic 'sysex' action for anything not in it. "
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
            "'go'/'stop'/'resume'/'go_off' may include 'q_number'/"
            "'q_list'/'q_path' (none required; ASCII digit-and-dot strings, "
            "e.g. 'q_list' requires 'q_number' too, 'q_path' requires "
            "'q_list' too). 'load' requires 'q_number' (same 'q_list'/"
            "'q_path' rules, neither required). 'timed_go' requires 'hours'/"
            "'minutes'/'seconds'/'frames'/'fractional_frames'/'frame_rate' "
            "(same meaning as 'mtc_full', plus 'fractional_frames' 0-99) "
            "and may include the same q_number/q_list/q_path as 'go'. "
            "'set' requires 'control_number' and 'control_value' (each "
            "0-16383) and may include the SAME 6 time fields as 'timed_go' "
            "— given ALL together or not at all. 'fire' requires "
            "'macro_number' (0-127). 'all_off'/'restore'/'reset' need no "
            "extra fields. 'device_id' 0-127, not required, defaults to 127/all-"
            "devices, same convention as 'mtc_full'/'mmc'. NOTE: the "
            "extended 15-command MSC 'Sound Commands' set (clock/cue-list-"
            "path management) is deliberately NOT supported — use the "
            "generic 'sysex' action directly if ever needed. "
            "All of the above are sent on an open output handle. "
            "'poll' checks for buffered messages on an open input handle "
            "— decodes any incoming MIDI message generically, not just "
            "the types 'send' explicitly supports. Each returned message "
            "has 'message' (mido's text), 'hex' (the raw bytes), "
            "'decoded' (the same dict 'send' takes; SysEx with no decoder "
            "comes back as type 'sysex', MMC replies as type "
            "'mmc_response'), and, when it finishes a multi-message "
            "change, 'completes' (an 'rpn'/'nrpn' parameter change or an "
            "'mtc_quarter_frame_sequence' time). Each also "
            "includes a real wall-clock 'received_at' timestamp (epoch "
            "seconds) captured at actual arrival time, and nothing is "
            "lost between calls while the port stays open (continuously "
            "captured into a bounded 10,000-message buffer regardless of "
            "poll timing). By default returns instantly with whatever's "
            "already buffered; set 'timeout_seconds' (0-60, not required, "
            "default 0) to instead BLOCK until either a message arrives "
            "or that many seconds elapse, waking up early rather than "
            "always waiting the full duration. 'close' closes a "
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
            "'midi_file_type' (0/1/2, default 1, not required) and "
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
            "a built-in round-trip sanity check. "
            "'run_clock' drives a real, precisely-paced MIDI Real-Time "
            "Clock stream (24 pulses per quarter note) on an open output "
            "handle for a fixed duration — for hardware that's been set "
            "to follow an EXTERNAL clock/transport source (common on "
            "grooveboxes/drum machines with a 'sync source' menu set to "
            "MIDI/USB/AUTO rather than INTERNAL), a single 'send' of "
            "'start' alone typically only ARMS the transport; the device "
            "then waits for actual Clock pulses to advance, and pulses "
            "sent one 'send' call at a time can't be paced tightly enough "
            "(round-trip call latency dwarfs the ~20ms/tick a musical "
            "tempo needs) — hence this dedicated, self-contained action "
            "that paces the whole stream internally with a real "
            "monotonic schedule instead of depending on per-message call "
            "timing. Required: 'handle' (an open OUTPUT handle), 'bpm' "
            "(20-300), 'duration_seconds' (0 exclusive to 120 inclusive — "
            "capped short deliberately, since unlike 'poll' this is "
            "ACTIVELY driving hardware I/O the whole time, not just "
            "idly waiting; call again for a longer run). Not required: "
            "'transport' — 'start' (default, sent once before the clock "
            "stream begins), 'continue' (resume rather than restart-from-"
            "beginning, on gear that distinguishes the two), or 'none' "
            "(send bare Clock only, no transport message at all — for "
            "tempo-following without triggering play/already-started "
            "gear). Also not required: 'stop_at_end' (boolean, default true) — "
            "sends a 'stop' message once the clock stream finishes; set "
            "false to leave the receiving device running/armed on its "
            "own after this call returns. The call blocks for "
            "approximately 'duration_seconds' (the tool's own timeout is "
            "extended to accommodate this, same mechanism as 'poll's "
            "'timeout_seconds'). Returns 'ticks_sent', 'elapsed_seconds' "
            "(actual measured wall-clock duration of the clock stream, "
            "for comparing against the requested 'duration_seconds'), "
            "'transport_sent', and 'stop_sent'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_devices", "open", "close", "send", "poll",
                        "read_midi_file", "write_midi_file",
                        "decode_mmc_response", "run_clock",
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
                "timeout_seconds": {
                    "type": "number",
                    "description": (
                        "Not required for 'poll'. 0-60, default 0 (instant, "
                        "non-blocking — returns immediately with whatever "
                        "is already buffered). A value above 0 instead "
                        "BLOCKS until either a message arrives or this "
                        "many seconds elapse, returning early as soon as "
                        "something shows up rather than always waiting "
                        "the full duration."
                    ),
                },
                "bpm": {
                    "type": "number",
                    "description": (
                        "Required for 'run_clock'. Tempo in beats per "
                        "minute, 20-300. Converted internally to a "
                        "24-pulses-per-quarter-note MIDI Clock interval "
                        "(seconds/tick = 60/bpm/24)."
                    ),
                },
                "duration_seconds": {
                    "type": "number",
                    "description": (
                        "Required for 'run_clock'. How long to run the "
                        "Clock stream, > 0 and <= 120 seconds. The tool "
                        "call itself blocks for approximately this long — "
                        "call again for a longer run rather than raising "
                        "this past the cap."
                    ),
                },
                "transport": {
                    "type": "string",
                    "enum": ["start", "continue", "none"],
                    "description": (
                        "Not required for 'run_clock', default 'start'. "
                        "Which (if any) MIDI Real-Time transport message "
                        "to send once, immediately before the Clock "
                        "stream begins: 'start' (from the beginning), "
                        "'continue' (resume, on gear that distinguishes "
                        "the two), or 'none' (bare Clock only, e.g. for "
                        "tempo-following gear that's already running/"
                        "armed by other means)."
                    ),
                },
                "stop_at_end": {
                    "type": "boolean",
                    "description": (
                        "Not required for 'run_clock', default true. Sends a "
                        "'stop' message once the Clock stream finishes. "
                        "Set false to leave the receiving device running/"
                        "armed on its own after this call returns."
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
                                # Channel messages
                                "note_on", "note_off", "control_change",
                                "program_change", "pitchwheel", "aftertouch",
                                "polytouch", "channel_mode",
                                # System Common
                                "quarter_frame", "songpos", "song_select",
                                "tune_request",
                                # System Real-Time
                                "clock", "start", "stop", "continue",
                                "active_sensing", "reset",
                                # System Exclusive, raw and typed
                                "sysex",
                                "mtc_full", "mtc_nak", "mmc", "msc",
                                "gm_system", "device_inquiry",
                                "device_control", "midi_tuning", "notation",
                                "mtc_cueing", "mtc_cueing_nrt", "file_dump",
                                # Several wire messages each; 'sent' is a list
                                "rpn", "nrpn", "mtc_quarter_frame_sequence",
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
                                "0-127. Not required for 'mtc_full'/'mmc', "
                                "defaults to 127 (all devices) — the spec's "
                                "own default."
                            ),
                        },
                        "command": {
                            "type": "string",
                            "enum": _command_enum(),
                            "description": (
                                "Sub-command for mmc, msc, gm_system, "
                                "device_inquiry, device_control, channel_mode, "
                                "midi_tuning, notation, mtc_cueing, "
                                "mtc_cueing_nrt and file_dump. Valid values "
                                "depend on 'type' (grouped by type in this "
                                "enum); a wrong one returns an error listing "
                                "the valid values for that type."
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
                                "'235.6'. Required for 'load', not required "
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
                "data": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Required for 'decode_mmc_response'. The FULL raw "
                        "sysex byte sequence to decode, INCLUDING the "
                        "leading 0xF0 and trailing 0xF7 (the opposite "
                        "convention from 'message.data' on 'sysex' sends, "
                        "which excludes both — chosen this way since this "
                        "is meant to accept bytes copied straight from a "
                        "real captured MMC Response, e.g. a 'poll' "
                        "result's own raw bytes). Decodes the 15 "
                        "Information Fields already supported by "
                        "'read'/'write'/'update' (SELECTED_TIME_CODE, "
                        "SELECTED_MASTER_CODE, REQUESTED_OFFSET, "
                        "ACTUAL_OFFSET, LOCK_DEVIATION, GENERATOR_TIME_CODE, "
                        "MIDI_TIME_CODE_INPUT, GP0-GP7) plus RESPONSE "
                        "ERROR; anything else is reported as an 'unknown' "
                        "type with the raw name byte rather than guessed."
                    ),
                },
            },
            "required": ["action"],
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}
_MESSAGE_TYPES = TOOLS[0]["input_schema"]["properties"]["message"]["properties"]["type"]["enum"]

# handle -> (direction, mido port object). Process memory only.
_OPEN_PORTS: dict = {}
_HANDLE_COUNTER = itertools.count(1)

# Per input handle: a deque of (received_at, mido.Message), an Event that
# _poll waits on, and a _StreamDecoder that _poll feeds in arrival order. The callback _open registers fills both. It replaces mido's
# internal queue (mido's rtmidi Input delivers each message to the callback
# or the queue, never both) so each message gets a time.time() stamp when it
# arrives. The deque drops its oldest entries past _INPUT_BUFFER_MAXLEN.
_INPUT_BUFFER_MAXLEN = 10_000
_INPUT_BUFFERS: dict = {}
_INPUT_EVENTS: dict = {}
_STREAM_DECODERS: dict = {}


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


def _err(message: str) -> str:
    return json.dumps({"error": message})


# execute() runs every action in a worker thread under asyncio.wait_for, so
# a hung driver or device can't block the caller forever. A timeout only ends
# the wait: Python can't kill the thread, so a stuck call holds its slot in
# the shared thread pool until the process exits. That's why poll's wait and
# run_clock's duration have caps.
_DEFAULT_TIMEOUT = 10.0

# Largest 'timeout_seconds' poll accepts.
_MAX_POLL_TIMEOUT = 60.0

# Extra time execute() allows past poll's own wait, so an empty wait returns
# normally instead of being reported as a hung driver.
_POLL_TIMEOUT_MARGIN = 2.0

# Largest 'duration_seconds' run_clock accepts. Call again for a longer run.
_MAX_CLOCK_DURATION = 120.0

# Extra time execute() allows past run_clock's duration, for the loop to
# finish and send Stop.
_CLOCK_TIMEOUT_MARGIN = 5.0


async def execute(name: str, tool_input: dict) -> str:
    if name != "midi1":
        return json.dumps({"error": f"unknown midi1 tool {name!r}"})

    effective_timeout = _DEFAULT_TIMEOUT
    action = tool_input.get("action")
    if action == "poll":
        requested = tool_input.get("timeout_seconds")
        if isinstance(requested, (int, float)) and not isinstance(requested, bool):
            effective_timeout = max(
                _DEFAULT_TIMEOUT, requested + _POLL_TIMEOUT_MARGIN
            )
    elif action == "run_clock":
        requested = tool_input.get("duration_seconds")
        if isinstance(requested, (int, float)) and not isinstance(requested, bool):
            effective_timeout = max(
                _DEFAULT_TIMEOUT, requested + _CLOCK_TIMEOUT_MARGIN
            )

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_run, tool_input), timeout=effective_timeout
        )
    except TimeoutError:
        return _err(
            f"midi1 action {tool_input.get('action')!r} timed out after "
            f"{effective_timeout}s — a MIDI driver or device may be hung "
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
    if action == "decode_mmc_response":
        return _decode_mmc_response(tool_input)
    if action == "run_clock":
        return _run_clock(tool_input)
    return _err(
        f"unknown action {action!r} — expected one of "
        "list_devices, open, close, send, poll, read_midi_file, "
        "write_midi_file, decode_mmc_response, run_clock"
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

    handle = f"midi1-{next(_HANDLE_COUNTER)}"

    try:
        if direction == "input":
            # Runs on rtmidi's thread with an already-parsed mido.Message.
            # deque append/popleft and Event set/wait/clear need no lock
            # with one producer and one consumer.
            buf: deque = deque(maxlen=_INPUT_BUFFER_MAXLEN)
            event = threading.Event()

            def _on_message(msg: "mido.Message", _buf: deque = buf, _event: threading.Event = event) -> None:
                _buf.append((time.time(), msg))
                _event.set()

            port = mido.open_input(port_name, callback=_on_message)
            _INPUT_BUFFERS[handle] = buf
            _INPUT_EVENTS[handle] = event
            _STREAM_DECODERS[handle] = _StreamDecoder()
        else:
            port = mido.open_output(port_name)
    except Exception as e:
        _INPUT_BUFFERS.pop(handle, None)
        _INPUT_EVENTS.pop(handle, None)
        _STREAM_DECODERS.pop(handle, None)
        return _err(
            f"failed to open {direction} port {port_name!r}: "
            f"{type(e).__name__}: {e}"
        )

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
    # Output handles have no buffer entries; pop(..., None) covers both.
    _INPUT_BUFFERS.pop(handle, None)
    _INPUT_EVENTS.pop(handle, None)
    _STREAM_DECODERS.pop(handle, None)
    try:
        port.close()
    except Exception as e:
        return _err(f"error closing handle {handle!r}: {type(e).__name__}: {e}")
    return json.dumps({"status": "ok", "handle": handle, "closed": True})


def close_all() -> None:
    """Close every open port; called by local_tools.shutdown(). Safe when
    nothing is open. A failure on one port doesn't stop the others.
    """
    for handle in list(_OPEN_PORTS):
        _, port = _OPEN_PORTS.pop(handle)
        _INPUT_BUFFERS.pop(handle, None)
        _INPUT_EVENTS.pop(handle, None)
        _STREAM_DECODERS.pop(handle, None)
        try:
            port.close()
        except Exception as e:
            print(f"[midi1] close_all: failed to close {handle!r} (ignored): {e}")


# Hour byte 0yyzzzzz (yy = frame rate, zzzzz = hours), shared by every
# SMPTE-style time field in this file: MTC Full Message, MMC Standard Time
# Code, MSC time, and MTC Cueing time.
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


def _encode_standard_speed(speed: float, reverse: bool) -> tuple:
    """MMC Standard Speed (RP-013 p.10): 3 bytes sh sm sl, used by
    VARIABLE PLAY, SEARCH and SHUTTLE. `speed` is the play-speed multiple
    (0 to ~1023.99); `reverse` sets the sign bit.

    sh = 0 g sss ppp (g = sign, sss = shift 0-7, ppp = top 3 bits of a
    17-bit magnitude); sm/sl = the middle and low 7 bits. The magnitude is
    round(speed * 2**(14 - sss)). The smallest sss that fits is used, for
    the most precision.
    """
    if speed < 0:
        raise ValueError(
            f"'speed' must be >= 0 (use 'reverse' for direction), "
            f"got {speed!r}"
        )
    raw = None
    shift = None
    for candidate_shift in range(8):
        candidate_raw = round(speed * (2 ** (14 - candidate_shift)))
        if candidate_raw <= 0x1FFFF:
            raw = candidate_raw
            shift = candidate_shift
            break
    if raw is None or shift is None:
        raise ValueError(f"'speed' out of range (max ~1023.99), got {speed!r}")
    ppp = (raw >> 14) & 0x7
    sm = (raw >> 7) & 0x7F
    sl = raw & 0x7F
    sh = (0x40 if reverse else 0x00) | (shift << 3) | ppp
    return sh, sm, sl


# MMC Information Field names (RP-013 pp.14-16). 01h-0Fh use the 5-byte
# Standard Time Code format; 10h-1Fh are unassigned in the spec. Used by
# the mmc commands move/add/subtract/drop_frame_adjust/read/write/
# masked_write/update and by _decode_mmc_response. RP-013 defines more
# fields than are registered here.
_INFO_FIELD_NAMES = {
    "selected_time_code": 0x01,
    "selected_master_code": 0x02,
    "requested_offset": 0x03,
    "actual_offset": 0x04,
    "lock_deviation": 0x05,
    "generator_time_code": 0x06,
    "midi_time_code_input": 0x07,
    "gp0": 0x08,
    "gp1": 0x09,
    "gp2": 0x0A,
    "gp3": 0x0B,
    "gp4": 0x0C,
    "gp5": 0x0D,
    "gp6": 0x0E,
    "gp7": 0x0F,
    # Standard Track Bitmap fields (RP-013 p.17): <count> <bitmap bytes...>,
    # one bit per track. Reachable through read/update (whole field) and
    # masked_write. 'write' doesn't support them (see _WRITEABLE_INFO_FIELDS).
    "track_record_status": 0x4E,
    "track_record_ready": 0x4F,
    "track_sync_monitor": 0x52,
    "track_input_monitor": 0x53,
    "track_mute": 0x62,
}

# Read/Writeable Standard Time Code fields: valid destinations for write/
# move/add/subtract/drop_frame_adjust. Sources may be any registered name.
# Track Bitmap fields stay out even when writeable, because those commands
# encode the 5-byte time code format only.
_WRITEABLE_INFO_FIELDS = frozenset({
    "selected_time_code", "requested_offset", "generator_time_code",
    "gp0", "gp1", "gp2", "gp3", "gp4", "gp5", "gp6", "gp7",
})

# Fields in Standard Track Bitmap format; _decode_mmc_response branches on
# this.
_TRACK_BITMAP_INFO_FIELDS = frozenset({
    "track_record_status", "track_record_ready", "track_sync_monitor",
    "track_input_monitor", "track_mute",
})

# Valid masked_write targets: the writeable Track Bitmap fields (RP-013
# MASKED WRITE note 1). TRACK_RECORD_STATUS is read-only.
_MASK_WRITEABLE_INFO_FIELDS = frozenset({
    "track_record_ready", "track_sync_monitor", "track_input_monitor",
    "track_mute",
})


def _resolve_info_field_name(
    name: str,
    *,
    require_writeable: bool = False,
    require_mask_writeable: bool = False,
) -> int:
    """Return the name byte for an MMC Information Field name.

    require_writeable: the field must be in _WRITEABLE_INFO_FIELDS (a
    destination for write/move/add/subtract/drop_frame_adjust).
    require_mask_writeable: the field must be in
    _MASK_WRITEABLE_INFO_FIELDS (a masked_write target).
    """
    if name not in _INFO_FIELD_NAMES:
        raise ValueError(
            f"unknown Information Field name {name!r}; must be one of "
            f"{sorted(_INFO_FIELD_NAMES)}"
        )
    if require_writeable and name not in _WRITEABLE_INFO_FIELDS:
        raise ValueError(
            f"Information Field {name!r} is read-only; valid destinations "
            f"are {sorted(_WRITEABLE_INFO_FIELDS)}"
        )
    if require_mask_writeable and name not in _MASK_WRITEABLE_INFO_FIELDS:
        raise ValueError(
            f"Information Field {name!r} is not mask-writeable; valid "
            f"targets are {sorted(_MASK_WRITEABLE_INFO_FIELDS)}"
        )
    return _INFO_FIELD_NAMES[name]


def _encode_nested_mmc_command(
    nested: dict,
    *,
    forbid_assemble: bool = False,
    forbid_define: bool = False,
    forbid_execute_name: "int | None" = None,
) -> tuple:
    """Encode an mmc command dict for use inside PROCEDURE [ASSEMBLE] or
    EVENT [DEFINE] (RP-013 pp.34-37).

    Returns _mmc_command_bytes(nested): the command without the
    `7F <device_id> 06` prefix, which the enclosing message writes once.

    Nesting rules from the spec:
    - forbid_assemble: no nested PROCEDURE [ASSEMBLE] (both callers).
    - forbid_define: no nested EVENT [DEFINE] (EVENT [DEFINE] only).
    - forbid_execute_name: no nested PROCEDURE [EXECUTE] of the procedure
      being assembled (PROCEDURE [ASSEMBLE] only).
    """
    if nested.get("type") != "mmc":
        raise ValueError(
            f"nested commands must be type 'mmc', got "
            f"{nested.get('type')!r}"
        )
    nested_command = nested.get("command")
    if (
        forbid_assemble
        and nested_command == "procedure"
        and nested.get("action") == "assemble"
    ):
        raise ValueError(
            "a nested command cannot be another PROCEDURE [ASSEMBLE] "
            "(nested/recursive ASSEMBLE is not permitted)"
        )
    if (
        forbid_define
        and nested_command == "event"
        and nested.get("action") == "define"
    ):
        raise ValueError(
            "a nested command cannot be another EVENT [DEFINE] "
            "(nested/recursive DEFINE is not permitted)"
        )
    if (
        forbid_execute_name is not None
        and nested_command == "procedure"
        and nested.get("action") == "execute"
        and nested.get("procedure") == forbid_execute_name
    ):
        raise ValueError(
            f"a PROCEDURE [ASSEMBLE] cannot nest a PROCEDURE [EXECUTE] "
            f"naming the SAME procedure ({forbid_execute_name!r}) "
            f"currently being assembled (recursive EXECUTE)"
        )
    return _mmc_command_bytes(nested)


def _encode_standard_time_code(
    hours: int,
    minutes: int,
    seconds: int,
    frames: int,
    frame_rate: str,
    *,
    subframes: int = 0,
    color_frame: bool = False,
    blank: bool = False,
    negative: bool = False,
    use_status_byte: bool = False,
    estimated: bool = False,
    invalid: bool = False,
    video_field_1: bool = False,
    no_time_code: bool = False,
) -> tuple:
    """MMC Standard Time Code (RP-013 section 3), 5 bytes: the format of
    Information Fields 01h-0Fh. With the flags off it is also the MTC, MSC
    and MTC Cueing time layout (see _time_code_bytes).

    hr = 0 tt hhhhh  (_encode_smpte_hour_byte)
    mn = 0 c mmmmmm  (c = color frame)
    sc = 0 k ssssss  (k = blank: never loaded since power-up or MMC RESET)
    fr = 0 g i fffff (g = sign; i selects the 5th byte's format)
    5th byte: subframes 0-99 when i=0, or status flags 0 e v d n 000
    (estimated, invalid, video field 1, no time code) when i=1.
    All flags default to off.
    """
    if not (0 <= minutes <= 59):
        raise ValueError(f"'minutes' must be 0-59, got {minutes!r}")
    if not (0 <= seconds <= 59):
        raise ValueError(f"'seconds' must be 0-59, got {seconds!r}")
    if not (0 <= frames <= 29):
        raise ValueError(f"'frames' must be 0-29, got {frames!r}")
    hr_byte = _encode_smpte_hour_byte(hours, frame_rate)
    mn_byte = (0x40 if color_frame else 0x00) | minutes
    sc_byte = (0x40 if blank else 0x00) | seconds
    fr_byte = (
        (0x40 if negative else 0x00)
        | (0x20 if use_status_byte else 0x00)
        | frames
    )
    if use_status_byte:
        fifth_byte = (
            (0x40 if estimated else 0x00)
            | (0x20 if invalid else 0x00)
            | (0x10 if video_field_1 else 0x00)
            | (0x08 if no_time_code else 0x00)
        )
    else:
        if not (0 <= subframes <= 99):
            raise ValueError(
                f"'subframes' must be 0-99, got {subframes!r}"
            )
        fifth_byte = subframes
    return hr_byte, mn_byte, sc_byte, fr_byte, fifth_byte


def _decode_standard_time_code(
    hr_byte: int, mn_byte: int, sc_byte: int, fr_byte: int, fifth_byte: int
) -> dict:
    """Decode MMC Standard Time Code; inverse of
    _encode_standard_time_code.
    """
    frame_rate_names = {v: k for k, v in _FRAME_RATE_BITS.items()}
    result: dict = {
        "hours": hr_byte & 0x1F,
        "frame_rate": frame_rate_names[(hr_byte >> 5) & 0x3],
        "color_frame": bool(mn_byte & 0x40),
        "minutes": mn_byte & 0x3F,
        "blank": bool(sc_byte & 0x40),
        "seconds": sc_byte & 0x3F,
        "negative": bool(fr_byte & 0x40),
        "frames": fr_byte & 0x1F,
    }
    use_status_byte = bool(fr_byte & 0x20)
    result["use_status_byte"] = use_status_byte
    if use_status_byte:
        result["estimated"] = bool(fifth_byte & 0x40)
        result["invalid"] = bool(fifth_byte & 0x20)
        result["video_field_1"] = bool(fifth_byte & 0x10)
        result["no_time_code"] = bool(fifth_byte & 0x08)
    else:
        result["subframes"] = fifth_byte
    return result


def _time_code_bytes(
    message: dict, subframe_field: "str | None" = None,
    context: "str | None" = None,
) -> tuple:
    """Read the time fields from `message` and encode them as Standard Time
    Code (_encode_standard_time_code, flags off): hr mn sc fr, plus the 5th
    byte when `subframe_field` names the field that holds it. MTC, MSC and
    MTC Cueing times all use this layout.
    """
    extra = (subframe_field,) if subframe_field else ()
    hours, minutes, seconds, frames, frame_rate, *rest = _time_code_fields(
        message, *extra, context=context,
    )
    encoded = _encode_standard_time_code(
        hours, minutes, seconds, frames, frame_rate,
        subframes=rest[0] if rest else 0,
    )
    return encoded if subframe_field else encoded[:4]


def _additional_info_bytes(message: dict, command: str) -> list:
    """MTC Cueing additional info, before nibblizing: the bytes of
    'additional_info_message' (built with _build_message) or the raw
    'additional_info_bytes'. Exactly one of the two."""
    info_message = message.get("additional_info_message")
    raw_bytes = message.get("additional_info_bytes")
    if info_message is not None and raw_bytes is not None:
        raise ValueError(
            "specify only ONE of 'additional_info_message' or "
            "'additional_info_bytes', not both"
        )
    if info_message is None and raw_bytes is None:
        raise KeyError(
            f"'additional_info_message' (or 'additional_info_bytes') — "
            f"required for {command!r}"
        )
    if info_message is not None:
        return _build_message(info_message).bytes()
    return list(raw_bytes)


def _cueing_event(message: dict, command: str) -> tuple:
    """sl sm <additional info> for both MTC Cueing types: the 14-bit event
    number, then nibblized ASCII for event_name or a nibblized MIDI message
    for the *_with_info commands."""
    sl, sm = _split14("event_number", _required(message, "event_number"))
    if command == "event_name":
        name = _required(message, "event_name", command)
        info = _nibblize(_ascii("event_name", name))
    elif command.endswith("_with_info"):
        info = _nibblize(_additional_info_bytes(message, command))
    else:
        info = ()
    return (sl, sm, *info)


def _mmc_response_fields(data: list) -> dict:
    """Decode an MMC Response (F0 7F <device_id> 07 <name> ... F7; 07 is
    mcr, device to controller), given the whole SysEx including F0 and F7.
    Raises ValueError for a malformed one.

    Decodes the fields in _INFO_FIELD_NAMES (Standard Time Code or Track
    Bitmap format) and RESPONSE ERROR (42h, a count then the failed field
    names). Any other name byte, including COMMAND ERROR and segmented
    responses, comes back as type "unknown" with the raw byte. A response
    carrying several fields isn't supported.
    """
    if len(data) < 5 or data[0] != 0xF0 or data[-1] != 0xF7:
        raise ValueError(
            "'data' must be a complete sysex, including the leading "
            "0xF0 and trailing 0xF7"
        )
    if data[1] != 0x7F or data[3] != 0x07:
        raise ValueError(
            "'data' is not an MMC Response sysex — expected "
            "F0 7F <device_id> 07 ... F7"
        )
    device_id = data[2]
    name_byte = data[4]
    payload = data[5:-1]
    if name_byte == 0x42:
        field_names_by_byte = {v: k for k, v in _INFO_FIELD_NAMES.items()}
        unsupported = [
            field_names_by_byte.get(b, f"0x{b:02X}")
            for b in payload[1:]  # payload[0] is the count byte
        ]
        return {
            "device_id": device_id,
            "type": "response_error",
            "unsupported_fields": unsupported,
        }
    field_name = {v: k for k, v in _INFO_FIELD_NAMES.items()}.get(name_byte)
    if field_name is None:
        return {
            "device_id": device_id,
            "type": "unknown",
            "raw_name_byte": name_byte,
            "note": (
                f"not decodable: only the {len(_INFO_FIELD_NAMES)} "
                "registered Information Fields plus RESPONSE ERROR are "
                "supported"
            ),
        }
    if field_name in _TRACK_BITMAP_INFO_FIELDS:
        # <count> <bitmap bytes...>. A device may leave out trailing zero
        # bytes; missing tracks are inactive. Returns the raw bytes (for
        # masked_write) and the active track numbers (bit 0 of byte 0 is
        # track 1).
        if not payload:
            raise ValueError(
                f"expected at least a <count> byte for field "
                f"{field_name!r}, got an empty payload"
            )
        bitmap_count = payload[0]
        bitmap_bytes = payload[1:]
        if len(bitmap_bytes) != bitmap_count:
            raise ValueError(
                f"'{field_name}' declared byte count {bitmap_count} but "
                f"{len(bitmap_bytes)} bitmap byte(s) actually followed"
            )
        active_tracks = [
            byte_index * 7 + bit_index + 1
            for byte_index, byte_value in enumerate(bitmap_bytes)
            for bit_index in range(7)
            if byte_value & (1 << bit_index)
        ]
        return {
            "device_id": device_id,
            "type": "field_value",
            "name": field_name,
            "byte_count": bitmap_count,
            "bitmap_bytes": list(bitmap_bytes),
            "active_tracks": active_tracks,
        }
    if len(payload) != 5:
        raise ValueError(
            f"expected exactly 5 data bytes for field {field_name!r}, "
            f"got {len(payload)}"
        )
    return {
        "device_id": device_id,
        "type": "field_value",
        "name": field_name,
        **_decode_standard_time_code(*payload),
    }


def _decode_mmc_response(tool_input: dict) -> str:
    """The decode_mmc_response action: _mmc_response_fields on 'data'."""
    data = tool_input.get("data")
    if not data:
        return _err(
            "'data' is required: a non-empty list of ints, the full "
            "sysex including the leading 0xF0 and trailing 0xF7"
        )
    try:
        fields = _mmc_response_fields(list(data))
    except ValueError as e:
        return _err(str(e))
    return json.dumps({"status": "ok", **fields})


def _encode_msc_ascii_field(name: str, value: str) -> tuple:
    """Encode an MSC Q_number/Q_list/Q_path: ASCII digits with '.' as
    the decimal point (MSC 1.0 example: cue "235.6" -> 32 33 35 2E 36).
    Only the character set is checked; the spec's rules for stray dots
    apply to receivers.
    """
    if not value or any(c not in "0123456789." for c in value):
        raise ValueError(
            f"{name!r} must be a non-empty string of digits and '.' only, "
            f"got {value!r}"
        )
    return tuple(ord(c) for c in value)


def _encode_msc_cue_data(q_number, q_list, q_path) -> tuple:
    """Cue data for MSC GO, STOP, RESUME, TIMED_GO and GO_OFF: the
    fields present, separated by 00. Q_list needs Q_number; Q_path needs
    Q_list. Absent trailing fields are left off.
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


def _encode_tuning_frequency(entry: dict) -> tuple:
    """Encode one MIDI Tuning frequency (Frequency Data Format), used by
    Bulk Tuning Dump and Single Note Tuning Change: the equal-tempered
    semitone at or below the frequency (0-127), then a 14-bit fraction of
    100 cents above it, MSB byte first.

    Takes {"semitone": 0-127, "cents": 0 <= cents < 100}, or
    {"no_change": true} for the 7F 7F 7F "leave this key unchanged" value.
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
    frac14 = min(frac14, 16383)  # cents just under 100 can round to 16384
    return (semitone, (frac14 >> 7) & 0x7F, frac14 & 0x7F)


def _encode_time_signature_pair(numerator: int, denominator: int) -> tuple:
    """Encode one Notation Time Signature pair as nn dd. `denominator` is
    the note value (2, 4, 8, ...), as in mido's time_signature meta event;
    the wire byte is its power of 2.
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


def _nibblize(raw_bytes) -> tuple:
    """Split 8-bit bytes into nibbles, low nibble first, for MTC Cueing
    additional info (MTC spec, "Additional Information"). Example:
    91 46 7F -> 01 09 06 04 0F 07.
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


def _encode_file_dump_data(stored_bytes) -> tuple:
    """Encode 1-112 file bytes for a File Dump Data Packet. Each group of
    up to 7 bytes becomes a sign byte (bit 7 of each byte, first byte in
    bit 6, left-justified for a short group) followed by each byte's low 7
    bits.
    """
    if not (1 <= len(stored_bytes) <= 112):
        raise ValueError(
            f"a single Data Packet holds 1-112 stored bytes (encodes to "
            f"a max 128-byte payload), got {len(stored_bytes)}"
        )
    out: list = []
    for group_start in range(0, len(stored_bytes), 7):
        group = stored_bytes[group_start:group_start + 7]
        sign_byte = 0
        for i, b in enumerate(group):
            if not (0 <= b <= 255):
                raise ValueError(
                    f"stored bytes must each be 0-255, got {b!r}"
                )
            sign_byte |= ((b >> 7) & 1) << (6 - i)
        out.append(sign_byte)
        out.extend(b & 0x7F for b in group)
    return tuple(out)


# --- Shared message-building helpers ---------------------------------------
# Missing required field -> KeyError; bad value -> ValueError. _send and
# _write_midi_file report both.


def _required(message: dict, field: str, context: "str | None" = None):
    value = message.get(field)
    if value is None:
        where = f" (required for {context!r})" if context else ""
        raise KeyError(f"'{field}'{where}")
    return value


def _check_range(field: str, value, low: int, high: int):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"'{field}' must be an integer, got {value!r}")
    if not (low <= value <= high):
        raise ValueError(f"'{field}' must be {low}-{high}, got {value!r}")
    return value


def _choice(message: dict, field: str, table, context: str):
    """Read a required field whose value must be a key of `table`."""
    value = _required(message, field)
    if value not in table:
        raise ValueError(
            f"'{field}' must be one of {sorted(table)} for {context!r}, "
            f"got {value!r}"
        )
    return value


def _device_id(message: dict, default: "int | None" = 0x7F) -> int:
    """SysEx device ID, 0-127. default=None makes it required."""
    device_id = message.get("device_id", default)
    if device_id is None:
        raise KeyError("'device_id'")
    return _check_range("device_id", device_id, 0, 127)


def _sysex(*data: int, time: int = 0) -> "mido.Message":
    """A SysEx message; `data` excludes F0/F7."""
    return mido.Message("sysex", data=data, time=time)


def _split14(field: str, value: int) -> tuple:
    """A 0-16383 value as (LSB, MSB) 7-bit bytes."""
    _check_range(field, value, 0, 0x3FFF)
    return value & 0x7F, (value >> 7) & 0x7F


def _xor_checksum(data) -> int:
    checksum = 0
    for b in data:
        checksum ^= b
    return checksum


def _ascii(field: str, text: str, *, printable: bool = False) -> tuple:
    """Encode text as 7-bit ASCII bytes; printable=True allows 20h-7Eh only."""
    low, high = (0x20, 0x7E) if printable else (0x00, 0x7F)
    for c in text:
        if not (low <= ord(c) <= high):
            kind = "printable ASCII (0x20-0x7E)" if printable else "7-bit ASCII"
            raise ValueError(f"'{field}' must be {kind}, got {c!r}")
    return tuple(ord(c) for c in text)


def _time_code_fields(message: dict, *extra: str, context: "str | None" = None) -> tuple:
    """The required hours, minutes, seconds, frames, frame_rate fields, plus
    any `extra` ones, in that order."""
    names = ("hours", "minutes", "seconds", "frames", "frame_rate", *extra)
    return tuple(_required(message, name, context) for name in names)


def _required_list(message: dict, field: str, context: str) -> list:
    """A required field that must be a non-empty list."""
    value = message.get(field)
    if not value:
        raise KeyError(f"'{field}' (required non-empty list for {context!r})")
    return list(value)


# --- MIDI Machine Control (RP-013) -------------------------------------------
# Every command is F0 7F <device_id> 06 <opcode> [<count> <data...>] F7.
# _mmc_command_bytes builds <opcode> [<count> <data...>]. Commands that carry
# data have a builder in _MMC_DATA_BUILDERS that returns <data>; the 13
# transport commands in _MMC_NO_DATA_COMMANDS, WAIT and RESUME have none.

_MMC_GENERATOR_ACTIONS = {"stop": 0x00, "run": 0x01, "copy_jam": 0x02}
_MMC_MTC_COMMAND_ACTIONS = {"off": 0x00, "follow": 0x02}
_MMC_GROUP_ACTIONS = {"assign": 0x00, "dis_assign": 0x01}
_MMC_PROCEDURE_ACTIONS = {
    "assemble": 0x00, "delete": 0x01, "set": 0x02, "execute": 0x03,
}
_MMC_EVENT_ACTIONS = {"define": 0x00, "delete": 0x01, "set": 0x02, "test": 0x03}
_MMC_EVENT_DIRECTIONS = {"forward": 0b00, "reverse": 0b01, "both": 0b10}
_MMC_EVENT_TRIGGER_SOURCES = (
    "selected_time_code", "selected_master_code", "generator_time_code",
    "midi_time_code_input",
)
_MMC_GP_REGISTERS = ("gp0", "gp1", "gp2", "gp3", "gp4", "gp5", "gp6", "gp7")
_MMC_UPDATE_ACTIONS = {"begin": 0x00, "end": 0x01}

# Always sent to the all-call device ID 7F, whatever 'device_id' says
# (RP-013 pp.30-31 and p.42).
_MMC_ALL_CALL = frozenset({"assign_system_master", "wait", "resume"})

# The per-bit flags of Standard Time Code (_encode_standard_time_code).
_TIME_CODE_FLAGS = (
    "color_frame", "blank", "negative", "use_status_byte", "estimated",
    "invalid", "video_field_1", "no_time_code",
)


def _each(field: str, entries, encode) -> tuple:
    """encode(entry) for every entry of a list field, concatenated. An error
    is re-raised with the entry's index in front."""
    out: list = []
    for index, entry in enumerate(entries):
        try:
            out += encode(entry)
        except KeyError as e:
            raise KeyError(f"'{field}'[{index}]: {e}") from e
        except ValueError as e:
            raise ValueError(f"'{field}'[{index}]: {e}") from e
    return tuple(out)


def _mmc_locate(message: dict, command: str) -> tuple:
    # LOCATE [TARGET] (44h, RP-013 p.28): 01 then Standard Time Code with
    # subframes. LOCATE [I/F] (sub-command 00, locate to a GP register)
    # isn't implemented.
    return (0x01, *_time_code_bytes(message, "subframes"))


def _mmc_step(message: dict, command: str) -> tuple:
    # STEP (48h, RP-013 p.30): one byte 0 g ssssss (g = reverse,
    # s = quantity 0-63).
    quantity = _check_range(
        "quantity", _required(message, "quantity", command), 0, 63,
    )
    return ((0x40 if message.get("reverse", False) else 0x00) | quantity,)


def _mmc_assign_system_master(message: dict, command: str) -> tuple:
    # ASSIGN SYSTEM MASTER (49h, RP-013 pp.30-31). target_device_id 7F
    # dis-assigns. Sent to all-call (_MMC_ALL_CALL).
    target = _required(message, "target_device_id", command)
    return (_check_range("target_device_id", target, 0, 127),)


def _mmc_generator_command(message: dict, command: str) -> tuple:
    # GENERATOR COMMAND (4Ah, RP-013 p.31): stop, run, or copy/jam the time
    # code generator. The GENERATOR SET UP Information Field isn't
    # registered.
    action = _choice(message, "action", _MMC_GENERATOR_ACTIONS, command)
    return (_MMC_GENERATOR_ACTIONS[action],)


def _mmc_midi_time_code_command(message: dict, command: str) -> tuple:
    # MIDI TIME CODE COMMAND (4Bh, RP-013 p.31). The spec defines only 00 and
    # 02. The MIDI TIME CODE SET UP Information Field isn't registered.
    action = _choice(message, "action", _MMC_MTC_COMMAND_ACTIONS, command)
    return (_MMC_MTC_COMMAND_ACTIONS[action],)


def _mmc_speed(message: dict, command: str) -> tuple:
    # VARIABLE PLAY 45h, SEARCH 46h, SHUTTLE 47h (RP-013 pp.29-30), DEFERRED
    # VARIABLE PLAY 54h (p.37), RECORD STROBE VARIABLE 55h (p.41): sh sm sl
    # (_encode_standard_speed).
    speed = _required(message, "speed", command)
    return _encode_standard_speed(speed, message.get("reverse", False))


def _mmc_drop_frame_adjust(message: dict, command: str) -> tuple:
    # DROP FRAME ADJUST (4Fh, RP-013 p.33): converts a writeable field to
    # drop-frame in place (the device ignores it unless the field is 30fps
    # non-drop).
    name = _required(message, "name", command)
    return (_resolve_info_field_name(name, require_writeable=True),)


def _mmc_move(message: dict, command: str) -> tuple:
    # MOVE (4Ch, RP-013 p.32): destination = source. The destination must be
    # writeable; the source can be any field.
    destination = _required(message, "destination", command)
    source = _required(message, "source", command)
    return (
        _resolve_info_field_name(destination, require_writeable=True),
        _resolve_info_field_name(source),
    )


def _mmc_math(message: dict, command: str) -> tuple:
    # ADD (4Dh) / SUBTRACT (4Eh), RP-013 pp.32-33:
    # destination = source_1 +/- source_2. Same field rules as MOVE. The
    # destination may also be a source.
    destination, source_1, source_2 = (
        _required(message, field, command)
        for field in ("destination", "source_1", "source_2")
    )
    return (
        _resolve_info_field_name(destination, require_writeable=True),
        _resolve_info_field_name(source_1),
        _resolve_info_field_name(source_2),
    )


def _mmc_group(message: dict, command: str) -> tuple:
    # GROUP (52h, RP-013 p.39):
    #   assign (00): device_ids join group; group can't be 7F.
    #   dis_assign (01): device_ids leave group; group 7F means all groups.
    #     7F in device_ids means all devices.
    # Spec example (all devices, all groups): F0 7F 7F 06 52 03 01 7F 7F F7.
    action = _choice(message, "action", _MMC_GROUP_ACTIONS, command)
    group = _check_range("group", _required(message, "group", command), 0, 127)
    if action == "assign" and group == 0x7F:
        raise ValueError(
            "'group' must not be 0x7F (127) for action='assign' -- 0x7F is "
            "the all-call address, not a group number"
        )
    device_ids = _required_list(message, "device_ids", command)
    for device_id in device_ids:
        _check_range("device_ids entry", device_id, 0, 127)
    return (_MMC_GROUP_ACTIONS[action], group, *device_ids)


def _mmc_slot(message: dict, field: str, command: str, all_allowed: bool) -> int:
    """A procedure or event number. 7F means "all" where `all_allowed`
    (delete, set) and is reserved otherwise."""
    value = _required(message, field, command)
    return _check_range(field, value, 0, 0x7F if all_allowed else 0x7E)


def _mmc_procedure(message: dict, command: str) -> tuple:
    # PROCEDURE (50h, RP-013 pp.34-35): stored command lists.
    #   assemble (00): procedure + nested 'commands'.
    #   delete (01), set (02): procedure 7F means all procedures.
    #   execute (03).
    action = _choice(message, "action", _MMC_PROCEDURE_ACTIONS, command)
    procedure = _mmc_slot(
        message, "procedure", command, all_allowed=action in ("delete", "set"),
    )
    if action != "assemble":
        return (_MMC_PROCEDURE_ACTIONS[action], procedure)
    nested: list = []
    for entry in _required_list(message, "commands", "assemble"):
        nested += _encode_nested_mmc_command(
            entry, forbid_assemble=True, forbid_execute_name=procedure,
        )
    return (0x00, procedure, *nested)


def _mmc_event(message: dict, command: str) -> tuple:
    # EVENT (51h, RP-013 pp.35-38): commands triggered at a time.
    # define (00), delete (01), set (02), test (03); event 7F means all events
    # for delete/set and is reserved for define/test.
    # define payload: event, flags, trigger_source, name, trigger_command.
    #   flags = 0 k 0 a 00 dd: k = non_delete (stays armed after firing),
    #     a = all_speeds, dd = direction (00 forward, 01 reverse, 10 both).
    #   trigger_source: a time code field.
    #   name: the GP0-GP7 register holding the trigger time.
    #   trigger_command: one nested mmc command; it can't be an EVENT
    #     [DEFINE] or PROCEDURE [ASSEMBLE].
    action = _choice(message, "action", _MMC_EVENT_ACTIONS, command)
    event = _mmc_slot(
        message, "event", command, all_allowed=action in ("delete", "set"),
    )
    if action != "define":
        return (_MMC_EVENT_ACTIONS[action], event)
    direction = _choice(message, "direction", _MMC_EVENT_DIRECTIONS, "define")
    flags = (
        (0x40 if message.get("non_delete", False) else 0x00)
        | (0x10 if message.get("all_speeds", False) else 0x00)
        | _MMC_EVENT_DIRECTIONS[direction]
    )
    trigger_source = _choice(
        message, "trigger_source", _MMC_EVENT_TRIGGER_SOURCES, "define",
    )
    name = _choice(message, "name", _MMC_GP_REGISTERS, "define")
    # Not 'command': that key already holds "event".
    trigger_command = _required(message, "trigger_command", "define")
    nested = _encode_nested_mmc_command(
        trigger_command, forbid_assemble=True, forbid_define=True,
    )
    return (
        0x00, event, flags, _INFO_FIELD_NAMES[trigger_source],
        _INFO_FIELD_NAMES[name], *nested,
    )


def _mmc_read(message: dict, command: str) -> tuple:
    # READ (42h, RP-013 p.26): ask for the current value of any registered
    # fields, read-only ones included. The device answers with an MMC
    # Response (decode with decode_mmc_response), or RESPONSE ERROR for
    # fields it doesn't support.
    names = _required_list(message, "names", command)
    return tuple(_resolve_info_field_name(name) for name in names)


def _mmc_write(message: dict, command: str) -> tuple:
    # WRITE (40h, RP-013 p.25): <name> <data> for each entry in 'fields'.
    # Only writeable Standard Time Code fields are accepted, so <data> is
    # always 5 bytes with no length prefix. Count-prefixed fields (such as
    # the Track Bitmaps) aren't supported.
    data: list = []
    for field in _required_list(message, "fields", command):
        name = _required(field, "name", "each 'fields' entry")
        data.append(_resolve_info_field_name(name, require_writeable=True))
        data += _encode_standard_time_code(
            *_time_code_fields(field, context=name),
            subframes=field.get("subframes", 0),
            **{flag: field.get(flag, False) for flag in _TIME_CODE_FLAGS},
        )
    return tuple(data)


def _mmc_masked_write(message: dict, command: str) -> tuple:
    # MASKED WRITE (41h, RP-013 pp.25-26): change selected bits of a Track
    # Bitmap field. Each entry in 'fields' becomes <name> <byte#> <mask>
    # <data>. byte# 0 is the first bitmap byte after the field's own count
    # byte. mask and data are 7-bit, so 7F means all ones.
    data: list = []
    for field in _required_list(message, "fields", command):
        name = _required(field, "name", "each 'fields' entry")
        data.append(_resolve_info_field_name(name, require_mask_writeable=True))
        for key in ("byte_number", "mask", "data"):
            data.append(_check_range(key, _required(field, key, name), 0, 127))
    return tuple(data)


def _mmc_update(message: dict, command: str) -> tuple:
    # UPDATE (43h, RP-013 pp.26-27):
    #   begin (00): send the named fields now, then again whenever they
    #     change (limited by the UPDATE RATE field).
    #   end (01): stop updating the named fields. The name "all" (sent as 7F)
    #     stops every update; it is valid only here.
    action = _choice(message, "action", _MMC_UPDATE_ACTIONS, command)
    names = _required_list(message, "names", command)
    return (_MMC_UPDATE_ACTIONS[action], *(
        0x7F if action == "end" and name == "all"
        else _resolve_info_field_name(name)
        for name in names
    ))


_MMC_DATA_BUILDERS = {
    "locate": _mmc_locate,
    "step": _mmc_step,
    "assign_system_master": _mmc_assign_system_master,
    "generator_command": _mmc_generator_command,
    "midi_time_code_command": _mmc_midi_time_code_command,
    "variable_play": _mmc_speed,
    "search": _mmc_speed,
    "shuttle": _mmc_speed,
    "deferred_variable_play": _mmc_speed,
    "record_strobe_variable": _mmc_speed,
    "drop_frame_adjust": _mmc_drop_frame_adjust,
    "move": _mmc_move,
    "add": _mmc_math,
    "subtract": _mmc_math,
    "group": _mmc_group,
    "procedure": _mmc_procedure,
    "event": _mmc_event,
    "read": _mmc_read,
    "write": _mmc_write,
    "masked_write": _mmc_masked_write,
    "update": _mmc_update,
}


def _mmc_command_bytes(message: dict) -> tuple:
    """<opcode> [<count> <data...>] for one mmc command dict; the count is
    the length of the data."""
    command = _choice(message, "command", _COMMANDS["mmc"], "mmc")
    opcode = _COMMANDS["mmc"][command]
    builder = _MMC_DATA_BUILDERS.get(command)
    if builder is None:
        return (opcode,)
    data = builder(message, command)
    return (opcode, len(data), *data)


# --- MIDI Show Control (RP-002/014) ------------------------------------------
# F0 7F <device_id> 02 <command_format> <command> <data> F7. Commands that
# carry data have a builder in _MSC_DATA_BUILDERS returning <data>; the ones
# in _MSC_NO_DATA_COMMANDS have none.

_MSC_NO_DATA_COMMANDS = frozenset({"all_off", "restore", "reset"})

# The time fields of MSC TIMED_GO and SET.
_MSC_TIME_FIELDS = (
    "hours", "minutes", "seconds", "frames", "fractional_frames", "frame_rate",
)


def _msc_command_format(message: dict) -> int:
    """The command_format byte: a name from _MSC_FORMATS, or
    'command_format_raw' (0-127) for a narrower sub-category. Exactly one."""
    name = message.get("command_format")
    raw = message.get("command_format_raw")
    if name is not None and raw is not None:
        raise ValueError(
            "specify only ONE of 'command_format' or 'command_format_raw', "
            "not both"
        )
    if raw is not None:
        return _check_range("command_format_raw", raw, 0, 127)
    if name is None:
        raise KeyError("'command_format' (or 'command_format_raw')")
    return _MSC_FORMATS[_choice(message, "command_format", _MSC_FORMATS, "msc")]


def _msc_cue(message: dict, command: str) -> tuple:
    # GO, STOP, RESUME, GO_OFF: optional cue data. LOAD: q_number required.
    if command == "load":
        q_number = _required(message, "q_number", command)
    else:
        q_number = message.get("q_number")
    return _encode_msc_cue_data(
        q_number, message.get("q_list"), message.get("q_path"),
    )


def _msc_timed_go(message: dict, command: str) -> tuple:
    # TIMED_GO: MSC time (hr mn sc fr ff), then the same cue data as GO.
    return (
        *_time_code_bytes(message, "fractional_frames", command),
        *_msc_cue(message, command),
    )


def _msc_set(message: dict, command: str) -> tuple:
    # SET: control number and value, 14 bits each, LSB first, then the MSC
    # time when all six time fields are given (none or all).
    control_number = _required(message, "control_number", command)
    control_value = _required(message, "control_value", command)
    data = (
        *_split14("control_number", control_number),
        *_split14("control_value", control_value),
    )
    given = [field for field in _MSC_TIME_FIELDS if message.get(field) is not None]
    if not given:
        return data
    if len(given) != len(_MSC_TIME_FIELDS):
        raise ValueError(
            f"SET's time fields ({'/'.join(_MSC_TIME_FIELDS)}) must be given "
            f"all together or not at all; got {given}"
        )
    return (*data, *_time_code_bytes(message, "fractional_frames", command))


def _msc_fire(message: dict, command: str) -> tuple:
    # FIRE: one macro number.
    macro = _required(message, "macro_number", command)
    return (_check_range("macro_number", macro, 0, 127),)


_MSC_DATA_BUILDERS = {
    "go": _msc_cue,
    "stop": _msc_cue,
    "resume": _msc_cue,
    "go_off": _msc_cue,
    "load": _msc_cue,
    "timed_go": _msc_timed_go,
    "set": _msc_set,
    "fire": _msc_fire,
}


# --- Channel and system messages ---------------------------------------------
# The types mido builds directly: type -> {field: default}, where _REQUIRED
# marks a field with no default. Types in _CHANNEL_TYPES also take 'channel'
# (default 0). mido checks the values' ranges.

_REQUIRED = object()

_MIDO_FIELDS = {
    "note_on": {"note": _REQUIRED, "velocity": 64},
    "note_off": {"note": _REQUIRED, "velocity": 0},
    "control_change": {"control": _REQUIRED, "value": 0},
    "program_change": {"program": _REQUIRED},
    "pitchwheel": {"pitch": 0},
    "aftertouch": {"value": 0},  # Channel Pressure: one value per channel
    "polytouch": {"note": _REQUIRED, "value": 0},  # Polyphonic Key Pressure
    "quarter_frame": {"frame_type": _REQUIRED, "frame_value": _REQUIRED},
    "songpos": {"pos": 0},  # 0-16383
    "song_select": {"song": _REQUIRED},
    "tune_request": {},
    # System Real-Time: status byte only.
    "clock": {}, "start": {}, "stop": {}, "continue": {},
    "active_sensing": {}, "reset": {},
}
_CHANNEL_TYPES = frozenset({
    "note_on", "note_off", "control_change", "program_change", "pitchwheel",
    "aftertouch", "polytouch",
})


def _build_message(message: dict) -> "mido.Message":
    """Build one mido.Message (channel, system or SysEx) from a typed dict.

    Used by send (through _build_message_sequence) and write_midi_file.
    Raises KeyError for a missing required field and ValueError for a bad
    value; callers catch both. 'time' is the delta in ticks, used only in
    files.
    """
    msg_type = message.get("type")
    channel = message.get("channel", 0)
    time = message.get("time", 0)
    if msg_type in _MIDO_FIELDS:
        fields = {
            name: message[name] if default is _REQUIRED else message.get(name, default)
            for name, default in _MIDO_FIELDS[msg_type].items()
        }
        if msg_type in _CHANNEL_TYPES:
            fields["channel"] = channel
        return mido.Message(msg_type, time=time, **fields)
    if msg_type == "sysex":
        # Payload without F0/F7 (mido adds them). mido raises ValueError for
        # a byte outside 0-127.
        raw_data = message.get("data")
        if raw_data is None:
            raise KeyError("'data'")
        return mido.Message("sysex", data=tuple(raw_data), time=time)
    if msg_type == "mtc_full":
        # MTC Full Message (RP-004/008): jumps to a position in one
        # message instead of eight Quarter Frames.
        #   F0 7F <device_id> 01 01 hr mn sc fr F7
        # hr: _encode_smpte_hour_byte. frame_rate has no default because it
        # changes what the position means. device_id defaults to 7F (all
        # devices), the spec's default.
        device_id = _device_id(message)
        return _sysex(
            0x7F, device_id, 0x01, 0x01, *_time_code_bytes(message), time=time,
        )
    if msg_type == "mtc_nak":
        # MTC sync-dropped NAK (RP-004/008 p.4): the receiver treats it as
        # "tape stopped". Same bytes as file_dump's 'nak', without its
        # required packet_number:
        #   F0 7E <device_id> 7E <packet_number, default 0> F7
        # device_id defaults to 7F (all devices).
        device_id = _device_id(message)
        packet_number = _check_range(
            "packet_number", message.get("packet_number", 0), 0, 127,
        )
        return _sysex(0x7E, device_id, 0x7E, packet_number, time=time)
    if msg_type == "mmc":
        # MIDI Machine Control (RP-013); see _mmc_command_bytes. Device
        # replies are decoded by the decode_mmc_response action.
        command = _choice(message, "command", _COMMANDS["mmc"], "mmc")
        device_id = _device_id(message)
        if command in _MMC_ALL_CALL:
            device_id = 0x7F
        return _sysex(
            0x7F, device_id, 0x06, *_mmc_command_bytes(message), time=time,
        )
    if msg_type == "msc":
        # MIDI Show Control (RP-002/014); see _MSC_DATA_BUILDERS. Only the 11
        # General Category commands, which apply to every command_format.
        # The 15 Sound Commands aren't implemented; send them with 'sysex'.
        command_format = _msc_command_format(message)
        command = _choice(message, "command", _COMMANDS["msc"], "msc")
        device_id = _device_id(message)
        builder = _MSC_DATA_BUILDERS.get(command)
        data = builder(message, command) if builder else ()
        return _sysex(
            0x7F, device_id, 0x02, command_format, _COMMANDS["msc"][command],
            *data, time=time,
        )
    if msg_type == "gm_system":
        # General MIDI System On/Off (Universal Non-Real Time, MIDI 1.0
        # Detailed Spec Table VIIa):
        #   F0 7E <device_id> 09 01 F7   on
        #   F0 7E <device_id> 09 02 F7   off
        # device_id defaults to 7F (all devices), as the spec suggests.
        command = _choice(
            message, "command", _COMMANDS["gm_system"], "gm_system",
        )
        device_id = _device_id(message)
        return _sysex(
            0x7E, device_id, 0x09, _COMMANDS["gm_system"][command], time=time,
        )
    if msg_type == "device_inquiry":
        # Device Inquiry (Universal Non-Real Time, MIDI 1.0 Detailed Spec):
        #   request: F0 7E <device_id> 06 01 F7
        #   reply:   F0 7E <device_id> 06 02 mm ff ff dd dd ss ss ss ss F7
        # mm: manufacturer ID, one byte 1-127 or three bytes 00 xx yy.
        # ff ff / dd dd: device family / member code, 14 bits, LSB first.
        # ss x4: software revision, device-specific format.
        command = _choice(
            message, "command", _COMMANDS["device_inquiry"], "device_inquiry",
        )
        device_id = _device_id(message)
        if command == "request":
            return _sysex(0x7E, device_id, 0x06, 0x01, time=time)

        manufacturer_id = _required(message, "manufacturer_id", "reply")
        family = _required(message, "device_family_code", "reply")
        member = _required(message, "device_family_member_code", "reply")
        software_revision = _required(message, "software_revision", "reply")

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
                _check_range("manufacturer_id byte", b, 0, 127)

        revision_bytes = tuple(software_revision)
        if len(revision_bytes) != 4:
            raise ValueError(
                f"'software_revision' must be exactly 4 bytes, got "
                f"{software_revision!r}"
            )
        for b in revision_bytes:
            _check_range("software_revision byte", b, 0, 127)

        return _sysex(
            0x7E, device_id, 0x06, 0x02, *mfr_bytes,
            *_split14("device_family_code", family),
            *_split14("device_family_member_code", member),
            *revision_bytes,
            time=time,
        )
    if msg_type == "device_control":
        # Device Control (Universal Real Time, MIDI 1.0 Detailed Spec),
        # whole-device rather than per channel:
        #   master_volume:  F0 7F <device_id> 04 01 vv vv F7 (0 = off)
        #   master_balance: F0 7F <device_id> 04 02 bb bb F7 (0 = left,
        #                   16383 = right)
        # value is 14 bits, LSB first. Master Fine/Coarse Tuning (04 03,
        # 04 04; CA-025) aren't implemented.
        command = _choice(
            message, "command", _COMMANDS["device_control"], "device_control",
        )
        value = _required(message, "value")
        device_id = _device_id(message)
        return _sysex(
            0x7F, device_id, 0x04, _COMMANDS["device_control"][command],
            *_split14("value", value),
            time=time,
        )
    if msg_type == "channel_mode":
        # Channel Mode messages: Control Change 120-127 (MIDI 1.0
        # Detailed Spec, Table IV), by name instead of controller number.
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
        # 124-127 also act as All Notes Off on the receiver (Detailed
        # Spec, "Mode Messages as All Notes Off Messages").
        command = _choice(
            message, "command", _COMMANDS["channel_mode"], "channel_mode",
        )
        if command == "local_control":
            mode_value = 127 if _required(message, "on", command) else 0
        elif command == "mono_on":
            mode_value = _check_range(
                "channel_count", _required(message, "channel_count", command),
                0, 16,
            )
        else:
            mode_value = 0
        return mido.Message(
            "control_change", channel=channel,
            control=_COMMANDS["channel_mode"][command], value=mode_value,
            time=time,
        )
    if msg_type == "midi_tuning":
        # MIDI Tuning (MIDI Tuning Updated Specification):
        #   bulk_dump_request: F0 7E <device_id> 08 00 tt F7
        #   bulk_dump_reply:   F0 7E <device_id> 08 01 tt <16-char name>
        #                      [xx yy zz] x128 (note 0 first) chksum F7
        #   note_change:       F0 7F <device_id> 08 02 tt ll
        #                      [kk xx yy zz] x ll F7
        # Checksum: XOR of every byte between F0 and the checksum. The
        # updated spec says the original bulk dump's checksum text is
        # ambiguous, that receivers may ignore it, and that every other
        # dump uses this rule.
        # The bank and scale/octave messages in the updated spec (08 03-09)
        # aren't implemented.
        command = _choice(
            message, "command", _COMMANDS["midi_tuning"], "midi_tuning",
        )
        code = _COMMANDS["midi_tuning"][command]
        device_id = _device_id(message)
        program = _check_range(
            "tuning_program", _required(message, "tuning_program"), 0, 127,
        )

        if command == "bulk_dump_request":
            return _sysex(0x7E, device_id, 0x08, code, program, time=time)

        if command == "bulk_dump_reply":
            name = message.get("tuning_name", "")
            if len(name) > 16:
                raise ValueError(
                    f"'tuning_name' must be at most 16 characters, got "
                    f"{len(name)} ({name!r})"
                )
            notes = _required(message, "notes", command)
            if len(notes) != 128:
                raise ValueError(
                    f"'notes' must have exactly 128 entries (one per MIDI key "
                    f"number), got {len(notes)}"
                )
            data = (
                0x7E, device_id, 0x08, code, program,
                *_ascii("tuning_name", name.ljust(16)),
                *_each("notes", notes, _encode_tuning_frequency),
            )
            return _sysex(*data, _xor_checksum(data), time=time)

        # note_change: [kk xx yy zz] per entry.
        changes = _required_list(message, "changes", command)
        if len(changes) > 127:
            raise ValueError(
                f"'changes' must have 1-127 entries, got {len(changes)}"
            )
        return _sysex(
            0x7F, device_id, 0x08, code, program, len(changes),
            *_each("changes", changes, lambda entry: (
                _check_range("key", _required(entry, "key"), 0, 127),
                *_encode_tuning_frequency(entry),
            )),
            time=time,
        )
    if msg_type == "notation":
        # Notation Information (Universal Real Time, MIDI 1.0 Detailed
        # Spec):
        #   bar_marker: F0 7F <device_id> 03 01 aa aa F7
        #     signed 14-bit bar number, LSB first, two's complement:
        #     -8192 not running, 0 count-in, 1-8190 bar, 8191 unknown.
        #   time_signature_immediate (02) / _delayed (42):
        #     F0 7F <device_id> 03 <02|42> ln nn dd cc bb [nn dd ...] F7
        #     ln = number of bytes that follow. The spec's one-line summary
        #     shows an extra 'bb'; this follows its field list (ln nn dd cc
        #     bb), which matches mido's time_signature meta event.
        command = _choice(
            message, "command", _COMMANDS["notation"], "notation",
        )
        code = _COMMANDS["notation"][command]
        device_id = _device_id(message)

        if command == "bar_marker":
            bar = _check_range(
                "bar_number", _required(message, "bar_number", command),
                -8192, 8191,
            )
            return _sysex(
                0x7F, device_id, 0x03, code,
                *_split14("bar_number", bar & 0x3FFF),
                time=time,
            )

        # time_signature_immediate / time_signature_delayed
        numerator, denominator, clocks, n32nds = (
            _required(message, field) for field in (
                "numerator", "denominator", "clocks_per_click",
                "notated_32nd_notes_per_beat",
            )
        )
        data = (
            *_encode_time_signature_pair(numerator, denominator),
            _check_range("clocks_per_click", clocks, 0, 127),
            _check_range("notated_32nd_notes_per_beat", n32nds, 0, 127),
            *_each("compound", message.get("compound", []), lambda pair: (
                _encode_time_signature_pair(
                    _required(pair, "numerator"), _required(pair, "denominator"),
                )
            )),
        )
        return _sysex(0x7F, device_id, 0x03, code, len(data), *data, time=time)
    if msg_type == "mtc_cueing":
        # MTC Real Time Cueing (RP-004/008):
        #   F0 7F <device_id> 05 <sub-id#2> sl sm <additional info> F7
        # sl sm: 14-bit event number, LSB first. Additional info is a
        # nibblized MIDI message (_nibblize), or nibblized ASCII for
        # event_name. The Non-Real-Time set-up messages are 'mtc_cueing_nrt'.
        command = _choice(
            message, "command", _COMMANDS["mtc_cueing"], "mtc_cueing",
        )
        device_id = _device_id(message)
        sub_id2 = _COMMANDS["mtc_cueing"][command]
        if command == "special_system_stop":
            # Special type 04 00 in place of the event number; the spec
            # reserves the other special types here.
            return _sysex(0x7F, device_id, 0x05, sub_id2, 0x04, 0x00, time=time)
        return _sysex(
            0x7F, device_id, 0x05, sub_id2, *_cueing_event(message, command),
            time=time,
        )
    if msg_type == "mtc_cueing_nrt":
        # MTC Non-Real Time Cueing set-up (RP-004/008):
        #   F0 7E <device_id> 04 <sub-id#2> hr mn sc fr ff sl sm
        #   <additional info> F7
        # Like 'mtc_cueing', plus a time field, delete commands, and six
        # special types. Specials (sub-id#2 00) put the special type
        # (00 00-05 00) where the event number goes. Receivers ignore the
        # time field for types 01-04, so it's sent as zeros; 00 (time
        # code offset) and 05 (event list request) take a time.
        command = _choice(
            message, "command", _COMMANDS["mtc_cueing_nrt"], "mtc_cueing_nrt",
        )
        device_id = _device_id(message)
        sub_id2 = _COMMANDS["mtc_cueing_nrt"][command]
        if command in _MTC_CUEING_NRT_SPECIAL_TYPES:
            if command in ("special_time_code_offset", "special_event_list_request"):
                time_bytes = _time_code_bytes(message, "fractional_frames", command)
            else:
                # Types 01-04: receivers ignore the time field.
                time_bytes = (0, 0, 0, 0, 0)
            return _sysex(
                0x7E, device_id, 0x04, sub_id2, *time_bytes,
                _MTC_CUEING_NRT_SPECIAL_TYPES[command], 0x00,
                time=time,
            )
        return _sysex(
            0x7E, device_id, 0x04, sub_id2,
            *_time_code_bytes(message, "fractional_frames"),
            *_cueing_event(message, command),
            time=time,
        )
    if msg_type == "file_dump":
        # File Dump (Universal Non-Real Time, MIDI 1.0 Detailed Spec):
        #   request:     F0 7E <device_id> 07 03 ss <type> <name> F7
        #   header:      F0 7E <device_id> 07 01 ss <type> <len> <name> F7
        #   data_packet: F0 7E <device_id> 07 02 <pkt#> <count> <data>
        #                <chksm> F7
        #   eof/wait/cancel/nak/ack: F0 7E <device_id> <7B-7F> pp F7
        # device_id has no default: File Dump is point to point, and the
        # source ID (ss) can't be 7F. <type> is 4 printable ASCII
        # characters ("MIDI", "BIN "); <name> is printable ASCII. <len> is
        # 28 bits in four 7-bit bytes, LSB first, 0 = unknown. Checksum:
        # XOR of every byte after F0 up to the checksum. Sample Dump
        # Standard isn't implemented.
        command = _choice(
            message, "command", _COMMANDS["file_dump"], "file_dump",
        )
        code = _COMMANDS["file_dump"][command]
        device_id = _device_id(message, default=None)

        if command in _FILE_DUMP_HANDSHAKE:
            # ack/nak name a packet; receivers ignore it for eof/wait/cancel.
            if command in ("ack", "nak"):
                packet = _required(message, "packet_number", command)
            else:
                packet = message.get("packet_number", 0)
            return _sysex(
                0x7E, device_id, code,
                _check_range("packet_number", packet, 0, 127),
                time=time,
            )

        if command == "data_packet":
            packet = _check_range(
                "packet_number", _required(message, "packet_number"), 0, 127,
            )
            encoded = _encode_file_dump_data(
                _required_list(message, "stored_bytes", command),
            )
            # <count> is the number of encoded data bytes minus 1.
            data = (0x7E, device_id, 0x07, code, packet, len(encoded) - 1, *encoded)
            return _sysex(*data, _xor_checksum(data), time=time)

        # request / header
        source = _check_range(
            "source_device_id", _required(message, "source_device_id"), 0, 126,
        )
        file_type = _required(message, "file_type")
        if len(file_type) != 4:
            raise ValueError(
                f"'file_type' must be exactly 4 characters (e.g. 'MIDI', "
                f"'TEXT', 'BIN '), got {len(file_type)} ({file_type!r})"
            )
        type_bytes = _ascii("file_type", file_type, printable=True)
        name_bytes = _ascii("filename", message.get("filename", ""), printable=True)
        if command == "request":
            return _sysex(
                0x7E, device_id, 0x07, code, source, *type_bytes, *name_bytes,
                time=time,
            )
        length = _check_range(
            "length", _required(message, "length", command), 0, 0xFFFFFFF,
        )
        return _sysex(
            0x7E, device_id, 0x07, code, source, *type_bytes,
            *((length >> shift) & 0x7F for shift in (0, 7, 14, 21)),
            *name_bytes,
            time=time,
        )
    raise ValueError(
        f"unknown message type {msg_type!r} — expected one of "
        f"{', '.join(_MESSAGE_TYPES)}"
    )


# Named RPNs: 0-4 from the MIDI 1.0 Detailed Spec Table IIIa, 6 from MPE.
# RPN 5 (Modulation Depth Range, CA-026) isn't named; send it with
# 'parameter_number'. NRPNs have no standard names.
_RPN_NAMED_PARAMETERS = {
    "pitch_bend_sensitivity": 0x0000,
    "fine_tuning": 0x0001,
    "coarse_tuning": 0x0002,
    "tuning_program_select": 0x0003,
    "tuning_bank_select": 0x0004,
    # MPE Configuration Message (M1-100-UM section 2.2.1): send on the
    # zone's Manager Channel (0 = Lower Zone, 15 = Upper Zone) with value =
    # number of Member Channels (0 turns the zone off) and msb_only true.
    "mpe_configuration": 0x0006,
}


def _build_rpn_or_nrpn_sequence(message: dict, *, registered: bool) -> list:
    """Build the Control Change sequence that selects and sets an RPN
    (registered=True) or NRPN (registered=False):
      1. parameter LSB: CC100 (RPN) or CC98 (NRPN)
      2. parameter MSB: CC101 (RPN) or CC99 (NRPN)
      3. Data Entry MSB: CC6
      4. Data Entry LSB: CC38, left out when msb_only is true
    The order matches the MIDI Tuning spec's example
    (Bn 64 03 65 00 06 tt). Data Increment/Decrement (CC96/97) aren't
    implemented because the spec doesn't define their value byte; send
    them with 'control_change'.
    """
    channel = message.get("channel", 0)
    time = message.get("time", 0)

    if registered:
        name = message.get("parameter")
        number = message.get("parameter_number")
        if name is not None and number is not None:
            raise ValueError(
                "specify only ONE of 'parameter' or 'parameter_number', not both"
            )
        if name is not None:
            number = _RPN_NAMED_PARAMETERS[
                _choice(message, "parameter", _RPN_NAMED_PARAMETERS, "rpn")
            ]
        elif number is None:
            raise KeyError("'parameter' (or 'parameter_number')")
        select_lsb_cc, select_msb_cc = 100, 101
    else:
        number = _required(message, "parameter_number")
        select_lsb_cc, select_msb_cc = 98, 99
    number_lsb, number_msb = _split14("parameter_number", number)

    value = _required(message, "value")
    if message.get("msb_only", False):
        data_entry = [(6, _check_range("value", value, 0, 127))]
    else:
        value_lsb, value_msb = _split14("value", value)
        data_entry = [(6, value_msb), (38, value_lsb)]

    controls = [(select_lsb_cc, number_lsb), (select_msb_cc, number_msb), *data_entry]
    return [
        mido.Message(
            "control_change", channel=channel, control=control, value=cc_value,
            time=time if position == 0 else 0,
        )
        for position, (control, cc_value) in enumerate(controls)
    ]


def _build_quarter_frame_sequence(message: dict) -> list:
    """Build the 8 Quarter Frame messages for one SMPTE time (RP-004/008
    pp.1-4); the same position as 'mtc_full', for receivers that only
    read Quarter Frames. Spec example: 01:37:52:16 at 30fps non-drop ->
    F1 00, F1 11, F1 24, F1 33, F1 45, F1 52, F1 61, F1 76.

    Types 0-7 carry the low/high nibbles of frames, seconds, minutes and
    the hour byte (_encode_smpte_hour_byte, so the frame rate rides in
    type 7). direction "reverse" sends types 7 to 0, as tape running
    backwards does. This is a single snapshot, not a running MTC clock.
    """
    direction = message.get("direction", "forward")
    hr, mn, sc, fr = _time_code_bytes(message)
    if direction not in ("forward", "reverse"):
        raise ValueError(
            f"'direction' must be 'forward' or 'reverse', got {direction!r}"
        )
    time = message.get("time", 0)
    # Indexed by quarter frame type; direction only changes send order.
    values_by_type = [
        fr & 0xF, fr >> 4, sc & 0xF, sc >> 4, mn & 0xF, mn >> 4, hr & 0xF, hr >> 4,
    ]
    type_order = range(8) if direction == "forward" else range(7, -1, -1)
    return [
        mido.Message(
            "quarter_frame", frame_type=frame_type,
            frame_value=values_by_type[frame_type],
            time=(time if position == 0 else 0),
        )
        for position, frame_type in enumerate(type_order)
    ]


def _build_message_sequence(message: dict) -> list:
    """Build the list of mido.Messages for a typed dict. rpn, nrpn and
    mtc_quarter_frame_sequence produce several; every other type is
    _build_message's single message in a one-item list.
    """
    msg_type = message.get("type")
    if msg_type == "rpn":
        return _build_rpn_or_nrpn_sequence(message, registered=True)
    if msg_type == "nrpn":
        return _build_rpn_or_nrpn_sequence(message, registered=False)
    if msg_type == "mtc_quarter_frame_sequence":
        return _build_quarter_frame_sequence(message)
    return [_build_message(message)]


# mido's meta message types (mido.midifiles.meta._META_SPEC_BY_TYPE).
# Valid only in write_midi_file tracks, not on a live send.
_META_TYPES = frozenset({
    "track_name", "text", "copyright", "lyrics", "marker", "cue_marker",
    "instrument_name", "device_name", "set_tempo", "time_signature",
    "key_signature", "smpte_offset", "midi_port", "channel_prefix",
    "sequence_number", "sequencer_specific", "end_of_track",
})


def _build_meta_message(message: dict) -> "mido.MetaMessage":
    """Build a mido.MetaMessage for write_midi_file. Fields pass straight
    to mido, which supplies defaults and rejects unknown names. set_tempo
    also accepts 'bpm', converted with mido.bpm2tempo().
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


# --- Decoding ----------------------------------------------------------------
# _decode_message turns a received mido.Message into the dict _build_message
# takes, with every field filled in. A SysEx decode is kept only when
# rebuilding it gives back the same bytes; otherwise the message is returned
# as plain {"type": "sysex", "data": [...]}. Either way, building the result
# reproduces the original message.


def _name_for(table: dict, code: int) -> str:
    """The name whose value is `code` in a name -> code table."""
    for name, value in table.items():
        if value == code:
            return name
    raise KeyError(code)


def _join14(lsb: int, msb: int) -> int:
    return lsb | (msb << 7)


def _decode_channel_mode(msg: "mido.Message") -> "dict | None":
    """Control Change 120-127 with a value the spec allows, as channel_mode;
    None for any other Control Change."""
    try:
        command = _name_for(_COMMANDS["channel_mode"], msg.control)
    except KeyError:
        return None
    out = {"type": "channel_mode", "command": command, "channel": msg.channel}
    if command == "local_control":
        if msg.value not in (0, 127):
            return None
        out["on"] = msg.value == 127
    elif command == "mono_on":
        if msg.value > 16:
            return None
        out["channel_count"] = msg.value
    elif msg.value != 0:
        return None
    return out


def _decode_gm_system(data: tuple) -> dict:
    _, device_id, _, code = data
    return {
        "type": "gm_system", "command": _name_for(_COMMANDS["gm_system"], code),
        "device_id": device_id,
    }


def _decode_device_inquiry(data: tuple) -> dict:
    device_id, code, rest = data[1], data[3], data[4:]
    command = _name_for(_COMMANDS["device_inquiry"], code)
    out: dict = {"type": "device_inquiry", "command": command, "device_id": device_id}
    if command == "reply":
        if rest[0] == 0:
            manufacturer_id, rest = list(rest[:3]), rest[3:]
        else:
            manufacturer_id, rest = rest[0], rest[1:]
        if len(rest) != 8:
            raise ValueError("Identity Reply has the wrong length")
        out.update(
            manufacturer_id=manufacturer_id,
            device_family_code=_join14(rest[0], rest[1]),
            device_family_member_code=_join14(rest[2], rest[3]),
            software_revision=list(rest[4:8]),
        )
    return out


def _decode_device_control(data: tuple) -> dict:
    _, device_id, _, code, lsb, msb = data
    return {
        "type": "device_control",
        "command": _name_for(_COMMANDS["device_control"], code),
        "device_id": device_id, "value": _join14(lsb, msb),
    }


def _decode_time_code(hr: int, mn: int, sc: int, fr: int, *fifth: int,
                      subframe_field: "str | None" = None) -> dict:
    """The time fields of a Standard Time Code with the flags off; inverse of
    _time_code_bytes."""
    out = {
        "hours": hr & 0x1F, "minutes": mn, "seconds": sc, "frames": fr,
        "frame_rate": _name_for(_FRAME_RATE_BITS, (hr >> 5) & 0x3),
    }
    if subframe_field:
        out[subframe_field] = fifth[0]
    return out


def _denibblize(nibbles) -> list:
    """Inverse of _nibblize: low nibble first, two nibbles per byte."""
    if len(nibbles) % 2:
        raise ValueError("odd number of nibbles")
    return [nibbles[i] | (nibbles[i + 1] << 4) for i in range(0, len(nibbles), 2)]


def _decode_file_dump_data(encoded) -> list:
    """Inverse of _encode_file_dump_data: groups of a sign byte plus up to 7
    low-7-bit bytes."""
    stored: list = []
    for start in range(0, len(encoded), 8):
        sign, *low = encoded[start:start + 8]
        stored += [b | (((sign >> (6 - i)) & 1) << 7) for i, b in enumerate(low)]
    return stored


def _checksum_ok(data: tuple) -> bool:
    """Whether the last byte is the XOR of the bytes before it."""
    return _xor_checksum(data[:-1]) == data[-1]


def _decode_mtc(data: tuple) -> dict:
    # Full Message only; User Bits (01 02) isn't implemented.
    _, device_id, _, code, hr, mn, sc, fr = data
    if code != 0x01:
        raise ValueError("not an MTC Full Message")
    return {"type": "mtc_full", "device_id": device_id,
            **_decode_time_code(hr, mn, sc, fr)}


def _decode_handshake(data: tuple) -> dict:
    # EOF/WAIT/CANCEL/NAK/ACK, shared by File Dump (and Sample Dump). The MTC
    # sync-dropped NAK has the same bytes and decodes as file_dump 'nak'.
    _, device_id, sub_id1, packet_number = data
    return {"type": "file_dump", "command": _name_for(_FILE_DUMP_HANDSHAKE, sub_id1),
            "device_id": device_id, "packet_number": packet_number}


def _decode_file_dump(data: tuple) -> dict:
    device_id, code = data[1], data[3]
    command = _name_for(_COMMANDS["file_dump"], code)
    out: dict = {"type": "file_dump", "command": command, "device_id": device_id}
    if command == "data_packet":
        packet_number, count, *encoded = data[4:-1]
        if count != len(encoded) - 1:
            raise ValueError("Data Packet count doesn't match its data")
        out.update(packet_number=packet_number,
                   stored_bytes=_decode_file_dump_data(encoded),
                   checksum_ok=_checksum_ok(data))
        return out
    source, file_type, rest = data[4], data[5:9], data[9:]
    out.update(source_device_id=source, file_type=bytes(file_type).decode("ascii"))
    if command == "header":
        out["length"] = sum(b << (7 * i) for i, b in enumerate(rest[:4]))
        rest = rest[4:]
    out["filename"] = bytes(rest).decode("ascii")
    return out


def _decode_midi_tuning(data: tuple) -> dict:
    device_id, code, program = data[1], data[3], data[4]
    command = _name_for(_COMMANDS["midi_tuning"], code)
    out: dict = {"type": "midi_tuning", "command": command,
                 "device_id": device_id, "tuning_program": program}

    def frequency(xx: int, yy: int, zz: int) -> dict:
        if (xx, yy, zz) == (0x7F, 0x7F, 0x7F):
            return {"no_change": True}
        return {"semitone": xx, "cents": _join14(zz, yy) * 100 / 16384}

    if command == "bulk_dump_reply":
        name, freq = data[5:21], data[21:-1]
        if len(freq) != 384:
            raise ValueError("bulk dump needs 128 x 3 frequency bytes")
        out.update(
            tuning_name=bytes(name).decode("ascii").rstrip(" "),
            notes=[frequency(*freq[i:i + 3]) for i in range(0, 384, 3)],
            checksum_ok=_checksum_ok(data),
        )
    elif command == "note_change":
        count, entries = data[5], data[6:]
        if len(entries) != 4 * count:
            raise ValueError("note change count doesn't match its entries")
        out["changes"] = [
            {"key": entries[i], **frequency(*entries[i + 1:i + 4])}
            for i in range(0, len(entries), 4)
        ]
    return out


def _decode_notation(data: tuple) -> dict:
    device_id, code = data[1], data[3]
    command = _name_for(_COMMANDS["notation"], code)
    out: dict = {"type": "notation", "command": command, "device_id": device_id}
    if command == "bar_marker":
        _, _, _, _, lsb, msb = data
        bar = _join14(lsb, msb)
        out["bar_number"] = bar - 0x4000 if bar >= 0x2000 else bar
        return out
    length, nn, dd, cc, bb, *compound = data[4:]
    if length != 4 + len(compound) or len(compound) % 2:
        raise ValueError("time signature length doesn't match its data")
    out.update(
        numerator=nn, denominator=2 ** dd, clocks_per_click=cc,
        notated_32nd_notes_per_beat=bb,
        compound=[{"numerator": compound[i], "denominator": 2 ** compound[i + 1]}
                  for i in range(0, len(compound), 2)],
    )
    return out


def _decode_cueing_event(command: str, sl: int, sm: int, info) -> dict:
    """Inverse of _cueing_event."""
    out: dict = {"event_number": _join14(sl, sm)}
    if command == "event_name":
        out["event_name"] = bytes(_denibblize(info)).decode("ascii")
    elif command.endswith("_with_info"):
        out["additional_info_bytes"] = _denibblize(info)
    elif info:
        raise ValueError(f"{command} carries no additional info")
    return out


def _decode_mtc_cueing(data: tuple) -> dict:
    device_id, code, sl, sm, info = data[1], data[3], data[4], data[5], data[6:]
    command = _name_for(_COMMANDS["mtc_cueing"], code)
    out: dict = {"type": "mtc_cueing", "command": command, "device_id": device_id}
    if command != "special_system_stop":
        out.update(_decode_cueing_event(command, sl, sm, info))
    return out


def _decode_mtc_cueing_nrt(data: tuple) -> dict:
    device_id, code = data[1], data[3]
    time_bytes, sl, sm, info = data[4:9], data[9], data[10], data[11:]
    out: dict = {"type": "mtc_cueing_nrt", "device_id": device_id}
    if code == 0x00:
        command = _name_for(_MTC_CUEING_NRT_SPECIAL_TYPES, sl)
        out["command"] = command
        if command in ("special_time_code_offset", "special_event_list_request"):
            out.update(_decode_time_code(*time_bytes, subframe_field="fractional_frames"))
        return out
    specials = set(_MTC_CUEING_NRT_SPECIAL_TYPES)
    command = _name_for(
        {k: v for k, v in _COMMANDS["mtc_cueing_nrt"].items() if k not in specials}, code,
    )
    out["command"] = command
    out.update(_decode_time_code(*time_bytes, subframe_field="fractional_frames"))
    out.update(_decode_cueing_event(command, sl, sm, info))
    return out


def _decode_msc_cue(data) -> dict:
    """Inverse of _encode_msc_cue_data: up to three ASCII fields separated
    by 00."""
    if not data:
        return {}
    parts = bytes(data).split(b"\x00")
    if len(parts) > 3 or not all(parts):
        raise ValueError("bad MSC cue data")
    return {
        name: part.decode("ascii")
        for name, part in zip(("q_number", "q_list", "q_path"), parts)
    }


def _decode_msc_timed_go(data) -> dict:
    return {
        **_decode_time_code(*data[:5], subframe_field="fractional_frames"),
        **_decode_msc_cue(data[5:]),
    }


def _decode_msc_set(data) -> dict:
    out = {
        "control_number": _join14(data[0], data[1]),
        "control_value": _join14(data[2], data[3]),
    }
    if len(data) > 4:
        out.update(_decode_time_code(*data[4:9], subframe_field="fractional_frames"))
    return out


# Inverses of _MSC_DATA_BUILDERS: <data> -> fields.
_MSC_DATA_DECODERS = {
    "go": _decode_msc_cue,
    "stop": _decode_msc_cue,
    "resume": _decode_msc_cue,
    "go_off": _decode_msc_cue,
    "load": _decode_msc_cue,
    "timed_go": _decode_msc_timed_go,
    "set": _decode_msc_set,
    "fire": lambda data: {"macro_number": data[0]},
}


def _decode_msc(data: tuple) -> dict:
    device_id, command_format, code, payload = data[1], data[3], data[4], data[5:]
    command = _name_for(_COMMANDS["msc"], code)
    out: dict = {"type": "msc", "command": command, "device_id": device_id}
    try:
        out["command_format"] = _name_for(_MSC_FORMATS, command_format)
    except KeyError:
        out["command_format_raw"] = command_format
    decoder = _MSC_DATA_DECODERS.get(command)
    if decoder is not None:
        out.update(decoder(payload))
    elif payload:
        raise ValueError(f"MSC {command} carries no data")
    return out


def _decode_speed(data) -> dict:
    """Inverse of _encode_standard_speed."""
    sh, sm, sl = data
    shift = (sh >> 3) & 0x7
    raw = ((sh & 0x7) << 14) | (sm << 7) | sl
    return {"speed": raw / 2 ** (14 - shift), "reverse": bool(sh & 0x40)}


def _decode_mmc_locate(data) -> dict:
    # Only LOCATE [TARGET] (sub-command 01) is implemented.
    if data[0] != 0x01:
        raise ValueError("only LOCATE [TARGET] is decoded")
    return _decode_time_code(*data[1:6], subframe_field="subframes")


def _info_field_name(code: int) -> str:
    return _name_for(_INFO_FIELD_NAMES, code)


def _decode_mmc_procedure(data) -> dict:
    action = _name_for(_MMC_PROCEDURE_ACTIONS, data[0])
    out: dict = {"action": action, "procedure": data[1]}
    if action == "assemble":
        out["commands"] = _mmc_parse_commands(data[2:])
    return out


def _decode_mmc_event(data) -> dict:
    action = _name_for(_MMC_EVENT_ACTIONS, data[0])
    out: dict = {"action": action, "event": data[1]}
    if action == "define":
        flags, source, name = data[2], data[3], data[4]
        nested = _mmc_parse_commands(data[5:])
        if len(nested) != 1:
            raise ValueError("EVENT [DEFINE] takes exactly one command")
        out.update(
            direction=_name_for(_MMC_EVENT_DIRECTIONS, flags & 0x03),
            all_speeds=bool(flags & 0x10), non_delete=bool(flags & 0x40),
            trigger_source=_info_field_name(source), name=_info_field_name(name),
            trigger_command=nested[0],
        )
    return out


# Inverses of _MMC_DATA_BUILDERS: <data> -> fields.
_MMC_DATA_DECODERS = {
    "locate": _decode_mmc_locate,
    "step": lambda d: {"quantity": d[0] & 0x3F, "reverse": bool(d[0] & 0x40)},
    "assign_system_master": lambda d: {"target_device_id": d[0]},
    "generator_command": lambda d: {"action": _name_for(_MMC_GENERATOR_ACTIONS, d[0])},
    "midi_time_code_command": lambda d: {
        "action": _name_for(_MMC_MTC_COMMAND_ACTIONS, d[0]),
    },
    "variable_play": _decode_speed,
    "search": _decode_speed,
    "shuttle": _decode_speed,
    "deferred_variable_play": _decode_speed,
    "record_strobe_variable": _decode_speed,
    "drop_frame_adjust": lambda d: {"name": _info_field_name(d[0])},
    "move": lambda d: {
        "destination": _info_field_name(d[0]), "source": _info_field_name(d[1]),
    },
    "add": lambda d: {
        "destination": _info_field_name(d[0]),
        "source_1": _info_field_name(d[1]), "source_2": _info_field_name(d[2]),
    },
    "subtract": lambda d: {
        "destination": _info_field_name(d[0]),
        "source_1": _info_field_name(d[1]), "source_2": _info_field_name(d[2]),
    },
    "group": lambda d: {
        "action": _name_for(_MMC_GROUP_ACTIONS, d[0]), "group": d[1],
        "device_ids": list(d[2:]),
    },
    "procedure": _decode_mmc_procedure,
    "event": _decode_mmc_event,
    "read": lambda d: {"names": [_info_field_name(b) for b in d]},
    "write": lambda d: {"fields": [
        {"name": _info_field_name(d[i]), **_decode_standard_time_code(*d[i + 1:i + 6])}
        for i in range(0, len(d), 6)
    ]},
    "masked_write": lambda d: {"fields": [
        {"name": _info_field_name(d[i]), "byte_number": d[i + 1],
         "mask": d[i + 2], "data": d[i + 3]}
        for i in range(0, len(d), 4)
    ]},
    "update": lambda d: {
        "action": _name_for(_MMC_UPDATE_ACTIONS, d[0]),
        "names": ["all" if b == 0x7F else _info_field_name(b) for b in d[1:]],
    },
}


def _mmc_parse_commands(body) -> list:
    """Inverse of _mmc_command_bytes over a run of commands: a list of mmc
    command dicts."""
    commands, at = [], 0
    while at < len(body):
        command = _name_for(_COMMANDS["mmc"], body[at])
        out: dict = {"type": "mmc", "command": command}
        decoder = _MMC_DATA_DECODERS.get(command)
        if decoder is None:
            at += 1
        else:
            count = body[at + 1]
            data = body[at + 2:at + 2 + count]
            if len(data) != count:
                raise ValueError(f"MMC {command} is truncated")
            out.update(decoder(data))
            at += 2 + count
        commands.append(out)
    return commands


def _decode_mmc(data: tuple) -> dict:
    # One command per message; a SysEx carrying several MMC commands (which
    # RP-013 allows) doesn't rebuild and stays raw.
    commands = _mmc_parse_commands(data[3:])
    if len(commands) != 1:
        raise ValueError("not exactly one MMC command")
    command = commands[0]
    return {"type": "mmc", "command": command["command"], "device_id": data[1],
            **{k: v for k, v in command.items() if k not in ("type", "command")}}


def _decode_mmc_response_sysex(data: tuple) -> dict:
    # Device-to-controller replies: decoded for reading, not for sending.
    # 'mmc_response' isn't a message type _build_message accepts.
    fields = _mmc_response_fields([0xF0, *data, 0xF7])
    return {"type": "mmc_response", "device_id": fields.pop("device_id"),
            "response": fields}


# SysEx decoded for reading only, with no rebuild check.
_SYSEX_READ_ONLY_DECODERS = {
    (0x7F, 0x07): _decode_mmc_response_sysex,
}


# (universal ID, sub-ID#1) -> decoder for the SysEx data (F0/F7 excluded).
_SYSEX_DECODERS = {
    (0x7E, 0x09): _decode_gm_system,
    (0x7E, 0x06): _decode_device_inquiry,
    (0x7F, 0x04): _decode_device_control,
    (0x7F, 0x01): _decode_mtc,
    **{(0x7E, code): _decode_handshake for code in _FILE_DUMP_HANDSHAKE.values()},
    (0x7E, 0x07): _decode_file_dump,
    (0x7E, 0x08): _decode_midi_tuning,
    (0x7F, 0x08): _decode_midi_tuning,
    (0x7F, 0x03): _decode_notation,
    (0x7F, 0x05): _decode_mtc_cueing,
    (0x7E, 0x04): _decode_mtc_cueing_nrt,
    (0x7F, 0x02): _decode_msc,
    (0x7F, 0x06): _decode_mmc,
}


def _decode_sysex(data: tuple) -> dict:
    """A SysEx payload (F0/F7 excluded) as its typed dict, or as plain
    'sysex' when no decoder applies or the decode doesn't rebuild to the
    same bytes. A decode carrying 'checksum_ok' is kept when everything but
    the checksum byte rebuilds; 'checksum_ok' says whether the received
    checksum was right."""
    if len(data) >= 3:
        read_only = _SYSEX_READ_ONLY_DECODERS.get((data[0], data[2]))
        if read_only is not None:
            try:
                return read_only(data)
            except (IndexError, KeyError, ValueError, TypeError):
                return {"type": "sysex", "data": list(data)}
        decoder = _SYSEX_DECODERS.get((data[0], data[2]))
        if decoder is not None:
            try:
                decoded = decoder(data)
                rebuilt = tuple(_build_message(dict(decoded)).data)
                if rebuilt == data or (
                    "checksum_ok" in decoded and rebuilt[:-1] == data[:-1]
                ):
                    return decoded
            except (IndexError, KeyError, ValueError, TypeError, UnicodeDecodeError):
                pass
    return {"type": "sysex", "data": list(data)}


def _decode_message(msg: "mido.Message") -> dict:
    """A received mido.Message as the dict _build_message takes."""
    if msg.type == "sysex":
        return _decode_sysex(tuple(msg.data))
    if msg.type == "control_change":
        mode = _decode_channel_mode(msg)
        if mode is not None:
            return mode
    out: dict = {"type": msg.type}
    if msg.type in _CHANNEL_TYPES:
        out["channel"] = msg.channel
    for name in _MIDO_FIELDS.get(msg.type, {}):
        out[name] = getattr(msg, name)
    return out

class _StreamDecoder:
    """Per-input state for changes that span several wire messages: RPN/NRPN
    parameter changes and MTC Quarter Frame sequences. feed() takes each
    received message in order and returns the combined message it completes,
    or None. A combined message is returned only when building it gives back
    exactly the messages received.

    RPN/NRPN (MIDI 1.0 Detailed Spec, per channel): CC 101/100 select an RPN
    and CC 99/98 an NRPN, in either order; the selection stays until another
    one. CC 6 (Data Entry MSB) completes a 7-bit change (msb_only), and a CC 38
    (Data Entry LSB) right after it completes the 14-bit change, so a 14-bit
    sender yields two results. RPN Null (127/127) makes data entry ignored.

    Quarter Frames: eight consecutive pieces, types 0 to 7 (forward) or 7 to 0
    (reverse), complete one time.
    """

    def __init__(self) -> None:
        # channel -> {"kind": "rpn"|"nrpn", "msb", "lsb", "value_msb"}
        self._params: dict = {}
        self._quarter_frames: list = []

    def feed(self, msg: "mido.Message") -> "dict | None":
        if msg.type == "control_change":
            return self._control_change(msg)
        if msg.type == "quarter_frame":
            return self._quarter_frame(msg)
        return None

    def _control_change(self, msg: "mido.Message") -> "dict | None":
        selects = {101: ("rpn", "msb"), 100: ("rpn", "lsb"),
                   99: ("nrpn", "msb"), 98: ("nrpn", "lsb")}
        state = self._params.setdefault(msg.channel, {})
        if msg.control in selects:
            kind, part = selects[msg.control]
            if state.get("kind") != kind:
                state.clear()
                state["kind"] = kind
            state[part] = msg.value
            state["value_msb"] = None
            return None
        if msg.control not in (6, 38) or state.get("msb") is None or state.get("lsb") is None:
            return None
        number = _join14(state["lsb"], state["msb"])
        if state["kind"] == "rpn" and number == 0x3FFF:
            return None  # RPN Null: data entry is ignored
        if msg.control == 6:
            state["value_msb"] = msg.value
            value, msb_only = msg.value, True
        elif state["value_msb"] is None:
            return None  # LSB with no MSB before it
        else:
            value, msb_only = _join14(msg.value, state["value_msb"]), False
        combined: dict = {"type": state["kind"], "channel": msg.channel}
        if state["kind"] == "rpn" and number in _RPN_NAMED_PARAMETERS.values():
            combined["parameter"] = _name_for(_RPN_NAMED_PARAMETERS, number)
        else:
            combined["parameter_number"] = number
        combined.update(value=value, msb_only=msb_only)
        # The rebuilt sequence must end with the data-entry message just read.
        try:
            rebuilt = _build_rpn_or_nrpn_sequence(
                dict(combined), registered=state["kind"] == "rpn",
            )
        except (KeyError, ValueError):
            return None
        return combined if rebuilt[-1].bytes() == msg.bytes() else None

    def _quarter_frame(self, msg: "mido.Message") -> "dict | None":
        # A run starting at type 0 goes forward 0..7; one starting at 7 goes
        # in reverse 7..0. Anything out of order starts over.
        pieces = self._quarter_frames
        if pieces:
            step = 1 if pieces[0].frame_type == 0 else -1
            if msg.frame_type != pieces[-1].frame_type + step:
                pieces.clear()
        if not pieces and msg.frame_type not in (0, 7):
            return None
        pieces.append(msg)
        if len(pieces) < 8:
            return None
        values = {p.frame_type: p.frame_value for p in pieces}
        received = [(p.frame_type, p.frame_value) for p in pieces]
        reverse = pieces[0].frame_type == 7
        pieces.clear()
        combined = {
            "type": "mtc_quarter_frame_sequence",
            "direction": "reverse" if reverse else "forward",
            **_decode_time_code(
                values[6] | (values[7] << 4), values[4] | (values[5] << 4),
                values[2] | (values[3] << 4), values[0] | (values[1] << 4),
            ),
        }
        try:
            rebuilt = _build_quarter_frame_sequence(dict(combined))
        except (KeyError, ValueError):
            return None
        if [(m.frame_type, m.frame_value) for m in rebuilt] != received:
            return None
        return combined


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

    # 'sent' is a string for one message, a list for several.
    if len(msgs) == 1:
        return json.dumps({"status": "ok", "sent": str(msgs[0])})
    return json.dumps({"status": "ok", "sent": [str(m) for m in msgs]})


def _poll(tool_input: dict) -> str:
    handle = tool_input.get("handle")
    entry = _OPEN_PORTS.get(handle) if handle else None
    if entry is None:
        return _err(f"no open port for handle {handle!r}")
    direction, _port = entry
    if direction != "input":
        return _err(f"handle {handle!r} is an output port, cannot poll it")

    buf = _INPUT_BUFFERS.get(handle)
    event = _INPUT_EVENTS.get(handle)
    if buf is None or event is None:
        # _open always creates both for an input handle.
        return _err(
            f"no message buffer for handle {handle!r} (internal "
            "inconsistency — the port may not have been opened correctly)"
        )

    timeout_seconds = tool_input.get("timeout_seconds", 0)
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
        return _err("'timeout_seconds' must be a number (0-60, default 0)")
    if not (0 <= timeout_seconds <= _MAX_POLL_TIMEOUT):
        return _err(
            f"'timeout_seconds' must be 0-{_MAX_POLL_TIMEOUT}, got "
            f"{timeout_seconds!r}"
        )

    # Wait until something is buffered or the timeout passes. The event is
    # cleared just before each wait and the buffer re-checked after, so a
    # set left over from an earlier drain can't end the wait early, and a
    # message that lands between the check and wait() still wakes it (the
    # callback appends before it sets).
    deadline = time.monotonic() + timeout_seconds
    while not buf:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        event.clear()
        if buf:
            break
        event.wait(timeout=remaining)

    # 'received_at' was stamped by the callback on arrival. 'decoded' is the
    # dict 'send' takes; 'completes' is an RPN/NRPN change or Quarter Frame
    # time that this message finishes.
    stream = _STREAM_DECODERS.setdefault(handle, _StreamDecoder())
    messages = []
    while buf:
        received_at, msg = buf.popleft()
        entry = {
            "received_at": received_at, "message": str(msg),
            "hex": msg.hex(), "decoded": _decode_message(msg),
        }
        completed = stream.feed(msg)
        if completed is not None:
            entry["completes"] = completed
        messages.append(entry)

    return json.dumps({"status": "ok", "messages": messages})


# MIDI Clock rate, fixed by the MIDI 1.0 spec.
_CLOCK_PULSES_PER_QUARTER_NOTE = 24


def _run_clock(tool_input: dict) -> str:
    handle = tool_input.get("handle")
    entry = _OPEN_PORTS.get(handle) if handle else None
    if entry is None:
        return _err(f"no open port for handle {handle!r}")
    direction, port = entry
    if direction != "output":
        return _err(f"handle {handle!r} is an input port, cannot run_clock on it")

    bpm = tool_input.get("bpm")
    if not isinstance(bpm, (int, float)) or isinstance(bpm, bool):
        return _err("'bpm' is required and must be a number for 'run_clock'")
    if not (20 <= bpm <= 300):
        return _err(f"'bpm' must be 20-300, got {bpm!r}")

    duration_seconds = tool_input.get("duration_seconds")
    if not isinstance(duration_seconds, (int, float)) or isinstance(duration_seconds, bool):
        return _err(
            "'duration_seconds' is required and must be a number for 'run_clock'"
        )
    if not (0 < duration_seconds <= _MAX_CLOCK_DURATION):
        return _err(
            f"'duration_seconds' must be > 0 and <= {_MAX_CLOCK_DURATION}, "
            f"got {duration_seconds!r}"
        )

    transport = tool_input.get("transport", "start")
    if transport not in ("start", "continue", "none"):
        return _err("'transport' must be 'start', 'continue', or 'none'")

    stop_at_end = tool_input.get("stop_at_end", True)
    if not isinstance(stop_at_end, bool):
        return _err("'stop_at_end' must be a boolean")

    interval = 60.0 / bpm / _CLOCK_PULSES_PER_QUARTER_NOTE
    n_ticks = int(duration_seconds / interval)

    try:
        transport_sent = None
        if transport != "none":
            port.send(mido.Message(transport))
            transport_sent = transport

        # Schedule against absolute tick times so the time spent in
        # port.send() doesn't add up as drift.
        start_time = time.time()
        next_tick = start_time
        for _ in range(n_ticks):
            now = time.time()
            sleep_time = next_tick - now
            if sleep_time > 0:
                time.sleep(sleep_time)
            port.send(mido.Message("clock"))
            next_tick += interval
        elapsed = time.time() - start_time

        stop_sent = False
        if stop_at_end:
            port.send(mido.Message("stop"))
            stop_sent = True
    except Exception as e:
        return _err(f"run_clock failed: {type(e).__name__}: {e}")

    return json.dumps(
        {
            "status": "ok",
            "handle": handle,
            "bpm": bpm,
            "ticks_sent": n_ticks,
            "elapsed_seconds": round(elapsed, 3),
            "transport_sent": transport_sent,
            "stop_sent": stop_sent,
        }
    )


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
    """Write a .mid/.midi file from 'tracks' or a .syx file from sysex
    'messages'. Refuses to replace an existing file unless 'overwrite'.
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
                        # Multi-message types put the caller's delta on
                        # the first message and 0 on the rest.
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

    # Re-read the new file; the summary doubles as a check.
    return _read_midi_file({"path": path, "max_messages": 0})
