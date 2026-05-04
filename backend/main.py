"""
NEXUS Satellite Tracking — FastAPI Backend
==========================================
Fetches real TLE data from CelesTrak every 6 hours.
Propagates positions with full SGP4 via the `sgp4` library.
Serves a clean JSON API consumed by the frontend.

Free deployment: Railway · Render · Fly.io (see README)
"""

import asyncio
import logging
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sgp4.api import Satrec

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
log = logging.getLogger("nexus")

# ── Config from env ───────────────────────────────────────────────────────────
CORS_ORIGINS: List[str] = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:3000,http://localhost:5500,http://127.0.0.1:5500,https://*.vercel.app",
).split(",")

TLE_REFRESH_HOURS: int = int(os.getenv("TLE_REFRESH_HOURS", "6"))
MAX_SATS_PER_GROUP: int = int(os.getenv("MAX_SATS_PER_GROUP", "500"))

# ── CelesTrak TLE sources ─────────────────────────────────────────────────────
TLE_SOURCES: Dict[str, str] = {
    "starlink": "https://celestrak.org/NORAD/elements/gp.php?GROUP=starlink&FORMAT=tle",
    "oneweb":   "https://celestrak.org/NORAD/elements/gp.php?GROUP=oneweb&FORMAT=tle",
    "gps":      "https://celestrak.org/NORAD/elements/gp.php?GROUP=gps-ops&FORMAT=tle",
    "weather":  "https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle",
    "stations": "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=tle",
    "iridium":  "https://celestrak.org/NORAD/elements/gp.php?GROUP=iridium-NEXT&FORMAT=tle",
}

# ── In-memory satellite registry ─────────────────────────────────────────────
# norad_id → (Satrec object, display name, group key)
Registry = Dict[int, Tuple[Satrec, str, str]]
_registry: Registry = {}
_last_refresh: Optional[datetime] = None

# ── Tracks the Unix timestamp of the previous /positions response ─────────────
_prev_positions_timestamp: Optional[float] = None

# ── WGS-84 constants ──────────────────────────────────────────────────────────
_WGS84_A  = 6378.137             # km — semi-major axis
_WGS84_F  = 1.0 / 298.257223563
_WGS84_E2 = 2.0 * _WGS84_F - _WGS84_F ** 2   # first eccentricity squared


# ══════════════════════════════════════════════════════════════════════════════
# COORDINATE MATH
# ══════════════════════════════════════════════════════════════════════════════

def _jd_from_utc(dt: datetime) -> Tuple[float, float]:
    """
    UTC datetime → (jd, fr) with the split convention required by sgp4.

    sgp4.sgp4(jd, fr) needs  jd + fr == true Julian Date.  The split is kept
    at the nearest 0.5-day boundary so `fr` retains maximum floating-point
    precision for the sub-day portion.

    Bug in the previous code: it baked the full JD (including hours) into `jd`
    and then put h/24 into `fr` again — double-counting the hours and shifting
    the propagation epoch by several hours.
    """
    y, m, d = dt.year, dt.month, dt.day
    if m <= 2:          # Meeus ch.7 — shift January/February into prior year
        y -= 1
        m += 12
    A = int(y / 100)
    B = 2 - A + int(A / 4)
    # Julian Date at 0h UT for this calendar day
    jd0 = math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + B - 1524.5

    # Sub-day fraction with microsecond precision
    day_frac = (
        dt.hour / 24.0
        + dt.minute / 1440.0
        + (dt.second + dt.microsecond / 1e6) / 86400.0
    )

    # Anchor jd at the .5 boundary; fr carries the remaining fraction.
    jd = math.floor(jd0) + 0.5
    fr = (jd0 - jd) + day_frac
    return jd, fr


def _gst(jd: float, fr: float) -> float:
    """
    Greenwich Mean Sidereal Time (radians) from a (jd, fr) pair.

    Bug in the previous code: it called _jd_from_utc and then discarded fr,
    computing GMST from the integer Julian day alone.  GMST advances at
    ~360.985 deg/day (~15 deg/hour), so dropping fr introduced longitude errors
    of many tens of degrees depending on the time of day.
    """
    jd_full = jd + fr
    T = (jd_full - 2_451_545.0) / 36_525.0
    # IAU 1982 GMST polynomial (degrees)
    gmst_deg = (
        280.46061837
        + 360.98564736629 * (jd_full - 2_451_545.0)
        + T * T * 0.000387933
        - T * T * T / 38_710_000.0
    ) % 360.0
    return math.radians(gmst_deg)


def _eci_to_geodetic(x: float, y: float, z: float, gst: float) -> Tuple[float, float, float]:
    """
    ECI position (km) + GMST (rad) → (latitude°, longitude°, altitude km).

    Uses Bowring's iterative method for geodetic latitude (5 iterations give
    sub-millimetre accuracy everywhere on Earth).

    Bug in the previous altitude formula: the conditional
        p/cos(φ) − N  (equator branch)  vs  z/sin(φ) − N(1−e²)  (pole branch)
    has a cos(φ) → 0 singularity near the poles, and the polar expression
    carries an incorrect factor.  Replaced with the magnitude-difference form
    which is numerically stable at all latitudes and azimuths.
    """
    # ── ECI → ECEF: rotate around Z by GMST ──────────────────────────────────
    cos_g = math.cos(gst)
    sin_g = math.sin(gst)
    xe =  x * cos_g + y * sin_g
    ye = -x * sin_g + y * cos_g
    ze =  z

    # Longitude
    lon = math.atan2(ye, xe)

    # Equatorial distance
    p = math.sqrt(xe * xe + ye * ye)

    # ── Bowring iterative geodetic latitude ───────────────────────────────────
    lat = math.atan2(ze, p * (1.0 - _WGS84_E2))
    for _ in range(5):
        sin_lat = math.sin(lat)
        N = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * sin_lat * sin_lat)
        lat = math.atan2(ze + _WGS84_E2 * N * sin_lat, p)

    # ── Altitude: geocentric distance minus ellipsoid radius along normal ─────
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    N = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * sin_lat * sin_lat)
    # Distance from geocentre to the ellipsoid surface at this geodetic (lat, lon)
    r_surface = math.sqrt(
        (N * cos_lat) ** 2
        + (N * (1.0 - _WGS84_E2) * sin_lat) ** 2
    )
    r_sat = math.sqrt(xe * xe + ye * ye + ze * ze)
    alt = r_sat - r_surface

    return math.degrees(lat), math.degrees(lon), alt


# ══════════════════════════════════════════════════════════════════════════════
# TLE FETCHING & PARSING
# ══════════════════════════════════════════════════════════════════════════════

def _parse_tle_text(raw: str, group: str) -> List[Tuple[str, str, str, str]]:
    """Parse CelesTrak 3-line TLE format → list of (name, line1, line2, group)."""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    records = []
    i = 0
    while i < len(lines) - 2:
        name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
        if l1.startswith("1 ") and l2.startswith("2 "):
            records.append((name.lstrip("0 ").strip(), l1, l2, group))
            i += 3
        else:
            i += 1
    return records


async def _fetch_group(client: httpx.AsyncClient, group: str, url: str) -> int:
    """Fetch one constellation's TLEs. Returns number of sats loaded."""
    try:
        r = await client.get(url)
        r.raise_for_status()
        count = 0
        for name, l1, l2, g in _parse_tle_text(r.text, group):
            try:
                sat = Satrec.twoline2rv(l1, l2)
                norad = int(l1[2:7])
                _registry[norad] = (sat, name, g)
                count += 1
                if count >= MAX_SATS_PER_GROUP:
                    break
            except Exception:
                pass
        log.info("  ✓ %s: %d satellites", group, count)
        return count
    except Exception as exc:
        log.warning("  ✗ %s: %s", group, exc)
        return 0


async def refresh_tles() -> None:
    """Fetch all TLE groups concurrently from CelesTrak."""
    global _last_refresh
    log.info("TLE refresh starting — %d groups", len(TLE_SOURCES))

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        tasks = [_fetch_group(client, grp, url) for grp, url in TLE_SOURCES.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    total = sum(r for r in results if isinstance(r, int))
    _last_refresh = datetime.now(timezone.utc)
    log.info("TLE refresh complete — %d total satellites in registry", total)


# ══════════════════════════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ══════════════════════════════════════════════════════════════════════════════

class SatellitePosition(BaseModel):
    id:        int
    name:      str
    group:     str
    lat:       float
    lng:       float
    alt:       float    # km above MSL
    vel:       float    # km/s scalar
    vx:        float    # km/s ECI x-component
    vy:        float    # km/s ECI y-component
    vz:        float    # km/s ECI z-component
    timestamp: float    # UNIX time seconds (full float precision)
    dt:        float    # seconds since previous /positions response (0 on first call)


class MetaResponse(BaseModel):
    satellites:   int
    last_refresh: Optional[str]
    groups:       Dict[str, int]
    server_time:  str


# ══════════════════════════════════════════════════════════════════════════════
# APP LIFESPAN
# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    await refresh_tles()

    async def _bg_refresh():
        while True:
            await asyncio.sleep(TLE_REFRESH_HOURS * 3600)
            await refresh_tles()

    task = asyncio.create_task(_bg_refresh())
    log.info("🛰  NEXUS API ready — %d satellites loaded", len(_registry))
    yield
    task.cancel()


# ══════════════════════════════════════════════════════════════════════════════
# FASTAPI APP
# ══════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="NEXUS Satellite Tracking API",
    description="Real-time SGP4-propagated satellite positions from CelesTrak TLEs.",
    version="2.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["system"])
async def health():
    """Uptime check — used by Railway/Render health probes."""
    return {
        "status": "ok",
        "satellites": len(_registry),
        "last_refresh": _last_refresh.isoformat() if _last_refresh else None,
    }


@app.get("/positions", response_model=List[SatellitePosition], tags=["satellites"])
async def get_positions(
    group: Optional[str] = Query(None, description="Filter by constellation group"),
    limit: int           = Query(2000, ge=1, le=10000),
):
    """
    Propagate all registered satellites to now (UTC) and return positions.

    A single timestamp is captured once before the loop so every satellite in
    the response is consistent with the same instant in time.  Full-precision
    floats are returned and ECI velocity components are included so the
    frontend can dead-reckon positions between poll intervals.
    """
    global _prev_positions_timestamp

    # ── One precise timestamp for the whole response ──────────────────────────
    now = datetime.now(timezone.utc)
    timestamp: float = now.timestamp()
    dt_hint: float = (
        0.0 if _prev_positions_timestamp is None
        else timestamp - _prev_positions_timestamp
    )
    _prev_positions_timestamp = timestamp

    # ── Precompute JD + GST once — never recomputed inside the loop ───────────
    jd, fr = _jd_from_utc(now)
    gst    = _gst(jd, fr)

    results: List[SatellitePosition] = []

    for norad, (sat, name, grp) in list(_registry.items()):
        if group and grp != group:
            continue

        err, r, v = sat.sgp4(jd, fr)
        if err != 0:
            continue

        try:
            lat, lng, alt = _eci_to_geodetic(r[0], r[1], r[2], gst)
            vel = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
        except Exception:
            continue

        results.append(SatellitePosition(
            id=norad,
            name=name,
            group=grp,
            lat=lat,
            lng=lng,
            alt=alt,
            vel=vel,
            vx=v[0],
            vy=v[1],
            vz=v[2],
            timestamp=timestamp,
            dt=dt_hint,
        ))

        if len(results) >= limit:
            break

    return results


@app.get("/meta", response_model=MetaResponse, tags=["system"])
async def get_meta():
    """System metadata — total satellites, last refresh time, group counts."""
    from collections import Counter
    counts = Counter(grp for _, _, grp in _registry.values())
    return MetaResponse(
        satellites=len(_registry),
        last_refresh=_last_refresh.isoformat() if _last_refresh else None,
        groups=dict(counts),
        server_time=datetime.now(timezone.utc).isoformat(),
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
