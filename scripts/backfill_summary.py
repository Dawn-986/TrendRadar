#!/usr/bin/env python
# coding: utf-8
"""Backfill output/summary.db from existing daily news and RSS databases."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable, Optional


SUMMARY_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    isactive INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT DEFAULT '',
    published_time TEXT NOT NULL,
    summary TEXT,
    FOREIGN KEY (source_id) REFERENCES sources(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_summary_news_source_url
ON news_items(source_id, url)
WHERE url != '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_summary_news_no_url
ON news_items(source_id, title, published_time)
WHERE url = '';
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill output/summary.db from output/news/*.db and output/rss/*.db.",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="TrendRadar output directory. Default: output",
    )
    parser.add_argument(
        "--summary-db",
        default=None,
        help="Summary DB path. Default: <output-dir>/summary.db",
    )
    parser.add_argument(
        "--only",
        choices=("all", "news", "rss"),
        default="all",
        help="Limit backfill source type. Default: all",
    )
    return parser.parse_args()


def init_summary_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SUMMARY_SCHEMA)
    conn.commit()
    return conn


def db_files(directory: Path) -> Iterable[Path]:
    if not directory.exists():
        return []
    return sorted(directory.glob("*.db"))


def date_from_db_path(path: Path) -> str:
    return path.stem


def format_summary_time(date: str, value: Optional[str]) -> str:
    """Normalize crawl/publish time to YYYY-MM-DD HH:MM:SS."""
    value = (value or "").strip()
    if not value:
        return f"{date} 00:00:00"
    if len(value) == 5 and value[2] == "-":
        return f"{date} {value.replace('-', ':')}:00"
    if len(value) == 5 and value[2] == ":":
        return f"{date} {value}:00"

    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass

    try:
        parsed = parsedate_to_datetime(value)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, IndexError, OverflowError):
        pass

    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M")
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def upsert_source(summary_conn: sqlite3.Connection, source_id: str, name: str) -> None:
    summary_conn.execute(
        """
        INSERT INTO sources (id, name, isactive)
        VALUES (?, ?, 1)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            isactive = 1
        """,
        (source_id, name or source_id),
    )


def upsert_item(
    summary_conn: sqlite3.Connection,
    title: str,
    source_id: str,
    url: str,
    published_time: str,
    summary: str,
) -> int:
    before = summary_conn.total_changes
    summary_conn.execute(
        """
        INSERT INTO news_items (title, source_id, url, published_time, summary)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_id, url) WHERE url != '' DO UPDATE SET
            title = excluded.title,
            published_time = excluded.published_time,
            summary = CASE
                WHEN excluded.summary != '' THEN excluded.summary
                ELSE news_items.summary
            END
        ON CONFLICT(source_id, title, published_time) WHERE url = '' DO UPDATE SET
            summary = CASE
                WHEN excluded.summary != '' THEN excluded.summary
                ELSE news_items.summary
            END
        """,
        (title, source_id, url or "", published_time, summary or ""),
    )
    return max(summary_conn.total_changes - before, 0)


def backfill_news_db(summary_conn: sqlite3.Connection, db_path: Path) -> tuple[int, int]:
    source_count = 0
    item_changes = 0
    date = date_from_db_path(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if not table_exists(conn, "news_items"):
            return source_count, item_changes

        query = """
            SELECT
                n.title,
                n.platform_id AS source_id,
                COALESCE(p.name, n.platform_id) AS source_name,
                COALESCE(n.url, '') AS url,
                COALESCE(n.first_crawl_time, n.last_crawl_time, '') AS published_time
            FROM news_items n
            LEFT JOIN platforms p ON n.platform_id = p.id
            ORDER BY n.id
        """
        for row in conn.execute(query):
            title = (row["title"] or "").strip()
            source_id = (row["source_id"] or "").strip()
            if not title or not source_id:
                continue
            upsert_source(summary_conn, source_id, row["source_name"] or source_id)
            source_count += 1
            item_changes += upsert_item(
                summary_conn,
                title=title,
                source_id=source_id,
                url=row["url"] or "",
                published_time=format_summary_time(date, row["published_time"]),
                summary="",
            )

    summary_conn.commit()
    return source_count, item_changes


def backfill_rss_db(summary_conn: sqlite3.Connection, db_path: Path) -> tuple[int, int]:
    source_count = 0
    item_changes = 0
    date = date_from_db_path(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if not table_exists(conn, "rss_items"):
            return source_count, item_changes

        query = """
            SELECT
                i.title,
                i.feed_id AS source_id,
                COALESCE(f.name, i.feed_id) AS source_name,
                COALESCE(i.url, '') AS url,
                COALESCE(i.published_at, i.first_crawl_time, '') AS published_time,
                COALESCE(i.summary, '') AS summary
            FROM rss_items i
            LEFT JOIN rss_feeds f ON i.feed_id = f.id
            ORDER BY i.id
        """
        for row in conn.execute(query):
            title = (row["title"] or "").strip()
            source_id = (row["source_id"] or "").strip()
            if not title or not source_id:
                continue
            upsert_source(summary_conn, source_id, row["source_name"] or source_id)
            source_count += 1
            published_time = format_summary_time(date, row["published_time"])
            item_changes += upsert_item(
                summary_conn,
                title=title,
                source_id=source_id,
                url=row["url"] or "",
                published_time=published_time,
                summary=row["summary"] or "",
            )

    summary_conn.commit()
    return source_count, item_changes


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    summary_path = Path(args.summary_db) if args.summary_db else output_dir / "summary.db"

    summary_conn = init_summary_db(summary_path)
    try:
        news_files = list(db_files(output_dir / "news")) if args.only in ("all", "news") else []
        rss_files = list(db_files(output_dir / "rss")) if args.only in ("all", "rss") else []

        total_news_changes = 0
        total_rss_changes = 0

        print(f"[summary] database: {summary_path}")

        for db_path in news_files:
            _, changes = backfill_news_db(summary_conn, db_path)
            total_news_changes += changes
            print(f"[summary] news {db_path.name}: changed {changes}")

        for db_path in rss_files:
            _, changes = backfill_rss_db(summary_conn, db_path)
            total_rss_changes += changes
            print(f"[summary] rss  {db_path.name}: changed {changes}")

        source_total = summary_conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        item_total = summary_conn.execute("SELECT COUNT(*) FROM news_items").fetchone()[0]
        print(
            "[summary] done: "
            f"sources={source_total}, items={item_total}, "
            f"news_changes={total_news_changes}, rss_changes={total_rss_changes}"
        )
        return 0
    finally:
        summary_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
