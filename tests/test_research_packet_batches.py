"""Preparation remains useful when evidence extraction needs several small calls."""
import copy
import json
import unittest

from src.research_design import MAX_QUOTE, parse_evidence
from src.research_packet import (EXTRACTION_BATCH_CHARS, EXTRACTION_BATCH_ITEMS,
    EXTRACTION_BATCH_SOURCES, MAX_EXTRACTION_BATCHES, prepare_research_packet,
    prompt_packet, render_packet_appendix)


TOPIC = "Demand and delivery constraints"
AS_OF = "2026-10-14"


def design():
    return {"version": 1, "topic": TOPIC, "as_of": AS_OF, "markets": ["global"],
        "questions": [{"id": "Q1", "question": "What supports or constrains demand?"}],
        "requirements": [{"id": "R1", "question_id": "Q1", "description": "Evidence for and against the operating outlook",
                          "kind": "document", "critical": True}], "methods": []}


def sources(count, size=1300):
    rows = []
    for number in range(1, count + 1):
        statements = "\n".join(f"Source {number} reports operating observation {index}." for index in range(5))
        rows.append({"id": f"S{number}", "title": f"Operating source {number}",
            "content": statements + "\n" + "x" * (size - len(statements) - 1),
            "publication_date": "2026-10-10", "source_type": "manual", "content_scope": "provided_text"})
    return rows


def extract(payload, per_source=1):
    rows = []
    for source in payload["sources"]:
        for statement in source["content"].splitlines()[:per_source]:
            if not statement.startswith("Source "):
                continue
            rows.append({"id": f"E{len(rows)+1}", "kind": "document", "requirement_id": "R1",
                "source_id": source["id"], "quote": statement, "statement": statement, "event_status": "unknown"})
    return {"items": rows}


class PacketBatchTests(unittest.TestCase):
    def prepare(self, source_rows, request, **kwargs):
        return prepare_research_packet(TOPIC, source_rows, as_of=AS_OF,
            request=request, retrieve_sources=False, **kwargs)

    def test_thirty_sources_are_covered_without_reemitting_prior_evidence(self):
        calls = []
        def request(system, payload, stage):
            if stage == "designing": return design()
            calls.append(copy.deepcopy(payload))
            return extract(payload)
        original = sources(30)
        result = self.prepare(original, request)
        self.assertGreater(len(calls), 1)
        self.assertEqual(len(result["sources"]), 30)
        self.assertEqual(len(result["evidence"]["items"]), 30)
        self.assertEqual({item["source_id"] for item in result["evidence"]["items"]}, {row["id"] for row in original})
        self.assertEqual(len({item["id"] for item in result["evidence"]["items"]}), 30)
        for payload in calls:
            self.assertLessEqual(len(payload["sources"]), EXTRACTION_BATCH_SOURCES)
            self.assertLessEqual(sum(len(row["content"]) for row in payload["sources"]), EXTRACTION_BATCH_CHARS)
            self.assertEqual(payload["previous_items"], [])
            self.assertEqual(payload["batch"]["max_items"], EXTRACTION_BATCH_ITEMS)
        self.assertEqual(result["preparation_warnings"], [])
        self.assertEqual(result["answer_status"], "full")

    def test_later_failure_preserves_first_batch_and_marks_pending_sources(self):
        snapshots, batches = [], 0
        def request(system, payload, stage):
            nonlocal batches
            if stage == "designing": return design()
            batches += 1
            if batches == 2: raise RuntimeError("provider stopped")
            return extract(payload)
        with self.assertRaisesRegex(RuntimeError, "provider stopped"):
            self.prepare(sources(30), request, on_checkpoint=snapshots.append)
        latest = snapshots[-1]
        self.assertTrue(latest["evidence"]["items"])
        self.assertEqual(len(latest["sources"]), 30)
        self.assertTrue(any(row["status"] == "pending" for row in latest["extraction_coverage"]))
        self.assertEqual(latest["answer_status"], "limited")
        self.assertEqual(latest["preparation_warnings"][0]["reason"], "evidence_extraction_incomplete")
        self.assertEqual(snapshots[0]["evidence"]["items"], [])

    def test_batch_budget_exposes_unprocessed_material_and_preserves_all_sources(self):
        calls = []
        def request(system, payload, stage):
            if stage == "designing": return design()
            calls.append(payload)
            return extract(payload)
        result = self.prepare(sources(40, size=4000), request)
        self.assertEqual(len(calls), MAX_EXTRACTION_BATCHES)
        self.assertEqual(result["stop_reason"], "extraction_batch_budget")
        self.assertEqual(len(result["sources"]), 40)
        self.assertEqual(len(result["preparation_warnings"][0]["source_ids"]), 16)
        self.assertEqual(result["answer_status"], "limited")
        self.assertIn("未完成證據提取", render_packet_appendix(result))

    def test_long_sources_use_overlapping_windows_and_global_anchor_offsets(self):
        original = sources(2)
        tail = "Source 1 reports a late delivery constraint."
        original[0]["content"] = "x" * 7500 + tail + "x" * 3500
        seen = []
        def request(system, payload, stage):
            if stage == "designing": return design()
            seen.extend(payload["sources"])
            rows = extract(payload)["items"]
            for source in payload["sources"]:
                if tail in source["content"]:
                    rows.append({"id": f"E{len(rows)+1}", "kind": "document", "requirement_id": "R1",
                        "source_id": source["id"], "quote": tail, "statement": tail, "event_status": "unknown"})
            return {"items": rows}
        result = self.prepare(original, request)
        windows = [row["excerpt_range"] for row in seen if row["id"] == "S1"]
        self.assertEqual(windows[0][0], 0)
        self.assertEqual(windows[-1][1], len(original[0]["content"]))
        self.assertGreaterEqual(windows[0][1] - windows[1][0], MAX_QUOTE)
        anchored = [row for row in result["evidence"]["items"] if row["source_id"] == "S1"]
        self.assertEqual(len(anchored), 1)  # overlap does not duplicate evidence
        self.assertEqual(anchored[0]["anchor"]["start"], 7500)
        self.assertEqual(result["preparation_warnings"], [])

    def test_oversized_batch_response_is_visible_without_erasing_prior_evidence(self):
        batches = 0
        def request(system, payload, stage):
            nonlocal batches
            if stage == "designing": return design()
            batches += 1
            value = extract(payload)
            if batches == 2:
                value["items"] = value["items"] * (EXTRACTION_BATCH_ITEMS + 1)
            return value
        result = self.prepare(sources(24), request)
        self.assertEqual(len(result["evidence"]["items"]), 12)
        self.assertTrue(any(row["status"] == "invalid_response" for row in result["extraction_coverage"]))
        self.assertEqual(result["answer_status"], "limited")

    def test_accumulation_limit_is_reported_without_dropping_source_text(self):
        def request(system, payload, stage):
            return design() if stage == "designing" else extract(payload, per_source=5)
        original = sources(24, size=4000)
        result = self.prepare(original, request)
        self.assertEqual(len(result["evidence"]["items"]), 80)
        self.assertEqual([row["content"] for row in result["sources"]], [row["content"] for row in original])
        self.assertTrue(any(row["reason"] == "evidence_capacity" for row in result["preparation_warnings"]))
        self.assertEqual(result["answer_status"], "limited")

    def test_compact_writer_packet_keeps_citations_and_saved_verification_records(self):
        def request(system, payload, stage):
            return design() if stage == "designing" else extract(payload)
        result = self.prepare(sources(12), request)
        before = copy.deepcopy(result)
        compact = prompt_packet(result)
        self.assertNotIn("source_manifest", compact["evidence"])
        self.assertNotIn("packet_hash", compact)
        self.assertNotIn("anchor", compact["evidence"]["items"][0])
        self.assertEqual(compact["evidence"]["items"][0]["source_id"], "S1")
        self.assertEqual(compact["evidence"]["items"][0]["quote"], result["evidence"]["items"][0]["quote"])
        self.assertEqual(compact["evidence"]["requirements"][0]["evidence_ids"], result["evidence"]["requirements"][0]["evidence_ids"])
        self.assertEqual(result, before)
        self.assertLess(len(json.dumps(compact)), len(json.dumps(result["evidence"])))

    def test_long_quote_is_not_accepted_as_a_short_evidence_item(self):
        row = sources(1, size=1000)[0]
        text = row["content"]
        extracted = {"items": [{"id": "E1", "kind": "document", "requirement_id": "R1", "source_id": "S1",
            "quote": text, "statement": text.splitlines()[0], "event_status": "unknown"}]}
        evidence = parse_evidence(extracted, [row], design(), AS_OF)
        self.assertEqual(evidence["items"], [])
        self.assertEqual(evidence["warnings"][0]["code"], "invalid_item")


if __name__ == "__main__":
    unittest.main()
