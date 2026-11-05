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

# --- 🔑 OKX KĽÚČE ---
OKX_API_KEY = os.getenv("OKX_API_KEY", "638848ca-e65c-4074-bcfd-20cf4d292f2c")
OKX_SECRET = os.getenv("OKX_SECRET", "885934366C54F33B276E9E49E8EF87CF")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "Australia111/")
WEBHOOK_SECRET = "moje_tajne_heslo_123"

# --- 📱 TELEGRAM NOTIFIKÁCIE & INTERAKTÍVNE BOT OVLÁDANIE ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9021881475:AAFn1oQSvaGkhpX2uyJscdcTybFgk1tNmV8")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "9130979784")

# --- 📊 DYNAMICKÉ PROFILY PRE ČASOVÉ RÁMCE ---
TIMEFRAME_PROFILES = {
    "1m": {"trail_pct": 0.5, "be_trigger_pct": 0.3, "ttp_trigger_pct": 0.6, "ttp_callback_pct": 0.15, "desc": "Skalper 1m"},
    "8m": {"trail_pct": 1.2, "be_trigger_pct": 0.4, "ttp_trigger_pct": 0.8, "ttp_callback_pct": 0.2, "desc": "Agresívny 8m scalp"},
    "15m": {"trail_pct": 0.7, "be_trigger_pct": 0.6, "ttp_trigger_pct": 0.8, "ttp_callback_pct": 0.2, "desc": "Rýchly 15m scalp"},
    "30m": {"trail_pct": 1.0, "be_trigger_pct": 0.9, "ttp_trigger_pct": 1.2, "ttp_callback_pct": 0.3, "desc": "30m trendový"},
    "45m": {"trail_pct": 1.5, "be_trigger_pct": 1.2, "ttp_trigger_pct": 1.8, "ttp_callback_pct": 0.4, "desc": "45m strednodobý"},
    "1h": {"trail_pct": 2.0, "be_trigger_pct": 1.6, "ttp_trigger_pct": 2.5, "ttp_callback_pct": 0.5, "desc": "1h hodinový swing"},
    "2h": {"trail_pct": 3.0, "be_trigger_pct": 2.2, "ttp_trigger_pct": 3.5, "ttp_callback_pct": 0.7, "desc": "2h dvojhodinový trend"},
    "3h": {"trail_pct": 4.0, "be_trigger_pct": 3.0, "ttp_trigger_pct": 5.0, "ttp_callback_pct": 1.0, "desc": "3h pozičný"},
    "4h": {"trail_pct": 5.5, "be_trigger_pct": 4.0, "ttp_trigger_pct": 7.0, "ttp_callback_pct": 1.5, "desc": "4h masívny swing"}
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
CURRENT_TIMEFRAME = "Multi-TF"
STORAGE_FILE = "trades_history.json"
ALLOWED_STABLES = ["USDC", "USDT"]

SETTINGS = TIMEFRAME_PROFILES["30m"].copy()
SETTINGS.update({
    "use_btc_filter": True, 
    "reentry_cooldown_sec": 120, 
    "use_breakeven": True,
    "max_budget_cap": 3000.0
})

def log_event(message: str):
    timestamp = datetime.now(timezone.utc) + timedelta(hours=1)
    time_str = timestamp.strftime("%H:%M:%S")
    entry = f"[{time_str}] {message}"
    SYSTEM_LOGS.insert(0, entry)
    if len(SYSTEM_LOGS) > 60:
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
    CLOSED_TRADES_HISTORY.insert(0, {"pair": pair, "pnl_pct": pnl_pct, "pnl_usdc": round(pnl_usdc, 2), "reason": reason, "time": get_sk_time_str()})
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
                        avail = safe_float(detail.get("availBal", 0))
                        cash = safe_float(detail.get("cashBal", 0))
                        eq = safe_float(detail.get("eq", 0))
                        best_val = max(avail, cash, eq)
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
                    except Exception:
                        pass
                    del ACTIVE_TRADES[coin]
                    continue

                if current_price > 0:
                    if current_price > trade["highest_price"]:
                        trade["highest_price"] = current_price
                    pnl_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
                    
                    # OPRAVENÉ: Výpočet live dolárového PnL presne z reálne minutých prostriedkov (coin_amount * entry_price)
                    invested = trade.get("invested_usdc", trade["amount"] * trade["entry_price"])
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
                            log_event(f"🚨 Uzavretý obchod ({reason}) pre {symbol} | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
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
                            log_event(f"🎯 Uzavretý obchod (Trailing TP) pre {symbol} | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
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
            
            incoming_tf = str(data.get("tf", "30m")).strip().lower()
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
                
                desired_from_balance = free_capital * 0.98
                budget_cap = SETTINGS.get("max_budget_cap", 3000.0)
                safe_budget = min(desired_from_balance, budget_cap)
                
                if safe_budget < 5.0:
                    log_event(f"❌ Nedostatok prostriedkov pre nákup: {free_capital} {stable_ccy}")
                    return
                
                ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={okx_symbol}")
                if ticker_res.get("code") != "0" or not ticker_res.get("data"):
                    log_event(f"❌ Nepodarilo sa získať cenu pre {okx_symbol}")
                    return
                current_price = safe_float(ticker_res["data"][0]["last"])
                
                coin_amount = round(safe_budget / current_price, 2)
                
                order_body = json.dumps({
                    "instId": okx_symbol,
                    "tdMode": "cash",
                    "side": "buy",
                    "ordType": "market",
                    "sz": f"{coin_amount}"
                })
                
                order_res = okx_request("POST", "/api/v5/trade/order", order_body)
                if order_res.get("code") != "0":
                    log_event(f"❌ Chyba nákupnej objednávky: {order_res}")
                    return
                
                actual_spent_usdc = coin_amount * current_price
                
                sl_price = current_price * 0.97
                be_price = current_price * (1 + (SETTINGS["be_trigger_pct"] / 100.0))
                ttp_price = current_price * (1 + (SETTINGS["ttp_trigger_pct"] / 100.0))
                
                ACTIVE_TRADES[coin_name] = {
                    "entry_price": current_price,
                    "current_price": current_price,
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
                    "invested_usdc": actual_spent_usdc,
                    "live_pnl_pct": 0.0,
                    "live_pnl_usdc": 0.0,
                    "tf": CURRENT_TIMEFRAME,
                    "settings": SETTINGS.copy()
                }
                log_event(f"🛒 Nová pozícia [{CURRENT_TIMEFRAME}]: Nákup {okx_symbol} za {current_price:.4f} USDC (Investované: {actual_spent_usdc:.2f}$ / Strop: {budget_cap}$)")
                
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
                    f"<b>Investícia:</b> {actual_spent_usdc:.2f} USDC (Strop: {budget_cap}$)\n"
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
                    okx_request("POST", "/api/v5/trade/order", order_body)
                    pnl_pct = ((curr_p - trade["entry_price"]) / trade["entry_price"]) * 100
                    invested = trade.get("invested_usdc", trade["amount"] * trade["entry_price"])
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
    log_event("🚀 Apex Scalper PRO v5.6.0 spustený.")
    monitor_task = asyncio.create_task(advanced_risk_monitor_loop())
    send_telegram_msg("🚀 <b>Apex Scalper PRO v5.6.0 pripravený!</b>")
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

@app.post("/api/toggle-btc-filter")
async def toggle_btc_filter():
    SETTINGS["use_btc_filter"] = not SETTINGS.get("use_btc_filter", True)
    status_str = "zapnutý (aktívny)" if SETTINGS["use_btc_filter"] else "vypnutý (deaktivovaný)"
    log_event(f"⚙️ BTC Trend Guard filter: {status_str.upper()}")
    send_telegram_msg(f"⚙️ <b>Zmena stavu:</b> BTC Trend Guard filter je teraz {status_str.upper()}.")
    return JSONResponse({"status": "success", "use_btc_filter": SETTINGS["use_btc_filter"]})

@app.post("/api/set-budget/{amount}")
async def set_budget(amount: int):
    if amount in [1000, 3000, 5000, 10000]:
        SETTINGS["max_budget_cap"] = float(amount)
        log_event(f"💰 Maximálny rozpočet (strop) zmenený na: {amount} USDC")
        send_telegram_msg(f"💰 <b>Zmena rozpočtu:</b> Maximálny strop pre obchod je teraz <b>{amount} USDC</b>.")
        return JSONResponse({"status": "success", "max_budget_cap": amount})
    return JSONResponse({"status": "error", "message": "Neplatná hodnota rozpočtu."})

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
                invested = trade.get("invested_usdc", trade["amount"] * trade["entry_price"])
                pnl_usdc = invested * (pnl_pct / 100.0)
                record_closed_trade(symbol, pnl_pct, pnl_usdc, "Ručný okamžitý predaj (UI)")
                log_event(f"⚡ Okamžitý ručný predaj pre {symbol} | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                send_telegram_msg(f"⚡ <b>OKAMŽITÝ RUČNÝ PREDAJ</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                sold.append(symbol)
            except Exception as e:
                log_event(f"⚠️ Chyba pri Emergency Sell pre {symbol}: {e}")
            finally:
                if coin in ACTIVE_TRADES:
                    del ACTIVE_TRADES[coin]

        return JSONResponse({"status": "success", "sold": sold})

HTML_TEMPLATE_CONTENT = """<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Apex Scalper v5.6.0 PRO — Terminal</title>
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
        body { background-color: var(--bg-color); color: var(--text-main); padding: 16px; min-height: 100vh; overflow-y: auto; display: flex; flex-direction: column; }
        .container { max-width: 1000px; width: 100%; margin: 0 auto; display: flex; flex-direction: column; gap: 14px; padding-bottom: 30px; }
        .top-bar { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); padding-bottom: 10px; flex-wrap: wrap; gap: 10px; }
        .brand h1 { font-size: 1rem; font-weight: 700; color: var(--accent-white); letter-spacing: -0.5px; }
        .brand p { font-size: 0.7rem; color: var(--text-muted); margin-top: 2px; text-transform: uppercase; letter-spacing: 1px; display: flex; gap: 10px; flex-wrap: wrap; }
        .balance-tag { color: var(--yellow); font-weight: 700; }
        .actions { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
        .btn { background: transparent; border: 1px solid var(--border-hover); color: var(--text-main); padding: 5px 10px; font-size: 0.7rem; font-weight: 500; cursor: pointer; border-radius: 4px; transition: all 0.15s ease; text-transform: uppercase; letter-spacing: 0.5px; }
        .btn:hover { border-color: var(--text-muted); background: rgba(255,255,255,0.02); }
        .btn-on { border-color: var(--green); color: var(--green); }
        .btn-off { border-color: var(--text-muted); color: var(--text-muted); }
        .btn-yellow { border-color: var(--yellow); color: var(--yellow); background: rgba(245, 158, 11, 0.08); font-weight: 700; }
        .btn-sell { background: var(--accent-white); color: var(--bg-color); font-weight: 700; border-color: var(--accent-white); }
        .btn-sell:hover { background: #e4e4e7; }
        .btn-sell:disabled { opacity: 0.2; cursor: not-allowed; background: transparent; color: var(--text-muted); border-color: var(--border-color); }
        .status-ticker { font-size: 0.75rem; color: var(--text-muted); background: var(--card-bg); border: 1px solid var(--border-color); padding: 8px 12px; border-radius: 4px; display: flex; align-items: center; gap: 8px; }
        .status-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--green); box-shadow: 0 0 8px var(--green); }
        .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
        .stat-card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 6px; padding: 10px 12px; }
        .stat-title { font-size: 0.65rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
        .stat-value { font-size: 1.1rem; font-weight: 700; margin-top: 4px; color: var(--accent-white); }
        .section-title { font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); letter-spacing: 1px; margin-top: 6px; margin-bottom: 4px; }
        .terminal-card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 6px; overflow: hidden; }
        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.75rem; }
        th { color: var(--text-muted); font-weight: 400; padding: 8px 10px; border-bottom: 1px solid var(--border-color); font-size: 0.65rem; text-transform: uppercase; background: var(--card-bg); position: sticky; top: 0; z-index: 2; }
        td { padding: 9px 10px; border-bottom: 1px solid var(--border-color); color: var(--text-main); }
        tr:last-child td { border-bottom: none; }
        
        /* Čisté, prehľadné rolovacie okno pre históriu a logy bez prekrývania textu */
        .scrollable-card { max-height: 160px; overflow-y: auto; }
        .logs-box { 
            background: var(--card-bg); 
            border: 1px solid var(--border-color); 
            border-radius: 6px; 
            padding: 10px 12px; 
            height: 180px; 
            max-height: 180px;
            overflow-y: auto; 
            font-size: 0.7rem; 
            color: var(--text-muted); 
            display: flex; 
            flex-direction: column; 
            gap: 6px; 
        }
        .log-line { 
            white-space: pre-wrap; 
            word-break: break-all; 
            line-height: 1.4;
            border-bottom: 1px dashed rgba(255,255,255,0.03);
            padding-bottom: 4px;
        }
        .text-green { color: var(--green); font-weight: 700; }
        .text-red { color: var(--red); font-weight: 700; }
    </style>
</head>
<body>
    <div class="container">
        <div class="top-bar">
            <div class="brand">
                <h1>APEX SCALPER // PRO v5.6.0</h1>
                <p>
                    <span id="header-pair">SOL-USDC · Multi-TF · OKX Demo</span>
                    <span>Zostatok: <span id="header-balance" class="balance-tag">-- USDC</span></span>
                </p>
            </div>
            <div class="actions">
                <button class="btn {% if SETTINGS.max_budget_cap == 1000 %}btn-yellow{% endif %}" onclick="setBudget(1000)">1K</button>
                <button class="btn {% if SETTINGS.max_budget_cap == 3000 %}btn-yellow{% endif %}" onclick="setBudget(3000)">3K</button>
                <button class="btn {% if SETTINGS.max_budget_cap == 5000 %}btn-yellow{% endif %}" onclick="setBudget(5000)">5K</button>
                <button class="btn {% if SETTINGS.max_budget_cap == 10000 %}btn-yellow{% endif %}" onclick="setBudget(10000)">10K</button>
                <button id="btc-btn" class="btn {% if SETTINGS.use_btc_filter %}btn-on{% else %}btn-off{% endif %}" onclick="toggleBtcFilter()">
                    BTC: {% if SETTINGS.use_btc_filter %}ON{% else %}OFF{% endif %}
                </button>
                <button id="bot-btn" class="btn {% if IS_BOT_ACTIVE %}btn-on{% else %}btn-off{% endif %}" onclick="toggleBot()">
                    {% if IS_BOT_ACTIVE %}● ACTIVE{% else %}○ PAUSED{% endif %}
                </button>
                <button id="sell-btn" class="btn btn-sell" onclick="sellNow()" disabled>⚡ EMERGENCY SELL</button>
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
            <div class="section-title">Active Position & Dynamic Levels</div>
            <div class="terminal-card">
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
                    <tbody id="active-position-tbody">
                        <tr><td colspan="7" style="text-align: center; color: var(--text-muted);">No active positions. Scanning orderbook for triggers...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div>
            <div class="section-title">Execution History (3-Month Persistent Storage)</div>
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
                        <tr><td colspan="4" style="text-align: center; color: var(--text-muted);">No closed trades recorded yet.</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div>
            <div class="section-title">System Execution Logs</div>
            <div class="logs-box" id="logs-container">
                <div class="log-line">[System Initialized] Čakám na prvé udalosti...</div>
            </div>
        </div>
    </div>

    <script>
        async function toggleBot() {
            let res = await fetch('/api/toggle-bot', {method: 'POST'});
            let data = await res.json();
            let btn = document.getElementById('bot-btn');
            if (data.is_bot_active) {
                btn.className = "btn btn-on";
                btn.innerText = "● ACTIVE";
            } else {
                btn.className = "btn btn-off";
                btn.innerText = "○ PAUSED";
            }
            updateState();
        }

        async function toggleBtcFilter() {
            let res = await fetch('/api/toggle-btc-filter', {method: 'POST'});
            let data = await res.json();
            let btn = document.getElementById('btc-btn');
            if (data.use_btc_filter) {
                btn.className = "btn btn-on";
                btn.innerText = "BTC: ON";
            } else {
                btn.className = "btn btn-off";
                btn.innerText = "BTC: OFF";
            }
        }

        async function setBudget(amount) {
            let res = await fetch(`/api/set-budget/${amount}`, {method: 'POST'});
            let data = await res.json();
            if (data.status === 'success') {
                document.querySelectorAll('.actions .btn').forEach(b => {
                    if (['1K','3K','5K','10K'].includes(b.innerText)) {
                        b.className = "btn";
                    }
                });
                event.target.className = "btn btn-yellow";
            }
        }

        async function sellNow() {
            if (!confirm("Naozaj vykonať núdzový okamžitý predaj všetkých pozícií?")) return;
            let res = await fetch('/api/sell-now', {method: 'POST'});
            let data = await res.json();
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
                        html += `<tr>
                            <td><b>${t.market_pair}</b></td>
                            <td>${t.entry_price.toFixed(4)}</td>
                            <td>${t.current_price.toFixed(4)}</td>
                            <td>${t.be_target_price.toFixed(4)}</td>
                            <td>${t.current_sl_price.toFixed(4)}</td>
                            <td>${t.ttp_target_price.toFixed(4)}</td>
                            <td class="${pnlClass}">${t.live_pnl_pct >= 0 ? "+" : ""}${t.live_pnl_pct.toFixed(2)}% (${t.live_pnl_usdc >= 0 ? "+" : ""}${t.live_pnl_usdc.toFixed(2)} $)</td>
                        </tr>`;
                    }
                    activeTbody.innerHTML = html;
                    sellBtn.disabled = false;
                } else {
                    activeTbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">No active positions. Scanning orderbook for triggers...</td></tr>`;
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
                    historyTbody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">No closed trades recorded yet.</td></tr>`;
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
        "history": CLOSED_TRADES_HISTORY[:15],
        "logs": SYSTEM_LOGS
    })

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
