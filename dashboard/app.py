"""Dashboard application with filters and password-protected access."""

import argparse
import logging
import math
import os
import webbrowser
from collections import defaultdict

from flask import has_request_context, jsonify, request

from . import app_core as _core
from .auth import configure_auth, valid_csrf
from .shortlists import (
    DuplicateShortlistName,
    InvalidShortlistName,
    ProtectedShortlist,
    ShortlistNotFound,
    ShortlistStore,
    ShortlistUnavailable,
)

_original_search = _core.BaseScoutingData.search
_original_summary = _core.BaseScoutingData.summary


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
    # BaseScoutingData is also used directly by import/parity tests and tools,
    # outside Flask. Those callers predate URL-only filters and should retain
    # plain search semantics rather than requiring a synthetic request context.
    league = request.args.get("league", "").strip() if has_request_context() else ""
    location = request.args.get("location", "").strip() if has_request_context() else ""
    radius_km = request.args.get("radius_km", default=50, type=float) if has_request_context() else 50
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


# Patched on the shared base class (not on CsvScoutingData) so the league/
# location search filters also apply to PostgresScoutingData once it exists --
# the two are siblings under BaseScoutingData, not one a subclass of the other.
_core.BaseScoutingData.search = search_with_filters
_core.BaseScoutingData.summary = summary_with_leagues

def _shortlist_error(error):
    if isinstance(error, (ShortlistNotFound,)):
        return jsonify({"error": "shortlist not found"}), 404
    if isinstance(error, ProtectedShortlist):
        return jsonify({"error": str(error)}), 409
    if isinstance(error, DuplicateShortlistName):
        return jsonify({"error": str(error)}), 409
    if isinstance(error, InvalidShortlistName):
        return jsonify({"error": str(error)}), 400
    if isinstance(error, ShortlistUnavailable):
        _core.log.exception("shortlist database is unavailable")
        return jsonify({"error": "shortlists are temporarily unavailable"}), 503
    raise error


def _csrf_or_error():
    token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    if not valid_csrf(token):
        return jsonify({"error": "invalid CSRF token"}), 400
    return None


def _json_body():
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def create_app(data_dir=None, data_source=None, user_store=None, shortlist_store=None):
    """Create the dashboard and require an authenticated user for every route."""
    app = _core.create_app(data_dir, data_source=data_source)
    app.config["APP_ENV"] = os.environ.get("APP_ENV", "development")
    app = configure_auth(app, user_store)
    app.extensions["shortlist_store"] = shortlist_store or app.config.get("SHORTLIST_STORE") or ShortlistStore()

    def store():
        return app.extensions["shortlist_store"]

    def current_player_or_404(player_id):
        player = app.extensions["scouting_data"].player(str(player_id))
        if player is None:
            return None
        return player

    def shortlist_payload(player_id=None):
        """Return bounded membership state; never one request per result row."""
        user_id = request.current_user.id
        shortlists = store().list_shortlists(user_id)
        payload = {
            "shortlists": [shortlist.as_dict() for shortlist in shortlists],
            "favorite_player_ids": store().favorite_player_ids(user_id),
        }
        if player_id is not None:
            payload["member_shortlist_ids"] = store().member_shortlist_ids(user_id, str(player_id))
        return payload

    @app.get("/api/shortlists")
    def list_shortlists():
        try:
            return jsonify(shortlist_payload(request.args.get("player_id")))
        except Exception as error:
            return _shortlist_error(error)

    @app.post("/api/shortlists")
    def create_shortlist():
        if response := _csrf_or_error():
            return response
        try:
            shortlist = store().create_shortlist(request.current_user.id, _json_body().get("name"))
            return jsonify({"shortlist": shortlist.as_dict()}), 201
        except Exception as error:
            return _shortlist_error(error)

    @app.get("/api/shortlists/<int:shortlist_id>")
    def get_shortlist(shortlist_id):
        try:
            shortlist = store().get_shortlist(request.current_user.id, shortlist_id)
            members = []
            for saved in store().shortlist_players(request.current_user.id, shortlist_id):
                current = current_player_or_404(saved["player_id"])
                # Do not fabricate current fields: saved identity is labeled as
                # last-known only when the current catalog no longer has it.
                members.append(
                    {
                        "player_id": saved["player_id"],
                        "player_name": (current or {}).get("player_name") or saved["player_name"] or saved["player_id"],
                        "birth_year": (current or {}).get("birth_year", saved["birth_year"]),
                        "current_team": (current or {}).get("current_team"),
                        "last_known_team": saved["last_known_team"],
                        "plays_above_age": bool((current or {}).get("plays_above_age", False)),
                        "age_groups_above": (current or {}).get("age_groups_above", 0),
                        "former_hapoel_player": bool((current or {}).get("former_hapoel_player", False)),
                        "available_in_current_catalog": current is not None,
                        "created_at": saved["created_at"],
                    }
                )
            return jsonify({"shortlist": shortlist.as_dict(), "players": members})
        except Exception as error:
            return _shortlist_error(error)

    @app.patch("/api/shortlists/<int:shortlist_id>")
    def rename_shortlist(shortlist_id):
        if response := _csrf_or_error():
            return response
        try:
            shortlist = store().rename_shortlist(request.current_user.id, shortlist_id, _json_body().get("name"))
            return jsonify({"shortlist": shortlist.as_dict()})
        except Exception as error:
            return _shortlist_error(error)

    @app.delete("/api/shortlists/<int:shortlist_id>")
    def delete_shortlist(shortlist_id):
        if response := _csrf_or_error():
            return response
        try:
            store().delete_shortlist(request.current_user.id, shortlist_id)
            return jsonify({"deleted": True, "shortlist_id": shortlist_id})
        except Exception as error:
            return _shortlist_error(error)

    def add_shortlist_player(shortlist_id, player_id):
        if response := _csrf_or_error():
            return response
        player = current_player_or_404(player_id)
        if player is None:
            return jsonify({"error": "unknown player_id"}), 404
        try:
            created = store().add_player(request.current_user.id, shortlist_id, player)
            return jsonify({"player_id": str(player_id), "added": True, "created": created})
        except Exception as error:
            return _shortlist_error(error)

    app.add_url_rule(
        "/api/shortlists/<int:shortlist_id>/players/<player_id>",
        view_func=add_shortlist_player, methods=["POST"], endpoint="add_shortlist_player",
    )

    @app.delete("/api/shortlists/<int:shortlist_id>/players/<player_id>")
    def remove_shortlist_player(shortlist_id, player_id):
        if response := _csrf_or_error():
            return response
        try:
            removed = store().remove_player(request.current_user.id, shortlist_id, str(player_id))
            return jsonify({"player_id": str(player_id), "removed": removed})
        except Exception as error:
            return _shortlist_error(error)

    @app.put("/api/favorites/<player_id>")
    def add_favorite(player_id):
        if response := _csrf_or_error():
            return response
        player = current_player_or_404(player_id)
        if player is None:
            return jsonify({"error": "unknown player_id"}), 404
        try:
            favorites = store().favorites(request.current_user.id)
            created = store().add_player(request.current_user.id, favorites.id, player)
            return jsonify({"player_id": str(player_id), "favorite": True, "created": created})
        except Exception as error:
            return _shortlist_error(error)

    @app.delete("/api/favorites/<player_id>")
    def remove_favorite(player_id):
        if response := _csrf_or_error():
            return response
        try:
            favorites = store().favorites(request.current_user.id)
            store().remove_player(request.current_user.id, favorites.id, str(player_id))
            return jsonify({"player_id": str(player_id), "favorite": False})
        except Exception as error:
            return _shortlist_error(error)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )
    app = create_app()
    url = f"http://{args.host}:{args.port}/"
    _core.log.info("scouting dashboard on %s", url)
    if not args.no_browser:
        webbrowser.open(url)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
