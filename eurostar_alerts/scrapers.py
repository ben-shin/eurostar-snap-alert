from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from .config import AppConfig
from .models import FareHit, Provider, RouteQuery

PRICE_RE = re.compile(
    r"(?P<sym>[£€])\s?(?P<amount>\d{1,4}(?:[,.]\d{1,2})?)|(?P<amount2>\d{1,4}(?:[,.]\d{1,2})?)\s?(?P<code>GBP|EUR)",
    re.IGNORECASE,
)


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
        # Avoid accidentally treating years/dates as prices if a code/symbol slipped through weird text.
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


def fill_text_field(page: Page, labels: list[str], value: str) -> bool:
    # Try accessible labels first.
    for label in labels:
        try:
            field = page.get_by_label(re.compile(label, re.I)).first
            field.fill(value, timeout=2500)
            page.keyboard.press("Enter")
            return True
        except Exception:
            pass

    # Then placeholders.
    for label in labels:
        try:
            field = page.get_by_placeholder(re.compile(label, re.I)).first
            field.fill(value, timeout=2500)
            page.keyboard.press("Enter")
            return True
        except Exception:
            pass

    # Last resort: use the nth visible text/search input.
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

    # HTML date inputs are the most reliable if present.
    try:
        date_inputs = page.locator("input[type=date]")
        if date_inputs.count() > 0:
            date_inputs.first.fill(iso, timeout=2500)
            return True
    except Exception:
        pass

    # Accessible/placeholder date fields.
    for value in (human, iso):
        for labels in (["date", "depart", "outbound", "travel"], ["when"]):
            if fill_text_field(page, labels, value):
                return True

    return False


def set_passengers(page: Page, passengers: int) -> None:
    if passengers == 1:
        return
    # Generic attempt: open passenger dropdown and click plus buttons until requested count.
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


def click_search(page: Page) -> None:
    for label in ["Search", "Find", "Continue", "Book", "Show", "See tickets"]:
        try:
            page.get_by_role("button", name=re.compile(label, re.I)).first.click(timeout=4000)
            page.wait_for_load_state("networkidle", timeout=15000)
            return
        except Exception:
            pass


def run_with_browser(config: AppConfig) -> list[FareHit]:
    hits: list[FareHit] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=config.settings.headless)
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
                if config.checks.get("snap", True):
                    hits.extend(check_snap(page, route, config))
                if config.checks.get("normal_eurostar", True):
                    hits.extend(check_normal_eurostar(page, route, config))
        finally:
            browser.close()
    return hits


def check_snap(page: Page, route: RouteQuery, config: AppConfig) -> list[FareHit]:
    today = date.today()
    max_snap_date = today + timedelta(days=config.settings.snap_max_days_ahead)
    hits: list[FareHit] = []

    for travel_date in date_range(route.start_date, route.end_date):
        if travel_date < today or travel_date > max_snap_date:
            continue
        try:
            page.goto(route.snap_link, wait_until="domcontentloaded")
            accept_cookies(page)
            page.wait_for_load_state("networkidle", timeout=15000)

            fill_text_field(page, ["from", "origin", "departure"], route.origin)
            fill_text_field(page, ["to", "destination", "arrival"], route.destination)
            fill_date_field(page, travel_date)
            set_passengers(page, route.passengers)
            click_search(page)

            text = page.locator("body").inner_text(timeout=10000)
            lower = text.lower()
            if any(blocked in lower for blocked in ["sold out", "no snap", "not available", "no tickets"]):
                continue

            found_price = cheapest_allowed_price(text, {"GBP", "EUR"})
            availability_words = any(w in lower for w in ["snap", "standard", "checkout", "continue", "ticket", "fare", "book"])
            if found_price or availability_words:
                currency, amount = found_price if found_price else (None, None)
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
        except PlaywrightTimeoutError:
            save_debug(page, f"snap_timeout_{route.name}_{travel_date}", config.settings.debug)
        except Exception:
            save_debug(page, f"snap_error_{route.name}_{travel_date}", config.settings.debug)
    return hits


def check_normal_eurostar(page: Page, route: RouteQuery, config: AppConfig) -> list[FareHit]:
    hits: list[FareHit] = []
    threshold = config.normal_eurostar.threshold_amount
    allowed = config.normal_eurostar.allowed_currencies

    for travel_date in date_range(route.start_date, route.end_date):
        if travel_date < date.today():
            continue
        try:
            page.goto(route.booking_link, wait_until="domcontentloaded")
            accept_cookies(page)
            page.wait_for_load_state("networkidle", timeout=15000)

            fill_text_field(page, ["from", "origin", "departure"], route.origin)
            fill_text_field(page, ["to", "destination", "arrival"], route.destination)
            fill_date_field(page, travel_date)
            set_passengers(page, route.passengers)
            click_search(page)

            text = page.locator("body").inner_text(timeout=12000)
            price = cheapest_allowed_price(text, allowed)
            if not price:
                continue
            currency, amount = price
            if amount <= threshold:
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
            save_debug(page, f"normal_timeout_{route.name}_{travel_date}", config.settings.debug)
        except Exception:
            save_debug(page, f"normal_error_{route.name}_{travel_date}", config.settings.debug)
    return hits
