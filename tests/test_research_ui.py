"""End-to-end editing with a real local store, without contacting any API."""
import copy
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


class ResearchUiHarness:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'reports.db'
        self.patch('dotenv.load_dotenv')
        self.patch('src.workbench_ui.render_reader_tab')
        self.patch('src.workbench_ui.render_source_operations')
        self.patch('src.chartbook_ui.render_chartbook_tab')
        self.patch('src.research_ui.ResearchStore', side_effect=lambda: ResearchStore(self.db))
        self.start_patch(patch.dict(os.environ, {'LLM_PROVIDER': 'deepseek', 'OPENAI_API_KEY': '',
                         'DEEPSEEK_API_KEY': '', 'INVESTAT_MCP_TOKEN': '', 'OPENAI_BASE_URL': ''}))
        source = Source('test', 'Test RSS', 'https://example.org/rss', 'media', 'us')
        self.patch('src.feeds.load_sources', return_value=[source])
        self.patch('src.research_ui.load_sources', return_value=[source])
        inbox = MagicMock()
        self.inbox = inbox
        inbox.stats.return_value = {'total': 1, 'analyzed': 0}
        inbox.export_articles.return_value = [{'id': 1, 'title': '行業擁擠度與科技AI持倉', 'summary': '機構科技持倉上升。',
            'content': '', 'content_scope': 'summary', 'url': 'https://example.org/article',
            'published_at': datetime.now(timezone.utc).isoformat(), 'source_id': 'test',
            'source_name': 'Test RSS', 'source_kind': 'media', 'sources': [
                {'source_id': 'test', 'source_name': 'Test RSS', 'source_kind': 'media'}]}]
        inbox.search_articles.side_effect = lambda **kwargs: {
            'items': inbox.export_articles.return_value, 'total': len(inbox.export_articles.return_value)}
        factory = MagicMock()
        factory.return_value.__enter__.return_value = inbox
        self.patch('src.news_store.NewsStore', new=factory)
        self.patch('src.research_ui.NewsStore', new=factory)
        self.patch('src.report.load_report_events', return_value=[])
        self.initial_draft = {
            'title': 'AI草稿', 'body': '# AI草稿\n\n原始分析[S1]。',
            'metadata': {'provider': 'deepseek', 'model': 'deepseek-v4-pro',
                         'report_scope': 'rss_investat_only', 'workflow_stage': 'writer_saved',
                         'review_status': 'pending', 'validation_warnings': []}}
        self.reviewed_draft = {
            'title': '審稿後草稿', 'body': '# 審稿後草稿\n\n審稿修訂分析[S1]。',
            'metadata': {**self.initial_draft['metadata'], 'workflow_stage': 'review_complete',
                         'review_status': 'reviewed', 'review_findings': [], 'review_summary': '已核對來源快照。'}}

        def completed_workflow(*args, on_progress=None, on_draft=None, **kwargs):
            on_draft(copy.deepcopy(self.initial_draft))
            on_progress('reviewing', 0)
            return copy.deepcopy(self.reviewed_draft)

        self.generator = self.patch('src.research_ui.generate_research_workflow', side_effect=completed_workflow)

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

    def app(self, report_mode='gap'):
        app = AppTest.from_file(str(APP), default_timeout=10).run()
        self.assertEqual(len(app.exception), 0)
        if report_mode == 'gap':
            app.radio(key='research_report_mode').set_value('資料缺口簡報').run()
            self.assertEqual(len(app.exception), 0)
        elif report_mode == 'research':
            app.radio(key='research_report_mode').set_value('完整研究報告（建議）').run()
            self.assertEqual(len(app.exception), 0)
        return app


class ResearchUiTests(ResearchUiHarness, unittest.TestCase):
    def test_generate_edit_restore_and_export_without_additional_generation(self):
        progress = self.patch('src.research_ui._generation_progress', wraps=ui._generation_progress)
        app = self.app()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        stages = [call.args[1] for call in progress.call_args_list]
        self.assertIn('saving', stages)
        self.assertEqual(stages[-1], 'saved')
        self.assertNotIn('repair_citations', self.generator.call_args.kwargs)
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
        self.assertEqual(saved['version'], 3)
        self.assertIn('原始分析', saved['original_body'])
        self.assertNotIn('synthetic-session-credential', json.dumps(saved))
        self.assertEqual(self.generator.call_count, 1)
        original = next(item for item in revisions if item['version'] == 1)
        app.selectbox(key=f'research_revision_{identifier}').select(original['id']).run()
        app.button(key=f'research_restore_{identifier}').click().run()
        with ResearchStore(self.db) as store:
            restored = store.get_report(identifier)
        self.assertEqual(restored['version'], 4)
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
            'id': 2, 'title': '行業槓桿與AI科技融資研究', 'summary': 'Synthetic distinct financing excerpt.',
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
        self.patch('src.research_ui.run_generation', side_effect=lambda generate, update, **kwargs: run_generation(
            generate, update, **{**kwargs, 'timeout': 0.04, 'tick_seconds': 0.01}))
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
        self.assertIn('saving_initial', stages)
        self.assertNotIn('saved', stages)
        with ResearchStore(self.db) as store:
            self.assertEqual(store.list_reports(), [])

    def test_generation_explains_independent_review_and_retains_both_versions(self):
        app = self.app()
        self.assertFalse(any(item.key == 'research_repair_citations' for item in app.checkbox))
        self.assertTrue(any('審稿' in item.value and '2' in item.value for item in app.caption))
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        with ResearchStore(self.db) as store:
            reports = store.list_reports()
            self.assertEqual(len(reports), 1)
            report = reports[0]
            self.assertEqual(report['version'], 2)
            self.assertEqual(report['body'], self.reviewed_draft['body'])
            self.assertEqual(report['original_body'], self.initial_draft['body'])
            self.assertEqual([item['version'] for item in store.list_revisions(report['id'])], [2, 1])

    def test_review_updates_generation_progress_label(self):
        release = Event()
        self.addCleanup(release.set)
        bar = MagicMock()
        self.patch('src.research_ui.st.progress', return_value=bar)
        render_progress = ui._generation_progress

        def track_progress(progress_bar, stage, *args, **kwargs):
            render_progress(progress_bar, stage, *args, **kwargs)
            if stage == 'reviewing':
                release.set()

        self.patch('src.research_ui._generation_progress', side_effect=track_progress)

        def reviewed_draft(*args, on_progress, on_draft, **kwargs):
            on_draft(copy.deepcopy(self.initial_draft))
            on_progress('reviewing', 50)
            if not release.wait(2):
                raise AssertionError('Independent review progress was never displayed.')
            return copy.deepcopy(self.reviewed_draft)

        self.generator.side_effect = reviewed_draft
        app = self.app()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any('審稿' in call.kwargs.get('text', '')
                            for call in bar.progress.call_args_list))
        with ResearchStore(self.db) as store:
            self.assertEqual(len(store.list_reports()), 1)

    def test_writer_draft_is_persisted_before_independent_review_completes(self):
        observed = []

        def paused_review(*args, on_draft, on_progress, **kwargs):
            on_draft(copy.deepcopy(self.initial_draft))
            with ResearchStore(self.db) as store:
                reports = store.list_reports()
                observed.append(reports)
            on_progress('reviewing', 0)
            return copy.deepcopy(self.reviewed_draft)

        self.generator.side_effect = paused_review
        app = self.app()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(observed), 1)
        self.assertEqual(len(observed[0]), 1)
        self.assertEqual(observed[0][0]['body'], self.initial_draft['body'])
        self.assertEqual(observed[0][0]['version'], 1)
        self.assertEqual(observed[0][0]['metadata']['review_status'], 'pending')

    def test_review_failure_preserves_writer_draft_and_user_editing(self):
        def failed_review(*args, on_draft, on_progress, **kwargs):
            on_draft(copy.deepcopy(self.initial_draft))
            on_progress('reviewing', 0)
            return {**copy.deepcopy(self.initial_draft), 'metadata': {
                **self.initial_draft['metadata'], 'workflow_stage': 'review_failed',
                'review_status': 'review_failed', 'review_error': '審稿回應不完整，初稿仍已保存。'}}

        self.generator.side_effect = failed_review
        app = self.app()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        with ResearchStore(self.db) as store:
            report = store.list_reports()[0]
        self.assertEqual(report['body'], self.initial_draft['body'])
        self.assertEqual(report['original_body'], self.initial_draft['body'])
        self.assertEqual(report['metadata']['review_status'], 'review_failed')
        self.assertTrue(any('審稿未完成' in item.value for item in [*app.info, *app.warning]))
        app.text_area(key=f"research_editor_body_{report['id']}").input('審稿失敗後由我完成的正文').run()
        app.button(key='research_save_edit').click().run()
        with ResearchStore(self.db) as store:
            saved = store.get_report(report['id'])
        self.assertEqual(saved['body'], '審稿失敗後由我完成的正文')
        self.assertEqual(saved['version'], report['version'] + 1)
        self.assertEqual(self.generator.call_count, 1)

    def test_timeout_after_writer_save_keeps_initial_draft_editable_and_discards_late_review(self):
        release = Event()
        finished = Event()
        self.addCleanup(release.set)

        def delayed_review(*args, on_draft, on_progress, **kwargs):
            on_draft(copy.deepcopy(self.initial_draft))
            on_progress('reviewing', 0)
            release.wait(5)
            finished.set()
            return copy.deepcopy(self.reviewed_draft)

        self.generator.side_effect = delayed_review
        self.patch('src.research_ui.run_generation', side_effect=lambda generate, update, **kwargs: run_generation(
            generate, update, **{**kwargs, 'timeout': 0.3, 'tick_seconds': 0.01}))
        app = self.app()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        with ResearchStore(self.db) as store:
            reports = store.list_reports()
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertEqual(report['version'], 2)
        self.assertEqual(report['body'], self.initial_draft['body'])
        self.assertEqual(report['metadata']['review_status'], 'review_failed')
        self.assertTrue(any('逾時' in item.value for item in [*app.error, *app.warning]))
        app.text_area(key=f"research_editor_body_{report['id']}").input('逾時後保存的手動修改').run()
        app.button(key='research_save_edit').click().run()
        release.set()
        self.assertTrue(finished.wait(1))
        app.run()
        with ResearchStore(self.db) as store:
            saved = store.get_report(report['id'])
        self.assertEqual(saved['body'], '逾時後保存的手動修改')
        self.assertEqual(saved['version'], 3)
        self.assertEqual(saved['original_body'], self.initial_draft['body'])

    def test_review_cannot_overwrite_a_manual_edit_saved_from_another_window(self):
        edited_id = []

        def review_while_user_edits(*args, on_draft, on_progress, **kwargs):
            on_draft(copy.deepcopy(self.initial_draft))
            with ResearchStore(self.db) as store:
                report = store.list_reports()[0]
                edited_id.append(report['id'])
                store.save_revision(report['id'], title='我保存的標題', body='另一窗口保存的人工正文',
                                    expected_version=1, note='Manual edit while review runs')
            on_progress('reviewing', 0)
            return copy.deepcopy(self.reviewed_draft)

        self.generator.side_effect = review_while_user_edits
        app = self.app()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        with ResearchStore(self.db) as store:
            reports = store.list_reports()
            user_report = store.get_report(edited_id[0])
        self.assertEqual(len(reports), 2)
        self.assertEqual(user_report['body'], '另一窗口保存的人工正文')
        self.assertEqual(user_report['version'], 2)
        reviewed = next(report for report in reports if report['id'] != user_report['id'])
        self.assertEqual(reviewed['body'], self.reviewed_draft['body'])
        self.assertEqual(reviewed['metadata']['parent_report_id'], user_report['id'])
        self.assertEqual(user_report['original_body'], self.initial_draft['body'])

    def test_failed_review_conflict_stays_unreviewed_after_manual_edit_and_export(self):
        from src.research_export import export_html, export_markdown
        edited_id = []

        def failed_review_while_user_edits(*args, on_draft, on_progress, **kwargs):
            on_draft(copy.deepcopy(self.initial_draft))
            with ResearchStore(self.db) as store:
                report = store.list_reports()[0]
                edited_id.append(report['id'])
                store.save_revision(report['id'], title='人工版本', body='另一窗口的人工修改',
                                    expected_version=1, note='Edit while reviewer is pending')
            on_progress('reviewing', 0)
            return {**copy.deepcopy(self.initial_draft), 'metadata': {
                **self.initial_draft['metadata'], 'workflow_stage': 'review_failed',
                'review_status': 'review_failed', 'review_error': '審稿服務未完成；初稿保留。'}}

        self.generator.side_effect = failed_review_while_user_edits
        app = self.app()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        with ResearchStore(self.db) as store:
            original = store.get_report(edited_id[0])
            reports = store.list_reports()
        self.assertEqual(original['body'], '另一窗口的人工修改')
        self.assertEqual(len(reports), 2)
        failed = next(report for report in reports if report['id'] != original['id'])
        self.assertEqual(failed['metadata']['parent_report_id'], original['id'])
        self.assertEqual(failed['metadata']['review_status'], 'review_failed')
        self.assertNotIn('reviewed_version', failed['metadata'])
        app.selectbox(key='research_active_report').select(failed['id']).run()
        app.text_area(key=f"research_editor_body_{failed['id']}").input('審稿未完成，我自行修改。').run()
        app.button(key='research_save_edit').click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any('獨立審稿未完成' in item.value for item in app.info))
        self.assertFalse(any('AI審稿基於v' in item.value for item in app.info))
        with ResearchStore(self.db) as store:
            saved = store.get_report(failed['id'])
        self.assertEqual(saved['body'], '審稿未完成，我自行修改。')
        self.assertEqual(saved['metadata']['review_status'], 'review_failed')
        self.assertNotIn('reviewed_version', saved['metadata'])
        for export in (export_markdown, export_html):
            text = export(saved).decode('utf-8')
            self.assertIn('審稿未完成，初稿保留', text)
            self.assertNotIn('AI審稿基於v', text)
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


class ResearchPreparationUiTests(ResearchUiHarness, unittest.TestCase):
    def test_default_complete_report_requires_specific_evidence_despite_relevant_news(self):
        app = self.app(report_mode='research')
        self.assertEqual(app.radio(key='research_report_mode').value, '完整研究報告（建議）')
        self.assertEqual(app.multiselect(key='research_article_ids').value, ['1'])
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        self.assertTrue(app.button(key='research_generate').disabled)
        warning = ' '.join(item.value for item in app.warning)
        for required in ('行業持倉與擁擠度', 'FINRA', 'SEC', '同口徑歷史比較'):
            self.assertIn(required, warning)
        self.generator.assert_not_called()

        app.radio(key='research_report_mode').set_value('資料缺口簡報').run()
        self.assertFalse(app.button(key='research_generate').disabled)
        app.button(key='research_generate').click().run()
        self.assertEqual(len(app.exception), 0)
        arguments = self.generator.call_args.kwargs
        self.assertEqual(arguments['report_mode'], 'gap')
        self.assertFalse(arguments['research_brief']['ready'])
        with ResearchStore(self.db) as store:
            report = store.list_reports()[0]
        self.assertEqual(report['metadata']['report_mode'], 'gap')
        self.assertTrue(any('報告模式：資料缺口簡報' in item.value for item in app.warning))

    def test_unrelated_articles_require_show_all_and_are_never_quick_selected(self):
        unrelated = {**self.inbox.export_articles.return_value[0], 'id': 2, 'title': '城市公園春季園藝',
                     'summary': '市民學習種植花卉。', 'url': 'https://example.org/garden'}
        self.inbox.export_articles.return_value.append(unrelated)
        app = self.app(report_mode='research')
        self.assertEqual(app.multiselect(key='research_article_ids').value, ['1'])
        self.assertEqual(len(app.multiselect(key='research_article_ids').options), 1)
        self.assertTrue(any('推薦｜' in item.value and '主題詞' in item.value for item in app.caption))
        app.checkbox(key='research_show_all_articles').check().run()
        self.assertEqual(len(app.multiselect(key='research_article_ids').options), 2)
        self.assertEqual(app.multiselect(key='research_article_ids').value, ['1'])
        app.button(key='research_clear_articles').click().run()
        app.button(key='research_select_articles').click().run()
        self.assertEqual(app.multiselect(key='research_article_ids').value, ['1'])
        self.assertEqual(len(app.exception), 0)

    def test_unrelated_topic_has_no_automatic_catalog_selection(self):
        app = self.app(report_mode='research')
        app.text_input(key='research_topic').input('火星探測').run()
        self.assertEqual(app.multiselect(key='research_article_ids').value, [])
        self.assertTrue(app.button(key='research_fetch_recommended').disabled)
        app.checkbox(key='research_use_evidence').check().run()
        self.assertEqual(app.multiselect(key='research_official_keys').value, [])
        self.assertEqual(len(app.exception), 0)

    def test_gap_fetch_preserves_previous_success_when_refresh_fails(self):
        evidence = {'title': ui.CATALOG['finra'][0], 'content': 'FINRA margin balances excerpt.',
                    'source_type': 'official', 'publisher': 'FINRA', 'url': ui.CATALOG['finra'][1]}
        collector = self.patch('src.research_ui.collect_evidence', side_effect=[
            {'evidence': [evidence], 'sources': [{'id': 'finra', 'status': 'ok', 'name': 'FINRA'},
                {'id': 'sec_pf', 'status': 'error', 'name': 'SEC', 'error': 'Synthetic fetch failure'}]},
            {'evidence': [], 'sources': [{'id': 'finra', 'status': 'error', 'name': 'FINRA',
                                         'error': 'Synthetic refresh failure'}]}])
        app = self.app(report_mode='research')
        app.button(key='research_fetch_recommended').click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(app.checkbox(key='research_use_evidence').value)
        self.assertEqual(set(collector.call_args.args[0]), {'sector_pack', 'finra', 'sec_pf'})
        self.assertEqual(len(app.session_state['research_official_evidence']), 1)
        self.assertTrue(any('待提取與核對｜FINRA' in item.value for item in app.markdown))
        self.assertTrue(any('Synthetic fetch failure' in item.value for item in app.warning))
        app.button(key='research_fetch_recommended').click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state['research_official_evidence'], [evidence])
        self.assertTrue(any('先前成功取得的資料已保留' in item.value for item in app.warning))
        self.assertTrue(any('公開核對資料1份' in item.value for item in app.caption))
        self.assertTrue(app.button(key='research_generate').disabled)

    def _generic_data_app(self):
        article = self.inbox.export_articles.return_value[0]
        article.update(title='AI revenue', content_scope='provided_text', summary='',
            content='AI revenue for US sample was 10 USD billion in 2026-08. '
                    'AI revenue for US sample was 12 USD billion in 2026-09.')
        app = self.app(report_mode='research')
        app.text_input(key='research_topic').input('AI revenue').run()
        app.button(key='research_select_articles').click().run()
        app.sidebar.text_input(key='ai_api_key').input('synthetic-session-credential').run()
        return app

    def _observation(self, period='2026-08', value='10'):
        return {'source_id': 'S1', 'requirement_id': 'generic_data', 'metric': 'AI revenue',
                'value': value, 'unit': 'USD billion', 'period': period, 'scope': 'US sample',
                'evidence_text': f'AI revenue for US sample was {value} USD billion in {period}.'}

    def test_manual_observations_validate_anchors_and_show_comparable_periods(self):
        app = self._generic_data_app()
        self.assertTrue(app.button(key='research_generate').disabled)
        for period, value in [('2026-08', '10'), ('2026-09', '12')]:
            app.selectbox(key='research_observation_source').select('S1')
            app.selectbox(key='research_observation_requirement').select('generic_data')
            for key, item in {'metric': 'AI revenue', 'value': value, 'unit': 'USD billion',
                              'period': period, 'scope': 'US sample'}.items():
                app.text_input(key='research_observation_' + key).input(item)
            app.text_area(key='research_observation_excerpt').input(self._observation(period, value)['evidence_text'])
            next(item for item in app.button if item.label == '核對並加入觀測值').click().run()
            self.assertEqual(len(app.exception), 0)
        self.assertFalse(app.button(key='research_generate').disabled)
        self.assertEqual(len(app.dataframe), 2)
        self.assertEqual(list(app.dataframe[1].value['變化（原單位／百分點）']), ['2'])
        self.assertEqual(list(app.dataframe[1].value['相對變化（%）']), ['20'])
        app.button(key='research_generate').click().run()
        self.assertEqual(self.generator.call_args.kwargs['report_mode'], 'research')
        self.assertTrue(self.generator.call_args.kwargs['research_brief']['ready'])

    def test_invalid_manual_excerpt_does_not_unlock_generation(self):
        app = self._generic_data_app()
        app.selectbox(key='research_observation_source').select('S1')
        app.selectbox(key='research_observation_requirement').select('generic_data')
        for key, item in {'metric': 'AI revenue', 'value': '999', 'unit': 'USD billion',
                          'period': '2026-08', 'scope': 'US sample'}.items():
            app.text_input(key='research_observation_' + key).input(item)
        app.text_area(key='research_observation_excerpt').input('AI revenue for US sample was 999 USD billion in 2026-08.')
        next(item for item in app.button if item.label == '核對並加入觀測值').click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(app.button(key='research_generate').disabled)
        self.assertTrue(any('完整原句' in item.value for item in app.warning), [item.value for item in app.warning])
        self.generator.assert_not_called()

    def test_observation_cannot_rebind_to_new_source_that_reuses_s1(self):
        app = self._generic_data_app()
        original = ui.prepare_sources(self.inbox.export_articles.return_value)[0]
        app.session_state['research_observations'] = [{**self._observation(),
            'source_fingerprint': ui._source_fingerprint(original)}]
        app.run()
        self.assertFalse(app.button(key='research_generate').disabled)
        replacement = {**self.inbox.export_articles.return_value[0], 'id': 2, 'title': 'AI revenue revision',
                       'content': original['content'] + ' Different data revision.', 'url': 'https://example.org/revised'}
        self.inbox.export_articles.return_value = [replacement]
        app.run()
        app.multiselect(key='research_article_ids').set_value(['2']).run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(app.button(key='research_generate').disabled)
        self.assertTrue(any('來源已改變' in item.value for item in app.warning))

    def test_ai_preparation_saves_only_accepted_bound_rows_and_displays_rejections(self):
        app = self._generic_data_app()
        sources = ui.prepare_sources(self.inbox.export_articles.return_value)
        result = ui.build_research_brief('AI revenue', sources, observations=[self._observation()], as_of='2026-10-14')
        result['preparation'] = {'suggested_count': 2, 'accepted_count': 1, 'manually_verified': False}
        result['warnings'] = [{'code': 'unanchored_value', 'message': 'Synthetic rejected row', 'observation': 2}]
        extractor = self.patch('src.research_ui.extract_research_observations', return_value=result)
        app.button(key='research_organize_data').click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.session_state['research_observations']), 1)
        self.assertIn('source_fingerprint', app.session_state['research_observations'][0])
        self.assertTrue(any('建議2筆，通過原文錨點檢查1筆' in item.value for item in app.caption))
        self.assertTrue(any('Synthetic rejected row' in item.value for item in app.warning))
        self.assertFalse(app.button(key='research_generate').disabled)
        extractor.assert_called_once()
        self.generator.assert_not_called()


class ResearchHistorySelectionTests(ResearchUiHarness, unittest.TestCase):
    def test_pages_preserve_selected_articles_and_include_old_matches(self):
        original = self.inbox.export_articles.return_value[0]
        self.inbox.export_articles.return_value = [
            {**original, 'id': i, 'title': f'AI earnings {i}',
             'url': f'https://example.org/articles/{i}'} for i in range(1, 602)]
        app = self.app()
        app.text_input(key='research_topic').input('AI earnings').run()
        self.assertEqual(app.number_input(key='research_candidate_page').max, 4)
        app.multiselect(key='research_article_ids').set_value(['1']).run()
        app.number_input(key='research_candidate_page').set_value(4).run()
        app.multiselect(key='research_article_ids').set_value(['1', '601']).run()
        app.number_input(key='research_candidate_page').set_value(1).run()
        self.assertEqual(app.multiselect(key='research_article_ids').value, ['1', '601'])
        self.assertEqual(len(app.exception), 0)
        self.generator.assert_not_called()

    def test_reader_handoff_keeps_explicit_historical_selection(self):
        original = self.inbox.export_articles.return_value[0]
        self.inbox.export_articles.return_value.append({
            **original, 'id': 2, 'title': 'Historical AI outlook',
            'published_at': '2024-01-01T00:00:00+00:00', 'url': 'https://example.org/old'})
        app = self.app()
        app.session_state['research_pending_article_ids'] = ['2']
        app.run()
        self.assertIn('2', app.multiselect(key='research_article_ids').value)
        self.assertTrue(any('目前篩選範圍外' in item.value for item in app.info))
        app.button(key='research_clear_articles').click().run()
        self.assertEqual(app.multiselect(key='research_article_ids').value, [])
        self.assertEqual(len(app.exception), 0)
        self.generator.assert_not_called()


if __name__ == '__main__':
    unittest.main()
