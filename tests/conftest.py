"""pytest 公共夹具：隔离的临时工作区 + FastAPI TestClient"""

import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.builder import IncrementalBuilder, BuildManager  # noqa: E402
from app.web import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILD_WORKSPACE", str(tmp_path / "data"))
    builder = IncrementalBuilder(workspace=Path(os.environ["BUILD_WORKSPACE"]))
    manager = BuildManager(builder=builder)
    app = create_app(build_manager=manager)
    with TestClient(app) as c:
        c.app_state = app.state
        c.manager = manager
        c.builder = builder
        yield c


def wait_build(c, build_id, timeout=60):
    """轮询直到构建结束。"""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get(f"/api/builds/{build_id}")
        assert r.status_code == 200
        body = r.json()
        if body["status"] in ("success", "failed"):
            return body
        time.sleep(0.1)
    raise AssertionError("构建超时未结束")
