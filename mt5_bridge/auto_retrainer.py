"""
auto_retrainer.py — Hot-swappable model reference + background AutoRetrainer.

Two classes:

  ModelRef      Thread-safe mutable wrapper around a trained model.
                SymbolWorker passes this to run_live_loop() instead of the
                raw model; AutoRetrainer calls .swap() to replace it while
                live trading continues uninterrupted.

  AutoRetrainer Background daemon thread that fires a retrain when:
                1. Weekly schedule  (every Monday, once per ISO week)
                2. Drift detected   (win_rate < DRIFT_WIN_RATE_THRESHOLD for
                                     >= RETRAIN_MIN_TRADES consecutive trades)

                After training, the new model is evaluated against the current
                one on RETRAIN_EVAL_BARS recent bars (3 random-seed episodes).
                The new model is accepted only if:
                    new_sharpe >= current_sharpe * RETRAIN_MODEL_ACCEPT_RATIO
                Old model is archived with a timestamp before being replaced.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np

from config import (
    DRIFT_WIN_RATE_THRESHOLD,
    MODEL_DIR,
    RETRAIN_BARS,
    RETRAIN_CHECK_INTERVAL,
    RETRAIN_EVAL_BARS,
    RETRAIN_MODEL_ACCEPT_RATIO,
    RETRAIN_TIMESTEPS,
    RETRAIN_WEEKLY_DAY,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# ModelRef
# ──────────────────────────────────────────────────────────────────────────────

class ModelRef:
    """Thread-safe swappable reference to the live model.

    Exposes the same .predict() interface as a raw stable-baselines3 model so
    it can be passed directly to run_live_loop() as the ``model`` argument.
    """

    def __init__(self, model):
        self._model = model
        self._lock = threading.RLock()

    # Drop-in replacement for model.predict() used inside run_live_loop
    def predict(self, obs, deterministic: bool = True):
        with self._lock:
            return self._model.predict(obs, deterministic=deterministic)

    def get(self):
        with self._lock:
            return self._model

    def swap(self, new_model):
        """Replace the live model; returns the old one (caller may del it)."""
        with self._lock:
            old = self._model
            self._model = new_model
            return old


# ──────────────────────────────────────────────────────────────────────────────
# AutoRetrainer
# ──────────────────────────────────────────────────────────────────────────────

class AutoRetrainer:
    """Daemon thread that monitors performance and retrains the model when
    drift is detected or the weekly schedule fires.

    Args:
        symbol:         Trading instrument (e.g. "XAUUSD").
        model_ref:      ModelRef wrapping the current live model.
        perf_monitor:   PerformanceMonitor tracking closed-trade P&L.
        stop_event:     Event shared with SymbolWorker; set on chart close.
        set_status_fn:  Callable(str) → updates worker_status.json display.
    """

    # Minimum hours between two drift-triggered retrains (prevents thrashing)
    _DRIFT_COOLDOWN_HOURS = 24

    def __init__(
        self,
        symbol: str,
        model_ref: ModelRef,
        perf_monitor,
        stop_event: threading.Event,
        set_status_fn: Callable[[str], None] | None = None,
    ):
        self.symbol = symbol
        self.model_ref = model_ref
        self.perf_monitor = perf_monitor
        self._stop_event = stop_event
        self._set_status = set_status_fn or (lambda s: None)

        self._training_lock = threading.Lock()   # prevent concurrent retrains
        self._last_drift_retrain: datetime | None = None
        self._thread: threading.Thread | None = None
        MODEL_DIR.mkdir(parents=True, exist_ok=True)

    # ── public ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop,
            name=f"AutoRetrain-{self.symbol}",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"[{self.symbol}] AutoRetrainer started (check every {RETRAIN_CHECK_INTERVAL}s)")

    # ── main loop ────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=RETRAIN_CHECK_INTERVAL)
            if self._stop_event.is_set():
                break
            reason = self._should_retrain()
            if reason:
                t = threading.Thread(
                    target=self._retrain_and_maybe_swap,
                    args=(reason,),
                    name=f"Retrain-{self.symbol}",
                    daemon=True,
                )
                t.start()

    # ── trigger logic ────────────────────────────────────────────────────────

    def _should_retrain(self) -> str | None:
        now = datetime.now(timezone.utc)

        # 1. Weekly scheduled retrain (first check of this ISO week, any hour Monday)
        if now.weekday() == RETRAIN_WEEKLY_DAY:
            iso_year, iso_week, _ = now.isocalendar()
            flag = MODEL_DIR / f".retrain_w{iso_year}w{iso_week:02d}_{self.symbol.lower()}"
            if not flag.exists():
                flag.touch()
                return f"weekly_schedule (ISO {iso_year}-W{iso_week:02d})"

        # 2. Drift detection — only if enough data and cooldown elapsed
        if self.perf_monitor.is_drifting():
            if self._last_drift_retrain is None or (
                (now - self._last_drift_retrain).total_seconds()
                > self._DRIFT_COOLDOWN_HOURS * 3600
            ):
                self._last_drift_retrain = now
                wr = self.perf_monitor.win_rate()
                return f"drift_detected (win_rate={wr:.1%} < {DRIFT_WIN_RATE_THRESHOLD:.1%})"

        return None

    # ── core retrain logic ────────────────────────────────────────────────────

    def _retrain_and_maybe_swap(self, reason: str) -> None:
        if not self._training_lock.acquire(blocking=False):
            logger.info(f"[{self.symbol}] Retrain already running — skipping")
            return
        try:
            logger.info(f"[{self.symbol}] ── AutoRetrain START ── reason: {reason}")
            self._set_status("retraining")

            new_model = self._train_new()
            if new_model is None:
                logger.error(f"[{self.symbol}] Training failed — keeping current model")
                self._set_status("live")
                return

            logger.info(f"[{self.symbol}] Evaluating models on {RETRAIN_EVAL_BARS} bars...")
            old_sharpe = self._evaluate(self.model_ref.get())
            new_sharpe = self._evaluate(new_model)
            threshold  = old_sharpe * RETRAIN_MODEL_ACCEPT_RATIO

            logger.info(
                f"[{self.symbol}] old_sharpe={old_sharpe:.4f}  "
                f"new_sharpe={new_sharpe:.4f}  "
                f"accept_threshold={threshold:.4f}"
            )

            if new_sharpe >= threshold:
                self._backup_and_deploy(new_model)
                self.model_ref.swap(new_model)
                self.perf_monitor.reset()   # reset drift counter for fresh window
                logger.info(
                    f"[{self.symbol}] ✓ New model ACCEPTED — "
                    f"Sharpe {old_sharpe:.4f} → {new_sharpe:.4f}"
                )
            else:
                # Cleanup candidate file
                candidate = MODEL_DIR / f"ppo_{self.symbol.lower()}_candidate.zip"
                candidate.unlink(missing_ok=True)
                logger.info(
                    f"[{self.symbol}] ✗ New model REJECTED — "
                    f"new={new_sharpe:.4f} < threshold={threshold:.4f}, "
                    f"keeping current"
                )
        except Exception:
            logger.exception(f"[{self.symbol}] AutoRetrain error")
        finally:
            self._set_status("live")
            self._training_lock.release()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _train_new(self):
        """Fetch fresh MT5 data (with force-refresh) and train a new PPO model.
        Saves to a _candidate path so the live model is untouched during training.
        Returns the trained model object, or None on failure.
        """
        try:
            import MetaTrader5 as mt5

            from ai_models.rl_agent import train_ppo
            from data.pipeline import generate_synthetic_data, load_or_fetch

            df = None
            if mt5.initialize():
                try:
                    # Try force_refresh kwarg first; fall back if not supported
                    try:
                        df = load_or_fetch(
                            symbol=self.symbol,
                            timeframe="M1",
                            num_bars=RETRAIN_BARS,
                            force_refresh=True,
                        )
                    except TypeError:
                        df = load_or_fetch(
                            symbol=self.symbol,
                            timeframe="M1",
                            num_bars=RETRAIN_BARS,
                        )
                    logger.info(f"[{self.symbol}] Fetched {len(df)} fresh bars for retrain")
                except Exception as e:
                    logger.warning(f"[{self.symbol}] MT5 fetch failed: {e} — using synthetic")
                finally:
                    mt5.shutdown()

            if df is None or len(df) < 500:
                logger.info(f"[{self.symbol}] Using {RETRAIN_BARS} synthetic bars")
                df = generate_synthetic_data(n_bars=RETRAIN_BARS)

            candidate_path = MODEL_DIR / f"ppo_{self.symbol.lower()}_candidate"
            logger.info(
                f"[{self.symbol}] Training new model "
                f"({len(df)} bars, {RETRAIN_TIMESTEPS} steps) ..."
            )
            model = train_ppo(
                df,
                total_timesteps=RETRAIN_TIMESTEPS,
                symbol=self.symbol,
                save_path=candidate_path,
            )
            return model

        except Exception:
            logger.exception(f"[{self.symbol}] _train_new failed")
            return None

    def _evaluate(self, model) -> float:
        """Run model through RETRAIN_EVAL_BARS recent bars (3 episodes).
        Returns mean Sharpe ratio.  Returns 0.0 on any failure.
        """
        try:
            from ai_models.features import build_feature_matrix
            from ai_models.rl_agent import XauIntradayEnv
            from data.pipeline import generate_synthetic_data, load_or_fetch

            try:
                df = load_or_fetch(
                    symbol=self.symbol,
                    timeframe="M1",
                    num_bars=RETRAIN_EVAL_BARS,
                )
            except Exception:
                df = generate_synthetic_data(n_bars=RETRAIN_EVAL_BARS)

            features = build_feature_matrix(df)
            closes = (
                df["close"].values
                if "close" in df.columns
                else ((df["bid"] + df["ask"]) / 2).values
            )

            sharpes: list[float] = []
            for seed in (0, 1, 2):
                env = XauIntradayEnv(features, closes)
                obs, _ = env.reset(seed=seed)
                done = False
                rewards: list[float] = []
                while not done:
                    action, _ = model.predict(
                        np.array(obs, dtype=np.float32), deterministic=True
                    )
                    obs, reward, terminated, truncated, _ = env.step(int(action))
                    done = terminated or truncated
                    rewards.append(float(reward))
                if len(rewards) > 1:
                    arr = np.array(rewards)
                    s = float(arr.mean() / arr.std()) if arr.std() > 0 else 0.0
                    sharpes.append(s)

            return float(np.mean(sharpes)) if sharpes else 0.0

        except Exception as e:
            logger.warning(f"[{self.symbol}] Evaluation failed: {e} — returning 0.0")
            return 0.0

    def _backup_and_deploy(self, new_model) -> None:  # noqa: ARG002
        """Archive current live model file and promote candidate to live."""
        live = MODEL_DIR / f"ppo_{self.symbol.lower()}.zip"
        candidate = MODEL_DIR / f"ppo_{self.symbol.lower()}_candidate.zip"

        if live.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M")
            backup = MODEL_DIR / f"ppo_{self.symbol.lower()}_backup_{ts}.zip"
            live.rename(backup)
            logger.info(f"[{self.symbol}] Old model archived → {backup.name}")

        if candidate.exists():
            candidate.rename(live)
            logger.info(f"[{self.symbol}] Candidate promoted → {live.name}")
