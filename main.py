import os
import time
from datetime import datetime
import requests
import threading
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn
from jinja2 import Template

app = FastAPI()

# --- NASTAVENIA BOTA ---
SETTINGS = {
    "bot_enabled": True,
    "webhook_secret": "moje_tajne_heslo_123",
    "max_parallel_trades": 1,
    "trail_pct": 1.5,
    "be_trigger_pct": 1.2,
    "ttp_trigger_pct": 1.2,
    "ttp_callback_pct": 0.4
}

# Globálny stav bota pre Dashboard
bot_state = {
    "status": "online",
    "bot_type": "Webhook Scalper Dashboard (FastAPI)",
    "last_scan": "Aktívne (Webhook režim)",
    "account_balances": {"USDC": {"available_balance": "227.20"}},
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

# --- TELEGRAM NOTIFIKÁCIE ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        log_message(f"Chyba pri odosielaní Telegram správy: {e}")

# --- HTML DASHBOARD ŠABLÓNA ---
DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Top Gainer Scalper - CyberHUD</title>
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
        button.danger { background: #da3633; color: white; border: none; padding: 10px 15px; cursor: pointer; font-weight: bold; border-radius: 4px; margin-top: 10px; }
        button.danger:hover { opacity: 0.9; }
    </style>
    <meta http-equiv="refresh" content="10">
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>🤖 Top Gainer Scalper [CyberHUD]</h1>
                <p style="margin: 0; color: #94a3b8;">Webhook Režim (FastAPI)</p>
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
                        <tr><th>Pár</th><th>Vstup</th><th>Status</th></tr>
                        {% for t, details in trades.items() %}
                        <tr>
                            <td>{{ t }}</td>
                            <td>{{ details.entry_price }}</td>
                            <td>Aktívna pozícia</td>
                        </tr>
                        {% endfor %}
                    </table>
                {% else %}
                    <p style="color: #94a3b8; text-align: center; margin-top: 30px;">Žiadny aktívny obchod. Čakám na webhook...</p>
                {% endif %}
            </div>
        </div>

        <div class="card" style="margin-top: 20px;">
            <h2>🚨 Núdzové Ovládanie</h2>
            <form action="/kill" method="POST">
                <button type="submit" class="danger">KILL SWITCH (Zavrieť všetko)</button>
            </form>
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
@app.get("/", response_class=HTMLResponse)
def dashboard():
    template = Template(DASHBOARD_TEMPLATE)
    return template.render(
        balances=bot_state["account_balances"],
        trades=bot_state["active_trades"],
        logs=bot_state["logs"],
        current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

@app.post("/api/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        if not data:
            return JSONResponse({"status": "error", "message": "Invalid JSON"}, status_code=400)

        if not SETTINGS["bot_enabled"]:
            return {"status": "ignored", "message": "Bot is disabled"}

        # Overenie tajného hesla
        secret = data.get("secret")
        if secret != SETTINGS["webhook_secret"]:
            log_message("Neautorizovaný pokus o webhook (nesprávne heslo).")
            return JSONResponse({"status": "error", "message": "Unauthorized"}, status_code=401)

        action = data.get("action")
        ticker = data.get("ticker", "SOL-USDC")

        if action == "buy":
            # Ochrana proti triešteniu (max parallel trades)
            if len(bot_state["active_trades"]) >= SETTINGS["max_parallel_trades"]:
                return {"status": "ignored", "message": "Max parallel trades reached"}

            if ticker in bot_state["active_trades"]:
                return {"status": "ignored", "message": "Position already open"}

            entry_price = 100.0
            bot_state["active_trades"][ticker] = {
                "entry_price": entry_price,
                "time": time.time()
            }
            log_message(f"Nákup úspešný cez webhook: {ticker}")
            send_telegram(f"🚀 *WEBHOOK NÁKUP*\nTicker: `{ticker}`")
            return {"status": "success", "action": "buy", "ticker": ticker}

        elif action == "sell":
            if ticker in bot_state["active_trades"]:
                del bot_state["active_trades"][ticker]
                log_message(f"Predaj úspešný cez webhook: {ticker}")
                send_telegram(f"🔴 *WEBHOOK PREDAJ*\nTicker: `{ticker}`")
                return {"status": "success", "action": "sell", "ticker": ticker}
            return {"status": "ignored", "message": "No active position"}

        return JSONResponse({"status": "error", "message": "Unknown action"}, status_code=400)
    except Exception as e:
        log_message(f"Chyba vo webhook spracovaní: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/kill", response_class=HTMLResponse)
def kill_switch():
    bot_state["active_trades"].clear()
    log_message("🚨 KILL SWITCH AKTIVOVANÝ!")
    send_telegram("🚨 *KILL SWITCH AKTIVOVANÝ* - Všetky pozície boli zatvorené.")
    return "<h3>Kill switch úspešný. Všetky pozície boli zatvorené.</h3><a href='/'>Späť na Dashboard</a>"

@app.get("/api/status")
def api_status():
    return {
        "status": bot_state["status"],
        "active_trades": bot_state["active_trades"],
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
