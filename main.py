import asyncio
import base64
import hmac
import json
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Template
import requests
import uvicorn

# --- OKX KLUCE & BEZPECNOST ---

OKX_API_KEY = os.getenv("OKX_API_KEY", "54ebe9fe-1fd0-443d-8f76-0cd1a3439ddb")
OKX_SECRET = os.getenv("OKX_SECRET", "FBD5C8332D88E3ED5662470CA9C3E813")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "Novyzeland111/")
WEBHOOK_SECRET = "moje_tajne_heslo_123"

# --- TELEGRAM NOTIFIKACIE & INTERAKTIVNY BOT ---

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9021881475:AAFn1oQSvaGkhpX2uyJscdcTybFgk1tNmV8")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "9130979784")

# --- DYNAMICKE PROFILY PRE CASOVE RAMCE ---

TIMEFRAME_PROFILES = {
    "1m": {"trail_pct": 0.5, "be_trigger_pct": 0.3, "ttp_trigger_pct": 0.6, "ttp_callback_pct": 0.15, "target_pct": 0.5, "desc": "Skalper 1m"},
    "5m": {"trail_pct": 0.0, "be_trigger_pct": 0.0, "ttp_trigger_pct": 0.0, "ttp_callback_pct": 0.0, "target_pct": 0.0, "desc": "Manuál 5m"},
    "8m": {"trail_pct": 0.0, "be_trigger_pct": 0.0, "ttp_trigger_pct": 0.0, "ttp_callback_pct": 0.0, "target_pct": 0.0, "desc": "Manuál 8m"},
    "15m": {"trail_pct": 0.7, "be_trigger_pct": 0.5, "ttp_trigger_pct": 0.8, "ttp_callback_pct": 0.2, "target_pct": 0.75, "desc": "Rýchly 15m scalp (+0.75%)"},
    "30m": {"trail_pct": 1.0, "be_trigger_pct": 0.9, "ttp_trigger_pct": 1.2, "ttp_callback_pct": 0.3, "target_pct": 1.2, "desc": "30m trendový"},
    "45m": {"trail_pct": 1.5, "be_trigger_pct": 1.2, "ttp_trigger_pct": 1.8, "ttp_callback_pct": 0.4, "target_pct": 1.5, "desc": "45m strednodobý"},
    "1h": {"trail_pct": 2.0, "be_trigger_pct": 1.6, "ttp_trigger_pct": 2.5, "ttp_callback_pct": 0.5, "target_pct": 1.7, "desc": "1h swing"},
    "2h": {"trail_pct": 3.0, "be_trigger_pct": 2.2, "ttp_trigger_pct": 3.5, "ttp_callback_pct": 0.7, "target_pct": 2.0, "desc": "2h trend"},
    "3h": {"trail_pct": 4.0, "be_trigger_pct": 3.0, "ttp_trigger_pct": 5.0, "ttp_callback_pct": 1.0, "target_pct": 3.0, "desc": "3h pozičný"},
    "4h": {"trail_pct": 5.5, "be_trigger_pct": 4.0, "ttp_trigger_pct": 7.0, "ttp_callback_pct": 1.5, "target_pct": 3.5, "desc": "4h swing"}
}

ACTIVE_TRADES = {}
DAILY_PNL_CALENDAR = []
CLOSED_TRADES_HISTORY = []
SYSTEM_LOGS = []
CURRENT_STAGE = "STAV: Mimo obchodu. Čakám na autonómny signál..."
LAST_TRADE_TIME = {}
trade_lock = asyncio.Lock()

IS_BOT_ACTIVE = True
CURRENT_TIMEFRAME = "15M"
STORAGE_FILE = "trades_history.json"
ALLOWED_STABLES = ["USDC"]

# Top likvidné symboly pre skenovanie (s 24h objemovým filtrom)
TOP_LIQUID_SYMBOLS = ["SOL-USDC", "SUI-USDC", "XRP-USDC", "ADA-USDC", "AVAX-USDC", "LINK-USDC", "DOGE-USDC", "PEPE-USDC", "NEAR-USDC", "RUNE-USDC", "RENDER-USDC"]

SETTINGS = TIMEFRAME_PROFILES["15m"].copy()
SETTINGS.update({
    "use_btc_filter": True,
    "reentry_cooldown_sec": 120,
    "use_breakeven": True,
    "max_budget_cap": 1000.0,
    "compounding_enabled": False,
    "max_parallel": 1
})

def log_event(message: str):
    timestamp = datetime.now(timezone.utc) + timedelta(hours=1)
    time_str = timestamp.strftime("%H:%M:%S")
    entry = f"[{time_str}] {message}"
    SYSTEM_LOGS.insert(0, entry)
    if len(SYSTEM_LOGS) > 80:
        SYSTEM_LOGS.pop()
    print(entry, flush=True)

def send_telegram_msg(message: str, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("DOPLŇ"):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        requests.post(url, json=payload, timeout=5)
    except Exception as err:
        print(f"Chyba pri odosielaní Telegram notifikácie: {err}", flush=True)

def load_persistent_data():
    global CLOSED_TRADES_HISTORY, DAILY_PNL_CALENDAR
    if os.path.exists(STORAGE_FILE):
        try:
            with open(STORAGE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                CLOSED_TRADES_HISTORY = data.get("history", [])
                DAILY_PNL_CALENDAR = data.get("calendar", [])
                log_event(f"Načítaných {len(CLOSED_TRADES_HISTORY)} histórie obchodov z disku.")
        except Exception as e:
            log_event(f"Chyba pri načítaní úložiska: {e}")

def save_persistent_data():
    try:
        cutoff_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        filtered_history = [h for h in CLOSED_TRADES_HISTORY if h.get("time", "") >= cutoff_date]
        data = {"history": filtered_history, "calendar": DAILY_PNL_CALENDAR}
        with open(STORAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_event(f"Chyba pri ukladaní údajov na disk: {e}")

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

def calculate_stats():
    total = len(CLOSED_TRADES_HISTORY)
    if total == 0:
        return {"win_rate": 0.0, "total_trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
    wins = sum(1 for h in CLOSED_TRADES_HISTORY if h.get("pnl_usdc", 0) > 0)
    losses = sum(1 for h in CLOSED_TRADES_HISTORY if h.get("pnl_usdc", 0) <= 0)
    win_rate = (wins / total) * 100.0
    total_pnl = sum(h.get("pnl_usdc", 0) for h in CLOSED_TRADES_HISTORY)
    return {"win_rate": round(win_rate, 1), "total_trades": total, "wins": wins, "losses": losses, "total_pnl": round(total_pnl, 2)}

def record_closed_trade(pair, pnl_pct, pnl_usdc, reason):
    global CLOSED_TRADES_HISTORY, DAILY_PNL_CALENDAR
    day_str = get_sk_date_str()
    day_found = False
    for item in DAILY_PNL_CALENDAR:
        if item["date"] == day_str:
            item["pnl"] += pnl_usdc
            item["trades"] += 1
            day_found = True
            break
    if not day_found:
        DAILY_PNL_CALENDAR.insert(0, {"date": day_str, "pnl": pnl_usdc, "trades": 1})
    CLOSED_TRADES_HISTORY.insert(0, {"pair": pair, "pnl_pct": round(pnl_pct, 2), "pnl_usdc": round(pnl_usdc, 2), "reason": reason, "time": get_sk_time_str()})
    save_persistent_data()

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

def get_instrument_rules(symbol: str):
    try:
        res = okx_request("GET", f"/api/v5/public/instruments?instType=SPOT&instId={symbol}")
        if res.get("code") == "0" and res.get("data"):
            inst = res["data"][0]
            lot_sz = safe_float(inst.get("lotSz"), 0.01)
            min_sz = safe_float(inst.get("minSz"), 0.01)
            return lot_sz, min_sz
    except Exception as e:
        log_event(f"Chyba pri načítaní pravidiel pre {symbol}: {e}")
    return 0.01, 0.01

def format_amount(amount: float, lot_sz: float, min_sz: float) -> str:
    if amount < min_sz:
        return ""
    precision = 0
    if lot_sz < 1:
        precision = len(str(lot_sz).split('.')[1]) if '.' in str(lot_sz) else 0
    steps = math.floor(amount / lot_sz)
    valid_amount = round(steps * lot_sz, precision)
    if valid_amount < min_sz:
        return ""
    return f"{valid_amount:.{precision}f}"

def fetch_account_capital(stable_ccy="USDC") -> float:
    try:
        balance_res = okx_request("GET", "/api/v5/account/balance")
        if balance_res.get("code") == "0":
            for acc in balance_res.get("data", []):
                for detail in acc.get("details", []):
                    if detail.get("ccy") == stable_ccy:
                        avail = safe_float(detail.get("availBal", 0))
                        cash = safe_float(detail.get("cashBal", 0))
                        eq = safe_float(detail.get("eq", 0))
                        best_val = max(avail, cash, eq)
                        if best_val > 0:
                            return best_val
    except Exception as err:
        log_event(f"Chyba pri načítaní zostatku: {err}")
    return 13000.0

def check_btc_health() -> bool:
    if not SETTINGS["use_btc_filter"]:
        return True
    try:
        url = "/api/v5/market/candles?instId=BTC-USDC&bar=15m&limit=10"
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

def update_top_liquid_symbols():
    global TOP_LIQUID_SYMBOLS
    try:
        res = okx_request("GET", "/api/v5/market/tickers?instType=SPOT")
        if res.get("code") == "0" and res.get("data"):
            tickers = res["data"]
            valid = []
            for t in tickers:
                inst_id = t.get("instId", "")
                if not inst_id.endswith("-USDC"):
                    continue
                coin = inst_id.split("-")[0]
                if coin in ["BTC", "ETH"]:
                    continue
                vol = safe_float(t.get("volCcy24h", 0))
                if vol >= 500000.0:
                    valid.append((inst_id, vol))
            valid.sort(key=lambda x: x[1], reverse=True)
            top_20 = [item[0] for item in valid[:20]]
            if top_20:
                TOP_LIQUID_SYMBOLS = top_20
    except Exception as e:
        log_event(f"Chyba pri aktualizácii top symbolov: {e}")

# --- PINE SCRIPT LOGIKA (ALL GREEN) V PYTHONE ---

def calc_ema(values, length):
    if len(values) < length:
        return [values[-1]] * len(values)
    alpha = 2 / (length + 1)
    ema = [sum(values[:length]) / length]
    for price in values[length:]:
        ema.append(price * alpha + ema[-1] * (1 - alpha))
    return ema

def evaluate_pine_strategy(symbol: str, bar_tf: str = "15m") -> bool:
    try:
        url = f"/api/v5/market/candles?instId={symbol}&bar={bar_tf}&limit=50"
        res = okx_request("GET", url)
        if res.get("code") != "0" or not res.get("data"):
            return False

        candles = res["data"]
        candles.reverse()
        
        ha_closes, ha_opens, volumes = [], [], []
        for i, c in enumerate(candles):
            o, h, l, cl, v = safe_float(c[1]), safe_float(c[2]), safe_float(c[3]), safe_float(c[4]), safe_float(c[5])
            ha_cl = (o + h + l + cl) / 4.0
            ha_op = (o + cl) / 2.0 if i == 0 else (ha_opens[-1] + ha_closes[-1]) / 2.0
            ha_closes.append(ha_cl)
            ha_opens.append(ha_op)
            volumes.append(v)
            
        vol_dirs = [v if ha_closes[i] > ha_opens[i] else -v for i, v in enumerate(volumes)]
        fast_ma = calc_ema(vol_dirs, 5)
        slow_ma = calc_ema(vol_dirs, 20)
        
        oscillator = [f - s for f, s in zip(fast_ma, slow_ma)]
        signal_line = calc_ema(oscillator, 9)
        close_ema_9 = calc_ema(ha_closes, 9)
        
        curr_osc, prev_osc = oscillator[-1], oscillator[-2]
        curr_sig, prev_sig = signal_line[-1], signal_line[-2]
        
        cond_volume_bullish = curr_osc > 0 and curr_osc > curr_sig
        cond_volume_crossover = (prev_osc <= prev_sig) and (curr_osc > curr_sig) and (curr_osc > 0)
        
        curr_close, prev_close = ha_closes[-1], ha_closes[-2]
        curr_ema9, prev_ema9 = close_ema_9[-1], close_ema_9[-2]
        close_crossover_ema9 = (prev_close <= prev_ema9) and (curr_close > curr_ema9)
        
        all_green_condition = cond_volume_bullish and (cond_volume_crossover or close_crossover_ema9)
        return all_green_condition
    except Exception:
        return False

# --- AUTONOMNA SKENOVACIA SLUCKA ---

async def autonomous_scanner_loop():
    await asyncio.sleep(5)
    log_event("Autonómny Pine Script skener (All Green) úspešne spustený!")
    while True:
        try:
            max_allowed = SETTINGS.get("max_parallel", 1)
            if IS_BOT_ACTIVE and len(ACTIVE_TRADES) < max_allowed:
                if check_btc_health():
                    tf_str = CURRENT_TIMEFRAME.lower()
                    for symbol in TOP_LIQUID_SYMBOLS:
                        coin_name = symbol.split("-")[0]
                        now_ts = datetime.now().timestamp()

                        if coin_name in ACTIVE_TRADES or (coin_name in LAST_TRADE_TIME and (now_ts - LAST_TRADE_TIME[coin_name] < SETTINGS["reentry_cooldown_sec"])):
                            continue
                            
                        signal_met = evaluate_pine_strategy(symbol, bar_tf=tf_str)
                        if signal_met:
                            log_event(f"PINE SCRIPT 'ALL GREEN' SIGNÁL DETEGOVANÝ PRE {symbol}!")
                            await execute_autonomous_buy(symbol)
                            break
        except Exception as e:
            log_event(f"Chyba v autonómnom skeneri: {e}")
        await asyncio.sleep(30)

async def top_symbols_updater_loop():
    while True:
        try:
            update_top_liquid_symbols()
        except Exception:
            pass
        await asyncio.sleep(3600)

async def execute_autonomous_buy(okx_symbol: str):
    global ACTIVE_TRADES, LAST_TRADE_TIME
    async with trade_lock:
        coin_name = okx_symbol.split("-")[0]
        now_ts = datetime.now().timestamp()
        max_allowed = SETTINGS.get("max_parallel", 1)

        if coin_name in ACTIVE_TRADES or len(ACTIVE_TRADES) >= max_allowed:
            return
            
        LAST_TRADE_TIME[coin_name] = now_ts
        stable_ccy = "USDC"
        
        free_capital = fetch_account_capital(stable_ccy)
        budget_cap = SETTINGS.get("max_budget_cap", 1000.0)
        
        allocated_capital_base = free_capital / max_allowed
        
        if SETTINGS.get("compounding_enabled", False):
            safe_budget = min(allocated_capital_base * 0.98, allocated_capital_base)
            log_event(f"Compounding AKTÍVNY (Alokácia pre {max_allowed} pozície, 98% vybitia): {safe_budget:.2f} USDC")
        else:
            safe_budget = min(allocated_capital_base * 0.98, budget_cap)
            log_event(f"Compounding VYPNUTÝ (Alokácia pre {max_allowed} pozície, strop {budget_cap} USDC): {safe_budget:.2f} USDC")

        if safe_budget < 5.0:
            log_event(f"Nedostatok USDC prostriedkov: {free_capital}")
            return
            
        ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={okx_symbol}")
        if ticker_res.get("code") != "0" or not ticker_res.get("data"):
            log_event(f"Nepodarilo sa získať cenu pre {okx_symbol}")
            return
            
        current_price = safe_float(ticker_res["data"][0]["last"])
        raw_amount = safe_budget / current_price
        
        lot_sz, min_sz = get_instrument_rules(okx_symbol)
        formatted_sz = format_amount(raw_amount, lot_sz, min_sz)
        
        if not formatted_sz:
            log_event(f"Množstvo pre {okx_symbol} nedosahuje minimum burzy minSz ({min_sz})")
            return
            
        order_body = json.dumps({
            "instId": okx_symbol, "tdMode": "cash", "side": "buy",
            "ordType": "market", "sz": formatted_sz
        })
        
        order_res = okx_request("POST", "/api/v5/trade/order", order_body)
        if order_res.get("code") != "0":
            err_msg = order_res.get('msg', 'Neznáma chyba OKX')
            log_event(f"Chyba nákupnej objednávky ({okx_symbol}): {err_msg}")
            send_telegram_msg(f"<b>Chyba nákupu ({okx_symbol}):</b> {err_msg}")
            return
            
        coin_amount = float(formatted_sz)
        actual_spent_usdc = coin_amount * current_price
        sl_price = current_price * (1 - 0.006)
        be_price = current_price * (1 + (SETTINGS["be_trigger_pct"] / 100.0)) if SETTINGS.get("be_trigger_pct", 0) > 0 else 0
        ttp_price = current_price * (1 + (SETTINGS["ttp_trigger_pct"] / 100.0)) if SETTINGS.get("ttp_trigger_pct", 0) > 0 else 0
        target_pct = SETTINGS.get("target_pct", 0.75)
        
        ACTIVE_TRADES[coin_name] = {
            "entry_price": current_price,
            "current_price": current_price,
            "current_sl_price": sl_price,
            "be_target_price": be_price,
            "ttp_target_price": ttp_price,
            "amount": coin_amount,
            "initial_amount": coin_amount,
            "lot_sz": lot_sz,
            "min_sz": min_sz,
            "bought_at": get_sk_time_str(),
            "market_pair": okx_symbol,
            "highest_price": current_price,
            "break_even_activated": False,
            "ttp_activated": False,
            "invested_usdc": actual_spent_usdc,
            "live_pnl_pct": 0.0,
            "live_pnl_usdc": 0.0,
            "tf": CURRENT_TIMEFRAME,
            "settings": SETTINGS.copy()
        }
        
        target_str = f"+{target_pct}%"
        log_event(f"Autonómny nákup [{CURRENT_TIMEFRAME}]: {okx_symbol} za {current_price:.4f} USDC | Investícia: {actual_spent_usdc:.2f} USDC")
        
        inline_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "ZATVORIŤ VŠETKO", "callback_data": "sell_now"},
                    {"text": "PAUZA BOTA", "callback_data": "toggle_bot"}
                ]
            ]
        }
        send_telegram_msg(
            f"<b>AUTONÓMNY NÁKUP [TF: {CURRENT_TIMEFRAME}]</b>\n"
            f"<b>Pár:</b> {okx_symbol}\n"
            f"<b>Vstup:</b> {current_price:.4f}\n"
            f"<b>Investícia:</b> {actual_spent_usdc:.2f} USDC\n"
            f"<b>Cieľ (TP):</b> {target_str}",
            reply_markup=inline_keyboard
        )

# --- MONITOR RIZIK (100% PREDAJ NARAZ) ---

async def advanced_risk_monitor_loop():
    global ACTIVE_TRADES, CURRENT_STAGE
    await asyncio.sleep(3)
    while True:
        if not ACTIVE_TRADES:
            if IS_BOT_ACTIVE:
                max_p = SETTINGS.get("max_parallel", 1)
                CURRENT_STAGE = f"STAV: Autonómny Bot aktívny (Max {max_p} paralelné). Skenujem Top {len(TOP_LIQUID_SYMBOLS)} altcoinov cez Pine Script..."
            else:
                CURRENT_STAGE = "STAV: Bot pozastavený."
            await asyncio.sleep(2)
            continue
        try:
            now_time = datetime.now(timezone.utc) + timedelta(hours=1)
            for coin in list(ACTIVE_TRADES.keys()):
                if coin not in ACTIVE_TRADES:
                    continue
                trade = ACTIVE_TRADES[coin]
                symbol = trade["market_pair"]
                trade_settings = trade.get("settings", SETTINGS)

                ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={symbol}")
                if ticker_res.get("code") == "0" and ticker_res.get("data"):
                    current_price = safe_float(ticker_res["data"][0]["last"])
                else:
                    current_price = trade["entry_price"]

                trade["current_price"] = current_price

                bought_time = datetime.strptime(trade["bought_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=1)))
                if now_time - bought_time > timedelta(hours=48):
                    try:
                        fmt_sz = format_amount(trade["amount"], trade.get("lot_sz", 0.01), trade.get("min_sz", 0.01))
                        if fmt_sz:
                            order_body = json.dumps({
                                "instId": symbol, "tdMode": "cash", "side": "sell",
                                "ordType": "market", "sz": fmt_sz
                            })
                            okx_request("POST", "/api/v5/trade/order", order_body)
                        pnl_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
                        invested = trade.get("invested_usdc", trade["amount"] * trade["entry_price"])
                        pnl_usdc = invested * (pnl_pct / 100.0)
                        record_closed_trade(symbol, pnl_pct, pnl_usdc, "GTC Expirácia 48h")
                        log_event(f"Expirácia 48h pre {symbol} | PnL: {pnl_pct:+.2f}%")
                        send_telegram_msg(f"<b>GTC EXPIRÁCIA 48H</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}%")
                    except Exception:
                        pass
                    del ACTIVE_TRADES[coin]
                    continue

                if current_price > 0:
                    if current_price > trade["highest_price"]:
                        trade["highest_price"] = current_price
                    
                    pnl_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
                    invested = trade.get("invested_usdc", trade["amount"] * trade["entry_price"])
                    pnl_usdc = invested * (pnl_pct / 100.0)
                    
                    target_pct = trade_settings.get("target_pct", 0.75)
                    
                    if target_pct > 0 and pnl_pct >= target_pct:
                        reason = f"Take Profit 100% (+{target_pct}%)"
                        try:
                            fmt_sz = format_amount(trade["amount"], trade.get("lot_sz", 0.01), trade.get("min_sz", 0.01))
                            if fmt_sz:
                                order_body = json.dumps({
                                    "instId": symbol, "tdMode": "cash", "side": "sell",
                                    "ordType": "market", "sz": fmt_sz
                                })
                                okx_request("POST", "/api/v5/trade/order", order_body)
                            record_closed_trade(symbol, pnl_pct, pnl_usdc, reason)
                            log_event(f"Uzavretý 100% Take Profit pre {symbol} | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                            send_telegram_msg(f"<b>UZAVRETÝ 100% TAKE PROFIT</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                        except Exception:
                            pass
                        del ACTIVE_TRADES[coin]
                        continue

                    be_trigger = trade_settings.get("be_trigger_pct", 0.5)
                    if be_trigger > 0:
                        be_price = trade["entry_price"] * (1 + (be_trigger / 100.0))
                        trail_stop_price = trade["highest_price"] * (1 - (trade_settings["trail_pct"] / 100.0))
                        
                        if pnl_pct >= be_trigger:
                            trade["break_even_activated"] = True
                            effective_sl = max(trail_stop_price, trade["entry_price"])
                        else:
                            effective_sl = trade["entry_price"] * (1 - 0.006)
                            
                        ttp_trigger = trade_settings.get("ttp_trigger_pct", 0.0)
                        if ttp_trigger > 0:
                            ttp_activation_price = trade["entry_price"] * (1 + (ttp_trigger / 100.0))
                            if current_price >= ttp_activation_price:
                                trade["ttp_activated"] = True
                            ttp_stop_level = trade["highest_price"] * (1 - (trade_settings["ttp_callback_pct"] / 100.0)) if trade.get("ttp_activated") else ttp_activation_price
                        else:
                            ttp_stop_level = 0
                        
                        trade["live_pnl_pct"] = pnl_pct
                        trade["live_pnl_usdc"] = pnl_usdc
                        CURRENT_STAGE = f"STAV: Aktívnych pozícií: {len(ACTIVE_TRADES)} | Posledná: {symbol} ({pnl_pct:+.2f}%)"
                        
                        if current_price <= effective_sl:
                            reason = "Break-Even Ochrana" if effective_sl >= trade["entry_price"] else "Stop-Loss (-0.6%)"
                            try:
                                fmt_sz = format_amount(trade["amount"], trade.get("lot_sz", 0.01), trade.get("min_sz", 0.01))
                                if fmt_sz:
                                    order_body = json.dumps({
                                        "instId": symbol, "tdMode": "cash", "side": "sell",
                                        "ordType": "market", "sz": fmt_sz
                                    })
                                    okx_request("POST", "/api/v5/trade/order", order_body)
                                record_closed_trade(symbol, pnl_pct, pnl_usdc, reason)
                                log_event(f"Uzavretá 100% pozícia ({reason}) pre {symbol} | PnL: {pnl_pct:+.2f}%")
                                send_telegram_msg(f"<b>UZAVRETÁ 100% POZÍCIA ({reason})</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}%")
                            except Exception:
                                pass
                            del ACTIVE_TRADES[coin]
                            continue
                            
                        if trade.get("ttp_activated") and current_price <= ttp_stop_level:
                            reason = "Trailing Take Profit"
                            try:
                                fmt_sz = format_amount(trade["amount"], trade.get("lot_sz", 0.01), trade.get("min_sz", 0.01))
                                if fmt_sz:
                                    order_body = json.dumps({
                                        "instId": symbol, "tdMode": "cash", "side": "sell",
                                        "ordType": "market", "sz": fmt_sz
                                    })
                                    okx_request("POST", "/api/v5/trade/order", order_body)
                                record_closed_trade(symbol, pnl_pct, pnl_usdc, reason)
                                log_event(f"Uzavretá 100% pozícia (Trailing TP) pre {symbol} | PnL: {pnl_pct:+.2f}%")
                                send_telegram_msg(f"<b>UZAVRETÁ 100% POZÍCIA (TTP)</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}%")
                            except Exception:
                                pass
                            del ACTIVE_TRADES[coin]
                            continue
        except Exception:
            pass
        await asyncio.sleep(2)

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_persistent_data()
    update_top_liquid_symbols()
    log_event("Autonómny Apex Scalper PRO v5.6.3 spustený.")
    monitor_task = asyncio.create_task(advanced_risk_monitor_loop())
    scanner_task = asyncio.create_task(autonomous_scanner_loop())
    updater_task = asyncio.create_task(top_symbols_updater_loop())
    send_telegram_msg("Autonómny Apex Scalper PRO v5.6.3 plne pripravený a beží!")
    yield
    monitor_task.cancel()
    scanner_task.cancel()
    updater_task.cancel()

app = FastAPI(lifespan=lifespan)

@app.post("/api/telegram-webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        if "callback_query" in data:
            cb = data["callback_query"]
            cb_data = cb.get("data", "")
            if cb_data == "sell_now":
                await sell_now()
                send_telegram_msg("Ručný predaj aktivovaný z Telegramu!")
            elif cb_data == "toggle_bot":
                await toggle_bot()
    except Exception:
        pass
    return {"status": "ok"}

@app.post("/api/toggle-bot")
async def toggle_bot():
    global IS_BOT_ACTIVE, CURRENT_STAGE
    IS_BOT_ACTIVE = not IS_BOT_ACTIVE
    status_str = "aktívny" if IS_BOT_ACTIVE else "pozastavený"
    CURRENT_STAGE = f"STAV: Bot je {status_str}."
    log_event(f"Prepínač bota: {status_str.upper()}")
    send_telegram_msg(f"Zmena stavu: Bot je teraz {status_str.upper()}.")
    return JSONResponse({"status": "success", "is_bot_active": IS_BOT_ACTIVE})

@app.post("/api/toggle-btc-filter")
async def toggle_btc_filter():
    SETTINGS["use_btc_filter"] = not SETTINGS.get("use_btc_filter", True)
    status_str = "zapnutý" if SETTINGS["use_btc_filter"] else "vypnutý"
    log_event(f"BTC Trend Guard filter: {status_str.upper()}")
    send_telegram_msg(f"Zmena stavu: BTC Trend Guard filter je teraz {status_str.upper()}.")
    return JSONResponse({"status": "success", "use_btc_filter": SETTINGS["use_btc_filter"]})

@app.post("/api/toggle-compounding")
async def toggle_compounding():
    SETTINGS["compounding_enabled"] = not SETTINGS.get("compounding_enabled", False)
    status_str = "ZAPNUTÉ" if SETTINGS["compounding_enabled"] else "VYPNUTÉ"
    log_event(f"Automatické reinvestovanie (COMPOUNDING): {status_str}")
    send_telegram_msg(f"Zmena stavu: Auto-Compounding je teraz {status_str}.")
    return JSONResponse({"status": "success", "compounding_enabled": SETTINGS["compounding_enabled"]})

@app.post("/api/set-budget/{amount}")
async def set_budget(amount: int):
    if amount in [500, 1000, 3000, 5000, 10000]:
        SETTINGS["max_budget_cap"] = float(amount)
        log_event(f"Maximálny rozpočet zmenený na: {amount} USDC")
        send_telegram_msg(f"Zmena rozpočtu: Maximálny strop je teraz {amount} USDC.")
        return JSONResponse({"status": "success", "max_budget_cap": amount})
    return JSONResponse({"status": "error", "message": "Neplatná hodnota rozpočtu."})

@app.post("/api/set-parallel/{count}")
async def set_parallel(count: int):
    if count in [1, 2, 3, 4]:
        SETTINGS["max_parallel"] = count
        log_event(f"Maximálny počet paralelných obchodov zmenený na: {count}")
        send_telegram_msg(f"Zmena nastavenia: Max. {count} paralelných obchodov.")
        return JSONResponse({"status": "success", "max_parallel": count})
    return JSONResponse({"status": "error", "message": "Neplatná hodnota."})

@app.post("/api/set-tf/{tf_val}")
async def set_tf(tf_val: str):
    global CURRENT_TIMEFRAME, SETTINGS
    tf_lower = tf_val.lower()
    if tf_lower in TIMEFRAME_PROFILES:
        CURRENT_TIMEFRAME = tf_val.upper()
        SETTINGS.update(TIMEFRAME_PROFILES[tf_lower])
        log_event(f"Časový rámec (TF) zmenený na: {CURRENT_TIMEFRAME}")
        return JSONResponse({"status": "success", "tf": CURRENT_TIMEFRAME})
    return JSONResponse({"status": "error", "message": "Neplatný časový rámec."})

@app.post("/api/manual-buy/{coin}")
async def manual_buy(coin: str):
    global ACTIVE_TRADES, LAST_TRADE_TIME
    async with trade_lock:
        try:
            if not IS_BOT_ACTIVE:
                return JSONResponse({"status": "error", "message": "Bot je pozastavený."})

            coin_upper = coin.upper().strip()
            max_allowed = SETTINGS.get("max_parallel", 1)
            
            if len(ACTIVE_TRADES) >= max_allowed or coin_upper in ACTIVE_TRADES:
                return JSONResponse({"status": "error", "message": "Dosiahnutý limit paralelných pozícií alebo minca už je aktívna."})
            
            okx_symbol = f"{coin_upper}-USDC"
            stable_ccy = "USDC"
            free_capital = fetch_account_capital(stable_ccy)
            budget_cap = SETTINGS.get("max_budget_cap", 1000.0)
            
            allocated_capital_base = free_capital / max_allowed
            
            if SETTINGS.get("compounding_enabled", False):
                safe_budget = min(allocated_capital_base * 0.98, allocated_capital_base)
            else:
                safe_budget = min(allocated_capital_base * 0.98, budget_cap)

            if safe_budget < 5.0:
                return JSONResponse({"status": "error", "message": "Nedostatok prostriedkov."})
            
            ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={okx_symbol}")
            if ticker_res.get("code") != "0" or not ticker_res.get("data"):
                return JSONResponse({"status": "error", "message": f"Nepodarilo sa získať cenu pre {okx_symbol}"})
            
            current_price = safe_float(ticker_res["data"][0]["last"])
            raw_amount = safe_budget / current_price
            lot_sz, min_sz = get_instrument_rules(okx_symbol)
            formatted_sz = format_amount(raw_amount, lot_sz, min_sz)
            
            if not formatted_sz:
                return JSONResponse({"status": "error", "message": f"Nedosahuje minimálnu veľkosť pre OKX ({min_sz})"})
            
            order_body = json.dumps({
                "instId": okx_symbol, "tdMode": "cash", "side": "buy",
                "ordType": "market", "sz": formatted_sz
            })
            
            order_res = okx_request("POST", "/api/v5/trade/order", order_body)
            if order_res.get("code") != "0":
                err_msg = order_res.get('msg', 'Neznáma chyba OKX')
                return JSONResponse({"status": "error", "message": err_msg})
            
            coin_amount = float(formatted_sz)
            actual_spent_usdc = coin_amount * current_price
            sl_price = current_price * (1 - 0.006)
            be_price = current_price * (1 + (SETTINGS["be_trigger_pct"] / 100.0)) if SETTINGS.get("be_trigger_pct", 0) > 0 else 0
            ttp_price = current_price * (1 + (SETTINGS["ttp_trigger_pct"] / 100.0)) if SETTINGS.get("ttp_trigger_pct", 0) > 0 else 0
            target_pct = SETTINGS.get("target_pct", 0.75)
            
            ACTIVE_TRADES[coin_upper] = {
                "entry_price": current_price,
                "current_price": current_price,
                "current_sl_price": sl_price,
                "be_target_price": be_price,
                "ttp_target_price": ttp_price,
                "amount": coin_amount,
                "initial_amount": coin_amount,
                "lot_sz": lot_sz,
                "min_sz": min_sz,
                "bought_at": get_sk_time_str(),
                "market_pair": okx_symbol,
                "highest_price": current_price,
                "break_even_activated": False,
                "ttp_activated": False,
                "invested_usdc": actual_spent_usdc,
                "live_pnl_pct": 0.0,
                "live_pnl_usdc": 0.0,
                "tf": CURRENT_TIMEFRAME,
                "settings": SETTINGS.copy()
            }
            log_event(f"Manuálny nákup [{CURRENT_TIMEFRAME}]: {okx_symbol} za {current_price:.4f} | Investícia: {actual_spent_usdc:.2f}$")
            send_telegram_msg(f"<b>MANUÁLNY NÁKUP [TF: {CURRENT_TIMEFRAME}]</b>\n<b>Pár:</b> {okx_symbol}\n<b>Vstup:</b> {current_price:.4f}\n<b>Investícia:</b> {actual_spent_usdc:.2f} USDC")
            
            return JSONResponse({"status": "success", "symbol": okx_symbol})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

@app.post("/api/sell-now")
async def sell_now():
    global ACTIVE_TRADES
    async with trade_lock:
        if not ACTIVE_TRADES:
            return JSONResponse({"status": "error", "message": "Žiadna aktívna pozícia na predaj."})
        sold = []
        for coin in list(ACTIVE_TRADES.keys()):
            trade = ACTIVE_TRADES[coin]
            symbol = trade["market_pair"]
            try:
                ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={symbol}")
                curr_p = safe_float(ticker_res['data'][0]['last']) if ticker_res.get('code') == '0' and ticker_res.get('data') else trade["entry_price"]

                fmt_sz = format_amount(trade["amount"], trade.get("lot_sz", 0.01), trade.get("min_sz", 0.01))
                if fmt_sz:
                    order_body = json.dumps({
                        "instId": symbol, "tdMode": "cash", "side": "sell",
                        "ordType": "market", "sz": fmt_sz
                    })
                    okx_request("POST", "/api/v5/trade/order", order_body)
                pnl_pct = ((curr_p - trade["entry_price"]) / trade["entry_price"]) * 100
                invested = trade.get("invested_usdc", trade["amount"] * trade["entry_price"])
                pnl_usdc = invested * (pnl_pct / 100.0)
                record_closed_trade(symbol, pnl_pct, pnl_usdc, "Ručný okamžitý predaj (UI)")
                log_event(f"Okamžitý ručný predaj pre {symbol} | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                send_telegram_msg(f"<b>OKAMŽITÝ RUČNÝ PREDAJ</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                sold.append(symbol)
            except Exception as e:
                log_event(f"Chyba pri Emergency Sell pre {symbol}: {e}")
            finally:
                if coin in ACTIVE_TRADES:
                    del ACTIVE_TRADES[coin]

        return JSONResponse({"status": "success", "sold": sold})

HTML_TEMPLATE_CONTENT = """
<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <title>Apex Scalper PRO</title>
    <style>
        :root { --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --text-muted: #94a3b8; --border: #334155; --border-hover: #475569; }
        body { background: var(--bg); color: var(--text); font-family: sans-serif; margin: 0; padding: 20px; }
        .container { max-width: 1200px; margin: auto; }
        .header { display: flex; justify-content: space-between; align-items: center; background: var(--card); padding: 15px; border-radius: 8px; border: 1px solid var(--border); margin-bottom: 20px; }
        .btn { background: var(--border); color: var(--text); border: none; padding: 8px 12px; border-radius: 4px; cursor: pointer; font-weight: bold; }
        .btn-on { background: #10b981; color: white; }
        .btn-off { background: #ef4444; color: white; }
        .btn-yellow { background: #f59e0b; color: white; }
        .btn-sell { background: #dc2626; color: white; }
        .status-ticker { background: var(--card); padding: 10px 15px; border-radius: 8px; border: 1px solid var(--border); margin-bottom: 20px; display: flex; align-items: center; gap: 10px; }
        .status-dot { width: 10px; height: 10px; background: #10b981; border-radius: 50%; animation: pulse 1.5s infinite; }
        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.4; } 100% { opacity: 1; } }
        .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin-bottom: 20px; }
        .stat-card { background: var(--card); padding: 15px; border-radius: 8px; border: 1px solid var(--border); }
        .stat-title { font-size: 0.85rem; color: var(--text-muted); margin-bottom: 5px; }
        .stat-value { font-size: 1.25rem; font-weight: bold; }
        .terminal-card { background: var(--card); border-radius: 8px; border: 1px solid var(--border); overflow: hidden; margin-bottom: 20px; }
        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.9rem; }
        th, td { padding: 12px 15px; border-bottom: 1px solid var(--border); }
        th { background: rgba(255,255,255,0.02); color: var(--text-muted); }
        .scrollable-card { max-height: 300px; overflow-y: auto; }
        .logs-box { background: #020617; border: 1px solid var(--border); border-radius: 8px; padding: 15px; font-family: monospace; font-size: 0.85rem; max-height: 200px; overflow-y: auto; color: #38bdf8; }
        .log-line { margin-bottom: 4px; }
        .section-title { font-size: 1rem; font-weight: bold; margin-bottom: 10px; color: var(--text-muted); }
        .text-green { color: #10b981; }
        .text-red { color: #ef4444; }
        .text-yellow-highlight { color: #f59e0b; }
        .custom-buy-box { display: flex; gap: 5px; }
        .custom-buy-input { background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 4px 8px; border-radius: 4px; width: 70px; }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h2>Apex Scalper PRO <span id="header-tf" style="font-size:0.9rem; color:var(--text-muted);">{{ CURRENT_TIMEFRAME }}</span></h2>
            <div style="font-size: 0.85rem; color: var(--text-muted);">Zostatok: <span id="header-balance" style="color:var(--text); font-weight:bold;">{{ balance:.2f }} USDC</span></div>
        </div>
        <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
            <div style="display: flex; gap: 2px; align-items: center; border: 1px solid var(--border-hover); padding: 2px 4px; border-radius: 4px; background: rgba(255,255,255,0.01);">
                <span style="font-size: 0.65rem; color: var(--text-muted); margin-right: 2px;">PARALLEL:</span>
                <button class="parallel-btn btn {% if SETTINGS.max_parallel == 1 %}btn-yellow{% endif %}" data-val="1" onclick="setParallel(1)">1</button>
                <button class="parallel-btn btn {% if SETTINGS.max_parallel == 2 %}btn-yellow{% endif %}" data-val="2" onclick="setParallel(2)">2</button>
                <button class="parallel-btn btn {% if SETTINGS.max_parallel == 3 %}btn-yellow{% endif %}" data-val="3" onclick="setParallel(3)">3</button>
                <button class="parallel-btn btn {% if SETTINGS.max_parallel == 4 %}btn-yellow{% endif %}" data-val="4" onclick="setParallel(4)">4</button>
            </div>
            <button class="budget-btn btn {% if SETTINGS.max_budget_cap == 500 %}btn-yellow{% endif %}" data-val="500" onclick="setBudget(500)">500</button>
            <button class="budget-btn btn {% if SETTINGS.max_budget_cap == 1000 %}btn-yellow{% endif %}" data-val="1000" onclick="setBudget(1000)">1K</button>
            <button class="budget-btn btn {% if SETTINGS.max_budget_cap == 3000 %}btn-yellow{% endif %}" data-val="3000" onclick="setBudget(3000)">3K</button>
            <button class="budget-btn btn {% if SETTINGS.max_budget_cap == 5000 %}btn-yellow{% endif %}" data-val="5000" onclick="setBudget(5000)">5K</button>
            <button class="budget-btn btn {% if SETTINGS.max_budget_cap == 10000 %}btn-yellow{% endif %}" data-val="10000" onclick="setBudget(10000)">10K</button>
            <div class="custom-buy-box">
                <input type="text" id="custom-coin" class="custom-buy-input" placeholder="SOL/SUI">
                <button class="btn btn-on" style="padding: 2px 6px;" onclick="manualBuyCustom()">BUY</button>
            </div>
            <button id="compounding-btn" class="btn {% if SETTINGS.compounding_enabled %}btn-yellow{% endif %}" onclick="toggleCompounding()">
                COMPOUNDING: {% if SETTINGS.compounding_enabled %}ON{% else %}OFF{% endif %}
            </button>
            <button id="btc-btn" class="btn {% if SETTINGS.use_btc_filter %}btn-on{% else %}btn-off{% endif %}" onclick="toggleBtcFilter()">
                BTC: {% if SETTINGS.use_btc_filter %}ON{% else %}OFF{% endif %}
            </button>
            <button id="bot-btn" class="btn {% if IS_BOT_ACTIVE %}btn-on{% else %}btn-off{% endif %}" onclick="toggleBot()">
                {% if IS_BOT_ACTIVE %}ACTIVE{% else %}PAUSED{% endif %}
            </button>
            <button id="sell-btn" class="btn btn-sell" onclick="sellNow()" disabled>EMERGENCY SELL</button>
        </div>
    </div>

    <div class="status-ticker">
        <div class="status-dot"></div>
        <span id="current-stage">{{ CURRENT_STAGE }}</span>
    </div>

    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-title">Win Rate</div>
            <div class="stat-value" id="stat-winrate">0.0%</div>
        </div>
        <div class="stat-card">
            <div class="stat-title">Total Trades</div>
            <div class="stat-value" id="stat-trades">0</div>
        </div>
        <div class="stat-card">
            <div class="stat-title">Win / Loss</div>
            <div class="stat-value" id="stat-winloss">0W / 0L</div>
        </div>
        <div class="stat-card">
            <div class="stat-title">Net PnL</div>
            <div class="stat-value" id="stat-pnl">+0.00 $</div>
        </div>
    </div>

    <div>
        <div class="section-title">Active Position Matrix (100% Exit)</div>
        <div class="terminal-card">
            <table>
                <thead>
                    <tr>
                        <th>Pair / TF</th>
                        <th>Entry</th>
                        <th>Current</th>
                        <th>Status</th>
                        <th>Position Size</th>
                        <th>Live PnL (%)</th>
                        <th>Total PnL (USDC)</th>
                    </tr>
                </thead>
                <tbody id="active-position-tbody">
                    <tr><td colspan="7" style="text-align: center; color: var(--text-muted);">Autonómny skener priebežne vyhodnocuje Top likvidné USDC páry...</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <div>
        <div class="section-title">Execution History</div>
        <div class="terminal-card scrollable-card">
            <table>
                <thead>
                    <tr>
                        <th>Timestamp</th>
                        <th>Pair</th>
                        <th>Trigger Reason</th>
                        <th>Net PnL</th>
                    </tr>
                </thead>
                <tbody id="history-tbody">
                    <tr><td colspan="4" style="text-align: center; color: var(--text-muted);">Zatiaľ žiadne ukončené obchody.</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <div>
        <div class="section-title">System Execution Logs</div>
        <div class="logs-box" id="logs-container">
            <div class="log-line">[System Initialized] Autonómny režim pripravený.</div>
        </div>
    </div>
</div>

<script>
    async function toggleBot() {
        let res = await fetch('/api/toggle-bot', {method: 'POST'});
        let data = await res.json();
        let btn = document.getElementById('bot-btn');
        btn.className = data.is_bot_active ? "btn btn-on" : "btn btn-off";
        btn.innerText = data.is_bot_active ? "ACTIVE" : "PAUSED";
        updateState();
    }

    async function toggleBtcFilter() {
        let res = await fetch('/api/toggle-btc-filter', {method: 'POST'});
        let data = await res.json();
        let btn = document.getElementById('btc-btn');
        btn.className = data.use_btc_filter ? "btn btn-on" : "btn btn-off";
        btn.innerText = data.use_btc_filter ? "BTC: ON" : "BTC: OFF";
    }

    async function toggleCompounding() {
        let res = await fetch('/api/toggle-compounding', {method: 'POST'});
        let data = await res.json();
        let btn = document.getElementById('compounding-btn');
        if (data.compounding_enabled) {
            btn.className = "btn btn-yellow";
            btn.innerText = "COMPOUNDING: ON";
        } else {
            btn.className = "btn";
            btn.innerText = "COMPOUNDING: OFF";
        }
    }

    async function setBudget(amount) {
        let res = await fetch(`/api/set-budget/${amount}`, {method: 'POST'});
        let data = await res.json();
        if (data.status === 'success') {
            document.querySelectorAll('.budget-btn').forEach(b => {
                b.classList.remove('btn-yellow');
                if (parseInt(b.getAttribute('data-val')) === amount) {
                    b.classList.add('btn-yellow');
                }
            });
        }
    }

    async function setParallel(count) {
        let res = await fetch(`/api/set-parallel/${count}`, {method: 'POST'});
        let data = await res.json();
        if (data.status === 'success') {
            document.querySelectorAll('.parallel-btn').forEach(b => {
                b.classList.remove('btn-yellow');
                if (parseInt(b.getAttribute('data-val')) === count) {
                    b.classList.add('btn-yellow');
                }
            });
        }
    }

    async function manualBuyCustom() {
        let coinInput = document.getElementById('custom-coin');
        let coin = coinInput.value.trim().toUpperCase();
        if (!coin) {
            alert("Zadajte skratku tokenu (napr. SOL, SUI, XRP)!");
            return;
        }
        if (!confirm(`Naozaj vykonať manuálny nákup ${coin}-USDC?`)) return;
        
        let res = await fetch(`/api/manual-buy/${coin}`, {method: 'POST'});
        let data = await res.json();
        if (data.status === 'success') {
            coinInput.value = "";
            updateState();
        } else {
            alert("Chyba pri manuálnom nákupe: " + data.message);
        }
    }

    async function sellNow() {
        if (!confirm("Naozaj vykonať núdzový okamžitý predaj všetkých pozícií?")) return;
        await fetch('/api/sell-now', {method: 'POST'});
        updateState();
    }

    async function updateState() {
        try {
            let res = await fetch('/api/state');
            let data = await res.json();
            
            document.getElementById('header-balance').innerText = data.balance.toFixed(2) + " USDC";
            document.getElementById('current-stage').innerText = data.current_stage;
            
            let stats = data.stats;
            document.getElementById('stat-winrate').innerText = stats.win_rate.toFixed(1) + "%";
            document.getElementById('stat-trades').innerText = stats.total_trades;
            document.getElementById('stat-winloss').innerText = stats.wins + "W / " + stats.losses + "L";
            let pnlEl = document.getElementById('stat-pnl');
            pnlEl.innerText = (stats.total_pnl >= 0 ? "+" : "") + stats.total_pnl.toFixed(2) + " $";
            pnlEl.className = "stat-value " + (stats.total_pnl >= 0 ? "text-green" : "text-red");

            let activeTbody = document.getElementById('active-position-tbody');
            let sellBtn = document.getElementById('sell-btn');
            if (Object.keys(data.active_trades).length > 0) {
                let html = "";
                for (let [coin, t] of Object.entries(data.active_trades)) {
                    let pnlClass = t.live_pnl_pct >= 0 ? "text-green" : "text-red";
                    let statusText = "V TRHU (ČAKÁ NA TP)";
                    
                    html += `<tr>
                        <td><b>${t.market_pair}</b><br><span style="font-size:0.6rem; color:var(--text-muted);">TF: ${t.tf}</span></td>
                        <td>${t.entry_price.toFixed(4)}</td>
                        <td>${t.current_price.toFixed(4)}</td>
                        <td><span class="text-yellow-highlight">${statusText}</span></td>
                        <td>${t.amount.toFixed(2)} ks</td>
                        <td class="${pnlClass}">${t.live_pnl_pct >= 0 ? "+" : ""}${t.live_pnl_pct.toFixed(2)}%</td>
                        <td class="${pnlClass}">${t.live_pnl_usdc >= 0 ? "+" : ""}${t.live_pnl_usdc.toFixed(2)} $</td>
                    </tr>`;
                }
                activeTbody.innerHTML = html;
                sellBtn.disabled = false;
            } else {
                activeTbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">Skenujem trh pre Pine Script All Green signály...</td></tr>`;
                sellBtn.disabled = true;
            }

            let historyTbody = document.getElementById('history-tbody');
            if (data.history.length > 0) {
                let hHtml = "";
                for (let h of data.history) {
                    let pnlClass = h.pnl_usdc >= 0 ? "text-green" : "text-red";
                    hHtml += `<tr>
                        <td>${h.time}</td>
                        <td><b>${h.pair}</b></td>
                        <td>${h.reason}</td>
                        <td class="${pnlClass}">${h.pnl_usdc >= 0 ? "+" : ""}${h.pnl_usdc.toFixed(2)} $ (${h.pnl_pct >= 0 ? "+" : ""}${h.pnl_pct.toFixed(2)}%)</td>
                    </tr>`;
                }
                historyTbody.innerHTML = hHtml;
            } else {
                historyTbody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">Zatiaľ žiadne ukončené obchody.</td></tr>`;
            }

            let logsBox = document.getElementById('logs-container');
            let lHtml = "";
            for (let log of data.logs) {
                lHtml += `<div class="log-line">${log}</div>`;
            }
            logsBox.innerHTML = lHtml;

        } catch (err) {
            console.error("Chyba pri obnove stavu:", err);
        }
    }

    setInterval(updateState, 2000);
    updateState();
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    balance = fetch_account_capital("USDC")
    t = Template(HTML_TEMPLATE_CONTENT)
    return t.render(
        SETTINGS=SETTINGS,
        IS_BOT_ACTIVE=IS_BOT_ACTIVE,
        CURRENT_TIMEFRAME=CURRENT_TIMEFRAME,
        CURRENT_STAGE=CURRENT_STAGE,
        balance=balance
    )

@app.get("/api/state")
async def get_state():
    balance = fetch_account_capital("USDC")
    stats = calculate_stats()
    return JSONResponse({
        "balance": balance,
        "is_bot_active": IS_BOT_ACTIVE,
        "current_stage": CURRENT_STAGE,
        "stats": stats,
        "active_trades": ACTIVE_TRADES,
        "history": CLOSED_TRADES_HISTORY[:20],
        "logs": SYSTEM_LOGS
    })

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
