# harness.storage — SQLite 存储中间层代码设计文档

> 对应代码：`harness/storage/`（约 5200 行）
> 相关配置：`runtime_config.py`（`storage_backend` 等键）
> 相关测试：`tests/test_storage_*.py`

---

## 1. 背景与目标

harness 原先以每任务一组 JSON/JSONL 文件（`run.jsonl`、`traces/`、`task_state.json`、offloaded observations）持久化过程数据。`harness.storage` 包将其替换为**单一 SQLite 数据库**，并满足以下目标：

1. **业务代码零 SQL**：业务模块只依赖 `base.Storage` 抽象接口，永远拿不到 `sqlite3.Connection`，从 `file` 切换到 `db` 是配置变更而非调用点改写。
2. **多进程安全**：多个 harness 进程可并发使用同一个数据库文件（WAL + `BEGIN IMMEDIATE` + busy 重试）。
3. **平滑迁移（de-risking）**：`dual` 双写模式让文件与数据库同时接收写入并互相校验，文件保持权威，数据库在被证明等价之前不承载业务。
4. **模型兼容**：`virtual_fs` 把数据库行渲染成 agent 期望的文件路径与 glob 语义（`run.jsonl`、`traces/*.jsonl` 等），避免重写 prompt/skill 契约。

### 分层总览

```
业务模块 (task_control / resume_state / local_fs / main …)
        │  只依赖
        ▼
base.Storage  ←———— 抽象接口 + 错误类型 + 统一 glob 语义
        ▲
        ├─────────────┬──────────────┬──────────────┐
 FileStore      SqliteStore     DualStore      virtual_fs / admin
 (遗留文件布局)   (SQLite 实现)   (双写+校验)     (模型视角/运维删除)
                       │
                 dao.py（参数化 SQL，无 ORM）
                       │
              sqlite_connection.py（连接/事务规则）
                       │
              migrations.py + schema.sql（版本化 DDL）
```

| 模块 | 职责 | 关键约束 |
|---|---|---|
| `base.py` | 后端无关接口、错误体系、`canonical_json`、glob 编译器 | 业务唯一允许的依赖面 |
| `factory.py` | 后端选择与数据库引导（`create_storage*`、`open_registry`） | `file` 模式不导入任何 SQLite 模块 |
| `file_store.py` | 现有磁盘布局的 `Storage` 实现，供 `dual` 对照与 `file` 兼容 | 不改文件格式；无对应概念处在内存补齐并标注 |
| `sqlite_store.py` | SQLite 后端：资源 URI、超限 offload、版本化资源、搜索 | 资源读取二次校验 task 归属 |
| `dao.py` | 每张表的参数化 SQL；snapshot CAS；run_number/序列号分配 | 表名/列名为字面量，值一律 `?` 占位 |
| `sqlite_connection.py` | 连接配置、`write_transaction`、`ConnectionRegistry` | 事务体内只允许 SQL |
| `migrations.py` | 版本化迁移（`schema_migrations` 表） | DDL 与版本行同一事务提交 |
| `resource_codec.py` | 资源逻辑/物理双层表示、zlib 压缩、解压完整性校验 | 读写共用同一函数防止口径漂移 |
| `dual_store.py` | 双写双后端，按 `canonical_json` 语义比对 | 数据库侧异常降级为 finding，不上抛 |
| `virtual_fs.py` | 数据库行 → 模型熟悉的文件路径/glob 视图 | 渲染结果与文件后端逐行一致 |
| `admin.py` | 仅运维可用的软删/硬删（purge） | 不进 `Storage` 接口、不暴露为 agent 工具 |

---

## 2. 后端选择与配置

```python
BACKEND_FILE = "file"   # 默认安全的文件模式（不触 SQLite 模块）
BACKEND_DUAL = "dual"   # 双写：文件权威 + 数据库影子 + 比对
BACKEND_DB   = "db"     # 数据库权威
```

`runtime_config.py` 相关键（当前默认 `storage_backend = "db"`）：

| 配置键 | 默认 | 说明 |
|---|---|---|
| `storage_backend` | `db` | `file` / `dual` / `db`，非法值 fail-fast |
| `storage_sqlite_path` | `harness.db` | 相对路径相对 `worktree_dir` 解析（**不是**进程 cwd —— resume 会迁移 worktree） |
| `storage_busy_timeout_ms` | 5000 | SQLite busy timeout |
| `storage_dual_verify` | `True` | dual 模式是否开启读校验 |
| `resource_compression` | `zlib` | `none`/`zlib`；`none` 是字节级回滚开关 |
| `resource_compression_min_bytes` | 16384 | 逻辑字节达到阈值才压缩 |
| `resource_compression_level` | 6 | 0–9 |

工厂刻意**不读取** `runtime_config`，只接收显式参数；`create_storage_from_config` 在其上提供薄封装（读 `HarnessConfig` 形状对象）。

---

## 3. 连接与事务（`sqlite_connection.py`）

### 3.1 每条规则存在的原因

* **`isolation_level=None`**：禁用驱动的隐式事务管理。Python 默认 DEFERRED 事务先拿读锁、首次写时升级；若期间别的连接已提交，升级会**立即**返回 SQLITE_BUSY 且不经过 `busy_timeout`（等待可能死锁）。
* **`BEGIN IMMEDIATE`**：写事务一开始就取写锁——这是 `busy_timeout` 唯一真正生效的场景。
* **连接按线程私有**：`check_same_thread` 保持默认，泄漏的跨线程句柄会响亮失败而非静默损坏状态。
* **事务体内只允许 SQL**：无网络、无文件 I/O、无 LLM 调用、无 `await`。WAL 下单写者，一个长事务会拖住所有进程的写。

### 3.2 连接 PRAGMA（`configure_connection`）

```
busy_timeout = <配置值>          # 先于一切可能争锁的语句
（仅空库时）auto_vacuum = INCREMENTAL   # 硬删除释放的页可回收，否则库只增不减
journal_mode = WAL               # 持久化在文件头；后续连接重复执行是幂等读
synchronous  = NORMAL
foreign_keys = ON
page_size    = 保持默认 4096      # 有意为之；改动需退出 WAL + VACUUM，仅在基准证据下重议
```

`page_size` / `auto_vacuum` 只对空库生效，因此必须在 WAL 之前、任何建表之前下发——由 `migrations.py` 在连接上先执行再应用 `schema.sql`。

### 3.3 `write_transaction(connection)`

上下文管理器：`BEGIN IMMEDIATE` + 指数退避 + 抖动的有界重试（默认 5 次、基数 50ms）。**只重试取锁**；事务体开始后的 SQLITE_BUSY 直接上抛——重放任意语句并不普遍安全，需要重试语义的调用方（snapshot CAS）每次尝试重新推导值。超预算抛 `StorageBusyError`。

### 3.4 `ConnectionRegistry`

* 每线程懒创建一个连接（thread-local 存储 + 全表登记）；查询所有权仍是线程私有的。
* `check_same_thread=False` 仅为了让关闭协调线程在 worker join 之后能 `close_all()` 释放全部休眠句柄——是生命周期机制，不是共享许可。
* `close()` 后再取连接抛 `StorageClosedError`（懒重开会把 close 变 no-op，掩盖"关闭后仍写入"的生命周期 bug）。

### 3.5 空间回收

`maybe_incremental_vacuum`：freelist ≥ 1000 页才执行 `incremental_vacuum(1000)`。回收本身是写操作，**绝不**在任务完成路径调用（正常 finish 不释放任何页），仅维护流程在硬删除之后调用。

---

## 4. 版本化迁移（`migrations.py` + `schema.sql`）

* schema 版本记录在独立表 `schema_migrations(version, migration_name, applied_at)`，与"从 `tasks` 表推断"区分开：空库与未迁移库可辨别。
* 每个迁移（DDL 语句序列 + 版本行）在**同一个 `BEGIN IMMEDIATE` 事务**内提交；崩溃则停留在上一版本，绝不半应用。刻意不用 `executescript()`（它会强制自己的 COMMIT，破坏原子性）。
* 建迁移表本身也包在写事务里——首个 DDL 是所有进程同时发出的第一个争锁点。
* `apply_migrations` 幂等：已应用版本跳过；锁内二次检查防止多进程首启竞态重放 DDL。
* `split_sql_statements` 用 `sqlite3.complete_statement`（与 sqlite3 shell 同一解析器）拆分脚本，避免朴素 `split(";")` 被字符串字面量/注释中的分号破坏。

当前迁移序列：

| 版本 | 名称 | 内容 |
|---|---|---|
| 1 | `initial_schema` | `schema.sql` 全部初始表 |
| 2 | `run_git_sha` | `task_runs` 增加 `git_sha TEXT`（记录实际产出行的代码版本，手写版本号会过期） |
| 3 | `resource_stored_byte_size` | `task_resources` 增加 `stored_byte_size INTEGER`（物理存储尺寸与逻辑 `byte_size` 分离；旧行 NULL 是诚实的"从未测量"） |

`SCHEMA_VERSION = 3`。

---

## 5. 数据库表结构设计（`schema.sql`）

### 5.1 总体 ER 关系

```
tasks (1) ──< task_runs (1) ──< run_events
  │               │        └──< task_resources（按 (task_id, run_id) 复合外键）
  │               │        └──< worker_trace_events
  │               │        └──< strategy_attempts
  │               └───< task_snapshots          (updated_run_id 外键)
  │               └───< task_plan_versions      (run_id 外键)
  │               └───< task_plan_reviews       (run_id 外键)
  └── schema_migrations（全局，不属于任务）
```

所有子表对 `tasks` 均为 `ON DELETE CASCADE`（purge 用）；对 `task_runs` 用**复合外键 `(task_id, run_id)`**，因此父表 `task_runs` 上有冗余的 `UNIQUE(task_id, run_id)` 索引（SQLite 复合子外键要求父侧精确同列序的 UNIQUE 索引）。`task_id` 同时复制为每行的第一列，使每个外键都能以"任务范围"判定。

### 5.2 tasks — 任务注册表

| 列 | 类型 | 说明 |
|---|---|---|
| `task_id` | TEXT PK | |
| `create_time` / `last_run_at` | TEXT | |
| `snapshot_json` | TEXT NOT NULL DEFAULT '{}' | 列表页摘要（由 `commit_accepted_plan` 与摘要回调在同一事务内更新） |
| `is_deleted` / `deleted_at` | INTEGER CHECK(0,1) / TEXT | 软删除；数据对运维保持可查 |
| `purge_status` | TEXT CHECK('none','pending','purging','failed') | 硬删除两阶段标记 |
| `purge_requested_at` | TEXT | |
| `created_harness_version` / `last_harness_version` / `created_schema_version` | TEXT/TEXT/INTEGER | 溯源 |

设计要点：
* **purge 两阶段**：外部文件先于行删除，事务无法同时覆盖两者；中途崩溃必须仍可辨识为 "purging"，而不是回退成一个看起来可运行的任务。
* 索引 `idx_tasks_live(is_deleted, create_time)` 支撑活跃任务列表。

### 5.3 task_runs — 运行生命周期

| 列 | 说明 |
|---|---|
| `run_id` TEXT PK / `task_id` / `run_number` | `UNIQUE(task_id, run_number)`；`UNIQUE(task_id, run_id)` 服务复合外键 |
| `started_at` / `finished_at` / `status` | status CHECK ∈ {running, completed, failed, cancelled, interrupted} |
| `harness_version` / `process_id` / `host_name` / `git_sha`(v2) | "哪段代码、哪个进程实际产出这些行" |
| `error_json` | 终态错误 |

**run_number 在写事务内分配**（`SELECT COALESCE(MAX(run_number),0)+1` + INSERT 同事务），两个进程不可能发出同一个编号。

### 5.4 task_resources — 版本化过程资源（核心表）

| 列 | 说明 |
|---|---|
| `resource_id` TEXT PK | uuid4 hex；`UNIQUE(task_id, resource_id)` |
| `task_id` / `run_id` | 双外键（任务 + 复合 run） |
| `resource_type` / `logical_path` / `media_type` | 逻辑文件身份 |
| `content_encoding` | `identity` / `zlib-json-v1` / `zlib-text-v1` |
| `content_json` / `content_text` / `content_blob` / `external_path` | **四选一**（CHECK 四列非空计数 = 1） |
| `metadata_json` / `byte_size` / `stored_byte_size`(v3) / `sha256` | `byte_size`/`sha256` 描述**逻辑**字节；`stored_byte_size` 描述物理 blob |
| `resource_version` / `is_current` / `supersedes_resource_id` | 同一 `logical_path` 在同一 run 内可被重写；旧版本保留可寻址 |

设计要点：
* **为什么有 `external_path`**：`Download.start` 等场景 ABCP 平台直接写盘、harness 只拿到回执，从不持有字节。`EXTERNAL_RESOURCE_TYPES = {download, coding_agent_output, file_evidence}`。
* **版本化动机**：历史 `run_events.payload_resource_id` 引用的必须是事件发生时的字节，绝不能解析到新内容。
* 部分索引 `uq_current_resource_path(task_id, run_id, logical_path) WHERE is_current = 1`：同一 run 内每个路径至多一个当前版本。
* `normalize_external_path`：任务目录内的路径存**任务相对**（搬移 worktree 不失效）；目录外保持绝对并标记 `external_unmanaged`（purge 不得删除任务不拥有的数据）。

### 5.5 run_events — 事件流

| 列 | 说明 |
|---|---|
| `event_id` INTEGER PK AUTOINCREMENT | keyset 分页游标 |
| `task_id` / `run_id` | run_id 是关系字段，不是 payload 字段 |
| `event_time` / `event_type` / `actor_type` / `worker_id` | |
| `payload_json` / `payload_resource_id` | **二选一异或**（CHECK `!=`）；小 payload 内联，超限搬入 task_resources 只留引用 |
| `payload_byte_size` | 逻辑字节数（两种形态都填） |

索引：`(task_id, event_id)`、`(run_id, event_id)`、`(task_id, event_type, event_id)`、`(task_id, worker_id, event_id)`。读取一律 `event_id > ?` keyset 分页，**从不用大 OFFSET**。

offload 阈值 `EVENT_PAYLOAD_OFFLOAD_THRESHOLD = 64KB`（实测 run.jsonl 行 p99 ≈ 26KB）。资源行与指向它的事件行**同一事务**提交，否则崩溃后出现孤儿资源 + 丢失事件；压缩在事务开启前完成（最大活负载压缩可达 ~100ms，不得持锁执行）。

### 5.6 task_snapshots — 可变当前态（CAS）

| 列 | 说明 |
|---|---|
| `(task_id, snapshot_key)` PK | key 是封闭集合：`task_state`、`current_task_plan`（对应原整文件覆写的 `task_state.json` / `task_plan.json`） |
| `value_json` / `revision` / `updated_at` / `updated_run_id` | revision 是并发控制版本，与 harness/schema 版本无关 |

### 5.7 task_plan_versions / task_plan_reviews — 计划历史

* `task_plan_versions(task_id, plan_version)` PK，附 `plan_hash`、`previous_plan_version`、`replan_reason`、`plan_json`、`diff_json`、`validator_review_json`。版本 1 是 resume 锚点；不可变追加。
* `task_plan_reviews`：validator 对候选计划的评审，`UNIQUE(task_id, review_sequence)`，存 `candidate_hash`、`decision`、候选全文与评审全文。
* 版本号/评审序号在写事务内分配（或采纳 dual 模式下文件侧已分配的编号，防止两套账本对同一计划编号漂移）。

### 5.8 worker_trace_events — Worker 轨迹

`UNIQUE(task_id, run_id, worker_id, sequence_no)`；批量追加时在事务内 `MAX(sequence_no)+1` 续号。读序 `ORDER BY worker_id, sequence_no`。

### 5.9 strategy_attempts — 跨任务策略遥测

半结构化投影列（`strategy_ids_json`、`status`、`status_category`、`validated_status`、`failure_classification`、`row_count`、`artifact_count`），`phase_id`/`worker_id` 可空。**跨任务分析是这个表不按任务拆分的原因**。文件侧对应 `strategy_attempts.jsonl`。

---

## 6. 核心机制设计

### 6.1 snapshot 三方合并 + CAS（`dao.save_snapshot`）

并发控制的分层（从宽到窄）：

1. `.run.lock` 保证两个 harness 进程不同跑一个任务；
2. 调用方的进程内锁串行化线程；
3. `merge(base, current, proposed)` 把基于过期快照的编辑折叠到不同字段；
4. **revision CAS 是最后一道闸**，捕获跨连接的任何写入。

流程：读 `(current, expected_revision)` → 计算 `persisted`（无 merge 则深拷贝 proposed）→ 事务内按 revision 匹配 UPDATE（或首写 INSERT … ON CONFLICT DO NOTHING）→ 变更行数 = 1 即成功；否则读最新 revision，触发 `on_conflict` 钩子（遥测），重试至 `DEFAULT_MAX_CAS_ATTEMPTS = 5`。

不变量：
* CAS 失败重算 merge 时，**`base` 与 `proposed` 保持原值**，只换新 `current`——用新 current 替换 base 会把本调用方的编辑与对方编辑混淆并静默丢失。
* `replace=True`（整态重建）丢掉 merge；CAS 失败直接抛 `RevisionConflictError` 而不重试——整态重建遇到并发写是生命周期协调失败，重试会覆盖对方刚提交的内容。
* 首写（无行）不是冲突——UPDATE-only 的 CAS 会在这里报 0 行，让每个新任务耗尽重试预算，故显式 INSERT 分支。
* 库内 JSON 解析失败**上抛** `StorageError` 而非降级 `{}`：磁盘上撕裂写有可能、宽容是对的；库里它意味着损坏，静默返回空态会让下一次写抹掉真实任务。
* 返回 `(persisted, new_revision)`，调用方必须写回快照对象并重置基线，否则下次 merge 又对陈旧 base 进行。

### 6.2 `commit_accepted_plan` — 计划代际原子发布

版本行、`current_task_plan` 别名、重置后的 `task_state` 三者描述**同一代际**，单事务写入；分开写会在崩溃间隔留下 plan 与 state 对"存在哪些 phase"意见不一的撕裂代际（文件后端需要修复遍历的就是这种状态）。要点：

* plan version 在事务内分配并**先盖进 state** 再写快照，二者不可能对代际意见不一。
* 无 CAS：接受计划本就是整代重建。
* `summarize` 是回调而非值——版本号在事务内分配，摘要引用它，因此摘要无法在事务开启前计算；且它在**同一事务内**执行，列表页永远不可能展示一个已回滚代际的摘要。

### 6.3 资源 URI 与任务隔离（`sqlite_store`）

* URI 形如 `sqlite://tasks/<task_id>/resources/<resource_id>`（`build_resource_uri` / `parse_resource_uri`）。
* **解析不授权**：`read_resource` 解析后必须比较 URI 中的 task_id 与当前运行任务；WHERE 子句再重复 task_id——全局唯一 resource_id 本身永远不充分。跨任务访问抛 `ResourceAccessError`。
* 外部文件读取时 `_reread_external` 重算哈希，报告 `content_available` / `content_drifted`：下载目标可被后续 run 原地重写，harness 不控制保存路径，诚实保证是"本次读取时完好"而非不可变。

### 6.4 resource_codec — 逻辑/物理双层表示

* **逻辑文件** = 读者所见：`indent=2` 漂亮打印，与文件后端字节一致。行号、读预算、`local_fs_search` 行命中、artifact SHA-256 都描述这一形态。
* **存储列** = 紧凑 JSON：缩进占语义树 76% 字节，库里没人直接读列。
* 大型 harness 内部资源（`COMPRESSIBLE_RESOURCE_TYPES = {event_payload, observation, tool_result, context_compaction}`，业务产出与操作员可见内容保持原位可读）逻辑字节 ≥ `min_bytes` 时 zlib 压缩入 `content_blob`。选 zlib 而非逐行 delta 链是实测结论：链省 48% 但每次读都要重放，zlib 一次有界解压省 92%。
* 压缩的是**逻辑文本**：解压字节恰好等于 `byte_size`，成为每次读取执行的完整性校验；`byte_size`/`sha256` 恒为逻辑值，`stored_byte_size` 单独描述 blob。
* 防炸弹：`MAX_DECOMPRESSED_BYTES = 256MB` 硬顶，独立于行自称的尺寸；未知 `content_encoding`、截断流、解压长度与 `byte_size` 不符 → `StorageCorruptError`（存储损坏，db 权威读路径必须让其传播，不得回退到陈旧文件）。
* 写侧在此测量/哈希，读侧在此渲染逻辑文本——**同一模块两侧共用**是防止口径漂移的手段（漂移会以"artifact 损坏"的形式在写后很久才暴露）。

### 6.5 统一 glob 语义与两段式搜索（`base.glob_matches` + `search_resources`）

* 唯一的 glob 匹配器服务四个面：文件后端、数据库后端、virtual_fs、真实 worktree 扫描。语义同 `pathlib.Path.glob`：`*`/`?` 不跨 `/`，`**` 跨任意层（`*` 只匹配任务根，`**/*` 是全部）。
* 字符类 `[...]` 的**跨度**自找、**内容**委托 `fnmatch.translate`（`^` 是普通成员而 `!` 取反、开头的 `]` 是成员、未闭合是字面量、`[z-a]`/`[a--b]` 需归一……手写版错三次，委托一次全对）；产出片段前缀 `(?!/)` 保证类不匹配分隔符。
* `glob_sql_prefilter`：SQLite GLOB 与路径 glob 不是同一语言（`[^...]` vs `[!...]`），只有第一个魔法字符前的字面前缀可安全下推，其余在 Python 精确判定。
* `pattern`（正则）**不**推导 LIKE 预过滤：`foo|bar` 没有必须出现的单一字面量，按一支预过滤会静默丢另一支的命中。正则带 `re.MULTILINE`（被搜的是文件，`^`/`$` 指行边界）。
* 搜索按 `(logical_path, rowid)` 游标分批（按路径而非 rowid 排序：文件后端按路径序遍历，`max_results` 下两后端须返回相同的前 N）；当前版本规则与 virtual_fs 一致——resume 后每路径每 run 一行，取 `MAX(run_number)` 的当前行。

### 6.6 virtual_fs — 数据库即文件系统

路径族映射：

```
run.jsonl                -> run_events
traces/<worker>.jsonl    -> worker_trace_events
strategy_attempts.jsonl  -> strategy_attempts
其余一切                  -> task_resources (logical_path)
```

渲染复刻文件后端本会写出的逐行字节，使 `db` 模式下模型看到与 `file` 模式相同的内容；`local_fs_read` / `local_fs_search` 的路径/glob 契约不变，prompt 与 skill 无需改动。

### 6.7 dual_store — 双写校验

* 文件权威 + 数据库影子；**相等性以同一 Python 对象的 `canonical_json` 判定**（`sort_keys` + 固定分隔符；两后端独立序列化，原始字节比较会把无意义的键序差异全报成 mismatch）。
* 语义哈希 `semantic_sha256(canonical form)`；事件流摘要取有序 `(type, actor, worker, canonical(payload))` 元组；资源指纹记录 `kind`（json/bytes/text/external），因为同一值两側形态不同，只有知道起点形态才能从任一侧重算摘要；策略遥测双侧各记一份指纹（文件侧存整个 payload，表侧存类型化投影，各查各所承诺保留的）。
* **数据库侧故障不上抛**：此模式在被评估的正是 SQLite，其错误记为 finding 而不是拖垮文件后端已正确处理的任务。

### 6.8 admin — 运维删除

* 刻意不在 `Storage` 接口上、绝不暴露为 agent 工具：它会销毁数据，LLM 可达的任何东西都不应能调用；仅 CLI / 运维 RPC 可达。
* `soft_delete_task`：可逆，子行对运维保持可查。
* `purge_task`：硬删除不可能原子（外部文件在库外，无事务可覆盖两者），因此**分段可重启**：先标记 `purging`，外部文件删除、行删除、（freelist 超阈值时）`incremental_vacuum`。中途崩溃留下的是"可辨识的删除中"而非"看似可运行"的任务。`expected_deleted=True` 前置条件保证 purge 不会是对活跃任务发生的第一件事；`external_unmanaged` 文件永不删除。

---

## 7. 错误体系

```
StorageError (RuntimeError)
├── RevisionConflictError   # CAS 失败；携带 task/snapshot/expected/actual/attempts
├── ResourceAccessError     # 跨任务资源寻址
└── StorageCorruptError     # 行的物理表示无法还原（未知编码/截断/炸弹/长度不符）
StorageBusyError (RuntimeError)   # 写锁重试预算耗尽
StorageClosedError (RuntimeError) # 注册表关闭后再取连接
PurgeRefused (StorageError)       # 硬删除前置条件不满足
```

---

## 8. 关键不变量清单（评审与测试对照）

1. `run_events` 每行 payload_json / payload_resource_id 恰好其一；offload 时资源与事件同事务。
2. `task_resources` 四个内容列恰好其一非空；`is_current=1` 的行在 `(task_id, run_id, logical_path)` 上唯一。
3. 历史 `payload_resource_id` / 旧版本资源行永远可解析到事件发生时的字节。
4. 所有子表行经复合外键锚定 `(task_id, run_id)`；`task_id` 重复出现在每个 WHERE 中，resource_id 全局唯一不构成授权。
5. 事务体只含 SQL；压缩/哈希/外部文件探测在事务开启前完成。
6. 事件读取 keyset 分页；搜索按 `(logical_path, rowid)` 游标且与文件后端结果一致。
7. `byte_size`/`sha256` 恒为逻辑形态度量，与 FileStore 的 `st_size` 口径一致；压缩不改变它们。
8. snapshot 首写不是冲突；replace 输掉 CAS 即抛错不重试；库内 JSON 损坏上抛不降级。
9. 迁移 DDL 与版本行原子提交；`apply_migrations` 幂等。
10. `file` 模式不导入 SQLite 模块；`dual` 模式下数据库异常不传染。

---

## 9. 测试覆盖地图

| 测试文件 | 覆盖面 |
|---|---|
| `test_storage_sqlite_dao.py` | dao 层 SQL、CAS、序列号分配 |
| `test_storage_schema.py` | schema/约束/CHECK/外键 |
| `test_storage_backends.py` | 三后端接口一致性 |
| `test_storage_db_mode.py` | `db` 模式端到端 |
| `test_storage_virtual_fs.py` | 数据库 → 文件视图渲染一致性 |
| `test_storage_compression.py` | 编解码、阈值、防炸弹、损坏检测 |
| `test_storage_wiring.py` | 工厂/配置接线 |
| `test_storage_review_regressions.py` | 评审回归（glob 语义、CAS 细节等） |

---

## 10. 扩展守则

* **新增表**：`schema.sql` 保持只读历史，新迁移 append 到 `MIGRATIONS`（版本递增、DDL 与版本行同事务）；空库引导路径不受影响。
* **新增快照键**：加入 `base` 的封闭集合，并确认 `file_store.SNAPSHOT_FILES` 有对应文件。
* **新增资源类型**：若属 harness 内部大块捕获，评估加入 `COMPRESSIBLE_RESOURCE_TYPES`（业务/操作员可见内容不进）；若是平台写盘回执型，加入 `EXTERNAL_RESOURCE_TYPES`。
* **新增 Storage 方法**：先在 `base.Storage` 定义抽象并同步三个实现（`dual` 需声明比较口径），`file` 模式不得因此引入 SQLite 依赖。
* **改 page_size / journal**：仅带基准证据；page_size 变更需退出 WAL 并 VACUUM。
