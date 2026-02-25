# undp-jobs

A Python script that extracts current job opportunities from the [UNDP careers website](https://jobs.undp.org/cj_view_jobs.cfm), filters them for relevance to cinfoPoste, and generates an RSS feed.

## RSS Feed URL

**Live feed (via GitHub Pages):**
https://cinfoposte.github.io/undp-jobs/undp_jobs.xml

## What it does

- Scrapes job listings from `jobs.undp.org`
- Filters for **International Professional** (P-1 … P-5, D-1, D-2), **Internship**, and **Fellowship** positions
- Excludes Consultant/Consultancy, General Service (G-1 … G-7), National Officer (NO-A … NO-D), Service Contracts (SB-1 … SB-4), and Local Service Contracts (LSC-1 … LSC-11)
- Generates a valid RSS 2.0 feed (`undp_jobs.xml`)
- Accumulates jobs across runs (new jobs are prepended, existing ones kept)
- Runs automatically on **Thursday and Sunday at 06:00 UTC** via GitHub Actions

## Local run

```bash
pip install -r requirements.txt
python scraper.py
```

The feed is written to `undp_jobs.xml` in the repo root.

## GitHub Pages activation

1. Go to **Settings → Pages**
2. Under **Source**, select **Deploy from a branch**
3. Choose branch **main** and folder **/ (root)**
4. Click **Save**

The feed will be available at:
https://cinfoposte.github.io/undp-jobs/undp_jobs.xml

## cinfoPoste import mapping

| Portal-Feld | Dropdown-Auswahl |
|-------------|-----------------|
| TITLE       | → Title         |
| LINK        | → Link          |
| DESCRIPTION | → Description   |
| PUBDATE     | → Date          |
| ITEM        | → Start item    |
| GUID        | → Unique ID     |
