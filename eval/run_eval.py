import json
import sqlite3
from pathlib import Path

from db.db import connect

GOLD_PATH = Path("eval/gold.json")


def _load_gold() -> dict[str, dict]:
    entries = json.loads(GOLD_PATH.read_text())
    return {entry["file"].removesuffix(".json"): entry for entry in entries}


def _decisions_for_config(conn: sqlite3.Connection, config: str) -> dict[str, dict]:
    decisions: dict[str, dict] = {}
    for row in conn.execute(
        """
        SELECT e.message_id, d.route, d.matter_id
        FROM decisions d JOIN emails e ON e.id = d.email_id
        WHERE d.config = ?
        """,
        (config,),
    ):
        stem = row["message_id"].strip("<>").split("@")[0]
        decisions[stem] = {"route": row["route"], "matter_id": row["matter_id"]}
    return decisions


def _penalty(gold_route: str, gold_matter_id: int | None, system_route: str, system_matter_id: int | None) -> int:
    if system_route == "matched":
        if gold_route == "matched" and gold_matter_id == system_matter_id:
            return 0
        return 10
    if system_route == "refused":
        if gold_route == "matched":
            return 5
        if gold_route == "refused":
            return 0
        return 1  # gold == needs_review
    # system_route == "needs_review"
    if gold_route == "needs_review":
        return 0
    return 1  # gold matched or refused, system sent it to review


def _latest_run_id_for_config(conn: sqlite3.Connection, config: str) -> str | None:
    # decisions has no run_id column, and 'extract'/'lookup'/'refscan'
    # traces are config-agnostic - every run.py invocation rewrites them
    # for whatever email it touches, regardless of config. Once more than
    # one config calls extract_one for the same email (B and C both do),
    # "the latest extract trace for this email" can belong to a LATER
    # config's run, not this one's. Bound the search to traces created at
    # or before this config's own last decision was recorded, so a config
    # that ran afterwards can't leak in; break ties by id (insertion order)
    # since created_at only has 1-second resolution.
    if config == "A":
        return None
    row = conn.execute(
        "SELECT email_id, created_at FROM decisions WHERE config = ? ORDER BY id DESC LIMIT 1",
        (config,),
    ).fetchone()
    if row is None:
        return None
    trace_row = conn.execute(
        """
        SELECT run_id FROM traces
        WHERE email_id = ? AND stage = 'extract' AND created_at <= ?
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (row["email_id"], row["created_at"]),
    ).fetchone()
    return trace_row["run_id"] if trace_row else None


def _cost_for_run(conn: sqlite3.Connection, run_id: str | None) -> tuple[float, int]:
    if run_id is None:
        return 0.0, 0
    row = conn.execute(
        """
        SELECT COALESCE(SUM(cost_usd), 0) cost,
               COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) tokens
        FROM traces WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    return row["cost"], row["tokens"]


def evaluate_config(conn: sqlite3.Connection, config: str, gold_by_stem: dict[str, dict]) -> dict:
    decisions_by_stem = _decisions_for_config(conn, config)

    confusion: dict[str, dict[str, int]] = {}
    mismatches: list[dict] = []
    total_penalty = 0
    correct = 0
    matched_count = 0
    refused_count = 0
    total = 0

    for stem, gold in sorted(gold_by_stem.items()):
        decision = decisions_by_stem.get(stem)
        if decision is None:
            continue

        total += 1
        gold_route = gold["expected_route"]
        gold_matter_id = gold["expected_matter_id"]
        system_route = decision["route"]
        system_matter_id = decision["matter_id"]

        confusion.setdefault(gold_route, {}).setdefault(system_route, 0)
        confusion[gold_route][system_route] += 1

        penalty = _penalty(gold_route, gold_matter_id, system_route, system_matter_id)
        total_penalty += penalty
        if penalty == 0:
            correct += 1
        else:
            mismatches.append(
                {
                    "file": f"{stem}.json",
                    "gold_route": gold_route,
                    "gold_matter_id": gold_matter_id,
                    "system_route": system_route,
                    "system_matter_id": system_matter_id,
                    "penalty": penalty,
                }
            )

        if system_route == "matched":
            matched_count += 1
        elif system_route == "refused":
            refused_count += 1

    accuracy = correct / total if total else 0.0
    automation_rate = (matched_count + refused_count) / total if total else 0.0

    run_id = _latest_run_id_for_config(conn, config)
    cost, tokens = _cost_for_run(conn, run_id)
    cost_per_item = cost / total if total else 0.0

    return {
        "config": config,
        "total": total,
        "penalty": total_penalty,
        "accuracy": accuracy,
        "automation_rate": automation_rate,
        "cost_per_item": cost_per_item,
        "projection_x10000": cost_per_item * 10000,
        "tokens": tokens,
        "confusion": confusion,
        "mismatches": mismatches,
    }


def _fmt_gold_or_system(route: str, matter_id: int | None) -> str:
    return route if matter_id is None else f"{route}({matter_id})"


def _print_report(results: list[dict]) -> None:
    print(f"{'config':<8}{'penalty':<9}{'accuracy':<11}{'automation%':<13}{'cost/item':<13}{'x10k':<10}")
    for r in results:
        print(
            f"{r['config']:<8}{r['penalty']:<9}{r['accuracy'] * 100:<10.1f}"
            f"{r['automation_rate'] * 100:<13.1f}{r['cost_per_item']:<13.6f}{r['projection_x10000']:<10.4f}"
        )

    for r in results:
        print()
        print(f"--- config {r['config']}: confusion (gold x system) ---")
        routes = ("matched", "needs_review", "refused")
        header = "gold\\system".ljust(14) + "".join(route.ljust(14) for route in routes)
        print(header)
        for gold_route in routes:
            row_counts = r["confusion"].get(gold_route, {})
            line = gold_route.ljust(14) + "".join(str(row_counts.get(sys_route, 0)).ljust(14) for sys_route in routes)
            print(line)

        print(f"--- config {r['config']}: mismatches ---")
        if not r["mismatches"]:
            print("  (none)")
        for m in r["mismatches"]:
            gold_str = _fmt_gold_or_system(m["gold_route"], m["gold_matter_id"])
            system_str = _fmt_gold_or_system(m["system_route"], m["system_matter_id"])
            print(f"  {m['file']}: gold={gold_str} system={system_str} penalty={m['penalty']}")


def main() -> None:
    conn = connect()
    gold_by_stem = _load_gold()
    configs = [row["config"] for row in conn.execute("SELECT DISTINCT config FROM decisions ORDER BY config")]

    results = [evaluate_config(conn, config, gold_by_stem) for config in configs]
    conn.close()

    _print_report(results)


if __name__ == "__main__":
    main()
