import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import base64
import hmac
import json
import os
from fastapi import FastAPI, Request
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

# --- STAV BOT-A & PARAMETRE PODĽA v5.4.5 ---
ACTIVE_TRADES = {}
MARKET_RADAR = []
DAILY_PNL_CALENDAR = {}
CLOSED_TRADES_HISTORY = []
PRICE_HISTORY = []
CURRENT_STAGE = "STAV: Mimo obchodu. Skenujem top 20 USDC trh..."
LAST_TRADE_TIME = {}
MAX_PARALLEL_TRADES = 1
trade_lock = asyncio.Lock()
IS_ORDER_PROCESSING = False

IS_BOT_ACTIVE = True
CURRENT_STRATEGY_MODE = "Agresívny Momentum (Breakout)"
CURRENT_TIMEFRAME = "15m"
ALLOWED_CURRENCIES = ["USDC", "USDT", "EUR"]
EXCLUDED_COINS = ["ZAMA"]

# Štatistiky pokročilého riadenia rizika
PEAK_PORTFOLIO_VALUE = 0.0
MAX_DRAWDOWN_PCT = 0.0

# 🎛️ NASTAVITEĽNÉ PARAMETRE INDIKÁTORA + 1000 USDC ALOKÁCIA
SETTINGS = {
    "fast_len": 5,
    "slow_len": 20,
    "signal_len": 9,
    "pivot_len": 2,
    "fib_choice": "0.786",
    "use_trailing": True,
    "trail_pct": 1.5,
    "use_breakeven": True,
    "be_trigger_pct": 1.2,
    "use_trailing_tp": True,
    "ttp_trigger_pct": 1.2,
    "ttp_callback_pct": 0.4,
    "min_vol": 10000.0,
    "min_change": -50.0,
    "max_change": 100.0,
    "fixed_trade_amount": 1000.0, # 💰 Presne 1000 USDC na obchod
    "use_btc_filter": True,       # BTC Trend Guard
    "use_weekend_filter": False,  # Víkendový filter
    "reentry_cooldown_sec": 60    # Kooldown pre opätovný vstup
}

def get_sk_time_str() -> str:
    sk_time = datetime.now(timezone.utc) + timedelta(hours=1)
    return sk_time.strftime("%Y-%m-%d %H:%M:%S")

def get_sk_date_str() -> str:
    sk_time = datetime.now(timezone.utc) + timedelta(hours=1)
    return sk_time.strftime("%Y-%m-%d")

def safe_float(val, default=0.0) -> float:
    if val is None:
        return default
    try:
        if isinstance(val, str) and val.strip() == "":
            return default
        return float(val)
    except (ValueError, TypeError):
        return default

def update_drawdown_stats(current_balance: float):
    global PEAK_PORTFOLIO_VALUE, MAX_DRAWDOWN_PCT
    if current_balance > PEAK_PORTFOLIO_VALUE:
        PEAK_PORTFOLIO_VALUE = current_balance
    elif PEAK_PORTFOLIO_VALUE > 0:
        dd = ((PEAK_PORTFOLIO_VALUE - current_balance) / PEAK_PORTFOLIO_VALUE) * 100.0
        if dd > MAX_DRAWDOWN_PCT:
            MAX_DRAWDOWN_PCT = dd

def record_closed_trade(pair, pnl_pct, pnl_usdc, reason):
    global CLOSED_TRADES_HISTORY
    day_str = get_sk_date_str()
    if day_str not in DAILY_PNL_CALENDAR:
        DAILY_PNL_CALENDAR[day_str] = {"pnl": 0.0, "trades": 0}
    DAILY_PNL_CALENDAR[day_str]["pnl"] += pnl_usdc
    DAILY_PNL_CALENDAR[day_str]["trades"] += 1

    CLOSED_TRADES_HISTORY.insert(0, {
        "pair": pair, "pnl_pct": pnl_pct, "pnl_usdc": pnl_usdc, "reason": reason, "time": get_sk_time_str()
    })
    if len(CLOSED_TRADES_HISTORY) > 20:
        CLOSED_TRADES_HISTORY.pop()

def okx_request(method: str, path: str, body: str = ""):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    message = timestamp + method.upper() + path + body
    mac = hmac.new(OKX_SECRET.encode('utf-8'), message.encode('utf-8'), digestmod='sha256')
    signature = base64.b64encode(mac.digest()).decode('utf-8')

    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "x-simulated-trading": "1",
        "Content-Type": "application/json"
    }

    url = "https://eea.okx.com" + path
    try:
        if method.upper() == "GET":
            response = requests.get(url, headers=headers, timeout=5)
        else:
            response = requests.post(url, headers=headers, data=body, timeout=5)
        return response.json()
    except Exception as err:
        return {"code": "-1", "msg": str(err)}

def check_btc_health() -> bool:
    """🛡️ Overí, či Bitcoin neklesá (BTC Trend Guard)"""
    if not SETTINGS["use_btc_filter"]:
        return True
    try:
        url = f"/api/v5/market/candles?instId=BTC-USDC&bar={CURRENT_TIMEFRAME}&limit=10"
        res = okx_request("GET", url)
        if res.get("code") == "0" and res.get("data"):
            data = res["data"]
            data.reverse()
            closes = [safe_float(c[4]) for c in data]
            avg_btc = sum(closes) / len(closes)
            return closes[-1] >= avg_btc
    except Exception:
        pass
    return True

def get_position_budget(free_capital: float) -> float:
    """💰 Vždy obchodovať za presne 1000 USDC (ak je dostatok kapitálu)"""
    target = float(SETTINGS["fixed_trade_amount"])
    if free_capital < target:
        return free_capital * 0.95 # Bezpečnostná poistka ak demo účet nemá plných 1000 USDC
    return target

def evaluate_strategy_checklist(inst_id: str) -> dict:
    result = {
        "volume_status": False,
        "impulse_status": False,
        "fib_status": False,
        "buy_ready": False,
        "probability": 35,
        "pokyn_text": "⚪ ČAKAJ",
        "pokyn_color": "silver",
        "volume_text": "🔴 SLABÉ",
        "impuls_text": "⚪ ČAKÁ",
        "impuls_color": "gray"
    }
    try:
        url = f"/api/v5/market/candles?instId={inst_id}&bar={CURRENT_TIMEFRAME}&limit=45"
        res = okx_request("GET", url)
        if res.get("code") != "0" or not res.get("data"):
            return result

        raw_candles = res["data"]
        raw_candles.reverse()

        if len(raw_candles) < 30:
            return result

        closes = [safe_float(c[4]) for c in raw_candles]
        highs = [safe_float(c[2]) for c in raw_candles]
        lows = [safe_float(c[3]) for c in raw_candles]
        volumes = [safe_float(c[5]) for c in raw_candles]
        opens = [safe_float(c[1]) for c in raw_candles]

        vol_directions = [v if closes[i] > opens[i] else -v for i, v in enumerate(volumes)]

        def calc_ema(data, length):
            alpha = 2 / (length + 1)
            ema_val = data[0]
            for val in data[1:]:
                ema_val = (val * alpha) + (ema_val * (1 - alpha))
            return ema_val

        fast_len = int(SETTINGS["fast_len"])
        slow_len = int(SETTINGS["slow_len"])
        signal_len = int(SETTINGS["signal_len"])

        fast_ma = calc_ema(vol_directions, min(fast_len, len(vol_directions)))
        slow_ma = calc_ema(vol_directions, min(slow_len, len(vol_directions)))
        oscillator_val = fast_ma - slow_ma

        osc_history = [fast_ma - slow_ma for _ in range(signal_len + 5)]
        signal_line = calc_ema(osc_history, signal_len)
        prev_signal_line = calc_ema(osc_history[:-1], signal_len) if len(osc_history) > 1 else signal_line

        signal_above_zero = signal_line > 0
        signal_crossover = (signal_line > prev_signal_line) and (oscillator_val >= signal_line)
        volume_bullish = (oscillator_val > 0) and signal_above_zero and signal_crossover

        result["volume_status"] = volume_bullish
        result["volume_text"] = "🟢 SILNÉ" if volume_bullish else "🔴 SLABÉ"

        recent_high = max(highs[-25:])
        recent_low = min(lows[-25:])
        range_size = recent_high - recent_low
        
        fib_val = float(SETTINGS["fib_choice"])
        fib_target = recent_high - (range_size * fib_val) if range_size > 0 else recent_low
        fib_touch = lows[-1] <= fib_target
        result["fib_status"] = fib_touch

        if CURRENT_STRATEGY_MODE == "Agresívny Momentum (Breakout)":
            price_action_ok = closes[-1] > closes[-2]
            buy_trigger = volume_bullish and price_action_ok
            result["impulse_status"] = price_action_ok
            result["buy_ready"] = buy_trigger
            result["probability"] = 90 if buy_trigger else (60 if volume_bullish else 35)
        else:
            fib_buy = volume_bullish and fib_touch
            result["impulse_status"] = fib_touch
            result["buy_ready"] = fib_buy
            result["probability"] = 95 if fib_buy else (55 if volume_bullish else 30)

        coin_base = inst_id.split("-")[0]
        is_in_trade = coin_base in ACTIVE_TRADES

        if is_in_trade:
            result["impuls_text"] = "🟢 AKTÍVNY (V TRHU)"
            result["impuls_color"] = "green"
            result["pokyn_text"] = "🔥 DRŽAŤ"
            result["pokyn_color"] = "orange"
        elif result["buy_ready"]:
            result["impuls_text"] = "🟢 AKTÍVNY (IMPULZ)"
            result["impuls_color"] = "green"
            result["pokyn_text"] = "🚀 NAKÚPIŤ"
            result["pokyn_color"] = "green"
        else:
            result["impuls_text"] = "⚪ ČAKÁ"
            result["impuls_color"] = "gray"
            result["pokyn_text"] = "⚪ ČAKAJ"
            result["pokyn_color"] = "silver"

    except Exception as e:
        print(f"❌ Chyba checklistu pre {inst_id}: {e}", flush=True)

    return result

async def top_gainer_scanner_loop():
    global ACTIVE_TRADES, LAST_TRADE_TIME, MARKET_RADAR, CURRENT_STAGE, PRICE_HISTORY, IS_ORDER_PROCESSING, IS_BOT_ACTIVE
    await asyncio.sleep(2)

    print(f"[{get_sk_time_str()}] ⚡ APEX SCALPER BOT SPUSTENÝ [1000 USDC | TF: {CURRENT_TIMEFRAME}]", flush=True)
    send_telegram_msg(f"🚀 <b>[APEX SCALPER]</b> Bot spustil monitorovanie trhu (Alokácia: 1000 USDC).")

    while True:
        try:
            if not IS_BOT_ACTIVE:
                CURRENT_STAGE = "STAV: ⏸️ BOT JE VYPNUTÝ"
                await asyncio.sleep(3)
                continue

            if SETTINGS["use_weekend_filter"] and datetime.now(timezone.utc).weekday() in [5, 6]:
                CURRENT_STAGE = "STAV: ⏸️ VÍKENDOVÝ FILTER AKTÍVNY"
                await asyncio.sleep(10)
                continue

            if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES or IS_ORDER_PROCESSING:
                CURRENT_STAGE = "STAV: 🔴 V OBCHODE / SPRACOVÁVAM PRÍKAZ!"
                await asyncio.sleep(4)
                continue

            if not check_btc_health():
                CURRENT_STAGE = "STAV: 🛡️ BTC TREND GUARD AKTÍVNY (BTC klesá)"
                await asyncio.sleep(6)
                continue

            balance_res = okx_request("GET", "/api/v5/account/balance")
            free_capital = 1000.0
            if balance_res.get("code") == "0":
                for acc in balance_res.get("data", []):
                    for detail in acc.get("details", []):
                        if detail.get("ccy") == "USDC":
                            free_capital = max(free_capital, safe_float(detail.get("availBal", 0)))
                            update_drawdown_stats(free_capital)

            CURRENT_STAGE = f"STAV: 🟢 SKENER TOP 20 USDC [TF: {CURRENT_TIMEFRAME}]. Skenujem..."
            tickers_res = okx_request("GET", "/api/v5/market/tickers?instType=SPOT")
            if tickers_res.get("code") != "0" or not tickers_res.get("data"):
                await asyncio.sleep(5)
                continue

            candidates = []
            for item in tickers_res["data"]:
                inst_id = item.get("instId", "").upper()
                if not inst_id.endswith("-USDC") or inst_id in ["USDC-USDC", "USDT-USDC", "BTC-USDC", "ETH-USDC"]:
                    continue
                if inst_id.split("-")[0] in EXCLUDED_COINS:
                    continue

                try:
                    open24h = safe_float(item.get("open24h", 0))
                    last_p = safe_float(item.get("last", 0))
                    vol24h = safe_float(item.get("volCcy24h", 0) or item.get("vol24h", 0) or 0)

                    if open24h > 0 and last_p > 0 and vol24h >= float(SETTINGS["min_vol"]):
                        change_pct = ((last_p - open24h) / open24h) * 100
                        if float(SETTINGS["min_change"]) <= change_pct <= float(SETTINGS["max_change"]):
                            chk = evaluate_strategy_checklist(inst_id)

                            candidates.append({
                                "instId": inst_id, "price": last_p, "change": change_pct, "volume": vol24h,
                                "volume_status": chk["volume_status"], "impulse_status": chk["impulse_status"],
                                "fib_status": chk["fib_status"], "buy_ready": chk["buy_ready"], "probability": chk["probability"],
                                "volume_text": chk["volume_text"], "impuls_text": chk["impuls_text"],
                                "pokyn_text": chk["pokyn_text"], "pokyn_color": chk["pokyn_color"]
                            })
                except Exception:
                    continue

            candidates.sort(key=lambda x: (x["buy_ready"], x["probability"], x["volume"]), reverse=True)
            if candidates:
                MARKET_RADAR = candidates[:20]

            for cand in candidates:
                if not cand["buy_ready"]:
                    continue

                coin_name = cand["instId"].split("-")[0]
                now_ts = datetime.now().timestamp()
                
                cooldown_val = int(SETTINGS["reentry_cooldown_sec"])
                if coin_name in LAST_TRADE_TIME and now_ts - LAST_TRADE_TIME[coin_name] < cooldown_val:
                    continue

                async with trade_lock:
                    if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES or IS_ORDER_PROCESSING:
                        break

                    IS_ORDER_PROCESSING = True
                    LAST_TRADE_TIME[coin_name] = now_ts

                    current_price = cand["price"]
                    safe_budget = get_position_budget(free_capital)
                    
                    coin_amount = round(safe_budget / current_price, 2)
                    if coin_amount <= 0:
                        coin_amount = round(safe_budget / current_price, 4)

                    order_body = json.dumps({
                        "instId": cand["instId"], "tdMode": "cash", "side": "buy",
                        "ordType": "limit", "px": str(round(current_price, 4)), "sz": str(coin_amount), "tgtCcy": "base_ccy"
                    })

                    order_res = okx_request("POST", "/api/v5/trade/order", order_body)
                    if order_res.get("code") != "0":
                        IS_ORDER_PROCESSING = False
                        break

                    trail_pct_val = float(SETTINGS["trail_pct"]) / 100.0
                    sl_price = current_price * (1 - trail_pct_val)
                    PRICE_HISTORY = [current_price]

                    ACTIVE_TRADES[coin_name] = {
                        "market_pair": cand["instId"],
                        "entry_price": current_price,
                        "current_price": current_price,
                        "highest_price": current_price,
                        "current_sl_price": sl_price,
                        "break_even_activated": False,
                        "trailing_tp_active": False,
                        "trailing_tp_stop_level": 0.0,
                        "amount": coin_amount,
                        "bought_at": get_sk_time_str(),
                        "invested_usdc": safe_budget,
                        "live_pnl_pct": 0.0,
                        "live_pnl_usdc": 0.0,
                    }

                    IS_ORDER_PROCESSING = False
                    CURRENT_STAGE = f"STAV: 🔴 V OBCHODE [{cand['instId']}] za {safe_budget} USDC..."

                    send_telegram_msg(
                        f"🟢 <b>[APEX SCALPER - NÁKUP 1000 USDC]</b>\n"
                        f"<b>Pár:</b> {cand['instId']}\n"
                        f"<b>Cena:</b> {current_price:.4f}\n"
                        f"<b>Alokácia:</b> {safe_budget:.2f} USDC\n"
                        f"<b>Stop-Loss:</b> {sl_price:.4f} (-{SETTINGS['trail_pct']}%)\n"
                        f"<b>Pravdepodobnosť:</b> {cand['probability']}%"
                    )
                    break

        except Exception as scan_err:
            IS_ORDER_PROCESSING = False
            print(f"❌ Chyba v skeneri: {scan_err}", flush=True)

        await asyncio.sleep(10)

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
                    current_price = safe_float(ticker_res["data"][0]["last"])
                    if current_price <= 0:
                        continue

                    pnl_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
                    pnl_usdc = trade.get("invested_usdc", 1000.0) * (pnl_pct / 100.0)

                    trade["current_price"] = current_price
                    trade["live_pnl_pct"] = pnl_pct
                    trade["live_pnl_usdc"] = pnl_usdc

                    trail_dist = float(SETTINGS["trail_pct"]) / 100.0
                    be_trigger = float(SETTINGS["be_trigger_pct"])
                    ttp_trigger = float(SETTINGS["ttp_trigger_pct"])
                    ttp_callback = float(SETTINGS["ttp_callback_pct"]) / 100.0

                    if current_price > trade["highest_price"]:
                        trade["highest_price"] = current_price
                        if SETTINGS["use_trailing"] and pnl_pct >= ttp_trigger:
                            new_trailing_sl = trade["highest_price"] * (1 - trail_dist)
                            if new_trailing_sl > trade["current_sl_price"]:
                                trade["current_sl_price"] = new_trailing_sl

                    if SETTINGS["use_breakeven"] and not trade["break_even_activated"] and pnl_pct >= be_trigger:
                        trade["break_even_activated"] = True
                        if trade["entry_price"] > trade["current_sl_price"]:
                            trade["current_sl_price"] = trade["entry_price"]

                    if SETTINGS["use_trailing_tp"] and not trade["trailing_tp_active"] and pnl_pct >= ttp_trigger:
                        trade["trailing_tp_active"] = True

                    if trade["trailing_tp_active"]:
                        trade["trailing_tp_stop_level"] = trade["highest_price"] * (1 - ttp_callback)
                        if current_price <= trade["trailing_tp_stop_level"]:
                            try:
                                okx_request("POST", "/api/v5/trade/order", json.dumps({
                                    "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                                }))
                                record_closed_trade(symbol, pnl_pct, pnl_usdc, "Trailing TP")
                                send_telegram_msg(f"🎯 <b>[TP EXIT (Trailing TP)]</b>\n<b>Pár:</b> {symbol}\n<b>Zisk:</b> +{pnl_pct:.2f}% (+{pnl_usdc:.2f}$)")
                            except Exception as err:
                                print(f"❌ Chyba Trailing TP: {err}", flush=True)

                            if coin in ACTIVE_TRADES:
                                del ACTIVE_TRADES[coin]
                            CURRENT_STAGE = "STAV: 🟢 Mimo obchodu. Trailing TP úspešne uzamkol zisk!"
                            continue

                    if current_price <= trade["current_sl_price"]:
                        try:
                            okx_request("POST", "/api/v5/trade/order", json.dumps({
                                "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                            }))
                            record_closed_trade(symbol, pnl_pct, pnl_usdc, "Stop-Loss")
                            send_telegram_msg(f"🚨 <b>[STOP-LOSS ZATVORENÝ]</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}%")
                        except Exception as err:
                            print(f"❌ Chyba SL: {err}", flush=True)

                        if coin in ACTIVE_TRADES:
                            del ACTIVE_TRADES[coin]
                        CURRENT_STAGE = "STAV: 🟢 Mimo obchodu. Pozícia zatvorená na Stop-Loss."
                        continue

        except Exception as main_err:
            print(f"❌ Chyba v monitorovaní: {main_err}", flush=True)

        await asyncio.sleep(2)

@asynccontextmanager
async def lifespan(app: FastAPI):
    s_task = asyncio.create_task(top_gainer_scanner_loop())
    m_task = asyncio.create_task(advanced_risk_monitor_loop())
    yield
    s_task.cancel()
    m_task.cancel()

app = FastAPI(lifespan=lifespan)

@app.get("/api/set-strategy/{mode}")
async def set_strategy(mode: str):
    global CURRENT_STRATEGY_MODE
    CURRENT_STRATEGY_MODE = "Agresívny Momentum (Breakout)" if "Breakout" in mode else "Korekcia (Fibonacci Touch)"
    return JSONResponse({"status": "success", "mode": CURRENT_STRATEGY_MODE})

@app.get("/api/set-tf/{tf}")
async def set_timeframe(tf: str):
    global CURRENT_TIMEFRAME
    if tf in ["3m", "5m", "15m", "30m", "1h", "4h"]:
        CURRENT_TIMEFRAME = tf
    return JSONResponse({"status": "success", "timeframe": CURRENT_TIMEFRAME})

@app.get("/api/bot/start")
async def bot_start():
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = True
    send_telegram_msg("🟢 Bot bol manuálne <b>ZAPNUTÝ</b>.")
    return JSONResponse({"status": "success", "message": "Bot bol zapnutý."})

@app.get("/api/bot/stop")
async def bot_stop():
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = False
    send_telegram_msg("🔴 Bot bol manuálne <b>VYPNUTÝ</b>.")
    return JSONResponse({"status": "success", "message": "Bot bol pozastavený."})

@app.post("/api/update-settings")
async def update_settings(request: Request):
    global SETTINGS
    try:
        data = await request.json()
        for key in SETTINGS.keys():
            if key in data:
                if isinstance(SETTINGS[key], bool):
                    SETTINGS[key] = bool(data[key])
                elif isinstance(SETTINGS[key], int):
                    SETTINGS[key] = int(data[key])
                elif isinstance(SETTINGS[key], float):
                    SETTINGS[key] = float(data[key])
                else:
                    SETTINGS[key] = str(data[key])
        return JSONResponse({"status": "success", "message": "Nastavenia boli úspešne uložené!"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)

@app.get("/api/live-candles/{symbol}")
async def get_live_candles(symbol: str):
    try:
        url = f"/api/v5/market/candles?instId={symbol}&bar={CURRENT_TIMEFRAME}&limit=60"
        res = okx_request("GET", url)
        if res.get("code") != "0" or not res.get("data"):
            return JSONResponse({"status": "error", "candles": [], "fib0786": None})

        raw = res["data"]
        raw.reverse()

        candles = []
        highs = []
        lows = []
        for c in raw:
            ts = int(c[0]) // 1000
            candles.append({"time": ts, "open": safe_float(c[1]), "high": safe_float(c[2]), "low": safe_float(c[3]), "close": safe_float(c[4])})
            highs.append(safe_float(c[2]))
            lows.append(safe_float(c[3]))

        recent_high = max(highs[-25:]) if len(highs) >= 25 else max(highs)
        recent_low = min(lows[-25:]) if len(lows) >= 25 else min(lows)
        fib_0786 = recent_high - ((recent_high - recent_low) * 0.786)

        return JSONResponse({"status": "success", "candles": candles, "fib0786": fib_0786})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/emergency-sell")
async def emergency_sell():
    global ACTIVE_TRADES, CURRENT_STAGE
    async with trade_lock:
        if not ACTIVE_TRADES:
            return JSONResponse({"status": "warning", "message": "Žiadny aktívny obchod na predaj."})

        sold_summary = []
        for coin in list(ACTIVE_TRADES.keys()):
            trade = ACTIVE_TRADES[coin]
            symbol = trade["market_pair"]
            try:
                okx_request("POST", "/api/v5/trade/order", json.dumps({
                    "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                }))
                record_closed_trade(symbol, trade.get("live_pnl_pct", 0.0), trade.get("live_pnl_usdc", 0.0), "Kill Switch")
                sold_summary.append(symbol)
            except Exception as e:
                print(f"Chyba emergency sell: {e}", flush=True)
            del ACTIVE_TRADES[coin]

        CURRENT_STAGE = "STAV: 🟢 Mimo obchodu (Kill Switch)."
        return JSONResponse({"status": "success", "message": "Pozícia predaná.", "sold": sold_summary})

HTML_TEMPLATE_CONTENT = """<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Apex Scalper PRO (1000 USDC)</title>
    <script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        * { box-sizing: border-box; }
        body { font-family: 'Segoe UI', Tahoma, sans-serif; margin: 0; padding: 20px; background: #0b0e14; color: #fff; min-height: 100vh; }
        .container { max-width: 900px; margin: 0 auto; background: #131722; border: 1px solid rgba(0,255,255,0.2); border-radius: 12px; padding: 20px; box-shadow: 0 0 20px rgba(0,255,255,0.1); }
        .header-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }
        .brand-title { font-size: 1.1rem; font-weight: 800; color: #00f0ff; letter-spacing: 1px; }
        .controls-row { display: flex; justify-content: space-between; align-items: center; background: rgba(0,255,255,0.05); padding: 10px; border-radius: 8px; margin-bottom: 15px; gap: 8px; flex-wrap: wrap; }
        .mode-buttons a, .tf-buttons a { text-decoration: none; background: #1e222d; border: 1px solid #00f0ff; color: #fff; padding: 5px 10px; font-size: 0.75rem; font-weight: bold; border-radius: 5px; }
        .mode-buttons a.active, .tf-buttons a.active { background: #00f0ff; color: #000; }
        .bot-state-btn { padding: 5px 10px; border-radius: 5px; font-weight: bold; font-size: 0.75rem; cursor: pointer; border: 1px solid; }
        .btn-start { background: rgba(0,255,136,0.2); color: #00ff88; border-color: #00ff88; }
        .btn-stop { background: rgba(255,68,68,0.2); color: #ff4444; border-color: #ff4444; }
        .kill-switch-btn { background: #ff0055; color: #fff; border: none; padding: 5px 10px; border-radius: 5px; font-weight: bold; font-size: 0.75rem; cursor: pointer; }
        .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 15px; }
        .stat-card { background: #1e222d; border: 1px solid rgba(0,255,255,0.1); border-radius: 8px; padding: 10px; text-align: center; }
        .stat-label { font-size: 0.65rem; color: #00f0ff; text-transform: uppercase; font-weight: bold; }
        .stat-value { font-size: 1rem; font-weight: bold; margin-top: 4px; }
        .status-banner { background: rgba(0,240,255,0.1); border: 1px solid #00f0ff; border-radius: 8px; padding: 10px; font-size: 0.8rem; font-weight: bold; margin-bottom: 15px; }
        .section-title { font-size: 0.85rem; font-weight: bold; color: #00f0ff; margin-bottom: 8px; text-transform: uppercase; }
        table { width: 100%; border-collapse: collapse; margin-top: 5px; }
        th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 0.75rem; }
        th { color: #00f0ff; text-transform: uppercase; }
        .pnl-green { color: #00ff88; font-weight: bold; }
        .pnl-red { color: #ff4444; font-weight: bold; }
        #tvChartContainer { width: 100%; height: 280px; background: #0b0e14; border: 1px solid rgba(0,255,255,0.2); border-radius: 8px; margin-top: 10px; }
        .settings-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; background: #1e222d; padding: 10px; border-radius: 8px; }
        .setting-item { display: flex; flex-direction: column; font-size: 0.7rem; }
        .setting-item label { color: #00f0ff; margin-bottom: 2px; font-weight: bold; }
        .setting-item input { background: #131722; border: 1px solid rgba(0,255,255,0.3); color: #fff; padding: 5px; border-radius: 4px; }
        .save-btn { margin-top: 8px; background: #00f0ff; color: #000; border: none; padding: 6px 12px; font-weight: bold; border-radius: 4px; cursor: pointer; font-size: 0.7rem; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header-bar">
            <div class="brand-title">⚡ Apex Scalper Bot (1000 USDC Alokácia)</div>
            <a href="/" style="color: #00f0ff; font-size: 0.75rem; text-decoration: none;">🔄 Obnoviť</a>
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
            </div>
            <div>
                {% if is_bot_active %}
                    <button class="bot-state-btn btn-stop" onclick="toggleBot('/api/bot/stop')">🔴 Vypnúť</button>
                {% else %}
                    <button class="bot-state-btn btn-start" onclick="toggleBot('/api/bot/start')">🟢 Zapnúť</button>
                {% endif %}
                <button class="kill-switch-btn" onclick="triggerKillSwitch()">🚨 KILL</button>
            </div>
        </div>

        <div class="stats-grid">
            <div class="stat-card"><div class="stat-label">Obchody</div><div class="stat-value">{{ total_trades }}</div></div>
            <div class="stat-card"><div class="stat-label">Win Rate</div><div class="stat-value" style="color: #00ff88;">{{ win_rate }}%</div></div>
            <div class="stat-card"><div class="stat-label">Celkový PnL</div><div class="stat-value {{ 'pnl-green' if total_pnl_usdc >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(total_pnl_usdc) }} $</div></div>
            <div class="stat-card"><div class="stat-label">Max Drawdown</div><div class="stat-value" style="color: #ffcc00;">{{ "%.2f" | format(max_drawdown) }}%</div></div>
        </div>
        
        <div class="status-banner">{{ current_stage }}</div>

        <div style="margin-bottom: 20px;">
            <div class="section-title">⚙️ Nastavenia (Pevná alokácia 1000 USDC)</div>
            <form id="settingsForm" onsubmit="saveSettings(event)">
                <div class="settings-grid">
                    <div class="setting-item"><label>Veľkosť pozície (USDC)</label><input type="number" name="fixed_trade_amount" value="{{ settings.fixed_trade_amount }}"></div>
                    <div class="setting-item"><label>Min. Objem (USDC)</label><input type="number" name="min_vol" value="{{ settings.min_vol }}"></div>
                    <div class="setting-item"><label>Trailing Stop (%)</label><input type="number" step="0.1" name="trail_pct" value="{{ settings.trail_pct }}"></div>
                    <div class="setting-item"><label>Break-Even (%)</label><input type="number" step="0.1" name="be_trigger_pct" value="{{ settings.be_trigger_pct }}"></div>
                    <div class="setting-item"><label>Trailing TP (%)</label><input type="number" step="0.1" name="ttp_trigger_pct" value="{{ settings.ttp_trigger_pct }}"></div>
                    <div class="setting-item"><label>TP Callback (%)</label><input type="number" step="0.1" name="ttp_callback_pct" value="{{ settings.ttp_callback_pct }}"></div>
                </div>
                <button type="submit" class="save-btn">💾 Uložiť nastavenia</button>
            </form>
        </div>

        <div style="margin-bottom: 20px;">
            <div class="section-title">🔴 Aktívny Obchod & Živý Graf</div>
            {% if trades %}
                <table>
                    <tr><th>Pár</th><th>Vstup</th><th>Alokacja</th><th>Cena</th><th>PnL (%)</th><th>PnL ($)</th></tr>
                    {% for t, details in trades.items() %}
                    <tr id="activeTradeRow" data-symbol="{{ details.market_pair }}">
                        <td><strong>{{ details.market_pair }}</strong></td>
                        <td>{{ "%.4f" | format(details.entry_price) }}</td>
                        <td>{{ "%.2f" | format(details.invested_usdc) }} USDC</td>
                        <td>{{ "%.4f" | format(details.current_price) }}</td>
                        <td class="{{ 'pnl-green' if details.live_pnl_pct >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(details.live_pnl_pct) }}%</td>
                        <td class="{{ 'pnl-green' if details.live_pnl_usdc >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(details.live_pnl_usdc) }} $</td>
                    </tr>
                    {% endfor %}
                </table>
                <div id="tvChartContainer"></div>
            {% else %}
                <div style="font-size: 0.75rem; color: #888; background: #1e222d; padding: 10px; border-radius: 6px;">
                    ✅ Žiadny aktívny obchod. Skenujem Top 20 USDC trh za 1000 USDC...
                </div>
            {% endif %}
        </div>

        <div style="margin-bottom: 20px;">
            <div class="section-title">📊 Top 20 USDC Radar</div>
            {% if radar %}
                <table>
                    <tr><th>Symbol</th><th>24h Zmena</th><th>Objemový Mom.</th><th>Pripravenosť</th></tr>
                    {% for item in radar[:8] %}
                    <tr>
                        <td><strong>{{ item.instId }}</strong></td>
                        <td class="{{ 'pnl-green' if item.change >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(item.change) }}%</td>
                        <td><span style="color: {{ '#00ff88' if item.volume_status else '#ff4444' }};">{{ '🟢' if item.volume_status else '🔴' }}</span></td>
                        <td><strong style="color: {{ '#00ff88' if item.buy_ready else '#00f0ff' }};">{{ '🚀 NAKUPOVAŤ' if item.buy_ready else item.probability|string + '%' }}</strong></td>
                    </tr>
                    {% endfor %}
                </table>
            {% else %}
                <div style="font-size: 0.75rem; color: #888;">Načítavam radar...</div>
            {% endif %}
        </div>

        <div>
            <div class="section-title">📜 História Obchodov</div>
            {% if history %}
                <table>
                    <tr><th>Čas</th><th>Pár</th><th>Dôvod</th><th>Výsledok (%)</th><th>Výsledok ($)</th></tr>
                    {% for h in history[:5] %}
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
                <div style="font-size: 0.75rem; color: #888;">Zatiaľ žiadne uzavreté obchody.</div>
            {% endif %}
        </div>
    </div>

    <script>
        async function toggleBot(endpoint) {
            const res = await fetch(endpoint);
            const data = await res.json();
            alert(data.message);
            window.location.reload();
        }

        async function triggerKillSwitch() {
            if (confirm("⚠️ Naozaj chceš okamžite predať pozíciu?")) {
                const res = await fetch('/api/emergency-sell', { method: 'POST' });
                const data = await res.json();
                alert(data.message);
                window.location.reload();
            }
        }

        async function saveSettings(event) {
            event.preventDefault();
            const form = event.target;
            const data = {
                fixed_trade_amount: parseFloat(form.fixed_trade_amount.value),
                min_vol: parseFloat(form.min_vol.value),
                trail_pct: parseFloat(form.trail_pct.value),
                be_trigger_pct: parseFloat(form.be_trigger_pct.value),
                ttp_trigger_pct: parseFloat(form.ttp_trigger_pct.value),
                ttp_callback_pct: parseFloat(form.ttp_callback_pct.value)
            };
            const res = await fetch('/api/update-settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });
            const result = await res.json();
            alert(result.message);
            window.location.reload();
        }

        async function initLiveChart() {
            const row = document.getElementById('activeTradeRow');
            const container = document.getElementById('tvChartContainer');
            if (!row || !container) return;
            const symbol = row.getAttribute('data-symbol');
            try {
                const res = await fetch(`/api/live-candles/${symbol}`);
                const data = await res.json();
                if (data.status !== 'success' || !data.candles) return;

                const chart = LightweightCharts.createChart(container, {
                    width: container.clientWidth, height: 280,
                    layout: { background: { type: 'solid', color: 'transparent' }, textColor: '#00f0ff' },
                    grid: { vertLines: { color: 'rgba(255,255,255,0.03)' }, horzLines: { color: 'rgba(255,255,255,0.03)' } }
                });
                const series = chart.addCandlestickSeries({ upColor: '#00ff88', downColor: '#ff4444' });
                series.setData(data.candles);
                if (data.fib0786) {
                    series.createPriceLine({ price: data.fib0786, color: '#ffcc00', lineWidth: 2, title: 'Fib 0.786' });
                }
                chart.timeScale().fitContent();
            } catch (e) { console.error(e); }
        }
        window.addEventListener('DOMContentLoaded', initLiveChart);
    </script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    account_balances = {curr: {"available_balance": 0.0} for curr in ALLOWED_CURRENCIES}
    try:
        balance_res = okx_request("GET", "/api/v5/account/balance")
        if balance_res.get("code") == "0":
            for acc in balance_res.get("data", []):
                for detail in acc.get("details", []):
                    ccy = detail.get("ccy")
                    if ccy in ALLOWED_CURRENCIES:
                        account_balances[ccy] = {"available_balance": safe_float(detail.get("availBal", 0))}
    except Exception:
        pass

    total_trades = len(CLOSED_TRADES_HISTORY)
    winning_trades = sum(1 for t in CLOSED_TRADES_HISTORY if t["pnl_pct"] > 0)
    win_rate = round((winning_trades / total_trades * 100) if total_trades > 0 else 0.0, 1)
    total_pnl_usdc = sum(t["pnl_usdc"] for t in CLOSED_TRADES_HISTORY)

    template = Template(HTML_TEMPLATE_CONTENT)
    return template.render(
        balances=account_balances,
        trades=ACTIVE_TRADES,
        radar=MARKET_RADAR,
        history=CLOSED_TRADES_HISTORY,
        total_trades=total_trades,
        win_rate=win_rate,
        total_pnl_usdc=total_pnl_usdc,
        max_drawdown=MAX_DRAWDOWN_PCT,
        current_mode=CURRENT_STRATEGY_MODE,
        current_tf=CURRENT_TIMEFRAME,
        current_stage=CURRENT_STAGE,
        is_bot_active=IS_BOT_ACTIVE,
        settings=SETTINGS
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
