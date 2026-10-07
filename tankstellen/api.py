"""Minimal client for the Tankerkoenig free API (list.php)."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

BASE_URL = "https://creativecommons.tankerkoenig.de/json"
MAX_RADIUS_KM = 25.0
FUELS = ("diesel", "e5", "e10")


class ApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class StationPrices:
    id: str
    name: str
    brand: str
    street: str
    house_number: str
    post_code: str
    place: str
    lat: float
    lng: float
    is_open: bool
    # fuel -> price in EUR, or None if the fuel is not offered / no valid price
    prices: dict[str, float | None]


def _price(value) -> float | None:
    # API returns false/null when a fuel is not offered.
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def parse_stations(payload: dict) -> list[StationPrices]:
    if not payload.get("ok"):
        raise ApiError(payload.get("message", "unknown API error"))
    result = []
    for s in payload.get("stations", []):
        result.append(
            StationPrices(
                id=s["id"],
                name=s.get("name") or "",
                brand=s.get("brand") or "",
                street=s.get("street") or "",
                house_number=str(s.get("houseNumber") or "").strip(),
                post_code=str(s.get("postCode") or ""),
                place=s.get("place") or "",
                lat=float(s["lat"]),
                lng=float(s["lng"]),
                is_open=bool(s.get("isOpen")),
                prices={f: _price(s.get(f)) for f in FUELS},
            )
        )
    return result


def fetch_radius(
    api_key: str, lat: float, lng: float, radius_km: float, timeout: float = 30.0
) -> list[StationPrices]:
    if not 0 < radius_km <= MAX_RADIUS_KM:
        raise ValueError(f"radius must be in (0, {MAX_RADIUS_KM}] km")
    query = urllib.parse.urlencode(
        {
            "lat": lat,
            "lng": lng,
            "rad": radius_km,
            "type": "all",  # returns diesel, e5 and e10 in one call
            "apikey": api_key,
        }
    )
    req = urllib.request.Request(
        f"{BASE_URL}/list.php?{query}",
        headers={"User-Agent": "tankstellen-12uhr-analyzer/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        # The API sends a JSON body with a message on errors as well.
        try:
            msg = json.load(e).get("message", e.reason)
        except Exception:
            msg = e.reason
        raise ApiError(f"HTTP {e.code}: {msg}") from e
    except urllib.error.URLError as e:
        raise ApiError(f"network error: {e.reason}") from e
    return parse_stations(payload)
