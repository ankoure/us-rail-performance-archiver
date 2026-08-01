from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DASHBOARD_API_")

    env: str = "dev"
    cors_origins: str = "http://localhost:3000"


settings = Settings()
