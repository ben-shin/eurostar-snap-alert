from __future__ import annotations

import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from playwright.sync_api import BrowserContext, Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from .config import AppConfig
from .models import CheckOutcome, CheckStatus, FareHit, Provider, RouteQuery, ScrapeReport


PRICE_RE = re.compile(
    r"(?:(?P<symbol>[£€])\s*(?P<symbol_amount>\d{1,4}(?:[.,]\d{1,2})?)|"
    r"(?P<code_amount>\d{1,4}(?:[.,]\d{1,2})?)\s*(?P<code>GBP|EUR))",
    re.IGNORECASE,
)

SNAP_CLOSED_PHRASES = (
    "you can't book with eurostar snap right now",
    "you cannot book with eurostar snap right now",
)
SNAP_UNAVAILABLE_PHRASES = (
    "no snap tickets are available on this date",
    "this route is currently sold out",
)
NORMAL_UNAVAILABLE_PHRASES = (
    "no trains available",
    "no tickets available",
    "no journeys available",
    "we couldn't find any trains",
)
ERROR_PHRASES = (
    "sorry, something went wrong",
    "access denied",
    "unusual activity",
)


def log(message: str) -> None:
    print(message, flush=True)


def date_range(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def parse_prices(text: str) -> list[tuple[str, float]]:
    normalized = text.replace("\xa0", " ")
    normalized = re.sub(r"([£€])\s+(\d{1,4})\s*[,.]\s*(\d{1,2})", r"\1\2.\3", normalized)
    normalized = re.sub(r"(\d{1,4})\s*[,.]\s*(\d{1,2})\s*(GBP|EUR)", r"\1.\2 \3", normalized, flags=re.I)

    prices: list[tuple[str, float]] = []
    for match in PRICE_RE.finditer(normalized):
        symbol = match.group("symbol")
        currency = "GBP" if symbol == "£" else "EUR" if symbol == "€" else match.group("code").upper()
        amount_text = match.group("symbol_amount") or match.group("code_amount")
        try:
            amount = float(amount_text.replace(",", "."))
        except (AttributeError, ValueError):
            continue
        if 1 <= amount <= 500:
            prices.append((currency, amount))
    return prices


def cheapest_allowed_price(texts: Iterable[str], allowed: set[str], minimum: float = 1) -> Optional[tuple[str, float]]:
    prices = [
        (currency, amount)
        for text in texts
        for currency, amount in parse_prices(text)
        if currency in allowed and minimum <= amount <= 500
    ]
    return min(prices, key=lambda item: item[1]) if prices else None


def _compact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()


def station_candidates(value: str) -> list[str]:
    candidates = [value, value.replace("/", " ")]
    lower = value.lower()
    if "london" in lower or "pancras" in lower:
        candidates.extend(["London St Pancras", "St Pancras"])
    if "brussels" in lower or "bruxelles" in lower:
        candidates.extend(["Brussels Midi", "Bruxelles Midi"])
    if "paris" in lower:
        candidates.extend(["Paris Gare du Nord", "Paris Nord"])
    if "amsterdam" in lower:
        candidates.extend(["Amsterdam Centraal", "Amsterdam"])
    if "rotterdam" in lower:
        candidates.extend(["Rotterdam Centraal", "Rotterdam"])
    if "lille" in lower:
        candidates.extend(["Lille Europe", "Lille"])

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = _compact(candidate)
        key = _normalize(candidate)
        if len(key) >= 3 and key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _visible_first(locator: Locator) -> Optional[Locator]:
    try:
        count = locator.count()
    except Exception:
        return None
    for index in range(count):
        item = locator.nth(index)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return None


def _accept_cookies(page: Page) -> None:
    for label in ("Accept all", "Accept", "I agree", "Allow all"):
        button = _visible_first(page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I)))
        if button is None:
            continue
        try:
            button.click(timeout=2_000)
            return
        except Exception:
            continue


def _body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5_000)
    except Exception:
        return ""


def _save_debug(page: Page, prefix: str, enabled: bool) -> None:
    if not enabled:
        return
    debug_dir = Path("debug")
    debug_dir.mkdir(exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", prefix)[:120]
    try:
        page.screenshot(path=str(debug_dir / f"{safe}.png"), full_page=True)
    except Exception:
        pass
    try:
        (debug_dir / f"{safe}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def _visible_form(page: Page, testid: str) -> Optional[Locator]:
    forms = page.locator(f'form[data-testid="{testid}"]')
    try:
        forms.first.wait_for(state="attached", timeout=15_000)
    except Exception:
        return None
    for index in range(forms.count()):
        form = forms.nth(index)
        try:
            if form.locator('button[data-testid="button-search"]').is_visible():
                return form
        except Exception:
            continue
    return None


def _matching_option(options: Locator, value: str) -> Optional[Locator]:
    desired = [_normalize(candidate) for candidate in station_candidates(value)]
    try:
        count = options.count()
    except Exception:
        return None
    for index in range(count):
        option = options.nth(index)
        try:
            if not option.is_visible():
                continue
            option_text = _normalize(option.inner_text())
        except Exception:
            continue
        if any(candidate in option_text or option_text.startswith(candidate) for candidate in desired):
            return option
    return None


def _fill_station(page: Page, form: Locator, field_testid: str, value: str) -> bool:
    field = form.locator(f'input[data-testid="{field_testid}"]').first
    for query in station_candidates(value):
        try:
            field.click(timeout=3_000)
            field.fill(query, timeout=3_000)
            controls = field.get_attribute("aria-controls")
            scope = page.locator(f'[id="{controls}"]') if controls else page.locator("body")
            options = scope.locator('[role="option"]')
            options.first.wait_for(state="visible", timeout=4_000)
            option = _matching_option(options, value)
            if option is None:
                continue
            option.click(timeout=3_000)
            page.wait_for_timeout(250)
            selected = _compact(field.input_value())
            invalid = field.get_attribute("aria-invalid")
            if selected and invalid != "true":
                log(f"Station selected: {field_testid}={selected!r}")
                return True
        except Exception:
            continue
    return False


def _calendar_navigation(page: Page, scope: Locator, direction: str) -> Optional[Locator]:
    testid = "nextNavBtn" if direction == "next" else "prevNavBtn"
    candidates = [
        scope.locator(f'[data-testid="{testid}"]'),
        page.locator(f'[data-testid="{testid}"]'),
        scope.get_by_role("button", name=re.compile(rf"{direction}.*month|month.*{direction}", re.I)),
    ]
    for candidate in candidates:
        item = _visible_first(candidate)
        if item is not None:
            return item
    return None


def _set_outbound_date(page: Page, form: Locator, travel_date: date) -> bool:
    iso = travel_date.isoformat()
    button = form.locator('button[data-testid="start-date"]').first
    try:
        current_text = button.get_attribute("data-date")
        if current_text == iso:
            return True
        current_date = date.fromisoformat(current_text) if current_text else travel_date
        controls = button.get_attribute("aria-controls")
        button.click(timeout=4_000)
        page.wait_for_timeout(300)
    except Exception:
        return False

    scope = page.locator(f'[id="{controls}"]') if controls else page.locator("body")
    direction = "next" if travel_date >= current_date else "previous"
    for _ in range(14):
        target = _visible_first(scope.locator(f'[role="button"][data-date="{iso}"]'))
        if target is not None:
            try:
                target.click(timeout=4_000)
                page.wait_for_timeout(300)
                return button.get_attribute("data-date") == iso
            except Exception:
                return False
        navigation = _calendar_navigation(page, scope, "next" if direction == "next" else "previous")
        if navigation is None:
            break
        try:
            navigation.click(timeout=3_000)
            page.wait_for_timeout(300)
        except Exception:
            break
    return False


def _set_passengers(page: Page, form: Locator, passengers: int) -> bool:
    if passengers == 1:
        return True
    opener = _visible_first(form.locator('[data-testid="train-travellers-selector"]'))
    if opener is not None:
        try:
            opener.click(timeout=3_000)
        except Exception:
            return False
    for _ in range(passengers - 1):
        add_button = _visible_first(page.locator('[data-testid="adult-add"]'))
        if add_button is None:
            return False
        try:
            add_button.click(timeout=3_000)
        except Exception:
            return False
    return True


def _submit_search(page: Page, form: Locator) -> bool:
    previous_url = page.url
    try:
        button = form.locator('button[data-testid="button-search"]').first
        if button.is_disabled():
            return False
        button.click(timeout=5_000)
        try:
            page.wait_for_url(re.compile(r"/search(?:/|\?)"), timeout=15_000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(1_000)
        return page.url != previous_url and "/search" in page.url
    except Exception:
        return False


def _captcha_visible(page: Page) -> bool:
    dialog = _visible_first(page.locator('[data-testid="captcha-dialog"]'))
    return dialog is not None


def _url_has_date(page: Page, travel_date: date) -> bool:
    outbound = parse_qs(urlparse(page.url).query).get("outbound", [])
    return travel_date.isoformat() in outbound


def _wait_for_result(page: Page, provider: Provider, travel_date: date, timeout_seconds: int = 18) -> None:
    deadline = time.monotonic() + timeout_seconds
    iso = travel_date.isoformat()
    while time.monotonic() < deadline:
        text = _body_text(page)
        lower = text.lower()
        if _captcha_visible(page) or any(phrase in lower for phrase in ERROR_PHRASES):
            return
        if provider == Provider.SNAP:
            exact_prices = page.locator(f'[data-testid^="{iso}-outbound-"][data-testid$="-price"]')
            if exact_prices.count() or any(phrase in lower for phrase in SNAP_UNAVAILABLE_PHRASES):
                return
        else:
            if any(phrase in lower for phrase in NORMAL_UNAVAILABLE_PHRASES):
                return
            if cheapest_allowed_price([text], {"GBP", "EUR"}, minimum=30):
                return
        page.wait_for_timeout(500)


def _open_and_fill(
    page: Page,
    route: RouteQuery,
    travel_date: date,
    provider: Provider,
) -> tuple[Optional[Locator], str]:
    url = route.snap_link if provider == Provider.SNAP else route.booking_link
    form_testid = "booking-magnet-form-snap" if provider == Provider.SNAP else "booking-magnet-form-trains"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=35_000)
    except PlaywrightTimeoutError:
        return None, "page load timed out"
    _accept_cookies(page)
    form = _visible_form(page, form_testid)
    if form is None:
        lower = _body_text(page).lower()
        if provider == Provider.SNAP and any(phrase in lower for phrase in SNAP_CLOSED_PHRASES):
            return None, "Snap booking is currently closed"
        return None, f"{form_testid} was not available"
    if not _fill_station(page, form, "origin-field", route.origin):
        return None, f"could not select origin {route.origin!r}"
    if not _fill_station(page, form, "destination-field", route.destination):
        return None, f"could not select destination {route.destination!r}"
    if not _set_outbound_date(page, form, travel_date):
        return None, f"could not select outbound date {travel_date.isoformat()}"
    if not _set_passengers(page, form, route.passengers):
        return None, f"could not set {route.passengers} passengers"
    return form, ""


def _failed(provider: Provider, route: RouteQuery, travel_date: date, message: str) -> CheckOutcome:
    log(f"{provider.value.upper()} failed: {route.name} {travel_date} | {message}")
    return CheckOutcome(provider, route.name, travel_date, CheckStatus.FAILED, message)


def _check_snap(page: Page, route: RouteQuery, travel_date: date, config: AppConfig) -> CheckOutcome:
    form, problem = _open_and_fill(page, route, travel_date, Provider.SNAP)
    if form is None:
        if problem == "Snap booking is currently closed":
            return CheckOutcome(Provider.SNAP, route.name, travel_date, CheckStatus.UNAVAILABLE, problem)
        _save_debug(page, f"snap_setup_{route.name}_{travel_date}", config.settings.debug)
        return _failed(Provider.SNAP, route, travel_date, problem)
    if not _submit_search(page, form):
        _save_debug(page, f"snap_submit_{route.name}_{travel_date}", config.settings.debug)
        return _failed(Provider.SNAP, route, travel_date, "search did not navigate to results")

    _wait_for_result(page, Provider.SNAP, travel_date)
    text = _body_text(page)
    lower = text.lower()
    if _captcha_visible(page):
        problem = "Eurostar displayed a CAPTCHA"
    elif any(phrase in lower for phrase in ERROR_PHRASES):
        problem = "Eurostar returned an error page"
    elif not _url_has_date(page, travel_date):
        problem = "results URL does not contain the requested date"
    else:
        problem = ""
    if problem:
        _save_debug(page, f"snap_result_{route.name}_{travel_date}", config.settings.debug)
        return _failed(Provider.SNAP, route, travel_date, problem)

    if any(phrase in lower for phrase in SNAP_UNAVAILABLE_PHRASES):
        log(f"SNAP unavailable: {route.name} {travel_date}")
        return CheckOutcome(Provider.SNAP, route.name, travel_date, CheckStatus.UNAVAILABLE, "No Snap tickets")

    iso = travel_date.isoformat()
    price_elements = page.locator(f'[data-testid^="{iso}-outbound-"][data-testid$="-price"]')
    price_texts = price_elements.all_inner_texts()
    price = cheapest_allowed_price(price_texts, {"GBP", "EUR"})
    if price is None:
        _save_debug(page, f"snap_unrecognized_{route.name}_{travel_date}", config.settings.debug)
        return _failed(Provider.SNAP, route, travel_date, "result contained neither exact-date fares nor unavailability")

    currency, amount = price
    hit = FareHit(
        provider=Provider.SNAP,
        route_name=route.name,
        origin=route.origin,
        destination=route.destination,
        travel_date=travel_date,
        passengers=route.passengers,
        price_amount=amount,
        currency=currency,
        booking_url=page.url,
        summary="Exact-date Snap availability found. Verify the fare before booking.",
    )
    log(f"SNAP available: {route.name} {travel_date} {currency} {amount:g}")
    return CheckOutcome(Provider.SNAP, route.name, travel_date, CheckStatus.AVAILABLE, f"{currency} {amount:g}", hit)


def _normal_price_texts(page: Page) -> list[str]:
    targeted = page.locator('[data-testid*="price" i]')
    texts: list[str] = []
    try:
        for index in range(targeted.count()):
            item = targeted.nth(index)
            if item.is_visible():
                texts.append(item.inner_text())
    except Exception:
        pass
    return texts or [_body_text(page)]


def _check_normal(page: Page, route: RouteQuery, travel_date: date, config: AppConfig) -> CheckOutcome:
    form, problem = _open_and_fill(page, route, travel_date, Provider.NORMAL)
    if form is None:
        _save_debug(page, f"normal_setup_{route.name}_{travel_date}", config.settings.debug)
        return _failed(Provider.NORMAL, route, travel_date, problem)
    if not _submit_search(page, form):
        _save_debug(page, f"normal_submit_{route.name}_{travel_date}", config.settings.debug)
        return _failed(Provider.NORMAL, route, travel_date, "search did not navigate to results")

    _wait_for_result(page, Provider.NORMAL, travel_date)
    text = _body_text(page)
    lower = text.lower()
    if _captcha_visible(page):
        problem = "Eurostar displayed a CAPTCHA"
    elif any(phrase in lower for phrase in ERROR_PHRASES):
        problem = "Eurostar returned an error page"
    elif not _url_has_date(page, travel_date):
        problem = "results URL does not contain the requested date"
    else:
        problem = ""
    if problem:
        _save_debug(page, f"normal_result_{route.name}_{travel_date}", config.settings.debug)
        return _failed(Provider.NORMAL, route, travel_date, problem)

    if any(phrase in lower for phrase in NORMAL_UNAVAILABLE_PHRASES):
        log(f"NORMAL unavailable: {route.name} {travel_date}")
        return CheckOutcome(Provider.NORMAL, route.name, travel_date, CheckStatus.UNAVAILABLE, "No normal fares")

    allowed = set(config.normal_eurostar.allowed_currencies)
    price = cheapest_allowed_price(_normal_price_texts(page), allowed, minimum=30)
    if price is None:
        _save_debug(page, f"normal_unrecognized_{route.name}_{travel_date}", config.settings.debug)
        return _failed(Provider.NORMAL, route, travel_date, "results page contained no parseable fare")

    currency, amount = price
    if amount > config.normal_eurostar.threshold_amount:
        log(f"NORMAL checked: {route.name} {travel_date} {currency} {amount:g}")
        return CheckOutcome(
            Provider.NORMAL,
            route.name,
            travel_date,
            CheckStatus.UNAVAILABLE,
            f"cheapest {currency} {amount:g} exceeds threshold",
        )

    hit = FareHit(
        provider=Provider.NORMAL,
        route_name=route.name,
        origin=route.origin,
        destination=route.destination,
        travel_date=travel_date,
        passengers=route.passengers,
        price_amount=amount,
        currency=currency,
        booking_url=page.url,
        summary=f"Normal one-way fare is at or below {config.normal_eurostar.threshold_amount:g}.",
    )
    log(f"NORMAL available: {route.name} {travel_date} {currency} {amount:g}")
    return CheckOutcome(Provider.NORMAL, route.name, travel_date, CheckStatus.AVAILABLE, f"{currency} {amount:g}", hit)


def _new_page(context: BrowserContext, timeout_ms: int) -> Page:
    page = context.new_page()
    page.set_default_timeout(timeout_ms)
    return page


def run_with_browser(config: AppConfig) -> ScrapeReport:
    outcomes: list[CheckOutcome] = []
    today = datetime.now(ZoneInfo(config.settings.timezone)).date()
    max_snap_date = today + timedelta(days=config.settings.snap_max_days_ahead)
    log(f"Starting Eurostar fare check for {today} ({config.settings.timezone})")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=config.settings.headless,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        context_options: dict[str, object] = {
            "viewport": {"width": 1365, "height": 900},
            "locale": "en-GB",
            "timezone_id": config.settings.timezone,
        }
        if config.settings.user_agent:
            context_options["user_agent"] = config.settings.user_agent
        context = browser.new_context(**context_options)

        try:
            for route in config.routes:
                for travel_date in date_range(route.start_date, route.end_date):
                    if travel_date < today:
                        continue
                    if config.checks.get("snap", True) and travel_date <= max_snap_date:
                        page = _new_page(context, config.settings.page_timeout_ms)
                        try:
                            outcomes.append(_check_snap(page, route, travel_date, config))
                        except Exception as exc:
                            _save_debug(page, f"snap_exception_{route.name}_{travel_date}", config.settings.debug)
                            outcomes.append(_failed(Provider.SNAP, route, travel_date, f"{type(exc).__name__}: {exc}"))
                        finally:
                            page.close()
                    if config.checks.get("normal_eurostar", True):
                        page = _new_page(context, config.settings.page_timeout_ms)
                        try:
                            outcomes.append(_check_normal(page, route, travel_date, config))
                        except Exception as exc:
                            _save_debug(page, f"normal_exception_{route.name}_{travel_date}", config.settings.debug)
                            outcomes.append(_failed(Provider.NORMAL, route, travel_date, f"{type(exc).__name__}: {exc}"))
                        finally:
                            page.close()
        finally:
            browser.close()

    report = ScrapeReport(outcomes)
    log(
        "Finished Eurostar fare check: "
        f"attempted={report.attempted} completed={report.completed} "
        f"failed={len(report.failures)} hits={len(report.hits)}"
    )
    return report
