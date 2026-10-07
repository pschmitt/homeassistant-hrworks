"""Read the employee portal through a private browser context.

Only rendered, structured data leaves this module. Never return HTML, credentials,
cookies or arbitrary Playwright exception messages to clients or logs.
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from hrworks.models import TYPE_LABELS
from playwright.async_api import Browser, BrowserContext, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

BERLIN = ZoneInfo("Europe/Berlin")
LOGIN_URL = "https://ssl6.hrworks.de/o/dashboard"
TIME_RE = re.compile(r"^([+−-]?)(\d+):(\d{2})(?:\s*(?:Hours|hours|Stunden))?$")
DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4}|\d{2})\b")
PAIR_RE = re.compile(r"(.+?)\s*\((.+)\)")
ACCOUNT_LABELS = {
    "target hours": "monthly_target",
    "sollstunden": "monthly_target",
    "working hours": "monthly_worked",
    "arbeitsstunden": "monthly_worked",
    "time credit": "time_credit",
    "zeitgutschrift": "time_credit",
    "end of month balance": "monthly_balance",
    "monatssaldo": "monthly_balance",
    "transferred from previous month": "carryover",
    "übertrag aus vormonat": "carryover",
    "absences": "absence_deduction",
    "abwesenheiten": "absence_deduction",
    "total balance": "total_balance",
    "gesamtsaldo": "total_balance",
}
PREVIOUS_DAY_LABELS = {
    "monthly balance to the previous day": "monthly_balance",
    "total balance to the previous day": "total_balance",
}
BALANCES = {"monthly_balance", "total_balance"}


class PortalError(Exception):
    """Only fixed, non-sensitive error codes cross the worker boundary."""

    def __init__(self, code: str, status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def profile_id(company: str, username: str) -> str:
    return hashlib.sha256(f"{company.casefold()}:{username.casefold()}".encode()).hexdigest()


def minutes(value: str) -> int | None:
    match = TIME_RE.fullmatch(" ".join(value.split()))
    if not match or int(match[3]) >= 60:
        return None
    return (-1 if match[1] in {"-", "−"} else 1) * (int(match[2]) * 60 + int(match[3]))


def dates(value: str) -> list[date]:
    return [
        date(int(y) + (2000 if len(y) == 2 else 0), int(m), int(d))
        for d, m, y in DATE_RE.findall(value)
    ]


def normalize(value: str) -> str:
    return " ".join(value.replace("’", "'").split()).casefold().rstrip(":").strip()


def account_metrics(rows: list[list[str]]) -> tuple[dict, str]:
    """Map time-account rows; values the portal does not show stay absent.

    Rows such as "Recorded hours (Working hours)" / "33:44 (33:35)" carry two
    values. Previous-day balances replace end-of-month projections, and a
    balance is never mixed across both bases.
    """
    current, previous = {}, {}
    for label, value in rows:
        labels, values = PAIR_RE.fullmatch(label.strip()), PAIR_RE.fullmatch(value.strip())
        pairs = (
            [(labels[1], values[1]), (labels[2], values[2])]
            if labels and values
            else [(label, value)]
        )
        for name, text in pairs:
            parsed = minutes(text)
            if parsed is None:
                continue
            if key := PREVIOUS_DAY_LABELS.get(normalize(name)):
                previous[key] = parsed
            elif key := ACCOUNT_LABELS.get(normalize(name)):
                current[key] = parsed
    if not previous:
        return current, "end_of_month"
    return {
        **{k: v for k, v in current.items() if k not in BALANCES},
        **previous,
    }, "previous_day"


VIEWPORT = {"width": 1600, "height": 1000}


async def ensure_viewport(page: Page) -> None:
    """Make the page really render at VIEWPORT.

    Some CDP browsers (Browserless) ignore Playwright's viewport emulation and render
    at 800x600. The portal then switches to its off-canvas mobile layout, so menu
    links count as outside the viewport and every click times out.
    """
    size = await page.evaluate("() => [innerWidth, innerHeight]")
    if size == [VIEWPORT["width"], VIEWPORT["height"]]:
        return
    session = await page.context.new_cdp_session(page)
    await session.send(
        "Emulation.setDeviceMetricsOverride",
        {**VIEWPORT, "deviceScaleFactor": 1, "mobile": False},
    )


class EmployeePortal:
    """An isolated context and serialized operations for one employee."""

    def __init__(self, browser: Browser, state_dir: Path, identity: str) -> None:
        self.browser = browser
        self.state_dir = state_dir
        self.identity = identity
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.lock = asyncio.Lock()
        self.mfa_started: datetime | None = None

    @property
    def state_path(self) -> Path:
        return self.state_dir / f"{self.identity}.json"

    async def open(self, *, fresh: bool = False) -> Page:
        if self.page is not None and not self.page.is_closed():
            return self.page
        self.context = await self.browser.new_context(
            storage_state=str(self.state_path) if self.state_path.exists() and not fresh else None,
            locale="en-GB",
            timezone_id="Europe/Berlin",
            viewport=VIEWPORT,
            accept_downloads=False,
            service_workers="block",
        )
        self.page = await self.context.new_page()
        await ensure_viewport(self.page)
        self.page.set_default_timeout(20000)
        return self.page

    async def save_session(self) -> None:
        state = await self.context.storage_state()
        state["cookies"] = [
            c
            for c in state["cookies"]
            if c["domain"].lstrip(".") == "hrworks.de" or c["domain"].endswith(".hrworks.de")
        ]
        state["origins"] = []
        temporary = self.state_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream)
        temporary.replace(self.state_path)

    async def close(self) -> None:
        if self.context:
            await self.context.close()
        self.context = self.page = None

    async def settle(self) -> None:
        """Wait for the portal's event queue and lazy render to become stable."""
        page = await self.open()
        previous = None
        stable = 0
        for _ in range(40):
            await asyncio.sleep(0.25)
            state = await page.evaluate("""() => ({
                text: document.body.innerText,
                busy: (window.jQuery?.active || 0) > 0,
                shimmer: [...document.querySelectorAll('.hrw-loadingShimmer')]
                    .some(e => e.checkVisibility() && !e.closest('.me-data-list-group'))
            })""")
            digest = hashlib.sha256(state["text"].encode()).digest()
            stable = stable + 1 if digest == previous and not state["busy"] else 0
            previous = digest
            if stable >= 4 and not state["shimmer"]:
                return
        raise PortalError("portal_changed", 502)

    async def navigate(self, route: str, *, reuse: bool = False) -> Page:
        page = await self.open()
        target = f"/o/time-management/{route}"
        link = page.locator(f'a[href$="{target}"]:visible').first
        if reuse and urlsplit(page.url).path.endswith(target):
            pass
        elif "/o/" in page.url and await link.count():
            await link.click()
        else:
            # Third-party scripts can delay DOMContentLoaded on this portal.
            await page.goto(
                urljoin(page.url if "/o/" in page.url else LOGIN_URL, target),
                wait_until="commit",
                timeout=45000,
            )
            await page.locator("body").wait_for(timeout=45000)
        if "login.hrworks.de" in page.url:
            raise PortalError("session_expired", 403)
        try:
            await page.locator(".m-portlet").first.wait_for(state="visible", timeout=45000)
        except PlaywrightTimeout:
            if "login.hrworks.de" in page.url:
                raise PortalError("session_expired", 403) from None
            raise
        await self.settle()
        if "login.hrworks.de" in page.url:
            raise PortalError("session_expired", 403)
        return page

    async def login(
        self, company: str, username: str, password: str, *, force: bool = False
    ) -> dict:
        if force:
            await self.close()
        page = await self.open(fresh=force)
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        if "/o/" in page.url:
            await self.settle()
            await self.save_session()
            return {"profile_id": self.identity, "authenticated": True}
        if not force:
            # An expired session may retain cookies which interfere with a new
            # login. Reuse authenticated sessions, then start login cleanly.
            await self.close()
            page = await self.open(fresh=True)
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await page.locator("input[name=company]").fill(company)
        await page.locator("input[name=company]").press("Tab")
        await self.settle()
        await page.locator("input[name=login]").fill(username)
        await page.locator("input[type=password]").fill(password)
        await page.get_by_role("button", name=re.compile(r"Log in to HR WORKS|Anmelden")).click()
        try:
            await page.wait_for_function("location.pathname !== '/'", timeout=15000)
        except PlaywrightTimeout:
            raise PortalError("invalid_auth", 403) from None
        if "2fa-selection" in page.url:
            authenticator = page.get_by_text("Authenticator", exact=True)
            if not await authenticator.count():
                raise PortalError("unsupported_mfa", 403)
            await authenticator.click()
            await page.wait_for_url("**/2fa-totp")
        if "2fa-totp" in page.url:
            self.mfa_started = datetime.now(BERLIN)
            return {"profile_id": self.identity, "mfa_required": True}
        if "/o/" not in page.url:
            raise PortalError("unsupported_mfa", 403)
        await self.settle()
        await self.save_session()
        return {"profile_id": self.identity, "authenticated": True}

    async def mfa(self, code: str) -> dict:
        if (
            not re.fullmatch(r"[0-9]{6}", code)
            or self.mfa_started is None
            or datetime.now(BERLIN) - self.mfa_started > timedelta(minutes=10)
        ):
            raise PortalError("invalid_code", 403)
        page = await self.open()
        if "2fa-totp" not in page.url:
            raise PortalError("session_expired", 403)
        await page.locator("input:visible").fill(code)
        await page.get_by_role("button", name=re.compile(r"^Unlock$|^Freischalten$")).click()
        try:
            await page.wait_for_url("**/o/**", timeout=10000, wait_until="domcontentloaded")
        except PlaywrightTimeout:
            raise PortalError("invalid_code", 403) from None
        await self.settle()
        await self.save_session()
        self.mfa_started = None
        return {"profile_id": self.identity, "authenticated": True}

    async def select_year(self, year: int) -> bool:
        page = await self.open()
        selects = page.locator("select:visible")
        for index in range(await selects.count()):
            select = selects.nth(index)
            labels = await select.locator("option").all_text_contents()
            if any(re.fullmatch(r"20\d{2}", label.strip()) for label in labels):
                if str(year) not in [label.strip() for label in labels]:
                    return False
                selected = await select.locator("option:checked").inner_text()
                if selected.strip() != str(year):
                    await select.select_option(label=str(year))
                    await self.settle()
                return True
        raise PortalError("portal_changed", 502)

    async def drain_list(self) -> None:
        """Trigger all waypoint pages; fail rather than publish a partial calendar."""
        page = await self.open()
        for _ in range(80):
            waypoints = page.locator(".me-waypoint:visible")
            if not await waypoints.count():
                return
            count = await page.locator(".me-master-detail-list-group-item").count()
            await waypoints.last.scroll_into_view_if_needed()
            await page.locator(".me-scroller").evaluate_all(
                "es => es.forEach(e => { e.scrollTop=e.scrollHeight; e.dispatchEvent(new Event('scroll')); })"
            )
            await self.settle()
            new_count = await page.locator(".me-master-detail-list-group-item").count()
            if count == new_count and await page.locator(".me-waypoint:visible").count():
                # Waypoints need an actual crossing, not just a scroll event.
                await page.locator(".me-scroller").evaluate_all(
                    "es => es.forEach(e => {e.scrollTop=0;})"
                )
                await asyncio.sleep(0.3)
                await waypoints.last.scroll_into_view_if_needed()
                await self.settle()
                if await page.locator(".me-master-detail-list-group-item").count() == count:
                    raise PortalError("portal_changed", 502)
        raise PortalError("portal_changed", 502)

    async def account(self, today: date) -> tuple[dict, dict]:
        page = await self.navigate("working-time-months")
        if not await self.select_year(today.year):
            return {}, {"period": None, "current_month": False}
        # First item is the newest available month; do not rely on old selected preferences.
        items = page.locator(".me-master-detail-list-group-item").filter(
            has_not_text=re.compile(r"Not worked|Nicht gearbeitet", re.I)
        )
        if not await items.count():
            return {}, {"period": None, "current_month": False}
        title = await items.first.locator(".m-widget4__title").inner_text()
        months = {name.casefold(): index for index, name in enumerate(calendar.month_name) if name}
        months.update(
            {
                name: i + 1
                for i, name in enumerate(
                    [
                        "januar",
                        "februar",
                        "märz",
                        "april",
                        "mai",
                        "juni",
                        "juli",
                        "august",
                        "september",
                        "oktober",
                        "november",
                        "dezember",
                    ]
                )
            }
        )
        match = re.fullmatch(r"\s*(\w+)\s+(\d{4})\s*", title)
        if not match or match[1].casefold() not in months:
            raise PortalError("portal_changed", 502)
        period = f"{match[2]}-{months[match[1].casefold()]:02d}"
        await items.first.locator(".m-widget4__title a").click()
        await self.settle()
        rows = await page.locator("div.row").evaluate_all(r"""es => es
            .filter(e => e.checkVisibility() && e.children.length === 2)
            .map(e => [...e.children].map(c => c.innerText.trim()))
            .filter(a => a[0].length < 90 && /^[+−-]?\d+:\d{2}(\s|$)/.test(a[1]))""")
        result, balance_basis = account_metrics(rows)
        if not result:
            raise PortalError("portal_changed", 502)
        return result, {
            "period": period,
            "current_month": period == today.strftime("%Y-%m"),
            "balance_basis": balance_basis,
            "as_of": (today - timedelta(days=1)).isoformat()
            if balance_basis == "previous_day"
            else None,
        }

    async def event_list(self, kind: str, year: int) -> tuple[list[dict], dict]:
        page = await self.navigate("absences" if kind == "leave" else "sicknesses")
        if not await self.select_year(year):
            return [], {}
        # Include all statuses in the source; filter them explicitly in the worker.
        for select in await page.locator("select:visible").all():
            options = await select.locator("option").all_text_contents()
            all_label = next((x for x in options if x.strip() in {"All", "Alle"}), None)
            if all_label and not any(re.fullmatch(r"20\d{2}", x.strip()) for x in options):
                if (
                    await select.locator("option:checked").inner_text()
                ).strip() != all_label.strip():
                    await select.select_option(label=all_label)
                    await self.settle()
        await self.drain_list()
        items = await page.locator(".me-master-detail-list-group-item").evaluate_all("""es => es
            .filter(e => e.checkVisibility()).map(e => ({
                href:e.querySelector('.m-widget4__title a')?.getAttribute('href'),
                title:e.querySelector('.m-widget4__title')?.innerText,
                subtitle:e.querySelector('.m-widget4__sub')?.innerText,
                status:e.querySelector('.me-badge')?.innerText,
                approved:!!e.querySelector('.m-badge--success'),
                count:e.querySelector('.m-widget4__ext')?.innerText
            }))""")
        events = []
        expected = "/absence/" if kind == "leave" else "/sickness/"
        for item in items:
            if expected not in (item.get("href") or ""):
                continue
            ds = dates(item.get("subtitle") or "")
            if not ds:
                raise PortalError("portal_changed", 502)
            # Portal ranges are inclusive. HA all-day calendar end dates are exclusive.
            count = normalize(item.get("count") or "")
            half_day = bool(re.search(r"(?:0[.,]5|½)\s*(?:vacation|urlaub|day|tag)", count))
            events.append(
                {
                    "uid": f"hrworks-{kind}-{item['href'].rstrip('/').split('/')[-1]}",
                    "kind": kind,
                    "summary": " ".join(item["title"].split()),
                    "start": ds[0].isoformat(),
                    "end": (ds[-1] + timedelta(days=1)).isoformat(),
                    "approved": item["approved"],
                    "status": item.get("status") or "",
                    "half_day": half_day,
                    "description": (item.get("status") or "") + (" · Half day" if half_day else ""),
                }
            )
        vacation = {}
        if kind == "leave":
            texts = await page.locator(".m-portlet").evaluate_all(
                "es=>es.filter(e=>e.checkVisibility()).map(e=>e.innerText)"
            )
            account = next(
                (t for t in texts if t.startswith(("Vacation account", "Urlaubskonto"))), ""
            )
            for key, suffix in [
                ("vacation_remaining", "Available|Verfügbar"),
                ("vacation_approved", "Approved|Genehmigt"),
            ]:
                match = re.search(r"(\d+(?:[.,]\d+)?)\s+(?:" + suffix + r")", account)
                if match:
                    vacation[key] = float(match[1].replace(",", "."))
        return events, vacation

    async def working_day(self, day: date, *, reuse: bool = False) -> tuple[dict, list[dict]]:
        """Read actual day entries; keep every split interval separate."""
        page = await self.navigate("working-times", reuse=reuse)
        if not await self.select_year(day.year):
            return {}, []
        selects = page.locator("select:visible")
        for select in await selects.all():
            labels = await select.locator("option").all_text_contents()
            if len(labels) == 12:
                if await select.evaluate("e => e.selectedIndex") != day.month - 1:
                    await select.select_option(index=day.month - 1)
                    await self.settle()
                break
        for tab in await page.locator(".m-accordion__item-head").all():
            title = await tab.locator(".m-accordion__item-title").inner_text()
            span = re.search(r"(\d{2})\.(\d{2})\.\s*-\s*(\d{2})\.(\d{2})\.", title)
            if span and date(day.year, int(span[2]), int(span[1])) <= day <= date(
                day.year, int(span[4]), int(span[3])
            ):
                if "collapsed" in (await tab.get_attribute("class") or "").split():
                    await tab.click()
                    await self.settle()
                break
        header = page.locator(".hrw-me-working-time-day-header-row").filter(
            has_text=day.strftime("%d.%m.%y")
        )
        if not await header.count():
            raise PortalError("invalid_interval")
        if not await header.is_visible():
            raise PortalError("portal_changed", 502)
        holder = header.locator("..").first
        # Expansion is retained by the portal. Never toggle an already-open editor closed.
        if not await holder.locator("input:visible, select:visible").count():
            await header.locator(":scope > div").first.click()
            await self.settle()
        entries = await self.editor_entries(holder)
        header_text = await header.inner_text()
        if not entries and re.search(
            r"Open working time|Offene Arbeitszeit|\b\d{2}:\d{2}\s*\n\s*\d{2}:\d{2}\b",
            header_text,
            re.I,
        ):
            raise PortalError("portal_changed", 502)
        # The info popup reports net credited work and target time, including break rules.
        info = header.locator("a").filter(has=page.locator(".icon-streamline-information-circle"))
        metrics = {}
        balance_text = await header.locator(
            "span.hrw-me-working-time-day-header-span"
        ).all_text_contents()
        for text in balance_text:
            parsed = minutes(text.strip())
            if parsed is not None:
                metrics["today_balance"] = parsed
        if await info.count():
            await info.click()
            await self.settle()
            rows = await page.locator("div.row").evaluate_all(r"""es=>es
                .filter(e=>e.checkVisibility() && e.children.length===2)
                .map(e=>[...e.children].map(c=>c.innerText.trim()))
                .filter(a=>a[0].length<80 && /^[+-]?\d+:\d{2}/.test(a[1]))""")
            for label, value in rows:
                key = {
                    "target working time": "today_target",
                    "target hours": "today_target",
                    "actual working time": "today_worked",
                    "working time": "today_worked",
                    "balance": "today_balance",
                }.get(normalize(label))
                val = minutes(value)
                if key and val is not None:
                    metrics[key] = val
            tooltip = await holder.inner_text()
            for key, label in [
                ("today_target", "Target hours|Sollstunden"),
                ("today_worked", "Credited working hours|Angerechnete Arbeitsstunden"),
            ]:
                match = re.search(r"(?:" + label + r"):\s*([+-]?\d+:\d{2})", tooltip)
                if match:
                    metrics[key] = minutes(match[1])
            await info.click()
            await self.settle()
        return metrics, entries

    async def editor_entries(self, holder) -> list[dict]:
        """Read the visible rows without changing the portal editor."""
        fields = await holder.locator("label").evaluate_all("""labels => labels
            .filter(label => label.checkVisibility() || label.closest('.me-form-group, .form-group')?.checkVisibility()).map(label => {
                const group = label.closest('.me-form-group, .form-group');
                const control = label.control || label.querySelector('input,select,textarea')
                    || group?.querySelector('input,select,textarea');
                return {label: label.innerText,
                    value: control?.selectedOptions?.[0]?.text ?? control?.value
                        ?? group?.querySelector('.me-form-item-div')?.innerText};
            })""")
        pairs = [
            (normalize(f.get("label") or "").rstrip(":"), (f.get("value") or "").strip())
            for f in fields
        ]
        entries = []
        current = {}
        for label, value in pairs:
            if label in {"start time", "beginn", "startzeit"} and value:
                if current:
                    entries.append(current)
                current = {"start": value}
            elif current and label in {"end time", "ende", "endzeit"}:
                current["end"] = value
            elif current and label in {"working time type", "arbeitszeitart"}:
                current["type"] = value
            elif current and label in {"comment", "kommentar", "bemerkung"}:
                current["comment"] = value
        if current:
            entries.append(current)
        if any("end" not in entry or not entry.get("type") for entry in entries):
            raise PortalError("portal_changed", 502)
        if (
            not entries
            and await holder.locator("input:visible,select:visible,textarea:visible").count()
        ):
            raise PortalError("portal_changed", 502)
        return entries

    async def discard_row(self, start_field) -> None:
        """Remove a request's own draft row; never guess which entry to delete."""
        page = await self.open()
        row = start_field.locator(
            "xpath=ancestor::div[contains(concat(' ',normalize-space(@class),' '),' row ')][2]"
        )
        await row.locator("a.m-dropdown__toggle").click()
        await self.settle()
        delete = row.get_by_role("link", name=re.compile(r"^(Delete|Löschen)$"), exact=True)
        if await delete.count() != 1:
            raise PortalError("write_uncertain", 502)
        # Portal dropdowns may extend outside the scroll container.
        await delete.evaluate("el => el.click()")
        await self.settle()
        modal = page.locator(".modal.show")
        if await modal.count() != 1:
            raise PortalError("write_uncertain", 502)
        await modal.get_by_role("button", name=re.compile(r"^(Yes|Ja)$")).click()
        await self.settle()
        if await start_field.count():
            raise PortalError("write_uncertain", 502)

    async def record(self, data: dict) -> dict:
        """Submit one interval, preserving all existing rows and refusing ambiguity."""
        try:
            start = datetime.fromisoformat(data["start"])
            end = datetime.fromisoformat(data["end"])
            kind = data.get("type", "working_time")
            comment = data.get("comment", "")
            if start.tzinfo is None or end.tzinfo is None:
                raise ValueError
            start, end = start.astimezone(BERLIN), end.astimezone(BERLIN)
            if (
                start.date() != end.date()
                or start >= end
                or end > datetime.now(BERLIN)
                or start.second
                or end.second
                or start.microsecond
                or end.microsecond
                or not isinstance(comment, str)
                or len(comment) > 500
            ):
                raise ValueError
            type_labels = TYPE_LABELS
            if kind not in type_labels:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise PortalError("invalid_interval") from None
        _, entries = await self.working_day(start.date())
        lower, upper = start.hour * 60 + start.minute, end.hour * 60 + end.minute
        wanted_start, wanted_end = start.strftime("%H:%M"), end.strftime("%H:%M")
        labels = {normalize(x) for x in type_labels[kind]}
        for entry in entries:
            entry_start = minutes(entry.get("start", ""))
            entry_end = minutes(entry.get("end", ""))
            if entry_start is None or entry_end is None or entry_start >= entry_end:
                raise PortalError("overlap")
            if (
                entry_start == lower
                and entry_end == upper
                and normalize(entry.get("type", "")) in labels
                and entry.get("comment", "") == comment
            ):
                return {
                    "saved": False,
                    "already_exists": True,
                    "dry_run": data.get("dry_run", True),
                }
            if lower < entry_end and upper > entry_start:
                raise PortalError("overlap")
        result = {
            "saved": False,
            "dry_run": data.get("dry_run", True),
            "date": start.date().isoformat(),
            "start": wanted_start,
            "end": wanted_end,
            "type": kind,
            "duration_minutes": upper - lower,
        }
        if data.get("dry_run", True):
            return result
        page = await self.open()
        header = page.locator(".hrw-me-working-time-day-header-row").filter(
            has_text=start.strftime("%d.%m.%y")
        )
        add = header.locator("a").filter(has=page.locator(".icon-streamline-add"))
        if not await add.count() or not await add.is_visible():
            raise PortalError("writes_disabled", 403)
        holder = header.locator("..").first
        controls = holder.locator("input,select,textarea")
        new_start = None
        try:
            await add.click()
            await self.settle()
            groups = holder.locator(".me-form-group").filter(
                has=page.locator("input,select,textarea")
            )
            starts = groups.filter(
                has=page.locator(
                    "label", has_text=re.compile(r"^\s*(Start time|Startzeit|Beginn)\s*$", re.I)
                )
            ).locator("input")
            # Adding a row regenerates the existing fields' DOM IDs. Existing
            # intervals have closed ends (validated above); only the new row is open.
            actual = await self.editor_entries(holder)
            if len(actual) != len(entries) + 1 or any(e not in actual for e in entries):
                raise PortalError("write_uncertain", 502)
            added = []
            for node in await starts.all():
                candidate = node.locator(
                    "xpath=ancestor::div[contains(concat(' ',normalize-space(@class),' '),' row ')][2]"
                )
                end_control = (
                    candidate.locator(".me-form-group")
                    .filter(
                        has=page.locator(
                            "label", has_text=re.compile(r"^\s*(End time|Endzeit|Ende)\s*$", re.I)
                        )
                    )
                    .locator("input")
                )
                if await end_control.count() == 1 and not await end_control.input_value():
                    added.append(node)
            if len(added) != 1:
                raise PortalError("write_uncertain", 502)
            new_id = await added[0].get_attribute("id")
            if not new_id:
                raise PortalError("write_uncertain", 502)
            new_start = page.locator("[id=" + json.dumps(new_id) + "]")
            row = new_start.locator(
                "xpath=ancestor::div[contains(concat(' ',normalize-space(@class),' '),' row ')][2]"
            )

            new_ids = set(
                await row.locator("input,select,textarea").evaluate_all("es=>es.map(e=>e.id)")
            )
            previous_values = {
                key: value
                for key, value in (
                    await controls.evaluate_all("es=>Object.fromEntries(es.map(e=>[e.id,e.value]))")
                ).items()
                if key not in new_ids
            }

            async def field(pattern: str):
                group = row.locator(".me-form-group").filter(
                    has=page.locator("label", has_text=re.compile(pattern, re.I))
                )
                control = group.locator("input,select,textarea")
                if await control.count() != 1:
                    raise PortalError("portal_changed", 502)
                identifier = await control.get_attribute("id")
                if not identifier:
                    raise PortalError("portal_changed", 502)
                return page.locator("[id=" + json.dumps(identifier) + "]")

            start_field = new_start
            end_field = await field(r"^\s*(End time|Endzeit|Ende)\s*$")
            select = await field(r"^\s*(Working time type|Arbeitszeitart)\s*$")
            comment_field = await field(r"^\s*(Comment|Kommentar|Bemerkung)\s*$")
            options = await select.locator("option").all_text_contents()
            chosen = next((label for label in options if normalize(label) in labels), None)
            if chosen is None:
                raise PortalError("writes_disabled", 403)
            for control, value in [(start_field, wanted_start), (end_field, wanted_end)]:
                await control.press("ControlOrMeta+A")
                await control.press_sequentially(value)
                await control.press("Tab")
            await select.select_option(label=chosen)
            await comment_field.fill(comment)
            await comment_field.press("Tab")
            await self.settle()
            if (
                await start_field.input_value() != wanted_start
                or await end_field.input_value() != wanted_end
                or await select.locator("option:checked").inner_text() != chosen
                or await comment_field.input_value() != comment
            ):
                raise PortalError("portal_changed", 502)
            current_values = await controls.evaluate_all(
                "es=>Object.fromEntries(es.map(e=>[e.id,e.value]))"
            )
            if any(current_values.get(key) != value for key, value in previous_values.items()):
                raise PortalError("portal_changed", 502)
            save = page.get_by_role("button", name=re.compile(r"^\s*(Save|Speichern)\s*$"))
            if await save.count() != 1:
                raise PortalError("portal_changed", 502)
        except Exception:
            # The portal retains added rows across sessions even before Save.
            # Remove only the field identified as newly added by this request.
            if new_start is not None:
                try:
                    await self.discard_row(new_start)
                except Exception:
                    raise PortalError("write_uncertain", 502) from None
            else:
                raise PortalError("write_uncertain", 502) from None
            raise
        try:
            await save.click()
            await self.settle()
            await page.reload(wait_until="commit", timeout=45000)
            _, verified = await self.working_day(start.date())
            matches = [
                e
                for e in verified
                if e.get("start") == wanted_start
                and e.get("end") == wanted_end
                and normalize(e.get("type", "")) in labels
                and e.get("comment", "") == comment
            ]

            def old(e):
                return (e.get("start"), e.get("end"), e.get("type"), e.get("comment", ""))

            if len(matches) != 1 or any(old(e) not in [old(v) for v in verified] for e in entries):
                raise PortalError("write_uncertain", 502)
        except Exception:
            raise PortalError("write_uncertain", 502) from None
        await self.save_session()
        return {**result, "saved": True}

    async def snapshot(self, options: dict) -> dict:
        today = datetime.now(BERLIN).date()
        start = today - timedelta(days=options["calendar_past_days"])
        end = today + timedelta(days=options["calendar_future_days"] + 1)
        metrics, account = await self.account(today)
        event_map = {}
        for year in range(start.year, end.year + 1):
            for kind in ["leave", "sickness"] if options["include_sickness"] else ["leave"]:
                events, vacation = await self.event_list(kind, year)
                if year == today.year:
                    metrics.update(vacation)
                for event in events:
                    if event["end"] <= start.isoformat() or event["start"] >= end.isoformat():
                        continue
                    if not event["approved"]:
                        pending = normalize(event["status"]) in {
                            "applied for",
                            "pending",
                            "requested",
                            "beantragt",
                            "in approval",
                            "awaiting approval",
                        }
                        if not options["include_pending"] or not pending:
                            continue
                    event_map[event["uid"]] = event
        today_metrics, today_entries = await self.working_day(today)
        metrics.update(today_metrics)
        page = await self.open()
        text = await page.locator("body").inner_text()
        clocked_in = any(e.get("start") and not e.get("end") for e in today_entries)
        if re.search(r"\bClock in\b|\bEinstempeln\b", text):
            clocked_in = False
        elif re.search(r"\bClock out\b|\bAusstempeln\b", text):
            clocked_in = True
        await self.save_session()
        return {
            "metrics": metrics,
            "account": account,
            "today": {"date": today.isoformat()},
            "vacation_year": today.year,
            "events": sorted(event_map.values(), key=lambda e: (e["start"], e["uid"])),
            "calendar_window": {"start": start.isoformat(), "end": end.isoformat()},
            "clocked_in": clocked_in,
            "fetched_at": datetime.now(BERLIN).isoformat(),
        }
