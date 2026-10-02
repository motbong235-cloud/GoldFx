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
from pathlib import Path
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
MAX_AGE_BARS = 5         # post a signal if its candle closed within the last N bars (admin: SIGNAL_MAX_AGE_BARS)
                         # larger = fewer missed signals after a restart / sleep; smaller = never post stale ones
NAMES = {"PAXGUSDT": "XAUUSD", "XAUUSDT": "XAUUSD"}
# Same defaults as the Pine indicator / dashboard (overridden by input() defaults in Admin → Indicator code)
PARAMS = {"swingLen": 10, "slBuf": 0.5, "rr1": 1.0, "rr2": 2.0, "et2Pct": 0.78, "slLookback": 10,
          "minWick": 0.0, "minRisk": 0.0, "maxRisk": 0.0}
# "Clear signal" filters (0 = off): minWick = rejection wick must be >= this share of the candle range,
# minRisk / maxRisk = allowed SL distance in $ (skips noise-tight and over-wide setups)
# slLookback = how many candles to count BACK from the signal candle (signal candle included) to place SL
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
    p["slLookback"] = max(1, int(p["slLookback"]))
    return p


def _clear(p: dict, wick: float, risk: float) -> bool:
    """Only keep signals with a real rejection wick and a sensible SL distance."""
    if p["minWick"] and wick < p["minWick"]:
        return False
    if p["minRisk"] and risk < p["minRisk"]:
        return False
    if p["maxRisk"] and risk > p["maxRisk"]:
        return False
    return True


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
            win = closed[max(0, i - p["slLookback"] + 1): i + 1]      # count candles backwards
            sl = max(float(k[2]) for k in win) + p["slBuf"]            # SL above the highest high of those candles
            risk = sl - c
            o = float(closed[i][1]); rng = h - l
            wick = (h - max(o, c)) / rng if rng else 0.0      # upper rejection wick share
            if _clear(p, wick, risk):
                out.append({"side": "SELL", "i": i, "t": t, "close_ms": float(closed[i][6]), "liq": last_high,
                        "entry": c, "et2": sl - risk * p["et2Pct"], "sl": sl,
                        "tp1": c - risk * p["rr1"], "tp2": c - risk * p["rr2"], "risk": risk, "n": len(win), "wick": wick})
        if not low_swept and last_low is not None and l < last_low and c > last_low:
            low_swept = True
            win = closed[max(0, i - p["slLookback"] + 1): i + 1]      # count candles backwards
            sl = min(float(k[3]) for k in win) - p["slBuf"]            # SL below the lowest low of those candles
            risk = c - sl
            o = float(closed[i][1]); rng = h - l
            wick = (min(o, c) - l) / rng if rng else 0.0      # lower rejection wick share
            if _clear(p, wick, risk):
                out.append({"side": "BUY", "i": i, "t": t, "close_ms": float(closed[i][6]), "liq": last_low,
                        "entry": c, "et2": sl + risk * p["et2Pct"], "sl": sl,
                        "tp1": c + risk * p["rr1"], "tp2": c + risk * p["rr2"], "risk": risk, "n": len(win), "wick": wick})
    return out


def format_signal(sym: str, tf: str, s: dict, price: float) -> str:
    name = NAMES.get(sym, sym)
    sell = s["side"] == "SELL"
    head = "🔴 SELL" if sell else "🟢 BUY"
    key = "BSL $$$ 💵" if sell else "Key 🔑"
    ict = time.strftime("%H:%M:%S", time.gmtime(time.time() + 7 * 3600))
    e = s["entry"]

    def dist(x):                       # distance from entry: $ and pips (1 pip = $0.10 on gold)
        d = abs(x - e)
        return f"{d:.2f}$ · {d * 10:.0f} pips"

    rr1 = abs(s["tp1"] - e) / s["risk"] if s["risk"] else 0
    rr2 = abs(s["tp2"] - e) / s["risk"] if s["risk"] else 0
    return (
        f"{head} · <b>{name}</b> · {tf}\n"
        f"━━━━━━━━━━━━━━\n"
        f"📍 Entry 1: <code>{e:.2f}</code>\n"
        f"📍 Entry 2: <code>{s['et2']:.2f}</code>\n"
        f"🛑 SL: <code>{s['sl']:.2f}</code>  (−{dist(s['sl'])})\n"
        f"🎯 TP 1: <code>{s['tp1']:.2f}</code>  (+{dist(s['tp1'])} · RR 1:{rr1:g})\n"
        f"🎯 TP 2: <code>{s['tp2']:.2f}</code>  (+{dist(s['tp2'])} · RR 1:{rr2:g})\n"
        f"━━━━━━━━━━━━━━\n"
        f"{key} <code>{s['liq']:.2f}</code> (liquidity swept)\n"
        f"✅ Rejection wick {s.get('wick', 0) * 100:.0f}% · SL counted back {s.get('n', 1)} candle(s)\n"
        f"💰 Price: <code>{price:.2f}</code>\n"
        f"⏰ {ict} (ICT) · Gold Fx Signal"
    )


_CHAT_MAP: dict = {}      # old group id -> supergroup id (Telegram migrates groups when upgraded)


def send(token: str, chat_id: str, text: str, thread_id: str | None = None, _retry: bool = True):
    """Returns (ok, error_message). chat_id = @channel or -100xxxxxxxxxx (group / supergroup / channel)."""
    chat_id = str(chat_id).strip()
    chat_id = _CHAT_MAP.get(chat_id, chat_id)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if thread_id:
        payload["message_thread_id"] = str(thread_id).strip()      # forum topic inside a group
    data = urllib.parse.urlencode(payload).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
            j = json.loads(r.read().decode())
            return bool(j.get("ok")), "" if j.get("ok") else str(j)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {}
        desc = body.get("description", "")
        params = body.get("parameters") or {}
        mig = params.get("migrate_to_chat_id")
        if mig and _retry:                                           # group was upgraded to supergroup
            _CHAT_MAP[chat_id] = str(mig)
            return send(token, str(mig), text, thread_id, False)
        wait = params.get("retry_after")
        if e.code == 429 and wait and _retry and int(wait) <= 20:    # flood control
            time.sleep(int(wait) + 1)
            return send(token, chat_id, text, thread_id, False)
        hint = ""
        if e.code in (400, 403):
            hint = " → Add the bot to the group/channel as ADMIN (allow Post messages) and use the -100… chat id"
        return False, f"Telegram {e.code}: {desc}{hint}"
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


_sent_path = None


def _load_sent() -> dict:
    """Remember which signals were already handled so a restart never re-sends or loses them."""
    try:
        d = json.loads(Path(_sent_path).read_text(encoding="utf-8")) if _sent_path else {}
        out = {}
        for key, v in d.items():
            sym, tf = key.split("|", 1)
            out[(sym, tf)] = {(a, int(t)) for a, t in v}
        return out
    except Exception:
        return {}


def _save_sent(seen: dict):
    if not _sent_path:
        return
    try:
        d = {f"{k[0]}|{k[1]}": sorted(v, key=lambda x: x[1])[-200:] for k, v in seen.items()}
        tmp = str(_sent_path) + ".tmp"
        Path(tmp).write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, _sent_path)
    except Exception:
        pass


def _ict(ms: float) -> str:
    return time.strftime("%d %b %H:%M", time.gmtime(ms / 1000 + 7 * 3600))


def send_latest(settings: dict) -> dict:
    """Admin tool: send the most recent signal of every symbol/timeframe to the group right now
    (ignores age) and report what was found, so it can be compared with TradingView."""
    s = settings or {}
    tok = str(s.get("TG_BOT_TOKEN") or "").strip()
    ch = str(s.get("SIGNAL_CHANNEL_ID") or "").strip()
    thread = str(s.get("SIGNAL_THREAD_ID") or "").strip() or None
    if not (tok and ch):
        return {"ok": False, "error": "Bot Token / Channel ID missing", "found": [], "sent": 0}
    _, _, _, syms, tfs = _cfg(lambda: s)
    params = load_params(s)
    found, sent, err = [], 0, ""
    for sym in syms:
        for tf in tfs:
            try:
                kl = market(f"klines?symbol={sym}&interval={tf}&limit={KL_LIMIT}")
                sigs = find_signals(kl, params)
                price = float(kl[-1][4])
                if not sigs:
                    found.append(f"{sym} {tf}: no signal in last {len(kl)} candles")
                    continue
                last = sigs[-1]
                found.append(f"{sym} {tf}: {last['side']} @ {last['entry']:.2f} · candle {_ict(last['t'])} ICT")
                ok, e = send(tok, ch, format_signal(sym, tf, last, price), thread)
                if ok:
                    sent += 1
                else:
                    err = e
            except Exception as ex:
                err = f"{sym} {tf}: {ex}"
    return {"ok": not err, "error": err, "found": found, "sent": sent}


def _loop(get_settings):
    last_kl = {}
    seen = _load_sent()
    STATE["running"] = True
    while True:
        try:
            tok, ch, on, syms, tfs = _cfg(get_settings)
            st = get_settings() or {}
            params = load_params(st)
            thread = str(st.get("SIGNAL_THREAD_ID") or "").strip() or None
            try:
                max_age = max(1, min(20, int(float(st.get("SIGNAL_MAX_AGE_BARS") or MAX_AGE_BARS))))
            except Exception:
                max_age = MAX_AGE_BARS
        except Exception as e:
            STATE["last_error"] = f"settings: {e}"
            time.sleep(POLL_SEC)
            continue
        if not (tok and ch and on):
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
                    if sigs:
                        l = sigs[-1]
                        STATE.setdefault("last_found", {})[f"{sym} {tf}"] = f"{l['side']} @ {l['entry']:.2f} · candle {_ict(l['t'])} ICT"
                    seen.setdefault(k, set())
                    age = TF_SEC.get(tf, 900) * max_age * 1000
                    changed = False
                    for s in sigs:
                        sk = (s["side"], s["t"])
                        if sk in seen[k]:
                            continue
                        if now * 1000 - s["close_ms"] > age:      # too old → just remember it
                            seen[k].add(sk)
                            changed = True
                            continue
                        ok, err = send(tok, ch, format_signal(sym, tf, s, price), thread)
                        if ok:
                            seen[k].add(sk)
                            changed = True
                            STATE["sent_count"] += 1
                            STATE["last_sent"] = f"{s['side']} {NAMES.get(sym, sym)} {tf} @ {s['entry']:.2f}"
                            STATE["last_error"] = ""
                            print(f"[signal] sent {STATE['last_sent']}", flush=True)
                        else:
                            print(f"[signal] SEND FAILED {sym} {tf} {s['side']}: {err}", flush=True)
                            STATE["last_error"] = err           # not marked → retried next poll while still fresh
                    if changed:
                        _save_sent(seen)
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
    global _sent_path
    _sent_path = str(Path(lock_path).with_name("signal_sent.json"))
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
