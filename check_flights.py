"""
Velocity Flight Tracker — v3
Madrid (MAD) → Sydney (SYD) | 14 Sep 2026 | 2 adults | max 1 stop
Carriers: Qatar (preferred), Emirates, Etihad, Singapore Airlines
APIs: seats.aero (Velocity rewards) + Amadeus (cash fares + price analysis)
Notification: Daily 7am AEST summary via Slack or WhatsApp
Change detection: persists previous results to state.json in repo via GitHub API
"""

import os
import json
import base64
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

# ── Configuration ─────────────────────────────────────────────────────────────

TRAVEL_DATE  = "2026-09-14"
PASSENGERS   = 2
MAX_STOPS    = 1

ALL_CARRIERS = ["QR", "EK", "EY", "SQ"]
CARRIER_NAMES = {
    "QR": "Qatar Airways",
    "EK": "Emirates",
    "EY": "Etihad",
    "SQ": "Singapore Airlines",
}

VELOCITY_PROGRAM = "velocity"

# Deal thresholds (per person)
DEAL_CASH_BIZ_PP    = 3_000     # AUD
DEAL_POINTS_BIZ_PP  = 135_000   # Velocity points

# Notification
NOTIFY_VIA = os.environ.get("NOTIFY_VIA", "slack")

# Secrets
SEATS_API_KEY         = os.environ.get("SEATS_API_KEY", "")
AMADEUS_CLIENT_ID     = os.environ.get("AMADEUS_CLIENT_ID", "")
AMADEUS_CLIENT_SECRET = os.environ.get("AMADEUS_CLIENT_SECRET", "")
SLACK_WEBHOOK_URL     = os.environ.get("SLACK_WEBHOOK_URL", "")
TWILIO_ACCOUNT_SID    = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN     = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_WHATSAPP  = os.environ.get("TWILIO_FROM_WHATSAPP", "")
TWILIO_TO_WHATSAPP    = os.environ.get("TWILIO_TO_WHATSAPP", "")

# GitHub state persistence
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")   # auto-provided by Actions
GITHUB_REPO  = os.environ.get("GITHUB_REPOSITORY", "")  # auto-provided: "owner/repo"
STATE_FILE   = "state.json"

# ── Points Booster Price Table (AUD incl. GST, standard no-promo rates) ───────

BOOSTER_TABLE = {
    1_000: 36,     1_500: 51,     2_000: 68,     2_500: 85,
    3_000: 101,    3_500: 118,    4_000: 135,    4_500: 152,
    5_000: 168,    6_000: 201,    7_000: 233,    8_000: 264,
    9_000: 296,    10_000: 325,   11_000: 353,   12_000: 375,
    13_000: 399,   14_000: 427,   15_000: 452,   16_000: 469,
    17_000: 492,   18_000: 512,   19_000: 535,   20_000: 555,
    21_000: 580,   22_000: 605,   23_000: 629,   24_000: 653,
    25_000: 677,   26_000: 698,   27_000: 720,   28_000: 743,
    29_000: 765,   30_000: 787,   35_000: 894,   40_000: 994,
    45_000: 1_086, 50_000: 1_172, 60_000: 1_404, 70_000: 1_638,
    80_000: 1_872, 90_000: 2_106, 100_000: 2_340,
    150_000: 3_510, 200_000: 4_680, 250_000: 5_850,
}
BOOSTER_TIERS = sorted(BOOSTER_TABLE.keys())


def booster_cost(points_needed: int) -> tuple[int, int]:
    """Round up to nearest booster tier; split purchases >250k. Returns (pts_bought, aud_cost)."""
    total_cost = 0
    total_bought = 0
    remaining = points_needed
    while remaining > 0:
        chunk = min(remaining, 250_000)
        tier = next((t for t in BOOSTER_TIERS if t >= chunk), 250_000)
        total_cost += BOOSTER_TABLE[tier]
        total_bought += tier
        remaining -= chunk
    return total_bought, total_cost


# ── State Persistence (GitHub repo file) ─────────────────────────────────────

def load_previous_state() -> dict:
    """Fetch state.json from the GitHub repo. Returns {} if not found."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("[State] No GitHub token/repo — skipping diff.")
        return {}
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_FILE}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code == 404:
        return {}
    if resp.status_code != 200:
        print(f"[State] Load error {resp.status_code}")
        return {}
    content = resp.json().get("content", "")
    try:
        return json.loads(base64.b64decode(content).decode())
    except Exception:
        return {}


def save_current_state(data: dict, previous_sha: str | None = None):
    """Write state.json back to the GitHub repo."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_FILE}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    # Get current SHA if we don't have it (needed for updates)
    if not previous_sha:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            previous_sha = r.json().get("sha")

    payload = {
        "message": f"tracker: state update {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "content": base64.b64encode(json.dumps(data, indent=2).encode()).decode(),
    }
    if previous_sha:
        payload["sha"] = previous_sha

    resp = requests.put(url, headers=headers, json=payload, timeout=15)
    print(f"[State] Save {'OK' if resp.status_code in (200, 201) else f'error {resp.status_code}'}")


def get_previous_sha() -> str | None:
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_FILE}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    resp = requests.get(url, headers=headers, timeout=15)
    return resp.json().get("sha") if resp.status_code == 200 else None


# ── Amadeus Auth ──────────────────────────────────────────────────────────────

_amadeus_cache: dict = {}


def get_amadeus_token() -> str:
    now = datetime.utcnow().timestamp()
    if _amadeus_cache.get("token") and now < _amadeus_cache.get("expiry", 0) - 60:
        return _amadeus_cache["token"]
    resp = requests.post(
        "https://test.api.amadeus.com/v1/security/oauth2/token",
        data={
            "grant_type":    "client_credentials",
            "client_id":     AMADEUS_CLIENT_ID,
            "client_secret": AMADEUS_CLIENT_SECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _amadeus_cache["token"]  = data["access_token"]
    _amadeus_cache["expiry"] = now + data["expires_in"]
    return _amadeus_cache["token"]


# ── Amadeus: Cash Fares ───────────────────────────────────────────────────────

def search_cash_fares(origin: str, dest: str, cabin: str) -> list[dict]:
    """
    Returns top cash fare offers for our allowed carriers, sorted by:
    Qatar first, then cheapest of others.
    cabin: "BUSINESS" or "ECONOMY"
    """
    if not AMADEUS_CLIENT_ID:
        return []
    try:
        token = get_amadeus_token()
    except Exception as e:
        print(f"  [Amadeus cash] Auth error: {e}")
        return []

    try:
        resp = requests.get(
            "https://test.api.amadeus.com/v2/shopping/flight-offers",
            params={
                "originLocationCode":      origin,
                "destinationLocationCode": dest,
                "departureDate":           TRAVEL_DATE,
                "adults":                  PASSENGERS,
                "travelClass":             cabin,
                "max":                     50,
                "currencyCode":            "AUD",
                "nonStop":                 "false",
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        resp.raise_for_status()
        offers = resp.json().get("data", [])
    except Exception as e:
        print(f"  [Amadeus cash] Search error ({origin}→{dest}): {e}")
        return []

    qatar_results  = []
    other_results  = []

    for offer in offers:
        itins = offer.get("itineraries", [])
        if not itins:
            continue
        segments = itins[0].get("segments", [])
        if len(segments) > MAX_STOPS + 1:
            continue

        carriers_on_flight = set()
        for seg in segments:
            code = (seg.get("operating", {}).get("carrierCode")
                    or seg.get("carrierCode", ""))
            carriers_on_flight.add(code)

        if not carriers_on_flight.issubset(set(ALL_CARRIERS)):
            continue

        price_total = float(offer.get("price", {}).get("grandTotal", 0))
        price_pp    = price_total / PASSENGERS
        via         = segments[1]["departure"]["iataCode"] if len(segments) > 1 else "—"
        layover_min = _layover_minutes(segments)
        total_min   = _total_minutes(itins[0].get("duration", ""))
        dep         = segments[0]["departure"]["at"]
        arr         = segments[-1]["arrival"]["at"]
        flight_nums = " / ".join(
            f"{seg.get('carrierCode','')}{seg.get('number','')}" for seg in segments
        )
        carrier_codes = sorted(carriers_on_flight)
        carrier_label = ", ".join(CARRIER_NAMES.get(c, c) for c in carrier_codes)
        is_qatar      = "QR" in carriers_on_flight

        record = {
            "cabin":         cabin.lower(),
            "carrier_codes": carrier_codes,
            "carrier_label": carrier_label,
            "is_qatar":      is_qatar,
            "flight_nums":   flight_nums,
            "via":           via,
            "layover_min":   layover_min,
            "total_min":     total_min,
            "dep":           dep,
            "arr":           arr,
            "price_total":   price_total,
            "price_pp":      price_pp,
        }
        if is_qatar:
            qatar_results.append(record)
        else:
            other_results.append(record)

    qatar_results.sort(key=lambda x: x["price_total"])
    other_results.sort(key=lambda x: x["price_total"])

    # Return Qatar cheapest + one cheapest non-Qatar
    results = []
    if qatar_results:
        results.append(qatar_results[0])
    if other_results:
        results.append(other_results[0])
    return results


def _layover_minutes(segments: list) -> int:
    """Calculate layover duration in minutes between first and second segment."""
    if len(segments) < 2:
        return 0
    try:
        arr1 = datetime.fromisoformat(segments[0]["arrival"]["at"])
        dep2 = datetime.fromisoformat(segments[1]["departure"]["at"])
        return int((dep2 - arr1).total_seconds() / 60)
    except Exception:
        return 0


def _total_minutes(iso_duration: str) -> int:
    """Parse ISO 8601 duration string like PT21H40M into total minutes."""
    if not iso_duration:
        return 0
    import re
    h = int(m[0]) if (m := re.findall(r"(\d+)H", iso_duration)) else 0
    mins = int(m[0]) if (m := re.findall(r"(\d+)M", iso_duration)) else 0
    return h * 60 + mins


def fmt_duration(minutes: int) -> str:
    if not minutes:
        return "—"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def fmt_datetime(iso: str) -> str:
    """Format ISO datetime to '14 Sep 23:55'."""
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%-d %b %H:%M")
    except Exception:
        return iso[:16]


# ── Amadeus: Price Analysis ───────────────────────────────────────────────────

def get_price_analysis(origin: str, dest: str, cabin: str) -> dict | None:
    """
    Calls Amadeus itinerary-price-metrics endpoint.
    Returns dict with percentile and label, or None if unavailable.
    cabin: "BUSINESS" or "ECONOMY"
    """
    if not AMADEUS_CLIENT_ID:
        return None
    try:
        token = get_amadeus_token()
    except Exception:
        return None

    # Amadeus price metrics uses departure month
    dep_month = TRAVEL_DATE[:7]   # "2026-09"

    try:
        resp = requests.get(
            "https://test.api.amadeus.com/v1/analytics/itinerary-price-metrics",
            params={
                "originIataCode":      origin,
                "destinationIataCode": dest,
                "departureDate":       dep_month,
                "currencyCode":        "AUD",
                "oneWay":              "true",
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", [])
        if not data:
            return None
    except Exception as e:
        print(f"  [Amadeus price analysis] Error ({origin}→{dest}): {e}")
        return None

    # Find the cabin-relevant price metrics
    for item in data:
        metrics = item.get("priceMetrics", [])
        for m in metrics:
            if m.get("travelClass", "").upper() == cabin.upper():
                # Extract percentile buckets
                amounts = m.get("amount", {})
                # amounts typically has keys: min, max, medium, low, high
                # We'll interpolate our current price against the range
                return {
                    "min":    float(amounts.get("min", 0)),
                    "max":    float(amounts.get("max", 0)),
                    "median": float(amounts.get("medium", 0)),
                    "low":    float(amounts.get("low", 0)),
                    "high":   float(amounts.get("high", 0)),
                }
    return None


def price_percentile_label(price_pp: float, metrics: dict | None) -> str:
    """
    Given a per-person price and the metrics dict, return a short
    contextual label: percentile estimate + emoji.
    """
    if not metrics or not metrics.get("median"):
        return "_(price data unavailable)_"

    mn  = metrics["min"]
    low = metrics["low"]
    med = metrics["median"]
    hi  = metrics["high"]
    mx  = metrics["max"]

    # Rough percentile estimation from the 5-point distribution
    if price_pp <= mn:
        pct, emoji = "~10th", "🟢"
    elif price_pp <= low:
        pct, emoji = "~25th", "🟢"
    elif price_pp <= med:
        # Interpolate between 25th and 50th
        ratio = (price_pp - low) / max(med - low, 1)
        p = int(25 + ratio * 25)
        pct, emoji = f"~{p}th", "🟢"
    elif price_pp <= hi:
        ratio = (price_pp - med) / max(hi - med, 1)
        p = int(50 + ratio * 25)
        pct, emoji = f"~{p}th", "🟡"
    elif price_pp <= mx:
        ratio = (price_pp - hi) / max(mx - hi, 1)
        p = int(75 + ratio * 25)
        pct, emoji = f"~{p}th", "🔴"
    else:
        pct, emoji = ">95th", "🔴"

    vs_median = price_pp - med
    direction = f"AUD ${abs(vs_median):,.0f} {'above' if vs_median > 0 else 'below'} median"
    return f"{pct} percentile · {direction} {emoji}"


# ── seats.aero: Velocity Reward Seats ────────────────────────────────────────

def search_velocity_seats(origin: str, dest: str, cabin: str) -> list[dict]:
    """
    Returns Qatar cheapest + one cheapest non-Qatar Velocity reward option.
    cabin: "business" or "economy"
    """
    if not SEATS_API_KEY:
        return []

    is_biz     = cabin == "business"
    avail_key  = "JAvailable"      if is_biz else "YAvailable"
    seats_key  = "JRemainingSeats" if is_biz else "YRemainingSeats"
    points_key = "JMileageCost"    if is_biz else "YMileageCost"
    taxes_key  = "JTaxes"          if is_biz else "YTaxes"

    try:
        resp = requests.get(
            "https://seats.aero/partnerapi/search",
            params={
                "origin_airport":      origin,
                "destination_airport": dest,
                "start_date":          TRAVEL_DATE,
                "end_date":            TRAVEL_DATE,
                "cabin":               cabin,
                "take":                50,
            },
            headers={
                "Partner-Authorization": SEATS_API_KEY,
                "Accept": "application/json",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception as e:
        print(f"  [seats.aero] Error ({origin}→{dest} {cabin}): {e}")
        return []

    qatar_results = []
    other_results = []

    for item in data:
        if item.get("Source", "").lower() != VELOCITY_PROGRAM:
            continue
        if not item.get(avail_key, False):
            continue

        points_pp   = int(item.get(points_key, 0) or 0)
        taxes_pp    = float(item.get(taxes_key, 0) or 0)
        seats_avail = int(item.get(seats_key, 0) or 0)
        carriers    = item.get("Carriers", "")
        flights     = item.get("FlightNumbers", "")

        carrier_codes = [c.strip() for c in carriers.split(",") if c.strip()]
        if not any(c in ALL_CARRIERS for c in carrier_codes):
            continue

        total_points   = points_pp * PASSENGERS
        taxes_total    = taxes_pp * PASSENGERS
        pts_bought, booster_aud = booster_cost(total_points)
        booster_total  = booster_aud + taxes_total
        is_qatar       = "QR" in carrier_codes

        record = {
            "cabin":          cabin,
            "carrier_codes":  carrier_codes,
            "carrier_label":  ", ".join(CARRIER_NAMES.get(c, c) for c in carrier_codes),
            "is_qatar":       is_qatar,
            "flights":        flights,
            "seats_avail":    seats_avail,
            "points_pp":      points_pp,
            "taxes_pp":       taxes_pp,
            "total_points":   total_points,
            "taxes_total":    taxes_total,
            "pts_bought":     pts_bought,
            "booster_aud":    booster_aud,
            "booster_total":  booster_total,
        }
        if is_qatar:
            qatar_results.append(record)
        else:
            other_results.append(record)

    qatar_results.sort(key=lambda x: x["points_pp"])
    other_results.sort(key=lambda x: x["points_pp"])

    results = []
    if qatar_results:
        results.append(qatar_results[0])
    if other_results:
        results.append(other_results[0])
    return results


# ── Change Detection ──────────────────────────────────────────────────────────

def summarise_for_state(cash_biz, pts_biz,
                        cash_biz_mad_doh, pts_biz_mad_doh,
                        cash_biz_doh_syd, pts_biz_doh_syd,
                        cash_eco, pts_eco) -> dict:
    """Flatten key metrics into a simple dict for diff comparison."""
    def cash_pp(offers):
        return round(offers[0]["price_pp"]) if offers else None

    def pts_pp(offers):
        return offers[0]["points_pp"] if offers else None

    return {
        "biz_cash_pp":         cash_pp(cash_biz),
        "biz_pts_pp_through":  pts_pp(pts_biz),
        "biz_pts_pp_doh_syd":  pts_pp(pts_biz_doh_syd),
        "eco_cash_pp":         cash_pp(cash_eco),
        "eco_pts_pp":          pts_pp(pts_eco),
        "biz_seats_through":   pts_biz[0]["seats_avail"] if pts_biz else 0,
        "biz_seats_doh_syd":   pts_biz_doh_syd[0]["seats_avail"] if pts_biz_doh_syd else 0,
    }


def build_diff(current: dict, previous: dict) -> list[str]:
    """Return list of human-readable change strings, or [] if no changes."""
    if not previous:
        return ["_(first run — no previous data)_"]

    labels = {
        "biz_cash_pp":         "Business cash (pp)",
        "biz_pts_pp_through":  "Business points through (pp)",
        "biz_pts_pp_doh_syd":  "DOH→SYD points (pp)",
        "eco_cash_pp":         "Economy cash (pp)",
        "eco_pts_pp":          "Economy points (pp)",
        "biz_seats_through":   "Business seats (through)",
        "biz_seats_doh_syd":   "Business seats (DOH→SYD)",
    }
    changes = []
    for key, label in labels.items():
        cur = current.get(key)
        prev = previous.get(key)
        if cur is None and prev is None:
            continue
        if cur != prev:
            is_price = "cash" in key or "pts" in key
            if prev is None:
                changes.append(f"   {label}: now available ({'AUD $' + f'{cur:,}' if is_price else str(cur)}) 🆕")
            elif cur is None:
                changes.append(f"   {label}: no longer available ❌")
            else:
                diff = cur - prev
                unit = "pts" if "pts" in key else ("seats" if "seats" in key else "AUD $")
                sign = "+" if diff > 0 else ""
                emoji = ("🔴" if diff > 0 and is_price else
                         "🟢" if diff < 0 and is_price else
                         "🟢" if diff > 0 and "seats" in key else "🔴")
                val_str = f"AUD ${cur:,}" if "cash" in key else f"{cur:,}"
                prev_str = f"AUD ${prev:,}" if "cash" in key else f"{prev:,}"
                changes.append(
                    f"   {label}: {prev_str} → {val_str} "
                    f"({sign}{abs(diff):,} {unit}) {emoji}"
                )

    return changes if changes else ["   No changes from yesterday"]


# ── Report Builder ────────────────────────────────────────────────────────────

UNAVAIL = "— unavail —"
COL1 = 20   # label column width
COL2 = 18   # first data column
COL3 = 18   # second data column


def row(label: str, v1: str, v2: str = "") -> str:
    return f"{label:<{COL1}}{v1:<{COL2}}{v2}"


def section_header(title: str) -> str:
    return f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n{title}\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


def format_cash_offer(o: dict) -> str:
    deal = "  🔥 GREAT DEAL" if (o["cabin"] == "business" and o["price_pp"] < DEAL_CASH_BIZ_PP) else ""
    star = "⭐ " if o["is_qatar"] else "   "
    return (
        f"{star}{o['carrier_label']}\n"
        + f"```"
        + f"\n{row('Flights', o['flight_nums'])}"
        + f"\n{row('Via / Stops', f\"{o['via']} · 1 stop\")}"
        + f"\n{row('Departs', fmt_datetime(o['dep']))}"
        + f"\n{row('Arrives', fmt_datetime(o['arr']))}"
        + f"\n{row('Layover', fmt_duration(o['layover_min']))}"
        + f"\n{row('Total time', fmt_duration(o['total_min']))}"
        + f"\n{row('Cash (2 pax)', f\"AUD ${o['price_total']:,.0f}  (~${o['price_pp']:,.0f} pp)\")}{deal}"
        + f"```"
    )


def format_points_offer(o: dict, show_deal: bool = True) -> str:
    deal = ("  🔥 GREAT DEAL"
            if show_deal and o["cabin"] == "business" and o["points_pp"] < DEAL_POINTS_BIZ_PP
            else "")
    star = "⭐ " if o["is_qatar"] else "   "
    return (
        f"{star}{o['carrier_label']}\n"
        + f"```"
        + f"\n{row('Flights', o['flights'])}"
        + f"\n{row('Seats avail', str(o['seats_avail']))}"
        + f"\n{row('Points (pp)', f\"{o['points_pp']:,}  ({o['total_points']:,} total)\")}{deal}"
        + f"\n{row('Points + taxes', f\"{o['total_points']:,} pts + AUD ${o['taxes_total']:,.0f}\")}"
        + f"\n{row('Buy pts cost', f\"AUD ${o['booster_aud']:,.0f}  ({o['pts_bought']:,} pts)\")}"
        + f"\n{row('All-in (buy pts)', f\"AUD ${o['booster_total']:,.0f}\")}"
        + f"```"
    )


def format_price_insight(origin: str, dest: str, cabin: str, price_pp: float) -> str:
    metrics = get_price_analysis(origin, dest, cabin)
    label   = price_percentile_label(price_pp, metrics)
    route   = f"{origin}→{dest} {cabin.title()}"
    return f"_Price insight ({route}): {label}_"


def build_report(
    today_str: str,
    diff_lines: list[str],
    cash_biz: list, pts_biz: list,
    cash_biz_mad_doh: list, pts_biz_mad_doh: list,
    cash_biz_doh_syd: list, pts_biz_doh_syd: list,
    cash_eco: list, pts_eco: list,
) -> str:
    lines = []

    # ── Header ────────────────────────────────────────────────────────────────
    lines.append(
        f"✈️ *MAD → SYD  |  14 Sep 2026  |  2 adults  |  {today_str}*"
    )

    # ── Changes ───────────────────────────────────────────────────────────────
    lines.append("\n*📋 CHANGES SINCE YESTERDAY*")
    lines.extend(diff_lines)

    # ── Business Through ──────────────────────────────────────────────────────
    lines.append("\n" + section_header("BUSINESS — THROUGH BOOKING"))

    if cash_biz:
        for o in cash_biz:
            lines.append(format_cash_offer(o))
        # Price insight on Qatar if available
        qatar_cash = next((o for o in cash_biz if o["is_qatar"]), None)
        if qatar_cash:
            lines.append(format_price_insight("MAD", "SYD", "BUSINESS", qatar_cash["price_pp"]))
    else:
        lines.append("_No cash fares found (Amadeus)_")

    if pts_biz:
        lines.append("*Velocity reward seats:*")
        for o in pts_biz:
            lines.append(format_points_offer(o))
    else:
        lines.append("_No Velocity reward seats (through itinerary)_")

    # ── Business Self-Transfer ────────────────────────────────────────────────
    lines.append("\n" + section_header("BUSINESS — SELF-TRANSFER  _(book legs separately)_"))

    lines.append("*Leg 1: MAD → DOH*")
    if cash_biz_mad_doh:
        for o in cash_biz_mad_doh:
            lines.append(format_cash_offer(o))
    else:
        lines.append("_No cash fares found_")

    if pts_biz_mad_doh:
        for o in pts_biz_mad_doh:
            lines.append(format_points_offer(o))
    else:
        lines.append("_No Velocity reward seats_")

    lines.append("*Leg 2: DOH → SYD  ⚑ priority leg*")
    if cash_biz_doh_syd:
        for o in cash_biz_doh_syd:
            lines.append(format_cash_offer(o))
        qatar_doh = next((o for o in cash_biz_doh_syd if o["is_qatar"]), None)
        if qatar_doh:
            lines.append(format_price_insight("DOH", "SYD", "BUSINESS", qatar_doh["price_pp"]))
    else:
        lines.append("_No cash fares found_")

    if pts_biz_doh_syd:
        for o in pts_biz_doh_syd:
            lines.append(format_points_offer(o))
    else:
        lines.append("_No Velocity reward seats_")

    # Combined self-transfer totals (if both legs have data)
    if (cash_biz_mad_doh or pts_biz_mad_doh) and (cash_biz_doh_syd or pts_biz_doh_syd):
        lines.append("*Combined self-transfer:*")
        if cash_biz_mad_doh and cash_biz_doh_syd:
            qt1 = next((o for o in cash_biz_mad_doh if o["is_qatar"]), cash_biz_mad_doh[0])
            qt2 = next((o for o in cash_biz_doh_syd if o["is_qatar"]), cash_biz_doh_syd[0])
            combined_cash = qt1["price_total"] + qt2["price_total"]
            lines.append(f"```\n{row('Cash (2 pax)', f'AUD ${combined_cash:,.0f}')}\n```")
        if pts_biz_mad_doh and pts_biz_doh_syd:
            qt1 = next((o for o in pts_biz_mad_doh if o["is_qatar"]), pts_biz_mad_doh[0])
            qt2 = next((o for o in pts_biz_doh_syd if o["is_qatar"]), pts_biz_doh_syd[0])
            combined_pts   = qt1["total_points"] + qt2["total_points"]
            combined_taxes = qt1["taxes_total"] + qt2["taxes_total"]
            _, combined_booster = booster_cost(combined_pts)
            combined_allin = combined_booster + combined_taxes
            lines.append(
                f"```"
                f"\n{row('Points + taxes', f'{combined_pts:,} pts + AUD ${combined_taxes:,.0f}')}"
                f"\n{row('All-in (buy pts)', f'AUD ${combined_allin:,.0f}')}"
                f"\n```"
            )

    # ── Economy Through ───────────────────────────────────────────────────────
    lines.append("\n" + section_header("ECONOMY — THROUGH BOOKING"))

    if cash_eco:
        for o in cash_eco:
            lines.append(format_cash_offer(o))
        qatar_eco = next((o for o in cash_eco if o["is_qatar"]), None)
        if qatar_eco:
            lines.append(format_price_insight("MAD", "SYD", "ECONOMY", qatar_eco["price_pp"]))
    else:
        lines.append("_No cash fares found_")

    if pts_eco:
        lines.append("*Velocity reward seats:*")
        for o in pts_eco:
            lines.append(format_points_offer(o, show_deal=False))
    else:
        lines.append("_No Velocity reward seats_")

    # ── Assessment ────────────────────────────────────────────────────────────
    lines.append("\n" + "─" * 50)
    lines.append("*📊 ASSESSMENT*")

    all_pts_biz = pts_biz + pts_biz_doh_syd
    if all_pts_biz:
        best_pts = min(o["points_pp"] for o in all_pts_biz)
        pts_emoji = "🟢" if best_pts < DEAL_POINTS_BIZ_PP else "🟡"
        lines.append(f"   Best Business points: {best_pts:,} pp {pts_emoji}")
    else:
        lines.append("   Business reward seats: none found 🔴")

    if cash_biz:
        best_cash = min(o["price_pp"] for o in cash_biz)
        cash_emoji = "🟢" if best_cash < DEAL_CASH_BIZ_PP else "🟡"
        lines.append(f"   Best Business cash: AUD ${best_cash:,.0f} pp {cash_emoji}")
    else:
        lines.append("   Business cash fares: none found 🔴")

    today = datetime.utcnow().date()
    months_out = ((datetime(2026, 9, 14).date()) - today).days / 30.44
    if months_out > 6:
        lines.append(f"   💡 {months_out:.0f} months out — prime booking window opens May/Jun 2026")
    elif months_out > 3:
        lines.append(f"   ⚡ {months_out:.0f} months out — act quickly on good availability")
    else:
        lines.append(f"   ⚠️ Only {months_out:.0f} months out — book urgently if suitable")

    lines.append("─" * 50)

    return "\n".join(lines)


# ── Notifications ─────────────────────────────────────────────────────────────

def send_slack(message: str):
    if not SLACK_WEBHOOK_URL:
        print("[Slack] No webhook configured.")
        return
    resp = requests.post(SLACK_WEBHOOK_URL, json={"text": message}, timeout=15)
    print(f"[Slack] {'Sent ✓' if resp.status_code == 200 else f'Error {resp.status_code}'}")


def send_whatsapp(message: str):
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                TWILIO_FROM_WHATSAPP, TWILIO_TO_WHATSAPP]):
        print("[WhatsApp] Twilio credentials incomplete.")
        return
    url = (f"https://api.twilio.com/2010-04-01/Accounts/"
           f"{TWILIO_ACCOUNT_SID}/Messages.json")
    for i, chunk in enumerate(
        [message[i:i+1500] for i in range(0, len(message), 1500)], 1
    ):
        resp = requests.post(
            url,
            data={"From": TWILIO_FROM_WHATSAPP, "To": TWILIO_TO_WHATSAPP, "Body": chunk},
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            timeout=15,
        )
        print(f"[WhatsApp] Part {i}: {'✓' if resp.status_code in (200,201) else resp.status_code}")


def notify(message: str):
    if NOTIFY_VIA == "whatsapp":
        send_whatsapp(message)
    else:
        send_slack(message)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    aedt     = ZoneInfo("Australia/Sydney")
    today_str = datetime.now(aedt).strftime("%-d %b %Y %I:%M %p AEST")

    print(f"\n{'='*60}")
    print(f"Velocity Tracker v3 — {today_str}")
    print(f"{'='*60}\n")

    # Load yesterday's state for diff
    previous_state = load_previous_state()
    previous_sha   = get_previous_sha()

    # ── Fetch all data ────────────────────────────────────────────────────────
    print("Fetching Business through...")
    cash_biz = search_cash_fares("MAD", "SYD", "BUSINESS")
    pts_biz  = search_velocity_seats("MAD", "SYD", "business")

    print("Fetching Business self-transfer legs...")
    cash_biz_mad_doh = search_cash_fares("MAD", "DOH", "BUSINESS")
    pts_biz_mad_doh  = search_velocity_seats("MAD", "DOH", "business")
    cash_biz_doh_syd = search_cash_fares("DOH", "SYD", "BUSINESS")
    pts_biz_doh_syd  = search_velocity_seats("DOH", "SYD", "business")

    print("Fetching Economy through...")
    cash_eco = search_cash_fares("MAD", "SYD", "ECONOMY")
    pts_eco  = search_velocity_seats("MAD", "SYD", "economy")

    # ── Diff ──────────────────────────────────────────────────────────────────
    current_state = summarise_for_state(
        cash_biz, pts_biz,
        cash_biz_mad_doh, pts_biz_mad_doh,
        cash_biz_doh_syd, pts_biz_doh_syd,
        cash_eco, pts_eco,
    )
    diff_lines = build_diff(current_state, previous_state)

    # ── Build & send report ───────────────────────────────────────────────────
    report = build_report(
        today_str,
        diff_lines,
        cash_biz, pts_biz,
        cash_biz_mad_doh, pts_biz_mad_doh,
        cash_biz_doh_syd, pts_biz_doh_syd,
        cash_eco, pts_eco,
    )

    print("\n" + report + "\n")
    notify(report)

    # ── Persist state ─────────────────────────────────────────────────────────
    save_current_state(current_state, previous_sha)

    print("Done.\n")


if __name__ == "__main__":
    main()
