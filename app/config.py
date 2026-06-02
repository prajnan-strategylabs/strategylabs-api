from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    supabase_url: str = ""
    supabase_secret_key: str = ""  # service-role key — never expose to browser
    blog_pipeline_secret: str = "strategylabs-secret-blog-key-2026"  # protected header key for AI pipeline posts
    is_launched: bool = False
    waitlist_full: bool = False  # when True, hide waitlist signup forms and show "full" state instead
    admin_enabled: bool = False  # kill switch — disable all /admin/* endpoints
    admin_emails: str = ""  # comma-separated admin emails via ADMIN_EMAILS env var
    allowed_origins: list[str] = [
        "http://localhost:5173",
        "https://strategylabs.trade",
        "https://www.strategylabs.trade",
        "http://localhost",
        "https://localhost",
        "capacitor://localhost"
    ]

    # ── AI / Quant Coach (Claude & xAI integrations) ─────────────────────────
    ai_provider: str = "claude"  # "claude" or "xai"
    ai_api_key: str = ""         # API key (Anthropic or xAI Grok bearer token)
    ai_model: str = "claude-opus-4-6" # The specific Claude or xAI model to query (defaults to claude-opus-4-6 for 2026 support)



    # ── Cloudflare R2 (S3-compatible storage for blog cover images) ──────────
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = "strategylabs-blogs"
    r2_public_url: str = ""

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
    telegram_signal_min_tier: str = "trader"
    revenuecat_webhook_auth: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
