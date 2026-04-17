//+------------------------------------------------------------------+
//| ATS_Panel.mq5 — Comprehensive Live Monitoring Panel v2.2        |
//|                                                                  |
//| Sections (top-left, 325 px wide):                                |
//|   MARKET    : Chart TF, ATS TF, Session, Spread                  |
//|   POSITION  : Status, Regime, Entry, P&L, Sym DD, Acct DD        |
//|   SIGNALS   : Rolling table — last 5 (Dir, Price, Win%, R/R, t)  |
//|   RISK      : Kelly f*, Equity, Balance                           |
//|   AI MODEL  : Version, Status, Last retrain, WinRate, Sharpe     |
//|   SYSTEM    : Signal count, Kill reason, Heartbeat, Updated      |
//|                                                                  |
//| Data source : ats_live_state.json  (Python LiveStateWriter)      |
//+------------------------------------------------------------------+
#property copyright "ATS"
#property version   "2.20"
#property indicator_chart_window
#property indicator_plots 0

//--- Inputs
input int    PanelX          = 5;               // Panel X — pixels from left edge
input int    PanelY          = 5;               // Panel Y — pixels from top edge
input color  ClrBg           = C'18,18,30';     // Background
input color  ClrBorder       = C'55,55,90';     // Border / dividers
input color  ClrSection      = C'110,170,255';  // Section header text
input color  ClrLabel        = C'155,155,175';  // Label text
input color  ClrValuePos     = clrLimeGreen;    // Positive / Long / Good
input color  ClrValueNeg     = clrTomato;       // Negative / Short / Alert
input color  ClrValueNeutral = clrWhite;        // Neutral values
input color  ClrTimestamp    = C'80,180,255';   // Timestamp accent
input int    FontSz          = 9;               // Font size
input string FontName        = "Consolas";      // Font
input int    RefreshSec      = 2;               // JSON refresh interval (s)

#define OBJ_PREFIX   "ATSP_"
#define JSON_FILE    "ats_live_state.json"
#define SIG_ROWS     5        // rows in signals history table
#define PANEL_W      330      // panel width px
#define LH           13       // normal row height px
#define LH_DIV        8       // divider row height px
#define LH_SIG       12       // signals-table row height px

//--- Parsed state
// NOTE: MQL5 forbids arrays of structs-with-strings inside another struct.
// History fields are stored as parallel global arrays instead.

struct PanelData
{
   // _account
   double equity;
   double balance;
   double acct_drawdown;
   // _system
   bool   system_alive;
   bool   system_killed;
   int    signal_count;
   long   unix_time;
   string kill_reason;
   string last_heartbeat;
   // symbol
   int    position;
   double entry_price;
   double unrealized_pnl;
   int    regime;
   double kelly_f;
   double sym_drawdown;
   // last_signal (full detail for current / most-recent signal)
   int    sig_side;
   double sig_sl;
   double sig_tp;
   double sig_lot;
   double sig_win_prob;
   double sig_z_score;
   double sig_rr;
   string sig_timestamp;
   // model
   string model_version;
   bool   model_is_training;
   string model_last_retrain;
   string model_retrain_reason;
   double model_win_rate;
   double model_sharpe;
   int    model_total_trades;
};

PanelData g_pd;
datetime  g_lastRefresh = 0;

// Signals history — parallel arrays (MQL5 restriction: no string inside struct arrays)
int    g_hist_side [SIG_ROWS];
double g_hist_price[SIG_ROWS];
double g_hist_wp   [SIG_ROWS];
double g_hist_lot  [SIG_ROWS];
double g_hist_rr   [SIG_ROWS];
string g_hist_ts   [SIG_ROWS];
int    g_hist_count = 0;

//+------------------------------------------------------------------+
//| Register / unregister this chart with Python multi-runner        |
//+------------------------------------------------------------------+
void _RegisterChart(bool active)
{
   string fname = "ats_chart_" + _Symbol + ".txt";
   int fh = FileOpen(fname, FILE_WRITE | FILE_TXT | FILE_COMMON);
   if(fh == INVALID_HANDLE) return;
   FileWriteString(fh, active ? "1" : "0");
   FileClose(fh);
}

//+------------------------------------------------------------------+
int OnInit()
{
   _RegisterChart(true);
   EventSetTimer(RefreshSec);
   _LoadState();
   _DrawPanel();
   return INIT_SUCCEEDED;
}
void OnDeinit(const int reason)
{
   _RegisterChart(false);
   EventKillTimer();
   _DeletePanel();
}
int OnCalculate(const int rates_total, const int prev_calculated,
                const datetime &time[], const double &open[],
                const double &high[], const double &low[],
                const double &close[], const long &vol[],
                const long &spread[], const int &real_volume[])
{ return rates_total; }
void OnTimer()
{
   if(TimeLocal() - g_lastRefresh >= RefreshSec)
   { _LoadState(); _DrawPanel(); g_lastRefresh = TimeLocal(); }
}

//===================================================================
//  JSON helpers — lightweight, no external library
//===================================================================
double _JsonDouble(const string &j, const string &key)
{
   string s = "\"" + key + "\":";
   int p = StringFind(j, s);
   if(p < 0) return 0.0;
   p += StringLen(s);
   while(p < StringLen(j) && StringGetCharacter(j, p) == ' ') p++;
   string v = "";
   while(p < StringLen(j))
   {
      ushort c = StringGetCharacter(j, p);
      if(c == ',' || c == '}' || c == ' ' || c == '\n' || c == '\r') break;
      v += CharToString((uchar)c); p++;
   }
   return StringToDouble(v);
}
bool _JsonBool(const string &j, const string &key)
{
   string s = "\"" + key + "\":";
   int p = StringFind(j, s);
   if(p < 0) return false;
   p += StringLen(s);
   while(p < StringLen(j) && StringGetCharacter(j, p) == ' ') p++;
   return StringGetCharacter(j, p) == 't';
}
long _JsonInt(const string &j, const string &key)  { return (long)_JsonDouble(j, key); }

string _JsonObject(const string &j, const string &key)
{
   string s = "\"" + key + "\":";
   int p = StringFind(j, s);
   if(p < 0) return "";
   p += StringLen(s);
   while(p < StringLen(j) && StringGetCharacter(j, p) == ' ') p++;
   if(StringGetCharacter(j, p) != '{') return "";
   int depth = 0, start = p;
   while(p < StringLen(j))
   {
      ushort c = StringGetCharacter(j, p);
      if(c == '{') depth++;
      else if(c == '}') { depth--; if(depth == 0) { p++; break; } }
      p++;
   }
   return StringSubstr(j, start, p - start);
}
string _JsonString(const string &j, const string &key)
{
   string s = "\"" + key + "\":";
   int p = StringFind(j, s);
   if(p < 0) return "";
   p += StringLen(s);
   while(p < StringLen(j) && StringGetCharacter(j, p) == ' ') p++;
   if(StringGetCharacter(j, p) != '"') return "";
   p++;
   string v = "";
   while(p < StringLen(j))
   {
      ushort c = StringGetCharacter(j, p);
      if(c == '"') break;
      if(c == '\\') { p++; if(p < StringLen(j)) v += CharToString((uchar)StringGetCharacter(j,p)); p++; continue; }
      v += CharToString((uchar)c); p++;
   }
   return v;
}

// Extract the Nth object { } from a JSON array  "key": [{...},{...},...]
// Returns "" when index is out of range.
string _JsonArrayObj(const string &j, const string &key, int idx)
{
   string s = "\"" + key + "\":";
   int p = StringFind(j, s);
   if(p < 0) return "";
   p += StringLen(s);
   while(p < StringLen(j) && StringGetCharacter(j, p) == ' ') p++;
   if(StringGetCharacter(j, p) != '[') return "";
   p++;   // skip '['
   int count = 0;
   while(p < StringLen(j))
   {
      while(p < StringLen(j) && (StringGetCharacter(j,p) == ' ' || StringGetCharacter(j,p) == '\n'
            || StringGetCharacter(j,p) == ',' || StringGetCharacter(j,p) == '\r')) p++;
      if(StringGetCharacter(j, p) != '{') break;   // end of array or malformed
      // scan to matching '}'
      int depth = 0, start = p;
      while(p < StringLen(j))
      {
         ushort c = StringGetCharacter(j, p);
         if(c == '{') depth++;
         else if(c == '}') { depth--; if(depth == 0) { p++; break; } }
         p++;
      }
      if(count == idx) return StringSubstr(j, start, p - start);
      count++;
   }
   return "";
}

//===================================================================
//  Utility helpers
//===================================================================
string _IsoTime(const string &iso) { return StringLen(iso) >= 19 ? StringSubstr(iso, 11, 8) : "---"; }
string _IsoDate(const string &iso) { return StringLen(iso) >= 10 ? StringSubstr(iso,  0,10) : "---"; }
string _Trunc(const string &s, int n) { return StringLen(s) <= n ? s : StringSubstr(s, 0, n-2) + ".."; }

string _Session()
{
   MqlDateTime dt; TimeToStruct(TimeGMT(), dt); int h = dt.hour;
   if(h >= 7 && h < 13)  return "LONDON";
   if(h >= 13 && h < 16) return "LON+NY";
   if(h >= 16 && h < 22) return "NEW YORK";
   if(h >= 23 || h < 7)  return "TOKYO";
   return "CLOSED";
}
color _SessionClr()
{
   string s = _Session();
   if(s == "LON+NY")   return clrGold;
   if(s == "LONDON")   return clrCyan;
   if(s == "NEW YORK") return clrLightSkyBlue;
   if(s == "TOKYO")    return clrOrchid;
   return clrDimGray;
}
string _TFStr()
{
   switch(_Period)
   {
      case PERIOD_M1:  return "M1";  case PERIOD_M5:  return "M5";
      case PERIOD_M15: return "M15"; case PERIOD_M30: return "M30";
      case PERIOD_H1:  return "H1";  case PERIOD_H4:  return "H4";
      case PERIOD_D1:  return "D1";  case PERIOD_W1:  return "W1";
      case PERIOD_MN1: return "MN";  default: return "???";
   }
}
string _SideStr(int s) { return s == 1 ? "BUY" : (s == -1 ? "SELL" : (s == 0 ? "CLOSE" : "---")); }
color  _SideClr(int s) { return s == 1 ? ClrValuePos : (s == -1 ? ClrValueNeg : clrDimGray); }

// Fixed-width field: right-justify val in a field of width w chars
string _FW(const string &val, int w)
{
   string v = val;
   while(StringLen(v) < w) v = " " + v;
   return v;
}

//===================================================================
//  Load & parse ats_live_state.json
//===================================================================
void _LoadState()
{
   int h = FileOpen(JSON_FILE, FILE_READ | FILE_BIN | FILE_COMMON);
   if(h == INVALID_HANDLE) return;
   int sz = (int)FileSize(h);
   if(sz < 5) { FileClose(h); return; }
   uchar buf[]; ArrayResize(buf, sz);
   FileReadArray(h, buf, 0, sz);
   FileClose(h);
   string content = CharArrayToString(buf, 0, sz, CP_UTF8);
   if(StringLen(content) < 5) return;

   // _account
   string acct = _JsonObject(content, "_account");
   if(StringLen(acct) > 0)
   {
      g_pd.equity        = _JsonDouble(acct, "equity");
      g_pd.balance       = _JsonDouble(acct, "balance");
      g_pd.acct_drawdown = _JsonDouble(acct, "drawdown_pct");
   }
   // _system
   string sys = _JsonObject(content, "_system");
   if(StringLen(sys) > 0)
   {
      g_pd.system_alive   = _JsonBool(sys, "alive");
      g_pd.system_killed  = _JsonBool(sys, "killed");
      g_pd.signal_count   = (int)_JsonInt(sys, "signal_count");
      g_pd.unix_time      = _JsonInt(sys, "unix_time");
      g_pd.kill_reason    = _JsonString(sys, "kill_reason");
      g_pd.last_heartbeat = _JsonString(sys, "last_heartbeat");
   }
   // symbol section
   string sym = _JsonObject(content, _Symbol);
   if(StringLen(sym) > 0)
   {
      g_pd.position       = (int)_JsonInt(sym, "position");
      g_pd.entry_price    = _JsonDouble(sym, "entry_price");
      g_pd.unrealized_pnl = _JsonDouble(sym, "unrealized_pnl");
      g_pd.regime         = (int)_JsonInt(sym, "regime");
      g_pd.kelly_f        = _JsonDouble(sym, "kelly_f");
      g_pd.sym_drawdown   = _JsonDouble(sym, "drawdown_pct");
      // model fields
      g_pd.model_version        = _JsonString(sym, "model_version");
      g_pd.model_is_training    = _JsonBool(sym,   "is_training");
      g_pd.model_last_retrain   = _JsonString(sym, "last_retrain_time");
      g_pd.model_retrain_reason = _JsonString(sym, "last_retrain_reason");
      g_pd.model_win_rate       = _JsonDouble(sym, "win_rate");
      g_pd.model_sharpe         = _JsonDouble(sym, "model_sharpe");
      g_pd.model_total_trades   = (int)_JsonInt(sym, "total_trades");
      // last_signal (full detail)
      string lsig = _JsonObject(sym, "last_signal");
      if(StringLen(lsig) > 0)
      {
         g_pd.sig_side      = (int)_JsonInt(lsig, "side");
         g_pd.sig_sl        = _JsonDouble(lsig, "sl");
         g_pd.sig_tp        = _JsonDouble(lsig, "tp");
         g_pd.sig_lot       = _JsonDouble(lsig, "lot");
         g_pd.sig_win_prob  = _JsonDouble(lsig, "win_prob");
         g_pd.sig_z_score   = _JsonDouble(lsig, "z_score");
         g_pd.sig_rr        = _JsonDouble(lsig, "rr");
         g_pd.sig_timestamp = _JsonString(lsig, "timestamp");
      }
      // signals_history array (newest first) → parallel global arrays
      g_hist_count = 0;
      for(int i = 0; i < SIG_ROWS; i++)
      {
         string item = _JsonArrayObj(sym, "signals_history", i);
         if(StringLen(item) == 0) break;
         g_hist_side [i] = (int)_JsonInt(item, "s");
         g_hist_price[i] = _JsonDouble(item, "p");
         g_hist_wp   [i] = _JsonDouble(item, "w");
         g_hist_lot  [i] = _JsonDouble(item, "l");
         g_hist_rr   [i] = _JsonDouble(item, "r");
         g_hist_ts   [i] = _JsonString(item, "t");
         g_hist_count++;
      }
   }
}

//===================================================================
//  Drawing helpers
//===================================================================
void _T(const string name, const string text, int x, int y, color clr, int sz = -1)
{
   string full = OBJ_PREFIX + name;
   if(sz < 0) sz = FontSz;
   if(ObjectFind(0, full) < 0) ObjectCreate(0, full, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, full, OBJPROP_CORNER,    CORNER_LEFT_UPPER);
   ObjectSetInteger(0, full, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, full, OBJPROP_YDISTANCE, y);
   ObjectSetString(0,  full, OBJPROP_TEXT,      text);
   ObjectSetString(0,  full, OBJPROP_FONT,      FontName);
   ObjectSetInteger(0, full, OBJPROP_FONTSIZE,  sz);
   ObjectSetInteger(0, full, OBJPROP_COLOR,     clr);
   ObjectSetInteger(0, full, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, full, OBJPROP_HIDDEN,    true);
}

void _DrawBg(int x, int y, int w, int h)
{
   string nm = OBJ_PREFIX + "bg";
   if(ObjectFind(0, nm) < 0) ObjectCreate(0, nm, OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, nm, OBJPROP_CORNER,      CORNER_LEFT_UPPER);
   ObjectSetInteger(0, nm, OBJPROP_XDISTANCE,   x);
   ObjectSetInteger(0, nm, OBJPROP_YDISTANCE,   y);
   ObjectSetInteger(0, nm, OBJPROP_XSIZE,       w);
   ObjectSetInteger(0, nm, OBJPROP_YSIZE,       h);
   ObjectSetInteger(0, nm, OBJPROP_BGCOLOR,     ClrBg);
   ObjectSetInteger(0, nm, OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSetInteger(0, nm, OBJPROP_COLOR,       ClrBorder);
   ObjectSetInteger(0, nm, OBJPROP_WIDTH,       1);
   ObjectSetInteger(0, nm, OBJPROP_BACK,        false);
   ObjectSetInteger(0, nm, OBJPROP_SELECTABLE,  false);
   ObjectSetInteger(0, nm, OBJPROP_HIDDEN,      true);
}

// Thin horizontal separator line (OBJ_RECTANGLE_LABEL, 1 px tall)
void _DrawSep(const string id, int y)
{
   string nm = OBJ_PREFIX + "sep_" + id;
   if(ObjectFind(0, nm) < 0) ObjectCreate(0, nm, OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, nm, OBJPROP_CORNER,      CORNER_LEFT_UPPER);
   ObjectSetInteger(0, nm, OBJPROP_XDISTANCE,   PanelX + 6);
   ObjectSetInteger(0, nm, OBJPROP_YDISTANCE,   y);
   ObjectSetInteger(0, nm, OBJPROP_XSIZE,       PANEL_W - 12);
   ObjectSetInteger(0, nm, OBJPROP_YSIZE,       1);
   ObjectSetInteger(0, nm, OBJPROP_BGCOLOR,     ClrBorder);
   ObjectSetInteger(0, nm, OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSetInteger(0, nm, OBJPROP_COLOR,       ClrBorder);
   ObjectSetInteger(0, nm, OBJPROP_BACK,        false);
   ObjectSetInteger(0, nm, OBJPROP_SELECTABLE,  false);
   ObjectSetInteger(0, nm, OBJPROP_HIDDEN,      true);
}

//===================================================================
//  Main draw
//===================================================================
void _DrawPanel()
{
   // Column X anchors (absolute px from chart left)
   int lx   = PanelX + 8;    // label start
   int vx   = PanelX + 113;  // value start (single col)
   int c2lx = PanelX + 167;  // 2nd pair — label start
   int c2vx = PanelX + 252;  // 2nd pair — value start

   // Panel height: 6 sections × (LH+LH_DIV) + 27 rows×LH + 6 rows×LH_SIG + top pad
   // With LH=13, LH_DIV=8, LH_SIG=12 → ~465 px content; use 480 with padding
   int panelH = 480;
   _DrawBg(PanelX, PanelY, PANEL_W, panelH);

   int ty = PanelY + 8;

   // ── TITLE ──────────────────────────────────────────────────────
   bool isStale  = (g_pd.unix_time > 0) && ((long)TimeGMT() - g_pd.unix_time > 30);
   string badge  = g_pd.system_killed ? " [KILLED]"
                 : isStale            ? " [STALE]"
                 : g_pd.system_alive  ? " [LIVE]" : " [OFFLINE]";
   color badgeC  = g_pd.system_killed ? ClrValueNeg
                 : isStale            ? clrOrange
                 : g_pd.system_alive  ? ClrValuePos : clrDimGray;
   _T("title", "ATS  " + _Symbol + badge, lx, ty, badgeC, FontSz + 2);
   ty += LH + 5;
   _DrawSep("0", ty); ty += LH_DIV;

   // ── MARKET ─────────────────────────────────────────────────────
   _T("s_mkt", "MARKET", lx, ty, ClrSection);
   ty += LH;

   // Row: Chart TF  |  ATS TF (M1 — feature window)
   _T("mkt_ct_l", "Chart TF  :", lx,   ty, ClrLabel);
   _T("mkt_ct_v", _TFStr(),      vx,   ty, ClrValueNeutral);
   _T("mkt_at_l", "ATS TF  :",  c2lx,  ty, ClrLabel);
   _T("mkt_at_v", "M1 (RL)",    c2vx,  ty, C'150,150,200');
   ty += LH;

   // Row: Session  |  Spread
   long spr = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   color sprC = spr <= 15 ? ClrValuePos : spr <= 30 ? clrYellow : ClrValueNeg;
   _T("mkt_ss_l", "Session   :", lx,   ty, ClrLabel);
   _T("mkt_ss_v", _Session(),    vx,   ty, _SessionClr());
   _T("mkt_sp_l", "Spread  :",  c2lx,  ty, ClrLabel);
   _T("mkt_sp_v", IntegerToString((int)spr) + " pts", c2vx, ty, sprC);
   ty += LH;

   _DrawSep("1", ty); ty += LH_DIV;

   // ── POSITION ───────────────────────────────────────────────────
   _T("s_pos", "POSITION", lx, ty, ClrSection);
   ty += LH;

   string posS = g_pd.position ==  1 ? "LONG"  : g_pd.position == -1 ? "SHORT" : "FLAT";
   color  posC = g_pd.position ==  1 ? ClrValuePos : g_pd.position == -1 ? ClrValueNeg : clrDimGray;
   string rgS  = g_pd.regime == 1 ? "TREND" : g_pd.regime == 0 ? "RANGE" : "---";
   color  rgC  = g_pd.regime == 1 ? ClrValuePos : g_pd.regime == 0 ? clrYellow : clrDimGray;

   _T("pos_st_l", "Status    :", lx,   ty, ClrLabel);  _T("pos_st_v", posS, vx,   ty, posC);
   _T("pos_rg_l", "Regime  :",   c2lx, ty, ClrLabel);  _T("pos_rg_v", rgS,  c2vx, ty, rgC);
   ty += LH;

   string entS = g_pd.entry_price > 0 ? DoubleToString(g_pd.entry_price, _Digits) : "---";
   _T("pos_en_l", "Entry     :", lx, ty, ClrLabel);
   _T("pos_en_v", entS,          vx, ty, ClrValueNeutral);
   ty += LH;

   bool   hasP  = g_pd.position != 0;
   string pnlS  = hasP ? (g_pd.unrealized_pnl >= 0 ? "+" : "") + DoubleToString(g_pd.unrealized_pnl, 2) + " USD" : "---";
   color  pnlC  = !hasP ? clrDimGray : g_pd.unrealized_pnl >= 0 ? ClrValuePos : ClrValueNeg;
   _T("pos_pl_l", "Unrealized:", lx, ty, ClrLabel);
   _T("pos_pl_v", pnlS,          vx, ty, pnlC);
   ty += LH;

   color sdC = g_pd.sym_drawdown  < 5 ? ClrValuePos : g_pd.sym_drawdown  < 10 ? clrYellow : ClrValueNeg;
   color adC = g_pd.acct_drawdown < 5 ? ClrValuePos : g_pd.acct_drawdown < 10 ? clrYellow : ClrValueNeg;
   _T("pos_sd_l", "Sym DD    :", lx,   ty, ClrLabel);  _T("pos_sd_v", DoubleToString(g_pd.sym_drawdown, 2)  + "%", vx,   ty, sdC);
   _T("pos_ad_l", "Acct DD :",  c2lx,  ty, ClrLabel);  _T("pos_ad_v", DoubleToString(g_pd.acct_drawdown, 2) + "%", c2vx, ty, adC);
   ty += LH;

   _DrawSep("2", ty); ty += LH_DIV;

   // ── SIGNALS ────────────────────────────────────────────────────
   // Section header row: show total count
   _T("s_sig", "SIGNALS", lx, ty, ClrSection);
   _T("s_sig_cnt", "  total: " + IntegerToString(g_pd.signal_count), lx + 58, ty, C'120,120,150', FontSz - 1);
   ty += LH;

   // Column headers for the table
   int thx0 = lx + 2;   // Dir
   int thx1 = lx + 42;  // Price
   int thx2 = lx + 118; // Win%
   int thx3 = lx + 158; // R/R
   int thx4 = lx + 202; // Lot
   int thx5 = lx + 248; // Time
   color hdrC = C'90,90,120';
   _T("sgh_d", "Dir ",  thx0, ty, hdrC, FontSz - 1);
   _T("sgh_p", "Price", thx1, ty, hdrC, FontSz - 1);
   _T("sgh_w", "Win%",  thx2, ty, hdrC, FontSz - 1);
   _T("sgh_r", "R/R",   thx3, ty, hdrC, FontSz - 1);
   _T("sgh_l", "Lot",   thx4, ty, hdrC, FontSz - 1);
   _T("sgh_t", "Time",  thx5, ty, hdrC, FontSz - 1);
   ty += LH_SIG - 2;

   // Signal rows
   for(int i = 0; i < SIG_ROWS; i++)
   {
      string row = "sr" + IntegerToString(i);
      if(i < g_hist_count)
      {
         string dirS = _SideStr(g_hist_side[i]);
         color  dirC = _SideClr(g_hist_side[i]);
         string prS  = DoubleToString(g_hist_price[i], 2);
         string wpS  = g_hist_wp[i] > 0 ? DoubleToString(g_hist_wp[i] * 100, 1) + "%" : "---";
         string rrS  = g_hist_rr[i] > 0 ? DoubleToString(g_hist_rr[i], 1) : "---";
         string ltS  = g_hist_lot[i] > 0 ? DoubleToString(g_hist_lot[i], 2) : "---";
         color  wpC  = g_hist_wp[i] >= 0.60 ? ClrValuePos
                     : g_hist_wp[i] >= 0.50 ? clrYellow : ClrValueNeg;
         _T(row + "_d", dirS,           thx0, ty, dirC,            FontSz - 1);
         _T(row + "_p", prS,            thx1, ty, ClrValueNeutral, FontSz - 1);
         _T(row + "_w", wpS,            thx2, ty, wpC,             FontSz - 1);
         _T(row + "_r", rrS,            thx3, ty, ClrValueNeutral, FontSz - 1);
         _T(row + "_l", ltS,            thx4, ty, ClrValueNeutral, FontSz - 1);
         _T(row + "_t", g_hist_ts[i],   thx5, ty, ClrTimestamp,    FontSz - 1);
      }
      else
      {
         // Empty row placeholder — keep objects stable
         _T(row + "_d", "---", thx0, ty, clrDimGray, FontSz - 1);
         _T(row + "_p", "",    thx1, ty, clrDimGray, FontSz - 1);
         _T(row + "_w", "",    thx2, ty, clrDimGray, FontSz - 1);
         _T(row + "_r", "",    thx3, ty, clrDimGray, FontSz - 1);
         _T(row + "_l", "",    thx4, ty, clrDimGray, FontSz - 1);
         _T(row + "_t", "",    thx5, ty, clrDimGray, FontSz - 1);
      }
      ty += LH_SIG;
   }

   // Latest signal detail (SL / TP / Z-Score) below the table
   bool   hasSig = StringLen(g_pd.sig_timestamp) > 0;
   int    d      = _Digits;
   string slS = (hasSig && g_pd.sig_sl > 0) ? DoubleToString(g_pd.sig_sl, d) : "---";
   string tpS = (hasSig && g_pd.sig_tp > 0) ? DoubleToString(g_pd.sig_tp, d) : "---";
   string zsS = hasSig ? DoubleToString(g_pd.sig_z_score, 2) : "---";
   _T("sig_sl_l", "SL        :", lx,   ty, ClrLabel);  _T("sig_sl_v", slS, vx,   ty, ClrValueNeg);
   _T("sig_tp_l", "TP      :",  c2lx,  ty, ClrLabel);  _T("sig_tp_v", tpS, c2vx, ty, ClrValuePos);
   ty += LH;
   _T("sig_zs_l", "Z-Score   :", lx, ty, ClrLabel);
   _T("sig_zs_v", zsS,           vx, ty, ClrValueNeutral);
   ty += LH;

   _DrawSep("3", ty); ty += LH_DIV;

   // ── RISK & ACCOUNT ─────────────────────────────────────────────
   _T("s_rsk", "RISK & ACCOUNT", lx, ty, ClrSection);
   ty += LH;
   _T("rsk_kf_l", "Kelly f*  :", lx, ty, ClrLabel);
   _T("rsk_kf_v", DoubleToString(g_pd.kelly_f * 100, 2) + "%", vx, ty, ClrValueNeutral);
   ty += LH;
   _T("rsk_eq_l", "Equity    :", lx,   ty, ClrLabel);  _T("rsk_eq_v", DoubleToString(g_pd.equity,  2), vx,   ty, ClrValueNeutral);
   _T("rsk_bl_l", "Balance :",  c2lx,  ty, ClrLabel);  _T("rsk_bl_v", DoubleToString(g_pd.balance, 2), c2vx, ty, ClrValueNeutral);
   ty += LH;

   _DrawSep("4", ty); ty += LH_DIV;

   // ── AI MODEL ───────────────────────────────────────────────────
   string mdlS = g_pd.model_is_training ? "TRAINING..." : "READY";
   color  mdlC = g_pd.model_is_training ? clrOrange     : ClrValuePos;
   _T("s_mdl",   "AI MODEL",        lx,        ty, ClrSection);
   _T("s_mdl_s", "  [" + mdlS + "]", lx + 60,  ty, mdlC, FontSz);
   ty += LH;

   string ver = StringLen(g_pd.model_version) > 0 ? _Trunc(g_pd.model_version, 29) : "---";
   _T("mdl_vr_l", "Version   :", lx, ty, ClrLabel);  _T("mdl_vr_v", ver, vx, ty, mdlC);
   ty += LH;

   string lrFull = _IsoDate(g_pd.model_last_retrain) != "---"
                 ? _IsoDate(g_pd.model_last_retrain) + "  " + _IsoTime(g_pd.model_last_retrain)
                 : "---";
   _T("mdl_lr_l", "Last Train:", lx, ty, ClrLabel);  _T("mdl_lr_v", lrFull, vx, ty, ClrValueNeutral);
   ty += LH;

   string rsn = StringLen(g_pd.model_retrain_reason) > 0 ? _Trunc(g_pd.model_retrain_reason, 26) : "---";
   _T("mdl_rs_l", "Reason    :", lx, ty, ClrLabel);  _T("mdl_rs_v", rsn, vx, ty, C'140,140,170');
   ty += LH;

   string wrS = g_pd.model_win_rate > 0 ? DoubleToString(g_pd.model_win_rate * 100, 1) + "%" : "---";
   string shS = g_pd.model_sharpe  != 0 ? DoubleToString(g_pd.model_sharpe, 3)           : "---";
   color  wrC = g_pd.model_win_rate >= 0.5 ? ClrValuePos : g_pd.model_win_rate >= 0.43 ? clrYellow : ClrValueNeg;
   color  shC = g_pd.model_sharpe  >= 1.0 ? ClrValuePos : g_pd.model_sharpe  >= 0.5   ? clrYellow : ClrValueNeg;
   if(g_pd.model_win_rate <= 0) wrC = clrDimGray;
   if(g_pd.model_sharpe  == 0) shC = clrDimGray;
   _T("mdl_wr_l", "Win Rate  :", lx,   ty, ClrLabel);  _T("mdl_wr_v", wrS, vx,   ty, wrC);
   _T("mdl_sh_l", "Sharpe  :",  c2lx,  ty, ClrLabel);  _T("mdl_sh_v", shS, c2vx, ty, shC);
   ty += LH;

   _T("mdl_tc_l", "Trades(win):", lx, ty, ClrLabel);
   _T("mdl_tc_v", IntegerToString(g_pd.model_total_trades), vx, ty, ClrValueNeutral);
   ty += LH;

   _DrawSep("5", ty); ty += LH_DIV;

   // ── SYSTEM ─────────────────────────────────────────────────────
   _T("s_sys", "SYSTEM", lx, ty, ClrSection);
   ty += LH;

   string kr   = StringLen(g_pd.kill_reason) > 0 ? _Trunc(g_pd.kill_reason, 22) : "---";
   color  krC  = StringLen(g_pd.kill_reason) > 0 ? ClrValueNeg : clrDimGray;
   _T("sys_kr_l", "Kill Reason:", lx, ty, ClrLabel);  _T("sys_kr_v", kr, vx, ty, krC);
   ty += LH;

   long   hbAgo = (g_pd.unix_time > 0) ? ((long)TimeGMT() - g_pd.unix_time) : -1;
   string hbS   = (hbAgo < 0)   ? "---"
                : (hbAgo < 60)  ? IntegerToString(hbAgo)     + "s ago"
                : (hbAgo < 3600)? IntegerToString(hbAgo/60)  + "m ago"
                :                 "> 1h ago";
   color  hbC   = (hbAgo < 0) ? clrDimGray
                : (hbAgo < 15) ? ClrValuePos
                : (hbAgo < 45) ? clrYellow : ClrValueNeg;
   _T("sys_hb_l", "Heartbeat :", lx, ty, ClrLabel);
   _T("sys_hb_v", hbS,           vx, ty, hbC);
   ty += LH;

   _T("sys_up_l", "Updated   :", lx, ty, ClrLabel);
   _T("sys_up_v", TimeToString(TimeLocal(), TIME_SECONDS), vx, ty, ClrTimestamp);

   ChartRedraw(0);
}

//===================================================================
void _DeletePanel() { ObjectsDeleteAll(0, OBJ_PREFIX); ChartRedraw(0); }
//+------------------------------------------------------------------+
