import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.research_evidence import EvidenceError, MAX_CHARS, _pdf, fetch_evidence
from src.research import prepare_evidence


def response(text, status=200):
    return SimpleNamespace(status_code=status, iter_content=lambda **kwargs: iter([text.encode()]), close=Mock())


class EvidenceTests(unittest.TestCase):
    def test_finra_records_observation_not_fetch_date(self):
        fake = response('<h1>Margin Statistics</h1><p>FINRA Statistics ($ millions)</p><table><tr><td>Aug-26</td><td>1,453,832</td></tr><tr><td>Jul-26</td><td>1,417,225</td></tr></table>')
        item = fetch_evidence('finra', get=Mock(return_value=fake))
        self.assertEqual(item['observation_date'], '2026-08-31')
        self.assertIn('1,453,832', item['content'])
        self.assertEqual(item['source_type'], 'official')
        source = prepare_evidence([], [item])[0]
        self.assertIsNone(source['publication_date'])
        self.assertEqual(source['observation_date'], '2026-08-31')
        fake.close.assert_called_once()

    def test_scoos_follows_only_latest_official_link(self):
        landing = response('<a href="/data/scoos/scoos-202606.htm">June</a><a href="/data/scoos/scoos-202609.htm">September</a>')
        report = response('<h1>Summary</h1><p>Observed June to August 2026.</p>')
        get = Mock(side_effect=[landing, report])
        item = fetch_evidence('scoos', get=get)
        self.assertIn('202609.htm', item['url'])
        self.assertIn('Observed June to August', item['content'])
        self.assertEqual(len(get.call_args_list), 2)
        self.assertFalse(get.call_args_list[0].kwargs['allow_redirects'])
        source = prepare_evidence([], [item])[0]
        self.assertIsNone(source['published_at'])
        self.assertEqual(source['observation_date'], item['observation_date'])

    def test_manual_quarter_is_preserved_without_inventing_a_day(self):
        source = prepare_evidence([], [{'title': 'Quarterly table', 'content': 'Financing 8 billion.',
            'source_type': 'manual', 'observation_date': '2026Q3'}])[0]
        self.assertEqual(source['observation_date'], '2026Q3')
        self.assertIsNone(source['publication_date'])

    def test_failure_does_not_echo_response(self):
        fake = response('sensitive synthetic credential', status=403)
        with self.assertRaises(EvidenceError) as caught:
            fetch_evidence('finra', get=Mock(return_value=fake))
        self.assertNotIn('sensitive', str(caught.exception))

    def test_long_front_matter_cannot_hide_table(self):
        pages = [SimpleNamespace(extract_text=lambda: 'cover ' * MAX_CHARS),
                 SimpleNamespace(extract_text=lambda: 'Table 8.27\nPrime Broker 3,237')]
        with patch('pypdf.PdfReader', return_value=SimpleNamespace(pages=pages)):
            text = _pdf(b'fake', ('Table 8.27',))
        self.assertIn('Prime Broker 3,237', text)
        self.assertLessEqual(len(text), MAX_CHARS)

    def test_marker_after_long_header_is_retained(self):
        page = SimpleNamespace(extract_text=lambda: 'header ' * MAX_CHARS + 'Table 8.28\nTop25 55.3')
        with patch('pypdf.PdfReader', return_value=SimpleNamespace(pages=[page])):
            text = _pdf(b'fake', ('Table 8.28',))
        self.assertIn('Top25 55.3', text)

    def test_overbroad_pdf_is_explicit_error_not_silent_page_loss(self):
        pages = [SimpleNamespace(extract_text=lambda: 'Table 8.27\nData') for _ in range(30)]
        with patch('pypdf.PdfReader', return_value=SimpleNamespace(pages=pages)):
            with self.assertRaises(EvidenceError) as caught:
                _pdf(b'fake', ('Table 8.27',))
        self.assertEqual(caught.exception.reason, 'size')


if __name__ == '__main__':
    unittest.main()
