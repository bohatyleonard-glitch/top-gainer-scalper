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
CURRENT_STAGE = "FAZA 1: Skenujem trh..." # Sledovač fáz
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
    global ACTIVE_TRADES, CURRENT_STAGE
    await asyncio.sleep(3)
    
    while True:
        if not ACTIVE_TRADES:
            await asyncio.sleep(1)
            continue
            
        try:
            now_time = datetime.now(timezone.utc) + timedelta(hours=1)
            
            for coin in list(ACTIVE_TRADES.keys()):
                trade = ACTIVE_TRADES[coin]
                symbol = trade["market_pair"]
                
                if trade.get("amount") is None or trade["amount"] <= 0:
                    continue
                
                bought_time = datetime.strptime(trade["bought_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=1)))
                
                # TIMEOUT 15 MINÚT
                if now_time - bought_time > timedelta(minutes=15):
                    try:
                        order_body = json.dumps({
                            "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                        })
                        okx_request('POST', '/api/v5/trade/order', order_body)
                        send_telegram_msg(f"⏱️ <b>[TIMEOUT 15m]</b> Uzatvorený pár {symbol}")
                        record_daily_pnl(trade.get("live_pnl_usdc", 0.0))
                        del ACTIVE_TRADES[coin]
                        CURRENT_STAGE = "FAZA 1: Hľadám ďalšieho lídra..."
                    except Exception:
                        pass
                    continue
                
                ticker_res = okx_request('GET', f'/api/v5/market/ticker?instId={symbol}')
                if ticker_res.get('code') == '0' and ticker_res.get('data'):
                    current_price = float(ticker_res['data'][0]['last'])
                    pnl_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
                    pnl_usdc = trade.get("invested_usdc", 100.0) * (pnl_pct / 100.0)

                    # Aktuálny Živý PnL pre HUD
                    trade["current_price"] = current_price
                    trade["live_pnl_pct"] = pnl_pct
                    trade["live_pnl_usdc"] = pnl_usdc

                    # 1. DOSIAHNUTÝ TAKE PROFIT (+2.0%)
                    if current_price >= trade["target_tp_price"]:
                        try:
                            order_body = json.dumps({
                                "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                            })
                            okx_request('POST', '/api/v5/trade/order', order_body)
                            record_daily_pnl(pnl_usdc)

                            send_telegram_msg(
                                f"🎯 <b>[TAKE PROFIT +2%]</b>\n"
                                f"<b>Pár:</b> {symbol}\n"
                                f"<b>Zisk:</b> +{pnl_pct:.2f}% (~{pnl_usdc:+.2f} USDC)"
                            )
                            del ACTIVE_TRADES[coin]
                            CURRENT_STAGE = "FAZA 1: Obchod v zisku uzavretý! Skenujem ďalej..."
                        except Exception as err:
                            print(f"❌ Chyba TP: {err}", flush=True)
                        continue

                    # 2. DOSIAHNUTÝ STOP LOSS (-2.0%)
                    if current_price <= trade["current_sl_price"]:
                        try:
                            order_body = json.dumps({
                                "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                            })
                            okx_request('POST', '/api/v5/trade/order', order_body)
                            record_daily_pnl(pnl_usdc)

                            send_telegram_msg(
                                f"🚨 <b>[STOP-LOSS EXECUTED]</b>\n"
                                f"<b>Pár:</b> {symbol}\n"
                                f"<b>Strata:</b> {pnl_pct:.2f}% (~{pnl_usdc:+.2f} USDC)"
                            )
                            del ACTIVE_TRADES[coin]
                            CURRENT_STAGE = "FAZA 1: Obchod uzavretý v SL. Hľadám nový signál..."
                        except Exception as err:
                            print(f"❌ Chyba SL: {err}", flush=True)
                        continue

        except Exception as main_err:
            print(f"❌ Chyba v monitorovaní PnL: {main_err}", flush=True)
            
        await asyncio.sleep(1)

@asynccontextmanager
async def lifespan(app: FastAPI):
    scanner_task = asyncio.create_task(top_gainer_scanner_loop())
    monitor_task = asyncio.create_task(advanced_risk_monitor_loop())
    yield
    scanner_task.cancel()
    monitor_task.cancel()

app = FastAPI(lifespan=lifespan)

# --- 🖼️ CYBERPUNK HUD ŠABLÓNA S SLEDOM FÁZ A ŽIVÝM PNL ---
HTML_TEMPLATE_CONTENT = """<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Apex Scalper Pro - 3 Stage HUD</title>
    <style>
        * { box-sizing: border-box; }
        body { 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
            margin: 0; 
            padding: 25px;
            background: linear-gradient(rgba(0,0,0,0.55), rgba(0,0,0,0.55)), url('https://images.unsplash.com/photo-1519501025264-65ba15a82390?q=80&w=1920&auto=format&fit=crop');
            background-size: cover;
            background-position: center center;
            background-repeat: no-repeat;
            background-attachment: fixed;
            color: #ffffff;
            min-height: 100vh;
            overflow-x: hidden;
            position: relative;
        }

        body::before {
            content: "";
            position: fixed;
            top: -10%;
            left: -10%;
            width: 120%;
            height: 120%;
            background: radial-gradient(circle at 20% 30%, rgba(0,255,255,0.12), transparent 40%),
                        radial-gradient(circle at 80% 50%, rgba(180,0,255,0.12), transparent 40%),
                        radial-gradient(circle at 50% 80%, rgba(0,128,255,0.10), transparent 45%);
            animation: fogMove 28s ease-in-out infinite;
            pointer-events: none;
            z-index: -1;
        }

        @keyframes fogMove {
            0% { transform: translate(0px, 0px); }
            25% { transform: translate(25px, -10px); }
            50% { transform: translate(45px, 15px); }
            75% { transform: translate(15px, 25px); }
            100% { transform: translate(0px, 0px); }
        }

        .container { 
            max-width: 620px; 
            margin: 0; 
            background: rgba(0, 0, 0, 0.35);
            backdrop-filter: blur(14px);
            -webkit-backdrop-filter: blur(14px);
            border: 1px solid rgba(0, 255, 255, 0.22);
            border-radius: 16px;
            padding: 20px;
            box-shadow: 0 0 25px rgba(0, 255, 255, 0.18), 0 0 50px rgba(180, 0, 255, 0.12);
        }

        .brand-title {
            font-size: 0.9rem;
            font-weight: 800;
            color: #00f0ff;
            letter-spacing: 2px;
            text-transform: uppercase;
            margin-bottom: 12px;
            text-shadow: 0 0 10px rgba(0, 240, 255, 0.7);
        }

        .stage-box {
            background: rgba(0, 240, 255, 0.10);
            border: 1px solid #00f0ff;
            border-radius: 8px;
            padding: 10px 14px;
            font-size: 0.82rem;
            font-weight: 700;
            color: #ffffff;
            margin-bottom: 20px;
            text-shadow: 0 0 5px rgba(0, 240, 255, 0.5);
        }

        .section-block { margin-bottom: 22px; }

        .section-title {
            font-size: 0.82rem;
            font-weight: 700;
            color: #ffffff;
            margin-bottom: 6px;
            text-shadow: 0 0 5px rgba(255, 255, 255, 0.4);
        }

        table { width: 100%; border-collapse: collapse; margin-top: 4px; }
        th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid rgba(0, 255, 255, 0.15); font-size: 0.78rem; color: #ffffff; }
        th { font-weight: 700; color: #00f0ff; text-transform: uppercase; font-size: 0.68rem; letter-spacing: 1px; }

        .pnl-green { color: #00ff88; font-weight: bold; }
        .pnl-red { color: #ff4444; font-weight: bold; }

        .calendar-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(85px, 1fr)); gap: 6px; margin-top: 4px; }
        .calendar-day { 
            background: rgba(0, 0, 0, 0.50); 
            border: 1px solid rgba(0, 255, 255, 0.25); 
            border-radius: 6px; 
            padding: 6px; 
            text-align: center; 
            font-size: 0.72rem;
            color: #ffffff;
        }

        .refresh-info { color: rgba(255, 255, 255, 0.6); font-size: 0.68rem; margin-top: 20px; font-weight: 600; }
    </style>
    <meta http-equiv="refresh" content="2">
</head>
<body>
    <div class="container">
        <div class="brand-title">⚡ Apex Scalper Pro (3-Stage HUD)</div>
        
        <div class="stage-box">
            📡 AKTUÁLNY STAV BOT-A: <br>
            <span style="color: #00ff88;">{{ current_stage }}</span>
        </div>

        <div class="section-block">
            <div class="section-title">📈 FAZA 3: Aktívny Obchod & Živý PnL</div>
            {% if trades %}
                <table>
                    <tr>
                        <th>Pár</th>
                        <th>Vstup</th>
                        <th>Kusy</th>
                        <th>Aktuálna Cena</th>
                        <th>Živý PnL (%)</th>
                        <th>Živý PnL ($)</th>
                    </tr>
                    {% for t, details in trades.items() %}
                    <tr>
                        <td><strong>{{ details.market_pair }}</strong></td>
                        <td>{{ "%.4f" | format(details.entry_price) }}</td>
                        <td>{{ details.amount }}</td>
                        <td>{{ "%.4f" | format(details.current_price) }}</td>
                        <td class="{{ 'pnl-green' if details.live_pnl_pct >= 0 else 'pnl-red' }}">
                            {{ "%+.2f" | format(details.live_pnl_pct) }}%
                        </td>
                        <td class="{{ 'pnl-green' if details.live_pnl_usdc >= 0 else 'pnl-red' }}">
                            {{ "%+.2f" | format(details.live_pnl_usdc) }} $
                        </td>
                    </tr>
                    {% endfor %}
                </table>
            {% else %}
                <div style="font-size: 0.78rem; color: rgba(255, 255, 255, 0.7);">Žiadny aktívny obchod. Čakám na výber v Fáze 2...</div>
            {% endif %}
        </div>

        <div class="section-block">
            <div class="section-title">⚡ FAZA 1 & 2: Radar Kandidátov na Vstup</div>
            {% if radar %}
                <table>
                    <tr><th>Symbol</th><th>Cena</th><th>24h Rast</th><th>Objem</th><th>Šanca</th></tr>
                    {% for item in radar %}
                    <tr>
                        <td><strong>{{ item.instId }}</strong></td>
                        <td>{{ "%.4f" | format(item.price) }}</td>
                        <td>+{{ "%.2f" | format(item.change) }}%</td>
                        <td>{{ "{:,.0f}".format(item.volume) }}</td>
                        <td><strong style="color: #00f0ff;">{{ item.probability }}%</strong></td>
                    </tr>
                    {% endfor %}
                </table>
            {% else %}
                <div style="font-size: 0.78rem; color: rgba(255, 255, 255, 0.7);">Skenujem trh, hľadám Top Gainera...</div>
            {% endif %}
        </div>

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
            <div class="section-title">📅 Kalendár Výsledkov (Denné PnL)</div>
            {% if calendar %}
                <div class="calendar-grid">
                    {% for day, data in calendar.items() %}
                    <div class="calendar-day">
                        <div>{{ day }}</div>
                        <div style="font-weight: bold; color: #00ff88;">{{ "%+.2f" | format(data.pnl) }} $</div>
                        <div style="font-size: 0.6rem; color: rgba(255,255,255,0.7);">{{ data.trades }} obch.</div>
                    </div>
                    {% endfor %}
                </div>
            {% else %}
                <div style="font-size: 0.78rem; color: rgba(255, 255, 255, 0.7);">Zatiaľ žiadne uzavreté obchody.</div>
            {% endif %}
        </div>

        <div class="refresh-info">
            REAL-TIME 3-STAGE SCALPER | Obnovenie 2s | SK Čas: {{ current_time }}
        </div>
    </div>
</body>
</html>"""

with open("index.html", "w", encoding="utf-8") as f:
    f.write(HTML_TEMPLATE_CONTENT)

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

    with open("index.html", "r", encoding="utf-8") as f:
        template_content = f.read()

    template = Template(template_content)
    rendered_html = template.render(
        balances=account_balances,
        trades=ACTIVE_TRADES,
        radar=MARKET_RADAR,
        calendar=DAILY_PNL_CALENDAR,
        current_stage=CURRENT_STAGE,
        current_time=get_sk_time_str()
    )
    return rendered_html

@app.get("/api/status")
async def api_status():
    return {
        "status": "online",
        "bot_type": "Apex Rapid 3-Stage Scalper",
        "current_stage": CURRENT_STAGE,
        "active_trades": ACTIVE_TRADES,
        "market_radar": MARKET_RADAR,
        "calendar": DAILY_PNL_CALENDAR,
        "sk_time": get_sk_time_str()
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
