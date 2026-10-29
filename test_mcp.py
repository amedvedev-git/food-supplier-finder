"""Проверка MCP-сервера настоящим клиентом по HTTP. Запуск: python test_mcp.py [URL], по умолчанию http://127.0.0.1:8766/mcp"""
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8766/mcp"


async def main():
    async with streamable_http_client(URL) as (read, write, *_), ClientSession(read, write) as s:
        await s.initialize()
        tools = {t.name: t for t in (await s.list_tools()).tools}
        assert set(tools) == {"list_categories", "search_suppliers", "get_supplier", "draft_rfq"}, tools
        assert all(t.annotations.read_only_hint for t in tools.values())
        call = lambda name, **kw: s.call_tool(name, kw)  # noqa: E731
        cats = (await call("list_categories")).structured_content
        assert any(c["id"] == "salmon" for c in cats["categories"])
        found = (await call("search_suppliers", category="salmon", city="Тюмень", limit=3)).structured_content
        assert found["city"] == "Тюмень" and 0 < len(found["suppliers"]) <= 3, found
        top = found["suppliers"][0]
        card = (await call("get_supplier", supplier_id=top["id"], category="salmon", city="Тюмень")).structured_content
        assert card["score"] == top["score"] and card["score_breakdown"], card
        assert all(f["source_url"] for facts in card["facts"].values() for f in facts)  # у каждого факта источник
        rfq = (await call("draft_rfq", supplier_id=top["id"], category="salmon", city="Тюмень")).structured_content
        assert "Тюмень" in rfq["body"], rfq
        bad = await call("search_suppliers", category="нет-такой")
        assert bad.is_error
        print(json.dumps({"top": top["name"], "score": top["score"], "facts": list(card["facts"]),
                          "subject": rfq["subject"]}, ensure_ascii=False))
        print("ok")


asyncio.run(main())
