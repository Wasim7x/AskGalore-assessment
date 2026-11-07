# One-page reflection

**Wasim Ehsan** · 2026-11-07

## What I'd build with three months instead of eight hours

The slice I shipped is honest about what it is — one component of a six-or-seven-piece system, built with deterministic scaffolding and one bounded LLM call. With three months the shape would stay the same; I'd fill in the gaps and harden the parts that are obviously thin.

First, **retrieval**. TF-IDF works at 73 accounts. At 10k+ it quietly starts missing things that share no character n-grams — `"ACH fees"` and `"Bank Charges"` is the canonical example. I'd swap in an embedding model (Voyage-3 or a small fine-tuned bi-encoder) and an ANN index (hnswlib or pgvector depending on where the data lives). The retriever interface in the prototype is intentionally small so this is a local change.

Second, **the active-learning loop**. Every row in `review_queue.json` that gets a human decision is a labeled example waiting to happen. Fine-tune a small reranker on top of the retriever, and the mapper's calibration gets measurably better within the first few hundred decisions. This is the kind of flywheel that only exists if you build the queue right from day one, which is why I spent the time to get the audit log and review queue shapes correct even for one slice.

Third, **finish the other three slices.** The adjustment validator — with LLM-generated rejection explanations that read like a senior accountant wrote them, not like a compiler error. The statement builder with a verifier agent that checks accounting identities (`A = L + E`, cash tie-out, OCI roll-forward). And the reconciliation agent that narrates every delta between pre- and post-adjustment TBs, line by line.

Fourth, **multi-entity consolidation**. Intercompany elimination is matching `AR` on one entity to `AP` on another with currency tolerance and timing tolerance. It's a graph problem, not a mapping problem, and it deserves its own sub-agent.

Fifth, **the statement-cell lineage UI.** `audit_log.jsonl` has everything an auditor needs, but JSONL is not a UI. Click a BS cell → see every contributing TB row, adjustment, FX reval, with deep links back to source CSV lines. This is probably the feature that makes or breaks the product in a real sales cycle.

Sixth, **idempotency and replay.** Rerunning the pipeline with the same inputs should produce byte-identical outputs. Right now the `run_id` changes; in production I'd hash the inputs and return cached output when nothing's changed.

Seventh, **an evaluation harness.** A corpus of de-identified mapping errors from real client onboardings becomes the regression suite. Every model change or prompt change runs against it, with precision and recall reported at each confidence threshold. The team moves faster when "did I break anything?" has a 30-second answer.

## Where the prototype breaks at scale

- **COA size.** TF-IDF's lexical bias is fine at 73 accounts; at 10k+ it needs embeddings.
- **TB row count.** One LLM call per orphan is cheap at 1 orphan in 60 rows. At 500 orphans across a multi-entity consolidation, batching matters — one call deciding several related orphans together.
- **Multi-entity.** The code assumes one TB and one COA. Consolidation needs an entity hierarchy, per-entity functional currency, IC pairing, and NCI tracking. None of which this slice attempts.
- **Prior-period restatements.** The ingest flags the `6905 → 6900` rename but has no mechanism to restate comparatives. That's its own module with its own audit model.
- **Partial-failure idempotency.** If the pipeline dies at row 30 of 61, re-running starts over. Production needs per-row checkpointing and resume.
- **Concurrency.** Two preparers running the pipeline against the same period would race on the audit log. Needs a per-period lock and a version column.

## How I used AI tools

I used Claude (via Claude Code) throughout. The honest version:

**Where it helped.** I'd write a module and then ask "is any part of this better left as an LLM call?" and vice-versa — useful for catching both over-use and under-use. I used it to pressure-test the verifier finding codes and their severity rules, specifically the contra-account warning, because the right behaviour is non-obvious — blocking is wrong, silently accepting is also wrong, and "warn and route" is the compromise. It wrote first drafts of the README and most of this document (I then edited). And it suggested `char_wb` n-grams for the TF-IDF vectorizer when I would have defaulted to word tokens, which handle `"Conf travel"` → `"Travel and Entertainment"` worse.

**Where it led me wrong.** It initially proposed a multi-agent LangGraph topology with six or more agents. For a six-hour prototype against a brief that explicitly calls out "12-agent swarm is a red flag" that was exactly the wrong instinct, and I cut it back to one real agent (the mapper) with deterministic everything else. It also wanted to put the `DR = CR` balance check into the mapper's post-processing, which is wrong — the mapper only sees one TB row at a time, so it has no basis for a journal-level check. That belongs in the adjustment validator slice I didn't build. And it floated the idea of a graph DB for the audit log, which would have been serious over-engineering for a prototype where JSONL is clearly the right primitive.

The pattern I kept noticing: it's very good inside the boundaries of a well-scoped module, and very bad at *where to draw the module boundaries*. Module-boundary decisions I kept as my own calls.

## One thing I think you're underestimating

**The cost of being wrong about accounting is asymmetric, and that asymmetry has to be baked into the product, not patched on later.**

A statement generator that silently picks the wrong cash-flow category on 1% of accounts looks fine in a demo. It also causes a restatement two quarters later. A system that *refuses to emit* cash flow when 2% of accounts have `cf_category=TBD` looks worse in a demo. It's also correct. The hard part of this product isn't making the agent smart — it's making the product *comfortable refusing to output*. Every piece of my prototype errs toward escalation over confidence, and I'd double down on that discipline in production: the review queue should feel busy, not empty. An empty queue means the model is overconfident.

The second-order problem: this only works if human review is *fast*. One-click accept/reject with keyboard shortcuts, batched by similar-looking entries, with the top few mappings pre-selected. If opening the queue feels like homework, preparers will stop using it and start auto-accepting, which defeats the whole architecture. The mapping engine is a weekend project. **The review UX is where the actual moat is.**
