import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal

from db.db import connect

# Our world only has the SEN_A prefix. A new prefix (e.g. SEN_B) means
# extending this pattern, not adding a second one.
REF_PATTERN = re.compile(r"\bsen[\s_.-]*a[\s_.-]*0*(\d{1,4})\b", re.IGNORECASE)

Location = Literal["new", "quoted"]


@dataclass
class RefHit:
    raw: str
    normalised: str
    location: Location


@dataclass
class ResolvedRef:
    raw: str
    normalised: str
    location: Location
    matter_id: int | None
    matter_status: str | None


@dataclass
class EmailRefs:
    email_id: int
    refs: list[ResolvedRef]


@dataclass
class ScanSummary:
    scanned: int = 0
    with_refs: int = 0
    results: list[EmailRefs] = field(default_factory=list)


def normalise_ref(raw: str) -> str:
    match = REF_PATTERN.fullmatch(raw.strip())
    if not match:
        raise ValueError(f"not a valid SEN_A reference: {raw!r}")
    return f"sen_a_{int(match.group(1))}"


def scan_refs(body_new: str, body_quoted: str) -> list[RefHit]:
    hits: list[RefHit] = []
    for location, text in (("new", body_new), ("quoted", body_quoted)):
        seen: set[str] = set()
        for match in REF_PATTERN.finditer(text or ""):
            normalised = normalise_ref(match.group(0))
            if normalised in seen:
                continue
            seen.add(normalised)
            hits.append(RefHit(raw=match.group(0), normalised=normalised, location=location))
    return hits


def resolve(conn: sqlite3.Connection, hits: list[RefHit]) -> list[ResolvedRef]:
    normalised_values = sorted({hit.normalised for hit in hits})
    lookup: dict[str, tuple[int, str]] = {}
    if normalised_values:
        placeholders = ",".join("?" * len(normalised_values))
        rows = conn.execute(
            f"""
            SELECT mi.value_normalised, m.id, m.status
            FROM matter_identifiers mi
            JOIN matters m ON m.id = mi.matter_id
            WHERE mi.type = 'our_ref' AND mi.value_normalised IN ({placeholders})
            """,
            normalised_values,
        ).fetchall()
        lookup = {row["value_normalised"]: (row["id"], row["status"]) for row in rows}

    resolved: list[ResolvedRef] = []
    for hit in hits:
        matter_id, matter_status = lookup.get(hit.normalised, (None, None))
        resolved.append(
            ResolvedRef(
                raw=hit.raw,
                normalised=hit.normalised,
                location=hit.location,
                matter_id=matter_id,
                matter_status=matter_status,
            )
        )
    return resolved


def scan_pending(conn: sqlite3.Connection, run_id: str) -> ScanSummary:
    summary = ScanSummary()
    rows = conn.execute(
        "SELECT id, body_new, body_quoted FROM emails WHERE status = 'processing'"
    ).fetchall()

    for row in rows:
        hits = scan_refs(row["body_new"] or "", row["body_quoted"] or "")
        resolved = resolve(conn, hits)

        output_json = {"refs": [asdict(r) for r in resolved]}
        conn.execute(
            """
            INSERT INTO traces (run_id, email_id, stage, output_json)
            VALUES (?, ?, 'refscan', ?)
            """,
            (run_id, row["id"], json.dumps(output_json)),
        )
        conn.commit()

        summary.scanned += 1
        if hits:
            summary.with_refs += 1
        summary.results.append(EmailRefs(email_id=row["id"], refs=resolved))

    return summary


def _make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    run_id = _make_run_id()
    conn = connect()
    try:
        summary = scan_pending(conn, run_id)
    finally:
        conn.close()
    print(f"run_id={run_id} scanned={summary.scanned} with_refs={summary.with_refs}")
