from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    supabase_url: str = ""
    supabase_secret_key: str = ""  # service-role key — never expose to browser
    blog_pipeline_secret: str = "strategylabs-secret-blog-key-2026"  # protected header key for AI pipeline posts
    is_launched: bool = False
    waitlist_full: bool = False  # when True, hide waitlist signup forms and show "full" state instead
    allowed_origins: list[str] = ["http://localhost:5173", "https://strategylabs.trade"]

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
