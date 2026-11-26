"""Magis monitor: Telegram (or WhatsApp) + phone call when a Magis home in Eindhoven opens up.

Completely separate from the Codomo monitor:
  - its own workflow (.github/workflows/magis.yml), its own concurrency group
  - its own state file, magis_state.json, saved on the branch "magis-state", never on main
  - it only borrows send() and call() from rental_monitor.py, and changes nothing there
So if anything here breaks, the Codomo monitor keeps running exactly as before.

How it reads Magis:
  1. The overview page LIST_URL; every link /for-rent/<building>/<unit> is a candidate
     (all buildings: the overview has no city in its links).
  2. Each new candidate's own page; "currently not available" there means skip it.
     The page also shows "City, Street 12"; only Eindhoven homes are alerted.
  3. Travel time by bike and on foot to both bus 305 stops in Eindhoven, CS and TU/e
     (free OpenStreetMap services).
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
from urllib.parse import unquote

import requests

from rental_monitor import call, send   # rental_monitor itself is unchanged
from rental_monitor import GEO_USER_AGENT, ROUTER, geocode

SITE = "https://magisrealestate.com"
LIST_URL = SITE + "/for-rent"
CITY = "Eindhoven"         # only homes in this city get alerts
MAX_RENT = 1100            # basic rent, same rule as for Codomo
MAX_UNIT_PAGES = 15        # unit pages per run, to stay polite
STATE_FILE = Path(__file__).with_name("magis_state.json")
USER_AGENT = "personal-rental-monitor/1.0 (a few requests per run)"
# "the-general/28S"; the unit part runs to the end of the link, so codes with dots,
# underscores or more slashes (like "mister-x/9.04" or "the-city/Shared/3") are kept whole
UNIT_LINK = re.compile(r'/for-rent/([a-z0-9-]+/[^"\'\s?#<>\\]+)')
# names of Magis buildings; if none of them is on the overview page, the page changed
KNOWN_NAMES = ("The General", "The City", "The Rumour", "The Garden", "Mister X")
NOT_AVAILABLE = re.compile(r"currently not available", re.I)
# the overview when nothing is for rent: "0 available residences", "No residences found"
EMPTY_PAGE = re.compile(r"No residences found|\b0\s+available residences", re.I)
# the home page shows its address as "Rijswijk, Verrijn Stuartlaan 2 F828"
CITIES = ("Eindhoven", "Tilburg", "Rijswijk", "Amersfoort", "'s-Hertogenbosch", "Den Bosch",
          "Arnhem", "Delft", "Woerden", "Wageningen", "Capelle aan den IJssel")
ADDRESS = re.compile(r"\b(" + "|".join(re.escape(c) for c in CITIES)
                     + r"),\s+([A-Z][A-Za-z'. -]{2,50}?\s\d+[A-Za-z]?)\b")
# Bus 305 (to Veghel / Vanderlande) stops only twice in Eindhoven. (lat, lon) from
# transitapp.com, middle of the two platforms (one per direction).
STOPS_305 = {"Centraal Station": (51.4438, 5.4787),
             "TU Eindhoven": (51.4512, 5.4837)}     # John F. Kennedylaan


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


def watched_building() -> str:
    """"all" (every building; the city filter comes later), except in a test run, where
    MAGIS_BUILDING can pick one building, e.g. "novum", to test with."""
    if os.environ.get("TEST") == "true" and os.environ.get("MAGIS_BUILDING", "").strip():
        return os.environ["MAGIS_BUILDING"].strip().lower()
    return "all"


def read_list() -> list[str]:
    """Units ("building/code") of the watched building on the overview page, in page order."""
    local = os.environ.get("LOCAL_LIST_HTML")
    html = Path(local).read_text(encoding="utf-8") if local else get_page(LIST_URL)
    every = list(dict.fromkeys(u.rstrip("/") for u in UNIT_LINK.findall(html)))
    if os.environ.get("TEST") == "true":
        # keep the page in a test run, so the workflow uploads it and I can see its links
        Path(__file__).with_name("magis_debug_page.html").write_text(html, encoding="utf-8")
    b = watched_building()
    units = every if b == "all" else [u for u in every if u.split("/")[0] == b]
    # (normal runs: all buildings; the city is only known from each home's own page)
    print(f"Overview page links {len(every)} homes in total: {every or 'none'}")
    if not every and EMPTY_PAGE.search(html):
        print("Magis says: no residences available right now")
        return []
    if not every and not any(n in html for n in KNOWN_NAMES):
        Path(__file__).with_name("magis_debug_page.html").write_text(html, encoding="utf-8")
        raise Problem("changed", "Magis overview page looks different, I can't read it")
    return units


def read_unit(unit: str) -> dict:
    """Facts from a unit's own page. Missing facts stay None, never a reason to skip."""
    local_dir = os.environ.get("LOCAL_UNIT_DIR")
    if local_dir:
        html = Path(local_dir, f"{unit.split('/')[-1]}.html").read_text(encoding="utf-8")
    else:
        time.sleep(1)      # be polite
        html = get_page(f"{SITE}/for-rent/{unit}".replace("&amp;", "&"))
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text.replace("&nbsp;", " "))
    info = {"unit": unit, "available": not NOT_AVAILABLE.search(text),
            "rent": None, "extra": None, "total": None, "area": None, "floor": None,
            "students_only": "students only" in text.lower(),
            "city": None, "street": None}
    if m := ADDRESS.search(text):
        info["city"], info["street"] = m.group(1), m.group(2).strip()
    elif CITY.lower() in text.lower():
        info["city"] = CITY
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
    building, code = u["unit"].split("/", 1)
    code = unquote(code).replace("/", " ")     # "2%20F828" -> "2 F828"
    lines = [f"🆕 Magis · {building.replace('-', ' ').title()}",
             f"🏠 {u['street'] or 'Home ' + code}" + (f" ({code})" if u["street"] else "")]
    if u["city"] != CITY:
        lines.append(f"⚠️ City: {u['city'] or 'not found'}")
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
    lines += travel_lines(u)
    if u["students_only"]:
        lines.append("🎓 Students only")
    lines.append(f"{SITE}/for-rent/{u['unit']}")
    return "\n".join(lines)


def route_minutes(lat: float, lon: float, to: tuple[float, float], mode: str) -> int:
    url = ROUTER.format(mode=mode, a=f"{lon},{lat}", b=f"{to[1]},{to[0]}")
    r = requests.get(url, params={"overview": "false"},
                     headers={"User-Agent": GEO_USER_AGENT}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError(f"router answered {data.get('code')}")
    return max(1, round(data["routes"][0]["duration"] / 60))


PLACES: dict = {}   # address -> [lat, lon], filled from magis_state.json


def travel_lines(u: dict) -> list[str]:
    """Bike and walking time to the two 305 stops (CS and TU/e). Problems show as ⚠️."""
    if not u["street"]:
        return ["⚠️ Address not found, no travel times"]
    key = f"{u['street']}, {u['city'] or CITY}"
    try:
        if key not in PLACES:
            point, rough = geocode(u["street"], u["city"] or CITY), False
            if point is None:        # "Europalaan 28" may be unknown: try the street alone
                point = geocode(re.sub(r"\s+\d.*$", "", u["street"]), u["city"] or CITY)
                rough = True
            if point is None:
                return ["⚠️ Address not found on map"]
            PLACES[key] = [point[0], point[1], rough]
        lat, lon = PLACES[key][:2]
        approx = "≈" if len(PLACES[key]) > 2 and PLACES[key][2] else ""   # street only
        lines = []
        for (name, at), icon, short in zip(STOPS_305.items(), ("🚉", "🏫"), ("CS", "TU/e")):
            bike = route_minutes(lat, lon, at, "bike")
            walk = route_minutes(lat, lon, at, "foot")
            lines.append(f"{icon} {short}: 🚲 {approx}{bike} min · 🚶 {approx}{walk} min")
        return lines
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Travel time failed for {key}: {type(e).__name__}")
        return ["⚠️ Travel times unknown"]


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
    PLACES.update(state.get("places", {}))
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
    print(f"Watching {watched_building()}: {units or 'none'}")

    if test:
        found, failed = available_units(units, set())
        safe_send("\n".join([f"🧪 Magis test, real data",
                             f"Building: {watched_building()}",
                             f"{len(units)} homes of it on the overview page",
                             f"{len(found)} available now"]
                            + ([f"⚠️ Pages failed: {', '.join(failed)}"] if failed else [])))
        for u in found[:2]:
            safe_send("🧪 Test\n" + home_text(u))
        return 0

    if state.get("problem"):
        safe_send("✅ Magis monitor works again")
        state["problem"] = None

    known: dict = state["available"]
    found_all, failed = available_units(units, set(known))
    # only Eindhoven; a home whose city can't be read is kept (with a ⚠️), never dropped
    found = [u for u in found_all if u["city"] in (CITY, None)]
    elsewhere = [u for u in found_all if u not in found]
    if elsewhere:
        print("Not in Eindhoven, no alert: " + ", ".join(f"{u['unit']} ({u['city']})" for u in elsewhere))
    cheap = [u for u in found if u["rent"] is None or u["rent"] <= MAX_RENT]
    pricey = [u for u in found if u not in cheap]
    first_run = not state.get("started")

    if first_run:
        safe_send(f"✅ Magis monitor started\n{len(found)} Eindhoven homes available now"
                  f"\n(Codomo monitor not changed)")

    caller = None
    result: dict = {}
    sure = [u for u in cheap if u["city"] == CITY]      # call only when it's surely Eindhoven
    if sure and not first_run:
        first = sure[0]
        rent = f", {first['rent']:.0f} euro" if first["rent"] is not None else ""
        where = first["street"] or first["unit"].split("/")[0].replace("-", " ")
        say = (f"New Magis home in Eindhoven. {where}{rent}. Check WhatsApp."
               if len(sure) == 1 else
               f"{len(sure)} new Magis homes in Eindhoven. For example {where}{rent}. Check WhatsApp.")
        caller = threading.Thread(target=lambda: result.update(problem=call(say)))
        caller.start()

    for u in cheap[:5]:
        safe_send(home_text(u))
    if pricey:
        safe_send(f"➕ {len(pricey)} new Eindhoven Magis home(s) over {euro(MAX_RENT)}\n{LIST_URL}")

    if caller is not None:
        caller.join(timeout=240)
        problem = result.get("problem", "no answer from Twilio in time")
        if problem:
            safe_send(f"⚠️ Magis phone call failed\n{problem}")

    # keep only units still on the overview page; a unit that comes back counts as new.
    # Failed unit pages are not marked, so they are tried again next run.
    still = {k: v for k, v in known.items() if k in units}
    for u in found_all:              # other cities too, so their pages aren't read again
        still[u["unit"]] = stamp
    state["available"] = still
    state["started"] = True
    state["places"] = PLACES
    save_state(state)
    print(f"{len(found_all)} new available, {len(found)} in Eindhoven, {len(cheap)} under budget, "
          f"{len(failed)} pages failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
