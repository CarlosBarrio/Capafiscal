from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
PROJECT_DIR = BACKEND_DIR.parent

DEFAULT_DATA_DIR = BACKEND_DIR / "data"
DEFAULT_UPLOAD_DIR = BACKEND_DIR / "uploads"
DEFAULT_DATABASE_PATH = DEFAULT_DATA_DIR / "capafiscal.db"


class Settings(BaseSettings):
    app_mode: str = "real"
    seed_demo_data: bool = False
    enable_demo_connectors: bool = False
    reset_data_on_startup: bool = False
    app_name: str = "CapaFiscal"
    app_environment: str = "development"
    debug: bool = True

    database_url: str = (
        f"sqlite:///{DEFAULT_DATABASE_PATH.as_posix()}"
    )

    data_dir: Path = DEFAULT_DATA_DIR
    upload_dir: Path = DEFAULT_UPLOAD_DIR
    max_upload_size: int = 15 * 1024 * 1024
    allowed_extensions: str = ".pdf,.txt"

    company_name: str | None = None
    company_tax_id: str | None = None
    company_tax_ids: str | None = None

    amount_tolerance: float = 0.02
    minimum_auto_confidence: int = 80

    model_config = SettingsConfigDict(
        env_file=PROJECT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def allowed_extension_set(self) -> set[str]:
        extensions: set[str] = set()

        for extension in self.allowed_extensions.split(","):
            normalized = extension.strip().lower()

            if not normalized:
                continue

            if not normalized.startswith("."):
                normalized = f".{normalized}"

            extensions.add(normalized)

        return extensions


@lru_cache
def get_settings() -> Settings:
    settings = Settings()

    settings.data_dir.mkdir(
        parents=True,
        exist_ok=True,
    )
    settings.upload_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    return settings


settings = get_settings()