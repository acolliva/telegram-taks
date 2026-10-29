"""Run with: python -m taskbot"""

import logging

from telegram import Update

from .bot import create_app
from .config import Config
from .db import DB


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = Config.from_env()
    if not cfg.allowed_users:
        logging.warning("ALLOWED_USERS is empty: write to the bot to get your id, then add it to .env")
    app = create_app(cfg, DB(cfg.db_path))
    app.run_polling(allowed_updates=Update.ALL_TYPES)  # long polling: no public IP needed


if __name__ == "__main__":
    main()
