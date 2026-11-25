
#!/usr/bin/env python3
"""MedRadar AI - Saudi Healthcare and Insurance Monitor."""

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import feedparser
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DB = ROOT / "medradar.db"
REPORT_DIR = ROOT / "reports"
REPORT_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "MedRadarAI/0.3 (Public information monitor)"
}

KEYWORDS = {
    "insurance": ["insurance", "تأمين", "الضمان الصحي"],
    "approvals": ["approval", "authorization", "موافقة", "موافقات"],
    "claims": ["claim", "billing", "مطالبات", "فواتير"],
    "regulation": ["regulation", "policy", "قرار", "لائحة", "تعميم"],
    "healthcare": ["healthcare", "hospital", "مستشفى", "صحي"],
    "medicines": ["medicine", "drug", "دواء", "أدوية"],
    "quality": ["quality", "accreditation", "جودة", "اعتماد"]
}

session = requests.Session()
session.headers.update(HEADERS)


def connect_db():
    conn = sqlite3.connect(DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            title TEXT,
            url TEXT UNIQUE,
            published TEXT,
            summary TEXT,
            fingerprint TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    return conn


def make_fingerprint(text):
    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


def classify(text):
    text = text.lower()
    found = []

    for topic, words in KEYWORDS.items():
        if any(word.lower() in text for word in words):
            found.append(topic)

    return found or ["general"]


def clean_text(html):
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup([
        "script", "style", "nav",
        "footer", "header", "noscript"
    ]):
        tag.decompose()

    return re.sub(
        r"\s+", " ",
        soup.get_text(" ", strip=True)
    )[:5000]


def save_item(conn, source, title, url, published, summary):
    now = datetime.now(timezone.utc).isoformat()

    fp = make_fingerprint(
        title + "|" + url + "|" + summary
    )

    old = conn.execute(
        "SELECT fingerprint FROM items WHERE url=?",
        (url,)
    ).fetchone()

    if old is None:
        conn.execute("""
            INSERT INTO items (
                source, title, url, published,
                summary, fingerprint, first_seen, last_seen
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            source, title, url, published,
            summary, fp, now, now
        ))

        return "NEW"

    if old[0] != fp:
        conn.execute("""
            UPDATE items
            SET title=?, published=?, summary=?,
                fingerprint=?, last_seen=?
            WHERE url=?
        """, (
            title, published, summary,
            fp, now, url
        ))

        return "UPDATED"

    conn.execute(
        "UPDATE items SET last_seen=? WHERE url=?",
        (now, url)
    )

    return "UNCHANGED"


def monitor_rss(conn, source):
    feed = feedparser.parse(source["url"])
    count = 0

    for entry in feed.entries[:50]:
        title = entry.get("title", "Untitled")
        url = entry.get("link", "")

        if not url.startswith("http"):
            continue

        summary = clean_text(
            entry.get("summary", "")
        )

        status = save_item(
            conn,
            source["name"],
            title,
            url,
            entry.get("published", ""),
            summary
        )

        if status != "UNCHANGED":
            count += 1

    return count


def is_article_link(url, source_url):
    parsed = urlparse(url)
    base = urlparse(source_url)

    if parsed.netloc != base.netloc:
        return False

    path = parsed.path.lower()

    ignored = [
        "/about", "/contact", "/login",
        "/search", "/services", "/privacy",
        "/terms", "/category", "/tag",
        "/page/", "/media"
    ]

    if any(item in path for item in ignored):
        return False

    return len(path.strip("/")) >= 8


def monitor_page(conn, source):
    response = session.get(
        source["url"],
        timeout=30
    )
    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    candidates = {}

    for link in soup.find_all("a", href=True):
        title = re.sub(
            r"\s+",
            " ",
            link.get_text(" ", strip=True)
        )

        url = urljoin(
            source["url"],
            link["href"]
        ).split("#")[0]

        if len(title) < 15:
            continue

        if not is_article_link(url, source["url"]):
            continue

        candidates[url] = title

    count = 0

    for url, title in list(candidates.items())[:40]:
        try:
            article = session.get(
                url,
                timeout=25
            )
            article.raise_for_status()

            body = clean_text(article.text)

            if len(body) < 100:
                continue

            status = save_item(
                conn,
                source["name"],
                title,
                url,
                "",
                body[:2500]
            )

            if status != "UNCHANGED":
                count += 1

        except requests.RequestException as error:
            print("Article error:", url, error)
            continue

    return count


def write_report(conn):
    rows = conn.execute("""
        SELECT source, title, url, published,
               summary, first_seen
        FROM items
        ORDER BY first_seen DESC
        LIMIT 100
    """).fetchall()

    now = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# MedRadar AI",
        "## Healthcare and Insurance Intelligence",
        "",
        f"Generated: {now}",
        f"Total monitored items: {len(rows)}",
        ""
    ]

    for source, title, url, published, summary, seen in rows:
        topics = classify(title + " " + summary)

        lines.extend([
            f"## {title}",
            f"- Source: {source}",
            f"- Topics: {', '.join(topics)}",
            f"- Published: {published or 'Not provided'}",
            f"- First detected: {seen}",
            f"- Link: {url}",
            "",
            summary[:500] if summary else "No summary available.",
            "",
            "---",
            ""
        ])

    lines.extend([
        "Disclaimer:",
        "This service monitors public information only.",
        "Verify regulatory information with official sources."
    ])

    report = REPORT_DIR / "latest.md"

    report.write_text(
        "\n".join(lines),
        encoding="utf-8"
    )


def main():
    config_file = ROOT / "sources.json"

    with config_file.open(
        encoding="utf-8"
    ) as file:
        sources = json.load(file)

    conn = connect_db()
    total = 0

    for source in sources:
        try:
            if source["type"] == "rss":
                found = monitor_rss(conn, source)

            elif source["type"] == "page":
                found = monitor_page(conn, source)

            else:
                print(
                    "Unknown source type:",
                    source["type"]
                )
                continue

            total += found

            print(
                source["name"],
                "=>",
                found,
                "new or updated"
            )

        except Exception as error:
            print(
                "SOURCE ERROR:",
                source["name"],
                str(error)
            )

    conn.commit()
    write_report(conn)
    conn.close()

    print("Monitoring completed.")
    print("New or updated items:", total)
    print("Report: reports/latest.md")


if __name__ == "__main__":
    main()
