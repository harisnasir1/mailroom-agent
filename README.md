# matter-match

Routes inbound legal correspondence to the right case file — or to a human
when it shouldn't decide alone.

Pipeline: ingest a mailbox, extract facts, match against open matters, route
every email to **matched / needs_review / refused** — with evidence, cost
tracking, and a penalty-weighted eval.

Design rule: **AI reads and proposes. Code counts and decides.** The model
never sees the full case list, never writes to the database, never makes a
final call.

## Run

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env                 # add your API key
    python data/generate.py              # deterministic world, seed 42
    python -m matcher.run --config C     # full pipeline
    python -m matcher.run --config C     # rerun: all skipped, £0 — idempotent
    python -m eval.run_eval              # A/B/C comparison vs gold

Configs: A = rules only (no LLM) · B = + extract · C = + adjudicate on ties.
Reset the world anytime: rerun generate.py.

## Pipeline

    ingest → clean → ref scan → [extract LLM] → lookup → rules
                                 → [adjudicate LLM, ties only → gate] → act

- Clean ref + known sender skips the LLM entirely (cost path).
- Fail-closed: timeouts, bad JSON, conflicts, injection flags only ever
  downgrade toward human review — never upgrade to matched.
- Adjudicate's proposals are gate-checked in code: candidate must be in the
  list, quotes must be verbatim substrings, no injection flag.

## Data

Fully synthetic, generated (seed 42): 15 matters, ~45 identifiers, 30
mailbox emails with planted typos, conflicting refs, shared names,
quoted-chain traps, and prompt injections. `MANIFEST.md` lists every case;
`eval/gold.json` is the answer key.

Hand-written unseen tests in `tests/handwritten/` — copy into `mailbox/`,
run C:
- **t01** messy client email, thin evidence → adjudicate declines → review
- **t02** real client quoting the wrong John Smith's ref → conflict, never auto-filed
- **t03** shared insurer sender, two candidates → adjudicate picks, gate verifies → matched

## Eval

Penalty-weighted: wrong auto-match = 10, unnecessary review = 1. Automation
rate reported alongside — "send everything to a human" scores a perfect
penalty and is useless.

| config | penalty | accuracy | automation | cost/item | ×10k/mo |
|--------|---------|----------|------------|-----------|---------|
| A      | 6       | 77.8%    | 37.0%      | £0        | £0      |
| B      | 2       | 92.6%    | 51.9%      | £0.00016  | £1.60   |
| C      | 1*      | 96.7%    | 50.0%      | £0.00015† | £1.53   |

\* remaining miss: e25, a borderline injection the small model flips on —
kept as a documented limitation, argues for multi-run evals.
† C < B is sampling variance in output tokens, larger than adjudicate's
true cost at this scale — single-run cost comparisons are noise.

## Audit a decision

    sqlite3 matcher.db "SELECT stage, output_json FROM traces t
      JOIN emails e ON e.id=t.email_id
      WHERE e.message_id='<e11@testgen.local>' ORDER BY t.id;"

Every stage logs one row: votes, gate results, tokens, cost. An ops person
can reconstruct any decision without reading source.

## Limitations

- Identifiers are seeded, never learned. The loop that promotes evidence
  from confirmed matches into matter_identifiers is designed (with a
  poisoning defence) but not built. See DECISION_LOG entry 3.
- One planted attack (e25) flips between runs on the small model. Kept
  as a visible miss; it fails closed either way.
- No PDFs or attachments, no threading headers, no new client intake,
  English only. Full cut list in DECISION_LOG.