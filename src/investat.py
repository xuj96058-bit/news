"""
Investat 金融信息流 MCP 客户端
使用个人 MCP 令牌读取已发布文章。
"""

import os
import json
import requests
from typing import Any, Optional

MCP_URL = "https://investat.ai/api/mcp/feed"


def get_token() -> str:
    token = os.getenv("INVESTAT_MCP_TOKEN")
    if not token:
        raise RuntimeError("請设定环境变量 INVESTAT_MCP_TOKEN")
    return token


def mcp_call(method: str, params: Optional[dict] = None, req_id: int = 1) -> dict:
    token = get_token()
    payload = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method,
        "params": params or {},
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    resp = requests.post(MCP_URL, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data.get("result", {})


def list_articles(limit: int = 20, search: str = "", ticker: str = "", institution: str = "") -> list[dict]:
    """搜索已发布文章摘要。"""
    args: dict[str, Any] = {"limit": limit}
    if search:
        args["search"] = search
    if ticker:
        args["ticker"] = ticker
    if institution:
        args["institution"] = institution
    result = mcp_call("tools/call", {"name": "list_articles", "arguments": args})
    # result.content[0].text 是 JSON 字符串
    content = result.get("content", [])
    if content and content[0].get("type") == "text":
        return json.loads(content[0]["text"]).get("items", [])
    return []


def get_article(article_id: int, body_offset: int = 0) -> dict:
    """读取单篇文章详情。"""
    result = mcp_call("tools/call", {
        "name": "get_article",
        "arguments": {"id": article_id, "bodyOffset": body_offset},
    })
    content = result.get("content", [])
    if content and content[0].get("type") == "text":
        return json.loads(content[0]["text"])
    return {}


def get_filters() -> dict:
    result = mcp_call("tools/call", {"name": "get_feed_filters", "arguments": {}})
    content = result.get("content", [])
    if content and content[0].get("type") == "text":
        return json.loads(content[0]["text"])
    return {}
