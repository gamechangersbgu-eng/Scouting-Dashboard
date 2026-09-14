import re
import tempfile
import unittest
from pathlib import Path

from flask import Flask, jsonify
from werkzeug.security import generate_password_hash

from dashboard.auth import InvalidUserImport, User, configure_auth, load_user_csv, normalise_username


class MemoryUserStore:
    def __init__(self):
        self.users = {
            "scout": User(
                id="1",
                username="Scout",
                username_key="scout",
                password_hash=generate_password_hash("a secure password"),
            )
        }
        self.active_ids = {"1"}

    def find_active_by_key(self, key):
        user = self.users.get(key)
        return user if user and user.id in self.active_ids else None

    def find_active_by_id(self, user_id):
        return next(
            (user for user in self.users.values() if user.id == user_id and user.id in self.active_ids),
            None,
        )


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.config.update(TESTING=True, FLASK_SECRET_KEY="test-secret")

        @app.get("/")
        def index():
            return "private dashboard"

        @app.get("/api/secret")
        def secret():
            return jsonify({"private": True})

        self.store = MemoryUserStore()
        self.app = configure_auth(app, self.store)
        self.client = self.app.test_client()

    def _login_token(self):
        page = self.client.get("/login")
        match = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))
        self.assertIsNotNone(match)
        return match.group(1)

    def test_content_requires_login(self):
        self.assertEqual(self.client.get("/api/secret").status_code, 401)
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_login_logout_and_disabled_user(self):
        token = self._login_token()
        rejected = self.client.post(
            "/login",
            data={"csrf_token": token, "username": "Scout", "password": "wrong password"},
        )
        self.assertIn("אינם נכונים", rejected.get_data(as_text=True))

        accepted = self.client.post(
            "/login",
            data={"csrf_token": self._login_token(), "username": "SCOUT", "password": "a secure password"},
            follow_redirects=True,
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(self.client.get("/api/secret").status_code, 200)

        csrf = self.client.get("/api/auth/csrf").get_json()["csrf_token"]
        logged_out = self.client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
        self.assertEqual(logged_out.status_code, 302)
        self.assertEqual(self.client.get("/api/secret").status_code, 401)

        self.store.active_ids.clear()
        blocked = self.client.post(
            "/login",
            data={"csrf_token": self._login_token(), "username": "scout", "password": "a secure password"},
        )
        self.assertIn("אינם נכונים", blocked.get_data(as_text=True))


class UserCsvTests(unittest.TestCase):
    def test_loads_valid_roster_without_plaintext_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.csv"
            path.write_text("username,password\nAlice,a secure password\n", encoding="utf-8")
            users = load_user_csv(path)
        self.assertEqual(users[0]["username_key"], "alice")
        self.assertNotEqual(users[0]["password_hash"], "a secure password")
        self.assertEqual(normalise_username("  ALICE "), "alice")

    def test_rejects_duplicate_or_weak_passwords(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.csv"
            path.write_text("username,password\nAlice,short\n", encoding="utf-8")
            with self.assertRaises(InvalidUserImport):
                load_user_csv(path)


if __name__ == "__main__":
    unittest.main()
