"""Thresholds, paths, and model configuration for the TB -> COA mapper."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUTS = ROOT / "inputs"
OUTPUT = ROOT / "output"

TB_PATH = INPUTS / "trial_balance.csv"
COA_PATH = INPUTS / "chart_of_accounts.csv"
ADJ_PATH = INPUTS / "manual_adjustments.json"
FX_PATH = INPUTS / "fx_rates.csv"
PRIOR_PATH = INPUTS / "prior_period_tb.csv"


@dataclass(frozen=True)
class Thresholds:
    # Confidence >= auto_accept  -> record as 'auto'
    # propose_min <= conf < auto -> record as 'propose' (needs reviewer sign-off)
    # conf < propose_min         -> record as 'escalate' (HITL queue)
    auto_accept: float = 0.90
    propose_min: float = 0.60


@dataclass(frozen=True)
class ModelConfig:
    # Claude is used only for the ambiguous slice. Default to a fast model;
    # the pipeline also runs fully offline via the retrieve-only fallback.
    model_id: str = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
    max_tokens: int = 1024
    temperature: float = 0.0  # mapping decisions should be reproducible


THRESHOLDS = Thresholds()
MODEL = ModelConfig()

# Top-k COA candidates retrieved before the LLM is asked to decide.
# Keep small to bound prompt cost and to force the LLM to reject when nothing fits.
TOPK_CANDIDATES = 5
