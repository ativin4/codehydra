from typing import List, Dict, Any
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

class MCPClient:
    def __init__(self):
        self.sessions: Dict[str, ClientSession] = {}

    async def connect_to_server(self, name: str, command: str, args: List[str] = []):
        """Connects to an MCP server via stdio."""
        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=None
        )
        
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self.sessions[name] = session
                print(f"Connected to MCP server: {name}")
                
                # Keep session alive or handle requests
                # For this implementation, we might need a background task 
                # or a way to keep the session accessible.
                # Since this is a CLI, we might connect/disconnect per tool call 
                # or keep them open in the background.

    async def list_tools(self, server_name: str) -> List[Dict[str, Any]]:
        """Lists tools available on a specific MCP server."""
        if server_name not in self.sessions:
            return []
        
        tools = await self.sessions[server_name].list_tools()
        return [tool.dict() for tool in tools.tools]

    async def get_tool_specs(self, server_name: str) -> List[Dict[str, Any]]:
        """Returns tools formatted as OpenAI-style function specs."""
        mcp_tools = await self.list_tools(server_name)
        tool_specs = []

        for tool in mcp_tools:
            tool_specs.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", {
                        "type": "object",
                        "properties": {}
                    })
                }
            })

        return tool_specs

if __name__ == "__main__":
    # Example usage (would need a running MCP server)
    client = MCPClient()
    # asyncio.run(client.connect_to_server("sqlite", "mcp-server-sqlite", ["--db", "test.db"]))
