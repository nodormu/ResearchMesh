"""Persistent bash — a real shell process that survives across tool calls.

The `bash` tool (core/claude_learned_schemas.py) spawns a fresh subprocess
every call, so `cd`, exported env vars, sourced venvs, shell functions, and
background jobs all die at the end of that call. This module keeps ONE
shell alive for the life of the session — same singleton-process pattern
core/kernel.py uses for the IPython kernel, just driven over a pty via
pexpect (already a hard dependency — see core/processes.py, which uses the
same library for interactive_run) instead of ZeroMQ.

Framing: each command is wrapped in a brace group together with a
`printf` that emits a random per-spawn sentinel plus `$?`. We `expect()`
that sentinel to know exactly where the command's output ends and what it
returned. The group also keeps a defensive PS1/PROMPT_COMMAND reset inside
the same still-open construct as the command itself, so a command that
changes the prompt (a venv/conda/direnv activation) can't leak prompt text
into the output — see the reset in `_run()` for the mechanism.

Not a replacement for `bash`: use plain `bash` for one-off commands, this
for anything that needs state to survive across multiple calls. Also not a
replacement for `interactive_run` — a foreground command that blocks on
its own stdin (a password prompt, `read`, an installer) will hang here
exactly as it would in `bash`, until the per-call timeout fires.
"""

import asyncio
import json
import re
import uuid

from core.claude_learned_schemas import SHELL_EXECUTABLE, apply_shell_prelude
from core.output import clip

# A bare interactive shell loads the user's own ~/.bashrc in full, which on
# most distros (confirmed live here) sets a colored PS1, an OSC window-title
# escape, and bash enables readline's bracketed-paste mode automatically on
# every interactive read — none of that is driven by *our* commands, so it
# can't be avoided by just not asking for color. Suppressed once at spawn
# (bracketed paste, PROMPT_COMMAND, PS1) rather than per call, since it's a
# persistent shell. Also stripped defensively from every captured output
# below, in case anything the user's OWN commands run emits its own color
# codes, or an unusual shell config leaks something past the spawn-time fix.
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[a-zA-Z]")

TOOLS = [
    {
        "name": "bash_session",
        "description": (
            "Run a command in a PERSISTENT bash shell. Unlike the `bash` tool, "
            "which spawns a fresh subprocess every call, this keeps the SAME "
            "shell alive across calls — `cd`, exported environment variables, "
            "sourced virtualenvs/`nvm`/etc., shell functions, and background "
            "jobs (`command &`) all survive from one call to the next. Use "
            "plain `bash` for quick one-off commands; use this when you need "
            "that persistence (e.g. `cd` into a project once and run several "
            "commands relative to it, or activate a venv and keep using it). "
            "stdout and stderr are merged into one `output` string, same as a "
            "real terminal, since this runs on a real pty. Like `bash`, a "
            "command that blocks waiting on its own stdin (a password prompt, "
            "`read`, an installer) will hang until `timeout` — use "
            "`interactive_run` for those instead. Pass `restart: true` (alone, "
            "as its own call) to kill and respawn the shell, discarding all "
            "state, if the session gets wedged or you want a clean environment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Shell command to run in the persistent session. May "
                        "be multi-line (e.g. a for-loop or heredoc)."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Seconds to wait for this command to finish (default "
                        "120). On timeout, the session attempts to recover "
                        "with Ctrl-C so the NEXT call isn't necessarily also "
                        "stuck — check the `recovered` field."
                    ),
                },
                "restart": {
                    "type": "boolean",
                    "description": (
                        "Kill and respawn the shell, discarding cwd/env/"
                        "background jobs. Send this alone, without `command`, "
                        "in its own call."
                    ),
                },
            },
        },
    }
]

_TOOL_NAMES = {t["name"] for t in TOOLS}
_MAX_OUTPUT = 12000
_DEFAULT_TIMEOUT = 120
_RECOVERY_TIMEOUT = 10

try:
    import pexpect
except ImportError:  # pragma: no cover - pexpect is a hard dependency; this
    # mirrors the friendly-degrade style core/processes.py already uses for
    # the same import, in case anything is ever run from a stripped venv.
    pexpect = None

_shell = None
_sentinel = None


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


async def execute(name: str, tool_input: dict) -> str:
    if name != "bash_session":
        return json.dumps({"error": f"unknown bash_session tool {name!r}"})
    return await asyncio.to_thread(_run, tool_input)


def _sentinel_pattern() -> str:
    # Only ever called after `_spawn()` has set `_sentinel` (from `_spawn()`
    # itself, or from `_run()`/`_handle_timeout()` post-self-heal) — spelled
    # out for mypy rather than assumed, same reasoning as kernel.py's own
    # equivalent None-check before dereferencing its globals.
    assert _sentinel is not None
    return re.escape(_sentinel) + r":(-?\d+)"


def _spawn() -> str | None:
    """(Re)start the persistent shell. Returns an error string, or None."""
    global _shell, _sentinel

    if pexpect is None:
        return "pexpect is not installed — `pip install pexpect` to enable the bash_session tool"

    _shutdown_sync()
    _sentinel = uuid.uuid4().hex

    try:
        shell = pexpect.spawn(
            SHELL_EXECUTABLE,
            encoding="utf-8",
            codec_errors="replace",
            echo=False,
            timeout=_DEFAULT_TIMEOUT,
        )
        # Quiet the interactive-shell decoration that ~/.bashrc turns on and
        # that a bare spawn otherwise inherits in full: bracketed-paste mode
        # (readline toggles \e[?2004h/l around every line it reads, whether
        # or not PS1 uses color), any OSC window-title sequence woven into
        # PS1 or PROMPT_COMMAND, PS1 itself, and PS2 (the continuation
        # prompt bash prints while still reading a multi-line for-loop or
        # heredoc — confirmed live: without blanking this too, "> " leaks
        # into the merged output of any multi-line command). `bind` is
        # bash-specific and
        # silently no-ops (stderr swallowed) on other shells, which is fine
        # — this priming line's output is discarded either way. Then apply
        # the zsh word-splitting/glob prelude ONCE here (a no-op string on
        # bash — apply_shell_prelude returns the command unchanged when
        # SHELL_EXECUTABLE isn't zsh) rather than on every call the way the
        # stateless `bash` tool has to: a `setopt`/`unsetopt` made now stays
        # in effect for the rest of this shell's life. The same sendline
        # also serves as the initial sentinel round-trip, so the shell's
        # own startup banner/first prompt never leaks into command #1's
        # captured output.
        # PROMPT_COMMAND is armed with a defensive PS1-blanking hook here,
        # not unset — see the matching per-call reset in `_run()` below for
        # why (bash runs PROMPT_COMMAND immediately before printing each
        # prompt, which is what closes the PS1-leak race).
        primed = apply_shell_prelude("true")
        shell.sendline(
            "bind 'set enable-bracketed-paste off' 2>/dev/null; "
            "PROMPT_COMMAND='PS1=\"\"'; PS2=''\n"
            f"{primed}\nprintf '\\n{_sentinel}:%d\\n' $?"
        )
        shell.expect(_sentinel_pattern(), timeout=_DEFAULT_TIMEOUT)
    except Exception as e:
        try:
            shell.close(force=True)
        except Exception as close_error:
            print(
                f"[bash_session] cleanup after failed spawn also failed "
                f"(ignored): {close_error}"
            )
        return f"could not spawn persistent shell: {e}"

    _shell = shell
    return None


def _run(tool_input: dict) -> str:
    if tool_input.get("restart"):
        error = _spawn()
        if error:
            return json.dumps({"error": error})
        return json.dumps({"restarted": True})

    if _shell is None:
        error = _spawn()
        if error:
            return json.dumps({"error": error})

    # Set by `_spawn()` above, which returns an error string if it
    # couldn't — so this is unreachable in practice. Spelled out for mypy
    # rather than assumed, since every use below dereferences `_shell`
    # directly and a silent None here would be an AttributeError mid-
    # command instead of a clean error — same pattern/reasoning as
    # kernel.py's own equivalent check before its own globals.
    if _shell is None:
        return json.dumps({"error": "shell is not running"})

    command = tool_input.get("command", "")
    if not command.strip():
        return json.dumps({"error": "no command provided"})

    timeout = int(tool_input.get("timeout") or _DEFAULT_TIMEOUT)

    try:
        # Command + reset/sentinel are wrapped in ONE brace group, not two
        # separate top-level lines. Bash only runs PROMPT_COMMAND (prints
        # PS1) before reading a NEW top-level command; while a compound
        # construct (brace group, heredoc, for-loop) is still open, every
        # line is read via PS2 (blanked at spawn) and PROMPT_COMMAND is
        # never touched. Keeping the reset inside the same group means it
        # always runs before any real prompt can print — closes the leak
        # even for commands that reassign PROMPT_COMMAND itself (conda/
        # direnv), not just plain `venv`. A naive `;`-join (same idea, no
        # group) breaks on a trailing `#` comment or a heredoc; the group
        # doesn't. Doesn't fork, so cd/export/functions still persist; a
        # bare `exit` inside still kills the shell same as before (handled
        # by the `pexpect.EOF` branch below).
        #
        # `$?` is captured into a variable first, before the reset
        # assignments run (each has its own exit status). Variable name is
        # tied to the per-spawn sentinel to avoid colliding with the
        # command's own variables.
        _shell.sendline(
            "{\n"
            f"{command}\n"
            f"__rc_{_sentinel}=$?; PROMPT_COMMAND='PS1=\"\"'; PS2=''; "
            f"printf '\\n{_sentinel}:%d\\n' $__rc_{_sentinel}\n"
            "}"
        )
        _shell.expect(_sentinel_pattern(), timeout=timeout)
    except pexpect.TIMEOUT:
        return json.dumps(_handle_timeout())
    except pexpect.EOF:
        partial = _clean(_shell.before or "")
        _shutdown_sync()
        return json.dumps(
            {
                "output": clip(partial, _MAX_OUTPUT),
                "error": "the persistent shell exited unexpectedly; it will "
                "respawn fresh on the next call",
            }
        )

    output = _clean(_shell.before or "")
    return_code = int(_shell.match.group(1))
    return json.dumps(
        {
            "output": clip(output, _MAX_OUTPUT) or "(no output)",
            "return_code": return_code,
        }
    )


def _clean(text: str) -> str:
    """Strip ANSI/OSC escapes and normalize CRLF -> LF."""
    return _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "")


def _handle_timeout() -> dict:
    """A command blew past its timeout. Try Ctrl-C, then re-sync on the
    sentinel so a SUBSEQUENT call isn't necessarily also stuck behind the
    same hung command — report whether that recovery attempt worked.
    """
    # Only ever called from `_run()`'s `except pexpect.TIMEOUT:` handler,
    # by which point `_shell` is already known non-None there — but this
    # function reads the global fresh in its OWN scope, so that narrowing
    # doesn't carry over. Spelled out here too, same reasoning as `_run()`.
    assert _shell is not None
    partial = _clean(_shell.before or "")
    try:
        _shell.sendcontrol("c")
        # Same brace-group reset as the main path above, for consistency —
        # a command interrupted mid-way could just as easily have already
        # changed PROMPT_COMMAND before the Ctrl-C landed, and Ctrl-C alone
        # doesn't retroactively undo that.
        _shell.sendline(
            "{\n"
            f"PROMPT_COMMAND='PS1=\"\"'; PS2=''; "
            f"printf '\\n{_sentinel}:130\\n'\n"
            "}"
        )
        _shell.expect(_sentinel_pattern(), timeout=_RECOVERY_TIMEOUT)
        recovered = True
    except Exception:
        recovered = False
    return {
        "output": clip(partial, _MAX_OUTPUT),
        "timed_out": True,
        "recovered": recovered,
        "return_code": None,
    }


def _shutdown_sync():
    global _shell, _sentinel
    if _shell is not None:
        try:
            _shell.close(force=True)
        except Exception as e:
            print(f"[bash_session] shell close on shutdown failed (ignored): {e}")
    _shell = None
    _sentinel = None


async def shutdown():
    """Kill the persistent shell. Safe to call even if it was never started."""
    if _shell is None:
        return
    await asyncio.to_thread(_shutdown_sync)
