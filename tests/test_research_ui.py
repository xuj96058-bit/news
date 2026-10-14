"""End-to-end editing with a real local store, without contacting any API."""
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
from threading import Event
import unittest
from unittest.mock import MagicMock, patch

with patch('dotenv.load_dotenv'):
    from streamlit.testing.v1 import AppTest
    from src.feeds import Source
    from src.research import ResearchError
    from src.research_progress import run_generation
    from src.research_store import ResearchStore, ResearchStoreError
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
        self.inbox = inbox
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
        progress = self.patch('src.research_ui._generation_progress', wraps=ui._generation_progress)
        app = self.app()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        stages = [call.args[1] for call in progress.call_args_list]
        self.assertIn('saving', stages)
        self.assertEqual(stages[-1], 'saved')
        self.assertIs(self.generator.call_args.kwargs['repair_citations'], True)
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

    def test_empty_selection_explains_disabled_button_and_quick_selection_enables(self):
        app = self.app()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_clear_articles').click().run()
        self.assertEqual(app.multiselect(key='research_article_ids').value, [])
        self.assertTrue(app.button(key='research_generate').disabled)
        self.assertTrue(any('預覽至少有1個來源' in item.value for item in app.info))
        self.assertTrue(any('找到1篇可用候選' in item.value for item in app.caption))
        self.generator.assert_not_called()

        app.button(key='research_select_articles').click().run()
        self.assertEqual(app.multiselect(key='research_article_ids').value, ['1'])
        self.assertFalse(app.button(key='research_generate').disabled)
        self.assertEqual(len(app.exception), 0)

    def test_missing_api_key_explains_disabled_generation(self):
        app = self.app()
        self.assertEqual(app.multiselect(key='research_article_ids').value, ['1'])
        self.assertTrue(app.button(key='research_generate').disabled)
        self.assertTrue(any('API key並按Enter' in item.value for item in app.info))
        self.generator.assert_not_called()

    def test_evidence_only_selection_shows_source_counts_and_news_hint(self):
        app = self.app()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_clear_articles').click().run()
        app.session_state['research_official_evidence'] = [{
            'title': ui.CATALOG['finra'][0], 'content': 'Synthetic official excerpt.',
            'source_type': 'official', 'publisher': 'Synthetic official publisher'}]
        app.session_state['research_manual_evidence'] = [{
            'title': 'Synthetic manual evidence', 'content': 'Synthetic manual excerpt.',
            'source_type': 'manual', 'publisher': 'Synthetic analyst'}]
        app.checkbox(key='research_use_evidence').check().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any(item.value == '本次送給AI：RSS新聞0篇 · Investat研究0篇 · 公開核對資料1份 · 手動證據1份。'
                            for item in app.caption))
        self.assertTrue(any('本次只使用公開核對資料或手動證據' in item.value for item in app.info))
        self.assertTrue(any(item.label == '預覽即將送給AI的資料（2個來源）' for item in app.expander))
        self.assertFalse(app.button(key='research_generate').disabled)
        self.generator.assert_not_called()

    def test_rss_and_investat_counts_use_selected_sources_without_double_counting_origins(self):
        rss = self.inbox.export_articles.return_value[0]
        rss['sources'].append({'source_id': 'investat', 'source_name': 'Investat',
                               'source_kind': 'research', 'title': rss['title'], 'url': rss['url']})
        self.inbox.export_articles.return_value.append({
            'id': 2, 'title': 'AI科技融資研究', 'summary': 'Synthetic distinct financing excerpt.',
            'content': '', 'content_scope': 'snippet', 'url': 'https://example.org/investat-study',
            'published_at': datetime.now(timezone.utc).isoformat(), 'source_id': 'investat',
            'source_name': 'Investat', 'source_kind': 'research', 'sources': []})
        app = self.app()
        app.multiselect(key='research_news_sources').set_value(['test', 'investat']).run()
        app.button(key='research_select_articles').click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any(item.value == '本次送給AI：RSS新聞1篇 · Investat研究1篇 · 公開核對資料0份 · 手動證據0份。'
                            for item in app.caption))
        app.multiselect(key='research_article_ids').set_value(['1']).run()
        self.assertTrue(any(item.value == '本次送給AI：RSS新聞1篇 · Investat研究0篇 · 公開核對資料0份 · 手動證據0份。'
                            for item in app.caption))
        self.assertFalse(any('本次只使用公開核對資料或手動證據' in item.value for item in app.info))
        self.generator.assert_not_called()

    def test_generation_failure_preserves_selection_and_saves_no_report(self):
        self.generator.side_effect = ResearchError('invalid_citations')
        app = self.app()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any('來源編號' in item.value for item in app.error))
        self.assertEqual(app.multiselect(key='research_article_ids').value, ['1'])
        self.assertFalse(app.button(key='research_generate').disabled)
        with ResearchStore(self.db) as store:
            self.assertEqual(store.list_reports(), [])

    def test_generation_timeout_discards_late_draft_without_saving(self):
        release = Event()
        finished = Event()
        self.addCleanup(release.set)

        def delayed_draft(*args, on_progress=None, **kwargs):
            on_progress('requesting', 0)
            release.wait(5)
            finished.set()
            return {'title': '遲來的草稿', 'body': '遲來的內容', 'metadata': {}}

        self.generator.side_effect = delayed_draft
        self.patch('src.research_ui.run_generation', side_effect=lambda generate, update: run_generation(
            generate, update, timeout=0.04, tick_seconds=0.01))
        app = self.app()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any('生成逾時' in item.value for item in app.error))
        with ResearchStore(self.db) as store:
            self.assertEqual(store.list_reports(), [])
        release.set()
        self.assertTrue(finished.wait(1))
        app.run()
        with ResearchStore(self.db) as store:
            self.assertEqual(store.list_reports(), [])

    def test_storage_failure_reports_error_without_saved_success(self):
        self.patch('src.research_store.ResearchStore.create_report',
                   side_effect=ResearchStoreError('無法保存報告。請檢查資料庫。'))
        progress = self.patch('src.research_ui._generation_progress', wraps=ui._generation_progress)
        app = self.app()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any('無法保存報告' in item.value for item in app.error))
        stages = [call.args[1] for call in progress.call_args_list]
        self.assertIn('saving', stages)
        self.assertNotIn('saved', stages)
        with ResearchStore(self.db) as store:
            self.assertEqual(store.list_reports(), [])

    def test_citation_repair_can_be_disabled_for_generation(self):
        app = self.app()
        self.assertTrue(app.checkbox(key='research_repair_citations').value)
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.checkbox(key='research_repair_citations').uncheck().run()
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertIs(self.generator.call_args.kwargs['repair_citations'], False)
        with ResearchStore(self.db) as store:
            self.assertEqual(len(store.list_reports()), 1)

    def test_citation_repair_updates_generation_progress_label(self):
        release = Event()
        self.addCleanup(release.set)
        draft = self.generator.return_value
        bar = MagicMock()
        self.patch('src.research_ui.st.progress', return_value=bar)
        render_progress = ui._generation_progress

        def track_progress(progress_bar, stage, *args, **kwargs):
            render_progress(progress_bar, stage, *args, **kwargs)
            if stage == 'repairing':
                release.set()

        self.patch('src.research_ui._generation_progress', side_effect=track_progress)

        def repaired_draft(*args, on_progress, **kwargs):
            on_progress('repairing', 50)
            if not release.wait(2):
                raise AssertionError('Citation repair progress was never displayed.')
            return draft

        self.generator.side_effect = repaired_draft
        app = self.app()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any('AI正在修正來源引用（最多一次）' in call.kwargs.get('text', '')
                            for call in bar.progress.call_args_list))
        with ResearchStore(self.db) as store:
            self.assertEqual(len(store.list_reports()), 1)

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
