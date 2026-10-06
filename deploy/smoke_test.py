"""Usage: python deploy/smoke_test.py https://your-host/mcp"""

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main(url: str) -> None:
    async with (
        streamablehttp_client(url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        initialized = await session.initialize()
        listing = await session.list_tools()
        print(f"Connected to {initialized.serverInfo.name}: {len(listing.tools)} tools")
        assert len(listing.tools) == 16, "Expected 16 tools"
        assert all(tool.annotations.readOnlyHint for tool in listing.tools)
        result = await session.call_tool("get_fpl_overview", {})
        if result.isError:
            raise RuntimeError(str(result.content))
        print(json.dumps(result.structuredContent, indent=2))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/mcp"))
