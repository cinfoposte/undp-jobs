#!/usr/bin/env python3
"""
UNDP Jobs Scraper — fetches job listings from jobs.undp.org,
filters for International Professional / Internship / Fellowship positions,
and produces an RSS 2.0 feed (undp_jobs.xml).
"""

import hashlib
import logging
import os
import re
import sys
import time
import unicodedata
import xml.dom.minidom as minidom
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = "https://jobs.undp.org/cj_view_jobs.cfm"
DETAIL_BASE = "https://jobs.undp.org/"
MAX_INCLUDED = 50
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_BACKOFF = 2          # seconds, doubles each retry
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "undp_jobs.xml")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP session (keeps cookies across requests — required for ColdFusion)
# ---------------------------------------------------------------------------
session = requests.Session()
session.headers.update(HEADERS)


def fetch(url: str) -> requests.Response | None:
    """GET with retries and exponential backoff, using the shared session."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            wait = RETRY_BACKOFF * (2 ** (attempt - 1))
            log.warning(
                "Attempt %d for %s failed (%s) – retrying in %ds",
                attempt, url, exc, wait,
            )
            time.sleep(wait)
    log.error("All %d attempts failed for %s", MAX_RETRIES, url)
    return None


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def strip_xml_illegal(text: str) -> str:
    """Remove characters illegal in XML 1.0."""
    return re.sub(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f]", "", text
    )


def normalize_text(text: str) -> str:
    """Uppercase, normalize dashes, collapse whitespace, expand compact grades."""
    t = unicodedata.normalize("NFKC", text).upper()
    # Unicode dashes → ASCII hyphen
    t = re.sub(r"[\u2010-\u2015\u2212\uFE58\uFE63\uFF0D]", "-", t)
    t = re.sub(r"\s+", " ", t).strip()
    # Expand compact grade forms: P4→P-4, D1→D-1, G6→G-6, SB3→SB-3, LSC10→LSC-10
    t = re.sub(r"\b(P)(\d)\b", r"\1-\2", t)
    t = re.sub(r"\b(D)(\d)\b", r"\1-\2", t)
    t = re.sub(r"\b(G)\s*(\d)\b", r"\1-\2", t)
    t = re.sub(r"\b(NO)\s*([A-D])\b", r"\1-\2", t)
    t = re.sub(r"\b(SB)\s*(\d)\b", r"\1-\2", t)
    t = re.sub(r"\b(LSC)\s*(\d{1,2})\b", r"\1-\2", t)
    return t


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

INCLUDED_GRADES = {f"P-{i}" for i in range(1, 6)} | {"D-1", "D-2"}
EXCLUDED_GRADE_PATTERNS = [
    re.compile(r"\bG-[1-7]\b"),
    re.compile(r"\bNO-[A-D]\b"),
    re.compile(r"\bSB-[1-4]\b"),
    re.compile(r"\bLSC-(?:1[0-1]|[1-9])\b"),
]
CONSULTANT_RE = re.compile(r"\bCONSULTAN(?:T|CY)\b")
INTERN_FELLOW_RE = re.compile(r"\b(?:INTERNSHIP|INTERN|FELLOWSHIP|FELLOW)\b")


def classify_job(title: str, grade: str, contract_type: str, description: str) -> bool:
    """Return True if the job should be INCLUDED according to the filter rules."""
    blob = " ".join(normalize_text(t) for t in [title, grade, contract_type, description])

    # 1) Consultant → EXCLUDE
    if CONSULTANT_RE.search(blob):
        return False
    # 2) Excluded grades → EXCLUDE
    for pat in EXCLUDED_GRADE_PATTERNS:
        if pat.search(blob):
            return False
    # 3) Included grades → INCLUDE
    for g in INCLUDED_GRADES:
        if g in blob:
            return True
    # 4) Internship / Fellowship → INCLUDE
    if INTERN_FELLOW_RE.search(blob):
        return True
    # 5) Else → EXCLUDE
    return False


# ---------------------------------------------------------------------------
# GUID (mandatory 16-digit numeric, zero-padded)
# ---------------------------------------------------------------------------

def generate_numeric_id(url: str) -> str:
    hex_dig = hashlib.md5(url.encode()).hexdigest()
    return str(int(hex_dig[:16], 16) % 10000000000000000).zfill(16)


# ---------------------------------------------------------------------------
# Scraping — listing pages
# ---------------------------------------------------------------------------

def parse_listing_page(html: str, page_url: str) -> tuple[list[dict], str | None]:
    """
    Parse a UNDP listing page.
    Returns (job_stubs, next_page_url).
    Each stub: {"title": ..., "detail_url": ...}
    """
    soup = BeautifulSoup(html, "lxml")
    stubs: list[dict] = []

    # UNDP ColdFusion site: links contain cj_view_job.cfm?cur_job_id=XXXXXX
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if "cj_view_job" in href.lower() and "cur_job_id" in href.lower():
            detail_url = urljoin(page_url, href)
            title_text = a_tag.get_text(strip=True)
            if title_text and len(title_text) >= 3:
                stubs.append({"title": title_text, "detail_url": detail_url})

    # Deduplicate by canonical URL (just the job ID matters)
    seen: set[str] = set()
    unique: list[dict] = []
    for s in stubs:
        parsed = urlparse(s["detail_url"])
        qs = parse_qs(parsed.query)
        job_id = qs.get("cur_job_id", qs.get("CUR_JOB_ID", [""]))[0]
        key = job_id if job_id else s["detail_url"]
        if key not in seen:
            seen.add(key)
            unique.append(s)
    stubs = unique

    # Pagination: look for "Next" link
    next_url = None
    for a_tag in soup.find_all("a", href=True):
        text = a_tag.get_text(strip=True).lower()
        href = a_tag["href"]
        if any(kw in text for kw in ["next", "»", ">>"]) or text == ">":
            next_url = urljoin(page_url, href)
            break

    # Fallback: look for start_row / startrow pagination links
    if not next_url:
        current_qs = parse_qs(urlparse(page_url).query)
        try:
            current_start = int(
                current_qs.get("start_row", current_qs.get("startrow", ["1"]))[0]
            )
        except (ValueError, IndexError):
            current_start = 1

        best_next = None
        best_start = float("inf")
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if "start_row" in href.lower() or "startrow" in href.lower():
                candidate = urljoin(page_url, href)
                qs = parse_qs(urlparse(candidate).query)
                try:
                    cand_start = int(
                        qs.get("start_row", qs.get("startrow", ["0"]))[0]
                    )
                except (ValueError, IndexError):
                    continue
                if current_start < cand_start < best_start:
                    best_start = cand_start
                    best_next = candidate
        next_url = best_next

    return stubs, next_url


# ---------------------------------------------------------------------------
# Scraping — detail page
# ---------------------------------------------------------------------------

def extract_field(soup: BeautifulSoup, *labels: str) -> str:
    """Find a labelled field (td/th/dt text contains label → value in next sibling)."""
    for label in labels:
        label_lower = label.lower()
        for cell in soup.find_all(["td", "th", "dt", "strong", "b", "span"]):
            cell_text = cell.get_text(strip=True).lower()
            if label_lower in cell_text:
                nxt = cell.find_next_sibling(["td", "dd"])
                if nxt:
                    return nxt.get_text(strip=True)
                parent = cell.find_parent("tr")
                if parent:
                    cells = parent.find_all("td")
                    if len(cells) >= 2:
                        return cells[-1].get_text(strip=True)
    return ""


def detect_grade_in_text(text: str) -> str:
    """Regex-detect a grade/level in free text."""
    norm = normalize_text(text[:3000])  # limit to avoid huge texts
    for g in sorted(INCLUDED_GRADES) + [
        "G-1", "G-2", "G-3", "G-4", "G-5", "G-6", "G-7",
        "NO-A", "NO-B", "NO-C", "NO-D",
        "SB-1", "SB-2", "SB-3", "SB-4",
        "LSC-1", "LSC-2", "LSC-3", "LSC-4", "LSC-5", "LSC-6",
        "LSC-7", "LSC-8", "LSC-9", "LSC-10", "LSC-11",
    ]:
        if g in norm:
            return g
    m = INTERN_FELLOW_RE.search(norm)
    if m:
        return m.group(0).title()
    return ""


def parse_detail_page(html: str) -> dict:
    """Extract structured fields from a UNDP job detail page."""
    soup = BeautifulSoup(html, "lxml")

    title = ""
    for tag in ["h1", "h2", "h3"]:
        el = soup.find(tag)
        if el:
            t = el.get_text(strip=True)
            if len(t) >= 5:
                title = t
                break

    location = extract_field(soup, "location", "duty station", "country")
    contract_type = extract_field(
        soup, "contract type", "contract", "type of contract",
        "appointment type", "vacancy type",
    )
    grade = extract_field(soup, "grade", "level", "band", "category")
    closing_date = extract_field(
        soup, "closing date", "deadline", "application deadline", "close date",
    )

    page_text = soup.get_text(" ", strip=True)

    if not grade:
        grade = detect_grade_in_text(page_text)

    # Description snippet
    desc_parts: list[str] = []
    for label in [
        "background", "description", "overview", "duties",
        "responsibilities", "scope of work", "organizational context",
    ]:
        for el in soup.find_all(["h2", "h3", "h4", "strong", "b"]):
            if label in el.get_text(strip=True).lower():
                text_after = ""
                for sib in el.find_next_siblings():
                    txt = sib.get_text(strip=True)
                    if txt:
                        text_after += " " + txt
                    if len(text_after) > 500:
                        break
                if text_after.strip():
                    desc_parts.append(text_after.strip()[:500])
                    break
        if desc_parts:
            break

    description = " ".join(desc_parts) if desc_parts else page_text[:500]

    return {
        "title": strip_xml_illegal(title),
        "location": strip_xml_illegal(location),
        "contract_type": strip_xml_illegal(contract_type),
        "grade": strip_xml_illegal(grade),
        "closing_date": strip_xml_illegal(closing_date),
        "description": strip_xml_illegal(description),
    }


# ---------------------------------------------------------------------------
# Main scraping loop
# ---------------------------------------------------------------------------

def scrape_jobs() -> list[dict]:
    """
    Crawl UNDP listing pages, fetch details, filter, return included jobs.
    """
    included: list[dict] = []
    visited_detail_urls: set[str] = set()
    page_url: str | None = BASE_URL
    pages_visited = 0
    total_listed = 0
    details_fetched = 0
    excluded_count = 0

    # Warm up session — visit home page to get cookies
    log.info("Establishing session with jobs.undp.org …")
    warmup = fetch("https://jobs.undp.org/")
    if warmup:
        log.info("Session established (status %d, cookies: %s)",
                 warmup.status_code, list(session.cookies.keys()))
    else:
        log.warning("Could not establish session with jobs.undp.org home page")

    while page_url and len(included) < MAX_INCLUDED:
        pages_visited += 1
        log.info("Fetching listing page %d: %s", pages_visited, page_url)

        resp = fetch(page_url)
        if not resp:
            log.error("Could not fetch listing page, stopping pagination.")
            break

        log.info("  Listing page status: %d, length: %d bytes",
                 resp.status_code, len(resp.text))

        stubs, next_url = parse_listing_page(resp.text, page_url)
        total_listed += len(stubs)
        log.info("  Found %d job links on page %d", len(stubs), pages_visited)

        if not stubs and pages_visited == 1:
            # Debug: log a snippet of the HTML so we can diagnose structure
            log.warning("  No job links found on first page! HTML snippet (first 2000 chars):")
            log.warning("  %s", resp.text[:2000])

        for stub in stubs:
            if len(included) >= MAX_INCLUDED:
                break

            detail_url = stub["detail_url"]
            if detail_url in visited_detail_urls:
                continue
            visited_detail_urls.add(detail_url)

            log.info("  Fetching detail: %s", detail_url)
            dresp = fetch(detail_url)
            if not dresp:
                continue
            details_fetched += 1

            detail = parse_detail_page(dresp.text)
            job_title = detail["title"] if len(detail["title"]) >= 5 else stub["title"]

            if classify_job(
                job_title, detail["grade"], detail["contract_type"], detail["description"],
            ):
                desc_text = f"UNDP has a vacancy for the position of {job_title}."
                if detail["location"]:
                    desc_text += f" Location: {detail['location']}."
                if detail["grade"]:
                    desc_text += f" Level: {detail['grade']}."
                if detail["closing_date"]:
                    desc_text += f" Closing date: {detail['closing_date']}."

                included.append({
                    "title": job_title,
                    "link": detail_url,
                    "description": desc_text,
                    "location": detail["location"],
                    "grade": detail["grade"],
                    "closing_date": detail["closing_date"],
                })
                log.info("    INCLUDED (%d/%d): %s", len(included), MAX_INCLUDED, job_title)
            else:
                excluded_count += 1
                log.debug("    EXCLUDED: %s (grade=%s, type=%s)",
                          job_title, detail["grade"], detail["contract_type"])

            time.sleep(0.5)

        page_url = next_url

    log.info(
        "Scraping complete: %d pages visited, %d jobs listed, %d details fetched, "
        "%d included, %d excluded",
        pages_visited, total_listed, details_fetched, len(included), excluded_count,
    )
    return included


# ---------------------------------------------------------------------------
# RSS generation
# ---------------------------------------------------------------------------

ATOM_NS = "http://www.w3.org/2005/Atom"
DC_NS = "http://purl.org/dc/elements/1.1/"
FEED_URL = "https://cinfoposte.github.io/undp-jobs/undp_jobs.xml"


def load_existing_items(path: str) -> tuple[list[ET.Element], set[str]]:
    """Load existing RSS items and their link set from an XML file."""
    items: list[ET.Element] = []
    links: set[str] = set()
    if not os.path.exists(path):
        return items, links
    try:
        tree = ET.parse(path)
        for item in tree.findall(".//item"):
            link_el = item.find("link")
            if link_el is not None and link_el.text:
                links.add(link_el.text.strip())
            items.append(item)
        log.info("Loaded %d existing items from %s", len(items), path)
    except ET.ParseError as exc:
        log.warning("Could not parse existing %s (%s), starting fresh.", path, exc)
    return items, links


def build_rss(new_jobs: list[dict], output_path: str) -> None:
    """Build RSS 2.0 XML and write to output_path, merging with existing items."""

    existing_items, existing_links = load_existing_items(output_path)
    now_rfc2822 = format_datetime(datetime.now(timezone.utc))

    # Build XML string manually for full control over namespaces and CDATA
    items_xml: list[str] = []

    # New items first
    new_count = 0
    for job in new_jobs:
        if job["link"] in existing_links:
            continue
        desc_escaped = job["description"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        title_escaped = job["title"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        items_xml.append(
            f"    <item>\n"
            f"      <title>{title_escaped}</title>\n"
            f"      <link>{job['link']}</link>\n"
            f"      <description><![CDATA[{job['description']}]]></description>\n"
            f"      <guid isPermaLink=\"false\">{generate_numeric_id(job['link'])}</guid>\n"
            f"      <pubDate>{now_rfc2822}</pubDate>\n"
            f"      <source url=\"{BASE_URL}\">UNDP Job Vacancies</source>\n"
            f"    </item>"
        )
        new_count += 1

    # Existing items
    for old_item in existing_items:
        # Re-serialize existing item elements
        raw = ET.tostring(old_item, encoding="unicode")
        items_xml.append(f"    {raw}")

    items_block = "\n".join(items_xml)

    xml_str = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rss version="2.0"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:atom="http://www.w3.org/2005/Atom">\n'
        '  <channel>\n'
        '    <title>UNDP Job Vacancies</title>\n'
        f'    <link>{BASE_URL}</link>\n'
        '    <description>List of vacancies at UNDP</description>\n'
        '    <language>en</language>\n'
        f'    <atom:link href="{FEED_URL}" rel="self" type="application/rss+xml"/>\n'
        f'    <pubDate>{now_rfc2822}</pubDate>\n'
        f'{items_block}\n'
        '  </channel>\n'
        '</rss>\n'
    )

    # Validate: try parsing with ElementTree to ensure well-formedness
    try:
        ET.fromstring(xml_str)
    except ET.ParseError as exc:
        log.error("Generated XML is not well-formed: %s", exc)
        log.error("XML (first 1000 chars): %s", xml_str[:1000])
        return

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(xml_str)

    log.info("Wrote RSS feed to %s (%d new + %d existing items)", output_path, new_count, len(existing_items))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Starting UNDP jobs scraper")
    try:
        jobs = scrape_jobs()
        build_rss(jobs, OUTPUT_FILE)
        log.info("Done. %d jobs included in this run.", len(jobs))
    except Exception:
        log.exception("Unhandled exception in scraper")
        # Still exit 0 so the workflow doesn't fail — the XML stays as-is
        sys.exit(0)


if __name__ == "__main__":
    main()
