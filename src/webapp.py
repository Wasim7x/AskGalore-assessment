"""Streamlit UI for the TB -> COA Mapper.

Run:
    streamlit run src/webapp.py

What the UI gives you (vs. the CLI):
  - A provider picker (anthropic / openai / groq / gemini / ollama)
  - Model name + API key fields (overrides .env for this session only)
  - Uploaders for your own trial_balance.csv and chart_of_accounts.csv,
    or a checkbox to reuse the bundled messy sample inputs
  - Live decision summary after the run
  - Download buttons for mappings.json, review_queue.json, audit_log.jsonl
  - Collapsible previews of each artifact
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

# When Streamlit runs this file directly (`streamlit run src/webapp.py`), only
# the script's own directory lands on sys.path — not the project root — so
# `from src import ...` would fail with ModuleNotFoundError. Prepend the root
# so `src` is importable as a package regardless of how we were launched.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st


# ─── Page chrome ────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="TB → COA Mapper",
    page_icon="📊",
    layout="wide",
)

PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic Claude",
        "default_model": "claude-haiku-5-5",
        "env_key": "ANTHROPIC_API_KEY",
        "needs_base_url": False,
    },
    "openai": {
        "label": "OpenAI",
        "default_model": "gpt-4o-mini",
        "env_key": "OPENAI_API_KEY",
        "needs_base_url": False,
    },
    "groq": {
        "label": "Groq",
        "default_model": "llama-3.3-70b-versatile",
        "env_key": "GROQ_API_KEY",
        "needs_base_url": False,
    },
    "gemini": {
        "label": "Google Gemini",
        "default_model": "gemini-2.0-flash",
        "env_key": "GEMINI_API_KEY",
        "needs_base_url": False,
    },
    "ollama": {
        "label": "Ollama (local)",
        "default_model": "llama3.1:8b",
        "env_key": None,
        "needs_base_url": True,
    },
}


# ─── Sidebar: model + provider ──────────────────────────────────────────────

with st.sidebar:
    st.header("Model configuration")

    provider_key = st.selectbox(
        "Provider",
        options=list(PROVIDERS),
        format_func=lambda k: PROVIDERS[k]["label"],
        index=0,
        help="Which LLM to call for orphan TB rows. Exact matches never hit the model.",
    )
    cfg = PROVIDERS[provider_key]

    model_name = st.text_input(
        "Model name",
        value=cfg["default_model"],
        help=f"Default for {cfg['label']}: {cfg['default_model']}",
    )

    api_key = ""
    if cfg["env_key"]:
        api_key = st.text_input(
            f"{cfg['env_key']}",
            type="password",
            placeholder="paste your key…",
            help="Stored only in this browser session. Overrides .env for this run.",
        )

    ollama_base_url = ""
    if cfg["needs_base_url"]:
        ollama_base_url = st.text_input(
            "Ollama base URL",
            value=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        )

    use_llm = st.checkbox(
        "Use LLM for orphan rows",
        value=True,
        help="Uncheck for a fully deterministic run (heuristic fallback only).",
    )

    st.divider()
    st.caption(
        "Only the key matching the selected provider is used. "
        "Nothing is written back to `.env`."
    )


# ─── Main header ────────────────────────────────────────────────────────────

st.title("TB → COA Mapper")
st.caption("AskGalore AI Agentic Engineer assignment — Slice 1")

st.markdown(
    """
    Pipeline:
    `ingest` → `classify (exact vs orphan)` → `retrieve top-k` →
    `propose (LLM)` → `verify (deterministic)` → `route` → `audit`.

    60 of 61 rows on the bundled sample never touch the model — only true orphans do.
    """
)


# ─── Inputs ─────────────────────────────────────────────────────────────────

st.subheader("Input files")

in_col1, in_col2 = st.columns(2)
with in_col1:
    tb_upload = st.file_uploader(
        "Trial balance CSV",
        type=["csv"],
        help="Columns: account_code, account_name, currency, debit, credit",
    )
with in_col2:
    coa_upload = st.file_uploader(
        "Chart of accounts CSV",
        type=["csv"],
        help="Columns: account_code, account_name, account_type, parent_code, "
             "statement, cf_category, normal_balance",
    )

use_bundled = st.checkbox(
    "Use the bundled messy sample inputs",
    value=True,
    help="Leave this checked to see the pipeline work end-to-end without uploading anything.",
)

run_clicked = st.button(
    "Run pipeline",
    type="primary",
    use_container_width=True,
)


# ─── Pipeline ───────────────────────────────────────────────────────────────

def _run_pipeline(
    tb_path: Path,
    coa_path: Path,
    output_dir: Path,
    provider: str,
    model_id: str,
    use_llm: bool,
) -> tuple[dict, dict, dict]:
    """Thin wrapper around the existing modules that also overrides MODEL.

    Returns (stats, ingest_summary, output_paths).
    """
    from src import config
    from src import mapper as mapper_module
    from src.audit import build_audit_record, write_outputs
    from src.ingest import ingest
    from src.mapper import Proposal, propose
    from src.retrieve import COARetriever
    from src.router import route
    from src.verifier import verify

    # Replace MODEL everywhere it's bound. mapper.py does
    # `from .config import MODEL`, so updating config.MODEL alone is not enough.
    new_model = config.ModelConfig(
        provider=provider,
        model_id=model_id,
    )
    config.MODEL = new_model
    mapper_module.MODEL = new_model

    ing = ingest(tb_path, coa_path)
    retriever = COARetriever(ing.coa_all)

    run_id = uuid.uuid4().hex[:12]
    records: list[dict] = []
    stats = {"auto": 0, "propose": 0, "escalate": 0, "exact": 0, "agent": 0}

    for row in ing.tb_rows:
        if row.account_code in ing.coa_by_code:
            p = Proposal(
                chosen_code=row.account_code,
                confidence=1.0,
                rationale="Exact COA code match on the TB row — no inference needed.",
                alternatives=[],
                source="exact",
            )
            v = verify(row, p, ing.coa_by_code)
            d = route(v)
            records.append(build_audit_record(d, [], stage="exact", run_id=run_id))
            stats[d.decision] += 1
            stats["exact"] += 1
            continue

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
    ingest_summary = {
        "run_id": run_id,
        "tb_rows": len(ing.tb_rows),
        "duplicates": len(ing.duplicates),
        "orphan_codes": ing.orphan_codes,
        "ambiguous_coa_codes": ing.ambiguous_coa_codes,
    }
    return stats, ingest_summary, {k: str(v) for k, v in paths.items()}


# ─── Run on click ───────────────────────────────────────────────────────────

if run_clicked:
    # 1) Apply provider configuration via environment variables.
    #    providers.py reads these fresh inside each provider __init__.
    if cfg["env_key"]:
        if api_key:
            os.environ[cfg["env_key"]] = api_key
        else:
            # Allow fallback: the user may have left the field blank intentionally
            # so the pipeline degrades to the heuristic. Don't wipe whatever was
            # already in the environment though.
            pass
    if cfg["needs_base_url"] and ollama_base_url:
        os.environ["OLLAMA_BASE_URL"] = ollama_base_url
    os.environ["MODEL_PROVIDER"] = provider_key
    os.environ["MODEL_NAME"] = model_name or ""

    # 2) Resolve inputs. Either the bundled samples or the user's uploads.
    work = Path(tempfile.mkdtemp(prefix="coa_ui_"))
    output_dir = work / "output"
    output_dir.mkdir(parents=True)

    project_root = Path(__file__).resolve().parent.parent
    if use_bundled:
        tb_path = project_root / "inputs" / "trial_balance.csv"
        coa_path = project_root / "inputs" / "chart_of_accounts.csv"
        if not tb_path.exists() or not coa_path.exists():
            st.error(f"Bundled inputs not found under {project_root / 'inputs'}.")
            st.stop()
    else:
        if not tb_upload or not coa_upload:
            st.error("Upload both files, or tick 'Use the bundled messy sample inputs'.")
            st.stop()
        tb_path = work / "trial_balance.csv"
        coa_path = work / "chart_of_accounts.csv"
        tb_path.write_bytes(tb_upload.getvalue())
        coa_path.write_bytes(coa_upload.getvalue())

    # 3) Run.
    try:
        with st.spinner(
            f"Running pipeline · provider={provider_key} · "
            f"model={model_name or cfg['default_model']} · use_llm={use_llm}"
        ):
            stats, summary, paths = _run_pipeline(
                tb_path=tb_path,
                coa_path=coa_path,
                output_dir=output_dir,
                provider=provider_key,
                model_id=model_name or cfg["default_model"],
                use_llm=use_llm,
            )
    except Exception as exc:  # noqa: BLE001
        st.error(f"Pipeline failed: {type(exc).__name__}: {exc}")
        st.stop()

    st.success(f"Done. run_id = `{summary['run_id']}`")

    # 4) Pre-flight triage.
    st.subheader("Pre-flight triage")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("TB rows (after dedupe)", summary["tb_rows"])
    m2.metric("Duplicate (code, ccy) merges", summary["duplicates"])
    m3.metric("Orphan TB codes", len(summary["orphan_codes"]))
    m4.metric("COA cf_category = TBD", len(summary["ambiguous_coa_codes"]))
    if summary["orphan_codes"]:
        st.info(f"Orphan TB codes: {summary['orphan_codes']}")
    if summary["ambiguous_coa_codes"]:
        st.info(f"Ambiguous COA cf_category (TBD): {summary['ambiguous_coa_codes']}")

    # 5) Decision summary.
    st.subheader("Decision summary")
    d1, d2, d3, d4, d5 = st.columns(5)
    d1.metric("auto",     stats["auto"])
    d2.metric("propose",  stats["propose"])
    d3.metric("escalate", stats["escalate"])
    d4.metric("of which exact", stats["exact"])
    d5.metric("of which agent", stats["agent"])

    # 6) Downloads.
    st.subheader("Download outputs")
    dl1, dl2, dl3 = st.columns(3)
    mappings_bytes = Path(paths["mappings"]).read_bytes()
    queue_bytes    = Path(paths["queue"]).read_bytes()
    audit_bytes    = Path(paths["audit"]).read_bytes()
    with dl1:
        st.download_button(
            "mappings.json",
            data=mappings_bytes,
            file_name="mappings.json",
            mime="application/json",
            use_container_width=True,
        )
    with dl2:
        st.download_button(
            "review_queue.json",
            data=queue_bytes,
            file_name="review_queue.json",
            mime="application/json",
            use_container_width=True,
        )
    with dl3:
        st.download_button(
            "audit_log.jsonl",
            data=audit_bytes,
            file_name="audit_log.jsonl",
            mime="application/x-ndjson",
            use_container_width=True,
        )

    # 7) Previews.
    st.subheader("Preview")

    with st.expander("review_queue.json  —  items a human must action", expanded=True):
        queue = json.loads(queue_bytes.decode("utf-8") or "[]")
        if queue:
            st.json(queue)
        else:
            st.caption("Empty — nothing to review this run.")

    with st.expander("mappings.json  —  first 10 rows"):
        mappings = json.loads(mappings_bytes.decode("utf-8") or "[]")
        st.json(mappings[:10])
        st.caption(f"Showing 10 of {len(mappings)} total mappings.")

    with st.expander("audit_log.jsonl  —  agent-path records only"):
        agent_records = [
            json.loads(line)
            for line in audit_bytes.decode("utf-8").splitlines()
            if line.strip() and json.loads(line).get("stage") == "agent"
        ]
        if agent_records:
            st.json(agent_records)
        else:
            st.caption("No agent-path records — every TB row matched the COA exactly.")

else:
    st.info(
        "Set the provider on the left, (optionally) upload your own CSVs above, "
        "and click **Run pipeline**."
    )
