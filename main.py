import os
import hmac
import base64
import json
import asyncio
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI
import uvicorn
import requests

# --- 🔑 OKX DEMO KĽÚČE ---
OKX_API_KEY = "638848ca-e65c-4074-bcfd-20cf4d292f2c"
OKX_SECRET = "885934366C54F33B276E9E49E8EF87CF"
OKX_PASSPHRASE = "Australia111/"

# --- 📱 TELEGRAM NOTIFIKÁCIE ---
TELEGRAM_BOT_TOKEN = "9021881475:AAFn1oQSvaGkhpX2uyJscdcTybFgk1tNmV8"
TELEGRAM_CHAT_ID = "9130979784"

def send_telegram_msg(message: str):
    """Pomocná funkcia pre odosielanie správ na Telegram"""
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
LAST_TRADE_TIME = {}  # 🛡️ Ochrana proti zacykleniu (odstup medzi obchodmi)
MAX_PARALLEL_TRADES = 1  # 🛡️ Výhradne 1 aktívna pozícia naraz
trade_lock = asyncio.Lock()

def get_sk_time_str() -> str:
    sk_time = datetime.now(timezone.utc) + timedelta(hours=1)
    return sk_time.strftime("%Y-%m-%d %H:%M:%S")

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
    if method.upper() == 'GET':
        response = requests.get(url, headers=headers, timeout=10)
    else:
        response = requests.post(url, headers=headers, data=body, timeout=10)
    
    try:
        res_json = response.json()
        return res_json
    except Exception as err:
        return {"code": "-1", "msg": response.text}

# --- 🔍 AUTONÓMNY SKENER TOP GAINEROV ---
async def top_gainer_scanner_loop():
    global ACTIVE_TRADES, LAST_TRADE_TIME
    await asyncio.sleep(10)
    print(f"[{get_sk_time_str()}] 🚀 Autonómny Top Gainer Skener úspešne spustený...", flush=True)
    send_telegram_msg("🤖 <b>Top Gainer Scalping Bot spustený!</b> Skenujem trh a hľadám príležitosti.")

    while True:
        try:
            if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES:
                await asyncio.sleep(10)
                continue

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
                            candidates.append({
                                "instId": inst_id,
                                "price": last_p,
                                "change": change_pct,
                                "volume": vol24h
                            })
                except Exception:
                    continue

            if not candidates:
                await asyncio.sleep(20)
                continue

            candidates.sort(key=lambda x: x['change'], reverse=True)
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
                    "partial_tp_activated": False
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

@app.get("/dashboard")
async def dashboard():
    account_balances = {}
    try:
        balance_res = okx_request('GET', '/api/v5/account/balance')
        if balance_res.get('code') == '0':
            for acc in balance_res.get('data', []):
                for detail in acc.get('details', []):
                    ccy = detail.get('ccy')
                    avail = float(detail.get('availBal', 0))
                    if avail > 0:
                        account_balances[ccy] = {"available_balance": avail}
    except Exception:
        pass

    return {
        "status": "online",
        "bot_type": "Full-Auto Top Gainer Scalper (1.5% - 20% waves)",
        "account_balances": account_balances,
        "active_trades": ACTIVE_TRADES,
        "sk_time": get_sk_time_str()
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
