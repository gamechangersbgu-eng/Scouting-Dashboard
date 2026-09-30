"""Safely refresh the shared Postgres dataset from the scraper's CSV artifacts.

The normal scrape remains the documented three-step scraper workflow: recent
stats/details, full history, then team locations.  This command only composes
those existing entry points with the existing import, validation, parity, and
atomic publication code; it never deploys or performs Git writes.

Usage:
    python -m scripts.refresh_dataset
    python -m scripts.refresh_dataset --no-publish
    python -m scripts.refresh_dataset --dataset-id 7 --skip-scrape --skip-import
"""

import argparse
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ifa_scraper import config, db
from ifa_scraper import history as history_scraper
from ifa_scraper import run as recent_scraper
from ifa_scraper import venues as venues_scraper
from scripts.check_parity import run_parity_check
from scripts.import_canonical_dataset import (
    ensure_required_source_files,
    import_dataset,
)
from scripts.publish_dataset import publish
from scripts.validate_dataset import validate_existing_dataset

log = logging.getLogger("scripts.refresh_dataset")
DEFAULT_PARITY_SAMPLE_SIZE = 1000


class RefreshFailed(RuntimeError):
    """A refresh stage failed before publication could safely proceed."""

    def __init__(self, stage, cause, candidate_dataset_id=None):
        super().__init__(str(cause))
        self.stage = stage
        self.candidate_dataset_id = candidate_dataset_id


@dataclass
class RefreshSummary:
    previous_dataset_id: int | None
    dataset_id: int
    row_counts: dict
    parity_sample_size: int | None
    published: bool


def current_git_sha():
    """Return the source revision recorded on a new dataset version."""
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=config.PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    sha = completed.stdout.strip()
    if not sha:
        raise RuntimeError("git rev-parse HEAD returned no commit SHA")
    return sha


def current_live_dataset_id(database_url=None):
    with db.connect(database_url) as connection:
        return db.current_dataset_id(connection)


def dataset_row_counts(dataset_id, database_url=None):
    """Read the importer's already-recorded counts for the final summary."""
    with db.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT row_counts FROM dataset_versions WHERE dataset_id = %s",
                (dataset_id,),
            )
            row = cursor.fetchone()
    return (row[0] or {}) if row else {}


def run_normal_scrape(data_dir):
    """Run the existing documented scraper workflow in its required order."""
    recent_scraper.main(argv=[], data_dir=data_dir)
    history_scraper.main(argv=[], data_dir=data_dir)
    venues_scraper.main(argv=[], data_dir=data_dir)


def _raise_stage_failure(stage, cause, candidate_dataset_id=None):
    if isinstance(cause, RefreshFailed):
        raise cause
    failure = RefreshFailed(stage, cause, candidate_dataset_id)
    if isinstance(cause, BaseException):
        raise failure from cause
    raise failure


def refresh_dataset(
    data_dir=None,
    *,
    skip_scrape=False,
    skip_import=False,
    skip_parity=False,
    parity_sample_size=DEFAULT_PARITY_SAMPLE_SIZE,
    publish_candidate=True,
    scraper_git_sha=None,
    dataset_id=None,
    database_url=None,
):
    """Run a candidate refresh and publish only after every gate passes."""
    if skip_import != (dataset_id is not None):
        raise ValueError("--skip-import and --dataset-id must be supplied together")
    if parity_sample_size < 0:
        raise ValueError("parity_sample_size must be non-negative")

    data_dir = Path(data_dir) if data_dir is not None else config.DATA_DIR
    candidate_dataset_id = dataset_id
    try:
        previous_dataset_id = current_live_dataset_id(database_url)
    except Exception as exc:
        _raise_stage_failure("preflight", exc)

    if not skip_scrape:
        try:
            run_normal_scrape(data_dir)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            _raise_stage_failure("scrape", exc)

    try:
        ensure_required_source_files(data_dir)
    except Exception as exc:
        _raise_stage_failure("source artifact check", exc, candidate_dataset_id)

    if skip_import:
        try:
            validation_result = validate_existing_dataset(
                candidate_dataset_id, database_url=database_url
            )
        except Exception as exc:
            _raise_stage_failure("validation", exc, candidate_dataset_id)
    else:
        try:
            sha = scraper_git_sha or current_git_sha()
            candidate_dataset_id, validation_result = import_dataset(
                database_url=database_url,
                data_dir=data_dir,
                scraper_git_sha=sha,
            )
        except Exception as exc:
            _raise_stage_failure("import", exc, candidate_dataset_id)

    if not validation_result.ok:
        _raise_stage_failure(
            "validation",
            "; ".join(validation_result.errors) or "candidate validation failed",
            candidate_dataset_id,
        )

    sampled_players = None
    if not skip_parity:
        try:
            mismatches, sampled_players = run_parity_check(
                data_dir=data_dir,
                database_url=database_url,
                sample_size=parity_sample_size,
                dataset_id=candidate_dataset_id,
            )
        except Exception as exc:
            _raise_stage_failure("parity", exc, candidate_dataset_id)
        if mismatches:
            _raise_stage_failure(
                "parity",
                f"{len(mismatches)} mismatch(es): " + "; ".join(mismatches[:5]),
                candidate_dataset_id,
            )

    try:
        row_counts = dataset_row_counts(candidate_dataset_id, database_url)
    except Exception as exc:
        _raise_stage_failure("summary", exc, candidate_dataset_id)

    if publish_candidate:
        try:
            publish(candidate_dataset_id, database_url=database_url)
        except Exception as exc:
            _raise_stage_failure("publish", exc, candidate_dataset_id)

    return RefreshSummary(
        previous_dataset_id=previous_dataset_id,
        dataset_id=candidate_dataset_id,
        row_counts=row_counts,
        parity_sample_size=sampled_players,
        published=publish_candidate,
    )


def _print_summary(summary):
    print("Refresh complete")
    print(f"Previous live dataset: {summary.previous_dataset_id}")
    print(f"New dataset: {summary.dataset_id}")
    for label, key in (
        ("Raw recent rows", "raw_season_rows"),
        ("Raw history rows", "raw_history_rows"),
        ("Canonical rows", "player_team_seasons_rows"),
        ("Observation rows", "player_team_season_observations_rows"),
    ):
        if key in summary.row_counts:
            print(f"{label}: {summary.row_counts[key]}")
    if summary.parity_sample_size is not None:
        print(f"Parity sample: {summary.parity_sample_size} players, 0 mismatches")
    if summary.published:
        print(f"Published dataset: {summary.dataset_id}")
    else:
        print("Not published (--no-publish)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--skip-scrape", action="store_true")
    parser.add_argument("--skip-import", action="store_true")
    parser.add_argument("--dataset-id", type=int, default=None)
    parser.add_argument("--skip-parity", action="store_true")
    parser.add_argument("--parity-sample-size", type=int, default=DEFAULT_PARITY_SAMPLE_SIZE)
    parser.add_argument("--no-publish", action="store_true")
    parser.add_argument("--scraper-git-sha", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        summary = refresh_dataset(
            data_dir=args.data_dir,
            skip_scrape=args.skip_scrape,
            skip_import=args.skip_import,
            skip_parity=args.skip_parity,
            parity_sample_size=args.parity_sample_size,
            publish_candidate=not args.no_publish,
            scraper_git_sha=args.scraper_git_sha,
            dataset_id=args.dataset_id,
        )
    except RefreshFailed as exc:
        print(f"Refresh failed during {exc.stage}.")
        if exc.candidate_dataset_id is not None:
            print(f"Candidate dataset: {exc.candidate_dataset_id}")
        print("The previously live dataset remains unchanged.")
        print(f"Reason: {exc}")
        raise SystemExit(1)
    _print_summary(summary)


if __name__ == "__main__":
    main()
