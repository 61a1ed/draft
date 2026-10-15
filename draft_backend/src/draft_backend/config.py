import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


def flag(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"true", "1"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    session_secret: str
    public_base_url: str = "http://127.0.0.1:8000"
    app_env: str = "production"
    test_login_enabled: bool = False
    cookie_secure: bool = True
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_callback_path: str = "/auth/callback"
    session_ttl_seconds: int = 86400
    pool_size: int = 10
    transaction_retries: int = 4
    sse_heartbeat_seconds: float = 15
    sse_max_connections: int = 30

    def __post_init__(self):
        if not self.database_url:
            raise ValueError("DATABASE_URL is required")
        if len(self.session_secret) < 32:
            raise ValueError("SESSION_SECRET must contain at least 32 characters")
        url = urlsplit(self.public_base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError("PUBLIC_BASE_URL must be an HTTP(S) origin without a path")
        if self.test_login_enabled and self.app_env != "test":
            raise ValueError("Test login requires APP_ENV=test and a marked test database")
        if self.app_env not in {"production", "development", "test"}:
            raise ValueError("APP_ENV must be production, development, or test")
        if self.app_env == "production" and (not self.cookie_secure or url.scheme != "https"):
            raise ValueError("Production requires HTTPS PUBLIC_BASE_URL and COOKIE_SECURE=true")
        oidc = (self.oidc_issuer, self.oidc_client_id, self.oidc_client_secret)
        if any(oidc) and not all(oidc):
            raise ValueError("Set OIDC_ISSUER, OIDC_CLIENT_ID, and OIDC_CLIENT_SECRET together")
        if not all(oidc) and not self.test_login_enabled:
            raise ValueError("OIDC configuration is required when test login is disabled")
        if self.oidc_issuer:
            issuer = urlsplit(self.oidc_issuer)
            if (
                issuer.scheme != "https"
                or not issuer.hostname
                or issuer.query
                or issuer.fragment
                or issuer.username
                or issuer.password
            ):
                raise ValueError("OIDC_ISSUER must be an HTTPS issuer URL")
        callback = self.oidc_callback_path
        reserved = (
            "/api", "/images", "/assets", "/ready", "/healthz", "/docs", "/redoc",
            "/openapi.json", "/auth/login", "/auth/logout", "/auth/me", "/auth/test",
        )
        if (
            not re.fullmatch(r"/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*", callback)
            or any(part in {".", ".."} for part in callback.split("/"))
            or callback == "/auth"
            or any(callback == path or callback.startswith(path + "/") for path in reserved)
        ):
            raise ValueError("OIDC_CALLBACK_PATH must be a non-conflicting root-relative path")
        if (
            min(
                self.session_ttl_seconds,
                self.pool_size,
                self.sse_heartbeat_seconds,
                self.sse_max_connections,
            )
            <= 0
        ):
            raise ValueError("Timeouts and pool size must be positive")
        if self.transaction_retries < 0:
            raise ValueError("Retry count must be nonnegative")

    @property
    def origin(self) -> str:
        return self.public_base_url.rstrip("/")

    @classmethod
    def from_env(cls):
        return cls(
            database_url=os.environ["DATABASE_URL"],
            session_secret=os.environ["SESSION_SECRET"],
            public_base_url=os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000"),
            app_env=os.getenv("APP_ENV", "production"),
            test_login_enabled=flag("TEST_LOGIN_ENABLED", False),
            cookie_secure=flag("COOKIE_SECURE", True),
            oidc_issuer=os.getenv("OIDC_ISSUER", ""),
            oidc_client_id=os.getenv("OIDC_CLIENT_ID", ""),
            oidc_client_secret=os.getenv("OIDC_CLIENT_SECRET", ""),
            oidc_callback_path=os.getenv("OIDC_CALLBACK_PATH", "/auth/callback"),
            session_ttl_seconds=int(os.getenv("SESSION_TTL_SECONDS", "86400")),
            pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
            sse_max_connections=int(os.getenv("SSE_MAX_CONNECTIONS", "30")),
        )
