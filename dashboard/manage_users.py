"""Create the authentication schema and synchronise an authoritative users CSV."""

import argparse
import os

from .auth import AuthenticationUnavailable, InvalidUserImport, PostgresUserStore, load_user_csv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("migrate", "sync"))
    parser.add_argument("--csv", type=str, help="CSV containing username,password")
    parser.add_argument("--dry-run", action="store_true", help="show sync changes without writing")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()

    if args.command == "sync" and not args.csv:
        parser.error("sync requires --csv")
    if args.command == "migrate" and (args.csv or args.dry_run):
        parser.error("migrate does not accept --csv or --dry-run")

    store = PostgresUserStore(args.database_url)
    try:
        store.ensure_schema()
        if args.command == "migrate":
            print("users schema is ready")
            return
        users = load_user_csv(args.csv)
        stats = store.sync(users, dry_run=args.dry_run)
    except (AuthenticationUnavailable, InvalidUserImport) as exc:
        parser.error(str(exc))

    mode = "dry run" if args.dry_run else "synchronised"
    print(
        f"{mode}: {len(users)} supplied | created {stats['created']} | "
        f"updated {stats['updated']} | reactivated {stats['reactivated']} | "
        f"deactivated {stats['deactivated']}"
    )


if __name__ == "__main__":
    main()
