import os

from core.claude import Claude
from mcp_client import MCPClient
from core.tools import ToolManager
from core import local_tools
from anthropic.types import MessageParam

MAX_TOOL_ITERATIONS = 30

# Set CLAUDE_SHOW_USAGE=1 to print token and cache counters per request. Prompt
# caching fails *silently* (a too-short prefix or a changed byte early in the
# prefix just means no hit, with no error), so this is the only way to confirm
# the cache_control breakpoint in core/claude.py is actually paying off.
SHOW_USAGE = os.getenv("CLAUDE_SHOW_USAGE") == "1"

# Sent as the `system` parameter on every request. Without it, Claude has nothing
# but the tool schemas to reason from and will describe capabilities it doesn't
# have (e.g. inventing a sandboxed code-execution container, which this app has
# no such thing as). Everything here is either a fact about this environment that
# Claude cannot infer, or a choice between genuinely overlapping tools.
SYSTEM_PROMPT = """\
You are the assistant in a command-line research client running on the user's own Linux
machine. What follows describes your actual environment.

These 16 tools are the ones built into this client: bash, str_replace_based_edit_tool,
web_search, web_fetch, browser_navigate, browser_extract, browser_click, browser_fill,
browser_links, browser_back, document_convert, python, interactive_run, config_edit,
sql_query, trash. Any other tool in your list comes from a connected MCP server and runs
on that server — those are real; use them. But if you are about to name a tool that is in
neither group, you are mistaken.

Of the built-in 16, only `web_search` and `web_fetch` run on Anthropic's servers.
Everything else runs locally, in this user's own account — including the browser, which is
a headless Chromium process on this machine, so pages are fetched from the user's own
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
- The browser holds one live page, and `sql_query` one DuckDB connection, for the session.

Choosing between overlapping tools:
- Deleting: use `trash`, which is recoverable, rather than `rm`.
- YAML/TOML/JSON config files: use `config_edit`. It preserves comments and key order;
  the file editor and `sed` silently destroy them.
- Commands that prompt for input: use `interactive_run`. `bash` has no stdin and hangs.
- Reading the web: `browser_navigate` is the primary way, since it renders JavaScript and
  `browser_links`/`browser_back` let you follow links. Use `web_fetch` for a single known
  document you don't need to interact with.
- Querying a CSV, Parquet, or JSON file: `sql_query` reads it in place, no import step.
- Producing a document: write markdown with the file editor, then `document_convert` it.
  From markdown the targets are pdf, docx, odt, html, epub, rtf, and txt — xlsx and pptx
  are reachable only from another office format, not from markdown.
- The file editor is text-only (UTF-8). It cannot view images, PDFs, or other binary
  files; it will return a decoding error. Use `bash` to inspect those.

Report what actually happened. If a command failed, say so and include its output. If you
haven't verified something, say that rather than implying you have.
"""


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
    """Local tool executors normally return a plain string. The text_editor
    'view' command can also return a dict marker for image files
    ({"__kind__": "image", ...}) which we translate into a real
    tool_result content list carrying an `image` block, so the model
    actually receives pixels instead of a UTF-8 decode error."""
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
            {"type": "text", "text": f"Displayed image: {local['path']}"},
        ]
    return local


class Chat:
    def __init__(self, claude_service: Claude, clients: dict[str, MCPClient]):
        self.claude_service: Claude = claude_service
        self.clients: dict[str, MCPClient] = clients
        self.messages: list[MessageParam] = []

    async def _run_tool_uses(self, message) -> list:
        """Route each tool_use block: local executor, or the MCP ToolManager."""
        blocks = [b for b in message.content if b.type == "tool_use"]
        results: list = []
        mcp_blocks: list = []

        for block in blocks:
            local = await local_tools.execute(block.name, block.input)
            if local is not None:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _local_result_to_content(local),
                    }
                )
            else:
                mcp_blocks.append(block)

        if mcp_blocks:
            results.extend(
                await ToolManager.execute_blocks(self.clients, mcp_blocks)
            )
        return results

    async def run(self, query: str, thinking: bool=False) -> str:
        final_text_response = ""
        self.claude_service.add_user_message(self.messages, query)

        # The MCP tool list is fetched once per user turn, not once per
        # tool-use iteration — it can't change mid-turn, and re-listing was a
        # round trip per client per loop pass (up to MAX_TOOL_ITERATIONS).
        mcp_tools = await ToolManager.get_all_tools(self.clients)
        tool_defs = local_tools.TOOLS + mcp_tools

        response = None
        iterations = 0
        while True:
            iterations += 1
            if iterations > MAX_TOOL_ITERATIONS:
                final_text_response = (
                    self.claude_service.text_from_message(response)
                    or "[stopped: exceeded tool-iteration limit]"
                )
                break

            response = self.claude_service.chat(
                messages=self.messages,
                system=SYSTEM_PROMPT,
                tools=tool_defs,
                thinking=thinking
            )
            if SHOW_USAGE:
                _report_usage(response)
            if thinking:
                thought = [b for b in response.content if b.type == "thinking"]
                print(f"[thinking blocks: {len(thought)}]")
            self.claude_service.add_assistant_message(self.messages, response)

            if response.stop_reason == "tool_use":
                print(self.claude_service.text_from_message(response))
                tool_result_parts = await self._run_tool_uses(response)
                self.claude_service.add_user_message(
                    self.messages, tool_result_parts
                )
            elif response.stop_reason == "pause_turn":
                # A server-side tool (web_search / web_fetch) paused mid-run;
                # resend the conversation so the server resumes it.
                continue
            else:
                final_text_response = self.claude_service.text_from_message(
                    response
                )
                break

        return final_text_response
