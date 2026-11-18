"""Codomo rental monitor: WhatsApp me (CallMeBot) when a new home appears.

Reads the Codomo list page in LIST_URL, keeps the homes it has already seen in
state.json (committed back to the repo by the workflow), and sends one WhatsApp
message per new home with basic rent <= MAX_RENT.

Env:
  WHATSAPP_PHONE, CALLMEBOT_APIKEY   CallMeBot (required unless DRY_RUN=1)
  DRY_RUN=1                          print messages instead of sending
  LOCAL_HTML=path[.gz]               read this saved page instead of codomo.eu (testing)
"""
from __future__ import annotations

import gzip
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import codomo

LIST_URL = "https://codomo.eu/homes/nl/en?city=eindhoven&publish_mode=direct_booking"
SITE = "https://codomo.eu"
MAX_RENT = 1200            # basic rent, without service costs
MAX_HOME_MESSAGES = 5      # per run; more new homes -> one summary line
FORGET_AFTER_DAYS = 30     # drop homes from state.json when gone this long
PAGE_SIZE = 25             # Codomo shows 25 homes per list page
MAX_PAGES = 6
STATE_FILE = Path(__file__).with_name("state.json")
USER_AGENT = "personal-rental-monitor/1.0 (one request per run)"

# Travel time to Eindhoven Centraal, free OpenStreetMap services, no API key:
#   Nominatim (address -> coordinates), max 1 request per second
#   routing.openstreetmap.de (FOSSGIS OSRM), bike and foot profiles
EINDHOVEN_CS = (51.4434, 5.4797)   # lat, lon of the station entrance
NOMINATIM = "https://nominatim.openstreetmap.org/search"
ROUTER = "https://routing.openstreetmap.de/routed-{mode}/route/v1/{mode}/{a};{b}"
GEO_USER_AGENT = "personal-rental-monitor/1.0 (travel time for a few homes per day)"


class Problem(Exception):
    """Something went wrong reading Codomo. kind groups errors so I only warn once."""

    def __init__(self, kind: str, text: str):
        super().__init__(text)
        self.kind = kind
        self.text = text


# ---------------------------------------------------------------- reading Codomo

def get_page(url: str) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
                         timeout=30, allow_redirects=False)
    except requests.RequestException as e:
        raise Problem("unreachable", f"Can't reach Codomo ({type(e).__name__})")
    head = r.text[:5000] if r.content else ""
    loc = r.headers.get("Location", "")
    if r.status_code == 429:
        raise Problem("blocked", "Codomo says too many requests (429)")
    if r.status_code in (403, 503) and (r.headers.get("cf-mitigated") or "Just a moment" in head):
        raise Problem("blocked", "Cloudflare is blocking the GitHub server")
    if "maintenance" in loc.lower() or (r.status_code in (502, 503) and "maintenance" in head.lower()):
        raise Problem("maintenance", "Codomo is down for maintenance")
    if r.status_code == 403:
        raise Problem("blocked", "Codomo refused the request (403)")
    if 300 <= r.status_code < 400:
        raise Problem("changed", f"Codomo redirects the page to {loc or 'somewhere else'}")
    if r.status_code != 200:
        raise Problem("unreachable", f"Codomo answered HTTP {r.status_code}")
    return r.text


def read_homes() -> tuple[list[codomo.Card], list[str]]:
    """All homes on the list page (all pages). Also returns warnings for the message."""
    warnings: list[str] = []
    local = os.environ.get("LOCAL_HTML")
    found: dict[str, codomo.Card] = {}
    counts = None
    for page in range(1, MAX_PAGES + 1):
        if local:
            opener = gzip.open if local.endswith(".gz") else open
            with opener(local, "rt", encoding="utf-8") as f:
                html = f.read()
        else:
            if page > 1:
                time.sleep(3)
            html = get_page(LIST_URL + (f"&page={page}" if page > 1 else ""))
        try:
            cards, counts = codomo.parse_list(html)
        except codomo.PageError as e:
            raise Problem("changed", f"Codomo changed its page layout ({e})")
        new = 0
        for c in cards:
            if c.path not in found:
                found[c.path] = c
                new += 1
        # a page past the end shows page 1 again
        if local or len(cards) < PAGE_SIZE or new == 0:
            break
    homes = list(found.values())

    # the page's own number of book-directly homes; far off means I misread it
    expected = counts.booking.get("direct_booking") if counts else None
    if expected is not None and (len(homes) > expected or (expected >= 4 and len(homes) < expected / 2)):
        warnings.append(f"⚠️ Page says {expected} homes, I read {len(homes)}")
    not_direct = [c for c in homes if not c.direct]
    if not_direct:
        warnings.append(f"⚠️ {len(not_direct)} homes are not 'book directly'")
    other_city = [c for c in homes if c.city and c.city.lower() != "eindhoven"]
    if other_city:
        warnings.append(f"⚠️ {len(other_city)} homes are not in Eindhoven")
    return homes, warnings


# ---------------------------------------------------------------- booking conditions

def condition_lines(c: codomo.Card) -> list[str]:
    """Who can book (students / employed), from the home's own page (one request per
    home that gets a message; robots.txt allows home pages)."""
    try:
        local = os.environ.get("LOCAL_UNIT_HTML")
        if local:
            with gzip.open(local, "rt", encoding="utf-8") as f:
                html = f.read()
        else:
            time.sleep(2)
            html = get_page(SITE + c.path)
        unit = codomo.parse_unit(html)
    except (Problem, codomo.PageError) as e:
        print(f"Unit page failed for {c.name}: {e}")
        # the list card itself sometimes says "Students only"
        return ["⚠️ Booking conditions unknown"] + [f"ℹ️ {t}" for t in c.tags]

    who = set(unit.audience)
    lines = []
    if who == {"student"}:
        lines.append("🎓 Students only")
    elif who == {"employed"}:
        lines.append("💼 Employed only")
    elif who == {"student", "employed"}:
        lines.append("👤 Students or employed")
    elif who:
        lines.append(f"👤 For: {', '.join(sorted(who))}")
    else:
        lines.append("⚠️ Who can book: not found")

    return lines


# ---------------------------------------------------------------- travel time to CS

def geocode(street: str, city: str) -> tuple[float, float] | None:
    time.sleep(1.1)    # Nominatim: max 1 request per second
    r = requests.get(NOMINATIM, params={"street": street, "city": city, "country": "Netherlands",
                                        "format": "jsonv2", "limit": 1},
                     headers={"User-Agent": GEO_USER_AGENT}, timeout=20)
    r.raise_for_status()
    hits = r.json()
    if not hits:
        return None
    return float(hits[0]["lat"]), float(hits[0]["lon"])


def find_place(c: codomo.Card, places: dict) -> dict:
    """Coordinates of a home, cached in state.json so each address is looked up once.
    "rough" = only the street was found, not the house number."""
    key = f"{c.name}, {c.city or 'Eindhoven'}"
    if key in places:
        return places[key]
    city = c.city or "Eindhoven"
    place = {"lat": None, "lon": None, "rough": False}
    point = geocode(c.name, city)
    if point is None:
        # "Boschdijk 293B" or "Kastanjelaan 1-18" may be unknown: try the street alone
        street = re.sub(r"\s+\d.*$", "", c.name).strip()
        if street and street != c.name:
            point = geocode(street, city)
            place["rough"] = point is not None
    if point is not None:
        place["lat"], place["lon"] = point
        places[key] = place     # only cache successes, so a failure is retried next time
    return place


def minutes_to_cs(lat: float, lon: float, mode: str) -> int:
    url = ROUTER.format(mode=mode, a=f"{lon},{lat}", b=f"{EINDHOVEN_CS[1]},{EINDHOVEN_CS[0]}")
    r = requests.get(url, params={"overview": "false"},
                     headers={"User-Agent": GEO_USER_AGENT}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError(f"router answered {data.get('code')}")
    return max(1, round(data["routes"][0]["duration"] / 60))


def travel_line(c: codomo.Card, places: dict) -> str:
    try:
        place = find_place(c, places)
        if place["lat"] is None:
            return "⚠️ Address not found on map"
        bike = minutes_to_cs(place["lat"], place["lon"], "bike")
        walk = minutes_to_cs(place["lat"], place["lon"], "foot")
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Travel time failed for {c.name}: {type(e).__name__} {e}")
        return "⚠️ Time to CS unknown"
    rough = "≈" if place["rough"] else ""
    return f"🚲 {rough}{bike} min · 🚶 {rough}{walk} min to CS"


# ---------------------------------------------------------------- message text

def euro(x: float | None) -> str:
    if x is None:
        return "?"
    return f"€{x:,.0f}"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def nice_day(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%-d %b %Y")
    except ValueError:
        return iso or "?"


def home_text(c: codomo.Card, places: dict) -> str:
    lines = [f"🏠 {c.name or c.key}"]
    if c.building:
        lines.append(f"🏢 {c.building}")
    if c.rent is None:
        lines.append("⚠️ Rent not found")
    elif c.extra is not None:
        lines.append(f"💶 {euro(c.rent)} + {euro(c.extra)} costs")
        lines.append(f"💶 Total {euro(c.total)}/month")
    else:
        lines.append(f"💶 {euro(c.rent)}/month")
        lines.append("⚠️ Service costs not found")
    size = " · ".join(x for x in [f"{c.area} m²" if c.area else "", c.bedrooms_label] if x)
    if size:
        lines.append(f"📐 {size}")
    lines.append(travel_line(c, places))
    if c.available_from:
        lines.append(f"📅 From {nice_day(c.available_from)}")
    if c.min_stay:
        lines.append(f"⏳ Min {c.min_stay}")
    lines += condition_lines(c)
    lines.append(f"{SITE}{c.path}")
    return "\n".join(lines)


# ---------------------------------------------------------------- WhatsApp

def send(text: str) -> None:
    if os.environ.get("DRY_RUN") == "1":
        print("----- WhatsApp (dry run) -----")
        print(text)
        print("------------------------------")
        return
    # The repo is public, so its Actions logs are public too. Never print the request URL or
    # CallMeBot's answer: both can contain the phone number (URL-encoded, so GitHub's
    # automatic *** masking of secrets would miss it).
    try:
        r = requests.get("https://api.callmebot.com/whatsapp.php",
                         params={"phone": os.environ["WHATSAPP_PHONE"], "text": text,
                                 "apikey": os.environ["CALLMEBOT_APIKEY"]},
                         timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"Can't reach CallMeBot ({type(e).__name__})") from None
    ok = r.status_code == 200 and "APIKey is invalid" not in r.text
    print(f"CallMeBot HTTP {r.status_code}, {'accepted' if ok else 'NOT accepted'}")
    if not ok:
        raise RuntimeError("CallMeBot did not accept the message")
    time.sleep(3)   # CallMeBot drops messages sent too quickly after each other


# ---------------------------------------------------------------- state

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen": {}, "problem": None, "started": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- test mode

def test_run() -> int:
    """Real Codomo page, real map lookups, real WhatsApp, but state.json is not changed.
    Sends what is listed right now, so I can compare it with the website."""
    try:
        homes, warnings = read_homes()
    except Problem as p:
        send(f"🧪 Test failed\n⚠️ {p.text}")
        return 0
    cheap = sorted([c for c in homes if c.rent is not None and c.rent <= MAX_RENT], key=lambda c: c.rent)
    send("\n".join([f"🧪 Test, real data",
                    f"{plural(len(homes), 'home')} listed now",
                    f"{len(cheap)} with rent ≤ {euro(MAX_RENT)}"] + warnings))
    places: dict = {}
    if cheap:
        for c in cheap[:2]:
            send("🧪 Test\n" + home_text(c, places))
    elif homes:
        c = min(homes, key=lambda c: c.rent if c.rent is not None else 10**9)
        send(f"🧪 Test, cheapest home\n(over {euro(MAX_RENT)}, only to show the format)\n"
             + home_text(c, places))
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    if os.environ.get("TEST") == "true":
        return test_run()
    state = load_state()
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M")
    first_run = not state.get("started")

    try:
        homes, warnings = read_homes()
    except Problem as p:
        print(f"PROBLEM [{p.kind}]: {p.text}")
        if state.get("problem") != p.kind:      # warn once per kind, not every run
            send(f"⚠️ Rental monitor\n{p.text}\nI keep trying.")
            state["problem"] = p.kind
            save_state(state)
        return 0

    print(f"Read {len(homes)} homes: " + ", ".join(f"{c.key} {euro(c.rent)}" for c in homes))
    if state.get("problem"):
        send("✅ Rental monitor works again")
        state["problem"] = None

    seen: dict = state["seen"]
    new = [c for c in homes if c.key not in seen]
    cheap = [c for c in new if c.rent is None or c.rent <= MAX_RENT]   # unknown rent: show it
    pricey = [c for c in new if c.rent is not None and c.rent > MAX_RENT]

    if first_run:
        head = [f"✅ Rental monitor started",
                f"{len(homes)} homes listed now",
                f"{len(cheap)} with rent ≤ {euro(MAX_RENT)}"]
        send("\n".join(head + warnings))
    elif warnings and new:
        send("\n".join(["Rental monitor"] + warnings))

    sent = []
    for c in cheap[:MAX_HOME_MESSAGES]:
        send(("🆕 New home\n" if not first_run else "") + home_text(c, state.setdefault("places", {})))
        sent.append(c)
    later = cheap[len(sent):]   # not marked as seen, so they come in the next runs
    extra_lines = []
    if later:
        extra_lines.append(f"➕ {plural(len(later), 'more home')} ≤ {euro(MAX_RENT)}, next check")
    if pricey and not first_run:
        extra_lines.append(f"➕ {plural(len(pricey), 'new home')} over {euro(MAX_RENT)}")
    if extra_lines:
        send("\n".join(extra_lines + [LIST_URL]))

    # only mark homes as seen once the messages went out
    state["started"] = True
    later_keys = {c.key for c in later}
    for c in homes:
        if c.key in later_keys:
            continue
        # "last" is a day, so state.json (and the repo) changes at most once a day per home
        seen.setdefault(c.key, {"first": stamp, "name": c.name})["last"] = now.strftime("%Y-%m-%d")
    cutoff = (now - timedelta(days=FORGET_AFTER_DAYS)).strftime("%Y-%m-%d")
    for k in [k for k, v in seen.items() if v.get("last", "") < cutoff]:
        del seen[k]
    save_state(state)
    print(f"{len(new)} new, {len(sent)} sent, {len(later)} for next run, {len(pricey)} over budget")
    return 0


if __name__ == "__main__":
    sys.exit(main())
