"""Network-free end-to-end regressions for conference collection and cache state."""
import copy
import datetime as dt
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from scripts.collect_papers import collect, parse_conference_sources, parse_topics

YEAR = dt.datetime.now(dt.timezone.utc).year - 1


def config(venues=None):
    return {'sources':[{'type':'arxiv','name':'arXiv'}],
            'conference_sources':{'enabled':True,'years':[YEAR], 'venues':venues or [
                {'id':'acl','name':'ACL','provider':'acl','group':'NLP'}]},
            'topics':[{'id':'llm','name':'LLM','description':'',
                       'keywords':['LoRA','fine tuning'],'required_keywords':['LLM'],
                       'arxiv_categories':['cs.CL']} ]}


def paper(identity='acl:example',venue='acl'):
    return {'id':identity,'title':'LoRA fine tuning for LLM', 'source':'Official',
            'source_type':'conference','authors':['Example'],
            'summary':'LoRA improves LLM fine tuning with stable adaptation. '*8,
            'abstract_source':'ACL','paper_url':'https://aclanthology.org/example/',
            'pdf_url':'https://aclanthology.org/example.pdf','categories':[],
            'published':f'{YEAR}-01-01T00:00:00+00:00','updated':f'{YEAR}-01-01T00:00:00+00:00',
            'conference':{'id':venue,'name':venue.upper(),'year':YEAR,'track':'main',
                          'status':'published','evidence_url':'https://aclanthology.org/example/'}}


class ConferenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.cfg=self.root/'config.json'
        self.daily=self.root/'papers.json'
        self.conference=self.root/'conference.json'
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ,{
            'MAX_CONFERENCE_YEAR_FETCHES':'12','MIN_DAILY_PAPERS':'0',
            'MAX_CONFERENCE_ABSTRACT_ENRICHMENTS':'0','GITHUB_TOKEN':'',
            'LLM_API_KEY':'','OPENAI_API_KEY':'','DEEPSEEK_API_KEY':''},clear=True))
        self.stack.enter_context(mock.patch('scripts.collect_papers.time.sleep'))
        self.stack.enter_context(mock.patch('scripts.collect_papers.fetch_source_topic',return_value=[]))
        self.official=self.stack.enter_context(mock.patch('scripts.collect_papers.fetch_official_conference',return_value=([],{'missing':True})))
        self.dblp=self.stack.enter_context(mock.patch('scripts.collect_papers.fetch_dblp_proceedings',return_value=([],{})))
        self.write_config(config())

    def write_config(self,value):
        self.cfg.write_text(json.dumps(value))

    def run_collection(self,summaries=0):
        collect(self.cfg,self.daily,self.conference,days=1,max_per_topic=1,
                max_summaries=summaries,max_new_papers=10,max_stored_papers=10,
                max_new_conference_papers=10,max_stored_conference_papers=20,
                max_data_bytes=0,incremental_since_last_run=False,
                recent_history_days=45,clear_cache=False)
        return json.loads(self.conference.read_text())

    def test_empty_manifest_survives_next_run_and_avoids_refetch(self):
        first=self.run_collection()
        self.assertEqual(first['papers'],[])
        self.assertIn(f'acl:{YEAR}',first['conference_fetch_state'])
        self.official.reset_mock()
        second=self.run_collection()
        self.official.assert_not_called()
        self.assertEqual(first['conference_fetch_state'],second['conference_fetch_state'])

    def test_refresh_failure_retains_existing_relevant_papers(self):
        self.official.return_value=([paper()],{'requests':1})
        first=self.run_collection()
        self.assertEqual(len(first['papers']),1)
        first['conference_fetch_state']={}
        self.conference.write_text(json.dumps(first))
        self.official.side_effect=RuntimeError('HTTP 202 anti-bot challenge')
        second=self.run_collection()
        self.assertEqual([p['id'] for p in second['papers']],['acl:example'])
        self.assertEqual(second['conference_fetch_state'][f'acl:{YEAR}']['status'],'error')

    def test_budget_is_applied_to_actual_fetches(self):
        venues=[{'id':i,'name':i.upper(),'provider':'acl'} for i in ['acl','emnlp','naacl']]
        self.write_config(config(venues))
        os.environ['MAX_CONFERENCE_YEAR_FETCHES']='2'
        result=self.run_collection()
        self.assertEqual(self.official.call_count,2)
        self.assertEqual(result['stats']['deferred_conference_years'],1)
        self.assertEqual(len(result['conference_fetch_state']),2)

    def test_official_duplicate_replaces_dblp_same_title_year(self):
        venues=[{'id':'acl','name':'ACL','provider':'acl'},
                {'id':'kdd','name':'KDD','provider':'dblp','options':{'stream':'conf/kdd'}}]
        self.write_config(config(venues))
        self.official.return_value=([paper()],{'requests':1})
        self.dblp.return_value=([paper('dblp:conf/kdd/example','kdd')],{'requests':1})
        result=self.run_collection()
        self.assertEqual([p['id'] for p in result['papers']],['acl:example'])

    def test_cached_chinese_summary_does_not_repeat_llm_work(self):
        previous=paper()
        previous['chinese_summary']={'problem':'Verified cached summary','method':'Existing translation'}
        self.official.return_value=([previous],{'requests':1})
        first=self.run_collection()
        self.assertEqual(first['papers'][0]['chinese_summary']['problem'],'Verified cached summary')
        with mock.patch('scripts.collect_papers.summarize_with_llm') as summarize:
            summarize.return_value=({'problem':'Regenerated'},first['papers'][0]['best_match'])
            second=self.run_collection(summaries=1)
        summarize.assert_not_called()
        self.assertEqual(second['papers'][0]['chinese_summary']['problem'],'Verified cached summary')

    def test_repository_keeps_all_nine_topics_and_rolling_years(self):
        repository=Path(__file__).parents[1]
        actual=json.loads((repository/'config/interests.json').read_text())
        ids={t.id for t in parse_topics(actual)}
        self.assertEqual(len(ids),9)
        self.assertTrue({'radiotherapy_dose_prediction','intelligent_radiotherapy_planning',
                         'medical_image_segmentation','time_series_analysis_forecasting',
                         'time_series_anomaly_detection'}.issubset(ids))
        now=dt.datetime(YEAR+2,1,1,tzinfo=dt.timezone.utc)
        sources=parse_conference_sources(actual,now)
        self.assertTrue({'acl','emnlp','iclr','neurips','icml','cvpr','miccai','kdd','sigir'}.issubset({s.id for s in sources}))
        for s in sources:
            self.assertTrue(set(s.years).issubset({now.year,now.year-1,now.year-2}))

    def test_expired_scopes_rotate_instead_of_starving_later_failures(self):
        from scripts.conference_pipeline import refresh_plan, signature
        now=dt.datetime.now(dt.timezone.utc)
        configured=config([{'id':str(i),'name':str(i),'provider':'acl'} for i in range(3)])
        sources=parse_conference_sources(configured,now)
        topics=parse_topics(configured)
        state={f'{s.id}:{YEAR}':{'signature':signature(s,topics),'status':'error',
                'checked_at':(now-dt.timedelta(days=2)).isoformat()} for s in sources}
        first,_=refresh_plan(sources,topics,state,now,2)
        for _,_,key,_ in first:
            state[key]['checked_at']=now.isoformat()
        second,_=refresh_plan(sources,topics,state,now+dt.timedelta(days=1,seconds=1),2)
        self.assertEqual(second[0][2],f'2:{YEAR}')

    def test_real_abstract_upgrades_title_only_fallback_summary(self):
        from scripts.conference_pipeline import select_new_and_refresh
        old=paper('dblp:conf/kdd/example')
        old.pop('abstract_source')
        old['summary']='DBLP 题录：ACL 会议论文。'
        old['chinese_summary']={'problem':'DBLP没有摘要'}
        fresh=paper()
        selected=select_new_and_refresh([fresh],{'papers':[old]},10)
        self.assertFalse(selected[0].get('summary_reused'))
        self.assertNotEqual(selected[0].get('chinese_summary'),old['chinese_summary'])

    def test_bibliographic_refresh_preserves_previously_enriched_abstract(self):
        from scripts.conference_pipeline import select_new_and_refresh
        old=paper('dblp:conf/kdd/example')
        old['abstract_source']='arXiv'
        old['chinese_summary']={'problem':'Verified translation'}
        fresh=copy.deepcopy(old)
        fresh.pop('abstract_source')
        fresh.pop('chinese_summary')
        fresh['summary']='DBLP 题录：ACL 会议论文。'
        selected=select_new_and_refresh([fresh],{'papers':[old]},10)
        self.assertEqual(selected[0]['summary'],old['summary'])
        self.assertEqual(selected[0]['chinese_summary'],old['chinese_summary'])
