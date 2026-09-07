import asyncio
import json

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style

from core import listen, speak
from core.chat import Chat


class CliApp:
    def __init__(self, agent: Chat):
        self.agent = agent

        # Phase 1 of the REPL-level voice work (see speak_listen_tool_
        # integration_plan.md in /memories). Off by default: this only
        # controls whether MY reply also gets spoken via the `speak` tool's
        # own local `_run` helper; it has no bearing on whether `speak`/
        # `listen` are reachable as Claude-invoked tools at all (that's
        # config.toml's own `[speak].enabled`).
        self.auto_speak = False

        # Phase 2 of the REPL-level voice work: `/listen` (see below)
        # transcribes speech and stages it here rather than auto-submitting
        # it — the NEXT prompt_async call opens pre-filled with this text
        # (via its own `default=` param) so there's a review/edit step
        # before it's actually sent. Reset to "" the instant it's consumed,
        # whether kept, edited, or ignored, so it never leaks into a later
        # turn.
        self._next_default = ""

        self.history = InMemoryHistory()
        self.session: PromptSession[str] = PromptSession(
            history=self.history,
            style=Style.from_dict({"prompt": "#aaaaaa"}),
        )

    async def run(self):
        while True:
            try:
                user_input = await self.session.prompt_async(
                    "> ", default=self._next_default
                )
                self._next_default = ""
                if not user_input.strip():
                    continue

                text = user_input.strip()

                # `/clear` is the recovery path from a history the API will no
                # longer accept — an unanswered tool_use block, or a
                # conversation past the context window. Both persist for the
                # life of the process, so without this the only way out is
                # killing the app, taking the browser page, the kernel and
                # every MCP connection with it.
                if text in ("/clear", "/reset"):
                    print(self.agent.clear())
                    continue

                # Toggle for whether my reply also gets spoken aloud, on top
                # of always being printed as text (never a replacement for
                # it — see the "dual input-output modality without losing
                # context" reasoning in speak_listen_tool_integration_plan.md
                # in /memories). Reuses speak.py's own `_run` rather than
                # re-implementing synthesis/playback here.
                if text.startswith("/voice"):
                    arg = text[len("/voice"):].strip().lower()
                    if arg in ("on", "true", "1"):
                        self.auto_speak = True
                    elif arg in ("off", "false", "0"):
                        self.auto_speak = False
                    elif arg:
                        print(f"[voice: unrecognized arg {arg!r} — use /voice on|off]")
                        continue
                    print(f"[voice: {'on' if self.auto_speak else 'off'}]")
                    continue

                # Dictation: record+transcribe via listen.py's own `_run`
                # (same shared-helper reuse as `/voice` above), then STAGE
                # the transcript as the next prompt's pre-filled text rather
                # than sending it immediately — you review/edit it like any
                # normal typed input, then press Enter yourself. Optional
                # `/listen <N>` overrides [listen]'s configured duration for
                # just this one call.
                if text.startswith("/listen"):
                    arg = text[len("/listen"):].strip()
                    tool_input = {}
                    if arg:
                        try:
                            tool_input["duration_seconds"] = int(arg)
                        except ValueError:
                            print(
                                f"[listen: bad duration {arg!r} — expected "
                                "an integer number of seconds]"
                            )
                            continue
                    print("[listening... speak now]")
                    result = json.loads(
                        await asyncio.to_thread(listen._run, tool_input)
                    )
                    if result.get("status") == "ok":
                        transcript = result["transcript"]
                        print(f"[dictated: {transcript!r}]")
                        self._next_default = transcript
                    else:
                        print(
                            f"[listen: {result.get('status')} — "
                            f"{result.get('reason', result.get('error', ''))}]"
                        )
                    continue

                thinking = False
                if text.startswith("/think "):
                    text = text[len("/think "):]
                    thinking = True

                response = await self.agent.run(text, thinking=thinking)
                print(f"\nResponse:\n{response}")

                if self.auto_speak and response:
                    # Off the event loop thread, same as every other local
                    # tool call — speak.py's _run does blocking subprocess
                    # I/O (piper synthesis, then paplay playback).
                    result = json.loads(
                        await asyncio.to_thread(speak._run, {"text": response})
                    )
                    if result.get("status") != "ok":
                        print(
                            f"[voice: {result.get('status')} — "
                            f"{result.get('reason', result.get('error', ''))}]"
                        )

            except KeyboardInterrupt:
                break
            except Exception as e:
                # Chat.run() now resolves any pending tool_use blocks before
                # returning or raising (see core/chat.py), so self.messages
                # stays valid even after a bad turn — safe to report the
                # error and keep prompting instead of taking the whole
                # session down for what may be a single tool's failure.
                print(f"\n[error: {e}]")
