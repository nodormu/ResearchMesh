import asyncio
import json

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style

from core import listen, processes, speak
from core.chat import Chat
from core.claude import load_claude_models, resolve_model_swap


class CliApp:
    def __init__(self, agent: Chat):
        self.agent = agent

        # Off by default. Controls only whether replies are also spoken through
        # the `speak` tool's `_run`; it does not affect whether `speak` and
        # `listen` are reachable as tools (`[speak].enabled` does that).
        self.auto_speak = False

        self.history = InMemoryHistory()
        self.session: PromptSession[str] = PromptSession(
            history=self.history,
            style=Style.from_dict({"prompt": "#aaaaaa"}),
        )

    async def _submit(self, text: str):
        """Send `text` to the agent as one turn, print the reply, and speak it
        if `/voice` (auto_speak) is on. Shared by typed input and a finished
        `/listen` dictation: auto_speak only gates speaking the reply, never
        sending the input.
        """
        thinking = False
        if text.startswith("/think "):
            text = text[len("/think "):]
            thinking = True

        # Only the user's own typed or dictated text confirms a vault entry; a
        # task delegated over MCP reaches Chat.run without passing through here.
        processes.note_user_message(text)
        response = await self.agent.run(text, thinking=thinking)
        print(f"\nResponse:\n{response}")

        if self.auto_speak and response:
            # Off the event loop thread, same as every other local tool
            # call — speak.py's _run does blocking subprocess I/O (piper
            # synthesis, then paplay playback).
            result = json.loads(
                await asyncio.to_thread(speak._run, {"text": response})
            )
            if result.get("status") != "ok":
                print(
                    f"[voice: {result.get('status')} — "
                    f"{result.get('reason', result.get('error', ''))}]"
                )

    async def run(self):
        while True:
            try:
                user_input = await self.session.prompt_async("> ")
                if not user_input.strip():
                    continue

                text = user_input.strip()

                # `/clear` is the recovery path from a history the API will not
                # accept: an unanswered tool_use block, or a conversation past
                # the context window. Both last for the life of the process, so
                # without it the only way out is killing the app, which takes
                # the browser, the kernel and every MCP connection with it.
                if text in ("/clear", "/reset"):
                    print(self.agent.clear())
                    continue

                # Toggle whether replies are also spoken, in addition to being
                # printed. Reuses speak.py's `_run` instead of re-implementing
                # synthesis and playback.
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

                # Dictation: record and transcribe with listen.py's `_run`
                # (shared with `/voice`), then auto-submit the transcript as a
                # turn through `_submit`, whether `/voice` is on or off. There
                # is no stage-and-edit step. `/listen <N>` overrides [listen]'s
                # configured duration for that call.
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
                        await self._submit(transcript)
                    else:
                        print(
                            f"[listen: {result.get('status')} — "
                            f"{result.get('reason', result.get('error', ''))}]"
                        )
                    continue

                # /model lists config.toml's claude_models (re-read on each
                # call by core/claude.py's load_claude_models, so an edit shows
                # without a restart). /model swap <name/index> changes the
                # model for the session only and never writes config.toml, so a
                # new session starts on claude_models[0]. An invalid name or
                # index is rejected with the valid list.
                if text == "/model" or text.startswith("/model "):
                    rest = text[len("/model"):].strip()
                    parts = rest.split(None, 1)
                    sub = parts[0] if parts else ""
                    arg = parts[1].strip() if len(parts) > 1 else ""

                    try:
                        models = load_claude_models()
                    except ValueError as e:
                        print(f"[model: {e}]")
                        continue

                    if not sub:
                        current = self.agent.claude_service.model
                        lines = [
                            f"  {i}. {m}" + ("  (current)" if m == current else "")
                            for i, m in enumerate(models, start=1)
                        ]
                        print("[model: available]\n" + "\n".join(lines))
                        continue

                    if sub == "swap":
                        if not arg:
                            print("[usage: /model swap <name or index>]")
                            continue
                        chosen = resolve_model_swap(models, arg)
                        if chosen is None:
                            print(
                                f"[model: {arg!r} not recognized — "
                                "run /model to see the list]"
                            )
                            continue
                        self.agent.claude_service.model = chosen
                        print(f"[model: swapped to {chosen}]")
                        continue

                    print(
                        f"[model: unrecognized subcommand {sub!r} — "
                        "use /model or /model swap <name/index>]"
                    )
                    continue

                await self._submit(text)

            except KeyboardInterrupt:
                break
            except Exception as e:
                # Chat.run() resolves any pending tool_use blocks before it
                # returns or raises (core/chat.py), so self.messages stays
                # valid after a bad turn. Report the error and keep prompting
                # instead of ending the session over one tool's failure.
                print(f"\n[error: {e}]")
