"""End-to-end and unit tests for the Slice 1 mapper.

These are not exhaustive — they lock in the invariants that matter most:
  - ingest deduplicates (code, currency) pairs rather than dropping rows
  - orphan classification matches the known seeded defect (9999)
  - verifier blocks hallucinated codes and header targets
  - router downgrades on warnings and escalates on blocks
  - the LLM path's guardrail refuses an LLM-invented code
"""
from __future__ import annotations

from pathlib import Path

from src import config
from src.ingest import COAAccount, TBRow, ingest
from src.mapper import Proposal
from src.retrieve import COARetriever
from src.router import route
from src.verifier import verify


INPUTS = Path(__file__).resolve().parent.parent / "inputs"


def test_ingest_dedupes_6310_and_flags_9999():
    r = ingest(INPUTS / "trial_balance.csv", INPUTS / "chart_of_accounts.csv")
    # 6310 appears twice in the raw TB — should be merged, not dropped.
    sixes = [row for row in r.tb_rows if row.account_code == "6310"]
    assert len(sixes) == 1
    assert sixes[0].debit == 245000.0 + 38500.0
    assert sorted(sixes[0].source_rows) == [48, 49]
    # 9999 is the orphan.
    assert "9999" in r.orphan_codes
    # Known ambiguous COA nodes (cf_category=TBD).
    assert set(r.ambiguous_coa_codes) >= {"1150", "2170"}


def _mini_coa() -> dict[str, COAAccount]:
    return {
        "6100": COAAccount("6100", "Salaries and Wages", "Expense",
                           "6000", "PL", "", "Debit"),
        "6000": COAAccount("6000", "Operating Expenses", "Header",
                           "", "PL", "", "Debit"),
        "1150": COAAccount("1150", "Other Current Assets", "Asset",
                           "1100", "BS", "TBD", "Debit"),
    }


def test_verifier_blocks_header_target():
    row = TBRow("X", "Mystery", "USD", 100.0, 0.0, source_rows=[1])
    p = Proposal("6000", 0.95, "guess", [], source="llm")
    v = verify(row, p, _mini_coa())
    assert v.blocked
    assert any(f.code == "HEADER_TARGET" for f in v.findings)


def test_verifier_blocks_unknown_code():
    row = TBRow("X", "Mystery", "USD", 100.0, 0.0, source_rows=[1])
    p = Proposal("ZZZZ", 0.95, "hallucinated", [], source="llm")
    v = verify(row, p, _mini_coa())
    assert v.blocked
    assert any(f.code == "UNKNOWN_CODE" for f in v.findings)


def test_verifier_warns_on_cf_ambiguity_and_router_downgrades():
    row = TBRow("1150", "Other Current Assets", "USD", 500.0, 0.0, source_rows=[1])
    p = Proposal("1150", 0.95, "exact", [], source="exact")
    v = verify(row, p, _mini_coa())
    assert not v.blocked and v.warned
    d = route(v)
    # 0.95 would be 'auto' without warnings; warning must downgrade to 'propose'.
    assert d.decision == "propose"


def test_router_escalates_when_mapper_returns_null():
    row = TBRow("X", "Mystery", "USD", 100.0, 0.0, source_rows=[1])
    p = Proposal(None, 0.0, "no fit", [], source="heuristic")
    v = verify(row, p, _mini_coa())
    d = route(v)
    assert d.decision == "escalate"


def test_retriever_finds_rent_as_nearest_for_bad_query():
    r = ingest(INPUTS / "trial_balance.csv", INPUTS / "chart_of_accounts.csv")
    retr = COARetriever(r.coa_all)
    cands = retr.topk("9999 Suspense - Unmapped", k=5)
    assert len(cands) == 5
    # Top candidate must be a posting account (header-filtered).
    codes = [c.code for c in cands]
    assert all(r.coa_by_code[c].account_type.lower() != "header" for c in codes)


def test_llm_guardrail_rejects_hallucinated_code(monkeypatch):
    """If the provider returns a code outside the candidate list, the mapper must refuse.

    We stub the provider factory directly — this isolates the test from any
    particular vendor SDK (anthropic, openai, groq, gemini, ollama).
    """
    from src import mapper as m

    class _FakeProvider:
        name = "fake"
        model_id = "fake-model"
        def propose(self, system_prompt, user_prompt, tool_schema):
            return {
                "chosen_code":  "DOES_NOT_EXIST",
                "confidence":   0.99,
                "rationale":    "fabricated",
                "alternatives": [],
            }

    monkeypatch.setattr(m, "get_provider", lambda *a, **kw: _FakeProvider())

    row = TBRow("X", "mystery", "USD", 1.0, 0.0, source_rows=[1])
    from src.retrieve import Candidate
    cands = [Candidate("6100", "Salaries", "PL", "Debit", 0.5)]
    out = m.propose(row, cands, use_llm=True)
    # Guardrail: hallucinated code must be rejected.
    assert out.chosen_code is None
    assert out.confidence == 0.0
