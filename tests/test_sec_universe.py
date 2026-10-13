from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.feeds import FeedFetchError
from src.sec_universe import (
    SEC_FORMS, SecConfigError, build_universe, collect_sec_filings, load_universe,
    normalize_symbol, parse_constituents, parse_sec_filings, parse_sec_tickers,
    refresh_universe, sec_feed_url, sec_user_agent,
)


# A fictional contact belongs only in injected test fixtures and is never sent.
AGENT = "fixture-reader contact analyst@fixture.invalid"


def table(index, count):
    labels = ("Symbol", "Security") if index == "sp500" else ("Ticker", "Company")
    rows = "".join(f"<tr><td>A{i}</td><td>Company {i}</td></tr>" for i in range(count))
    return (f"<html><table><tr><th>{labels[0]}</th><th>{labels[1]}</th></tr>{rows}</table></html>").encode()


def universe():
    mapping = {str(i): {"ticker": f"A{i}", "cik_str": i + 1} for i in range(503)}
    return build_universe(table("sp500", 503), table("nasdaq100", 101), mapping)


def entry(cik=1, form="8-K", key="one", *, link_cik=None, host="www.sec.gov"):
    path_cik = cik if link_cik is None else link_cik
    return (f'<entry><id>tag:sec.gov,2008:accession-number={key}</id>'
            f'<title>{form} - Company ({cik:010d}) (Filer)</title>'
            f'<link rel="alternate" href="https://{host}/Archives/edgar/data/{path_cik}/0001/{key}-index.htm"/>'
            '<updated>2026-10-13T09:30:00-04:00</updated>'
            '<summary type="html">Filing Date: 2026-10-13; form notice only</summary></entry>')


def atom(entries=""):
    return ('<feed xmlns="http://www.w3.org/2005/Atom"><title>Latest Filings</title>'
            + entries + '</feed>').encode()


def response(body, status=200):
    value = Mock(status_code=status)
    value.iter_content.return_value = [body]
    return value


class SecUniverseTests(unittest.TestCase):
    def test_membership_union_and_shared_cik_are_not_double_counted(self):
        data = universe()
        self.assertEqual(data["counts"], {"sp500": 503, "nasdaq100": 101, "symbols": 503, "companies": 503})
        self.assertEqual(data["members"][0]["indices"], ["sp500", "nasdaq100"])
        self.assertEqual(data["mapping_status"], "complete")
        self.assertIn("unofficial", data["sources"][0]["label"])
        self.assertEqual(len(data["sources"][0]["sha256"]), 64)
        mapping = parse_sec_tickers({"0": {"ticker": "GOOG", "cik_str": 1652044},
                                    "1": {"ticker": "GOOGL", "cik_str": 1652044}})
        self.assertEqual(mapping["GOOG"], mapping["GOOGL"])

    def test_truncated_duplicate_and_missing_membership_tables_fail(self):
        for body in (table("sp500", 10), table("sp500", 503) * 2,
                     table("sp500", 503).replace(b">A1<", b">A0<"), b"<html>Denied</html>"):
            with self.subTest(body=body[:20]):
                with self.assertRaises(SecConfigError):
                    parse_constituents(body, "sp500")

    def test_symbol_aliases_and_conflicting_official_mapping(self):
        self.assertEqual(normalize_symbol(" BRK.B "), "BRK-B")
        self.assertEqual(parse_sec_tickers({"0": {"ticker": "BRK-B", "cik_str": 1067983}})["BRK-B"], "0001067983")
        with self.assertRaises(SecConfigError):
            parse_sec_tickers({"0": {"ticker": "A", "cik_str": 1}, "1": {"ticker": "A", "cik_str": 2}})

    def test_contact_is_required_and_not_echoed(self):
        for value in ("", "reader", "reader test@example.com", "reader bad\ncontact@fixture.invalid"):
            with self.assertRaises(SecConfigError) as error:
                sec_user_agent(value)
            self.assertNotIn("@", str(error.exception))
        self.assertEqual(sec_user_agent(AGENT), AGENT)

    def test_pending_stale_and_truncated_universes_do_not_request_sec(self):
        pending = build_universe(table("sp500", 503), table("nasdaq100", 101))
        stale = universe()
        stale["fetched_at"] = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        truncated = universe()
        truncated["members"] = truncated["members"][:10]
        for data in (pending, stale, truncated):
            session = Mock()
            with self.assertRaises(SecConfigError):
                collect_sec_filings(data, user_agent=AGENT, session=session)
            session.get.assert_not_called()

    def test_cik_form_amendment_and_archive_host_filtering(self):
        body = atom(entry(1) + entry(1, "8-K/A", "amended") + entry(1, "10-K", "wrongform")
                    + entry(9999, key="outside") + entry(1, key="mismatch", link_cik=2)
                    + entry(1, key="evilhost", host="evil.invalid"))
        articles, scanned = parse_sec_filings(body, "8-K", {"0000000001", "0000000002"})
        self.assertEqual(scanned, 6)
        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[1].content_scope, "filing_notice")
        self.assertEqual(articles[1].content, "")

    @patch("src.sec_universe.time.sleep")
    def test_pagination_continues_after_page_with_no_tracked_companies(self, sleep):
        first = atom("".join(entry(9999, key=str(i)) for i in range(100)))
        session = Mock()
        session.get.side_effect = [response(first), response(atom(entry())), *[response(atom()) for _ in range(4)]]
        articles, report = collect_sec_filings(universe(), user_agent=AGENT, session=session, max_pages=2)
        self.assertEqual(len(articles), 1)
        self.assertEqual(report[0]["scanned"], 101)
        self.assertEqual(report[0]["pages"], 2)
        self.assertTrue(report[0]["window_exhausted"])
        self.assertIn("start=100", session.get.call_args_list[1].args[0])
        self.assertEqual(session.get.call_count, 6)
        self.assertGreaterEqual(len(sleep.call_args_list), 5)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    @patch("src.sec_universe.time.sleep")
    def test_page_limit_is_reported_as_partial(self, sleep):
        first = atom("".join(entry(1, key=str(i)) for i in range(100)))
        session = Mock()
        session.get.side_effect = [response(first), *[response(atom()) for _ in range(4)]]
        articles, report = collect_sec_filings(universe(), user_agent=AGENT, session=session, max_pages=1)
        self.assertEqual(len(articles), 100)
        self.assertEqual(report[0]["status"], "partial")
        self.assertIn("older filings may be missing", report[0]["warnings"][0])

    @patch("src.sec_universe.time.sleep")
    def test_repeated_page_stops_without_duplicate_entries(self, sleep):
        first = atom("".join(entry(1, key=str(i)) for i in range(100)))
        session = Mock()
        session.get.side_effect = [response(first), response(first), *[response(atom()) for _ in range(4)]]
        articles, report = collect_sec_filings(universe(), user_agent=AGENT, session=session, max_pages=3)
        self.assertEqual(len(articles), 100)
        self.assertEqual(report[0]["status"], "partial")
        self.assertIn("Repeated SEC page", report[0]["warnings"][0])

    def test_sec_access_block_stops_remaining_feeds(self):
        for status in (403, 429):
            session = Mock()
            session.get.return_value = response(b"private body contact@example.com", status)
            articles, report = collect_sec_filings(universe(), user_agent=AGENT, session=session)
            self.assertEqual(articles, [])
            self.assertEqual(session.get.call_count, 1)
            self.assertEqual(report[0]["error"], f"HTTP {status}")
            self.assertNotIn("contact@", json.dumps(report))

    def test_incomplete_refresh_preserves_existing_snapshot_and_does_not_save_contact(self):
        session = Mock()
        session.get.side_effect = [response(table("sp500", 503)), response(table("nasdaq100", 101)),
                                   response(json.dumps({"0": {"ticker": "A0", "cik_str": 1}}).encode())]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universe.json"
            path.write_text("preserve me", encoding="utf-8")
            with self.assertRaisesRegex(SecConfigError, "unresolved"):
                refresh_universe(path, user_agent=AGENT, session=session)
            self.assertEqual(path.read_text(encoding="utf-8"), "preserve me")
        self.assertNotIn(AGENT, json.dumps(universe()))

    def test_snapshot_read_validates_counts_and_preserves_index_symbols(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universe.json"
            data = universe()
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(load_universe(path)["counts"]["sp500"], 503)
            data["counts"]["sp500"] = 501
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(SecConfigError):
                load_universe(path)


if __name__ == "__main__":
    unittest.main()
