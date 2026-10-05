"""Registry of the client-executed tools.

Every module in MODULES exposes `TOOLS` (Anthropic tool schemas),
`handles(name)` and `await execute(name, input)`, so adding a tool means
writing one module and listing it here. Third-party packages are imported
inside each module's `execute`, so a tool whose dependency is missing still
declares itself and returns an install hint if the model calls it.
"""

import inspect

from core import (
    bash_session,
    browser,
    computer,
    config_edit,
    data,
    desktop_window,
    documents,
    files,
    kernel,
    listen,
    memory,
    midi1,
    processes,
    screen_find,
    speak,
    text_embeddings,
    vision,
)
from core import claude_learned_schemas as learned

MODULES = [
    learned,     # bash, text editor, web_search, web_fetch
    memory,      # cross-session memory (learned schema)
    computer,    # screen/mouse/keyboard control (client toolset, no beta header)
    desktop_window,  # list/focus/move windows (KDE, KWin scripting)
    screen_find,  # locate on-screen text and buttons by OCR, in computer coordinates
    browser,     # Playwright DOM surfing
    documents,   # LibreOffice / pandoc conversion
    kernel,      # stateful IPython
    bash_session,  # persistent bash — cd/env/venv/bg jobs survive across calls
    processes,   # pexpect interactive commands
    config_edit,  # comment-preserving YAML/TOML/JSON edits
    data,        # DuckDB
    files,       # trash
    text_embeddings,  # your own private embedding server, config-driven
    vision,       # your own private vision-capable model, config-driven
    speak,        # local text-to-speech via Piper, config-driven
    listen,       # local speech-to-text via faster-whisper, config-driven
    midi1,        # MIDI 1.0 device I/O via mido/python-rtmidi — device
                  # discovery, open/close/send/poll, every standard channel,
                  # System Common and System Real-Time message, generic SysEx,
                  # .mid/.syx read and write, and typed SysEx messages (MTC,
                  # MMC, MSC, RPN/NRPN, General MIDI system, device inquiry,
                  # channel mode, MIDI tuning, notation). MIDI 2.0/UMP is a
                  # separate project.
]

TOOLS = [tool for module in MODULES for tool in module.TOOLS]

# A client TOOLSET entry (e.g. computer.COMPUTER_TOOL) has no "name": its dated
# `type` fixes the member set, so duplicate-checking skips it. That cannot hide
# a real collision, because a toolset's members are declared server-side.
_NAMED = [t["name"] for t in TOOLS if "name" in t]
_DUPLICATES = {name for name in _NAMED if _NAMED.count(name) > 1}
if _DUPLICATES:
    raise ValueError(f"duplicate local tool names: {sorted(_DUPLICATES)}")


def handles(name: str) -> bool:
    return any(module.handles(name) for module in MODULES)


async def execute(name: str, tool_input: dict) -> str | None:
    """Run a local tool, or return None if no local module owns that name."""
    for module in MODULES:
        if module.handles(name):
            return await module.execute(name, tool_input)
    return None


async def shutdown():
    """Release everything a local tool may have started; safe if unused.

    Each step is isolated because this runs as an AsyncExitStack callback on
    exit: an exception escaping one step would skip the rest and turn Ctrl-C
    into a traceback.
    """
    for label, close in (
        ("browser", browser.shutdown),
        ("computer", computer.shutdown),
        ("desktop_window", desktop_window.shutdown),
        ("kernel", kernel.shutdown),
        ("bash_session", bash_session.shutdown),
        ("sql_query", data.close),
        ("midi1", midi1.close_all),
    ):
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            print(f"[shutdown] {label} cleanup failed (ignored): {e}")
