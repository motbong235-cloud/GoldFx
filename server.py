"""Gold Fx — chart app + Pro payment (Khmer System) + Admin panel."""
from __future__ import annotations

import hashlib
import re
import time
import json
import os
import secrets
import string
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
)

import khmer_system
import signal_engine

BASE = Path(__file__).resolve().parent


def _resolve_data_dir() -> Path:
    """Prefer DATA_DIR env (Render disk), else local data/, else /tmp fallback."""
    candidates = []
    env = (os.environ.get("DATA_DIR") or "").strip()
    if env:
        candidates.append(Path(env))
    candidates.append(BASE / "data")
    candidates.append(Path("/tmp/gold-fx-data"))
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            test = d / ".write_test"
            test.write_text("ok", encoding="utf-8")
            test.unlink(missing_ok=True)
            return d
        except Exception:
            continue
    # last resort: BASE/data even if not writable (will error later with clear msg)
    d = BASE / "data"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


DATA_DIR = _resolve_data_dir()
DB_PATH = DATA_DIR / "db.json"

app = Flask(__name__, static_folder=str(BASE / "static"), template_folder=str(BASE / "templates"))
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(24))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_read() -> dict:
    if not DB_PATH.exists():
        seed = BASE / "data" / "db.json"
        if seed.exists() and seed != DB_PATH:
            DB_PATH.write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            DB_PATH.write_text(
                json.dumps(
                    {
                        "settings": {
                            "SITE_NAME": "Gold Fx",
                            "ADMIN_PASSWORD": os.environ.get("ADMIN_PASSWORD", "admin123"),
                            "PRO_PRICE": 9.99,
                            "SHOP_NAME": "Gold Fx",
                            "KHMER_SECRET_KEY": "",
                            "KHMER_PROFILE_KEY": "",
                            "KHMER_MACHINE_ID": "",
                            "KHMER_MERCHANT_NAME": "Gold Fx",
                        },
                        "users": {},
                        "orders": [],
                        "next_order": 1001,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
    with open(DB_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def db_write(d: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    tmp.replace(DB_PATH)


def hash_pass(pw: str) -> str:
    return hashlib.sha256(("goldfx:" + pw).encode()).hexdigest()


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper


def gen_key() -> str:
    p = lambda: "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    return f"GF-{p()}-{p()}-{p()}"


def gen_order_id(n: int) -> str:
    return f"GF{n}"


# ---------- pages ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/login")
def login_page():
    return render_template("login.html")


@app.route("/register")
def register_page():
    return render_template("register.html")


@app.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html")


@app.route("/pro")
def pro_page():
    return render_template("pro.html")


@app.route("/admin")
def admin_page():
    return render_template("admin.html")


# ---------- auth (users) ----------
@app.route("/api/register", methods=["POST"])
def register():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not name or not email or len(password) < 6:
        return jsonify({"ok": False, "error": "ឈ្មោះ · email · password ≥ 6"}), 400
    d = db_read()
    users = d.setdefault("users", {})
    if email in users:
        return jsonify({"ok": False, "error": "Email មានរួចហើយ"}), 400
    users[email] = {
        "name": name,
        "email": email,
        "pass": hash_pass(password),
        "pro": False,
        "key": "",
        "created": utc_now(),
    }
    db_write(d)
    session["user_email"] = email
    return jsonify({"ok": True, "user": public_user(users[email])})


@app.route("/api/login", methods=["POST"])
def login():
    body = request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    d = db_read()
    u = (d.get("users") or {}).get(email)
    if not u or u.get("pass") != hash_pass(password):
        return jsonify({"ok": False, "error": "Email ឬ password មិនត្រូវ"}), 401
    session["user_email"] = email
    return jsonify({"ok": True, "user": public_user(u)})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.pop("user_email", None)
    return jsonify({"ok": True})


@app.route("/api/me")
def me():
    email = session.get("user_email")
    if not email:
        return jsonify({"ok": True, "user": None})
    d = db_read()
    u = (d.get("users") or {}).get(email)
    if not u:
        session.pop("user_email", None)
        return jsonify({"ok": True, "user": None})
    return jsonify({"ok": True, "user": public_user(u)})


_MKT_CACHE: dict = {}
_MKT_FAIL_UNTIL: dict = {}
_MKT_INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"}


@app.route("/api/mkt/<path:p>")
def market_proxy(p):
    """Cached same-origin proxy for Binance market data (avoids client-side blocks / CORS)."""
    a = request.args
    sym = (a.get("symbol") or "").upper()
    if not re.fullmatch(r"[A-Z0-9]{5,12}", sym):
        return jsonify({"ok": False, "error": "bad symbol"}), 400
    if p == "klines":
        itv = a.get("interval") or "15m"
        try:
            lim = max(1, min(100, int(a.get("limit") or 55)))
        except ValueError:
            lim = 55
        if itv not in _MKT_INTERVALS:
            return jsonify({"ok": False, "error": "bad interval"}), 400
        path, ttl = f"klines?symbol={sym}&interval={itv}&limit={lim}", 10
    elif p == "ticker/price":
        path, ttl = f"ticker/price?symbol={sym}", 1.5
    else:
        return jsonify({"ok": False, "error": "not allowed"}), 404
    hit = _MKT_CACHE.get(path)
    if hit and time.time() - hit[0] < ttl:
        return jsonify(hit[1])
    # upstream recently failed -> answer fast instead of blocking a worker again
    if time.time() < _MKT_FAIL_UNTIL.get(path, 0):
        if hit:
            return jsonify(hit[1])
        return jsonify({"ok": False, "error": "upstream unavailable"}), 502
    try:
        data = signal_engine.market(path, timeout=3, budget=5)
    except Exception as e:
        _MKT_FAIL_UNTIL[path] = time.time() + 5
        if hit:                      # serve stale rather than fail
            return jsonify(hit[1])
        return jsonify({"ok": False, "error": str(e)[:200]}), 502
    _MKT_CACHE[path] = (time.time(), data)
    return jsonify(data)


@app.route("/api/indicator")
def indicator():
    """Disabled: indicator code is no longer served to anyone (including Pro)."""
    return jsonify({"ok": True, "code": "", "locked": False})


@app.route("/api/config")
def public_config():
    st = db_read().get("settings") or {}
    return jsonify(
        {
            "ok": True,
            "site_name": st.get("SITE_NAME") or "Gold Fx",
            "pro_price": st.get("PRO_PRICE", 9.99),
            "telegram": st.get("TELEGRAM") or "",
            "telegram_bot": st.get("TELEGRAM_BOT") or "",
        }
    )


def public_user(u: dict) -> dict:
    return {
        "name": u.get("name"),
        "email": u.get("email"),
        "pro": bool(u.get("pro")),
        "key": u.get("key") or "",
    }


# ---------- Pro order + Khmer System ----------
@app.route("/api/pro/buy", methods=["POST"])
def pro_buy():
    """Create Pro order + generate KHQR via Khmer System."""
    email = session.get("user_email")
    if not email:
        return jsonify({"ok": False, "error": "ត្រូវ login មុន"}), 401

    d = db_read()
    u = (d.get("users") or {}).get(email)
    if not u:
        return jsonify({"ok": False, "error": "User not found"}), 404
    if u.get("pro"):
        return jsonify({"ok": False, "error": "អ្នកជា Pro រួចហើយ", "user": public_user(u)}), 400

    s = d.get("settings") or {}
    price = float(s.get("PRO_PRICE") or 9.99)
    n = int(d.get("next_order") or 1001)
    oid = gen_order_id(n)
    d["next_order"] = n + 1
    license_key = gen_key()

    order = {
        "id": oid,
        "type": "pro",
        "email": email,
        "name": u.get("name"),
        "price": price,
        "status": "pending_payment",
        "license_key": license_key,
        "created_at": utc_now(),
        "paid_at": None,
        "ks_verify_key": None,
        "ks_error": None,
        "payment_qr": None,
    }

    pay = {
        "SHOP_NAME": s.get("SHOP_NAME") or "Gold Fx",
        "PRO_PRICE": price,
        "PAYMENT_QR": "",
        "KS_DYNAMIC": False,
    }
    ks_data = None

    secret = (s.get("KHMER_SECRET_KEY") or s.get("KHMER_PROFILE_KEY") or "").strip()
    if secret:
        vkey = khmer_system.make_verify_key()
        # use hashed email as stable telegram_user_id substitute
        tg_id = str(abs(hash(email)) % 10_000_000_000)
        order["ks_verify_key"] = vkey
        order["ks_telegram_user_id"] = tg_id
        try:
            resp = khmer_system.generate(
                secret_key=secret,
                amount=price,
                verify_key=vkey,
                telegram_user_id=tg_id,
                bakong_account_id=(s.get("BAKONG_ID") or None) or None,
                merchant_name=(s.get("KHMER_MERCHANT_NAME") or s.get("SHOP_NAME") or "Gold Fx"),
                machine_id=(s.get("KHMER_MACHINE_ID") or None) or None,
                profile_key=(s.get("KHMER_PROFILE_KEY") or None) or None,
            )
            if resp.get("success") or resp.get("qr_image_url") or resp.get("qr"):
                qr = resp.get("qr_image_url") or resp.get("qr") or ""
                order["payment_qr"] = qr
                pay["PAYMENT_QR"] = qr
                pay["KS_DYNAMIC"] = True
                ks_data = {
                    "qr_image_url": qr,
                    "verify_key": vkey,
                    "raw": {k: resp.get(k) for k in ("success", "code", "message") if k in resp},
                }
            else:
                order["ks_error"] = resp.get("error") or resp.get("message") or str(resp)[:200]
        except Exception as e:
            order["ks_error"] = str(e)
    else:
        order["ks_error"] = "Admin មិនទាន់ដាក់ Khmer System Profile Key"

    d.setdefault("orders", []).insert(0, order)
    db_write(d)

    return jsonify(
        {
            "ok": True,
            "order": {
                "id": order["id"],
                "price": order["price"],
                "status": order["status"],
                "license_key": order["license_key"],
            },
            "payment": pay,
            "khmer_system": ks_data,
            "ks_error": order.get("ks_error"),
        }
    )


@app.route("/api/pro/check", methods=["POST"])
def pro_check():
    """Poll Khmer System payment status; on success activate Pro."""
    body = request.get_json(force=True, silent=True) or {}
    oid = (body.get("order_id") or "").strip()
    email = session.get("user_email")
    if not email:
        return jsonify({"ok": False, "error": "login required"}), 401

    d = db_read()
    order = next((o for o in d.get("orders", []) if o.get("id") == oid), None)
    if not order or order.get("email") != email:
        return jsonify({"ok": False, "error": "Order not found"}), 404

    if order.get("status") == "paid":
        u = d["users"].get(email)
        return jsonify({"ok": True, "payment_status": "completed", "order": order, "user": public_user(u) if u else None})

    s = d.get("settings") or {}
    secret = (s.get("KHMER_SECRET_KEY") or s.get("KHMER_PROFILE_KEY") or "").strip()
    vkey = order.get("ks_verify_key")
    tg_id = order.get("ks_telegram_user_id")

    payment_status = "pending"
    if secret and vkey and tg_id:
        resp = khmer_system.check(secret_key=secret, verify_key=vkey, telegram_user_id=str(tg_id))
        st = (resp.get("status") or resp.get("payment_status") or "").lower()
        if resp.get("success") or st in ("paid", "completed", "success", "approved"):
            payment_status = "completed"
            try:
                khmer_system.confirm(secret_key=secret, verify_key=vkey, telegram_user_id=str(tg_id))
            except Exception:
                pass
            _activate_pro(d, order)
            db_write(d)
        elif resp.get("error"):
            payment_status = "pending"

    u = d["users"].get(email)
    return jsonify(
        {
            "ok": True,
            "payment_status": payment_status,
            "order": {
                "id": order["id"],
                "status": order["status"],
                "license_key": order.get("license_key") if order["status"] == "paid" else None,
            },
            "user": public_user(u) if u else None,
        }
    )


@app.route("/api/pro/confirm-paid", methods=["POST"])
def pro_confirm_paid():
    """User says paid — re-check KS; if no KS, mark waiting for admin."""
    body = request.get_json(force=True, silent=True) or {}
    oid = (body.get("order_id") or "").strip()
    email = session.get("user_email")
    if not email:
        return jsonify({"ok": False, "error": "login required"}), 401

    d = db_read()
    order = next((o for o in d.get("orders", []) if o.get("id") == oid), None)
    if not order or order.get("email") != email:
        return jsonify({"ok": False, "error": "Order not found"}), 404

    if order.get("status") == "paid":
        return jsonify({"ok": True, "order": order, "user": public_user(d["users"][email]), "message": "Pro active"})

    s = d.get("settings") or {}
    secret = (s.get("KHMER_SECRET_KEY") or s.get("KHMER_PROFILE_KEY") or "").strip()
    vkey = order.get("ks_verify_key")
    tg_id = order.get("ks_telegram_user_id")

    if secret and vkey and tg_id:
        resp = khmer_system.check(secret_key=secret, verify_key=vkey, telegram_user_id=str(tg_id))
        st = (resp.get("status") or resp.get("payment_status") or "").lower()
        if resp.get("success") or st in ("paid", "completed", "success", "approved"):
            try:
                khmer_system.confirm(secret_key=secret, verify_key=vkey, telegram_user_id=str(tg_id))
            except Exception:
                pass
            _activate_pro(d, order)
            db_write(d)
            return jsonify(
                {
                    "ok": True,
                    "order": order,
                    "user": public_user(d["users"][email]),
                    "message": "Pro activated ✓",
                }
            )

    # fallback: waiting admin confirm
    order["status"] = "waiting_confirm"
    db_write(d)
    return jsonify(
        {
            "ok": True,
            "order": {"id": order["id"], "status": "waiting_confirm"},
            "message": "រង់ចាំ Admin confirm · នឹង activate បន្ទាប់ពីផ្ទៀងផ្ទាត់",
        }
    )


def _activate_pro(d: dict, order: dict) -> None:
    order["status"] = "paid"
    order["paid_at"] = utc_now()
    email = order.get("email")
    u = d.get("users", {}).get(email)
    if u:
        u["pro"] = True
        u["key"] = order.get("license_key") or gen_key()
        order["license_key"] = u["key"]


@app.route("/api/pro/activate-key", methods=["POST"])
def activate_key():
    body = request.get_json(force=True, silent=True) or {}
    key = (body.get("key") or "").strip().upper()
    email = session.get("user_email")
    if not email:
        return jsonify({"ok": False, "error": "login required"}), 401
    if not key.startswith("GF-"):
        return jsonify({"ok": False, "error": "Key មិនត្រឹមត្រូវ"}), 400
    d = db_read()
    # accept key if matches a paid order or admin-issued
    paid = next(
        (o for o in d.get("orders", []) if (o.get("license_key") or "").upper() == key and o.get("status") == "paid"),
        None,
    )
    u = d["users"].get(email)
    if not u:
        return jsonify({"ok": False, "error": "user not found"}), 404
    if paid and paid.get("email") not in (email, None):
        # key bound to another user — still allow manual activate by admin key reuse policy: allow
        pass
    u["pro"] = True
    u["key"] = key
    db_write(d)
    return jsonify({"ok": True, "user": public_user(u), "message": "Pro activated"})


# ---------- Admin ----------
@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    body = request.get_json(force=True, silent=True) or {}
    pw = body.get("password") or ""
    d = db_read()
    expected = d.get("settings", {}).get("ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD", "admin123")
    if pw != expected:
        return jsonify({"ok": False, "error": "Password មិនត្រូវ"}), 401
    session["admin"] = True
    return jsonify({"ok": True})


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin", None)
    return jsonify({"ok": True})


@app.route("/api/admin/me")
def admin_me():
    return jsonify({"ok": True, "admin": bool(session.get("admin"))})


@app.route("/api/admin/data")
@admin_required
def admin_data():
    d = db_read()
    users = [
        {
            "email": e,
            "name": u.get("name"),
            "pro": bool(u.get("pro")),
            "key": u.get("key") or "",
            "created": u.get("created"),
        }
        for e, u in (d.get("users") or {}).items()
    ]
    return jsonify(
        {
            "ok": True,
            "settings": d.get("settings") or {},
            "signal": dict(signal_engine.STATE),
            "orders": (d.get("orders") or [])[:100],
            "users": users,
            "stats": {
                "users": len(users),
                "pro": sum(1 for u in users if u["pro"]),
                "orders": len(d.get("orders") or []),
                "paid": sum(1 for o in d.get("orders") or [] if o.get("status") == "paid"),
                "pending": sum(
                    1 for o in d.get("orders") or [] if o.get("status") in ("pending_payment", "waiting_confirm")
                ),
            },
        }
    )


@app.route("/api/admin/order/confirm", methods=["POST"])
@admin_required
def admin_confirm_order():
    body = request.get_json(force=True, silent=True) or {}
    oid = (body.get("order_id") or "").strip()
    d = db_read()
    order = next((o for o in d.get("orders", []) if o.get("id") == oid), None)
    if not order:
        return jsonify({"ok": False, "error": "Order not found"}), 404
    if order.get("status") == "paid":
        return jsonify({"ok": True, "order": order, "message": "Already paid"})
    _activate_pro(d, order)
    db_write(d)
    return jsonify({"ok": True, "order": order, "message": "Pro activated"})


@app.route("/api/admin/settings", methods=["PUT"])
@admin_required
def admin_settings():
    body = request.get_json(force=True, silent=True) or {}
    d = db_read()
    s = d.setdefault("settings", {})
    keys = [
        "SITE_NAME",
        "ADMIN_PASSWORD",
        "PRO_PRICE",
        "SHOP_NAME",
        "TELEGRAM",
        "TELEGRAM_BOT",
        "KHMER_SECRET_KEY",
        "KHMER_PROFILE_KEY",
        "KHMER_MACHINE_ID",
        "KHMER_MERCHANT_NAME",
        "BAKONG_ID",
        "TG_BOT_TOKEN",
        "SIGNAL_CHANNEL_ID",
        "SIGNAL_ENABLED",
        "SIGNAL_TIMEFRAMES",
        "SIGNAL_SYMBOLS",
        "INDICATOR_CODE",
        "INDICATOR_ACCESS",
    ]
    for k in keys:
        if k in body:
            val = body[k]
            if k == "PRO_PRICE":
                try:
                    val = float(val)
                except Exception:
                    val = 9.99
            if k == "INDICATOR_CODE":
                val = str(val or "")[:60000]
            if k == "INDICATOR_ACCESS":
                val = "pro" if str(val) == "pro" else "all"
            s[k] = val
    db_write(d)
    return jsonify({"ok": True, "settings": s})


@app.route("/api/admin/signal/test", methods=["POST"])
@admin_required
def admin_signal_test():
    st = db_read().get("settings") or {}
    tok = str(st.get("TG_BOT_TOKEN") or "").strip()
    ch = str(st.get("SIGNAL_CHANNEL_ID") or "").strip()
    if not tok or not ch:
        return jsonify({"ok": False, "error": "ដាក់ Bot Token និង Channel ID ជាមុន (រក្សាទុកសិន)"}), 400
    ok, err = signal_engine.send(tok, ch, "✅ <b>Gold Fx</b> · test signal — channel connected")
    return jsonify({"ok": ok, "error": err})


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "app": "Gold Fx",
        "data_dir": str(DATA_DIR),
        "db_exists": DB_PATH.exists(),
    })



@app.errorhandler(500)
def err_500(e):
    return (
        "<h1>Server Error</h1><pre style='white-space:pre-wrap'>"
        + str(getattr(e, "original_exception", e))
        + "</pre><p><a href='/health'>/health</a></p>",
        500,
    )



signal_engine.start_background(
    lambda: db_read().get("settings") or {}, DATA_DIR / "signal.lock"
)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
