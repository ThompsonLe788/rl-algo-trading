//+------------------------------------------------------------------+
//| ATS_SMC.mq5 — Smart Money Concepts indicator                    |
//| Market Structure (BOS/CHoCH), Order Blocks, FVG, Liquidity,     |
//| Premium/Discount zones, on-chart dashboard                       |
//+------------------------------------------------------------------+
#property copyright "ATS"
#property version   "1.00"
#property strict
#property indicator_chart_window
#property indicator_plots 0

#define OBJ_PREFIX  "SMC_"

//+------------------------------------------------------------------+
//| Input parameters                                                  |
//+------------------------------------------------------------------+
//--- Structure detection
input int    SwingStrength   = 5;       // Bars each side for swing pivot
input int    LookbackBars    = 500;     // Historical bars to analyse

//--- Feature toggles
input bool   ShowBOS             = true;   // Show Break of Structure
input bool   ShowCHoCH           = true;   // Show Change of Character
input bool   ShowOB              = true;   // Show Order Blocks
input bool   ShowFVG             = true;   // Show Fair Value Gaps
input bool   ShowLiquidity       = true;   // Show Equal Highs/Lows ($$$)
input bool   ShowPremiumDiscount = true;   // Show Premium/Discount zones
input bool   ShowDashboard       = true;   // Show info panel

//--- Limits
input int    MaxOBCount    = 50;   // Max order blocks displayed
input int    MaxFVGCount   = 50;   // Max FVGs displayed

//--- Colors: Structure
input color  ClrBOS       = clrDodgerBlue;  // BOS line colour
input color  ClrCHoCH     = clrOrangeRed;   // CHoCH line colour

//--- Colors: Order Blocks (muted for dark chart)
input color  ClrBullOB    = C'15,50,15';     // Bullish OB fill (dark green)
input color  ClrBearOB    = C'55,15,15';     // Bearish OB fill (dark red)
input color  ClrBullOBBdr = clrLime;         // Bullish OB border
input color  ClrBearOBBdr = clrRed;          // Bearish OB border

//--- Colors: FVG (muted for dark chart)
input color  ClrBullFVG   = C'15,20,50';    // Bullish FVG fill (dark blue)
input color  ClrBearFVG   = C'55,30,10';    // Bearish FVG fill (dark orange)
input color  ClrBullFVGBdr = C'70,130,255';  // Bullish FVG border
input color  ClrBearFVGBdr = C'255,160,50';  // Bearish FVG border

//--- Colors: Liquidity
input color  ClrLiquidity = clrGold;        // EQH/EQL line colour

//--- Colors: Premium/Discount
input color  ClrPremium     = C'60,20,20';   // Premium zone (dark red)
input color  ClrDiscount    = C'20,50,20';   // Discount zone (dark green)
input color  ClrEquilibrium = C'180,180,0';  // Equilibrium line (yellow)

//--- Dashboard
input int    PanelX    = 10;             // Panel left margin (px)
input int    PanelY    = 30;             // Panel top margin (px)
input color  ClrBg     = C'20,20,35';    // Panel background
input color  ClrTitle  = clrGold;        // Panel title colour
input color  ClrLabel  = clrSilver;      // Panel label colour
input int    FontSz    = 9;              // Font size
input string FontName  = "Consolas";     // Font name

//--- Tolerance
input double EqhEqlTolerancePct = 0.05;  // EQH/EQL tolerance (% of price)

//+------------------------------------------------------------------+
//| Struct definitions                                                |
//+------------------------------------------------------------------+
struct SwingPoint
{
   int       barIndex;   // index (as-series=false, 0=oldest)
   datetime  time;       // bar time
   double    price;      // high for SH, low for SL
   int       type;       // +1 swing high, -1 swing low
   bool      broken;     // level broken by subsequent price
};

struct StructureBreak
{
   int       barIndex;      // bar of the break
   datetime  time;
   double    level;          // broken price level
   int       direction;      // +1 bullish, -1 bearish break
   bool      isCHoCH;        // false=BOS, true=CHoCH
   datetime  swingTime;      // time of the original swing
};

struct OrderBlock
{
   datetime  timeStart;
   datetime  timeEnd;
   double    high;
   double    low;
   int       direction;   // +1 bullish, -1 bearish
   bool      mitigated;
   int       barIndex;
};

struct FairValueGap
{
   datetime  timeStart;   // middle candle time
   datetime  timeEnd;
   double    upper;       // upper gap boundary
   double    lower;       // lower gap boundary
   int       direction;   // +1 bullish, -1 bearish
   bool      filled;
   int       barIndex;
};

struct LiquidityLevel
{
   double    price;
   int       type;        // +1=EQH, -1=EQL
   int       touchCount;
   datetime  firstTime;
   datetime  lastTime;
   bool      swept;
   datetime  sweepTime;
};

struct DashboardState
{
   int       marketBias;      // +1/-1/0
   string    trendLabel;      // "BULLISH"/"BEARISH"/"NEUTRAL"
   string    lastBreakLabel;  // "BOS Bull @ 2345.60"
   int       activeOBCount;
   int       activeFVGCount;
   int       totalBOS;
   int       totalCHoCH;
   int       totalSwings;
   string    nearestOBLabel;  // nearest unmitigated OB info
   int       liqEQHCount;     // equal highs clusters
   int       liqEQLCount;     // equal lows clusters
   string    zoneLabel;       // "PREMIUM"/"DISCOUNT"/"EQUILIBRIUM"
   double    swingHigh;
   double    swingLow;
   double    equilibrium;
};

//+------------------------------------------------------------------+
//| Globals                                                           |
//+------------------------------------------------------------------+
SwingPoint       g_swings[];
StructureBreak   g_breaks[];
OrderBlock       g_obs[];
FairValueGap     g_fvgs[];
LiquidityLevel   g_liquidity[];
DashboardState   g_dash;

int              g_trendDir;    // +1 bullish, -1 bearish, 0 neutral
int              g_prevBars;    // previous rates_total
datetime         g_lastBarTime;

//+------------------------------------------------------------------+
//| OnInit                                                            |
//+------------------------------------------------------------------+
int OnInit()
{
   g_trendDir   = 0;
   g_prevBars   = 0;
   g_lastBarTime = 0;

   ArrayResize(g_swings,    0);
   ArrayResize(g_breaks,    0);
   ArrayResize(g_obs,       0);
   ArrayResize(g_fvgs,      0);
   ArrayResize(g_liquidity, 0);

   g_dash.marketBias      = 0;
   g_dash.trendLabel      = "NEUTRAL";
   g_dash.lastBreakLabel  = "---";
   g_dash.activeOBCount   = 0;
   g_dash.activeFVGCount  = 0;
   g_dash.totalBOS        = 0;
   g_dash.totalCHoCH      = 0;
   g_dash.totalSwings     = 0;
   g_dash.nearestOBLabel  = "---";
   g_dash.liqEQHCount     = 0;
   g_dash.liqEQLCount     = 0;
   g_dash.zoneLabel       = "---";
   g_dash.swingHigh       = 0;
   g_dash.swingLow        = 0;
   g_dash.equilibrium     = 0;

   EventSetTimer(1);
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| OnDeinit                                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   ObjectsDeleteAll(0, OBJ_PREFIX);
   ChartRedraw(0);
}

//+------------------------------------------------------------------+
//| OnCalculate — main analytical engine (runs on new bar only)       |
//+------------------------------------------------------------------+
int OnCalculate(const int rates_total, const int prev_calculated,
                const datetime &time[], const double &open[],
                const double &high[], const double &low[],
                const double &close[], const long &vol[],
                const long &spread[], const int &real_volume[])
{
   if(rates_total < SwingStrength * 2 + 10) return rates_total;

   //--- New bar detection
   if(rates_total == g_prevBars && time[rates_total - 1] == g_lastBarTime)
      return rates_total;
   g_prevBars   = rates_total;
   g_lastBarTime = time[rates_total - 1];

   int startBar = MathMax(0, rates_total - LookbackBars);

   //--- Step 1: Detect swing points
   _DetectSwings(startBar, rates_total, time, high, low);

   //--- Step 2: Analyse market structure (BOS / CHoCH)
   _AnalyzeStructure(rates_total, close);

   //--- Step 3: Detect order blocks
   if(ShowOB)  _DetectOrderBlocks(startBar, rates_total, time, open, high, low, close);

   //--- Step 4: Detect fair value gaps
   if(ShowFVG) _DetectFVGs(startBar, rates_total, time, high, low);

   //--- Step 5: Detect liquidity levels
   if(ShowLiquidity) _DetectLiquidity();

   //--- Step 6: Update mitigation / fill status
   _UpdateMitigation(rates_total, time, high, low, close);

   //--- Step 7: Calculate premium / discount
   _CalcPremiumDiscount(rates_total, close);

   //--- Step 8: Build dashboard state
   _BuildDashboardState();

   //--- Step 9: Draw everything
   _DrawAll(rates_total, time);

   ChartRedraw(0);
   return rates_total;
}

//+------------------------------------------------------------------+
//| OnTimer — refresh dashboard                                       |
//+------------------------------------------------------------------+
void OnTimer()
{
   if(ShowDashboard) _DrawDashboard();
   ChartRedraw(0);
}

//+------------------------------------------------------------------+
//|                  ANALYSIS FUNCTIONS                                |
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
//| Detect swing highs and swing lows (fractal pivot)                 |
//+------------------------------------------------------------------+
void _DetectSwings(int startBar, int total,
                   const datetime &time[], const double &high[], const double &low[])
{
   ArrayResize(g_swings, 0);

   int from = MathMax(startBar, SwingStrength);
   int to   = total - SwingStrength - 1;

   for(int i = from; i <= to; i++)
   {
      //--- Check swing high
      bool isSH = true;
      for(int k = 1; k <= SwingStrength; k++)
      {
         if(high[i] < high[i - k] || high[i] < high[i + k])
         { isSH = false; break; }
      }
      if(isSH)
      {
         int idx = ArraySize(g_swings);
         ArrayResize(g_swings, idx + 1);
         g_swings[idx].barIndex = i;
         g_swings[idx].time     = time[i];
         g_swings[idx].price    = high[i];
         g_swings[idx].type     = +1;
         g_swings[idx].broken   = false;
      }

      //--- Check swing low
      bool isSL = true;
      for(int k = 1; k <= SwingStrength; k++)
      {
         if(low[i] > low[i - k] || low[i] > low[i + k])
         { isSL = false; break; }
      }
      if(isSL)
      {
         int idx = ArraySize(g_swings);
         ArrayResize(g_swings, idx + 1);
         g_swings[idx].barIndex = i;
         g_swings[idx].time     = time[i];
         g_swings[idx].price    = low[i];
         g_swings[idx].type     = -1;
         g_swings[idx].broken   = false;
      }
   }
}

//+------------------------------------------------------------------+
//| Analyse market structure — determine BOS and CHoCH                |
//+------------------------------------------------------------------+
void _AnalyzeStructure(int total, const double &close[])
{
   ArrayResize(g_breaks, 0);
   g_trendDir = 0;

   if(ArraySize(g_swings) < 2) return;

   //--- Track latest unbroken swing high and swing low
   int lastSH = -1;   // index into g_swings
   int lastSL = -1;

   for(int s = 0; s < ArraySize(g_swings); s++)
   {
      if(g_swings[s].type == +1)
      {
         //--- Check if close has broken above previous swing high
         if(lastSH >= 0 && !g_swings[lastSH].broken)
         {
            //--- Scan bars between old SH and this new SH for a close above
            for(int b = g_swings[lastSH].barIndex + 1; b < g_swings[s].barIndex && b < total; b++)
            {
               if(close[b] > g_swings[lastSH].price)
               {
                  g_swings[lastSH].broken = true;

                  int idx = ArraySize(g_breaks);
                  ArrayResize(g_breaks, idx + 1);
                  g_breaks[idx].barIndex   = b;
                  g_breaks[idx].time       = 0; // set in draw
                  g_breaks[idx].level      = g_swings[lastSH].price;
                  g_breaks[idx].direction  = +1;
                  g_breaks[idx].isCHoCH    = (g_trendDir == -1);
                  g_breaks[idx].swingTime  = g_swings[lastSH].time;

                  g_trendDir = +1;
                  break;
               }
            }
         }
         lastSH = s;
      }
      else // swing low
      {
         if(lastSL >= 0 && !g_swings[lastSL].broken)
         {
            for(int b = g_swings[lastSL].barIndex + 1; b < g_swings[s].barIndex && b < total; b++)
            {
               if(close[b] < g_swings[lastSL].price)
               {
                  g_swings[lastSL].broken = true;

                  int idx = ArraySize(g_breaks);
                  ArrayResize(g_breaks, idx + 1);
                  g_breaks[idx].barIndex   = b;
                  g_breaks[idx].time       = 0;
                  g_breaks[idx].level      = g_swings[lastSL].price;
                  g_breaks[idx].direction  = -1;
                  g_breaks[idx].isCHoCH    = (g_trendDir == +1);
                  g_breaks[idx].swingTime  = g_swings[lastSL].time;

                  g_trendDir = -1;
                  break;
               }
            }
         }
         lastSL = s;
      }
   }

   //--- Also check unbroken swings against the most recent close
   int latestBar = total - 1;
   if(lastSH >= 0 && !g_swings[lastSH].broken && close[latestBar] > g_swings[lastSH].price)
   {
      g_swings[lastSH].broken = true;
      int idx = ArraySize(g_breaks);
      ArrayResize(g_breaks, idx + 1);
      g_breaks[idx].barIndex   = latestBar;
      g_breaks[idx].time       = 0;
      g_breaks[idx].level      = g_swings[lastSH].price;
      g_breaks[idx].direction  = +1;
      g_breaks[idx].isCHoCH    = (g_trendDir == -1);
      g_breaks[idx].swingTime  = g_swings[lastSH].time;
      g_trendDir = +1;
   }
   if(lastSL >= 0 && !g_swings[lastSL].broken && close[latestBar] < g_swings[lastSL].price)
   {
      g_swings[lastSL].broken = true;
      int idx = ArraySize(g_breaks);
      ArrayResize(g_breaks, idx + 1);
      g_breaks[idx].barIndex   = latestBar;
      g_breaks[idx].time       = 0;
      g_breaks[idx].level      = g_swings[lastSL].price;
      g_breaks[idx].direction  = -1;
      g_breaks[idx].isCHoCH    = (g_trendDir == +1);
      g_breaks[idx].swingTime  = g_swings[lastSL].time;
      g_trendDir = -1;
   }
}

//+------------------------------------------------------------------+
//| Detect order blocks — last opposite candle before structure break  |
//+------------------------------------------------------------------+
void _DetectOrderBlocks(int startBar, int total,
                        const datetime &time[], const double &open[],
                        const double &high[], const double &low[],
                        const double &close[])
{
   ArrayResize(g_obs, 0);

   for(int i = 0; i < ArraySize(g_breaks); i++)
   {
      int bBar = g_breaks[i].barIndex;

      if(g_breaks[i].direction == +1)
      {
         //--- Bullish break: find last bearish candle before the impulse
         for(int j = bBar - 1; j >= MathMax(startBar, bBar - 10); j--)
         {
            if(close[j] < open[j]) // bearish candle
            {
               int idx = ArraySize(g_obs);
               ArrayResize(g_obs, idx + 1);
               g_obs[idx].timeStart  = time[j];
               g_obs[idx].timeEnd    = time[total - 1];
               g_obs[idx].high       = high[j];
               g_obs[idx].low        = low[j];
               g_obs[idx].direction  = +1;
               g_obs[idx].mitigated  = false;
               g_obs[idx].barIndex   = j;
               break;
            }
         }
      }
      else // bearish break
      {
         //--- Bearish break: find last bullish candle
         for(int j = bBar - 1; j >= MathMax(startBar, bBar - 10); j--)
         {
            if(close[j] > open[j]) // bullish candle
            {
               int idx = ArraySize(g_obs);
               ArrayResize(g_obs, idx + 1);
               g_obs[idx].timeStart  = time[j];
               g_obs[idx].timeEnd    = time[total - 1];
               g_obs[idx].high       = high[j];
               g_obs[idx].low        = low[j];
               g_obs[idx].direction  = -1;
               g_obs[idx].mitigated  = false;
               g_obs[idx].barIndex   = j;
               break;
            }
         }
      }
   }

   //--- Trim to MaxOBCount (keep newest)
   while(ArraySize(g_obs) > MaxOBCount)
   {
      //--- Remove the oldest (index 0)
      int sz = ArraySize(g_obs);
      for(int j = 0; j < sz - 1; j++)
         g_obs[j] = g_obs[j + 1];
      ArrayResize(g_obs, sz - 1);
   }
}

//+------------------------------------------------------------------+
//| Detect fair value gaps — 3-candle imbalance pattern               |
//+------------------------------------------------------------------+
void _DetectFVGs(int startBar, int total,
                 const datetime &time[], const double &high[], const double &low[])
{
   ArrayResize(g_fvgs, 0);

   for(int i = MathMax(startBar + 2, 2); i < total; i++)
   {
      //--- Bullish FVG: low[i] > high[i-2]
      if(low[i] > high[i - 2])
      {
         int idx = ArraySize(g_fvgs);
         ArrayResize(g_fvgs, idx + 1);
         g_fvgs[idx].timeStart  = time[i - 1];
         g_fvgs[idx].timeEnd    = time[total - 1];
         g_fvgs[idx].upper      = low[i];
         g_fvgs[idx].lower      = high[i - 2];
         g_fvgs[idx].direction  = +1;
         g_fvgs[idx].filled     = false;
         g_fvgs[idx].barIndex   = i - 1;
      }

      //--- Bearish FVG: high[i] < low[i-2]
      if(high[i] < low[i - 2])
      {
         int idx = ArraySize(g_fvgs);
         ArrayResize(g_fvgs, idx + 1);
         g_fvgs[idx].timeStart  = time[i - 1];
         g_fvgs[idx].timeEnd    = time[total - 1];
         g_fvgs[idx].upper      = low[i - 2];
         g_fvgs[idx].lower      = high[i];
         g_fvgs[idx].direction  = -1;
         g_fvgs[idx].filled     = false;
         g_fvgs[idx].barIndex   = i - 1;
      }
   }

   //--- Trim to MaxFVGCount (keep newest)
   while(ArraySize(g_fvgs) > MaxFVGCount)
   {
      int sz = ArraySize(g_fvgs);
      for(int j = 0; j < sz - 1; j++)
         g_fvgs[j] = g_fvgs[j + 1];
      ArrayResize(g_fvgs, sz - 1);
   }
}

//+------------------------------------------------------------------+
//| Detect liquidity levels — clusters of equal highs / equal lows    |
//+------------------------------------------------------------------+
void _DetectLiquidity()
{
   ArrayResize(g_liquidity, 0);

   for(int s = 0; s < ArraySize(g_swings); s++)
   {
      double tol = g_swings[s].price * EqhEqlTolerancePct / 100.0;
      bool found = false;

      //--- Check if this swing clusters with an existing liquidity level
      for(int l = 0; l < ArraySize(g_liquidity); l++)
      {
         if(g_liquidity[l].type != g_swings[s].type) continue;
         if(MathAbs(g_liquidity[l].price - g_swings[s].price) <= tol)
         {
            //--- Merge: update average price, count, time range
            g_liquidity[l].price = (g_liquidity[l].price * g_liquidity[l].touchCount
                                    + g_swings[s].price)
                                   / (g_liquidity[l].touchCount + 1);
            g_liquidity[l].touchCount++;
            if(g_swings[s].time < g_liquidity[l].firstTime)
               g_liquidity[l].firstTime = g_swings[s].time;
            if(g_swings[s].time > g_liquidity[l].lastTime)
               g_liquidity[l].lastTime = g_swings[s].time;
            found = true;
            break;
         }
      }

      if(!found)
      {
         int idx = ArraySize(g_liquidity);
         ArrayResize(g_liquidity, idx + 1);
         g_liquidity[idx].price      = g_swings[s].price;
         g_liquidity[idx].type       = g_swings[s].type;
         g_liquidity[idx].touchCount = 1;
         g_liquidity[idx].firstTime  = g_swings[s].time;
         g_liquidity[idx].lastTime   = g_swings[s].time;
         g_liquidity[idx].swept      = false;
         g_liquidity[idx].sweepTime  = 0;
      }
   }
}

//+------------------------------------------------------------------+
//| Update mitigation (OB), fill (FVG), and sweep (liquidity)         |
//+------------------------------------------------------------------+
void _UpdateMitigation(int total, const datetime &time[],
                       const double &high[], const double &low[],
                       const double &close[])
{
   //--- OB mitigation: price returns into OB zone
   for(int i = 0; i < ArraySize(g_obs); i++)
   {
      if(g_obs[i].mitigated) continue;

      for(int b = g_obs[i].barIndex + 1; b < total; b++)
      {
         if(g_obs[i].direction == +1)
         {
            // Bullish OB mitigated when price dips into it
            if(low[b] <= g_obs[i].high)
            {
               g_obs[i].mitigated = true;
               g_obs[i].timeEnd   = time[b];
               break;
            }
         }
         else
         {
            // Bearish OB mitigated when price rises into it
            if(high[b] >= g_obs[i].low)
            {
               g_obs[i].mitigated = true;
               g_obs[i].timeEnd   = time[b];
               break;
            }
         }
      }
   }

   //--- FVG fill: price fills the gap
   for(int i = 0; i < ArraySize(g_fvgs); i++)
   {
      if(g_fvgs[i].filled) continue;

      for(int b = g_fvgs[i].barIndex + 2; b < total; b++)
      {
         if(g_fvgs[i].direction == +1)
         {
            // Bullish FVG filled when price dips below upper boundary (into the gap)
            if(low[b] <= g_fvgs[i].lower)
            {
               g_fvgs[i].filled  = true;
               g_fvgs[i].timeEnd = time[b];
               break;
            }
         }
         else
         {
            // Bearish FVG filled when price rises above lower boundary (into the gap)
            if(high[b] >= g_fvgs[i].upper)
            {
               g_fvgs[i].filled  = true;
               g_fvgs[i].timeEnd = time[b];
               break;
            }
         }
      }
   }

   //--- Liquidity sweep: wick through then close back
   for(int i = 0; i < ArraySize(g_liquidity); i++)
   {
      if(g_liquidity[i].swept)      continue;
      if(g_liquidity[i].touchCount < 2) continue;

      int lastBar = total - 1;
      if(g_liquidity[i].type == +1) // EQH
      {
         // Swept: high wicked above, close below
         if(high[lastBar] > g_liquidity[i].price && close[lastBar] < g_liquidity[i].price)
         {
            g_liquidity[i].swept     = true;
            g_liquidity[i].sweepTime = time[lastBar];
         }
      }
      else // EQL
      {
         // Swept: low wicked below, close above
         if(low[lastBar] < g_liquidity[i].price && close[lastBar] > g_liquidity[i].price)
         {
            g_liquidity[i].swept     = true;
            g_liquidity[i].sweepTime = time[lastBar];
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Calculate premium / discount zones                                |
//+------------------------------------------------------------------+
void _CalcPremiumDiscount(int total, const double &close[])
{
   //--- Find most recent significant swing high and swing low
   g_dash.swingHigh = 0;
   g_dash.swingLow  = DBL_MAX;

   for(int s = ArraySize(g_swings) - 1; s >= 0; s--)
   {
      if(g_swings[s].type == +1 && g_dash.swingHigh == 0)
         g_dash.swingHigh = g_swings[s].price;
      if(g_swings[s].type == -1 && g_dash.swingLow == DBL_MAX)
         g_dash.swingLow = g_swings[s].price;
      if(g_dash.swingHigh > 0 && g_dash.swingLow < DBL_MAX)
         break;
   }

   if(g_dash.swingHigh == 0 || g_dash.swingLow == DBL_MAX)
   {
      g_dash.zoneLabel = "---";
      return;
   }

   double eq = (g_dash.swingHigh + g_dash.swingLow) / 2.0;
   double cp = close[total - 1];

   if(cp > eq)      g_dash.zoneLabel = "PREMIUM";
   else if(cp < eq) g_dash.zoneLabel = "DISCOUNT";
   else             g_dash.zoneLabel = "EQUILIBRIUM";
}

//+------------------------------------------------------------------+
//| Build dashboard state from detected structures                    |
//+------------------------------------------------------------------+
void _BuildDashboardState()
{
   g_dash.marketBias = g_trendDir;
   g_dash.trendLabel = (g_trendDir == +1) ? "BULLISH"
                     : (g_trendDir == -1) ? "BEARISH" : "NEUTRAL";

   //--- Total swings
   g_dash.totalSwings = ArraySize(g_swings);

   //--- BOS / CHoCH counts + last break
   g_dash.totalBOS   = 0;
   g_dash.totalCHoCH = 0;
   int brkSz = ArraySize(g_breaks);
   for(int i = 0; i < brkSz; i++)
   {
      if(g_breaks[i].isCHoCH) g_dash.totalCHoCH++;
      else                     g_dash.totalBOS++;
   }

   if(brkSz > 0)
   {
      StructureBreak last;
      last = g_breaks[brkSz - 1];
      g_dash.lastBreakLabel = (last.isCHoCH ? "CHoCH " : "BOS ")
                            + (last.direction == +1 ? "Bull" : "Bear")
                            + " @ " + DoubleToString(last.level, (int)_Digits);
   }
   else
   {
      g_dash.lastBreakLabel = "---";
   }

   //--- Count unmitigated OBs + nearest OB
   g_dash.activeOBCount = 0;
   g_dash.nearestOBLabel = "---";
   double nearestOBDist = DBL_MAX;
   double lastClose = 0;
   if(g_dash.swingHigh > 0) lastClose = (g_dash.swingHigh + g_dash.swingLow) / 2.0;

   for(int i = 0; i < ArraySize(g_obs); i++)
   {
      if(!g_obs[i].mitigated)
      {
         g_dash.activeOBCount++;
         double mid = (g_obs[i].high + g_obs[i].low) / 2.0;
         double dist = MathAbs(mid - lastClose);
         if(dist < nearestOBDist)
         {
            nearestOBDist = dist;
            g_dash.nearestOBLabel = (g_obs[i].direction == +1 ? "Bull " : "Bear ")
                                  + DoubleToString(g_obs[i].low, (int)_Digits) + "-"
                                  + DoubleToString(g_obs[i].high, (int)_Digits);
         }
      }
   }

   //--- Count unfilled FVGs
   g_dash.activeFVGCount = 0;
   for(int i = 0; i < ArraySize(g_fvgs); i++)
      if(!g_fvgs[i].filled) g_dash.activeFVGCount++;

   //--- Liquidity EQH/EQL counts
   g_dash.liqEQHCount = 0;
   g_dash.liqEQLCount = 0;
   for(int i = 0; i < ArraySize(g_liquidity); i++)
   {
      if(g_liquidity[i].touchCount < 2) continue;
      if(g_liquidity[i].type == +1) g_dash.liqEQHCount++;
      else                           g_dash.liqEQLCount++;
   }

   //--- Equilibrium
   if(g_dash.swingHigh > 0 && g_dash.swingLow > 0 && g_dash.swingLow < DBL_MAX)
      g_dash.equilibrium = (g_dash.swingHigh + g_dash.swingLow) / 2.0;
   else
      g_dash.equilibrium = 0;
}

//+------------------------------------------------------------------+
//|                  DRAWING FUNCTIONS                                 |
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
//| Master draw dispatcher                                            |
//+------------------------------------------------------------------+
void _DrawAll(int total, const datetime &time[])
{
   //--- Clean all chart objects — full redraw
   ObjectsDeleteAll(0, OBJ_PREFIX);

   if(ShowBOS || ShowCHoCH)     _DrawStructure(total, time);
   if(ShowOB)                    _DrawOrderBlocks(total, time);
   if(ShowFVG)                   _DrawFVGs(total, time);
   if(ShowLiquidity)             _DrawLiquidity(total, time);
   if(ShowPremiumDiscount)       _DrawPremiumDiscount(total, time);
   if(ShowDashboard)             _DrawDashboard();
}

//+------------------------------------------------------------------+
//| Draw BOS and CHoCH lines with text labels                         |
//+------------------------------------------------------------------+
void _DrawStructure(int total, const datetime &time[])
{
   for(int i = 0; i < ArraySize(g_breaks); i++)
   {
      bool isCH = g_breaks[i].isCHoCH;

      //--- Filter based on ShowBOS / ShowCHoCH
      if(isCH  && !ShowCHoCH) continue;
      if(!isCH && !ShowBOS)   continue;

      string suffix = (isCH ? "CHO_" : "BOS_") + IntegerToString(i);
      string lineName = OBJ_PREFIX + suffix;

      //--- Resolve bar time for break point
      datetime breakTime = (g_breaks[i].barIndex < total)
                         ? time[g_breaks[i].barIndex] : time[total - 1];
      datetime swingTime = g_breaks[i].swingTime;

      //--- Trend line at the broken level
      ObjectCreate(0, lineName, OBJ_TREND, 0,
                   swingTime, g_breaks[i].level,
                   breakTime, g_breaks[i].level);
      ObjectSetInteger(0, lineName, OBJPROP_COLOR, isCH ? ClrCHoCH : ClrBOS);
      ObjectSetInteger(0, lineName, OBJPROP_STYLE, isCH ? STYLE_SOLID : STYLE_DASH);
      ObjectSetInteger(0, lineName, OBJPROP_WIDTH, isCH ? 2 : 1);
      ObjectSetInteger(0, lineName, OBJPROP_RAY_RIGHT, false);
      ObjectSetInteger(0, lineName, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, lineName, OBJPROP_BACK, false);

      //--- Text label at midpoint
      string lblName = OBJ_PREFIX + "LBL_S_" + IntegerToString(i);
      datetime midTime = (datetime)((long)swingTime + ((long)breakTime - (long)swingTime) / 2);

      ObjectCreate(0, lblName, OBJ_TEXT, 0, midTime, g_breaks[i].level);
      ObjectSetString(0, lblName, OBJPROP_TEXT, isCH ? "CHoCH" : "BOS");
      ObjectSetInteger(0, lblName, OBJPROP_COLOR, isCH ? ClrCHoCH : ClrBOS);
      ObjectSetString(0, lblName, OBJPROP_FONT, FontName);
      ObjectSetInteger(0, lblName, OBJPROP_FONTSIZE, FontSz - 1);
      ObjectSetInteger(0, lblName, OBJPROP_ANCHOR,
                       g_breaks[i].direction == +1 ? ANCHOR_LOWER : ANCHOR_UPPER);
      ObjectSetInteger(0, lblName, OBJPROP_SELECTABLE, false);
   }
}

//+------------------------------------------------------------------+
//| Draw order block rectangles                                       |
//+------------------------------------------------------------------+
void _DrawOrderBlocks(int total, const datetime &time[])
{
   for(int i = 0; i < ArraySize(g_obs); i++)
   {
      string name = OBJ_PREFIX + "OB_" + IntegerToString(i);

      datetime rightEdge = g_obs[i].mitigated ? g_obs[i].timeEnd : time[total - 1];
      bool isBull = (g_obs[i].direction == +1);

      //--- Filled rectangle (muted dark fill, behind candles)
      ObjectCreate(0, name, OBJ_RECTANGLE, 0,
                   g_obs[i].timeStart, g_obs[i].high,
                   rightEdge,          g_obs[i].low);

      ObjectSetInteger(0, name, OBJPROP_COLOR, isBull ? ClrBullOB : ClrBearOB);
      ObjectSetInteger(0, name, OBJPROP_FILL, true);
      ObjectSetInteger(0, name, OBJPROP_BACK, true);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      if(g_obs[i].mitigated)
         ObjectSetInteger(0, name, OBJPROP_STYLE, STYLE_DOT);

      //--- Border lines (bright, visible on top of candles)
      string topName = OBJ_PREFIX + "OBT_" + IntegerToString(i);
      string botName = OBJ_PREFIX + "OBB_" + IntegerToString(i);
      color  bdrClr  = isBull ? ClrBullOBBdr : ClrBearOBBdr;
      int    bdrStyle = g_obs[i].mitigated ? STYLE_DOT : STYLE_SOLID;

      ObjectCreate(0, topName, OBJ_TREND, 0,
                   g_obs[i].timeStart, g_obs[i].high, rightEdge, g_obs[i].high);
      ObjectSetInteger(0, topName, OBJPROP_COLOR, bdrClr);
      ObjectSetInteger(0, topName, OBJPROP_STYLE, bdrStyle);
      ObjectSetInteger(0, topName, OBJPROP_WIDTH, 1);
      ObjectSetInteger(0, topName, OBJPROP_RAY_RIGHT, false);
      ObjectSetInteger(0, topName, OBJPROP_SELECTABLE, false);

      ObjectCreate(0, botName, OBJ_TREND, 0,
                   g_obs[i].timeStart, g_obs[i].low, rightEdge, g_obs[i].low);
      ObjectSetInteger(0, botName, OBJPROP_COLOR, bdrClr);
      ObjectSetInteger(0, botName, OBJPROP_STYLE, bdrStyle);
      ObjectSetInteger(0, botName, OBJPROP_WIDTH, 1);
      ObjectSetInteger(0, botName, OBJPROP_RAY_RIGHT, false);
      ObjectSetInteger(0, botName, OBJPROP_SELECTABLE, false);

      //--- Label (bright colour for visibility)
      string lblName = OBJ_PREFIX + "LBL_OB_" + IntegerToString(i);
      ObjectCreate(0, lblName, OBJ_TEXT, 0, g_obs[i].timeStart, g_obs[i].high);

      string marker = isBull ? "OB \x25B2" : "OB \x25BC";
      if(g_obs[i].mitigated) marker += " [M]";

      ObjectSetString(0, lblName, OBJPROP_TEXT, marker);
      ObjectSetInteger(0, lblName, OBJPROP_COLOR, bdrClr);
      ObjectSetString(0, lblName, OBJPROP_FONT, FontName);
      ObjectSetInteger(0, lblName, OBJPROP_FONTSIZE, FontSz - 1);
      ObjectSetInteger(0, lblName, OBJPROP_ANCHOR,
                       isBull ? ANCHOR_LEFT_LOWER : ANCHOR_LEFT_UPPER);
      ObjectSetInteger(0, lblName, OBJPROP_SELECTABLE, false);
   }
}

//+------------------------------------------------------------------+
//| Draw fair value gap rectangles                                    |
//+------------------------------------------------------------------+
void _DrawFVGs(int total, const datetime &time[])
{
   for(int i = 0; i < ArraySize(g_fvgs); i++)
   {
      string name = OBJ_PREFIX + "FVG_" + IntegerToString(i);
      bool isBull = (g_fvgs[i].direction == +1);

      datetime rightEdge = g_fvgs[i].filled ? g_fvgs[i].timeEnd : time[total - 1];

      //--- Filled rectangle (muted dark fill)
      ObjectCreate(0, name, OBJ_RECTANGLE, 0,
                   g_fvgs[i].timeStart, g_fvgs[i].upper,
                   rightEdge,           g_fvgs[i].lower);

      ObjectSetInteger(0, name, OBJPROP_COLOR, isBull ? ClrBullFVG : ClrBearFVG);
      ObjectSetInteger(0, name, OBJPROP_FILL, true);
      ObjectSetInteger(0, name, OBJPROP_BACK, true);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      if(g_fvgs[i].filled)
         ObjectSetInteger(0, name, OBJPROP_STYLE, STYLE_DOT);

      //--- Border lines (bright, on top)
      color  bdrClr  = isBull ? ClrBullFVGBdr : ClrBearFVGBdr;
      int    bdrStyle = g_fvgs[i].filled ? STYLE_DOT : STYLE_DASH;

      string topName = OBJ_PREFIX + "FVGT_" + IntegerToString(i);
      ObjectCreate(0, topName, OBJ_TREND, 0,
                   g_fvgs[i].timeStart, g_fvgs[i].upper, rightEdge, g_fvgs[i].upper);
      ObjectSetInteger(0, topName, OBJPROP_COLOR, bdrClr);
      ObjectSetInteger(0, topName, OBJPROP_STYLE, bdrStyle);
      ObjectSetInteger(0, topName, OBJPROP_WIDTH, 1);
      ObjectSetInteger(0, topName, OBJPROP_RAY_RIGHT, false);
      ObjectSetInteger(0, topName, OBJPROP_SELECTABLE, false);

      string botName = OBJ_PREFIX + "FVGB_" + IntegerToString(i);
      ObjectCreate(0, botName, OBJ_TREND, 0,
                   g_fvgs[i].timeStart, g_fvgs[i].lower, rightEdge, g_fvgs[i].lower);
      ObjectSetInteger(0, botName, OBJPROP_COLOR, bdrClr);
      ObjectSetInteger(0, botName, OBJPROP_STYLE, bdrStyle);
      ObjectSetInteger(0, botName, OBJPROP_WIDTH, 1);
      ObjectSetInteger(0, botName, OBJPROP_RAY_RIGHT, false);
      ObjectSetInteger(0, botName, OBJPROP_SELECTABLE, false);

      //--- Label (bright)
      string lblName = OBJ_PREFIX + "LBL_FVG_" + IntegerToString(i);
      ObjectCreate(0, lblName, OBJ_TEXT, 0,
                   g_fvgs[i].timeStart, g_fvgs[i].upper);

      string marker = g_fvgs[i].filled ? "FVG [F]" : "FVG";
      ObjectSetString(0, lblName, OBJPROP_TEXT, marker);
      ObjectSetInteger(0, lblName, OBJPROP_COLOR, bdrClr);
      ObjectSetString(0, lblName, OBJPROP_FONT, FontName);
      ObjectSetInteger(0, lblName, OBJPROP_FONTSIZE, FontSz - 2);
      ObjectSetInteger(0, lblName, OBJPROP_ANCHOR, ANCHOR_LEFT_LOWER);
      ObjectSetInteger(0, lblName, OBJPROP_SELECTABLE, false);
   }
}

//+------------------------------------------------------------------+
//| Draw liquidity levels — equal highs/lows with "$$$"               |
//+------------------------------------------------------------------+
void _DrawLiquidity(int total, const datetime &time[])
{
   int drawn = 0;
   for(int i = 0; i < ArraySize(g_liquidity); i++)
   {
      if(g_liquidity[i].touchCount < 2) continue;

      string name = OBJ_PREFIX + "LIQ_" + IntegerToString(drawn);

      ObjectCreate(0, name, OBJ_TREND, 0,
                   g_liquidity[i].firstTime, g_liquidity[i].price,
                   time[total - 1],          g_liquidity[i].price);
      ObjectSetInteger(0, name, OBJPROP_STYLE, STYLE_DOT);
      ObjectSetInteger(0, name, OBJPROP_COLOR, ClrLiquidity);
      ObjectSetInteger(0, name, OBJPROP_WIDTH, 1);
      ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, false);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_BACK, false);

      //--- "$$$" label at the right side
      string lblName = OBJ_PREFIX + "LBL_LIQ_" + IntegerToString(drawn);
      ObjectCreate(0, lblName, OBJ_TEXT, 0,
                   time[total - 1], g_liquidity[i].price);

      string marker = "$$$";
      if(g_liquidity[i].type == +1)   marker += " EQH";
      else                             marker += " EQL";
      if(g_liquidity[i].swept)         marker += " [Swept]";

      ObjectSetString(0, lblName, OBJPROP_TEXT, marker);
      ObjectSetInteger(0, lblName, OBJPROP_COLOR, ClrLiquidity);
      ObjectSetString(0, lblName, OBJPROP_FONT, FontName);
      ObjectSetInteger(0, lblName, OBJPROP_FONTSIZE, FontSz - 1);
      ObjectSetInteger(0, lblName, OBJPROP_ANCHOR, ANCHOR_LEFT);
      ObjectSetInteger(0, lblName, OBJPROP_SELECTABLE, false);

      drawn++;
   }
}

//+------------------------------------------------------------------+
//| Draw premium/discount zone shading                                |
//+------------------------------------------------------------------+
void _DrawPremiumDiscount(int total, const datetime &time[])
{
   if(g_dash.swingHigh == 0 || g_dash.swingLow == DBL_MAX || g_dash.swingLow == 0)
      return;

   double eq = (g_dash.swingHigh + g_dash.swingLow) / 2.0;

   //--- Time range: last ~100 bars for visual clarity
   datetime t1 = time[MathMax(0, total - 100)];
   datetime t2 = time[total - 1];

   //--- Premium zone (equilibrium → swing high)
   string premName = OBJ_PREFIX + "PD_PREM";
   ObjectCreate(0, premName, OBJ_RECTANGLE, 0,
                t1, g_dash.swingHigh, t2, eq);
   ObjectSetInteger(0, premName, OBJPROP_COLOR, ClrPremium);
   ObjectSetInteger(0, premName, OBJPROP_FILL, true);
   ObjectSetInteger(0, premName, OBJPROP_BACK, true);
   ObjectSetInteger(0, premName, OBJPROP_SELECTABLE, false);

   //--- Discount zone (equilibrium → swing low)
   string discName = OBJ_PREFIX + "PD_DISC";
   ObjectCreate(0, discName, OBJ_RECTANGLE, 0,
                t1, eq, t2, g_dash.swingLow);
   ObjectSetInteger(0, discName, OBJPROP_COLOR, ClrDiscount);
   ObjectSetInteger(0, discName, OBJPROP_FILL, true);
   ObjectSetInteger(0, discName, OBJPROP_BACK, true);
   ObjectSetInteger(0, discName, OBJPROP_SELECTABLE, false);

   //--- Equilibrium line
   string eqName = OBJ_PREFIX + "PD_EQ";
   ObjectCreate(0, eqName, OBJ_TREND, 0, t1, eq, t2, eq);
   ObjectSetInteger(0, eqName, OBJPROP_STYLE, STYLE_DASHDOTDOT);
   ObjectSetInteger(0, eqName, OBJPROP_COLOR, ClrEquilibrium);
   ObjectSetInteger(0, eqName, OBJPROP_WIDTH, 1);
   ObjectSetInteger(0, eqName, OBJPROP_RAY_RIGHT, false);
   ObjectSetInteger(0, eqName, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, eqName, OBJPROP_BACK, false);

   //--- Labels — positioned at right edge, large bold text
   int lblFontSz = FontSz + 3;
   datetime tMid = time[MathMax(0, total - 50)];

   string premLbl = OBJ_PREFIX + "LBL_PD_P";
   ObjectCreate(0, premLbl, OBJ_TEXT, 0, tMid, (g_dash.swingHigh + eq) / 2.0);
   ObjectSetString(0, premLbl, OBJPROP_TEXT, "P R E M I U M");
   ObjectSetInteger(0, premLbl, OBJPROP_COLOR, C'220,90,90');
   ObjectSetString(0, premLbl, OBJPROP_FONT, "Arial Bold");
   ObjectSetInteger(0, premLbl, OBJPROP_FONTSIZE, lblFontSz);
   ObjectSetInteger(0, premLbl, OBJPROP_ANCHOR, ANCHOR_CENTER);
   ObjectSetInteger(0, premLbl, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, premLbl, OBJPROP_BACK, true);

   string discLbl = OBJ_PREFIX + "LBL_PD_D";
   ObjectCreate(0, discLbl, OBJ_TEXT, 0, tMid, (g_dash.swingLow + eq) / 2.0);
   ObjectSetString(0, discLbl, OBJPROP_TEXT, "D I S C O U N T");
   ObjectSetInteger(0, discLbl, OBJPROP_COLOR, C'90,200,90');
   ObjectSetString(0, discLbl, OBJPROP_FONT, "Arial Bold");
   ObjectSetInteger(0, discLbl, OBJPROP_FONTSIZE, lblFontSz);
   ObjectSetInteger(0, discLbl, OBJPROP_ANCHOR, ANCHOR_CENTER);
   ObjectSetInteger(0, discLbl, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, discLbl, OBJPROP_BACK, true);

   string eqLbl = OBJ_PREFIX + "LBL_PD_E";
   ObjectCreate(0, eqLbl, OBJ_TEXT, 0, tMid, eq);
   ObjectSetString(0, eqLbl, OBJPROP_TEXT, "── EQUILIBRIUM  " + DoubleToString(eq, (int)_Digits) + " ──");
   ObjectSetInteger(0, eqLbl, OBJPROP_COLOR, ClrEquilibrium);
   ObjectSetString(0, eqLbl, OBJPROP_FONT, "Arial Bold");
   ObjectSetInteger(0, eqLbl, OBJPROP_FONTSIZE, FontSz);
   ObjectSetInteger(0, eqLbl, OBJPROP_ANCHOR, ANCHOR_CENTER);
   ObjectSetInteger(0, eqLbl, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, eqLbl, OBJPROP_BACK, true);
}

//+------------------------------------------------------------------+
//|                  DASHBOARD PANEL                                   |
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
//| Create/update a pixel-anchored label (left-upper corner)          |
//+------------------------------------------------------------------+
void _Text(const string name, const string text, int x, int y,
           color clr, int sz = -1)
{
   string full = OBJ_PREFIX + name;
   if(sz < 0) sz = FontSz;

   if(ObjectFind(0, full) < 0)
      ObjectCreate(0, full, OBJ_LABEL, 0, 0, 0);

   ObjectSetInteger(0, full, OBJPROP_CORNER,    CORNER_LEFT_UPPER);
   ObjectSetInteger(0, full, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, full, OBJPROP_YDISTANCE, y);
   ObjectSetString(0,  full, OBJPROP_TEXT,       text);
   ObjectSetString(0,  full, OBJPROP_FONT,       FontName);
   ObjectSetInteger(0, full, OBJPROP_FONTSIZE,   sz);
   ObjectSetInteger(0, full, OBJPROP_COLOR,      clr);
   ObjectSetInteger(0, full, OBJPROP_SELECTABLE,  false);
}

//+------------------------------------------------------------------+
//| Create/update background rectangle (pixel-anchored)               |
//+------------------------------------------------------------------+
void _Background(int x, int y, int w, int h)
{
   string name = OBJ_PREFIX + "bg";
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_RECTANGLE_LABEL, 0, 0, 0);

   ObjectSetInteger(0, name, OBJPROP_CORNER,      CORNER_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE,   x);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE,   y);
   ObjectSetInteger(0, name, OBJPROP_XSIZE,        w);
   ObjectSetInteger(0, name, OBJPROP_YSIZE,        h);
   ObjectSetInteger(0, name, OBJPROP_BGCOLOR,      ClrBg);
   ObjectSetInteger(0, name, OBJPROP_BORDER_TYPE,  BORDER_FLAT);
   ObjectSetInteger(0, name, OBJPROP_COLOR,        clrDimGray);
   ObjectSetInteger(0, name, OBJPROP_WIDTH,        1);
   ObjectSetInteger(0, name, OBJPROP_BACK,         false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE,   false);
}

//+------------------------------------------------------------------+
//| Draw the SMC dashboard panel (top-left)                           |
//+------------------------------------------------------------------+
void _DrawDashboard()
{
   int panelW = 280;
   int panelH = 350;
   int bx = PanelX;
   int by = PanelY;

   _Background(bx, by, panelW, panelH);

   int lh = 17;
   int tx = bx + 10;      // label x (from left)
   int vx = tx + 115;     // value x
   int ty = by + 8;

   //--- Title
   _Text("title", "SMC  " + _Symbol, tx, ty, ClrTitle, FontSz + 2);
   ty += lh + 4;

   //--- Divider 1
   _Text("div1", "--------------------------------------", tx, ty, C'60,60,80', FontSz - 2);
   ty += lh;

   //--- MARKET STRUCTURE section header
   _Text("hd_ms", "MARKET STRUCTURE", tx, ty, C'120,180,255', FontSz);
   ty += lh;

   //--- Bias
   color biasClr = (g_dash.marketBias ==  1) ? clrLimeGreen
                 : (g_dash.marketBias == -1) ? clrTomato : clrWhite;
   _Text("bias_lbl", "Bias        :", tx, ty, ClrLabel);
   _Text("bias_val", g_dash.trendLabel, vx, ty, biasClr, FontSz + 1);
   ty += lh;

   //--- Last Break
   _Text("brk_lbl", "Last Break  :", tx, ty, ClrLabel);
   color brkClr = (StringFind(g_dash.lastBreakLabel, "CHoCH") >= 0) ? ClrCHoCH : ClrBOS;
   if(g_dash.lastBreakLabel == "---") brkClr = clrGray;
   _Text("brk_val", g_dash.lastBreakLabel, vx, ty, brkClr);
   ty += lh;

   //--- BOS / CHoCH counts
   _Text("cnt_lbl", "BOS / CHoCH :", tx, ty, ClrLabel);
   _Text("cnt_val", IntegerToString(g_dash.totalBOS) + " / " + IntegerToString(g_dash.totalCHoCH),
         vx, ty, clrWhite);
   ty += lh;

   //--- Total swing pivots
   _Text("sw_lbl", "Swing Pivots:", tx, ty, ClrLabel);
   _Text("sw_val", IntegerToString(g_dash.totalSwings), vx, ty, clrWhite);
   ty += lh + 2;

   //--- Divider 2
   _Text("div2", "--------------------------------------", tx, ty, C'60,60,80', FontSz - 2);
   ty += lh;

   //--- ORDER BLOCKS & FVG section
   _Text("hd_ob", "ORDER BLOCKS & FVG", tx, ty, C'120,180,255', FontSz);
   ty += lh;

   //--- Active OBs
   color obClr = (g_dash.activeOBCount > 0) ? clrLimeGreen : clrGray;
   _Text("ob_lbl", "Active OB   :", tx, ty, ClrLabel);
   _Text("ob_val", IntegerToString(g_dash.activeOBCount), vx, ty, obClr);
   ty += lh;

   //--- Nearest OB
   _Text("nob_lbl", "Nearest OB  :", tx, ty, ClrLabel);
   _Text("nob_val", g_dash.nearestOBLabel, vx, ty, C'200,200,200');
   ty += lh;

   //--- Active FVGs
   color fvgClr = (g_dash.activeFVGCount > 0) ? clrLimeGreen : clrGray;
   _Text("fvg_lbl", "Active FVG  :", tx, ty, ClrLabel);
   _Text("fvg_val", IntegerToString(g_dash.activeFVGCount), vx, ty, fvgClr);
   ty += lh + 2;

   //--- Divider 3
   _Text("div3", "--------------------------------------", tx, ty, C'60,60,80', FontSz - 2);
   ty += lh;

   //--- LIQUIDITY & ZONES section
   _Text("hd_lz", "LIQUIDITY & ZONES", tx, ty, C'120,180,255', FontSz);
   ty += lh;

   //--- EQH / EQL
   _Text("liq_lbl", "EQH / EQL   :", tx, ty, ClrLabel);
   _Text("liq_val", IntegerToString(g_dash.liqEQHCount) + " / " + IntegerToString(g_dash.liqEQLCount),
         vx, ty, ClrLiquidity);
   ty += lh;

   //--- Zone
   color zoneClr = (g_dash.zoneLabel == "PREMIUM")  ? clrTomato
                 : (g_dash.zoneLabel == "DISCOUNT") ? clrLimeGreen : clrGray;
   _Text("zone_lbl", "Zone        :", tx, ty, ClrLabel);
   _Text("zone_val", g_dash.zoneLabel, vx, ty, zoneClr, FontSz + 1);
   ty += lh;

   //--- Equilibrium
   string eqStr = (g_dash.equilibrium > 0)
                ? DoubleToString(g_dash.equilibrium, (int)_Digits) : "---";
   _Text("eq_lbl", "Equilibrium :", tx, ty, ClrLabel);
   _Text("eq_val", eqStr, vx, ty, C'180,180,0');
   ty += lh;

   //--- Swing High
   string shStr = (g_dash.swingHigh > 0)
                ? DoubleToString(g_dash.swingHigh, (int)_Digits) : "---";
   _Text("sh_lbl", "Swing High  :", tx, ty, ClrLabel);
   _Text("sh_val", shStr, vx, ty, clrTomato);
   ty += lh;

   //--- Swing Low
   string slStr = (g_dash.swingLow > 0 && g_dash.swingLow < DBL_MAX)
                ? DoubleToString(g_dash.swingLow, (int)_Digits) : "---";
   _Text("sl_lbl", "Swing Low   :", tx, ty, ClrLabel);
   _Text("sl_val", slStr, vx, ty, clrLimeGreen);
}
//+------------------------------------------------------------------+
