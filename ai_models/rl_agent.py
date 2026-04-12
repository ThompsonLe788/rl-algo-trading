"""PPO/SAC Reinforcement Learning agent for XAU/USD intraday trading.

Gym environment with Ornstein-Uhlenbeck z-score signal,
Sharpe-adjusted reward, cost penalties, and hard EOD liquidation.

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
    RL_TOTAL_TIMESTEPS, FEATURE_DIM, MODEL_DIR,
)
from ai_models.features import build_feature_matrix


class XauIntradayEnv(gym.Env):
    """Intraday XAU/USD trading environment.

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
                 atr_trail_mult: float | None = None):
        super().__init__()
        self.df = tick_df.reset_index(drop=True)
        self.regime_model = regime_model
        self.max_hold = max_hold_bars
        self.cost_bps = SPREAD_BPS
        self.atr_trail_mult = atr_trail_mult if atr_trail_mult is not None else self.ATR_TRAIL_MULT

        self.action_space = spaces.Discrete(4)
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(FEATURE_DIM,), dtype=np.float32
        )

        # Precompute features
        self.features = build_feature_matrix(tick_df).values.astype(np.float32)

        # Episode state
        self.start_idx = 50  # need lookback
        self.i = self.start_idx
        self.position = 0  # -1, 0, 1
        self.entry_price = 0.0
        self.hold_bars = 0
        self.returns = []
        self.trade_count = 0

        # ATR trailing stop state
        self.trail_level = 0.0   # current trail price; 0 = inactive

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
        """Read precomputed ATR from feature matrix (column 1, no look-ahead)."""
        feat_idx = min(idx, len(self.features) - 1)
        atr = float(self.features[feat_idx, self._ATR_FEAT_IDX])
        return atr if atr > 0 else 1.0  # fallback: 1 pip

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
            # Close position (voluntary, EOD, max_hold, or trailing stop)
            pnl = self.position * (mid - self.entry_price)
            cost = self.cost_bps * 1e-4 * mid
            self.position = 0
            self.entry_price = 0.0
            self.hold_bars = 0
            self.trail_level = 0.0
            self.trade_count += 1

        elif action == 1 and self.position == 0:
            # Open long — initialise trail
            self.position = 1
            self.entry_price = mid
            self.trail_level = mid - self.atr_trail_mult * atr
            cost = self.cost_bps * 1e-4 * mid
            self.trade_count += 1

        elif action == 2 and self.position == 0:
            # Open short — initialise trail
            self.position = -1
            self.entry_price = mid
            self.trail_level = mid + self.atr_trail_mult * atr
            cost = self.cost_bps * 1e-4 * mid
            self.trade_count += 1

        elif self.position != 0:
            # Mark-to-market (still open); update trail but don't close here
            self._update_trail(mid, atr)
            pnl = self.position * (mid - prev_mid)

        return pnl - cost

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        self.i = self.start_idx
        self.position = 0
        self.entry_price = 0.0
        self.hold_bars = 0
        self.returns = []
        self.trade_count = 0
        self.trail_level = 0.0
        obs = self.features[self.i]
        obs = np.nan_to_num(obs, nan=0.0, posinf=0.0, neginf=0.0)
        return obs, {}

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

        # Sharpe-adjusted reward
        window = self.returns[-100:]
        mean_r = np.mean(window)
        std_r = np.std(window) + 1e-9
        reward = mean_r / std_r

        # Penalty for overtrading
        trade_penalty = 0.001 if action in (1, 2) else 0.0
        reward -= trade_penalty

        self.i += 1
        done = self._done()
        truncated = False

        obs = self.features[min(self.i, len(self.features) - 1)]
        obs = np.nan_to_num(obs, nan=0.0, posinf=0.0, neginf=0.0)

        return obs, float(reward), done, truncated, {
            "pnl": pnl,
            "position": self.position,
            "trade_count": self.trade_count,
        }


def make_env(tick_df, regime_model=None):
    """Factory for vectorized env (discrete actions — PPO)."""
    def _init():
        return XauIntradayEnv(tick_df, regime_model)
    return _init


# ---------------------------------------------------------------------------
# SAC Continuous-Action Wrapper
# ---------------------------------------------------------------------------
class XauIntradaySACEnv(XauIntradayEnv):
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

    def __init__(self, tick_df, regime_model=None, max_hold_bars=MAX_HOLD_BARS):
        super().__init__(tick_df, regime_model, max_hold_bars)
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


def make_sac_env(tick_df, regime_model=None):
    """Factory for SAC continuous env."""
    def _init():
        return XauIntradaySACEnv(tick_df, regime_model)
    return _init


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
    env = DummyVecEnv([make_env(tick_df, regime_model)])
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
    env = DummyVecEnv([make_sac_env(tick_df, regime_model)])
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
