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
        requests.post(url, json=payload, timeout=5)
    except Exception as err:
        print(f"❌ Chyba pri odosielaní Telegram notifikácie: {err}", flush=True)

ACTIVE_TRADES = {}
MARKET_RADAR = []  
DAILY_PNL_CALENDAR = {} 
LAST_TRADE_TIME = {}  
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

# --- 🔍 AUTONÓMNY SKENER TOP GAINEROV ---
async def top_gainer_scanner_loop():
    global ACTIVE_TRADES, LAST_TRADE_TIME, MARKET_RADAR
    await asyncio.sleep(10)
    print(f"[{get_sk_time_str()}] 🚀 Autonómny Top Gainer Skener úspešne spustený...", flush=True)
    send_telegram_msg("🤖 <b>Top Gainer Scalping Bot spustený!</b> Skenujem trh a hľadám príležitosti.")

    while True:
        try:
            tickers_res = okx_request('GET', '/api/v5/market/tickers?instType=SPOT')
            if tickers_res.get('code') != '0' or not tickers_res.get('data'):
                await asyncio.sleep(15)
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

                    if open24h > 0 and last_p > 0 and vol24h > 50000:
                        change_pct = ((last_p - open24h) / open24h) * 100
                        if 2.0 <= change_pct <= 25.0:
                            vol_score = min(vol24h / 500000.0, 1.0) * 40
                            growth_score = (change_pct / 25.0) * 60
                            probability = int(min(max(vol_score + growth_score, 45), 98))

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
            MARKET_RADAR = candidates[:10]

            if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES:
                await asyncio.sleep(10)
                continue

            if not candidates:
                await asyncio.sleep(20)
                continue

            best_target = candidates[0]
            coin_name = best_target['instId'].split('-')[0]

            now_ts = datetime.now().timestamp()
            if coin_name in LAST_TRADE_TIME:
                if now_ts - LAST_TRADE_TIME[coin_name] < 120:
                    await asyncio.sleep(10)
                    continue

            async with trade_lock:
                if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES:
                    continue

                LAST_TRADE_TIME[coin_name] = now_ts
                print(f"🎯 Nájdený Top Gainer: {best_target['instId']} s rastom +{best_target['change']:.2f}%", flush=True)

                balance_res = okx_request('GET', '/api/v5/account/balance')
                if balance_res.get('code') != '0':
                    await asyncio.sleep(10)
                    continue

                free_capital = 0.0
                for acc in balance_res.get('data', []):
                    for detail in acc.get('details', []):
                        if detail.get('ccy') == 'USDC':
                            free_capital = float(detail.get('availBal', 0))

                if free_capital < 10:
                    print(f"❌ Nedostatok voľného kapitálu: {free_capital} USDC", flush=True)
                    await asyncio.sleep(30)
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
                    print(f"❌ Chyba nákupu pre {best_target['instId']}: {order_res}", flush=True)
                    await asyncio.sleep(10)
                    continue

                sl_price = current_price * 0.97
                ACTIVE_TRADES[coin_name] = {
                    "entry_price": current_price,
                    "current_sl_price": sl_price,
                    "amount": formatted_sz,
                    "bought_at": get_sk_time_str(),
                    "market_pair": best_target['instId'],
                    "highest_price": current_price,
                    "partial_tp_activated": False,
                    "invested_usdc": safe_budget
                }

                send_telegram_msg(
                    f"🟢 <b>[AUTO-SCALP NÁKUP]</b>\n"
                    f"<b>Pár:</b> {best_target['instId']}\n"
                    f"<b>24h Rast pri nákupe:</b> +{best_target['change']:.2f}%\n"
                    f"<b>Vstupná cena:</b> {current_price:.4f} USDC\n"
                    f"<b>Investícia:</b> ~{safe_budget:.2f} USDC\n"
                    f"<b>Stop-Loss:</b> {sl_price:.4f} USDC (-3%)"
                )

        except Exception as scan_err:
            print(f"❌ Chyba v skeneri: {scan_err}", flush=True)

        await asyncio.sleep(15)

# --- 🛡️ MONITOROVACÍ CYKLUS PRE VLNY ---
async def advanced_risk_monitor_loop():
    global ACTIVE_TRADES
    await asyncio.sleep(5)
    print(f"[{get_sk_time_str()}] 🛡️ Risk Management Monitor pre vlny spustený...", flush=True)
    
    while True:
        if not ACTIVE_TRADES:
            await asyncio.sleep(2)
            continue
            
        try:
            now_time = datetime.now(timezone.utc) + timedelta(hours=1)
            
            for coin in list(ACTIVE_TRADES.keys()):
                trade = ACTIVE_TRADES[coin]
                symbol = trade["market_pair"]
                
                if trade.get("amount") is None or trade["amount"] <= 0:
                    continue
                
                bought_time = datetime.strptime(trade["bought_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=1)))
                if now_time - bought_time > timedelta(hours=48):
                    try:
                        order_body = json.dumps({
                            "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                        })
                        okx_request('POST', '/api/v5/trade/order', order_body)
                        send_telegram_msg(f"⏳ <b>[GTC EXSPIRÁCIA 48h]</b> Uzatvorený pár {symbol}")
                        record_daily_pnl(0.0)
                        del ACTIVE_TRADES[coin]
                    except Exception:
                        pass
                    continue
                
                ticker_res = okx_request('GET', f'/api/v5/market/ticker?instId={symbol}')
                if ticker_res.get('code') == '0' and ticker_res.get('data'):
                    current_price = float(ticker_res['data'][0]['last'])
                    pnl_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
                    
                    if current_price > trade["highest_price"]:
                        trade["highest_price"] = current_price

                    if current_price <= trade["current_sl_price"]:
                        reason = "[STOP-LOSS -3%]" if trade["current_sl_price"] < trade["entry_price"] else "[BREAK-EVEN OCHRANA]"
                        try:
                            order_body = json.dumps({
                                "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                            })
                            okx_request('POST', '/api/v5/trade/order', order_body)
                            
                            est_pnl_usdc = trade.get("invested_usdc", 100.0) * (pnl_pct / 100.0)
                            record_daily_pnl(est_pnl_usdc)

                            send_telegram_msg(
                                f"🚨 <b>UZAVRETÝ OBCHOD {reason}</b>\n"
                                f"<b>Pár:</b> {symbol}\n"
                                f"<b>Výsledné PnL:</b> {pnl_pct:+.2f}%"
                            )
                            del ACTIVE_TRADES[coin]
                        except Exception as err:
                            print(f"❌ Chyba SL: {err}", flush=True)
                        continue

                    if pnl_pct >= 1.5 and not trade.get("be_activated", False):
                        trade["current_sl_price"] = trade["entry_price"] * 1.002
                        trade["be_activated"] = True
                        send_telegram_msg(f"🛡️ <b>[BREAK-EVEN AKTÍVNY]</b> Pár {symbol} dosiahol +1.5%, ochrana posunutá na vstup.")

                    max_pnl_pct = ((trade["highest_price"] - trade["entry_price"]) / trade["entry_price"]) * 100
                    if max_pnl_pct >= 3.0:
                        drop_from_high = ((trade["highest_price"] - current_price) / trade["highest_price"]) * 100
                        if drop_from_high >= 1.5:
                            try:
                                order_body = json.dumps({
                                    "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                                })
                                okx_request('POST', '/api/v5/trade/order', order_body)
                                
                                est_pnl_usdc = trade.get("invested_usdc", 100.0) * (pnl_pct / 100.0)
                                record_daily_pnl(est_pnl_usdc)

                                send_telegram_msg(
                                    f"💰 <b>[ÚSPEŠNÝ TRAILING EXIT VLNY]</b>\n"
                                    f"<b>Pár:</b> {symbol}\n"
                                    f"<b>Max dosiahnutý zisk:</b> +{max_pnl_pct:.2f}%\n"
                                    f"<b>Výsledný zisk PnL:</b> {pnl_pct:+.2f}%"
                                )
                                del ACTIVE_TRADES[coin]
                            except Exception as err:
                                print(f"❌ Chyba Trailing Exit: {err}", flush=True)
                            continue

        except Exception as main_err:
            print(f"❌ Chyba v monitorovacom cykle: {main_err}", flush=True)
            
        await asyncio.sleep(2)

@asynccontextmanager
async def lifespan(app: FastAPI):
    scanner_task = asyncio.create_task(top_gainer_scanner_loop())
    monitor_task = asyncio.create_task(advanced_risk_monitor_loop())
    yield
    scanner_task.cancel()
    monitor_task.cancel()

app = FastAPI(lifespan=lifespan)

# --- 🖼️ PRESNÝ DIZAJN: OBRÁZOK AKO POZADIE + ČIERNE NÁPISY NA ĽAVEJ STRANE ---
DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Apex Scalper Pro</title>
    <style>
        * { box-sizing: border-box; }
        body { 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
            margin: 0; 
            padding: 25px;
            /* Fixný obrázok mesta ako pozadie na celú obrazovku */
            background-image: url('https://images.unsplash.com/photo-1518709268805-4e9042af9f23?q=80&w=1920&auto=format&fit=crop'); /* fallback na high-res cyberpunk alternatívu, príp. priamo tvoj obrázok */
            background: url('https://images.unsplash.com/photo-1607604276583-eef5d076aa5f?q=80&w=2074&auto=format&fit=crop') no-repeat center center fixed;
            background-size: cover;
            color: #000000; /* Všetko písmo je čierne */
            min-height: 100vh;
        }

        /* Ak chceš použiť presne ten tvoj nahratý obrázok, použijeme ho ako base64 alebo url. Tu je nastavený na celé pozadie */
        body::before {
            content: "";
            position: fixed;
            top: 0; left: 0; width: 100vw; height: 100vh;
            background: url('https://chatgpt.com/imagestoragenew/...') no-repeat center center fixed; /* ak treba, fallback prekrýva jemným svetlým filtrom, aby čierne písmo perfektne vyniklo na svetlejších i tmavších častiach */
            z-index: -1;
        }

        .container { 
            max-width: 650px; /* Zarovnané na ľavú stranu, aby bolo vidno mesto */
            margin: 0; 
            background: rgba(255, 255, 255, 0.75); /* Mierne priesvitný biely/krémový podklad, aby čierne písmo bolo dokonale čitateľné na neónovom pozadí */
            padding: 25px;
            border-radius: 12px;
            backdrop-filter: blur(8px);
            box-shadow: 0 8px 32px rgba(0,0,0,0.3);
        }

        /* 1. Hore názov malým písmom */
        .brand-title {
            font-size: 0.85rem;
            font-weight: bold;
            color: #000000;
            letter-spacing: 1px;
            text-transform: uppercase;
            margin-bottom: 15px;
            border-bottom: 1px solid rgba(0,0,0,0.2);
            padding-bottom: 5px;
        }

        /* Sekcie pod sebou */
        .section-block {
            margin-bottom: 20px;
        }

        .section-title {
            font-size: 0.9rem;
            font-weight: bold;
            color: #000000;
            margin-bottom: 6px;
        }

        table { width: 100%; border-collapse: collapse; margin-top: 5px; }
        th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid rgba(0,0,0,0.15); font-size: 0.8rem; color: #000000; }
        th { font-weight: 600; color: #333333; text-transform: uppercase; font-size: 0.7rem; }

        .calendar-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(90px, 1fr)); gap: 6px; margin-top: 5px; }
        .calendar-day { 
            background: rgba(255, 255, 255, 0.5); 
            border: 1px solid rgba(0,0,0,0.2); 
            border-radius: 6px; 
            padding: 6px; 
            text-align: center; 
            font-size: 0.75rem;
            color: #000000;
        }

        .refresh-info { color: #222222; font-size: 0.7rem; margin-top: 15px; }
    </style>
    <meta http-equiv="refresh" content="10">
</head>
<body>
    <div class="container">
        <!-- 1. Hore názov malým písmom -->
        <div class="brand-title">APEX SCALPER PRO</div>

        <!-- 2. Pod ním zoznam zostatkov -->
        <div class="section-block">
            <div class="section-
