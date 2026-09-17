# matter-match

Routes inbound legal correspondence to the right case file — or to a human
when it shouldn't decide alone.

An agent pipeline built as a practice take-home: ingest a mailbox, extract
facts, match against open matters, and route every email to
**matched / needs_review / refused** with evidence, cost tracking, and an
eval that prices mistakes differently.

Design rule the whole system follows: **AI reads and proposes. Code counts
and decides.** The model never sees the full case list, never writes to the
database, and never makes a final call.

## How to run

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env          # add your Anthropic API key
    python data/generate.py       # builds the synthetic world (seed 42)
    python -m matcher.ingest      # TODO: replace with single run command
    python -m matcher.clean
    # TODO: further stages as they land / final: python -m matcher.run --config C

Re-running is safe by design: ingest skips known message_ids, decisions are
unique per (email, config). Run twice — the second run is a no-op. To reset
the world: run generate.py again.

## The pipeline

    ingest → clean → ref scan → extract(LLM) → lookup → rules
                                   → adjudicate(LLM, ties only) → gate → act

Cheap path: a clean ref from a known contact never touches the LLM.
Fail-closed: timeouts, invalid JSON, conflicts, and injection flags can
only downgrade an email toward human review — never upgrade to matched.

## Data

Fully synthetic. `data/generate.py` (seeded, deterministic) creates 15
matters, ~45 identifiers, and 25 mailbox emails including planted typos,
conflicting refs, a shared client name, quoted-chain traps, and prompt
injections. `MANIFEST.md` lists every planted case; `eval/gold.json` is
the answer key. No real data anywhere.

## Eval

    python eval/run_eval.py      # TODO once built

Compares configs (A: rules only / B: +extract / C: +adjudicate) on the
same gold set. Metric: penalty-weighted errors — wrong auto-match costs
10, unnecessary human review costs 1 — plus automation rate and cost per
item. Automation rate is reported because "send everything to a human"
scores a perfect penalty and is useless.

## Status

Working: schema, generator, ingest, clean.        # TODO keep current
Next: ref scan, extract, lookup, rules, gate, act, eval.

## Decisions

See DECISION_LOG.md for cuts and trade-offs (history emails cut,
identifier auto-learning cut, injection routing rule, SQLite over
Postgres, etc).        # TODO write this file — you have ~10 entries
                       # already decided in this build