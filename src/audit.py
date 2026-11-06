"""Audit trail writers.

Three artifacts land on disk every run:
  output/mappings.json      : the final decision per TB row (what gets used downstream)
  output/review_queue.json  : items the finance team must action
  output/audit_log.jsonl    : append-only, one JSON object per decision + evidence

The JSONL log is the primary audit artifact. Given a cell on a statement, an
auditor can follow: statement cell -> account_code -> audit_log entry ->
source CSV rows (line numbers) and the full evidence used by the mapper.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .mapper import proposal_to_dict
from .retrieve import Candidate
from .router import RoutedDecision
from .verifier import finding_to_dict


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_audit_record(
    decision: RoutedDecision,
    candidates: list[Candidate],
    stage: str,
    run_id: str,
) -> dict:
    v = decision.verified
    return {
        "ts": _now_iso(),
        "run_id": run_id,
        "stage": stage,  # 'exact' or 'agent'
        "tb_row": {
            "account_code": v.row.account_code,
            "account_name": v.row.account_name,
            "currency": v.row.currency,
            "debit": v.row.debit,
            "credit": v.row.credit,
            "source_csv_rows": v.row.source_rows,
        },
        "retrieved_candidates": [
            {
                "code": c.code,
                "name": c.name,
                "statement": c.statement,
                "normal_balance": c.normal_balance,
                "similarity": round(c.similarity, 4),
            }
            for c in candidates
        ],
        "proposal": proposal_to_dict(v.proposal),
        "findings": [finding_to_dict(f) for f in v.findings],
        "chosen_coa": (
            {
                "code": v.chosen_coa.code,
                "name": v.chosen_coa.name,
                "statement": v.chosen_coa.statement,
                "cf_category": v.chosen_coa.cf_category,
                "normal_balance": v.chosen_coa.normal_balance,
            }
            if v.chosen_coa else None
        ),
        "decision": decision.decision,
        "decision_reason": decision.reason,
    }


def write_outputs(
    records: list[dict],
    output_dir: Path,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Append-only JSONL audit log
    audit_path = output_dir / "audit_log.jsonl"
    with audit_path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # 2. Final mappings (what the next stage of the pipeline would consume)
    mappings_path = output_dir / "mappings.json"
    mappings = [
        {
            "account_code": r["tb_row"]["account_code"],
            "account_name": r["tb_row"]["account_name"],
            "currency": r["tb_row"]["currency"],
            "debit": r["tb_row"]["debit"],
            "credit": r["tb_row"]["credit"],
            "mapped_to": r["chosen_coa"]["code"] if r["chosen_coa"] else None,
            "statement": r["chosen_coa"]["statement"] if r["chosen_coa"] else None,
            "decision": r["decision"],
            "confidence": r["proposal"]["confidence"],
            "source": r["proposal"]["source"],
        }
        for r in records
    ]
    with mappings_path.open("w", encoding="utf-8") as fh:
        json.dump(mappings, fh, indent=2)

    # 3. Review queue — only the items a human must touch
    queue_path = output_dir / "review_queue.json"
    queue = [
        {
            "account_code": r["tb_row"]["account_code"],
            "account_name": r["tb_row"]["account_name"],
            "decision": r["decision"],
            "reason": r["decision_reason"],
            "proposed_code": r["proposal"]["chosen_code"],
            "proposed_name": (r["chosen_coa"]["name"] if r["chosen_coa"] else None),
            "confidence": r["proposal"]["confidence"],
            "rationale": r["proposal"]["rationale"],
            "alternatives": r["proposal"]["alternatives"],
            "findings": r["findings"],
            "source_csv_rows": r["tb_row"]["source_csv_rows"],
        }
        for r in records
        if r["decision"] in ("propose", "escalate")
    ]
    with queue_path.open("w", encoding="utf-8") as fh:
        json.dump(queue, fh, indent=2)

    return {"audit": audit_path, "mappings": mappings_path, "queue": queue_path}
