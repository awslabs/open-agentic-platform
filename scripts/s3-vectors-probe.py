"""MCP client probe for scripts/test-s3-vectors-matrix.sh.

Runs inside the cluster (in the strands-agent image, which ships the mcp client) and
talks to an MCP server through the gateway, exactly as an agent does.

  python - <url> <token> list
  python - <url> <token> call <tool> '<json arguments>'

Prints one JSON object: {"tools": [...]} or {"isError": bool, "result": ...},
or {"error": "<exception>"} when the connection itself fails.
"""

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client


async def main(url: str, token: str, op: str, *rest: str) -> dict:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with streamable_http_client(url, http_client=http) as (read, write, *_):
        async with ClientSession(read, write) as s:
            await s.initialize()
            if op == "list":
                return {"tools": sorted(t.name for t in (await s.list_tools()).tools)}
            r = await s.call_tool(rest[0], json.loads(rest[1]) if len(rest) > 1 else {})
            # mcp 2.x names these structured_content / is_error; 1.x used camelCase.
            sc = getattr(r, "structured_content", getattr(r, "structuredContent", None))
            err = getattr(r, "is_error", getattr(r, "isError", False))
            if sc is not None:
                result = sc.get("result", sc)
            else:
                result = " ".join(getattr(c, "text", "") for c in r.content)
            return {"isError": bool(err), "result": result}


try:
    out = asyncio.run(main(*sys.argv[1:]))
except BaseException as e:  # noqa: BLE001 - report every failure as data
    def leaves(x):
        subs = getattr(x, "exceptions", None)
        return [y for s in subs for y in leaves(s)] if subs else [x]
    out = {"error": "; ".join(f"{type(x).__name__}: {x}" for x in leaves(e))}
print(json.dumps(out))
