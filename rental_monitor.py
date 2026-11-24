"""Codomo rental monitor: message me (Telegram, or CallMeBot WhatsApp) when a new home appears.

Reads the Codomo list page in LIST_URL, keeps the homes listed right now in
state.json (committed back to the repo by the workflow), and sends one WhatsApp
message per new home with basic rent <= MAX_RENT. A home that disappears and comes
back later counts as new again.

Env:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   Telegram messages (used when both are set)
  WHATSAPP_PHONE, CALLMEBOT_APIKEY   CallMeBot WhatsApp (fallback without Telegram secrets)
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, CALL_TO   Twilio phone call
  DRY_RUN=1                          print messages instead of sending
  LOCAL_HTML=path[.gz]               read this saved page instead of codomo.eu (testing)
"""
from __future__ import annotations

import gzip
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import codomo

LIST_URL = "https://codomo.eu/homes/nl/en?city=eindhoven&publish_mode=direct_booking"
SITE = "https://codomo.eu"
MAX_RENT = 1100            # basic rent, without service costs
MAX_HOME_MESSAGES = 5      # per run; more new homes -> one summary line
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
            # keep the page so the workflow can upload it, to see what Codomo changed
            Path(__file__).with_name("debug_page.html").write_text(html, encoding="utf-8")
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
    # More homes than the counter is normal: homes on hold for someone else's payment
    # stay on the list but are not counted. Fewer means I may have missed some.
    if expected is not None and len(homes) < expected:
        warnings.append(f"⚠️ Page says {expected} homes, I read only {len(homes)}")
    not_direct = [c for c in homes if not c.direct]
    if not_direct:
        warnings.append(f"⚠️ {len(not_direct)} homes are not 'book directly'")
    other_city = [c for c in homes if c.city and c.city.lower() != "eindhoven"]
    if other_city:
        warnings.append(f"⚠️ {len(other_city)} homes are not in Eindhoven")
    return homes, warnings


# ---------------------------------------------------------------- booking conditions

UNIT_CACHE: dict = {}
_last_unit_fetch = [0.0]


def fetch_unit(c: codomo.Card) -> dict:
    """Who can book and the energy label, from the home's own page. Fetched once per home
    per run (cached), so the call filter and the WhatsApp details share one request.
    robots.txt allows home pages."""
    if c.key in UNIT_CACHE:
        return UNIT_CACHE[c.key]
    info = {"who": None, "label": None, "error": None}
    try:
        local = os.environ.get("LOCAL_UNIT_HTML")
        if local:
            with gzip.open(local, "rt", encoding="utf-8") as f:
                html = f.read()
        else:
            # no wait before the first page (speed), 1 s between pages (be polite)
            wait = 1.0 - (time.time() - _last_unit_fetch[0])
            if wait > 0:
                time.sleep(wait)
            html = get_page(SITE + c.path)
            _last_unit_fetch[0] = time.time()
        unit = codomo.parse_unit(html)
        info["who"] = set(unit.audience)
        info["label"] = energy_label(html)
    except (Problem, codomo.PageError) as e:
        print(f"Unit page failed for {c.name}: {e}")
        info["error"] = str(e)
    UNIT_CACHE[c.key] = info
    return info


def employed_only(c: codomo.Card) -> bool:
    """True only when the home page clearly says employed only. Unknown -> False,
    so a home is never skipped because a page could not be read."""
    return fetch_unit(c)["who"] == {"employed"}


def condition_lines(c: codomo.Card) -> list[str]:
    info = fetch_unit(c)
    if info["error"]:
        # the list card itself sometimes says "Students only"
        return ["⚠️ Booking conditions unknown"] + [f"ℹ️ {t}" for t in c.tags]
    who = info["who"]
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
    label = info["label"]
    lines.append(f"⚡ Energy label {label}" if label else "⚠️ Energy label not found")
    return lines


def energy_label(html: str) -> str | None:
    """The value shown next to "Energy label" on the home page, e.g. "A+" or "C"."""
    text = html.replace('\\"', '"')
    i = text.find('"children":"Energy label"')
    if i < 0:
        return None
    m = re.search(r'"children":"([^"]{1,6})"', text[i + 25:i + 600])
    if m and re.fullmatch(r"[A-G]\+{0,5}", m.group(1)):
        return m.group(1)
    return None


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


def quick_text(c: codomo.Card) -> str:
    """The first, fast message: only what the list page already says, plus the link."""
    lines = ["🆕 New home", f"🏠 {c.name or c.key}"]
    if c.rent is None:
        lines.append("⚠️ Rent not found")
    elif c.extra is not None:
        lines.append(f"💶 {euro(c.rent)} + {euro(c.extra)} costs")
    else:
        lines.append(f"💶 {euro(c.rent)}/month")
    lines.append(f"{SITE}{c.path}")
    return "\n".join(lines)


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
    if c.floor == "0":
        floor = "ground floor"
    elif c.floor:
        floor = f"floor {c.floor}"
    else:
        floor = "⚠️ floor ?"
    size = " · ".join(x for x in [f"{c.area} m²" if c.area else "⚠️ m² ?",
                                  c.bedrooms_label or "⚠️ bedrooms ?", floor] if x)
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
    if os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and os.environ.get("TELEGRAM_CHAT_ID", "").strip():
        send_telegram(text)
        return
    # No Telegram secrets yet: fall back to CallMeBot WhatsApp, as before.
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


def send_telegram(text: str) -> None:
    """Telegram bot message. Never print the URL: it contains the bot token."""
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": os.environ["TELEGRAM_CHAT_ID"].strip(), "text": text},
                          timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"Can't reach Telegram ({type(e).__name__})") from None
    try:
        ok = r.json().get("ok") is True
    except ValueError:
        ok = False
    print(f"Telegram HTTP {r.status_code}, {'delivered' if ok else 'NOT delivered'}")
    if not ok:
        raise RuntimeError("Telegram did not accept the message")
    time.sleep(1)   # Telegram: max about 1 message per second to one chat


def call(text: str) -> str | None:
    """Real phone call through Twilio. Returns a problem text, or None if OK.
    Needs secrets TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM (your Twilio number)
    and CALL_TO (your phone, like +31612345678; falls back to WHATSAPP_PHONE).
    An unanswered call costs nothing; it stops ringing after CALL_RING_SECONDS, before
    voicemail picks up (voicemail would count as answered and cost money)."""
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    from_ = os.environ.get("TWILIO_FROM", "").strip()
    to = (os.environ.get("CALL_TO") or os.environ.get("WHATSAPP_PHONE", "")).strip()
    missing = [n for n, v in [("TWILIO_ACCOUNT_SID", sid), ("TWILIO_AUTH_TOKEN", token),
                              ("TWILIO_FROM", from_), ("CALL_TO", to)] if not v]
    if missing:
        return "missing secret " + ", ".join(missing)
    if os.environ.get("DRY_RUN") == "1":
        print(f"----- Phone call (dry run) -----\n{text}\n--------------------------------")
        return None
    say = (text.replace("&", " and ").replace("<", " ").replace(">", " "))
    twiml = (f'<Response><Say language="en-GB">{say}</Say><Pause length="1"/>'
             f'<Say language="en-GB">{say}</Say></Response>')
    base = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls"
    # Never print the request or Twilio's answer: they contain your phone number
    # (public logs). Problems go only to your own WhatsApp.
    # Full accounts: my own spoken message and a 20 s ring limit.
    # Trial accounts only allow a Twilio template (fixed text) and no ring limit,
    # so on the trial error I retry with only what the trial allows.
    attempts = [
        ("own message", {"To": to, "From": from_, "Twiml": twiml, "Timeout": CALL_RING_SECONDS}),
        ("trial template", {"To": to, "From": from_, "Url": TRIAL_TEMPLATE}),
        ("trial template, no From", {"To": to, "Url": TRIAL_TEMPLATE}),
    ]
    data: dict = {}
    for name, params in attempts:
        try:
            r = requests.post(f"{base}.json", auth=(sid, token), timeout=20, data=params)
        except requests.RequestException as e:
            return f"can't reach Twilio ({type(e).__name__})"
        try:
            data = r.json()
        except ValueError:
            data = {}
        print(f"Phone call ({name}): Twilio HTTP {r.status_code}")
        if r.status_code == 201:
            break
        message = str(data.get("message") or "")
        if "trial" not in message.lower() and "parameter" not in message.lower():
            break           # a real error (wrong number etc.): trying other settings won't help
    if r.status_code != 201:
        return f"Twilio said: {data.get('message') or 'HTTP ' + str(r.status_code)}"

    # follow the call until it ends, to catch "failed" (wrong number etc.)
    call_sid = data.get("sid")
    status = data.get("status", "?")
    for _ in range(CALL_RING_SECONDS // 5 + 12):
        if status in ("completed", "busy", "no-answer", "failed", "canceled") or not call_sid:
            break
        time.sleep(5)
        try:
            status = requests.get(f"{base}/{call_sid}.json", auth=(sid, token),
                                  timeout=20).json().get("status", status)
        except (requests.RequestException, ValueError):
            break
    print(f"Phone call ended: {status}")
    if status == "failed":
        return "Twilio could not make the call (status failed)"
    return None


CALL_RING_SECONDS = 20     # stop ringing before voicemail picks up (full accounts only)
TRIAL_TEMPLATE = "https://webhooks.twilio.com/v1/Voice/Template/voice_text_to_speech"


def call_text(homes: list[codomo.Card]) -> str:
    first = homes[0]
    rent = f", {first.rent:.0f} euro" if first.rent is not None else ""
    if len(homes) == 1:
        return f"New Codomo home. {first.name}{rent}. Check WhatsApp."
    return f"{len(homes)} new Codomo homes. For example {first.name}{rent}. Check WhatsApp."


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
    print(f"TEST RUN (state.json not changed). Read {len(homes)} homes: "
          + ", ".join(f"{c.key} {euro(c.rent)}" for c in homes))
    print(f"{len(cheap)} with rent <= {euro(MAX_RENT)}")
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
    problem = call("This is a test call from your rental monitor. "
                   + (call_text(cheap[:2]) if cheap else "No homes under budget right now."))
    send("🧪 Phone call made, did it ring?" if not problem
         else f"🧪 ⚠️ Phone call failed\n{problem}")
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

    # Call filter, part 2: skip homes that are clearly "employed only" (I'm a student).
    # Needs each home's own page (about 1 s per home), but that page is needed for the
    # details anyway, so it is fetched once and reused.
    to_send, employed, later = [], [], []
    for c in cheap:
        if len(to_send) >= MAX_HOME_MESSAGES:
            later.append(c)          # not marked as seen, so they come in the next runs
        elif employed_only(c):
            employed.append(c)
        else:
            to_send.append(c)

    if first_run:
        head = [f"✅ Rental monitor started",
                f"{len(homes)} homes listed now",
                f"{len(cheap)} with rent ≤ {euro(MAX_RENT)}"]
        send("\n".join(head + warnings))

    # 1) CALL FIRST. It runs in the background: the script follows the call while it rings,
    #    and the WhatsApp messages should not wait for that.
    call_result: dict = {}
    caller = None
    if to_send and not first_run:
        caller = threading.Thread(target=lambda: call_result.update(problem=call(call_text(to_send))))
        caller.start()

    # 2) Short message right away: everything already known from the list page + the link.
    if not first_run:
        for c in to_send:
            send(quick_text(c))

    if warnings and new and not first_run:
        send("\n".join(["Rental monitor"] + warnings))

    # 3) Full details a few seconds later (who can book, energy label, travel times).
    sent = []
    for c in to_send:
        head = "📋 Details\n" if not first_run else ""
        send(head + home_text(c, state.setdefault("places", {})))
        sent.append(c)
    extra_lines = []
    if later:
        extra_lines.append(f"➕ {plural(len(later), 'more home')} ≤ {euro(MAX_RENT)}, next check")
    if not first_run:
        if pricey:
            # not time-critical any more (call and details are out), so check their pages
            # too, to say which ones are employed only (max 5 pages, to stay polite)
            checked = pricey[:5]
            n_emp = sum(1 for c in checked if employed_only(c))
            line = f"➕ {plural(len(pricey), 'new home')} over {euro(MAX_RENT)}"
            if n_emp == len(pricey):
                line += ", employed only" if len(pricey) == 1 else ", all employed only"
            elif n_emp:
                line += f", {n_emp} of them employed only"
            if len(pricey) > len(checked):
                line += f" ({len(pricey) - len(checked)} not checked)"
            extra_lines.append(line)
        if employed:
            extra_lines.append(f"➕ {plural(len(employed), 'new home')} for employed only")
    if extra_lines:
        send("\n".join(extra_lines + [LIST_URL]))

    if caller is not None:
        caller.join(timeout=240)   # stop waiting after 4 min
        problem = call_result.get("problem", "no answer from Twilio in time")
        if problem:
            print("Phone call failed")    # details only on WhatsApp
            send(f"⚠️ Phone call failed\n{problem}")

    # state.json keeps only the homes listed right now. A home that disappears is dropped,
    # so if it comes back later it counts as new and I get a message again.
    # Homes waiting for the next run ("later") are left out, so they still come.
    state["started"] = True
    later_keys = {c.key for c in later}
    listed_now = {c.key: {"first": (seen.get(c.key) or {}).get("first", stamp), "name": c.name}
                  for c in homes if c.key not in later_keys}
    if any(w.startswith("⚠️ Page says") for w in warnings):
        # I may have read only part of the page: don't drop homes now, or they would
        # come back as "new" on the next normal run
        seen.update(listed_now)
    else:
        state["seen"] = listed_now
    save_state(state)
    print(f"{len(new)} new, {len(sent)} sent, {len(later)} for next run, "
          f"{len(pricey)} over budget, {len(employed)} employed only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
