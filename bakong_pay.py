"""Gold Fx — Official Bakong (NBC) KHQR payment.

Uses National Bank of Cambodia Open API:
  https://api-bakong.nbc.gov.kh/v1

Flow:
  1. Build dynamic KHQR EMVCo string (local)
  2. MD5(qr) as transaction id
  3. Show QR image to user
  4. Poll POST /v1/check_transaction_by_md5  (Bearer token)
  5. responseCode 0 → PAID → activate Pro

Register token: https://api-bakong.nbc.gov.kh/register
Note: Official API often requires a Cambodia IP. For global hosting,
      Bakong Relay token (rbk_...) via bakong-khqr package also works.
"""
from __future__ import annotations

import hashlib
import time
import io
import json
import secrets
import string
import urllib.error
import urllib.request
from typing import Any

NBC_API = "https://api-bakong.nbc.gov.kh/v1"
TIMEOUT = 15

# EMVCo CRC-16/CCITT-FALSE
def _crc16(data: str) -> str:
    crc = 0xFFFF
    for ch in data.encode("utf-8"):
        crc ^= ch << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return f"{crc:04X}"


def _tlv(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


def make_verify_key() -> str:
    """10-char id kept for order bookkeeping (not sent to NBC)."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(10))


def create_khqr(
    *,
    bakong_id: str,
    merchant_name: str,
    amount: float,
    currency: str = "USD",
    merchant_city: str = "Phnom Penh",
    bill_number: str = "",
    store_label: str = "Gold Fx",
    expire_minutes: int = 10,
) -> str:
    """Dynamic KHQR (Individual Tag 29) per NBC KHQR Content Guideline.

    Tag order matches official SDKs:
      00 · 01 · 29 · 52 · 53 · 54 · 58 · 59 · 60 · [62] · 99 · 63
    Tag 99 (creation+expiry ms, 13 digits each) is required when amount > 0.
    CRC-16/CCITT-FALSE over payload including literal "6304".
    """
    bakong_id = (bakong_id or "").strip()
    merchant_name = (merchant_name or "Gold Fx").strip()[:25]
    # Many banking apps expect city without excessive spaces; keep title case
    merchant_city = (merchant_city or "Phnom Penh").strip()[:15]
    currency = (currency or "USD").upper()
    if currency not in ("USD", "KHR"):
        currency = "USD"
    if not bakong_id or "@" not in bakong_id:
        raise ValueError("Bakong ID ត្រូវទម្រង់ name@bank (ឧ. name@aba)")

    # Tag 29 Individual — only sub-tag 00 = Bakong Account ID
    mai = _tlv("00", bakong_id)

    parts = []
    parts.append(_tlv("00", "01"))                 # Payload Format Indicator
    parts.append(_tlv("01", "12"))                 # Dynamic
    parts.append(_tlv("29", mai))                  # Merchant Account Information
    parts.append(_tlv("52", "5999"))               # MCC
    parts.append(_tlv("53", "840" if currency == "USD" else "116"))
    if currency == "USD":
        # Keep two decimals (official USD rule)
        amt_s = f"{float(amount):.2f}"
    else:
        amt_s = str(int(round(float(amount))))
    parts.append(_tlv("54", amt_s))
    parts.append(_tlv("58", "KH"))
    parts.append(_tlv("59", merchant_name))
    parts.append(_tlv("60", merchant_city))

    add = ""
    bn = str(bill_number or "").strip()[:25]
    if bn:
        add += _tlv("01", bn)
    sl = str(store_label or "").strip()[:25]
    if sl:
        add += _tlv("03", sl)
    if add:
        parts.append(_tlv("62", add))

    # Tag 99 — timestamps MUST be 13-digit millisecond strings
    now_ms = int(time.time() * 1000)
    exp_ms = now_ms + max(1, int(expire_minutes)) * 60 * 1000
    ts00 = f"{now_ms:013d}"[-13:]   # ensure exactly 13 digits
    ts01 = f"{exp_ms:013d}"[-13:]
    ts = _tlv("00", ts00) + _tlv("01", ts01)
    parts.append(_tlv("99", ts))

    payload = "".join(parts)
    # CRC over entire string including the "6304" introducer
    payload_with_tag = payload + "6304"
    crc = _crc16(payload_with_tag)
    return payload_with_tag + crc



def generate_md5(qr: str) -> str:
    return hashlib.md5(qr.encode("utf-8")).hexdigest()


def qr_to_data_uri(qr: str, size: int = 280) -> str:
    """PNG data-URI. Prefer qrcode lib; fallback to public chart API."""
    try:
        import qrcode

        img = qrcode.make(qr, border=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        import base64

        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception:
        pass
    # Fallback: Google Chart QR (works without extra deps)
    from urllib.parse import quote

    url = (
        "https://api.qrserver.com/v1/create-qr-code/"
        f"?size={size}x{size}&margin=8&data={quote(qr)}"
    )
    return url


def check_transaction(*, token: str, md5: str) -> dict[str, Any]:
    """POST /v1/check_transaction_by_md5 — responseCode 0 = paid."""
    token = (token or "").strip()
    md5 = (md5 or "").strip()
    if not token:
        return {"success": False, "status": "error", "error": "missing BAKONG_TOKEN"}
    if not md5:
        return {"success": False, "status": "error", "error": "missing md5"}

    body = json.dumps({"md5": md5}).encode("utf-8")
    req = urllib.request.Request(
        f"{NBC_API}/check_transaction_by_md5",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "GoldFx/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"success": False, "status": "error", "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"success": False, "status": "error", "error": str(e)[:200]}

    code = data.get("responseCode")
    # 0 = found/paid success
    if code == 0 or code == "0":
        return {
            "success": True,
            "status": "paid",
            "payment_status": "completed",
            "data": data.get("data"),
            "raw": data,
        }
    # 1 = not found / unpaid (common while waiting)
    err = data.get("responseMessage") or data.get("errorCode") or "UNPAID"
    return {
        "success": True,
        "status": "pending",
        "payment_status": "pending",
        "error": str(err),
        "raw": data,
    }


def generate(
    *,
    token: str,
    bakong_id: str,
    amount: float,
    merchant_name: str = "Gold Fx",
    merchant_city: str = "Phnom Penh",
    bill_number: str = "",
    currency: str = "USD",
) -> dict[str, Any]:
    """Create KHQR + md5 + image URL/data-uri for display."""
    bakong_id = (bakong_id or "").strip()
    token = (token or "").strip()
    if not bakong_id:
        return {"success": False, "error": "ដាក់ Bakong ID (ឧ. name@aba)"}
    if not token:
        return {"success": False, "error": "ដាក់ Bakong Developer Token"}

    bill = bill_number or ("GF" + secrets.token_hex(4).upper())
    try:
        # Prefer bakong-khqr package if installed (handles NBC + Relay tokens)
        try:
            from bakong_khqr import KHQR

            khqr = KHQR(token)
            # Newer API uses account_id; older uses bank_account
            try:
                qr = khqr.create_qr(
                    account_id=bakong_id,
                    merchant_name=merchant_name[:25],
                    merchant_city=merchant_city[:15],
                    amount=float(amount),
                    currency=currency,
                    store_label="Gold Fx",
                    bill_number=bill,
                    static=False,
                )
            except TypeError:
                qr = khqr.create_qr(
                    bank_account=bakong_id,
                    merchant_name=merchant_name[:25],
                    merchant_city=merchant_city[:15],
                    amount=float(amount),
                    currency=currency,
                    store_label="Gold Fx",
                    bill_number=bill,
                    static=False,
                )
            qr = str(qr)
            md5 = khqr.generate_md5(qr) if hasattr(khqr, "generate_md5") else generate_md5(qr)
        except ImportError:
            qr = create_khqr(
                bakong_id=bakong_id,
                merchant_name=merchant_name,
                amount=amount,
                currency=currency,
                merchant_city=merchant_city,
                bill_number=bill,
            )
            md5 = generate_md5(qr)

        img = qr_to_data_uri(qr)
        return {
            "success": True,
            "qr": img,
            "qr_image_url": img,
            "qr_string": qr,
            "md5": md5,
            "bill_number": bill,
            "amount": float(amount),
            "currency": currency,
            "status": "pending",
        }
    except Exception as e:
        return {"success": False, "error": str(e)[:300]}


def check(*, token: str, md5: str) -> dict[str, Any]:
    return check_transaction(token=token, md5=md5)


def confirm(*, token: str = "", md5: str = "", **_kwargs) -> dict[str, Any]:
    """NBC has no separate confirm — payment is final when PAID."""
    return {"success": True, "status": "credit_confirmed"}
