import asyncio
import base64
import hmac
import json
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Template
import requests
import uvicorn

# --- OKX KLUCE & BEZPECNOST ---

OKX_API_KEY = os.getenv("OKX_API_KEY", "54ebe9fe-1fd0-443d-8f76-0cd1a3439ddb")
OKX_SECRET = os.getenv("OKX_SECRET", "FBD5C8332D88E3ED5662470CA9C3E813")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "Novyzeland111/")
WEBHOOK_SECRET = "moje_tajne_heslo_123"

# --- TELEGRAM NOTIFIKACIE & INTERAKTIVNY BOT ---

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9158932827:AAHvZT_gQrVH6dk54BRx8_mxTFtkBud0tUI")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "9130979784")

# --- NASTAVENIA PODLA PINE SCRIPTU + FIXNY STOP-LOSS & FIXNY TP ---

ALLOWED_TIMEFRAMES = ["1m", "5m", "8m", "15m", "30m", "45m", "1h", "2h", "3h", "4h"]

EXCLUDED_SYMBOLS = {
    "ZEC-USDC",
    "ENA-USDC",
    "LIT-USDC",
    "CT-USDC",
    "XMSTR-USDC",
    "XSOXL-USDC",
    "XANT-USDC",
}

ACTIVE_TRADES = {}
DAILY_PNL_CALENDAR = []
CLOSED_TRADES_HISTORY = []
SYSTEM_LOGS = []
CURRENT_STAGE = "STAV: Mimo obchodu. Čakám na BUY signál z Pine Scriptu..."
LAST_TRADE_TIME = {}
MARKET_SCANNER_CACHE = {}
trade_lock = asyncio.Lock()

IS_SIDEWAYS_MARKET = False
MARKET_REGIME = "BULL"  # "BULL", "BEAR", "SIDEWAYS"
LAST_SIDEWAYS_ALERT_TS = 0.0
SIDEWAYS_ALERT_COOLDOWN_SEC = 3600

IS_BOT_ACTIVE = True
CURRENT_TIMEFRAME = "15M"
STORAGE_FILE = "trades_history.json"
ALLOWED_STABLES = ["USDC"]

TOP_LIQUID_SYMBOLS = [
    "SOL-USDC", "SUI-USDC", "XRP-USDC", "ADA-USDC", "AVAX-USDC",
    "LINK-USDC", "DOGE-USDC", "PEPE-USDC", "NEAR-USDC", "RUNE-USDC",
    "RENDER-USDC", "APT-USDC", "ARB-USDC", "OP-USDC", "INJ-USDC",
    "TIA-USDC", "SEI-USDC", "DOT-USDC", "ATOM-USDC", "UNI-USDC"
]

# Vstup a výstup riadi BUY / SELL z Pine Scriptu + fixný Stop-Loss + voliteľný fixný Take Profit.
SETTINGS = {
    "fixed_sl_pct": 1.5,            # Pevný Stop-Loss (-1.5 % od vstupu)
    "use_fixed_tp": False,          # Voliteľný pevný Take Profit v 2s monitore
    "fixed_tp_pct": 1.5,            # Pevný Take Profit (+1.5 % od vstupu)
    "mode_choice": "ALL_GREEN",     # "ALL_GREEN", "BREAKOUT", "FIB" (predvolené z Pine Scriptu: All Green)
    "strict_buy_filter": False,     # Prísnejší BUY filter priamo na signálnej sviečke
    "fast_len": 5,
    "slow_len": 20,
    "signal_len": 9,
    "pred_look": 3,
    "pivot_len": 2,
    "fib_choice": "0.786",
    "use_trailing": True,
    "trail_pct": 1.5,
    "use_breakeven": True,
    "be_trigger_pct": 0.35,
    "use_trailing_tp": True,
    "ttp_trigger_pct": 0.8,
    "ttp_callback_pct": 0.2,
    "use_ha_exit": True,
    "ha_exit_consecutive": 2,
    "use_btc_filter": True,
    "reentry_cooldown_sec": 120,
    "max_budget_cap": 1000.0,
    "compounding_enabled": False,
    "max_parallel": 1
}

# --- JEDNODUCHE AI UCENIE & BLOKOVANIE FALOSNYCH / ZLYCH OPAKOVANI ---

AI_STORAGE_FILE = "ai_memory.json"
AI_MEMORY = {
    "trades": [],
    "setup_stats": {},
    "feature_weights": {
        "osc_pos": 0.0,
        "pred_up": 0.0,
        "above_ema9": 0.0,
        "ha_green": 0.0,
        "not_sideways": 0.0
    }
}

SETTINGS.update({
    "use_ai_learning": True,
    "ai_block_bad_repeats": True,
    "ai_bad_streak_limit": 2,
    "ai_bad_cooldown_sec": 3600,
    "ai_min_win_prob": 0.45
})

def _ai_setup_key(symbol: str, tf: str, mode: str, regime: str) -> str:
    return f"{symbol.upper()}|{tf.upper()}|{mode.upper()}|{regime.upper()}"

def load_ai_memory():
    global AI_MEMORY
    if not os.path.exists(AI_STORAGE_FILE):
        return
    try:
        with open(AI_STORAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        AI_MEMORY["trades"] = data.get("trades", [])[-400:]
        AI_MEMORY["setup_stats"] = data.get("setup_stats", {})
        loaded_w = data.get("feature_weights", {})
        for k in AI_MEMORY["feature_weights"]:
            AI_MEMORY["feature_weights"][k] = safe_float(loaded_w.get(k), 0.0)
        log_event(f"🧠 AI pamäť načítaná ({len(AI_MEMORY['trades'])} vyhodnotených obchodov).")
    except Exception as e:
        log_event(f"⚠️ Chyba pri načítaní AI pamäte: {e}")

def save_ai_memory():
    try:
        payload = {
            "trades": AI_MEMORY.get("trades", [])[-400:],
            "setup_stats": AI_MEMORY.get("setup_stats", {}),
            "feature_weights": AI_MEMORY.get("feature_weights", {})
        }
        with open(AI_STORAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_event(f"⚠️ Chyba pri ukladaní AI pamäte: {e}")

def check_ai_signal(symbol: str, tf: str, features: dict):
    if not SETTINGS.get("use_ai_learning", True):
        return True, "AI OFF", 0.5
    key = _ai_setup_key(symbol, tf, SETTINGS.get("mode_choice", "ALL_GREEN"), MARKET_REGIME)
    now_ts = datetime.now().timestamp()
    st = AI_MEMORY.get("setup_stats", {}).get(key)

    if SETTINGS.get("ai_block_bad_repeats", True) and st:
        blocked_until = safe_float(st.get("blocked_until"), 0.0)
        if now_ts < blocked_until:
            rem_min = max(1, int((blocked_until - now_ts) / 60))
            return False, f"AI zablokovalo opakovaný zlý setup ({rem_min} min)", 0.2

    score = 0.0
    for feat_name, w in AI_MEMORY.get("feature_weights", {}).items():
        feat_val = 1.0 if features.get(feat_name) else -0.5
        score += feat_val * safe_float(w, 0.0)

    if st and (st.get("wins", 0) + st.get("losses", 0)) >= 2:
        w_cnt = st.get("wins", 0)
        l_cnt = st.get("losses", 0)
        local_wr = w_cnt / max(1, w_cnt + l_cnt)
        score += (local_wr - 0.5) * 1.4

    win_prob = 1.0 / (1.0 + math.exp(-max(-5.0, min(5.0, score))))
    if len(AI_MEMORY.get("trades", [])) >= 6:
        min_prob = safe_float(SETTINGS.get("ai_min_win_prob"), 0.45)
        if win_prob < min_prob:
            return False, f"AI vyhodnotilo nízku úspešnosť ({win_prob*100:.0f}% < {min_prob*100:.0f}%)", win_prob

    return True, f"AI OK ({win_prob*100:.0f}%)", win_prob

def record_ai_trade_outcome(trade: dict, pnl_pct: float, pnl_usdc: float, reason: str):
    if not SETTINGS.get("use_ai_learning", True):
        return
    symbol = trade.get("market_pair", "UNKNOWN")
    tf = trade.get("tf", CURRENT_TIMEFRAME)
    mode = trade.get("mode_choice", SETTINGS.get("mode_choice", "ALL_GREEN"))
    regime = trade.get("market_regime", MARKET_REGIME)
    features = trade.get("ai_features", {})
    is_win = (pnl_usdc > 0) or (pnl_pct > 0)

    key = _ai_setup_key(symbol, tf, mode, regime)
    st = AI_MEMORY["setup_stats"].setdefault(key, {
        "wins": 0,
        "losses": 0,
        "bad_streak": 0,
        "blocked_until": 0.0
    })

    delta_base = 0.08 if is_win else -0.08
    for feat_name in AI_MEMORY["feature_weights"]:
        sign = 1.0 if features.get(feat_name) else -1.0
        cur_w = safe_float(AI_MEMORY["feature_weights"].get(feat_name), 0.0)
        AI_MEMORY["feature_weights"][feat_name] = max(-2.0, min(2.0, round(cur_w + delta_base * sign, 4)))

    if is_win:
        st["wins"] += 1
        st["bad_streak"] = 0
        st["blocked_until"] = 0.0
    else:
        st["losses"] += 1
        st["bad_streak"] += 1
        limit = max(1, int(SETTINGS.get("ai_bad_streak_limit", 2)))
        if SETTINGS.get("ai_block_bad_repeats", True) and st["bad_streak"] >= limit:
            cd_sec = max(60, int(SETTINGS.get("ai_bad_cooldown_sec", 3600)))
            st["blocked_until"] = datetime.now().timestamp() + cd_sec
            log_event(f"🧠 AI ZAMEDZILO FALOŠNÝ/ZLÝ SETUP: {key} ({st['bad_streak']} straty) – pauza {cd_sec // 60} min.")

    AI_MEMORY["trades"].append({
        "key": key,
        "symbol": symbol,
        "tf": tf,
        "mode": mode,
        "regime": regime,
        "features": features,
        "win": is_win,
        "pnl_pct": round(pnl_pct, 2),
        "pnl_usdc": round(pnl_usdc, 2),
        "reason": reason,
        "time": get_sk_time_str()
    })
    AI_MEMORY["trades"] = AI_MEMORY["trades"][-400:]
    save_ai_memory()

def get_ai_summary():
    trades = AI_MEMORY.get("trades", [])
    total = len(trades)
    wins = sum(1 for t in trades if t.get("win"))
    wr = round((wins / total) * 100.0, 1) if total > 0 else 0.0
    now_ts = datetime.now().timestamp()
    blocked = [k for k, st in AI_MEMORY.get("setup_stats", {}).items() if safe_float(st.get("blocked_until"), 0.0) > now_ts]
    return {
        "enabled": bool(SETTINGS.get("use_ai_learning", True)),
        "block_bad_repeats": bool(SETTINGS.get("ai_block_bad_repeats", True)),
        "trades_count": total,
        "win_rate": wr,
        "blocked_count": len(blocked),
        "blocked_setups": blocked
    }


def log_event(message: str):
    timestamp = datetime.now(timezone.utc) + timedelta(hours=1)
    time_str = timestamp.strftime("%H:%M:%S")
    entry = f"[{time_str}] {message}"
    SYSTEM_LOGS.insert(0, entry)
    if len(SYSTEM_LOGS) > 80:
        SYSTEM_LOGS.pop()
    print(entry, flush=True)

def send_telegram_msg(message: str, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("DOPLŇ"):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        requests.post(url, json=payload, timeout=5)
    except Exception as err:
        print(f"❌ Chyba pri odosielaní Telegram notifikácie: {err}", flush=True)

def load_persistent_data():
    global CLOSED_TRADES_HISTORY, DAILY_PNL_CALENDAR
    if os.path.exists(STORAGE_FILE):
        try:
            with open(STORAGE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                CLOSED_TRADES_HISTORY = data.get("history", [])
                DAILY_PNL_CALENDAR = data.get("calendar", [])
                log_event(f"📂 Načítaných {len(CLOSED_TRADES_HISTORY)} histórie obchodov z disku.")
        except Exception as e:
            log_event(f"⚠️ Chyba pri načítaní úložiska: {e}")

def save_persistent_data():
    try:
        cutoff_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        filtered_history = [h for h in CLOSED_TRADES_HISTORY if h.get("time", "") >= cutoff_date]
        data = {"history": filtered_history, "calendar": DAILY_PNL_CALENDAR}
        with open(STORAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_event(f"⚠️ Chyba pri ukladaní údajov na disk: {e}")

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

def calculate_stats():
    total = len(CLOSED_TRADES_HISTORY)
    if total == 0:
        return {"win_rate": 0.0, "total_trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
    wins = sum(1 for h in CLOSED_TRADES_HISTORY if h.get("pnl_usdc", 0) > 0)
    losses = sum(1 for h in CLOSED_TRADES_HISTORY if h.get("pnl_usdc", 0) <= 0)
    win_rate = (wins / total) * 100.0
    total_pnl = sum(h.get("pnl_usdc", 0) for h in CLOSED_TRADES_HISTORY)
    return {"win_rate": round(win_rate, 1), "total_trades": total, "wins": wins, "losses": losses, "total_pnl": round(total_pnl, 2)}

def record_closed_trade(pair, pnl_pct, pnl_usdc, reason):
    global CLOSED_TRADES_HISTORY, DAILY_PNL_CALENDAR
    day_str = get_sk_date_str()
    day_found = False
    for item in DAILY_PNL_CALENDAR:
        if item["date"] == day_str:
            item["pnl"] += pnl_usdc
            item["trades"] += 1
            day_found = True
            break
    if not day_found:
        DAILY_PNL_CALENDAR.insert(0, {"date": day_str, "pnl": pnl_usdc, "trades": 1})
    CLOSED_TRADES_HISTORY.insert(0, {"pair": pair, "pnl_pct": round(pnl_pct, 2), "pnl_usdc": round(pnl_usdc, 2), "reason": reason, "time": get_sk_time_str()})
    save_persistent_data()

# --- HYBRIDNÝ REQUEST SYSTEM ---

def okx_public_request(path: str):
    url = "https://www.okx.com" + path
    try:
        response = requests.get(url, timeout=5)
        return response.json()
    except Exception as err:
        return {"code": "-1", "msg": str(err)}

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

def get_instrument_rules(symbol: str):
    try:
        res = okx_public_request(f"/api/v5/public/instruments?instType=SPOT&instId={symbol}")
        if res.get("code") == "0" and res.get("data") and len(res["data"]) > 0:
            inst = res["data"][0]
            lot_sz = safe_float(inst.get("lotSz"), 0.01)
            min_sz = safe_float(inst.get("minSz"), 0.01)
            return lot_sz, min_sz
    except Exception as e:
        log_event(f"⚠️ Chyba pri načítaní pravidiel pre {symbol}: {e}")
    return 0.01, 0.01

def format_amount(amount: float, lot_sz: float, min_sz: float) -> str:
    if amount < min_sz:
        return ""
    precision = 0
    if lot_sz < 1:
        precision = len(str(lot_sz).split('.')[1]) if '.' in str(lot_sz) else 0
    steps = math.floor(amount / lot_sz)
    valid_amount = round(steps * lot_sz, precision)
    if valid_amount < min_sz:
        return ""
    return f"{valid_amount:.{precision}f}"

def fetch_account_capital(stable_ccy="USDC") -> float:
    try:
        balance_res = okx_request("GET", "/api/v5/account/balance")
        if balance_res.get("code") == "0":
            for acc in balance_res.get("data", []):
                for detail in acc.get("details", []):
                    if detail.get("ccy") == stable_ccy:
                        avail = safe_float(detail.get("availBal", 0))
                        cash = safe_float(detail.get("cashBal", 0))
                        eq = safe_float(detail.get("eq", 0))
                        best_val = max(avail, cash, eq)
                        if best_val > 0:
                            return best_val
    except Exception as err:
        log_event(f"⚠️ Chyba pri načítaní zostatku: {err}")
    return 13000.0

def check_btc_health() -> bool:
    if not SETTINGS["use_btc_filter"]:
        return True
    try:
        tf_str = CURRENT_TIMEFRAME.lower()
        url = f"/api/v5/market/candles?instId=BTC-USDC&bar={tf_str}&limit=10"
        res = okx_public_request(url)
        if res.get("code") == "0" and res.get("data"):
            data = res["data"]
            data.reverse()
            closes = [safe_float(c[4]) for c in data]
            avg_btc = sum(closes) / len(closes)
            return closes[-1] >= avg_btc
    except Exception:
        pass
    return True

def get_market_regime_and_sideways(bar_tf: str) -> tuple[str, bool]:
    """
    Určí stav trhu podľa BTC-USDC na zvolenom TF:
    - "SIDEWAYS" (True): úzke rozpätie posledných 12 uzavretých sviečok + časté kríženie EMA9 / EMA20
    - "BULL" (False): nie je bočný trh a cena / EMA20 ukazujú rastový trend
    - "BEAR" (False): nie je bočný trh a cena / EMA20 ukazujú klesajúci trend
    """
    try:
        url = f"/api/v5/market/candles?instId=BTC-USDC&bar={bar_tf}&limit=25"
        res = okx_public_request(url)
        if res.get("code") != "0" or not res.get("data") or len(res["data"]) < 15:
            return ("BULL", False)

        candles = res["data"]
        candles.reverse()
        closed = candles[:-1]
        if len(closed) < 12:
            return ("BULL", False)

        closes = [safe_float(c[4]) for c in closed]
        ema9 = calc_ema_series(closes, 9)
        ema20 = calc_ema_series(closes, 20)

        window = closed[-12:]
        highs = [safe_float(c[2]) for c in window]
        lows = [safe_float(c[3]) for c in window]
        min_low = min(lows)
        max_high = max(highs)
        if min_low <= 0:
            return ("BULL", False)

        range_pct = ((max_high - min_low) / min_low) * 100.0

        tf_lower = bar_tf.lower()
        if tf_lower in ["1m", "5m", "8m"]:
            max_range_pct = 0.65
        elif tf_lower == "15m":
            max_range_pct = 0.95
        elif tf_lower == "30m":
            max_range_pct = 1.35
        else:
            max_range_pct = 1.85

        narrow_range = range_pct <= max_range_pct

        start_idx = max(1, len(closes) - 12)
        crosses = 0
        for idx in range(start_idx, len(closes)):
            prev_diff = closes[idx - 1] - ema9[idx - 1]
            curr_diff = closes[idx] - ema9[idx]
            if (prev_diff > 0 and curr_diff < 0) or (prev_diff < 0 and curr_diff > 0):
                crosses += 1

            prev_ema_diff = ema9[idx - 1] - ema20[idx - 1]
            curr_ema_diff = ema9[idx] - ema20[idx]
            if (prev_ema_diff > 0 and curr_ema_diff < 0) or (prev_ema_diff < 0 and curr_ema_diff > 0):
                crosses += 1

        choppy_crosses = crosses >= 4
        if narrow_range and choppy_crosses:
            return ("SIDEWAYS", True)

        curr_close = closes[-1]
        curr_ema20 = ema20[-1]
        if curr_close >= curr_ema20:
            return ("BULL", False)
        return ("BEAR", False)
    except Exception:
        return ("BULL", False)

def update_top_liquid_symbols():
    global TOP_LIQUID_SYMBOLS
    try:
        live_symbols = set()
        inst_res = okx_public_request("/api/v5/public/instruments?instType=SPOT")
        if inst_res.get("code") == "0" and inst_res.get("data"):
            for inst in inst_res["data"]:
                inst_id = inst.get("instId", "")
                if inst.get("state") == "live" and inst_id.endswith("-USDC") and inst_id not in EXCLUDED_SYMBOLS:
                    live_symbols.add(inst_id)

        res = okx_public_request("/api/v5/market/tickers?instType=SPOT")
        if res.get("code") == "0" and res.get("data"):
            tickers = res["data"]
            valid = []
            for t in tickers:
                inst_id = t.get("instId", "")
                if not inst_id.endswith("-USDC"):
                    continue
                if inst_id in EXCLUDED_SYMBOLS:
                    continue
                if live_symbols and inst_id not in live_symbols:
                    continue
                coin = inst_id.split("-")[0]
                if coin in ["BTC", "ETH"]:
                    continue
                vol = safe_float(t.get("volCcy24h", 0))
                if vol >= 500000.0:
                    valid.append((inst_id, vol))
            valid.sort(key=lambda x: x[1], reverse=True)
            top_20 = [item[0] for item in valid[:20]]
            if len(top_20) >= 5:
                TOP_LIQUID_SYMBOLS = top_20
            else:
                fallback_clean = [s for s in TOP_LIQUID_SYMBOLS if s not in EXCLUDED_SYMBOLS]
                merged = list(dict.fromkeys(top_20 + fallback_clean))
                if merged:
                    TOP_LIQUID_SYMBOLS = merged[:20]
    except Exception as e:
        log_event(f"⚠️ Chyba pri aktualizácii top symbolov: {e}")

# --- 1:1 PINE SCRIPT MATEMATIKA (BUY A SELL SIGNÁLY) ---

def calc_ema_series(values, length):
    """Vypočíta EMA sériu rovnako ako ta.ema v Pine Scripte."""
    if not values:
        return []
    alpha = 2.0 / (length + 1.0)
    ema = [values[0]]
    for val in values[1:]:
        ema.append(val * alpha + ema[-1] * (1.0 - alpha))
    return ema

def calc_linreg_series(values, length, offset=0):
    """Vypočíta ta.linreg(source, length, offset) pre celú sériu."""
    n = len(values)
    out = [None] * n
    if length <= 1:
        for i in range(n):
            idx = i - offset
            if 0 <= idx < n:
                out[i] = values[idx]
        return out

    x_vals = list(range(length))
    x_mean = sum(x_vals) / length
    denom = sum((x - x_mean) ** 2 for x in x_vals)

    for i in range(length - 1, n):
        window = values[i - length + 1: i + 1]
        y_mean = sum(window) / length
        num = sum((x_vals[j] - x_mean) * (window[j] - y_mean) for j in range(length))
        slope = num / denom if denom != 0 else 0.0
        intercept = y_mean - slope * x_mean
        out[i] = intercept + slope * (length - 1 - offset)
    return out

def calc_wma_series(values, length):
    """Vypočíta ta.wma(source, length) pre celú sériu (ignoruje počiatočné None)."""
    n = len(values)
    out = [None] * n
    weights = list(range(1, length + 1))
    w_sum = sum(weights)
    for i in range(length - 1, n):
        window = values[i - length + 1: i + 1]
        if any(v is None for v in window):
            continue
        out[i] = sum(window[j] * weights[j] for j in range(length)) / w_sum
    return out

def evaluate_pine_strategy(symbol: str, bar_tf: str = "15m"):
    """
    Vyhodnocuje presne signály BUY (ultimate_buy_condition) a SELL (ultimate_sell_condition)
    z priloženého Pine Scriptu na uzavretých sviečkach podľa aktuálne nastavených parametrov v SETTINGS.
    Vracia dict: {"buy": bool, "sell": bool, "sell_reason": str}
    """
    global MARKET_SCANNER_CACHE, ACTIVE_TRADES
    coin_key = symbol.split("-")[0]

    try:
        url = f"/api/v5/market/candles?instId={symbol}&bar={bar_tf}&limit=120"
        res = okx_public_request(url)
        if res.get("code") != "0" or not res.get("data") or len(res["data"]) < 30:
            MARKET_SCANNER_CACHE[coin_key] = {
                "symbol": symbol, "price": 0.0, "oscillator": 0.0,
                "momentum": "SLABÉ", "impulse": "ČAKÁ", "trade_status": "---",
                "be_status": "VYPNUTÝ", "ttp_status": "VYPNUTÝ", "ts_status": "VYPNUTÝ",
                "ai_status": "--", "exec_signal": "ČAKAJ", "signal": "⚠️ CHYBA DÁT", "is_green": False
            }
            return {"buy": False, "sell": False, "sell_reason": "", "features": {}, "ai_allowed": True, "ai_note": ""}

        candles = res["data"]
        candles.reverse()  # Od najstaršej po najnovšiu

        # Len UZATVORENÉ sviečky (candles[:-1]), presne ako potvrdené sviečky v TradingView
        closed_candles = candles[:-1]

        ha_closes, ha_opens, ha_highs, ha_lows, volumes = [], [], [], [], []
        for i, c in enumerate(closed_candles):
            o, h, l, cl, v = safe_float(c[1]), safe_float(c[2]), safe_float(c[3]), safe_float(c[4]), safe_float(c[5])
            ha_cl = (o + h + l + cl) / 4.0
            ha_op = (o + cl) / 2.0 if i == 0 else (ha_opens[-1] + ha_closes[-1]) / 2.0
            ha_hi = max(h, ha_op, ha_cl)
            ha_lo = min(l, ha_op, ha_cl)
            ha_closes.append(ha_cl)
            ha_opens.append(ha_op)
            ha_highs.append(ha_hi)
            ha_lows.append(ha_lo)
            volumes.append(v)

        n = len(ha_closes)
        fast_len = max(1, int(SETTINGS.get("fast_len", 5)))
        slow_len = max(1, int(SETTINGS.get("slow_len", 20)))
        signal_len = max(1, int(SETTINGS.get("signal_len", 9)))
        pred_look = max(1, min(10, int(SETTINGS.get("pred_look", 3))))
        pivot_len = max(1, min(10, int(SETTINGS.get("pivot_len", 2))))
        fib_choice = str(SETTINGS.get("fib_choice", "0.786"))
        mode_choice = str(SETTINGS.get("mode_choice", "ALL_GREEN")).upper()
        strict_buy_filter = bool(SETTINGS.get("strict_buy_filter", False))

        use_trailing = bool(SETTINGS.get("use_trailing", True))
        trail_pct = float(SETTINGS.get("trail_pct", 1.5))
        use_breakeven = bool(SETTINGS.get("use_breakeven", True))
        be_trigger_pct = float(SETTINGS.get("be_trigger_pct", 0.35))
        use_trailing_tp = bool(SETTINGS.get("use_trailing_tp", True))
        ttp_trigger_pct = float(SETTINGS.get("ttp_trigger_pct", 0.8))
        ttp_callback_pct = float(SETTINGS.get("ttp_callback_pct", 0.2))
        use_ha_exit = bool(SETTINGS.get("use_ha_exit", True))
        ha_exit_consecutive = max(1, min(3, int(SETTINGS.get("ha_exit_consecutive", 2))))

        vol_dirs = [volumes[i] if ha_closes[i] > ha_opens[i] else -volumes[i] for i in range(n)]
        fast_ma = calc_ema_series(vol_dirs, fast_len)
        slow_ma = calc_ema_series(vol_dirs, slow_len)
        oscillator = [fast_ma[i] - slow_ma[i] for i in range(n)]
        signal_line = calc_ema_series(oscillator, signal_len)
        close_ema_9 = calc_ema_series(ha_closes, 9)

        lr0 = calc_linreg_series(oscillator, pred_look, 0)
        lr1 = calc_linreg_series(oscillator, pred_look, 1 if pred_look > 1 else 0)
        raw_slope = [(lr0[i] - lr1[i]) if (lr0[i] is not None and lr1[i] is not None) else None for i in range(n)]
        smooth_slope = calc_wma_series(raw_slope, 3)
        predicted_next = [
            (oscillator[i] + smooth_slope[i]) if smooth_slope[i] is not None else oscillator[i]
            for i in range(n)
        ]

        last_high = None
        last_low = None

        obchod_otvoreny = False
        pine_entry_price = None
        highest_since_buy = 0.0
        breakeven_active = False
        trailing_tp_active = False
        trailing_tp_stop_level = None

        last_bar_buy = False
        last_bar_sell = False
        last_bar_sell_reason = ""
        last_cond_volume_bullish = False
        last_buy_trigger = False

        for i in range(n):
            if i >= 2 * pivot_len:
                p_idx = i - pivot_len
                left_h = ha_highs[p_idx - pivot_len: p_idx]
                right_h = ha_highs[p_idx + 1: p_idx + pivot_len + 1]
                cand_h = ha_highs[p_idx]
                if all(cand_h > x for x in left_h) and all(cand_h >= x for x in right_h):
                    last_high = cand_h

                left_l = ha_lows[p_idx - pivot_len: p_idx]
                right_l = ha_lows[p_idx + 1: p_idx + pivot_len + 1]
                cand_l = ha_lows[p_idx]
                if all(cand_l < x for x in left_l) and all(cand_l <= x for x in right_l):
                    last_low = cand_l

            fib_target = None
            if last_high is not None and last_low is not None:
                rng = last_high - last_low
                if fib_choice == "0.50":
                    fib_target = last_high - rng * 0.50
                elif fib_choice == "0.618":
                    fib_target = last_high - rng * 0.618
                else:
                    fib_target = last_high - rng * 0.786

            osc_i = oscillator[i]
            sig_i = signal_line[i]
            pred_i = predicted_next[i]
            cl_i = ha_closes[i]
            ema9_i = close_ema_9[i]

            cond_volume_bullish = (osc_i > 0) and (osc_i > sig_i) and (pred_i > osc_i)
            cond_volume_crossover = (i > 0) and (oscillator[i - 1] <= signal_line[i - 1]) and (osc_i > sig_i) and (osc_i > 0)
            fib_touch = (fib_target is not None) and (cl_i > fib_target)

            breakout_trigger = cond_volume_crossover or (cond_volume_bullish and (cl_i > ema9_i))
            fib_trigger_cond = cond_volume_bullish and fib_touch
            all_green_condition = cond_volume_bullish and (cond_volume_crossover or (cl_i > ema9_i))

            if mode_choice == "BREAKOUT":
                buy_trigger = breakout_trigger
            elif mode_choice == "FIB":
                buy_trigger = fib_trigger_cond
            else:
                buy_trigger = all_green_condition

            if strict_buy_filter and buy_trigger:
                ha_green_body = ha_closes[i] > ha_opens[i]
                above_prev_high = (i == 0) or (cl_i > ha_highs[i - 1])
                buy_trigger = buy_trigger and ha_green_body and (cl_i > ema9_i) and above_prev_high

            raw_buy = buy_trigger and (not obchod_otvoreny)
            if raw_buy:
                obchod_otvoreny = True
                pine_entry_price = cl_i
                highest_since_buy = cl_i
                breakeven_active = False
                trailing_tp_active = False
                trailing_tp_stop_level = None

            if obchod_otvoreny:
                if cl_i > highest_since_buy:
                    highest_since_buy = cl_i

                if use_breakeven and not breakeven_active:
                    if cl_i >= pine_entry_price * (1.0 + (be_trigger_pct / 100.0)):
                        breakeven_active = True

                if use_trailing_tp:
                    if cl_i >= pine_entry_price * (1.0 + (ttp_trigger_pct / 100.0)):
                        trailing_tp_active = True
                    if trailing_tp_active:
                        trailing_tp_stop_level = highest_since_buy * (1.0 - (ttp_callback_pct / 100.0))

            trail_stop_level = highest_since_buy * (1.0 - (trail_pct / 100.0))
            effective_stop = max(trail_stop_level, pine_entry_price) if (breakeven_active and pine_entry_price is not None) else trail_stop_level

            ha_is_red = ha_closes[i] < ha_opens[i]
            ha_exit_triggered = False
            if use_ha_exit:
                if ha_exit_consecutive == 1:
                    ha_exit_triggered = ha_is_red
                elif ha_exit_consecutive == 2:
                    ha_exit_triggered = (i >= 1) and ha_is_red and (ha_closes[i - 1] < ha_opens[i - 1])
                else:
                    ha_exit_triggered = (i >= 2) and ha_is_red and (ha_closes[i - 1] < ha_opens[i - 1]) and (ha_closes[i - 2] < ha_opens[i - 2])

            cond_trail_triggered = use_trailing and obchod_otvoreny and (cl_i < effective_stop)
            cond_ttp_triggered = use_trailing_tp and trailing_tp_active and (trailing_tp_stop_level is not None) and (cl_i <= trailing_tp_stop_level)

            raw_sell = obchod_otvoreny and (cond_trail_triggered or cond_ttp_triggered or ha_exit_triggered)
            sell_reason = ""
            if raw_sell:
                if cond_ttp_triggered:
                    sell_reason = "Pine SELL (Trailing TP)"
                elif ha_exit_triggered:
                    sell_reason = f"Pine SELL (HA Exit {ha_exit_consecutive}x)"
                else:
                    sell_reason = "Pine SELL (Trailing Stop)"

                obchod_otvoreny = False
                pine_entry_price = None
                highest_since_buy = 0.0
                breakeven_active = False
                trailing_tp_active = False
                trailing_tp_stop_level = None

            if i == n - 1:
                last_bar_buy = raw_buy
                last_bar_sell = raw_sell
                last_bar_sell_reason = sell_reason
                last_cond_volume_bullish = cond_volume_bullish
                last_buy_trigger = buy_trigger

        curr_close = ha_closes[-1]
        curr_osc = oscillator[-1]
        is_in_trade = coin_key in ACTIVE_TRADES

        features = {
            "osc_pos": bool(curr_osc > 0),
            "pred_up": bool(predicted_next[-1] > curr_osc),
            "above_ema9": bool(curr_close > close_ema_9[-1]),
            "ha_green": bool(ha_closes[-1] > ha_opens[-1]),
            "not_sideways": bool(not IS_SIDEWAYS_MARKET)
        }
        ai_allowed, ai_note, ai_prob = check_ai_signal(symbol, bar_tf, features)

        momentum_str = "SILNÉ" if last_cond_volume_bullish else "SLABÉ"
        impuls_active = last_buy_trigger or is_in_trade
        impulse_str = "AKTÍVNY (V TRHU)" if impuls_active else "ČAKÁ"

        trade_status_txt = f"{ACTIVE_TRADES[coin_key]['entry_price']:.4f}" if is_in_trade else "---"
        be_status_txt = f"{ACTIVE_TRADES[coin_key]['entry_price']:.4f}" if (is_in_trade and use_breakeven and breakeven_active) else ("ČAKÁ" if (is_in_trade and use_breakeven) else "VYPNUTÝ")
        ttp_status_txt = f"{trailing_tp_stop_level:.4f}" if (is_in_trade and use_trailing_tp and trailing_tp_active and trailing_tp_stop_level) else ("V TRHU" if (is_in_trade and use_trailing_tp) else "VYPNUTÝ")
        ts_status_txt = f"{ACTIVE_TRADES[coin_key]['fixed_sl_price']:.4f}" if is_in_trade else f"-{SETTINGS['fixed_sl_pct']}%"

        if last_bar_buy and not is_in_trade:
            exec_signal_str = "NAKÚPIŤ (BUY)" if ai_allowed else "AI STOP"
        elif last_bar_sell and is_in_trade:
            exec_signal_str = "PREDAŤ (SELL)"
        elif is_in_trade:
            exec_signal_str = "DRŽAŤ"
        else:
            exec_signal_str = "ČAKAJ" if ai_allowed else "AI STOP"

        MARKET_SCANNER_CACHE[coin_key] = {
            "symbol": symbol,
            "price": curr_close,
            "oscillator": round(curr_osc, 2),
            "momentum": momentum_str,
            "impulse": impulse_str,
            "trade_status": trade_status_txt,
            "be_status": be_status_txt,
            "ttp_status": ttp_status_txt,
            "ts_status": ts_status_txt,
            "ai_status": f"{int(ai_prob*100)}% {'OK' if ai_allowed else 'STOP'}",
            "exec_signal": exec_signal_str,
            "signal": "🟢 BUY" if last_bar_buy else ("🔴 SELL" if last_bar_sell else ("🟡 BULLISH" if last_cond_volume_bullish else "⚪ ČAKÁ")),
            "is_green": last_bar_buy and ai_allowed
        }

        return {
            "buy": last_bar_buy,
            "sell": last_bar_sell,
            "sell_reason": last_bar_sell_reason,
            "features": features,
            "ai_allowed": ai_allowed,
            "ai_note": ai_note
        }
    except Exception:
        MARKET_SCANNER_CACHE[coin_key] = {
            "symbol": symbol, "price": 0.0, "oscillator": 0.0,
            "momentum": "SLABÉ", "impulse": "ČAKÁ", "trade_status": "---",
            "be_status": "VYPNUTÝ", "ttp_status": "VYPNUTÝ", "ts_status": "VYPNUTÝ",
            "ai_status": "--", "exec_signal": "ČAKAJ", "signal": "⚠️ VÝNIMKA", "is_green": False
        }
        return {"buy": False, "sell": False, "sell_reason": "", "features": {}, "ai_allowed": True, "ai_note": ""}

# --- AUTONOMNA SKENOVACIA SLUCKA (BUY A SELL PODLA PINE SCRIPTU) ---

async def autonomous_scanner_loop():
    global IS_SIDEWAYS_MARKET, MARKET_REGIME, LAST_SIDEWAYS_ALERT_TS
    await asyncio.sleep(3)
    log_event(f"🤖 Autonómny Pine Script skener (BUY & SELL Sync) spustený pre {len(TOP_LIQUID_SYMBOLS)} symbolov!")
    while True:
        try:
            tf_str = CURRENT_TIMEFRAME.lower()
            max_allowed = SETTINGS.get("max_parallel", 1)

            regime_now, sideways_now = get_market_regime_and_sideways(tf_str)
            MARKET_REGIME = regime_now
            IS_SIDEWAYS_MARKET = sideways_now
            now_ts = datetime.now().timestamp()
            if sideways_now and (now_ts - LAST_SIDEWAYS_ALERT_TS >= SIDEWAYS_ALERT_COOLDOWN_SEC):
                LAST_SIDEWAYS_ALERT_TS = now_ts
                log_event(f"🔻 POZOR: BOČNÝ TRH zistený na TF {CURRENT_TIMEFRAME} (úzke rozpätie + časté kríženie EMA).")
                send_telegram_msg(
                    f"🔻 <b>POZOR: BOČNÝ TRH</b>\n"
                    f"<b>TF:</b> {CURRENT_TIMEFRAME}\n"
                    f"Trh sa pohybuje v úzkom pásme s častými obratmi – zvýšené riziko falošných signálov."
                )

            for coin_name in list(ACTIVE_TRADES.keys()):
                if coin_name not in ACTIVE_TRADES:
                    continue
                trade = ACTIVE_TRADES[coin_name]
                symbol = trade["market_pair"]
                sig = evaluate_pine_strategy(symbol, bar_tf=tf_str)
                if sig["sell"]:
                    log_event(f"🔴 PINE SCRIPT 'SELL' SIGNÁL PRE {symbol} ({sig['sell_reason']})!")
                    await close_trade_position(coin_name, sig["sell_reason"])
                await asyncio.sleep(0.25)

            if IS_BOT_ACTIVE and len(ACTIVE_TRADES) < max_allowed:
                if check_btc_health():
                    for symbol in list(TOP_LIQUID_SYMBOLS):
                        if not IS_BOT_ACTIVE or len(ACTIVE_TRADES) >= SETTINGS.get("max_parallel", 1):
                            break

                        if symbol in EXCLUDED_SYMBOLS:
                            continue

                        coin_name = symbol.split("-")[0]
                        now_ts = datetime.now().timestamp()

                        if coin_name in ACTIVE_TRADES:
                            continue

                        if coin_name in LAST_TRADE_TIME and (now_ts - LAST_TRADE_TIME[coin_name] < SETTINGS["reentry_cooldown_sec"]):
                            evaluate_pine_strategy(symbol, bar_tf=tf_str)
                            continue

                        sig = evaluate_pine_strategy(symbol, bar_tf=tf_str)
                        if sig["buy"]:
                            if not sig.get("ai_allowed", True):
                                log_event(f"🧠 AI zamedzilo BUY {symbol}: {sig.get('ai_note', 'zlý setup')}")
                                continue
                            log_event(f"🚀 PINE SCRIPT 'BUY' SIGNÁL PRE {symbol} ({sig.get('ai_note', '')})!")
                            await execute_autonomous_buy(symbol, ai_features=sig.get("features"))

                        await asyncio.sleep(0.3)
        except Exception as e:
            log_event(f"⚠️ Chyba v autonómnom skeneri: {e}")
        await asyncio.sleep(10)

async def top_symbols_updater_loop():
    while True:
        try:
            update_top_liquid_symbols()
        except Exception:
            pass
        await asyncio.sleep(3600)

async def execute_autonomous_buy(okx_symbol: str, ai_features: dict = None):
    global ACTIVE_TRADES, LAST_TRADE_TIME
    async with trade_lock:
        coin_name = okx_symbol.split("-")[0]
        now_ts = datetime.now().timestamp()
        max_allowed = SETTINGS.get("max_parallel", 1)

        if coin_name in ACTIVE_TRADES or len(ACTIVE_TRADES) >= max_allowed:
            return

        LAST_TRADE_TIME[coin_name] = now_ts
        stable_ccy = "USDC"

        free_capital = fetch_account_capital(stable_ccy)
        budget_cap = SETTINGS.get("max_budget_cap", 1000.0)
        allocated_capital_base = free_capital / max_allowed

        if SETTINGS.get("compounding_enabled", False):
            safe_budget = min(allocated_capital_base * 0.98, allocated_capital_base)
        else:
            safe_budget = min(allocated_capital_base * 0.98, budget_cap)

        if safe_budget < 5.0:
            log_event(f"❌ Nedostatok USDC prostriedkov: {free_capital}")
            return

        ticker_res = okx_public_request(f"/api/v5/market/ticker?instId={okx_symbol}")
        if ticker_res.get("code") != "0" or not ticker_res.get("data"):
            log_event(f"❌ Nepodarilo sa získať cenu pre {okx_symbol}")
            return

        current_price = safe_float(ticker_res["data"][0]["last"])
        raw_amount = safe_budget / current_price

        lot_sz, min_sz = get_instrument_rules(okx_symbol)
        formatted_sz = format_amount(raw_amount, lot_sz, min_sz)

        if not formatted_sz:
            log_event(f"❌ Množstvo pre {okx_symbol} nedosahuje minimum burzy minSz ({min_sz})")
            return

        order_body = json.dumps({
            "instId": okx_symbol, "tdMode": "cash", "side": "buy",
            "ordType": "market", "sz": formatted_sz
        })

        order_res = okx_request("POST", "/api/v5/trade/order", order_body)
        if order_res.get("code") != "0":
            err_msg = order_res.get('msg', 'Neznáma chyba OKX')
            log_event(f"⚠️ Pár {okx_symbol} odmietnutý v Demo režime ({err_msg}).")
            return

        coin_amount = float(formatted_sz)
        actual_spent_usdc = coin_amount * current_price
        fixed_sl_pct = SETTINGS.get("fixed_sl_pct", 1.5)
        fixed_sl_price = current_price * (1.0 - (fixed_sl_pct / 100.0))
        use_fixed_tp = bool(SETTINGS.get("use_fixed_tp", False))
        fixed_tp_pct = float(SETTINGS.get("fixed_tp_pct", 1.5))
        fixed_tp_price = current_price * (1.0 + (fixed_tp_pct / 100.0))

        ACTIVE_TRADES[coin_name] = {
            "entry_price": current_price,
            "current_price": current_price,
            "fixed_sl_price": fixed_sl_price,
            "fixed_sl_pct": fixed_sl_pct,
            "use_fixed_tp": use_fixed_tp,
            "fixed_tp_price": fixed_tp_price,
            "fixed_tp_pct": fixed_tp_pct,
            "amount": coin_amount,
            "initial_amount": coin_amount,
            "lot_sz": lot_sz,
            "min_sz": min_sz,
            "bought_at": get_sk_time_str(),
            "market_pair": okx_symbol,
            "highest_price": current_price,
            "invested_usdc": actual_spent_usdc,
            "live_pnl_pct": 0.0,
            "live_pnl_usdc": 0.0,
            "tf": CURRENT_TIMEFRAME,
            "mode_choice": SETTINGS.get("mode_choice", "ALL_GREEN"),
            "market_regime": MARKET_REGIME,
            "ai_features": ai_features or {"osc_pos": True, "pred_up": True, "above_ema9": True, "ha_green": True, "not_sideways": not IS_SIDEWAYS_MARKET}
        }

        tp_info = f" | Fixný TP: +{fixed_tp_pct}% ({fixed_tp_price:.4f})" if use_fixed_tp else ""
        log_event(f"🛒 NÁKUP (Pine BUY) [{CURRENT_TIMEFRAME}]: {okx_symbol} za {current_price:.4f} USDC | Investícia: {actual_spent_usdc:.2f} USDC | Fixný SL: -{fixed_sl_pct}% ({fixed_sl_price:.4f}){tp_info}")

        inline_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "🛑 ZATVORIŤ VŠETKO", "callback_data": "sell_now"},
                    {"text": "⏸️ PAUZA BOTA", "callback_data": "toggle_bot"}
                ]
            ]
        }
        send_telegram_msg(
            f"🟢 <b>PINE BUY NÁKUP [TF: {CURRENT_TIMEFRAME}]</b>\n"
            f"<b>Pár:</b> {okx_symbol}\n"
            f"<b>Vstup:</b> {current_price:.4f}\n"
            f"<b>Investícia:</b> {actual_spent_usdc:.2f} USDC\n"
            f"🛡️ <b>Fixný Stop-Loss:</b> -{fixed_sl_pct}% ({fixed_sl_price:.4f})\n"
            + (f"🎯 <b>Fixný Take Profit:</b> +{fixed_tp_pct}% ({fixed_tp_price:.4f})\n" if use_fixed_tp else "")
            + f"📤 <b>Výstup:</b> Pine SELL signál alebo Fixný SL" + (" / Fixný TP" if use_fixed_tp else ""),
            reply_markup=inline_keyboard
        )

async def close_trade_position(coin: str, reason: str):
    """Bezpečne uzavrie pozíciu na OKX, zapíše PnL a aktualizuje LAST_TRADE_TIME."""
    global ACTIVE_TRADES, LAST_TRADE_TIME
    async with trade_lock:
        if coin not in ACTIVE_TRADES:
            return
        trade = ACTIVE_TRADES[coin]
        symbol = trade["market_pair"]
        try:
            ticker_res = okx_public_request(f"/api/v5/market/ticker?instId={symbol}")
            curr_p = safe_float(ticker_res['data'][0]['last']) if ticker_res.get('code') == '0' and ticker_res.get('data') else trade.get("current_price", trade["entry_price"])

            fmt_sz = format_amount(trade["amount"], trade.get("lot_sz", 0.01), trade.get("min_sz", 0.01))
            if not fmt_sz:
                fmt_sz = f"{trade['amount']:.4f}"

            order_body = json.dumps({
                "instId": symbol, "tdMode": "cash", "side": "sell",
                "ordType": "market", "sz": fmt_sz
            })
            okx_request("POST", "/api/v5/trade/order", order_body)

            pnl_pct = ((curr_p - trade["entry_price"]) / trade["entry_price"]) * 100.0
            invested = trade.get("invested_usdc", trade["amount"] * trade["entry_price"])
            pnl_usdc = invested * (pnl_pct / 100.0)

            record_closed_trade(symbol, pnl_pct, pnl_usdc, reason)
            record_ai_trade_outcome(trade, pnl_pct, pnl_usdc, reason)
            LAST_TRADE_TIME[coin] = datetime.now().timestamp()

            emoji = "🎯" if pnl_usdc >= 0 else "🚨"
            log_event(f"{emoji} Uzavretá pozícia ({reason}) pre {symbol} | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
            send_telegram_msg(f"{emoji} <b>UZAVRETÁ POZÍCIA ({reason})</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
        except Exception as e:
            log_event(f"⚠️ Chyba pri zatváraní {symbol}: {e}")
        finally:
            if coin in ACTIVE_TRADES:
                del ACTIVE_TRADES[coin]

# --- MONITOR RIZIK (FIXNY STOP-LOSS A VOLITELNY FIXNY TP) ---

async def fixed_stoploss_monitor_loop():
    global ACTIVE_TRADES, CURRENT_STAGE
    await asyncio.sleep(3)
    while True:
        if not ACTIVE_TRADES:
            if IS_BOT_ACTIVE:
                max_p = SETTINGS.get("max_parallel", 1)
                sl_p = SETTINGS.get("fixed_sl_pct", 1.5)
                tp_txt = f" | Fixný TP: +{SETTINGS.get('fixed_tp_pct', 1.5)}%" if SETTINGS.get("use_fixed_tp", False) else ""
                CURRENT_STAGE = f"STAV: Bot aktívny (Max {max_p}x | Fixný SL: -{sl_p}%{tp_txt}). Čakám na BUY/SELL..."
            else:
                CURRENT_STAGE = "STAV: Bot pozastavený."
            await asyncio.sleep(2)
            continue

        try:
            for coin in list(ACTIVE_TRADES.keys()):
                if coin not in ACTIVE_TRADES:
                    continue
                trade = ACTIVE_TRADES[coin]
                symbol = trade["market_pair"]

                ticker_res = okx_public_request(f"/api/v5/market/ticker?instId={symbol}")
                if ticker_res.get("code") == "0" and ticker_res.get("data"):
                    current_price = safe_float(ticker_res["data"][0]["last"])
                else:
                    current_price = trade["entry_price"]

                if current_price > 0:
                    trade["current_price"] = current_price
                    if current_price > trade["highest_price"]:
                        trade["highest_price"] = current_price

                    pnl_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100.0
                    invested = trade.get("invested_usdc", trade["amount"] * trade["entry_price"])
                    pnl_usdc = invested * (pnl_pct / 100.0)
                    trade["live_pnl_pct"] = pnl_pct
                    trade["live_pnl_usdc"] = pnl_usdc

                    fixed_sl_price = trade["fixed_sl_price"]
                    fixed_sl_pct = trade["fixed_sl_pct"]
                    use_fixed_tp = bool(trade.get("use_fixed_tp", False))
                    fixed_tp_price = trade.get("fixed_tp_price")
                    fixed_tp_pct = trade.get("fixed_tp_pct", SETTINGS.get("fixed_tp_pct", 1.5))

                    tp_stage = f" | Fixný TP: +{fixed_tp_pct}%" if use_fixed_tp else ""
                    CURRENT_STAGE = f"STAV: Pozícií: {len(ACTIVE_TRADES)} | {symbol} ({pnl_pct:+.2f}%) | Fixný SL: -{fixed_sl_pct}%{tp_stage}"

                    if current_price <= fixed_sl_price:
                        await close_trade_position(coin, f"Fixný Stop-Loss (-{fixed_sl_pct}%)")
                        continue

                    if use_fixed_tp and fixed_tp_price is not None and current_price >= fixed_tp_price:
                        await close_trade_position(coin, f"Fixný Take-Profit (+{fixed_tp_pct}%)")
                        continue
        except Exception:
            pass
        await asyncio.sleep(2)

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_persistent_data()
    load_ai_memory()
    update_top_liquid_symbols()
    log_event(f"🚀 Autonómny APEX MATRIX_SCALPER_v1_BOT (Pine BUY & SELL + Fixný SL -{SETTINGS['fixed_sl_pct']}%) spustený.")
    monitor_task = asyncio.create_task(fixed_stoploss_monitor_loop())
    scanner_task = asyncio.create_task(autonomous_scanner_loop())
    updater_task = asyncio.create_task(top_symbols_updater_loop())
    send_telegram_msg(f"🚀 APEX MATRIX_SCALPER_v1_BOT (Pine BUY & SELL + Fixný SL -{SETTINGS['fixed_sl_pct']}%) pripravený!")
    yield
    monitor_task.cancel()
    scanner_task.cancel()
    updater_task.cancel()

app = FastAPI(lifespan=lifespan)

@app.post("/api/telegram-webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        if "callback_query" in data:
            cb = data["callback_query"]
            cb_data = cb.get("data", "")
            if cb_data == "sell_now":
                await sell_now()
                send_telegram_msg("⚡ Ručný predaj aktivovaný z Telegramu!")
            elif cb_data == "toggle_bot":
                await toggle_bot()
    except Exception:
        pass
    return {"status": "ok"}

@app.post("/api/toggle-bot")
async def toggle_bot():
    global IS_BOT_ACTIVE, CURRENT_STAGE
    IS_BOT_ACTIVE = not IS_BOT_ACTIVE
    status_str = "aktívny" if IS_BOT_ACTIVE else "pozastavený"
    CURRENT_STAGE = f"STAV: Bot je {status_str}."
    log_event(f"⚙️ Prepínač bota: {status_str.upper()}")
    send_telegram_msg(f"⚙️ Zmena stavu: Bot je teraz {status_str.upper()}.")
    return JSONResponse({"status": "success", "is_bot_active": IS_BOT_ACTIVE})

@app.post("/api/toggle-btc-filter")
async def toggle_btc_filter():
    SETTINGS["use_btc_filter"] = not SETTINGS.get("use_btc_filter", True)
    status_str = "zapnutý (aktívny)" if SETTINGS["use_btc_filter"] else "vypnutý (deaktivovaný)"
    log_event(f"⚙️ BTC Trend Guard filter: {status_str.upper()}")
    send_telegram_msg(f"⚙️ Zmena stavu: BTC Trend Guard filter je teraz {status_str.upper()}.")
    return JSONResponse({"status": "success", "use_btc_filter": SETTINGS["use_btc_filter"]})

@app.post("/api/toggle-compounding")
async def toggle_compounding():
    SETTINGS["compounding_enabled"] = not SETTINGS.get("compounding_enabled", False)
    status_str = "ZAPNUTÉ (Zložené úročenie)" if SETTINGS["compounding_enabled"] else "VYPNUTÉ (Pevný rozpočet)"
    log_event(f"⚙️ Automatické reinvestovanie (COMPOUNDING): {status_str.upper()}")
    send_telegram_msg(f"⚙️ Zmena stavu: Auto-Compounding je teraz {status_str}.")
    return JSONResponse({"status": "success", "compounding_enabled": SETTINGS["compounding_enabled"]})

@app.post("/api/set-budget/{amount}")
async def set_budget(amount: int):
    if amount in [500, 1000, 3000, 5000, 10000]:
        SETTINGS["max_budget_cap"] = float(amount)
        log_event(f"💰 Maximálny rozpočet (strop) zmenený na: {amount} USDC")
        send_telegram_msg(f"💰 Zmena rozpočtu: Maximálny strop pre obchod je teraz {amount} USDC.")
        return JSONResponse({"status": "success", "max_budget_cap": amount})
    return JSONResponse({"status": "error", "message": "Neplatná hodnota rozpočtu."})

@app.post("/api/set-parallel/{count}")
async def set_parallel(count: int):
    if count in [1, 2, 3, 4]:
        SETTINGS["max_parallel"] = count
        log_event(f"⚙️ Maximálny počet paralelných obchodov zmenený na: {count}")
        send_telegram_msg(f"⚙️ Zmena nastavenia: Bot môže teraz otvoriť naraz max. {count} obchody.")
        return JSONResponse({"status": "success", "max_parallel": count})
    return JSONResponse({"status": "error", "message": "Neplatná hodnota."})

@app.post("/api/set-sl/{sl_val}")
async def set_sl(sl_val: float):
    if 0.1 <= sl_val <= 50.0:
        SETTINGS["fixed_sl_pct"] = float(sl_val)
        log_event(f"🛡️ Fixný Stop-Loss zmenený na: -{sl_val}%")
        send_telegram_msg(f"🛡️ Zmena Stop-Lossu: Fixný SL pre nové obchody je teraz -{sl_val}%.")
        return JSONResponse({"status": "success", "fixed_sl_pct": sl_val})
    return JSONResponse({"status": "error", "message": "Neplatná hodnota SL (povolene 0.1 - 50.0 %)."})

@app.post("/api/set-tf/{tf_val}")
async def set_tf(tf_val: str):
    global CURRENT_TIMEFRAME, MARKET_SCANNER_CACHE, MARKET_REGIME, IS_SIDEWAYS_MARKET
    tf_lower = tf_val.lower()
    if tf_lower in ALLOWED_TIMEFRAMES:
        CURRENT_TIMEFRAME = tf_val.upper()
        MARKET_SCANNER_CACHE.clear()
        regime_now, sideways_now = get_market_regime_and_sideways(tf_lower)
        MARKET_REGIME = regime_now
        IS_SIDEWAYS_MARKET = sideways_now
        log_event(f"⏱️ Časový rámec (TF) zmenený na: {CURRENT_TIMEFRAME} (bez zmeny profilov, riadi sa Pine BUY/SELL)")
        return JSONResponse({"status": "success", "tf": CURRENT_TIMEFRAME})
    return JSONResponse({"status": "error", "message": "Neplatný časový rámec."})

@app.post("/api/update-pine-settings")
async def update_pine_settings(request: Request):
    """Uloží a uplatní všetky nastavenia z Pine Scriptu priamo na stránke Apex."""
    global MARKET_SCANNER_CACHE
    try:
        payload = await request.json()
        mode_val = str(payload.get("mode_choice", SETTINGS["mode_choice"])).upper()
        if mode_val not in ["ALL_GREEN", "BREAKOUT", "FIB"]:
            mode_val = "ALL_GREEN"

        fib_val = str(payload.get("fib_choice", SETTINGS["fib_choice"]))
        if fib_val not in ["0.50", "0.618", "0.786"]:
            fib_val = "0.786"

        SETTINGS["mode_choice"] = mode_val
        SETTINGS["strict_buy_filter"] = bool(payload.get("strict_buy_filter", SETTINGS["strict_buy_filter"]))
        SETTINGS["fast_len"] = max(1, min(200, int(payload.get("fast_len", SETTINGS["fast_len"]))))
        SETTINGS["slow_len"] = max(1, min(300, int(payload.get("slow_len", SETTINGS["slow_len"]))))
        SETTINGS["signal_len"] = max(1, min(200, int(payload.get("signal_len", SETTINGS["signal_len"]))))
        SETTINGS["pred_look"] = max(1, min(10, int(payload.get("pred_look", SETTINGS["pred_look"]))))

        SETTINGS["pivot_len"] = max(1, min(10, int(payload.get("pivot_len", SETTINGS["pivot_len"]))))
        SETTINGS["fib_choice"] = fib_val

        if "fixed_sl_pct" in payload and payload["fixed_sl_pct"] is not None:
            SETTINGS["fixed_sl_pct"] = max(0.1, min(50.0, float(payload.get("fixed_sl_pct", SETTINGS["fixed_sl_pct"]))))

        SETTINGS["use_fixed_tp"] = bool(payload.get("use_fixed_tp", SETTINGS["use_fixed_tp"]))
        SETTINGS["fixed_tp_pct"] = max(0.1, min(100.0, float(payload.get("fixed_tp_pct", SETTINGS["fixed_tp_pct"]))))

        SETTINGS["use_trailing"] = bool(payload.get("use_trailing", SETTINGS["use_trailing"]))
        SETTINGS["trail_pct"] = max(0.1, min(50.0, float(payload.get("trail_pct", SETTINGS["trail_pct"]))))
        SETTINGS["use_breakeven"] = bool(payload.get("use_breakeven", SETTINGS["use_breakeven"]))
        SETTINGS["be_trigger_pct"] = max(0.05, min(50.0, float(payload.get("be_trigger_pct", SETTINGS["be_trigger_pct"]))))

        SETTINGS["use_trailing_tp"] = bool(payload.get("use_trailing_tp", SETTINGS["use_trailing_tp"]))
        SETTINGS["ttp_trigger_pct"] = max(0.1, min(100.0, float(payload.get("ttp_trigger_pct", SETTINGS["ttp_trigger_pct"]))))
        SETTINGS["ttp_callback_pct"] = max(0.05, min(50.0, float(payload.get("ttp_callback_pct", SETTINGS["ttp_callback_pct"]))))

        SETTINGS["use_ha_exit"] = bool(payload.get("use_ha_exit", SETTINGS["use_ha_exit"]))
        SETTINGS["ha_exit_consecutive"] = max(1, min(3, int(payload.get("ha_exit_consecutive", SETTINGS["ha_exit_consecutive"]))))

        SETTINGS["use_ai_learning"] = bool(payload.get("use_ai_learning", SETTINGS["use_ai_learning"]))
        SETTINGS["ai_block_bad_repeats"] = bool(payload.get("ai_block_bad_repeats", SETTINGS["ai_block_bad_repeats"]))
        SETTINGS["ai_bad_streak_limit"] = max(1, min(10, int(payload.get("ai_bad_streak_limit", SETTINGS["ai_bad_streak_limit"]))))
        SETTINGS["ai_bad_cooldown_sec"] = max(60, min(86400, int(payload.get("ai_bad_cooldown_sec", SETTINGS["ai_bad_cooldown_sec"]))))

        MARKET_SCANNER_CACHE.clear()
        log_event(
            f"🎛️ Pine nastavenia aktualizované: Režim={mode_val}, PrísnyBUY={SETTINGS['strict_buy_filter']}, Osc({SETTINGS['fast_len']}/{SETTINGS['slow_len']}/{SETTINGS['signal_len']}, pred={SETTINGS['pred_look']}), "
            f"Fib={fib_val}(piv={SETTINGS['pivot_len']}), FixSL=-{SETTINGS['fixed_sl_pct']}%, FixTP={SETTINGS['use_fixed_tp']}({SETTINGS['fixed_tp_pct']}%), "
            f"TS={SETTINGS['use_trailing']}({SETTINGS['trail_pct']}%), BE={SETTINGS['use_breakeven']}({SETTINGS['be_trigger_pct']}%), "
            f"TTP={SETTINGS['use_trailing_tp']}({SETTINGS['ttp_trigger_pct']}%/{SETTINGS['ttp_callback_pct']}%), HA_Exit={SETTINGS['use_ha_exit']}({SETTINGS['ha_exit_consecutive']}x)"
        )
        return JSONResponse({"status": "success", "pine_settings": get_pine_settings_dict()})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)})

def get_pine_settings_dict():
    return {
        "mode_choice": SETTINGS["mode_choice"],
        "strict_buy_filter": SETTINGS["strict_buy_filter"],
        "fast_len": SETTINGS["fast_len"],
        "slow_len": SETTINGS["slow_len"],
        "signal_len": SETTINGS["signal_len"],
        "pred_look": SETTINGS["pred_look"],
        "pivot_len": SETTINGS["pivot_len"],
        "fib_choice": SETTINGS["fib_choice"],
        "fixed_sl_pct": SETTINGS["fixed_sl_pct"],
        "use_fixed_tp": SETTINGS["use_fixed_tp"],
        "fixed_tp_pct": SETTINGS["fixed_tp_pct"],
        "use_trailing": SETTINGS["use_trailing"],
        "trail_pct": SETTINGS["trail_pct"],
        "use_breakeven": SETTINGS["use_breakeven"],
        "be_trigger_pct": SETTINGS["be_trigger_pct"],
        "use_trailing_tp": SETTINGS["use_trailing_tp"],
        "ttp_trigger_pct": SETTINGS["ttp_trigger_pct"],
        "ttp_callback_pct": SETTINGS["ttp_callback_pct"],
        "use_ha_exit": SETTINGS["use_ha_exit"],
        "ha_exit_consecutive": SETTINGS["ha_exit_consecutive"],
        "use_ai_learning": SETTINGS["use_ai_learning"],
        "ai_block_bad_repeats": SETTINGS["ai_block_bad_repeats"],
        "ai_bad_streak_limit": SETTINGS["ai_bad_streak_limit"],
        "ai_bad_cooldown_sec": SETTINGS["ai_bad_cooldown_sec"]
    }

@app.post("/api/manual-buy/{coin}")
async def manual_buy(coin: str):
    global ACTIVE_TRADES, LAST_TRADE_TIME
    async with trade_lock:
        try:
            if not IS_BOT_ACTIVE:
                return JSONResponse({"status": "error", "message": "Bot je pozastavený."})

            coin_upper = coin.upper().strip()
            max_allowed = SETTINGS.get("max_parallel", 1)
            if len(ACTIVE_TRADES) >= max_allowed or coin_upper in ACTIVE_TRADES:
                return JSONResponse({"status": "error", "message": "Dosiahnutý limit paralelných pozícií alebo pozícia už existuje."})

            okx_symbol = f"{coin_upper}-USDC"
            if okx_symbol in EXCLUDED_SYMBOLS:
                return JSONResponse({"status": "error", "message": f"Pár {okx_symbol} je na zozname vylúčených párov."})

            stable_ccy = "USDC"
            free_capital = fetch_account_capital(stable_ccy)
            budget_cap = SETTINGS.get("max_budget_cap", 1000.0)
            allocated_capital_base = free_capital / max_allowed

            if SETTINGS.get("compounding_enabled", False):
                safe_budget = min(allocated_capital_base * 0.98, allocated_capital_base)
            else:
                safe_budget = min(allocated_capital_base * 0.98, budget_cap)

            if safe_budget < 5.0:
                return JSONResponse({"status": "error", "message": "Nedostatok prostriedkov."})

            ticker_res = okx_public_request(f"/api/v5/market/ticker?instId={okx_symbol}")
            if ticker_res.get("code") != "0" or not ticker_res.get("data"):
                return JSONResponse({"status": "error", "message": f"Nepodarilo sa získať cenu pre {okx_symbol}"})

            current_price = safe_float(ticker_res["data"][0]["last"])
            raw_amount = safe_budget / current_price

            lot_sz, min_sz = get_instrument_rules(okx_symbol)
            formatted_sz = format_amount(raw_amount, lot_sz, min_sz)
            if not formatted_sz:
                return JSONResponse({"status": "error", "message": f"Množstvo pod minimom minSz ({min_sz})"})

            order_body = json.dumps({
                "instId": okx_symbol, "tdMode": "cash", "side": "buy",
                "ordType": "market", "sz": formatted_sz
            })

            order_res = okx_request("POST", "/api/v5/trade/order", order_body)
            if order_res.get("code") != "0":
                err_msg = order_res.get('msg', 'Neznáma chyba OKX')
                return JSONResponse({"status": "error", "message": f"OKX odmietlo príkaz ({err_msg})"})

            coin_amount = float(formatted_sz)
            actual_spent_usdc = coin_amount * current_price
            fixed_sl_pct = SETTINGS.get("fixed_sl_pct", 1.5)
            fixed_sl_price = current_price * (1.0 - (fixed_sl_pct / 100.0))
            use_fixed_tp = bool(SETTINGS.get("use_fixed_tp", False))
            fixed_tp_pct = float(SETTINGS.get("fixed_tp_pct", 1.5))
            fixed_tp_price = current_price * (1.0 + (fixed_tp_pct / 100.0))

            ACTIVE_TRADES[coin_upper] = {
                "entry_price": current_price,
                "current_price": current_price,
                "fixed_sl_price": fixed_sl_price,
                "fixed_sl_pct": fixed_sl_pct,
                "use_fixed_tp": use_fixed_tp,
                "fixed_tp_price": fixed_tp_price,
                "fixed_tp_pct": fixed_tp_pct,
                "amount": coin_amount,
                "initial_amount": coin_amount,
                "lot_sz": lot_sz,
                "min_sz": min_sz,
                "bought_at": get_sk_time_str(),
                "market_pair": okx_symbol,
                "highest_price": current_price,
                "invested_usdc": actual_spent_usdc,
                "live_pnl_pct": 0.0,
                "live_pnl_usdc": 0.0,
                "tf": CURRENT_TIMEFRAME
            }
            tp_info = f" | Fixný TP: +{fixed_tp_pct}%" if use_fixed_tp else ""
            log_event(f"🛒 Manuálny nákup [{CURRENT_TIMEFRAME}]: {okx_symbol} za {current_price:.4f} | Investícia: {actual_spent_usdc:.2f}$ | Fixný SL: -{fixed_sl_pct}%{tp_info}")
            send_telegram_msg(f"🟢 <b>MANUÁLNY NÁKUP [TF: {CURRENT_TIMEFRAME}]</b>\n<b>Pár:</b> {okx_symbol}\n<b>Vstup:</b> {current_price:.4f}\n<b>Investícia:</b> {actual_spent_usdc:.2f} USDC\n🛡️ <b>Fixný SL:</b> -{fixed_sl_pct}%" + (f"\n🎯 <b>Fixný TP:</b> +{fixed_tp_pct}%" if use_fixed_tp else ""))

            return JSONResponse({"status": "success", "symbol": okx_symbol})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

@app.post("/api/sell-now")
async def sell_now():
    if not ACTIVE_TRADES:
        return JSONResponse({"status": "error", "message": "Žiadna aktívna pozícia na predaj."})
    sold = []
    for coin in list(ACTIVE_TRADES.keys()):
        symbol = ACTIVE_TRADES[coin]["market_pair"]
        await close_trade_position(coin, "Ručný okamžitý predaj (Emergency)")
        sold.append(symbol)
    return JSONResponse({"status": "success", "sold": sold})

# --- HTML ŠABLÓNA ---

HTML_TEMPLATE_CONTENT = """<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>APEX MATRIX_SCALPER_v1_BOT</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #08090c;
            --card-bg: #111318;
            --border-color: #1f232b;
            --border-hover: #2e3440;
            --text-main: #eceff4;
            --text-muted: #6c788e;
            --accent-white: #ffffff;
            --green: #10b981;
            --red: #ef4444;
            --yellow: #f59e0b;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'JetBrains Mono', monospace; }
        body { background-color: var(--bg-color); color: var(--text-main); padding: 16px; min-height: 100vh; overflow-y: auto; display: flex; flex-direction: column; }
        .container { max-width: 1350px; width: 100%; margin: 0 auto; display: flex; flex-direction: column; gap: 14px; padding-bottom: 30px; }
        .top-bar { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); padding-bottom: 12px; flex-wrap: wrap; gap: 12px; }
        .brand h1 { font-size: 1.05rem; font-weight: 700; color: var(--accent-white); letter-spacing: -0.5px; }
        .brand p { font-size: 0.7rem; color: var(--text-muted); margin-top: 3px; text-transform: uppercase; letter-spacing: 0.8px; display: flex; gap: 12px; flex-wrap: wrap; }
        .balance-tag { color: var(--yellow); font-weight: 700; }
        .actions { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
        .btn { background: transparent; border: 1px solid var(--border-hover); color: var(--text-main); padding: 5px 10px; font-size: 0.7rem; font-weight: 600; cursor: pointer; border-radius: 4px; transition: all 0.15s ease; text-transform: uppercase; letter-spacing: 0.5px; }
        .btn:hover { border-color: var(--text-muted); background: rgba(255,255,255,0.03); }
        .btn-on { border-color: var(--green); color: var(--green); }
        .btn-off { border-color: var(--text-muted); color: var(--text-muted); }
        .btn-yellow { border-color: var(--yellow) !important; color: var(--yellow) !important; background: rgba(245, 158, 11, 0.12) !important; font-weight: 700 !important; }
        .btn-sell { background: var(--accent-white); color: var(--bg-color); font-weight: 700; border-color: var(--accent-white); }
        .btn-sell:hover { background: #e4e4e7; }
        .btn-sell:disabled { opacity: 0.25; cursor: not-allowed; background: transparent; color: var(--text-muted); border-color: var(--border-color); }
        .status-ticker { font-size: 0.75rem; color: var(--yellow); background: var(--card-bg); border: 1px solid var(--border-color); padding: 9px 14px; border-radius: 5px; display: flex; align-items: center; justify-content: space-between; gap: 10px; font-weight: 600; flex-wrap: wrap; }
        .status-left { display: flex; align-items: center; gap: 10px; }
        .regime-badge { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 4px; font-size: 0.7rem; font-weight: 700; letter-spacing: 0.4px; border: 1px solid var(--border-hover); }
        .regime-bull { color: var(--green); background: rgba(16, 185, 129, 0.12); border-color: rgba(16, 185, 129, 0.45); }
        .regime-bear { color: var(--red); background: rgba(239, 68, 68, 0.12); border-color: rgba(239, 68, 68, 0.45); }
        .regime-sideways { color: var(--red); background: rgba(239, 68, 68, 0.18); border-color: rgba(239, 68, 68, 0.65); }
        .status-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--green); box-shadow: 0 0 10px var(--green); }
        .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
        .stat-card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 5px; padding: 12px 14px; }
        .stat-title { font-size: 0.65rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.6px; }
        .stat-value { font-size: 1.15rem; font-weight: 700; margin-top: 4px; color: var(--accent-white); }
        .section-title { font-size: 0.72rem; text-transform: uppercase; color: var(--text-muted); letter-spacing: 1px; margin-top: 8px; margin-bottom: 5px; font-weight: 600; display: flex; justify-content: space-between; align-items: center; }
        .terminal-card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 5px; overflow: hidden; }
        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.72rem; }
        th { color: var(--text-muted); font-weight: 500; padding: 9px 10px; border-bottom: 1px solid var(--border-color); font-size: 0.62rem; text-transform: uppercase; background: var(--card-bg); position: sticky; top: 0; z-index: 2; }
        td { padding: 9px 10px; border-bottom: 1px solid var(--border-color); color: var(--text-main); }
        tr:last-child td { border-bottom: none; }
        .scrollable-card { max-height: 220px; overflow-y: auto; }
        .logs-box { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 5px; padding: 10px 12px; height: 200px; max-height: 200px; overflow-y: auto; font-size: 0.7rem; color: var(--text-muted); display: flex; flex-direction: column; gap: 6px; }
        .log-line { white-space: pre-wrap; word-break: break-all; line-height: 1.4; border-bottom: 1px dashed rgba(255,255,255,0.03); padding-bottom: 4px; }
        .text-green { color: var(--green); font-weight: 700; }
        .text-red { color: var(--red); font-weight: 700; }
        .text-yellow { color: var(--yellow); font-weight: 700; }
        .custom-buy-box { display: flex; align-items: center; gap: 4px; border: 1px solid var(--border-hover); padding: 2px 6px; border-radius: 4px; background: rgba(255,255,255,0.01); }
        .custom-buy-input { background: transparent; border: none; color: var(--text-main); font-size: 0.7rem; width: 55px; outline: none; text-transform: uppercase; font-weight: 600; }

        /* Panel nastavení Pine Scriptu */
        .pine-panel { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 5px; padding: 12px 14px; display: flex; flex-direction: column; gap: 12px; }
        .pine-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
        @media (max-width: 1100px) { .pine-grid { grid-template-columns: repeat(2, 1fr); } .stats-grid { grid-template-columns: repeat(2, 1fr); } }
        @media (max-width: 650px) { .pine-grid { grid-template-columns: 1fr; } .stats-grid { grid-template-columns: 1fr; } }
        .pine-group { background: #0c0e13; border: 1px solid var(--border-color); border-radius: 4px; padding: 10px; display: flex; flex-direction: column; gap: 8px; }
        .pine-group-title { font-size: 0.65rem; font-weight: 700; color: var(--yellow); text-transform: uppercase; letter-spacing: 0.6px; border-bottom: 1px solid var(--border-color); padding-bottom: 5px; }
        .pine-row { display: flex; justify-content: space-between; align-items: center; gap: 8px; font-size: 0.68rem; color: var(--text-main); }
        .pine-row label { color: var(--text-muted); flex: 1; }
        .pine-input, .pine-select { background: var(--card-bg); border: 1px solid var(--border-hover); color: var(--text-main); border-radius: 3px; padding: 3px 6px; font-size: 0.68rem; outline: none; }
        .pine-input { width: 72px; text-align: right; }
        .pine-select { max-width: 155px; }
        .pine-checkbox { width: 15px; height: 15px; accent-color: var(--green); cursor: pointer; }
        .pine-footer { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; border-top: 1px solid var(--border-color); padding-top: 10px; }
        .pine-save-status { font-size: 0.68rem; color: var(--green); font-weight: 600; }
    </style>
</head>
<body>
    <div class="container">
        <div class="top-bar">
            <div class="brand">
                <h1>APEX MATRIX_SCALPER_v1_BOT</h1>
                <p>
                    <span id="header-pair">Top USDC · OKX Demo</span>
                    <span>Zostatok: <span id="header-balance" class="balance-tag">-- USDC</span></span>
                    <span>Aktívny TF: <span id="header-tf" class="balance-tag">{{ CURRENT_TIMEFRAME }}</span></span>
                    <span>Fixný SL: <span id="header-sl" class="balance-tag">-{{ SETTINGS.fixed_sl_pct }}%</span></span>
                </p>
            </div>
            <div class="actions">
                <button class="tf-btn btn {% if CURRENT_TIMEFRAME|upper == '5M' %}btn-yellow{% endif %}" data-tf="5m" onclick="setTf('5m')">5M</button>
                <button class="tf-btn btn {% if CURRENT_TIMEFRAME|upper == '8M' %}btn-yellow{% endif %}" data-tf="8m" onclick="setTf('8m')">8M</button>
                <button class="tf-btn btn {% if CURRENT_TIMEFRAME|upper == '15M' %}btn-yellow{% endif %}" data-tf="15m" onclick="setTf('15m')">15M</button>
                <button class="tf-btn btn {% if CURRENT_TIMEFRAME|upper == '30M' %}btn-yellow{% endif %}" data-tf="30m" onclick="setTf('30m')">30M</button>
                <button class="tf-btn btn {% if CURRENT_TIMEFRAME|upper == '45M' %}btn-yellow{% endif %}" data-tf="45m" onclick="setTf('45m')">45M</button>

                <button class="parallel-btn btn {% if SETTINGS.max_parallel == 1 %}btn-yellow{% endif %}" data-val="1" onclick="setParallel(1)">1x</button>
                <button class="parallel-btn btn {% if SETTINGS.max_parallel == 2 %}btn-yellow{% endif %}" data-val="2" onclick="setParallel(2)">2x</button>
                <button class="parallel-btn btn {% if SETTINGS.max_parallel == 3 %}btn-yellow{% endif %}" data-val="3" onclick="setParallel(3)">3x</button>
                <button class="parallel-btn btn {% if SETTINGS.max_parallel == 4 %}btn-yellow{% endif %}" data-val="4" onclick="setParallel(4)">4x</button>

                <button class="budget-btn btn {% if SETTINGS.max_budget_cap == 500 %}btn-yellow{% endif %}" data-val="500" onclick="setBudget(500)">500</button>
                <button class="budget-btn btn {% if SETTINGS.max_budget_cap == 1000 %}btn-yellow{% endif %}" data-val="1000" onclick="setBudget(1000)">1K</button>
                <button class="budget-btn btn {% if SETTINGS.max_budget_cap == 3000 %}btn-yellow{% endif %}" data-val="3000" onclick="setBudget(3000)">3K</button>
                <button class="budget-btn btn {% if SETTINGS.max_budget_cap == 5000 %}btn-yellow{% endif %}" data-val="5000" onclick="setBudget(5000)">5K</button>
                <button class="budget-btn btn {% if SETTINGS.max_budget_cap == 10000 %}btn-yellow{% endif %}" data-val="10000" onclick="setBudget(10000)">10K</button>

                <div class="custom-buy-box">
                    <input type="text" id="custom-coin" class="custom-buy-input" placeholder="SOL">
                    <button class="btn btn-on" style="padding: 2px 5px;" onclick="manualBuyCustom()">BUY</button>
                </div>

                <button id="compounding-btn" class="btn {% if SETTINGS.compounding_enabled %}btn-yellow{% endif %}" onclick="toggleCompounding()">
                    COMPOUND: {% if SETTINGS.compounding_enabled %}ON{% else %}OFF{% endif %}
                </button>
                <button id="btc-btn" class="btn {% if SETTINGS.use_btc_filter %}btn-on{% else %}btn-off{% endif %}" onclick="toggleBtcFilter()">
                    BTC: {% if SETTINGS.use_btc_filter %}ON{% else %}OFF{% endif %}
                </button>
                <button id="bot-btn" class="btn {% if IS_BOT_ACTIVE %}btn-on{% else %}btn-off{% endif %}" onclick="toggleBot()">
                    {% if IS_BOT_ACTIVE %}● ACTIVE{% else %}○ PAUSED{% endif %}
                </button>
                <button id="sell-btn" class="btn btn-sell" onclick="sellNow()" disabled>⚡ SELL</button>
            </div>
        </div>

        <div class="status-ticker">
            <div class="status-left">
                <div class="status-dot"></div>
                <span id="current-stage">{{ CURRENT_STAGE }}</span>
            </div>
            <span id="market-regime-badge" class="regime-badge regime-bull" title="Stav trhu (BTC-USDC) na aktívnom TF">🐂 BÝČÍ TRH</span>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-title">Win Rate</div>
                <div class="stat-value" id="stat-winrate">0.0%</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">Total Trades</div>
                <div class="stat-value" id="stat-trades">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">Win / Loss</div>
                <div class="stat-value" id="stat-winloss">0W / 0L</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">Net PnL</div>
                <div class="stat-value" id="stat-pnl">+0.00 $</div>
            </div>
        </div>

        <div>
            <div class="section-title">
                <span>Nastavenia Pine Script Indikátora (BUY / SELL Sync)</span>
                <span id="pine-active-badge" class="text-yellow">REŽIM: {{ SETTINGS.mode_choice }}</span>
            </div>
            <div class="pine-panel">
                <div class="pine-grid">
                    <div class="pine-group">
                        <div class="pine-group-title">0. Režim & 1. Objemový Oscilátor</div>
                        <div class="pine-row">
                            <label for="pine-mode">Režim stratégie</label>
                            <select id="pine-mode" class="pine-select">
                                <option value="ALL_GREEN" {% if SETTINGS.mode_choice == 'ALL_GREEN' %}selected{% endif %}>All Green</option>
                                <option value="BREAKOUT" {% if SETTINGS.mode_choice == 'BREAKOUT' %}selected{% endif %}>Breakout</option>
                                <option value="FIB" {% if SETTINGS.mode_choice == 'FIB' %}selected{% endif %}>Fibonacci Touch</option>
                            </select>
                        </div>
                        <div class="pine-row">
                            <label for="pine-strict-buy">Prísnejší filter BUY signálu</label>
                            <input type="checkbox" id="pine-strict-buy" class="pine-checkbox" {% if SETTINGS.strict_buy_filter %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="pine-fast-len">Rýchla dĺžka (Vol)</label>
                            <input type="number" id="pine-fast-len" class="pine-input" min="1" max="200" step="1" value="{{ SETTINGS.fast_len }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-slow-len">Pomalá dĺžka (Vol)</label>
                            <input type="number" id="pine-slow-len" class="pine-input" min="1" max="300" step="1" value="{{ SETTINGS.slow_len }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-signal-len">Signálna čiara (MA)</label>
                            <input type="number" id="pine-signal-len" class="pine-input" min="1" max="200" step="1" value="{{ SETTINGS.signal_len }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-pred-look">Citlivosť predikcie</label>
                            <input type="number" id="pine-pred-look" class="pine-input" min="1" max="10" step="1" value="{{ SETTINGS.pred_look }}">
                        </div>
                    </div>

                    <div class="pine-group">
                        <div class="pine-group-title">2. Fibonacci Hladiny</div>
                        <div class="pine-row">
                            <label for="pine-pivot-len">Rýchlosť potvrdenia (Pivot)</label>
                            <input type="number" id="pine-pivot-len" class="pine-input" min="1" max="10" step="1" value="{{ SETTINGS.pivot_len }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-fib-choice">Nákupná Fib hladina</label>
                            <select id="pine-fib-choice" class="pine-select">
                                <option value="0.50" {% if SETTINGS.fib_choice == '0.50' %}selected{% endif %}>0.50</option>
                                <option value="0.618" {% if SETTINGS.fib_choice == '0.618' %}selected{% endif %}>0.618</option>
                                <option value="0.786" {% if SETTINGS.fib_choice == '0.786' %}selected{% endif %}>0.786</option>
                            </select>
                        </div>
                    </div>

                    <div class="pine-group">
                        <div class="pine-group-title">3. Trailing Stop, BE & Trailing TP</div>
                        <div class="pine-row">
                            <label for="pine-fixed-sl-pct">Fixný SL (%)</label>
                            <input type="number" id="pine-fixed-sl-pct" class="pine-input" min="0.1" max="50.0" step="0.1" value="{{ SETTINGS.fixed_sl_pct }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-use-fixed-tp">Zapnúť Fixný TP</label>
                            <input type="checkbox" id="pine-use-fixed-tp" class="pine-checkbox" {% if SETTINGS.use_fixed_tp %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="pine-fixed-tp-pct">Fixný TP (%)</label>
                            <input type="number" id="pine-fixed-tp-pct" class="pine-input" min="0.1" max="100.0" step="0.1" value="{{ SETTINGS.fixed_tp_pct }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-use-trailing">Zapnúť Trailing Stop</label>
                            <input type="checkbox" id="pine-use-trailing" class="pine-checkbox" {% if SETTINGS.use_trailing %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="pine-trail-pct">Vzdialenosť TS (%)</label>
                            <input type="number" id="pine-trail-pct" class="pine-input" min="0.1" max="50.0" step="0.1" value="{{ SETTINGS.trail_pct }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-use-be">Zapnúť Break-Even</label>
                            <input type="checkbox" id="pine-use-be" class="pine-checkbox" {% if SETTINGS.use_breakeven %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="pine-be-trigger">Zisk % pre Break-Even</label>
                            <input type="number" id="pine-be-trigger" class="pine-input" min="0.05" max="50.0" step="0.05" value="{{ SETTINGS.be_trigger_pct }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-use-ttp">Zapnúť Trailing TP</label>
                            <input type="checkbox" id="pine-use-ttp" class="pine-checkbox" {% if SETTINGS.use_trailing_tp %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="pine-ttp-trigger">Aktivácia TTP (%)</label>
                            <input type="number" id="pine-ttp-trigger" class="pine-input" min="0.1" max="100.0" step="0.1" value="{{ SETTINGS.ttp_trigger_pct }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-ttp-callback">Odstup z maxima TTP (%)</label>
                            <input type="number" id="pine-ttp-callback" class="pine-input" min="0.05" max="50.0" step="0.05" value="{{ SETTINGS.ttp_callback_pct }}">
                        </div>
                    </div>

                    <div class="pine-group">
                        <div class="pine-group-title">3.2. Exit pri Otočení Heikin-Ashi</div>
                        <div class="pine-row">
                            <label for="pine-use-ha-exit">Povoliť exit pri otočení HA</label>
                            <input type="checkbox" id="pine-use-ha-exit" class="pine-checkbox" {% if SETTINGS.use_ha_exit %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="pine-ha-consec">Počet červených HA sviečok</label>
                            <select id="pine-ha-consec" class="pine-select">
                                <option value="1" {% if SETTINGS.ha_exit_consecutive == 1 %}selected{% endif %}>1</option>
                                <option value="2" {% if SETTINGS.ha_exit_consecutive == 2 %}selected{% endif %}>2</option>
                                <option value="3" {% if SETTINGS.ha_exit_consecutive == 3 %}selected{% endif %}>3</option>
                            </select>
                        </div>
                        <div class="pine-group-title" style="margin-top: 4px;">4. AI Učenie & Blokovanie Falošných</div>
                        <div class="pine-row">
                            <label for="ai-use-learning">Zapnúť učenie AI</label>
                            <input type="checkbox" id="ai-use-learning" class="pine-checkbox" {% if SETTINGS.use_ai_learning %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="ai-block-repeats">Zamedziť opakovaným zlým</label>
                            <input type="checkbox" id="ai-block-repeats" class="pine-checkbox" {% if SETTINGS.ai_block_bad_repeats %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="ai-bad-streak">Po koľkých stratách stop</label>
                            <input type="number" id="ai-bad-streak" class="pine-input" min="1" max="10" step="1" value="{{ SETTINGS.ai_bad_streak_limit }}">
                        </div>
                        <div class="pine-row">
                            <label for="ai-cooldown-min">Pauza zlého setupu (min)</label>
                            <input type="number" id="ai-cooldown-min" class="pine-input" min="1" max="1440" step="1" value="{{ (SETTINGS.ai_bad_cooldown_sec // 60) }}">
                        </div>
                    </div>
                </div>
                <div class="pine-footer">
                    <span id="pine-save-status" class="pine-save-status">Pripravené – zmeny sa okamžite premietnu do BUY / SELL skenera.</span>
                    <div style="display: flex; gap: 6px;">
                        <button class="btn" onclick="resetPineDefaults()">OBNOVIŤ ZÁKLAD</button>
                        <button class="btn btn-on" onclick="savePineSettings()">ULOŽIŤ PINE NASTAVENIA</button>
                    </div>
                </div>
            </div>
        </div>

        <div>
            <div class="section-title">Active Positions Matrix (Pine SELL, Fixný TP alebo Fixný SL)</div>
            <div class="terminal-card">
                <table>
                    <thead>
                        <tr>
                            <th>Pair / TF</th>
                            <th>Entry Price</th>
                            <th>Current Price</th>
                            <th>Fixed TP Price</th>
                            <th>Fixed SL Price</th>
                            <th>Invested USDC</th>
                            <th>Live PnL</th>
                        </tr>
                    </thead>
                    <tbody id="active-position-tbody">
                        <tr><td colspan="7" style="text-align: center; color: var(--text-muted);">Skenujem trh pre Pine Script BUY signály...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div>
            <div class="section-title">Live Market Scanner Status (20 Top Liquid USDC Pairs - Pine BUY/SELL Sync)</div>
            <div class="terminal-card scrollable-card" style="max-height: 260px;">
                <table>
                    <thead>
                        <tr>
                            <th>Token / Pár</th>
                            <th>Cena</th>
                            <th>1. Objem. Momentum</th>
                            <th>2. Nákupný Impulz</th>
                            <th>3. Stav Obchodu</th>
                            <th>4. Break-Even</th>
                            <th>5. Trailing TP</th>
                            <th>6. Fixný SL</th>
                            <th>AI Stav</th>
                            <th>7. Exekučný Pokyn</th>
                        </tr>
                    </thead>
                    <tbody id="scanner-tbody">
                        <tr><td colspan="10" style="text-align: center; color: var(--text-muted);">Načítavam stav skenera...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div>
            <div class="section-title">System Execution Logs</div>
            <div class="logs-box" id="logs-container">
                <div class="log-line">[System Initialized] Pine BUY & SELL + Fixný SL pripravený.</div>
            </div>
        </div>
    </div>

    <script>
        function syncSlUi(slVal) {
            const numVal = parseFloat(slVal);
            if (isNaN(numVal)) return;
            document.getElementById('header-sl').innerText = `-${numVal}%`;
            const pineSlInput = document.getElementById('pine-fixed-sl-pct');
            if (pineSlInput) {
                pineSlInput.value = numVal;
            }
        }

        async function toggleBot() {
            let res = await fetch('/api/toggle-bot', {method: 'POST'});
            let data = await res.json();
            let btn = document.getElementById('bot-btn');
            btn.className = data.is_bot_active ? "btn btn-on" : "btn btn-off";
            btn.innerText = data.is_bot_active ? "● ACTIVE" : "○ PAUSED";
            updateState();
        }

        async function toggleBtcFilter() {
            let res = await fetch('/api/toggle-btc-filter', {method: 'POST'});
            let data = await res.json();
            let btn = document.getElementById('btc-btn');
            btn.className = data.use_btc_filter ? "btn btn-on" : "btn btn-off";
            btn.innerText = data.use_btc_filter ? "BTC: ON" : "BTC: OFF";
        }

        async function toggleCompounding() {
            let res = await fetch('/api/toggle-compounding', {method: 'POST'});
            let data = await res.json();
            let btn = document.getElementById('compounding-btn');
            if (data.compounding_enabled) {
                btn.className = "btn btn-yellow";
                btn.innerText = "COMPOUND: ON";
            } else {
                btn.className = "btn";
                btn.innerText = "COMPOUND: OFF";
            }
        }

        async function setBudget(amount) {
            let res = await fetch(`/api/set-budget/${amount}`, {method: 'POST'});
            let data = await res.json();
            if (data.status === 'success') {
                document.querySelectorAll('.budget-btn').forEach(b => {
                    b.classList.remove('btn-yellow');
                    if (parseInt(b.getAttribute('data-val')) === amount) {
                        b.classList.add('btn-yellow');
                    }
                });
            }
        }

        async function setParallel(count) {
            let res = await fetch(`/api/set-parallel/${count}`, {method: 'POST'});
            let data = await res.json();
            if (data.status === 'success') {
                document.querySelectorAll('.parallel-btn').forEach(b => {
                    b.classList.remove('btn-yellow');
                    if (parseInt(b.getAttribute('data-val')) === count) {
                        b.classList.add('btn-yellow');
                    }
                });
            }
        }

        async function setTf(tfVal) {
            let res = await fetch(`/api/set-tf/${tfVal}`, {method: 'POST'});
            let data = await res.json();
            if (data.status === 'success') {
                document.querySelectorAll('.tf-btn').forEach(b => {
                    b.classList.remove('btn-yellow');
                    if (b.getAttribute('data-tf').toLowerCase() === tfVal.toLowerCase()) {
                        b.classList.add('btn-yellow');
                    }
                });
                document.getElementById('header-tf').innerText = data.tf;
                updateState();
            }
        }

        function applyPineFormValues(ps) {
            if (!ps) return;
            document.getElementById('pine-mode').value = ps.mode_choice;
            document.getElementById('pine-strict-buy').checked = !!ps.strict_buy_filter;
            document.getElementById('pine-fast-len').value = ps.fast_len;
            document.getElementById('pine-slow-len').value = ps.slow_len;
            document.getElementById('pine-signal-len').value = ps.signal_len;
            document.getElementById('pine-pred-look').value = ps.pred_look;
            document.getElementById('pine-pivot-len').value = ps.pivot_len;
            document.getElementById('pine-fib-choice').value = ps.fib_choice;
            if (ps.fixed_sl_pct !== undefined) {
                document.getElementById('pine-fixed-sl-pct').value = ps.fixed_sl_pct;
            }
            document.getElementById('pine-use-fixed-tp').checked = !!ps.use_fixed_tp;
            document.getElementById('pine-fixed-tp-pct').value = ps.fixed_tp_pct;
            document.getElementById('pine-use-trailing').checked = !!ps.use_trailing;
            document.getElementById('pine-trail-pct').value = ps.trail_pct;
            document.getElementById('pine-use-be').checked = !!ps.use_breakeven;
            document.getElementById('pine-be-trigger').value = ps.be_trigger_pct;
            document.getElementById('pine-use-ttp').checked = !!ps.use_trailing_tp;
            document.getElementById('pine-ttp-trigger').value = ps.ttp_trigger_pct;
            document.getElementById('pine-ttp-callback').value = ps.ttp_callback_pct;
            document.getElementById('pine-use-ha-exit').checked = !!ps.use_ha_exit;
            document.getElementById('pine-ha-consec').value = String(ps.ha_exit_consecutive);
            if (ps.use_ai_learning !== undefined) document.getElementById('ai-use-learning').checked = !!ps.use_ai_learning;
            if (ps.ai_block_bad_repeats !== undefined) document.getElementById('ai-block-repeats').checked = !!ps.ai_block_bad_repeats;
            if (ps.ai_bad_streak_limit !== undefined) document.getElementById('ai-bad-streak').value = ps.ai_bad_streak_limit;
            if (ps.ai_bad_cooldown_sec !== undefined) document.getElementById('ai-cooldown-min').value = Math.max(1, Math.round(ps.ai_bad_cooldown_sec / 60));
            document.getElementById('pine-active-badge').innerText = `REŽIM: ${ps.mode_choice}`;
        }

        async function savePineSettings() {
            const payload = {
                mode_choice: document.getElementById('pine-mode').value,
                strict_buy_filter: document.getElementById('pine-strict-buy').checked,
                fast_len: parseInt(document.getElementById('pine-fast-len').value, 10),
                slow_len: parseInt(document.getElementById('pine-slow-len').value, 10),
                signal_len: parseInt(document.getElementById('pine-signal-len').value, 10),
                pred_look: parseInt(document.getElementById('pine-pred-look').value, 10),
                pivot_len: parseInt(document.getElementById('pine-pivot-len').value, 10),
                fib_choice: document.getElementById('pine-fib-choice').value,
                fixed_sl_pct: parseFloat(document.getElementById('pine-fixed-sl-pct').value),
                use_fixed_tp: document.getElementById('pine-use-fixed-tp').checked,
                fixed_tp_pct: parseFloat(document.getElementById('pine-fixed-tp-pct').value),
                use_trailing: document.getElementById('pine-use-trailing').checked,
                trail_pct: parseFloat(document.getElementById('pine-trail-pct').value),
                use_breakeven: document.getElementById('pine-use-be').checked,
                be_trigger_pct: parseFloat(document.getElementById('pine-be-trigger').value),
                use_trailing_tp: document.getElementById('pine-use-ttp').checked,
                ttp_trigger_pct: parseFloat(document.getElementById('pine-ttp-trigger').value),
                ttp_callback_pct: parseFloat(document.getElementById('pine-ttp-callback').value),
                use_ha_exit: document.getElementById('pine-use-ha-exit').checked,
                ha_exit_consecutive: parseInt(document.getElementById('pine-ha-consec').value, 10),
                use_ai_learning: document.getElementById('ai-use-learning').checked,
                ai_block_bad_repeats: document.getElementById('ai-block-repeats').checked,
                ai_bad_streak_limit: parseInt(document.getElementById('ai-bad-streak').value, 10),
                ai_bad_cooldown_sec: (parseInt(document.getElementById('ai-cooldown-min').value, 10) || 60) * 60
            };

            let res = await fetch('/api/update-pine-settings', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            let data = await res.json();
            let statusEl = document.getElementById('pine-save-status');
            if (data.status === 'success') {
                applyPineFormValues(data.pine_settings);
                if (data.pine_settings && data.pine_settings.fixed_sl_pct !== undefined) {
                    syncSlUi(data.pine_settings.fixed_sl_pct);
                }
                statusEl.style.color = 'var(--green)';
                statusEl.innerText = 'Uložené! Skener teraz počíta BUY a SELL podľa nových nastavení.';
                updateState();
            } else {
                statusEl.style.color = 'var(--red)';
                statusEl.innerText = 'Chyba: ' + data.message;
            }
        }

        async function resetPineDefaults() {
            const defaults = {
                mode_choice: "ALL_GREEN",
                strict_buy_filter: false,
                fast_len: 5,
                slow_len: 20,
                signal_len: 9,
                pred_look: 3,
                pivot_len: 2,
                fib_choice: "0.786",
                fixed_sl_pct: 1.5,
                use_fixed_tp: false,
                fixed_tp_pct: 1.5,
                use_trailing: true,
                trail_pct: 1.5,
                use_breakeven: true,
                be_trigger_pct: 0.35,
                use_trailing_tp: true,
                ttp_trigger_pct: 0.8,
                ttp_callback_pct: 0.2,
                use_ha_exit: true,
                ha_exit_consecutive: 2
            };
            applyPineFormValues(defaults);
            await savePineSettings();
        }

        async function manualBuyCustom() {
            let coinInput = document.getElementById('custom-coin');
            let coin = coinInput.value.trim().toUpperCase();
            if (!coin) { alert("Zadajte skratku tokenu (napr. SOL, SUI)!"); return; }
            if (!confirm(`Naozaj vykonať manuálny nákup ${coin}-USDC?`)) return;

            let res = await fetch(`/api/manual-buy/${coin}`, {method: 'POST'});
            let data = await res.json();
            if (data.status === 'success') {
                coinInput.value = "";
                updateState();
            } else {
                alert("Chyba: " + data.message);
            }
        }

        async function sellNow() {
            if (!confirm("Naozaj núdzovo zatvoriť všetky pozície?")) return;
            await fetch('/api/sell-now', {method: 'POST'});
            updateState();
        }

        async function updateState() {
            try {
                let res = await fetch('/api/state');
                let data = await res.json();

                document.getElementById('header-balance').innerText = data.balance.toFixed(2) + " USDC";
                document.getElementById('current-stage').innerText = data.current_stage;
                document.getElementById('header-tf').innerText = data.current_tf;
                document.getElementById('header-sl').innerText = `-${data.fixed_sl_pct}%`;
                if (data.pine_settings) {
                    document.getElementById('pine-active-badge').innerText = `REŽIM: ${data.pine_settings.mode_choice}`;
                }

                let regimeBadge = document.getElementById('market-regime-badge');
                if (regimeBadge) {
                    const regime = data.market_regime || (data.is_sideways_market ? "SIDEWAYS" : "BULL");
                    regimeBadge.classList.remove('regime-bull', 'regime-bear', 'regime-sideways');
                    if (regime === "SIDEWAYS") {
                        regimeBadge.classList.add('regime-sideways');
                        regimeBadge.innerText = `🔻 BOČNÝ TRH (${data.current_tf})`;
                    } else if (regime === "BEAR") {
                        regimeBadge.classList.add('regime-bear');
                        regimeBadge.innerText = `🐻 MEDVEDÍ TRH (${data.current_tf})`;
                    } else {
                        regimeBadge.classList.add('regime-bull');
                        regimeBadge.innerText = `🐂 BÝČÍ TRH (${data.current_tf})`;
                    }
                }

                let stats = data.stats;
                document.getElementById('stat-winrate').innerText = stats.win_rate.toFixed(1) + "%";
                document.getElementById('stat-trades').innerText = stats.total_trades;
                document.getElementById('stat-winloss').innerText = stats.wins + "W / " + stats.losses + "L";
                let pnlEl = document.getElementById('stat-pnl');
                pnlEl.innerText = (stats.total_pnl >= 0 ? "+" : "") + stats.total_pnl.toFixed(2) + " $";
                pnlEl.className = "stat-value " + (stats.total_pnl >= 0 ? "text-green" : "text-red");

                let activeTbody = document.getElementById('active-position-tbody');
                let sellBtn = document.getElementById('sell-btn');
                if (Object.keys(data.active_trades).length > 0) {
                    let html = "";
                    for (let [coin, t] of Object.entries(data.active_trades)) {
                        let pnlClass = t.live_pnl_pct >= 0 ? "text-green" : "text-red";
                        let tpCell = (t.use_fixed_tp && t.fixed_tp_price)
                            ? `<span class="text-green">${t.fixed_tp_price.toFixed(4)} (+${t.fixed_tp_pct}%)</span>`
                            : `<span style="color:var(--text-muted);">VYPNUTÝ</span>`;
                        html += `<tr>
                            <td><b>${t.market_pair}</b><br><span style="font-size:0.6rem; color:var(--text-muted);">TF: ${t.tf}</span></td>
                            <td>${t.entry_price.toFixed(4)}</td>
                            <td>${t.current_price.toFixed(4)}</td>
                            <td>${tpCell}</td>
                            <td class="text-red">${t.fixed_sl_price.toFixed(4)} (-${t.fixed_sl_pct}%)</td>
                            <td>${t.invested_usdc.toFixed(2)} USDC</td>
                            <td class="${pnlClass}">${t.live_pnl_pct >= 0 ? "+" : ""}${t.live_pnl_pct.toFixed(2)}% (${t.live_pnl_usdc >= 0 ? "+" : ""}${t.live_pnl_usdc.toFixed(2)} $)</td>
                        </tr>`;
                    }
                    activeTbody.innerHTML = html;
                    sellBtn.disabled = false;
                } else {
                    activeTbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">Skenujem trh pre Pine Script BUY signály...</td></tr>`;
                    sellBtn.disabled = true;
                }

                let scannerTbody = document.getElementById('scanner-tbody');
                let scannerData = data.scanner_cache || {};
                if (Object.keys(scannerData).length > 0) {
                    let sHtml = "";
                    for (let [coin, info] of Object.entries(scannerData)) {
                        let momClass = info.momentum === "SILNÉ" ? "text-green" : "text-red";
                        let impClass = info.impulse.includes("AKTÍVNY") ? "text-green" : "text-yellow";
                        let aiClass = (info.ai_status || "").includes("STOP") ? "text-red" : "text-green";
                        let execClass = info.exec_signal.includes("NAKÚPIŤ") ? "text-green" : ((info.exec_signal.includes("PREDAŤ") || info.exec_signal.includes("STOP")) ? "text-red" : "text-yellow");

                        sHtml += `<tr>
                            <td><b>${info.symbol}</b></td>
                            <td>${info.price ? info.price.toFixed(4) : '--'}</td>
                            <td class="${momClass}">${info.momentum}</td>
                            <td class="${impClass}">${info.impulse}</td>
                            <td class="text-yellow">${info.trade_status}</td>
                            <td>${info.be_status}</td>
                            <td>${info.ttp_status}</td>
                            <td>${info.ts_status}</td>
                            <td class="${aiClass}">${info.ai_status || '--'}</td>
                            <td class="${execClass}">${info.exec_signal}</td>
                        </tr>`;
                    }
                    scannerTbody.innerHTML = sHtml;
                } else {
                    scannerTbody.innerHTML = `<tr><td colspan="10" style="text-align: center; color: var(--text-muted);">Prebieha úvodné skenovanie trhu...</td></tr>`;
                }

                let logsBox = document.getElementById('logs-container');
                let lHtml = "";
                for (let log of data.logs) {
                    lHtml += `<div class="log-line">${log}</div>`;
                }
                logsBox.innerHTML = lHtml;

            } catch (err) {
                console.error("Chyba pri obnove stavu:", err);
            }
        }

        setInterval(updateState, 2000);
        updateState();
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    balance = fetch_account_capital("USDC")
    t = Template(HTML_TEMPLATE_CONTENT)
    return t.render(
        SETTINGS=SETTINGS,
        IS_BOT_ACTIVE=IS_BOT_ACTIVE,
        CURRENT_TIMEFRAME=CURRENT_TIMEFRAME,
        CURRENT_STAGE=CURRENT_STAGE,
        balance=balance
    )

@app.get("/api/state")
async def get_state():
    balance = fetch_account_capital("USDC")
    stats = calculate_stats()
    return JSONResponse({
        "balance": balance,
        "is_bot_active": IS_BOT_ACTIVE,
        "current_stage": CURRENT_STAGE,
        "current_tf": CURRENT_TIMEFRAME,
        "is_sideways_market": IS_SIDEWAYS_MARKET,
        "market_regime": MARKET_REGIME,
        "fixed_sl_pct": SETTINGS.get("fixed_sl_pct", 1.5),
        "pine_settings": get_pine_settings_dict(),
        "ai": get_ai_summary(),
        "stats": stats,
        "active_trades": ACTIVE_TRADES,
        "scanner_cache": MARKET_SCANNER_CACHE,
        "history": CLOSED_TRADES_HISTORY[:20],
        "logs": SYSTEM_LOGS
    })

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
