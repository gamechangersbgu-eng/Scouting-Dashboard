"""Shortlist API and persistence-contract tests without a live PostgreSQL server.

The production ``ShortlistStore`` is PostgreSQL-only.  This small in-memory
contract double lets the Flask routes exercise authentication, CSRF, ownership
scoping, idempotency, and resilient identity rendering locally; it is not a
claim that the migration has been exercised against a real database.
"""

import math
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from werkzeug.security import generate_password_hash

from dashboard.app import create_app
from dashboard.auth import User
from dashboard.shortlists import (
    FAVORITES_NAME,
    DuplicateShortlistName,
    InvalidShortlistName,
    ProtectedShortlist,
    Shortlist,
    ShortlistNotFound,
    validate_shortlist_name,
)


class MemoryUserStore:
    def __init__(self):
        self.users = {
            name: User(str(index), name, name, generate_password_hash(f"{name} password long"))
            for index, name in enumerate(("alice", "bob"), 1)
        }

    def find_active_by_key(self, key):
        return self.users.get(key)

    def find_active_by_id(self, user_id):
        return next((user for user in self.users.values() if user.id == str(user_id)), None)


class MemoryShortlistStore:
    """Same ownership contract as the SQL store, intentionally kept test-only."""

    def __init__(self):
        self._next_id = 1
        self.rows = {}
        self.members = {}
        self.favorites_created = 0

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    def _new(self, user_id, name, is_default=False):
        row = Shortlist(self._next_id, name, is_default, self._now(), self._now(), 0)
        self._next_id += 1
        self.rows[row.id] = (str(user_id), row)
        self.members[row.id] = {}
        return row

    def _owned(self, user_id, shortlist_id):
        record = self.rows.get(int(shortlist_id))
        if not record or record[0] != str(user_id):
            raise ShortlistNotFound("shortlist not found")
        row = record[1]
        return Shortlist(row.id, row.name, row.is_default, row.created_at, row.updated_at, len(self.members[row.id]))

    def list_shortlists(self, user_id):
        self.favorites(user_id)
        return sorted(
            (self._owned(user_id, shortlist_id) for shortlist_id, record in self.rows.items() if record[0] == str(user_id)),
            key=lambda row: (not row.is_default, row.id),
        )

    def create_shortlist(self, user_id, name):
        name = validate_shortlist_name(name)
        if any(owner == str(user_id) and row.name == name for owner, row in self.rows.values()):
            raise DuplicateShortlistName("a shortlist with that name already exists")
        return self._new(user_id, name)

    def get_shortlist(self, user_id, shortlist_id):
        return self._owned(user_id, shortlist_id)

    def rename_shortlist(self, user_id, shortlist_id, name):
        row = self._owned(user_id, shortlist_id)
        if row.is_default:
            raise ProtectedShortlist("Favorites cannot be renamed or deleted")
        name = validate_shortlist_name(name)
        if any(owner == str(user_id) and other.id != row.id and other.name == name for owner, other in self.rows.values()):
            raise DuplicateShortlistName("a shortlist with that name already exists")
        changed = Shortlist(row.id, name, False, row.created_at, self._now(), len(self.members[row.id]))
        self.rows[row.id] = (str(user_id), changed)
        return changed

    def delete_shortlist(self, user_id, shortlist_id):
        row = self._owned(user_id, shortlist_id)
        if row.is_default:
            raise ProtectedShortlist("Favorites cannot be renamed or deleted")
        del self.rows[row.id]
        del self.members[row.id]

    def favorites(self, user_id):
        existing = next((row for owner, row in self.rows.values() if owner == str(user_id) and row.is_default), None)
        if existing:
            return self._owned(user_id, existing.id)
        self.favorites_created += 1
        return self._new(user_id, FAVORITES_NAME, True)

    def favorite_player_ids(self, user_id):
        return list(self.members[self.favorites(user_id).id])

    def member_shortlist_ids(self, user_id, player_id):
        return [row.id for row in self.list_shortlists(user_id) if str(player_id) in self.members[row.id]]

    def add_player(self, user_id, shortlist_id, player):
        row = self._owned(user_id, shortlist_id)
        player_id = str(player["player_id"])
        if player_id in self.members[row.id]:
            return False
        self.members[row.id][player_id] = {
            "player_id": player_id, "player_name": player.get("player_name"),
            "birth_year": player.get("birth_year"), "last_known_team": player.get("current_team"),
            "created_at": self._now().isoformat(),
        }
        return True

    def remove_player(self, user_id, shortlist_id, player_id):
        row = self._owned(user_id, shortlist_id)
        return self.members[row.id].pop(str(player_id), None) is not None

    def shortlist_players(self, user_id, shortlist_id):
        row = self._owned(user_id, shortlist_id)
        return list(self.members[row.id].values())


class FakeScoutingData:
    def __init__(self):
        self.players = {
            "p1": {"player_id": "p1", "player_name": "Player One", "birth_year": 2010, "current_team": "Blue", "plays_above_age": True, "age_groups_above": 1, "former_hapoel_player": True},
            "p2": {"player_id": "p2", "player_name": "Player Two", "birth_year": 2011, "current_team": "Red", "plays_above_age": False, "age_groups_above": 0, "former_hapoel_player": False},
        }

    def player(self, player_id):
        return self.players.get(str(player_id))

    def summary(self):
        return {"players": len(self.players), "teams_located": 0, "teams": 0, "above_age": 0, "seasons": [], "history_seasons": [], "birth_years": [], "current_teams": [], "locations": []}

    def search(self, *args, **kwargs):
        return list(self.players.values())

    def player_movements(self):
        return {}


class ShortlistApiTests(unittest.TestCase):
    def setUp(self):
        self.users = MemoryUserStore()
        self.shortlists = MemoryShortlistStore()
        self.data = FakeScoutingData()
        with mock.patch("dashboard.app._core._resolve_data_source", return_value=self.data):
            self.app = create_app(user_store=self.users, shortlist_store=self.shortlists)
        self.app.config.update(TESTING=True, FLASK_SECRET_KEY="shortlist-test-secret")
        self.alice = self.app.test_client()
        self.bob = self.app.test_client()

    def login(self, client, username):
        page = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
        response = client.post("/login", data={"csrf_token": token, "username": username, "password": f"{username} password long"})
        self.assertEqual(response.status_code, 302)

    def csrf(self, client):
        return {"X-CSRF-Token": client.get("/api/auth/csrf").get_json()["csrf_token"]}

    def post(self, client, url, payload=None, method="post"):
        return getattr(client, method)(url, json=payload, headers=self.csrf(client))

    def test_auth_csrf_and_favorites_created_once(self):
        self.assertEqual(self.alice.get("/api/shortlists").status_code, 401)
        self.login(self.alice, "alice")
        self.assertEqual(self.alice.post("/api/shortlists", json={"name": "x"}).status_code, 400)
        first = self.alice.get("/api/shortlists").get_json()
        second = self.alice.get("/api/shortlists").get_json()
        self.assertEqual(first["shortlists"][0]["name"], FAVORITES_NAME)
        self.assertEqual(len(first["favorite_player_ids"]), 0)
        self.assertEqual(second["shortlists"][0]["id"], first["shortlists"][0]["id"])
        self.assertEqual(self.shortlists.favorites_created, 1)

    def test_custom_crud_duplicates_and_default_protection(self):
        self.login(self.alice, "alice")
        invalid = self.post(self.alice, "/api/shortlists", {"name": "   "})
        self.assertEqual(invalid.status_code, 400)
        created = self.post(self.alice, "/api/shortlists", {"name": "חלוצים למעקב"})
        self.assertEqual(created.status_code, 201)
        shortlist_id = created.get_json()["shortlist"]["id"]
        self.assertEqual(self.post(self.alice, "/api/shortlists", {"name": "חלוצים למעקב"}).status_code, 409)
        renamed = self.post(self.alice, f"/api/shortlists/{shortlist_id}", {"name": "ילידי 2010"}, "patch")
        self.assertEqual(renamed.get_json()["shortlist"]["name"], "ילידי 2010")
        default_id = self.alice.get("/api/shortlists").get_json()["shortlists"][0]["id"]
        self.assertEqual(self.post(self.alice, f"/api/shortlists/{default_id}", {"name": "x"}, "patch").status_code, 409)
        self.assertEqual(self.post(self.alice, f"/api/shortlists/{default_id}", method="delete").status_code, 409)
        self.assertEqual(self.post(self.alice, f"/api/shortlists/{shortlist_id}", method="delete").status_code, 200)

    def test_same_name_is_private_and_all_cross_user_operations_are_404(self):
        self.login(self.alice, "alice")
        self.login(self.bob, "bob")
        alice_list = self.post(self.alice, "/api/shortlists", {"name": "לבדיקה נוספת"}).get_json()["shortlist"]["id"]
        self.assertEqual(self.post(self.bob, "/api/shortlists", {"name": "לבדיקה נוספת"}).status_code, 201)
        self.assertEqual(self.bob.get(f"/api/shortlists/{alice_list}").status_code, 404)
        self.assertEqual(self.post(self.bob, f"/api/shortlists/{alice_list}", {"name": "x"}, "patch").status_code, 404)
        self.assertEqual(self.post(self.bob, f"/api/shortlists/{alice_list}", method="delete").status_code, 404)
        self.assertEqual(self.post(self.bob, f"/api/shortlists/{alice_list}/players/p1").status_code, 404)
        self.assertEqual(self.post(self.bob, f"/api/shortlists/{alice_list}/players/p1", method="delete").status_code, 404)

    def test_membership_favorites_and_durable_snapshot(self):
        self.login(self.alice, "alice")
        custom_id = self.post(self.alice, "/api/shortlists", {"name": "לקראת העונה הבאה"}).get_json()["shortlist"]["id"]
        first = self.post(self.alice, f"/api/shortlists/{custom_id}/players/p1").get_json()
        second = self.post(self.alice, f"/api/shortlists/{custom_id}/players/p1").get_json()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        favorite = self.post(self.alice, "/api/favorites/p1", method="put")
        self.assertTrue(favorite.get_json()["favorite"])
        membership = self.alice.get("/api/shortlists?player_id=p1").get_json()
        self.assertEqual(len(membership["member_shortlist_ids"]), 2)
        self.assertEqual(self.post(self.alice, "/api/shortlists/999/players/p1").status_code, 404)
        self.assertEqual(self.post(self.alice, f"/api/shortlists/{custom_id}/players/nope").status_code, 404)
        # Simulates an independent current-catalog refresh: saved membership and
        # last-known identity remain even though current scouting fields do not.
        self.data.players.pop("p1")
        detail = self.alice.get(f"/api/shortlists/{custom_id}").get_json()["players"][0]
        self.assertEqual(detail["player_name"], "Player One")
        self.assertFalse(detail["available_in_current_catalog"])
        self.assertIsNone(detail["current_team"])
        self.assertTrue(self.post(self.alice, "/api/favorites/p1", method="delete").get_json()["favorite"] is False)
        self.assertTrue(self.post(self.alice, f"/api/shortlists/{custom_id}/players/p1", method="delete").get_json()["removed"])

    def test_name_validation_and_migration_keep_dataset_state_separate(self):
        self.assertEqual(validate_shortlist_name("  רשימה, 2010!  "), "רשימה, 2010!")
        with self.assertRaises(InvalidShortlistName):
            validate_shortlist_name(FAVORITES_NAME)
        with self.assertRaises(InvalidShortlistName):
            validate_shortlist_name("x" * 81)
        migration = Path(__file__).parents[1] / "migrations" / "versions" / "0004_user_shortlists.py"
        text = migration.read_text(encoding="utf-8")
        self.assertIn("UNIQUE (user_id, name)", text)
        self.assertIn("uq_shortlists_one_default_per_user", text)
        self.assertIn("PRIMARY KEY (shortlist_id, player_id)", text)
        self.assertNotIn("dataset_id", text[text.index("def upgrade"):])

    def test_shortlist_json_has_no_nan(self):
        self.login(self.alice, "alice")
        payload = self.alice.get("/api/shortlists").get_json()
        self.assertFalse(any(math.isnan(value) for value in walk_numbers(payload) if isinstance(value, float)))


def walk_numbers(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from walk_numbers(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_numbers(item)
    else:
        yield value


if __name__ == "__main__":
    unittest.main()
