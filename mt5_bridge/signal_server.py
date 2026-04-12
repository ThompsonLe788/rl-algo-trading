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
import zmq
import numpy as np
from datetime import datetime, timezone
from pathlib import Path

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import ZMQ_SIGNAL_ADDR, EOD_HOUR_GMT, LOG_DIR, LIVE_STATE_PATH, get_symbol_config
from risk.kelly import KellyPositionSizer, vwap_slice_orders
from risk.kill_switch import KillSwitch

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
        self._state: dict = {
            "_account": {"equity": 0.0, "balance": 0.0, "drawdown_pct": 0.0},
            "_system": {
                "alive": True, "killed": False,
                "signal_count": 0, "kill_reason": "",
                "last_heartbeat": datetime.now(timezone.utc).isoformat(),
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
            self._state[symbol.upper()] = {
                "position": position,
                "entry_price": entry_price,
                "unrealized_pnl": round(unrealized_pnl, 4),
                "regime": regime,
                "kelly_f": round(kelly_f, 6),
                "drawdown_pct": round(drawdown_pct, 4),
                "last_signal": last_signal or {},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self._dirty = True

    def update_account(self, equity: float, balance: float, drawdown_pct: float):
        with self._lock:
            self._state["_account"] = {
                "equity": round(equity, 2),
                "balance": round(balance, 2),
                "drawdown_pct": round(drawdown_pct, 4),
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
            self._state["_system"] = {
                "alive": alive,
                "killed": killed,
                "signal_count": signal_count,
                "kill_reason": kill_reason,
                "last_heartbeat": datetime.now(timezone.utc).isoformat(),
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
        addr: str = ZMQ_SIGNAL_ADDR,
        symbol: str = "XAUUSD",
        heartbeat_interval: float = 5.0,
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
        self.kelly = KellyPositionSizer()
        sym_cfg = get_symbol_config(self.symbol)
        self._contract_size = sym_cfg["contract_size"]
        self._atr_mult_sl   = sym_cfg["atr_mult_sl"]
        self.kill_switch = KillSwitch()
        self._signal_count = 0
        self.state_writer = LiveStateWriter()

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
                    signal_count=self._signal_count,
                    kill_reason=self.kill_switch.kill_reason,
                )
                self.state_writer.flush()
            except zmq.ZMQError:
                pass
            self._hb_stop.wait(self._hb_interval)

    def publish(self, signal: Signal):
        """Publish signal to MT5 subscriber(s)."""
        msg = self._topic + b" " + signal.to_json().encode()
        with self._lock:
            self.socket.send(msg)
            self._signal_count += 1
        logger.info(
            f"[#{self._signal_count}] {self.symbol} Published: side={signal.side} "
            f"price={signal.price} lot={signal.lot} regime={signal.regime}"
        )
        if self._file_writer:
            self._file_writer.write(signal)

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
    ) -> Signal | None:
        """Generate a signal from RL agent action.

        Args:
            action: 0=hold, 1=long, 2=short, 3=close
            current_position/entry_price: for unrealized P&L in live state
        """
        if atr_mult_sl is None:
            atr_mult_sl = self._atr_mult_sl
        status = self.kill_switch.check(account_equity)

        # Update shared live state every call
        unrealized = current_position * (mid_price - entry_price) if entry_price else 0.0
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

        sig = Signal(
            side=side,
            price=round(mid_price, 2),
            sl=round(sl, 2),
            tp=round(tp, 2),
            lot=lot,
            regime=regime,
            z_score=round(z_score, 4),
            win_prob=round(self.kelly.win_rate, 4),
            rr=round(rr, 2),
            symbol=self.symbol,
        )
        # Update last_signal in live state
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
        self.state_writer.flush()
        return sig

    def close(self):
        """Shutdown server: stop heartbeat, close socket (only if owned)."""
        self._hb_stop.set()
        self._hb_thread.join(timeout=2)
        self.state_writer.update_system(
            alive=False, killed=self.kill_switch.is_killed,
            signal_count=self._signal_count,
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
                    # Combine astype + nan_to_num in one pass (BUG-14)
                    _obs_cache = np.nan_to_num(
                        feat_df.values[-1], nan=0.0, posinf=0.0, neginf=0.0
                    ).astype(np.float32, copy=False)
                except Exception:
                    pass  # keep previous cache on error

            regime = (regime_model.predict(df_window.iloc[-50:].values)
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
