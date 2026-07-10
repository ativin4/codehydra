"""LLM-assisted trade parsing via local Ollama models.

This module manages the full Ollama lifecycle:
  1. Detect installation  →  install guidance if missing
  2. Start the server     →  auto-launch ``ollama serve`` in background
  3. Suggest models       →  curated catalog of open models for finance parsing
  4. Pull a model         →  download chosen model
  5. Parse PDFs           →  send raw text for structured extraction

The model catalog is curated specifically for **structured financial data
extraction** — parsing broker trade statements into clean JSON. Models are
ranked by their ability to follow strict output schemas, handle tabular
financial data, and run efficiently on consumer hardware.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx

from .models import Trade, TradeAction, AssetRegion

logger = logging.getLogger(__name__)

# Ollama API endpoint.
OLLAMA_API_BASE = "http://localhost:11434"


# ------------------------------------------------------------------ #
#                      MODEL CATALOG                                  #
# ------------------------------------------------------------------ #

@dataclass
class ModelInfo:
    """Metadata for a recommended open-source model."""
    name: str               # Ollama model tag (e.g. "qwen3:8b")
    display_name: str       # Human-readable name
    params: str             # Parameter count (e.g. "8B")
    ram_gb: float           # Minimum RAM/VRAM required (GB)
    speed_tier: str         # "fast", "medium", "slow"
    accuracy_tier: str      # "good", "great", "excellent"
    size_mb: int            # Approximate download size (MB)
    description: str        # Why this model is good for the task
    strengths: List[str] = field(default_factory=list)
    best_for: str = ""      # One-line recommendation
    recommended: bool = False  # Our top pick?


# Curated catalog — models proven good at structured JSON extraction
# from financial documents. Ordered by our recommendation.
MODEL_CATALOG: Dict[str, ModelInfo] = {}

_CATALOG_LIST: List[ModelInfo] = [
    # ---- SMALL / FAST (4-8B) — runs on 8GB RAM ----
    ModelInfo(
        name="qwen3:8b",
        display_name="Qwen 3 8B",
        params="8B", ram_gb=6, speed_tier="fast", accuracy_tier="great",
        size_mb=5100, recommended=True,
        description=(
            "Alibaba's latest — outstanding at structured JSON output and "
            "following strict schemas. Best balance of speed and accuracy "
            "for financial document parsing."
        ),
        strengths=[
            "Excellent structured JSON output",
            "Strong with tabular financial data",
            "Fast inference on consumer hardware",
            "Multilingual (handles Indian + international brokers)",
        ],
        best_for="Most users — best speed/accuracy tradeoff",
    ),
    ModelInfo(
        name="gemma3:4b",
        display_name="Google Gemma 3 4B",
        params="4B", ram_gb=4, speed_tier="fast", accuracy_tier="good",
        size_mb=3000,
        description=(
            "Google's compact model. Surprisingly capable for its size. "
            "Great if you have limited RAM or want the fastest processing."
        ),
        strengths=[
            "Runs on as little as 4GB RAM",
            "Very fast inference",
            "Good at following formatting instructions",
        ],
        best_for="Low-RAM machines or fastest possible processing",
    ),
    ModelInfo(
        name="phi4-mini:3.8b",
        display_name="Microsoft Phi-4 Mini",
        params="3.8B", ram_gb=4, speed_tier="fast", accuracy_tier="good",
        size_mb=2500,
        description=(
            "Microsoft's small reasoning model. Punches above its weight "
            "on structured extraction tasks despite tiny size."
        ),
        strengths=[
            "Tiny footprint, runs anywhere",
            "Strong reasoning for its size",
            "Good at number parsing",
        ],
        best_for="Minimal resource environments",
    ),
    ModelInfo(
        name="llama3.1:8b",
        display_name="Meta Llama 3.1 8B",
        params="8B", ram_gb=6, speed_tier="fast", accuracy_tier="great",
        size_mb=4700,
        description=(
            "Meta's workhorse model. Solid all-around performer with strong "
            "instruction following. Well-tested and widely used."
        ),
        strengths=[
            "Battle-tested and reliable",
            "Strong instruction following",
            "Good at parsing structured text",
            "Large community and ecosystem",
        ],
        best_for="Reliable general-purpose choice",
    ),
    ModelInfo(
        name="mistral:7b",
        display_name="Mistral 7B",
        params="7B", ram_gb=6, speed_tier="fast", accuracy_tier="good",
        size_mb=4100,
        description=(
            "Mistral AI's efficient model. Fast and reliable for "
            "straightforward extraction tasks."
        ),
        strengths=[
            "Fast inference speed",
            "Efficient architecture",
            "Good at structured output",
        ],
        best_for="Quick processing of standard broker formats",
    ),

    # ---- MEDIUM (12-14B) — needs 12-16GB RAM ----
    ModelInfo(
        name="qwen3:14b",
        display_name="Qwen 3 14B",
        params="14B", ram_gb=12, speed_tier="medium", accuracy_tier="excellent",
        size_mb=9000,
        description=(
            "Bigger sibling of Qwen 3 8B. Noticeably better at handling "
            "messy/ambiguous PDFs and complex multi-column layouts."
        ),
        strengths=[
            "Handles ambiguous/messy PDFs much better",
            "Excellent at multi-column table parsing",
            "Better number accuracy than 8B models",
            "Great structured JSON fidelity",
        ],
        best_for="Complex PDFs with non-standard layouts (if you have 12GB+ RAM)",
    ),
    ModelInfo(
        name="gemma3:12b",
        display_name="Google Gemma 3 12B",
        params="12B", ram_gb=10, speed_tier="medium", accuracy_tier="great",
        size_mb=8100,
        description=(
            "Google's mid-range model. Strong performance with good "
            "efficiency. Handles diverse broker formats well."
        ),
        strengths=[
            "Good accuracy/speed balance at medium scale",
            "Strong multilingual support",
            "Reliable structured output",
        ],
        best_for="Step up from 8B if you want better accuracy",
    ),
    ModelInfo(
        name="mistral-small:24b",
        display_name="Mistral Small 24B",
        params="24B", ram_gb=16, speed_tier="medium", accuracy_tier="excellent",
        size_mb=14000,
        description=(
            "Mistral's mid-tier model. Excellent reasoning and very "
            "reliable structured extraction."
        ),
        strengths=[
            "Strong reasoning capability",
            "Very reliable JSON output",
            "Handles edge cases well",
        ],
        best_for="High accuracy when you have 16GB+ RAM",
    ),

    # ---- LARGE (32B+) — needs 24-48GB RAM ----
    ModelInfo(
        name="qwen3:32b",
        display_name="Qwen 3 32B",
        params="32B", ram_gb=24, speed_tier="slow", accuracy_tier="excellent",
        size_mb=20000,
        description=(
            "Top-tier open model for financial parsing. Handles the most "
            "complex and ambiguous PDFs. Near-perfect structured output."
        ),
        strengths=[
            "Near-perfect structured JSON output",
            "Handles badly scanned / OCR'd documents",
            "Excellent at inferring missing data",
            "Best accuracy in the catalog",
        ],
        best_for="Maximum accuracy (needs 24GB+ RAM/VRAM)",
    ),
    ModelInfo(
        name="deepseek-r1:32b",
        display_name="DeepSeek R1 32B",
        params="32B", ram_gb=24, speed_tier="slow", accuracy_tier="excellent",
        size_mb=20000,
        description=(
            "DeepSeek's reasoning model. Uses chain-of-thought to work "
            "through ambiguous financial documents step by step."
        ),
        strengths=[
            "Chain-of-thought reasoning",
            "Excellent at resolving ambiguities",
            "Handles complex multi-broker merges",
        ],
        best_for="Most complex/ambiguous documents (needs 24GB+ RAM)",
    ),
    ModelInfo(
        name="llama3.3:70b",
        display_name="Meta Llama 3.3 70B",
        params="70B", ram_gb=48, speed_tier="slow", accuracy_tier="excellent",
        size_mb=40000,
        description=(
            "Meta's flagship. Maximum capability but requires serious hardware. "
            "Only choose this if you have a powerful GPU or Apple Silicon with "
            "48GB+ unified memory."
        ),
        strengths=[
            "Highest overall capability",
            "Handles any broker format",
            "Best at edge cases and rare layouts",
        ],
        best_for="Maximum power (needs 48GB+ RAM — M2 Ultra, GPU server, etc.)",
    ),
]

# Build the lookup dict.
for _m in _CATALOG_LIST:
    MODEL_CATALOG[_m.name] = _m

# Preference order for auto-selection (small models first).
PREFERRED_MODELS = [m.name for m in _CATALOG_LIST]

# Default model for auto-pull.
DEFAULT_MODEL = "qwen3:8b"

# How long to wait for Ollama server to start (seconds).
SERVER_START_TIMEOUT = 15

# How long to wait for a model pull (seconds).
MODEL_PULL_TIMEOUT = 900  # 15 minutes for large models

# PID file for tracking Ollama server we started.
_PIDFILE_DIR = os.path.join(os.path.expanduser("~"), ".codehydra", "finmerge")
_PIDFILE = os.path.join(_PIDFILE_DIR, "ollama.pid")

# System prompt for the LLM to extract structured trade data.
EXTRACTION_PROMPT = """\
You are a financial document parser. Given raw text extracted from a broker \
trade statement PDF, extract ALL buy and sell transactions into a JSON array.

Each trade object must have these fields:
- "date": trade date as "YYYY-MM-DD"
- "symbol": stock ticker/symbol (uppercase, no exchange suffixes like -EQ, .NS)
- "action": "buy" or "sell"
- "quantity": number of shares (positive number)
- "price": price per share (number)
- "amount": total trade value (number, = quantity × price)
- "exchange": exchange name if visible (e.g. "NSE", "NASDAQ"), else ""
- "currency": "INR" for Indian, "USD" for US stocks, etc.

Return ONLY a JSON array of trade objects. No markdown, no explanation.
If you cannot find any trades, return an empty array: []

Example output:
[
  {"date": "2024-03-15", "symbol": "RELIANCE", "action": "buy", "quantity": 10, "price": 2450.50, "amount": 24505.00, "exchange": "NSE", "currency": "INR"},
  {"date": "2024-06-20", "symbol": "AAPL", "action": "sell", "quantity": 5, "price": 195.00, "amount": 975.00, "exchange": "NASDAQ", "currency": "USD"}
]
"""


# ------------------------------------------------------------------ #
#                    OLLAMA SERVER MANAGEMENT                         #
# ------------------------------------------------------------------ #

def is_ollama_installed() -> bool:
    """Check if the ``ollama`` binary is on PATH."""
    return bool(shutil.which("ollama"))


def is_ollama_running() -> bool:
    """Check if the Ollama API is responding."""
    try:
        resp = httpx.get(f"{OLLAMA_API_BASE}/api/tags", timeout=3.0)
        return resp.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException, httpx.ConnectTimeout):
        return False


def start_ollama() -> dict:
    """Start the Ollama server in the background.

    Returns a status dict:
        {"started": bool, "pid": int | None, "message": str, "already_running": bool}
    """
    if not is_ollama_installed():
        return {
            "started": False,
            "pid": None,
            "already_running": False,
            "message": (
                "Ollama is not installed.\n"
                "  macOS:   brew install ollama\n"
                "  Linux:   curl -fsSL https://ollama.ai/install.sh | sh\n"
                "  Windows: Download from https://ollama.ai"
            ),
        }

    if is_ollama_running():
        return {
            "started": True,
            "pid": None,
            "already_running": True,
            "message": "Ollama server is already running.",
        }

    # Launch `ollama serve` as a detached background process.
    try:
        devnull = open(os.devnull, "w")
        proc = subprocess.Popen(
            ["ollama", "serve"],
            stdout=devnull,
            stderr=devnull,
            start_new_session=True,
        )

        # Save PID so we can stop it later.
        os.makedirs(_PIDFILE_DIR, exist_ok=True)
        with open(_PIDFILE, "w") as f:
            f.write(str(proc.pid))

        # Wait for the server to become responsive.
        deadline = time.monotonic() + SERVER_START_TIMEOUT
        while time.monotonic() < deadline:
            if is_ollama_running():
                return {
                    "started": True,
                    "pid": proc.pid,
                    "already_running": False,
                    "message": f"Ollama server started (PID {proc.pid}).",
                }
            time.sleep(0.5)

        return {
            "started": False,
            "pid": proc.pid,
            "already_running": False,
            "message": (
                f"Ollama process started (PID {proc.pid}) but server didn't "
                f"respond within {SERVER_START_TIMEOUT}s. It may still be loading."
            ),
        }
    except Exception as e:
        return {
            "started": False,
            "pid": None,
            "already_running": False,
            "message": f"Failed to start Ollama: {e}",
        }


def stop_ollama() -> dict:
    """Stop the Ollama server if we started it.

    Returns:
        {"stopped": bool, "message": str}
    """
    if not os.path.exists(_PIDFILE):
        return {"stopped": False, "message": "No PID file found — Ollama was not started by FinMerge."}

    try:
        with open(_PIDFILE) as f:
            pid = int(f.read().strip())

        os.kill(pid, signal.SIGTERM)
        os.remove(_PIDFILE)
        return {"stopped": True, "message": f"Stopped Ollama server (PID {pid})."}
    except ProcessLookupError:
        os.remove(_PIDFILE)
        return {"stopped": False, "message": "Ollama process was already stopped."}
    except Exception as e:
        return {"stopped": False, "message": f"Error stopping Ollama: {e}"}


# ------------------------------------------------------------------ #
#                      MODEL MANAGEMENT                               #
# ------------------------------------------------------------------ #

def list_local_models() -> List[str]:
    """Return names of locally available Ollama models."""
    try:
        resp = httpx.get(f"{OLLAMA_API_BASE}/api/tags", timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        pass
    return []


def pick_best_model() -> Optional[str]:
    """Pick the best available local model from our preference list."""
    local = list_local_models()
    local_base = {m.split(":")[0]: m for m in local}

    for pref in PREFERRED_MODELS:
        pref_base = pref.split(":")[0]
        # Exact match first.
        if pref in local:
            return pref
        # Base name match (e.g. "qwen3" matches "qwen3:8b-instruct").
        if pref_base in local_base:
            return local_base[pref_base]

    # Fall back to any available model.
    return local[0] if local else None


def pull_model(model_name: str) -> dict:
    """Pull an Ollama model.

    Returns:
        {"success": bool, "model": str, "message": str}
    """
    if not is_ollama_installed():
        return {
            "success": False, "model": model_name,
            "message": "Ollama is not installed.",
        }
    if not is_ollama_running():
        return {
            "success": False, "model": model_name,
            "message": "Ollama server is not running. Start it first.",
        }

    try:
        logger.info("Pulling Ollama model: %s", model_name)
        result = subprocess.run(
            ["ollama", "pull", model_name],
            capture_output=True, text=True, timeout=MODEL_PULL_TIMEOUT,
        )
        if result.returncode == 0:
            return {
                "success": True, "model": model_name,
                "message": f"Successfully pulled {model_name}.",
            }
        return {
            "success": False, "model": model_name,
            "message": f"Pull failed: {result.stderr.strip() or result.stdout.strip()}",
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False, "model": model_name,
            "message": f"Pull timed out after {MODEL_PULL_TIMEOUT}s.",
        }
    except FileNotFoundError:
        return {
            "success": False, "model": model_name,
            "message": "ollama binary not found.",
        }


def delete_model(model_name: str) -> dict:
    """Delete a local Ollama model.

    Returns:
        {"success": bool, "model": str, "message": str}
    """
    try:
        result = subprocess.run(
            ["ollama", "rm", model_name],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return {"success": True, "model": model_name, "message": f"Deleted {model_name}."}
        return {"success": False, "model": model_name, "message": result.stderr.strip()}
    except Exception as e:
        return {"success": False, "model": model_name, "message": str(e)}


# ------------------------------------------------------------------ #
#                   ENSURE READY (AUTO-SETUP)                         #
# ------------------------------------------------------------------ #

def ensure_ready(auto_pull: bool = True) -> dict:
    """Ensure Ollama is installed, running, and has a model.

    This is the one-call lifecycle manager:
      1. Check installation → fail fast with install instructions
      2. Start server if not running → launch in background
      3. Pull preferred model if none available → auto-pull

    Args:
        auto_pull: If True, automatically pull the default model when
                   none are available. If False, just report status.

    Returns:
        {"ready": bool, "model": str | None, "steps": list[str], "message": str}
    """
    steps: List[str] = []

    # Step 1: Installation check.
    if not is_ollama_installed():
        return {
            "ready": False,
            "model": None,
            "steps": ["install_needed"],
            "message": (
                "Ollama is not installed.\n"
                "  macOS:   brew install ollama\n"
                "  Linux:   curl -fsSL https://ollama.ai/install.sh | sh\n"
                "  Windows: Download from https://ollama.ai"
            ),
        }

    # Step 2: Start server if needed.
    if not is_ollama_running():
        start_result = start_ollama()
        steps.append("server_started" if start_result["started"] else "server_start_failed")
        if not start_result["started"]:
            return {
                "ready": False,
                "model": None,
                "steps": steps,
                "message": start_result["message"],
            }
    else:
        steps.append("server_already_running")

    # Step 3: Check for models.
    model = pick_best_model()
    if model:
        steps.append(f"model_available:{model}")
        return {
            "ready": True,
            "model": model,
            "steps": steps,
            "message": f"✅ Ready with model: {model}",
        }

    # No model — pull one if allowed.
    if not auto_pull:
        return {
            "ready": False,
            "model": None,
            "steps": steps + ["no_model"],
            "message": (
                "Ollama is running but no model is available.\n"
                f"Recommended: ollama pull {DEFAULT_MODEL}"
            ),
        }

    steps.append(f"pulling_model:{DEFAULT_MODEL}")
    pull_result = pull_model(DEFAULT_MODEL)
    if pull_result["success"]:
        model = pick_best_model() or DEFAULT_MODEL
        steps.append(f"model_pulled:{model}")
        return {
            "ready": True,
            "model": model,
            "steps": steps,
            "message": f"✅ Pulled and ready with model: {model}",
        }

    steps.append("pull_failed")
    return {
        "ready": False,
        "model": None,
        "steps": steps,
        "message": pull_result["message"],
    }


# ------------------------------------------------------------------ #
#                     LLM TRADE PARSING                               #
# ------------------------------------------------------------------ #

def parse_with_llm(
    raw_text: str,
    model: Optional[str] = None,
    source_file: str = "",
    auto_setup: bool = False,
) -> List[Trade]:
    """Send raw PDF text to Ollama for structured trade extraction.

    Args:
        raw_text: The raw text content from a PDF.
        model: Ollama model to use (auto-selects if None).
        source_file: Original PDF filename for metadata.
        auto_setup: If True, automatically start Ollama and pull a model
                    if not already set up.

    Returns:
        List of parsed Trade objects (may be empty).
    """
    if not raw_text.strip():
        return []

    # Auto-setup if requested.
    if auto_setup:
        ready = ensure_ready(auto_pull=True)
        if not ready["ready"]:
            logger.warning("Auto-setup failed: %s", ready["message"])
            return []
        model = model or ready["model"]

    chosen_model = model or pick_best_model()
    if not chosen_model:
        logger.warning("No Ollama model available for LLM parsing")
        return []

    # Truncate very long texts to avoid overwhelming the model.
    text_input = raw_text[:12_000]

    try:
        resp = httpx.post(
            f"{OLLAMA_API_BASE}/api/generate",
            json={
                "model": chosen_model,
                "prompt": f"{EXTRACTION_PROMPT}\n\n--- PDF TEXT ---\n{text_input}",
                "stream": False,
                "options": {
                    "temperature": 0.1,
                    "num_predict": 4096,
                },
            },
            timeout=120.0,
        )

        if resp.status_code != 200:
            logger.warning("Ollama returned status %d", resp.status_code)
            return []

        response_text = resp.json().get("response", "")
        return _parse_llm_response(response_text, source_file)

    except (httpx.ConnectError, httpx.TimeoutException) as e:
        logger.warning("Ollama connection error: %s", e)
        return []
    except Exception as e:
        logger.warning("LLM parsing failed: %s", e)
        return []


def _parse_llm_response(response_text: str, source_file: str) -> List[Trade]:
    """Parse the LLM's JSON response into Trade objects."""
    from datetime import datetime
    from decimal import Decimal, InvalidOperation

    # Extract JSON from the response (handle markdown code blocks).
    text = response_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (``` markers).
        lines = [l for l in lines[1:] if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        trades_data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find a JSON array in the response.
        import re
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                trades_data = json.loads(match.group())
            except json.JSONDecodeError:
                return []
        else:
            return []

    if not isinstance(trades_data, list):
        return []

    trades: List[Trade] = []
    for item in trades_data:
        try:
            if not isinstance(item, dict):
                continue

            trade_date = datetime.strptime(item["date"], "%Y-%m-%d").date()
            symbol = str(item["symbol"]).strip().upper()
            action_str = str(item["action"]).strip().lower()
            action = TradeAction.BUY if action_str == "buy" else TradeAction.SELL

            qty = Decimal(str(item["quantity"]))
            price = Decimal(str(item["price"]))
            amount = Decimal(str(item.get("amount", qty * price)))

            exchange = str(item.get("exchange", "")).strip().upper()
            currency = str(item.get("currency", "INR")).strip().upper()

            trades.append(Trade(
                date=trade_date,
                symbol=symbol,
                action=action,
                quantity=abs(qty),
                price=abs(price),
                amount=abs(amount),
                exchange=exchange,
                currency=currency,
                broker="llm_parsed",
                source_file=source_file,
                notes=f"Parsed via Ollama LLM ({pick_best_model() or 'unknown'})",
            ))
        except (KeyError, ValueError, InvalidOperation):
            continue

    return trades


# ------------------------------------------------------------------ #
#                       STATUS REPORTING                              #
# ------------------------------------------------------------------ #

def setup_status() -> dict:
    """Full status report for the Ollama + model stack.

    Returns a dict with:
      - ollama_installed: bool
      - ollama_running: bool
      - available_models: list of model names
      - recommended_model: str or None
      - ready: bool (True if everything is set up)
      - default_model: str (what we'd auto-pull)
      - system_ram_gb: float
    """
    installed = is_ollama_installed()
    running = False
    models: List[str] = []
    recommended: Optional[str] = None

    if installed:
        running = is_ollama_running()
        if running:
            models = list_local_models()
            recommended = pick_best_model()

    return {
        "ollama_installed": installed,
        "ollama_running": running,
        "available_models": models,
        "recommended_model": recommended,
        "ready": running and recommended is not None,
        "default_model": DEFAULT_MODEL,
        "system_ram_gb": get_system_ram_gb(),
    }


# ------------------------------------------------------------------ #
#                    SYSTEM & CATALOG HELPERS                          #
# ------------------------------------------------------------------ #

def get_system_ram_gb() -> float:
    """Detect total system RAM in GB."""
    import platform
    try:
        if platform.system() == "Darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return int(result.stdout.strip()) / (1024 ** 3)
        elif platform.system() == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal"):
                        kb = int(line.split()[1])
                        return kb / (1024 ** 2)
    except Exception:
        pass
    return 0.0


def get_suggested_models(ram_gb: float = 0) -> List[dict]:
    """Return the model catalog filtered by available RAM.

    Each entry is a dict with all ModelInfo fields + a ``fits_ram`` bool
    and ``status`` string ("available", "downloadable", "too_large").

    Args:
        ram_gb: System RAM in GB. If 0, auto-detect.
    """
    if ram_gb <= 0:
        ram_gb = get_system_ram_gb()

    local_models = list_local_models()
    local_base = {m.split(":")[0] for m in local_models}

    result = []
    for m in _CATALOG_LIST:
        fits = m.ram_gb <= ram_gb if ram_gb > 0 else True

        # Check if already downloaded (exact or base-name match).
        installed = (
            m.name in local_models
            or m.name.split(":")[0] in local_base
        )

        if installed:
            status = "✅ installed"
        elif fits:
            status = "⬇️  downloadable"
        else:
            status = f"⚠️  needs {m.ram_gb}GB RAM"

        result.append({
            "name": m.name,
            "display_name": m.display_name,
            "params": m.params,
            "ram_gb": m.ram_gb,
            "size_mb": m.size_mb,
            "speed": m.speed_tier,
            "accuracy": m.accuracy_tier,
            "description": m.description,
            "strengths": m.strengths,
            "best_for": m.best_for,
            "recommended": m.recommended,
            "fits_ram": fits,
            "installed": installed,
            "status": status,
        })

    return result


def format_model_catalog(ram_gb: float = 0) -> str:
    """Render the model catalog as a human-readable formatted string.

    Groups models by size tier with clear hardware requirements
    and recommendations.
    """
    if ram_gb <= 0:
        ram_gb = get_system_ram_gb()

    models = get_suggested_models(ram_gb)
    lines: List[str] = []

    lines.append("=" * 65)
    lines.append("  🧠 OPEN-SOURCE MODELS FOR FINANCIAL DOCUMENT PARSING")
    lines.append(f"  Your system: {ram_gb:.0f} GB RAM")
    lines.append("=" * 65)
    lines.append("")

    tiers = [
        ("⚡ SMALL / FAST  (runs on 8GB RAM)", "fast"),
        ("🔶 MEDIUM  (needs 12-16GB RAM)", "medium"),
        ("🔷 LARGE  (needs 24-48GB RAM)", "slow"),
    ]

    for tier_label, speed in tiers:
        tier_models = [m for m in models if m["speed"] == speed]
        if not tier_models:
            continue

        lines.append(f"  {tier_label}")
        lines.append("  " + "─" * 55)

        for m in tier_models:
            tag = "⭐" if m["recommended"] else " "
            lines.append(
                f"  {tag} {m['display_name']:25s}  {m['params']:>4s}  "
                f"~{m['size_mb'] / 1000:.1f}GB  [{m['status']}]"
            )
            lines.append(f"       ollama pull {m['name']}")
            lines.append(f"       {m['best_for']}")
            if not m["fits_ram"]:
                lines.append(f"       ⚠ Needs {m['ram_gb']}GB RAM — won't fit on your system")
            lines.append("")

    lines.append("─" * 65)
    lines.append("  ⭐ = Recommended for most users")
    lines.append("")
    lines.append("  To pull a model:  finmerge_model(action='pull', model='qwen3:8b')")
    lines.append("  To auto-setup:    setup_finmerge()")
    lines.append("=" * 65)

    return "\n".join(lines)


def get_model_info(model_name: str) -> Optional[dict]:
    """Get detailed info for a specific model from the catalog.

    Returns None if the model isn't in our catalog (but it can still
    be used — any Ollama model works).
    """
    # Try exact match.
    info = MODEL_CATALOG.get(model_name)
    if info:
        return _model_info_to_dict(info)

    # Try base-name match (e.g. "qwen3" matches "qwen3:8b").
    base = model_name.split(":")[0]
    for m in _CATALOG_LIST:
        if m.name.split(":")[0] == base:
            return _model_info_to_dict(m)

    return None


def _model_info_to_dict(m: ModelInfo) -> dict:
    return {
        "name": m.name,
        "display_name": m.display_name,
        "params": m.params,
        "ram_gb": m.ram_gb,
        "speed_tier": m.speed_tier,
        "accuracy_tier": m.accuracy_tier,
        "size_mb": m.size_mb,
        "description": m.description,
        "strengths": m.strengths,
        "best_for": m.best_for,
        "recommended": m.recommended,
    }

