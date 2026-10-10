"""
Singapore PSI + Weather combined Telegram bot.

One hourly message (PSI, PM2.5, temperature, rainfall, 2-hour forecast),
plus the existing midnight daily/weekly PSI chart reports.

Required secrets / environment variables:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""
import io
import json
import math
import os
import re
import sys
import time as time_module
from datetime import date, datetime, time, timedelta
from html import escape, unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")  # Render images on GitHub without a display.
import matplotlib.dates as mdates
import matplotlib.patches
import matplotlib.pyplot as plt
import requests

# ==========================================================
# CONFIGURATION
# ==========================================================
API_BASE = "https://api-open.data.gov.sg/v2/real-time/api"
PSI_URL = f"{API_BASE}/psi"
PM25_URL = f"{API_BASE}/pm25"
FORECAST_URL = f"{API_BASE}/two-hr-forecast"
RAINFALL_URL = f"{API_BASE}/rainfall"
TEMPERATURE_URL = f"{API_BASE}/air-temperature"

SGT = ZoneInfo("Asia/Singapore")
REGIONS = ["north", "south", "east", "west", "central"]
COLORS = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#8C6400"]
STYLES = ["-", "--", "-.", ":", (0, (5, 2, 1, 2))]

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "last_sent_psi.txt"
PM25_STATE_FILE = BASE_DIR / "last_sent_pm25.txt"
REPORT_FILE = BASE_DIR / "psi_reports.json"
RECORDS_FILE = BASE_DIR / "psi_records.json"

STALE_SECONDS = 7200  # Readings older than 2 hours are flagged.
MAX_MESSAGE_LENGTH = 4000  # Telegram's limit is 4096.
# If NEA has not yet published the current hour's PSI / PM2.5, wait up to
# this long (checking every 30 s) before sending the latest available data.
# Set to 0 to never wait. Longer waits use more GitHub Actions minutes.
WAIT_FOR_LATEST_MINUTES = 10
WAIT_INTERVAL_SECONDS = 30
# Send ONE message: the map image with legend plus a short caption.
# MAP_ABOVE_CAPTION = True puts the map on top with the caption underneath;
# False puts the caption on top with the map underneath.
# Telegram caps captions at 1,024 characters. If the image fails, the full text
# message is sent instead. Set to False to always send the full text message.
SEND_MAP_IMAGE = True
MAP_ABOVE_CAPTION = True
CAPTION_LIMIT = 1000  # Telegram's limit is 1,024; keep a small safety margin.

ERRORS = (requests.RequestException, ValueError, KeyError, TypeError,
          AttributeError, OSError, RuntimeError, IndexError)

# Approximate regional reference points (NOT official boundaries).
# Used to group weather stations and forecast areas into the 5 regions.
REGION_CENTRES = {
    "north": (1.418, 103.820),
    "east": (1.357, 103.940),
    "south": (1.270, 103.820),
    "west": (1.357, 103.700),
    "central": (1.350, 103.820),
}


# ==========================================================
# SHARED HELPERS
# ==========================================================
def parse_time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timestamp has no timezone.")
    return result.astimezone(SGT)


def clock(moment):
    """'6:05 PM' style time (no leading zero)."""
    return f"{moment:%I:%M %p}".lstrip("0")


def save_text(path, text):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def is_number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def get_region(latitude, longitude):
    """Nearest regional reference point."""
    best, best_distance = None, float("inf")
    for region, (lat, lon) in REGION_CENTRES.items():
        d_lat = latitude - lat
        d_lon = (longitude - lon) * math.cos(math.radians(latitude))
        distance = d_lat ** 2 + d_lon ** 2
        if distance < best_distance:
            best, best_distance = region, distance
    return best


# ==========================================================
# PSI / PM2.5 (unchanged logic from the original PSI bot)
# ==========================================================
def fetch_items(day=None, url=PSI_URL):
    params = {} if day is None else {"date": day.isoformat()}
    items, seen = [], set()
    for _ in range(20):
        response = requests.get(url, params=params, timeout=20)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise ValueError("PSI API returned an error.")
        data = payload["data"]
        if not isinstance(data["items"], list):
            raise ValueError("Invalid PSI response.")
        items.extend(data["items"])
        token = data.get("paginationToken")
        if not token:
            return items
        if not isinstance(token, str) or token in seen:
            raise ValueError("Invalid pagination token.")
        seen.add(token)
        params["paginationToken"] = token
    raise ValueError("Too many API pages; refusing an incomplete response.")


def read_values(item, field="psi_twenty_four_hourly"):
    raw = item["readings"][field]
    values = {}
    for region in REGIONS:
        value = raw[region]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0 or value != int(value)):
            raise ValueError(f"Invalid PSI for {region}.")
        values[region] = int(value)
    return values


def get_psi_indicator(psi):
    for limit, label in [(50, "🟢 Good"), (100, "🟡 Moderate"),
                         (200, "🟠 Unhealthy"), (300, "🔴 Very Unhealthy")]:
        if psi <= limit:
            return label
    return "🟣 Hazardous"


def get_pm25_indicator(value):
    # NEA's 1-hour PM2.5 bands (ug/m3). These are NOT the PSI bands.
    if value <= 55:
        return "🟢 Normal"
    if value <= 150:
        return "🟡 Elevated"
    if value <= 250:
        return "🟠 High"
    return "🔴 Very High"


def get_health_advisory(psi):
    if psi <= 100:
        return ["• All groups: Continue usual activities."]
    if psi <= 200:
        return [
            "For prolonged or strenuous outdoor exertion:",
            "• Healthy adults: Reduce.",
            "• Older adults, pregnant people and children: Minimise.",
            "• People with chronic heart/lung disease: Avoid.",
        ]
    if psi <= 300:
        return [
            "• Healthy adults: Avoid prolonged/strenuous outdoor exertion.",
            "• Older adults, pregnant people and children: Minimise time outdoors.",
            "• People with chronic heart/lung disease: Avoid outdoor activity.",
        ]
    return [
        "• Healthy adults: Minimise outdoor activity.",
        "• Vulnerable people: Avoid outdoor activity.",
    ]


def get_air_reading(url, field, now):
    items = fetch_items(url=url)
    if not items:
        raise ValueError("No readings available.")
    latest = max(items, key=lambda item: parse_time(item["timestamp"]))
    reading_time = parse_time(latest["timestamp"])
    age = (now - reading_time).total_seconds()
    if age < -300:
        raise ValueError("Timestamp is unexpectedly in the future.")
    return {
        "time": reading_time,
        "values": read_values(latest, field),
        "stale": age > STALE_SECONDS,
        # True if the current clock hour's reading is not published yet.
        "behind": reading_time < now.replace(minute=0, second=0, microsecond=0),
    }


def get_psi(now):
    return get_air_reading(PSI_URL, "psi_twenty_four_hourly", now)


def get_pm25(now):
    return get_air_reading(PM25_URL, "pm25_one_hourly", now)


# ==========================================================
# WEATHER DATA (from the original weather bot)
# ==========================================================
def fetch_weather(url):
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise ValueError("Weather API returned an error.")
    return payload["data"]


def station_lookup(stations):
    lookup = {}
    for station in stations:
        location = station.get("location") or {}
        lat, lon = location.get("latitude"), location.get("longitude")
        if station.get("id") and is_number(lat) and is_number(lon):
            lookup[station["id"]] = (station.get("name", station["id"]),
                                     get_region(lat, lon))
    return lookup


def latest_station_readings(data):
    """Return (timestamp, [(station name, region, value), ...])."""
    readings = data["readings"]
    if not readings:
        raise ValueError("No readings available.")
    latest = max(readings, key=lambda r: parse_time(r["timestamp"]))
    lookup = station_lookup(data["stations"])
    rows = []
    for item in latest["data"]:
        station = lookup.get(item.get("stationId"))
        value = item.get("value")
        if station and is_number(value):
            rows.append((station[0], station[1], float(value)))
    return parse_time(latest["timestamp"]), rows


def get_temperature(now):
    reading_time, rows = latest_station_readings(fetch_weather(TEMPERATURE_URL))
    if not rows:
        raise ValueError("No valid temperature readings.")
    by_region = {r: [] for r in REGIONS}
    for name, region, value in rows:
        by_region[region].append((name, value))
    return {
        "time": reading_time,
        "regions": by_region,
        "overall": sum(v for _, _, v in rows) / len(rows),
    }


def get_rainfall(now):
    reading_time, rows = latest_station_readings(fetch_weather(RAINFALL_URL))
    if not rows:
        raise ValueError("No valid rainfall readings.")
    wet = {r: [] for r in REGIONS}
    for name, region, value in rows:
        if value > 0:
            wet[region].append((name, value))
    for region in REGIONS:
        wet[region].sort(key=lambda item: item[1], reverse=True)
    return {"time": reading_time, "wet": wet}


def get_forecast(now):
    data = fetch_weather(FORECAST_URL)
    if not data["items"]:
        raise ValueError("No forecast available.")
    latest = max(data["items"], key=lambda i: parse_time(i["update_timestamp"]))
    coordinates = {}
    for area in data["area_metadata"]:
        location = area.get("label_location") or {}
        if is_number(location.get("latitude")) and is_number(location.get("longitude")):
            coordinates[area["name"]] = (location["latitude"], location["longitude"])
    regions = {r: {} for r in REGIONS}
    for forecast in latest["forecasts"]:
        area, condition = forecast.get("area"), forecast.get("forecast")
        if not area or not condition or area not in coordinates:
            continue
        regions[get_region(*coordinates[area])].setdefault(condition, []).append(area)
    if not any(regions.values()):
        raise ValueError("No usable forecast areas.")
    return {
        "period": (latest.get("valid_period") or {}).get("text", ""),
        "updated": parse_time(latest["update_timestamp"]),
        "regions": regions,
    }


def get_weather_emoji(condition):
    text = condition.lower()
    night = "night" in text
    for key, emoji in [
        ("thundery", "⛈️"), ("heavy", "🌧️"), ("moderate rain", "🌧️"),
        ("light rain", "🌦️"), ("showers", "🌦️"), ("rain", "🌧️"),
        ("partly cloudy", "⛅"), ("cloudy", "☁️"),
        ("fair", "🌙" if night else "☀️"),
        ("hazy", "🌫️"), ("windy", "💨"),
    ]:
        if key in text:
            return emoji
    return "🌤️"


# ==========================================================
# COMBINED MESSAGE FORMATTING
# ==========================================================
def region_condition_label(conditions):
    return " / ".join(sorted(conditions))


def forecast_groups(forecast):
    """Group regions that share the same forecast: {label: [regions]}."""
    groups = {}
    for region in REGIONS:
        conditions = forecast["regions"][region]
        if conditions:
            groups.setdefault(region_condition_label(conditions), []).append(region)
    return groups


def as_of(reading):
    text = f"as of {clock(reading['time'])}"
    return text + " ⚠️ latest available" if reading["behind"] else text


def split_indicator(indicator):
    """'🟠 Unhealthy' -> ('🟠', 'Unhealthy')"""
    emoji, label = indicator.split(" ", 1)
    return emoji, label


def top_regions(values):
    highest = max(values.values())
    return highest, ", ".join(r.title() for r in REGIONS if values[r] == highest)


# A change smaller than this (vs the previous hour) counts as "steady".
TREND_STEADY = {"psi": 2, "pm25": 5}


def trend_symbol(reading, region, kind):
    """('▲', 6) rising, ('▼', 4) easing, ('→', 0) steady, ('', 0) unknown."""
    previous = reading.get("previous")
    if not previous or region not in previous:
        return "", 0
    delta = reading["values"][region] - previous[region]
    if delta >= TREND_STEADY[kind]:
        return "▲", delta
    if delta <= -TREND_STEADY[kind]:
        return "▼", -delta
    return "→", 0


def trend_text(reading, region, kind):
    symbol, size = trend_symbol(reading, region, kind)
    if not symbol:
        return ""
    return f" {symbol}{size}" if size else f" {symbol}"


def first_top_region(values):
    highest = max(values.values())
    return next(r for r in REGIONS if values[r] == highest)


def headline_psi(data):
    psi = data.get("psi")
    if not psi:
        return "⚠️ PSI unavailable this hour"
    highest, top = top_regions(psi["values"])
    emoji, label = split_indicator(get_psi_indicator(highest))
    trend = trend_text(psi, first_top_region(psi["values"]), "psi")
    text = f"{emoji} Highest PSI: {top} {highest}{trend} ({label})"
    return text + (" ⚠️ old data" if psi["stale"] else "")


def headline_pm25(data):
    pm25 = data.get("pm25")
    if not pm25:
        return "⚠️ PM2.5 unavailable this hour"
    highest, top = top_regions(pm25["values"])
    emoji, label = split_indicator(get_pm25_indicator(highest))
    trend = trend_text(pm25, first_top_region(pm25["values"]), "pm25")
    text = f"{emoji} Highest PM2.5: {top} {highest}{trend} µg/m³ ({label})"
    return text + (" ⚠️ old data" if pm25["stale"] else "")


def headline_weather(data):
    parts = []
    forecast = data.get("forecast")
    if forecast:
        groups = forecast_groups(forecast)
        if len(groups) == 1:
            label = next(iter(groups))
            parts.append(f"{get_weather_emoji(label)} {label}")
        else:
            parts.append("🌤️ Conditions vary by region")
    rain = data.get("rainfall")
    if rain:
        wet_regions = [r.title() for r in REGIONS if rain["wet"][r]]
        parts.append("🌧 Rain: " + ", ".join(wet_regions) if wet_regions
                     else "No rain measured")
    temp = data.get("temp")
    if temp:
        parts.append(f"{temp['overall']:.1f}°C")
    return " · ".join(parts) if parts else "⚠️ Weather unavailable this hour"


def status_table(values, unit_label, indicator):
    lines = [f"{'Region':<8}{unit_label:>6}  Status"]
    for region in REGIONS:
        label = split_indicator(indicator(values[region]))[1]
        lines.append(f"{region.title():<8}{values[region]:>6}  {label}")
    return "<pre>" + escape("\n".join(lines)) + "</pre>"


def psi_advice(psi):
    if psi["stale"]:
        return ""
    highest, top = top_regions(psi["values"])
    advice = get_health_advisory(highest)
    if highest <= 100:
        return "✅ " + escape(advice[0].lstrip("• "))
    return "\n".join([f"<b>⚠️ Advice (highest PSI: {escape(top)})</b>"]
                     + [escape(line) for line in advice])


def pm25_advice(pm25):
    # Wording for "Elevated" matches the original PM2.5 bot. Bands above it
    # are a conservative summary: check against NEA guidance (haze.gov.sg).
    if pm25["stale"]:
        return ""
    highest, top = top_regions(pm25["values"])
    if highest <= 55:
        return ""
    if highest <= 150:
        lines = ["• Most people: Reduce vigorous outdoor activity.",
                 "• Vulnerable people: Avoid vigorous outdoor activity."]
    else:
        lines = ["• Most people: Avoid vigorous outdoor activity.",
                 "• Vulnerable people: Minimise time outdoors."]
    return "\n".join([f"<b>⚠️ PM2.5 advice ({escape(top)})</b>"]
                     + [escape(line) for line in lines])


def psi_section(data):
    psi = data.get("psi")
    title = "📈 <b>24-HOUR PSI</b>"
    if not psi:
        return title + "\nUnavailable this hour."
    parts = [f"{title} · {as_of(psi)}",
             status_table(psi["values"], "PSI", get_psi_indicator)]
    advice = psi_advice(psi)
    if advice:
        parts.append(advice)
    return "\n".join(parts)


def pm25_section(data):
    pm25 = data.get("pm25")
    title = "🌫️ <b>1-HOUR PM2.5</b> (µg/m³)"
    if not pm25:
        return title + "\nUnavailable this hour."
    parts = [f"{title} · {as_of(pm25)}",
             status_table(pm25["values"], "µg/m³", get_pm25_indicator)]
    advice = pm25_advice(pm25)
    if advice:
        parts.append(advice)
    return "\n".join(parts)


RAIN_RANKS = [("thundery", 5), ("heavy", 4), ("moderate", 3),
              ("light rain", 2), ("shower", 2), ("rain", 2)]


def rain_rank(condition):
    """0 = not a rain forecast; higher = wetter."""
    text = condition.lower()
    for key, rank in RAIN_RANKS:
        if key in text:
            return rank
    return 0


def short_condition(condition):
    return re.sub(r"\s*\((day|night)\)", "", condition, flags=re.IGNORECASE)


def weather_section(data, towns=True, note="full", max_towns=None):
    temp, rain, forecast = data.get("temp"), data.get("rainfall"), data.get("forecast")
    title = "🌦 <b>WEATHER</b>"
    if not (temp or rain or forecast):
        return title + "\nUnavailable this hour."

    rows = [f"{'Region':<8}{'°C':<6}{'Rain':<5}Forecast"]
    detail, partial = [], False
    for region in REGIONS:
        stations = temp["regions"][region] if temp else []
        temp_text = (f"{sum(v for _, v in stations) / len(stations):.1f}"
                     if stations else "–")
        rain_text = "–" if not rain else ("Yes" if rain["wet"][region] else "No")
        outlook = "–"
        conditions = forecast["regions"][region] if forecast else {}
        if conditions:
            ordered = sorted(conditions, key=lambda c: (-rain_rank(c), c))
            rainy = [c for c in ordered if rain_rank(c) > 0]
            outlook = short_condition(ordered[0])
            if rainy and len(rainy) < len(ordered):
                outlook += " *"
                partial = True
            for condition in rainy:
                areas = conditions[condition]
                if max_towns and len(areas) > max_towns:
                    areas = areas[:max_towns] + [f"+{len(areas) - max_towns} more"]
                detail.append(f"{region.title()}: {short_condition(condition)} in "
                              + ", ".join(areas))
        rows.append(f"{region.title():<8}{temp_text:<6}{rain_text:<5}{outlook}")

    parts = [title, "<pre>" + escape("\n".join(rows)) + "</pre>"]
    if detail and towns:
        parts.append(escape("\n".join(detail)))
    if partial and towns:
        parts.append("* some towns only")
    period = f" ({forecast['period']})" if forecast and forecast["period"] else ""
    if note == "full":
        parts.append(
            "<i>°C and Rain are measured at weather stations (rain: last 5 min; "
            "local showers can be missed). Forecast is NEA's 2-hour outlook"
            + escape(period) + ", not a guarantee.</i>"
        )
    elif note == "short":
        parts.append("<i>Rain: measured, last 5 min. Forecast: NEA 2-hour outlook"
                     + escape(period) + ".</i>")
    return "\n".join(parts)


def details_block(data):
    lines = ["Details"]
    times = []
    if data.get("psi"):
        times.append(f"PSI {clock(data['psi']['time'])}")
    if data.get("pm25"):
        times.append(f"PM2.5 {clock(data['pm25']['time'])}")
    if data.get("temp"):
        times.append(f"Temp {clock(data['temp']['time'])}")
    if data.get("rainfall"):
        times.append(f"Rain {clock(data['rainfall']['time'])}")
    if data.get("forecast"):
        times.append(f"Forecast {clock(data['forecast']['updated'])}")
    if times:
        lines.append("Readings as of: " + " · ".join(times))

    rain = data.get("rainfall")
    if rain:
        lines.append("")
        wet = [(r, rain["wet"][r]) for r in REGIONS if rain["wet"][r]]
        if wet:
            lines.append("Rainfall (latest 5 min)")
            for region, stations in wet:
                text = ", ".join(f"{n} {mm:.1f} mm" for n, mm in stations)
                lines.append(f"{region.title()}: {text}")
        else:
            lines.append("Rainfall: none measured at any station in the latest 5-min reading.")

    temp = data.get("temp")
    if temp:
        lines += ["", "Temperature by station"]
        for region in REGIONS:
            stations = temp["regions"][region]
            if stations:
                text = ", ".join(f"{n} {v:.1f}°" for n, v in stations)
                lines.append(f"{region.title()}: {text}")

    forecast = data.get("forecast")
    if forecast:
        lines += ["", "Forecast areas"]
        for region in REGIONS:
            for condition, areas in forecast["regions"][region].items():
                lines.append(f"{region.title()} · {condition}: {', '.join(areas)}")
    return "<blockquote expandable>" + escape("\n".join(lines)) + "</blockquote>"


def build_message(data, now, with_details=True):
    hour = now.replace(minute=0, second=0, microsecond=0)
    header = f"🇸🇬 <b>Singapore Update · {hour.day} {hour:%b}, {clock(hour)}</b>"
    parts = [
        header,
        "\n".join(data.get("alerts") or []),
        "\n".join(escape(line) for line in
                  (headline_psi(data), headline_pm25(data), headline_weather(data))),
        psi_section(data),
        pm25_section(data),
        weather_section(data),
    ]
    if with_details:
        parts.append(details_block(data))
    parts.append(escape(
        "Source: NEA / data.gov.sg · Guidance: NEA/MOH · haze.gov.sg\n"
        "Regional grouping of stations is approximate."
    ))
    return "\n\n".join(part for part in parts if part)


# ==========================================================
# TELEGRAM
# ==========================================================
def telegram_request(token, method, **kwargs):
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            timeout=60, **kwargs
        )
        result = response.json()
    except (requests.RequestException, ValueError):
        # Never print request exceptions: they can contain the bot token.
        raise RuntimeError("Telegram connection failed; delivery is unconfirmed.") from None
    if response.status_code != 200 or not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError("Telegram rejected the request. Check bot permissions and secrets.")


def send_telegram_message(token, chat_id, message):
    telegram_request(token, "sendMessage", json={
        "chat_id": chat_id, "text": message, "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    })


def plain_length(html_text):
    """Length Telegram counts for a caption (tags removed, entities decoded)."""
    return len(unescape(re.sub(r"<[^>]*>", "", html_text)))


def build_caption(data, now):
    """Short caption shown above the map; trimmed until it fits Telegram's limit."""
    hour = now.replace(minute=0, second=0, microsecond=0)
    header = f"🇸🇬 <b>Singapore Update · {hour.day} {hour:%b}, {clock(hour)}</b>"
    haze = "\n".join(escape(line) for line in (headline_psi(data), headline_pm25(data)))
    has_trend = any((data.get(k) or {}).get("previous") for k in ("psi", "pm25"))
    where = "above ⬆️" if MAP_ABOVE_CAPTION else "below ⬇️"
    pointer = f"(Every region: see the map {where}" + (
        " · ▲▼ = change vs last hour)" if has_trend else ")")
    haze += f"\n<i>{pointer}</i>"
    weather_line = escape(headline_weather(data))
    alerts = "\n".join(data.get("alerts") or [])
    psi, pm25 = data.get("psi"), data.get("pm25")
    psi_text = psi_advice(psi) if psi else ""
    pm25_text = pm25_advice(pm25) if pm25 else ""

    # Most to least detailed; the first version that fits is used.
    for towns, with_pm25_advice, note in [
        (True, True, "short"), (False, True, "short"),
        (False, True, None), (False, False, None),
    ]:
        parts = [header, alerts, haze, weather_line, psi_text]
        if with_pm25_advice:
            parts.append(pm25_text)
        parts.append(weather_section(data, towns=towns, note=note, max_towns=4))
        caption = "\n\n".join(part for part in parts if part)
        if plain_length(caption) <= CAPTION_LIMIT:
            return caption
    return caption


# ==========================================================
# MAP IMAGE (schematic map + colour legend)
# ==========================================================
# (upper limit, label, range text, fill colour, text colour). None = no upper limit.
PSI_BANDS = [
    (50, "Good", "0–50", "#2E9E5B", "white"),
    (100, "Moderate", "51–100", "#F2C200", "#222222"),
    (200, "Unhealthy", "101–200", "#F28C28", "#222222"),
    (300, "Very Unhealthy", "201–300", "#D64545", "white"),
    (None, "Hazardous", ">300", "#7B3FA0", "white"),
]
PM25_BANDS = [
    (55, "Normal", "0–55", "#2E9E5B", "white"),
    (150, "Elevated", "56–150", "#F2C200", "#222222"),
    (250, "High", "151–250", "#F28C28", "#222222"),
    (None, "Very High", "≥251", "#D64545", "white"),
]

# Rough, simplified outline of Singapore's main island (lon, lat).
# Schematic only: not survey-accurate and not to scale.
SG_OUTLINE = [
    (103.640, 1.292), (103.625, 1.318), (103.648, 1.335), (103.665, 1.350),
    (103.675, 1.385), (103.700, 1.425), (103.720, 1.440), (103.745, 1.455),
    (103.770, 1.450), (103.800, 1.462), (103.840, 1.470), (103.870, 1.450),
    (103.895, 1.425), (103.925, 1.420), (103.960, 1.395), (104.000, 1.395),
    (104.030, 1.370), (104.010, 1.330), (103.980, 1.315), (103.945, 1.305),
    (103.905, 1.300), (103.870, 1.285), (103.850, 1.265), (103.810, 1.262),
    (103.770, 1.272), (103.740, 1.285), (103.700, 1.290),
]
# Where each region's badge sits on the schematic map (lon, lat).
BADGE_POSITIONS = {
    "north": (103.820, 1.415), "central": (103.820, 1.345),
    "south": (103.820, 1.292), "east": (103.950, 1.355),
    "west": (103.690, 1.355),
}


def band_for(value, bands):
    for band in bands:
        if band[0] is None or value <= band[0]:
            return band
    return bands[-1]


def draw_map_panel(ax, title, reading, bands, kind):
    ax.set_facecolor("#F3F7FB")
    ax.set_xlim(103.56, 104.08)
    ax.set_ylim(1.245, 1.485)
    ax.set_aspect("equal", adjustable="datalim")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_title(title, loc="left", fontsize=13, fontweight="bold", color="#1F2937", pad=8)
    ax.add_patch(plt.Polygon(SG_OUTLINE, closed=True, facecolor="#E3E9F0",
                             edgecolor="#9AA7B5", linewidth=1.5))
    if reading is None:
        ax.text(103.82, 1.36, "Unavailable this hour", ha="center", va="center",
                fontsize=13, color="#6B7280")
        return
    for region, (lon, lat) in BADGE_POSITIONS.items():
        value = reading["values"][region]
        _limit, _label, _range, fill, ink = band_for(value, bands)
        ax.add_patch(matplotlib.patches.FancyBboxPatch(
            (lon - 0.047, lat - 0.024), 0.094, 0.048,
            boxstyle="round,pad=0,rounding_size=0.007",
            facecolor=fill, edgecolor="white", linewidth=2, zorder=3))
        ax.text(lon, lat + 0.009, region.upper(), ha="center", va="center",
                fontsize=8.5, fontweight="bold", color=ink, zorder=4)
        arrow = trend_symbol(reading, region, kind)[0]
        ax.text(lon, lat - 0.008, f"{value} {arrow}".strip(), ha="center", va="center",
                fontsize=18, fontweight="bold", color=ink, zorder=4)


def draw_legend_row(ax, y_top, title, bands):
    ax.text(0.0, y_top, title, ha="left", va="top", fontsize=10,
            fontweight="bold", color="#1F2937")
    count, gap = len(bands), 0.012
    width = (1 - gap * (count - 1)) / count
    for index, (_limit, label, rng, fill, ink) in enumerate(bands):
        x = index * (width + gap)
        ax.add_patch(matplotlib.patches.FancyBboxPatch(
            (x, y_top - 0.34), width, 0.22,
            boxstyle="round,pad=0,rounding_size=0.01",
            facecolor=fill, edgecolor="none", transform=ax.transAxes))
        ax.text(x + width / 2, y_top - 0.23, f"{label}\n{rng}", ha="center",
                va="center", fontsize=9, fontweight="bold", color=ink,
                linespacing=1.3, transform=ax.transAxes)


def make_map_image(data):
    psi, pm25 = data.get("psi"), data.get("pm25")
    fig = plt.figure(figsize=(8, 11.0), dpi=130)
    try:
        fig.patch.set_facecolor("white")
        psi_title = "24-hour PSI" + (f" · {as_of(psi)}" if psi else "")
        pm_title = "1-hour PM2.5 (µg/m³)" + (f" · {as_of(pm25)}" if pm25 else "")
        draw_map_panel(fig.add_axes([0.03, 0.6727, 0.94, 0.2909]), psi_title, psi, PSI_BANDS, "psi")
        draw_map_panel(fig.add_axes([0.03, 0.3409, 0.94, 0.2909]), pm_title, pm25, PM25_BANDS, "pm25")

        legend = fig.add_axes([0.03, 0.0546, 0.94, 0.2636])
        legend.axis("off")
        legend.set_xlim(0, 1)
        legend.set_ylim(0, 1)
        legend.text(0.0, 1.0, "LEGEND", ha="left", va="top", fontsize=12.5,
                    fontweight="bold", color="#1F2937")
        legend.text(0.0, 0.925,
                    "What each reading range means: the further from green, "
                    "the worse the air quality",
                    ha="left", va="top", fontsize=9, color="#4B5563")
        legend.plot([0, 1], [0.87, 0.87], color="#D1D5DB", linewidth=1,
                    transform=legend.transAxes)
        draw_legend_row(legend, 0.83, "24-hour PSI", PSI_BANDS)
        draw_legend_row(legend, 0.43, "1-hour PM2.5 (µg/m³)", PM25_BANDS)
        fig.text(0.03, 0.034,
                 "Arrows show the change since the previous hour: ▲ rising · ▼ easing · → steady",
                 fontsize=8.5, color="#6B7280")
        fig.text(0.03, 0.016,
                 "Schematic map, not to scale · Source: NEA / data.gov.sg",
                 fontsize=8.5, color="#6B7280")
        output = io.BytesIO()
        fig.savefig(output, format="png", facecolor=fig.get_facecolor())
        output.seek(0)
        return output
    finally:
        plt.close(fig)


# ==========================================================
# TRENDS, BAND CHANGES AND RECORD ALERTS
# ==========================================================
KINDS = {
    "psi": (PSI_URL, "psi_twenty_four_hourly", PSI_BANDS, "PSI"),
    "pm25": (PM25_URL, "pm25_one_hourly", PM25_BANDS, "PM2.5"),
}
RECORD_WINDOW_DAYS = 30   # "highest in N days" looks back this far.
RECORD_MIN_DAYS = 14      # No record alerts until this many past days are known.
ALERT_MIN = {"psi": 100, "pm25": 55}       # Record alerts only above these values.
ALERT_BAND_INDEX = {"psi": 2, "pm25": 2}   # Band-change alerts: Unhealthy+ / High+.


def band_index(value, bands):
    for index, band in enumerate(bands):
        if band[0] is None or value <= band[0]:
            return index
    return len(bands) - 1


def get_previous_values(url, field, reading_time):
    """Regional values for the hour before `reading_time` (None if not found)."""
    target = (reading_time - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    for item in fetch_items(target.date(), url=url):
        stamp = parse_time(item["timestamp"]).replace(minute=0, second=0, microsecond=0)
        if stamp == target:
            try:
                return read_values(item, field)
            except (ValueError, KeyError, TypeError):
                return None
    return None


def add_previous(data):
    for kind, (url, field, _bands, _name) in KINDS.items():
        reading = data.get(kind)
        if not reading:
            continue
        try:
            reading["previous"] = get_previous_values(url, field, reading["time"])
        except ERRORS as error:
            print(f"Previous-hour {kind} unavailable ({type(error).__name__}).")
            reading["previous"] = None


def load_records():
    if RECORDS_FILE.exists():
        try:
            records = json.loads(RECORDS_FILE.read_text(encoding="utf-8"))
            if isinstance(records, dict) and isinstance(records.get("daily_max"), dict):
                return records
        except ValueError:
            pass
    return {"daily_max": {}}


def day_max(day, url, field):
    """Highest regional reading on `day`: {'value', 'region', 'time'} or None."""
    best = None
    for item in fetch_items(day, url=url):
        try:
            values = read_values(item, field)
            stamp = parse_time(item["timestamp"])
        except (ValueError, KeyError, TypeError):
            continue
        for region in REGIONS:
            if best is None or values[region] > best["value"]:
                best = {"value": values[region], "region": region,
                        "time": stamp.isoformat()}
    return best


def fill_missing_days(records, today):
    """Fetch each past day's highest reading once; later runs reuse the saved file."""
    daily = records["daily_max"]
    failures = 0
    for offset in range(1, RECORD_WINDOW_DAYS + 1):
        day = today - timedelta(days=offset)
        entry = daily.setdefault(day.isoformat(), {})
        for kind, (url, field, _bands, _name) in KINDS.items():
            if kind in entry:
                continue
            if failures >= 3:  # Probably rate-limited; try again next hour.
                return
            try:
                entry[kind] = day_max(day, url, field)  # None = no data that day.
                time_module.sleep(0.25)
            except ERRORS as error:
                failures += 1
                print(f"History for {day} ({kind}) unavailable ({type(error).__name__}).")


def update_today(records, data):
    for kind in KINDS:
        reading = data.get(kind)
        if not reading:
            continue
        values = reading["values"]
        highest = max(values.values())
        entry = records["daily_max"].setdefault(reading["time"].date().isoformat(), {})
        current = entry.get(kind)
        if not current or highest > current["value"]:
            entry[kind] = {"value": highest, "region": first_top_region(values),
                           "time": reading["time"].isoformat()}


def record_alerts(data, records):
    """Alert lines (HTML) for band jumps since last hour and record highs."""
    record_lines, band_lines = [], []
    daily = records["daily_max"]
    for kind, (_url, _field, bands, name) in KINDS.items():
        reading = data.get(kind)
        if not reading or reading["stale"]:
            continue
        values = reading["values"]
        highest, top = top_regions(values)

        previous = reading.get("previous")
        alerted = records.setdefault("band_alerted", {})
        reading_key = reading["time"].isoformat()
        if previous and alerted.get(kind) != reading_key:
            moved = []
            for region in REGIONS:
                new = band_index(values[region], bands)
                if new >= ALERT_BAND_INDEX[kind] and new > band_index(previous[region], bands):
                    moved.append(f"{region.title()} → {bands[new][1]}")
            if moved:
                shown = ", ".join(moved[:3])
                if len(moved) > 3:
                    shown += f" +{len(moved) - 3} more"
                band_lines.append(f"⬆️ {name} moved up a band: {escape(shown)}")
                alerted[kind] = reading_key

        if highest > ALERT_MIN[kind]:
            day = reading["time"].date()
            earlier_today = ((daily.get(day.isoformat()) or {}).get(kind) or {}).get("value", 0)
            window = []
            for day_key, entry in daily.items():
                try:
                    when = date.fromisoformat(day_key)
                except ValueError:
                    continue
                past = (entry or {}).get(kind)
                if past and 0 < (day - when).days <= RECORD_WINDOW_DAYS:
                    window.append((when, past["value"]))
            if (len(window) >= RECORD_MIN_DAYS and highest > earlier_today
                    and highest > max(value for _when, value in window)):
                span = (day - min(when for when, _value in window)).days
                record_lines.append(
                    f"🚨 <b>{name} {highest} ({escape(top)}): highest in {span} days</b>")
    return record_lines + band_lines


def save_records(records, today):
    cutoff = today - timedelta(days=RECORD_WINDOW_DAYS + 5)
    kept = {}
    for key, entry in records["daily_max"].items():
        try:
            if date.fromisoformat(key) >= cutoff:
                kept[key] = entry
        except ValueError:
            continue
    records["daily_max"] = kept
    save_text(RECORDS_FILE, json.dumps(records, indent=1, sort_keys=True) + "\n")


# ==========================================================
# HOURLY UPDATE
# ==========================================================
def send_hourly_update(token, chat_id):
    """Fetch every source independently; one failure never blocks the rest."""
    now = datetime.now(SGT)
    sources = [
        ("psi", "24-hour PSI", get_psi),
        ("pm25", "1-hour PM2.5", get_pm25),
        ("temp", "temperature", get_temperature),
        ("rainfall", "rainfall", get_rainfall),
        ("forecast", "2-hour forecast", get_forecast),
    ]
    data, failed = {}, False
    for key, label, getter in sources:
        try:
            data[key] = getter(now)
        except ERRORS as error:
            print(f"{label} unavailable ({type(error).__name__}).")
            failed = True

    # Give NEA time to publish the current hour's PSI / PM2.5 before sending.
    deadline = time_module.monotonic() + WAIT_FOR_LATEST_MINUTES * 60
    while (any(key in data and data[key]["behind"] for key in ("psi", "pm25"))
           and time_module.monotonic() < deadline):
        print("Current hour's reading not published yet; waiting...")
        time_module.sleep(WAIT_INTERVAL_SECONDS)
        for key, _label, getter in sources[:2]:
            try:
                data[key] = getter(now)
            except ERRORS:
                pass  # Keep the earlier reading and try again.

    if not data:
        print("Every data source failed; nothing to send.")
        return True

    # Extra context (trend arrows, alerts). Never blocks the update itself.
    records = load_records()
    try:
        add_previous(data)
        fill_missing_days(records, now.date())
        data["alerts"] = record_alerts(data, records)
        update_today(records, data)
    except ERRORS as error:
        print(f"Trend/record context unavailable ({type(error).__name__}).")

    sent = False
    if SEND_MAP_IMAGE and ("psi" in data or "pm25" in data):
        try:
            picture = make_map_image(data)
            caption = build_caption(data, now)
            with picture:
                telegram_request(
                    token, "sendPhoto",
                    data={"chat_id": chat_id, "caption": caption,
                          "parse_mode": "HTML",
                          # Telegram's default is caption below the photo.
                          "show_caption_above_media":
                              "false" if MAP_ABOVE_CAPTION else "true"},
                    files={"photo": ("sg_air_quality_map.png", picture, "image/png")},
                )
            sent = True
            print(f"Hourly update sent as map + caption ({plain_length(caption)} characters).")
        except Exception as error:  # Fall back so readers never miss the figures.
            print(f"Map message failed ({type(error).__name__}); sending full text instead.")
            failed = True

    if not sent:
        message = build_message(data, now)
        if len(message) > MAX_MESSAGE_LENGTH:
            message = build_message(data, now, with_details=False)
        send_telegram_message(token, chat_id, message)
        print(f"Hourly update sent as text ({len(message)} characters).")

    # Record what was sent, after Telegram confirms delivery.
    for key, path in (("psi", STATE_FILE), ("pm25", PM25_STATE_FILE)):
        if key in data:
            save_text(path, data[key]["time"].isoformat() + "\n")
    try:
        save_records(records, now.date())
    except ERRORS as error:
        print(f"Could not save records ({type(error).__name__}).")
    return failed


# ==========================================================
# MIDNIGHT DAILY / WEEKLY PSI CHART REPORTS (unchanged)
# ==========================================================
def period_rows(start, end, cache):
    # end is exclusive. Fetch history rather than relying on prior bot runs.
    rows = {}
    day = start.date()
    while day < end.date():
        if day not in cache:
            cache[day] = fetch_items(day)
        for item in cache[day]:
            timestamp = parse_time(item["timestamp"])
            if not start <= timestamp < end:
                continue
            try:
                values = read_values(item)
            except (ValueError, KeyError, TypeError):
                continue  # A missing/invalid hour becomes a gap, never zero.
            # Use the newest revision if the API repeats a reading timestamp.
            updated = parse_time(item.get("updatedTimestamp") or item["timestamp"])
            if timestamp not in rows or updated > rows[timestamp][0]:
                rows[timestamp] = (updated, values)
        day += timedelta(days=1)
    # Select the latest timestamp in each hour; normally PSI is hourly.
    hourly = {}
    for timestamp in sorted(rows):
        hourly[timestamp.replace(minute=0, second=0, microsecond=0)] = rows[timestamp][1]
    return hourly


def make_chart(kind, start, end, hourly):
    hours = int((end - start).total_seconds() // 3600)
    times = [start + timedelta(hours=i) for i in range(hours)]
    period = (f"{start:%d %b %Y}" if kind == "daily" else
              f"{start:%d %b %Y} - {end - timedelta(days=1):%d %b %Y}")
    fig, ax = plt.subplots(figsize=(12, 6.8), dpi=160)
    try:
        fig.patch.set_facecolor("#F7F9FC")
        ax.set_facecolor("white")
        for region, color, style in zip(REGIONS, COLORS, STYLES):
            values = [hourly.get(t, {}).get(region, float("nan")) for t in times]
            ax.plot(times, values, label=region.title(), color=color,
                    linestyle=style, linewidth=2, marker="o",
                    markersize=3 if kind == "daily" else 1.6, alpha=0.85)
        ax.set_title(f"Singapore PSI | {kind.title()} Report\n{period}",
                     fontsize=18, fontweight="bold", pad=20)
        ax.set_ylabel("24-hour PSI", fontsize=12)
        ax.set_xlabel("Time (Singapore time)" if kind == "daily" else "Date (Singapore time)")
        ax.set_xlim(start, end - timedelta(minutes=1))
        ax.set_ylim(bottom=0)
        if kind == "daily":
            ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3), tz=SGT))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=SGT))
        else:
            ax.xaxis.set_major_locator(mdates.DayLocator(tz=SGT))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%a\n%d %b", tz=SGT))
        ax.grid(True, alpha=0.2)
        ax.spines[["top", "right"]].set_visible(False)
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.17), ncol=5, frameon=False)
        fig.text(0.06, 0.035,
                 f"Source: NEA / data.gov.sg | Hours available: {len(hourly)}/{hours}\n"
                 "Hourly snapshots of 24-hour PSI. Missing hours are gaps; equal readings may overlap.",
                 fontsize=9, color="#475569")
        fig.subplots_adjust(left=0.08, right=0.98, top=0.83, bottom=0.28)
        output = io.BytesIO()
        fig.savefig(output, format="png", facecolor=fig.get_facecolor())
        output.seek(0)
        return output, period, hours
    finally:
        plt.close(fig)


def get_report_caption(kind, period, hours, hourly):
    # Use the same readings as the chart, across all five regions.
    entries = [
        (values[region], timestamp, region)
        for timestamp, values in sorted(hourly.items())
        for region in REGIONS
    ]
    if not entries:
        raise ValueError("No readings available for the summary.")
    lowest = min(entries, key=lambda entry: entry[0])
    highest = max(entries, key=lambda entry: entry[0])
    categories = list(dict.fromkeys(
        get_psi_indicator(value)
        for value in sorted({entry[0] for entry in entries})
    ))

    def describe(entry):
        value, timestamp, region = entry
        when = (timestamp.strftime("%I:%M %p") if kind == "daily"
                else timestamp.strftime("%a %d %b, %I:%M %p"))
        return f"{value} — {region.title()}, {when}"

    unhealthy_hours = sum(
        any(values[region] > 100 for region in REGIONS)
        for values in hourly.values()
    )
    lines = [
        f"📊 Singapore PSI — {kind.title()} Summary",
        period,
        "",
        "Recorded categories: " + "; ".join(categories),
        f"⬇️ Lowest: {describe(lowest)}",
        f"⬆️ Highest: {describe(highest)}",
    ]
    if (sum(entry[0] == lowest[0] for entry in entries) > 1
            or sum(entry[0] == highest[0] for entry in entries) > 1):
        lines.append("Ties: earliest hour, then first region shown.")
    lines.append("")
    if unhealthy_hours:
        lines.append(
            f"⚠️ At least one region recorded Unhealthy PSI or higher "
            f"in {unhealthy_hours} of {len(hourly)} available hourly snapshots."
        )
    else:
        lines.append("✅ No Unhealthy-or-higher PSI readings in the available data.")
    lines += ["", f"Hours available: {len(hourly)}/{hours}."]
    if len(hourly) < hours:
        lines.append("⚠️ Incomplete data: summary covers available readings only; chart gaps are missing hours.")
    lines += [
        "Based on hourly snapshots of 24-hour PSI, not instantaneous conditions.",
        "All times Singapore time (SGT).",
        "Source: NEA / data.gov.sg",
    ]
    return "\n".join(lines)


def check_reports(token, chat_id, now=None):
    now = now or datetime.now(SGT)
    today = datetime.combine(now.date(), time.min, tzinfo=SGT)
    monday = today - timedelta(days=today.weekday())
    state = json.loads(REPORT_FILE.read_text(encoding="utf-8")) if REPORT_FILE.exists() else {}
    if not isinstance(state, dict):
        raise ValueError("Invalid report state.")
    cache, failed = {}, False
    for kind, end, days in [("daily", today, 1), ("weekly", monday, 7)]:
        start = end - timedelta(days=days)
        key = (end - timedelta(days=1)).date().isoformat()
        try:
            previous = state.get(kind)
            if previous is not None and date.fromisoformat(previous) >= date.fromisoformat(key):
                continue
            hourly = period_rows(start, end, cache)
            expected = days * 24
            if not hourly:
                raise ValueError("No historical readings available.")
            # Allow delayed readings to appear during the first hour after midnight.
            if len(hourly) < expected and now < end + timedelta(hours=1):
                print(f"{kind.title()} chart waiting for missing readings; retry next run.")
                continue
            picture, period, hours = make_chart(kind, start, end, hourly)
            caption = get_report_caption(kind, period, hours, hourly)
            with picture:
                telegram_request(token, "sendPhoto", data={"chat_id": chat_id, "caption": caption},
                                 files={"photo": (f"psi_{kind}_{key}.png", picture, "image/png")})
            state[kind] = key
            save_text(REPORT_FILE, json.dumps(state, indent=2) + "\n")
            print(f"{kind.title()} chart sent for period ending {key}.")
        except ERRORS as error:
            print(f"{kind.title()} report failed ({type(error).__name__}); retry next run.")
            failed = True
    return failed


# ==========================================================
# MAIN
# ==========================================================
def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret.")
        return 1

    failed = False
    try:
        failed = send_hourly_update(token, chat_id)
    except ERRORS as error:
        print(f"Hourly update failed ({type(error).__name__}).")
        failed = True

    # Reports must still run even if the hourly update had a problem.
    try:
        failed = check_reports(token, chat_id) or failed
    except ERRORS as error:
        print(f"Report check failed ({type(error).__name__}). Check psi_reports.json and API access.")
        failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
