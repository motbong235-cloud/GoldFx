"""Gold Fx signal engine: real level-break signals -> Telegram channel.

Runs as a background thread inside server.py. Reads settings from db.json
(admin panel), so nothing needs to be configured in env.
Settings used: TG_BOT_TOKEN, SIGNAL_CHANNEL_ID, SIGNAL_ENABLED ("0" = off),
               SIGNAL_TIMEFRAMES (default "15m"), SIGNAL_SYMBOLS (default "PAXGUSDT,BTCUSDT")
Standalone: BOT_TOKEN / CHANNEL_ID env vars.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# data-api.binance.vision = public market-data mirror (not geo-blocked like api.binance.com)
BASES = [
    "https://data-api.binance.vision/api/v3/",
    "https://api.binance.com/api/v3/",
    "https://api1.binance.com/api/v3/",
    "https://api2.binance.com/api/v3/",
]
_good = 0
LEN, RNG = 50, 1.0            # same as Pine indicator + dashboard
COOLDOWN = 5 * 60             # sec per symbol/tf/level/direction
LEVEL_REFRESH = 30
POLL_SEC = 5
NAMES = {"PAXGUSDT": "XAUUSD", "BTCUSDT": "BTCUSDT"}

STATE = {"running": False, "last_sent": "", "last_error": "", "sent_count": 0}
_lock_fh = None


def market(path: str, timeout: int = 6):
    """GET a Binance market-data path (e.g. 'klines?symbol=..') trying mirrors in turn."""
    global _good
    last = None
    for n in range(len(BASES)):
        i = (_good + n) % len(BASES)
        try:
            req = urllib.request.Request(BASES[i] + path, headers={"User-Agent": "goldfx-signal"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
            _good = i
            return data
        except Exception as e:
            last = e
    raise last


def compute_levels(kl):
    w = kl[-LEN:]
    mean = sum(float(k[4]) for k in w) / len(w)
    hi = max(float(k[2]) for k in w)
    lo = min(float(k[3]) for k in w)
    r = (hi - lo) * RNG
    return {
        "MEAN": mean,
        "UP1": mean + r * .25, "UP2": mean + r * .50, "UP3": mean + r * .75,
        "DN1": mean - r * .25, "DN2": mean - r * .50, "DN3": mean - r * .75,
    }


def send(token: str, chat_id: str, text: str):
    """Returns (ok, error_message)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
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
    syms = [x.strip().upper() for x in str(s.get("SIGNAL_SYMBOLS") or "PAXGUSDT,BTCUSDT").split(",") if x.strip()]
    tfs = [x.strip() for x in str(s.get("SIGNAL_TIMEFRAMES") or "15m").split(",") if x.strip()]
    return tok, ch, on, syms, tfs


def _loop(get_settings):
    levels, last_lvl, prev, fired = {}, {}, {}, {}
    STATE["running"] = True
    while True:
        try:
            tok, ch, on, syms, tfs = _cfg(get_settings)
        except Exception as e:
            STATE["last_error"] = f"settings: {e}"
            time.sleep(POLL_SEC)
            continue
        if not (tok and ch and on):
            prev.clear()           # avoid a fake "cross" when re-enabled
            time.sleep(POLL_SEC)
            continue
        now = time.time()
        for sym in syms:
            try:
                for tf in tfs:
                    k = (sym, tf)
                    if now - last_lvl.get(k, 0) >= LEVEL_REFRESH:
                        kl = market(f"klines?symbol={sym}&interval={tf}&limit={LEN + 5}")
                        levels[k] = compute_levels(kl)
                        last_lvl[k] = now
                price = float(market(f"ticker/price?symbol={sym}")["price"])
                p0, prev[sym] = prev.get(sym), price
                if p0 is None:
                    continue
                for tf in tfs:
                    for tag, lv in levels.get((sym, tf), {}).items():
                        up, dn = p0 < lv <= price, p0 > lv >= price
                        if not (up or dn):
                            continue
                        fk = (sym, tf, tag, "u" if up else "d")
                        if now - fired.get(fk, 0) < COOLDOWN:
                            continue
                        fired[fk] = now
                        head = "🟢 ▲ Break" if up else "🔴 ▼ Break"
                        msg = (f"{head} <b>{tag}</b>\n<b>{NAMES.get(sym, sym)}</b> · {tf}\n"
                               f"Level: <code>{lv:.2f}</code>\nPrice: <code>{price:.2f}</code>\n"
                               f"⏰ {time.strftime('%H:%M:%S', time.gmtime(now + 7 * 3600))} (ICT)")
                        ok, err = send(tok, ch, msg)
                        if ok:
                            STATE["sent_count"] += 1
                            STATE["last_sent"] = f"{tag} {NAMES.get(sym, sym)} {tf} @ {price:.2f}"
                            STATE["last_error"] = ""
                        else:
                            STATE["last_error"] = err
            except Exception as e:
                STATE["last_error"] = f"{sym}: {e}"
        time.sleep(POLL_SEC)


def _acquire_lock(path):
    """One engine per machine even with several gunicorn workers / reloader."""
    global _lock_fh
    try:
        import fcntl
        _lock_fh = open(path, "w")
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except ImportError:
        return True                # Windows: no lock, assume single process
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
    _loop(lambda: {"TG_BOT_TOKEN": t, "SIGNAL_CHANNEL_ID": c,
                   "SIGNAL_SYMBOLS": os.environ.get("SYMBOLS", ""),
                   "SIGNAL_TIMEFRAMES": os.environ.get("TIMEFRAMES", "")})
