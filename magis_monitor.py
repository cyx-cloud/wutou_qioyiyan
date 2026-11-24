"""Magis monitor: WhatsApp + phone call when a studio at The General (Eindhoven) opens up.

Completely separate from the Codomo monitor:
  - its own workflow (.github/workflows/magis.yml), its own concurrency group
  - its own state file, magis_state.json, saved on the branch "magis-state", never on main
  - it only borrows send() and call() from rental_monitor.py, and changes nothing there
So if anything here breaks, the Codomo monitor keeps running exactly as before.

How it reads Magis:
  1. The overview page LIST_URL; every link /for-rent/the-general/<unit> is a candidate.
  2. Each new candidate's own page; "currently not available" there means skip it.
Magis posts new homes every Wednesday and Friday around 11:00.

Env: same secrets as the Codomo monitor, plus
  DRY_RUN=1                  print instead of sending
  TEST=true                  send what is listed now, state not changed
  LOCAL_LIST_HTML=path       read this saved overview page instead of the website (testing)
  LOCAL_UNIT_DIR=dir         read unit pages from dir/<unit>.html instead (testing)
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from rental_monitor import call, send   # only these two; rental_monitor itself is unchanged

SITE = "https://magisrealestate.com"
LIST_URL = SITE + "/for-rent"
BUILDING = "the-general"
MAX_RENT = 1100            # basic rent, same rule as for Codomo
MAX_UNIT_PAGES = 15        # unit pages per run, to stay polite
STATE_FILE = Path(__file__).with_name("magis_state.json")
USER_AGENT = "personal-rental-monitor/1.0 (a few requests per run)"
UNIT_LINK = re.compile(r'(?:https://magisrealestate\.com)?/for-rent/' + BUILDING + r'/([A-Za-z0-9-]+)')
ANY_UNIT_LINK = re.compile(r'/for-rent/[a-z0-9-]+/[A-Za-z0-9-]+')
# names of Magis buildings; if none of them is on the overview page, the page changed
KNOWN_NAMES = ("The General", "The City", "The Rumour", "The Garden", "Mister X")
NOT_AVAILABLE = re.compile(r"currently not available", re.I)


class Problem(Exception):
    def __init__(self, kind: str, text: str):
        super().__init__(text)
        self.kind = kind
        self.text = text


def get_page(url: str) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}, timeout=30)
    except requests.RequestException as e:
        raise Problem("unreachable", f"Can't reach Magis ({type(e).__name__})")
    if r.status_code == 429:
        raise Problem("blocked", "Magis says too many requests (429)")
    if r.status_code == 403:
        raise Problem("blocked", "Magis refused the request (403)")
    if r.status_code != 200:
        raise Problem("unreachable", f"Magis answered HTTP {r.status_code}")
    return r.text


def read_list() -> list[str]:
    """Unit codes of The General on the overview page, in page order."""
    local = os.environ.get("LOCAL_LIST_HTML")
    html = Path(local).read_text(encoding="utf-8") if local else get_page(LIST_URL)
    units = list(dict.fromkeys(UNIT_LINK.findall(html)))
    if not units and not ANY_UNIT_LINK.search(html) and not any(n in html for n in KNOWN_NAMES):
        Path(__file__).with_name("magis_debug_page.html").write_text(html, encoding="utf-8")
        raise Problem("changed", "Magis overview page looks different, I can't read it")
    return units


def read_unit(unit: str) -> dict:
    """Facts from a unit's own page. Missing facts stay None, never a reason to skip."""
    local_dir = os.environ.get("LOCAL_UNIT_DIR")
    if local_dir:
        html = Path(local_dir, f"{unit}.html").read_text(encoding="utf-8")
    else:
        time.sleep(1)      # be polite
        html = get_page(f"{SITE}/for-rent/{BUILDING}/{unit}")
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text.replace("&nbsp;", " "))
    info = {"unit": unit, "available": not NOT_AVAILABLE.search(text),
            "rent": None, "extra": None, "total": None, "area": None, "floor": None,
            "students_only": "students only" in text.lower()}
    if m := re.search(r"€\s?([\d.]+,\d{2}|\d[\d.]*)\s*per month", text):
        info["rent"] = dutch_number(m.group(1))
    if m := re.search(r"may amount to\s*€\s?([\d.]+,\d{2}|\d[\d.]*)", text):
        info["extra"] = dutch_number(m.group(1))
    if m := re.search(r"Total\s*€\s?([\d.]+,\d{2}|\d[\d.]*)", text):
        info["total"] = dutch_number(m.group(1))
    if m := re.search(r"(\d+(?:[.,]\d+)?)\s*m²", text):
        info["area"] = m.group(1).replace(",", ".")
    if m := re.search(r"(\d+)(?:st|nd|rd|th)\s+floor|ground floor", text, re.I):
        info["floor"] = m.group(0)
    return info


def dutch_number(s: str) -> float | None:
    try:
        return float(s.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def euro(x: float | None) -> str:
    return "?" if x is None else f"€{x:,.0f}"


def home_text(u: dict) -> str:
    lines = ["🆕 Magis · The General", f"🏠 Studio {u['unit']}"]
    if u["rent"] is None:
        lines.append("⚠️ Rent not found")
    else:
        lines.append(f"💶 {euro(u['rent'])} basic rent")
        total = u["total"] or (u["rent"] + u["extra"] if u["extra"] is not None else None)
        if total:
            lines.append(f"💶 About {euro(total)}/month all-in")
    size = " · ".join(x for x in [f"{u['area']} m²" if u["area"] else "", u["floor"] or ""] if x)
    if size:
        lines.append(f"📐 {size}")
    if u["students_only"]:
        lines.append("🎓 Students only")
    lines.append(f"{SITE}/for-rent/{BUILDING}/{u['unit']}")
    return "\n".join(lines)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"available": {}, "problem": None, "started": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


def safe_send(text: str) -> None:
    try:
        send(text)
    except RuntimeError as e:
        print(f"WhatsApp failed: {e}")


def available_units(units: list[str], skip: set[str]) -> tuple[list[dict], list[str]]:
    """Unit pages for units not in skip. Returns available ones and units whose page failed."""
    found, failed = [], []
    todo = [u for u in units if u not in skip]
    if len(todo) > MAX_UNIT_PAGES:
        # the overview probably lists unavailable homes too; don't hammer the site
        print(f"{len(todo)} unit pages to check, only checking {MAX_UNIT_PAGES}")
    for unit in todo[:MAX_UNIT_PAGES]:
        try:
            u = read_unit(unit)
        except (Problem, OSError) as e:
            print(f"Unit page failed for {unit}: {e}")
            failed.append(unit)
            continue
        if u["available"]:
            found.append(u)
    return found, failed


def main() -> int:
    test = os.environ.get("TEST") == "true"
    state = load_state()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")

    try:
        units = read_list()
    except Problem as p:
        print(f"PROBLEM [{p.kind}]: {p.text}")
        if test:
            safe_send(f"🧪 Magis test failed\n⚠️ {p.text}")
        elif state.get("problem") != p.kind:
            safe_send(f"⚠️ Magis monitor\n{p.text}\nI keep trying. Codomo is not affected.")
            state["problem"] = p.kind
            save_state(state)
        return 0
    print(f"The General on the overview page: {units or 'none'}")

    if test:
        found, failed = available_units(units, set())
        safe_send("\n".join([f"🧪 Magis test, real data",
                             f"{len(units)} The General links on the overview page",
                             f"{len(found)} available now"]
                            + ([f"⚠️ {len(failed)} unit pages failed"] if failed else [])))
        for u in found[:2]:
            safe_send("🧪 Test\n" + home_text(u))
        return 0

    if state.get("problem"):
        safe_send("✅ Magis monitor works again")
        state["problem"] = None

    known: dict = state["available"]
    found, failed = available_units(units, set(known))
    cheap = [u for u in found if u["rent"] is None or u["rent"] <= MAX_RENT]
    pricey = [u for u in found if u not in cheap]
    first_run = not state.get("started")

    if first_run:
        safe_send(f"✅ Magis monitor started\n{len(found)} The General homes available now"
                  f"\n(Codomo monitor not changed)")

    caller = None
    result: dict = {}
    if cheap and not first_run:
        first = cheap[0]
        rent = f", {first['rent']:.0f} euro" if first["rent"] is not None else ""
        say = (f"New Magis home at The General. Studio {first['unit']}{rent}. Check WhatsApp."
               if len(cheap) == 1 else
               f"{len(cheap)} new Magis homes at The General. Check WhatsApp.")
        caller = threading.Thread(target=lambda: result.update(problem=call(say)))
        caller.start()

    for u in cheap[:5]:
        safe_send(home_text(u))
    if pricey:
        safe_send(f"➕ {len(pricey)} new The General home(s) over {euro(MAX_RENT)}\n{LIST_URL}")

    if caller is not None:
        caller.join(timeout=240)
        problem = result.get("problem", "no answer from Twilio in time")
        if problem:
            safe_send(f"⚠️ Magis phone call failed\n{problem}")

    # keep only units still on the overview page; a unit that comes back counts as new.
    # Failed unit pages are not marked, so they are tried again next run.
    still = {k: v for k, v in known.items() if k in units}
    for u in found:
        still[u["unit"]] = stamp
    state["available"] = still
    state["started"] = True
    save_state(state)
    print(f"{len(found)} new available, {len(cheap)} under budget, {len(failed)} pages failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
