#!/usr/bin/env python3
"""LAYOVER DETECTION from AIMS eCrew — the roster the bot consumes.

Uses the company-wide endpoints:
  * POST /eCrew/FlightInformation/FetchFlightInfoAction  {OptionIdx:1 = All Flights}
  * POST /eCrew/FlightInformation/ShowCrewOnFlight        {LegId: <legid string>}

Algorithm: a crew member who ARRIVES at a foreign airport is on layover if
their next departure from that airport is at least MIN_LAYOVER_HOURS away
(measured from actual times where AIMS has them). Set difference
(arrived − departed) auto-handles split crews and PAX/DHC returns (a
deadheading member shows up in the departed set); the hour threshold then
separates a genuine rest from a same-day turnaround.

This is a standalone mirror of scripts/ecrew_layovers.py from the private
LayoverBot repo, kept in this SEPARATE PUBLIC repository for one reason: a
private repo's GitHub Actions minutes are metered, a public repo's are not,
and this hourly scan is what was burning through them. It holds no
credentials — ECREW_USERNAME/ECREW_PASSWORD live in this repo's own Actions
Secrets — and no operational data; it only reads AIMS and writes a JSON roster
that the bot (running elsewhere, on Oracle) picks up.

Run in GitHub Actions (reaches AIMS; Oracle cannot). Window length via
ECREW_DAYS (default 3).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import datetime as dt

# Inlined from the private LayoverBot repo's flight_stats.py, which this repo
# does not carry — it comes with a Database/Oracle apparatus this scanner has
# no business pulling in. Keep the two in sync by hand if the cell format ever
# changes; the pair is small enough that a diff catches drift on sight.
_CELL = re.compile(
    r"^\s*([A-Z]{3})\s*(\d{4})"       # airport + scheduled
    r"(?:E(\d{4}))?"                  # estimate, when published
    r"(?:A(\d{4})(\d{4})?)?"          # actual, then the delay if late
)


def _to_minutes(hhmm: str) -> int | None:
    try:
        value = int(hhmm)
    except (TypeError, ValueError):
        return None
    hours, minutes = divmod(value, 100)
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes


def parse_times(raw: str) -> dict:
    """Scheduled / estimated / actual minutes-of-day from one AIMS time cell.
    Unknown parts come back as None rather than guesses — the report says how
    much it could parse so a format surprise shows up instead of hiding."""
    match = _CELL.match(raw or "")
    if not match:
        return {"sched": None, "est": None, "actual": None}
    _, sched, est, actual, _delay = match.groups()
    return {
        "sched": _to_minutes(sched),
        "est": _to_minutes(est) if est else None,
        "actual": _to_minutes(actual) if actual else None,
    }

BASE = os.getenv("ECREW_BASE_URL", "https://aims.airastana.com").rstrip("/")
USER = os.getenv("ECREW_USERNAME", "").strip()
PW = os.getenv("ECREW_PASSWORD", "").strip()
DAYS = int(os.getenv("ECREW_DAYS", "3"))
# Also scan a couple of days BACK so a layover that already started (crew still
# resting) stays visible after its arrival day — needed for retro-add on register.
BACKFILL = int(os.getenv("ECREW_BACKFILL", "2"))
# Optional explicit window (YYYY-MM-DD, inclusive) — overrides DAYS/BACKFILL.
# Lets the study re-scan a week that already happened, where every leg carries
# its actual times and final crew.
FROM_DATE = os.getenv("ECREW_FROM", "").strip()
TO_DATE = os.getenv("ECREW_TO", "").strip()
# Skip the per-leg crew lookup. No layovers are detected without it (the roster
# comes out empty), so this is ONLY for schedule-shape scans — never for the run
# that feeds the bot.
SKIP_CREW = os.getenv("ECREW_SKIP_CREW", "").strip() in ("1", "true", "yes")
# Fetch crew for EVERY leg, domestic ones included. The bot never needs this —
# it only asks who is on a layover abroad — but the study does: you cannot show a
# crew stayed in a city without being able to see them not flying anywhere else,
# and their Almaty–Astana legs are where they actually were. Roughly triples the
# number of per-leg requests, so it is off unless a study run asks for it.
CREW_ALL = os.getenv("ECREW_CREW_ALL", "").strip() in ("1", "true", "yes")
# How long a crew must be on the ground before it counts as a layover worth a
# chat. The old rule was "did not depart the same calendar day", which is not a
# duration at all: it created a chat for a seven-hour night stop that crossed
# midnight, and created none for eighteen hours that happened to fit inside one
# day. Ten hours, measured — matching the crew-rest floor the planner uses.
MIN_LAYOVER_HOURS = float(os.getenv("LAYOVER_MIN_HOURS", "10"))

FIA = BASE + "/eCrew/FlightInformation/FetchFlightInfoAction"
CREW = BASE + "/eCrew/FlightInformation/ShowCrewOnFlight"

# Kazakhstan (domestic) airports — arrivals here are NOT layover destinations.
KZ = {"ALA", "NQZ", "TSE", "NUR", "ATY", "GUW", "CIT", "KGF", "KSN", "PWQ",
      "SCO", "AKX", "KZO", "URA", "PPK", "DMB", "HRC", "EKB", "UKK", "DZN",
      "AYK", "BXH", "UST", "USK", "PLX"}

XHR_JS = """
(args) => new Promise((resolve) => {
  const [url, body, method] = args;
  try {
    const xhr = new XMLHttpRequest();
    xhr.open(method || 'POST', url, true);
    xhr.setRequestHeader('Content-type', 'application/json');
    xhr.onreadystatechange = () => {
      if (xhr.readyState === 4) resolve({status: xhr.status, text: xhr.responseText});
    };
    xhr.onerror = () => resolve({status: -1, text: 'xhr error'});
    xhr.send(body ? JSON.stringify(body) : null);
  } catch (e) { resolve({status: -2, text: String(e)}); }
})
"""

_TAG = re.compile(r"<[^>]+>")
_SPAN3 = re.compile(r"<span>\s*([A-Z0-9]{3})\b")
_PID = re.compile(r"^\s*(\d+)")


async def call(page, url, body=None, method="POST"):
    return await page.evaluate(XHR_JS, [url, body, method])


def first_airport(html_field: str) -> str:
    m = _SPAN3.search(html_field or "")
    return m.group(1) if m else ""


def strip_tags(s: str) -> str:
    return _TAG.sub("", s or "").strip()


_ACTUAL = re.compile(r"\bA\d{3,4}\b")  # e.g. "A1036" = actual (flight has landed)


def has_landed(arrival_field: str) -> bool:
    """True when the arrival field carries an ACTUAL time (A-prefixed) — i.e. the
    flight has really arrived. Avoids creating groups for diversions/returns
    (those either show a different airport or have no actual arrival yet)."""
    return bool(_ACTUAL.search(arrival_field or ""))


def leg_moment(leg: dict, which: str) -> dt.datetime | None:
    """When a leg departed or arrived, as a datetime. Prefers the ACTUAL time
    where AIMS has one. An arrival earlier than its departure crossed midnight."""
    try:
        day = dt.datetime.fromisoformat(str(leg.get("date")))
    except (TypeError, ValueError):
        return None
    cells = {"dep": parse_times(leg.get("dep_raw", "")),
             "arr": parse_times(leg.get("arr_raw", ""))}
    minutes = cells[which]["actual"]
    if minutes is None:
        minutes = cells[which]["sched"]
    if minutes is None:
        return None
    moment = day + dt.timedelta(minutes=minutes)
    if which == "arr":
        dep_minutes = cells["dep"]["actual"]
        if dep_minutes is None:
            dep_minutes = cells["dep"]["sched"]
        if dep_minutes is not None and minutes < dep_minutes:
            moment += dt.timedelta(days=1)
    return moment


def legs_needing_crew(legs: list, crew_all: bool = False) -> list:
    """Which legs to spend a crew lookup on.

    The bot only ever asks "who is on a layover", so legs that touch a foreign
    airport are enough for it, and one request per leg is what dominates the
    scan. The STUDY needs more: to prove a crew was really on the ground in a
    city, you have to be able to see them NOT flying anywhere else — and a crew
    that went home and spent three days on Almaty–Astana is invisible unless
    domestic legs carry their crew too. That is how Urumqi kept producing
    145-hour layovers on a flight that arrives every other day.
    """
    if crew_all:
        return [lg for lg in legs if lg.get("legid")]
    return [lg for lg in legs
            if (lg["arr"] and lg["arr"] not in KZ)
            or (lg["dep"] and lg["dep"] not in KZ)]


def departure_moments(dep_by_city: dict, crew_of: dict) -> dict:
    """Per city: every departure with a readable time, earliest first, paired with
    who was on it."""
    moments = {}
    for city, city_legs in dep_by_city.items():
        timed = [(leg_moment(lg, "dep"), crew_of.get(lg["legid"], set()))
                 for lg in city_legs]
        moments[city] = sorted(((m, c) for m, c in timed if m), key=lambda x: x[0])
    return moments


def hours_on_the_ground(dep_moments: dict, city: str, pid: str,
                        arr_dt: dt.datetime) -> float | None:
    """How long this person waits in the city for their next flight out of it.

    None when no such departure is inside the scanned window. That is missing
    information, not a short stay, and the caller must not read it as one."""
    for moment, crew in dep_moments.get(city, []):
        if moment > arr_dt and pid in crew:
            return (moment - arr_dt).total_seconds() / 3600
    return None


def person_ids(crew_rows) -> set:
    ids = set()
    for c in crew_rows or []:
        m = _PID.match(str(c.get("id_name", "")))
        if m:
            ids.add(m.group(1))
    return ids


# Flight-deck ranks as AIMS writes them. Everything else on the crew list is
# cabin. Matched on the VALUE rather than a field name: the aircraft-type field
# was never found by guessing key names, and this avoids repeating that.
FLIGHT_DECK_RANKS = {
    "CP", "CPT", "CAP", "CA", "PIC", "COM",       # captain
    "FO", "F/O", "SFO", "FP", "COP",              # first officer
    "SO", "RP", "CRP", "IP", "TRI", "TRE",        # relief / instructor
}
_RANK_TOKEN = re.compile(r"^[A-Z/]{2,4}$")


def crew_ranks(crew_rows) -> dict:
    """eCrew person id -> rank string, for whichever field carries it.

    Returns {} when no field on the row looks like a rank; the caller reports
    that rather than silently treating every crew member as cabin."""
    out = {}
    for c in crew_rows or []:
        m = _PID.match(str(c.get("id_name", "")))
        if not m:
            continue
        for key, value in (c or {}).items():
            if key == "id_name":
                continue
            token = str(value or "").strip().upper()
            if _RANK_TOKEN.match(token) and token in FLIGHT_DECK_RANKS:
                out[m.group(1)] = token
                break
        else:
            # Present but not flight deck — record what it said, so cabin ranks
            # are distinguishable from "we failed to read the row".
            for key, value in (c or {}).items():
                token = str(value or "").strip().upper()
                if key != "id_name" and _RANK_TOKEN.match(token):
                    out[m.group(1)] = token
                    break
    return out


def is_pilot(rank: str) -> bool:
    return str(rank or "").strip().upper() in FLIGHT_DECK_RANKS


def person_names(crew_rows) -> dict:
    """Map eCrew person id -> "NAME SURNAME" from id_name ("3084 - KHASSANOV ...")."""
    out = {}
    for c in crew_rows or []:
        s = str(c.get("id_name", ""))
        m = _PID.match(s)
        if not m:
            continue
        name = s.split("-", 1)[1].strip() if "-" in s else ""
        out[m.group(1)] = name
    return out


def find_rows(txt: str):
    try:
        data = json.loads(txt)
    except Exception:
        return []
    def rec(o, d=0):
        if d > 5:
            return None
        if isinstance(o, list) and o and isinstance(o[0], dict):
            return o
        if isinstance(o, dict):
            for v in o.values():
                r = rec(v, d + 1)
                if r:
                    return r
        return None
    return rec(data) or []


async def login(page):
    await page.goto(BASE + "/eCrew", wait_until="networkidle", timeout=45000)
    pw_loc = page.locator('input[placeholder="Password"]')
    if await pw_loc.count() == 0:
        pw_loc = page.locator('input[type="password"]:visible')
    user_loc = page.locator('input[type="text"]:visible').first
    await user_loc.click(timeout=8000)
    await user_loc.fill(USER)
    await pw_loc.first.click(timeout=8000)
    await pw_loc.first.fill(PW)
    await pw_loc.first.press("Enter")
    try:
        await page.wait_for_url("**/Dashboard**", timeout=20000)
    except Exception:
        await page.wait_for_timeout(6000)


async def goto_ready(page, url, tries=3) -> bool:
    for _ in range(tries):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        cond = ("() => !!(window.$$ && typeof $$ === 'function' && window.jQuery "
                "&& document.querySelector('input[name=\\\"__RequestVerificationToken\\\"]'))")
        try:
            await page.wait_for_function(cond, timeout=20000)
            await page.wait_for_timeout(600)
            return True
        except Exception:
            await page.wait_for_timeout(1200)
    return False


def fia_body(dobj: dt.date):
    return {"Airport": "", "FlightNo": "", "ArrDep": 0, "Carrier": "", "ACType": "",
            "ACReg": "", "ForDate": dobj.strftime("%d/%m/%Y"), "TimesIn": "1", "OptionIdx": 1}


async def main() -> None:
    if not USER or not PW:
        print("❌ No ECREW_USERNAME / ECREW_PASSWORD in env.")
        sys.exit(0)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        ctx = await browser.new_context(
            locale="en-US",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"))
        page = await ctx.new_page()

        print("logging in…")
        await login(page)
        ok = await goto_ready(page, BASE + "/eCrew/Dashboard")
        print("dashboard ready:", ok, "| URL:", page.url)

        today = dt.date.today()
        if FROM_DATE and TO_DATE:
            # Explicit date range: scan a week that has already happened, so every
            # flight carries its ACTUAL times and final crew. Used by the study.
            start = dt.date.fromisoformat(FROM_DATE)
            end = dt.date.fromisoformat(TO_DATE)
            if end < start:
                raise SystemExit(f"ECREW_TO ({TO_DATE}) is before ECREW_FROM ({FROM_DATE})")
            window = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
            print(f"explicit window: {start} .. {end} ({len(window)} days)")
        else:
            window = [today + dt.timedelta(days=i) for i in range(-BACKFILL, DAYS)]

        # 1) collect all legs across the window (dedupe by legid)
        legs = {}  # legid -> {date, dep, arr, flt, legid}
        for d in window:
            for attempt in range(3):
                r = await call(page, FIA, fia_body(d), "POST")
                rows = find_rows(r.get("text") or "")
                if rows:
                    break
                await page.wait_for_timeout(1500)
            n_new = 0
            for row in rows:
                legid = row.get("legid")
                if not legid or legid in legs:
                    continue
                legs[legid] = {
                    "date": row.get("date") or d.isoformat(),
                    "dep": first_airport(row.get("departure", "")),
                    "arr": first_airport(row.get("arrival", "")),
                    "flt": strip_tags(row.get("flight", "")),
                    "landed": has_landed(row.get("arrival", "")),
                    "legid": legid,
                    # Keep the fields verbatim (tags stripped). They carry the
                    # scheduled AND actual times; storing the raw text means the
                    # history can be re-parsed later without re-scraping AIMS.
                    "dep_raw": strip_tags(row.get("departure", "")),
                    "arr_raw": strip_tags(row.get("arrival", "")),
                    # Aircraft type. Which fleet a route is flown by decides whether
                    # it is biddable at all — a narrow-body pilot cannot take a
                    # wide-body layover, however long it is. The key name is not
                    # documented, so try the plausible ones (the keys of the first
                    # row are printed below when this comes back empty).
                    "actype": strip_tags(
                        row.get("actype") or row.get("acType") or row.get("ac_type")
                        or row.get("aircraft") or row.get("acTypeCode") or ""),
                }
                n_new += 1
            print(f"  {d:%a %d/%m}: {len(rows)} flights ({n_new} new legs)")
        print(f"total unique legs: {len(legs)}")
        if legs and not any(lg["actype"] for lg in legs.values()):
            # None of the guessed key names matched. Print what the row actually
            # has, so the right one can be picked without another scrape.
            sample = next(iter(find_rows((await call(page, FIA, fia_body(window[0]),
                                                     "POST")).get("text") or "")), {})
            print(f"!! no aircraft type parsed. row keys: {sorted(sample.keys())}")

        # 2) which legs need a crew lookup?
        foreign_legs = legs_needing_crew(list(legs.values()), CREW_ALL)
        print(f"legs needing crew: {len(foreign_legs)}"
              f"{' (ALL legs — domestic included)' if CREW_ALL else ' (foreign-touching only)'}")

        crew_of = {}   # legid -> set(person ids)
        names = {}     # person id -> "NAME SURNAME" (for member tags)
        ranks = {}     # person id -> rank ("CP"/"FO"/cabin code), for the study
        rank_keys_reported = False
        if SKIP_CREW:
            # Schedule-only scan. Crew lookup is one request PER LEG and dominates
            # the runtime, but questions about the SHAPE of the schedule — how long
            # a layover in a city lasts, which weekday it starts — are answered by
            # the flight times alone. Months can then be scanned in a minute.
            print("ECREW_SKIP_CREW=1 — schedule only, no crew lookups")
            foreign_legs = []
        for i, lg in enumerate(foreign_legs):
            r = await call(page, CREW, {"LegId": lg["legid"]}, "POST")
            crew_rows = find_rows(r.get("text") or "")
            crew_of[lg["legid"]] = person_ids(crew_rows)
            names.update(person_names(crew_rows))
            found = crew_ranks(crew_rows)
            ranks.update(found)
            if not found and crew_rows and not rank_keys_reported:
                # Same failure the aircraft type hit: guessing a field name and
                # getting silence. Print the row's shape once so the next run can
                # be fixed against real data instead of another guess.
                rank_keys_reported = True
                print("!! no rank parsed from the crew row. keys: "
                      f"{sorted((crew_rows[0] or {}).keys())}")
            if (i + 1) % 25 == 0:
                print(f"    crew fetched {i + 1}/{len(foreign_legs)}")
        print(f"crew fetched for all foreign legs"
              f" ({sum(1 for r in ranks.values() if is_pilot(r))} flight deck,"
              f" {len(ranks)} with a rank)")

        # 3) per foreign city per day: arrived − departed(same day) = staying
        # index legs by (city, date) for arrivals and departures
        arr_idx, dep_idx = {}, {}
        for lg in legs.values():
            if lg["arr"] and lg["arr"] not in KZ:
                arr_idx.setdefault((lg["arr"], lg["date"]), []).append(lg)
            if lg["dep"] and lg["dep"] not in KZ:
                dep_idx.setdefault((lg["dep"], lg["date"]), []).append(lg)

        # departures from a city across ALL dates (for layover-length lookahead)
        dep_by_city = {}
        for lg in legs.values():
            if lg["dep"] and lg["dep"] not in KZ:
                dep_by_city.setdefault(lg["dep"], []).append(lg)

        def next_departure_day(city, pid, after_date):
            best = None
            for lg in dep_by_city.get(city, []):
                if lg["date"] > after_date and pid in crew_of.get(lg["legid"], set()):
                    if best is None or lg["date"] < best:
                        best = lg["date"]
            return best

        # The same departures, with times, for measuring the stay in hours.
        dep_moments = departure_moments(dep_by_city, crew_of)

        print("\n" + "=" * 62 + "\nLAYOVERS DETECTED (dry run — nothing created)\n" + "=" * 62)
        city_totals = {}
        group_sizes = []
        layovers = []  # machine-readable roster the bot consumes
        for (city, date), arr_legs in sorted(arr_idx.items()):
            arrived = set()
            arrived_landed = set()   # arrived on a flight that ACTUALLY landed here
            in_flts = []
            landed_at = {}           # pid -> when they touched down here
            for lg in arr_legs:
                crew = crew_of.get(lg["legid"], set())
                arrived |= crew
                moment = leg_moment(lg, "arr")
                if moment:
                    for pid in crew:
                        if pid not in landed_at or moment < landed_at[pid]:
                            landed_at[pid] = moment
                if lg.get("landed"):
                    arrived_landed |= crew
                in_flts.append(lg["flt"])
            departed = set()
            for lg in dep_idx.get((city, date), []):
                departed |= crew_of.get(lg["legid"], set())

            # A layover is a duration, not a date comparison: they are staying if
            # their next flight out of this city is at least MIN_LAYOVER_HOURS
            # away. No departure in view means we cannot tell — treat them as
            # staying, because a chat that should not exist is recoverable and a
            # crew left without one is not.
            staying = set()
            stay_hours = {}
            for pid in arrived:
                arr_dt = landed_at.get(pid)
                if arr_dt is None:
                    if pid not in departed:      # unreadable times: old rule
                        staying.add(pid)
                    continue
                gap = hours_on_the_ground(dep_moments, city, pid, arr_dt)
                if gap is None:
                    staying.add(pid)
                elif gap >= MIN_LAYOVER_HOURS:
                    staying.add(pid)
                    stay_hours[pid] = round(gap, 1)
            if not staying:
                continue
            # CONFIRMED = arrived on a flight that really landed AND not yet departed.
            # This excludes diversions/returns (they never land at this airport).
            confirmed = (arrived_landed - departed) & staying
            group_sizes.append(len(staying))
            city_totals[city] = city_totals.get(city, 0) + len(staying)
            # estimate layover length per staying member (next departure from city)
            lengths = []
            for pid in staying:
                nd = next_departure_day(city, pid, date)
                if nd:
                    try:
                        lengths.append(
                            (dt.date.fromisoformat(nd) - dt.date.fromisoformat(date)).days)
                    except Exception:
                        pass
            avg_len = round(sum(lengths) / len(lengths), 1) if lengths else None
            measured = sorted(stay_hours.values())
            est_hours = measured[len(measured) // 2] if measured else None
            status = ("confirmed" if confirmed and len(confirmed) == len(staying)
                      else "partial" if confirmed else "planned")
            crew_list = [{"id": pid, "name": names.get(pid, ""), "confirmed": pid in confirmed}
                         for pid in sorted(staying, key=lambda x: int(x) if x.isdigit() else 0)]
            # Per-arrival-flight crew breakdown so the bot can build an EPHEMERAL
            # crew chat per flight (each экипаж = one flight's crew), separate from
            # the permanent city chat. Only flights that actually landed contribute
            # confirmed crew.
            flights = []
            for lg in arr_legs:
                if not lg.get("landed"):
                    continue
                fcrew = crew_of.get(lg["legid"], set())
                fconfirmed = sorted(fcrew & confirmed, key=lambda x: int(x) if x.isdigit() else 0)
                if not fconfirmed:
                    continue
                flights.append({
                    "flight": lg["flt"],
                    "dep": lg["dep"],
                    "legid": lg["legid"],
                    "confirmed_ids": fconfirmed,
                    "crew": [{"id": pid, "name": names.get(pid, "")} for pid in fconfirmed],
                })
            layovers.append({
                "city": city,
                "date": date,
                "arrival_flights": sorted(set(in_flts)),
                "staying_count": len(staying),
                "confirmed_count": len(confirmed),
                "status": status,          # bot creates groups only for confirmed crew
                "est_length_days": avg_len,
                "est_length_hours": est_hours,
                "crew": crew_list,
                "crew_ids": [c["id"] for c in crew_list],
                "confirmed_ids": sorted(confirmed, key=lambda x: int(x) if x.isdigit() else 0),
                "flights": flights,
            })
            print(f"\n{city}  {date}  arrivals={sorted(set(in_flts))}  [{status}]")
            print(f"   staying: {len(staying)} crew  (landed/confirmed: {len(confirmed)})  "
                  f"est.length(days)~{avg_len if avg_len is not None else '?'}")

        # write the machine-readable roster for the bot to consume
        roster = {
            "generated_at": dt.datetime.utcnow().isoformat() + "Z",
            "window_days": DAYS,
            "window": [window[0].isoformat(), window[-1].isoformat()],
            "cities": dict(sorted(city_totals.items(), key=lambda kv: -kv[1])),
            "layovers": layovers,
        }
        os.makedirs("artifacts", exist_ok=True)
        out_path = os.path.join("artifacts", "ecrew_layovers.json")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(roster, fh, ensure_ascii=False, indent=2)
        print(f"\nwrote roster -> {out_path} ({len(layovers)} layover entries)")

        # Raw per-leg snapshot for the flight-history study: how far actual times
        # drift from the schedule, how often a crew changes after publication, and
        # how many chats are needed at peak. Free to produce — every field here was
        # already fetched above, so this costs AIMS nothing extra.
        snapshot = {
            "captured_at": dt.datetime.utcnow().isoformat() + "Z",
            "window": [window[0].isoformat(), window[-1].isoformat()],
            "legs": [
                {
                    "legid": lg["legid"],
                    "date": lg["date"],
                    "flt": lg["flt"],
                    "dep": lg["dep"],
                    "arr": lg["arr"],
                    "dep_raw": lg.get("dep_raw", ""),
                    "arr_raw": lg.get("arr_raw", ""),
                    "landed": bool(lg.get("landed")),
                    "crew": sorted(crew_of.get(lg["legid"], ())),
                    # Rank per person, so the study can separate flight deck from
                    # cabin. Empty when AIMS gave us nothing we could read.
                    "crew_roles": {pid: ranks[pid]
                                   for pid in crew_of.get(lg["legid"], ())
                                   if pid in ranks},
                }
                for lg in legs.values()
            ],
            "layovers": [
                {
                    "city": lv["city"], "date": lv["date"], "status": lv["status"],
                    "est_length_days": lv["est_length_days"],
                    "flights": [f["flight"] for f in lv.get("flights", [])],
                    "staying_count": lv["staying_count"],
                    "confirmed_count": lv["confirmed_count"],
                }
                for lv in layovers
            ],
        }
        snap_path = os.path.join("artifacts", "ecrew_flights.json")
        with open(snap_path, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False)
        print(f"wrote flight snapshot -> {snap_path} ({len(snapshot['legs'])} legs)")

        print("\n" + "-" * 62)
        print("SUMMARY")
        print(f"  window: {DAYS} days ({window[0]:%d/%m}–{window[-1]:%d/%m})")
        print(f"  layover cities: {len(city_totals)}")
        top = sorted(city_totals.items(), key=lambda kv: -kv[1])
        print(f"  by staying-crew: {json.dumps(top, ensure_ascii=False)}")
        if group_sizes:
            group_sizes.sort()
            print(f"  layover groups: {len(group_sizes)}  "
                  f"min/median/max size = {group_sizes[0]}/"
                  f"{group_sizes[len(group_sizes)//2]}/{group_sizes[-1]}")
        by_status = {}
        for lv in layovers:
            by_status[lv["status"]] = by_status.get(lv["status"], 0) + 1
        print(f"  entries by status: {json.dumps(by_status, ensure_ascii=False)} "
              f"(bot acts on confirmed/partial crew only)")
        print(f"  crew API calls made: {len(foreign_legs)}")

        await browser.close()
    print("\n✅ layover dry-run finished")


if __name__ == "__main__":
    asyncio.run(main())
