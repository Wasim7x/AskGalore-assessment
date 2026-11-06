# One-Page Reflection

**Author:** Wasim Ehsan · **Date:** 2026-11-07

## What I would build with 3 months instead of 8 hours

1. **Replace TF-IDF with a real retrieval layer.** An embedding model (Voyage-3 or a small fine-tuned bi-encoder) + an ANN index (hnswlib / pgvector). TF-IDF works at 73 accounts; at 10k+ it will miss semantic matches that share no character n-grams (`"ACH fees"` ↔ `"Bank Charges"`).
2. **Active-learning loop.** Every HITL decision in `review_queue.json` becomes a labeled example. Fine-tune a small reranker on top of retrieval. The mapper's confidence calibration improves measurably within the first few hundred decisions.
3. **Finish the other three slices.** Adjustment validator (with LLM-generated rejection explanations in finance-user English), statement builder with a verifier agent that checks the accounting identities (`A = L + E`, `cash tie-out`, `OCI roll-forward`), and the reconciliation agent that narrates every delta between pre- and post-adjustment TBs.
4. **Multi-entity consolidation.** Intercompany elimination requires matching `AR` on one entity to `AP` on another with a currency and timing-difference tolerance. This is a graph problem, not a mapping problem, and deserves its own sub-agent.
5. **Statement-cell lineage UI.** The `audit_log.jsonl` has everything an auditor needs; it needs a web UI to actually be usable. Click a BS cell → see all contributing TB rows, adjustments, FX revals, with links to the source CSV lines.
6. **Idempotency + replay.** Re-running the pipeline with the same inputs should produce byte-identical outputs. Right now the `run_id` changes; in production, we'd hash the inputs and return cached output when unchanged.
7. **Evaluation harness.** A corpus of real (de-identified) mapping errors from client onboardings becomes the regression suite. Every model/prompt change runs against it with precision/recall at each confidence threshold.

## Where the prototype would break at scale

- **COA size.** At 10k+ accounts, TF-IDF's lexical bias dominates — needs embeddings.
- **TB row count.** One LLM call per orphan is fine at 1 orphan per 60 rows; at 500 orphans across a multi-entity consolidation, cost and latency need batching (one LLM call deciding several related orphans in one prompt).
- **Multi-entity.** The current code assumes one TB + one COA. Consolidation needs: entity hierarchy, per-entity functional currency, intercompany pairing, and NCI (non-controlling interest) tracking. None of which this slice attempts.
- **Prior-period restatements.** The current ingest flags the `6905 → 6900` rename but has no mechanism to restate comparatives. A restatement engine is its own module with its own audit model.
- **Idempotency under partial failure.** If the pipeline dies at row 30 of 61, re-running starts over. Production needs per-row checkpointing and resume.
- **Concurrency.** Multiple preparers running the pipeline against the same period would race on the audit log. Needs a per-period lock and a version column.

## How I used AI tools

**Where they helped.** I used Claude (via Claude Code) to:
- Pressure-test the deterministic/LLM boundary — I'd write a module and ask "is any part of this better left as an LLM call?" and vice-versa. Useful for catching both over- and under-use.
- Draft the verifier finding codes and their severity rules. I iterated on the contra-account warning (`NORMAL_BALANCE_MISMATCH`) with it because the right behavior is non-obvious — blocking is wrong, silently accepting is also wrong, so warn-and-route is the compromise.
- Write the README and most of this architecture doc. Then I edited them.
- Suggest the TF-IDF `char_wb` n-gram setup — I would have defaulted to word tokens, which handle `"Conf travel"` → `"Travel and Entertainment"` worse.

**Where it led me wrong (and I pushed back).**
- It initially proposed a multi-agent LangGraph topology with 6+ agents. For a 6–8 hour prototype against the brief's explicit "12-agent swarm is a red flag" warning, that was the wrong instinct. I cut it back to one real agent (the mapper) with deterministic everything else.
- It wanted to put the balance-check for debits = credits into the mapper's post-processing. That's wrong — the mapper doesn't know about full adjustments, only one TB row at a time. The balance check belongs in the (unimplemented) adjustment validator slice.
- It suggested over-engineering the audit log with a graph DB. JSONL is the right primitive for a prototype; the schema is what matters, not the storage.

The pattern: it is very good at the *inside* of a well-scoped module and very bad at where to draw module boundaries. I kept module-boundary decisions as human calls.

## One thing I think you are underestimating

**The cost of being wrong about accounting semantics is asymmetric.** A statement generator that silently picks the wrong cash-flow category on 1% of accounts looks fine in a demo and causes a restatement two quarters later. A system that refuses to emit cash flow when 2% of accounts have `cf_category=TBD` looks worse in a demo but is correct. The hard part of this product is not making the agent smart — it is making the product *comfortable refusing to output*. Everything in my prototype errs toward escalation over confidence, and I would double down on that discipline in production: the review queue should feel busy, not empty. An empty review queue means the model is overconfident.

The second-order version: this means your onboarding UX has to make human review *fast* — a one-click accept/reject with keyboard shortcuts, batched by similar-looking entries. If onboarding feels like staring at a queue, the product dies. The mapping engine is a weekend project; the review UX is the actual moat.
