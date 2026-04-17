"""Parse live_state.json into typed dataclasses for Streamlit + Telegram."""
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import LIVE_STATE_PATH, LOG_DIR


@dataclass
class SymbolState:
    symbol: str
    position: int = 0        # -1 short, 0 flat, 1 long
    entry_price: float = 0.0
    unrealized_pnl: float = 0.0
    regime: int = -1         # 0=range, 1=trend, -1=unknown
    kelly_f: float = 0.0
    drawdown_pct: float = 0.0
    last_signal: dict = field(default_factory=dict)
    timestamp: str = ""

    @property
    def position_str(self) -> str:
        return {1: "LONG", -1: "SHORT", 0: "FLAT"}.get(self.position, "FLAT")

    @property
    def regime_str(self) -> str:
        return {1: "TREND", 0: "RANGE", -1: "---"}.get(self.regime, "---")


@dataclass
class LiveState:
    symbols: dict = field(default_factory=dict)  # str → SymbolState
    account: dict = field(default_factory=lambda: {
        "equity": 0.0, "balance": 0.0, "drawdown_pct": 0.0
    })
    system: dict = field(default_factory=lambda: {
        "alive": False, "killed": False, "signal_count": 0,
        "kill_reason": "", "last_heartbeat": "",
    })
    updated_at: float = 0.0

    @property
    def is_alive(self) -> bool:
        return bool(self.system.get("alive", False))

    @property
    def is_killed(self) -> bool:
        return bool(self.system.get("killed", False))

    @property
    def equity(self) -> float:
        return float(self.account.get("equity", 0.0))

    @property
    def balance(self) -> float:
        return float(self.account.get("balance", 0.0))

    @property
    def drawdown_pct(self) -> float:
        return float(self.account.get("drawdown_pct", 0.0))

    @property
    def signal_count(self) -> int:
        return int(self.system.get("signal_count", 0))

    @property
    def last_heartbeat(self) -> str:
        return self.system.get("last_heartbeat", "")


_state_cache: dict = {"mtime": 0.0, "state": None}


def read_state(path: Path | None = None) -> LiveState:
    """Read and parse live_state.json.

    Uses mtime-based caching: returns the cached LiveState if the file has not
    changed since the last read, avoiding redundant I/O on every 2-second poll.
    Returns empty LiveState on any read failure.
    """
    path = path or LIVE_STATE_PATH
    try:
        mtime = path.stat().st_mtime
        if mtime == _state_cache["mtime"] and _state_cache["state"] is not None:
            return _state_cache["state"]
    except OSError:
        return LiveState()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LiveState()
    except json.JSONDecodeError as exc:
        log.warning("state_reader: JSON decode error in %s: %s", path, exc)
        return _state_cache["state"] or LiveState()   # return last good state
    except OSError as exc:
        log.warning("state_reader: OS error reading %s: %s", path, exc)
        return _state_cache["state"] or LiveState()
    except Exception as exc:
        log.warning("state_reader: unexpected error reading %s: %s", path, exc)
        return LiveState()

    symbols: dict[str, SymbolState] = {}
    for key, val in raw.items():
        if key.startswith("_") or not isinstance(val, dict):
            continue
        symbols[key] = SymbolState(
            symbol=key,
            position=int(val.get("position", 0)),
            entry_price=float(val.get("entry_price", 0.0)),
            unrealized_pnl=float(val.get("unrealized_pnl", 0.0)),
            regime=int(val.get("regime", -1)),
            kelly_f=float(val.get("kelly_f", 0.0)),
            drawdown_pct=float(val.get("drawdown_pct", 0.0)),
            last_signal=val.get("last_signal", {}),
            timestamp=val.get("timestamp", ""),
        )

    result = LiveState(
        symbols=symbols,
        account=raw.get("_account", {}),
        system=raw.get("_system", {}),
        updated_at=time.time(),
    )
    _state_cache["mtime"] = mtime
    _state_cache["state"] = result
    return result


def read_worker_status() -> dict[str, str]:
    """Return {symbol: status} from worker_status.json (written by multi_runner).

    Status values: "waiting" | "training" | "live" | "error"
    Returns empty dict if file missing (single-symbol live mode).
    """
    path = LOG_DIR / "worker_status.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def read_active_charts(mt5_files_path: "Path | None" = None) -> set:
    """Return set of symbols whose ats_chart_{SYMBOL}.txt contains '1'.

    Mirrors scan_active_symbols() in multi_runner.py — same file format.
    Returns empty set if MT5 Common Files directory is absent or unreadable.
    """
    if mt5_files_path is None:
        from config import MT5_FILES_PATH
        mt5_files_path = MT5_FILES_PATH
    active: set = set()
    try:
        for f in mt5_files_path.glob("ats_chart_*.txt"):
            try:
                content = ""
                for enc in ("utf-16", "utf-8", "latin-1"):
                    try:
                        content = f.read_text(encoding=enc).strip()
                        break
                    except (UnicodeDecodeError, UnicodeError):
                        pass
                if content == "1":
                    sym = f.stem.replace("ats_chart_", "").upper()
                    active.add(sym)
            except OSError:
                pass
    except OSError:
        pass
    return active


def tail_log(lines: int = 50) -> list[str]:
    """Return last `lines` lines of signal_server.log.

    Seeks near EOF to avoid reading the entire file on every dashboard refresh.
    """
    log_path = LOG_DIR / "signal_server.log"
    try:
        with log_path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, lines * 200)  # ~200 chars per line estimate
            f.seek(max(0, size - chunk))
            data = f.read()
        return data.decode("utf-8", errors="replace").splitlines()[-lines:]
    except FileNotFoundError:
        return ["[log file not found]"]
