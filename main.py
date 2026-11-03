import os
import hmac
import base64
import json
import asyncio
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn
import requests
from jinja2 import Template

# --- 🔑 9. OKX DEMO KĽÚČE ---
OKX_API_KEY = os.getenv("OKX_API_KEY", "638848ca-e65c-4074-bcfd-20cf4d292f2c")
OKX_SECRET = os.getenv("OKX_SECRET", "885934366C54F33B276E9E49E8EF87CF")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "Australia111/")

# --- 📱 TELEGRAM NOTIFIKÁCIE ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9021881475:AAFn1oQSvaGkhpX2uyJscdcTybFgk1tNmV8")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "9130979784")

def send_telegram_msg(message: str):
    """Pomocná funkcia pre odosielanie správ na Telegram"""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("DOPLŇ"):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=5)
    except Exception as err:
        print(f"❌ Chyba pri odosielaní Telegram notifikácie: {err}", flush=True)

# --- 🟢 1. POVOLENÝ WATCHLIST (TOP 20/30 PROFILY & USDC PÁRY) ---
WATCHLIST_SOL = ["SOL"]
ALLOWED_STABLES = ["USDC", "USDT"]

# --- GLOBAL STAV BOTA & LIMITOV ---
ACTIVE_TRADES = {}
MARKET_RADAR = []
DAILY_PNL_CALENDAR = {}
CLOSED_TRADES_HISTORY = []
PRICE_HISTORY = []
CURRENT_STAGE = "STAV: Mimo obchodu. Skenujem trh a čakám na webhooky..."
LAST_TRADE_TIME = {}  # 🛡️ 5. 120s Zámok proti zacykleniu
MAX_PARALLEL_TRADES = 1  # 🛡️ 2. Kapacitný limit paralelných obchodov
trade_lock = asyncio.Lock()  # 🛡️ Ochrana proti súbehu
IS_ORDER_PROCESSING = False

# 🔘 HLAVNÝ PREPÍNAČ BOTA (ZAPNUTÝ / VYPNUTÝ)
IS_BOT_ACTIVE = True

CURRENT_STRATEGY_MODE = "Agresívny Momentum (Breakout)"
CURRENT_TIMEFRAME = "15m"

PEAK_PORTFOLIO_VALUE = 0.0
MAX_DRAWDOWN_PCT = 0.0

SETTINGS = {
    "fast_len": 3,
    "slow_len": 20,
    "signal_len": 9,
    "trail_pct": 0.7,
    "use_breakeven": True,
    "be_trigger_pct": 0.6,
    "use_trailing_tp": True,
    "ttp_trigger_pct": 0.8,
    "ttp_callback_pct": 0.2,
    "min_vol": 10000.0,
    "min_change": -50.0,
    "max_change": 100.0,
    "use_btc_filter": True,
    "use_dynamic_sizing": True,
    "use_weekend_filter": False,
    "reentry_cooldown_sec": 120,
    "webhook_secret": "moje_tajne_heslo_123"
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

def format_tv_ticker(raw_ticker: str):
    clean = raw_ticker.replace("OKX:", "").replace("/", "").replace("-", "").strip().upper()
    for stable in ALLOWED_STABLES:
        if clean.endswith(stable) and clean != stable:
            coin = clean[:-len(stable)]
            return coin, f"{coin}-{stable}"
    return clean, f"{clean}-USDC"

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
    if len(CLOSED_TRADES_HISTORY) > 30:
        CLOSED_TRADES_HISTORY.pop()

# --- 9. OKX EEA DEMO PROSTREDIE & AUTENTIFIKÁCIA ---
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

# --- 11. ZISŤOVANIE REÁLNEHO ZOSTATKU CEZ API ---
def fetch_account_capital(stable_ccy="USDC") -> float:
    try:
        balance_res = okx_request("GET", "/api/v5/account/balance")
        if balance_res.get("code") == "0":
            for acc in balance_res.get("data", []):
                for detail in acc.get("details", []):
                    if detail.get("ccy") == stable_ccy:
                        avail = safe_float(detail.get("availBal", 0))
                        update_drawdown_stats(avail)
                        return avail
    except Exception as err:
        print(f"⚠️ Chyba pri načítaní zostatku: {err}", flush=True)
    return 3000.0

def check_btc_health() -> bool:
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

def calculate_position_budget(free_capital: float, current_price: float, inst_id: str) -> float:
    if MAX_PARALLEL_TRADES == 1:
        base_budget = free_capital * 0.98
    else:
        base_budget = free_capital * 0.25

    if base_budget < 5.0:
        return 0.0

    return base_budget

# --- 10. NEPRETRŽITÝ MONITOROVACÍ CYKLUS (RISK MANAGEMENT & HLADINY) ---
async def advanced_risk_monitor_loop():
    global ACTIVE_TRADES, CURRENT_STAGE
    await asyncio.sleep(3)
    print(f"[{get_sk_time_str()}] 🛡️ Risk Management Monitor spustený...", flush=True)

    while True:
        if not ACTIVE_TRADES:
            if IS_BOT_ACTIVE:
                CURRENT_STAGE = "STAV: Bot ZAPNUTÝ. Skenujem trh a čakám na webhooky..."
            else:
                CURRENT_STAGE = "STAV: Bot VYPNUTÝ (Pozastavený). Ignoruje nákupné webhooky."
            await asyncio.sleep(2)
            continue

        try:
            now_time = datetime.now(timezone.utc) + timedelta(hours=1)
            for coin in list(ACTIVE_TRADES.keys()):
                if coin not in ACTIVE_TRADES:
                    continue
                trade = ACTIVE_TRADES[coin]
                symbol = trade["market_pair"]

                # GTC Expirácia 48h
                bought_time = datetime.strptime(trade["bought_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=1)))
                if now_time - bought_time > timedelta(hours=48):
                    print(f"⏳ [GTC EXSPIRÁCIA 48H] Zatváram {symbol}", flush=True)
                    try:
                        ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={symbol}")
                        curr_p = safe_float(ticker_res['data'][0]['last']) if ticker_res.get('code') == '0' and ticker_res.get('data') else trade["entry_price"]
                        limit_exit_price = curr_p * 0.998
                        order_body = json.dumps({
                            "instId": symbol, "tdMode": "cash", "side": "sell",
                            "ordType": "limit", "px": str(round(limit_exit_price, 4)),
                            "sz": str(round(trade["amount"], 4)), "timeInForce": "ioc"
                        })
                        okx_request("POST", "/api/v5/trade/order", order_body)
                        pnl_pct = ((curr_p - trade["entry_price"]) / trade["entry_price"]) * 100
                        pnl_usdc = trade.get("invested_usdc", 100.0) * (pnl_pct / 100.0)
                        record_closed_trade(symbol, pnl_pct, pnl_usdc, "GTC Expirácia 48h")
                    except Exception as err:
                        print(f"❌ Chyba GTC: {err}", flush=True)
                    del ACTIVE_TRADES[coin]
                    continue

                ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={symbol}")
                if ticker_res.get("code") == "0" and ticker_res.get("data"):
                    current_price = safe_float(ticker_res["data"][0]["last"])
                    if current_price <= 0:
                        continue

                    # Aktualizácia najvyššej dosiahnutej ceny (Peak Price)
                    if current_price > trade["highest_price"]:
                        trade["highest_price"] = current_price

                    pnl_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
                    pnl_usdc = trade.get("invested_usdc", 100.0) * (pnl_pct / 100.0)

                    # 🎯 VÝPOČET ŽIVÝCH HLADÍN DYNAMICKY PRE CYBERHUD
                    be_price = trade["entry_price"] * (1 + (SETTINGS["be_trigger_pct"] / 100.0))
                    trail_stop_price = trade["highest_price"] * (1 - (SETTINGS["trail_pct"] / 100.0))
                    
                    if pnl_pct >= SETTINGS["be_trigger_pct"]:
                        trade["break_even_activated"] = True
                        effective_sl = math.max(trail_stop_price, trade["entry_price"])
                    else:
                        effective_sl = trade["entry_price"] * 0.97  # Pevný SL -3%

                    ttp_activation_price = trade["entry_price"] * (1 + (SETTINGS["ttp_trigger_pct"] / 100.0))
                    if current_price >= ttp_activation_price:
                        trade["ttp_activated"] = True

                    ttp_stop_level = trade["highest_price"] * (1 - (SETTINGS["ttp_callback_pct"] / 100.0)) if trade.get("ttp_activated") else ttp_activation_price

                    # Uloženie dynamických hladín pre zobrazenie v UI
                    trade["current_price"] = current_price
                    trade["live_pnl_pct"] = pnl_pct
                    trade["live_pnl_usdc"] = pnl_usdc
                    trade["be_target_price"] = be_price
                    trade["current_sl_price"] = effective_sl
                    trade["ttp_target_price"] = ttp_stop_level

                    CURRENT_STAGE = f"STAV: 🟢 Aktívny obchod {symbol} | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)"

                    # Vyhodnotenie Stop-Loss / Break-Even exekúcie
                    if current_price <= effective_sl:
                        reason = "[BREAK-EVEN OCHRANA]" if effective_sl >= trade["entry_price"] else "[STOP-LOSS -3%]"
                        try:
                            limit_exit_price = current_price * 0.998
                            order_body = json.dumps({
                                "instId": symbol, "tdMode": "cash", "side": "sell",
                                "ordType": "limit", "px": str(round(limit_exit_price, 4)),
                                "sz": str(round(trade["amount"], 4)), "timeInForce": "ioc"
                            })
                            okx_request("POST", "/api/v5/trade/order", order_body)
                            record_closed_trade(symbol, pnl_pct, pnl_usdc, reason)
                            send_telegram_msg(f"🚨 <b>UZAVRETÝ OBCHOD ({reason})</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                        except Exception as err:
                            print(f"❌ Chyba SL: {err}", flush=True)
                        del ACTIVE_TRADES[coin]
                        continue

                    # Vyhodnotenie Trailing Take Profit Exitu
                    if trade.get("ttp_activated") and current_price <= ttp_stop_level:
                        reason = "[TRAILING TAKE PROFIT EXIT]"
                        try:
                            limit_exit_price = current_price * 0.998
                            order_body = json.dumps({
                                "instId": symbol, "tdMode": "cash", "side": "sell",
                                "ordType": "limit", "px": str(round(limit_exit_price, 4)),
                                "sz": str(round(trade["amount"], 4)), "timeInForce": "ioc"
                            })
                            okx_request("POST", "/api/v5/trade/order", order_body)
                            record_closed_trade(symbol, pnl_pct, pnl_usdc, reason)
                            send_telegram_msg(f"🎯 <b>UZAVRETÝ OBCHOD ({reason})</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
                        except Exception as err:
                            print(f"❌ Chyba TTP: {err}", flush=True)
                        del ACTIVE_TRADES[coin]
                        continue

        except Exception as main_err:
            print(f"❌ Chyba v monitorovaní: {main_err}", flush=True)

        await asyncio.sleep(2)

# --- WEBHOOK SPRACUJÚCI SIGNÁLY Z TRADINGVIEW ---
async def execute_trade_in_background(data: dict):
    global ACTIVE_TRADES, LAST_TRADE_TIME, IS_ORDER_PROCESSING
    async with trade_lock:
        try:
            action = str(data.get("action", "")).lower().strip()
            tv_ticker = str(data.get("symbol", data.get("ticker", ""))).upper().strip()
            coin_name, okx_symbol = format_tv_ticker(tv_ticker)

            now_ts = datetime.now().timestamp()

            if action == "buy":
                if not IS_BOT_ACTIVE:
                    print("⚠️ IGNOROVANÝ NÁKUP: Bot je momentálne VYPNUTÝ v ovládacom paneli.", flush=True)
                    return

                if coin_name in LAST_TRADE_TIME and (now_ts - LAST_TRADE_TIME[coin_name] < SETTINGS["reentry_cooldown_sec"]):
                    print(f"⚠️ IGNOROVANÝ NÁKUP: Pre {coin_name} prebehol nákup pred menej ako 120s.", flush=True)
                    return

                if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES or coin_name in ACTIVE_TRADES:
                    print(f"⚠️ NÁKUP PRESKOČENÝ: Dosiahnutý limit pozícií ({len(ACTIVE_TRADES)}/{MAX_PARALLEL_TRADES}).", flush=True)
                    return

                if not check_btc_health():
                    print("⚠️ NÁKUP BLOKOVANÝ: BTC Trend Guard detegoval pokles.", flush=True)
                    return

                LAST_TRADE_TIME[coin_name] = now_ts
                stable_ccy = "USDC" if "USDC" in okx_symbol else "USDT"

                free_capital = fetch_account_capital(stable_ccy)
                safe_budget = calculate_position_budget(free_capital, 0.0, okx_symbol)
                if safe_budget <= 0:
                    print(f"❌ Nedostatok prostriedkov: {free_capital} {stable_ccy}", flush=True)
                    return

                ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={okx_symbol}")
                if ticker_res.get("code") != "0" or not ticker_res.get("data"):
                    print(f"❌ Chyba ceny: {ticker_res}", flush=True)
                    return

                current_price = safe_float(ticker_res["data"][0]["last"])
                coin_amount = safe_budget / current_price

                limit_buy_price = current_price * 1.002
                order_body = json.dumps({
                    "instId": okx_symbol, "tdMode": "cash", "side": "buy",
                    "ordType": "limit", "px": str(round(limit_buy_price, 4)),
                    "sz": str(round(coin_amount, 4)), "timeInForce": "ioc"
                })

                order_res = okx_request("POST", "/api/v5/trade/order", order_body)
                if order_res.get("code") != "0":
                    print(f"❌ Chyba nákupu: {order_res}", flush=True)
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
                    "live_pnl_usdc": 0.0
                }

                send_telegram_msg(
                    f"🟢 <b>OTVORENÝ NOVÝ OBCHOD (BUY)</b>\n"
                    f"<b>Pár:</b> {okx_symbol}\n"
                    f"<b>Vstupná cena:</b> {current_price:.4f}\n"
                    f"<b>Investícia:</b> {safe_budget:.2f} {stable_ccy}\n"
                    f"🎯 <b>Break-Even (+0.6%):</b> {be_price:.4f}\n"
                    f"🎯 <b>Trailing TP (+0.8%):</b> {ttp_price:.4f}"
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

                    limit_sell_price = curr_p * 0.998
                    order_body = json.dumps({
                        "instId": pair_to_sell, "tdMode": "cash", "side": "sell",
                        "ordType": "limit", "px": str(round(limit_sell_price, 4)),
                        "sz": str(round(trade["amount"], 4)), "timeInForce": "ioc"
                    })

                    order_res = okx_request("POST", "/api/v5/trade/order", order_body)
                    if order_res.get("code") == "0":
                        pnl_pct = ((curr_p - trade["entry_price"]) / trade["entry_price"]) * 100
                        pnl_usdc = trade.get("invested_usdc", 100.0) * (pnl_pct / 100.0)
                        record_closed_trade(pair_to_sell, pnl_pct, pnl_usdc, "Predaj z Webhooku")
                        send_telegram_msg(f"🔴 <b>UZAVRETÝ OBCHOD (WEBHOOK PREDAJ)</b>\n<b>Pár:</b> {pair_to_sell}\n<b>Výsledok:</b> {pnl_pct:+.2f}%")
                        del ACTIVE_TRADES[target_coin]
        except Exception as e:
            print(f"❌ CHYBA WEBHOOKU: {e}", flush=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    monitor_task = asyncio.create_task(advanced_risk_monitor_loop())
    send_telegram_msg("🚀 <b>Apex Scalper PRO v5.4.5 spustený s novým ovládaním!</b>")
    yield
    monitor_task.cancel()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
@app.post("/api/webhook")
async def tradingview_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        print(f"📥 PRIJATÝ WEBHOOK: {data}", flush=True)
        action = str(data.get("action", "")).lower().strip()
        tv_ticker = str(data.get("symbol", data.get("ticker", ""))).upper().strip()
        coin_name, okx_symbol = format_tv_ticker(tv_ticker)

        if action == "sell" and coin_name not in ACTIVE_TRADES:
            return {"status": "ignored_no_active_trade"}

        background_tasks.add_task(execute_trade_in_background, data)
        return {"status": "accepted"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# 🔘 1. NOVÝ ENDPOINT PRE PREPÍNANIE ZAPNUTÉ / VYPNUTÉ
@app.post("/api/toggle-bot")
async def toggle_bot():
    global IS_BOT_ACTIVE, CURRENT_STAGE
    IS_BOT_ACTIVE = not IS_BOT_ACTIVE
    status_str = "ZAPNUTÝ" if IS_BOT_ACTIVE else "VYPNUTÝ"
    CURRENT_STAGE = f"STAV: Bot je teraz {status_str}."
    send_telegram_msg(f"⚙️ <b>ZMENA STAVU BOTA:</b> Bot bol ručne {status_str}.")
    return JSONResponse({"status": "success", "is_bot_active": IS_BOT_ACTIVE})

# 🔘 2. NOVÝ ENDPOINT PRE OKAMŽITÝ RUČNÝ PREDAJ (SELL BUTTON)
@app.post("/api/sell-now")
async def sell_now():
    global ACTIVE_TRADES
    async with trade_lock:
        if not ACTIVE_TRADES:
            return JSONResponse({"status": "error", "message": "Žiadny aktívny obchod na predaj."})
        
        sold = []
        for coin in list(ACTIVE_TRADES.keys()):
            trade = ACTIVE_TRADES[coin]
            symbol = trade["market_pair"]
            try:
                ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={symbol}")
                curr_p = safe_float(ticker_res['data'][0]['last']) if ticker_res.get('code') == '0' and ticker_res.get('data') else trade["entry_price"]
                
                limit_sell_price = curr_p * 0.998
                order_body = json.dumps({
                    "instId": symbol, "tdMode": "cash", "side": "sell",
                    "ordType": "limit", "px": str(round(limit_sell_price, 4)),
                    "sz": str(round(trade["amount"], 4)), "timeInForce": "ioc"
                })
                okx_request("POST", "/api/v5/trade/order", order_body)
                
                pnl_pct = ((curr_p - trade["entry_price"]) / trade["entry_price"]) * 100
                pnl_usdc = trade.get("invested_usdc", 100.0) * (pnl_pct / 100.0)
                record_closed_trade(symbol, pnl_pct, pnl_usdc, "Ručný okamžitý predaj (UI SELL)")
                
                send_telegram_msg(
                    f"⚡ <b>OKAMŽITÝ RUČNÝ PREDAJ (UI BUTTON)</b>\n"
                    f"<b>Pár:</b> {symbol}\n"
                    f"<b>Predané pri cene:</b> {curr_p:.4f}\n"
                    f"<b>PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)"
                )
                sold.append(symbol)
            except Exception as e:
                print(f"Chyba pri ručnom predaji: {e}", flush=True)
            del ACTIVE_TRADES[coin]

        return JSONResponse({"status": "success", "sold": sold})

HTML_TEMPLATE_CONTENT = """<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Apex Scalper v5.4.5 PRO - Command Center</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Inter', sans-serif; background: #070a12; color: #e5e7eb; padding: 24px; min-height: 100vh; }
        .container { max-width: 1100px; margin: 0 auto; }
        
        .header-card { 
            background: linear-gradient(135deg, rgba(17,24,39,0.95), rgba(15,23,42,0.8)); 
            border: 1px solid rgba(0,240,255,0.25); 
            border-radius: 20px; padding: 24px; 
            margin-bottom: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.5);
            display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 16px;
        }
        .title-area h1 { color: #00f0ff; font-weight: 800; font-size: 1.6rem; letter-spacing: -0.5px; }
        .title-area p { color: #9ca3af; font-size: 0.9rem; margin-top: 4px; }
        
        .controls-group { display: flex; gap: 12px; align-items: center; }
        
        .btn { 
            border: none; border-radius: 12px; padding: 12px 24px; font-weight: 700; 
            font-size: 0.95rem; cursor: pointer; transition: all 0.2s ease; display: inline-flex; align-items: center; gap: 8px;
        }
        .btn-toggle-on { background: #10b981; color: #ffffff; box-shadow: 0 0 15px rgba(16,185,129,0.4); }
        .btn-toggle-on:hover { background: #059669; }
        .btn-toggle-off { background: #6b7280; color: #ffffff; }
        .btn-toggle-off:hover { background: #4b5563; }
        
        .btn-sell-now { background: linear-gradient(135deg, #ef4444, #dc2626); color: white; box-shadow: 0 0 15px rgba(239,68,68,0.4); }
        .btn-sell-now:hover { background: #b91c1c; transform: translateY(-1px); }
        .btn-sell-now:disabled { opacity: 0.4; cursor: not-allowed; box-shadow: none; transform: none; }
        
        .status-banner {
            background: rgba(17, 24, 39, 0.7); border-left: 4px solid #00f0ff;
            padding: 14px 20px; border-radius: 8px; margin-bottom: 24px; font-weight: 600; color: #38bdf8;
        }

        .card { 
            background: rgba(17,24,39,0.8); border: 1px solid rgba(255,255,255,0.08); 
            border-radius: 16px; padding: 20px; margin-bottom: 24px; 
        }
        .card-header { font-size: 1.1rem; font-weight: 700; color: #f3f4f6; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }
        
        table { width: 100%; border-collapse: collapse; text-align: left; }
        th { color: #9ca3af; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.5px; padding: 12px; border-bottom: 1px solid rgba(255,255,255,0.1); }
        td { padding: 14px 12px; border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 0.95rem; }
        
        .badge { padding: 4px 10px; border-radius: 6px; font-size: 0.8rem; font-weight: 700; }
        .badge-sol { background: rgba(168,85,247,0.2); color: #c084fc; border: 1px solid rgba(168,85,247,0.4); }
        
        .level-highlight { font-weight: 600; color: #fbbf24; }
        .sl-highlight { color: #f87171; font-weight: 600; }
        .tp-highlight { color: #34d399; font-weight: 600; }

        .pnl-green { color: #34d399; font-weight: 800; } 
        .pnl-red { color: #f87171; font-weight: 800; }
        
        .empty-state { text-align: center; color: #6b7280; padding: 30px; font-style: italic; }
    </style>
</head>
<body>
    <div class="container">
        <!-- HEADER -->
        <div class="header-card">
            <div class="title-area">
                <h1>⚡ APEX SCALPER v5.4.5 PRO</h1>
                <p>SOL-USDC 15m Scalping System | OKX Demo Mode</p>
            </div>
            <div class="controls-group">
                <!-- 🔘 1. ZAPNÚŤ / VYPNUTÚŤ BOTA -->
                <button id="toggleBtn" onclick="toggleBot()" class="btn {{ 'btn-toggle-on' if is_bot_active else 'btn-toggle-off' }}">
                    {{ '🟢 BOT AKTÍVNY' if is_bot_active else '🔴 BOT VYPNUTÝ' }}
                </button>
                
                <!-- 🔘 2. OKAMŽITÝ PREDAJ (SELL NOW) -->
                <button id="sellBtn" onclick="sellNow()" class="btn btn-sell-now" {{ '' if trades else 'disabled' }}>
                    ⚡ SELL NOW (PREDAŤ)
                </button>
            </div>
        </div>

        <!-- STAV BANNER -->
        <div class="status-banner">
            {{ current_stage }}
        </div>

        <!-- AKTÍVNE OBCHODY & ŽIVÉ HLADINY -->
        <div class="card">
            <div class="card-header">🔴 Aktívny Obchod & Živé Hladiny (15m Wave Tracking)</div>
            {% if trades %}
                <table>
                    <thead>
                        <tr>
                            <th>Pár</th>
                            <th>Vstupná cena</th>
                            <th>Aktuálna cena</th>
                            <th>Break-Even (+0.6%)</th>
                            <th>Trailing Stop (0.7%)</th>
                            <th>Trailing TP (+0.8%)</th>
                            <th>Živý PnL</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for t, d in trades.items() %}
                        <tr>
                            <td><span class="badge badge-sol">{{ d.market_pair }}</span></td>
                            <td><b>{{ "%.4f"|format(d.entry_price) }} $</b></td>
                            <td><b style="color:#00f0ff;">{{ "%.4f"|format(d.current_price) }} $</b></td>
                            <td class="level-highlight">{{ "%.4f"|format(d.be_target_price) }} $</td>
                            <td class="sl-highlight">{{ "%.4f"|format(d.current_sl_price) }} $</td>
                            <td class="tp-highlight">{{ "%.4f"|format(d.ttp_target_price) }} $</td>
                            <td class="{{ 'pnl-green' if d.live_pnl_pct >= 0 else 'pnl-red' }}">
                                {{ "%+.2f"|format(d.live_pnl_pct) }}% ({{ "%+.2f"|format(d.live_pnl_usdc) }} $)
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            {% else %}
                <div class="empty-state">Momentálne nie je otvorená žiadna pozícia. Bot čaká na breakout signál.</div>
            {% endif %}
        </div>

        <!-- HISTÓRIA OBCHODOV -->
        <div class="card">
            <div class="card-header">📜 História Uzavretých Obchodov</div>
            {% if history %}
                <table>
                    <thead>
                        <tr>
                            <th>Čas exekúcie</th>
                            <th>Pár</th>
                            <th>Dôvod ukončenia</th>
                            <th>Čistý PnL ($)</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for h in history %}
                        <tr>
                            <td style="color:#9ca3af;">{{ h.time }}</td>
                            <td><b>{{ h.pair }}</b></td>
                            <td><span class="badge" style="background:rgba(255,255,255,0.05); color:#d1d5db;">{{ h.reason }}</span></td>
                            <td class="{{ 'pnl-green' if h.pnl_usdc >= 0 else 'pnl-red' }}">
                                {{ "%+.2f"|format(h.pnl_usdc) }} $ ({{ "%+.2f"|format(h.pnl_pct) }}%)
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            {% else %}
                <div class="empty-state">Žiadne uzavreté obchody v histórii.</div>
            {% endif %}
        </div>
    </div>

    <script>
        async function toggleBot() {
            try {
                const res = await fetch('/api/toggle-bot', { method: 'POST' });
                const data = await res.json();
                if(data.status === 'success') {
                    location.reload();
                }
            } catch(e) { alert('Chyba pri zmene stavu bota: ' + e); }
        }

        async function sellNow() {
            if(!confirm("Naozaj chceš okamžite predaj aktívny obchod za aktuálnu trhovú cenu?")) return;
            try {
                const res = await fetch('/api/sell-now', { method: 'POST' });
                const data = await res.json();
                if(data.status === 'success') {
                    alert('Obchod bol okamžite uzavretý!');
                    location.reload();
                } else {
                    alert('Chyba: ' + data.message);
                }
            } catch(e) { alert('Chyba pri okamžitom predaji: ' + e); }
        }

        // Automatické obnovovanie panela každé 3 sekundy
        setTimeout(() => { location.reload(); }, 3000);
    </script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    template = Template(HTML_TEMPLATE_CONTENT)
    return template.render(
        trades=ACTIVE_TRADES,
        history=CLOSED_TRADES_HISTORY,
        current_stage=CURRENT_STAGE,
        is_bot_active=IS_BOT_ACTIVE
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
