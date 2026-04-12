"""Streamlit multi-symbol ATS monitoring dashboard.

Run: streamlit run dashboard/app.py

Auto-refreshes every 5 seconds by calling st.rerun().
Reads live_state.json written by Python LiveStateWriter (signal_server.py).
"""
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
import plotly.graph_objects as go

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dashboard.state_reader import read_state, read_worker_status, tail_log
from config import LIVE_STATE_PATH

st.set_page_config(
    page_title="ATS Monitor",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Session state: keep equity history across reruns
# ---------------------------------------------------------------------------
if "equity_history" not in st.session_state:
    st.session_state.equity_history = deque(maxlen=500)


def _update_equity_history(equity: float):
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    st.session_state.equity_history.append((now, equity))


# ---------------------------------------------------------------------------
# Read state
# ---------------------------------------------------------------------------
state = read_state()
worker_status = read_worker_status()   # {symbol: "training"|"live"|"waiting"|"error"}
if state.equity > 0:
    _update_equity_history(state.equity)

# Merge all known symbols: from live_state + from worker_status
all_known_symbols = sorted(set(state.symbols) | set(worker_status))

# ---------------------------------------------------------------------------
# Sidebar — System status & account
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("ATS Monitor")

    # System badge
    if state.is_killed:
        st.error("SYSTEM: KILLED")
    elif state.is_alive:
        st.success("SYSTEM: LIVE")
    else:
        st.warning("SYSTEM: OFFLINE")

    st.divider()
    st.subheader("Account")
    col1, col2 = st.columns(2)
    col1.metric("Equity", f"${state.equity:,.2f}")
    col2.metric("Balance", f"${state.balance:,.2f}")

    session_pnl = state.equity - state.balance
    pnl_delta = f"+${session_pnl:,.2f}" if session_pnl >= 0 else f"-${abs(session_pnl):,.2f}"
    st.metric("Session P&L", pnl_delta)

    # Drawdown gauge
    dd = state.drawdown_pct
    bar_color = "green" if dd < 5 else ("orange" if dd < 10 else "red")
    fig_gauge = go.Figure(go.Indicator(
        mode="gauge+number",
        value=dd,
        title={"text": "Drawdown %"},
        gauge={
            "axis": {"range": [0, 20]},
            "bar": {"color": bar_color},
            "steps": [
                {"range": [0, 5],   "color": "darkgreen"},
                {"range": [5, 10],  "color": "darkorange"},
                {"range": [10, 20], "color": "darkred"},
            ],
            "threshold": {"line": {"color": "red", "width": 2}, "value": 15},
        },
    ))
    fig_gauge.update_layout(height=200, margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig_gauge, use_container_width=True)

    st.divider()
    st.caption(f"Signals received: **{state.signal_count}**")
    hb = state.last_heartbeat
    if hb:
        st.caption(f"Last heartbeat: {hb[:19].replace('T', ' ')} UTC")
    st.caption(f"Refreshed: {datetime.now().strftime('%H:%M:%S')}")
    if not LIVE_STATE_PATH.exists():
        st.warning("live_state.json not found — start the signal server.")

# ---------------------------------------------------------------------------
# Main — tabs per symbol + System tab
# ---------------------------------------------------------------------------
_STATUS_BADGE = {
    "training":   " ⏳",
    "retraining": " 🔄",
    "live":       " 🟢",
    "waiting":    " ⚪",
    "error":      " 🔴",
}

symbol_list = all_known_symbols
tab_labels = [
    sym + _STATUS_BADGE.get(worker_status.get(sym, ""), "")
    for sym in symbol_list
] + ["System"]

if not tab_labels:
    tab_labels = ["System"]

tabs = st.tabs(tab_labels)

# Read log once for all symbol tabs (avoids N file reads per refresh)
all_log_lines = tail_log(100)

# Symbol tabs
for tab, sym in zip(tabs[: len(symbol_list)], symbol_list):
    sym_state = state.symbols.get(sym)
    wstatus   = worker_status.get(sym, "")
    with tab:
        # --- Training in progress (first-time bootstrap) ---
        if wstatus == "training":
            st.warning(f"**{sym}** — Model is being trained automatically, please wait...")
            st.markdown(
                "Training PPO on 50 000 bars × 200 000 timesteps (~3 min on CPU)\n\n"
                "The tab will update automatically when training completes."
            )
            # Show recent log lines related to this symbol
            train_logs = [ln for ln in all_log_lines if sym in ln or "train" in ln.lower()][-15:]
            if train_logs:
                st.code("\n".join(train_logs), language=None)
            continue

        # --- Auto-retraining in background (model search) ---
        if wstatus == "retraining":
            st.info(f"**{sym}** 🔄 — AutoRetrainer is searching for a better model in the background.\n\nLive trading continues with the current model uninterrupted.")
            retrain_logs = [ln for ln in all_log_lines if sym in ln and any(k in ln.lower() for k in ("retrain", "sharpe", "candidate", "accepted", "rejected"))][-20:]
            if retrain_logs:
                st.code("\n".join(retrain_logs), language=None)

        if wstatus == "error":
            st.error(f"**{sym}** — Worker encountered an error. Check System tab log.")
            continue

        if sym_state is None:
            st.info(f"**{sym}** — Waiting for first tick data...")
            continue

        # Row 1 — key metrics
        c1, c2, c3, c4 = st.columns(4)

        # Regime chip
        regime_color = {"TREND": ":green", "RANGE": ":orange"}.get(sym_state.regime_str, ":gray")
        c1.metric("Regime", sym_state.regime_str)

        # Position chip
        pos_color = {"LONG": "green", "SHORT": "red", "FLAT": "gray"}
        c2.metric("Position", sym_state.position_str)

        c3.metric("Kelly f*", f"{sym_state.kelly_f * 100:.2f}%")

        dd_delta = None
        c4.metric("Drawdown", f"{sym_state.drawdown_pct:.2f}%")

        st.divider()

        # Row 2 — equity curve (session history)
        if len(st.session_state.equity_history) >= 2:
            times, equities = zip(*st.session_state.equity_history)
            fig_eq = go.Figure()
            fig_eq.add_trace(go.Scatter(
                x=list(times), y=list(equities),
                mode="lines", name="Equity",
                line=dict(color="#00cc44", width=2),
            ))
            fig_eq.update_layout(
                title=f"{sym} — Session Equity Curve",
                xaxis_title="Time",
                yaxis_title="Equity ($)",
                height=280,
                margin=dict(l=10, r=10, t=40, b=10),
                template="plotly_dark",
            )
            st.plotly_chart(fig_eq, use_container_width=True)
        else:
            st.info("Equity curve will build up as the session progresses.")

        st.divider()

        # Row 3 — position details
        if sym_state.position != 0:
            st.subheader("Open Position")
            dc1, dc2, dc3 = st.columns(3)
            dc1.metric("Entry Price", f"{sym_state.entry_price:.5f}")
            pnl = sym_state.unrealized_pnl
            dc2.metric(
                "Unrealized P&L",
                f"${pnl:+.2f}",
                delta=f"{pnl:+.2f}",
                delta_color="normal",
            )
            dc3.metric("Symbol", sym)
        else:
            st.info("No open position.")

        st.divider()

        # Row 4 — Last signal details
        if sym_state.last_signal:
            st.subheader("Last Signal")
            sig = sym_state.last_signal
            sig_df_rows = {k: v for k, v in sig.items() if v is not None}
            st.json(sig_df_rows, expanded=False)

        # Row 5 — Recent log lines relevant to this symbol
        sym_logs = [ln for ln in all_log_lines if sym in ln][-20:]
        if sym_logs:
            st.subheader("Recent Log (this symbol)")
            st.code("\n".join(sym_logs), language=None)

# System tab (always last)
with tabs[-1]:
    st.subheader("System Status")

    col_a, col_b = st.columns(2)
    with col_a:
        st.json(state.system, expanded=True)
    with col_b:
        st.json(state.account, expanded=True)

    st.divider()
    st.subheader("Log Tail (last 50 lines)")
    log_lines = tail_log(50)
    st.code("\n".join(log_lines), language=None)

# ---------------------------------------------------------------------------
# Auto-refresh every 5 seconds
# ---------------------------------------------------------------------------
time.sleep(5)
st.rerun()
