# C 诊断工具 · 增量构建网页平台

固件工程师修改 C 诊断工具的公共头文件后，用本平台快速判断**哪些源文件需要重编**。
基于 **FastAPI + GCC** 的在线增量构建服务：浏览器里改代码并提交，多个构建并行执行、
各自使用提交瞬间的文件内容；源文件与预处理实际读到的项目头均未变化时复用目标文件；
并发构建需要同一份目标文件时只编译一次。

## 一、内置的 C 项目（文件名与编译参数固定）

```
app/c_project/
├── include/
│   ├── diag_config.h   # 公共配置头（演示中修改它可观察全量重编）
│   ├── diag_util.h     # 嵌套引用 diag_config.h，定义 diag_sensor_t
│   └── diag.h          # 嵌套引用 diag_util.h（形成 diag.h→util→config 链）
└── src/
    ├── main.c          # 源文件 1/2：诊断工具入口
    └── util.c          # 源文件 2/2：诊断逻辑实现
```

固定编译参数（不接受外部覆盖）：

| 阶段 | 命令 |
|---|---|
| 编译 | `gcc -std=c11 -Wall -Wextra -O2 -Iinclude -c <src> -o build/<name>.o` |
| 链接 | `gcc -std=c11 -O2 build/main.o build/util.o -o build/diag_tool` |

## 二、核心机制

### 1. 不可变快照 —— 并行构建互不干扰

每次提交构建时，把当时全部文件内容写入**内容寻址 blob 仓库**（SHA-256），
并在该构建专属目录物化一份完整源码树（blob 用硬链接共享，零拷贝）。
构建全程只读自己的快照，因此：

- 多个构建可并行，各自看到的是**自己提交时**的文件内容；
- 构建进行中继续在编辑器改代码，不影响任何在途构建。

### 2. 依赖感知的目标文件复用 —— 精确回答"要不要重编"

对每个源文件先执行 `gcc -MM`，解析出**预处理实际读到的项目头文件**
（只保留快照目录内的 `.h`，自动排除 `stdio.h` 等系统头，也排除 `.c` 自身）。
然后计算缓存键：

```
object_key = SHA256(
    gcc 编译器标识
  + 固定编译参数
  + 源文件相对路径 + 源文件内容哈希
  + 每个实际依赖头的相对路径 + 内容哈希   # 嵌套头由 gcc -MM 自动展开
)
```

- 源文件 **和** 预处理实际读到的项目头**都没变** → key 不变 → 状态 `reused`（复用 `.o`）；
- 任一内容变化 → key 变化 → 状态 `compiled`（重新调用 gcc）。
- 只改一个源文件 → 只有它重编；改公共头 → 所有实际包含它的源文件重编。

### 3. 并发单次编译

每个 `object_key` 对应一把锁 + 双检（double-checked locking）：
N 个并发构建命中同一 key 时，只有一个真正执行 gcc，其余构建等待后直接复用，
页面上分别显示 1 个「重编」和 N-1 个「复用」。构建任务运行在有界线程池中。

### 4. 失败可见与下载控制

- 编译失败：对应源文件显示「失败」并回传完整 gcc 错误（含行号/源码上下文）；
- 链接失败：构建卡片显示链接器错误（如 `undefined reference to 'main'`）；
- 只有成功链接后才开放下载按钮，失败构建调用下载接口返回 `409`。

## 三、启动方式

### Docker Compose（推荐交付方式）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

数据卷 `build-data` 挂载在 `/workspace/data`，持久化 blob 仓库与目标文件缓存，
容器重启后缓存仍可复用。

### 本地直接运行（无需 Docker）

```bash
pip install -r requirements.txt
uvicorn app.web:app --host 0.0.0.0 --port 8000
# 工作目录由环境变量 BUILD_WORKSPACE 控制，默认 /workspace/data
```

### 命令行演示（服务启动后另开终端）

```bash
scripts/demo.sh                       # 默认 http://127.0.0.1:8000
BASE=http://127.0.0.1:8001 scripts/demo.sh
```

脚本依次演示：基线构建 → 无改动复用 → 单源文件改动选择性重编 →
公共头改动全量重编 → 编译错误回传/禁止下载 → 成功下载并运行程序。

## 四、HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 网页（文件树 + 编辑器 + 构建记录） |
| GET | `/api/files` | 当前编辑器文件内容、固定文件名与编译参数 |
| PUT | `/api/files` | 保存编辑器修改（仅接受固定文件名） |
| POST | `/api/builds` | 提交构建（请求体携带提交瞬间的全部文件，可选 note） |
| GET | `/api/builds` | 构建列表（每个源文件的 compiled/reused/failed、依赖头、object key、耗时） |
| GET | `/api/builds/{id}` | 单个构建详情与错误信息 |
| GET | `/api/builds/{id}/download` | 下载成功链接的可执行程序（失败返回 409） |

## 五、自测

```bash
pip install -r requirements.txt
python3 -m pytest tests/ -v
```

`tests/test_builds.py` 共 12 个用例，使用真实 GCC 与临时工作区，覆盖：
初始文件/固定参数、首构建全量编译并下载运行、无改动全复用、
公共头改动全量重编、单源文件改动选择性重编、编译错误逐源文件回传、
链接错误回传、缺失头文件被 `gcc -MM` 捕获、
**3 并发相同输入只编译 1 次 + 在途编辑快照隔离**、
**并发失败不污染缓存且修复后可重新编译**、非法文件名拒绝、404 处理。

## 六、目录结构

```
app/
├── builder.py          # 快照仓库、依赖解析、object 缓存、key 锁、构建编排
├── web.py              # FastAPI 路由与数据模型
├── initial_project.py  # 内置 C 项目加载
├── c_project/          # 两份 C 源文件 + 三个嵌套引用的项目头
└── templates/index.html# 单页前端（无外部依赖，原生 JS）
tests/                  # pytest 端到端测试
scripts/demo.sh         # curl 端到端演示脚本
Dockerfile / docker-compose.yml / requirements.txt
```
