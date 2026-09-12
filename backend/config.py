"""Application configuration loaded from environment variables."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Turso / libSQL
    turso_database_url: str = ""
    turso_auth_token: str = ""

    # Auth
    jwt_secret: str = "dev-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 43200  # 30 days

    # Payments
    payment_provider: str = "upi_gmail"  # "upi_gmail" | "mock"

    # ── UPI + Gmail confirmation (free, no gateway) ────────────
    # Your receiving VPA and the payee name shown in the UPI app.
    upi_vpa: str = "8105651736@ptyes"
    upi_payee_name: str = "AutoTyper"
    # Each order reserves the base price plus a random tag in this paise range so
    # the incoming bank alert amount maps to exactly one order.
    upi_tag_min_paise: int = 1
    upi_tag_max_paise: int = 99
    # Pending orders older than this (minutes) are expired and won't be matched.
    upi_order_ttl_minutes: int = 30

    # Gmail API (read-only) for reading bank credit-alert emails.
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    # Only read emails under this Gmail label (set a filter to apply it to bank
    # alerts). Leave blank to search the inbox with a query instead.
    gmail_label: str = "upi-alerts"
    # Which bank/wallet parser to use for the alert format.
    upi_bank_parser: str = "hdfc"
    # Only trust alerts from this sender (defends against spoofed emails matching
    # an amount). Blank disables the sender check.
    upi_alert_sender: str = "alerts@hdfcbank.bank.in"

    # Shared secret required to trigger the Gmail poll endpoint (Vercel cron
    # sends it; prevents random callers from driving the poller).
    poll_secret: str = ""

    # Pricing
    price_per_hour_paise: int = 4900  # ₹49.00
    currency: str = "INR"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
