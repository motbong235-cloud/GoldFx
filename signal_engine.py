"""Gold Fx signal engine: level-break → Telegram group with Entry / TP / SL.

Runs as a background thread inside server.py.
Settings (Admin → Bakong · Settings / Signal):
  TG_BOT_TOKEN, SIGNAL_CHANNEL_ID, SIGNAL_ENABLED ("0"=off),
  SIGNAL_TIMEFRAMES (default "5m,15m,1h"), SIGNAL_SYMBOLS (default "PAXGUSDT")
"""
from __future__ import annotations

import json
import os
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
LEN, RNG = 50, 1.0
COOLDOWN = 5 * 60
LEVEL_REFRESH = 180  # 3 min — levels stable enough to be crossed
POLL_SEC = 5
NAMES = {"PAXGUSDT": "XAUUSD", "XAUUSDT": "XAUUSD"}

STATE = {"running": False, "last_sent": "", "last_error": "", "sent_count": 0, "last_price": "", "last_check": "", "last_levels": ""}
_lock_fh = None

# Level ladder order (low → high)
LADDER = ("DN3", "DN2", "DN1", "MEAN", "UP1", "UP2", "UP3")


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


def compute_levels(kl):
    """Levels from closed candles only (exclude last forming bar) so levels don't chase price."""
    closed = kl[:-1] if len(kl) > LEN else kl
    w = closed[-LEN:] if len(closed) >= LEN else closed
    if len(w) < 10:
        w = kl[-LEN:]
    mean = sum(float(k[4]) for k in w) / len(w)
    hi = max(float(k[2]) for k in w)
    lo = min(float(k[3]) for k in w)
    r = (hi - lo) * RNG
    if r < 1e-9:
        r = mean * 0.001
    return {
        "MEAN": mean,
        "UP1": mean + r * 0.25,
        "UP2": mean + r * 0.50,
        "UP3": mean + r * 0.75,
        "DN1": mean - r * 0.25,
        "DN2": mean - r * 0.50,
        "DN3": mean - r * 0.75,
    }


def _targets(levels: dict, tag: str, up: bool):
    """Entry = broken level; TP1/TP2 next levels; SL opposite side."""
    entry = levels[tag]
    idx = LADDER.index(tag) if tag in LADDER else 3
    if up:
        tps = [levels[LADDER[i]] for i in range(idx + 1, min(idx + 3, len(LADDER)))]
        sl_i = max(0, idx - 1)
        sl = levels[LADDER[sl_i]]
        side = "BUY"
    else:
        tps = [levels[LADDER[i]] for i in range(idx - 1, max(idx - 3, -1), -1)]
        sl_i = min(len(LADDER) - 1, idx + 1)
        sl = levels[LADDER[sl_i]]
        side = "SELL"
    while len(tps) < 2:
        # fallback distance from range
        step = abs(levels["UP1"] - levels["MEAN"]) or (entry * 0.001)
        if up:
            tps.append(entry + step * (len(tps) + 1))
        else:
            tps.append(entry - step * (len(tps) + 1))
    return side, entry, tps[0], tps[1], sl


def format_signal(sym: str, tf: str, tag: str, up: bool, levels: dict, price: float) -> str:
    name = NAMES.get(sym, sym)
    side, entry, tp1, tp2, sl = _targets(levels, tag, up)
    if up:
        head = "🟢 ▲ BREAK"
        emoji = "📈"
    else:
        head = "🔴 ▼ BREAK"
        emoji = "📉"
    rr = abs(tp1 - entry) / abs(entry - sl) if abs(entry - sl) > 1e-9 else 0
    ict = time.strftime("%H:%M:%S", time.gmtime(time.time() + 7 * 3600))
    return (
        f"{head} <b>{tag}</b> {emoji}\n"
        f"<b>{name}</b> · {tf} · <b>{side}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"📍 Entry: <code>{entry:.2f}</code>\n"
        f"🎯 TP1: <code>{tp1:.2f}</code>\n"
        f"🎯 TP2: <code>{tp2:.2f}</code>\n"
        f"🛡 SL: <code>{sl:.2f}</code>\n"
        f"💰 Price: <code>{price:.2f}</code>\n"
        f"📊 R:R ≈ 1:{rr:.1f}\n"
        f"━━━━━━━━━━━━━━\n"
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
            prev.clear()
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
                        L = levels[k]
                        STATE["last_levels"] = (
                            f"{sym} {tf} MEAN={L['MEAN']:.2f} "
                            f"UP1={L['UP1']:.2f} DN1={L['DN1']:.2f}"
                        )
                # Same source as klines (PAXGUSDT) — do NOT blend spot APIs here
                price = float(market(f"ticker/price?symbol={sym}")["price"])
                p0 = prev.get(sym)
                prev[sym] = price
                STATE["last_price"] = f"{sym} {price:.2f}"
                STATE["last_check"] = time.strftime("%H:%M:%S", time.gmtime(now + 7 * 3600))
                if p0 is None:
                    continue
                for tf in tfs:
                    lvmap = levels.get((sym, tf), {})
                    if not lvmap:
                        continue
                    for tag, lv in lvmap.items():
                        up = p0 < lv <= price
                        dn = p0 > lv >= price
                        if not (up or dn):
                            continue
                        fk = (sym, tf, tag, "u" if up else "d")
                        if now - fired.get(fk, 0) < COOLDOWN:
                            continue
                        fired[fk] = now
                        msg = format_signal(sym, tf, tag, up, lvmap, price)
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
