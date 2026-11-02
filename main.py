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

# 🎛️ NASTAVITEĽNÉ PARAMETRE INDIKÁTORA v5.4.5 + TV ZLADENIE
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
    "use_btc_filter": True,       # BTC Trend Guard (Kúp len keď BTC stúpa)
    "use_dynamic_sizing": True,   # Dynamická veľkość pozície podľa ATR/volatility
    "use_weekend_filter": False,  # Filtrovať obchodovanie cez víkendy
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
    """🛡️ Filter 1: Overí, či Bitcoin neklesá (BTC Trend Guard)"""
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

def calculate_volatility_position_size(free_capital: float, current_price: float, inst_id: str) -> float:
    """📉 Filter 2: Dynamická veľkosť pozície podľa volatility (ATR)"""
    base_budget = min(free_capital * 0.95, 500.0)
    if not SETTINGS["use_dynamic_sizing"]:
        return base_budget
    
    try:
        url = f"/api/v5/market/candles?instId={inst_id}&bar={CURRENT_TIMEFRAME}&limit=15"
        res = okx_request("GET", url)
        if res.get("code") == "0" and res.get("data"):
            candles = res["data"]
            highs = [safe_float(c[2]) for c in candles]
            lows = [safe_float(c[3]) for c in candles]
            ranges = [h - l for h, l in zip(highs, lows)]
            avg_range = sum(ranges) / len(ranges)
            volatility_pct = (avg_range / current_price) * 100.0
            
            if volatility_pct > 3.0:
                return base_budget * 0.75
            elif volatility_pct < 1.0:
                return base_budget
    except Exception:
        pass
    return base_budget

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

    print(f"[{get_sk_time_str()}] ⚡ APEX SCALPER v5.4.5 SPUSTENÝ [Top 20 USDC | TF: {CURRENT_TIMEFRAME}]", flush=True)
    send_telegram_msg(f"🚀 <b>[APEX SCALPER v5.4.5]</b> Skener spustil monitorovanie trhu.")

    while True:
        try:
            if not IS_BOT_ACTIVE:
                CURRENT_STAGE = "STAV: ⏸️ BOT JE VYPNUTÝ (Pozastavené cez HUD)"
                await asyncio.sleep(3)
                continue

            if SETTINGS["use_weekend_filter"] and datetime.now(timezone.utc).weekday() in [5, 6]:
                CURRENT_STAGE = "STAV: ⏸️ VÍKENDOVÝ FILTER AKTÍVNY (Čakám na pondelok)"
                await asyncio.sleep(10)
                continue

            if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES or IS_ORDER_PROCESSING:
                CURRENT_STAGE = "STAV: 🔴 V OBCHODE / SPRACOVÁVAM PRÍKAZ! Čakám..."
                await asyncio.sleep(4)
                continue

            btc_ok = check_btc_health()
            if not btc_ok:
                CURRENT_STAGE = "STAV: 🛡️ BTC TREND GUARD AKTÍVNY (BTC klesá, pozastavujem nákupy)"
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

            CURRENT_STAGE = f"STAV: 🟢 SKENER TOP 20 USDC [TF: {CURRENT_TIMEFRAME} | Režim: {CURRENT_STRATEGY_MODE}]. Skenujem..."
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
                    safe_budget = calculate_volatility_position_size(free_capital, current_price, cand["instId"])
                    
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
                    CURRENT_STAGE = f"STAV: 🔴 V OBCHODE [{cand['instId']}] (TF: {CURRENT_TIMEFRAME})..."

                    send_telegram_msg(
                        f"🟢 <b>[APEX SCALPER v5.4.5 - NÁKUP]</b>\n"
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
                coin_base = symbol.split("-")[0]

                ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={symbol}")
                if ticker_res.get("code") == "0" and ticker_res.get("data"):
                    current_price = safe_float(ticker_res["data"][0]["last"])
                    if current_price <= 0:
                        continue

                    PRICE_HISTORY.append(current_price)
                    if len(PRICE_HISTORY) > 30:
                        PRICE_HISTORY.pop(0)

                    pnl_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
                    pnl_usdc = trade.get("invested_usdc", 100.0) * (pnl_pct / 100.0)

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
                                record_closed_trade(symbol, pnl_pct, pnl_usdc, "Trailing TP v5.4.5")
                                send_telegram_msg(f"🎯 <b>[TP EXIT v5.4.5]</b>\n<b>Pár:</b> {symbol}\n<b>Zisk:</b> +{pnl_pct:.2f}% (+{pnl_usdc:.2f}$)")
                            except Exception as err:
                                print(f"❌ Chyba Trailing TP: {err}", flush=True)

                            if coin in ACTIVE_TRADES:
                                del ACTIVE_TRADES[coin]
                            CURRENT_STAGE = "STAV: 🟢 Mimo obchodu. Trailing TP úspešne uzamkol zisk!"
                            continue

                    candles_res = okx_request("GET", f"/api/v5/market/candles?instId={symbol}&bar={CURRENT_TIMEFRAME}&limit=10")
                    sell_signal_triggered = False
                    if candles_res.get("code") == "0" and candles_res.get("data"):
                        c_list = candles_res["data"]
                        c_list.reverse()
                        closes_c = [safe_float(c[4]) for c in c_list]
                        if closes_c[-1] < closes_c[-2] and pnl_pct < -0.3:
                            sell_signal_triggered = True

                    if current_price <= trade["current_sl_price"] or sell_signal_triggered:
                        exit_reason = "Stop-Loss / Exit" if current_price <= trade["current_sl_price"] else "Signál predaja"
                        try:
                            okx_request("POST", "/api/v5/trade/order", json.dumps({
                                "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                            }))
                            record_closed_trade(symbol, pnl_pct, pnl_usdc, exit_reason)
                            send_telegram_msg(f"🚨 <b>[{exit_reason.upper()}]</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}%")
                        except Exception as err:
                            print(f"❌ Chyba SL/Sell: {err}", flush=True)

                        if coin in ACTIVE_TRADES:
                            del ACTIVE_TRADES[coin]
                        CURRENT_STAGE = f"STAV: 🟢 Mimo obchodu. Pozícia zatvorená ({exit_reason})."
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
    send_telegram_msg("🟢 <b>[CYBERHUD]</b> Bot bol manuálne <b>ZAPNUTÝ</b>.")
    return JSONResponse({"status": "success", "message": "Bot bol zapnutý."})

@app.get("/api/bot/stop")
async def bot_stop():
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = False
    send_telegram_msg("🔴 <b>[CYBERHUD]</b> Bot bol manuálne <b>VYPNUTÝ</b>.")
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
            o = safe_float(c[1])
            h = safe_float(c[2])
            l = safe_float(c[3])
            cl = safe_float(c[4])
            candles.append({"time": ts, "open": o, "high": h, "low": l, "close": cl})
            highs.append(h)
            lows.append(l)

        recent_high = max(highs[-25:]) if len(highs) >= 25 else max(highs)
        recent_low = min(lows[-25:]) if len(lows) >= 25 else min(lows)
        fib_0786 = recent_high - ((recent_high - recent_low) * 0.786)

        return JSONResponse({
            "status": "success",
            "candles": candles,
            "fib0786": fib_0786
        })
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

        CURRENT_STAGE = "STAV: 🟢 Mimo obchodu (Kill Switch). Skenujem..."
        return JSONResponse({"status": "success", "message": "Pozícia predaná.", "sold": sold_summary})

HTML_TEMPLATE_CONTENT = """<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Apex Scalper v5.4.5 PRO - CyberHUD</title>
    <script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        * { box-sizing: border-box; }
        body { 
            font-family: 'Inter', 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
            margin: 0; padding: 25px;
            background: radial-gradient(circle at 50% 20%, #111827 0%, #030712 100%);
            color: #f3f4f6; min-height: 100vh;
        }
        .container { 
            max-width: 920px; margin: 0 auto; 
            background: rgba(17, 24, 39, 0.82);
            backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
            border: 1px solid rgba(0, 240, 255, 0.2); 
            border-radius: 20px; padding: 24px;
            box-shadow: 0 20px 50px rgba(0, 0, 0, 0.5);
        }
        .header-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px; border-bottom: 1px solid rgba(255,255,255,0.06); padding-bottom: 12px; }
        .brand-title { font-size: 1.05rem; font-weight: 800; color: #00f0ff; letter-spacing: 1.5px; text-transform: uppercase; }
        
        .controls-row { display: flex; justify-content: space-between; align-items: center; background: rgba(0,255,255,0.04); border: 1px solid rgba(0,255,255,0.15); padding: 12px 16px; border-radius: 12px; margin-bottom: 20px; flex-wrap: wrap; gap: 10px; }
        
        .mode-buttons a { text-decoration: none; background: rgba(0,0,0,0.4); border: 1px solid rgba(0,240,255,0.4); color: #fff; padding: 6px 12px; font-size: 0.7rem; font-weight: 600; border-radius: 8px; display: inline-block; transition: all 0.2s ease; }
        .mode-buttons a.active { background: #00f0ff; color: #000; border-color: #00f0ff; box-shadow: 0 0 15px rgba(0,240,255,0.4); font-weight: 700; }
        
        .tf-buttons { display: flex; gap: 6px; flex-wrap: wrap; }
        .tf-buttons a { text-decoration: none; background: rgba(0,0,0,0.4); border: 1px solid rgba(255,0,255,0.4); color: #fff; padding: 6px 10px; font-size: 0.7rem; font-weight: 600; border-radius: 8px; display: inline-block; transition: all 0.2s ease; }
        .tf-buttons a.active { background: #ff00ff; color: #000; border-color: #ff00ff; box-shadow: 0 0 15px rgba(255,0,255,0.4); font-weight: 700; }
        
        .bot-state-btn { padding: 6px 12px; border-radius: 8px; font-weight: 700; font-size: 0.7rem; cursor: pointer; text-transform: uppercase; border: 1px solid; transition: transform 0.1s; }
        .bot-state-btn:active { transform: scale(0.95); }
        .btn-start { background: rgba(0, 255, 136, 0.15); color: #00ff88; border-color: #00ff88; }
        .btn-stop { background: rgba(255, 68, 68, 0.15); color: #ff4444; border-color: #ff4444; }
        
        .kill-switch-btn { background: linear-gradient(135deg, #ff0055, #990033); color: #fff; border: 1px solid #ff0055; padding: 6px 12px; border-radius: 8px; font-weight: 700; font-size: 0.7rem; cursor: pointer; text-transform: uppercase; }
        
        .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 20px; }
        .stat-card { background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.06); border-radius: 12px; padding: 14px; text-align: center; transition: border-color 0.2s; }
        .stat-card:hover { border-color: rgba(0,240,255,0.3); }
        .stat-label { font-size: 0.65rem; color: #94a3b8; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px; }
        .stat-value { font-size: 1.1rem; font-weight: 800; margin-top: 6px; color: #fff; }
        
        .status-banner { background: rgba(0, 240, 255, 0.08); border: 1px solid rgba(0, 240, 255, 0.3); border-radius: 10px; padding: 12px 16px; font-size: 0.8rem; font-weight: 600; color: #e2e8f0; margin-bottom: 20px; display: flex; align-items: center; }
        
        .section-block { margin-bottom: 24px; }
        .section-title { font-size: 0.8rem; font-weight: 700; color: #00f0ff; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 1px; }
        
        .checklist-table, table { width: 100%; border-collapse: collapse; background: rgba(3, 7, 18, 0.6); border: 1px solid rgba(255, 255, 255, 0.06); border-radius: 12px; overflow: hidden; margin-bottom: 15px; }
        .checklist-table th, table th { background: rgba(255, 255, 255, 0.03); color: #00f0ff; padding: 10px 14px; font-size: 0.7rem; text-transform: uppercase; border-bottom: 1px solid rgba(255, 255, 255, 0.06); letter-spacing: 0.5px; }
        .checklist-table td, table td { padding: 10px 14px; font-size: 0.75rem; border-bottom: 1px solid rgba(255, 255, 255, 0.04); color: #e2e8f0; }

        .settings-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; background: rgba(0,0,0,0.3); padding: 16px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.06); }
        .setting-item { display: flex; flex-direction: column; font-size: 0.72rem; }
        .setting-item label { color: #94a3b8; margin-bottom: 4px; font-weight: 600; }
        .setting-item input, .setting-item select { background: rgba(0,0,0,0.4); border: 1px solid rgba(255,255,255,0.1); color: #fff; padding: 7px 10px; border-radius: 8px; font-size: 0.75rem; transition: border-color 0.2s; }
        .setting-item input:focus { border-color: #00f0ff; outline: none; }
        
        .checkbox-item { display: flex; align-items: center; gap: 10px; font-size: 0.72rem; color: #e2e8f0; font-weight: 600; margin-top: 8px; cursor: pointer; }
        
        .save-settings-btn { margin-top: 14px; background: #00f0ff; color: #000; border: none; padding: 9px 18px; font-weight: 800; border-radius: 8px; cursor: pointer; text-transform: uppercase; font-size: 0.72rem; letter-spacing: 0.5px; transition: opacity 0.2s; }
        .save-settings-btn:hover { opacity: 0.9; }
        
        #tvChartContainer { width: 100%; height: 320px; background: rgba(0,0,0,0.4); border: 1px solid rgba(255,255,255,0.06); border-radius: 12px; margin-top: 10px; }
        
        .pnl-green { color: #00ff88; font-weight: 700; }
        .pnl-red { color: #ff4444; font-weight: 700; }
        
        .refresh-info { color: rgba(255, 255, 255, 0.4); font-size: 0.68rem; margin-top: 24px; font-weight: 500; text-align: left; border-top: 1px solid rgba(255,255,255,0.06); padding-top: 12px; }
        .manual-refresh { background: rgba(0,255,255,0.08); border: 1px solid rgba(0,240,255,0.3); color: #00f0ff; padding: 5px 12px; border-radius: 8px; cursor: pointer; font-size: 0.7rem; font-weight: 700; text-decoration: none; display: inline-block; transition: background 0.2s; }
        .manual-refresh:hover { background: rgba(0,255,255,0.15); }
    </style>
</head>
<body>
    <div class="container">
        <div class="header-bar">
            <div class="brand-title">⚡ Apex Scalper v5.4.5 PRO CyberHUD</div>
            <a href="/" class="manual-refresh">🔄 Obnoviť stránku</a>
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
            <div class="stat-card">
                <div class="stat-label">Celkovo Obchodov</div>
                <div class="stat-value">{{ total_trades }}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Úspešność (Win Rate)</div>
                <div class="stat-value" style="color: #00ff88;">{{ win_rate }}%</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Kumulatívny Zisk</div>
                <div class="stat-value {{ 'pnl-green' if total_pnl_usdc >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(total_pnl_usdc) }} $</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Max Drawdown</div>
                <div class="stat-value" style="color: #ffcc00;">{{ "%.2f" | format(max_drawdown) }}%</div>
            </div>
        </div>
        
        <div class="status-banner">{{ current_stage }}</div>

        <!-- 📋 LIVE CHECKLIST PANEL -->
        <div class="section-block">
            <div class="section-title">📊 Riadiaci Panel (Checklist v5.4.5 PRO)</div>
            <table class="checklist-table">
                <thead>
                    <tr>
                        <th>AI BREAKOUT v5.4.5 PRO</th>
                        <th style="color: #ffcc00; text-align: right;">{{ '⚡ BREAKOUT' if 'Breakout' in current_mode else '🎯 FIB' }}</th>
                    </tr>
                </thead>
                <tbody>
                    {% set top_item = radar[0] if radar else None %}
                    <tr>
                        <td>1. Objemový Momentum</td>
                        <td style="text-align: right;"><strong style="color: {{ '#00ff88' if (top_item and top_item.volume_status) else '#ff4444' }};">{{ top_item.volume_text if top_item else '⚪ ČAKÁ' }}</strong></td>
                    </tr>
                    <tr>
                        <td>2. Nákupný Impulz</td>
                        <td style="text-align: right;"><strong style="color: {{ '#00ff88' if trades else ('#00ff88' if (top_item and top_item.impulse_status) else '#888888') }};">{{ '🟢 AKTÍVNY (V TRHU)' if trades else (top_item.impuls_text if top_item else '⚪ ČAKÁ') }}</strong></td>
                    </tr>
                    <tr>
                        <td>3. Stav Obchodu</td>
                        <td style="text-align: right;"><strong style="color: {{ '#00ff88' if trades else '#ff4444' }};">{{ '🟢 NAKÚPENÉ' if trades else '🔴 PREDAJ / MIMO' }}</strong></td>
                    </tr>
                    <tr>
                        <td>4. Break-Even Ochrana</td>
                        <td style="text-align: right;">
                            {% if trades %}
                                {% set active_t = trades.values() | list | first %}
                                <strong style="color: {{ '#00f0ff' if active_t.break_even_activated else '#888888' }};">{{ '🛡️ AKTÍVNY' if active_t.break_even_activated else '⏳ ČAKÁ' }}</strong>
                            {% else %}
                                <strong style="color: #888888;">💤 VYPNUTÝ</strong>
                            {% endif %}
                        </td>
                    </tr>
                    <tr>
                        <td>5. Trailing Take Profit (TTP)</td>
                        <td style="text-align: right;">
                            {% if trades %}
                                {% set active_t = trades.values() | list | first %}
                                <strong style="color: {{ '#ffcc00' if active_t.trailing_tp_active else '#00ff88' }};">{{ '🟡 TRAIL TP AKTÍVNY' if active_t.trailing_tp_active else '🟢 V TRHU (ČAKÁ)' }}</strong>
                            {% else %}
                                <strong style="color: #888888;">💤 VYPNUTÝ</strong>
                            {% endif %}
                        </td>
                    </tr>
                    <tr style="background: rgba(25, 29, 39, 0.9);">
                        <td><strong>EXEKUČNÝ POKYN</strong></td>
                        <td style="text-align: right;">
                            {% if trades %}
                                <strong style="color: #ff9900;">🔥 DRŽAŤ</strong>
                            {% elif top_item and top_item.buy_ready %}
                                <strong style="color: #00ff88;">🚀 NAKÚPIŤ</strong>
                            {% else %}
                                <strong style="color: #cccccc;">⚪ ČAKAJ</strong>
                            {% endif %}
                        </td>
                    </tr>
                </tbody>
            </table>
        </div>

        <!-- 🎛️ PANEL NASTAVENÍ A POKROČILÝCH FILTROV -->
        <div class="section-block">
            <div class="section-title">⚙️ Nastavenia stratégie & Pokročilé Filtre PRO</div>
            <form id="settingsForm" onsubmit="saveSettings(event)">
                <div class="settings-grid">
                    <div class="setting-item">
                        <label>Fast Len (Rýchla dĺžka)</label>
                        <input type="number" name="fast_len" value="{{ settings.fast_len }}">
                    </div>
                    <div class="setting-item">
                        <label>Slow Len (Pomalá dĺžka)</label>
                        <input type="number" name="slow_len" value="{{ settings.slow_len }}">
                    </div>
                    <div class="setting-item">
                        <label>Signal Len (Signálna dĺžka)</label>
                        <input type="number" name="signal_len" value="{{ settings.signal_len }}">
                    </div>
                    <div class="setting-item">
                        <label>Min. Objem (USDC)</label>
                        <input type="number" name="min_vol" value="{{ settings.min_vol }}">
                    </div>
                    <div class="setting-item">
                        <label>Min. Zmena 24h (%)</label>
                        <input type="number" step="0.1" name="min_change" value="{{ settings.min_change }}">
                    </div>
                    <div class="setting-item">
                        <label>Trailing Stop (%)</label>
                        <input type="number" step="0.1" name="trail_pct" value="{{ settings.trail_pct }}">
                    </div>
                    <div class="setting-item">
                        <label>Break-Even akt. (%)</label>
                        <input type="number" step="0.1" name="be_trigger_pct" value="{{ settings.be_trigger_pct }}">
                    </div>
                    <div class="setting-item">
                        <label>Trailing TP akt. (%)</label>
                        <input type="number" step="0.1" name="ttp_trigger_pct" value="{{ settings.ttp_trigger_pct }}">
                    </div>
                    <div class="setting-item">
                        <label>Trailing TP Callback (%)</label>
                        <input type="number" step="0.1" name="ttp_callback_pct" value="{{ settings.ttp_callback_pct }}">
                    </div>
                </div>

                <div style="margin-top: 12px; background: rgba(0,255,255,0.03); padding: 12px; border-radius: 10px; border: 1px solid rgba(0,255,255,0.1); font-size: 0.75rem;">
                    <div style="font-size: 0.72rem; color: #00f0ff; font-weight: bold; margin-bottom: 6px;">🛡️ Bezpečnostné & Inteligentné Filtre:</div>
                    <div class="checkbox-item">
                        <input type="checkbox" name="use_btc_filter" id="use_btc_filter" {{ 'checked' if settings.use_btc_filter else '' }}>
                        <label for="use_btc_filter">BTC Trend Guard (Kúp len keď BTC stúpa)</label>
                    </div>
                    <div class="checkbox-item">
                        <input type="checkbox" name="use_dynamic_sizing" id="use_dynamic_sizing" {{ 'checked' if settings.use_dynamic_sizing else '' }}>
                        <label for="use_dynamic_sizing">Dynamická alokácia (Upraviť rozpočet podľa volatility ATR)</label>
                    </div>
                    <div class="checkbox-item">
                        <input type="checkbox" name="use_weekend_filter" id="use_weekend_filter" {{ 'checked' if settings.use_weekend_filter else '' }}>
                        <label for="use_weekend_filter">Víkendový filter (Pozastaviť obchody počas víkendov)</label>
                    </div>
                </div>

                <button type="submit" class="save-settings-btn">💾 Uložiť všetky nastavenia PRO</button>
            </form>
        </div>

        <div class="section-block">
            <div class="section-title">🔴 Aktívny Obchod & Živý Sviečkový Graf (Fib 0.786)</div>
            {% if trades %}
                <table>
                    <tr><th>Pár</th><th>Vstup</th><th>Investícia</th><th>Cena</th><th>PnL (%)</th><th>PnL ($)</th></tr>
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
                <div style="font-size: 0.8rem; color: rgba(255, 255, 255, 0.8); background: rgba(0,0,0,0.4); padding: 12px; border-radius: 8px; border: 1px dashed rgba(0,255,255,0.3);">
                    ✅ Žiadny aktívny obchod. Skenujem Top 20 USDC trh na <strong>{{ current_tf }}</strong> TF...
                </div>
            {% endif %}
        </div>

        <div class="section-block">
            <div class="section-title">📊 Top 20 USDC Radar & Checklist (TF: {{ current_tf }})</div>
            {% if radar %}
                <table>
                    <tr><th>Symbol</th><th>24h Zmena</th><th>Objemový Mom.</th><th>Impulz / Fib</th><th>Pripravenosť</th></tr>
                    {% for item in radar %}
                    <tr>
                        <td><strong>{{ item.instId }}</strong></td>
                        <td class="{{ 'pnl-green' if item.change >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(item.change) }}%</td>
                        <td><span style="color: {{ '#00ff88' if item.volume_status else '#ff4444' }};">{{ '🟢' if item.volume_status else '🔴' }}</span></td>
                        <td><span style="color: {{ '#00ff88' if item.impulse_status else '#ff4444' }};">{{ '🟢' if item.impulse_status else '⚪' }}</span></td>
                        <td><strong style="color: {{ '#00ff88' if item.buy_ready else '#00f0ff' }};">{{ '🚀 NAKUPOVAT' if item.buy_ready else item.probability|string + '%' }}</strong></td>
                    </tr>
                    {% endfor %}
                </table>
            {% else %}
                <div style="font-size: 0.8rem; color: rgba(255, 255, 255, 0.7);">Načítavam Top 20 USDC mincí do radaru...</div>
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
                <div style="font-size: 0.8rem; color: rgba(255, 255, 255, 0.7);">Zatiaľ žiadne uzavreté obchody.</div>
            {% endif %}
        </div>

        <div class="section-block">
            <div class="section-title">💰 Zostatky (OKX Demo)</div>
            <table>
                <tr><th>Mena</th><th>Dostupné (availBal)</th></tr>
                {% for coin, data in balances.items() %}
                <tr><td><strong>{{ coin }}</strong></td><td>{{ "%.4f" | format(data.available_balance | float) }}</td></tr>
                {% endfor %}
            </table>
        </div>

        <div class="refresh-info">Apex Scalper v5.4.5 PRO | Stav bota: {{ '🟢 ZAPNUTÝ' if is_bot_active else '🔴 VYPNUTÝ' }} | Režim: {{ current_mode }} | TF: {{ current_tf }} | Čas: {{ current_time }}</div>
    </div>

    <script>
        async function toggleBot(endpoint) {
            const res = await fetch(endpoint);
            const data = await res.json();
            alert(data.message || "Stav zmenený!");
            window.location.reload();
        }

        async function triggerKillSwitch() {
            if (confirm("⚠️ Naozaj chceš okamžite predať aktívnu pozíciu?")) {
                const res = await fetch('/api/emergency-sell', { method: 'POST' });
                const data = await res.json();
                alert(data.message || "Pozícia uzavretá!");
                window.location.reload();
            }
        }

        async function saveSettings(event) {
            event.preventDefault();
            const form = event.target;
            const data = {
                fast_len: parseInt(form.fast_len.value),
                slow_len: parseInt(form.slow_len.value),
                signal_len: parseInt(form.signal_len.value),
                min_vol: form.min_vol.value,
                min_change: form.min_change.value,
                trail_pct: form.trail_pct.value,
                be_trigger_pct: form.be_trigger_pct.value,
                ttp_trigger_pct: form.ttp_trigger_pct.value,
                ttp_callback_pct: form.ttp_callback_pct.value,
                use_btc_filter: form.use_btc_filter.checked,
                use_dynamic_sizing: form.use_dynamic_sizing.checked,
                use_weekend_filter: form.use_weekend_filter.checked
            };

            const res = await fetch('/api/update-settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });
            const result = await res.json();
            alert(result.message || "Uložené!");
            window.location.reload();
        }

        async function initLiveChart() {
            const row = document.getElementById('activeTradeRow');
            const container = document.getElementById('tvChartContainer');
            if (!row || !container) return;

            const symbol = row.getAttribute('data-symbol');
            if (!symbol) return;

            try {
                const res = await fetch(`/api/live-candles/${symbol}`);
                const data = await res.json();
                if (data.status !== 'success' || !data.candles || data.candles.length === 0) return;

                const chart = LightweightCharts.createChart(container, {
                    width: container.clientWidth,
                    height: 320,
                    layout: {
                        background: { type: 'solid', color: 'transparent' },
                        textColor: '#00f0ff',
                    },
                    grid: {
                        vertLines: { color: 'rgba(255, 255, 255, 0.04)' },
                        horzLines: { color: 'rgba(255, 255, 255, 0.04)' },
                    },
                    timeScale: {
                        timeVisible: true,
                        secondsVisible: false,
                        borderColor: 'rgba(0, 240, 255, 0.2)'
                    }
                });

                const candlestickSeries = chart.addCandlestickSeries({
                    upColor: '#00ff88',
                    downColor: '#ff4444',
                    borderVisible: false,
                    wickUpColor: '#00ff88',
                    wickDownColor: '#ff4444',
                });

                candlestickSeries.setData(data.candles);

                if (data.fib0786) {
                    candlestickSeries.createPriceLine({
                        price: data.fib0786,
                        color: '#ffcc00',
                        lineWidth: 2,
                        lineStyle: LightweightCharts.LineStyle.Solid,
                        axisLabelVisible: true,
                        title: 'Fib 0.786',
                    });
                }

                chart.timeScale().fitContent();

                window.addEventListener('resize', () => {
                    chart.applyOptions({ width: container.clientWidth });
                });
            } catch (err) {
                console.error("Chyba načítania živého grafu:", err);
            }
        }

        window.addEventListener('DOMContentLoaded', () => {
            initLiveChart();
        });
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    account_balances = {curr: {"available_balance": 0.0} for curr in ALLOWED_CURRENCIES}
    try:
        balance_res = okx_request("GET", "/api/v5/account/balance")
        if balance_res.get("code") == "0":
            found = {}
            for acc in balance_res.get("data", []):
                for detail in acc.get("details", []):
                    ccy = detail.get("ccy")
                    if ccy in ALLOWED_CURRENCIES:
                        found[ccy] = {"available_balance": safe_float(detail.get("availBal", 0))}
            if found:
                for curr in ALLOWED_CURRENCIES:
                    if curr in found:
                        account_balances[curr] = found[curr]
    except Exception:
        pass

    total_trades = len(CLOSED_TRADES_HISTORY)
    winning_trades = sum(1 for t in CLOSED_TRADES_HISTORY if t["pnl_pct"] > 0)
    win_rate = round((winning_trades / total_trades * 100) if total_trades > 0 else 0.0, 1)
    total_pnl_usdc = sum(t["pnl_usdc"] for t in CLOSED_TRADES_HISTORY) if "CLOSED_TRADES_HISTORY" in globals() else 0.0

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
        max_drawdown=MAX_DRAWDOWN_PCT,
        current_mode=CURRENT_STRATEGY_MODE,
        current_tf=CURRENT_TIMEFRAME,
        current_stage=CURRENT_STAGE,
        price_history=PRICE_HISTORY,
        is_bot_active=IS_BOT_ACTIVE,
        settings=SETTINGS,
        current_time=get_sk_time_str()
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
