"""Bounded, validated DBLP bibliographic discovery (not an acceptance oracle).

Only one configured conference stream and year are queried. Strict venue labels
exclude workshops/companions even when they share the same DBLP stream. No
challenge solving, mirror hopping, abstract lookup, or paid API is performed.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any

ALIASES = {
    'miccai': ['MICCAI'], 'ipmi': ['IPMI'], 'isbi': ['ISBI'],
    'embc': ['EMBC'], 'bhi': ['BHI'], 'cinc': ['Computing in Cardiology', 'CinC'],
    'eccv': ['ECCV'], 'kdd': ['KDD'], 'www': ['WWW', 'The Web Conference'],
    'aaai': ['AAAI'], 'ijcai': ['IJCAI'], 'sigir': ['SIGIR'], 'cikm': ['CIKM'],
    'icdm': ['ICDM'], 'sdm': ['SDM'], 'icassp': ['ICASSP'], 'ijcnn': ['IJCNN'],
    'acm_mm': ['ACM Multimedia', 'ACM MM', 'MM'], 'mm': ['ACM Multimedia', 'ACM MM', 'MM'],
    'bmvc': ['BMVC'], 'cvpr': ['CVPR'], 'iccv': ['ICCV'], 'wacv': ['WACV'],
    'icml': ['ICML'], 'iclr': ['ICLR'], 'neurips': ['NeurIPS', 'NIPS'],
    'aistats': ['AISTATS'], 'uai': ['UAI'], 'midl': ['MIDL'], 'ml4h': ['ML4H'],
    'chil': ['CHIL'],
}


def text(value: Any) -> str:
    return re.sub(r'\s+', ' ', html.unescape(str(value or ''))).strip()


def words(value: str) -> str:
    return re.sub(r'[^\w]+', ' ', text(value).casefold()).strip()


def as_list(value: Any) -> list:
    return value if isinstance(value, list) else ([] if value is None else [value])


def stream_for(source: Any) -> str:
    options = getattr(source, 'options', {}) or {}
    stream = options.get('stream') or options.get('dblp_stream')
    if not stream:
        patterns = getattr(source, 'dblp_toc_patterns', [])
        match = re.search(r'db/(conf/[^/]+)/', patterns[0]) if patterns else None
        stream = match.group(1) if match else 'conf/' + {'acm_mm': 'mm', 'neurips': 'nips'}.get(source.id, source.id)
    if '/' not in stream:
        stream = 'conf/' + stream
    if not re.fullmatch(r'conf/[a-zA-Z0-9_-]+', stream):
        raise ValueError('DBLP source must specify a single conference stream')
    return stream


def venue_matches(venue: str, source: Any) -> bool:
    options = getattr(source, 'options', {}) or {}
    aliases = options.get('venue_aliases') or ALIASES.get(source.id) or [source.name]
    # A numeric suffix denotes a volume, not a workshop or companion track.
    base = re.sub(r'\s*\(\d+\)\s*$', '', text(venue))
    return any(words(base) == words(alias) for alias in aliases)


def title_matches(title: str, topics: list) -> bool:
    if not topics:
        return True
    normalized = ' ' + words(title) + ' '
    def hit(keyword):
        return (' ' + words(keyword) + ' ') in normalized
    for topic in topics:
        keywords = getattr(topic, 'keywords', [])
        required = getattr(topic, 'required_keywords', [])
        if any(hit(k) for k in keywords) and (not required or any(hit(k) for k in required)):
            return True
    return False


def request_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        'User-Agent': 'paper-daily/2.0 (+https://github.com/LcWave/paper-daily)',
        'Accept': 'application/json',
    })
    timeout = min(30, max(1, float(os.getenv('CONFERENCE_TIMEOUT_SECONDS', '20'))))
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read(8 * 1024 * 1024 + 1)
        if response.status != 200:
            raise RuntimeError(f'DBLP returned HTTP {response.status}, not a completed JSON response')
    if len(body) > 8 * 1024 * 1024:
        raise RuntimeError('DBLP response exceeded 8 MiB limit')
    try:
        result = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError('DBLP returned non-JSON content (possibly an anti-bot challenge)') from exc
    if not isinstance(result, dict) or not isinstance(result.get('result', {}).get('hits'), dict):
        raise RuntimeError('DBLP response lacks result.hits; refusing to treat it as an empty result')
    return result


def paper_from_info(info: dict, source: Any, year: int, stream: str) -> dict | None:
    key, title = text(info.get('key')), text(info.get('title')).rstrip('.')
    if not key.startswith(stream + '/') or not title:
        return None
    if str(info.get('year')) != str(year) or info.get('type') != 'Conference and Workshop Papers':
        return None
    venue = text(info.get('venue'))
    if not venue_matches(venue, source):
        return None
    authors = info.get('authors') or {}
    if not isinstance(authors, dict):
        authors = {}
    names = [text(a.get('text') if isinstance(a, dict) else a) for a in as_list(authors.get('author'))]
    url = 'https://dblp.org/rec/' + key
    ee = next((text(v) for v in as_list(info.get('ee')) if str(v).startswith(('https://', 'http://'))), '')
    doi = text(info.get('doi'))
    return {
        'id': 'dblp:' + key, 'source': 'DBLP · ' + source.name,
        'source_type': 'conference', 'title': title, 'authors': [n for n in names if n],
        'summary': f'DBLP 题录：{source.name} {year} 会议论文。尚无摘要；年份来自出版题录，不代表精确录用日期。',
        'published': f'{year}-01-01T00:00:00+00:00',
        'updated': f'{year}-01-01T00:00:00+00:00', 'date_precision': 'year',
        'paper_url': url, 'pdf_url': ee or url, 'venue': venue,
        'categories': [source.name, source.group, str(year)],
        'conference': {'id': source.id, 'name': source.name, 'group': source.group,
                       'year': year, 'dblp_key': key, 'doi': doi, 'ee': ee,
                       'pages': text(info.get('pages')), 'track': 'main',
                       'status': 'published', 'evidence_url': url,
                       'evidence_type': 'bibliographic_index', 'provider': 'dblp'},
    }


def fetch_dblp_proceedings(source: Any, year: int, topics: list, max_results: int) -> tuple[list[dict], dict]:
    stream = stream_for(source)
    options = getattr(source, 'options', {}) or {}
    max_requests = min(8, max(1, int(options.get('max_requests', 8))))
    page_size = min(1000, max(1, int(options.get('page_size', 1000))))
    max_results = max(1, int(max_results))
    endpoint = 'https://dblp.org/search/publ/api'
    query = f'stream:{stream}: year:{year}:'
    stats = {'provider': 'dblp', 'requests': 0, 'scanned': 0, 'total_hits': 0,
             'truncated': False, 'title_only': True, 'rejected_metadata': 0,
             'query': query, 'evidence_type': 'bibliographic_index'}
    papers, seen = [], set()
    offset = 0
    for page in range(max_requests):
        if page:
            time.sleep(max(0, float(os.getenv('DBLP_PATTERN_DELAY_SECONDS', '1'))))
        url = endpoint + '?' + urllib.parse.urlencode({'q': query, 'format': 'json', 'h': page_size, 'f': offset, 'c': 0})
        stats['requests'] += 1
        data = request_json(url)
        hits = data['result']['hits']
        try:
            total = int(hits['@total'])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError('DBLP result lacks a valid total count') from exc
        stats['total_hits'] = total
        batch = as_list(hits.get('hit'))
        if not batch and offset < total:
            raise RuntimeError('DBLP returned an incomplete page before its reported total')
        stats['scanned'] += len(batch)
        for hit in batch:
            info = hit.get('info', {}) if isinstance(hit, dict) else {}
            paper = paper_from_info(info, source, year, stream)
            if not paper:
                stats['rejected_metadata'] += 1
                continue
            if paper['id'] not in seen and title_matches(paper['title'], topics):
                seen.add(paper['id'])
                papers.append(paper)
        offset += len(batch)
        if offset >= total or len(papers) >= max_results:
            break
    stats['truncated'] = offset < stats['total_hits'] or len(papers) > max_results
    stats['matched'] = len(papers)
    stats['returned'] = min(len(papers), max_results)
    return papers[:max_results], stats
