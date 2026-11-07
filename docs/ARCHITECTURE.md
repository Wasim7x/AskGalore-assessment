# Architecture — ERP-Driven Financial Statement Generation

**Mohammad Wasim** · 2026-11-07 · AskGalore AI Agentic Engineer take-home

I built one slice end-to-end (TB → COA mapper). This document describes the system I would build around it — all four statements — and the reasoning behind the agent topology, the deterministic/LLM boundary, how failures are caught, and how an auditor traces a number back to its source.

---

## 1. What "generate the four statements" actually is

Before drawing boxes I had to make myself write out the sub-problems, because the brief is clear that the interesting part is noticing that this isn't one problem, it's six or seven stapled together. They don't have the same reliability requirements, so they don't want the same treatment.

1. **TB ingest + dedupe + FX translation.** Pure arithmetic. If you let a model near it you deserve what you get.
2. **TB → COA mapping.** Mostly dictionary lookup, but the hard 5–10% (orphans, typos, finance shorthand) is where the LLM actually earns rent.
3. **Manual adjustment validation.** The `DR = CR` check is arithmetic. But explaining *why* JE-002 was rejected in a way a controller can act on — that's writing, not accounting.
4. **Statement arithmetic (BS, P&L).** Deterministic. Non-negotiable. No LLM goes anywhere near a rollup.
5. **Cash Flow classification.** Partly deterministic (follow `cf_category`), partly judgment (what do you do with `TBD`? what about reclassifications?).
6. **SOCIE walk and prior-period restatements.** Mostly deterministic once (4) is right. The narrative for restatements is the LLM piece.
7. **Final tie-out.** `Σ(DR) = Σ(CR)`, `A = L + E`, cash reconciles. Deterministic. If any check fails the system must refuse to emit — not "mostly emit with a warning."

A single-prompt pipeline flattens all seven into one unreliable generation step. The whole point of agentic design here is the opposite — split, assign each piece the right tool, and make the boring parts boring.

## 2. Agent topology

**Orchestrator with specialist sub-agents and a hard deterministic/LLM boundary.** Not a 12-agent swarm (the brief's explicit red flag), not a single god-agent with 40 tools (loses traceability the moment anything goes wrong).

```
                 ┌─────────────────────────────────┐
                 │     Orchestrator (plain code)   │
                 │  idempotent run_id              │
                 │  dependency graph of steps      │
                 │  resumable from checkpoint      │
                 └──────────────┬──────────────────┘
                                │
     ┌──────────┬───────────────┼────────────┬───────────────┬──────────┐
     ▼          ▼               ▼            ▼               ▼          ▼
  Ingest    COA Mapper     Adjustment    Statement       Cash-Flow    SOCIE /
  & FX     (det.+LLM)      Validator     Builder         Classifier   Narrative
  (det.)                   (det.+LLM)    (det.)          (det.+LLM)   (LLM-light)
                                │                             │
                                ▼                             ▼
                            Verifier                       Verifier
                            (det.)                         (det.)
```

A few things I want to call out:

- **The orchestrator is code.** There's no reasoning to be done at the control-flow level; it's a DAG. Making the orchestrator an LLM is the mistake that turns a system into a pile of retries.
- **Every LLM step has a deterministic verifier downstream.** The verifier is cheap, boring, and never wrong about accounting identities. That asymmetry is the whole design.
- **Sub-agents own one responsibility each.** When something goes wrong in prod, I want to be able to say "the mapper did X" or "the validator did Y", not "something upstream". Narrow responsibility is a debugging feature.

## 3. Deterministic vs LLM — where does the AI earn its keep?

The brief asks this directly. Here's my boundary:

| Step | Who does it | Reasoning |
|---|---|---|
| TB ingest, dedupe, FX translation | **Code** | Arithmetic. Period. |
| Exact COA code lookup | **Code** | ~90% of TB rows. It's a dictionary. |
| Mapping orphans / ambiguous codes | **LLM** | Short, noisy labels like `"Conf travel"` or `"Sundry Operating Expenses"`. Lexical retrieval narrows the field, semantic judgment picks the node. |
| Adjustment `DR = CR` check | **Code** | Arithmetic. |
| Explaining *why* an adjustment was rejected | **LLM** | "Debit ≠ credit" is useless to a preparer. "JE-002 is short $3,500 — the T&E reversal doesn't match the marketing reclass. Did you mean to reduce T&E as well?" is useful. |
| BS / P&L rollups | **Code** | The numbers have to tie. |
| Cash-flow classification of `TBD` nodes | **LLM** | Judgment — is `2170 Intercompany Payable` operating or financing? Policy question. |
| SOCIE narrative (restatements) | **LLM** | Generation, not calculation. |
| Final tie-out (`DR=CR`, `A=L+E`, cash) | **Code** | Non-negotiable. |

The rule I keep repeating to myself: **LLM for semantic ambiguity and language generation, never for arithmetic and never for the final tie-out.** If I ever find an LLM on the hot path of a statement number, that's a bug.

## 4. Failure modes

The brief names five that'll kill you in production. I thought through each one:

### 4.1 Hallucinated account mappings

Three layers so I can sleep at night:

1. The LLM only sees the top-k candidates the retriever surfaces. It never sees the full COA. It can't propose something it wasn't shown.
2. The mapper's post-processing refuses any `chosen_code` that isn't in the candidate list it was given. If the LLM invents one anyway (it sometimes does), we drop the choice and escalate the row. There's a test for this exact case.
3. The verifier re-checks the chosen node against the COA dictionary. Unknown codes block. Header-level targets block.

Three layers is probably one more than strictly needed, but I'd rather be redundant on a failure mode that silently moves revenue to the wrong statement.

### 4.2 Debits ≠ credits after adjustments

Two places to catch this:

- **Pre-posting:** the adjustment validator rejects any entry where `Σ debits ≠ Σ credits`, with a plain-English explanation of what's missing.
- **Post-posting:** after everything accepted has been applied, the statement builder recomputes `Σ(DR) − Σ(CR)` on the post-adjustment TB. Any nonzero delta blocks statement emission. It does not warn. It refuses.

### 4.3 An account that fits no COA node

In the prototype, the mapper returns `chosen_code = null` with a rationale, and the router escalates. Nothing is silently stuffed into a suspense account or quietly dropped. In production I'd give this a proper HITL queue with a UI that offers two choices — map to an existing node, or create a new one — and treat the reviewer's decision as training signal for a reranker.

### 4.4 FX gaps

`fx_rates.csv` is missing `GBP period_end`. I noticed this on ingest. The right behaviour is: any GBP-denominated **balance sheet** line becomes unresolvable, and BS emission is blocked *for that currency*. P&L (which uses period-average, which is present) can still generate. FX-gap findings always block — a missing rate is not a warning, it's a hole in the data.

### 4.5 Circular intercompany entries

JE-008 posts DR and CR to the same account (`2170`). It's a no-op in P&L / BS impact but the entry exists for a reason — audit workbooks sometimes need a journal trail for an economically neutral event. So my rule is: **warn, don't block.** The validator flags it as `CIRCULAR_IC` and asks the preparer to confirm the entry is for trail purposes, not posting purposes. Blocking here would frustrate users; silently accepting would hide real errors.

## 5. Validation and self-correction

Every agent decision runs through the same shape:

```
   Propose (LLM) ──► Verifier (code) ──► Route
                                          │
         ┌────────────────────────────────┼────────────────────────────────┐
         ▼                                ▼                                ▼
       AUTO                           PROPOSE                          ESCALATE
   (write through)                  (sign-off)                        (HITL queue)
```

On a block, the orchestrator retries **once** with a tighter prompt that names the specific finding ("your previous proposal picked a header; pick a leaf"). If the retry also blocks, the row escalates. No infinite correction loops — the brief rewards knowing when to stop and ask a person, and I agree with that framing.

For the prototype mapper, the verifier asks five questions:

1. Did the mapper only pick from the candidate list? (Guardrail in `mapper.py`.)
2. Does the code exist in the COA? (`UNKNOWN_CODE` → block.)
3. Is the target a leaf posting account? (`HEADER_TARGET` → block.)
4. Does the normal balance match the TB side? (Warn on mismatch — contra accounts are legitimate, so I don't block.)
5. Is `cf_category` set? (Warn if `TBD`.)

A warning downgrades `auto` to `propose`. A block always escalates. This is the point where "but the model said 0.95" stops being the final word.

## 6. Auditor traceability

An auditor should be able to click any cell on the Balance Sheet and work back to the source rows that produced it. My prototype's `audit_log.jsonl` is the proof of concept.

The trace path:

```
BS cell
   └── sum of mapped_to == <account_code>
         └── for each contributing TB row, find its (run_id, account_code) in audit_log.jsonl
               └── read the record:
                     tb_row.source_csv_rows      → raw trial_balance.csv line numbers
                     retrieved_candidates        → what the mapper considered
                     proposal.rationale          → why this one
                     findings                    → what the verifier said
                     decision + decision_reason  → why auto/propose/escalate
                     chosen_coa                  → the COA node metadata used
```

`audit_log.jsonl` is append-only per run. Re-runs get a new `run_id`; I never mutate a prior run's log. `mappings.json` and `review_queue.json` are overwritten each run — the audit log is the source of truth, and it diffs cleanly across runs so you can see what changed.

For manual adjustments and statement cells the same pattern holds: every statement cell carries a `contributors` list of `(source, id, amount)` where `source ∈ {TB, ADJUSTMENT, FX_REVAL, ELIMINATION}`. The adjustment store references the original `manual_adjustments.json` entry by `id`. That's the data model that lets drill-through actually work in a UI.

## 7. The prototype slice — TB → COA Mapper

Code in [src/](../src/). It's a clean implementation of the "COA Mapper" box from §2.

```
     trial_balance.csv ──┐
                         ▼
     chart_of_accounts.csv
            │
            ▼
    ┌───────────────┐   exact match    ┌──────────┐    ┌────────┐    ┌───────────┐
    │   ingest      │─────────────────►│ verifier │───►│ router │───►│   audit   │
    │ (dedupe,      │                  └──────────┘    └────────┘    │ mappings  │
    │  classify)    │                                                │ queue     │
    └───────┬───────┘                                                │ audit_log │
            │  orphan or ambiguous                                   └───────────┘
            ▼
    ┌────────────┐   top-k    ┌──────────┐   proposal   ┌──────────┐
    │  retrieve  │───────────►│  mapper  │─────────────►│ verifier │──► router + audit
    │  (TF-IDF)  │            │ (LLM +   │              │          │
    │            │            │ fallback)│              │          │
    └────────────┘            └──────────┘              └──────────┘
```

**Why this slice.** It's the single place in the full system where the LLM both (a) actually earns its keep — lexical similarity alone cannot disambiguate `9999 Suspense - Unmapped` — and (b) is bounded on all sides: retrieval limits what it sees, the guardrail limits what it can pick, the verifier re-checks what it picked, and the router decides whether to trust the result. If I can get this slice right, the pattern replicates to every other LLM step in the system.

**What the slice demonstrates against the rubric:**

| Rubric signal | Where in the slice |
|---|---|
| Problem decomposition | Hard separation: ingest / retrieve / propose / verify / route / audit |
| Agentic judgment ("when NOT to use an LLM") | 60 of 61 TB rows don't touch the LLM — exact lookup handles them |
| Production thinking | Audit log, idempotent run_ids, HITL escalation, offline fallback, CSV row traceability |
| Domain humility | Contra-account is a warn not a block; TBD cf_category is a warn; questions in §9 |
| AI tool fluency | Tool-use with JSON schema, hard guardrail, confidence routing by threshold |

**What I deliberately didn't build** (would be in a production version):

- Embeddings over character n-grams. TF-IDF is fine at 73 accounts; it won't be fine at 10k.
- Active-learning loop — HITL decisions as training signal for a small reranker.
- Multi-tenant COA versioning.
- Real intercompany-pair detection across entities. The slice is single-entity.

## 8. Defects I noticed in the mock inputs

Over and above what the brief pre-announced:

- **The TB does not balance.** `Σ(DR) − Σ(CR) ≈ +$12,400`, which is exactly the `9999 Suspense - Unmapped` amount. In other words, the TB balances *after* the suspense account is reclassed. This is realistic — NetSuite exports often ship with suspense balances.
- **Account renamed between periods.** `prior_period_tb.csv` uses `6905 Sundry Operating Expenses`; the current period uses `6900 Other Operating Expenses`. This is the "one account renamed" hint. The opening-balance walk needs a COA-remap table.
- **Missing FX rate:** no `GBP period_end` (the brief warned about this).
- **No EUR period_average** for `2024-Q3` or earlier — opening translation for comparatives will either fail cleanly or need to fall back to `rate_type=opening`.
- `manual_adjustments.json`:
  - **JE-002:** DR 28,500, CR 25,000 — short $3,500. **Block.**
  - **JE-005:** posts to `6315`, which isn't in the COA. **Block** until mapped.
  - **JE-008:** DR and CR on `2170`. **Warn** as `CIRCULAR_IC`.
- COA has `cf_category=TBD` on `1150` and `2170`. **Blocks cash-flow emission** until resolved; BS and P&L can still run.
- `1290 Other Assets` is a header with no children — unreachable for postings but harmless.

## 9. Clarifying questions I would email before starting

The brief allows up to three. These are the three I'd send:

1. **Suspense treatment.** Is `9999 Suspense - Unmapped` meant to be reclassed by the mapping agent, or does your platform intentionally keep suspense as its own BS line until operations resolves it? The design differs materially.
2. **Contra-account convention.** When a posting account's TB side disagrees with its COA normal balance (gain booked against a Debit-normal account, for instance), do you block or warn in your current product? I defaulted to warn, but strict blocking is defensible.
3. **Prior-period restatements vs comparatives.** For the `6905 → 6900` rename, should the system restate prior-period comparatives automatically, or always escalate? This is a materiality call that depends on the audit framework you're targeting.

## 10. Scale and extension

See [REFLECTION.md](REFLECTION.md) for the three-month roadmap and where the prototype would break at scale.
