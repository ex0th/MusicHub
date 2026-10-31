# MusicHub Backend

FastAPI-Backend und Hintergrund-Worker von MusicHub. Projektüberblick, Start und
Architektur stehen in der [README im Repo-Root](../README.md) und unter [`docs/`](../docs/).

```bash
uv sync                                  # Abhängigkeiten inkl. Dev-Tools
uv run alembic upgrade head              # Migrationen
uv run uvicorn musichub.main:app --reload
uv run musichub-worker                   # Hintergrundjobs
uv run pytest                            # Tests (DB-Tests brauchen TEST_DATABASE_URL)
uv run ruff check . && uv run ruff format --check .
```
