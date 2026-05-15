# Fly.io Deployment Notes

This app should be deployed as a paper-trading dashboard first. Keep the bot disabled until secrets, auth, and the volume are configured.

## Required Secrets

Set these with `fly secrets set`:

```powershell
fly secrets set OPENAI_API_KEY=...
fly secrets set TELEGRAM_BOT_TOKEN=...
fly secrets set TELEGRAM_CHAT_ID=...
fly secrets set DASHBOARD_PASSWORD=choose-a-strong-password
```

The dashboard uses HTTP Basic Auth with username `admin` and the `DASHBOARD_PASSWORD` value.

## Optional Runtime Flags

```powershell
fly secrets set BOT_ENABLED=true
fly secrets set TRADING_MODE=paper
fly secrets set PAPER_NOTIFICATIONS_ENABLED=1
fly secrets set PAPER_NOTIFICATION_INTERVAL_SECONDS=1800
```

Keep `TRADING_MODE=paper`. The app refuses to run the paper-trading bot if this is changed.

## Persistent State

Create a Fly volume before deploying:

```powershell
fly volumes create tradingagents_data --size 1 --region sin
```

The included `fly.toml` mounts that volume at `/data` and stores:

- `/data/paper_portfolio.json`
- `/data/paper_trades.log`
- `/data/paper_trader.lock`

## Deploy

```powershell
fly launch --no-deploy
fly deploy
```

If your Fly app name is different, update `app = "tradingagents-india-paper"` in `fly.toml`.

## Safety Checks

- Do not commit `.env`.
- Do not commit live runtime state files.
- Keep `min_machines_running = 1`.
- Avoid scaling this app above one machine unless the scheduler is moved to a separate locked worker.
