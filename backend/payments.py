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
    """Real Juspay integration (Orders API).

    Docs: Juspay creates an "order" server-side; the response includes a
    payment page link the customer opens. Juspay then calls your webhook
    (HTTP Basic auth) on status change. We credit hours on 'CHARGED'.
    """
    name = "juspay"

    def create_order(self, order_id: str, amount_paise: int, currency: str,
                     customer_id: str) -> CreatedOrder:
        settings = get_settings()
        # Juspay amounts are in major units (rupees) as a decimal string.
        amount_major = f"{amount_paise / 100:.2f}"
        auth = base64.b64encode(f"{settings.juspay_api_key}:".encode()).decode()
        resp = httpx.post(
            f"{settings.juspay_base_url}/orders",
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
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        data = resp.json()
        pay_url = (
            data.get("payment_links", {}).get("web")
            or data.get("payment_links", {}).get("mobile")
            or ""
        )
        return CreatedOrder(provider_ref=data.get("id", order_id), pay_url=pay_url)

    def verify_webhook(self, headers: dict, body: bytes) -> None:
        settings = get_settings()
        expected = base64.b64encode(
            f"{settings.juspay_webhook_username}:{settings.juspay_webhook_password}".encode()
        ).decode()
        got = (headers.get("authorization") or "").removeprefix("Basic ").strip()
        if got != expected:
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Invalid webhook auth")


def get_provider() -> PaymentProvider:
    settings = get_settings()
    if settings.payment_provider == "juspay":
        return JuspayProvider()
    return MockProvider()
