from dataclasses import dataclass
import os
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    database_url: str
    environment: str
    secret: str
    public_url: str = "http://127.0.0.1:8090"
    timezone: str = "Africa/Johannesburg"
    secure_cookies: bool = False
    station_stale_seconds: int = 30
    sorter_host: str = "127.0.0.1"
    sorter_port: int = 8000
    machine_jobs_port: int = 8091
    machine_jobs_token_file: str = ""

    def __post_init__(self):
        if self.environment not in {"real", "simulation"}:
            raise ValueError("BEANOSORT_ENVIRONMENT must be real or simulation")
        if len(self.secret) < 32 or self.secret.startswith("replace-with-"):
            raise ValueError("Set BEANOSORT_SECRET to at least 32 random characters")
        url = urlparse(self.public_url)
        if url.scheme not in {"http", "https"} or not url.netloc or url.query or url.fragment:
            raise ValueError("BEANOSORT_PUBLIC_URL must be an absolute HTTP(S) base URL")
        ZoneInfo(self.timezone)
        if self.station_stale_seconds < 1:
            raise ValueError("Station stale interval must be positive")

    @classmethod
    def from_env(cls):
        return cls(
            database_url=os.environ.get("BEANOSORT_DATABASE_URL", "postgresql+psycopg://beanosort@localhost/beanosort"),
            environment=os.environ.get("BEANOSORT_ENVIRONMENT", "simulation"),
            secret=os.environ.get("BEANOSORT_SECRET", ""),
            public_url=os.environ.get("BEANOSORT_PUBLIC_URL", "http://127.0.0.1:8090").rstrip("/"),
            timezone=os.environ.get("BEANOSORT_TIMEZONE", "Africa/Johannesburg"),
            secure_cookies=os.environ.get("BEANOSORT_SECURE_COOKIES") == "1",
            station_stale_seconds=int(os.environ.get("BEANOSORT_STATION_STALE_SECONDS", "30")),
            sorter_host=os.environ.get("BEANOSORT_SORTER_HOST", "127.0.0.1"),
            sorter_port=int(os.environ.get("BEANOSORT_SORTER_PORT", "8000")),
            machine_jobs_port=int(os.environ.get("BEANOSORT_MACHINE_JOBS_PORT", "8091")),
            machine_jobs_token_file=os.environ.get("BEANOSORT_MACHINE_JOBS_TOKEN_FILE", ""),
        )
