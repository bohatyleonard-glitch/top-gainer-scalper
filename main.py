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

# --- OKX KLUCE & BEZPECNOST ---

OKX_API_KEY = os.getenv("OKX_API_KEY", "54ebe9fe-1fd0-443d-8f76-0cd1a3439ddb").strip()
OKX_SECRET = os.getenv("OKX_SECRET", "FBD5C8332D88E3ED5662470CA9C3E813").strip()
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "Novyzeland111/").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "moje_tajne_heslo_123").strip()

# --- TELEGRAM NOTIFIKACIE & INTERAKTIVNY BOT ---

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9158932827:AAHvZT_gQrVH6dk54BRx8_mxTFtkBud0tUI").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "9130979784").strip()

# Sukromne poziadavky (zostatok, nakup, predaj) -> OKX Demo EEA (oddelene od OKX_BASE_URL)
_env_demo_url = os.getenv("OKX_DEMO_BASE_URL", os.getenv("OKX_BASE_URL", "https://eea.okx.com")).strip().rstrip("/")
OKX_DEMO_BASE_URL = "https://eea.okx.com" if (not _env_demo_url or "www.okx.com" in _env_demo_url) else _env_demo_url
# Verejne trhove data (grafy/sviecky, tickery, instrumenty) -> OKX Live
OKX_PUBLIC_URL = os.getenv("OKX_PUBLIC_URL", "https://www.okx.com").strip().rstrip("/")
OKX_SIMULATED = os.getenv("OKX_SIMULATED", "1").strip()  # "1" pre OKX Demo Trading

STATE_FILE = os.getenv("BOT_STATE_FILE", "/var/data/apex_ai_bot_state.json")
LOG_RETENTION_SEC = 24 * 3600


def _resolve_ai_memory_path() -> str:
    custom_path = os.getenv("AI_MEMORY_FILE", "").strip()
    if custom_path:
        return custom_path
    state_dir = os.path.dirname(STATE_FILE)
    if state_dir and os.path.isdir(state_dir):
        return os.path.join(state_dir, "ai_memory.json")
    if os.path.isdir("/var/data"):
        return "/var/data/ai_memory.json"
    return "ai_memory.json"


AI_MEMORY_FILE = _resolve_ai_memory_path()
ALLOWED_TIMEFRAMES = ["5m", "8m", "15m", "30m", "45m"]

EXCLUDED_SYMBOLS = {
    "BTC-USDC", "ETH-USDC",
    "USDT-USDC", "DAI-USDC", "FDUSD-USDC",
    "ZEC-USDC", "ENA-USDC", "LIT-USDC", "CT-USDC",
    "XMSTR-USDC", "XSOXL-USDC", "XANT-USDC",
}

# --- GLOBALNY STAV BOTA ---
IS_BOT_ACTIVE = True
CURRENT_TIMEFRAME = "15M"
CURRENT_STAGE = "STAV: Inicializujem bota..."
MARKET_REGIME = "BULL"
IS_SIDEWAYS_MARKET = False
LAST_SIDEWAYS_ALERT_TS = 0.0
SIDEWAYS_ALERT_COOLDOWN_SEC = 1800
LAST_BALANCE_ERROR_LOG_TS = 0.0
LAST_BLOCK_LOG_TS: dict[str, float] = {}
ACTIVE_BALANCE_CCY = "USDC"

TOP_LIQUID_SYMBOLS = [
    "SOL-USDC", "SUI-USDC", "XRP-USDC", "TAO-USDC",
    "DOGE-USDC", "ADA-USDC", "AVAX-USDC", "LINK-USDC", "TON-USDC",
    "NEAR-USDC", "APT-USDC", "TRX-USDC", "LTC-USDC", "PEPE-USDC",
    "AAVE-USDC", "UNI-USDC", "POL-USDC", "RENDER-USDC", "TIA-USDC",
    "PUMP-USDC"
]

ACTIVE_TRADES: dict[str, dict] = {}
LAST_TRADE_TIME: dict[str, float] = {}
CLOSED_TRADES_HISTORY: list[dict] = []
AI_LEARNED_TRADES: list[dict] = []
MARKET_SCANNER_CACHE: dict[str, dict] = {}
SYSTEM_LOG_ENTRIES: list[dict] = []
SYSTEM_LOGS: list[str] = []
trade_lock = asyncio.Lock()

# Predvolene nastavenia: zachovavaju logiku 1. kodu + Pine Script,
# cielenou vlnou ~0.8-1%+ cez Trailing TP a AI ucenim bez predcasneho blokovania na starte.
SETTINGS = {
    "max_parallel": 2,
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
    "fixed_sl_pct": 2.0,
    "use_fixed_tp": False,
    "fixed_tp_pct": 0.9,
    "use_trailing": True,
    "trail_pct": 1.2,
    "use_breakeven": True,
    "be_trigger_pct": 0.6,
    "use_trailing_tp": True,
    "ttp_trigger_pct": 0.85,
    "ttp_callback_pct": 0.3,
    "use_ha_exit": False,
    "ha_exit_consecutive": 3,
    "block_sideways_buy": False,
    "min_rel_volume": 0.8,
    "use_anti_chase": False,
    "max_candle_pump_pct": 1.5,
    "max_ema9_dist_pct": 1.4,
    "use_ai_filter": True,
    "ai_min_prob": 0.35,
}

AI_STATE = {
    "weights": [0.0] * 6,
    "bias": 0.0,
    "samples_count": 0,
    "live_learned_count": 0,
    "live_wins_count": 0,
    "win_rate_train": 0.0,
    "accuracy_train": 0.0,
    "trained_at": "--",
    "last_symbol": "--",
    "last_prob": None,
    "blocked_count": 0,
    "passed_count": 0,
    "_last_train_ts": 0.0,
}

# --- POMOCNE FUNKCIE, LOGOVANIE, TELEGRAM & OKX API ---

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

def _prune_old_logs(now_ts: float | None = None):
    cutoff = (time.time() if now_ts is None else now_ts) - LOG_RETENTION_SEC
    SYSTEM_LOG_ENTRIES[:] = [
        entry for entry in SYSTEM_LOG_ENTRIES
        if safe_float(entry.get("ts"), 0.0) >= cutoff
    ]
    SYSTEM_LOGS[:] = [entry["line"] for entry in SYSTEM_LOG_ENTRIES[:200]]

def log_event(msg: str, persist: bool = False):
    now_ts = time.time()
    ts = get_sk_time_str()
    line = f"[{ts}] {msg}"
    SYSTEM_LOG_ENTRIES.insert(0, {"ts": now_ts, "line": line})
    if len(SYSTEM_LOG_ENTRIES) > 500:
        del SYSTEM_LOG_ENTRIES[500:]
    _prune_old_logs(now_ts)
    print(line, flush=True)
    if persist:
        save_persistent_data()

def log_block_throttled(symbol: str, reason: str, cooldown_sec: float = 60.0):
    key = f"{symbol}:{reason}"
    now_ts = time.time()
    last_ts = LAST_BLOCK_LOG_TS.get(key, 0.0)
    if now_ts - last_ts >= cooldown_sec:
        LAST_BLOCK_LOG_TS[key] = now_ts
        log_event(f"Signal {symbol} [{CURRENT_TIMEFRAME}] pozastaveny: {reason}")

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
        req_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
        }
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="ignore")[:200]
        except Exception:
            pass
        return {"code": str(e.code), "msg": f"HTTP {e.code} {e.reason}: {err_body}", "data": []}
    except Exception as e:
        return {"code": "-1", "msg": str(e), "data": []}

def okx_public_request(path_and_query: str) -> dict:
    """Cita verejne trhove data (grafy/sviecky, tickery, instrumenty) z Live OKX."""
    for base in [OKX_PUBLIC_URL, "https://www.okx.com", "https://aws.okx.com"]:
        base_clean = base.strip().rstrip("/")
        if not base_clean:
            continue
        res = _http_get_json(f"{base_clean}{path_and_query}")
        if res.get("code") == "0":
            return res
    return _http_get_json(f"{OKX_PUBLIC_URL}{path_and_query}")

def _build_okx_auth_headers(method: str, request_path: str, body: str = "") -> dict:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    prehash = f"{ts}{method.upper()}{request_path}{body}"
    sig = hmac.new(
        OKX_SECRET.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    sign_b64 = base64.b64encode(sig).decode("utf-8")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign_b64,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
    }
    if OKX_SIMULATED == "1":
        headers["x-simulated-trading"] = "1"
    return headers

def okx_request(method: str, request_path: str, body: str = "") -> dict:
    """Posiela sukromne poziadavky (zostatok, nakup, predaj) na OKX Demo endpoint."""
    if not OKX_API_KEY or not OKX_SECRET or not OKX_PASSPHRASE:
        return {"code": "-1", "msg": "Chybaju OKX API kluce", "data": []}
    try:
        target_base = OKX_DEMO_BASE_URL or "https://eea.okx.com"
        data_bytes = body.encode("utf-8") if body else None
        headers = _build_okx_auth_headers(method, request_path, body)
        try:
            req = urllib.request.Request(f"{target_base}{request_path}", data=data_bytes, headers=headers, method=method.upper())
            with urllib.request.urlopen(req, timeout=10) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
                if parsed.get("code") == "0":
                    return parsed
                return {
                    "code": str(parsed.get("code", "-1")),
                    "msg": f"[{target_base}] {parsed.get('msg', 'Neznama chyba OKX')} (code={parsed.get('code')})",
                    "data": parsed.get("data", []),
                }
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="ignore")[:200]
            except Exception:
                pass
            return {"code": str(e.code), "msg": f"[{target_base}] HTTP {e.code} {e.reason}: {err_body}", "data": []}
        except Exception as e:
            return {"code": "-1", "msg": f"[{target_base}] {e}", "data": []}
    except Exception as e:
        return {"code": "-1", "msg": str(e), "data": []}

def fetch_account_capital(ccy: str = "USDC") -> float:
    global LAST_BALANCE_ERROR_LOG_TS, ACTIVE_BALANCE_CCY
    try:
        res = okx_request("GET", "/api/v5/account/balance")
        if res.get("code") == "0":
            balances_by_ccy: dict[str, float] = {}
            for acc in res.get("data", []):
                for item in acc.get("details", []):
                    item_ccy = str(item.get("ccy", "")).upper()
                    if not item_ccy:
                        continue
                    avail = safe_float(item.get("availBal"), 0.0)
                    cash = safe_float(item.get("cashBal"), 0.0)
                    eq = safe_float(item.get("eq"), 0.0)
                    best_val = max(avail, cash, eq)
                    if best_val > balances_by_ccy.get(item_ccy, 0.0):
                        balances_by_ccy[item_ccy] = best_val

            preferred = str(ccy).upper()
            if balances_by_ccy.get(preferred, 0.0) > 0:
                ACTIVE_BALANCE_CCY = preferred
                return balances_by_ccy[preferred]

            for fallback_ccy in ["USDC", "USDT"]:
                if balances_by_ccy.get(fallback_ccy, 0.0) > 0:
                    ACTIVE_BALANCE_CCY = fallback_ccy
                    return balances_by_ccy[fallback_ccy]

            ACTIVE_BALANCE_CCY = preferred
            return 0.0

        now_ts = time.time()
        if now_ts - LAST_BALANCE_ERROR_LOG_TS >= 60:
            LAST_BALANCE_ERROR_LOG_TS = now_ts
            log_event(f"OKX zostatok chyba (code={res.get('code')}): {res.get('msg')}")
    except Exception as err:
        now_ts = time.time()
        if now_ts - LAST_BALANCE_ERROR_LOG_TS >= 60:
            LAST_BALANCE_ERROR_LOG_TS = now_ts
            log_event(f"Chyba pri nacitani zostatku: {err}")
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

def record_closed_trade(symbol: str, pnl_pct: float, pnl_usdc: float, reason: str, entry_features: list[float] | None = None):
    trade_rec = {
        "time": get_sk_time_str(),
        "symbol": symbol,
        "pnl_pct": round(pnl_pct, 2),
        "pnl_usdc": round(pnl_usdc, 2),
        "reason": reason,
        "entry_features": entry_features,
    }
    CLOSED_TRADES_HISTORY.insert(0, trade_rec)
    if len(CLOSED_TRADES_HISTORY) > 200:
        del CLOSED_TRADES_HISTORY[200:]
    if entry_features and len(entry_features) >= 6:
        AI_LEARNED_TRADES.insert(0, {
            "time": trade_rec["time"],
            "symbol": symbol,
            "pnl_pct": trade_rec["pnl_pct"],
            "pnl_usdc": trade_rec["pnl_usdc"],
            "reason": reason,
            "entry_features": [round(safe_float(x), 5) for x in entry_features[:6]],
        })
        if len(AI_LEARNED_TRADES) > 200:
            del AI_LEARNED_TRADES[200:]
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

def _ensure_parent_dir(file_path: str):
    parent = os.path.dirname(file_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

def save_ai_memory():
    try:
        _ensure_parent_dir(AI_MEMORY_FILE)
        ai_clean = {k: v for k, v in AI_STATE.items() if not k.startswith("_")}
        learned_items = [
            t for t in AI_LEARNED_TRADES
            if t.get("entry_features") and len(t.get("entry_features")) >= 6
        ][:200]
        payload = {
            "saved_at": get_sk_time_str(),
            "ai_state": ai_clean,
            "learned_trades": learned_items,
        }
        tmp_path = f"{AI_MEMORY_FILE}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, AI_MEMORY_FILE)
    except Exception:
        pass

def load_ai_memory():
    global AI_LEARNED_TRADES, CLOSED_TRADES_HISTORY
    if not os.path.exists(AI_MEMORY_FILE):
        return
    try:
        with open(AI_MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("ai_state"), dict):
            for k, v in data["ai_state"].items():
                if not str(k).startswith("_"):
                    AI_STATE[k] = v
        if isinstance(data.get("learned_trades"), list):
            AI_LEARNED_TRADES = [
                t for t in data["learned_trades"][:200]
                if isinstance(t, dict) and t.get("entry_features") and len(t.get("entry_features")) >= 6
            ]
            if AI_LEARNED_TRADES:
                AI_STATE["live_learned_count"] = max(int(AI_STATE.get("live_learned_count", 0)), len(AI_LEARNED_TRADES))
                AI_STATE["live_wins_count"] = max(
                    int(AI_STATE.get("live_wins_count", 0)),
                    sum(1 for t in AI_LEARNED_TRADES if safe_float(t.get("pnl_usdc")) > 0),
                )
                if not CLOSED_TRADES_HISTORY:
                    CLOSED_TRADES_HISTORY = list(AI_LEARNED_TRADES[:100])
    except Exception:
        pass

def save_ai_model():
    save_ai_memory()
    save_persistent_data()

def save_persistent_data():
    try:
        _ensure_parent_dir(STATE_FILE)
        _prune_old_logs()
        ai_clean = {k: v for k, v in AI_STATE.items() if not k.startswith("_")}
        payload = {
            "settings": SETTINGS,
            "ai_state": ai_clean,
            "history": CLOSED_TRADES_HISTORY[:100],
            "logs": SYSTEM_LOGS[:100],
            "log_entries": SYSTEM_LOG_ENTRIES[:100],
        }
        tmp_path = f"{STATE_FILE}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, STATE_FILE)
        save_ai_memory()
    except Exception:
        pass

def load_persistent_data():
    global CLOSED_TRADES_HISTORY, SYSTEM_LOGS, SYSTEM_LOG_ENTRIES, AI_LEARNED_TRADES
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("settings"), dict):
                SETTINGS.update(data["settings"])
            if isinstance(data.get("ai_state"), dict):
                for k, v in data["ai_state"].items():
                    if not str(k).startswith("_"):
                        AI_STATE[k] = v
            if isinstance(data.get("history"), list):
                CLOSED_TRADES_HISTORY = data["history"][:100]
                real_with_feats = [t for t in CLOSED_TRADES_HISTORY if t.get("entry_features") and len(t.get("entry_features")) >= 6]
                if real_with_feats and not AI_LEARNED_TRADES:
                    AI_LEARNED_TRADES = list(real_with_feats[:200])
                if real_with_feats:
                    AI_STATE["live_learned_count"] = max(int(AI_STATE.get("live_learned_count", 0)), len(real_with_feats))
                    AI_STATE["live_wins_count"] = max(
                        int(AI_STATE.get("live_wins_count", 0)),
                        sum(1 for t in real_with_feats if safe_float(t.get("pnl_usdc")) > 0),
                    )
            now_ts = time.time()
            if isinstance(data.get("log_entries"), list):
                loaded_entries = []
                for item in data["log_entries"][:200]:
                    if isinstance(item, dict) and item.get("line"):
                        loaded_entries.append({
                            "ts": safe_float(item.get("ts"), now_ts),
                            "line": str(item["line"]),
                        })
                SYSTEM_LOG_ENTRIES[:] = loaded_entries
            elif isinstance(data.get("logs"), list):
                SYSTEM_LOG_ENTRIES[:] = [
                    {"ts": now_ts, "line": str(line)}
                    for line in data["logs"][:200]
                ]
            _prune_old_logs(now_ts)
        except Exception:
            pass
    load_ai_memory()

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
            log_event(f"Nepodarilo sa nacitat tickery pre 20 symbolov: {res.get('msg')}")
            return
        candidates = []
        available_usdc = set()
        for t in res["data"]:
            inst = t.get("instId", "")
            if inst.endswith("-USDC") and inst not in EXCLUDED_SYMBOLS:
                available_usdc.add(inst)
                vol_ccy = safe_float(t.get("volCcy24h"), 0.0)
                if vol_ccy >= 500000.0:
                    candidates.append((vol_ccy, inst))
        candidates.sort(key=lambda x: x[0], reverse=True)
        top = [inst for _, inst in candidates[:20]]
        if "RENDER-USDC" in available_usdc and "RENDER-USDC" not in top:
            if len(top) >= 20:
                top[-1] = "RENDER-USDC"
            else:
                top.append("RENDER-USDC")
        if len(top) >= 5:
            TOP_LIQUID_SYMBOLS = top
    except Exception as e:
        log_event(f"Chyba pri aktualizacii top symbolov: {e}")

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

# --- AI MODEL PRE HODNOTENIE KVALITY BUY SIGNALOV ---

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

def get_current_ai_features_for_symbol(symbol: str, bar_tf: str = "15m") -> list[float] | None:
    try:
        res = okx_public_request(f"/api/v5/market/candles?instId={symbol}&bar={bar_tf}&limit=80")
        if res.get("code") != "0" or not res.get("data") or len(res["data"]) < 25:
            return None
        candles = list(reversed(res["data"]))
        closed = candles[:-1]
        n = len(closed)
        if n < 20:
            return None

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

        return extract_ai_features(n - 1, ha_closes, ha_opens, ha_highs, ha_lows, volumes, oscillator, signal_line, close_ema_9, predicted_next)
    except Exception:
        return None

def train_ai_model_from_candles(symbol: str, bar_tf: str = "15m", force: bool = False) -> dict:
    global AI_STATE
    now_ts = time.time()
    if not force and (now_ts - AI_STATE.get("_last_train_ts", 0.0)) < 1800 and AI_STATE.get("samples_count", 0) >= 15:
        return {"ok": True, "samples": AI_STATE["samples_count"], "win_rate": AI_STATE["win_rate_train"]}

    try:
        res = okx_public_request(f"/api/v5/market/candles?instId={symbol}&bar={bar_tf}&limit=300")
        if res.get("code") != "0" or not res.get("data") or len(res["data"]) < 60:
            return {"ok": False, "msg": "Nedostatok historickych sviecok z OKX."}

        candles = list(reversed(res["data"]))
        closed = candles[:-1]
        n = len(closed)
        if n < 50:
            return {"ok": False, "msg": "Prilis malo uzavretych sviecok."}

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

        target_gain_pct = max(0.85, float(SETTINGS.get("ttp_trigger_pct", 0.85)))
        stop_loss_pct = float(SETTINGS.get("fixed_sl_pct", 2.0))
        horizon = 14

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
            return {"ok": False, "msg": f"Najdenych len {len(dataset)} historickych signalov."}

        weights = [0.0] * 6
        bias = 0.0
        lr = 0.12
        reg = 0.05

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

        acc_pct = round((correct / len(dataset)) * 100.0, 1)
        AI_STATE["weights"] = [round(w, 5) for w in weights]
        AI_STATE["bias"] = round(bias, 5)
        AI_STATE["samples_count"] = len(dataset)
        AI_STATE["win_rate_train"] = round((wins_cnt / len(dataset)) * 100.0, 1)
        AI_STATE["accuracy_train"] = acc_pct
        AI_STATE["trained_at"] = get_sk_time_str()
        AI_STATE["last_symbol"] = f"{symbol} ({bar_tf.upper()})"
        AI_STATE["_last_train_ts"] = now_ts

        source_trades = AI_LEARNED_TRADES if AI_LEARNED_TRADES else [
            t for t in CLOSED_TRADES_HISTORY if t.get("entry_features") and len(t.get("entry_features")) >= 6
        ]
        for t in reversed(source_trades[:50]):
            t_feats = t.get("entry_features")
            if t_feats and len(t_feats) >= 6:
                pnl_usdc_val = safe_float(t.get("pnl_usdc"))
                pnl_pct_val = safe_float(t.get("pnl_pct"))
                y_real = 1.0 if pnl_usdc_val > 0 else 0.0
                steps_for_trade = min(20, max(8, int(8 + abs(pnl_pct_val) * 6)))
                step_lr = min(0.22, 0.12 + min(0.10, abs(pnl_pct_val) * 0.04))
                for _ in range(steps_for_trade):
                    z = AI_STATE["bias"] + sum(AI_STATE["weights"][k] * t_feats[k] for k in range(6))
                    err = _sigmoid(z) - y_real
                    for k in range(6):
                        AI_STATE["weights"][k] -= step_lr * (err * t_feats[k] + 0.03 * AI_STATE["weights"][k])
                    AI_STATE["bias"] -= step_lr * err
                AI_STATE["weights"] = [round(w, 5) for w in AI_STATE["weights"]]
                AI_STATE["bias"] = round(AI_STATE["bias"], 5)

        if source_trades:
            AI_STATE["live_learned_count"] = max(int(AI_STATE.get("live_learned_count", 0)), len(source_trades))
            AI_STATE["live_wins_count"] = max(
                int(AI_STATE.get("live_wins_count", 0)),
                sum(1 for t in source_trades if safe_float(t.get("pnl_usdc")) > 0),
            )

        save_ai_model()

        live_cnt = int(AI_STATE.get("live_learned_count", 0))
        live_wins = int(AI_STATE.get("live_wins_count", 0))
        live_losses = max(0, live_cnt - live_wins)
        log_event(
            f"AI model natrenovany na {symbol} [{bar_tf.upper()}]: {len(dataset)} vzoriek | "
            f"Nastudovane a ulozene obchody: {live_cnt} ({live_wins}W/{live_losses}L) | Presnost: {acc_pct}%."
        )
        return {
            "ok": True,
            "samples": len(dataset),
            "win_rate": AI_STATE["win_rate_train"],
            "accuracy": acc_pct,
            "live_learned_count": live_cnt,
            "live_wins_count": live_wins,
            "live_losses_count": live_losses,
        }
    except Exception as e:
        return {"ok": False, "msg": str(e)}

def _effective_ai_min_prob() -> float:
    base_min = float(SETTINGS.get("ai_min_prob", 0.35))
    live_cnt = int(AI_STATE.get("live_learned_count", 0))
    if live_cnt < 10:
        return min(base_min, 0.35)
    if live_cnt < 25:
        return max(base_min, 0.42)
    return max(base_min, 0.48)

def predict_ai_probability(feats: list[float]) -> float | None:
    if AI_STATE.get("samples_count", 0) < 10:
        return None
    w = AI_STATE.get("weights", [0.0] * 6)
    b = float(AI_STATE.get("bias", 0.0))
    z = b + sum(w[k] * feats[k] for k in range(min(len(w), len(feats))))
    z = max(-2.2, min(2.2, z))
    raw_prob = _sigmoid(z)

    live_cnt = int(AI_STATE.get("live_learned_count", 0))
    acc_train = safe_float(AI_STATE.get("accuracy_train"), 0.0)
    if live_cnt == 0 and 0.0 < acc_train < 50.0:
        confidence = max(0.35, acc_train / 100.0)
        calibrated = 0.5 + (raw_prob - 0.5) * confidence
        return round(calibrated, 3)

    return round(raw_prob, 3)

def learn_from_closed_trade(features: list[float] | None, pnl_usdc: float, pnl_pct: float = 0.0):
    global AI_STATE
    if not features or len(features) < 6:
        return
    y = 1.0 if pnl_usdc > 0 else 0.0
    weights = list(AI_STATE.get("weights", [0.0] * 6))
    while len(weights) < 6:
        weights.append(0.0)
    bias = float(AI_STATE.get("bias", 0.0))
    mag = abs(pnl_pct) if abs(pnl_pct) > 0 else min(5.0, abs(pnl_usdc) / 10.0)
    lr = min(0.24, 0.14 + min(0.10, mag * 0.05))
    epochs = min(24, max(12, int(12 + mag * 6)))
    reg = 0.03

    for _ in range(epochs):
        z = bias + sum(weights[k] * features[k] for k in range(6))
        pred_p = _sigmoid(z)
        err = pred_p - y
        for k in range(6):
            weights[k] -= lr * (err * features[k] + reg * weights[k])
        bias -= lr * err

    live_cnt = int(AI_STATE.get("live_learned_count", 0)) + 1
    live_wins = int(AI_STATE.get("live_wins_count", 0)) + (1 if pnl_usdc > 0 else 0)
    AI_STATE["weights"] = [round(w, 5) for w in weights]
    AI_STATE["bias"] = round(bias, 5)
    AI_STATE["live_learned_count"] = live_cnt
    AI_STATE["live_wins_count"] = live_wins
    AI_STATE["trained_at"] = get_sk_time_str()
    save_ai_model()
    log_event(
        f"AI sa doucila z realneho obchodu ({'ZISK' if pnl_usdc > 0 else 'STRATA'} {pnl_pct:+.2f}% / {pnl_usdc:+.2f} $) | Realnych vzoriek: {live_cnt} ({live_wins}W)",
        persist=True,
    )

# --- VYHODNOCOVANIE PINE SIGNALOV + KVALITA + AI ---

def evaluate_pine_strategy(symbol: str, bar_tf: str = "15m"):
    global MARKET_SCANNER_CACHE, ACTIVE_TRADES, AI_STATE
    coin_key = symbol.split("-")[0]

    try:
        url = f"/api/v5/market/candles?instId={symbol}&bar={bar_tf}&limit=120"
        res = okx_public_request(url)
        if res.get("code") != "0" or not res.get("data") or len(res["data"]) < 30:
            MARKET_SCANNER_CACHE[coin_key] = {
                "symbol": symbol, "price": 0.0, "oscillator": 0.0,
                "momentum": "SLABE", "impulse": "CAKA", "trade_status": "---",
                "be_status": "VYPNUTY", "ttp_status": "VYPNUTY", "ts_status": "VYPNUTY",
                "ai_score": "--", "exec_signal": "CAKAJ", "signal": "CHYBA DAT", "is_green": False
            }
            return {"buy": False, "sell": False, "sell_reason": "", "ai_prob": None, "block_reason": "", "features": None}

        candles = list(reversed(res["data"]))
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
        trail_pct = float(SETTINGS.get("trail_pct", 1.2))
        use_breakeven = bool(SETTINGS.get("use_breakeven", True))
        be_trigger_pct = float(SETTINGS.get("be_trigger_pct", 0.6))
        use_trailing_tp = bool(SETTINGS.get("use_trailing_tp", True))
        ttp_trigger_pct = float(SETTINGS.get("ttp_trigger_pct", 0.85))
        ttp_callback_pct = float(SETTINGS.get("ttp_callback_pct", 0.3))
        use_ha_exit = bool(SETTINGS.get("use_ha_exit", False))
        ha_exit_consecutive = max(1, min(3, int(SETTINGS.get("ha_exit_consecutive", 3))))

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

        if is_in_trade:
            ha_is_red_now = ha_closes[-1] < ha_opens[-1]
            ha_live_exit = False
            if use_ha_exit:
                if ha_exit_consecutive == 1:
                    ha_live_exit = ha_is_red_now
                elif ha_exit_consecutive == 2:
                    ha_live_exit = (n >= 2) and ha_is_red_now and (ha_closes[-2] < ha_opens[-2])
                else:
                    ha_live_exit = (n >= 3) and ha_is_red_now and (ha_closes[-2] < ha_opens[-2]) and (ha_closes[-3] < ha_opens[-3])
            if ha_live_exit:
                last_bar_sell = True
                last_bar_sell_reason = f"Pine SELL (HA Exit {ha_exit_consecutive}x)"

        feats = extract_ai_features(n - 1, ha_closes, ha_opens, ha_highs, ha_lows, volumes, oscillator, signal_line, close_ema_9, predicted_next)
        ai_prob = predict_ai_probability(feats)
        if ai_prob is not None:
            AI_STATE["last_prob"] = ai_prob

        final_buy = last_bar_buy
        block_reason = ""

        if last_bar_buy and not is_in_trade:
            if bool(SETTINGS.get("block_sideways_buy", False)) and IS_SIDEWAYS_MARKET:
                final_buy = False
                block_reason = "Bocny trh (SIDEWAYS)"

            if final_buy and bool(SETTINGS.get("use_anti_chase", False)):
                last_raw = closed_candles[-1]
                raw_o = safe_float(last_raw[1])
                raw_c = safe_float(last_raw[4])
                candle_pump_pct = ((raw_c - raw_o) / raw_o * 100.0) if raw_o > 0 else 0.0
                ema9_last = close_ema_9[-1] if close_ema_9[-1] and close_ema_9[-1] > 0 else raw_c
                dist_ema9_pct = ((raw_c - ema9_last) / ema9_last * 100.0) if ema9_last > 0 else 0.0
                max_pump = float(SETTINGS.get("max_candle_pump_pct", 1.5))
                max_ema_dist = float(SETTINGS.get("max_ema9_dist_pct", 1.4))
                if candle_pump_pct > max_pump:
                    final_buy = False
                    block_reason = f"Anti-Chase: sviecka +{candle_pump_pct:.2f}% > {max_pump:.2f}%"
                elif dist_ema9_pct > max_ema_dist:
                    final_buy = False
                    block_reason = f"Anti-Chase: nad EMA9 +{dist_ema9_pct:.2f}% > {max_ema_dist:.2f}%"

            min_rel_vol = float(SETTINGS.get("min_rel_volume", 0.0))
            if final_buy and min_rel_vol > 0:
                vol_win = volumes[max(0, n - 15): n - 1]
                avg_v = (sum(vol_win) / len(vol_win)) if vol_win else 0.0
                rel_v = (volumes[-1] / avg_v) if avg_v > 0 else 1.0
                if rel_v < min_rel_vol:
                    final_buy = False
                    block_reason = f"Nizky objem ({rel_v:.2f}x < {min_rel_vol:.2f}x)"

            if final_buy and bool(SETTINGS.get("use_ai_filter", True)):
                if AI_STATE.get("samples_count", 0) < 10:
                    train_ai_model_from_candles(symbol, bar_tf=bar_tf, force=False)
                    feats = extract_ai_features(n - 1, ha_closes, ha_opens, ha_highs, ha_lows, volumes, oscillator, signal_line, close_ema_9, predicted_next)
                    ai_prob = predict_ai_probability(feats)
                    if ai_prob is not None:
                        AI_STATE["last_prob"] = ai_prob

                min_prob = _effective_ai_min_prob()
                if ai_prob is not None and ai_prob < min_prob:
                    final_buy = False
                    block_reason = f"AI skore {ai_prob*100:.1f}% < {min_prob*100:.1f}%"
                    AI_STATE["blocked_count"] = AI_STATE.get("blocked_count", 0) + 1
                else:
                    AI_STATE["passed_count"] = AI_STATE.get("passed_count", 0) + 1

        momentum_str = "SILNE" if last_cond_volume_bullish else "SLABE"
        impuls_active = last_buy_trigger or is_in_trade
        impulse_str = "AKTIVNY (V TRHU)" if impuls_active else "CAKA"

        if is_in_trade:
            t_ref = ACTIVE_TRADES[coin_key]
            trade_status_txt = f"{t_ref['entry_price']:.4f}"
            be_status_txt = f"{t_ref['entry_price']:.4f}" if (use_breakeven and t_ref.get("be_active", False)) else ("CAKA" if use_breakeven else "VYPNUTY")
            ttp_stop_live = t_ref.get("ttp_stop_price")
            ttp_status_txt = f"{ttp_stop_live:.4f}" if (use_trailing_tp and t_ref.get("ttp_active", False) and ttp_stop_live) else ("V TRHU" if use_trailing_tp else "VYPNUTY")
            ts_status_txt = f"{t_ref['fixed_sl_price']:.4f}"
        else:
            trade_status_txt = "---"
            be_status_txt = "VYPNUTY"
            ttp_status_txt = "VYPNUTY"
            ts_status_txt = f"-{SETTINGS['fixed_sl_pct']}%"

        ai_score_txt = f"{ai_prob*100:.1f}%" if ai_prob is not None else ("UCI SA" if SETTINGS.get("use_ai_filter", True) else "OFF")

        if final_buy and not is_in_trade:
            exec_signal_str = "NAKUPIT (AI BUY)"
        elif last_bar_buy and not final_buy and not is_in_trade:
            exec_signal_str = f"AI BLOK ({block_reason})"
        elif last_bar_sell and is_in_trade:
            exec_signal_str = "PREDAT (SELL)"
        elif is_in_trade:
            exec_signal_str = "DRZAT"
        else:
            exec_signal_str = "CAKAJ"

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
            "signal": "BUY" if final_buy else ("AI BLOK" if last_bar_buy else ("SELL" if last_bar_sell else ("BULLISH" if last_cond_volume_bullish else "CAKA"))),
            "is_green": final_buy
        }

        return {"buy": final_buy, "sell": last_bar_sell, "sell_reason": last_bar_sell_reason, "ai_prob": ai_prob, "block_reason": block_reason, "features": feats}
    except Exception as e:
        log_event(f"Vynimka pri vyhodnocovani {symbol}: {e}")
        MARKET_SCANNER_CACHE[coin_key] = {
            "symbol": symbol, "price": 0.0, "oscillator": 0.0,
            "momentum": "SLABE", "impulse": "CAKA", "trade_status": "---",
            "be_status": "VYPNUTY", "ttp_status": "VYPNUTY", "ts_status": "VYPNUTY",
            "ai_score": "--", "exec_signal": "CAKAJ", "signal": "VYNIMKA", "is_green": False
        }
        return {"buy": False, "sell": False, "sell_reason": "", "ai_prob": None, "block_reason": "", "features": None}

# --- AUTONOMNA SKENOVACIA SLUCKA ---

async def autonomous_scanner_loop():
    global IS_SIDEWAYS_MARKET, MARKET_REGIME, LAST_SIDEWAYS_ALERT_TS
    await asyncio.sleep(3)
    log_event(f"Autonomny Pine Script + AI skener spusteny pre {len(TOP_LIQUID_SYMBOLS)} symbolov!")
    while True:
        try:
            _prune_old_logs()
            tf_str = CURRENT_TIMEFRAME.lower()
            max_allowed = SETTINGS.get("max_parallel", 1)

            regime_now, sideways_now = get_market_regime_and_sideways(tf_str)
            MARKET_REGIME = regime_now
            IS_SIDEWAYS_MARKET = sideways_now
            now_ts = datetime.now().timestamp()
            if sideways_now and (now_ts - LAST_SIDEWAYS_ALERT_TS >= SIDEWAYS_ALERT_COOLDOWN_SEC):
                LAST_SIDEWAYS_ALERT_TS = now_ts
                log_event(f"POZOR: BOCNY TRH zisteny na TF {CURRENT_TIMEFRAME} (uzke rozpatie + caste krizenie EMA).")
                send_telegram_msg(
                    "<b>POZOR: BOCNY TRH</b>\n"
                    f"<b>TF:</b> {CURRENT_TIMEFRAME}\n"
                    "Trh sa pohybuje v uzkom pasme s castymi obratmi – zvysene riziko falosnych signalov."
                )

            for coin_name in list(ACTIVE_TRADES.keys()):
                if coin_name not in ACTIVE_TRADES:
                    continue
                trade = ACTIVE_TRADES[coin_name]
                symbol = trade["market_pair"]
                sig = evaluate_pine_strategy(symbol, bar_tf=tf_str)
                if sig["sell"]:
                    log_event(f"PINE SCRIPT 'SELL' SIGNAL PRE {symbol} ({sig['sell_reason']})!")
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
                            log_event(f"PINE SCRIPT + AI 'BUY' SIGNAL PRE {symbol}{prob_txt}!")
                            await execute_autonomous_buy(symbol, ai_prob=sig.get("ai_prob"), entry_features=sig.get("features"))
                        elif sig.get("block_reason"):
                            log_block_throttled(symbol, sig["block_reason"], cooldown_sec=60.0)

                        await asyncio.sleep(0.3)
        except Exception as e:
            log_event(f"Chyba v autonomnom skeneri: {e}")
        await asyncio.sleep(10)

async def top_symbols_updater_loop():
    while True:
        try:
            update_top_liquid_symbols()
        except Exception as e:
            log_event(f"Chyba v slucke top symbolov: {e}")
        await asyncio.sleep(3600)

async def execute_autonomous_buy(okx_symbol: str, ai_prob: float | None = None, entry_features: list[float] | None = None):
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
            log_event(f"Nedostatok prostriedkov na ucte: {free_capital:.2f} {ACTIVE_BALANCE_CCY}")
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
            err_msg = order_res.get("msg", "Neznama chyba OKX")
            log_event(f"Par {okx_symbol} odmietnuty v Demo rezime ({err_msg}).")
            return

        if entry_features is None:
            entry_features = get_current_ai_features_for_symbol(okx_symbol, bar_tf=CURRENT_TIMEFRAME.lower())
        if ai_prob is None and entry_features is not None:
            ai_prob = predict_ai_probability(entry_features)

        coin_amount = float(formatted_sz)
        actual_spent_usdc = coin_amount * current_price
        fixed_sl_pct = SETTINGS.get("fixed_sl_pct", 2.0)
        fixed_sl_price = current_price * (1.0 - (fixed_sl_pct / 100.0))
        use_fixed_tp = bool(SETTINGS.get("use_fixed_tp", False))
        fixed_tp_pct = float(SETTINGS.get("fixed_tp_pct", 0.9))
        fixed_tp_price = current_price * (1.0 + (fixed_tp_pct / 100.0))

        ACTIVE_TRADES[coin_name] = {
            "entry_price": current_price,
            "current_price": current_price,
            "fixed_sl_price": fixed_sl_price,
            "fixed_sl_pct": fixed_sl_pct,
            "use_fixed_tp": use_fixed_tp,
            "fixed_tp_price": fixed_tp_price,
            "fixed_tp_pct": fixed_tp_pct,
            "be_active": False,
            "ttp_active": False,
            "ttp_stop_price": None,
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
            "ai_prob": ai_prob,
            "entry_features": entry_features,
            "source": "AUTO",
        }

        tp_info = f" | Fixny TP: +{fixed_tp_pct}% ({fixed_tp_price:.4f})" if use_fixed_tp else ""
        ai_info = f" | AI: {ai_prob*100:.1f}%" if ai_prob is not None else ""
        log_event(f"NAKUP (Pine + AI BUY) [{CURRENT_TIMEFRAME}]: {okx_symbol} za {current_price:.4f} {ACTIVE_BALANCE_CCY} | Investicia: {actual_spent_usdc:.2f} {ACTIVE_BALANCE_CCY} | Fixny SL: -{fixed_sl_pct}% ({fixed_sl_price:.4f}){tp_info}{ai_info}", persist=True)

        inline_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "ZATVORIŤ VŠETKO", "callback_data": "sell_now"},
                    {"text": "PAUZA BOTA", "callback_data": "toggle_bot"}
                ]
            ]
        }
        send_telegram_msg(
            f"<b>PINE + AI BUY NAKUP [TF: {CURRENT_TIMEFRAME}]</b>\n"
            f"<b>Par:</b> {okx_symbol}\n"
            f"<b>Vstup:</b> {current_price:.4f}\n"
            f"<b>Investicia:</b> {actual_spent_usdc:.2f} {ACTIVE_BALANCE_CCY}\n"
            + (f"<b>AI skore:</b> {ai_prob*100:.1f}%\n" if ai_prob is not None else "")
            + f"<b>Fixny Stop-Loss:</b> -{fixed_sl_pct}% ({fixed_sl_price:.4f})\n"
            + (f"<b>Fixny Take Profit:</b> +{fixed_tp_pct}% ({fixed_tp_price:.4f})\n" if use_fixed_tp else "")
            + "<b>Vystup:</b> Pine SELL signal alebo Fixny SL" + (" / Fixny TP" if use_fixed_tp else ""),
            reply_markup=inline_keyboard
        )

async def close_trade_position(coin: str, reason: str):
    global ACTIVE_TRADES, LAST_TRADE_TIME
    async with trade_lock:
        if coin not in ACTIVE_TRADES:
            return
        trade = ACTIVE_TRADES[coin]
        symbol = trade["market_pair"]
        sell_ok = False
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
            sell_res = okx_request("POST", "/api/v5/trade/order", order_body)
            if sell_res.get("code") != "0":
                log_event(f"Predaj {symbol} vratil chybu z OKX Demo ({sell_res.get('msg')}) – pozicia ostava otvorena na dalsi pokus.", persist=True)
                return

            sell_ok = True
            pnl_pct = ((curr_p - trade["entry_price"]) / trade["entry_price"]) * 100.0
            invested = trade.get("invested_usdc", trade["amount"] * trade["entry_price"])
            pnl_usdc = invested * (pnl_pct / 100.0)

            feats = trade.get("entry_features")
            if not feats:
                feats = get_current_ai_features_for_symbol(symbol, bar_tf=str(trade.get("tf", CURRENT_TIMEFRAME)).lower())

            record_closed_trade(symbol, pnl_pct, pnl_usdc, reason, feats)
            learn_from_closed_trade(feats, pnl_usdc, pnl_pct=pnl_pct)
            LAST_TRADE_TIME[coin] = datetime.now().timestamp()

            log_event(f"Uzavreta pozicia ({reason}) pre {symbol} | PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)", persist=True)
            send_telegram_msg(f"<b>UZAVRETA POZICIA ({reason})</b>\n<b>Par:</b> {symbol}\n<b>PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)")
        except Exception as e:
            log_event(f"Chyba pri zatvarani {symbol}: {e}")
        finally:
            if sell_ok and coin in ACTIVE_TRADES:
                del ACTIVE_TRADES[coin]

# --- MONITOR RIZIK ---

async def fixed_stoploss_monitor_loop():
    global ACTIVE_TRADES, CURRENT_STAGE
    await asyncio.sleep(3)
    while True:
        if not ACTIVE_TRADES:
            if IS_BOT_ACTIVE:
                max_p = SETTINGS.get("max_parallel", 1)
                sl_p = SETTINGS.get("fixed_sl_pct", 2.0)
                tp_txt = f" | Fixny TP: +{SETTINGS.get('fixed_tp_pct', 0.9)}%" if SETTINGS.get("use_fixed_tp", False) else ""
                ai_txt = f" | AI >= {_effective_ai_min_prob()*100:.0f}%" if SETTINGS.get("use_ai_filter", True) else ""
                CURRENT_STAGE = f"STAV: Bot aktivny (Max {max_p}x | Fixny SL: -{sl_p}%{tp_txt}{ai_txt}). Cakam na BUY/SELL..."
            else:
                CURRENT_STAGE = "STAV: Bot pozastaveny."
            await asyncio.sleep(2)
            continue

        try:
            use_be = bool(SETTINGS.get("use_breakeven", True))
            be_trig = float(SETTINGS.get("be_trigger_pct", 0.6))
            use_ts = bool(SETTINGS.get("use_trailing", True))
            ts_pct = float(SETTINGS.get("trail_pct", 1.2))
            use_ttp = bool(SETTINGS.get("use_trailing_tp", True))
            ttp_trig = float(SETTINGS.get("ttp_trigger_pct", 0.85))
            ttp_cb = float(SETTINGS.get("ttp_callback_pct", 0.3))

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

                    entry_p = trade["entry_price"]
                    highest_p = trade["highest_price"]
                    pnl_pct = ((current_price - entry_p) / entry_p) * 100.0
                    invested = trade.get("invested_usdc", trade["amount"] * entry_p)
                    pnl_usdc = invested * (pnl_pct / 100.0)
                    trade["live_pnl_pct"] = pnl_pct
                    trade["live_pnl_usdc"] = pnl_usdc

                    fixed_sl_price = trade["fixed_sl_price"]
                    fixed_sl_pct = trade["fixed_sl_pct"]
                    use_fixed_tp = bool(trade.get("use_fixed_tp", False))
                    fixed_tp_price = trade.get("fixed_tp_price")
                    fixed_tp_pct = trade.get("fixed_tp_pct", SETTINGS.get("fixed_tp_pct", 0.9))

                    tp_stage = f" | Fixny TP: +{fixed_tp_pct}%" if use_fixed_tp else ""
                    CURRENT_STAGE = f"STAV: Pozicii: {len(ACTIVE_TRADES)} | {symbol} ({pnl_pct:+.2f}%) | Fixny SL: -{fixed_sl_pct}%{tp_stage}"

                    if current_price <= fixed_sl_price:
                        await close_trade_position(coin, f"Fixny Stop-Loss (-{fixed_sl_pct}%)")
                        continue

                    if use_fixed_tp and fixed_tp_price is not None and current_price >= fixed_tp_price:
                        await close_trade_position(coin, f"Fixny Take-Profit (+{fixed_tp_pct}%)")
                        continue

                    if use_be and not trade.get("be_active", False):
                        if current_price >= entry_p * (1.0 + (be_trig / 100.0)):
                            trade["be_active"] = True
                            log_event(f"Break-Even aktivovany pre {symbol} pri zisku +{pnl_pct:.2f}% (ochrana vstupu {entry_p:.4f}).")

                    if use_be and trade.get("be_active", False) and current_price <= entry_p:
                        await close_trade_position(coin, "Break-Even Exit (0.00% / ochrana vstupu)")
                        continue

                    if use_ttp:
                        if not trade.get("ttp_active", False) and current_price >= entry_p * (1.0 + (ttp_trig / 100.0)):
                            trade["ttp_active"] = True
                            log_event(f"Trailing TP aktivovany pre {symbol} pri +{pnl_pct:.2f}% (odstup {ttp_cb}%).")
                        if trade.get("ttp_active", False):
                            ttp_stop = highest_p * (1.0 - (ttp_cb / 100.0))
                            trade["ttp_stop_price"] = ttp_stop
                            if current_price <= ttp_stop:
                                await close_trade_position(coin, f"Trailing Take-Profit (-{ttp_cb}% z maxima)")
                                continue

                    if use_ts and highest_p > entry_p:
                        ts_stop = highest_p * (1.0 - (ts_pct / 100.0))
                        if trade.get("be_active", False):
                            ts_stop = max(ts_stop, entry_p)
                        if current_price <= ts_stop:
                            await close_trade_position(coin, f"Trailing Stop (-{ts_pct}% z maxima)")
                            continue
        except Exception as e:
            log_event(f"Chyba v monitore rizik: {e}")
        await asyncio.sleep(2)

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_persistent_data()
    update_top_liquid_symbols()
    if SETTINGS.get("use_ai_filter", True) and AI_STATE.get("samples_count", 0) < 10:
        seed_sym = TOP_LIQUID_SYMBOLS[0] if TOP_LIQUID_SYMBOLS else "SOL-USDC"
        train_ai_model_from_candles(seed_sym, bar_tf=CURRENT_TIMEFRAME.lower(), force=True)
    else:
        save_ai_memory()
    log_event(
        f"Autonomny APEX MATRIX_SCALPER_v1_BOT ({len(TOP_LIQUID_SYMBOLS)} symbolov · Live grafy + OKX Demo obchod + AI Filter + Fixny SL -{SETTINGS['fixed_sl_pct']}%) spusteny | AI pamat: {AI_MEMORY_FILE}",
        persist=True,
    )
    monitor_task = asyncio.create_task(fixed_stoploss_monitor_loop())
    scanner_task = asyncio.create_task(autonomous_scanner_loop())
    updater_task = asyncio.create_task(top_symbols_updater_loop())
    send_telegram_msg(f"APEX MATRIX_SCALPER_v1_BOT ({len(TOP_LIQUID_SYMBOLS)} symbolov · Live grafy + OKX Demo obchod + AI Filter + Fixny SL -{SETTINGS['fixed_sl_pct']}%) pripraveny!")
    yield
    save_persistent_data()
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
        "use_anti_chase": SETTINGS["use_anti_chase"],
        "max_candle_pump_pct": SETTINGS["max_candle_pump_pct"],
        "max_ema9_dist_pct": SETTINGS["max_ema9_dist_pct"],
        "use_ai_filter": SETTINGS["use_ai_filter"],
        "ai_min_prob": SETTINGS["ai_min_prob"],
    }

@app.post("/api/telegram-webhook")
async def telegram_webhook(request: Request):
    try:
        secret_hdr = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if WEBHOOK_SECRET and secret_hdr and secret_hdr != WEBHOOK_SECRET:
            return JSONResponse({"status": "unauthorized"}, status_code=401)
        data = await request.json()
        if "callback_query" in data:
            cb = data["callback_query"]
            cb_data = cb.get("data", "")
            if cb_data == "sell_now":
                await sell_now()
                send_telegram_msg("Rucny predaj aktivovany z Telegramu!")
            elif cb_data == "toggle_bot":
                await toggle_bot()
    except Exception as e:
        log_event(f"Chyba v Telegram webhooku: {e}")
    return {"status": "ok"}

@app.post("/api/toggle-bot")
async def toggle_bot():
    global IS_BOT_ACTIVE, CURRENT_STAGE
    IS_BOT_ACTIVE = not IS_BOT_ACTIVE
    status_str = "aktivny" if IS_BOT_ACTIVE else "pozastaveny"
    CURRENT_STAGE = f"STAV: Bot je {status_str}."
    log_event(f"Prepinač bota: {status_str.upper()}", persist=True)
    send_telegram_msg(f"Zmena stavu: Bot je teraz {status_str.upper()}.")
    return JSONResponse({"status": "success", "is_bot_active": IS_BOT_ACTIVE})

@app.post("/api/toggle-btc-filter")
async def toggle_btc_filter():
    SETTINGS["use_btc_filter"] = not SETTINGS.get("use_btc_filter", True)
    status_str = "zapnuty (aktivny)" if SETTINGS["use_btc_filter"] else "vypnuty (deaktivovany)"
    log_event(f"BTC Trend Guard filter: {status_str.upper()}", persist=True)
    send_telegram_msg(f"Zmena stavu: BTC Trend Guard filter je teraz {status_str.upper()}.")
    return JSONResponse({"status": "success", "use_btc_filter": SETTINGS["use_btc_filter"]})

@app.post("/api/toggle-compounding")
async def toggle_compounding():
    SETTINGS["compounding_enabled"] = not SETTINGS.get("compounding_enabled", False)
    status_str = "ZAPNUTE (Zloyene urovenie)" if SETTINGS["compounding_enabled"] else "VYPNUTE (Pevny rozpocet)"
    log_event(f"Automaticke reinvestovanie (COMPOUNDING): {status_str.upper()}", persist=True)
    send_telegram_msg(f"Zmena stavu: Auto-Compounding je teraz {status_str}.")
    return JSONResponse({"status": "success", "compounding_enabled": SETTINGS["compounding_enabled"]})

@app.post("/api/set-budget/{amount}")
async def set_budget(amount: int):
    if amount in [500, 1000, 3000, 5000, 10000]:
        SETTINGS["max_budget_cap"] = float(amount)
        log_event(f"Maximalny rozpocet (strop) zmeneny na: {amount} {ACTIVE_BALANCE_CCY}", persist=True)
        send_telegram_msg(f"Zmena rozpoctu: Maximalny strop pre obchod je teraz {amount} {ACTIVE_BALANCE_CCY}.")
        return JSONResponse({"status": "success", "max_budget_cap": amount})
    return JSONResponse({"status": "error", "message": "Neplatna hodnota rozpoctu."})

@app.post("/api/set-parallel/{count}")
async def set_parallel(count: int):
    if count in [1, 2, 3, 4]:
        SETTINGS["max_parallel"] = count
        log_event(f"Maximalny pocet paralelnych obchodov zmeneny na: {count}", persist=True)
        send_telegram_msg(f"Zmena nastavenia: Bot moze teraz otvorit naraz max. {count} obchody.")
        return JSONResponse({"status": "success", "max_parallel": count})
    return JSONResponse({"status": "error", "message": "Neplatna hodnota."})

@app.post("/api/set-sl/{sl_val}")
async def set_sl(sl_val: float):
    if 0.1 <= sl_val <= 50.0:
        SETTINGS["fixed_sl_pct"] = float(sl_val)
        log_event(f"Fixny Stop-Loss zmeneny na: -{sl_val}%", persist=True)
        send_telegram_msg(f"Zmena Stop-Lossu: Fixny SL pre nove obchody je teraz -{sl_val}%.")
        return JSONResponse({"status": "success", "fixed_sl_pct": sl_val})
    return JSONResponse({"status": "error", "message": "Neplatna hodnota SL (povolene 0.1 - 50.0 %)." })

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
        log_event(f"Casovy ramec (TF) zmeneny na: {CURRENT_TIMEFRAME}", persist=True)
        return JSONResponse({"status": "success", "tf": CURRENT_TIMEFRAME})
    return JSONResponse({"status": "error", "message": "Neplatny casovy ramec."})

@app.post("/api/train-ai")
async def api_train_ai():
    seed_sym = TOP_LIQUID_SYMBOLS[0] if TOP_LIQUID_SYMBOLS else "SOL-USDC"
    res = train_ai_model_from_candles(seed_sym, bar_tf=CURRENT_TIMEFRAME.lower(), force=True)
    if res.get("ok"):
        MARKET_SCANNER_CACHE.clear()
        return JSONResponse({"status": "success", "ai_state": AI_STATE, "details": res})
    return JSONResponse({"status": "error", "message": res.get("msg", "Chyba trenovania AI.")})

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
        SETTINGS["use_anti_chase"] = bool(payload.get("use_anti_chase", SETTINGS["use_anti_chase"]))
        SETTINGS["max_candle_pump_pct"] = max(0.10, min(5.0, float(payload.get("max_candle_pump_pct", SETTINGS["max_candle_pump_pct"]))))
        SETTINGS["max_ema9_dist_pct"] = max(0.10, min(5.0, float(payload.get("max_ema9_dist_pct", SETTINGS["max_ema9_dist_pct"]))))
        SETTINGS["use_ai_filter"] = bool(payload.get("use_ai_filter", SETTINGS["use_ai_filter"]))
        SETTINGS["ai_min_prob"] = max(0.25, min(0.90, float(payload.get("ai_min_prob", SETTINGS["ai_min_prob"]))))

        MARKET_SCANNER_CACHE.clear()
        save_persistent_data()
        log_event(
            f"Nastavenia aktualizovane: Rezim={mode_val}, AI={SETTINGS['use_ai_filter']}(min={SETTINGS['ai_min_prob']*100:.0f}%), "
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
                return JSONResponse({"status": "error", "message": "Bot je pozastaveny."})

            coin_upper = coin.upper().strip()
            max_allowed = SETTINGS.get("max_parallel", 1)
            if len(ACTIVE_TRADES) >= max_allowed or coin_upper in ACTIVE_TRADES:
                return JSONResponse({"status": "error", "message": "Dosiahnuty limit paralelnych pozicii alebo pozicia uz existuje."})

            okx_symbol = f"{coin_upper}-USDC"
            if okx_symbol in EXCLUDED_SYMBOLS:
                return JSONResponse({"status": "error", "message": f"Par {okx_symbol} je na zozname vylucenych parov."})

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
                err_msg = order_res.get("msg", "Neznama chyba OKX")
                return JSONResponse({"status": "error", "message": f"OKX odmietlo prikaz ({err_msg})"})

            entry_features = get_current_ai_features_for_symbol(okx_symbol, bar_tf=CURRENT_TIMEFRAME.lower())
            ai_prob = predict_ai_probability(entry_features) if entry_features is not None else None

            coin_amount = float(formatted_sz)
            actual_spent_usdc = coin_amount * current_price
            fixed_sl_pct = SETTINGS.get("fixed_sl_pct", 2.0)
            fixed_sl_price = current_price * (1.0 - (fixed_sl_pct / 100.0))
            use_fixed_tp = bool(SETTINGS.get("use_fixed_tp", False))
            fixed_tp_pct = float(SETTINGS.get("fixed_tp_pct", 0.9))
            fixed_tp_price = current_price * (1.0 + (fixed_tp_pct / 100.0))

            ACTIVE_TRADES[coin_upper] = {
                "entry_price": current_price,
                "current_price": current_price,
                "fixed_sl_price": fixed_sl_price,
                "fixed_sl_pct": fixed_sl_pct,
                "use_fixed_tp": use_fixed_tp,
                "fixed_tp_price": fixed_tp_price,
                "fixed_tp_pct": fixed_tp_pct,
                "be_active": False,
                "ttp_active": False,
                "ttp_stop_price": None,
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
                "ai_prob": ai_prob,
                "entry_features": entry_features,
                "source": "MANUAL",
            }
            tp_info = f" | Fixny TP: +{fixed_tp_pct}%" if use_fixed_tp else ""
            ai_info = f" | AI: {ai_prob*100:.1f}%" if ai_prob is not None else ""
            log_event(f"Manualny nakup [{CURRENT_TIMEFRAME}]: {okx_symbol} za {current_price:.4f} | Investicia: {actual_spent_usdc:.2f}$ | Fixny SL: -{fixed_sl_pct}%{tp_info}{ai_info}", persist=True)
            send_telegram_msg(f"<b>MANUALNY NAKUP [TF: {CURRENT_TIMEFRAME}]</b>\n<b>Par:</b> {okx_symbol}\n<b>Vstup:</b> {current_price:.4f}\n<b>Investicia:</b> {actual_spent_usdc:.2f} {ACTIVE_BALANCE_CCY}\n<b>Fixny SL:</b> -{fixed_sl_pct}%" + (f"\n<b>Fixny TP:</b> +{fixed_tp_pct}%" if use_fixed_tp else ""))

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
        await close_trade_position(coin, "Rucny okamzity predaj (Emergency)")
        sold.append(symbol)
    return JSONResponse({"status": "success", "sold": sold})

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
                    <span id="header-pair">Top 20 USDC · OKX Demo (Live Grafy)</span>
                    <span>Zostatok: <span id="header-balance" class="balance-tag">-- USDC</span></span>
                    <span>Aktivny TF: <span id="header-tf" class="balance-tag">{{ CURRENT_TIMEFRAME }}</span></span>
                    <span>Fixny SL: <span id="header-sl" class="balance-tag">-{{ SETTINGS.fixed_sl_pct }}%</span></span>
                    <span>AI Model: <span id="header-ai" class="balance-tag">Nacitavam...</span></span>
                    <span>AI Pamat (ulozene): <span id="header-ai-learned" class="balance-tag">0 obchodov (0W / 0L)</span></span>
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
                    {% if IS_BOT_ACTIVE %}ACTIVE{% else %}PAUSED{% endif %}
                </button>
                <button id="sell-btn" class="btn btn-sell" onclick="sellNow()" disabled>SELL</button>
            </div>
        </div>

        <div class="status-ticker">
            <div class="status-left">
                <div class="status-dot"></div>
                <span id="current-stage">{{ CURRENT_STAGE }}</span>
            </div>
            <span id="market-regime-badge" class="regime-badge regime-bull">BYCI TRH</span>
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
                <span>Nastavenia Pine Script Indikatora + AI Filter Kvality</span>
                <span id="pine-active-badge" class="text-yellow">REZIM: {{ SETTINGS.mode_choice }}</span>
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
                            <label for="pine-strict-buy">Prisnejsi filter BUY signalu</label>
                            <input type="checkbox" id="pine-strict-buy" class="pine-checkbox" {% if SETTINGS.strict_buy_filter %}checked{% endif %}>
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
                        <div class="pine-group-title">AI Predikcia & Filter Kvality</div>
                        <div class="pine-row">
                            <label for="pine-use-ai">Zapnut AI filter BUY</label>
                            <input type="checkbox" id="pine-use-ai" class="pine-checkbox" {% if SETTINGS.use_ai_filter %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="pine-ai-prob">Min. AI pravdepodobnost</label>
                            <input type="number" id="pine-ai-prob" class="pine-input" min="0.25" max="0.90" step="0.01" value="{{ SETTINGS.ai_min_prob }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-min-rel-vol">Min. relativny objem (x)</label>
                            <input type="number" id="pine-min-rel-vol" class="pine-input" min="0" max="5" step="0.05" value="{{ SETTINGS.min_rel_volume }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-block-sideways">Blokovat bocny trh (SIDEWAYS)</label>
                            <input type="checkbox" id="pine-block-sideways" class="pine-checkbox" {% if SETTINGS.block_sideways_buy %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="pine-use-anti-chase">Anti-Chase (nekupovat vrchol)</label>
                            <input type="checkbox" id="pine-use-anti-chase" class="pine-checkbox" {% if SETTINGS.use_anti_chase %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="pine-max-candle-pump">Max rast sviecky (%)</label>
                            <input type="number" id="pine-max-candle-pump" class="pine-input" min="0.10" max="5.0" step="0.05" value="{{ SETTINGS.max_candle_pump_pct }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-max-ema9-dist">Max vzdialenost nad EMA9 (%)</label>
                            <input type="number" id="pine-max-ema9-dist" class="pine-input" min="0.10" max="5.0" step="0.05" value="{{ SETTINGS.max_ema9_dist_pct }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-pivot-len">Pivot dlzka (Fib)</label>
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
                            <label for="pine-fixed-sl-pct">Fixny SL (%)</label>
                            <input type="number" id="pine-fixed-sl-pct" class="pine-input" min="0.1" max="50.0" step="0.1" value="{{ SETTINGS.fixed_sl_pct }}">
                        </div>
                        <div class="pine-row">
                            <label for="pine-use-fixed-tp">Zapnut Fixny TP</label>
                            <input type="checkbox" id="pine-use-fixed-tp" class="pine-checkbox" {% if SETTINGS.use_fixed_tp %}checked{% endif %}>
                        </div>
                        <div class="pine-row">
                            <label for="pine-fixed-tp-pct">Fixny TP (%)</label>
                            <input type="number" id="pine-fixed-tp-pct" class="pine-input" min="0.1" max="100.0" step="0.1" value="{{ SETTINGS.fixed_tp_pct }}">
                        </div>
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
                    </div>

                    <div class="pine-group">
                        <div class="pine-group-title">3.1 & 3.2. TTP & Heikin-Ashi Exit</div>
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
                    </div>
                </div>
                <div class="pine-footer">
                    <div style="display:flex; flex-direction:column; gap:3px;">
                        <span id="pine-save-status" class="pine-save-status">Pripravene – zmeny sa okamzite premietnu do BUY / SELL skenera.</span>
                        <span id="ai-learned-summary" style="font-size:0.68rem; color:var(--yellow); font-weight:600;">AI nastudovane a ulozene obchody: 0 (0W / 0L)</span>
                    </div>
                    <div style="display: flex; gap: 6px; flex-wrap: wrap;">
                        <button class="btn btn-yellow" onclick="trainAiNow()">TRENOVAT AI MODEL</button>
                        <button class="btn" onclick="resetPineDefaults()">OBNOVIT ZAKLAD</button>
                        <button class="btn btn-on" onclick="savePineSettings()">ULOZIT NASTAVENIA</button>
                    </div>
                </div>
            </div>
        </div>

        <div>
            <div class="section-title">Active Positions Matrix (Pine SELL, Fixny TP alebo Fixny SL)</div>
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
                        <tr><td colspan="7" style="text-align: center; color: var(--text-muted);">Skenujem trh pre Pine Script + AI BUY signaly...</td></tr>
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
                            <th>Token / Par</th>
                            <th>Cena</th>
                            <th>1. Objem. Momentum</th>
                            <th>2. Nakupny Impulz</th>
                            <th>3. Stav Obchodu</th>
                            <th>4. AI Skore</th>
                            <th>5. Fixny SL</th>
                            <th>6. Exekucny Pokyn</th>
                        </tr>
                    </thead>
                    <tbody id="scanner-tbody">
                        <tr><td colspan="8" style="text-align: center; color: var(--text-muted);">Nacitavam stav skenera...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div>
            <div class="section-title">
                <span>System Execution Logs (Auto-cistenie po 24h)</span>
                <span style="font-size:0.64rem; color:var(--text-muted);">Starsie ako 24h sa mazu automaticky</span>
            </div>
            <div class="logs-box" id="logs-container">
                <div class="log-line">[System Initialized] Pine BUY & SELL + AI Filter + Fixny SL pripraveny.</div>
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
            btn.innerText = data.is_bot_active ? "ACTIVE" : "PAUSED";
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
            statusEl.innerText = 'Trenujem AI model na historickych svieckach z OKX...';
            let res = await fetch('/api/train-ai', {method: 'POST'});
            let data = await res.json();
            if (data.status === 'success') {
                const lc = data.details.live_learned_count || 0;
                const lw = data.details.live_wins_count || 0;
                const ll = data.details.live_losses_count || 0;
                statusEl.style.color = 'var(--green)';
                statusEl.innerText = `AI natrenovane (${data.details.samples} vzoriek, presnost ${data.details.accuracy}%) | Nastudovane a ulozene obchody: ${lc} (${lw}W / ${ll}L).`;
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
            if (ps.use_anti_chase !== undefined) document.getElementById('pine-use-anti-chase').checked = !!ps.use_anti_chase;
            if (ps.max_candle_pump_pct !== undefined) document.getElementById('pine-max-candle-pump').value = ps.max_candle_pump_pct;
            if (ps.max_ema9_dist_pct !== undefined) document.getElementById('pine-max-ema9-dist').value = ps.max_ema9_dist_pct;
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
            document.getElementById('pine-active-badge').innerText = `REZIM: ${ps.mode_choice}`;
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
                use_anti_chase: document.getElementById('pine-use-anti-chase').checked,
                max_candle_pump_pct: parseFloat(document.getElementById('pine-max-candle-pump').value),
                max_ema9_dist_pct: parseFloat(document.getElementById('pine-max-ema9-dist').value),
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
                statusEl.innerText = 'Ulozene! Skener teraz pocita BUY a SELL podla novych nastaveni.';
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
                ai_min_prob: 0.35,
                min_rel_volume: 0.8,
                block_sideways_buy: false,
                use_anti_chase: false,
                max_candle_pump_pct: 1.5,
                max_ema9_dist_pct: 1.4,
                pivot_len: 2,
                fib_choice: "0.786",
                fixed_sl_pct: 2.0,
                use_fixed_tp: false,
                fixed_tp_pct: 0.9,
                use_trailing: true,
                trail_pct: 1.2,
                use_breakeven: true,
                be_trigger_pct: 0.6,
                use_trailing_tp: true,
                ttp_trigger_pct: 0.85,
                ttp_callback_pct: 0.3,
                use_ha_exit: false,
                ha_exit_consecutive: 3
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

                const ccy = data.balance_ccy || "USDC";
                document.getElementById('header-balance').innerText = data.balance.toFixed(2) + " " + ccy;
                document.getElementById('current-stage').innerText = data.current_stage;
                document.getElementById('header-tf').innerText = data.current_tf;
                document.getElementById('header-sl').innerText = `-${data.fixed_sl_pct}%`;

                if (data.ai_state) {
                    const sc = data.ai_state.samples_count || 0;
                    const wr = data.ai_state.win_rate_train || 0;
                    const lc = data.ai_state.live_learned_count || 0;
                    const lw = data.ai_state.live_wins_count || 0;
                    const ll = Math.max(0, lc - lw);
                    document.getElementById('header-ai').innerText = sc > 0 ? `${sc} vz. (${wr}%) | Ulozene: ${lc}` : "CAKA";
                    const hLearned = document.getElementById('header-ai-learned');
                    if (hLearned) hLearned.innerText = `${lc} obchodov (${lw}W / ${ll}L)`;
                    const sumEl = document.getElementById('ai-learned-summary');
                    if (sumEl) sumEl.innerText = `AI nastudovane a ulozene obchody: ${lc} (${lw}W / ${ll}L) | Celkovo vzoriek v pamati: ${sc}`;
                }

                if (data.pine_settings) {
                    document.getElementById('pine-active-badge').innerText = `REZIM: ${data.pine_settings.mode_choice}`;
                }

                let regimeBadge = document.getElementById('market-regime-badge');
                if (regimeBadge) {
                    const regime = data.market_regime || (data.is_sideways_market ? "SIDEWAYS" : "BULL");
                    regimeBadge.classList.remove('regime-bull', 'regime-bear', 'regime-sideways');
                    if (regime === "SIDEWAYS") {
                        regimeBadge.classList.add('regime-sideways');
                        regimeBadge.innerText = `BOCNY TRH (${data.current_tf})`;
                    } else if (regime === "BEAR") {
                        regimeBadge.classList.add('regime-bear');
                        regimeBadge.innerText = `MEDVEDI TRH (${data.current_tf})`;
                    } else {
                        regimeBadge.classList.add('regime-bull');
                        regimeBadge.innerText = `BYCI TRH (${data.current_tf})`;
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
                            : `<span style="color:var(--text-muted);">VYPNUTY</span>`;
                        html += `<tr>
                            <td><b>${t.market_pair}</b><br><span style="font-size:0.6rem; color:var(--text-muted);">TF: ${t.tf}</span></td>
                            <td>${t.entry_price.toFixed(4)}</td>
                            <td>${t.current_price.toFixed(4)}</td>
                            <td>${tpCell}</td>
                            <td class="text-red">${t.fixed_sl_price.toFixed(4)} (-${t.fixed_sl_pct}%)</td>
                            <td>${t.invested_usdc.toFixed(2)} ${ccy}</td>
                            <td class="${pnlClass}">${t.live_pnl_pct >= 0 ? "+" : ""}${t.live_pnl_pct.toFixed(2)}% (${t.live_pnl_usdc >= 0 ? "+" : ""}${t.live_pnl_usdc.toFixed(2)} $)</td>
                        </tr>`;
                    }
                    activeTbody.innerHTML = html;
                    sellBtn.disabled = false;
                } else {
                    activeTbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">Skenujem trh pre Pine Script + AI BUY signaly...</td></tr>`;
                    sellBtn.disabled = true;
                }

                let scannerTbody = document.getElementById('scanner-tbody');
                let scannerData = data.scanner_cache || {};
                if (Object.keys(scannerData).length > 0) {
                    let sHtml = "";
                    for (let [coin, info] of Object.entries(scannerData)) {
                        let momClass = info.momentum === "SILNE" ? "text-green" : "text-red";
                        let impClass = info.impulse.includes("AKTIVNY") ? "text-green" : "text-yellow";
                        let execClass = info.exec_signal.includes("NAKUPIT") ? "text-green" : (info.exec_signal.includes("PREDAT") || info.exec_signal.includes("AI BLOK") ? "text-red" : "text-yellow");

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
                    scannerTbody.innerHTML = `<tr><td colspan="8" style="text-align: center; color: var(--text-muted);">Prebieha uvodne skenovanie trhu...</td></tr>`;
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
    _prune_old_logs()
    balance = fetch_account_capital("USDC")
    stats = calculate_stats()
    ai_public = {k: v for k, v in AI_STATE.items() if not k.startswith("_")}
    return JSONResponse({
        "balance": balance,
        "balance_ccy": ACTIVE_BALANCE_CCY,
        "is_bot_active": IS_BOT_ACTIVE,
        "current_stage": CURRENT_STAGE,
        "current_tf": CURRENT_TIMEFRAME,
        "is_sideways_market": IS_SIDEWAYS_MARKET,
        "market_regime": MARKET_REGIME,
        "fixed_sl_pct": SETTINGS.get("fixed_sl_pct", 2.0),
        "pine_settings": get_pine_settings_dict(),
        "ai_state": ai_public,
        "stats": stats,
        "active_trades": ACTIVE_TRADES,
        "scanner_cache": MARKET_SCANNER_CACHE,
        "history": CLOSED_TRADES_HISTORY[:20],
        "logs": SYSTEM_LOGS,
    })

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
