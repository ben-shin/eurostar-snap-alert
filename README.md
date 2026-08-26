# Eurostar fare alerts

A small Playwright monitor for:

- Eurostar Snap availability on exact configured travel dates.
- Normal one-way Eurostar fares at or below a configured threshold.
- WhatsApp notifications through Twilio.
- Scheduled execution with GitHub Actions.

The monitor uses Eurostar's visible booking forms and does not log in, bypass CAPTCHAs, purchase tickets, or automate payment. Eurostar does not provide a simple public consumer fare API, so selectors can still require maintenance when the website changes.

## Reliability model

Each provider/date check must end in one of three states: `available`, `unavailable`, or `failed`. A missing form, rejected date, CAPTCHA, error page, or unrecognised result is a failure—not a successful run with zero fares.

Snap prices are read only from result elements whose `data-testid` contains the requested date. This prevents adjacent-date prices from producing false alerts.

Every run writes `debug/report.json`. Failed pages also produce HTML and screenshot diagnostics, which the GitHub workflow uploads as an artifact.

## Configuration

Copy the example and edit the route/date window:

```bash
cp config.example.yml config.yml
```

Important settings:

- `snap_max_days_ahead`: only dates inside this rolling Snap window are checked.
- `threshold_amount`: alert threshold for normal fares.
- `allowed_currencies`: currencies accepted by the normal-fare check; no FX conversion is performed.
- `debug`: capture a screenshot and HTML when a check fails.

The tracked `config.yml` contains the production searches. Keep the repository private if those searches are sensitive.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m playwright install chromium
pytest
python -m eurostar_alerts.main --config config.yml --dry-run
```

`--dry-run` performs live checks and prints matches without sending WhatsApp messages or updating notification state.

## GitHub Actions setup

Create these repository secrets under **Settings → Secrets and variables → Actions**:

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_FROM_WHATSAPP`, for example `whatsapp:+14155238886`
- `TWILIO_TO_WHATSAPP`, for example `whatsapp:+447700900000`

Set **Settings → Actions → General → Workflow permissions** to **Read and write permissions**. This lets successful notifications update `data/notified.json` to prevent duplicates.

The workflow runs tests on pull requests. Scheduled and manual runs perform live fare checks, upload diagnostics, preserve notification state, and fail visibly if any requested scrape could not be classified.

## Commands

```bash
# Unit tests
pytest

# Live check without notifications
python -m eurostar_alerts.main --config config.yml --dry-run

# Production-equivalent run (requires Twilio environment variables)
python -m eurostar_alerts.main --config config.yml
```
