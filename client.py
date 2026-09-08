"""HTTP client for football.org.il with DNS override, retries and on-disk caching."""

import hashlib
import logging
import re
import socket
import subprocess
import threading
import time

import certifi
import requests

from . import config

log = logging.getLogger(__name__)

_dns_patched = False
_dns_lock = threading.Lock()
# Candidate IPs for the host, with the one currently in use first.
_pinned_ips = []


def _lookup_via_system():
    try:
        infos = socket.getaddrinfo(config.HOSTNAME, 443, socket.AF_INET)
    except socket.gaierror:
        return []
    return list(dict.fromkeys(info[4][0] for info in infos))


def _lookup_via_public_dns():
    """Resolve the host through a public DNS server, bypassing the system resolver."""
    try:
        out = subprocess.run(
            ["nslookup", config.HOSTNAME, config.PUBLIC_DNS],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("nslookup failed (%s), using fallback IPs", exc)
        return []
    # Skip the leading block describing the DNS server itself.
    body = out.split("Non-authoritative answer:", 1)[-1]
    return re.findall(r"Address:\s*(\d+\.\d+\.\d+\.\d+)", body)


def install_dns_override():
    """Pin the host to a fixed IP for the whole run.

    Some resolvers SERVFAIL on this domain, and even a resolver that works at startup
    can fail mid-run, so the address is resolved once and pinned rather than left to
    per-request DNS. Patching getaddrinfo rather than rewriting URLs keeps SNI and
    certificate verification pointed at the real hostname.
    """
    global _dns_patched
    with _dns_lock:
        if _dns_patched:
            return

        candidates = _lookup_via_system() or _lookup_via_public_dns()
        # Keep the known-good addresses as a last resort behind anything resolved.
        for ip in config.FALLBACK_IPS:
            if ip not in candidates:
                candidates.append(ip)
        _pinned_ips.extend(candidates)
        log.info("pinning %s to %s (%s candidates)",
                 config.HOSTNAME, _pinned_ips[0], len(_pinned_ips))

        original = socket.getaddrinfo

        def patched(host, port, *args, **kwargs):
            if host == config.HOSTNAME and _pinned_ips:
                host = _pinned_ips[0]
            return original(host, port, *args, **kwargs)

        socket.getaddrinfo = patched
        _dns_patched = True


def rotate_pinned_ip():
    """Move to the next candidate address after a connection failure."""
    with _dns_lock:
        if len(_pinned_ips) > 1:
            _pinned_ips.append(_pinned_ips.pop(0))
            log.warning("rotating to %s after connection failure", _pinned_ips[0])


class IFAClient:
    """Thread-safe fetcher with retry/backoff and a content-addressed disk cache."""

    def __init__(self, use_cache=True, cache_dir=None):
        install_dns_override()
        self.use_cache = use_cache
        self.cache_dir = cache_dir or config.CACHE_DIR
        if self.use_cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.stats = {"hits": 0, "fetched": 0, "errors": 0}
        self._stats_lock = threading.Lock()

    @property
    def session(self):
        # One session per thread; requests.Session is not thread-safe.
        if not hasattr(self._local, "session"):
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": config.USER_AGENT,
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept": "*/*",
                    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
                }
            )
            self._local.session = session
        return self._local.session

    def _cache_path(self, path):
        digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:32]
        return self.cache_dir / f"{digest}.txt"

    def _bump(self, key):
        with self._stats_lock:
            self.stats[key] += 1

    def get(self, path, cache=None):
        """Fetch a site-relative path, returning response text or None on failure.

        Pass cache=False for large pages that are cheaper to re-fetch than to store.
        """
        use_cache = self.use_cache if cache is None else (self.use_cache and cache)
        cache_file = self._cache_path(path) if use_cache else None
        if cache_file is not None and cache_file.exists():
            self._bump("hits")
            return cache_file.read_text(encoding="utf-8")

        last_error = None
        for attempt in range(config.MAX_RETRIES):
            try:
                response = self.session.get(
                    config.BASE_URL + path,
                    timeout=config.REQUEST_TIMEOUT,
                    verify=certifi.where(),
                )
                if response.status_code == 200:
                    text = response.text
                    if cache_file is not None:
                        cache_file.write_text(text, encoding="utf-8")
                    self._bump("fetched")
                    time.sleep(config.POLITENESS_DELAY)
                    return text
                # 404 means this league/season simply has no data; don't retry.
                if response.status_code == 404:
                    self._bump("errors")
                    return None
                last_error = f"HTTP {response.status_code}"
            except requests.RequestException as exc:
                last_error = str(exc)
                if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
                    # The pinned address may have gone bad; try the next one.
                    rotate_pinned_ip()

            time.sleep(config.RETRY_BACKOFF ** attempt)

        log.warning("giving up on %s: %s", path, last_error)
        self._bump("errors")
        return None

    def league_tables(self, league_id, season_id, box=0, round_id=1):
        return self.get(
            f"/Components.asmx/League_AllTables?league_id={league_id}"
            f"&season_id={season_id}&box={box}&round_id={round_id}"
        )

    def team_player_stats(self, team_id, season_id):
        return self.get(
            f"/Components.asmx/GetTeamPlayersStatisticsList?teamId={team_id}"
            f'&seasonId={season_id}&isFemale="false"'
        )

    def player_games(self, player_id, season_id):
        return self.get(
            f"/Components.asmx/GetPlayerGames?player_id={player_id}&season_id={season_id}"
        )

    def player_page(self, player_id):
        # ~150KB per page and only one field is needed, so results are checkpointed
        # by the caller rather than cached as raw HTML.
        return self.get(f"/players/player/?player_id={player_id}", cache=False)

    def team_page(self, team_id, season_id):
        # ~190KB per page, of which only the grounds block is used; checkpointed instead.
        return self.get(
            f"/team-details/?season_id={season_id}&team_id={team_id}", cache=False
        )

    def fields_directory(self):
        return self.get("/association/fields/", cache=False)
