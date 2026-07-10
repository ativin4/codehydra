"""Classify trades for Indian income tax purposes.

Determines holding period and capital gains type (STCG / LTCG) based on
Indian tax law post-2024 Finance Act (applicable AY 2025-26 onwards).

Tax rules applied:
  Indian listed equity (STT paid):
    - STCG: held < 12 months  →  20 % (Section 111A)
    - LTCG: held ≥ 12 months  →  12.5 % above ₹1.25 L exemption (Section 112A)
  Foreign assets:
    - STCG: held < 24 months  →  as per income-tax slab
    - LTCG: held ≥ 24 months  →  12.5 % without indexation (Section 112)
"""

from __future__ import annotations

from datetime import date

from .models import AssetRegion, GainType


# Holding-period thresholds (months) for LTCG classification.
LTCG_THRESHOLD_MONTHS = {
    AssetRegion.INDIAN:  12,   # Listed Indian equity (assumes STT paid)
    AssetRegion.FOREIGN: 24,   # Foreign assets
}

# Human-readable tax-rate labels.
TAX_RATES = {
    (AssetRegion.INDIAN,  GainType.STCG): "20%",
    (AssetRegion.INDIAN,  GainType.LTCG): "12.5%",
    (AssetRegion.FOREIGN, GainType.STCG): "As per slab",
    (AssetRegion.FOREIGN, GainType.LTCG): "12.5%",
}

# LTCG exemption threshold for listed Indian equity (₹1,25,000).
LTCG_EXEMPTION_INDIAN = 125_000


# ---------- helpers ----------

def months_between(d1: date, d2: date) -> int:
    """Approximate calendar months between two dates."""
    return (d2.year - d1.year) * 12 + (d2.month - d1.month)


def classify_gain(
    buy_date: date,
    sell_date: date,
    region: AssetRegion,
) -> tuple[GainType, int]:
    """Classify a capital gain as STCG or LTCG.

    Returns:
        (gain_type, holding_days)
    """
    holding_days = (sell_date - buy_date).days
    holding_months = months_between(buy_date, sell_date)
    threshold = LTCG_THRESHOLD_MONTHS.get(region, 24)

    gain_type = GainType.LTCG if holding_months >= threshold else GainType.STCG
    return gain_type, holding_days


def get_tax_rate(region: AssetRegion, gain_type: GainType) -> str:
    """Get the applicable tax-rate description string."""
    return TAX_RATES.get((region, gain_type), "Unknown")


def get_schedule_name(region: AssetRegion, gain_type: GainType) -> str:
    """Get the ITR schedule/section reference for this gain category."""
    if region == AssetRegion.INDIAN:
        if gain_type == GainType.STCG:
            return "Schedule CG → Section 111A (STCG on listed equity)"
        return "Schedule CG → Section 112A (LTCG on listed equity)"
    else:
        if gain_type == GainType.STCG:
            return "Schedule CG → Short-term (other) + Schedule FA"
        return "Schedule CG → Section 112 (LTCG other) + Schedule FA"
