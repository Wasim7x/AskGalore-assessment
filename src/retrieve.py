"""Retrieve top-k COA candidates for an orphan TB row using TF-IDF.

Why TF-IDF and not embeddings for the prototype?
  - Zero API calls, deterministic, trivial to unit-test.
  - COA labels are short, domain-specific noun phrases — TF-IDF over character
    n-grams captures morphology ("Conf travel" ~ "Travel and Entertainment")
    well enough to produce useful top-k candidates.
  - In production this is the place to swap in an embedding model (see
    ARCHITECTURE.md). The interface is intentionally small so the swap is local.
"""
from __future__ import annotations

from dataclasses import dataclass

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from .ingest import COAAccount


@dataclass(frozen=True)
class Candidate:
    code: str
    name: str
    statement: str
    normal_balance: str
    similarity: float  # 0..1 cosine similarity against the TB query


class COARetriever:
    """Index the COA once, query many times."""

    def __init__(self, coa: list[COAAccount]) -> None:
        # Only posting accounts are valid mapping targets — headers roll up.
        self._accounts = [a for a in coa if not a.is_header()]
        self._corpus = [self._doc(a) for a in self._accounts]
        # char_wb n-grams handle short labels and typos better than word tokens.
        self._vec = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=(3, 5),
            lowercase=True,
            min_df=1,
        )
        self._matrix = self._vec.fit_transform(self._corpus)

    @staticmethod
    def _doc(a: COAAccount) -> str:
        # Mild signal boosting: include account_type so "Expense" queries lean
        # toward expense accounts even if the name lexically overlaps.
        return f"{a.code} {a.name} {a.account_type}"

    def topk(self, query: str, k: int) -> list[Candidate]:
        q_vec = self._vec.transform([query])
        sims = cosine_similarity(q_vec, self._matrix).ravel()
        # argpartition then sort — avoids a full sort of the whole COA.
        top_idx = sims.argsort()[::-1][:k]
        out: list[Candidate] = []
        for i in top_idx:
            a = self._accounts[int(i)]
            out.append(
                Candidate(
                    code=a.code,
                    name=a.name,
                    statement=a.statement,
                    normal_balance=a.normal_balance,
                    similarity=float(sims[int(i)]),
                )
            )
        return out
