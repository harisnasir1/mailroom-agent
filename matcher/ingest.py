import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from db.db import connect

mailboxdir = Path("mailbox")
REQUIRED_KEYS = ("message_id", "sender", "subject", "body")


@dataclass
class IngestSummary:
    ingested: int = 0
    skipped: int = 0
    malformed: int = 0


def _parse_email(raw: bytes) -> dict:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("email file must contain a JSON object")
    missing = [key for key in REQUIRED_KEYS if not isinstance(data.get(key), str)]
    if missing:
        raise ValueError(f"missing required keys: {', '.join(missing)}")
    return data


def _record_trace(
    conn: sqlite3.Connection,
    run_id: str,
    email_id: int | None,
    latency_ms: int,
    output_json: dict,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO traces (run_id, email_id, stage, latency_ms, error, output_json)
        VALUES (?, ?, 'ingest', ?, ?, ?)
        """,
        (run_id, email_id, latency_ms, error, json.dumps(output_json)),
    )


def _ingest_file(conn: sqlite3.Connection, path: Path, run_id: str) -> str:
    started = time.monotonic()
    conn.execute("BEGIN")
    try:
        try:
            email = _parse_email(path.read_bytes())
        except (json.JSONDecodeError, ValueError) as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            _record_trace(
                conn, run_id, None, latency_ms, {"file": path.name}, error=str(exc)
            )
            conn.commit()
            return "malformed"

        cursor = conn.execute(
            """
            INSERT INTO emails (message_id, sender, subject, body_raw, status)
            VALUES (?, ?, ?, ?, 'received')
            ON CONFLICT(message_id) DO NOTHING
            """,
            (email["message_id"], email["sender"], email["subject"], email["body"]),
        )

        if cursor.rowcount:
            result = "ingested"
            email_id = cursor.lastrowid
        else:
            result = "skipped"
            row = conn.execute(
                "SELECT id FROM emails WHERE message_id = ?",
                (email["message_id"],),
            ).fetchone()
            email_id = row["id"]

        latency_ms = int((time.monotonic() - started) * 1000)
        _record_trace(
            conn, run_id, email_id, latency_ms, {"file": path.name, "result": result}
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def ingest_mailbox(conn: sqlite3.Connection, mailbox_dir: Path, run_id: str) -> IngestSummary:
    summary = IngestSummary()
    for path in sorted(mailbox_dir.glob("*.json")):
        result = _ingest_file(conn, path, run_id)
        setattr(summary, result, getattr(summary, result) + 1)
    return summary


def _make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    run_id = _make_run_id()
    conn = connect()
    try:
        summary = ingest_mailbox(conn, mailboxdir, run_id)
    finally:
        conn.close()
    print(
        f"run_id={run_id} ingested={summary.ingested} "
        f"skipped={summary.skipped} malformed={summary.malformed}"
    )
