"""Password-based access control and PostgreSQL-backed user provisioning."""

import csv
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

log = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 12
SESSION_LIFETIME = timedelta(hours=8)


class AuthenticationUnavailable(RuntimeError):
    """Raised when the application cannot reach its user store."""


class InvalidUserImport(ValueError):
    """Raised when an operator-provided user CSV is unsafe or malformed."""


@dataclass(frozen=True)
class User:
    id: str
    username: str
    username_key: str
    password_hash: str


def normalise_username(value):
    """Return the case-insensitive lookup key for a user-facing username."""
    return (value or "").strip().casefold()


def validate_username(value):
    username = (value or "").strip()
    if not 3 <= len(username) <= 64 or any(ord(char) < 32 for char in username):
        raise InvalidUserImport("username must contain 3-64 printable characters")
    return username


def validate_password(value):
    if not isinstance(value, str) or len(value) < MIN_PASSWORD_LENGTH:
        raise InvalidUserImport(
            f"password must contain at least {MIN_PASSWORD_LENGTH} characters"
        )
    return value


def load_user_csv(path):
    """Load a complete, plaintext input roster without retaining the file contents."""
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"username", "password"}.issubset(reader.fieldnames):
            raise InvalidUserImport("CSV must contain username,password headers")

        users = []
        seen = set()
        for line_number, row in enumerate(reader, 2):
            try:
                username = validate_username(row.get("username"))
                password = validate_password(row.get("password"))
            except InvalidUserImport as exc:
                raise InvalidUserImport(f"row {line_number}: {exc}") from exc
            username_key = normalise_username(username)
            if username_key in seen:
                raise InvalidUserImport(f"row {line_number}: duplicate username")
            seen.add(username_key)
            users.append(
                {
                    "username": username,
                    "username_key": username_key,
                    "password_hash": generate_password_hash(password),
                }
            )
    if not users:
        raise InvalidUserImport("CSV must contain at least one user")
    return users


class PostgresUserStore:
    """Small PostgreSQL access layer used by the web application and importer."""

    def __init__(self, database_url=None):
        self.database_url = database_url or os.environ.get("DATABASE_URL")

    def _connect(self):
        if not self.database_url:
            raise AuthenticationUnavailable("DATABASE_URL is not configured")
        try:
            import psycopg
        except ImportError as exc:  # Makes local read-only dashboard work before install.
            raise AuthenticationUnavailable("psycopg is not installed") from exc
        try:
            return psycopg.connect(self.database_url, connect_timeout=5)
        except Exception as exc:
            raise AuthenticationUnavailable("could not connect to user database") from exc

    def ensure_schema(self):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    username VARCHAR(64) NOT NULL,
                    username_key VARCHAR(64) NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

    @staticmethod
    def _user(row):
        if row is None:
            return None
        return User(
            id=str(row[0]),
            username=row[1],
            username_key=row[2],
            password_hash=row[3],
        )

    def find_active_by_key(self, username_key):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, username, username_key, password_hash
                FROM users WHERE username_key = %s AND active = TRUE
                """,
                (username_key,),
            )
            return self._user(cursor.fetchone())

    def find_active_by_id(self, user_id):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, username, username_key, password_hash
                FROM users WHERE id = %s AND active = TRUE
                """,
                (user_id,),
            )
            return self._user(cursor.fetchone())

    def sync(self, users, dry_run=False):
        """Make ``users`` the authoritative active roster in one transaction."""
        supplied_keys = {user["username_key"] for user in users}
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT username_key, active FROM users")
            existing = {row[0]: row[1] for row in cursor.fetchall()}
            created = sum(key not in existing for key in supplied_keys)
            reactivated = sum(key in existing and not existing[key] for key in supplied_keys)
            deactivated = sum(key not in supplied_keys and active for key, active in existing.items())
            stats = {
                "created": created,
                "updated": len(users) - created,
                "reactivated": reactivated,
                "deactivated": deactivated,
            }
            if dry_run:
                connection.rollback()
                return stats

            for user in users:
                cursor.execute(
                    """
                    INSERT INTO users (username, username_key, password_hash, active)
                    VALUES (%(username)s, %(username_key)s, %(password_hash)s, TRUE)
                    ON CONFLICT (username_key) DO UPDATE SET
                        username = EXCLUDED.username,
                        password_hash = EXCLUDED.password_hash,
                        active = TRUE,
                        updated_at = NOW()
                    """,
                    user,
                )
            if supplied_keys:
                cursor.execute(
                    "UPDATE users SET active = FALSE, updated_at = NOW() "
                    "WHERE active = TRUE AND NOT (username_key = ANY(%s))",
                    (list(supplied_keys),),
                )
            return stats


def _csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _valid_csrf(value):
    expected = session.get("csrf_token")
    return bool(expected and value and secrets.compare_digest(expected, value))


def _safe_next(value):
    if not value:
        return None
    parsed = urlparse(value)
    return value if not parsed.scheme and not parsed.netloc and value.startswith("/") else None


def _is_api_request():
    return request.path.startswith("/api/")


def configure_auth(app: Flask, store=None):
    """Add login/session protection to an existing Flask application."""
    template_directory = str(Path(__file__).with_name("templates"))
    if template_directory not in app.jinja_loader.searchpath:
        app.jinja_loader.searchpath.append(template_directory)
    production = app.config.get("APP_ENV", os.environ.get("APP_ENV")) == "production"
    secret_key = app.config.get("FLASK_SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY")
    if not secret_key:
        if production:
            raise RuntimeError("FLASK_SECRET_KEY must be configured in production")
        secret_key = secrets.token_urlsafe(32)
        log.warning("using an ephemeral development session key")

    app.config.update(
        SECRET_KEY=secret_key,
        PERMANENT_SESSION_LIFETIME=SESSION_LIFETIME,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=production,
    )
    app.extensions["user_store"] = store or app.config.get("USER_STORE") or PostgresUserStore()

    def user_store():
        return app.extensions["user_store"]

    @app.before_request
    def require_login():
        if request.endpoint == "login":
            return None
        user_id = session.get("user_id")
        if not user_id:
            if _is_api_request():
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        try:
            user = user_store().find_active_by_id(user_id)
        except AuthenticationUnavailable:
            log.exception("authentication database is unavailable")
            return jsonify({"error": "authentication unavailable"}), 503
        if user is None:
            session.clear()
            if _is_api_request():
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for("login"))
        request.current_user = user
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("user_id") and request.method == "GET":
            return redirect(url_for("index"))
        next_url = _safe_next(request.values.get("next"))
        error = None
        if request.method == "POST":
            if not _valid_csrf(request.form.get("csrf_token")):
                error = "בקשה לא תקינה. נסו שוב."
            else:
                username_key = normalise_username(request.form.get("username"))
                try:
                    user = user_store().find_active_by_key(username_key)
                except AuthenticationUnavailable:
                    log.exception("authentication database is unavailable")
                    return "שירות ההתחברות אינו זמין כרגע.", 503
                if user is None or not check_password_hash(user.password_hash, request.form.get("password", "")):
                    error = "שם המשתמש או הסיסמה אינם נכונים."
                else:
                    session.clear()
                    session["user_id"] = user.id
                    session.permanent = True
                    _csrf_token()
                    return redirect(next_url or url_for("index"))
        return render_template("login.html", csrf_token=_csrf_token(), next_url=next_url, error=error)

    @app.get("/api/auth/csrf")
    def csrf():
        return jsonify({"csrf_token": _csrf_token()})

    @app.post("/logout")
    def logout():
        if not _valid_csrf(request.form.get("csrf_token")):
            return jsonify({"error": "invalid CSRF token"}), 400
        session.clear()
        return redirect(url_for("login"))

    return app
