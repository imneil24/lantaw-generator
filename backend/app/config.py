from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    runpod_video_key: str
    runpod_image_key: str
    runpod_video_endpoint: str
    runpod_image_endpoint: str
    r2_write_key: str
    r2_write_secret: str
    r2_read_key: str
    r2_read_secret: str
    r2_bucket: str
    r2_endpoint: str
    postgres_dsn: str
    redis_url: str
    backend_api_key_hash: str
    webhook_url: str
    moderation_api_key: str | None = None

    def all_secrets(self) -> list[str]:
        return [
            self.runpod_video_key, self.runpod_image_key,
            self.r2_write_secret, self.r2_read_secret,
            self.postgres_dsn, self.redis_url,
            self.backend_api_key_hash, self.webhook_url,
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
