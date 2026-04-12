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
input double   AtrMultSL         = 1.5;                    // ATR multiplier for SL
input double   MaxDrawdownPct    = 15.0;                   // Kill switch drawdown %
input int      AtrPeriod         = 14;                     // ATR calculation period
input int      MagicNumber       = 20250411;               // EA magic number
input string   SignalFile        = "";                     // Fallback file (blank = {symbol}_signal.json)
input int      ZmqRecvTimeoutMs  = 50;                     // ZMQ recv timeout (ms)
input int      HeartbeatMaxSec   = 30;                     // Max seconds without HB

//--- Input parameters (trailing stop)
input double   TrailAtrMult      = 1.0;                    // Trailing stop ATR multiplier
input bool     EnableTrailingStop = true;                  // Enable dynamic trailing stop

//--- Global variables
CTrade trade;
double g_peakEquity;
int    g_atrHandle;
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

//--- ZeroMQ objects
Context  g_zmqCtx;
Socket   g_zmqSub(g_zmqCtx, ZMQ_SUB);
bool     g_zmqConnected = false;
datetime g_lastHeartbeat = 0;

//+------------------------------------------------------------------+
struct SignalData
{
   int    side;       // 1=long, -1=short, 0=close
   double price;
   double sl;
   double tp;
   double lot;
   double win_prob;
   double rr;
   int    regime;
   double z_score;
   bool   is_heartbeat;
};

//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(10);
   trade.SetTypeFilling(ORDER_FILLING_IOC);

   //--- Resolve dynamic topic: use ZmqTopic input or fall back to _Symbol
   g_effectiveTopic   = (StringLen(ZmqTopic) > 0) ? ZmqTopic : _Symbol;
   //--- Resolve signal file name: use SignalFile input or derive from symbol
   string symLower = _Symbol;
   StringToLower(symLower);
   g_effectiveSigFile = (StringLen(SignalFile) > 0) ? SignalFile
                        : symLower + "_signal.json";

   g_peakEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   g_atrHandle  = iATR(_Symbol, PERIOD_M5, AtrPeriod);

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
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
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

   //--- Update ATR trailing stop on every tick
   if(EnableTrailingStop)
      UpdateTrailingStops();
}

//+------------------------------------------------------------------+
//| ATR-based dynamic trailing stop                                  |
//| Long:  trail = max(trail_prev, mid - mult * ATR)                 |
//| Short: trail = min(trail_prev, mid + mult * ATR)                 |
//+------------------------------------------------------------------+
void UpdateTrailingStops()
{
   double atr[];
   if(CopyBuffer(g_atrHandle, 0, 0, 1, atr) <= 0)
      return;
   double atrVal = atr[0];
   if(atrVal <= 0)
      return;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket))
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber)
         continue;

      double mid   = (SymbolInfoDouble(_Symbol, SYMBOL_BID) +
                      SymbolInfoDouble(_Symbol, SYMBOL_ASK)) / 2.0;
      long   type  = PositionGetInteger(POSITION_TYPE);
      double curSL = PositionGetDouble(POSITION_SL);
      double curTP = PositionGetDouble(POSITION_TP);
      double newSL = curSL;

      if(type == POSITION_TYPE_BUY)
      {
         // trail = max(current_sl, mid - mult * ATR)
         double candidate = mid - TrailAtrMult * atrVal;
         if(candidate > curSL + _Point)
            newSL = NormalizeDouble(candidate, _Digits);
      }
      else if(type == POSITION_TYPE_SELL)
      {
         // trail = min(current_sl, mid + mult * ATR)
         double candidate = mid + TrailAtrMult * atrVal;
         if(curSL <= 0 || candidate < curSL - _Point)
            newSL = NormalizeDouble(candidate, _Digits);
      }

      if(newSL != curSL && newSL > 0)
      {
         if(!trade.PositionModify(ticket, newSL, curTP))
            Print("TrailingStop modify failed: ", trade.ResultRetcodeDescription(),
                  " ticket=", ticket, " newSL=", newSL);
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

   //--- 3. Heartbeat watchdog (ZMQ only)
   if(g_zmqConnected && HeartbeatMaxSec > 0)
   {
      if((int)(TimeGMT() - g_lastHeartbeat) > HeartbeatMaxSec)
      {
         Print("WARNING: No heartbeat for ", (int)(TimeGMT() - g_lastHeartbeat),
               "s — Python publisher may be down");
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
   //--- Close signal
   if(sig.side == 0)
   {
      CloseAll();
      return;
   }

   //--- No new trades after cutoff
   MqlDateTime dt;
   TimeGMT(dt);
   if(dt.hour >= NoNewTradesHour)
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

   //--- SL/TP from signal or calculate from ATR
   double sl = sig.sl;
   double tp = sig.tp;
   if(sl == 0)
   {
      if(sig.side > 0)
      {
         sl = sig.price - AtrMultSL * atr[0];
         tp = sig.price + 2.0 * AtrMultSL * atr[0];
      }
      else
      {
         sl = sig.price + AtrMultSL * atr[0];
         tp = sig.price - 2.0 * AtrMultSL * atr[0];
      }
   }

   //--- Skip if already in position (same direction)
   if(HasPosition(sig.side))
      return;

   //--- Execute LIMIT ORDER only
   sl = NormalizeDouble(sl, _Digits);
   tp = NormalizeDouble(tp, _Digits);
   double price = NormalizeDouble(sig.price, _Digits);

   if(sig.side > 0)
   {
      if(!trade.BuyLimit(lot, price, _Symbol, sl, tp, ORDER_TIME_GTC, 0,
                         "PPO Long " + _Symbol))
         Print("BuyLimit failed: ", trade.ResultRetcodeDescription());
      else
         Print("BuyLimit OK: lot=", lot, " price=", price,
               " sl=", sl, " tp=", tp, " regime=", sig.regime);
   }
   else if(sig.side < 0)
   {
      if(!trade.SellLimit(lot, price, _Symbol, sl, tp, ORDER_TIME_GTC, 0,
                          "PPO Short " + _Symbol))
         Print("SellLimit failed: ", trade.ResultRetcodeDescription());
      else
         Print("SellLimit OK: lot=", lot, " price=", price,
               " sl=", sl, " tp=", tp, " regime=", sig.regime);
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
   int handle = FileOpen(g_effectiveSigFile, FILE_READ | FILE_TXT | FILE_COMMON);
   if(handle == INVALID_HANDLE)
      return false;

   string content = "";
   while(!FileIsEnding(handle))
      content += FileReadString(handle);
   FileClose(handle);

   if(!ParseSignalJson(content, sig))
      return false;

   //--- Clear signal file after reading
   int wh = FileOpen(g_effectiveSigFile, FILE_WRITE | FILE_TXT | FILE_COMMON);
   if(wh != INVALID_HANDLE)
   {
      FileWriteString(wh, "{}");
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
}
//+------------------------------------------------------------------+
