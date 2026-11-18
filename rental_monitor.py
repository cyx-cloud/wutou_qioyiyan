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


def home_text(c: codomo.Card) -> str:
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
    if c.available_from:
        lines.append(f"📅 From {nice_day(c.available_from)}")
    if c.min_stay:
        lines.append(f"⏳ Min {c.min_stay}")
    for t in c.tags:
        lines.append(f"ℹ️ {t}")
    lines.append(f"{SITE}{c.path}")
    return "\n".join(lines)


# ---------------------------------------------------------------- WhatsApp

def send(text: str) -> None:
    if os.environ.get("DRY_RUN") == "1":
        print("----- WhatsApp (dry run) -----")
        print(text)
        print("------------------------------")
        return
    r = requests.get("https://api.callmebot.com/whatsapp.php",
                     params={"phone": os.environ["WHATSAPP_PHONE"], "text": text,
                             "apikey": os.environ["CALLMEBOT_APIKEY"]},
                     timeout=30)
    print(f"CallMeBot HTTP {r.status_code}: {r.text[:200]!r}")
    if r.status_code != 200 or "APIKey is invalid" in r.text:
        raise RuntimeError("CallMeBot did not accept the message")
    time.sleep(3)   # CallMeBot drops messages sent too quickly after each other


# ---------------------------------------------------------------- state

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen": {}, "problem": None, "started": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- main

def main() -> int:
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
        send(("🆕 New home\n" if not first_run else "") + home_text(c))
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
