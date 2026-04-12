"""Tests for the ATS system.

Verifies:
1. No look-ahead bias in features (features at t use only data ≤ t)
2. EOD liquidation closes 100% positions by 22:00 GMT in backtest
3. Kelly sizing respects caps
4. Kill switch triggers correctly
"""
import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timezone, timedelta

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def synthetic_df():
    """Generate synthetic OHLCV data with datetime index."""
    from data.pipeline import generate_synthetic_data
    return generate_synthetic_data(n_bars=5000, seed=42)


@pytest.fixture
def multiday_df():
    """3-day OHLCV data spanning full trading hours including 22:00 GMT EOD boundary."""
    rng = np.random.default_rng(99)
    n_bars = 1440 * 3  # 3 full days of 1-min bars
    start = datetime(2025, 3, 10, 0, 0, tzinfo=timezone.utc)
    idx = pd.date_range(start, periods=n_bars, freq="1min", tz="UTC")

    price = 2300.0
    prices = []
    for _ in range(n_bars):
        price += rng.normal(0, 0.3)
        price = max(price, 2000)
        prices.append(price)
    prices = np.array(prices)
    noise = rng.uniform(0.5, 2.0, n_bars)

    df = pd.DataFrame({
        "open": prices,
        "high": prices + noise,
        "low": prices - noise,
        "close": prices + rng.normal(0, 0.2, n_bars),
        "volume": rng.integers(100, 5000, n_bars),
    }, index=idx)
    df["mid"] = (df["high"] + df["low"]) / 2
    # rl_agent expects a 'datetime' column for EOD detection
    df["datetime"] = df.index
    return df


@pytest.fixture
def kelly_sizer():
    from risk.kelly import KellyPositionSizer
    sizer = KellyPositionSizer()
    # Seed with 200 trades: 55% win rate, avg win=3, avg loss=2
    rng = np.random.default_rng(42)
    for _ in range(200):
        if rng.random() < 0.55:
            sizer.record_trade(rng.uniform(1, 5))
        else:
            sizer.record_trade(-rng.uniform(0.5, 3.5))
    return sizer


@pytest.fixture
def kill_switch():
    from risk.kill_switch import KillSwitch
    ks = KillSwitch(max_drawdown_pct=15.0, daily_loss_limit_pct=5.0)
    ks.set_session_start(10000.0)
    return ks


# ─── Feature look-ahead bias tests ──────────────────────────────────

class TestNoLookAhead:
    """Assert features at time t use only data ≤ t."""

    def test_zscore_no_future(self, synthetic_df):
        from ai_models.features import ou_zscore, compute_mid
        mid = compute_mid(synthetic_df)
        z = ou_zscore(mid, window=50)

        # z-score at index t should be NaN for t < window-1
        assert z.iloc[:49].isna().all(), "z-score should be NaN before window fills"

        # Verify z at t=100 uses only data up to t=100
        z_at_100 = z.iloc[100]
        manual_mu = mid.iloc[51:101].mean()
        manual_sig = mid.iloc[51:101].std() + 1e-9
        manual_z = (mid.iloc[100] - manual_mu) / manual_sig
        np.testing.assert_allclose(z_at_100, manual_z, rtol=1e-5)

    def test_atr_no_future(self, synthetic_df):
        from ai_models.features import rolling_atr
        atr = rolling_atr(synthetic_df, window=14)
        # TR uses close.shift(1), but max(axis=1, skipna=True) fills index 0
        # from high-low alone, so 14 valid TRs exist at index 13 → first valid ATR
        assert atr.iloc[:13].isna().all(), "ATR should be NaN before window fills"
        assert not np.isnan(atr.iloc[13]), "ATR should be valid at index 13 (14th bar)"

    def test_feature_matrix_shape(self, synthetic_df):
        from ai_models.features import build_feature_matrix
        feats = build_feature_matrix(synthetic_df, window=50)
        assert feats.shape[1] == 24, f"Expected 24 features, got {feats.shape[1]}"
        assert len(feats) == len(synthetic_df)

    # ---- Truncation test: features must not change when future is removed ----

    def test_truncation_invariance(self, synthetic_df):
        """Feature row at index t must be identical whether computed on
        data[:t+1] or data[:t+500]. Any difference proves look-ahead."""
        from ai_models.features import build_feature_matrix

        full_feats = build_feature_matrix(synthetic_df, window=50)
        T = 500  # test point (well past warm-up)

        # Build features on truncated frame [0..T]
        trunc_df = synthetic_df.iloc[: T + 1].copy()
        trunc_feats = build_feature_matrix(trunc_df, window=50)

        row_full = full_feats.iloc[T].values
        row_trunc = trunc_feats.iloc[T].values

        # Replace NaNs with 0 for comparison (both should match)
        row_full = np.nan_to_num(row_full)
        row_trunc = np.nan_to_num(row_trunc)

        np.testing.assert_allclose(
            row_full, row_trunc, rtol=1e-6, atol=1e-9,
            err_msg=f"Feature at t={T} differs between full and truncated data → look-ahead bias"
        )

    def test_truncation_invariance_multi_points(self, synthetic_df):
        """Same as above but at 10 random points spread across the series."""
        from ai_models.features import build_feature_matrix

        full_feats = build_feature_matrix(synthetic_df, window=50)
        rng = np.random.default_rng(7)
        test_points = sorted(rng.choice(range(200, len(synthetic_df) - 1), size=10, replace=False))

        for T in test_points:
            trunc_feats = build_feature_matrix(synthetic_df.iloc[: T + 1].copy(), window=50)
            row_full = np.nan_to_num(full_feats.iloc[T].values)
            row_trunc = np.nan_to_num(trunc_feats.iloc[T].values)
            np.testing.assert_allclose(
                row_full, row_trunc, rtol=1e-6, atol=1e-9,
                err_msg=f"Look-ahead bias detected at t={T}"
            )

    def test_zscore_independent_of_future_data(self, synthetic_df):
        """z-score at t must not change if we corrupt data after t."""
        from ai_models.features import ou_zscore, compute_mid

        mid = compute_mid(synthetic_df)
        z_orig = ou_zscore(mid, window=50)

        T = 300
        mid_corrupted = mid.copy()
        mid_corrupted.iloc[T + 1 :] = 9999.0  # poison future
        z_corrupt = ou_zscore(mid_corrupted, window=50)

        np.testing.assert_allclose(
            z_orig.iloc[T], z_corrupt.iloc[T], rtol=1e-12,
            err_msg="z-score at t changed when future data was corrupted"
        )

    def test_atr_independent_of_future_data(self, synthetic_df):
        """ATR at t must not change if we corrupt data after t."""
        from ai_models.features import rolling_atr

        atr_orig = rolling_atr(synthetic_df, window=14)

        T = 200
        df_corrupt = synthetic_df.copy()
        df_corrupt.iloc[T + 1 :, df_corrupt.columns.get_loc("high")] = 99999
        df_corrupt.iloc[T + 1 :, df_corrupt.columns.get_loc("low")] = 1
        atr_corrupt = rolling_atr(df_corrupt, window=14)

        np.testing.assert_allclose(
            atr_orig.iloc[T], atr_corrupt.iloc[T], rtol=1e-12,
            err_msg="ATR at t changed when future data was corrupted"
        )

    def test_vwap_deviation_no_future(self, synthetic_df):
        """VWAP dev at t must not change when future is poisoned."""
        from ai_models.features import vwap_deviation

        vd_orig = vwap_deviation(synthetic_df, window=14)

        T = 300
        df_corrupt = synthetic_df.copy()
        df_corrupt.iloc[T + 1 :, df_corrupt.columns.get_loc("close")] = 99999
        df_corrupt.iloc[T + 1 :, df_corrupt.columns.get_loc("volume")] = 0
        vd_corrupt = vwap_deviation(df_corrupt, window=14)

        np.testing.assert_allclose(
            vd_orig.iloc[T], vd_corrupt.iloc[T], rtol=1e-6,
            err_msg="VWAP deviation at t changed when future data was corrupted"
        )

    def test_momentum_no_future(self, synthetic_df):
        """Momentum features at t must not depend on t+1..T."""
        from ai_models.features import momentum, compute_mid

        mid = compute_mid(synthetic_df)
        mom_orig = momentum(mid, periods=[5, 15, 60])

        T = 300
        mid_corrupt = mid.copy()
        mid_corrupt.iloc[T + 1 :] = 0.001
        mom_corrupt = momentum(mid_corrupt, periods=[5, 15, 60])

        for col in mom_orig.columns:
            np.testing.assert_allclose(
                mom_orig[col].iloc[T], mom_corrupt[col].iloc[T], rtol=1e-12,
                err_msg=f"Momentum {col} at t={T} depends on future data"
            )

    def test_realized_vol_no_future(self, synthetic_df):
        """Realized vol at t must not change when future data is corrupted."""
        from ai_models.features import realized_vol, compute_mid

        mid = compute_mid(synthetic_df)
        rv_orig = realized_vol(mid, window=50)

        T = 300
        mid_corrupt = mid.copy()
        mid_corrupt.iloc[T + 1 :] = 1.0
        rv_corrupt = realized_vol(mid_corrupt, window=50)

        np.testing.assert_allclose(
            rv_orig.iloc[T], rv_corrupt.iloc[T], rtol=1e-12,
            err_msg="Realized vol at t depends on future data"
        )

    def test_lob_imbalance_no_future(self, synthetic_df):
        """LOB imbalance proxy at t must not use data > t."""
        from ai_models.features import lob_imbalance_proxy

        lob_orig = lob_imbalance_proxy(synthetic_df)

        T = 300
        df_corrupt = synthetic_df.copy()
        df_corrupt.iloc[T + 1 :, df_corrupt.columns.get_loc("close")] = 99999
        lob_corrupt = lob_imbalance_proxy(df_corrupt)

        np.testing.assert_allclose(
            lob_orig.iloc[T], lob_corrupt.iloc[T], rtol=1e-6,
            err_msg="LOB imbalance at t depends on future data"
        )

    def test_full_feature_matrix_no_future(self, synthetic_df):
        """The entire 24-dim feature vector at t must not use data > t."""
        from ai_models.features import build_feature_matrix

        feats_orig = build_feature_matrix(synthetic_df, window=50)

        T = 400
        df_corrupt = synthetic_df.copy()
        # Corrupt all OHLCV columns after T
        for col in ["open", "high", "low", "close"]:
            df_corrupt.iloc[T + 1 :, df_corrupt.columns.get_loc(col)] = 99999
        df_corrupt.iloc[T + 1 :, df_corrupt.columns.get_loc("volume")] = 0
        feats_corrupt = build_feature_matrix(df_corrupt, window=50)

        row_orig = np.nan_to_num(feats_orig.iloc[T].values)
        row_corrupt = np.nan_to_num(feats_corrupt.iloc[T].values)

        np.testing.assert_allclose(
            row_orig, row_corrupt, rtol=1e-5, atol=1e-9,
            err_msg=f"Feature vector at t={T} changed when future data was corrupted"
        )


# ─── EOD liquidation tests ──────────────────────────────────────────

class TestEODLiquidation:
    """Verify 100% position closure by 22:00 GMT in backtest env."""

    def test_env_forces_close_at_eod(self, multiday_df):
        """Step through the env with a permanent long: position must be
        flat at or before the first bar where hour >= 22."""
        from ai_models.rl_agent import XauIntradayEnv

        env = XauIntradayEnv(multiday_df, max_hold_bars=99999)
        obs, _ = env.reset()

        # Open a long immediately
        obs, _, done, _, info = env.step(1)
        assert info["position"] != 0, "Should have opened a position"

        closed_by_eod = False

        while not done:
            # Always hold (action=0) — never voluntarily close
            obs, _, done, truncated, info = env.step(0)
            # env.i was incremented after step; the bar processed was env.i - 1
            processed_idx = env.i - 1
            bar_dt = multiday_df.iloc[min(processed_idx, len(multiday_df) - 1)]["datetime"]

            if bar_dt.hour >= 22 and info["position"] == 0:
                closed_by_eod = True
                break

            if done or truncated:
                break

        assert closed_by_eod, (
            "Position was NOT closed by 22:00 GMT — EOD liquidation failed"
        )

    def test_no_position_held_past_eod(self, multiday_df):
        """Across the full episode, assert position == 0 at every bar
        where hour >= 22, regardless of agent action."""
        from ai_models.rl_agent import XauIntradayEnv

        env = XauIntradayEnv(multiday_df, max_hold_bars=99999)
        obs, _ = env.reset()

        # Strategy: open long, re-open every time it closes
        action = 1
        violations = []

        while True:
            obs, _, done, truncated, info = env.step(action)
            if done or truncated:
                break

            # The bar that was just processed is env.i - 1
            processed_idx = env.i - 1
            bar_dt = multiday_df.iloc[min(processed_idx, len(multiday_df) - 1)]["datetime"]
            if bar_dt.hour >= 22 and info["position"] != 0:
                violations.append((processed_idx, bar_dt, info["position"]))

            # Re-open if flat and well before EOD
            if info["position"] == 0 and bar_dt.hour < 21:
                action = 1
            else:
                action = 0

        assert len(violations) == 0, (
            f"{len(violations)} bars with open position past 22:00 GMT:\n"
            + "\n".join(f"  idx={v[0]} dt={v[1]} pos={v[2]}" for v in violations[:10])
        )

    def test_kill_switch_eod_closes_all(self):
        """KillSwitch.check() returns should_close_all=True at/after 22:00 GMT."""
        from risk.kill_switch import KillSwitch
        from unittest.mock import patch

        ks = KillSwitch(eod_hour_gmt=22)
        ks.set_session_start(10000.0)

        # Mock datetime.now to 22:01 GMT
        fake_now = datetime(2025, 6, 15, 22, 1, 0, tzinfo=timezone.utc)
        with patch("risk.kill_switch.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = ks.check(10000.0)

        assert result["should_close_all"], "KillSwitch must close all at 22:00+ GMT"
        assert not result["allow_new_trades"], "No new trades allowed at EOD"

    def test_kill_switch_allows_before_eod(self):
        """KillSwitch should allow trades before 21:00 GMT."""
        from risk.kill_switch import KillSwitch
        from unittest.mock import patch

        ks = KillSwitch(eod_hour_gmt=22)
        ks.set_session_start(10000.0)

        fake_now = datetime(2025, 6, 15, 15, 0, 0, tzinfo=timezone.utc)
        with patch("risk.kill_switch.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = ks.check(10000.0)

        assert not result["should_close_all"]
        assert result["allow_new_trades"]

    def test_kill_switch_no_new_trades_hour_before_eod(self):
        """No new trades within 1 hour of EOD (21:00-22:00 GMT)."""
        from risk.kill_switch import KillSwitch
        from unittest.mock import patch

        ks = KillSwitch(eod_hour_gmt=22)
        ks.set_session_start(10000.0)

        fake_now = datetime(2025, 6, 15, 21, 30, 0, tzinfo=timezone.utc)
        with patch("risk.kill_switch.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = ks.check(10000.0)

        assert not result["allow_new_trades"], "No new trades in 21:00-22:00 window"


# ─── Kelly Position Sizing tests ────────────────────────────────────

class TestKellySizing:
    def test_fraction_capped_at_2pct(self, kelly_sizer):
        f = kelly_sizer.optimal_fraction()
        assert f <= 0.02, f"Kelly fraction {f} exceeds 2% cap"

    def test_zero_win_rate_returns_zero(self):
        from risk.kelly import KellyPositionSizer
        sizer = KellyPositionSizer()
        for _ in range(50):
            sizer.record_trade(-1.0)
        assert sizer.optimal_fraction() == 0.0

    def test_lot_size_positive(self, kelly_sizer):
        lot = kelly_sizer.calc_lot_size(
            account_equity=10000,
            entry_price=2000,
            sl_distance=5.0,
        )
        assert lot >= 0.01
        assert lot <= 10000 * 2000 / (2000 * 100)  # leverage cap

    def test_lot_size_zero_sl(self, kelly_sizer):
        lot = kelly_sizer.calc_lot_size(
            account_equity=10000,
            entry_price=2000,
            sl_distance=0.0,
        )
        assert lot == 0.0

    def test_vwap_slicing(self):
        from risk.kelly import vwap_slice_orders
        orders = vwap_slice_orders(
            total_lot=1.0,
            base_price=2000.0,
            atr=5.0,
            num_slices=5,
        )
        assert len(orders) == 5
        total_lot = sum(o["lot"] for o in orders)
        assert abs(total_lot - 1.0) < 0.05  # rounding tolerance
        # Check 30s spacing
        for i, o in enumerate(orders):
            assert o["delay_seconds"] == i * 30


# ─── Kill Switch tests ──────────────────────────────────────────────

class TestKillSwitch:
    def test_mdd_triggers(self, kill_switch):
        # 15% drawdown: equity drops from 10000 to 8500
        result = kill_switch.check(8500.0)
        assert result["should_close_all"] is True
        assert "MAX DRAWDOWN" in result["reason"]

    def test_daily_loss_triggers(self, kill_switch):
        # 5% daily loss: equity drops from 10000 to 9500
        result = kill_switch.check(9500.0)
        assert result["should_close_all"] is True
        assert "DAILY LOSS" in result["reason"]

    def test_normal_operation(self, kill_switch):
        result = kill_switch.check(9800.0)
        # 2% drawdown — should be fine
        assert result["should_close_all"] is False

    def test_drawdown_calculation(self, kill_switch):
        kill_switch.update_equity(12000.0)  # new peak
        dd = kill_switch.drawdown_pct(10200.0)
        assert abs(dd - 15.0) < 0.01

    def test_reset_daily(self, kill_switch):
        kill_switch.check(8500.0)  # trigger kill
        assert kill_switch.is_killed is True
        kill_switch.reset_daily(10000.0)
        assert kill_switch.is_killed is False


# ─── T-KAN model tests ──────────────────────────────────────────────

class TestTKAN:
    def test_forward_shape(self):
        import torch
        from ai_models.regime_tkan import TKAN
        model = TKAN(input_dim=6, hidden_dim=32, num_classes=2, order=4)
        x = torch.randn(4, 50, 6)
        out = model(x)
        assert out.shape == (4, 2)

    def test_predict_returns_label(self):
        import torch
        from ai_models.regime_tkan import TKAN
        model = TKAN(input_dim=6, hidden_dim=32, num_classes=2, order=4)
        x = np.random.randn(50, 6).astype(np.float32)
        label = model.predict(x)
        assert label in (0, 1)

    def test_predict_proba_sums_to_one(self):
        from ai_models.regime_tkan import TKAN
        model = TKAN(input_dim=6, hidden_dim=32, num_classes=2, order=4)
        x = np.random.randn(50, 6).astype(np.float32)
        proba = model.predict_proba(x)
        np.testing.assert_allclose(proba.sum(), 1.0, atol=1e-5)


# ─── RL Environment tests ───────────────────────────────────────────

class TestRLEnv:
    def test_env_reset(self, synthetic_df):
        from ai_models.rl_agent import XauIntradayEnv
        env = XauIntradayEnv(synthetic_df)
        obs, info = env.reset()
        assert obs.shape == (24,)
        assert not np.any(np.isnan(obs))

    def test_env_step(self, synthetic_df):
        from ai_models.rl_agent import XauIntradayEnv
        env = XauIntradayEnv(synthetic_df)
        env.reset()
        obs, reward, done, truncated, info = env.step(0)  # hold
        assert obs.shape == (24,)
        assert isinstance(reward, float)

    def test_action_space(self, synthetic_df):
        from ai_models.rl_agent import XauIntradayEnv
        env = XauIntradayEnv(synthetic_df)
        assert env.action_space.n == 4


# ─── OU MLE parameter estimation tests ─────────────────────────────

class TestOUParams:
    def test_mle_returns_correct_columns(self, synthetic_df):
        from ai_models.features import ou_params_mle, compute_mid
        mid = compute_mid(synthetic_df)
        ou = ou_params_mle(mid, window=100)
        assert {"ou_theta", "ou_mu", "ou_sigma", "ou_halflife"}.issubset(ou.columns)
        assert len(ou) == len(mid)

    def test_theta_positive(self, synthetic_df):
        from ai_models.features import ou_params_mle, compute_mid
        mid = compute_mid(synthetic_df)
        ou = ou_params_mle(mid, window=100)
        assert (ou["ou_theta"].iloc[200:] >= 0).all(), "θ must be non-negative"

    def test_halflife_mostly_finite(self, synthetic_df):
        from ai_models.features import ou_params_mle, compute_mid
        mid = compute_mid(synthetic_df)
        ou = ou_params_mle(mid, window=100)
        valid = ou["ou_halflife"].iloc[200:]
        assert (valid < 1e10).mean() > 0.8, "Most half-lives should be finite"

    def test_feature_matrix_24_cols_with_ou(self, synthetic_df):
        from ai_models.features import build_feature_matrix
        feats = build_feature_matrix(synthetic_df, window=50)
        assert feats.shape[1] == 24, f"Must stay at 24 features, got {feats.shape[1]}"
        # ou_theta is col 19 — should have non-NaN values after warmup
        assert not feats.iloc[300:, 19].isna().all(), "ou_theta empty after warmup"


# ─── Walk-forward TCA fill tracking test ────────────────────────────

class TestWalkForwardTCA:
    def test_evaluate_agent_returns_tca(self, synthetic_df):
        from stable_baselines3 import PPO
        from stable_baselines3.common.vec_env import DummyVecEnv
        from ai_models.rl_agent import XauIntradayEnv
        from backtest.walkforward import evaluate_agent

        env = DummyVecEnv([lambda: XauIntradayEnv(synthetic_df)])
        model = PPO("MlpPolicy", env, n_steps=64, batch_size=32, verbose=0)
        model.learn(total_timesteps=128)

        metrics = evaluate_agent(model, synthetic_df)
        assert metrics.tca is not None, "TCA report must be populated"
        assert isinstance(metrics.tca.avg_slippage_bps, float)
        assert isinstance(metrics.avg_slippage_bps, float)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
