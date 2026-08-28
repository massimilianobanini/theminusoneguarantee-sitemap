# The -1 Guarantee — public sitemap and archive

This repository exposes a public, crawlable archive for **The -1 Guarantee** Substack publication.

## Main files

- `sitemap.xml` — XML sitemap with `loc` and `lastmod`.
- `sitemap.txt` — plain text sitemap, one canonical URL per line.
- `archive.html` — human- and bot-readable archive.
- `llms.txt` — LLM-oriented recent index.
- `articles.json` — structured article list.
- `articles.csv` — spreadsheet-friendly article list.
- `article_notes.json` — short editorial notes/excerpts useful for downstream video selection.

## Counts

- Articles: **227**
- First article: **2026-01-14**
- Latest article: **2026-08-28**
- Source publication: <https://theminusoneguarantee.substack.com/>

## Automation

New posts are discovered from the Gmail copy of the publication newsletter and synchronized by Google Apps Script. The GitHub repository is append-only: existing historical canonical URLs are never deleted automatically.
