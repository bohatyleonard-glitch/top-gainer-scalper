import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import base64
import hmac
import json
import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import requests
from jinja2 import Template
import uvicorn

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
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=5)
    except Exception as err:
        print(f"❌ Chyba pri odosielaní Telegram notifikácie: {err}", flush=True)

# --- STAV BOT-A & PARAMETRE ---
ACTIVE_TRADES = {}
CLOSED_TRADES_HISTORY = []
PRICE_HISTORY = []
CURRENT_STAGE = "STAV: Čakám na TradingView Webhook signál..."
LAST_TRADE_TIME = {}
MAX_PARALLEL_TRADES = 1
trade_lock = asyncio.Lock()
IS_ORDER_PROCESSING = False

IS_BOT_ACTIVE = True
CURRENT_STRATEGY_MODE = "TradingView Webhook Režim"
CURRENT_TIMEFRAME = "15m"
ALLOWED_CURRENCIES = ["USDC", "USDT", "EUR"]

PEAK_PORTFOLIO_VALUE = 0.0
MAX_DRAWDOWN_PCT = 0.0

SETTINGS = {
    "trail_pct": 1.5,
    "use_breakeven": True,
    "be_trigger_pct": 1.2,
    "use_trailing_tp": True,
    "ttp_trigger_pct": 1.2,
    "ttp_callback_pct": 0.4,
    "use_btc_filter": True,
    "use_dynamic_sizing": True,
    "webhook_secret": "moje_tajne_heslo_123"  # 🔒 Heslo pre ochranu webhooku
}

def get_sk_time_str() -> str:
    sk_time = datetime.now(timezone.utc) + timedelta(hours=1)
    return sk_time.strftime("%Y-%m-%d %H:%M:%S")

def get_sk_date_str() -> str:
    sk_time = datetime.now(timezone.utc) + timedelta(hours=1)
    return sk_time.strftime("%Y-%m-%d")

def safe_float(val, default=0.0) -> float:
    if val is None:
        return default
    try:
        if isinstance(val, str) and val.strip() == "":
            return default
        return float(val)
    except (ValueError, TypeError):
        return default

def update_drawdown_stats(current_balance: float):
    global PEAK_PORTFOLIO_VALUE, MAX_DRAWDOWN_PCT
    if current_balance > PEAK_PORTFOLIO_VALUE:
        PEAK_PORTFOLIO_VALUE = current_balance
    elif PEAK_PORTFOLIO_VALUE > 0:
        dd = ((PEAK_PORTFOLIO_VALUE - current_balance) / PEAK_PORTFOLIO_VALUE) * 100.0
        if dd > MAX_DRAWDOWN_PCT:
            MAX_DRAWDOWN_PCT = dd

def record_closed_trade(pair, pnl_pct, pnl_usdc, reason):
    global CLOSED_TRADES_HISTORY
    CLOSED_TRADES_HISTORY.insert(0, {
        "pair": pair, "pnl_pct": pnl_pct, "pnl_usdc": pnl_usdc, "reason": reason, "time": get_sk_time_str()
    })
    if len(CLOSED_TRADES_HISTORY) > 20:
        CLOSED_TRADES_HISTORY.pop()

def okx_request(method: str, path: str, body: str = ""):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    message = timestamp + method.upper() + path + body
    mac = hmac.new(OKX_SECRET.encode('utf-8'), message.encode('utf-8'), digestmod='sha256')
    signature = base64.b64encode(mac.digest()).decode('utf-8')

    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "x-simulated-trading": "1",
        "Content-Type": "application/json"
    }

    url = "https://eea.okx.com" + path
    try:
        if method.upper() == "GET":
            response = requests.get(url, headers=headers, timeout=5)
        else:
            response = requests.post(url, headers=headers, data=body, timeout=5)
        return response.json()
    except Exception as err:
        return {"code": "-1", "msg": str(err)}

def check_btc_health() -> bool:
    if not SETTINGS["use_btc_filter"]:
        return True
    try:
        url = f"/api/v5/market/candles?instId=BTC-USDC&bar={CURRENT_TIMEFRAME}&limit=10"
        res = okx_request("GET", url)
        if res.get("code") == "0" and res.get("data"):
            data = res["data"]
            data.reverse()
            closes = [safe_float(c[4]) for c in data]
            avg_btc = sum(closes) / len(closes)
            return closes[-1] >= avg_btc
    except Exception:
        pass
    return True

def calculate_volatility_position_size(free_capital: float, current_price: float, inst_id: str) -> float:
    base_budget = min(free_capital * 0.95, 500.0)
    if not SETTINGS["use_dynamic_sizing"]:
        return base_budget
    
    try:
        url = f"/api/v5/market/candles?instId={inst_id}&bar={CURRENT_TIMEFRAME}&limit=15"
        res = okx_request("GET", url)
        if res.get("code") == "0" and res.get("data"):
            candles = res["data"]
            highs = [safe_float(c[2]) for c in candles]
            lows = [safe_float(c[3]) for c in candles]
            ranges = [h - l for h, l in zip(highs, lows)]
            avg_range = sum(ranges) / len(ranges)
            volatility_pct = (avg_range / current_price) * 100.0
            
            if volatility_pct > 3.0:
                return base_budget * 0.75
            elif volatility_pct < 1.0:
                return base_budget
    except Exception:
        pass
    return base_budget

# --- 🚀 FASTAPI WEBHOOK PRE TRADINGVIEW ---
app = FastAPI()

@app.post("/api/webhook")
async def tradingview_webhook(request: Request):
    global ACTIVE_TRADES, CURRENT_STAGE, IS_ORDER_PROCESSING
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Neplatný JSON formát")

    if data.get("secret") != SETTINGS.get("webhook_secret", ""):
        raise HTTPException(status_code=403, detail="Neautorizovaný prístup")

    if not IS_BOT_ACTIVE:
        return JSONResponse({"status": "ignored", "message": "Bot je vypnutý cez CyberHUD."})

    action = data.get("action", "").lower()
    ticker = data.get("ticker", "").upper() # Napr. "HYPE-USDC"

    if action == "buy" and ticker:
        if len(ACTIVE_TRADES) >= MAX_PARALLEL_TRADES or IS_ORDER_PROCESSING:
            return JSONResponse({"status": "busy", "message": "Bot už má otvorenú pozíciu."})

        if not check_btc_health():
            return JSONResponse({"status": "blocked", "message": "BTC Trend Guard zablokoval nákup."})

        async with trade_lock:
            IS_ORDER_PROCESSING = True
            ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={ticker}")
            if ticker_res.get("code") != "0" or not ticker_res.get("data"):
                IS_ORDER_PROCESSING = False
                return JSONResponse({"status": "error", "message": "Chyba ceny z OKX."})

            current_price = safe_float(ticker_res["data"][0]["last"])

            balance_res = okx_request("GET", "/api/v5/account/balance")
            free_capital = 1000.0
            if balance_res.get("code") == "0":
                for acc in balance_res.get("data", []):
                    for detail in acc.get("details", []):
                        if detail.get("ccy") == "USDC":
                            free_capital = max(free_capital, safe_float(detail.get("availBal", 0)))

            safe_budget = calculate_volatility_position_size(free_capital, current_price, ticker)
            coin_amount = round(safe_budget / current_price, 4)

            order_body = json.dumps({
                "instId": ticker, "tdMode": "cash", "side": "buy",
                "ordType": "limit", "px": str(round(current_price, 4)), "sz": str(coin_amount), "tgtCcy": "base_ccy"
            })

            order_res = okx_request("POST", "/api/v5/trade/order", order_body)
            if order_res.get("code") != "0":
                IS_ORDER_PROCESSING = False
                return JSONResponse({"status": "error", "message": order_res.get("msg")})

            trail_pct_val = float(SETTINGS["trail_pct"]) / 100.0
            sl_price = current_price * (1 - trail_pct_val)
            coin_base = ticker.split("-")[0]

            ACTIVE_TRADES[coin_base] = {
                "market_pair": ticker,
                "entry_price": current_price,
                "current_price": current_price,
                "highest_price": current_price,
                "current_sl_price": sl_price,
                "break_even_activated": False,
                "trailing_tp_active": False,
                "amount": coin_amount,
                "bought_at": get_sk_time_str(),
                "invested_usdc": safe_budget,
                "live_pnl_pct": 0.0,
                "live_pnl_usdc": 0.0,
            }

            IS_ORDER_PROCESSING = False
            CURRENT_STAGE = f"STAV: 🔴 V OBCHODE [Webhook: {ticker}]"
            
            # 📱 TELEGRAM NOTIFIKÁCIA PRE NÁKUP
            send_telegram_msg(
                f"🟢 <b>[TRADINGVIEW WEBHOOK - NÁKUP]</b>\n"
                f"<b>Pár:</b> {ticker}\n"
                f"<b>Cena:</b> {current_price:.4f}\n"
                f"<b>Alokácia:</b> {safe_budget:.2f} USDC\n"
                f"<b>Stop-Loss:</b> {sl_price:.4f} (-{SETTINGS['trail_pct']}%)"
            )

        return JSONResponse({"status": "success", "message": f"Nakúpené {ticker}!"})

    return JSONResponse({"status": "ignored", "message": "Neznáma akcia."})

async def advanced_risk_monitor_loop():
    global ACTIVE_TRADES, CURRENT_STAGE
    await asyncio.sleep(3)
    while True:
        if not ACTIVE_TRADES:
            await asyncio.sleep(2)
            continue
        try:
            for coin in list(ACTIVE_TRADES.keys()):
                if coin not in ACTIVE_TRADES:
                    continue
                trade = ACTIVE_TRADES[coin]
                symbol = trade["market_pair"]

                ticker_res = okx_request("GET", f"/api/v5/market/ticker?instId={symbol}")
                if ticker_res.get("code") == "0" and ticker_res.get("data"):
                    current_price = safe_float(ticker_res["data"][0]["last"])
                    if current_price <= 0:
                        continue

                    pnl_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
                    pnl_usdc = trade.get("invested_usdc", 100.0) * (pnl_pct / 100.0)

                    trade["current_price"] = current_price
                    trade["live_pnl_pct"] = pnl_pct
                    trade["live_pnl_usdc"] = pnl_usdc

                    if current_price > trade["highest_price"]:
                        trade["highest_price"] = current_price
                        trail_dist = float(SETTINGS["trail_pct"]) / 100.0
                        new_sl = trade["highest_price"] * (1 - trail_dist)
                        if new_sl > trade["current_sl_price"]:
                            trade["current_sl_price"] = new_sl

                    if current_price <= trade["current_sl_price"]:
                        try:
                            okx_request("POST", "/api/v5/trade/order", json.dumps({
                                "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                            }))
                            record_closed_trade(symbol, pnl_pct, pnl_usdc, "Stop-Loss")
                            
                            # 📱 TELEGRAM NOTIFIKÁCIA PRE PREDAJ (STOP-LOSS)
                            send_telegram_msg(
                                f"🚨 <b>[STOP-LOSS / PREDAJ]</b>\n"
                                f"<b>Pár:</b> {symbol}\n"
                                f"<b>Výsledok:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)"
                            )
                        except Exception as err:
                            print(f"Chyba SL: {err}", flush=True)
                        del ACTIVE_TRADES[coin]
                        CURRENT_STAGE = "STAV: Mimo obchodu. Čakám na Webhook..."
        except Exception as e:
            print(f"Chyba monitoringu: {e}", flush=True)
        await asyncio.sleep(2)

@asynccontextmanager
async def lifespan(app: FastAPI):
    m_task = asyncio.create_task(advanced_risk_monitor_loop())
    yield
    m_task.cancel()

app.router.lifespan_context = lifespan

@app.get("/api/bot/start")
async def bot_start():
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = True
    send_telegram_msg("🟢 <b>[CYBERHUD]</b> Prijímanie signálov bolo <b>ZAPNUTÉ</b>.")
    return JSONResponse({"status": "success", "message": "Bot bol zapnutý."})

@app.get("/api/bot/stop")
async def bot_stop():
    global IS_BOT_ACTIVE
    IS_BOT_ACTIVE = False
    send_telegram_msg("🔴 <b>[CYBERHUD]</b> Prijímanie signálov bolo <b>VYPNUTÉ</b>.")
    return JSONResponse({"status": "success", "message": "Bot bol pozastavený."})

@app.post("/api/emergency-sell")
async def emergency_sell():
    global ACTIVE_TRADES, CURRENT_STAGE
    async with trade_lock:
        if not ACTIVE_TRADES:
            return JSONResponse({"status": "warning", "message": "Žiadny aktívny obchod na predaj."})
        sold = []
        for coin in list(ACTIVE_TRADES.keys()):
            trade = ACTIVE_TRADES[coin]
            symbol = trade["market_pair"]
            try:
                okx_request("POST", "/api/v5/trade/order", json.dumps({
                    "instId": symbol, "tdMode": "cash", "side": "sell", "ordType": "market", "sz": str(trade["amount"])
                }))
                pnl_pct = trade.get("live_pnl_pct", 0.0)
                pnl_usdc = trade.get("live_pnl_usdc", 0.0)
                record_closed_trade(symbol, pnl_pct, pnl_usdc, "Kill Switch")
                sold.append(symbol)
                
                # 📱 TELEGRAM NOTIFIKÁCIA PRE KILL SWITCH
                send_telegram_msg(
                    f"🚨 <b>[KILL SWITCH - NÚDZOVÝ PREDAJ]</b>\n"
                    f"<b>Pár:</b> {symbol}\n"
                    f"<b>Výsledok:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)"
                )
            except Exception as e:
                print(f"Chyba kill switch: {e}", flush=True)
            del ACTIVE_TRADES[coin]
        CURRENT_STAGE = "STAV: Pozícia zatvorená cez Kill Switch."
        return JSONResponse({"status": "success", "message": "Pozícia úspešne predaná!", "sold": sold})

HTML_TEMPLATE_CONTENT = """<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Apex Scalper v5.4.5 PRO - Webhook CyberHUD</title>
    <style>
        * { box-sizing: border-box; }
        body { 
            font-family: 'Inter', sans-serif; margin: 0; padding: 25px;
            background: radial-gradient(circle at 50% 20%, #111827 0%, #030712 100%);
            color: #f3f4f6; min-height: 100vh;
        }
        .container { 
            max-width: 900px; margin: 0 auto; 
            background: rgba(17, 24, 39, 0.85);
            backdrop-filter: blur(20px); border: 1px solid rgba(0, 240, 255, 0.2); 
            border-radius: 20px; padding: 24px; box-shadow: 0 20px 50px rgba(0,0,0,0.5);
        }
        .header-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px; border-bottom: 1px solid rgba(255,255,255,0.06); padding-bottom: 12px; }
        .brand-title { font-size: 1.1rem; font-weight: 800; color: #00f0ff; letter-spacing: 1px; text-transform: uppercase; }
        .controls-row { display: flex; justify-content: space-between; align-items: center; background: rgba(0,255,255,0.04); border: 1px solid rgba(0,255,255,0.15); padding: 12px 16px; border-radius: 12px; margin-bottom: 20px; }
        .bot-state-btn { padding: 8px 16px; border-radius: 8px; font-weight: 700; font-size: 0.75rem; cursor: pointer; border: 1px solid; }
        .btn-start { background: rgba(0, 255, 136, 0.15); color: #00ff88; border-color: #00ff88; }
        .btn-stop { background: rgba(255, 68, 68, 0.15); color: #ff4444; border-color: #ff4444; }
        .kill-switch-btn { background: linear-gradient(135deg, #ff0055, #990033); color: #fff; border: 1px solid #ff0055; padding: 8px 16px; border-radius: 8px; font-weight: 700; font-size: 0.75rem; cursor: pointer; text-transform: uppercase; }
        .stats-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 20px; }
        .stat-card { background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.06); border-radius: 12px; padding: 14px; text-align: center; }
        .stat-label { font-size: 0.65rem; color: #94a3b8; text-transform: uppercase; font-weight: 700; }
        .stat-value { font-size: 1.1rem; font-weight: 800; margin-top: 6px; color: #fff; }
        .status-banner { background: rgba(0, 240, 255, 0.08); border: 1px solid rgba(0, 240, 255, 0.3); border-radius: 10px; padding: 12px 16px; font-size: 0.8rem; font-weight: 600; color: #e2e8f0; margin-bottom: 20px; }
        .section-block { margin-bottom: 24px; }
        .section-title { font-size: 0.8rem; font-weight: 700; color: #00f0ff; margin-bottom: 10px; text-transform: uppercase; }
        table { width: 100%; border-collapse: collapse; background: rgba(3, 7, 18, 0.6); border: 1px solid rgba(255, 255, 255, 0.06); border-radius: 12px; overflow: hidden; margin-bottom: 15px; }
        th { background: rgba(255, 255, 255, 0.03); color: #00f0ff; padding: 10px 14px; font-size: 0.7rem; text-transform: uppercase; }
        td { padding: 10px 14px; font-size: 0.75rem; border-bottom: 1px solid rgba(255, 255, 255, 0.04); color: #e2e8f0; }
        .pnl-green { color: #00ff88; font-weight: 700; }
        .pnl-red { color: #ff4444; font-weight: 700; }
        code { background: rgba(0,0,0,0.5); padding: 4px 8px; border-radius: 4px; color: #00ff88; font-size: 0.8rem; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header-bar">
            <div class="brand-title">⚡ Apex Scalper PRO - Webhook CyberHUD</div>
            <a href="/" style="color: #00f0ff; font-size: 0.75rem; text-decoration: none;">🔄 Obnoviť</a>
        </div>

        <div class="controls-row">
            <div>
                {% if is_bot_active %}
                    <button class="bot-state-btn btn-stop" onclick="toggleBot('/api/bot/stop')">🔴 Vypnúť prijem signálov</button>
                {% else %}
                    <button class="bot-state-btn btn-start" onclick="toggleBot('/api/bot/start')">🟢 Zapnúť prijem signálov</button>
                {% endif %}
            </div>
            <div>
                <button class="kill-switch-btn" onclick="triggerKillSwitch()">🚨 KILL SWITCH (Predať všetko)</button>
            </div>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Uzavreté obchody</div>
                <div class="stat-value">{{ total_trades }}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Úspešnosť (Win Rate)</div>
                <div class="stat-value" style="color: #00ff88;">{{ win_rate }}%</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Kumulatívny Zisk</div>
                <div class="stat-value {{ 'pnl-green' if total_pnl_usdc >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(total_pnl_usdc) }} $</div>
            </div>
        </div>
        
        <div class="status-banner">{{ current_stage }}</div>

        <div class="section-block">
            <div class="section-title">🔗 TradingView Webhook adresa (Vložte do alertu)</div>
            <p style="font-size: 0.75rem; color: #94a3b8;">Skopírujte túto URL do nastavení alertu vo vašom TradingView grafe:</p>
            <code>https://&lt;vasa-render-url&gt;.onrender.com/api/webhook</code>
        </div>

        <div class="section-block">
            <div class="section-title">🔴 Aktívne Obchody</div>
            {% if trades %}
                <table>
                    <tr><th>Pár</th><th>Vstup</th><th>Investícia</th><th>Aktuálna cena</th><th>PnL (%)</th><th>PnL ($)</th></tr>
                    {% for t, details in trades.items() %}
                    <tr>
                        <td><strong>{{ details.market_pair }}</strong></td>
                        <td>{{ "%.4f" | format(details.entry_price) }}</td>
                        <td>{{ "%.2f" | format(details.invested_usdc) }} USDC</td>
                        <td>{{ "%.4f" | format(details.current_price) }}</td>
                        <td class="{{ 'pnl-green' if details.live_pnl_pct >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(details.live_pnl_pct) }}%</td>
                        <td class="{{ 'pnl-green' if details.live_pnl_usdc >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(details.live_pnl_usdc) }} $</td>
                    </tr>
                    {% endfor %}
                </table>
            {% else %}
                <div style="font-size: 0.8rem; color: rgba(255, 255, 255, 0.7); background: rgba(0,0,0,0.3); padding: 12px; border-radius: 8px;">
                    ✅ Žiadny aktívny obchod. Pripravený na TradingView alert...
                </div>
            {% endif %}
        </div>

        <div class="section-block">
            <div class="section-title">📜 História Posledných Obchodov</div>
            {% if history %}
                <table>
                    <tr><th>Čas</th><th>Pár</th><th>Dôvod</th><th>Výsledok (%)</th><th>Výsledok ($)</th></tr>
                    {% for h in history %}
                    <tr>
                        <td>{{ h.time }}</td>
                        <td><strong>{{ h.pair }}</strong></td>
                        <td>{{ h.reason }}</td>
                        <td class="{{ 'pnl-green' if h.pnl_pct >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(h.pnl_pct) }}%</td>
                        <td class="{{ 'pnl-green' if h.pnl_usdc >= 0 else 'pnl-red' }}">{{ "%+.2f" | format(h.pnl_usdc) }} $</td>
                    </tr>
                    {% endfor %}
                </table>
            {% else %}
                <div style="font-size: 0.8rem; color: rgba(255, 255, 255, 0.7);">Zatiaľ žiadne uzavreté obchody.</div>
            {% endif %}
        </div>
    </div>

    <script>
        async function toggleBot(endpoint) {
            const res = await fetch(endpoint);
            const data = await res.json();
            alert(data.message || "Stav zmenený!");
            window.location.reload();
        }

        async function triggerKillSwitch() {
            if (confirm("⚠️ Naozaj chceš okamžite predať všetky aktívne pozície (Kill Switch)?")) {
                const res = await fetch('/api/emergency-sell', { method: 'POST' });
                const data = await res.json();
                alert(data.message || "Hotovo!");
                window.location.reload();
            }
        }
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    total_trades = len(CLOSED_TRADES_HISTORY)
    winning_trades = sum(1 for t in CLOSED_TRADES_HISTORY if t["pnl_pct"] > 0)
    win_rate = round((winning_trades / total_trades * 100) if total_trades > 0 else 0.0, 1)
    total_pnl_usdc = sum(t["pnl_usdc"] for t in CLOSED_TRADES_HISTORY)

    template = Template(HTML_TEMPLATE_CONTENT)
    return template.render(
        trades=ACTIVE_TRADES,
        history=CLOSED_TRADES_HISTORY,
        total_trades=total_trades,
        win_rate=win_rate,
        total_pnl_usdc=total_pnl_usdc,
        current_stage=CURRENT_STAGE,
        is_bot_active=IS_BOT_ACTIVE
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
