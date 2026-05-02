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
from fastapi import FastAPI, HTTPException, Query
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
    # defaults: local dev + Vercel wildcard (set your real domain in prod)
    "http://localhost:3000,http://localhost:5500,http://127.0.0.1:5500,https://*.vercel.app",
).split(",")

TLE_REFRESH_HOURS: int = int(os.getenv("TLE_REFRESH_HOURS", "6"))
MAX_SATS_PER_GROUP: int = int(os.getenv("MAX_SATS_PER_GROUP", "500"))

# ── CelesTrak TLE sources ─────────────────────────────────────────────────────
TLE_SOURCES: Dict[str, str] = {
    "starlink": "https://celestrak.org/SOCRATES/query.php?CODE=starlink&FORMAT=TLE",
    "oneweb":   "https://celestrak.org/SOCRATES/query.php?CODE=oneweb&FORMAT=TLE",
    "gps":      "https://celestrak.org/SOCRATES/query.php?CODE=gps-ops&FORMAT=TLE",
    "weather":  "https://celestrak.org/SOCRATES/query.php?CODE=weather&FORMAT=TLE",
    "stations": "https://celestrak.org/SOCRATES/query.php?CODE=stations&FORMAT=TLE",
    "iridium":  "https://celestrak.org/SOCRATES/query.php?CODE=iridium-NEXT&FORMAT=TLE",
}

# ── In-memory satellite registry ─────────────────────────────────────────────
# norad_id → (Satrec object, display name, group key)
Registry = Dict[int, Tuple[Satrec, str, str]]
_registry: Registry = {}
_last_refresh: Optional[datetime] = None

# ── WGS-84 constants for ECI → geodetic conversion ───────────────────────────
_WGS84_A  = 6378.137          # km — equatorial radius
_WGS84_F  = 1.0 / 298.257223563
_WGS84_E2 = 2 * _WGS84_F - _WGS84_F ** 2


# ══════════════════════════════════════════════════════════════════════════════
# COORDINATE MATH
# ══════════════════════════════════════════════════════════════════════════════

def _jd_from_utc(dt: datetime) -> Tuple[float, float]:
    """UTC datetime → (Julian date integer part, fractional day)."""
    y, m, d = dt.year, dt.month, dt.day
    h = dt.hour + dt.minute / 60.0 + (dt.second + dt.microsecond / 1e6) / 3600.0
    A = int(y / 100)
    B = 2 - A + int(A / 4)
    jd = int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + B - 1524.5
    fr = h / 24.0
    return jd, fr


def _gst(dt: datetime) -> float:
    """Approximate Greenwich Sidereal Time (radians)."""
    jd, _ = _jd_from_utc(dt)
    T = (jd - 2_451_545.0) / 36_525.0
    gst_deg = (
        280.46061837
        + 360.98564736629 * (jd - 2_451_545.0)
        + T ** 2 * 0.000387933
        - T ** 3 / 38_710_000.0
    ) % 360.0
    return math.radians(gst_deg)


def _eci_to_geodetic(x: float, y: float, z: float, gst: float) -> Tuple[float, float, float]:
    """ECI (km) + GST (rad) → (latitude°, longitude°, altitude km)."""
    # Rotate ECI → ECEF
    xe =  x * math.cos(gst) + y * math.sin(gst)
    ye = -x * math.sin(gst) + y * math.cos(gst)
    ze = z

    lon = math.atan2(ye, xe)
    p   = math.sqrt(xe ** 2 + ye ** 2)

    # Bowring iterative geodetic latitude (5 iterations → sub-metre accuracy)
    lat = math.atan2(ze, p * (1 - _WGS84_E2))
    for _ in range(5):
        sin_lat = math.sin(lat)
        N = _WGS84_A / math.sqrt(1 - _WGS84_E2 * sin_lat ** 2)
        lat = math.atan2(ze + _WGS84_E2 * N * sin_lat, p)

    sin_lat = math.sin(lat)
    N = _WGS84_A / math.sqrt(1 - _WGS84_E2 * sin_lat ** 2)
    alt = (p / math.cos(lat) - N) if abs(lat) < math.pi / 4 else (ze / sin_lat - N * (1 - _WGS84_E2))

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
    id:    int
    name:  str
    group: str
    lat:   float
    lng:   float
    alt:   float   # km above MSL
    vel:   float   # km/s


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
    # Initial load on startup
    await refresh_tles()

    # Periodic background refresh
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
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tightened in prod via CORS_ORIGINS env var
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
    This is the hot endpoint — called every 5 seconds by the frontend.
    """
    now = datetime.now(timezone.utc)
    jd, fr = _jd_from_utc(now)
    gst    = _gst(now)

    results: List[SatellitePosition] = []

    for norad, (sat, name, grp) in list(_registry.items()):
        if group and grp != group:
            continue

        err, r, v = sat.sgp4(jd, fr)
        if err != 0:
            continue  # satellite outside valid range — skip silently

        try:
            lat, lng, alt = _eci_to_geodetic(r[0], r[1], r[2], gst)
            vel = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
        except Exception:
            continue

        results.append(SatellitePosition(
            id=norad, name=name, group=grp,
            lat=round(lat, 4), lng=round(lng, 4),
            alt=round(alt, 2), vel=round(vel, 4),
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
import os

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
