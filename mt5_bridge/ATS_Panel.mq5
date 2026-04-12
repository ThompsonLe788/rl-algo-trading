//+------------------------------------------------------------------+
//| ATS_Panel.mq5 — Live monitoring panel for ATS system            |
//| Reads ats_live_state.json written by Python LiveStateWriter      |
//| Displays on chart: Regime, Position, P&L, Kelly, DD, HB status  |
//+------------------------------------------------------------------+
#property copyright "ATS"
#property version   "1.00"
#property indicator_chart_window
#property indicator_plots 0

//--- Input parameters
input int    PanelX        = 10;     // Panel right margin from chart edge (pixels)
input int    PanelY        = 30;     // Panel top margin from chart top (pixels)
input color  ClrBg         = C'20,20,35';   // Background colour
input color  ClrTitle      = clrGold;       // Title colour
input color  ClrLabel      = clrSilver;     // Label colour
input color  ClrValuePos   = clrLimeGreen;  // Value colour (positive / long)
input color  ClrValueNeg   = clrTomato;     // Value colour (negative / short)
input color  ClrValueNeutral = clrWhite;    // Value colour (neutral)
input int    FontSz        = 9;      // Font size
input string FontName      = "Consolas";    // Font name
input int    RefreshSec    = 2;      // State file refresh interval (seconds)

//--- Panel object name prefix
#define OBJ_PREFIX "ATS_"
#define JSON_FILE  "ats_live_state.json"

//--- Cached parsed values
struct PanelData
{
   string symbol;
   int    position;   // -1 short, 0 flat, 1 long
   double entry_price;
   double unrealized_pnl;
   int    regime;     // 0=range, 1=trend, -1=unknown
   double kelly_f;
   double drawdown_pct;
   double equity;
   bool   system_alive;
   bool   system_killed;
   int    signal_count;
   string last_heartbeat;
   datetime last_hb_time;
};

PanelData g_pd;
datetime  g_lastRefresh = 0;

//+------------------------------------------------------------------+
//| Write "1" or "0" to ats_chart_{SYMBOL}.txt in Common Files      |
//| Python multi_runner.py scans these to know which charts are open |
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
   g_pd.symbol = _Symbol;
   _RegisterChart(true);   // signal Python: this chart is open
   EventSetTimer(RefreshSec);
   _LoadState();
   _DrawPanel();
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   _RegisterChart(false);  // signal Python: chart closed
   EventKillTimer();
   _DeletePanel();
}

//+------------------------------------------------------------------+
int OnCalculate(const int rates_total, const int prev_calculated,
                const datetime &time[], const double &open[],
                const double &high[], const double &low[],
                const double &close[], const long &vol[],
                const long &spread[], const int &real_volume[])
{
   return rates_total;
}

//+------------------------------------------------------------------+
void OnTimer()
{
   if(TimeLocal() - g_lastRefresh >= RefreshSec)
   {
      _LoadState();
      _DrawPanel();
      g_lastRefresh = TimeLocal();
   }
}

//+------------------------------------------------------------------+
//| Parse a double value from JSON string                            |
//+------------------------------------------------------------------+
double _JsonDouble(const string &json, const string &key)
{
   string search = "\"" + key + "\":";
   int pos = StringFind(json, search);
   if(pos < 0) return 0.0;
   pos += StringLen(search);
   while(pos < StringLen(json) && StringGetCharacter(json, pos) == ' ') pos++;
   string val = "";
   while(pos < StringLen(json))
   {
      ushort ch = StringGetCharacter(json, pos);
      if(ch == ',' || ch == '}' || ch == ' ' || ch == '\n') break;
      val += CharToString((uchar)ch);
      pos++;
   }
   return StringToDouble(val);
}

//+------------------------------------------------------------------+
//| Parse a bool value from JSON string                              |
//+------------------------------------------------------------------+
bool _JsonBool(const string &json, const string &key)
{
   string search = "\"" + key + "\":";
   int pos = StringFind(json, search);
   if(pos < 0) return false;
   pos += StringLen(search);
   while(pos < StringLen(json) && StringGetCharacter(json, pos) == ' ') pos++;
   return (StringGetCharacter(json, pos) == 't');  // "true"
}

//+------------------------------------------------------------------+
//| Parse an int value from JSON string                              |
//+------------------------------------------------------------------+
long _JsonInt(const string &json, const string &key)
{
   return (long)_JsonDouble(json, key);
}

//+------------------------------------------------------------------+
//| Extract a JSON sub-object as string                              |
//+------------------------------------------------------------------+
string _JsonObject(const string &json, const string &key)
{
   string search = "\"" + key + "\":";
   int pos = StringFind(json, search);
   if(pos < 0) return "";
   pos += StringLen(search);
   while(pos < StringLen(json) && StringGetCharacter(json, pos) == ' ') pos++;
   if(StringGetCharacter(json, pos) != '{') return "";
   int depth = 0;
   int start = pos;
   while(pos < StringLen(json))
   {
      ushort ch = StringGetCharacter(json, pos);
      if(ch == '{') depth++;
      else if(ch == '}') { depth--; if(depth == 0) { pos++; break; } }
      pos++;
   }
   return StringSubstr(json, start, pos - start);
}

//+------------------------------------------------------------------+
//| Load and parse ats_live_state.json                               |
//+------------------------------------------------------------------+
void _LoadState()
{
   int handle = FileOpen(JSON_FILE, FILE_READ | FILE_TXT | FILE_COMMON);
   if(handle == INVALID_HANDLE) return;

   string content = "";
   while(!FileIsEnding(handle))
      content += FileReadString(handle);
   FileClose(handle);

   if(StringLen(content) < 5) return;

   //--- Parse _account section
   string acct = _JsonObject(content, "_account");
   if(StringLen(acct) > 0)
      g_pd.equity = _JsonDouble(acct, "equity");

   //--- Parse _system section
   string sys = _JsonObject(content, "_system");
   if(StringLen(sys) > 0)
   {
      g_pd.system_alive   = _JsonBool(sys, "alive");
      g_pd.system_killed  = _JsonBool(sys, "killed");
      g_pd.signal_count   = (int)_JsonInt(sys, "signal_count");
   }

   //--- Parse symbol section (using _Symbol)
   string sym_sec = _JsonObject(content, _Symbol);
   if(StringLen(sym_sec) > 0)
   {
      g_pd.position        = (int)_JsonInt(sym_sec,  "position");
      g_pd.entry_price     = _JsonDouble(sym_sec,    "entry_price");
      g_pd.unrealized_pnl  = _JsonDouble(sym_sec,    "unrealized_pnl");
      g_pd.regime          = (int)_JsonInt(sym_sec,  "regime");
      g_pd.kelly_f         = _JsonDouble(sym_sec,    "kelly_f");
      g_pd.drawdown_pct    = _JsonDouble(sym_sec,    "drawdown_pct");
   }
}

//+------------------------------------------------------------------+
//| Create/update a text object on the chart                         |
//+------------------------------------------------------------------+
void _Text(const string name, const string text, int x, int y,
           color clr, int sz = -1)
{
   string full = OBJ_PREFIX + name;
   if(sz < 0) sz = FontSz;

   if(ObjectFind(0, full) < 0)
      ObjectCreate(0, full, OBJ_LABEL, 0, 0, 0);

   ObjectSetInteger(0, full, OBJPROP_CORNER,   CORNER_RIGHT_UPPER);
   ObjectSetInteger(0, full, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, full, OBJPROP_YDISTANCE, y);
   ObjectSetString(0,  full, OBJPROP_TEXT,      text);
   ObjectSetString(0,  full, OBJPROP_FONT,      FontName);
   ObjectSetInteger(0, full, OBJPROP_FONTSIZE,  sz);
   ObjectSetInteger(0, full, OBJPROP_COLOR,     clr);
   ObjectSetInteger(0, full, OBJPROP_SELECTABLE, false);
}

//+------------------------------------------------------------------+
//| Create/update background rectangle                               |
//+------------------------------------------------------------------+
void _Background(int x, int y, int w, int h)
{
   string name = OBJ_PREFIX + "bg";
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_RECTANGLE_LABEL, 0, 0, 0);

   ObjectSetInteger(0, name, OBJPROP_CORNER,    CORNER_RIGHT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, name, OBJPROP_XSIZE,     w);
   ObjectSetInteger(0, name, OBJPROP_YSIZE,     h);
   ObjectSetInteger(0, name, OBJPROP_BGCOLOR,   ClrBg);
   ObjectSetInteger(0, name, OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSetInteger(0, name, OBJPROP_COLOR,     clrDimGray);
   ObjectSetInteger(0, name, OBJPROP_WIDTH,     1);
   ObjectSetInteger(0, name, OBJPROP_BACK,      false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
}

//+------------------------------------------------------------------+
//| Draw the full panel                                               |
//+------------------------------------------------------------------+
void _DrawPanel()
{
   int panelW  = 220;
   int panelH  = 175;
   int bx = PanelX + panelW;  // right-edge anchor distance from chart right
   int by = PanelY;

   _Background(bx, by, panelW, panelH);

   int lh = 18;  // line height px
   int tx = bx - 10;  // text x anchor (from right)
   int ty = by + 8;   // start y

   //--- Title
   string systemBadge = g_pd.system_killed ? " [KILLED]"
                      : (g_pd.system_alive  ? " [LIVE]" : " [OFFLINE]");
   color  badgeClr    = g_pd.system_killed ? ClrValueNeg
                      : (g_pd.system_alive  ? ClrValuePos : clrGray);
   _Text("title",  "ATS  " + _Symbol + systemBadge, tx, ty, ClrTitle, FontSz + 1);
   ty += lh + 4;

   //--- Divider line (simulated with dashes)
   _Text("div1", "- - - - - - - - - - - - - - -", tx, ty, clrDimGray, FontSz - 2);
   ty += lh;

   //--- Regime
   string regimeStr = (g_pd.regime == 1) ? "TREND" : (g_pd.regime == 0) ? "RANGE" : "---";
   color  regimeClr = (g_pd.regime == 1) ? ClrValuePos : (g_pd.regime == 0) ? clrYellow : clrGray;
   _Text("rg_lbl", "Regime :", tx, ty, ClrLabel);
   _Text("rg_val", regimeStr,  tx - 90, ty, regimeClr);
   ty += lh;

   //--- Position
   string posStr = (g_pd.position ==  1) ? "LONG"
                 : (g_pd.position == -1) ? "SHORT" : "FLAT";
   color  posClr = (g_pd.position ==  1) ? ClrValuePos
                 : (g_pd.position == -1) ? ClrValueNeg : ClrValueNeutral;
   _Text("pos_lbl", "Position:", tx, ty, ClrLabel);
   _Text("pos_val", posStr,     tx - 90, ty, posClr);
   ty += lh;

   //--- Entry price
   string entryStr = (g_pd.entry_price > 0)
                   ? DoubleToString(g_pd.entry_price, _Digits) : "---";
   _Text("entry_lbl", "Entry   :", tx, ty,       ClrLabel);
   _Text("entry_val", entryStr,    tx - 90, ty,  ClrValueNeutral);
   ty += lh;

   //--- Unrealized P&L
   string pnlStr = (g_pd.position != 0)
                 ? (g_pd.unrealized_pnl >= 0 ? "+" : "")
                   + DoubleToString(g_pd.unrealized_pnl, 2) + " USD"
                 : "---";
   color  pnlClr = (g_pd.unrealized_pnl >= 0) ? ClrValuePos : ClrValueNeg;
   if(g_pd.position == 0) pnlClr = clrGray;
   _Text("pnl_lbl", "Unrealzd:", tx, ty,      ClrLabel);
   _Text("pnl_val", pnlStr,      tx - 90, ty, pnlClr);
   ty += lh;

   //--- Kelly f*
   _Text("kf_lbl", "Kelly f*:", tx, ty, ClrLabel);
   _Text("kf_val", DoubleToString(g_pd.kelly_f * 100, 2) + "%",
         tx - 90, ty, ClrValueNeutral);
   ty += lh;

   //--- Drawdown
   color ddClr = (g_pd.drawdown_pct < 5) ? ClrValuePos
               : (g_pd.drawdown_pct < 10) ? clrYellow : ClrValueNeg;
   _Text("dd_lbl", "Drawdown:", tx, ty, ClrLabel);
   _Text("dd_val", DoubleToString(g_pd.drawdown_pct, 2) + "%",
         tx - 90, ty, ddClr);
   ty += lh;

   //--- Equity
   _Text("eq_lbl", "Equity  :", tx, ty, ClrLabel);
   _Text("eq_val", DoubleToString(g_pd.equity, 2),
         tx - 90, ty, ClrValueNeutral);
   ty += lh;

   //--- Signals count
   _Text("sig_lbl", "Signals :", tx, ty, ClrLabel);
   _Text("sig_val", IntegerToString(g_pd.signal_count),
         tx - 90, ty, ClrValueNeutral);

   ChartRedraw(0);
}

//+------------------------------------------------------------------+
//| Remove all panel objects                                          |
//+------------------------------------------------------------------+
void _DeletePanel()
{
   ObjectsDeleteAll(0, OBJ_PREFIX);
   ChartRedraw(0);
}
//+------------------------------------------------------------------+
