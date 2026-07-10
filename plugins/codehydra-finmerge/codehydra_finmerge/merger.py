"""FIFO trade matching and cross-statement merging.

Matches buy and sell transactions using First-In-First-Out (FIFO)
methodology as required by Indian tax law for equity capital-gains
computation.  Groups trades by (symbol, region) so Indian and Foreign
holdings of the same ticker are tracked independently.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Dict, List, Tuple

from .models import (
    AssetRegion,
    GainType,
    OpenPosition,
    TaxLot,
    TaxSummary,
    Trade,
    TradeAction,
)
from .classifier import classify_gain


def merge_and_match(trades: List[Trade]) -> TaxSummary:
    """FIFO-match all trades and produce a :class:`TaxSummary`.

    1. Groups trades by ``(symbol, region)``.
    2. Sorts chronologically within each group (buys before sells on the
       same date).
    3. Maintains a FIFO buy queue per group.
    4. Matches each sell against the oldest remaining buy lots first.
    5. Collects unmatched buys as open positions.
    """
    summary = TaxSummary()
    summary.total_trades = len(trades)

    # Group by (symbol, region).
    groups: Dict[Tuple[str, AssetRegion], List[Trade]] = defaultdict(list)
    for trade in trades:
        groups[(trade.symbol, trade.region)].append(trade)

    for (symbol, region), group in sorted(groups.items()):
        # Buys before sells on the same day so sells can match same-day buys.
        group.sort(
            key=lambda t: (t.date, 0 if t.action == TradeAction.BUY else 1)
        )

        buy_queue: List[dict] = []

        for trade in group:
            if trade.action == TradeAction.BUY:
                summary.total_buy_value += trade.amount
                buy_queue.append({
                    "date":          trade.date,
                    "qty_remaining": trade.quantity,
                    "price":         trade.price,
                    "broker":        trade.broker,
                    "exchange":      trade.exchange,
                    "currency":      trade.currency,
                })

            elif trade.action == TradeAction.SELL:
                summary.total_sell_value += trade.amount
                sell_qty   = trade.quantity
                sell_price = trade.price
                sell_date  = trade.date

                while sell_qty > 0 and buy_queue:
                    buy = buy_queue[0]
                    matched_qty = min(sell_qty, buy["qty_remaining"])

                    buy_amount  = matched_qty * buy["price"]
                    sell_amount = matched_qty * sell_price
                    gain        = sell_amount - buy_amount

                    gain_type, holding_days = classify_gain(
                        buy["date"], sell_date, region,
                    )

                    lot = TaxLot(
                        symbol=symbol,
                        region=region,
                        buy_date=buy["date"],
                        sell_date=sell_date,
                        quantity=matched_qty,
                        buy_price=buy["price"],
                        sell_price=sell_price,
                        buy_amount=buy_amount,
                        sell_amount=sell_amount,
                        gain=gain,
                        gain_type=gain_type,
                        holding_days=holding_days,
                        currency=buy["currency"],
                        exchange=buy.get("exchange", ""),
                        buy_broker=buy["broker"],
                        sell_broker=trade.broker,
                    )
                    summary.tax_lots.append(lot)
                    summary.total_lots += 1
                    summary.total_realized_gain += gain

                    # Accumulate into the right category bucket.
                    _accum(summary, region, gain_type, gain)

                    buy["qty_remaining"] -= matched_qty
                    sell_qty -= matched_qty

                    if buy["qty_remaining"] <= 0:
                        buy_queue.pop(0)

                if sell_qty > 0:
                    summary.errors.append(
                        f"⚠ {symbol}: Sold {sell_qty} units on {sell_date} "
                        f"with no matching buy (short sale or missing statement?)"
                    )

        # Remaining buys → open (unrealized) positions.
        for buy in buy_queue:
            if buy["qty_remaining"] > 0:
                summary.open_positions.append(OpenPosition(
                    symbol=symbol,
                    date=buy["date"],
                    quantity=buy["qty_remaining"],
                    price=buy["price"],
                    amount=buy["qty_remaining"] * buy["price"],
                    region=region,
                    currency=buy["currency"],
                    exchange=buy.get("exchange", ""),
                    broker=buy["broker"],
                ))

    return summary


# ------------------------------------------------------------------ #

def _accum(
    summary: TaxSummary,
    region: AssetRegion,
    gain_type: GainType,
    gain: Decimal,
) -> None:
    """Add *gain* to the correct category bucket on *summary*."""
    if region == AssetRegion.INDIAN:
        if gain_type == GainType.STCG:
            summary.indian_stcg += gain
            summary.indian_stcg_count += 1
        else:
            summary.indian_ltcg += gain
            summary.indian_ltcg_count += 1
    else:
        if gain_type == GainType.STCG:
            summary.foreign_stcg += gain
            summary.foreign_stcg_count += 1
        else:
            summary.foreign_ltcg += gain
            summary.foreign_ltcg_count += 1
