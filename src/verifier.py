"""Verifier agent: deterministic cross-checks on every mapping proposal.

The mapper is the only component allowed to pick a code. The verifier does NOT
override that choice — it attaches structured findings. The router then uses
the findings to decide whether the proposal can be auto-accepted or must be
escalated regardless of the mapper's stated confidence.

Separating propose and verify is the single most important pattern here:
  - the LLM can be wrong,
  - the checks are cheap, boring, and never wrong about accounting identities,
  - so we always run the checks after any LLM decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .ingest import COAAccount, TBRow
from .mapper import Proposal


@dataclass(frozen=True)
class Finding:
    code: str        # short machine-readable id
    severity: str    # 'info' | 'warn' | 'block'
    message: str     # human-readable


@dataclass
class VerifiedProposal:
    row: TBRow
    proposal: Proposal
    chosen_coa: Optional[COAAccount]
    findings: list[Finding]

    @property
    def blocked(self) -> bool:
        return any(f.severity == "block" for f in self.findings)

    @property
    def warned(self) -> bool:
        return any(f.severity == "warn" for f in self.findings)


def _check_code_exists(p: Proposal, coa_by_code: dict[str, COAAccount]) -> list[Finding]:
    if p.chosen_code is None:
        return []
    if p.chosen_code not in coa_by_code:
        return [Finding("UNKNOWN_CODE", "block",
                        f"Proposed code {p.chosen_code} is not in the COA.")]
    return []


def _check_not_header(chosen: Optional[COAAccount]) -> list[Finding]:
    if chosen is None:
        return []
    if chosen.is_header():
        return [Finding("HEADER_TARGET", "block",
                        f"Proposed node {chosen.code} is a header; postings must land on a leaf.")]
    return []


def _check_normal_balance(row: TBRow, chosen: Optional[COAAccount]) -> list[Finding]:
    if chosen is None or not chosen.normal_balance:
        return []
    side = "Debit" if row.net_debit >= 0 else "Credit"
    if chosen.normal_balance != side:
        # Not necessarily wrong (contra-accounts exist) — warn, don't block.
        return [Finding(
            "NORMAL_BALANCE_MISMATCH", "warn",
            f"TB row is net {side} but proposed {chosen.code} has normal_balance="
            f"{chosen.normal_balance}. Could be a contra-account; please confirm.",
        )]
    return []


def _check_cf_category(chosen: Optional[COAAccount]) -> list[Finding]:
    if chosen is None:
        return []
    if chosen.has_ambiguous_cf():
        return [Finding(
            "AMBIGUOUS_CF_CATEGORY", "warn",
            f"COA node {chosen.code} has cf_category=TBD; Cash Flow Statement "
            "generation will be ambiguous until this is set.",
        )]
    return []


def verify(
    row: TBRow,
    proposal: Proposal,
    coa_by_code: dict[str, COAAccount],
) -> VerifiedProposal:
    findings: list[Finding] = []
    findings += _check_code_exists(proposal, coa_by_code)

    chosen = coa_by_code.get(proposal.chosen_code) if proposal.chosen_code else None

    findings += _check_not_header(chosen)
    findings += _check_normal_balance(row, chosen)
    findings += _check_cf_category(chosen)

    return VerifiedProposal(row=row, proposal=proposal, chosen_coa=chosen, findings=findings)


def finding_to_dict(f: Finding) -> dict:
    return {"code": f.code, "severity": f.severity, "message": f.message}
