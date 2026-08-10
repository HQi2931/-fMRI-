"""Consistent SQLite metadata backup without copying raw or derived images."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    destination = args.destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as destination_db:
        source_db.backup(destination_db)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "database_file": destination.name,
        "sha256": digest,
        "includes_research_data": True,
        "contains_sensitive_research_metadata": True,
        "contains_demographics_mappings": True,
        "contains_absolute_source_paths": True,
        "includes_raw_or_derived_imaging_files": False,
        "handling_notice": (
            "Protect this backup as sensitive research data: the SQLite database may contain "
            "subject identifiers, demographics mappings, and absolute source paths."
        ),
    }
    destination.with_suffix(".json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
