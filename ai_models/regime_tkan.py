"""T-KAN (Temporal Kolmogorov-Arnold Network) regime classifier.

Classifies XAU/USD intraday regimes:
  0 = range-bound (mean-reversion favorable)
  1 = trending    (momentum favorable)

Uses Chebyshev polynomial basis functions in KAN layers,
applied over a temporal sequence (batch, seq_len=50, input_dim=6).
"""
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from pathlib import Path

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import (
    TKAN_SEQ_LEN, TKAN_INPUT_DIM, TKAN_CHEBY_ORDER,
    TKAN_HIDDEN_DIM, TKAN_NUM_CLASSES, MODEL_DIR,
)


class ChebyshevBasis(nn.Module):
    """Learnable Chebyshev polynomial basis expansion."""

    def __init__(self, in_features: int, out_features: int, order: int = 4):
        super().__init__()
        self.order = order
        self.coeffs = nn.Parameter(
            torch.randn(in_features, out_features, order + 1) * 0.1
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (batch, in_features)  ->  (batch, out_features)
        x_clamped = torch.clamp(x, -1.0, 1.0)  # Chebyshev domain [-1, 1]
        # Build Chebyshev polynomials T_0 ... T_order
        T = [torch.ones_like(x_clamped), x_clamped]
        for n in range(2, self.order + 1):
            T.append(2 * x_clamped * T[-1] - T[-2])
        T = torch.stack(T, dim=-1)  # (batch, in_features, order+1)
        # Weighted sum: (b, i, k) x (i, o, k) -> (b, o)
        out = torch.einsum("bik,iok->bo", T, self.coeffs)
        return out


class KANLayer(nn.Module):
    """Single KAN layer with Chebyshev basis + residual."""

    def __init__(self, in_dim: int, out_dim: int, order: int = 4):
        super().__init__()
        self.basis = ChebyshevBasis(in_dim, out_dim, order)
        self.norm = nn.LayerNorm(out_dim)
        self.residual = nn.Linear(in_dim, out_dim) if in_dim != out_dim else nn.Identity()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.norm(self.basis(x) + self.residual(x))


class TKAN(nn.Module):
    """Temporal KAN for sequence classification.

    Architecture:
      Input (batch, seq_len, input_dim)
      -> KAN layers applied per-timestep
      -> GRU for temporal aggregation
      -> Classification head
    """

    def __init__(
        self,
        input_dim: int = TKAN_INPUT_DIM,
        hidden_dim: int = TKAN_HIDDEN_DIM,
        num_classes: int = TKAN_NUM_CLASSES,
        order: int = TKAN_CHEBY_ORDER,
        num_kan_layers: int = 2,
    ):
        super().__init__()
        self.input_norm = nn.LayerNorm(input_dim)

        # KAN feature extractor (per timestep)
        layers = [KANLayer(input_dim, hidden_dim, order)]
        for _ in range(num_kan_layers - 1):
            layers.append(KANLayer(hidden_dim, hidden_dim, order))
        self.kan_layers = nn.ModuleList(layers)

        # Temporal aggregation
        self.gru = nn.GRU(hidden_dim, hidden_dim, batch_first=True)

        # Classifier
        self.head = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(hidden_dim // 2, num_classes),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: (batch, seq_len, input_dim) -> (batch, num_classes) logits."""
        batch, seq_len, _ = x.shape
        x = self.input_norm(x)

        # Apply KAN layers per timestep
        x_flat = x.reshape(batch * seq_len, -1)
        for layer in self.kan_layers:
            x_flat = layer(x_flat)
        x = x_flat.reshape(batch, seq_len, -1)

        # GRU: use last hidden state
        _, h_n = self.gru(x)
        h = h_n.squeeze(0)  # (batch, hidden_dim)

        return self.head(h)

    def predict(self, window: np.ndarray | torch.Tensor) -> int:
        """Single-sample inference. Returns regime label 0 or 1."""
        self.eval()
        with torch.no_grad():
            if isinstance(window, np.ndarray):
                window = torch.from_numpy(window).float()
            if window.ndim == 2:
                window = window.unsqueeze(0)
            logits = self.forward(window)
            return int(logits.argmax(dim=-1).item())

    def predict_proba(self, window: np.ndarray | torch.Tensor) -> np.ndarray:
        """Return class probabilities."""
        self.eval()
        with torch.no_grad():
            if isinstance(window, np.ndarray):
                window = torch.from_numpy(window).float()
            if window.ndim == 2:
                window = window.unsqueeze(0)
            logits = self.forward(window)
            return F.softmax(logits, dim=-1).cpu().numpy().squeeze()


def _adx(df: "pd.DataFrame", window: int = 14) -> "pd.Series":
    """Average Directional Index (Wilder smoothing).

    Returns a Series in [0, 100].  ADX > 25 = trending.
    """
    import pandas as pd
    high = df["high"]
    low  = df["low"]
    close_prev = df["close"].shift(1)

    tr  = pd.concat([
        high - low,
        (high - close_prev).abs(),
        (low  - close_prev).abs(),
    ], axis=1).max(axis=1)

    plus_dm  = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    # Only keep the larger DM; set the other to 0
    cond = plus_dm >= minus_dm
    plus_dm[~cond]  = 0.0
    minus_dm[cond]  = 0.0

    # Wilder smoothing (α = 1/window)
    alpha = 1.0 / window
    atr_s   = tr.ewm(alpha=alpha, adjust=False).mean()
    pdm_s   = plus_dm.ewm(alpha=alpha, adjust=False).mean()
    mdm_s   = minus_dm.ewm(alpha=alpha, adjust=False).mean()

    pdi = 100.0 * pdm_s / (atr_s + 1e-9)
    mdi = 100.0 * mdm_s / (atr_s + 1e-9)
    dx  = 100.0 * (pdi - mdi).abs() / (pdi + mdi + 1e-9)
    adx = dx.ewm(alpha=alpha, adjust=False).mean()
    return adx


def _fractal_efficiency_ratio(mid: "pd.Series", window: int = 20) -> "pd.Series":
    """Fractal Efficiency Ratio (FER).

    FER = |Price(t) - Price(t-N)| / Σ|ΔPrice| over N bars.

    Range [0, 1].  FER near 1 → directionally efficient (trending).
    FER near 0 → choppy / mean-reverting.
    """
    net_move   = (mid - mid.shift(window)).abs()
    path_len   = mid.diff().abs().rolling(window, min_periods=window).sum() + 1e-9
    return (net_move / path_len).clip(0.0, 1.0)


def _hurst_variance_ratio(mid: "pd.Series", short: int = 5, long: int = 20) -> "pd.Series":
    """Simplified variance-ratio Hurst estimate.

    VR = Var(r_long) / (k * Var(r_short)),  k = long/short
    H ≈ 0.5 * log2(VR + 1e-9) / log2(k)

    H > 0.55 → persistent (trending)
    H < 0.45 → anti-persistent (mean-reverting)
    Returns H estimate in [0, 1].
    """
    import numpy as np
    r_s = mid.pct_change(short)
    r_l = mid.pct_change(long)
    var_s = r_s.rolling(long * 4, min_periods=long).var() + 1e-12
    var_l = r_l.rolling(long * 4, min_periods=long).var() + 1e-12
    k = long / short
    vr = var_l / (k * var_s)
    h = 0.5 * np.log2(vr.clip(lower=1e-9)) / np.log2(k)
    return h.clip(0.0, 1.0).fillna(0.5)


def label_regimes(df, atr_window: int = 14, threshold: float = 1.5) -> np.ndarray:
    """Multi-signal regime labeling: trend (1) or range (0).

    Three independent signals vote; majority (≥ 2 of 3) wins:
      1. ADX > 25                            (trend strength)
      2. Fractal Efficiency Ratio > 0.40     (directional efficiency)
      3. Hurst estimate > 0.55               (persistence)

    Falls back gracefully when high/low are missing (uses close only).
    """
    import pandas as pd
    mid = df["close"] if "mid" not in df.columns else df["mid"]

    # ── Signal 1: ADX ──────────────────────────────────────────────────────
    if "high" in df.columns and "low" in df.columns:
        adx_val = _adx(df, window=atr_window)
        vote_adx = (adx_val > 25.0).astype(int)
    else:
        # Fallback: use original ATR-directional heuristic
        atr_fb  = mid.diff().abs().rolling(atr_window).mean()
        directional = (mid - mid.shift(atr_window)).abs()
        vote_adx = (directional > threshold * atr_fb).astype(int)

    # ── Signal 2: Fractal Efficiency Ratio ─────────────────────────────────
    fer      = _fractal_efficiency_ratio(mid, window=atr_window)
    vote_fer = (fer > 0.40).astype(int)

    # ── Signal 3: Hurst variance ratio ─────────────────────────────────────
    hurst    = _hurst_variance_ratio(mid, short=5, long=atr_window)
    vote_hurst = (hurst > 0.55).astype(int)

    # ── Majority vote ───────────────────────────────────────────────────────
    total_votes = vote_adx.fillna(0) + vote_fer.fillna(0) + vote_hurst.fillna(0)
    labels = (total_votes >= 2).astype(int).values
    return labels


def train_tkan(
    train_x: np.ndarray,
    train_y: np.ndarray,
    epochs: int = 50,
    lr: float = 1e-3,
    batch_size: int = 64,
    save_path: Path | None = None,
) -> TKAN:
    """Train the T-KAN regime classifier.

    Args:
        train_x: (N, seq_len, input_dim)
        train_y: (N,) integer labels
    """
    model = TKAN()
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    criterion = nn.CrossEntropyLoss()

    dataset = torch.utils.data.TensorDataset(
        torch.from_numpy(train_x).float(),
        torch.from_numpy(train_y).long(),
    )
    loader = torch.utils.data.DataLoader(
        dataset, batch_size=batch_size, shuffle=True, drop_last=True
    )

    model.train()
    for epoch in range(epochs):
        total_loss = 0.0
        correct = 0
        total = 0
        for xb, yb in loader:
            optimizer.zero_grad()
            logits = model(xb)
            loss = criterion(logits, yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            total_loss += loss.item() * xb.size(0)
            correct += (logits.argmax(-1) == yb).sum().item()
            total += xb.size(0)
        scheduler.step()
        if (epoch + 1) % 10 == 0:
            print(f"Epoch {epoch+1}/{epochs}  loss={total_loss/total:.4f}  acc={correct/total:.3f}")

    if save_path is None:
        save_path = MODEL_DIR / "regime_tkan.pt"
    save_path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), save_path)
    print(f"T-KAN saved to {save_path}")
    return model
