"""Street-level refinement of ground locations via OpenStreetMap Nominatim.

The association publishes a street address for every ground but no coordinates (its own
map geocodes in the browser and ships `-1` placeholders), so coordinates are derived
here. The locality comes from the gazetteer; Nominatim is used only to sharpen a known
locality to a street, and a result is rejected unless it lands near that locality.
Without that guard, free-text geocoding silently returns far-away same-named streets.

Results are cached on disk so the dashboard never needs the network at run time.
"""

import json
import logging
import math
import re
import threading
import time

import certifi
import requests

from . import config

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy requires an identifying agent and at most one request/second.
GEOCODE_USER_AGENT = "ifa-youth-scout/1.0 (local scouting dashboard)"
MIN_REQUEST_INTERVAL = 1.1
RESULTS_PER_QUERY = 5

# Rough bounding box for Israel; results outside it are discarded outright.
LAT_RANGE = (29.4, 33.45)
LON_RANGE = (34.2, 35.95)

# How far a street match may sit from its locality centre before it is treated as a
# different place. Generous enough for a long city like Jerusalem, tight enough to
# reject a same-named street in another town.
MAX_STREET_OFFSET_KM = 12.0

# Nominatim place types that represent somewhere people live, used when there is no
# gazetteer anchor to validate against.
SETTLEMENT_TYPES = {
    "city", "town", "village", "hamlet", "municipality", "suburb",
    "quarter", "neighbourhood", "locality", "isolated_dwelling", "administrative",
}

# Nominatim's structured address, in decreasing preference, for a locality label.
LOCALITY_KEYS = ("city", "town", "village", "municipality", "suburb", "county")

_HOUSE_NUMBER_RE = re.compile(r"\b\d+\b")


def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    inner = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(inner))


class Geocoder:
    """Serialised, cached Nominatim client.

    Calls are rate limited and single threaded on purpose: Nominatim is a free shared
    service and the whole job is only a few hundred lookups.
    """

    def __init__(self, cache_path=None):
        self.cache_path = cache_path or config.DATA_DIR / "geocode_cache.json"
        self.cache = self._load_cache()
        self.stats = {"cached": 0, "fetched": 0, "misses": 0, "rejected": 0}
        self._last_request = 0.0
        self._lock = threading.Lock()

    def _load_cache(self):
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("could not read geocode cache; starting empty")
            return {}

    def save(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self.cache, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def _fetch(self, query):
        with self._lock:
            wait = MIN_REQUEST_INTERVAL - (time.time() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.time()

        try:
            response = requests.get(
                NOMINATIM_URL,
                params={
                    "q": query,
                    "format": "json",
                    "limit": RESULTS_PER_QUERY,
                    "addressdetails": 1,
                },
                headers={"User-Agent": GEOCODE_USER_AGENT},
                timeout=config.REQUEST_TIMEOUT,
                verify=certifi.where(),
            )
            if response.status_code != 200:
                log.warning("geocode HTTP %s for %r", response.status_code, query)
                return []
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("geocode failed for %r: %s", query, exc)
            return []

    def _request(self, query):
        """Return the first result inside the bounding box, or None.

        The query is tried bare before being qualified with the country: some grounds
        sit in Area C, which Nominatim does not file under Israel, and qualifying the
        query makes it prefer an Israeli *road* of the same name over the village.
        Scanning several results rather than taking the first also skips same-named
        places abroad.
        """
        for attempt in (query, f"{query}, ישראל"):
            for hit in self._fetch(attempt):
                lat, lon = float(hit["lat"]), float(hit["lon"])
                if not (
                    LAT_RANGE[0] <= lat <= LAT_RANGE[1]
                    and LON_RANGE[0] <= lon <= LON_RANGE[1]
                ):
                    continue
                details = hit.get("address") or {}
                return {
                    "lat": lat,
                    "lon": lon,
                    "city": next(
                        (details[k] for k in LOCALITY_KEYS if details.get(k)), ""
                    ),
                    "kind": hit.get("addresstype") or hit.get("type") or "",
                    "matched": hit.get("display_name", ""),
                }
        return None

    def lookup(self, query):
        """Geocode one query string, consulting and updating the cache.

        Failures are cached as None so a rerun does not repeat a lookup the service has
        already said it cannot resolve.
        """
        query = (query or "").strip()
        if not query:
            return None
        if query in self.cache:
            self.stats["cached"] += 1
            return self.cache[query]

        result = self._request(query)
        self.cache[query] = result
        self.stats["fetched" if result else "misses"] += 1
        return result

    def refine_to_street(self, address, anchor):
        """Sharpen a located ground to its street, or return None to keep the anchor.

        A candidate is only accepted if it falls within MAX_STREET_OFFSET_KM of the
        anchor, which is what stops a same-named street elsewhere in the country from
        being taken as a match.
        """
        if not address or not anchor:
            return None

        candidates = [address]
        # House numbers often prevent a match outright; try without them too.
        without_number = _HOUSE_NUMBER_RE.sub("", address).strip()
        if without_number and without_number != address:
            candidates.append(without_number)

        for candidate in candidates:
            result = self.lookup(candidate)
            if not result:
                continue
            distance = haversine_km(
                result["lat"], result["lon"], anchor["lat"], anchor["lon"]
            )
            if distance <= MAX_STREET_OFFSET_KM:
                return {**result, "query": candidate, "offset_km": round(distance, 2)}
            self.stats["rejected"] += 1
            log.info(
                "rejected street match %r: %.1f km from %s",
                candidate, distance, anchor.get("name", "anchor"),
            )
        return None

    def find_settlement(self, *queries):
        """Geocode the first query that resolves to somewhere people live.

        Used only when the gazetteer has no entry for a locality, so there is no anchor
        to validate against; requiring a settlement type keeps a street or a field from
        being accepted as a town.
        """
        for query in queries:
            result = self.lookup(query)
            if result and result.get("kind") in SETTLEMENT_TYPES:
                return {**result, "query": query}
        return None
