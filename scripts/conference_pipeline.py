"""Bounded conference refresh state and topic-balanced selection (no network)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections import defaultdict


def signature(source, topics):
    value = {"version": 2, "source": source.__dict__,
             "topics": [topic.__dict__ for topic in topics]}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:20]


def refresh_plan(sources, topics, state, now, limit=12):
    """Fresh/missing/error results expire; paper existence is never scan completion.

    New/changed scopes precede expired ones. Interleave years by source, so the
    first bounded run samples ML, NLP, vision and medicine instead of one corpus.
    """
    due = []
    for position, source in enumerate(sources):
        fingerprint = signature(source, topics)
        ordered_years = sorted(source.years, key=lambda year: (year != now.year - 1, -year))
        for year_index, year in enumerate(ordered_years):
            key = f"{source.id}:{year}"
            entry = state.get(key, {})
            same_scope = entry.get("signature") == fingerprint
            try:
                checked = dt.datetime.fromisoformat(entry.get("checked_at", ""))
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=dt.timezone.utc)
                elapsed = now - checked
            except (ValueError, TypeError):
                checked = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
                elapsed = dt.timedelta.max
            if entry.get("status") == "error":
                ttl = 1
            elif entry.get("status") == "missing" or year >= now.year:
                ttl = 7
            else:
                ttl = 30
            if same_scope and elapsed < dt.timedelta(days=ttl):
                continue
            priority = (True, checked, year_index, position) if same_scope else (False, year_index, position)
            due.append((priority, source, year, key, fingerprint))
    due.sort(key=lambda item: item[0])
    selected = due[:max(0, limit)]
    return [(s, y, k, sig) for _, s, y, k, sig in selected], len(due) - len(selected)


def canonical_conference_id(value):
    return re.sub(r"_20\d{2}$", "", str(value))


def migrate_conference_papers(payload):
    for paper in payload.get("papers", []):
        conf = paper.get("conference")
        if not isinstance(conf, dict):
            continue
        conf["id"] = canonical_conference_id(conf.get("id", ""))
        conf["name"] = re.sub(r"\s+20\d{2}$", "", str(conf.get("name", "")))
        paper.setdefault("published_precision", "year")
        conf.setdefault("evidence_url", "https://dblp.org/rec/" + conf["dblp_key"] if conf.get("dblp_key") else paper.get("paper_url", ""))
        conf.setdefault("status", "proceedings-indexed")
    return payload


def title_year_key(paper):
    title = re.sub(r"[^\w]", "", str(paper.get("title", "")).casefold())
    return title, (paper.get("conference") or {}).get("year")


def dedupe_conference_papers(papers):
    """Prefer official proceedings over DBLP, preserving one title/year record."""
    by_key = {}
    for paper in papers:
        key = title_year_key(paper)
        if not key[0]:
            key = (paper.get("id"), key[1])
        old = by_key.get(key)
        if old is None or (str(old.get("id", "")).startswith("dblp:")
                           and not str(paper.get("id", "")).startswith("dblp:")):
            by_key[key] = paper
    return list(by_key.values())


def balanced_selection(papers, limit):
    """Round robin best-topic buckets; preserve relevance order within each."""
    if limit <= 0 or len(papers) <= limit:
        return list(papers)
    buckets = defaultdict(list)
    for paper in papers:
        buckets[(paper.get("best_match") or {}).get("topic_id", "")].append(paper)
    selected = []
    for index in range(max(map(len, buckets.values()), default=0)):
        for bucket in buckets.values():
            if index < len(bucket):
                selected.append(bucket[index])
                if len(selected) == limit:
                    return selected
    return selected


def select_new_and_refresh(papers, existing, limit):
    previous = {title_year_key(p): p for p in existing.get("papers", [])}
    new, refreshed = [], []
    for paper in dedupe_conference_papers(papers):
        old = previous.get(title_year_key(paper))
        if old is None:
            new.append(paper)
        else:
            if old.get("first_seen_at"):
                paper.setdefault("first_seen_at", old["first_seen_at"])
            # A metadata refresh must not discard a previously verified abstract.
            if old.get("abstract_source") and not paper.get("abstract_source"):
                for field in ("summary", "abstract_source", "abstract_source_url"):
                    if old.get(field):
                        paper[field] = old[field]
            if old.get("chinese_summary") and old.get("summary") == paper.get("summary"):
                paper["chinese_summary"] = old["chinese_summary"]
                paper["summary_reused"] = True
            else:
                paper.pop("chinese_summary", None)
                paper.pop("summary_reused", None)
            refreshed.append(paper)
    return balanced_selection(new, limit) + refreshed
