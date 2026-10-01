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

    # Conector de Outlook (Microsoft Graph).
    outlook_client_id: str = ""
    outlook_client_secret: str = ""
    outlook_tenant_id: str = "common"
    outlook_redirect_uri: str = (
        "http://127.0.0.1:8000/api/connectors/outlook/callback"
    )
    app_encryption_key: str = ""

    # Envío de correo (bandeja de salida). Sin SMTP, los mensajes se
    # descargan como borrador .eml y se abren en Outlook o Thunderbird.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_ssl: bool = False

    # Correo entrante (conector de correo): buzón IMAP del que se leen
    # facturas y notificaciones. Sin IMAP, se puede usar la carpeta
    # data/buzon (deja ahí los .eml) o importar un .eml desde la pantalla.
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_use_ssl: bool = True

    # IA opcional (Claude). Sin clave, los agentes usan reglas y plantillas.
    anthropic_api_key: str = ""
    agent_model: str = "claude-opus-5-5"
    # Dirección pública para los enlaces de subida de documentos.
    public_base_url: str = "http://127.0.0.1:8000"

    # Automatizaciones programadas del agente (se desactivan en los tests).
    enable_scheduler: bool = True

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