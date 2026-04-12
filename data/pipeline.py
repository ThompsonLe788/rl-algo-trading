"""Data pipeline: MT5 tick data acquisition, caching, and preprocessing.

Fetches XAUUSD tick/OHLC data from MetaTrader 5,
caches as Parquet for fast reload, and provides
iterators for training and live inference.
"""
import pandas as pd
import numpy as np
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
import logging

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import DATA_DIR, SYMBOL

logger = logging.getLogger("data_pipeline")


def fetch_mt5_ohlc(
    symbol: str = SYMBOL,
    timeframe: str = "M1",
    start: datetime | None = None,
    end: datetime | None = None,
    num_bars: int = 100_000,
) -> pd.DataFrame:
    """Fetch OHLC data from MetaTrader 5.

    Requires MetaTrader5 package and running MT5 terminal.
    """
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")

    tf_map = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "H1": mt5.TIMEFRAME_H1,
        "D1": mt5.TIMEFRAME_D1,
    }
    tf = tf_map.get(timeframe, mt5.TIMEFRAME_M1)

    if start and end:
        rates = mt5.copy_rates_range(symbol, tf, start, end)
    else:
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, num_bars)

    mt5.shutdown()

    if rates is None or len(rates) == 0:
        raise ValueError(f"No data returned for {symbol}")

    df = pd.DataFrame(rates)
    df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.rename(columns={"tick_volume": "volume"})
    df = df[["datetime", "open", "high", "low", "close", "volume"]]
    df["mid"] = (df["high"] + df["low"]) / 2
    df = df.set_index("datetime")
    return df


def fetch_mt5_ticks(
    symbol: str = SYMBOL,
    start: datetime | None = None,
    end: datetime | None = None,
    num_ticks: int = 500_000,
) -> pd.DataFrame:
    """Fetch tick data from MetaTrader 5."""
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")

    if start and end:
        ticks = mt5.copy_ticks_range(symbol, start, end, mt5.COPY_TICKS_ALL)
    else:
        ticks = mt5.copy_ticks_from(
            symbol,
            datetime.now(timezone.utc) - timedelta(days=30),
            num_ticks,
            mt5.COPY_TICKS_ALL,
        )

    mt5.shutdown()

    if ticks is None or len(ticks) == 0:
        raise ValueError(f"No ticks returned for {symbol}")

    df = pd.DataFrame(ticks)
    df["datetime"] = pd.to_datetime(df["time_msc"], unit="ms", utc=True)
    df["mid"] = (df["bid"] + df["ask"]) / 2
    df = df.set_index("datetime")
    return df


def save_parquet(df: pd.DataFrame, name: str):
    """Cache DataFrame as Parquet."""
    path = DATA_DIR / f"{name}.parquet"
    df.to_parquet(path, engine="pyarrow")
    logger.info(f"Saved {len(df)} rows to {path}")
    return path


def load_parquet(name: str) -> pd.DataFrame:
    """Load cached Parquet data."""
    path = DATA_DIR / f"{name}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run fetch first.")
    return pd.read_parquet(path, engine="pyarrow")


def load_or_fetch(
    name: str | None = None,
    symbol: str = SYMBOL,
    timeframe: str = "M1",
    num_bars: int = 100_000,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Load from cache or fetch from MT5.

    Cache file name is derived from symbol + timeframe if name is not given:
      XAUUSD + M1  → xauusd_m1.parquet
      EURUSD + M5  → eurusd_m5.parquet
    """
    if name is None:
        name = f"{symbol.lower()}_{timeframe.lower()}"
    path = DATA_DIR / f"{name}.parquet"
    if path.exists() and not force_refresh:
        logger.info(f"Loading cached {path}")
        return pd.read_parquet(path)

    logger.info(f"Fetching {symbol} {timeframe} from MT5...")
    df = fetch_mt5_ohlc(symbol, timeframe, num_bars=num_bars)
    save_parquet(df, name)
    return df


def generate_synthetic_data(
    n_bars: int = 50_000,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate synthetic XAUUSD-like OHLC data for testing.

    Uses geometric Brownian motion with mean-reversion overlay.
    """
    rng = np.random.default_rng(seed)
    dt = 1 / (252 * 1440)  # 1-minute

    # GBM + OU
    price = 2000.0
    theta = 5.0  # mean reversion speed
    mu = 2000.0
    sigma = 0.15

    prices = [price]
    for _ in range(n_bars - 1):
        dW = rng.normal(0, np.sqrt(dt))
        price += theta * (mu - price) * dt + sigma * price * dW
        price = max(price, 1500)  # floor
        prices.append(price)

    prices = np.array(prices)
    noise = rng.uniform(0.5, 3.0, n_bars)

    df = pd.DataFrame({
        "datetime": pd.date_range("2025-01-01", periods=n_bars, freq="1min", tz="UTC"),
        "open": prices,
        "high": prices + noise,
        "low": prices - noise,
        "close": prices + rng.normal(0, 0.5, n_bars),
        "volume": rng.integers(100, 5000, n_bars),
    })
    df["mid"] = (df["high"] + df["low"]) / 2
    df = df.set_index("datetime")
    return df


class LiveTickStream:
    """Iterator for live tick data from MT5.

    Uses a deque ring-buffer (O(1) append/discard) instead of pd.concat.
    MT5 is initialized once and reconnected only on failure.
    """

    def __init__(self, symbol: str = SYMBOL, window: int = 100):
        self.symbol = symbol
        self.window = window
        self._buf: deque = deque(maxlen=window)
        self._mt5_ok: bool = False

    def _ensure_mt5(self) -> bool:
        if self._mt5_ok:
            return True
        import MetaTrader5 as mt5
        self._mt5_ok = mt5.initialize()
        return self._mt5_ok

    def __iter__(self):
        return self

    def __next__(self):
        import MetaTrader5 as mt5

        if not self._ensure_mt5():
            raise StopIteration

        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            # Reconnect once on failure
            self._mt5_ok = False
            if not self._ensure_mt5():
                raise StopIteration
            tick = mt5.symbol_info_tick(self.symbol)
            if tick is None:
                raise StopIteration

        mid = (tick.bid + tick.ask) / 2
        self._buf.append({
            "datetime": datetime.now(timezone.utc),
            "bid":      tick.bid,
            "ask":      tick.ask,
            "mid":      mid,
            "volume":   tick.volume,
        })

        df = pd.DataFrame(list(self._buf)).set_index("datetime")
        spread = df["ask"] - df["bid"]
        atr    = spread.mean() * 10 if len(df) > 1 else spread.iloc[-1] * 10

        return df, mid, float(atr)
