"""Thresholds, paths, and model/provider configuration for the TB -> COA mapper.

Settings are read from the environment. If a `.env` file exists at the project
root it is loaded first (python-dotenv). The provider abstraction lives in
src/providers.py; this module just surfaces the two values that decide which
provider+model the mapper will try.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
INPUTS = ROOT / "inputs"
OUTPUT = ROOT / "output"

# Load .env once, as early as possible. Values already in the process env win.
load_dotenv(ROOT / ".env", override=False)

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
    """Provider + model id the mapper will use when an LLM path is taken.

    `provider` picks the client; `model_id` is optional — each provider has a
    sensible default (see providers.DEFAULT_MODELS). The resolved model id is
    computed lazily inside the provider so the user can override just one.
    """
    provider: str = os.environ.get("MODEL_PROVIDER", "anthropic").strip().lower()
    model_id: str = os.environ.get("MODEL_NAME", "").strip()
    max_tokens: int = 1024
    temperature: float = 0.0  # mapping decisions should be reproducible


THRESHOLDS = Thresholds()
MODEL = ModelConfig()

# Top-k COA candidates retrieved before the LLM is asked to decide.
# Keep small to bound prompt cost and to force the LLM to reject when nothing fits.
TOPK_CANDIDATES = 5
