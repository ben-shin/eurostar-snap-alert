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

- `snap_max_days_ahead`: Snap checks run from tomorrow through this many days ahead (inclusive). Snap disables same-day departures; normal-fare checks still include today.
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

## Phone controls (WhatsApp)

Instant replies run in a protected Twilio Function. Search settings live in a
Twilio Sync document, so the phone and GitHub scanner share the same state.
Only the configured TWILIO_TO_WHATSAPP phone can control this bot. Twilio
validates request signatures, and message IDs prevent webhook retry replay.

Deploy with the **Deploy and test WhatsApp bot** GitHub workflow. It uses the
existing four Twilio secrets, seeds searches from config.yml on first deployment,
and saves non-secret connection metadata in data/twilio-control.json. Redeploying
preserves existing phone settings. The deployment tests signed access, rejects
unsigned requests and checks the authorized phone restriction.

After deployment, copy the URL from the workflow summary into Twilio Console →
Messaging → Try it out → Send a WhatsApp message → Sandbox settings →
**When a message comes in**, select **POST**, and save. For a production WhatsApp
sender, set its inbound webhook instead. No computer needs to stay on.

Send these messages from your configured phone:

| Message | Effect |
| --- | --- |
| HELP | Commands and available city names |
| TEST | Immediate reply proving inbound commands work |
| STATUS or SEARCH LIST | Show IDs, parameters and active/paused/expired searches |
| SCAN STOP | Pause all scans and fare alerts |
| SCAN START | Resume active, unexpired searches |
| SEARCH STOP s1 | Pause one search |
| SEARCH START s1 | Resume one search |
| SEARCH DELETE s1 | Remove one search |

Add or edit with key=value fields (city names are case-insensitive):

    SEARCH ADD from=Brussels to=London start=2026-10-05 end=2026-10-10
    SEARCH SET s1 start=2026-10-06 end=2026-10-12 passengers=2 max=60 mode=snap

Supported cities: London, Brussels, Paris, Amsterdam, Rotterdam and Lille.
mode is snap, normal or both. max applies to normal fares in the configured
currencies; no FX conversion is performed. Normal-fare scraping remains optional
and may fail when Eurostar serves its generic automated-runner error page.

Controls reply immediately. Fare scans still use the GitHub schedule (every
15 minutes requested, with possible GitHub delays). SCAN START does not dispatch
an immediate scan. The scanner rereads settings before each date check and each
alert, so a stop or edit interrupts an ongoing search after its in-flight request.
Already queued WhatsApp messages cannot be recalled.

Each search expires automatically after its end date in Europe/London time
(or the configured timezone). Its end date is inclusive; Snap still excludes
same-day departures. An expired search remains listed but is never scanned.
Extend its dates to use it again. If all searches are paused/expired, no browser
starts. The schedule remains enabled so new searches can resume automatically.
Deleting a search does not reuse its ID. Limits are eight searches and a 61-day
window per search; broad normal-fare searches may reach the workflow time limit.

After phone controls are connected, the Sync document is authoritative for search
parameters. Editing config.yml no longer changes those searches. Other settings,
including allowed currencies and the Snap window, still come from YAML.
Unavailable control storage fails the run rather than silently using stale settings.
For a local YAML-only diagnostic, use --dry-run --local-config.

### Test delivery and WhatsApp setup

Run **Deploy and test WhatsApp bot** with deploy=false and send_test=true to send
one test message. The test checks delivery for up to 45 seconds and distinguishes
delivered/read from queued/sent and failed/undelivered. It logs error codes, never
secret values or full message bodies. Send TEST on the phone to verify the
complete inbound-and-reply path after connecting the webhook.

If using the Twilio Sandbox:

- Join with the exact join phrase shown in your Twilio Console.
- Membership expires after three days; rejoin when needed (error 63015).
- A phone message opens a 24-hour window for free-form replies and fare alerts.
  Send TEST to reopen it if needed (error 63016).
- Use SCAN STOP to pause. Bare STOP disconnects the Sandbox.
- Reliable unattended alerts outside that window require a production WhatsApp
  sender and an approved message template; this bot currently sends free-form text.

See [Twilio Sandbox documentation](https://www.twilio.com/docs/whatsapp/sandbox)
and [incoming webhook setup](https://www.twilio.com/docs/whatsapp/quickstart).
