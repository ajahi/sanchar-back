"""Application settings, loaded from environment / .env (see .env.example)."""
import logging
from functools import lru_cache
from typing import Optional

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

# ENVIRONMENT values that mean "this is a real deployment serving real users".
_PRODUCTION_NAMES = frozenset({"production", "prod"})

# Substrings that mark a value as copied from .env.example rather than generated.
_PLACEHOLDER_MARKERS = ("change-me", "changeme", "example", "placeholder", "your-")


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

    # ---- Instagram Login (the primary flow) ----
    # These are the *Instagram* app credentials from
    # Instagram -> API setup with Instagram login. They are DIFFERENT NUMBERS from the
    # Facebook app credentials below, and mixing them up is the single most confusing
    # failure in this integration: feeding an Instagram app id to Facebook's OAuth dialog
    # returns "Invalid app ID: The provided app ID does not look like a valid app ID",
    # and using it as a `{id}|{secret}` app token returns code 190
    # "Error validating application". Hence two clearly separated groups.
    instagram_app_id: str = ""
    instagram_app_secret: str = ""
    # Must match the redirect URI registered under Instagram -> Business login settings.
    instagram_redirect_uri: str = ""
    instagram_scopes: str = (
        "instagram_business_basic,instagram_business_manage_messages"
    )
    instagram_dialog_url: str = "https://www.instagram.com"
    instagram_oauth_base_url: str = "https://api.instagram.com"
    instagram_graph_base_url: str = "https://graph.instagram.com"

    # ---- Facebook Login for Business (optional second flow) ----
    # Only needed for Messenger/WhatsApp later, or for Instagram accounts reached through a
    # Page. The META_* spellings are accepted so an existing .env keeps working.
    # `pages_read_engagement` is a required dependency for the Instagram User node and
    # `business_management` is in Meta's Instagram-messaging setup list; omitting either
    # gives a login that succeeds but reads nothing.
    facebook_app_id: str = Field(
        "", validation_alias=AliasChoices("FACEBOOK_APP_ID", "META_APP_ID")
    )
    facebook_app_secret: str = Field(
        "", validation_alias=AliasChoices("FACEBOOK_APP_SECRET", "META_APP_SECRET")
    )
    facebook_redirect_uri: str = Field(
        "", validation_alias=AliasChoices("FACEBOOK_REDIRECT_URI", "META_REDIRECT_URI")
    )
    facebook_scopes: str = (
        "instagram_basic,instagram_manage_messages,pages_manage_metadata,"
        "pages_show_list,pages_read_engagement,business_management,pages_messaging"
    )
    facebook_dialog_url: str = "https://www.facebook.com"
    facebook_graph_base_url: str = "https://graph.facebook.com"

    oauth_state_expire_minutes: int = 10
    # Any string you also paste into the dashboard's webhook "Verify token" field
    meta_webhook_verify_token: str = Field(
        "",
        validation_alias=AliasChoices(
            "META_WEBHOOK_VERIFY_TOKEN", "INSTAGRAM_WEBHOOK_VERIFY_TOKEN"
        ),
    )

    # ---- Graph API transport ----
    # The host and path root are chosen per account (see services/meta/target.py), because
    # Instagram Login talks to graph.instagram.com while Facebook Login talks to
    # graph.facebook.com. This is only the fallback.
    graph_api_version: str = "v21.0"
    graph_base_url: str = "https://graph.facebook.com"
    graph_timeout_seconds: float = 20.0
    # Retries cover 429/5xx/transport blips and Meta's transient error codes.
    graph_max_attempts: int = 4
    graph_backoff_base_seconds: float = 0.5
    graph_backoff_max_seconds: float = 8.0
    # The Conversations API allows ~2 calls/second per Instagram account, and Meta treats a
    # sudden burst as abuse (error 613, subcode 1996). Paginated crawls are paced to this
    # interval so a backfill looks like steady traffic rather than a spike.
    graph_min_interval_seconds: float = 0.5
    # Conversations backfill: how many threads to pull per Graph page, how many to
    # process concurrently, and a hard ceiling so one sync can never run away.
    sync_page_size: int = 25
    sync_max_conversations: int = 2000
    sync_max_messages_per_conversation: int = 500
    # Kick off a history import as soon as an account is connected, so the inbox is not
    # empty on first load.
    auto_sync_on_connect: bool = True

    # Where the callback sends the browser after a successful connect/login
    frontend_url: str = "http://localhost:3000"

    # Session cookie handed to the dashboard after OAuth
    session_cookie_name: str = "ns_session"
    # Forced on in production by _apply_environment_defaults (see below).
    cookie_secure: bool = False
    cookie_samesite: str = "lax"  # "none" for cross-site frontend<->backend over HTTPS

    # ---- App ----
    # "development" | "production". This drives real behaviour: cookie hardening, log
    # level and whether the interactive docs are exposed.
    environment: str = "development"
    log_level: Optional[str] = None  # defaults to DEBUG locally, INFO in production

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in _PRODUCTION_NAMES

    @model_validator(mode="after")
    def _apply_environment_defaults(self) -> "Settings":
        """Make ENVIRONMENT actually mean something.

        Declared but never read, this flag was worse than useless: `ENVIRONMENT=production`
        looked like it hardened the deployment while `COOKIE_SECURE` stayed false and the
        session cookie was issued without the Secure attribute, so it could be replayed
        over plain HTTP. Everything environment-dependent is resolved here, in one place.
        """
        if self.is_production:
            if not self.cookie_secure:
                log.warning(
                    "COOKIE_SECURE was false with ENVIRONMENT=%s; forcing it on. A session "
                    "cookie without the Secure attribute can leak over plain HTTP.",
                    self.environment,
                )
            # There is no legitimate reason to issue a session cookie insecurely in
            # production, so this is forced rather than merely suggested.
            self.cookie_secure = True

        if self.log_level is None:
            self.log_level = "INFO" if self.is_production else "DEBUG"

        if self.is_production:
            for name, value in (
                ("JWT_SECRET", self.jwt_secret),
                ("TOKEN_ENCRYPTION_KEY", self.token_encryption_key),
            ):
                lowered = (value or "").lower()
                if not value or any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
                    log.error(
                        "%s is unset or still a placeholder while ENVIRONMENT=%s. "
                        "Generate a real value before serving traffic.",
                        name,
                        self.environment,
                    )

        # Log which flows are usable and under which app id. App ids are public (they appear
        # in every OAuth URL), and printing them here turns a confusing browser-side
        # "Invalid app ID" error into an immediately obvious misconfiguration.
        log.info(
            "meta config: instagram_login=%s (app_id=%s) | facebook_login=%s (app_id=%s) | "
            "webhook_verify_token=%s",
            "on" if self.instagram_login_configured else "off",
            self.instagram_app_id or "-",
            "on" if self.facebook_login_configured else "off",
            self.facebook_app_id or "-",
            "set" if self.meta_webhook_verify_token else "MISSING",
        )
        if self.instagram_app_id and self.instagram_app_id == self.facebook_app_id:
            log.error(
                "INSTAGRAM_APP_ID and FACEBOOK_APP_ID are the same value (%s). They are "
                "different app identities; using one for the other breaks the login flow.",
                self.instagram_app_id,
            )
        if self.instagram_app_id and not self.instagram_app_secret:
            log.error(
                "INSTAGRAM_APP_ID is set but INSTAGRAM_APP_SECRET is empty; Instagram "
                "Login cannot exchange a code without it."
            )

        return self

    @property
    def instagram_login_configured(self) -> bool:
        return bool(
            self.instagram_app_id
            and self.instagram_app_secret
            and self.instagram_redirect_uri
        )

    @property
    def facebook_login_configured(self) -> bool:
        return bool(
            self.facebook_app_id
            and self.facebook_app_secret
            and self.facebook_redirect_uri
        )


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
