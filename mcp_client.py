import sys
import asyncio
import json
from typing import Optional, Any, Literal
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from pydantic import AnyUrl

try:
    # Streamable HTTP transport (mcp >= 1.8.0). This is what n8n's MCP Server
    # Trigger exposes when configured as `"type": "http"`.
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:  # older mcp SDK without Streamable HTTP support
    streamablehttp_client = None


Transport = Literal["stdio", "sse", "http"]


class MCPClient:
    """MCP client supporting stdio, SSE, and Streamable HTTP transports.

    - stdio: spawns a local server process (`command` + `args`).
    - sse:   connects to a remote server's SSE endpoint (`url`).
    - http:  connects to a remote server's Streamable HTTP endpoint (`url`).

    For the remote transports (sse / http) pass optional `headers` for auth,
    e.g. {"Authorization": "Bearer <token>"} for n8n's Bearer auth.
    """

    def __init__(
        self,
        command: Optional[str] = None,
        args: Optional[list[str]] = None,
        env: Optional[dict] = None,
        *,
        url: Optional[str] = None,
        transport: Transport = "stdio",
        headers: Optional[dict[str, str]] = None,
    ):
        self._command = command
        self._args = args or []
        self._env = env
        self._url = url
        self._transport = transport
        self._headers = headers
        self._session: Optional[ClientSession] = None
        self._exit_stack: AsyncExitStack = AsyncExitStack()

    async def connect(self):
        if self._transport == "stdio":
            read, write = await self._connect_stdio()
        elif self._transport == "sse":
            read, write = await self._connect_sse()
        elif self._transport == "http":
            read, write = await self._connect_http()
        else:
            raise ValueError(f"Unknown transport: {self._transport!r}")

        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await self._session.initialize()

    async def _connect_stdio(self):
        if not self._command:
            raise ValueError("stdio transport requires a `command`")
        server_params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=self._env,
        )
        read, write = await self._exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        return read, write

    async def _connect_sse(self):
        if not self._url:
            raise ValueError("sse transport requires a `url`")
        read, write = await self._exit_stack.enter_async_context(
            sse_client(self._url, headers=self._headers)
        )
        return read, write

    async def _connect_http(self):
        if streamablehttp_client is None:
            raise RuntimeError(
                "Streamable HTTP transport requires mcp >= 1.8.0 "
                "(mcp.client.streamable_http.streamablehttp_client not found)."
            )
        if not self._url:
            raise ValueError("http transport requires a `url`")
        # streamablehttp_client yields (read, write, get_session_id_callback);
        # we only need the read/write streams.
        transport = await self._exit_stack.enter_async_context(
            streamablehttp_client(self._url, headers=self._headers)
        )
        read, write = transport[0], transport[1]
        return read, write

    def session(self) -> ClientSession:
        if self._session is None:
            raise ConnectionError(
                "Client session not initialized. Call connect() first."
            )
        return self._session

    async def list_tools(self) -> list[types.Tool]:
        result = await self.session().list_tools()
        return result.tools

    async def call_tool(
        self, tool_name: str, tool_input
    ) -> types.CallToolResult | None:
        return await self.session().call_tool(tool_name, tool_input)

    async def list_prompts(self) -> list[types.Prompt]:
        result = await self.session().list_prompts()
        return result.prompts

    async def get_prompt(self, prompt_name, args: dict[str, str]):
        result = await self.session().get_prompt(prompt_name, args)
        return result.messages

    async def read_resource(self, uri: str) -> Any:
        result = await self.session().read_resource(AnyUrl(uri))
        resource = result.contents[0]  # only the first content is used

        if isinstance(resource, types.TextResourceContents):
            if resource.mimeType == "application/json":
                return json.loads(resource.text)

            return resource.text  # fallback: return as plain text

    async def cleanup(self):
        await self._exit_stack.aclose()
        self._session = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup()


# For testing the client standalone. Point it at your n8n MCP server:
#   N8N_MCP_URL=http://192.168.2.12:5678/mcp-server/http \
#   N8N_MCP_TOKEN=<your token> python mcp_client.py
async def main():
    import os

    url = os.getenv("N8N_MCP_URL", "http://192.168.2.12:5678/mcp-server/http")
    token = os.getenv("N8N_MCP_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else None

    async with MCPClient(transport="http", url=url, headers=headers) as client:
        tools = await client.list_tools()
        print(f"Connected to {url} — {len(tools)} tool(s):")
        for tool in tools:
            print(f"  - {tool.name}: {tool.description}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
