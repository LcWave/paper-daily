import datetime as dt
import unittest
from types import SimpleNamespace

from scripts.conference_pipeline import (
    refresh_plan, signature, migrate_conference_papers,
    dedupe_conference_papers, balanced_selection, select_new_and_refresh,
)


class ConferencePipelineTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc)
        self.sources = [SimpleNamespace(id=i, years=[2026, 2025], provider="acl") for i in ("acl", "emnlp")]
        self.topics = [SimpleNamespace(id="llm", keywords=["LoRA"])]

    def test_budget_interleaves_venues_not_all_years_of_first_venue(self):
        plan, deferred = refresh_plan(self.sources, self.topics, {}, self.now, 2)
        self.assertEqual([x[2] for x in plan], ["acl:2025", "emnlp:2025"])
        self.assertEqual(deferred, 2)

    def test_new_scope_runs_before_expired_scope(self):
        state = {"acl:2026": {"signature": signature(self.sources[0], self.topics), "checked_at": "2025-01-01", "status": "success"}}
        plan, _ = refresh_plan(self.sources, self.topics, state, self.now, 1)
        self.assertNotEqual(plan[0][2], "acl:2026")

    def test_topic_changes_invalidate_recent_cache(self):
        state = {"acl:2026": {"signature": signature(self.sources[0], self.topics), "checked_at": self.now.isoformat(), "status": "success"}}
        plan, _ = refresh_plan(self.sources, self.topics, state, self.now, 8)
        self.assertNotIn("acl:2026", [x[2] for x in plan])
        self.topics[0].keywords.append("GRPO")
        plan, _ = refresh_plan(self.sources, self.topics, state, self.now, 8)
        self.assertIn("acl:2026", [x[2] for x in plan])

    def test_missing_and_failure_do_not_get_permanent_cache(self):
        for status, age in [("missing", 8), ("error", 2), ("success", 31)]:
            state = {"acl:2025": {"signature": signature(self.sources[0], self.topics),
                "checked_at": (self.now - dt.timedelta(days=age)).isoformat(), "status": status}}
            plan, _ = refresh_plan(self.sources, self.topics, state, self.now, 8)
            self.assertIn("acl:2025", [x[2] for x in plan])

    def test_migrate_year_specific_ids_retains_evidence(self):
        paper = {"conference": {"id": "miccai_2025", "name": "MICCAI 2025", "dblp_key": "conf/miccai/test"}}
        migrate_conference_papers({"papers": [paper]})
        self.assertEqual(paper["conference"]["id"], "miccai")
        self.assertIn("dblp.org/rec/", paper["conference"]["evidence_url"])
        self.assertEqual(paper["published_precision"], "year")

    def test_balancing_preserves_small_topics(self):
        papers = [{"id": str(i), "best_match": {"topic_id": "medical"}} for i in range(20)]
        papers += [{"id": "llm", "best_match": {"topic_id": "llm"}}]
        self.assertEqual([p["id"] for p in balanced_selection(papers, 2)], ["0", "llm"])

    def test_official_dedup_and_new_papers_not_starved_by_old(self):
        old = {"id": "dblp:x", "title": "LoRA: Adaptation.", "conference": {"year": 2025}}
        official = {"id": "acl:x", "title": "LoRA Adaptation", "conference": {"year": 2025}}
        new = {"id": "acl:new", "title": "New LLM", "conference": {"year": 2025}}
        self.assertEqual(dedupe_conference_papers([old, official]), [official])
        selected = select_new_and_refresh([official, new], {"papers": [old]}, 1)
        self.assertEqual(selected, [new, official])


if __name__ == "__main__":
    unittest.main()
