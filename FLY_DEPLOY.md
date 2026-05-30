# Deploy to fly.io

## Prerequisites

```bash
# Install flyctl (once)
curl -L https://fly.io/install.sh | sh   # macOS / Linux
# Windows: https://fly.io/docs/hands-on/install-flyctl/

fly auth login
```

## First deploy

```bash
cd strategylabs-api

# Create the app (only once — uses app name from fly.toml)
fly apps create strategylabs-api

# Set secrets (never committed to repo)
fly secrets set \
  SUPABASE_URL="https://YOUR_PROJECT.supabase.co" \
  SUPABASE_SECRET_KEY="sb_secret_..." \
  SUPABASE_JWT_SECRET="your-jwt-secret" \
  ALLOWED_ORIGINS="https://strategylabs.trade,http://localhost:5173"

# Deploy
fly deploy
```

## Subsequent deploys

```bash
fly deploy
```

## Check logs

```bash
fly logs
```

## Always-on (required by the V22 live scanner)

The `fly.toml` is configured with `auto_stop_machines = false` and
`min_machines_running = 1` because the V22 scanner runs as an asyncio
background task — it needs to run continuously, not just when there's
inbound HTTP traffic. The VM is also bumped to `512MB` to give pandas /
numpy / ccxt and the in-memory OHLCV cache enough headroom. Costs ~$4/mo
on the smallest shared-cpu VM.

If you ever want to disable the scanner (e.g. for local-only testing),
set `V22_SCANNER_DISABLED=1` in fly secrets — the rest of the API keeps
working without it.

## Migrations

After the first deploy, run any pending SQL migrations from
`app/migrations/` in the Supabase SQL editor. The current ones:

- `001_v22_signals.sql` — creates `v22_signals` + `v22_scanner_state`
  with public-read RLS. Required for the live scanner to persist anything.

## Env vars reference

| Variable | Where to find it |
|---|---|
| `SUPABASE_URL` | Supabase → Settings → API → Project URL |
| `SUPABASE_SECRET_KEY` | Supabase → Settings → API → service_role key |
| `SUPABASE_JWT_SECRET` | Supabase → Settings → API → JWT secret |
| `ALLOWED_ORIGINS` | Your frontend URL(s), comma-separated |

## Health check

```bash
curl https://strategylabs-api.fly.dev/health
# → {"status":"ok"}
```

## Interactive API docs

```
https://strategylabs-api.fly.dev/docs
```
