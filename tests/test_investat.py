import json
import os
import unittest
from unittest.mock import patch

import requests
from urllib3.exceptions import ReadTimeoutError

from src import investat
from src.investat import InvestatClient, InvestatError, MCP_URL, get_token


TOKEN = "synthetic-investat-token"
SECRET = "synthetic-sensitive-server-value"


class Response:
    def __init__(self, value=None, *, status=200, headers=None, text=None, chunks=None):
        self.status_code = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.payload = text.encode("utf-8") if text is not None else json.dumps(value).encode("utf-8")
        self.chunks = chunks
        self.closed = False
        self.read_count = 0
        self.chunk_size = None

    def iter_content(self, chunk_size):
        self.chunk_size = chunk_size
        for chunk in self.chunks if self.chunks is not None else [self.payload]:
            self.read_count += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


def rpc(identifier, result):
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def tool(value):
    return {"content": [{"type": "text", "text": json.dumps(value)}]}


class InvestatClientTests(unittest.TestCase):
    def post(self, result, *, protocol="2025-06-18", session="synthetic-session"):
        calls = []
        responses = []

        def send(url, **kwargs):
            calls.append((url, kwargs))
            payload = kwargs["json"]
            if payload["method"] == "initialize":
                response = Response(rpc(payload["id"], {"protocolVersion": protocol}),
                                    headers={"Mcp-Session-Id": session})
            elif payload["method"] == "notifications/initialized":
                response = Response(status=202, text="")
            else:
                response = result(payload) if callable(result) else Response(rpc(payload["id"], result))
            responses.append(response)
            return response
        return send, calls, responses

    def test_json_initialization_and_session_are_forwarded_with_local_token(self):
        send, calls, responses = self.post(tool({"items": [{"id": 3, "title": "Research"}]}))
        with patch.dict(os.environ, {"INVESTAT_MCP_TOKEN": "synthetic-env-token"}):
            client = InvestatClient(TOKEN, timeout=9, post=send)
            self.assertEqual(client.list_articles(limit=4)[0]["id"], 3)
            self.assertEqual(os.environ["INVESTAT_MCP_TOKEN"], "synthetic-env-token")
        self.assertEqual([entry[1]["json"]["method"] for entry in calls],
                         ["initialize", "notifications/initialized", "tools/call"])
        for url, kwargs in calls:
            self.assertEqual(url, MCP_URL)
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + TOKEN)
            self.assertFalse(kwargs["allow_redirects"])
            self.assertTrue(kwargs["stream"])
            self.assertEqual(kwargs["timeout"], 9)
        for _, kwargs in calls[1:]:
            self.assertEqual(kwargs["headers"]["Mcp-Session-Id"], "synthetic-session")
            self.assertEqual(kwargs["headers"]["MCP-Protocol-Version"], "2025-06-18")
        self.assertNotIn("id", calls[1][1]["json"])
        self.assertTrue(all(response.closed for response in responses))

    def test_sse_returns_matching_multiline_event_before_stream_ends(self):
        def result(payload):
            value = json.dumps(rpc(payload["id"], tool({"items": [{"id": 9}]})))
            prefix, suffix = value.split('"result":', 1)
            stream = (": heartbeat\n\nevent: message\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notice\"}\n\n"
                      f"event: message\ndata: {prefix}\ndata: \"result\":{suffix}\n\n")
            return Response(headers={"Content-Type": "text/event-stream"},
                            chunks=[stream.encode(), RuntimeError(SECRET)])
        send, _, responses = self.post(result)
        self.assertEqual(InvestatClient(TOKEN, post=send).list_articles(), [{"id": 9}])
        self.assertEqual(responses[-1].read_count, 1)
        self.assertEqual(responses[-1].chunk_size, 1)
        self.assertTrue(responses[-1].closed)

    def test_sse_event_can_be_split_in_the_middle_of_utf8_and_lines(self):
        def result(payload):
            stream = ("data: " + json.dumps(rpc(payload["id"], tool({"items": [{"title": "市場"}]})),
                                            ensure_ascii=False) + "\r\n\r\n").encode()
            return Response(headers={"Content-Type": "text/event-stream"},
                            chunks=[stream[index:index + 3] for index in range(0, len(stream), 3)])
        send, _, _ = self.post(result)
        self.assertEqual(InvestatClient(TOKEN, post=send).list_articles()[0]["title"], "市場")

    def test_http_and_redirect_errors_do_not_read_or_expose_raw_body(self):
        for status in (401, 403, 429, 500, 302):
            response = Response(status=status, text=SECRET)
            with self.subTest(status=status), self.assertRaises(InvestatError) as caught:
                InvestatClient(TOKEN, post=lambda *a, **k: response).list_articles()
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertEqual(response.read_count, 0)
            self.assertTrue(response.closed)

    def test_rpc_and_tool_errors_are_safe(self):
        for error_result in ("rpc", "tool"):
            def result(payload):
                if error_result == "rpc":
                    return Response({"jsonrpc": "2.0", "id": payload["id"],
                                     "error": {"code": -32602, "message": SECRET, "data": {"token": TOKEN}}})
                return Response(rpc(payload["id"], {"isError": True, "content": [{"type": "text", "text": SECRET}]}))
            send, _, _ = self.post(result)
            with self.subTest(error=error_result), self.assertRaises(InvestatError) as caught:
                InvestatClient(TOKEN, post=send).list_articles()
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_bad_payloads_and_mismatched_ids_are_safe(self):
        payloads = [SECRET, "[]", '{"jsonrpc":"2.0","id":999,"result":{}}']
        for text in payloads:
            send, _, _ = self.post(lambda _: Response(text=text))
            with self.subTest(text=text), self.assertRaises(InvestatError) as caught:
                InvestatClient(TOKEN, post=send).list_articles()
            self.assertNotIn(SECRET, str(caught.exception))

    def test_connection_timeout_and_stream_failure_are_safe(self):
        for failure in (requests.exceptions.Timeout(SECRET), requests.exceptions.ConnectionError(SECRET)):
            def failed(*args, **kwargs):
                raise failure
            with self.subTest(failure=type(failure).__name__), self.assertRaises(InvestatError) as caught:
                InvestatClient(TOKEN, post=failed).list_articles()
            self.assertNotIn(SECRET, str(caught.exception))
        send, _, responses = self.post(lambda _: Response(chunks=[RuntimeError(SECRET)]))
        with self.assertRaises(InvestatError) as caught:
            InvestatClient(TOKEN, post=send).list_articles()
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertTrue(responses[-1].closed)

    def test_response_size_and_overall_time_are_bounded(self):
        send, _, responses = self.post(lambda _: Response(chunks=[b"x" * 100]))
        with patch("src.investat.MAX_RESPONSE_BYTES", 50), self.assertRaises(InvestatError) as caught:
            InvestatClient(TOKEN, post=send).list_articles()
        self.assertEqual(caught.exception.category, "response_limit")
        self.assertTrue(responses[-1].closed)
        send, _, _ = self.post(tool({"items": []}))
        with patch("src.investat.time.monotonic", side_effect=[0, 100]), self.assertRaises(InvestatError) as caught:
            InvestatClient(TOKEN, timeout=1, post=send).list_articles()
        self.assertEqual(caught.exception.category, "timeout")

    def test_wrapped_sse_read_timeout_is_safe_and_recognized(self):
        failure = requests.exceptions.ConnectionError(ReadTimeoutError(None, None, SECRET))
        send, _, responses = self.post(lambda _: Response(headers={"Content-Type": "text/event-stream"},
                                                          chunks=[failure]))
        with self.assertRaises(InvestatError) as caught:
            InvestatClient(TOKEN, post=send).list_articles()
        self.assertEqual(caught.exception.category, "timeout")
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertTrue(responses[-1].closed)

    def test_malformed_or_unrelated_sse_events_are_safe(self):
        send, _, _ = self.post(lambda _: Response(headers={"Content-Type": "text/event-stream"},
                                                  text=f"data: {SECRET}\n\ndata: {{\"id\":999}}\n\n"))
        with self.assertRaises(InvestatError) as caught:
            InvestatClient(TOKEN, post=send).list_articles()
        self.assertEqual(caught.exception.category, "protocol")
        self.assertNotIn(SECRET, str(caught.exception))

    def test_stateless_server_can_report_initialize_not_found(self):
        calls = []
        def send(url, **kwargs):
            payload = kwargs["json"]
            calls.append(payload["method"])
            if payload["method"] == "initialize":
                return Response({"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32601, "message": SECRET}})
            return Response(rpc(payload["id"], tool({"items": []})))
        self.assertEqual(InvestatClient(TOKEN, post=send).list_articles(), [])
        self.assertEqual(calls, ["initialize", "tools/call"])

    def test_expired_session_is_reinitialized_once(self):
        sessions = []
        reads = []
        def send(url, **kwargs):
            payload = kwargs["json"]
            if payload["method"] == "initialize":
                session = "session-" + str(len(sessions) + 1)
                sessions.append(session)
                return Response(rpc(payload["id"], {}), headers={"Mcp-Session-Id": session})
            if payload["method"] == "notifications/initialized":
                return Response(status=202)
            reads.append(kwargs["headers"]["Mcp-Session-Id"])
            return Response(status=404) if len(reads) == 1 else Response(rpc(payload["id"], tool({"items": []})))
        self.assertEqual(InvestatClient(TOKEN, post=send).list_articles(), [])
        self.assertEqual(reads, ["session-1", "session-2"])

    def test_structured_results_details_and_filters(self):
        def result(payload):
            name = payload["params"]["name"]
            value = ({"article": {"id": 1, "body": "text"}, "nextBodyOffset": 4}
                     if name == "get_article" else {"institutions": ["Bank"]})
            return Response(rpc(payload["id"], {"structuredContent": value}))
        send, _, _ = self.post(result)
        client = InvestatClient(TOKEN, post=send)
        self.assertEqual(client.get_article(1), {"id": 1, "body": "text", "nextBodyOffset": 4})
        self.assertEqual(client.get_filters(), {"institutions": ["Bank"]})

    def test_detail_body_can_be_returned_beside_article_metadata(self):
        send, _, _ = self.post(tool({"article": {"id": 1, "title": "Research"},
                                    "body": "Provided fragment", "nextBodyOffset": 17}))
        self.assertEqual(InvestatClient(TOKEN, post=send).get_article(1),
                         {"id": 1, "title": "Research", "body": "Provided fragment", "nextBodyOffset": 17})

    def test_credentials_are_resolved_without_mutating_the_environment(self):
        with patch.dict(os.environ, {"INVESTAT_MCP_TOKEN": "synthetic-env-token"}, clear=True):
            self.assertEqual(get_token(), "synthetic-env-token")
            self.assertEqual(get_token("  " + TOKEN + "  "), TOKEN)
            self.assertEqual(os.environ["INVESTAT_MCP_TOKEN"], "synthetic-env-token")
            for invalid in ("", "\n" + TOKEN, TOKEN + "\r"):
                with self.assertRaises(InvestatError):
                    get_token(invalid)
        with patch.dict(os.environ, {}, clear=True), patch("src.investat.requests.post") as post:
            with self.assertRaises(InvestatError):
                InvestatClient().list_articles()
            post.assert_not_called()

    def test_public_compatibility_helpers_preserve_tools_and_arguments(self):
        with patch("src.investat.mcp_call", return_value=tool({"items": [{"id": 8}]})) as call:
            self.assertEqual(investat.list_articles(5, "rates", "ABC", "Bank", token=TOKEN), [{"id": 8}])
            self.assertEqual(call.call_args.args[1]["arguments"],
                             {"limit": 5, "search": "rates", "ticker": "ABC", "institution": "Bank"})
        with patch("src.investat.mcp_call", return_value=tool({"id": 8, "body": "text"})) as call:
            self.assertEqual(investat.get_article(8, 10)["body"], "text")
            self.assertEqual(call.call_args.args[1]["arguments"], {"id": 8, "bodyOffset": 10})
        with patch("src.investat.mcp_call", return_value=tool({"tickers": ["ABC"]})):
            self.assertEqual(investat.get_filters(), {"tickers": ["ABC"]})


if __name__ == "__main__":
    unittest.main()
