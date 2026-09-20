import json
import sqlite3

from matcher.rules import Decision


def apply_decision(conn: sqlite3.Connection, email_id: int, config: str, decision: Decision, packet: dict) -> str:
    conn.execute("BEGIN")
    try:
        cursor = conn.execute(
            """
            INSERT INTO decisions (email_id, config, route, matter_id, reason_code, evidence_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(email_id, config) DO NOTHING
            """,
            (email_id, config, decision.route, decision.matter_id, decision.reason_code, json.dumps(packet)),
        )

        if cursor.rowcount == 0:
            conn.execute(
                """
                INSERT INTO traces (email_id, stage, output_json)
                VALUES (?, 'act', ?)
                """,
                (email_id, json.dumps({"route": decision.route, "reason": decision.reason_code, "skipped": True})),
            )
            conn.commit()
            return "skipped"

        decision_id = cursor.lastrowid

        if decision.route == "needs_review":
            conn.execute(
                """
                INSERT INTO review_tasks (decision_id, packet_json, status)
                VALUES (?, ?, 'open')
                """,
                (decision_id, json.dumps(packet)),
            )

        conn.execute(
            "UPDATE emails SET status = ? WHERE id = ?",
            (decision.route, email_id),
        )

        conn.execute(
            """
            INSERT INTO traces (email_id, stage, output_json)
            VALUES (?, 'act', ?)
            """,
            (email_id, json.dumps({"route": decision.route, "reason": decision.reason_code, "skipped": False})),
        )

        conn.commit()
        return "applied"
    except Exception:
        conn.rollback()
        raise
