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
    "5m": {"trail_pct": 0.0, "be_trigger_pct": 0.0, "ttp_trigger_pct": 0.0, "ttp_callback_pct": 0.0, "target_pct": 0.0, "desc": "Manuál 5m (Bez % podmienok)"},
    "8m": {"trail_pct": 0.0, "be_trigger_pct": 0.0, "ttp_trigger_pct": 0.0, "ttp_callback_pct": 0.0, "desc": "Manuál 8m (Bez % podmienok)"},
    "15m": {"trail_pct": 0.7, "be_trigger_pct": 0.6, "ttp_trigger_pct": 0.8, "ttp_callback_pct": 0.2, "target_pct": 0.65, "desc": "Rýchly 15m scalp (+0.65%)"},
    "30m": {"trail_pct": 1.0, "be_trigger_pct": 0.9, "ttp_trigger_pct": 1.2, "ttp_callback_pct": 0.3, "target_pct": 1.2, "desc": "30m trendový"},
    "45m": {"trail_pct": 1.5, "be_trigger_pct": 1.2, "ttp_trigger_pct": 1.8, "ttp_callback_pct": 0.4, "target_pct": 1.5, "desc": "45m strednodobý"},
    "1h": {"trail_pct": 2.0, "be_trigger_pct": 1.6, "ttp_trigger_pct": 2.5, "ttp_callback_pct": 0.5, "target_pct": 1.7, "desc": "1h hodinový swing"},
    "2h": {"trail_pct": 3.0, "be_trigger_pct": 2.2, "ttp_trigger_pct": 3.5, "ttp_callback_pct": 0.7, "target_pct": 2.0, "desc": "2h dvojhodinový trend"},
    "3h": {"trail_pct": 4.0, "be_trigger_pct": 3.0, "ttp_trigger_pct": 5.0, "ttp_callback_pct": 1.0, "target_pct": 3.0, "desc": "3h pozičný"},
    "4h": {"trail_pct": 5.5, "be_trigger_pct": 4.0, "ttp_trigger_pct": 7.0, "ttp_callback_pct": 1.5, "target_pct": 3.5, "desc": "4h masívny swing"}
}

# Top 20 likvidných tokenov pre autonómne skenovanie
TOP_20_TOKENS = [
    "BTC", "ETH", "SOL", "SUI", "AVAX", "NEAR", "LINK", "DOGE", "XRP", "ADA",
    "DOT", "MATIC", "UNI", "ATOM", "LTC", "SHIB", "FET", "RENDER", "OP", "ARB"
]

ACTIVE_TRADES = {}
DAILY_PNL_CALENDAR = []
CLOSED_TRADES_HISTORY = []
SYSTEM_LOGS = []
CURRENT_STAGE = "STAV: Mimo obchodu. Čakám na signál..."
LAST_TRADE_TIME = {}
MAX_PARALLEL_TRADES = 1
trade_lock = asyncio.Lock()

IS_BOT_ACTIVE = True
CURRENT_TIMEFRAME = "15M"  # Predvolený TF
STORAGE_FILE = "trades_history.json"
ALLOWED_STABLES = ["USDC", "USDT"]

SETTINGS = TIMEFRAME_PROFILES["15m"].copy()
SETTINGS.update({
    "use_btc_filter": True, 
    "reentry_cooldown_sec": 120, 
    "use_breakeven": True,
    "max_budget_cap": 3000.0, # Predvolené nastavenie na 3000 USD
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

# --- 🧠 VÝPOČET INDIKÁTORA V PYTHONU (ALL GREEN & VOLUME OSCILLATOR) ---
def evaluate_all_green_strategy(candles) -> bool:
    """
    Vyhodnotí Heikin-Ashi + Volume Oscillator podmienku 'All Green'
    candles: zoznam sviečok z OKX API (najstaršia po najnovšiu alebo obrátene, tu predpokladáme štandard OKX: index 0 je najnovšia)
    OKX formát candles: [ts, o, h, l, c, vol, ...]
    """
    if not candles or len(candles) < 30:
        return False
    
    # OKX vrací dáta od najnovšej po najstaršiu, otočíme ich chronologicky
    sorted_candles = list(candles)
    sorted_candles.reverse()
    
    ha_opens = []
    ha_closes = []
    volumes = []
    
    for i, c in enumerate(sorted_candles):
        o = safe_float(c[1])
        h = safe_float(c[2])
        l = safe_float(c[3])
        cl = safe_float(c[4])
        vol = safe_float(c[5])
        
        ha_close = (o + h + l + cl) / 4.0
        if i == 0:
            ha_open = (o + cl) / 2.0
        else:
            ha_open = (ha_opens[i-1] + ha_closes[i-1]) / 2.0
            
        ha_opens.append(ha_open)
        ha_closes.append(ha_close)
        
        # Volume direction
        vol_dir = vol if ha_close > ha_open else -vol
        volumes.append(vol_dir)

    # Výpočet EMA (fast_len=5, slow_len=20, signal_len=9)
    fast_len = 5
    slow_len = 20
    signal_len = 9
    
    def calculate_ema(data, period):
        emas = []
        multiplier = 2 / (period + 1)
        for idx, val in enumerate(data):
            if idx == 0:
                emas.append(val)
            else:
                emas.append((val - emas[-1]) * multiplier + emas[-1])
        return emas

    fast_ma = calculate_ema(volumes, fast_len)
    slow_ma = calculate_ema(volumes, slow_len)
    
    oscillator = [f - s for f, s in zip(fast_ma, slow_ma)]
    signal_line = calculate_ema(oscillator, signal_len)
    
    # Kontrola poslednej uzavretej sviečky (index -2, lebo index -1 je práve prebiehajúca živá sviečka)
    if len(oscillator) < 3:
        return False
        
    osc_val = oscillator[-2]
    sig_val = signal_line[-2]
    
    # All Green podmienka: oscillator > 0 a zároveň oscillator > signal_line (rastúci momentum)
    cond_volume_bullish = (osc_val > 0) and (osc_val > sig_val)
    
    return cond_volume_bullish

# --- 🤖 AUTONÓMNY SKENER TOP 20 TOKENOV ---
async def autonomous_market_scanner():
    global ACTIVE_TRADES
    await asyncio.sleep(5)
    log_event("🟢 Autonómny Multi-Asset Sklener pre Top 20 tokenov bol spustený.")
    
    while True:
        try:
            if not IS_BOT_ACTIVE or len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES:
                await asyncio.sleep(15)
                continue
                
            if not check_btc_health():
                log_event("⚠️ Autonómny skener: Nákup pozastavený klesajúcim trendom na BTC.")
                await asyncio.sleep(60)
                continue

            # Skenovanie všetkých 20 tokenov
            for coin in TOP_20_TOKENS:
                if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES or not IS_BOT_ACTIVE:
                    break
                    
                okx_symbol = f"{coin}-USDC"
                
                # Zamedzenie zacyklenia (cooldown)
                now_ts = datetime.now().timestamp()
                if coin in LAST_TRADE_TIME and (now_ts - LAST_TRADE_TIME[coin] < SETTINGS["reentry_cooldown_sec"]):
                    continue

                # Stiahnutie 15m sviečok z OKX
                res = okx_request("GET", f"/api/v5/market/candles?instId={okx_symbol}&bar=15m&limit=50")
                if res.get("code") == "0" and res.get("data"):
                    candles = res["data"]
                    # Vyhodnotenie All Green indikátora
                    is_all_green = evaluate_all_green_strategy(candles)
                    
                    if is_all_green:
                        log_event(f"🎯 AUTONÓMNY SIGNÁL 'ALL GREEN' DETEGOVANÝ PRE {okx_symbol}!")
                        
                        # Vykonanie nákupu
                        LAST_TRADE_TIME[coin] = now_ts
                        free_capital = fetch_account_capital("USDC")
                        budget_cap = SETTINGS.get("max_budget_cap", 3000.0)
                        
                        if SETTINGS.get("compounding_enabled", False):
                            safe_budget = min(free_capital * 0.98, budget_cap)
                        else:
                            safe_budget = min(free_capital * 0.98, budget_cap)
                            
                        if safe_budget < 5.0:
                            continue
                            
                        ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={okx_symbol}")
                        if ticker_res.get("code") != "0" or not ticker_res.get("data"):
                            continue
                        current_price = safe_float(ticker_res["data"][0]["last"])
                        coin_amount = round(safe_budget / current_price, 2)
                        
                        order_body = json.dumps({
                            "instId": okx_symbol, "tdMode": "cash", "side": "buy",
                            "ordType": "market", "sz": f"{coin_amount}"
                        })
                        
                        order_res = okx_request("POST", "/api/v5/trade/order", order_body)
                        if order_res.get("code") == "0":
                            actual_spent_usdc = coin_amount * current_price
                            sl_price = current_price * (1 - 0.006)  # Prísny stop-loss -0.6%
                            be_price = current_price * (1 + (SETTINGS["be_trigger_pct"] / 100.0))
                            target_pct = SETTINGS.get("target_pct", 0.65)
                            
                            ACTIVE_TRADES[coin] = {
                                "entry_price": current_price,
                                "current_price": current_price,
                                "current_sl_price": sl_price,
                                "be_target_price": be_price,
                                "ttp_target_price": 0,
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
                            
                            log_event(f"🛒 Autonómny nákup [{CURRENT_TIMEFRAME}]: {okx_symbol} za {current_price:.4f} | Investícia: {actual_spent_usdc:.2f}$")
                            send_telegram_msg(f"🟢 <b>AUTONÓMNY NÁKUP [TF: {CURRENT_TIMEFRAME}]</b>\n<b>Pár:</b> {okx_symbol}\n<b>Vstup:</b> {current_price:.4f}\n<b>Investícia:</b> {actual_spent_usdc:.2f} USDC")
                            break # Keďže máme limit 1 obchod naraz, po nákupe prerušíme skenovanie cyklu
                            
                await asyncio.sleep(2) # Krátka pauza medzi dotazmi na jednotlivé coiny
                
        except Exception as e:
            log_event(f"⚠️ Chyba v autonómnom skeneri: {e}")
            
        await asyncio.sleep(30) # Skenovanie prebieha každých 30 sekúnd

async def advanced_risk_monitor_loop():
    global ACTIVE_TRADES, CURRENT_STAGE
    await asyncio.sleep(3)
    while True:
        if not ACTIVE_TRADES:
            if IS_BOT_ACTIVE:
                CURRENT_STAGE = "STAV: Bot aktívny. Skenujem Top 20 tokenov (All Green / Scaling Out)..."
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

                # --- ⏳ GTC 48h Expirácia ---
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
                    
                    target_pct = trade_settings.get("target_pct", 0.65)
                    
                    if target_pct > 0:
                        # --- 🎯 50/50 SCALING OUT LOGIKA ---
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

                        # --- 🛡️ BREAK-EVEN & TRAILING STOP LOGIKA ---
                        be_trigger = trade_settings.get("be_trigger_pct", 0.6)
                        if be_trigger > 0:
                            be_price = trade["entry_price"] * (1 + (be_trigger / 100.0))
                            trail_stop_price = trade["highest_price"] * (1 - (trade_settings["trail_pct"] / 100.0))
                            
                            if pnl_pct >= be_trigger:
                                trade["break_even_activated"] = True
                                effective_sl = max(trail_stop_price, trade["entry_price"])
                            else:
                                effective_sl = trade["entry_price"] * (1 - 0.006)  # Prísny stop-loss -0.6%
                                
                            trade["live_pnl_pct"] = pnl_pct
                            trade["live_pnl_usdc"] = pnl_usdc
                            trade["be_target_price"] = be_price
                            trade["current_sl_price"] = effective_sl
                            
                            second_half_invested = invested / 2.0 if trade.get("partial_sold") else invested
                            trade["second_half_pnl_usdc"] = second_half_invested * (pnl_pct / 100.0)
                            
                            partial_status_text = "50% ODPREDANÉ (TRAIL AKTÍVNY)" if trade.get("partial_sold") else f"ČAKÁ NA +{target_pct}%"
                            CURRENT_STAGE = f"STAV: {symbol} | Hodnota: {current_total_value:.2f}$ | PnL: {pnl_pct:+.2f}% | {partial_status_text}"
                            
                            # --- 🚨 STOP LOSS / BREAK-EVEN EXEKÚCIA ---
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
        except Exception:
            pass
        await asyncio.sleep(2)

async def execute_trade_in_background(data: dict):
    global ACTIVE_TRADES, LAST_TRADE_TIME, CURRENT_TIMEFRAME, SETTINGS
    async with trade_lock:
        try:
            req_secret = str(data.get("secret", "")).strip()
            if req_secret != WEBHOOK_SECRET:
                return
            action = str(data.get("action", "")).lower().strip()
            tv_ticker = str(data.get("symbol", data.get("ticker", ""))).upper().strip()
            coin_name, okx_symbol = format_tv_ticker(tv_ticker)
            
            if action == "buy":
                if not IS_BOT_ACTIVE or len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES or coin_name in ACTIVE_TRADES:
                    return
                free_capital = fetch_account_capital("USDC")
                budget_cap = SETTINGS.get("max_budget_cap", 3000.0)
                safe_budget = min(free_capital * 0.98, budget_cap)
                
                ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={okx_symbol}")
                if ticker_res.get("code") != "0": return
                current_price = safe_float(ticker_res["data"][0]["last"])
                coin_amount = round(safe_budget / current_price, 2)
                
                order_body = json.dumps({"instId": okx_symbol, "tdMode": "cash", "side": "buy", "ordType": "market", "sz": f"{coin_amount}"})
                order_res = okx_request("POST", "/api/v5/trade/order", order_body)
                if order_res.get("code") != "0": return
                
                actual_spent_usdc = coin_amount * current_price
                sl_price = current_price * (1 - 0.006)
                
                ACTIVE_TRADES[coin_name] = {
                    "entry_price": current_price, "current_price": current_price, "current_sl_price": sl_price,
                    "amount": coin_amount, "initial_amount": coin_amount, "bought_at": get_sk_time_str(),
                    "market_pair": okx_symbol, "highest_price": current_price, "partial_sold": False,
                    "invested_usdc": actual_spent_usdc, "live_pnl_pct": 0.0, "live_pnl_usdc": 0.0, "tf": CURRENT_TIMEFRAME, "settings": SETTINGS.copy()
                }
                log_event(f"🛒 Manuálny webhook nákup: {okx_symbol}")
        except Exception as e:
            log_event(f"❌ Chyba webhooku: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_persistent_data()
    log_event("🚀 Apex Scalper PRO v5.8 (Plne autonómny Multi-Asset Sklener) spustený!")
    scanner_task = asyncio.create_task(autonomous_market_scanner())
    monitor_task = asyncio.create_task(advanced_risk_monitor_loop())
    send_telegram_msg("🚀 <b>Apex Scalper PRO v5.8 spustený v autonómnom režime pre Top 20 tokenov!</b>")
    yield
    scanner_task.cancel()
    monitor_task.cancel()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
@app.post("/api/webhook")
async def tradingview_webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    background_tasks.add_task(execute_trade_in_background, data)
    return {"status": "accepted"}

@app.post("/api/telegram-webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        if "callback_query" in data:
            cb = data["callback_query"]
            if cb.get("data", "") == "sell_now":
                await sell_now()
    except Exception:
        pass
    return {"status": "ok"}

@app.post("/api/toggle-bot")
async def toggle_bot():
    global IS_BOT_ACTIVE, CURRENT_STAGE
    IS_BOT_ACTIVE = not IS_BOT_ACTIVE
    status_str = "aktívny" if IS_BOT_ACTIVE else "pozastavený"
    CURRENT_STAGE = f"STAV: Bot je {status_str}."
    return JSONResponse({"status": "success", "is_bot_active": IS_BOT_ACTIVE})

@app.post("/api/toggle-btc-filter")
async def toggle_btc_filter():
    SETTINGS["use_btc_filter"] = not SETTINGS.get("use_btc_filter", True)
    return JSONResponse({"status": "success", "use_btc_filter": SETTINGS["use_btc_filter"]})

@app.post("/api/toggle-compounding")
async def toggle_compounding():
    SETTINGS["compounding_enabled"] = not SETTINGS.get("compounding_enabled", False)
    return JSONResponse({"status": "success", "compounding_enabled": SETTINGS["compounding_enabled"]})

@app.post("/api/set-budget/{amount}")
async def set_budget(amount: int):
    if amount in [500, 1000, 3000, 5000, 10000]:
        SETTINGS["max_budget_cap"] = float(amount)
        return JSONResponse({"status": "success", "max_budget_cap": amount})
    return JSONResponse({"status": "error", "message": "Neplatná hodnota."})

@app.post("/api/set-tf/{tf_val}")
async def set_tf(tf_val: str):
    global CURRENT_TIMEFRAME, SETTINGS
    tf_lower = tf_val.lower()
    if tf_lower in TIMEFRAME_PROFILES:
        CURRENT_TIMEFRAME = tf_val.upper()
        SETTINGS.update(TIMEFRAME_PROFILES[tf_lower])
        return JSONResponse({"status": "success", "tf": CURRENT_TIMEFRAME})
    return JSONResponse({"status": "error", "message": "Neplatný TF."})

@app.post("/api/manual-buy/{coin}")
async def manual_buy(coin: str):
    global ACTIVE_TRADES, LAST_TRADE_TIME
    async with trade_lock:
        try:
            if not IS_BOT_ACTIVE: return JSONResponse({"status": "error", "message": "Bot je pozastavený."})
            coin_upper = coin.upper().strip()
            if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES or coin_upper in ACTIVE_TRADES:
                return JSONResponse({"status": "error", "message": "Pozícia už je aktívna."})
            
            okx_symbol = f"{coin_upper}-USDC"
            free_capital = fetch_account_capital("USDC")
            budget_cap = SETTINGS.get("max_budget_cap", 3000.0)
            safe_budget = min(free_capital * 0.98, budget_cap)
            
            ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={okx_symbol}")
            if ticker_res.get("code") != "0": return JSONResponse({"status": "error", "message": "Chyba ceny."})
            
            current_price = safe_float(ticker_res["data"][0]["last"])
            coin_amount = round(safe_budget / current_price, 2)
            
            order_res = okx_request("POST", "/api/v5/trade/order", json.dumps({"instId": okx_symbol, "tdMode": "cash", "side": "buy", "ordType": "market", "sz": f"{coin_amount}"}))
            if order_res.get("code") != "0": return JSONResponse({"status": "error", "message": order_res.get('msg')})
            
            actual_spent_usdc = coin_amount * current_price
            ACTIVE_TRADES[coin_upper] = {
                "entry_price": current_price, "current_price": current_price, "current_sl_price": current_price * (1 - 0.006),
                "amount": coin_amount, "initial_amount": coin_amount, "bought_at": get_sk_time_str(),
                "market_pair": okx_symbol, "highest_price": current_price, "partial_sold": False,
                "invested_usdc": actual_spent_usdc, "live_pnl_pct": 0.0, "live_pnl_usdc": 0.0, "tf": CURRENT_TIMEFRAME, "settings": SETTINGS.copy()
            }
            return JSONResponse({"status": "success", "symbol": okx_symbol})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

@app.post("/api/sell-now")
async def sell_now():
    global ACTIVE_TRADES
    async with trade_lock:
        sold = []
        for coin in list(ACTIVE_TRADES.keys()):
            trade = ACTIVE_TRADES[coin]
            okx_request("POST", "/api/v5/trade/order", json.dumps({"instId": trade["market_pair"], "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(round(trade["amount"], 2))}))
            record_closed_trade(trade["market_pair"], trade.get("live_pnl_pct", 0), trade.get("live_pnl_usdc", 0), "Emergency Sell")
            sold.append(trade["market_pair"])
            del ACTIVE_TRADES[coin]
        return JSONResponse({"status": "success", "sold": sold})

@app.get("/api/top-tokens")
async def get_top_tokens():
    return JSONResponse({"tokens": TOP_20_TOKENS})

HTML_TEMPLATE_CONTENT = """<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Apex Scalper PRO v5.8 Terminal</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root { --bg-color: #08090c; --card-bg: #111318; --border-color: #1f232b; --border-hover: #2e3440; --text-main: #eceff4; --text-muted: #6c788e; --accent-white: #ffffff; --green: #10b981; --red: #ef4444; --yellow: #f59e0b; }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'JetBrains Mono', monospace; }
        body { background-color: var(--bg-color); color: var(--text-main); padding: 16px; min-height: 100vh; display: flex; flex-direction: column; }
        .container { max-width: 1250px; width: 100%; margin: 0 auto; display: flex; flex-direction: column; gap: 14px; padding-bottom: 30px; }
        .top-bar { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); padding-bottom: 12px; flex-wrap: wrap; gap: 12px; }
        .brand h1 { font-size: 1.05rem; font-weight: 700; color: var(--accent-white); }
        .brand p { font-size: 0.7rem; color: var(--text-muted); margin-top: 3px; text-transform: uppercase; display: flex; gap: 12px; }
        .balance-tag { color: var(--yellow); font-weight: 700; }
        .actions { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
        .btn { background: transparent; border: 1px solid var(--border-hover); color: var(--text-main); padding: 5px 10px; font-size: 0.7rem; font-weight: 600; cursor: pointer; border-radius: 4px; text-transform: uppercase; }
        .btn:hover { background: rgba(255,255,255,0.03); }
        .btn-on { border-color: var(--green); color: var(--green); }
        .btn-off { border-color: var(--text-muted); color: var(--text-muted); }
        .btn-yellow { border-color: var(--yellow) !important; color: var(--yellow) !important; background: rgba(245, 158, 11, 0.12) !important; font-weight: 700 !important; }
        .btn-sell { background: var(--accent-white); color: var(--bg-color); font-weight: 700; }
        .btn-sell:disabled { opacity: 0.25; cursor: not-allowed; background: transparent; color: var(--text-muted); }
        .status-ticker { font-size: 0.75rem; color: var(--yellow); background: var(--card-bg); border: 1px solid var(--border-color); padding: 9px 14px; border-radius: 5px; display: flex; align-items: center; gap: 10px; font-weight: 600; }
        .status-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--green); box-shadow: 0 0 10px var(--green); }
        .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
        .stat-card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 5px; padding: 12px 14px; }
        .stat-title { font-size: 0.65rem; color: var(--text-muted); text-transform: uppercase; }
        .stat-value { font-size: 1.15rem; font-weight: 700; margin-top: 4px; color: var(--accent-white); }
        .section-title { font-size: 0.72rem; text-transform: uppercase; color: var(--text-muted); margin-top: 8px; margin-bottom: 5px; font-weight: 600; }
        .terminal-card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 5px; overflow: hidden; }
        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.75rem; }
        th { color: var(--text-muted); font-weight: 500; padding: 9px 12px; border-bottom: 1px solid var(--border-color); font-size: 0.65rem; text-transform: uppercase; background: var(--card-bg); }
        td { padding: 10px 12px; border-bottom: 1px solid var(--border-color); color: var(--text-main); }
        .scrollable-card { max-height: 180px; overflow-y: auto; }
        .logs-box { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 5px; padding: 10px 12px; height: 200px; overflow-y: auto; font-size: 0.7rem; color: var(--text-muted); display: flex; flex-direction: column; gap: 6px; }
        .log-line { white-space: pre-wrap; word-break: break-all; border-bottom: 1px dashed rgba(255,255,255,0.03); padding-bottom: 4px; }
        .text-green { color: var(--green); font-weight: 700; }
        .text-red { color: var(--red); font-weight: 700; }
        .text-yellow-highlight { color: var(--yellow); font-weight: 700; }
        .custom-buy-box { display: flex; align-items: center; gap: 4px; border: 1px solid var(--border-hover); padding: 2px 6px; border-radius: 4px; }
        .custom-buy-input { background: transparent; border: none; color: var(--text-main); font-size: 0.7rem; width: 65px; outline: none; text-transform: uppercase; font-weight: 600; }
        .select-token { background: var(--card-bg); border: 1px solid var(--border-hover); color: var(--text-main); padding: 4px 8px; font-size: 0.7rem; border-radius: 4px; outline: none; font-weight: 600; }
    </style>
</head>
<body>
    <div class="container">
        <div class="top-bar">
            <div class="brand">
                <h1>APEX SCALPER // PRO v5.8 (Autonómny Sklener Top 20)</h1>
                <p>
                    <span id="header-pair">15M All Green · OKX Demo</span>
                    <span>Zostatok: <span id="header-balance" class="balance-tag">-- USDC</span></span>
                    <span>Aktivny TF: <span id="header-tf" class="balance-tag">{{ CURRENT_TIMEFRAME }}</span></span>
                </p>
            </div>
            <div class="actions">
                <button class="tf-btn btn {% if CURRENT_TIMEFRAME == '15M' %}btn-yellow{% endif %}" data-tf="15m" onclick="setTf('15m')">15M</button>
                <button class="budget-btn btn {% if SETTINGS.max_budget_cap == 500 %}btn-yellow{% endif %}" data-val="500" onclick="setBudget(500)">500</button>
                <button class="budget-btn btn {% if SETTINGS.max_budget_cap == 1000 %}btn-yellow{% endif %}" data-val="1000" onclick="setBudget(1000)">1K</button>
                <button class="budget-btn btn {% if SETTINGS.max_budget_cap == 3000 %}btn-yellow{% endif %}" data-val="3000" onclick="setBudget(3000)">3K</button>
                <button class="budget-btn btn {% if SETTINGS.max_budget_cap == 5000 %}btn-yellow{% endif %}" data-val="5000" onclick="setBudget(5000)">5K</button>
                <button class="budget-btn btn {% if SETTINGS.max_budget_cap == 10000 %}btn-yellow{% endif %}" data-val="10000" onclick="setBudget(10000)">10K</button>

                <select id="top-token-select" class="select-token" onchange="buySelectedToken()">
                    <option value="">-- Manuálny výber Top 20 --</option>
                </select>

                <div class="custom-buy-box">
                    <input type="text" id="custom-coin" class="custom-buy-input" placeholder="BTC/ETH">
                    <button class="btn btn-on" style="padding: 2px 6px;" onclick="manualBuyCustom()">🚀 BUY</button>
                </div>

                <button id="compounding-btn" class="btn {% if SETTINGS.compounding_enabled %}btn-yellow{% endif %}" onclick="toggleCompounding()">COMPOUNDING: OFF</button>
                <button id="btc-btn" class="btn {% if SETTINGS.use_btc_filter %}btn-on{% else %}btn-off{% endif %}" onclick="toggleBtcFilter()">BTC: ON</button>
                <button id="bot-btn" class="btn {% if IS_BOT_ACTIVE %}btn-on{% else %}btn-off{% endif %}" onclick="toggleBot()">● ACTIVE</button>
                <button id="sell-btn" class="btn btn-sell" onclick="sellNow()" disabled>⚡ EMERGENCY SELL</button>
            </div>
        </div>

        <div class="status-ticker">
            <div class="status-dot"></div>
            <span id="current-stage">{{ CURRENT_STAGE }}</span>
        </div>

        <div class="stats-grid">
            <div class="stat-card"><div class="stat-title">Win Rate</div><div class="stat-value" id="stat-winrate">0.0%</div></div>
            <div class="stat-card"><div class="stat-title">Total Trades</div><div class="stat-value" id="stat-trades">0</div></div>
            <div class="stat-card"><div class="stat-title">Win / Loss</div><div class="stat-value" id="stat-winloss">0W / 0L</div></div>
            <div class="stat-card"><div class="stat-title">Net PnL</div><div class="stat-value" id="stat-pnl">+0.00 $</div></div>
        </div>

        <div>
            <div class="section-title">Active Position & Scaling Out Matrix (50% / 50%)</div>
            <div class="terminal-card">
                <table>
                    <thead>
                        <tr><th>Pair / TF</th><th>Entry</th><th>Current</th><th>Scale-Out Status (50%)</th><th>Remaining Size</th><th>2. Half Live PnL</th><th>Total PnL</th></tr>
                    </thead>
                    <tbody id="active-position-tbody">
                        <tr><td colspan="7" style="text-align: center; color: var(--text-muted);">Autonómny skener prechádza Top 20 tokenov na 15M...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div>
            <div class="section-title">Execution History</div>
            <div class="terminal-card scrollable-card">
                <table>
                    <thead><tr><th>Timestamp</th><th>Pair</th><th>Trigger Reason</th><th>Net PnL</th></tr></thead>
                    <tbody id="history-tbody"><tr><td colspan="4" style="text-align: center; color: var(--text-muted);">Zatiaľ žiadne ukončené obchody.</td></tr></tbody>
                </table>
            </div>
        </div>

        <div>
            <div class="section-title">System Execution Logs</div>
            <div class="logs-box" id="logs-container"><div class="log-line">[System Initialized] Autonómny režim pripravený.</div></div>
        </div>
    </div>

    <script>
        async function loadTopTokens() {
            let res = await fetch('/api/top-tokens');
            let data = await res.json();
            let select = document.getElementById('top-token-select');
            let html = '<option value="">-- Manuálny výber Top 20 --</option>';
            data.tokens.forEach(t => { html += `<option value="${t}">${t}</option>`; });
            select.innerHTML = html;
        }

        function buySelectedToken() {
            let select = document.getElementById('top-token-select');
            if (!select.value) return;
            manualBuy(select.value);
            select.value = "";
        }

        async function toggleBot() {
            let res = await fetch('/api/toggle-bot', {method: 'POST'});
            let data = await res.json();
            let btn = document.getElementById('bot-btn');
            btn.className = data.is_bot_active ? "btn btn-on" : "btn btn-off";
            btn.innerText = data.is_bot_active ? "● ACTIVE" : "○ PAUSED";
            updateState();
        }

        async function toggleBtcFilter() { await fetch('/api/toggle-btc-filter', {method: 'POST'}); }
        async function toggleCompounding() { await fetch('/api/toggle-compounding', {method: 'POST'}); }

        async function setBudget(amount) {
            let res = await fetch(`/api/set-budget/${amount}`, {method: 'POST'});
            if ((await res.json()).status === 'success') {
                document.querySelectorAll('.budget-btn').forEach(b => {
                    b.classList.remove('btn-yellow');
                    if (parseInt(b.getAttribute('data-val')) === amount) b.classList.add('btn-yellow');
                });
            }
        }

        async function setTf(tfVal) { await fetch(`/api/set-tf/${tfVal}`, {method: 'POST'}); }

        async function manualBuy(coin) {
            if (!confirm(`Naozaj vykonať nákup ${coin}-USDC?`)) return;
            let res = await fetch(`/api/manual-buy/${coin}`, {method: 'POST'});
            let data = await res.json();
            if (data.status === 'success') updateState();
            else alert("Chyba: " + data.message);
        }

        async function manualBuyCustom() {
            let inp = document.getElementById('custom-coin');
            if (inp.value.trim()) { manualBuy(inp.value.trim()); inp.value = ""; }
        }

        async function sellNow() {
            if (!confirm("Núdzový predaj všetkých pozícií?")) return;
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
                        let secondHalfPnl = t.second_half_pnl_usdc || 0;
                        let statusText = t.partial_sold ? "✅ 50% ODPREDANÉ (TRAIL)" : "⏳ ČAKÁ NA CIEĽ";
                        html += `<tr><td><b>${t.market_pair}</b></td><td>${t.entry_price.toFixed(4)}</td><td>${t.current_price.toFixed(4)}</td><td><span class="text-yellow-highlight">${statusText}</span></td><td>${t.amount.toFixed(2)}</td><td class="text-yellow-highlight">${secondHalfPnl >= 0 ? "+" : ""}${secondHalfPnl.toFixed(2)} $</td><td class="${pnlClass}">${t.live_pnl_pct >= 0 ? "+" : ""}${t.live_pnl_pct.toFixed(2)}%</td></tr>`;
                    }
                    activeTbody.innerHTML = html;
                    sellBtn.disabled = false;
                } else {
                    activeTbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">Autonómny skener prechádza Top 20 tokenov na 15M...</td></tr>`;
                    sellBtn.disabled = true;
                }

                let historyTbody = document.getElementById('history-tbody');
                if (data.history.length > 0) {
                    let hHtml = "";
                    for (let h of data.history) {
                        let pnlClass = h.pnl_usdc >= 0 ? "text-green" : "text-red";
                        hHtml += `<tr><td>${h.time}</td><td><b>${h.pair}</b></td><td>${h.reason}</td><td class="${pnlClass}">${h.pnl_usdc >= 0 ? "+" : ""}${h.pnl_usdc.toFixed(2)} $</td></tr>`;
                    }
                    historyTbody.innerHTML = hHtml;
                }

                let logsBox = document.getElementById('logs-container');
                let lHtml = "";
                data.logs.forEach(l => { lHtml += `<div class="log-line">${l}</div>`; });
                logsBox.innerHTML = lHtml;
            } catch (err) {}
        }

        loadTopTokens();
        setInterval(updateState, 2000);
        updateState();
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return Template(HTML_TEMPLATE_CONTENT).render(SETTINGS=SETTINGS, IS_BOT_ACTIVE=IS_BOT_ACTIVE, CURRENT_TIMEFRAME=CURRENT_TIMEFRAME, CURRENT_STAGE=CURRENT_STAGE)

@app.get("/api/state")
async def get_state():
    return JSONResponse({"balance": fetch_account_capital("USDC"), "is_bot_active": IS_BOT_ACTIVE, "current_stage": CURRENT_STAGE, "stats": calculate_stats(), "active_trades": ACTIVE_TRADES, "history": CLOSED_TRADES_HISTORY[:20], "logs": SYSTEM_LOGS})

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
