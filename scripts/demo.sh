#!/usr/bin/env bash
# 端到端演示脚本：用 curl 驱动完整增量构建流程（需要服务已在 $BASE 上运行）
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

jq_pick() { python3 -c 'import sys,json;d=json.load(sys.stdin);print(eval(sys.argv[1]))' "$1"; }

wait_build() {
  local bid="$1"
  for _ in $(seq 1 200); do
    curl -s "$BASE/api/builds/$bid" > "$WORK/b.json"
    local st; st="$(jq_pick 'd["status"]' < "$WORK/b.json")"
    if [[ "$st" == "success" || "$st" == "failed" ]]; then cat "$WORK/b.json"; return; fi
    sleep 0.2
  done
  echo "构建超时" >&2; exit 1
}

show() {
  python3 - "$1" <<'PY'
import json, sys
b = json.load(open(sys.argv[1]))
print(f"[{b['note']}] {b['build_id']} -> {b['status']} ({b['message']})")
for r in b["results"]:
    print(f"  {r['source']:12s} {r['status']:8s} 依赖头={','.join(r['dependency_headers'])} key={r['object_key'][:10]}")
for r in b["results"]:
    if r.get("error"):
        print("  编译错误:\n" + "\n".join("    " + l for l in r["error"].splitlines()[:6]))
if b.get("link_error"):
    print("  链接错误:\n" + "\n".join("    " + l for l in b["link_error"].splitlines()[:6]))
PY
}

echo "== 获取当前文件 =="
curl -s "$BASE/api/files" > "$WORK/files.json"
python3 -c 'import json;d=json.load(open("'$WORK'/files.json"));print("源文件:",d["source_files"]);print("头文件:",d["header_files"]);print("编译参数:"," ".join(d["compile_flags"]))'

submit() { curl -s -X POST "$BASE/api/builds" -H 'Content-Type: application/json' -d "$1" | jq_pick 'd["build_id"]'; }

echo; echo "== 1. 基线构建（预期两份源文件均 compiled）=="
B1=$(submit "$(python3 -c 'import json;d=json.load(open("'$WORK'/files.json"));print(json.dumps({"files":d["files"],"note":"1-基线"}))')")
wait_build "$B1" > "$WORK/b1.json"; show "$WORK/b1.json"

echo; echo "== 2. 无改动再构建（预期全部 reused，object key 不变）=="
B2=$(submit "$(python3 -c 'import json;d=json.load(open("'$WORK'/files.json"));print(json.dumps({"files":d["files"],"note":"2-无改动"}))')")
wait_build "$B2" > "$WORK/b2.json"; show "$WORK/b2.json"

echo; echo "== 3. 只修改 src/util.c（预期仅 util.c 重编，main.c 复用）=="
python3 - <<PY
import json
d=json.load(open("$WORK/files.json"))
d["files"]["src/util.c"]=d["files"]["src/util.c"].replace(
  "return value >= DIAG_CRITICAL_THRESHOLD;",
  "return value > DIAG_CRITICAL_THRESHOLD;")
json.dump({"files":d["files"],"note":"3-只改util.c"}, open("$WORK/req3.json","w"))
PY
B3=$(submit "$(cat "$WORK/req3.json")")
wait_build "$B3" > "$WORK/b3.json"; show "$WORK/b3.json"

echo; echo "== 4. 修改公共头 diag_config.h 阈值（嵌套头，预期两份源文件都重编）=="
python3 - <<PY
import json
d=json.load(open("$WORK/req3.json"))
d["files"]["include/diag_config.h"]=d["files"]["include/diag_config.h"].replace(
  "#define DIAG_CRITICAL_THRESHOLD 80",
  "#define DIAG_CRITICAL_THRESHOLD 50")
d["note"]="4-改公共头"
json.dump(d, open("$WORK/req4.json","w"))
PY
B4=$(submit "$(cat "$WORK/req4.json")")
wait_build "$B4" > "$WORK/b4.json"; show "$WORK/b4.json"

echo; echo "== 5. 注入编译错误（预期失败并回传 gcc 错误，下载被拒绝 409）=="
python3 - <<PY
import json
d=json.load(open("$WORK/req4.json"))
d["files"]["src/main.c"] += "\nvoid oops( { bad syntax\n"
d["note"]="5-编译错误"
json.dump(d, open("$WORK/req5.json","w"))
PY
B5=$(submit "$(cat "$WORK/req5.json")")
wait_build "$B5" > "$WORK/b5.json"; show "$WORK/b5.json"
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/builds/$B5/download")
echo "  失败构建下载 HTTP 状态: $code（预期 409）"

echo; echo "== 6. 成功构建后下载并运行程序 =="
B6=$(submit "$(cat "$WORK/req4.json" | python3 -c 'import json,sys;d=json.load(sys.stdin);d["note"]="6-成功可下载";print(json.dumps(d))')")
wait_build "$B6" > "$WORK/b6.json"; show "$WORK/b6.json"
curl -s "$BASE/api/builds/$B6/download" -o "$WORK/diag_tool"
chmod +x "$WORK/diag_tool"
echo "  程序运行输出:"; "$WORK/diag_tool" | sed 's/^/    /'

echo; echo "演示完成。浏览器打开 $BASE 可在网页上重复以上操作。"
