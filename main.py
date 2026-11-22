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

# --- NASTAVENIA PODLA PINE SCRIPTU + FIXNY STOP-LOSS + JEDNODUCHE AI ---

ALLOWED_TIMEFRAMES = ["1m", "5m", "8m", "15m", "30m", "45m", "1h", "2h", "3h", "4h"]

ACTIVE_TRADES = {}
DAILY_PNL_CALENDAR = []
CLOSED_TRADES_HISTORY = []
SYSTEM_LOGS = []
CURRENT_STAGE = "STAV: Mimo obchodu. Cakam na BUY signal z Pine Scriptu..."
LAST_TRADE_TIME = {}
MARKET_SCANNER_CACHE = {}
trade_lock = asyncio.Lock()

IS_BOT_ACTIVE = True
CURRENT_TIMEFRAME = "15M"
STORAGE_FILE = "trades_history.json"
AI_STORAGE_FILE = "ai_memory.json"
ALLOWED_STABLES = ["USDC"]

TOP_LIQUID_SYMBOLS = [
    "SOL-USDC", "SUI-USDC", "XRP-USDC", "ADA-USDC", "AVAX-USDC",
    "LINK-USDC", "DOGE-USDC", "PEPE-USDC", "NEAR-USDC", "RUNE-USDC",
    "RENDER-USDC", "APT-USDC", "ARB-USDC", "OP-USDC", "INJ-USDC",
    "TIA-USDC", "SEI-USDC", "DOT-USDC", "ATOM-USDC", "UNI-USDC"
]

SETTINGS = {
    "fixed_sl_pct": 1.5,
    "mode_choice": "ALL_GREEN",
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
    "max_parallel": 1,
    # Jednoduche AI ucenie a blokovanie zlych opakovani
    "use_ai_learning": True,
    "ai_block_bad_repeats": True,
    "ai_bad_streak_limit": 2,
    "ai_bad_cooldown_sec": 3600,
    "ai_min_win_prob": 0.45
}

AI_MEMORY = {
    "samples": [],          # zoznam nazbieranych obchodov: {key, symbol, tf, mode, osc_pos, pred_up, above_ema9, win, pnl_pct, time}
    "pair_tf_stats": {},    # kluc "SYMBOL|TF|MODE" -> {wins, losses, bad_streak, blocked_until}
    "feature_scores": {     # jednoduche skore priznakov (-1.0 az +1.0)
        "osc_pos": 0.0,
        "pred_up": 0.0,
        "above_ema9": 0.0
    }
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
        print(f"Chyba pri odosielani Telegram notifikacie: {err}", flush=True)


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


# --- JEDNODUCHE AI UCENIE & BLOKOVANIE ZLYCH OPAKOVANI ---

def make_setup_key(symbol: str, tf: str, mode: str) -> str:
    return f"{symbol.upper()}|{tf.upper()}|{mode.upper()}"


def save_ai_memory():
    try:
        payload = {
            "samples": AI_MEMORY.get("samples", [])[-300:],
            "pair_tf_stats": AI_MEMORY.get("pair_tf_stats", {}),
            "feature_scores": AI_MEMORY.get("feature_scores", {"osc_pos": 0.0, "pred_up": 0.0, "above_ema9": 0.0})
        }
        with open(AI_STORAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_event(f"Chyba pri ukladani AI pamate na disk: {e}")


def load_ai_memory():
    global AI_MEMORY
    if not os.path.exists(AI_STORAGE_FILE):
        return
    try:
        with open(AI_STORAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        AI_MEMORY["samples"] = data.get("samples", [])
        AI_MEMORY["pair_tf_stats"] = data.get("pair_tf_stats", {})
        loaded_scores = data.get("feature_scores", {})
        for k in ["osc_pos", "pred_up", "above_ema9"]:
            AI_MEMORY["feature_scores"][k] = safe_float(loaded_scores.get(k), 0.0)
        log_event(f"AI pamat nacitana z disku ({len(AI_MEMORY['samples'])} obchodov).")
    except Exception as e:
        log_event(f"Chyba pri nacitani AI pamate: {e}")


def check_ai_allows_buy(symbol: str, tf: str, features: dict):
    """
    Vracia (allowed: bool, reason: str, win_prob: float).
    Ak sa rovnaky setup (par + TF + rezim) opakovane nevyplati, AI ho docasne zastavi.
    """
    if not SETTINGS.get("use_ai_learning", True):
        return True, "AI vypnute", 0.5

    key = make_setup_key(symbol, tf, SETTINGS.get("mode_choice", "ALL_GREEN"))
    now_ts = datetime.now().timestamp()
    stat = AI_MEMORY["pair_tf_stats"].get(key)

    if SETTINGS.get("ai_block_bad_repeats", True) and stat:
        blocked_until = safe_float(stat.get("blocked_until"), 0.0)
        if now_ts < blocked_until:
            rem_min = max(1, int((blocked_until - now_ts) / 60))
            return False, f"AI stoplo zly setup ({key}), pauza este ~{rem_min} min", 0.2

    scores = AI_MEMORY.get("feature_scores", {})
    raw_score = 0.0
    for feat_name in ["osc_pos", "pred_up", "above_ema9"]:
        val = 1.0 if features.get(feat_name) else -0.5
        raw_score += val * safe_float(scores.get(feat_name), 0.0)

    # Jemny vplyv historie konkretneho paru/TF, ak uz ma aspon 2 obchody
    if stat and (stat.get("wins", 0) + stat.get("losses", 0)) >= 2:
        w = stat.get("wins", 0)
        l = stat.get("losses", 0)
        local_rate = w / max(1, w + l)
        raw_score += (local_rate - 0.5) * 1.2

    win_prob = 1.0 / (1.0 + math.exp(-max(-5.0, min(5.0, raw_score))))

    # Kym nema AI nazbieranych aspon 6 obchodov, neblokuje kvoli nizkej pravdepodobnosti
    if len(AI_MEMORY.get("samples", [])) >= 6:
        min_prob = safe_float(SETTINGS.get("ai_min_win_prob"), 0.45)
        if win_prob < min_prob:
            return False, f"AI skore {win_prob*100:.0f}% < {min_prob*100:.0f}%", win_prob

    return True, f"AI OK ({win_prob*100:.0f}%)", win_prob


def learn_from_closed_trade(trade: dict, pnl_pct: float, pnl_usdc: float, reason: str):
    """Ulozi uzavrety obchod do AI pamate, upravi vahy a pri opakovanych stratach zablokuje setup."""
    if not SETTINGS.get("use_ai_learning", True):
        return

    symbol = trade.get("market_pair", "UNKNOWN")
    tf = trade.get("tf", CURRENT_TIMEFRAME)
    mode = trade.get("mode_choice", SETTINGS.get("mode_choice", "ALL_GREEN"))
    features = trade.get("ai_features", {"osc_pos": True, "pred_up": True, "above_ema9": True})
    is_win = (pnl_usdc > 0) or (pnl_pct > 0)

    key = make_setup_key(symbol, tf, mode)
    stat = AI_MEMORY["pair_tf_stats"].setdefault(key, {
        "wins": 0,
        "losses": 0,
        "bad_streak": 0,
        "blocked_until": 0.0
    })

    step = 0.08
    target = 1.0 if is_win else -1.0
    for feat_name in ["osc_pos", "pred_up", "above_ema9"]:
        feat_sign = 1.0 if features.get(feat_name) else -1.0
        cur_w = safe_float(AI_MEMORY["feature_scores"].get(feat_name), 0.0)
        delta = step * target * feat_sign
        AI_MEMORY["feature_scores"][feat_name] = max(-2.0, min(2.0, round(cur_w + delta, 4)))

    if is_win:
        stat["wins"] += 1
        stat["bad_streak"] = 0
        stat["blocked_until"] = 0.0
    else:
        stat["losses"] += 1
        stat["bad_streak"] += 1
        limit = max(1, int(SETTINGS.get("ai_bad_streak_limit", 2)))
        if SETTINGS.get("ai_block_bad_repeats", True) and stat["bad_streak"] >= limit:
            cd_sec = max(60, int(SETTINGS.get("ai_bad_cooldown_sec", 3600)))
            stat["blocked_until"] = datetime.now().timestamp() + cd_sec
            log_event(f"AI ZASTAVILO OPKOVANIE: {key} ({stat['bad_streak']} straty za sebou) na {cd_sec // 60} min.")

    AI_MEMORY["samples"].append({
        "key": key,
        "symbol": symbol,
        "tf": tf,
        "mode": mode,
        "features": features,
        "win": is_win,
        "pnl_pct": round(pnl_pct, 2),
        "pnl_usdc": round(pnl_usdc, 2),
        "reason": reason,
        "time": get_sk_time_str()
    })
    AI_MEMORY["samples"] = AI_MEMORY["samples"][-300:]
    save_ai_memory()


def get_ai_summary():
    samples = AI_MEMORY.get("samples", [])
    total = len(samples)
    wins = sum(1 for s in samples if s.get("win"))
    win_rate = round((wins / total) * 100.0, 1) if total > 0 else 0.0
    now_ts = datetime.now().timestamp()
    blocked_setups = [
        k for k, st in AI_MEMORY.get("pair_tf_stats", {}).items()
        if safe_float(st.get("blocked_until"), 0.0) > now_ts
    ]
    return {
        "enabled": bool(SETTINGS.get("use_ai_learning", True)),
        "block_bad_repeats": bool(SETTINGS.get("ai_block_bad_repeats", True)),
        "collected_trades": total,
        "ai_win_rate": win_rate,
        "blocked_count": len(blocked_setups),
        "blocked_setups": blocked_setups,
        "scores": AI_MEMORY.get("feature_scores", {})
    }


def load_persistent_data():
    global CLOSED_TRADES_HISTORY, DAILY_PNL_CALENDAR
    if os.path.exists(STORAGE_FILE):
        try:
            with open(STORAGE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                CLOSED_TRADES_HISTORY = data.get("history", [])
                DAILY_PNL_CALENDAR = data.get("calendar", [])
                log_event(f"Nacitanych {len(CLOSED_TRADES_HISTORY)} obchodov z disku.")
        except Exception as e:
            log_event(f"Chyba pri nacitani uloziska: {e}")


def save_persistent_data():
    try:
        cutoff_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        filtered_history = [h for h in CLOSED_TRADES_HISTORY if h.get("time", "") >= cutoff_date]
        data = {"history": filtered_history, "calendar": DAILY_PNL_CALENDAR}
        with open(STORAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_event(f"Chyba pri ukladani udajov na disk: {e}")


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


# --- HYBRIDNY REQUEST SYSTEM ---

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
        log_event(f"Chyba pri nacitani pravidiel pre {symbol}: {e}")
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
        log_event(f"Chyba pri nacitani zostatku: {err}")
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


def update_top_liquid_symbols():
    global TOP_LIQUID_SYMBOLS
    try:
        res = okx_public_request("/api/v5/market/tickers?instType=SPOT")
        if res.get("code") == "0" and res.get("data"):
            tickers = res["data"]
            valid = []
            for t in tickers:
                inst_id = t.get("instId", "")
                if not inst_id.endswith("-USDC"):
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
            elif top_20:
                merged = list(dict.fromkeys(top_20 + TOP_LIQUID_SYMBOLS))
                TOP_LIQUID_SYMBOLS = merged[:20]
    except Exception as e:
        log_event(f"Chyba pri aktualizacii top symbolov: {e}")


# --- 1:1 PINE SCRIPT MATEMATIKA (BUY A SELL SIGNALY) ---

def calc_ema_series(values, length):
    if not values:
        return []
    alpha = 2.0 / (length + 1.0)
    ema = [values[0]]
    for val in values[1:]:
        ema.append(val * alpha + ema[-1] * (1.0 - alpha))
    return ema


def calc_linreg_series(values, length, offset=0):
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
    global MARKET_SCANNER_CACHE, ACTIVE_TRADES
    coin_key = symbol.split("-")[0]

    try:
        url = f"/api/v5/market/candles?instId={symbol}&bar={bar_tf}&limit=120"
        res = okx_public_request(url)
        if res.get("code") != "0" or not res.get("data") or len(res["data"]) < 30:
            MARKET_SCANNER_CACHE[coin_key] = {
                "symbol": symbol, "price": 0.0, "oscillator": 0.0,
                "momentum": "SLABE", "impulse": "CAKA", "trade_status": "---",
                "be_status": "VYPNUTY", "ttp_status": "VYPNUTY", "ts_status": "VYPNUTY",
                "exec_signal": "CAKAJ", "signal": "CHYBA DAT", "is_green": False
            }
            return {"buy": False, "sell": False, "sell_reason": "", "features": {}}

        candles = res["data"]
        candles.reverse()
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
            "above_ema9": bool(curr_close > close_ema_9[-1])
        }

        ai_allowed, ai_note, ai_prob = check_ai_allows_buy(symbol, bar_tf, features)

        momentum_str = "SILNE" if last_cond_volume_bullish else "SLABE"
        impuls_active = last_buy_trigger or is_in_trade
        impulse_str = "AKTIVNY (V TRHU)" if impuls_active else "CAKA"

        trade_status_txt = f"{ACTIVE_TRADES[coin_key]['entry_price']:.4f}" if is_in_trade else "---"
        be_status_txt = f"{ACTIVE_TRADES[coin_key]['entry_price']:.4f}" if (is_in_trade and use_breakeven and breakeven_active) else ("CAKA" if (is_in_trade and use_breakeven) else "VYPNUTY")
        ttp_status_txt = f"{trailing_tp_stop_level:.4f}" if (is_in_trade and use_trailing_tp and trailing_tp_active and trailing_tp_stop_level) else ("V TRHU" if (is_in_trade and use_trailing_tp) else "VYPNUTY")
        ts_status_txt = f"{ACTIVE_TRADES[coin_key]['fixed_sl_price']:.4f}" if is_in_trade else f"-{SETTINGS['fixed_sl_pct']}%"

        if last_bar_buy and not is_in_trade:
            exec_signal_str = "NAKUPIŤ (BUY)" if ai_allowed else "AI STOP"
        elif last_bar_sell and is_in_trade:
            exec_signal_str = "PREDAŤ (SELL)"
        elif is_in_trade:
            exec_signal_str = "DRŽAŤ"
        else:
            exec_signal_str = "CAKAJ" if ai_allowed else "AI STOP"

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
            "exec_signal": exec_signal_str,
            "ai_status": f"{int(ai_prob*100)}% {'OK' if ai_allowed else 'STOP'}",
            "signal": "BUY" if last_bar_buy else ("SELL" if last_bar_sell else ("BULLISH" if last_cond_volume_bullish else "CAKA")),
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
            "momentum": "SLABE", "impulse": "CAKA", "trade_status": "---",
            "be_status": "VYPNUTY", "ttp_status": "VYPNUTY", "ts_status": "VYPNUTY",
            "exec_signal": "CAKAJ", "ai_status": "--", "signal": "VYNIMKA", "is_green": False
        }
        return {"buy": False, "sell": False, "sell_reason": "", "features": {}, "ai_allowed": True, "ai_note": ""}


# --- AUTONOMNA SKENOVACIA SLUCKA ---

async def autonomous_scanner_loop():
    await asyncio.sleep(3)
    log_event(f"Autonomny Pine Script skener + jednoduche AI spusteny pre {len(TOP_LIQUID_SYMBOLS)} symbolov.")
    while True:
        try:
            tf_str = CURRENT_TIMEFRAME.lower()
            max_allowed = SETTINGS.get("max_parallel", 1)

            for coin_name in list(ACTIVE_TRADES.keys()):
                if coin_name not in ACTIVE_TRADES:
                    continue
                trade = ACTIVE_TRADES[coin_name]
                symbol = trade["market_pair"]
                sig = evaluate_pine_strategy(symbol, bar_tf=tf_str)
                if sig["sell"]:
                    log_event(f"PINE SELL signal pre {symbol} ({sig['sell_reason']}).")
                    await close_trade_position(coin_name, sig["sell_reason"])
                await asyncio.sleep(0.25)

            if IS_BOT_ACTIVE and len(ACTIVE_TRADES) < max_allowed:
                if check_btc_health():
                    for symbol in list(TOP_LIQUID_SYMBOLS):
                        if not IS_BOT_ACTIVE or len(ACTIVE_TRADES) >= SETTINGS.get("max_parallel", 1):
                            break

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
                                log_event(f"AI preskocilo {symbol}: {sig.get('ai_note', 'zly setup')}")
                                continue
                            log_event(f"PINE BUY signal pre {symbol} ({sig.get('ai_note', '')}).")
                            await execute_autonomous_buy(symbol, ai_features=sig.get("features"))

                        await asyncio.sleep(0.3)
        except Exception as e:
            log_event(f"Chyba v autonomnom skeneri: {e}")
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
            log_event(f"Nedostatok USDC prostriedkov: {free_capital}")
            return

        ticker_res = okx_public_request(f"/api/v5/market/ticker?instId={okx_symbol}")
        if ticker_res.get("code") != "0" or not ticker_res.get("data"):
            log_event(f"Nepodarilo sa ziskat cenu pre {okx_symbol}")
            return

        current_price = safe_float(ticker_res["data"][0]["last"])
        raw_amount = safe_budget / current_price

        lot_sz, min_sz = get_instrument_rules(okx_symbol)
        formatted_sz = format_amount(raw_amount, lot_sz, min_sz)

        if not formatted_sz:
            log_event(f"Mnozstvo pre {okx_symbol} nedosahuje minimum burzy minSz ({min_sz})")
            return

        order_body = json.dumps({
            "instId": okx_symbol, "tdMode": "cash", "side": "buy",
            "ordType": "market", "sz": formatted_sz
        })

        order_res = okx_request("POST", "/api/v5/trade/order", order_body)
        if order_res.get("code") != "0":
            err_msg = order_res.get('msg', 'Neznama chyba OKX')
            log_event(f"Par {okx_symbol} odmietnuty v Demo rezime ({err_msg}).")
            return

        coin_amount = float(formatted_sz)
        actual_spent_usdc = coin_amount * current_price
        fixed_sl_pct = SETTINGS.get("fixed_sl_pct", 1.5)
        fixed_sl_price = current_price * (1.0 - (fixed_sl_pct / 100.0))

        ACTIVE_TRADES[coin_name] = {
            "entry_price": current_price,
            "current_price": current_price,
            "fixed_sl_price": fixed_sl_price,
            "fixed_sl_pct": fixed_sl_pct,
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
            "ai_features": ai_features or {"osc_pos": True, "pred_up": True, "above_ema9": True}
        }

        log_event(f"NAKUP [{CURRENT_TIMEFRAME}]: {okx_symbol} za {current_price:.4f} USDC | Investicia: {actual_spent_usdc:.2f} USDC | Fixny SL: -{fixed_sl_pct}%")

        inline_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "ZATVORIT VSETKO", "callback_data": "sell_now"},
                    {"text": "PAUZA BOTA", "callback_data": "toggle_bot"}
                ]
            ]
        }
        send_telegram_msg(
            f"<b>PINE BUY NAKUP [TF: {CURRENT_TIMEFRAME}]</b>\n"
            f"<b>Par:</b> {okx_symbol}\n"
            f"<b>Vstup:</b> {current_price:.4f}\n"
            f"<b>Investicia:</b> {actual_spent_usdc:.2f} USDC\n"
            f"<b>Fixny Stop-Loss:</b> -{fixed_sl_pct}% ({fixed_sl_price:.4f})",
            reply_markup=inline_keyboard
        )


async def close_trade_position(coin: str, reason: str):
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
            learn_from_closed_trade(trade, pnl_pct, pnl_usdc, reason)
            LAST_TRADE_TIME[coin] = datetime.now().timestamp()

            log_event(f"Uzavreta pozicia ({reason}) pre {symbol} | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
            send_telegram_msg(f"<b>UZAVRETA POZICIA ({reason})</b>\n<b>Par:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
        except Exception as e:
            log_event(f"Chyba pri zatvarani {symbol}: {e}")
        finally:
            if coin in ACTIVE_TRADES:
                del ACTIVE_TRADES[coin]


# --- MONITOR RIZIK (JEDINA FIXNA POISTKA: PEVNY STOP-LOSS) ---

async def fixed_stoploss_monitor_loop():
    global ACTIVE_TRADES, CURRENT_STAGE
    await asyncio.sleep(3)
    while True:
        if not ACTIVE_TRADES:
            if IS_BOT_ACTIVE:
                max_p = SETTINGS.get("max_parallel", 1)
                sl_p = SETTINGS.get("fixed_sl_pct", 1.5)
                ai_cnt = len(AI_MEMORY.get("samples", []))
                CURRENT_STAGE = f"STAV: Bot aktivny (Max {max_p}x | Fixny SL: -{sl_p}% | AI obchodov: {ai_cnt}). Cakam na BUY/SELL..."
            else:
                CURRENT_STAGE = "STAV: Bot pozastaveny."
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
                    CURRENT_STAGE = f"STAV: Pozicii: {len(ACTIVE_TRADES)} | {symbol} ({pnl_pct:+.2f}%) | Fixny SL: -{fixed_sl_pct}%"

                    if current_price <= fixed_sl_price:
                        await close_trade_position(coin, f"Fixny Stop-Loss (-{fixed_sl_pct}%)")
                        continue
        except Exception:
            pass
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_persistent_data()
    load_ai_memory()
    update_top_liquid_symbols()
    log_event(f"Apex Scalper PRO + jednoduche AI spusteny (Fixny SL -{SETTINGS['fixed_sl_pct']}%).")
    monitor_task = asyncio.create_task(fixed_stoploss_monitor_loop())
    scanner_task = asyncio.create_task(autonomous_scanner_loop())
    updater_task = asyncio.create_task(top_symbols_updater_loop())
    send_telegram_msg(f"Apex Scalper PRO + jednoduche AI pripraveny!")
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
                send_telegram_msg("Rucny predaj aktivovany z Telegramu!")
            elif cb_data == "toggle_bot":
                await toggle_bot()
    except Exception:
        pass
    return {"status": "ok"}


@app.post("/api/toggle-bot")
async def toggle_bot():
    global IS_BOT_ACTIVE, CURRENT_STAGE
    IS_BOT_ACTIVE = not IS_BOT_ACTIVE
    status_str = "aktivny" if IS_BOT_ACTIVE else "pozastaveny"
    CURRENT_STAGE = f"STAV: Bot je {status_str}."
    log_event(f"Prepinač bota: {status_str.upper()}")
    send_telegram_msg(f"Zmena stavu: Bot je teraz {status_str.upper()}.")
    return JSONResponse({"status": "success", "is_bot_active": IS_BOT_ACTIVE})


@app.post("/api/toggle-btc-filter")
async def toggle_btc_filter():
    SETTINGS["use_btc_filter"] = not SETTINGS.get("use_btc_filter", True)
    status_str = "zapnuty" if SETTINGS["use_btc_filter"] else "vypnuty"
    log_event(f"BTC Trend Guard filter: {status_str.upper()}")
    return JSONResponse({"status": "success", "use_btc_filter": SETTINGS["use_btc_filter"]})


@app.post("/api/toggle-compounding")
async def toggle_compounding():
    SETTINGS["compounding_enabled"] = not SETTINGS.get("compounding_enabled", False)
    status_str = "ZAPNUTE" if SETTINGS["compounding_enabled"] else "VYPNUTE"
    log_event(f"COMPOUNDING: {status_str}")
    return JSONResponse({"status": "success", "compounding_enabled": SETTINGS["compounding_enabled"]})


@app.post("/api/set-budget/{amount}")
async def set_budget(amount: int):
    if amount in [500, 1000, 3000, 5000, 10000]:
        SETTINGS["max_budget_cap"] = float(amount)
        log_event(f"Maximalny rozpocet zmeneny na: {amount} USDC")
        return JSONResponse({"status": "success", "max_budget_cap": amount})
    return JSONResponse({"status": "error", "message": "Neplatna hodnota rozpoctu."})


@app.post("/api/set-parallel/{count}")
async def set_parallel(count: int):
    if count in [1, 2, 3, 4]:
        SETTINGS["max_parallel"] = count
        log_event(f"Maximalny pocet paralelnych obchodov: {count}")
        return JSONResponse({"status": "success", "max_parallel": count})
    return JSONResponse({"status": "error", "message": "Neplatna hodnota."})


@app.post("/api/set-sl/{sl_val}")
async def set_sl(sl_val: float):
    if 0.2 <= sl_val <= 20.0:
        SETTINGS["fixed_sl_pct"] = float(sl_val)
        log_event(f"Fixny Stop-Loss zmeneny na: -{sl_val}%")
        return JSONResponse({"status": "success", "fixed_sl_pct": sl_val})
    return JSONResponse({"status": "error", "message": "Neplatna hodnota SL."})


@app.post("/api/set-tf/{tf_val}")
async def set_tf(tf_val: str):
    global CURRENT_TIMEFRAME, MARKET_SCANNER_CACHE
    tf_lower = tf_val.lower()
    if tf_lower in ALLOWED_TIMEFRAMES:
        CURRENT_TIMEFRAME = tf_val.upper()
        MARKET_SCANNER_CACHE.clear()
        log_event(f"Casovy ramec (TF) zmeneny na: {CURRENT_TIMEFRAME}")
        return JSONResponse({"status": "success", "tf": CURRENT_TIMEFRAME})
    return JSONResponse({"status": "error", "message": "Neplatny casovy ramec."})


@app.post("/api/update-pine-settings")
async def update_pine_settings(request: Request):
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
        SETTINGS["fast_len"] = max(1, min(200, int(payload.get("fast_len", SETTINGS["fast_len"]))))
        SETTINGS["slow_len"] = max(1, min(300, int(payload.get("slow_len", SETTINGS["slow_len"]))))
        SETTINGS["signal_len"] = max(1, min(200, int(payload.get("signal_len", SETTINGS["signal_len"]))))
        SETTINGS["pred_look"] = max(1, min(10, int(payload.get("pred_look", SETTINGS["pred_look"]))))
        SETTINGS["pivot_len"] = max(1, min(10, int(payload.get("pivot_len", SETTINGS["pivot_len"]))))
        SETTINGS["fib_choice"] = fib_val

        SETTINGS["use_trailing"] = bool(payload.get("use_trailing", SETTINGS["use_trailing"]))
        SETTINGS["trail_pct"] = max(0.1, min(50.0, float(payload.get("trail_pct", SETTINGS["trail_pct"]))))
        SETTINGS["use_breakeven"] = bool(payload.get("use_breakeven", SETTINGS["use_breakeven"]))
        SETTINGS["be_trigger_pct"] = max(0.05, min(50.0, float(payload.get("be_trigger_pct", SETTINGS["be_trigger_pct"]))))
        SETTINGS["use_trailing_tp"] = bool(payload.get("use_trailing_tp", SETTINGS["use_trailing_tp"]))
        SETTINGS["ttp_trigger_pct"] = max(0.1, min(100.0, float(payload.get("ttp_trigger_pct", SETTINGS["ttp_trigger_pct"]))))
        SETTINGS["ttp_callback_pct"] = max(0.05, min(50.0, float(payload.get("ttp_callback_pct", SETTINGS["ttp_callback_pct"]))))
        SETTINGS["use_ha_exit"] = bool(payload.get("use_ha_exit", SETTINGS["use_ha_exit"]))
        SETTINGS["ha_exit_consecutive"] = max(1, min(3, int(payload.get("ha_exit_consecutive", SETTINGS["ha_exit_consecutive"]))))

        # Jednoduche AI nastavenia
        SETTINGS["use_ai_learning"] = bool(payload.get("use_ai_learning", SETTINGS["use_ai_learning"]))
        SETTINGS["ai_block_bad_repeats"] = bool(payload.get("ai_block_bad_repeats", SETTINGS["ai_block_bad_repeats"]))
        SETTINGS["ai_bad_streak_limit"] = max(1, min(10, int(payload.get("ai_bad_streak_limit", SETTINGS["ai_bad_streak_limit"]))))
        SETTINGS["ai_bad_cooldown_sec"] = max(60, min(86400, int(payload.get("ai_bad_cooldown_sec", SETTINGS["ai_bad_cooldown_sec"]))))

        MARKET_SCANNER_CACHE.clear()
        log_event(f"Nastavenia aktualizovane (Rezim: {mode_val}, AI ucenie: {SETTINGS['use_ai_learning']}, Stop zlych opakovani: {SETTINGS['ai_block_bad_repeats']}).")
        return JSONResponse({"status": "success", "pine_settings": get_pine_settings_dict()})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)})


def get_pine_settings_dict():
    return {
        "mode_choice": SETTINGS["mode_choice"],
        "fast_len": SETTINGS["fast_len"],
        "slow_len": SETTINGS["slow_len"],
        "signal_len": SETTINGS["signal_len"],
        "pred_look": SETTINGS["pred_look"],
        "pivot_len": SETTINGS["pivot_len"],
        "fib_choice": SETTINGS["fib_choice"],
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
                return JSONResponse({"status": "error", "message": "Bot je pozastaveny."})

            coin_upper = coin.upper().strip()
            max_allowed = SETTINGS.get("max_parallel", 1)
            if len(ACTIVE_TRADES) >= max_allowed or coin_upper in ACTIVE_TRADES:
                return JSONResponse({"status": "error", "message": "Dosiahnuty limit paralelnych pozicii alebo pozicia uz existuje."})

            okx_symbol = f"{coin_upper}-USDC"
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
                return JSONResponse({"status": "error", "message": f"Nepodarilo sa ziskat cenu pre {okx_symbol}"})

            current_price = safe_float(ticker_res["data"][0]["last"])
            raw_amount = safe_budget / current_price

            lot_sz, min_sz = get_instrument_rules(okx_symbol)
            formatted_sz = format_amount(raw_amount, lot_sz, min_sz)
            if not formatted_sz:
                return JSONResponse({"status": "error", "message": f"Mnozstvo pod minimom minSz ({min_sz})"})

            order_body = json.dumps({
                "instId": okx_symbol, "tdMode": "cash", "side": "buy",
                "ordType": "market", "sz": formatted_sz
            })

            order_res = okx_request("POST", "/api/v5/trade/order", order_body)
            if order_res.get("code") != "0":
                err_msg = order_res.get('msg', 'Neznama chyba OKX')
                return JSONResponse({"status": "error", "message": f"OKX odmietlo prikaz ({err_msg})"})

            coin_amount = float(formatted_sz)
            actual_spent_usdc = coin_amount * current_price
            fixed_sl_pct = SETTINGS.get("fixed_sl_pct", 1.5)
            fixed_sl_price = current_price * (1.0 - (fixed_sl_pct / 100.0))

            ACTIVE_TRADES[coin_upper] = {
                "entry_price": current_price,
                "current_price": current_price,
                "fixed_sl_price": fixed_sl_price,
                "fixed_sl_pct": fixed_sl_pct,
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
                "ai_features": {"osc_pos": True, "pred_up": True, "above_ema9": True}
            }
            log_event(f"Manualny nakup [{CURRENT_TIMEFRAME}]: {okx_symbol} za {current_price:.4f} | Investicia: {actual_spent_usdc:.2f}$ | Fixny SL: -{fixed_sl_pct}%")
            return JSONResponse({"status": "success", "symbol": okx_symbol})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})


@app.post("/api/sell-now")
async def sell_now():
    if not ACTIVE_TRADES:
        return JSONResponse({"status": "error", "message": "Ziadna aktivna pozicia na predaj."})
    sold = []
    for coin in list(ACTIVE_TRADES.keys()):
        symbol = ACTIVE_TRADES[coin]["market_pair"]
        await close_trade_position(coin, "Rucny okamzity predaj")
        sold.append(symbol)
    return JSONResponse({"status": "success", "sold": sold})


HTML_TEMPLATE_CONTENT = """<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Apex Scalper PRO + Simple AI</title>
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
        .status-ticker { font-size: 0.75rem; color: var(--yellow); background: var(--card-bg); border: 1px solid var(--border-color); padding: 9px 14px; border-radius: 5px; display: flex; align-items: center; gap: 10px; font-weight: 600; }
        .status-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--green); box-shadow: 0 0 10px var(--green); }
        .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
        .stat-card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 5px; padding: 12px 14px; }
        .stat-title { font-size: 0.65rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.6px; }
        .stat-value { font-size: 1.15rem; font-weight: 700; margin-top: 4px; color: var(--accent-white); }
        .section-title { font-size: 0.72rem; text-transform: uppercase; color: var(--text-muted); letter-spacing: 1px; margin-top: 8px; margin-bottom: 5px; font-weight: 600; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
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
                <h1>APEX SCALPER // PINE BUY & SELL + SIMPLE AI</h1>
                <p>
                    <span id="header-pair">Top USDC · OKX Demo</span>
                    <span>Zostatok: <span id="header-balance" class="balance-tag">-- USDC</span></span>
                    <span>Aktivny TF: <span id="header-tf" class="balance-tag">{{ CURRENT_TIMEFRAME }}</span></span>
                    <span>Fixny SL: <span id="header-sl" class="balance-tag">-{{ SETTINGS.fixed_sl_pct }}%</span></span>
                    <span>AI pamäť: <span id="header-ai" class="balance-tag">0 obch.</span></span>
                </p>
            </div>
            <div class="actions">
                <button class="tf-btn btn {% if CURRENT_TIMEFRAME|upper == '5M' %}btn-yellow{% endif %}" data-tf="5m" onclick="setTf('5m')">5M</button>
                <button class="tf-btn btn {% if CURRENT_TIMEFRAME|upper == '8M' %}btn-yellow{% endif %}" data-tf="8m" onclick="setTf('8m')">8M</button>
                <button class="tf-btn btn {% if CURRENT_TIMEFRAME|upper == '15M' %}btn-yellow{% endif %}" data-tf="15m" onclick="setTf('15m')">15M</button>
                <button class="tf-btn btn {% if CURRENT_TIMEFRAME|upper == '30M' %}btn-yellow{% endif %}" data-tf="30m" onclick="setTf('30m')">30M</button>
                <button class="tf-btn btn {% if CURRENT_TIMEFRAME|upper == '45M' %}btn-yellow{% endif %}" data-tf="45m" onclick="setTf('45m')">45M</button>

                <button class="sl-btn btn {% if SETTINGS.fixed_sl_pct == 1.0 %}btn-yellow{% endif %}" data-sl="1.0" onclick="setSl(1.0)">SL 1%</button>
                <button class="sl-btn btn {% if SETTINGS.fixed_sl_pct == 1.5 %}btn-yellow{% endif %}" data-sl="1.5" onclick="setSl(1.5)">SL 1.5%</button>
                <button class="sl-btn btn {% if SETTINGS.fixed_sl_pct == 2.0 %}btn-yellow{% endif %}" data-sl="2.0" onclick="setSl(2.0)">SL 2%</button>

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
            <div class="status-dot"></div>
            <span id="current-stage">{{ CURRENT_STAGE }}</span>
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
                <span>Nastavenia Pine Script Indikatora + Jednoduche AI</span>
                <span id="ai-badge" class="text-yellow">AI: 0 OBCH. | ZASTAVENE: 0</span>
            </div>
            <div class="pine-panel">
                <div class="pine-grid">
                    <div class="pine-group">
                        <div class="pine-group-title">0. Rezim & 1. Objemovy Oscilator</div>
                        <div class="pine-row">
                            <label for="pine-mode">Rezim strategie</label>
                            <select id="pine-mode" class="pine-select">
                                <option value="ALL_GREEN" {% if SETTINGS.mode_choice == 'ALL_GREEN' %}selected{% endif %}>All Green</option>
                                <option value="BREAKOUT" {% if SETTINGS.mode_choice == 'BREAKOUT' %}selected{% endif %}>Breakout</option>
                                <option value="FIB" {% if SETTINGS.mode_choice == 'FIB' %}selected{% endif %}>Fibonacci Touch</option>
                            </select>
                        </div>
                        <div class="pine-row">
                            <label for="pine-fast-len">Rychla dlzka (Vol)</label>
                            <input type="number" id="pine-fast-len" class="pine-input" min="1" max="200" step="1" value="{{ SETTINGS.fast_len }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-slow-len">Pomala dlzka (Vol)</label>
                            <input type="number" id="pine-slow-len" class="pine-input" min="1" max="300" step="1" value="{{ SETTINGS.slow_len }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-signal-len">Signalna ciara (MA)</label>
                            <input type="number" id="pine-signal-len" class="pine-input" min="1" max="200" step="1" value="{{ SETTINGS.signal_len }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-pred-look">Citlivost predikcie</label>
                            <input type="number" id="pine-pred-look" class="pine-input" min="1" max="10" step="1" value="{{ SETTINGS.pred_look }}">
                        </div>
                    </div>

                    <div class="pine-group">
                        <div class="pine-group-title">2. Fibonacci Hladiny</div>
                        <div class="pine-row">
                            <label for="pine-pivot-len">Rychlost potvrdenia (Pivot)</label>
                            <input type="number" id="pine-pivot-len" class="pine-input" min="1" max="10" step="1" value="{{ SETTINGS.pivot_len }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-fib-choice">Nakupna Fib hladina</label>
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
                            <label for="pine-use-trailing">Zapnut Trailing Stop</label>
                            <input type="checkbox" id="pine-use-trailing" class="pine-checkbox" {% if SETTINGS.use_trailing %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="pine-trail-pct">Vzdialenost TS (%)</label>
                            <input type="number" id="pine-trail-pct" class="pine-input" min="0.1" max="50.0" step="0.1" value="{{ SETTINGS.trail_pct }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-use-be">Zapnut Break-Even</label>
                            <input type="checkbox" id="pine-use-be" class="pine-checkbox" {% if SETTINGS.use_breakeven %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="pine-be-trigger">Zisk % pre Break-Even</label>
                            <input type="number" id="pine-be-trigger" class="pine-input" min="0.05" max="50.0" step="0.05" value="{{ SETTINGS.be_trigger_pct }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-use-ttp">Zapnut Trailing TP</label>
                            <input type="checkbox" id="pine-use-ttp" class="pine-checkbox" {% if SETTINGS.use_trailing_tp %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="pine-ttp-trigger">Aktivacia TTP (%)</label>
                            <input type="number" id="pine-ttp-trigger" class="pine-input" min="0.1" max="100.0" step="0.1" value="{{ SETTINGS.ttp_trigger_pct }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-ttp-callback">Odstup z maxima TTP (%)</label>
                            <input type="number" id="pine-ttp-callback" class="pine-input" min="0.05" max="50.0" step="0.05" value="{{ SETTINGS.ttp_callback_pct }}">
                        </div>
                    </div>

                    <div class="pine-group">
                        <div class="pine-group-title">3.2 HA Exit & 4. Jednoduche AI</div>
                        <div class="pine-row">
                            <label for="pine-use-ha-exit">Povolit exit pri otoceni HA</label>
                            <input type="checkbox" id="pine-use-ha-exit" class="pine-checkbox" {% if SETTINGS.use_ha_exit %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="pine-ha-consec">Pocet cervenych HA sviecok</label>
                            <select id="pine-ha-consec" class="pine-select">
                                <option value="1" {% if SETTINGS.ha_exit_consecutive == 1 %}selected{% endif %}>1</option>
                                <option value="2" {% if SETTINGS.ha_exit_consecutive == 2 %}selected{% endif %}>2</option>
                                <option value="3" {% if SETTINGS.ha_exit_consecutive == 3 %}selected{% endif %}>3</option>
                            </select>
                        </div>
                        <div class="pine-row">
                            <label for="ai-use-learning">Zapnut ucenie AI</label>
                            <input type="checkbox" id="ai-use-learning" class="pine-checkbox" {% if SETTINGS.use_ai_learning %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="ai-block-repeats">Zastavit zle opakovania</label>
                            <input type="checkbox" id="ai-block-repeats" class="pine-checkbox" {% if SETTINGS.ai_block_bad_repeats %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="ai-bad-streak">Po kolkych stratach stop</label>
                            <input type="number" id="ai-bad-streak" class="pine-input" min="1" max="10" step="1" value="{{ SETTINGS.ai_bad_streak_limit }}">
                        </div>
                        <div class="pine-row">
                            <label for="ai-cooldown-min">Pauza zleho setupu (min)</label>
                            <input type="number" id="ai-cooldown-min" class="pine-input" min="1" max="1440" step="1" value="{{ (SETTINGS.ai_bad_cooldown_sec // 60) }}">
                        </div>
                    </div>
                </div>
                <div class="pine-footer">
                    <span id="pine-save-status" class="pine-save-status">Pripravene – AI uklada obchody do ai_memory.json.</span>
                    <div style="display: flex; gap: 6px;">
                        <button class="btn" onclick="resetPineDefaults()">OBNOVIT ZAKLAD</button>
                        <button class="btn btn-on" onclick="savePineSettings()">ULOZIT NASTAVENIA</button>
                    </div>
                </div>
            </div>
        </div>

        <div>
            <div class="section-title">Active Positions Matrix (Pine SELL alebo Fixny SL)</div>
            <div class="terminal-card">
                <table>
                    <thead>
                        <tr>
                            <th>Pair / TF</th>
                            <th>Entry Price</th>
                            <th>Current Price</th>
                            <th>Fixed SL Price</th>
                            <th>Invested USDC</th>
                            <th>Live PnL</th>
                        </tr>
                    </thead>
                    <tbody id="active-position-tbody">
                        <tr><td colspan="6" style="text-align: center; color: var(--text-muted);">Skenujem trh pre Pine Script BUY signaly...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div>
            <div class="section-title">Live Market Scanner Status (20 Top Liquid USDC Pairs)</div>
            <div class="terminal-card scrollable-card" style="max-height: 260px;">
                <table>
                    <thead>
                        <tr>
                            <th>Token / Par</th>
                            <th>Cena</th>
                            <th>1. Objem. Momentum</th>
                            <th>2. Nakupny Impulz</th>
                            <th>3. Stav Obchodu</th>
                            <th>4. Break-Even</th>
                            <th>5. Trailing TP</th>
                            <th>6. Fixny SL</th>
                            <th>AI Stav</th>
                            <th>7. Exekucny Pokyn</th>
                        </tr>
                    </thead>
                    <tbody id="scanner-tbody">
                        <tr><td colspan="10" style="text-align: center; color: var(--text-muted);">Nacitavam stav skenera...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div>
            <div class="section-title">Execution History (3-Month Persistent Storage)</div>
            <div class="terminal-card scrollable-card">
                <table>
                    <thead>
                        <tr>
                            <th>Timestamp</th>
                            <th>Pair</th>
                            <th>Trigger Reason</th>
                            <th>Net PnL</th>
                        </tr>
                    </thead>
                    <tbody id="history-tbody">
                        <tr><td colspan="4" style="text-align: center; color: var(--text-muted);">Zatial ziadne ukoncene obchody.</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div>
            <div class="section-title">System Execution Logs</div>
            <div class="logs-box" id="logs-container">
                <div class="log-line">[System Initialized] Pine BUY & SELL + Simple AI pripraveny.</div>
            </div>
        </div>
    </div>

    <script>
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

        async function setSl(slVal) {
            let res = await fetch(`/api/set-sl/${slVal}`, {method: 'POST'});
            let data = await res.json();
            if (data.status === 'success') {
                document.querySelectorAll('.sl-btn').forEach(b => {
                    b.classList.remove('btn-yellow');
                    if (parseFloat(b.getAttribute('data-sl')) === slVal) {
                        b.classList.add('btn-yellow');
                    }
                });
                document.getElementById('header-sl').innerText = `-${slVal}%`;
                updateState();
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
            document.getElementById('pine-fast-len').value = ps.fast_len;
            document.getElementById('pine-slow-len').value = ps.slow_len;
            document.getElementById('pine-signal-len').value = ps.signal_len;
            document.getElementById('pine-pred-look').value = ps.pred_look;
            document.getElementById('pine-pivot-len').value = ps.pivot_len;
            document.getElementById('pine-fib-choice').value = ps.fib_choice;
            document.getElementById('pine-use-trailing').checked = !!ps.use_trailing;
            document.getElementById('pine-trail-pct').value = ps.trail_pct;
            document.getElementById('pine-use-be').checked = !!ps.use_breakeven;
            document.getElementById('pine-be-trigger').value = ps.be_trigger_pct;
            document.getElementById('pine-use-ttp').checked = !!ps.use_trailing_tp;
            document.getElementById('pine-ttp-trigger').value = ps.ttp_trigger_pct;
            document.getElementById('pine-ttp-callback').value = ps.ttp_callback_pct;
            document.getElementById('pine-use-ha-exit').checked = !!ps.use_ha_exit;
            document.getElementById('pine-ha-consec').value = String(ps.ha_exit_consecutive);
            document.getElementById('ai-use-learning').checked = !!ps.use_ai_learning;
            document.getElementById('ai-block-repeats').checked = !!ps.ai_block_bad_repeats;
            document.getElementById('ai-bad-streak').value = ps.ai_bad_streak_limit;
            document.getElementById('ai-cooldown-min').value = Math.max(1, Math.round((ps.ai_bad_cooldown_sec || 3600) / 60));
        }

        async function savePineSettings() {
            const cooldownMin = Math.max(1, parseInt(document.getElementById('ai-cooldown-min').value, 10) || 60);
            const payload = {
                mode_choice: document.getElementById('pine-mode').value,
                fast_len: parseInt(document.getElementById('pine-fast-len').value, 10),
                slow_len: parseInt(document.getElementById('pine-slow-len').value, 10),
                signal_len: parseInt(document.getElementById('pine-signal-len').value, 10),
                pred_look: parseInt(document.getElementById('pine-pred-look').value, 10),
                pivot_len: parseInt(document.getElementById('pine-pivot-len').value, 10),
                fib_choice: document.getElementById('pine-fib-choice').value,
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
                ai_bad_cooldown_sec: cooldownMin * 60
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
                statusEl.style.color = 'var(--green)';
                statusEl.innerText = 'Ulozene! Nastavenia a AI pravidla su aktivne.';
                updateState();
            } else {
                statusEl.style.color = 'var(--red)';
                statusEl.innerText = 'Chyba: ' + data.message;
            }
        }

        async function resetPineDefaults() {
            const defaults = {
                mode_choice: "ALL_GREEN",
                fast_len: 5,
                slow_len: 20,
                signal_len: 9,
                pred_look: 3,
                pivot_len: 2,
                fib_choice: "0.786",
                use_trailing: true,
                trail_pct: 1.5,
                use_breakeven: true,
                be_trigger_pct: 0.35,
                use_trailing_tp: true,
                ttp_trigger_pct: 0.8,
                ttp_callback_pct: 0.2,
                use_ha_exit: true,
                ha_exit_consecutive: 2,
                use_ai_learning: true,
                ai_block_bad_repeats: true,
                ai_bad_streak_limit: 2,
                ai_bad_cooldown_sec: 3600
            };
            applyPineFormValues(defaults);
            await savePineSettings();
        }

        async function manualBuyCustom() {
            let coinInput = document.getElementById('custom-coin');
            let coin = coinInput.value.trim().toUpperCase();
            if (!coin) { alert("Zadajte skratku tokenu (napr. SOL, SUI)!"); return; }
            if (!confirm(`Naozaj vykonat manualny nakup ${coin}-USDC?`)) return;

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
            if (!confirm("Naozaj nudzovo zatvorit vsetky pozicie?")) return;
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

                if (data.ai) {
                    document.getElementById('header-ai').innerText = `${data.ai.collected_trades} obch. (${data.ai.ai_win_rate}%)`;
                    document.getElementById('ai-badge').innerText = `AI: ${data.ai.collected_trades} OBCH. | ZASTAVENE: ${data.ai.blocked_count}`;
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
                        html += `<tr>
                            <td><b>${t.market_pair}</b><br><span style="font-size:0.6rem; color:var(--text-muted);">TF: ${t.tf}</span></td>
                            <td>${t.entry_price.toFixed(4)}</td>
                            <td>${t.current_price.toFixed(4)}</td>
                            <td class="text-red">${t.fixed_sl_price.toFixed(4)} (-${t.fixed_sl_pct}%)</td>
                            <td>${t.invested_usdc.toFixed(2)} USDC</td>
                            <td class="${pnlClass}">${t.live_pnl_pct >= 0 ? "+" : ""}${t.live_pnl_pct.toFixed(2)}% (${t.live_pnl_usdc >= 0 ? "+" : ""}${t.live_pnl_usdc.toFixed(2)} $)</td>
                        </tr>`;
                    }
                    activeTbody.innerHTML = html;
                    sellBtn.disabled = false;
                } else {
                    activeTbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">Skenujem trh pre Pine Script BUY signaly...</td></tr>`;
                    sellBtn.disabled = true;
                }

                let scannerTbody = document.getElementById('scanner-tbody');
                let scannerData = data.scanner_cache || {};
                if (Object.keys(scannerData).length > 0) {
                    let sHtml = "";
                    for (let [coin, info] of Object.entries(scannerData)) {
                        let momClass = info.momentum === "SILNE" ? "text-green" : "text-red";
                        let impClass = info.impulse.includes("AKTIVNY") ? "text-green" : "text-yellow";
                        let aiClass = (info.ai_status || "").includes("STOP") ? "text-red" : "text-green";
                        let execClass = info.exec_signal.includes("NAKUPIŤ") ? "text-green" : (info.exec_signal.includes("PREDAŤ") || info.exec_signal.includes("STOP") ? "text-red" : "text-yellow");

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
                    scannerTbody.innerHTML = `<tr><td colspan="10" style="text-align: center; color: var(--text-muted);">Prebieha uvodne skenovanie trhu...</td></tr>`;
                }

                let historyTbody = document.getElementById('history-tbody');
                if (data.history.length > 0) {
                    let hHtml = "";
                    for (let h of data.history) {
                        let pnlClass = h.pnl_usdc >= 0 ? "text-green" : "text-red";
                        hHtml += `<tr>
                            <td>${h.time}</td>
                            <td><b>${h.pair}</b></td>
                            <td>${h.reason}</td>
                            <td class="${pnlClass}">${h.pnl_usdc >= 0 ? "+" : ""}${h.pnl_usdc.toFixed(2)} $ (${h.pnl_pct >= 0 ? "+" : ""}${h.pnl_pct.toFixed(2)}%)</td>
                        </tr>`;
                    }
                    historyTbody.innerHTML = hHtml;
                } else {
                    historyTbody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">Zatial ziadne ukoncene obchody.</td></tr>`;
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
