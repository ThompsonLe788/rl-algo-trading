"""Feature engineering for XAU/USD intraday signals.

Extracts Ornstein-Uhlenbeck z-score, ATR, VWAP deviation,
LOB imbalance proxy, time-of-day encoding, and more.
All features at index t use only data ≤ t (no look-ahead).
"""
import numpy as np
import pandas as pd
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import MINUTES_PER_YEAR


def compute_mid(df: pd.DataFrame) -> pd.Series:
    """Mid price from bid/ask or fallback to close."""
    if "bid" in df.columns and "ask" in df.columns:
        return (df["bid"] + df["ask"]) / 2
    if "mid" in df.columns:
        return df["mid"]
    return df["close"]


def ou_zscore(mid: pd.Series, window: int = 50) -> pd.Series:
    """Rolling z-score for mean-reversion signal (OU proxy)."""
    mu = mid.rolling(window, min_periods=window).mean()
    sigma = mid.rolling(window, min_periods=window).std() + 1e-9
    return (mid - mu) / sigma


def ou_params_mle(mid: pd.Series, window: int = 200) -> pd.DataFrame:
    """Rolling MLE estimates of Ornstein-Uhlenbeck parameters.

    Fully vectorized via closed-form rolling OLS — no Python loop, no lstsq.
    O(N) instead of O(N²).  Suitable for both training (50k bars) and live tick.

    OLS:  ΔX = a + b·X_{t-1}  →  θ = -b/dt,  μ = a/(θ·dt)
    Σ-formulas:
        b = (n·Σxy - Σx·Σy) / (n·Σx² - (Σx)²)
        a = (Σy  - b·Σx) / n
    """
    dt = 1.0 / MINUTES_PER_YEAR
    n  = window

    dx    = mid.diff()
    x_lag = mid.shift(1)

    roll = lambda s: s.rolling(n, min_periods=max(10, n // 10))

    s1  = roll(x_lag).sum()            # Σ x_lag
    s2  = roll(x_lag ** 2).sum()       # Σ x_lag²
    sy  = roll(dx).sum()               # Σ dx
    sxy = roll(x_lag * dx).sum()       # Σ x_lag·dx
    cnt = roll(x_lag.notna().astype(float)).sum()  # actual count (handles NaNs)

    denom = cnt * s2 - s1 ** 2
    b = (cnt * sxy - s1 * sy) / denom.where(denom.abs() > 1e-12)
    a = (sy - b * s1) / cnt

    theta    = (-b / dt).clip(lower=1e-9).fillna(1e-9)
    mu       = (a / (theta * dt + 1e-15)).fillna(mid)

    # σ from rolling RMS of residuals
    resid    = dx - a - b * x_lag
    sigma    = (roll(resid ** 2).mean() ** 0.5 / (dt ** 0.5)).fillna(1e-9).clip(lower=1e-9)

    halflife = (np.log(2) / theta).clip(upper=1e9)

    return pd.DataFrame({
        "ou_theta":    theta,
        "ou_mu":       mu,
        "ou_sigma":    sigma,
        "ou_halflife": halflife,
    }, index=mid.index)


def rolling_atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Average True Range on OHLC data."""
    high = df["high"]
    low = df["low"]
    close = df["close"].shift(1)
    tr = pd.concat(
        [high - low, (high - close).abs(), (low - close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(window, min_periods=window).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP (resets daily). Requires 'volume' and 'close' cols."""
    if "volume" not in df.columns:
        return df["close"]  # fallback
    if isinstance(df.index, pd.DatetimeIndex):
        date_key = df.index.date
    else:
        date_key = np.zeros(len(df), dtype=int)
    pv = df["close"] * df["volume"]
    cum_pv = pv.groupby(date_key).cumsum()
    cum_vol = df["volume"].groupby(date_key).cumsum()
    return cum_pv / (cum_vol + 1e-9)


def vwap_deviation(df: pd.DataFrame, window: int = 14, atr: pd.Series | None = None) -> pd.Series:
    """(Price - VWAP) / ATR — normalized deviation from VWAP."""
    v = vwap(df)
    if atr is None:
        atr = rolling_atr(df, window)
    return (df["close"] - v) / (atr + 1e-9)


def lob_imbalance_proxy(df: pd.DataFrame) -> pd.Series:
    """Order-book imbalance proxy from tick volume & direction."""
    if "bid_vol" in df.columns and "ask_vol" in df.columns:
        total = df["bid_vol"] + df["ask_vol"] + 1e-9
        return (df["bid_vol"] - df["ask_vol"]) / total
    # Fallback: sign of return * volume
    ret_sign = np.sign(df["close"].diff())
    vol = df.get("volume", pd.Series(1.0, index=df.index))
    return ret_sign * vol / (vol.rolling(50).mean() + 1e-9)


def time_of_day_encoding(index: pd.DatetimeIndex) -> pd.DataFrame:
    """Sine/cosine time encoding for hour of day (GMT)."""
    hour_frac = index.hour + index.minute / 60.0
    sin_t = np.sin(2 * np.pi * hour_frac / 24)
    cos_t = np.cos(2 * np.pi * hour_frac / 24)
    return pd.DataFrame({"time_sin": sin_t, "time_cos": cos_t}, index=index)


def momentum(mid: pd.Series, periods: list[int] | None = None) -> pd.DataFrame:
    """Log returns over multiple lookback periods."""
    if periods is None:
        periods = [5, 15, 60]
    cols = {}
    for p in periods:
        cols[f"mom_{p}"] = np.log(mid / mid.shift(p))
    return pd.DataFrame(cols, index=mid.index)


def realized_vol(mid: pd.Series, window: int = 50) -> pd.Series:
    """Rolling realized volatility."""
    log_ret = np.log(mid / mid.shift(1))
    return log_ret.rolling(window, min_periods=window).std() * np.sqrt(MINUTES_PER_YEAR)


def build_feature_matrix(df: pd.DataFrame, window: int = 50) -> pd.DataFrame:
    """Build full feature matrix. Shape (N, 24). No look-ahead bias.

    Feature index:
     0  z_score         — OU rolling z-score
     1  atr             — Average True Range (14-bar)
     2  vwap_dev        — (Price-VWAP)/ATR normalized
     3  lob_imb         — LOB imbalance proxy
     4  rvol            — Realized volatility (annualized)
     5  mom_5           — Log return 5-bar
     6  mom_15          — Log return 15-bar
     7  mom_60          — Log return 60-bar
     8  time_sin        — Hour-of-day sine encoding
     9  time_cos        — Hour-of-day cosine encoding
    10  spread          — Bid-ask spread / mid
    11  high_low_range  — (High-Low) / mid
    12  close_open      — (Close-Open) / mid
    13  z_score_abs     — |z_score| (magnitude signal)
    14  atr_change      — ATR 5-bar % change (vol acceleration)
    15  vol_ratio       — rvol / 100-bar mean rvol
    16  ret_1           — 1-bar return
    17  ret_5           — 5-bar return
    18  ret_15          — 15-bar return
    19  ou_theta        — OU mean-reversion speed (MLE, 200-bar)
    20  ou_mu_dev       — (mid - ou_mu) / atr  — distance from OU long-run mean
    21  ou_halflife     — log(2)/theta in minutes (capped at 1000)
    22  pad_22          — reserved
    23  pad_23          — reserved
    """
    mid  = compute_mid(df)
    atr  = rolling_atr(df)
    rvol = realized_vol(mid, window)

    # Time encoding — reuse the helper
    if isinstance(df.index, pd.DatetimeIndex):
        tod = time_of_day_encoding(df.index)
        time_sin = tod["time_sin"].values
        time_cos = tod["time_cos"].values
    else:
        time_sin = np.zeros(len(df))
        time_cos = np.zeros(len(df))

    # OU MLE (vectorized, O(N))
    ou_est = ou_params_mle(mid, window=200)

    # Momentum — reuse the helper
    moms = momentum(mid)

    # Accumulate in dict — build DataFrame once (no intermediate concat)
    cols: dict = {
        "z_score":        ou_zscore(mid, window),
        "atr":            atr,
        "vwap_dev":       vwap_deviation(df, atr=atr),
        "lob_imb":        lob_imbalance_proxy(df),
        "rvol":           rvol,
        "mom_5":          moms["mom_5"],
        "mom_15":         moms["mom_15"],
        "mom_60":         moms["mom_60"],
        "time_sin":       pd.Series(time_sin, index=df.index),
        "time_cos":       pd.Series(time_cos, index=df.index),
        "spread":         (df.get("ask", mid) - df.get("bid", mid)) / (mid + 1e-9),
        "high_low_range": (df["high"] - df["low"]) / (mid + 1e-9),
        "close_open":     (df["close"] - df["open"]) / (mid + 1e-9),
    }

    z = cols["z_score"]
    cols["z_score_abs"] = z.abs()
    cols["atr_change"]  = atr.pct_change(5)
    cols["vol_ratio"]   = rvol / (rvol.rolling(100).mean() + 1e-9)
    cols["ret_1"]       = mid.pct_change(1)
    cols["ret_5"]       = mid.pct_change(5)
    cols["ret_15"]      = mid.pct_change(15)
    cols["ou_theta"]    = np.clip(ou_est["ou_theta"] / 1000.0, 0.0, 1.0)
    cols["ou_mu_dev"]   = (mid - ou_est["ou_mu"]) / (atr + 1e-9)
    cols["ou_halflife"] = np.clip(ou_est["ou_halflife"] * (252 * 1440), 0.0, 1000.0) / 1000.0

    feats = pd.DataFrame(cols, index=df.index)

    # Pad / truncate to exactly 24 features
    target_cols = 24
    if feats.shape[1] > target_cols:
        feats = feats.iloc[:, :target_cols]
    while feats.shape[1] < target_cols:
        feats[f"pad_{feats.shape[1]}"] = 0.0

    return feats
