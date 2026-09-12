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
    payment_provider: str = "mock"  # "mock" | "juspay"
    juspay_api_key: str = ""
    juspay_merchant_id: str = ""
    juspay_base_url: str = "https://sandbox.juspay.in"
    juspay_webhook_username: str = ""
    juspay_webhook_password: str = ""
    # Shared secret Juspay uses to HMAC-SHA256 sign webhook payloads. Configure
    # the same value in the Juspay dashboard webhook settings.
    juspay_webhook_secret: str = ""
    payment_return_url: str = "https://example.com/payment-return"

    # Pricing
    price_per_hour_paise: int = 4900  # ₹49.00
    currency: str = "INR"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
