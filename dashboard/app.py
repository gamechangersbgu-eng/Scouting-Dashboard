"""Compatibility wrapper for the scouting dashboard with a league filter."""

from flask import request

from . import app_core as _core


_original_search = _core.ScoutingData.search
_original_summary = _core.ScoutingData.summary


def _league_names(value):
    if value is None:
        return set()
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return set()
    return {part.strip() for part in text.split(",") if part.strip()}


def search_with_league(
    self,
    query,
    above_age_only=False,
    birth_year=None,
    current_team=None,
    limit=40,
):
    league = request.args.get("league", "").strip()
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
    return summary


_core.ScoutingData.search = search_with_league
_core.ScoutingData.summary = summary_with_leagues

# Re-export the normal app factory for callers that import dashboard.app.
create_app = _core.create_app
main = _core.main


if __name__ == "__main__":
    main()
