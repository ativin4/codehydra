"""Report generation for tax filing and portfolio summary.

Generates:
  1. CSV trade log of all FIFO-matched lots (realized gains)
  2. CSV of open (unrealized) positions
  3. Human-readable capital-gains tax report for ITR filing
  4. Schedule FA detail for foreign-asset reporting
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import List, Optional

from .models import AssetRegion, GainType, OpenPosition, TaxLot, TaxSummary
from .classifier import get_tax_rate, get_schedule_name, LTCG_EXEMPTION_INDIAN


# ------------------------------------------------------------------ #
#                          FORMATTING                                #
# ------------------------------------------------------------------ #

def _fmt(value: Decimal, places: int = 2) -> str:
    """Format a Decimal with commas and fixed decimal places."""
    q = value.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP)
    return f"{q:,}"


# ------------------------------------------------------------------ #
#                          CSV REPORTS                               #
# ------------------------------------------------------------------ #

def generate_csv(
    tax_lots: List[TaxLot],
    output_path: Optional[Path] = None,
) -> str:
    """CSV of all FIFO-matched lots.  Returns the CSV string and
    optionally writes to *output_path*."""
    buf = io.StringIO()
    w = csv.writer(buf)

    w.writerow([
        "Symbol", "Region", "Buy Date", "Sell Date", "Holding Days",
        "Gain Type", "Quantity", "Buy Price", "Sell Price",
        "Buy Amount", "Sell Amount", "Gain/Loss", "Gain %",
        "Currency", "Exchange", "Tax Rate", "ITR Schedule",
    ])

    for lot in sorted(tax_lots, key=lambda l: (l.region.value, l.sell_date, l.symbol)):
        w.writerow([
            lot.symbol,
            lot.region.value.upper(),
            lot.buy_date.isoformat(),
            lot.sell_date.isoformat(),
            lot.holding_days,
            lot.gain_type.value.upper(),
            str(lot.quantity),
            _fmt(lot.buy_price),
            _fmt(lot.sell_price),
            _fmt(lot.buy_amount),
            _fmt(lot.sell_amount),
            _fmt(lot.gain),
            f"{lot.gain_pct}%",
            lot.currency,
            lot.exchange,
            get_tax_rate(lot.region, lot.gain_type),
            get_schedule_name(lot.region, lot.gain_type),
        ])

    result = buf.getvalue()
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result)
    return result


def generate_open_positions_csv(
    positions: List[OpenPosition],
    output_path: Optional[Path] = None,
) -> str:
    """CSV of open (unrealized) positions."""
    buf = io.StringIO()
    w = csv.writer(buf)

    w.writerow([
        "Symbol", "Region", "Buy Date", "Quantity", "Buy Price",
        "Invested Amount", "Currency", "Exchange", "Broker",
    ])

    for p in sorted(positions, key=lambda x: (x.region.value, x.symbol, x.date)):
        w.writerow([
            p.symbol,
            p.region.value.upper(),
            p.date.isoformat(),
            str(p.quantity),
            _fmt(p.price),
            _fmt(p.amount),
            p.currency,
            p.exchange,
            p.broker,
        ])

    result = buf.getvalue()
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result)
    return result


# ------------------------------------------------------------------ #
#                    HUMAN-READABLE TAX REPORT                       #
# ------------------------------------------------------------------ #

def generate_tax_report(summary: TaxSummary) -> str:
    """Generate a comprehensive capital-gains tax report for ITR filing."""
    L: List[str] = []

    L.append("=" * 70)
    L.append("  CAPITAL GAINS TAX REPORT — INDIAN INCOME TAX")
    L.append(f"  Generated: {date.today().isoformat()}")
    L.append("=" * 70)
    L.append("")

    # ---- Overview ----
    L.append("📊 OVERVIEW")
    L.append("─" * 40)
    L.append(f"  Total Trades Parsed:        {summary.total_trades}")
    L.append(f"  FIFO Matched Lots:          {summary.total_lots}")
    L.append(f"  Open Positions:             {len(summary.open_positions)}")
    L.append(f"  Total Buy Value:            ₹{_fmt(summary.total_buy_value)}")
    L.append(f"  Total Sell Value:           ₹{_fmt(summary.total_sell_value)}")
    L.append(f"  Net Realized Gain/Loss:     ₹{_fmt(summary.total_realized_gain)}")
    L.append("")

    # ---- Indian Equity ----
    has_indian = summary.indian_stcg_count + summary.indian_ltcg_count > 0
    if has_indian:
        L.append("🇮🇳 INDIAN EQUITY (Schedule CG)")
        L.append("─" * 40)

        if summary.indian_stcg_count:
            L.append("  STCG (Section 111A — held < 12 months)")
            L.append(f"    Lots:       {summary.indian_stcg_count}")
            L.append(f"    Gain/Loss:  ₹{_fmt(summary.indian_stcg)}")
            L.append(f"    Tax Rate:   {get_tax_rate(AssetRegion.INDIAN, GainType.STCG)}")
            L.append("")

        if summary.indian_ltcg_count:
            exemption = Decimal(str(LTCG_EXEMPTION_INDIAN))
            taxable = max(summary.indian_ltcg - exemption, Decimal("0"))
            L.append("  LTCG (Section 112A — held ≥ 12 months)")
            L.append(f"    Lots:          {summary.indian_ltcg_count}")
            L.append(f"    Gain/Loss:     ₹{_fmt(summary.indian_ltcg)}")
            L.append(f"    Exemption:     ₹{_fmt(exemption)} (u/s 112A)")
            L.append(f"    Taxable LTCG:  ₹{_fmt(taxable)}")
            L.append(f"    Tax Rate:      {get_tax_rate(AssetRegion.INDIAN, GainType.LTCG)}")
            L.append("")

    # ---- Foreign Assets ----
    has_foreign = summary.foreign_stcg_count + summary.foreign_ltcg_count > 0
    if has_foreign:
        L.append("🌍 FOREIGN ASSETS (Schedule CG + Schedule FA)")
        L.append("─" * 40)
        L.append("  ⚠ Foreign assets MUST be declared in Schedule FA of ITR")
        L.append("  ⚠ Check DTAA provisions for tax credit on foreign tax paid")
        L.append("")

        if summary.foreign_stcg_count:
            L.append("  STCG (held < 24 months)")
            L.append(f"    Lots:       {summary.foreign_stcg_count}")
            L.append(f"    Gain/Loss:  ₹{_fmt(summary.foreign_stcg)}")
            L.append(f"    Tax Rate:   {get_tax_rate(AssetRegion.FOREIGN, GainType.STCG)}")
            L.append("")

        if summary.foreign_ltcg_count:
            L.append("  LTCG (Section 112 — held ≥ 24 months)")
            L.append(f"    Lots:       {summary.foreign_ltcg_count}")
            L.append(f"    Gain/Loss:  ₹{_fmt(summary.foreign_ltcg)}")
            L.append(f"    Tax Rate:   {get_tax_rate(AssetRegion.FOREIGN, GainType.LTCG)}")
            L.append("")

    if not has_indian and not has_foreign:
        L.append("  No realized capital gains found.")
        L.append("")

    # ---- Per-Stock Breakdown ----
    if summary.tax_lots:
        L.append("📈 PER-STOCK BREAKDOWN")
        L.append("─" * 40)
        _append_stock_breakdown(L, summary.tax_lots)
        L.append("")

    # ---- Open Positions ----
    if summary.open_positions:
        L.append("📦 OPEN POSITIONS (unrealized)")
        L.append("─" * 40)

        indian_pos  = [p for p in summary.open_positions if p.region == AssetRegion.INDIAN]
        foreign_pos = [p for p in summary.open_positions if p.region == AssetRegion.FOREIGN]

        if indian_pos:
            total_inv = sum(p.amount for p in indian_pos)
            L.append(f"  Indian: {len(indian_pos)} positions, invested ₹{_fmt(total_inv)}")
            for p in sorted(indian_pos, key=lambda x: x.symbol):
                L.append(f"    {p.symbol}: {p.quantity} @ ₹{_fmt(p.price)} (bought {p.date})")

        if foreign_pos:
            total_inv = sum(p.amount for p in foreign_pos)
            cur = foreign_pos[0].currency
            L.append(f"  Foreign: {len(foreign_pos)} positions, invested {cur} {_fmt(total_inv)}")
            for p in sorted(foreign_pos, key=lambda x: x.symbol):
                L.append(f"    {p.symbol}: {p.quantity} @ {p.currency} {_fmt(p.price)} (bought {p.date})")

        L.append("")

    # ---- Warnings ----
    if summary.errors:
        L.append("⚠ WARNINGS")
        L.append("─" * 40)
        for err in summary.errors:
            L.append(f"  {err}")
        L.append("")

    # ---- Disclaimer ----
    L.append("─" * 70)
    L.append("DISCLAIMER: This report is auto-generated from parsed trade statements.")
    L.append("Verify all figures against original broker statements before ITR filing.")
    L.append("Consult a CA for complex cases (F&O, intraday, DTAA claims, currency")
    L.append("conversion for FA). Tax rates per Finance Act 2024 — check for amendments.")
    L.append("=" * 70)

    return "\n".join(L)


def _append_stock_breakdown(lines: List[str], lots: List[TaxLot]) -> None:
    """Add a per-symbol gain/loss summary to *lines*."""
    from collections import defaultdict

    by_symbol: dict[str, dict] = defaultdict(lambda: {
        "gain": Decimal("0"), "lots": 0, "region": None, "currency": "INR",
    })
    for lot in lots:
        d = by_symbol[lot.symbol]
        d["gain"] += lot.gain
        d["lots"] += 1
        d["region"] = lot.region
        d["currency"] = lot.currency

    for symbol in sorted(by_symbol):
        d = by_symbol[symbol]
        tag = "🇮🇳" if d["region"] == AssetRegion.INDIAN else "🌍"
        sign = "+" if d["gain"] >= 0 else ""
        cur = "₹" if d["currency"] == "INR" else d["currency"] + " "
        lines.append(
            f"  {tag} {symbol:20s}  {d['lots']:3d} lots  →  {sign}{cur}{_fmt(d['gain'])}"
        )


# ------------------------------------------------------------------ #
#                        SCHEDULE FA                                 #
# ------------------------------------------------------------------ #

def generate_schedule_fa(summary: TaxSummary) -> str:
    """Generate Schedule FA (Foreign Assets) data for ITR filing."""
    foreign_lots = [l for l in summary.tax_lots if l.region == AssetRegion.FOREIGN]
    foreign_pos  = [p for p in summary.open_positions if p.region == AssetRegion.FOREIGN]

    if not foreign_lots and not foreign_pos:
        return "No foreign assets to report in Schedule FA."

    L: List[str] = []
    L.append("=" * 60)
    L.append("  SCHEDULE FA — FOREIGN ASSETS DETAIL")
    L.append("=" * 60)
    L.append("")
    L.append("Use this data to fill Schedule FA in your ITR-2/ITR-3.")
    L.append("Each foreign stock holding must be individually declared.")
    L.append("")

    symbols = sorted({l.symbol for l in foreign_lots} | {p.symbol for p in foreign_pos})

    for symbol in symbols:
        L.append(f"  📌 {symbol}")

        sym_lots = [l for l in foreign_lots if l.symbol == symbol]
        sym_pos  = [p for p in foreign_pos  if p.symbol == symbol]

        if sym_lots:
            total_gain = sum(l.gain for l in sym_lots)
            cur = sym_lots[0].currency
            L.append(f"    Realized trades:  {len(sym_lots)}")
            L.append(f"    Total gain/loss:  {cur} {_fmt(total_gain)}")

        if sym_pos:
            total_held  = sum(p.quantity for p in sym_pos)
            total_value = sum(p.amount   for p in sym_pos)
            cur = sym_pos[0].currency
            L.append(f"    Currently held:   {total_held} units")
            L.append(f"    Cost basis:       {cur} {_fmt(total_value)}")

        L.append("")

    L.append("─" * 60)
    L.append("NOTE: Convert foreign currency amounts to INR using")
    L.append("SBI TT buying rate on the date of each transaction.")
    L.append("Peak balance/value during the year must also be reported.")
    L.append("=" * 60)

    return "\n".join(L)
