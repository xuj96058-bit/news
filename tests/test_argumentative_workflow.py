"""Real planning/writing/review boundaries with synthetic provider streams."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from src.research import prepare_sources
from src.research_workflow import generate_research_workflow
from test_argumentation import essay, plan


def sources():
    return prepare_sources([{"id":i,"title":title,"content":text,"source_name":"Test",
        "content_scope":"provided_text","published_at":"2026-10-14T01:00:00Z"}
        for i,title,text in [(1,"Orders","Orders improved in the disclosed business."),
            (2,"Costs","Costs constrained margins in the disclosed business."),
            (3,"Travel","An unrelated travel promotion.")]])


class Stream:
    def __init__(self, value): self.text=json.dumps(value)
    def __iter__(self):
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=self.text),finish_reason=None)])
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None),finish_reason="stop")])
    def close(self): pass


def review(draft=None, verdict="pass"):
    return {"draft":draft or essay(),"findings":[],"summary":"Evidence and reasoning compared with supplied sources.",
        "quality":{k:3 for k in ("relevance","evidence","reasoning","readability")} | {"verdict":verdict}}


class ArgumentativeWorkflowTests(unittest.TestCase):
    def client(self, *values):
        c=MagicMock()
        c.chat.completions.create.side_effect=[Stream(value) if isinstance(value,dict) else value for value in values]
        return c

    def generate(self, client, **kwargs):
        return generate_research_workflow("Orders and profits",sources(),api_key="synthetic-key",
            as_of="2026-10-14",writing_style="argumentative",report_mode="argumentative",
            retrieve_sources=False,client=client,**kwargs)

    def test_plan_filters_sources_and_writer_saved_before_review(self):
        c=self.client(plan(),essay(),review())
        saved=[]
        def checkpoint(draft):
            self.assertEqual(c.chat.completions.create.call_count,2)
            saved.append(draft)
        result=self.generate(c,on_draft=checkpoint)
        self.assertEqual(c.chat.completions.create.call_count,3)
        self.assertEqual([s["id"] for s in result["sources"]],["S1","S2"])
        self.assertNotIn("Travel",result["body"])
        self.assertEqual(saved[0]["metadata"]["workflow_stage"],"writer_saved")
        self.assertEqual(result["metadata"]["api_call_count"],3)
        self.assertEqual(result["metadata"]["review_status"],"needs_review")
        for call in c.chat.completions.create.call_args_list:
            self.assertEqual(call.kwargs["extra_body"]["thinking"]["type"],"enabled")
            self.assertNotIn("temperature",call.kwargs)

    def test_persistence_failure_stops_paid_review(self):
        c=self.client(plan(),essay(),review())
        def failed(_): raise RuntimeError("database unavailable")
        with self.assertRaisesRegex(RuntimeError,"database unavailable"):
            self.generate(c,on_draft=failed)
        self.assertEqual(c.chat.completions.create.call_count,2)

    def test_review_failure_returns_initial_without_credentials(self):
        c=self.client(plan(),essay(),RuntimeError("synthetic provider error"))
        saved=[]
        result=self.generate(c,on_draft=saved.append)
        self.assertEqual(len(saved),1)
        self.assertEqual(result["metadata"]["review_status"],"review_failed")
        self.assertNotIn("synthetic-key",json.dumps(result))

    def test_scores_cannot_clear_untraceable_numbers(self):
        d=essay();d["arguments"][0]["claim"]["text"]="Orders increased 987 percent."
        p=plan();p.update(answer_scope="full",missing_evidence=[])
        result=self.generate(self.client(p,d,review(d)))
        self.assertEqual(result["metadata"]["review_status"],"needs_review")
        self.assertTrue(any(w["code"]=="untraceable_numbers" for w in result["metadata"]["validation_warnings"]))

    def test_one_revision_only(self):
        c=self.client(plan(),essay(),review(verdict="revise"),review(verdict="revise"))
        result=self.generate(c)
        self.assertEqual(c.chat.completions.create.call_count,4)
        self.assertEqual(result["metadata"]["review_status"],"needs_review")

    def test_unknown_plan_reference_never_reaches_writer(self):
        p=plan();p["source_ids"].append("S999")
        c=self.client(p,essay(),review())
        from src.analysis_errors import ModelResponseError
        with self.assertRaises(ModelResponseError): self.generate(c)
        self.assertEqual(c.chat.completions.create.call_count,1)

    def test_numeric_headings_and_title_cannot_bypass_checks(self):
        for location in ("title","heading"):
            d=essay()
            if location=="title": d["title"]="Orders increased 987 percent"
            else: d["arguments"][0]["heading"]="Orders increased 987 percent"
            result=self.generate(self.client(plan(),d,review(d)))
            self.assertTrue(any(w["code"]=="untraceable_numbers" for w in result["metadata"]["validation_warnings"]))

    def test_plan_quality_and_sources_survive_store_reload(self):
        from src.research_store import ResearchStore
        result=self.generate(self.client(plan(),essay(),review()))
        result["metadata"]["argument_plan"]["api_key"]="synthetic-secret"
        with ResearchStore(":memory:") as store:
            saved=store.create_report(topic="Orders",title=result["title"],body=result["body"],
                sources=result["sources"],provider="deepseek",model="deepseek-flash",
                as_of="2026-10-14",metadata=result["metadata"])
            loaded=store.get_report(saved["id"])
        self.assertEqual(loaded["metadata"]["writing_style"],"argumentative")
        self.assertEqual(loaded["metadata"]["quality"]["relevance"],3)
        self.assertEqual(loaded["metadata"]["argument_plan"]["source_ids"],["S1","S2"])
        self.assertNotIn("synthetic-secret",json.dumps(loaded))

    def test_reasoning_stream_has_separate_bounded_chunk_budget(self):
        from src.research import _stream_content
        chunks=[SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None,reasoning_content="thinking"),finish_reason=None)])]*12001
        chunks += [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='{"ok":true}'),finish_reason="stop")])]
        self.assertEqual(_stream_content(iter(chunks),max_chunks=50000),'{"ok":true}')
        from src.analysis_errors import ProviderAPIError
        with self.assertRaises(ProviderAPIError): _stream_content(iter(chunks))

    def test_quantitative_scope_cannot_pass_missing_direct_evidence(self):
        p=plan();p.update(answer_scope="full",missing_evidence=[])
        c=self.client(p,essay(),review())
        result=generate_research_workflow("美股槓桿與行業擁擠度",sources(),api_key="synthetic-key",
            as_of="2026-10-14",writing_style="argumentative",retrieve_sources=False,client=c)
        self.assertEqual(result["metadata"]["argument_plan"]["answer_scope"],"narrowed")
        self.assertEqual(result["metadata"]["review_status"],"needs_review")


from test_research_ui import ResearchUiHarness


class ArgumentativeUiTests(ResearchUiHarness, unittest.TestCase):
    def test_default_essay_generation_persists_actual_plan_and_review(self):
        p=plan();p["source_ids"]=["S1"]
        for argument in p["arguments"]:
            argument["source_ids"]=["S1"];argument["counter_source_ids"]=["S1"]
        d=essay()
        from src.argumentation import essay_paragraphs
        for _section,_index,paragraph in essay_paragraphs(d): paragraph["source_ids"]=["S1"]
        client=MagicMock()
        client.chat.completions.create.side_effect=[Stream(p),Stream(d),Stream(review(d))]
        def real_flow(*args,**kwargs):
            return generate_research_workflow(*args,client=client,retrieve_sources=False,**kwargs)
        self.generator.side_effect=real_flow
        app=self.app(report_mode="argumentative")
        self.assertEqual(app.radio(key="research_report_mode").value,"議論文式研究報告（建議）")
        app.sidebar.text_input(key="ai_api_key").input("synthetic-ui-key").run()
        self.assertFalse(app.button(key="research_generate").disabled)
        app.button(key="research_generate").click().run()
        self.assertEqual(len(app.exception),0)
        from src.research_store import ResearchStore
        with ResearchStore(self.db) as store:
            saved=store.list_reports()[0]
            saved=store.get_report(saved["id"])
        self.assertEqual(saved["version"],2)
        self.assertEqual(saved["metadata"]["writing_style"],"argumentative")
        self.assertEqual(saved["metadata"]["argument_plan"]["source_ids"],["S1"])
        self.assertEqual(saved["metadata"]["api_call_count"],3)
        self.assertNotIn("synthetic-ui-key",json.dumps(saved))
