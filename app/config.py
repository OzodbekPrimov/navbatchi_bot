from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    database_url: str
    admin_ids: str = ""
    timezone: str = "Asia/Tashkent"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def parsed_admin_ids(self) -> set[int]:
        return {int(value.strip()) for value in self.admin_ids.split(",") if value.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
