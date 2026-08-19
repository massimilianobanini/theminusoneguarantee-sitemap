#!/usr/bin/env python3
"""
Append newly published Substack posts to the permanent GitHub archive.

Discovery order:
1) RSS /feed
2) Substack archive JSON API /api/v1/archive
3) Substack posts JSON API /api/v1/posts
4) Jina Reader server-side browser fallback

Safety:
- Existing historical URLs are never deleted automatically.
- Only canonical URLs from this publication under /p/ are accepted.
- The script writes files only when at least one new canonical article is found.
"""

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
ARCHIVE_API = BASE + "/api/v1/archive"
POSTS_API = BASE + "/api/v1/posts"



JINA = "https://r.jina.ai/"


def jina_json(target_url: str):
    reader_url = JINA + target_url
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "X-No-Cache": "true",
        "X-Cache-Tolerance": "0",
        "X-Timeout": "40",
    }
    req = urllib.request.Request(reader_url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as response:
        raw = response.read().decode("utf-8-sig", errors="replace")
    payload = json.loads(raw)
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


def first_dict_value(obj, keys: tuple[str, ...]):
    if not isinstance(obj, dict):
        return ""
    lower = {str(k).lower(): v for k, v in obj.items()}
    for key in keys:
        value = lower.get(key.lower())
        if value not in (None, "", [], {}):
            return value
    for value in obj.values():
        if isinstance(value, dict):
            found = first_dict_value(value, keys)
            if found not in (None, "", [], {}):
                return found
    return ""


def jina_content(payload) -> str:
    value = first_dict_value(payload, ("content", "markdown", "text"))
    return str(value or "")


def normalize_title(value: str) -> str:
    t = clean(value)
    for suffix in (
        " | The -1 Guarantee",
        " — The -1 Guarantee",
        " - The -1 Guarantee",
    ):
        if t.endswith(suffix):
            t = t[: -len(suffix)].strip()
    return t


def date_from_text(text: str) -> str:
    candidates = []
    month = (
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
        r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
        r"Nov(?:ember)?|Dec(?:ember)?)"
    )
    candidates.extend(re.findall(month + r"\s+\d{1,2},\s+\d{4}", text, flags=re.I))
    candidates.extend(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text))
    for value in candidates:
        for fmt in (
            "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
                return dt.isoformat().replace("+00:00", "Z")
            except ValueError:
                pass
    return ""


def subtitle_from_content(content: str, title: str) -> str:
    lines = []
    for raw in content.splitlines():
        line = clean(re.sub(r"^[#>*\-]+\s*", "", raw))
        line = re.sub(r"!\[[^]]*\]\([^)]*\)", "", line).strip()
        if line:
            lines.append(line)

    title_norm = clean(title).lower()
    start = 0
    for i, line in enumerate(lines[:40]):
        if title_norm and title_norm in line.lower():
            start = i + 1
            break

    skip_re = re.compile(
        r"^(massimiliano banini|subscribe|share|comments?|like|restack|"
        r"paid|free|the -1 guarantee|\d+ min read)$", re.I
    )
    date_re = re.compile(
        r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* "
        r"\d{1,2}, \d{4}$", re.I
    )
    for line in lines[start:start + 15]:
        if skip_re.match(line) or date_re.match(line):
            continue
        if line.startswith("http") or line.startswith("["):
            continue
        if len(line) >= 20 and clean(line).lower() != title_norm:
            return line[:500]
    return ""


def jina_article_record(url: str, title_hint: str = "") -> dict[str, str] | None:
    payload = jina_json(url)
    content = jina_content(payload)

    title = normalize_title(
        str(first_dict_value(payload, ("title", "pageTitle", "name")) or title_hint)
    )
    if not title and content:
        m = re.search(r"^#\s+(.+)$", content, flags=re.M)
        if m:
            title = normalize_title(m.group(1))

    subtitle = clean(
        first_dict_value(payload, ("description", "subtitle", "excerpt"))
    )
    if not subtitle or subtitle.lower() == title.lower():
        subtitle = subtitle_from_content(content, title)

    dt = ""
    for key in (
        "publishedTime", "published_time", "datePublished",
        "date_published", "publishedAt", "published_at", "post_date",
    ):
        value = first_dict_value(payload, (key,))
        dt = parse_iso_datetime(value)
        if dt:
            break
    if not dt:
        dt = date_from_text(content[:5000])

    if not title or not dt:
        raise RuntimeError(
            f"Jina could not extract required title/date for {url}"
        )

    return {
        "date": dt[:10],
        "datetime_utc": dt,
        "title": title,
        "subtitle": subtitle,
        "url": canonical_url(url),
    }


def jina_recent() -> list[dict[str, str]]:
    discovered: dict[str, str] = {}
    errors = []

    for target in (BASE + "/archive", BASE + "/"):
        try:
            payload = jina_json(target)
            content = jina_content(payload)
            if not content:
                raise RuntimeError("empty Reader content")

            # Markdown links first so we retain a useful title hint.
            link_re = re.compile(
                r"\[([^]\n]{5,300})\]\((https://theminusoneguarantee\.substack\.com/p/[^)\s?#]+)"
            )
            for title, url in link_re.findall(content):
                discovered.setdefault(canonical_url(url), clean(title))

            # Also accept raw canonical URLs.
            raw_re = re.compile(
                r"https://theminusoneguarantee\.substack\.com/p/[A-Za-z0-9_-]+"
            )
            for url in raw_re.findall(content):
                discovered.setdefault(canonical_url(url), "")

            print(f"OK Jina discovery {target}: {len(discovered)} canonical URLs seen")
        except Exception as exc:
            errors.append(f"{target}: {exc}")
            print(f"WARN Jina discovery failed for {target}: {exc}")

    if not discovered:
        raise RuntimeError("Jina discovery returned no Substack post URLs: " + " | ".join(errors))

    rows = []
    # Only enrich the recent visible set; archive cards are newest first.
    for url, title_hint in list(discovered.items())[:30]:
        try:
            row = jina_article_record(url, title_hint)
            if row:
                rows.append(row)
        except Exception as exc:
            print(f"WARN Jina article extraction failed for {url}: {exc}")

    if not rows:
        raise RuntimeError("Jina found URLs but could not extract any usable article records")

    return list({r["url"]: r for r in rows}.values())

# Use a normal browser UA. Some edge/CDN rules reject obvious bot UAs.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
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
    return urllib.parse.urlunsplit(
        ("https", parsed.netloc.lower(), path, "", "")
    )


def http_get(url: str, *, accept: str, no_cache: bool = True) -> bytes:
    headers = {
        "User-Agent": UA,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": BASE + "/",
        "Connection": "close",
    }
    if no_cache:
        headers["Cache-Control"] = "no-cache, no-store, max-age=0"
        headers["Pragma"] = "no-cache"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=40) as response:
        return response.read()


def fetch_text(url: str) -> str:
    return http_get(
        url,
        accept=(
            "application/rss+xml, application/xml, text/xml, "
            "text/html;q=0.9, */*;q=0.8"
        ),
    ).decode("utf-8-sig", errors="replace")


def fetch_json(url: str):
    raw = http_get(
        url,
        accept="application/json,text/plain,*/*",
    )
    return json.loads(raw.decode("utf-8-sig", errors="replace"))


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


def parse_iso_datetime(value: object) -> str:
    s = clean(value)
    if not s:
        return ""

    # RSS date
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (
            dt.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except Exception:
        pass

    # ISO date
    try:
        dt = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (
            dt.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except Exception:
        return ""


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

        dt = parse_iso_datetime(pub)
        if not dt:
            continue

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
        raise RuntimeError("RSS returned no usable article items")

    return list({r["url"]: r for r in records}.values())


def api_record(post: dict) -> dict[str, str] | None:
    if not isinstance(post, dict):
        return None

    # Skip drafts/non-published records when flags exist.
    if post.get("draft") is True or post.get("is_published") is False:
        return None

    post_type = clean(post.get("type")).lower()
    # Keep newsletters/articles. Empty type is tolerated for compatibility.
    if post_type and post_type not in {"newsletter", "post"}:
        return None

    title = clean(post.get("title"))
    subtitle = clean(
        post.get("subtitle")
        or post.get("description")
        or post.get("social_title")
    )

    url = ""
    for key in ("canonical_url", "canonicalUrl", "web_url", "url"):
        candidate = canonical_url(post.get(key, ""))
        if candidate.startswith(BASE + "/p/"):
            url = candidate
            break

    if not url:
        slug = clean(post.get("slug"))
        if slug:
            url = canonical_url(BASE + "/p/" + slug)

    dt = ""
    for key in (
        "post_date",
        "published_at",
        "publication_date",
        "publishedAt",
        "created_at",
    ):
        dt = parse_iso_datetime(post.get(key))
        if dt:
            break

    if not title or not url.startswith(BASE + "/p/") or not dt:
        return None

    return {
        "date": dt[:10],
        "datetime_utc": dt,
        "title": title,
        "subtitle": subtitle,
        "url": url,
    }


def rows_from_api_payload(payload) -> list[dict[str, str]]:
    if isinstance(payload, list):
        posts = payload
    elif isinstance(payload, dict):
        posts = []
        for key in ("posts", "items", "results", "data"):
            if isinstance(payload.get(key), list):
                posts = payload[key]
                break
    else:
        posts = []

    rows = []
    for post in posts:
        record = api_record(post)
        if record:
            rows.append(record)

    return list({r["url"]: r for r in rows}.values())


def fetch_api_source(base_url: str, source_name: str) -> list[dict[str, str]]:
    # We only need a recent window because GitHub is the permanent archive.
    # Fetch several small pages to survive missed workflow runs.
    collected: dict[str, dict[str, str]] = {}

    for offset in range(0, 60, 12):
        params = urllib.parse.urlencode(
            {
                "sort": "new",
                "limit": 12,
                "offset": offset,
                "type": "newsletter",
                "_": int(time.time()),
            }
        )
        url = base_url + "?" + params
        payload = fetch_json(url)
        rows = rows_from_api_payload(payload)

        for row in rows:
            collected[row["url"]] = row

        if len(rows) < 12:
            break

    if not collected:
        raise RuntimeError(f"{source_name} returned no usable article items")

    return list(collected.values())


def load_existing() -> list[dict[str, str]]:
    rows = json.loads(
        (ROOT / "articles.json").read_text(encoding="utf-8-sig")
    )
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("articles.json is empty or invalid")
    return rows


def source_newest(rows: list[dict[str, str]]) -> str:
    return max(r["datetime_utc"] for r in rows)


def discover_recent(existing: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    attempts = [
        (
            "RSS",
            lambda: parse_feed(
                fetch_text(FEED + "?archive_check=" + str(int(time.time())))
            ),
        ),
        (
            "archive API",
            lambda: fetch_api_source(ARCHIVE_API, "archive API"),
        ),
        (
            "posts API",
            lambda: fetch_api_source(POSTS_API, "posts API"),
        ),
        (
            "Jina Reader",
            jina_recent,
        ),
    ]

    errors: list[str] = []
    successful: list[tuple[str, list[dict[str, str]]]] = []

    for name, fn in attempts:
        try:
            rows = fn()
            print(
                f"OK {name}: {len(rows)} usable items; "
                f"newest={source_newest(rows)}"
            )
            successful.append((name, rows))
        except Exception as exc:
            msg = f"{name}: {exc}"
            errors.append(msg)
            print(f"WARN {msg}")

    if not successful:
        raise RuntimeError(
            "All Substack discovery methods failed: " + " | ".join(errors)
        )

    # Choose the source with the newest visible post.
    return max(successful, key=lambda pair: source_newest(pair[1]))


def validate(rows: list[dict[str, str]], historical_urls: set[str]) -> None:
    urls = [canonical_url(r.get("url", "")) for r in rows]

    if len(urls) != len(set(urls)):
        raise RuntimeError("Duplicate canonical URLs detected")

    if not historical_urls.issubset(set(urls)):
        missing = sorted(historical_urls - set(urls))
        raise RuntimeError(f"Historical URL loss detected: {missing[:5]}")

    for row in rows:
        if not row.get("title"):
            raise RuntimeError(f"Missing title: {row}")

        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row.get("date", "")):
            raise RuntimeError(f"Invalid date: {row}")

        if not canonical_url(row.get("url", "")).startswith(BASE + "/p/"):
            raise RuntimeError(f"Unexpected URL: {row}")


def markdown_cell(value: str) -> str:
    return clean(value).replace("|", r"\|")


def write_outputs(
    rows: list[dict[str, str]], new_count: int, source_name: str
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
        "Archive of all public articles. Canonical articles are hosted on Substack.",
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
        '> The -1 Guarantee is a Substack publication by Massimiliano Banini about Negative Price Guarantees ("-1"), measurable value, accountability, risk reversal, business systems, work, AI, and economic incentives.',
        "",
        "Canonical articles are hosted on Substack. This GitHub Pages project provides a static discovery layer because the Substack archive is dynamically loaded.",
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
- [`archive.html`](https://massimilianobanini.github.io/theminusoneguarantee-sitemap/archive.html) — human- and bot-readable archive.
- [`llms.txt`](https://massimilianobanini.github.io/theminusoneguarantee-sitemap/llms.txt) — LLM-oriented recent index.
- [`articles.json`](https://massimilianobanini.github.io/theminusoneguarantee-sitemap/articles.json) — structured article list.
- [`articles.csv`](https://massimilianobanini.github.io/theminusoneguarantee-sitemap/articles.csv) — spreadsheet-friendly article list.

## Counts

- Articles: **{len(public_rows)}**
- First article: **{first['date']}**
- Latest article: **{last['date']}**
- Source publication: <{BASE}/>

## Automation

GitHub Actions checks Substack public metadata every 6 hours. Discovery order: RSS, archive API, posts API, Jina Reader fallback. Existing historical URLs are never deleted automatically.
"""
    (ROOT / "README.md").write_text(readme, encoding="utf-8")

    today = datetime.now(timezone.utc).date().isoformat()
    checks = f"""The -1 Guarantee — GitHub files verification report
Generated: {today}

SOURCE
- Discovery source used: {source_name}
- New public articles identified in this run: {new_count}

FULL ARCHIVE STRUCTURAL CHECKS
- Articles: {len(public_rows)}
- Unique URLs: {len(set(r['url'] for r in public_rows))}/{len(public_rows)}
- Duplicate URLs: 0
- URLs with expected Substack /p/ prefix: {sum(r['url'].startswith(BASE + '/p/') for r in public_rows)}/{len(public_rows)}
- Titles present: {sum(bool(r['title']) for r in public_rows)}/{len(public_rows)}
- Subtitles present: {sum(bool(r.get('subtitle')) for r in public_rows)}/{len(public_rows)}
- Date range: {first['date']} to {last['date']}

SAFETY
- Existing historical URLs are preserved automatically.
- New URLs are accepted only from public Substack discovery sources.
"""
    (ROOT / "CHECKS.txt").write_text(checks, encoding="utf-8")


def main() -> None:
    existing = load_existing()
    historical_urls = {
        canonical_url(r.get("url", "")) for r in existing
    }

    source_name, recent_rows = discover_recent(existing)
    recent_rows.sort(key=lambda r: (r["datetime_utc"], r["url"]))

    newest_existing = max(
        r.get("datetime_utc", r["date"]) for r in existing
    )
    newest_source = source_newest(recent_rows)

    print(
        f"Existing archive: {len(existing)} articles; "
        f"newest={newest_existing}"
    )
    print(
        f"Selected source: {source_name}; "
        f"newest={newest_source}"
    )

    if newest_source < newest_existing:
        raise RuntimeError(
            "All successful Substack sources appear older than the "
            "current repository archive. Stopping safely."
        )

    by_url: dict[str, dict[str, str]] = {
        canonical_url(r["url"]): dict(r) for r in existing
    }
    new_rows: list[dict[str, str]] = []

    for row in recent_rows:
        url = row["url"]
        if url in historical_urls:
            continue
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
    write_outputs(merged, len(new_rows), source_name)

    print(
        f"Added {len(new_rows)} new article(s). "
        f"New total={len(merged)}"
    )
    for row in sorted(new_rows, key=lambda r: r["datetime_utc"]):
        print(
            f"+ {row['date']} | {row['title']} | {row['url']}"
        )


if __name__ == "__main__":
    main()
