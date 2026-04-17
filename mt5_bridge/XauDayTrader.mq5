//+------------------------------------------------------------------+
//| XauDayTrader.mq5 — Hybrid Python-MT5 EA for XAU/USD             |
//| Receives signals via ZeroMQ SUB (primary) or file IPC (fallback) |
//| LIMIT ORDERS ONLY — no market orders                             |
//| Hard EOD liquidation at 22:00 GMT                                |
//| Kill switch on 15% MDD                                          |
//+------------------------------------------------------------------+
//| REQUIRES: mql-zmq library                                       |
//|   https://github.com/dingmaotu/mql-zmq                          |
//|   Copy Include/Mql/ and Libraries/ into your MT5 data folder     |
//+------------------------------------------------------------------+
#property copyright "ATS"
#property version   "2.00"
#property strict

#include <Trade\Trade.mqh>
#include <Zmq/Zmq.mqh>

//--- Input parameters
input string   ZmqAddress        = "tcp://127.0.0.1:5555"; // ZMQ PUB address
input string   ZmqTopic          = "";                     // ZMQ topic (blank = use _Symbol)
input bool     UseZmq            = true;                   // true=ZMQ, false=file
input double   RiskFractionKelly = 0.1;                    // Kelly fraction (1/10)
input int      EodHourGMT        = 22;                     // Hard liquidation hour
input int      NoNewTradesHour   = 21;                     // No new trades after this
input double   AtrMultSL         = 2.0;                    // ATR multiplier for SL
input double   MaxDrawdownPct    = 15.0;                   // Kill switch drawdown %
input int      AtrPeriod         = 14;                     // ATR calculation period
input int      MagicNumberInput  = 0;                      // Magic number (0 = auto from symbol)
input string   SignalFile        = "";                     // Fallback file (blank = {symbol}_signal.json)
input int      ZmqRecvTimeoutMs  = 50;                     // ZMQ recv timeout (ms)
input int      HeartbeatMaxSec   = 30;                     // Max seconds without HB
input bool     EnableDeadManClose = false;                 // false = block signals only, SL handles risk

//--- Input parameters (trailing stop)
input double   TrailAtrMult      = 1.0;                    // Trailing stop ATR multiplier
input bool     EnableTrailingStop = false;                 // Disabled: partial-BE replaces trail

//--- Input parameters (partial TP / breakeven)
input bool     EnablePartialTP   = true;                   // At +1R: close 50%, SL → entry


//--- Input parameters (on-chart panel)
input bool     ShowPanel         = true;                   // Show EA status panel
input int      PanelX            = 5;                      // Panel right margin (px)
input int      PanelY            = 30;                     // Panel top margin (px)
input color    PnlClrBg          = C'20,20,35';            // Panel background
input color    PnlClrTitle       = clrGold;                // Panel title colour
input color    PnlClrLabel       = clrSilver;              // Panel label colour
input int      PnlFontSz         = 9;                      // Panel font size
input string   PnlFontName       = "Consolas";             // Panel font name

#define EA_PREFIX "XDT_"

//--- Global variables
CTrade trade;
double g_peakEquity;
int    g_atrHandle;
int    MagicNumber;         // resolved in OnInit (auto or manual)
string g_effectiveTopic;    // = ZmqTopic if set, else _Symbol
string g_effectiveSigFile;  // = SignalFile if set, else {symbol_lower}_signal.json

//--- Trailing stop state per position (keyed by ticket)
struct TrailState
{
   ulong  ticket;
   double trailLevel; // current trailing stop price
   int    side;       // +1 long, -1 short
};
TrailState g_trails[100];
int        g_trailCount = 0;

//--- Partial TP state per position
struct PartialTPState
{
   ulong  ticket;
   double tp1Level;      // price at which 50% is closed
   double entryPrice;    // original entry (used for breakeven SL move)
   double origLot;       // full lot at entry
   bool   partialDone;   // true once partial close executed
   int    side;          // +1 long, -1 short
};
PartialTPState g_partialTPs[100];
int            g_partialTPCount = 0;

//--- ZeroMQ objects
Context  g_zmqCtx;
Socket   g_zmqSub(g_zmqCtx, ZMQ_SUB);
bool     g_zmqConnected = false;
datetime g_lastHeartbeat = 0;
bool     g_deadManTriggered = false;  // prevents log spam every second

//+------------------------------------------------------------------+
struct SignalData
{
   int    side;       // 1=long, -1=short, 0=close
   double price;
   double sl;
   double tp;
   double sl_dist;    // SL distance in price (from Python SLTPOptimizer)
   double tp_dist;    // TP distance in price (from Python SLTPOptimizer)
   double lot;
   double win_prob;
   double rr;
   int    regime;
   double z_score;
   bool   is_heartbeat;
};

//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| Generate a deterministic magic number from symbol name            |
//| DJB2 hash seeded with 0xATS prefix → unique per symbol           |
//+------------------------------------------------------------------+
int _SymbolMagic(const string &sym)
{
   uint hash = 5381;  // DJB2 seed
   for(int i = 0; i < StringLen(sym); i++)
      hash = ((hash << 5) + hash) + StringGetCharacter(sym, i);
   // Keep positive 31-bit, prefix with 20 for readability
   return (int)(2000000000 + (hash % 100000000));
}

//+------------------------------------------------------------------+
int OnInit()
{
   //--- Resolve magic number: 0 = auto from symbol
   MagicNumber = (MagicNumberInput > 0) ? MagicNumberInput : _SymbolMagic(_Symbol);
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(10);
   // SYMBOL_FILLING_MODE bitmask: 1=FOK, 2=IOC, 0=RETURN only
   // For pending limit orders RETURN is correct (order stays until filled/cancelled).
   // Only fall back to FOK when the broker truly forbids RETURN (bitmask == 1).
   {
      int fillBits = (int)SymbolInfoInteger(_Symbol, SYMBOL_FILLING_MODE);
      if(fillBits == 1)   // broker supports FOK only
         trade.SetTypeFilling(ORDER_FILLING_FOK);
      else
         trade.SetTypeFilling(ORDER_FILLING_RETURN);
   }

   //--- Resolve dynamic topic: use ZmqTopic input or fall back to _Symbol
   g_effectiveTopic   = (StringLen(ZmqTopic) > 0) ? ZmqTopic : _Symbol;
   //--- Resolve signal file name: use SignalFile input or derive from symbol
   string symLower = _Symbol;
   StringToLower(symLower);
   g_effectiveSigFile = (StringLen(SignalFile) > 0) ? SignalFile
                        : symLower + "_signal.json";

   g_peakEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   // Use PERIOD_M1 to match the ATR timeframe used in Python feature engineering.
   // Training uses M1 ATR; using M5 here would create SL distance mismatch.
   g_atrHandle  = iATR(_Symbol, PERIOD_M1, AtrPeriod);

   if(g_atrHandle == INVALID_HANDLE)
   {
      Print("Failed to create ATR indicator");
      return INIT_FAILED;
   }

   //--- Initialize ZeroMQ subscriber
   if(UseZmq)
   {
      g_zmqSub.setReceiveTimeout(ZmqRecvTimeoutMs);
      g_zmqSub.setLinger(0);

      if(!g_zmqSub.connect(ZmqAddress))
      {
         Print("ZMQ connect failed: ", ZmqAddress);
         Print("Falling back to file-based IPC");
      }
      else
      {
         //--- Subscribe using resolved topic
         g_zmqSub.subscribe(g_effectiveTopic);
         g_zmqConnected = true;
         g_lastHeartbeat = TimeGMT();
         Print("ZMQ SUB connected to ", ZmqAddress,
               " topic=", g_effectiveTopic);
      }
   }

   EventSetTimer(1);
   Print("XauDayTrader v2 initialized. Symbol=", _Symbol,
         " Magic=", MagicNumber, " ZMQ=", (g_zmqConnected ? "ON" : "OFF"),
         " Topic=", g_effectiveTopic, " SigFile=", g_effectiveSigFile);
   _DrawEAPanel();
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   _DeleteEAPanel();
   if(g_atrHandle != INVALID_HANDLE)
      IndicatorRelease(g_atrHandle);

   //--- Cleanup ZMQ
   if(g_zmqConnected)
   {
      g_zmqSub.unsubscribe(g_effectiveTopic);
      g_zmqSub.disconnect(ZmqAddress);
      Print("ZMQ disconnected");
   }
}

//+------------------------------------------------------------------+
void OnTick()
{
   //--- Poll ZMQ on every tick for lowest latency
   if(g_zmqConnected)
      DrainZmqMessages();

   //--- Check partial TP levels before trailing stop
   if(EnablePartialTP)
      CheckPartialTP();

   //--- Update ATR trailing stop — only for positions registered after partial TP
   UpdateTrailingStops();
}

//+------------------------------------------------------------------+
//| ATR-based dynamic trailing stop                                  |
//| Long:  trail = max(trail_prev, mid - mult * ATR)                 |
//| Short: trail = min(trail_prev, mid + mult * ATR)                 |
//+------------------------------------------------------------------+
void UpdateTrailingStops()
{
   //--- Only trail positions registered in g_trails (after partial TP fires)
   if(g_trailCount <= 0) return;

   double atr[];
   if(CopyBuffer(g_atrHandle, 0, 0, 1, atr) <= 0)
      return;
   double atrVal = atr[0];
   if(atrVal <= 0)
      return;

   double mid = (SymbolInfoDouble(_Symbol, SYMBOL_BID) +
                 SymbolInfoDouble(_Symbol, SYMBOL_ASK)) / 2.0;

   for(int ti = g_trailCount - 1; ti >= 0; ti--)
   {
      ulong ticket = g_trails[ti].ticket;
      int   side   = g_trails[ti].side;

      if(!PositionSelectByTicket(ticket))
      {
         //--- Position closed — remove from trail list
         for(int k = ti; k < g_trailCount - 1; k++)
            g_trails[k] = g_trails[k + 1];
         g_trailCount--;
         continue;
      }
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;

      long   type  = PositionGetInteger(POSITION_TYPE);
      double curSL = PositionGetDouble(POSITION_SL);
      double curTP = PositionGetDouble(POSITION_TP);
      double newSL = curSL;

      if(type == POSITION_TYPE_BUY)
      {
         double candidate = mid - TrailAtrMult * atrVal;
         if(candidate > curSL + _Point)
            newSL = NormalizeDouble(candidate, _Digits);
      }
      else if(type == POSITION_TYPE_SELL)
      {
         double candidate = mid + TrailAtrMult * atrVal;
         if(curSL <= 0 || candidate < curSL - _Point)
            newSL = NormalizeDouble(candidate, _Digits);
      }

      if(newSL != curSL && newSL > 0)
      {
         if(!trade.PositionModify(ticket, newSL, curTP))
            Print("TrailingStop modify failed: ", trade.ResultRetcodeDescription(),
                  " ticket=", ticket, " newSL=", newSL);
         else
            g_trails[ti].trailLevel = newSL;
      }
   }
}

//+------------------------------------------------------------------+
void OnTimer()
{
   MqlDateTime dt;
   TimeGMT(dt);

   //--- Update peak equity
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   if(equity > g_peakEquity)
      g_peakEquity = equity;

   //--- 1. Kill switch: MDD check
   if(AccountDrawdownPct() > MaxDrawdownPct)
   {
      Print("KILL SWITCH: Drawdown ", DoubleToString(AccountDrawdownPct(), 1),
            "% exceeds ", DoubleToString(MaxDrawdownPct, 1), "%");
      CloseAll();
      ExpertRemove();
      return;
   }

   //--- 2. Hard EOD liquidation
   if(dt.hour >= EodHourGMT)
   {
      CloseAll();
      return;
   }

   //--- 3. Dead man's switch: close all if Python heartbeat lost
   if(g_zmqConnected && HeartbeatMaxSec > 0)
   {
      int hbAge = (int)(TimeGMT() - g_lastHeartbeat);
      if(hbAge > HeartbeatMaxSec)
      {
         if(!g_deadManTriggered)
         {
            if(EnableDeadManClose)
            {
               Print("DEAD MAN'S SWITCH: No heartbeat for ", hbAge,
                     "s — Python is down, closing all positions");
               CloseAll();
            }
            else
            {
               Print("DEAD MAN'S SWITCH: No heartbeat for ", hbAge,
                     "s — Python is down, blocking new signals (SL active)");
            }
            g_deadManTriggered = true;
         }
         return;   // block new signals until Python recovers
      }
      else
      {
         g_deadManTriggered = false;   // heartbeat restored — reset flag
      }
   }

   //--- 4. Poll ZMQ again on timer (in case no ticks arriving)
   if(g_zmqConnected)
      DrainZmqMessages();

   //--- 5. If ZMQ not available, try file-based fallback
   if(!g_zmqConnected)
   {
      if(dt.hour >= NoNewTradesHour)
         return;

      SignalData sig;
      if(ReadSignalFromFile(sig))
         ExecuteSignal(sig);
   }

   //--- 6. Refresh panel
   _DrawEAPanel();
}

//+------------------------------------------------------------------+
//| Drain all pending ZMQ messages (non-blocking)                    |
//+------------------------------------------------------------------+
void DrainZmqMessages()
{
   ZmqMsg msg;
   while(g_zmqSub.recv(msg, true))  // non-blocking
   {
      string raw = msg.getData();
      if(StringLen(raw) < 3)
         continue;

      //--- Strip topic prefix: "XAU {...}"
      int spacePos = StringFind(raw, " ");
      if(spacePos < 0)
         continue;

      string jsonStr = StringSubstr(raw, spacePos + 1);

      SignalData sig;
      if(!ParseSignalJson(jsonStr, sig))
         continue;

      //--- Handle heartbeat
      if(sig.is_heartbeat)
      {
         g_lastHeartbeat = TimeGMT();
         continue;
      }

      g_lastHeartbeat = TimeGMT();

      //--- Time-of-day filters
      MqlDateTime dt;
      TimeGMT(dt);
      if(dt.hour >= EodHourGMT)
      {
         CloseAll();
         continue;
      }

      //--- Execute signal
      ExecuteSignal(sig);
   }
}

//+------------------------------------------------------------------+
//| Execute a validated signal                                       |
//+------------------------------------------------------------------+
void ExecuteSignal(const SignalData &sig)
{
   //--- Python only sends LONG(+1) / SHORT(-1). side=0 is ignored — EA manages
   //    all exits via SL, TP, partial TP, trailing stop, and EOD CloseAll().
   if(sig.side == 0) return;

   //--- New signal: cancel ALL pending orders (both directions) to replace with fresh price.
   //    Filled positions are NOT touched — they run to SL/TP.
   CancelPendingOrders(sig.side);

   //--- No new trades after cutoff
   MqlDateTime dt;
   TimeGMT(dt);
   if(dt.hour >= NoNewTradesHour)
      return;

   //--- Skip if already in a FILLED position same direction (don't pyramid)
   if(HasFilledPosition(sig.side))
      return;

   //--- Calculate ATR
   double atr[];
   if(CopyBuffer(g_atrHandle, 0, 0, 1, atr) <= 0)
      return;

   //--- Position sizing
   double lot = sig.lot;
   if(lot <= 0)
      lot = CalcKellyLot(sig.win_prob, sig.rr);

   lot = NormalizeLot(lot);
   if(lot < SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN))
      return;

   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   // Step 1: Resolve SL/TP distances — prefer signal distances, fallback to ATR
   double sl_dist, tp_dist;
   if(sig.sl_dist > 0)
   {
      sl_dist = sig.sl_dist;
      tp_dist = (sig.tp_dist > 0) ? sig.tp_dist : 2.0 * sl_dist;
   }
   else
   {
      sl_dist = AtrMultSL * atr[0];
      tp_dist = 2.0 * AtrMultSL * atr[0];
   }

   // Step 2: Enforce SYMBOL_TRADE_STOPS_LEVEL minimum distance from order price
   long   stopsPoints = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   double minDist     = (stopsPoints + 1) * _Point;
   sl_dist = MathMax(sl_dist, minDist);
   tp_dist = MathMax(tp_dist, minDist);

   // Step 3: Adjust limit price into passive zone (BuyLimit < Ask, SellLimit > Bid)
   double price = NormalizeDouble(sig.price, _Digits);
   if(sig.side > 0 && price >= ask)
   {
      price = NormalizeDouble(ask - _Point, _Digits);
      Print("BuyLimit price adjusted below Ask: ", price, " Ask=", ask);
   }
   else if(sig.side < 0 && price <= bid)
   {
      price = NormalizeDouble(bid + _Point, _Digits);
      Print("SellLimit price adjusted above Bid: ", price, " Bid=", bid);
   }

   // Step 4: Compute absolute SL/TP from ADJUSTED price (not stale sig.price)
   double sl, tp;
   if(sig.side > 0) { sl = price - sl_dist;  tp = price + tp_dist; }
   else             { sl = price + sl_dist;  tp = price - tp_dist; }
   sl = NormalizeDouble(sl, _Digits);
   tp = NormalizeDouble(tp, _Digits);

   bool ok = false;
   if(sig.side > 0)
     {
      ok = trade.BuyLimit(lot, price, _Symbol, sl, tp, ORDER_TIME_GTC, 0,
                          "PPO Long " + _Symbol);
      if(!ok)
         Print("BuyLimit failed: ", trade.ResultRetcodeDescription(),
               " price=", price, " ask=", ask, " sl=", sl, " tp=", tp);
      else
         Print("BuyLimit OK: lot=", lot, " price=", price,
               " sl=", sl, " tp=", tp, " regime=", sig.regime);
     }
   else if(sig.side < 0)
     {
      ok = trade.SellLimit(lot, price, _Symbol, sl, tp, ORDER_TIME_GTC, 0,
                           "PPO Short " + _Symbol);
      if(!ok)
         Print("SellLimit failed: ", trade.ResultRetcodeDescription(),
               " price=", price, " bid=", bid, " sl=", sl, " tp=", tp);
      else
         Print("SellLimit OK: lot=", lot, " price=", price,
               " sl=", sl, " tp=", tp, " regime=", sig.regime);
     }

   //--- Register partial TP tracking for this order once it fills
   //    We store the intended entry price and TP1 level now;
   //    CheckPartialTP() will match by position ticket when filled.
   if(ok && EnablePartialTP && g_partialTPCount < 99)
   {
      // tp1Level = 0 sentinel: CheckPartialTP() computes it dynamically
      // from POSITION_PRICE_OPEN and POSITION_SL once the order fills.
      g_partialTPs[g_partialTPCount].ticket      = 0;
      g_partialTPs[g_partialTPCount].tp1Level     = 0;
      g_partialTPs[g_partialTPCount].entryPrice   = price;
      g_partialTPs[g_partialTPCount].origLot      = lot;
      g_partialTPs[g_partialTPCount].partialDone  = false;
      g_partialTPs[g_partialTPCount].side         = sig.side;
      g_partialTPCount++;
   }
}

//+------------------------------------------------------------------+
//| Parse JSON string into SignalData                                |
//+------------------------------------------------------------------+
bool ParseSignalJson(const string &json, SignalData &sig)
{
   if(StringLen(json) < 5)
      return false;

   //--- Check for heartbeat message
   sig.is_heartbeat = (StringFind(json, "\"heartbeat\"") >= 0);
   if(sig.is_heartbeat)
      return true;

   sig.side     = (int)JsonGetInt(json, "side");
   sig.price    = JsonGetDouble(json, "price");
   sig.sl       = JsonGetDouble(json, "sl");
   sig.tp       = JsonGetDouble(json, "tp");
   sig.sl_dist  = JsonGetDouble(json, "sl_dist");
   sig.tp_dist  = JsonGetDouble(json, "tp_dist");
   sig.lot      = JsonGetDouble(json, "lot");
   sig.win_prob = JsonGetDouble(json, "win_prob");
   sig.rr       = JsonGetDouble(json, "rr");
   sig.regime   = (int)JsonGetInt(json, "regime");
   sig.z_score  = JsonGetDouble(json, "z_score");

   return (sig.side != 0 || sig.price > 0);
}

//+------------------------------------------------------------------+
//| Read signal from file (fallback when ZMQ unavailable)            |
//+------------------------------------------------------------------+
bool ReadSignalFromFile(SignalData &sig)
{
   // FILE_BIN required — FILE_TXT truncates at '\n', corrupting multi-line JSON
   int handle = FileOpen(g_effectiveSigFile, FILE_READ | FILE_BIN | FILE_COMMON);
   if(handle == INVALID_HANDLE)
      return false;

   ulong sz = FileSize(handle);
   string content = "";
   if(sz > 0)
   {
      uchar buf[];
      ArrayResize(buf, (int)sz);
      FileReadArray(handle, buf, 0, (int)sz);
      content = CharArrayToString(buf, 0, (int)sz, CP_UTF8);
   }
   FileClose(handle);

   if(!ParseSignalJson(content, sig))
      return false;

   //--- Clear signal file after reading (FILE_BIN write)
   int wh = FileOpen(g_effectiveSigFile, FILE_WRITE | FILE_BIN | FILE_COMMON);
   if(wh != INVALID_HANDLE)
   {
      uchar empty[];
      StringToCharArray("{}", empty, 0, -1, CP_UTF8);
      FileWriteArray(wh, empty, 0, ArraySize(empty) - 1);
      FileClose(wh);
   }

   return true;
}

//+------------------------------------------------------------------+
double JsonGetDouble(const string &json, const string &key)
{
   string search = "\"" + key + "\":";
   int pos = StringFind(json, search);
   if(pos < 0) return 0.0;

   pos += StringLen(search);
   // Skip whitespace
   while(pos < StringLen(json) && StringGetCharacter(json, pos) == ' ')
      pos++;

   string val = "";
   while(pos < StringLen(json))
   {
      ushort ch = StringGetCharacter(json, pos);
      if(ch == ',' || ch == '}' || ch == ' ')
         break;
      val += CharToString((uchar)ch);
      pos++;
   }
   return StringToDouble(val);
}

//+------------------------------------------------------------------+
long JsonGetInt(const string &json, const string &key)
{
   return (long)JsonGetDouble(json, key);
}

//+------------------------------------------------------------------+
double CalcKellyLot(double winProb, double rr)
{
   if(rr <= 0) rr = 1.0;
   double q = 1.0 - winProb;
   double kellyRaw = (winProb * rr - q) / rr;
   if(kellyRaw <= 0) kellyRaw = 0.001;

   double f = RiskFractionKelly * kellyRaw;
   // Cap at 2% risk
   if(f > 0.02) f = 0.02;

   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskAmount = equity * f;

   // ATR for SL distance
   double atr[];
   if(CopyBuffer(g_atrHandle, 0, 0, 1, atr) <= 0)
      return SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);

   double slDist = AtrMultSL * atr[0];
   if(slDist <= 0) return SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);

   double contractSize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   double lot = riskAmount / (slDist * contractSize);

   return NormalizeLot(lot);
}

//+------------------------------------------------------------------+
double NormalizeLot(double lot)
{
   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double stepLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);

   lot = MathMax(lot, minLot);
   lot = MathMin(lot, maxLot);
   lot = MathFloor(lot / stepLot) * stepLot;
   return NormalizeDouble(lot, 2);
}

//+------------------------------------------------------------------+
double AccountDrawdownPct()
{
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   if(g_peakEquity <= 0) return 0.0;
   return (g_peakEquity - equity) / g_peakEquity * 100.0;
}

//+------------------------------------------------------------------+
bool HasPosition(int side)
{
   // Check filled positions
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(PositionGetSymbol(i) != _Symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;

      long posType = PositionGetInteger(POSITION_TYPE);
      if(side > 0 && posType == POSITION_TYPE_BUY)  return true;
      if(side < 0 && posType == POSITION_TYPE_SELL) return true;
   }
   // Check pending limit/stop orders (BuyLimit, SellLimit, BuyStop, SellStop)
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0) continue;
      if(OrderGetString(ORDER_SYMBOL) != _Symbol) continue;
      if(OrderGetInteger(ORDER_MAGIC) != MagicNumber) continue;

      long orderType = OrderGetInteger(ORDER_TYPE);
      if(side > 0 && (orderType == ORDER_TYPE_BUY_LIMIT  || orderType == ORDER_TYPE_BUY_STOP))  return true;
      if(side < 0 && (orderType == ORDER_TYPE_SELL_LIMIT || orderType == ORDER_TYPE_SELL_STOP)) return true;
   }
   return false;
}

//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| Check partial TP — close 50% at TP1, move SL to breakeven       |
//| Called on every tick.                                             |
//+------------------------------------------------------------------+
void CheckPartialTP()
{
   double mid = (SymbolInfoDouble(_Symbol, SYMBOL_BID) +
                 SymbolInfoDouble(_Symbol, SYMBOL_ASK)) / 2.0;

   //--- Guard: tickets already closed this tick — prevent double-close when
   //    multiple g_partialTPs records share the same side (e.g. pyramid entries).
   ulong processedThisTick[100];
   int   processedCount = 0;

   //--- Match open positions to pending partial-TP records (by side)
   for(int pi = 0; pi < g_partialTPCount; pi++)
   {
      if(g_partialTPs[pi].partialDone) continue;

      //--- Find matching live position (link by side + symbol + magic)
      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong ticket = PositionGetTicket(i);
         if(!PositionSelectByTicket(ticket))            continue;
         if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
         if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;

         long posType = PositionGetInteger(POSITION_TYPE);
         int  posSide = (posType == POSITION_TYPE_BUY) ? 1 : -1;
         if(posSide != g_partialTPs[pi].side) continue;

         //--- Skip if this ticket was already partially closed this tick
         bool alreadyDone = false;
         for(int k = 0; k < processedCount; k++)
            if(processedThisTick[k] == ticket) { alreadyDone = true; break; }
         if(alreadyDone) break;

         //--- Store ticket if not yet linked
         if(g_partialTPs[pi].ticket == 0)
            g_partialTPs[pi].ticket = ticket;

         //--- Compute TP1 dynamically from actual fill price and current SL
         //    (avoids stale pre-order estimates; works correctly after EA restart)
         double posEntry = PositionGetDouble(POSITION_PRICE_OPEN);
         double posSL    = PositionGetDouble(POSITION_SL);
         if(posSL <= 0) break;                      // SL not set yet — skip
         double r1 = MathAbs(posEntry - posSL);
         if(r1 < _Point) break;                     // degenerate — skip

         double tp1 = NormalizeDouble(posEntry + posSide * r1, _Digits);

         //--- Check if TP1 hit
         bool tp1Hit = (posSide > 0) ? (mid >= tp1) : (mid <= tp1);
         if(!tp1Hit) break;

         //--- Close 50% of remaining lot
         double curLot  = PositionGetDouble(POSITION_VOLUME);
         double halfLot = NormalizeLot(curLot * 0.5);
         if(halfLot < SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN))
            halfLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);

         if(halfLot >= curLot)
         {
            // Position too small to split — skip partial, let trailing handle it
            g_partialTPs[pi].partialDone = true;
            break;
         }

         bool closed = trade.PositionClosePartial(ticket, halfLot);
         if(closed)
         {
            Print("PartialTP: closed ", halfLot, " lots at ", DoubleToString(mid, _Digits),
                  " TP1=", DoubleToString(tp1, _Digits));

            //--- Mark ticket as processed this tick to prevent double-close
            if(processedCount < 100)
               processedThisTick[processedCount++] = ticket;

            //--- Move SL to breakeven — use actual fill price, not pending order price
            double be = NormalizeDouble(posEntry, _Digits);
            double curTP = PositionGetDouble(POSITION_TP);
            // Re-select because partial close may have refreshed the position
            if(PositionSelectByTicket(ticket))
            {
               double newSL = be;
               // Only tighten — never widen existing SL
               double existSL = PositionGetDouble(POSITION_SL);
               if(posSide > 0 && newSL > existSL)
                  trade.PositionModify(ticket, newSL, curTP);
               else if(posSide < 0 && (existSL <= 0 || newSL < existSL))
                  trade.PositionModify(ticket, newSL, curTP);
            }

            //--- Register in g_trails so UpdateTrailingStops() trails remaining 50%
            double atr[];
            double atrVal = 0;
            if(CopyBuffer(g_atrHandle, 0, 0, 1, atr) > 0) atrVal = atr[0];
            if(atrVal > 0 && g_trailCount < 100)
            {
               // Initial trail level: breakeven ± TrailAtrMult*ATR (locks in some profit)
               double initTrail = (posSide > 0)
                  ? NormalizeDouble(be + posSide * TrailAtrMult * atrVal, _Digits)
                  : NormalizeDouble(be + posSide * TrailAtrMult * atrVal, _Digits);
               // Only register if initTrail is better than breakeven
               bool alreadyRegistered = false;
               for(int t = 0; t < g_trailCount; t++)
                  if(g_trails[t].ticket == ticket) { alreadyRegistered = true; break; }
               if(!alreadyRegistered)
               {
                  g_trails[g_trailCount].ticket     = ticket;
                  g_trails[g_trailCount].trailLevel = initTrail;
                  g_trails[g_trailCount].side       = posSide;
                  g_trailCount++;
                  Print("Trail registered after partial TP: ticket=", ticket,
                        " initTrail=", DoubleToString(initTrail, _Digits));
               }
            }
         }
         g_partialTPs[pi].partialDone = true;
         break;
      }
   }

   //--- Compact: remove done records to prevent array overflow
   int newCount = 0;
   for(int pi = 0; pi < g_partialTPCount; pi++)
   {
      if(!g_partialTPs[pi].partialDone)
         g_partialTPs[newCount++] = g_partialTPs[pi];
   }
   g_partialTPCount = newCount;
}

//+------------------------------------------------------------------+
//--- Close open positions only; leave pending limit orders intact.
//    NOT called from Python signals (Python only sends LONG/SHORT).
//    Reserved for future internal EA use if needed.
void ClosePositionsOnly()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(PositionGetSymbol(i) != _Symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      trade.PositionClose(PositionGetTicket(i));
   }
   g_partialTPCount = 0;
   g_trailCount     = 0;
}

//--- Cancel ALL pending limit/stop orders for this symbol.
//    Called when a new entry signal arrives — pending orders get replaced with
//    fresh price. Filled positions are never touched here.
//    Also cleans up g_partialTPs records that were linked to cancelled orders
//    (ticket==0 means the limit never filled).
void CancelPendingOrders(int newSide)
{
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0) continue;
      if(OrderGetString(ORDER_SYMBOL) != _Symbol) continue;
      if(OrderGetInteger(ORDER_MAGIC) != MagicNumber) continue;
      long ot = OrderGetInteger(ORDER_TYPE);
      bool isPending = (ot == ORDER_TYPE_BUY_LIMIT  || ot == ORDER_TYPE_BUY_STOP ||
                        ot == ORDER_TYPE_SELL_LIMIT || ot == ORDER_TYPE_SELL_STOP);
      if(!isPending) continue;
      // Determine side of the pending order
      bool isBuy = (ot == ORDER_TYPE_BUY_LIMIT || ot == ORDER_TYPE_BUY_STOP);
      int  cancelledSide = isBuy ? 1 : -1;
      trade.OrderDelete(ticket);
      // Remove unlinked g_partialTPs records for this side (ticket==0 = never filled)
      int newCount = 0;
      for(int pi = 0; pi < g_partialTPCount; pi++)
      {
         bool isZombie = (g_partialTPs[pi].side == cancelledSide &&
                          g_partialTPs[pi].ticket == 0 &&
                          !g_partialTPs[pi].partialDone);
         if(!isZombie) g_partialTPs[newCount++] = g_partialTPs[pi];
      }
      g_partialTPCount = newCount;
   }
}

//--- Returns true only if there is a FILLED (open) position in this direction.
//    Pending limit orders are NOT counted — they can be replaced by new signals.
bool HasFilledPosition(int side)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(PositionGetSymbol(i) != _Symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      long posType = PositionGetInteger(POSITION_TYPE);
      if(side > 0 && posType == POSITION_TYPE_BUY)  return true;
      if(side < 0 && posType == POSITION_TYPE_SELL) return true;
   }
   return false;
}

void CloseAll()
{
   //--- Close positions
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(PositionGetSymbol(i) != _Symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      trade.PositionClose(PositionGetTicket(i));
   }

   //--- Cancel pending limit orders
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if(OrderGetString(ORDER_SYMBOL) != _Symbol) continue;
      if(OrderGetInteger(ORDER_MAGIC) != MagicNumber) continue;
      trade.OrderDelete(ticket);
   }

   //--- Clear partial TP state and trail state
   g_partialTPCount = 0;
   g_trailCount     = 0;
}

//+------------------------------------------------------------------+
//|                  EA STATUS PANEL                                   |
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
//| Create/update a pixel-anchored label (right-upper corner)         |
//+------------------------------------------------------------------+
void _PanelText(const string name, const string text, int x, int y,
                color clr, int sz = -1)
{
   string full = EA_PREFIX + name;
   if(sz < 0) sz = PnlFontSz;

   if(ObjectFind(0, full) < 0)
      ObjectCreate(0, full, OBJ_LABEL, 0, 0, 0);

   ObjectSetInteger(0, full, OBJPROP_CORNER,    CORNER_RIGHT_UPPER);
   ObjectSetInteger(0, full, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, full, OBJPROP_YDISTANCE, y);
   ObjectSetString(0,  full, OBJPROP_TEXT,       text);
   ObjectSetString(0,  full, OBJPROP_FONT,       PnlFontName);
   ObjectSetInteger(0, full, OBJPROP_FONTSIZE,   sz);
   ObjectSetInteger(0, full, OBJPROP_COLOR,      clr);
   ObjectSetInteger(0, full, OBJPROP_SELECTABLE,  false);
}

//+------------------------------------------------------------------+
//| Create/update background rectangle (right-upper)                  |
//+------------------------------------------------------------------+
void _PanelBg(int x, int y, int w, int h)
{
   string name = EA_PREFIX + "bg";
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_RECTANGLE_LABEL, 0, 0, 0);

   ObjectSetInteger(0, name, OBJPROP_CORNER,      CORNER_RIGHT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE,   x);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE,   y);
   ObjectSetInteger(0, name, OBJPROP_XSIZE,        w);
   ObjectSetInteger(0, name, OBJPROP_YSIZE,        h);
   ObjectSetInteger(0, name, OBJPROP_BGCOLOR,      PnlClrBg);
   ObjectSetInteger(0, name, OBJPROP_BORDER_TYPE,  BORDER_FLAT);
   ObjectSetInteger(0, name, OBJPROP_COLOR,        clrDimGray);
   ObjectSetInteger(0, name, OBJPROP_WIDTH,        1);
   ObjectSetInteger(0, name, OBJPROP_BACK,         false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE,   false);
}

//+------------------------------------------------------------------+
//| Draw EA status panel                                              |
//+------------------------------------------------------------------+
void _DrawEAPanel()
{
   if(!ShowPanel) return;

   int panelW = 280;
   int panelH = 130;
   int bx = PanelX + panelW;
   int by = PanelY;

   _PanelBg(bx, by, panelW, panelH);

   int lh = 17;
   int tx = bx - 10;
   int vx = tx - 115;
   int ty = by + 8;

   //--- Title
   string zmqBadge = g_zmqConnected ? " [ZMQ]" : " [FILE]";
   _PanelText("title", "EA  " + _Symbol + zmqBadge, tx, ty, PnlClrTitle, PnlFontSz + 2);
   ty += lh + 4;

   //--- Divider
   _PanelText("div1", "--------------------------------------", tx, ty, C'60,60,80', PnlFontSz - 2);
   ty += lh;

   //--- Position
   string posStr = "FLAT";
   color  posClr = clrWhite;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(PositionGetSymbol(i) != _Symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      long pt = PositionGetInteger(POSITION_TYPE);
      if(pt == POSITION_TYPE_BUY)  { posStr = "LONG";  posClr = clrLimeGreen; }
      else                          { posStr = "SHORT"; posClr = clrTomato;    }
      break;
   }
   _PanelText("pos_lbl", "Position    :", tx, ty, PnlClrLabel);
   _PanelText("pos_val", posStr,           vx, ty, posClr);
   ty += lh;

   //--- Drawdown
   double dd = AccountDrawdownPct();
   color  ddClr = (dd < 5) ? clrLimeGreen : (dd < 10) ? clrYellow : clrTomato;
   _PanelText("dd_lbl", "Drawdown    :", tx, ty, PnlClrLabel);
   _PanelText("dd_val", DoubleToString(dd, 2) + "%", vx, ty, ddClr);
   ty += lh;

   //--- Heartbeat
   string hbStr = "---";
   color  hbClr = clrGray;
   if(g_zmqConnected && g_lastHeartbeat > 0)
   {
      int elapsed = (int)(TimeGMT() - g_lastHeartbeat);
      hbStr = IntegerToString(elapsed) + "s ago";
      hbClr = (elapsed <= HeartbeatMaxSec) ? clrLimeGreen : clrTomato;
   }
   _PanelText("hb_lbl", "Last HB     :", tx, ty, PnlClrLabel);
   _PanelText("hb_val", hbStr,             vx, ty, hbClr);
   ty += lh;

   //--- EOD
   _PanelText("eod_lbl", "EOD         :", tx, ty, PnlClrLabel);
   _PanelText("eod_val", IntegerToString(EodHourGMT) + ":00 GMT", vx, ty, clrWhite);

   ChartRedraw(0);
}

//+------------------------------------------------------------------+
//| Remove all panel objects                                          |
//+------------------------------------------------------------------+
void _DeleteEAPanel()
{
   ObjectsDeleteAll(0, EA_PREFIX);
   ChartRedraw(0);
}
//+------------------------------------------------------------------+
