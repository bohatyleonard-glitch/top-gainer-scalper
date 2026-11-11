import asyncio
import base64
import hmac
import json
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Template
import requests
import uvicorn

# --- 🔑 OKX KĽÚČE & BEZPEČNOSŤ ---
OKX_API_KEY = os.getenv("OKX_API_KEY", "54ebe9fe-1fd0-443d-8f76-0cd1a3439ddb")
OKX_SECRET = os.getenv("OKX_SECRET", "FBD5C8332D88E3ED5662470CA9C3E813")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "Novyzeland111/")

# --- 📱 TELEGRAM NOTIFIKÁCIE ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9021881475:AAFn1oQSvaGkhpX2uyJscdcTybFgk1tNmV8")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "9130979784")

TIMEFRAME_PROFILES = {
    "15m": {"trail_pct": 0.5, "be_trigger_pct": 0.5, "ttp_trigger_pct": 0.75, "ttp_callback_pct": 0.2, "target_pct": 0.75, "desc": "Rýchly 15m scalp (+0.75%)"},
    "30m": {"trail_pct": 1.0, "be_trigger_pct": 0.8, "ttp_trigger_pct": 1.2, "ttp_callback_pct": 0.3, "target_pct": 1.2, "desc": "30m trendový"}
}

ACTIVE_TRADES = {}
DAILY_PNL_CALENDAR = []
CLOSED_TRADES_HISTORY = []
SYSTEM_LOGS = []
CURRENT_STAGE = "STAV: Mimo obchodu. Čakám na autonómny signál..."
LAST_TRADE_TIME = {}
MAX_PARALLEL_TRADES = 1
trade_lock = asyncio.Lock()

IS_BOT_ACTIVE = True
CURRENT_TIMEFRAME = "15M"
STORAGE_FILE = "trades_history.json"

TOP_LIQUID_SYMBOLS = ["SOL-USDC", "SUI-USDC", "XRP-USDC", "ADA-USDC", "AVAX-USDC", "LINK-USDC", "DOGE-USDC", "PEPE-USDC", "BNB-USDC", "NEAR-USDC"]

SETTINGS = TIMEFRAME_PROFILES["15m"].copy()
SETTINGS.update({
    "use_btc_filter": True, 
    "reentry_cooldown_sec": 120, 
    "use_breakeven": True,
    "max_budget_cap": 1000.0,
    "compounding_enabled": False
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
    return 12000.0

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
                if vol > 0:
                    valid.append((inst_id, vol))
            valid.sort(key=lambda x: x[1], reverse=True)
            top_20 = [item[0] for item in valid[:20]]
            if top_20:
                TOP_LIQUID_SYMBOLS = top_20
    except Exception as e:
        log_event(f"⚠️ Chyba pri aktualizácii top symbolov: {e}")

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
        ha_closes, ha_opens, ha_highs, ha_lows, volumes = [], [], [], [], []
        for i, c in enumerate(candles):
            o, h, l, cl, v = safe_float(c[1]), safe_float(c[2]), safe_float(c[3]), safe_float(c[4]), safe_float(c[5])
            ha_cl = (o + h + l + cl) / 4.0
            ha_op = (o + cl) / 2.0 if i == 0 else (ha_opens[-1] + ha_closes[-1]) / 2.0
            ha_hi = max(h, ha_op, ha_cl)
            ha_lo = min(l, ha_op, ha_cl)
            ha_closes.append(ha_cl)
            ha_opens.append(ha_op)
            ha_highs.append(ha_hi)
            ha_lows.append(ha_lo)
            volumes.append(v)
        vol_dirs = [v if ha_closes[i] > ha_opens[i] else -v for i, v in enumerate(volumes)]
        fast_ma = calc_ema(vol_dirs, 5)
        slow_ma = calc_ema(vol_dirs, 20)
        oscillator = [f - s for f, s in zip(fast_ma, slow_ma)]
        signal_line = calc_ema(oscillator, 9)
        close_ema_9 = calc_ema(ha_closes, 9)
        curr_osc, prev_osc = oscillator[-1], oscillator[-2]
        curr_sig, prev_sig = signal_line[-1], signal_line[-2]
        cond_vol_bullish = curr_osc > 0 and curr_osc > curr_sig
        cond_vol_crossover = (prev_osc <= prev_sig) and (curr_osc > curr_sig) and (curr_osc > 0)
        curr_close, prev_close = ha_closes[-1], ha_closes[-2]
        curr_ema9, prev_ema9 = close_ema_9[-1], close_ema_9[-2]
        close_crossover_ema9 = (prev_close <= prev_ema9) and (curr_close > curr_ema9)
        return cond_vol_bullish and (cond_vol_crossover or close_crossover_ema9)
    except Exception:
        return False

async def autonomous_scanner_loop():
    await asyncio.sleep(5)
    log_event("🤖 Autonómny Pine Script skener (All Green) úspešne spustený!")
    while True:
        try:
            if IS_BOT_ACTIVE and len(ACTIVE_TRADES) < MAX_PARALLEL_TRADES:
                if check_btc_health():
                    tf_str = CURRENT_TIMEFRAME.lower()
                    for symbol in TOP_LIQUID_SYMBOLS:
                        coin_name = symbol.split("-")[0]
                        now_ts = datetime.now().timestamp()
                        if coin_name in ACTIVE_TRADES or (coin_name in LAST_TRADE_TIME and (now_ts - LAST_TRADE_TIME[coin_name] < SETTINGS["reentry_cooldown_sec"])):
                            continue
                        if evaluate_pine_strategy(symbol, bar_tf=tf_str):
                            log_event(f"🚀 PINE SCRIPT 'ALL GREEN' SIGNÁL DETEGOVANÝ PRE {symbol}!")
                            await execute_autonomous_buy(symbol)
                            break
        except Exception as e:
            log_event(f"⚠️ Chyba v autonómnom skeneri: {e}")
        await asyncio.sleep(20)

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
        if coin_name in ACTIVE_TRADES or len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES:
            return
        LAST_TRADE_TIME[coin_name] = now_ts
        free_capital = fetch_account_capital("USDC")
        budget_cap = SETTINGS.get("max_budget_cap", 1000.0)
        safe_budget = min(free_capital * 0.98, budget_cap)
        if safe_budget < 5.0:
            log_event(f"❌ Nedostatok USDC prostriedkov: {free_capital}")
            return
        ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={okx_symbol}")
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
        order_body = json.dumps({"instId": okx_symbol, "tdMode": "cash", "side": "buy", "ordType": "market", "sz": formatted_sz})
        order_res = okx_request("POST", "/api/v5/trade/order", order_body)
        if order_res.get("code") != "0":
            err_msg = order_res.get('msg', 'Neznáma chyba OKX')
            log_event(f"❌ Chyba nákupnej objednávky ({okx_symbol}): {err_msg}")
            send_telegram_msg(f"❌ <b>Chyba nákupu ({okx_symbol}):</b> {err_msg}")
            return
        coin_amount = float(formatted_sz)
        actual_spent_usdc = coin_amount * current_price
        sl_price = current_price * (1 - 0.006)
        be_price = current_price * (1 + (SETTINGS["be_trigger_pct"] / 100.0))
        ttp_price = current_price * (1 + (SETTINGS["ttp_trigger_pct"] / 100.0))
        target_pct = SETTINGS.get("target_pct", 0.75)
        
        ACTIVE_TRADES[coin_name] = {
            "entry_price": current_price, "current_price": current_price, "current_sl_price": sl_price,
            "be_target_price": be_price, "ttp_target_price": ttp_price, "amount": coin_amount,
            "initial_amount": coin_amount, "lot_sz": lot_sz, "min_sz": min_sz, "bought_at": get_sk_time_str(),
            "market_pair": okx_symbol, "highest_price": current_price, "partial_sold": False,
            "break_even_activated": False, "ttp_activated": False, "invested_usdc": actual_spent_usdc,
            "live_pnl_pct": 0.0, "live_pnl_usdc": 0.0, "second_half_pnl_usdc": 0.0, "tf": CURRENT_TIMEFRAME, "settings": SETTINGS.copy()
        }
        log_event(f"🛒 Autonómny nákup [{CURRENT_TIMEFRAME}]: {okx_symbol} za {current_price:.4f} USDC | Target: +{target_pct}% (BE pri +0.5%)")
        send_telegram_msg(f"🟢 <b>AUTONÓMNY NÁKUP [TF: {CURRENT_TIMEFRAME}]</b>\n<b>Pár:</b> {okx_symbol}\n<b>Vstup:</b> {current_price:.4f}\n<b>Investícia:</b> {actual_spent_usdc:.2f} USDC")

async def advanced_risk_monitor_loop():
    global ACTIVE_TRADES, CURRENT_STAGE
    await asyncio.sleep(3)
    while True:
        if not ACTIVE_TRADES:
            if IS_BOT_ACTIVE:
                CURRENT_STAGE = f"STAV: Autonómny Bot aktívny. Skenujem Top {len(TOP_LIQUID_SYMBOLS)} altcoinov cez Pine Script..."
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
                current_price = safe_float(ticker_res["data"][0]["last"]) if ticker_res.get("code") == "0" and ticker_res.get("data") else trade["entry_price"]
                trade["current_price"] = current_price
                
                if current_price > trade["highest_price"]:
                    trade["highest_price"] = current_price
                
                pnl_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
                invested = trade.get("invested_usdc", trade["amount"] * trade["entry_price"])
                pnl_usdc = invested * (pnl_pct / 100.0)
                target_pct = trade_settings.get("target_pct", 0.75)
                be_trigger = trade_settings.get("be_trigger_pct", 0.5)
                
                if not trade.get("partial_sold", False) and pnl_pct >= target_pct:
                    half_raw = trade["initial_amount"] / 2.0
                    half_sz = format_amount(half_raw, trade["lot_sz"], trade["min_sz"])
                    if half_sz:
                        part_body = json.dumps({"instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": half_sz})
                        res_part = okx_request("POST", "/api/v5/trade/order", part_body)
                        if res_part.get("code") == "0":
                            trade["amount"] = round(trade["amount"] - float(half_sz), 6)
                            trade["partial_sold"] = True
                            record_closed_trade(symbol, pnl_pct, (invested / 2.0) * (pnl_pct / 100.0), f"Scaling Out 50% (+{target_pct}%)")
                            log_event(f"🎯 Scaling Out 50% pre {symbol} pri {pnl_pct:+.2f}% | Zisk uzamknutý!")
                
                if pnl_pct >= be_trigger:
                    trade["break_even_activated"] = True
                    effective_sl = trade["entry_price"]
                else:
                    effective_sl = trade["entry_profile"] if "entry_profile" in trade else trade["entry_price"] * (1 - 0.006)

                trade["live_pnl_pct"] = pnl_pct
                trade["live_pnl_usdc"] = pnl_usdc
                CURRENT_STAGE = f"STAV: {symbol} | PnL: {pnl_pct:+.2f}% | BE: {'AKTÍVNY' if trade['break_even_activated'] else 'SL -0.6%'}"

                if current_price <= effective_sl:
                    reason = "Break-Even Ochrana (0%)" if trade["break_even_activated"] else "Stop-Loss (-0.6%)"
                    try:
                        fmt_sz = format_amount(trade["amount"], trade["lot_sz"], trade["min_sz"])
                        if fmt_sz:
                            order_body = json.dumps({"instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": fmt_sz})
                            okx_request("POST", "/api/v5/trade/order", order_body)
                        record_closed_trade(symbol, pnl_pct, pnl_usdc, reason)
                        log_event(f"🚨 Uzavretý zvyšok ({reason}) pre {symbol} | PnL: {pnl_pct:+.2f}%")
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
    log_event("🚀 Autonómny Apex Scalper PRO v5.7 spustený.")
    monitor_task = asyncio.create_task(advanced_risk_monitor_loop())
    scanner_task = asyncio.create_task(autonomous_scanner_loop())
    updater_task = asyncio.create_task(top_symbols_updater_loop())
    yield
    monitor_task.cancel()
    scanner_task.cancel()
    updater_task.cancel()

app = FastAPI(lifespan=lifespan)

HTML_TEMPLATE_CONTENT = """<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <title>Apex Scalper PRO v5.7 Terminal</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root { --bg-color: #08090c; --card-bg: #111318; --border-color: #1f232b; --text-main: #eceff4; --text-muted: #6c788e; --green: #10b981; --red: #ef4444; --yellow: #f59e0b; }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'JetBrains Mono', monospace; }
        body { background-color: var(--bg-color); color: var(--text-main); padding: 16px; min-height: 100vh; display: flex; flex-direction: column; }
        .container { max-width: 1250px; width: 100%; margin: 0 auto; display: flex; flex-direction: column; gap: 14px; }
        .top-bar { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); padding-bottom: 12px; }
        .brand h1 { font-size: 1.05rem; font-weight: 700; color: #fff; }
        .brand p { font-size: 0.7rem; color: var(--text-muted); margin-top: 3px; }
        .balance-tag { color: var(--yellow); font-weight: 700; }
        .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
        .stat-card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 5px; padding: 12px; }
        .stat-title { font-size: 0.65rem; color: var(--text-muted); text-transform: uppercase; }
        .stat-value { font-size: 1.15rem; font-weight: 700; margin-top: 4px; color: #fff; }
        .terminal-card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 5px; overflow: hidden; }
        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.75rem; }
        th { color: var(--text-muted); font-weight: 500; padding: 9px 12px; border-bottom: 1px solid var(--border-color); background: var(--card-bg); }
        td { padding: 10px 12px; border-bottom: 1px solid var(--border-color); color: var(--text-main); }
        .logs-box { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 5px; padding: 10px; height: 180px; overflow-y: auto; font-size: 0.7rem; color: var(--text-muted); display: flex; flex-direction: column; gap: 4px; }
        .text-green { color: var(--green); font-weight: 700; }
        .text-red { color: var(--red); font-weight: 700; }
    </style>
</head>
<body>
    <div class="container">
        <div class="top-bar">
            <div class="brand">
                <h1>APEX SCALPER // AUTO-PILOT</h1>
                <p>Zostatok: <span id="header-balance" class="balance-tag">-- USDC</span> | Aktívny TF: <span class="balance-tag">15M</span></p>
            </div>
        </div>
        <div class="stats-grid">
            <div class="stat-card"><div class="stat-title">Win Rate</div><div class="stat-value" id="stat-winrate">0.0%</div></div>
            <div class="stat-card"><div class="stat-title">Total Trades</div><div class="stat-value" id="stat-trades">0</div></div>
            <div class="stat-card"><div class="stat-title">Win / Loss</div><div class="stat-value" id="stat-winloss">0W / 0L</div></div>
            <div class="stat-card"><div class="stat-title">Net PnL</div><div class="stat-value" id="stat-pnl">+0.00 $</div></div>
        </div>
        <div class="terminal-card">
            <table>
                <thead><tr><th>Pár</th><th>Vstup</th><th>Aktuálna cena</th><th>Stav / PnL</th></tr></thead>
                <tbody id="active-position-tbody"><tr><td colspan="4" style="text-align: center; color: var(--text-muted);">Skenujem trh pre All Green signály...</td></tr></tbody>
            </table>
        </div>
        <div class="logs-box" id="logs-container"><div class="log-line">[System Initialized] Pripravené.</div></div>
    </div>
    <script>
        async function updateState() {
            try {
                let res = await fetch('/api/state');
                let data = await res.json();
                document.getElementById('header-balance').innerText = data.balance.toFixed(2) + " USDC";
                let stats = data.stats;
                document.getElementById('stat-winrate').innerText = stats.win_rate.toFixed(1) + "%";
                document.getElementById('stat-trades').innerText = stats.total_trades;
                document.getElementById('stat-winloss').innerText = stats.wins + "W / " + stats.losses + "L";
                let pnlEl = document.getElementById('stat-pnl');
                pnlEl.innerText = (stats.total_pnl >= 0 ? "+" : "") + stats.total_pnl.toFixed(2) + " $";
                
                let activeTbody = document.getElementById('active-position-tbody');
                if (Object.keys(data.active_trades).length > 0) {
                    let html = "";
                    for (let [coin, t] of Object.entries(data.active_trades)) {
                        html += `<tr><td><b>${t.market_pair}</b></td><td>${t.entry_price.toFixed(4)}</td><td>${t.current_price.toFixed(4)}</td><td class="${t.live_pnl_pct >= 0 ? 'text-green' : 'text-red'}">${t.live_pnl_pct.toFixed(2)}%</td></tr>`;
                    }
                    activeTbody.innerHTML = html;
                } else {
                    activeTbody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">Skenujem trh...</td></tr>`;
                }
                let logsBox = document.getElementById('logs-container');
                logsBox.innerHTML = data.logs.map(l => `<div class="log-line">${l}</div>`).join('');
            } catch (err) { console.error(err); }
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
    return t.render(balance=balance)

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
