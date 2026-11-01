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
CURRENT_STAGE = "STAV: Mimo obchodu. Skenujem živý trh..."
LAST_TRADE_TIME = {}
MAX_PARALLEL_TRADES = 1  # 🛡️ Ochrana proti triešteniu: Výhradne 1 obchod naraz
trade_lock = asyncio.Lock()
IS_ORDER_PROCESSING = False

CURRENT_MODE = "AGGRESSIVE"
ALLOWED_CURRENCIES = ["USDC", "USDT", "EUR"]

# 🛡️ BLACKLIST: Mince, ktoré na demo účte robia drobenie a trieštenie objednávok
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


# --- 📈 FLEXIBILNÁ 5M INDIKÁTOROVÁ LOGIKA PRE ŽIVÉ TOKENY ---
def evaluate_indicator_signal(inst_id: str) -> bool:
  try:
    url = f"/api/v5/market/candles?instId={inst_id}&bar=5m&limit=50"
    res = okx_request("GET", url)
    if res.get("code") != "0" or not res.get("data"):
      return False

    raw_candles = res["data"]
    raw_candles.reverse()  # Od najstaršej po najnovšiu

    if len(raw_candles) < 20:
      return False

    closes = [float(c[4]) for c in raw_candles]
    volumes = [float(c[5]) for c in raw_candles]

    # Rýchla kontrola, či token nie je mŕtvy (posledné sviečky majú nulový objem)
    recent_vol_avg = sum(volumes[-5:]) / 5.0
    if recent_vol_avg < 10.0:  # Zamedzenie obchodovania mŕtvych mincí
      return False

    # Flexibilnejšia podmienka pre živé tokeny:
    # Overujeme, či cena rastie na posledných sviečkach a objem podporuje pohyb
    price_trend_up = closes[-1] > closes[-3]  # Rast oproti predchádzajúcim sviečkam
    return price_trend_up
  except Exception as e:
    print(f"❌ Chyba indikátora pre {inst_id}: {e}", flush=True)
    return True  # V prípade výpadku API neblokujeme inak sľubný gainer


# --- 🔍 AUTOMATICKÝ 5M SKENER S PODMIENKAMI ---
async def top_gainer_scanner_loop():
  global ACTIVE_TRADES, LAST_TRADE_TIME, MARKET_RADAR, CURRENT_STAGE, PRICE_HISTORY, CURRENT_MODE, IS_ORDER_PROCESSING
  await asyncio.sleep(5)

  start_msg = f"[{get_sk_time_str()}] ⚡ APEX SCALPER 3.2 SPUSTENÝ [Flexi Top Gainer]"
  print(start_msg, flush=True)
  send_telegram_msg(
      "🤖 <b>Apex Scalper 3.2 spustený!</b> Optimalizovaný skener pre živé"
      " tokeny je aktívny."
  )

  while True:
    try:
      # 🛡️ Ochrana proti triešteniu: Maximálne 1 obchod naraz
      if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES or IS_ORDER_PROCESSING:
        CURRENT_STAGE = "STAV: 🔴 V OBCHODE / SPRACOVÁVAM PRÍKAZ! Čakám..."
        await asyncio.sleep(5)
        continue

      balance_res = okx_request("GET", "/api/v5/account/balance")
      if balance_res.get("code") != "0":
        CURRENT_STAGE = "STAV: ⚠️ Chyba pri načítavaní zostatku z OKX."
        await asyncio.sleep(10)
        continue

      free_capital = 0.0
      for acc in balance_res.get("data", []):
        for detail in acc.get("details", []):
          if detail.get("ccy") == "USDC":
            avail_raw = detail.get("availBal", 0)
            free_capital = float(avail_raw) if avail_raw else 0.0
            break

      if free_capital < 10.0:
        CURRENT_STAGE = (
            f"STAV: 🛑 Nedostatok USDC ({free_capital:.2f}$). Skontrolujte"
            " zostatok!"
        )
        await asyncio.sleep(10)
        continue

      CURRENT_STAGE = f"STAV: 🟢 SKENER [Živé Tokeny]. USDC OK ({free_capital:.2f}$). Skenujem trh..."
      tickers_res = okx_request("GET", "/api/v5/market/tickers?instType=SPOT")
      if tickers_res.get("code") != "0" or not tickers_res.get("data"):
        await asyncio.sleep(5)
        continue

      min_vol = 15000.0  # Minimálny objem, aby sme vylúčili mŕtve tokeny
      min_change = 0.5  # Minimálny 24h rast od +0.5%
      max_change = 25.0  # Maximálny 24h rast do +25%

      candidates = []
      for item in tickers_res["data"]:
        inst_id = item.get("instId", "").upper()
        if not inst_id.endswith("-USDC"):
          continue
        if inst_id in ["USDC-USDC", "USDT-USDC", "BTC-USDC", "ETH-USDC"]:
          continue

        # 🛡️ Kontrola Blacklistu (ZAMA atď.)
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
              # Overenie cez flexibilný skener, či token žije a má rastový trend
              if evaluate_indicator_signal(inst_id):
                probability = int(
                    min(max((change_pct / max_change) * 100, 60), 99)
                )
                candidates.append({
                    "instId": inst_id,
                    "price": last_p,
                    "change": change_pct,
                    "volume": vol24h,
                    "probability": probability,
                })
        except Exception:
          continue

      candidates.sort(key=lambda x: x["change"], reverse=True)
      if candidates:
        MARKET_RADAR = candidates[:10]

      if not candidates:
        await asyncio.sleep(5)
        continue

      best_target = candidates[0]
      coin_name = best_target["instId"].split("-")[0]

      now_ts = datetime.now().timestamp()
      if coin_name in LAST_TRADE_TIME:
        if now_ts - LAST_TRADE_TIME[coin_name] < 1800:  # 30 min cooldown
          await asyncio.sleep(5)
          continue

      # 🛡️ Zámok proti triešteniu a paralelnému odosielaniu
      async with trade_lock:
        if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES or IS_ORDER_PROCESSING:
          continue

        IS_ORDER_PROCESSING = True
        LAST_TRADE_TIME[coin_name] = now_ts
        CURRENT_STAGE = (
            f"STAV: ⚡ Našiel som živý token! Vystavujem objednávku pre"
            f" {best_target['instId']}..."
        )

        safe_budget = min(free_capital * 0.95, 500.0)
        current_price = best_target["price"]

        coin_amount = safe_budget / current_price
        formatted_sz = round(coin_amount, 2)
        if formatted_sz <= 0:
          formatted_sz = round(coin_amount, 4)

        order_body = json.dumps({
            "instId": best_target["instId"],
            "tdMode": "cash",
            "side": "buy",
            "ordType": "limit",
            "px": str(round(current_price, 4)),
            "sz": str(formatted_sz),
            "tgtCcy": "base_ccy",
        })

        order_res = okx_request("POST", "/api/v5/trade/order", order_body)
        if order_res.get("code") != "0":
          print(f"❌ Nákup zlyhal na burze: {order_res}", flush=True)
          send_telegram_msg(
              "❌ <b>Chyba nákupu USDC:</b>"
              f" {order_res.get('msg', 'Neznáma chyba')}"
          )
          IS_ORDER_PROCESSING = False
          await asyncio.sleep(5)
          continue

        tp_price = current_price * 1.012  # +1.2% Take Profit
        sl_price = current_price * 0.99  # -1.0% Stop-Loss
        PRICE_HISTORY = [current_price]

        ACTIVE_TRADES[coin_name] = {
            "market_pair": best_target["instId"],
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
            f"STAV: 🔴 V OBCHODE [{best_target['instId']}] (Živý Gainer"
            " aktívny)..."
        )

        send_telegram_msg(
            f"🟢 <b>[ŽIVÝ GAINER NÁKUP]</b>\n"
            f"<b>Pár:</b> {best_target['instId']}\n"
            f"<b>Cena:</b> {current_price:.4f} USDC\n"
            f"<b>Investícia:</b> ~{safe_budget:.2f} USDC\n"
            f"<b>Stop-Loss:</b> {sl_price:.4f} USDC (-1%)"
        )

    except Exception as scan_err:
      IS_ORDER_PROCESSING = False
      print(f"❌ Chyba v skeneri: {scan_err}", flush=True)

    await asyncio.sleep(10)


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

          # Break-Even aktivácia pri +0.4%
          if not trade["break_even_activated"] and pnl_pct >= 0.4:
            trade["break_even_activated"] = True
            if trade["entry_price"] > trade["current_sl_price"]:
              trade["current_sl_price"] = trade["entry_price"]

          # Trailing Take Profit aktivácia pri +0.8% s callbackom 0.3%
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
                    f"<b>Výsledný zisk:</b> +{pnl_pct:.2f}%"
                    f" (+{pnl_usdc:+.2f} USDC)"
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
                  f"🚨 <b>[STOP-LOSS / OCHRANA - VÝSLEDOK]</b>\n"
                  f"<b>Pár:</b> {symbol}\n"
                  f"<b>Výsledok:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} USDC)"
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


# --- API ENDPOINTS ---
@app.get("/api/set-mode/{mode}")
async def set_mode(mode: str):
  global CURRENT_MODE
  if mode in ["AGGRESSIVE", "CONSERVATIVE"]:
    CURRENT_MODE = mode
    return JSONResponse({"status": "success", "mode": CURRENT_MODE})
  return JSONResponse({"status": "error", "message": "Neplatný režim."})


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
            f"🚨 <b>[NÚDZOVÝ MANUÁLNY PREDAJ / KILL SWITCH]</b>\n"
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


# --- 🖼️ CYBERPUNK HUD WEB ROZHRANIE ---
HTML_TEMPLATE_CONTENT = """<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Apex Scalper Pro - CyberHUD 3.2 (Živé Tokeny)</title>
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
            max-width: 680px; margin: 0; background: rgba(0, 0, 0, 0.75);
            backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(0, 255, 255, 0.3); border-radius: 18px; padding: 22px;
            box-shadow: 0 0 35px rgba(0, 255, 255, 0.25);
        }
        .header-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
        .brand-title { font-size: 1rem; font-weight: 800; color: #00f0ff; letter-spacing: 2px; text-transform: uppercase; }
        
        .controls-row { display: flex; justify-content: space-between; align-items: center; background: rgba(0,255,255,0.08); border: 1px solid rgba(0,255,255,0.2); padding: 10px 14px; border-radius: 10px; margin-bottom: 15px; }
        .mode-buttons a { text-decoration: none; background: rgba(0,0,0,0.5); border: 1px solid #00f0ff; color: #fff; padding: 6px 12px; font-size: 0.75rem; font-weight: bold; border-radius: 6px; display: inline-block; transition: all 0.2s; }
        .mode-buttons a.active { background: #00f0ff; color: #000; box-shadow: 0 0 10px #00f0ff; }
        
        .kill-switch-btn {
            background: linear-gradient(135deg, #ff0055, #990033);
            color: #fff; border: 1px solid #ff0055; padding: 6px 12px;
            border-radius: 6px; font-weight: 800; font-size: 0.75rem;
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
        th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid rgba(0, 255, 255, 0.20); font-size: 0.8rem; color: #ffffff; }
        th { font-weight: 700; color: #00f0ff; text-transform: uppercase; font-size: 0.72rem; }
        .pnl-green { color: #00ff88; font-weight: bold; }
        .pnl-red { color: #ff4444; font-weight: bold; }
        .chart-container { background: rgba(0, 0, 0, 0.5); border: 1px solid rgba(0, 255, 255, 0.2); border-radius: 10px; padding: 10px; margin-top: 10px; height: 150px; }
        .calendar-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(95px, 1fr)); gap: 8px; margin-top: 4px; }
        .calendar-day { background: rgba(0, 0, 0, 0.70); border: 1px solid rgba(0, 255, 255, 0.30); border-radius: 8px; padding: 8px; text-align: center; font-size: 0.75rem; }
        .refresh-info { color: rgba(255, 255, 255, 0.6); font-size: 0.7rem; margin-top: 20px; font-weight: 600; text-align: left; }
    </style>
    <meta http-equiv="refresh" content="2">
</head>
<body>
    <div class="container">
        <div class="header-bar">
            <div class="brand-title">⚡ Apex Scalper HUD 3.2 (Živé Tokeny)</div>
        </div>

        <div class="controls-row">
            <div class="mode-buttons">
                <span style="font-size: 0.75rem; color: #00f0ff; font-weight: bold; margin-right: 6px;">REŽIM:</span>
                <a href="/api/set-mode/AGGRESSIVE" class="{{ 'active' if current_mode == 'AGGRESSIVE' else '' }}">Agresívny</a>
                <a href="/api/set-mode/CONSERVATIVE" class="{{ 'active' if current_mode == 'CONSERVATIVE' else '' }}">Konzervatívny</a>
            </div>
            <button class="kill-switch-btn" onclick="triggerKillSwitch()">🚨 KILL SWITCH</button>
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
                    <tr><th>Pár</th><th>Vstup</th><th>Investícia</th><th>Aktuálna Cena</th><th>PnL (%)</th><th>PnL ($)</th></tr>
                    {% for t, details in trades.items() %}
                    <tr>
                        <td><strong>{{ details.market_pair }}</strong></td>
                        <td>{{ "%.4f" | format(details.entry_price) }}</td>
                        <td>{{ "%.2f" | format(details.invested_usdc) }} $</td>
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
                    ✅ Žiadny aktívny obchod. Bot vyčkáva na živú príležitosť.
                </div>
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
            <div class="section-title">📊 Živý Radar Trhu (USDC - Top Gainers)</div>
            {% if radar %}
                <table>
                    <tr><th>Symbol</th><th>Cena</th><th>24h Rast</th><th>Objem</th><th>Šanca</th></tr>
                    {% for item in radar %}
                    <tr>
                        <td><strong>{{ item.instId }}</strong></td>
                        <td>{{ "%.4f" | format(item.price) }}</td>
                        <td>+{{ "%.2f" | format(item.change) }}%</td>
                        <td>{{ "{:,.0f}".format(item.volume) }}</td>
                        <td><strong style="color: #00f0ff;">{{ item.probability }}%</strong></td>
                    </tr>
                    {% endfor %}
                </table>
            {% else %}
                <div style="font-size: 0.8rem; color: rgba(255, 255, 255, 0.7);">Skenujem trh...</div>
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

        <div class="section-block">
            <div class="section-title">📅 Kalendár Výsledkov (Denný PnL)</div>
            {% if calendar %}
                <div class="calendar-grid">
                    {% for day, data in calendar.items() %}
                    <div class="calendar-day">
                        <div>{{ day }}</div>
                        <div style="font-weight: bold; color: #00ff88; margin-top: 4px;">{{ "%+.2f" | format(data.pnl) }} $</div>
                        <div style="font-size: 0.65rem; color: rgba(255,255,255,0.7); margin-top: 2px;">{{ data.trades }} obch.</div>
                    </div>
                    {% endfor %}
                </div>
            {% else %}
                <div style="font-size: 0.8rem; color: rgba(255, 255, 255, 0.7);">Zatiaľ žiadne uzavreté obchody.</div>
            {% endif %}
        </div>

        <div class="refresh-info">APEX HUD 3.2 | Obnovenie 2s | SK Čas: {{ current_time }}</div>
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
                        label: 'Cena (USDC)',
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
      current_mode=CURRENT_MODE,
      current_stage=CURRENT_STAGE,
      price_history=PRICE_HISTORY,
      current_time=get_sk_time_str(),
  )


@app.get("/api/status")
async def api_status():
  return {
      "status": "online",
      "bot_type": "Apex Scalper Pro 3.2 (Live Top Gainer + Anti-Frag)",
      "mode": CURRENT_MODE,
      "current_stage": CURRENT_STAGE,
      "active_trades": ACTIVE_TRADES,
      "market_radar": MARKET_RADAR,
      "history": CLOSED_TRADES_HISTORY,
      "sk_time": get_sk_time_str(),
  }


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 10000))
  uvicorn.run(app, host="0.0.0.0", port=port)
