"""Minimal Gmail reader + bank credit-alert parsers.

We talk to Gmail's REST API directly with httpx (no google-api-python-client, to
keep the serverless bundle small). Auth is an OAuth2 refresh token with the
read-only scope, minted once via get_gmail_token.py and stored in env.

Only the backend ever holds these credentials.

Flow:
  refresh_access_token() -> short-lived access token
  fetch_recent_alerts()  -> list of {amount_paise, ref, subject, snippet}
                            parsed from recent bank-alert emails

Bank parsers are pluggable via PARSERS[<name>]. Each takes the plain-text
content of one email and returns (amount_paise, ref) or None.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass

import httpx

from config import get_settings

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"


@dataclass
class CreditAlert:
    amount_paise: int
    ref: str
    subject: str
    snippet: str


# ── OAuth ───────────────────────────────────────────────────────
def refresh_access_token() -> str:
    settings = get_settings()
    if not (settings.gmail_client_id and settings.gmail_client_secret
            and settings.gmail_refresh_token):
        raise RuntimeError(
            "Gmail credentials missing: set GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, "
            "GMAIL_REFRESH_TOKEN."
        )
    resp = httpx.post(
        _TOKEN_URL,
        data={
            "client_id": settings.gmail_client_id,
            "client_secret": settings.gmail_client_secret,
            "refresh_token": settings.gmail_refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20.0,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ── Reading messages ────────────────────────────────────────────
def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _list_message_ids(token: str, query: str, max_results: int = 25) -> list[str]:
    resp = httpx.get(
        f"{_GMAIL_API}/messages",
        headers=_headers(token),
        params={"q": query, "maxResults": max_results},
        timeout=20.0,
    )
    resp.raise_for_status()
    return [m["id"] for m in resp.json().get("messages", [])]


def _get_message(token: str, msg_id: str) -> tuple[str, str, str, str]:
    """Return (from_addr, subject, snippet, plain_text_body) for a message."""
    resp = httpx.get(
        f"{_GMAIL_API}/messages/{msg_id}",
        headers=_headers(token),
        params={"format": "full"},
        timeout=20.0,
    )
    resp.raise_for_status()
    data = resp.json()
    snippet = data.get("snippet", "")
    subject = ""
    from_addr = ""
    for h in data.get("payload", {}).get("headers", []):
        name = h.get("name", "").lower()
        if name == "subject":
            subject = h.get("value", "")
        elif name == "from":
            from_addr = h.get("value", "")
    body = _extract_plain_text(data.get("payload", {}))
    # Fall back to the snippet if we couldn't decode a text part.
    return from_addr, subject, snippet, (body or snippet)


def _extract_plain_text(payload: dict) -> str:
    """Depth-first search for a text/plain part; decode base64url."""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    if mime == "text/plain" and body.get("data"):
        return _b64url(body["data"])
    for part in payload.get("parts", []) or []:
        text = _extract_plain_text(part)
        if text:
            return text
    # Some alerts are text/html only; decode and strip tags crudely.
    if mime == "text/html" and body.get("data"):
        html = _b64url(body["data"])
        return re.sub(r"<[^>]+>", " ", html)
    return ""


def _b64url(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode()).decode("utf-8", "ignore")
    except Exception:
        return ""


def fetch_recent_alerts() -> list[CreditAlert]:
    """Read recent bank-alert emails and parse them into CreditAlerts."""
    settings = get_settings()
    token = refresh_access_token()

    # Restrict to the alert label if configured, and to recent mail only.
    parts = ["newer_than:1d"]
    if settings.gmail_label:
        parts.append(f'label:{settings.gmail_label}')
    query = " ".join(parts)

    parser = PARSERS.get(settings.upi_bank_parser, parse_generic)
    trusted_sender = (settings.upi_alert_sender or "").lower()
    alerts: list[CreditAlert] = []
    for msg_id in _list_message_ids(token, query):
        from_addr, subject, snippet, text = _get_message(token, msg_id)
        # Only trust alerts from the configured bank sender (anti-spoofing).
        if trusted_sender and trusted_sender not in from_addr.lower():
            continue
        parsed = parser(f"{subject}\n{text}")
        if parsed:
            amount_paise, ref = parsed
            alerts.append(CreditAlert(amount_paise, ref, subject, snippet))
    return alerts


# ── Bank / wallet parsers ───────────────────────────────────────
# Each returns (amount_paise, ref) or None. Amounts like "Rs 49.37",
# "INR 49.37", "₹49.37". Refs are UPI/UTR numbers when present.

# Match "Rs.3000.00", "INR 1,49,000.00", "₹49.37" — allow any comma grouping
# (Indian lakh/crore style) and an optional 1-2 decimal part.
_AMOUNT_RE = re.compile(
    r"(?:rs|inr|₹)\.?\s*([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
# A UPI reference / UTR: at least 6 chars and mostly digits. We anchor on the
# label and then take the following mostly-numeric token, so we never grab the
# word "Reference" itself.
_REF_RE = re.compile(
    r"(?:UPI\s*Ref(?:erence)?\s*(?:no|id)?\.?\s*:?\s*|UTR\s*:?\s*|Ref(?:erence)?\s*no\.?\s*:?\s*)"
    r"(\d[\dA-Za-z]{5,})",
    re.IGNORECASE,
)


def _to_paise(amount_str: str) -> int:
    amount_str = amount_str.replace(",", "")
    return int(round(float(amount_str) * 100))


def _first_ref(text: str) -> str:
    m = _REF_RE.search(text)
    return m.group(1) if m else ""


def parse_generic(text: str) -> tuple[int, str] | None:
    """Credit-alert parser: needs a 'credited'/'received' cue + an amount."""
    low = text.lower()
    if not any(k in low for k in ("credited", "received", "added to", "money received")):
        return None
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    return _to_paise(m.group(1)), _first_ref(text)


def parse_paytm(text: str) -> tuple[int, str] | None:
    """Paytm 'received money' / 'credited to your Paytm' alerts."""
    low = text.lower()
    if not any(k in low for k in (
        "received", "credited", "added to your paytm", "money received", "paytm"
    )):
        return None
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    return _to_paise(m.group(1)), _first_ref(text)


def parse_hdfc(text: str) -> tuple[int, str] | None:
    """HDFC Bank UPI credit alert.

    Example wording (from alerts@hdfcbank.bank.in):
      "Rs.3000.00 has been successfully credited to your HDFC Bank account
       ending in 7692 ... UPI Reference No.: 213906566909"
    """
    low = text.lower()
    # Require a credit cue so debit alerts / statements don't match.
    if "credited" not in low:
        return None
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    amount_paise = _to_paise(m.group(1))
    # HDFC labels the reference as "UPI Reference No.: <digits>".
    ref = ""
    rm = re.search(r"UPI\s*Reference\s*No\.?\s*:?\s*([0-9]{6,})", text, re.IGNORECASE)
    if rm:
        ref = rm.group(1)
    else:
        ref = _first_ref(text)
    return amount_paise, ref


PARSERS = {
    "generic": parse_generic,
    "paytm": parse_paytm,
    "hdfc": parse_hdfc,
}
