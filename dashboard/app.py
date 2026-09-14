"""Compatibility wrapper for dashboard filters that are not part of the core app."""

import math
from collections import defaultdict

from flask import request

from . import app_core as _core

_original_search = _core.ScoutingData.search
_original_summary = _core.ScoutingData.summary


def _coordinates(row):
    """Return valid map coordinates from a location row, or ``None``."""
    try:
        lat, lon = float(row.get("lat")), float(row.get("lon"))
    except (TypeError, ValueError):
        return None
    return (lat, lon) if math.isfinite(lat) and math.isfinite(lon) else None


def _location_anchors(self):
    """Return one stable centre point for each mapped city.

    A city can have several home grounds, so its anchor is the average of their
    coordinates. It is only used as the centre of the user's radius filter; player
    distance is always calculated from the actual home ground of the current team.
    """
    cached = getattr(self, "_location_anchors", None)
    if cached is not None:
        return cached

    points = defaultdict(list)
    for row in self.locations.values():
        city = str(row.get("city") or "").strip()
        coords = _coordinates(row)
        if city and city.lower() != "nan" and coords:
            points[city].append(coords)

    anchors = {
        city: (
            sum(lat for lat, _ in city_points) / len(city_points),
            sum(lon for _, lon in city_points) / len(city_points),
        )
        for city, city_points in points.items()
    }
    self._location_anchors = anchors
    return anchors


def _current_team_locations(self):
    """Map every player to their latest-season home-ground coordinates."""
    cached = getattr(self, "_current_team_locations", None)
    if cached is not None:
        return cached

    locations = {}
    for player_id, rows in self.rows_by_player.items():
        latest_season = max((row["season_id"] for row in rows), default=None)
        current = []
        for row in rows:
            if row["season_id"] != latest_season:
                continue
            coords = _coordinates(self.locations.get(row["team_id"], {}))
            if coords and coords not in current:
                current.append(coords)
        locations[player_id] = current

    self._current_team_locations = locations
    return locations


def _haversine_km(origin, destination):
    """Great-circle distance between two ``(lat, lon)`` points in kilometres."""
    lat1, lon1 = map(math.radians, origin)
    lat2, lon2 = map(math.radians, destination)
    inner = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(inner))


def _league_names(value):
    if value is None:
        return set()
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return set()
    return {part.strip() for part in text.split(",") if part.strip()}


def search_with_filters(
    self,
    query,
    above_age_only=False,
    birth_year=None,
    current_team=None,
    limit=40,
):
    league = request.args.get("league", "").strip()
    location = request.args.get("location", "").strip()
    radius_km = request.args.get("radius_km", default=50, type=float)
    # Fetch a large candidate set before applying the league filter, otherwise the
    # original limit could hide matching players from the selected league.
    candidates = _original_search(
        self,
        query,
        above_age_only=above_age_only,
        birth_year=birth_year,
        current_team=current_team,
        limit=max(limit, 100000),
    )
    if league:
        candidates = [
            player
            for player in candidates
            if league in _league_names(self.players[player["player_id"]].get("leagues"))
        ]
    if location and radius_km is not None and radius_km > 0:
        anchor = _location_anchors(self).get(location)
        if anchor:
            current_locations = _current_team_locations(self)
            candidates = [
                player
                for player in candidates
                if any(
                    _haversine_km(anchor, team_location) <= radius_km
                    for team_location in current_locations.get(player["player_id"], [])
                )
            ]
    return candidates[:limit]


def summary_with_leagues(self):
    summary = _original_summary(self)
    leagues = {
        str(row.get("league_name")).strip()
        for row in self.season_rows
        if row.get("league_name") is not None
        and str(row.get("league_name")).strip()
        and str(row.get("league_name")).lower() != "nan"
    }
    summary["leagues"] = sorted(leagues)
    summary["locations"] = [
        {"city": city, "lat": lat, "lon": lon}
        for city, (lat, lon) in sorted(_location_anchors(self).items())
    ]
    return summary


_core.ScoutingData.search = search_with_filters
_core.ScoutingData.summary = summary_with_leagues

# Re-export the normal app factory for callers that import dashboard.app.
create_app = _core.create_app
main = _core.main


if __name__ == "__main__":
    main()
