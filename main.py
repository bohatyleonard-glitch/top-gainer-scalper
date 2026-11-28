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
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Template
import uvicorn

# --- OKX KLUCE & BEZPECNOST ---

OKX_API_KEY = os.getenv("OKX_API_KEY", "54ebe9fe-1fd0-443d-8f76-0cd1a3439ddb").strip()
OKX_SECRET = os.getenv("OKX_SECRET", "FBD5C8332D88E3ED5662470CA9C3E813").strip()
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "Novyzeland111/").strip()

_DEFAULT_TG_TOKEN = "9158932827:AAHvZT_gQrVH6dk54BRx8_mxTFtkBud0tUI"
_DEFAULT_TG_CHAT_ID = "9130979784"

# --- TELEGRAM NOTIFIKACIE & INTERAKTIVNY BOT ---

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", _DEFAULT_TG_TOKEN).strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", _DEFAULT_TG_CHAT_ID).strip()
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()

# Sukromne poziadavky (zostatok, nakup, predaj) -> OKX Demo EEA
_env_demo_url = os.getenv("OKX_DEMO_BASE_URL", os.getenv("OKX_BASE_URL", "https://eea.okx.com")).strip().rstrip("/")
OKX_DEMO_BASE_URL = "https://eea.okx.com" if (not _env_demo_url or "www.okx.com" in _env_demo_url) else _env_demo_url
# Verejne trhove data (grafy/sviecky, tickery, instrumenty) -> OKX Live
OKX_PUBLIC_URL = os.getenv("OKX_PUBLIC_URL", "https://www.okx.com").strip().rstrip("/")
OKX_SIMULATED = os.getenv("OKX_SIMULATED", "1").strip()


def _resolve_state_file_path() -> str:
    custom_path = os.getenv("BOT_STATE_FILE", "").strip()
    if custom_path:
        return custom_path
    try:
        os.makedirs("/var/data", exist_ok=True)
        test_p = "/var/data/.write_test"
        with open(test_p, "w", encoding="utf-8") as tf:
            tf.write("ok")
        os.remove(test_p)
        return "/var/data/apex_ai_bot_state.json"
    except Exception:
        return "apex_ai_bot_state.json"


STATE_FILE = _resolve_state_file_path()
LOG_RETENTION_SEC = 2 * 3600
LOG_24H_RETENTION_SEC = 24 * 3600
BALANCE_CACHE_TTL_SEC = 15.0
_BALANCE_429_BACKOFF_UNTIL = 0.0


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
ALLOWED_TIMEFRAMES = ["5m", "8m", "10m", "15m", "20m", "30m", "45m", "2h", "4h"]
ALLOWED_BUDGET_CAPS = [500, 1000, 2000, 3000, 5000, 10000]

# Pary, ktore demo ucet nepodporuje alebo ich nechces obchodovat
EXCLUDED_SYMBOLS = {
    "BTC-USDC", "ETH-USDC",
    "USDT-USDC", "DAI-USDC", "FDUSD-USDC",
    "ZEC-USDC", "ENA-USDC", "LIT-USDC", "CT-USDC",
    "XMSTR-USDC", "XSOXL-USDC", "XANT-USDC",
    "HYPE-USDC", "TAO-USDC", "XRP-USDC",
    "XNVDA-USDC", "RENDER-USDC", "XVID-USDC", "PEPE-USDC",
    "XSKHY-USDC",
}
DEMO_REJECTED_COOLDOWN: dict[str, float] = {}
DEMO_REJECTED_COOLDOWN_SEC = 1800.0

AI_FEATURE_COUNT = 9

# --- GLOBALNY STAV BOTA ---
IS_BOT_ACTIVE = True
CURRENT_TIMEFRAME = "15M"
CURRENT_STAGE = "STAV: Cisty start bota (Pine logika + nastavenia z APEX panela)..."
MARKET_REGIME = "BULL"
IS_SIDEWAYS_MARKET = False
LAST_SIDEWAYS_ALERT_TS = 0.0
SIDEWAYS_ALERT_COOLDOWN_SEC = 1800
LAST_BALANCE_ERROR_LOG_TS = 0.0
LAST_BLOCK_LOG_TS: dict[str, float] = {}
ACTIVE_BALANCE_CCY = "USDC"
_BALANCE_CACHE: dict[str, tuple[float, float, str]] = {}
LAST_LOG_PRUNE_STR = "--"

INITIAL_TOP_SYMBOLS = [
    "SOL-USDC", "SUI-USDC", "DOGE-USDC", "ADA-USDC",
    "AVAX-USDC", "LINK-USDC", "TON-USDC", "NEAR-USDC",
    "APT-USDC", "TRX-USDC", "LTC-USDC", "PEPE-USDC",
    "AAVE-USDC", "UNI-USDC", "POL-USDC", "TIA-USDC",
    "PUMP-USDC"
]
TOP_LIQUID_SYMBOLS: list[str] = [s for s in INITIAL_TOP_SYMBOLS if s not in EXCLUDED_SYMBOLS]

ACTIVE_TRADES: dict[str, dict] = {}
LAST_TRADE_TIME: dict[str, float] = {}
WAIT_FOR_SIGNAL_RESET: dict[str, bool] = {}
WAITING_FOR_SELL_AFTER_EXIT: dict[str, bool] = {}
CLOSED_TRADES_HISTORY: list[dict] = []
AI_LEARNED_TRADES: list[dict] = []
MARKET_SCANNER_CACHE: dict[str, dict] = {}
SYSTEM_LOG_ENTRIES: list[dict] = []
SYSTEM_LOG_24H_ENTRIES: list[dict] = []
SYSTEM_LOGS: list[str] = []
trade_lock = asyncio.Lock()

SETTINGS = {
    "max_parallel": 4,
    "max_budget_cap": 1000.0,
    "compounding_enabled": False,
    "use_btc_filter": False,
    "reentry_cooldown_sec": 120,
    "require_sell_before_rebuy": False,
    "mode_choice": "FIB",
    "require_green_ha_buy": False,
    "require_bull_tf_buy": False,
    "strict_buy_filter": False,
    "fast_len": 5,
    "slow_len": 20,
    "signal_len": 9,
    "pred_look": 3,
    "pivot_len": 2,
    "fib_choice": "0.786",
    "use_fixed_sl": False,
    "fixed_sl_pct": 1.5,
    "use_fixed_tp": False,
    "fixed_tp_pct": 1.2,
    "use_trailing": True,
    "trail_pct": 1.5,
    "use_breakeven": True,
    "be_trigger_pct": 1.2,
    "use_trailing_tp": True,
    "ttp_trigger_pct": 1.2,
    "ttp_callback_pct": 0.4,
    "use_ha_exit": True,
    "ha_exit_consecutive": 1,
    "sell_on_osc_reversal": False,
    "block_sideways_buy": False,
    "min_rel_volume": 0.0,
    "use_anti_chase": False,
    "max_candle_pump_pct": 0.9,
    "max_ema9_dist_pct": 0.7,
    "max_upper_wick_ratio": 0.45,
    "max_entry_slip_pct": 0.35,
    "use_ai_filter": False,
    "ai_min_prob": 0.38,
    "simulate_fees": True,
    "taker_fee_pct": 0.10,
}

AI_STATE = {
    "weights": [0.0] * AI_FEATURE_COUNT,
    "bias": 0.0,
    "samples_count": 0,
    "live_learned_count": 0,
    "live_wins_count": 0,
    "win_rate_train": 0.0,
    "accuracy_train": 0.0,
    "trained_at": "--",
    "last_symbol": "--",
    "last_prob": None,
    "last_factors": "--",
    "blocked_count": 0,
    "passed_count": 0,
    "_last_train_ts": 0.0,
}


def safe_float(val, default: float = 0.0) -> float:
    try:
        if val is None or val == "":
            return default
        num = float(val)
        return default if (math.isnan(num) or math.isinf(num)) else num
    except Exception:
        return default


def get_sk_time_str() -> str:
    try:
        sk_tz = ZoneInfo("Europe/Bratislava")
    except Exception:
        sk_tz = timezone(timedelta(hours=1))
    return datetime.now(sk_tz).strftime("%H:%M:%S")


def _detect_log_category_and_symbol(msg: str) -> tuple[str, str]:
    upper = msg.upper()
    sym = "SYS"
    for token in TOP_LIQUID_SYMBOLS + ["BTC-USDC", "ETH-USDC"]:
        base = token.split("-")[0]
        if token in upper or f" {base} " in f" {upper} " or f" {base}-" in f" {upper} ":
            sym = token
            break
    if any(k in upper for k in ["CHYBA", "ZLYHAL", "ODMIETNUTY", "VYNIMKA", "POZOR"]):
        cat = "ERROR"
    elif any(k in upper for k in ["NAKUP", "UZAVRETA POZICIA", "PREDAJ", "SIGNAL 'BUY'", "SIGNAL 'SELL'"]):
        cat = "TRADE"
    elif any(k in upper for k in ["BLOK", "POZASTAVENY", "ZABLOKOVANY", "PAUZA"]):
        cat = "FILTER"
    else:
        cat = "INFO"
    return cat, sym


def _prune_old_logs(now_ts: float | None = None):
    global LAST_LOG_PRUNE_STR
    ref_ts = time.time() if now_ts is None else now_ts
    cutoff_2h = ref_ts - LOG_RETENTION_SEC
    cutoff_24h = ref_ts - LOG_24H_RETENTION_SEC
    before_len = len(SYSTEM_LOG_ENTRIES)
    SYSTEM_LOG_ENTRIES[:] = [
        entry for entry in SYSTEM_LOG_ENTRIES
        if safe_float(entry.get("ts"), 0.0) >= cutoff_2h
    ]
    SYSTEM_LOG_24H_ENTRIES[:] = [
        entry for entry in SYSTEM_LOG_24H_ENTRIES
        if safe_float(entry.get("ts"), 0.0) >= cutoff_24h
    ]
    if len(SYSTEM_LOG_ENTRIES) != before_len:
        LAST_LOG_PRUNE_STR = get_sk_time_str()
    SYSTEM_LOGS[:] = [entry["line"] for entry in SYSTEM_LOG_ENTRIES[:200]]


def log_event(msg: str, persist: bool = False):
    now_ts = time.time()
    ts = get_sk_time_str()
    line = f"[{ts}] {msg}"
    cat, sym = _detect_log_category_and_symbol(msg)
    entry_obj = {
        "ts": now_ts,
        "time": ts,
        "category": cat,
        "symbol": sym,
        "message": msg,
        "line": line,
    }
    SYSTEM_LOG_ENTRIES.insert(0, entry_obj)
    if len(SYSTEM_LOG_ENTRIES) > 500:
        del SYSTEM_LOG_ENTRIES[500:]
    SYSTEM_LOG_24H_ENTRIES.insert(0, entry_obj)
    if len(SYSTEM_LOG_24H_ENTRIES) > 1500:
        del SYSTEM_LOG_24H_ENTRIES[1500:]
    _prune_old_logs(now_ts)
    print(line, flush=True)
    if persist:
        save_persistent_data()


def log_block_throttled(symbol: str, reason: str, cooldown_sec: float = 180.0):
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


def _http_get_json_sync(url: str, headers: dict | None = None) -> dict:
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


_CUSTOM_BAR_AGGREGATION = {
    "10m": ("5m", 2),
    "20m": ("5m", 4),
}


def _aggregate_okx_candles(raw_candles: list, factor: int) -> list:
    if not raw_candles or factor <= 1:
        return raw_candles
    asc = list(reversed(raw_candles))
    usable_len = (len(asc) // factor) * factor
    if usable_len < factor:
        return []
    asc = asc[-usable_len:]
    grouped = []
    for i in range(0, len(asc), factor):
        chunk = asc[i:i + factor]
        first_c = chunk[0]
        last_c = chunk[-1]
        ts_val = str(first_c[0])
        o_val = safe_float(first_c[1])
        h_val = max(safe_float(c[2]) for c in chunk)
        l_val = min(safe_float(c[3]) for c in chunk)
        c_val = safe_float(last_c[4])
        v_val = sum(safe_float(c[5]) for c in chunk)
        vol_ccy_val = sum(safe_float(c[6]) if len(c) > 6 else 0.0 for c in chunk)
        vol_ccy_quote_val = sum(safe_float(c[7]) if len(c) > 7 else 0.0 for c in chunk)
        confirm_val = str(last_c[8]) if len(last_c) > 8 else "1"
        grouped.append([
            ts_val,
            f"{o_val:.8f}".rstrip("0").rstrip(".") or "0",
            f"{h_val:.8f}".rstrip("0").rstrip(".") or "0",
            f"{l_val:.8f}".rstrip("0").rstrip(".") or "0",
            f"{c_val:.8f}".rstrip("0").rstrip(".") or "0",
            f"{v_val:.8f}".rstrip("0").rstrip(".") or "0",
            f"{vol_ccy_val:.8f}".rstrip("0").rstrip(".") or "0",
            f"{vol_ccy_quote_val:.8f}".rstrip("0").rstrip(".") or "0",
            confirm_val,
        ])
    return list(reversed(grouped))


def okx_public_request_sync(path_and_query: str) -> dict:
    parsed_url = urllib.parse.urlparse(path_and_query)
    qs = urllib.parse.parse_qs(parsed_url.query)
    bar_val = (qs.get("bar", [""])[0] or "").lower().strip()

    for base in [OKX_PUBLIC_URL, "https://www.okx.com", "https://aws.okx.com"]:
        base_clean = base.strip().rstrip("/")
        if not base_clean:
            continue
        res = _http_get_json_sync(f"{base_clean}{path_and_query}")
        if res.get("code") == "0" and res.get("data"):
            return res

    if bar_val in _CUSTOM_BAR_AGGREGATION and parsed_url.path.endswith("/api/v5/market/candles"):
        src_bar, factor = _CUSTOM_BAR_AGGREGATION[bar_val]
        orig_limit = int(qs.get("limit", ["120"])[0] or "120")
        need_limit = min(300, max(60, orig_limit * factor + factor))
        new_q = dict(qs)
        new_q["bar"] = [src_bar]
        new_q["limit"] = [str(need_limit)]
        flat_pairs = [(k, v_item) for k, v_list in new_q.items() for v_item in v_list]
        fallback_path = f"{parsed_url.path}?{urllib.parse.urlencode(flat_pairs)}"
        for base in [OKX_PUBLIC_URL, "https://www.okx.com", "https://aws.okx.com"]:
            base_clean = base.strip().rstrip("/")
            if not base_clean:
                continue
            fb_res = _http_get_json_sync(f"{base_clean}{fallback_path}")
            if fb_res.get("code") == "0" and fb_res.get("data"):
                agg_data = _aggregate_okx_candles(fb_res["data"], factor)
                if agg_data:
                    return {"code": "0", "msg": "", "data": agg_data[:orig_limit]}

    return _http_get_json_sync(f"{OKX_PUBLIC_URL}{path_and_query}")


async def okx_public_request(path_and_query: str) -> dict:
    return await asyncio.to_thread(okx_public_request_sync, path_and_query)


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


_LAST_503_FALLBACK_LOG_TS = 0.0


def _okx_single_base_request_sync(base_url: str, method: str, request_path: str, body: str = "") -> dict:
    target_base = base_url.strip().rstrip("/")
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


def okx_request_sync(method: str, request_path: str, body: str = "") -> dict:
    global _LAST_503_FALLBACK_LOG_TS
    if not OKX_API_KEY or not OKX_SECRET or not OKX_PASSPHRASE:
        return {"code": "-1", "msg": "Chybaju OKX API kluce", "data": []}

    primary_base = OKX_DEMO_BASE_URL or "https://eea.okx.com"
    fallback_bases = [b for b in ["https://www.okx.com", "https://eea.okx.com"] if b.rstrip("/") != primary_base.rstrip("/")]

    first_res = _okx_single_base_request_sync(primary_base, method, request_path, body)
    if first_res.get("code") == "0":
        return first_res

    is_transient_503 = str(first_res.get("code")) in {"503", "-1"} or "503" in str(first_res.get("msg", ""))
    if is_transient_503:
        for fb in fallback_bases:
            fb_res = _okx_single_base_request_sync(fb, method, request_path, body)
            if fb_res.get("code") == "0":
                now_ts = time.time()
                if now_ts - _LAST_503_FALLBACK_LOG_TS >= 300:
                    _LAST_503_FALLBACK_LOG_TS = now_ts
                    log_event(f"OKX {primary_base} vratil 503 - automaticky prepnute na zalozny {fb} (Demo).")
                return fb_res

    return first_res


async def okx_request(method: str, request_path: str, body: str = "") -> dict:
    return await asyncio.to_thread(okx_request_sync, method, request_path, body)


def fetch_account_capital_sync(ccy: str = "USDC", force_refresh: bool = False) -> float:
    global LAST_BALANCE_ERROR_LOG_TS, ACTIVE_BALANCE_CCY, _BALANCE_429_BACKOFF_UNTIL
    preferred = str(ccy).upper()
    now_ts = time.time()
    cached = _BALANCE_CACHE.get(preferred)
    if not force_refresh and cached is not None and (now_ts - cached[0]) < BALANCE_CACHE_TTL_SEC:
        ACTIVE_BALANCE_CCY = cached[2]
        return cached[1]
    if not force_refresh and cached is not None and now_ts < _BALANCE_429_BACKOFF_UNTIL:
        ACTIVE_BALANCE_CCY = cached[2]
        return cached[1]
    try:
        res = okx_request_sync("GET", "/api/v5/account/balance")
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

            if balances_by_ccy.get(preferred, 0.0) > 0:
                ACTIVE_BALANCE_CCY = preferred
                val = balances_by_ccy[preferred]
                _BALANCE_CACHE[preferred] = (now_ts, val, ACTIVE_BALANCE_CCY)
                return val

            for fallback_ccy in ["USDC", "USDT"]:
                if balances_by_ccy.get(fallback_ccy, 0.0) > 0:
                    ACTIVE_BALANCE_CCY = fallback_ccy
                    val = balances_by_ccy[fallback_ccy]
                    _BALANCE_CACHE[preferred] = (now_ts, val, ACTIVE_BALANCE_CCY)
                    return val

            ACTIVE_BALANCE_CCY = preferred
            _BALANCE_CACHE[preferred] = (now_ts, 0.0, ACTIVE_BALANCE_CCY)
            return 0.0

        err_code = str(res.get("code", ""))
        err_msg = str(res.get("msg", ""))
        is_rate_or_busy = err_code in {"429", "503", "50013"} or any(k in err_msg for k in ["429", "503", "50013", "Too Many Requests", "System is busy"])
        if is_rate_or_busy:
            _BALANCE_429_BACKOFF_UNTIL = now_ts + 25.0
            if cached is not None:
                ACTIVE_BALANCE_CCY = cached[2]
                if now_ts - LAST_BALANCE_ERROR_LOG_TS >= 300:
                    LAST_BALANCE_ERROR_LOG_TS = now_ts
                    log_event(f"OKX zostatok docasne zatazeny ({err_code}) - pouzivam posledny zostatok {cached[1]:.2f} {cached[2]}.")
                return cached[1]

        if now_ts - LAST_BALANCE_ERROR_LOG_TS >= 300:
            LAST_BALANCE_ERROR_LOG_TS = now_ts
            log_event(f"OKX zostatok chyba (code={res.get('code')}): {res.get('msg')}")
    except Exception as err:
        if now_ts - LAST_BALANCE_ERROR_LOG_TS >= 300:
            LAST_BALANCE_ERROR_LOG_TS = now_ts
            log_event(f"Chyba pri nacitani zostatku: {err}")
    return cached[1] if cached is not None else 0.0


async def fetch_account_capital(ccy: str = "USDC", force_refresh: bool = False) -> float:
    return await asyncio.to_thread(fetch_account_capital_sync, ccy, force_refresh)


def get_instrument_rules_sync(inst_id: str) -> tuple[float, float]:
    try:
        res = okx_public_request_sync(f"/api/v5/public/instruments?instType=SPOT&instId={inst_id}")
        if res.get("code") == "0" and res.get("data"):
            d = res["data"][0]
            lot_sz = safe_float(d.get("lotSz"), 0.01)
            min_sz = safe_float(d.get("minSz"), 0.01)
            return max(1e-8, lot_sz), max(1e-8, min_sz)
    except Exception:
        pass
    return 0.01, 0.01


async def get_instrument_rules(inst_id: str) -> tuple[float, float]:
    return await asyncio.to_thread(get_instrument_rules_sync, inst_id)


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


def classify_trade_style(tf_str: str, source: str = "AUTO") -> str:
    if str(source).upper() == "MANUAL":
        return "MANUAL"
    tf_clean = str(tf_str).lower().strip()
    if tf_clean in ("30m", "45m", "1h", "2h", "4h"):
        return "SWING"
    return "SCALPING"


def calc_simulated_fee_metrics(invested_usdc: float, exit_value_usdc: float) -> tuple[float, float, float]:
    if not SETTINGS.get("simulate_fees", True):
        return 0.0, 0.0, 0.0
    fee_rate = max(0.0, safe_float(SETTINGS.get("taker_fee_pct"), 0.10)) / 100.0
    entry_fee = invested_usdc * fee_rate
    exit_fee = exit_value_usdc * fee_rate
    return round(entry_fee, 4), round(exit_fee, 4), round(entry_fee + exit_fee, 4)


def record_closed_trade(
    symbol: str,
    pnl_pct: float,
    pnl_usdc: float,
    reason: str,
    entry_features: list[float] | None = None,
    trade_style: str = "SCALPING",
    gross_pnl_pct: float | None = None,
    gross_pnl_usdc: float | None = None,
    fee_usdc: float = 0.0,
    persist: bool = False,
):
    g_pct = pnl_pct if gross_pnl_pct is None else gross_pnl_pct
    g_usdc = pnl_usdc if gross_pnl_usdc is None else gross_pnl_usdc
    result_badge = "🟩" if pnl_usdc >= 0 else "🟥"
    trade_rec = {
        "time": get_sk_time_str(),
        "symbol": symbol,
        "result_badge": result_badge,
        "pnl_pct": round(pnl_pct, 2),
        "pnl_usdc": round(pnl_usdc, 2),
        "gross_pnl_pct": round(g_pct, 2),
        "gross_pnl_usdc": round(g_usdc, 2),
        "fee_usdc": round(fee_usdc, 4),
        "reason": reason,
        "style": trade_style,
        "entry_features": entry_features,
    }
    CLOSED_TRADES_HISTORY.insert(0, trade_rec)
    if len(CLOSED_TRADES_HISTORY) > 200:
        del CLOSED_TRADES_HISTORY[200:]
    if entry_features and len(entry_features) >= 6:
        AI_LEARNED_TRADES.insert(0, {
            "time": trade_rec["time"],
            "symbol": symbol,
            "result_badge": result_badge,
            "pnl_pct": trade_rec["pnl_pct"],
            "pnl_usdc": trade_rec["pnl_usdc"],
            "gross_pnl_pct": trade_rec["gross_pnl_pct"],
            "gross_pnl_usdc": trade_rec["gross_pnl_usdc"],
            "fee_usdc": trade_rec["fee_usdc"],
            "reason": reason,
            "style": trade_style,
            "entry_features": [round(safe_float(x), 5) for x in entry_features],
        })
        if len(AI_LEARNED_TRADES) > 200:
            del AI_LEARNED_TRADES[200:]
    record_trade_pattern_experience(
        symbol=symbol,
        pnl_pct=pnl_pct,
        pnl_usdc=pnl_usdc,
        exit_reason=reason,
        features=entry_features,
        tf_str=CURRENT_TIMEFRAME,
        mode_str=SETTINGS.get("mode_choice", "FIB"),
    )
    if persist:
        save_persistent_data()


def calculate_stats() -> dict:
    total = len(CLOSED_TRADES_HISTORY)
    wins = sum(1 for t in CLOSED_TRADES_HISTORY if safe_float(t.get("pnl_usdc")) > 0)
    losses = total - wins
    total_pnl = sum(safe_float(t.get("pnl_usdc")) for t in CLOSED_TRADES_HISTORY)
    gross_pnl = sum(safe_float(t.get("gross_pnl_usdc"), safe_float(t.get("pnl_usdc"))) for t in CLOSED_TRADES_HISTORY)
    total_fees = sum(safe_float(t.get("fee_usdc"), 0.0) for t in CLOSED_TRADES_HISTORY)
    win_rate = (wins / total * 100.0) if total > 0 else 0.0
    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "total_pnl": round(total_pnl, 2),
        "gross_pnl": round(gross_pnl, 2),
        "total_fees": round(total_fees, 2),
        "win_rate": round(win_rate, 1),
    }


PATTERN_MEMORY: dict[str, dict] = {}
LEARNING_READINESS_TARGET_PCT = 80.0
READINESS_FULL_TRADES_BENCHMARK = 200
MIN_MATURE_TRADES_FOR_AI = 30
MIN_MATURE_ACC_FOR_AI = 55.0
_LAST_PERIODIC_TRAIN_TS = 0.0


def _pattern_context_key(tf_str: str | None = None, mode_str: str | None = None) -> str:
    tf_part = str(tf_str or CURRENT_TIMEFRAME or "15M").upper().strip()
    mode_part = str(mode_str or SETTINGS.get("mode_choice", "FIB") or "FIB").upper().strip()
    return f"{tf_part}_{mode_part}"


def _ensure_context_pattern_bucket(ctx_key: str) -> dict:
    if ctx_key not in PATTERN_MEMORY or not isinstance(PATTERN_MEMORY[ctx_key], dict):
        PATTERN_MEMORY[ctx_key] = {
            "buy_patterns": {},
            "sell_patterns": {},
            "total_records": 0,
            "wins": 0,
            "losses": 0,
        }
    bucket = PATTERN_MEMORY[ctx_key]
    bucket.setdefault("buy_patterns", {})
    bucket.setdefault("sell_patterns", {})
    bucket["total_records"] = int(bucket.get("total_records", 0))
    bucket["wins"] = int(bucket.get("wins", 0))
    bucket["losses"] = int(bucket.get("losses", 0))
    return bucket


def _build_entry_pattern_signature(features: list[float] | None, tf_str: str, mode_str: str) -> str:
    regime_lbl = "SIDEWAYS" if IS_SIDEWAYS_MARKET else str(MARKET_REGIME).upper()
    if not features or len(features) < 6:
        return f"{str(tf_str).upper()}|{str(mode_str).upper()}|{regime_lbl}"
    mom_dir = "UP" if safe_float(features[2]) >= 0 else "DN"
    vol_dir = "HI" if safe_float(features[1]) >= 0 else "LO"
    ha_dir = "GRN" if safe_float(features[5]) >= 0 else "RED"
    return f"{str(tf_str).upper()}|{str(mode_str).upper()}|{regime_lbl}|OSC:{mom_dir}|VOL:{vol_dir}|HA:{ha_dir}"


def _build_exit_pattern_signature(reason: str, tf_str: str, mode_str: str) -> str:
    r_clean = str(reason or "EXIT").strip()
    return f"{str(tf_str).upper()}|{str(mode_str).upper()}|EXIT:{r_clean}"


def record_trade_pattern_experience(
    symbol: str,
    pnl_pct: float,
    pnl_usdc: float,
    exit_reason: str,
    features: list[float] | None,
    tf_str: str | None = None,
    mode_str: str | None = None,
):
    tf_use = str(tf_str or CURRENT_TIMEFRAME).upper().strip()
    mode_use = str(mode_str or SETTINGS.get("mode_choice", "FIB")).upper().strip()
    ctx_key = _pattern_context_key(tf_use, mode_use)
    bucket = _ensure_context_pattern_bucket(ctx_key)

    buy_sig = _build_entry_pattern_signature(features, tf_use, mode_use)
    sell_sig = _build_exit_pattern_signature(exit_reason, tf_use, mode_use)
    is_win = safe_float(pnl_usdc) > 0
    pnl_v = safe_float(pnl_usdc)
    pct_v = safe_float(pnl_pct)

    b_rec = bucket["buy_patterns"].setdefault(buy_sig, {"count": 0, "wins": 0, "losses": 0, "sum_pnl": 0.0, "sum_pct": 0.0})
    b_rec["count"] = int(b_rec.get("count", 0)) + 1
    b_rec["wins"] = int(b_rec.get("wins", 0)) + (1 if is_win else 0)
    b_rec["losses"] = int(b_rec.get("losses", 0)) + (0 if is_win else 1)
    b_rec["sum_pnl"] = round(safe_float(b_rec.get("sum_pnl")) + pnl_v, 4)
    b_rec["sum_pct"] = round(safe_float(b_rec.get("sum_pct")) + pct_v, 4)

    s_rec = bucket["sell_patterns"].setdefault(sell_sig, {"count": 0, "wins": 0, "losses": 0, "sum_pnl": 0.0, "sum_pct": 0.0})
    s_rec["count"] = int(s_rec.get("count", 0)) + 1
    s_rec["wins"] = int(s_rec.get("wins", 0)) + (1 if is_win else 0)
    s_rec["losses"] = int(s_rec.get("losses", 0)) + (0 if is_win else 1)
    s_rec["sum_pnl"] = round(safe_float(s_rec.get("sum_pnl")) + pnl_v, 4)
    s_rec["sum_pct"] = round(safe_float(s_rec.get("sum_pct")) + pct_v, 4)

    bucket["total_records"] = int(bucket.get("total_records", 0)) + 1
    if is_win:
        bucket["wins"] = int(bucket.get("wins", 0)) + 1
    else:
        bucket["losses"] = int(bucket.get("losses", 0)) + 1


def compute_ai_readiness_info() -> dict:
    ctx_key = _pattern_context_key(CURRENT_TIMEFRAME, SETTINGS.get("mode_choice", "FIB"))
    bucket = _ensure_context_pattern_bucket(ctx_key)
    ctx_trades = int(bucket.get("total_records", 0))
    total_learned = max(int(AI_STATE.get("live_learned_count", 0)), len(AI_LEARNED_TRADES), len(CLOSED_TRADES_HISTORY))
    effective_trades = max(ctx_trades, total_learned)

    sample_progress = min(1.0, effective_trades / float(READINESS_FULL_TRADES_BENCHMARK))
    model_samples = int(AI_STATE.get("samples_count", 0))
    sample_depth_bonus = min(0.15, model_samples / 2000.0)
    raw_readiness_pct = min(100.0, (sample_progress * 0.85 + sample_depth_bonus) * 100.0)
    readiness_pct = round(raw_readiness_pct, 1)

    target_trades_for_80 = int(math.ceil(READINESS_FULL_TRADES_BENCHMARK * (LEARNING_READINESS_TARGET_PCT / 100.0)))
    remaining_trades_to_80 = max(0, target_trades_for_80 - effective_trades)

    is_ready_80 = readiness_pct >= LEARNING_READINESS_TARGET_PCT
    is_mature_for_filter = (effective_trades >= MIN_MATURE_TRADES_FOR_AI) and (safe_float(AI_STATE.get("accuracy_train"), 0.0) >= MIN_MATURE_ACC_FOR_AI)

    if is_ready_80 and is_mature_for_filter:
        advisor_mode = "AKTIVNY PORADCA"
    elif readiness_pct >= 40.0:
        advisor_mode = "DOUCUJE SA (ORIENTACNY TIP)"
    else:
        advisor_mode = "ZBIERA VZORY (LEN POZOROVATEL)"

    top_buy_patterns = []
    for sig_k, rec in sorted(bucket.get("buy_patterns", {}).items(), key=lambda item: int(item[1].get("count", 0)), reverse=True)[:3]:
        cnt = int(rec.get("count", 0))
        w_cnt = int(rec.get("wins", 0))
        wr = round((w_cnt / cnt) * 100.0, 1) if cnt > 0 else 0.0
        top_buy_patterns.append({"pattern": sig_k, "count": cnt, "win_rate": wr, "pnl": round(safe_float(rec.get("sum_pnl")), 2)})

    top_sell_patterns = []
    for sig_k, rec in sorted(bucket.get("sell_patterns", {}).items(), key=lambda item: int(item[1].get("count", 0)), reverse=True)[:3]:
        cnt = int(rec.get("count", 0))
        w_cnt = int(rec.get("wins", 0))
        wr = round((w_cnt / cnt) * 100.0, 1) if cnt > 0 else 0.0
        top_sell_patterns.append({"pattern": sig_k, "count": cnt, "win_rate": wr, "pnl": round(safe_float(rec.get("sum_pnl")), 2)})

    return {
        "context_key": ctx_key,
        "readiness_pct": readiness_pct,
        "target_pct": LEARNING_READINESS_TARGET_PCT,
        "is_ready_80": is_ready_80,
        "is_mature_for_filter": is_mature_for_filter,
        "advisor_mode": advisor_mode,
        "effective_trades": effective_trades,
        "context_trades": ctx_trades,
        "target_trades_for_80": target_trades_for_80,
        "remaining_trades_to_80": remaining_trades_to_80,
        "top_buy_patterns": top_buy_patterns,
        "top_sell_patterns": top_sell_patterns,
    }


def _ensure_parent_dir(file_path: str):
    parent = os.path.dirname(file_path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _pad_or_trim_features(feats: list[float] | None, target_len: int = AI_FEATURE_COUNT) -> list[float] | None:
    if not feats or len(feats) < 6:
        return None
    cleaned = [safe_float(x, 0.0) for x in feats[:target_len]]
    while len(cleaned) < target_len:
        cleaned.append(0.0)
    return cleaned


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
            "pattern_memory": PATTERN_MEMORY,
        }
        tmp_path = f"{AI_MEMORY_FILE}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, AI_MEMORY_FILE)
    except Exception:
        pass


def load_ai_memory():
    global AI_STATE, AI_LEARNED_TRADES, PATTERN_MEMORY
    try:
        if not os.path.exists(AI_MEMORY_FILE):
            return
        with open(AI_MEMORY_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            return
        saved_ai = payload.get("ai_state")
        if isinstance(saved_ai, dict):
            for k, v in saved_ai.items():
                if not str(k).startswith("_"):
                    AI_STATE[k] = v
        saved_learned = payload.get("learned_trades")
        if isinstance(saved_learned, list):
            AI_LEARNED_TRADES[:] = saved_learned[:200]
        saved_patterns = payload.get("pattern_memory")
        if isinstance(saved_patterns, dict):
            PATTERN_MEMORY.clear()
            PATTERN_MEMORY.update(saved_patterns)
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
            "active_trades": ACTIVE_TRADES,
            "history": CLOSED_TRADES_HISTORY[:100],
            "logs": SYSTEM_LOGS[:100],
            "log_entries": SYSTEM_LOG_ENTRIES[:100],
            "log_entries_24h": SYSTEM_LOG_24H_ENTRIES[:400],
        }
        tmp_path = f"{STATE_FILE}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, STATE_FILE)
        save_ai_memory()
    except Exception:
        pass


def load_persistent_data():
    global SETTINGS, AI_STATE, ACTIVE_TRADES, CLOSED_TRADES_HISTORY, SYSTEM_LOG_ENTRIES, SYSTEM_LOG_24H_ENTRIES, SYSTEM_LOGS
    load_ai_memory()
    try:
        if not os.path.exists(STATE_FILE):
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            return
        saved_settings = payload.get("settings")
        if isinstance(saved_settings, dict):
            for k, v in saved_settings.items():
                if k in SETTINGS:
                    SETTINGS[k] = v
        saved_ai = payload.get("ai_state")
        if isinstance(saved_ai, dict):
            for k, v in saved_ai.items():
                if not str(k).startswith("_"):
                    AI_STATE[k] = v
        saved_active = payload.get("active_trades")
        if isinstance(saved_active, dict):
            ACTIVE_TRADES.clear()
            ACTIVE_TRADES.update(saved_active)
        saved_history = payload.get("history")
        if isinstance(saved_history, list):
            CLOSED_TRADES_HISTORY[:] = saved_history[:200]
        if not AI_LEARNED_TRADES and CLOSED_TRADES_HISTORY:
            for t in CLOSED_TRADES_HISTORY:
                if t.get("entry_features") and len(t.get("entry_features")) >= 6:
                    AI_LEARNED_TRADES.append(t)
        saved_log_24h = payload.get("log_entries_24h")
        if isinstance(saved_log_24h, list):
            SYSTEM_LOG_24H_ENTRIES[:] = saved_log_24h[:1500]
        saved_log_2h = payload.get("log_entries")
        if isinstance(saved_log_2h, list):
            SYSTEM_LOG_ENTRIES[:] = saved_log_2h[:500]
        saved_logs_txt = payload.get("logs")
        if isinstance(saved_logs_txt, list):
            SYSTEM_LOGS[:] = saved_logs_txt[:200]
        if not SYSTEM_LOGS and SYSTEM_LOG_ENTRIES:
            SYSTEM_LOGS[:] = [e.get("line", "") for e in SYSTEM_LOG_ENTRIES[:200] if e.get("line")]
        _prune_old_logs()
    except Exception:
        pass


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


def get_market_regime_and_sideways_sync(bar_tf: str = "15m") -> tuple[str, bool]:
    try:
        tf_clean = str(bar_tf).lower().strip()
        res = okx_public_request_sync(f"/api/v5/market/candles?instId=BTC-USDC&bar={tf_clean}&limit=50")
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
        if tf_clean in ("5m", "8m", "10m"):
            min_crosses, max_rng_pct = 6, 0.90
        elif tf_clean in ("15m", "20m"):
            min_crosses, max_rng_pct = 5, 1.45
        elif tf_clean in ("30m", "45m", "1h"):
            min_crosses, max_rng_pct = 4, 2.20
        else:
            min_crosses, max_rng_pct = 4, 3.20

        is_sideways = (crosses >= min_crosses) and (rng_pct < max_rng_pct)
        if is_sideways:
            return "SIDEWAYS", True
        regime = "BULL" if ema_fast[-1] >= ema_slow[-1] else "BEAR"
        return regime, False
    except Exception:
        return "BULL", False


async def get_market_regime_and_sideways(bar_tf: str = "15m") -> tuple[str, bool]:
    return await asyncio.to_thread(get_market_regime_and_sideways_sync, bar_tf)


def is_bull_market_on_tf_sync(bar_tf: str | None = None) -> tuple[bool, str]:
    tf_clean = (bar_tf or CURRENT_TIMEFRAME).lower().strip()
    regime, sideways = get_market_regime_and_sideways_sync(tf_clean)
    if sideways or str(regime).upper() == "SIDEWAYS":
        return False, f"Bocny trh (SIDEWAYS) na TF {tf_clean.upper()}"
    if str(regime).upper() != "BULL":
        return False, f"Medvedi trh ({regime}) na TF {tf_clean.upper()}"
    return True, "BULL"


async def is_bull_market_on_tf(bar_tf: str | None = None) -> tuple[bool, str]:
    return await asyncio.to_thread(is_bull_market_on_tf_sync, bar_tf)


def check_btc_health_sync() -> bool:
    if not SETTINGS.get("use_btc_filter", False):
        return True
    try:
        res = okx_public_request_sync("/api/v5/market/candles?instId=BTC-USDC&bar=15m&limit=30")
        if res.get("code") != "0" or not res.get("data") or len(res["data"]) < 20:
            return True
        closes = [safe_float(c[4]) for c in reversed(res["data"]) if safe_float(c[4]) > 0]
        if len(closes) < 15:
            return True
        ema20 = calc_ema_series(closes, 20)[-1]
        return closes[-1] >= ema20 * 0.992
    except Exception:
        return True


async def check_btc_health() -> bool:
    return await asyncio.to_thread(check_btc_health_sync)


def update_top_liquid_symbols_sync():
    global TOP_LIQUID_SYMBOLS
    try:
        res = okx_public_request_sync("/api/v5/market/tickers?instType=SPOT")
        if res.get("code") != "0" or not res.get("data"):
            log_event(f"Nepodarilo sa nacitat tickery pre symboly: {res.get('msg')}")
            return
        candidates = []
        for t in res["data"]:
            inst = t.get("instId", "")
            if inst.endswith("-USDC") and inst not in EXCLUDED_SYMBOLS:
                vol_ccy = safe_float(t.get("volCcy24h"), 0.0)
                if vol_ccy >= 500000.0:
                    candidates.append((vol_ccy, inst))
        candidates.sort(key=lambda x: x[0], reverse=True)
        top = [inst for _, inst in candidates[:20] if inst not in EXCLUDED_SYMBOLS]
        if len(top) >= 5:
            TOP_LIQUID_SYMBOLS = top
        else:
            TOP_LIQUID_SYMBOLS = [s for s in INITIAL_TOP_SYMBOLS if s not in EXCLUDED_SYMBOLS]
    except Exception as e:
        log_event(f"Chyba pri aktualizacii top symbolov: {e}")


async def update_top_liquid_symbols():
    await asyncio.to_thread(update_top_liquid_symbols_sync)


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-min(x, 60.0))
        return 1.0 / (1.0 + z)
    z = math.exp(min(-x, 60.0))
    return z / (1.0 + z)


def _regime_numeric_value() -> float:
    if IS_SIDEWAYS_MARKET or str(MARKET_REGIME).upper() == "SIDEWAYS":
        return 0.0
    if str(MARKET_REGIME).upper() == "BEAR":
        return -0.75
    return 0.75


def extract_ai_features(idx: int, ha_closes, ha_opens, ha_highs, ha_lows, volumes, oscillator, signal_line, close_ema_9, predicted_next, close_ema_50=None):
    cl = ha_closes[idx]
    ema9 = close_ema_9[idx] if (close_ema_9 and close_ema_9[idx] and close_ema_9[idx] > 0) else cl
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

    if close_ema_50 and idx < len(close_ema_50) and close_ema_50[idx] and close_ema_50[idx] > 0:
        ema50 = close_ema_50[idx]
        dist_ema50_norm = max(-1.0, min(1.0, (((cl - ema50) / ema50) * 100.0) / 4.0))
    else:
        dist_ema50_norm = 0.0

    atr_win_start = max(0, idx - 13)
    atr_vals = [
        max(0.0, ha_highs[k] - ha_lows[k]) / ha_closes[k] * 100.0
        for k in range(atr_win_start, idx + 1)
        if ha_closes[k] > 0
    ]
    atr_pct = (sum(atr_vals) / len(atr_vals)) if atr_vals else 0.8
    atr_norm = max(-1.0, min(1.0, (atr_pct - 0.8) / 1.2))

    regime_norm = _regime_numeric_value()

    return [
        dist_ema9_pct,
        rel_vol,
        osc_ratio,
        sig_diff,
        pred_diff,
        body_ratio,
        dist_ema50_norm,
        atr_norm,
        regime_norm,
    ]


def describe_ai_factors(feats: list[float] | None) -> str:
    if not feats or len(feats) < 6:
        return "--"
    labels = [
        ("EMA9", feats[0]),
        ("Objem", feats[1]),
        ("Osc", feats[2]),
        ("Sig", feats[3]),
        ("Pred", feats[4]),
        ("Sviecka", feats[5]),
    ]
    if len(feats) >= 9:
        labels.extend([
            ("EMA50", feats[6]),
            ("ATR", feats[7]),
            ("Trh", feats[8]),
        ])
    ranked = sorted(labels, key=lambda item: abs(item[1]), reverse=True)[:3]
    parts = [f"{name}{'+' if val >= 0 else '-'}" for name, val in ranked]
    return ", ".join(parts)


def _build_series_and_signals(candles_raw, mode_choice: str):
    candles = list(reversed(candles_raw))
    closed = candles[:-1]
    n = len(closed)
    if n < 30:
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
    close_ema_50 = calc_ema_series(ha_closes, 50)
    lr0 = calc_linreg_series(oscillator, pred_look, 0)
    lr1 = calc_linreg_series(oscillator, pred_look, 1)
    raw_slope = [(lr0[i] - lr1[i]) if (lr0[i] is not None and lr1[i] is not None) else None for i in range(n)]
    smooth_slope = calc_wma_series(raw_slope, 3)
    predicted_next = [(oscillator[i] + smooth_slope[i]) if smooth_slope[i] is not None else oscillator[i] for i in range(n)]

    return {
        "n": n,
        "ha_closes": ha_closes,
        "ha_opens": ha_opens,
        "ha_highs": ha_highs,
        "ha_lows": ha_lows,
        "volumes": volumes,
        "oscillator": oscillator,
        "signal_line": signal_line,
        "close_ema_9": close_ema_9,
        "close_ema_50": close_ema_50,
        "predicted_next": predicted_next,
        "mode_choice": mode_choice,
    }


def get_current_ai_features_for_symbol_sync(symbol: str, bar_tf: str = "15m") -> list[float] | None:
    try:
        res = okx_public_request_sync(f"/api/v5/market/candles?instId={symbol}&bar={bar_tf}&limit=90")
        if res.get("code") != "0" or not res.get("data") or len(res["data"]) < 30:
            return None
        mode_choice = str(SETTINGS.get("mode_choice", "FIB")).upper()
        pack = _build_series_and_signals(res["data"], mode_choice)
        if not pack:
            return None
        idx = pack["n"] - 1
        return extract_ai_features(
            idx,
            pack["ha_closes"],
            pack["ha_opens"],
            pack["ha_highs"],
            pack["ha_lows"],
            pack["volumes"],
            pack["oscillator"],
            pack["signal_line"],
            pack["close_ema_9"],
            pack["predicted_next"],
            pack["close_ema_50"],
        )
    except Exception:
        return None


async def get_current_ai_features_for_symbol(symbol: str, bar_tf: str = "15m") -> list[float] | None:
    return await asyncio.to_thread(get_current_ai_features_for_symbol_sync, symbol, bar_tf)


def _collect_symbol_training_samples_sync(symbol: str, bar_tf: str, target_gain_pct: float, stop_loss_pct: float, mode_choice: str) -> list[tuple[list[float], int, float]]:
    res = okx_public_request_sync(f"/api/v5/market/candles?instId={symbol}&bar={bar_tf}&limit=250")
    if res.get("code") != "0" or not res.get("data") or len(res["data"]) < 60:
        return []
    pack = _build_series_and_signals(res["data"], mode_choice)
    if not pack:
        return []

    n = pack["n"]
    ha_closes = pack["ha_closes"]
    ha_opens = pack["ha_opens"]
    ha_highs = pack["ha_highs"]
    ha_lows = pack["ha_lows"]
    volumes = pack["volumes"]
    oscillator = pack["oscillator"]
    signal_line = pack["signal_line"]
    close_ema_9 = pack["close_ema_9"]
    close_ema_50 = pack["close_ema_50"]
    predicted_next = pack["predicted_next"]

    pivot_len = max(1, min(10, int(SETTINGS.get("pivot_len", 2))))
    fib_choice = str(SETTINGS.get("fib_choice", "0.786"))
    last_high = None
    last_low = None

    horizon = 12
    samples = []
    for i in range(25, n - horizon):
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

        cond_vol_bull = (osc_i > 0.0) and (osc_i > sig_i) and (pred_i > osc_i)
        cond_vol_cross = (oscillator[i - 1] <= signal_line[i - 1]) and (osc_i > sig_i) and (osc_i > 0.0)
        cond_ema9 = cl_i > ema9_i
        fib_touch = (fib_target is not None) and (cl_i > fib_target)
        if mode_choice == "BREAKOUT":
            is_sig = cond_vol_cross or (cond_vol_bull and cond_ema9)
        elif mode_choice == "FIB":
            is_sig = cond_vol_bull and fib_touch
        else:
            is_sig = cond_vol_bull and (cond_vol_cross or cond_ema9)

        if not is_sig or cl_i <= 0:
            continue

        label = 0
        move_magnitude_pct = 0.4
        for j in range(i + 1, i + 1 + horizon):
            chg_pct = ((ha_closes[j] - cl_i) / cl_i) * 100.0
            if chg_pct >= target_gain_pct:
                label = 1
                move_magnitude_pct = max(0.3, abs(chg_pct))
                break
            if chg_pct <= -stop_loss_pct:
                label = 0
                move_magnitude_pct = max(0.4, abs(chg_pct))
                break

        weight = max(0.5, min(1.8, move_magnitude_pct / max(0.4, target_gain_pct)))
        feats = extract_ai_features(
            i,
            ha_closes,
            ha_opens,
            ha_highs,
            ha_lows,
            volumes,
            oscillator,
            signal_line,
            close_ema_9,
            predicted_next,
            close_ema_50,
        )
        samples.append((feats, label, weight))
    return samples


def train_ai_model_from_candles_sync(symbol: str, bar_tf: str = "15m", force: bool = False) -> dict:
    global AI_STATE
    now_ts = time.time()
    if not force and (now_ts - AI_STATE.get("_last_train_ts", 0.0)) < 1800 and AI_STATE.get("samples_count", 0) >= 15:
        return {"ok": True, "samples": AI_STATE["samples_count"], "win_rate": AI_STATE["win_rate_train"]}

    try:
        mode_choice = str(SETTINGS.get("mode_choice", "FIB")).upper()
        if SETTINGS.get("use_fixed_tp", False):
            target_gain_pct = max(0.35, float(SETTINGS.get("fixed_tp_pct", 1.2)))
        else:
            target_gain_pct = max(0.35, float(SETTINGS.get("ttp_trigger_pct", 1.2)))
        stop_loss_pct = max(0.25, float(SETTINGS.get("fixed_sl_pct", 1.5)))

        train_symbols = []
        for cand in [symbol] + list(TOP_LIQUID_SYMBOLS[:5]):
            if cand and cand not in EXCLUDED_SYMBOLS and cand not in train_symbols:
                train_symbols.append(cand)
            if len(train_symbols) >= 5:
                break

        dataset: list[tuple[list[float], int, float]] = []
        used_symbols: list[str] = []
        for sym in train_symbols:
            sym_samples = _collect_symbol_training_samples_sync(sym, bar_tf, target_gain_pct, stop_loss_pct, mode_choice)
            if sym_samples:
                dataset.extend(sym_samples)
                used_symbols.append(sym.split("-")[0])

        if len(dataset) < 10:
            return {"ok": False, "msg": f"Najdenych len {len(dataset)} historickych signalov."}

        weights = [0.0] * AI_FEATURE_COUNT
        bias = 0.0
        lr = 0.045
        reg = 0.08

        for _ in range(45):
            for feats, y, w_sample in dataset:
                z = bias + sum(weights[k] * feats[k] for k in range(AI_FEATURE_COUNT))
                pred_p = _sigmoid(z * 0.65)
                err = (pred_p - y) * w_sample
                for k in range(AI_FEATURE_COUNT):
                    weights[k] -= lr * (err * feats[k] + reg * weights[k])
                bias -= lr * err

        correct = 0
        wins_cnt = 0
        for feats, y, _ in dataset:
            if y == 1:
                wins_cnt += 1
            z = bias + sum(weights[k] * feats[k] for k in range(AI_FEATURE_COUNT))
            pred_cls = 1 if _sigmoid(z * 0.65) >= 0.5 else 0
            if pred_cls == y:
                correct += 1

        acc_pct = round((correct / len(dataset)) * 100.0, 1)
        AI_STATE["weights"] = [round(max(-2.5, min(2.5, w)), 5) for w in weights]
        AI_STATE["bias"] = round(max(-1.5, min(1.5, bias)), 5)
        AI_STATE["samples_count"] = len(dataset)
        AI_STATE["win_rate_train"] = round((wins_cnt / len(dataset)) * 100.0, 1)
        AI_STATE["accuracy_train"] = acc_pct
        AI_STATE["trained_at"] = get_sk_time_str()
        sym_label = ",".join(used_symbols[:3]) if used_symbols else symbol
        AI_STATE["last_symbol"] = f"{sym_label} ({bar_tf.upper()})"
        AI_STATE["_last_train_ts"] = now_ts

        source_trades = AI_LEARNED_TRADES if AI_LEARNED_TRADES else [
            t for t in CLOSED_TRADES_HISTORY if t.get("entry_features") and len(t.get("entry_features")) >= 6
        ]
        for t in reversed(source_trades[:50]):
            t_feats = _pad_or_trim_features(t.get("entry_features"), AI_FEATURE_COUNT)
            if t_feats:
                pnl_val = safe_float(t.get("pnl_usdc"))
                pnl_pct_val = abs(safe_float(t.get("pnl_pct"), 0.5))
                y_real = 1.0 if pnl_val > 0 else 0.0
                trade_w = max(0.35, min(1.8, pnl_pct_val / 0.6))
                for _ in range(2):
                    z = AI_STATE["bias"] + sum(AI_STATE["weights"][k] * t_feats[k] for k in range(AI_FEATURE_COUNT))
                    err = (_sigmoid(z * 0.65) - y_real) * trade_w
                    for k in range(AI_FEATURE_COUNT):
                        AI_STATE["weights"][k] -= 0.04 * (err * t_feats[k] + 0.08 * AI_STATE["weights"][k])
                    AI_STATE["bias"] -= 0.04 * err
                AI_STATE["weights"] = [round(max(-2.5, min(2.5, w)), 5) for w in AI_STATE["weights"]]
                AI_STATE["bias"] = round(max(-1.5, min(1.5, AI_STATE["bias"])), 5)

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
            f"AI model natrenovany na [{', '.join(used_symbols)}] [{bar_tf.upper()}]: {len(dataset)} vzoriek (TP {target_gain_pct:.1f}% / SL {stop_loss_pct:.1f}%) | "
            f"Ulozene obchody: {live_cnt} ({live_wins}W/{live_losses}L) | Presnost: {acc_pct}%."
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


async def train_ai_model_from_candles(symbol: str, bar_tf: str = "15m", force: bool = False) -> dict:
    return await asyncio.to_thread(train_ai_model_from_candles_sync, symbol, bar_tf, force)


def predict_ai_probability(feats: list[float] | None) -> float | None:
    if AI_STATE.get("samples_count", 0) < 10:
        return None
    aligned_feats = _pad_or_trim_features(feats, AI_FEATURE_COUNT)
    if not aligned_feats:
        return None
    w = list(AI_STATE.get("weights", [0.0] * AI_FEATURE_COUNT))
    while len(w) < AI_FEATURE_COUNT:
        w.append(0.0)
    b = float(AI_STATE.get("bias", 0.0))
    z = b + sum(safe_float(w[k]) * aligned_feats[k] for k in range(AI_FEATURE_COUNT))
    raw_prob = _sigmoid(z * 0.65)
    clamped = max(0.08, min(0.88, raw_prob))
    return round(clamped, 3)


def learn_from_closed_trade(features: list[float] | None, pnl_usdc: float, trade_style: str = "SCALPING", pnl_pct: float | None = None, persist: bool = False):
    global AI_STATE
    aligned_feats = _pad_or_trim_features(features, AI_FEATURE_COUNT)
    if not aligned_feats:
        return
    y = 1.0 if pnl_usdc > 0 else 0.0
    mag_pct = abs(safe_float(pnl_pct, 0.5)) if pnl_pct is not None else 0.5
    sample_weight = max(0.35, min(1.8, mag_pct / 0.6))

    weights = list(AI_STATE.get("weights", [0.0] * AI_FEATURE_COUNT))
    while len(weights) < AI_FEATURE_COUNT:
        weights.append(0.0)
    bias = float(AI_STATE.get("bias", 0.0))
    lr = 0.045
    reg = 0.08

    for _ in range(2):
        z = bias + sum(safe_float(weights[k]) * aligned_feats[k] for k in range(AI_FEATURE_COUNT))
        pred_p = _sigmoid(z * 0.65)
        err = (pred_p - y) * sample_weight
        for k in range(AI_FEATURE_COUNT):
            weights[k] = max(-2.5, min(2.5, safe_float(weights[k]) - lr * (err * aligned_feats[k] + reg * safe_float(weights[k]))))
        bias = max(-1.5, min(1.5, bias - lr * err))

    live_cnt = int(AI_STATE.get("live_learned_count", 0)) + 1
    live_wins = int(AI_STATE.get("live_wins_count", 0)) + (1 if pnl_usdc > 0 else 0)
    AI_STATE["weights"] = [round(x, 5) for x in weights]
    AI_STATE["bias"] = round(bias, 5)
    AI_STATE["live_learned_count"] = live_cnt
    AI_STATE["live_wins_count"] = live_wins
    AI_STATE["trained_at"] = get_sk_time_str()
    if persist:
        save_ai_model()
    log_event(
        f"AI sa doucilo [{trade_style}] ({'ZISK' if pnl_usdc > 0 else 'STRATA'} {pnl_usdc:+.2f} $, vaha {sample_weight:.2f}x) | Ulozenych obchodov: {live_cnt} ({live_wins}W)",
        persist=False,
    )


def evaluate_pine_strategy_sync(symbol: str, bar_tf: str = "15m", bull_ok_override: tuple[bool, str] | None = None, exit_only_for_active: bool = False):
    global MARKET_SCANNER_CACHE, ACTIVE_TRADES, AI_STATE
    coin_key = symbol.split("-")[0]

    try:
        url = f"/api/v5/market/candles?instId={symbol}&bar={bar_tf}&limit=120"
        res = okx_public_request_sync(url)
        if res.get("code") != "0" or not res.get("data") or len(res["data"]) < 30:
            MARKET_SCANNER_CACHE[coin_key] = {
                "symbol": symbol, "price": 0.0, "oscillator": 0.0,
                "momentum": "SLABE", "impulse": "CAKA", "trade_status": "---",
                "be_status": "VYPNUTY", "ttp_status": "VYPNUTY", "ts_status": "VYPNUTY",
                "ai_score": "--", "ai_factors": "--", "exec_signal": "CAKAJ", "signal": "CHYBA DAT", "is_green": False
            }
            return {"buy": False, "raw_buy": False, "sell": False, "sell_reason": "", "ai_prob": None, "ai_factors": "--", "block_reason": "", "features": None, "last_closed_price": 0.0, "ha_is_green": False}

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
        mode_choice = str(SETTINGS.get("mode_choice", "FIB")).upper()
        require_green_ha_buy = bool(SETTINGS.get("require_green_ha_buy", False))
        require_bull_tf_buy = bool(SETTINGS.get("require_bull_tf_buy", False))
        strict_buy_filter = bool(SETTINGS.get("strict_buy_filter", False))

        use_fixed_sl = bool(SETTINGS.get("use_fixed_sl", False))
        fixed_sl_pct = float(SETTINGS.get("fixed_sl_pct", 1.5))
        use_trailing = bool(SETTINGS.get("use_trailing", True))
        trail_pct = float(SETTINGS.get("trail_pct", 1.5))
        use_breakeven = bool(SETTINGS.get("use_breakeven", True))
        be_trigger_pct = float(SETTINGS.get("be_trigger_pct", 1.2))
        use_trailing_tp = bool(SETTINGS.get("use_trailing_tp", True))
        ttp_trigger_pct = float(SETTINGS.get("ttp_trigger_pct", 1.2))
        ttp_callback_pct = float(SETTINGS.get("ttp_callback_pct", 0.4))
        use_ha_exit = bool(SETTINGS.get("use_ha_exit", True))
        ha_exit_consecutive = max(1, min(3, int(SETTINGS.get("ha_exit_consecutive", 1))))
        sell_on_osc_reversal = bool(SETTINGS.get("sell_on_osc_reversal", False))

        vol_dirs = [volumes[i] if ha_closes[i] > ha_opens[i] else -volumes[i] for i in range(n)]
        fast_ma = calc_ema_series(vol_dirs, fast_len)
        slow_ma = calc_ema_series(vol_dirs, slow_len)
        oscillator = [fast_ma[i] - slow_ma[i] for i in range(n)]
        signal_line = calc_ema_series(oscillator, signal_len)
        close_ema_9 = calc_ema_series(ha_closes, 9)
        close_ema_50 = calc_ema_series(ha_closes, 50)

        lr0 = calc_linreg_series(oscillator, pred_look, 0)
        lr1 = calc_linreg_series(oscillator, pred_look, 1)
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
        last_ha_is_green = False
        last_red_ha_blocked = False
        last_effective_stop = None

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
            ha_is_green = ha_closes[i] > ha_opens[i]

            cond_volume_bullish = (osc_i > 0.0) and (osc_i > sig_i) and (pred_i > osc_i)
            cond_volume_crossover = (i > 0) and (oscillator[i - 1] <= signal_line[i - 1]) and (osc_i > sig_i) and (osc_i > 0.0)
            cond_ema9 = cl_i > ema9_i
            fib_touch = (fib_target is not None) and (cl_i > fib_target)

            breakout_trigger = cond_volume_crossover or (cond_volume_bullish and cond_ema9)
            fib_trigger_cond = cond_volume_bullish and fib_touch
            all_green_condition = cond_volume_bullish and (cond_volume_crossover or cond_ema9)

            if mode_choice == "BREAKOUT":
                base_trigger = breakout_trigger
            elif mode_choice == "FIB":
                base_trigger = fib_trigger_cond
            else:
                base_trigger = all_green_condition

            red_ha_blocked_now = False
            if require_green_ha_buy and base_trigger and (not ha_is_green):
                base_trigger = False
                red_ha_blocked_now = True

            if strict_buy_filter and base_trigger:
                above_prev_high = (i == 0) or (cl_i > ha_highs[i - 1])
                base_trigger = base_trigger and ha_is_green and (cl_i > ema9_i) and above_prev_high

            buy_trigger = base_trigger

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

            osc_cross_down = (
                sell_on_osc_reversal
                and i > 0
                and (
                    ((oscillator[i - 1] >= signal_line[i - 1]) and (osc_i < sig_i))
                    or ((oscillator[i - 1] >= 0.0) and (osc_i < 0.0))
                )
            )

            cond_trail_triggered = use_trailing and obchod_otvoreny and (cl_i < effective_stop)
            cond_ttp_triggered = use_trailing_tp and trailing_tp_active and (trailing_tp_stop_level is not None) and (cl_i <= trailing_tp_stop_level)

            raw_sell = obchod_otvoreny and (cond_trail_triggered or cond_ttp_triggered or ha_exit_triggered or osc_cross_down)
            sell_reason = ""
            if raw_sell:
                if cond_ttp_triggered:
                    sell_reason = "Pine SELL (Trailing TP)"
                elif ha_exit_triggered:
                    sell_reason = f"Pine SELL (HA Exit {ha_exit_consecutive}x)"
                elif osc_cross_down:
                    sell_reason = "Pine SELL (Obrat oscilatora)"
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
                last_ha_is_green = ha_is_green
                last_red_ha_blocked = red_ha_blocked_now
                last_effective_stop = effective_stop

        curr_close = ha_closes[-1]
        curr_osc = oscillator[-1]
        is_in_trade = coin_key in ACTIVE_TRADES

        ha_is_red_now = ha_closes[-1] < ha_opens[-1]
        ha_signal_now = False
        if use_ha_exit:
            if ha_exit_consecutive == 1:
                ha_signal_now = ha_is_red_now
            elif ha_exit_consecutive == 2:
                ha_signal_now = (n >= 2) and ha_is_red_now and (ha_closes[-2] < ha_opens[-2])
            else:
                ha_signal_now = (n >= 3) and ha_is_red_now and (ha_closes[-2] < ha_opens[-2]) and (ha_closes[-3] < ha_opens[-3])

        osc_signal_now = (
            sell_on_osc_reversal
            and n >= 2
            and (
                ((oscillator[-2] >= signal_line[-2]) and (oscillator[-1] < signal_line[-1]))
                or ((oscillator[-2] >= 0.0) and (oscillator[-1] < 0.0))
            )
        )
        pine_sell_signal_now = bool(last_bar_sell or ha_signal_now or osc_signal_now)

        live_exit_now = False
        live_exit_reason = ""
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

            osc_live_exit = (
                sell_on_osc_reversal
                and n >= 2
                and (
                    ((oscillator[-2] >= signal_line[-2]) and (oscillator[-1] < signal_line[-1]))
                    or ((oscillator[-2] >= 0.0) and (oscillator[-1] < 0.0))
                )
            )

            if ha_live_exit:
                live_exit_now = True
                live_exit_reason = f"Pine SELL (HA Exit {ha_exit_consecutive}x)"
            elif osc_live_exit:
                live_exit_now = True
                live_exit_reason = "Pine SELL (Obrat oscilatora)"

        feats = extract_ai_features(
            n - 1,
            ha_closes,
            ha_opens,
            ha_highs,
            ha_lows,
            volumes,
            oscillator,
            signal_line,
            close_ema_9,
            predicted_next,
            close_ema_50,
        )
        ai_factors_txt = describe_ai_factors(feats)
        ai_prob = predict_ai_probability(feats)

        final_buy = last_bar_buy
        block_reason = ""
        non_bull_blocked = False

        if not is_in_trade:
            if require_bull_tf_buy:
                if bull_ok_override is not None:
                    bull_ok, bull_msg = bull_ok_override
                else:
                    bull_ok = (not IS_SIDEWAYS_MARKET) and (str(MARKET_REGIME).upper() == "BULL")
                    bull_msg = f"Trh nie je BYCI ({MARKET_REGIME}) na TF {bar_tf.upper()}"
                if not bull_ok:
                    if final_buy:
                        non_bull_blocked = True
                    final_buy = False
                    block_reason = bull_msg

            if require_green_ha_buy and not last_ha_is_green:
                if final_buy or last_red_ha_blocked:
                    last_red_ha_blocked = True
                final_buy = False
                if last_red_ha_blocked:
                    block_reason = "Zakaz nakupu: HA sviecka nie je zelena"

            if final_buy and bool(SETTINGS.get("block_sideways_buy", False)) and IS_SIDEWAYS_MARKET:
                final_buy = False
                block_reason = "Bocny trh (SIDEWAYS)"

            if final_buy and bool(SETTINGS.get("use_anti_chase", False)):
                last_raw = closed_candles[-1]
                raw_o = safe_float(last_raw[1])
                raw_h = safe_float(last_raw[2])
                raw_l = safe_float(last_raw[3])
                raw_c = safe_float(last_raw[4])

                candle_pump_pct = ((raw_c - raw_o) / raw_o * 100.0) if raw_o > 0 else 0.0
                ema9_last = close_ema_9[-1] if close_ema_9[-1] and close_ema_9[-1] > 0 else raw_c
                dist_ema9_pct = ((raw_c - ema9_last) / ema9_last * 100.0) if ema9_last > 0 else 0.0

                c_range = max(1e-9, raw_h - raw_l)
                upper_wick = max(0.0, raw_h - max(raw_o, raw_c))
                wick_ratio = upper_wick / c_range

                max_pump = float(SETTINGS.get("max_candle_pump_pct", 0.9))
                max_ema_dist = float(SETTINGS.get("max_ema9_dist_pct", 0.7))
                max_wick = float(SETTINGS.get("max_upper_wick_ratio", 0.45))

                if candle_pump_pct > max_pump:
                    final_buy = False
                    block_reason = f"Anti-Top: sviecka +{candle_pump_pct:.2f}% > {max_pump:.2f}%"
                elif dist_ema9_pct > max_ema_dist:
                    final_buy = False
                    block_reason = f"Anti-Top: nad EMA9 +{dist_ema9_pct:.2f}% > {max_ema_dist:.2f}%"
                elif wick_ratio > max_wick:
                    final_buy = False
                    block_reason = f"Anti-Top: horny knot {wick_ratio*100:.0f}% > {max_wick*100:.0f}%"

            min_rel_vol = float(SETTINGS.get("min_rel_volume", 0.0))
            if final_buy and min_rel_vol > 0:
                vol_win = volumes[max(0, n - 15): n - 1]
                avg_v = (sum(vol_win) / len(vol_win)) if vol_win else 0.0
                rel_v = (volumes[-1] / avg_v) if avg_v > 0 else 1.0
                if rel_v < min_rel_vol:
                    final_buy = False
                    block_reason = f"Nizky objem ({rel_v:.2f}x < {min_rel_vol:.2f}x)"

            if final_buy and bool(SETTINGS.get("use_ai_filter", False)):
                if AI_STATE.get("samples_count", 0) < 10:
                    train_ai_model_from_candles_sync(symbol, bar_tf=bar_tf, force=False)
                    feats = extract_ai_features(
                        n - 1,
                        ha_closes,
                        ha_opens,
                        ha_highs,
                        ha_lows,
                        volumes,
                        oscillator,
                        signal_line,
                        close_ema_9,
                        predicted_next,
                        close_ema_50,
                    )
                    ai_factors_txt = describe_ai_factors(feats)
                    ai_prob = predict_ai_probability(feats)

                readiness_info = compute_ai_readiness_info()
                min_prob = float(SETTINGS.get("ai_min_prob", 0.38))
                if not readiness_info.get("is_mature_for_filter", False):
                    AI_STATE["passed_count"] = AI_STATE.get("passed_count", 0) + 1
                elif ai_prob is not None and ai_prob < min_prob:
                    final_buy = False
                    block_reason = f"AI skore {ai_prob*100:.1f}% < {min_prob*100:.1f}% [{ai_factors_txt}]"
                    AI_STATE["blocked_count"] = AI_STATE.get("blocked_count", 0) + 1
                else:
                    AI_STATE["passed_count"] = AI_STATE.get("passed_count", 0) + 1

        if ai_prob is not None and (final_buy or bool(SETTINGS.get("use_ai_filter", False))):
            AI_STATE["last_prob"] = ai_prob
            AI_STATE["last_factors"] = ai_factors_txt

        momentum_str = "SILNE" if last_cond_volume_bullish else "SLABE"
        impuls_active = last_buy_trigger or is_in_trade
        impulse_str = "AKTIVNY (V TRHU)" if impuls_active else ("CERVENA HA" if (not last_ha_is_green and require_green_ha_buy) else "CAKA")

        if is_in_trade:
            t_ref = ACTIVE_TRADES[coin_key]
            trade_status_txt = f"{t_ref['entry_price']:.4f}"
            be_status_txt = f"{t_ref['entry_price']:.4f}" if (use_breakeven and t_ref.get("be_active", False)) else ("CAKA" if use_breakeven else "VYPNUTY")
            ttp_stop_live = t_ref.get("ttp_stop_price")
            ttp_status_txt = f"{ttp_stop_live:.4f}" if (use_trailing_tp and t_ref.get("ttp_active", False) and ttp_stop_live) else ("V TRHU" if use_trailing_tp else "VYPNUTY")
            if bool(t_ref.get("use_fixed_sl", False)) and safe_float(t_ref.get("fixed_sl_price"), 0.0) > 0:
                ts_status_txt = f"SL {safe_float(t_ref.get('fixed_sl_price')):.4f} (-{safe_float(t_ref.get('fixed_sl_pct'), fixed_sl_pct)}%)"
            elif use_trailing and safe_float(t_ref.get("trail_stop_price"), 0.0) > 0:
                ts_status_txt = f"TS {safe_float(t_ref.get('trail_stop_price')):.4f}"
            else:
                ts_status_txt = "VYPNUTY"
        else:
            trade_status_txt = "---"
            be_status_txt = "VYPNUTY"
            ttp_status_txt = "VYPNUTY"
            if use_fixed_sl:
                ts_status_txt = f"Fixny SL -{fixed_sl_pct}%"
            elif use_trailing:
                ts_status_txt = f"TS -{trail_pct}%"
            else:
                ts_status_txt = "VYPNUTY"

        ai_score_txt = f"{ai_prob*100:.1f}%" if ai_prob is not None else ("UCI SA" if SETTINGS.get("use_ai_filter", False) else "OFF")

        if final_buy and not is_in_trade:
            exec_signal_str = f"NAKUPIT ({mode_choice})"
        elif non_bull_blocked and not is_in_trade:
            exec_signal_str = f"BLOK: NIE JE BYCI ({MARKET_REGIME})"
        elif last_red_ha_blocked and not is_in_trade:
            exec_signal_str = "BLOK: CERVENA HA"
        elif last_bar_buy and not final_buy and not is_in_trade:
            exec_signal_str = f"BLOK ({block_reason})"
        elif live_exit_now and is_in_trade:
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
            "ai_factors": ai_factors_txt,
            "exec_signal": exec_signal_str,
            "signal": "BUY" if final_buy else ("CERVENA HA" if last_red_ha_blocked else ("BLOK" if last_bar_buy else ("SELL" if live_exit_now else ("BULLISH" if last_cond_volume_bullish else "CAKA")))),
            "is_green": final_buy and last_ha_is_green
        }

        last_closed_raw_price = safe_float(closed_candles[-1][4], curr_close)
        return {
            "buy": False if exit_only_for_active else final_buy,
            "raw_buy": False if exit_only_for_active else last_bar_buy,
            "sell": live_exit_now,
            "pine_sell_signal": pine_sell_signal_now,
            "sell_reason": live_exit_reason,
            "ai_prob": ai_prob,
            "ai_factors": ai_factors_txt,
            "block_reason": block_reason,
            "features": feats,
            "last_closed_price": last_closed_raw_price,
            "ha_is_green": last_ha_is_green,
        }
    except Exception as e:
        log_event(f"Vynimka pri vyhodnocovani {symbol}: {e}")
        MARKET_SCANNER_CACHE[coin_key] = {
            "symbol": symbol, "price": 0.0, "oscillator": 0.0,
            "momentum": "SLABE", "impulse": "CAKA", "trade_status": "---",
            "be_status": "VYPNUTY", "ttp_status": "VYPNUTY", "ts_status": "VYPNUTY",
            "ai_score": "--", "ai_factors": "--", "exec_signal": "CAKAJ", "signal": "VYNIMKA", "is_green": False
        }
        return {"buy": False, "raw_buy": False, "sell": False, "sell_reason": "", "ai_prob": None, "ai_factors": "--", "block_reason": "", "features": None, "last_closed_price": 0.0, "ha_is_green": False}


async def evaluate_pine_strategy(symbol: str, bar_tf: str = "15m", bull_ok_override: tuple[bool, str] | None = None, exit_only_for_active: bool = False):
    return await asyncio.to_thread(evaluate_pine_strategy_sync, symbol, bar_tf, bull_ok_override, exit_only_for_active)


async def autonomous_scanner_loop():
    global IS_SIDEWAYS_MARKET, MARKET_REGIME, LAST_SIDEWAYS_ALERT_TS
    await asyncio.sleep(3)
    log_event(f"Autonomny skener spusteny pre {len(TOP_LIQUID_SYMBOLS)} symbolov (rezim: {SETTINGS.get('mode_choice')}).")
    while True:
        try:
            _prune_old_logs()
            tf_str = CURRENT_TIMEFRAME.lower()
            max_allowed = SETTINGS.get("max_parallel", 1)

            regime_now, sideways_now = await get_market_regime_and_sideways(tf_str)
            MARKET_REGIME = regime_now
            IS_SIDEWAYS_MARKET = sideways_now
            bull_ok, bull_reason = await is_bull_market_on_tf(tf_str)

            now_ts = time.time()
            if sideways_now and bool(SETTINGS.get("block_sideways_buy", False)) and (now_ts - LAST_SIDEWAYS_ALERT_TS >= SIDEWAYS_ALERT_COOLDOWN_SEC):
                LAST_SIDEWAYS_ALERT_TS = now_ts
                log_event(f"POZOR: BOCNY TRH zisteny na TF {CURRENT_TIMEFRAME} - nove nakupy su blokovane podla nastavenia.")
                send_telegram_msg(
                    "<b>POZOR: BOCNY TRH</b>\n"
                    f"<b>TF:</b> {CURRENT_TIMEFRAME}\n"
                    "Blokovanie bocneho trhu je zapnute v nastaveniach."
                )

            for coin_name in list(ACTIVE_TRADES.keys()):
                if coin_name not in ACTIVE_TRADES:
                    continue
                trade = ACTIVE_TRADES[coin_name]
                symbol = trade["market_pair"]
                sig = await evaluate_pine_strategy(symbol, bar_tf=tf_str, bull_ok_override=(bull_ok, bull_reason), exit_only_for_active=True)
                if sig["sell"]:
                    log_event(f"STRATEGIA 'SELL' SIGNAL PRE {symbol} ({sig['sell_reason']}) -> PREDAJ!")
                    await close_trade_position(coin_name, sig["sell_reason"])
                await asyncio.sleep(0.25)

            can_buy_now = (
                IS_BOT_ACTIVE
                and len(ACTIVE_TRADES) < max_allowed
                and (not bool(SETTINGS.get("require_bull_tf_buy", False)) or bull_ok)
                and await check_btc_health()
            )

            if IS_BOT_ACTIVE and len(ACTIVE_TRADES) < max_allowed and bool(SETTINGS.get("require_bull_tf_buy", False)) and not bull_ok:
                log_block_throttled("TRH", f"Nakupy pozastavene: {bull_reason}.", cooldown_sec=180.0)

            for symbol in list(TOP_LIQUID_SYMBOLS):
                if symbol in EXCLUDED_SYMBOLS:
                    continue

                coin_name = symbol.split("-")[0]
                now_ts = time.time()
                if now_ts < DEMO_REJECTED_COOLDOWN.get(symbol, 0.0):
                    continue

                sig = await evaluate_pine_strategy(symbol, bar_tf=tf_str, bull_ok_override=(bull_ok, bull_reason))

                if sig.get("sell", False) or sig.get("pine_sell_signal", False):
                    if WAITING_FOR_SELL_AFTER_EXIT.get(coin_name, False):
                        WAITING_FOR_SELL_AFTER_EXIT[coin_name] = False
                        log_event(f"Signal SELL pre {symbol} zaznamenany -> odomknuty dalsi nakup.")

                if not can_buy_now or len(ACTIVE_TRADES) >= SETTINGS.get("max_parallel", 1) or coin_name in ACTIVE_TRADES:
                    await asyncio.sleep(0.2)
                    continue

                if bool(SETTINGS.get("require_sell_before_rebuy", False)) and WAITING_FOR_SELL_AFTER_EXIT.get(coin_name, False):
                    log_block_throttled(symbol, "Caka na signal SELL po predošlom obchode", cooldown_sec=180.0)
                    await asyncio.sleep(0.2)
                    continue

                if WAIT_FOR_SIGNAL_RESET.get(coin_name, False):
                    if not sig.get("raw_buy", False):
                        WAIT_FOR_SIGNAL_RESET[coin_name] = False
                    else:
                        continue

                if coin_name in LAST_TRADE_TIME and (now_ts - LAST_TRADE_TIME[coin_name] < SETTINGS["reentry_cooldown_sec"]):
                    continue

                if sig["buy"]:
                    prob_txt = f" (AI: {sig['ai_prob']*100:.1f}% [{sig.get('ai_factors', '--')}])" if sig.get("ai_prob") is not None else ""
                    log_event(f"STRATEGIA 'BUY' SIGNAL PRE {symbol} [{SETTINGS.get('mode_choice')}]{prob_txt}!")
                    await execute_autonomous_buy(
                        symbol,
                        ai_prob=sig.get("ai_prob"),
                        ai_factors=sig.get("ai_factors", "--"),
                        entry_features=sig.get("features"),
                        signal_close_price=sig.get("last_closed_price", 0.0),
                        ha_is_green=sig.get("ha_is_green", False),
                    )
                elif sig.get("block_reason"):
                    log_block_throttled(symbol, sig["block_reason"], cooldown_sec=180.0)

                await asyncio.sleep(0.3)
            global _LAST_PERIODIC_TRAIN_TS
            if IS_BOT_ACTIVE and (time.time() - _LAST_PERIODIC_TRAIN_TS >= 1800.0):
                _LAST_PERIODIC_TRAIN_TS = time.time()
                seed_sym = TOP_LIQUID_SYMBOLS[0] if TOP_LIQUID_SYMBOLS else "SOL-USDC"
                await train_ai_model_from_candles(seed_sym, bar_tf=tf_str, force=False)
        except Exception as e:
            log_event(f"Chyba v autonomnom skeneri: {e}")
        await asyncio.sleep(10)


async def top_symbols_updater_loop():
    while True:
        try:
            await update_top_liquid_symbols()
        except Exception as e:
            log_event(f"Chyba v slucke top symbolov: {e}")
        await asyncio.sleep(3600)


async def execute_autonomous_buy(okx_symbol: str, ai_prob: float | None = None, ai_factors: str = "--", entry_features: list[float] | None = None, signal_close_price: float = 0.0, ha_is_green: bool = False):
    global ACTIVE_TRADES, LAST_TRADE_TIME
    async with trade_lock:
        if okx_symbol in EXCLUDED_SYMBOLS:
            return
        coin_name = okx_symbol.split("-")[0]
        now_ts = time.time()
        max_allowed = SETTINGS.get("max_parallel", 1)

        if coin_name in ACTIVE_TRADES or len(ACTIVE_TRADES) >= max_allowed:
            return

        if bool(SETTINGS.get("require_bull_tf_buy", False)):
            bull_ok, bull_reason = await is_bull_market_on_tf(CURRENT_TIMEFRAME.lower())
            if not bull_ok:
                log_event(f"Nakup {okx_symbol} zablokovany: {bull_reason}.")
                return

        if bool(SETTINGS.get("require_green_ha_buy", False)) and not ha_is_green:
            chk = await evaluate_pine_strategy(okx_symbol, bar_tf=CURRENT_TIMEFRAME.lower())
            if not chk.get("ha_is_green", False):
                log_event(f"Nakup {okx_symbol} zablokovany: HA sviecka nie je zelena.")
                return

        LAST_TRADE_TIME[coin_name] = now_ts
        stable_ccy = "USDC"

        free_capital = await fetch_account_capital(stable_ccy)
        budget_cap = SETTINGS.get("max_budget_cap", 1000.0)
        allocated_capital_base = free_capital / max_allowed

        if SETTINGS.get("compounding_enabled", False):
            safe_budget = min(allocated_capital_base * 0.98, allocated_capital_base)
        else:
            safe_budget = min(allocated_capital_base * 0.98, budget_cap)

        if safe_budget < 5.0:
            log_event(f"Nedostatok prostriedkov na ucte: {free_capital:.2f} {ACTIVE_BALANCE_CCY}")
            return

        ticker_res = await okx_public_request(f"/api/v5/market/ticker?instId={okx_symbol}")
        if ticker_res.get("code") != "0" or not ticker_res.get("data"):
            log_event(f"Nepodarilo sa ziskat cenu pre {okx_symbol}")
            return

        current_price = safe_float(ticker_res["data"][0]["last"])
        max_slip_pct = float(SETTINGS.get("max_entry_slip_pct", 0.35))
        if bool(SETTINGS.get("use_anti_chase", False)) and signal_close_price > 0 and current_price > 0:
            slip_above_close_pct = ((current_price - signal_close_price) / signal_close_price) * 100.0
            if slip_above_close_pct > max_slip_pct:
                WAIT_FOR_SIGNAL_RESET[coin_name] = True
                log_event(f"Nakup {okx_symbol} preskoceny: aktualna cena uz utiekla +{slip_above_close_pct:.2f}% nad uzavretie sviecky.")
                return
        raw_amount = safe_budget / current_price

        lot_sz, min_sz = await get_instrument_rules(okx_symbol)
        formatted_sz = format_amount(raw_amount, lot_sz, min_sz)

        if not formatted_sz:
            log_event(f"Mnozstvo pre {okx_symbol} nedosahuje minimum burzy minSz ({min_sz})")
            return

        order_body = json.dumps({
            "instId": okx_symbol, "tdMode": "cash", "side": "buy",
            "ordType": "market", "sz": formatted_sz
        })

        order_res = await okx_request("POST", "/api/v5/trade/order", order_body)
        if order_res.get("code") != "0":
            err_msg = order_res.get("msg", "Neznama chyba OKX")
            DEMO_REJECTED_COOLDOWN[okx_symbol] = time.time() + DEMO_REJECTED_COOLDOWN_SEC
            log_event(f"Par {okx_symbol} odmietnuty v Demo rezime ({err_msg}) - pauza {int(DEMO_REJECTED_COOLDOWN_SEC // 60)} min.")
            return

        if entry_features is None:
            entry_features = await get_current_ai_features_for_symbol(okx_symbol, bar_tf=CURRENT_TIMEFRAME.lower())
        if entry_features is not None and (not ai_factors or ai_factors == "--"):
            ai_factors = describe_ai_factors(entry_features)
        if ai_prob is None and entry_features is not None:
            ai_prob = predict_ai_probability(entry_features)

        coin_amount = float(formatted_sz)
        actual_spent_usdc = coin_amount * current_price
        entry_fee_est, exit_fee_est, total_fee_est = calc_simulated_fee_metrics(actual_spent_usdc, actual_spent_usdc)
        init_net_usdc = -total_fee_est
        init_net_pct = (init_net_usdc / actual_spent_usdc * 100.0) if actual_spent_usdc > 0 else 0.0
        use_fixed_sl = bool(SETTINGS.get("use_fixed_sl", False))
        fixed_sl_pct = float(SETTINGS.get("fixed_sl_pct", 1.5))
        fixed_sl_price = (current_price * (1.0 - (fixed_sl_pct / 100.0))) if use_fixed_sl else None
        use_fixed_tp = bool(SETTINGS.get("use_fixed_tp", False))
        fixed_tp_pct = float(SETTINGS.get("fixed_tp_pct", 1.2))
        fixed_tp_price = current_price * (1.0 + (fixed_tp_pct / 100.0))

        ACTIVE_TRADES[coin_name] = {
            "entry_price": current_price,
            "current_price": current_price,
            "use_fixed_sl": use_fixed_sl,
            "fixed_sl_price": fixed_sl_price,
            "fixed_sl_pct": fixed_sl_pct,
            "use_fixed_tp": use_fixed_tp,
            "fixed_tp_price": fixed_tp_price,
            "fixed_tp_pct": fixed_tp_pct,
            "be_active": False,
            "ttp_active": False,
            "ttp_stop_price": None,
            "trail_stop_price": None,
            "amount": coin_amount,
            "initial_amount": coin_amount,
            "lot_sz": lot_sz,
            "min_sz": min_sz,
            "bought_at": get_sk_time_str(),
            "market_pair": okx_symbol,
            "highest_price": current_price,
            "invested_usdc": actual_spent_usdc,
            "entry_fee_usdc": entry_fee_est,
            "est_total_fee_usdc": total_fee_est,
            "gross_pnl_pct": 0.0,
            "gross_pnl_usdc": 0.0,
            "live_pnl_pct": round(init_net_pct, 3),
            "live_pnl_usdc": round(init_net_usdc, 4),
            "tf": CURRENT_TIMEFRAME,
            "ai_prob": ai_prob,
            "ai_factors": ai_factors,
            "entry_features": entry_features,
            "source": "AUTO",
        }
        save_persistent_data()

        sl_info = f" | Fixny SL: -{fixed_sl_pct}% ({fixed_sl_price:.4f})" if (use_fixed_sl and fixed_sl_price) else " | Fixny SL: VYPNUTY"
        tp_info = f" | Fixny TP: +{fixed_tp_pct}% ({fixed_tp_price:.4f})" if use_fixed_tp else ""
        ai_info = f" | AI: {ai_prob*100:.1f}% [{ai_factors}]" if ai_prob is not None else ""
        log_event(f"NAKUP [{SETTINGS.get('mode_choice')}] [{CURRENT_TIMEFRAME}]: {okx_symbol} za {current_price:.4f} {ACTIVE_BALANCE_CCY} | Investicia: {actual_spent_usdc:.2f} {ACTIVE_BALANCE_CCY}{sl_info}{tp_info}{ai_info}", persist=True)

        inline_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "ZATVORIT VSETKO", "callback_data": "sell_now"},
                    {"text": "PAUZA BOTA", "callback_data": "toggle_bot"}
                ]
            ]
        }
        send_telegram_msg(
            f"<b>NAKUP [{SETTINGS.get('mode_choice')} · TF: {CURRENT_TIMEFRAME}]</b>\n"
            f"<b>Par:</b> {okx_symbol}\n"
            f"<b>Vstup:</b> {current_price:.4f}\n"
            f"<b>Investicia:</b> {actual_spent_usdc:.2f} {ACTIVE_BALANCE_CCY}\n"
            + (f"<b>AI skore:</b> {ai_prob*100:.1f}% [{ai_factors}]\n" if ai_prob is not None else "")
            + (f"<b>Fixny Stop-Loss:</b> -{fixed_sl_pct}% ({fixed_sl_price:.4f})\n" if (use_fixed_sl and fixed_sl_price) else "")
            + (f"<b>Fixny Take Profit:</b> +{fixed_tp_pct}% ({fixed_tp_price:.4f})\n" if use_fixed_tp else "")
            + "<b>Vystup:</b> podla nastaveni v APEX (TTP / TS / BE / Fixny SL / HA Exit)",
            reply_markup=inline_keyboard
        )


async def fetch_coin_available_balance(coin_ccy: str) -> float | None:
    try:
        res = await okx_request("GET", f"/api/v5/account/balance?ccy={coin_ccy.upper()}")
        if res.get("code") == "0":
            for acc in res.get("data", []):
                for item in acc.get("details", []):
                    if str(item.get("ccy", "")).upper() == coin_ccy.upper():
                        return safe_float(item.get("availBal"), 0.0)
            return 0.0
    except Exception:
        pass
    return None


async def close_trade_position(coin: str, reason: str):
    global ACTIVE_TRADES, LAST_TRADE_TIME
    async with trade_lock:
        if coin not in ACTIVE_TRADES:
            return
        trade = ACTIVE_TRADES[coin]
        now_ts = time.time()
        if now_ts < safe_float(trade.get("sell_retry_after"), 0.0):
            return

        symbol = trade["market_pair"]
        sell_ok = False
        try:
            ticker_res = await okx_public_request(f"/api/v5/market/ticker?instId={symbol}")
            curr_p = safe_float(ticker_res["data"][0]["last"]) if ticker_res.get("code") == "0" and ticker_res.get("data") else trade.get("current_price", trade["entry_price"])

            lot_sz = trade.get("lot_sz", 0.01)
            min_sz = trade.get("min_sz", 0.01)
            sell_amount = safe_float(trade["amount"], 0.0)

            real_avail = await fetch_coin_available_balance(coin)
            if real_avail is not None:
                if real_avail < min_sz:
                    log_event(f"Pozicia {symbol} uz nema predajny zostatok na OKX ({real_avail} < {min_sz}) - uvolnujem zo zoznamu.", persist=True)
                    del ACTIVE_TRADES[coin]
                    LAST_TRADE_TIME[coin] = time.time()
                    save_persistent_data()
                    return
                sell_amount = min(sell_amount, real_avail)

            fmt_sz = format_amount(sell_amount, lot_sz, min_sz)
            if not fmt_sz:
                fmt_sz = f"{sell_amount:.4f}"

            order_body = json.dumps({
                "instId": symbol, "tdMode": "cash", "side": "sell",
                "ordType": "market", "sz": fmt_sz
            })
            sell_res = await okx_request("POST", "/api/v5/trade/order", order_body)
            if sell_res.get("code") != "0":
                fail_cnt = int(trade.get("sell_fail_count", 0)) + 1
                trade["sell_fail_count"] = fail_cnt
                trade["sell_retry_after"] = now_ts + 30.0
                if fail_cnt >= 10:
                    log_event(f"Predaj {symbol} zlyhal {fail_cnt}x ({sell_res.get('msg')}) - uvolnujem poziciu, aby neblokovala sloty.", persist=True)
                    del ACTIVE_TRADES[coin]
                    LAST_TRADE_TIME[coin] = time.time()
                    save_persistent_data()
                else:
                    log_event(f"Predaj {symbol} vratil chybu z OKX Demo ({sell_res.get('msg')}) - pokus #{fail_cnt}/10 (dalsi o 30s).", persist=True)
                return

            sell_ok = True
            gross_pnl_pct = ((curr_p - trade["entry_price"]) / trade["entry_price"]) * 100.0
            invested = trade.get("invested_usdc", trade["amount"] * trade["entry_price"])
            gross_pnl_usdc = invested * (gross_pnl_pct / 100.0)
            exit_val_usdc = max(0.0, invested + gross_pnl_usdc)
            _, _, total_fee_usdc = calc_simulated_fee_metrics(invested, exit_val_usdc)
            pnl_usdc = gross_pnl_usdc - total_fee_usdc
            pnl_pct = (pnl_usdc / invested * 100.0) if invested > 0 else 0.0
            result_square = "🟩" if pnl_usdc >= 0 else "🟥"

            feats = trade.get("entry_features")
            if not feats:
                feats = await get_current_ai_features_for_symbol(symbol, bar_tf=str(trade.get("tf", CURRENT_TIMEFRAME)).lower())

            t_style = classify_trade_style(trade.get("tf", CURRENT_TIMEFRAME), trade.get("source", "AUTO"))
            record_closed_trade(
                symbol,
                pnl_pct,
                pnl_usdc,
                reason,
                feats,
                trade_style=t_style,
                gross_pnl_pct=gross_pnl_pct,
                gross_pnl_usdc=gross_pnl_usdc,
                fee_usdc=total_fee_usdc,
                persist=False,
            )
            learn_from_closed_trade(feats, pnl_usdc, trade_style=t_style, pnl_pct=pnl_pct, persist=False)
            LAST_TRADE_TIME[coin] = time.time()
            WAIT_FOR_SIGNAL_RESET[coin] = True
            if "SELL" in str(reason).upper():
                WAITING_FOR_SELL_AFTER_EXIT[coin] = False
            else:
                WAITING_FOR_SELL_AFTER_EXIT[coin] = True

            fee_log_txt = f" | Hruby: {gross_pnl_usdc:+.2f}$ | Poplatky: -{total_fee_usdc:.2f}$" if SETTINGS.get("simulate_fees", True) else ""
            log_event(f"{result_square} Uzavreta pozicia ({reason}) pre {symbol} | Cisty PnL: {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $){fee_log_txt}", persist=False)
            send_telegram_msg(
                f"{result_square} <b>UZAVRETA POZICIA ({reason})</b>\n"
                f"<b>Par:</b> {symbol}\n"
                f"<b>Cisty PnL:</b> {pnl_pct:+.2f}% ({pnl_usdc:+.2f} $)"
                + (f"\n<b>Hruby PnL / Poplatky:</b> {gross_pnl_usdc:+.2f}$ / -{total_fee_usdc:.2f}$" if SETTINGS.get("simulate_fees", True) else "")
            )
        except Exception as e:
            log_event(f"Chyba pri zatvarani {symbol}: {e}")
        finally:
            if sell_ok and coin in ACTIVE_TRADES:
                del ACTIVE_TRADES[coin]
                save_persistent_data()


async def fixed_stoploss_monitor_loop():
    global ACTIVE_TRADES, CURRENT_STAGE
    await asyncio.sleep(3)
    while True:
        if not ACTIVE_TRADES:
            if IS_BOT_ACTIVE:
                max_p = SETTINGS.get("max_parallel", 1)
                mode_lbl = SETTINGS.get("mode_choice", "FIB")
                sl_txt = f" | Fixny SL: -{SETTINGS.get('fixed_sl_pct', 1.5)}%" if SETTINGS.get("use_fixed_sl", False) else " | Fixny SL: OFF"
                tp_txt = f" | Fixny TP: +{SETTINGS.get('fixed_tp_pct', 1.2)}%" if SETTINGS.get("use_fixed_tp", False) else ""
                ai_txt = f" | AI >= {SETTINGS.get('ai_min_prob', 0.38)*100:.0f}%" if SETTINGS.get("use_ai_filter", False) else ""
                CURRENT_STAGE = f"STAV: Bot aktivny ({mode_lbl} · Max {max_p}x{sl_txt}{tp_txt}{ai_txt}). Cakam na BUY/SELL..."
            else:
                CURRENT_STAGE = "STAV: Bot pozastaveny."
            await asyncio.sleep(4)
            continue

        try:
            use_be = bool(SETTINGS.get("use_breakeven", True))
            be_trig = float(SETTINGS.get("be_trigger_pct", 1.2))
            use_ts = bool(SETTINGS.get("use_trailing", True))
            ts_pct = float(SETTINGS.get("trail_pct", 1.5))
            use_ttp = bool(SETTINGS.get("use_trailing_tp", True))
            ttp_trig = float(SETTINGS.get("ttp_trigger_pct", 1.2))
            ttp_cb = float(SETTINGS.get("ttp_callback_pct", 0.4))

            for coin in list(ACTIVE_TRADES.keys()):
                if coin not in ACTIVE_TRADES:
                    continue
                trade = ACTIVE_TRADES[coin]
                symbol = trade["market_pair"]

                ticker_res = await okx_public_request(f"/api/v5/market/ticker?instId={symbol}")
                if ticker_res.get("code") == "0" and ticker_res.get("data"):
                    current_price = safe_float(ticker_res["data"][0]["last"])
                else:
                    current_price = trade["entry_price"]

                if coin not in ACTIVE_TRADES:
                    continue

                if current_price > 0:
                    trade["current_price"] = current_price
                    if current_price > trade["highest_price"]:
                        trade["highest_price"] = current_price

                    entry_p = trade["entry_price"]
                    highest_p = trade["highest_price"]
                    gross_pnl_pct = ((current_price - entry_p) / entry_p) * 100.0
                    invested = trade.get("invested_usdc", trade["amount"] * entry_p)
                    gross_pnl_usdc = invested * (gross_pnl_pct / 100.0)
                    curr_val_usdc = max(0.0, invested + gross_pnl_usdc)
                    entry_fee_u, _, total_fee_u = calc_simulated_fee_metrics(invested, curr_val_usdc)
                    pnl_usdc = gross_pnl_usdc - total_fee_u
                    pnl_pct = (pnl_usdc / invested * 100.0) if invested > 0 else 0.0
                    trade["entry_fee_usdc"] = entry_fee_u
                    trade["est_total_fee_usdc"] = total_fee_u
                    trade["gross_pnl_pct"] = gross_pnl_pct
                    trade["gross_pnl_usdc"] = gross_pnl_usdc
                    trade["live_pnl_pct"] = pnl_pct
                    trade["live_pnl_usdc"] = pnl_usdc

                    use_fixed_sl_now = bool(SETTINGS.get("use_fixed_sl", False))
                    fixed_sl_pct_now = float(SETTINGS.get("fixed_sl_pct", trade.get("fixed_sl_pct", 1.5)))
                    fixed_sl_price_now = entry_p * (1.0 - (fixed_sl_pct_now / 100.0))
                    trade["use_fixed_sl"] = use_fixed_sl_now
                    trade["fixed_sl_pct"] = fixed_sl_pct_now
                    trade["fixed_sl_price"] = fixed_sl_price_now

                    use_fixed_tp = bool(SETTINGS.get("use_fixed_tp", False))
                    fixed_tp_pct = float(SETTINGS.get("fixed_tp_pct", trade.get("fixed_tp_pct", 1.2)))
                    fixed_tp_price = entry_p * (1.0 + (fixed_tp_pct / 100.0))
                    trade["use_fixed_tp"] = use_fixed_tp
                    trade["fixed_tp_pct"] = fixed_tp_pct
                    trade["fixed_tp_price"] = fixed_tp_price

                    sl_stage = f" | Fixny SL: -{fixed_sl_pct_now}%" if use_fixed_sl_now else ""
                    tp_stage = f" | Fixny TP: +{fixed_tp_pct}%" if use_fixed_tp else ""
                    CURRENT_STAGE = f"STAV: Pozicii: {len(ACTIVE_TRADES)} | {symbol} ({pnl_pct:+.2f}%){sl_stage}{tp_stage}"

                    if use_fixed_sl_now and current_price <= fixed_sl_price_now:
                        await close_trade_position(coin, f"Fixny Stop-Loss (-{fixed_sl_pct_now}%)")
                        continue

                    if use_fixed_tp and current_price >= fixed_tp_price:
                        if not trade.get("tp_goal_reached", False):
                            trade["tp_goal_reached"] = True
                            if use_be:
                                trade["be_active"] = True
                            if use_ttp:
                                if not trade.get("ttp_active", False):
                                    trade["ttp_active"] = True
                                    trade["ttp_stop_price"] = highest_p * (1.0 - (ttp_cb / 100.0))
                                log_event(f"Ciel TP (+{fixed_tp_pct}%) dosiahnuty pre {symbol} -> uzamknute a pokracuje TTP (odstup {ttp_cb}%)!")
                            else:
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
                            trade["ttp_stop_price"] = highest_p * (1.0 - (ttp_cb / 100.0))
                            log_event(f"Trailing TP aktivovany pre {symbol} pri +{pnl_pct:.2f}% (odstup {ttp_cb}%).")
                        if trade.get("ttp_active", False):
                            ttp_stop = highest_p * (1.0 - (ttp_cb / 100.0))
                            trade["ttp_stop_price"] = ttp_stop
                            if current_price <= ttp_stop:
                                ttp_reason = (
                                    f"Fixny TP (+{fixed_tp_pct}%) -> Trailing TP (-{ttp_cb}% z maxima)"
                                    if trade.get("tp_goal_reached", False)
                                    else f"Trailing Take-Profit (-{ttp_cb}% z maxima)"
                                )
                                await close_trade_position(coin, ttp_reason)
                                continue

                    if use_ts and highest_p > entry_p:
                        ts_stop = highest_p * (1.0 - (ts_pct / 100.0))
                        if use_be and trade.get("be_active", False):
                            ts_stop = max(ts_stop, entry_p)
                        trade["trail_stop_price"] = ts_stop
                        if current_price <= ts_stop:
                            await close_trade_position(coin, f"Trailing Stop (-{ts_pct}% z maxima)")
                            continue
                    else:
                        trade["trail_stop_price"] = None
        except Exception as e:
            log_event(f"Chyba v monitore rizik: {e}")
        await asyncio.sleep(4)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global MARKET_REGIME, IS_SIDEWAYS_MARKET
    load_persistent_data()
    if ACTIVE_TRADES:
        log_event(f"Obnovenych {len(ACTIVE_TRADES)} otvorenych pozicii z pamate: {', '.join(ACTIVE_TRADES.keys())}", persist=True)
    await update_top_liquid_symbols()
    regime_now, sideways_now = await get_market_regime_and_sideways(CURRENT_TIMEFRAME.lower())
    MARKET_REGIME = regime_now
    IS_SIDEWAYS_MARKET = sideways_now
    if SETTINGS.get("use_ai_filter", False) and AI_STATE.get("samples_count", 0) < 10:
        seed_sym = TOP_LIQUID_SYMBOLS[0] if TOP_LIQUID_SYMBOLS else "SOL-USDC"
        await train_ai_model_from_candles(seed_sym, bar_tf=CURRENT_TIMEFRAME.lower(), force=True)
    else:
        save_ai_memory()
    log_event(
        f"Autonomny APEX BOT ({len(TOP_LIQUID_SYMBOLS)} symbolov · Rezim: {SETTINGS.get('mode_choice')} · Fixny SL: {'ON' if SETTINGS.get('use_fixed_sl') else 'OFF'}) spusteny | AI pamat: {AI_MEMORY_FILE}",
        persist=True,
    )
    monitor_task = asyncio.create_task(fixed_stoploss_monitor_loop())
    scanner_task = asyncio.create_task(autonomous_scanner_loop())
    updater_task = asyncio.create_task(top_symbols_updater_loop())
    send_telegram_msg(f"APEX BOT ({len(TOP_LIQUID_SYMBOLS)} symbolov · Rezim: {SETTINGS.get('mode_choice')}) pripraveny!")
    try:
        yield
    finally:
        save_persistent_data()
        for task in (monitor_task, scanner_task, updater_task):
            task.cancel()
        await asyncio.gather(monitor_task, scanner_task, updater_task, return_exceptions=True)


app = FastAPI(lifespan=lifespan)


def get_pine_settings_dict():
    return {
        "mode_choice": SETTINGS["mode_choice"],
        "require_green_ha_buy": SETTINGS["require_green_ha_buy"],
        "require_bull_tf_buy": SETTINGS.get("require_bull_tf_buy", False),
        "require_sell_before_rebuy": SETTINGS.get("require_sell_before_rebuy", False),
        "strict_buy_filter": SETTINGS["strict_buy_filter"],
        "fast_len": SETTINGS["fast_len"],
        "slow_len": SETTINGS["slow_len"],
        "signal_len": SETTINGS["signal_len"],
        "pred_look": SETTINGS["pred_look"],
        "pivot_len": SETTINGS["pivot_len"],
        "fib_choice": SETTINGS["fib_choice"],
        "use_fixed_sl": SETTINGS.get("use_fixed_sl", False),
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
        "sell_on_osc_reversal": SETTINGS["sell_on_osc_reversal"],
        "block_sideways_buy": SETTINGS["block_sideways_buy"],
        "min_rel_volume": SETTINGS["min_rel_volume"],
        "use_anti_chase": SETTINGS["use_anti_chase"],
        "max_candle_pump_pct": SETTINGS["max_candle_pump_pct"],
        "max_ema9_dist_pct": SETTINGS["max_ema9_dist_pct"],
        "max_upper_wick_ratio": SETTINGS["max_upper_wick_ratio"],
        "max_entry_slip_pct": SETTINGS["max_entry_slip_pct"],
        "use_ai_filter": SETTINGS["use_ai_filter"],
        "ai_min_prob": SETTINGS["ai_min_prob"],
        "simulate_fees": SETTINGS.get("simulate_fees", True),
        "taker_fee_pct": SETTINGS.get("taker_fee_pct", 0.10),
    }


@app.post("/api/telegram-webhook")
async def telegram_webhook(request: Request):
    try:
        secret_hdr = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if WEBHOOK_SECRET and secret_hdr != WEBHOOK_SECRET:
            return JSONResponse({"status": "unauthorized"}, status_code=401)
        data = await request.json()
        incoming_chat_id = ""
        if "callback_query" in data:
            incoming_chat_id = str(data["callback_query"].get("message", {}).get("chat", {}).get("id", "")).strip()
        elif "message" in data:
            incoming_chat_id = str(data["message"].get("chat", {}).get("id", "")).strip()
        if TELEGRAM_CHAT_ID and incoming_chat_id and incoming_chat_id != TELEGRAM_CHAT_ID:
            return JSONResponse({"status": "forbidden"}, status_code=403)
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
    log_event(f"Prepinal bota: {status_str.upper()}", persist=True)
    send_telegram_msg(f"Zmena stavu: Bot je teraz {status_str.upper()}.")
    return JSONResponse({"status": "success", "is_bot_active": IS_BOT_ACTIVE})


@app.post("/api/toggle-btc-filter")
async def toggle_btc_filter():
    SETTINGS["use_btc_filter"] = not SETTINGS.get("use_btc_filter", False)
    status_str = "zapnuty (aktivny)" if SETTINGS["use_btc_filter"] else "vypnuty (deaktivovany)"
    log_event(f"BTC Trend Guard filter: {status_str.upper()}", persist=True)
    send_telegram_msg(f"Zmena stavu: BTC Trend Guard filter je teraz {status_str.upper()}.")
    return JSONResponse({"status": "success", "use_btc_filter": SETTINGS["use_btc_filter"]})


@app.post("/api/toggle-compounding")
async def toggle_compounding():
    SETTINGS["compounding_enabled"] = not SETTINGS.get("compounding_enabled", False)
    status_str = "ZAPNUTE (Zlozene urocenie)" if SETTINGS["compounding_enabled"] else "VYPNUTE (Pevny rozpocet)"
    log_event(f"Automaticke reinvestovanie (COMPOUNDING): {status_str.upper()}", persist=True)
    send_telegram_msg(f"Zmena stavu: Auto-Compounding je teraz {status_str}.")
    return JSONResponse({"status": "success", "compounding_enabled": SETTINGS["compounding_enabled"]})


@app.post("/api/set-budget/{amount}")
async def set_budget(amount: int):
    if amount in ALLOWED_BUDGET_CAPS:
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
        SETTINGS["use_fixed_sl"] = True
        log_event(f"Fixny Stop-Loss nastaveny na -{sl_val}% a zapnuty.", persist=True)
        send_telegram_msg(f"Zmena Stop-Lossu: Fixny SL je zapnuty na -{sl_val}%.")
        return JSONResponse({"status": "success", "fixed_sl_pct": sl_val, "use_fixed_sl": True})
    return JSONResponse({"status": "error", "message": "Neplatna hodnota SL (povolene 0.1 - 50.0 %)." })


@app.post("/api/set-tf/{tf_val}")
async def set_tf(tf_val: str):
    global CURRENT_TIMEFRAME, MARKET_SCANNER_CACHE, MARKET_REGIME, IS_SIDEWAYS_MARKET
    tf_lower = tf_val.lower()
    if tf_lower in ALLOWED_TIMEFRAMES:
        CURRENT_TIMEFRAME = tf_val.upper()
        MARKET_SCANNER_CACHE.clear()
        regime_now, sideways_now = await get_market_regime_and_sideways(tf_lower)
        MARKET_REGIME = regime_now
        IS_SIDEWAYS_MARKET = sideways_now
        if SETTINGS.get("use_ai_filter", False):
            seed_sym = TOP_LIQUID_SYMBOLS[0] if TOP_LIQUID_SYMBOLS else "SOL-USDC"
            await train_ai_model_from_candles(seed_sym, bar_tf=tf_lower, force=True)
        log_event(f"Casovy ramec (TF) zmeneny na: {CURRENT_TIMEFRAME} (Rezim: {regime_now})", persist=True)
        return JSONResponse({"status": "success", "tf": CURRENT_TIMEFRAME, "market_regime": regime_now})
    return JSONResponse({"status": "error", "message": "Neplatny casovy ramec."})


@app.post("/api/train-ai")
async def api_train_ai():
    seed_sym = TOP_LIQUID_SYMBOLS[0] if TOP_LIQUID_SYMBOLS else "SOL-USDC"
    res = await train_ai_model_from_candles(seed_sym, bar_tf=CURRENT_TIMEFRAME.lower(), force=True)
    if res.get("ok"):
        MARKET_SCANNER_CACHE.clear()
        return JSONResponse({"status": "success", "ai_state": AI_STATE, "details": res})
    return JSONResponse({"status": "error", "message": res.get("msg", "Chyba trenovania AI.")})


@app.post("/api/clean-reset-learning")
async def api_clean_reset_learning():
    global CLOSED_TRADES_HISTORY, AI_LEARNED_TRADES, PATTERN_MEMORY, AI_STATE, SYSTEM_LOG_ENTRIES, SYSTEM_LOG_24H_ENTRIES, SYSTEM_LOGS
    CLOSED_TRADES_HISTORY.clear()
    AI_LEARNED_TRADES.clear()
    PATTERN_MEMORY.clear()
    SYSTEM_LOG_ENTRIES.clear()
    SYSTEM_LOG_24H_ENTRIES.clear()
    SYSTEM_LOGS.clear()
    AI_STATE = {
        "weights": [0.0] * AI_FEATURE_COUNT,
        "bias": 0.0,
        "samples_count": 0,
        "live_learned_count": 0,
        "live_wins_count": 0,
        "win_rate_train": 0.0,
        "accuracy_train": 0.0,
        "trained_at": "--",
        "last_symbol": "--",
        "last_prob": None,
        "last_factors": "--",
        "blocked_count": 0,
        "passed_count": 0,
        "_last_train_ts": 0.0,
    }
    save_persistent_data()
    seed_sym = TOP_LIQUID_SYMBOLS[0] if TOP_LIQUID_SYMBOLS else "SOL-USDC"
    await train_ai_model_from_candles(seed_sym, bar_tf=CURRENT_TIMEFRAME.lower(), force=True)
    log_event("Cisty reset AI pamate, vzorov a historie obchodov vykonany pre novy zber vzoriek.", persist=True)
    return JSONResponse({"status": "success", "ai_readiness": compute_ai_readiness_info()})


@app.post("/api/clear-logs")
async def api_clear_logs():
    global LAST_LOG_PRUNE_STR
    SYSTEM_LOG_ENTRIES.clear()
    SYSTEM_LOGS.clear()
    LAST_LOG_PRUNE_STR = get_sk_time_str()
    log_event("System Execution Logs rucne precistene (24h zaznamy zachovane).", persist=True)
    return JSONResponse({"status": "success"})


@app.post("/api/update-pine-settings")
async def update_pine_settings(request: Request):
    global MARKET_SCANNER_CACHE
    try:
        payload = await request.json()
        mode_val = str(payload.get("mode_choice", SETTINGS["mode_choice"])).upper()
        if mode_val not in ["ALL_GREEN", "BREAKOUT", "FIB"]:
            mode_val = "FIB"

        fib_val = str(payload.get("fib_choice", SETTINGS["fib_choice"]))
        if fib_val not in ["0.50", "0.618", "0.786"]:
            fib_val = "0.786"

        SETTINGS["mode_choice"] = mode_val
        SETTINGS["require_green_ha_buy"] = bool(payload.get("require_green_ha_buy", SETTINGS["require_green_ha_buy"]))
        SETTINGS["require_bull_tf_buy"] = bool(payload.get("require_bull_tf_buy", SETTINGS.get("require_bull_tf_buy", False)))
        SETTINGS["require_sell_before_rebuy"] = bool(payload.get("require_sell_before_rebuy", SETTINGS.get("require_sell_before_rebuy", False)))
        SETTINGS["strict_buy_filter"] = bool(payload.get("strict_buy_filter", SETTINGS["strict_buy_filter"]))
        SETTINGS["fast_len"] = max(1, min(200, int(payload.get("fast_len", SETTINGS["fast_len"]))))
        SETTINGS["slow_len"] = max(1, min(300, int(payload.get("slow_len", SETTINGS["slow_len"]))))
        SETTINGS["signal_len"] = max(1, min(200, int(payload.get("signal_len", SETTINGS["signal_len"]))))
        SETTINGS["pred_look"] = max(1, min(10, int(payload.get("pred_look", SETTINGS["pred_look"]))))

        SETTINGS["pivot_len"] = max(1, min(10, int(payload.get("pivot_len", SETTINGS["pivot_len"]))))
        SETTINGS["fib_choice"] = fib_val

        SETTINGS["use_fixed_sl"] = bool(payload.get("use_fixed_sl", SETTINGS.get("use_fixed_sl", False)))
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
        SETTINGS["sell_on_osc_reversal"] = bool(payload.get("sell_on_osc_reversal", SETTINGS["sell_on_osc_reversal"]))

        SETTINGS["block_sideways_buy"] = bool(payload.get("block_sideways_buy", SETTINGS["block_sideways_buy"]))
        SETTINGS["min_rel_volume"] = max(0.0, min(5.0, float(payload.get("min_rel_volume", SETTINGS["min_rel_volume"]))))
        SETTINGS["use_anti_chase"] = bool(payload.get("use_anti_chase", SETTINGS["use_anti_chase"]))
        SETTINGS["max_candle_pump_pct"] = max(0.10, min(5.0, float(payload.get("max_candle_pump_pct", SETTINGS["max_candle_pump_pct"]))))
        SETTINGS["max_ema9_dist_pct"] = max(0.10, min(5.0, float(payload.get("max_ema9_dist_pct", SETTINGS["max_ema9_dist_pct"]))))
        SETTINGS["max_upper_wick_ratio"] = max(0.10, min(0.95, float(payload.get("max_upper_wick_ratio", SETTINGS["max_upper_wick_ratio"]))))
        SETTINGS["max_entry_slip_pct"] = max(0.05, min(5.0, float(payload.get("max_entry_slip_pct", SETTINGS["max_entry_slip_pct"]))))
        SETTINGS["use_ai_filter"] = bool(payload.get("use_ai_filter", SETTINGS["use_ai_filter"]))
        SETTINGS["ai_min_prob"] = max(0.25, min(0.90, float(payload.get("ai_min_prob", SETTINGS["ai_min_prob"]))))
        SETTINGS["simulate_fees"] = bool(payload.get("simulate_fees", SETTINGS.get("simulate_fees", True)))
        if "taker_fee_pct" in payload and payload["taker_fee_pct"] is not None:
            SETTINGS["taker_fee_pct"] = max(0.0, min(2.0, float(payload.get("taker_fee_pct", SETTINGS.get("taker_fee_pct", 0.10)))))

        MARKET_SCANNER_CACHE.clear()
        save_persistent_data()
        sl_log = f"-{SETTINGS['fixed_sl_pct']}% (ZAP)" if SETTINGS["use_fixed_sl"] else "VYP"
        log_event(
            f"Nastavenia aktualizovane: Rezim={mode_val} (Fib={fib_val}), ZelenaHA={SETTINGS['require_green_ha_buy']}, "
            f"LenBULL={SETTINGS['require_bull_tf_buy']}, HAExit={SETTINGS['use_ha_exit']}({SETTINGS['ha_exit_consecutive']}x), FixSL={sl_log}",
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
            free_capital = await fetch_account_capital(stable_ccy)
            budget_cap = SETTINGS.get("max_budget_cap", 1000.0)
            allocated_capital_base = free_capital / max_allowed

            if SETTINGS.get("compounding_enabled", False):
                safe_budget = min(allocated_capital_base * 0.98, allocated_capital_base)
            else:
                safe_budget = min(allocated_capital_base * 0.98, budget_cap)

            if safe_budget < 5.0:
                return JSONResponse({"status": "error", "message": "Nedostatok prostriedkov."})

            ticker_res = await okx_public_request(f"/api/v5/market/ticker?instId={okx_symbol}")
            if ticker_res.get("code") != "0" or not ticker_res.get("data"):
                return JSONResponse({"status": "error", "message": f"Nepodarilo sa ziskat cenu pre {okx_symbol}"})

            current_price = safe_float(ticker_res["data"][0]["last"])
            raw_amount = safe_budget / current_price

            lot_sz, min_sz = await get_instrument_rules(okx_symbol)
            formatted_sz = format_amount(raw_amount, lot_sz, min_sz)
            if not formatted_sz:
                return JSONResponse({"status": "error", "message": f"Mnozstvo pod minimom minSz ({min_sz})"})

            order_body = json.dumps({
                "instId": okx_symbol, "tdMode": "cash", "side": "buy",
                "ordType": "market", "sz": formatted_sz
            })

            order_res = await okx_request("POST", "/api/v5/trade/order", order_body)
            if order_res.get("code") != "0":
                err_msg = order_res.get("msg", "Neznama chyba OKX")
                return JSONResponse({"status": "error", "message": f"OKX odmietlo prikaz ({err_msg})"})

            entry_features = await get_current_ai_features_for_symbol(okx_symbol, bar_tf=CURRENT_TIMEFRAME.lower())
            ai_factors = describe_ai_factors(entry_features) if entry_features is not None else "--"
            ai_prob = predict_ai_probability(entry_features) if entry_features is not None else None

            coin_amount = float(formatted_sz)
            actual_spent_usdc = coin_amount * current_price
            entry_fee_est, _, total_fee_est = calc_simulated_fee_metrics(actual_spent_usdc, actual_spent_usdc)
            init_net_usdc = -total_fee_est
            init_net_pct = (init_net_usdc / actual_spent_usdc * 100.0) if actual_spent_usdc > 0 else 0.0
            use_fixed_sl = bool(SETTINGS.get("use_fixed_sl", False))
            fixed_sl_pct = float(SETTINGS.get("fixed_sl_pct", 1.5))
            fixed_sl_price = (current_price * (1.0 - (fixed_sl_pct / 100.0))) if use_fixed_sl else None
            use_fixed_tp = bool(SETTINGS.get("use_fixed_tp", False))
            fixed_tp_pct = float(SETTINGS.get("fixed_tp_pct", 1.2))
            fixed_tp_price = current_price * (1.0 + (fixed_tp_pct / 100.0))

            ACTIVE_TRADES[coin_upper] = {
                "entry_price": current_price,
                "current_price": current_price,
                "use_fixed_sl": use_fixed_sl,
                "fixed_sl_price": fixed_sl_price,
                "fixed_sl_pct": fixed_sl_pct,
                "use_fixed_tp": use_fixed_tp,
                "fixed_tp_price": fixed_tp_price,
                "fixed_tp_pct": fixed_tp_pct,
                "be_active": False,
                "ttp_active": False,
                "ttp_stop_price": None,
                "trail_stop_price": None,
                "amount": coin_amount,
                "initial_amount": coin_amount,
                "lot_sz": lot_sz,
                "min_sz": min_sz,
                "bought_at": get_sk_time_str(),
                "market_pair": okx_symbol,
                "highest_price": current_price,
                "invested_usdc": actual_spent_usdc,
                "entry_fee_usdc": entry_fee_est,
                "est_total_fee_usdc": total_fee_est,
                "gross_pnl_pct": 0.0,
                "gross_pnl_usdc": 0.0,
                "live_pnl_pct": round(init_net_pct, 3),
                "live_pnl_usdc": round(init_net_usdc, 4),
                "tf": CURRENT_TIMEFRAME,
                "ai_prob": ai_prob,
                "ai_factors": ai_factors,
                "entry_features": entry_features,
                "source": "MANUAL",
            }
            save_persistent_data()
            sl_info = f" | Fixny SL: -{fixed_sl_pct}%" if use_fixed_sl else " | Fixny SL: OFF"
            tp_info = f" | Fixny TP: +{fixed_tp_pct}%" if use_fixed_tp else ""
            ai_info = f" | AI: {ai_prob*100:.1f}% [{ai_factors}]" if ai_prob is not None else ""
            log_event(f"Manualny nakup [{CURRENT_TIMEFRAME}]: {okx_symbol} za {current_price:.4f} | Investicia: {actual_spent_usdc:.2f}${sl_info}{tp_info}{ai_info}", persist=True)
            send_telegram_msg(f"<b>MANUALNY NAKUP [TF: {CURRENT_TIMEFRAME}]</b>\n<b>Par:</b> {okx_symbol}\n<b>Vstup:</b> {current_price:.4f}\n<b>Investicia:</b> {actual_spent_usdc:.2f} {ACTIVE_BALANCE_CCY}" + (f"\n<b>Fixny SL:</b> -{fixed_sl_pct}%" if use_fixed_sl else "") + (f"\n<b>Fixny TP:</b> +{fixed_tp_pct}%" if use_fixed_tp else ""))

            return JSONResponse({"status": "success", "symbol": okx_symbol})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})


@app.post("/api/sell-coin/{coin}")
async def sell_single_coin(coin: str):
    coin_upper = coin.upper().strip()
    if coin_upper not in ACTIVE_TRADES:
        return JSONResponse({"status": "error", "message": f"Pozicia {coin_upper} nie je otvorena."})
    symbol = ACTIVE_TRADES[coin_upper]["market_pair"]
    await close_trade_position(coin_upper, f"Rucny Emergency predaj ({symbol})")
    return JSONResponse({"status": "success", "sold": symbol})


@app.post("/api/sell-now")
async def sell_now():
    if not ACTIVE_TRADES:
        return JSONResponse({"status": "error", "message": "Ziadna aktivna pozicia na predaj."})
    sold = []
    for coin in list(ACTIVE_TRADES.keys()):
        if coin not in ACTIVE_TRADES:
            continue
        symbol = ACTIVE_TRADES[coin]["market_pair"]
        await close_trade_position(coin, "Rucny okamzity predaj (Emergency)")
        sold.append(symbol)
    return JSONResponse({"status": "success", "sold": sold})


HTML_TEMPLATE_CONTENT = r"""<!DOCTYPE html>
<html lang="sk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>APEX MATRIX_SCALPER_v1_BOT + AI · Quantum Neural Grid</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-void: #02040a;
            --bg-panel: rgba(7, 12, 24, 0.88);
            --bg-elevated: rgba(12, 20, 38, 0.94);
            --bg-input: rgba(4, 8, 17, 0.92);
            --border-subtle: rgba(56, 189, 248, 0.16);
            --border-strong: rgba(56, 189, 248, 0.36);
            --text-primary: #f1f5f9;
            --text-secondary: #94a3b8;
            --text-muted: #64748b;
            --accent-white: #ffffff;
            --accent-emerald: #10b981;
            --accent-emerald-dim: rgba(16, 185, 129, 0.14);
            --accent-crimson: #f43f5e;
            --accent-crimson-dim: rgba(244, 63, 94, 0.14);
            --accent-amber: #f59e0b;
            --accent-amber-dim: rgba(245, 158, 11, 0.16);
            --accent-cyan: #22d3ee;
            --accent-cyan-dim: rgba(34, 211, 238, 0.15);
            --accent-violet: #a855f7;
            --accent-violet-dim: rgba(168, 85, 247, 0.15);
            --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            --font-mono: 'JetBrains Mono', monospace;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        html, body {
            background-color: var(--bg-void);
            background-image:
                radial-gradient(circle at 18% 0%, rgba(34, 211, 238, 0.09), transparent 45%),
                radial-gradient(circle at 82% 8%, rgba(168, 85, 247, 0.08), transparent 45%),
                linear-gradient(to right, rgba(34, 211, 238, 0.032) 1px, transparent 1px),
                linear-gradient(to bottom, rgba(34, 211, 238, 0.032) 1px, transparent 1px);
            background-size: 100% 100%, 100% 100%, 36px 36px, 36px 36px;
            color: var(--text-primary);
            font-family: var(--font-sans);
            min-height: 100vh;
            padding: 16px 20px 40px;
            -webkit-font-smoothing: antialiased;
        }
        .mono { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
        .container { max-width: 1460px; width: 100%; margin: 0 auto; display: flex; flex-direction: column; gap: 14px; }

        .quantum-panel {
            background: var(--bg-panel);
            backdrop-filter: blur(14px);
            border: 1px solid var(--border-subtle);
            border-radius: 10px;
            position: relative;
            overflow: hidden;
        }
        .quantum-panel::before {
            content: "";
            position: absolute; top: 0; left: 0; right: 0; height: 1px;
            background: linear-gradient(90deg, transparent, rgba(34, 211, 238, 0.45), rgba(168, 85, 247, 0.35), transparent);
            pointer-events: none;
        }

        .desk-header { padding: 14px 18px; display: flex; flex-direction: column; gap: 12px; }
        .header-top { display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 14px; }
        .brand-title-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
        .quantum-badge {
            display: inline-flex; align-items: center; gap: 6px;
            padding: 3px 9px; border-radius: 999px;
            background: var(--accent-cyan-dim); border: 1px solid rgba(34, 211, 238, 0.4);
            font-family: var(--font-mono); font-size: 0.63rem; font-weight: 700;
            color: var(--accent-cyan); letter-spacing: 0.08em; text-transform: uppercase;
        }
        .quantum-badge-dot {
            width: 6px; height: 6px; border-radius: 50%;
            background: var(--accent-cyan); box-shadow: 0 0 8px var(--accent-cyan);
        }
        .brand h1 { font-size: 1.15rem; font-weight: 800; color: var(--accent-white); letter-spacing: -0.02em; font-family: var(--font-mono); }
        .telemetry-strip { display: flex; flex-wrap: wrap; gap: 8px 14px; margin-top: 8px; font-size: 0.7rem; color: var(--text-secondary); font-family: var(--font-mono); }
        .telemetry-item { display: inline-flex; align-items: center; gap: 5px; }
        .telemetry-val { color: var(--accent-amber); font-weight: 600; }
        .telemetry-val-cyan { color: var(--accent-cyan); font-weight: 600; }

        .header-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
        .btn {
            background: var(--bg-input); border: 1px solid var(--border-subtle); color: var(--text-primary);
            padding: 6px 11px; font-size: 0.68rem; font-weight: 600; font-family: var(--font-mono);
            cursor: pointer; border-radius: 6px; transition: all 0.15s ease;
            text-transform: uppercase; letter-spacing: 0.04em; display: inline-flex; align-items: center; justify-content: center; gap: 5px;
        }
        .btn:hover { border-color: var(--accent-cyan); background: var(--bg-elevated); color: var(--accent-white); }
        .btn-on { border-color: rgba(16, 185, 129, 0.55); color: var(--accent-emerald); background: var(--accent-emerald-dim); }
        .btn-on:hover { background: rgba(16, 185, 129, 0.22); border-color: var(--accent-emerald); }
        .btn-off { border-color: rgba(100, 116, 139, 0.35); color: var(--text-muted); }
        .btn-active-pill { border-color: var(--accent-cyan) !important; color: var(--accent-cyan) !important; background: var(--accent-cyan-dim) !important; font-weight: 700 !important; }
        .btn-sell { background: linear-gradient(135deg, #f43f5e, #e11d48); color: #ffffff; font-weight: 700; border-color: rgba(244, 63, 94, 0.6); }
        .btn-sell:hover { filter: brightness(1.08); }
        .btn-sell:disabled { opacity: 0.28; cursor: not-allowed; background: var(--bg-input); color: var(--text-muted); border-color: var(--border-subtle); filter: none; }

        .control-ribbon {
            display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
            gap: 10px; padding-top: 10px; border-top: 1px solid var(--border-subtle);
        }
        .ribbon-group { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; }
        .ribbon-label {
            font-size: 0.62rem; font-weight: 700; text-transform: uppercase;
            letter-spacing: 0.07em; color: var(--text-muted); margin-right: 4px; font-family: var(--font-mono);
        }
        .btn-mini { padding: 4px 8px; font-size: 0.65rem; }
        .custom-buy-box {
            display: inline-flex; align-items: center; gap: 4px;
            border: 1px solid var(--border-strong); padding: 2px 4px 2px 8px;
            border-radius: 6px; background: var(--bg-input);
        }
        .custom-buy-input {
            background: transparent; border: none; color: var(--accent-white);
            font-family: var(--font-mono); font-size: 0.68rem; font-weight: 600;
            width: 58px; outline: none; text-transform: uppercase;
        }
        .custom-buy-input::placeholder { color: var(--text-muted); }

        .status-bar {
            padding: 9px 14px;
            display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
        }
        .status-left { display: flex; align-items: center; gap: 10px; font-family: var(--font-mono); font-size: 0.73rem; font-weight: 600; color: var(--accent-amber); }
        .pulse-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent-emerald); box-shadow: 0 0 10px var(--accent-emerald); animation: pulseGlow 2s infinite; }
        @keyframes pulseGlow { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.55; transform: scale(0.85); } }
        .status-right { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
        .update-tag { font-family: var(--font-mono); font-size: 0.66rem; color: var(--text-muted); }
        .regime-badge {
            display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px;
            border-radius: 5px; font-family: var(--font-mono); font-size: 0.68rem; font-weight: 700;
            letter-spacing: 0.05em; border: 1px solid var(--border-strong);
        }
        .regime-bull { color: var(--accent-emerald); background: var(--accent-emerald-dim); border-color: rgba(16, 185, 129, 0.45); }
        .regime-bear { color: var(--accent-crimson); background: var(--accent-crimson-dim); border-color: rgba(244, 63, 94, 0.45); }
        .regime-sideways { color: var(--accent-amber); background: var(--accent-amber-dim); border-color: rgba(245, 158, 11, 0.55); }

        .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
        .stat-card { padding: 13px 16px; }
        .stat-label { font-size: 0.64rem; font-weight: 700; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.07em; }
        .stat-value { font-family: var(--font-mono); font-size: 1.25rem; font-weight: 700; color: var(--accent-white); margin-top: 5px; }
        .stat-sub { font-family: var(--font-mono); font-size: 0.64rem; color: var(--text-muted); margin-top: 4px; }

        /* Quantum Visual Grid: Candlestick Chart + OKX Spot Top 24h Gainers (USDC) Scanner */
        .visual-desk-grid { display: grid; grid-template-columns: 1.48fr 1.22fr; gap: 12px; }
        .visual-card { padding: 12px 14px; display: flex; flex-direction: column; gap: 10px; }
        .visual-header { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }
        .visual-title {
            font-family: var(--font-mono); font-size: 0.7rem; font-weight: 700;
            color: var(--accent-white); text-transform: uppercase; letter-spacing: 0.06em;
            display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
        }
        .live-pill {
            font-size: 0.58rem; padding: 2px 7px; border-radius: 999px;
            background: var(--accent-emerald-dim); color: var(--accent-emerald);
            border: 1px solid rgba(16, 185, 129, 0.35); font-weight: 700;
        }
        .cyan-pill {
            font-size: 0.58rem; padding: 2px 7px; border-radius: 999px;
            background: var(--accent-cyan-dim); color: var(--accent-cyan);
            border: 1px solid rgba(34, 211, 238, 0.35); font-weight: 700;
        }
        .violet-pill {
            font-size: 0.58rem; padding: 2px 7px; border-radius: 999px;
            background: var(--accent-violet-dim); color: #d8b4fe;
            border: 1px solid rgba(168, 85, 247, 0.38); font-weight: 700;
        }

        .chart-canvas-wrap {
            position: relative; width: 100%; height: 252px;
            background: #03060f; border: 1px solid var(--border-subtle); border-radius: 7px; overflow: hidden;
        }
        canvas.desk-canvas { width: 100%; height: 100%; display: block; cursor: crosshair; }
        .chart-crosshair-readout {
            position: absolute; top: 8px; left: 10px;
            background: rgba(4, 8, 18, 0.85); border: 1px solid var(--border-subtle);
            border-radius: 5px; padding: 3px 8px; font-family: var(--font-mono); font-size: 0.62rem;
            color: var(--text-secondary); pointer-events: none;
        }
        .chart-legend {
            display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap;
            gap: 8px; font-family: var(--font-mono); font-size: 0.63rem; color: var(--text-secondary);
        }
        .legend-items { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
        .legend-dot { width: 7px; height: 7px; border-radius: 2px; display: inline-block; margin-right: 4px; }

        .vol-scanner-layout { display: grid; grid-template-columns: 146px 1fr; gap: 12px; align-items: stretch; min-height: 252px; }
        .radar-column { display: flex; flex-direction: column; align-items: center; justify-content: space-between; gap: 8px; }
        .radar-stage {
            position: relative; width: 142px; height: 142px;
            border-radius: 50%; background: radial-gradient(circle, rgba(34,211,238,0.14) 0%, rgba(168,85,247,0.05) 55%, #03060f 76%);
            border: 1px solid rgba(34, 211, 238, 0.32); overflow: hidden;
        }
        .radar-ring {
            position: absolute; border: 1px dashed rgba(34, 211, 238, 0.14); border-radius: 50%;
            top: 50%; left: 50%; transform: translate(-50%, -50%);
        }
        .radar-ring.r1 { width: 46px; height: 46px; }
        .radar-ring.r2 { width: 94px; height: 94px; }
        .radar-ring.r3 { width: 136px; height: 136px; }
        .radar-cross-h { position: absolute; top: 50%; left: 0; right: 0; height: 1px; background: rgba(34,211,238,0.08); }
        .radar-cross-v { position: absolute; left: 50%; top: 0; bottom: 0; width: 1px; background: rgba(34,211,238,0.08); }
        .radar-sweep {
            position: absolute; inset: 0; border-radius: 50%;
            background: conic-gradient(from 0deg, rgba(34, 211, 238, 0.42) 0deg, rgba(168, 85, 247, 0.12) 55deg, transparent 105deg);
            animation: radarSpin 3.4s linear infinite;
        }
        @keyframes radarSpin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
        .radar-blip {
            position: absolute; width: 7px; height: 7px; border-radius: 50%;
            transform: translate(-50%, -50%); animation: blipPulse 2.1s infinite ease-in-out;
        }
        .blip-buy { background: var(--accent-emerald); box-shadow: 0 0 10px var(--accent-emerald); }
        .blip-scan { background: var(--accent-cyan); box-shadow: 0 0 8px var(--accent-cyan); }
        .blip-warn { background: var(--accent-amber); box-shadow: 0 0 8px var(--accent-amber); }
        @keyframes blipPulse {
            0%, 100% { transform: translate(-50%, -50%) scale(1); opacity: 0.95; }
            50% { transform: translate(-50%, -50%) scale(1.45); opacity: 0.5; }
        }
        .radar-meta-box {
            width: 100%; background: var(--bg-input); border: 1px solid var(--border-subtle);
            border-radius: 6px; padding: 7px 9px; font-family: var(--font-mono); font-size: 0.6rem;
            color: var(--text-secondary); display: flex; flex-direction: column; gap: 3px;
        }

        .top24-panel-col { display: flex; flex-direction: column; gap: 6px; min-height: 252px; }
        .top24-list-wrap { display: flex; flex-direction: column; gap: 5px; max-height: 222px; overflow-y: auto; padding-right: 2px; }
        .top24-toolbar { display: flex; justify-content: space-between; align-items: center; gap: 6px; flex-wrap: wrap; }
        .top24-row {
            display: grid; grid-template-columns: 92px 68px 1fr 72px; gap: 8px; align-items: center;
            padding: 6px 9px; border-radius: 6px; background: var(--bg-input);
            border: 1px solid var(--border-subtle); font-family: var(--font-mono); font-size: 0.64rem;
            cursor: pointer; transition: all 0.14s ease;
        }
        .top24-row:hover { border-color: var(--accent-cyan); background: var(--bg-elevated); }
        .top24-row.selected-pair { border-color: var(--accent-cyan); background: rgba(34, 211, 238, 0.08); }
        .vol-meter-track { width: 100%; height: 4px; background: rgba(148, 163, 184, 0.15); border-radius: 3px; overflow: hidden; margin-top: 3px; }
        .vol-meter-fill { height: 100%; background: linear-gradient(90deg, #10b981, var(--accent-cyan), var(--accent-violet)); border-radius: 3px; }
        .gainer-subname { font-size: 0.56rem; color: var(--text-muted); display: block; margin-top: 1px; }

        .section-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 6px; }
        .section-title { font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-secondary); font-family: var(--font-mono); }

        .settings-panel { padding: 14px 16px; display: flex; flex-direction: column; gap: 12px; }
        .ai-readiness-box {
            background: var(--bg-input); border: 1px solid var(--border-subtle);
            border-radius: 7px; padding: 10px 12px; display: flex; flex-direction: column; gap: 7px;
        }
        .ai-readiness-top { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; font-family: var(--font-mono); font-size: 0.68rem; }
        .progress-track { width: 100%; height: 6px; background: #0f172a; border-radius: 999px; overflow: hidden; }
        .progress-fill { height: 100%; width: 0%; background: linear-gradient(90deg, var(--accent-amber), var(--accent-cyan), var(--accent-emerald)); transition: width 0.35s ease; border-radius: 999px; }

        .quick-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
        .settings-group { background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: 7px; padding: 11px 12px; display: flex; flex-direction: column; gap: 8px; }
        .settings-group-title { font-family: var(--font-mono); font-size: 0.66rem; font-weight: 700; color: var(--accent-cyan); text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid var(--border-subtle); padding-bottom: 6px; }
        .setting-row { display: flex; justify-content: space-between; align-items: center; gap: 8px; font-size: 0.72rem; color: var(--text-secondary); }
        .setting-row label { flex: 1; cursor: pointer; }
        .setting-input, .setting-select {
            background: #070d1b; border: 1px solid var(--border-strong); color: var(--text-primary);
            font-family: var(--font-mono); border-radius: 5px; padding: 4px 7px; font-size: 0.68rem; outline: none;
        }
        .setting-input:focus, .setting-select:focus { border-color: var(--accent-cyan); }
        .setting-input { width: 74px; text-align: right; }
        .setting-select { max-width: 155px; }
        .setting-checkbox { width: 15px; height: 15px; accent-color: var(--accent-cyan); cursor: pointer; }

        details.adv-box { background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: 7px; padding: 9px 12px; }
        details.adv-box > summary { cursor: pointer; font-family: var(--font-mono); font-size: 0.68rem; font-weight: 700; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.05em; list-style: none; }
        details.adv-box[open] > summary { color: var(--accent-cyan); margin-bottom: 10px; }
        .adv-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }

        .settings-footer { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; border-top: 1px solid var(--border-subtle); padding-top: 11px; }
        .save-status { font-family: var(--font-mono); font-size: 0.68rem; color: var(--accent-emerald); font-weight: 600; }

        .table-card { overflow: hidden; }
        .table-scroll { max-height: 240px; overflow-y: auto; }
        table { width: 100%; border-collapse: collapse; text-align: left; font-family: var(--font-mono); font-size: 0.7rem; }
        th {
            color: var(--text-muted); font-weight: 600; padding: 9px 11px;
            border-bottom: 1px solid var(--border-subtle); font-size: 0.62rem;
            text-transform: uppercase; letter-spacing: 0.05em;
            background: rgba(10, 17, 34, 0.96); position: sticky; top: 0; z-index: 2;
        }
        td { padding: 9px 11px; border-bottom: 1px solid rgba(56, 189, 248, 0.08); color: var(--text-primary); }
        tr:hover td { background: rgba(34, 211, 238, 0.03); }
        tr:last-child td { border-bottom: none; }

        .filter-bar { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
        .filter-input {
            background: var(--bg-input); border: 1px solid var(--border-strong); color: var(--text-primary);
            font-family: var(--font-mono); border-radius: 5px; padding: 4px 9px; font-size: 0.68rem; outline: none; width: 130px;
        }
        .filter-input:focus { border-color: var(--accent-cyan); }
        .logs-box {
            padding: 11px 14px; height: 210px; max-height: 210px; overflow-y: auto;
            font-family: var(--font-mono); font-size: 0.69rem; color: var(--text-secondary);
            display: flex; flex-direction: column; gap: 5px;
        }
        .log-line { white-space: pre-wrap; word-break: break-all; line-height: 1.45; border-bottom: 1px dashed rgba(56, 189, 248, 0.08); padding-bottom: 4px; }
        .log-line:last-child { border-bottom: none; }

        .text-green { color: var(--accent-emerald); font-weight: 700; }
        .text-red { color: var(--accent-crimson); font-weight: 700; }
        .text-yellow { color: var(--accent-amber); font-weight: 700; }
        .text-cyan { color: var(--accent-cyan); font-weight: 700; }

        .modal-backdrop {
            position: fixed; inset: 0; background: rgba(2, 5, 13, 0.78);
            backdrop-filter: blur(5px); display: none; align-items: center; justify-content: center; z-index: 999; padding: 16px;
        }
        .modal-backdrop.open { display: flex; }
        .modal-card {
            background: #080f20; border: 1px solid var(--border-strong); border-radius: 9px;
            max-width: 420px; width: 100%; padding: 18px; display: flex; flex-direction: column; gap: 14px;
            box-shadow: 0 22px 55px rgba(0, 0, 0, 0.85);
        }
        .modal-title { font-family: var(--font-mono); font-size: 0.82rem; font-weight: 700; color: var(--accent-cyan); }
        .modal-msg { font-size: 0.78rem; color: var(--text-secondary); line-height: 1.5; }
        .modal-actions { display: flex; justify-content: flex-end; gap: 8px; }

        .toast-container { position: fixed; right: 18px; bottom: 18px; display: flex; flex-direction: column; gap: 8px; z-index: 1000; }
        .toast {
            background: #091226; border: 1px solid var(--border-strong); color: var(--text-primary);
            font-family: var(--font-mono); font-size: 0.68rem; padding: 9px 13px; border-radius: 6px;
            box-shadow: 0 10px 25px rgba(0, 0, 0, 0.65); max-width: 340px;
        }
        .toast.toast-ok { border-color: rgba(16, 185, 129, 0.6); color: var(--accent-emerald); }
        .toast.toast-err { border-color: rgba(244, 63, 94, 0.6); color: var(--accent-crimson); }

        @media (max-width: 1180px) {
            .quick-grid { grid-template-columns: repeat(2, 1fr); }
            .visual-desk-grid { grid-template-columns: 1fr; }
        }
        @media (max-width: 1000px) { .stats-grid { grid-template-columns: repeat(2, 1fr); } }
        @media (max-width: 720px) {
            .quick-grid, .adv-grid, .stats-grid, .vol-scanner-layout { grid-template-columns: 1fr; }
            .top24-row { grid-template-columns: 82px 62px 1fr 64px; }
            body { padding: 10px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header class="quantum-panel desk-header">
            <div class="header-top">
                <div class="brand">
                    <div class="brand-title-row">
                        <span class="quantum-badge"><span class="quantum-badge-dot"></span>QUANTUM NEURAL GRID</span>
                        <h1>APEX MATRIX_SCALPER_v1_BOT + AI</h1>
                    </div>
                    <div class="telemetry-strip">
                        <span class="telemetry-item" id="header-pair">Demo exekucia (Live OKX data)</span>
                        <span class="telemetry-item">Zostatok: <span id="header-balance" class="telemetry-val">-- USDC</span></span>
                        <span class="telemetry-item">Aktivny TF: <span id="header-tf" class="telemetry-val">{{ CURRENT_TIMEFRAME }}</span></span>
                        <span class="telemetry-item">Fib: <span id="header-fib" class="telemetry-val">{{ SETTINGS.fib_choice }}</span></span>
                        <span class="telemetry-item">Fixny SL: <span id="header-sl" class="telemetry-val">{% if SETTINGS.use_fixed_sl %}-{{ SETTINGS.fixed_sl_pct }}%{% else %}OFF{% endif %}</span></span>
                        <span class="telemetry-item">AI Model: <span id="header-ai" class="telemetry-val-cyan">Nacitavam...</span></span>
                        <span class="telemetry-item">AI Pamat (ulozene): <span id="header-ai-learned" class="telemetry-val">0 obchodov (0W / 0L)</span></span>
                    </div>
                </div>

                <div class="header-actions">
                    <div class="custom-buy-box">
                        <input type="text" id="custom-coin" class="custom-buy-input" placeholder="NES">
                        <button class="btn btn-on btn-mini" onclick="manualBuyCustom()">BUY</button>
                    </div>
                    <button id="compounding-btn" class="btn {% if SETTINGS.compounding_enabled %}btn-active-pill{% endif %}" onclick="toggleCompounding()">
                        COMPOUND: {% if SETTINGS.compounding_enabled %}ON{% else %}OFF{% endif %}
                    </button>
                    <button id="btc-btn" class="btn {% if SETTINGS.use_btc_filter %}btn-on{% else %}btn-off{% endif %}" onclick="toggleBtcFilter()">
                        BTC: {% if SETTINGS.use_btc_filter %}ON{% else %}OFF{% endif %}
                    </button>
                    <button id="bot-btn" class="btn {% if IS_BOT_ACTIVE %}btn-on{% else %}btn-off{% endif %}" onclick="toggleBot()">
                        {% if IS_BOT_ACTIVE %}ACTIVE{% else %}PAUSED{% endif %}
                    </button>
                    <button id="sell-btn" class="btn btn-sell" onclick="sellNow()" disabled>SELL ALL</button>
                </div>
            </div>

            <div class="control-ribbon">
                <div class="ribbon-group">
                    <span class="ribbon-label">Timeframe</span>
                    <button class="tf-btn btn btn-mini {% if CURRENT_TIMEFRAME|upper == '5M' %}btn-active-pill{% endif %}" data-tf="5m" onclick="setTf('5m')">5M</button>
                    <button class="tf-btn btn btn-mini {% if CURRENT_TIMEFRAME|upper == '8M' %}btn-active-pill{% endif %}" data-tf="8m" onclick="setTf('8m')">8M</button>
                    <button class="tf-btn btn btn-mini {% if CURRENT_TIMEFRAME|upper == '10M' %}btn-active-pill{% endif %}" data-tf="10m" onclick="setTf('10m')">10M</button>
                    <button class="tf-btn btn btn-mini {% if CURRENT_TIMEFRAME|upper == '15M' %}btn-active-pill{% endif %}" data-tf="15m" onclick="setTf('15m')">15M</button>
                    <button class="tf-btn btn btn-mini {% if CURRENT_TIMEFRAME|upper == '20M' %}btn-active-pill{% endif %}" data-tf="20m" onclick="setTf('20m')">20M</button>
                    <button class="tf-btn btn btn-mini {% if CURRENT_TIMEFRAME|upper == '30M' %}btn-active-pill{% endif %}" data-tf="30m" onclick="setTf('30m')">30M</button>
                    <button class="tf-btn btn btn-mini {% if CURRENT_TIMEFRAME|upper == '45M' %}btn-active-pill{% endif %}" data-tf="45m" onclick="setTf('45m')">45M</button>
                    <button class="tf-btn btn btn-mini {% if CURRENT_TIMEFRAME|upper == '2H' %}btn-active-pill{% endif %}" data-tf="2h" onclick="setTf('2h')">2H</button>
                    <button class="tf-btn btn btn-mini {% if CURRENT_TIMEFRAME|upper == '4H' %}btn-active-pill{% endif %}" data-tf="4h" onclick="setTf('4h')">4H</button>
                </div>

                <div class="ribbon-group">
                    <span class="ribbon-label">Paralelne</span>
                    <button class="parallel-btn btn btn-mini {% if SETTINGS.max_parallel == 1 %}btn-active-pill{% endif %}" data-val="1" onclick="setParallel(1)">1x</button>
                    <button class="parallel-btn btn btn-mini {% if SETTINGS.max_parallel == 2 %}btn-active-pill{% endif %}" data-val="2" onclick="setParallel(2)">2x</button>
                    <button class="parallel-btn btn btn-mini {% if SETTINGS.max_parallel == 3 %}btn-active-pill{% endif %}" data-val="3" onclick="setParallel(3)">3x</button>
                    <button class="parallel-btn btn btn-mini {% if SETTINGS.max_parallel == 4 %}btn-active-pill{% endif %}" data-val="4" onclick="setParallel(4)">4x</button>
                </div>

                <div class="ribbon-group">
                    <span class="ribbon-label">Rozpocet</span>
                    <button class="budget-btn btn btn-mini {% if SETTINGS.max_budget_cap|int == 500 %}btn-active-pill{% endif %}" data-val="500" onclick="setBudget(500)">500</button>
                    <button class="budget-btn btn btn-mini {% if SETTINGS.max_budget_cap|int == 1000 %}btn-active-pill{% endif %}" data-val="1000" onclick="setBudget(1000)">1K</button>
                    <button class="budget-btn btn btn-mini {% if SETTINGS.max_budget_cap|int == 2000 %}btn-active-pill{% endif %}" data-val="2000" onclick="setBudget(2000)">2K</button>
                    <button class="budget-btn btn btn-mini {% if SETTINGS.max_budget_cap|int == 3000 %}btn-active-pill{% endif %}" data-val="3000" onclick="setBudget(3000)">3K</button>
                    <button class="budget-btn btn btn-mini {% if SETTINGS.max_budget_cap|int == 5000 %}btn-active-pill{% endif %}" data-val="5000" onclick="setBudget(5000)">5K</button>
                    <button class="budget-btn btn btn-mini {% if SETTINGS.max_budget_cap|int == 10000 %}btn-active-pill{% endif %}" data-val="10000" onclick="setBudget(10000)">10K</button>
                </div>
            </div>
        </header>

        <div class="quantum-panel status-bar">
            <div class="status-left">
                <div class="pulse-dot"></div>
                <span id="current-stage">{{ CURRENT_STAGE }}</span>
            </div>
            <div class="status-right">
                <span id="last-update-tag" class="update-tag">Aktualizacia: --:--:--</span>
                <span id="market-regime-badge" class="regime-badge regime-bull">BYCI TRH</span>
            </div>
        </div>

        <section class="stats-grid">
            <div class="quantum-panel stat-card">
                <div class="stat-label">Win Rate</div>
                <div class="stat-value" id="stat-winrate">0.0%</div>
                <div class="stat-sub">Uspesnost uzavretych obchodov</div>
            </div>
            <div class="quantum-panel stat-card">
                <div class="stat-label">Total Trades</div>
                <div class="stat-value" id="stat-trades">0</div>
                <div class="stat-sub">Dokoncene pozicie v historii</div>
            </div>
            <div class="quantum-panel stat-card">
                <div class="stat-label">Win / Loss</div>
                <div class="stat-value" id="stat-winloss">0W / 0L</div>
                <div class="stat-sub">Ziskove vs. stratove obchody</div>
            </div>
            <div class="quantum-panel stat-card">
                <div class="stat-label">Net PnL (po poplatkoch)</div>
                <div class="stat-value" id="stat-pnl">+0.00 $</div>
                <div class="stat-sub" id="stat-pnl-sub">Hruby: +0.00 $ | Poplatky: -0.00 $</div>
            </div>
        </section>

        <!-- Quantum Visual Grid: Candlestick Chart + OKX 24h Spot Top Gainers (USDC) Scanner -->
        <section class="visual-desk-grid">
            <div class="quantum-panel visual-card">
                <div class="visual-header">
                    <div class="visual-title">
                        <span>Heikin-Ashi + EMA9 + FIB Matrix</span>
                        <span class="live-pill">LIVE STREAM</span>
                        <span class="violet-pill" id="chart-tf-pill">{{ CURRENT_TIMEFRAME }}</span>
                    </div>
                    <div class="ribbon-group" id="chart-coin-buttons">
                        <button class="btn btn-mini btn-active-pill chart-coin-btn" data-coin="NES" onclick="selectChartCoin('NES')">NES</button>
                        <button class="btn btn-mini chart-coin-btn" data-coin="ACE" onclick="selectChartCoin('ACE')">ACE</button>
                        <button class="btn btn-mini chart-coin-btn" data-coin="SKY" onclick="selectChartCoin('SKY')">SKY</button>
                        <button class="btn btn-mini chart-coin-btn" data-coin="SPACE" onclick="selectChartCoin('SPACE')">SPACE</button>
                    </div>
                </div>
                <div class="chart-canvas-wrap">
                    <canvas id="haFibCanvas" class="desk-canvas"></canvas>
                    <div class="chart-crosshair-readout" id="chart-hover-info">Kurzor: pohybom nad grafom zobrazis cenu</div>
                </div>
                <div class="chart-legend">
                    <div class="legend-items">
                        <span><i class="legend-dot" style="background:#10b981;"></i>HA Bull</span>
                        <span><i class="legend-dot" style="background:#f43f5e;"></i>HA Bear</span>
                        <span><i class="legend-dot" style="background:#22d3ee;"></i>EMA9</span>
                        <span><i class="legend-dot" style="background:#f59e0b;"></i>Fib <span id="chart-fib-label">{{ SETTINGS.fib_choice }}</span></span>
                    </div>
                    <span id="chart-live-readout" class="text-cyan">NES/USDC · --</span>
                </div>
            </div>

            <div class="quantum-panel visual-card">
                <div class="visual-header">
                    <div class="visual-title">
                        <span>OKX Top 24h Volume Scanner · Spot Top Gainers (USDC)</span>
                        <span class="live-pill" id="top24-source-pill">OKX 24H LIVE</span>
                    </div>
                    <span id="top24-updated-tag" class="update-tag">24h USDC Gainers</span>
                </div>

                <div class="vol-scanner-layout">
                    <div class="radar-column">
                        <div class="radar-stage" id="radar-stage">
                            <div class="radar-ring r1"></div>
                            <div class="radar-ring r2"></div>
                            <div class="radar-ring r3"></div>
                            <div class="radar-cross-h"></div>
                            <div class="radar-cross-v"></div>
                            <div class="radar-sweep"></div>
                            <div class="radar-blip blip-buy" style="top: 24%; left: 68%;" title="Top Gainer #1"></div>
                            <div class="radar-blip blip-buy" style="top: 58%; left: 32%;" title="Top Gainer #2"></div>
                            <div class="radar-blip blip-scan" style="top: 38%; left: 26%;" title="USDC Spot Scan"></div>
                            <div class="radar-blip blip-warn" style="top: 72%; left: 70%;" title="24h Range Pulse"></div>
                        </div>
                        <div class="radar-meta-box">
                            <span>Aktivny par: <b class="text-cyan" id="radar-active-pair">NES/USDC</b></span>
                            <span>24h Rast: <b class="text-green" id="radar-active-chg">+19.24%</b></span>
                            <span>24h Objem: <b class="text-yellow" id="radar-active-vol">$84.73M</b></span>
                        </div>
                    </div>

                    <div class="top24-panel-col">
                        <div class="top24-toolbar">
                            <span class="update-tag" id="top24-count-lbl">Spot Top Gainers · USDC par (zoradene podla 24h %)</span>
                            <button class="btn btn-mini" onclick="refreshOkxTop24Volume(true)">OBNOVIT 24H</button>
                        </div>
                        <div class="top24-list-wrap" id="top24-volume-list">
                            <div class="top24-row">
                                <b>NES/USDC</b>
                                <span class="text-green">+19.24%</span>
                                <span>Nacitavam OKX 24h USDC gainers...</span>
                                <span class="text-green">TOP #1</span>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </section>

        <section>
            <div class="section-header">
                <span class="section-title">Nastavenia bota (vsetky pravidla si riadite tu v APEX rozhrani)</span>
                <span id="pine-active-badge" class="text-yellow mono" style="font-size: 0.68rem;">REZIM: {{ SETTINGS.mode_choice }} · FIB {{ SETTINGS.fib_choice }}</span>
            </div>
            <div class="quantum-panel settings-panel">
                <div class="ai-readiness-box">
                    <div class="ai-readiness-top">
                        <span id="ai-learned-summary" class="text-yellow">AI naucenie: 0.0% / ciel 80% (chyba ~160 obchodov) | Ulozene obchody: 0 (0W / 0L) | Pravdepodobnost: --</span>
                        <button class="btn btn-mini btn-on" onclick="manualTrainAi()">DOTRENOVAT AI TERAZ</button>
                    </div>
                    <div class="progress-track">
                        <div id="ai-readiness-bar" class="progress-fill"></div>
                    </div>
                </div>

                <div class="quick-grid">
                    <div class="settings-group">
                        <div class="settings-group-title">1. Vstup, FIB a volitelne filtre</div>
                        <div class="setting-row">
                            <label for="pine-mode">Rezim strategie</label>
                            <select id="pine-mode" class="setting-select">
                                <option value="FIB" {% if SETTINGS.mode_choice == 'FIB' %}selected{% endif %}>Fibonacci Touch (FIB)</option>
                                <option value="ALL_GREEN" {% if SETTINGS.mode_choice == 'ALL_GREEN' %}selected{% endif %}>All Green</option>
                                <option value="BREAKOUT" {% if SETTINGS.mode_choice == 'BREAKOUT' %}selected{% endif %}>Breakout</option>
                            </select>
                        </div>
                        <div class="setting-row">
                            <label for="pine-fib-choice">Nakupna Fib hladina</label>
                            <select id="pine-fib-choice" class="setting-select">
                                <option value="0.50" {% if SETTINGS.fib_choice == '0.50' %}selected{% endif %}>0.50</option>
                                <option value="0.618" {% if SETTINGS.fib_choice == '0.618' %}selected{% endif %}>0.618</option>
                                <option value="0.786" {% if SETTINGS.fib_choice == '0.786' %}selected{% endif %}>0.786</option>
                            </select>
                        </div>
                        <div class="setting-row">
                            <label for="pine-green-ha">Vyzadovat zelenu HA sviecku</label>
                            <input type="checkbox" id="pine-green-ha" class="setting-checkbox" {% if SETTINGS.require_green_ha_buy %}checked{% endif %}>
                        </div>
                        <div class="setting-row">
                            <label for="pine-require-bull">Len BYCI trh (BULL) na TF</label>
                            <input type="checkbox" id="pine-require-bull" class="setting-checkbox" {% if SETTINGS.require_bull_tf_buy %}checked{% endif %}>
                        </div>
                        <div class="setting-row">
                            <label for="pine-sell-before-rebuy">Dalsi BUY az po signale SELL</label>
                            <input type="checkbox" id="pine-sell-before-rebuy" class="setting-checkbox" {% if SETTINGS.require_sell_before_rebuy %}checked{% endif %}>
                        </div>
                        <div class="setting-row">
                            <label for="pine-use-ai">Autonomny AI filter</label>
                            <input type="checkbox" id="pine-use-ai" class="setting-checkbox" {% if SETTINGS.use_ai_filter %}checked{% endif %}>
                        </div>
                    </div>

                    <div class="settings-group">
                        <div class="settings-group-title">2. Ciel (TP) a Stop-Loss (SL)</div>
                        <div class="setting-row">
                            <label for="pine-use-fixed-sl">Fixny SL zapnuty</label>
                            <input type="checkbox" id="pine-use-fixed-sl" class="setting-checkbox" {% if SETTINGS.use_fixed_sl %}checked{% endif %}>
                        </div>
                        <div class="setting-row">
                            <label for="pine-fixed-sl-pct">Fixny SL (%)</label>
                            <input type="number" id="pine-fixed-sl-pct" class="setting-input" min="0.1" max="50.0" step="0.1" value="{{ SETTINGS.fixed_sl_pct }}">
                        </div>
                        <div class="setting-row">
                            <label for="pine-use-fixed-tp">Fixny TP zapnuty</label>
                            <input type="checkbox" id="pine-use-fixed-tp" class="setting-checkbox" {% if SETTINGS.use_fixed_tp %}checked{% endif %}>
                        </div>
                        <div class="setting-row">
                            <label for="pine-fixed-tp-pct">Fixny TP (%)</label>
                            <input type="number" id="pine-fixed-tp-pct" class="setting-input" min="0.1" max="100.0" step="0.1" value="{{ SETTINGS.fixed_tp_pct }}">
                        </div>
                    </div>

                    <div class="settings-group">
                        <div class="settings-group-title">3. Rychly vystup & Simulacia poplatkov</div>
                        <div class="setting-row">
                            <label for="pine-use-ha-exit">Exit pri otoceni HA</label>
                            <input type="checkbox" id="pine-use-ha-exit" class="setting-checkbox" {% if SETTINGS.use_ha_exit %}checked{% endif %}>
                        </div>
                        <div class="setting-row">
                            <label for="pine-ha-consec">Cervene HA sviecky</label>
                            <select id="pine-ha-consec" class="setting-select">
                                <option value="1" {% if SETTINGS.ha_exit_consecutive == 1 %}selected{% endif %}>1</option>
                                <option value="2" {% if SETTINGS.ha_exit_consecutive == 2 %}selected{% endif %}>2</option>
                                <option value="3" {% if SETTINGS.ha_exit_consecutive == 3 %}selected{% endif %}>3</option>
                            </select>
                        </div>
                        <div class="setting-row">
                            <label for="pine-use-osc-sell">SELL pri obrate oscilatora</label>
                            <input type="checkbox" id="pine-use-osc-sell" class="setting-checkbox" {% if SETTINGS.sell_on_osc_reversal %}checked{% endif %}>
                        </div>
                        <div class="setting-row">
                            <label for="pine-sim-fees">Simulovat OKX poplatky (BUY+SELL)</label>
                            <input type="checkbox" id="pine-sim-fees" class="setting-checkbox" {% if SETTINGS.simulate_fees %}checked{% endif %}>
                        </div>
                        <div class="setting-row">
                            <label for="pine-taker-fee">Poplatok za 1 stranu (%)</label>
                            <input type="number" id="pine-taker-fee" class="setting-input" min="0.00" max="2.00" step="0.01" value="{{ SETTINGS.taker_fee_pct }}">
                        </div>
                    </div>

                    <div class="settings-group">
                        <div class="settings-group-title">4. Ochrana TS / BE / TTP</div>
                        <div class="setting-row">
                            <label for="pine-use-trailing">Trailing Stop zapnuty</label>
                            <input type="checkbox" id="pine-use-trailing" class="setting-checkbox" {% if SETTINGS.use_trailing %}checked{% endif %}>
                        </div>
                        <div class="setting-row">
                            <label for="pine-trail-pct">Vzdialenost TS (%)</label>
                            <input type="number" id="pine-trail-pct" class="setting-input" min="0.1" max="50.0" step="0.1" value="{{ SETTINGS.trail_pct }}">
                        </div>
                        <div class="setting-row">
                            <label for="pine-use-be">Break-Even zapnuty</label>
                            <input type="checkbox" id="pine-use-be" class="setting-checkbox" {% if SETTINGS.use_breakeven %}checked{% endif %}>
                        </div>
                        <div class="setting-row">
                            <label for="pine-be-trigger">Zisk % pre Break-Even</label>
                            <input type="number" id="pine-be-trigger" class="setting-input" min="0.05" max="50.0" step="0.05" value="{{ SETTINGS.be_trigger_pct }}">
                        </div>
                        <div class="setting-row">
                            <label for="pine-use-ttp">Trailing TP zapnuty</label>
                            <input type="checkbox" id="pine-use-ttp" class="setting-checkbox" {% if SETTINGS.use_trailing_tp %}checked{% endif %}>
                        </div>
                        <div class="setting-row">
                            <label for="pine-ttp-trigger">Aktivacia TTP (%)</label>
                            <input type="number" id="pine-ttp-trigger" class="setting-input" min="0.1" max="100.0" step="0.1" value="{{ SETTINGS.ttp_trigger_pct }}">
                        </div>
                        <div class="setting-row">
                            <label for="pine-ttp-callback">Odstup TTP (%)</label>
                            <input type="number" id="pine-ttp-callback" class="setting-input" min="0.05" max="50.0" step="0.05" value="{{ SETTINGS.ttp_callback_pct }}">
                        </div>
                    </div>
                </div>

                <details class="adv-box">
                    <summary>+ Rozsirene nastavenia (Oscilator, Pivot, Anti-Top)</summary>
                    <div class="adv-grid">
                        <div class="settings-group">
                            <div class="settings-group-title">Oscilator & Pivot</div>
                            <div class="setting-row">
                                <label for="pine-strict-buy">Prisnejsi filter BUY</label>
                                <input type="checkbox" id="pine-strict-buy" class="setting-checkbox" {% if SETTINGS.strict_buy_filter %}checked{% endif %}>
                            </div>
                            <div class="setting-row">
                                <label for="pine-fast-len">Rychla dlzka (Vol)</label>
                                <input type="number" id="pine-fast-len" class="setting-input" min="1" max="200" step="1" value="{{ SETTINGS.fast_len }}">
                            </div>
                            <div class="setting-row">
                                <label for="pine-slow-len">Pomala dlzka (Vol)</label>
                                <input type="number" id="pine-slow-len" class="setting-input" min="1" max="300" step="1" value="{{ SETTINGS.slow_len }}">
                            </div>
                            <div class="setting-row">
                                <label for="pine-signal-len">Signalna ciara (MA)</label>
                                <input type="number" id="pine-signal-len" class="setting-input" min="1" max="200" step="1" value="{{ SETTINGS.signal_len }}">
                            </div>
                            <div class="setting-row">
                                <label for="pine-pred-look">Citlivost predikcie</label>
                                <input type="number" id="pine-pred-look" class="setting-input" min="1" max="10" step="1" value="{{ SETTINGS.pred_look }}">
                            </div>
                            <div class="setting-row">
                                <label for="pine-pivot-len">Pivot dlzka (Fib)</label>
                                <input type="number" id="pine-pivot-len" class="setting-input" min="1" max="10" step="1" value="{{ SETTINGS.pivot_len }}">
                            </div>
                        </div>

                        <div class="settings-group">
                            <div class="settings-group-title">Anti-Top (volitelne)</div>
                            <div class="setting-row">
                                <label for="pine-use-anti-top">Anti-Top filter zapnuty</label>
                                <input type="checkbox" id="pine-use-anti-top" class="setting-checkbox" {% if SETTINGS.use_anti_chase %}checked{% endif %}>
                            </div>
                            <div class="setting-row">
                                <label for="pine-max-pump">Max rast sviecky (%)</label>
                                <input type="number" id="pine-max-pump" class="setting-input" min="0.1" max="5.0" step="0.1" value="{{ SETTINGS.max_candle_pump_pct }}">
                            </div>
                            <div class="setting-row">
                                <label for="pine-max-ema-dist">Max nad EMA9 (%)</label>
                                <input type="number" id="pine-max-ema-dist" class="setting-input" min="0.1" max="5.0" step="0.1" value="{{ SETTINGS.max_ema9_dist_pct }}">
                            </div>
                            <div class="setting-row">
                                <label for="pine-max-wick">Max horny knot (0.1-0.9)</label>
                                <input type="number" id="pine-max-wick" class="setting-input" min="0.1" max="0.9" step="0.05" value="{{ SETTINGS.max_upper_wick_ratio }}">
                            </div>
                        </div>
                    </div>
                </details>

                <div class="settings-footer">
                    <span id="pine-save-status" class="save-status">Trvala pamat aktivna — data sa pri restartoch nemazu.</span>
                    <div style="display: flex; gap: 6px; flex-wrap: wrap;">
                        <button class="btn btn-off" onclick="cleanResetLearning()">CISTY RESET AI</button>
                        <button class="btn" onclick="resetPineDefaults()">OBNOVIT ZAKLAD (PINE)</button>
                        <button class="btn btn-on" onclick="savePineSettings()">ULOZIT NASTAVENIA</button>
                    </div>
                </div>
            </div>
        </section>

        <section>
            <div class="section-header">
                <span class="section-title">Active Positions Matrix (TTP / Trailing Stop / Break-Even / Volitelny Fixny SL)</span>
            </div>
            <div class="quantum-panel table-card">
                <table>
                    <thead>
                        <tr>
                            <th>Pair / TF</th>
                            <th>Entry Price</th>
                            <th>Current Price</th>
                            <th>TP -> TTP Status</th>
                            <th>Stop-Loss / TS</th>
                            <th>Invested USDC</th>
                            <th>Live PnL</th>
                            <th>Emergency</th>
                        </tr>
                    </thead>
                    <tbody id="active-position-tbody">
                        <tr><td colspan="8" style="text-align: center; color: var(--text-muted);">Skenujem trh podla nastaveni v APEX...</td></tr>
                    </tbody>
                </table>
            </div>
        </section>

        <section>
            <div class="section-header">
                <span class="section-title">Closed Trades History (Posledne uzavrete obchody)</span>
                <span id="closed-trades-count" class="update-tag">0 obchodov</span>
            </div>
            <div class="quantum-panel table-card table-scroll" style="max-height: 205px;">
                <table>
                    <thead>
                        <tr>
                            <th>Cas</th>
                            <th>Par</th>
                            <th>Rezim</th>
                            <th>Cisty PnL (%)</th>
                            <th>Cisty PnL ($)</th>
                            <th>Hruby / Poplatky</th>
                            <th>Dovod predaja</th>
                        </tr>
                    </thead>
                    <tbody id="closed-trades-tbody">
                        <tr><td colspan="7" style="text-align: center; color: var(--text-muted);">Zatial ziadne uzavrete obchody.</td></tr>
                    </tbody>
                </table>
            </div>
        </section>

        <section>
            <div class="section-header">
                <span class="section-title">Live Market Scanner Status (Podporovane USDC pary · FIB: <span id="scanner-fib-level" class="text-yellow">{{ SETTINGS.fib_choice }}</span>)</span>
                <div class="filter-bar">
                    <button class="btn btn-mini btn-active-pill scanner-filter-btn" data-filter="ALL" onclick="setScannerFilter('ALL')">VSETKY</button>
                    <button class="btn btn-mini scanner-filter-btn" data-filter="BUY" onclick="setScannerFilter('BUY')">BUY</button>
                    <button class="btn btn-mini scanner-filter-btn" data-filter="BLOK" onclick="setScannerFilter('BLOK')">BLOK</button>
                    <button class="btn btn-mini scanner-filter-btn" data-filter="IN_TRADE" onclick="setScannerFilter('IN_TRADE')">V OBCHODE</button>
                    <input type="text" id="scanner-search" class="filter-input" placeholder="Hladat coin..." oninput="updateState()">
                </div>
            </div>
            <div class="quantum-panel table-card table-scroll" style="max-height: 265px;">
                <table>
                    <thead>
                        <tr>
                            <th>Token / Par</th>
                            <th>Cena</th>
                            <th>1. Objem. Momentum</th>
                            <th>2. Nakupny Impulz</th>
                            <th>3. Stav Obchodu</th>
                            <th>4. AI Skore</th>
                            <th>5. Ochrana / SL</th>
                            <th>6. Exekucny Pokyn</th>
                        </tr>
                    </thead>
                    <tbody id="scanner-tbody">
                        <tr><td colspan="8" style="text-align: center; color: var(--text-muted);">Nacitavam stav skenera...</td></tr>
                    </tbody>
                </table>
            </div>
        </section>

        <section>
            <div class="section-header">
                <span class="section-title">24h System Execution Logs (Auto-cistenie po 2h)</span>
                <div class="filter-bar">
                    <span id="log-prune-info" class="update-tag">Auto-cistenie: kazde 2h (posledne: --) · 24h register: 0</span>
                    <button class="btn btn-mini log-filter-btn btn-active-pill" data-logcat="ALL" onclick="setLogFilter('ALL')">ALL</button>
                    <button class="btn btn-mini log-filter-btn" data-logcat="TRADE" onclick="setLogFilter('TRADE')">TRADE</button>
                    <button class="btn btn-mini log-filter-btn" data-logcat="FILTER" onclick="setLogFilter('FILTER')">FILTER</button>
                    <button class="btn btn-mini log-filter-btn" data-logcat="ERROR" onclick="setLogFilter('ERROR')">ERROR</button>
                    <input type="text" id="log-search" class="filter-input" placeholder="Filter coin/text..." oninput="updateState()">
                    <button class="btn btn-mini" onclick="exportLogs24h()">EXPORT 24H</button>
                    <button class="btn btn-mini btn-off" onclick="clearLogsNow()">VYCISTIT 2H</button>
                </div>
            </div>
            <div class="quantum-panel logs-box" id="logs-container">
                <div class="log-line">[System Initialized] Cisty start — ziadne stare zaznamy.</div>
            </div>
        </section>
    </div>

    <div id="confirm-modal" class="modal-backdrop" role="dialog" aria-modal="true">
        <div class="modal-card">
            <div class="modal-title" id="modal-title">Potvrdenie akcie</div>
            <div class="modal-msg" id="modal-msg">Naozaj chcete pokracovat?</div>
            <div class="modal-actions">
                <button class="btn" id="modal-cancel-btn">Zrusit</button>
                <button class="btn btn-on" id="modal-confirm-btn">Potvrdit</button>
            </div>
        </div>
    </div>
    <div id="toast-container" class="toast-container"></div>

    <script>
        let currentScannerFilter = "ALL";
        let currentLogCategory = "ALL";
        let latestLogs24h = [];
        let selectedChartCoin = "NES";
        let currentFibLevel = parseFloat("{{ SETTINGS.fib_choice }}") || 0.786;

        // OKX 24h Spot Top Gainers na páre USDC (zoradené od najvyššieho 24h rastu %)
        const OKX_SPOT_USDC_GAINERS_SNAPSHOT = [
            { coin: "NES", instId: "NES/USDC", okxInstId: "NES-USDC", name: "Nesa", last: 0.2839, chgPct: 19.24, range24h: "0.2256 - 0.2925", vol24hM: 84.73, volBase: "89.90K NES", signal: "BUY" },
            { coin: "ACE", instId: "ACE/USDC", okxInstId: "ACE-USDC", name: "ACE", last: 0.05298, chgPct: 13.54, range24h: "0.04442 - 0.05655", vol24hM: 6.25, volBase: "454.07K ACE", signal: "BUY" },
            { coin: "SKY", instId: "SKY/USDC", okxInstId: "SKY-USDC", name: "Sky", last: 0.05161, chgPct: 11.86, range24h: "0.04499 - 0.05458", vol24hM: 1210.0, volBase: "989.43K SKY", signal: "BUY" },
            { coin: "SPACE", instId: "SPACE/USDC", okxInstId: "SPACE-USDC", name: "Spacecoin", last: 0.004543, chgPct: 11.79, range24h: "0.004064 - 0.004677", vol24hM: 29.55, volBase: "2.54M SPACE", signal: "BUY" },
            { coin: "NIGHT", instId: "NIGHT/USDC", okxInstId: "NIGHT-USDC", name: "Midnight", last: 0.053966, chgPct: 10.16, range24h: "0.047892 - 0.055380", vol24hM: 902.45, volBase: "4.74M NIGHT", signal: "BUY" },
            { coin: "SENT", instId: "SENT/USDC", okxInstId: "SENT-USDC", name: "Sentient", last: 0.02486, chgPct: 9.13, range24h: "0.02203 - 0.02511", vol24hM: 178.70, volBase: "1.69M SENT", signal: "SCAN" },
            { coin: "xANT", instId: "xANT/USDC", okxInstId: "XANT-USDC", name: "Anthropic", last: 91.36, chgPct: 6.78, range24h: "84.33 - 93.13", vol24hM: 48.39, volBase: "5.41K xANT", signal: "SCAN" },
            { coin: "YB", instId: "YB/USDC", okxInstId: "YB-USDC", name: "Yield Basis", last: 0.06104, chgPct: 6.36, range24h: "0.05739 - 0.06348", vol24hM: 18.48, volBase: "22.24K YB", signal: "SCAN" },
            { coin: "xSNDK", instId: "xSNDK/USDC", okxInstId: "XSNDK-USDC", name: "SanDisk", last: 85.52, chgPct: 5.26, range24h: "77.33 - 86.26", vol24hM: 20.98, volBase: "444.92 xSNDK", signal: "SCAN" },
            { coin: "CFG", instId: "CFG/USDC", okxInstId: "CFG-USDC", name: "Centrifuge", last: 0.05963, chgPct: 3.90, range24h: "0.05528 - 0.07290", vol24hM: 9.60, volBase: "83.40K CFG", signal: "SCAN" },
            { coin: "WIN", instId: "WIN/USDC", okxInstId: "WIN-USDC", name: "WINkLink", last: 0.00005520, chgPct: 3.86, range24h: "0.00005264 - 0.00005571", vol24hM: 51.21, volBase: "4.36M WIN", signal: "SCAN" },
            { coin: "AUCTION", instId: "AUCTION/USDC", okxInstId: "AUCTION-USDC", name: "AUCTION", last: 2.614, chgPct: 3.16, range24h: "2.534 - 2.736", vol24hM: 19.05, volBase: "457.05 AUCTION", signal: "SCAN" },
            { coin: "BABY", instId: "BABY/USDC", okxInstId: "BABY-USDC", name: "Babylon", last: 0.009644, chgPct: 3.02, range24h: "0.009361 - 0.009781", vol24hM: 49.94, volBase: "108.29K BABY", signal: "SCAN" },
            { coin: "LQTY", instId: "LQTY/USDC", okxInstId: "LQTY-USDC", name: "Liquity", last: 0.1324, chgPct: 2.87, range24h: "0.1287 - 0.1324", vol24hM: 13.15, volBase: "1.43K LQTY", signal: "SCAN" },
            { coin: "xTSLA", instId: "xTSLA/USDC", okxInstId: "XTSLA-USDC", name: "Tesla", last: 399.14, chgPct: 2.38, range24h: "383.57 - 401.46", vol24hM: 50.89, volBase: "128.36 xTSLA", signal: "SCAN" }
        ].sort((a, b) => b.chgPct - a.chgPct);

        const COIN_BASE_PRICES = Object.fromEntries(
            OKX_SPOT_USDC_GAINERS_SNAPSHOT.map(item => [item.coin, item.last])
        );

        let top24Pairs = [...OKX_SPOT_USDC_GAINERS_SNAPSHOT];
        let hoverChartX = null;
        let hoverChartY = null;

        function formatPairPrice(price) {
            const p = Number(price || 0);
            if (!isFinite(p) || p <= 0) return "--";
            if (p < 0.001) return p.toFixed(8);
            if (p < 0.05) return p.toFixed(6);
            if (p < 2) return p.toFixed(4);
            return p.toFixed(2);
        }

        function generateHaSeries(coin) {
            const matched = top24Pairs.find(p => p.coin === coin) || OKX_SPOT_USDC_GAINERS_SNAPSHOT.find(p => p.coin === coin);
            const base = COIN_BASE_PRICES[coin] || (matched ? matched.last : 0.2839);
            const chgFactor = matched ? (matched.chgPct / 100.0) : 0.05;
            const volScale = base * 0.012;
            const list = [];
            const startRaw = Math.max(base * 0.65, base / (1 + Math.max(0.02, chgFactor)));
            let prevOpen = startRaw;
            let prevClose = startRaw;
            let rawClose = startRaw;

            for (let i = 0; i < 46; i++) {
                const progress = i / 45;
                const wave = Math.sin(i * 0.34) * volScale * 1.35 + Math.cos(i * 0.17) * volScale * 0.85;
                const trendTarget = startRaw + (base - startRaw) * progress;
                const o = rawClose;
                const c = Math.max(base * 0.60, trendTarget + wave);
                const h = Math.max(o, c) + Math.abs(Math.sin(i * 0.7)) * volScale * 0.65;
                const l = Math.min(o, c) - Math.abs(Math.cos(i * 0.5)) * volScale * 0.65;
                rawClose = c;

                const haClose = (o + h + l + c) / 4;
                const haOpen = i === 0 ? (o + c) / 2 : (prevOpen + prevClose) / 2;
                const haHigh = Math.max(h, haOpen, haClose);
                const haLow = Math.min(l, haOpen, haClose);
                prevOpen = haOpen;
                prevClose = haClose;
                list.push({ o: haOpen, h: haHigh, l: haLow, c: haClose });
            }
            return list;
        }

        let haSeries = generateHaSeries("NES");
        let liveTickPhase = 0;

        function selectChartCoin(coin) {
            const clean = String(coin || "NES").replace("/USDC", "").replace("-USDC", "").trim();
            selectedChartCoin = clean;
            document.querySelectorAll('.chart-coin-btn').forEach(b => {
                b.classList.toggle('btn-active-pill', b.getAttribute('data-coin') === clean);
            });
            document.querySelectorAll('.top24-row').forEach(r => {
                r.classList.toggle('selected-pair', r.getAttribute('data-coin') === clean);
            });
            const matched = top24Pairs.find(p => p.coin === clean);
            const radarPairEl = document.getElementById('radar-active-pair');
            const radarChgEl = document.getElementById('radar-active-chg');
            const radarVolEl = document.getElementById('radar-active-vol');
            if (radarPairEl) radarPairEl.innerText = matched ? matched.instId : `${clean}/USDC`;
            if (radarChgEl && matched) {
                radarChgEl.innerText = `${matched.chgPct >= 0 ? '+' : ''}${matched.chgPct.toFixed(2)}%`;
                radarChgEl.className = matched.chgPct >= 0 ? "text-green" : "text-red";
            }
            if (radarVolEl && matched) radarVolEl.innerText = `$${matched.vol24hM.toFixed(2)}M`;
            const customBuyInput = document.getElementById('custom-coin');
            if (customBuyInput && !customBuyInput.value) {
                customBuyInput.placeholder = clean;
            }
            haSeries = generateHaSeries(clean);
        }

        function attachChartHover() {
            const canvas = document.getElementById('haFibCanvas');
            if (!canvas) return;
            canvas.addEventListener('mousemove', (e) => {
                const rect = canvas.getBoundingClientRect();
                hoverChartX = e.clientX - rect.left;
                hoverChartY = e.clientY - rect.top;
            });
            canvas.addEventListener('mouseleave', () => {
                hoverChartX = null;
                hoverChartY = null;
                const hInfo = document.getElementById('chart-hover-info');
                if (hInfo) hInfo.innerText = "Kurzor: pohybom nad grafom zobrazis cenu";
            });
        }

        function renderHaFibChart() {
            const canvas = document.getElementById('haFibCanvas');
            if (!canvas) return;
            const dpr = window.devicePixelRatio || 1;
            const rect = canvas.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) return;
            if (canvas.width !== Math.floor(rect.width * dpr) || canvas.height !== Math.floor(rect.height * dpr)) {
                canvas.width = Math.floor(rect.width * dpr);
                canvas.height = Math.floor(rect.height * dpr);
            }
            const ctx = canvas.getContext('2d');
            ctx.save();
            ctx.scale(dpr, dpr);

            const w = rect.width;
            const h = rect.height;
            ctx.clearRect(0, 0, w, h);

            ctx.strokeStyle = 'rgba(34, 211, 238, 0.05)';
            ctx.lineWidth = 1;
            for (let gx = 42; gx < w - 58; gx += 46) {
                ctx.beginPath(); ctx.moveTo(gx, 12); ctx.lineTo(gx, h - 22); ctx.stroke();
            }
            for (let gy = 22; gy < h - 22; gy += 40) {
                ctx.beginPath(); ctx.moveTo(42, gy); ctx.lineTo(w - 58, gy); ctx.stroke();
            }

            liveTickPhase += 0.14;
            const matched = top24Pairs.find(p => p.coin === selectedChartCoin) || OKX_SPOT_USDC_GAINERS_SNAPSHOT.find(p => p.coin === selectedChartCoin);
            const base = COIN_BASE_PRICES[selectedChartCoin] || (matched ? matched.last : 0.2839);
            const jitter = Math.sin(liveTickPhase) * base * 0.0022;
            const lastBar = haSeries[haSeries.length - 1];
            if (lastBar) {
                const prevClose = lastBar.c;
                const nextClose = Math.max(base * 0.65, base + jitter);
                lastBar.c = nextClose;
                lastBar.h = Math.max(lastBar.h, nextClose, prevClose);
                lastBar.l = Math.min(lastBar.l, nextClose, prevClose);
            }

            const prices = [];
            haSeries.forEach(c => { prices.push(c.h, c.l); });
            const maxP = Math.max(...prices);
            const minP = Math.min(...prices);
            const pad = Math.max(1e-9, (maxP - minP) * 0.14);
            const topP = maxP + pad;
            const botP = Math.max(1e-9, minP - pad);
            const plotTop = 24;
            const plotBot = h - 24;
            const plotLeft = 44;
            const plotRight = w - 62;
            const plotW = Math.max(40, plotRight - plotLeft);
            const plotH = Math.max(30, plotBot - plotTop);

            const yOf = (val) => plotTop + ((topP - val) / (topP - botP)) * plotH;

            const fibLevel = currentFibLevel || 0.786;
            const fibPrice = topP - (topP - botP) * fibLevel;
            const fibY = yOf(fibPrice);
            ctx.save();
            ctx.setLineDash([5, 4]);
            ctx.strokeStyle = 'rgba(245, 158, 11, 0.72)';
            ctx.lineWidth = 1.2;
            ctx.beginPath();
            ctx.moveTo(plotLeft, fibY);
            ctx.lineTo(plotRight, fibY);
            ctx.stroke();
            ctx.restore();
            ctx.fillStyle = '#f59e0b';
            ctx.font = '600 10px "JetBrains Mono", monospace';
            ctx.fillText(`FIB ${fibLevel}`, plotRight + 5, fibY + 3);

            const ema9Series = [];
            const k = 2 / (9 + 1);
            haSeries.forEach((c, idx) => {
                if (idx === 0) ema9Series.push(c.c);
                else ema9Series.push(c.c * k + ema9Series[idx - 1] * (1 - k));
            });

            const stepX = plotW / haSeries.length;
            const candleW = Math.max(3, Math.min(11, stepX * 0.62));

            haSeries.forEach((c, idx) => {
                const cx = plotLeft + idx * stepX + stepX * 0.5;
                const isBull = c.c >= c.o;
                const color = isBull ? '#10b981' : '#f43f5e';
                ctx.strokeStyle = color;
                ctx.lineWidth = 1.1;
                ctx.beginPath();
                ctx.moveTo(cx, yOf(c.h));
                ctx.lineTo(cx, yOf(c.l));
                ctx.stroke();

                const yOpen = yOf(c.o);
                const yClose = yOf(c.c);
                const bodyTop = Math.min(yOpen, yClose);
                const bodyH = Math.max(2, Math.abs(yClose - yOpen));
                ctx.fillStyle = isBull ? 'rgba(16, 185, 129, 0.85)' : 'rgba(244, 63, 94, 0.85)';
                ctx.fillRect(cx - candleW / 2, bodyTop, candleW, bodyH);
            });

            ctx.strokeStyle = '#22d3ee';
            ctx.lineWidth = 1.75;
            ctx.beginPath();
            ema9Series.forEach((ev, idx) => {
                const cx = plotLeft + idx * stepX + stepX * 0.5;
                const ey = yOf(ev);
                if (idx === 0) ctx.moveTo(cx, ey);
                else ctx.lineTo(cx, ey);
            });
            ctx.stroke();

            const lastVal = lastBar ? lastBar.c : base;
            const lastY = yOf(lastVal);
            ctx.fillStyle = '#071124';
            ctx.strokeStyle = 'rgba(34, 211, 238, 0.45)';
            ctx.fillRect(plotRight + 3, lastY - 8, 56, 16);
            ctx.strokeRect(plotRight + 3, lastY - 8, 56, 16);
            ctx.fillStyle = '#22d3ee';
            ctx.font = '600 9.5px "JetBrains Mono", monospace';
            ctx.fillText(formatPairPrice(lastVal), plotRight + 6, lastY + 3);

            if (hoverChartX !== null && hoverChartY !== null) {
                const clampedX = Math.max(plotLeft, Math.min(plotRight, hoverChartX));
                const clampedY = Math.max(plotTop, Math.min(plotBot, hoverChartY));
                ctx.save();
                ctx.setLineDash([3, 3]);
                ctx.strokeStyle = 'rgba(226, 232, 240, 0.35)';
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.moveTo(clampedX, plotTop); ctx.lineTo(clampedX, plotBot);
                ctx.moveTo(plotLeft, clampedY); ctx.lineTo(plotRight, clampedY);
                ctx.stroke();
                ctx.restore();

                const hoveredPrice = topP - ((clampedY - plotTop) / plotH) * (topP - botP);
                const hInfo = document.getElementById('chart-hover-info');
                if (hInfo) {
                    hInfo.innerText = `${selectedChartCoin}/USDC · Cena: ${formatPairPrice(hoveredPrice)} · Fib ${fibLevel}: ${formatPairPrice(fibPrice)}`;
                }
            }

            const readout = document.getElementById('chart-live-readout');
            if (readout) {
                const chgTxt = matched ? ` (${matched.chgPct >= 0 ? '+' : ''}${matched.chgPct.toFixed(2)}% 24h)` : '';
                readout.innerText = `${selectedChartCoin}/USDC · HA ${formatPairPrice(lastVal)}${chgTxt}`;
            }
            ctx.restore();
        }

        function renderTop24VolumeList() {
            const listEl = document.getElementById('top24-volume-list');
            if (!listEl) return;
            const sortedPairs = [...top24Pairs].sort((a, b) => Number(b.chgPct || 0) - Number(a.chgPct || 0));
            const maxGainer = Math.max(1.0, ...sortedPairs.map(p => Math.abs(Number(p.chgPct || 0))));

            listEl.innerHTML = sortedPairs.map((item, idx) => {
                const chg = Number(item.chgPct || 0);
                const chgCls = chg >= 0 ? "text-green" : "text-red";
                const chgStr = `${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%`;
                const meterPct = Math.min(100, Math.max(10, (Math.abs(chg) / maxGainer) * 100));
                const isSel = item.coin === selectedChartCoin ? "selected-pair" : "";
                const sigCls = item.signal === "BUY" ? "text-green" : "text-cyan";
                const badgeText = item.signal === "BUY" ? `BUY #${idx + 1}` : `#${idx + 1} · ${item.signal}`;
                return `
                    <div class="top24-row ${isSel}" data-coin="${item.coin}" onclick="selectChartCoin('${item.coin}')" title="${item.name} · 24h rozsah ${item.range24h} · Objem ${item.volBase}">
                        <div>
                            <b>${item.instId}</b>
                            <span class="gainer-subname">${item.name} · ${formatPairPrice(item.last)}</span>
                        </div>
                        <span class="${chgCls}">${chgStr}</span>
                        <div>
                            <div style="display:flex;justify-content:space-between;font-size:0.57rem;color:var(--text-secondary);">
                                <span>24h: ${item.range24h}</span>
                                <span>$${Number(item.vol24hM || 0).toFixed(2)}M</span>
                            </div>
                            <div class="vol-meter-track"><div class="vol-meter-fill" style="width:${meterPct.toFixed(0)}%;"></div></div>
                        </div>
                        <span class="${sigCls}" style="text-align:right;">${badgeText}</span>
                    </div>
                `;
            }).join('');
        }

        async function refreshOkxTop24Volume(showToast = false) {
            try {
                const res = await fetch('/api/okx-top24-volume');
                const data = await res.json();
                if (data.status === 'success' && Array.isArray(data.pairs) && data.pairs.length > 0) {
                    top24Pairs = data.pairs.map((p, idx) => {
                        const baseCoin = String(p.coin || String(p.instId || "").split("-")[0] || "").trim();
                        const snapMeta = OKX_SPOT_USDC_GAINERS_SNAPSHOT.find(s => s.coin.toUpperCase() === baseCoin.toUpperCase());
                        const chgVal = Number(p.chg_24h_pct !== undefined ? p.chg_24h_pct : (snapMeta ? snapMeta.chgPct : 0));
                        const lastVal = Number(p.last_price || (snapMeta ? snapMeta.last : 0));
                        const volM = Number(p.vol_24h_m || 0);
                        return {
                            coin: snapMeta ? snapMeta.coin : baseCoin,
                            instId: `${snapMeta ? snapMeta.coin : baseCoin}/USDC`,
                            okxInstId: String(p.instId || `${baseCoin}-USDC`),
                            name: snapMeta ? snapMeta.name : baseCoin,
                            last: lastVal > 0 ? lastVal : (snapMeta ? snapMeta.last : 0),
                            chgPct: chgVal,
                            range24h: p.range_24h || (snapMeta ? snapMeta.range24h : "--"),
                            vol24hM: volM,
                            volBase: p.vol_base || (snapMeta ? snapMeta.volBase : ""),
                            signal: p.signal || (idx < 5 ? "BUY" : "SCAN")
                        };
                    }).sort((a, b) => Number(b.chgPct || 0) - Number(a.chgPct || 0));

                    top24Pairs.forEach(item => {
                        if (Number(item.last) > 0) {
                            COIN_BASE_PRICES[item.coin] = Number(item.last);
                        }
                    });
                    haSeries = generateHaSeries(selectedChartCoin);
                    renderTop24VolumeList();

                    const matchedSel = top24Pairs.find(p => p.coin === selectedChartCoin) || top24Pairs[0];
                    if (matchedSel) {
                        const radarPairEl = document.getElementById('radar-active-pair');
                        const radarChgEl = document.getElementById('radar-active-chg');
                        const radarVolEl = document.getElementById('radar-active-vol');
                        if (radarPairEl) radarPairEl.innerText = matchedSel.instId;
                        if (radarChgEl) {
                            radarChgEl.innerText = `${matchedSel.chgPct >= 0 ? '+' : ''}${Number(matchedSel.chgPct).toFixed(2)}%`;
                            radarChgEl.className = matchedSel.chgPct >= 0 ? "text-green" : "text-red";
                        }
                        if (radarVolEl) radarVolEl.innerText = `$${Number(matchedSel.vol24hM || 0).toFixed(2)}M`;
                    }

                    const tag = document.getElementById('top24-updated-tag');
                    const pill = document.getElementById('top24-source-pill');
                    const cntLbl = document.getElementById('top24-count-lbl');
                    if (tag) tag.innerText = `OKX ${data.updated_at || ''}`;
                    if (pill) pill.innerText = data.is_live ? "OKX 24H LIVE" : "OKX 24H SNAPSHOT";
                    if (cntLbl) cntLbl.innerText = `Spot Top Gainers · ${top24Pairs.length} USDC par (zoradene podla 24h %)`;
                    if (showToast) showToastMessage("OKX 24h USDC gainers obnovene.", "ok");
                }
            } catch (e) {
                if (showToast) showToastMessage("Chyba pri nacitani 24h USDC gainers z OKX.", "err");
            }
        }

        function showToastMessage(msg, kind = "info") {
            const container = document.getElementById("toast-container");
            if (!container) return;
            const el = document.createElement("div");
            el.className = `toast ${kind === "ok" ? "toast-ok" : (kind === "err" ? "toast-err" : "")}`;
            el.innerText = msg;
            container.appendChild(el);
            setTimeout(() => { el.remove(); }, 3400);
        }

        function askConfirmation(title, message, onConfirm) {
            const modal = document.getElementById("confirm-modal");
            document.getElementById("modal-title").innerText = title;
            document.getElementById("modal-msg").innerText = message;
            const cancelBtn = document.getElementById("modal-cancel-btn");
            const confirmBtn = document.getElementById("modal-confirm-btn");
            const close = () => {
                modal.classList.remove("open");
                cancelBtn.onclick = null;
                confirmBtn.onclick = null;
            };
            cancelBtn.onclick = close;
            confirmBtn.onclick = async () => { close(); if (onConfirm) await onConfirm(); };
            modal.classList.add("open");
        }

        async function toggleBot() {
            const res = await fetch('/api/toggle-bot', { method: 'POST' });
            const data = await res.json();
            if (data.status === 'success') { showToastMessage(data.is_bot_active ? "Bot aktivovany." : "Bot pozastaveny.", "ok"); updateState(); }
        }
        async function toggleBtcFilter() {
            const res = await fetch('/api/toggle-btc-filter', { method: 'POST' });
            const data = await res.json();
            if (data.status === 'success') { showToastMessage(`BTC filter: ${data.use_btc_filter ? 'ON' : 'OFF'}`, "ok"); updateState(); }
        }
        async function toggleCompounding() {
            const res = await fetch('/api/toggle-compounding', { method: 'POST' });
            const data = await res.json();
            if (data.status === 'success') { showToastMessage(`Compounding: ${data.compounding_enabled ? 'ON' : 'OFF'}`, "ok"); updateState(); }
        }
        async function setBudget(amt) {
            const res = await fetch(`/api/set-budget/${amt}`, { method: 'POST' });
            const data = await res.json();
            if (data.status === 'success') { showToastMessage(`Rozpocet nastaveny na ${amt}.`, "ok"); updateState(); }
        }
        async function setParallel(cnt) {
            const res = await fetch(`/api/set-parallel/${cnt}`, { method: 'POST' });
            const data = await res.json();
            if (data.status === 'success') { showToastMessage(`Paralelne obchody: ${cnt}x`, "ok"); updateState(); }
        }
        async function setTf(tfVal) {
            const res = await fetch(`/api/set-tf/${tfVal}`, { method: 'POST' });
            const data = await res.json();
            if (data.status === 'success') { showToastMessage(`Timeframe zmeneny na ${data.tf}.`, "ok"); updateState(); }
        }

        function collectPinePayload() {
            return {
                mode_choice: document.getElementById('pine-mode').value,
                fib_choice: document.getElementById('pine-fib-choice').value,
                require_green_ha_buy: document.getElementById('pine-green-ha').checked,
                require_bull_tf_buy: document.getElementById('pine-require-bull').checked,
                require_sell_before_rebuy: document.getElementById('pine-sell-before-rebuy').checked,
                use_ai_filter: document.getElementById('pine-use-ai').checked,
                use_fixed_sl: document.getElementById('pine-use-fixed-sl').checked,
                fixed_sl_pct: parseFloat(document.getElementById('pine-fixed-sl-pct').value || "1.5"),
                use_fixed_tp: document.getElementById('pine-use-fixed-tp').checked,
                fixed_tp_pct: parseFloat(document.getElementById('pine-fixed-tp-pct').value || "1.2"),
                use_ha_exit: document.getElementById('pine-use-ha-exit').checked,
                ha_exit_consecutive: parseInt(document.getElementById('pine-ha-consec').value || "1", 10),
                sell_on_osc_reversal: document.getElementById('pine-use-osc-sell').checked,
                simulate_fees: document.getElementById('pine-sim-fees').checked,
                taker_fee_pct: parseFloat(document.getElementById('pine-taker-fee').value || "0.10"),
                use_trailing: document.getElementById('pine-use-trailing').checked,
                trail_pct: parseFloat(document.getElementById('pine-trail-pct').value || "1.5"),
                use_breakeven: document.getElementById('pine-use-be').checked,
                be_trigger_pct: parseFloat(document.getElementById('pine-be-trigger').value || "1.2"),
                use_trailing_tp: document.getElementById('pine-use-ttp').checked,
                ttp_trigger_pct: parseFloat(document.getElementById('pine-ttp-trigger').value || "1.2"),
                ttp_callback_pct: parseFloat(document.getElementById('pine-ttp-callback').value || "0.4"),
                strict_buy_filter: document.getElementById('pine-strict-buy').checked,
                fast_len: parseInt(document.getElementById('pine-fast-len').value || "5", 10),
                slow_len: parseInt(document.getElementById('pine-slow-len').value || "20", 10),
                signal_len: parseInt(document.getElementById('pine-signal-len').value || "9", 10),
                pred_look: parseInt(document.getElementById('pine-pred-look').value || "3", 10),
                pivot_len: parseInt(document.getElementById('pine-pivot-len').value || "2", 10),
                use_anti_chase: document.getElementById('pine-use-anti-top').checked,
                max_candle_pump_pct: parseFloat(document.getElementById('pine-max-pump').value || "0.9"),
                max_ema9_dist_pct: parseFloat(document.getElementById('pine-max-ema-dist').value || "0.7"),
                max_upper_wick_ratio: parseFloat(document.getElementById('pine-max-wick').value || "0.45")
            };
        }

        function applySettingsToForm(s) {
            if (!s) return;
            document.getElementById('pine-mode').value = s.mode_choice || "FIB";
            document.getElementById('pine-fib-choice').value = s.fib_choice || "0.786";
            document.getElementById('pine-green-ha').checked = Boolean(s.require_green_ha_buy);
            document.getElementById('pine-require-bull').checked = Boolean(s.require_bull_tf_buy);
            document.getElementById('pine-sell-before-rebuy').checked = Boolean(s.require_sell_before_rebuy);
            document.getElementById('pine-use-ai').checked = Boolean(s.use_ai_filter);
            document.getElementById('pine-use-fixed-sl').checked = Boolean(s.use_fixed_sl);
            document.getElementById('pine-fixed-sl-pct').value = s.fixed_sl_pct ?? 1.5;
            document.getElementById('pine-use-fixed-tp').checked = Boolean(s.use_fixed_tp);
            document.getElementById('pine-fixed-tp-pct').value = s.fixed_tp_pct ?? 1.2;
            document.getElementById('pine-use-ha-exit').checked = Boolean(s.use_ha_exit);
            document.getElementById('pine-ha-consec').value = String(s.ha_exit_consecutive ?? 1);
            document.getElementById('pine-use-osc-sell').checked = Boolean(s.sell_on_osc_reversal);
            document.getElementById('pine-sim-fees').checked = s.simulate_fees !== undefined ? Boolean(s.simulate_fees) : true;
            document.getElementById('pine-taker-fee').value = s.taker_fee_pct ?? 0.10;
            document.getElementById('pine-use-trailing').checked = Boolean(s.use_trailing);
            document.getElementById('pine-trail-pct').value = s.trail_pct ?? 1.5;
            document.getElementById('pine-use-be').checked = Boolean(s.use_breakeven);
            document.getElementById('pine-be-trigger').value = s.be_trigger_pct ?? 1.2;
            document.getElementById('pine-use-ttp').checked = Boolean(s.use_trailing_tp);
            document.getElementById('pine-ttp-trigger').value = s.ttp_trigger_pct ?? 1.2;
            document.getElementById('pine-ttp-callback').value = s.ttp_callback_pct ?? 0.4;
            document.getElementById('pine-strict-buy').checked = Boolean(s.strict_buy_filter);
            document.getElementById('pine-fast-len').value = s.fast_len ?? 5;
            document.getElementById('pine-slow-len').value = s.slow_len ?? 20;
            document.getElementById('pine-signal-len').value = s.signal_len ?? 9;
            document.getElementById('pine-pred-look').value = s.pred_look ?? 3;
            document.getElementById('pine-pivot-len').value = s.pivot_len ?? 2;
            document.getElementById('pine-use-anti-top').checked = Boolean(s.use_anti_chase);
            document.getElementById('pine-max-pump').value = s.max_candle_pump_pct ?? 0.9;
            document.getElementById('pine-max-ema-dist').value = s.max_ema9_dist_pct ?? 0.7;
            document.getElementById('pine-max-wick').value = s.max_upper_wick_ratio ?? 0.45;
            document.getElementById('header-fib').innerText = s.fib_choice || "0.786";
            document.getElementById('scanner-fib-level').innerText = s.fib_choice || "0.786";
            document.getElementById('chart-fib-label').innerText = s.fib_choice || "0.786";
            currentFibLevel = parseFloat(s.fib_choice || "0.786") || 0.786;
            document.getElementById('header-sl').innerText = s.use_fixed_sl ? `-${s.fixed_sl_pct ?? 1.5}%` : "OFF";
            document.getElementById('pine-active-badge').innerText = `REZIM: ${s.mode_choice || "FIB"} · FIB ${s.fib_choice || "0.786"}`;
        }

        async function savePineSettings() {
            const payload = collectPinePayload();
            const res = await fetch('/api/update-pine-settings', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (data.status === 'success') {
                applySettingsToForm(data.pine_settings);
                document.getElementById('pine-save-status').innerText = "Ulozene v trvalej pamati!";
                showToastMessage("Nastavenia ulozene.", "ok");
                updateState();
            } else {
                showToastMessage(data.message || "Chyba pri ukladani.", "err");
            }
        }

        async function resetPineDefaults() {
            const defaults = {
                mode_choice: "FIB", fib_choice: "0.786", require_green_ha_buy: false,
                require_bull_tf_buy: false, require_sell_before_rebuy: false, use_ai_filter: false,
                use_fixed_sl: false, fixed_sl_pct: 1.5, use_fixed_tp: false, fixed_tp_pct: 1.2,
                use_ha_exit: true, ha_exit_consecutive: 1, sell_on_osc_reversal: false,
                simulate_fees: true, taker_fee_pct: 0.10, use_trailing: true, trail_pct: 1.5,
                use_breakeven: true, be_trigger_pct: 1.2, use_trailing_tp: true, ttp_trigger_pct: 1.2,
                ttp_callback_pct: 0.4, strict_buy_filter: false, fast_len: 5, slow_len: 20,
                signal_len: 9, pred_look: 3, pivot_len: 2, use_anti_chase: false,
                max_candle_pump_pct: 0.9, max_ema9_dist_pct: 0.7, max_upper_wick_ratio: 0.45
            };
            applySettingsToForm(defaults);
            await savePineSettings();
        }

        async function manualTrainAi() {
            const res = await fetch('/api/train-ai', { method: 'POST' });
            const data = await res.json();
            if (data.status === 'success') { showToastMessage("AI model dotrenovany.", "ok"); updateState(); }
            else showToastMessage(data.message || "Chyba trenovania AI.", "err");
        }

        async function cleanResetLearning() {
            askConfirmation(
                "Cisty reset AI pamate",
                "Vymaze historiu obchodov, vzory a natrenovane vahy AI a spusti novy trening. Pokracovat?",
                async () => {
                    const res = await fetch('/api/clean-reset-learning', { method: 'POST' });
                    const data = await res.json();
                    if (data.status === 'success') { showToastMessage("Cisty reset AI dokonceny.", "ok"); updateState(); }
                }
            );
        }

        async function clearLogsNow() {
            const res = await fetch('/api/clear-logs', { method: 'POST' });
            const data = await res.json();
            if (data.status === 'success') { showToastMessage("2h logy precistene.", "ok"); updateState(); }
        }

        function exportLogs24h() {
            const items = latestLogs24h.length ? latestLogs24h : [];
            const txt = items.map(e => e.line || `[${e.time || ''}] ${e.message || ''}`).join('\n');
            const blob = new Blob([txt || "Ziadne 24h logy."], { type: "text/plain;charset=utf-8" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `apex_logs_24h_${new Date().toISOString().slice(0, 10)}.txt`;
            a.click();
            URL.revokeObjectURL(url);
        }

        async function manualBuyCustom() {
            const inp = document.getElementById('custom-coin');
            const coin = (inp.value || inp.placeholder || "").trim().toUpperCase();
            if (!coin) return;
            const res = await fetch(`/api/manual-buy/${encodeURIComponent(coin)}`, { method: 'POST' });
            const data = await res.json();
            if (data.status === 'success') { inp.value = ""; showToastMessage(`Manualny nakup ${data.symbol} odoslany.`, "ok"); updateState(); }
            else showToastMessage(data.message || "Nakup zamietnuty.", "err");
        }

        async function sellSingleCoin(coin) {
            askConfirmation(`Predat ${coin}`, `Naozaj chcete okamzite predat poziciu ${coin}?`, async () => {
                const res = await fetch(`/api/sell-coin/${encodeURIComponent(coin)}`, { method: 'POST' });
                const data = await res.json();
                if (data.status === 'success') { showToastMessage(`Pozicia ${data.sold} predana.`, "ok"); updateState(); }
                else showToastMessage(data.message || "Chyba predaja.", "err");
            });
        }

        async function sellNow() {
            askConfirmation("Predat vsetky pozicie", "Naozaj chcete okamzite uzavriet vsetky otvorene pozicie?", async () => {
                const res = await fetch('/api/sell-now', { method: 'POST' });
                const data = await res.json();
                if (data.status === 'success') { showToastMessage("Vsetky pozicie zatvorene.", "ok"); updateState(); }
                else showToastMessage(data.message || "Chyba predaja.", "err");
            });
        }

        function setScannerFilter(f) {
            currentScannerFilter = f;
            document.querySelectorAll('.scanner-filter-btn').forEach(b => b.classList.toggle('btn-active-pill', b.getAttribute('data-filter') === f));
            updateState();
        }
        function setLogFilter(cat) {
            currentLogCategory = cat;
            document.querySelectorAll('.log-filter-btn').forEach(b => b.classList.toggle('btn-active-pill', b.getAttribute('data-logcat') === cat));
            updateState();
        }

        async function updateState() {
            try {
                const res = await fetch('/api/state');
                const data = await res.json();

                document.getElementById('header-balance').innerText = `${Number(data.balance || 0).toFixed(2)} ${data.balance_ccy || 'USDC'}`;
                document.getElementById('header-tf').innerText = data.timeframe || '15M';
                document.getElementById('chart-tf-pill').innerText = data.timeframe || '15M';
                document.getElementById('current-stage').innerText = data.stage || '';
                document.getElementById('last-update-tag').innerText = `Aktualizacia: ${data.last_update || '--:--:--'}`;

                const regime = String(data.market_regime || 'BULL').toUpperCase();
                const badge = document.getElementById('market-regime-badge');
                badge.className = 'regime-badge ' + (regime === 'SIDEWAYS' ? 'regime-sideways' : (regime === 'BEAR' ? 'regime-bear' : 'regime-bull'));
                badge.innerText = regime === 'SIDEWAYS' ? 'BOCNY TRH (SIDEWAYS)' : (regime === 'BEAR' ? 'MEDVEDI TRH (BEAR)' : 'BYCI TRH (BULL)');

                document.querySelectorAll('.tf-btn').forEach(b => b.classList.toggle('btn-active-pill', b.getAttribute('data-tf').toUpperCase() === String(data.timeframe || '').toUpperCase()));
                document.querySelectorAll('.parallel-btn').forEach(b => b.classList.toggle('btn-active-pill', Number(b.getAttribute('data-val')) === Number(data.max_parallel)));
                document.querySelectorAll('.budget-btn').forEach(b => b.classList.toggle('btn-active-pill', Number(b.getAttribute('data-val')) === Number(data.max_budget_cap)));

                const botBtn = document.getElementById('bot-btn');
                botBtn.className = 'btn ' + (data.is_bot_active ? 'btn-on' : 'btn-off');
                botBtn.innerText = data.is_bot_active ? 'ACTIVE' : 'PAUSED';

                const btcBtn = document.getElementById('btc-btn');
                btcBtn.className = 'btn ' + (data.use_btc_filter ? 'btn-on' : 'btn-off');
                btcBtn.innerText = `BTC: ${data.use_btc_filter ? 'ON' : 'OFF'}`;

                const cmpBtn = document.getElementById('compounding-btn');
                cmpBtn.className = 'btn ' + (data.compounding_enabled ? 'btn-active-pill' : '');
                cmpBtn.innerText = `COMPOUND: ${data.compounding_enabled ? 'ON' : 'OFF'}`;

                const st = data.stats || {};
                document.getElementById('stat-winrate').innerText = `${Number(st.win_rate || 0).toFixed(1)}%`;
                document.getElementById('stat-trades').innerText = st.total_trades ?? 0;
                document.getElementById('stat-winloss').innerText = `${st.wins ?? 0}W / ${st.losses ?? 0}L`;
                const pnlVal = Number(st.total_pnl || 0);
                const pnlEl = document.getElementById('stat-pnl');
                pnlEl.innerText = `${pnlVal >= 0 ? '+' : ''}${pnlVal.toFixed(2)} $`;
                pnlEl.className = 'stat-value ' + (pnlVal > 0 ? 'text-green' : (pnlVal < 0 ? 'text-red' : ''));
                const grossPnl = Number(st.gross_pnl || 0);
                const totalFees = Number(st.total_fees || 0);
                document.getElementById('stat-pnl-sub').innerText = `Hruby: ${grossPnl >= 0 ? '+' : ''}${grossPnl.toFixed(2)} $ | Poplatky: -${totalFees.toFixed(2)} $`;

                const ai = data.ai_state || {};
                const rInfo = data.ai_readiness || {};
                const liveCnt = Number(ai.live_learned_count || 0);
                const liveWins = Number(ai.live_wins_count || 0);
                const liveLosses = Math.max(0, liveCnt - liveWins);
                document.getElementById('header-ai').innerText = (ai.samples_count || 0) >= 10 ? `${ai.accuracy_train || 0}% (${ai.samples_count} vz.)` : "Zbieram data...";
                document.getElementById('header-ai-learned').innerText = `${liveCnt} obchodov (${liveWins}W / ${liveLosses}L)`;

                const readPct = Number(rInfo.readiness_pct || 0);
                const remTrades = Number(rInfo.remaining_trades_to_80 ?? 160);
                const probTxt = (ai.last_prob !== null && ai.last_prob !== undefined) ? `${(Number(ai.last_prob) * 100).toFixed(1)}%` : "--";
                document.getElementById('ai-learned-summary').innerText =
                    `AI naucenie: ${readPct.toFixed(1)}% / ciel 80% (chyba ~${remTrades} obchodov) | Ulozene obchody: ${liveCnt} (${liveWins}W / ${liveLosses}L) | Pravdepodobnost: ${probTxt}`;
                document.getElementById('ai-readiness-bar').style.width = `${Math.min(100, Math.max(2, readPct))}%`;

                const activeList = Object.entries(data.active_trades || {});
                document.getElementById('sell-btn').disabled = activeList.length === 0;
                const posTbody = document.getElementById('active-position-tbody');
                if (activeList.length === 0) {
                    posTbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--text-muted);">Ziadne otvorene pozicie — cakam na BUY signal...</td></tr>';
                } else {
                    posTbody.innerHTML = activeList.map(([coin, t]) => {
                        const pnl = Number(t.live_pnl_pct || 0);
                        const pnlUsd = Number(t.live_pnl_usdc || 0);
                        const cls = pnl >= 0 ? 'text-green' : 'text-red';
                        const slTxt = t.use_fixed_sl && t.fixed_sl_price ? `SL ${Number(t.fixed_sl_price).toFixed(4)}` : (t.trail_stop_price ? `TS ${Number(t.trail_stop_price).toFixed(4)}` : 'Sleduje');
                        const ttpTxt = t.ttp_active && t.ttp_stop_price ? `TTP ${Number(t.ttp_stop_price).toFixed(4)}` : (t.use_fixed_tp ? `TP +${t.fixed_tp_pct}%` : 'Caka');
                        return `<tr>
                            <td><b>${t.market_pair}</b> <span class="text-cyan">[${t.tf || '15M'}]</span></td>
                            <td>${Number(t.entry_price || 0).toFixed(4)}</td>
                            <td>${Number(t.current_price || t.entry_price || 0).toFixed(4)}</td>
                            <td class="text-yellow">${ttpTxt}</td>
                            <td>${slTxt}</td>
                            <td>${Number(t.invested_usdc || 0).toFixed(2)}</td>
                            <td class="${cls}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}% (${pnlUsd >= 0 ? '+' : ''}${pnlUsd.toFixed(2)}$)</td>
                            <td><button class="btn btn-sell btn-mini" onclick="sellSingleCoin('${coin}')">SELL</button></td>
                        </tr>`;
                    }).join('');
                }

                const hist = data.history || [];
                document.getElementById('closed-trades-count').innerText = `${hist.length} obchodov`;
                const histTbody = document.getElementById('closed-trades-tbody');
                if (hist.length === 0) {
                    histTbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">Zatial ziadne uzavrete obchody.</td></tr>';
                } else {
                    histTbody.innerHTML = hist.slice(0, 30).map(h => {
                        const pUsd = Number(h.pnl_usdc || 0);
                        const pPct = Number(h.pnl_pct || 0);
                        const cls = pUsd >= 0 ? 'text-green' : 'text-red';
                        return `<tr>
                            <td>${h.time || '--'}</td>
                            <td><b>${h.symbol}</b></td>
                            <td>${h.style || 'SCALPING'}</td>
                            <td class="${cls}">${pPct >= 0 ? '+' : ''}${pPct.toFixed(2)}%</td>
                            <td class="${cls}">${pUsd >= 0 ? '+' : ''}${pUsd.toFixed(2)} $</td>
                            <td>${Number(h.gross_pnl_usdc || pUsd).toFixed(2)}$ / -${Number(h.fee_usdc || 0).toFixed(2)}$</td>
                            <td>${h.reason || '--'}</td>
                        </tr>`;
                    }).join('');
                }

                const searchVal = (document.getElementById('scanner-search').value || "").trim().toUpperCase();
                const scanEntries = Object.entries(data.scanner || {}).filter(([coin, row]) => {
                    if (searchVal && !coin.toUpperCase().includes(searchVal) && !String(row.symbol || "").toUpperCase().includes(searchVal)) return false;
                    if (currentScannerFilter === "BUY") return String(row.signal || "").toUpperCase() === "BUY";
                    if (currentScannerFilter === "BLOK") return String(row.exec_signal || "").toUpperCase().includes("BLOK");
                    if (currentScannerFilter === "IN_TRADE") return row.trade_status && row.trade_status !== "---";
                    return true;
                });
                const scanTbody = document.getElementById('scanner-tbody');
                if (scanEntries.length === 0) {
                    scanTbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--text-muted);">Ziadne pary nezodpovedaju filtru.</td></tr>';
                } else {
                    scanTbody.innerHTML = scanEntries.map(([coin, r]) => {
                        const sigCls = r.signal === "BUY" ? "text-green" : (String(r.exec_signal || "").includes("BLOK") ? "text-red" : "text-secondary");
                        return `<tr>
                            <td><b>${r.symbol || coin}</b></td>
                            <td>${Number(r.price || 0) > 0 ? Number(r.price).toFixed(4) : '--'}</td>
                            <td>${r.momentum || '--'}</td>
                            <td>${r.impulse || '--'}</td>
                            <td>${r.trade_status || '---'}</td>
                            <td class="text-cyan">${r.ai_score || '--'}</td>
                            <td>${r.ts_status || '--'}</td>
                            <td class="${sigCls}">${r.exec_signal || r.signal || '--'}</td>
                        </tr>`;
                    }).join('');
                }

                latestLogs24h = data.log_entries_24h || [];
                const logSearch = (document.getElementById('log-search').value || "").trim().toUpperCase();
                const logList = (data.log_entries || []).filter(e => {
                    if (currentLogCategory !== "ALL" && e.category !== currentLogCategory) return false;
                    if (logSearch && !String(e.line || "").toUpperCase().includes(logSearch)) return false;
                    return true;
                });
                document.getElementById('log-prune-info').innerText = `Auto-cistenie: kazde 2h (posledne: ${data.last_log_prune || '--'}) · 24h register: ${latestLogs24h.length}`;
                const logsBox = document.getElementById('logs-container');
                if (logList.length === 0) {
                    logsBox.innerHTML = '<div class="log-line">Ziadne logy pre zvoleny filter.</div>';
                } else {
                    logsBox.innerHTML = logList.slice(0, 120).map(e => {
                        const cls = e.category === "ERROR" ? "text-red" : (e.category === "TRADE" ? "text-green" : (e.category === "FILTER" ? "text-yellow" : ""));
                        return `<div class="log-line ${cls}">${e.line || ''}</div>`;
                    }).join('');
                }
            } catch (err) {}
        }

        @app.get("/")
        async def dashboard():
            pass
    </script>
</body>
</html>
"""


@app.get("/api/okx-top24-volume")
async def api_okx_top24_volume():
    def _fetch_okx_usdc_gainers_sync():
        res = okx_public_request_sync("/api/v5/market/tickers?instType=SPOT")
        pairs_out = []
        is_live = False
        if res.get("code") == "0" and res.get("data"):
            is_live = True
            snap_by_coin = {item["coin"].upper(): item for item in [
                {"coin": "NES", "name": "Nesa", "volBase": "89.90K NES", "signal": "BUY"},
                {"coin": "ACE", "name": "ACE", "volBase": "454.07K ACE", "signal": "BUY"},
                {"coin": "SKY", "name": "Sky", "volBase": "989.43K SKY", "signal": "BUY"},
                {"coin": "SPACE", "name": "Spacecoin", "volBase": "2.54M SPACE", "signal": "BUY"},
                {"coin": "NIGHT", "name": "Midnight", "volBase": "4.74M NIGHT", "signal": "BUY"},
                {"coin": "SENT", "name": "Sentient", "volBase": "1.69M SENT", "signal": "SCAN"},
                {"coin": "xANT", "name": "Anthropic", "volBase": "5.41K xANT", "signal": "SCAN"},
                {"coin": "YB", "name": "Yield Basis", "volBase": "22.24K YB", "signal": "SCAN"},
                {"coin": "xSNDK", "name": "SanDisk", "volBase": "444.92 xSNDK", "signal": "SCAN"},
                {"coin": "CFG", "name": "Centrifuge", "volBase": "83.40K CFG", "signal": "SCAN"},
                {"coin": "WIN", "name": "WINkLink", "volBase": "4.36M WIN", "signal": "SCAN"},
                {"coin": "AUCTION", "name": "AUCTION", "volBase": "457.05 AUCTION", "signal": "SCAN"},
                {"coin": "BABY", "name": "Babylon", "volBase": "108.29K BABY", "signal": "SCAN"},
                {"coin": "LQTY", "name": "Liquity", "volBase": "1.43K LQTY", "signal": "SCAN"},
                {"coin": "xTSLA", "name": "Tesla", "volBase": "128.36 xTSLA", "signal": "SCAN"},
            ]}
            rows = []
            for t in res["data"]:
                inst = str(t.get("instId", "")).strip()
                if not inst.endswith("-USDC"):
                    continue
                base_raw = inst[:-5]
                last_p = safe_float(t.get("last"), 0.0)
                open24 = safe_float(t.get("open24h"), 0.0)
                high24 = safe_float(t.get("high24h"), 0.0)
                low24 = safe_float(t.get("low24h"), 0.0)
                vol_ccy = safe_float(t.get("volCcy24h"), 0.0)
                if last_p <= 0 or open24 <= 0 or vol_ccy < 25000.0:
                    continue
                chg_pct = ((last_p - open24) / open24) * 100.0
                if chg_pct <= 0:
                    continue
                rows.append((chg_pct, vol_ccy, inst, base_raw, last_p, high24, low24))

            rows.sort(key=lambda x: x[0], reverse=True)
            for idx, (chg_pct, vol_ccy, inst, base_raw, last_p, high24, low24) in enumerate(rows[:15]):
                meta = snap_by_coin.get(base_raw.upper())
                disp_coin = meta["coin"] if meta else base_raw
                fmt_p = lambda v: f"{v:.8f}" if (0 < v < 0.001) else (f"{v:.6f}" if v < 0.05 else (f"{v:.4f}" if v < 2 else f"{v:.2f}"))
                rng_txt = f"{fmt_p(low24)} - {fmt_p(high24)}" if (high24 > 0 and low24 > 0) else "--"
                pairs_out.append({
                    "coin": disp_coin,
                    "instId": inst,
                    "last_price": last_p,
                    "chg_24h_pct": round(chg_pct, 2),
                    "range_24h": rng_txt,
                    "vol_24h_m": round(vol_ccy / 1e6, 2),
                    "vol_base": meta["volBase"] if meta else f"{vol_ccy/1e3:.1f}K {disp_coin}",
                    "signal": "BUY" if idx < 5 else "SCAN",
                })

        if not pairs_out:
            is_live = False
            fallback_items = [
                ("NES", "NES-USDC", 0.2839, 19.24, "0.2256 - 0.2925", 84.73, "89.90K NES", "BUY"),
                ("ACE", "ACE-USDC", 0.05298, 13.54, "0.04442 - 0.05655", 6.25, "454.07K ACE", "BUY"),
                ("SKY", "SKY-USDC", 0.05161, 11.86, "0.04499 - 0.05458", 1210.0, "989.43K SKY", "BUY"),
                ("SPACE", "SPACE-USDC", 0.004543, 11.79, "0.004064 - 0.004677", 29.55, "2.54M SPACE", "BUY"),
                ("NIGHT", "NIGHT-USDC", 0.053966, 10.16, "0.047892 - 0.055380", 902.45, "4.74M NIGHT", "BUY"),
                ("SENT", "SENT-USDC", 0.02486, 9.13, "0.02203 - 0.02511", 178.70, "1.69M SENT", "SCAN"),
                ("xANT", "XANT-USDC", 91.36, 6.78, "84.33 - 93.13", 48.39, "5.41K xANT", "SCAN"),
                ("YB", "YB-USDC", 0.06104, 6.36, "0.05739 - 0.06348", 18.48, "22.24K YB", "SCAN"),
                ("xSNDK", "XSNDK-USDC", 85.52, 5.26, "77.33 - 86.26", 20.98, "444.92 xSNDK", "SCAN"),
                ("CFG", "CFG-USDC", 0.05963, 3.90, "0.05528 - 0.07290", 9.60, "83.40K CFG", "SCAN"),
                ("WIN", "WIN-USDC", 0.00005520, 3.86, "0.00005264 - 0.00005571", 51.21, "4.36M WIN", "SCAN"),
                ("AUCTION", "AUCTION-USDC", 2.614, 3.16, "2.534 - 2.736", 19.05, "457.05 AUCTION", "SCAN"),
                ("BABY", "BABY-USDC", 0.009644, 3.02, "0.009361 - 0.009781", 49.94, "108.29K BABY", "SCAN"),
                ("LQTY", "LQTY-USDC", 0.1324, 2.87, "0.1287 - 0.1324", 13.15, "1.43K LQTY", "SCAN"),
                ("xTSLA", "XTSLA-USDC", 399.14, 2.38, "383.57 - 401.46", 50.89, "128.36 xTSLA", "SCAN"),
            ]
            for coin, inst, last_p, chg_pct, rng_txt, vol_m, vol_b, sig in fallback_items:
                pairs_out.append({
                    "coin": coin,
                    "instId": inst,
                    "last_price": last_p,
                    "chg_24h_pct": chg_pct,
                    "range_24h": rng_txt,
                    "vol_24h_m": vol_m,
                    "vol_base": vol_b,
                    "signal": sig,
                })
        return pairs_out, is_live

    pairs_list, is_live = await asyncio.to_thread(_fetch_okx_usdc_gainers_sync)
    return JSONResponse({
        "status": "success",
        "is_live": is_live,
        "updated_at": get_sk_time_str(),
        "pairs": pairs_list,
    })


@app.get("/api/state")
async def get_state():
    _prune_old_logs()
    balance = await fetch_account_capital("USDC", force_refresh=False)
    stats = calculate_stats()
    ai_readiness = compute_ai_readiness_info()
    return JSONResponse({
        "is_bot_active": IS_BOT_ACTIVE,
        "balance": round(balance, 2),
        "balance_ccy": ACTIVE_BALANCE_CCY,
        "timeframe": CURRENT_TIMEFRAME,
        "market_regime": MARKET_REGIME,
        "is_sideways_market": IS_SIDEWAYS_MARKET,
        "max_parallel": SETTINGS["max_parallel"],
        "max_budget_cap": SETTINGS["max_budget_cap"],
        "compounding_enabled": SETTINGS.get("compounding_enabled", False),
        "use_btc_filter": SETTINGS.get("use_btc_filter", False),
        "pine_settings": get_pine_settings_dict(),
        "ai_state": {k: v for k, v in AI_STATE.items() if not str(k).startswith("_")},
        "ai_readiness": ai_readiness,
        "stage": CURRENT_STAGE,
        "last_update": get_sk_time_str(),
        "active_trades": ACTIVE_TRADES,
        "history": CLOSED_TRADES_HISTORY[:50],
        "scanner": MARKET_SCANNER_CACHE,
        "stats": stats,
        "logs": SYSTEM_LOGS[:120],
        "log_entries": SYSTEM_LOG_ENTRIES[:120],
        "log_entries_24h": SYSTEM_LOG_24H_ENTRIES[:400],
        "last_log_prune": LAST_LOG_PRUNE_STR,
    })


@app.get("/", response_class=HTMLResponse)
async def index():
    html_clean = HTML_TEMPLATE_CONTENT.replace(
        '        @app.get("/")\n        async def dashboard():\n            pass\n',
        """
        attachChartHover();
        renderTop24VolumeList();
        renderHaFibChart();
        refreshOkxTop24Volume(false);
        updateState();
        setInterval(renderHaFibChart, 900);
        setInterval(updateState, 2500);
        setInterval(() => refreshOkxTop24Volume(false), 30000);
"""
    )
    template = Template(html_clean)
    rendered = template.render(
        IS_BOT_ACTIVE=IS_BOT_ACTIVE,
        CURRENT_TIMEFRAME=CURRENT_TIMEFRAME,
        CURRENT_STAGE=CURRENT_STAGE,
        SETTINGS=SETTINGS,
    )
    return HTMLResponse(content=rendered)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
