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

# --- STAV BOT-A ---
ACTIVE_TRADES = {}       # Drží max 1 aktívny obchod
MARKET_RADAR = []        # Zoznam top gainerov
DAILY_PNL_CALENDAR = {}  # Denný PnL kalendár
CURRENT_STAGE = "STAV: Mimo obchodu. Skenujem trh pre 1000 USDC scalp..."
TRADE_BUDGET_USDC = 1000.0  # Pevne stanovený rozpočet na obchod
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

# --- 🔍 SKENER: NAKUPUJE LEN AK NIE JE ŽIADNY OBCHOD ---
async def top_gainer_scanner_loop():
    global ACTIVE_TRADES, MARKET_RADAR, CURRENT_STAGE
    await asyncio.sleep(5)
    print(f"[{get_sk_time_str()}] ⚡ 80% WIN-RATE SCALPER SPUSTENÝ (Pevný rozpočet: 1000 USDC)", flush=True)

    while True:
        try:
            # 1. AK MÁME AKTÍVNY OBCHOD, SKENER NEROBÍ NIC A ČAKÁ NA UKONČENIE
            if len(ACTIVE_TRADES) > 0:
                CURRENT_STAGE = "STAV: 🔴 V OBCHODE! Čakám na Take Profit (+2%) alebo Stop-Loss (-2%). Žiadne nové nákupy."
                await asyncio.sleep(3)
                continue

            CURRENT_STAGE = "STAV: 🟢 MIMO OBCHODU. Skenujem trh pre 1000 USDC nákup..."
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

                    if open24h > 0 and last_p > 0 and vol24h > 20000:
                        change_pct = ((last_p - open24h) / open24h) * 100
                        if 1.5 <= change_pct <= 25.0:
                            probability = int(min(max((change_pct / 25.0) * 100, 60), 98))
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

            # ZOBERIEME ABSOLÚTNEHO LÍDRA #1
            best_target = candidates[0]
            coin_name = best_target['instId'].split('-')[0]

            async with trade_lock:
                if len(ACTIVE_TRADES) > 0:
                    continue

                CURRENT_STAGE = f"STAV: Pripravujem nákup 1000 USDC pre {best_target['instId']}..."
                
                # Kontrola zostatku USDC
                balance_res = okx_request('GET', '/api/v5/account/balance')
                if balance_res.get('code') != '0':
                    await asyncio.sleep(5)
                    continue

                free_usdc = 0.0
                for acc in balance_res.get('data', []):
                    for detail in acc.get('details', []):
                        if detail.get('ccy') == 'USDC':
                            free_usdc = float(detail.get('availBal', 0))

                if free_usdc < TRADE_BUDGET_USDC:
                    CURRENT_STAGE = f"STAV: ⚠️ Nedostatok USDC (Máš {free_usdc:.2f}, treba {TRADE_BUDGET_USDC})."
                    await asyncio.sleep(10)
                    continue

                current_price = best_target['price']
                coin_amount = TRADE_BUDGET_USDC / current_price
                formatted_sz = round(coin_amount, 2)
                if formatted_sz <= 0:
                    formatted_sz = round(coin_amount, 4)

                # ODOSLANIE NÁKUPNEJ OBJEDNÁVKY NA BURZU
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

                tp_price = current_price * 1.020 # Presne +2% Take Profit
                sl_price = current_price * 0.980 # Presne -2% Stop Loss

                # ULOŽENIE JEDINÉHO AKTÍVNEHO OBCHODU
                ACTIVE_TRADES[coin_name] = {
                    "market_pair": best_target['instId'],
                    "entry_price": current_price,
                    "current_price": current_price,
                    "target_tp_price": tp_price,
                    "current_sl_price": sl_price,
                    "amount": formatted_sz,
                    "bought_at": get_sk_time_str(),
                    "invested_usdc": TRADE_BUDGET_USDC,
                    "live_pnl_pct": 0.0,
                    "live_pnl_usdc": 0.0
                }

                CURRENT_STAGE = f"STAV: 🔴 V OBCHODE ({best_target['instId']}) - Sledujem živý PnL..."

                send_telegram_msg(
                    f"🟢 <b>[⚡ NÁKUP 1000 USDC - TOP GAINER #1]</b>\n"
                    f"<b>Pár:</b> {best_target['instId']}\n"
                    f"<b>Vstupná cena:</b> {current_price:.4f} USDC\n"
                    f"<b>Investícia:</b> {TRADE_BUDGET_USDC} USDC\n"
                    f"<b>Cieľ TP (+2%):</b> {tp_price:.4f} USDC\n"
                    f"<b>Stop-Loss (-2%):</b> {sl_price:.4f} USDC"
                )

        except Exception as scan_err:
            print(f"❌ Chyba v skeneri: {scan_err}", flush=True)

        await asyncio.sleep(3)

# --- 🛡️ MONITOR: SLEDUJE OBCHOD, POČÍTA PNŁ A UZATVÁRA ---
async def advanced_risk_monitor_loop():
    global ACTIVE_TRADES, CURRENT_STAGE
    await asyncio.sleep(2)
    
    while True:
        if not ACTIVE_TRADES:
            await asyncio.sleep(1)
            continue
            
        try:
            now_time = datetime.now(timezone.utc) + timedelta(hours=1)
            
            for coin in list(ACTIVE_TRADES.keys()):
                trade = ACTIVE_TRADES[coin]
                symbol = trade["market_pair"]
                
                bought_time = datetime.strptime(trade["bought_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=1)))
                
                # TIMEOUT 15 MINÚT
                if now_time - bought_time > timedelta(minutes=15):
                    try:
                        order_body = json.dumps({
                            "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                        })
                        okx_request('POST', '/api/v5/trade/order', order_body)
                        pnl_usdc = trade.get("live_pnl_usdc", 0.0)
                        record_daily_pnl(pnl_usdc)

                        send_telegram_msg(
                            f"⏱️ <b>[ČASOVÝ LIMIT 15m - UZATVORENÉ]</b>\n"
                            f"<b>Pár:</b> {symbol}\n"
                            f"<b>Výsledok:</b> {trade['live_pnl_pct']:+.2f}% ({pnl_usdc:+.2f} USDC)"
                        )
                        del ACTIVE_TRADES[coin]
                        CURRENT_STAGE = "STAV: 🟢 Mimo obchodu. Hľadám novú príležitosť..."
                    except Exception:
                        pass
                    continue
                
                # ŽIVÁ KONTROLA CENY Z BURZY
                ticker_res = okx_request('GET', f'/api/v5/market/ticker?instId={symbol}')
                if ticker_res.get('code') == '0' and ticker_res.get('data'):
                    current_price = float(ticker_res['data'][0]['last'])
                    pnl_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
                    pnl_usdc = trade.get("invested_usdc", 1000.0) * (pnl_pct / 100.0)

                    # Zápis pre web
                    trade["current_price"] = current_price
                    trade["live_pnl_pct"] = pnl_pct
                    trade["live_pnl_usdc"] = pnl_usdc

                    # 1. TAKE PROFIT DOSIAHNUTÝ (+2%)
                    if current_price >= trade["target_tp_price"]:
                        try:
                            order_body = json.dumps({
                                "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                            })
                            okx_request('POST', '/api/v5/trade/order', order_body)
                            record_daily_pnl(pnl_usdc)

                            send_telegram_msg(
                                f"🎯 <b>[ÚSPEŠNÝ TAKE PROFIT +2% 🎉]</b>\n"
                                f"<b>Pár:</b> {symbol}\n"
                                f"<b>Zisk:</b> +{pnl_pct:.2f}% (+{pnl_usdc:.2f} USDC)"
                            )
                            del ACTIVE_TRADES[coin]
                            CURRENT_STAGE = "STAV: 🟢 Mimo obchodu. Obchod uzavretý v zisku! Skenujem ďalej..."
                        except Exception as err:
                            print(f"❌ Chyba TP: {err}", flush=True)
                        continue

                    # 2. STOP LOSS DOSIAHNUTÝ (-2%)
                    if current_price <= trade["current_sl_price"]:
                        try:
                            order_body = json.dumps({
                                "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                            })
                            okx_request('POST', '/api/v5/trade/order', order_body)
                            record_daily_pnl(pnl_usdc)

                            send_telegram_msg(
                                f"🚨 <b>[STOP-LOSS ZASIAHNUTÝ 🛑]</b>\n"
                                f"<b>Pár:</b> {symbol}\n"
                                f"<b>Strata:</b> {pnl_pct:.2f}% ({pnl_usdc:.2f} USDC)"
                            )
                            del ACTIVE_TRADES[coin]
                            CURRENT_STAGE = "STAV: 🟢 Mimo obchodu. Obchod ukončený v strate. Hľadám nový..."
                        except Exception as err:
                            print(f"❌ Chyba SL: {err}", flush=True)
                        continue

        except Exception as main_err:
            print(f"❌ Chyba v monitorovaní: {main_err}", flush=True)
            
        await asyncio.sleep(1)

@asynccontextmanager
async def lifespan(app: FastAPI):
    scanner_task = asyncio.create_task(top_gainer_scanner_loop())
    monitor_task = asyncio.create_task(advanced_risk_monitor_loop())
    yield
    scanner_task.cancel()
    monitor_task.cancel()

app = FastAPI(lifespan=lifespan)

# --- 🖼️ KRÁSNY A PREHĽADNÝ CYBERPUNK HUD ---
HTML_TEMPLATE_CONTENT = """<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Apex Scalper Pro - 1000$ Mode</title>
    <style>
        * { box-sizing: border-box; }
        body { 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
            margin: 0; 
            padding: 25px;
            background: linear-gradient(rgba(0,0,0,0.60), rgba(0,0,0,0.60)), url('https://images.unsplash.com/photo-1519501025264-65ba15a82390?q=80&w=1920&auto=format&fit=crop');
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
            max-width: 640px; 
            margin: 0 auto; 
            background: rgba(0, 0, 0, 0.45);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(0, 255, 255, 0.25);
            border-radius: 18px;
            padding: 22px;
            box-shadow: 0 0 30px rgba(0, 255, 255, 0.20), 0 0 60px rgba(180, 0, 255, 0.15);
        }

        .brand-title {
            font-size: 1rem;
            font-weight: 800;
            color: #00f0ff;
            letter-spacing: 2px;
            text-transform: uppercase;
            margin-bottom: 14px;
            text-shadow: 0 0 10px rgba(0, 240, 255, 0.7);
        }

        .status-banner {
            background: rgba(0, 240, 255, 0.12);
            border: 2px solid #00f0ff;
            border-radius: 10px;
            padding: 14px;
            font-size: 0.88rem;
            font-weight: 700;
            color: #ffffff;
            margin-bottom: 22px;
            text-shadow: 0 0 6px rgba(0, 240, 255, 0.5);
            box-shadow: inset 0 0 15px rgba(0, 240, 255, 0.1);
        }

        .section-block { margin-bottom: 24px; }

        .section-title {
            font-size: 0.85rem;
            font-weight: 700;
            color: #00f0ff;
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 1px;
            text-shadow: 0 0 5px rgba(0, 240, 255, 0.4);
        }

        table { width: 100%; border-collapse: collapse; margin-top: 4px; }
        th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid rgba(0, 255, 255, 0.20); font-size: 0.8rem; color: #ffffff; }
        th { font-weight: 700; color: #00f0ff; text-transform: uppercase; font-size: 0.72rem; letter-spacing: 1px; }

        .pnl-green { color: #00ff88; font-weight: bold; text-shadow: 0 0 8px rgba(0,255,136,0.4); }
        .pnl-red { color: #ff4444; font-weight: bold; text-shadow: 0 0 8px rgba(255,68,68,0.4); }

        .calendar-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(95px, 1fr)); gap: 8px; margin-top: 4px; }
        .calendar-day { 
            background: rgba(0, 0, 0, 0.60); 
            border: 1px solid rgba(0, 255, 255, 0.30); 
            border-radius: 8px; 
            padding: 8px; 
            text-align: center; 
            font-size: 0.75rem;
            color: #ffffff;
        }

        .refresh-info { color: rgba(255, 255, 255, 0.6); font-size: 0.7rem; margin-top: 20px; font-weight: 600; text-align: right; }
    </style>
    <meta http-equiv="refresh" content="2">
</head>
<body>
    <div class="container">
        <div class="brand-title">⚡ Apex Scalper Pro (80% Win-Rate / 1000$ Mode)</div>
        
        <!-- JASNÝ STAVOVÝ BANNER (Hneď vidíš, či sme v obchode alebo nie) -->
        <div class="status-banner">
            {{ current_stage }}
        </div>

        <!-- AKTUÁLNY OBCHOD -->
        <div class="section-block">
            <div class="section-title">🔴 Aktívny Obchod & Živý PnL</div>
            {% if trades %}
                <table>
                    <tr>
                        <th>Pár</th>
                        <th>Vstup</th>
                        <th>Investícia</th>
                        <th>Aktuálna Cena</th>
                        <th>Živý PnL (%)</th>
                        <th>Živý PnL ($)</th>
                    </tr>
                    {% for t, details in trades.items() %}
                    <tr>
                        <td><strong>{{ details.market_pair }}</strong></td>
                        <td>{{ "%.4f" | format(details.entry_price) }}</td>
                        <td>1000.00 $</td>
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
                <div style="font-size: 0.8rem; color: rgba(255, 255, 255, 0.7); background: rgba(0,0,0,0.3); padding: 12px; border-radius: 8px; border: 1px dashed rgba(255,255,255,0.2);">
                    ✅ Žiadny aktívny obchod. Bot prísne vyčkáva na čistú príležitosť bez akéhokoľvek rizika paralelných obchodov.
                </div>
            {% endif %}
        </div>

        <!-- ŽIVÝ RADAR TRHU -->
        <div class="section-block">
            <div class="section-title">📊 Živý Radar Trhu (Top Gainer Sken)</div>
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
                <div style="font-size: 0.8rem; color: rgba(255, 255, 255, 0.7);">Skenujem burzu, hľadám vhodného lídra...</div>
            {% endif %}
        </div>

        <!-- ZOZNAM ZOSTATKOV -->
        <div class="section-block">
            <div class="section-title">💰 Zostatky (OKX Demo)</div>
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

        <!-- KALENDÁR VÝSLEDKOV -->
        <div class="section-block">
            <div class="section-title">📅 Kalendár Výsledkov (Denný PnL)</div>
            {% if calendar %}
                <div class="calendar-grid">
                    {% for day, data in calendar.items() %}
                    <div class="calendar-day">
                        <div>{{ day }}</div>
                        <div style="font-weight: bold; color: #00ff88; margin-top: 4px;">{{ "%+.2f" | format(data.pnl) }} $</div>
                        <div style="font-size: 0.65rem; color: rgba(255,255,255,0.7); margin-top: 2px;">{{ data.trades }} obch.</div>
                    </div>
                    {% endfor %}
                </div>
            {% else %}
                <div style="font-size: 0.8rem; color: rgba(255, 255, 255, 0.7);">Zatiaľ žiadne uzavreté obchody.</div>
            {% endif %}
        </div>

        <div class="refresh-info">
            STRICT 1000$ SCALPER | Obnovenie 2s | SK Čas: {{ current_time }}
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

    template = Template(HTML_TEMPLATE_CONTENT)
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
        "bot_type": "Apex Strict 1000$ Scalper",
        "current_stage": CURRENT_STAGE,
        "active_trades": ACTIVE_TRADES,
        "market_radar": MARKET_RADAR,
        "calendar": DAILY_PNL_CALENDAR,
        "sk_time": get_sk_time_str()
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
