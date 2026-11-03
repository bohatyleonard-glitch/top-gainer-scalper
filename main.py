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
MAX_PARALLEL_TRADES = 1  # 🛡️ 2. Kapacitný limit paralelných obchodov (nastaviteľný na 1-4)
trade_lock = asyncio.Lock()  # 🛡️ Ochrana proti súbehu (Race Condition)
IS_ORDER_PROCESSING = False

IS_BOT_ACTIVE = True
CURRENT_STRATEGY_MODE = "Agresívny Momentum (Breakout)"
CURRENT_TIMEFRAME = "15m"

PEAK_PORTFOLIO_VALUE = 0.0
MAX_DRAWDOWN_PCT = 0.0

SETTINGS = {
    "fast_len": 5,
    "slow_len": 20,
    "signal_len": 9,
    "trail_pct": 1.5,
    "use_breakeven": True,
    "be_trigger_pct": 1.2,
    "use_trailing_tp": True,
    "ttp_trigger_pct": 1.2,
    "ttp_callback_pct": 0.4,
    "min_vol": 10000.0,
    "min_change": -50.0,
    "max_change": 100.0,
    "use_btc_filter": True,       # BTC Trend Guard
    "use_dynamic_sizing": True,   # 🛡️ 3. Dynamické určovanie veľkosti podľa kapitálu
    "use_weekend_filter": False,
    "reentry_cooldown_sec": 120,  # 🛡️ 5. 120-sekundová ochrana
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
        "x-simulated-trading": "1",  # 🛡️ 9. Demo prostredie
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
    return 1000.0

def check_btc_health() -> bool:
    """🛡️ Filter pre BTC Trend Guard"""
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

# --- 3. & 12. DYNAMICKÁ ALOKÁCIA KAPITÁLU A OCHRANA PROTI DROBENIU ---
def calculate_position_budget(free_capital: float, current_price: float, inst_id: str) -> float:
    """Vypočíta 25% rozpočet (alebo 98% pri jedinej pozícii) a ošetria minimálnu čiastku"""
    if MAX_PARALLEL_TRADES == 1:
        base_budget = free_capital * 0.98
    else:
        base_budget = free_capital * 0.25  # 🛡️ 3. Dynamická alokácia 25% na obchod

    # 🛡️ 12. Ochrana proti drobeniu (minimálna hodnota obchodu 5 USDC)
    if base_budget < 5.0:
        return 0.0

    return base_budget

# --- 10. NEPZETRŽITÝ MONITOROVACÍ CYKLUS (RISK MANAGEMENT) ---
async def advanced_risk_monitor_loop():
    global ACTIVE_TRADES, CURRENT_STAGE, PRICE_HISTORY
    await asyncio.sleep(3)
    print(f"[{get_sk_time_str()}] 🛡️ 10. Risk Management Monitor spustený na pozadí...", flush=True)

    while True:
        if not ACTIVE_TRADES:
            await asyncio.sleep(2)
            continue

        try:
            now_time = datetime.now(timezone.utc) + timedelta(hours=1)
            for coin in list(ACTIVE_TRADES.keys()):
                if coin not in ACTIVE_TRADES:
                    continue
                trade = ACTIVE_TRADES[coin]
                symbol = trade["market_pair"]
                coin_base = symbol.split("-")[0]

                # 🛡️ 8. GTC Expirácia pozície po 48 hodinách
                bought_time = datetime.strptime(trade["bought_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=1)))
                if now_time - bought_time > timedelta(hours=48):
                    print(f"⏳ [8. GTC EXSPIRÁCIA 48H] Zatváram neaktívnu pozíciu {symbol}", flush=True)
                    try:
                        ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={symbol}")
                        curr_p = safe_float(ticker_res['data'][0]['last']) if ticker_res.get('code') == '0' and ticker_res.get('data') else trade["entry_price"]
                        
                        # 🛡️ 4. Aggressive Limit IOC pre predaj
                        limit_exit_price = curr_p * 0.998
                        order_body = json.dumps({
                            "instId": symbol, "tdMode": "cash", "side": "sell",
                            "ordType": "limit", "px": str(round(limit_exit_price, 4)),
                            "sz": str(trade["amount"]), "timeInForce": "ioc"
                        })
                        okx_request("POST", "/api/v5/trade/order", order_body)
                        pnl_pct = ((curr_p - trade["entry_price"]) / trade["entry_price"]) * 100
                        pnl_usdc = trade.get("invested_usdc", 100.0) * (pnl_pct / 100.0)

                        record_closed_trade(symbol, pnl_pct, pnl_usdc, "GTC Expirácia 48h")
                        send_telegram_msg(
                            f"⏳ <b>[GTC EXSPIRÁCIA (48h)]</b>\n"
                            f"<b>Pár:</b> {symbol}\n"
                            f"<b>Výstup:</b> {curr_p:.4f}\n"
                            f"<b>Výsledok:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)"
                        )
                    except Exception as err:
                        print(f"❌ Chyba pri GTC exspirácii pre {coin}: {err}", flush=True)
                    del ACTIVE_TRADES[coin]
                    continue

                ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={symbol}")
                if ticker_res.get("code") == "0" and ticker_res.get("data"):
                    current_price = safe_float(ticker_res["data"][0]["last"])
                    if current_price <= 0:
                        continue

                    pnl_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
                    pnl_usdc = trade.get("invested_usdc", 100.0) * (pnl_pct / 100.0)

                    trade["current_price"] = current_price
                    trade["live_pnl_pct"] = pnl_pct
                    trade["live_pnl_usdc"] = pnl_usdc

                    # 🛡️ 7. Dynamický Take Profit +3.5% a výber 50% zisku
                    if pnl_pct >= 3.5 and not trade.get("partial_tp_activated", False):
                        half_amount = trade["amount"] / 2
                        print(f"💰 [7. DYNAMICKÝ TP +3.5%] Predávam 50% pozície pre {symbol}", flush=True)
                        try:
                            limit_exit_price = current_price * 0.998
                            order_body = json.dumps({
                                "instId": symbol, "tdMode": "cash", "side": "sell",
                                "ordType": "limit", "px": str(round(limit_exit_price, 4)),
                                "sz": str(round(half_amount, 4)), "timeInForce": "ioc"
                            })
                            res = okx_request("POST", "/api/v5/trade/order", order_body)
                            if res.get("code") == "0":
                                trade["amount"] -= half_amount
                                trade["partial_tp_activated"] = True
                                # 🛡️ 7. Posun Stop-Lossu na Break-Even (+0.5%)
                                trade["current_sl_price"] = trade["entry_price"] * 1.005
                                trade["break_even_activated"] = True

                                send_telegram_msg(
                                    f"💰 <b>[DYNAMICKÝ TP +3.5%]</b>\n"
                                    f"<b>Pár:</b> {symbol}\n"
                                    f"<b>Predaných:</b> 50% pozície\n"
                                    f"🔒 <i>Stop-Loss pre zvyšok posunutý na Break-Even (+0.5%).</i>"
                                )
                        except Exception as err:
                            print(f"❌ Chyba exekúcie dynamického TP pre {coin}: {err}", flush=True)

                    # 🛡️ 6. Pevný Stop-Loss (-3%) alebo Break-Even Ochrana
                    if current_price <= trade["current_sl_price"]:
                        reason = "[STOP-LOSS -3%]" if trade["current_sl_price"] < trade["entry_price"] else "[BREAK-EVEN OCHRANA]"
                        try:
                            limit_exit_price = current_price * 0.998
                            order_body = json.dumps({
                                "instId": symbol, "tdMode": "cash", "side": "sell",
                                "ordType": "limit", "px": str(round(limit_exit_price, 4)),
                                "sz": str(round(trade["amount"], 4)), "timeInForce": "ioc"
                            })
                            okx_request("POST", "/api/v5/trade/order", order_body)
                            record_closed_trade(symbol, pnl_pct, pnl_usdc, reason)

                            send_telegram_msg(
                                f"🚨 <b>UZAVRETÝ OBCHOD ({reason})</b>\n"
                                f"<b>Pár:</b> {symbol}\n"
                                f"<b>Vstup:</b> {trade['entry_price']:.4f}\n"
                                f"<b>Výstup:</b> {current_price:.4f}\n"
                                f"<b>Výsledok:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)"
                            )
                        except Exception as err:
                            print(f"❌ Chyba SL: {err}", flush=True)
                        del ACTIVE_TRADES[coin]
                        CURRENT_STAGE = f"STAV: Mimo obchodu. Pozícia zatvorená ({reason})."
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
                # 🛡️ 5. Ochrana 120s proti zacykleniu
                if coin_name in LAST_TRADE_TIME and (now_ts - LAST_TRADE_TIME[coin_name] < SETTINGS["reentry_cooldown_sec"]):
                    print(f"⚠️ 5. IGNOROVANÝ NÁKUP: Pre {coin_name} prebehol nákup pred menej ako 120 sekundami.", flush=True)
                    return

                # 🛡️ 2. Limit paralelných obchodov
                if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES or coin_name in ACTIVE_TRADES:
                    print(f"⚠️ 2. NÁKUP PRESKOČENÝ: Dosiahnutý limit pozícií ({len(ACTIVE_TRADES)}/{MAX_PARALLEL_TRADES}).", flush=True)
                    return

                # 🛡️ Filter BTC Guard
                if not check_btc_health():
                    print("⚠️ NÁKUP BLOKOVANÝ: BTC Trend Guard detegoval pokles Bitcoinu.", flush=True)
                    return

                LAST_TRADE_TIME[coin_name] = now_ts
                stable_ccy = "USDC" if "USDC" in okx_symbol else "USDT"

                # 🛡️ 11. Overenie zostatku na OKX
                free_capital = fetch_account_capital(stable_ccy)
                
                # 🛡️ 3. & 12. Dynamický rozpočet a poistka minimálnej čiastky
                safe_budget = calculate_position_budget(free_capital, 0.0, okx_symbol)
                if safe_budget <= 0:
                    print(f"❌ 12. Nedostatok prostriedkov alebo rozpočet pod minimom: {free_capital} {stable_ccy}", flush=True)
                    return

                ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={okx_symbol}")
                if ticker_res.get("code") != "0" or not ticker_res.get("data"):
                    print(f"❌ Chyba získať cenu pre {okx_symbol}: {ticker_res}", flush=True)
                    return

                current_price = safe_float(ticker_res["data"][0]["last"])
                coin_amount = safe_budget / current_price

                # 🛡️ 4. Aggressive Limit Nákup (+0.2% nad trh s IOC)
                limit_buy_price = current_price * 1.002
                print(f"🛒 4. POSIELAM IOC AGGRESSIVE LIMIT BUY na {okx_symbol} za {limit_buy_price:.4f}...", flush=True)

                order_body = json.dumps({
                    "instId": okx_symbol,
                    "tdMode": "cash",
                    "side": "buy",
                    "ordType": "limit",
                    "px": str(round(limit_buy_price, 4)),
                    "sz": str(round(coin_amount, 4)),
                    "timeInForce": "ioc"  # 🛡️ 4. Immediate-Or-Cancel
                })

                order_res = okx_request("POST", "/api/v5/trade/order", order_body)
                if order_res.get("code") != "0":
                    print(f"❌ Chyba nákupnej objednávky: {order_res}", flush=True)
                    return

                # 🛡️ 6. Pevný Stop-Loss na -3%
                sl_price = current_price * 0.97
                ACTIVE_TRADES[coin_name] = {
                    "entry_price": current_price,
                    "current_sl_price": sl_price,
                    "amount": coin_amount,
                    "bought_at": get_sk_time_str(),
                    "market_pair": okx_symbol,
                    "highest_price": current_price,
                    "partial_tp_activated": False,
                    "break_even_activated": False,
                    "invested_usdc": safe_budget,
                    "live_pnl_pct": 0.0,
                    "live_pnl_usdc": 0.0
                }

                send_telegram_msg(
                    f"🟢 <b>OTVORENÝ NOVÝ OBCHOD (BUY - WEBHOOK)</b>\n"
                    f"<b>Pár:</b> {okx_symbol}\n"
                    f"<b>Vstupná cena:</b> {current_price:.4f}\n"
                    f"<b>Investícia:</b> {safe_budget:.2f} {stable_ccy}\n"
                    f"<b>Stop-Loss (-3%):</b> {sl_price:.4f}"
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
                    print(f"🚨 PREDAJ Z WEBHOOKU: Ukončujem {pair_to_sell}...", flush=True)

                    ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={pair_to_sell}")
                    curr_p = safe_float(ticker_res['data'][0]['last']) if ticker_res.get('code') == '0' and ticker_res.get('data') else trade["entry_price"]

                    # 🛡️ 4. Aggressive Limit Predaj (-0.2% pod trh s IOC)
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

                        send_telegram_msg(
                            f"🔴 <b>UZAVRETÝ OBCHOD (WEBHOOK PREDAJ)</b>\n"
                            f"<b>Pár:</b> {pair_to_sell}\n"
                            f"<b>Výstup:</b> {curr_p:.4f}\n"
                            f"<b>Výsledok:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)"
                        )
                        del ACTIVE_TRADES[target_coin]
        except Exception as e:
            print(f"❌ CHYBA EXEKÚCIE WEBHOOKU: {e}", flush=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    monitor_task = asyncio.create_task(advanced_risk_monitor_loop())
    send_telegram_msg("🚀 <b>Apex Scalper PRO v5.4.5 spustený. Všetkých 12 pravidiel aktívnych!</b>")
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

        # 🛡️ Ignorovanie zbytočných SELL správ ak nie sme v obchode
        if action == "sell" and coin_name not in ACTIVE_TRADES:
            return {"status": "ignored_no_active_trade"}

        background_tasks.add_task(execute_trade_in_background, data)
        return {"status": "accepted"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/emergency-sell")
async def emergency_sell():
    """🚨 Núdzové okamžité zatvorenie pozícií"""
    global ACTIVE_TRADES
    async with trade_lock:
        sold = []
        for coin in list(ACTIVE_TRADES.keys()):
            trade = ACTIVE_TRADES[coin]
            symbol = trade["market_pair"]
            try:
                order_body = json.dumps({
                    "instId": symbol, "tdMode": "cash", "side": "sell",
                    "ordType": "market", "sz": str(round(trade["amount"], 4))
                })
                okx_request("POST", "/api/v5/trade/order", order_body)
                record_closed_trade(symbol, trade.get("live_pnl_pct", 0), trade.get("live_pnl_usdc", 0), "Kill Switch")
                sold.append(symbol)
            except Exception as e:
                print(f"Chyba kill switch: {e}", flush=True)
            del ACTIVE_TRADES[coin]
        return JSONResponse({"status": "success", "sold": sold})

HTML_TEMPLATE_CONTENT = """<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8"><title>Apex Scalper v5.4.5 PRO</title>
    <style>
        body { font-family: 'Inter', sans-serif; background: #090d16; color: #f3f4f6; padding: 20px; }
        .container { max-width: 900px; margin: 0 auto; background: rgba(17,24,39,0.9); border: 1px solid rgba(0,240,255,0.2); border-radius: 16px; padding: 20px; }
        .title { color: #00f0ff; font-weight: 800; font-size: 1.2rem; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th { color: #00f0ff; text-align: left; padding: 8px; border-bottom: 1px solid rgba(255,255,255,0.1); }
        td { padding: 8px; border-bottom: 1px solid rgba(255,255,255,0.05); }
        .pnl-green { color: #00ff88; font-weight: bold; } .pnl-red { color: #ff4444; font-weight: bold; }
    </style>
</head>
<body>
    <div class="container">
        <div class="title">⚡ Apex Scalper v5.4.5 PRO - CyberHUD</div>
        <p>{{ current_stage }}</p>
        <h3>🔴 Aktívne obchody</h3>
        {% if trades %}
            <table>
                <tr><th>Pár</th><th>Vstup</th><th>Množstvo</th><th>SL Cena</th><th>PnL (%)</th></tr>
                {% for t, d in trades.items() %}
                <tr><td>{{ d.market_pair }}</td><td>{{ d.entry_price }}</td><td>{{ "%.4f"|format(d.amount) }}</td><td>{{ "%.4f"|format(d.current_sl_price) }}</td><td class="{{ 'pnl-green' if d.live_pnl_pct >= 0 else 'pnl-red' }}">{{ "%+.2f"|format(d.live_pnl_pct) }}%</td></tr>
                {% endfor %}
            </table>
        {% else %}<p>Žiadne aktívne obchody.</p>{% endif %}
        <h3>📜 História obchodov</h3>
        <table>
            <tr><th>Čas</th><th>Pár</th><th>Dôvod</th><th>PnL ($)</th></tr>
            {% for h in history %}
            <tr><td>{{ h.time }}</td><td>{{ h.pair }}</td><td>{{ h.reason }}</td><td class="{{ 'pnl-green' if h.pnl_usdc >= 0 else 'pnl-red' }}">{{ "%+.2f"|format(h.pnl_usdc) }} $</td></tr>
            {% endfor %}
        </table>
    </div>
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
