import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import base64
import hmac
import json
import os
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import requests
from jinja2 import Template
import uvicorn

# --- 🔑 OKX DEMO KĽÚČE ---
OKX_API_KEY = "638848ca-e65c-4074-bcfd-20cf4d292f2c"
OKX_SECRET = "885934366C54F33B276E9E49E8EF87CF"
OKX_PASSPHRASE = "Australia111/"

# --- 📱 TELEGRAM NOTIFIKÁCIE ---
TELEGRAM_BOT_TOKEN = "9021881475:AAFn1oQSvaGkhpX2uyJscdcTybFgk1tNmV8"
TELEGRAM_CHAT_ID = "9130979784"


def send_telegram_msg(message: str):
  if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("DOPLŇ"):
    return
  try:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    requests.post(url, json=payload, timeout=5)
  except Exception as err:
    print(f"❌ Chyba pri odosielaní Telegram notifikácie: {err}", flush=True)


# --- STAV BOT-A & PROFILY ---
ACTIVE_TRADES = {}
MARKET_RADAR = []
DAILY_PNL_CALENDAR = {}
CLOSED_TRADES_HISTORY = []
PRICE_HISTORY = []
CURRENT_STAGE = "STAV: Mimo obchodu. Skenujem živý trh podľa checklistu..."
LAST_TRADE_TIME = {}
MAX_PARALLEL_TRADES = 1  # 🛡️ Maximálne 1 obchod naraz
trade_lock = asyncio.Lock()
IS_ORDER_PROCESSING = False

# Režimy & Rozšírené Timeframes (3m až 4h)
CURRENT_STRATEGY_MODE = "Agresívny Momentum (Breakout)"
CURRENT_TIMEFRAME = "5m"  # Predvolený TF
ALLOWED_CURRENCIES = ["USDC", "USDT", "EUR"]
EXCLUDED_COINS = ["ZAMA"]


def get_sk_time_str() -> str:
  sk_time = datetime.now(timezone.utc) + timedelta(hours=1)
  return sk_time.strftime("%Y-%m-%d %H:%M:%S")


def get_sk_date_str() -> str:
  sk_time = datetime.now(timezone.utc) + timedelta(hours=1)
  return sk_time.strftime("%Y-%m-%d")


def record_closed_trade(pair, pnl_pct, pnl_usdc, reason):
  global CLOSED_TRADES_HISTORY
  day_str = get_sk_date_str()
  if day_str not in DAILY_PNL_CALENDAR:
    DAILY_PNL_CALENDAR[day_str] = {"pnl": 0.0, "trades": 0}
  DAILY_PNL_CALENDAR[day_str]["pnl"] += pnl_usdc
  DAILY_PNL_CALENDAR[day_str]["trades"] += 1

  CLOSED_TRADES_HISTORY.insert(
      0,
      {
          "pair": pair,
          "pnl_pct": pnl_pct,
          "pnl_usdc": pnl_usdc,
          "reason": reason,
          "time": get_sk_time_str(),
      },
  )
  if len(CLOSED_TRADES_HISTORY) > 20:
    CLOSED_TRADES_HISTORY.pop()


# --- OKX EEA DEMO PROSTREDIE & AUTENTIFIKÁCIA ---
def okx_request(method: str, path: str, body: str = ""):
  timestamp = (
      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
  )
  message = timestamp + method.upper() + path + body
  mac = hmac.new(
      OKX_SECRET.encode("utf-8"), message.encode("utf-8"), digestmod="sha256"
  )
  signature = base64.b64encode(mac.digest()).decode("utf-8")

  headers = {
      "OK-ACCESS-KEY": OKX_API_KEY,
      "OK-ACCESS-SIGN": signature,
      "OK-ACCESS-TIMESTAMP": timestamp,
      "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
      "x-simulated-trading": "1",
      "Content-Type": "application/json",
  }

  url = "https://eea.okx.com" + path
  try:
    if method.upper() == "GET":
      response = requests.get(url, headers=headers, timeout=10)
    else:
      response = requests.post(url, headers=headers, data=body, timeout=10)
    return response.json()
  except Exception as err:
    return {"code": "-1", "msg": str(err)}


# --- 📈 INDIKÁTOROVÁ LOGIKA PODĽA PINE SCRIPT v5.4.4 ---
def evaluate_strategy_checklist(inst_id: str) -> dict:
  result = {
      "volume_status": False,
      "impulse_status": False,
      "fib_status": False,
      "buy_ready": False,
      "probability": 0,
  }
  try:
    url = f"/api/v5/market/candles?instId={inst_id}&bar={CURRENT_TIMEFRAME}&limit=40"
    res = okx_request("GET", url)
    if res.get("code") != "0" or not res.get("data"):
      return result

    raw_candles = res["data"]
    raw_candles.reverse()

    if len(raw_candles) < 25:
      return result

    closes = [float(c[4]) for c in raw_candles]
    highs = [float(c[2]) for c in raw_candles]
    lows = [float(c[3]) for c in raw_candles]
    volumes = [float(c[5]) for c in raw_candles]
    opens = [float(c[1]) for c in raw_candles]

    vol_directions = [
        v if closes[i] > opens[i] else -v for i, v in enumerate(volumes)
    ]

    def calc_ema(data, length):
      alpha = 2 / (length + 1)
      ema_val = data[0]
      for val in data[1:]:
        ema_val = (val * alpha) + (ema_val * (1 - alpha))
      return ema_val

    fast_ma = calc_ema(vol_directions, 5)
    slow_ma = calc_ema(vol_directions, 20)
    oscillator_val = fast_ma - slow_ma

    volume_bullish = oscillator_val > 0
    result["volume_status"] = volume_bullish

    recent_high = max(highs[-25:])
    recent_low = min(lows[-25:])
    range_size = recent_high - recent_low
    fib_target = recent_high - (range_size * 0.786)
    fib_touch = lows[-1] <= fib_target
    result["fib_status"] = fib_touch

    if CURRENT_STRATEGY_MODE == "Agresívny Momentum (Breakout)":
      price_breakout = closes[-1] > closes[-3]
      buy_trigger = volume_bullish and price_breakout
      result["impulse_status"] = price_breakout
      result["buy_ready"] = buy_trigger
      result["probability"] = (
          85 if buy_trigger else (55 if volume_bullish else 30)
      )
    else:
      fib_buy = volume_bullish and fib_touch
      result["impulse_status"] = fib_touch
      result["buy_ready"] = fib_buy
      result["probability"] = 90 if fib_buy else (50 if volume_bullish else 20)

  except Exception as e:
    print(f"❌ Chyba vyhodnocovania checklistu pre {inst_id}: {e}", flush=True)

  return result


# --- 🔍 AUTOMATICKÝ USDC SKENER (ŠIRŠÍ RADAR) ---
async def top_gainer_scanner_loop():
  global ACTIVE_TRADES, LAST_TRADE_TIME, MARKET_RADAR, CURRENT_STAGE, PRICE_HISTORY, IS_ORDER_PROCESSING
  await asyncio.sleep(5)

  start_msg = (
      f"[{get_sk_time_str()}] ⚡ APEX SCALPER SPUSTENÝ [USDC Široký Radar | TF:"
      f" {CURRENT_TIMEFRAME}]"
  )
  print(start_msg, flush=True)

  while True:
    try:
      if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES or IS_ORDER_PROCESSING:
        CURRENT_STAGE = "STAV: 🔴 V OBCHODE / SPRACOVÁVAM PRÍKAZ! Čakám..."
        await asyncio.sleep(4)
        continue

      balance_res = okx_request("GET", "/api/v5/account/balance")
      if balance_res.get("code") != "0":
        CURRENT_STAGE = "STAV: ⚠️ Chyba pri načítavaní zostatku z OKX."
        await asyncio.sleep(10)
        continue

      free_capital = 0.0
      for acc in balance_res.get("data", []):
        for detail in acc.get("details", []):
          ccy = detail.get("ccy")
          avail_raw = detail.get("availBal", 0)
          val = float(avail_raw) if avail_raw else 0.0
          if ccy == "USDC" and val > free_capital:
            free_capital = val

      if free_capital < 10.0:
        CURRENT_STAGE = (
            f"STAV: 🛑 Nedostatok USDC ({free_capital:.2f}$). Skontrolujte"
            " zostatok!"
        )
        await asyncio.sleep(10)
        continue

      CURRENT_STAGE = f"STAV: 🟢 SKENER USDC [TF: {CURRENT_TIMEFRAME} | Režim: {CURRENT_STRATEGY_MODE}]. Skenujem..."
      tickers_res = okx_request("GET", "/api/v5/market/tickers?instType=SPOT")
      if tickers_res.get("code") != "0" or not tickers_res.get("data"):
        await asyncio.sleep(4)
        continue

      min_vol = 5000.0   # Znížený limit objemu na skúšku, aby zobralo viac mincí
      min_change = 0.2   # Znížená počiatočná zmena už od +0.2%
      max_change = 100.0

      candidates = []
      for item in tickers_res["data"]:
        inst_id = item.get("instId", "").upper()
        
        # 🎯 STRICT USDC FILTER: Výhradne páry končiace na -USDC
        if not inst_id.endswith("-USDC"):
          continue
        if inst_id in ["USDC-USDC", "USDT-USDC", "BTC-USDC", "ETH-USDC"]:
          continue

        coin_base = inst_id.split("-")[0]
        if coin_base in EXCLUDED_COINS:
          continue

        try:
          open24h = float(item.get("open24h", 0))
          last_p = float(item.get("last", 0))
          vol24h = float(
              item.get("volCcy24h", 0) or item.get("vol24h", 0) or 0
          )

          if open24h > 0 and last_p > 0 and vol24h >= min_vol:
            change_pct = ((last_p - open24h) / open24h) * 100
            if min_change <= change_pct <= max_change:
              chk = evaluate_strategy_checklist(inst_id)

              candidates.append({
                  "instId": inst_id,
                  "price": last_p,
                  "change": change_pct,
                  "volume": vol24h,
                  "volume_status": chk["volume_status"],
                  "impulse_status": chk["impulse_status"],
                  "fib_status": chk["fib_status"],
                  "buy_ready": chk["buy_ready"],
                  "probability": chk["probability"],
              })
        except Exception:
          continue

      candidates.sort(
          key=lambda x: (x["buy_ready"], x["probability"], x["change"]),
          reverse=True,
      )
      if candidates:
        MARKET_RADAR = candidates[:25]  # Zobrazenie až 25 USDC mincí naraz

      for cand in candidates:
        if not cand["buy_ready"]:
          continue

        coin_name = cand["instId"].split("-")[0]
        now_ts = datetime.now().timestamp()
        if (
            coin_name in LAST_TRADE_TIME
            and now_ts - LAST_TRADE_TIME[coin_name] < 300
        ):
          continue

        async with trade_lock:
          if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES or IS_ORDER_PROCESSING:
            break

          IS_ORDER_PROCESSING = True
          safe_budget = min(free_capital * 0.95, 500.0)
          current_price = cand["price"]

          coin_amount = safe_budget / current_price
          formatted_sz = round(coin_amount, 2)
          if formatted_sz <= 0:
            formatted_sz = round(coin_amount, 4)

          order_body = json.dumps({
              "instId": cand["instId"],
              "tdMode": "cash",
              "side": "buy",
              "ordType": "limit",
              "px": str(round(current_price, 4)),
              "sz": str(formatted_sz),
              "tgtCcy": "base_ccy",
          })

          order_res = okx_request("POST", "/api/v5/trade/order", order_body)

          if order_res.get("code") != "0":
            err_data = order_res.get("data", [{}])
            s_code = (
                err_data[0].get("sCode")
                if isinstance(err_data, list) and len(err_data) > 0
                else ""
            )

            if s_code == "51155" or "compliance" in str(order_res).lower():
              print(
                  f"⏩ {cand['instId']} blokovaný demo účtom. Skúšam ďalšieho"
                  " Top Gainera...",
                  flush=True,
              )
              IS_ORDER_PROCESSING = False
              continue

            print(f"❌ Nákup zlyhal pre {cand['instId']}: {order_res}", flush=True)
            IS_ORDER_PROCESSING = False
            break

          LAST_TRADE_TIME[coin_name] = datetime.now().timestamp()
          tp_price = current_price * 1.012
          sl_price = current_price * 0.99
          PRICE_HISTORY = [current_price]

          ACTIVE_TRADES[coin_name] = {
              "market_pair": cand["instId"],
              "entry_price": current_price,
              "current_price": current_price,
              "highest_price": current_price,
              "target_tp_price": tp_price,
              "current_sl_price": sl_price,
              "break_even_activated": False,
              "trailing_tp_activated": False,
              "amount": formatted_sz,
              "bought_at": get_sk_time_str(),
              "invested_usdc": safe_budget,
              "live_pnl_pct": 0.0,
              "live_pnl_usdc": 0.0,
          }

          IS_ORDER_PROCESSING = False
          CURRENT_STAGE = (
              f"STAV: 🔴 V OBCHODE [{cand['instId']}] (TF: {CURRENT_TIMEFRAME} |"
              f" {CURRENT_STRATEGY_MODE})..."
          )

          send_telegram_msg(
              f"🟢 <b>[NÁKUP - USDC | TF {CURRENT_TIMEFRAME}]</b>\n"
              f"<b>Pár:</b> {cand['instId']}\n"
              f"<b>24h Rast:</b> +{cand['change']:.2f}%\n"
              f"<b>Pravdepodobnosť:</b> {cand['probability']}%\n"
              f"<b>Cena:</b> {current_price:.4f}\n"
              f"<b>Investícia:</b> ~{safe_budget:.2f} USDC\n"
              f"<b>Stop-Loss:</b> {sl_price:.4f} (-1%)"
          )
          break

    except Exception as scan_err:
      IS_ORDER_PROCESSING = False
      print(f"❌ Chyba v skeneri: {scan_err}", flush=True)

    await asyncio.sleep(5)


# --- 🛡️ MONITOR OBCHODU & RISK MANAGEMENT ---
async def advanced_risk_monitor_loop():
  global ACTIVE_TRADES, CURRENT_STAGE, PRICE_HISTORY
  await asyncio.sleep(3)

  while True:
    if not ACTIVE_TRADES:
      await asyncio.sleep(2)
      continue

    try:
      for coin in list(ACTIVE_TRADES.keys()):
        if coin not in ACTIVE_TRADES:
          continue
        trade = ACTIVE_TRADES[coin]
        symbol = trade["market_pair"]

        ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={symbol}")
        if ticker_res.get("code") == "0" and ticker_res.get("data"):
          current_price = float(ticker_res["data"][0]["last"])
          PRICE_HISTORY.append(current_price)
          if len(PRICE_HISTORY) > 30:
            PRICE_HISTORY.pop(0)

          pnl_pct = (
              (current_price - trade["entry_price"]) / trade["entry_price"]
          ) * 100
          pnl_usdc = trade.get("invested_usdc", 100.0) * (pnl_pct / 100.0)

          trade["current_price"] = current_price
          trade["live_pnl_pct"] = pnl_pct
          trade["live_pnl_usdc"] = pnl_usdc

          if current_price > trade["highest_price"]:
            trade["highest_price"] = current_price
            if pnl_pct >= 0.8:
              new_trailing_sl = trade["highest_price"] * 0.995
              if new_trailing_sl > trade["current_sl_price"]:
                trade["current_sl_price"] = new_trailing_sl

          if not trade["break_even_activated"] and pnl_pct >= 0.4:
            trade["break_even_activated"] = True
            if trade["entry_price"] > trade["current_sl_price"]:
              trade["current_sl_price"] = trade["entry_price"]

          if not trade["trailing_tp_activated"] and pnl_pct >= 0.8:
            trade["trailing_tp_activated"] = True

          if trade["trailing_tp_activated"]:
            drop_from_peak = (
                (trade["highest_price"] - current_price)
                / trade["highest_price"]
            ) * 100
            if drop_from_peak >= 0.3:
              try:
                order_body = json.dumps({
                    "instId": symbol,
                    "tdMode": "cash",
                    "side": "sell",
                    "ordType": "market",
                    "sz": str(trade["amount"]),
                })
                okx_request("POST", "/api/v5/trade/order", order_body)
                record_closed_trade(symbol, pnl_pct, pnl_usdc, "Take Profit")

                send_telegram_msg(
                    f"🎯 <b>[TAKE PROFIT - VÝSLEDOK]</b>\n"
                    f"<b>Pár:</b> {symbol}\n"
                    f"<b>Zisk:</b> +{pnl_pct:.2f}% (+{pnl_usdc:+.2f})"
                )
              except Exception as err:
                print(f"❌ Chyba Take Profit: {err}", flush=True)

              if coin in ACTIVE_TRADES:
                del ACTIVE_TRADES[coin]
              CURRENT_STAGE = "STAV: 🟢 Mimo obchodu. Zisk uzamknutý!"
              continue

          if current_price <= trade["current_sl_price"]:
            try:
              order_body = json.dumps({
                  "instId": symbol,
                  "tdMode": "cash",
                  "side": "sell",
                  "ordType": "market",
                  "sz": str(trade["amount"]),
              })
              okx_request("POST", "/api/v5/trade/order", order_body)
              record_closed_trade(
                  symbol, pnl_pct, pnl_usdc, "Stop-Loss / Ochrana"
              )

              send_telegram_msg(
                  f"🚨 <b>[STOP-LOSS / OCHRANA]</b>\n"
                  f"<b>Pár:</b> {symbol}\n"
                  f"<b>Výsledok:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f})"
              )
            except Exception as err:
              print(f"❌ Chyba SL: {err}", flush=True)

            if coin in ACTIVE_TRADES:
              del ACTIVE_TRADES[coin]
            CURRENT_STAGE = "STAV: 🟢 Mimo obchodu. Pozícia zatvorená."
            continue

    except Exception as main_err:
      print(f"❌ Chyba v monitorovaní: {main_err}", flush=True)

    await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
  scanner_task = asyncio.create_task(top_gainer_scanner_loop())
  monitor_task = asyncio.create_task(advanced_risk_monitor_loop())
  yield
  scanner_task.cancel()
  monitor_task.cancel()


app = FastAPI(lifespan=lifespan)


# --- 🎛️ API ENDPOINTS ---
@app.get("/api/set-strategy/{mode}")
async def set_strategy(mode: str):
  global CURRENT_STRATEGY_MODE
  decoded_mode = (
      "Agresívny Momentum (Breakout)"
      if "Breakout" in mode
      else "Korekcia (Fibonacci Touch)"
  )
  CURRENT_STRATEGY_MODE = decoded_mode
  return JSONResponse({"status": "success", "mode": CURRENT_STRATEGY_MODE})


@app.get("/api/set-tf/{tf}")
async def set_timeframe(tf: str):
  global CURRENT_TIMEFRAME
  allowed_tfs = ["3m", "5m", "15m", "30m", "1h", "4h"]
  if tf in allowed_tfs:
    CURRENT_TIMEFRAME = tf
  return JSONResponse({"status": "success", "timeframe": CURRENT_TIMEFRAME})


@app.post("/api/emergency-sell")
async def emergency_sell():
  global ACTIVE_TRADES, CURRENT_STAGE
  async with trade_lock:
    if not ACTIVE_TRADES:
      return JSONResponse({
          "status": "warning",
          "message": "Žiadny aktívny obchod na predaj.",
      })

    sold_summary = []
    for coin in list(ACTIVE_TRADES.keys()):
      trade = ACTIVE_TRADES[coin]
      symbol = trade["market_pair"]
      try:
        order_body = json.dumps({
            "instId": symbol,
            "tdMode": "cash",
            "side": "sell",
            "ordType": "market",
            "sz": str(trade["amount"]),
        })
        okx_request("POST", "/api/v5/trade/order", order_body)
        pnl_usdc = trade.get("live_pnl_usdc", 0.0)
        record_closed_trade(
            symbol,
            trade.get("live_pnl_pct", 0.0),
            pnl_usdc,
            "Manuálny Kill Switch",
        )

        send_telegram_msg(
            f"🚨 <b>[NÚDZOVÝ PREDAJ / KILL SWITCH]</b>\n"
            f"<b>Pár:</b> {symbol}\n"
            f"<b>PnL:</b> {trade.get('live_pnl_pct', 0.0):+.2f}%"
        )
        sold_summary.append(symbol)
      except Exception as e:
        print(f"Chyba pri emergency sell: {e}", flush=True)

      del ACTIVE_TRADES[coin]

    CURRENT_STAGE = "STAV: 🟢 Mimo obchodu (Ručný výstup). Skenujem..."
    return JSONResponse({
        "status": "success",
        "message": "Pozícia úspešne predaná cez Kill Switch",
        "sold": sold_summary,
    })


# --- 🖼️ CYBERPUNK HUD WEB ROZHRANIE S ŠIROKÝM RADAROM ---
HTML_TEMPLATE_CONTENT = """<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Apex Scalper Pro - CyberHUD 4.0 (Wide Radar)</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * { box-sizing: border-box; }
        body { 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
            margin: 0; padding: 25px;
            background: linear-gradient(rgba(0,0,0,0.4), rgba(0,0,0,0.4)), url('https://images.unsplash.com/photo-1519501025264-65ba15a82390?q=80&w=1920&auto=format&fit=crop');
            background-size: cover; background-position: center; background-attachment: fixed;
            color: #ffffff; min-height: 100vh;
        }
        .container { 
            max-width: 780px; margin: 0; background: rgba(0, 0, 0, 0.78);
            backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(0, 255, 255, 0.3); border-radius: 18px; padding: 22px;
            box-shadow: 0 0 35px rgba(0, 255, 255, 0.25);
        }
        .header-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
        .brand-title { font-size: 1rem; font-weight: 800; color: #00f0ff; letter-spacing: 2px; text-transform: uppercase; }
        
        .controls-row { display: flex; justify-content: space-between; align-items: center; background: rgba(0,255,255,0.08); border: 1px solid rgba(0,255,255,0.2); padding: 10px 14px; border-radius: 10px; margin-bottom: 15px; flex-wrap: wrap; gap: 8px; }
        .mode-buttons a { text-decoration: none; background: rgba(0,0,0,0.5); border: 1px solid #00f0ff; color: #fff; padding: 5px 9px; font-size: 0.68rem; font-weight: bold; border-radius: 6px; display: inline-block; transition: all 0.2s; }
        .mode-buttons a.active { background: #00f0ff; color: #000; box-shadow: 0 0 10px #00f0ff; }
        
        .tf-buttons { display: flex; gap: 4px; flex-wrap: wrap; }
        .tf-buttons a { text-decoration: none; background: rgba(0,0,0,0.5); border: 1px solid #ff00ff; color: #fff; padding: 5px 8px; font-size: 0.68rem; font-weight: bold; border-radius: 6px; display: inline-block; transition: all 0.2s; }
        .tf-buttons a.active { background: #ff00ff; color: #000; box-shadow: 0 0 10px #ff00ff; }

        .kill-switch-btn {
            background: linear-gradient(135deg, #ff0055, #990033);
            color: #fff; border: 1px solid #ff0055; padding: 5px 10px;
            border-radius: 6px; font-weight: 800; font-size: 0.68rem;
            cursor: pointer; text-transform: uppercase; letter-spacing: 1px;
            box-shadow: 0 0 10px rgba(255, 0, 85, 0.4);
        }
        .kill-switch-btn:hover { background: #ff0055; }

        .stats-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 20px; }
        .stat-card { background: rgba(0,0,0,0.6); border: 1px solid rgba(0,255,255,0.25); border-radius: 10px; padding: 10px; text-align: center; }
        .stat-label { font-size: 0.7rem; color: #00f0ff; text-transform: uppercase; font-weight: bold; }
        .stat-value { font-size: 1.1rem; font-weight: bold; margin-top: 4px; }

        .status-banner { background: rgba(0, 240, 255, 0.15); border: 2px solid #00f0ff; border-radius: 10px; padding: 12px; font-size: 0.85rem; font-weight: 700; color: #ffffff; margin-bottom: 20px; }
        .section-block { margin-bottom: 22px; }
        .section-title { font-size: 0.85rem; font-weight: 700; color: #00f0ff; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px; }
        table { width: 100%; border-collapse: collapse; margin-top: 4px; }
        th, td { text-align: left; padding: 7px 9px; border-bottom: 1px solid rgba(0, 255, 255, 0.20); font-size: 0.75rem; color: #ffffff; }
        th { font-weight: 700; color: #00f0ff; text-transform: uppercase; font-size: 0.68rem; }
        .pnl-green { color: #00ff88; font-weight: bold; }
        .pnl-red { color: #ff4444; font-weight: bold; }
        .chart-container { background: rgba(0, 0, 0, 0.5); border: 1px solid rgba(0, 255, 255, 0.2); border-radius: 10px; padding: 10px; margin-top: 10px; height: 150px; }
        .refresh-info { color: rgba(255, 255, 255, 0.6); font-size: 0.7rem; margin-top: 20px; font-weight: 600; text-align: left; }
    </style>
    <meta http-equiv="refresh" content="2">
</head>
<body>
    <div class="container">
        <div class="header-bar">
            <div class="brand-title">⚡ Apex Scalper HUD 4.0 (Wide USDC Radar)</div>
        </div>

        <div class="controls-row">
            <div class="mode-buttons">
                <a href="/api/set-strategy/Breakout" class="{{ 'active' if 'Breakout' in current_mode else '' }}">⚡ Breakout</a>
                <a href="/api/set-strategy/Fibonacci" class="{{ 'active' if 'Fibonacci' in current_mode else '' }}">🎯 Fib</a>
            </div>
            <div class="tf-buttons">
                <a href="/api/set-tf/3m" class="{{ 'active' if current_tf == '3m' else '' }}">3m</a>
                <a href="/api/set-tf/5m" class="{{ 'active' if current_tf == '5m' else '' }}">5m</a>
                <a href="/api/set-tf/15m" class="{{ 'active' if current_tf == '15m' else '' }}">15m</a>
                <a href="/api/set-tf/30m" class="{{ 'active' if current_tf == '30m' else '' }}">30m</a>
                <a href="/api/set-tf/1h" class="{{ 'active' if current_tf == '1h' else '' }}">1h</a>
                <a href="/api/set-tf/4h" class="{{ 'active' if current_tf == '4h' else '' }}">4h</a>
            </div>
            <button class="kill-switch-btn" onclick="triggerKillSwitch()">🚨 KILL</button>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Celkovo Obchodov</div>
                <div class="stat-value">{{ total_trades }}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Úspešnosť (Win Rate)</div>
                <div class="stat-value" style="color: #00ff88;">{{ win_rate }}%</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Kumulatívny Zisk</div>
                <div class="stat-value {{ 'pnl-green' if total_pnl_usdc >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(total_pnl_usdc) }} $</div>
            </div>
        </div>
        
        <div class="status-banner">{{ current_stage }}</div>

        <div class="section-block">
            <div class="section-title">🔴 Aktívny Obchod & Živý PnL</div>
            {% if trades %}
                <table>
                    <tr><th>Pár</th><th>Vstup</th><th>Investícia</th><th>Cena</th><th>PnL (%)</th><th>PnL ($)</th></tr>
                    {% for t, details in trades.items() %}
                    <tr>
                        <td><strong>{{ details.market_pair }}</strong></td>
                        <td>{{ "%.4f" | format(details.entry_price) }}</td>
                        <td>{{ "%.2f" | format(details.invested_usdc) }} USDC</td>
                        <td>{{ "%.4f" | format(details.current_price) }}</td>
                        <td class="{{ 'pnl-green' if details.live_pnl_pct >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(details.live_pnl_pct) }}%</td>
                        <td class="{{ 'pnl-green' if details.live_pnl_usdc >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(details.live_pnl_usdc) }} $</td>
                    </tr>
                    {% endfor %}
                </table>
                <div class="chart-container">
                    <canvas id="livePriceChart"></canvas>
                </div>
            {% else %}
                <div style="font-size: 0.8rem; color: rgba(255, 255, 255, 0.8); background: rgba(0,0,0,0.4); padding: 12px; border-radius: 8px; border: 1px dashed rgba(0,255,255,0.3);">
                    ✅ Žiadny aktívny obchod. Skenujem široký USDC trh na <strong>{{ current_tf }}</strong> TF...
                </div>
            {% endif %}
        </div>

        <div class="section-block">
            <div class="section-title">📊 Široký Radar USDC Trhu & Checklist (TF: {{ current_tf }})</div>
            {% if radar %}
                <table>
                    <tr><th>Symbol</th><th>24h Zmena</th><th>Objemový Mom.</th><th>Impulz / Fib</th><th>Pripravenosť</th></tr>
                    {% for item in radar %}
                    <tr>
                        <td><strong>{{ item.instId }}</strong></td>
                        <td class="{{ 'pnl-green' if item.change >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(item.change) }}%</td>
                        <td><span style="color: {{ '#00ff88' if item.volume_status else '#ff4444' }}; font-size: 0.9rem;">{{ '🟢' if item.volume_status else '🔴' }}</span></td>
                        <td><span style="color: {{ '#00ff88' if item.impulse_status else '#ff4444' }}; font-size: 0.9rem;">{{ '🟢' if item.impulse_status else '⚪' }}</span></td>
                        <td><strong style="color: {{ '#00ff88' if item.buy_ready else '#00f0ff' }};">{{ '🚀 NAKUPOVAT' if item.buy_ready else item.probability|string + '%' }}</strong></td>
                    </tr>
                    {% endfor %}
                </table>
            {% else %}
                <div style="font-size: 0.8rem; color: rgba(255, 255, 255, 0.7);">Načítavam dáta USDC do radaru...</div>
            {% endif %}
        </div>

        <div class="section-block">
            <div class="section-title">📜 História Posledných Obchodov</div>
            {% if history %}
                <table>
                    <tr><th>Čas</th><th>Pár</th><th>Dôvod</th><th>Výsledok (%)</th><th>Výsledok ($)</th></tr>
                    {% for h in history %}
                    <tr>
                        <td>{{ h.time }}</td>
                        <td><strong>{{ h.pair }}</strong></td>
                        <td>{{ h.reason }}</td>
                        <td class="{{ 'pnl-green' if h.pnl_pct >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(h.pnl_pct) }}%</td>
                        <td class="{{ 'pnl-green' if h.pnl_usdc >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(h.pnl_usdc) }} $</td>
                    </tr>
                    {% endfor %}
                </table>
            {% else %}
                <div style="font-size: 0.8rem; color: rgba(255, 255, 255, 0.7);">Zatiaľ žiadne uzavreté obchody v tejto relácii.</div>
            {% endif %}
        </div>

        <div class="section-block">
            <div class="section-title">💰 Zostatky (OKX Demo - USDC, USDT, EUR)</div>
            <table>
                <tr><th>Mena</th><th>Dostupné (availBal)</th></tr>
                {% for coin, data in balances.items() %}
                <tr><td><strong>{{ coin }}</strong></td><td>{{ "%.4f" | format(data.available_balance | float) }}</td></tr>
                {% endfor %}
            </table>
        </div>

        <div class="refresh-info">APEX HUD 4.0 | Režim: {{ current_mode }} | TF: {{ current_tf }} | SK Čas: {{ current_time }}</div>
    </div>

    <script>
        async function triggerKillSwitch() {
            if (confirm("⚠️ Naozaj chceš okamžite predajom zatvoriť aktívnu pozíciu?")) {
                const res = await fetch('/api/emergency-sell', { method: 'POST' });
                const data = await res.json();
                alert(data.message || "Pozícia zatvorená!");
                window.location.reload();
            }
        }

        const rawHistory = {{ price_history | tojson }};
        if (rawHistory && rawHistory.length > 0 && document.getElementById('livePriceChart')) {
            const ctxChart = document.getElementById('livePriceChart').getContext('2d');
            new Chart(ctxChart, {
                type: 'line',
                data: {
                    labels: rawHistory.map((_, i) => i + 1),
                    datasets: [{
                        label: 'Cena',
                        data: rawHistory,
                        borderColor: '#00f0ff',
                        backgroundColor: 'rgba(0, 240, 255, 0.1)',
                        fill: true,
                        borderWidth: 2,
                        tension: 0.3,
                        pointRadius: 2
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { display: false } },
                    scales: {
                        x: { display: false },
                        y: { 
                            ticks: { color: '#00f0ff', font: { size: 10 } },
                            grid: { color: 'rgba(255,255,255,0.05)' }
                        }
                    }
                }
            });
        }
    </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
  account_balances = {
      curr: {"available_balance": 0.0} for curr in ALLOWED_CURRENCIES
  }
  try:
    balance_res = okx_request("GET", "/api/v5/account/balance")
    if balance_res.get("code") == "0":
      found = {}
      for acc in balance_res.get("data", []):
        for detail in acc.get("details", []):
          ccy = detail.get("ccy")
          if ccy in ALLOWED_CURRENCIES:
            avail_raw = detail.get("availBal", 0)
            avail = float(avail_raw) if avail_raw else 0.0
            found[ccy] = {"available_balance": avail}
      if found:
        for curr in ALLOWED_CURRENCIES:
          if curr in found:
            account_balances[curr] = found[curr]
  except Exception:
    pass

  total_trades = len(CLOSED_TRADES_HISTORY)
  winning_trades = sum(1 for t in CLOSED_TRADES_HISTORY if t["pnl_pct"] > 0)
  win_rate = round(
      (winning_trades / total_trades * 100) if total_trades > 0 else 0.0, 1
  )
  total_pnl_usdc = (
      sum(t["pnl_usdc"] for t in CLOSED_TRADES_HISTORY)
      if "CLOSED_TRADES_HISTORY" in globals()
      else 0.0
  )

  template = Template(HTML_TEMPLATE_CONTENT)
  return template.render(
      balances=account_balances,
      trades=ACTIVE_TRADES,
      radar=MARKET_RADAR,
      calendar=DAILY_PNL_CALENDAR,
      history=CLOSED_TRADES_HISTORY,
      total_trades=total_trades,
      win_rate=win_rate,
      total_pnl_usdc=total_pnl_usdc,
      current_mode=CURRENT_STRATEGY_MODE,
      current_tf=CURRENT_TIMEFRAME,
      current_stage=CURRENT_STAGE,
      price_history=PRICE_HISTORY,
      current_time=get_sk_time_str(),
  )


@app.get("/api/status")
async def api_status():
  return {
      "status": "online",
      "bot_type": "Apex Scalper Pro 4.0 (Wide USDC Radar)",
      "mode": CURRENT_STRATEGY_MODE,
      "timeframe": CURRENT_TIMEFRAME,
      "current_stage": CURRENT_STAGE,
      "active_trades": ACTIVE_TRADES,
      "market_radar": MARKET_RADAR,
      "history": CLOSED_TRADES_HISTORY,
      "sk_time": get_sk_time_str(),
  }


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 10000))
  uvicorn.run(app, host="0.0.0.0", port=port)
