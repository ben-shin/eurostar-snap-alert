from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from .config import AppConfig
from .models import FareHit, Provider, RouteQuery

PRICE_RE = re.compile(
    r"(?P<sym>[£€])\s?(?P<amount>\d{1,4}(?:[,.]\d{1,2})?)|(?P<amount2>\d{1,4}(?:[,.]\d{1,2})?)\s?(?P<code>GBP|EUR)",
    re.IGNORECASE,
)

SNAP_CLOSED_PHRASES = [
    "you can't book with eurostar snap right now",
    "you cannot book with eurostar snap right now",
    "can't book with eurostar snap",
    "cannot book with eurostar snap",
]

NO_AVAILABILITY_PHRASES = [
    "sold out",
    "no snap",
    "not available",
    "no tickets",
    "can't book with eurostar snap",
    "cannot book with eurostar snap",
]


def log(message: str) -> None:
    print(message, flush=True)


def date_range(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def parse_prices(text: str) -> list[tuple[str, float]]:
    prices: list[tuple[str, float]] = []
    for m in PRICE_RE.finditer(text):
        if m.group("sym"):
            currency = "GBP" if m.group("sym") == "£" else "EUR"
            amount_s = m.group("amount")
        else:
            currency = m.group("code").upper()
            amount_s = m.group("amount2")

        if not amount_s:
            continue

        try:
            amount = float(amount_s.replace(",", "."))
        except ValueError:
            continue

        if 1 <= amount <= 500:
            prices.append((currency, amount))

    return prices


def cheapest_allowed_price(text: str, allowed: set[str]) -> Optional[tuple[str, float]]:
    prices = [(cur, amt) for cur, amt in parse_prices(text) if cur in allowed]
    if not prices:
        return None
    return min(prices, key=lambda item: item[1])


def accept_cookies(page: Page) -> None:
    patterns = ["Accept all", "Accept", "I agree", "Allow all", "Agree", "OK"]
    for label in patterns:
        try:
            page.get_by_role("button", name=re.compile(label, re.I)).click(timeout=1500)
            return
        except Exception:
            pass


def safe_wait(page: Page, timeout: int = 8000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass


def save_debug(page: Page, prefix: str, enabled: bool) -> None:
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


def body_text(page: Page, timeout: int = 10000) -> str:
    try:
        return page.locator("body").inner_text(timeout=timeout)
    except Exception:
        return ""


def fill_text_field(page: Page, labels: list[str], value: str) -> bool:
    for label in labels:
        try:
            field = page.get_by_label(re.compile(label, re.I)).first
            field.fill(value, timeout=2500)
            page.keyboard.press("Enter")
            return True
        except Exception:
            pass

    for label in labels:
        try:
            field = page.get_by_placeholder(re.compile(label, re.I)).first
            field.fill(value, timeout=2500)
            page.keyboard.press("Enter")
            return True
        except Exception:
            pass

    try:
        inputs = page.locator("input:not([type=hidden]):not([type=checkbox]):not([type=radio])")
        count = min(inputs.count(), 8)
        for i in range(count):
            el = inputs.nth(i)
            try:
                if el.is_visible():
                    current = el.input_value(timeout=1000)
                    if not current:
                        el.fill(value, timeout=2500)
                        page.keyboard.press("Enter")
                        return True
            except Exception:
                continue
    except Exception:
        pass

    return False


def fill_date_field(page: Page, travel_date: date) -> bool:
    iso = travel_date.isoformat()
    human = travel_date.strftime("%d/%m/%Y")

    try:
        date_inputs = page.locator("input[type=date]")
        if date_inputs.count() > 0:
            date_inputs.first.fill(iso, timeout=2500)
            return True
    except Exception:
        pass

    for value in (human, iso):
        for labels in (["date", "depart", "outbound", "travel"], ["when"]):
            if fill_text_field(page, labels, value):
                return True

    return False


def set_passengers(page: Page, passengers: int) -> None:
    if passengers == 1:
        return

    for label in ["passenger", "adult", "traveller", "traveler"]:
        try:
            page.get_by_role("button", name=re.compile(label, re.I)).first.click(timeout=2000)
            break
        except Exception:
            pass

    for _ in range(max(0, passengers - 1)):
        for plus_label in ["Add adult", "Increase", "plus", "Add passenger"]:
            try:
                page.get_by_role("button", name=re.compile(plus_label, re.I)).first.click(timeout=1500)
                break
            except Exception:
                continue


def click_search(page: Page) -> bool:
    for label in ["Search", "Find", "Continue", "Book", "Show", "See tickets"]:
        try:
            page.get_by_role("button", name=re.compile(label, re.I)).first.click(timeout=4000)
            safe_wait(page, timeout=8000)
            return True
        except Exception:
            pass
    return False


def run_with_browser(config: AppConfig) -> list[FareHit]:
    hits: list[FareHit] = []
    log("Starting Eurostar fare check")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=config.settings.headless,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        context = browser.new_context(
            user_agent=config.settings.user_agent,
            viewport={"width": 1365, "height": 900},
            locale="en-GB",
            timezone_id=config.settings.timezone,
        )
        context.set_default_timeout(config.settings.page_timeout_ms)
        page = context.new_page()

        try:
            for route in config.routes:
                log(f"Route: {route.name} | {route.origin} -> {route.destination}")

                if config.checks.get("snap", True):
                    hits.extend(check_snap(page, route, config))

                if config.checks.get("normal_eurostar", True):
                    hits.extend(check_normal_eurostar(page, route, config))
        finally:
            browser.close()

    log(f"Finished Eurostar fare check. Hits found: {len(hits)}")
    return hits


def check_snap(page: Page, route: RouteQuery, config: AppConfig) -> list[FareHit]:
    today = date.today()
    max_snap_date = today + timedelta(days=config.settings.snap_max_days_ahead)
    hits: list[FareHit] = []

    log(f"Checking Snap for {route.name}")

    for travel_date in date_range(route.start_date, route.end_date):
        if travel_date < today:
            log(f"SNAP skip past date: {route.name} {travel_date}")
            continue
        if travel_date > max_snap_date:
            log(f"SNAP skip outside Snap window: {route.name} {travel_date}")
            continue

        log(f"SNAP check: {route.name} {travel_date}")

        try:
            page.goto(route.snap_link, wait_until="domcontentloaded", timeout=30000)
            accept_cookies(page)
            safe_wait(page, timeout=8000)

            text = body_text(page, timeout=10000)
            lower = text.lower()

            if any(phrase in lower for phrase in SNAP_CLOSED_PHRASES):
                log(f"SNAP closed detected. No Snap booking form available for {route.name}. URL: {page.url}")
                return hits

            origin_ok = fill_text_field(page, ["from", "origin", "departure"], route.origin)
            dest_ok = fill_text_field(page, ["to", "destination", "arrival"], route.destination)
            date_ok = fill_date_field(page, travel_date)
            set_passengers(page, route.passengers)
            clicked = click_search(page)

            if not origin_ok or not dest_ok or not date_ok or not clicked:
                log(
                    f"SNAP form incomplete: {route.name} {travel_date} "
                    f"origin_ok={origin_ok} dest_ok={dest_ok} date_ok={date_ok} clicked={clicked}"
                )
                save_debug(page, f"snap_form_incomplete_{route.name}_{travel_date}", config.settings.debug)
                continue

            text = body_text(page, timeout=10000)
            lower = text.lower()

            if any(blocked in lower for blocked in NO_AVAILABILITY_PHRASES):
                log(f"SNAP no availability: {route.name} {travel_date}")
                continue

            found_price = cheapest_allowed_price(text, {"GBP", "EUR"})
            availability_words = any(
                w in lower for w in ["standard", "checkout", "continue", "ticket", "fare", "book"]
            )

            if found_price or availability_words:
                currency, amount = found_price if found_price else (None, None)
                log(f"SNAP potential hit: {route.name} {travel_date} {currency or ''} {amount or ''}")
                hits.append(
                    FareHit(
                        provider=Provider.SNAP,
                        route_name=route.name,
                        origin=route.origin,
                        destination=route.destination,
                        travel_date=travel_date,
                        passengers=route.passengers,
                        price_amount=amount,
                        currency=currency,
                        booking_url=page.url or route.snap_link,
                        summary="Potential Snap availability found. Open the link and verify before booking.",
                    )
                )
            else:
                log(f"SNAP checked with no hit: {route.name} {travel_date}")

        except PlaywrightTimeoutError:
            log(f"SNAP timeout: {route.name} {travel_date}")
            save_debug(page, f"snap_timeout_{route.name}_{travel_date}", config.settings.debug)
        except Exception as exc:
            log(f"SNAP error: {route.name} {travel_date} | {type(exc).__name__}: {exc}")
            save_debug(page, f"snap_error_{route.name}_{travel_date}", config.settings.debug)

    return hits


def check_normal_eurostar(page: Page, route: RouteQuery, config: AppConfig) -> list[FareHit]:
    hits: list[FareHit] = []
    threshold = config.normal_eurostar.threshold_amount
    allowed = config.normal_eurostar.allowed_currencies

    log(f"Checking normal Eurostar fares for {route.name}")

    for travel_date in date_range(route.start_date, route.end_date):
        if travel_date < date.today():
            log(f"NORMAL skip past date: {route.name} {travel_date}")
            continue

        log(f"NORMAL check: {route.name} {travel_date}")

        try:
            page.goto(route.booking_link, wait_until="domcontentloaded", timeout=30000)
            accept_cookies(page)
            safe_wait(page, timeout=8000)

            origin_ok = fill_text_field(page, ["from", "origin", "departure"], route.origin)
            dest_ok = fill_text_field(page, ["to", "destination", "arrival"], route.destination)
            date_ok = fill_date_field(page, travel_date)
            set_passengers(page, route.passengers)
            clicked = click_search(page)

            if not origin_ok or not dest_ok or not date_ok or not clicked:
                log(
                    f"NORMAL form incomplete: {route.name} {travel_date} "
                    f"origin_ok={origin_ok} dest_ok={dest_ok} date_ok={date_ok} clicked={clicked}"
                )
                save_debug(page, f"normal_form_incomplete_{route.name}_{travel_date}", config.settings.debug)
                continue

            text = body_text(page, timeout=12000)
            price = cheapest_allowed_price(text, allowed)

            if not price:
                log(f"NORMAL no parseable price: {route.name} {travel_date}")
                save_debug(page, f"normal_no_price_{route.name}_{travel_date}", config.settings.debug)
                continue

            currency, amount = price
            log(f"NORMAL cheapest seen: {route.name} {travel_date} {currency} {amount:g}")

            if amount <= threshold:
                log(f"NORMAL threshold hit: {route.name} {travel_date} {currency} {amount:g}")
                hits.append(
                    FareHit(
                        provider=Provider.NORMAL,
                        route_name=route.name,
                        origin=route.origin,
                        destination=route.destination,
                        travel_date=travel_date,
                        passengers=route.passengers,
                        price_amount=amount,
                        currency=currency,
                        booking_url=page.url or route.booking_link,
                        summary=f"Normal one-way fare appears at or below threshold {threshold:g}.",
                    )
                )

        except PlaywrightTimeoutError:
            log(f"NORMAL timeout: {route.name} {travel_date}")
            save_debug(page, f"normal_timeout_{route.name}_{travel_date}", config.settings.debug)
        except Exception as exc:
            log(f"NORMAL error: {route.name} {travel_date} | {type(exc).__name__}: {exc}")
            save_debug(page, f"normal_error_{route.name}_{travel_date}", config.settings.debug)

    return hits
