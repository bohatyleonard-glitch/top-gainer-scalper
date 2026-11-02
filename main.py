import os
import logging
import time
import requests
from flask import Flask, request, jsonify, render_template_string

# --- LOGOVANIE ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- INICIALIZÁCIA FLASK APLIKÁCIE ---
app = Flask(__name__)

# --- ZÁKLADNÉ NASTAVENIA ---
SETTINGS = {
    "bot_enabled": True,                 // 🟢 Hlavný vypínač bota
    "webhook_secret": "moje_tajne_heslo_123", // 🔒 Heslo pre ochranu webhooku
    "max_parallel_trades": 1,             // 🛡️ Max. súbežné obchody
    "trail_pct": 1.5,                     // Odstup Trailing Stopu v %
    "be_trigger_pct": 1.2,                // Aktivácia Break-Even v %
    "ttp_trigger_pct": 1.2,               // Aktivácia Trailing TP v %
    "ttp_callback_pct": 0.4               // Odstup Trailing TP v %
}

# Pamäť pre aktívne pozície a históriu
active_positions = {}
trade_history = []

# --- TELEGRAM NOTIFIKÁCIE ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logging.error(f"Chyba pri odosielaní Telegram správy: {e}")

# --- WEBHOOK ENDPOINT PRE TRADINGVIEW ---
@app.route('/api/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"status": "error", "message": "Invalid JSON"}), 400

        # Overenie zapnutia bota
        if not SETTINGS["bot_enabled"]:
            return jsonify({"status": "ignored", "message": "Bot is disabled"}), 200

        # Overenie tajného hesla
        secret = data.get("secret")
        if secret != SETTINGS["webhook_secret"]:
            logging.warning("Neautorizovaný pokus o webhook (nesprávne heslo).")
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

        action = data.get("action") # "buy" alebo "sell"
        ticker = data.get("ticker", "SOL-USDC")

        if action == "buy":
            # Kontrola paralelnej kapacity
            if len(active_positions) >= SETTINGS["max_parallel_trades"]:
                return jsonify({"status": "ignored", "message": "Max parallel trades reached"}), 200

            if ticker in active_positions:
                return jsonify({"status": "ignored", "message": "Position already open for this ticker"}), 200

            # Simulácia / Výkon nákupu na OKX (v reálnom kóde tu voláte OKX API)
            entry_price = 100.0 # Sem priradíte aktuálnu cenu z burzy
            active_positions[ticker] = {
                "entry_price": entry_price,
                "highest_price": entry_price,
                "breakeven_active": False,
                "trailing_tp_active": False,
                "time": time.time()
            }

            msg = f"🚀 *WEBHOOK NÁKUP ÚSPEŠNÝ*\nTicker: `{ticker}`\nCena: `{entry_price}`"
            send_telegram(msg)
            logging.info(f"Otvorená pozícia pre {ticker} cez webhook.")
            return jsonify({"status": "success", "action": "buy", "ticker": ticker}), 200

        elif action == "sell":
            if ticker in active_positions:
                del active_positions[ticker]
                msg = f"🔴 *WEBHOOK PREDAJ / UZATVORENIE*\nTicker: `{ticker}`"
                send_telegram(msg)
                logging.info(f"Zatvorená pozícia pre {ticker} cez webhook.")
                return jsonify({"status": "success", "action": "sell", "ticker": ticker}), 200
            else:
                return jsonify({"status": "ignored", "message": "No active position for this ticker"}), 200

        return jsonify({"status": "error", "message": "Unknown action"}), 400

    except Exception as e:
        logging.error(f"Chyba vo webhook spracovaní: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# --- CYBERHUD WEB INTERFACE ---
HUD_TEMPLATE = """
<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <title>Top Gainer Scalper - CyberHUD</title>
    <style>
        body { background-color: #0d1117; color: #c9d1d9; font-family: monospace; padding: 20px; }
        h1 { color: #58a6ff; }
        .card { background: #161b22; border: 1px solid #30363d; padding: 15px; margin-bottom: 15px; border-radius: 6px; }
        button { background: #238636; color: white; border: none; padding: 10px 15px; cursor: pointer; font-weight: bold; border-radius: 4px; }
        button.danger { background: #da3633; }
        button:hover { opacity: 0.9; }
    </style>
</head>
<body>
    <h1>⚡ Top Gainer Scalper [CyberHUD v5.4.5]</h1>
    <div class="card">
        <h3>Stav Bota: <span style="color: {{ 'green' if bot_enabled else 'red' }}">{{ 'ZAPNUTÝ' if bot_enabled else 'VYPNUTÝ' }}</span></h3>
        <p>Webhook URL: <code>https://top-gainer-scalper.onrender.com/api/webhook</code></p>
    </div>
    <div class="card">
        <h3>Aktívne Pozície</h3>
        <pre>{{ active_positions }}</pre>
    </div>
    <div class="card">
        <h3>Núdzové Ovládanie</h3>
        <form action="/kill" method="POST">
            <button type="submit" class="danger">🚨 KILL SWITCH (Zavrieť všetko)</button>
        </form>
    </div>
</body>
</html>
"""

@app.route('/')
def hud():
    return render_template_string(HUD_TEMPLATE, bot_enabled=SETTINGS["bot_enabled"], active_positions=active_positions)

@app.route('/kill', methods=['POST'])
def kill_switch():
    active_positions.clear()
    send_telegram("🚨 *KILL SWITCH AKTIVOVANÝ* - Všetky pozície boli núdzovo zrušené cez CyberHUD.")
    return "Kill switch executed. <a href='/'>Späť na HUD</a>"

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
