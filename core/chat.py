import os
from pathlib import Path

from anthropic.types import MessageParam

from core import local_tools
from core.claude import Claude
from core.claude_learned_schemas import SH_TARGET, SHELL_EXECUTABLE
from core.tools import ToolManager
from mcp_client import MCPClient

# Name of the interpreter the `bash` tool runs commands through (e.g. "bash",
# "zsh", "dash"), taken from SHELL_EXECUTABLE and interpolated into
# SYSTEM_PROMPT so Claude knows which shell dialect it writes for. zsh splits
# unquoted variables differently from bash and dash.
_SHELL_EXECUTABLE_NAME = Path(SHELL_EXECUTABLE).name

# Name of what /bin/sh points to, taken from SH_TARGET
# (core/claude_learned_schemas.py), which is checked at every process start.
# Interpolated into SYSTEM_PROMPT.
_SH_NAME = Path(SH_TARGET).name if SH_TARGET.startswith("/") else SH_TARGET

# Cap on chat-loop rounds per user message. A round can batch several tool_use
# calls, and the count resets with every user message. 200 leaves room for a
# long turn without cutting it off on round count alone.
MAX_TOOL_ITERATIONS = 200

# Extra rounds for continuations the API requires: an open pause_turn, or a
# server_tool_use left dangling by a mixed tool_use response.
# MAX_TOOL_ITERATIONS alone must not block these. 5 is the default
# `max_continuations` in Anthropic's reference example. See Chat._finalize_turn
# for what happens when this runs out.
EXTRA_CONTINUATION_LIMIT = 5

# Set CLAUDE_SHOW_USAGE=1 to print token and cache counters per request. A
# cache miss raises no error, so this is how to check that the cache_control
# breakpoint in core/claude.py pays off.
SHOW_USAGE = os.getenv("CLAUDE_SHOW_USAGE") == "1"

# Sent as the `system` parameter on every request. States what the tool schemas
# cannot: facts about this environment (for example, there is no sandboxed
# code-execution container) and how to choose between overlapping tools.
SYSTEM_PROMPT = f"""\
You are the assistant in a command-line research client running on the user's own Linux
machine. What follows describes your actual environment.

These 27 tools are the ones built into this client: bash, str_replace_based_edit_tool,
web_search, web_fetch, memory, computer, desktop_window, screen_find, browser_navigate,
browser_extract, browser_click, browser_fill, browser_links, browser_back, browser_tab,
document_convert, python, bash_session, interactive_run, config_edit, sql_query, trash,
text_embeddings, vision_query, speak, listen, midi1.
Any other tool in your list comes from a connected MCP server and runs on that server — those
are real; use them. But if you are about to name a tool that is in neither group, you are
mistaken.

Of the built-in 27, only `web_search` and `web_fetch` run on Anthropic's servers.
Everything else runs locally, in this user's own account — including the browser, which runs
on this machine (headless Chromium by default), so pages are fetched from the user's own
network.

There is no sandbox and no code-execution container, and there are no `code_execution`,
`bash_code_execution`, or `text_editor_code_execution` definitions in your tool list. The
2026 `web_search`/`web_fetch` variants do filter their results using server-side code
execution internally, which is likely why those names feel available — but that is
machinery inside those two tools, not something you can call. `bash` and `python` run as
the user, with their permissions, their filesystem, and their network. Nothing you run is
isolated or automatically reversible, so treat destructive actions as real.

State between calls:
- `python` is a persistent IPython kernel: variables, imports, and loaded data survive
  across calls. Load data once and keep working with it.
- `bash` is a fresh subprocess every call. `cd`, exported variables, and activated
  virtualenvs do not carry over; chain with `&&` in a single call instead.
- `bash` actually runs commands through **{_SHELL_EXECUTABLE_NAME}** ({SHELL_EXECUTABLE})
  — not necessarily bash despite the tool's name; configurable via config.toml's
  `[bash].shell`. If it's `zsh`, a prelude already neutralizes the two behavioral
  gotchas that would otherwise matter (unquoted `$var` word-splitting, and an
  unmatched glob hard-erroring instead of passing through literally), so ordinary
  bash/POSIX syntax is safe as written — the one real difference left is that zsh
  array indices are 1-based instead of 0-based; everything else, including
  `[[ ]]`/`$(...)`/`&&`/`||`, is identical to bash. (Separately, `/bin/sh` on this
  machine resolves to **{_SH_NAME}** ({SH_TARGET}) — relevant only if you ever write
  a standalone `#!/bin/sh` script rather than running an inline command.)
- `bash_session` is the stateful alternative to `bash`: one real shell that survives
  across calls, so `cd`, exported variables, sourced venvs, and background jobs all
  persist. Use it instead of `bash` for anything that needs that; use plain `bash` for
  one-off commands. A foreground program that blocks on its own input (a password
  prompt, `vim`, `less`, a REPL) still hangs there for the call's timeout — `restart:
  true` gives a clean shell if one ever gets stuck.
- The browser holds one live session, and `sql_query` one DuckDB connection, for the session.
- `memory` is the only state that outlives this process. Everything above is gone when the
  session ends; files under `/memories` are still there next time.

Choosing between overlapping tools:
- Deleting: use `trash`, which is recoverable, rather than `rm`.
- YAML/TOML/JSON config files: use `config_edit`. It preserves comments and key order;
  the file editor and `sed` silently destroy them.
- Commands that prompt for input: use `interactive_run`. `bash` has no stdin and hangs.
- Reading the web: `web_fetch` reads one known document. `browser_navigate` is for anything
  that needs rendering, links, forms or a login: follow links with `browser_links` and
  `browser_back`; a click that opens a tab switches to it, and `browser_tab` lists,
  switches and closes tabs. It starts headless. When a Cloudflare human check stops a
  fresh visit it reopens itself in `virtual` mode (a hidden display); if the report still
  says `Human check: pending`, navigate again with `mode: real` (the user's installed
  Chrome in a visible window they can click in) or ask the user to click it. A `profile`
  name keeps logins between sessions. Files the browser downloads land in ~/Downloads.
- Querying a CSV, Parquet, or JSON file: `sql_query` reads it in place, no import step.
- Vector embeddings: there is no Anthropic-hosted embeddings endpoint, so use
  `text_embeddings` — it calls the user's own private embedding server, configured under
  `[embeddings]` in config.toml. It errors with a clear message (rather than silently doing
  nothing) if no `url` is set there yet.
- Asking a question about an image without spending an Anthropic API vision call: use
  `vision_query` — it calls the user's own private vision-capable chat server, configured
  under `[vision]` in config.toml. If that server is unset or unreachable it returns a
  `local_unavailable` status and stops rather than silently falling back — tell the user
  and get explicit confirmation before using your own vision on the image instead, since
  that means sending it to Anthropic's API rather than keeping it on their local/private
  compute.
- Producing a document: write markdown with the file editor, then `document_convert` it.
  From markdown the targets are pdf, docx, odt, html, epub, rtf, and txt — xlsx and pptx
  are reachable only from another office format, not from markdown.
- The file editor is text-only (UTF-8) apart from .png/.jpg/.jpeg, which it returns as an
  image. It cannot view PDFs or other binary files; it will return a decoding error. Use
  `bash` to inspect those.
- Anything scriptable: prefer `bash`, `python`, or the browser over `computer`. `computer`
  drives the real desktop by moving the pointer and synthesising keystrokes, so it is slow,
  it returns a screenshot per action, and it competes with the user for their own mouse and
  keyboard. Reach for it only when there is no other way in — a GUI-only application, or
  something you must see rendered on their actual screen. Put the right window in front with
  `desktop_window` before typing (typing goes to whichever window has focus; KDE Plasma only), and
  get a button's click position from `screen_find` (OCR, accurate) instead of estimating it
  from a screenshot.
- `memory` writes to a private `/memories` store, not to the user's project files. Notes
  meant for you later go there; files the user asked for go on the real filesystem.

Report what actually happened. If a command failed, say so and include its output. If you
haven't verified something, say that rather than implying you have.

`interactive_run` and a password or token prompt: never answer it with a plain `send`
field, and never ask the user to type the real value into this conversation, under any
circumstance. This applies to every call that could touch a secret, including ones that
look trivial (`sudo whoami`) exactly the same as ones that look consequential
(`sudo apt upgrade`) — there is no size of command where typing a real password into a
`send` field or into the chat becomes acceptable. Use a step's `send_env` (an environment
variable, named only) or `send_secret` (a `pass` entry, named only) instead — the real
value is resolved locally and never has to appear in this conversation at all.

The same rule applies to `browser_fill` on a password or long-lived token field: never put the
real value in `value`. Use `value_secret` (a `pass` entry name), with the same entry
check and prompt shape below.

A one-time code (from an authenticator app, SMS or email) is not a vault secret: it
expires in seconds and is useless afterwards. The user may paste it into the chat, and
when they do, type it at once with `browser_fill` `value` and `submit: true`. Never ask
the user to type it into the browser themselves; they may be unable to use a keyboard
or mouse, which is what you are here to cover. If the site says the code did not
verify, ask for a fresh one and fill it at once, with nothing else in between. The
vault rules above are for passwords and other long-lived secrets.

Before asking the user to name a `send_secret` entry, check what actually exists first:
run `pass ls` yourself (via `bash` — it lists entry names only, decrypts nothing, needs no
passphrase). Then say exactly this shape, nothing more elaborate:

please select the cred name I need to use:
<one name per line, exactly what `pass ls` printed>

Do not wrap this in a longer explanation, do not mention `pass` as a vague, hypothetical
option ("if you use pass, tell me the entry name") without having checked, and do not add
reasoning about why you're asking — the short prompt above, with the real names from
`pass ls`, is the complete response. If `pass ls` shows nothing, or `pass` is not
installed at all, say that plainly and offer `send_env` instead, or walk through the
one-time `pass` setup — do not fall back to asking for the raw value just because nothing
is configured yet. Never pick an entry yourself from that list, no matter how obvious a
name looks — the user names the exact entry for every real task, every time.
"""


def _block_field(block, name: str):
    """Read a field from a content block that may be an SDK object or a dict.

    Assistant turns hold SDK objects; tool_result turns built here are plain
    dicts.
    """
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def _orphaned_tool_uses(messages) -> list[str]:
    """tool_use ids that never got a result block.

    The API requires each tool_use to be answered in the next message; an
    unanswered one fails every later request. Covers client `tool_use`
    (answered by `tool_result`) and server-flavored
    `server_tool_use`/`mcp_tool_use` (answered by a type-specific result
    block). Pairs by id and matches by the `_tool_use`/`_tool_result` suffix,
    so new server tools need no edit here.
    """
    answered: set[str] = set()
    issued: list[str] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            kind = _block_field(block, "type")
            if not kind:
                continue
            if kind == "tool_use" or kind.endswith("_tool_use"):
                block_id = _block_field(block, "id")
                if block_id:
                    issued.append(block_id)
            elif kind == "tool_result" or kind.endswith("_tool_result"):
                used = _block_field(block, "tool_use_id")
                if used:
                    answered.add(used)
    return [i for i in issued if i not in answered]


def _classify_orphans(messages) -> tuple[list[str], list[str]]:
    """Split `_orphaned_tool_uses` into (client_ids, server_ids).

    A client `tool_use` can be closed with a synthetic error `tool_result`. A
    server-flavored block cannot: its result block was never ours to build.
    """
    ids = _orphaned_tool_uses(messages)
    if not ids:
        return [], []
    orphan_set = set(ids)
    client_ids: list[str] = []
    server_ids: list[str] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            block_id = _block_field(block, "id")
            if block_id not in orphan_set:
                continue
            kind = _block_field(block, "type")
            if kind == "tool_use":
                client_ids.append(block_id)
            elif kind and kind.endswith("_tool_use"):
                server_ids.append(block_id)
    return client_ids, server_ids


def _duplicate_tool_result_ids(messages) -> dict[str, int]:
    """tool_use ids answered by more than one tool_result-family block, as {id:
    count}.

    The API rejects these: `each tool_use must have a single result`.
    """
    counts: dict[str, int] = {}
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            kind = _block_field(block, "type")
            if not kind:
                continue
            if kind == "tool_result" or kind.endswith("_tool_result"):
                used = _block_field(block, "tool_use_id")
                if used:
                    counts[used] = counts.get(used, 0) + 1
    return {k: v for k, v in counts.items() if v > 1}


def _dedupe_duplicate_tool_results(messages) -> int:
    """Remove duplicate tool_result blocks in place, keeping the first per id.
    Returns the count removed.

    Drops blocks, never messages.
    """
    dup_counts = _duplicate_tool_result_ids(messages)
    if not dup_counts:
        return 0
    seen: set[str] = set()
    removed = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        kept = []
        for block in content:
            kind = _block_field(block, "type")
            is_result = bool(kind) and (kind == "tool_result" or kind.endswith("_tool_result"))
            used = _block_field(block, "tool_use_id") if is_result else None
            if is_result and used in dup_counts:
                if used in seen:
                    removed += 1
                    continue
                seen.add(used)
            kept.append(block)
        message["content"] = kept
    return removed


def _excise_dangling_blocks(messages, ids: set[str]) -> int:
    """Remove blocks whose `id` is in `ids`, in place. Returns the count
    removed.

    Used for server-flavored orphans, which no synthetic result can satisfy.
    The message holding a block is kept.
    """
    if not ids:
        return 0
    removed = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        kept = []
        for block in content:
            block_id = _block_field(block, "id")
            if block_id in ids:
                removed += 1
                continue
            kept.append(block)
        message["content"] = kept
    return removed


def _answer_orphaned_client_tool_uses(
    messages, client_ids: list[str], content: str
) -> None:
    """Answer each id in `client_ids` with a synthetic error `tool_result`, in
    place.

    The result goes in the message immediately after the one holding the
    tool_use, which is what the API checks; appending to the end is wrong once
    anything follows. A following `user` message gets the results merged at the
    front of its content; otherwise a new message is inserted. Messages are
    processed back to front so insertions do not shift later indexes.
    """
    if not client_ids:
        return
    orphan_set = set(client_ids)
    by_index: dict[int, list[str]] = {}
    for idx, message in enumerate(messages):
        content_list = message.get("content")
        if not isinstance(content_list, list):
            continue
        for block in content_list:
            if _block_field(block, "type") != "tool_use":
                continue
            block_id = _block_field(block, "id")
            if block_id in orphan_set:
                by_index.setdefault(idx, []).append(block_id)

    for idx in sorted(by_index, reverse=True):
        results = [
            {
                "type": "tool_result",
                "tool_use_id": i,
                "content": content,
                "is_error": True,
            }
            for i in by_index[idx]
        ]
        next_idx = idx + 1
        if next_idx < len(messages) and messages[next_idx].get("role") == "user":
            existing = messages[next_idx].get("content")
            if isinstance(existing, str):
                existing = [{"type": "text", "text": existing}]
            elif not isinstance(existing, list):
                existing = []
            messages[next_idx]["content"] = results + existing
        else:
            messages.insert(next_idx, {"role": "user", "content": results})


def _approx_size(messages) -> tuple[int, int]:
    """(message count, character count) for the conversation.

    Characters, not tokens: `count_tokens` rejects the server tools
    (`web_search`, `web_fetch`) this conversation contains.
    """
    chars = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    chars += len(str(block.get("content") or block.get("text") or ""))
                else:
                    chars += len(str(getattr(block, "text", "") or ""))
    return len(messages), chars


def _report_usage(response) -> None:
    """One line of token accounting. From the second request onward, cache read
    should be large and cache write near zero — that means the prefix is being
    reused. Cache read staying at 0 means the breakpoint isn't landing."""
    usage = response.usage
    print(
        "[usage: input {} | cache write {} | cache read {} | output {}]".format(
            usage.input_tokens,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
            usage.output_tokens,
        )
    )


def _local_result_to_content(local):
    """Local tool executors return a string, or the image marker from
    core.output.image_result (file `view` on an image, every computer
    screenshot), which becomes a tool_result content list with an `image`
    block.

    Worker results are built the same way in core/tools.py `_call_one`.
    """
    if isinstance(local, dict) and local.get("__kind__") == "image":
        return [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": local["media_type"],
                    "data": local["data"],
                },
            },
            {"type": "text", "text": local["text"]},
        ]
    return local


class Chat:
    def __init__(self, claude_service: Claude, clients: dict[str, MCPClient]):
        self.claude_service: Claude = claude_service
        self.clients: dict[str, MCPClient] = clients
        self.messages: list[MessageParam] = []

    def clear(self) -> str:
        """`/clear`: drop the conversation, keep the process and its MCP
        connections.

        `self.messages` lives for the life of the process, so an unanswered
        tool_use block, or a conversation that has outgrown the context window,
        makes every later turn fail the same way. `/clear` is the recovery
        path. It leaves `self.clients`, the kernel, the browser and `/memories`
        alone, since none of them is why the history is unusable.
        """
        count, chars = _approx_size(self.messages)
        orphans = _orphaned_tool_uses(self.messages)
        self.messages = []
        detail = f"cleared {count} messages (~{chars:,} chars)"
        if orphans:
            detail += (
                f" — including {len(orphans)} unanswered tool_use block"
                f"{'s' if len(orphans) != 1 else ''}, which is what was "
                f"breaking every turn"
            )
        return f"[{detail}]"

    def _report_api_failure(
        self, error: Exception, repair_attempted: str | None = None
    ) -> None:
        """Say which failure this is.

        `_call_chat_with_auto_repair` has already tried the mechanical repair
        and retried once; `repair_attempted` is what it found. This reports
        facts (error, size, remaining orphans) and never recommends `/clear` or
        any step that discards conversation.
        """
        text = str(error)
        count, chars = _approx_size(self.messages)
        orphans = _orphaned_tool_uses(self.messages)

        print(f"[api error] {text}")
        print(f"[api error] conversation: {count} messages, ~{chars:,} chars")

        if repair_attempted:
            print(
                f"[api error] an automatic repair ran first ({repair_attempted}), "
                "but the retried request still failed — this is a different, "
                "unrelated problem."
            )

        if orphans:
            print(
                f"[api error] {len(orphans)} unanswered tool_use block(s) "
                f"still present after the repair attempt: "
                f"{', '.join(orphans[:3])}"
                f"{' …' if len(orphans) > 3 else ''}"
            )
        elif "too long" in text.lower() or "context" in text.lower():
            print(
                "[api error] the conversation has outgrown the context window."
            )

    async def _run_tool_uses(self, message) -> list:
        """Route each tool_use block to a local executor or the MCP
        ToolManager.

        Every tool_use block owes the API a tool_result in the next message, so
        a local executor that raises becomes an error tool_result and does not
        abort the batch, as ToolManager.execute_blocks does for MCP tools.
        """
        blocks = [b for b in message.content if b.type == "tool_use"]
        results: list = []
        mcp_blocks: list = []

        for block in blocks:
            # For a computer-toolset member, `block.toolset_name` is "computer"
            # (None for any other tool_use). The paired tool_result must echo
            # it or the API rejects the batch.
            toolset_name = getattr(block, "toolset_name", None)

            try:
                local = await local_tools.execute(block.name, block.input)
            except Exception as e:
                print(f"[local tool '{block.name}' raised: {e}]")
                error_result = {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Error executing tool '{block.name}': {e}",
                    "is_error": True,
                }
                if toolset_name is not None:
                    error_result["toolset_name"] = toolset_name
                results.append(error_result)
                continue

            if local is not None:
                ok_result = {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": _local_result_to_content(local),
                }
                if toolset_name is not None:
                    ok_result["toolset_name"] = toolset_name
                results.append(ok_result)
            else:
                mcp_blocks.append(block)

        if mcp_blocks:
            results.extend(
                await ToolManager.execute_blocks(self.clients, mcp_blocks)
            )
        return results

    def _finalize_turn(self, reason: str) -> str:
        """Close out a turn that ends abnormally.

        Re-scans `self.messages` for what is still dangling. Never deletes a
        turn or message: a client `tool_use` gets a synthetic error
        `tool_result`, a server-flavored block is removed. Returns the text to
        show the user.
        """
        client_ids, server_ids = _classify_orphans(self.messages)
        if client_ids:
            _answer_orphaned_client_tool_uses(
                self.messages, client_ids, f"[{reason}]"
            )
        base = f"[{reason}]"
        if server_ids:
            _excise_dangling_blocks(self.messages, set(server_ids))
            base += (
                f" {len(server_ids)} background tool call"
                f"{'s' if len(server_ids) != 1 else ''} that never finished "
                f"{'were' if len(server_ids) != 1 else 'was'} removed so the "
                "conversation can continue — nothing else was touched."
            )
        return base

    def _auto_repair_poisoned_history(self) -> str | None:
        """Repair `self.messages` after a failed `chat()`: dedupe tool_results,
        answer orphaned client tool_uses, remove orphaned server-flavored
        blocks.

        Touches only the offending blocks. Returns a short description, or None
        if there was nothing to fix.
        """
        repairs: list[str] = []

        removed_dupes = _dedupe_duplicate_tool_results(self.messages)
        if removed_dupes:
            repairs.append(
                f"removed {removed_dupes} duplicate tool_result block"
                f"{'s' if removed_dupes != 1 else ''}"
            )

        client_ids, server_ids = _classify_orphans(self.messages)
        if client_ids:
            _answer_orphaned_client_tool_uses(
                self.messages,
                client_ids,
                "[repaired: this tool_use was never answered]",
            )
            repairs.append(
                f"answered {len(client_ids)} orphaned tool_use block"
                f"{'s' if len(client_ids) != 1 else ''}"
            )
        if server_ids:
            _excise_dangling_blocks(self.messages, set(server_ids))
            repairs.append(
                f"removed {len(server_ids)} dangling background tool block"
                f"{'s' if len(server_ids) != 1 else ''} (no synthetic fix "
                f"exists for these)"
            )

        return "; ".join(repairs) if repairs else None

    def _call_chat_with_auto_repair(self, tool_defs, thinking):
        """The one real `chat()` call site: on failure, try
        `_auto_repair_poisoned_history` and retry once. Returns the
        response on success (either attempt), or None if both raised
        (`_report_api_failure` already called in that case).
        """
        try:
            return self.claude_service.chat(
                messages=self.messages,
                system=SYSTEM_PROMPT,
                tools=tool_defs,
                thinking=thinking,
            )
        except Exception as e:
            repair = self._auto_repair_poisoned_history()
            if repair is None:
                self._report_api_failure(e)
                return None
            print(f"[api error] {e}")
            print(f"[api error] auto-repaired the conversation history: {repair}")
            print("[api error] retrying this request once...")
            try:
                return self.claude_service.chat(
                    messages=self.messages,
                    system=SYSTEM_PROMPT,
                    tools=tool_defs,
                    thinking=thinking,
                )
            except Exception as e2:
                self._report_api_failure(e2, repair_attempted=repair)
                return None

    async def run(self, query: str, thinking: bool=False) -> str:
        final_text_response = ""
        self.claude_service.add_user_message(self.messages, query)

        # Fetched once per turn, not per iteration -- can't change mid-turn.
        mcp_tools = await ToolManager.get_all_tools(self.clients)
        tool_defs = local_tools.TOOLS + mcp_tools

        # Index of this turn's own assistant message while a pause_turn
        # continuation is open. Each continuation replaces this slot instead of
        # appending a sibling message, as Anthropic's reference implementation
        # does.
        pending_pause_turn_idx: int | None = None

        iterations = 0
        extra_continuations = 0
        # Set only when the next chat() call is required by the API: an open
        # pause_turn, or a server_tool_use left dangling by a mixed tool_use
        # response. Reset every pass so the grace budget is not spent on an
        # ordinary continuation.
        mandatory_continuation = False
        while True:
            if iterations >= MAX_TOOL_ITERATIONS:
                if not mandatory_continuation:
                    final_text_response = "[stopped: exceeded tool-iteration limit]"
                    break
                if extra_continuations >= EXTRA_CONTINUATION_LIMIT:
                    final_text_response = self._finalize_turn(
                        "stopped: exceeded tool-iteration limit"
                    )
                    break
                extra_continuations += 1
            else:
                iterations += 1
            mandatory_continuation = False

            response = self._call_chat_with_auto_repair(tool_defs, thinking)
            if response is None:
                return "[api error: chat request failed]"
            if SHOW_USAGE:
                _report_usage(response)
            if thinking:
                thought = [b for b in response.content if b.type == "thinking"]
                print(f"[thinking blocks: {len(thought)}]")

            if pending_pause_turn_idx is not None:
                self.messages[pending_pause_turn_idx]["content"] = response.content
            else:
                self.claude_service.add_assistant_message(self.messages, response)

            if response.stop_reason == "pause_turn":
                if pending_pause_turn_idx is None:
                    pending_pause_turn_idx = len(self.messages) - 1
                mandatory_continuation = True
                continue

            pending_pause_turn_idx = None

            if response.stop_reason == "tool_use":
                print(self.claude_service.text_from_message(response))
                try:
                    tool_result_parts = await self._run_tool_uses(response)
                except Exception as e:
                    # Genuinely unanswered -- first time seeing these blocks,
                    # not a re-resolution.
                    print(f"[tool routing error: {e}]")
                    final_text_response = self._finalize_turn(
                        f"tool execution failed: {e}"
                    )
                    break
                self.claude_service.add_user_message(
                    self.messages, tool_result_parts
                )

                # A dangling server_tool_use here means the API owes us one
                # more mandatory round trip to resolve it (Server tools doc).
                _, server_ids = _classify_orphans(self.messages)
                if server_ids:
                    mandatory_continuation = True
                    continue

                if iterations >= MAX_TOOL_ITERATIONS:
                    final_text_response = "[stopped: exceeded tool-iteration limit]"
                    break
                continue

            # Anything else (end_turn, stop_sequence, max_tokens) falls through
            # here. A max_tokens cutoff in the middle of a tool_use (for
            # example a large `create` call) is appended above like any
            # assistant turn, but stop_reason is "max_tokens", so nothing above
            # answered it; left unresolved, that tool_use poisons every later
            # turn. Re-check the live message list instead of trusting
            # stop_reason alone, and finalize.
            if _orphaned_tool_uses(self.messages):
                final_text_response = self._finalize_turn(
                    f"stopped: response ended early (stop_reason="
                    f"{response.stop_reason!r}) with an unresolved tool_use"
                )
                break

            final_text_response = self.claude_service.text_from_message(response)
            break

        return final_text_response
