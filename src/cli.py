"""CLI entry point: run the full TB -> COA mapping pipeline on the inputs.

Usage:
    python -m src.cli
    python -m src.cli --no-llm          # force offline heuristic path
    python -m src.cli --inputs <dir>    # alternative inputs directory
    python -m src.cli --output <dir>    # alternative outputs directory
"""
from __future__ import annotations

import argparse
import uuid
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import config
from .audit import build_audit_record, write_outputs
from .ingest import TBRow, ingest
from .mapper import Proposal, mapper_banner, propose
from .retrieve import COARetriever
from .router import route
from .verifier import verify

console = Console()


def _exact_proposal(code: str) -> Proposal:
    return Proposal(
        chosen_code=code,
        confidence=1.0,
        rationale="Exact COA code match on the TB row — no inference needed.",
        alternatives=[],
        source="exact",
    )


def run(
    tb_path: Path,
    coa_path: Path,
    output_dir: Path,
    use_llm: bool,
) -> dict:
    run_id = uuid.uuid4().hex[:12]
    console.rule(f"[bold]TB -> COA Mapper[/bold] run_id={run_id}")
    console.print(f"[dim]{mapper_banner()}  use_llm={use_llm}[/dim]")

    ing = ingest(tb_path, coa_path)

    # Pre-flight summary. Deterministic triage BEFORE any model call.
    pre = Table(title="Pre-flight triage", show_header=True)
    pre.add_column("Metric")
    pre.add_column("Count", justify="right")
    pre.add_row("TB rows after dedupe", str(len(ing.tb_rows)))
    pre.add_row("Duplicate (code, ccy) merges", str(len(ing.duplicates)))
    pre.add_row("Distinct orphan TB codes", str(len(ing.orphan_codes)))
    pre.add_row("COA accounts with cf_category=TBD", str(len(ing.ambiguous_coa_codes)))
    console.print(pre)

    if ing.duplicates:
        console.print(f"[yellow]Duplicates merged:[/yellow] {ing.duplicates}")
    if ing.orphan_codes:
        console.print(f"[yellow]Orphan TB codes:[/yellow] {ing.orphan_codes}")
    if ing.ambiguous_coa_codes:
        console.print(
            f"[yellow]Ambiguous COA cf_category (TBD):[/yellow] {ing.ambiguous_coa_codes}"
        )

    retriever = COARetriever(ing.coa_all)

    records: list[dict] = []
    stats = {"auto": 0, "propose": 0, "escalate": 0, "exact": 0, "agent": 0}

    for row in ing.tb_rows:
        if row.account_code in ing.coa_by_code:
            # Deterministic exact path — no LLM involvement.
            p = _exact_proposal(row.account_code)
            v = verify(row, p, ing.coa_by_code)
            d = route(v)
            records.append(build_audit_record(d, [], stage="exact", run_id=run_id))
            stats[d.decision] += 1
            stats["exact"] += 1
            continue

        # Agent path: retrieve top-k, propose, verify, route.
        candidates = retriever.topk(
            query=f"{row.account_code} {row.account_name}",
            k=config.TOPK_CANDIDATES,
        )
        p = propose(row, candidates, use_llm=use_llm)
        v = verify(row, p, ing.coa_by_code)
        d = route(v)
        records.append(build_audit_record(d, candidates, stage="agent", run_id=run_id))
        stats[d.decision] += 1
        stats["agent"] += 1

    paths = write_outputs(records, output_dir)

    # Post-flight summary
    post = Table(title="Decision summary", show_header=True)
    post.add_column("Decision")
    post.add_column("Count", justify="right")
    post.add_row("auto",     str(stats["auto"]))
    post.add_row("propose",  str(stats["propose"]))
    post.add_row("escalate", str(stats["escalate"]))
    post.add_row("[dim]of which exact[/dim]", f"[dim]{stats['exact']}[/dim]")
    post.add_row("[dim]of which agent[/dim]", f"[dim]{stats['agent']}[/dim]")
    console.print(post)

    console.print(f"[green]Wrote[/green] {paths['mappings']}")
    console.print(f"[green]Wrote[/green] {paths['queue']}")
    console.print(f"[green]Wrote[/green] {paths['audit']}")

    return {"stats": stats, "paths": {k: str(v) for k, v in paths.items()}}


def main() -> None:
    parser = argparse.ArgumentParser(description="TB -> COA mapper agent (Slice 1).")
    parser.add_argument("--inputs", type=Path, default=config.INPUTS)
    parser.add_argument("--output", type=Path, default=config.OUTPUT)
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip the LLM and run the heuristic fallback only.",
    )
    args = parser.parse_args()

    tb_path = args.inputs / "trial_balance.csv"
    coa_path = args.inputs / "chart_of_accounts.csv"
    run(tb_path, coa_path, args.output, use_llm=not args.no_llm)


if __name__ == "__main__":
    main()
