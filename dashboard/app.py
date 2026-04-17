"""Streamlit professional ATS monitoring dashboard.

Run: streamlit run dashboard/app.py

Uses @st.fragment(run_every=5) for smooth, flicker-free auto-refresh.
Reads live_state.json written by LiveStateWriter (signal_server.py)
and worker_status.json written by SymbolWorker (multi_runner.py).
"""
from collections import deque
from datetime import datetime, timedelta, timezone

import plotly.graph_objects as go
import streamlit as st

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import EOD_HOUR_GMT, LIVE_STATE_PATH, MAX_HOLD_BARS
from dashboard.state_reader import read_state, read_worker_status, read_active_charts, tail_log

# ─── Page config (runs once, outside fragment) ────────────────────────────────
st.set_page_config(
    page_title="ATS Monitor",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─── CSS (injected once, outside fragment) ────────────────────────────────────
st.markdown("""
<style>
:root {
    --gh-bg:      #0d1117;
    --gh-surface: #161b22;
    --gh-border:  #30363d;
    --gh-text:    #e6edf3;
    --gh-muted:   #8b949e;
    --gh-green:   #3fb950;
    --gh-red:     #f85149;
    --gh-blue:    #58a6ff;
    --gh-yellow:  #d29922;
    --gh-purple:  #bc8cff;
}

/* --- Header bar --- */
.ats-header {
    display: flex;
    align-items: center;
    gap: 0;
    flex-wrap: wrap;
    background: var(--gh-surface);
    border: 1px solid var(--gh-border);
    border-radius: 8px;
    padding: 8px 16px;
    margin-bottom: 12px;
    font-family: 'Segoe UI', ui-monospace, monospace;
}
.header-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 4px 14px;
    border-right: 1px solid var(--gh-border);
}
.header-item:last-child { border-right: none; }
.header-label {
    font-size: 0.60rem;
    font-weight: 700;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: var(--gh-muted);
    margin-bottom: 2px;
    white-space: nowrap;
}
.header-value {
    font-size: 1.0rem;
    font-weight: 700;
    color: var(--gh-text);
    white-space: nowrap;
}

/* --- Badges --- */
.badge {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 12px;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.03em;
    white-space: nowrap;
}
.badge-live       { background:#161b22; color:#3fb950; border:1px solid #238636; }
.badge-killed     { background:#1c0a0a; color:#f85149; border:1px solid #da3633; }
.badge-offline    { background:#1e1a0e; color:#d29922; border:1px solid #9e6a03; }
.badge-training   { background:#0d1b2a; color:#58a6ff; border:1px solid #1f6feb; }
.badge-retraining { background:#120d1f; color:#bc8cff; border:1px solid #6e40c9; }
.badge-waiting    { background:#1c1c1c; color:#8b949e; border:1px solid #30363d; }
.badge-error      { background:#1c0a0a; color:#f85149; border:1px solid #da3633; }

/* --- Metric card --- */
.metric-card {
    background: var(--gh-surface);
    border: 1px solid var(--gh-border);
    border-radius: 8px;
    padding: 10px 14px;
    text-align: center;
    margin-bottom: 4px;
}
.metric-card .label {
    font-size: 0.60rem;
    font-weight: 700;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: var(--gh-muted);
    margin-bottom: 4px;
}
.metric-card .value {
    font-size: 1.1rem;
    font-weight: 700;
    color: var(--gh-text);
}

/* --- Position card --- */
.pos-card {
    background: var(--gh-surface);
    border: 1px solid var(--gh-border);
    border-left: 4px solid;
    border-radius: 8px;
    padding: 14px 18px;
    margin-top: 4px;
    font-family: 'Segoe UI', ui-monospace, monospace;
}
.pos-long  { border-left-color: #3fb950; }
.pos-short { border-left-color: #f85149; }
.pos-flat  { border-left-color: #8b949e; }
.pos-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
    margin-top: 10px;
}
.pg-label {
    font-size: 0.60rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--gh-muted);
}
.pg-value {
    font-size: 0.95rem;
    font-weight: 700;
    color: var(--gh-text);
    margin-top: 2px;
}
.progress-wrap {
    background: var(--gh-border);
    border-radius: 4px;
    height: 5px;
    margin-top: 3px;
    overflow: hidden;
}
.progress-fill {
    height: 100%;
    border-radius: 4px;
}

/* --- Signal card --- */
.sig-card {
    background: var(--gh-surface);
    border: 1px solid var(--gh-border);
    border-radius: 8px;
    padding: 12px 18px;
    margin-top: 4px;
    display: flex;
    flex-wrap: wrap;
    gap: 18px;
    align-items: center;
    font-family: 'Segoe UI', ui-monospace, monospace;
}
.sf-label {
    font-size: 0.60rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--gh-muted);
}
.sf-value {
    font-size: 0.9rem;
    font-weight: 700;
    color: var(--gh-text);
    margin-top: 2px;
}

/* --- Section header --- */
.sec-hdr {
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.09em;
    text-transform: uppercase;
    color: var(--gh-muted);
    border-bottom: 1px solid var(--gh-border);
    padding-bottom: 5px;
    margin: 14px 0 8px 0;
}

/* --- Banners --- */
.banner {
    border-radius: 8px;
    padding: 12px 16px;
    margin-bottom: 8px;
    font-family: 'Segoe UI', ui-monospace, monospace;
}
.banner-training   { background:#0d1b2a; border:1px solid #1f6feb; }
.banner-retraining { background:#120d1f; border:1px solid #6e40c9; }
.banner-error      { background:#1c0a0a; border:1px solid #da3633; }
.banner-waiting    { background:#1c1c1c; border:1px solid #30363d; }
</style>
""", unsafe_allow_html=True)


# ─── Helper functions (pure, no st.* calls) ───────────────────────────────────

def _hb_age(hb_str: str, now_utc: datetime) -> float:
    if not hb_str:
        return float("inf")
    try:
        hb = datetime.fromisoformat(hb_str)
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        return (now_utc - hb).total_seconds()
    except Exception:
        return float("inf")


def _eod_countdown(now_utc: datetime) -> str:
    eod = now_utc.replace(hour=EOD_HOUR_GMT, minute=0, second=0, microsecond=0)
    if now_utc >= eod:
        eod += timedelta(days=1)
    diff = int((eod - now_utc).total_seconds())
    h, r = divmod(diff, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _plotly_dark(fig: go.Figure, height: int = 240, title: str = "") -> go.Figure:
    kw: dict = dict(
        height=height,
        margin=dict(l=10, r=10, t=32 if title else 12, b=10),
        paper_bgcolor="#0d1117",
        plot_bgcolor="#0d1117",
        font=dict(color="#8b949e", size=11),
        xaxis=dict(showgrid=False, zeroline=False,
                   tickfont=dict(color="#8b949e"), color="#30363d"),
        yaxis=dict(showgrid=True, gridcolor="#1c2129", zeroline=False,
                   tickfont=dict(color="#8b949e"), color="#30363d"),
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color="#8b949e")),
    )
    if title:
        kw["title"] = dict(text=title, font=dict(color="#8b949e", size=12))
    fig.update_layout(**kw)
    return fig


def _equity_chart(history: list) -> "go.Figure | None":
    if len(history) < 2:
        return None
    times, values = zip(*history)
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=list(times), y=list(values),
        mode="lines", name="Equity",
        line=dict(color="#58a6ff", width=2),
        fill="tozeroy", fillcolor="rgba(88,166,255,0.12)",
    ))
    fig.add_hline(y=values[0], line=dict(color="#30363d", dash="dash", width=1))
    return _plotly_dark(fig, height=240, title="Session Equity Curve")


def _pnl_chart(sym: str, history: list) -> "go.Figure | None":
    if len(history) < 2:
        return None
    times, values = zip(*history)
    last   = values[-1]
    color  = "#3fb950" if last >= 0 else "#f85149"
    fill_c = "rgba(63,185,80,0.12)" if last >= 0 else "rgba(248,81,73,0.12)"
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=list(times), y=list(values),
        mode="lines", name="Unrealized P&L",
        line=dict(color=color, width=2),
        fill="tozeroy", fillcolor=fill_c,
    ))
    fig.add_hline(y=0, line=dict(color="#30363d", dash="dash", width=1))
    return _plotly_dark(fig, height=200, title=f"{sym} — Unrealized P&L")


def _z_chart(sym: str, history: list) -> "go.Figure | None":
    if len(history) < 2:
        return None
    times, values = zip(*history)
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=list(times), y=list(values),
        mode="lines", name="Z-Score",
        line=dict(color="#bc8cff", width=2),
    ))
    fig.add_hline(y=2.0,  line=dict(color="#f85149", dash="dash", width=1))
    fig.add_hline(y=-2.0, line=dict(color="#3fb950", dash="dash", width=1))
    fig.add_hline(y=0.0,  line=dict(color="#30363d", dash="dot",  width=1))
    return _plotly_dark(fig, height=200, title=f"{sym} — Z-Score")


# ─── Main dashboard (fragment = smooth partial refresh, no page flicker) ──────

@st.fragment(run_every=2)
def _dashboard() -> None:
    # ── Session state ─────────────────────────────────────────────────────────
    if "equity_history" not in st.session_state:
        st.session_state.equity_history = deque(maxlen=500)
    if "pnl_history" not in st.session_state:
        st.session_state.pnl_history = {}
    if "z_history" not in st.session_state:
        st.session_state.z_history = {}
    if "prev_positions" not in st.session_state:
        st.session_state.prev_positions = {}
    if "entry_times" not in st.session_state:
        st.session_state.entry_times = {}

    # ── Read live state ───────────────────────────────────────────────────────
    state             = read_state()
    worker_status     = read_worker_status()
    active_charts     = read_active_charts()
    # Tab symbols = currently open MT5 charts only (chart open → tab shown, closed → removed).
    # Fall back to live_state + worker_status when ATS_Panel not attached yet.
    tab_symbols       = sorted(active_charts) if active_charts else sorted(set(state.symbols) | set(worker_status))
    all_known_symbols = sorted(set(state.symbols) | set(worker_status))  # System tab history
    now_utc           = datetime.now(timezone.utc)
    ts_str            = now_utc.strftime("%H:%M:%S UTC")

    if state.equity > 0:
        st.session_state.equity_history.append((ts_str, state.equity))

    for sym, sym_state in state.symbols.items():
        if sym not in st.session_state.pnl_history:
            st.session_state.pnl_history[sym] = deque(maxlen=300)
        if sym not in st.session_state.z_history:
            st.session_state.z_history[sym] = deque(maxlen=300)

        st.session_state.pnl_history[sym].append((ts_str, sym_state.unrealized_pnl))
        z_val = sym_state.last_signal.get("z_score", 0.0) if sym_state.last_signal else 0.0
        st.session_state.z_history[sym].append((ts_str, float(z_val)))

        prev = st.session_state.prev_positions.get(sym, 0)
        if sym_state.position != 0 and prev == 0:
            st.session_state.entry_times[sym] = now_utc
        elif sym_state.position == 0:
            st.session_state.entry_times.pop(sym, None)
        st.session_state.prev_positions[sym] = sym_state.position

    # ── Header bar ────────────────────────────────────────────────────────────
    hb_age   = _hb_age(state.last_heartbeat, now_utc)
    hb_color = "#3fb950" if hb_age < 10 else ("#d29922" if hb_age < 30 else "#f85149")
    hb_str   = f"{int(hb_age)}s ago" if hb_age != float("inf") else "N/A"

    session_pnl = state.equity - state.balance
    pnl_color   = "#3fb950" if session_pnl >= 0 else "#f85149"
    pnl_sign    = "+" if session_pnl >= 0 else ""

    dd       = state.drawdown_pct
    dd_color = "#3fb950" if dd < 5 else ("#d29922" if dd < 10 else "#f85149")

    active_count = sum(1 for s in tab_symbols if worker_status.get(s) == "live")

    if state.is_killed:
        badge_html = '<span class="badge badge-killed">⬛ KILLED</span>'
    elif state.is_alive:
        badge_html = '<span class="badge badge-live">● LIVE</span>'
    else:
        badge_html = '<span class="badge badge-offline">⚠ OFFLINE</span>'

    st.markdown(f"""
    <div class="ats-header">
      <div class="header-item" style="border-right:1px solid #30363d;">
        <span style="font-size:1.05rem;font-weight:900;color:#e6edf3;letter-spacing:0.04em;">ATS</span>
        <span style="font-size:0.58rem;color:#8b949e;letter-spacing:0.08em;">MONITOR</span>
      </div>
      <div class="header-item">
        <span class="header-label">Status</span>
        {badge_html}
      </div>
      <div class="header-item">
        <span class="header-label">Equity</span>
        <span class="header-value">${state.equity:,.2f}</span>
      </div>
      <div class="header-item">
        <span class="header-label">Balance</span>
        <span class="header-value">${state.balance:,.2f}</span>
      </div>
      <div class="header-item">
        <span class="header-label">Session P&amp;L</span>
        <span class="header-value" style="color:{pnl_color};">{pnl_sign}${session_pnl:,.2f}</span>
      </div>
      <div class="header-item">
        <span class="header-label">Drawdown</span>
        <span class="header-value" style="color:{dd_color};">{dd:.2f}%</span>
      </div>
      <div class="header-item">
        <span class="header-label">Heartbeat</span>
        <span class="header-value" style="color:{hb_color};">{hb_str}</span>
      </div>
      <div class="header-item">
        <span class="header-label">Signals</span>
        <span class="header-value">{state.signal_count}</span>
      </div>
      <div class="header-item">
        <span class="header-label">Active</span>
        <span class="header-value">{active_count}/{len(tab_symbols)}</span>
      </div>
      <div class="header-item">
        <span class="header-label">EOD</span>
        <span class="header-value" style="color:#d29922;">{_eod_countdown(now_utc)}</span>
      </div>
      <div style="flex:1;"></div>
      <div style="text-align:right;color:#8b949e;font-size:0.68rem;padding:0 8px;">
        {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Tabs ──────────────────────────────────────────────────────────────────
    _STATUS_BADGE = {
        "training":   " ⏳",
        "retraining": " 🔄",
        "live":       " 🟢",
        "waiting":    " ⚪",
        "error":      " 🔴",
    }
    tab_labels = [
        sym + _STATUS_BADGE.get(worker_status.get(sym, ""), "")
        for sym in tab_symbols
    ] + ["⚙ System"]

    tabs = st.tabs(tab_labels)
    all_log_lines = tail_log(120)

    # ── Per-symbol tabs ───────────────────────────────────────────────────────
    for tab, sym in zip(tabs[: len(tab_symbols)], tab_symbols):
        sym_state = state.symbols.get(sym)
        wstatus   = worker_status.get(sym, "")

        with tab:
            # Training
            if wstatus == "training":
                st.markdown(f"""
                <div class="banner banner-training">
                  <span class="badge badge-training">⏳ TRAINING</span>
                  <span style="margin-left:10px;font-weight:700;color:#e6edf3;">{sym}</span>
                  <p style="color:#8b949e;margin:8px 0 0 0;font-size:0.85rem;">
                    Auto-training PPO on 50,000 bars × 200,000 timesteps (~3 min on CPU).
                    This tab updates automatically when training completes.
                  </p>
                </div>""", unsafe_allow_html=True)
                train_logs = [ln for ln in all_log_lines
                              if sym in ln or "train" in ln.lower()][-15:]
                if train_logs:
                    with st.expander("Training log", expanded=True):
                        st.code("\n".join(train_logs), language=None)
                continue

            # Retraining banner (non-blocking)
            if wstatus == "retraining":
                st.markdown(f"""
                <div class="banner banner-retraining">
                  <span class="badge badge-retraining">🔄 RETRAINING</span>
                  <span style="margin-left:10px;color:#bc8cff;font-size:0.83rem;">
                    AutoRetrainer searching for a better model — live trading continues uninterrupted.
                  </span>
                </div>""", unsafe_allow_html=True)
                retrain_logs = [ln for ln in all_log_lines
                                if sym in ln and any(k in ln.lower()
                                for k in ("retrain", "sharpe", "candidate",
                                          "accepted", "rejected"))][-20:]
                if retrain_logs:
                    with st.expander("Retrain log", expanded=False):
                        st.code("\n".join(retrain_logs), language=None)

            # Error
            if wstatus == "error":
                st.markdown(f"""
                <div class="banner banner-error">
                  <span class="badge badge-error">🔴 ERROR</span>
                  <span style="margin-left:10px;color:#f85149;">
                    Worker encountered an error — see System tab for full log.
                  </span>
                </div>""", unsafe_allow_html=True)
                err_logs = [ln for ln in all_log_lines if sym in ln][-20:]
                if err_logs:
                    with st.expander("Error log", expanded=True):
                        st.code("\n".join(err_logs), language=None)
                continue

            # Waiting for first data
            if sym_state is None:
                st.markdown(f"""
                <div class="banner banner-waiting">
                  <span class="badge badge-waiting">⚪ WAITING</span>
                  <span style="margin-left:10px;color:#8b949e;">
                    {sym} — awaiting first tick data...
                  </span>
                </div>""", unsafe_allow_html=True)
                continue

            # ── Metric row ────────────────────────────────────────────────────
            pos_color = ("#3fb950" if sym_state.position == 1
                         else "#f85149" if sym_state.position == -1 else "#8b949e")
            reg_color = ("#3fb950" if sym_state.regime_str == "TREND"
                         else "#d29922" if sym_state.regime_str == "RANGE" else "#8b949e")
            pnl_val   = sym_state.unrealized_pnl
            pnl_c     = "#3fb950" if pnl_val >= 0 else "#f85149"
            dd_val    = sym_state.drawdown_pct
            dd_c      = ("#3fb950" if dd_val < 5
                         else "#d29922" if dd_val < 10 else "#f85149")
            kelly_pct = sym_state.kelly_f * 100

            tick_ts = sym_state.timestamp
            if tick_ts:
                try:
                    ts_dt = datetime.fromisoformat(tick_ts)
                    if ts_dt.tzinfo is None:
                        ts_dt = ts_dt.replace(tzinfo=timezone.utc)
                    tick_age = int((now_utc - ts_dt).total_seconds())
                    tick_str = f"{tick_age}s ago"
                except Exception:
                    tick_str = tick_ts[:19].replace("T", " ") + " UTC"
            else:
                tick_str = "—"

            m1, m2, m3, m4, m5, m6 = st.columns(6)
            for col, lbl, val, color in [
                (m1, "Position",  sym_state.position_str, pos_color),
                (m2, "Regime",    sym_state.regime_str,   reg_color),
                (m3, "Kelly f*",  f"{kelly_pct:.2f}%",     "#58a6ff"),
                (m4, "Drawdown",  f"{dd_val:.2f}%",         dd_c),
                (m5, "Unr. P&L",  f"${pnl_val:+.2f}",      pnl_c),
                (m6, "Last Tick", tick_str,                 "#8b949e"),
            ]:
                col.markdown(
                    f'<div class="metric-card"><div class="label">{lbl}</div>'
                    f'<div class="value" style="color:{color};">{val}</div></div>',
                    unsafe_allow_html=True,
                )

            # ── Charts row ────────────────────────────────────────────────────
            ch1, ch2, ch3 = st.columns(3)
            with ch1:
                fig = _equity_chart(list(st.session_state.equity_history))
                if fig:
                    st.plotly_chart(fig, width="stretch", key=f"eq_{sym}")
                else:
                    st.info("Equity curve builds as session progresses.")
            with ch2:
                fig = _pnl_chart(sym, list(st.session_state.pnl_history.get(sym, [])))
                if fig:
                    st.plotly_chart(fig, width="stretch", key=f"pnl_{sym}")
                else:
                    st.info("P&L chart builds with tick data.")
            with ch3:
                fig = _z_chart(sym, list(st.session_state.z_history.get(sym, [])))
                if fig:
                    st.plotly_chart(fig, width="stretch", key=f"z_{sym}")
                else:
                    st.info("Z-score chart builds with signal data.")

            # ── Position card ─────────────────────────────────────────────────
            st.markdown('<div class="sec-hdr">Open Position</div>',
                        unsafe_allow_html=True)

            if sym_state.position != 0:
                sig_data = sym_state.last_signal or {}
                lot      = sig_data.get("lot", 0.0)
                sl       = sig_data.get("sl", 0.0)
                tp       = sig_data.get("tp", 0.0)
                rr       = sig_data.get("rr", 0.0)
                atr_est  = (abs(sym_state.entry_price - sl) / 1.5
                            if sl and sym_state.entry_price else 0.0)

                entry_t  = st.session_state.entry_times.get(sym)
                if entry_t:
                    hold_sec  = int((now_utc - entry_t).total_seconds())
                    hold_bars = hold_sec // 60
                    hold_str  = f"{hold_sec // 60}m {hold_sec % 60}s"
                    hold_pct  = min(100, int(hold_bars / max(1, MAX_HOLD_BARS) * 100))
                else:
                    hold_str, hold_pct = "—", 0

                bar_c   = ("#3fb950" if hold_pct < 70
                           else "#d29922" if hold_pct < 90 else "#f85149")
                pos_cls = "pos-long" if sym_state.position == 1 else "pos-short"
                dir_str = "▲ LONG" if sym_state.position == 1 else "▼ SHORT"

                st.markdown(f"""
                <div class="pos-card {pos_cls}">
                  <div style="display:flex;align-items:center;gap:10px;">
                    <span style="font-size:1.2rem;color:{pos_color};font-weight:900;">{dir_str}</span>
                    <span style="color:#8b949e;font-size:0.78rem;">{sym}</span>
                  </div>
                  <div class="pos-grid">
                    <div><div class="pg-label">Entry Price</div>
                         <div class="pg-value">{sym_state.entry_price:.5f}</div></div>
                    <div><div class="pg-label">Unrealized P&amp;L</div>
                         <div class="pg-value" style="color:{pnl_c};">${pnl_val:+.2f}</div></div>
                    <div><div class="pg-label">Lot Size</div>
                         <div class="pg-value">{lot:.2f}</div></div>
                    <div><div class="pg-label">Risk:Reward</div>
                         <div class="pg-value">{rr:.2f}</div></div>
                    <div><div class="pg-label">Stop Loss</div>
                         <div class="pg-value" style="color:#f85149;">{sl:.5f}</div></div>
                    <div><div class="pg-label">Take Profit</div>
                         <div class="pg-value" style="color:#3fb950;">{tp:.5f}</div></div>
                    <div><div class="pg-label">ATR (est.)</div>
                         <div class="pg-value">{atr_est:.4f}</div></div>
                    <div><div class="pg-label">Hold Time</div>
                         <div class="pg-value">{hold_str}</div></div>
                  </div>
                  <div style="margin-top:10px;">
                    <div style="display:flex;justify-content:space-between;
                                font-size:0.62rem;color:#8b949e;margin-bottom:3px;">
                      <span>Hold limit progress ({MAX_HOLD_BARS} bars)</span>
                      <span style="color:{bar_c};">{hold_pct}%</span>
                    </div>
                    <div class="progress-wrap">
                      <div class="progress-fill" style="width:{hold_pct}%;background:{bar_c};"></div>
                    </div>
                  </div>
                </div>""", unsafe_allow_html=True)
            else:
                st.markdown(
                    '<div style="color:#8b949e;font-size:0.85rem;padding:6px 0;">'
                    'No open position.</div>',
                    unsafe_allow_html=True,
                )

            # ── Last signal card ──────────────────────────────────────────────
            sig_data = sym_state.last_signal
            if sig_data and sig_data.get("side") is not None:
                st.markdown('<div class="sec-hdr">Last Signal</div>',
                            unsafe_allow_html=True)
                side_v  = sig_data.get("side", 0)
                s_icon  = "▲" if side_v == 1 else "▼" if side_v == -1 else "○"
                s_color = ("#3fb950" if side_v == 1
                           else "#f85149" if side_v == -1 else "#8b949e")
                s_label = ("LONG" if side_v == 1
                           else "SHORT" if side_v == -1 else "CLOSE")
                z_v     = float(sig_data.get("z_score", 0.0))
                z_c     = ("#f85149" if abs(z_v) > 2
                           else "#d29922" if abs(z_v) > 1 else "#8b949e")
                wp      = float(sig_data.get("win_prob", 0.0))
                rr_v    = float(sig_data.get("rr", 0.0))
                reg_v   = {1: "TREND", 0: "RANGE", -1: "—"}.get(
                    sig_data.get("regime", -1), "—")
                sig_ts  = (sig_data.get("timestamp", "")[:19].replace("T", " ") + " UTC"
                           if sig_data.get("timestamp") else "—")

                st.markdown(f"""
                <div class="sig-card">
                  <div style="text-align:center;">
                    <div style="font-size:1.8rem;color:{s_color};">{s_icon}</div>
                    <div style="font-size:0.9rem;font-weight:800;color:{s_color};">{s_label}</div>
                    <div style="font-size:0.65rem;color:#8b949e;white-space:nowrap;">{sig_ts} UTC</div>
                  </div>
                  <div><div class="sf-label">Price</div>
                       <div class="sf-value">{sig_data.get('price', 0):.2f}</div></div>
                  <div><div class="sf-label">Z-Score</div>
                       <div class="sf-value" style="color:{z_c};">{z_v:+.3f}</div></div>
                  <div><div class="sf-label">Win Prob</div>
                       <div class="sf-value">{wp:.1%}</div></div>
                  <div><div class="sf-label">R:R</div>
                       <div class="sf-value">{rr_v:.2f}</div></div>
                  <div><div class="sf-label">Regime</div>
                       <div class="sf-value">{reg_v}</div></div>
                  <div><div class="sf-label">Stop Loss</div>
                       <div class="sf-value" style="color:#f85149;">{sig_data.get('sl', 0):.2f}</div></div>
                  <div><div class="sf-label">Take Profit</div>
                       <div class="sf-value" style="color:#3fb950;">{sig_data.get('tp', 0):.2f}</div></div>
                  <div><div class="sf-label">Lot</div>
                       <div class="sf-value">{sig_data.get('lot', 0):.2f}</div></div>
                </div>""", unsafe_allow_html=True)

            # ── Log expander ──────────────────────────────────────────────────
            sym_logs = [ln for ln in all_log_lines if sym in ln][-25:]
            if sym_logs:
                with st.expander("Recent log", expanded=False):
                    st.code("\n".join(sym_logs), language=None)

    # ── System tab ────────────────────────────────────────────────────────────
    with tabs[-1]:
        st.markdown('<div class="sec-hdr">Worker Status</div>',
                    unsafe_allow_html=True)

        if all_known_symbols:
            grid_cols = st.columns(min(len(all_known_symbols), 5))
            for i, sym in enumerate(all_known_symbols):
                wst = worker_status.get(sym, "unknown")
                badge_cls = {
                    "live": "badge-live", "training": "badge-training",
                    "retraining": "badge-retraining", "waiting": "badge-waiting",
                    "error": "badge-error",
                }.get(wst, "badge-waiting")
                icon = {
                    "live": "●", "training": "⏳", "retraining": "🔄",
                    "waiting": "○", "error": "✕",
                }.get(wst, "○")
                grid_cols[i % 5].markdown(
                    f'<div class="metric-card">'
                    f'<div class="label">{sym}</div>'
                    f'<div style="margin-top:5px;">'
                    f'<span class="badge {badge_cls}">{icon} {wst.upper()}</span>'
                    f'</div></div>',
                    unsafe_allow_html=True,
                )
        else:
            st.markdown(
                '<div class="banner banner-waiting">'
                '<span class="badge badge-waiting">⚪ WAITING</span>'
                '<span style="margin-left:10px;color:#8b949e;">'
                'No active workers — run <code>python start.py</code> '
                'and open an MT5 chart.'
                '</span></div>',
                unsafe_allow_html=True,
            )

        st.markdown('<div class="sec-hdr">Account Overview</div>',
                    unsafe_allow_html=True)
        g1, g2 = st.columns([1, 1])
        with g1:
            dd_v  = state.drawdown_pct
            g_clr = "green" if dd_v < 5 else ("orange" if dd_v < 10 else "red")
            fig_g = go.Figure(go.Indicator(
                mode="gauge+number",
                value=dd_v,
                title={"text": "Account Drawdown %",
                       "font": {"color": "#e6edf3", "size": 14}},
                number={"font": {"color": "#e6edf3", "size": 36}},
                gauge={
                    "axis": {"range": [0, 20],
                             "tickfont": {"color": "#8b949e"}},
                    "bar": {"color": g_clr},
                    "bgcolor": "#161b22",
                    "bordercolor": "#30363d",
                    "steps": [
                        {"range": [0,  5],  "color": "#1a3a1a"},
                        {"range": [5,  10], "color": "#3a2a1a"},
                        {"range": [10, 20], "color": "#3a1a1a"},
                    ],
                    "threshold": {"line": {"color": "#f85149", "width": 2},
                                  "value": 15},
                },
            ))
            fig_g.update_layout(
                height=260,
                margin=dict(l=20, r=20, t=50, b=20),
                paper_bgcolor="#0d1117",
                font=dict(color="#e6edf3"),
            )
            st.plotly_chart(fig_g, width="stretch")
        with g2:
            acct = state.account
            eq_v  = acct.get("equity", 0)
            bal_v = acct.get("balance", 0)
            dd_a  = acct.get("drawdown_pct", 0)
            pnl_v = eq_v - bal_v

            pnl_c = "#3fb950" if pnl_v >= 0 else "#f85149"
            dd_c2 = "#3fb950" if dd_a < 5 else ("#d29922" if dd_a < 10 else "#f85149")

            st.markdown(f"""
<div style="display:flex; flex-direction:column; gap:10px; padding:4px 0;">
  <div style="display:flex; gap:10px;">
    <div style="flex:1; background:#161b22; border:1px solid #30363d;
                border-radius:8px; padding:14px;">
      <div style="font-size:0.62rem; color:#8b949e; text-transform:uppercase;
                  letter-spacing:0.08em; font-weight:700;">Equity</div>
      <div style="font-size:1.5rem; font-weight:700; color:#e6edf3;
                  margin-top:4px;">${eq_v:,.2f}</div>
    </div>
    <div style="flex:1; background:#161b22; border:1px solid #30363d;
                border-radius:8px; padding:14px;">
      <div style="font-size:0.62rem; color:#8b949e; text-transform:uppercase;
                  letter-spacing:0.08em; font-weight:700;">Balance</div>
      <div style="font-size:1.5rem; font-weight:700; color:#e6edf3;
                  margin-top:4px;">${bal_v:,.2f}</div>
    </div>
  </div>
  <div style="display:flex; gap:10px;">
    <div style="flex:1; background:#161b22; border:1px solid #30363d;
                border-radius:8px; padding:14px;">
      <div style="font-size:0.62rem; color:#8b949e; text-transform:uppercase;
                  letter-spacing:0.08em; font-weight:700;">Session P&amp;L</div>
      <div style="font-size:1.3rem; font-weight:700; color:{pnl_c};
                  margin-top:4px;">{'+' if pnl_v>=0 else ''}{pnl_v:,.2f}</div>
    </div>
    <div style="flex:1; background:#161b22; border:1px solid #30363d;
                border-radius:8px; padding:14px;">
      <div style="font-size:0.62rem; color:#8b949e; text-transform:uppercase;
                  letter-spacing:0.08em; font-weight:700;">Drawdown</div>
      <div style="font-size:1.3rem; font-weight:700; color:{dd_c2};
                  margin-top:4px;">{dd_a:.2f}%</div>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

        st.markdown('<div class="sec-hdr">System State</div>',
                    unsafe_allow_html=True)
        st.json(state.system, expanded=True)

        if not LIVE_STATE_PATH.exists():
            st.warning("live_state.json not found — "
                       "run `python start.py` to start the signal server.")

        st.markdown('<div class="sec-hdr">Log Tail — last 50 lines</div>',
                    unsafe_allow_html=True)
        st.code("\n".join(tail_log(50)), language=None)


# ─── Entry point ─────────────────────────────────────────────────────────────
_dashboard()
