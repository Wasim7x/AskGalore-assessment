"""Confidence-based routing of verified proposals.

Decision table:
  blocked by verifier                       -> ESCALATE  (must be reviewed)
  confidence >= auto_accept AND no warnings -> AUTO
  confidence >= propose_min                 -> PROPOSE   (needs sign-off)
  otherwise                                 -> ESCALATE

A warning downgrades AUTO to PROPOSE. A block always escalates.
This is the point where "the LLM said 0.95" stops being the final word.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .config import THRESHOLDS
from .verifier import VerifiedProposal

Decision = Literal["auto", "propose", "escalate"]


@dataclass
class RoutedDecision:
    decision: Decision
    reason: str
    verified: VerifiedProposal


def route(v: VerifiedProposal) -> RoutedDecision:
    if v.blocked:
        return RoutedDecision("escalate", "blocking verifier finding", v)
    if v.proposal.chosen_code is None:
        return RoutedDecision("escalate", "mapper returned no candidate", v)

    conf = v.proposal.confidence
    if conf >= THRESHOLDS.auto_accept and not v.warned:
        return RoutedDecision("auto", f"confidence {conf:.2f} ≥ {THRESHOLDS.auto_accept}", v)
    if conf >= THRESHOLDS.propose_min:
        warn = " with warnings" if v.warned else ""
        return RoutedDecision(
            "propose",
            f"confidence {conf:.2f} ≥ {THRESHOLDS.propose_min}{warn}",
            v,
        )
    return RoutedDecision("escalate", f"confidence {conf:.2f} below {THRESHOLDS.propose_min}", v)
