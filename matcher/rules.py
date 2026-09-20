import json
import sqlite3
import sys
from dataclasses import dataclass

from db.db import connect
from matcher.extract import Extracted
from matcher.lookup import Vote


@dataclass
class Decision:
    route:str
    matter_id:int|None
    reason_code:str



def decide(matter_votes:list[Vote],closed_refs:list[dict],injection_flag:bool,extracted:Extracted,extract_attempted:bool,sender_known:bool)->Decision:

    if extracted is None and extract_attempted:
        return Decision(route="needs_review",matter_id=None,reason_code="model_error")

    elif injection_flag or (extracted is not None and extracted.contains_instructions):
        case_facts = len(matter_votes) > 0 or (extracted is not None and extracted.is_matter_related)
        if not sender_known and not case_facts:
            return Decision("refused", None, "hostile_content")
        return Decision("needs_review", None, "injection_suspected")

    elif len(matter_votes)==0 and (extracted is not None and extracted.is_matter_related==False):
        return Decision(route="refused",matter_id=None,reason_code="not_matter_related")

    elif len({mid for v in matter_votes if v.tier in ("decisive","specific") for mid in v.matter_ids})>1:
        return Decision(route="needs_review",matter_id=None,reason_code="conflict")

    elif len(closed_refs)>0 and not any(v.tier=="decisive" for v in matter_votes):
        return Decision(route="needs_review",matter_id=None,reason_code="closed_matter")

    elif len(decisive_matters:={mid for v in matter_votes if v.tier=="decisive" for mid in v.matter_ids})==1 and not any(mid not in decisive_matters for v in matter_votes if v.tier=="specific" for mid in v.matter_ids) and sender_known:
        return Decision(route="matched",matter_id=next(iter(decisive_matters)),reason_code="decisive_identifier")

    elif len(specific_matters:={mid for v in matter_votes if v.tier=="specific" for mid in v.matter_ids})==1 and len({v.field for v in matter_votes if v.tier=="specific"})>=2 and sender_known:
        return Decision(route="matched",matter_id=next(iter(specific_matters)),reason_code="corroborated")

    elif sender_known==False:
        return Decision(route="needs_review",matter_id=None,reason_code="new_sender")

    else:
        return Decision(route="needs_review",matter_id=None,reason_code="insufficient_evidence")


def _latest_trace(conn: sqlite3.Connection, email_id: int, stage: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT model, output_json FROM traces WHERE email_id = ? AND stage = ? ORDER BY id DESC LIMIT 1",
        (email_id, stage),
    ).fetchone()


def _load_refs(conn: sqlite3.Connection, email_id: int) -> list[dict]:
    row = _latest_trace(conn, email_id, "refscan")
    if row is None or row["output_json"] is None:
        return []
    return json.loads(row["output_json"]).get("refs", [])


def _load_votes(conn: sqlite3.Connection, email_id: int) -> list[Vote]:
    row = _latest_trace(conn, email_id, "lookup")
    if row is None or row["output_json"] is None:
        return []
    return [Vote(**v) for v in json.loads(row["output_json"]).get("votes", [])]


def _load_extract(conn: sqlite3.Connection, email_id: int) -> tuple[Extracted | None, bool]:
    row = _latest_trace(conn, email_id, "extract")
    if row is None or row["output_json"] is None:
        return None, False
    data = json.loads(row["output_json"])
    if "is_matter_related" in data:
        return Extracted.model_validate(data), True
    if "skipped" in data:
        return None, False
    # a raw llm.py attempt trace that never produced a valid result: a
    # genuine attempt was made and it failed.
    return None, True


def _sender_known(conn: sqlite3.Connection, sender: str | None) -> bool:
    sender_norm = (sender or "").strip().lower()
    if not sender_norm:
        return False
    row = conn.execute(
        """
        SELECT 1 FROM matter_identifiers mi
        JOIN matters m ON m.id = mi.matter_id
        WHERE mi.type = 'contact_email' AND mi.value_normalised = ? AND m.status = 'open'
        LIMIT 1
        """,
        (sender_norm,),
    ).fetchone()
    return row is not None


def decide_email(conn: sqlite3.Connection, email_id: int) -> Decision:
    email_row = conn.execute(
        "SELECT sender, injection_flag FROM emails WHERE id = ?", (email_id,)
    ).fetchone()

    refs = _load_refs(conn, email_id)
    closed_refs = [
        ref for ref in refs
        if ref["location"] == "new" and ref["matter_id"] is not None and ref["matter_status"] == "closed"
    ]
    matter_votes = _load_votes(conn, email_id)
    extracted, extract_attempted = _load_extract(conn, email_id)
    sender_known = _sender_known(conn, email_row["sender"])
    injection_flag = bool(email_row["injection_flag"])

    return decide(matter_votes, closed_refs, injection_flag, extracted, extract_attempted, sender_known)


def _print_row(message_id: str, decision: Decision) -> None:
    print(f"{message_id}\t{decision.route}\t{decision.matter_id}\t{decision.reason_code}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m matcher.rules <message_id> | python -m matcher.rules --all")
        raise SystemExit(1)

    arg = sys.argv[1]
    conn = connect()

    if arg == "--all":
        route_counts: dict[str, int] = {}
        for row in conn.execute(
            "SELECT id, message_id FROM emails WHERE status = 'processing' ORDER BY id"
        ):
            decision = decide_email(conn, row["id"])
            _print_row(row["message_id"], decision)
            route_counts[decision.route] = route_counts.get(decision.route, 0) + 1
        conn.close()
        print()
        print("summary: " + " ".join(f"{route}={count}" for route, count in sorted(route_counts.items())))
    else:
        email_row = conn.execute(
            "SELECT id FROM emails WHERE message_id = ?", (arg,)
        ).fetchone()
        if email_row is None:
            print(f"no email found with message_id={arg!r}")
            conn.close()
            raise SystemExit(1)
        decision = decide_email(conn, email_row["id"])
        conn.close()
        _print_row(arg, decision)
