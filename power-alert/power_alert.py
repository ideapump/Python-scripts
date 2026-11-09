#!/usr/bin/env python3
"""
power_alert.py - Track electricity usage against a billing-cycle goal and
report to a GroupMe bot.

What it does on each run:
  1. Gets your daily kWh, either from the usage CSVs your utility emails you
     (source = "email", reads Gmail over IMAP) or by logging into your
     utility website (source = "website", headless Chromium via Playwright).
  2. Saves every day it sees into a local SQLite file, so the cycle total
     builds up even if the site only shows a few recent days.
  3. Works out where you stand for the current billing cycle: total so far,
     daily average, projected end-of-cycle total, and how many kWh/day you
     need from here to hit your goal (default 1050, safely over the 1000
     bonus threshold).
  4. Posts a summary to GroupMe - only when a new day of data shows up, so
     it is safe to run several times a day.

Run `python3 power_alert.py --help` for options.
Requires Python 3.11+. The website source also needs
`pip install playwright && playwright install chromium`.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import traceback
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import tomllib

HERE = Path(__file__).resolve().parent
log = logging.getLogger("power_alert")

GROUPME_URL = "https://api.groupme.com/v3/bots/post"
GROUPME_MAX = 990  # GroupMe caps bot messages at 1000 characters


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def keychain(name: str) -> str:
    """Read a secret saved with: security add-generic-password -s power-alert -a <name> -w"""
    if sys.platform != "darwin":
        return ""
    r = subprocess.run(
        ["security", "find-generic-password", "-s", "power-alert", "-a", name, "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    # Environment variables override the file (handy for keeping secrets out of it)
    site = cfg.setdefault("site", {})
    site["username"] = os.environ.get("POWER_USERNAME", site.get("username", ""))
    site["password"] = os.environ.get("POWER_PASSWORD", site.get("password", ""))
    # Secrets left blank in the file come from the macOS Keychain
    gm = cfg.setdefault("groupme", {})
    gm["bot_id"] = os.environ.get("GROUPME_BOT_ID") or gm.get("bot_id") or keychain("groupme_bot_id")
    em = cfg.setdefault("email", {})
    em["app_password"] = (
        os.environ.get("GMAIL_APP_PASSWORD") or em.get("app_password") or keychain("gmail_app_password")
    )
    return cfg


def resolve(p: str) -> Path:
    path = Path(p).expanduser()
    return path if path.is_absolute() else HERE / path


# --------------------------------------------------------------------------
# Billing cycle
# --------------------------------------------------------------------------
def _start_in(year: int, month: int, start_day: int) -> date:
    return date(year, month, min(start_day, calendar.monthrange(year, month)[1]))


def billing_cycle(today: date, goal_cfg: dict) -> tuple[date, date]:
    """Return (first_day, last_day) of the cycle containing `today`.

    Uses explicit cycle_start/cycle_end if set (meter-read dates often drift),
    otherwise a fixed day of the month.
    """
    if goal_cfg.get("cycle_start") and goal_cfg.get("cycle_end"):
        s, e = goal_cfg["cycle_start"], goal_cfg["cycle_end"]
        s = s if isinstance(s, date) else date.fromisoformat(str(s))
        e = e if isinstance(e, date) else date.fromisoformat(str(e))
        return s, e

    start_day = int(goal_cfg.get("cycle_start_day", 1))
    s = _start_in(today.year, today.month, start_day)
    if today < s:
        y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        s = _start_in(y, m, start_day)
    y, m = (s.year, s.month + 1) if s.month < 12 else (s.year + 1, 1)
    return s, _start_in(y, m, start_day) - timedelta(days=1)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS usage (day TEXT PRIMARY KEY, kwh REAL NOT NULL, fetched_at TEXT)"
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        self.db.commit()

    def upsert(self, usage: dict[date, float]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.db.executemany(
            "INSERT INTO usage(day, kwh, fetched_at) VALUES (?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET kwh=excluded.kwh, fetched_at=excluded.fetched_at",
            [(d.isoformat(), k, now) for d, k in usage.items()],
        )
        self.db.commit()

    def between(self, start: date, end: date) -> dict[date, float]:
        rows = self.db.execute(
            "SELECT day, kwh FROM usage WHERE day BETWEEN ? AND ?", (start.isoformat(), end.isoformat())
        )
        return {date.fromisoformat(d): k for d, k in rows}

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.db.commit()


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------
def _parse_kwh(text: str) -> float | None:
    m = re.search(r"-?\d[\d,]*\.?\d*", text)
    return float(m.group(0).replace(",", "")) if m else None


def _parse_date(text: str, fmt: str) -> date | None:
    text = text.strip()
    try:
        return datetime.strptime(text, fmt).date()
    except ValueError:
        # Tolerate extra words around the date, e.g. "Tue 11/05/2026"
        for token in re.findall(r"[\w/.\-,]+(?: \d{1,2},? \d{4})?", text):
            try:
                return datetime.strptime(token.strip(","), fmt).date()
            except ValueError:
                continue
    return None


def fetch_usage(cfg: dict, debug: bool = False, headed: bool = False) -> dict[date, float]:
    """Log in and scrape the daily usage table. Returns {date: kWh}."""
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    site = cfg["site"]
    sel = cfg["selectors"]
    profile_dir = resolve(cfg.get("paths", {}).get("browser_profile", "browser-profile"))
    debug_dir = resolve(cfg.get("paths", {}).get("debug_dir", "debug"))
    json_hits: list[dict] = []

    def wait_for(page, selector: str, ms: int) -> bool:
        try:
            page.locator(selector).first.wait_for(state="visible", timeout=ms)
            return True
        except PWTimeout:
            return False

    def dump(page, tag: str) -> None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        page.screenshot(path=str(debug_dir / f"{stamp}-{tag}.png"), full_page=True)
        (debug_dir / f"{stamp}-{tag}.html").write_text(page.content(), encoding="utf-8")
        (debug_dir / f"{stamp}-{tag}-json.json").write_text(json.dumps(json_hits, indent=2), encoding="utf-8")
        log.info("Saved debug files to %s (prefix %s-%s)", debug_dir, stamp, tag)

    with sync_playwright() as p:
        # A persistent profile keeps cookies between runs, so you log in (and
        # pass any "remember this device" 2FA prompt) far less often.
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=not headed,
            viewport={"width": 1400, "height": 1000},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(30_000)

        if debug:
            def on_response(resp):
                if "json" in (resp.headers.get("content-type") or ""):
                    try:
                        body = resp.text()[:4000]
                    except Exception:
                        body = "<unreadable>"
                    json_hits.append({"url": resp.url, "status": resp.status, "body": body})
            page.on("response", on_response)

        try:
            page.goto(site["usage_url"], wait_until="domcontentloaded")
            ready_selector = sel.get("ready", sel["row"])

            if not wait_for(page, ready_selector, 15_000):
                log.info("Not on the usage page yet - logging in")
                if not wait_for(page, sel["username"], 5_000):
                    page.goto(site["login_url"], wait_until="domcontentloaded")
                    page.locator(sel["username"]).first.wait_for(state="visible")
                page.locator(sel["username"]).first.fill(site["username"])
                if sel.get("next_button"):  # two-step logins (username, then password)
                    page.locator(sel["next_button"]).first.click()
                page.locator(sel["password"]).first.fill(site["password"])
                page.locator(sel["submit"]).first.click()
                page.wait_for_load_state("networkidle")
                page.goto(site["usage_url"], wait_until="domcontentloaded")

            # Clicks needed to reveal the daily table (e.g. a "Daily" tab, "Table view")
            for click in sel.get("usage_clicks", []):
                page.locator(click).first.click()
                page.wait_for_load_state("networkidle")

            if not wait_for(page, sel["row"], 30_000):
                dump(page, "no-usage-table")
                raise RuntimeError(
                    "Couldn't find the usage table after logging in. Check the selectors "
                    f"in your config; a screenshot and HTML were saved to {debug_dir}."
                )

            usage: dict[date, float] = {}
            date_i, kwh_i = int(sel["date_col"]), int(sel["kwh_col"])
            for row in page.locator(sel["row"]).all():
                cells = row.locator(sel.get("cell", "td")).all_inner_texts()
                if len(cells) <= max(date_i, kwh_i):
                    continue
                d = _parse_date(cells[date_i], sel["date_format"])
                k = _parse_kwh(cells[kwh_i])
                if d is not None and k is not None:
                    usage[d] = k

            if debug:
                dump(page, "usage-page")
            if not usage:
                dump(page, "empty-parse")
                raise RuntimeError("Found the table but couldn't read any date/kWh rows - check date_col, kwh_col and date_format.")
            log.info("Scraped %d days (%s to %s)", len(usage), min(usage), max(usage))
            return usage
        finally:
            ctx.close()


# --------------------------------------------------------------------------
# Email (CSV attachments)
# --------------------------------------------------------------------------
CSV_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%d-%b-%Y")


def _parse_csv_date(text: str, fmt: str | None) -> date | None:
    if fmt:
        return _parse_date(text, fmt)
    text = text.strip()
    try:
        return datetime.fromisoformat(text).date()  # 2026-11-05, 2026-11-05 00:15, ...
    except ValueError:
        pass
    for f in CSV_DATE_FORMATS:
        d = _parse_date(text, f)
        if d is not None:
            return d
    return None


def _pick_column(names: list[str], wanted: str | None, hints: tuple[str, ...], skip: int | None = None) -> int | None:
    if wanted:
        wanted = wanted.strip().lower()
        return names.index(wanted) if wanted in names else None
    for hint in hints:
        for i, n in enumerate(names):
            if hint in n and i != skip:
                return i
    return None


def parse_usage_csv(text: str, csv_cfg: dict) -> dict[date, float]:
    """Total kWh per day from a usage CSV. Works for daily rows and for hourly/15-minute rows."""
    rows = list(csv.reader(io.StringIO(text)))
    # Some utilities put a few lines of account info above the real header row
    for h, header in enumerate(rows):
        names = [c.strip().lower() for c in header]
        date_i = _pick_column(names, csv_cfg.get("date_column"), ("date", "day", "start", "time"))
        kwh_i = _pick_column(names, csv_cfg.get("kwh_column"), ("kwh", "usage", "consumption"), skip=date_i)
        if date_i is not None and kwh_i is not None:
            break
    else:
        raise RuntimeError(
            "Couldn't find the date and kWh columns in the CSV - set date_column and kwh_column under [csv]."
        )

    usage: dict[date, float] = {}
    for row in rows[h + 1:]:
        if len(row) <= max(date_i, kwh_i):
            continue
        d = _parse_csv_date(row[date_i], csv_cfg.get("date_format"))
        k = _parse_kwh(row[kwh_i])
        if d is not None and k is not None:
            usage[d] = usage.get(d, 0.0) + k
    return usage


def fetch_usage_email(cfg: dict, debug: bool = False) -> dict[date, float]:
    """Read the usage CSVs attached to recent emails from your utility. Returns {date: kWh}."""
    import email
    import imaplib

    em = cfg["email"]
    debug_dir = resolve(cfg.get("paths", {}).get("debug_dir", "debug"))
    days_back = int(em.get("days_back", 10))
    criteria = ["SINCE", (date.today() - timedelta(days=days_back)).strftime("%d-%b-%Y")]
    if em.get("sender"):
        criteria += ["FROM", f'"{em["sender"]}"']
    if em.get("subject"):
        criteria += ["SUBJECT", f'"{em["subject"]}"']

    usage: dict[date, float] = {}
    files = 0
    with imaplib.IMAP4_SSL(em.get("imap_host", "imap.gmail.com")) as imap:
        imap.login(em["address"], em["app_password"].replace(" ", ""))  # Google shows it as "abcd efgh ..."
        imap.select(f'"{em.get("folder", "INBOX")}"', readonly=True)  # read-only: nothing gets marked as read
        _, data = imap.search(None, *criteria)
        # Oldest first, so a newer email's numbers replace an older one's for the same day
        for num in data[0].split():
            _, parts = imap.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(parts[0][1])
            for part in msg.walk():
                name = part.get_filename() or ""
                if not name.lower().endswith(".csv"):
                    continue
                text = part.get_payload(decode=True).decode("utf-8-sig", errors="replace")
                if debug:
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    (debug_dir / Path(name).name).write_text(text, encoding="utf-8")
                days = parse_usage_csv(text, cfg.get("csv", {}))
                log.info("Read %d days from %s (%s)", len(days), name, msg.get("Subject", ""))
                usage.update(days)
                files += 1

    if not files:
        raise RuntimeError(
            f"No CSV attachments found in emails from the last {days_back} days - check sender/subject under [email]."
        )
    if not usage:
        raise RuntimeError("Found the CSV but couldn't read any date/kWh rows - check the [csv] settings.")
    log.info("Got %d days (%s to %s)", len(usage), min(usage), max(usage))
    return usage


# --------------------------------------------------------------------------
# Math
# --------------------------------------------------------------------------
@dataclass
class Status:
    cycle_start: date
    cycle_end: date
    total_days: int
    last_day: date
    last_day_kwh: float
    days_with_data: int
    missing_days: int
    total: float
    avg: float
    projected: float
    goal: float
    bonus: float
    days_left: int
    remaining: float
    needed_per_day: float
    pace_gap: float           # needed_per_day - avg  (>0: use more per day)
    schedule_diff: float      # total - where you'd be on an even pace to goal
    proj_vs_goal: float
    proj_vs_bonus: float


def compute(usage: dict[date, float], cs: date, ce: date, goal: float, bonus: float) -> Status | None:
    days = {d: k for d, k in usage.items() if cs <= d <= ce}
    if not days:
        return None
    total_days = (ce - cs).days + 1
    last = max(days)
    elapsed = (last - cs).days + 1
    total = sum(days.values())
    avg = total / len(days)
    # Days with no data yet (future days + any gaps) are assumed to match your average
    projected = total + avg * (total_days - len(days))
    days_left = (ce - last).days
    remaining = max(0.0, goal - total)
    needed = remaining / days_left if days_left else 0.0
    return Status(
        cycle_start=cs, cycle_end=ce, total_days=total_days,
        last_day=last, last_day_kwh=days[last],
        days_with_data=len(days), missing_days=elapsed - len(days),
        total=total, avg=avg, projected=projected, goal=goal, bonus=bonus,
        days_left=days_left, remaining=remaining, needed_per_day=needed,
        pace_gap=needed - avg if days_left else 0.0,
        schedule_diff=total - goal * elapsed / total_days,
        proj_vs_goal=projected - goal, proj_vs_bonus=projected - bonus,
    )


def _md(d: date) -> str:
    return f"{d:%b} {d.day}"


def format_message(s: Status) -> str:
    lines = [
        f"⚡ Power update for {d_label(s.last_day)}",
        f"That day: {s.last_day_kwh:.1f} kWh",
        f"Cycle {_md(s.cycle_start)}–{_md(s.cycle_end)}: {s.total:.0f} kWh so far "
        f"(day {(s.last_day - s.cycle_start).days + 1} of {s.total_days})",
        f"Your avg: {s.avg:.1f} kWh/day",
    ]

    if s.total >= s.goal:
        lines.append(f"🎉 Goal reached! {s.total:.0f} ≥ {s.goal:.0f} kWh. Bonus locked in.")
    else:
        if s.projected >= s.goal:
            icon = "✅"
        elif s.projected >= s.bonus:
            icon = "⚠️"  # would hit the bonus, but without your safety margin
        else:
            icon = "❌"
        lines.append(
            f"{icon} On track for {s.projected:.0f} kWh "
            f"({s.proj_vs_goal:+.0f} vs {s.goal:.0f} goal, {s.proj_vs_bonus:+.0f} vs {s.bonus:.0f} bonus)"
        )
        if s.days_left:
            lines.append(f"Need {s.remaining:.0f} kWh more in {s.days_left} days → {s.needed_per_day:.1f} kWh/day")
            if s.pace_gap > 0:
                lines.append(f"👉 Use {s.pace_gap:.1f} kWh/day MORE than your current pace")
            else:
                lines.append(f"👍 You can use {-s.pace_gap:.1f} kWh/day LESS and still make it")
        else:
            lines.append(f"Cycle is over: {s.remaining:.0f} kWh short of the goal.")
        ahead = "ahead of" if s.schedule_diff >= 0 else "behind"
        lines.append(f"You're {abs(s.schedule_diff):.0f} kWh {ahead} an even pace to {s.goal:.0f}")

    if s.missing_days:
        lines.append(f"(Note: {s.missing_days} day(s) this cycle had no data; projection assumes your average.)")
    return "\n".join(lines)


def d_label(d: date) -> str:
    return f"{d:%a} {_md(d)}"


# --------------------------------------------------------------------------
# GroupMe
# --------------------------------------------------------------------------
def groupme_post(bot_id: str, text: str) -> None:
    if not bot_id:
        raise RuntimeError("No GroupMe bot_id configured")
    chunks, buf = [], ""
    for line in text.splitlines():
        if len(buf) + len(line) + 1 > GROUPME_MAX:
            chunks.append(buf)
            buf = ""
        buf = f"{buf}\n{line}" if buf else line
    chunks.append(buf)
    for chunk in chunks:
        req = urllib.request.Request(
            GROUPME_URL,
            data=json.dumps({"bot_id": bot_id, "text": chunk}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status not in (200, 201, 202):
                raise RuntimeError(f"GroupMe returned HTTP {resp.status}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def demo_usage(today: date, goal_cfg: dict) -> dict[date, float]:
    cs, _ = billing_cycle(today, goal_cfg)
    vals = [28.4, 31.2, 35.0, 30.1, 26.8, 33.9, 36.2, 29.5, 31.7, 34.4, 30.0, 27.3, 32.8, 35.6, 31.1]
    out, d = {}, cs
    for v in vals:
        if d >= today:
            break
        out[d] = v
        d += timedelta(days=1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Electricity usage → GroupMe bonus tracker")
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    ap.add_argument("--dry-run", action="store_true", help="print the message instead of posting it")
    ap.add_argument("--force", action="store_true", help="post even if there's no new day of data")
    ap.add_argument("--debug", action="store_true", help="save screenshots, HTML, JSON responses and emailed CSVs")
    ap.add_argument("--headed", action="store_true", help="show the browser window (needs a display)")
    ap.add_argument("--demo", action="store_true", help="use fake data - no website, implies --dry-run")
    ap.add_argument("--test-groupme", action="store_true", help="send a test message and exit")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(Path(args.config))
    goal_cfg = cfg.get("goal", {})
    goal = float(goal_cfg.get("target_kwh", 1050))
    bonus = float(goal_cfg.get("bonus_kwh", 1000))
    bot_id = cfg.get("groupme", {}).get("bot_id", "")

    if args.test_groupme:
        groupme_post(bot_id, "✅ power_alert test message - the bot works!")
        log.info("Test message sent")
        return 0

    today = date.today()
    store = Store(resolve(cfg.get("paths", {}).get("database", "usage.db")))

    try:
        if args.demo:
            args.dry_run = True
            usage = demo_usage(today, goal_cfg)
        else:
            if cfg.get("source", "website") == "email":
                usage = fetch_usage_email(cfg, debug=args.debug)
            else:
                usage = fetch_usage(cfg, debug=args.debug, headed=args.headed)
            store.upsert(usage)

        # Use the cycle containing the newest data day (the site lags a day or two)
        latest = max(usage)
        cs, ce = billing_cycle(latest, goal_cfg)
        cycle_usage = usage if args.demo else store.between(cs, ce)
        status = compute(cycle_usage, cs, ce, goal, bonus)
        if status is None:
            log.info("No usage data yet for the cycle %s to %s", cs, ce)
            return 0

        msg = format_message(status)
        last_reported = store.get("last_reported_day")
        if not args.force and not args.dry_run and last_reported == status.last_day.isoformat():
            log.info("No new day since %s - not posting", last_reported)
            return 0

        if args.dry_run:
            print(msg)
        else:
            groupme_post(bot_id, msg)
            store.set("last_reported_day", status.last_day.isoformat())
            log.info("Posted update for %s", status.last_day)
        return 0

    except Exception as e:
        log.error("Run failed: %s\n%s", e, traceback.format_exc())
        # Tell you about failures, but at most once per day
        if not args.dry_run and bot_id and store.get("last_error_day") != today.isoformat():
            try:
                groupme_post(bot_id, f"⚠️ power_alert couldn't get your usage: {str(e)[:300]}")
                store.set("last_error_day", today.isoformat())
            except Exception:
                log.exception("Also failed to send the error alert")
        return 1


if __name__ == "__main__":
    sys.exit(main())
