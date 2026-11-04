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

# --- 🔑 OKX DEMO KĽÚČE ---
OKX_API_KEY = os.getenv("OKX_API_KEY", "638848ca-e65c-4074-bcfd-20cf4d292f2c")
OKX_SECRET = os.getenv("OKX_SECRET", "885934366C54F33B276E9E49E8EF87CF")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "Australia111/")
WEBHOOK_SECRET = "moje_tajne_heslo_123"

# --- 📱 TELEGRAM NOTIFIKÁCIE & INTERAKTÍVNE BOT OVLÁDANIE ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9021881475:AAFn1oQSvaGkhpX2uyJscdcTybFgk1tNmV8")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "9130979784")

# --- 📊 DYNAMICKÉ PROFILY PRE ČASOVÉ RÁMCE (8m až 4h) ---
TIMEFRAME_PROFILES = {
    "8m": {"trail_pct": 1.2, "be_trigger_pct": 0.4, "ttp_trigger_pct": 0.8, "ttp_callback_pct": 0.2, "desc": "Agresívny 8m scalp"},
    "15m": {"trail_pct": 0.7, "be_trigger_pct": 0.6, "ttp_trigger_pct": 0.8, "ttp_callback_pct": 0.2, "desc": "Rýchly 15m scalp"},
    "30m": {"trail_pct": 1.0, "be_trigger_pct": 0.9, "ttp_trigger_pct": 1.2, "ttp_callback_pct": 0.3, "desc": "30m trendový"},
    "45m": {"trail_pct": 1.5, "be_trigger_pct": 1.2, "ttp_trigger_pct": 1.8, "ttp_callback_pct": 0.4, "desc": "45m strednodobý"},
    "1h": {"trail_pct": 2.0, "be_trigger_pct": 1.6, "ttp_trigger_pct": 2.5, "ttp_callback_pct": 0.5, "desc": "1h hodinový swing"},
    "2h": {"trail_pct": 3.0, "be_trigger_pct": 2.2, "ttp_trigger_pct": 3.5, "ttp_callback_pct": 0.7, "desc": "2h dvojhodinový trend"},
    "3h": {"trail_pct": 4.0, "be_trigger_pct": 3.0, "ttp_trigger_pct": 5.0, "ttp_callback_pct": 1.0, "desc": "3h pozičný"},
    "4h": {"trail_pct": 5.5, "be_trigger_pct": 4.0, "ttp_trigger_pct": 7.0, "ttp_callback_pct": 1.5, "desc": "4h masívny swing"}
}

# --- GLOBAL STAV BOTA & LIMITOV ---
ACTIVE_TRADES = {}
DAILY_PNL_CALENDAR = []
CLOSED_TRADES_HISTORY = []
SYSTEM_LOGS = []
CURRENT_STAGE = "STAV: Mimo obchodu. Čakám na signál..."
LAST_TRADE_TIME = {}
MAX_PARALLEL_TRADES = 1
trade_lock = asyncio.Lock()

IS_BOT_ACTIVE = True
CURRENT_TIMEFRAME = "Multi-TF"
STORAGE_FILE = "trades_history.json"
ALLOWED_STABLES = ["USDC", "USDT"]

SETTINGS = TIMEFRAME_PROFILES["15m"].copy()
SETTINGS.update({"use_btc_filter": True, "reentry_cooldown_sec": 120, "use_breakeven": True})

def log_event(message: str):
    timestamp = datetime.now(timezone.utc) + timedelta(hours=1)
    time_str = timestamp.strftime("%H:%M:%S")
    entry = f"[{time_str}] {message}"
    SYSTEM_LOGS.insert(0, entry)
    if len(SYSTEM_LOGS) > 15:
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

def format_tv_ticker(raw_ticker: str):
    clean = raw_ticker.replace("OKX:", "").replace("/", "").replace("-", "").strip().upper()
    for stable in ALLOWED_STABLES:
        if clean.endswith(stable) and clean != stable:
            coin = clean[:-len(stable)]
            return coin, f"{coin}-{stable}"
    return clean, f"{clean}-USDC"

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
    CLOSED_TRADES_HISTORY.insert(0, {"pair": pair, "pnl_pct": pnl_pct, "pnl_usdc": pnl_usdc, "reason": reason, "time": get_sk_time_str()})
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

def fetch_account_capital(stable_ccy="USDC") -> float:
    try:
        balance_res = okx_request("GET", "/api/v5/account/balance")
        if balance_res.get("code") == "0":
            for acc in balance_res.get("data", []):
                for detail in acc.get("details", []):
                    if detail.get("ccy") == stable_ccy:
                        # Vyskúšame zobrať celkovú hodnotu účtu (eq) alebo dostupnú hotovosť
                        eq_val = safe_float(detail.get("eq", 0))
                        cash_val = safe_float(detail.get("cashBal", 0))
                        avail_val = safe_float(detail.get("availBal", 0))
                        
                        best_val = max(eq_val, cash_val, avail_val)
                        if best_val > 0:
                            return best_val
    except Exception as err:
        log_event(f"⚠️ Chyba pri načítaní zostatku: {err}")
    return 3000.0

def check_btc_health() -> bool:
    if not SETTINGS["use_btc_filter"]:
        return True
    try:
        url = f"/api/v5/market/candles?instId=BTC-USDC&bar=15m&limit=10"
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

def calculate_position_budget(free_capital: float, current_price: float, inst_id: str) -> float:
    base_budget = free_capital * 0.98
    if base_budget < 5.0:
        return 0.0
    return base_budget

async def advanced_risk_monitor_loop():
    global ACTIVE_TRADES, CURRENT_STAGE
    await asyncio.sleep(3)
    while True:
        if not ACTIVE_TRADES:
            if IS_BOT_ACTIVE:
                CURRENT_STAGE = "STAV: Bot aktívny. Skenujem Multi-TF trh..."
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
                bought_time = datetime.strptime(trade["bought_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=1)))
                if now_time - bought_time > timedelta(hours=48):
                    try:
                        ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={symbol}")
                        curr_p = safe_float(ticker_res['data'][0]['last']) if ticker_res.get('code') == '0' and ticker_res.get('data') else trade["entry_price"]
                        order_body = json.dumps({
                            "instId": symbol, "tdMode": "cash", "side": "sell",
                            "ordType": "market", "sz": str(round(trade["amount"], 2))
                        })
                        okx_request("POST", "/api/v5/trade/order", order_body)
                        pnl_pct = ((curr_p - trade["entry_price"]) / trade["entry_price"]) * 100
                        invested = trade.get("invested_usdc", 100.0)
                        pnl_usdc = invested * (pnl_pct / 100.0)
                        record_closed_trade(symbol, pnl_pct, pnl_usdc, "GTC Expirácia 48h")
                        log_event(f"⏳ Expirácia 48h pre {symbol} | PnL: {pnl_pct:+.2f}%")
                    except Exception:
                        pass
                    del ACTIVE_TRADES[coin]
                    continue
                ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={symbol}")
                if ticker_res.get("code") == "0" and ticker_res.get("data"):
                    current_price = safe_float(ticker_res["data"][0]["last"])
                    if current_price <= 0:
                        continue
                    if current_price > trade["highest_price"]:
                        trade["highest_price"] = current_price
                    pnl_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
                    invested = trade.get("invested_usdc", 100.0)
                    pnl_usdc = invested * (pnl_pct / 100.0)
                    be_price = trade["entry_price"] * (1 + (trade_settings["be_trigger_pct"] / 100.0))
                    trail_stop_price = trade["highest_price"] * (1 - (trade_settings["trail_pct"] / 100.0))
                    if pnl_pct >= trade_settings["be_trigger_pct"]:
                        trade["break_even_activated"] = True
                        effective_sl = max(trail_stop_price, trade["entry_price"])
                    else:
                        effective_sl = trade["entry_price"] * 0.97
                    ttp_activation_price = trade["entry_price"] * (1 + (trade_settings["ttp_trigger_pct"] / 100.0))
                    if current_price >= ttp_activation_price:
                        trade["ttp_activated"] = True
                    ttp_stop_level = trade["highest_price"] * (1 - (trade_settings["ttp_callback_pct"] / 100.0)) if trade.get("ttp_activated") else ttp_activation_price
                    trade["current_price"] = current_price
                    trade["live_pnl_pct"] = pnl_pct
                    trade["live_pnl_usdc"] = pnl_usdc
                    trade["be_target_price"] = be_price
                    trade["current_sl_price"] = effective_sl
                    trade["ttp_target_price"] = ttp_stop_level
                    CURRENT_STAGE = f"STAV: Pozícia {symbol} aktívna | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)"
                    if current_price <= effective_sl:
                        reason = "Break-Even Ochrana" if effective_sl >= trade["entry_price"] else "Stop-Loss (-3%)"
                        try:
                            order_body = json.dumps({
                                "instId": symbol, "tdMode": "cash", "side": "sell",
                                "ordType": "market", "sz": str(round(trade["amount"], 2))
                            })
                            okx_request("POST", "/api/v5/trade/order", order_body)
                            record_closed_trade(symbol, pnl_pct, pnl_usdc, reason)
                            log_event(f"🚨 Uzavretý obchod ({reason}) pre {symbol} | PnL: {pnl_pct:+.2f}%")
                            send_telegram_msg(f"🚨 <b>UZAVRETÝ OBCHOD ({reason})</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                        except Exception:
                            pass
                        del ACTIVE_TRADES[coin]
                        continue
                    if trade.get("ttp_activated") and current_price <= ttp_stop_level:
                        reason = "Trailing Take Profit"
                        try:
                            order_body = json.dumps({
                                "instId": symbol, "tdMode": "cash", "side": "sell",
                                "ordType": "market", "sz": str(round(trade["amount"], 2))
                            })
                            okx_request("POST", "/api/v5/trade/order", order_body)
                            record_closed_trade(symbol, pnl_pct, pnl_usdc, reason)
                            log_event(f"🎯 Uzavretý obchod (Trailing TP) pre {symbol} | PnL: {pnl_pct:+.2f}%")
                            send_telegram_msg(f"🎯 <b>UZAVRETÝ OBCHOD ({reason})</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                        except Exception:
                            pass
                        del ACTIVE_TRADES[coin]
                        continue
        except Exception:
            pass
        await asyncio.sleep(2)

async def execute_trade_in_background(data: dict):
    global ACTIVE_TRADES, LAST_TRADE_TIME, CURRENT_TIMEFRAME, SETTINGS
    async with trade_lock:
        try:
            req_secret = str(data.get("secret", "")).strip()
            if req_secret != WEBHOOK_SECRET:
                log_event("⚠️ Ignorovaný webhook: Neplatné alebo chýbajúce heslo (secret).")
                return
            action = str(data.get("action", "")).lower().strip()
            if "{{" in action or "}}" in action:
                log_event(f"⚠️ Ignorovaný webhook: TradingView nepreložil šablónu action: {action}")
                return
            
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
                stable_ccy = "USDC" if "USDC" in okx_symbol else "USDT"
                free_capital = fetch_account_capital(stable_ccy)
                safe_budget = calculate_position_budget(free_capital, 0.0, okx_symbol)
                if safe_budget <= 0:
                    log_event(f"❌ Nedostatok prostriedkov pre nákup: {free_capital} {stable_ccy}")
                    return
                ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={okx_symbol}")
                if ticker_res.get("code") != "0" or not ticker_res.get("data"):
                    return
                current_price = safe_float(ticker_res["data"][0]["last"])
                
                coin_amount = round(safe_budget / current_price, 2)

                order_body = json.dumps({
                    "instId": okx_symbol, "tdMode": "cash", "side": "buy",
                    "ordType": "market", "sz": str(coin_amount)
                })
                order_res = okx_request("POST", "/api/v5/trade/order", order_body)
                if order_res.get("code") != "0":
                    log_event(f"❌ Chyba nákupnej objednávky: {order_res}")
                    return
                
                sl_price = current_price * 0.97
                be_price = current_price * (1 + (SETTINGS["be_trigger_pct"] / 100.0))
                ttp_price = current_price * (1 + (SETTINGS["ttp_trigger_pct"] / 100.0))
                ACTIVE_TRADES[coin_name] = {
                    "entry_price": current_price,
                    "current_sl_price": sl_price,
                    "be_target_price": be_price,
                    "ttp_target_price": ttp_price,
                    "amount": coin_amount,
                    "bought_at": get_sk_time_str(),
                    "market_pair": okx_symbol,
                    "highest_price": current_price,
                    "partial_tp_activated": False,
                    "break_even_activated": False,
                    "ttp_activated": False,
                    "invested_usdc": safe_budget,
                    "live_pnl_pct": 0.0,
                    "live_pnl_usdc": 0.0,
                    "tf": CURRENT_TIMEFRAME,
                    "settings": SETTINGS.copy()
                }
                log_event(f"🛒 Nová pozícia [{CURRENT_TIMEFRAME}]: Nákup {okx_symbol} za {current_price:.4f} USDC (Rozpočet: {safe_budget:.2f}$)")
                inline_keyboard = {
                    "inline_keyboard": [
                        [
                            {"text": "🛑 ZATVORIŤ POZÍCIU", "callback_data": "sell_now"},
                            {"text": "⏸️ PAUZA BOTA", "callback_data": "toggle_bot"}
                        ]
                    ]
                }
                send_telegram_msg(
                    f"🟢 <b>NOVÝ OBCHOD [TF: {CURRENT_TIMEFRAME}]</b>\n"
                    f"<b>Pár:</b> {okx_symbol}\n"
                    f"<b>Vstup:</b> {current_price:.4f}\n"
                    f"<b>Investícia:</b> {safe_budget:.2f} USDC\n"
                    f"🎯 <b>BE (+{SETTINGS['be_trigger_pct']}%):</b> {be_price:.4f} | <b>Trail TP (+{SETTINGS['ttp_trigger_pct']}%):</b> {ttp_price:.4f}",
                    reply_markup=inline_keyboard
                )
            elif action == "sell":
                target_coin = coin_name if coin_name in ACTIVE_TRADES else None
                if not target_coin:
                    for c, t in ACTIVE_TRADES.items():
                        if t["market_pair"] == okx_symbol or c.upper() == coin_name:
                            target_coin = c
                            break
                if target_coin:
                    trade = ACTIVE_TRADES[target_coin]
                    pair_to_sell = trade["market_pair"]
                    ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={pair_to_sell}")
                    curr_p = safe_float(ticker_res['data'][0]['last']) if ticker_res.get('code') == '0' and ticker_res.get('data') else trade["entry_price"]
                    
                    order_body = json.dumps({
                        "instId": pair_to_sell, "tdMode": "cash", "side": "sell",
                        "ordType": "market", "sz": str(round(trade["amount"], 2))
                    })
                    order_res = okx_request("POST", "/api/v5/trade/order", order_body)
                    pnl_pct = ((curr_p - trade["entry_price"]) / trade["entry_price"]) * 100
                    invested = trade.get("invested_usdc", 100.0)
                    pnl_usdc = invested * (pnl_pct / 100.0)
                    record_closed_trade(pair_to_sell, pnl_pct, pnl_usdc, "Webhook Predaj")
                    log_event(f"🔴 Predaj z Webhooku pre {pair_to_sell} | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                    send_telegram_msg(f"🔴 <b>PREDAJ (WEBHOOK)</b>\n<b>Pár:</b> {pair_to_sell}\n<b>PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                    del ACTIVE_TRADES[target_coin]
        except Exception as e:
            log_event(f"❌ Chyba webhooku: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_persistent_data()
    log_event("🚀 Apex Scalper PRO v5.4.8 (Multi-TF) spustený.")
    monitor_task = asyncio.create_task(advanced_risk_monitor_loop())
    send_telegram_msg("🚀 <b>Apex Scalper PRO v5.4.8 pripravený!</b>")
    yield
    monitor_task.cancel()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
@app.post("/api/webhook")
async def tradingview_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        log_event(f"📥 Prijatý webhook: {data}")
        action = str(data.get("action", "")).lower().strip()
        tv_ticker = str(data.get("symbol", data.get("ticker", ""))).upper().strip()
        coin_name, okx_symbol = format_tv_ticker(tv_ticker)
        if action == "sell" and coin_name not in ACTIVE_TRADES:
            return {"status": "ignored_no_active_trade"}
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
    log_event(f"⚙️ Prepínač bota: {status_str.upper()}")
    send_telegram_msg(f"⚙️ <b>Zmena stavu:</b> Bot je teraz {status_str.upper()}.")
    return JSONResponse({"status": "success", "is_bot_active": IS_BOT_ACTIVE})

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
                
                order_body = json.dumps({
                    "instId": symbol, "tdMode": "cash", "side": "sell",
                    "ordType": "market", "sz": str(round(trade["amount"], 2))
                })
                okx_request("POST", "/api/v5/trade/order", order_body)
                pnl_pct = ((curr_p - trade["entry_price"]) / trade["entry_price"]) * 100
                invested = trade.get("invested_usdc", 100.0)
                pnl_usdc = invested * (pnl_pct / 100.0)
                record_closed_trade(symbol, pnl_pct, pnl_usdc, "Ručný okamžitý predaj (UI)")
                log_event(f"⚡ Okamžitý ručný predaj pre {symbol} | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                send_telegram_msg(f"⚡ <b>OKAMŽITÝ RUČNÝ PREDAJ</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                sold.append(symbol)
            except Exception as e:
                log_event(f"⚠️ Chyba pri Emergency Sell pre {symbol}: {e}")
            finally:
                # Bezpečné odstránenie pozície zo zoznamu za každých okolností, aby sa UI okamžite odblokovalo
                if coin in ACTIVE_TRADES:
                    del ACTIVE_TRADES[coin]

        return JSONResponse({"status": "success", "sold": sold})

HTML_TEMPLATE_CONTENT = """<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Apex Scalper v5.4.8 PRO — Terminal</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #000000;
            --card-bg: #0a0a0c;
            --border-color: #1a1a1e;
            --border-hover: #33333b;
            --text-main: #f4f4f5;
            --text-muted: #71717a;
            --accent-white: #ffffff;
            --green: #22c55e;
            --red: #ef4444;
            --yellow: #f59e0b;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'JetBrains Mono', monospace; }
        body { background-color: var(--bg-color); color: var(--text-main); padding: 12px 16px; height: 100vh; overflow: hidden; display: flex; flex-direction: column; justify-content: center; }
        
        .container { max-width: 1000px; width: 100%; margin: 0 auto; display: flex; flex-direction: column; gap: 10px; }

        .top-bar {
            display: flex; justify-content: space-between; align-items: center;
            border-bottom: 1px solid var(--border-color); padding-bottom: 8px;
            flex-wrap: wrap; gap: 10px;
        }
        .brand h1 { font-size: 1rem; font-weight: 700; color: var(--accent-white); letter-spacing: -0.5px; }
        .brand p { font-size: 0.7rem; color: var(--text-muted); margin-top: 2px; text-transform: uppercase; letter-spacing: 1px; display: flex; gap: 10px; flex-wrap: wrap; }
        .balance-tag { color: var(--yellow); font-weight: 700; }

        .actions { display: flex; gap: 8px; align-items: center; }

        .btn {
            background: transparent; border: 1px solid var(--border-hover); color: var(--text-main);
            padding: 6px 12px; font-size: 0.75rem; font-weight: 500; cursor: pointer; border-radius: 4px;
            transition: all 0.15s ease; text-transform: uppercase; letter-spacing: 0.5px;
        }
        .btn:hover { border-color: var(--text-muted); background: rgba(255,255,255,0.02); }
        
        .btn-on { border-color: var(--green); color: var(--green); }
        .btn-off { border-color: var(--text-muted); color: var(--text-muted); }
        
        .btn-sell { background: var(--accent-white); color: var(--bg-color); font-weight: 700; border-color: var(--accent-white); }
        .btn-sell:hover { background: #e4e4e7; }
        .btn-sell:disabled { opacity: 0.2; cursor: not-allowed; background: transparent; color: var(--text-muted); border-color: var(--border-color); }

        .status-ticker {
            font-size: 0.75rem; color: var(--text-muted); background: var(--card-bg);
            border: 1px solid var(--border-color); padding: 6px 12px; border-radius: 4px;
            display: flex; align-items: center; gap: 8px;
        }
        .status-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--green); box-shadow: 0 0 8px var(--green); }

        .stats-grid {
            display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px;
        }
        .stat-card {
            background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 6px; padding: 8px 12px;
        }
        .stat-title { font-size: 0.65rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
        .stat-value { font-size: 1.05rem; font-weight: 700; margin-top: 2px; color: var(--accent-white); }

        .section-title { font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); letter-spacing: 1px; margin-top: 4px; margin-bottom: 4px; }
        
        .terminal-card {
            background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 6px;
            overflow: hidden;
        }

        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.75rem; }
        th { color: var(--text-muted); font-weight: 400; padding: 6px 10px; border-bottom: 1px solid var(--border-color); font-size: 0.65rem; text-transform: uppercase; }
        td { padding: 8px 10px; border-bottom: 1px solid var(--border-color); color: var(--text-main); }
        tr:last-child td { border-bottom: none; }

        .tag { font-size: 0.7rem; padding: 2px 5px; border: 1px solid var(--border-hover); border-radius: 3px; color: var(--accent-white); }
        
        .val-price { color: var(--accent-white); font-weight: 500; }
        .val-be { color: var(--yellow); }
        .val-sl { color: var(--red); }
        .val-tp { color: var(--green); }

        .pnl-green { color: var(--green); font-weight: 700; }
        .pnl-red { color: var(--red); font-weight: 700; }

        .logs-box {
            padding: 8px 12px; font-size: 0.7rem; color: var(--text-muted); max-height: 85px; overflow-y: auto; line-height: 1.4;
        }

        .empty { padding: 12px; text-align: center; color: var(--text-muted); font-size: 0.75rem; }
    </style>
</head>
<body>
    <div class="container">
        <div class="top-bar">
            <div class="brand">
                <h1>APEX SCALPER // PRO v5.4.8</h1>
                <p>
                    <span>SOL-USDC · <b style="color: #ffffff;">{{ current_tf }}</b> · OKX Demo</span>
                    <span>Zostatok: <b class="balance-tag">{{ "%.2f"|format(usdc_balance) }} USDC</b></span>
                </p>
            </div>
            <div class="actions">
                <button id="toggleBtn" onclick="toggleBot()" class="btn {{ 'btn-on' if is_bot_active else 'btn-off' }}">
                    {{ '● ACTIVE' if is_bot_active else '○ PAUSED' }}
                </button>
                <button id="sellBtn" onclick="sellNow()" class="btn btn-sell" {{ '' if trades else 'disabled' }}>
                    ⚡ EMERGENCY SELL
                </button>
            </div>
        </div>

        <div class="status-ticker">
            <div class="status-dot"></div>
            <span>{{ current_stage }}</span>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-title">Win Rate</div>
                <div class="stat-value" style="color: var(--green);">{{ stats.win_rate }}%</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">Total Trades</div>
                <div class="stat-value">{{ stats.total_trades }}</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">Win / Loss</div>
                <div class="stat-value"><span style="color: var(--green);">{{ stats.wins }}W</span> / <span style="color: var(--red);">{{ stats.losses }}L</span></div>
            </div>
            <div class="stat-card">
                <div class="stat-title">Net PnL</div>
                <div class="stat-value {{ 'pnl-green' if stats.total_pnl >= 0 else 'pnl-red' }}">{{ "%+.2f"|format(stats.total_pnl) }} $</div>
            </div>
        </div>

        <div class="section-title">Active Position & Dynamic Levels</div>
        <div class="terminal-card">
            {% if trades %}
                <table>
                    <thead>
                        <tr>
                            <th>Pair</th>
                            <th>Entry</th>
                            <th>Current</th>
                            <th>Break-Even</th>
                            <th>Trailing SL</th>
                            <th>Trailing TP</th>
                            <th>Live PnL</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for t, d in trades.items() %}
                        <tr>
                            <td><span class="tag">{{ d.market_pair }}</span></td>
                            <td>{{ "%.4f"|format(d.entry_price) }}</td>
                            <td class="val-price">{{ "%.4f"|format(d.current_price) }}</td>
                            <td class="val-be">{{ "%.4f"|format(d.be_target_price) }}</td>
                            <td class="val-sl">{{ "%.4f"|format(d.current_sl_price) }}</td>
                            <td class="val-tp">{{ "%.4f"|format(d.ttp_target_price) }}</td>
                            <td class="{{ 'pnl-green' if d.live_pnl_pct >= 0 else 'pnl-red' }}">
                                {{ "%+.2f"|format(d.live_pnl_pct) }}% ({{ "%+.2f"|format(d.live_pnl_usdc) }} $)
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            {% else %}
                <div class="empty">No active positions. Scanning orderbook for triggers...</div>
            {% endif %}
        </div>

        <div class="section-title">Execution History (3-Month Persistent Storage)</div>
        <div class="terminal-card">
            {% if history %}
                <table>
                    <thead>
                        <tr>
                            <th>Timestamp</th>
                            <th>Pair</th>
                            <th>Trigger Reason</th>
                            <th>Net PnL</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for h in history[:3] %}
                        <tr>
                            <td style="color: var(--text-muted); font-size: 0.7rem;">{{ h.time }}</td>
                            <td><b>{{ h.pair }}</b></td>
                            <td style="color: var(--text-muted);">{{ h.reason }}</td>
                            <td class="{{ 'pnl-green' if h.pnl_usdc >= 0 else 'pnl-red' }}">
                                {{ "%+.2f"|format(h.pnl_usdc) }} $ ({{ "%+.2f"|format(h.pnl_pct) }}%)
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            {% else %}
                <div class="empty">No closed trades recorded yet.</div>
            {% endif %}
        </div>

        <div class="section-title">System Execution Logs</div>
        <div class="terminal-card">
            <div class="logs-box">
                {% for log in logs[:4] %}
                    <div>{{ log }}</div>
                {% endfor %}
            </div>
        </div>
    </div>

    <script>
        async function toggleBot() {
            try {
                const res = await fetch('/api/toggle-bot', { method: 'POST' });
                const data = await res.json();
                if(data.status === 'success') location.reload();
            } catch(e) { alert('Error: ' + e); }
        }

        async function sellNow() {
            if(!confirm("Execute immediate market exit for active position?")) return;
            try {
                const res = await fetch('/api/sell-now', { method: 'POST' });
                const data = await res.json();
                if(data.status === 'success') {
                    location.reload();
                } else {
                    alert('Error: ' + data.message);
                }
            } catch(e) { alert('Error: ' + e); }
        }

        setTimeout(() => { location.reload(); }, 3000);
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    usdc_balance = fetch_account_capital("USDC")
    stats = calculate_stats()
    template = Template(HTML_TEMPLATE_CONTENT)
    return template.render(
        trades=ACTIVE_TRADES,
        history=CLOSED_TRADES_HISTORY,
        calendar=DAILY_PNL_CALENDAR,
        current_stage=CURRENT_STAGE,
        is_bot_active=IS_BOT_ACTIVE,
        usdc_balance=usdc_balance,
        stats=stats,
        logs=SYSTEM_LOGS,
        current_tf=CURRENT_TIMEFRAME
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
