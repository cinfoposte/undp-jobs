# UNDP Jobs — D/P Professional RSS Feed

Automated scraper that extracts job listings from [jobs.undp.org](https://jobs.undp.org/cj_view_jobs.cfm), applies **strict filtering** for international professional levels, and publishes a valid RSS 2.0 feed.

## Live Feed URL

**https://cinfoposte.github.io/undp-jobs/undp_jobs.xml**

Add this URL to any RSS reader to receive updates.

## What It Does

1. **Crawls** listing pages at `jobs.undp.org` following pagination links (up to 25 pages).
2. **Extracts** Oracle Cloud job links and their surrounding text from each listing page.
3. **Filters strictly** — only D/P professional levels are included:
   - **Included:** D1, D2, P1, P2, P3, P4, P5, P6 (supports variants like `P-3`, `P3`, `P 3`)
   - **Excluded:** everything else — IPSA, NPSA, NO-A through NO-D, G-1 through G-7, SB, LSC, internships, fellowships, consultants, consultancies
4. **Generates** a valid RSS 2.0 feed (`undp_jobs.xml`) with up to **200 matching items** (`MAX_ITEMS=200`).
5. **Runs daily** at 06:15 UTC via GitHub Actions and commits the updated feed automatically.

## Changing MAX_ITEMS

Edit `scraper.py` and change the `MAX_ITEMS` constant at the top:

```python
MAX_ITEMS = 200   # change to any number you need
```

## "No Babysit" Behaviour

This scraper is designed to run unattended without manual intervention:

- **Zero items?** The feed is still written as valid RSS (empty `<channel>`) and committed. The workflow then exits with a non-zero code so the run shows as "failed" — but the schedule continues.
- **Workflow schedules won't auto-disable.** GitHub disables scheduled workflows after 60 days of repo inactivity. A separate **keepalive workflow** runs monthly, touching a `.keepalive` file to maintain activity.
- **Failed runs are expected** when there happen to be zero D/P-level jobs. The feed stays up to date regardless.

## Local Run

```bash
pip install -r requirements.txt
python scraper.py
```

The feed is written to `undp_jobs.xml` in the repo root.

## GitHub Pages Setup

1. Go to **Settings → Pages**
2. Under **Source**, select **Deploy from a branch**
3. Choose branch **main** and folder **/ (root)**
4. Click **Save**

The feed will be available at:
https://cinfoposte.github.io/undp-jobs/undp_jobs.xml

## Dependencies

- `requests` — HTTP client
- `beautifulsoup4` — HTML parsing
- `lxml` — fast HTML parser backend

## Workflows

| Workflow | Schedule | Purpose |
|----------|----------|---------|
| `scrape.yml` | Daily 06:15 UTC | Run scraper, validate XML, commit feed |
| `keepalive.yml` | Monthly 1st at 07:05 UTC | Prevent GitHub from disabling scheduled workflows |
