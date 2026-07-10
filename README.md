# Eurostar Snap + normal fare WhatsApp alerts

This repository checks:

1. Eurostar Snap one-way availability for configured route/date windows.
2. Normal one-way Eurostar fares below a configured threshold, defaulting to 70 in GBP/EUR.
3. Sends WhatsApp notifications through Twilio.
4. Runs on GitHub Actions every 15 minutes, with explicit midnight and midday triggers.

## Important limitations

Eurostar does not provide a simple public consumer fare endpoint. This project uses low-frequency Playwright browser automation and text extraction. It does **not** log in, bypass CAPTCHA, automate payment, or purchase tickets. If Eurostar changes its website, selectors may need updating.

Use a private repository if your route/date searches are sensitive.

## Setup

### 1. Copy the config

```bash
cp config.example.yml config.yml
```

Edit `config.yml` with your routes, date ranges, passenger count, and normal fare threshold.

### 2. Create Twilio WhatsApp credentials

For testing, use Twilio's WhatsApp sandbox. Put these in GitHub repo secrets:

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_FROM_WHATSAPP`, e.g. `whatsapp:+14155238886`
- `TWILIO_TO_WHATSAPP`, e.g. `whatsapp:+447700900000`

### 3. Add GitHub secrets

GitHub repo → Settings → Secrets and variables → Actions → New repository secret.

### 4. Enable workflow write permissions

Repo → Settings → Actions → General → Workflow permissions → **Read and write permissions**.

This lets the workflow update `data/notified.json` so you do not get the same alert every 15 minutes.

### 5. Run manually first

GitHub repo → Actions → Eurostar fare monitor → Run workflow.

## Local test

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp config.example.yml config.yml
python -m eurostar_alerts.main --config config.yml
```

Set `settings.headless: false` locally if you want to watch the browser.

## Notes on duplicate alerts

Alerts are keyed by provider, route, date, passenger count, and price. The state file is committed back to the repo after each successful run.
