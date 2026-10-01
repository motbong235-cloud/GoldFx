"""Gold Fx signal engine: liquidity sweep (same logic as goldfx_signal.pine / dashboard chart) → Telegram.

Runs as a background thread inside server.py.
Settings (Admin → Bakong · Settings / Signal):
  TG_BOT_TOKEN, SIGNAL_CHANNEL_ID, SIGNAL_ENABLED ("0"=off),
  SIGNAL_TIMEFRAMES (default "5m,15m,1h"), SIGNAL_SYMBOLS (default "PAXGUSDT")
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import gold_feed
except ImportError:
    gold_feed = None

BASES = [
    "https://data-api.binance.vision/api/v3/",
    "https://api.binance.com/api/v3/",
    "https://api1.binance.com/api/v3/",
    "https://api2.binance.com/api/v3/",
]
_good = 0
POLL_SEC = 10
KL_REFRESH = 20          # seconds between kline refreshes per (symbol, timeframe)
KL_LIMIT = 300
MAX_AGE_BARS = 3         # only post a signal if its candle closed within the last N bars
NAMES = {"PAXGUSDT": "XAUUSD", "XAUUSDT": "XAUUSD"}
# Same defaults as the Pine indicator / dashboard (overridden by input() defaults in Admin → Indicator code)
PARAMS = {"swingLen": 10, "slBuf": 0.5, "rr1": 1.0, "rr2": 2.0, "et2Pct": 0.78}
TF_SEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400}

STATE = {"running": False, "last_sent": "", "last_error": "", "sent_count": 0, "last_price": "", "last_check": "", "last_levels": ""}
_lock_fh = None

def market(path: str, timeout: int = 6, budget: int | None = None, **_kwargs):
    """Fetch Binance public market data. `budget` = max mirrors to try (optional)."""
    global _good
    last = None
    n_try = len(BASES) if budget is None else max(1, min(int(budget), len(BASES)))
    for n in range(n_try):
        i = (_good + n) % len(BASES)
        try:
            req = urllib.request.Request(
                BASES[i] + path,
                headers={"User-Agent": "GoldFx/1.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
            _good = i
            return data
        except Exception as e:
            last = e
    raise last if last else RuntimeError("market data unavailable")


def load_params(settings: dict) -> dict:
    """Read input() defaults from the admin's Pine code so chart, Pine and Telegram agree."""
    p = dict(PARAMS)
    code = str((settings or {}).get("INDICATOR_CODE") or "")
    for name in p:
        m = re.search(r"\b" + name + r"\s*=\s*input\.(?:int|float)\(\s*(-?[\d.]+)", code)
        if m:
            try:
                p[name] = float(m.group(1))
            except ValueError:
                pass
    p["swingLen"] = max(2, int(p["swingLen"]))
    return p


def find_signals(kl, p: dict, now_ms: float | None = None):
    """Liquidity sweep: swing high/low is taken, candle closes back inside → signal.
    Mirrors goldfx_signal.pine and findSignals() in dashboard.html (closed candles only)."""
    now_ms = now_ms or time.time() * 1000
    closed = [k for k in kl if float(k[6]) < now_ms]
    L = p["swingLen"]
    out = []
    last_high = last_low = None
    hi_idx = lo_idx = -1
    high_swept = low_swept = True
    for i in range(len(closed)):
        q = i - L
        if q >= L:
            is_h = is_l = True
            qh, ql = float(closed[q][2]), float(closed[q][3])
            for j in range(q - L, q + L + 1):
                if j == q:
                    continue
                if float(closed[j][2]) > qh:
                    is_h = False
                if float(closed[j][3]) < ql:
                    is_l = False
            if is_h:
                last_high, hi_idx, high_swept = qh, q, False
            if is_l:
                last_low, lo_idx, low_swept = ql, q, False
        h, l, c = float(closed[i][2]), float(closed[i][3]), float(closed[i][4])
        t = int(closed[i][0])
        if not high_swept and last_high is not None and h > last_high and c < last_high:
            high_swept = True
            sl = h + p["slBuf"]
            risk = sl - c
            out.append({"side": "SELL", "i": i, "t": t, "close_ms": float(closed[i][6]), "liq": last_high,
                        "entry": c, "et2": sl - risk * p["et2Pct"], "sl": sl,
                        "tp1": c - risk * p["rr1"], "tp2": c - risk * p["rr2"]})
        if not low_swept and last_low is not None and l < last_low and c > last_low:
            low_swept = True
            sl = l - p["slBuf"]
            risk = c - sl
            out.append({"side": "BUY", "i": i, "t": t, "close_ms": float(closed[i][6]), "liq": last_low,
                        "entry": c, "et2": sl + risk * p["et2Pct"], "sl": sl,
                        "tp1": c + risk * p["rr1"], "tp2": c + risk * p["rr2"]})
    return out


def format_signal(sym: str, tf: str, s: dict, price: float) -> str:
    name = NAMES.get(sym, sym)
    sell = s["side"] == "SELL"
    head = "🔴 SELL" if sell else "🟢 BUY"
    key = "BSL $$$ 💵" if sell else "Key 🔑"
    ict = time.strftime("%H:%M:%S", time.gmtime(time.time() + 7 * 3600))
    return (
        f"{head} · <b>{name}</b> · {tf}\n"
        f"━━━━━━━━━━━━━━\n"
        f"TP 2: <code>{s['tp2']:.2f}</code>\n"
        f"TP 1: <code>{s['tp1']:.2f}</code>\n"
        f"Et 1: <code>{s['entry']:.2f}</code>\n"
        f"Et 2: <code>{s['et2']:.2f}</code>\n"
        f"SL: <code>{s['sl']:.2f}</code>\n"
        f"━━━━━━━━━━━━━━\n"
        f"{key} <code>{s['liq']:.2f}</code> (liquidity swept)\n"
        f"💰 Price: <code>{price:.2f}</code>\n"
        f"⏰ {ict} (ICT) · Gold Fx Signal"
    )


def send(token: str, chat_id: str, text: str):
    """Returns (ok, error_message). chat_id = @channel or -100xxxxxxxxxx"""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
            j = json.loads(r.read().decode())
            return bool(j.get("ok")), "" if j.get("ok") else str(j)
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read().decode()).get("description", "")
        except Exception:
            desc = ""
        return False, f"Telegram {e.code}: {desc}"
    except Exception as e:
        return False, str(e)


def _cfg(get_settings):
    s = get_settings() or {}
    tok = str(s.get("TG_BOT_TOKEN") or "").strip()
    ch = str(s.get("SIGNAL_CHANNEL_ID") or "").strip()
    on = str(s.get("SIGNAL_ENABLED", "1")) != "0"
    syms = [
        x.strip().upper()
        for x in str(s.get("SIGNAL_SYMBOLS") or "PAXGUSDT").split(",")
        if x.strip()
    ]
    tfs = [
        x.strip()
        for x in str(s.get("SIGNAL_TIMEFRAMES") or "5m,15m,1h").split(",")
        if x.strip()
    ]
    return tok, ch, on, syms, tfs


def _loop(get_settings):
    last_kl, seen = {}, {}
    STATE["running"] = True
    while True:
        try:
            tok, ch, on, syms, tfs = _cfg(get_settings)
            params = load_params(get_settings())
        except Exception as e:
            STATE["last_error"] = f"settings: {e}"
            time.sleep(POLL_SEC)
            continue
        if not (tok and ch and on):
            seen.clear()      # re-seed when turned back on → no burst of old signals
            time.sleep(POLL_SEC)
            continue
        now = time.time()
        for sym in syms:
            for tf in tfs:
                k = (sym, tf)
                if now - last_kl.get(k, 0) < KL_REFRESH:
                    continue
                try:
                    kl = market(f"klines?symbol={sym}&interval={tf}&limit={KL_LIMIT}")
                    last_kl[k] = now
                    sigs = find_signals(kl, params, now * 1000)
                    price = float(kl[-1][4])
                    STATE["last_price"] = f"{sym} {price:.2f}"
                    STATE["last_check"] = time.strftime("%H:%M:%S", time.gmtime(now + 7 * 3600))
                    STATE["last_levels"] = f"{sym} {tf} sweep · swing={params['swingLen']} signals_in_window={len(sigs)}"
                    keys = {(s["side"], s["t"]) for s in sigs}
                    if k not in seen:
                        seen[k] = keys           # first pass: remember history, don't send
                        continue
                    age = TF_SEC.get(tf, 900) * MAX_AGE_BARS * 1000
                    for s in sigs:
                        sk = (s["side"], s["t"])
                        if sk in seen[k]:
                            continue
                        seen[k].add(sk)
                        if now * 1000 - s["close_ms"] > age:
                            continue
                        ok, err = send(tok, ch, format_signal(sym, tf, s, price))
                        if ok:
                            STATE["sent_count"] += 1
                            STATE["last_sent"] = f"{s['side']} {NAMES.get(sym, sym)} {tf} @ {s['entry']:.2f}"
                            STATE["last_error"] = ""
                        else:
                            STATE["last_error"] = err
                except Exception as e:
                    STATE["last_error"] = f"{sym} {tf}: {e}"
        time.sleep(POLL_SEC)


def _acquire_lock(path):
    global _lock_fh
    try:
        import fcntl
        _lock_fh = open(path, "w")
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except ImportError:
        return True
    except OSError:
        return False


def start_background(get_settings, lock_path):
    if not _acquire_lock(lock_path):
        return False
    threading.Thread(target=_loop, args=(get_settings,), daemon=True, name="signal-engine").start()
    return True


if __name__ == "__main__":
    t, c = os.environ.get("BOT_TOKEN", ""), os.environ.get("CHANNEL_ID", "")
    if not (t and c):
        raise SystemExit("Set BOT_TOKEN and CHANNEL_ID")
    _loop(lambda: {
        "TG_BOT_TOKEN": t,
        "SIGNAL_CHANNEL_ID": c,
        "SIGNAL_SYMBOLS": os.environ.get("SYMBOLS", "PAXGUSDT"),
        "SIGNAL_TIMEFRAMES": os.environ.get("TIMEFRAMES", "5m,15m,1h"),
    })
