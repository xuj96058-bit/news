"""Real writer/reviewer logic through the editor, with synthetic SDK streams only.

These checks deliberately never contact an AI service. They verify the actual
structured response parser, staged persistence and editable export path together.
"""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from test_research_ui import ResearchUiHarness
from src.research_export import export_html, export_markdown, export_pdf
from src.research_store import ResearchStore, ResearchStoreError


def structured_writer():
    text = {
        'executive_findings': '機構科技持倉上升，仍需區分持倉集中與交易擁擠。',
        'chart_data_interpretation': '來源 S1 沒有提供圖表或完整槓桿數據。',
        'differences_and_alternatives': '持倉集中不能直接視作融資集中，現有摘要不足以識別資金流。',
        'conditional_mechanisms': '若持倉由槓桿支持，波動可能傳導至融資需求，這只是待驗證的條件推論。',
        'watchlist': '後續核對融資餘額、行業持倉及觀測期間。',
        'limitations': '材料只有摘要，尚未取得完整行業槓桿分布。',
    }
    return {'title': '行業擁擠度與美股槓桿集中度', **{
        field: [{'text': value, 'source_ids': ['S1']}] for field, value in text.items()}}


class SyntheticStream:
    def __init__(self, payload):
        self.raw = json.dumps(payload, ensure_ascii=False)
        self.closed = False

    def __iter__(self):
        for offset in range(0, len(self.raw), 47):
            yield SimpleNamespace(choices=[SimpleNamespace(finish_reason=None,
                delta=SimpleNamespace(content=self.raw[offset:offset + 47]))])
        yield SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
            delta=SimpleNamespace(content=None))])

    def close(self):
        self.closed = True


class WorkflowIntegrationTests(ResearchUiHarness, unittest.TestCase):
    def use_real_workflow(self, responses):
        from src.research_workflow import generate_research_workflow
        client = MagicMock()
        client.chat.completions.create.side_effect = responses

        def generate(*args, **kwargs):
            return generate_research_workflow(*args, client=client, **kwargs)

        self.generator.side_effect = generate
        return client

    def test_failed_initial_save_prevents_second_api_call(self):
        first_stream = SyntheticStream(structured_writer())
        client = self.use_real_workflow([first_stream])
        self.patch('src.research_store.ResearchStore.create_report',
                   side_effect=ResearchStoreError('無法保存報告。請檢查資料庫。'))
        app = self.app()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertTrue(first_stream.closed)
        self.assertTrue(any('無法保存報告' in item.value for item in app.error))
        with ResearchStore(self.db) as store:
            self.assertEqual(store.list_reports(), [])

    def test_structured_writer_independent_review_save_edit_and_all_exports(self):
        writer = structured_writer()
        reviewed = copy.deepcopy(writer)
        reviewed['executive_findings'][0]['text'] = '審稿修訂：機構科技持倉上升尚不足以確定交易擁擠或槓桿集中。'
        streams = [SyntheticStream(writer), SyntheticStream({
            'draft': reviewed, 'findings': [{'section': 'limitations', 'paragraph': 1,
                'severity': 'warning', 'message': '發布前仍需核對完整融資和持倉原始資料。',
                'source_ids': ['S1']}], 'summary': '已對照提供的來源快照；保留未能證實的資料缺口。'})]
        client = self.use_real_workflow(streams)
        markdown_export = self.patch('src.research_ui.export_markdown', wraps=export_markdown)
        html_export = self.patch('src.research_ui.export_html', wraps=export_html)
        pdf_export = self.patch('src.research_ui.export_pdf', wraps=export_pdf)
        app = self.app()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertTrue(all(stream.closed for stream in streams))
        first, second = client.chat.completions.create.call_args_list
        self.assertTrue(first.kwargs['stream'])
        self.assertTrue(second.kwargs['stream'])
        self.assertNotEqual(first.kwargs['messages'][0]['content'], second.kwargs['messages'][0]['content'])
        self.assertIn('機構科技持倉上升', second.kwargs['messages'][1]['content'])
        self.assertNotIn('synthetic-session-credential', json.dumps(first.kwargs))
        self.assertNotIn('synthetic-session-credential', json.dumps(second.kwargs))
        with ResearchStore(self.db) as store:
            reports = store.list_reports()
            self.assertEqual(len(reports), 1)
            report = reports[0]
            revisions = store.list_revisions(report['id'])
        self.assertEqual(report['version'], 2)
        self.assertIn('審稿修訂', report['body'])
        self.assertNotIn('審稿修訂', report['original_body'])
        self.assertIn('[S1]', report['body'])
        self.assertIn('來源 S1 沒有提供圖表', report['original_body'])
        self.assertEqual(report['metadata']['review_status'], 'needs_review')
        self.assertEqual(report['metadata']['structured_draft'], reviewed)
        self.assertEqual([item['version'] for item in revisions], [2, 1])
        self.assertTrue(any('發布前仍需核對' in item.value for item in app.text))
        identifier = report['id']
        app.text_input(key=f'research_editor_title_{identifier}').input('我的行業研究').run()
        app.text_area(key=f'research_editor_body_{identifier}').input(
            '# 我的完整修改\n\n我已手動補充分析；保留來源核對。[S1]').run()
        app.button(key='research_save_edit').click().run()
        self.assertEqual(len(app.exception), 0)
        with ResearchStore(self.db) as store:
            saved = store.get_report(identifier)
        self.assertEqual(saved['version'], 3)
        self.assertEqual(saved['title'], '我的行業研究')
        self.assertIn('我已手動補充分析', saved['body'])
        self.assertEqual(saved['original_body'], report['original_body'])
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(markdown_export.call_args.args[0]['body'], saved['body'])
        self.assertEqual(html_export.call_args.args[0]['body'], saved['body'])
        self.assertIn('我已手動補充分析', export_markdown(saved).decode('utf-8'))
        self.assertIn('我已手動補充分析', export_html(saved).decode('utf-8'))
        app.button(key='research_prepare_pdf').click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(pdf_export.call_args.args[0]['body'], saved['body'])
        self.assertTrue(app.session_state['research_pdf']['data'].startswith(b'%PDF-'))
        self.assertEqual(app.session_state['research_pdf']['version'], 3)
        self.assertTrue(any(item.label == '下載PDF' for item in app.get('download_button')))
        self.assertNotIn('synthetic-session-credential', json.dumps(saved))

    def test_unknown_structured_source_and_failed_reviewer_keep_editable_writer(self):
        writer = structured_writer()
        writer['executive_findings'][0]['source_ids'] = ['S1', 'S999']
        first_stream = SyntheticStream(writer)
        client = self.use_real_workflow([first_stream, RuntimeError('synthetic-session-credential raw API detail')])
        app = self.app()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertTrue(first_stream.closed)
        with ResearchStore(self.db) as store:
            report = store.list_reports()[0]
        self.assertIn(writer['executive_findings'][0]['text'], report['body'])
        self.assertIn(writer['executive_findings'][0]['text'], report['original_body'])
        self.assertNotIn('[S999]', report['body'])
        self.assertEqual(report['metadata']['review_status'], 'review_failed')
        self.assertTrue(report['metadata']['validation_warnings'])
        self.assertTrue(any(item['section'] == 'executive_findings'
                            for item in report['metadata']['validation_warnings']))
        self.assertNotIn('synthetic-session-credential', json.dumps(report))
        app.text_area(key=f"research_editor_body_{report['id']}").input('我已人工修正來源標記。[S1]').run()
        app.button(key='research_save_edit').click().run()
        with ResearchStore(self.db) as store:
            saved = store.get_report(report['id'])
        self.assertEqual(saved['body'], '我已人工修正來源標記。[S1]')
        self.assertIn('我已人工修正來源標記', export_markdown(saved).decode('utf-8'))
        self.assertEqual(client.chat.completions.create.call_count, 2)


if __name__ == '__main__':
    unittest.main()
