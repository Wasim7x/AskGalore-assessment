"""LLM mapping agent for orphan TB accounts.

Contract:
  Input : TBRow (orphan — not in COA), list[Candidate] (top-k retrieved COA nodes).
  Output: Proposal(code|None, confidence, rationale, alternatives, source).

Guardrails (deliberate, not defensive paranoia):
  1. The LLM may only choose from the retrieved candidate codes OR return None.
     This closes the "hallucinated account mapping" failure mode the PDF names.
  2. Structured JSON output via Anthropic tool-use — no free-text parsing.
  3. If no API key is present, a transparent heuristic fallback runs so the
     prototype is fully demoable offline (CI, air-gapped reviewers, etc.).
     The fallback never claims high confidence — it caps at propose_min, so
     the router escalates every offline decision to HITL.

What the LLM genuinely earns its keep on:
  - Reading short, noisy account names ("Conf travel", "Sundry Operating
    Expenses", "Suspense - Unmapped") and deciding which COA node they belong
    under given the account type, parent, and normal balance.
  - Returning a calibrated 'no suitable match' when nothing in top-k fits.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

from .config import MODEL, THRESHOLDS
from .ingest import TBRow
from .retrieve import Candidate


@dataclass
class Proposal:
    chosen_code: Optional[str]      # None => reject, no suitable COA node
    confidence: float               # 0..1
    rationale: str                  # one-sentence, auditor-readable
    alternatives: list[str]         # other candidate codes considered
    source: str                     # 'llm' | 'heuristic' | 'exact'


_SYSTEM = """You are a conservative accounting mapper.

Your job: given an orphan trial-balance row (an account code that is NOT in the
chart of accounts) and a short list of candidate COA accounts retrieved by
lexical similarity, pick AT MOST ONE candidate that is the correct parent
account to map the orphan to for statement generation.

Rules you must follow:
- You may only choose a code that appears in the provided candidate list,
  or return null to reject.
- Prefer null over a weak guess. A bad mapping is much worse than an escalation.
- The chosen account's statement (BS vs PL) and normal_balance must be
  consistent with the orphan's observed debit/credit direction.
- Confidence must reflect the quality of fit, not the quality of the candidates.
  If every candidate is mediocre, confidence is low.

You respond ONLY by calling the propose_mapping tool. Never free text."""


_TOOL = {
    "name": "propose_mapping",
    "description": "Return the mapping decision for an orphan TB account.",
    "input_schema": {
        "type": "object",
        "properties": {
            "chosen_code": {
                "type": ["string", "null"],
                "description": "Candidate code to map to, or null to reject.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Calibrated confidence in the chosen code (0 if null).",
            },
            "rationale": {
                "type": "string",
                "description": "One short sentence an auditor can read.",
            },
            "alternatives": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Other candidate codes considered but rejected.",
            },
        },
        "required": ["chosen_code", "confidence", "rationale", "alternatives"],
    },
}


def _build_user_prompt(row: TBRow, candidates: list[Candidate]) -> str:
    side = "DEBIT" if row.net_debit >= 0 else "CREDIT"
    lines = [
        "ORPHAN TB ROW",
        f"  code    : {row.account_code}",
        f"  name    : {row.account_name}",
        f"  currency: {row.currency}",
        f"  debit   : {row.debit:,.2f}",
        f"  credit  : {row.credit:,.2f}",
        f"  net side: {side}",
        "",
        "CANDIDATE COA ACCOUNTS (ranked by lexical similarity):",
    ]
    for i, c in enumerate(candidates, 1):
        lines.append(
            f"  {i}. code={c.code}  name={c.name!r}  "
            f"statement={c.statement}  normal={c.normal_balance}  "
            f"sim={c.similarity:.3f}"
        )
    lines.append("")
    lines.append("Choose at most one candidate code, or null.")
    return "\n".join(lines)


def _llm_propose(row: TBRow, candidates: list[Candidate]) -> Proposal:
    # Local import so the module can be imported in environments without the SDK.
    from anthropic import Anthropic

    client = Anthropic()
    resp = client.messages.create(
        model=MODEL.model_id,
        max_tokens=MODEL.max_tokens,
        temperature=MODEL.temperature,
        system=_SYSTEM,
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "propose_mapping"},
        messages=[{"role": "user", "content": _build_user_prompt(row, candidates)}],
    )
    # Tool-use is forced, so there is exactly one tool_use block.
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "propose_mapping":
            data = block.input
            chosen = data.get("chosen_code")
            # Hard guardrail: reject any code the LLM invented.
            allowed = {c.code for c in candidates}
            if chosen is not None and chosen not in allowed:
                return Proposal(
                    chosen_code=None,
                    confidence=0.0,
                    rationale=(
                        f"LLM proposed code {chosen!r} which is not in the "
                        "retrieved candidate list; rejected to prevent hallucination."
                    ),
                    alternatives=[c.code for c in candidates],
                    source="llm",
                )
            return Proposal(
                chosen_code=chosen,
                confidence=float(data.get("confidence", 0.0)),
                rationale=str(data.get("rationale", "")).strip(),
                alternatives=list(data.get("alternatives", [])),
                source="llm",
            )
    # Model refused to emit the tool call — treat as explicit reject.
    return Proposal(
        chosen_code=None,
        confidence=0.0,
        rationale="LLM returned no structured proposal; escalating.",
        alternatives=[c.code for c in candidates],
        source="llm",
    )


def _heuristic_propose(row: TBRow, candidates: list[Candidate]) -> Proposal:
    """Offline fallback. Uses only the retriever's similarity + sign check.

    Confidence is capped below THRESHOLDS.propose_min, so the router always
    escalates heuristic decisions to HITL. This is intentional: without an LLM
    the system should err toward 'ask a human', never toward auto-accept.
    """
    if not candidates:
        return Proposal(
            chosen_code=None,
            confidence=0.0,
            rationale="No COA candidates retrieved; nothing to propose.",
            alternatives=[],
            source="heuristic",
        )
    top = candidates[0]
    side = "Debit" if row.net_debit >= 0 else "Credit"
    sign_ok = top.normal_balance == side
    # Cap so the router always routes heuristic picks through HITL.
    capped = min(top.similarity, THRESHOLDS.propose_min - 0.01)
    if not sign_ok:
        capped = min(capped, 0.3)
    rationale = (
        f"Heuristic fallback (no LLM): nearest COA node by TF-IDF is "
        f"{top.code} {top.name!r} (sim={top.similarity:.2f}); "
        f"normal_balance match: {sign_ok}."
    )
    return Proposal(
        chosen_code=top.code,
        confidence=capped,
        rationale=rationale,
        alternatives=[c.code for c in candidates[1:]],
        source="heuristic",
    )


def propose(row: TBRow, candidates: list[Candidate], use_llm: bool = True) -> Proposal:
    """Entry point. Picks LLM vs heuristic based on availability + caller intent."""
    if use_llm and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _llm_propose(row, candidates)
        except Exception as exc:  # noqa: BLE001 — network/SDK failures are many
            # Degrade gracefully: offline heuristic + a note the auditor can see.
            fallback = _heuristic_propose(row, candidates)
            return Proposal(
                chosen_code=fallback.chosen_code,
                confidence=fallback.confidence,
                rationale=f"LLM call failed ({type(exc).__name__}); {fallback.rationale}",
                alternatives=fallback.alternatives,
                source="heuristic",
            )
    return _heuristic_propose(row, candidates)


def proposal_to_dict(p: Proposal) -> dict:
    return {
        "chosen_code": p.chosen_code,
        "confidence": round(p.confidence, 4),
        "rationale": p.rationale,
        "alternatives": p.alternatives,
        "source": p.source,
    }


# Kept for completeness — the CLI prints a short banner using these values.
def mapper_banner() -> str:
    mode = "LLM" if os.environ.get("ANTHROPIC_API_KEY") else "HEURISTIC-ONLY"
    return f"mapper={mode} model={MODEL.model_id if mode == 'LLM' else 'n/a'}"


# Re-export for the __main__ pathway below.
__all__ = ["Proposal", "propose", "proposal_to_dict", "mapper_banner"]


# Allow quick manual inspection:  python -m src.mapper
if __name__ == "__main__":  # pragma: no cover
    print(json.dumps({"banner": mapper_banner()}, indent=2))
