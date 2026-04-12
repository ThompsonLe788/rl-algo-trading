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


def label_regimes(df, atr_window: int = 14, threshold: float = 1.5) -> np.ndarray:
    """Heuristic labeling: trend if directional move > threshold * ATR, else range."""
    mid = df["close"] if "mid" not in df.columns else df["mid"]
    atr = (df["high"] - df["low"]).rolling(atr_window).mean()
    directional = (mid - mid.shift(atr_window)).abs()
    labels = (directional > threshold * atr).astype(int).values
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
