"""Multi-symbol live runner.

Scans MT5 Common Files for ats_chart_{SYMBOL}.txt written by ATS_Panel.mq5.
Spawns/stops a signal-server thread for each active chart automatically.

Usage:
  python mt5_bridge/multi_runner.py          # auto-detect from open charts
  python main.py multi-live                  # same via main.py CLI
"""
import os
import sys
import time
import json
import logging
import threading
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import LOG_DIR, MT5_FILES_PATH

logger = logging.getLogger("multi_runner")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [multi_runner] %(message)s",
)

# Default MT5 Common Files path (same as LiveStateWriter)
_DEFAULT_MT5_FILES = MT5_FILES_PATH

SCAN_INTERVAL = 5  # seconds between scans


# ---------------------------------------------------------------------------
# Per-symbol worker thread
# ---------------------------------------------------------------------------

class SymbolWorker(threading.Thread):
    """Runs run_live_loop for one symbol in a daemon thread.

    If no trained model exists for the symbol, auto-trains one first:
      1. Try to fetch real MT5 OHLC data (50 000 bars M1)
      2. Fall back to synthetic GBM data if MT5 returns nothing
      3. Train PPO (200 000 timesteps, ~3 min on CPU)
      4. Start live loop
    """

    # Class-level registry so multi_runner + dashboard can read training status
    _status: dict = {}   # symbol → "waiting" | "training" | "live" | "error"
    _status_lock = threading.Lock()

    # Shared ZMQ PUB socket — one bind() for all symbols
    _zmq_ctx = None
    _zmq_socket = None
    _zmq_send_lock = threading.Lock()   # thread-safe sends on shared socket
    _zmq_init_lock = threading.Lock()   # guards one-time socket creation

    AUTO_TRAIN_BARS       = 50_000
    AUTO_TRAIN_TIMESTEPS  = 200_000

    @classmethod
    def _get_shared_socket(cls):
        """Lazy-init one ZMQ PUB socket shared by all workers."""
        with cls._zmq_init_lock:
            if cls._zmq_socket is None:
                import zmq
                from config import ZMQ_SIGNAL_ADDR
                cls._zmq_ctx = zmq.Context()
                cls._zmq_socket = cls._zmq_ctx.socket(zmq.PUB)
                cls._zmq_socket.setsockopt(zmq.SNDHWM, 100)
                cls._zmq_socket.setsockopt(zmq.LINGER, 0)
                cls._zmq_socket.bind(ZMQ_SIGNAL_ADDR)
                logger.info(f"Shared ZMQ PUB socket bound to {ZMQ_SIGNAL_ADDR}")
        return cls._zmq_socket

    def __init__(self, symbol: str):
        super().__init__(name=f"worker-{symbol}", daemon=True)
        self.symbol = symbol
        self._stop_event = threading.Event()
        self._set_status("waiting")

    def _set_status(self, status: str):
        with self.__class__._status_lock:
            if self.__class__._status.get(self.symbol) == status:
                return  # no change — skip JSON write
            self.__class__._status[self.symbol] = status
            payload = dict(self.__class__._status)  # snapshot under single lock
        logger.info(f"[{self.symbol}] status → {status}")
        # Write to LOG_DIR so Streamlit dashboard can read it
        try:
            status_path = LOG_DIR / "worker_status.json"
            tmp = status_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(status_path)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _auto_train(self, mt5_already_init: bool = False) -> object:
        """Fetch data and train PPO. Returns trained model.

        mt5_already_init=True: skip initialize/shutdown (caller owns the session).
        """
        import MetaTrader5 as mt5
        from config import MODEL_DIR
        from data.pipeline import load_or_fetch, generate_synthetic_data
        from ai_models.rl_agent import train_ppo

        self._set_status("training")
        df = None

        # Fetch real MT5 data — reuse caller's session if possible (BUG-17)
        init_here = not mt5_already_init and mt5.initialize()
        if mt5_already_init or init_here:
            try:
                logger.info(f"[{self.symbol}] Fetching {self.AUTO_TRAIN_BARS} bars from MT5...")
                df = load_or_fetch(          # BUG-18: no force_refresh — use parquet cache when fresh
                    symbol=self.symbol,
                    timeframe="M1",
                    num_bars=self.AUTO_TRAIN_BARS,
                )
                logger.info(f"[{self.symbol}] Fetched {len(df)} bars from MT5")
            except Exception as e:
                logger.warning(f"[{self.symbol}] MT5 data fetch failed: {e} — using synthetic")
            finally:
                if init_here:               # only shutdown if we initiated
                    mt5.shutdown()

        if df is None or len(df) < 500:
            logger.info(f"[{self.symbol}] Generating {self.AUTO_TRAIN_BARS} synthetic bars")
            df = generate_synthetic_data(n_bars=self.AUTO_TRAIN_BARS)

        logger.info(
            f"[{self.symbol}] Training PPO on {len(df)} bars, "
            f"{self.AUTO_TRAIN_TIMESTEPS} timesteps..."
        )
        model = train_ppo(df, total_timesteps=self.AUTO_TRAIN_TIMESTEPS, symbol=self.symbol)
        logger.info(f"[{self.symbol}] Auto-train complete → {MODEL_DIR}/ppo_{self.symbol.lower()}.zip")
        return model

    # ------------------------------------------------------------------
    def run(self):
        try:
            import MetaTrader5 as mt5
            from data.pipeline import LiveTickStream
            from ai_models.rl_agent import load_ppo
            from mt5_bridge.signal_server import run_live_loop

            # Initialize MT5 once for both training and live (BUG-17)
            mt5_ok = mt5.initialize()

            # Load or auto-train model
            try:
                model = load_ppo(symbol=self.symbol)
                logger.info(f"[{self.symbol}] Model loaded")
            except Exception:
                logger.warning(f"[{self.symbol}] No model found — starting auto-train")
                try:
                    model = self._auto_train(mt5_already_init=mt5_ok)
                except Exception as e:
                    logger.error(f"[{self.symbol}] Auto-train failed: {e}")
                    self._set_status("error")
                    return

            if self._stop_event.is_set():
                return  # chart was closed while training

            if not mt5_ok:
                logger.error(f"[{self.symbol}] MT5 init failed: {mt5.last_error()}")
                self._set_status("error")
                return

            def account_info():
                info = mt5.account_info()
                if info is None:
                    return 0.0, 0.0
                return info.equity, info.balance

            tick_stream = _StoppableTickStream(
                symbol=self.symbol,
                window=200,
                stop_event=self._stop_event,
            )

            self._set_status("live")
            run_live_loop(
                model, None, tick_stream, account_info,
                symbol=self.symbol,
                zmq_socket=self.__class__._get_shared_socket(),
                zmq_lock=self.__class__._zmq_send_lock,
            )
            mt5.shutdown()

        except Exception as e:
            logger.exception(f"[{self.symbol}] Worker crashed: {e}")
            self._set_status("error")
        finally:
            if self.__class__._status.get(self.symbol) == "live":
                self._set_status("waiting")
            logger.info(f"[{self.symbol}] Signal loop stopped")

    def stop(self):
        self._stop_event.set()


# ---------------------------------------------------------------------------
# Stoppable tick stream wrapper
# ---------------------------------------------------------------------------

class _StoppableTickStream:
    """Wraps LiveTickStream, checks stop_event between ticks."""

    def __init__(self, symbol: str, window: int, stop_event: threading.Event):
        from data.pipeline import LiveTickStream
        self._inner = LiveTickStream(symbol=symbol, window=window)
        self._stop = stop_event

    def __iter__(self):
        return self

    def __next__(self):
        if self._stop.is_set():
            raise StopIteration
        result = next(self._inner)      # BUG-15: was next(iter(self._inner))
        # Block up to 1s but wake immediately on stop (BUG-16: was 10× sleep(0.1))
        if self._stop.wait(timeout=1.0):
            raise StopIteration
        return result


# ---------------------------------------------------------------------------
# Symbol file scanner
# ---------------------------------------------------------------------------

def scan_active_symbols(mt5_files_path: Path = _DEFAULT_MT5_FILES) -> set[str]:
    """Return set of symbols whose ats_chart_{SYMBOL}.txt contains '1'.

    MQL5 FileWriteString uses UTF-16 LE with BOM — try both encodings.
    """
    active = set()
    try:
        for f in mt5_files_path.glob("ats_chart_*.txt"):
            try:
                # MQL5 writes UTF-16 LE; fall back to UTF-8/Latin-1
                for enc in ("utf-16", "utf-8", "latin-1"):
                    try:
                        content = f.read_text(encoding=enc).strip()
                        break
                    except (UnicodeDecodeError, UnicodeError):
                        content = ""
                if content == "1":
                    sym = f.stem.replace("ats_chart_", "").upper()
                    active.add(sym)
            except OSError:
                pass
    except OSError:
        pass
    return active


# ---------------------------------------------------------------------------
# Main orchestrator loop
# ---------------------------------------------------------------------------

def run_multi_live(mt5_files_path: Path = _DEFAULT_MT5_FILES):
    """Watch for open charts and manage per-symbol worker threads."""
    workers: dict[str, SymbolWorker] = {}

    logger.info(
        f"Multi-runner started. Scanning {mt5_files_path} every {SCAN_INTERVAL}s"
    )
    logger.info("Open a chart in MT5 and attach ATS_Panel — it will appear here automatically.")

    try:
        while True:
            active_syms = scan_active_symbols(mt5_files_path)

            # Start workers for newly active symbols
            for sym in active_syms:
                if sym not in workers or not workers[sym].is_alive():
                    logger.info(f"New chart detected: {sym} — starting worker")
                    w = SymbolWorker(sym)
                    workers[sym] = w
                    w.start()

            # Stop workers for closed charts
            for sym in list(workers.keys()):
                if sym not in active_syms and workers[sym].is_alive():
                    logger.info(f"Chart closed: {sym} — stopping worker")
                    workers[sym].stop()

            # Log current status
            alive = [s for s, w in workers.items() if w.is_alive()]
            if alive:
                logger.info(f"Active symbols: {', '.join(sorted(alive))}")
            else:
                logger.info("No active charts detected yet — waiting...")

            time.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Shutting down multi-runner...")
        for sym, w in workers.items():
            w.stop()
        for sym, w in workers.items():
            w.join(timeout=5)
        logger.info("All workers stopped.")


if __name__ == "__main__":
    run_multi_live()
