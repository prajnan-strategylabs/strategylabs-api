-- ────────────────────────────────────────────────────────────────────────────
-- v22_signals — live signal log for the V22 scanner.
-- One row per call. Created with status='open' and patched to status='closed'
-- once an exit (TP / SL / trail) fires.
-- ────────────────────────────────────────────────────────────────────────────

create table if not exists public.v22_signals (
    id                bigserial primary key,
    entry_time        timestamptz       not null,
    symbol            text              not null,            -- e.g. "BTC/USDT"
    asset             text              not null,            -- e.g. "BTC" (derived)
    strategy          text              not null,            -- "S3" | "S5"
    direction         text              not null,            -- "long" | "short"
    entry             double precision  not null,
    stop_loss         double precision  not null,
    tp1               double precision  not null,
    tp2               double precision,
    rr                double precision  not null,
    risk_usd          double precision,
    position_size     double precision,

    -- lifecycle
    status            text              not null default 'open',
                                                              -- 'open' | 'closed'
    exit_time         timestamptz,
    exit_price        double precision,
    exit_reason       text,                                   -- 'tp1' | 'tp2' | 'sl' | 'trail' | 'timeout'
    outcome           text,                                   -- 'win' | 'loss' | 'partial'
    pnl               double precision,                       -- realized $ on $5K account
    ret_pct           double precision,                       -- (exit-entry)/entry × direction_sign

    last_checked_at   timestamptz       not null default now(),
    created_at        timestamptz       not null default now()
);

create index if not exists v22_signals_status_idx     on public.v22_signals (status);
create index if not exists v22_signals_entry_time_idx on public.v22_signals (entry_time desc);
create unique index if not exists v22_signals_dedupe  on public.v22_signals (symbol, entry_time, strategy);

-- ────────────────────────────────────────────────────────────────────────────
-- v22_scanner_state — single-row heartbeat for the live scanner.
-- The frontend reads last_scan_at to render a "Live · scanned Xs ago" stamp.
-- ────────────────────────────────────────────────────────────────────────────

create table if not exists public.v22_scanner_state (
    id                int               primary key default 1
                                                              constraint singleton check (id = 1),
    last_scan_at      timestamptz,                            -- last full V22 scan
    last_exit_check   timestamptz,                            -- last open-position check
    last_signal_at    timestamptz,                            -- last time a new signal fired
    open_count        int               not null default 0,
    closed_count      int               not null default 0,
    updated_at        timestamptz       not null default now()
);
insert into public.v22_scanner_state (id) values (1) on conflict (id) do nothing;

-- RLS: signals are public-read (drive the upsell page) and service-role-write.
alter table public.v22_signals          enable row level security;
alter table public.v22_scanner_state    enable row level security;

drop policy if exists "v22_signals_read"        on public.v22_signals;
create policy        "v22_signals_read"
    on public.v22_signals for select using (true);

drop policy if exists "v22_state_read"          on public.v22_scanner_state;
create policy        "v22_state_read"
    on public.v22_scanner_state for select using (true);
-- inserts/updates happen via service_role from the backend; no anon write policy.
