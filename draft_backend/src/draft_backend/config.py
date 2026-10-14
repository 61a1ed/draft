import os
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
    google_client_id: str = ""
    google_client_secret: str = ""
    session_ttl_seconds: int = 86400
    pool_size: int = 10
    transaction_retries: int = 4
    sse_heartbeat_seconds: float = 15

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
        if bool(self.google_client_id) != bool(self.google_client_secret):
            raise ValueError("Set both GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET")
        if not self.google_client_id and not self.test_login_enabled:
            raise ValueError("Google credentials are required when test login is disabled")
        if (
            min(
                self.session_ttl_seconds,
                self.pool_size,
                self.transaction_retries,
                self.sse_heartbeat_seconds,
            )
            <= 0
        ):
            raise ValueError("Timeouts, pool size, and retry count must be positive")

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
            google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
            session_ttl_seconds=int(os.getenv("SESSION_TTL_SECONDS", "86400")),
            pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
        )
