import os
import time
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

# Globálny stav bota pre Dashboard a logiku
bot_state = {
    "status": "online",
    "bot_type": "Apex Scalper Pro (Stabilized USDC Spot)",
    "last_scan": "Zatiaľ neskenované",
    "account_balances": {},
    "active_trades": {},
    "logs": []
}

def log_message(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {msg}"
    print(formatted, flush=True)
    bot_state["logs"].insert(0, formatted)
    if len(bot_state["logs"]) > 50:
        bot_state["logs"].pop()

LAST_TRADE_TIME = {}
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
    try:
        if method.upper() == 'GET':
            response = requests.get(url, headers=headers, timeout=10)
        else:
            response = requests.post(url, headers=headers, data=body, timeout=10)
        return response.json()
    except Exception as err:
        return {"code": "-1", "msg": str(err)}

# --- HTML DASHBOARD ŠABLÓNA ---
DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Apex Scalper Pro - Dashboard</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }
        .container { max-width: 1000px; margin: 0 auto; }
        .header { display: flex; justify-content: space-between; align-items: center; background: #1e293b; padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        .status-badge { background: #22c55e; color: white; padding: 6px 12px; border-radius: 20px; font-weight: bold; font-size: 0.9em; }
        .card-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 20px; }
        .card { background: #1e293b; padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        h2 { margin-top: 0; color: #38bdf8; font-size: 1.2em; border-bottom: 2px solid #334155; padding-bottom: 8px; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { text-align: left; padding: 8px; border-bottom: 1px solid #334155; font-size: 0.9em; }
        th { color: #94a3b8; }
        .logs-box { background: #090d16; padding: 15px; border-radius: 8px; font-family: monospace; font-size: 0.85em; height: 200px; overflow-y: auto; color: #34d399; margin-top: 20px; }
        .refresh-info { text-align: center; color: #64748b; font-size: 0.8em; margin-top: 15px; }
        .pnl-green { color: #22c55e; font-weight: bold; }
        .pnl-red { color: #ef4444; font-weight: bold; }
    </style>
    <meta http-equiv="refresh" content="10">
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>⚡ Apex Scalper Pro (Stabilized)</h1>
                <p style="margin: 0; color: #94a3b8;">Spot USDC Scalper (OKX Demo)</p>
            </div>
            <div>
                <span class="status-badge">ONLINE</span>
            </div>
        </div>

        <div class="card-grid">
            <div class="card">
                <h2>💰 Zostatky (availBal)</h2>
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
                        <tr><th>Pár</th><th>Vstup</th><th>PnL (%)</th></tr>
                        {% for t, details in trades.items() %}
                        <tr>
                            <td><strong>{{ details.market_pair }}</strong></td>
                            <td>{{ "%.4f" | format(details.entry_price) }}</td>
                            <td class="{{ 'pnl-green' if details.live_pnl_pct >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(details.live_pnl_pct) }}%</td>
                        </tr>
                        {% endfor %}
                    </table>
                {% else %}
                    <p style="color: #94a3b8; text-align: center; margin-top: 30px;">✅ Žiadny aktívny obchod. Skenujem trh...</p>
                {% endif %}
            </div>
        </div>

        <div class="card" style="margin-top: 20px;">
            <h2>📋 Živé Logy Bota</h2>
            <div class="logs-box">
                {% for log in logs %}
                <div>{{ log }}</div>
                {% endfor %}
            </div>
        </div>

        <div class="refresh-info">
            Stránka sa automaticky obnovuje každých 10 sekúnd. | Aktuálny čas: {{ current_time }}
        </div>
    </div>
</body>
</html>
"""

# --- 🔍 SKENOVACIA SLUČKA (BACKGROUND WORKER) ---
async def top_gainer_scanner_loop():
    global bot_state, LAST_TRADE_TIME
    await asyncio.sleep(5)
    log_message("⚡ Stabilizovaný Skener spustený [USDC Spot Režim]...")
    send_telegram_msg("🤖 <b>Apex Scalper Pro spustený!</b> Skenujem USDC páry s ochranou proti zacykleniu.")

    while True:
        try:
            bot_state["last_scan"] = get_sk_time_str()

            # Aktualizácia zostatkov pre dashboard
            balance_res = okx_request('GET', '/api/v5/account/balance')
            if balance_res.get('code') == '0':
                acc_bals = {}
                free_capital = 0.0
                for acc in balance_res.get('data', []):
                    for detail in acc.get('details', []):
                        ccy = detail.get('ccy')
                        if ccy in ["USDC", "USDT", "EUR"]:
                            avail = float(detail.get('availBal', 0) or 0)
                            acc_bals[ccy] = {"available_balance": avail}
                            if ccy == 'USDC':
                                free_capital = avail
                bot_state["account_balances"] = acc_bals

            if len(bot_state["active_trades"]) >= MAX_PARALLEL_TRADES:
                await asyncio.sleep(10)
                continue

            if balance_res.get('code') != '0':
                await asyncio.sleep(10)
                continue

            if free_capital < 10.0:
                log_message(f"🛑 Nedostatok USDC kapitálu: {free_capital:.2f}")
                await asyncio.sleep(15)
                continue

            tickers_res = okx_request('GET', '/api/v5/market/tickers?instType=SPOT')
            if tickers_res.get('code') != '0' or not tickers_res.get('data'):
                await asyncio.sleep(10)
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

                    if open24h > 0 and last_p > 0 and vol24h >= 20000: # Zvýšený objem pre istotu
                        change_pct = ((last_p - open24h) / open24h) * 100
                        if 1.5 <= change_pct <= 25.0: # Konzervatívnejšie rozpätie rastu
                            candidates.append({
                                "instId": inst_id,
                                "price": last_p,
                                "change": change_pct,
                                "volume": vol24h
                            })
                except Exception:
                    continue

            if not candidates:
                await asyncio.sleep(15)
                continue

            candidates.sort(key=lambda x: x['change'], reverse=True)
            best_target = candidates[0]
            coin_name = best_target['instId'].split('-')[0]

            now_ts = datetime.now().timestamp()
            if coin_name in LAST_TRADE_TIME:
                # 🛡️ 5-MINÚTOVÝ COOLDOWN (žiadne bleskové opakovanie tej istej mince)
                if now_ts - LAST_TRADE_TIME[coin_name] < 300:
                    await asyncio.sleep(10)
                    continue

            async with trade_lock:
                if len(bot_state["active_trades"]) >= MAX_PARALLEL_TRADES:
                    continue

                LAST_TRADE_TIME[coin_name] = now_ts
                log_message(f"🎯 Vybraný stabilný kandidát: {best_target['instId']} (+{best_target['change']:.2f}%)")

                safe_budget = min(free_capital * 0.95, 500.0) # Bezpečnejšia alokácia
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
                    "sz": str(formatted_sz),
                    "tgtCcy": "base_ccy"
                })

                order_res = okx_request('POST', '/api/v5/trade/order', order_body)
                if order_res.get('code') != '0':
                    log_message(f"❌ Nákup zlyhal: {order_res}")
                    send_telegram_msg(f"❌ <b>Chyba nákupu USDC:</b> {order_res.get('msg', 'Neznáma')}")
                    await asyncio.sleep(10)
                    continue

                tp_price = current_price * 1.025
                sl_price = current_price * 0.98  # Bezpečnejší Stop-Loss (-2%)

                bot_state["active_trades"][coin_name] = {
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

                send_telegram_msg(
                    f"🟢 <b>[STABILIZOVANÝ SPOT NÁKUP]</b>\n"
                    f"<b>Pár:</b> {best_target['instId']}\n"
                    f"<b>Vstup:</b> {current_price:.4f} USDC\n"
                    f"<b>Investícia:</b> ~{safe_budget:.2f} USDC\n"
                    f"<b>Stop-Loss:</b> {sl_price:.4f} USDC"
                )

        except Exception as scan_err:
            log_message(f"❌ Chyba v skeneri: {scan_err}")

        await asyncio.sleep(10)

# --- 🛡️ MONITOROVACÍ CYKLUS (RISK MANAGEMENT) ---
async def advanced_risk_monitor_loop():
    global bot_state
    await asyncio.sleep(5)
    log_message("🛡️ Stabilný Risk Management Monitor spustený...")

    while True:
        if not bot_state["active_trades"]:
            await asyncio.sleep(3)
            continue
            
        try:
            now_time = datetime.now(timezone.utc) + timedelta(hours=1)
            
            for coin in list(bot_state["active_trades"].keys()):
                if coin not in bot_state["active_trades"]:
                    continue
                trade = bot_state["active_trades"][coin]
                symbol = trade["market_pair"]
                
                bought_time = datetime.strptime(trade["bought_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=1)))
                
                # Predĺžený 30-minútový časový limit na priestor pre rast
                if now_time - bought_time > timedelta(minutes=30):
                    try:
                        order_body = json.dumps({
                            "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                        })
                        okx_request('POST', '/api/v5/trade/order', order_body)
                        log_message(f"⏱️ 30m časový limit vypršal pre {symbol}")
                        send_telegram_msg(f"⏱️ <b>[30m LIMIT - UZATVORENÉ]</b> Pár {symbol} ({trade['live_pnl_pct']:+.2f}%)")
                    except Exception:
                        pass
                    
                    if coin in bot_state["active_trades"]:
                        del bot_state["active_trades"][coin]
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
                        if pnl_pct >= 1.2:
                            new_trailing_sl = trade["highest_price"] * 0.992
                            if new_trailing_sl > trade["current_sl_price"]:
                                trade["current_sl_price"] = new_trailing_sl

                    # Break-Even na +0.8%
                    if not trade["break_even_activated"] and pnl_pct >= 0.8:
                        trade["break_even_activated"] = True
                        if trade["entry_price"] > trade["current_sl_price"]:
                            trade["current_sl_price"] = trade["entry_price"]
                            send_telegram_msg(f"🛡️ <b>[BREAK-EVEN]</b> {symbol} zabezpečený na vstupe.")

                    # Trailing TP od +1.5%
                    if not trade["trailing_tp_activated"] and pnl_pct >= 1.5:
                        trade["trailing_tp_activated"] = True
                        send_telegram_msg(f"🌊 <b>[TRAILING TP AKTÍVNY]</b> Pár {symbol} (+{pnl_pct:.2f}%)")

                    if trade["trailing_tp_activated"]:
                        drop_from_peak = ((trade["highest_price"] - current_price) / trade["highest_price"]) * 100
                        if drop_from_peak >= 0.5:
                            try:
                                order_body = json.dumps({
                                    "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                                })
                                okx_request('POST', '/api/v5/trade/order', order_body)
                                log_message(f"🎯 Trailing TP pre {symbol} s výnosom {pnl_pct:+.2f}%")
                                send_telegram_msg(f"🎯 <b>[TAKE PROFIT]</b> {symbol} uzavretý v zisku +{pnl_pct:.2f}% (+{pnl_usdc:+.2f} USDC)")
                            except Exception as err:
                                log_message(f"❌ Chyba Trailing TP: {err}")

                            if coin in bot_state["active_trades"]:
                                del bot_state["active_trades"][coin]
                            continue

                    if current_price <= trade["current_sl_price"]:
                        try:
                            order_body = json.dumps({
                                "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                            })
                            okx_request('POST', '/api/v5/trade/order', order_body)
                            log_message(f"🚨 Stop-loss / Ochrana pre {symbol} pri {pnl_pct:+.2f}%")
                            send_telegram_msg(f"🚨 <b>[STOP-LOSS / OCHRANA]</b> Pár {symbol} ({pnl_pct:+.2f}%)")
                        except Exception as err:
                            log_message(f"❌ Chyba SL: {err}")

                        if coin in bot_state["active_trades"]:
                            del bot_state["active_trades"][coin]
                        continue

        except Exception as main_err:
            log_message(f"❌ Chyba v monitorovaní: {main_err}")
            
        await asyncio.sleep(3)

@asynccontextmanager
async def lifespan(app: FastAPI):
    scanner_task = asyncio.create_task(top_gainer_scanner_loop())
    monitor_task = asyncio.create_task(advanced_risk_monitor_loop())
    yield
    scanner_task.cancel()
    monitor_task.cancel()

app = FastAPI(lifespan=lifespan)

# --- WEBOVÉ CESTY (ROUTES) ---
@app.get("/", response_class=HTMLResponse)
def dashboard():
    balances = bot_state["account_balances"]
    if not balances:
        balances = {"USDC": {"available_balance": "0.00"}}
    
    template = Template(DASHBOARD_TEMPLATE)
    rendered_html = template.render(
        balances=balances,
        trades=bot_state["active_trades"],
        logs=bot_state["logs"],
        current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    return rendered_html

@app.get("/api/status")
def api_status():
    return {
        "status": bot_state["status"],
        "bot_type": bot_state["bot_type"],
        "account_balances": bot_state["account_balances"],
        "active_trades": bot_state["active_trades"],
        "sk_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
