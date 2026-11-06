# Architecture — ERP-Driven Financial Statement Generation

**Author:** Wasim Ehsan · **Date:** 2026-11-07 · **Assignment:** AskGalore AI Agentic Engineer take-home

This document is the main evaluation artifact. It covers the *whole* system (all four statements), not just the slice I prototyped. The prototype — a TB → COA Mapper — is one component of the broader topology described in §3.

---

## 1. Problem decomposition

"Generate the four financial statements" decomposes into **at least six sub-problems** with wildly different reliability requirements:

| # | Sub-problem | Deterministic? | LLM role | Blast radius if wrong |
|---|---|---|---|---|
| 1 | TB ingest + dedupe + FX translation to functional currency | Yes | None | High — garbage in → garbage everywhere |
| 2 | **TB → COA mapping** (orphans, ambiguity) | Partial | Judge borderline cases | High — mis-mapped revenue hits Net Income |
| 3 | Manual adjustment validation (DR=CR, COA coverage, IC circularity) | Mostly | Explain rejections to finance users | Medium — rejected entries can be re-posted |
| 4 | Statement arithmetic (BS, P&L rollups) | Yes | None | Catastrophic — the numbers must tie |
| 5 | Cash Flow Statement classification (operating / investing / financing) | Partial | Reason about reclassifications, non-cash items | Medium — misclassification is a restatement risk |
| 6 | SOCIE walk (opening → closing equity) | Mostly | Narrative generation for prior-period restatements | Low — mostly deterministic once (4) is correct |

The brief rewards systems that recognize this. A single prompt-the-model pipeline collapses all six into one unreliable generation step.

## 2. Agent topology

**Orchestrator + specialist sub-agents, with a strict deterministic–LLM boundary.** Not a 12-agent swarm (the brief's explicit anti-pattern), and not a single agent with 40 tools (loses traceability).

```
                       ┌─────────────────────────────────────────┐
                       │          Orchestrator (code)            │
                       │  - idempotent run_id                    │
                       │  - dependency graph of steps            │
                       │  - resumable from last checkpoint       │
                       └───────────────┬─────────────────────────┘
                                       │
         ┌────────────────┬────────────┼─────────────┬───────────────┬──────────────┐
         ▼                ▼            ▼             ▼               ▼              ▼
   Ingest & FX       COA Mapper   Adjustment    Statement       Cash-Flow       Narrative /
   (det.)            (det.+LLM)   Validator     Builder         Classifier      SOCIE walk
                                  (det.+LLM)    (det.)          (det.+LLM)      (LLM-light)
                                       │                             │
                                       ▼                             ▼
                                  Verifier                      Verifier
                                   (det.)                        (det.)
```

**Justification.** Each sub-agent owns exactly one responsibility with its own reliability contract. The orchestrator is plain code, not an LLM — the control flow is a finite dependency graph, not something that benefits from reasoning. Verifiers are always deterministic; they are the "boring and never wrong" counterweight to any LLM step.

## 3. Deterministic vs LLM boundary

> *"Where does the AI actually earn its keep?"*

| Component | Mechanism | Why |
|---|---|---|
| TB ingest, dedupe, FX translation | **Deterministic** | Arithmetic. An LLM here is pure liability. |
| Exact COA code lookup | **Deterministic** | ~90% of TB rows. Dictionary lookup, not reasoning. |
| **Mapping orphans / ambiguous codes** | **LLM** | Short, noisy labels (`"Conf travel"`, `"Sundry Operating Expenses"`). Lexical similarity narrows the field; semantic judgment picks the right node. |
| Adjustment balance check (DR = CR) | **Deterministic** | Pure arithmetic. |
| **Adjustment rejection explanations** | **LLM** | "Debit ≠ credit" is useless to a finance user. "You posted JE-002 with a $3,500 imbalance — the T&E reversal is missing $3,500 compared to the marketing reclass. Did you intend to also reduce T&E by $3,500?" is useful. |
| Statement arithmetic (BS, P&L) | **Deterministic** | The numbers must tie. There is nothing to reason about. |
| Cash-flow classification of `cf_category=TBD` nodes | **LLM** | Requires judgment: is `2170 Intercompany Payable` operating or financing? Depends on policy. |
| SOCIE narrative (restatement commentary) | **LLM** | Generation, not calculation. |
| Final reconciliation (DR=CR, A=L+E, cash tie-out) | **Deterministic** | Non-negotiable. |

The rule: **LLM is used for semantic ambiguity and natural-language generation, never for arithmetic and never for the final tie-out.**

## 4. Failure modes and how we handle them

The brief names five production-killing failure modes. One by one:

### 4.1 Hallucinated account mappings
- **Guard 1 (retrieval):** the LLM only sees top-k COA candidates retrieved by lexical similarity. It never sees the full COA.
- **Guard 2 (code-level enforcement):** the mapper's post-processing refuses any `chosen_code` not in the candidate list. Even if the LLM invents one, it is dropped and the row is escalated. Tested in [tests/test_pipeline.py](../tests/test_pipeline.py).
- **Guard 3 (verifier):** the verifier re-checks the chosen node against the COA dictionary, blocking unknown codes and header-level targets.

### 4.2 Debits ≠ credits after adjustments
- **Pre-posting:** adjustment validator rejects any entry where `sum(debits) ≠ sum(credits)` with a plain-English explanation.
- **Post-posting:** after all accepted adjustments are applied, the statement builder recomputes `Σ(DR) − Σ(CR)` across the post-adjustment TB. Any non-zero delta blocks statement emission and surfaces as a reconciliation task.

### 4.3 An account that fits no COA node
- **Prototype behavior:** the mapper returns `chosen_code=null` with a plain-English rationale. The router escalates. Nothing is silently dropped or stuffed into a suspense account.
- **Production extension:** HITL queue with a UI that proposes creating a new COA node OR mapping to an existing one, with the reviewer's decision becoming a training signal.

### 4.4 FX gaps
- `fx_rates.csv` is missing `GBP period_end` — observed during ingest. The system marks any GBP-denominated balance sheet row as *unresolvable* and blocks balance-sheet emission for that currency. P&L (which uses period-average, which IS present) can still generate.
- An FX-gap finding on the verifier always blocks, never warns.

### 4.5 Circular intercompany entries
- JE-008 posts DR and CR to the same account (`2170`) — a classic no-op that reconciliation workbooks sometimes rely on.
- **Rule:** adjustment validator detects any entry whose lines reduce to a net-zero impact on *every* account it touches. These are flagged as `CIRCULAR_IC` — warn (not block) with a message asking the preparer to confirm the journal exists for audit-trail reasons rather than accounting effect.

## 5. Validation and self-correction loop

For any agent decision:

```
          ┌─────────────┐
   ──────►│  Propose    │                                   ┌──────────────┐
          │  (LLM)      │──── structured JSON ─────────────►│   Verifier   │ (deterministic)
          └─────────────┘                                   └──────┬───────┘
                                                                   │
                                                                   ▼
                                                              ┌────────┐
                                                              │ Route  │   warning → downgrade
                                                              └───┬────┘   block   → escalate
                                                                  │
                                       ┌──────────────────────────┼──────────────────────────┐
                                       ▼                          ▼                          ▼
                                     AUTO                      PROPOSE                    ESCALATE
                                   (write-through)          (needs sign-off)         (HITL queue)
```

**Self-correction policy.** On a block, the orchestrator retries *once* with a tighter prompt that names the specific finding (e.g. "your previous proposal picked a header; pick a leaf"). If the retry also blocks, the row is escalated. No infinite correction loops — the brief rewards knowing when to stop and ask a human.

**What the agent checks before returning output (prototype):**
1. Did I only choose a code from the candidate list? *(Guardrail in mapper.)*
2. Does the code exist in the COA? *(UNKNOWN_CODE block.)*
3. Is the target a leaf posting account? *(HEADER_TARGET block.)*
4. Does the normal balance match the TB side? *(Warn on mismatch — contra accounts are legitimate.)*
5. Is the `cf_category` set on the chosen node? *(Warn if TBD.)*

## 6. Auditor traceability

An auditor should be able to click any cell on the Balance Sheet and get back to source rows. The prototype's `audit_log.jsonl` is the proof-of-concept for this.

**Trace path (BS cell → source):**

```
BS cell value
   │
   └── sum of mapped_to == <account_code>
         │
         └── for each contributing TB row, look up its run_id + account_code in audit_log.jsonl
               │
               └── read the record:
                     - tb_row.source_csv_rows      ──► trial_balance.csv line numbers
                     - retrieved_candidates        ──► what the mapper considered
                     - proposal.rationale          ──► why this one
                     - findings                    ──► what the verifier flagged
                     - decision + decision_reason  ──► why auto/propose/escalate
                     - chosen_coa                  ──► the COA node metadata
```

**Immutability.** `audit_log.jsonl` is append-only per run. Re-runs produce a new `run_id`; we never mutate a prior run's log. The mappings.json and review_queue.json are overwritten each run, but the audit log is the source of truth and can be diffed across runs to see what changed.

**For manual adjustments and statement cells**, the pattern is the same: every statement cell carries a `contributors` list of `(source, id, amount)` tuples where `source` is one of `TB`, `ADJUSTMENT`, `FX_REVAL`, `ELIMINATION`. The adjustment store references the original `manual_adjustments.json` entry by `id`.

## 7. Prototype slice — TB → COA Mapper

Implementation of §3's "COA Mapper" box. Source in [src/](../src/).

**Component diagram:**

```
         inputs/trial_balance.csv ───┐
                                     ▼
         inputs/chart_of_accounts.csv
                    │
                    ▼
         ┌────────────────────┐    exact match     ┌──────────┐    ┌────────┐    ┌──────────────┐
         │     ingest.py      │────────────────────►│verifier  │───►│router  │───►│  audit.py    │
         │  (dedupe, classify)│                    │          │    │        │    │              │
         └─────────┬──────────┘                    └──────────┘    └────────┘    │  mappings    │
                   │ orphan or ambiguous                                         │  queue       │
                   ▼                                                             │  audit_log   │
            ┌──────────────┐    top-k      ┌──────────┐    structured   ┌──────┐ └──────────────┘
            │ retrieve.py  │──────────────►│ mapper   │─── proposal ───►│verif.│
            │   (TF-IDF)   │               │ (LLM +   │                 │      │
            │              │               │ heuristic│                 │      │
            │              │               │ fallback)│                 │      │
            └──────────────┘               └──────────┘                 └───┬──┘
                                                                            ▼
                                                                        router + audit
```

**Why this slice.** It is the single place in the end-to-end system where the LLM's contribution is both necessary (lexical similarity alone can't disambiguate `9999 Suspense - Unmapped`) and bounded (retrieval + guardrails + verifier + routing keep the LLM from eating the whole problem).

**What it demonstrates against the rubric:**

| Rubric signal | Where in the slice |
|---|---|
| Problem decomposition | Hard separation of ingest / retrieve / propose / verify / route / audit |
| Agentic judgment ("when NOT to use an LLM") | 60 of 61 TB rows never touch the LLM; exact lookup handles them |
| Production thinking | Audit log, idempotent run_ids, HITL escalation, offline fallback, CSV row traceability |
| Domain humility | Contra-account warning (not block), TBD cf_category warning, clarifying-question plan in §9 |
| AI tool fluency | Tool-use with JSON schema, hard guardrail against hallucinated codes, confidence calibration by threshold |

**What's deliberately NOT in the prototype** (would be in production):
- Embeddings over character n-grams. TF-IDF is fine for a 73-account COA; at 10k+ accounts we need a real embedding model + ANN index.
- Active-learning loop that turns HITL decisions into fine-tuning data.
- Multi-tenant COA versioning — the current code reads one COA file.
- Real intercompany-pair detection (`2170` on one entity = `1270` on another) — this slice handles single-entity.

## 8. Known defects I noticed in the mock inputs

Beyond the brief's seeded defects, the data contains:

- `trial_balance.csv` **does not balance**. Σ(DR) − Σ(CR) ≈ **+$12,400**, which is the exact `9999 Suspense - Unmapped` amount — i.e. the TB is balanced *after* the suspense account is reclassed. This is realistic: NetSuite exports often include suspense balances.
- `prior_period_tb.csv` uses `6905 Sundry Operating Expenses`; the current period uses `6900 Other Operating Expenses`. This is the "one account renamed" hint — the opening balance walk needs a COA-remap table.
- `fx_rates.csv` has no `GBP period_end` rate (as the brief warned).
- `fx_rates.csv` has no `EUR` entry for `rate_type=period_average` at `2024-Q3` or earlier — opening-balance translation for comparatives will fail cleanly or fall back to `rate_type=opening`.
- `manual_adjustments.json`:
  - JE-002: DR 28,500 vs CR 25,000 — imbalanced by $3,500. **Block.**
  - JE-005: posts to `6315`, which is not in the COA. **Block** until mapped.
  - JE-008: DR and CR on the same account `2170`. **Warn** as CIRCULAR_IC.
- COA has `cf_category=TBD` on `1150` and `2170`. **Blocks cash-flow emission** until resolved; does not block BS or P&L.
- COA node `1290 Other Assets` is a Header with no children — unreachable for postings but harmless.

## 9. Clarifying questions I would email before starting

These are the three I would send under the brief's "up to 3 clarifying questions" allowance:

1. **Suspense treatment.** Is `9999 Suspense - Unmapped` meant to be reclassed by the mapping agent, or does your platform preserve suspense accounts as a dedicated BS line until operationally resolved? The design differs materially.
2. **Contra-account convention.** When a posting-account's TB side disagrees with its COA normal balance (e.g. a gain booked against a Debit-normal expense account), does your current product block or warn? I defaulted to warn, but blocking is reasonable if your users expect strict postings.
3. **Prior-period restatements vs comparatives.** For the "one account renamed" case (`6905 → 6900`), should the system restate prior-period comparatives automatically or always escalate to a human? This is a materiality call that depends on your audit framework.

## 10. Scale and extension plan

See [REFLECTION.md](REFLECTION.md) for the 3-month roadmap and scale breakpoints.
