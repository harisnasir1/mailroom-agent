import json
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from rapidfuzz import fuzz

from db.db import connect
from matcher.extract import Extracted
from matcher.refscan import normalise_ref



_SOURCE_PRIORITY = {"new": 0, "sender": 1, "extract": 2, "body": 3}

_DECISIVE_TYPES = {"our_ref", "insurer_ref", "vehicle_reg", "property_address"}

_TITLE_RE = re.compile(r"\b(mr|mrs|ms|dr)\b")
_PUNCT_RE = re.compile(r"[^\w\s]")
_SPACE_RE = re.compile(r"\s+")


@dataclass
class World:
    identifiers: dict[str, list[tuple[int, str]]]  # value_normalised -> [(matter_id, type)]
    client_names: list[tuple[int, str]]  # [(matter_id, normalised_client_name)]


@dataclass
class Vote:
    field: str
    value: str
    matter_ids: list[int]
    tier: str
    location: str


@dataclass
class LookupResult:
    votes_by_matter: dict[int, list[Vote]]
    closed_refs: list[dict]
    all_votes: list[Vote]


def _normalise_text(text: str) -> str:
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _TITLE_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def _normalise_fingerprint(text: str) -> str:
    return text.strip().lower()


def load_world(conn: sqlite3.Connection) -> World:
    identifiers: dict[str, list[tuple[int, str]]] = {}
    for row in conn.execute(
        """
        SELECT mi.value_normalised, mi.type, mi.matter_id
        FROM matter_identifiers mi
        JOIN matters m ON m.id = mi.matter_id
        WHERE m.status = 'open'
        """
    ):
        identifiers.setdefault(row["value_normalised"], []).append((row["matter_id"], row["type"]))

    client_names: list[tuple[int, str]] = []
    for row in conn.execute(
        "SELECT id, client_name FROM matters WHERE status = 'open' AND client_name IS NOT NULL"
    ):
        client_names.append((row["id"], _normalise_text(row["client_name"])))

    return World(identifiers=identifiers, client_names=client_names)


def _fuzzy_names_match(a: str, b: str) -> bool:
    if len(a) < 6 or len(b) < 6:
        return False
    return fuzz.token_sort_ratio(a, b) >= 90


def _fuzzy_name_in_text(name: str, text: str) -> bool:
    if len(name) < 6:
        return False
    words = text.split()
    window_size = len(name.split())
    if window_size == 0:
        return False
    for i in range(len(words) - window_size + 1):
        window = " ".join(words[i : i + window_size])
        if fuzz.token_sort_ratio(name, window) >= 90:
            return True
    return False


def _assign_tier(field_name: str, matter_ids: list[int], location: str) -> str:
    if len(matter_ids) >= 2:
        return "shared"
    if field_name in _DECISIVE_TYPES and location == "new":
        return "decisive"
    return "specific"


def collect_votes(
    world: World,
    refs: list[dict],
    extracted: Extracted | None,
    sender: str,
    body_new: str,
) -> list[Vote]:
    candidates: dict[tuple[str, str], tuple[list[int], str]] = {}

    def offer(field_name: str, value: str, matter_ids: list[int], location: str) -> None:
        if not matter_ids:
            return
        key = (field_name, value)
        existing = candidates.get(key)
        if existing is None or _SOURCE_PRIORITY[location] < _SOURCE_PRIORITY[existing[1]]:
            candidates[key] = (matter_ids, location)

    # a. refscan refs - new location, resolved to an open matter, only.
    for ref in refs:
        if ref["location"] != "new":
            continue
        if ref["matter_id"] is None or ref["matter_status"] != "open":
            continue
        offer("our_ref", ref["normalised"], [ref["matter_id"]], "new")

    # b. sender - exact contact_email lookup.
    sender_norm = (sender or "").strip().lower()
    if sender_norm:
        matches = world.identifiers.get(sender_norm, [])
        matter_ids = sorted({mid for mid, typ in matches if typ == "contact_email"})
        offer("contact_email", sender_norm, matter_ids, "sender")

    # c. extract's structured fields.
    if extracted is not None:
        for raw_ref in extracted.references:
            try:
                normalised = normalise_ref(raw_ref)
            except ValueError:
                continue
            matches = world.identifiers.get(normalised, [])
            matter_ids = sorted({mid for mid, typ in matches if typ == "our_ref"})
            offer("our_ref", normalised, matter_ids, "extract")

        if extracted.fingerprint:
            fp = _normalise_fingerprint(extracted.fingerprint)
            matches = world.identifiers.get(fp, [])
            for typ in ("vehicle_reg", "property_address"):
                matter_ids = sorted({mid for mid, t in matches if t == typ})
                offer(typ, fp, matter_ids, "extract")

        if extracted.client_name:
            name = _normalise_text(extracted.client_name)
            matter_ids = sorted({mid for mid, cand in world.client_names if _fuzzy_names_match(name, cand)})
            offer("client_name", name, matter_ids, "extract")

    # d. always: scan body_new in code, regardless of whether extract ran.
    body_lower = (body_new or "").lower()
    for value, matches in world.identifiers.items():
        if len(value) < 5 or value not in body_lower:
            continue
        by_type: dict[str, set[int]] = {}
        for mid, typ in matches:
            by_type.setdefault(typ, set()).add(mid)
        for typ, matter_ids in by_type.items():
            offer(typ, value, sorted(matter_ids), "body")

    body_norm = _normalise_text(body_new or "")
    seen_names: set[str] = set()
    for _, cand_name in world.client_names:
        if cand_name in seen_names:
            continue
        seen_names.add(cand_name)
        if _fuzzy_name_in_text(cand_name, body_norm):
            matter_ids = sorted({mid for mid, n in world.client_names if n == cand_name})
            offer("client_name", cand_name, matter_ids, "body")

    votes: list[Vote] = []
    for (field_name, value), (matter_ids, location) in candidates.items():
        tier = _assign_tier(field_name, matter_ids, location)
        votes.append(Vote(field=field_name, value=value, matter_ids=matter_ids, tier=tier, location=location))
    return votes


def group_by_matter(votes: list[Vote], refs: list[dict]) -> tuple[dict[int, list[Vote]], list[dict]]:
    votes_by_matter: dict[int, list[Vote]] = {}
    for vote in votes:
        for matter_id in vote.matter_ids:
            votes_by_matter.setdefault(matter_id, []).append(vote)

    closed_refs = [
        ref
        for ref in refs
        if ref["location"] == "new" and ref["matter_id"] is not None and ref["matter_status"] == "closed"
    ]
    return votes_by_matter, closed_refs


def lookup_email(
    conn: sqlite3.Connection,
    world: World,
    run_id: str,
    email_id: int,
    refs: list[dict],
    extracted: Extracted | None,
    sender: str,
    body_new: str,
) -> LookupResult:
    votes = collect_votes(world, refs, extracted, sender, body_new)
    votes_by_matter, closed_refs = group_by_matter(votes, refs)

    conn.execute(
        """
        INSERT INTO traces (run_id, email_id, stage, output_json)
        VALUES (?, ?, 'lookup', ?)
        """,
        (run_id, email_id, json.dumps({"votes": [asdict(v) for v in votes]})),
    )
    conn.commit()

    return LookupResult(votes_by_matter=votes_by_matter, closed_refs=closed_refs, all_votes=votes)


def _make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _latest_trace(conn: sqlite3.Connection, email_id: int, stage: str) -> dict | None:
    row = conn.execute(
        "SELECT output_json FROM traces WHERE email_id = ? AND stage = ? ORDER BY id DESC LIMIT 1",
        (email_id, stage),
    ).fetchone()
    if row is None or row["output_json"] is None:
        return None
    return json.loads(row["output_json"])


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m matcher.lookup <message_id>")
        raise SystemExit(1)

    target_message_id = sys.argv[1]

    conn = connect()

    email_row = conn.execute(
        "SELECT id, sender, body_new FROM emails WHERE message_id = ?", (target_message_id,)
    ).fetchone()
    if email_row is None:
        print(f"no email found with message_id={target_message_id!r}")
        raise SystemExit(1)


    email_id = email_row["id"]

    refscan_data = _latest_trace(conn, email_id, "refscan")

    refs = refscan_data["refs"] if refscan_data else []

    extract_data = _latest_trace(conn, email_id, "extract")

    extracted = None

    if extract_data is not None and "is_matter_related" in extract_data:
        extracted = Extracted.model_validate(extract_data)

    world = load_world(conn)

    result = lookup_email(
        conn, world, _make_run_id(), email_id, refs, extracted,
        email_row["sender"] or "", email_row["body_new"] or "",
    )

    conn.close()

    print(f"message_id={target_message_id} email_id={email_id}")
    print(f"votes ({len(result.all_votes)}):")

    for v in result.all_votes:
        print(f"  {v.field}={v.value!r} matter_ids={v.matter_ids} tier={v.tier} location={v.location}")

    print(f"votes_by_matter: {{ {', '.join(f'{k}: {len(v)}' for k, v in result.votes_by_matter.items())} }}")
    print(f"closed_refs: {result.closed_refs}")
 