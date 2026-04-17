//+------------------------------------------------------------------+
//| ATS_StrategyView.mq5  v1.10                                      |
//| Visualizes ATS strategy signals in real-time.                    |
//|                                                                  |
//| Main chart (chart objects on window 0):                          |
//|   - VWAP line (session, reset daily)                             |
//|   - ATR upper/lower bands (±ATRMultSL × ATR from VWAP)          |
//|   - Signal arrows from ats_live_state.json (↑ long, ↓ short)    |
//|                                                                  |
//| Subwindow (indicator buffers):                                   |
//|   - Rolling OU z-score (ZScoreWindow bars)                       |
//|   - +2.0 overbought reference line                               |
//|   - −2.0 / 0.0 drawn as horizontal objects                       |
//|                                                                  |
//| Background: grey = RANGE regime, light blue = TREND regime       |
//+------------------------------------------------------------------+
#property copyright "XAU ATS"
#property link      ""
#property version   "1.10"
#property strict

// Subwindow for z-score (NOT indicator_chart_window — they conflict)
#property indicator_separate_window
#property indicator_minimum -4.5
#property indicator_maximum  4.5
#property indicator_buffers 2
#property indicator_plots   2

#property indicator_label1  "OU Z-Score"
#property indicator_type1   DRAW_LINE
#property indicator_color1  clrMagenta
#property indicator_style1  STYLE_SOLID
#property indicator_width1  2

#property indicator_label2  "+2 Ref"
#property indicator_type2   DRAW_LINE
#property indicator_color2  clrOrangeRed
#property indicator_style2  STYLE_DOT
#property indicator_width2  1

// ---- Buffers ----
double ZScoreBuf[];   // OU z-score  → plotted in subwindow
double OBBuf[];       // constant +2 reference line

// ---- Inputs ----
input int    ZScoreWindow = 50;
input int    ATRPeriod    = 14;
input double ATRMultSL    = 1.5;
input string StateFile    = "ats_live_state.json";
input int    RefreshSec   = 2;
input int    VwapSegBars  = 10;   // bars per VWAP object segment (lower = smoother)

// ---- State ----
datetime g_lastArrowTime = 0;
int      g_lastPosition  = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   SetIndexBuffer(0, ZScoreBuf, INDICATOR_DATA);
   SetIndexBuffer(1, OBBuf,     INDICATOR_DATA);
   ArraySetAsSeries(ZScoreBuf, false);
   ArraySetAsSeries(OBBuf,     false);

   // Horizontal reference lines in the subwindow
   int sw = ChartWindowFind(0, IndicatorSetString(INDICATOR_SHORTNAME,
            StringFormat("ATS View [Z%d ATR%d]", ZScoreWindow, ATRPeriod)));
   _HLine("ATS_OB",  2.0, clrOrangeRed, STYLE_DOT,   sw);
   _HLine("ATS_OS", -2.0, clrLime,      STYLE_DOT,   sw);
   _HLine("ATS_Z0",  0.0, clrGray,      STYLE_SOLID, sw);

   EventSetTimer(RefreshSec);
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   ObjectDelete(0, "ATS_OB");
   ObjectDelete(0, "ATS_OS");
   ObjectDelete(0, "ATS_Z0");
   _DeletePrefix("ATS_VW_");
   _DeletePrefix("ATS_AU_");
   _DeletePrefix("ATS_AL_");
   _DeletePrefix("ATS_SIG_");
  }

//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double   &open[],
                const double   &high[],
                const double   &low[],
                const double   &close[],
                const long     &tick_volume[],
                const long     &volume[],
                const int      &spread[])
  {
   int start = MathMax(prev_calculated - 1, ZScoreWindow + ATRPeriod + 1);
   if(start >= rates_total) return rates_total;

   // ---- 1. OU Z-Score (subwindow buffer) ----
   for(int i = start; i < rates_total; i++)
     {
      if(i < ZScoreWindow) { ZScoreBuf[i] = 0; OBBuf[i] = 2.0; continue; }
      double sum = 0;
      for(int j = i - ZScoreWindow + 1; j <= i; j++) sum += close[j];
      double mu = sum / ZScoreWindow;
      double sq = 0;
      for(int j = i - ZScoreWindow + 1; j <= i; j++) sq += (close[j]-mu)*(close[j]-mu);
      double sigma = MathSqrt(sq / ZScoreWindow);
      ZScoreBuf[i] = (sigma > 1e-9) ? (close[i] - mu) / sigma : 0.0;
      OBBuf[i]     = 2.0;
     }

   // ---- 2. VWAP + ATR bands on main chart as trend-line objects ----
   _UpdateVwapObjects(time, close, high, low, tick_volume,
                      rates_total, prev_calculated);

   // ---- 3. Regime background from JSON ----
   _UpdateRegimeBackground();

   return rates_total;
  }

//+------------------------------------------------------------------+
void OnTimer()
  {
   _DrawSignalArrows();
   ChartRedraw();
  }

//+------------------------------------------------------------------+
// Draw/update VWAP + ATR band chart objects on window 0.
// Segments every VwapSegBars bars for performance.
//+------------------------------------------------------------------+
void _UpdateVwapObjects(const datetime &time[],
                        const double   &close[],
                        const double   &high[],
                        const double   &low[],
                        const long     &tv[],
                        int total, int prev_calc)
  {
   // Full recalc: remove all existing ATS VWAP/ATR objects
   if(prev_calc == 0)
     {
      _DeletePrefix("ATS_VW_");
      _DeletePrefix("ATS_AU_");
      _DeletePrefix("ATS_AL_");
     }

   // Compute VWAP for all bars (daily reset)
   double vwap[];
   ArrayResize(vwap, total);
   double cumPV = 0, cumVol = 0;
   datetime curDay = 0;
   for(int i = 0; i < total; i++)
     {
      datetime d = (datetime)(MathFloor((double)time[i] / 86400.0) * 86400);
      if(d != curDay) { curDay = d; cumPV = 0; cumVol = 0; }
      double v = (double)tv[i]; if(v < 1) v = 1;
      cumPV  += close[i] * v;
      cumVol += v;
      vwap[i] = cumPV / cumVol;
     }

   // Starting segment index (partial update starts a few segments back)
   int seg0 = (prev_calc > VwapSegBars * 3)
              ? ((prev_calc / VwapSegBars) - 2) * VwapSegBars
              : 0;
   seg0 = MathMax(seg0, ATRPeriod);

   for(int i = seg0; i < total - 1; i += VwapSegBars)
     {
      int j = MathMin(i + VwapSegBars, total - 1);
      double atrI = _ATR(i, ATRPeriod, high, low, close, total);
      double atrJ = _ATR(j, ATRPeriod, high, low, close, total);

      _TrendObj(StringFormat("ATS_VW_%d", i),
                time[i], vwap[i],
                time[j], vwap[j],
                clrDodgerBlue, 2);
      _TrendObj(StringFormat("ATS_AU_%d", i),
                time[i], vwap[i] + ATRMultSL * atrI,
                time[j], vwap[j] + ATRMultSL * atrJ,
                clrOrange, 1);
      _TrendObj(StringFormat("ATS_AL_%d", i),
                time[i], vwap[i] - ATRMultSL * atrI,
                time[j], vwap[j] - ATRMultSL * atrJ,
                clrOrange, 1);
     }
  }

//+------------------------------------------------------------------+
// Create or update a non-extending OBJ_TREND on window 0.
//+------------------------------------------------------------------+
void _TrendObj(const string name,
               datetime t1, double p1,
               datetime t2, double p2,
               color clr, int width)
  {
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_TREND, 0, t1, p1, t2, p2);
   ObjectSetInteger(0, name, OBJPROP_TIME,  0, t1);
   ObjectSetDouble (0, name, OBJPROP_PRICE, 0, p1);
   ObjectSetInteger(0, name, OBJPROP_TIME,  1, t2);
   ObjectSetDouble (0, name, OBJPROP_PRICE, 1, p2);
   ObjectSetInteger(0, name, OBJPROP_COLOR,       clr);
   ObjectSetInteger(0, name, OBJPROP_WIDTH,       width);
   ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT,   false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE,  false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN,      false);
   ObjectSetInteger(0, name, OBJPROP_BACK,        true);
  }

//+------------------------------------------------------------------+
// Read ats_live_state.json (FILE_BIN — safe for multi-line JSON).
// Draw an arrow when position changes.
//+------------------------------------------------------------------+
void _DrawSignalArrows()
  {
   string raw = _ReadJsonFile(StateFile);
   if(StringLen(raw) == 0) return;

   // Find this symbol's block
   int symPos = StringFind(raw, "\"" + _Symbol + "\"");
   if(symPos < 0) return;
   string chunk = StringSubstr(raw, symPos, 500);

   int posKey = StringFind(chunk, "\"position\":");
   if(posKey < 0) return;
   string posStr = StringSubstr(chunk, posKey + 11, 4);
   StringTrimLeft(posStr); StringTrimRight(posStr);
   int newPos = (int)StringToInteger(StringSubstr(posStr, 0, 2));

   if(newPos == g_lastPosition) return;

   datetime t = TimeCurrent();
   if(t == g_lastArrowTime) return;

   double price  = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   string aname  = StringFormat("ATS_SIG_%I64d", (long)t);

   if(newPos == 1)         // Long — up arrow below price
     {
      ObjectCreate(0, aname, OBJ_ARROW, 0, t, price * 0.9999);
      ObjectSetInteger(0, aname, OBJPROP_ARROWCODE, 233);
      ObjectSetInteger(0, aname, OBJPROP_COLOR,     clrLime);
      ObjectSetInteger(0, aname, OBJPROP_WIDTH,     3);
     }
   else if(newPos == -1)   // Short — down arrow above price
     {
      ObjectCreate(0, aname, OBJ_ARROW, 0, t, price * 1.0001);
      ObjectSetInteger(0, aname, OBJPROP_ARROWCODE, 234);
      ObjectSetInteger(0, aname, OBJPROP_COLOR,     clrRed);
      ObjectSetInteger(0, aname, OBJPROP_WIDTH,     3);
     }
   else if(newPos == 0 && g_lastPosition != 0)   // Closed — circle
     {
      ObjectCreate(0, aname, OBJ_ARROW, 0, t, price);
      ObjectSetInteger(0, aname, OBJPROP_ARROWCODE, 159);
      ObjectSetInteger(0, aname, OBJPROP_COLOR,     clrYellow);
      ObjectSetInteger(0, aname, OBJPROP_WIDTH,     2);
     }

   if(ObjectFind(0, aname) >= 0)
     {
      ObjectSetInteger(0, aname, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, aname, OBJPROP_HIDDEN,     false);
     }

   g_lastPosition  = newPos;
   g_lastArrowTime = t;
  }

//+------------------------------------------------------------------+
// Read JSON and tint chart background by regime (0=Range, 1=Trend).
//+------------------------------------------------------------------+
void _UpdateRegimeBackground()
  {
   string raw = _ReadJsonFile(StateFile);
   if(StringLen(raw) == 0) return;

   int symPos = StringFind(raw, "\"" + _Symbol + "\"");
   if(symPos < 0) return;
   string chunk  = StringSubstr(raw, symPos, 300);
   int    regKey = StringFind(chunk, "\"regime\":");
   if(regKey < 0) return;
   int regime = (int)StringToInteger(StringSubstr(chunk, regKey + 9, 2));

   color bg = (regime == 1) ? C'220,235,255' : C'240,240,240';
   ChartSetInteger(0, CHART_COLOR_BACKGROUND, bg);
  }

//+------------------------------------------------------------------+
// Read a Common Files JSON using FILE_BIN (avoids FILE_TXT newline
// truncation that broke ATS_Panel earlier).
//+------------------------------------------------------------------+
string _ReadJsonFile(const string filename)
  {
   int fh = FileOpen(filename, FILE_READ | FILE_BIN | FILE_COMMON);
   if(fh == INVALID_HANDLE) return "";
   ulong size = FileSize(fh);
   if(size == 0) { FileClose(fh); return ""; }
   uchar buf[];
   ArrayResize(buf, (int)size);
   FileReadArray(fh, buf, 0, (int)size);
   FileClose(fh);
   return CharArrayToString(buf, 0, (int)size, CP_UTF8);
  }

//+------------------------------------------------------------------+
// Simple ATR (SMA of True Range, no Wilder smoothing).
//+------------------------------------------------------------------+
double _ATR(int idx, int period,
            const double &high[], const double &low[], const double &close[],
            int total)
  {
   if(idx < period) return 0;
   double sum = 0;
   for(int i = idx - period + 1; i <= idx; i++)
     {
      double tr = high[i] - low[i];
      if(i > 0)
        {
         tr = MathMax(tr, MathAbs(high[i] - close[i-1]));
         tr = MathMax(tr, MathAbs(low[i]  - close[i-1]));
        }
      sum += tr;
     }
   return sum / period;
  }

//+------------------------------------------------------------------+
// Create a horizontal line object in the given subwindow.
//+------------------------------------------------------------------+
void _HLine(const string name, double price, color clr, int style, int sw)
  {
   if(sw < 0) sw = 1;
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_HLINE, sw, 0, price);
   ObjectSetDouble (0, name, OBJPROP_PRICE,      price);
   ObjectSetInteger(0, name, OBJPROP_COLOR,      clr);
   ObjectSetInteger(0, name, OBJPROP_STYLE,      style);
   ObjectSetInteger(0, name, OBJPROP_WIDTH,      1);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN,     true);
  }

//+------------------------------------------------------------------+
// Delete all chart objects whose names start with a given prefix.
//+------------------------------------------------------------------+
void _DeletePrefix(const string prefix)
  {
   int total = ObjectsTotal(0, 0, -1);
   for(int k = total - 1; k >= 0; k--)
     {
      string nm = ObjectName(0, k, 0, -1);
      if(StringFind(nm, prefix) == 0)
         ObjectDelete(0, nm);
     }
  }
//+------------------------------------------------------------------+
