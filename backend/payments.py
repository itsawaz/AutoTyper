"""Payment providers behind a common interface.

The rest of the app talks only to `get_provider()`. Two implementations:

- MockProvider   : no real money. `create_order` returns a fake pay page URL that,
                   when opened, immediately calls our own confirm endpoint. Lets the
                   whole buy-hours flow be tested end-to-end with zero setup.
- JuspayProvider : real Juspay session create + webhook verification. Reads secrets
                   from env (server-only). Stubbed against Juspay's Orders API shape;
                   plug in your merchant credentials to go live.

Neither provider's secrets are ever shipped in the desktop client — this module
only runs on the backend.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import urllib.parse
from dataclasses import dataclass

import httpx

from config import get_settings


@dataclass
class CreatedOrder:
    provider_ref: str        # gateway's id for this order
    pay_url: str             # URL the client opens in a browser to pay


class PaymentProvider:
    name = "base"

    def create_order(self, order_id: str, amount_paise: int, currency: str,
                     customer_id: str) -> CreatedOrder:
        raise NotImplementedError

    def verify_webhook(self, headers: dict, body: bytes) -> None:
        """Raise if the webhook is not authentic."""
        raise NotImplementedError


class MockProvider(PaymentProvider):
    """Simulates a gateway. The pay_url points back at our own mock-confirm
    endpoint so opening it in a browser marks the order paid."""
    name = "mock"

    def create_order(self, order_id: str, amount_paise: int, currency: str,
                     customer_id: str) -> CreatedOrder:
        settings = get_settings()
        # Points at our own backend; base is filled in by the router which knows
        # the request host. We return a relative path and let the router absolutize.
        pay_url = f"/payments/mock/pay?order_id={order_id}"
        return CreatedOrder(provider_ref=f"mock_{order_id}", pay_url=pay_url)

    def verify_webhook(self, headers: dict, body: bytes) -> None:
        # Mock webhook is trusted (local, no money involved).
        return None


class JuspayProvider(PaymentProvider):
    """Real Juspay (Express Checkout) integration.

    Flow (per Juspay's Orders API docs):
      1. create_order  -> server-to-server POST /orders with order_id + amount.
         The response carries payment links (web / mobile / iframe); we hand the
         web link to the client to open in a browser.
      2. Juspay calls our webhook on status change. We verify it, then credit
         hours when the order/transaction status is CHARGED.

    Order id recovery: we set udf1 = our order_id so the webhook can always map
    the event back to the order even if the top-level field name varies.

    Webhook auth: Juspay signs webhook payloads with HMAC-SHA256 (documented
    mechanism) and can also send HTTP Basic credentials. We verify the HMAC when
    a secret is configured, and additionally check Basic auth when configured.
    Docs: create-order-api, webhooks, status-verification on juspay.io.
    """
    name = "juspay"

    def _require_creds(self, settings) -> None:
        missing = [
            n for n, v in (
                ("JUSPAY_API_KEY", settings.juspay_api_key),
                ("JUSPAY_MERCHANT_ID", settings.juspay_merchant_id),
                ("JUSPAY_BASE_URL", settings.juspay_base_url),
            ) if not v
        ]
        if missing:
            raise RuntimeError(
                "Juspay is selected but these settings are missing: "
                + ", ".join(missing)
            )

    def create_order(self, order_id: str, amount_paise: int, currency: str,
                     customer_id: str) -> CreatedOrder:
        settings = get_settings()
        self._require_creds(settings)

        # Juspay amounts are in major units (rupees) as a decimal string.
        amount_major = f"{amount_paise / 100:.2f}"
        # API key is the Basic-auth username with an empty password.
        auth = base64.b64encode(f"{settings.juspay_api_key}:".encode()).decode()
        resp = httpx.post(
            f"{settings.juspay_base_url.rstrip('/')}/orders",
            headers={
                "Authorization": f"Basic {auth}",
                "x-merchantid": settings.juspay_merchant_id,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "order_id": order_id,
                "amount": amount_major,
                "currency": currency,
                "customer_id": customer_id,
                "return_url": settings.payment_return_url,
                # Recover our order id from the webhook regardless of field name.
                "udf1": order_id,
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        data = resp.json()
        links = data.get("payment_links", {}) or {}
        pay_url = links.get("web") or links.get("mobile") or links.get("iframe") or ""
        if not pay_url:
            raise RuntimeError(f"Juspay order response had no payment link: {data}")
        return CreatedOrder(provider_ref=str(data.get("id", order_id)), pay_url=pay_url)

    def verify_webhook(self, headers: dict, body: bytes) -> None:
        from fastapi import HTTPException

        settings = get_settings()
        verified = False

        # 1) HMAC-SHA256 signature verification (preferred).
        if settings.juspay_webhook_secret:
            # Juspay sends the signature (and the algorithm) in headers; header
            # names have varied across versions, so accept the common variants.
            sig = (
                headers.get("x-juspay-signature")
                or headers.get("x-webhook-signature")
                or headers.get("signature")
                or ""
            ).strip()
            # Signatures are commonly URL-encoded base64; try as-is and decoded.
            candidates = {sig, urllib.parse.unquote(sig)}
            expected = hmac.new(
                settings.juspay_webhook_secret.encode(),
                body,
                hashlib.sha256,
            ).digest()
            expected_b64 = base64.b64encode(expected).decode()
            expected_hex = expected.hex()
            for cand in candidates:
                if cand and (
                    hmac.compare_digest(cand, expected_b64)
                    or hmac.compare_digest(cand.lower(), expected_hex.lower())
                ):
                    verified = True
                    break
            if not verified:
                raise HTTPException(status_code=401, detail="Bad webhook signature")

        # 2) Optional HTTP Basic auth on the webhook endpoint.
        if settings.juspay_webhook_username or settings.juspay_webhook_password:
            expected_basic = base64.b64encode(
                f"{settings.juspay_webhook_username}:{settings.juspay_webhook_password}".encode()
            ).decode()
            got = (headers.get("authorization") or "").removeprefix("Basic ").strip()
            if not hmac.compare_digest(got, expected_basic):
                raise HTTPException(status_code=401, detail="Invalid webhook auth")
            verified = True

        # If neither mechanism is configured, refuse rather than trust blindly.
        if not verified:
            raise HTTPException(
                status_code=401,
                detail="Webhook not verified: configure JUSPAY_WEBHOOK_SECRET "
                       "and/or webhook Basic-auth credentials.",
            )


def get_provider() -> PaymentProvider:
    settings = get_settings()
    if settings.payment_provider == "juspay":
        return JuspayProvider()
    return MockProvider()
