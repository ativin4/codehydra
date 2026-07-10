"""MCP tool definitions for the FinMerge trade-statement processor.

Exposes five tools on the CodeHydra built-in MCP server:

  • ``process_trade_statements`` — batch-process PDFs with auto-setup of LLM.
  • ``preview_statement``       — dry-run a single PDF.
  • ``setup_finmerge``          — one-click: start Ollama + pull model.
  • ``finmerge_model``          — manage models: list / pull / delete / switch.
  • ``stop_finmerge``           — stop the Ollama server if started by FinMerge.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .extractor import extract_from_pdf
from .parser import parse_statement, detect_broker
from .llm_parser import (
    ensure_ready,
    format_model_catalog,
    get_model_info,
    get_suggested_models,
    get_system_ram_gb,
    is_ollama_installed,
    is_ollama_running,
    list_local_models,
    parse_with_llm,
    pick_best_model,
    pull_model,
    delete_model,
    setup_status,
    start_ollama,
    stop_ollama,
    DEFAULT_MODEL,
    PREFERRED_MODELS,
)
from .merger import merge_and_match
from .reporter import (
    generate_csv,
    generate_open_positions_csv,
    generate_tax_report,
    generate_schedule_fa,
)

logger = logging.getLogger(__name__)

# Default output directory (relative to workspace root).
OUTPUT_DIR = Path(".codehydra") / "finmerge"


def register_tools(mcp: FastMCP) -> None:
    """Register FinMerge tools on the shared CodeHydra MCP server."""

    # ================================================================ #
    #                   CORE PROCESSING TOOLS                          #
    # ================================================================ #

    @mcp.tool()
    def process_trade_statements(
        directory: str,
        output_dir: str = "",
        use_llm: bool = True,
        auto_setup: bool = True,
    ) -> dict:
        """Process all PDF trade statements in a directory and generate a
        merged, tax-ready capital-gains report.

        Reads every ``.pdf`` file in *directory*, extracts trade data (buy/sell
        transactions), performs FIFO matching across all statements, and
        classifies each matched lot as STCG or LTCG for both Indian and
        Foreign assets.

        When ``auto_setup`` is True (default), the tool will automatically:
          1. Start the Ollama server if not running
          2. Pull the recommended model (qwen3:8b) if none is available
          3. Use the LLM to parse any PDFs the heuristic parser can't handle

        **Outputs written to disk:**

        - ``tax_report.txt``        — human-readable ITR-ready report
        - ``matched_lots.csv``      — every FIFO-matched buy–sell pair
        - ``open_positions.csv``    — shares still held (unrealized)
        - ``schedule_fa.txt``       — foreign-asset declaration data
        - ``processing_log.json``   — per-file extraction status

        Args:
            directory:  Path to folder containing PDF trade statements.
            output_dir: Where to write reports (default: ``.codehydra/finmerge/``).
            use_llm:    Use local LLM for PDFs that heuristic parsing fails on.
            auto_setup: Automatically start Ollama and pull a model if needed.

        Returns:
            Dict with ``tax_report``, ``schedule_fa``, ``stats``,
            ``output_files``, ``processing_log``, and ``llm_setup`` status.
        """
        dir_path = Path(directory).resolve()
        if not dir_path.is_dir():
            return {"error": f"Not a directory: {directory}"}

        out = Path(output_dir) if output_dir else OUTPUT_DIR
        out.mkdir(parents=True, exist_ok=True)

        pdfs = sorted(
            [p for p in dir_path.iterdir() if p.suffix.lower() == ".pdf"]
        )
        if not pdfs:
            return {"error": f"No PDF files found in {directory}"}

        # ---- Phase 0: Auto-setup LLM if requested ----
        llm_ready = False
        llm_model = None
        llm_setup_info = {}

        if use_llm and auto_setup:
            setup_result = ensure_ready(auto_pull=True)
            llm_ready = setup_result["ready"]
            llm_model = setup_result.get("model")
            llm_setup_info = setup_result
            if llm_ready:
                logger.info("LLM ready: %s", llm_model)
            else:
                logger.warning("LLM auto-setup incomplete: %s", setup_result["message"])
        elif use_llm:
            llm_ready = is_ollama_running() and pick_best_model() is not None
            llm_model = pick_best_model()

        # ---- Phase 1: Extract & Parse ----
        all_trades = []
        log = []

        for pdf in pdfs:
            try:
                extracted = extract_from_pdf(pdf)
                trades = parse_statement(extracted)
                broker = detect_broker(extracted.get("text", ""))
                parse_method = "heuristic"

                # If heuristic parser found nothing, try LLM.
                if not trades and use_llm and llm_ready:
                    raw_text = extracted.get("text", "")
                    if raw_text.strip():
                        trades = parse_with_llm(
                            raw_text,
                            model=llm_model,
                            source_file=pdf.name,
                        )
                        if trades:
                            parse_method = "llm"
                            logger.info(
                                "LLM extracted %d trades from %s",
                                len(trades), pdf.name,
                            )

                log.append({
                    "file": pdf.name,
                    "broker": broker,
                    "trades_found": len(trades),
                    "parse_method": parse_method,
                    "status": "ok" if trades else "no_trades_found",
                })
                all_trades.extend(trades)
            except Exception as e:
                log.append({
                    "file": pdf.name,
                    "status": "error",
                    "error": str(e),
                })

        if not all_trades:
            hint = (
                "Ensure the PDFs contain tabular trade data with "
                "recognizable column headers (Date, Symbol, Buy/Sell, "
                "Qty, Price)."
            )
            if not llm_ready:
                hint += (
                    " TIP: Run setup_finmerge to set up Ollama for "
                    "LLM-assisted parsing of complex PDFs."
                )
            return {
                "error": "No trades could be parsed from any PDF.",
                "processing_log": log,
                "hint": hint,
                "llm_setup": llm_setup_info,
            }

        # ---- Phase 2: FIFO Match & Classify ----
        summary = merge_and_match(all_trades)

        # ---- Phase 3: Generate Reports ----
        tax_report = generate_tax_report(summary)

        lots_csv_path = out / "matched_lots.csv"
        pos_csv_path  = out / "open_positions.csv"
        generate_csv(summary.tax_lots, lots_csv_path)
        generate_open_positions_csv(summary.open_positions, pos_csv_path)

        report_path = out / "tax_report.txt"
        report_path.write_text(tax_report)

        schedule_fa = generate_schedule_fa(summary)
        fa_path = out / "schedule_fa.txt"
        fa_path.write_text(schedule_fa)

        log_path = out / "processing_log.json"
        log_path.write_text(json.dumps(log, indent=2))

        return {
            "tax_report": tax_report,
            "schedule_fa": schedule_fa,
            "stats": {
                "pdfs_processed":      len(pdfs),
                "total_trades_parsed": len(all_trades),
                "matched_lots":        summary.total_lots,
                "open_positions":      len(summary.open_positions),
                "indian_stcg":         str(summary.indian_stcg),
                "indian_ltcg":         str(summary.indian_ltcg),
                "foreign_stcg":        str(summary.foreign_stcg),
                "foreign_ltcg":        str(summary.foreign_ltcg),
                "total_realized_gain": str(summary.total_realized_gain),
            },
            "output_files": {
                "tax_report":        str(report_path.resolve()),
                "matched_lots_csv":  str(lots_csv_path.resolve()),
                "open_positions_csv": str(pos_csv_path.resolve()),
                "schedule_fa":       str(fa_path.resolve()),
                "processing_log":    str(log_path.resolve()),
            },
            "processing_log": log,
            "llm_setup": {
                "llm_used": llm_ready,
                "model": llm_model,
            },
        }

    @mcp.tool()
    def preview_statement(file_path: str, use_llm: bool = True) -> dict:
        """Preview parsed trades from a single PDF without running the full
        merge / tax calculation.

        Useful for verifying that a PDF is being parsed correctly before
        committing to a full batch run with ``process_trade_statements``.

        Args:
            file_path: Path to a single PDF trade statement.
            use_llm:   If True, try LLM parsing when heuristic fails (default: True).

        Returns:
            Dict with ``broker_detected``, ``trades_found``, ``trades`` list,
            ``parse_method``, ``text_preview``, and ``tables_found``.
        """
        pdf = Path(file_path).resolve()
        if not pdf.exists():
            return {"error": f"File not found: {file_path}"}
        if pdf.suffix.lower() != ".pdf":
            return {"error": f"Not a PDF file: {file_path}"}

        extracted = extract_from_pdf(pdf)
        broker = detect_broker(extracted.get("text", ""))
        trades = parse_statement(extracted)
        parse_method = "heuristic"

        # If heuristic failed, try LLM (with auto-setup).
        if not trades and use_llm:
            raw_text = extracted.get("text", "")
            if raw_text.strip():
                llm_trades = parse_with_llm(
                    raw_text, source_file=pdf.name, auto_setup=True,
                )
                if llm_trades:
                    trades = llm_trades
                    parse_method = "llm"

        return {
            "file": pdf.name,
            "broker_detected": broker,
            "trades_found": len(trades),
            "parse_method": parse_method,
            "trades": [
                {
                    "date":     t.date.isoformat(),
                    "symbol":   t.symbol,
                    "action":   t.action.value,
                    "quantity": str(t.quantity),
                    "price":    str(t.price),
                    "amount":   str(t.amount),
                    "exchange": t.exchange,
                    "currency": t.currency,
                    "region":   t.region.value,
                }
                for t in trades
            ],
            "text_preview":  extracted.get("text", "")[:500],
            "tables_found":  len(extracted.get("tables", [])),
        }

    # ================================================================ #
    #                   LLM LIFECYCLE TOOLS                             #
    # ================================================================ #

    @mcp.tool()
    def setup_finmerge(
        auto_start: bool = True,
        auto_pull: bool = False,
        model: str = "",
    ) -> dict:
        """Set up Ollama for LLM-assisted PDF parsing.

        When called without ``model``, shows a curated catalog of
        open-source models recommended for financial document parsing,
        filtered by your system's RAM. Pick one and pass its name to
        start using it.

        When called with ``model='qwen3:8b'`` (or any model name), it will:
          1. Start the Ollama server if not running
          2. Pull the specified model
          3. Confirm everything is ready

        You can also pass any Ollama model name not in our catalog — any
        model that Ollama supports will work.

        Args:
            auto_start: Start Ollama server if not running (default: True).
            auto_pull:  Pull the recommended model automatically without
                        showing the catalog (default: False — show catalog).
            model:      Model to pull. If empty and auto_pull is False,
                        shows the model catalog for you to choose from.

        Returns:
            Dict with setup status, model catalog, and instructions.
        """
        steps: list = []
        ram_gb = get_system_ram_gb()

        # Step 1: Check installation.
        if not is_ollama_installed():
            return {
                "ready": False,
                "steps": ["install_needed"],
                "message": (
                    "❌ Ollama is not installed.\n\n"
                    "Install it first:\n"
                    "  macOS:   brew install ollama\n"
                    "  Linux:   curl -fsSL https://ollama.ai/install.sh | sh\n"
                    "  Windows: Download from https://ollama.ai\n\n"
                    "Then run this tool again."
                ),
            }

        # Step 2: Start server if needed.
        if not is_ollama_running():
            if auto_start:
                start_result = start_ollama()
                steps.append({
                    "action": "start_server",
                    "result": start_result,
                })
                if not start_result["started"]:
                    return {
                        "ready": False,
                        "steps": steps,
                        "message": f"❌ Failed to start Ollama: {start_result['message']}",
                    }
            else:
                return {
                    "ready": False,
                    "steps": ["server_not_running"],
                    "message": "Ollama is installed but not running. Run with auto_start=True.",
                }
        else:
            steps.append({"action": "server_check", "result": "already_running"})

        # Step 3: Check for existing models.
        current_model = pick_best_model()

        if current_model and not model:
            # Already have a model — report ready status + show catalog.
            models = list_local_models()
            return {
                "ready": True,
                "model": current_model,
                "all_models": models,
                "steps": steps,
                "system_ram_gb": ram_gb,
                "model_catalog": format_model_catalog(ram_gb),
                "suggested_models": get_suggested_models(ram_gb),
                "message": (
                    f"✅ FinMerge is ready with model: {current_model}\n\n"
                    f"Want a different model? See the catalog below and run:\n"
                    f"  setup_finmerge(model='<model_name>')\n\n"
                    f"Or use finmerge_model(action='suggest') to browse all options."
                ),
            }

        # Step 4: No model yet — either pull specified or show catalog.
        if model:
            # User chose a specific model — pull it.
            target = model
            info = get_model_info(target)
            steps.append({"action": "pulling_model", "model": target})

            pull_result = pull_model(target)
            steps.append({"action": "pull_result", "result": pull_result})

            if not pull_result["success"]:
                return {
                    "ready": False,
                    "steps": steps,
                    "model_info": info,
                    "message": f"❌ Failed to pull {target}: {pull_result['message']}",
                }

            current_model = pick_best_model() or target
            models = list_local_models()
            return {
                "ready": True,
                "model": current_model,
                "all_models": models,
                "model_info": info,
                "steps": steps,
                "message": (
                    f"✅ Pulled and ready!\n\n"
                    f"  Model:  {current_model}\n"
                    + (f"  Info:   {info['description']}\n" if info else "")
                    + f"\nProcess your PDFs with: process_trade_statements(directory='path/to/pdfs')"
                ),
            }

        if auto_pull:
            # Auto-pull the default without showing catalog.
            target = DEFAULT_MODEL
            steps.append({"action": "auto_pulling", "model": target})
            pull_result = pull_model(target)
            steps.append({"action": "pull_result", "result": pull_result})

            if not pull_result["success"]:
                return {
                    "ready": False,
                    "steps": steps,
                    "message": f"❌ Failed to pull {target}: {pull_result['message']}",
                    "model_catalog": format_model_catalog(ram_gb),
                }

            current_model = pick_best_model() or target
            models = list_local_models()
            return {
                "ready": True,
                "model": current_model,
                "all_models": models,
                "steps": steps,
                "message": (
                    f"✅ Auto-pulled and ready!\n\n"
                    f"  Model:  {current_model}\n\n"
                    f"Process your PDFs with: process_trade_statements(directory='path/to/pdfs')"
                ),
            }

        # No model specified, auto_pull=False → show the catalog.
        catalog = format_model_catalog(ram_gb)
        suggested = get_suggested_models(ram_gb)

        return {
            "ready": False,
            "steps": steps + [{"action": "awaiting_model_choice"}],
            "system_ram_gb": ram_gb,
            "model_catalog": catalog,
            "suggested_models": suggested,
            "message": (
                "Ollama server is running but no model is installed yet.\n\n"
                "Here are the recommended open-source models for financial "
                "document parsing. Choose one based on your hardware:\n\n"
                + catalog
                + "\n\nTo pull your chosen model, run:\n"
                "  setup_finmerge(model='qwen3:8b')  ← replace with your pick\n\n"
                "Or auto-pull the recommended model:\n"
                "  setup_finmerge(auto_pull=True)"
            ),
        }

    @mcp.tool()
    def finmerge_model(
        action: str = "suggest",
        model: str = "",
    ) -> dict:
        """Browse, pull, and manage open-source models for FinMerge.

        Args:
            action: One of:
                    - ``suggest`` — show the curated model catalog with
                      recommendations based on your hardware (default)
                    - ``info``    — show detailed info about a specific model
                    - ``list``    — show locally installed models
                    - ``pull``    — download a model (specify ``model`` name)
                    - ``delete``  — remove a model (specify ``model`` name)
                    - ``status``  — show full LLM stack status
            model:  Model name for info/pull/delete (e.g. ``qwen3:8b``,
                    ``llama3.1:8b``). For ``suggest``, filters by name prefix.

        Returns:
            Dict with action results, model catalog, or model details.
        """
        if action == "suggest":
            ram_gb = get_system_ram_gb()
            catalog = format_model_catalog(ram_gb)
            suggested = get_suggested_models(ram_gb)

            # If a model name prefix was given, filter suggestions.
            if model:
                filtered = [
                    m for m in suggested
                    if model.lower() in m["name"].lower()
                    or model.lower() in m["display_name"].lower()
                ]
                if filtered:
                    suggested = filtered

            return {
                "system_ram_gb": ram_gb,
                "model_catalog": catalog,
                "suggested_models": suggested,
                "installed_models": list_local_models(),
                "active_model": pick_best_model(),
                "message": (
                    "Here are the recommended models for financial "
                    "document parsing:\n\n" + catalog
                ),
            }

        elif action == "info":
            if not model:
                return {
                    "error": "Specify a model name to get info about.",
                    "hint": "Try: finmerge_model(action='info', model='qwen3:8b')",
                }
            info = get_model_info(model)
            if info:
                installed = model in list_local_models() or any(
                    model.split(':')[0] == m.split(':')[0]
                    for m in list_local_models()
                )
                info["installed"] = installed
                info["message"] = (
                    f"📋 {info['display_name']} ({info['params']})\n"
                    f"   {info['description']}\n\n"
                    f"   Speed:    {info['speed_tier']}\n"
                    f"   Accuracy: {info['accuracy_tier']}\n"
                    f"   RAM:      {info['ram_gb']}GB minimum\n"
                    f"   Download: ~{info['size_mb'] / 1000:.1f}GB\n"
                    f"   Status:   {'✅ installed' if installed else '⬇️  not installed'}\n\n"
                    f"   Strengths:\n"
                    + "\n".join(f"     • {s}" for s in info['strengths'])
                    + f"\n\n   Best for: {info['best_for']}\n"
                    + (f"\n   ⭐ This is our recommended model!" if info['recommended'] else "")
                    + (f"\n\n   To install: finmerge_model(action='pull', model='{info['name']}')" if not installed else "")
                )
                return info
            else:
                return {
                    "model": model,
                    "in_catalog": False,
                    "message": (
                        f"'{model}' is not in our curated catalog, but you can "
                        f"still use it — any Ollama model works.\n\n"
                        f"To pull it: finmerge_model(action='pull', model='{model}')\n"
                        f"To see our recommendations: finmerge_model(action='suggest')"
                    ),
                }

        elif action == "list":
            models = list_local_models()
            recommended = pick_best_model()
            # Annotate with catalog info.
            annotated = []
            for m in models:
                info = get_model_info(m)
                annotated.append({
                    "name": m,
                    "active": m == recommended,
                    "in_catalog": info is not None,
                    "display_name": info["display_name"] if info else m,
                    "accuracy": info["accuracy_tier"] if info else "unknown",
                })
            return {
                "models": annotated,
                "active_model": recommended,
                "total": len(models),
                "message": (
                    f"Installed models ({len(models)}):\n"
                    + ("\n".join(
                        f"  {'→' if m['active'] else ' '} {m['name']}"
                        + (f"  ({m['display_name']}, {m['accuracy']} accuracy)" if m['in_catalog'] else "")
                        for m in annotated
                    ) if annotated else "  (none)")
                    + f"\n\nActive: {recommended or '(none)'}"
                    + "\n\nBrowse available models: finmerge_model(action='suggest')"
                ),
            }

        elif action == "pull":
            target = model or DEFAULT_MODEL
            info = get_model_info(target)

            if not is_ollama_running():
                start_result = start_ollama()
                if not start_result["started"]:
                    return {
                        "success": False,
                        "message": f"Cannot pull — Ollama server failed to start: {start_result['message']}",
                    }

            result = pull_model(target)
            if result["success"]:
                new_models = list_local_models()
                active = pick_best_model()
                result["all_models"] = new_models
                result["active_model"] = active
                result["model_info"] = info
                result["message"] = (
                    f"✅ Successfully pulled {target}!\n"
                    + (f"   {info['description']}\n" if info else "")
                    + f"\nActive model: {active}"
                )
            return result

        elif action == "delete":
            if not model:
                return {"success": False, "message": "Specify model name to delete."}
            return delete_model(model)

        elif action == "status":
            return setup_status()

        return {
            "error": f"Unknown action: {action}.",
            "available_actions": ["suggest", "info", "list", "pull", "delete", "status"],
        }

    @mcp.tool()
    def stop_finmerge() -> dict:
        """Stop the Ollama server if it was started by FinMerge.

        Only stops servers that FinMerge itself launched — won't kill a
        system Ollama service or one started by the user manually.

        Returns:
            Dict with ``stopped`` status and ``message``.
        """
        return stop_ollama()
