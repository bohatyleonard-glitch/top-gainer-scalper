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
ACTIVE_TRADES = {}       
MARKET_RADAR = []        
DAILY_PNL_CALENDAR = {}  
CURRENT_STAGE = "STAV: Mimo obchodu. Skenujem trh pre stabilný scalp..."
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

# --- 🔍 SKENER S FLEXIBILNÝM ROZPOČTOM A OCHRANOU ---
async def top_gainer_scanner_loop():
    global ACTIVE_TRADES, LAST_TRADE_TIME, MARKET_RADAR, CURRENT_STAGE
    await asyncio.sleep(5)
    
    start_msg = f"[{get_sk_time_str()}] ⚡ STABILNÝ SCALPER SPUSTENÝ (Flexibilný rozpočet & Vlny)"
    print(start_msg, flush=True)
    send_telegram_msg(start_msg)

    while True:
        try:
            if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES:
                CURRENT_STAGE = "STAV: 🔴 V OBCHODE! Čakám na dokončenie pozície..."
                await asyncio.sleep(5)
                continue

            CURRENT_STAGE = "STAV: 🟢 MIMO OBCHODU. Skenujem trh..."
            tickers_res = okx_request('GET', '/api/v5/market/tickers?instType=SPOT')
            if tickers_res.get('code') != '0' or not tickers_res.get('data'):
                await asyncio.sleep(5)
                continue

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

                    if open24h > 0 and last_p > 0 and vol24h > 10000: #[cite: 2] Zmiernený objem
                        change_pct = ((last_p - open24h) / open24h) * 100
                        if 1.0 <= change_pct <= 30.0: # Zmiernený rast
                            probability = int(min(max((change_pct / 30.0) * 100, 50), 99))
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

                LAST_TRADE_TIME[coin_name] = now_ts
                CURRENT_STAGE = f"STAV: Overujem zostatok pre {best_target['instId']}..."
                
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
                    CURRENT_STAGE = f"STAV: ⚠️ Nedostatok USDC (Máš {free_capital:.2f})."
                    await asyncio.sleep(15)
                    continue

                # Flexibilné využitie kapitálu (zamedzuje chybám 51008 ak nemáš presne 1000 USDC)
                safe_budget = min(free_capital * 0.98, 1000.0)
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
                    print(f"❌ Nákup zlyhal na burze: {order_res}", flush=True)
                    await asyncio.sleep(5)
                    continue

                tp_price = current_price * 1.02
                sl_price = current_price * 0.985

                ACTIVE_TRADES[coin_name] = {
                    "market_pair": best_target['instId'],
                    "entry_price": current_price,
                    "current_price": current_price,
                    "highest_price": current_price,
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

                CURRENT_STAGE = f"STAV: 🔴 V OBCHODE ({best_target['instId']}) - Sledujem vlnu..."

                send_telegram_msg(
                    f"🟢 <b>[⚡ ČISTÝ SPOT NÁKUP]</b>\n"
                    f"<b>Pár:</b> {best_target['instId']}\n"
                    f"<b>Vstupná cena:</b> {current_price:.4f} USDC\n"
                    f"<b>Investícia:</b> ~{safe_budget:.2f} USDC\n"
                    f"<b>Stop-Loss:</b> {sl_price:.4f} USDC"
                )

        except Exception as scan_err:
            print(f"❌ Chyba v skeneri: {scan_err}", flush=True)

        await asyncio.sleep(5)

# --- 🛡️ MONITOR OBCHODU S BREAK-EVEN A TRAILING TP ---
async def advanced_risk_monitor_loop():
    global ACTIVE_TRADES, CURRENT_STAGE
    await asyncio.sleep(3)
    
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
                
                bought_time = datetime.strptime(trade["bought_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=1)))
                
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
                            f"<b>Výsledok:</b> {trade['live_pnl_pct']:+.2f}%"
                        )
                    except Exception:
                        pass
                    
                    if coin in ACTIVE_TRADES:
                        del ACTIVE_TRADES[coin]
                    CURRENT_STAGE = "STAV: 🟢 Mimo obchodu. Hľadám príležitosť..."
                    continue
                
                ticker_res = okx_request('GET', f'/api/v5/market/ticker?instId={symbol}')
                if ticker_res.get('code') == '0' and ticker_res.get('data'):
                    current_price = float(ticker_res['data'][0]['last'])
                    pnl_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
                    pnl_usdc = trade.get("invested_usdc", 100.0) * (pnl_pct / 100.0)

                    trade["current_price"] = current_price
                    trade["live_pnl_pct"] = pnl_pct
                    trade["live_pnl_usdc"] = pnl_usdc

                    if current_price > trade["highest_price"]:
                        trade["highest_price"] = current_price
                        
                        if pnl_pct >= 1.0:
                            new_trailing_sl = trade["highest_price"] * 0.995
                            if new_trailing_sl > trade["current_sl_price"]:
                                trade["current_sl_price"] = new_trailing_sl

                    if not trade["break_even_activated"] and pnl_pct >= 0.7:
                        trade["break_even_activated"] = True
                        if trade["entry_price"] > trade["current_sl_price"]:
                            trade["current_sl_price"] = trade["entry_price"]
                            send_telegram_msg(
                                f"🛡️ <b>[BREAK-EVEN AKTIVOVANÝ]</b>\n"
                                f"<b>Pár:</b> {symbol}\n"
                                f"<b>Ochrana na vstupe:</b> {trade['entry_price']:.4f} USDC"
                            )

                    if not trade["trailing_tp_activated"] and pnl_pct >= 1.2:
                        trade["trailing_tp_activated"] = True
                        send_telegram_msg(
                            f"🌊 <b>[TRAILING TP AKTIVOVANÝ]</b>\n"
                            f"<b>Pár:</b> {symbol}\n"
                            f"<b>Zisk:</b> +{pnl_pct:.2f}%. Sledujem vlnu."
                        )

                    if trade["trailing_tp_activated"]:
                        drop_from_peak = ((trade["highest_price"] - current_price) / trade["highest_price"]) * 100
                        if drop_from_peak >= 0.4:
                            try:
                                order_body = json.dumps({
                                    "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                                })
                                okx_request('POST', '/api/v5/trade/order', order_body)
                                record_daily_pnl(pnl_usdc)

                                send_telegram_msg(
                                    f"🎯 <b>[TRAILING TAKE PROFIT 🎉]</b>\n"
                                    f"<b>Pár:</b> {symbol}\n"
                                    f"<b>Výsledný zisk:</b> +{pnl_pct:.2f}% (+{pnl_usdc:+.2f} USDC)"
                                )
                            except Exception as err:
                                print(f"❌ Chyba Trailing TP: {err}", flush=True)

                            if coin in ACTIVE_TRADES:
                                del ACTIVE_TRADES[coin]
                            CURRENT_STAGE = "STAV: 🟢 Mimo obchodu. Vlna vyťažená!"
                            continue

                    if current_price <= trade["current_sl_price"]:
                        try:
                            order_body = json.dumps({
                                "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                            })
                            okx_request('POST', '/api/v5/trade/order', order_body)
                            record_daily_pnl(pnl_usdc)

                            sl_type_msg = "Zabezpečený výstup (Break-Even/Trailing)" if pnl_pct >= 0 else "Stop-Loss"
                            send_telegram_msg(
                                f"🚨 <b>[{sl_type_msg} ZASIAHNUTÝ 🛑]</b>\n"
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

# --- 🖼️ WEB DASHBOARD ---
HTML_TEMPLATE_CONTENT = """<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Apex Scalper Pro - Dashboard</title>
    <style>
        * { box-sizing: border-box; }
        body { 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
            margin: 0; padding: 25px;
            background: linear-gradient(rgba(0,0,0,0.35), rgba(0,0,0,0.35)), url('https://images.unsplash.com/photo-1519501025264-65ba15a82390?q=80&w=1920&auto=format&fit=crop');
            background-size: cover; background-position: center; background-attachment: fixed;
            color: #ffffff; min-height: 100vh;
        }
        .container { 
            max-width: 620px; margin: 0; background: rgba(0, 0, 0, 0.65);
            backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(0, 255, 255, 0.3); border-radius: 18px; padding: 22px;
            box-shadow: 0 0 35px rgba(0, 255, 255, 0.25);
        }
        .brand-title { font-size: 1rem; font-weight: 800; color: #00f0ff; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 14px; }
        .status-banner { background: rgba(0, 240, 255, 0.15); border: 2px solid #00f0ff; border-radius: 10px; padding: 14px; font-size: 0.88rem; font-weight: 700; color: #ffffff; margin-bottom: 22px; }
        .section-block { margin-bottom: 24px; }
        .section-title { font-size: 0.85rem; font-weight: 700; color: #00f0ff; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px; }
        table { width: 100%; border-collapse: collapse; margin-top: 4px; }
        th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid rgba(0, 255, 255, 0.20); font-size: 0.8rem; color: #ffffff; }
        th { font-weight: 700; color: #00f0ff; text-transform: uppercase; font-size: 0.72rem; }
        .pnl-green { color: #00ff88; font-weight: bold; }
        .pnl-red { color: #ff4444; font-weight: bold; }
        .calendar-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(95px, 1fr)); gap: 8px; margin-top: 4px; }
        .calendar-day { background: rgba(0, 0, 0, 0.70); border: 1px solid rgba(0, 255, 255, 0.30); border-radius: 8px; padding: 8px; text-align: center; font-size: 0.75rem; }
        .refresh-info { color: rgba(255, 255, 255, 0.6); font-size: 0.7rem; margin-top: 20px; font-weight: 600; text-align: left; }
    </style>
    <meta http-equiv="refresh" content="2">
</head>
<body>
    <div class="container">
        <div class="brand-title">⚡ Apex Scalper Pro (Flexible Budget)</div>
        
        <div class="status-banner">{{ current_stage }}</div>

        <div class="section-block">
            <div class="section-title">🔴 Aktívny Obchod & Živý PnL</div>
            {% if trades %}
                <table>
                    <tr><th>Pár</th><th>Vstup</th><th>Investícia</th><th>Aktuálna Cena</th><th>PnL (%)</th><th>PnL ($)</th></tr>
                    {% for t, details in trades.items() %}
                    <tr>
                        <td><strong>{{ details.market_pair }}</strong></td>
                        <td>{{ "%.4f" | format(details.entry_price) }}</td>
                        <td>{{ "%.2f" | format(details.invested_usdc) }} $</td>
                        <td>{{ "%.4f" | format(details.current_price) }}</td>
                        <td class="{{ 'pnl-green' if details.live_pnl_pct >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(details.live_pnl_pct) }}%</td>
                        <td class="{{ 'pnl-green' if details.live_pnl_usdc >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(details.live_pnl_usdc) }} $</td>
                    </tr>
                    {% endfor %}
                </table>
            {% else %}
                <div style="font-size: 0.8rem; color: rgba(255, 255, 255, 0.8); background: rgba(0,0,0,0.4); padding: 12px; border-radius: 8px; border: 1px dashed rgba(0,255,255,0.3);">
                    ✅ Žiadny aktívny obchod. Bot vyčkáva na príležitosť.
                </div>
            {% endif %}
        </div>

        <div class="section-block">
            <div class="section-title">📊 Živý Radar Trhu</div>
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
                <div style="font-size: 0.8rem; color: rgba(255, 255, 255, 0.7);">Skenujem trh...</div>
            {% endif %}
        </div>

        <div class="section-block">
            <div class="section-title">💰 Zostatky (OKX Demo)</div>
            <table>
                <tr><th>Mena</th><th>Dostupné</th></tr>
                {% for coin, data in balances.items() %}
                <tr><td><strong>{{ coin }}</strong></td><td>{{ "%.4f" | format(data.available_balance | float) }}</td></tr>
                {% endfor %}
            </table>
        </div>

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

        <div class="refresh-info">FLEXIBLE SCALPER | Obnovenie 2s | SK Čas: {{ current_time }}</div>
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
    return template.render(
        balances=account_balances,
        trades=ACTIVE_TRADES,
        radar=MARKET_RADAR,
        calendar=DAILY_PNL_CALENDAR,
        current_stage=CURRENT_STAGE,
        current_time=get_sk_time_str()
    )

@app.get("/api/status")
async def api_status():
    return {
        "status": "online",
        "bot_type": "Apex Flexible Scalper",
        "current_stage": CURRENT_STAGE,
        "active_trades": ACTIVE_TRADES,
        "market_radar": MARKET_RADAR,
        "calendar": DAILY_PNL_CALENDAR,
        "sk_time": get_sk_time_str()
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
