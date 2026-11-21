"""增量构建平台端到端测试（FastAPI TestClient + 真实 GCC）"""

import os
import stat
import subprocess

import pytest

from conftest import wait_build


def test_initial_files_and_fixed_params(client):
    r = client.get("/api/files")
    assert r.status_code == 200
    data = r.json()
    assert data["source_files"] == ["src/main.c", "src/util.c"]
    assert set(data["header_files"]) == {
        "include/diag.h", "include/diag_util.h", "include/diag_config.h"
    }
    assert data["compile_flags"] == ["-std=c11", "-Wall", "-Wextra", "-O2", "-Iinclude"]
    assert "DIAG_CRITICAL_THRESHOLD" in data["files"]["include/diag_config.h"]


def _submit(client, overrides=None, note=None):
    files = client.get("/api/files").json()["files"]
    if overrides:
        files.update(overrides)
    r = client.post("/api/builds", json={"files": files, "note": note})
    assert r.status_code == 201, r.text
    return r.json()["build_id"], files


def test_first_build_compiles_all_and_download_runs(client, tmp_path):
    build_id, _ = _submit(client, note="基线构建")
    body = wait_build(client, build_id)
    assert body["status"] == "success", body
    assert {r["source"]: r["status"] for r in body["results"]} == {
        "src/main.c": "compiled", "src/util.c": "compiled"
    }
    # 预处理实际读到的项目头（嵌套头都应被 gcc -MM 捕获）
    deps = {r["source"]: r["dependency_headers"] for r in body["results"]}
    assert "include/diag.h" in deps["src/main.c"]
    assert "include/diag_util.h" in deps["src/main.c"]
    assert "include/diag_config.h" in deps["src/main.c"]
    assert "include/diag_config.h" in deps["src/util.c"]
    assert body["program_available"] is True

    # 下载并实际运行程序
    r = client.get(f"/api/builds/{build_id}/download")
    assert r.status_code == 200
    prog = tmp_path / "diag_tool"
    prog.write_bytes(r.content)
    prog.chmod(prog.stat().st_mode | stat.S_IEXEC)
    out = subprocess.run([str(prog)], capture_output=True, text=True, timeout=15)
    assert out.returncode == 0
    assert "c-diag-tool 1.0.0" in out.stdout
    assert "critical readings: 1" in out.stdout


def test_unchanged_rebuild_reuses_everything(client):
    bid1, _ = _submit(client)
    b1 = wait_build(client, bid1)
    bid2, _ = _submit(client)
    b2 = wait_build(client, bid2)
    assert b2["status"] == "success"
    assert all(r["status"] == "reused" for r in b2["results"])
    # 缓存键与第一次完全一致
    k1 = {r["source"]: r["object_key"] for r in b1["results"]}
    k2 = {r["source"]: r["object_key"] for r in b2["results"]}
    assert k1 == k2


def test_header_change_rebuilds_all_sources(client):
    bid1, files = _submit(client)
    wait_build(client, bid1)

    # 修改公共头：阈值 80 -> 50（两个源文件的预处理结果都受影响）
    files["include/diag_config.h"] = files["include/diag_config.h"].replace(
        "#define DIAG_CRITICAL_THRESHOLD 80",
        "#define DIAG_CRITICAL_THRESHOLD 50",
    )
    r = client.post("/api/builds", json={"files": files, "note": "改公共头"})
    b2 = wait_build(client, r.json()["build_id"])
    assert b2["status"] == "success"
    assert all(r["status"] == "compiled" for r in b2["results"])

    # 再次原样构建 -> 全部复用
    r = client.post("/api/builds", json={"files": files})
    b3 = wait_build(client, r.json()["build_id"])
    assert all(r["status"] == "reused" for r in b3["results"])


def test_one_source_change_rebuilds_only_that_source(client):
    bid1, files = _submit(client)
    wait_build(client, bid1)

    files["src/util.c"] = files["src/util.c"].replace(
        "return value >= DIAG_CRITICAL_THRESHOLD;",
        "return value > DIAG_CRITICAL_THRESHOLD;",
    )
    r = client.post("/api/builds", json={"files": files, "note": "只改 util.c"})
    b2 = wait_build(client, r.json()["build_id"])
    status = {x["source"]: x["status"] for x in b2["results"]}
    assert status == {"src/util.c": "compiled", "src/main.c": "reused"}


def test_compile_error_is_reported_per_source(client):
    bid1, files = _submit(client)
    wait_build(client, bid1)

    # 制造一个真正的语法错误（仅 return; 在 int 函数里只是警告）
    files["src/util.c"] += "\nint broken_syntax(void) { this is not C }\n"
    r = client.post("/api/builds", json={"files": files, "note": "编译错误演示"})
    body = wait_build(client, r.json()["build_id"])
    assert body["status"] == "failed"
    failed = [x for x in body["results"] if x["source"] == "src/util.c"][0]
    assert failed["status"] == "failed"
    assert "error" in failed["error"]
    # 失败构建不允许下载
    dl = client.get(f"/api/builds/{body['build_id']}/download")
    assert dl.status_code == 409


def test_link_error_is_reported(client):
    bid1, files = _submit(client)
    wait_build(client, bid1)

    # main.c 不再定义 main，链接阶段报 undefined reference
    files["src/main.c"] = files["src/main.c"].replace(
        "int main(void)", "int diag_main_unused(void)"
    )
    r = client.post("/api/builds", json={"files": files, "note": "链接错误演示"})
    body = wait_build(client, r.json()["build_id"])
    assert body["status"] == "failed"
    assert body["link_status"] == "failed"
    assert "main" in (body["link_error"] or "")


def test_missing_header_detected_via_mm(client):
    bid1, files = _submit(client)
    wait_build(client, bid1)

    files["src/main.c"] = files["src/main.c"].replace(
        '#include "diag.h"', '#include "diag.h"\n#include "no_such_header.h"'
    )
    r = client.post("/api/builds", json={"files": files})
    body = wait_build(client, r.json()["build_id"])
    assert body["status"] == "failed"
    res = [x for x in body["results"] if x["source"] == "src/main.c"][0]
    assert res["status"] == "failed"
    assert "no_such_header.h" in res["error"]


def test_parallel_builds_single_compilation_and_isolation(client):
    """并发构建：相同输入只编译一次；构建期间改文件不影响在途构建。"""
    base_files = client.get("/api/files").json()["files"]

    # 同时提交三个相同内容的构建
    ids = []
    for i in range(3):
        r = client.post("/api/builds", json={"files": dict(base_files),
                                             "note": f"并行-{i}"})
        ids.append(r.json()["build_id"])
    # 构建进行中修改编辑器文件（不应影响任何在途快照）
    client.put("/api/files", json={
        "files": {"include/diag_config.h":
                  base_files["include/diag_config.h"].replace("80", "81")}
    })
    bodies = [wait_build(client, bid) for bid in ids]
    for b in bodies:
        assert b["status"] == "success"

    # 三个构建 object_key 一致；且只有一个 compiled，其余两个 reused（单次编译）
    keys = {r["object_key"] for b in bodies for r in b["results"]}
    assert len(keys) == 2  # 每个源文件一个 key
    for source in ("src/main.c", "src/util.c"):
        statuses = [next(r for r in b["results"] if r["source"] == source)["status"]
                    for b in bodies]
        assert sorted(statuses) == ["compiled", "reused", "reused"]

    # 在途构建看到的仍是提交时的内容（快照隔离），程序可正常下载
    for b in bodies:
        dl = client.get(f"/api/builds/{b['build_id']}/download")
        assert dl.status_code == 200

    # 之后的新构建使用了新编辑器内容 -> 公共头变化，全部重编
    r = client.post("/api/builds", json={"files": client.get("/api/files").json()["files"]})
    later = wait_build(client, r.json()["build_id"])
    assert all(x["status"] == "compiled" for x in later["results"])


def test_reject_unknown_filename(client):
    r = client.post("/api/builds", json={"files": {"src/evil.c": "int x;"}})
    assert r.status_code == 400
    r2 = client.put("/api/files", json={"files": {"../escape.h": "x"}})
    assert r2.status_code == 400


def test_parallel_failing_builds_are_not_cached(client):
    """并发失败构建不得污染缓存；修复后重新提交应能成功编译。"""
    files = client.get("/api/files").json()["files"]
    files["src/util.c"] += "\nvoid oops( { bad syntax\n"
    ids = [
        client.post("/api/builds", json={"files": dict(files)}).json()["build_id"]
        for _ in range(3)
    ]
    bodies = [wait_build(client, bid) for bid in ids]
    assert all(b["status"] == "failed" for b in bodies)
    for b in bodies:
        row = next(r for r in b["results"] if r["source"] == "src/util.c")
        assert row["status"] == "failed" and row["object_relpath"] is None

    # 失败的 util.c 不得写入缓存（main.c 正常，其 .o 允许存在）
    util_rows = [next(r for r in b["results"] if r["source"] == "src/util.c")
                 for b in bodies]
    cached_keys = {p.stem for p in client.builder.cache_dir.rglob("*.o")}
    assert all(r["object_key"] not in cached_keys for r in util_rows)

    # 修复编辑器内容后重新提交：应重新编译并成功
    good = client.get("/api/files").json()["files"]  # 编辑器仍是坏代码
    good["src/util.c"] = load_good_util()
    ok = wait_build(client, client.post("/api/builds", json={"files": good}).json()["build_id"])
    assert ok["status"] == "success"
    statuses = {r["source"]: r["status"] for r in ok["results"]}
    # 坏代码从未入缓存 -> util.c 必须真正重编；main.c 此前成功过 -> 复用
    assert statuses == {"src/util.c": "compiled", "src/main.c": "reused"}


def load_good_util() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent
            / "app/c_project/src/util.c").read_text(encoding="utf-8")


def test_build_list_and_404(client):
    bid, _ = _submit(client)
    wait_build(client, bid)
    r = client.get("/api/builds")
    assert r.status_code == 200
    assert any(b["build_id"] == bid for b in r.json()["builds"])
    assert client.get("/api/builds/nonexistent").status_code == 404
