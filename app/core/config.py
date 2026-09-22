"""Application settings, loaded from environment / .env (see .env.example)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Database
    database_url: str

    # JWT
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 14

    # Encryption for Meta access tokens at rest (Fernet key)
    token_encryption_key: str

    # Instagram OAuth (Instagram API with Instagram Login)
    instagram_app_id: str = ""
    instagram_app_secret: str = ""
    # Must match a redirect URI registered in the Meta app dashboard, e.g.
    # https://<tunnel>/api/v1/social-accounts/instagram/callback
    instagram_redirect_uri: str = ""
    instagram_scopes: str = "instagram_business_basic,instagram_business_manage_messages"
    oauth_state_expire_minutes: int = 10
    # Any string you also paste into the Meta dashboard's webhook "Verify token" field
    instagram_webhook_verify_token: str = ""
    meta_app_secret: str=""

    # Where the callback sends the browser after a successful connect/login
    frontend_url: str = "http://localhost:3000"

    # Session cookie handed to the dashboard after OAuth
    session_cookie_name: str = "ns_session"
    cookie_secure: bool = False  # True in production (HTTPS); needed with SameSite=None
    cookie_samesite: str = "lax"  # "none" for cross-site frontend<->backend over HTTPS

    # App
    environment: str = "development"

    @property
    def instagram_configured(self) -> bool:
        return bool(
            self.instagram_app_id
            and self.instagram_app_secret
            and self.instagram_redirect_uri
        )


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
