"""Export the FastAPI contract deterministically for the frontend generator."""

from __future__ import annotations

import json
from pathlib import Path

from neuroagent.api.app import create_app
from neuroagent.application.settings import Settings


def main() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    output = repository_root / "web" / "openapi.json"
    app = create_app(Settings(database_url="sqlite+pysqlite:///:memory:"))
    try:
        document = app.openapi()
    finally:
        app.state.service.close()
    output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()
