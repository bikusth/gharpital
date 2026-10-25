#!/usr/bin/env python3
"""
Gharpital NMC scraper.

The Nepal Medical Council search page (https://nmc.org.np/search-registered-doctor/)
is a Blazor Server app: results arrive over a live websocket, there is no plain
web API. So this script drives a real (headless) Chromium with Playwright, types an
NMC number into the search box, and reads the result card - the same thing a person
does in the browser, one number at a time, at a polite pace.

What it stores per doctor: NMC no., full name, gender, degree, speciality.
What it deliberately does NOT store: the address field (it is a home/permanent
address, not a workplace) - Gharpital only publishes professional information.

Output: data/raw/nmc_doctors.csv   (merged with whatever is already there)
State:  data/raw/scrape_state.json (lets a run resume where the last one stopped)

Usage:
  pip install -r scraper/requirements.txt && python -m playwright install chromium
  python scraper/nmc_scraper.py                 # continue the full sweep, then look for new numbers
  python scraper/nmc_scraper.py --start 1 --end 500     # a specific range
  python scraper/nmc_scraper.py --minutes 330   # stop after N minutes (used by GitHub Actions)
  python scraper/nmc_scraper.py --workers 2     # two browser tabs in parallel (default 1)
"""
import argparse
import asyncio
import csv
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

import os
URL = os.environ.get("NMC_URL", "https://nmc.org.np/search-registered-doctor/")  # override only for testing
ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
CSV_PATH = RAW / "nmc_doctors.csv"
STATE_PATH = RAW / "scrape_state.json"
FIELDS = ["nmc_no", "full_name", "gender", "degree", "speciality", "remarks", "first_seen", "last_seen"]

# How many empty numbers in a row past the highest known one before we decide we've reached the end.
END_GAP = 300
# Polite pacing between lookups, per worker (seconds).
MIN_DELAY, MAX_DELAY = 0.6, 1.2
# How long to wait for a result before treating a number as empty. NMC shows no "not found"
# message (an empty number just shows no card), so this is learned from how fast real results
# arrive: a few times the recent slow end, kept between these bounds. Anything missed on a
# slow moment is picked up by the next re-check pass.
MIN_WAIT, MAX_WAIT = 2.0, 8.0

# Runs in the page: fill the NMC box, click Search, wait until the card for this number shows up.
LOOKUP_JS = r"""
async ([n, timeoutMs]) => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const setVal = (el, v) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(el, v);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const name = document.querySelector('input[name=name]');
  const no = document.querySelector('input[name=symbolNo]');
  const deg = document.querySelector('input[name=degree]');
  const btn = [...document.querySelectorAll('button')].find(b => /search/i.test(b.innerText));
  if (!name || !no || !btn) return {error: 'search form not found'};
  setVal(name, ''); if (deg) setVal(deg, ''); setVal(no, String(n));
  await sleep(120);
  btn.click();
  const re = new RegExp('NMC No\\s+' + n + '(\\s|$)');
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    await sleep(80);
    for (const t of document.querySelectorAll('table')) {
      if (re.test(t.innerText)) {
        const rec = {};
        for (const tr of t.querySelectorAll('tr')) {
          const cells = [...tr.querySelectorAll('td,th')].map(c => c.innerText.trim());
          if (cells.length >= 2) rec[cells[0]] = cells.slice(1).join(' ').trim();
        }
        return {found: true, rec, ms: Date.now() - t0};
      }
    }
  }
  return {found: false};
}
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_existing():
    rows = {}
    if CSV_PATH.exists():
        with CSV_PATH.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[r["nmc_no"]] = r
    return rows


def save_rows(rows):
    RAW.mkdir(parents=True, exist_ok=True)
    tmp = CSV_PATH.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for k in sorted(rows, key=lambda x: int(x) if x.isdigit() else 10**9):
            w.writerow({f: rows[k].get(f, "") for f in FIELDS})
    tmp.replace(CSV_PATH)


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"next": 1, "max_found": 0, "sweeps_completed": 0}


def save_state(state):
    RAW.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def parse(rec):
    get = lambda *keys: next((rec[k] for k in keys if rec.get(k)), "")
    name = re.sub(r"\s+", " ", get("Full Name", "Name")).strip()
    return {
        "full_name": name,
        "gender": get("Gender"),
        "degree": get("Degree"),
        "speciality": get("Speciality", "Specialty", "Specialization"),
        "remarks": get("Remarks", "Remark", "Status"),  # e.g. "Deregistered After Death"
    }


class Scraper:
    def __init__(self, workers, deadline):
        self.workers = workers
        self.deadline = deadline
        self.rows = load_existing()
        self.state = load_state()
        self.found = self.updated = self.empty = 0
        self.lock = asyncio.Lock()
        self.last_save = time.time()
        # recent response times (s) of numbers that were found; remembered between runs
        self.latencies = [float(x) for x in self.state.get("latencies", [])][-100:]

    def wait_limit(self):
        if len(self.latencies) < 5:
            return 4.0  # not enough measurements yet: a cautious middle value
        recent = sorted(self.latencies[-200:])
        p90 = recent[int(len(recent) * 0.9) - 1]
        return max(MIN_WAIT, min(MAX_WAIT, p90 * 3 + 0.5))

    def time_left(self):
        return self.deadline is None or time.time() < self.deadline

    async def open_page(self, browser):
        page = await browser.new_page(user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36 GharpitalDirectoryBot/1.0 (+https://github.com/bikusth/gharpital)"))
        await page.goto(URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_selector("input[name=symbolNo]", timeout=30000)
        return page

    async def lookup(self, page, n):
        for attempt in range(3):
            try:
                res = await page.evaluate(LOOKUP_JS, [n, int(self.wait_limit() * 1000)])
                if res.get("found") and "ms" in res:
                    self.latencies.append(res["ms"] / 1000)
                if res.get("error"):
                    raise RuntimeError(res["error"])
                return res
            except Exception as e:  # connection to the Blazor server dropped, page reload fixes it
                print(f"  NMC {n}: {e} - reloading (attempt {attempt + 1})", flush=True)
                await asyncio.sleep(5 * (attempt + 1))
                try:
                    await page.goto(URL, wait_until="networkidle", timeout=60000)
                    await page.wait_for_selector("input[name=symbolNo]", timeout=30000)
                except Exception:
                    pass
        return {"found": False, "failed": True}

    async def record(self, n, res):
        async with self.lock:
            key = str(n)
            if res.get("found"):
                data = parse(res["rec"])
                old = self.rows.get(key)
                self.rows[key] = {"nmc_no": key, **data,
                                  "first_seen": old.get("first_seen") if old else now(),
                                  "last_seen": now()}
                if old and any(old.get(k) != data[k] for k in data):
                    self.updated += 1
                self.found += 1
                self.state["max_found"] = max(self.state.get("max_found", 0), n)
            elif not res.get("failed"):
                self.empty += 1
            if time.time() - self.last_save > 60:
                save_rows(self.rows)
                self.state["latencies"] = [round(x, 2) for x in self.latencies[-100:]]
                save_state(self.state)
                self.last_save = time.time()

    async def run_range(self, browser, numbers, track_next=False):
        """Look up a list of numbers with N workers. Returns the numbers that were empty."""
        queue = asyncio.Queue()
        for n in numbers:
            queue.put_nowait(n)
        empties = []
        done_upto = []

        async def worker(i):
            page = await self.open_page(browser)
            while self.time_left():
                try:
                    n = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                res = await self.lookup(page, n)
                await self.record(n, res)
                if not res.get("found"):
                    empties.append(n)
                done_upto.append(n)
                if track_next:
                    self.state["next"] = max(self.state.get("next", 1), n + 1)
                if (self.found + self.empty) % 100 == 0:
                    print(f"  ...NMC {n}: {self.found} found, {self.empty} empty "
                          f"(waiting up to {self.wait_limit():.1f}s per number)", flush=True)
                await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
            await page.close()

        await asyncio.gather(*(worker(i) for i in range(self.workers)))
        return empties, queue.qsize() == 0

    async def main(self, start, end):
        """Returns True when this run's work is complete (nothing left until the next schedule)."""
        complete = False
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                if start is not None:
                    print(f"Scanning NMC {start}..{end}", flush=True)
                    _, complete = await self.run_range(browser, range(start, end + 1))
                else:
                    known_top = lambda: max(self.state.get("max_found", 0),
                                            max((int(k) for k in self.rows if k.isdigit()), default=0))
                    # 1) Frontier: numbers above the highest known one. On the very first run this is
                    #    the whole register; later it just picks up new registrations. Resumes exactly
                    #    where a timed-out run stopped (state["frontier"] = last number fully checked).
                    n = max(known_top(), self.state.get("frontier", 0)) + 1
                    print(f"New numbers: scanning from NMC {n}", flush=True)
                    reached_end = False
                    while self.time_left():
                        _, finished = await self.run_range(browser, range(n, n + 50))
                        if not finished:
                            break
                        n += 50
                        self.state["frontier"] = n - 1
                        if n - 1 - known_top() >= END_GAP:
                            reached_end = True
                            break
                    if reached_end:
                        self.state["frontier"] = known_top()  # next run starts just above the last real doctor
                    # 2) Rolling re-check of known numbers, to catch changes (new speciality,
                    #    deregistration...). Spread over several runs; resumes via state["next"].
                    top = known_top()
                    nxt = self.state.get("next", 1)
                    if reached_end and not self.state.get("sweeps_completed"):
                        # the first complete frontier pass covered every number from 1: that is a full check
                        self.state["sweeps_completed"] = 1
                        self.state["next"] = 1
                        complete = True
                    elif reached_end and self.time_left() and nxt <= top:
                        print(f"Re-check: continuing from NMC {nxt} up to {top}", flush=True)
                        _, finished = await self.run_range(browser, range(nxt, top + 1), track_next=True)
                        if finished:
                            self.state["next"] = 1
                            self.state["sweeps_completed"] = self.state.get("sweeps_completed", 0) + 1
                            complete = True
                    elif reached_end and nxt > top:
                        complete = True
            finally:
                await browser.close()
                save_rows(self.rows)
                self.state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self.state["latencies"] = [round(x, 2) for x in self.latencies[-100:]]
                save_state(self.state)
        print(f"Done: {self.found} found ({self.updated} changed), {self.empty} empty. "
              f"Register now has {len(self.rows)} doctors; highest NMC no. {self.state.get('max_found')}."
              + (" Nothing left to do this run." if complete else ""))
        return complete


def cli():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=int)
    ap.add_argument("--end", type=int)
    ap.add_argument("--minutes", type=float, help="stop after this many minutes")
    ap.add_argument("--workers", type=int, default=1, help="parallel browser tabs (keep this small: 1-3)")
    a = ap.parse_args()
    if (a.start is None) != (a.end is None):
        ap.error("--start and --end go together")
    workers = max(1, min(a.workers, 3))
    deadline = time.time() + a.minutes * 60 if a.minutes else None
    complete = asyncio.run(Scraper(workers, deadline).main(a.start, a.end))
    # exit code 10 = "finished everything for now", so a calling loop knows it can stop
    return 10 if complete else 0


if __name__ == "__main__":
    sys.exit(cli())
