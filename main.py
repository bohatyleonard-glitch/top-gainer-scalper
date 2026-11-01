import os
import time
import hmac
import base64
import json
import asyncio
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn
import requests
from jinja2 import Template

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
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        requests.post(url, json=payload, timeout=5)
    except Exception as err:
        print(f"❌ Chyba pri odosielaní Telegram notifikácie: {err}", flush=True)

# --- STAV BOT-A & PROFILY ---
ACTIVE_TRADES = {}       
MARKET_RADAR = []        
DAILY_PNL_CALENDAR = {}  
CLOSED_TRADES_HISTORY = [] 
PRICE_HISTORY = []       
CURRENT_STAGE = "STAV: Mimo obchodu. Skenujem trh..."
LAST_TRADE_TIME = {}  
MAX_PARALLEL_TRADES = 1  # 🛡️ Anti-Fragmentation: Výhradne 1 obchod naraz
trade_lock = asyncio.Lock()  

CURRENT_MODE = "AGGRESSIVE" # AGGRESSIVE / CONSERVATIVE
ALLOWED_CURRENCIES = ["USDC", "USDT", "EUR"]

def get_sk_time_str() -> str:
    sk_time = datetime.now(timezone.utc) + timedelta(hours=1)
    return sk_time.strftime("%Y-%m-%d %H:%M:%S")

def get_sk_date_str() -> str:
    sk_time = datetime.now(timezone.utc) + timedelta(hours=1)
    return sk_time.strftime("%Y-%m-%d")

def record_closed_trade(pair, pnl_pct, pnl_usdc, reason):
    global CLOSED_TRADES_HISTORY
    day_str = get_sk_date_str()
    if day_str not in DAILY_PNL_CALENDAR:
        DAILY_PNL_CALENDAR[day_str] = {"pnl": 0.0, "trades": 0}
    DAILY_PNL_CALENDAR[day_str]["pnl"] += pnl_usdc
    DAILY_PNL_CALENDAR[day_str]["trades"] += 1

    CLOSED_TRADES_HISTORY.insert(0, {
        "pair": pair,
        "pnl_pct": pnl_pct,
        "pnl_usdc": pnl_usdc,
        "reason": reason,
        "time": get_sk_time_str()
    })
    if len(CLOSED_TRADES_HISTORY) > 20:
        CLOSED_TRADES_HISTORY.pop()

# --- OKX EEA DEMO PROSTREDIE & AUTENTIFIKÁCIA ---
def okx_request(method: str, path: str, body: str = ''):
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    message = timestamp + method.upper() + path + body
    mac = hmac.new(OKX_SECRET.encode('utf-8'), message.encode('utf-8'), digestmod='sha256')
    signature = base64.b64encode(mac.digest()).decode('utf-8')
    
    headers = {
        'OK-ACCESS-KEY': OKX_API_KEY,
        'OK-ACCESS-SIGN': signature,
        'OK-ACCESS-TIMESTAMP': timestamp,
        'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
        'x-simulated-trading': '1',
        'Content-Type': 'application/json'
    }
    
    url = 'https://eea.okx.com' + path
    try:
        if method.upper() == 'GET':
            response = requests.get(url, headers=headers, timeout=10)
        else:
            response = requests.post(url, headers=headers, data=body, timeout=10)
        return response.json()
    except Exception as err:
        return {"code": "-1", "msg": str(err)}

# --- 🔍 AUTOMATICKÝ SKENER TRHU (SMART LIMIT + 1H COOLDOWN) ---
async def top_gainer_scanner_loop():
    global ACTIVE_TRADES, LAST_TRADE_TIME, MARKET_RADAR, CURRENT_STAGE, PRICE_HISTORY, CURRENT_MODE
    await asyncio.sleep(5)
    
    start_msg = f"[{get_sk_time_str()}] ⚡ APEX SCALPER PRO SPUSTENÝ [Smart Limit USDC Režim]"
    print(start_msg, flush=True)
    send_telegram_msg(f"🤖 <b>Apex Scalper Pro spustený!</b> Používam inteligentné Limitné príkazy.")

    while True:
        try:
            if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES:
                CURRENT_STAGE = "STAV: 🔴 V OBCHODE! Čakám na dokončenie pozície..."
                await asyncio.sleep(5)
                continue

            balance_res = okx_request('GET', '/api/v5/account/balance')
            if balance_res.get('code') != '0':
                CURRENT_STAGE = "STAV: ⚠️ Chyba pri načítavaní zostatku z OKX."
                await asyncio.sleep(10)
                continue

            free_capital = 0.0
            for acc in balance_res.get('data', []):
                for detail in acc.get('details', []):
                    if detail.get('ccy') == 'USDC':
                        avail_raw = detail.get('availBal', 0)
                        free_capital = float(avail_raw) if avail_raw else 0.0
                        break

            if free_capital < 10.0:
                CURRENT_STAGE = f"STAV: 🛑 Nedostatok USDC ({free_capital:.2f}$). Skontrolujte zostatok na OKX!"
                await asyncio.sleep(10)
                continue

            CURRENT_STAGE = f"STAV: 🟢 MIMO OBCHODU [{CURRENT_MODE}]. USDC OK ({free_capital:.2f}$). Skenujem trh..."
            tickers_res = okx_request('GET', '/api/v5/market/tickers?instType=SPOT')
            if tickers_res.get('code') != '0' or not tickers_res.get('data'):
                await asyncio.sleep(5)
                continue

            if CURRENT_MODE == "CONSERVATIVE":
                min_vol = 50000.0
                min_change = 2.0
                max_change = 15.0
            else:
                min_vol = 20000.0  # Min objem 20k USDC
                min_change = 1.5   # Rast od 1.5%
                max_change = 25.0  # Rast do 25.0%

            candidates = []
            for item in tickers_res['data']:
                inst_id = item.get('instId', '').upper()
                if not inst_id.endswith('-USDC'): 
                    continue
                if inst_id in ['USDC-USDC', 'USDT-USDC', 'BTC-USDC', 'ETH-USDC']:
                    continue

                try:
                    open24h = float(item.get('open24h', 0))
                    last_p = float(item.get('last', 0))
                    vol24h = float(item.get('volCcy24h', 0) or item.get('vol24h', 0) or 0)

                    if open24h > 0 and last_p > 0 and vol24h >= min_vol:
                        change_pct = ((last_p - open24h) / open24h) * 100
                        if min_change <= change_pct <= max_change:
                            probability = int(min(max((change_pct / max_change) * 100, 50), 99))
                            candidates.append({
                                "instId": inst_id,
                                "price": last_p,
                                "change": change_pct,
                                "volume": vol24h,
                                "probability": probability
                            })
                except Exception:
                    continue

            candidates.sort(key=lambda x: x['change'], reverse=True)
            if candidates:
                MARKET_RADAR = candidates[:10]

            if not candidates:
                await asyncio.sleep(5)
                continue

            best_target = candidates[0]
            coin_name = best_target['instId'].split('-')[0]

            now_ts = datetime.now().timestamp()
            if coin_name in LAST_TRADE_TIME:
                # 🛡️ 1-HODINOVÝ COOLDOWN na rovnakú mincu po predaji
                if now_ts - LAST_TRADE_TIME[coin_name] < 3600:
                    await asyncio.sleep(5)
                    continue

            async with trade_lock:
                if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES:
                    continue

                LAST_TRADE_TIME[coin_name] = now_ts
                CURRENT_STAGE = f"STAV: Vystavujem Limit nákup pre {best_target['instId']}..."
                
                safe_budget = min(free_capital * 0.95, 500.0)
                current_price = best_target['price']
                
                # 💡 SMART LIMIT: Nákupná cena o 0.15% vyššie ako aktuálna pre istotu exekúcie bez slippage
                limit_buy_price = current_price * 1.0015
                coin_amount = safe_budget / limit_buy_price

                formatted_sz = round(coin_amount, 2)
                if formatted_sz <= 0:
                    formatted_sz = round(coin_amount, 4)

                order_body = json.dumps({
                    "instId": best_target['instId'],
                    "tdMode": "cash",
                    "side": "buy",
                    "ordType": "limit",
                    "px": str(round(limit_buy_price, 4)),
                    "sz": str(formatted_sz),
                    "tgtCcy": "base_ccy"
                })

                order_res = okx_request('POST', '/api/v5/trade/order', order_body)
                if order_res.get('code') != '0':
                    print(f"❌ Limit nákup zlyhal na burze: {order_res}", flush=True)
                    send_telegram_msg(f"❌ <b>Chyba Limit nákupu USDC:</b> {order_res.get('msg', 'Neznáma chyba')}")
                    await asyncio.sleep(5)
                    continue

                tp_price = limit_buy_price * 1.025
                sl_price = limit_buy_price * 0.98
                PRICE_HISTORY = [limit_buy_price]

                ACTIVE_TRADES[coin_name] = {
                    "market_pair": best_target['instId'],
                    "entry_price": limit_buy_price,
                    "current_price": limit_buy_price,
                    "highest_price": limit_buy_price,
                    "target_tp_price": tp_price,
                    "current_sl_price": sl_price,
                    "break_even_activated": False,
                    "trailing_tp_activated": False,
                    "amount": formatted_sz,
                    "bought_at": get_sk_time_str(),
                    "invested_usdc": safe_budget,
                    "live_pnl_pct": 0.0,
                    "live_pnl_usdc": 0.0
                }

                CURRENT_STAGE = f"STAV: 🔴 V OBCHODE ({best_target['instId']}) - Čakám na vývoj..."

                send_telegram_msg(
                    f"🟢 <b>[SMART LIMIT NÁKUP USDC]</b>\n"
                    f"<b>Pár:</b> {best_target['instId']}\n"
                    f"<b>Limit cena (+0.15%):</b> {limit_buy_price:.4f} USDC\n"
                    f"<b>Investícia:</b> ~{safe_budget:.2f} USDC\n"
                    f"<b>Stop-Loss:</b> {sl_price:.4f} USDC (-2%)"
                )

        except Exception as scan_err:
            print(f"❌ Chyba v skeneri: {scan_err}", flush=True)

        await asyncio.sleep(5)

# --- 🛡️ MONITOR OBCHODU & RISK MANAGEMENT (BEZ 15M LIMITU) ---
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
                
                # ŽIADNY ČASOVÝ LIMIT - pozícia čaká výhradne na TP alebo SL
                
                ticker_res = okx_request('GET', f'/api/v5/market/ticker?instId={symbol}')
                if ticker_res.get('code') == '0' and ticker_res.get('data'):
                    current_price = float(ticker_res['data'][0]['last'])
                    PRICE_HISTORY.append(current_price)
                    if len(PRICE_HISTORY) > 30:
                        PRICE_HISTORY.pop(0)

                    pnl_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
                    pnl_usdc = trade.get("invested_usdc", 100.0) * (pnl_pct / 100.0)

                    trade["current_price"] = current_price
                    trade["live_pnl_pct"] = pnl_pct
                    trade["live_pnl_usdc"] = pnl_usdc

                    if current_price > trade["highest_price"]:
                        trade["highest_price"] = current_price
                        if pnl_pct >= 1.2:
                            new_trailing_sl = trade["highest_price"] * 0.992
                            if new_trailing_sl > trade["current_sl_price"]:
                                trade["current_sl_price"] = new_trailing_sl

                    # Break-Even na +0.8%
                    if not trade["break_even_activated"] and pnl_pct >= 0.8:
                        trade["break_even_activated"] = True
                        if trade["entry_price"] > trade["current_sl_price"]:
                            trade["current_sl_price"] = trade["entry_price"]

                    # Trailing TP aktivácia od +1.5%
                    if not trade["trailing_tp_activated"] and pnl_pct >= 1.5:
                        trade["trailing_tp_activated"] = True

                    # Výstup pri poklese o 0.5% z maxima
                    if trade["trailing_tp_activated"]:
                        drop_from_peak = ((trade["highest_price"] - current_price) / trade["highest_price"]) * 100
                        if drop_from_peak >= 0.5:
                            try:
                                order_body = json.dumps({
                                    "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                                })
                                okx_request('POST', '/api/v5/trade/order', order_body)
                                record_closed_trade(symbol, pnl_pct, pnl_usdc, "Take Profit")

                                send_telegram_msg(
                                    f"🎯 <b>[TAKE PROFIT - VÝSLEDOK]</b>\n"
                                    f"<b>Pár:</b> {symbol}\n"
                                    f"<b>Výsledný zisk:</b> +{pnl_pct:.2f}% (+{pnl_usdc:+.2f} USDC)"
                                )
                            except Exception as err:
                                print(f"❌ Chyba Take Profit: {err}", flush=True)

                            if coin in ACTIVE_TRADES:
                                del ACTIVE_TRADES[coin]
                            CURRENT_STAGE = "STAV: 🟢 Mimo obchodu. Zisk uzamknutý!"
                            continue

                    if current_price <= trade["current_sl_price"]:
                        try:
                            order_body = json.dumps({
                                "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                            })
                            okx_request('POST', '/api/v5/trade/order', order_body)
                            record_closed_trade(symbol, pnl_pct, pnl_usdc, "Stop-Loss / Ochrana")

                            send_telegram_msg(
                                f"🚨 <b>[STOP-LOSS / OCHRANA - VÝSLEDOK]</b>\n"
                                f"<b>Pár:</b> {symbol}\n"
                                f"<b>Výsledok:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} USDC)"
                            )
                        except Exception as err:
                            print(f"❌ Chyba SL: {err}", flush=True)

                        if coin in ACTIVE_TRADES:
                            del ACTIVE_TRADES[coin]
                        CURRENT_STAGE = "STAV: 🟢 Mimo obchodu. Pozícia zatvorená."
                        continue

        except Exception as main_err:
            print(f"❌ Chyba v monitorovaní: {main_err}", flush=True)
            
        await asyncio.sleep(2)

@asynccontextmanager
async def lifespan(app: FastAPI):
    scanner_task = asyncio.create_task(top_gainer_scanner_loop())
    monitor_task = asyncio.create_task(advanced_risk_monitor_loop())
    yield
    scanner_task.cancel()
    monitor_task.cancel()

app = FastAPI(lifespan=lifespan)

# --- API ENDPOINTS ---
@app.get("/api/set-mode/{mode}")
async def set_mode(mode: str):
    global CURRENT_MODE
    if mode in ["AGGRESSIVE", "CONSERVATIVE"]:
        CURRENT_MODE = mode
        return JSONResponse({"status": "success", "mode": CURRENT_MODE})
    return JSONResponse({"status": "error", "message": "Neplatný režim."})

@app.post("/api/emergency-sell")
async def emergency_
