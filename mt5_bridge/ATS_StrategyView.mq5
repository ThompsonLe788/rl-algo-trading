//+------------------------------------------------------------------+
//| ATS_StrategyView.mq5                                             |
//| Visualizes the ATS strategy signals in real-time.               |
//|                                                                  |
//| Subwindow 0 (Price chart):                                       |
//|   - VWAP line (session, reset daily)                             |
//|   - ATR upper/lower bands (±1.5×ATR from VWAP)                  |
//|   - Signal arrows from ats_live_state.json (↑ long, ↓ short)    |
//|                                                                  |
//| Subwindow 1 (OU Z-Score):                                        |
//|   - Rolling OU z-score (50-bar window)                           |
//|   - Overbought/oversold levels at ±2.0                           |
//|   - Zero line                                                    |
//|                                                                  |
//| Background shading: grey = RANGE regime, blue = TREND regime     |
//+------------------------------------------------------------------+
#property copyright "XAU ATS"
#property link      ""
#property version   "1.00"
#property strict

#property indicator_chart_window
#property indicator_buffers 6
#property indicator_plots   5

// --- Price window plots ---
#property indicator_label1  "VWAP"
#property indicator_type1   DRAW_LINE
#property indicator_color1  clrDodgerBlue
#property indicator_style1  STYLE_SOLID
#property indicator_width1  2

#property indicator_label2  "ATR Upper"
#property indicator_type2   DRAW_LINE
#property indicator_color2  clrOrange
#property indicator_style2  STYLE_DOT
#property indicator_width2  1

#property indicator_label3  "ATR Lower"
#property indicator_type3   DRAW_LINE
#property indicator_color3  clrOrange
#property indicator_style3  STYLE_DOT
#property indicator_width3  1

// --- Separate subwindow for Z-Score ---
#property indicator_separate_window
#property indicator_minimum -4.0
#property indicator_maximum  4.0

#property indicator_label4  "Z-Score"
#property indicator_type4   DRAW_LINE
#property indicator_color4  clrMagenta
#property indicator_style4  STYLE_SOLID
#property indicator_width4  2

#property indicator_label5  "OB Level"
#property indicator_type5   DRAW_LINE
#property indicator_color5  clrRed
#property indicator_style5  STYLE_DOT
#property indicator_width5  1

// Buffers
double VwapBuf[];
double AtrUpperBuf[];
double AtrLowerBuf[];
double ZScoreBuf[];
double OBLevel[];     // overbought +2.0
double OSLevel[];     // oversold  -2.0 (shown via horizontal line objects)

// Parameters
input int ZScoreWindow = 50;       // Z-Score rolling window (bars)
input int ATRPeriod    = 14;       // ATR period
input double ATRMultSL = 1.5;      // ATR multiplier for bands
input string StateFile = "ats_live_state.json"; // Live state JSON file
input int RefreshSec   = 2;        // Refresh interval (seconds)

// Signal tracking
datetime g_lastArrowTime = 0;
int      g_lastPosition  = 0;   // last known position from JSON

//+------------------------------------------------------------------+
int OnInit()
  {
   SetIndexBuffer(0, VwapBuf,    INDICATOR_DATA);
   SetIndexBuffer(1, AtrUpperBuf, INDICATOR_DATA);
   SetIndexBuffer(2, AtrLowerBuf, INDICATOR_DATA);
   SetIndexBuffer(3, ZScoreBuf,  INDICATOR_DATA);
   SetIndexBuffer(4, OBLevel,    INDICATOR_DATA);

   PlotIndexSetInteger(4, PLOT_DRAW_TYPE, DRAW_LINE);

   // Second OB line is drawn as a horizontal object
   _DrawHLine("ATS_OS", -2.0, clrLime,  1);
   _DrawHLine("ATS_OB", +2.0, clrRed,   1);
   _DrawHLine("ATS_Z0",  0.0, clrGray,  0);

   EventSetTimer(RefreshSec);

   IndicatorSetString(INDICATOR_SHORTNAME,
     StringFormat("ATS Strategy [Z%d ATR%d]", ZScoreWindow, ATRPeriod));

   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   ObjectDelete(0, "ATS_OS");
   ObjectDelete(0, "ATS_OB");
   ObjectDelete(0, "ATS_Z0");
   EventKillTimer();
  }

//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
  {
   int start = MathMax(prev_calculated - 1, ZScoreWindow + ATRPeriod + 1);
   if(start < 0) start = 0;

   // --- 1. VWAP (resets daily) ---
   datetime curDate = 0;
   double   cumPV   = 0, cumVol = 0;
   for(int i = 0; i < rates_total; i++)
     {
      datetime dayStart = (datetime)(MathFloor((double)time[i] / 86400.0) * 86400);
      if(dayStart != curDate)
        { curDate = dayStart; cumPV = 0; cumVol = 0; }
      double tv = (double)tick_volume[i];
      if(tv < 1) tv = 1;
      cumPV  += close[i] * tv;
      cumVol += tv;
      VwapBuf[i] = cumPV / cumVol;
     }

   // --- 2. ATR bands ---
   for(int i = start; i < rates_total; i++)
     {
      double atr = _ATR(i, ATRPeriod, high, low, close, rates_total);
      AtrUpperBuf[i] = VwapBuf[i] + ATRMultSL * atr;
      AtrLowerBuf[i] = VwapBuf[i] - ATRMultSL * atr;
     }

   // --- 3. OU Z-Score ---
   for(int i = start; i < rates_total; i++)
     {
      if(i < ZScoreWindow) { ZScoreBuf[i] = 0; continue; }
      double sum = 0, sum2 = 0;
      for(int j = i - ZScoreWindow + 1; j <= i; j++)
          sum += close[j];
      double mu = sum / ZScoreWindow;
      for(int j = i - ZScoreWindow + 1; j <= i; j++)
          sum2 += (close[j] - mu) * (close[j] - mu);
      double sigma = MathSqrt(sum2 / ZScoreWindow);
      ZScoreBuf[i] = (sigma > 1e-9) ? (close[i] - mu) / sigma : 0;
      OBLevel[i]   = 2.0;   // constant +2 reference
     }

   // --- 4. Background regime shading from live state ---
   _UpdateRegimeBackground();

   return rates_total;
  }

//+------------------------------------------------------------------+
void OnTimer()
  {
   // Poll signal file and draw new arrows at current bar
   _DrawSignalArrows();
   ChartRedraw();
  }

//+------------------------------------------------------------------+
// Helpers
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
void _DrawHLine(const string name, double price,
                color clr, int style)
  {
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_HLINE, 1, 0, price);  // subwindow 1
   ObjectSetDouble(0, name, OBJPROP_PRICE, price);
   ObjectSetInteger(0, name, OBJPROP_COLOR,    clr);
   ObjectSetInteger(0, name, OBJPROP_STYLE,    style);
   ObjectSetInteger(0, name, OBJPROP_WIDTH,    1);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN,   true);
  }

//+------------------------------------------------------------------+
void _DrawSignalArrows()
  {
   // Read ats_live_state.json from MT5 Common Files
   string path = StateFile;
   int fh = FileOpen(path, FILE_READ | FILE_TXT | FILE_COMMON | FILE_ANSI);
   if(fh == INVALID_HANDLE) return;

   string raw = "";
   while(!FileIsEnding(fh))
      raw += FileReadString(fh);
   FileClose(fh);

   // Parse "position": X for this symbol
   string symKey = "\"" + _Symbol + "\"";
   int symPos = StringFind(raw, symKey);
   if(symPos < 0) return;

   string chunk = StringSubstr(raw, symPos, 400);
   int posKey = StringFind(chunk, "\"position\":");
   if(posKey < 0) return;

   string posStr = StringSubstr(chunk, posKey + 11, 4);
   StringTrimLeft(posStr); StringTrimRight(posStr);
   int newPos = (int)StringToInteger(StringSubstr(posStr, 0, 2));

   // Only draw arrow on position CHANGE
   if(newPos == g_lastPosition) return;

   datetime t = TimeCurrent();
   if(t == g_lastArrowTime) return;  // already drew this bar

   double price = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   string arrowName = StringFormat("ATS_SIG_%I64d", (long)t);

   if(newPos == 1)
     {  // Long signal — up arrow below bar
      ObjectCreate(0, arrowName, OBJ_ARROW, 0, t, price * 0.9999);
      ObjectSetInteger(0, arrowName, OBJPROP_ARROWCODE, 233);   // ↑
      ObjectSetInteger(0, arrowName, OBJPROP_COLOR,    clrLime);
      ObjectSetInteger(0, arrowName, OBJPROP_WIDTH,    3);
     }
   else if(newPos == -1)
     {  // Short signal — down arrow above bar
      ObjectCreate(0, arrowName, OBJ_ARROW, 0, t, price * 1.0001);
      ObjectSetInteger(0, arrowName, OBJPROP_ARROWCODE, 234);   // ↓
      ObjectSetInteger(0, arrowName, OBJPROP_COLOR,    clrRed);
      ObjectSetInteger(0, arrowName, OBJPROP_WIDTH,    3);
     }
   else if(newPos == 0 && g_lastPosition != 0)
     {  // Position closed — circle
      ObjectCreate(0, arrowName, OBJ_ARROW, 0, t, price);
      ObjectSetInteger(0, arrowName, OBJPROP_ARROWCODE, 159);   // ○
      ObjectSetInteger(0, arrowName, OBJPROP_COLOR,    clrYellow);
      ObjectSetInteger(0, arrowName, OBJPROP_WIDTH,    2);
     }

   if(ObjectFind(0, arrowName) >= 0)
     {
      ObjectSetInteger(0, arrowName, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, arrowName, OBJPROP_HIDDEN,  false);
     }

   g_lastPosition  = newPos;
   g_lastArrowTime = t;
  }

//+------------------------------------------------------------------+
void _UpdateRegimeBackground()
  {
   string path = StateFile;
   int fh = FileOpen(path, FILE_READ | FILE_TXT | FILE_COMMON | FILE_ANSI);
   if(fh == INVALID_HANDLE) return;

   string raw = "";
   while(!FileIsEnding(fh))
      raw += FileReadString(fh);
   FileClose(fh);

   // Parse "regime": X for this symbol
   string symKey = "\"" + _Symbol + "\"";
   int symPos = StringFind(raw, symKey);
   if(symPos < 0) return;
   string chunk = StringSubstr(raw, symPos, 300);
   int regKey = StringFind(chunk, "\"regime\":");
   if(regKey < 0) return;
   int regime = (int)StringToInteger(StringSubstr(chunk, regKey + 9, 2));

   // 0=RANGE (grey bg), 1=TREND (light blue bg)
   color bgColor = (regime == 1) ? C'220,235,255' : C'240,240,240';
   ChartSetInteger(0, CHART_COLOR_BACKGROUND, bgColor);
  }
//+------------------------------------------------------------------+
