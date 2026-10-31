"""Container-Einstieg ``musichub-serve``: Datenbank abwarten, migrieren, API starten."""

from __future__ import annotations

import os

import uvicorn

from musichub.core.config import get_settings
from musichub.core.logging import configure_logging
from musichub.db.migrate import upgrade_to_head, wait_for_database


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    if os.environ.get("MUSICHUB_MIGRATE_ON_START", "true").lower() not in {"0", "false", "no"}:
        wait_for_database(settings.database_url)
        upgrade_to_head(settings.database_url)

    uvicorn.run(
        "musichub.main:app",
        host=os.environ.get("MUSICHUB_HOST", "0.0.0.0"),
        port=int(os.environ.get("MUSICHUB_PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("MUSICHUB_FORWARDED_ALLOW_IPS", "*"),
        server_header=False,
        log_config=None,  # unser Logging-Setup bleibt aktiv
    )


if __name__ == "__main__":
    main()
