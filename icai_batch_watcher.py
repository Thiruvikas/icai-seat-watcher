#!/usr/bin/env python3
"""ICAI seat watcher: WATCHES the Launch Batch Details page and NOTIFIES via ntfy.
It never logs in, never books, never touches captchas."""
import argparse
import os
import re
import sys
import time
import urllib.request

URL = "https://www.icaionlineregistration.org/launchbatchdetail.aspx"
DEBUG_DIR = "debug"
MIN_INTERVAL = 300  # seconds; never poll faster than 5 minutes


class OptionNotFound(Exception):
    pass


def log(msg):
    print(msg, flush=True)


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()


def send_ntfy(title, body, priority="urgent", tags="rotating_light"):
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    if not topic:
        log("ERROR: NTFY_TOPIC is not set (add it as a GitHub secret).")
        return False
    safe_title = title.encode("ascii", "ignore").decode()
    req = urllib.request.Request(
        "https://ntfy.sh/" + topic,
        data=body.encode("utf-8"),
        method="POST",
        headers={"Title": safe_title, "Priority": priority, "Tags": tags},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = 200 <= r.status < 300
        log("Alert sent." if ok else "Alert failed.")
        return ok
    except Exception as e:  # noqa: BLE001
        log("Alert failed: %s" % e)
        return False


def find_match(options, wanted):
    """Return index of the option whose text matches `wanted` (case-insensitive).
    Exact text first, then 'contains'."""
    w = norm(wanted)
    for i, o in enumerate(options):
        if norm(o) == w:
            return i
    for i, o in enumerate(options):
        if w and w in norm(o):
            return i
    return None


def settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(1000)


def pick_option(page, position, wanted, label, wait_seconds=45):
    """Choose option in the Nth visible dropdown. Retries because later
    dropdowns fill in only after the earlier one is chosen."""
    deadline = time.time() + wait_seconds
    last = []
    while time.time() < deadline:
        sel = page.locator("select:visible").nth(position)
        try:
            last = sel.evaluate(
                "el => Array.from(el.options).map(o => o.text.trim())", timeout=4000
            )
        except Exception:  # noqa: BLE001
            last = []
        idx = find_match(last, wanted)
        if idx is not None:
            sel.select_option(index=idx)
            settle(page)
            return last[idx]
        time.sleep(1.5)
    shown = ", ".join(last[:40]) if last else "(dropdown not found or empty)"
    raise OptionNotFound("Option '%s' not found in %s. Available: %s" % (wanted, label, shown))


def read_rows(page):
    tables = page.evaluate(
        """() => Array.from(document.querySelectorAll('table')).map(t =>
              Array.from(t.rows).map(r => Array.from(r.cells).map(c => c.innerText.trim())))"""
    )
    for rows in tables:
        for hi, row in enumerate(rows):
            heads = [norm(c) for c in row]
            if "available seats" in heads:
                col = {h: i for i, h in enumerate(heads)}
                out = []
                for r in rows[hi + 1:]:
                    if len(r) < len(row) - 1 or not any(r):
                        continue

                    def g(name):
                        i = col.get(name)
                        return r[i] if i is not None and i < len(r) else ""

                    seats_txt = g("available seats")
                    m = re.search(r"\d+", seats_txt)
                    out.append({
                        "batch": g("batch no"),
                        "seats": int(m.group()) if m else 0,
                        "from": g("from date"),
                        "to": g("to date"),
                        "time": g("batch time"),
                        "open_for": g("open for"),
                    })
                return out
    return None


def shot(page, name):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        page.screenshot(path=os.path.join(DEBUG_DIR, name + ".png"), full_page=True)
    except Exception:  # noqa: BLE001
        pass


def click_get_list(page):
    btn = page.locator(
        "input[type=submit][value*='Get List' i], input[type=button][value*='Get List' i], "
        "button:has-text('Get List'), a:has-text('Get List')"
    ).first
    btn.click(timeout=10000)
    settle(page)


def check_centre(browser, args, centre):
    """Returns (ok, open_batches). ok=False means a technical error."""
    tag = re.sub(r"[^A-Za-z0-9]+", "_", centre)
    page = browser.new_page(viewport={"width": 1366, "height": 900})
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        settle(page)
        pick_option(page, 0, args.region, "Region")
        pick_option(page, 1, centre, "Pou")
        pick_option(page, 2, args.course, "Course")
        click_get_list(page)
        rows = None
        for _ in range(6):  # wait for the table to appear
            rows = read_rows(page)
            if rows is not None:
                break
            time.sleep(2)
        if rows is None:
            log("[%s] WARNING: no results table found." % centre)
            shot(page, "no-rows-" + tag)
            return False, []
        open_batches = [r for r in rows if r["seats"] > 0]
        log("[%s] %d batches, %d with seats." % (centre, len(rows), len(open_batches)))
        return True, open_batches
    except OptionNotFound as e:
        log("[%s] ERROR: %s" % (centre, e))
        shot(page, "error-" + tag)
        return False, []
    except Exception as e:  # noqa: BLE001
        log("[%s] ERROR: %s" % (centre, e))
        shot(page, "error-" + tag)
        return False, []
    finally:
        page.close()


def run_once(args):
    from playwright.sync_api import sync_playwright

    centres = [c.strip() for c in args.centres.split(",") if c.strip()]
    all_ok = True
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for centre in centres:
                ok, open_batches = check_centre(browser, args, centre)
                all_ok = all_ok and ok
                for b in open_batches:
                    title = "SEAT OPEN: %d seat(s) %s" % (b["seats"], centre)
                    body = (
                        "Batch: %s\nSeats: %d\nFrom: %s\nTo: %s\nTime: %s\nOpen for: %s\n"
                        "Book NOW: %s" % (b["batch"], b["seats"], b["from"], b["to"],
                                          b["time"], b["open_for"], URL)
                    )
                    log("SEATS OPEN -> " + b["batch"])
                    send_ntfy(title, body)
        finally:
            browser.close()
    return all_ok


def main():
    ap = argparse.ArgumentParser(description="ICAI seat watcher (watch + notify only)")
    ap.add_argument("--region", default="Southern")
    ap.add_argument("--centres", help="comma separated, e.g. CHENNAI,Chengalpattu")
    ap.add_argument("--course", help="part of the course text")
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--interval", type=int, default=MIN_INTERVAL, help="seconds (min 300)")
    ap.add_argument("--test", action="store_true", help="send a test phone alert and exit")
    args = ap.parse_args()

    if args.test:
        ok = send_ntfy("ICAI watcher test", "Test alert OK. Your phone alerts work.",
                       priority="high", tags="white_check_mark")
        return 0 if ok else 1

    if not args.centres or not args.course:
        ap.error("--centres and --course are required")

    if args.once:
        return 0 if run_once(args) else 1

    interval = max(MIN_INTERVAL, args.interval)
    while True:
        run_once(args)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
