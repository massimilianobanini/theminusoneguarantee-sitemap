#!/usr/bin/env python3
# Append newly published Substack posts to the permanent GitHub archive.
# Primary source: https://theminusoneguarantee.substack.com/feed
# Safety rule: existing historical URLs are never deleted automatically.

from __future__ import annotations

import csv
import html
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://theminusoneguarantee.substack.com"
FEED = BASE + "/feed"
UA = (
    "Mozilla/5.0 (compatible; TheMinusOneGuaranteeArchiveBot/1.0; "
    "+https://github.com/massimilianobanini/theminusoneguarantee-sitemap)"
)


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def canonical_url(url: str) -> str:
    url = clean(url)
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit(("https", parsed.netloc.lower(), path, "", ""))


def fetch_text(url: str, *, no_cache: bool = False) -> str:
    headers = {
        "User-Agent": UA,
        "Accept": (
            "application/rss+xml, application/xml, text/xml, "
            "text/html;q=0.9, */*;q=0.8"
        ),
    }
    if no_cache:
        headers["Cache-Control"] = "no-cache, no-store, max-age=0"
        headers["Pragma"] = "no-cache"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=40) as response:
        return response.read().decode("utf-8-sig", errors="replace")


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data)


def strip_html(fragment: str) -> str:
    parser = TextExtractor()
    try:
        parser.feed(fragment or "")
        return clean(html.unescape(" ".join(parser.parts)))
    except Exception:
        return clean(re.sub(r"<[^>]+>", " ", fragment or ""))


class MetaDescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, str] = {}

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() != "meta":
            return
        a = {clean(k).lower(): clean(v) for k, v in attrs if k and v}
        key = (a.get("property") or a.get("name") or "").lower()
        content = a.get("content", "")
        if key and content and key not in self.values:
            self.values[key] = content


def page_subtitle(url: str) -> str:
    try:
        page = fetch_text(url)
        parser = MetaDescriptionParser()
        parser.feed(page)
        for key in ("og:description", "twitter:description", "description"):
            value = clean(html.unescape(parser.values.get(key, "")))
            if value:
                return value
    except Exception as exc:
        print(f"WARN subtitle metadata unavailable for {url}: {exc}")
    return ""


def parse_pubdate(value: str) -> str:
    dt = parsedate_to_datetime(clean(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (
        dt.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def child_text(item: ET.Element, local_name: str) -> str:
    for child in list(item):
        if child.tag.rsplit("}", 1)[-1] == local_name:
            return clean(child.text)
    return ""


def parse_feed(xml_text: str) -> list[dict[str, str]]:
    root = ET.fromstring(xml_text)
    records: list[dict[str, str]] = []

    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] != "item":
            continue

        title = child_text(item, "title")
        url = canonical_url(child_text(item, "link"))
        pub = child_text(item, "pubDate")
        description = child_text(item, "description")

        if not title or not url or not pub:
            continue
        if not url.startswith(BASE + "/p/"):
            continue

        dt = parse_pubdate(pub)
        records.append(
            {
                "date": dt[:10],
                "datetime_utc": dt,
                "title": title,
                "subtitle": strip_html(description),
                "url": url,
            }
        )

    if not records:
        raise RuntimeError("RSS feed returned no usable article items")

    by_url = {r["url"]: r for r in records}
    return list(by_url.values())


def load_existing() -> list[dict[str, str]]:
    rows = json.loads(
        (ROOT / "articles.json").read_text(encoding="utf-8-sig")
    )
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("articles.json is empty or invalid")
    return rows


def validate(
    rows: list[dict[str, str]], historical_urls: set[str]
) -> None:
    urls = [canonical_url(r.get("url", "")) for r in rows]

    if len(urls) != len(set(urls)):
        raise RuntimeError("Duplicate canonical URLs detected")

    if not historical_urls.issubset(set(urls)):
        missing = sorted(historical_urls - set(urls))
        raise RuntimeError(
            f"Historical URL loss detected: {missing[:5]}"
        )

    for row in rows:
        if not row.get("title"):
            raise RuntimeError(f"Missing title: {row}")

        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", row.get("date", "")
        ):
            raise RuntimeError(f"Invalid date: {row}")

        if not canonical_url(row.get("url", "")).startswith(
            BASE + "/p/"
        ):
            raise RuntimeError(f"Unexpected URL: {row}")


def markdown_cell(value: str) -> str:
    return clean(value).replace("|", r"\|")


def write_outputs(
    rows: list[dict[str, str]], new_count: int
) -> None:
    public_rows = [
        {
            "date": r["date"],
            "datetime_utc": r["datetime_utc"],
            "title": r["title"],
            "subtitle": r.get("subtitle", ""),
            "url": r["url"],
        }
        for r in rows
    ]

    (ROOT / "articles.json").write_text(
        json.dumps(public_rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with (ROOT / "articles.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "date",
                "datetime_utc",
                "title",
                "subtitle",
                "url",
            ],
        )
        writer.writeheader()
        writer.writerows(public_rows)

    (ROOT / "sitemap.txt").write_text(
        "\n".join(r["url"] for r in public_rows) + "\n",
        encoding="utf-8",
    )

    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for r in public_rows:
        xml_lines.extend(
            [
                "  <url>",
                f"    <loc>{html.escape(r['url'])}</loc>",
                f"    <lastmod>{r['date']}</lastmod>",
                "  </url>",
            ]
        )
    xml_lines.append("</urlset>")
    (ROOT / "sitemap.xml").write_text(
        "\n".join(xml_lines) + "\n", encoding="utf-8"
    )

    newest_first = list(reversed(public_rows))

    md_lines = [
        "# The -1 Guarantee — Full Article Archive",
        "",
        (
            "Archive of all public articles. "
            "Canonical articles are hosted on Substack."
        ),
        "",
        "| Publication date | Title | Subtitle | URL |",
        "|---|---|---|---|",
    ]
    md_lines.extend(
        (
            f"| {r['date']} | {markdown_cell(r['title'])} | "
            f"{markdown_cell(r['subtitle'])} | {r['url']} |"
        )
        for r in newest_first
    )
    (ROOT / "archive.md").write_text(
        "\n".join(md_lines) + "\n", encoding="utf-8"
    )

    table_rows: list[str] = []
    for r in newest_first:
        url = html.escape(r["url"], quote=True)
        title = html.escape(r["title"])
        subtitle = html.escape(r.get("subtitle", ""))
        table_rows.append(
            "\n".join(
                [
                    "<tr>",
                    f'<td class="date">{r["date"]}</td>',
                    "<td>",
                    f'<a href="{url}"><strong>{title}</strong></a>',
                    f'<div class="subtitle">{subtitle}</div>',
                    "</td>",
                    f'<td><a href="{url}">{url}</a></td>',
                    "</tr>",
                ]
            )
        )

    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="index,follow">
  <title>The -1 Guarantee — Full article archive</title>
  <meta name="description" content="Public sitemap and archive for The -1 Guarantee Substack publication.">
  <link rel="sitemap" type="application/xml" href="https://massimilianobanini.github.io/theminusoneguarantee-sitemap/sitemap.xml">
  <style>:root{{color-scheme:light dark}}body{{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.55;margin:0;background:#eff6ff;color:#111827}}.wrap{{max-width:1120px;margin:0 auto;padding:40px 20px}}header{{margin-bottom:28px}}.kicker{{text-transform:uppercase;letter-spacing:.08em;font-size:12px;color:#1d4ed8;font-weight:700}}h1{{font-size:40px;line-height:1.1;margin:8px 0 12px}}.summary{{font-size:18px;color:#374151;max-width:820px}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin:24px 0}}.card{{background:#fff;border:1px solid #dbeafe;border-radius:14px;padding:16px}}.card strong{{display:block;font-size:24px}}a{{color:#1d4ed8}}table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dbeafe;border-radius:14px;overflow:hidden}}th,td{{padding:12px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top}}th{{background:#dbeafe;font-size:13px;text-transform:uppercase;letter-spacing:.04em}}td.date{{white-space:nowrap;font-weight:700;color:#1f2937}}.subtitle{{color:#4b5563;font-size:14px}}.footer{{margin-top:28px;font-size:14px;color:#4b5563}}@media (prefers-color-scheme:dark){{body{{background:#0b1120;color:#e5e7eb}}.summary,.subtitle,.footer,td.date{{color:#cbd5e1}}.card,table{{background:#111827;border-color:#1f2937}}th{{background:#1f2937}}th,td{{border-bottom-color:#243244}}a{{color:#93c5fd}}}}</style>
</head>
<body>
<div class="wrap">
<header>
  <div class="kicker">The -1 Guarantee</div>
  <h1>The -1 Guarantee — Full article archive</h1>
  <p class="summary">Chronological public archive of The -1 Guarantee. Every article title, subtitle, publication date and canonical Substack URL is listed below.</p>
  <div class="cards">
    <div class="card"><strong>{len(public_rows)}</strong>Articles indexed</div>
    <div class="card"><strong><a href="sitemap.xml">XML</a></strong>Sitemap with dates</div>
    <div class="card"><strong><a href="archive.html">Archive</a></strong>Readable table</div>
    <div class="card"><strong><a href="llms.txt">LLMS</a></strong>AI-oriented map</div>
  </div>
</header>
<h2>Full archive</h2>
<table>
<thead><tr><th>Date</th><th>Article</th><th>URL</th></tr></thead>
<tbody>
{chr(10).join(table_rows)}
</tbody>
</table>
<p class="footer">Canonical source: <a href="{BASE}/">The -1 Guarantee on Substack</a>.</p>
</div>
</body>
</html>
"""
    (ROOT / "archive.html").write_text(page, encoding="utf-8")
    (ROOT / "index.html").write_text(page, encoding="utf-8")

    latest = newest_first[:50]
    llms = [
        "# The -1 Guarantee",
        "",
        (
            '> The -1 Guarantee is a Substack publication by '
            'Massimiliano Banini about Negative Price Guarantees '
            '("-1"), measurable value, accountability, risk reversal, '
            "business systems, work, AI, and economic incentives."
        ),
        "",
        (
            "Canonical articles are hosted on Substack. "
            "This GitHub Pages project provides a static discovery "
            "layer because the Substack archive is dynamically loaded."
        ),
        "",
        "## Latest articles",
        "",
    ]
    for r in latest:
        suffix = f": {r['subtitle']}" if r.get("subtitle") else ""
        llms.append(
            f"- [{r['date']} — {r['title']}]({r['url']}){suffix}"
        )
    llms.extend(["", f"Total indexed articles: {len(public_rows)}", ""])
    (ROOT / "llms.txt").write_text(
        "\n".join(llms), encoding="utf-8"
    )

    first, last = public_rows[0], public_rows[-1]
    readme = f"""# The -1 Guarantee — public sitemap and archive

This repository exposes a public, crawlable archive for **The -1 Guarantee** Substack publication.

## Main files

- [`sitemap.xml`](https://massimilianobanini.github.io/theminusoneguarantee-sitemap/sitemap.xml) — XML sitemap with `loc` and `lastmod`.
- [`sitemap.txt`](https://massimilianobanini.github.io/theminusoneguarantee-sitemap/sitemap.txt) — plain text sitemap, one URL per line.
- [`archive.html`](https://massimilianobanini.github.io/theminusoneguarantee-sitemap/archive.html) — human- and bot-readable archive with publication date, title, subtitle, and URL.
- [`llms.txt`](https://massimilianobanini.github.io/theminusoneguarantee-sitemap/llms.txt) — LLM-oriented overview and recent links.
- [`articles.json`](https://massimilianobanini.github.io/theminusoneguarantee-sitemap/articles.json) — structured article list.
- [`articles.csv`](https://massimilianobanini.github.io/theminusoneguarantee-sitemap/articles.csv) — spreadsheet-friendly article list.

## Counts

- Articles: **{len(public_rows)}**
- First article: **{first['date']}**
- Latest article: **{last['date']}**
- Source publication: <{BASE}/>

## Automation

GitHub Actions checks the publication RSS feed every 6 hours. New canonical article URLs are appended to the archive; historical URLs are never deleted automatically.
"""
    (ROOT / "README.md").write_text(readme, encoding="utf-8")

    today = datetime.now(timezone.utc).date().isoformat()
    checks = f"""The -1 Guarantee — archive verification report
Generated: {today}

SOURCE
- Primary source: {FEED}
- New public articles added in this run: {new_count}

STRUCTURAL CHECKS
- Articles: {len(public_rows)}
- Unique URLs: {len(set(r['url'] for r in public_rows))}/{len(public_rows)}
- Duplicate URLs: 0
- URLs with expected Substack /p/ prefix: {sum(r['url'].startswith(BASE + '/p/') for r in public_rows)}/{len(public_rows)}
- Titles present: {sum(bool(r['title']) for r in public_rows)}/{len(public_rows)}
- Subtitles present: {sum(bool(r.get('subtitle')) for r in public_rows)}/{len(public_rows)}
- Date range: {first['date']} to {last['date']}

SAFETY
- Existing historical URLs are preserved automatically.
- New URLs are accepted only from the publication RSS feed.
"""
    (ROOT / "CHECKS.txt").write_text(checks, encoding="utf-8")


def get_best_feed(existing: list[dict[str, str]]) -> list[dict[str, str]]:
    candidates: list[list[dict[str, str]]] = []

    for url in (
        FEED,
        FEED + "?archive_check=" + str(int(time.time())),
    ):
        try:
            rows = parse_feed(fetch_text(url, no_cache=True))
            candidates.append(rows)
        except Exception as exc:
            print(f"WARN feed fetch failed for {url}: {exc}")

    if not candidates:
        raise RuntimeError("All RSS feed fetch attempts failed")

    def newest(rows: list[dict[str, str]]) -> str:
        return max(r["datetime_utc"] for r in rows)

    best = max(candidates, key=newest)
    newest_existing = max(
        r.get("datetime_utc", r["date"]) for r in existing
    )
    newest_feed = newest(best)

    print(
        f"Existing archive: {len(existing)} articles; "
        f"newest={newest_existing}"
    )
    print(
        f"RSS feed: {len(best)} usable items; "
        f"newest={newest_feed}"
    )

    if newest_feed < newest_existing:
        raise RuntimeError(
            "RSS feed appears older than the current repository archive. "
            "Stopping without modifying historical data."
        )

    return best


def main() -> None:
    existing = load_existing()
    historical_urls = {
        canonical_url(r.get("url", "")) for r in existing
    }

    feed_rows = get_best_feed(existing)
    feed_rows.sort(
        key=lambda r: (r["datetime_utc"], r["url"])
    )

    by_url: dict[str, dict[str, str]] = {
        canonical_url(r["url"]): dict(r) for r in existing
    }
    new_rows: list[dict[str, str]] = []

    for row in feed_rows:
        url = row["url"]
        if url in historical_urls:
            continue

        row["subtitle"] = (
            page_subtitle(url) or row.get("subtitle", "")
        )
        by_url[url] = row
        new_rows.append(row)

    if not new_rows:
        print(
            "No new canonical article URLs found. "
            "Repository left unchanged."
        )
        return

    merged = list(by_url.values())
    merged.sort(
        key=lambda r: (
            r.get("datetime_utc", r["date"]),
            canonical_url(r["url"]),
        )
    )

    validate(merged, historical_urls)
    write_outputs(merged, len(new_rows))

    print(
        f"Added {len(new_rows)} new article(s). "
        f"New total={len(merged)}"
    )
    for row in sorted(
        new_rows, key=lambda r: r["datetime_utc"]
    ):
        print(
            f"+ {row['date']} | {row['title']} | {row['url']}"
        )


if __name__ == "__main__":
    main()
