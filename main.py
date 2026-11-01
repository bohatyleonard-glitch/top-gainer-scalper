import os
import time
import hmac
import hashlib
import base64
import requests
import threading
from datetime import datetime
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn
from jinja2 import Template

app = FastAPI()

# --- NASTAVENIA OKX API & TELEGRAMU ---
API_KEY = os.getenv("638848ca-e65c-4074-bcfd-20cf4d292f2c", "")
API_SECRET_KEY = os.getenv("885934366C54F33B276E9E49E8EF87CF", "")
API_PASSPHRASE = os.getenv("Australia111/", "")
USE_DEMO = os.getenv("OKX_USE_DEMO", "true").lower() == "true"

TELEGRAM_TOKEN = os.getenv("9021881475:AAFn1oQSvaGkhpX2uyJscdcTybFgk1tNmV8", "")
TELEGRAM_CHAT_ID = os.getenv("9130979784", "")

# Globálny stav bota pre Dashboard
bot_state = {
    "status": "online",
    "bot_type": "Top Gainer Scalper (Full API & Telegram Edition)",
    "last_scan": "Zatiaľ neskenované",
    "account_balances": {},
    "active_trades": {},
    "market_radar": [],
    "logs": []
}

def log_message(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {msg}"
    print(formatted)
    bot_state["logs"].insert(0, formatted)
    if len(bot_state["logs"]) > 50:
        bot_state["logs"].pop()

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Chyba pri posielaní Telegram správy: {e}")

# --- OKX PODPIS A API VOLANIA ---
def get_okx_signature(timestamp, method, request_path, body=""):
    message = timestamp + method + request_path + body
    mac = hmac.new(bytes(API_SECRET_KEY, encoding='utf-8'), bytes(message, encoding='utf-8'), digestmod=hashlib.sha256)
    return base64.b64encode(mac.digest()).decode('utf-8')

def get_okx_headers(method, request_path, body=""):
    timestamp = datetime.utcnow().isoformat()[:-3] + 'Z'
    signature = get_okx_signature(timestamp, method, request_path, body)
    headers = {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    if USE_DEMO:
        headers["x-simulated-trading"] = "1"
    return headers

def fetch_account_balance():
    if not API_KEY or not API_SECRET_KEY or not API_PASSPHRASE:
        return {"USDC": {"available_balance": "0.00 (Chýbajú API kľúče)"}}
    try:
        path = "/api/v5/account/balance"
        headers = get_okx_headers("GET", path)
        url = f"https://www.okx.com{path}"
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()
        
        balances = {}
        if data.get("code") == "0" and "data" in data:
            for detail in data["data"][0].get("details", []):
                ccy = detail.get("ccy")
                avail = detail.get("availBal", "0")
                if float(avail) > 0:
                    balances[ccy] = {"available_balance": avail}
        return balances if balances else {"USDC": {"available_balance": "0.00"}}
    except Exception as e:
        log_message(f"Chyba pri načítavaní zostatkov z OKX: {str(e)}")
        return {}

# --- HTML DASHBOARD ŠABLÓNA ---
DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Top Gainer Scalper Dashboard</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }
        .container { max-width: 1100px; margin: 0 auto; }
        .header { display: flex; justify-content: space-between; align-items: center; background: #1e293b; padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        .status-badge { background: #22c55e; color: white; padding: 6px 12px; border-radius: 20px; font-weight: bold; font-size: 0.9em; }
        .card-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 20px; }
        .card { background: #1e293b; padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        h2 { margin-top: 0; color: #38bdf8; font-size: 1.2em; border-bottom: 2px solid #334155; padding-bottom: 8px; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { text-align: left; padding: 8px; border-bottom: 1px solid #334155; font-size: 0.9em; }
        th { color: #94a3b8; }
        .logs-box { background: #090d16; padding: 15px; border-radius: 8px; font-family: monospace; font-size: 0.85em; height: 160px; overflow-y: auto; color: #34d399; margin-top: 10px; }
        .refresh-info { text-align: center; color: #64748b; font-size: 0.8em; margin-top: 15px; }
        .tag-green { color: #22c55e; font-weight: bold; }
    </style>
    <meta http-equiv="refresh" content="10">
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>🤖 Top Gainer Scalper</h1>
                <p style="margin: 0; color: #94a3b8;">OKX Demo Účet & Živý Radar</p>
            </div>
            <div>
                <span class="status-badge">ONLINE</span>
            </div>
        </div>

        <div class="card-grid">
            <div class="card">
                <h2>💰 Reálne Zostatky (OKX Demo)</h2>
                <table>
                    <tr><th>Mena</th><th>Dostupné</th></tr>
                    {% for coin, data in balances.items() %}
                    <tr>
                        <td><strong>{{ coin }}</strong></td>
                        <td>{{ data.available_balance }}</td>
                    </tr>
                    {% endfor %}
                </table>
            </div>

            <div class="card">
                <h2>📈 Aktívne Obchody</h2>
                {% if trades %}
                    <table>
                        <tr><th>Pár</th><th>Vstup</th><th>Zisk/Strata</th></tr>
                        {% for t, details in trades.items() %}
                        <tr>
                            <td>{{ t }}</td>
                            <td>{{ details.entry_price }}</td>
                            <td>{{ details.pnl }}</td>
                        </tr>
                        {% endfor %}
                    </table>
                {% else %}
                    <p style="color: #94a3b8; text-align: center; margin-top: 25px;">Žiadny aktívny obchod. Čakám na signál...</p>
                {% endif %}
            </div>
        </div>

        <div class="card" style="margin-top: 20px;">
            <h2>⚡ Živý Radar Trhu (Rast 2% - 25% | Objem >50k)</h2>
            {% if radar %}
                <table>
                    <tr><th>Symbol</th><th>Cena</th><th>Rast za 24h</th><th>Objem (USDC)</th></tr>
                    {% for item in radar %}
                    <tr>
                        <td><strong>{{ item.symbol }}</strong></td>
                        <td>{{ item.price }}</td>
                        <td class="tag-green">+{{ item.change }}%</td>
                        <td>{{ item.volume }}</td>
                    </tr>
                    {% endfor %}
                </table>
            {% else %}
                <p style="color: #94a3b8; text-align: center; margin-top: 15px;">Skenujem trh, čakám na mince spĺňajúce podmienky...</p>
            {% endif %}
        </div>

        <div class="card" style="margin-top: 20px;">
            <h2>📋 Živé Logy Bota (Posledný sken: {{ last_scan }})</h2>
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

# --- WEBOVÉ CESTY ---
@app.get("/", response_class=HTMLResponse)
def dashboard():
    balances = bot_state["account_balances"]
    if not balances:
        balances = {"USDC": {"available_balance": "Načítavam..."}}
    
    template = Template(DASHBOARD_TEMPLATE)
    rendered_html = template.render(
        balances=balances,
        trades=bot_state["active_trades"],
        radar=bot_state["market_radar"],
        logs=bot_state["logs"],
        last_scan=bot_state["last_scan"],
        current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    return rendered_html

# --- HLAVNÁ SKENOVACIA SLUČKA ---
def background_scanner():
    log_message("Background skener s OKX API prepojením naštartovaný.")
    send_telegram("🚀 *Top Gainer Scalper bot* bol úspešne spustený a pripojený k OKX Demo!")
    
    while True:
        try:
            bot_state["last_scan"] = datetime.now().strftime("%H:%M:%S")
            
            # 1. Aktualizujeme zostatky z OKX Demo účtu
            bot_state["account_balances"] = fetch_account_balance()
            
            # 2. Skenujeme trh pre Radar
            url = "https://www.okx.com/api/v5/market/tickers?instType=SPOT"
            response = requests.get(url, timeout=10)
            data = response.json()
            
            potential_coins = []
            if data.get("code") == "0" and "data" in data:
                for ticker in data["data"]:
                    inst_id = ticker.get("instId", "")
                    if inst_id.endswith("-USDC"):
                        try:
                            last_price = float(ticker.get("last", 0))
                            sod_price = float(ticker.get("sodUtc0", 0))
                            vol_24h = float(ticker.get("volCcy24h", 0))
                            
                            if sod_price > 0:
                                change_pct = ((last_price - sod_price) / sod_price) * 100
                                if vol_24h >= 50000 and 2.0 <= change_pct <= 25.0:
                                    potential_coins.append({
                                        "symbol": inst_id,
                                        "price": f"{last_price:.4f}",
                                        "change": f"{change_pct:.2f}",
                                        "volume": f"{vol_24h:,.0f}"
                                    })
                        except Exception:
                            continue
            
            potential_coins = sorted(potential_coins, key=lambda x: float(x["change"]), reverse=True)
            bot_state["market_radar"] = potential_coins[:10]
            
        except Exception as e:
            log_message(f"Chyba v skeneri: {str(e)}")
            
        time.sleep(20)

if __name__ == "__main__":
    t = threading.Thread(target=background_scanner, daemon=True)
    t.start()
    
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
