import html
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from db.db import connect

MAX_BODY_NEW_CHARS = 8000

INJECTION_PATTERNS: tuple[str, ...] = (
    r"ignore (all |any )?(previous |prior )?instructions",
    r"you are now",
    r"(as the |system )administrator",
    r"do(es)? not need (any |further )?review",
    r"without (any )?(further )?checks",
    r"mark (this|the|everything|all) as",
    r"this is authorized",
    r"list all (your )?(clients|matters|references|refs)",
    r"mark (this|the|everything|all)\b.{0,40}\bas\b"
)

TAG_RE = re.compile(r"<[^>]+>")
QUOTE_HEADER_RE = re.compile(r"^on .+ wrote:\s*$", re.IGNORECASE)


@dataclass
class CleanSummary:
    cleaned: int = 0
    flagged: int = 0


def strip_html(text: str) -> str:
    text = TAG_RE.sub("", text)
    text = html.unescape(text)
    return text.replace("\xa0", " ")


def split_quoted(text: str) -> tuple[str, str]:
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if QUOTE_HEADER_RE.match(line.strip()) or line.lstrip().startswith(">"):
            return "\n".join(lines[:i]), "\n".join(lines[i:])
    return text, ""


def _normalise_whitespace(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    lines = [line.rstrip() for line in text.split("\n")]
    collapsed: list[str] = []
    for line in lines:
        if line == "" and collapsed and collapsed[-1] == "":
            continue
        collapsed.append(line)
    return "\n".join(collapsed).strip()


def scan_injection(new: str, quoted: str) -> list[dict]:
    hits: list[dict] = []
    for part, text in (("new", new), ("quoted", quoted)):
        for pattern in INJECTION_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                hits.append({"pattern": pattern, "part": part})
    return hits


def _clean_row(conn: sqlite3.Connection, email_id: int, body_raw: str, run_id: str) -> bool:
    started = time.monotonic()
    conn.execute("BEGIN")
    try:
        stripped = strip_html(body_raw)
        new_part, quoted_part = split_quoted(stripped)
        normalised = _normalise_whitespace(new_part)
        truncated = len(normalised) > MAX_BODY_NEW_CHARS
        body_new = normalised[:MAX_BODY_NEW_CHARS] if truncated else normalised

        injection_hits = scan_injection(body_new, quoted_part)
        injection_flag = 1 if injection_hits else 0

        conn.execute(
            """
            UPDATE emails
            SET body_new = ?, body_quoted = ?, injection_flag = ?, status = 'processing'
            WHERE id = ?
            """,
            (body_new, quoted_part, injection_flag, email_id),
        )

        latency_ms = int((time.monotonic() - started) * 1000)
        output_json = {
            "quoted_split": bool(quoted_part),
            "injection": injection_hits,
            "truncated": truncated,
        }
        conn.execute(
            """
            INSERT INTO traces (run_id, email_id, stage, latency_ms, output_json)
            VALUES (?, ?, 'clean', ?, ?)
            """,
            (run_id, email_id, latency_ms, json.dumps(output_json)),
        )
        conn.commit()
        return bool(injection_flag)
    except Exception:
        conn.rollback()
        raise


def clean_pending(conn: sqlite3.Connection, run_id: str) -> CleanSummary:
    summary = CleanSummary()
    rows = conn.execute("SELECT id, body_raw FROM emails WHERE status = 'received'").fetchall()
    for row in rows:
        flagged = _clean_row(conn, row["id"], row["body_raw"], run_id)
        summary.cleaned += 1
        if flagged:
            summary.flagged += 1
    return summary


def _make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    run_id = _make_run_id()
    conn = connect()
    try:
        summary = clean_pending(conn, run_id)
    finally:
        conn.close()
    print(f"run_id={run_id} cleaned={summary.cleaned} flagged={summary.flagged}")
