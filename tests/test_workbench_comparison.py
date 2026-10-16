import json
import tempfile
import unittest
from pathlib import Path

from src.news_store import NewsStore
from src.workbench import build_comparison, organize_comparison, save_viewpoints, load_viewpoints


def article(identifier, institution="Morgan Stanley", day="2026-10-01", text="未來十二個月盈利增長將改善。若需求下滑則此判斷失效。"):
    return {"id": identifier, "source_id": "test", "source_name": institution,
            "institution": institution, "source_kind": "research", "region": "us",
            "title": "研究" + identifier, "url": "https://example.com/" + identifier,
            "published_at": day + "T00:00:00+00:00", "summary": text, "content": text,
            "content_scope": "provided_text", "external_id": identifier}


class ComparisonTests(unittest.TestCase):
    def test_no_model_for_reader_and_no_invented_view(self):
        rows = build_comparison([article("a")])
        self.assertEqual(rows[0]["change"], "尚未比較")
        self.assertIn("來源摘要", rows[0]["status"])

    def test_reject_unknown_id_and_fabricated_quote(self):
        a = article("a")
        with self.assertRaises(ValueError):
            organize_comparison([a], "盈利", completion=lambda *args: {"viewpoints": [
                {"article_id": "a", "judgement": "買入", "evidence": "來源並沒有這一段文字內容"},
                {"article_id": "b", "judgement": "買入", "evidence": a["content"]}]})

    def test_valid_quotes_and_condition_but_no_cross_institution_change(self):
        earlier, later = article("a", "UBS"), article("b", day="2026-10-02")
        row = {"article_id": "b", "judgement": "盈利改善", "evidence": later["content"],
               "conditions": "若需求下滑則此判斷失效。", "horizon": "未來十二個月",
               "previous_article_id": "a", "previous_evidence": earlier["content"], "change": "觀點改變"}
        results = organize_comparison([earlier, later], "盈利", completion=lambda *a: {"viewpoints": [row]})
        self.assertEqual(results[1]["conditions"], row["conditions"])
        self.assertEqual(results[1]["change"], "未建立可比的前次觀點")

    def test_aliases_and_date_bound_comparison(self):
        earlier, later = article("a", "摩根士丹利"), article("b", day="2026-10-02")
        row = {"article_id": "b", "judgement": "盈利改善", "evidence": later["content"],
               "horizon": "未來十二個月", "previous_article_id": "a",
               "previous_evidence": earlier["content"], "change": "延續改善判斷"}
        results = organize_comparison([later, earlier], "盈利", completion=lambda *a: json.dumps({"viewpoints": [row]}))
        self.assertEqual(results[1]["previous_article_id"], "a")
        self.assertEqual(results[1]["change"], "延續改善判斷")

    def test_viewpoint_invalidated_by_article_update(self):
        with tempfile.TemporaryDirectory() as directory, NewsStore(Path(directory) / "news.db") as store:
            store.ingest([article("a")])
            items = store.export_articles()
            identifier = items[0]["id"]
            results = organize_comparison(items, "盈利", completion=lambda *a: {"viewpoints": [
                {"article_id": identifier, "judgement": "盈利改善", "evidence": items[0]["content"]}]})
            self.assertEqual(save_viewpoints(store, results, "盈利"), 1)
            self.assertEqual(len(load_viewpoints(store, items, "盈利")), 1)
            store.ingest([article("a", text="新數據顯示需求下降，先前判斷不再成立。")])
            self.assertEqual(load_viewpoints(store, store.export_articles(), "盈利"), [])

    def test_acquisition_provider_does_not_imply_same_institution(self):
        earlier, later = article("a"), article("b", day="2026-10-02")
        for item in (earlier, later):
            item.update(institution="", publisher="", source_name="Investat", provider="Investat")
        row = {"article_id": "b", "judgement": "盈利改善", "evidence": later["content"],
               "horizon": "未來十二個月", "previous_article_id": "a",
               "previous_evidence": earlier["content"], "change": "同一家機構改變觀點"}
        result = organize_comparison([earlier, later], "盈利", completion=lambda *a: {"viewpoints": [row]})[1]
        self.assertEqual(result["institution"], "")
        self.assertNotIn("previous_article_id", result)

    def test_previous_hash_and_attribution_revalidated_on_save_and_load(self):
        for mutate_before_save in (False, True):
            with self.subTest(mutate_before_save=mutate_before_save), NewsStore(":memory:") as store:
                older, newer = article("a"), article("b", day="2026-10-02")
                store.ingest([older, newer])
                stored = sorted(store.export_articles(), key=lambda row: row["published_at"])
                earlier, later = stored
                row = {"article_id": later["id"], "judgement": "盈利改善", "evidence": later["content"],
                       "horizon": "未來十二個月", "previous_article_id": earlier["id"],
                       "previous_evidence": earlier["content"], "change": "延續改善判斷"}
                result = organize_comparison(stored, "盈利", completion=lambda *a: {"viewpoints": [row]})
                if mutate_before_save:
                    store.ingest([article("a", text="未來十二個月盈利預期已更新，最新數據與前期不同。")])
                self.assertEqual(save_viewpoints(store, result, "盈利"), 1)
                if not mutate_before_save:
                    # NewsStore retains the same content hash for metadata-only enrichment.
                    store.ingest([{**older, "institution": "UBS"}])
                    self.assertEqual(store.get_article(earlier["id"])["content_hash"], earlier["content_hash"])
                saved = load_viewpoints(store, [store.get_article(later["id"])], "盈利")[0]
                self.assertNotIn("previous_article_id", saved)
                self.assertIn("需重新比較", saved["change"])

    def test_current_attribution_change_invalidates_persisted_viewpoint(self):
        with NewsStore(":memory:") as store:
            original = article("a")
            store.ingest([original])
            current = store.export_articles()[0]
            result = organize_comparison([current], "盈利", completion=lambda *a: {"viewpoints": [
                {"article_id": current["id"], "judgement": "盈利改善", "evidence": current["content"]}]})
            self.assertEqual(save_viewpoints(store, result, "盈利"), 1)
            store.ingest([{**original, "institution": "UBS"}])
            self.assertEqual(load_viewpoints(store, [current], "盈利"), [])
            self.assertEqual(save_viewpoints(store, result, "盈利"), 0)

    def test_saved_fields_use_current_provenance_and_recheck_quoted_conditions(self):
        with NewsStore(":memory:") as store:
            store.ingest([article("a")])
            current = store.export_articles()[0]
            result = organize_comparison([current], "盈利", completion=lambda *a: {"viewpoints": [
                {"article_id": current["id"], "judgement": "盈利改善", "evidence": current["content"]}]})
            result[0].update(source_url="https://made-up.example/?token=secret", conditions="憑空加入的成立條件",
                             horizon="2099年", previous_article_id="made-up", change="捏造變化")
            self.assertEqual(save_viewpoints(store, result, "盈利"), 1)
            saved = load_viewpoints(store, [current], "盈利")[0]
            self.assertEqual(saved["source_url"], current["url"])
            self.assertEqual(saved["conditions"], "")
            self.assertEqual(saved["horizon"], "")
            self.assertNotIn("previous_article_id", saved)

    def test_rss_publication_dates_and_credential_free_model_context(self):
        older, newer = article("a"), article("b", day="2026-10-02")
        older["published_at"] = "Thu, 01 Oct 2026 00:00:00 +0000"
        newer["url"] += "?token=secret-value&id=12#session-secret"
        captured = []
        def complete(system, user):
            captured.append(user)
            return {"viewpoints": [{"article_id": "b", "judgement": "盈利改善", "evidence": newer["content"],
                "horizon": "未來十二個月", "previous_article_id": "a", "previous_evidence": older["content"],
                "change": "延續改善判斷"}]}
        results = organize_comparison([older, newer], "盈利", completion=complete)
        self.assertEqual(results[1]["previous_article_id"], "a")
        self.assertNotIn("secret", captured[0])


if __name__ == "__main__":
    unittest.main()
