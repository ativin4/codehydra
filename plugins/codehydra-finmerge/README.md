# codehydra-finmerge

**FinMerge** is a CodeHydra plugin that processes PDF trade statements from multiple Indian and international brokers, merges all buy/sell transactions, and generates tax-ready capital gains reports for Indian ITR filing.

> **Privacy-first**: All processing happens locally. Your financial data never leaves your machine. LLM parsing uses a local Ollama model — no cloud APIs.

## Features

- **Multi-broker PDF parsing** — Zerodha, Groww, Angel One, ICICI Direct, Interactive Brokers, Schwab, Vested, and more
- **FIFO matching** — First-In-First-Out lot matching as required by Indian tax law
- **Tax classification** — Automatic STCG/LTCG classification for both Indian (§111A/§112A) and Foreign (§112) assets
- **Schedule FA** — Foreign asset declaration data for ITR-2/ITR-3
- **LLM-assisted parsing** — Local Ollama model for intelligent extraction from messy PDFs
- **Full Ollama lifecycle** — Auto-start server, curated model catalog, one-click setup
- **11 curated models** — Hardware-aware suggestions from 3.8B to 70B parameters

## Installation

```bash
# From the CodeHydra project root:
uv pip install -e plugins/codehydra-finmerge

# Or with pip:
pip install -e plugins/codehydra-finmerge
```

The plugin is auto-discovered by CodeHydra via entry_points — no configuration needed.

## LLM Setup

The plugin manages Ollama for you. Use the `setup_finmerge` MCP tool:

```
# Shows a curated model catalog filtered by your hardware
setup_finmerge()

# One-click: starts Ollama + pulls a model
setup_finmerge(model='qwen3:8b')

# Or fully automatic
setup_finmerge(auto_pull=True)
```

### Manual setup (alternative)

```bash
# Install Ollama
brew install ollama          # macOS
curl -fsSL https://ollama.ai/install.sh | sh  # Linux

# The plugin handles the rest — or do it manually:
ollama serve
ollama pull qwen3:8b
```

## Model Catalog

The plugin includes a curated catalog of open-source models ranked for financial document parsing:

| Model | Params | RAM | Speed | Accuracy | Best For |
|-------|--------|-----|-------|----------|----------|
| ⭐ **Qwen 3 8B** | 8B | 6GB | Fast | Great | Most users — best speed/accuracy |
| Google Gemma 3 4B | 4B | 4GB | Fast | Good | Low-RAM / fastest processing |
| Microsoft Phi-4 Mini | 3.8B | 4GB | Fast | Good | Minimal resource environments |
| Meta Llama 3.1 8B | 8B | 6GB | Fast | Great | Reliable general-purpose |
| Mistral 7B | 7B | 6GB | Fast | Good | Standard broker formats |
| Qwen 3 14B | 14B | 12GB | Medium | Excellent | Complex/messy PDFs |
| Google Gemma 3 12B | 12B | 10GB | Medium | Great | Step up from 8B |
| Mistral Small 24B | 24B | 16GB | Medium | Excellent | High accuracy |
| Qwen 3 32B | 32B | 24GB | Slow | Excellent | Maximum accuracy |
| DeepSeek R1 32B | 32B | 24GB | Slow | Excellent | Ambiguous documents |
| Meta Llama 3.3 70B | 70B | 48GB | Slow | Excellent | Maximum power |

Browse interactively: `finmerge_model(action='suggest')`

## MCP Tools

Once installed, CodeHydra auto-discovers these tools:

| Tool | Description |
|------|-------------|
| `process_trade_statements` | Batch-process PDFs → merged tax reports. Auto-starts Ollama & pulls models. |
| `preview_statement` | Dry-run a single PDF to verify parsing before full batch. |
| `setup_finmerge` | One-click setup: start Ollama + show model catalog + pull model. |
| `finmerge_model` | Browse, pull, delete, and inspect open-source models. |
| `stop_finmerge` | Stop the Ollama server if started by FinMerge. |

### Quick Start

```
# Process all PDFs in a folder — fully automatic
process_trade_statements(directory='~/Documents/trade_statements/')

# Preview a single PDF first
preview_statement(file_path='~/Documents/zerodha_mar2025.pdf')

# Browse available models
finmerge_model(action='suggest')

# Get details on a specific model
finmerge_model(action='info', model='qwen3:14b')
```

## Outputs

After processing, the plugin generates:

| File | Contents |
|------|----------|
| `tax_report.txt` | Human-readable ITR-ready capital gains report |
| `matched_lots.csv` | Every FIFO-matched buy–sell pair with gain/loss |
| `open_positions.csv` | Shares still held (unrealized positions) |
| `schedule_fa.txt` | Foreign asset declarations for Schedule FA |
| `processing_log.json` | Per-file extraction status and parse method |

Default output directory: `.codehydra/finmerge/`

## Tax Rules Applied

**Indian listed equity (STT paid):**
- STCG: held < 12 months → 20% (Section 111A)
- LTCG: held ≥ 12 months → 12.5% above ₹1.25L exemption (Section 112A)

**Foreign assets:**
- STCG: held < 24 months → as per income-tax slab
- LTCG: held ≥ 24 months → 12.5% without indexation (Section 112)

> ⚠️ Schedule FA: All foreign stock holdings must be declared in Schedule FA of ITR-2/ITR-3, regardless of whether you realized any gains.

## Architecture

```
PDF → pdfplumber (extract) → heuristic parser → FIFO merger → tax reporter
                                   ↓ (fallback)
                           Ollama LLM parser (local)
```

- **Heuristic parser**: Regex-based table detection for known broker formats
- **LLM parser**: Sends raw text to local Ollama for structured JSON extraction
- **FIFO merger**: Matches buys to sells chronologically, per-symbol
- **Tax classifier**: Applies Indian tax rules based on holding period and asset region

## Privacy & Security

- **100% local processing** — no data leaves your machine
- **No cloud APIs** — all LLM calls go to `localhost:11434` (Ollama)
- **No telemetry** — no usage tracking or analytics
- **Audited** — automated tests verify no external network calls in code
