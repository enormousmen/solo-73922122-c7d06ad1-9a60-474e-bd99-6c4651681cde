"""
builder.py — 增量构建核心

设计要点
========
1. 不可变快照（snapshot）
   每次提交构建时，把当时的全部文件内容写入内容寻址 blob 仓库，
   并在 build 专属目录中用硬链接物化一份完整源码树。
   并行构建各自使用自己提交时的文件内容，互不影响。

2. 目标文件复用（object cache）
   对每个源文件用 `gcc -MM` 求出预处理实际读到的 *项目头文件*
   （仅项目内头文件，系统头如 stdio.h 不参与，编译器/容器固定即可）。
   object key = 编译器标识 + 固定编译参数 + 源文件内容 hash
                + 每个实际依赖头的相对路径与内容 hash
   源文件和预处理实际读到的项目头文件均未变 -> key 不变 -> 复用 .o。

3. 并发只编译一次
   每个 object key 一把锁 + 双检（double-checked）：
   多个并发构建命中同一个 object 时，只有一个真正调用 gcc，
   其余构建等待并直接复用，状态标记为 reused。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 固定的项目结构与编译参数（文件名与编译参数固定，不接受外部覆盖）
# ---------------------------------------------------------------------------

WORKSPACE_ROOT = Path(os.environ.get("BUILD_WORKSPACE", "/workspace/data"))
BLOB_DIR = WORKSPACE_ROOT / "blobs"
BUILDS_DIR = WORKSPACE_ROOT / "builds"
CACHE_DIR = WORKSPACE_ROOT / "objcache"

INCLUDE_DIR = "include"
SOURCE_DIR = "src"

# 相对路径固定的两份源文件
SOURCE_FILES: Tuple[str, ...] = ("src/main.c", "src/util.c")
# 相对路径固定的项目头文件（嵌套引用：diag.h -> diag_util.h -> diag_config.h）
HEADER_FILES: Tuple[str, ...] = (
    "include/diag.h",
    "include/diag_util.h",
    "include/diag_config.h",
)
PROJECT_FILES: Tuple[str, ...] = SOURCE_FILES + HEADER_FILES

# 固定编译参数
GCC = os.environ.get("GCC_BIN", "gcc")
COMPILE_FLAGS: Tuple[str, ...] = ("-std=c11", "-Wall", "-Wextra", "-O2", "-Iinclude")
LINK_FLAGS: Tuple[str, ...] = ("-std=c11", "-O2")
PROGRAM_NAME = "diag_tool"

# 构建状态
PENDING = "pending"
RUNNING = "running"
SUCCESS = "success"
FAILED = "failed"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _gcc_identity() -> str:
    """编译器自身标识，纳入缓存键（容器固定，但保留正确性）。"""
    try:
        out = subprocess.run(
            [GCC, "--version"], capture_output=True, text=True, check=True, timeout=15
        )
        return out.stdout.splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return f"gcc-unavailable:{exc}"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class SourceResult:
    source: str
    status: str                 # "compiled" | "reused" | "failed"
    object_key: Optional[str] = None
    object_relpath: Optional[str] = None
    dependency_headers: List[str] = field(default_factory=list)
    duration_ms: int = 0
    error: Optional[str] = None  # 编译错误（gcc 输出）


@dataclass
class BuildRecord:
    build_id: str
    status: str = PENDING
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    snapshot_dir: Optional[str] = None
    file_hashes: Dict[str, str] = field(default_factory=dict)
    results: List[SourceResult] = field(default_factory=list)
    link_status: Optional[str] = None        # "linked" | "failed" | None
    program_path: Optional[str] = None
    error: Optional[str] = None              # 链接错误 / 汇总错误
    message: str = ""


# ---------------------------------------------------------------------------
# 快照：内容寻址 blob + 硬链接物化
# ---------------------------------------------------------------------------

class SnapshotStore:
    def __init__(self, blob_dir: Path):
        self.blob_dir = blob_dir
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        # 并发提交可能写入同一个 blob，写入过程需串行化
        self._put_lock = threading.Lock()

    def _blob_path(self, digest: str) -> Path:
        return self.blob_dir / digest[:2] / digest[2:4] / digest

    def put(self, data: bytes) -> str:
        digest = _sha256_bytes(data)
        path = self._blob_path(digest)
        if path.exists():
            return digest
        with self._put_lock:
            # 双检：等锁期间可能已由其他线程写入
            if path.exists():
                return digest
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{digest}.{uuid.uuid4().hex}.tmp")
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        return digest

    def materialize(self, files: Dict[str, bytes], target_dir: Path) -> Dict[str, str]:
        """把文件内容物化到 target_dir（硬链接，失败则回退拷贝）。"""
        hashes: Dict[str, str] = {}
        if target_dir.exists():
            shutil.rmtree(target_dir)
        for relpath, data in sorted(files.items()):
            digest = self.put(data)
            hashes[relpath] = digest
            dest = target_dir / relpath
            dest.parent.mkdir(parents=True, exist_ok=True)
            blob = self._blob_path(digest)
            try:
                os.link(blob, dest)
            except OSError:
                shutil.copyfile(blob, dest)
        return hashes


# ---------------------------------------------------------------------------
# 增量构建器
# ---------------------------------------------------------------------------

class IncrementalBuilder:
    def __init__(self, workspace: Path = WORKSPACE_ROOT, max_workers: int = 4):
        self.workspace = workspace
        self.blobs = SnapshotStore(workspace / "blobs")
        self.builds_dir = workspace / "builds"
        self.cache_dir = workspace / "objcache"
        self.builds_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # 每个 object key 一把编译锁
        self._key_locks: Dict[str, threading.Lock] = {}
        self._key_locks_guard = threading.Lock()
        self._gcc_id = _gcc_identity()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="build"
        )

    # ----- 锁管理 ----------------------------------------------------------

    def _lock_for(self, key: str) -> threading.Lock:
        with self._key_locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    # ----- 依赖头解析 ------------------------------------------------------

    @staticmethod
    def _parse_mm_output(mm_output: str, snapshot_dir: Path) -> List[str]:
        """解析 gcc -MM 输出，仅保留快照目录内的项目头文件（去重、排序）。"""
        # 形如: main.o: src/main.c include/diag.h include/diag_util.h ...
        joined = mm_output.replace("\\\n", " ")
        if ":" in joined:
            joined = joined.split(":", 1)[1]
        deps: List[str] = []
        seen = set()
        snap = os.path.realpath(snapshot_dir)
        for token in joined.split():
            dep_path = Path(token)
            if not dep_path.is_absolute():
                dep_path = snapshot_dir / token
            try:
                real = os.path.realpath(dep_path)
            except OSError:
                continue
            # 只统计项目内头文件（位于快照目录中），排除系统头
            if not real.startswith(snap + os.sep):
                continue
            rel = os.path.relpath(real, snap)
            # 只统计项目内头文件（位于快照目录中且为 .h），
            # 排除系统头以及 gcc -MM 一并输出的 .c 源文件本身
            if rel not in seen and rel.endswith(".h"):
                seen.add(rel)
                deps.append(rel)
        return sorted(deps)

    def _header_dependencies(self, source_rel: str, snapshot_dir: Path) -> Tuple[List[str], Optional[str]]:
        """运行 gcc -MM 求预处理实际读到的项目头；失败返回错误信息。"""
        cmd = [GCC, *COMPILE_FLAGS, "-MM", source_rel]
        proc = subprocess.run(
            cmd, cwd=snapshot_dir, capture_output=True, text=True, timeout=60
        )
        if proc.returncode != 0:
            return [], proc.stderr.strip() or f"gcc -MM failed: {proc.returncode}"
        return self._parse_mm_output(proc.stdout, snapshot_dir), None

    # ----- object 缓存键 ---------------------------------------------------

    def _object_key(
        self, source_rel: str, source_hash: str, header_hashes: Dict[str, str]
    ) -> str:
        h = hashlib.sha256()
        h.update(b"gcc|")
        h.update(self._gcc_id.encode())
        h.update(b"|flags|")
        h.update(" ".join(COMPILE_FLAGS).encode())
        h.update(b"|src|")
        h.update(source_rel.encode())
        h.update(b":")
        h.update(source_hash.encode())
        h.update(b"|hdrs|")
        for rel in sorted(header_hashes):
            h.update(rel.encode())
            h.update(b":")
            h.update(header_hashes[rel].encode())
            h.update(b"|")
        return h.hexdigest()

    def _cache_object_path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / key[2:4] / f"{key}.o"

    # ----- 单个源文件编译（带缓存与单次编译保证） --------------------------

    def compile_source(self, source_rel: str, snapshot_dir: Path) -> SourceResult:
        started = time.monotonic()
        result = SourceResult(source=source_rel, status=FAILED)
        src_path = snapshot_dir / source_rel

        # 1) 解析预处理实际读到的项目头
        deps, dep_err = self._header_dependencies(source_rel, snapshot_dir)
        if dep_err is not None:
            result.error = dep_err
            result.duration_ms = int((time.monotonic() - started) * 1000)
            return result
        result.dependency_headers = deps

        # 2) 计算缓存键（源文件 + 实际依赖头内容均参与）
        header_hashes = {rel: _sha256_file(snapshot_dir / rel) for rel in deps}
        source_hash = _sha256_file(src_path)
        key = self._object_key(source_rel, source_hash, header_hashes)
        result.object_key = key

        cache_obj = self._cache_object_path(key)
        build_obj_rel = f"build/{Path(source_rel).stem}.o"
        build_obj = snapshot_dir / build_obj_rel
        build_obj.parent.mkdir(parents=True, exist_ok=True)

        lock = self._lock_for(key)
        with lock:
            # 双检：并发构建命中同一 key 时只编译一次
            if cache_obj.exists():
                # 复制到本构建独立的快照目录（不用硬链接：构建快照会被
                # 单独清理，硬链接会让缓存对象的链接计数/归属变得脆弱）
                try:
                    if build_obj.exists() or build_obj.is_symlink():
                        build_obj.unlink()
                except OSError:
                    pass
                shutil.copyfile(cache_obj, build_obj)
                result.status = "reused"
                result.object_relpath = build_obj_rel
                result.duration_ms = int((time.monotonic() - started) * 1000)
                return result

            # 3) 缓存未命中，真正编译
            cmd = [GCC, *COMPILE_FLAGS, "-c", source_rel, "-o", build_obj_rel]
            proc = subprocess.run(
                cmd, cwd=snapshot_dir, capture_output=True, text=True, timeout=120
            )
            if proc.returncode != 0:
                result.error = (proc.stderr or proc.stdout or "").strip()
                result.duration_ms = int((time.monotonic() - started) * 1000)
                return result

            # 4) 放入共享缓存供后续/并发构建复用
            os.makedirs(cache_obj.parent, exist_ok=True)
            shutil.copyfile(build_obj, cache_obj)
            result.status = "compiled"
            result.object_relpath = build_obj_rel
            result.duration_ms = int((time.monotonic() - started) * 1000)
            return result

    # ----- 链接 ------------------------------------------------------------

    def link_program(self, snapshot_dir: Path, objects: List[str]) -> Tuple[bool, Optional[str]]:
        program_rel = f"build/{PROGRAM_NAME}"
        cmd = [GCC, *LINK_FLAGS, *objects, "-o", program_rel]
        proc = subprocess.run(
            cmd, cwd=snapshot_dir, capture_output=True, text=True, timeout=120
        )
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout or "").strip()
        try:
            os.chmod(snapshot_dir / program_rel, 0o755)
        except OSError:  # pragma: no cover
            pass
        return True, None

    # ----- 一次完整构建 ----------------------------------------------------

    def run_build(self, build_id: str, files: Dict[str, bytes]) -> BuildRecord:
        record = BuildRecord(build_id=build_id, status=RUNNING, started_at=time.time())
        snapshot_dir = self.builds_dir / build_id / "snapshot"
        record.snapshot_dir = str(snapshot_dir)
        try:
            # 提交时固化文件内容：后续编辑不影响本次构建
            record.file_hashes = self.blobs.materialize(files, snapshot_dir)

            # 两份源文件可并行编译（线程池内仍由 key 锁保证单次编译）
            futures = {
                self._executor.submit(self.compile_source, src, snapshot_dir): src
                for src in SOURCE_FILES
            }
            collected: Dict[str, SourceResult] = {}
            for fut in futures:
                src = futures[fut]
                try:
                    collected[src] = fut.result()
                except Exception as exc:  # pragma: no cover - 防御性
                    collected[src] = SourceResult(source=src, status="failed", error=str(exc))
            record.results = [collected[src] for src in SOURCE_FILES]

            failed = [r for r in record.results if r.status == "failed"]
            if failed:
                record.status = FAILED
                record.message = "编译失败"
                record.error = failed[0].error
                record.finished_at = time.time()
                return record

            # 全部目标文件就绪后链接
            objects = [r.object_relpath for r in record.results]
            ok, link_err = self.link_program(snapshot_dir, objects)
            if not ok:
                record.status = FAILED
                record.link_status = "failed"
                record.message = "链接失败"
                record.error = link_err
                record.finished_at = time.time()
                return record

            record.link_status = "linked"
            record.program_path = str(snapshot_dir / "build" / PROGRAM_NAME)
            record.status = SUCCESS
            record.message = "构建成功"
            record.finished_at = time.time()
            return record
        except Exception as exc:  # pragma: no cover - 防御性
            record.status = FAILED
            record.message = "构建异常"
            record.error = str(exc)
            record.finished_at = time.time()
            return record


# ---------------------------------------------------------------------------
# 构建管理器：任务表 + 后台提交
# ---------------------------------------------------------------------------

class BuildManager:
    def __init__(self, builder: Optional[IncrementalBuilder] = None):
        self.builder = builder or IncrementalBuilder()
        self._records: Dict[str, BuildRecord] = {}
        self._guard = threading.Lock()

    def submit(self, files: Dict[str, bytes]) -> str:
        build_id = uuid.uuid4().hex[:12]
        with self._guard:
            self._records[build_id] = BuildRecord(build_id=build_id)
        # 后台线程执行，多个构建可并行
        self.builder._executor.submit(self._run, build_id, files)
        return build_id

    def _run(self, build_id: str, files: Dict[str, bytes]) -> None:
        record = self.builder.run_build(build_id, files)
        with self._guard:
            self._records[build_id] = record

    def get(self, build_id: str) -> Optional[BuildRecord]:
        with self._guard:
            return self._records.get(build_id)

    def list_builds(self) -> List[BuildRecord]:
        with self._guard:
            return sorted(self._records.values(), key=lambda r: r.created_at, reverse=True)


# 单例
manager = BuildManager()
