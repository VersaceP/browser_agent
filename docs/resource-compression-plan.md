# 资源压缩执行计划（zlib，非增量）

状态：**已实施**（见文末 §13 实施记录；单元/dual/db/race 验证通过，真实 live dual 验证待运行）。

基线：`dd62e74` + 未提交的存储改造（A+B）。**本计划必须在存储改造合并之后、作为独立提交实施。**

---

## 0. 结论与依据

在两个 live 任务的真实数据上实测了四种方案（182 条 observation，28.62 MB）：

| 方案 | 体积 | 省 | 读路径 |
|---|---|---|---|
| 现状（紧凑 JSON） | 28.62 MB | 0% | 直接读列 |
| 完全去重（同 sha256） | 26.83 MB | 6% | 一次 join |
| 行级增量链 | 14.88 MB | 48% | 回放 base + N 个 delta |
| **逐条 zlib** | **2.29 MB** | **92%** | 解压一次 |
| 增量 + zlib | 1.00 MB | 97% | 回放 + 解压 |

**不做增量链。** 增量抓的冗余主要是同一份文档内部的重复（一棵 semantic tree 是几千个结构雷同的节点），通用压缩器吃得更干净；叠在压缩之上只多省 5 个百分点，却要付出链回放、链修复、dual 对拍重建三笔复杂度。跨快照的完全相同只有 6%，说明页面状态确实在变。

全库预期：`harness.db` 129 MB → 约 57 MB。

---

## 1. 范围

### 压缩

`resource_type` ∈ `{event_payload, observation, tool_result, context_compaction}`
且原始表示是 JSON 或 text
且**物理字节 ≥ `resource_compression_min_bytes`（默认 16384）**

实测阈值影响（80.09 MB 基数）：

| 阈值 | 压后 | 省 | 跳过条数 |
|---|---|---|---|
| 0 | 8.88 MB | 89% | 0 |
| 16384 | 9.57 MB | 88% | 82 |
| 65536 | 11.06 MB | 86% | 150 |

16 KiB 只多花 0.7 MB，换来 82 条小资源不必 BLOB 化。

### 不压缩

- `extraction`（业务产出，仅 1.46 MB，可读性优先）
- 原生 `bytes` 内容（已经是二进制，再压意义不大且混淆 `kind`）
- `external_path` 资源（harness 不持有字节）
- `run_events.payload_json`、`worker_trace_events.trace_json`、`task_snapshots`、`task_plan_versions`、`strategy_attempts`（单条小、且是 GUI 里真正会翻的表）

### 明确不做

- 不做增量 / delta 链
- **不新增列、不新增 migration**（见 §2）
- 不迁移历史数据；只影响新写入。旧库要缩小需另行原地重写 + 显式 `VACUUM`，本阶段不做
- 不改 `byte_size` / `sha256` 的语义

---

## 2. 数据形态

`content_encoding` 列**已存在**，见 `harness/storage/schema.sql:67`：

```sql
content_encoding  TEXT NOT NULL DEFAULT 'identity',
```

建表时就留了位子，只是写入从未显式填过。**复用它，不要加列，不要写 migration 0004。**

四选一 CHECK（`schema.sql:93-98`）已支持 blob-only 形态：

```sql
CHECK (
    (content_json  IS NOT NULL) +
    (content_text  IS NOT NULL) +
    (content_blob  IS NOT NULL) +
    (external_path IS NOT NULL) = 1
)
```

### 编码值

| 值 | 含义 | 列 |
|---|---|---|
| `identity` | 未压缩（默认，现状） | `content_json` 或 `content_text` |
| `zlib-json-v1` | zlib(紧凑 JSON 的 UTF-8 字节) | `content_blob`，`content_json` 为 NULL |
| `zlib-text-v1` | zlib(原始文本的 UTF-8 字节) | `content_blob`，`content_text` 为 NULL |

**必须区分 json 和 text。** 数据进了 `content_blob` 之后无法反推原始类型，而两者的读取语义不同：JSON 要按 `indent=2` 渲染成逻辑文件，text 要做换行归一化。

### 元数据口径不变

沿用已建立的逻辑/物理分离：

| 字段 | 含义 | 压缩后 |
|---|---|---|
| `byte_size` | **逻辑**大小 = 文件后端磁盘上那份字节 | 不变 |
| `sha256` | **逻辑**内容的哈希 | 不变 |
| `stored_byte_size` | **物理**列大小 | 改为压缩后的字节数 |

压缩是纯物理层变化，现有的逻辑/物理分离刚好容纳它，不引入新概念。

---

## 3. 唯一编解码器

**这是本计划最重要的一条。** 前七轮 review 的每一次分叉，根因都是"同一个问题有两套答案"。压缩必须一开始就只有一个入口。

在 `harness/storage/resource_codec.py` 新增：

```python
ENCODING_IDENTITY = "identity"
ENCODING_ZLIB_JSON = "zlib-json-v1"
ENCODING_ZLIB_TEXT = "zlib-text-v1"

# 解压后的全局硬上限，防止损坏或构造的流撑爆内存
MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024


class StoredResource(NamedTuple):
    """一条资源在库里的完整物理形态。"""
    content_json: Optional[str]
    content_text: Optional[str]
    content_blob: Optional[bytes]
    content_encoding: str
    logical_text: str          # 一次读取应当返回的文本
    logical_byte_size: int     # = byte_size
    logical_sha256: str        # = sha256
    stored_byte_size: int      # = stored_byte_size


def encode_resource(
    content: Any,
    *,
    resource_type: str,
    compression: str = "none",          # "none" | "zlib"
    min_bytes: int = 16384,
) -> StoredResource:
    """决定一条资源怎么落库。压缩与否的判定全在这里，调用方不再自己判断。"""


def decode_resource_row(row: Mapping[str, Any]) -> Optional[str]:
    """把任意一行的内容还原成逻辑文本。未知 encoding / 解码失败 → 抛 StorageCorruptError。

    row 需要至少带 content_json / content_text / content_blob /
    content_encoding / byte_size 五个键。
    """


def logical_text_from_row(row: Mapping[str, Any]) -> Optional[str]:
    """decode_resource_row 的别名语义：给读路径用，None 表示这一行没有内容
    （external_path 资源）。"""
```

新增异常，放在 `harness/storage/base.py`：

```python
class StorageCorruptError(StorageError):
    """一行资源的物理表示无法还原成它声称的内容。"""
```

**约束：除 `resource_codec.py` 外，任何模块不得再自行判断 `content_json` / `content_text` / `content_blob` 三个列。** review 时会 grep 这三个列名，出现在 codec 之外的读取判断即视为不通过。

---

## 4. 写入路径

### 4.1 `SqliteStore.save_resource`（`sqlite_store.py:480-560`）

现状已经正确的部分（**不要改坏**）：编码发生在 `with write_transaction(...)` **之前**（`sqlite_store.py:531/540` vs `:547`）。压缩耗时实测中位 1.88 ms、最大约 106 ms，绝不能占着 `BEGIN IMMEDIATE` 写锁做。

改动：`str` 与 dict/list 两个分支统一改走 `encode_resource(...)`，把返回的 `StoredResource` 铺进 `columns` 与 `content_encoding`。

### 4.2 `SqliteStore.append_event` 大载荷外置（`sqlite_store.py:206-226`）

同样先编码后开事务（现状 `:213` vs `:214` 已正确）。

**resource 行与 event 行必须仍在同一个 `write_transaction` 内提交** —— 崩在中间会留下孤儿 resource 和丢失的 event。这一条现有测试 `OversizedEventAtomicityTest` 已覆盖，不得回退。

### 4.3 `_insert_resource_rows`（`sqlite_store.py:565-610`）

INSERT 语句加上 `content_encoding` 列。签名多一个参数，或改为接收 `StoredResource`。

---

## 5. 读取路径（8 处，逐一改）

GPT 给的清单是 7 处，**漏了第 8 处**，那一处会让压缩后的资源从文件列表里整个消失。

| # | 位置 | 现状 | 必须改成 |
|---|---|---|---|
| 1 | `SqliteStore.read_resource` `sqlite_store.py:635-660` | `dict(row)`，然后对 `content_text` 做归一化 | 用 codec 解码，并把逻辑内容**放回** `content_json` / `content_text`（按原始类型），保留 `content_encoding` 字段供溯源。这样所有既有调用方不必改 |
| 2 | `SqliteStore.search_resources` `sqlite_store.py:720-770` | SELECT 只取 `content_json, content_text`；正则跑在原文上 | SELECT 增加 `content_blob, content_encoding`；正则跑在 **codec 解码后的逻辑文本**上 |
| 3 | `VirtualTaskFs._iter_resource` `virtual_fs.py:271-288` | 自行判断两列 | 走 codec，再 `_universal_lines()` |
| 4 | `VirtualTaskFs._payload_for` `virtual_fs.py:216-228` | `SELECT content_json, content_text`，取 `or` | SELECT 增加两列，走 codec 后 `json.loads` |
| 5 | **`VirtualTaskFs.list_files` `virtual_fs.py:122-128`** | `AND (r.content_json IS NOT NULL OR r.content_text IS NOT NULL)` | **加上 `OR r.content_blob IS NOT NULL`**。不改的话压缩后的资源不出现在虚拟文件列表里 —— `local_fs_search` 搜不到、`size_of()` 返回 None、`local_fs_read` 的 `byteSize` 退化 |
| 6 | `DualStore._stored_resource_digest` `dual_store.py:828-846` | 分支判断 `content_json` / `content_text`；`kind=="bytes"` 时读 `row["sha256"]` | json/text 两种 kind 走 codec 解码后再算摘要。**注意**：`kind` 来自写入日志，压缩不改变 kind，所以压缩过的 json 资源仍然是 `kind=="json"`，不得落进 `bytes` 分支 |
| 7 | `DualStore._resolve_offloaded_payload` `dual_store.py:888-895` | `content_json or content_text or "null"` | 走 codec |
| 8 | 大事件反解（`virtual_fs._payload_for` 的 resource 分支，与 #4 同处） | 同 #4 | 同 #4 |

### 5.1 `read_resource` 的兼容策略（重要）

`read_resource` 返回的 dict 目前被这些地方按 `content_json` / `content_text` 消费：`DualStore._stored_resource_digest`、`_resolve_offloaded_payload`、operator 工具。若压缩后这两个键变成 NULL，调用方全部要改。

**采用"解码回填"策略**：`read_resource` 内部解码后，把逻辑内容写回 `content_json`（原类型是 JSON 时）或 `content_text`（原类型是 text 时），同时保留：

- `content_encoding`：原样返回，让调用方知道物理形态
- `content_blob`：**不返回**（避免调用方误用；需要原始字节的走 operator 命令）

这样 §5 表里 #6 #7 两处其实只需确认行为不变，不需要重写逻辑。

---

## 6. 解压安全边界

**禁止裸调 `zlib.decompress(blob)`。** 损坏或构造的流会导致内存膨胀。

实现要求：

1. 用 `zlib.decompressobj()` + `max_length` 分块解压
2. 上限取 `min(byte_size * 1.05 + 4096, MAX_DECOMPRESSED_BYTES)`；`byte_size` 为 NULL 时取全局上限
3. 解压完成后校验 `decompressobj.eof` 为真 —— 流被截断则报错
4. 校验 `decompressobj.unused_data` 为空 —— 有 trailing data 则报错
5. 解压结果的字节数与 `byte_size` 不一致 → 报错（`byte_size` 是逻辑大小，压缩不改变它，所以必须相等）
6. 未知 `content_encoding` 值 → 报错，**不要**猜测或回落
7. 以上任何一条失败 → 抛 `StorageCorruptError`，消息里带 `task_id` / `resource_id` / `logical_path` / `content_encoding`

### 6.1 db 权威模式下不得回落

`backend="db"` 时解码失败必须向上抛。已有的 `harness/resume_state.py` 规则是"数据库拥有该任务时，缺失即报错，不回落文件"；解码失败属于同一类，**不得**降级去读磁盘上的旧文件。

`dual` 模式下解码失败记为 verify 的 `writeErrors`，不打断任务（文件侧权威）。

---

## 7. 配置

`runtime_config.py`（现有 storage 字段在 `:1011-1015`）新增：

```python
resource_compression: str = "zlib"            # "none" | "zlib"
resource_compression_min_bytes: int = 16384
resource_compression_level: int = 6
```

- 走现有 `HarnessConfig` 的 `from_dict` 模式，与 `storage_backend` 一致
- 非法值 fail-fast（参照 `_validated_storage_backend`）
- `"none"` 必须能完全关掉压缩，写出的行与今天逐字节相同 —— 这是回滚开关

`config.json` 不需要改（该文件被 gitignore，只写覆盖项）。

---

## 8. dual 模式的影响

- 文件侧**不受任何影响**：FileStore 照旧写明文文件
- `DualStore` 的写入指纹（`_resource_fingerprint`）基于**逻辑内容**，压缩不改变它 → 对拍口径不变
- `resources.db.content` 检查会经过解码，因此它顺带成为压缩往返的正确性守卫
- 预期：开启压缩后 dual 对拍仍然全绿。**若出现 mismatch，说明编解码不是无损的，必须停下查**

---

## 9. 必须补的测试

放在 `tests/test_storage_compression.py`（新文件）。

### 往返正确性
1. JSON 资源压缩后，`local_fs_read` 的 content / linesRead / truncated 与未压缩逐字节相同
2. text 资源同上，且 CRLF / LF / CR / 无末尾换行四种都验
3. `read_resource` 返回的 `content_json` / `content_text` 与未压缩相同
4. `search_resources` 的正则（含 `^...$` 行锚点）在压缩前后命中相同
5. extraction 的 `_artifact_sha256` 在压缩前后相同
6. 大事件外置载荷压缩后，`run.jsonl` 虚拟视图逐行相同

### 边界与选择
7. 小于阈值的资源**不**被压缩（`content_encoding == "identity"`）
8. 不在四类白名单内的 `resource_type` 不被压缩
9. 原生 bytes 内容不被压缩，仍走 `content_blob` + `identity`
10. `resource_compression="none"` 时写出的行与开启前逐字节相同

### 元数据
11. `byte_size` / `sha256` 是**逻辑**口径，压缩前后不变
12. `stored_byte_size` 等于压缩后的实际列长度，且小于 `byte_size`

### 列表可见性（对应 §5 #5）
13. 压缩后的资源仍出现在 `VirtualTaskFs.list_files()` / `match_files()` 中
14. `local_fs_search` 能搜到压缩后的资源
15. `local_fs_read` 对压缩资源的 `byteSize` 是整份逻辑大小、`bytesRead` 是本次读取字节

### 损坏防护
16. `content_blob` 被截断 → `StorageCorruptError`，不是 `zlib.error`
17. `content_blob` 尾部追加垃圾字节 → `StorageCorruptError`
18. `content_encoding` 是未知值 → `StorageCorruptError`
19. 解压结果与 `byte_size` 不符 → `StorageCorruptError`
20. 构造一个高压缩比的巨型流，验证受 `max_length` 限制而不是吃满内存
21. db 权威模式下解码失败**不**回落到磁盘文件

### 并发与事务
22. 压缩发生在 `write_transaction` 之外（用记录型 connection 断言 `BEGIN IMMEDIATE` 之前没有压缩调用；或断言持锁时长不随载荷增大而增长）
23. 大事件的 resource 行与 event 行仍在同一事务（沿用现有 `OversizedEventAtomicityTest` 的手法）

### 三态透明
24. 扩展现有 `BackendTransparencyTest`：同一份大载荷在 file / dual / db 三态下，`local_fs_read` 返回的字段集合与数值完全一致

---

## 10. 验收标准

实施完成后必须给出以下证据，缺一不可：

```bash
# 1. 全量绿
python3 -m pytest tests/ -q

# 2. 存储专项绿
python3 -m pytest tests/test_storage_*.py -q

# 3. dual 端到端对拍 ok（压缩开启）
python3 <scratchpad>/dual_smoke.py

# 4. db 模式读路径全通
python3 <scratchpad>/db_mode_smoke.py

# 5. 并发首次建库不退化
python3 <scratchpad>/race_probe.py 20 8
```

以及一次**真实 live 任务**（`dual` 模式）跑完后：

```bash
grep storage.dual_verify "worktree/<task_id>/run.jsonl" | tail -1 | python3 -m json.tool
# status 必须是 ok
```

并给出该任务的压缩实效：

```sql
SELECT content_encoding, COUNT(*), SUM(byte_size), SUM(stored_byte_size)
FROM task_resources WHERE task_id = '<task_id>' GROUP BY content_encoding;
```

---

## 11. 需要接受的代价

1. 被压的四类资源在 VS Code SQLite 插件里看不到正文，只能看到 `content_encoding` / `content_blob` / 大小 / 哈希。`run_events`、任务状态、计划、strategy、extraction 仍可直接查 —— GUI 里真正会翻的是这些
2. `search_resources` 对这四类跑正则时要先解压。实测全量解压约 65 ms，可接受但不是零
3. 现有 129 MB 的库**不会自动缩小**。即使以后原地重写旧行，也要显式 `VACUUM`（需要 2 倍磁盘 + 独占锁）才会归还磁盘空间

建议后续单独提供一个只读 operator 命令导出/查看压缩资源正文，而不是依赖 GUI 插件。

---

## 12. 提交顺序

1. **先合并已验证的存储改造（A+B）** —— 当前工作区里我的 86 个 hunk 与并行开发的 132 个 hunk 交织在 15 个文件中（11 个混杂）。机械切分试过三轮，每轮都会静默丢掉不含关键词的 hunk（如 `local_fs.py` 的 `scanned = 0`），只有测试碰巧覆盖才会暴露。**正确做法是并行开发方先提交自己的改动**，之后存储改造成为唯一剩余 diff，`git add` 逐文件精确
2. 决定 `tests/test_storage_*.py` 是否跟踪（`tests/` 被 `.gitignore:12` 整个忽略，目前仓库只跟踪 4 个测试文件）
3. 在干净基线上实施本计划，作为**独立提交**

不要把压缩和存储改造混在一个提交里 —— 压缩要动的 `save_resource` 和读路径正好是刚被七轮 review、刚跑完 live 验证的那块，混在一起下次出问题分不清是哪一次引入的。

---

## 13. 实施记录（2026-06）

状态：**已实施**（工作区待提交；live 验证待运行）。九条全部落实，代码改动限于 `harness/storage/`、`runtime_config.py` 与新增测试。验收命令 §10.1–§10.5 全部通过（§10.1 全量套件 2734 通过，仅余 5 个与本次无关的预存在失败：4 个缺 `anthropic`/`openai` 可选包，1 个是 Python 3.9 的 `pathlib.glob` 对 `[z-a]` 直接抛错的参照物问题）。**§10 的真实 live dual 任务尚未运行**，合并前需补。

实施中圆了三处设计细节，均与本文不冲突、目的相同：

1. **压缩的是逻辑字节，不是紧凑列字节**（§2）。若按 "zlib(紧凑 JSON)" 存，解压长度将永远不等于 `byte_size`，§6 第 5 条的完整性校验对每行都会失败。改为压缩逻辑文本后，"解压长度 == byte_size" 恰好成为逐行完整性不变量，且解压结果就是读路径要的文档，无需二次渲染。压缩率几乎不受影响（缩进本就被 zlib 吃干净）。
2. **list_files 的可见性过滤是 `content_blob IS NOT NULL AND content_encoding <> 'identity'`**（§5 #5）。直接加 `OR content_blob IS NOT NULL` 会把原生二进制资源第一次带进虚拟文件列表，改变 file/db 模式下模型可见的文件集合（dual_store 既有注释明说二进制"故意不进列表"）。带 encoding 守卫后只放入压缩资源，恰好修复目标问题。
3. **解码对已还原的行幂等**。`read_resource` 回填后 `content_blob` 已移除而 `content_encoding` 保留溯源值，对同一行二次解码（如 `_resolve_offloaded_payload`）必须是无操作而不是报损坏：blob 缺失且文本列已填时按 identity 语义返回。

另：`read_resource` 回填给 `content_json` 的是**逻辑渲染文本**（identity 旧行是紧凑列值）。所有既有消费者都只做 `json.loads`，语义不变；文本列则严格字节一致。

验收证据脚本落在 `scratchpad/`：`dual_smoke.py`（对拍 ok；实测 identity 行 63900 字节 vs 三条 zlib-json 行共 2041 字节存 204268 逻辑字节）、`db_mode_smoke.py`（九项读路径全 ok）、`race_probe.py 20 8`（20 并发首建库全 PASS，20/20 行均为 zlib 编码）。
