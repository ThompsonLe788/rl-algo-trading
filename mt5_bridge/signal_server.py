"""ZeroMQ signal server: Python -> MT5 bridge.

Publishes JSON trading signals on tcp://127.0.0.1:5555 (ZMQ PUB).
MT5 EA subscribes (ZMQ SUB) and polls every tick.
Includes heartbeat for connection health monitoring.
Fallback: file-based IPC when ZMQ is unavailable on MT5 side.

Multi-symbol: topic = symbol bytes (e.g. b"XAUUSD", b"EURUSD").
LiveStateWriter keeps a shared live_state.json read by Streamlit + Telegram.
"""
import json
import math
import time
import logging
import threading
from collections import deque
import zmq
import numpy as np
from datetime import datetime, timezone
from pathlib import Path

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import (
    ZMQ_SIGNAL_ADDR, EOD_HOUR_GMT, LOG_DIR, LIVE_STATE_PATH, get_symbol_config,
    HEARTBEAT_INTERVAL, SPREAD_SPIKE_MULT,
)
from risk.kelly import KellyPositionSizer, vwap_slice_orders
from risk.kill_switch import KillSwitch
from risk.news_filter import NewsFilter

logger = logging.getLogger("signal_server")
_handler = logging.FileHandler(LOG_DIR / "signal_server.log")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Shared live state writer (Streamlit + Telegram consumer)
# ---------------------------------------------------------------------------
class LiveStateWriter:
    """Writes a single live_state.json used by the monitoring stack.

    Singleton: all SignalServer instances share one writer so multi-symbol
    state accumulates correctly instead of each worker overwriting the file.

    Structure:
    {
      "_account": {"equity": float, "balance": float, "drawdown_pct": float},
      "_system":  {"alive": bool, "killed": bool, "signal_count": int,
                   "kill_reason": str, "last_heartbeat": str},
      "XAUUSD": {"position": int, "entry_price": float, "unrealized_pnl": float,
                  "regime": int, "kelly_f": float, "drawdown_pct": float,
                  "last_signal": dict, "timestamp": str},
      ...
    }

    Writes are atomic: write to .tmp then rename, so readers never see partial JSON.
    Mirrors to MT5 Common Files path so ATS_Panel.mq5 can read it.
    """

    _instance: "LiveStateWriter | None" = None
    _instance_lock = threading.Lock()
    _active_servers: int = 0          # ref-count for alive tracking
    _total_signal_count: int = 0      # aggregate across all servers

    @classmethod
    def instance(cls, **kwargs) -> "LiveStateWriter":
        """Return singleton instance (thread-safe lazy init)."""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(**kwargs)
            return cls._instance

    def register_server(self):
        """Track active server count for alive status."""
        with self.__class__._instance_lock:
            self.__class__._active_servers += 1

    def unregister_server(self) -> bool:
        """Decrement server count. Returns True if this was the last server."""
        with self.__class__._instance_lock:
            self.__class__._active_servers = max(0, self.__class__._active_servers - 1)
            return self.__class__._active_servers == 0

    def add_signals(self, count: int = 1):
        """Thread-safe increment of total signal count across all servers."""
        with self.__class__._instance_lock:
            self.__class__._total_signal_count += count

    @property
    def total_signals(self) -> int:
        return self.__class__._total_signal_count

    def __init__(
        self,
        log_path: Path | None = None,
        mt5_files_path: Path | None = None,
    ):
        self._log_path = log_path or LIVE_STATE_PATH
        # MT5 Common Files for panel indicator
        if mt5_files_path is None:
            from config import MT5_FILES_PATH
            mt5_files_path = MT5_FILES_PATH
        self._mt5_path = mt5_files_path / "ats_live_state.json"
        # Ensure parent directories exist once at construction time
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._mt5_path.parent.mkdir(parents=True, exist_ok=True)
        _now = datetime.now(timezone.utc)
        self._state: dict = {
            "_account": {"equity": 0.0, "balance": 0.0, "drawdown_pct": 0.0},
            "_system": {
                "alive": True, "killed": False,
                "signal_count": 0, "kill_reason": "",
                "last_heartbeat": _now.isoformat(),
                "unix_time": int(_now.timestamp()),
            },
        }
        self._lock = threading.Lock()
        self._dirty = False  # set True by any update method; cleared after write

    def update_symbol(
        self,
        symbol: str,
        *,
        position: int = 0,
        entry_price: float = 0.0,
        unrealized_pnl: float = 0.0,
        regime: int = -1,
        kelly_f: float = 0.0,
        drawdown_pct: float = 0.0,
        last_signal: dict | None = None,
    ):
        with self._lock:
            sym = symbol.upper()
            existing = self._state.get(sym, {})
            # Preserve model / history fields written by update_model() and
            # add_signal_to_history() — do NOT overwrite them on every tick.
            _PRESERVE = (
                "model_version", "is_training", "last_retrain_time",
                "last_retrain_reason", "win_rate", "total_trades",
                "model_sharpe", "signals_history",
            )
            new_state = {
                "position": position,
                "entry_price": entry_price,
                "unrealized_pnl": round(unrealized_pnl, 4),
                "regime": regime,
                "kelly_f": round(kelly_f, 6),
                "drawdown_pct": round(drawdown_pct, 4),
                "last_signal": last_signal or {},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            for key in _PRESERVE:
                if key in existing:
                    new_state[key] = existing[key]
            self._state[sym] = new_state
            self._dirty = True

    def update_model(
        self,
        symbol: str,
        *,
        version: str | None = None,
        is_training: bool | None = None,
        last_retrain_time: str | None = None,
        last_retrain_reason: str | None = None,
        win_rate: float | None = None,
        total_trades: int | None = None,
        sharpe: float | None = None,
    ) -> None:
        """Merge AI-model / retrain stats into the symbol's state entry.

        Only fields explicitly passed (non-None) are written; existing values
        are preserved for omitted fields.
        """
        with self._lock:
            sym = symbol.upper()
            if sym not in self._state:
                self._state[sym] = {}
            if version is not None:
                self._state[sym]["model_version"] = version
            if is_training is not None:
                self._state[sym]["is_training"] = is_training
            if last_retrain_time is not None:
                self._state[sym]["last_retrain_time"] = last_retrain_time
            if last_retrain_reason is not None:
                self._state[sym]["last_retrain_reason"] = last_retrain_reason
            if win_rate is not None:
                self._state[sym]["win_rate"] = round(win_rate, 4)
            if total_trades is not None:
                self._state[sym]["total_trades"] = total_trades
            if sharpe is not None:
                self._state[sym]["model_sharpe"] = round(sharpe, 4)
            self._dirty = True

    def add_signal_to_history(
        self, symbol: str, side: int, price: float,
        win_prob: float, lot: float, rr: float,
    ) -> None:
        """Prepend signal to rolling history (max 5); newest first."""
        with self._lock:
            sym = symbol.upper()
            if sym not in self._state:
                self._state[sym] = {}
            hist = list(self._state[sym].get("signals_history", []))
            entry = {
                "s": side,                              # 1=buy,-1=sell,0=close
                "p": round(price, 2),
                "w": round(win_prob, 4),
                "l": round(lot, 2),
                "r": round(rr, 2),
                "t": datetime.now(timezone.utc).strftime("%H:%M"),
            }
            hist.insert(0, entry)
            self._state[sym]["signals_history"] = hist[:5]
            self._dirty = True

    def update_account(
        self,
        equity: float,
        balance: float,
        drawdown_pct: float,
        *,
        daily_loss_usd: float = 0.0,
        max_loss_usd: float = 0.0,
        profit_usd: float = 0.0,
        daily_loss_limit_usd: float = 0.0,
        max_loss_limit_usd: float = 0.0,
        profit_target_usd: float = 0.0,
        initial_balance: float = 0.0,
    ):
        with self._lock:
            self._state["_account"] = {
                "equity": round(equity, 2),
                "balance": round(balance, 2),
                "drawdown_pct": round(drawdown_pct, 4),
                "daily_loss_usd": round(daily_loss_usd, 2),
                "max_loss_usd": round(max_loss_usd, 2),
                "profit_usd": round(profit_usd, 2),
                "daily_loss_limit_usd": round(daily_loss_limit_usd, 2),
                "max_loss_limit_usd": round(max_loss_limit_usd, 2),
                "profit_target_usd": round(profit_target_usd, 2),
                "initial_balance": round(initial_balance, 2),
            }
            self._dirty = True

    def update_system(
        self,
        alive: bool,
        killed: bool,
        signal_count: int,
        kill_reason: str = "",
    ):
        with self._lock:
            now = datetime.now(timezone.utc)
            self._state["_system"] = {
                "alive": alive,
                "killed": killed,
                "signal_count": signal_count,
                "kill_reason": kill_reason,
                "last_heartbeat": now.isoformat(),
                "unix_time": int(now.timestamp()),
            }
            self._dirty = True

    def flush(self):
        """Atomically write state to both log path and MT5 path.

        Skips write when no update has occurred since last flush (_dirty flag).
        Uses compact JSON (no indent) to reduce serialization time by ~35%.
        Pre-sanitizes NaN/Inf to 0.0 before serializing (allow_nan=False raises
        ValueError directly — the `default` callback is not invoked for floats).
        """
        def _sanitize(obj):
            if isinstance(obj, dict):
                return {k: _sanitize(v) for k, v in obj.items()}
            if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
                return 0.0
            return obj

        with self._lock:
            if not self._dirty:
                return
            # Always stamp heartbeat on flush so panel sees fresh unix_time
            _now = datetime.now(timezone.utc)
            self._state["_system"]["last_heartbeat"] = _now.isoformat()
            self._state["_system"]["unix_time"] = int(_now.timestamp())
            payload = json.dumps(_sanitize(self._state))
            self._dirty = False

        for target in (self._log_path, self._mt5_path):
            try:
                tmp = target.with_suffix(".tmp")
                tmp.write_text(payload, encoding="utf-8")
                tmp.replace(target)
            except Exception as e:
                logger.debug(f"LiveStateWriter flush to {target}: {e}")


class Signal:
    """Trading signal data."""
    def __init__(
        self,
        side: int,       # 1=long, -1=short, 0=close
        price: float,
        sl: float,
        tp: float,
        lot: float,
        regime: int,
        z_score: float,
        win_prob: float,
        rr: float,
        timestamp: str | None = None,
        symbol: str = "",
    ):
        self.side = side
        self.price = price
        self.sl = sl
        self.tp = tp
        self.lot = lot
        self.regime = regime
        self.z_score = z_score
        self.win_prob = win_prob
        self.rr = rr
        self.timestamp = timestamp or datetime.now(timezone.utc).isoformat()
        self.symbol = symbol  # carrying symbol for Streamlit/Telegram

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, data: str) -> "Signal":
        d = json.loads(data)
        return cls(**d)


class SignalServer:
    """ZMQ PUB server that broadcasts trading signals to MT5.

    Topic = symbol name bytes (e.g. b"XAUUSD") so multiple EAs on different
    symbols can subscribe independently on the same port.

    Features:
      - Dynamic topic per symbol (replaces hardcoded b"XAU")
      - Sends periodic heartbeat so EA can detect publisher health
      - Thread-safe: heartbeat runs in background thread
      - Dual write: optionally mirrors signals to file for fallback EA
      - Writes shared live_state.json for Streamlit + Telegram
      - Multi-symbol: pass zmq_socket + zmq_lock to share one socket across workers
    """

    def __init__(
        self,
        symbol: str,
        addr: str = ZMQ_SIGNAL_ADDR,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
        file_fallback: bool = True,
        zmq_socket=None,
        zmq_lock=None,   # threading.Lock for thread-safe sends on shared socket
    ):
        self.symbol = symbol.upper()
        self._topic = self.symbol.encode()   # ZMQ topic = symbol bytes

        if zmq_socket is not None:
            # Shared socket provided by multi_runner — don't bind, don't own
            self.ctx = None
            self.socket = zmq_socket
            self._owns_socket = False
            self._lock = zmq_lock or threading.Lock()
        else:
            self.ctx = zmq.Context()
            self.socket = self.ctx.socket(zmq.PUB)
            self.socket.setsockopt(zmq.SNDHWM, 100)
            self.socket.setsockopt(zmq.LINGER, 0)
            self.socket.bind(addr)
            self._owns_socket = True
            self._lock = threading.Lock()

        self.addr = addr
        self.kelly = KellyPositionSizer(symbol=self.symbol)
        sym_cfg = get_symbol_config(self.symbol)
        self._contract_size = sym_cfg["contract_size"]
        self._atr_mult_sl   = sym_cfg["atr_mult_sl"]
        self._price_digits  = sym_cfg.get("price_digits", 5)
        self._max_lot       = sym_cfg.get("max_lot", 10.0)
        self._spread_bps    = sym_cfg["spread_bps"]

        # News filter: shared singleton — one HTTP calendar for all symbols
        self._news_filter = NewsFilter()

        # Kill switch: wired with news filter + session filter
        self.kill_switch = KillSwitch(
            news_filter=self._news_filter,
            symbol=self.symbol,
            session_filter=True,
        )

        # Spread monitoring: rolling history of recent spreads
        # Block entry when current_spread > SPREAD_SPIKE_MULT × rolling average
        self._spread_history: deque[float] = deque(maxlen=200)
        self._spread_spike_mult: float = SPREAD_SPIKE_MULT
        self._signal_count = 0
        self.state_writer = LiveStateWriter.instance()
        self.state_writer.register_server()

        # Heartbeat thread
        self._hb_interval = heartbeat_interval
        self._hb_stop = threading.Event()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True
        )
        self._hb_thread.start()

        # Optional file fallback
        self._file_writer = None
        if file_fallback:
            self._file_writer = SignalFileWriter(symbol=self.symbol)

        logger.info(
            f"Signal server {'attached to shared' if not self._owns_socket else 'bound to'} "
            f"{addr} topic={self.symbol} (HB every {heartbeat_interval}s)"
        )

    def _heartbeat_loop(self):
        """Background thread that sends heartbeat messages."""
        while not self._hb_stop.is_set():
            try:
                hb = json.dumps({
                    "heartbeat": True,
                    "symbol": self.symbol,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "signal_count": self._signal_count,
                })
                with self._lock:
                    self.socket.send(
                        self._topic + b" " + hb.encode(), zmq.NOBLOCK
                    )
                # Update system liveness in shared state
                self.state_writer.update_system(
                    alive=True,
                    killed=self.kill_switch.is_killed,
                    signal_count=self.state_writer.total_signals,
                    kill_reason=self.kill_switch.kill_reason,
                )
                # Refresh model_version every heartbeat so panel always shows it
                try:
                    from config import MODEL_DIR as _MD
                    _ckpt = _MD / f"ppo_{self.symbol.lower()}.zip"
                    _ver  = f"ppo_{self.symbol.lower()}"
                    if _ckpt.exists():
                        from datetime import datetime as _dt2
                        _mt = _dt2.fromtimestamp(_ckpt.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                        _ver = f"ppo_{self.symbol.lower()} [{_mt}]"
                    self.state_writer.update_model(self.symbol, version=_ver, is_training=False)
                except Exception:
                    pass
                self.state_writer.flush()
            except zmq.ZMQError as _e:
                # EAGAIN = socket would block (transient) — safe to ignore.
                # Any other error means the socket is in trouble; log it.
                if _e.errno != zmq.EAGAIN:
                    logger.warning("Heartbeat ZMQ error (errno=%d): %s", _e.errno, _e)
            self._hb_stop.wait(self._hb_interval)

    def publish(self, signal: Signal):
        """Publish signal to MT5 subscriber(s)."""
        msg = self._topic + b" " + signal.to_json().encode()
        with self._lock:
            self.socket.send(msg)
            self._signal_count += 1
        self.state_writer.add_signals(1)
        logger.info(
            f"[#{self._signal_count}] {self.symbol} Published: side={signal.side} "
            f"price={signal.price} lot={signal.lot} regime={signal.regime}"
        )
        if self._file_writer:
            self._file_writer.write(signal)

    def _update_spread(self, spread: float) -> bool:
        """Record spread and return True if it is within normal range.

        A spike (current > SPREAD_SPIKE_MULT × rolling mean) returns False,
        which causes generate_signal() to block new entries.
        Spread tracking is always updated regardless of outcome.
        """
        if spread > 0:
            self._spread_history.append(spread)
        if len(self._spread_history) < 10:
            return True          # not enough history — allow through
        avg = float(np.mean(self._spread_history))
        if avg <= 0:
            return True
        is_normal = spread <= avg * self._spread_spike_mult
        if not is_normal:
            logger.warning(
                f"[{self.symbol}] Spread spike: {spread:.5f} > {self._spread_spike_mult}× "
                f"avg {avg:.5f} — skipping entry"
            )
        return is_normal

    def generate_signal(
        self,
        action: int,
        mid_price: float,
        atr: float,
        z_score: float,
        regime: int,
        account_equity: float,
        atr_mult_sl: float | None = None,
        current_position: int = 0,
        entry_price: float = 0.0,
        current_spread: float = 0.0,   # live bid-ask spread for spike check
    ) -> Signal | None:
        """Generate a signal from RL agent action.

        Args:
            action: 0=hold, 1=long, 2=short, 3=close
            current_position/entry_price: for unrealized P&L in live state
        """
        if atr_mult_sl is None:
            atr_mult_sl = self._atr_mult_sl

        # Spread spike guard — update history and check before kill_switch
        spread_ok = self._update_spread(current_spread)

        status = self.kill_switch.check(account_equity)

        # Unrealised P&L — mark at the exit side (bid for longs, ask for shorts)
        # so the dashboard reflects what we'd actually receive if we closed now.
        # half_spread is derived from per-symbol spread_bps config.
        if entry_price and current_position != 0:
            half_spread = mid_price * (self._spread_bps / 10_000) / 2
            mark_price = mid_price - current_position * half_spread
            unrealized = current_position * (mark_price - entry_price)
        else:
            unrealized = 0.0
        self.state_writer.update_symbol(
            self.symbol,
            position=current_position,
            entry_price=entry_price,
            unrealized_pnl=unrealized,
            regime=regime,
            kelly_f=self.kelly.optimal_fraction(),
            drawdown_pct=status["drawdown_pct"],
        )
        self.state_writer.update_account(
            equity=account_equity,
            balance=account_equity,
            drawdown_pct=status["drawdown_pct"],
        )
        self.state_writer.flush()

        if status["should_close_all"]:
            return Signal(
                side=0, price=mid_price, sl=0, tp=0, lot=0,
                regime=regime, z_score=z_score, win_prob=0, rr=0,
                symbol=self.symbol,
            )
        if not status["allow_new_trades"] and action in (1, 2):
            return None

        # Spread spike: skip new entries only (allow close/hold)
        if not spread_ok and action in (1, 2):
            return None

        if action == 0:
            return None
        if action == 3:
            return Signal(
                side=0, price=mid_price, sl=0, tp=0, lot=0,
                regime=regime, z_score=z_score, win_prob=0, rr=0,
                symbol=self.symbol,
            )

        side = 1 if action == 1 else -1
        sl_dist = atr_mult_sl * atr
        tp_dist = 2 * atr_mult_sl * atr

        sl = mid_price - side * sl_dist
        tp = mid_price + side * tp_dist
        rr = tp_dist / (sl_dist + 1e-9)
        lot = self.kelly.calc_lot_size(
            account_equity=account_equity,
            entry_price=mid_price,
            sl_distance=sl_dist,
            contract_size=self._contract_size,
        )
        lot = min(lot, self._max_lot)   # per-symbol hard cap

        d = self._price_digits
        sig = Signal(
            side=side,
            price=round(mid_price, d),
            sl=round(sl, d),
            tp=round(tp, d),
            lot=lot,
            regime=regime,
            z_score=round(z_score, 4),
            win_prob=round(self.kelly.win_rate, 4),
            rr=round(rr, 2),
            symbol=self.symbol,
        )
        # Update last_signal + rolling signals history in live state
        self.state_writer.update_symbol(
            self.symbol,
            position=side,
            entry_price=mid_price,
            unrealized_pnl=0.0,
            regime=regime,
            kelly_f=self.kelly.optimal_fraction(),
            drawdown_pct=status["drawdown_pct"],
            last_signal=sig.__dict__,
        )
        self.state_writer.add_signal_to_history(
            self.symbol, side=side, price=mid_price,
            win_prob=self.kelly.win_rate, lot=lot, rr=rr,
        )
        self.state_writer.flush()
        return sig

    def close(self):
        """Shutdown server: stop heartbeat, close socket (only if owned)."""
        self._hb_stop.set()
        self._hb_thread.join(timeout=2)
        is_last = self.state_writer.unregister_server()
        if is_last:
            self.state_writer.update_system(
                alive=False, killed=self.kill_switch.is_killed,
                signal_count=self.state_writer.total_signals,
            )
            self.state_writer.flush()
        if self._owns_socket:
            with self._lock:
                self.socket.close()
            self.ctx.term()
        logger.info(
            f"Signal server closed. Symbol={self.symbol} "
            f"Total signals: {self._signal_count}"
        )


class SignalFileWriter:
    """Fallback file-based IPC when ZMQ is not available.
    Writes signal to a JSON file that the EA polls.
    File name is symbol-specific: {symbol_lower}_signal.json
    """

    def __init__(self, signal_path: Path | None = None, symbol: str = "XAUUSD"):
        if signal_path is None:
            from config import MT5_FILES_PATH
            signal_path = MT5_FILES_PATH
        fname = f"{symbol.lower()}_signal.json"
        self.signal_file = signal_path / fname
        self.signal_file.parent.mkdir(parents=True, exist_ok=True)

    def write(self, signal: Signal):
        self.signal_file.write_text(signal.to_json(), encoding="utf-8")
        logger.info(f"Signal written to {self.signal_file}")

    def clear(self):
        if self.signal_file.exists():
            self.signal_file.write_text("{}", encoding="utf-8")


def run_live_loop(
    model,
    regime_model,
    tick_source,
    account_info_fn,
    symbol: str = "XAUUSD",
    zmq_socket=None,
    zmq_lock=None,   # threading.Lock for thread-safe sends on shared socket
    perf_monitor=None,  # PerformanceMonitor — records closed-trade P&L for drift detection
):
    """Main live trading loop.

    Args:
        model: Trained PPO/SAC model
        regime_model: T-KAN regime classifier
        tick_source: Iterator yielding (df_window, mid_price, atr)
        account_info_fn: Callable returning (equity, balance)
        symbol: Trading instrument (default XAUUSD)
        zmq_socket: Optional shared ZMQ PUB socket (multi-symbol mode)
        zmq_lock: Lock for thread-safe sends on shared socket
    """
    server = SignalServer(symbol=symbol, zmq_socket=zmq_socket, zmq_lock=zmq_lock)
    time.sleep(1)  # wait for subscriber to connect

    current_position = 0
    entry_price = 0.0

    from ai_models.features import build_feature_matrix
    from config import FEATURE_DIM

    def _to_ohlc(df):
        """Return df augmented with OHLC columns if missing, without copying."""
        if "close" in df.columns:
            return df
        mid = df["mid"] if "mid" in df.columns else (df["bid"] + df["ask"]) / 2
        # Build only the new columns; assign to a view to avoid full copy
        extra = {"close": mid, "open": mid,
                 "high": df.get("ask", mid), "low": df.get("bid", mid)}
        return df.assign(**extra)

    # Feature cache: recompute every FEAT_INTERVAL ticks (features are smooth)
    FEAT_INTERVAL = 3
    _tick_count   = 0
    _obs_cache    = np.zeros(FEATURE_DIM, dtype=np.float32)
    _z_cache      = 0.0

    try:
        for df_window, mid_price, atr in tick_source:
            equity, _ = account_info_fn()
            _tick_count += 1

            # Recompute features every FEAT_INTERVAL ticks only
            if _tick_count % FEAT_INTERVAL == 1:
                try:
                    feat_df    = build_feature_matrix(_to_ohlc(df_window))
                    _z_cache   = float(feat_df["z_score"].iloc[-1])
                    _obs_cache = np.nan_to_num(
                        feat_df.values[-1], nan=0.0, posinf=0.0, neginf=0.0
                    ).astype(np.float32, copy=False)
                    # Feed realized vol to Kelly for volatility regime sizing (⑩)
                    if "rvol" in feat_df.columns:
                        rvol_val = float(feat_df["rvol"].iloc[-1])
                        server.kelly.update_rvol(rvol_val)
                except Exception:
                    pass  # keep previous cache on error

            # Use iloc[-51:-1] to exclude the current open (unfinished) bar
            # and use only the last 50 fully closed bars — avoids look-ahead bias.
            regime = (regime_model.predict(df_window.iloc[-51:-1].values)
                      if regime_model else 0)

            action, _ = model.predict(_obs_cache, deterministic=True)

            signal = server.generate_signal(
                action=int(action),
                mid_price=mid_price,
                atr=atr,
                z_score=_z_cache,
                regime=regime,
                account_equity=equity,
                current_position=current_position,
                entry_price=entry_price,
            )

            if signal is not None:
                new_side = signal.side
                # Only publish on genuine state change — suppress spam:
                #   • CLOSE when already flat  → no-op
                #   • LONG  when already LONG  → no-op
                #   • SHORT when already SHORT → no-op
                is_noop = (
                    (new_side == 0 and current_position == 0) or
                    (new_side != 0 and new_side == current_position)
                )
                if not is_noop:
                    server.publish(signal)
                    if new_side != 0:
                        current_position = new_side
                        entry_price = signal.price
                    else:
                        # Position closed — record P&L for drift detection
                        if current_position != 0 and perf_monitor is not None:
                            closed_pnl = current_position * (mid_price - entry_price)
                            perf_monitor.record_trade(closed_pnl)
                        current_position = 0
                        entry_price = 0.0

            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("Live loop stopped by user")
    finally:
        server.close()
