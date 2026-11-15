import unittest
from types import SimpleNamespace
from unittest import mock
from scripts.conference_dblp import (fetch_dblp_proceedings, paper_from_info,
    request_json, stream_for, title_matches, venue_matches)


def source(**options):
    return SimpleNamespace(id='kdd',name='KDD',group='data mining', options=options)


def info(**extra):
    item={'key':'conf/kdd/Example25','title':'Time Series Forecasting.',
          'venue':'KDD (2)','year':'2025','type':'Conference and Workshop Papers',
          'authors':{'author':{'text':'A. Author'}}, 'ee':'https://doi.org/example'}
    item.update(extra)
    return item


def payload(items,total=None):
    return {'result':{'hits':{'@total':str(len(items) if total is None else total),
                              'hit':[{'info':x} for x in items]}}}


class DblpTests(unittest.TestCase):
    def test_verifies_year_type_stream_and_main_venue(self):
        for extra in ({'year':'2024'}, {'key':'journals/corr/Example25'},
                      {'type':'Informal Publications'}, {'venue':'KDD Workshops'},
                      {'venue':'KDD (Companion)'}, {'venue':'Not KDD'}):
            self.assertIsNone(paper_from_info(info(**extra), source(),2025,'conf/kdd'))
        paper=paper_from_info(info(),source(),2025,'conf/kdd')
        self.assertEqual(paper['conference']['evidence_type'],'bibliographic_index')
        self.assertEqual(paper['date_precision'],'year')
        self.assertEqual(paper['id'],'dblp:conf/kdd/Example25')
        self.assertEqual(paper['authors'],['A. Author'])

    def test_stream_and_alias_configuration(self):
        self.assertEqual(stream_for(source(stream='conf/kdd')),'conf/kdd')
        self.assertTrue(venue_matches('KDD (1)',source()))
        self.assertFalse(venue_matches('KDD Workshops (1)',source()))
        self.assertTrue(venue_matches('Alternate (2)',source(venue_aliases=['Alternate'])))
        with self.assertRaises(ValueError): stream_for(source(stream='conf/kdd: evil'))

    def test_requires_subtopic_and_model_context(self):
        topics=[SimpleNamespace(keywords=['LoRA'],required_keywords=['LLM'])]
        self.assertTrue(title_matches('LoRA for LLM models',topics))
        self.assertFalse(title_matches('LoRA for vision',topics))
        self.assertFalse(title_matches('LLM applications',topics))

    @mock.patch('scripts.conference_dblp.time.sleep')
    @mock.patch('scripts.conference_dblp.request_json')
    def test_paginates_bounded_stream_year_query(self, get, sleep):
        get.side_effect=[payload([info()],2),payload([info(key='conf/kdd/Other25')],2)]
        papers,stats=fetch_dblp_proceedings(source(page_size=1),2025,[],10)
        self.assertEqual(len(papers),2)
        self.assertFalse(stats['truncated'])
        self.assertIn('f=1',get.call_args[0][0])
        self.assertIn('stream%3Aconf%2Fkdd%3A',get.call_args[0][0])
        self.assertEqual(stats['requests'],2)

    @mock.patch('scripts.conference_dblp.request_json')
    def test_empty_is_valid_only_with_zero_total(self,get):
        get.return_value=payload([])
        papers,stats=fetch_dblp_proceedings(source(),2025,[],10)
        self.assertEqual(papers,[])
        self.assertFalse(stats['truncated'])
        get.return_value=payload([],10)
        with self.assertRaises(RuntimeError): fetch_dblp_proceedings(source(),2025,[],10)

    @mock.patch('scripts.conference_dblp.time.sleep')
    @mock.patch('scripts.conference_dblp.request_json')
    def test_request_cap_reports_truncation(self,get,sleep):
        get.return_value=payload([info()],100)
        papers,stats=fetch_dblp_proceedings(source(page_size=1,max_requests=99),2025,[],100)
        self.assertEqual(get.call_count,8)
        self.assertTrue(stats['truncated'])
        self.assertEqual(len(papers),1)

    @mock.patch('scripts.conference_dblp.urllib.request.urlopen')
    def test_challenge_and_malformed_success_are_not_empty_results(self,open_url):
        response=open_url.return_value.__enter__.return_value
        response.status=202
        response.read.return_value=b'<html>Making sure you are not a bot</html>'
        with self.assertRaisesRegex(RuntimeError,'HTTP 202'): request_json('https://dblp.org/')
        response.status=200
        with self.assertRaisesRegex(RuntimeError,'non-JSON'): request_json('https://dblp.org/')
        response.read.return_value=b'{}'
        with self.assertRaisesRegex(RuntimeError,'result.hits'): request_json('https://dblp.org/')
