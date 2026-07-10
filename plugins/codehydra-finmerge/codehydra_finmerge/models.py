"""Data models for the FinMerge trade statement processor.

Defines Trade, TaxLot, OpenPosition, and TaxSummary dataclasses used
throughout the extraction → parsing → matching → reporting pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import List


class AssetRegion(str, Enum):
    """Whether the asset is an Indian domestic security or a Foreign Asset."""
    INDIAN = "indian"
    FOREIGN = "foreign"


class TradeAction(str, Enum):
    BUY = "buy"
    SELL = "sell"


class GainType(str, Enum):
    STCG = "stcg"  # Short-Term Capital Gain
    LTCG = "ltcg"  # Long-Term Capital Gain


# ---------- Exchange / Currency classification ----------

INDIAN_EXCHANGES = frozenset({"NSE", "BSE", "MCX", "NCDEX"})

FOREIGN_EXCHANGES = frozenset({
    "NYSE", "NASDAQ", "AMEX", "LSE", "TSE", "HKEX", "SGX",
    "ASX", "TSX", "XETRA", "EURONEXT",
})

FOREIGN_CURRENCIES = frozenset({
    "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "SGD", "HKD", "CHF",
})


# ---------- Dataclasses ----------

@dataclass
class Trade:
    """A single buy or sell transaction parsed from a broker statement."""
    date: date
    symbol: str
    action: TradeAction
    quantity: Decimal
    price: Decimal          # per unit
    amount: Decimal         # total trade value (qty × price)
    exchange: str = ""
    currency: str = "INR"
    broker: str = ""
    region: AssetRegion = AssetRegion.INDIAN
    isin: str = ""
    source_file: str = ""
    notes: str = ""

    def __post_init__(self):
        # Auto-detect region from exchange/currency when not explicitly set.
        if self.exchange.upper() in FOREIGN_EXCHANGES or self.currency.upper() in FOREIGN_CURRENCIES:
            self.region = AssetRegion.FOREIGN
        elif self.exchange.upper() in INDIAN_EXCHANGES or self.currency.upper() == "INR":
            self.region = AssetRegion.INDIAN


@dataclass
class TaxLot:
    """A FIFO-matched buy–sell pair for capital gains computation."""
    symbol: str
    region: AssetRegion
    buy_date: date
    sell_date: date
    quantity: Decimal
    buy_price: Decimal      # per unit
    sell_price: Decimal     # per unit
    buy_amount: Decimal     # quantity × buy_price
    sell_amount: Decimal    # quantity × sell_price
    gain: Decimal           # sell_amount − buy_amount
    gain_type: GainType
    holding_days: int
    currency: str = "INR"
    exchange: str = ""
    buy_broker: str = ""
    sell_broker: str = ""

    @property
    def gain_pct(self) -> Decimal:
        """Return percentage gain/loss on the lot."""
        if self.buy_amount == 0:
            return Decimal("0")
        return ((self.gain / self.buy_amount) * 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )


@dataclass
class OpenPosition:
    """An unmatched buy — shares still held (unrealized)."""
    symbol: str
    date: date
    quantity: Decimal
    price: Decimal
    amount: Decimal
    region: AssetRegion
    currency: str = "INR"
    exchange: str = ""
    broker: str = ""


@dataclass
class TaxSummary:
    """Aggregated tax computation results from FIFO matching."""
    # Indian equity
    indian_stcg: Decimal = Decimal("0")
    indian_ltcg: Decimal = Decimal("0")
    indian_stcg_count: int = 0
    indian_ltcg_count: int = 0
    # Foreign assets
    foreign_stcg: Decimal = Decimal("0")
    foreign_ltcg: Decimal = Decimal("0")
    foreign_stcg_count: int = 0
    foreign_ltcg_count: int = 0
    # Totals
    total_buy_value: Decimal = Decimal("0")
    total_sell_value: Decimal = Decimal("0")
    total_realized_gain: Decimal = Decimal("0")
    total_trades: int = 0
    total_lots: int = 0
    # Detail lists
    tax_lots: List[TaxLot] = field(default_factory=list)
    open_positions: List[OpenPosition] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
