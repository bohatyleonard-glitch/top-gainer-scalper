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

# --- 🖼️ HTML ŠABLÓNA (BEZ RIZIKA SYNTAKTICKÝCH CHÝB) ---
DASHBOARD_TEMPLATE = """<!DOCTYPE html>
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
            background: url('https://images.unsplash.com/photo-1578632767115-351597cf2477?q=80&w=1920&auto=format&fit=crop') no-repeat center center fixed;
            background-size: cover;
            color: #000000;
            min-height: 100vh;
        }

        .container { 
            max-width: 580px; 
            margin: 0; 
            background: transparent; 
            padding: 0;
            box-shadow: none;
        }

        .brand-title {
            font-size: 0.8rem;
            font-weight: 800;
            color: #000000;
            letter-spacing: 1.5px;
            text-transform: uppercase;
            margin-bottom: 20px;
        }

        .section-block {
            margin-bottom: 22px;
        }

        .section-title {
            font-size: 0.85rem;
            font-weight: 700;
            color: #000000;
            margin-bottom: 6px;
        }

        table { width: 100%; border-collapse: collapse; margin-top: 4px; }
        th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid rgba(0,0,0,0.25); font-size: 0.78rem; color: #000000; }
        th { font-weight: 700; color: #111111; text-transform: uppercase; font-size: 0.68rem; }

        .calendar-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(85px, 1fr)); gap: 6px; margin-top: 4px; }
        .calendar-day { 
            background: rgba(255, 255, 255, 0.25); 
            border: 1px solid rgba(0,0,0,0.3); 
            border-radius: 4px; 
            padding: 5px; 
            text-align: center; 
            font-size: 0.72rem;
            color: #000000;
        }

        .refresh-info { color: #111111; font-size: 0.68rem; margin-top: 20px; font-weight: 600; }
    </style>
    <meta http-equiv="refresh" content="10">
</head>
<body>
    <div class="container">
        <div class="brand-title">Apex Scalper Pro</div>

        <div class="section-block">
            <div class="section-title">💰 Zoznam zostatkov (OKX Demo)</div>
            <table>
                <tr><th>Mena</th><th>Dostupné</th></tr>
                {% for coin, data in balances.items() %}
                <tr>
                    <td><strong>{{ coin }}</strong></td>
                    <td>{{ "%.4f" | format(data.available_balance | float) }}</td>
                </tr>
                {% endfor %}
            </table>
        </div>

        <div class="section-block">
            <div class="section-title">⚡ Živý Radar Trhu (Top Gainers 2% - 25% | Objem >50k)</div>
            {% if radar %}
                <table>
                    <tr><th>Symbol</th><th>Cena</th><th>Rast</th><th>Objem</th><th>Šanca</th></tr>
                    {% for item in radar %}
                    <tr>
                        <td><strong>{{ item.instId }}</strong></td>
                        <td>{{ "%.4f" | format(item.price) }}</td>
                        <td>+{{ "%.2f" | format(item.change) }}%</td>
                        <td>{{ "{:,.0f}".format(item.volume) }}</td>
                        <td><strong>{{ item.probability }}%</strong></td>
                    </tr>
                    {% endfor %}
                </table>
            {% else %}
                <div style="font-size: 0.78rem; color: #111111;">Skenujem trh, čakám na mince spĺňajúce kritériá...</div>
            {% endif %}
        </div>

        <div class="section-block">
            <div class="section-title">📅 Kalendár Denných Výsledkov (PnL Tracker)</div>
            {% if calendar %}
                <div class="calendar-grid">
                    {% for day, data in calendar.items() %}
                    <div class="calendar-day">
                        <div>{{ day }}</div>
                        <div style="font-weight: bold;">{{ "%+.2f" | format(data.pnl) }} $</div>
                        <div style="font-size: 0.6rem;">{{ data.trades }} obch.</div>
                    </div>
                    {% endfor %}
                </div>
            {% else %}
                <div style="font-size: 0.78rem; color: #111111;">Zatiaľ žiadne uzavreté obchody.</div>
            {% endif %}
        </div>

        <div class="section-block">
            <div class="section-title">📈 Aktívne Obchody</div>
            {% if trades %}
                <table>
                    <tr><th>Pár</th><th>Vstup</th><th>Stop-Loss</th></tr>
                    {% for t, details in trades.items() %}
                    <tr>
                        <td><strong>{{ details.market_pair }}</strong></td>
                        <td>{{ "%.4f" | format(details.entry_price) }}</td>
                        <td>{{ "%.4f" | format(details.current_sl_price) }}</td>
                    </tr>
                    {% endfor %}
                </table>
            {% else %}
                <div style="font-size: 0.78rem; color: #111111;">Žiadny aktívny obchod. Čakám na signál...</div>
            {% endif %}
        </div>

        <div class="refresh-info">
            Obnovenie každých 10s | Aktuálny čas: {{ current_time }}
        </div>
    </div>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    account_balances = {"USDC": {"available_balance": 0.0}}
    try:
        balance_res = okx_request('GET', '/api/v5/account/balance')
        if balance_res.get('code') == '0':
            found = {}
            for acc in balance_res.get('data', []):
                for detail in acc.get('details', []):
                    ccy = detail.get('ccy')
                    avail = float(detail.get('availBal', 0))
                    if avail > 0:
                        found[ccy] = {"available_balance": avail}
            if found:
                account_balances = found
    except Exception:
        pass

    template = Template(DASHBOARD_TEMPLATE)
    rendered_html = template.render(
        balances=account_balances,
        trades=ACTIVE_TRADES,
        radar=MARKET_RADAR,
        calendar=DAILY_PNL_CALENDAR,
        current_time=get_sk_time_str()
    )
    return rendered_html

@app.get("/api/status")
async def api_status():
    return {
        "status": "online",
        "bot_type": "Apex Gainer Scalper Pro",
        "active_trades": ACTIVE_TRADES,
        "market_radar": MARKET_RADAR,
        "calendar": DAILY_PNL_CALENDAR,
        "sk_time": get_sk_time_str()
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
