"""Application settings, loaded from environment / .env (see .env.example)."""
import logging
import os
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

    # ---- Meta app (one app serves both Facebook Login for Business and webhooks) ----
    # The INSTAGRAM_* spellings are still accepted so an existing deployment's .env
    # keeps working during the migration.
    meta_app_id: str = Field(
        "", validation_alias=AliasChoices("META_APP_ID", "INSTAGRAM_APP_ID")
    )
    meta_app_secret: str = Field(
        "", validation_alias=AliasChoices("META_APP_SECRET", "INSTAGRAM_APP_SECRET")
    )
    # Must match a redirect URI registered in the Meta app dashboard, e.g.
    # https://<host>/api/v1/social-accounts/facebook/callback
    meta_redirect_uri: str = Field(
        "", validation_alias=AliasChoices("META_REDIRECT_URI", "INSTAGRAM_REDIRECT_URI")
    )
    # Facebook Login for Business: Page + linked Instagram messaging permissions.
    # `pages_read_engagement` is a required dependency for the Instagram User node, and
    # `business_management` is required by Meta's Instagram-messaging setup steps — both
    # are easy to omit and produce a login that succeeds but reads nothing.
    # `pages_messaging` is Messenger-only and safe to drop if App Review pushes back.
    #
    # Deliberately NOT aliased to the legacy INSTAGRAM_SCOPES: those values
    # (`instagram_business_basic`, `instagram_business_manage_messages`) only mean anything
    # to an Instagram-Login dialog. Inheriting them would silently request permissions that
    # Facebook Login does not recognise, so a stale value is reported rather than used.
    meta_scopes: str = (
        "instagram_basic,instagram_manage_messages,pages_manage_metadata,"
        "pages_show_list,pages_read_engagement,business_management,pages_messaging"
    )
    oauth_state_expire_minutes: int = 10
    # Any string you also paste into the Meta dashboard's webhook "Verify token" field
    meta_webhook_verify_token: str = Field(
        "",
        validation_alias=AliasChoices(
            "META_WEBHOOK_VERIFY_TOKEN", "INSTAGRAM_WEBHOOK_VERIFY_TOKEN"
        ),
    )

    # ---- Graph API transport ----
    graph_api_version: str = "v21.0"
    graph_base_url: str = "https://graph.facebook.com"
    graph_oauth_dialog_url: str = "https://www.facebook.com"
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

        # A leftover INSTAGRAM_SCOPES from the Instagram-Login era is now ignored. Say so
        # loudly, because the failure it causes (a consent screen that grants nothing
        # useful) is otherwise very hard to diagnose.
        legacy_scopes = os.environ.get("INSTAGRAM_SCOPES", "").strip()
        if legacy_scopes:
            log.warning(
                "INSTAGRAM_SCOPES=%r is ignored since the move to Facebook Login for "
                "Business. Those permission names only apply to Instagram Login. Using "
                "META_SCOPES instead: %s. Remove INSTAGRAM_SCOPES from the environment.",
                legacy_scopes,
                self.meta_scopes,
            )

        if self.is_production:
            for name, value in (
                ("JWT_SECRET", self.jwt_secret),
                ("TOKEN_ENCRYPTION_KEY", self.token_encryption_key),
                ("META_APP_SECRET", self.meta_app_secret),
            ):
                lowered = (value or "").lower()
                if not value or any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
                    log.error(
                        "%s is unset or still a placeholder while ENVIRONMENT=%s. "
                        "Generate a real value before serving traffic.",
                        name,
                        self.environment,
                    )

        return self

    @property
    def meta_configured(self) -> bool:
        return bool(
            self.meta_app_id
            and self.meta_app_secret
            and self.meta_redirect_uri
        )


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
