"""Comprehensive tests for the codehydra-finmerge plugin.

Tests the full pipeline:
  - Model catalog & system detection
  - Parser helpers (dates, decimals, actions, broker detection)
  - FIFO matching & tax classification
  - Report generation (tax report, CSV, Schedule FA)
  - LLM response parsing (JSON extraction)
  - MCP tool registration
  - Privacy: all network calls are localhost-only

All tests run fully offline — no Ollama server or network needed.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import patch, MagicMock
import json

import pytest

# Skip entire module if the finmerge plugin is not installed.
pytest.importorskip("codehydra_finmerge", reason="codehydra-finmerge plugin not installed")

from codehydra_finmerge.models import (
    AssetRegion, GainType, Trade, TradeAction, TaxLot, OpenPosition, TaxSummary,
)
from codehydra_finmerge.classifier import (
    classify_gain, get_tax_rate, get_schedule_name, months_between,
    LTCG_EXEMPTION_INDIAN,
)
from codehydra_finmerge.merger import merge_and_match
from codehydra_finmerge.reporter import (
    generate_tax_report, generate_csv, generate_open_positions_csv,
    generate_schedule_fa,
)
from codehydra_finmerge.parser import (
    detect_broker, parse_statement, _parse_date, _parse_decimal,
    _parse_action, _clean_symbol, _map_columns, _infer_region,
)
from codehydra_finmerge.llm_parser import (
    MODEL_CATALOG, PREFERRED_MODELS, DEFAULT_MODEL, _CATALOG_LIST,
    _parse_llm_response, format_model_catalog, get_suggested_models,
    get_model_info, get_system_ram_gb, setup_status, ModelInfo,
    OLLAMA_API_BASE,
)


# ================================================================== #
#                     MODEL CATALOG TESTS                              #
# ================================================================== #

class TestModelCatalog:
    """Verify the model catalog is well-formed and consistent."""

    def test_catalog_not_empty(self):
        assert len(MODEL_CATALOG) >= 5, "Should have at least 5 models"
        assert len(_CATALOG_LIST) == len(MODEL_CATALOG)

    def test_preferred_models_all_in_catalog(self):
        for name in PREFERRED_MODELS:
            assert name in MODEL_CATALOG, f"{name} should be in catalog"

    def test_default_model_in_catalog(self):
        assert DEFAULT_MODEL in MODEL_CATALOG

    def test_exactly_one_recommended(self):
        recommended = [m for m in _CATALOG_LIST if m.recommended]
        assert len(recommended) == 1, "Exactly one model should be recommended"
        assert recommended[0].name == DEFAULT_MODEL

    def test_model_info_fields(self):
        for m in _CATALOG_LIST:
            assert m.name, f"Model {m} missing name"
            assert m.display_name, f"{m.name} missing display_name"
            assert m.params, f"{m.name} missing params"
            assert m.ram_gb > 0, f"{m.name} has invalid ram_gb"
            assert m.speed_tier in ("fast", "medium", "slow")
            assert m.accuracy_tier in ("good", "great", "excellent")
            assert m.size_mb > 0, f"{m.name} has invalid size_mb"
            assert m.description, f"{m.name} missing description"
            assert m.best_for, f"{m.name} missing best_for"
            assert len(m.strengths) >= 2, f"{m.name} should list ≥2 strengths"

    def test_models_ordered_by_size(self):
        """Models should generally go from small to large RAM."""
        ram_vals = [m.ram_gb for m in _CATALOG_LIST]
        # Not strictly sorted (within tiers), but should be non-decreasing
        # between tier boundaries. Just check first < last.
        assert ram_vals[0] <= ram_vals[-1]

    def test_get_model_info_exact(self):
        info = get_model_info("qwen3:8b")
        assert info is not None
        assert info["name"] == "qwen3:8b"
        assert info["recommended"] is True

    def test_get_model_info_base_name(self):
        info = get_model_info("qwen3")
        assert info is not None
        assert info["name"].startswith("qwen3")

    def test_get_model_info_unknown(self):
        info = get_model_info("nonexistent-model:99b")
        assert info is None

    def test_get_suggested_models_filters_by_ram(self):
        # 8GB system should exclude 24GB+ models
        suggested = get_suggested_models(ram_gb=8)
        for m in suggested:
            if m["ram_gb"] > 8:
                assert not m["fits_ram"]
            else:
                assert m["fits_ram"]

    def test_format_model_catalog_output(self):
        catalog = format_model_catalog(ram_gb=16)
        assert "OPEN-SOURCE MODELS" in catalog
        assert "Your system: 16 GB RAM" in catalog
        assert "qwen3:8b" in catalog
        assert "SMALL / FAST" in catalog
        assert "⭐" in catalog

    def test_system_ram_detection(self):
        ram = get_system_ram_gb()
        # On the test machine, should detect something > 0
        assert ram > 0, "Should detect system RAM"


# ================================================================== #
#                    PRIVACY / LOCALITY TESTS                          #
# ================================================================== #

class TestPrivacyLocality:
    """Ensure all network calls stay on localhost — critical for PII."""

    def test_ollama_api_base_is_localhost(self):
        assert "localhost" in OLLAMA_API_BASE
        assert "11434" in OLLAMA_API_BASE

    def test_no_external_urls_in_code(self):
        """Audit: no httpx calls to anything other than localhost."""
        import inspect
        import codehydra_finmerge.llm_parser as mod

        source = inspect.getsource(mod)
        # Find all httpx.get / httpx.post calls
        import re
        url_calls = re.findall(r'httpx\.\w+\(\s*f?"([^"]+)"', source)
        for url in url_calls:
            # URLs should only reference OLLAMA_API_BASE (localhost)
            assert "localhost" in url or "OLLAMA_API_BASE" in url or "{OLLAMA_API_BASE}" in url, \
                f"Found non-local URL in httpx call: {url}"

    def test_model_catalog_is_static(self):
        """Catalog should not require any network calls to build."""
        # Just accessing the catalog should work offline
        assert len(MODEL_CATALOG) > 0
        assert all(isinstance(m, ModelInfo) for m in _CATALOG_LIST)


# ================================================================== #
#                    CLASSIFIER TESTS                                  #
# ================================================================== #

class TestClassifier:
    def test_indian_stcg(self):
        gain_type, days = classify_gain(
            date(2025, 1, 15), date(2025, 6, 15), AssetRegion.INDIAN
        )
        assert gain_type == GainType.STCG
        assert days == 151

    def test_indian_ltcg(self):
        gain_type, days = classify_gain(
            date(2024, 1, 15), date(2025, 6, 15), AssetRegion.INDIAN
        )
        assert gain_type == GainType.LTCG

    def test_foreign_stcg(self):
        # Foreign requires 24 months for LTCG, so 12 months is still STCG.
        gain_type, _ = classify_gain(
            date(2024, 1, 15), date(2025, 1, 15), AssetRegion.FOREIGN
        )
        assert gain_type == GainType.STCG

    def test_foreign_ltcg(self):
        gain_type, _ = classify_gain(
            date(2023, 1, 15), date(2025, 6, 15), AssetRegion.FOREIGN
        )
        assert gain_type == GainType.LTCG

    def test_months_between(self):
        assert months_between(date(2024, 1, 1), date(2024, 7, 1)) == 6
        assert months_between(date(2024, 1, 1), date(2025, 1, 1)) == 12
        assert months_between(date(2023, 6, 15), date(2025, 6, 15)) == 24

    def test_tax_rates(self):
        assert get_tax_rate(AssetRegion.INDIAN, GainType.STCG) == "20%"
        assert get_tax_rate(AssetRegion.INDIAN, GainType.LTCG) == "12.5%"
        assert get_tax_rate(AssetRegion.FOREIGN, GainType.STCG) == "As per slab"
        assert get_tax_rate(AssetRegion.FOREIGN, GainType.LTCG) == "12.5%"

    def test_schedule_names(self):
        assert "111A" in get_schedule_name(AssetRegion.INDIAN, GainType.STCG)
        assert "112A" in get_schedule_name(AssetRegion.INDIAN, GainType.LTCG)
        assert "Schedule FA" in get_schedule_name(AssetRegion.FOREIGN, GainType.STCG)

    def test_ltcg_exemption_value(self):
        assert LTCG_EXEMPTION_INDIAN == 125_000


# ================================================================== #
#                     PARSER TESTS                                     #
# ================================================================== #

class TestParserHelpers:
    def test_detect_broker_zerodha(self):
        assert detect_broker("ZERODHA Broking Ltd Contract Note") == "zerodha"

    def test_detect_broker_groww(self):
        assert detect_broker("Trade book from Groww") == "groww"

    def test_detect_broker_angelone(self):
        assert detect_broker("Angel One Securities Report") == "angelone"

    def test_detect_broker_interactive_brokers(self):
        assert detect_broker("Interactive Brokers Activity Statement") == "interactive_brokers"

    def test_detect_broker_schwab(self):
        assert detect_broker("Charles Schwab Brokerage Statement") == "charles_schwab"

    def test_detect_broker_vested(self):
        assert detect_broker("Vested Finance Trade Report") == "vested"

    def test_detect_broker_unknown(self):
        assert detect_broker("Some random broker") == "unknown"

    def test_parse_date_formats(self):
        assert _parse_date("2025-01-15") == date(2025, 1, 15)
        assert _parse_date("15-01-2025") == date(2025, 1, 15)
        assert _parse_date("15/01/2025") == date(2025, 1, 15)
        assert _parse_date("15-Jan-2025") == date(2025, 1, 15)
        assert _parse_date("15 Jan 2025") == date(2025, 1, 15)
        assert _parse_date("01/15/2025") == date(2025, 1, 15)

    def test_parse_date_invalid(self):
        assert _parse_date("") is None
        assert _parse_date("not-a-date") is None

    def test_parse_decimal(self):
        assert _parse_decimal("1,234.56") == Decimal("1234.56")
        assert _parse_decimal("₹500.00") == Decimal("500.00")
        assert _parse_decimal("$100") == Decimal("100")
        assert _parse_decimal("(50.25)") == Decimal("-50.25")
        assert _parse_decimal("€1000") == Decimal("1000")
        assert _parse_decimal("") is None
        assert _parse_decimal("abc") is None

    def test_parse_action(self):
        assert _parse_action("Buy") == TradeAction.BUY
        assert _parse_action("SELL") == TradeAction.SELL
        assert _parse_action("B") == TradeAction.BUY
        assert _parse_action("S") == TradeAction.SELL
        assert _parse_action("bought") == TradeAction.BUY
        assert _parse_action("sold") == TradeAction.SELL
        assert _parse_action("random") is None

    def test_clean_symbol(self):
        assert _clean_symbol("RELIANCE-EQ") == "RELIANCE"
        assert _clean_symbol("INFY.NS") == "INFY"
        assert _clean_symbol("TCS.BO") == "TCS"
        assert _clean_symbol("  aapl  ") == "AAPL"

    def test_map_columns(self):
        headers = ["Trade Date", "Symbol", "Buy/Sell", "Qty", "Price", "Amount"]
        mapping = _map_columns(headers)
        assert "date" in mapping
        assert "symbol" in mapping
        assert "action" in mapping
        assert "quantity" in mapping
        assert "price" in mapping

    def test_infer_region_foreign_broker(self):
        assert _infer_region("some text", "vested") == AssetRegion.FOREIGN
        assert _infer_region("some text", "interactive_brokers") == AssetRegion.FOREIGN

    def test_infer_region_indian_default(self):
        assert _infer_region("some text", "zerodha") == AssetRegion.INDIAN
        assert _infer_region("NSE equity segment", "unknown") == AssetRegion.INDIAN

    def test_infer_region_from_text(self):
        assert _infer_region("Trading in USD currency", "unknown") == AssetRegion.FOREIGN


class TestTableParsing:
    def test_parse_statement_from_tables(self):
        extracted = {
            "text": "Zerodha contract note",
            "tables": [
                [
                    ["Trade Date", "Symbol", "Buy/Sell", "Qty", "Price", "Amount"],
                    ["2025-03-15", "RELIANCE", "Buy", "10", "2450.50", "24505.00"],
                    ["2025-03-16", "TCS", "Sell", "5", "3200.00", "16000.00"],
                ]
            ],
            "file": "test.pdf",
        }
        trades = parse_statement(extracted)
        assert len(trades) == 2
        assert trades[0].symbol == "RELIANCE"
        assert trades[0].action == TradeAction.BUY
        assert trades[0].quantity == Decimal("10")
        assert trades[1].symbol == "TCS"
        assert trades[1].action == TradeAction.SELL


# ================================================================== #
#                     MERGER (FIFO) TESTS                               #
# ================================================================== #

def _make_test_trades():
    """Create a standard set of test trades."""
    return [
        Trade(date=date(2024, 3, 1), symbol="RELIANCE", action=TradeAction.BUY,
              quantity=Decimal("10"), price=Decimal("2500"), amount=Decimal("25000"),
              exchange="NSE", currency="INR", broker="zerodha"),
        Trade(date=date(2024, 6, 1), symbol="RELIANCE", action=TradeAction.BUY,
              quantity=Decimal("5"), price=Decimal("2600"), amount=Decimal("13000"),
              exchange="NSE", currency="INR", broker="zerodha"),
        Trade(date=date(2024, 9, 1), symbol="RELIANCE", action=TradeAction.SELL,
              quantity=Decimal("12"), price=Decimal("2800"), amount=Decimal("33600"),
              exchange="NSE", currency="INR", broker="zerodha"),
        # Foreign trade
        Trade(date=date(2023, 1, 15), symbol="AAPL", action=TradeAction.BUY,
              quantity=Decimal("5"), price=Decimal("150"), amount=Decimal("750"),
              exchange="NASDAQ", currency="USD", broker="vested"),
        Trade(date=date(2025, 6, 1), symbol="AAPL", action=TradeAction.SELL,
              quantity=Decimal("3"), price=Decimal("200"), amount=Decimal("600"),
              exchange="NASDAQ", currency="USD", broker="vested"),
    ]


class TestMerger:
    def test_fifo_basic(self):
        summary = merge_and_match(_make_test_trades())
        assert summary.total_trades == 5
        # RELIANCE: 10+2=12 sold → 2 lots; AAPL: 3 sold → 1 lot
        assert summary.total_lots == 3
        # Open: 3 RELIANCE (5-2) + 2 AAPL (5-3)
        assert len(summary.open_positions) == 2

    def test_fifo_gain_calculation(self):
        summary = merge_and_match(_make_test_trades())
        reliance_lots = [l for l in summary.tax_lots if l.symbol == "RELIANCE"]
        assert len(reliance_lots) == 2
        # Lot 1: 10 × (2800 - 2500) = 3000
        # Lot 2: 2 × (2800 - 2600) = 400
        assert sum(l.gain for l in reliance_lots) == Decimal("3400")

    def test_fifo_reliance_stcg(self):
        summary = merge_and_match(_make_test_trades())
        for lot in summary.tax_lots:
            if lot.symbol == "RELIANCE":
                assert lot.gain_type == GainType.STCG
                assert lot.region == AssetRegion.INDIAN

    def test_foreign_ltcg(self):
        summary = merge_and_match(_make_test_trades())
        aapl_lots = [l for l in summary.tax_lots if l.symbol == "AAPL"]
        assert len(aapl_lots) == 1
        lot = aapl_lots[0]
        assert lot.gain_type == GainType.LTCG
        assert lot.region == AssetRegion.FOREIGN
        assert lot.gain == Decimal("150")  # 3 × (200 - 150)

    def test_unmatched_sell_warning(self):
        trades = [
            Trade(date=date(2025, 1, 1), symbol="TCS", action=TradeAction.SELL,
                  quantity=Decimal("5"), price=Decimal("3000"), amount=Decimal("15000"),
                  exchange="NSE", currency="INR"),
        ]
        summary = merge_and_match(trades)
        assert len(summary.errors) == 1
        assert "TCS" in summary.errors[0]

    def test_same_day_buy_sell(self):
        trades = [
            Trade(date=date(2025, 1, 1), symbol="INFY", action=TradeAction.BUY,
                  quantity=Decimal("10"), price=Decimal("1500"), amount=Decimal("15000"),
                  exchange="NSE", currency="INR"),
            Trade(date=date(2025, 1, 1), symbol="INFY", action=TradeAction.SELL,
                  quantity=Decimal("10"), price=Decimal("1520"), amount=Decimal("15200"),
                  exchange="NSE", currency="INR"),
        ]
        summary = merge_and_match(trades)
        assert summary.total_lots == 1
        assert summary.tax_lots[0].gain == Decimal("200")

    def test_partial_sell(self):
        trades = [
            Trade(date=date(2025, 1, 1), symbol="HDFC", action=TradeAction.BUY,
                  quantity=Decimal("100"), price=Decimal("1000"), amount=Decimal("100000"),
                  exchange="NSE", currency="INR"),
            Trade(date=date(2025, 2, 1), symbol="HDFC", action=TradeAction.SELL,
                  quantity=Decimal("30"), price=Decimal("1100"), amount=Decimal("33000"),
                  exchange="NSE", currency="INR"),
        ]
        summary = merge_and_match(trades)
        assert summary.total_lots == 1
        assert summary.tax_lots[0].quantity == Decimal("30")
        assert len(summary.open_positions) == 1
        assert summary.open_positions[0].quantity == Decimal("70")

    def test_multi_broker_merge(self):
        """Trades from different brokers for the same symbol should merge."""
        trades = [
            Trade(date=date(2024, 1, 1), symbol="RELIANCE", action=TradeAction.BUY,
                  quantity=Decimal("5"), price=Decimal("2500"), amount=Decimal("12500"),
                  exchange="NSE", currency="INR", broker="zerodha"),
            Trade(date=date(2024, 2, 1), symbol="RELIANCE", action=TradeAction.BUY,
                  quantity=Decimal("5"), price=Decimal("2600"), amount=Decimal("13000"),
                  exchange="NSE", currency="INR", broker="groww"),
            Trade(date=date(2024, 6, 1), symbol="RELIANCE", action=TradeAction.SELL,
                  quantity=Decimal("8"), price=Decimal("2800"), amount=Decimal("22400"),
                  exchange="NSE", currency="INR", broker="zerodha"),
        ]
        summary = merge_and_match(trades)
        # FIFO: 5 from zerodha @2500 + 3 from groww @2600
        assert summary.total_lots == 2
        assert summary.tax_lots[0].buy_broker == "zerodha"
        assert summary.tax_lots[1].buy_broker == "groww"


# ================================================================== #
#                      REPORTER TESTS                                  #
# ================================================================== #

class TestReporter:
    def test_tax_report_sections(self):
        summary = merge_and_match(_make_test_trades())
        report = generate_tax_report(summary)
        assert "CAPITAL GAINS TAX REPORT" in report
        assert "INDIAN EQUITY" in report
        assert "FOREIGN ASSETS" in report
        assert "Section 111A" in report or "STCG" in report
        assert "Schedule FA" in report
        assert "DISCLAIMER" in report

    def test_tax_report_open_positions(self):
        summary = merge_and_match(_make_test_trades())
        report = generate_tax_report(summary)
        assert "OPEN POSITIONS" in report

    def test_csv_output(self):
        summary = merge_and_match(_make_test_trades())
        csv_str = generate_csv(summary.tax_lots)
        assert "Symbol" in csv_str
        assert "RELIANCE" in csv_str
        assert "AAPL" in csv_str
        lines = csv_str.strip().split("\n")
        assert len(lines) == 4  # header + 3 lots

    def test_open_positions_csv(self):
        summary = merge_and_match(_make_test_trades())
        csv_str = generate_open_positions_csv(summary.open_positions)
        assert "Symbol" in csv_str
        lines = csv_str.strip().split("\n")
        assert len(lines) == 3  # header + 2 positions

    def test_schedule_fa(self):
        summary = merge_and_match(_make_test_trades())
        fa = generate_schedule_fa(summary)
        assert "AAPL" in fa
        assert "Schedule FA" in fa or "SCHEDULE FA" in fa
        assert "INR" in fa or "SBI TT" in fa

    def test_schedule_fa_no_foreign(self):
        trades = [
            Trade(date=date(2025, 1, 1), symbol="RELIANCE", action=TradeAction.BUY,
                  quantity=Decimal("10"), price=Decimal("2500"), amount=Decimal("25000"),
                  exchange="NSE", currency="INR"),
        ]
        summary = merge_and_match(trades)
        fa = generate_schedule_fa(summary)
        assert "No foreign assets" in fa


# ================================================================== #
#                    LLM RESPONSE PARSING TESTS                        #
# ================================================================== #

class TestLLMResponseParsing:
    """Test JSON parsing from LLM responses — no network needed."""

    def test_parse_clean_json(self):
        response = json.dumps([
            {"date": "2025-03-15", "symbol": "RELIANCE", "action": "buy",
             "quantity": 10, "price": 2450.50, "amount": 24505.00,
             "exchange": "NSE", "currency": "INR"},
        ])
        trades = _parse_llm_response(response, "test.pdf")
        assert len(trades) == 1
        assert trades[0].symbol == "RELIANCE"
        assert trades[0].action == TradeAction.BUY
        assert trades[0].quantity == Decimal("10")

    def test_parse_markdown_wrapped_json(self):
        response = "```json\n" + json.dumps([
            {"date": "2025-01-01", "symbol": "TCS", "action": "sell",
             "quantity": 5, "price": 3200, "amount": 16000,
             "exchange": "NSE", "currency": "INR"},
        ]) + "\n```"
        trades = _parse_llm_response(response, "test.pdf")
        assert len(trades) == 1
        assert trades[0].symbol == "TCS"

    def test_parse_json_with_surrounding_text(self):
        response = "Here are the trades I found:\n" + json.dumps([
            {"date": "2024-06-20", "symbol": "AAPL", "action": "sell",
             "quantity": 5, "price": 195, "amount": 975,
             "exchange": "NASDAQ", "currency": "USD"},
        ]) + "\nLet me know if you need more details."
        trades = _parse_llm_response(response, "test.pdf")
        assert len(trades) == 1
        assert trades[0].symbol == "AAPL"
        assert trades[0].region == AssetRegion.FOREIGN

    def test_parse_empty_array(self):
        trades = _parse_llm_response("[]", "test.pdf")
        assert trades == []

    def test_parse_garbage(self):
        trades = _parse_llm_response("I couldn't find any trades.", "test.pdf")
        assert trades == []

    def test_parse_partial_valid(self):
        """Should keep valid trades and skip broken ones."""
        response = json.dumps([
            {"date": "2025-01-01", "symbol": "INFY", "action": "buy",
             "quantity": 10, "price": 1500, "amount": 15000,
             "exchange": "NSE", "currency": "INR"},
            {"date": "invalid", "symbol": "BAD"},  # broken
            {"date": "2025-02-01", "symbol": "TCS", "action": "sell",
             "quantity": 5, "price": 3000, "amount": 15000,
             "exchange": "NSE", "currency": "INR"},
        ])
        trades = _parse_llm_response(response, "test.pdf")
        assert len(trades) == 2
        assert trades[0].symbol == "INFY"
        assert trades[1].symbol == "TCS"

    def test_parse_auto_calculates_amount(self):
        response = json.dumps([
            {"date": "2025-01-01", "symbol": "HDFC", "action": "buy",
             "quantity": 10, "price": 1500},  # no amount field
        ])
        trades = _parse_llm_response(response, "test.pdf")
        assert len(trades) == 1
        assert trades[0].amount == Decimal("15000")


# ================================================================== #
#                   MODEL / DATACLASS TESTS                            #
# ================================================================== #

class TestModels:
    def test_trade_auto_region_from_exchange(self):
        t = Trade(date=date(2025, 1, 1), symbol="MSFT", action=TradeAction.BUY,
                  quantity=Decimal("1"), price=Decimal("400"), amount=Decimal("400"),
                  exchange="NASDAQ", currency="INR")
        assert t.region == AssetRegion.FOREIGN

    def test_trade_auto_region_from_currency(self):
        t = Trade(date=date(2025, 1, 1), symbol="INFY", action=TradeAction.BUY,
                  quantity=Decimal("10"), price=Decimal("1500"), amount=Decimal("15000"),
                  exchange="", currency="USD")
        assert t.region == AssetRegion.FOREIGN

    def test_trade_default_indian(self):
        t = Trade(date=date(2025, 1, 1), symbol="RELIANCE", action=TradeAction.BUY,
                  quantity=Decimal("10"), price=Decimal("2500"), amount=Decimal("25000"),
                  exchange="NSE", currency="INR")
        assert t.region == AssetRegion.INDIAN

    def test_tax_lot_gain_pct(self):
        lot = TaxLot(
            symbol="TEST", region=AssetRegion.INDIAN,
            buy_date=date(2025, 1, 1), sell_date=date(2025, 6, 1),
            quantity=Decimal("10"), buy_price=Decimal("100"),
            sell_price=Decimal("120"), buy_amount=Decimal("1000"),
            sell_amount=Decimal("1200"), gain=Decimal("200"),
            gain_type=GainType.STCG, holding_days=151,
        )
        assert lot.gain_pct == Decimal("20.00")

    def test_tax_summary_defaults(self):
        s = TaxSummary()
        assert s.total_trades == 0
        assert s.total_lots == 0
        assert s.indian_stcg == Decimal("0")
        assert s.foreign_ltcg == Decimal("0")
        assert s.tax_lots == []
        assert s.open_positions == []


# ================================================================== #
#                    MCP TOOL REGISTRATION TEST                        #
# ================================================================== #

class TestMCPRegistration:
    def test_register_tools(self):
        """Verify register_tools doesn't crash and registers on a FastMCP."""
        from mcp.server.fastmcp import FastMCP
        from codehydra_finmerge import register_tools

        test_mcp = FastMCP("test-server")
        register_tools(test_mcp)
        # If we get here without error, tools registered fine.


# ================================================================== #
#                   SETUP STATUS (MOCKED) TESTS                        #
# ================================================================== #

class TestSetupStatusMocked:
    """Test setup_status with mocked Ollama responses."""

    @patch("codehydra_finmerge.llm_parser.is_ollama_installed", return_value=False)
    def test_not_installed(self, _):
        status = setup_status()
        assert status["ollama_installed"] is False
        assert status["ready"] is False

    @patch("codehydra_finmerge.llm_parser.is_ollama_installed", return_value=True)
    @patch("codehydra_finmerge.llm_parser.is_ollama_running", return_value=False)
    def test_not_running(self, *_):
        status = setup_status()
        assert status["ollama_installed"] is True
        assert status["ollama_running"] is False
        assert status["ready"] is False

    @patch("codehydra_finmerge.llm_parser.is_ollama_installed", return_value=True)
    @patch("codehydra_finmerge.llm_parser.is_ollama_running", return_value=True)
    @patch("codehydra_finmerge.llm_parser.list_local_models", return_value=["qwen3:8b"])
    @patch("codehydra_finmerge.llm_parser.pick_best_model", return_value="qwen3:8b")
    def test_fully_ready(self, *_):
        status = setup_status()
        assert status["ollama_installed"] is True
        assert status["ollama_running"] is True
        assert status["ready"] is True
        assert status["recommended_model"] == "qwen3:8b"
