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

# --- 🌆 KOMPAKTNÝ CYBERPUNK TERMINÁL S VIDITEĽNÝM MESTOM A NEÓNOM BAR ---
DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Apex Scalper Pro | Cyberpunk City Terminal</title>
    <style>
        :root {
            --neon-blue: #00f0ff;
            --neon-pink: #ff007f;
            --neon-green: #00ff66;
            --bg-glass: rgba(8, 12, 24, 0.65);
            --border-glow: rgba(0, 240, 255, 0.25);
            --text-main: #f1f5f9;
            --text-dim: #94a3b8;
        }

        * { box-sizing: border-box; }
        body { 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
            background-color: #02040a; 
            color: var(--text-main); 
            margin: 0; 
            padding: 15px;
            overflow-x: hidden;
            position: relative;
        }

        /* Živé mesto pozadie s paralaxou/animáciou */
        #city-canvas {
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;
            z-index: -1;
            background: linear-gradient(135deg, #050b18 0%, #020408 100%);
        }

        .container { max-width: 1250px; margin: 0 auto; position: relative; z-index: 1; }

        .header { 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            background: var(--bg-glass); 
            padding: 15px 25px; 
            border-radius: 12px; 
            border: 1px solid var(--border-glow); 
            box-shadow: 0 0 20px rgba(0, 240, 255, 0.1);
            backdrop-filter: blur(8px);
        }

        .logo-area h1 { 
            margin: 0; 
            font-size: 1.5rem; 
            color: var(--neon-blue); 
            letter-spacing: 1.5px;
            text-shadow: 0 0 10px rgba(0, 240, 255, 0.5);
            text-transform: uppercase;
        }

        .logo-area p { margin: 2px 0 0 0; color: var(--text-dim); font-size: 0.75rem; }

        .status-badge { 
            display: flex; 
            align-items: center; 
            gap: 8px; 
            background: rgba(0, 255, 102, 0.1); 
            color: var(--neon-green); 
            padding: 6px 14px; 
            border-radius: 20px; 
            font-weight: 700; 
            font-size: 0.8rem; 
            border: 1px solid rgba(0, 255, 102, 0.3);
            box-shadow: 0 0 10px rgba(0, 255, 102, 0.15);
        }

        .pulse-dot { 
            width: 8px; 
            height: 8px; 
            background: var(--neon-green); 
            border-radius: 50%; 
            box-shadow: 0 0 8px var(--neon-green); 
            animation: pulse 1.5s infinite alternate; 
        }

        @keyframes pulse { 0% { opacity: 0.4; transform: scale(0.9); } 100% { opacity: 1; transform: scale(1.2); } }

        .grid-top { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 15px; }

        /* Kompaktnejšie a priehľadnejšie karty, aby bolo vidno pozadie mesta */
        .card { 
            background: rgba(10, 14, 28, 0.7); 
            padding: 16px 20px; 
            border-radius: 12px; 
            border: 1px solid rgba(0, 240, 255, 0.15); 
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
            backdrop-filter: blur(6px);
        }

        h2 { 
            margin-top: 0; 
            color: var(--neon-blue); 
            font-size: 0.95rem; 
            border-bottom: 1px solid rgba(255, 255, 255, 0.08); 
            padding-bottom: 8px; 
            display: flex; 
            justify-content: space-between; 
            align-items: center;
        }

        table { width: 100%; border-collapse: collapse; margin-top: 8px; }
        th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid rgba(255, 255, 255, 0.04); font-size: 0.85rem; }
        th { color: var(--text-dim); text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.5px; }

        .tag-green { color: var(--neon-green); font-weight: bold; text-shadow: 0 0 6px rgba(0, 255, 102, 0.3); }
        
        .badge-prob { 
            background: rgba(255, 0, 127, 0.15); 
            color: var(--neon-pink); 
            padding: 3px 8px; 
            border-radius: 6px; 
            font-weight: 700; 
            font-size: 0.75rem; 
            border: 1px solid rgba(255, 0, 127, 0.3);
        }

        .calendar-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 8px; margin-top: 10px; }
        .calendar-day { 
            background: rgba(4, 6, 12, 0.6); 
            border: 1px solid rgba(0, 240, 255, 0.15); 
            border-radius: 8px; 
            padding: 8px; 
            text-align: center; 
        }

        .calendar-date { font-size: 0.7rem; color: var(--text-dim); margin-bottom: 4px; }
        .calendar-pnl { font-size: 0.9rem; font-weight: bold; }
        .pnl-plus { color: var(--neon-green); text-shadow: 0 0 6px rgba(0, 255, 102, 0.2); }
        .pnl-minus { color: #ff3366; }
        .calendar-trades { font-size: 0.65rem; color: var(--text-dim); margin-top: 2px; }

        .refresh-info { text-align: center; color: var(--text-dim); font-size: 0.75rem; margin-top: 15px; }
    </style>
    <meta http-equiv="refresh" content="10">
</head>
<body>
    <!-- Živé plátno s kyberpunkovým mestom a blikajúcim neónom BAR -->
    <canvas id="city-canvas"></canvas>

    <div class="container">
        <!-- Hlavička -->
        <div class="header">
            <div class="logo-area">
                <h1>⚡ APEX SCALPER PRO</h1>
                <p>Cyberpunk OKX Terminal & Autonomous Top Gainer Scalper</p>
            </div>
            <div>
                <div class="status-badge">
                    <div class="pulse-dot"></div> TERMINÁL ONLINE
                </div>
            </div>
        </div>

        <!-- Zostatky & Obchody -->
        <div class="grid-top">
            <div class="card">
                <h2>💰 Reálne Zostatky (OKX Demo)</h2>
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

            <div class="card">
                <h2>📈 Aktívne Obchody</h2>
                {% if trades %}
                    <table>
                        <tr><th>Pár</th><th>Vstup</th><th>Stop-Loss</th></tr>
                        {% for t, details in trades.items() %}
                        <tr>
                            <td><strong style="color: var(--neon-blue);">{{ details.market_pair }}</strong></td>
                            <td>{{ "%.4f" | format(details.entry_price) }}</td>
                            <td>{{ "%.4f" | format(details.current_sl_price) }}</td>
                        </tr>
                        {% endfor %}
                    </table>
                {% else %}
                    <p style="color: var(--text-dim); text-align: center; margin-top: 20px; font-size: 0.85rem;">Žiadny aktívny obchod. Čakám na signál...</p>
                {% endif %}
            </div>
        </div>

        <!-- Radar -->
        <div class="card" style="margin-top: 20px;">
            <h2>
                <span>⚡ Živý Radar Trhu (Top Gainers 2% - 25% | Objem >50k)</span>
                <span style="font-size: 0.75rem; color: var(--neon-pink);">AI Volatility Radar</span>
            </h2>
            {% if radar %}
                <table>
                    <tr><th>Symbol</th><th>Cena (USDC)</th><th>Rast za 24h</th><th>Objem (USDC)</th><th>Pravdepodobnosť výstrelu</th></tr>
                    {% for item in radar %}
                    <tr>
                        <td><strong>{{ item.instId }}</strong></td>
                        <td>{{ "%.4f" | format(item.price) }}</td>
                        <td class="tag-green">+{{ "%.2f" | format(item.change) }}%</td>
                        <td>{{ "{:,.0f}".format(item.volume) }}</td>
                        <td><span class="badge-prob">{{ item.probability }}% Šanca</span></td>
                    </tr>
                    {% endfor %}
                </table>
            {% else %}
                <p style="color: var(--text-dim); text-align: center; margin-top: 15px; font-size: 0.85rem;">Skenujem trh, čakám na mince spĺňajúce kritériá...</p>
            {% endif %}
        </div>

        <!-- Kalendár PnL -->
        <div class="card" style="margin-top: 20px;">
            <h2>📅 Kalendár Denných Výsledkov (PnL Tracker)</h2>
            {% if calendar %}
                <div class="calendar-grid">
                    {% for day, data in calendar.items() %}
                    <div class="calendar-day">
                        <div class="calendar-date">{{ day }}</div>
                        <div class="calendar-pnl {% if data.pnl >= 0 %}pnl-plus{% else %}pnl-minus{% endif %}">
                            {{ "%+.2f" | format(data.pnl) }} $
                        </div>
                        <div class="calendar-trades">{{ data.trades }} obchodov</div>
                    </div>
                    {% endfor %}
                </div>
            {% else %}
                <p style="color: var(--text-dim); text-align: center; margin-top: 10px; font-size: 0.85rem;">Zatiaľ žiadne uzavreté obchody v tomto období.</p>
            {% endif %}
        </div>

        <div class="refresh-info">
            Cyberpunk Terminal sa automaticky obnovuje každých 10 sekúnd. | Čas: {{ current_time }}
        </div>
    </div>

    <script>
        const canvas = document.getElementById('city-canvas');
        const ctx = canvas.getContext('2d');
        let width = canvas.width = window.innerWidth;
        let height = canvas.height = window.innerHeight;

        window.addEventListener('resize', () => {
            width = canvas.width = window.innerWidth;
            height = canvas.height = window.innerHeight;
        });

        // Generovanie budov v pozadí (mesto)
        const buildings = [];
        for(let i = 0; i < 22; i++) {
            buildings.push({
                x: i * (width / 20),
                w: width / 22,
                h: Math.random() * (height * 0.6) + (height * 0.2),
                color: i % 2 === 0 ? '#070d1d' : '#040710'
            });
        }

        // Svetelné lúče / lietajúce objekty
        const lights = [];
        for(let i = 0; i < 15; i++) {
            lights.push({
                x: Math.random() * width,
                y: Math.random() * height,
                speed: Math.random() * 2 + 1,
                size: Math.random() * 2 + 1,
                color: i % 3 === 0 ? '#00f0ff' : (i % 3 === 1 ? '#ff007f' : '#00ff66')
            });
        }

        let barGlow = 0;

        function drawCity() {
            ctx.clearRect(0, 0, width, height);

            // Vykreslenie siluety budov mesta
            buildings.forEach(b => {
                ctx.fillStyle = b.color;
                ctx.fillRect(b.x, height - b.h, b.w, b.h);

                // Okná na budovách (niektoré svietia)
                ctx.fillStyle = 'rgba(0, 240, 255, 0.15)';
                for(let wy = height - b.h + 20; wy < height - 20; wy += 30) {
                    for(let wx = b.x + 10; wx < b.x + b.w - 10; wx += 20) {
                        if((wx + wy) % 5 === 0) {
                            ctx.fillRect(wx, wy, 8, 12);
                        }
                    }
                }
            });

            // Blikajúci neónový nápis "BAR" v pozadí mesta
            barGlow += 0.05;
            let alphaBar = Math.abs(Math.sin(barGlow)) * 0.7 + 0.3;
            ctx.save();
            ctx.font = 'bold 36px monospace';
            ctx.fillStyle = `rgba(255, 0, 127, ${alphaBar})`;
            ctx.shadowColor = '#ff007f';
            ctx.shadowBlur = 20;
            ctx.fillText("B A R 24H", width * 0.75, height * 0.35);
            ctx.restore();

            // Pohybujúce sa svetlá / dážď / lúče v meste
            lights.forEach(l => {
                l.x -= l.speed;
                if(l.x < 0) l.x = width;

                ctx.beginPath();
                ctx.arc(l.x, l.y, l.size, 0, Math.PI * 2);
                ctx.fillStyle = l.color;
                ctx.shadowBlur = 10;
                ctx.shadowColor = l.color;
                ctx.globalAlpha = 0.8;
                ctx.fill();
            });

            requestAnimationFrame(drawCity);
        }
        drawCity();
    </script>
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
        "bot_type": "Apex Gainer Scalper Pro (Cyberpunk City Edition)",
        "active_trades": ACTIVE_TRADES,
        "market_radar": MARKET_RADAR,
        "calendar": DAILY_PNL_CALENDAR,
        "sk_time": get_sk_time_str()
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
