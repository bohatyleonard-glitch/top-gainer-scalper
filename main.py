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
import requests
import uvicorn

# --- OKX KLÚČE & BEZPEČNOSŤ ---

OKX_API_KEY = os.getenv("OKX_API_KEY", "54ebe9fe-1fd0-443d-8f76-0cd1a3439ddb")
OKX_SECRET = os.getenv("OKX_SECRET", "FBD5C8332D88E3ED5662470CA9C3E813")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "Novyzeland111/")
WEBHOOK_SECRET = "moje_tajne_heslo_123"

# --- TELEGRAM NOTIFIKÁCIE & INTERAKTÍVNY BOT ---

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9015239181:AAE4OZ7roF6nInGLCHDb9tuQX0R3kq3-su8")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "9130979784")

# --- DYNAMICKÉ PROFILY PRE ČASOVÉ RÁMCE ---

TIMEFRAME_PROFILES = {
    "1m": {"trail_pct": 0.25, "be_trigger_pct": 0.25, "ttp_trigger_pct": 0.45, "ttp_callback_pct": 0.1, "target_pct": 0.0, "max_sl_pct": 0.5, "desc": "Skalper 1m (SL max -0.5%)"},
    "5m": {"trail_pct": 0.3, "be_trigger_pct": 0.3, "ttp_trigger_pct": 0.5, "ttp_callback_pct": 0.1, "target_pct": 0.0, "max_sl_pct": 0.6, "desc": "Skalper 5m (Rýchly SL max -0.6%)"},
    "8m": {"trail_pct": 0.35, "be_trigger_pct": 0.35, "ttp_trigger_pct": 0.55, "ttp_callback_pct": 0.12, "target_pct": 0.0, "max_sl_pct": 0.7, "desc": "Skalper 8m (SL max -0.7%)"},
    "15m": {"trail_pct": 0.4, "be_trigger_pct": 0.4, "ttp_trigger_pct": 0.65, "ttp_callback_pct": 0.15, "target_pct": 0.0, "max_sl_pct": 1.0, "desc": "15m Fibo 0.618 (SL max -1.0%)"},
    "30m": {"trail_pct": 0.6, "be_trigger_pct": 0.5, "ttp_trigger_pct": 1.0, "ttp_callback_pct": 0.2, "target_pct": 0.0, "max_sl_pct": 1.5, "desc": "30m trendový (SL max -1.5%)"},
    "45m": {"trail_pct": 0.8, "be_trigger_pct": 0.6, "ttp_trigger_pct": 1.2, "ttp_callback_pct": 0.25, "target_pct": 0.0, "max_sl_pct": 2.0, "desc": "45m strednodobý (SL max -2.0%)"},
    "1h": {"trail_pct": 1.0, "be_trigger_pct": 0.7, "ttp_trigger_pct": 1.4, "ttp_callback_pct": 0.3, "target_pct": 0.0, "max_sl_pct": 2.2, "desc": "1h swing (SL max -2.2%)"},
    "2h": {"trail_pct": 1.2, "be_trigger_pct": 0.8, "ttp_trigger_pct": 1.6, "ttp_callback_pct": 0.4, "target_pct": 0.0, "max_sl_pct": 2.5, "desc": "2h trend (SL max -2.5%)"},
    "3h": {"trail_pct": 1.5, "be_trigger_pct": 1.0, "ttp_trigger_pct": 2.0, "ttp_callback_pct": 0.5, "target_pct": 0.0, "max_sl_pct": 3.0, "desc": "3h pozičný (SL max -3.0%)"},
    "4h": {"trail_pct": 2.0, "be_trigger_pct": 1.2, "ttp_trigger_pct": 2.5, "ttp_callback_pct": 0.6, "target_pct": 0.0, "max_sl_pct": 3.5, "desc": "4h swing (SL max -3.5%)"}
}

ACTIVE_TRADES = {}
DAILY_PNL_CALENDAR = []
CLOSED_TRADES_HISTORY = []
SYSTEM_LOGS = []
CURRENT_STAGE = "STAV: Mimo obchodu. Čakám na synchronizovaný signál..."
LAST_TRADE_TIME = {}
UNSUPPORTED_DEMO_SYMBOLS = set()
MARKET_SCANNER_CACHE = {} 
trade_lock = asyncio.Lock()

IS_BOT_ACTIVE = True
CURRENT_TIMEFRAME = "15M"
STORAGE_FILE = "trades_history.json"
ALLOWED_STABLES = ["USDC"]

TOP_LIQUID_SYMBOLS = [
    "SOL-USDC", "SUI-USDC", "XRP-USDC", "ADA-USDC", "AVAX-USDC",
    "LINK-USDC", "DOGE-USDC", "PEPE-USDC", "NEAR-USDC", "RUNE-USDC",
    "RENDER-USDC", "APT-USDC", "ARB-USDC", "OP-USDC", "INJ-USDC",
    "TIA-USDC", "SEI-USDC", "DOT-USDC", "ATOM-USDC", "UNI-USDC"
]

SETTINGS = TIMEFRAME_PROFILES["15m"].copy()
SETTINGS.update({
    "use_btc_filter": True,
    "reentry_cooldown_sec": 120,
    "use_breakeven": True,
    "max_budget_cap": 1000.0,
    "compounding_enabled": False,
    "max_parallel": 1,
    "ha_exit_consecutive": 3
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
        print(f"❌ Chyba pri odosielaní Telegram notifikácie: {err}", flush=True)

def load_persistent_data():
    global CLOSED_TRADES_HISTORY, DAILY_PNL_CALENDAR
    if os.path.exists(STORAGE_FILE):
        try:
            with open(STORAGE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                CLOSED_TRADES_HISTORY = data.get("history", [])
                DAILY_PNL_CALENDAR = data.get("calendar", [])
                log_event(f"📂 Načítaných {len(CLOSED_TRADES_HISTORY)} histórie obchodov z disku.")
        except Exception as e:
            log_event(f"⚠️ Chyba pri načítaní úložiska: {e}")

def save_persistent_data():
    try:
        cutoff_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        filtered_history = [h for h in CLOSED_TRADES_HISTORY if h.get("time", "") >= cutoff_date]
        data = {"history": filtered_history, "calendar": DAILY_PNL_CALENDAR}
        with open(STORAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_event(f"⚠️ Chyba pri ukladaní údajov na disk: {e}")

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

# --- HYBRIDNÝ REQUEST SYSTEM ---

def okx_public_request(path: str):
    url = "https://www.okx.com" + path
    try:
        response = requests.get(url, timeout=5)
        return response.json()
    except Exception as err:
        return {"code": "-1", "msg": str(err)}

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
        res = okx_public_request(f"/api/v5/public/instruments?instType=SPOT&instId={symbol}")
        if res.get("code") == "0" and res.get("data") and len(res["data"]) > 0:
            inst = res["data"][0]
            lot_sz = safe_float(inst.get("lotSz"), 0.01)
            min_sz = safe_float(inst.get("minSz"), 0.01)
            return lot_sz, min_sz
    except Exception as e:
        log_event(f"⚠️ Chyba pri načítaní pravidiel pre {symbol}: {e}")
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
        log_event(f"⚠️ Chyba pri načítaní zostatku: {err}")
    return 13000.0

def check_btc_health() -> bool:
    if not SETTINGS["use_btc_filter"]:
        return True
    try:
        tf_str = CURRENT_TIMEFRAME.lower()
        url = f"/api/v5/market/candles?instId=BTC-USDC&bar={tf_str}&limit=10"
        res = okx_public_request(url)
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
        res = okx_public_request("/api/v5/market/tickers?instType=SPOT")
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
            if len(top_20) >= 5:
                TOP_LIQUID_SYMBOLS = top_20
            elif top_20:
                merged = list(dict.fromkeys(top_20 + TOP_LIQUID_SYMBOLS))
                TOP_LIQUID_SYMBOLS = merged[:20]
    except Exception as e:
        log_event(f"⚠️ Chyba pri aktualizácii top symbolov: {e}")

# --- 100% PINE SCRIPT SYNCHRONIZOVANÁ MATEMATIKA ---

def calc_ema(values, length):
    if len(values) < length:
        return [values[-1]] * len(values)
    alpha = 2 / (length + 1)
    ema = [sum(values[:length]) / length]
    for price in values[length:]:
        ema.append(price * alpha + ema[-1] * (1 - alpha))
    return ema

def find_pivots(highs, lows, pivot_len=2):
    last_high = None
    last_low = None
    for i in range(pivot_len, len(highs) - pivot_len):
        is_high = True
        for j in range(i - pivot_len, i + pivot_len + 1):
            if highs[j] > highs[i]:
                is_high = False
                break
        if is_high:
            last_high = highs[i]

        is_low = True
        for j in range(i - pivot_len, i + pivot_len + 1):
            if lows[j] < lows[i]:
                is_low = False
                break
        if is_low:
            last_low = lows[i]
    return last_high, last_low

def evaluate_pine_strategy(symbol: str, bar_tf: str = "15m"):
    global MARKET_SCANNER_CACHE, ACTIVE_TRADES
    coin_key = symbol.split("-")[0]

    try:
        url = f"/api/v5/market/candles?instId={symbol}&bar={bar_tf}&limit=60"
        res = okx_public_request(url)
        if res.get("code") != "0" or not res.get("data") or len(res["data"]) < 10:
            MARKET_SCANNER_CACHE[coin_key] = {
                "symbol": symbol, "price": 0.0, "oscillator": 0.0,
                "momentum": "SLABÉ", "impulse": "ČAKÁ", "trade_status": "---",
                "be_status": "VYPNUTÝ", "ttp_status": "VYPNUTÝ", "ts_status": "VYPNUTÝ",
                "exec_signal": "ČAKÁ", "signal": "⚠️ CHYBA DÁT", "is_green": False,
                "fib_0618": None, "fib_0786": None
            }
            return False

        candles = res["data"]
        candles.reverse()
        
        closed_candles = candles[:-1]
        
        highs = [safe_float(c[2]) for c in closed_candles]
        lows = [safe_float(c[3]) for c in closed_candles]
        
        ha_closes, ha_opens, r_closes = [], [], []
        volumes = []
        for i, c in enumerate(closed_candles):
            o, h, l, cl, v = safe_float(c[1]), safe_float(c[2]), safe_float(c[3]), safe_float(c[4]), safe_float(c[5])
            ha_cl = (o + h + l + cl) / 4.0
            ha_op = (o + cl) / 2.0 if i == 0 else (ha_opens[-1] + ha_closes[-1]) / 2.0
            ha_closes.append(ha_cl)
            ha_opens.append(ha_op)
            r_closes.append(cl)
            volumes.append(v)
            
        vol_dirs = [v if ha_closes[i] > ha_opens[i] else -v for i, v in enumerate(volumes)]
        fast_ma = calc_ema(vol_dirs, 5)
        slow_ma = calc_ema(vol_dirs, 20)
        
        oscillator_value = [f - s for f, s in zip(fast_ma, slow_ma)]
        signal_line = calc_ema(oscillator_value, 9)
        
        curr_osc = oscillator_value[-1]
        curr_sig = signal_line[-1]
        
        cond_volume_bullish = curr_osc > 0 and curr_osc > curr_sig
        momentum_str = "SILNÉ" if cond_volume_bullish else "SLABÉ"

        last_high, last_low = find_pivots(highs, lows, pivot_len=2)
        fib_0618, fib_0786 = None, None
        if last_high and last_low and last_high > last_low:
            range_size = last_high - last_low
            fib_0618 = last_high - range_size * 0.618
            fib_0786 = last_high - range_size * 0.786

        curr_low = lows[-1]
        curr_real_close = r_closes[-1]
        buy_trigger = False
        if cond_volume_bullish and fib_0618 and fib_0786:
            if curr_low <= fib_0618 and curr_real_close >= fib_0786:
                buy_trigger = True

        ha_consec_req = SETTINGS.get("ha_exit_consecutive", 3)
        ha_exit_triggered = False
        if len(ha_closes) >= ha_consec_req:
            ha_exit_triggered = all(ha_closes[-k] < ha_opens[-k] for k in range(1, ha_consec_req + 1))

        is_in_trade = coin_key in ACTIVE_TRADES
        impuls_active = buy_trigger or is_in_trade
        impulse_str = "AKTÍVNY (V TRHU)" if impuls_active else "ČAKÁ"

        trade_status_txt = f"{ACTIVE_TRADES[coin_key]['entry_price']:.4f}" if is_in_trade else "---"
        be_status_txt = "VYPNUTÝ"
        if is_in_trade:
            be_status_txt = f"{ACTIVE_TRADES[coin_key]['entry_price']:.4f}" if ACTIVE_TRADES[coin_key].get("break_even_activated") else "ČAKÁ"

        ttp_status_txt = "VYPNUTÝ"
        if is_in_trade:
            if ACTIVE_TRADES[coin_key].get("ttp_activated"):
                ttp_status_txt = f"{ACTIVE_TRADES[coin_key].get('ttp_stop_level', 0):.4f}"
            else:
                ttp_status_txt = "V TRHU"

        ts_status_txt = "VYPNUTÝ"
        if is_in_trade:
            ts_status_txt = f"{ACTIVE_TRADES[coin_key].get('current_sl_price', 0):.4f}"

        if is_in_trade and ha_exit_triggered:
            exec_signal_str = "EXIT SIGNÁL"
        elif buy_trigger and not is_in_trade:
            exec_signal_str = "NAKÚPIŤ"
        elif is_in_trade:
            exec_signal_str = "DRŽAŤ"
        else:
            exec_signal_str = "ČAKAJ"

        MARKET_SCANNER_CACHE[coin_key] = {
            "symbol": symbol,
            "price": curr_real_close,
            "oscillator": round(curr_osc, 2),
            "momentum": momentum_str,
            "impulse": impulse_str,
            "trade_status": trade_status_txt,
            "be_status": be_status_txt,
            "ttp_status": ttp_status_txt,
            "ts_status": ts_status_txt,
            "exec_signal": exec_signal_str,
            "signal": "🟢 FIBO 0.618 (Sync)" if buy_trigger else ("🟡 BULLISH (Čaká)" if cond_volume_bullish else "🔴 SLABÝ / TRH"),
            "is_green": buy_trigger,
            "fib_0618": fib_0618,
            "fib_0786": fib_0786
        }

        return buy_trigger
    except Exception as e:
        MARKET_SCANNER_CACHE[coin_key] = {
            "symbol": symbol, "price": 0.0, "oscillator": 0.0,
            "momentum": "SLABÉ", "impulse": "ČAKÁ", "trade_status": "---",
            "be_status": "VYPNUTÝ", "ttp_status": "VYPNUTÝ", "ts_status": "VYPNUTÝ",
            "exec_signal": "ČAKAJ", "signal": "⚠️ VÝNIMKA", "is_green": False,
            "fib_0618": None, "fib_0786": None
        }
        return False

# --- AUTONÓMNA SKENOVACIA SLUČKA ---

async def autonomous_scanner_loop():
    await asyncio.sleep(3)
    log_event(f"🤖 Autonómny Pine Script skener (Fibo 0.618 + 100% Sync) spustený pre {len(TOP_LIQUID_SYMBOLS)} symbolov!")
    while True:
        try:
            max_allowed = SETTINGS.get("max_parallel", 1)
            if IS_BOT_ACTIVE:
                btc_ok = check_btc_health()
                tf_str = CURRENT_TIMEFRAME.lower()
                for symbol in list(TOP_LIQUID_SYMBOLS):
                    coin_name = symbol.split("-")[0]
                    now_ts = datetime.now().timestamp()

                    if coin_name in ACTIVE_TRADES or (coin_name in LAST_TRADE_TIME and (now_ts - LAST_TRADE_TIME[coin_name] < SETTINGS["reentry_cooldown_sec"])):
                        evaluate_pine_strategy(symbol, bar_tf=tf_str)
                        await asyncio.sleep(0.2)
                        continue

                    if len(ACTIVE_TRADES) < max_allowed and btc_ok:
                        signal_met = evaluate_pine_strategy(symbol, bar_tf=tf_str)
                        if signal_met:
                            log_event(f"🚀 FIBO 0.618 SYNCHRONIZOVANÝ SIGNÁL PRE {symbol}!")
                            await execute_autonomous_buy(symbol)
                    else:
                        evaluate_pine_strategy(symbol, bar_tf=tf_str)
                        
                    await asyncio.sleep(0.3)
        except Exception as e:
            log_event(f"⚠️ Chyba v autonómnom skeneri: {e}")
        await asyncio.sleep(10)

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
        else:
            safe_budget = min(allocated_capital_base * 0.98, budget_cap)

        if safe_budget < 5.0:
            log_event(f"❌ Nedostatok USDC prostriedkov: {free_capital}")
            return
            
        ticker_res = okx_public_request(f"/api/v5/market/ticker?instId={okx_symbol}")
        if ticker_res.get("code") != "0" or not ticker_res.get("data"):
            log_event(f"❌ Nepodarilo sa získať cenu pre {okx_symbol}")
            return
            
        current_price = safe_float(ticker_res["data"][0]["last"])
        raw_amount = safe_budget / current_price
        
        lot_sz, min_sz = get_instrument_rules(okx_symbol)
        formatted_sz = format_amount(raw_amount, lot_sz, min_sz)
        
        if not formatted_sz:
            log_event(f"❌ Množstvo pre {okx_symbol} nedosahuje minimum burzy minSz ({min_sz})")
            return
            
        order_body = json.dumps({
            "instId": okx_symbol, "tdMode": "cash", "side": "buy",
            "ordType": "market", "sz": formatted_sz
        })
        
        order_res = okx_request("POST", "/api/v5/trade/order", order_body)
        if order_res.get("code") != "0":
            err_msg = order_res.get('msg', 'Neznáma chyba OKX')
            log_event(f"⚠️ Pár {okx_symbol} odmietnutý v Demo režime ({err_msg}).")
            return
            
        coin_amount = float(formatted_sz)
        actual_spent_usdc = coin_amount * current_price

        max_allowed_sl_pct = SETTINGS.get("max_sl_pct", 1.0) / 100.0
        absolute_max_sl_price = current_price * (1.0 - max_allowed_sl_pct)

        cached_fib_0786 = MARKET_SCANNER_CACHE.get(coin_name, {}).get("fib_0786")
        if cached_fib_0786 and 0 < cached_fib_0786 < current_price:
            sl_price = max(cached_fib_0786, absolute_max_sl_price)
        else:
            sl_price = absolute_max_sl_price

        be_price = current_price * (1 + (SETTINGS["be_trigger_pct"] / 100.0)) if SETTINGS.get("be_trigger_pct", 0) > 0 else 0
        ttp_price = current_price * (1 + (SETTINGS["ttp_trigger_pct"] / 100.0)) if SETTINGS.get("ttp_trigger_pct", 0) > 0 else 0
        
        ACTIVE_TRADES[coin_name] = {
            "entry_price": current_price,
            "current_price": current_price,
            "initial_sl_price": sl_price,
            "current_sl_price": sl_price,
            "be_target_price": be_price,
            "ttp_target_price": ttp_price,
            "ttp_stop_level": 0.0,
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
        
        log_event(f"🛒 Autonómny nákup [{CURRENT_TIMEFRAME}]: {okx_symbol} za {current_price:.4f} USDC | SL (0.786/Max-{SETTINGS.get('max_sl_pct', 1.0)}%): {sl_price:.4f} | Investícia: {actual_spent_usdc:.2f} USDC")
        
        inline_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "🛑 ZATVORIŤ VŠETKO", "callback_data": "sell_now"},
                    {"text": "⏸️ PAUZA BOTA", "callback_data": "toggle_bot"}
                ]
            ]
        }
        send_telegram_msg(
            f"🟢 <b>SYNCHRONIZOVANÝ NÁKUP (Fibo 0.618) [TF: {CURRENT_TIMEFRAME}]</b>\n"
            f"<b>Pár:</b> {okx_symbol}\n"
            f"<b>Vstup:</b> {current_price:.4f}\n"
            f"<b>Počiatočný SL (0.786 / Max -{SETTINGS.get('max_sl_pct', 1.0)}%):</b> {sl_price:.4f}\n"
            f"<b>Investícia:</b> {actual_spent_usdc:.2f} USDC\n"
            f"🎯 <b>Prvý TP (Trailing):</b> +{SETTINGS['ttp_trigger_pct']}%",
            reply_markup=inline_keyboard
        )

# --- MONITOR RIZÍK ---

async def advanced_risk_monitor_loop():
    global ACTIVE_TRADES, CURRENT_STAGE
    await asyncio.sleep(3)
    while True:
        if not ACTIVE_TRADES:
            if IS_BOT_ACTIVE:
                max_p = SETTINGS.get("max_parallel", 1)
                CURRENT_STAGE = f"STAV: Autonómny Bot aktívny (Max {max_p} paralelné). Skenujem Fibo 0.618..."
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

                ticker_res = okx_public_request(f"/api/v5/market/ticker?instId={symbol}")
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
                        log_event(f"⏳ Expirácia 48h pre {symbol} | PnL: {pnl_pct:+.2f}%")
                        send_telegram_msg(f"⏳ <b>GTC EXPIRÁCIA 48H</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}%")
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
                    
                    trade["live_pnl_pct"] = pnl_pct
                    trade["live_pnl_usdc"] = pnl_usdc
                    CURRENT_STAGE = f"STAV: Aktívnych pozícií: {len(ACTIVE_TRADES)} | Posledná: {symbol} ({pnl_pct:+.2f}%)"

                    checklist_exec = MARKET_SCANNER_CACHE.get(coin, {}).get("exec_signal", "")
                    if checklist_exec in ["EXIT SIGNÁL", "PREDAJ"]:
                        reason = "Checklist Exit (3x červená HA sviečka)"
                        try:
                            fmt_sz = format_amount(trade["amount"], trade.get("lot_sz", 0.01), trade.get("min_sz", 0.01))
                            if fmt_sz:
                                order_body = json.dumps({
                                    "instId": symbol, "tdMode": "cash", "side": "sell",
                                    "ordType": "market", "sz": fmt_sz
                                })
                                okx_request("POST", "/api/v5/trade/order", order_body)
                            record_closed_trade(symbol, pnl_pct, pnl_usdc, reason)
                            log_event(f"🚨 Uzavretá pozícia ({reason}) pre {symbol} | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                            send_telegram_msg(f"🚨 <b>UZAVRETÁ POZÍCIA ({reason})</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                        except Exception:
                            pass
                        del ACTIVE_TRADES[coin]
                        continue

                    be_trigger = trade_settings.get("be_trigger_pct", 0.4)
                    trail_pct = trade_settings.get("trail_pct", 0.4)
                    
                    price_gain_since_entry = max(0.0, trade["highest_price"] - trade["entry_price"])
                    dynamic_fibo_sl = trade["initial_sl_price"] + price_gain_since_entry
                    trail_stop_price = trade["highest_price"] * (1 - (trail_pct / 100.0))
                    
                    effective_sl = max(trade["current_sl_price"], dynamic_fibo_sl)
                    
                    if be_trigger > 0 and pnl_pct >= be_trigger:
                        trade["break_even_activated"] = True
                        effective_sl = max(effective_sl, trail_stop_price, trade["entry_price"])
                    
                    trade["current_sl_price"] = effective_sl

                    ttp_trigger = trade_settings.get("ttp_trigger_pct", 0.65)
                    if ttp_trigger > 0:
                        ttp_activation_price = trade["entry_price"] * (1 + (ttp_trigger / 100.0))
                        if current_price >= ttp_activation_price:
                            trade["ttp_activated"] = True
                        
                        if trade.get("ttp_activated"):
                            callback_pct = trade_settings.get("ttp_callback_pct", 0.15)
                            current_ttp_level = trade["highest_price"] * (1 - (callback_pct / 100.0))
                            trade["ttp_stop_level"] = max(trade.get("ttp_stop_level", 0.0), current_ttp_level)
                            
                            if current_price <= trade["ttp_stop_level"]:
                                reason = f"Trailing TP Uzamknutý ({pnl_pct:+.2f}%)"
                                try:
                                    fmt_sz = format_amount(trade["amount"], trade.get("lot_sz", 0.01), trade.get("min_sz", 0.01))
                                    if fmt_sz:
                                        order_body = json.dumps({
                                            "instId": symbol, "tdMode": "cash", "side": "sell",
                                            "ordType": "market", "sz": fmt_sz
                                        })
                                        okx_request("POST", "/api/v5/trade/order", order_body)
                                    record_closed_trade(symbol, pnl_pct, pnl_usdc, reason)
                                    log_event(f"🎯 Trailing TP aktivovaný pre {symbol} | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                                    send_telegram_msg(f"🎯 <b>TRAILING TP UZAMKNUTÝ</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                                except Exception:
                                    pass
                                del ACTIVE_TRADES[coin]
                                continue

                    if current_price <= trade["current_sl_price"]:
                        reason = "Stop Loss zasiahol"
                        try:
                            fmt_sz = format_amount(trade["amount"], trade.get("lot_sz", 0.01), trade.get("min_sz", 0.01))
                            if fmt_sz:
                                order_body = json.dumps({
                                    "instId": symbol, "tdMode": "cash", "side": "sell",
                                    "ordType": "market", "sz": fmt_sz
                                })
                                okx_request("POST", "/api/v5/trade/order", order_body)
                            record_closed_trade(symbol, pnl_pct, pnl_usdc, reason)
                            log_event(f"🛑 Stop Loss aktivovaný pre {symbol} | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                            send_telegram_msg(f"🛑 <b>STOP LOSS AKTIVOVANÝ</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                        except Exception:
                            pass
                        del ACTIVE_TRADES[coin]
                        continue

        except Exception as e:
            log_event(f"⚠️ Chyba v risk monitore: {e}")
        await asyncio.sleep(2)

# --- WEB SERVER & ROZHRANIE ---

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OKX Autonómny Scalper Bot (Fibo 0.618)</title>
    <style>
        :root {
            --bg-color: #0d1117;
            --card-bg: #161b22;
            --border-color: #30363d;
            --text-color: #c9d1d9;
            --accent-green: #238636;
            --accent-red: #da3633;
            --accent-blue: #1f6feb;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            margin: 0;
            padding: 20px;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); padding-bottom: 15px; margin-bottom: 20px; }
        h1 { margin: 0; font-size: 24px; color: #ffffff; }
        .status-badge { background: var(--accent-blue); color: white; padding: 6px 12px; border-radius: 6px; font-weight: bold; font-size: 14px; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        @media(max-width: 900px) { .grid { grid-template-columns: 1fr; } }
        .card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 8px; padding: 20px; margin-bottom: 20px; }
        .card h2 { margin-top: 0; font-size: 18px; border-bottom: 1px solid var(--border-color); padding-bottom: 10px; color: #ffffff; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid var(--border-color); font-size: 13px; }
        th { color: #8b949e; }
        .btn { background: var(--accent-blue); color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-weight: bold; }
        .btn:hover { opacity: 0.9; }
        .btn-red { background: var(--accent-red); }
        .btn-green { background: var(--accent-green); }
        .logs { background: #010409; border: 1px solid var(--border-color); border-radius: 6px; padding: 10px; height: 250px; overflow-y: auto; font-family: monospace; font-size: 12px; color: #7ee787; }
        .positive { color: #3fb950; font-weight: bold; }
        .negative { color: #f85149; font-weight: bold; }
        .settings-form select, .settings-form input { background: var(--bg-color); border: 1px solid var(--border-color); color: var(--text-color); padding: 6px; border-radius: 4px; width: 100%; box-sizing: border-box; margin-bottom: 10px; }
        .settings-form label { font-size: 12px; color: #8b949e; display: block; margin-bottom: 4px; }
    </style>
</head>
<body>
<div class="container">
    <header>
        <h1>🤖 OKX Autonómny Scalper Bot (Fibo 0.618 + 100% Sync)</h1>
        <div id="status-badge" class="status-badge">Načítavam...</div>
    </header>

    <div class="grid">
        <!-- ĽAVÝ STĹPEC -->
        <div>
            <!-- AKTÍVNE POZÍCIE -->
            <div class="card">
                <h2>📊 Aktívne pozície v obchode</h2>
                <div id="active-trades-container">Načítavam...</div>
            </div>

            <!-- KOMPLETNÝ SKENER TRHOV -->
            <div class="card">
                <h2>🔎 Skener top 20 USDC trhov (Fibo 0.618)</h2>
                <div style="overflow-x: auto;">
                    <table>
                        <thead>
                            <tr>
                                <th>Pár</th>
                                <th>Cena</th>
                                <th>Oscilátor</th>
                                <th>Momentum</th>
                                <th>Impulz</th>
                                <th>Signál</th>
                                <th>Exekúcia</th>
                            </tr>
                        </thead>
                        <tbody id="market-scanner-body">
                            <tr><td colspan="7">Načítavam trhy...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- PRAVÝ STĹPEC -->
        <div>
            <!-- OVLÁDANIE A NASTAVENIA -->
            <div class="card">
                <h2>⚙️ Ovládanie a Parametre [TF: <span id="current-tf-label">15M</span>]</h2>
                <div style="display: flex; gap: 10px; margin-bottom: 15px;">
                    <button id="toggle-bot-btn" class="btn" onclick="toggleBot()">Prepnúť Bot</button>
                    <button class="btn btn-red" onclick="sellAll()">Zavrieť Všetko</button>
                </div>
                <div class="settings-form">
                    <label>Časový rámec (TF):</label>
                    <select id="tf-select" onchange="changeTimeframe()">
                        <option value="1m">1m</option>
                        <option value="5m">5m</option>
                        <option value="8m">8m</option>
                        <option value="15m" selected>15m</option>
                        <option value="30m">30m</option>
                        <option value="45m">45m</option>
                        <option value="1h">1h</option>
                        <option value="2h">2h</option>
                        <option value="3h">3h</option>
                        <option value="4h">4h</option>
                    </select>
                    
                    <label>Max paralelné obchody:</label>
                    <input type="number" id="max-parallel" value="1" min="1" max="5">
                    
                    <label>Max rozpočet na obchod (USDC):</label>
                    <input type="number" id="max-budget" value="1000.0" min="10" max="10000">

                    <label>Počet sviečok pre HA Exit (consecutive):</label>
                    <input type="number" id="ha-exit" value="3" min="1" max="10">

                    <button class="btn btn-green" style="width: 100%; margin-top: 10px;" onclick="saveSettings()">Uložiť nastavenia</button>
                </div>
            </div>

            <!-- HISTÓRIA A ŠTATISTIKY -->
            <div class="card">
                <h2>📈 Štatistiky a História</h2>
                <div id="stats-container" style="margin-bottom: 15px; font-size: 14px; line-height: 1.6;">
                    Celkovo obchodov: <b>0</b> | Úspešnosť (Win Rate): <b>0.0%</b> | Celkový PnL: <b>0.00 USDC</b>
                </div>
                <div style="overflow-x: auto; max-height: 250px;">
                    <table>
                        <thead>
                            <tr>
                                <th>Čas</th>
                                <th>Pár</th>
                                <th>PnL %</th>
                                <th>PnL $</th>
                                <th>Dôvod</th>
                            </tr>
                        </thead>
                        <tbody id="history-body">
                            <tr><td colspan="5">Zatiaľ žiadne ukončené obchody.</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- SYSTÉMOVÉ LOGY -->
            <div class="card">
                <h2>📜 Systémové logy</h2>
                <div id="logs-container" class="logs">Načítavam logy...</div>
            </div>
        </div>
    </div>
</div>

<script>
async function fetchState() {
    try {
        let res = await fetch('/api/state');
        let data = await res.json();
        
        document.getElementById('status-badge').innerText = data.current_stage;
        document.getElementById('current-tf-label').innerText = data.current_timeframe.toUpperCase();
        let botBtn = document.getElementById('toggle-bot-btn');
        botBtn.innerText = data.is_bot_active ? "⏸️ Pozastaviť Bota" : "▶️ Spustiť Bota";
        botBtn.className = data.is_bot_active ? "btn btn-red" : "btn btn-green";

        let tradesHtml = "";
        if (Object.keys(data.active_trades).length === 0) {
            tradesHtml = "<p>Žiadne aktívne pozície.</p>";
        } else {
            tradesHtml = "<table><thead><tr><th>Pár</th><th>Vstup</th><th>Aktuálna</th><th>Stop Loss</th><th>PnL</th></tr></thead><tbody>";
            for (let coin in data.active_trades) {
                let t = data.active_trades[coin];
                let pnlClass = t.live_pnl_pct >= 0 ? "positive" : "negative";
                tradesHtml += `<tr>
                    <td><b>${coin}</b></td>
                    <td>${t.entry_price.toFixed(4)}</td>
                    <td>${t.current_price.toFixed(4)}</td>
                    <td>${t.current_sl_price.toFixed(4)}</td>
                    <td class="${pnlClass}">${t.live_pnl_pct >= 0 ? '+' : ''}${t.live_pnl_pct.toFixed(2)}% (${t.live_pnl_usdc >= 0 ? '+' : ''}${t.live_pnl_usdc.toFixed(2)} $)</td>
                </tr>`;
            }
            tradesHtml += "</tbody></table>";
        }
        document.getElementById('active-trades-container').innerHTML = tradesHtml;

        let scannerHtml = "";
        let scannerData = data.market_scanner;
        for (let coin in scannerData) {
            let m = scannerData[coin];
            let sigColor = m.is_green ? "positive" : "";
            scannerHtml += `<tr>
                <td><b>${coin}</b></td>
                <td>${m.price.toFixed(4)}</td>
                <td>${m.oscillator}</td>
                <td>${m.momentum}</td>
                <td>${m.impulse}</td>
                <td class="${sigColor}">${m.signal}</td>
                <td><b>${m.exec_signal}</b></td>
            </tr>`;
        }
        document.getElementById('market-scanner-body').innerHTML = scannerHtml;

        let st = data.stats;
        document.getElementById('stats-container').innerHTML = `Celkovo obchodov: <b>${st.total_trades}</b> (Výhry: <span class="positive">${st.wins}</span> | Straty: <span class="negative">${st.losses}</span>) | Úspešnosť: <b>${st.win_rate}%</b> | Celkový PnL: <b class="${st.total_pnl >= 0 ? 'positive' : 'negative'}">${st.total_pnl >= 0 ? '+' : ''}${st.total_pnl} USDC</b>`;

        let histHtml = "";
        if (data.history.length === 0) {
            histHtml = "<tr><td colspan='5'>Zatiaľ žiadne ukončené obchody.</td></tr>";
        } else {
            data.history.slice(0, 15).forEach(h => {
                let pnlCl = h.pnl_usdc >= 0 ? "positive" : "negative";
                histHtml += `<tr>
                    <td>${h.time.substring(11, 19)}</td>
                    <td><b>${h.pair}</b></td>
                    <td class="${pnlCl}">${h.pnl_pct >= 0 ? '+' : ''}${h.pnl_pct}%</td>
                    <td class="${pnlCl}">${h.pnl_usdc >= 0 ? '+' : ''}${h.pnl_usdc} $</td>
                    <td>${h.reason}</td>
                </tr>`;
            });
        }
        document.getElementById('history-body').innerHTML = histHtml;

        document.getElementById('logs-container').innerHTML = data.logs.join('<br>');

    } catch (err) {
        console.error("Chyba pri načítaní stavu:", err);
    }
}

async function toggleBot() {
    let res = await fetch('/api/toggle-bot', { method: 'POST' });
    let data = await res.json();
    fetchState();
}

async function sellAll() {
    if (!confirm("Naozaj chcete okamžite uzavrieť všetky aktívne pozície na trhu?")) return;
    let res = await fetch('/api/sell-all', { method: 'POST' });
    let data = await res.json();
    alert(data.message);
    fetchState();
}

async function changeTimeframe() {
    let tf = document.getElementById('tf-select').value;
    let res = await fetch('/api/set-timeframe', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({timeframe: tf})
    });
    let data = await res.json();
    alert(data.message);
    fetchState();
}

async function saveSettings() {
    let maxParallel = parseInt(document.getElementById('max-parallel').value);
    let maxBudget = parseFloat(document.getElementById('max-budget').value);
    let haExit = parseInt(document.getElementById('ha-exit').value);

    let res = await fetch('/api/settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({max_parallel: maxParallel, max_budget_cap: maxBudget, ha_exit_consecutive: haExit})
    });
    let data = await res.json();
    alert("Nastavenia úspešne uložené!");
    fetchState();
}

setInterval(fetchState, 3000);
fetchState();
</script>
</body>
</html>
"""

app = FastAPI(title="OKX Autonómny Scalper Bot", version="4.0")

@app.on_event("startup")
async def startup_event():
    load_persistent_data()
    update_top_liquid_symbols()
    asyncio.create_task(autonomous_scanner_loop())
    asyncio.create_task(advanced_risk_monitor_loop())
    asyncio.create_task(top_symbols_updater_loop())
    log_event("🚀 Web server a autonómny bot úspešne naštartované.")
    
    # Automatická testovacia správa do Telegramu pri štarte na overenie spojenia
    send_telegram_msg("🤖 <b>OKX Scalper Bot úspešne naštartovaný a pripojený!</b>\nPripravený na autonómne obchodovanie.")

@app.get("/", response_class=HTMLResponse)
async def index_get():
    return HTMLTemplate

@app.get("/api/state")
async def api_state():
    return JSONResponse({
        "is_bot_active": IS_BOT_ACTIVE,
        "current_timeframe": CURRENT_TIMEFRAME,
        "current_stage": CURRENT_STAGE,
        "active_trades": ACTIVE_TRADES,
        "market_scanner": MARKET_SCANNER_CACHE,
        "stats": calculate_stats(),
        "history": CLOSED_TRADES_HISTORY,
        "logs": SYSTEM_LOGS[:40]
    })

@app.post("/api/toggle-bot")
async def api_toggle_bot():
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = not IS_BOT_ACTIVE
    status_str = "spustený" if IS_BOT_ACTIVE else "pozastavený"
    log_event(f"⚙️ Bot bol manuálne {status_str}.")
    return {"status": "success", "is_bot_active": IS_BOT_ACTIVE}

@app.post("/api/sell-all")
async def api_sell_all():
    global ACTIVE_TRADES
    async with trade_lock:
        closed_count = 0
        for coin in list(ACTIVE_TRADES.keys()):
            trade = ACTIVE_TRADES[coin]
            symbol = trade["market_pair"]
            try:
                fmt_sz = format_amount(trade["amount"], trade.get("lot_sz", 0.01), trade.get("min_sz", 0.01))
                if fmt_sz:
                    order_body = json.dumps({
                        "instId": symbol, "tdMode": "cash", "side": "sell",
                        "ordType": "market", "sz": fmt_sz
                    })
                    okx_request("POST", "/api/v5/trade/order", order_body)
                pnl_pct = trade.get("live_pnl_pct", 0.0)
                pnl_usdc = trade.get("live_pnl_usdc", 0.0)
                record_closed_trade(symbol, pnl_pct, pnl_usdc, "Manuálny núdzový predaj")
                closed_count += 1
            except Exception:
                pass
            del ACTIVE_TRADES[coin]
        log_event(f"🚨 Núdzový predaj: Uzavretých {closed_count} pozícií.")
    return {"status": "success", "message": f"Úspešne zatvorených {closed_count} pozícií."}

@app.post("/api/set-timeframe")
async def api_set_timeframe(request: Request):
    global CURRENT_TIMEFRAME, SETTINGS
    data = await request.json()
    tf = data.get("timeframe", "").lower()
    if tf in TIMEFRAME_PROFILES:
        CURRENT_TIMEFRAME = tf
        profile = TIMEFRAME_PROFILES[tf]
        SETTINGS.update(profile)
        log_event(f"⏱️ Časový rámec zmenený na {tf.upper()}. Profil: {profile.get('desc')}")
        return {"status": "success", "message": f"Časový rámec úspešne zmenený na {tf.upper()}"}
    return {"status": "error", "message": "Neplatný časový rámec"}

@app.post("/api/settings")
async def api_settings(request: Request):
    global SETTINGS
    data = await request.json()
    if "max_parallel" in data:
        SETTINGS["max_parallel"] = int(data["max_parallel"])
    if "max_budget_cap" in data:
        SETTINGS["max_budget_cap"] = float(data["max_budget_cap"])
    if "ha_exit_consecutive" in data:
        SETTINGS["ha_exit_consecutive"] = int(data["ha_exit_consecutive"])
    log_event(f"⚙️ Parametre bota aktualizované: Max paralelné: {SETTINGS.get('max_parallel')}, Rozpočet: {SETTINGS.get('max_budget_cap')}, HA Exit: {SETTINGS.get('ha_exit_consecutive')}")
    return {"status": "success"}

# --- ROZŠÍRENÝ TELEGRAM WEBHOOK PRE PRÍKAZY A TLAČIDLÁ ---

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    try:
        body = await request.json()
        
        # Spracovanie stlačení tlačidiel pod správou
        if "callback_query" in body:
            cq = body["callback_query"]
            data = cq.get("data")
            if data == "sell_now":
                global ACTIVE_TRADES
                async with trade_lock:
                    for coin in list(ACTIVE_TRADES.keys()):
                        trade = ACTIVE_TRADES[coin]
                        symbol = trade["market_pair"]
                        try:
                            fmt_sz = format_amount(trade["amount"], trade.get("lot_sz", 0.01), trade.get("min_sz", 0.01))
                            if fmt_sz:
                                order_body = json.dumps({
                                    "instId": symbol, "tdMode": "cash", "side": "sell",
                                    "ordType": "market", "sz": fmt_sz
                                })
                                okx_request("POST", "/api/v5/trade/order", order_body)
                            record_closed_trade(symbol, trade.get("live_pnl_pct", 0), trade.get("live_pnl_usdc", 0), "Telegram Tlačidlo")
                        except Exception:
                            pass
                        del ACTIVE_TRADES[coin]
                send_telegram_msg("🚨 Všetky aktívne pozície boli zatvorené cez Telegram tlačidlo!")
            elif data == "toggle_bot":
                global IS_BOT_ACTIVE
                IS_BOT_ACTIVE = not IS_BOT_ACTIVE
                status_str = "spustený" if IS_BOT_ACTIVE else "pozastavený"
                send_telegram_msg(f"⚙️ Bot bol prepnutý. Nový stav: <b>{status_str.upper()}</b>")

        # Spracovanie príkazov napísaných priamo do chatu (/start, /status, /stop)
        elif "message" in body:
            msg = body["message"]
            text = msg.get("text", "").strip().lower()
            chat_id = msg["chat"]["id"]
            
            if text == "/start":
                send_telegram_msg(
                    "🤖 <b>OKX Scalper Bot je online!</b>\n\n"
                    "Dostupné príkazy:\n"
                    "• /status - aktuálny stav a pozície\n"
                    "• /stop - pozastavenie bota",
                    reply_markup={"inline_keyboard": [[{"text": "⏸️ Prepnúť Bot", "callback_data": "toggle_bot"}, {"text": "🛑 Zatvoriť Všetko", "callback_data": "sell_now"}]]}
                )
            elif text == "/status":
                stats = calculate_stats()
                active_count = len(ACTIVE_TRADES)
                send_telegram_msg(
                    f"📊 <b>STATUS BOTA</b>\n"
                    f"• Aktívny stav: {'🟢 Zapnutý' if IS_BOT_ACTIVE else '🔴 Vypnutý'}\n"
                    f"• Časový rámec: {CURRENT_TIMEFRAME.upper()}\n"
                    f"• Aktívne pozície: {active_count}\n"
                    f"• Celkový PnL: {stats['total_pnl']} USDC (Win Rate: {stats['win_rate']}%)"
                )
            elif text == "/stop":
                IS_BOT_ACTIVE = False
                send_telegram_msg("⏸️ Bot bol príkazom z Telegramu pozastavený.")

    except Exception as e:
        print(f"Chyba v telegram webhooku: {e}")
    return {"ok": True}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
