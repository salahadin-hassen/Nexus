"""
NEXUS Satellite Tracking — FastAPI Backend
==========================================
Fetches real TLE data from CelesTrak every N hours.
Propagates positions with full SGP4 via the `sgp4` library.
Serves a clean JSON API consumed by the frontend.
"""

import asyncio
import logging
import math
import os
from collections import Counter
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

# ── Config from environment ───────────────────────────────────────────────────
CORS_ORIGINS: List[str] = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:3000,http://localhost:5500,http://127.0.0.1:5500,https://*.vercel.app",
).split(",")

TLE_REFRESH_HOURS: int = int(os.getenv("TLE_REFRESH_HOURS", "6"))
MAX_SATS_PER_GROUP: int = int(os.getenv("MAX_SATS_PER_GROUP", "500"))

# ── CelesTrak TLE sources ─────────────────────────────────────────────────────
TLE_SOURCES: Dict[str, str] = {
    "starlink": "https://celestrak.org/NORAD/elements/gp.php?GROUP=starlink&FORMAT=tle",
    "oneweb": "https://celestrak.org/NORAD/elements/gp.php?GROUP=oneweb&FORMAT=tle",
    "gps": "https://celestrak.org/NORAD/elements/gp.php?GROUP=gps-ops&FORMAT=tle",
    "weather": "https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle",
    "stations": "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=tle",
    "iridium": "https://celestrak.org/NORAD/elements/gp.php?GROUP=iridium-NEXT&FORMAT=tle",
}

# ── WGS‑84 constants for ECI → geodetic conversion ───────────────────────────
_WGS84_A = 6378.137          # km — equatorial radius
_WGS84_F = 1.0 / 298.257223563
_WGS84_E2 = 2 * _WGS84_F - _WGS84_F**2  # squared eccentricity

# ── In‑memory satellite registry (atomically replaced by refresher) ──────────
# norad_id → (Satrec object, display name, group key)
Registry = Dict[int, Tuple[Satrec, str, str]]
_registry: Registry = {}
_last_refresh: Optional[datetime] = None


# ══════════════════════════════════════════════════════════════════════════════
#  COORDINATE MATH – all functions work on UTC datetime objects
# ══════════════════════════════════════════════════════════════════════════════

def _jd_from_utc(dt: datetime) -> Tuple[float, float]:
    """Return (Julian date integer part at 0h UT, fractional day)."""
    y, m, d = dt.year, dt.month, dt.day
    h = dt.hour + dt.minute / 60.0 + (dt.second + dt.microsecond / 1e6) / 3600.0
    A = int(y / 100)
    B = 2 - A + int(A / 4)
    jd = int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + B - 1524.5
    fr = h / 24.0
    return jd, fr


def _gmst_rad(dt: datetime) -> float:
    """
    Greenwich Mean Sidereal Time (radians) for the exact UTC instant.
    Valid for any date (IAU 1982/2000 expression via full Julian date).
    """
    jd_int, fr = _jd_from_utc(dt)
    jd_full = jd_int + fr                     # Julian date with fraction
    # GMST in degrees (Meeus‑style, high accuracy)
    d = jd_full - 2451545.0                   # days since J2000.0
    gmst_deg = (280.46061837 + 360.98564736629 * d) % 360.0
    return math.radians(gmst_deg)


def _eci_to_geodetic(
    x: float, y: float, z: float, gmst: float
) -> Tuple[float, float, float]:
    """
    Convert ECI (km) to geodetic latitude (°), longitude (°), altitude (km).
    Uses WGS‑84 constants and a Bowring iterative solution.
    """
    # Rotate ECI → ECEF
    xe = x * math.cos(gmst) + y * math.sin(gmst)
    ye = -x * math.sin(gmst) + y * math.cos(gmst)
    ze = z

    lon = math.atan2(ye, xe)
    p = math.hypot(xe, ye)

    # Bowring iteration (5 steps => sub‑metre accuracy)
    lat = math.atan2(ze, p * (1 - _WGS84_E2))
    for _ in range(5):
        sin_lat = math.sin(lat)
        N = _WGS84_A / math.sqrt(1 - _WGS84_E2 * sin_lat**2)
        lat = math.atan2(ze + _WGS84_E2 * N * sin_lat, p)

    sin_lat = math.sin(lat)
    N = _WGS84_A / math.sqrt(1 - _WGS84_E2 * sin_lat**2)
    if abs(lat) < math.pi / 4:
        alt = p / math.cos(lat) - N
    else:
        alt = ze / sin_lat - N * (1 - _WGS84_E2)

    return math.degrees(lat), math.degrees(lon), alt


# ══════════════════════════════════════════════════════════════════════════════
#  TLE FETCHING & PARSING (thread‑safe – works on a supplied registry dict)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_tle_text(raw: str, group: str) -> List[Tuple[str, str, str, str]]:
    """Parse CelesTrak 3‑line TLE format → list of (name, line1, line2, group)."""
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


async def _fetch_group(
    client: httpx.AsyncClient, group: str, url: str, registry: Registry
) -> int:
    """Fetch one constellation's TLEs and fill the given registry dict."""
    try:
        r = await client.get(url)
        r.raise_for_status()
        count = 0
        for name, l1, l2, g in _parse_tle_text(r.text, group):
            if count >= MAX_SATS_PER_GROUP:
                break
            try:
                sat = Satrec.twoline2rv(l1, l2)
                norad = int(l1[2:7])
                registry[norad] = (sat, name, g)
                count += 1
            except Exception:
                pass
        log.info("  ✓ %s: %d satellites", group, count)
        return count
    except Exception as exc:
        log.warning("  ✗ %s: %s", group, exc)
        return 0


async def refresh_tles() -> None:
    """
    Fetch all TLE groups concurrently and atomically replace the global registry.
    This avoids any concurrent‑modification issues with the API reader.
    """
    global _registry, _last_refresh
    new_registry: Registry = {}

    log.info("TLE refresh starting — %d groups", len(TLE_SOURCES))

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        tasks = [
            _fetch_group(client, grp, url, new_registry)
            for grp, url in TLE_SOURCES.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    total = sum(r for r in results if isinstance(r, int))
    # Atomic replacement – readers see a consistent snapshot
    _registry = new_registry
    _last_refresh = datetime.now(timezone.utc)
    log.info("TLE refresh complete — %d total satellites in registry", total)


# ══════════════════════════════════════════════════════════════════════════════
#  PYDANTIC MODELS
# ══════════════════════════════════════════════════════════════════════════════

class SatellitePosition(BaseModel):
    id: int
    name: str
    group: str
    lat: float
    lng: float
    alt: float   # km above WGS‑84 ellipsoid
    vel: float   # km/s


class MetaResponse(BaseModel):
    satellites: int
    last_refresh: Optional[str]
    groups: Dict[str, int]
    server_time: str


# ══════════════════════════════════════════════════════════════════════════════
#  APP LIFESPAN
# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initial load
    await refresh_tles()

    # Background refresh task
    async def _bg_refresh():
        while True:
            await asyncio.sleep(TLE_REFRESH_HOURS * 3600)
            await refresh_tles()

    task = asyncio.create_task(_bg_refresh())
    log.info("🛰  NEXUS API ready — %d satellites loaded", len(_registry))
    yield
    task.cancel()


# ══════════════════════════════════════════════════════════════════════════════
#  FASTAPI APP
# ══════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="NEXUS Satellite Tracking API",
    description="Real‑time SGP4‑propagated satellite positions from CelesTrak TLEs.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production via CORS_ORIGINS env
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["system"])
async def health():
    """Health probe for orchestration platforms (Railway / Render)."""
    return {
        "status": "ok",
        "satellites": len(_registry),
        "last_refresh": _last_refresh.isoformat() if _last_refresh else None,
    }


@app.get("/positions", response_model=List[SatellitePosition], tags=["satellites"])
async def get_positions(
    group: Optional[str] = Query(None, description="Constellation filter"),
    limit: int = Query(2000, ge=1, le=10000),
):
    """
    Propagate all registered satellites to the current UTC time using SGP4
    and return geodetic positions. Called by the frontend every 5 seconds.
    """
    now = datetime.now(timezone.utc)
    jd_int, fr = _jd_from_utc(now)
    gmst = _gmst_rad(now)

    # Snapshot the registry – safe because background refresher replaces the dict
    registry_snapshot = _registry
    results: List[SatellitePosition] = []

    for norad, (sat, name, grp) in registry_snapshot.items():
        if group and grp != group:
            continue

        err, r, v = sat.sgp4(jd_int, fr)
        if err != 0:
            continue  # satellite outside SGP4 validity window

        lat, lng, alt = _eci_to_geodetic(r[0], r[1], r[2], gmst)
        vel = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)

        results.append(
            SatellitePosition(
                id=norad,
                name=name,
                group=grp,
                lat=round(lat, 4),
                lng=round(lng, 4),
                alt=round(alt, 2),
                vel=round(vel, 4),
            )
        )

        if len(results) >= limit:
            break

    return results


@app.get("/meta", response_model=MetaResponse, tags=["system"])
async def get_meta():
    """Return summary data: total satellites, group counts, last refresh."""
    counts = Counter(grp for _, _, grp in _registry.values())
    return MetaResponse(
        satellites=len(_registry),
        last_refresh=_last_refresh.isoformat() if _last_refresh else None,
        groups=dict(counts),
        server_time=datetime.now(timezone.utc).isoformat(),
    )


# ── Run (development) ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)