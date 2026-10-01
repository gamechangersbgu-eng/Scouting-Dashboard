"""Private, PostgreSQL-backed coach shortlists.

Shortlists are deliberately application state rather than scouting-data state.
In particular, neither table contains ``dataset_id``: publishing or rolling
back a canonical dataset must never change a coach's saved players.  A saved
membership keeps a small identity snapshot as well.  That permits a useful,
explicitly last-known display if a player is absent from a later current
catalog (for example, after a refresh) without inventing current statistics.
"""

import os
from dataclasses import dataclass


FAVORITES_NAME = "מועדפים"
MAX_SHORTLIST_NAME_LENGTH = 80


class ShortlistError(RuntimeError):
    """Base exception for an unavailable or invalid shortlist operation."""


class ShortlistUnavailable(ShortlistError):
    """Raised when the PostgreSQL application-state store cannot be reached."""


class InvalidShortlistName(ShortlistError):
    """Raised when a user-facing shortlist name is unsafe or unusable."""


class DuplicateShortlistName(ShortlistError):
    """Raised when a user already owns a shortlist with that name."""


class ShortlistNotFound(ShortlistError):
    """Raised for missing *or unowned* list IDs to avoid leaking ownership."""


class ProtectedShortlist(ShortlistError):
    """Raised when code attempts to rename or remove the protected Favorites list."""


@dataclass(frozen=True)
class Shortlist:
    id: int
    name: str
    is_default: bool
    created_at: object
    updated_at: object
    player_count: int = 0

    def as_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "is_default": self.is_default,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "player_count": self.player_count,
        }


def validate_shortlist_name(value):
    """Normalize a coach's name while reserving the protected Favorites name."""
    if not isinstance(value, str):
        raise InvalidShortlistName("shortlist name is required")
    name = value.strip()
    if not name:
        raise InvalidShortlistName("shortlist name cannot be empty")
    if len(name) > MAX_SHORTLIST_NAME_LENGTH:
        raise InvalidShortlistName(
            f"shortlist name must be at most {MAX_SHORTLIST_NAME_LENGTH} characters"
        )
    if any(ord(character) < 32 for character in name):
        raise InvalidShortlistName("shortlist name contains control characters")
    if name == FAVORITES_NAME:
        raise InvalidShortlistName("Favorites is the protected default shortlist")
    return name


class ShortlistStore:
    """The only persistence boundary for coach-owned shortlist data.

    Every method takes a server-derived ``user_id`` and scopes the SQL to it.
    The caller must never turn a browser-supplied user ID into this argument.
    ``PostgresUserStore`` follows the same small-psycopg access-layer style.
    """

    def __init__(self, database_url=None):
        self.database_url = database_url or os.environ.get("DATABASE_URL")

    def _connect(self):
        if not self.database_url:
            raise ShortlistUnavailable("DATABASE_URL is not configured")
        try:
            import psycopg
        except ImportError as exc:
            raise ShortlistUnavailable("psycopg is not installed") from exc
        try:
            return psycopg.connect(self.database_url, connect_timeout=5)
        except Exception as exc:
            raise ShortlistUnavailable("could not connect to shortlist database") from exc

    @staticmethod
    def _shortlist(row):
        return Shortlist(
            id=row[0], name=row[1], is_default=row[2], created_at=row[3],
            updated_at=row[4], player_count=row[5] if len(row) > 5 else 0,
        )

    @staticmethod
    def _duplicate(exc):
        return getattr(exc, "sqlstate", None) == "23505"

    def _ensure_favorites(self, cursor, user_id):
        # The partial unique index is the concurrency guard; this remains safe
        # when two first clicks for the same coach race on separate workers.
        cursor.execute(
            """
            INSERT INTO shortlists (user_id, name, is_default)
            VALUES (%s, %s, TRUE)
            ON CONFLICT (user_id) WHERE is_default DO NOTHING
            """,
            (user_id, FAVORITES_NAME),
        )

    def list_shortlists(self, user_id):
        with self._connect() as connection, connection.cursor() as cursor:
            self._ensure_favorites(cursor, user_id)
            cursor.execute(
                """
                SELECT s.id, s.name, s.is_default, s.created_at, s.updated_at,
                       COUNT(sp.player_id)::integer AS player_count
                FROM shortlists s
                LEFT JOIN shortlist_players sp ON sp.shortlist_id = s.id
                WHERE s.user_id = %s
                GROUP BY s.id
                ORDER BY s.is_default DESC, s.created_at, s.id
                """,
                (user_id,),
            )
            return [self._shortlist(row) for row in cursor.fetchall()]

    def create_shortlist(self, user_id, name):
        name = validate_shortlist_name(name)
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO shortlists (user_id, name, is_default)
                    VALUES (%s, %s, FALSE)
                    RETURNING id, name, is_default, created_at, updated_at, 0
                    """,
                    (user_id, name),
                )
                return self._shortlist(cursor.fetchone())
        except Exception as exc:
            if self._duplicate(exc):
                raise DuplicateShortlistName("a shortlist with that name already exists") from exc
            raise

    def get_shortlist(self, user_id, shortlist_id):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT s.id, s.name, s.is_default, s.created_at, s.updated_at,
                       COUNT(sp.player_id)::integer AS player_count
                FROM shortlists s
                LEFT JOIN shortlist_players sp ON sp.shortlist_id = s.id
                WHERE s.id = %s AND s.user_id = %s
                GROUP BY s.id
                """,
                (shortlist_id, user_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise ShortlistNotFound("shortlist not found")
            return self._shortlist(row)

    def rename_shortlist(self, user_id, shortlist_id, name):
        name = validate_shortlist_name(name)
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE shortlists SET name = %s, updated_at = NOW()
                    WHERE id = %s AND user_id = %s AND is_default = FALSE
                    RETURNING id, name, is_default, created_at, updated_at, 0
                    """,
                    (name, shortlist_id, user_id),
                )
                row = cursor.fetchone()
                if row is not None:
                    return self._shortlist(row)
                self._raise_missing_or_protected(cursor, user_id, shortlist_id)
        except Exception as exc:
            if self._duplicate(exc):
                raise DuplicateShortlistName("a shortlist with that name already exists") from exc
            raise

    def delete_shortlist(self, user_id, shortlist_id):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM shortlists
                WHERE id = %s AND user_id = %s AND is_default = FALSE
                RETURNING id
                """,
                (shortlist_id, user_id),
            )
            if cursor.fetchone() is not None:
                return
            self._raise_missing_or_protected(cursor, user_id, shortlist_id)

    @staticmethod
    def _raise_missing_or_protected(cursor, user_id, shortlist_id):
        # A list belonging to another user deliberately looks missing.  Only a
        # list proven to belong to this user may disclose that it is protected.
        cursor.execute(
            "SELECT is_default FROM shortlists WHERE id = %s AND user_id = %s",
            (shortlist_id, user_id),
        )
        row = cursor.fetchone()
        if row and row[0]:
            raise ProtectedShortlist("Favorites cannot be renamed or deleted")
        raise ShortlistNotFound("shortlist not found")

    def favorites(self, user_id):
        with self._connect() as connection, connection.cursor() as cursor:
            self._ensure_favorites(cursor, user_id)
            cursor.execute(
                """
                SELECT id, name, is_default, created_at, updated_at, 0
                FROM shortlists WHERE user_id = %s AND is_default = TRUE
                """,
                (user_id,),
            )
            return self._shortlist(cursor.fetchone())

    def favorite_player_ids(self, user_id):
        with self._connect() as connection, connection.cursor() as cursor:
            self._ensure_favorites(cursor, user_id)
            cursor.execute(
                """
                SELECT sp.player_id
                FROM shortlist_players sp
                JOIN shortlists s ON s.id = sp.shortlist_id
                WHERE s.user_id = %s AND s.is_default = TRUE
                """,
                (user_id,),
            )
            return [row[0] for row in cursor.fetchall()]

    def member_shortlist_ids(self, user_id, player_id):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT sp.shortlist_id
                FROM shortlist_players sp
                JOIN shortlists s ON s.id = sp.shortlist_id
                WHERE s.user_id = %s AND sp.player_id = %s
                """,
                (user_id, player_id),
            )
            return [row[0] for row in cursor.fetchall()]

    def add_player(self, user_id, shortlist_id, player):
        """Add a validated current player idempotently, retaining identity only."""
        with self._connect() as connection, connection.cursor() as cursor:
            # Ownership check precedes the insert so a foreign ID cannot be used
            # to infer whether some other coach has a list with that number.
            self.get_shortlist_in_cursor(cursor, user_id, shortlist_id)
            cursor.execute(
                """
                INSERT INTO shortlist_players
                    (shortlist_id, player_id, player_name, birth_year, last_known_team)
                SELECT s.id, %s, %s, %s, %s
                FROM shortlists s
                WHERE s.id = %s AND s.user_id = %s
                ON CONFLICT (shortlist_id, player_id) DO NOTHING
                RETURNING player_id
                """,
                (
                    player["player_id"], player.get("player_name"), player.get("birth_year"),
                    player.get("current_team"), shortlist_id, user_id,
                ),
            )
            return cursor.fetchone() is not None

    def remove_player(self, user_id, shortlist_id, player_id):
        with self._connect() as connection, connection.cursor() as cursor:
            self.get_shortlist_in_cursor(cursor, user_id, shortlist_id)
            cursor.execute(
                """
                DELETE FROM shortlist_players sp
                USING shortlists s
                WHERE sp.shortlist_id = s.id
                  AND sp.shortlist_id = %s AND sp.player_id = %s AND s.user_id = %s
                RETURNING sp.player_id
                """,
                (shortlist_id, player_id, user_id),
            )
            return cursor.fetchone() is not None

    def get_shortlist_in_cursor(self, cursor, user_id, shortlist_id):
        cursor.execute(
            """
            SELECT id, name, is_default, created_at, updated_at, 0
            FROM shortlists WHERE id = %s AND user_id = %s
            """,
            (shortlist_id, user_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise ShortlistNotFound("shortlist not found")
        return self._shortlist(row)

    def shortlist_players(self, user_id, shortlist_id):
        self.get_shortlist(user_id, shortlist_id)  # ownership check, no leak
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT sp.player_id, sp.player_name, sp.birth_year, sp.last_known_team, sp.created_at
                FROM shortlist_players sp
                JOIN shortlists s ON s.id = sp.shortlist_id
                WHERE sp.shortlist_id = %s AND s.user_id = %s
                ORDER BY sp.created_at DESC, sp.player_id
                """,
                (shortlist_id, user_id),
            )
            return [
                {
                    "player_id": row[0], "player_name": row[1], "birth_year": row[2],
                    "last_known_team": row[3], "created_at": row[4].isoformat() if row[4] else None,
                }
                for row in cursor.fetchall()
            ]
