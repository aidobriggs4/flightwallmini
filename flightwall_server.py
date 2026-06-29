#!/usr/bin/env python3
"""
FlightWall Mini - companion server + web dashboard.

Run on your computer / Raspberry Pi:   python3 flightwall_server.py
Open the dashboard (URL printed on startup) to view live aircraft and change
all settings in a browser. The ESP32 reads ready-made data from /flights.

Pick your DATA SOURCE in the dashboard:
  - Flightradar24 : positions + routes + type + airline in one paid call.
  - OpenSky       : free positions; routes/cities from adsbdb (also free).
  - FlightAware   : AeroAPI search; positions + routes + type (paid).

Airline logos are keyless (Google's logo CDN). Pillow only needed for logos.
"""

import os, json, time, math, threading, socket, base64, io, datetime
import urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.environ.get("FLIGHTWALL_SETTINGS", os.path.join(HERE, "settings.json"))
FR24_BASE = "https://fr24api.flightradar24.com/api"
AEROAPI_BASE = "https://aeroapi.flightaware.com/aeroapi"

DEFAULTS = {
    "center_lat": 33.6000,
    "center_lon": -117.6720,
    "radius_km": 50,
    "max_aircraft": 12,
    "refresh_sec": 60,
    "port": 8080,
    "data_source": "fr24",             # fr24 | opensky | flightaware
    "fr24_token": "",
    "opensky_client_id": "",
    "opensky_client_secret": "",
    "flightaware_api_key": "",
    "place_style": "city",             # code | city
    "airline_only": False,
    "text_color": "#ff8c00",
    "brightness": 90,
    "show_border": False,
    "show_logos": True,
    "logo_px": 20,
    "mode": "nearby",                  # nearby | track
    "track_flight": "",
    "show_clock": True,                # show the top-left clock on the panel
    "clock24h": False,                # 24-hour vs 12-hour clock
    "rainbow": False,                  # cycle the text through rainbow colors
    "night_mode": False,               # auto-dim during night hours
    "night_start": "22:00",            # HH:MM local
    "night_end": "07:00",              # HH:MM local
    "night_brightness": 15,            # brightness during night (0-255)
    "night_to_clock": False,           # switch to clock-only during night window
    "clock_date": True,                # show date on the clock-only screen
    "date_format": "month_day_year",   # see DATE_FORMATS below
    "auto_fallback": True,             # if the chosen source fails, try OpenSky
    "highlight_special": True,         # flag emergency-squawk / military flights
    "fav_airlines": "",               # comma IATA/ICAO codes; if set, show only these
    "fav_types": "",                  # comma aircraft types; if set, show only these
    "show_weather": False,            # show local weather on the clock screen
    "world_zones": "America/Los_Angeles,America/New_York,Europe/London,Asia/Tokyo",
    "rotate_screens": "nearby,world,clock,weather",
    "rotate_sec": 10,
    "ical_url": "",                   # Google Calendar secret iCal URL
    "cycle_sec": 8,                   # seconds each plane is shown (display only, not API)
}

_settings = dict(DEFAULTS)
_settings_lock = threading.Lock()
_data_lock = threading.Lock()
_refresh_now = threading.Event()

_aircraft = []
_last_update = 0
_last_error = ""
_device_ip = ""
_device_last = 0      # when the ESP32 last fetched /flights
_version = 1          # bumps on every settings change or data refresh (for instant ESP polling)
_active_source = ""   # the source actually used last (may differ if fallback kicked in)
_weather = {"txt": "", "exp": 0}
_picture = ""         # base64 RGB565 128x64 image for picture mode
_picture_ver = 0
_rotate_last = ""
PICTURE_FILE = os.environ.get("FLIGHTWALL_PICTURE", os.path.join(HERE, "picture.b64"))
_logo_cache = {}
_airport_cache = {}
_route_cache = {}
_os_token = {"val": None, "exp": 0}

# Credential fields: masked in GET, and never overwritten by a blank on POST.
CRED_KEYS = {"fr24_token", "opensky_client_id", "opensky_client_secret", "flightaware_api_key"}


# ---------------- settings ----------------
def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                saved = json.load(f)
            with _settings_lock:
                for k in DEFAULTS:
                    if k in saved:
                        _settings[k] = saved[k]
        except Exception as e:
            print("could not read settings.json:", e)


def save_settings():
    with _settings_lock:
        snap = dict(_settings)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(snap, f, indent=2)


def get(k):
    with _settings_lock:
        return _settings[k]


# ---------------- helpers ----------------
def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def airline_code(s):
    code = ""
    for ch in (s or ""):
        if ch.isalpha():
            code += ch
        else:
            break
    return code.upper()


def is_airline_callsign(cs):
    cs = (cs or "").strip()
    pre = airline_code(cs)
    rest = cs[len(pre):]
    return len(pre) == 3 and any(c.isdigit() for c in rest)


def bbox():
    lat, lon, rad = get("center_lat"), get("center_lon"), get("radius_km")
    dlat = rad / 111.0
    dlon = rad / (111.0 * max(0.1, math.cos(math.radians(lat))))
    return (lat + dlat, lat - dlat, lon - dlon, lon + dlon)   # N, S, W, E


def eta_minutes(ts):
    if not ts:
        return -1
    try:
        t = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return max(0, round((t.timestamp() - time.time()) / 60))
    except Exception:
        return -1


# ---------------- logos (keyless via gstatic) ----------------
def _logo_candidates(iata):
    c = []
    if iata and len(iata) == 2:
        c.append(f"https://www.gstatic.com/flights/airline_logos/70px/dark/{iata}.png")
        c.append(f"https://www.gstatic.com/flights/airline_logos/70px/{iata}.png")
    return c


def fetch_logo(iata):
    if not (get("show_logos") and HAVE_PIL) or not iata:
        return ""
    px = int(get("logo_px"))
    ck = (iata, px)
    if ck in _logo_cache:
        return _logo_cache[ck]
    b64 = ""
    for url in _logo_candidates(iata):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 FlightWallMini"})
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
            if not raw or len(raw) < 100:
                continue
            img = Image.open(io.BytesIO(raw)).convert("RGBA").resize((px, px))
            out = bytearray()
            for y in range(px):
                for x in range(px):
                    r_, g_, b_, a_ = img.getpixel((x, y))
                    if a_ < 40:
                        r_ = g_ = b_ = 0
                    v = ((r_ >> 3) << 11) | ((g_ >> 2) << 5) | (b_ >> 3)
                    out.append((v >> 8) & 0xFF)
                    out.append(v & 0xFF)
            b64 = base64.b64encode(bytes(out)).decode()
            break
        except Exception:
            continue
    _logo_cache[ck] = b64
    return b64


def logo_diag(code):
    if not HAVE_PIL:
        return {"ok": False, "error": "Pillow not installed - run: pip install Pillow"}
    code = (code or "").strip().upper()
    if len(code) != 2:
        return {"ok": False, "error": "use a 2-letter airline IATA code, e.g. UA"}
    last = ""
    for url in _logo_candidates(code):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 FlightWallMini"})
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
            img = Image.open(io.BytesIO(raw))
            return {"ok": True, "code": code, "bytes": len(raw),
                    "size": list(img.size), "format": img.format}
        except Exception as e:
            last = str(e)
    return {"ok": False, "code": code, "error": last}


# ============================================================
#  DATA SOURCE: Flightradar24
# ============================================================
def fr24_headers():
    return {"Accept": "application/json", "Accept-Version": "v1",
            "Authorization": "Bearer " + get("fr24_token").strip()}


def fr24_get(url):
    req = urllib.request.Request(url, headers=fr24_headers())
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} from FR24: {body or e.reason}")


def airport_info(code):
    """(city, lat, lon) for an airport code, cached (uses FR24 if a token is set)."""
    code = (code or "").strip().upper()
    if not code:
        return ("", None, None)
    if code in _airport_cache:
        return _airport_cache[code]
    info = (code, None, None)
    if get("fr24_token").strip():
        try:
            j = fr24_get(f"{FR24_BASE}/static/airports/{urllib.parse.quote(code)}/full")
            info = (j.get("city") or j.get("name") or code, j.get("lat"), j.get("lon"))
        except Exception:
            info = (code, None, None)
    _airport_cache[code] = info
    return info


def fr24_nearby():
    if not get("fr24_token").strip():
        raise RuntimeError("No Flightradar24 API token set in the dashboard")
    n, s, w, e = bbox()
    bounds = f"{n:.4f},{s:.4f},{w:.4f},{e:.4f}"
    try:
        j = fr24_get(f"{FR24_BASE}/live/flight-positions/full?bounds={bounds}")
    except RuntimeError as ex:
        if "HTTP 403" in str(ex):
            j = fr24_get(f"{FR24_BASE}/live/flight-positions/light?bounds={bounds}")
        else:
            raise
    recs = []
    for f in (j.get("data") or []):
        flightno = (f.get("flight") or "").strip()
        recs.append({
            "cs": (f.get("callsign") or flightno or "").strip(),
            "alt": int(f.get("alt") or 0),
            "spd": round((f.get("gspeed") or 0) * 1.15078),
            "trk": int(f.get("track") or 0),
            "vr": int(f.get("vspeed") or 0),
            "lat": f.get("lat"), "lon": f.get("lon"),
            "oc": (f.get("orig_iata") or "").strip(), "ocity": "",
            "dc": (f.get("dest_iata") or "").strip(), "dcity": "",
            "type": (f.get("type") or "").strip(),
            "aiata": airline_code(flightno)[:2] if flightno else "",
        })
    return recs


def fr24_tracked():
    flt = get("track_flight").strip().upper()
    if not flt:
        raise RuntimeError("No flight set to track")
    j = fr24_get(f"{FR24_BASE}/live/flight-positions/full?callsigns={urllib.parse.quote(flt)}")
    items = j.get("data") or []
    if not items:
        j = fr24_get(f"{FR24_BASE}/live/flight-positions/full?flights={urllib.parse.quote(flt)}")
        items = j.get("data") or []
    if not items:
        return []
    f = items[0]
    oc = (f.get("orig_iata") or "").strip()
    dc = (f.get("dest_iata") or "").strip()
    flightno = (f.get("flight") or "").strip()
    flat, flon = f.get("lat"), f.get("lon")
    ocity, olat, olon = airport_info(oc)
    dcity, dlat, dlon = airport_info(dc)
    progress = 0
    if None not in (flat, flon, olat, olon, dlat, dlon):
        total = haversine(olat, olon, dlat, dlon)
        if total > 0:
            progress = max(0, min(100, round(haversine(olat, olon, flat, flon) / total * 100)))
    rec = {
        "cs": (f.get("callsign") or flightno or flt).strip(),
        "alt": int(f.get("alt") or 0),
        "spd": round((f.get("gspeed") or 0) * 1.15078),
        "trk": int(f.get("track") or 0), "vr": int(f.get("vspeed") or 0),
        "lat": flat, "lon": flon,
        "oc": oc, "ocity": ocity, "dc": dc, "dcity": dcity,
        "type": (f.get("type") or "").strip(),
        "aiata": airline_code(flightno)[:2] if flightno else "",
        "progress": progress, "eta": eta_minutes(f.get("eta")),
    }
    return [rec]


# ============================================================
#  DATA SOURCE: OpenSky (+ adsbdb routes)
# ============================================================
def opensky_token():
    cid = get("opensky_client_id").strip()
    if not cid:
        return None
    if _os_token["val"] and time.time() < _os_token["exp"] - 30:
        return _os_token["val"]
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": cid,
        "client_secret": get("opensky_client_secret").strip()}).encode()
    url = ("https://auth.opensky-network.org/auth/realms/"
           "opensky-network/protocol/openid-connect/token")
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
        j = json.load(r)
    _os_token["val"] = j["access_token"]
    _os_token["exp"] = time.time() + j.get("expires_in", 1800)
    return _os_token["val"]


def opensky_states(extra=""):
    headers = {"User-Agent": "FlightWallMini"}
    tok = opensky_token()
    if tok:
        headers["Authorization"] = "Bearer " + tok
    url = "https://opensky-network.org/api/states/all" + extra
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from OpenSky: {e.reason}")


def adsbdb_route(cs):
    cs = (cs or "").strip()
    if len(cs) < 3:
        return {}
    now = time.time()
    cached = _route_cache.get(cs)
    if cached and now < cached[0]:
        return cached[1]
    # Only airline-style callsigns (3 letters + digits) have scheduled routes;
    # looking up GA/private callsigns returns unreliable or wrong data.
    if not is_airline_callsign(cs):
        _route_cache[cs] = (now + 3600, {})
        return {}
    r = {}
    try:
        url = "https://api.adsbdb.com/v0/callsign/" + urllib.parse.quote(cs)
        with urllib.request.urlopen(url, timeout=15) as resp:
            j = json.load(resp)
        fr = ((j.get("response") or {}).get("flightroute")) or {}
        if fr:
            o = fr.get("origin") or {}
            d = fr.get("destination") or {}
            r = {
                "oc": o.get("iata_code") or "", "ocity": o.get("municipality") or "",
                "olat": o.get("latitude"), "olon": o.get("longitude"),
                "dc": d.get("iata_code") or "", "dcity": d.get("municipality") or "",
                "dlat": d.get("latitude"), "dlon": d.get("longitude"),
                "aiata": (fr.get("airline") or {}).get("iata") or "",
            }
    except Exception:
        r = {}
    # cache good routes for 2h, empty/failed lookups for 10min (so they retry)
    _route_cache[cs] = (now + (7200 if r else 600), r)
    return r


def opensky_nearby():
    n, s, w, e = bbox()
    j = opensky_states(f"?lamin={s}&lamax={n}&lomin={w}&lomax={e}")
    recs = []
    for st in (j.get("states") or []):
        lon, lat = st[5], st[6]
        if lat is None or lon is None or st[8]:
            continue
        cs = (st[1] or "").strip()
        rt = adsbdb_route(cs)
        recs.append({
            "cs": cs or st[0],
            "alt": round((st[7] or st[13] or 0) * 3.28084),
            "spd": round((st[9] or 0) * 2.23694),
            "trk": int(st[10] or 0), "vr": round(st[11] or 0),
            "lat": lat, "lon": lon,
            "oc": rt.get("oc", ""), "ocity": rt.get("ocity", ""),
            "dc": rt.get("dc", ""), "dcity": rt.get("dcity", ""),
            "type": "", "aiata": rt.get("aiata", ""),
            "squawk": (st[14] if len(st) > 14 else ""),
        })
    return recs


def opensky_tracked():
    flt = get("track_flight").strip().upper()
    if not flt:
        raise RuntimeError("No flight set to track")
    j = opensky_states()
    matches = [st for st in (j.get("states") or [])
               if (st[1] or "").strip().upper() == flt]
    if not matches:
        return []
    # Prefer an airborne entry with a valid position over grounded/stale duplicates.
    good = [st for st in matches
            if not st[8] and st[5] is not None and st[6] is not None]
    match = (good or matches)[0]
    cs = (match[1] or "").strip()
    flat, flon = match[6], match[5]
    rt = adsbdb_route(cs)
    progress = 0
    olat, olon = rt.get("olat"), rt.get("olon")
    dlat, dlon = rt.get("dlat"), rt.get("dlon")
    if None not in (flat, flon, olat, olon, dlat, dlon):
        total = haversine(olat, olon, dlat, dlon)
        if total > 0:
            progress = max(0, min(100, round(haversine(olat, olon, flat, flon) / total * 100)))
    return [{
        "cs": cs, "alt": round((match[7] or 0) * 3.28084),
        "spd": round((match[9] or 0) * 2.23694),
        "trk": int(match[10] or 0), "vr": round(match[11] or 0),
        "lat": flat, "lon": flon,
        "oc": rt.get("oc", ""), "ocity": rt.get("ocity", ""),
        "dc": rt.get("dc", ""), "dcity": rt.get("dcity", ""),
        "type": "", "aiata": rt.get("aiata", ""),
        "squawk": (match[14] if len(match) > 14 else ""),
        "progress": progress, "eta": -1,
    }]


# ============================================================
#  DATA SOURCE: FlightAware AeroAPI
# ============================================================
def aero_get(url):
    req = urllib.request.Request(url, headers={"x-apikey": get("flightaware_api_key").strip()})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} from FlightAware: {body or e.reason}")


def aero_nearby():
    if not get("flightaware_api_key").strip():
        raise RuntimeError("No FlightAware API key set in the dashboard")
    n, s, w, e = bbox()
    query = f'-latlong "{n:.4f} {w:.4f} {s:.4f} {e:.4f}"'   # top left bottom right
    url = f"{AEROAPI_BASE}/flights/search?query={urllib.parse.quote(query)}&max_pages=1"
    j = aero_get(url)
    recs = []
    for f in (j.get("flights") or []):
        lp = f.get("last_position") or {}
        o = f.get("origin") or {}
        d = f.get("destination") or {}
        ident_iata = (f.get("ident_iata") or "").strip()
        recs.append({
            "cs": (f.get("ident") or "").strip(),
            "alt": int((lp.get("altitude") or 0) * 100),       # hundreds of ft -> ft
            "spd": round((lp.get("groundspeed") or 0) * 1.15078),
            "trk": int(lp.get("heading") or 0), "vr": 0,
            "lat": lp.get("latitude"), "lon": lp.get("longitude"),
            "oc": (o.get("code_iata") or "").strip(), "ocity": (o.get("city") or "").strip(),
            "dc": (d.get("code_iata") or "").strip(), "dcity": (d.get("city") or "").strip(),
            "type": (f.get("aircraft_type") or "").strip(),
            "aiata": airline_code(ident_iata)[:2] if ident_iata else "",
        })
    return recs


def aero_tracked():
    flt = get("track_flight").strip().upper()
    if not flt:
        raise RuntimeError("No flight set to track")
    j = aero_get(f"{AEROAPI_BASE}/flights/{urllib.parse.quote(flt)}?max_pages=1")
    flights = j.get("flights") or []
    chosen = next((f for f in flights if not f.get("actual_on")), None) or (flights[0] if flights else None)
    if not chosen:
        return []
    o = chosen.get("origin") or {}
    d = chosen.get("destination") or {}
    lp = chosen.get("last_position") or {}
    ident_iata = (chosen.get("ident_iata") or "").strip()
    progress = int(chosen.get("progress_percent") or 0)
    return [{
        "cs": (chosen.get("ident") or flt).strip(),
        "alt": int((lp.get("altitude") or 0) * 100),
        "spd": round((lp.get("groundspeed") or 0) * 1.15078),
        "trk": int(lp.get("heading") or 0), "vr": 0,
        "lat": lp.get("latitude"), "lon": lp.get("longitude"),
        "oc": (o.get("code_iata") or "").strip(), "ocity": (o.get("city") or "").strip(),
        "dc": (d.get("code_iata") or "").strip(), "dcity": (d.get("city") or "").strip(),
        "type": (chosen.get("aircraft_type") or "").strip(),
        "aiata": airline_code(ident_iata)[:2] if ident_iata else "",
        "progress": progress,
        "eta": eta_minutes(chosen.get("estimated_in") or chosen.get("estimated_on")),
    }]


# ============================================================
#  Dispatch + finalize
# ============================================================
NEARBY = {"fr24": fr24_nearby, "opensky": opensky_nearby, "flightaware": aero_nearby}
TRACKED = {"fr24": fr24_tracked, "opensky": opensky_tracked, "flightaware": aero_tracked}


def place(rec, which):
    code = rec.get(which + "c", "")
    city = rec.get(which + "city", "")
    if get("place_style") == "city":
        if city:
            return city
        if code and get("data_source") == "fr24":
            return airport_info(code)[0]
        return code
    return code


MIL_PREFIXES = {"RCH", "RRR", "CFC", "CNV", "IAM", "BAF", "ASY", "GAF", "NATO", "FORTE", "HOMER"}


def _csv_set(key):
    return {x.strip().upper() for x in get(key).split(",") if x.strip()}


def finalize(recs, track=False):
    clat, clon = get("center_lat"), get("center_lon")
    favA, favT = _csv_set("fav_airlines"), _csv_set("fav_types")
    hl_on = get("highlight_special")
    out = []
    for r in recs:
        cs = r.get("cs", "")
        if get("airline_only") and not is_airline_callsign(cs):
            continue
        aiata = (r.get("aiata") or "").upper()
        apre = airline_code(cs)
        if favA and not (aiata in favA or apre in favA):
            continue
        typ = (r.get("type") or "").upper()
        if favT and typ and typ not in favT:
            continue
        flat, flon = r.get("lat"), r.get("lon")
        item = {
            "cs": cs, "alt": r.get("alt", 0), "spd": r.get("spd", 0),
            "trk": r.get("trk", 0), "vr": r.get("vr", 0),
            "dist": round(haversine(clat, clon, flat, flon)) if flat is not None else 0,
            "from": place(r, "o"), "to": place(r, "d"),
            "type": r.get("type", ""), "logo": fetch_logo(r.get("aiata", "")),
        }
        if hl_on:
            sq = str(r.get("squawk") or "")
            if sq in ("7500", "7600", "7700"):
                item["hl"] = "EMERGENCY"
            elif apre in MIL_PREFIXES:
                item["hl"] = "MILITARY"
        if track:
            item["progress"] = r.get("progress", 0)
            item["eta"] = r.get("eta", -1)
            p = item["progress"]
            item["status"] = ("ARRIVING" if p >= 99 or (0 <= item["eta"] <= 2)
                              else "DEPARTING" if p <= 1 else "EN ROUTE")
        out.append(item)
    if not track:
        out.sort(key=lambda a: a["dist"])
        out = out[:get("max_aircraft")]
    return out


WEATHER_CODES = {0: "Clear", 1: "Clear", 2: "Cloudy", 3: "Overcast", 45: "Fog", 48: "Fog",
                 51: "Drizzle", 53: "Drizzle", 55: "Drizzle", 61: "Rain", 63: "Rain",
                 65: "Heavy Rain", 71: "Snow", 73: "Snow", 75: "Snow", 80: "Showers",
                 81: "Showers", 82: "Showers", 95: "Storm", 96: "Storm", 99: "Storm"}


_forecast = {"data": {}, "exp": 0}


def get_weather():
    get_forecast()
    return _weather["txt"]


def get_forecast():
    """Current temp + today's high/low + condition. Cached 15 min."""
    if time.time() < _forecast["exp"]:
        return _forecast["data"]
    data = {}
    try:
        lat, lon = get("center_lat"), get("center_lon")
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
               "&current=temperature_2m,weather_code"
               "&daily=temperature_2m_max,temperature_2m_min"
               "&temperature_unit=fahrenheit&timezone=auto&forecast_days=1")
        with urllib.request.urlopen(url, timeout=15) as r:
            j = json.load(r)
        cur = j.get("current") or {}
        daily = j.get("daily") or {}
        t = round(cur.get("temperature_2m", 0))
        desc = WEATHER_CODES.get(cur.get("weather_code"), "")
        hi = round((daily.get("temperature_2m_max") or [0])[0])
        lo = round((daily.get("temperature_2m_min") or [0])[0])
        data = {"temp": t, "hi": hi, "lo": lo, "desc": desc}
        _weather["txt"] = f"{t}F {desc}".strip()
    except Exception:
        data = {}
        _weather["txt"] = ""
    _forecast["data"] = data
    _forecast["exp"] = time.time() + 900
    return data


_calendar = {"events": [], "exp": 0}


def get_calendar():
    """Parse the next few events from a Google Calendar secret iCal URL. Cached 5 min."""
    url = get("ical_url").strip()
    if not url:
        return []
    if time.time() < _calendar["exp"]:
        return _calendar["events"]
    events = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FlightWallMini"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", "replace")
        now = datetime.datetime.now()
        for block in raw.split("BEGIN:VEVENT")[1:]:
            summary, dt = "", None
            for line in block.splitlines():
                if line.startswith("SUMMARY"):
                    summary = line.split(":", 1)[-1].strip()
                elif line.startswith("DTSTART"):
                    val = line.split(":", 1)[-1].strip()
                    try:
                        if "T" in val:
                            dt = datetime.datetime.strptime(val[:15], "%Y%m%dT%H%M%S")
                        else:
                            dt = datetime.datetime.strptime(val[:8], "%Y%m%d")
                    except Exception:
                        dt = None
            if summary and dt and dt >= now - datetime.timedelta(hours=1):
                events.append((dt, summary))
        events.sort(key=lambda e: e[0])
        out = []
        for dt, summary in events[:3]:
            tstr = dt.strftime("%-I:%M%p").lower() if dt.hour or dt.minute else dt.strftime("%-m/%-d")
            out.append({"time": tstr, "title": summary[:20]})
        _calendar["events"] = out
    except Exception:
        _calendar["events"] = []
    _calendar["exp"] = time.time() + 300
    return _calendar["events"]


# --- rotate mode: server cycles the reported screen; the panel just follows ---
def rotate_list():
    return [s.strip() for s in get("rotate_screens").split(",") if s.strip()] or ["clock"]


def current_rotate_screen():
    screens = rotate_list()
    rs = max(3, int(get("rotate_sec")))
    return screens[int(time.time() // rs) % len(screens)]




def fetch_data():
    global _active_source
    mode = effective_mode()
    if mode in ("clock", "world", "world4", "weather", "picture"):
        _active_source = get("data_source")
        return []
    src = get("data_source")
    table = TRACKED if mode == "track" else NEARBY
    try:
        res = finalize(table.get(src, fr24_nearby)(), track=(mode == "track"))
        _active_source = src
        return res
    except Exception as e:
        # auto-fallback to the free OpenSky source if the chosen one fails
        if get("auto_fallback") and src != "opensky":
            print(f"[{time.strftime('%H:%M:%S')}] {src} FAILED ({e}) -> falling back to OpenSky")
            res = finalize(table["opensky"](), track=(mode == "track"))
            _active_source = "opensky (fallback from " + src + ")"
            return res
        raise


def refresh_loop():
    global _aircraft, _last_update, _last_error, _version
    while True:
        try:
            ac = fetch_data()
            with _data_lock:
                _aircraft = ac
                _last_update = time.time()
                _last_error = ""
                _version += 1
            print(f"[{time.strftime('%H:%M:%S')}] source={_active_source}  mode={effective_mode()}  -> {len(ac)} aircraft")
        except Exception as e:
            with _data_lock:
                _last_error = str(e)
            print(f"[{time.strftime('%H:%M:%S')}] fetch error: {e}")
        _refresh_now.wait(timeout=get("refresh_sec"))
        _refresh_now.clear()


def _is_night():
    try:
        now = datetime.datetime.now().strftime("%H:%M")
        s, e = get("night_start"), get("night_end")
        if s == e:
            return False
        if s < e:                      # same-day window (e.g. 01:00-06:00)
            return s <= now < e
        return now >= s or now < e     # overnight window (e.g. 22:00-07:00)
    except Exception:
        return False


def effective_brightness():
    if get("night_mode") and _is_night():
        return int(get("night_brightness"))
    return int(get("brightness"))


def effective_mode():
    # Auto-switch to clock-only during the night window if enabled.
    if get("night_to_clock") and _is_night():
        return "clock"
    m = get("mode")
    if m == "rotate":
        return current_rotate_screen()
    return m


# date_format key -> human label (for the UI). Formatting is done in format_date().
DATE_FORMATS = [
    ("month_day_year",    "January 1, 2026"),
    ("mon_day_year",      "Jan 1, 2026"),
    ("numeric_mdy",       "1/1/2026"),
    ("numeric_mdy_pad",   "01/01/2026"),
    ("numeric_dmy",       "1/1/2026 (day first)"),
    ("iso",               "2026-01-01"),
    ("weekday_month_day", "Monday, January 1"),
    ("weekday_short",     "Mon Jan 1"),
    ("weekday_only",      "Monday"),
    ("weekday_mdy",       "Mon 1/1/2026"),
    ("day_month_year",    "1 January 2026"),
]


try:
    from zoneinfo import ZoneInfo
    HAVE_TZ = True
except Exception:
    HAVE_TZ = False

# Full list the UI offers: (zone id, abbreviation, city label). Spans every offset.
WORLD_ZONES = [
    ("Pacific/Midway", "MIT", "Midway"),
    ("Pacific/Honolulu", "HNL", "Honolulu"),
    ("America/Anchorage", "ANC", "Anchorage"),
    ("America/Los_Angeles", "LAX", "Los Angeles"),
    ("America/Phoenix", "PHX", "Phoenix"),
    ("America/Denver", "DEN", "Denver"),
    ("America/Chicago", "CHI", "Chicago"),
    ("America/New_York", "NYC", "New York"),
    ("America/Toronto", "YYZ", "Toronto"),
    ("America/Mexico_City", "MEX", "Mexico City"),
    ("America/Bogota", "BOG", "Bogota"),
    ("America/Sao_Paulo", "SAO", "Sao Paulo"),
    ("America/Argentina/Buenos_Aires", "BUE", "Buenos Aires"),
    ("Atlantic/Reykjavik", "REK", "Reykjavik"),
    ("Europe/London", "LON", "London"),
    ("Europe/Lisbon", "LIS", "Lisbon"),
    ("Europe/Paris", "PAR", "Paris"),
    ("Europe/Madrid", "MAD", "Madrid"),
    ("Europe/Berlin", "BER", "Berlin"),
    ("Europe/Rome", "ROM", "Rome"),
    ("Europe/Amsterdam", "AMS", "Amsterdam"),
    ("Europe/Athens", "ATH", "Athens"),
    ("Europe/Istanbul", "IST", "Istanbul"),
    ("Europe/Moscow", "MOW", "Moscow"),
    ("Africa/Lagos", "LOS", "Lagos"),
    ("Africa/Cairo", "CAI", "Cairo"),
    ("Africa/Johannesburg", "JNB", "Johannesburg"),
    ("Africa/Nairobi", "NBO", "Nairobi"),
    ("Asia/Jerusalem", "JLM", "Jerusalem"),
    ("Asia/Riyadh", "RUH", "Riyadh"),
    ("Asia/Dubai", "DXB", "Dubai"),
    ("Asia/Tehran", "THR", "Tehran"),
    ("Asia/Karachi", "KHI", "Karachi"),
    ("Asia/Kolkata", "DEL", "India"),
    ("Asia/Dhaka", "DAC", "Dhaka"),
    ("Asia/Bangkok", "BKK", "Bangkok"),
    ("Asia/Jakarta", "JKT", "Jakarta"),
    ("Asia/Singapore", "SIN", "Singapore"),
    ("Asia/Hong_Kong", "HKG", "Hong Kong"),
    ("Asia/Shanghai", "SHA", "Shanghai"),
    ("Asia/Manila", "MNL", "Manila"),
    ("Asia/Seoul", "SEL", "Seoul"),
    ("Asia/Tokyo", "TYO", "Tokyo"),
    ("Australia/Perth", "PER", "Perth"),
    ("Australia/Adelaide", "ADL", "Adelaide"),
    ("Australia/Sydney", "SYD", "Sydney"),
    ("Pacific/Auckland", "AKL", "Auckland"),
]
ZONE_AB = {z: ab for z, ab, _ in WORLD_ZONES}


def world_times():
    """For each selected zone, return its abbreviation and current UTC offset
    (minutes, DST-aware). The ESP32 ticks the actual time locally from this."""
    if not HAVE_TZ:
        return []
    out = []
    for zid in [z.strip() for z in get("world_zones").split(",") if z.strip()]:
        try:
            now = datetime.datetime.now(ZoneInfo(zid))
            off = int(now.utcoffset().total_seconds() // 60)
        except Exception:
            continue
        ab = ZONE_AB.get(zid, zid.split("/")[-1][:3].upper())
        hrs = off / 60.0
        sign = "+" if hrs >= 0 else "-"
        ah = abs(hrs)
        gmt = f"{sign}{int(ah)}" if ah == int(ah) else f"{sign}{int(ah)}:{int(round((ah-int(ah))*60)):02d}"
        out.append({"ab": ab, "off": off, "gmt": gmt})
    return out[:8]


def format_date(fmt):
    now = datetime.datetime.now()
    mon, monb = now.strftime("%B"), now.strftime("%b")
    wd, wdb = now.strftime("%A"), now.strftime("%a")
    d, m, y = now.day, now.month, now.year
    table = {
        "month_day_year":    f"{mon} {d}, {y}",
        "mon_day_year":      f"{monb} {d}, {y}",
        "numeric_mdy":       f"{m}/{d}/{y}",
        "numeric_mdy_pad":   f"{m:02d}/{d:02d}/{y}",
        "numeric_dmy":       f"{d}/{m}/{y}",
        "iso":               f"{y:04d}-{m:02d}-{d:02d}",
        "weekday_month_day": f"{wd}, {mon} {d}",
        "weekday_short":     f"{wdb} {monb} {d}",
        "weekday_only":      wd,
        "weekday_mdy":       f"{wdb} {m}/{d}/{y}",
        "day_month_year":    f"{d} {mon} {y}",
    }
    return table.get(fmt, table["month_day_year"])


def load_picture():
    global _picture
    try:
        if os.path.exists(PICTURE_FILE):
            with open(PICTURE_FILE) as f:
                _picture = f.read().strip()
    except Exception:
        _picture = ""


def store_picture(data_url):
    """Take a data: URL or base64 image, resize to 128x64, store as RGB565 base64."""
    global _picture, _picture_ver
    if not HAVE_PIL:
        raise RuntimeError("Pillow not installed (pip install Pillow)")
    b64 = data_url.split(",", 1)[-1]
    raw = base64.b64decode(b64)
    img = Image.open(io.BytesIO(raw)).convert("RGB").resize((128, 64))
    out = bytearray()
    for y in range(64):
        for x in range(128):
            r, g, b = img.getpixel((x, y))
            v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            out.append((v >> 8) & 0xFF)
            out.append(v & 0xFF)
    _picture = base64.b64encode(bytes(out)).decode()
    _picture_ver += 1
    try:
        with open(PICTURE_FILE, "w") as f:
            f.write(_picture)
    except Exception:
        pass


def config_obj():
    hexc = get("text_color").lstrip("#")
    try:
        r, g, b = int(hexc[0:2], 16), int(hexc[2:4], 16), int(hexc[4:6], 16)
    except Exception:
        r, g, b = 255, 140, 0
    date_str = format_date(get("date_format")) if get("clock_date") else ""
    m = effective_mode()
    zones = world_times() if m in ("world", "world4") else []
    forecast = get_forecast() if m == "weather" else {}
    cal = get_calendar() if m in ("clock", "weather") else []
    return {"color": [r, g, b], "brightness": effective_brightness(),
            "border": bool(get("show_border")), "logos": bool(get("show_logos")),
            "logo_px": int(get("logo_px")), "mode": m,
            "clock": bool(get("show_clock")), "clock24": bool(get("clock24h")),
            "rainbow": bool(get("rainbow")), "date": date_str,
            "weather": get_weather() if get("show_weather") else "",
            "zones": zones, "forecast": forecast, "cal": cal,
            "cycle": int(get("cycle_sec"))}


# ---------------- web ----------------
DASHBOARD = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>FlightWall Mini</title>
<link rel=manifest href="/manifest.json">
<meta name=theme-color content="#0c0e12">
<meta name=apple-mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-status-bar-style content=black-translucent>
<meta name=apple-mobile-web-app-title content="FlightWall">
<link rel=apple-touch-icon href="/icon.png">
<style>
:root{--bg:#0c0e12;--card:#161a21;--line:#262c36;--ink:#e7ecf3;--mut:#8b94a3;--acc:#ff8c00;--ok:#37d07a;--bad:#ff5d5d}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 50% -10%,#141925 0,var(--bg) 60%);color:var(--ink);font:15px/1.5 -apple-system,system-ui,sans-serif;padding-bottom:90px}
.wrap{max-width:760px;margin:0 auto;padding:20px 16px}
.head{display:flex;align-items:center;gap:11px}
.logo{width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,var(--acc),#ff5e00);display:flex;align-items:center;justify-content:center;font-size:18px}
h1{font-size:19px;margin:0;font-weight:700}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mut);display:inline-block;margin-right:5px}
.dot.on{background:var(--ok);box-shadow:0 0 8px var(--ok)}
.sub{color:var(--mut);font-size:13px;margin-top:4px}
.tabs{display:flex;gap:6px;overflow-x:auto;margin:18px 0 16px;padding-bottom:4px;-webkit-overflow-scrolling:touch}
.tab{flex:0 0 auto;background:var(--card);border:1px solid var(--line);color:var(--mut);border-radius:11px;padding:9px 15px;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap;transition:.15s}
.tab.on{background:var(--acc);border-color:var(--acc);color:#1a1206}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;margin-bottom:14px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);margin:0 0 14px;font-weight:700}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.05em;padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:8px}tr:not(:last-child) td{border-bottom:1px solid var(--line)}
.cs{font-weight:700;color:var(--acc)}.mut{color:var(--mut)}
.field{margin-bottom:13px}label{display:block;font-size:12px;color:var(--mut);margin-bottom:6px}
input,select{width:100%;background:#0e1217;border:1px solid var(--line);color:var(--ink);border-radius:9px;padding:10px 11px;font:inherit}
input:focus,select:focus{outline:none;border-color:var(--acc)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.chk{display:flex;align-items:center;gap:10px;padding:9px 0;cursor:pointer}
.chk input{width:20px;height:20px;accent-color:var(--acc);flex:0 0 auto}
.chk span{font-size:14px;color:var(--ink)}
.opts{display:grid;grid-template-columns:1fr 1fr;gap:4px 16px}
button{background:var(--acc);color:#1a1206;border:0;border-radius:11px;padding:12px 16px;font-weight:700;font-size:15px;cursor:pointer;width:100%}
button.ghost{background:#0e1217;color:var(--ink);border:1px solid var(--line)}
button:active{transform:translateY(1px)}
.note{font-size:12px;color:var(--mut);margin-top:8px}
.savebar{position:fixed;left:0;right:0;bottom:0;background:rgba(12,14,18,.92);backdrop-filter:blur(10px);border-top:1px solid var(--line);padding:12px 16px;z-index:20}
.savebar .inner{max-width:760px;margin:0 auto}
.toast{position:fixed;bottom:84px;left:50%;transform:translateX(-50%);background:var(--ok);color:#04240f;padding:11px 20px;border-radius:11px;font-weight:700;opacity:0;transition:.25s;pointer-events:none;z-index:30}
.toast.show{opacity:1}
.empty{color:var(--mut);text-align:center;padding:26px 0}
hr{border:0;border-top:1px solid var(--line);margin:16px 0}
.trk .big{font-size:24px;font-weight:800;color:var(--acc)}
.trk .route{font-size:16px;margin:6px 0 2px}
.bar{height:12px;background:#0e1217;border:1px solid var(--line);border-radius:7px;overflow:hidden;margin:12px 0 6px}
.bar>i{display:block;height:100%;background:var(--acc)}
.trk .meta{display:flex;justify-content:space-between;color:var(--mut);font-size:13px}
.pill{display:inline-block;font-size:12px;padding:3px 9px;border-radius:20px;background:#0e1217;border:1px solid var(--line);color:var(--mut);margin-right:6px}
.pill.ok{color:var(--ok);border-color:#1f5e3a}.pill.bad{color:var(--bad);border-color:#5e2020}
</style></head><body><div class=wrap>
<div class=head><div class=logo>&#9992;</div><div><h1>FlightWall Mini</h1><div class=sub id=status>starting...</div></div></div>

<div class=tabs id=tabs>
  <div class=tab data-tab=live>Live</div>
  <div class=tab data-tab=display>Display</div>
  <div class=tab data-tab=clock>Clock</div>
  <div class=tab data-tab=look>Look</div>
  <div class=tab data-tab=sources>Sources</div>
  <div class=tab data-tab=device>Device</div>
</div>

<div class=panel data-tab=live>
  <div class=card><h2>Live</h2><div id=list><div class=empty>loading...</div></div></div>
</div>

<div class=panel data-tab=display>
  <div class=card><h2>Mode</h2>
    <div class=field><select id=mode>
      <option value=nearby>Nearby - cycle local flights</option>
      <option value=track>Track - follow one flight</option>
      <option value=picture>Picture - show uploaded image</option>
      <option value=rotate>Rotate - cycle through screens</option>
      <option value=clock>Clock only</option>
      <option value=world>World clock</option>
      <option value=world4>World clock x4</option>
      <option value=weather>Weather</option></select></div>
    <div class=field id=trackrow><label>Flight to track</label><input id=track_flight placeholder="e.g. UAL123"></div>
  </div>
  <div class=card id=nearbycard><h2>Nearby</h2>
    <div class=row2>
      <div class=field><label>Max aircraft</label><input id=max_aircraft type=number></div>
      <div class=field><label>Seconds per plane</label><input id=cycle_sec type=number min=2 max=60></div>
    </div>
    <div class=note>Seconds per plane controls how fast the wall flips planes - it does not change how often data is fetched.</div>
  </div>
  <div class=card id=rotatecard><h2>Rotate screens</h2>
    <div class=opts id=rotopts></div>
    <div class=field style="margin-top:12px"><label>Seconds per screen</label><input id=rotate_sec type=number min=3 max=120></div>
  </div>
  <div class=card id=picturecard><h2>Picture</h2>
    <div class=field><input id=picfile type=file accept="image/*"></div>
    <button type=button class=ghost onclick=uploadPic()>Upload picture</button>
    <div class=note id=pichint>Resized to 128x64. Switch Mode to Picture to show it.</div>
  </div>
</div>

<div class=panel data-tab=clock>
  <div class=card><h2>Time</h2>
    <label class=chk><input id=clock24h type=checkbox><span>24-hour clock</span></label>
    <label class=chk><input id=show_clock type=checkbox><span>Show clock on flight screens</span></label>
  </div>
  <div class=card><h2>Date</h2>
    <label class=chk><input id=clock_date type=checkbox><span>Show date</span></label>
    <div class=field style="margin-top:10px"><label>Date format</label><select id=date_format>__DATEOPTS__</select></div>
  </div>
  <div class=card><h2>World clock zones</h2>
    <select id=world_zones multiple size=8 style="height:auto">__ZONEOPTS__</select>
    <div class=note>Cmd/Ctrl-click for multiple. World x4 uses the first 4.</div>
  </div>
  <div class=card><h2>Weather</h2>
    <label class=chk><input id=show_weather type=checkbox><span>Show weather on clock screen</span></label>
  </div>
  <div class=card><h2>Google Calendar (iCal)</h2>
    <div class=field><input id=ical_url placeholder="https://calendar.google.com/.../basic.ics"></div>
    <div class=note>Calendar &gt; Settings &gt; your calendar &gt; Secret address in iCal format.</div>
  </div>
</div>

<div class=panel data-tab=look>
  <div class=card><h2>Color</h2>
    <div class=row2>
      <div class=field><label>Text color</label><input id=text_color type=color style="height:44px;padding:4px"></div>
      <div class=field><label>Brightness (0-255)</label><input id=brightness type=number min=0 max=255></div>
    </div>
    <label class=chk><input id=rainbow type=checkbox><span>Rainbow color cycle</span></label>
  </div>
  <div class=card><h2>Layout</h2>
    <label class=chk><input id=show_border type=checkbox><span>Border around screen</span></label>
    <label class=chk><input id=show_logos type=checkbox><span>Airline logos</span></label>
    <div class=note id=logohint></div>
    <div class=field style="margin-top:10px"><label>Logo size (px)</label><input id=logo_px type=number min=12 max=32></div>
  </div>
  <div class=card><h2>Night mode</h2>
    <label class=chk><input id=night_mode type=checkbox><span>Auto-dim at night</span></label>
    <div class=row2 style="margin-top:8px">
      <div class=field><label>Night start</label><input id=night_start type=time></div>
      <div class=field><label>Night end</label><input id=night_end type=time></div>
    </div>
    <div class=field><label>Night brightness (0-255)</label><input id=night_brightness type=number min=0 max=255></div>
    <label class=chk><input id=night_to_clock type=checkbox><span>At night, switch to clock-only</span></label>
  </div>
</div>

<div class=panel data-tab=sources>
  <div class=card><h2>Data source</h2>
    <div class=field><select id=data_source>
      <option value=fr24>Flightradar24 (paid)</option>
      <option value=opensky>OpenSky (free)</option>
      <option value=flightaware>FlightAware (paid)</option></select></div>
    <div class=field src=fr24><label>Flightradar24 API token</label><input id=fr24_token type=password></div>
    <div class=row2 src=opensky>
      <div class=field><label>OpenSky client id</label><input id=opensky_client_id placeholder=optional></div>
      <div class=field><label>OpenSky secret</label><input id=opensky_client_secret type=password></div>
    </div>
    <div class=field src=flightaware><label>FlightAware API key</label><input id=flightaware_api_key type=password></div>
    <label class=chk><input id=auto_fallback type=checkbox><span>Auto-fallback to OpenSky if source fails</span></label>
  </div>
  <div class=card><h2>Location</h2>
    <div class=row2>
      <div class=field><label>Latitude</label><input id=center_lat type=number step=0.0001></div>
      <div class=field><label>Longitude</label><input id=center_lon type=number step=0.0001></div>
    </div>
    <div class=row2>
      <div class=field><label>Radius (km)</label><input id=radius_km type=number></div>
      <div class=field><label>Refresh data (s)</label><input id=refresh_sec type=number></div>
    </div>
  </div>
  <div class=card><h2>Filters</h2>
    <div class=field><label>Show airports as</label><select id=place_style>
      <option value=city>City name</option><option value=code>Airport code</option></select></div>
    <label class=chk><input id=airline_only type=checkbox><span>Hide private / GA flights</span></label>
    <label class=chk><input id=highlight_special type=checkbox><span>Highlight emergency / military</span></label>
    <div class=field style="margin-top:10px"><label>Only these airlines (blank = all)</label><input id=fav_airlines placeholder="UA, AAL, DL"></div>
    <div class=field><label>Only these aircraft types (blank = all)</label><input id=fav_types placeholder="B738, A320"></div>
  </div>
</div>

<div class=panel data-tab=device>
  <div class=card><h2>Status</h2><div id=health class=sub>checking...</div></div>
  <div class=card><h2>About</h2>
    <div class=note>This dashboard, the iOS app, and the wall all stay in sync automatically. Changes here appear on the others within a few seconds.</div>
  </div>
</div>

</div>
<div class=savebar><div class=inner><button onclick=save()>Save settings</button></div></div>
<div class=toast id=toast>Saved</div>
<script>
const $=id=>document.getElementById(id);
const FIELDS=["data_source","fr24_token","opensky_client_id","opensky_client_secret","flightaware_api_key","mode","track_flight","center_lat","center_lon","radius_km","max_aircraft","cycle_sec","refresh_sec","place_style","airline_only","auto_fallback","highlight_special","show_weather","fav_airlines","fav_types","show_clock","clock24h","rainbow","night_mode","night_start","night_end","night_brightness","night_to_clock","clock_date","date_format","rotate_sec","ical_url","text_color","brightness","show_border","show_logos","logo_px"];
const NUM=["center_lat","center_lon","radius_km","max_aircraft","cycle_sec","refresh_sec","brightness","logo_px","night_brightness","rotate_sec"];
const BOOL=["airline_only","auto_fallback","highlight_special","show_weather","show_clock","clock24h","rainbow","night_mode","night_to_clock","clock_date","show_border","show_logos"];
const CREDS=["fr24_token","opensky_client_id","opensky_client_secret","flightaware_api_key"];
const ROTOPTS=[["nearby","Nearby flights"],["track","Tracked flight"],["clock","Clock"],["world","World clock"],["world4","World clock x4"],["weather","Weather"],["picture","Picture"]];
$('rotopts').innerHTML=ROTOPTS.map(o=>`<label class=chk><input type=checkbox class=rotcb value="${o[0]}"><span>${o[1]}</span></label>`).join('');
function showTab(n){document.querySelectorAll('.panel').forEach(p=>p.style.display=p.dataset.tab===n?'':'none');document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.tab===n));}
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>showTab(t.dataset.tab)));
showTab('live');
function uploadPic(){
  const f=$('picfile').files[0]; if(!f){ $('pichint').textContent='Pick an image first.'; return; }
  const rd=new FileReader();
  rd.onload=async()=>{ $('pichint').textContent='Uploading...';
    const r=await fetch('/api/picture',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image:rd.result})});
    const j=await r.json(); $('pichint').textContent=j.ok?'Uploaded. Switch Mode to Picture to see it.':('Error: '+(j.error||'failed')); };
  rd.readAsDataURL(f);
}
function syncRows(){
  const src=$('data_source').value;
  document.querySelectorAll('[src]').forEach(el=>{ el.style.display = el.getAttribute('src')===src ? '' : 'none'; });
  const m=$('mode').value;
  $('trackrow').style.display = m==='track' ? '' : 'none';
  $('nearbycard').style.display = m==='nearby' ? '' : 'none';
  $('rotatecard').style.display = m==='rotate' ? '' : 'none';
  $('picturecard').style.display = m==='picture' ? '' : 'none';
}
async function loadSettings(){
  const s=await (await fetch('/api/settings')).json();
  FIELDS.forEach(k=>{ const el=$(k); if(!el) return; if(BOOL.includes(k)) el.checked=!!s[k]; else el.value=s[k]; });
  CREDS.forEach(k=>{ const el=$(k); if(el){ el.value=''; el.placeholder = s[k+'_set'] ? 'saved - leave blank to keep' : 'not set'; }});
  const wz=$('world_zones'); if(wz){ const sel=(s.world_zones||'').split(',').map(x=>x.trim()); for(const o of wz.options) o.selected=sel.includes(o.value); }
  const rs=(s.rotate_screens||'').split(',').map(x=>x.trim());
  document.querySelectorAll('.rotcb').forEach(c=>c.checked=rs.includes(c.value));
  syncRows();
}
$('data_source').addEventListener('change',syncRows);
$('mode').addEventListener('change',syncRows);
async function save(){
  const body={};
  FIELDS.forEach(k=>{ const el=$(k); if(!el) return; let v=BOOL.includes(k)?el.checked:el.value; if(NUM.includes(k)) v=parseFloat(v); body[k]=v; });
  CREDS.forEach(k=>{ if(body[k]==='') delete body[k]; });
  const wz=$('world_zones'); if(wz){ body.world_zones=[...wz.selectedOptions].map(o=>o.value).join(','); }
  body.rotate_screens=[...document.querySelectorAll('.rotcb:checked')].map(c=>c.value).join(',');
  await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const t=$('toast'); t.classList.add('show'); setTimeout(()=>t.classList.remove('show'),1400);
  loadSettings(); refresh();
}
function ago(ts){ if(!ts) return 'never'; const s=Math.round(Date.now()/1000-ts); return s<60?s+'s ago':Math.round(s/60)+'m ago'; }
function trackerView(a){
  const eta=a.eta>=0?(a.eta+' min'):'--';
  return `<div class=trk><div class=big>${a.cs||''}</div>
    <div class=route>${a.from||'?'} \u2192 ${a.to||'?'}</div>
    <div class=bar><i style="width:${a.progress||0}%"></i></div>
    <div class=meta><span>${a.status||''}</span><span>${a.progress||0}%</span><span>ETA ${eta}</span></div>
    <div class=meta style="margin-top:8px"><span>${a.type||''}</span><span>${a.alt}ft</span><span>${a.spd}mph</span></div></div>`;
}
async function refresh(){
  try{
    const d=await (await fetch('/api/status')).json();
    $('dot').classList.toggle('on', d.count>0);
    $('status').innerHTML = `<span class=dot id=dot></span>${d.mode==='track'?'tracking':d.count+' aircraft'} \u00b7 ${d.active_source||d.source} \u00b7 ${ago(d.last_update)}` + (d.error?` \u00b7 ${d.error}`:'');
    const hh=$('health');
    if(hh){
      const srv = d.server_ok ? '<span class="pill ok">Server OK</span>' : '<span class="pill bad">Server '+(d.error||'starting')+'</span>';
      const dev = d.device_online ? `<span class="pill ok">Display online (${ago(d.device_last)})</span>`
                  : (d.device_ip ? `<span class="pill bad">Display offline (${ago(d.device_last)})</span>`
                                 : '<span class="pill bad">Display never connected</span>');
      const wx = d.weather ? `<span class=pill>${d.weather}</span>` : '';
      hh.innerHTML = srv+' '+dev+' '+wx;
    }
    const lh=$('logohint');
    if(lh){
      if(!d.logos_on) lh.textContent='Logos off.';
      else if(!d.have_pil) lh.textContent='Pillow not installed - run: pip install Pillow';
      else if(d.logo_count>0) lh.textContent=`Logos active for ${d.logo_count}/${d.count} aircraft.`;
      else lh.textContent='No logos yet.';
    }
    const listEl=$('list'); if(!listEl) return;
    if(d.mode==='track'){
      listEl.innerHTML = d.aircraft.length ? trackerView(d.aircraft[0]) : '<div class=empty>That flight isn\\'t airborne right now.</div>';
      return;
    }
    const rows=d.aircraft.map(a=>{
      const route = a.from||a.to ? `${a.from||'?'} \u2192 ${a.to||'?'}` : '<span class=mut>\u2014</span>';
      return `<tr><td class=cs>${a.cs||''}</td><td class=mut>${a.type||''}</td><td>${route}</td>
              <td>${a.alt}ft</td><td>${a.spd}mph</td><td class=mut>${a.dist}km</td></tr>`;
    }).join('');
    listEl.innerHTML = d.aircraft.length
      ? `<table><tr><th>Flight</th><th>Type</th><th>Route</th><th>Alt</th><th>Spd</th><th>Dist</th></tr>${rows}</table>`
      : '<div class=empty>No aircraft nearby right now.</div>';
  }catch(e){ $('status').textContent='server unreachable'; }
}
loadSettings(); refresh(); setInterval(refresh,3000);
let lastTouch=0;
document.addEventListener('input',()=>{ lastTouch=Date.now(); });
async function syncSettings(){
  if(Date.now()-lastTouch < 4000) return;
  try{
    const v=(await (await fetch('/version')).json()).v;
    if(v===window._sv) return; window._sv=v;
    if(Date.now()-lastTouch >= 4000) loadSettings();
  }catch(e){}
}
setInterval(syncSettings,3000);
</script></body></html>"""

MANIFEST = json.dumps({
    "name": "FlightWall Mini", "short_name": "FlightWall", "start_url": "/",
    "display": "standalone", "background_color": "#0c0e12", "theme_color": "#0c0e12",
    "icons": [{"src": "/icon.png", "sizes": "180x180", "type": "image/png", "purpose": "any"}],
})

ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="180" height="180" viewBox="0 0 180 180">'
            '<rect width="180" height="180" rx="40" fill="#0c0e12"/>'
            '<path fill="#ff8c00" d="M90 26l9 44 50 30v10l-50-16v34l16 12v8l-25-7-25 7v-8l16-12v-34l-50 16v-10l50-30z"/></svg>')

_icon_png = None
def icon_bytes():
    global _icon_png
    if not HAVE_PIL:
        return None
    if _icon_png is not None:
        return _icon_png
    try:
        from PIL import ImageDraw
        S = 180
        img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, S - 1, S - 1], radius=40, fill=(12, 14, 18, 255))
        o = (255, 140, 0, 255)
        cx = 90
        d.polygon([(cx, 28), (cx + 11, 72), (cx + 11, 120), (cx, 140), (cx - 11, 120), (cx - 11, 72)], fill=o)
        d.polygon([(cx - 11, 84), (28, 120), (28, 130), (cx - 11, 108)], fill=o)
        d.polygon([(cx + 11, 84), (152, 120), (152, 130), (cx + 11, 108)], fill=o)
        d.polygon([(cx - 8, 128), (60, 152), (60, 158), (cx - 3, 140)], fill=o)
        d.polygon([(cx + 8, 128), (120, 152), (120, 158), (cx + 3, 140)], fill=o)
        b = io.BytesIO()
        img.save(b, "PNG")
        _icon_png = b.getvalue()
    except Exception:
        _icon_png = None
    return _icon_png


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/version":
            global _rotate_last, _version
            with _data_lock:
                if get("mode") == "rotate":
                    cur = effective_mode()
                    if cur != _rotate_last:
                        _rotate_last = cur
                        _version += 1
                self._send(200, "application/json", json.dumps({"v": _version, "pv": _picture_ver}))
            return
        if path == "/picture":
            self._send(200, "application/json", json.dumps({"pv": _picture_ver, "img": _picture}))
            return
        if path == "/flights":
            global _device_ip, _device_last
            _device_ip = self.client_address[0]
            _device_last = time.time()
            with _data_lock:
                body = json.dumps({"v": _version, "config": config_obj(), "aircraft": _aircraft})
            self._send(200, "application/json", body)
        elif path == "/api/status":
            with _data_lock:
                light = [{k: v for k, v in a.items() if k != "logo"} for a in _aircraft]
                logo_count = sum(1 for a in _aircraft if a.get("logo"))
                # the display is "online" if it fetched recently (within ~3 refreshes)
                window = max(90, get("refresh_sec") * 3)
                device_online = bool(_device_last) and (time.time() - _device_last) < window
                server_ok = (_last_error == "") and (_last_update > 0)
                body = json.dumps({
                    "aircraft": light, "count": len(_aircraft),
                    "last_update": _last_update, "error": _last_error,
                    "have_pil": HAVE_PIL, "logo_count": logo_count,
                    "logos_on": bool(get("show_logos")), "mode": get("mode"),
                    "source": get("data_source"), "device_ip": _device_ip,
                    "active_source": _active_source, "weather": _weather["txt"],
                    "device_last": _device_last, "device_online": device_online,
                    "server_ok": server_ok,
                })
            self._send(200, "application/json", body)
        elif path == "/api/settings":
            with _settings_lock:
                out = dict(_settings)
                for k in CRED_KEYS:                     # never expose stored secrets
                    out[k + "_set"] = bool(out.get(k))
                    out[k] = ""
                body = json.dumps(out)
            self._send(200, "application/json", body)
        elif path.startswith("/api/logotest"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._send(200, "application/json", json.dumps(logo_diag((q.get("code") or ["UA"])[0])))
        elif path == "/manifest.json":
            self._send(200, "application/manifest+json", MANIFEST)
        elif path == "/icon.png":
            png = icon_bytes()
            if png:
                self._send(200, "image/png", png)
            else:
                self._send(200, "image/svg+xml", ICON_SVG)
        elif path == "/":
            opts = "".join(f'<option value="{k}">{lbl}</option>' for k, lbl in DATE_FORMATS)
            zopts = "".join(f'<option value="{z}">{lbl} ({ab})</option>' for z, ab, lbl in WORLD_ZONES)
            html = DASHBOARD.replace("__DATEOPTS__", opts).replace("__ZONEOPTS__", zopts)
            self._send(200, "text/html; charset=utf-8", html)
        else:
            self._send(404, "text/plain", "not found")

    def do_POST(self):
        global _version
        if self.path == "/api/picture":
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n).decode() or "{}")
                store_picture(body.get("image", ""))
                with _data_lock:
                    _version += 1
                self._send(200, "application/json", json.dumps({"ok": True, "pv": _picture_ver}))
            except Exception as e:
                self._send(400, "application/json", json.dumps({"ok": False, "error": str(e)}))
            return
        if self.path != "/api/settings":
            self._send(404, "text/plain", "not found")
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            incoming = json.loads(self.rfile.read(n).decode() or "{}")
            with _settings_lock:
                for k in DEFAULTS:
                    if k in incoming and incoming[k] is not None:
                        # don't wipe a stored secret when the field is sent blank
                        if k in CRED_KEYS and incoming[k] == "":
                            continue
                        _settings[k] = incoming[k]
            save_settings()
            _logo_cache.clear()
            with _data_lock:
                _version += 1
            _refresh_now.set()
            self._send(200, "application/json", json.dumps({"ok": True}))
        except Exception as e:
            self._send(400, "application/json", json.dumps({"ok": False, "error": str(e)}))

    def log_message(self, *a):
        pass


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    load_settings()
    load_picture()
    threading.Thread(target=refresh_loop, daemon=True).start()
    ip, port = local_ip(), get("port")
    print("=" * 56)
    print("  FlightWall Mini  -  server + dashboard")
    print("=" * 56)
    print(f"  Dashboard (open in a browser): http://{ip}:{port}/")
    print(f"  Data source: {get('data_source')}")
    print(f"  ESP32 config.h:  #define SERVER_HOST \"{ip}\"   #define SERVER_PORT {port}")
    print("  Leave this running while the display is on.")
    print("=" * 56)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
