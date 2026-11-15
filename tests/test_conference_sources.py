import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import conference_sources as c


class OfficialConferenceTests(unittest.TestCase):
    def source(self, provider, id='acl', **options):
        return dict(id=id, name=id.upper(), group='test', provider=provider, options=options)

    def test_acl_filters_workshops_and_labels_findings(self):
        def entry(id, title):
            return f'''<div class="d-sm-flex align-items-stretch mb-3"><strong><a href=/{id}/>{title}</a></strong><a href=/people/alice/>Alice</a></div><div class="abstract-collapse"><div class="card-body p-3 small">A language model with LoRA.</div></div>'''
        text = ''.join(entry(id, 'LoRA research') for id in ['2025.acl-long.1', '2025.acl-short.2', '2025.findings-acl.3', '2025.acl-demo.4', '2025.workshop.5', '2024.acl-long.6', '2025.acl-long.0'])
        papers, scanned = c.parse_acl(text, self.source('acl'), 2025, [])
        self.assertEqual(scanned, 3)
        self.assertEqual([p['conference']['track'] for p in papers], ['main', 'main', 'findings'])
        self.assertEqual(papers[-1]['venue'], 'Findings of ACL')
        self.assertEqual(papers[0]['authors'], ['Alice'])
        self.assertEqual(papers[0]['summary'], 'A language model with LoRA.')

    def test_openreview_rechecks_accepted_venueid(self):
        venue = 'ICLR.cc/2025/Conference'
        def note(id, venueid, label):
            return {'id': id, 'content': {'title': {'value': 'LoRA'}, 'abstract': {'value': 'Language model'}, 'venueid': {'value': venueid}, 'venue': {'value': label}, 'authors': {'value': ['A']}}}
        notes = [note('yes', venue, 'ICLR 2025 Poster'), note('no', venue+'/Rejected', 'ICLR 2025'), note('bad', venue, 'Submitted to ICLR 2025'), note('old', 'ICLR.cc/2024/Conference', 'ICLR 2024')]
        result = c.parse_openreview(notes, self.source('openreview', 'iclr'), 2025, [], venue)
        self.assertEqual([p['id'] for p in result], ['openreview:yes'])
        self.assertEqual(result[0]['conference']['status'], 'accepted')
        self.assertEqual(result[0]['conference']['year'], 2025)

    def test_pmlr_exact_series_discovery_not_workshop(self):
        index = '<li><a href="v292">Volume 292</a> TerraBytes at ICML 2025</li><li><a href="v267">Volume 267</a> Proceedings of ICML 2025</li>'
        self.assertEqual(c.pmlr_volume(index, 'ICML', 2025), 'https://proceedings.mlr.press/v267/')
        self.assertIsNone(c.pmlr_volume(index, 'ICML', 2026))

    def test_cvf_excludes_workshop_and_other_year(self):
        def entry(venue):
            return f'<dt class="ptitle"><a href="/content/{venue}/html/Smith_Paper_paper.html">Time series</a></dt><dd><a href="#">Alice</a><a href="/paper.pdf">pdf</a></dd>'
        result, scanned = c.parse_cvf(entry('CVPR2025')+entry('CVPR2025W')+entry('CVPR2024'), self.source('cvf', 'cvpr'), 2025, [])
        self.assertEqual(len(result), 1)
        self.assertEqual(scanned, 1)
        self.assertEqual(result[0]['conference']['status'], 'published')

    def test_keyword_boundary_does_not_match_lora_in_exploration(self):
        self.assertFalse(c.relevant('Exploration', '', [{'keywords': ['LoRA']}]))
        self.assertTrue(c.relevant('LoRA adaptation', '', [{'keywords': ['LoRA']}]))

    def test_challenge_html_not_successful_empty_conference(self):
        with patch.object(c.Budget, 'get', return_value='<html>Checking your browser</html>'):
            with self.assertRaisesRegex(ValueError, 'no recognized paper entries'):
                c.fetch_official_conference(self.source('acl'), 2025, [], 10)

    def test_response_202_rejected(self):
        class Response:
            status = 202
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with patch('urllib.request.urlopen', return_value=Response()):
            with self.assertRaisesRegex(ValueError, 'HTTP 202'):
                c.Budget().get('https://aclanthology.org/events/acl-2025/')

    def test_openreview_pagination_budget_and_truncation(self):
        def get(budget, url, cache_index=False):
            budget.requests += 1
            budget.stats['requests'] = budget.requests
            return '{"count": 9000, "notes": [' + ','.join(['{}']*1000) + ']}'
        with patch.object(c.Budget, 'get', get):
            result, stats = c.fetch_official_conference(self.source('openreview', 'iclr', venue_id='ICLR.cc/{year}/Conference'), 2025, [], 10)
        self.assertEqual(stats['requests'], 8)
        self.assertEqual(stats['scanned'], 8000)
        self.assertTrue(stats['truncated'])
        self.assertEqual(result, [])

    def test_pmlr_no_proceedings_is_missing(self):
        with patch.object(c.Budget, 'get', return_value='<html><li><a href="v267">Volume 267</a> Proceedings of ICML 2025</li></html>'):
            result, stats = c.fetch_official_conference(self.source('pmlr', 'icml'), 2027, [], 10)
        self.assertEqual(result, [])
        self.assertTrue(stats['missing'])


if __name__ == '__main__':
    unittest.main()
