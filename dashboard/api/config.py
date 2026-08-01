from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# dashboard/api/config.py -> repo root is two parents up. Resolving from
# __file__ (rather than a bare relative path) means feeds.yaml is found
# regardless of the process's working directory — local `fastapi dev` and a
# Vercel Function don't share the same cwd convention.
_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DASHBOARD_API_")

    env: str = "dev"
    cors_origins: str = "http://localhost:3000"
    feeds_config_path: Path = _REPO_ROOT / "config" / "feeds.yaml"


settings = Settings()
