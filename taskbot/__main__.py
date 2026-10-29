"""Entry point.

    uv run taskbot                      start the bot
    uv run taskbot users                list users
    uv run taskbot users add ID [NAME]  allow someone (--admin to make them admin)
    uv run taskbot users remove ID      take access away
"""

import argparse
import logging

from .config import Config
from .db import DB


def run_bot() -> None:
    from telegram import Update

    from .bot import create_app

    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = Config.from_env()
    db = DB(cfg.db_path)
    if not (cfg.admin_ids or cfg.allowed_users or db.admins()):
        logging.warning("No admin yet: the first person who writes to the bot becomes admin")
    app = create_app(cfg, db)
    app.run_polling(allowed_updates=Update.ALL_TYPES)  # long polling: no public IP needed


def users_cli(args: argparse.Namespace) -> None:
    cfg = Config.from_env(require_token=False)
    db = DB(cfg.db_path)
    if args.action == "add":
        u = db.save_user(args.id, "allowed", args.name, is_admin=True if args.admin else None)
        print(f"✅ {u.label} ({u.id}) allowed" + (" as admin" if u.is_admin else ""))
        print("Takes effect immediately, no restart needed.")
    elif args.action == "remove":
        u = db.user(args.id)
        if u is None:
            print(f"{args.id} is not in the list")
        else:
            db.save_user(args.id, "blocked", is_admin=False)
            print(f"🚫 {u.label} ({u.id}) removed")
    else:
        users = db.users()
        if not users:
            print("No users yet. The first person who writes to the bot becomes admin.")
        for u in users:
            print(f"{u.id:>12}  {u.status:<8} {'admin' if u.is_admin else '     '}  {u.label}")
        extra = sorted(cfg.admin_ids | cfg.allowed_users)
        if extra:
            print(f"Also fixed in .env: {', '.join(map(str, extra))}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="taskbot", description="Shared to-do list bot for Telegram")
    sub = parser.add_subparsers(dest="command")
    users = sub.add_parser("users", help="manage who can use the bot")
    users_sub = users.add_subparsers(dest="action")
    users_sub.add_parser("list", help="list users (default)")
    add = users_sub.add_parser("add", help="allow a Telegram user id")
    add.add_argument("id", type=int)
    add.add_argument("name", nargs="?")
    add.add_argument("--admin", action="store_true", help="also make them admin")
    rm = users_sub.add_parser("remove", help="take access away")
    rm.add_argument("id", type=int)

    args = parser.parse_args()
    if args.command == "users":
        users_cli(args)
    else:
        run_bot()


if __name__ == "__main__":
    main()
