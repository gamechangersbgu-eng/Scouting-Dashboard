"""Authoritative Hebrew locality index for Israel, built from the GeoNames dump.

Ground addresses are published as "<street> <number> <locality>" with no delimiter, so
the locality has to be recovered by matching the end of the address. Doing that against
a closed list of real locality names is what makes it safe: a free-text geocoder happily
matches "נתיב דבורה ערד" to the village of Daburiyya 130km away, whereas a gazetteer
simply has no such locality and the match falls back to "ערד".
"""

import csv
import io
import logging
import re
import zipfile

import certifi
import requests

from . import config

log = logging.getLogger(__name__)

GEONAMES_URL = "https://download.geonames.org/export/dump/IL.zip"
GEONAMES_FILE = "IL.txt"

# GeoNames feature class P is "populated place", which is what a ground address ends in.
POPULATED_PLACE = "P"

# Longest locality name in the data is a few words; anything beyond that is street name.
MAX_LOCALITY_WORDS = 5

_HEBREW_RE = re.compile(r"[\u0590-\u05FF]")
_PUNCTUATION_RE = re.compile(r"[\"'\u2018\u2019\u05f3\u05f4\-\u2013\u05be.,()\[\]/]")
_MULTISPACE_RE = re.compile(r"\s+")
_DIGITS_RE = re.compile(r"\d+")

# The association transliterates the Arabic غ as ע', GeoNames usually as ג.
_GHAIN_RE = re.compile(r"ע['\u2018\u2019\u05f3]")


def normalize(text):
    """Fold the spelling differences between the two sources.

    Apostrophes, quotes and hyphens are used inconsistently (סח'נין / סח’נין / סכנין,
    תל אביב יפו / תל אביב-יפו), so they are dropped rather than matched on.
    """
    if not text:
        return ""
    return _MULTISPACE_RE.sub(" ", _PUNCTUATION_RE.sub("", text)).strip()


def spelling_variants(text):
    """Normalised forms to try for one name, most literal first."""
    base = normalize(text)
    variants = [base]
    ghain = normalize(_GHAIN_RE.sub("ג", text))
    if ghain != base:
        variants.append(ghain)
    return variants


class Gazetteer:
    """Hebrew locality name -> coordinates, from a cached GeoNames extract."""

    def __init__(self, cache_path=None):
        self.cache_path = cache_path or config.DATA_DIR / "geonames_IL.txt"
        self.places = self._build_index()

    def _raw_rows(self):
        if not self.cache_path.exists():
            log.info("downloading GeoNames Israel extract")
            response = requests.get(
                GEONAMES_URL, timeout=config.REQUEST_TIMEOUT, verify=certifi.where()
            )
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                payload = archive.read(GEONAMES_FILE).decode("utf-8")
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(payload, encoding="utf-8")

        with self.cache_path.open(encoding="utf-8", newline="") as handle:
            yield from csv.reader(handle, delimiter="\t")

    def _build_index(self):
        """Index every Hebrew name and alias of every populated place.

        Where two places share a name the more populous wins, so "תל אביב" resolves to
        the city rather than a like-named neighbourhood.
        """
        places = {}
        for row in self._raw_rows():
            if len(row) < 15 or row[6] != POPULATED_PLACE:
                continue
            try:
                lat, lon = float(row[4]), float(row[5])
                population = int(row[14] or 0)
            except ValueError:
                continue

            for name in [row[1], *row[3].split(",")]:
                name = name.strip()
                if not name or not _HEBREW_RE.search(name):
                    continue
                for key in spelling_variants(name):
                    if not key:
                        continue
                    existing = places.get(key)
                    if existing is None or population > existing["population"]:
                        places[key] = {
                            "name": name,
                            "lat": lat,
                            "lon": lon,
                            "population": population,
                        }
        log.info("gazetteer: %s Hebrew locality names", len(places))
        return places

    def lookup(self, name):
        for key in spelling_variants(name):
            found = self.places.get(key)
            if found:
                return found
        return None

    def match_suffix(self, address):
        """Find the locality at the end of an address, preferring the longest match.

        Longest-first matters: "אחד העם 8 רמת גן" must resolve to רמת גן and not to a
        place called גן.
        """
        words = [w for w in normalize(_DIGITS_RE.sub(" ", address or "")).split() if w]
        if not words:
            return None
        for size in range(min(MAX_LOCALITY_WORDS, len(words)), 0, -1):
            candidate = " ".join(words[-size:])
            found = self.lookup(candidate)
            if found:
                return {**found, "query": candidate}
        return None

    def match_prefix(self, name):
        """Find the locality at the start of a name, preferring the longest match.

        Ground names lead with their town ("חיפה קצף תחתון", "יבנה דרך הנשיאים"), so a
        ground name is matched from the front rather than the back.
        """
        words = [w for w in normalize(_DIGITS_RE.sub(" ", name or "")).split() if w]
        if not words:
            return None
        for size in range(min(MAX_LOCALITY_WORDS, len(words)), 0, -1):
            candidate = " ".join(words[:size])
            found = self.lookup(candidate)
            if found:
                return {**found, "query": candidate}
        return None
