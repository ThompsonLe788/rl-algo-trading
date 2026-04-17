#!/usr/bin/env bash
# Sync MQL5 source files from project → correct MT5 subdirectories.
# Run this after every edit to prevent MT5 from reverting to stale copies.
#
# Usage:  bash sync_mt5.sh

MT5_ROOT="C:/Users/hungl/AppData/Roaming/MetaQuotes/Terminal/D0E8209F77C8CF37AD8BF550E51FF075/MQL5"
SRC="d:/xau_ats/mt5_bridge"

# EA → Experts/
EA_FILES=("XauDayTrader.mq5")

# Indicators → Indicators/
IND_FILES=("ATS_Panel.mq5" "ATS_StrategyView.mq5" "ATS_SMC.mq5")

echo "=== Syncing MQL5 files to MT5 ==="

for f in "${EA_FILES[@]}"; do
    cp "$SRC/$f" "$MT5_ROOT/Experts/$f" && echo "  EA  → Experts/$f" || echo "  FAIL: $f"
done

for f in "${IND_FILES[@]}"; do
    cp "$SRC/$f" "$MT5_ROOT/Indicators/$f" && echo "  IND → Indicators/$f" || echo "  FAIL: $f"
done

echo ""
echo "Done. Recompile in MetaEditor: open each file → Ctrl+Shift+B"
