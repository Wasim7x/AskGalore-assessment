# AI Agentic Engineer — Slice 1: TB → COA Mapper

Prototype for the AskGalore AI Agentic Engineer take-home. Implements **one** of the four slices listed in the brief: a trial-balance → chart-of-accounts mapper that handles unmapped and ambiguous accounts with confidence scoring, deterministic verification, and human-in-the-loop escalation.

The full architecture (topology, failure handling, audit model) lives in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The reflection lives in [docs/REFLECTION.md](docs/REFLECTION.md).

## Why this slice

The brief says: *"Generating a balance sheet from a clean TB is arithmetic — where does the AI actually earn its keep?"*

The answer is **account mapping**. Exact `account_code` matches are deterministic and need no LLM. The LLM only touches the genuinely ambiguous slice: codes that aren't in the COA at all (orphans like `9999 Suspense - Unmapped`), typos, or finance-team shorthand (`6315 Conf travel`). That's where accounting judgment is required and where a model can save hours of reviewer time — if, and only if, its output is verified and traced.

## Pipeline

```
trial_balance.csv ┐
                  ├── ingest (dedupe, classify) ──┐
chart_of_accounts.csv ──────────────────────────┤
                                                ├── exact match? ──yes──► verify ──► route ──► audit
                                                └── no ──► retrieve top-k ──► LLM map ──► verify ──► route ──► audit
```

Five modules, ~550 lines of Python, zero LLM calls for 60 of 61 TB rows.

| File | Responsibility |
|---|---|
| [src/ingest.py](src/ingest.py) | Parse TB + COA, deduplicate `(code, currency)` rows, flag orphans and ambiguous COA nodes. No LLM. |
| [src/retrieve.py](src/retrieve.py) | TF-IDF over character n-grams on COA names. Returns top-k candidates for the mapper. |
| [src/mapper.py](src/mapper.py) | Anthropic tool-use with a hard guardrail: LLM may only pick codes from the retrieved candidate list, or return `null`. Offline heuristic fallback caps confidence below the propose threshold. |
| [src/verifier.py](src/verifier.py) | Deterministic cross-checks: header target? known code? normal balance match? `cf_category=TBD`? |
| [src/router.py](src/router.py) | Threshold routing: `auto` / `propose` / `escalate`. Warnings downgrade, blocks escalate. |
| [src/audit.py](src/audit.py) | Writes `mappings.json`, `review_queue.json`, `audit_log.jsonl`. Every decision includes retrieved candidates, findings, and source CSV row numbers. |

## Setup

```bash
pip install -r requirements.txt
```

Python 3.10+ recommended. On Windows PowerShell, set `PYTHONIOENCODING=utf-8` before running (the audit log contains em-dashes).

## Run

**Offline (heuristic-only, no API key needed):**

```bash
python -m src.cli --no-llm
```

**With an LLM provider:**

Copy `.env.example` → `.env`, set `MODEL_PROVIDER` to one of `anthropic | openai | groq | gemini | ollama`, and fill in the matching key. Then:

```bash
python -m src.cli
```

Per-command override (bypasses `.env`):

```bash
MODEL_PROVIDER=groq      python -m src.cli
MODEL_PROVIDER=openai    python -m src.cli
MODEL_PROVIDER=gemini    python -m src.cli
MODEL_PROVIDER=ollama    python -m src.cli   # requires `ollama serve` running
MODEL_PROVIDER=anthropic python -m src.cli
```

If the chosen provider is unreachable (missing key, no network, bad model id) the pipeline degrades to the heuristic and records the provider's error in each affected rationale — so the audit log always shows what was attempted.

**Web UI (Streamlit):**

```bash
streamlit run src/webapp.py
```

Opens a local app (default `http://localhost:8501`) with:
- a sidebar for provider / model / API key (overrides `.env` for the session only, nothing is persisted)
- uploaders for your own `trial_balance.csv` and `chart_of_accounts.csv`, or a checkbox to use the bundled messy inputs
- live pre-flight triage + decision summary after each run
- download buttons for `mappings.json`, `review_queue.json`, `audit_log.jsonl`
- collapsible previews of each artifact

## What the run produces

Three artifacts in `output/`:

| File | Purpose |
|---|---|
| `mappings.json` | The decision the downstream statement generator would consume. One row per TB entry with `mapped_to`, `decision`, `confidence`, `source`. |
| `review_queue.json` | Only the items a human reviewer must action (`propose` or `escalate`). Each includes rationale, alternatives, findings, and source CSV row numbers. |
| `audit_log.jsonl` | Append-only. One JSON object per decision with the TB row, retrieved candidates, verifier findings, chosen COA node, decision, and reason. This is the trace an auditor follows. |

## What the pipeline found in the messy inputs

Running `python -m src.cli --no-llm` on the bundled mock data produces:

```
TB rows after dedupe              61
Duplicate (code, ccy) merges       1     # 6310 Travel and Entertainment, lines 48+49
Distinct orphan TB codes           1     # 9999 Suspense - Unmapped
COA accounts with cf_category=TBD  2     # 1150 Other Current Assets, 2170 Intercompany Payable

Decision summary
  auto     56     # confident exact matches
  propose   4     # exact matches with verifier warnings
  escalate  1     # 9999 — heuristic can't confidently map; needs human
```

The 4 `propose` items are not low-confidence mappings — they're exact matches the verifier flagged because the downstream Cash Flow Statement cannot be generated cleanly (`1150`, `2170` have `cf_category=TBD`) or the TB sign disagrees with the COA's normal balance in a way that suggests a contra-account posting (`7310 FX Gain/Loss - Unrealized`, `7400 Gain/Loss on Disposal` — both booked as gains against Debit-normal accounts). This is the verifier earning its keep — catching accounting issues an auto-accept would silently paper over.

Additional messy-input issues observed but out of scope for this slice (flagged in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)):
- TB totals don't balance (DR − CR ≈ $12,400, which is the exact `9999 Suspense` figure).
- `fx_rates.csv` is missing `GBP period_end`.
- `manual_adjustments.json` has JE-002 unbalanced, JE-005 posting to the non-existent `6315`, JE-008 circular on `2170`.
- `prior_period_tb.csv` uses `6905 Sundry Operating Expenses` where the current period uses `6900 Other Operating Expenses` (the "one account renamed" hint in the brief).

## Tests

```bash
python -m pytest tests/ -q
```

Seven tests cover: dedupe correctness, orphan detection, verifier blocks on hallucinated codes and header targets, the warning-downgrade rule, null-proposal escalation, retriever header-filtering, and — critically — the guardrail that rejects any code an LLM invents outside the candidate list.

## How I used AI tools

Full note in [docs/REFLECTION.md](docs/REFLECTION.md). Short version: I used Claude to pressure-test the deterministic/LLM boundary (where exactly does the LLM add value here), to draft the verifier finding codes, and to write this README. I did not let it design the agent topology — that stays with the human engineer.

## Project layout

```
.
├── inputs/              # untouched messy mock data from the brief
├── src/                 # pipeline modules (see table above)
├── tests/               # pytest suite
├── output/              # generated artifacts (mappings, queue, audit log)
├── docs/
│   ├── ARCHITECTURE.md  # main evaluation artifact
│   └── REFLECTION.md    # one-page reflection
├── requirements.txt
└── README.md
```
