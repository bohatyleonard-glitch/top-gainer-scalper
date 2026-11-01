import os
import time
from datetime import datetime
import requests
import threading
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn
from jinja2 import Template

app = FastAPI()

# --- NASTAVENIA OKX API ---
API_KEY = os.getenv("OKX_API_KEY", "")
API_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
API_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
USE_DEMO = os.getenv("OKX_USE_DEMO", "true").lower() == "true"

# Globálny stav bota pre Dashboard
bot_state = {
    "status": "online",
    "bot_type": "Top Gainer Scalper (Live Radar Edition)",
    "last_scan": "Zatiaľ neskenované",
    "account_balances": {},
    "active_trades": {},
    "market_radar": [],  # Tu budeme ukladať mince, ktoré spĺňajú kritériá rastu
    "logs": []
}

def log_message(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {msg}"
    print(formatted)
    bot_state["logs"].insert(0, formatted)
    if len(bot_state["logs"]) > 50:
        bot_state["logs"].pop()

# --- HTML DASHBOARD ŠABLÓNA S RADAROM ---
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
                <p style="margin: 0; color: #94a3b8;">Live Radar & OKX Demo Monitor</p>
            </div>
            <div>
                <span class="status-badge">ONLINE</span>
            </div>
        </div>

        <div class="card-grid">
            <!-- 1. Zostatky -->
            <div class="card">
                <h2>💰 Zostatky (Účet)</h2>
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

            <!-- 2. Aktívne obchody -->
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

        <!-- 3. Živý Radar Trhu -->
        <div class="card" style="margin-top: 20px;">
            <h2>⚡ Živý Radar Trhu (Mince rastúce 2% - 25% s objemom >50k)</h2>
            {% if radar %}
                <table>
                    <tr><th>Symbol</th><th>Cena</th><th>Rast za 24h</th><th>Objem (USDC)</th></tr>
                    дова{% for item in radar %}
                    <tr>
                        <td><strong>{{ item.symbol }}</strong></td>
                        <td>{{ item.price }}</td>
                        <td class="tag-green">+{{ item.change }}%</td>
                        <td>{{ item.volume }}</td>
                    </tr>
                    {% endfor %}
                </table>
            {% else %}
                <p style="color: #94a3b8; text-align: center; margin-top: 15px;">Prebieha skenovanie trhu alebo žiadna minca momentálne nespĺňa kritériá...</p>
            {% endif %}
        </div>

        <!-- 4. Logy -->
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

# --- WEBOVÉ CESTY (ROUTES) ---
@app.get("/", response_class=HTMLResponse)
def dashboard():
    balances = bot_state["account_balances"]
    if not balances:
        balances = {"USDC": {"available_balance": "227.20"}}
    
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

@app.get("/api/status")
def api_status():
    return {
        "status": bot_state["status"],
        "market_radar": bot_state["market_radar"],
        "active_trades": bot_state["active_trades"],
        "sk_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

# --- HLAVNÁ SKENOVACIA SLUČKA (BACKGROUND WORKER) ---
def background_scanner():
    log_message("Background skener trhu úspešne naštartovaný.")
    while True:
        try:
            bot_state["last_scan"] = datetime.now().strftime("%H:%M:%S")
            
            # Stiahneme verejné tickery z OKX pre Spot trh
            url = "https://www.okx.com/api/v5/market/tickers?instType=SPOT"
            response = requests.get(url, timeout=10)
            data = response.json()
            
            potential_coins = []
            if data.get("code") == "0" and "data" in data:
                for ticker in data["data"]:
                    inst_id = ticker.get("instId", "")
                    # Zaujímajú nás iba USDC páry (napr. BTC-USDC, ETH-USDC, SOL-USDC...)
                    if inst_id.endswith("-USDC"):
                        try:
                            # OKX vracia zmenu za 24h v desatinnom čísle alebo percentách (napr. 0.05 = 5%)
                            # V OKX API v5 je 'sodUtc0' alebo priamo výpočet z ceny, skúsme 'change24h' ak existuje,
                            # alebo si spočítame open24h a last. Pozrime sa na štandardné pole v OKX:
                            # V OKX v5 /market/tickers vracajú pole 'last' (aktuálna cena) a 'sodUtc0' (cena o 00:00 UTC)
                            last_price = float(ticker.get("last", 0))
                            sod_price = float(ticker.get("sodUtc0", 0))
                            vol_24h = float(ticker.get("volCcy24h", 0)) # Objem v sekundárnej mene (USDC)
                            
                            if sod_price > 0:
                                change_pct = ((last_price - sod_price) / sod_price) * 100
                                
                                # Filter: Objem > 50 000 USDC a rast medzi 2% a 25%
                                if vol_24h >= 50000 and 2.0 <= change_pct <= 25.0:
                                    potential_coins.append({
                                        "symbol": inst_id,
                                        "price": f"{last_price:.4f}",
                                        "change": f"{change_pct:.2f}",
                                        "volume": f"{vol_24h:,.0f}"
                                    })
                        except Exception:
                            continue
            
            # Zoradíme podľa najväčšieho rastu
            potential_coins = sorted(potential_coins, key=lambda x: float(x["change"]), reverse=True)
            bot_state["market_radar"] = potential_coins[:10]  # Uložíme top 10 mincí
            
            if potential_coins:
                log_message(f"Sken OKX: Nájdených {len(potential_coins)} mincí spĺňajúcich kritériá.")
            else:
                log_message("Sken OKX: Žiadna mincia aktuálne nespĺňa rast 2-25% s objemom >50k.")

        except Exception as e:
            log_message(f"Chyba pri skenovaní OKX: {str(e)}")
            
        time.sleep(20)  # Skenuje každých 20 sekúnd

if __name__ == "__main__":
    t = threading.Thread(target=background_scanner, daemon=True)
    t.start()
    
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
