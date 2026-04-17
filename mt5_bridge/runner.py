"""Multi-symbol live runner — forex / CFD.

One SymbolWorker per open MT5 chart; one shared account-level risk manager
governs all symbols together (drawdown and daily-loss limits are account-wide).

Auto-detects open MT5 charts via ats_chart_{SYMBOL}.txt files written by
ATS_Panel.mq5. Any symbol works: XAUUSD, EURUSD, GBPUSD, USDJPY, BTCUSD, etc.

Cross-symbol risk allocation
-----------------------------
With N symbols active, each worker sizes Kelly positions against
``equity / N`` so total account exposure never exceeds the single-symbol
risk budget regardless of how many charts are open.

Usage:
    python start.py              # auto-detect open MT5 charts
    python main.py run           # same, CLI entry
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import (
    LOG_DIR, MT5_FILES_PATH, MAX_DRAWDOWN_PCT, DAILY_LOSS_LIMIT_PCT,
    PROFIT_TARGET_PCT, SCAN_INTERVAL, RETRAIN_BARS,
    AUTO_TRAIN_TIMESTEPS as _AUTO_TRAIN_TIMESTEPS,
)
from risk.journal import TradeJournal

logger = logging.getLogger("runner")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [runner] %(message)s",
)

_LOCK_FILE = LOG_DIR / "runner.lock"


# ─────────────────────────────────────────────────────────────────────────────
# Standard (non-prop-fund) risk adapter — same interface as PropFundRiskManager
# ─────────────────────────────────────────────────────────────────────────────

class StandardRiskAdapter:
    """Account-level risk for standard mode.

    Wraps KillSwitch and exposes the same dict interface used by SymbolWorker.
    so SymbolWorker runs identically regardless of mode.
    Session/news/EOD checks are handled inside SymbolWorker and not duplicated here.
    """

    def __init__(self) -> None:
        from risk.kill_switch import KillSwitch
        self._ks = KillSwitch(
            max_drawdown_pct=MAX_DRAWDOWN_PCT,
            daily_loss_limit_pct=DAILY_LOSS_LIMIT_PCT,
            session_filter=False,
            news_filter=None,
        )
        self._initial_equity: float = 0.0
        self._lock = threading.Lock()

    def set_initial_equity(self, equity: float) -> None:
        with self._lock:
            self._initial_equity = equity
            self._ks.set_session_start(equity)

    @property
    def initial_equity(self) -> float:
        return self._initial_equity

    @property
    def peak_equity(self) -> float:
        return self._ks.peak_equity

    @property
    def session_start_equity(self) -> float:
        return self._ks.session_start_equity

    def record_closed_trade(self, pnl_usd: float, trade_date=None) -> None:
        pass  # KillSwitch is equity-based; no per-trade bookkeeping needed

    @property
    def is_killed(self) -> bool:
        return self._ks.is_killed

    @property
    def kill_reason(self) -> str:
        return self._ks.kill_reason

    @property
    def trading_days_count(self) -> int:
        return 0

    @property
    def cumulative_profit(self) -> float:
        return 0.0

    def check(
        self,
        current_equity: float,
        open_pnl:       float,
        proposed_lot:   float,
        symbol:         str,
    ) -> dict:
        with self._lock:
            ks = self._ks.check(current_equity)
        dd_pct    = ks["drawdown_pct"]
        daily_pct = ks["daily_loss_pct"]
        # Use actual session_start and peak from KillSwitch for correct dollar amounts.
        # init_eq - current_equity is always 0 at startup restart; peak/session_start
        # reflect the real high-water marks that KillSwitch restores from journal.
        daily_loss_usd = round(max(0.0, self._ks.session_start_equity - current_equity), 2)
        total_loss_usd = round(max(0.0, self._ks.peak_equity - current_equity), 2)
        base = {
            "daily_loss_usd":  daily_loss_usd,
            "total_loss_usd":  total_loss_usd,
            "daily_loss_pct":  round(daily_pct, 4),
            "total_dd_pct":    round(dd_pct, 4),
            "profit_progress": 0.0,
            "trading_days":    0,
            "consistency_ok":  True,
        }
        if not ks["allow_new_trades"]:
            return {**base, "allow": False, "adjusted_lot": 0.0,
                    "reason": ks["reason"], "close_all": ks["should_close_all"]}
        return {**base, "allow": True, "adjusted_lot": proposed_lot,
                "reason": "", "close_all": False}

    def state_dict(self) -> dict:
        eq = self._initial_equity
        return {
            "phase":             "standard",
            "account_size":      eq,
            "initial_equity":    eq,
            "cumulative_profit": 0.0,
            "profit_pct":        0.0,
            "profit_target_usd": 0.0,
            "profit_target_pct": 0.0,
            "max_daily_loss_usd":round(eq * DAILY_LOSS_LIMIT_PCT / 100, 2),
            "max_total_loss_usd":round(eq * MAX_DRAWDOWN_PCT / 100, 2),
            "trading_days":      0,
            "min_trading_days":  0,
            "daily_limit_hit":   False,
            "total_limit_hit":   self._ks.is_killed,
            "kill_reason":       self._ks.kill_reason,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Per-symbol worker thread
# ─────────────────────────────────────────────────────────────────────────────

class SymbolWorker(threading.Thread):
    """Live-trading worker for one symbol — standard or prop-fund mode.

    Shares one account-level risk manager with all sibling workers so
    account-wide DD and daily-loss limits are consistent across all charts.
    Kelly lot sizes are scaled by 1/N (N = active workers) for proportional
    risk allocation.
    """

    _status: dict[str, str] = {}
    _status_lock = threading.Lock()

    _active_count: int = 0
    _active_lock  = threading.Lock()

    _zmq_ctx       = None
    _zmq_socket    = None
    _zmq_send_lock = threading.Lock()
    _zmq_init_lock = threading.Lock()

    AUTO_TRAIN_BARS      = RETRAIN_BARS          # from config
    AUTO_TRAIN_TIMESTEPS = _AUTO_TRAIN_TIMESTEPS # from config (bootstrap PPO)

    @classmethod
    def get_shared_socket(cls):
        with cls._zmq_init_lock:
            if cls._zmq_socket is None:
                import zmq
                from config import ZMQ_SIGNAL_ADDR
                cls._zmq_ctx    = zmq.Context()
                cls._zmq_socket = cls._zmq_ctx.socket(zmq.PUB)
                cls._zmq_socket.setsockopt(zmq.SNDHWM, 100)
                cls._zmq_socket.setsockopt(zmq.LINGER, 0)
                cls._zmq_socket.bind(ZMQ_SIGNAL_ADDR)
                logger.info("Shared ZMQ PUB socket bound")
        return cls._zmq_socket

    @classmethod
    def _inc_active(cls) -> None:
        with cls._active_lock:
            cls._active_count += 1

    @classmethod
    def _dec_active(cls) -> None:
        with cls._active_lock:
            cls._active_count = max(0, cls._active_count - 1)

    @classmethod
    def active_count(cls) -> int:
        """Number of workers currently in 'live' state."""
        with cls._status_lock:
            live = sum(1 for s in cls._status.values() if s == "live")
        if live > 0:
            return live
        with cls._active_lock:
            return max(1, cls._active_count)

    @classmethod
    def active_symbols(cls) -> list[str]:
        """List of symbols whose worker is in 'live' state (used by Portfolio Kelly)."""
        with cls._status_lock:
            live = [s for s, st in cls._status.items() if st == "live"]
        if live:
            return live
        # During startup before any worker goes live, return all registered symbols
        with cls._status_lock:
            return list(cls._status.keys()) or ["UNKNOWN"]

    def __init__(
        self,
        symbol: str,
        risk,           # StandardRiskAdapter
    ):
        super().__init__(name=f"worker-{symbol}", daemon=True)
        self.symbol   = symbol
        self._risk    = risk
        self._journal = TradeJournal(
            path=LOG_DIR / f"trade_journal_{symbol.lower()}.json",
        )
        # Seed _last_deal from existing journal so _sync_closed_trades
        # skips deals already recorded — prevents duplicate entries and
        # false consistency-rule triggers on restart.
        existing_tickets = [
            int(t.trade_id) for t in self._journal.trades
            if t.trade_id.isdigit()
        ]
        if existing_tickets:
            _last_deal[symbol] = max(existing_tickets)
        self._stop_event = threading.Event()
        self._set_status("waiting")

    def _set_status(self, status: str) -> None:
        with self.__class__._status_lock:
            if self.__class__._status.get(self.symbol) == status:
                return
            self.__class__._status[self.symbol] = status
            snapshot = dict(self.__class__._status)
        logger.info(f"[{self.symbol}] status -> {status}")
        try:
            p   = LOG_DIR / "worker_status.json"
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            tmp.replace(p)
        except Exception:
            pass

    def _auto_train(self, mt5_already_init: bool = False):
        import MetaTrader5 as mt5
        from data.pipeline import load_or_fetch, generate_synthetic_data
        from ai_models.rl_agent import train_ppo

        self._set_status("training")
        df = None

        init_here = not mt5_already_init and mt5.initialize()
        if mt5_already_init or init_here:
            try:
                logger.info(f"[{self.symbol}] Fetching {self.AUTO_TRAIN_BARS} bars from MT5...")
                df = load_or_fetch(symbol=self.symbol, timeframe="M1",
                                   num_bars=self.AUTO_TRAIN_BARS)
                logger.info(f"[{self.symbol}] Fetched {len(df)} bars")
            except Exception as exc:
                logger.warning(f"[{self.symbol}] MT5 fetch failed: {exc} — using synthetic")
            finally:
                if init_here:
                    mt5.shutdown()

        if df is None or len(df) < 500:
            from data.pipeline import generate_synthetic_data
            logger.info(f"[{self.symbol}] Generating {self.AUTO_TRAIN_BARS} synthetic bars")
            df = generate_synthetic_data(n_bars=self.AUTO_TRAIN_BARS)

        logger.info(f"[{self.symbol}] Training PPO ({self.AUTO_TRAIN_TIMESTEPS} steps)...")
        model = train_ppo(df, total_timesteps=self.AUTO_TRAIN_TIMESTEPS, symbol=self.symbol)
        logger.info(f"[{self.symbol}] Auto-train complete")
        return model

    def run(self) -> None:
        self.__class__._inc_active()
        try:
            import MetaTrader5 as mt5
            from data.pipeline import LiveTickStream
            from ai_models.rl_agent import load_ppo
            from mt5_bridge.auto_retrainer import AutoRetrainer, ModelRef
            from risk.news_filter import NewsFilter
            from risk.performance_monitor import PerformanceMonitor
            from config import EOD_HOUR_GMT, get_symbol_config
            from mt5_bridge.signal_server import LiveStateWriter
            # Initialize kelly early so it's never unbound in any code path
            from risk.kelly import KellyPositionSizer, PortfolioKellyAllocator
            kelly = KellyPositionSizer(symbol=self.symbol)
            _portfolio_alloc = PortfolioKellyAllocator.instance()

            sym_cfg = get_symbol_config(self.symbol)
            news    = NewsFilter()

            mt5_ok = mt5.initialize()

            try:
                model = load_ppo(symbol=self.symbol)
                logger.info(f"[{self.symbol}] Model loaded")
            except Exception:
                logger.warning(f"[{self.symbol}] No model — starting auto-train")
                try:
                    model = self._auto_train(mt5_already_init=mt5_ok)
                except Exception as exc:
                    logger.error(f"[{self.symbol}] Auto-train failed: {exc}")
                    self._set_status("error")
                    return

            if self._stop_event.is_set():
                return

            if not mt5_ok:
                logger.error(f"[{self.symbol}] MT5 init failed: {mt5.last_error()}")
                self._set_status("error")
                return

            _eq_cache: list[float] = [0.0]

            def account_info():
                info = mt5.account_info()
                if info is None or info.equity <= 0:
                    cached = _eq_cache[0]
                    return (cached, cached) if cached > 0 else (0.0, 0.0)
                _eq_cache[0] = info.equity
                return info.equity, info.balance

            model_ref    = ModelRef(model)
            _live_model  = model
            perf_monitor = PerformanceMonitor(symbol=self.symbol)
            state_writer = LiveStateWriter.instance()
            state_writer.register_server()

            # Background heartbeat thread — flushes live_state.json AND sends
            # ZMQ heartbeat every 5s even when no ticks arrive (EOD, news, etc.)
            def _heartbeat_loop():
                while not self._stop_event.is_set():
                    try:
                        eq, bal = account_info()
                        LiveStateWriter.instance().update_account(
                            eq, bal, _last_dd_pct[0], **_last_acct_extra)
                        state_writer.flush()
                    except Exception:
                        pass
                    try:
                        import zmq as _zmq
                        _sock = self.__class__.get_shared_socket()
                        _hb_payload = json.dumps({
                            "heartbeat": True,
                            "symbol": self.symbol,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                        # EA expects single-frame: "SYMBOL {...json...}"
                        _hb_frame = f"{self.symbol} {_hb_payload}".encode()
                        with self.__class__._zmq_send_lock:
                            _sock.send(_hb_frame, flags=_zmq.NOBLOCK)
                    except Exception:
                        pass
                    self._stop_event.wait(timeout=5.0)

            _last_dd_pct = [0.0]
            _last_acct_extra: dict = {}  # dollar amounts updated each tick, read by heartbeat
            _hb_thread = threading.Thread(
                target=_heartbeat_loop, name=f"hb-{self.symbol}", daemon=True
            )
            _hb_thread.start()

            from config import MODEL_DIR
            ckpt = MODEL_DIR / f"ppo_{self.symbol.lower()}.zip"
            ver  = f"ppo_{self.symbol.lower()}"
            if ckpt.exists():
                mtime = datetime.fromtimestamp(ckpt.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                ver   = f"ppo_{self.symbol.lower()} [{mtime}]"
            # version-only update at startup; Kelly stats added after kelly is init'd
            state_writer.update_model(self.symbol, version=ver, is_training=False)
            state_writer.flush()

            retrainer = AutoRetrainer(
                symbol=self.symbol,
                model_ref=model_ref,
                perf_monitor=perf_monitor,
                stop_event=self._stop_event,
                set_status_fn=self._set_status,
                state_writer=state_writer,
            )
            retrainer.start()

            tick_stream = _StoppableTicks(self.symbol, stop_event=self._stop_event)

            # Populate AI MODEL panel fields from Kelly history (loaded from JSON)
            state_writer.update_model(
                self.symbol,
                win_rate=kelly.win_rate,
                total_trades=kelly.num_trades,
                sharpe=kelly.expectancy,
            )
            state_writer.flush()

            from ai_models.regime_tkan import TKAN
            from config import MODEL_DIR, TKAN_SEQ_LEN, TKAN_INPUT_DIM
            import torch as _torch
            _tkan_path  = MODEL_DIR / f"regime_tkan_{self.symbol.lower()}.pt"
            _tkan_model = None
            try:
                _tkan_model = TKAN()
                _tkan_model.load_state_dict(
                    _torch.load(_tkan_path, map_location="cpu", weights_only=True)
                )
                _tkan_model.eval()
                logger.info(f"[{self.symbol}] T-KAN regime model loaded")
            except Exception as _e:
                logger.warning(f"[{self.symbol}] T-KAN not available: {_e}")

            import numpy as _np
            from ai_models.features import build_feature_matrix as _bfm

            self._set_status("live")
            logger.info(f"[{self.symbol}] Live loop started")

            zmq_socket = self.__class__.get_shared_socket()
            _tick_count = 0  # used to throttle periodic update_model() calls

            for tick_tuple in tick_stream:
                if self._stop_event.is_set():
                    break

                tick_df, mid_price, tick_atr = tick_tuple

                # Ensure OHLC columns exist before any feature computation
                if "close" not in tick_df.columns:
                    import numpy as _np2
                    mid_s = tick_df["mid"] if "mid" in tick_df.columns else (
                        tick_df["bid"] + tick_df["ask"]) / 2
                    tick_df = tick_df.assign(
                        close=mid_s, open=mid_s,
                        high=tick_df.get("ask", mid_s),
                        low=tick_df.get("bid", mid_s),
                    )

                equity, balance = account_info()
                now_utc = datetime.now(timezone.utc)

                # Heartbeat flush every tick — keeps dashboard/panel last_updated fresh
                # even during EOD/news blocks when no signal is generated.
                LiveStateWriter.instance().update_account(
                    equity, balance, _last_dd_pct[0], **_last_acct_extra)
                state_writer.flush()

                if self._risk.is_killed:
                    logger.warning(f"[{self.symbol}] Account killed — stopping")
                    break

                if now_utc.hour >= EOD_HOUR_GMT:
                    _send(zmq_socket, self.symbol, "HOLD", 0.0, 0.0,
                          "EOD", self.__class__._zmq_send_lock)
                    continue

                blocked, ev_name = news.is_blackout(now=now_utc, currencies=["USD"])
                if blocked:
                    _send(zmq_socket, self.symbol, "HOLD", 0.0, 0.0,
                          f"News:{ev_name}", self.__class__._zmq_send_lock)
                    continue

                total_open_pnl = _get_total_open_pnl()
                open_pnl       = _get_open_pnl(self.symbol)

                _regime = -1
                if _tkan_model is not None:
                    try:
                        _feat_df   = _bfm(tick_df).fillna(0.0)
                        _feat_vals = _feat_df.values[-TKAN_SEQ_LEN:, :TKAN_INPUT_DIM]
                        if len(_feat_vals) >= TKAN_SEQ_LEN:
                            _regime = _tkan_model.predict(_feat_vals[-TKAN_SEQ_LEN:])
                    except Exception as _tkan_err:
                        logger.warning(f"[{self.symbol}] T-KAN predict failed: {_tkan_err}")

                try:
                    sl_dist    = tick_atr * sym_cfg.get("atr_mult_sl", 1.5)
                    init_eq    = self._risk.initial_equity  # public property
                    kelly.set_drawdown(
                        (init_eq - equity) / max(init_eq, 1) * 100.0
                    )
                    kelly.set_regime(_regime)
                    kelly.update_rvol(tick_atr)

                    # Feed tick return to Portfolio Kelly allocator
                    if len(tick_df) >= 2:
                        _prev = float(tick_df["close"].iloc[-2])
                        _curr = float(tick_df["close"].iloc[-1])
                        if _prev > 0:
                            _portfolio_alloc.update_return(
                                self.symbol, (_curr - _prev) / _prev
                            )

                    # Correlation-adjusted equity budget (falls back to equity/N
                    # when < 20 observations or only one symbol active)
                    _active_syms = self.__class__.active_symbols()
                    equity_budget = _portfolio_alloc.equity_budget(
                        self.symbol, equity, _active_syms
                    )

                    kelly_lot = kelly.calc_lot_size(
                        account_equity=equity_budget,
                        entry_price=mid_price,
                        sl_distance=max(sl_dist, 1e-6),
                    )
                except Exception:
                    kelly_lot = sym_cfg["min_lot"]  # derived from lot_precision in config

                risk_result = self._risk.check(
                    current_equity=equity,
                    open_pnl=total_open_pnl,
                    proposed_lot=kelly_lot,
                    symbol=self.symbol,
                )
                _last_dd_pct[0] = risk_result.get("total_dd_pct", 0.0)
                # Use peak_equity as the reference base for limits so dollar amounts
                # reflect the KillSwitch high-water mark (survives restarts via journal).
                _peak_eq = self._risk.peak_equity or balance
                _sess_eq = self._risk.session_start_equity or balance
                _limit_base = max(_peak_eq, balance)
                _last_acct_extra.update(
                    daily_loss_usd=risk_result.get("daily_loss_usd", 0.0),
                    max_loss_usd=risk_result.get("total_loss_usd", 0.0),
                    profit_usd=max(0.0, round(equity - _sess_eq, 2)),
                    daily_loss_limit_usd=round(_limit_base * DAILY_LOSS_LIMIT_PCT / 100, 2),
                    max_loss_limit_usd=round(_limit_base * MAX_DRAWDOWN_PCT / 100, 2),
                    profit_target_usd=round(_limit_base * PROFIT_TARGET_PCT / 100, 2),
                    initial_balance=round(_limit_base, 2),
                )

                if risk_result.get("close_all"):
                    logger.critical(
                        f"[{self.symbol}] MAX DD — closing all positions: {risk_result['reason']}"
                    )
                    _close_all_positions(reason=risk_result["reason"])
                    _send(zmq_socket, self.symbol, "CLOSE", 0.0, 0.0,
                          risk_result["reason"], self.__class__._zmq_send_lock)
                    break

                if not risk_result["allow"]:
                    logger.warning(f"[{self.symbol}] Blocked: {risk_result['reason']}")
                    _send(zmq_socket, self.symbol, "HOLD", 0.0, 0.0,
                          risk_result["reason"], self.__class__._zmq_send_lock)
                    continue

                try:
                    import numpy as __np
                    feats = _bfm(tick_df).fillna(0.0)
                    _row  = feats.iloc[-1].to_numpy(dtype=float, na_value=0.0)
                    obs   = __np.where(__np.isfinite(_row), _row, 0.0).astype(__np.float32)
                    if model_ref._model is not _live_model:
                        _live_model = model_ref._model
                    action, _ = _live_model.predict(obs, deterministic=True)
                except Exception as exc:
                    logger.error(f"[{self.symbol}] Inference error: {exc}")
                    continue

                action_name = ["HOLD", "LONG", "SHORT", "CLOSE"][int(action)]
                final_lot   = risk_result["adjusted_lot"]
                price       = float(tick_df["close"].iloc[-1])
                z_score     = float(feats["z_score"].iloc[-1]) if "z_score" in feats.columns else 0.0
                win_prob    = float(kelly.win_rate)

                _send(zmq_socket, self.symbol, action_name, price, final_lot,
                      "", self.__class__._zmq_send_lock)

                _sync_closed_trades(self.symbol, self._journal, now_utc, kelly)

                _last_signal: dict = {}
                if action_name in ("LONG", "SHORT"):
                    _side = 1 if action_name == "LONG" else -1
                    _last_signal = {
                        "side": _side,
                        "price": round(price, 5),
                        "lot": round(final_lot, 2),
                        "sl": 0.0, "tp": 0.0,
                        "win_prob": round(win_prob, 4),
                        "z_score": round(z_score, 4),
                        "rr": 0.0,
                        "timestamp": now_utc.isoformat(),
                    }
                    state_writer.add_signal_to_history(
                        self.symbol, _side, price,
                        win_prob=win_prob, lot=final_lot, rr=0.0,
                    )

                state_writer.update_symbol(
                    symbol=self.symbol,
                    position=0,
                    entry_price=0.0,
                    unrealized_pnl=open_pnl,
                    regime=_regime,
                    kelly_f=kelly.optimal_fraction() / max(len(_active_syms), 1),
                    drawdown_pct=risk_result["total_dd_pct"],
                    last_signal=_last_signal or None,
                )
                LiveStateWriter.instance().update_account(equity, balance, risk_result["total_dd_pct"])
                state_writer.flush()

                # Refresh AI MODEL panel fields from Kelly every 60 ticks
                _tick_count += 1
                if _tick_count % 60 == 0:
                    state_writer.update_model(
                        self.symbol,
                        win_rate=kelly.win_rate,
                        total_trades=kelly.num_trades,
                        sharpe=kelly.expectancy,
                    )

            mt5.shutdown()

        except Exception as exc:
            logger.exception(f"[{self.symbol}] Worker crashed: {exc}")
            self._set_status("error")
        finally:
            self.__class__._dec_active()
            from mt5_bridge.signal_server import LiveStateWriter
            LiveStateWriter.instance().unregister_server()
            if self.__class__._status.get(self.symbol) == "live":
                self._set_status("waiting")
            logger.info(f"[{self.symbol}] Worker stopped")

    def stop(self) -> None:
        self._stop_event.set()


# ─────────────────────────────────────────────────────────────────────────────
# Stoppable tick stream
# ─────────────────────────────────────────────────────────────────────────────

class _StoppableTicks:
    def __init__(self, symbol: str, stop_event: threading.Event, window: int = 200):
        from data.pipeline import LiveTickStream
        self._inner = LiveTickStream(symbol=symbol, window=window)
        self._stop  = stop_event

    def __iter__(self):
        return self

    def __next__(self):
        if self._stop.is_set():
            raise StopIteration
        result = next(self._inner)
        if self._stop.wait(timeout=1.0):
            raise StopIteration
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _send(sock, symbol: str, action: str, price: float, lot: float,
          reason: str, lock: threading.Lock) -> None:
    import zmq
    _side_map = {"LONG": 1, "SHORT": -1, "CLOSE": 0, "HOLD": 0}
    payload = json.dumps({
        "symbol": symbol, "action": action,
        "side": _side_map.get(action, 0),   # numeric for EA compatibility
        "price": price, "lot": lot, "reason": reason,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    # EA expects single-frame: "SYMBOL {...json...}" — NOT multipart
    frame = f"{symbol} {payload}".encode()
    with lock:
        try:
            sock.send(frame, flags=zmq.NOBLOCK)
        except Exception:
            pass


def _get_open_pnl(symbol: str) -> float:
    try:
        import MetaTrader5 as mt5
        pos = mt5.positions_get(symbol=symbol)
        return sum(p.profit for p in pos) if pos else 0.0
    except Exception:
        return 0.0


def _get_total_open_pnl() -> float:
    try:
        import MetaTrader5 as mt5
        pos = mt5.positions_get()
        return sum(p.profit for p in pos) if pos else 0.0
    except Exception:
        return 0.0


def _close_all_positions(reason: str = "Max DD") -> None:
    try:
        import MetaTrader5 as mt5
        positions = mt5.positions_get()
        if not positions:
            return
        closed = 0
        for pos in positions:
            sym   = pos.symbol
            tick  = mt5.symbol_info_tick(sym)
            if tick is None:
                continue
            price = tick.bid if pos.type == 0 else tick.ask
            res   = mt5.order_send({
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       sym,
                "volume":       pos.volume,
                "type":         mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY,
                "position":     pos.ticket,
                "price":        price,
                "deviation":    20,
                "magic":        pos.magic,
                "comment":      reason,
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_RETURN,
            })
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                closed += 1
                logger.info(f"[CLOSE ALL] {sym} ticket={pos.ticket} OK")
            else:
                logger.error(f"[CLOSE ALL] {sym} ticket={pos.ticket} failed: {res}")
        logger.critical(f"[CLOSE ALL] {closed}/{len(positions)} positions closed — {reason}")
    except Exception as exc:
        logger.error(f"Close all positions error: {exc}")


_last_deal: dict[str, int] = {}


def _sync_closed_trades(symbol: str, journal: TradeJournal, now_utc, kelly=None) -> None:
    try:
        import MetaTrader5 as mt5
        from datetime import timedelta
        deals = mt5.history_deals_get(now_utc - timedelta(hours=24), now_utc,
                                      group=f"*{symbol}*")
        if not deals:
            return
        last = _last_deal.get(symbol, 0)
        for d in deals:
            if d.ticket <= last or d.entry != mt5.DEAL_ENTRY_OUT:
                continue
            direction = "long" if d.type == mt5.DEAL_TYPE_BUY else "short"
            t = datetime.fromtimestamp(d.time, tz=timezone.utc)
            journal.add_trade(symbol, direction, d.price, d.price, d.volume,
                              t, t, d.profit, d.commission, str(d.ticket))
            if kelly is not None:
                kelly.record_trade(pnl=d.profit, timestamp=d.time)
            _last_deal[symbol] = d.ticket
    except Exception as exc:
        logger.debug(f"Deal sync {symbol}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Single-instance lock
# ─────────────────────────────────────────────────────────────────────────────

def _acquire_instance_lock() -> bool:
    import atexit
    import subprocess

    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, AttributeError):
            pass
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                stderr=subprocess.DEVNULL,
            ).decode(errors="replace")
            return str(pid) in out
        except Exception:
            return False

    if _LOCK_FILE.exists():
        try:
            existing_pid = int(_LOCK_FILE.read_text().strip())
            if _pid_alive(existing_pid):
                logger.warning(
                    f"Another runner instance is already running (PID {existing_pid}). "
                    "Exiting to prevent duplicate trading."
                )
                return False
        except ValueError:
            pass
        logger.info("Removing stale lock file (PID no longer running).")

    _LOCK_FILE.write_text(str(os.getpid()))
    atexit.register(lambda: _LOCK_FILE.unlink(missing_ok=True))
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def _scan_symbols(mt5_files_path: Path) -> set[str]:
    """Return symbols whose ats_chart_*.txt == '1' AND have a live MT5 tick.

    The file check catches what ATS_Panel registered; the tick check filters
    out stale files left behind by crashes (OnDeinit not called).
    """
    candidates: set[str] = set()
    try:
        for f in mt5_files_path.glob("ats_chart_*.txt"):
            try:
                for enc in ("utf-16", "utf-8", "latin-1"):
                    try:
                        content = f.read_text(encoding=enc).strip()
                        break
                    except (UnicodeDecodeError, UnicodeError):
                        content = ""
                if content == "1":
                    candidates.add(f.stem.replace("ats_chart_", "").upper())
            except OSError:
                pass
    except OSError:
        pass

    if not candidates:
        return candidates

    # Cross-verify with MT5: symbol must be visible and have a live tick
    active: set[str] = set()
    try:
        import MetaTrader5 as mt5
        if mt5.initialize():
            for sym in candidates:
                tick = mt5.symbol_info_tick(sym)
                if tick is not None and tick.time > 0:
                    active.add(sym)
                else:
                    logger.warning(
                        f"[scan] {sym}: file='1' but no live tick — "
                        "stale file (crash?), skipping"
                    )
            mt5.shutdown()
    except Exception:
        # MT5 unavailable — fall back to file-only result
        active = candidates

    return active


def run_multi_live(mt5_files_path: Path = MT5_FILES_PATH) -> None:
    """Multi-symbol live runner — auto-detects all open MT5 charts."""
    if not _acquire_instance_lock():
        return

    risk = StandardRiskAdapter()

    logger.info("=" * 60)
    logger.info("Runner started — scanning for open MT5 charts")
    logger.info(f"  Max DD: {MAX_DRAWDOWN_PCT}%  Daily limit: {DAILY_LOSS_LIMIT_PCT}%")
    logger.info("=" * 60)
    logger.info(f"Scanning {mt5_files_path} every {SCAN_INTERVAL}s for open charts")
    logger.info("Open any chart in MT5 and attach ATS_Panel — it auto-registers.")

    _equity_initialized = False
    workers: dict[str, SymbolWorker] = {}

    try:
        import MetaTrader5 as mt5
        if mt5.initialize():
            info = mt5.account_info()
            if info and info.balance > 0:
                risk.set_initial_equity(info.balance)
                _equity_initialized = True
                logger.info(f"Account #{info.login}  balance=${info.balance:,.2f}  equity=${info.equity:,.2f}")
            mt5.shutdown()

        while True:
            if not _equity_initialized:
                try:
                    if mt5.initialize():
                        info = mt5.account_info()
                        if info and info.balance > 0:
                            risk.set_initial_equity(info.balance)
                            _equity_initialized = True
                            logger.info(f"Account #{info.login}  balance=${info.balance:,.2f}")
                        mt5.shutdown()
                except Exception:
                    pass

            active = _scan_symbols(mt5_files_path)

            for sym in active:
                if sym not in workers or not workers[sym].is_alive():
                    logger.info(f"New chart: {sym} — starting worker")
                    w = SymbolWorker(sym, risk)
                    workers[sym] = w
                    w.start()

            for sym in list(workers):
                if sym not in active and workers[sym].is_alive():
                    logger.info(f"Chart closed: {sym} — stopping worker")
                    workers[sym].stop()

            if risk.is_killed:
                logger.critical(
                    f"Kill-switch triggered — stopping all workers. "
                    f"Reason: {risk.kill_reason}"
                )
                for w in workers.values():
                    w.stop()
                break

            alive = [s for s, w in workers.items() if w.is_alive()]
            if alive:
                logger.info(f"Active: {', '.join(sorted(alive))}  "
                            f"(budget per symbol: equity/{len(alive)})")
            else:
                logger.info("No active charts — waiting...")

            time.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Shutting down...")
        for w in workers.values():
            w.stop()
        for w in workers.values():
            w.join(timeout=5)
        logger.info("All workers stopped.")


if __name__ == "__main__":
    run_multi_live()
