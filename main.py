import os
import time
import hmac
import hashlib
import base64
import requests
import threading
from datetime import datetime
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

# --- NASTAVENIA OKX API (Načítavajú sa z Render Environment Variables) ---
API_KEY = os.getenv("OKX_API_KEY", "")
API_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
API_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
USE_DEMO = os.getenv("OKX_USE_DEMO", "true").lower() == "true"

BASE_URL = "https://www.okx.com"  # Pre demo aj live sa endpointy zhodujú, mení sa len hlavička/flag

# Globálny stav bota pre Dashboard
bot_state = {
    "status": "online",
    "bot_type": "Full-Auto Top Gainer Scalper (Dashboard Edition)",
    "last_scan": "Zatiaľ neskenované",
    "account_balances": {},
    "active_trades": {},
    "logs": []
}

def log_message(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {msg}"
    print(formatted)
    bot_state["logs"].insert(0, formatted)
    if len(bot_state["logs"]) > 50:
        bot_state["logs"].pop()

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
    </style>
    <meta http-equiv="refresh" content="10">
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>🤖 Top Gainer Scalper</h1>
                <p style="margin: 0; color: #94a3b8;">Autonómny bot na OKX Demo</p>
            </div>
            <div>
                <span class="status-badge">ONLINE</span>
            </div>
        </div>

        <div class="card-grid">
            <div class="card">
                <h2>💰 Zostatky (Účet)</h2>
                <table>
                    <tr><th>Mena</th><th>Dostupné</th></tr>
                    {% for coin, data in balances.items() %}
                    {% if data.available_balance | float > 0 %}
                    <tr>
                        <td><strong>{{ coin }}</strong></td>
                        <td>{{ "%.4f" | format(data.available_balance | float) }}</td>
                    </tr>
                    {% endif %}
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
                    <p style="color: #94a3b8; text-align: center; margin-top: 30px;">Žiadny aktívny obchod. Čakám na signál...</p>
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

# --- WEBOVÉ CESTY (ROUTES) ---
@app.route("/")
def dashboard():
    # Získame aktuálne zostatky z OKX (alebo simulované, ak API kľúče zatiaľ chýbajú)
    balances = bot_state["account_balances"]
    if not balances:
        balances = {"USDC": {"available_balance": "227.20"}}
        
    return render_template_string(
        DASHBOARD_TEMPLATE,
        balances=balances,
        trades=bot_state["active_trades"],
        logs=bot_state["logs"],
        current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

@app.route("/api/status")
def api_status():
    return jsonify({
        "status": bot_state["status"],
        "bot_type": bot_state["bot_type"],
        "account_balances": bot_state["account_balances"],
        "active_trades": bot_state["active_trades"],
        "sk_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

# --- HLAVNÁ SKENOVACIA SLUČKA (BACKGROUND WORKER) ---
def background_scanner():
    log_message("Background skener úspešne naštartovaný.")
    while True:
        try:
            bot_state["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # Tu prebieha samotná logika skenovania trhu (zatiaľ len iteratívny stavový check)
            # Logujeme aktivitu, aby bolo v logoch vidieť, že bot žije
            # log_message("Skenujem OKX USDC trhy...")
            
            # TODO: Tu sa napojia reálne OKX volania na sťahovanie tickerov a vyhodnocovanie 2% - 25% rastu
            
        except Exception as e:
            log_message(f"Chyba v skeneri: {str(e)}")
            
        time.sleep(15)

if __name__ == "__main__":
    # Spustenie skenera na pozadí
    t = threading.Thread(target=background_scanner, daemon=True)
    t.start()
    
    # Spustenie webového servera pre Render
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
