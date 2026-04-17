"""News / Economic Calendar filter.

Blocks new trades during high-impact news events to avoid:
  - Extreme spread widening (5-10× normal)
  - Stop-hunting spikes
  - Erratic candles that confuse the RL agent

Blackout windows:
  - 15 minutes BEFORE event
  - 10 minutes AFTER event

Sources (in priority order):
  1. logs/economic_calendar.json  — editable override file
  2. Built-in recurring schedule  — NFP, CPI, FOMC (hardcoded dates + recurrence)

Usage in signal_server / kill_switch:
    from risk.news_filter import NewsFilter
    nf = NewsFilter()
    blocked, reason = nf.is_blackout()
    if blocked:
        skip_entry()
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import LOG_DIR, NEWS_BLACKOUT_BEFORE_MINS, NEWS_BLACKOUT_AFTER_MINS

logger = logging.getLogger("news_filter")
log    = logging.getLogger(__name__)

PRE_EVENT_MIN  = NEWS_BLACKOUT_BEFORE_MINS
POST_EVENT_MIN = NEWS_BLACKOUT_AFTER_MINS


# ---------------------------------------------------------------------------
# Recurring event generators
# ---------------------------------------------------------------------------

def _first_weekday(year: int, month: int, weekday: int) -> int:
    """Day-of-month for the first occurrence of weekday (Mon=0 … Sun=6)."""
    from calendar import monthrange as _mr
    first_dow = datetime(year, month, 1).weekday()
    offset = (weekday - first_dow) % 7
    return 1 + offset


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> int:
    """Day-of-month for the N-th (1-based) occurrence of weekday."""
    first = _first_weekday(year, month, weekday)
    return first + (n - 1) * 7


def _recurring_events(year: int) -> list[dict]:
    """Build a list of recurring USD high-impact events for `year`."""
    events: list[dict] = []

    for month in range(1, 13):
        # ── Non-Farm Payroll ────────────────────────────────────────────────
        # First Friday of each month, 13:30 UTC
        nfp_day = _first_weekday(year, month, 4)       # 4 = Friday
        events.append({
            "name": "NFP",
            "datetime": f"{year}-{month:02d}-{nfp_day:02d}T13:30:00+00:00",
            "impact": "high",
            "currencies": ["USD"],
        })

        # ── CPI ─────────────────────────────────────────────────────────────
        # Typically 2nd–3rd Wednesday; approximate with 2nd Wednesday, 13:30 UTC
        cpi_day = _nth_weekday(year, month, 2, 2)      # 2 = Wednesday, 2nd week
        events.append({
            "name": "CPI",
            "datetime": f"{year}-{month:02d}-{cpi_day:02d}T13:30:00+00:00",
            "impact": "high",
            "currencies": ["USD"],
        })

        # ── Retail Sales ─────────────────────────────────────────────────────
        # Typically 2nd Wednesday of month, 13:30 UTC
        rs_day = _nth_weekday(year, month, 3, 2)       # 3 = Thursday, 2nd week
        events.append({
            "name": "Retail Sales",
            "datetime": f"{year}-{month:02d}-{rs_day:02d}T13:30:00+00:00",
            "impact": "medium",
            "currencies": ["USD"],
        })

    # ── FOMC Rate Decisions ──────────────────────────────────────────────────
    # 8 meetings per year; approximate dates hardcoded through 2030
    _fomc: dict[int, list[str]] = {
        2025: [
            "2025-01-29T19:00:00+00:00",  "2025-03-19T18:00:00+00:00",
            "2025-05-07T18:00:00+00:00",  "2025-06-18T18:00:00+00:00",
            "2025-07-30T18:00:00+00:00",  "2025-09-17T18:00:00+00:00",
            "2025-10-29T18:00:00+00:00",  "2025-12-10T19:00:00+00:00",
        ],
        2026: [
            "2026-01-28T19:00:00+00:00",  "2026-03-18T18:00:00+00:00",
            "2026-04-29T18:00:00+00:00",  "2026-06-17T18:00:00+00:00",
            "2026-07-29T18:00:00+00:00",  "2026-09-16T18:00:00+00:00",
            "2026-10-28T18:00:00+00:00",  "2026-12-09T19:00:00+00:00",
        ],
        2027: [
            "2027-01-27T19:00:00+00:00",  "2027-03-17T18:00:00+00:00",
            "2027-05-05T18:00:00+00:00",  "2027-06-16T18:00:00+00:00",
            "2027-07-28T18:00:00+00:00",  "2027-09-15T18:00:00+00:00",
            "2027-10-27T18:00:00+00:00",  "2027-12-08T19:00:00+00:00",
        ],
        2028: [
            "2028-01-26T19:00:00+00:00",  "2028-03-15T18:00:00+00:00",
            "2028-05-03T18:00:00+00:00",  "2028-06-14T18:00:00+00:00",
            "2028-07-26T18:00:00+00:00",  "2028-09-20T18:00:00+00:00",
            "2028-11-01T18:00:00+00:00",  "2028-12-13T19:00:00+00:00",
        ],
        2029: [
            "2029-01-31T19:00:00+00:00",  "2029-03-19T18:00:00+00:00",
            "2029-05-02T18:00:00+00:00",  "2029-06-13T18:00:00+00:00",
            "2029-07-25T18:00:00+00:00",  "2029-09-18T18:00:00+00:00",
            "2029-10-30T18:00:00+00:00",  "2029-12-11T19:00:00+00:00",
        ],
        2030: [
            "2030-01-29T19:00:00+00:00",  "2030-03-18T18:00:00+00:00",
            "2030-05-01T18:00:00+00:00",  "2030-06-12T18:00:00+00:00",
            "2030-07-31T18:00:00+00:00",  "2030-09-17T18:00:00+00:00",
            "2030-10-29T18:00:00+00:00",  "2030-12-11T19:00:00+00:00",
        ],
    }
    for dt_str in _fomc.get(year, []):
        events.append({
            "name": "FOMC",
            "datetime": dt_str,
            "impact": "high",
            "currencies": ["USD"],
        })

    return events


# ---------------------------------------------------------------------------
# NewsFilter class
# ---------------------------------------------------------------------------

class NewsFilter:
    """Check whether current time falls inside a news blackout window.

    Args:
        calendar_path: Path to a JSON file containing a list of event dicts.
                       Each event must have at minimum:
                         {"name": str, "datetime": ISO-8601-with-timezone, "impact": str}
                       Optional: {"currencies": [str, ...]}
                       When ``None``, defaults to ``LOG_DIR / "economic_calendar.json"``.

    Example:
        nf = NewsFilter()
        blocked, reason = nf.is_blackout()
        # blocked=True, reason="NFP" during blackout window

    Update the calendar file:
        nf.save_calendar()          # writes current in-memory events to file
        # Edit logs/economic_calendar.json, then:
        nf.reload()                 # hot-reload without restart
    """

    def __init__(self, calendar_path: Path | None = None):
        self._path = calendar_path or (LOG_DIR / "economic_calendar.json")
        self._events: list[dict] = []
        self.reload()

    # ── public API ────────────────────────────────────────────────────────────

    # Swap rollover: forex brokers charge 3× swap on Wednesday at 22:00 UTC.
    # Block new trades from 21:45 to 22:15 UTC on Wednesday to avoid overnight
    # swap charges on positions opened just before rollover.
    SWAP_BLOCK_DAY      = 2          # Wednesday (Mon=0)
    SWAP_ROLLOVER_HOUR  = 22         # 22:00 UTC
    SWAP_PRE_MINUTES    = 15         # block 15 min before
    SWAP_POST_MINUTES   = 15         # block 15 min after

    def is_blackout(
        self,
        now: datetime | None = None,
        currencies: list[str] | None = None,
        check_swap: bool = True,
    ) -> tuple[bool, str]:
        """Return (True, reason) if now is within any blackout window.

        Checks:
        1. Economic calendar events (± pre/post window)
        2. Wednesday 3× swap rollover window (21:45–22:15 UTC)

        Args:
            now:         UTC datetime to check (defaults to datetime.now(utc)).
            currencies:  Optional filter — only check events for these currencies.
            check_swap:  Whether to include the Wednesday swap blackout.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        # ── Wednesday 3× swap rollover window ───────────────────────────────
        if check_swap and now.weekday() == self.SWAP_BLOCK_DAY:
            rollover = now.replace(
                hour=self.SWAP_ROLLOVER_HOUR, minute=0, second=0, microsecond=0
            )
            swap_start = rollover - timedelta(minutes=self.SWAP_PRE_MINUTES)
            swap_end   = rollover + timedelta(minutes=self.SWAP_POST_MINUTES)
            if swap_start <= now <= swap_end:
                return True, "Swap3x-Wed"

        # ── Economic calendar events ─────────────────────────────────────────
        for event in self._events:
            try:
                if currencies:
                    ev_currencies = event.get("currencies", [])
                    if ev_currencies and not any(c in ev_currencies for c in currencies):
                        continue

                ev_dt = datetime.fromisoformat(
                    event["datetime"].replace("Z", "+00:00")
                )
                window_start = ev_dt - timedelta(minutes=PRE_EVENT_MIN)
                window_end   = ev_dt + timedelta(minutes=POST_EVENT_MIN)

                if window_start <= now <= window_end:
                    return True, event["name"]

            except (KeyError, ValueError) as _parse_err:
                logger.warning("NewsFilter: skipping malformed event %s — %s", event, _parse_err)
                continue

        return False, ""

    def next_event(
        self,
        now: datetime | None = None,
        hours_ahead: int = 24,
    ) -> dict | None:
        """Return the next event within `hours_ahead` hours (UTC), or None."""
        if now is None:
            now = datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        horizon = now + timedelta(hours=hours_ahead)
        upcoming = []
        for event in self._events:
            try:
                ev_dt = datetime.fromisoformat(
                    event["datetime"].replace("Z", "+00:00")
                )
                if now < ev_dt <= horizon:
                    upcoming.append((ev_dt, event))
            except (KeyError, ValueError) as _parse_err:
                logger.warning("NewsFilter: skipping malformed event %s — %s", event, _parse_err)
                continue

        if not upcoming:
            return None
        upcoming.sort(key=lambda x: x[0])
        return upcoming[0][1]

    def reload(self) -> None:
        """Reload events: file (if exists) overrides built-in recurring.

        File events take priority: any built-in event whose name matches a
        file event within ±2 days is suppressed to avoid duplicates when the
        calendar file covers a year accurately.
        """
        file_events: list[dict] = []

        # 1. Override file
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    file_events.extend(data)
                    log.debug(f"NewsFilter: loaded {len(data)} events from {self._path}")
            except Exception as exc:
                log.warning(f"NewsFilter: calendar file load error: {exc}")

        # 2. Built-in recurring — skip any year/name already covered by file
        file_year_names: set[tuple[int, str]] = set()
        for ev in file_events:
            try:
                yr = datetime.fromisoformat(ev["datetime"].replace("Z", "+00:00")).year
                file_year_names.add((yr, ev["name"]))
            except Exception:
                pass

        now_year = datetime.now(timezone.utc).year
        builtin: list[dict] = []
        for yr in (now_year, now_year + 1):
            for ev in _recurring_events(yr):
                if (yr, ev["name"]) not in file_year_names:
                    builtin.append(ev)

        self._events = file_events + builtin
        log.info(f"NewsFilter: {len(file_events)} file + {len(builtin)} builtin = {len(self._events)} total events")

    def save_calendar(self) -> None:
        """Persist current in-memory events to the calendar JSON file.
        Useful for bootstrapping an editable calendar from the built-in schedule.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._events, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info(f"NewsFilter: calendar saved to {self._path} ({len(self._events)} events)")

    @property
    def event_count(self) -> int:
        return len(self._events)
