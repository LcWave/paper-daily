import json
import unittest
from pathlib import Path
from scripts.collect_papers import parse_topics, arxiv_query_for_topic, score_paper, topic_plain_query


class LLMTopicTests(unittest.TestCase):
    def setUp(self):
        self.topics = parse_topics(json.loads((Path(__file__).parents[1] / 'config/interests.json').read_text()))
        self.llm = {t.id: t for t in self.topics if t.required_keywords}

    def test_preserves_existing_topics(self):
        self.assertEqual(len(self.topics), 9)
        self.assertEqual(sum(not t.required_keywords for t in self.topics), 5)

    def test_queries_require_model_context(self):
        for topic in self.llm.values():
            self.assertIn(') AND (', arxiv_query_for_topic(topic))
            self.assertIn('all:"large language model"', arxiv_query_for_topic(topic))
            self.assertTrue(topic_plain_query(topic).startswith('large language model '))
            self.assertLessEqual(len(topic.keywords), 8)

    def test_gradient_noise_is_not_llm(self):
        topic = self.llm['llm_retention_gradient_geometry']
        for title in ['Gradient alignment for image segmentation', 'Bellman gradient conflict optimization']:
            self.assertEqual(score_paper(topic, {'title': title, 'summary': '', 'categories': ['cs.LG']})['score'], 0)
        self.assertGreater(score_paper(topic, {'title': 'Gradient alignment for LLM fine-tuning', 'summary': ''})['score'], 0)

    def test_requires_subtopic_not_just_llm(self):
        topic = self.llm['llm_rlvr']
        self.assertEqual(score_paper(topic, {'title': 'Large language models for weather reports', 'summary': ''})['score'], 0)
        self.assertGreater(score_paper(topic, {'title': 'RLVR for large language models', 'summary': ''})['score'], 0)

    def test_expected_examples_match(self):
        examples = {
            'llm_efficient_finetuning': 'Low-rank adaptation of large language models',
            'llm_retention_gradient_geometry': 'Catastrophic forgetting in LLMs',
            'llm_reasoning_training': 'Chain-of-thought reasoning training in language models',
            'llm_rlvr': 'Contrastive policy optimization for LLMs',
        }
        for key, title in examples.items():
            self.assertGreater(score_paper(self.llm[key], {'title': title, 'summary': ''})['score'], 0)
