import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timezone

from db.db import connect
from matcher.act import apply_decision
from matcher.adjudicate import adjudicate
from matcher.clean import _clean_row
from matcher.extract import extract_one
from matcher.lookup import load_world, lookup_email
from matcher.refscan import resolve as refscan_resolve
from matcher.refscan import scan_refs
from matcher.rules import _sender_known, decide

VALID_CONFIGS = ("A", "B", "C")
BODY_PREVIEW_CHARS = 200


def _make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _latest_output_json(conn: sqlite3.Connection, email_id: int, stage: str) -> dict | None:
    row = conn.execute(
        "SELECT output_json FROM traces WHERE email_id = ? AND stage = ? ORDER BY id DESC LIMIT 1",
        (email_id, stage),
    ).fetchone()
    if row is None or row["output_json"] is None:
        return None
    return json.loads(row["output_json"])


def _already_decided(conn: sqlite3.Connection, email_id: int, config: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM decisions WHERE email_id = ? AND config = ? LIMIT 1",
        (email_id, config),
    ).fetchone()
    return row is not None


def _clean_if_needed(conn: sqlite3.Connection, run_id: str, email_id: int, body_raw: str, body_new: str | None) -> None:
    if body_new is None:
        _clean_row(conn, email_id, body_raw, run_id)


def _refscan_one(conn: sqlite3.Connection, run_id: str, email_id: int, body_new: str, body_quoted: str) -> list[dict]:
    hits = scan_refs(body_new or "", body_quoted or "")
    resolved = refscan_resolve(conn, hits)
    conn.execute(
        """
        INSERT INTO traces (run_id, email_id, stage, output_json)
        VALUES (?, ?, 'refscan', ?)
        """,
        (run_id, email_id, json.dumps({"refs": [asdict(r) for r in resolved]})),
    )
    conn.commit()
    return [asdict(r) for r in resolved]


def _process_email(conn: sqlite3.Connection, world, run_id: str, config: str, row: sqlite3.Row) -> tuple[str, bool]:
   
    email_id = row["id"]

    _clean_if_needed(conn, run_id, email_id, row["body_raw"], row["body_new"])

    email_row = conn.execute(
        "SELECT body_new, body_quoted, injection_flag FROM emails WHERE id = ?", (email_id,)
    ).fetchone()
    body_new = email_row["body_new"] or ""
    body_quoted = email_row["body_quoted"] or ""
    injection_flag = email_row["injection_flag"]

    refs = _refscan_one(conn, run_id, email_id, body_new, body_quoted)

    sender = row["sender"] or ""
    called_llm = False

    if config in ("B", "C"):
        extracted, outcome = extract_one(
            conn, run_id, email_id, row["subject"], body_new, sender, refs, injection_flag
        )
        extract_attempted = outcome in ("extracted", "failed")
        called_llm = extract_attempted
    else:
        extracted = None
        extract_attempted = False

    lookup_result = lookup_email(conn, world, run_id, email_id, refs, extracted, sender, body_new)
    sender_known = _sender_known(conn, sender)

    decision = decide(
        lookup_result.all_votes,
        lookup_result.closed_refs,
        bool(injection_flag),
        extracted,
        extract_attempted,
        sender_known,
    )

    if config == "C" and decision.reason_code == "tie":
        decision = adjudicate(
            conn, run_id, email_id, lookup_result.all_votes, extracted, body_new, injection_flag
        )
        called_llm = True

    clean_data = _latest_output_json(conn, email_id, "clean")
    injection_patterns = clean_data.get("injection", []) if clean_data else []
    summary_text = extracted.summary if extracted is not None else body_new[:BODY_PREVIEW_CHARS]

    packet = {
        "summary": summary_text,
        "votes": [asdict(v) for v in lookup_result.all_votes],
        "closed_refs": lookup_result.closed_refs,
        "reason_code": decision.reason_code,
        "injection": {"flag": bool(injection_flag), "patterns": injection_patterns},
    }

    apply_decision(conn, email_id, config, decision, packet)
    return decision.route, called_llm


def run(config: str) -> None:
    if config not in VALID_CONFIGS:
        raise ValueError(f"unknown config: {config!r}")

    run_id = _make_run_id()
    conn = connect()
    world = load_world(conn)

    route_counts: dict[str, int] = {}
    already_decided_count = 0
    processed_count = 0
    llm_calls = 0
    llm_skipped = 0

    rows = conn.execute(
        "SELECT id, body_raw, body_new, subject, sender FROM emails ORDER BY id"
    ).fetchall()

    for row in rows:
        if _already_decided(conn, row["id"], config):
            already_decided_count += 1
            continue

        route, called_llm = _process_email(conn, world, run_id, config, row)
        processed_count += 1
        route_counts[route] = route_counts.get(route, 0) + 1
        if config in ("B", "C"):
            if called_llm:
                llm_calls += 1
            else:
                llm_skipped += 1

    tok_row = conn.execute(
        """
        SELECT COALESCE(SUM(input_tokens), 0) i, COALESCE(SUM(output_tokens), 0) o, COALESCE(SUM(cost_usd), 0) c
        FROM traces WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    total_tokens = tok_row["i"] + tok_row["o"]
    total_cost = tok_row["c"]
    cost_per_item = total_cost / processed_count if processed_count else 0.0

    conn.close()

    print(f"run_id={run_id} config={config}")
    print(f"processed={processed_count} already_decided_skipped={already_decided_count}")
    print("routes: " + (" ".join(f"{r}={c}" for r, c in sorted(route_counts.items())) or "(none)"))
    if config in ("B", "C"):
        print(f"llm_calls={llm_calls} llm_skipped={llm_skipped}")
    print(
        f"tokens={total_tokens} cost_usd={total_cost:.6f} "
        f"cost_per_item={cost_per_item:.6f} projection_x10000={cost_per_item * 10000:.4f}"
    )


if __name__ == "__main__":
    config = "B"
    args = sys.argv[1:]
    if args:
        if args[0] == "--config" and len(args) > 1:
            config = args[1]
        else:
            print("usage: python -m matcher.run [--config A|B|C]")
            raise SystemExit(1)
    run(config)
