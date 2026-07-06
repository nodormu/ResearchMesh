from core.claude import Claude
from mcp_client import MCPClient
from core.tools import ToolManager
from core import claude_learned_schemas as learned
from core import browser
from anthropic.types import MessageParam

MAX_TOOL_ITERATIONS = 30


class Chat:
    def __init__(self, claude_service: Claude, clients: dict[str, MCPClient]):
        self.claude_service: Claude = claude_service
        self.clients: dict[str, MCPClient] = clients
        self.messages: list[MessageParam] = []

    async def _process_query(self, query: str):
        self.messages.append({"role": "user", "content": query})

    def _local_tool_defs(self) -> list:
        # Anthropic "learned" tools (bash/editor/web_search/web_fetch) + our
        # custom Playwright browser tools.
        return learned.TOOLS + browser.TOOLS

    async def _execute_local(self, name: str, tool_input) -> str | None:
        """Run a client-side tool we own; return None if it isn't ours."""
        if learned.handles(name):
            return await learned.execute(name, tool_input)
        if browser.handles(name):
            return await browser.execute(name, tool_input)
        return None

    async def _run_tool_uses(self, message) -> list:
        """Route each tool_use block: local executor, or the MCP ToolManager."""
        blocks = [b for b in message.content if b.type == "tool_use"]
        results: list = []
        mcp_blocks: list = []

        for block in blocks:
            local = await self._execute_local(block.name, block.input)
            if local is not None:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": local,
                    }
                )
            else:
                mcp_blocks.append(block)

        if mcp_blocks:
            results.extend(
                await ToolManager.execute_blocks(self.clients, mcp_blocks)
            )
        return results

    async def run(self, query: str) -> str:
        final_text_response = ""
        await self._process_query(query)

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

            mcp_tools = await ToolManager.get_all_tools(self.clients)
            response = self.claude_service.chat(
                messages=self.messages,
                tools=self._local_tool_defs() + mcp_tools,
            )

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
