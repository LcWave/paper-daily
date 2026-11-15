"""Bounded, credential-free adapters for official conference proceedings.

Only proceedings membership or OpenReview's exact accepted venueid establishes
acceptance. Keywords never establish venue/year. No arXiv acceptance inference.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

MAX_REQUESTS = 8
MAX_RESPONSE_BYTES = 24 * 1024 * 1024
_INDEX_CACHE: dict[str, str] = {}


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", value))).strip()


class Tags(HTMLParser):
    """Read links and citation metadata, including unquoted HTML attributes."""
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.meta: dict[str, list[str]] = {}
        self.href: str | None = None
        self.label: list[str] = []
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "a":
            self.href = a.get("href")
            self.label = []
        if tag == "meta" and a.get("name") and a.get("content"):
            self.meta.setdefault(str(a["name"]).lower(), []).append(str(a["content"]))

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.label.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.href is not None:
            self.links.append((self.href, clean("".join(self.label))))
            self.href = None


class Budget:
    def __init__(self) -> None:
        self.requests = 0
        self.stats: dict[str, Any] = {"requests": 0, "scanned": 0, "truncated": False,
                                      "missing": False, "detail_failures": 0}

    def get(self, url: str, cache_index: bool = False) -> str:
        if cache_index and url in _INDEX_CACHE:
            return _INDEX_CACHE[url]
        if self.requests >= MAX_REQUESTS:
            raise RuntimeError("Official proceedings request budget exhausted")
        if self.requests:
            time.sleep(0.5)
        self.requests += 1
        self.stats["requests"] = self.requests
        request = urllib.request.Request(url, headers={"User-Agent": "paper-daily-collector/1.0"})
        with urllib.request.urlopen(request, timeout=25) as response:
            if response.status != 200:
                raise ValueError(f"Official proceedings returned HTTP {response.status}, not a complete response")
            data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValueError("Official proceedings response exceeded 24 MiB safety limit")
        text = data.decode("utf-8", "replace")
        if cache_index:
            _INDEX_CACHE[url] = text
        return text


def attr(source: Any, key: str, default: Any = None) -> Any:
    return source.get(key, default) if isinstance(source, dict) else getattr(source, key, default)


def relevant(title: str, abstract: str, topics: list[Any]) -> bool:
    """Cheap candidate filter; the collector performs its stricter final scoring."""
    if not topics:
        return True
    text = f"{title} {abstract}".lower().replace("‑", "-").replace("–", "-")
    for topic in topics:
        for keyword in attr(topic, "keywords", []):
            phrase = str(keyword).lower()
            if re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text):
                return True
    return False


def paper(source: Any, year: int, key: str, title: str, url: str,
          abstract: str = "", authors: list[str] | None = None, pdf: str = "",
          track: str = "main") -> dict[str, Any]:
    provider = attr(source, "provider")
    name = attr(source, "name")
    venue_name = f"Findings of {name}" if track == "findings" else name
    result = {
        "id": f"{provider}:{key}", "source": f"{provider.upper()} · {venue_name}",
        "source_type": "conference", "title": title, "authors": authors or [],
        "summary": abstract, "published": f"{year}-01-01T00:00:00+00:00",
        "updated": f"{year}-01-01T00:00:00+00:00", "paper_url": url,
        "pdf_url": pdf or url, "venue": venue_name,
        "categories": [venue_name, attr(source, "group", "conference"), str(year)],
        "conference": {"id": attr(source, "id"), "name": venue_name,
                       "group": attr(source, "group", "conference"), "year": year,
                       "provider": provider, "track": track,
                       "status": "accepted" if provider in {"openreview", "miccai"} else "published",
                       "evidence_url": url, "date_precision": "year"},
    }
    if abstract:
        result["abstract_source"] = provider.upper()
        result["abstract_source_url"] = url
    return result


def parse_acl(text: str, source: Any, year: int, topics: list[Any]) -> tuple[list[dict], int]:
    event = attr(source, "options", {}).get("event", attr(source, "id")).lower()
    allowed = {f"{year}.{event}-long", f"{year}.{event}-short", f"{year}.{event}-main"}
    if attr(source, "options", {}).get("include_findings", True):
        allowed.add(f"{year}.findings-{event}")
    results, scanned = [], 0
    # Each entry starts with its PDF/Bib controls and ends after its abstract.
    chunks = re.split(r'<div\b[^>]*class=["\'][^"\']*d-sm-flex align-items-stretch[^"\']*["\'][^>]*>', text)
    for chunk in chunks[1:]:
        title_match = re.search(r"<strong>\s*(<a\b.*?</a>)\s*</strong>", chunk, re.S)
        if not title_match:
            continue
        links = Tags(title_match.group(1)).links
        if not links:
            continue
        href, title = links[0]
        identity = urllib.parse.urlparse(href).path.strip("/")
        bits = identity.rsplit(".", 1)
        if len(bits) != 2 or bits[0] not in allowed or not bits[1].isdigit() or int(bits[1]) == 0:
            continue
        scanned += 1
        summary = re.search(r'<div\b[^>]*class=["\'][^"\']*card-body[^"\']*["\'][^>]*>(.*?)</div>', chunk, re.S)
        abstract = clean(summary.group(1)) if summary else ""
        if not relevant(title, abstract, topics):
            continue
        author_region = chunk[title_match.end():].split("</div>", 1)[0]
        authors = [label for link, label in Tags(author_region).links if link.startswith("/people/")]
        url = f"https://aclanthology.org/{identity}/"
        results.append(paper(source, year, identity, title, url, abstract, authors,
                             f"https://aclanthology.org/{identity}.pdf",
                             "findings" if ".findings-" in identity else "main"))
    return results, scanned


def pmlr_volume(index: str, series: str, year: int) -> str | None:
    # Exact proceedings name excludes workshops hosted at the same conference.
    for entry in re.findall(r"<li\b[^>]*>(.*?)</li>", index, re.S):
        label = clean(entry)
        if not re.search(r"Proceedings (?:of|or) " + re.escape(series) + r"\s+" + str(year) + r"$", label, re.I):
            continue
        for href, _ in Tags(entry).links:
            if re.fullmatch(r"/?v\d+/?", href):
                return "https://proceedings.mlr.press/" + href.strip("/") + "/"
    return None


def parse_pmlr(text: str, source: Any, year: int, topics: list[Any], volume: str) -> tuple[list[dict], int]:
    results, scanned = [], 0
    for chunk in re.split(r'<div\s+class="paper"\s*>', text)[1:]:
        title_match = re.search(r'<p\s+class="title"\s*>(.*?)</p>', chunk, re.S)
        if not title_match:
            continue
        scanned += 1
        title = clean(title_match.group(1))
        if not relevant(title, "", topics):
            continue
        links = Tags(chunk).links
        url = next((href for href, label in links if label == "abs"), "")
        url = urllib.parse.urljoin(volume, url)
        if not url.startswith(volume) or not url.endswith(".html"):
            continue
        names = re.search(r'<span\s+class="authors"\s*>(.*?)</span>', chunk, re.S)
        authors = [x.strip() for x in clean(names.group(1)).split(",")] if names else []
        pdf = next((href for href, label in links if label == "Download PDF"), "")
        results.append(paper(source, year, urllib.parse.urlparse(url).path.strip("/"), title, url,
                             authors=authors, pdf=pdf))
    return results, scanned


def parse_cvf(text: str, source: Any, year: int, topics: list[Any]) -> tuple[list[dict], int]:
    results, scanned = [], 0
    conference = attr(source, "options", {}).get("conference", attr(source, "id")).upper()
    prefix = f"/content/{conference}{year}/html/"
    for chunk in re.split(r'<dt\s+class="ptitle"\s*>', text)[1:]:
        links = Tags(chunk).links
        if not links:
            continue
        href, title = links[0]
        if not href.startswith(prefix) or not href.endswith("_paper.html"):
            continue
        scanned += 1
        if not relevant(title, "", topics):
            continue
        authors = [label for link, label in links if link == "#"]
        pdf = next((link for link, label in links if label == "pdf"), "")
        url = urllib.parse.urljoin("https://openaccess.thecvf.com", href)
        results.append(paper(source, year, href.strip("/"), title, url, authors=authors,
                             pdf=urllib.parse.urljoin(url, pdf) if pdf else ""))
    return results, scanned


def parse_miccai(text: str, source: Any, year: int, topics: list[Any]) -> tuple[list[dict], int]:
    """Parse the Society's main-conference OA listing, never satellite events."""
    results, scanned = [], 0
    base = f"https://papers.miccai.org/miccai-{year}/"
    for chunk in re.split(r'<div\s+class=["\']posts-list-item["\'][^>]*>', text)[1:]:
        title_match = re.search(r"<b>(.*?)</b>", chunk, re.S)
        if not title_match:
            continue
        links = Tags(chunk).links
        href = next((link for link, label in links if label == "Paper Information and Reviews"), "")
        url = urllib.parse.urljoin(base, href)
        # Only the main annual collection's paper-detail files are evidence.
        parsed = urllib.parse.urlparse(url)
        if parsed.netloc != "papers.miccai.org" or not re.fullmatch(
                rf"/miccai-{year}/[0-9]+-Paper[0-9]+\.html", parsed.path):
            continue
        if not re.search(r"year\s*=\s*\{\s*" + str(year) + r"\s*\}", chunk):
            continue
        scanned += 1
        title = clean(title_match.group(1))
        if not relevant(title, "", topics):
            continue
        authors = [label for link, label in links if link.startswith(f"/miccai-{year}/tags#")]
        pdf = next((link for link, label in links if label == "PDF"), "")
        pdf = urllib.parse.urljoin(base, pdf)
        if not pdf.startswith(base + "paper/"):
            pdf = ""
        results.append(paper(source, year, parsed.path.strip("/"), title, url,
                             authors=authors, pdf=pdf))
    return results, scanned


def enrich_details(results: list[dict], budget: Budget) -> None:
    # At most six or seven relevant papers, never every paper in a volume.
    for result in results:
        if budget.requests >= MAX_REQUESTS:
            break
        try:
            text = budget.get(result["paper_url"])
        except (OSError, ValueError):
            budget.stats["detail_failures"] += 1
            continue
        abstract = re.search(r'<(?:div|p)\b[^>]*id=["\']abstract["\'][^>]*>(.*?)</(?:div|p)>', text, re.S)
        if result["conference"]["provider"] == "miccai":
            # Do not use description metadata: it also contains peer reviews.
            abstract = re.search(r'<h[1-6]\b[^>]*id=["\']abstract-id["\'][^>]*>.*?</h[1-6]>(.*?)(?=<h[1-6]\b|$)', text, re.S)
        if abstract:
            summary = clean(abstract.group(1))
            if summary:
                result.update(summary=summary, abstract_source=result["conference"]["provider"].upper(),
                              abstract_source_url=result["paper_url"])


def value(content: dict, field: str, default: Any = "") -> Any:
    item = content.get(field, default)
    return item.get("value", default) if isinstance(item, dict) else item


def parse_openreview(notes: list[dict], source: Any, year: int, topics: list[Any], venueid: str) -> list[dict]:
    results = []
    for note in notes:
        content = note.get("content", {})
        # Recheck each result. API filter alone is not an acceptance assertion.
        if value(content, "venueid") != venueid or note.get("ddate"):
            continue
        venue = str(value(content, "venue"))
        if re.search(r"submitted|withdrawn|reject|under review", venue, re.I):
            continue
        key, title = note.get("id"), str(value(content, "title"))
        abstract = str(value(content, "abstract"))
        if not key or not title or not relevant(title, abstract, topics):
            continue
        url = "https://openreview.net/forum?" + urllib.parse.urlencode({"id": key})
        result = paper(source, year, key, title, url, abstract,
                       list(value(content, "authors", [])),
                       "https://openreview.net/pdf?" + urllib.parse.urlencode({"id": key}))
        result["conference"]["accepted_venueid"] = venueid
        result["conference"]["acceptance_label"] = venue
        results.append(result)
    return results


def fetch_official_conference(source: Any, year: int, topics: list[Any], max_results: int) -> tuple[list[dict], dict]:
    """Return relevant candidates and explicit coverage statistics; raises on failure.

    source: object/mapping with id/name/group/provider/options. options keys:
    acl event/include_findings; pmlr series; cvf conference; openreview venue_id
    (template e.g. ICLR.cc/{year}/Conference); miccai requires no options.
    Only current API-v2 OpenReview venues supported.
    """
    provider, options = attr(source, "provider"), attr(source, "options", {})
    budget = Budget()
    results: list[dict] = []
    limit = max(1, min(int(max_results), 5000))
    try:
        if provider == "acl":
            event = options.get("event", attr(source, "id")).lower()
            url = f"https://aclanthology.org/events/{event}-{year}/"
            results, budget.stats["scanned"] = parse_acl(budget.get(url), source, year, topics)
        elif provider == "pmlr":
            index = budget.get("https://proceedings.mlr.press/", cache_index=True)
            if not re.search(r'href=["\']/?v\d+/?["\']', index):
                raise ValueError("PMLR index has no recognized volumes; possible challenge or HTML change")
            volume = pmlr_volume(index, options.get("series", attr(source, "id")).upper(), year)
            if not volume:
                budget.stats["missing"] = True
                return [], budget.stats
            results, budget.stats["scanned"] = parse_pmlr(budget.get(volume), source, year, topics, volume)
            budget.stats["title_only_filter"] = True
            enrich_details(results[:limit], budget)
        elif provider == "cvf":
            conference = options.get("conference", attr(source, "id")).upper()
            url = f"https://openaccess.thecvf.com/{conference}{year}?day=all"
            results, budget.stats["scanned"] = parse_cvf(budget.get(url), source, year, topics)
            budget.stats["title_only_filter"] = True
            enrich_details(results[:limit], budget)
        elif provider == "miccai":
            url = f"https://papers.miccai.org/miccai-{year}/"
            results, budget.stats["scanned"] = parse_miccai(budget.get(url), source, year, topics)
            budget.stats["title_only_filter"] = True
            enrich_details(results[:limit], budget)
        elif provider == "openreview":
            venueid = options["venue_id"].format(year=year)
            offset, page_size = 0, 1000
            while budget.requests < MAX_REQUESTS:
                url = "https://api2.openreview.net/notes?" + urllib.parse.urlencode({
                    "content.venueid": venueid, "limit": page_size, "offset": offset, "sort": "number:asc"})
                payload = json.loads(budget.get(url))
                if not isinstance(payload, dict) or not isinstance(payload.get("notes"), list):
                    raise ValueError("OpenReview response lacks a notes array")
                notes = payload["notes"]
                budget.stats["scanned"] += len(notes)
                results.extend(parse_openreview(notes, source, year, topics, venueid))
                offset += len(notes)
                total = payload.get("count")
                if len(notes) < page_size or (isinstance(total, int) and offset >= total):
                    break
            else:
                budget.stats["truncated"] = True
        else:
            raise ValueError(f"Unknown official conference provider: {provider}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            budget.stats["missing"] = True
            return [], budget.stats
        raise
    if len(results) > limit:
        budget.stats["truncated"] = True
    budget.stats["matched"] = len(results)
    if not budget.stats["scanned"]:
        if provider != "openreview":
            raise ValueError(f"{provider} proceedings page had no recognized paper entries; possible challenge or HTML change")
        budget.stats["missing"] = True
    return results[:limit], budget.stats
