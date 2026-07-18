-- Pseudonymous first-party web analytics. Events are written by the backend
-- service role and are only exposed through the authenticated admin API.
create table if not exists public.page_views (
    id          bigserial primary key,
    visitor_id  text not null,
    session_id  text not null,
    path        text not null,
    title       text,
    referrer    text,
    utm         jsonb,
    created_at  timestamptz not null default now()
);

create index if not exists page_views_created_at_idx
    on public.page_views (created_at desc);
create index if not exists page_views_path_created_at_idx
    on public.page_views (path, created_at desc);
create index if not exists page_views_visitor_created_at_idx
    on public.page_views (visitor_id, created_at desc);

alter table public.page_views enable row level security;

-- No browser policies: reads and writes are only permitted via the backend's
-- service-role client. This keeps analytics data out of the public API.
