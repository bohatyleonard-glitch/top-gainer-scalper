import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Template
import uvicorn

# --- KONFIGURÁCIA OKX DEMO & TELEGRAM ---
OKX_API_KEY = os.getenv("OKX_API_KEY", "54ebe9fe-1fd0-443d-8f76-0cd1a3439ddb")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "FBD5C8332D88E3ED5662470CA9C3E813")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "Novyzeland111/")
OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://eea.okx.com")
OKX_PUBLIC_URL = os.getenv("OKX_PUBLIC_URL", "https://www.okx.com")
OKX_SIMULATED = os.getenv("OKX_SIMULATED", "1")  # "1" pre OKX Demo Trading

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9158932827:AAHvZT_gQrVH6dk54BRx8_mxTFtkBud0tUI")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "9130979784")

STATE_FILE = os.getenv("BOT_STATE_FILE", "apex_ai_bot_state.json")
ALLOWED_TIMEFRAMES = ["5m", "8m", "15m", "30m", "45m"]
EXCLUDED_SYMBOLS = {"USDT-USDC", "DAI-USDC", "FDUSD-USDC"}

# --- GLOBÁLNY STAV BOTA ---
IS_BOT_ACTIVE = True
CURRENT_TIMEFRAME = "15M"
CURRENT_STAGE = "STAV: Inicializujem bota..."
MARKET_REGIME = "BULL"
IS_SIDEWAYS_MARKET = False
LAST_SIDEWAYS_ALERT_TS = 0.0
SIDEWAYS_ALERT_COOLDOWN_SEC = 1800

TOP_LIQUID_SYMBOLS = [
    "SOL-USDC", "ETH-USDC", "BTC-USDC", "SUI-USDC", "XRP-USDC",
    "DOGE-USDC", "ADA-USDC", "AVAX-USDC", "LINK-USDC", "TON-USDC",
    "NEAR-USDC", "APT-USDC", "TRX-USDC", "LTC-USDC", "PEPE-USDC",
    "AAVE-USDC", "UNI-USDC", "POL-USDC", "RENDER-USDC", "TIA-USDC"
]

ACTIVE_TRADES: dict[str, dict] = {}
LAST_TRADE_TIME: dict[str, float] = {}
CLOSED_TRADES_HISTORY: list[dict] = []
MARKET_SCANNER_CACHE: dict[str, dict] = {}
SYSTEM_LOGS: list[str] = []
trade_lock = asyncio.Lock()

SETTINGS = {
    "max_parallel": 1,
    "max_budget_cap": 1000.0,
    "compounding_enabled": False,
    "use_btc_filter": True,
    "reentry_cooldown_sec": 300,
    "mode_choice": "ALL_GREEN",
    "strict_buy_filter": False,
    "fast_len": 5,
    "slow_len": 20,
    "signal_len": 9,
    "pred_look": 3,
    "pivot_len": 2,
    "fib_choice": "0.786",
    "fixed_sl_pct": 1.5,
    "use_fixed_tp": False,
    "fixed_tp_pct": 1.5,
    "use_trailing": True,
    "trail_pct": 1.5,
    "use_breakeven": True,
    "be_trigger_pct": 0.35,
    "use_trailing_tp": True,
    "ttp_trigger_pct": 0.8,
    "ttp_callback_pct": 0.2,
    "use_ha_exit": True,
    "ha_exit_consecutive": 2,
    "block_sideways_buy": False,
    "min_rel_volume": 0.55,
    "use_ai_filter": True,
    "ai_min_prob": 0.52,
}

AI_STATE = {
    "weights": [0.0] * 6,
    "bias": 0.0,
    "samples_count": 0,
    "win_rate_train": 0.0,
    "trained_at": "--",
    "last_symbol": "--",
    "last_prob": None,
    "blocked_count": 0,
    "passed_count": 0,
    "_last_train_ts": 0.0,
}

# --- POMOCNÉ FUNKCIE, LOGOVANIE, TELEGRAM & OKX API ---

def safe_float(val, default: float = 0.0) -> float:
    try:
        if val is None or val == "":
            return default
        num = float(val)
        return default if (math.isnan(num) or math.isinf(num)) else num
    except Exception:
        return default

def get_sk_time_str() -> str:
    sk_tz = timezone(timedelta(hours=1))
    return datetime.now(sk_tz).strftime("%H:%M:%S")

def log_event(msg: str, persist: bool = False):
    ts = get_sk_time_str()
    line = f"[{ts}] {msg}"
    SYSTEM_LOGS.insert(0, line)
    if len(SYSTEM_LOGS) > 200:
        del SYSTEM_LOGS[200:]
    print(line, flush=True)
    if persist:
        save_persistent_data()

def send_telegram_msg(text: str, reply_markup: dict | None = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        pass

def _http_get_json(url: str, headers: dict | None = None) -> dict:
    try:
        req_headers = {"User-Agent": "APEX-AI-BOT/1.0"}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers, method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
            return json.loads(body)
        except Exception:
            return {"code": str(e.code), "msg": str(e), "data": []}
    except Exception as e:
        return {"code": "-1", "msg": str(e), "data": []}

def okx_public_request(path_and_query: str) -> dict:
    url = f"{OKX_PUBLIC_URL}{path_and_query}"
    return _http_get_json(url)

def okx_request(method: str, request_path: str, body: str = "") -> dict:
    if not OKX_API_KEY or not OKX_SECRET_KEY or not OKX_PASSPHRASE:
        return {"code": "-1", "msg": "Chýbajú OKX API kľúče", "data": []}
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        prehash = f"{ts}{method.upper()}{request_path}{body}"
        sig = hmac.new(
            OKX_SECRET_KEY.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        sign_b64 = base64.b64encode(sig).decode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "OK-ACCESS-KEY": OKX_API_KEY,
            "OK-ACCESS-SIGN": sign_b64,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        }
        if OKX_SIMULATED == "1":
            headers["x-simulated-trading"] = "1"

        url = f"{OKX_BASE_URL}{request_path}"
        data_bytes = body.encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method.upper())
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"code": "-1", "msg": str(e), "data": []}

def fetch_account_capital(ccy: str = "USDC") -> float:
    try:
        res = okx_request("GET", "/api/v5/account/balance")
        if res.get("code") == "0":
            for acc in res.get("data", []):
                for item in acc.get("details", []):
                    if item.get("ccy") == ccy:
                        avail = safe_float(item.get("availBal"), 0.0)
                        cash = safe_float(item.get("cashBal"), 0.0)
                        eq = safe_float(item.get("eq"), 0.0)
                        best_val = max(avail, cash, eq)
                        if best_val > 0:
                            return best_val
    except Exception as err:
        log_event(f"⚠️ Chyba pri načítaní zostatku: {err}")
    return 0.0

def get_instrument_rules(inst_id: str) -> tuple[float, float]:
    try:
        res = okx_public_request(f"/api/v5/public/instruments?instType=SPOT&instId={inst_id}")
        if res.get("code") == "0" and res.get("data"):
            d = res["data"][0]
            lot_sz = safe_float(d.get("lotSz"), 0.01)
            min_sz = safe_float(d.get("minSz"), 0.01)
            return max(1e-8, lot_sz), max(1e-8, min_sz)
    except Exception:
        pass
    return 0.01, 0.01

def format_amount(raw_amount: float, lot_sz: float, min_sz: float) -> str | None:
    if raw_amount < min_sz or lot_sz <= 0:
        return None
    steps = math.floor(raw_amount / lot_sz)
    amt = steps * lot_sz
    if amt < min_sz:
        return None
    lot_str = f"{lot_sz:.10f}".rstrip("0").rstrip(".")
    decimals = len(lot_str.split(".")[1]) if "." in lot_str else 0
    return f"{amt:.{decimals}f}"

def record_closed_trade(symbol: str, pnl_pct: float, pnl_usdc: float, reason: str):
    CLOSED_TRADES_HISTORY.insert(0, {
        "time": get_sk_time_str(),
        "symbol": symbol,
        "pnl_pct": round(pnl_pct, 2),
        "pnl_usdc": round(pnl_usdc, 2),
        "reason": reason,
    })
    if len(CLOSED_TRADES_HISTORY) > 200:
        del CLOSED_TRADES_HISTORY[200:]
    save_persistent_data()

def calculate_stats() -> dict:
    total = len(CLOSED_TRADES_HISTORY)
    wins = sum(1 for t in CLOSED_TRADES_HISTORY if safe_float(t.get("pnl_usdc")) > 0)
    losses = total - wins
    total_pnl = sum(safe_float(t.get("pnl_usdc")) for t in CLOSED_TRADES_HISTORY)
    win_rate = (wins / total * 100.0) if total > 0 else 0.0
    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 1),
    }

def save_ai_model():
    save_persistent_data()

def save_persistent_data():
    try:
        ai_clean = {k: v for k, v in AI_STATE.items() if not k.startswith("_")}
        payload = {
            "settings": SETTINGS,
            "ai_state": ai_clean,
            "history": CLOSED_TRADES_HISTORY[:100],
            "logs": SYSTEM_LOGS[:100],
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def load_persistent_data():
    global CLOSED_TRADES_HISTORY, SYSTEM_LOGS
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("settings"), dict):
            SETTINGS.update(data["settings"])
        if isinstance(data.get("ai_state"), dict):
            AI_STATE.update(data["ai_state"])
        if isinstance(data.get("history"), list):
            CLOSED_TRADES_HISTORY = data["history"][:100]
        if isinstance(data.get("logs"), list):
            SYSTEM_LOGS = data["logs"][:100]
    except Exception:
        pass

def get_market_regime_and_sideways(bar_tf: str = "15m") -> tuple[str, bool]:
    try:
        res = okx_public_request(f"/api/v5/market/candles?instId=BTC-USDC&bar={bar_tf}&limit=50")
        if res.get("code") != "0" or not res.get("data") or len(res["data"]) < 25:
            return "BULL", False
        candles = list(reversed(res["data"]))[:-1]
        closes = [safe_float(c[4]) for c in candles if safe_float(c[4]) > 0]
        highs = [safe_float(c[2]) for c in candles if safe_float(c[2]) > 0]
        lows = [safe_float(c[3]) for c in candles if safe_float(c[3]) > 0]
        if len(closes) < 20:
            return "BULL", False

        ema_fast = calc_ema_series(closes, 9)
        ema_slow = calc_ema_series(closes, 21)
        crosses = 0
        for i in range(1, len(closes)):
            prev_diff = ema_fast[i - 1] - ema_slow[i - 1]
            curr_diff = ema_fast[i] - ema_slow[i]
            if (prev_diff <= 0 < curr_diff) or (prev_diff >= 0 > curr_diff):
                crosses += 1

        recent_win = closes[-15:]
        rng_pct = ((max(highs[-15:]) - min(lows[-15:])) / recent_win[0] * 100.0) if recent_win[0] > 0 else 0.0
        is_sideways = (crosses >= 4) and (rng_pct < 2.2)
        if is_sideways:
            return "SIDEWAYS", True
        regime = "BULL" if ema_fast[-1] >= ema_slow[-1] else "BEAR"
        return regime, False
    except Exception:
        return "BULL", False

def check_btc_health() -> bool:
    if not SETTINGS.get("use_btc_filter", True):
        return True
    try:
        res = okx_public_request("/api/v5/market/candles?instId=BTC-USDC&bar=15m&limit=30")
        if res.get("code") != "0" or not res.get("data") or len(res["data"]) < 20:
            return True
        closes = [safe_float(c[4]) for c in reversed(res["data"]) if safe_float(c[4]) > 0]
        if len(closes) < 15:
            return True
        ema20 = calc_ema_series(closes, 20)[-1]
        return closes[-1] >= ema20 * 0.992
    except Exception:
        return True

def update_top_liquid_symbols():
    global TOP_LIQUID_SYMBOLS
    try:
        res = okx_public_request("/api/v5/market/tickers?instType=SPOT")
        if res.get("code") != "0" or not res.get("data"):
            return
        candidates = []
        for t in res["data"]:
            inst = t.get("instId", "")
            if inst.endswith("-USDC") and inst not in EXCLUDED_SYMBOLS:
                vol_ccy = safe_float(t.get("volCcy24h"), 0.0)
                if vol_ccy > 0:
                    candidates.append((vol_ccy, inst))
        candidates.sort(key=lambda x: x[0], reverse=True)
        top = [inst for _, inst in candidates[:20]]
        if len(top) >= 5:
            TOP_LIQUID_SYMBOLS = top
    except Exception as e:
        log_event(f"⚠️ Chyba pri aktualizácii top symbolov: {e}")

# --- 1:1 PINE SCRIPT MATEMATIKA ---

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

# --- AI MODEL PRE HODNOTENIE KVALITY BUY SIGNÁLOV ---

def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-min(x, 60.0))
        return 1.0 / (1.0 + z)
    z = math.exp(min(-x, 60.0))
    return z / (1.0 + z)

def extract_ai_features(idx: int, ha_closes, ha_opens, ha_highs, ha_lows, volumes, oscillator, signal_line, close_ema_9, predicted_next):
    cl = ha_closes[idx]
    ema9 = close_ema_9[idx] if close_ema_9[idx] and close_ema_9[idx] > 0 else cl
    dist_ema9_pct = max(-5.0, min(5.0, ((cl - ema9) / ema9) * 100.0)) / 2.5

    vol_win = volumes[max(0, idx - 14): idx + 1]
    avg_vol = (sum(vol_win) / len(vol_win)) if vol_win else 1.0
    rel_vol = max(0.0, min(4.0, (volumes[idx] / avg_vol) if avg_vol > 0 else 1.0)) / 2.0 - 0.5

    osc_win = [abs(x) for x in oscillator[max(0, idx - 19): idx + 1]]
    avg_osc = (sum(osc_win) / len(osc_win)) if osc_win else 1.0
    osc_ratio = max(-2.0, min(4.0, (oscillator[idx] / avg_osc) if avg_osc > 0 else 0.0)) / 2.0

    sig_diff = max(-2.0, min(2.0, ((oscillator[idx] - signal_line[idx]) / avg_osc) if avg_osc > 0 else 0.0))
    pred_diff = max(-2.0, min(2.0, ((predicted_next[idx] - oscillator[idx]) / avg_osc) if avg_osc > 0 else 0.0))

    rng = max(1e-9, ha_highs[idx] - ha_lows[idx])
    body_ratio = max(-1.0, min(1.0, (ha_closes[idx] - ha_opens[idx]) / rng))

    return [dist_ema9_pct, rel_vol, osc_ratio, sig_diff, pred_diff, body_ratio]

def train_ai_model_from_candles(symbol: str, bar_tf: str = "15m", force: bool = False) -> dict:
    global AI_STATE
    now_ts = time.time()
    if not force and (now_ts - AI_STATE.get("_last_train_ts", 0.0)) < 1800 and AI_STATE.get("samples_count", 0) >= 15:
        return {"ok": True, "samples": AI_STATE["samples_count"], "win_rate": AI_STATE["win_rate_train"]}

    try:
        res = okx_public_request(f"/api/v5/market/candles?instId={symbol}&bar={bar_tf}&limit=300")
        if res.get("code") != "0" or not res.get("data") or len(res["data"]) < 60:
            return {"ok": False, "msg": "Nedostatok historických sviečok z OKX."}

        candles = res["data"]
        candles.reverse()
        closed = candles[:-1]
        n = len(closed)
        if n < 50:
            return {"ok": False, "msg": "Príliš málo uzavretých sviečok."}

        ha_closes, ha_opens, ha_highs, ha_lows, volumes = [], [], [], [], []
        for i, c in enumerate(closed):
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

        fast_len = max(1, int(SETTINGS.get("fast_len", 5)))
        slow_len = max(1, int(SETTINGS.get("slow_len", 20)))
        signal_len = max(1, int(SETTINGS.get("signal_len", 9)))
        pred_look = max(1, min(10, int(SETTINGS.get("pred_look", 3))))
        mode_choice = str(SETTINGS.get("mode_choice", "ALL_GREEN")).upper()

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
        predicted_next = [(oscillator[i] + smooth_slope[i]) if smooth_slope[i] is not None else oscillator[i] for i in range(n)]

        target_gain_pct = max(0.5, float(SETTINGS.get("fixed_sl_pct", 1.5)) * 0.55)
        stop_loss_pct = float(SETTINGS.get("fixed_sl_pct", 1.5))
        horizon = 12

        dataset = []
        for i in range(25, n - horizon):
            osc_i = oscillator[i]
            sig_i = signal_line[i]
            pred_i = predicted_next[i]
            cl_i = ha_closes[i]
            ema9_i = close_ema_9[i]

            cond_vol_bull = (osc_i > 0) and (osc_i > sig_i) and (pred_i > osc_i)
            cond_vol_cross = (oscillator[i - 1] <= signal_line[i - 1]) and (osc_i > sig_i) and (osc_i > 0)
            if mode_choice == "BREAKOUT":
                is_sig = cond_vol_cross or (cond_vol_bull and (cl_i > ema9_i))
            else:
                is_sig = cond_vol_bull and (cond_vol_cross or (cl_i > ema9_i))

            if not is_sig or cl_i <= 0:
                continue

            label = 0
            for j in range(i + 1, i + 1 + horizon):
                chg_pct = ((ha_closes[j] - cl_i) / cl_i) * 100.0
                if chg_pct >= target_gain_pct:
                    label = 1
                    break
                if chg_pct <= -stop_loss_pct:
                    label = 0
                    break

            feats = extract_ai_features(i, ha_closes, ha_opens, ha_highs, ha_lows, volumes, oscillator, signal_line, close_ema_9, predicted_next)
            dataset.append((feats, label))

        if len(dataset) < 10:
            return {"ok": False, "msg": f"Nájdených len {len(dataset)} historických signálov."}

        weights = [0.0] * 6
        bias = 0.0
        lr = 0.15
        reg = 0.01

        for _ in range(80):
            for feats, y in dataset:
                z = bias + sum(weights[k] * feats[k] for k in range(6))
                pred_p = _sigmoid(z)
                err = pred_p - y
                for k in range(6):
                    weights[k] -= lr * (err * feats[k] + reg * weights[k])
                bias -= lr * err

        correct = 0
        wins_cnt = 0
        for feats, y in dataset:
            if y == 1:
                wins_cnt += 1
            z = bias + sum(weights[k] * feats[k] for k in range(6))
            pred_cls = 1 if _sigmoid(z) >= 0.5 else 0
            if pred_cls == y:
                correct += 1

        AI_STATE["weights"] = [round(w, 5) for w in weights]
        AI_STATE["bias"] = round(bias, 5)
        AI_STATE["samples_count"] = len(dataset)
        AI_STATE["win_rate_train"] = round((wins_cnt / len(dataset)) * 100.0, 1)
        AI_STATE["trained_at"] = get_sk_time_str()
        AI_STATE["last_symbol"] = f"{symbol} ({bar_tf.upper()})"
        AI_STATE["_last_train_ts"] = now_ts
        save_ai_model()

        acc_pct = round((correct / len(dataset)) * 100.0, 1)
        log_event(f"🧠 AI model natrénovaný na {symbol} [{bar_tf.upper()}]: {len(dataset)} vzoriek | Historický úspech: {AI_STATE['win_rate_train']}% | Presnosť: {acc_pct}%.")
        return {"ok": True, "samples": len(dataset), "win_rate": AI_STATE["win_rate_train"], "accuracy": acc_pct}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

def predict_ai_probability(feats: list[float]) -> float | None:
    if AI_STATE.get("samples_count", 0) < 10:
        return None
    w = AI_STATE.get("weights", [0.0] * 6)
    b = float(AI_STATE.get("bias", 0.0))
    z = b + sum(w[k] * feats[k] for k in range(min(len(w), len(feats))))
    return round(_sigmoid(z), 3)

# --- VYHODNOCOVANIE PINE SIGNÁLOV + KVALITA + AI ---

def evaluate_pine_strategy(symbol: str, bar_tf: str = "15m"):
    global MARKET_SCANNER_CACHE, ACTIVE_TRADES, AI_STATE
    coin_key = symbol.split("-")[0]

    try:
        url = f"/api/v5/market/candles?instId={symbol}&bar={bar_tf}&limit=120"
        res = okx_public_request(url)
        if res.get("code") != "0" or not res.get("data") or len(res["data"]) < 30:
            MARKET_SCANNER_CACHE[coin_key] = {
                "symbol": symbol, "price": 0.0, "oscillator": 0.0,
                "momentum": "SLABÉ", "impulse": "ČAKÁ", "trade_status": "---",
                "be_status": "VYPNUTÝ", "ttp_status": "VYPNUTÝ", "ts_status": "VYPNUTÝ",
                "ai_score": "--", "exec_signal": "ČAKAJ", "signal": "⚠️ CHYBA DÁT", "is_green": False
            }
            return {"buy": False, "sell": False, "sell_reason": "", "ai_prob": None, "block_reason": ""}

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

        feats = extract_ai_features(n - 1, ha_closes, ha_opens, ha_highs, ha_lows, volumes, oscillator, signal_line, close_ema_9, predicted_next)
        ai_prob = predict_ai_probability(feats)
        if ai_prob is not None:
            AI_STATE["last_prob"] = ai_prob

        final_buy = last_bar_buy
        block_reason = ""

        if last_bar_buy and not is_in_trade:
            if bool(SETTINGS.get("block_sideways_buy", False)) and IS_SIDEWAYS_MARKET:
                final_buy = False
                block_reason = "Bočný trh (SIDEWAYS)"

            min_rel_vol = float(SETTINGS.get("min_rel_volume", 0.0))
            if final_buy and min_rel_vol > 0:
                vol_win = volumes[max(0, n - 15): n - 1]
                avg_v = (sum(vol_win) / len(vol_win)) if vol_win else 0.0
                rel_v = (volumes[-1] / avg_v) if avg_v > 0 else 1.0
                if rel_v < min_rel_vol:
                    final_buy = False
                    block_reason = f"Nízky objem ({rel_v:.2f}x < {min_rel_vol:.2f}x)"

            if final_buy and bool(SETTINGS.get("use_ai_filter", True)):
                if AI_STATE.get("samples_count", 0) < 10:
                    train_ai_model_from_candles(symbol, bar_tf=bar_tf, force=False)
                    feats = extract_ai_features(n - 1, ha_closes, ha_opens, ha_highs, ha_lows, volumes, oscillator, signal_line, close_ema_9, predicted_next)
                    ai_prob = predict_ai_probability(feats)
                    if ai_prob is not None:
                        AI_STATE["last_prob"] = ai_prob

                min_prob = float(SETTINGS.get("ai_min_prob", 0.52))
                if ai_prob is not None and ai_prob < min_prob:
                    final_buy = False
                    block_reason = f"AI skóre {ai_prob*100:.1f}% < {min_prob*100:.1f}%"
                    AI_STATE["blocked_count"] = AI_STATE.get("blocked_count", 0) + 1
                else:
                    AI_STATE["passed_count"] = AI_STATE.get("passed_count", 0) + 1

        momentum_str = "SILNÉ" if last_cond_volume_bullish else "SLABÉ"
        impuls_active = last_buy_trigger or is_in_trade
        impulse_str = "AKTÍVNY (V TRHU)" if impuls_active else "ČAKÁ"

        trade_status_txt = f"{ACTIVE_TRADES[coin_key]['entry_price']:.4f}" if is_in_trade else "---"
        be_status_txt = f"{ACTIVE_TRADES[coin_key]['entry_price']:.4f}" if (is_in_trade and use_breakeven and breakeven_active) else ("ČAKÁ" if (is_in_trade and use_breakeven) else "VYPNUTÝ")
        ttp_status_txt = f"{trailing_tp_stop_level:.4f}" if (is_in_trade and use_trailing_tp and trailing_tp_active and trailing_tp_stop_level) else ("V TRHU" if (is_in_trade and use_trailing_tp) else "VYPNUTÝ")
        ts_status_txt = f"{ACTIVE_TRADES[coin_key]['fixed_sl_price']:.4f}" if is_in_trade else f"-{SETTINGS['fixed_sl_pct']}%"
        ai_score_txt = f"{ai_prob*100:.1f}%" if ai_prob is not None else ("UČÍ SA" if SETTINGS.get("use_ai_filter", True) else "OFF")

        if final_buy and not is_in_trade:
            exec_signal_str = "NAKÚPIŤ (AI BUY)"
        elif last_bar_buy and not final_buy and not is_in_trade:
            exec_signal_str = f"AI BLOK ({block_reason})"
        elif last_bar_sell and is_in_trade:
            exec_signal_str = "PREDAŤ (SELL)"
        elif is_in_trade:
            exec_signal_str = "DRŽAŤ"
        else:
            exec_signal_str = "ČAKAJ"

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
            "ai_score": ai_score_txt,
            "exec_signal": exec_signal_str,
            "signal": "🟢 BUY" if final_buy else ("🟡 AI BLOK" if last_bar_buy else ("🔴 SELL" if last_bar_sell else ("🟡 BULLISH" if last_cond_volume_bullish else "⚪ ČAKÁ"))),
            "is_green": final_buy
        }

        return {"buy": final_buy, "sell": last_bar_sell, "sell_reason": last_bar_sell_reason, "ai_prob": ai_prob, "block_reason": block_reason}
    except Exception:
        MARKET_SCANNER_CACHE[coin_key] = {
            "symbol": symbol, "price": 0.0, "oscillator": 0.0,
            "momentum": "SLABÉ", "impulse": "ČAKÁ", "trade_status": "---",
            "be_status": "VYPNUTÝ", "ttp_status": "VYPNUTÝ", "ts_status": "VYPNUTÝ",
            "ai_score": "--", "exec_signal": "ČAKAJ", "signal": "⚠️ VÝNIMKA", "is_green": False
        }
        return {"buy": False, "sell": False, "sell_reason": "", "ai_prob": None, "block_reason": ""}

# --- AUTONÓMNA SKENOVACIA SLUČKA ---

async def autonomous_scanner_loop():
    global IS_SIDEWAYS_MARKET, MARKET_REGIME, LAST_SIDEWAYS_ALERT_TS
    await asyncio.sleep(3)
    log_event(f"🤖 Autonómny Pine Script + AI skener spustený pre {len(TOP_LIQUID_SYMBOLS)} symbolov!")
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
                            prob_txt = f" (AI: {sig['ai_prob']*100:.1f}%)" if sig.get("ai_prob") is not None else ""
                            log_event(f"🚀 PINE SCRIPT + AI 'BUY' SIGNÁL PRE {symbol}{prob_txt}!")
                            await execute_autonomous_buy(symbol, ai_prob=sig.get("ai_prob"))
                        elif sig.get("block_reason"):
                            log_event(f"🛡️ Signál {symbol} [{CURRENT_TIMEFRAME}] pozastavený: {sig['block_reason']}")

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

async def execute_autonomous_buy(okx_symbol: str, ai_prob: float | None = None):
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
            log_event(f"❌ Nedostatok prostriedkov na účte: {free_capital:.2f}")
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
            err_msg = order_res.get("msg", "Neznáma chyba OKX")
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
            "ai_prob": ai_prob
        }

        tp_info = f" | Fixný TP: +{fixed_tp_pct}% ({fixed_tp_price:.4f})" if use_fixed_tp else ""
        ai_info = f" | AI: {ai_prob*100:.1f}%" if ai_prob is not None else ""
        log_event(f"🛒 NÁKUP (Pine + AI BUY) [{CURRENT_TIMEFRAME}]: {okx_symbol} za {current_price:.4f} USDC | Investícia: {actual_spent_usdc:.2f} USDC | Fixný SL: -{fixed_sl_pct}% ({fixed_sl_price:.4f}){tp_info}{ai_info}", persist=True)

        inline_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "🛑 ZATVORIŤ VŠETKO", "callback_data": "sell_now"},
                    {"text": "⏸️ PAUZA BOTA", "callback_data": "toggle_bot"}
                ]
            ]
        }
        send_telegram_msg(
            f"🟢 <b>PINE + AI BUY NÁKUP [TF: {CURRENT_TIMEFRAME}]</b>\n"
            f"<b>Pár:</b> {okx_symbol}\n"
            f"<b>Vstup:</b> {current_price:.4f}\n"
            f"<b>Investícia:</b> {actual_spent_usdc:.2f} USDC\n"
            + (f"🧠 <b>AI skóre:</b> {ai_prob*100:.1f}%\n" if ai_prob is not None else "")
            + f"🛡️ <b>Fixný Stop-Loss:</b> -{fixed_sl_pct}% ({fixed_sl_price:.4f})\n"
            + (f"🎯 <b>Fixný Take Profit:</b> +{fixed_tp_pct}% ({fixed_tp_price:.4f})\n" if use_fixed_tp else "")
            + f"📤 <b>Výstup:</b> Pine SELL signál alebo Fixný SL" + (" / Fixný TP" if use_fixed_tp else ""),
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
            curr_p = safe_float(ticker_res["data"][0]["last"]) if ticker_res.get("code") == "0" and ticker_res.get("data") else trade.get("current_price", trade["entry_price"])

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
            LAST_TRADE_TIME[coin] = datetime.now().timestamp()

            emoji = "🎯" if pnl_usdc >= 0 else "🚨"
            log_event(f"{emoji} Uzavretá pozícia ({reason}) pre {symbol} | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)", persist=True)
            send_telegram_msg(f"{emoji} <b>UZAVRETÁ POZÍCIA ({reason})</b>\n<b>Pár:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
        except Exception as e:
            log_event(f"⚠️ Chyba pri zatváraní {symbol}: {e}")
        finally:
            if coin in ACTIVE_TRADES:
                del ACTIVE_TRADES[coin]

# --- MONITOR RIZÍK ---

async def fixed_stoploss_monitor_loop():
    global ACTIVE_TRADES, CURRENT_STAGE
    await asyncio.sleep(3)
    while True:
        if not ACTIVE_TRADES:
            if IS_BOT_ACTIVE:
                max_p = SETTINGS.get("max_parallel", 1)
                sl_p = SETTINGS.get("fixed_sl_pct", 1.5)
                tp_txt = f" | Fixný TP: +{SETTINGS.get('fixed_tp_pct', 1.5)}%" if SETTINGS.get("use_fixed_tp", False) else ""
                ai_txt = f" | AI ≥ {SETTINGS.get('ai_min_prob', 0.52)*100:.0f}%" if SETTINGS.get("use_ai_filter", True) else ""
                CURRENT_STAGE = f"STAV: Bot aktívny (Max {max_p}x | Fixný SL: -{sl_p}%{tp_txt}{ai_txt}). Čakám na BUY/SELL..."
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
    update_top_liquid_symbols()
    if SETTINGS.get("use_ai_filter", True) and AI_STATE.get("samples_count", 0) < 10:
        seed_sym = TOP_LIQUID_SYMBOLS[0] if TOP_LIQUID_SYMBOLS else "SOL-USDC"
        train_ai_model_from_candles(seed_sym, bar_tf=CURRENT_TIMEFRAME.lower(), force=True)
    log_event(f"🚀 Autonómny APEX MATRIX_SCALPER_v1_BOT (Pine BUY & SELL + AI Filter + Fixný SL -{SETTINGS['fixed_sl_pct']}%) spustený.", persist=True)
    monitor_task = asyncio.create_task(fixed_stoploss_monitor_loop())
    scanner_task = asyncio.create_task(autonomous_scanner_loop())
    updater_task = asyncio.create_task(top_symbols_updater_loop())
    send_telegram_msg(f"🚀 APEX MATRIX_SCALPER_v1_BOT (Pine BUY & SELL + AI Filter + Fixný SL -{SETTINGS['fixed_sl_pct']}%) pripravený!")
    yield
    monitor_task.cancel()
    scanner_task.cancel()
    updater_task.cancel()

app = FastAPI(lifespan=lifespan)

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
        "block_sideways_buy": SETTINGS["block_sideways_buy"],
        "min_rel_volume": SETTINGS["min_rel_volume"],
        "use_ai_filter": SETTINGS["use_ai_filter"],
        "ai_min_prob": SETTINGS["ai_min_prob"],
    }

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
    log_event(f"⚙️ Prepínač bota: {status_str.upper()}", persist=True)
    send_telegram_msg(f"⚙️ Zmena stavu: Bot je teraz {status_str.upper()}.")
    return JSONResponse({"status": "success", "is_bot_active": IS_BOT_ACTIVE})

@app.post("/api/toggle-btc-filter")
async def toggle_btc_filter():
    SETTINGS["use_btc_filter"] = not SETTINGS.get("use_btc_filter", True)
    status_str = "zapnutý (aktívny)" if SETTINGS["use_btc_filter"] else "vypnutý (deaktivovaný)"
    log_event(f"⚙️ BTC Trend Guard filter: {status_str.upper()}", persist=True)
    send_telegram_msg(f"⚙️ Zmena stavu: BTC Trend Guard filter je teraz {status_str.upper()}.")
    return JSONResponse({"status": "success", "use_btc_filter": SETTINGS["use_btc_filter"]})

@app.post("/api/toggle-compounding")
async def toggle_compounding():
    SETTINGS["compounding_enabled"] = not SETTINGS.get("compounding_enabled", False)
    status_str = "ZAPNUTÉ (Zložené úročenie)" if SETTINGS["compounding_enabled"] else "VYPNUTÉ (Pevný rozpočet)"
    log_event(f"⚙️ Automatické reinvestovanie (COMPOUNDING): {status_str.upper()}", persist=True)
    send_telegram_msg(f"⚙️ Zmena stavu: Auto-Compounding je teraz {status_str}.")
    return JSONResponse({"status": "success", "compounding_enabled": SETTINGS["compounding_enabled"]})

@app.post("/api/set-budget/{amount}")
async def set_budget(amount: int):
    if amount in [500, 1000, 3000, 5000, 10000]:
        SETTINGS["max_budget_cap"] = float(amount)
        log_event(f"💰 Maximálny rozpočet (strop) zmenený na: {amount} USDC", persist=True)
        send_telegram_msg(f"💰 Zmena rozpočtu: Maximálny strop pre obchod je teraz {amount} USDC.")
        return JSONResponse({"status": "success", "max_budget_cap": amount})
    return JSONResponse({"status": "error", "message": "Neplatná hodnota rozpočtu."})

@app.post("/api/set-parallel/{count}")
async def set_parallel(count: int):
    if count in [1, 2, 3, 4]:
        SETTINGS["max_parallel"] = count
        log_event(f"⚙️ Maximálny počet paralelných obchodov zmenený na: {count}", persist=True)
        send_telegram_msg(f"⚙️ Zmena nastavenia: Bot môže teraz otvoriť naraz max. {count} obchody.")
        return JSONResponse({"status": "success", "max_parallel": count})
    return JSONResponse({"status": "error", "message": "Neplatná hodnota."})

@app.post("/api/set-sl/{sl_val}")
async def set_sl(sl_val: float):
    if 0.1 <= sl_val <= 50.0:
        SETTINGS["fixed_sl_pct"] = float(sl_val)
        log_event(f"🛡️ Fixný Stop-Loss zmenený na: -{sl_val}%", persist=True)
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
        if SETTINGS.get("use_ai_filter", True):
            seed_sym = TOP_LIQUID_SYMBOLS[0] if TOP_LIQUID_SYMBOLS else "SOL-USDC"
            train_ai_model_from_candles(seed_sym, bar_tf=tf_lower, force=True)
        log_event(f"⏱️ Časový rámec (TF) zmenený na: {CURRENT_TIMEFRAME}", persist=True)
        return JSONResponse({"status": "success", "tf": CURRENT_TIMEFRAME})
    return JSONResponse({"status": "error", "message": "Neplatný časový rámec."})

@app.post("/api/train-ai")
async def api_train_ai():
    seed_sym = TOP_LIQUID_SYMBOLS[0] if TOP_LIQUID_SYMBOLS else "SOL-USDC"
    res = train_ai_model_from_candles(seed_sym, bar_tf=CURRENT_TIMEFRAME.lower(), force=True)
    if res.get("ok"):
        MARKET_SCANNER_CACHE.clear()
        return JSONResponse({"status": "success", "ai_state": AI_STATE, "details": res})
    return JSONResponse({"status": "error", "message": res.get("msg", "Chyba trénovania AI.")})

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

        SETTINGS["block_sideways_buy"] = bool(payload.get("block_sideways_buy", SETTINGS["block_sideways_buy"]))
        SETTINGS["min_rel_volume"] = max(0.0, min(5.0, float(payload.get("min_rel_volume", SETTINGS["min_rel_volume"]))))
        SETTINGS["use_ai_filter"] = bool(payload.get("use_ai_filter", SETTINGS["use_ai_filter"]))
        SETTINGS["ai_min_prob"] = max(0.25, min(0.90, float(payload.get("ai_min_prob", SETTINGS["ai_min_prob"]))))

        MARKET_SCANNER_CACHE.clear()
        log_event(
            f"🎛️ Nastavenia aktualizované: Režim={mode_val}, AI={SETTINGS['use_ai_filter']}(min={SETTINGS['ai_min_prob']*100:.0f}%), "
            f"SidewaysBlok={SETTINGS['block_sideways_buy']}, MinVol={SETTINGS['min_rel_volume']}x, FixSL=-{SETTINGS['fixed_sl_pct']}%",
            persist=True,
        )
        return JSONResponse({"status": "success", "pine_settings": get_pine_settings_dict()})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)})

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
                err_msg = order_res.get("msg", "Neznáma chyba OKX")
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
            log_event(f"🛒 Manuálny nákup [{CURRENT_TIMEFRAME}]: {okx_symbol} za {current_price:.4f} | Investícia: {actual_spent_usdc:.2f}$ | Fixný SL: -{fixed_sl_pct}%{tp_info}", persist=True)
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

# --- HTML ŠABLÓNA (ČIERNE POZADIE) ---

HTML_TEMPLATE_CONTENT = """<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>APEX MATRIX_SCALPER_v1_BOT + AI</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #000000;
            --card-bg: #08090c;
            --border-color: #1a1d24;
            --border-hover: #2b303c;
            --text-main: #f3f4f6;
            --text-muted: #7b8598;
            --accent-white: #ffffff;
            --green: #10b981;
            --red: #ef4444;
            --yellow: #f59e0b;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'JetBrains Mono', monospace; }
        html, body { background-color: #000000; color: var(--text-main); padding: 16px; min-height: 100vh; overflow-y: auto; display: flex; flex-direction: column; }
        .container { max-width: 1350px; width: 100%; margin: 0 auto; display: flex; flex-direction: column; gap: 14px; padding-bottom: 30px; }
        .top-bar { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); padding-bottom: 12px; flex-wrap: wrap; gap: 12px; }
        .brand h1 { font-size: 1.05rem; font-weight: 700; color: var(--accent-white); letter-spacing: -0.5px; }
        .brand p { font-size: 0.7rem; color: var(--text-muted); margin-top: 3px; text-transform: uppercase; letter-spacing: 0.8px; display: flex; gap: 12px; flex-wrap: wrap; }
        .balance-tag { color: var(--yellow); font-weight: 700; }
        .actions { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
        .btn { background: #050608; border: 1px solid var(--border-hover); color: var(--text-main); padding: 5px 10px; font-size: 0.7rem; font-weight: 600; cursor: pointer; border-radius: 4px; transition: all 0.15s ease; text-transform: uppercase; letter-spacing: 0.5px; }
        .btn:hover { border-color: var(--text-muted); background: #0e1015; }
        .btn-on { border-color: var(--green); color: var(--green); }
        .btn-off { border-color: var(--text-muted); color: var(--text-muted); }
        .btn-yellow { border-color: var(--yellow) !important; color: var(--yellow) !important; background: rgba(245, 158, 11, 0.12) !important; font-weight: 700 !important; }
        .btn-sell { background: var(--accent-white); color: #000000; font-weight: 700; border-color: var(--accent-white); }
        .btn-sell:hover { background: #e4e4e7; }
        .btn-sell:disabled { opacity: 0.25; cursor: not-allowed; background: #050608; color: var(--text-muted); border-color: var(--border-color); }
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
        .custom-buy-box { display: flex; align-items: center; gap: 4px; border: 1px solid var(--border-hover); padding: 2px 6px; border-radius: 4px; background: #050608; }
        .custom-buy-input { background: transparent; border: none; color: var(--text-main); font-size: 0.7rem; width: 55px; outline: none; text-transform: uppercase; font-weight: 600; }

        .pine-panel { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 5px; padding: 12px 14px; display: flex; flex-direction: column; gap: 12px; }
        .pine-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
        @media (max-width: 1100px) { .pine-grid { grid-template-columns: repeat(2, 1fr); } .stats-grid { grid-template-columns: repeat(2, 1fr); } }
        @media (max-width: 650px) { .pine-grid { grid-template-columns: 1fr; } .stats-grid { grid-template-columns: 1fr; } }
        .pine-group { background: #040507; border: 1px solid var(--border-color); border-radius: 4px; padding: 10px; display: flex; flex-direction: column; gap: 8px; }
        .pine-group-title { font-size: 0.65rem; font-weight: 700; color: var(--yellow); text-transform: uppercase; letter-spacing: 0.6px; border-bottom: 1px solid var(--border-color); padding-bottom: 5px; }
        .pine-row { display: flex; justify-content: space-between; align-items: center; gap: 8px; font-size: 0.68rem; color: var(--text-main); }
        .pine-row label { color: var(--text-muted); flex: 1; }
        .pine-input, .pine-select { background: #000000; border: 1px solid var(--border-hover); color: var(--text-main); border-radius: 3px; padding: 3px 6px; font-size: 0.68rem; outline: none; }
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
                <h1>APEX MATRIX_SCALPER_v1_BOT + AI</h1>
                <p>
                    <span id="header-pair">Top USDC · OKX Demo</span>
                    <span>Zostatok: <span id="header-balance" class="balance-tag">-- USDC</span></span>
                    <span>Aktívny TF: <span id="header-tf" class="balance-tag">{{ CURRENT_TIMEFRAME }}</span></span>
                    <span>Fixný SL: <span id="header-sl" class="balance-tag">-{{ SETTINGS.fixed_sl_pct }}%</span></span>
                    <span>AI Model: <span id="header-ai" class="balance-tag">Načítavam...</span></span>
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
                <span>Nastavenia Pine Script Indikátora + AI Filter Kvality</span>
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
                        <div class="pine-group-title">AI Predikcia & Filter Kvality</div>
                        <div class="pine-row">
                            <label for="pine-use-ai">Zapnúť AI filter BUY</label>
                            <input type="checkbox" id="pine-use-ai" class="pine-checkbox" {% if SETTINGS.use_ai_filter %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="pine-ai-prob">Min. AI pravdepodobnosť</label>
                            <input type="number" id="pine-ai-prob" class="pine-input" min="0.25" max="0.90" step="0.01" value="{{ SETTINGS.ai_min_prob }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-min-rel-vol">Min. relatívny objem (x)</label>
                            <input type="number" id="pine-min-rel-vol" class="pine-input" min="0" max="5" step="0.05" value="{{ SETTINGS.min_rel_volume }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-block-sideways">Blokovať bočný trh (SIDEWAYS)</label>
                            <input type="checkbox" id="pine-block-sideways" class="pine-checkbox" {% if SETTINGS.block_sideways_buy %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="pine-pivot-len">Pivot dĺžka (Fib)</label>
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
                    </div>

                    <div class="pine-group">
                        <div class="pine-group-title">3.1 & 3.2. TTP & Heikin-Ashi Exit</div>
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
                    </div>
                </div>
                <div class="pine-footer">
                    <span id="pine-save-status" class="pine-save-status">Pripravené – zmeny sa okamžite premietnu do BUY / SELL skenera.</span>
                    <div style="display: flex; gap: 6px; flex-wrap: wrap;">
                        <button class="btn btn-yellow" onclick="trainAiNow()">🧠 TRÉNOVAŤ AI MODEL</button>
                        <button class="btn" onclick="resetPineDefaults()">OBNOVIŤ ZÁKLAD</button>
                        <button class="btn btn-on" onclick="savePineSettings()">ULOŽIŤ NASTAVENIA</button>
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
                        <tr><td colspan="7" style="text-align: center; color: var(--text-muted);">Skenujem trh pre Pine Script + AI BUY signály...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div>
            <div class="section-title">Live Market Scanner Status (20 Top Liquid USDC Pairs - Pine + AI Sync)</div>
            <div class="terminal-card scrollable-card" style="max-height: 260px;">
                <table>
                    <thead>
                        <tr>
                            <th>Token / Pár</th>
                            <th>Cena</th>
                            <th>1. Objem. Momentum</th>
                            <th>2. Nákupný Impulz</th>
                            <th>3. Stav Obchodu</th>
                            <th>4. AI Skóre</th>
                            <th>5. Fixný SL</th>
                            <th>6. Exekučný Pokyn</th>
                        </tr>
                    </thead>
                    <tbody id="scanner-tbody">
                        <tr><td colspan="8" style="text-align: center; color: var(--text-muted);">Načítavam stav skenera...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div>
            <div class="section-title">System Execution Logs</div>
            <div class="logs-box" id="logs-container">
                <div class="log-line">[System Initialized] Pine BUY & SELL + AI Filter + Fixný SL pripravený.</div>
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

        async function trainAiNow() {
            let statusEl = document.getElementById('pine-save-status');
            statusEl.style.color = 'var(--yellow)';
            statusEl.innerText = 'Trénujem AI model na historických sviečkach z OKX...';
            let res = await fetch('/api/train-ai', {method: 'POST'});
            let data = await res.json();
            if (data.status === 'success') {
                statusEl.style.color = 'var(--green)';
                statusEl.innerText = `AI natrénované (${data.details.samples} vzoriek, presnosť ${data.details.accuracy}%).`;
                updateState();
            } else {
                statusEl.style.color = 'var(--red)';
                statusEl.innerText = 'AI chyba: ' + data.message;
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
            document.getElementById('pine-use-ai').checked = !!ps.use_ai_filter;
            document.getElementById('pine-ai-prob').value = ps.ai_min_prob;
            document.getElementById('pine-min-rel-vol').value = ps.min_rel_volume;
            document.getElementById('pine-block-sideways').checked = !!ps.block_sideways_buy;
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
                use_ai_filter: document.getElementById('pine-use-ai').checked,
                ai_min_prob: parseFloat(document.getElementById('pine-ai-prob').value),
                min_rel_volume: parseFloat(document.getElementById('pine-min-rel-vol').value),
                block_sideways_buy: document.getElementById('pine-block-sideways').checked,
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
                ha_exit_consecutive: parseInt(document.getElementById('pine-ha-consec').value, 10)
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
                use_ai_filter: true,
                ai_min_prob: 0.52,
                min_rel_volume: 0.55,
                block_sideways_buy: false,
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

                if (data.ai_state) {
                    const sc = data.ai_state.samples_count || 0;
                    const wr = data.ai_state.win_rate_train || 0;
                    document.getElementById('header-ai').innerText = sc > 0 ? `${sc} vz. (${wr}%)` : "ČAKÁ";
                }

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
                    activeTbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">Skenujem trh pre Pine Script + AI BUY signály...</td></tr>`;
                    sellBtn.disabled = true;
                }

                let scannerTbody = document.getElementById('scanner-tbody');
                let scannerData = data.scanner_cache || {};
                if (Object.keys(scannerData).length > 0) {
                    let sHtml = "";
                    for (let [coin, info] of Object.entries(scannerData)) {
                        let momClass = info.momentum === "SILNÉ" ? "text-green" : "text-red";
                        let impClass = info.impulse.includes("AKTÍVNY") ? "text-green" : "text-yellow";
                        let execClass = info.exec_signal.includes("NAKÚPIŤ") ? "text-green" : (info.exec_signal.includes("PREDAŤ") || info.exec_signal.includes("AI BLOK") ? "text-red" : "text-yellow");

                        sHtml += `<tr>
                            <td><b>${info.symbol}</b></td>
                            <td>${info.price ? info.price.toFixed(4) : '--'}</td>
                            <td class="${momClass}">${info.momentum}</td>
                            <td class="${impClass}">${info.impulse}</td>
                            <td class="text-yellow">${info.trade_status}</td>
                            <td>${info.ai_score || '--'}</td>
                            <td>${info.ts_status}</td>
                            <td class="${execClass}">${info.exec_signal}</td>
                        </tr>`;
                    }
                    scannerTbody.innerHTML = sHtml;
                } else {
                    scannerTbody.innerHTML = `<tr><td colspan="8" style="text-align: center; color: var(--text-muted);">Prebieha úvodné skenovanie trhu...</td></tr>`;
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
        balance=balance,
    )

@app.get("/api/state")
async def get_state():
    balance = fetch_account_capital("USDC")
    stats = calculate_stats()
    ai_public = {k: v for k, v in AI_STATE.items() if not k.startswith("_")}
    return JSONResponse({
        "balance": balance,
        "is_bot_active": IS_BOT_ACTIVE,
        "current_stage": CURRENT_STAGE,
        "current_tf": CURRENT_TIMEFRAME,
        "is_sideways_market": IS_SIDEWAYS_MARKET,
        "market_regime": MARKET_REGIME,
        "fixed_sl_pct": SETTINGS.get("fixed_sl_pct", 1.5),
        "pine_settings": get_pine_settings_dict(),
        "ai_state": ai_public,
        "stats": stats,
        "active_trades": ACTIVE_TRADES,
        "scanner_cache": MARKET_SCANNER_CACHE,
        "history": CLOSED_TRADES_HISTORY[:20],
        "logs": SYSTEM_LOGS,
    })

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
