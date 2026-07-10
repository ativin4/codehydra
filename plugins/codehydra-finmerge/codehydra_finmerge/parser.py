"""Multi-broker trade statement parser.

Parses extracted PDF content (text + tables) into normalized Trade objects.
Uses heuristic column detection to handle various broker formats without
hardcoded templates — works with Zerodha, Groww, Angel One, ICICI Direct,
Interactive Brokers, Schwab, Vested, and generic tabular statements.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple

from .models import Trade, TradeAction, AssetRegion


# ---------- Column-detection keywords ----------
# Maps a semantic column name to all known header variations across brokers.

COLUMN_KEYWORDS = {
    "date": [
        "trade date", "transaction date", "order date", "settlement date",
        "txn date", "exec date", "date",
    ],
    "symbol": [
        "scrip name", "stock symbol", "scrip code", "symbol", "scrip",
        "stock", "instrument", "security", "company", "script",
        "name", "ticker", "description",
    ],
    "action": [
        "buy/sell", "buy / sell", "transaction type", "trade type",
        "order type", "txn type", "action", "type", "side", "b/s",
    ],
    "quantity": [
        "no. of shares", "traded qty", "executed qty", "net qty",
        "qty", "quantity", "shares", "units", "volume",
    ],
    "price": [
        "avg. price", "avg price", "trade price", "execution price",
        "avg rate", "unit price", "price", "rate", "nav",
    ],
    "amount": [
        "net amount", "trade value", "total value", "net value",
        "amount", "value", "total", "consideration", "turnover",
    ],
    "exchange": [
        "exchange", "market", "segment", "exch",
    ],
    "isin": [
        "isin code", "isin no", "isin",
    ],
}

# ---------- Broker detection ----------

BROKER_PATTERNS = {
    "zerodha":              [r"zerodha", r"zbroker", r"z5\s*contract"],
    "groww":                [r"groww"],
    "angelone":             [r"angel\s*(one|broking)", r"angel\s*securities"],
    "icici_direct":         [r"icici\s*direct", r"icici\s*securities"],
    "hdfc_securities":      [r"hdfc\s*securities"],
    "upstox":               [r"upstox", r"rksv"],
    "kotak_securities":     [r"kotak\s*securities"],
    "motilal_oswal":        [r"motilal\s*oswal"],
    "interactive_brokers":  [r"interactive\s*brokers"],
    "charles_schwab":       [r"schwab", r"charles\s*schwab"],
    "vested":               [r"vested\s*finance", r"vested"],
    "indmoney":             [r"indmoney", r"ind\s*money"],
    "fi":                   [r"\bfi\b.*stocks"],
    "dhan":                 [r"\bdhan\b"],
}

# Brokers that primarily deal in foreign (US) stocks.
FOREIGN_BROKERS = frozenset({
    "interactive_brokers", "charles_schwab", "vested", "indmoney", "fi",
})

# ---------- Date formats to try ----------

DATE_FORMATS = [
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y",
    "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y",
    "%Y/%m/%d", "%m-%d-%Y", "%b %d, %Y", "%B %d, %Y",
    "%d.%m.%Y", "%Y.%m.%d",
]

# ---------- Buy / Sell indicators ----------

BUY_INDICATORS  = frozenset({"buy", "b", "bought", "purchase", "p"})
SELL_INDICATORS = frozenset({"sell", "s", "sold", "sale", "redemption"})


# ------------------------------------------------------------------ #
#                          PUBLIC FUNCTIONS                           #
# ------------------------------------------------------------------ #

def detect_broker(text: str) -> str:
    """Detect the broker from raw PDF text.  Returns a slug or ``"unknown"``."""
    text_lower = text.lower()
    for broker, patterns in BROKER_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text_lower):
                return broker
    return "unknown"


def parse_statement(extracted: dict) -> List[Trade]:
    """Parse a single extracted PDF into Trade objects.

    Args:
        extracted: dict from ``extractor.extract_from_pdf()`` with keys
                   ``text``, ``tables``, ``file``.

    Returns:
        List of parsed Trade objects (may be empty).
    """
    text   = extracted.get("text", "")
    tables = extracted.get("tables", [])
    source = extracted.get("file", "")

    broker = detect_broker(text)

    # Prefer table-based parsing (more reliable with structured PDFs).
    trades: List[Trade] = []
    if tables:
        trades = _parse_from_tables(tables, text, source, broker)

    # Fall back to line-based text parsing.
    if not trades and text:
        trades = _parse_from_text(text, source, broker)

    return trades


# ------------------------------------------------------------------ #
#                         TABLE-BASED PARSING                        #
# ------------------------------------------------------------------ #

def _parse_from_tables(
    tables: List[List[List[str]]],
    full_text: str,
    source_file: str,
    broker: str,
) -> List[Trade]:
    trades: List[Trade] = []
    region   = _infer_region(full_text, broker)
    currency = "USD" if region == AssetRegion.FOREIGN else "INR"

    for table in tables:
        if len(table) < 2:
            continue

        # Try the first few rows as potential header rows.
        for header_idx in range(min(3, len(table))):
            col_map = _map_columns(table[header_idx])

            # Minimum viable mapping: date + symbol + (qty or amount).
            if "date" not in col_map or "symbol" not in col_map:
                continue
            if "quantity" not in col_map and "amount" not in col_map:
                continue

            for row in table[header_idx + 1:]:
                trade = _row_to_trade(
                    row, col_map, region, currency, broker, source_file,
                )
                if trade is not None:
                    trades.append(trade)
            break  # found a valid header — don't retry with later rows

    return trades


def _map_columns(headers: List[str]) -> dict[str, int]:
    """Map semantic column names → header index using keyword matching."""
    mapping: dict[str, int] = {}
    normalized = [_norm(h) for h in headers]

    for col_name, keywords in COLUMN_KEYWORDS.items():
        for idx, header in enumerate(normalized):
            if idx in mapping.values():
                continue  # column already claimed
            for kw in keywords:
                if kw == header or kw in header:
                    mapping[col_name] = idx
                    break
            if col_name in mapping:
                break

    return mapping


def _row_to_trade(
    row: List[str],
    col_map: dict[str, int],
    default_region: AssetRegion,
    default_currency: str,
    broker: str,
    source_file: str,
) -> Optional[Trade]:
    """Convert a single table row into a Trade (or ``None`` on failure)."""
    try:
        # --- Date (required) ---
        date_idx = col_map.get("date")
        if date_idx is None or date_idx >= len(row):
            return None
        trade_date = _parse_date(row[date_idx])
        if trade_date is None:
            return None

        # --- Symbol (required) ---
        sym_idx = col_map.get("symbol")
        if sym_idx is None or sym_idx >= len(row):
            return None
        symbol = row[sym_idx].strip()
        if not symbol:
            return None

        # --- Action ---
        action: Optional[TradeAction] = None
        act_idx = col_map.get("action")
        if act_idx is not None and act_idx < len(row):
            action = _parse_action(row[act_idx])

        # --- Quantity ---
        qty: Optional[Decimal] = None
        qty_idx = col_map.get("quantity")
        if qty_idx is not None and qty_idx < len(row):
            qty = _parse_decimal(row[qty_idx])

        # --- Price ---
        price: Optional[Decimal] = None
        price_idx = col_map.get("price")
        if price_idx is not None and price_idx < len(row):
            price = _parse_decimal(row[price_idx])

        # --- Amount ---
        amount: Optional[Decimal] = None
        amt_idx = col_map.get("amount")
        if amt_idx is not None and amt_idx < len(row):
            amount = _parse_decimal(row[amt_idx])

        # --- Infer missing values ---
        if qty and price and not amount:
            amount = qty * price
        elif amount and qty and not price:
            price = (amount / qty) if qty != 0 else Decimal("0")
        elif amount and price and not qty:
            qty = (amount / price) if price != 0 else Decimal("0")

        if not qty or not price:
            return None

        if not amount:
            amount = qty * price

        # Negative quantity → sell (some brokers use this convention).
        if action is None:
            if qty < 0:
                action = TradeAction.SELL
                qty = abs(qty)
                amount = abs(amount)
            else:
                return None  # cannot determine buy/sell

        # --- Exchange ---
        exchange = ""
        exch_idx = col_map.get("exchange")
        if exch_idx is not None and exch_idx < len(row):
            exchange = row[exch_idx].strip().upper()

        # --- ISIN ---
        isin = ""
        isin_idx = col_map.get("isin")
        if isin_idx is not None and isin_idx < len(row):
            isin = row[isin_idx].strip()

        return Trade(
            date=trade_date,
            symbol=_clean_symbol(symbol),
            action=action,
            quantity=abs(qty),
            price=abs(price),
            amount=abs(amount),
            exchange=exchange,
            currency=default_currency,
            broker=broker,
            region=default_region,
            isin=isin,
            source_file=source_file,
        )
    except Exception:
        return None


# ------------------------------------------------------------------ #
#                       TEXT-BASED PARSING                            #
# ------------------------------------------------------------------ #

_DATE_LINE_RE = re.compile(
    r"(\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
    r"|\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r"|\d{1,2}\s+\w{3,9}\s+\d{4})"
)


def _parse_from_text(
    text: str,
    source_file: str,
    broker: str,
) -> List[Trade]:
    """Fallback parser: extract trades from raw text lines."""
    trades: List[Trade] = []
    region   = _infer_region(text, broker)
    currency = "USD" if region == AssetRegion.FOREIGN else "INR"

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue

        m = _DATE_LINE_RE.match(line)
        if not m:
            continue

        trade_date = _parse_date(m.group(1))
        if trade_date is None:
            continue

        # Split the remainder on 2+ spaces or tab boundaries.
        rest  = line[m.end():].strip()
        parts = re.split(r"\s{2,}|\t", rest)
        if len(parts) < 3:
            continue

        symbol: Optional[str]       = None
        action: Optional[TradeAction] = None
        numbers: List[Decimal]      = []

        for part in parts:
            p = part.strip()
            if not p:
                continue

            a = _parse_action(p)
            if a is not None:
                action = a
                continue

            d = _parse_decimal(p)
            if d is not None:
                numbers.append(d)
                continue

            if symbol is None and re.match(r"^[A-Za-z]", p):
                symbol = _clean_symbol(p)

        if not (trade_date and symbol and action and len(numbers) >= 2):
            continue

        qty   = numbers[0]
        price = numbers[1]
        amount = numbers[2] if len(numbers) >= 3 else qty * price

        trades.append(Trade(
            date=trade_date,
            symbol=symbol,
            action=action,
            quantity=abs(qty),
            price=abs(price),
            amount=abs(amount),
            currency=currency,
            broker=broker,
            region=region,
            source_file=source_file,
        ))

    return trades


# ------------------------------------------------------------------ #
#                             HELPERS                                #
# ------------------------------------------------------------------ #

def _norm(header: str) -> str:
    """Normalize a header cell for keyword matching."""
    return re.sub(r"[^a-z0-9/ .]", "", header.lower()).strip()


def _clean_symbol(raw: str) -> str:
    """Normalize a stock symbol: uppercase, strip exchange suffixes."""
    s = raw.strip().upper()
    # Remove common suffixes added by brokers: "-EQ", "-BE", ".NS", ".BO"
    s = re.sub(r"[-.](?:EQ|BE|NS|BO|NFO|BSE|NSE)$", "", s, flags=re.IGNORECASE)
    return s


def _parse_date(date_str: str) -> Optional[date]:
    """Try multiple date formats; return the first that succeeds."""
    ds = date_str.strip()
    if not ds:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(ds, fmt).date()
        except ValueError:
            continue
    return None


def _parse_decimal(value_str: str) -> Optional[Decimal]:
    """Parse a string into a Decimal, tolerating commas / currency symbols."""
    if not value_str:
        return None
    cleaned = re.sub(r"[₹$€£¥,\s]", "", value_str.strip())
    # Parenthetical negatives: (100) → -100
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    # Strip trailing non-numeric chars like "Cr", "Dr"
    cleaned = re.sub(r"[a-zA-Z]+$", "", cleaned)
    if not cleaned or cleaned == "-":
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _parse_action(value: str) -> Optional[TradeAction]:
    v = value.strip().lower()
    if v in BUY_INDICATORS:
        return TradeAction.BUY
    if v in SELL_INDICATORS:
        return TradeAction.SELL
    return None


def _infer_region(text: str, broker: str) -> AssetRegion:
    """Heuristic: decide Indian vs Foreign from context clues."""
    if broker in FOREIGN_BROKERS:
        return AssetRegion.FOREIGN
    if re.search(r"\b(USD|EUR|GBP|US\s*Dollar)\b", text, re.IGNORECASE):
        return AssetRegion.FOREIGN
    if re.search(
        r"\b(NSE|BSE|National Stock Exchange|Bombay Stock Exchange)\b",
        text, re.IGNORECASE,
    ):
        return AssetRegion.INDIAN
    return AssetRegion.INDIAN  # default for Indian-resident context
