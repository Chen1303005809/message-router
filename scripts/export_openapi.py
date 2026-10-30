"""Export FastAPI's OpenAPI document without connecting to a real database."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["OBJECT_STORAGE_BACKEND"] = "local"
os.environ["INITIAL_ADMIN_WECOM_USERID"] = ""
os.environ["AUTH_MODE"] = "development"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kefu.web.app import app  # noqa: E402

destination = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "frontend/openapi.json"
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(
    json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(f"Wrote FastAPI OpenAPI schema to {destination}")
