"""End-to-end editing with a real local store, without contacting any API."""
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock, patch

with patch('dotenv.load_dotenv'):
    from streamlit.testing.v1 import AppTest
    from src.feeds import Source
    from src.research_store import ResearchStore
    import src.research_ui as ui

APP = Path(__file__).resolve().parents[1] / 'app.py'


class ResearchUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'reports.db'
        self.patch('dotenv.load_dotenv')
        self.patch('src.research_ui.ResearchStore', side_effect=lambda: ResearchStore(self.db))
        self.start_patch(patch.dict(os.environ, {'LLM_PROVIDER': 'deepseek', 'OPENAI_API_KEY': '',
                         'DEEPSEEK_API_KEY': '', 'INVESTAT_MCP_TOKEN': '', 'OPENAI_BASE_URL': ''}))
        source = Source('test', 'Test RSS', 'https://example.org/rss', 'media', 'us')
        self.patch('src.feeds.load_sources', return_value=[source])
        self.patch('src.research_ui.load_sources', return_value=[source])
        inbox = MagicMock()
        inbox.stats.return_value = {'total': 1, 'analyzed': 0}
        inbox.export_articles.return_value = [{'id': 1, 'title': '科技AI持倉', 'summary': '機構科技持倉上升。',
            'content': '', 'content_scope': 'summary', 'url': 'https://example.org/article',
            'published_at': datetime.now(timezone.utc).isoformat(), 'source_id': 'test',
            'source_name': 'Test RSS', 'source_kind': 'media', 'sources': [
                {'source_id': 'test', 'source_name': 'Test RSS', 'source_kind': 'media'}]}]
        factory = MagicMock()
        factory.return_value.__enter__.return_value = inbox
        self.patch('src.news_store.NewsStore', new=factory)
        self.patch('src.research_ui.NewsStore', new=factory)
        self.patch('src.report.load_report_events', return_value=[])
        self.generator = self.patch('src.research_ui.generate_research_report', return_value={
            'title': 'AI草稿', 'body': '# AI草稿\n\n原始分析[S1]。',
            'metadata': {'provider': 'deepseek', 'model': 'deepseek-v4-pro', 'report_scope': 'rss_investat_only'}})

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def patch(self, target, **kwargs):
        return self.start_patch(patch(target, **kwargs))

    def create(self, title='報告A', body='原始內容'):
        with ResearchStore(self.db) as store:
            return store.create_report(topic=title, title=title, body=body, sources=[],
                                       provider='deepseek', model='deepseek-v4-pro', as_of='2026-10-14')

    def app(self):
        app = AppTest.from_file(str(APP), default_timeout=10).run()
        self.assertEqual(len(app.exception), 0)
        return app

    def test_generate_edit_restore_and_export_without_second_api_call(self):
        app = self.app()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        with ResearchStore(self.db) as store:
            report = store.list_reports()[0]
        identifier = report['id']
        app.text_input(key=f'research_editor_title_{identifier}').input('我的標題').run()
        app.text_area(key=f'research_editor_body_{identifier}').input('我的完整修改').run()
        app.button(key='research_save_edit').click().run()
        with ResearchStore(self.db) as store:
            saved = store.get_report(identifier)
            revisions = store.list_revisions(identifier)
        self.assertEqual(saved['body'], '我的完整修改')
        self.assertEqual(saved['version'], 2)
        self.assertIn('原始分析', saved['original_body'])
        self.assertNotIn('synthetic-session-credential', json.dumps(saved))
        self.assertEqual(self.generator.call_count, 1)
        original = next(item for item in revisions if item['version'] == 1)
        app.selectbox(key=f'research_revision_{identifier}').select(original['id']).run()
        app.button(key=f'research_restore_{identifier}').click().run()
        with ResearchStore(self.db) as store:
            restored = store.get_report(identifier)
        self.assertEqual(restored['version'], 3)
        self.assertEqual(restored['body'], restored['original_body'])
        self.assertEqual(self.generator.call_count, 1)

    def test_switching_reports_keeps_unsaved_editor_buffer(self):
        first = self.create('報告A', 'A正文')
        second = self.create('報告B', 'B正文')
        app = self.app()
        app.selectbox(key='research_active_report').select(first['id']).run()
        app.text_area(key=f"research_editor_body_{first['id']}").input('A尚未保存的修改').run()
        app.selectbox(key='research_active_report').select(second['id']).run()
        app.selectbox(key='research_active_report').select(first['id']).run()
        self.assertEqual(app.text_area(key=f"research_editor_body_{first['id']}").value, 'A尚未保存的修改')
        app.button(key='research_save_edit').click().run()
        with ResearchStore(self.db) as store:
            self.assertEqual(store.get_report(first['id'])['body'], 'A尚未保存的修改')
        self.generator.assert_not_called()

    def test_two_editors_conflict_keeps_user_text(self):
        report = self.create()
        app = self.app()
        with ResearchStore(self.db) as store:
            store.save_revision(report['id'], title='另一頁面', body='另一修改', expected_version=1)
        app.text_area(key=f"research_editor_body_{report['id']}").input('我的未保存修改').run()
        app.button(key='research_save_edit').click().run()
        self.assertTrue(app.error)
        self.assertEqual(app.text_area(key=f"research_editor_body_{report['id']}").value, '我的未保存修改')
        with ResearchStore(self.db) as store:
            self.assertEqual(store.get_report(report['id'])['version'], 2)
        self.generator.assert_not_called()


if __name__ == '__main__':
    unittest.main()
