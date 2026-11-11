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

# --- 🔑 OKX KĽÚČE & BEZPEČNOSŤ ---
OKX_API_KEY = os.getenv("OKX_API_KEY", "54ebe9fe-1fd0-443d-8f76-0cd1a3439ddb")
OKX_SECRET = os.getenv("OKX_SECRET", "FBD5C8332D88E3ED5662470CA9C3E813")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "Novyzeland111/")
WEBHOOK_SECRET = "moje_tajne_heslo_123"

# --- 📱 TELEGRAM NOTIFIKÁCIE & INTERAKTÍVNE BOT OVLÁDANIE ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9021881475:AAFn1oQSvaGkhpX2uyJscdcTybFgk1tNmV8")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "9130979784")

# --- 📊 DYNAMICKÉ PROFILY PRE ČASOVÉ RÁMCE ---
TIMEFRAME_PROFILES = {
    "1m": {"trail_pct": 0.5, "be_trigger_pct": 0.3, "ttp_trigger_pct": 0.6, "ttp_callback_pct": 0.15, "target_pct": 0.5, "desc": "Skalper 1m"},
    "5m": {"trail_pct": 0.0, "be_trigger_pct": 0.0, "ttp_trigger_pct": 0.0, "ttp_callback_pct": 0.0, "target_pct": 0.0, "desc": "Manuál 5m"},
    "8m": {"trail_pct": 0.0, "be_trigger_pct": 0.0, "ttp_trigger_pct": 0.0, "ttp_callback_pct": 0.0, "desc": "Manuál 8m"},
    "15m": {"trail_pct": 0.7, "be_trigger_pct": 0.6, "ttp_trigger_pct": 0.8, "ttp_callback_pct": 0.2, "target_pct": 0.75, "desc": "Rýchly 15m scalp (+0.75%)"},
    "30m": {"trail_pct": 1.0, "be_trigger_pct": 0.9, "ttp_trigger_pct": 1.2, "ttp_callback_pct": 0.3, "target_pct": 1.2, "desc": "30m trendový"},
    "45m": {"trail_pct": 1.5, "be_trigger_pct": 1.2, "ttp_trigger_pct": 1.8, "ttp_callback_pct": 0.4, "target_pct": 1.5, "desc": "45m strednodobý"},
    "1h": {"trail_pct": 2.0, "be_trigger_pct": 1.6, "ttp_trigger_pct": 2.5, "ttp_callback_pct": 0.5, "target_pct": 1.7, "desc": "1h hodinový swing"}
}

ACTIVE_TRADES = {}
DAILY_PNL_CALENDAR = []
CLOSED_TRADES_HISTORY = []
SYSTEM_LOGS = []
CURRENT_STAGE = "STAV: Mimo obchodu. Čakám na signál..."
LAST_TRADE_TIME = {}
MAX_PARALLEL_TRADES = 1
trade_lock = asyncio.Lock()

IS_BOT_ACTIVE = True
CURRENT_TIMEFRAME = "15M"
STORAGE_FILE = "trades_history.json"
ALLOWED_STABLES = ["USDT", "USDC"]  # Prioritne USDT pre OKX Demo

TOP_LIQUID_SYMBOLS = ["SOL-USDT", "SUI-USDT", "XRP-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT", "DOGE-USDT"]

SETTINGS = TIMEFRAME_PROFILES["15m"].copy()
SETTINGS.update({
    "use_btc_filter": True, 
    "reentry_cooldown_sec": 120, 
    "use_breakeven": True,
    "max_budget_cap": 1000.0,
    "compounding_enabled": False,
    "strategy_mode": "All Green"
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
        print(f"❌ Chyba Telegram notifikácie: {err}", flush=True)

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

def format_tv_ticker(raw_ticker: str):
    clean = raw_ticker.replace("OKX:", "").replace("/", "").replace("-", "").strip().upper()
    for stable in ALLOWED_STABLES:
        if clean.endswith(stable) and clean != stable:
            coin = clean[:-len(stable)]
            return coin, f"{coin}-{stable}"
    return clean, f"{clean}-USDT"

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

def fetch_account_capital(stable_ccy="USDT") -> float:
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
            for acc in balance_res.get("data", []):
                for detail in acc.get("details", []):
                    if detail.get("ccy") in ALLOWED_STABLES:
                        avail = safe_float(detail.get("availBal", 0))
                        if avail > 0:
                            return avail
    except Exception as err:
        log_event(f"⚠️ Chyba pri načítaní zostatku: {err}")
    return 10000.0

def get_instrument_specs(inst_id: str):
    """Zistí lotSz a presné pravidlá pre daný pár z OKX, aby nedochádzalo k 'All operations failed'."""
    try:
        res = okx_request("GET", f"/api/v5/public/instruments?instType=SPOT&instId={inst_id}")
        if res.get("code") == "0" and res.get("data"):
            item = res["data"][0]
            lot_sz = safe_float(item.get("lotSz", 1.0), 1.0)
            return lot_sz
    except Exception:
        pass
    return 1.0

def check_btc_health() -> bool:
    if not SETTINGS["use_btc_filter"]:
        return True
    try:
        url = "/api/v5/market/candles?instId=BTC-USDT&bar=15m&limit=10"
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

def calc_ema(values, length):
    if len(values) < length:
        return [values[-1]] * len(values)
    alpha = 2 / (length + 1)
    ema = [sum(values[:length]) / length]
    for price in values[length:]:
        ema.append(price * alpha + ema[-1] * (1 - alpha))
    return ema

def evaluate_pine_script_strategy(symbol: str, bar_tf: str = "15m") -> bool:
    try:
        url = f"/api/v5/market/candles?instId={symbol}&bar={bar_tf}&limit=50"
        res = okx_request("GET", url)
        if res.get("code") != "0" or not res.get("data"):
            return False
        
        raw_candles = res["data"]
        raw_candles.reverse()
        
        ha_closes, ha_opens, ha_highs, ha_lows, volumes = [], [], [], [], []
        
        for i, c in enumerate(raw_candles):
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
        
        all_green_cond = cond_vol_bullish and (cond_vol_crossover or close_crossover_ema9)
        breakout_cond = cond_vol_crossover or (cond_vol_bullish and close_crossover_ema9)
        
        return breakout_cond if SETTINGS.get("strategy_mode", "All Green") == "Breakout" else all_green_cond
    except Exception as e:
        log_event(f"⚠️ Chyba pri výpočte stratégie pre {symbol}: {e}")
        return False

def update_top_symbols():
    global TOP_LIQUID_SYMBOLS
    try:
        res = okx_request("GET", "/api/v5/market/tickers?instType=SPOT")
        if res.get("code") == "0" and res.get("data"):
            tickers = res["data"]
            valid_pairs = []
            for t in tickers:
                inst_id = t.get("instId", "")
                if not any(inst_id.endswith(f"-{s}") for s in ALLOWED_STABLES):
                    continue
                if "BTC" in inst_id or "ETH" in inst_id:
                    continue
                vol_24h = safe_float(t.get("volCcy24h", 0))
                if vol_24h > 0:
                    valid_pairs.append((inst_id, vol_24h))
            valid_pairs.sort(key=lambda x: x[1], reverse=True)
            top_20 = [pair[0] for pair in valid_pairs[:20]]
            if top_20:
                TOP_LIQUID_SYMBOLS = top_20
                log_event(f"🔍 Dynamický skener aktualizoval Top {len(TOP_LIQUID_SYMBOLS)} likvidných tokenov.")
    except Exception as e:
        log_event(f"⚠️ Chyba pri aktualizácii top symbolov: {e}")

async def autonomous_strategy_scanner_loop():
    await asyncio.sleep(5)
    log_event("🤖 Autonómny Pine Script skener spustený!")
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
                            
                        signal = evaluate_pine_script_strategy(symbol, bar_tf=tf_str)
                        if signal:
                            log_event(f"🚀 PINE SCRIPT SIGNÁL DETEGOVANÝ PRE {symbol}!")
                            await execute_trade_in_background({"secret": WEBHOOK_SECRET, "action": "buy", "symbol": symbol})
                            break
        except Exception as e:
            log_event(f"⚠️ Chyba v autonómnom skeneri: {e}")
        await asyncio.sleep(60)

async def top_symbols_scanner_loop():
    while True:
        try:
            update_top_symbols()
        except Exception:
            pass
        await asyncio.sleep(3600)

async def advanced_risk_monitor_loop():
    global ACTIVE_TRADES, CURRENT_STAGE
    await asyncio.sleep(3)
    while True:
        if not ACTIVE_TRADES:
            if IS_BOT_ACTIVE:
                CURRENT_STAGE = f"STAV: Autonómny Bot aktívny. Skenujem Top {len(TOP_LIQUID_SYMBOLS)} altcoinov cez Pine Script logiku (+0.75%)..."
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
                        order_body = json.dumps({
                            "instId": symbol, "tdMode": "cash", "side": "sell",
                            "ordType": "market", "sz": str(round(trade["amount"], 2))
                        })
                        okx_request("POST", "/api/v5/trade/order", order_body)
                        pnl_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
                        invested = trade.get("invested_usdc", trade["amount"] * trade["entry_price"])
                        pnl_usdc = invested * (pnl_pct / 100.0)
                        record_closed_trade(symbol, pnl_pct, pnl_usdc, "GTC Expirácia 48h")
                        log_event(f"⏳ Expirácia 48h pre {symbol} | PnL: {pnl_pct:+.2f}%")
                        send_telegram_msg(f"⏳ <b>GTC EXPIRÁCIA 48H</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
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
                    current_total_value = invested + pnl_usdc
                    
                    target_pct = trade_settings.get("target_pct", 0.0)
                    
                    if target_pct > 0:
                        if not trade.get("partial_sold", False) and pnl_pct >= target_pct:
                            half_amount = round(trade["initial_amount"] / 2.0, 2)
                            if half_amount >= 0.01:
                                part_body = json.dumps({
                                    "instId": symbol, "tdMode": "cash", "side": "sell",
                                    "ordType": "market", "sz": str(half_amount)
                                })
                                res_part = okx_request("POST", "/api/v5/trade/order", part_body)
                                if res_part.get("code") == "0":
                                    trade["amount"] = round(trade["amount"] - half_amount, 2)
                                    trade["partial_sold"] = True
                                    part_pnl_usdc = (invested / 2.0) * (pnl_pct / 100.0)
                                    record_closed_trade(symbol, pnl_pct, part_pnl_usdc, f"Scaling Out 50% (+{target_pct}%)")
                                    log_event(f"🎯 Scaling Out 50% pre {symbol} pri {pnl_pct:+.2f}% | Odpredaná polovica, zisk uzamknutý!")
                                    send_telegram_msg(f"🎯 <b>SCALING OUT 50% SPLNENÝ</b>\n<b>Pár:</b> {symbol}\n<b>Cieľ:</b> +{target_pct}%\n<b>PnL (50%):</b> +{part_pnl_usdc:.2f} $")

                    be_trigger = trade_settings.get("be_trigger_pct", 0.0)
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
                        trade["be_target_price"] = be_price
                        trade["current_sl_price"] = effective_sl
                        trade["ttp_target_price"] = ttp_stop_level
                        
                        second_half_invested = invested / 2.0 if trade.get("partial_sold") else invested
                        trade["second_half_pnl_usdc"] = second_half_invested * (pnl_pct / 100.0)
                        
                        partial_status_text = "50% ODPREDANÉ (TRAIL AKTÍVNY)" if trade.get("partial_sold") else f"ČAKÁ NA +{target_pct}%"
                        CURRENT_STAGE = f"STAV: {symbol} | Hodnota: {current_total_value:.2f}$ | PnL: {pnl_pct:+.2f}% | {partial_status_text}"
                        
                        if current_price <= effective_sl:
                            reason = "Break-Even Ochrana (Zvyšok)" if effective_sl >= trade["entry_price"] else "Stop-Loss (-0.6%)"
                            try:
                                order_body = json.dumps({
                                    "instId": symbol, "tdMode": "cash", "side": "sell",
                                    "ordType": "market", "sz": str(round(trade["amount"], 2))
                                })
                                okx_request("POST", "/api/v5/trade/order", order_body)
                                record_closed_trade(symbol, pnl_pct, pnl_usdc, reason)
                                log_event(f"🚨 Uzavretý zvyšok ({reason}) pre {symbol} | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                                send_telegram_msg(f"🚨 <b>UZAVRETÝ ZVYŠOK OBCHODU ({reason})</b>\n<b>Pár:</b> {symbol}\n<b>Celkový PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                            except Exception:
                                pass
                            del ACTIVE_TRADES[coin]
                            continue
                            
                        if trade.get("ttp_activated") and current_price <= ttp_stop_level:
                            reason = "Trailing Take Profit (Zvyšok)"
                            try:
                                order_body = json.dumps({
                                    "instId": symbol, "tdMode": "cash", "side": "sell",
                                    "ordType": "market", "sz": str(round(trade["amount"], 2))
                                })
                                okx_request("POST", "/api/v5/trade/order", order_body)
                                record_closed_trade(symbol, pnl_pct, pnl_usdc, reason)
                                log_event(f"🎯 Uzavretý zvyšok (Trailing TP) pre {symbol} | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                                send_telegram_msg(f"🎯 <b>UZAVRETÝ ZVYŠOK (TTP)</b>\n<b>Pár:</b> {symbol}\n<b>Celkový PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                            except Exception:
                                pass
                            del ACTIVE_TRADES[coin]
                            continue
                    else:
                        trade["live_pnl_pct"] = pnl_pct
                        trade["live_pnl_usdc"] = pnl_usdc
                        trade["second_half_pnl_usdc"] = pnl_usdc
                        CURRENT_STAGE = f"STAV [MANUÁLNY TF {trade['tf']}]: {symbol} | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f}$)"
        except Exception:
            pass
        await asyncio.sleep(2)

async def execute_trade_in_background(data: dict):
    global ACTIVE_TRADES, LAST_TRADE_TIME, CURRENT_TIMEFRAME, SETTINGS
    async with trade_lock:
        try:
            req_secret = str(data.get("secret", "")).strip()
            if req_secret != WEBHOOK_SECRET:
                log_event("⚠️ Ignorovaný nákup: Neplatné heslo.")
                return
            
            action = str(data.get("action", "")).lower().strip()
            incoming_tf = str(data.get("tf", "15m")).strip().lower()
            if incoming_tf in TIMEFRAME_PROFILES:
                CURRENT_TIMEFRAME = incoming_tf.upper()
                SETTINGS.update(TIMEFRAME_PROFILES[incoming_tf])
            
            tv_ticker = str(data.get("symbol", data.get("ticker", ""))).upper().strip()
            coin_name, okx_symbol = format_tv_ticker(tv_ticker)
            now_ts = datetime.now().timestamp()
            
            if action == "buy":
                if not IS_BOT_ACTIVE:
                    log_event("⚠️ Ignorovaný buy signal: Bot je pozastavený.")
                    return
                if coin_name in LAST_TRADE_TIME and (now_ts - LAST_TRADE_TIME[coin_name] < SETTINGS["reentry_cooldown_sec"]):
                    log_event(f"⚠️ Ignorovaný buy pre {coin_name}: 120s zamedzenie zacyklenia.")
                    return
                if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES or coin_name in ACTIVE_TRADES:
                    log_event("⚠️ Preskočený buy: Pozícia už je aktívna.")
                    return
                if not check_btc_health():
                    log_event("⚠️ Nákup blokovaný: BTC Trend Guard detegoval pokles.")
                    return
                
                LAST_TRADE_TIME[coin_name] = now_ts
                stable_ccy = "USDT" if "USDT" in okx_symbol else ("USDC" if "USDC" in okx_symbol else "USDT")
                
                free_capital = fetch_account_capital(stable_ccy)
                budget_cap = SETTINGS.get("max_budget_cap", 1000.0)
                
                safe_budget = min(free_capital * 0.98, budget_cap)
                log_event(f"⚙️ Rozpočet pre nákup: {safe_budget}$ (Zostatok: {free_capital:.2f} {stable_ccy})")

                if safe_budget < 5.0:
                    log_event(f"❌ Nedostatok prostriedkov pre nákup: {free_capital} {stable_ccy}")
                    return
                
                ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={okx_symbol}")
                if ticker_res.get("code") != "0" or not ticker_res.get("data"):
                    log_event(f"❌ Nepodarilo sa získať cenu pre {okx_symbol}")
                    return
                current_price = safe_float(ticker_res["data"][0]["last"])
                
                # Zistenie lotSz a správne zarovnanie množstva pre OKX
                lot_sz = get_instrument_specs(okx_symbol)
                raw_amount = safe_budget / current_price
                
                # Zarovnanie na násobky lotSz
                if lot_sz > 0:
                    coin_amount = math.floor(raw_amount / lot_sz) * lot_sz
                else:
                    coin_amount = raw_amount
                
                # Určenie počtu desatinných miest na základe lotSz
                if lot_sz < 1:
                    decimals = str(lot_sz).split(".")[1].find('1') + 1
                    coin_amount = round(coin_amount, decimals)
                else:
                    coin_amount = round(coin_amount, 2)

                if coin_amount <= 0:
                    coin_amount = lot_sz if lot_sz > 0 else 1.0

                order_body = json.dumps({
                    "instId": okx_symbol,
                    "tdMode": "cash",
                    "side": "buy",
                    "ordType": "market",
                    "sz": str(coin_amount)
                })
                
                order_res = okx_request("POST", "/api/v5/trade/order", order_body)
                if order_res.get("code") != "0":
                    err_msg = order_res.get('msg', 'Neznáma chyba OKX')
                    log_event(f"❌ Chyba nákupnej objednávky ({okx_symbol}): {err_msg}")
                    send_telegram_msg(f"❌ <b>Chyba nákupnej objednávky na OKX ({okx_symbol}):</b> {err_msg}")
                    return
                
                actual_spent_usdc = coin_amount * current_price
                sl_price = current_price * (1 - 0.006)
                be_price = current_price * (1 + (SETTINGS["be_trigger_pct"] / 100.0)) if SETTINGS.get("be_trigger_pct", 0) > 0 else 0
                ttp_price = current_price * (1 + (SETTINGS["ttp_trigger_pct"] / 100.0)) if SETTINGS.get("ttp_trigger_pct", 0) > 0 else 0
                target_pct = SETTINGS.get("target_pct", 0.0)
                
                ACTIVE_TRADES[coin_name] = {
                    "entry_price": current_price,
                    "current_price": current_price,
                    "current_sl_price": sl_price,
                    "be_target_price": be_price,
                    "ttp_target_price": ttp_price,
                    "amount": coin_amount,
                    "initial_amount": coin_amount,
                    "bought_at": get_sk_time_str(),
                    "market_pair": okx_symbol,
                    "highest_price": current_price,
                    "partial_sold": False,
                    "break_even_activated": False,
                    "ttp_activated": False,
                    "invested_usdc": actual_spent_usdc,
                    "live_pnl_pct": 0.0,
                    "live_pnl_usdc": 0.0,
                    "second_half_pnl_usdc": 0.0,
                    "tf": CURRENT_TIMEFRAME,
                    "settings": SETTINGS.copy()
                }
                target_str = f"+{target_pct}%" if target_pct > 0 else "MANUÁLNY (Bez automatických %)"
                log_event(f"🛒 Nová pozícia [{CURRENT_TIMEFRAME}]: Nákup {okx_symbol} za {current_price:.4f} | Target: {target_str} (Investícia: {actual_spent_usdc:.2f}$)")
                
                inline_keyboard = {
                    "inline_keyboard": [
                        [
                            {"text": "🛑 ZATVORIŤ VŠETKO", "callback_data": "sell_now"},
                            {"text": "⏸️ PAUZA BOTA", "callback_data": "toggle_bot"}
                        ]
                    ]
                }
                send_telegram_msg(
                    f"🟢 <b>NOVÝ AUTONÓMNY OBCHOD [TF: {CURRENT_TIMEFRAME}]</b>\n"
                    f"<b>Pár:</b> {okx_symbol}\n"
                    f"<b>Vstup:</b> {current_price:.4f}\n"
                    f"<b>Investícia:</b> {actual_spent_usdc:.2f}\n"
                    f"🎯 <b>Režim:</b> {target_str}",
                    reply_markup=inline_keyboard
                )
        except Exception as e:
            log_event(f"❌ Chyba pri exekúcii: {e}")

async def sell_now():
    global ACTIVE_TRADES
    async with trade_lock:
        for coin in list(ACTIVE_TRADES.keys()):
            trade = ACTIVE_TRADES[coin]
            symbol = trade["market_pair"]
            try:
                order_body = json.dumps({
                    "instId": symbol, "tdMode": "cash", "side": "sell",
                    "ordType": "market", "sz": str(trade["amount"])
                })
                okx_request("POST", "/api/v5/trade/order", order_body)
                pnl_pct = ((trade["current_price"] - trade["entry_price"]) / trade["entry_price"]) * 100
                invested = trade.get("invested_usdc", trade["amount"] * trade["entry_price"])
                pnl_usdc = invested * (pnl_pct / 100.0)
                record_closed_trade(symbol, pnl_pct, pnl_usdc, "Ručný predaj (Telegram)")
                log_event(f"⚡ Ručne zatvorená pozícia {symbol} | PnL: {pnl_pct:+.2f}%")
            except Exception as e:
                log_event(f"⚠️ Chyba pri ručnom predaji: {e}")
            del ACTIVE_TRADES[coin]

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_persistent_data()
    update_top_symbols()
    log_event("🚀 Apex Scalper PRO v5.8 FULL AUTO spustený (Bez TradingView).")
    monitor_task = asyncio.create_task(advanced_risk_monitor_loop())
    scanner_task = asyncio.create_task(top_symbols_scanner_loop())
    auto_strategy_task = asyncio.create_task(autonomous_strategy_scanner_loop())
    send_telegram_msg("🚀 <b>Apex Scalper PRO v5.8 AUTONÓMNY BEZ TRADINGVIEW pripravený!</b>")
    yield
    monitor_task.cancel()
    scanner_task.cancel()
    auto_strategy_task.cancel()

app = FastAPI(lifespan=lifespan)

@app.get("/api/state")
async def api_state():
    stats = calculate_stats()
    return JSONResponse({
        "bot_active": IS_BOT_ACTIVE,
        "current_stage": CURRENT_STAGE,
        "current_timeframe": CURRENT_TIMEFRAME,
        "active_trades": ACTIVE_TRADES,
        "stats": stats,
        "history": CLOSED_TRADES_HISTORY[:15],
        "calendar": DAILY_PNL_CALENDAR,
        "logs": SYSTEM_LOGS[:30],
        "top_symbols": TOP_LIQUID_SYMBOLS
    })

@app.get("/")
async def index_root():
    return HTMLResponse("<html><body><h3>Apex Scalper PRO v5.8 beží v poriadku.</h3></body></html>")

@app.post("/webhook")
@app.post("/api/webhook")
async def tradingview_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        background_tasks.add_task(execute_trade_in_background, data)
        return {"status": "accepted"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/telegram-webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        if "callback_query" in data:
            cb = data["callback_query"]
            cb_data = cb.get("data", "")
            if cb_data == "sell_now":
                await sell_now()
                send_telegram_msg("⚡ <b>Ručný predaj aktivovaný z Telegramu!</b>")
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
