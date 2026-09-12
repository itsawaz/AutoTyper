"""Payment providers behind a common interface.

The rest of the app talks only to `get_provider()`. Two implementations:

- MockProvider     : no real money. `create_order` returns a fake pay page URL
                     that, when opened, immediately calls our own confirm
                     endpoint. Lets the whole buy-hours flow be tested end-to-end
                     with zero setup.
- UpiGmailProvider : free UPI collection confirmed by reading bank credit-alert
                     emails from Gmail (no payment gateway, no KYC). The buy flow
                     reserves a unique amount so an incoming alert maps to exactly
                     one order; the Gmail poller does the confirmation.

Neither provider's secrets are ever shipped in the desktop client — this module
only runs on the backend.
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

from config import get_settings


@dataclass
class CreatedOrder:
    provider_ref: str        # provider's id for this order
    pay_url: str             # URL the client opens in a browser to pay
    # UPI-only extras (empty for other providers):
    upi_uri: str = ""        # upi://pay?... deep link for QR / app intent
    amount_paise: int = 0    # the actual (possibly tagged) amount to pay


class PaymentProvider:
    name = "base"

    def create_order(self, order_id: str, amount_paise: int, currency: str,
                     customer_id: str) -> CreatedOrder:
        raise NotImplementedError


class MockProvider(PaymentProvider):
    """Simulates a gateway. The pay_url points back at our own mock-confirm
    endpoint so opening it in a browser marks the order paid."""
    name = "mock"

    def create_order(self, order_id: str, amount_paise: int, currency: str,
                     customer_id: str) -> CreatedOrder:
        # Points at our own backend; base is filled in by the router which knows
        # the request host. We return a relative path and let the router absolutize.
        pay_url = f"/payments/mock/pay?order_id={order_id}"
        return CreatedOrder(provider_ref=f"mock_{order_id}", pay_url=pay_url)


class UpiGmailProvider(PaymentProvider):
    """Free UPI collection with Gmail-based confirmation (no gateway, no KYC).

    There is no external order create. Instead the buy flow reserves a unique
    amount (base price + random paise tag) so an incoming bank credit-alert email
    maps to exactly one order. This provider just builds the upi:// deep link and
    a browser-openable pay page. Confirmation happens asynchronously when the
    Gmail poller matches an alert amount to a pending order.
    """
    name = "upi_gmail"

    def create_order(self, order_id: str, amount_paise: int, currency: str,
                     customer_id: str) -> CreatedOrder:
        settings = get_settings()
        upi_uri = build_upi_uri(
            vpa=settings.upi_vpa,
            payee=settings.upi_payee_name,
            amount_paise=amount_paise,
            note=f"AutoTyper {order_id[:8]}",
        )
        # The client opens our own pay page which renders the QR + link and polls.
        pay_url = f"/payments/upi/pay?order_id={order_id}"
        return CreatedOrder(
            provider_ref=f"upi_{order_id}",
            pay_url=pay_url,
            upi_uri=upi_uri,
            amount_paise=amount_paise,
        )


def build_upi_uri(vpa: str, payee: str, amount_paise: int, note: str) -> str:
    """Construct a standard upi://pay deep link that any UPI app can open.

    UPI apps are picky about encoding: the payee address (pa) must keep its raw
    '@', and spaces should be %20 (not '+'). So we percent-encode each value with
    quote() using a safe set that preserves '@', rather than urlencode().
    """
    def enc(value: str) -> str:
        # Keep '@' unescaped (required for the VPA); everything else encoded,
        # spaces as %20.
        return urllib.parse.quote(str(value), safe="@")

    params = {
        "pa": vpa,
        "pn": payee,
        "am": f"{amount_paise / 100:.2f}",
        "cu": "INR",
        "tn": note,
    }
    return "upi://pay?" + "&".join(f"{k}={enc(v)}" for k, v in params.items())


def get_provider() -> PaymentProvider:
    settings = get_settings()
    if settings.payment_provider == "upi_gmail":
        return UpiGmailProvider()
    return MockProvider()
