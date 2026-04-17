"""Persistent trade journal — symbol-agnostic.

Thread-safe log of closed trades. Written on every closed deal and
reloaded on restart to seed Kelly trade history and last-deal tracking.

TradeRecord  — one closed trade (dataclass)
TradeJournal — thread-safe JSON + CSV persistent log
"""
import csv
import json
import logging
import threading
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import LOG_DIR

logger = logging.getLogger("journal")


@dataclass
class TradeRecord:
    """One closed trade."""
    trade_id:    str
    symbol:      str
    direction:   str    # "long" | "short"
    open_price:  float
    close_price: float
    lot_size:    float
    open_time:   str    # ISO-8601 UTC
    close_time:  str    # ISO-8601 UTC
    pnl_usd:     float
    commission:  float
    net_pnl:     float
    trade_date:  str    # YYYY-MM-DD

    @classmethod
    def create(
        cls,
        symbol:      str,
        direction:   str,
        open_price:  float,
        close_price: float,
        lot_size:    float,
        open_time:   datetime,
        close_time:  datetime,
        pnl_usd:     float,
        commission:  float = 0.0,
        trade_id:    str   = "",
    ) -> "TradeRecord":
        if not trade_id:
            trade_id = f"{symbol}_{close_time.strftime('%Y%m%d_%H%M%S')}"
        return cls(
            trade_id    = trade_id,
            symbol      = symbol.upper(),
            direction   = direction.lower(),
            open_price  = round(open_price,  5),
            close_price = round(close_price, 5),
            lot_size    = round(lot_size,    2),
            open_time   = open_time.isoformat(),
            close_time  = close_time.isoformat(),
            pnl_usd     = round(pnl_usd,     2),
            commission  = round(commission,  2),
            net_pnl     = round(pnl_usd + commission, 2),
            trade_date  = close_time.date().isoformat(),
        )

    def to_dict(self) -> dict:
        return asdict(self)


class TradeJournal:
    """Thread-safe, persistent trade log with CSV export.

    One journal per symbol. Loaded at startup to seed _last_deal and
    Kelly trade history so no deals are double-counted after restart.
    """

    _CSV_FIELDS = [
        "trade_id", "trade_date", "symbol", "direction",
        "open_time", "close_time", "open_price", "close_price",
        "lot_size", "pnl_usd", "commission", "net_pnl",
    ]

    def __init__(self, path: Path | None = None):
        self._path   = path or (LOG_DIR / "trade_journal.json")
        self._lock   = threading.Lock()
        self._trades: list[TradeRecord] = []
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def add_trade(
        self,
        symbol:      str,
        direction:   str,
        open_price:  float,
        close_price: float,
        lot_size:    float,
        open_time:   datetime,
        close_time:  datetime,
        pnl_usd:     float,
        commission:  float = 0.0,
        trade_id:    str   = "",
    ) -> TradeRecord:
        record = TradeRecord.create(
            symbol=symbol, direction=direction,
            open_price=open_price, close_price=close_price,
            lot_size=lot_size, open_time=open_time, close_time=close_time,
            pnl_usd=pnl_usd, commission=commission, trade_id=trade_id,
        )
        with self._lock:
            self._trades.append(record)
        self._save()
        logger.info(
            f"[Journal] {record.symbol} {record.direction.upper()} "
            f"{record.lot_size} lot  net={record.net_pnl:+.2f} USD"
        )
        return record

    @property
    def trades(self) -> list[TradeRecord]:
        with self._lock:
            return list(self._trades)

    @property
    def total_trades(self) -> int:
        with self._lock:
            return len(self._trades)

    @property
    def total_net_pnl(self) -> float:
        with self._lock:
            return round(sum(t.net_pnl for t in self._trades), 2)

    @property
    def win_rate(self) -> float:
        with self._lock:
            if not self._trades:
                return 0.0
            return round(sum(1 for t in self._trades if t.net_pnl > 0) / len(self._trades), 4)

    @property
    def profit_factor(self) -> float:
        with self._lock:
            gp = sum(t.net_pnl for t in self._trades if t.net_pnl > 0)
            gl = abs(sum(t.net_pnl for t in self._trades if t.net_pnl < 0))
            if gl == 0:
                return float("inf") if gp > 0 else 0.0
            return round(gp / gl, 4)

    def stats(self) -> dict:
        with self._lock:
            trades = list(self._trades)
        if not trades:
            return {
                "total_trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "total_net_pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "largest_win": 0.0, "largest_loss": 0.0, "trading_days": 0,
            }
        wins   = [t.net_pnl for t in trades if t.net_pnl > 0]
        losses = [t.net_pnl for t in trades if t.net_pnl < 0]
        gp     = sum(wins)
        gl     = abs(sum(losses))
        return {
            "total_trades":  len(trades),
            "win_rate":      round(len(wins) / len(trades), 4),
            "profit_factor": round(gp / gl, 4) if gl > 0 else float("inf"),
            "total_net_pnl": round(sum(t.net_pnl for t in trades), 2),
            "avg_win":       round(gp / len(wins),   2) if wins   else 0.0,
            "avg_loss":      round(sum(losses) / len(losses), 2) if losses else 0.0,
            "largest_win":   round(max(wins),  2) if wins   else 0.0,
            "largest_loss":  round(min(losses), 2) if losses else 0.0,
            "trading_days":  len({t.trade_date for t in trades}),
        }

    def to_csv(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            trades = list(self._trades)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self._CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for t in trades:
                writer.writerow(t.to_dict())
        logger.info(f"[Journal] Exported {len(trades)} trades to {path}")
        return path

    def _save(self) -> None:
        try:
            tmp = self._path.with_suffix(".tmp")
            with self._lock:
                data = [t.to_dict() for t in self._trades]
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except Exception as exc:
            logger.error(f"[Journal] Save error: {exc}")

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                self._trades = [TradeRecord(**r) for r in raw]
                logger.info(f"[Journal] Loaded {len(self._trades)} trades from {self._path}")
        except Exception as exc:
            logger.warning(f"[Journal] Load error (starting fresh): {exc}")
            self._trades = []
