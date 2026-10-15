import copy
import unittest

from src.research_status import review_completed, review_status_label


def completed(**overrides):
    return {"workflow_stage": "review_complete", "review_status": "needs_review",
            "quality": {"verdict": "pass"}, "answer_status": "limited", **overrides}


class ResearchStatusTests(unittest.TestCase):
    def test_pass_with_limited_evidence_distinguishes_review_from_fact_verification(self):
        metadata = completed(preparation_warnings=[{"reason": "evidence_extraction_incomplete"}],
            review_findings=[{"severity": "warning", "message": "Secondary sources require verification."}])
        original = copy.deepcopy(metadata)
        label = review_status_label(metadata)
        self.assertTrue(review_completed(metadata))
        self.assertIn("自動審稿已完成", label)
        self.assertIn("來源與主張核驗仍有限制", label)
        self.assertIn("尚未經獨立事實核實", label)
        self.assertNotIn("要求修訂", label)
        self.assertEqual(metadata, original)

    def test_revise_and_insufficient_never_claim_a_pass(self):
        for verdict, expected in (("revise", "審稿結果仍要求修訂"), ("insufficient", "可支持的論證仍不足")):
            with self.subTest(verdict=verdict):
                label = review_status_label(completed(quality={"verdict": verdict}))
                self.assertIn(expected, label)
                self.assertNotIn("評審通過", label)
                self.assertIn("尚未經獨立事實核實", label)

    def test_error_findings_override_a_model_pass_description(self):
        label = review_status_label(completed(review_findings=[{"severity": "error", "message": "Contradicted claim"}]))
        self.assertIn("程序核驗仍有未解問題", label)
        self.assertNotIn("來源與主張核驗仍有限制", label)
        self.assertNotIn("評審通過", label)

    def test_failed_and_intermediate_reviews_cannot_claim_completion(self):
        for metadata in (completed(review_error="Service failed"), completed(review_status="review_failed"),
                         completed(workflow_stage="review_saved", quality_status="reviewed_snapshot"),
                         completed(workflow_stage="writer_saved", review_status="pending")):
            with self.subTest(metadata=metadata):
                self.assertFalse(review_completed(metadata))
                self.assertNotIn("自動審稿已完成", review_status_label(metadata))
                self.assertNotIn("評審通過", review_status_label(metadata))
                if metadata.get("review_error") or metadata.get("review_status") == "review_failed":
                    self.assertEqual(review_status_label(metadata), "自動審稿未完成，已保留最後成功保存的正文")

    def test_manual_edit_precedence_keeps_the_reviewed_version_visible(self):
        label = review_status_label(completed(reviewed_version=2), version=3)
        self.assertEqual(label, "人工修改後待核對（AI審稿基於v2）")
        self.assertNotIn("自動審稿已完成", label)

    def test_model_pass_without_limits_still_does_not_claim_fact_verification(self):
        label = review_status_label(completed(review_status="reviewed", answer_status="full"))
        self.assertIn("模型評審通過", label)
        self.assertIn("尚未經獨立事實核實", label)

    def test_legacy_or_malformed_metadata_does_not_fabricate_completion(self):
        for metadata in (None, [], {"review_status": []}, {"review_status": "needs_review"},
                         {"review_status": "needs_review", "workflow_stage": []}):
            with self.subTest(metadata=metadata):
                self.assertFalse(review_completed(metadata))
                self.assertNotIn("自動審稿已完成", review_status_label(metadata))


if __name__ == "__main__":
    unittest.main()
