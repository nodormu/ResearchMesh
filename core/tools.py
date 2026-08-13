import json
from typing import Literal, List
from mcp.types import CallToolResult, Tool, TextContent
from mcp_client import MCPClient
from anthropic.types import ToolResultBlockParam


class ToolManager:
    @classmethod
    async def get_all_tools(cls, clients: dict[str, MCPClient]) -> list[Tool]:
        """Gets all tools from the provided clients."""
        tools = []
        for client in clients.values():
            tool_models = await client.list_tools()
            tools += [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.inputSchema,
                }
                for t in tool_models
            ]
        return tools

    @classmethod
    async def _tool_owners(
        cls, clients: dict[str, MCPClient]
    ) -> dict[str, MCPClient]:
        """Maps tool name -> the first client offering it (one list_tools per
        client, rather than one per client per tool being executed)."""
        owners: dict[str, MCPClient] = {}
        for client in clients.values():
            for tool in await client.list_tools():
                owners.setdefault(tool.name, client)
        return owners

    @classmethod
    def _build_tool_result_part(
        cls,
        tool_use_id: str,
        text: str,
        status: Literal["success"] | Literal["error"],
    ) -> ToolResultBlockParam:
        """Builds a tool result part dictionary."""
        return {
            "tool_use_id": tool_use_id,
            "type": "tool_result",
            "content": text,
            "is_error": status == "error",
        }

    @classmethod
    async def execute_blocks(
        cls, clients: dict[str, MCPClient], tool_use_blocks
    ) -> List[ToolResultBlockParam]:
        """Executes a list of tool_use blocks against the provided clients."""
        tool_result_blocks: list[ToolResultBlockParam] = []
        owners = await cls._tool_owners(clients)

        for tool_request in tool_use_blocks:
            tool_use_id = tool_request.id
            tool_name = tool_request.name
            tool_input = tool_request.input

            client = owners.get(tool_name)

            if not client:
                tool_result_blocks.append(
                    cls._build_tool_result_part(
                        tool_use_id, "Could not find that tool", "error"
                    )
                )
                continue

            try:
                tool_output: CallToolResult | None = await client.call_tool(
                    tool_name, tool_input
                )
                items = tool_output.content if tool_output else []
                content_list = [
                    item.text for item in items if isinstance(item, TextContent)
                ]
                content_json = json.dumps(content_list)
                status = (
                    "error"
                    if tool_output and tool_output.isError
                    else "success"
                )
                tool_result_blocks.append(
                    cls._build_tool_result_part(
                        tool_use_id, content_json, status
                    )
                )
            except Exception as e:
                error_message = f"Error executing tool '{tool_name}': {e}"
                print(error_message)
                tool_result_blocks.append(
                    cls._build_tool_result_part(
                        tool_use_id,
                        json.dumps({"error": error_message}),
                        "error",
                    )
                )
        return tool_result_blocks
