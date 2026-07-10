from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

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


def visible_first(locator: Locator, timeout: int = 500) -> Optional[Locator]:
    try:
        count = locator.count()
    except Exception:
        return None

    for i in range(count):
        item = locator.nth(i)
        try:
            if item.is_visible(timeout=timeout):
                return item
        except Exception:
            continue

    return None


def station_candidates(value: str) -> list[str]:
    candidates = [value]

    cleaned = value.replace("/", " ").replace("'", "")
    candidates.append(cleaned)

    lower = value.lower()

    if "london" in lower and "pancras" in lower:
        candidates.extend(["London St Pancras", "St Pancras", "London"])

    if "brussels" in lower or "bruxelles" in lower:
        candidates.extend(["Brussels Midi", "Bruxelles Midi", "Brussels", "Bruxelles"])

    if "paris" in lower:
        candidates.extend(["Paris Gare du Nord", "Paris Nord", "Paris"])

    if "amsterdam" in lower:
        candidates.extend(["Amsterdam Centraal", "Amsterdam"])

    if "rotterdam" in lower:
        candidates.extend(["Rotterdam Centraal", "Rotterdam"])

    if "lille" in lower:
        candidates.extend(["Lille Europe", "Lille"])

    seen: set[str] = set()
    unique: list[str] = []

    for c in candidates:
        c = re.sub(r"\s+", " ", c).strip()
        if len(c) < 3:
            continue

        key = c.lower()
        if key not in seen:
            seen.add(key)
            unique.append(c)

    return unique


def choose_station_option(page: Page, value: str) -> bool:
    for term in station_candidates(value):
        escaped = re.escape(term)

        patterns = [
            page.get_by_role("option", name=re.compile(escaped, re.I)),
            page.get_by_role("button", name=re.compile(escaped, re.I)),
            page.locator("[role='option']").filter(has_text=re.compile(escaped, re.I)),
            page.locator("li").filter(has_text=re.compile(escaped, re.I)),
        ]

        for locator in patterns:
            item = visible_first(locator, timeout=700)
            if item is None:
                continue

            try:
                item.click(timeout=2000)
                return True
            except Exception:
                continue

    return False


def fill_station_field(page: Page, form: Locator, field_testid: str, value: str) -> bool:
    field = form.locator(f'input[data-testid="{field_testid}"]').first

    terms = station_candidates(value)

    for term in terms:
        try:
            field.click(timeout=4000)
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.wait_for_timeout(300)

            field.fill(term, timeout=4000)
            page.wait_for_timeout(1200)

            selected = choose_station_option(page, term)

            if not selected:
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(300)
                page.keyboard.press("Enter")
                page.wait_for_timeout(800)

            current = field.input_value(timeout=2000).strip()

            if current:
                log(
                    f"Station selected for {field_testid}: "
                    f"requested={value!r}, typed={term!r}, got={current!r}"
                )
                return True

        except Exception:
            continue

    try:
        current = field.input_value(timeout=2000).strip()
        log(f"Station selection failed for {field_testid}: requested={value!r}, final={current!r}")
    except Exception:
        log(f"Station selection failed for {field_testid}: requested={value!r}, final=unknown")

    return False


def click_calendar_next(page: Page, scope: Optional[Locator] = None) -> bool:
    search_scope = scope if scope is not None else page.locator("body")

    candidates = [
        search_scope.get_by_role("button", name=re.compile(r"next", re.I)),
        search_scope.locator("button[aria-label*='Next']"),
        search_scope.locator("button").filter(has_text=re.compile(r"^\s*[›>]+\s*$")),
    ]

    for locator in candidates:
        item = visible_first(locator, timeout=700)
        if item is None:
            continue

        try:
            item.click(timeout=2000)
            page.wait_for_timeout(700)
            return True
        except Exception:
            continue

    return False

def click_locator_or_clickable_parent(item: Locator, page: Page, iso: str) -> bool:
    """
    Calendar dates are sometimes rendered as spans/divs inside clickable wrappers,
    not as direct buttons. Try normal click, force click, then JS-click the nearest
    button or role=button ancestor.
    """
    try:
        item.scroll_into_view_if_needed(timeout=1000)
    except Exception:
        pass

    try:
        item.click(timeout=2500)
        page.wait_for_timeout(1000)
        log(f"Clicked calendar element for {iso}")
        return True
    except Exception:
        pass

    try:
        item.click(timeout=2500, force=True)
        page.wait_for_timeout(1000)
        log(f"Force-clicked calendar element for {iso}")
        return True
    except Exception:
        pass

    try:
        item.evaluate(
            """
            el => {
                const clickable = el.closest('button, [role="button"], [tabindex]');
                if (clickable) clickable.click();
                else el.click();
            }
            """
        )
        page.wait_for_timeout(1000)
        log(f"JS-clicked calendar element/parent for {iso}")
        return True
    except Exception:
        return False

def select_calendar_date(page: Page, travel_date: date, scope: Optional[Locator] = None) -> bool:
    iso = travel_date.isoformat()
    day = str(travel_date.day)
    day_padded = travel_date.strftime("%d")
    month_full = travel_date.strftime("%B")
    month_short = travel_date.strftime("%b")
    year = str(travel_date.year)

    search_scope = scope if scope is not None else page.locator("body")

    for month_step in range(14):
        log(f"Trying to select date {iso}; calendar month step {month_step}")

        # First: dump some useful diagnostics.
        try:
            scope_text = search_scope.inner_text(timeout=1500)
            compact = re.sub(r"\s+", " ", scope_text).strip()
            log(f"Calendar scope text sample: {compact[:300]!r}")
        except Exception:
            log("Could not read calendar scope text")

        # Broad search: dates may be buttons, divs, spans, gridcells, or nested text.
        candidate_locators = [
            search_scope.locator(f'[data-date="{iso}"]'),
            search_scope.locator(f'[aria-label*="{iso}"]'),
            search_scope.locator(f'[data-testid*="{iso}"]'),

            search_scope.get_by_role(
                "button",
                name=re.compile(
                    rf"({re.escape(day)}|{re.escape(day_padded)}).*(?:{re.escape(month_full)}|{re.escape(month_short)}).*{year}",
                    re.I,
                ),
            ),
            search_scope.get_by_role(
                "gridcell",
                name=re.compile(
                    rf"({re.escape(day)}|{re.escape(day_padded)}).*(?:{re.escape(month_full)}|{re.escape(month_short)}).*{year}",
                    re.I,
                ),
            ),

            search_scope.get_by_text(re.compile(rf"^\s*{re.escape(day)}\s*$")),
            search_scope.get_by_text(re.compile(rf"^\s*{re.escape(day_padded)}\s*$")),

            search_scope.locator("button").filter(has_text=re.compile(rf"^\s*{re.escape(day)}\s*$")),
            search_scope.locator("[role='button']").filter(has_text=re.compile(rf"^\s*{re.escape(day)}\s*$")),
            search_scope.locator("[role='gridcell']").filter(has_text=re.compile(rf"^\s*{re.escape(day)}\s*$")),
            search_scope.locator("td").filter(has_text=re.compile(rf"^\s*{re.escape(day)}\s*$")),
            search_scope.locator("div").filter(has_text=re.compile(rf"^\s*{re.escape(day)}\s*$")),
            search_scope.locator("span").filter(has_text=re.compile(rf"^\s*{re.escape(day)}\s*$")),
        ]

        for locator in candidate_locators:
            try:
                count = min(locator.count(), 20)
            except Exception:
                count = 0

            for i in range(count):
                item = locator.nth(i)

                try:
                    if not item.is_visible(timeout=300):
                        continue
                except Exception:
                    continue

                if click_locator_or_clickable_parent(item, page, iso):
                    return True

        if not click_calendar_next(page, search_scope):
            log(f"Could not find next-month button while selecting {iso}")
            break

    return False
    
def keyboard_select_calendar_date(page: Page, current_iso: Optional[str], travel_date: date) -> bool:
    if not current_iso:
        return False

    try:
        current_date = date.fromisoformat(current_iso)
    except Exception:
        return False

    delta_days = (travel_date - current_date).days

    if abs(delta_days) > 370:
        log(f"Keyboard date fallback skipped: delta too large: {delta_days}")
        return False

    key = "ArrowRight" if delta_days > 0 else "ArrowLeft"
    steps = abs(delta_days)

    log(f"Keyboard date fallback: moving {steps} days with {key}")

    try:
        page.wait_for_timeout(500)

        for _ in range(steps):
            page.keyboard.press(key)
            page.wait_for_timeout(60)

        page.keyboard.press("Enter")
        page.wait_for_timeout(1200)
        return True

    except Exception as exc:
        log(f"Keyboard date fallback failed: {type(exc).__name__}: {exc}")
        return False

def click_calendar_date_by_coordinates(
    page: Page,
    popover_id: Optional[str],
    travel_date: date,
) -> bool:
    month_label = travel_date.strftime("%B %Y")
    day = str(travel_date.day)
    iso = travel_date.isoformat()

    log(f"Coordinate date fallback: target={iso}, month_label={month_label!r}, day={day!r}")

    try:
        point = page.evaluate(
            """
            ({ popoverId, monthLabel, day }) => {
                const root = popoverId
                    ? document.getElementById(popoverId)
                    : document.body;

                if (!root) return null;

                const isVisible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return (
                        style &&
                        style.visibility !== 'hidden' &&
                        style.display !== 'none' &&
                        rect.width > 0 &&
                        rect.height > 0
                    );
                };

                const textOf = (el) => (el.textContent || '').replace(/\\s+/g, ' ').trim();

                const all = Array.from(root.querySelectorAll('*')).filter(isVisible);

                const rootRect = root.getBoundingClientRect();

                const monthHeadings = all
                    .filter(el => textOf(el) === monthLabel)
                    .map(el => {
                        const r = el.getBoundingClientRect();
                        return {
                            el,
                            left: r.left,
                            right: r.right,
                            top: r.top,
                            bottom: r.bottom,
                            cx: r.left + r.width / 2,
                            cy: r.top + r.height / 2,
                            text: textOf(el)
                        };
                    })
                    .sort((a, b) => a.cx - b.cx || a.cy - b.cy);

                let leftBound = rootRect.left;
                let rightBound = rootRect.right;
                let topBound = rootRect.top;

                if (monthHeadings.length > 0) {
                    const heading = monthHeadings[0];
                    topBound = heading.bottom;

                    const allMonthHeadings = all
                        .filter(el => /^[A-Za-z]+\\s+\\d{4}$/.test(textOf(el)))
                        .map(el => {
                            const r = el.getBoundingClientRect();
                            return {
                                el,
                                text: textOf(el),
                                left: r.left,
                                right: r.right,
                                top: r.top,
                                bottom: r.bottom,
                                cx: r.left + r.width / 2,
                                cy: r.top + r.height / 2
                            };
                        })
                        .sort((a, b) => a.cx - b.cx || a.cy - b.cy);

                    const idx = allMonthHeadings.findIndex(h => h.text === monthLabel);

                    if (idx >= 0) {
                        const current = allMonthHeadings[idx];
                        const prev = allMonthHeadings[idx - 1];
                        const next = allMonthHeadings[idx + 1];

                        if (prev) {
                            leftBound = (prev.cx + current.cx) / 2;
                        }

                        if (next) {
                            rightBound = (current.cx + next.cx) / 2;
                        }
                    }
                }

                const dayCandidates = all
                    .filter(el => textOf(el) === day)
                    .map(el => {
                        const r = el.getBoundingClientRect();
                        const cx = r.left + r.width / 2;
                        const cy = r.top + r.height / 2;

                        return {
                            tag: el.tagName,
                            role: el.getAttribute('role'),
                            text: textOf(el),
                            x: cx,
                            y: cy,
                            left: r.left,
                            right: r.right,
                            top: r.top,
                            bottom: r.bottom,
                            width: r.width,
                            height: r.height
                        };
                    })
                    .filter(c =>
                        c.x >= leftBound &&
                        c.x <= rightBound &&
                        c.y > topBound &&
                        c.y <= rootRect.bottom
                    )
                    .sort((a, b) => {
                        // Prefer small date-cell-like elements over large wrappers.
                        const areaA = a.width * a.height;
                        const areaB = b.width * b.height;
                        return areaA - areaB || a.y - b.y;
                    });

                if (dayCandidates.length === 0) {
                    return {
                        found: false,
                        reason: 'no day candidate',
                        rootText: textOf(root).slice(0, 500),
                        leftBound,
                        rightBound,
                        topBound,
                        rootBottom: rootRect.bottom
                    };
                }

                return {
                    found: true,
                    candidate: dayCandidates[0],
                    allCandidates: dayCandidates.slice(0, 5),
                    leftBound,
                    rightBound,
                    topBound
                };
            }
            """,
            {
                "popoverId": popover_id,
                "monthLabel": month_label,
                "day": day,
            },
        )
    except Exception as exc:
        log(f"Coordinate date fallback JS failed: {type(exc).__name__}: {exc}")
        return False

    log(f"Coordinate date fallback result: {point!r}")

    if not point or not point.get("found"):
        return False

    candidate = point["candidate"]
    x = candidate["x"]
    y = candidate["y"]

    try:
        page.mouse.click(x, y)
        page.wait_for_timeout(1200)
        log(f"Coordinate-clicked date {iso} at x={x}, y={y}")
        return True
    except Exception as exc:
        log(f"Coordinate date fallback click failed: {type(exc).__name__}: {exc}")
        return False

def set_normal_outbound_date(page: Page, form: Locator, travel_date: date) -> bool:
    iso = travel_date.isoformat()
    date_button = form.locator('button[data-testid="start-date"]').first

    current_before: Optional[str] = None

    try:
        current_before = date_button.get_attribute("data-date", timeout=2000)
        log(f"Current outbound date before selection: {current_before!r}; target={iso!r}")
        if current_before == iso:
            return True
    except Exception:
        log("Could not read current outbound date before selection")

    try:
        date_button.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass

    popover_id = None
    try:
        popover_id = date_button.get_attribute("aria-controls", timeout=1000)
    except Exception:
        pass

    try:
        date_button.click(timeout=4000)
        page.wait_for_timeout(1200)
    except Exception:
        try:
            date_button.click(timeout=4000, force=True)
            page.wait_for_timeout(1200)
        except Exception:
            log("Could not click outbound date button")
            return False

    try:
        expanded = date_button.get_attribute("aria-expanded", timeout=1000)
        log(f"Date button aria-expanded after click: {expanded!r}; popover_id={popover_id!r}")
    except Exception:
        pass

    scope: Optional[Locator] = None

    if popover_id:
        try:
            possible_scope = page.locator(f'[id="{popover_id}"]')
            if possible_scope.count() > 0:
                scope = possible_scope
                log(f"Using date popover scope: {popover_id}")
        except Exception:
            scope = None

    selected = select_calendar_date(page, travel_date, scope=scope)

    if not selected and scope is not None:
        log("Date selection failed inside scoped popover; retrying on whole page")
        selected = select_calendar_date(page, travel_date, scope=None)

    if not selected:
        log(f"Click-based date selection failed for {iso}; trying keyboard fallback")
        selected = keyboard_select_calendar_date(page, current_before, travel_date)

    if not selected:
        log(f"Could not click or keyboard-select target calendar date: {iso}")
        return False

    try:
        page.wait_for_timeout(1200)
        updated = date_button.get_attribute("data-date", timeout=3000)
        log(f"Current outbound date after selection: {updated!r}; target={iso!r}")
        if updated == iso:
            return True
    except Exception:
        pass

    try:
        visible_text = date_button.inner_text(timeout=3000).lower()
        log(f"Outbound date visible text after selection: {visible_text!r}")

        day_ok = travel_date.strftime("%d").lstrip("0") in visible_text
        month_ok = (
            travel_date.strftime("%b").lower() in visible_text
            or travel_date.strftime("%B").lower() in visible_text
        )

        return day_ok and month_ok
    except Exception:
        return False


def set_normal_passengers(page: Page, form: Locator, passengers: int) -> None:
    if passengers == 1:
        return

    try:
        selector = form.locator('[data-testid="train-travellers-selector"] button').first
        selector.click(timeout=3000)
        page.wait_for_timeout(500)
    except Exception:
        return

    for _ in range(max(0, passengers - 1)):
        clicked = False

        for locator in [
            page.get_by_role("button", name=re.compile(r"increase.*adult|add.*adult|adult.*increase", re.I)),
            page.locator("button").filter(has_text=re.compile(r"^\s*\+\s*$")),
        ]:
            item = visible_first(locator, timeout=500)
            if item is None:
                continue

            try:
                item.click(timeout=1500)
                clicked = True
                break
            except Exception:
                continue

        if not clicked:
            break


def get_train_form(page: Page) -> Optional[Locator]:
    """
    Eurostar often renders multiple booking magnet forms. Some copies can be
    reported as non-visible by Playwright even though the fields are present.
    Prefer a visible usable form, but fall back to the first form with the
    expected fields.
    """
    selector = 'form[data-testid="booking-magnet-form-trains"]'

    try:
        page.wait_for_selector(selector, timeout=15000)
    except Exception:
        return None

    forms = page.locator(selector)

    try:
        count = forms.count()
    except Exception:
        return None

    for i in range(count):
        form = forms.nth(i)
        try:
            if form.locator('button[data-testid="button-search"]').first.is_visible(timeout=500):
                return form
        except Exception:
            continue

    for i in range(count):
        form = forms.nth(i)
        try:
            has_origin = form.locator('input[data-testid="origin-field"]').count() > 0
            has_dest = form.locator('input[data-testid="destination-field"]').count() > 0
            has_date = form.locator('button[data-testid="start-date"]').count() > 0
            has_search = form.locator('button[data-testid="button-search"]').count() > 0

            if has_origin and has_dest and has_date and has_search:
                return form
        except Exception:
            continue

    return None


def click_normal_search(page: Page, form: Locator) -> bool:
    try:
        form.locator('button[data-testid="button-search"]').first.click(timeout=4000)
        safe_wait(page, timeout=12000)
        page.wait_for_timeout(2500)
        return True
    except Exception:
        return False


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

            clicked = click_search(page) if origin_ok and dest_ok and date_ok else False

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
    allowed = set(config.normal_eurostar.allowed_currencies)

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

            form = get_train_form(page)

            if form is None:
                log(f"NORMAL form missing after wait: {route.name} {travel_date}; falling back to body locator")
                save_debug(page, f"normal_form_missing_{route.name}_{travel_date}", config.settings.debug)
                form = page.locator("body")

            origin_ok = fill_station_field(page, form, "origin-field", route.origin)
            dest_ok = fill_station_field(page, form, "destination-field", route.destination)
            date_ok = set_normal_outbound_date(page, form, travel_date)

            set_normal_passengers(page, form, route.passengers)

            clicked = click_normal_search(page, form) if origin_ok and dest_ok and date_ok else False

            if not origin_ok or not dest_ok or not date_ok or not clicked:
                try:
                    start_attr = form.locator('button[data-testid="start-date"]').first.get_attribute(
                        "data-date",
                        timeout=1000,
                    )
                except Exception:
                    start_attr = "unknown"

                try:
                    origin_value = form.locator('input[data-testid="origin-field"]').first.input_value(timeout=1000)
                except Exception:
                    origin_value = "unknown"

                try:
                    dest_value = form.locator('input[data-testid="destination-field"]').first.input_value(timeout=1000)
                except Exception:
                    dest_value = "unknown"

                log(
                    f"NORMAL form incomplete: {route.name} {travel_date} "
                    f"origin_ok={origin_ok} dest_ok={dest_ok} date_ok={date_ok} clicked={clicked} "
                    f"origin_value={origin_value!r} dest_value={dest_value!r} start_date_attr={start_attr!r}"
                )

                save_debug(page, f"normal_form_incomplete_{route.name}_{travel_date}", config.settings.debug)
                continue

            text = body_text(page, timeout=15000)
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
