from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    supabase_url: str = ""
    supabase_secret_key: str = ""  # service-role key — never expose to browser
    blog_pipeline_secret: str = "strategylabs-secret-blog-key-2026"  # protected header key for AI pipeline posts
    is_launched: bool = False
    waitlist_full: bool = False  # when True, hide waitlist signup forms and show "full" state instead
    allowed_origins: list[str] = ["http://localhost:5173", "https://strategylabs.trade"]

    # ── Telegram bot (V22 signal notifications) ──────────────────────────────
    # When empty, the notifier becomes a no-op — useful for local dev.
    telegram_bot_token: str = ""
    # Bot username without the @, used to build t.me/<username>?start=<token>
    # deep-links for the in-app "Connect Telegram" button.
    telegram_bot_username: str = ""
    # Webhook secret — Telegram echoes this back as a header so we can verify
    # incoming webhook calls actually originated from Telegram.
    telegram_webhook_secret: str = ""
    # Lowest tier that receives signal alerts. With the 3-tier model
    # (free / trader / auto) this defaults to "trader" — paying users get
    # signals, free users don't. Override per environment via fly secrets.
    # Legacy explorer / pro values are still honoured for back-compat.
    telegram_signal_min_tier: str = "trader"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
