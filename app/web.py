"""web.py — FastAPI 应用：在线编辑、并行增量构建、结果查询与程序下载"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .builder import (
    COMPILE_FLAGS,
    GCC,
    HEADER_FILES,
    LINK_FLAGS,
    PROGRAM_NAME,
    SOURCE_FILES,
    BuildManager,
    IncrementalBuilder,
)
from .initial_project import load_initial_files

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


class BuildSubmit(BaseModel):
    # 相对路径 -> 文件文本内容；仅接受固定文件名
    files: Dict[str, str] = Field(default_factory=dict)
    note: Optional[str] = None


class SourceResultOut(BaseModel):
    source: str
    status: str
    object_key: Optional[str] = None
    object_relpath: Optional[str] = None
    dependency_headers: List[str]
    duration_ms: int
    error: Optional[str] = None


class BuildOut(BaseModel):
    build_id: str
    status: str
    created_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    file_hashes: Dict[str, str]
    results: List[SourceResultOut]
    link_status: Optional[str] = None
    link_error: Optional[str] = None
    program_available: bool
    program_name: str
    message: str
    error: Optional[str] = None
    note: Optional[str] = None


def create_app(build_manager: Optional[BuildManager] = None) -> FastAPI:
    app = FastAPI(title="C 诊断工具增量构建平台", version="1.0.0")
    app.state.manager = build_manager or BuildManager()
    # 当前编辑器中的文件内容（可在线修改）；构建使用提交瞬间的快照
    app.state.editor_files: Dict[str, bytes] = load_initial_files()
    app.state.editor_lock = threading.Lock()
    app.state.notes: Dict[str, str] = {}

    def _record_to_out(build_id: str, record) -> BuildOut:
        return BuildOut(
            build_id=record.build_id,
            status=record.status,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            file_hashes=record.file_hashes,
            results=[
                SourceResultOut(
                    source=r.source,
                    status=r.status,
                    object_key=r.object_key,
                    object_relpath=r.object_relpath,
                    dependency_headers=r.dependency_headers,
                    duration_ms=r.duration_ms,
                    error=r.error,
                )
                for r in record.results
            ],
            link_status=record.link_status,
            link_error=record.error if record.link_status == "failed" else None,
            program_available=record.status == "success" and bool(record.program_path),
            program_name=PROGRAM_NAME,
            message=record.message,
            note=app.state.notes.get(build_id),
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((TEMPLATES_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/files")
    def get_files() -> dict:
        with app.state.editor_lock:
            files = {k: v.decode("utf-8", errors="replace") for k, v in app.state.editor_files.items()}
        return {
            "files": files,
            "source_files": list(SOURCE_FILES),
            "header_files": list(HEADER_FILES),
            "compiler": GCC,
            "compile_flags": list(COMPILE_FLAGS),
            "link_flags": list(LINK_FLAGS),
            "program_name": PROGRAM_NAME,
        }

    @app.put("/api/files")
    def update_files(payload: BuildSubmit) -> dict:
        invalid = [name for name in payload.files if name not in set(SOURCE_FILES + HEADER_FILES)]
        if invalid:
            raise HTTPException(status_code=400, detail=f"不允许的文件名: {invalid}")
        with app.state.editor_lock:
            for name, text in payload.files.items():
                app.state.editor_files[name] = text.encode("utf-8")
        return {"saved": sorted(payload.files.keys())}

    @app.post("/api/builds", status_code=201)
    def create_build(payload: BuildSubmit) -> dict:
        # 提交时把 payload 中的改动先落入编辑器，再对全部文件做不可变快照
        fixed = set(SOURCE_FILES + HEADER_FILES)
        invalid = [name for name in payload.files if name not in fixed]
        if invalid:
            raise HTTPException(status_code=400, detail=f"不允许的文件名: {invalid}")
        with app.state.editor_lock:
            for name, text in payload.files.items():
                app.state.editor_files[name] = text.encode("utf-8")
            snapshot = dict(app.state.editor_files)
        missing = sorted(fixed - set(snapshot.keys()))
        if missing:
            raise HTTPException(status_code=400, detail=f"缺少文件: {missing}")
        build_id = app.state.manager.submit(snapshot)
        if payload.note:
            app.state.notes[build_id] = payload.note
        return {"build_id": build_id}

    @app.get("/api/builds")
    def list_builds() -> dict:
        records = app.state.manager.list_builds()
        return {"builds": [_record_to_out(r.build_id, r).model_dump() for r in records]}

    @app.get("/api/builds/{build_id}")
    def get_build(build_id: str) -> dict:
        record = app.state.manager.get(build_id)
        if record is None:
            raise HTTPException(status_code=404, detail="构建不存在")
        return _record_to_out(build_id, record).model_dump()

    @app.get("/api/builds/{build_id}/download")
    def download_program(build_id: str):
        record = app.state.manager.get(build_id)
        if record is None:
            raise HTTPException(status_code=404, detail="构建不存在")
        if record.status != "success" or not record.program_path:
            raise HTTPException(status_code=409, detail="程序尚未成功链接，无法下载")
        path = Path(record.program_path)
        if not path.exists():
            raise HTTPException(status_code=410, detail="程序文件已不存在")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=f"{PROGRAM_NAME}-{build_id}",
        )

    @app.exception_handler(Exception)
    async def unhandled(_request, exc):  # pragma: no cover - 防御性
        return JSONResponse(status_code=500, content={"detail": f"服务器内部错误: {exc}"})

    return app


app = create_app()
