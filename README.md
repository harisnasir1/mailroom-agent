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
    python -m data.generate             # creates matcher.db, seeds world, writes mailbox
    python -m matcher.ingest            # loads mailbox into the db (idempotent)
    python -m matcher.run --config A
    python -m matcher.run --config B
    python -m matcher.run --config C
    python -m matcher.run --config C    # rerun: all skipped, idempotent
    python -m eval.run_eval

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

Penalty-weighted: wrong auto-match = 10, unnecessary human review = 1.
Automation rate is reported alongside because "send everything to a
human" scores a perfect penalty and is useless.

| config | penalty | accuracy | automation | cost/item | x10k/mo |
|--------|---------|----------|------------|-----------|---------|
| A      | 6       | 80.0%    | 33.3%      | £0        | £0      |
| B      | 3       | 90.0%    | 43.3%      | £0.000147 | £1.48   |
| C      | 2       | 93.3%    | 46.7%      | £0.000164 | £1.64   |

Figures from a cold-clone run of the 30-email world; LLM output varies
slightly between runs. Zero wrong matches in any config on any run —
every miss in every confusion matrix is a penalty-1 unnecessary review.

Known misses, kept visible rather than tuned away:
- e25: borderline injection the small model flips on between runs;
  fails closed either way.
- e27: a matchable tie adjudicate sometimes declines; it never matches
  wrongly.

## Audit a decision

    sqlite3 matcher.db "SELECT stage, output_json FROM traces t
      JOIN emails e ON e.id=t.email_id
      WHERE e.message_id='<e11@testgen.local>' ORDER BY t.id;"

Every stage logs one row: votes, gate results, tokens, cost. An ops person
can reconstruct any decision without reading source.

## See the results

All decisions for a run:

    sqlite3 -box matcher.db "SELECT e.message_id, d.route, d.matter_id, d.reason_code
      FROM decisions d JOIN emails e ON e.id = d.email_id
      WHERE d.config='C' ORDER BY e.id;"

One email, full story (route, evidence, every stage's trace):

    sqlite3 -box matcher.db "SELECT d.route, d.matter_id, d.reason_code, d.evidence_json
      FROM decisions d JOIN emails e ON e.id = d.email_id
      WHERE e.message_id='<e11@testgen.local>' AND d.config='C';"

    sqlite3 matcher.db "SELECT stage, output_json FROM traces t
      JOIN emails e ON e.id = t.email_id
      WHERE e.message_id='<e11@testgen.local>' ORDER BY t.id;"

The evidence_json on every decision holds the votes (which clue, which
matter, what tier) — the same packet a human reviewer would see.

## Limitations

- Identifiers are seeded, never learned. The loop that promotes evidence
  from confirmed matches into matter_identifiers is designed (with a
  poisoning defence) but not built. See DECISION_LOG entry 3.
- One planted attack (e25) flips between runs on the small model. Kept
  as a visible miss; it fails closed either way.
- No PDFs or attachments, no threading headers, no new client intake,
  English only. Full cut list in DECISION_LOG.