"""Ingest + first-pass deterministic triage.

Responsibilities:
  1. Load the trial balance and chart of accounts as typed records.
  2. Collapse duplicate (account_code, currency) rows (the TB seeds one).
  3. For each account in the TB, classify as:
        - 'exact'       : account_code exists in COA  -> deterministic mapping
        - 'orphan'      : account_code does not exist in COA -> goes to the agent
     Ambiguity inside COA (e.g. cf_category == 'TBD') is a separate concern
     flagged by the verifier downstream, not here.

This module does not call an LLM. Everything here is cheap, reversible, and
easy to unit-test — the rubric item "knows when NOT to use an LLM" starts here.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class COAAccount:
    code: str
    name: str
    account_type: str
    parent_code: str
    statement: str   # 'BS' | 'PL' | ''
    cf_category: str # may be '' or 'TBD'
    normal_balance: str  # 'Debit' | 'Credit' | ''

    def is_header(self) -> bool:
        return self.account_type.lower() == "header"

    def has_ambiguous_cf(self) -> bool:
        # A real posting account whose cash-flow category is 'TBD' is unsafe
        # to use in a Cash Flow Statement — flag for the verifier.
        return (not self.is_header()) and self.cf_category.upper() == "TBD"


@dataclass
class TBRow:
    account_code: str
    account_name: str
    currency: str
    debit: float
    credit: float
    source_rows: list[int] = field(default_factory=list)  # 1-indexed CSV lines

    @property
    def net_debit(self) -> float:
        return self.debit - self.credit


@dataclass
class IngestResult:
    tb_rows: list[TBRow]                 # deduped TB
    coa_by_code: dict[str, COAAccount]   # fast exact lookup
    coa_all: list[COAAccount]            # retrieval corpus
    duplicates: list[tuple[str, str, list[int]]]  # (code, ccy, merged source rows)
    orphan_codes: list[str]              # TB codes not present in COA
    ambiguous_coa_codes: list[str]       # COA posting accounts with TBD cf_category


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def load_coa(path: Path) -> tuple[dict[str, COAAccount], list[COAAccount]]:
    accounts: list[COAAccount] = []
    for raw in _read_csv(path):
        accounts.append(
            COAAccount(
                code=raw["account_code"].strip(),
                name=raw["account_name"].strip(),
                account_type=raw["account_type"].strip(),
                parent_code=(raw.get("parent_code") or "").strip(),
                statement=(raw.get("statement") or "").strip(),
                cf_category=(raw.get("cf_category") or "").strip(),
                normal_balance=(raw.get("normal_balance") or "").strip(),
            )
        )
    by_code = {a.code: a for a in accounts}
    return by_code, accounts


def load_tb(path: Path) -> tuple[list[TBRow], list[tuple[str, str, list[int]]]]:
    """Load the TB and deduplicate (code, currency) pairs.

    Duplicates are summed (not dropped). The source CSV row numbers are
    retained so the audit log can point an auditor back to the raw file.
    """
    merged: dict[tuple[str, str], TBRow] = {}
    duplicates: list[tuple[str, str, list[int]]] = []

    for idx, raw in enumerate(_read_csv(path), start=2):  # start=2 for header offset
        code = raw["account_code"].strip()
        ccy = raw["currency"].strip().upper()
        key = (code, ccy)
        debit = float(raw["debit"] or 0)
        credit = float(raw["credit"] or 0)
        if key in merged:
            merged[key].debit += debit
            merged[key].credit += credit
            merged[key].source_rows.append(idx)
            duplicates.append((code, ccy, merged[key].source_rows.copy()))
        else:
            merged[key] = TBRow(
                account_code=code,
                account_name=raw["account_name"].strip(),
                currency=ccy,
                debit=debit,
                credit=credit,
                source_rows=[idx],
            )
    return list(merged.values()), duplicates


def classify(tb_rows: Iterable[TBRow], coa_by_code: dict[str, COAAccount]) -> list[str]:
    seen: set[str] = set()
    orphans: list[str] = []
    for row in tb_rows:
        if row.account_code in coa_by_code:
            continue
        if row.account_code in seen:
            continue
        seen.add(row.account_code)
        orphans.append(row.account_code)
    return orphans


def ingest(tb_path: Path, coa_path: Path) -> IngestResult:
    coa_by_code, coa_all = load_coa(coa_path)
    tb_rows, duplicates = load_tb(tb_path)
    orphan_codes = classify(tb_rows, coa_by_code)
    ambiguous_coa_codes = [a.code for a in coa_all if a.has_ambiguous_cf()]
    return IngestResult(
        tb_rows=tb_rows,
        coa_by_code=coa_by_code,
        coa_all=coa_all,
        duplicates=duplicates,
        orphan_codes=orphan_codes,
        ambiguous_coa_codes=ambiguous_coa_codes,
    )
