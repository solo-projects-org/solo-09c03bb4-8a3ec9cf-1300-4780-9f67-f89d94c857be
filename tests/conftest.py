"""Pytest fixtures: isolated data directory per test, manually driven worker."""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEID_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEID_DB_PATH", str(tmp_path / "data" / "app.db"))
    monkeypatch.setenv("DEID_UPLOAD_DIR", str(tmp_path / "data" / "uploads"))
    monkeypatch.setenv("DEID_PACKAGE_DIR", str(tmp_path / "data" / "packages"))
    monkeypatch.setenv("DEID_WORKER_INTERVAL", "3600")  # never auto-tick in tests

    # Reload config so the env vars take effect, then re-import dependent modules.
    from app import config as config_module
    importlib.reload(config_module)
    from app import database, deid, services, worker as worker_module, main as main_module
    importlib.reload(database)
    importlib.reload(deid)
    importlib.reload(services)
    importlib.reload(worker_module)
    importlib.reload(main_module)

    database.init_db()

    from fastapi.testclient import TestClient

    with TestClient(main_module.app) as client:
        # The startup event launched a worker with the huge interval; stop it so
        # tests drive package generation deterministically.
        main_module.worker.stop()

        def tick() -> bool:
            return main_module.worker.run_once()

        yield {
            "client": client,
            "tick": tick,
            "modules": {
                "config": config_module,
                "database": database,
                "services": services,
                "main": main_module,
            },
            "base": tmp_path,
        }
