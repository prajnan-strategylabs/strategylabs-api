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

## Scale to zero (free tier)

The `fly.toml` already sets `min_machines_running = 0` — the API hibernates when idle
and wakes on the first request (~300ms cold start). Fine for pre-launch traffic.

Bump to `min_machines_running = 1` once you have paying users.

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
