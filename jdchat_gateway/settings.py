from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_version: str = "0.1.1"
    database_path: Path = Path("data/jdchat.sqlite3")
    api_token: str | None = None

    model_config = SettingsConfigDict(
        env_prefix="JDCHAT_",
        env_file=".env",
        extra="ignore",
    )
