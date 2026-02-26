#!/usr/bin/env python3
"""
UNDP Jobs Scraper — crawls listing pages at jobs.undp.org,
extracts Oracle Cloud job links, filters STRICTLY for D/P professional
levels only, and produces a valid RSS 2.0 feed (undp_jobs.xml).

NO Selenium.  Dependencies: requests, beautifulsoup4, lxml.
"""

import hashlib
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LISTING_URL = "https://jobs.undp.org/cj_view_jobs.cfm"
MAX_ITEMS = 200
FAIL_IF_ZERO_ITEMS = True       # exit non-zero AFTER writing RSS
HARD_PAGE_CAP = 25              # max listing pages to crawl
RAW_LINK_BUFFER = 1200          # stop paging once we have this many unique job links
OUTPUT_FILE = Path(__file__).resolve().parent / "undp_jobs.xml"
FEED_URL = "https://cinfoposte.github.io/undp-jobs/undp_jobs.xml"

REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_WAITS = [1, 3, 7]        # seconds between retries

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Oracle Cloud URL fragments that identify job links
ORACLE_FRAGMENTS = [
    "estm.fa.em2.oraclecloud.com",
    "oraclecloud.com",
    "/hcmUI/",
    "CandidateExperience",
]

# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------
session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
})


def fetch(url: str) -> requests.Response | None:
    """GET with retries and backoff."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                print(f"  [WARN] HTTP {resp.status_code} for {url}")
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            wait = RETRY_WAITS[attempt] if attempt < len(RETRY_WAITS) else 7
            print(f"  [RETRY {attempt+1}/{MAX_RETRIES}] {url} — {exc} — waiting {wait}s")
            time.sleep(wait)
    print(f"  [ERROR] All {MAX_RETRIES} attempts failed for {url}")
    return None


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def normalize_text(text: str) -> str:
    """Uppercase, normalize dashes, collapse whitespace."""
    t = unicodedata.normalize("NFKC", text).upper()
    t = re.sub(r"[\u2010-\u2015\u2212\uFE58\uFE63\uFF0D]", "-", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def strip_xml_illegal(text: str) -> str:
    """Remove characters illegal in XML 1.0."""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f]", "", text)


# ---------------------------------------------------------------------------
# STEP 3 — STRICT D/P detection
# ---------------------------------------------------------------------------

_RE_D = re.compile(r"\bD\s*[-]?\s*(1|2)\b")
_RE_P = re.compile(r"\bP\s*[-]?\s*([1-6])\b")

# Exclusion patterns (belt-and-suspenders)
_EXCLUDE_STRINGS = ["IPSA", "NPSA"]
_EXCLUDE_KEYWORDS = ["INTERN", "INTERNSHIP", "FELLOW", "FELLOWSHIP",
                     "CONSULTANT", "CONSULTANCY"]
_RE_NO = re.compile(r"\bNO\s*[-]?\s*[A-D]\b")
_RE_G = re.compile(r"\bG\s*[-]?\s*[1-7]\b")
_EXCLUDE_PREFIXES = ["SB-", "LSC-"]


def detect_level(text: str) -> str:
    """
    Return one of D1, D2, P1..P6 if found in text, else "".
    Prefers D over P if both appear.
    """
    norm = normalize_text(text)
    m = _RE_D.search(norm)
    if m:
        return f"D{m.group(1)}"
    m = _RE_P.search(norm)
    if m:
        return f"P{m.group(1)}"
    return ""


def is_excluded(norm_text: str) -> bool:
    """Return True if text trips the exclusion belt."""
    for s in _EXCLUDE_STRINGS:
        if s in norm_text:
            return True
    for kw in _EXCLUDE_KEYWORDS:
        if kw in norm_text:
            return True
    if _RE_NO.search(norm_text):
        return True
    if _RE_G.search(norm_text):
        return True
    for pref in _EXCLUDE_PREFIXES:
        if pref in norm_text:
            return True
    return False


def should_include(text: str) -> tuple[bool, str]:
    """
    Return (include, level) where level is e.g. "P3" or "D1".
    Include ONLY if a D/P level is detected AND no exclusion trips.
    """
    norm = normalize_text(text)
    level = detect_level(text)
    if not level:
        return False, ""
    if is_excluded(norm):
        return False, ""
    return True, level


# ---------------------------------------------------------------------------
# STEP 1 — Discover and crawl listing pages (queue crawl)
# ---------------------------------------------------------------------------

def is_listing_page_url(href: str) -> bool:
    """Check if href looks like a UNDP listing page."""
    return "cj_view_jobs.cfm" in href.lower()


def extract_job_links_and_pagination(html: str, page_url: str):
    """
    From a listing page, extract:
      - oracle cloud job links with text blobs
      - pagination links (other listing pages)
    """
    soup = BeautifulSoup(html, "lxml")
    all_anchors = soup.find_all("a", href=True)
    print(f"  Page {page_url}: {len(all_anchors)} <a> tags found")

    # --- Job links (Oracle Cloud) ---
    job_links: dict[str, str] = {}  # url -> text_blob
    for a in all_anchors:
        href = a["href"]
        if not any(frag in href for frag in ORACLE_FRAGMENTS):
            continue
        job_url = urljoin(page_url, href)

        # Best available text
        anchor_text = a.get_text(" ", strip=True)

        # Try parent elements for richer text
        parent_text = ""
        for parent_tag in ["tr", "li", "div"]:
            parent = a.find_parent(parent_tag)
            if parent:
                candidate = parent.get_text(" ", strip=True)
                if candidate and len(candidate) > len(parent_text):
                    parent_text = candidate[:800]
                    break

        # Choose longest non-empty text
        text_blob = anchor_text
        if parent_text and len(parent_text) > len(text_blob):
            text_blob = parent_text

        if job_url not in job_links:
            job_links[job_url] = text_blob

    # --- Pagination links ---
    pagination_links: list[str] = []
    for a in all_anchors:
        href = a["href"]
        if is_listing_page_url(href):
            abs_url = urljoin(page_url, href)
            # Skip if it's the same as current page
            if urlparse(abs_url).geturl() != urlparse(page_url).geturl():
                pagination_links.append(abs_url)

    print(f"  OracleCloud job links on this page: {len(job_links)}")
    print(f"  Pagination links discovered: {len(pagination_links)}")

    return job_links, pagination_links


def crawl_listing_pages() -> dict[str, str]:
    """
    Queue-based crawl of listing pages.
    Returns dict of job_url -> text_blob (de-duped).
    """
    queue: list[str] = [LISTING_URL]
    visited: set[str] = set()
    all_job_links: dict[str, str] = {}
    pages_fetched = 0
    first_page = True

    print(f"=== Starting crawl from {LISTING_URL}")
    print(f"    HARD_PAGE_CAP={HARD_PAGE_CAP}, RAW_LINK_BUFFER={RAW_LINK_BUFFER}")

    while queue and pages_fetched < HARD_PAGE_CAP and len(all_job_links) < RAW_LINK_BUFFER:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        resp = fetch(url)
        if not resp:
            continue
        pages_fetched += 1
        print(f"\n--- Listing page {pages_fetched}: status={resp.status_code} url={url}")

        job_links, pagination_links = extract_job_links_and_pagination(resp.text, url)

        # Debug: first page only — print sample links
        if first_page:
            first_page = False
            print(f"\n  [DEBUG] Total oraclecloud links on first page: {len(job_links)}")
            for i, (jurl, jtxt) in enumerate(list(job_links.items())[:10]):
                print(f"    [{i+1}] {jurl}")
                print(f"        text: {jtxt[:120]}...")

        # Merge job links
        for jurl, jtxt in job_links.items():
            if jurl not in all_job_links:
                all_job_links[jurl] = jtxt

        # Add pagination links to queue
        for purl in pagination_links:
            if purl not in visited:
                queue.append(purl)

        print(f"  Global: pages_fetched={pages_fetched}, queue_len={len(queue)}, "
              f"visited={len(visited)}, raw_unique_job_links={len(all_job_links)}")

    print(f"\n=== Crawl finished: {pages_fetched} pages, {len(all_job_links)} unique job links")
    return all_job_links


# ---------------------------------------------------------------------------
# STEP 4 — Parse optional fields (best-effort)
# ---------------------------------------------------------------------------

_LABELS = ["Job Title", "Post level", "Apply by", "Agency", "Location"]


def parse_fields(text_blob: str) -> dict:
    """
    Try to parse title, apply_by, agency, location from text_blob
    using label slicing between known labels.
    """
    fields = {"title": "", "apply_by": "", "agency": "", "location": ""}

    # Try label-based slicing
    upper = text_blob.upper()
    positions: list[tuple[int, str]] = []
    for label in _LABELS:
        idx = upper.find(label.upper())
        if idx >= 0:
            positions.append((idx, label))
    positions.sort()

    if positions:
        for i, (pos, label) in enumerate(positions):
            start = pos + len(label)
            # skip any separator chars
            while start < len(text_blob) and text_blob[start] in " :\t":
                start += 1
            end = positions[i + 1][0] if i + 1 < len(positions) else len(text_blob)
            value = text_blob[start:end].strip()

            lbl = label.lower()
            if "title" in lbl:
                fields["title"] = value
            elif "apply" in lbl:
                fields["apply_by"] = value
            elif "agency" in lbl:
                fields["agency"] = value
            elif "location" in lbl:
                fields["location"] = value

    # Fallback for title: use first reasonable segment of text
    if not fields["title"]:
        # Try first line or first sentence
        for sep in ["\n", "  ", ". "]:
            idx = text_blob.find(sep)
            if 5 < idx < 200:
                fields["title"] = text_blob[:idx].strip()
                break
        if not fields["title"]:
            fields["title"] = text_blob[:150].strip()

    return fields


# ---------------------------------------------------------------------------
# STEP 5 — RSS 2.0 generation
# ---------------------------------------------------------------------------

def generate_guid(job_url: str) -> str:
    """Stable 16-digit numeric GUID derived from md5 of the URL."""
    md5hex = hashlib.md5(job_url.encode()).hexdigest()
    n = int(md5hex, 16) % (10 ** 16)
    return f"{n:016d}"


def xml_escape(text: str) -> str:
    """Escape text for XML content."""
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    text = text.replace("'", "&apos;")
    return text


def build_rss(items: list[dict]) -> str:
    """
    Build a valid RSS 2.0 XML string.
    Each item dict has: job_url, level, title, apply_by, agency, location
    """
    now_rfc2822 = format_datetime(datetime.now(timezone.utc))

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        '  <channel>',
        '    <title>UNDP Jobs (filtered: D/P only)</title>',
        f'    <link>{LISTING_URL}</link>',
        '    <description>Only D1\u2013D2 and P1\u2013P6 jobs. Everything else excluded.</description>',
        '    <language>en</language>',
        f'    <lastBuildDate>{now_rfc2822}</lastBuildDate>',
        f'    <atom:link href="{FEED_URL}" rel="self" type="application/rss+xml" />',
    ]

    for item in items[:MAX_ITEMS]:
        job_url = item["job_url"]
        level = item["level"]
        title = item.get("title", "").strip()
        location = item.get("location", "").strip()
        apply_by = item.get("apply_by", "").strip()
        agency = item.get("agency", "").strip()

        # Build display title: title [LEVEL] — location
        parts = []
        if title:
            parts.append(title)
        parts.append(f"[{level}]")
        if location:
            parts.append(f"\u2014 {location}")
        display_title = " ".join(parts)

        # Build description as HTML-escaped lines
        desc_lines = []
        desc_lines.append(f"Level: {level}")
        if apply_by:
            desc_lines.append(f"Apply by: {apply_by}")
        if agency:
            desc_lines.append(f"Agency: {agency}")
        if location:
            desc_lines.append(f"Location: {location}")
        desc_lines.append(f"Link: {job_url}")
        description = "<br/>".join(desc_lines)

        guid = generate_guid(job_url)

        lines.append('    <item>')
        lines.append(f'      <title>{xml_escape(strip_xml_illegal(display_title))}</title>')
        lines.append(f'      <link>{xml_escape(job_url)}</link>')
        lines.append(f'      <guid isPermaLink="false">{guid}</guid>')
        lines.append(f'      <pubDate>{now_rfc2822}</pubDate>')
        lines.append(f'      <description>{xml_escape(strip_xml_illegal(description))}</description>')
        lines.append('    </item>')

    lines.append('  </channel>')
    lines.append('</rss>')
    lines.append('')

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# STEP 6 — Output + exit code semantics
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 70)
    print("UNDP Jobs Scraper — D/P Professional Levels Only")
    print("=" * 70)

    # Step 1+2: Crawl listing pages and collect Oracle Cloud job links
    raw_jobs = crawl_listing_pages()

    # Step 3: Filter for D/P levels
    included: list[dict] = []
    excluded_count = 0
    for job_url, text_blob in raw_jobs.items():
        inc, level = should_include(text_blob)
        if inc:
            fields = parse_fields(text_blob)
            included.append({
                "job_url": job_url,
                "level": level,
                "title": fields["title"],
                "apply_by": fields["apply_by"],
                "agency": fields["agency"],
                "location": fields["location"],
            })
        else:
            excluded_count += 1

    # Step 5: Generate RSS
    rss_xml = build_rss(included)

    # Validate XML well-formedness
    try:
        ET.fromstring(rss_xml)
        print("\n[OK] RSS XML is well-formed.")
    except ET.ParseError as exc:
        print(f"\n[ERROR] Generated XML is NOT well-formed: {exc}")
        # Still write it so we can debug
        print(rss_xml[:2000])

    # Write RSS file
    OUTPUT_FILE.write_text(rss_xml, encoding="utf-8")
    print(f"\n[OK] Wrote RSS to {OUTPUT_FILE}")

    # Step 6: Summary
    print(f"\n{'=' * 70}")
    print(f"SUMMARY")
    print(f"  Raw unique job links collected: {len(raw_jobs)}")
    print(f"  Included D/P items: {len(included)}")
    print(f"  Excluded items: {excluded_count}")
    print(f"{'=' * 70}")

    if included:
        print(f"\nFirst {min(5, len(included))} included items:")
        for i, item in enumerate(included[:5]):
            print(f"  [{i+1}] {item['level']} | {item['title'][:80]} | {item['job_url']}")

    if len(included) == 0:
        print("\n[WARN] Zero D/P items found.")
        if FAIL_IF_ZERO_ITEMS:
            print("[INFO] RSS file was written (valid skeleton). Exiting with code 2.")
            sys.exit(2)

    print("\nDone.")
    sys.exit(0)


if __name__ == "__main__":
    main()
