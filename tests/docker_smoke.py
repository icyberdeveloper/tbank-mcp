"""Run after docker build: actual stdio handshake, discovery and offline tool.

Not named test_*: the offline suite must not require a Docker daemon.
"""
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command='docker', args=[
        'run', '--rm', '-i', '--entrypoint', 'tbank-mcp', 'tbank-mcp:tunnel'])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {'flows', 'session_status', 'list_accounts'} <= {t.name for t in tools.tools}
            result = await session.call_tool('flows', {})
            assert not result.isError
            assert 'not found' not in str(result.content)
    print('Container stdio initialization, tools/list and offline flows: OK')

if __name__ == '__main__':
    asyncio.run(asyncio.wait_for(main(), timeout=60))
