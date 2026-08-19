from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_version: str = "0.1.1"
    database_path: Path = Path("data/jdchat.sqlite3")
    api_token: str | None = None
    media_storage_provider: str = "local"
    media_dir: Path = Path("data/media")
    media_public_base_url: str | None = None
    media_download_enabled: bool = True
    media_download_timeout_seconds: float = 10
    media_download_max_bytes: int = 10 * 1024 * 1024

    model_config = SettingsConfigDict(
        env_prefix="JDCHAT_",
        env_file=".env",
        extra="ignore",
    )
