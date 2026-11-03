#!/usr/bin/env python3
"""Flight Near Me - live display of aircraft passing overhead, fed by dump1090.

Reads the SBS/BaseStation feed (port 30003) from dump1090, optionally enriches
aircraft with type/registration/route data from adsbdb.com, and draws a radar
plot plus a list of nearby aircraft with pygame.
"""

import argparse
import copy
import json
import math
import os
import queue
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

try:  # some Python builds (notably python.org on macOS) lack system CA certs
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()

DEFAULT_CONFIG = {
    "latitude": 50.4176,
    "longitude": -5.1003,
    "feed_host": "localhost",
    "feed_port": 30003,
    "units": "km",             # distance units: km, nm or mi
    "range": 50,               # initial radar radius, in distance units
    "overhead_radius": 5,      # "overhead" banner threshold, in distance units
    "fullscreen": True,
    "window_size": [1280, 720],
    "fps": 10,
    "online_lookups": True,
    "cache_file": "lookup_cache.json",
    "trail_seconds": 180,
    "stale_seconds": 60,
    "coastline_file": "coastline.json",   # generate with make_coastline.py
    "landmarks": [
        {"name": "EGHQ", "lat": 50.4406, "lon": -4.9954},
    ],
}

UNIT_FACTORS = {"km": 1.0, "nm": 1 / 1.852, "mi": 1 / 1.609344}
RANGE_STEPS = [2, 5, 10, 15, 25, 50, 75, 100, 150, 200, 300]

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

EARTH_RADIUS_KM = 6371.0088
KM_PER_DEG_LAT = math.pi * EARTH_RADIUS_KM / 180
COMPASS_POINTS = "N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW".split()


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compass(bearing):
    return COMPASS_POINTS[int((bearing + 11.25) // 22.5) % 16]


def elevation_deg(dist_km, alt_ft):
    """Angle above the horizon of an aircraft, allowing for earth curvature."""
    height_km = alt_ft * 0.0003048 - dist_km ** 2 / (2 * EARTH_RADIUS_KM)
    return math.degrees(math.atan2(height_km, max(dist_km, 0.01)))


# ---------------------------------------------------------------------------
# Aircraft state, fed from the SBS stream
# ---------------------------------------------------------------------------


@dataclass
class Aircraft:
    hex: str
    callsign: str = ""
    altitude: int | None = None
    speed: float | None = None
    track: float | None = None
    vrate: int | None = None
    squawk: str = ""
    lat: float | None = None
    lon: float | None = None
    on_ground: bool = False
    last_seen: float = 0.0
    last_pos: float = 0.0
    messages: int = 0
    trail: deque = field(default_factory=lambda: deque(maxlen=400))


def _int(s):
    try:
        return int(float(s))
    except ValueError:
        return None


def _float(s):
    try:
        return float(s)
    except ValueError:
        return None


class AircraftStore:
    def __init__(self, stale_seconds, trail_seconds):
        self.stale_seconds = stale_seconds
        self.trail_seconds = trail_seconds
        self.lock = threading.Lock()
        self.aircraft = {}
        self.connected = False
        self.feed_error = ""
        self.msg_count = 0

    def handle_sbs(self, line):
        # MSG,type,session,aircraftID,hex,flightID,date,time,date,time,
        # callsign,alt,speed,track,lat,lon,vrate,squawk,alert,emerg,spi,ground
        parts = line.split(",")
        if len(parts) < 22 or parts[0] != "MSG":
            return
        hexid = parts[4].strip().upper()
        if not hexid:
            return
        now = time.time()
        with self.lock:
            self.msg_count += 1
            ac = self.aircraft.get(hexid)
            if ac is None:
                ac = self.aircraft[hexid] = Aircraft(hexid)
            ac.last_seen = now
            ac.messages += 1
            if cs := parts[10].strip():
                ac.callsign = cs
            if (alt := _int(parts[11])) is not None:
                ac.altitude = alt
            if (spd := _float(parts[12])) is not None:
                ac.speed = spd
            if (trk := _float(parts[13])) is not None:
                ac.track = trk
            if (vr := _int(parts[16])) is not None:
                ac.vrate = vr
            if sq := parts[17].strip():
                ac.squawk = sq
            if gnd := parts[21].strip():
                ac.on_ground = gnd in ("-1", "1")
            lat, lon = _float(parts[14]), _float(parts[15])
            if lat is not None and lon is not None and (lat or lon):
                ac.lat, ac.lon, ac.last_pos = lat, lon, now
                if not ac.trail or now - ac.trail[-1][0] >= 2:
                    ac.trail.append((now, lat, lon, ac.altitude))

    def snapshot(self):
        """Prune stale aircraft and return independent copies for drawing."""
        now = time.time()
        with self.lock:
            for k in [k for k, a in self.aircraft.items()
                      if now - a.last_seen > self.stale_seconds]:
                del self.aircraft[k]
            result = []
            for ac in self.aircraft.values():
                while ac.trail and now - ac.trail[0][0] > self.trail_seconds:
                    ac.trail.popleft()
                c = copy.copy(ac)
                c.trail = list(ac.trail)
                result.append(c)
            return result, self.msg_count


class SBSFeed(threading.Thread):
    """Keeps a connection to dump1090's SBS port open, reconnecting as needed."""

    def __init__(self, host, port, store):
        super().__init__(daemon=True)
        self.host, self.port, self.store = host, port, store

    def run(self):
        backoff = 1
        while True:
            try:
                with socket.create_connection((self.host, self.port), timeout=10) as s:
                    s.settimeout(90)
                    self.store.connected, self.store.feed_error = True, ""
                    backoff = 1
                    buf = b""
                    while True:
                        data = s.recv(65536)
                        if not data:
                            raise ConnectionError("connection closed")
                        buf += data
                        *lines, buf = buf.split(b"\n")
                        for ln in lines:
                            self.store.handle_sbs(ln.decode("ascii", "ignore").strip())
            except (OSError, ConnectionError) as e:
                self.store.connected, self.store.feed_error = False, str(e) or type(e).__name__
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)


# ---------------------------------------------------------------------------
# Online lookups (adsbdb.com), cached on disk
# ---------------------------------------------------------------------------


def _slim_airport(a):
    if not a:
        return None
    return {k: a.get(k) for k in ("iata_code", "icao_code", "name", "municipality",
                                   "latitude", "longitude")}


class LookupService(threading.Thread):
    API = "https://api.adsbdb.com/v0"
    TTL = {"aircraft": 30 * 86400, "route": 86400}
    MISS_TTL = 86400       # how long to remember "unknown" answers
    RETRY_AFTER = 300      # wait after network errors before retrying a key

    def __init__(self, cache_path, enabled):
        super().__init__(daemon=True)
        self.cache_path = cache_path
        self.enabled = enabled
        self.lock = threading.Lock()
        self.cache = {"aircraft": {}, "route": {}}
        self.queue = queue.Queue()
        self.pending = set()
        self.failed = {}
        self.dirty = False
        try:
            with open(cache_path) as f:
                loaded = json.load(f)
            for kind in self.cache:
                self.cache[kind].update(loaded.get(kind, {}))
        except (OSError, ValueError):
            pass

    def aircraft(self, hexid):
        return self._get("aircraft", hexid.upper())

    def route(self, callsign):
        callsign = callsign.upper()
        if not callsign.isalnum():
            return None
        return self._get("route", callsign)

    def _get(self, kind, key):
        if not self.enabled or not key:
            return None
        now = time.time()
        with self.lock:
            entry = self.cache[kind].get(key)
            data = entry["data"] if entry else None
            if entry and now - entry["t"] < (self.TTL[kind] if data else self.MISS_TTL):
                return data
            k = (kind, key)
            if k in self.pending or now - self.failed.get(k, 0) < self.RETRY_AFTER:
                return data
            self.pending.add(k)
        self.queue.put(k)
        return data

    def run(self):
        last_save = time.time()
        while True:
            try:
                kind, key = self.queue.get(timeout=5)
            except queue.Empty:
                kind = None
            if kind:
                self._lookup(kind, key)
                time.sleep(0.3)  # be polite to the free API
            if self.dirty and time.time() - last_save > 30:
                self.save()
                last_save = time.time()

    def _lookup(self, kind, key):
        path = "aircraft" if kind == "aircraft" else "callsign"
        req = urllib.request.Request(f"{self.API}/{path}/{key}",
                                     headers={"User-Agent": "flight-near-me/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT) as r:
                resp = json.load(r).get("response")
            resp = resp if isinstance(resp, dict) else {}
            if kind == "aircraft":
                result = self._slim_aircraft(resp.get("aircraft"))
            else:
                result = self._slim_route(resp.get("flightroute"))
            self._store(kind, key, result)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self._store(kind, key, None)
            else:
                self._fail(kind, key)
        except (OSError, ValueError):
            self._fail(kind, key)
        finally:
            with self.lock:
                self.pending.discard((kind, key))

    @staticmethod
    def _slim_aircraft(a):
        if not a:
            return None
        return {k: a.get(k) for k in ("type", "icao_type", "manufacturer",
                                       "registration", "registered_owner")}

    @staticmethod
    def _slim_route(r):
        if not r:
            return None
        airline = r.get("airline") or {}
        return {
            "airline": airline.get("name"),
            "origin": _slim_airport(r.get("origin")),
            "destination": _slim_airport(r.get("destination")),
        }

    def _store(self, kind, key, data):
        with self.lock:
            self.cache[kind][key] = {"t": time.time(), "data": data}
            self.dirty = True

    def _fail(self, kind, key):
        with self.lock:
            self.failed[(kind, key)] = time.time()

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            snapshot = json.dumps(self.cache)
            self.dirty = False
        tmp = self.cache_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(snapshot)
            os.replace(tmp, self.cache_path)
        except OSError:
            pass


def route_plausible(route, lat, lon):
    """adsbdb routes are keyed on callsign and are sometimes stale. Reject a
    route if the aircraft is well off the origin->destination corridor."""
    o, d = route.get("origin"), route.get("destination")
    if not o or not d or lat is None or None in (o.get("latitude"), d.get("latitude")):
        return True
    direct = haversine_km(o["latitude"], o["longitude"], d["latitude"], d["longitude"])
    via = (haversine_km(o["latitude"], o["longitude"], lat, lon)
           + haversine_km(lat, lon, d["latitude"], d["longitude"]))
    return via <= direct * 1.25 + 150


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

BG = (6, 10, 18)
PANEL_BG = (12, 18, 30)
RADAR_BG = (4, 14, 12)
RING = (28, 70, 55)
RING_TEXT = (60, 120, 95)
TEXT = (225, 232, 240)
DIM = (120, 132, 150)
FAINT = (70, 80, 95)
ACCENT = (255, 196, 0)
GOOD = (80, 220, 120)
BAD = (240, 80, 80)
HOME = (255, 255, 255)
LANDMARK = (150, 150, 220)
COAST = (50, 105, 135)


def altitude_color(alt, on_ground=False):
    if on_ground or alt is None:
        return (150, 150, 150)
    c = pygame.Color(0)
    hue = 20 + min(max(alt, 0), 40000) / 40000 * 280
    c.hsva = (hue, 80, 100, 100)
    return (c.r, c.g, c.b)


@dataclass
class View:
    """An aircraft plus everything derived for drawing it."""
    ac: Aircraft
    dist_km: float
    bearing: float
    info: dict | None
    route: dict | None


class Display:
    def __init__(self, cfg, store, lookup):
        self.cfg, self.store, self.lookup = cfg, store, lookup
        self.lat0, self.lon0 = cfg["latitude"], cfg["longitude"]
        self.units = cfg["units"] if cfg["units"] in UNIT_FACTORS else "km"
        self.unit_factor = UNIT_FACTORS[self.units]
        self.range = cfg["range"]
        self.overhead_km = cfg["overhead_radius"] / self.unit_factor
        self.fullscreen = cfg["fullscreen"]
        self.msg_hist = deque(maxlen=60)
        self.text_cache = {}
        self.label_slots = {}  # hex -> label position used last frame
        self.coastline = self._load_coastline(cfg.get("coastline_file"))

        pygame.init()
        pygame.display.set_caption("Flight Near Me")
        self._set_mode()

    # -- setup ---------------------------------------------------------------

    def _set_mode(self):
        if self.fullscreen:
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            self.screen = pygame.display.set_mode(self.cfg["window_size"], pygame.RESIZABLE)
        pygame.mouse.set_visible(not self.fullscreen)
        self._layout()

    def _font(self, size, bold=False):
        size = max(10, int(size * self.ui))
        for name in ("dejavusans", "helveticaneue", "helvetica", "arial"):
            path = pygame.font.match_font(name, bold=bold)
            if path:
                return pygame.font.Font(path, size)
        f = pygame.font.Font(None, int(size * 1.3))
        f.set_bold(bold)
        return f

    def _layout(self):
        W, H = self.screen.get_size()
        if W >= H * 1.2:
            side = min(H, int(W * 0.58))
            self.radar_rect = pygame.Rect(0, 0, side, H)
            self.panel_rect = pygame.Rect(side, 0, W - side, H)
        else:  # portrait monitor: radar on top
            side = min(W, int(H * 0.55))
            self.radar_rect = pygame.Rect(0, 0, W, side)
            self.panel_rect = pygame.Rect(0, side, W, H - side)
        if self.panel_rect.x:
            self.ui = max(0.6, min(self.panel_rect.w / 540, H / 720))
        else:
            self.ui = max(0.6, min(W / 720, H / 1280))
        self.f_tiny = self._font(14)
        self.f_small = self._font(17)
        self.f_med = self._font(21, bold=True)
        self.f_large = self._font(30, bold=True)
        self.f_huge = self._font(46, bold=True)
        self.arrow = "→" if self.f_small.metrics("→")[0] else "->"
        self.text_cache.clear()
        self._build_radar_bg()

    @staticmethod
    def _load_coastline(path):
        if not path:
            return []
        if not os.path.isabs(path):
            path = os.path.join(HERE, path)
        try:
            with open(path) as f:
                return [line for line in json.load(f)["lines"] if len(line) > 1]
        except (OSError, ValueError, KeyError):
            return []

    @property
    def range_km(self):
        return self.range / self.unit_factor

    def _build_radar_bg(self):
        r = self.radar_rect
        self.cx, self.cy = r.centerx, r.centery
        self.radius_px = min(r.w, r.h) / 2 - 12 * self.ui
        self.px_per_km = self.radius_px / self.range_km

        bg = pygame.Surface(r.size)
        bg.fill(RADAR_BG)
        cx, cy = self.cx - r.x, self.cy - r.y
        # Coastline
        for line in self.coastline:
            pts = [self.to_screen(lat, lon) for lat, lon in line]
            pygame.draw.aalines(bg, COAST, False, [(x - r.x, y - r.y) for x, y in pts])
        # Cross hairs and compass labels
        for ang in range(0, 360, 30):
            a = math.radians(ang)
            end = (cx + math.sin(a) * self.radius_px, cy - math.cos(a) * self.radius_px)
            pygame.draw.line(bg, (16, 40, 32), (cx, cy), end, 1)
        for label, ang in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
            a = math.radians(ang)
            d = self.radius_px - 14 * self.ui
            s = self.f_med.render(label, True, RING_TEXT)
            bg.blit(s, s.get_rect(center=(cx + math.sin(a) * d, cy - math.cos(a) * d)))
        # Range rings
        for i in range(1, 5):
            rr = self.radius_px * i / 4
            pygame.draw.circle(bg, RING, (cx, cy), rr, 1)
            val = self.range * i / 4
            txt = f"{val:g} {self.units}"
            s = self.f_tiny.render(txt, True, RING_TEXT)
            a = math.radians(135)
            bg.blit(s, (cx + math.sin(a) * rr + 3, cy - math.cos(a) * rr))
        # Overhead zone
        pygame.draw.circle(bg, (60, 55, 20), (cx, cy), max(3, self.overhead_km * self.px_per_km), 1)
        # Landmarks (their footprint is kept so aircraft labels avoid them)
        self.static_obstacles = [pygame.Rect(self.cx - 8, self.cy - 8, 16, 16)]
        for lm in self.cfg.get("landmarks", []):
            x, y = self.to_screen(lm["lat"], lm["lon"])
            x, y = x - r.x, y - r.y
            pygame.draw.rect(bg, LANDMARK, (x - 3, y - 3, 7, 7), 1)
            s = self.f_tiny.render(lm.get("name", ""), True, LANDMARK)
            bg.blit(s, (x + 6, y - s.get_height() / 2))
            self.static_obstacles.append(pygame.Rect(x + r.x - 4, y + r.y - s.get_height() / 2,
                                                     s.get_width() + 10, s.get_height()))
        # Home
        pygame.draw.line(bg, HOME, (cx - 7, cy), (cx + 7, cy), 2)
        pygame.draw.line(bg, HOME, (cx, cy - 7), (cx, cy + 7), 2)
        self.radar_bg = bg

    # -- helpers -------------------------------------------------------------

    def to_screen(self, lat, lon):
        dx = (lon - self.lon0) * KM_PER_DEG_LAT * math.cos(math.radians(self.lat0))
        dy = (lat - self.lat0) * KM_PER_DEG_LAT
        return self.cx + dx * self.px_per_km, self.cy - dy * self.px_per_km

    def text(self, s, font, color):
        key = (s, id(font), color)
        surf = self.text_cache.get(key)
        if surf is None:
            if len(self.text_cache) > 3000:
                self.text_cache.clear()
            surf = self.text_cache[key] = font.render(s, True, color)
        return surf

    def blit_text(self, s, font, color, pos, max_w=None, align="left"):
        if max_w is not None and font.size(s)[0] > max_w:
            while s and font.size(s + "…")[0] > max_w:
                s = s[:-1]
            s = s + "…" if s else ""
        surf = self.text(s, font, color)
        x, y = pos
        if align == "right":
            x -= surf.get_width()
        elif align == "center":
            x -= surf.get_width() / 2
        self.screen.blit(surf, (x, y))
        return surf.get_width()

    def fmt_dist(self, km):
        v = km * self.unit_factor
        return f"{v:.1f} {self.units}" if v < 100 else f"{v:.0f} {self.units}"

    @staticmethod
    def fmt_alt(ac):
        if ac.on_ground:
            return "GND"
        return f"{ac.altitude:,} ft" if ac.altitude is not None else "--- ft"

    def route_text(self, v, long=False):
        r = v.route
        if not r or not r.get("origin") or not r.get("destination"):
            return ""
        o, d = r["origin"], r["destination"]
        if long:
            def place(a):
                name = a.get("municipality") or a.get("name") or "?"
                code = a.get("iata_code") or a.get("icao_code")
                return f"{name} ({code})" if code else name
            return f"{place(o)} {self.arrow} {place(d)}"
        code = lambda a: a.get("iata_code") or a.get("icao_code") or "?"  # noqa: E731
        return f"{code(o)} {self.arrow} {code(d)}"

    # -- per-frame -----------------------------------------------------------

    def build_views(self, aircraft):
        views = []
        for ac in aircraft:
            if ac.lat is None:
                continue
            dist = haversine_km(self.lat0, self.lon0, ac.lat, ac.lon)
            info = self.lookup.aircraft(ac.hex)
            route = self.lookup.route(ac.callsign) if ac.callsign else None
            if route and not route_plausible(route, ac.lat, ac.lon):
                route = {"airline": route.get("airline")}
            views.append(View(ac, dist, bearing_deg(self.lat0, self.lon0, ac.lat, ac.lon),
                              info, route))
        views.sort(key=lambda v: v.dist_km)
        return views

    def draw(self, aircraft, msg_count):
        now = time.time()
        self.msg_hist.append((now, msg_count))
        views = self.build_views(aircraft)
        self.screen.fill(BG)
        self.draw_radar(views, now)
        self.draw_panel(views, len(aircraft), now)
        pygame.display.flip()

    def draw_radar(self, views, now):
        r = self.radar_rect
        self.screen.blit(self.radar_bg, r.topleft)
        self.screen.set_clip(r)
        featured = views[0] if views else None
        placed = []  # (view, x, y, colour, stale)
        for v in reversed(views):  # nearest drawn last, on top
            ac = v.ac
            # Trail
            pts = [(self.to_screen(lat, lon), alt) for _, lat, lon, alt in ac.trail]
            for (p1, _), (p2, alt) in zip(pts, pts[1:]):
                col = altitude_color(alt, ac.on_ground)
                pygame.draw.line(self.screen, [c // 2 for c in col], p1, p2, 2)
            x, y = self.to_screen(ac.lat, ac.lon)
            stale = now - ac.last_pos > 15
            col = altitude_color(ac.altitude, ac.on_ground)
            if stale:
                col = tuple(c // 2 for c in col)
            self.draw_plane(x, y, ac.track, col)
            if v is featured and v.dist_km <= self.overhead_km * 2:
                pygame.draw.circle(self.screen, ACCENT, (x, y), 16 * self.ui, 2)
            if r.collidepoint(x, y):
                placed.append((v, x, y, col, stale))
        self.draw_labels(reversed(placed))  # nearest aircraft get first pick of space
        self.screen.set_clip(None)
        # Range indicator
        self.blit_text(f"Range {self.range:g} {self.units}   [+/-]", self.f_tiny, DIM,
                       (r.x + 8, r.bottom - self.f_tiny.get_linesize() - 4))

    # Candidate label positions as unit offsets from the aircraft, tried in order
    LABEL_DIRS = [(1, 0), (0.7, -0.7), (0.7, 0.7), (-1, 0), (-0.7, -0.7), (-0.7, 0.7),
                  (0, -1), (0, 1)]

    def draw_labels(self, placed):
        """Place each label where it overlaps no other label or aircraft symbol.
        Labels may move further out (with a leader line) or, if there is no room
        at all, are left off."""
        placed = list(placed)
        sym = 9 * self.ui
        obstacles = [pygame.Rect(x - sym, y - sym, 2 * sym, 2 * sym) for _, x, y, _, _ in placed]
        obstacles += self.static_obstacles
        line_h = self.f_tiny.get_linesize() - 2
        pad = 2
        gap = 10 * self.ui
        bounds = self.radar_rect
        positions = {}
        for v, x, y, col, stale in placed:
            ac = v.ac
            name = self.text(ac.callsign or ac.hex, self.f_tiny, DIM if stale else TEXT)
            alt = "GND" if ac.on_ground else (
                f"{round(ac.altitude / 100):03d}" if ac.altitude is not None else "")
            alt_s = self.text(alt, self.f_tiny, col)
            w = max(name.get_width(), alt_s.get_width()) + 2 * pad
            h = 2 * line_h + 2 * pad

            prev = self.label_slots.get(ac.hex)
            slots = [(m, d) for m in (1, 2.2, 3.5, 5) for d in range(len(self.LABEL_DIRS))]
            if prev in slots:
                slots.remove(prev)
                slots.insert(0, prev)
            chosen = None
            for slot in slots:
                rect = self._label_rect(x, y, w, h, gap * slot[0], self.LABEL_DIRS[slot[1]])
                if bounds.contains(rect) and rect.collidelist(obstacles) < 0:
                    chosen = slot
                    break
            if chosen is None:
                continue
            positions[ac.hex] = chosen
            obstacles.append(rect)
            if chosen[0] > 1:  # leader line from aircraft to nearest point of label
                lx = min(max(x, rect.left), rect.right)
                ly = min(max(y, rect.top), rect.bottom)
                pygame.draw.line(self.screen, FAINT, (x, y), (lx, ly), 1)
            backing = pygame.Surface(rect.size, pygame.SRCALPHA)
            backing.fill((*RADAR_BG, 190))
            self.screen.blit(backing, rect.topleft)
            self.screen.blit(name, (rect.x + pad, rect.y + pad))
            self.screen.blit(alt_s, (rect.x + pad, rect.y + pad + line_h))
        self.label_slots = positions

    @staticmethod
    def _label_rect(x, y, w, h, dist, direction):
        dx, dy = direction
        # Anchor the label's near edge/corner at the offset point
        cx, cy = x + dx * dist, y + dy * dist
        left = cx if dx > 0 else cx - w if dx < 0 else cx - w / 2
        top = cy if dy > 0 else cy - h if dy < 0 else cy - h / 2
        return pygame.Rect(round(left), round(top), round(w), round(h))

    def draw_plane(self, x, y, track, col):
        s = 9 * self.ui
        if track is None:
            pygame.draw.circle(self.screen, col, (x, y), s * 0.5)
            return
        a = math.radians(track)
        def pt(ang, dist):
            return (x + math.sin(a + ang) * dist, y - math.cos(a + ang) * dist)
        poly = [pt(0, s), pt(2.5, s * 0.8), pt(math.pi, s * 0.35), pt(-2.5, s * 0.8)]
        pygame.draw.polygon(self.screen, col, poly)

    def draw_panel(self, views, total, now):
        p = self.panel_rect
        pygame.draw.rect(self.screen, PANEL_BG, p)
        pad = int(14 * self.ui)
        x, y, w = p.x + pad, p.y + pad, p.w - 2 * pad

        # Header: clock, counts, feed status
        self.blit_text(time.strftime("%H:%M:%S"), self.f_large, TEXT, (x, y))
        rate = 0.0
        if len(self.msg_hist) > 1:
            (t0, c0), (t1, c1) = self.msg_hist[0], self.msg_hist[-1]
            rate = (c1 - c0) / max(t1 - t0, 0.001)
        if self.store.connected:
            status, col = f"Feed OK  {rate:.0f} msg/s", GOOD
        else:
            status, col = (f"Feed DOWN: {self.store.feed_error}" if self.store.feed_error
                           else "Connecting…"), BAD
        self.blit_text(status, self.f_small, col, (x + w, y), max_w=w * 0.6, align="right")
        self.blit_text(f"{len(views)} tracked  /  {total} heard", self.f_small, DIM,
                       (x + w, y + self.f_small.get_linesize()), align="right")
        y += self.f_large.get_linesize() + pad // 2

        if views:
            y = self.draw_featured(views[0], x, y, w) + pad
        else:
            self.blit_text("No aircraft with position yet…", self.f_med, DIM, (x, y + pad))
            return

        # List header
        cols = self.list_columns(x, w)
        hdr = [("Flight", cols[0]), ("Type", cols[1]), ("Altitude", cols[2]),
               ("Speed", cols[3])]
        for label, cx in hdr:
            self.blit_text(label, self.f_tiny, FAINT, (cx, y))
        self.blit_text("Distance", self.f_tiny, FAINT, (x + w, y), align="right")
        y += self.f_tiny.get_linesize() + 2
        pygame.draw.line(self.screen, FAINT, (x, y), (x + w, y), 1)
        y += 4

        row_h = self.f_med.get_linesize() + self.f_small.get_linesize() + int(6 * self.ui)
        footer_h = self.f_tiny.get_linesize() + 6
        for v in views[1:]:
            if y + row_h > p.bottom - footer_h:
                break
            self.draw_row(v, cols, x, y, w)
            y += row_h
        self.blit_text("+/- zoom   F fullscreen   S screenshot   Q quit", self.f_tiny, FAINT,
                       (x, p.bottom - footer_h))

    def list_columns(self, x, w):
        return [x + 10 * self.ui, x + w * 0.30, x + w * 0.47, x + w * 0.67]

    def draw_row(self, v, cols, x, y, w):
        ac, info = v.ac, v.info or {}
        col = altitude_color(ac.altitude, ac.on_ground)
        line2 = y + self.f_med.get_linesize()
        h = line2 + self.f_small.get_linesize() - y
        pygame.draw.rect(self.screen, col, (x, y + 3, 4 * self.ui, h - 6))
        self.blit_text(ac.callsign or ac.hex, self.f_med, TEXT, (cols[0], y),
                       max_w=cols[1] - cols[0] - 4)
        self.blit_text(info.get("icao_type") or "", self.f_med, TEXT, (cols[1], y),
                       max_w=cols[2] - cols[1] - 4)
        self.blit_text(self.fmt_alt(ac), self.f_med, col, (cols[2], y))
        self.blit_text(f"{ac.speed:.0f} kt" if ac.speed is not None else "", self.f_med, TEXT,
                       (cols[3], y))
        self.blit_text(self.fmt_dist(v.dist_km), self.f_med, TEXT, (x + w, y), align="right")
        # Second line: route / airline, registration, vertical rate, bearing
        sub = self.route_text(v) or (v.route or {}).get("airline") or info.get("registered_owner") or ""
        self.blit_text(sub, self.f_small, DIM, (cols[0], line2), max_w=cols[2] - cols[0] - 4)
        vr = ""
        if ac.vrate and abs(ac.vrate) >= 200 and not ac.on_ground:
            vr = f"{'climb' if ac.vrate > 0 else 'desc'} {abs(ac.vrate):,} fpm"
        self.blit_text(vr, self.f_small, DIM, (cols[2], line2), max_w=cols[3] - cols[2] + w * 0.15)
        self.blit_text(compass(v.bearing), self.f_small, DIM, (x + w, line2), align="right")

    def draw_featured(self, v, x, y, w):
        ac, info = v.ac, v.info or {}
        overhead = v.dist_km <= self.overhead_km
        pad = int(12 * self.ui)
        lines_h = (self.f_small.get_linesize() + self.f_huge.get_linesize()
                   + self.f_med.get_linesize() * 2 + self.f_large.get_linesize()
                   + self.f_small.get_linesize() + pad * 2 + 6)
        box = pygame.Rect(x, y, w, lines_h)
        pygame.draw.rect(self.screen, (40, 34, 8) if overhead else (20, 28, 44), box, border_radius=8)
        pygame.draw.rect(self.screen, ACCENT if overhead else FAINT, box, 2, border_radius=8)
        ix, iy, iw = x + pad, y + pad, w - 2 * pad

        title = "OVERHEAD" if overhead else "NEAREST"
        self.blit_text(title, self.f_small, ACCENT if overhead else DIM, (ix, iy))
        self.blit_text(f"{self.fmt_dist(v.dist_km)} {compass(v.bearing)}", self.f_small, TEXT,
                       (ix + iw, iy), align="right")
        iy += self.f_small.get_linesize()

        cs_w = self.blit_text(ac.callsign or ac.hex, self.f_huge, TEXT, (ix, iy), max_w=iw * 0.5)
        airline = (v.route or {}).get("airline") or info.get("registered_owner") or ""
        self.blit_text(airline, self.f_med, DIM,
                       (ix + cs_w + pad, iy + self.f_huge.get_ascent() - self.f_med.get_ascent()),
                       max_w=iw - cs_w - pad)
        iy += self.f_huge.get_linesize()

        route = self.route_text(v, long=True)
        self.blit_text(route or "Route unknown", self.f_med, TEXT if route else DIM, (ix, iy), max_w=iw)
        iy += self.f_med.get_linesize()

        kind = " ".join(filter(None, [info.get("manufacturer"), info.get("type")]))
        reg = info.get("registration")
        desc = "  ·  ".join(filter(None, [kind, reg])) or f"ICAO {ac.hex}"
        self.blit_text(desc, self.f_med, DIM, (ix, iy), max_w=iw)
        iy += self.f_med.get_linesize() + 6

        col = altitude_color(ac.altitude, ac.on_ground)
        stats = [
            (self.fmt_alt(ac), col),
            (f"{ac.speed:.0f} kt" if ac.speed is not None else "--- kt", TEXT),
            (f"hdg {ac.track:03.0f}°" if ac.track is not None else "hdg ---", TEXT),
        ]
        sx = ix
        for s, c in stats:
            self.blit_text(s, self.f_large, c, (sx, iy))
            sx += iw / 3
        iy += self.f_large.get_linesize()

        bits = []
        if ac.altitude is not None and not ac.on_ground:
            el = elevation_deg(v.dist_km, ac.altitude)
            bits.append(f"Look {compass(v.bearing)}, {el:.0f}° above horizon" if el < 80
                        else "Look straight up!")
        if ac.vrate and abs(ac.vrate) >= 200:
            bits.append(f"{'climbing' if ac.vrate > 0 else 'descending'} {abs(ac.vrate):,} fpm")
        if ac.squawk:
            bits.append(f"sqk {ac.squawk}")
        self.blit_text("   ".join(bits), self.f_small, ACCENT if overhead else DIM, (ix, iy), max_w=iw)
        return box.bottom

    # -- input ---------------------------------------------------------------

    def handle_event(self, e):
        """Returns False when the app should quit."""
        if e.type == pygame.QUIT:
            return False
        if e.type == pygame.VIDEORESIZE and not self.fullscreen:
            self.screen = pygame.display.set_mode(e.size, pygame.RESIZABLE)
            self._layout()
        elif e.type == pygame.MOUSEWHEEL:
            self.zoom(-e.y)
        elif e.type == pygame.KEYDOWN:
            if e.key in (pygame.K_q, pygame.K_ESCAPE):
                return False
            if e.key in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
                self.zoom(-1)
            elif e.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                self.zoom(1)
            elif e.key == pygame.K_f:
                self.fullscreen = not self.fullscreen
                self._set_mode()
            elif e.key == pygame.K_s:
                path = os.path.join(HERE, time.strftime("screenshot-%Y%m%d-%H%M%S.png"))
                pygame.image.save(self.screen, path)
        return True

    def zoom(self, direction):
        """direction < 0 zooms in (smaller range), > 0 zooms out."""
        steps = sorted(set(RANGE_STEPS + [self.range]))
        i = steps.index(self.range) + (1 if direction > 0 else -1)
        self.range = steps[max(0, min(len(steps) - 1, i))]
        self._build_radar_bg()

    def run(self):
        clock = pygame.time.Clock()
        try:
            while True:
                for e in pygame.event.get():
                    if not self.handle_event(e):
                        return
                self.draw(*self.store.snapshot())
                clock.tick(self.cfg["fps"])
        finally:
            pygame.quit()


# ---------------------------------------------------------------------------


EXAMPLE_CONFIG = os.path.join(HERE, "config.example.json")


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if path and not os.path.exists(path) and os.path.exists(EXAMPLE_CONFIG):
        print(f"{path} not found, using config.example.json "
              "(copy it to config.json and set your own location)")
        path = EXAMPLE_CONFIG
    if path and os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    return cfg


def main():
    ap = argparse.ArgumentParser(description="Live display of aircraft overhead from dump1090")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--host", help="dump1090 host (overrides config)")
    ap.add_argument("--port", type=int, help="dump1090 SBS port (overrides config)")
    ap.add_argument("--windowed", action="store_true", help="run in a window, not fullscreen")
    ap.add_argument("--no-lookups", action="store_true", help="disable online lookups")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.host:
        cfg["feed_host"] = args.host
    if args.port:
        cfg["feed_port"] = args.port
    if args.windowed:
        cfg["fullscreen"] = False
    if args.no_lookups:
        cfg["online_lookups"] = False

    store = AircraftStore(cfg["stale_seconds"], cfg["trail_seconds"])
    SBSFeed(cfg["feed_host"], cfg["feed_port"], store).start()
    cache_path = cfg["cache_file"]
    if not os.path.isabs(cache_path):
        cache_path = os.path.join(HERE, cache_path)
    lookup = LookupService(cache_path, cfg["online_lookups"])
    lookup.start()
    try:
        Display(cfg, store, lookup).run()
    finally:
        lookup.save()


if __name__ == "__main__":
    main()
