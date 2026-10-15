"""Research snapshots and report generation use synthetic evidence only."""

import copy
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from openai import APITimeoutError
from requests.exceptions import ReadTimeout

from src.analysis_errors import ModelResponseError, ProviderAPIError, ProviderConfigurationError
from src.llm_providers import PROVIDERS
from src.research import (
    MAX_REPORT_TOKENS, MAX_RESPONSE_CHARS, MAX_SOURCE_CHARS, MAX_TOTAL_SOURCE_CHARS,
    ResearchError, filter_articles, format_sources_appendix, generate_research_report,
    prepare_evidence, prepare_sources,
)


KEY = "synthetic-research-session-key"
OTHER_KEY = "synthetic-other-provider-key"


def article(**overrides):
    data = {"id": "news-1", "source_id": "test-rss", "source_name": "Synthetic publisher",
            "title": "Synthetic financing report", "url": "https://example.org/story",
            "published_at": "2026-10-13T17:00:00Z", "last_seen": "2026-10-14T02:00:00Z",
            "content_scope": "summary", "content": "", "summary": "Reported gross financing of 20 billion in September.",
            "sources": []}
    data.update(overrides)
    return data


def payload(**overrides):
    data = {"title": "A synthetic research draft",
            "executive_findings": "Reported gross financing of 20 billion in September. [S1]",
            "chart_data_interpretation": "The excerpt describes financing; it does not supply a chart. [S1]",
            "differences_and_alternatives": "A gross financing figure need not measure net flows. [S1]",
            "conditional_mechanisms": "If the financing supports spending, demand could respond; the excerpt cannot establish that. [S1]",
            "watchlist": "Check the original table and the observation period. [S1]",
            "limitations": "The supplied excerpt is insufficient to infer investor ownership. [S1]"}
    data.update(overrides)
    return data


class FakeStream:
    def __init__(self, raw, *, reason="stop", chunk_size=41, end=True):
        self.raw = raw
        self.reason = reason
        self.chunk_size = chunk_size
        self.end = end
        self.closed = False

    def __iter__(self):
        for offset in range(0, len(self.raw), self.chunk_size):
            yield SimpleNamespace(choices=[SimpleNamespace(
                finish_reason=None, delta=SimpleNamespace(content=self.raw[offset:offset + self.chunk_size]))])
        if self.end:
            yield SimpleNamespace(choices=[SimpleNamespace(finish_reason=self.reason, delta=SimpleNamespace(content=None))])
            yield SimpleNamespace(choices=[])

    def close(self):
        self.closed = True


def fake_client(data=None, *, raw=None, **stream_options):
    stream = FakeStream(json.dumps(payload() if data is None else data) if raw is None else raw, **stream_options)
    client = MagicMock()
    client.chat.completions.create.return_value = stream
    return client, stream


class SourceSnapshotTests(unittest.TestCase):
    def test_snapshot_keeps_scope_dates_article_id_and_all_deduplicated_origins(self):
        original = article(sources=[{
            "source_id": "investat", "source_name": "Synthetic institutional research",
            "title": "Same syndicated story", "url": None, "published_at": "2026-10-13T17:00:00Z",
            "last_seen": "2026-10-14T03:00:00Z", "content_scope": "fragment",
            "raw_metadata": {"token": OTHER_KEY},
        }])
        source = prepare_sources([original])[0]
        self.assertEqual(source["id"], "S1")
        self.assertEqual(source["article_id"], "news-1")
        self.assertEqual(source["content_scope"], "summary")
        self.assertEqual(source["published_at"], "2026-10-13T17:00:00Z")
        self.assertEqual(source["publication_date"], "2026-10-14")
        self.assertEqual(source["retrieved_date"], "2026-10-14")
        self.assertEqual({item["source_type"] for item in source["origins"]}, {"rss", "investat"})
        self.assertEqual(source["verification_status"], "not_independently_verified")
        self.assertNotIn(OTHER_KEY, json.dumps(source))
        self.assertNotIn("S1", original)

    def test_metadata_only_articles_and_duplicate_selected_ids_are_skipped(self):
        articles = [article(content_scope="title_only"), article(content_scope="filing_notice"),
                    article(summary=""), article(summary="Synthetic financing report"),
                    article(), article(), article(id="news-2")]
        sources = prepare_sources(articles)
        self.assertEqual([source["id"] for source in sources], ["S1", "S2"])
        self.assertEqual([source["article_id"] for source in sources], ["news-1", "news-2"])

    def test_per_article_and_overall_source_budgets_are_explicit(self):
        sources = prepare_sources([article(id=f"news-{index}", content="x" * 14000) for index in range(40)])
        self.assertEqual(len(sources), 30)
        self.assertLessEqual(sum(len(item["content"]) for item in sources), MAX_TOTAL_SOURCE_CHARS)
        self.assertTrue(all(len(item["content"]) <= MAX_SOURCE_CHARS for item in sources))
        self.assertTrue(all(item["content_truncated"] for item in sources))
        self.assertTrue(all(item["supplied_character_count"] == 14000 for item in sources))
        limited = prepare_sources([article(), article(id="news-2")], max_articles=1, max_chars_per_article=10)
        self.assertEqual(len(limited), 1)
        self.assertEqual(len(limited[0]["content"]), 10)
        self.assertEqual(limited[0]["content_scope"], "summary")

    def test_zero_and_invalid_limits(self):
        self.assertEqual(prepare_sources([article()], max_articles=0), [])
        self.assertEqual(prepare_sources([article()], max_chars_per_article=0), [])
        for limit in (-1, True, "10"):
            with self.subTest(limit=limit), self.assertRaises(ResearchError):
                prepare_sources([article()], max_articles=limit)

    def test_credentials_and_nonpublic_investat_identity_never_become_urls(self):
        for url in ("investat:abc", "javascript:alert(1)", "https://user:secret@example.org", "https://example.org/<bad>"):
            with self.subTest(url=url):
                source = prepare_sources([article(source_id="investat", url=url)])[0]
                self.assertIsNone(source["url"])
                appendix = format_sources_appendix([source])
                self.assertNotIn(url, appendix)
                self.assertIn("未提供有效公開連結", appendix)

    def test_credential_query_fields_and_fragments_are_removed_from_all_origins(self):
        url = ("https://example.org/story?q=financing&page=2&API_KEY=" + KEY +
               "&access_token=" + OTHER_KEY + "&X-Amz-Signature=synthetic-signature#token=synthetic-fragment")
        source = prepare_sources([article(url=url, sources=[{
            "source_id": "other-rss", "source_name": "Another publisher", "url": url,
            "title": "Same story", "content_scope": "summary",
        }])])[0]
        self.assertEqual(source["url"], "https://example.org/story?q=financing&page=2")
        appendix = format_sources_appendix([source])
        for secret in (KEY, OTHER_KEY, "synthetic-signature", "synthetic-fragment"):
            self.assertNotIn(secret, json.dumps(source))
            self.assertNotIn(secret, appendix)

    def test_appendix_uses_real_snapshot_fields_and_escapes_metadata_markup(self):
        source = prepare_sources([article(title="A [S999] title", source_name="Publisher **markup**")])[0]
        appendix = format_sources_appendix([source])
        self.assertIn("[S1]", appendix)
        self.assertIn("https://example.org/story", appendix)
        self.assertIn("A \\[S999\\] title", appendix)
        self.assertIn("summary", appendix)
        self.assertNotIn("raw_metadata", appendix)

    def test_explicit_evidence_retains_separate_publication_and_observation_dates(self):
        original = prepare_sources([article()])
        before = copy.deepcopy(original)
        combined = prepare_evidence(original, [{
            "title": "Synthetic official table", "publisher": "Synthetic central bank",
            "url": "https://example.org/official/table", "publication_date": "2026-10-13",
            "observation_date": "2026-09-30", "content": "September net financing was 8 billion.",
            "source_type": "official", "api_key": OTHER_KEY,
        }, {"title": "Manual excerpt", "content": "A user supplied observation.", "source_type": "manual"}])
        self.assertEqual([item["id"] for item in combined], ["S1", "S2", "S3"])
        self.assertEqual(combined[1]["publication_date"], "2026-10-13")
        self.assertEqual(combined[1]["observation_date"], "2026-09-30")
        self.assertEqual(combined[1]["content_scope"], "official_evidence")
        self.assertEqual(combined[2]["content_scope"], "manual_evidence")
        self.assertEqual(original, before)
        self.assertNotIn(OTHER_KEY, json.dumps(combined))

    def test_evidence_can_be_used_without_news_and_bad_evidence_is_rejected(self):
        self.assertEqual(prepare_evidence([], [{"title": "Manual", "content": "Evidence.", "source_type": "manual"}])[0]["id"], "S1")
        for item in ({"title": "Missing content", "source_type": "manual"},
                     {"title": "Unsupported", "content": "Text", "source_type": "automatic_check"},
                     {"title": "Bad date", "content": "Text", "source_type": "official", "publication_date": "secret-invalid-date"}):
            with self.subTest(item=item), self.assertRaises(ResearchError) as caught:
                prepare_evidence([], [item])
            self.assertNotIn("secret-invalid-date", str(caught.exception))


class ArticleFilterTests(unittest.TestCase):
    def test_hong_kong_day_is_inclusive_and_unknown_dates_are_excluded(self):
        articles = [article(id="early", published_at="2026-10-13T15:59:59Z"),
                    article(id="start", published_at="2026-10-13T16:00:00Z"),
                    article(id="end", published_at="2026-10-14T15:59:59Z"),
                    article(id="late", published_at="2026-10-14T16:00:00Z"),
                    article(id="unknown", published_at=None)]
        selected = filter_articles(articles, start_date="2026-10-14", end_date="2026-10-14")
        self.assertEqual([item["id"] for item in selected], ["start", "end"])

    def test_comma_keywords_match_financial_traditional_simplified_variants(self):
        articles = [article(id="chinese", title="美聯儲資金流動與機構持倉"),
                    article(id="english", title="Gross FINANCING rose"),
                    article(id="none", title="Other news", summary="Unrelated excerpt")]
        selected = filter_articles(articles, keywords="美联储资金， financing")
        self.assertEqual([item["id"] for item in selected], ["chinese", "english"])

    def test_invalid_or_reversed_dates_have_safe_error(self):
        for start, end in (("sensitive-invalid-date", None), ("2026-10-15", "2026-10-14"),
                           ("9999-12-31T23:59:59Z", None), ("0001-01-01T00:00:00+23:59", None)):
            with self.subTest(start=start), self.assertRaises(ResearchError) as caught:
                filter_articles([article()], start_date=start, end_date=end)
            self.assertNotIn("sensitive", str(caught.exception))


class ResearchReportTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"OPENAI_API_KEY": OTHER_KEY, "OPENAI_BASE_URL": "https://untrusted.example"}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sources = prepare_sources([article()])

    def generate(self, client, **overrides):
        arguments = {"api_key": KEY, "as_of": "2026-10-14", "client": client}
        arguments.update(overrides)
        return generate_research_report("Synthetic research topic", self.sources, **arguments)

    def test_valid_stream_renders_required_sections_and_snapshot_appendix(self):
        client, stream = fake_client()
        result = self.generate(client)
        self.assertTrue(stream.closed)
        self.assertEqual(result["title"], "A synthetic research draft")
        for heading in ("核心結論", "圖表與數據解讀", "差異與替代解釋", "條件式傳導機制", "後續觀察清單", "限制與資料缺口", "來源附錄"):
            self.assertIn(heading, result["body"])
        self.assertIn("AI 研究草稿", result["body"])
        self.assertIn("尚未獨立核實", result["body"])
        self.assertIn("summary", result["body"])
        self.assertIn("https://example.org/story", result["body"])
        self.assertEqual(result["metadata"], {"provider": "deepseek", "model": "deepseek-v4-pro", "report_scope": "rss_investat_only"})
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn(OTHER_KEY, json.dumps(result))
        request = client.chat.completions.create.call_args.kwargs
        self.assertTrue(request["stream"])
        self.assertEqual(request["max_tokens"], MAX_REPORT_TOKENS)
        self.assertEqual(request["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertNotIn(KEY, json.dumps(request))
        client.chat.completions.create.assert_called_once()

    def test_progress_tracks_stream_lifecycle_without_response_text_or_credentials(self):
        client, stream = fake_client()
        events = []
        result = self.generate(client, on_progress=lambda stage, size: events.append((stage, size)))
        self.assertEqual(events[:2], [("preparing", 0), ("requesting", 0)])
        receiving = [(stage, size) for stage, size in events if stage == "receiving"]
        self.assertGreater(len(receiving), 1)
        counts = [size for _, size in receiving]
        self.assertEqual(counts, sorted(set(counts)))
        self.assertEqual(counts[-1], len(stream.raw))
        self.assertEqual(events[-2:], [("validating", len(stream.raw)), ("ready", len(stream.raw))])
        self.assertTrue(all(isinstance(stage, str) and isinstance(size, int) for stage, size in events))
        serialized = json.dumps(events)
        for sensitive in (KEY, OTHER_KEY, stream.raw, self.sources[0]["content"]):
            self.assertNotIn(sensitive, serialized)
        self.assertEqual(os.environ["OPENAI_API_KEY"], OTHER_KEY)
        self.assertEqual(os.environ["OPENAI_BASE_URL"], "https://untrusted.example")
        self.assertEqual(result["title"], payload()["title"])
        self.assertTrue(stream.closed)

    def test_invalid_response_progress_never_signals_ready_or_exposes_model_text(self):
        client, stream = fake_client(raw=KEY + " malformed-provider-response")
        events = []
        with self.assertRaises(ModelResponseError):
            self.generate(client, on_progress=lambda stage, size: events.append((stage, size)))
        self.assertEqual(events[-1], ("validating", len(stream.raw)))
        self.assertNotIn("ready", [stage for stage, _ in events])
        self.assertNotIn(KEY, json.dumps(events))
        self.assertTrue(stream.closed)

    def test_request_timeouts_have_specific_safe_error_and_close_owned_client(self):
        errors = (TimeoutError(KEY), ReadTimeout(KEY), APITimeoutError(request=SimpleNamespace()))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                client, _ = fake_client()
                client.chat.completions.create.side_effect = error
                events = []
                with patch("src.research.OpenAI", return_value=client):
                    with self.assertRaises(ResearchError) as caught:
                        self.generate(None, on_progress=lambda stage, size: events.append((stage, size)))
                self.assertEqual(caught.exception.reason, "generation_timeout")
                # Shared diagnostics also serve configurable deep-research
                # budgets; they must describe timeout without a fixed limit.
                self.assertIn("逾時", str(caught.exception))
                self.assertIn("時限", str(caught.exception))
                self.assertNotIn(KEY, str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertEqual(events, [("preparing", 0), ("requesting", 0)])
                client.close.assert_called_once()

    def test_claude_sanitized_transport_timeout_is_recognized_through_wrapper(self):
        events = []
        with patch("src.llm_providers.requests.post", side_effect=ReadTimeout(KEY)):
            with self.assertRaises(ResearchError) as caught:
                self.generate(None, provider="anthropic", on_progress=lambda stage, size: events.append((stage, size)))
        self.assertEqual(caught.exception.reason, "generation_timeout")
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual(events, [("preparing", 0), ("requesting", 0)])

    def test_stream_timeout_closes_stream_without_validation_or_ready_progress(self):
        class TimedOutStream:
            closed = False

            def __iter__(self):
                yield SimpleNamespace(choices=[SimpleNamespace(finish_reason=None, delta=SimpleNamespace(content="partial"))])
                raise ReadTimeout(KEY)

            def close(self):
                self.closed = True

        client = MagicMock()
        stream = TimedOutStream()
        client.chat.completions.create.return_value = stream
        events = []
        with self.assertRaises(ResearchError) as caught:
            self.generate(client, on_progress=lambda stage, size: events.append((stage, size)))
        self.assertEqual(caught.exception.reason, "generation_timeout")
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual(events[-1], ("receiving", len("partial")))
        self.assertNotIn("validating", [stage for stage, _ in events])
        self.assertNotIn("ready", [stage for stage, _ in events])
        self.assertTrue(stream.closed)

    def test_stream_deadline_has_specific_timeout_error_and_preserves_cleanup(self):
        client, stream = fake_client()
        events = []
        with patch("src.research.time.monotonic", side_effect=[0.0, 181.0]):
            with self.assertRaises(ResearchError) as caught:
                self.generate(client, on_progress=lambda stage, size: events.append((stage, size)))
        self.assertEqual(caught.exception.reason, "generation_timeout")
        self.assertEqual(events, [("preparing", 0), ("requesting", 0)])
        self.assertTrue(stream.closed)

    def test_claude_progress_reports_stage_and_count_after_native_response(self):
        raw = json.dumps(payload())
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=raw))])
        events = []
        with patch("src.research.claude_completion", return_value=response):
            self.generate(None, provider="anthropic", on_progress=lambda stage, size: events.append((stage, size)))
        self.assertEqual(events, [("preparing", 0), ("requesting", 0), ("receiving", len(raw)),
                                  ("validating", len(raw)), ("ready", len(raw))])

    def test_only_explicit_evidence_switches_report_mode(self):
        self.sources = prepare_evidence(self.sources, [{"title": "Manual", "content": "A manual excerpt.", "source_type": "manual"}])
        client, _ = fake_client(payload(executive_findings="Manual evidence was supplied. [S2]"))
        result = self.generate(client)
        self.assertEqual(result["metadata"]["report_scope"], "with_explicit_evidence")
        self.assertIn("manual\\_evidence", result["body"])

    def test_factory_pins_provider_endpoint_key_and_parameter_isolation(self):
        for provider in ("deepseek", "openai", "xai", "gemini"):
            with self.subTest(provider=provider):
                client, _ = fake_client()
                with patch("src.research.OpenAI", return_value=client) as factory:
                    result = generate_research_report("Synthetic topic", self.sources,
                                                      api_key="  " + KEY + "  ", provider=provider, as_of="2026-10-14")
                factory.assert_called_once_with(api_key=KEY, base_url=PROVIDERS[provider].base_url, timeout=60.0, max_retries=0)
                request = client.chat.completions.create.call_args.kwargs
                self.assertEqual(request["model"], PROVIDERS[provider].models[0])
                self.assertTrue(request["stream"])
                if provider == "openai":
                    self.assertEqual(request["max_completion_tokens"], MAX_REPORT_TOKENS)
                    self.assertNotIn("temperature", request)
                    self.assertNotIn("max_tokens", request)
                if provider != "deepseek":
                    self.assertNotIn("extra_body", request)
                client.close.assert_called_once()
                self.assertEqual(set(result["metadata"]), {"provider", "model", "report_scope"})
        self.assertEqual(os.environ["OPENAI_API_KEY"], OTHER_KEY)
        self.assertEqual(os.environ["OPENAI_BASE_URL"], "https://untrusted.example")

    def test_cancel_hooks_register_owned_client_before_request_and_stream_before_read(self):
        callbacks = []
        client = MagicMock()
        test = self

        class CheckedStream(FakeStream):
            def __iter__(self):
                test.assertEqual(callbacks, [client.close, self.close])
                yield from super().__iter__()

        stream = CheckedStream(json.dumps(payload()))

        def create(**kwargs):
            self.assertEqual(callbacks, [client.close])
            return stream

        client.chat.completions.create.side_effect = create
        with patch("src.research.OpenAI", return_value=client):
            result = self.generate(None, register_cancel=callbacks.append)
        self.assertEqual(callbacks, [client.close, stream.close])
        self.assertEqual(result["title"], payload()["title"])
        self.assertTrue(stream.closed)
        client.close.assert_called_once()

    def test_cancel_hook_registers_injected_stream_without_taking_client_ownership(self):
        client, stream = fake_client()
        callbacks = []
        self.generate(client, register_cancel=callbacks.append)
        self.assertEqual(callbacks, [stream.close])
        self.assertTrue(stream.closed)
        client.close.assert_not_called()

    def test_cancel_during_client_construction_closes_resource_before_any_request(self):
        client, _ = fake_client()

        def already_cancelled(close):
            close()
            raise ResearchError("generation_timeout")

        with patch("src.research.OpenAI", return_value=client):
            with self.assertRaises(ResearchError) as caught:
                self.generate(None, register_cancel=already_cancelled)
        self.assertEqual(caught.exception.reason, "generation_timeout")
        client.chat.completions.create.assert_not_called()
        self.assertGreaterEqual(client.close.call_count, 1)

    def test_stream_cancel_registration_failure_still_closes_stream(self):
        client, stream = fake_client()

        def already_cancelled(close):
            close()
            raise ResearchError("generation_timeout")

        with self.assertRaises(ResearchError) as caught:
            self.generate(client, register_cancel=already_cancelled)
        self.assertEqual(caught.exception.reason, "generation_timeout")
        self.assertTrue(stream.closed)
        client.close.assert_not_called()

    def test_claude_reuses_native_adapter_with_safe_prompt(self):
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(payload())))])
        with patch("src.research.claude_completion", return_value=response) as adapter, patch("src.research.OpenAI") as factory:
            result = self.generate(None, provider="anthropic")
        factory.assert_not_called()
        arguments = adapter.call_args.kwargs
        self.assertEqual(arguments["api_key"], KEY)
        self.assertEqual(arguments["model"], PROVIDERS["anthropic"].models[0])
        self.assertNotIn(KEY, arguments["user"])
        self.assertEqual(result["metadata"]["provider"], "anthropic")

    def test_missing_key_model_and_empty_sources_fail_before_request(self):
        client, _ = fake_client()
        for key in (None, "", " ", "a\nb"):
            with self.subTest(key=key), self.assertRaises(ProviderConfigurationError):
                self.generate(client, api_key=key)
        with self.assertRaises(ProviderConfigurationError):
            self.generate(client, model="claude-synthetic")
        with self.assertRaises(ResearchError):
            generate_research_report("Topic", [], api_key=KEY, as_of="2026-10-14", client=client)
        client.chat.completions.create.assert_not_called()

    def test_duplicate_or_metadata_only_source_ids_are_rejected(self):
        client, _ = fake_client()
        for bad in ([self.sources[0], self.sources[0]], [dict(self.sources[0], content_scope="title_only")],
                    [dict(self.sources[0], id="untrusted-id")]):
            with self.subTest(bad=bad), self.assertRaises(ResearchError):
                generate_research_report("Topic", bad, api_key=KEY, as_of="2026-10-14", client=client)
        client.chat.completions.create.assert_not_called()

    def test_unknown_and_malformed_source_refs_have_distinct_safe_errors(self):
        for citation, reason in (("[S999]", "unknown_citations"), ("[S0]", "unknown_citations"),
                                 ("[S1, S2]", "unknown_citations"), ("[S999", "unknown_citations"),
                                 ("S999", "unknown_citations"), ("S1", "invalid_citations"),
                                 ("[S1-S1]", "invalid_citations")):
            with self.subTest(citation=citation):
                client, _ = fake_client(payload(executive_findings="Claim. " + citation))
                with self.assertRaises(ResearchError) as caught:
                    self.generate(client, repair_citations=False)
                self.assertEqual(caught.exception.reason, reason)
                self.assertNotIn("S999", str(caught.exception))
                client.chat.completions.create.assert_called_once()

    def test_explicit_grouped_and_localized_citations_are_normalized_without_another_request(self):
        self.sources = prepare_sources([article(), article(id="news-2", summary="A second supplied excerpt.")])
        for citation in ("[S1, S2]", "[S1、S2]", "[S1，S2]", "[S1；S2]", "[S1 S2]",
                         "［Ｓ１，Ｓ２］", "【S1、S2】", "[s1, s2]", "[S 1, S 2]"):
            with self.subTest(citation=citation):
                client, _ = fake_client(payload(executive_findings="Supplied evidence. " + citation))
                result = self.generate(client)
                self.assertIn("Supplied evidence. [S1][S2]", result["body"])
                client.chat.completions.create.assert_called_once()
                self.assertNotIn("citation_repaired", result["metadata"])
        client, _ = fake_client(payload(executive_findings="Supplied evidence. [s1] ［S 2］"))
        self.assertIn("Supplied evidence. [S1] [S2]", self.generate(client)["body"])
        client.chat.completions.create.assert_called_once()

    def test_normalization_preserves_unknown_ids_instead_of_remapping_them(self):
        for citation in ("[S01]", "［Ｓ９９９］", "【S1、S999】", "Ｓ９９９. [S1]"):
            with self.subTest(citation=citation):
                client, _ = fake_client(payload(executive_findings="Claim. " + citation))
                with self.assertRaises(ResearchError) as caught:
                    self.generate(client, repair_citations=False)
                self.assertEqual(caught.exception.reason, "unknown_citations")

    def test_structural_headings_do_not_need_citations_but_factual_headings_do(self):
        client, _ = fake_client(payload(executive_findings=(
            "### 原文事實\n\nReported financing of 20 billion. [S1]\n\n"
            "### 分析推論\nThe excerpt cannot determine net flows. [S1]")))
        result = self.generate(client)
        self.assertIn("### 原文事實", result["body"])
        client.chat.completions.create.assert_called_once()
        for heading in ("### Financing was 500 billion.", "### 所有融資均流入股票市場", "### 原文事實"):
            with self.subTest(heading=heading):
                # A heading-only section is insufficient even if its label is structural.
                client, _ = fake_client(payload(executive_findings=heading))
                with self.assertRaises(ResearchError) as caught:
                    self.generate(client, repair_citations=False)
                self.assertEqual(caught.exception.reason, "missing_citations")

    def test_heading_boundaries_cannot_merge_uncited_claims_with_a_cited_block(self):
        for content in ("Uncited claim.\n### 原文事實\nCited excerpt. [S1]",
                        "### 所有融資均流入股票市場\nCited excerpt. [S1]",
                        "### 融資升至500億元\nCited excerpt. [S1]"):
            with self.subTest(content=content):
                client, _ = fake_client(payload(executive_findings=content))
                with self.assertRaises(ResearchError) as caught:
                    self.generate(client, repair_citations=False)
                self.assertEqual(caught.exception.reason, "missing_citations")
                self.assertIn("核心結論，第 1 段", str(caught.exception))
                self.assertNotIn("Uncited claim", str(caught.exception))
                self.assertNotIn("所有融資", str(caught.exception))

    def test_one_citation_repair_uses_same_sources_provider_schema_and_safe_progress(self):
        first = FakeStream(json.dumps(payload(executive_findings="Uncited factual paragraph.")))
        second = FakeStream(json.dumps(payload()))
        client = MagicMock()
        client.chat.completions.create.side_effect = [first, second]
        events, callbacks = [], []
        original_sources = copy.deepcopy(self.sources)
        result = self.generate(client, on_progress=lambda stage, size: events.append((stage, size)),
                               register_cancel=callbacks.append)
        self.assertTrue(result["metadata"]["citation_repaired"])
        self.assertEqual(client.chat.completions.create.call_count, 2)
        requests = [call.kwargs for call in client.chat.completions.create.call_args_list]
        initial, repair = [json.loads(request["messages"][1]["content"]) for request in requests]
        self.assertEqual(initial["sources"], repair["sources"])
        self.assertEqual(self.sources, original_sources)
        self.assertEqual(repair["citation_repair"]["failure"], "missing_citations")
        self.assertEqual(repair["citation_repair"]["draft"]["executive_findings"], "Uncited factual paragraph.")
        self.assertEqual(requests[0]["model"], requests[1]["model"])
        self.assertEqual(requests[1]["response_format"], {"type": "json_object"})
        self.assertIn("不得猜測未知來源編號", requests[1]["messages"][0]["content"])
        self.assertEqual(callbacks, [first.close, second.close])
        self.assertTrue(first.closed and second.closed)
        client.close.assert_not_called()
        repair_start = [stage for stage, _ in events].index("repairing")
        self.assertTrue(all(stage == "repairing" for stage, _ in events[repair_start:-1]))
        self.assertEqual(events[-1], ("ready", len(second.raw)))
        for secret in (KEY, OTHER_KEY, first.raw, second.raw, self.sources[0]["content"]):
            self.assertNotIn(secret, json.dumps(events))
        self.assertNotIn(KEY, json.dumps(requests))
        self.assertNotIn(OTHER_KEY, json.dumps(requests))

    def test_failed_citation_repair_does_not_try_a_third_request_or_signal_ready(self):
        first = FakeStream(json.dumps(payload(executive_findings="Claim. [S999]")))
        second = FakeStream(json.dumps(payload(executive_findings="Still uncited.")))
        client = MagicMock()
        client.chat.completions.create.side_effect = [first, second]
        events = []
        with self.assertRaises(ResearchError) as caught:
            self.generate(client, on_progress=lambda stage, size: events.append((stage, size)))
        self.assertEqual(caught.exception.reason, "missing_citations")
        self.assertIn("核心結論，第 1 段", str(caught.exception))
        self.assertIn("已嘗試一次自動修正", str(caught.exception))
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertTrue(first.closed and second.closed)
        self.assertNotIn("ready", [stage for stage, _ in events])

    def test_repaired_response_must_pass_all_link_and_shape_validation(self):
        for repaired, error_type, reason in ((payload(watchlist="Read https://invented.example/table. [S1]"),
                                             ResearchError, "invented_url"),
                                            (payload(title=7), ModelResponseError, "invalid_schema"),
                                            (payload(watchlist="Claim. [S999]"), ResearchError, "unknown_citations")):
            with self.subTest(reason=reason):
                first = FakeStream(json.dumps(payload(executive_findings="Uncited claim.")))
                second = FakeStream(json.dumps(repaired))
                client = MagicMock()
                client.chat.completions.create.side_effect = [first, second]
                with self.assertRaises(error_type) as caught:
                    self.generate(client)
                self.assertEqual(caught.exception.reason, reason)
                self.assertEqual(client.chat.completions.create.call_count, 2)
                self.assertTrue(first.closed and second.closed)

    def test_invented_url_even_with_bad_citations_is_not_a_repairable_failure(self):
        client, _ = fake_client(payload(watchlist="Read https://invented.example/table. [S999]"))
        with self.assertRaises(ResearchError) as caught:
            self.generate(client)
        self.assertEqual(caught.exception.reason, "invented_url")
        client.chat.completions.create.assert_called_once()

    def test_transport_failures_never_trigger_a_citation_retry(self):
        client, _ = fake_client()
        client.chat.completions.create.side_effect = RuntimeError(KEY + " private body")
        with self.assertRaises(ProviderAPIError):
            self.generate(client)
        client.chat.completions.create.assert_called_once()
        first = FakeStream(json.dumps(payload(executive_findings="Uncited claim.")))
        client.chat.completions.create.reset_mock()
        client.chat.completions.create.side_effect = [first, ReadTimeout(KEY)]
        with self.assertRaises(ResearchError) as caught:
            self.generate(client)
        self.assertEqual(caught.exception.reason, "generation_timeout")
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertNotIn(KEY, str(caught.exception))

    def test_repair_retains_owned_client_until_done_and_registers_both_streams(self):
        client = MagicMock()
        first = FakeStream(json.dumps(payload(executive_findings="Uncited claim.")))
        second = FakeStream(json.dumps(payload()))
        callbacks = []

        def create(**_kwargs):
            client.close.assert_not_called()
            return first if client.chat.completions.create.call_count == 1 else second

        client.chat.completions.create.side_effect = create
        with patch("src.research.OpenAI", return_value=client):
            result = self.generate(None, register_cancel=callbacks.append)
        self.assertTrue(result["metadata"]["citation_repaired"])
        self.assertEqual(callbacks, [client.close, first.close, second.close])
        client.close.assert_called_once()

    def test_repair_uses_remaining_shared_deadline_instead_of_resetting_it(self):
        clock = {"now": 0.0}

        class SlowFirstStream(FakeStream):
            def __iter__(self):
                yield from super().__iter__()
                clock["now"] = 179.0

        class LateRepairStream(FakeStream):
            def __iter__(self):
                clock["now"] = 181.0
                yield from super().__iter__()

        first = SlowFirstStream(json.dumps(payload(executive_findings="Uncited claim.")))
        second = LateRepairStream(json.dumps(payload()))
        client = MagicMock()
        client.chat.completions.create.side_effect = [first, second]
        with patch("src.research.time.monotonic", side_effect=lambda: clock["now"]):
            with self.assertRaises(ResearchError) as caught:
                self.generate(client)
        self.assertEqual(caught.exception.reason, "generation_timeout")
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(client.chat.completions.create.call_args.kwargs["timeout"], 1.0)
        self.assertTrue(first.closed and second.closed)

    def test_late_stream_termination_and_late_validation_never_return_a_draft(self):
        clock = {"now": 0.0}

        class LateTermination(FakeStream):
            def __iter__(self):
                yield from super().__iter__()
                clock["now"] = 181.0

        client = MagicMock()
        stream = LateTermination(json.dumps(payload()))
        client.chat.completions.create.return_value = stream
        with patch("src.research.time.monotonic", side_effect=lambda: clock["now"]):
            with self.assertRaises(ResearchError) as caught:
                self.generate(client)
        self.assertEqual(caught.exception.reason, "generation_timeout")
        self.assertTrue(stream.closed)
        from src.research import _parse_draft
        client, _ = fake_client()
        clock["now"] = 0.0

        def late_parse(*args):
            result = _parse_draft(*args)
            clock["now"] = 181.0
            return result

        with patch("src.research.time.monotonic", side_effect=lambda: clock["now"]), \
             patch("src.research._parse_draft", side_effect=late_parse):
            with self.assertRaises(ResearchError) as caught:
                self.generate(client)
        self.assertEqual(caught.exception.reason, "generation_timeout")
        client.chat.completions.create.assert_called_once()

    def test_repair_prompt_budget_is_checked_before_a_second_request(self):
        client, _ = fake_client()
        self.generate(client)
        request = client.chat.completions.create.call_args.kwargs
        initial_size = sum(len(message["content"]) for message in request["messages"])
        client, _ = fake_client(payload(executive_findings="Uncited claim."))
        with patch("src.research.MAX_PROMPT_CHARS", initial_size):
            with self.assertRaises(ResearchError) as caught:
                self.generate(client)
        self.assertEqual(caught.exception.reason, "missing_citations")
        client.chat.completions.create.assert_called_once()

    def test_claude_repair_respects_existing_adapter_and_remaining_timeout_budget(self):
        first = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
            content=json.dumps(payload(executive_findings="Uncited claim."))))])
        second = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
            content=json.dumps(payload())))])
        with patch("src.research.claude_completion", side_effect=[first, second]) as adapter:
            result = self.generate(None, provider="anthropic")
        self.assertEqual(adapter.call_count, 2)
        self.assertTrue(result["metadata"]["citation_repaired"])
        self.assertEqual(adapter.call_args.kwargs["api_key"], KEY)
        with patch("src.research.claude_completion", return_value=first) as adapter, \
             patch("src.research.time.monotonic", side_effect=[0.0, 81.0, 81.0]):
            with self.assertRaises(ResearchError) as caught:
                self.generate(None, provider="anthropic")
        self.assertEqual(caught.exception.reason, "missing_citations")
        adapter.assert_called_once()

    def test_evidence_only_report_body_describes_actual_scope(self):
        self.sources = prepare_evidence([], [{"title": "Manual", "content": "A supplied excerpt.",
                                            "source_type": "manual"}])
        client, _ = fake_client()
        result = self.generate(client)
        self.assertEqual(result["metadata"]["report_scope"], "with_explicit_evidence")
        self.assertIn("未選入 RSS／Investat 新聞", result["body"])

    def test_report_with_no_citations_is_rejected(self):
        client, _ = fake_client({key: value.replace(" [S1]", "") for key, value in payload().items()})
        with self.assertRaises(ResearchError) as caught:
            self.generate(client)
        self.assertEqual(caught.exception.reason, "missing_citations")

    def test_number_in_uncited_paragraph_is_rejected(self):
        client, _ = fake_client(payload(chart_data_interpretation="Uncited financing was 500 billion.\n\nThe cited excerpt is partial. [S1]"))
        with self.assertRaises(ResearchError) as caught:
            self.generate(client)
        self.assertEqual(caught.exception.reason, "missing_citations")

    def test_literal_number_mismatches_trigger_review_without_claiming_factcheck(self):
        for claim in ("Financing was 500 billion. [S1]", "融資五百億元。 [S1]"):
            with self.subTest(claim=claim):
                client, _ = fake_client(payload(chart_data_interpretation=claim))
                result = self.generate(client)
                self.assertTrue(result["metadata"]["review_warnings"])
                self.assertIn("數字核對提示", result["body"])
                self.assertIn("尚未獨立核實", result["body"])

    def test_explicit_unit_conversion_is_draft_with_review_warning(self):
        self.sources = prepare_sources([article(summary="Balance was 1,453,832 USDm during September.")])
        client, _ = fake_client(payload(
            executive_findings="Balance was 1,453,832 USDm in September. [S1]",
            chart_data_interpretation="Calculated conversion: 1,453,832 USDm / 1,000,000 = 1.453832 trillion USD for the same period. [S1]"))
        result = self.generate(client)
        self.assertIn("1.453832", result["body"])
        self.assertTrue(result["metadata"]["review_warnings"])

    def test_nonnumeric_or_chinese_number_uncited_claims_are_rejected(self):
        for claim in ("An uncited factual paragraph.", "融資五百億元。"):
            with self.subTest(claim=claim):
                client, _ = fake_client(payload(chart_data_interpretation=claim + "\n\nThe excerpt is partial. [S1]"))
                with self.assertRaises(ResearchError) as caught:
                    self.generate(client)
                self.assertEqual(caught.exception.reason, "missing_citations")

    def test_uncited_plus_and_parenthesis_list_items_are_rejected(self):
        for claim in ("1) Cited claim. [S1]\n2) An uncited factual claim.",
                      "+ Cited claim. [S1]\n+ An uncited factual claim.",
                      "1、Cited claim. [S1]\n2、An uncited factual claim."):
            with self.subTest(claim=claim):
                client, _ = fake_client(payload(chart_data_interpretation=claim))
                with self.assertRaises(ResearchError):
                    self.generate(client)

    def test_model_cannot_add_a_source_url_outside_snapshot(self):
        client, _ = fake_client(payload(watchlist="Read [invented source](https://invented.example/table). [S1]"))
        with self.assertRaises(ResearchError) as caught:
            self.generate(client)
        self.assertEqual(caught.exception.reason, "invented_url")
        self.assertNotIn("invented.example", str(caught.exception))

    def test_relative_ftp_and_html_links_cannot_bypass_snapshot_urls(self):
        for link in ("[table](//invented.example/table)", "[table](ftp://invented.example/table)",
                     "[table](relative/table)", '<a href="//invented.example/table">table</a>',
                     "[table][t]. [S1]\n[t]: //invented.example/table", "<ftp://invented.example/table>",
                     "<a href=//invented.example/table>table</a>"):
            with self.subTest(link=link):
                client, _ = fake_client(payload(watchlist="Check " + link + ". [S1]"))
                with self.assertRaises(ResearchError) as caught:
                    self.generate(client)
                self.assertEqual(caught.exception.reason, "invented_url")

    def test_claude_requires_a_successful_terminal_reason(self):
        for reason in (None, "tool_calls", "content_filter"):
            response = SimpleNamespace(choices=[SimpleNamespace(finish_reason=reason, message=SimpleNamespace(content=json.dumps(payload())))])
            with self.subTest(reason=reason), patch("src.research.claude_completion", return_value=response):
                with self.assertRaises(ModelResponseError) as caught:
                    self.generate(None, provider="anthropic")
                self.assertEqual(caught.exception.reason, "invalid_shape")

    def test_prompt_sets_evidence_boundaries_without_claiming_factchecking(self):
        client, _ = fake_client()
        self.generate(client)
        request = client.chat.completions.create.call_args.kwargs
        system = request["messages"][0]["content"]
        user = json.loads(request["messages"][1]["content"])
        for instruction in ("模型記憶", "淨額", "資金流", "持倉", "觀察期間", "新聞篇數", "不能聲稱已完成核查"):
            self.assertIn(instruction, system)
        self.assertEqual(user["sources"][0]["content_scope"], "summary")
        self.assertEqual(user["as_of_hong_kong_date"], "2026-10-14")
        self.assertEqual(user["sources"][0]["content"], self.sources[0]["content"])

    def test_empty_malformed_shape_schema_and_extra_fields_have_safe_errors(self):
        cases = [("", "empty_content"), ("  ", "empty_content"),
                 (KEY + " malformed-provider-response", "invalid_json"),
                 ("[]", "invalid_shape"), ("null", "invalid_shape"),
                 ("{}", "invalid_schema"),
                 (json.dumps(payload(title=7)), "invalid_schema"),
                 (json.dumps(payload(limitations=" ")), "invalid_schema"),
                 ("[" * 19000 + "0" + "]" * 19000, "invalid_json"),
                 (json.dumps(dict(payload(), invented_sources=[{"url": "https://invented.example"}])), "invalid_schema")]
        for raw, reason in cases:
            with self.subTest(reason=reason, raw=raw):
                client, stream = fake_client(raw=raw)
                with self.assertRaises(ModelResponseError) as caught:
                    self.generate(client)
                self.assertEqual(caught.exception.reason, reason)
                self.assertNotIn(KEY, str(caught.exception))
                self.assertNotIn("malformed-provider-response", str(caught.exception))
                self.assertTrue(stream.closed)

    def test_length_finish_missing_terminal_and_size_limit_are_truncated(self):
        for options in ({"reason": "length"}, {"end": False}, {"raw": "x" * (MAX_RESPONSE_CHARS + 1)}):
            with self.subTest(options={key: len(value) if key == "raw" else value for key, value in options.items()}):
                client, stream = fake_client(**options)
                with self.assertRaises(ModelResponseError) as caught:
                    self.generate(client)
                self.assertEqual(caught.exception.reason, "truncated")
                self.assertTrue(stream.closed)

    def test_provider_error_body_headers_and_key_are_not_exposed(self):
        client, _ = fake_client()
        raw_error = RuntimeError(KEY + " secret-response-body authorization-header")
        raw_error.status_code = 401
        client.chat.completions.create.side_effect = raw_error
        with self.assertRaises(ProviderAPIError) as caught:
            self.generate(client)
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(str(caught.exception), "Provider request failed")
        self.assertTrue(caught.exception.__suppress_context__)

    def test_stream_iteration_error_is_sanitized_and_closed(self):
        class FailedStream:
            closed = False

            def __iter__(self):
                raise RuntimeError(KEY + " sensitive-request-headers")

            def close(self):
                self.closed = True

        client = MagicMock()
        stream = FailedStream()
        client.chat.completions.create.return_value = stream
        with self.assertRaises(ProviderAPIError) as caught:
            self.generate(client)
        self.assertNotIn(KEY, str(caught.exception))
        self.assertTrue(stream.closed)

    def test_installed_sdk_stream_roundtrip_uses_only_mock_http_transport(self):
        from openai import OpenAI
        import inspect
        transport_module = "httpx2" if "httpx2" in str(inspect.signature(OpenAI)) else "httpx"
        http = __import__(transport_module)
        captured = []

        def handle_request(request):
            self.assertEqual(str(request.url), "https://api.deepseek.com/chat/completions")
            self.assertEqual(request.headers["authorization"], "Bearer " + KEY)
            captured.append(json.loads(request.content))
            events = [
                {"id": "synthetic-report", "object": "chat.completion.chunk", "created": 1,
                 "model": "deepseek-v4-pro", "choices": [{"index": 0, "finish_reason": None,
                 "delta": {"content": json.dumps(payload())}}]},
                {"id": "synthetic-report", "object": "chat.completion.chunk", "created": 1,
                 "model": "deepseek-v4-pro", "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]},
            ]
            body = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
            return http.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

        with http.Client(transport=http.MockTransport(handle_request)) as http_client, \
             OpenAI(api_key=KEY, base_url=PROVIDERS["deepseek"].base_url,
                    http_client=http_client, max_retries=0) as client:
            result = self.generate(client)
        self.assertEqual(result["title"], payload()["title"])
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0]["stream"])
        self.assertEqual(captured[0]["max_tokens"], MAX_REPORT_TOKENS)
        self.assertNotIn(KEY, json.dumps(captured))

    def test_injected_sdk_client_cannot_change_provider_endpoint_or_key_owner(self):
        from openai import OpenAI
        import inspect
        transport_module = "httpx2" if "httpx2" in str(inspect.signature(OpenAI)) else "httpx"
        http = __import__(transport_module)
        for endpoint, key in (("https://untrusted.example", KEY), (PROVIDERS["deepseek"].base_url, OTHER_KEY)):
            with self.subTest(endpoint=endpoint), http.Client(transport=http.MockTransport(
                lambda _: self.fail("Unexpected external request"))) as http_client, \
                 OpenAI(api_key=key, base_url=endpoint, http_client=http_client) as client:
                with patch.object(client.chat.completions, "create") as create:
                    with self.assertRaises(ProviderConfigurationError):
                        self.generate(client)
                    create.assert_not_called()

    def test_unknown_nested_metadata_is_not_forwarded_in_sources(self):
        source = dict(self.sources[0], publisher={"api_key": OTHER_KEY},
                      supplied_character_count={"secret": OTHER_KEY}, verification_status="verified")
        source["origins"] = [dict(source["origins"][0], raw_metadata={"api_key": OTHER_KEY})]
        client, _ = fake_client()
        generate_research_report("Topic", [source], api_key=KEY, as_of="2026-10-14", client=client)
        request = client.chat.completions.create.call_args.kwargs
        user = json.loads(request["messages"][1]["content"])
        self.assertNotIn(OTHER_KEY, json.dumps(user))
        self.assertEqual(user["sources"][0]["verification_status"], "not_independently_verified")

    def test_news_and_manual_evidence_url_secrets_never_reach_prompt_or_body(self):
        sensitive_url = "https://example.org/story?q=safe&token=" + OTHER_KEY + "&key=" + KEY + "#access_token=fragment-secret"
        self.sources = prepare_evidence(prepare_sources([article(url=sensitive_url)]), [{
            "title": "Manual table", "content": "A user-supplied table.", "source_type": "manual", "url": sensitive_url,
        }])
        client, _ = fake_client()
        result = self.generate(client)
        request = client.chat.completions.create.call_args.kwargs
        for secret in (KEY, OTHER_KEY, "fragment-secret"):
            self.assertNotIn(secret, json.dumps(self.sources))
            self.assertNotIn(secret, json.dumps(request))
            self.assertNotIn(secret, json.dumps(result))
        self.assertIn("https://example.org/story?q=safe", result["body"])


if __name__ == "__main__":
    unittest.main()
