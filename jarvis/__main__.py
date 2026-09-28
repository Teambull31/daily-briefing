"""Point d'entrée : `python -m jarvis`."""

import logging
from pathlib import Path

from .bot import Jarvis
from .config import Config, load_dotenv


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # évite d'écrire le token dans les logs
    load_dotenv(Path.cwd() / ".env")
    cfg = Config.from_env()
    if not cfg.allowed_user_ids:
        logging.warning("ALLOWED_USER_IDS vide : mode configuration, seul /id répond.")
    logging.info(
        "Jarvis démarré — modèle %s, agents : %s (défaut : %s)",
        cfg.chat_model, ", ".join(cfg.agents), cfg.default_agent,
    )
    Jarvis(cfg).build_app().run_polling()


if __name__ == "__main__":
    main()
