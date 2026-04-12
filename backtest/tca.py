"""Transaction Cost Analysis (TCA).

Measures execution quality by comparing fill prices vs arrival prices,
calculating slippage, market impact, and timing costs.
"""
import numpy as np
import pandas as pd
from dataclasses import dataclass


@dataclass
class TCAReport:
    total_trades: int
    avg_slippage_bps: float
    median_slippage_bps: float
    p95_slippage_bps: float
    avg_market_impact_bps: float
    total_cost_bps: float
    implementation_shortfall_bps: float

    def summary(self) -> str:
        return (
            f"TCA Report ({self.total_trades} trades)\n"
            f"  Avg slippage:     {self.avg_slippage_bps:.2f} bps\n"
            f"  Median slippage:  {self.median_slippage_bps:.2f} bps\n"
            f"  P95 slippage:     {self.p95_slippage_bps:.2f} bps\n"
            f"  Market impact:    {self.avg_market_impact_bps:.2f} bps\n"
            f"  Total cost:       {self.total_cost_bps:.2f} bps\n"
            f"  Impl shortfall:   {self.implementation_shortfall_bps:.2f} bps"
        )


def compute_slippage(fills: pd.DataFrame) -> pd.Series:
    """Slippage in bps: |fill_price - arrival_price| / arrival_price * 10000.

    Expects columns: fill_price, arrival_price, side (+1/-1).
    """
    slip = (fills["fill_price"] - fills["arrival_price"]) * fills["side"]
    return slip / fills["arrival_price"] * 1e4


def compute_market_impact(
    fills: pd.DataFrame,
    market_df: pd.DataFrame,
    impact_window_bars: int = 10,
) -> pd.Series:
    """Market impact: price move in the direction of the trade after fill.

    Positive = trade moved the market against us.
    """
    impacts = []
    mid_col = "mid" if "mid" in market_df.columns else "close"

    for _, fill in fills.iterrows():
        fill_idx = fill.get("bar_index", None)
        if fill_idx is None or pd.isna(fill_idx):
            impacts.append(0.0)
            continue
        fill_idx = int(fill_idx)
        if fill_idx + impact_window_bars >= len(market_df):
            impacts.append(0.0)
            continue

        price_at_fill = market_df.iloc[fill_idx][mid_col]
        price_after = market_df.iloc[fill_idx + impact_window_bars][mid_col]
        impact = (price_after - price_at_fill) * fill["side"]
        impacts.append(impact / price_at_fill * 1e4)

    return pd.Series(impacts, index=fills.index)


def implementation_shortfall(fills: pd.DataFrame) -> float:
    """Total implementation shortfall in bps.

    IS = sum(side * (fill_price - decision_price) * quantity) /
         sum(|decision_price * quantity|) * 10000
    """
    if "decision_price" not in fills.columns:
        fills = fills.copy()
        fills["decision_price"] = fills["arrival_price"]

    qty = fills.get("lot", pd.Series(1.0, index=fills.index))
    numerator = (fills["side"] * (fills["fill_price"] - fills["decision_price"]) * qty).sum()
    denominator = (fills["decision_price"].abs() * qty).sum() + 1e-9
    return numerator / denominator * 1e4


def run_tca(fills: pd.DataFrame, market_df: pd.DataFrame | None = None) -> TCAReport:
    """Run full TCA analysis.

    Args:
        fills: DataFrame with columns:
            fill_price, arrival_price, side, lot, bar_index (optional)
        market_df: Full market data for market impact (optional)
    """
    if len(fills) == 0:
        return TCAReport(0, 0, 0, 0, 0, 0, 0)

    slippage = compute_slippage(fills)

    if market_df is not None and "bar_index" in fills.columns:
        impact = compute_market_impact(fills, market_df)
        avg_impact = impact.mean()
    else:
        avg_impact = 0.0

    is_bps = implementation_shortfall(fills)

    return TCAReport(
        total_trades=len(fills),
        avg_slippage_bps=slippage.mean(),
        median_slippage_bps=slippage.median(),
        p95_slippage_bps=float(np.percentile(slippage, 95)),
        avg_market_impact_bps=avg_impact,
        total_cost_bps=slippage.mean() + avg_impact,
        implementation_shortfall_bps=is_bps,
    )
