import os
import hmac
import base64
import json
import asyncio
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
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
        res = requests.post(url, json=payload, timeout=5)
        print(f"📱 Telegram odpoveď: {res.status_code}", flush=True)
    except Exception as err:
        print(f"❌ Chyba pri odosielaní Telegram notifikácie: {err}", flush=True)

ACTIVE_TRADES = {}
MARKET_RADAR = []  
DAILY_PNL_CALENDAR = {} 
LAST_TRADE_TIME = {}  
CURRENT_STAGE = "FAZA 1: Skenujem trh..." 
MAX_PARALLEL_TRADES = 1  
trade_lock = asyncio.Lock()

def get_sk_time_str() -> str:
    sk_time = datetime.now(timezone.utc) + timedelta(hours=1)
    return sk_time.strftime("%Y-%m-%d %H:%M:%S")

def get_sk_date_str() -> str:
    sk_time = datetime.now(timezone.utc) + timedelta(hours=1)
    return sk_time.strftime("%Y-%m-%d")

def record_daily_pnl(pnl_amount: float):
    day_str = get_sk_date_str()
    if day_str not in DAILY_PNL_CALENDAR:
        DAILY_PNL_CALENDAR[day_str] = {"pnl": 0.0, "trades": 0}
    DAILY_PNL_CALENDAR[day_str]["pnl"] += pnl_amount
    DAILY_PNL_CALENDAR[day_str]["trades"] += 1

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

# --- 🔍 ULTRARÝCHLY SKENER A SPRAVODAJCA FÁZ ---
async def top_gainer_scanner_loop():
    global ACTIVE_TRADES, LAST_TRADE_TIME, MARKET_RADAR, CURRENT_STAGE
    await asyncio.sleep(5)
    print(f"[{get_sk_time_str()}] ⚡ AGRESÍVNY SCALPER PRO SPUSŤENÝ!", flush=True)

    while True:
        try:
            if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES:
                CURRENT_STAGE = "FAZA 3: Obchod Aktívny & Sledovanie PnL"
                await asyncio.sleep(5)
                continue

            CURRENT_STAGE = "FAZA 1: Detekcia Top Gainerov na burze..."
            tickers_res = okx_request('GET', '/api/v5/market/tickers?instType=SPOT')
            if tickers_res.get('code') != '0' or not tickers_res.get('data'):
                await asyncio.sleep(5)
                continue

            candidates = []
            for item in tickers_res['data']:
                inst_id = item.get('instId', '')
                if not inst_id.endswith('-USDC'):
                    continue
                if inst_id in ['USDC-USDC', 'USDT-USDC', 'BTC-USDC', 'ETH-USDC']:
                    continue

                try:
                    open24h = float(item.get('open24h', 0))
                    last_p = float(item.get('last', 0))
                    vol24h = float(item.get('volCcy24h', 0))

                    if open24h > 0 and last_p > 0 and vol24h > 10000:
                        change_pct = ((last_p - open24h) / open24h) * 100
                        if 1.5 <= change_pct <= 25.0:
                            probability = int(min(max((change_pct / 25.0) * 100, 50), 99))
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
                if now_ts - LAST_TRADE_TIME[coin_name] < 60:
                    await asyncio.sleep(5)
                    continue

            async with trade_lock:
                if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES:
                    continue

                # FAZA 2: VYBRANÝ TOKEN & PRÍPRAVA OBCHODU
                CURRENT_STAGE = f"FAZA 2: Vybraný {best_target['instId']} (+{best_target['change']:.2f}%) - Príprava nákupu..."
                LAST_TRADE_TIME[coin_name] = now_ts
                print(f"🎯 FAZA 2: Vybraný token {best_target['instId']}", flush=True)

                balance_res = okx_request('GET', '/api/v5/account/balance')
                if balance_res.get('code') != '0':
                    await asyncio.sleep(5)
                    continue

                free_capital = 0.0
                for acc in balance_res.get('data', []):
                    for detail in acc.get('details', []):
                        if detail.get('ccy') == 'USDC':
                            free_capital = float(detail.get('availBal', 0))

                if free_capital < 10:
                    CURRENT_STAGE = "FAZA 1: Nedostatok USDC kapitálu..."
                    await asyncio.sleep(15)
                    continue

                safe_budget = free_capital * 0.98
                current_price = best_target['price']
                coin_amount = safe_budget / current_price

                formatted_sz = round(coin_amount, 2)
                if formatted_sz <= 0:
                    formatted_sz = round(coin_amount, 4)

                order_body = json.dumps({
                    "instId": best_target['instId'],
                    "tdMode": "cash",
                    "side": "buy",
                    "ordType": "market",
                    "sz": str(formatted_sz)
                })

                order_res = okx_request('POST', '/api/v5/trade/order', order_body)
                if order_res.get('code') != '0':
                    print(f"❌ Nákup zlyhal: {order_res}", flush=True)
                    # Ak demo účet vráti chybu obchodu (ninsufficient balance atď.), nezablokujeme sa navždy, ale skúsime znova
                    await asyncio.sleep(5)
                    continue

                tp_price = current_price * 1.020
                sl_price = current_price * 0.980

                # FAZA 3: REALIZÁCIA NÁKUPU
                ACTIVE_TRADES[coin_name] = {
                    "entry_price": current_price,
                    "current_price": current_price,
                    "target_tp_price": tp_price,
                    "current_sl_price": sl_price,
                    "amount": formatted_sz,
                    "bought_at": get_sk_time_str(),
                    "market_pair": best_target['instId'],
                    "invested_usdc": safe_budget,
                    "live_pnl_pct": 0.0,
                    "live_pnl_usdc": 0.0
                }
                CURRENT_STAGE = f"FAZA 3: NAKÚPENÉ! Sledujem PnL pre {best_target['instId']}"

                send_telegram_msg(
                    f"🟢 <b>[⚡ NAKÚPENÉ - FAZA 3]</b>\n"
                    f"<b>Pár:</b> {best_target['instId']}\n"
                    f"<b>Vstupná cena:</b> {current_price:.4f} USDC\n"
                    f"<b>Množstvo:</b> {formatted_sz} {coin_name}\n"
                    f"<b>Investícia:</b> ~{safe_budget:.2f} USDC\n"
                    f"<b>Cieľ (+2% TP):</b> {tp_price:.4f} USDC\n"
                    f"<b>Stop-Loss (-2%):</b> {sl_price:.4f} USDC"
                )

        except Exception as scan_err:
            print(f"❌ Chyba v skeneri: {scan_err}", flush=True)

        await asyncio.sleep(5)

# --- 🛡️ RISK MONITOR & ŽIVÝ PNL PREPOČET (FAZA 3) ---
async def advanced_risk_monitor_loop():
    global ACTIVE_TRADES, CURRENT_
    
