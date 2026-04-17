"""PPO/SAC Reinforcement Learning agent — multi-symbol intraday trading.

Gym environment with Ornstein-Uhlenbeck z-score signal,
Sharpe-adjusted reward, cost penalties, and hard EOD liquidation.
Works with any symbol: XAUUSD, EURUSD, GBPUSD, USDJPY, BTCUSD, NAS100, etc.

Supported algorithms:
  - PPO  (on-policy,  discrete actions 0-3)
  - SAC  (off-policy, continuous action mapped to discrete via threshold)
"""
import numpy as np
import gymnasium as gym
from gymnasium import spaces
from stable_baselines3 import PPO, SAC
from stable_baselines3.common.vec_env import DummyVecEnv
from pathlib import Path

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import (
    SPREAD_BPS, EOD_HOUR_GMT, MAX_HOLD_BARS,
    RL_LEARNING_RATE, RL_N_STEPS, RL_BATCH_SIZE,
    RL_TOTAL_TIMESTEPS, FEATURE_DIM, OBS_DIM, MODEL_DIR,
)
from ai_models.features import build_feature_matrix


# ---------------------------------------------------------------------------
# Slippage model (⑫)
# ---------------------------------------------------------------------------

def calc_execution_price(
    mid: float,
    side: int,
    lot: float = 0.01,
    spread_bps: float = SPREAD_BPS,
    symbol: str = "XAUUSD",
) -> float:
    """Realistic execution price: mid ± (half-spread + market impact).

    Components:
      half_spread  = 0.5 × spread_bps × mid           — passive limit fill pays half-spread
      market_impact = impact_bps × mid × √(lot/0.01)  — square-root impact model

    Impact coefficients (bps per √(lot/0.01)):
      XAUUSD  0.10 bps  (liquid, 100-oz contract)
      EURUSD  0.05 bps  (most liquid forex pair)
      default 0.08 bps

    Args:
        mid:        Current mid price
        side:       +1 = buy, -1 = sell
        lot:        Order size in lots
        spread_bps: Typical bid-ask spread in basis points
        symbol:     Instrument name for impact coefficient lookup

    Returns:
        Simulated fill price (worse than mid by slippage amount).
    """
    _IMPACT_BPS = {
        "XAUUSD": 0.10,
        "EURUSD": 0.05,
        "GBPUSD": 0.06,
        "USDJPY": 0.05,
        "BTCUSD": 0.30,
        "NAS100": 0.15,
    }
    impact_bps = _IMPACT_BPS.get(symbol.upper(), 0.08)

    half_spread   = 0.5 * spread_bps * 1e-4 * mid
    market_impact = impact_bps * 1e-4 * mid * (max(lot, 0.01) / 0.01) ** 0.5

    return mid + side * (half_spread + market_impact)


class ATSIntradayEnv(gym.Env):
    """Intraday trading environment — works with any symbol.

    Actions: 0=hold, 1=long_limit, 2=short_limit, 3=close
    Observation: 24-dim feature vector (z-score, ATR, regime, etc.)
    Reward: Sharpe-adjusted PnL minus transaction costs.
    """
    metadata = {"render_modes": []}

    # ATR trailing stop multiplier (0 = disabled)
    ATR_TRAIL_MULT: float = 1.5

    # Feature column index for ATR (see features.py build_feature_matrix docstring)
    _ATR_FEAT_IDX: int = 1

    def __init__(self, tick_df, regime_model=None, max_hold_bars=MAX_HOLD_BARS,
                 atr_trail_mult: float | None = None, symbol: str = "XAUUSD"):
        super().__init__()
        self.df = tick_df.reset_index(drop=True)
        self.regime_model = regime_model
        self.max_hold = max_hold_bars
        self.cost_bps = SPREAD_BPS
        self.symbol = symbol
        self.atr_trail_mult = atr_trail_mult if atr_trail_mult is not None else self.ATR_TRAIL_MULT

        self.action_space = spaces.Discrete(4)
        # OBS_DIM = FEATURE_DIM(24) + POSITION_STATE_DIM(3)
        # Extra dims: [position(-1/0/1), unrealized_pnl_pct, bars_in_trade_norm]
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(OBS_DIM,), dtype=np.float32
        )

        # Precompute features
        self.features = build_feature_matrix(tick_df).values.astype(np.float32)

        # Episode state
        self.start_idx = 50  # need lookback
        self.i = self.start_idx
        self.position = 0  # -1, 0, 1
        self.entry_price = 0.0
        self._prev_mark = 0.0  # price used as base for incremental step PnL
        self.hold_bars = 0
        self.returns = []
        self.trade_count = 0

        # ATR trailing stop state
        self.trail_level = 0.0   # current trail price; 0 = inactive

        # MAE tracking (Maximum Adverse Excursion)
        self._mae_worst: float = 0.0  # worst adverse price move since entry

    def _build_obs(self, idx: int) -> np.ndarray:
        """Concatenate market features with live position state.

        Extra dims appended to the 24-dim price vector:
          [0] position        : -1 / 0 / +1
          [1] unrealized_pnl  : (mid - entry) / atr, clipped ±5
          [2] bars_in_trade   : hold_bars / MAX_HOLD_BARS, clipped 0-1
        Model learns to CLOSE when upnl deteriorates and to hold winners.
        """
        price_obs = self.features[min(idx, len(self.features) - 1)]
        price_obs = np.nan_to_num(price_obs, nan=0.0, posinf=0.0, neginf=0.0)

        mid = self._get_mid(min(idx, len(self.df) - 1))
        atr = self._get_atr(min(idx, len(self.df) - 1))

        if self.position != 0 and self.entry_price > 0 and atr > 0:
            upnl_pct = float(np.clip(
                self.position * (mid - self.entry_price) / atr, -5.0, 5.0))
        else:
            upnl_pct = 0.0

        bars_norm = float(np.clip(self.hold_bars / max(MAX_HOLD_BARS, 1), 0.0, 1.0))

        pos_state = np.array([float(self.position), upnl_pct, bars_norm],
                             dtype=np.float32)
        return np.concatenate([price_obs, pos_state])

    def _get_mid(self, idx):
        row = self.df.iloc[idx]
        if "mid" in self.df.columns:
            return row["mid"]
        return row["close"]

    def _is_eod(self):
        if "datetime" in self.df.columns:
            dt = self.df.iloc[self.i]["datetime"]
            if hasattr(dt, "hour"):
                return dt.hour >= EOD_HOUR_GMT
        return False

    def _done(self):
        return self.i >= len(self.df) - 1

    def _get_atr(self, idx: int) -> float:
        """Read precomputed ATR from feature matrix (column 1, no look-ahead).

        Fallback: 0.1% of current mid price — symbol-agnostic approximation
        (e.g. XAUUSD ~3200 → ~3.2, EURUSD ~1.10 → ~0.0011, BTCUSD ~90k → ~90).
        """
        feat_idx = min(idx, len(self.features) - 1)
        atr = float(self.features[feat_idx, self._ATR_FEAT_IDX])
        if atr > 0:
            return atr
        mid = self._get_mid(idx)
        return max(mid * 0.001, 1e-6)

    def _update_trail(self, mid: float, atr: float) -> bool:
        """Update ATR trailing stop level. Returns True if trail hit (force close).

        Long:  trail = max(trail_prev, mid - mult * ATR)
               Hit when mid < trail
        Short: trail = min(trail_prev, mid + mult * ATR)
               Hit when mid > trail
        """
        if self.position == 0 or self.atr_trail_mult <= 0:
            return False

        offset = self.atr_trail_mult * atr

        if self.position == 1:   # long
            candidate = mid - offset
            if self.trail_level == 0.0:
                self.trail_level = candidate        # initialise on first bar
            else:
                self.trail_level = max(self.trail_level, candidate)  # ratchet up
            return mid < self.trail_level           # hit if price fell below trail

        else:                    # short
            candidate = mid + offset
            if self.trail_level == 0.0:
                self.trail_level = candidate
            else:
                self.trail_level = min(self.trail_level, candidate)  # ratchet down
            return mid > self.trail_level           # hit if price rose above trail

    def _compute_pnl(self, action: int) -> float:
        mid = self._get_mid(self.i)
        prev_mid = self._get_mid(self.i - 1)
        atr = self._get_atr(self.i)
        pnl = 0.0
        cost = 0.0

        # ATR trailing stop check — overrides action to close if hit
        if self.position != 0 and action != 3:
            if self._update_trail(mid, atr):
                action = 3  # trailing stop triggered → force close

        if action == 3 and self.position != 0:
            # Close position — incremental step: from last mark to exit fill.
            # entry_price (fill) is NOT used here because hold-bar PnL has
            # already accumulated the price move from entry to prev bar.
            exit_price = calc_execution_price(mid, -self.position,
                                              spread_bps=self.cost_bps,
                                              symbol=self.symbol)
            pnl = self.position * (exit_price - self._prev_mark)
            cost = 0.0   # cost already baked into exit_price
            self.position = 0
            self.entry_price = 0.0
            self._prev_mark = 0.0
            self.hold_bars = 0
            self.trail_level = 0.0
            self._mae_worst = 0.0
            self.trade_count += 1

        elif action == 1 and self.position == 0:
            # Open long — use realistic fill price (⑫).
            # Set _prev_mark = fill so the first hold bar marks from fill,
            # capturing entry slippage as an immediate unrealised loss.
            fill = calc_execution_price(mid, 1, spread_bps=self.cost_bps,
                                        symbol=self.symbol)
            self.position = 1
            self.entry_price = fill      # kept for MAE / adverse-excursion
            self._prev_mark = fill       # incremental PnL baseline
            self.trail_level = fill - self.atr_trail_mult * atr
            self._mae_worst = 0.0
            cost = 0.0   # baked into fill price
            self.trade_count += 1

        elif action == 2 and self.position == 0:
            # Open short — same pattern as long.
            fill = calc_execution_price(mid, -1, spread_bps=self.cost_bps,
                                        symbol=self.symbol)
            self.position = -1
            self.entry_price = fill
            self._prev_mark = fill
            self.trail_level = fill + self.atr_trail_mult * atr
            self._mae_worst = 0.0
            cost = 0.0   # baked into fill price
            self.trade_count += 1

        elif self.position != 0:
            # Mark-to-market (still open): incremental move from last mark.
            self._update_trail(mid, atr)
            pnl = self.position * (mid - self._prev_mark)
            self._prev_mark = mid
            # Track worst adverse excursion since entry (uses fill as base)
            adverse = self.position * (self.entry_price - mid)
            if adverse > self._mae_worst:
                self._mae_worst = adverse

        return pnl - cost

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        self.i = self.start_idx
        self.position = 0
        self.entry_price = 0.0
        self._prev_mark = 0.0
        self.hold_bars = 0
        self.returns = []
        self.trade_count = 0
        self.trail_level = 0.0
        self._mae_worst = 0.0
        return self._build_obs(self.i), {}

    def step(self, action: int):
        # Force close at EOD
        if self._is_eod() and self.position != 0:
            action = 3

        # Force close if held too long
        if self.position != 0:
            self.hold_bars += 1
            if self.hold_bars >= self.max_hold:
                action = 3

        pnl = self._compute_pnl(action)
        self.returns.append(pnl)

        # ── Sharpe-adjusted reward (⑥) ────────────────────────────────────
        # Use a shorter rolling window (50 bars) for more responsive shaping.
        window = self.returns[-50:]
        mean_r = float(np.mean(window))
        std_r  = float(np.std(window)) + 1e-9
        sharpe_r = mean_r / std_r

        # Trade penalty — discourages overtrading
        trade_penalty = 0.001 if action in (1, 2) else 0.0

        # MAE penalty — penalises holding through large adverse excursions.
        # Only applies when in a position; scaled by ATR so it's instrument-agnostic.
        atr_now = self._get_atr(self.i)
        if self.position != 0 and atr_now > 0 and self._mae_worst > 0:
            # Penalty grows linearly once MAE > 0.5 ATR; capped at 1.5× ATR
            mae_atr = min(self._mae_worst / atr_now, 1.5)
            mae_penalty = max(0.0, mae_atr - 0.5) * 0.001
        else:
            mae_penalty = 0.0

        reward = sharpe_r - trade_penalty - mae_penalty

        self.i += 1
        done = self._done()
        truncated = False

        return self._build_obs(self.i), float(reward), done, truncated, {
            "pnl": pnl,
            "position": self.position,
            "trade_count": self.trade_count,
        }


def make_env(tick_df, regime_model=None, symbol: str = "XAUUSD"):
    """Factory for vectorized env (discrete actions — PPO)."""
    def _init():
        return ATSIntradayEnv(tick_df, regime_model, symbol=symbol)
    return _init


# ---------------------------------------------------------------------------
# SAC Continuous-Action Wrapper
# ---------------------------------------------------------------------------
class ATSIntradaySACEnv(ATSIntradayEnv):
    """Continuous-action wrapper for SAC.

    SAC outputs a 1-dim continuous action in [-1, 1]:
      action < -0.33   → short_limit  (mapped to discrete 2)
      -0.33 ≤ action ≤ 0.33 → hold   (mapped to discrete 0)
      action > 0.33    → long_limit   (mapped to discrete 1)
      |action| > 0.9   → close        (mapped to discrete 3)

    This lets SAC learn a smooth policy while sharing the same
    step/reward logic as the PPO environment.
    """

    # Action thresholds
    _LONG_THRESH  =  0.33
    _SHORT_THRESH = -0.33
    _CLOSE_THRESH =  0.90   # |action| above this = close

    def __init__(self, tick_df, regime_model=None, max_hold_bars=MAX_HOLD_BARS,
                 symbol: str = "XAUUSD"):
        super().__init__(tick_df, regime_model, max_hold_bars, symbol=symbol)
        # Override: 1-dim continuous Box action
        self.action_space = spaces.Box(
            low=-1.0, high=1.0, shape=(1,), dtype=np.float32
        )

    def _map_action(self, continuous_action: np.ndarray) -> int:
        a = float(continuous_action[0])
        if abs(a) > self._CLOSE_THRESH:
            return 3  # close
        if a > self._LONG_THRESH:
            return 1  # long
        if a < self._SHORT_THRESH:
            return 2  # short
        return 0      # hold

    def step(self, action):
        discrete = self._map_action(np.asarray(action))
        return super().step(discrete)


def make_sac_env(tick_df, regime_model=None, symbol: str = "XAUUSD"):
    """Factory for SAC continuous env."""
    def _init():
        return ATSIntradaySACEnv(tick_df, regime_model, symbol=symbol)
    return _init


# Backward-compat aliases (existing code using XauIntradayEnv still works)
XauIntradayEnv    = ATSIntradayEnv
XauIntradaySACEnv = ATSIntradaySACEnv


# ---------------------------------------------------------------------------
# PPO train / load
# ---------------------------------------------------------------------------
def train_ppo(
    tick_df,
    regime_model=None,
    total_timesteps: int = RL_TOTAL_TIMESTEPS,
    save_path: Path | None = None,
    symbol: str = "XAUUSD",
) -> PPO:
    """Train PPO agent. Model saved as ppo_{symbol_lower} in MODEL_DIR."""
    env = DummyVecEnv([make_env(tick_df, regime_model, symbol=symbol)])
    try:
        import tensorboard  # noqa: F401
        tb_log = str(MODEL_DIR.parent.parent / "logs" / f"tb_{symbol.lower()}")
    except ImportError:
        tb_log = None
    model = PPO(
        "MlpPolicy",
        env,
        learning_rate=RL_LEARNING_RATE,
        n_steps=RL_N_STEPS,
        batch_size=RL_BATCH_SIZE,
        verbose=1,
        tensorboard_log=tb_log,
    )
    model.learn(total_timesteps=total_timesteps)

    if save_path is None:
        save_path = MODEL_DIR / f"ppo_{symbol.lower()}"
    model.save(str(save_path))
    print(f"PPO [{symbol}] saved to {save_path}")
    return model


def load_ppo(path: Path | None = None, symbol: str = "XAUUSD") -> PPO:
    """Load PPO model for a given symbol."""
    if path is None:
        path = MODEL_DIR / f"ppo_{symbol.lower()}"
    return PPO.load(str(path))


# ---------------------------------------------------------------------------
# SAC train / load
# ---------------------------------------------------------------------------
def train_sac(
    tick_df,
    regime_model=None,
    total_timesteps: int = RL_TOTAL_TIMESTEPS,
    save_path: Path | None = None,
    symbol: str = "XAUUSD",
) -> SAC:
    """Train SAC agent. Model saved as sac_{symbol_lower} in MODEL_DIR.

    SAC is off-policy (replay buffer) — more sample-efficient than PPO
    but requires continuous action space (see XauIntradaySACEnv).
    """
    env = DummyVecEnv([make_sac_env(tick_df, regime_model, symbol=symbol)])
    try:
        import tensorboard  # noqa: F401
        tb_log = str(MODEL_DIR.parent.parent / "logs" / f"tb_sac_{symbol.lower()}")
    except ImportError:
        tb_log = None
    model = SAC(
        "MlpPolicy",
        env,
        learning_rate=RL_LEARNING_RATE,
        batch_size=RL_BATCH_SIZE,
        buffer_size=100_000,
        learning_starts=1_000,
        train_freq=1,
        gradient_steps=1,
        ent_coef="auto",
        verbose=1,
        tensorboard_log=tb_log,
    )
    model.learn(total_timesteps=total_timesteps)

    if save_path is None:
        save_path = MODEL_DIR / f"sac_{symbol.lower()}"
    model.save(str(save_path))
    print(f"SAC [{symbol}] saved to {save_path}")
    return model


def load_sac(path: Path | None = None, symbol: str = "XAUUSD") -> SAC:
    """Load SAC model for a given symbol."""
    if path is None:
        path = MODEL_DIR / f"sac_{symbol.lower()}"
    return SAC.load(str(path))
