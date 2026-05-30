-- ────────────────────────────────────────────────────────────────────────────
-- telegram_subscriptions — links a Strategy Labs user to their Telegram chat
-- for V22 signal alerts. Created in 'pending' state when the user clicks
-- "Connect Telegram"; transitioned to 'verified' when the bot receives the
-- /start command and captures the chat_id.
-- ────────────────────────────────────────────────────────────────────────────

create table if not exists public.telegram_subscriptions (
    id                bigserial primary key,
    user_id           uuid              not null references auth.users(id) on delete cascade,

    -- Linking flow: app generates a random `link_token`, hands the user a
    -- t.me/<bot>?start=<link_token> URL. When the user runs /start in
    -- Telegram, the bot webhook resolves the token → user_id, captures the
    -- chat_id, marks the row verified.
    link_token        text              not null,
    link_token_expires_at  timestamptz  not null,
    chat_id           bigint            unique,           -- null until verified
    telegram_handle   text,                                -- @username for display
    verified_at       timestamptz,                         -- set when /start completes

    -- Per-user controls
    enabled           boolean           not null default true,  -- user can pause
    last_sent_at      timestamptz,                              -- for cool-down + audit

    created_at        timestamptz       not null default now()
);

create unique index if not exists telegram_subs_user_idx
    on public.telegram_subscriptions (user_id);
create index if not exists telegram_subs_link_token_idx
    on public.telegram_subscriptions (link_token);
create index if not exists telegram_subs_verified_idx
    on public.telegram_subscriptions (chat_id)
    where verified_at is not null;

-- ── RLS ──────────────────────────────────────────────────────────────────────
-- Users can read / update their own row only. Inserts and the webhook update
-- go through the service-role backend (bypasses RLS).
alter table public.telegram_subscriptions enable row level security;

drop policy if exists "telegram_subs_self_read"   on public.telegram_subscriptions;
create policy        "telegram_subs_self_read"
    on public.telegram_subscriptions for select using (auth.uid() = user_id);

drop policy if exists "telegram_subs_self_update" on public.telegram_subscriptions;
create policy        "telegram_subs_self_update"
    on public.telegram_subscriptions for update using (auth.uid() = user_id);

drop policy if exists "telegram_subs_self_delete" on public.telegram_subscriptions;
create policy        "telegram_subs_self_delete"
    on public.telegram_subscriptions for delete using (auth.uid() = user_id);
