"""Investat's authenticated MCP feed, with request-local credentials."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

import requests
from urllib3.exceptions import ReadTimeoutError

from . import env as _env  # Load the project's .env through the existing loader.


MCP_URL = "https://investat.ai/api/mcp/feed"
MCP_PROTOCOL_VERSION = "2024-11-05"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_PROTOCOL_VERSIONS = {MCP_PROTOCOL_VERSION, "2025-03-26", "2025-06-18"}
_ERROR_MESSAGES = {
    "missing_token": "Investat MCP token is required (INVESTAT_MCP_TOKEN)",
    "invalid_token": "Investat MCP token is invalid",
    "authentication": "Investat authentication failed; check the MCP token",
    "rate_limit": "Investat request limit reached; try collecting again later",
    "timeout": "Investat request timed out",
    "transport": "Investat connection failed",
    "http": "Investat returned an HTTP error",
    "redirect": "Investat redirect was refused",
    "protocol": "Investat returned an invalid MCP response",
    "rpc": "Investat MCP request failed",
    "tool": "Investat tool reported an error",
    "payload": "Investat returned invalid article data",
    "response_limit": "Investat response exceeded the size limit",
}


class InvestatError(RuntimeError):
    """A deliberately safe error: no response body, headers, or credentials."""

    def __init__(self, category: str, *, status_code=None, rpc_code=None):
        self.category = category if category in _ERROR_MESSAGES else "protocol"
        self.status_code = status_code if isinstance(status_code, int) and 100 <= status_code <= 599 else None
        self.rpc_code = rpc_code if isinstance(rpc_code, int) and not isinstance(rpc_code, bool) else None
        message = _ERROR_MESSAGES[self.category]
        if self.status_code is not None:
            message += f" (HTTP {self.status_code})"
        elif self.rpc_code is not None:
            message += f" (code {self.rpc_code})"
        super().__init__(message)


def get_token(token: Optional[str] = None) -> str:
    """Resolve an explicit token or the .env/system default, without changing it."""
    value = os.getenv("INVESTAT_MCP_TOKEN") if token is None else token
    if not value:
        raise InvestatError("missing_token")
    if not isinstance(value, str) or "\r" in value or "\n" in value:
        raise InvestatError("invalid_token")
    value = value.strip()
    if not value:
        raise InvestatError("missing_token")
    return value


def _rpc_response(response, request_id: int, *, deadline: float) -> dict:
    """Accept ordinary JSON or the response event in a Streamable HTTP SSE body."""
    content_type = response.headers.get("Content-Type", "").lower()
    sse = "text/event-stream" in content_type
    received = 0
    buffer = bytearray()
    data_lines = []

    def event(line):
        if not line:
            if data_lines:
                try:
                    value = json.loads(b"\n".join(data_lines))
                except (ValueError, TypeError, UnicodeError):
                    value = None
                data_lines.clear()
                if (isinstance(value, dict) and value.get("jsonrpc") == "2.0"
                        and value.get("id") == request_id):
                    return value
        elif line.startswith(b"data:"):
            data_lines.append(line[5:].removeprefix(b" "))
        return None

    # Stop at the matching event: a live SSE connection need not close first.
    for chunk in response.iter_content(chunk_size=1 if sse else 16384):
        if time.monotonic() > deadline:
            raise InvestatError("timeout")
        if not chunk:
            continue
        received += len(chunk)
        if received > MAX_RESPONSE_BYTES:
            raise InvestatError("response_limit")
        if sse:
            # A byte-sized read handles open, unchunked SSE replies promptly.
            # Append without rescanning a growing line, keeping work linear.
            lines = chunk.split(b"\n")
            for index, line in enumerate(lines):
                buffer.extend(line)
                if index < len(lines) - 1:
                    value = event(bytes(buffer).removesuffix(b"\r"))
                    buffer.clear()
                    if value is not None:
                        return value
        else:
            buffer.extend(chunk)
    if not sse:
        try:
            value = json.loads(buffer)
        except (ValueError, TypeError, UnicodeError):
            raise InvestatError("protocol") from None
        if (not isinstance(value, dict) or value.get("jsonrpc") != "2.0"
                or value.get("id") != request_id):
            raise InvestatError("protocol")
        return value
    # Be tolerant of a finite SSE reply without a trailing blank line.
    if buffer:
        event(bytes(buffer).removesuffix(b"\r"))
    value = event(b"")
    if value is not None:
        return value
    raise InvestatError("protocol")


def _tool_data(result: dict):
    if result.get("isError"):
        raise InvestatError("tool")
    if "structuredContent" in result:
        value = result["structuredContent"]
        if isinstance(value, (dict, list)):
            return value
        raise InvestatError("payload")
    content = result.get("content", [])
    if not isinstance(content, list):
        raise InvestatError("payload")
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                value = json.loads(block.get("text", ""))
            except (ValueError, TypeError):
                continue
            if isinstance(value, (dict, list)):
                return value
    # Some stateless feed servers already return the tool's JSON object.
    if any(key in result for key in ("items", "articles", "article", "id")):
        return result
    if not result or not content:
        return {}
    raise InvestatError("payload")


def _articles(result: dict) -> list[dict]:
    data = _tool_data(result)
    items = data if isinstance(data, list) else data.get("items", data.get("articles", []))
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise InvestatError("payload")
    return items


def _article(result: dict) -> dict:
    data = _tool_data(result)
    if not isinstance(data, dict):
        raise InvestatError("payload")
    if "article" in data:
        value = data["article"]
        if not isinstance(value, dict):
            raise InvestatError("payload")
        # Pagination can be returned beside, rather than within, the article.
        fields = ("body", "bodyText", "content", "text", "nextBodyOffset", "hasMoreBody",
                  "hasMore", "truncated", "bodyTruncated")
        return {**value, **{key: data[key] for key in fields if key in data}}
    return data


class InvestatClient:
    """A small MCP client. A token and session belong only to this instance."""

    def __init__(self, token: Optional[str] = None, *, timeout: float = 15, post=None):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._token = get_token(token)
        self.timeout = timeout
        self._post = post or requests.post
        self._session_id = None
        self._protocol_version = MCP_PROTOCOL_VERSION
        self._initialized = False
        self._next_id = 1

    def _request(self, method: str, params: Optional[dict] = None, *, request_id=None, notification=False):
        if request_id is None and not notification:
            request_id = self._next_id
            self._next_id += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            payload["id"] = request_id
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._initialized or method == "notifications/initialized":
            headers["MCP-Protocol-Version"] = self._protocol_version
        try:
            deadline = time.monotonic() + self.timeout
            response = self._post(MCP_URL, headers=headers, json=payload,
                                  timeout=self.timeout, allow_redirects=False, stream=True)
        except requests.exceptions.Timeout:
            raise InvestatError("timeout") from None
        except Exception as exc:
            category = "timeout" if any(isinstance(arg, ReadTimeoutError) for arg in exc.args) else "transport"
            raise InvestatError(category) from None
        try:
            status = response.status_code
            if status in {401, 403}:
                raise InvestatError("authentication", status_code=status)
            if status == 429:
                raise InvestatError("rate_limit", status_code=status)
            if 300 <= status <= 399:
                raise InvestatError("redirect", status_code=status)
            if not 200 <= status <= 299:
                raise InvestatError("http", status_code=status)
            if notification:
                return {}
            data = _rpc_response(response, request_id, deadline=deadline)
        except InvestatError:
            raise
        except requests.exceptions.Timeout:
            raise InvestatError("timeout") from None
        except Exception as exc:
            category = "timeout" if any(isinstance(arg, ReadTimeoutError) for arg in exc.args) else "transport"
            raise InvestatError(category) from None
        finally:
            response.close()
        if "error" in data:
            error = data["error"]
            code = error.get("code") if isinstance(error, dict) else None
            raise InvestatError("rpc", rpc_code=code)
        result = data.get("result")
        if not isinstance(result, dict):
            raise InvestatError("protocol")
        if method == "initialize":
            session_id = response.headers.get("Mcp-Session-Id")
            if session_id is not None:
                if not isinstance(session_id, str) or not session_id or "\r" in session_id or "\n" in session_id:
                    raise InvestatError("protocol")
                self._session_id = session_id
        return result

    def initialize(self):
        if self._initialized:
            return
        try:
            result = self._request("initialize", {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "local-news-inbox", "version": "1.0"},
            })
        except InvestatError as exc:
            if exc.category != "rpc" or exc.rpc_code != -32601:
                raise
            # Legacy stateless servers may expose only tools/call.
            self._initialized = True
            return
        version = result.get("protocolVersion", MCP_PROTOCOL_VERSION)
        if not isinstance(version, str) or version not in _PROTOCOL_VERSIONS:
            raise InvestatError("protocol")
        self._protocol_version = version
        self._request("notifications/initialized", notification=True)
        self._initialized = True

    def mcp_call(self, method: str, params: Optional[dict] = None, req_id=None) -> dict:
        if method not in {"initialize", "notifications/initialized"}:
            self.initialize()
        try:
            return self._request(method, params, request_id=req_id)
        except InvestatError as exc:
            if (exc.status_code != 404 or not self._session_id
                    or method != "tools/call"):
                raise
            # MCP sessions may expire. Reinitialize once before retrying a read.
            self._session_id = None
            self._initialized = False
            self.initialize()
            return self._request(method, params, request_id=req_id)

    def list_articles(self, limit: int = 20, search: str = "", ticker: str = "", institution: str = "") -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("limit must be an integer from 1 to 50")
        args: dict[str, Any] = {"limit": limit}
        args.update({key: value for key, value in {"search": search, "ticker": ticker,
                                                  "institution": institution}.items() if value})
        return _articles(self.mcp_call("tools/call", {"name": "list_articles", "arguments": args}))

    def get_article(self, article_id, body_offset: int = 0) -> dict:
        if isinstance(body_offset, bool) or not isinstance(body_offset, int) or body_offset < 0:
            raise ValueError("body_offset must be a non-negative integer")
        return _article(self.mcp_call("tools/call", {
            "name": "get_article", "arguments": {"id": article_id, "bodyOffset": body_offset},
        }))

    def get_filters(self) -> dict:
        value = _tool_data(self.mcp_call("tools/call", {"name": "get_feed_filters", "arguments": {}}))
        if not isinstance(value, dict):
            raise InvestatError("payload")
        return value


def mcp_call(method: str, params: Optional[dict] = None, req_id: int = 1, *,
             token: Optional[str] = None, timeout: float = 60) -> dict:
    """Compatibility helper; prefer one InvestatClient for several calls."""
    return InvestatClient(token=token, timeout=timeout).mcp_call(method, params, req_id=req_id)


def list_articles(limit: int = 20, search: str = "", ticker: str = "", institution: str = "", *,
                  token: Optional[str] = None, timeout: float = 60) -> list[dict]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer from 1 to 50")
    args = {"limit": limit}
    args.update({key: value for key, value in {"search": search, "ticker": ticker,
                                             "institution": institution}.items() if value})
    return _articles(mcp_call("tools/call", {"name": "list_articles", "arguments": args},
                              token=token, timeout=timeout))


def get_article(article_id: int, body_offset: int = 0, *, token: Optional[str] = None,
                timeout: float = 60) -> dict:
    return _article(mcp_call("tools/call", {
        "name": "get_article", "arguments": {"id": article_id, "bodyOffset": body_offset},
    }, token=token, timeout=timeout))


def get_filters(*, token: Optional[str] = None, timeout: float = 60) -> dict:
    value = _tool_data(mcp_call("tools/call", {"name": "get_feed_filters", "arguments": {}},
                                token=token, timeout=timeout))
    if not isinstance(value, dict):
        raise InvestatError("payload")
    return value
