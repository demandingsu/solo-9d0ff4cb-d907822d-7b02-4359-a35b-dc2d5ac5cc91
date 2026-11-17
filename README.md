# 影像谱系 API（Image Lineage API）

遥感制图年度拼图交付前，用于追踪原始瓦片与拼图之间多级固定引用关系、
判定现役拼图可交付状态并列出受阻引用路径的 FastAPI + SQLite 服务；
并支持**异地备份盘点**：开单固定本地现役目录快照，逐页接收异地导出的
分页成果目录，封单后给出缺失 / 修订不符 / 摘要不符 / 备份多出的对账报告，
据以安排补拷。

## 功能与规则

- **分批提交清单**：`POST /api/batches`，每批可含任意数量条目；条目包含
  成果号、修订号、内容摘要，以及固定引用的父成果号 + 父修订号（原始瓦片无父项）。
- **多级引用、父项允许晚到**：拼图可指向另一拼图，父项尚未提交时不拒绝入库，
  查询时标记为「父项缺失」；补传父项后结论自动更新。
- **现役修订**：同一成果号以**最高已提交修订号**为现役；旧修订保留可追溯，
  可通过 `?revision=N` 查询历史修订当时的谱系结论。
- **可交付判定**：只有整条谱系有效才可交付。阻断原因三类，均给出完整引用路径：
  - `missing` 父项缺失（成果或修订从未提交）
  - `superseded` 父项已非现役（同成果号存在更高修订）
  - `cycle` 循环引用（引用链成环，含自引用）
- **整批拒收**：同号同修订但清单内容（摘要/父引用）不同时，整批拒绝、零写入，
  原有结论保持不变，并返回冲突明细；完全相同的重复提交按幂等处理。
- **并发安全**：写事务 `BEGIN IMMEDIATE` 串行化；每次查询在单个 SQLite 快照事务内
  完成全部读取，不会混用某次并发提交前后的谱系状态。
- **审计**：所有批次（含拒收/重复）写入 `batches` 表，可通过 `/api/batches` 追溯。

## 异地备份盘点（对账）

档案员从异地档案导出**分页成果目录**（每页含成果号、修订号、摘要），
与本地现役目录对账，找出尚未备份或版本不符的瓦片/拼图以安排补拷：

- **开单** `POST /api/inventory/orders`：指定预定页数；开单瞬间在**同一事务**内
  快照本地全部现役修订（成果号 → 现役修订号 + 摘要）作为对账基准。
  **开单后继续入库的新修订不影响本次对账。**
- **传页** `POST /api/inventory/orders/{id}/pages`：逐页上传，页号从 1 起、
  不得超过预定页数；每个成果号在整单中只允许出现一次（页内重复 → 422，
  跨页同号 → 409）。**同页同内容重试幂等**（条目顺序无关，返回 `duplicate`）；
  同页换内容 → 409 并保留原页。被拒的页整页零写入。
- **封单** `POST /api/inventory/orders/{id}/seal`：页数齐全才结算，
  否则 409 且不留任何结果；报告与封单状态在**同一事务**内原子落库，
  不会留下半份结果。重复封单幂等返回原报告。
- **报告** `GET /api/inventory/orders/{id}/report`：按**开单时**的本地现役修订列出
  - `missing`：本地现役但备份缺失（需补拷）
  - `mismatched`：备份存在但修订号或摘要与本地现役不符（需补拷，
    `reasons` 标明 `revision`/`summary` 维度，并给出两侧对照值）
  - `extra`：备份多出的成果（本地现役目录中不存在）

  **未封单一律 409**，不返回任何对账结论；已封单报告保持原样，
  封单后传页 → 409，后续入库新修订也不改变已完成报告。
- **重启安全**：单、页、报告全部持久化于 SQLite；服务重启后未封单可续传，
  已封单报告原样可查。

## 目录结构

```
app/
  main.py       FastAPI 路由与应用工厂
  schemas.py    Pydantic 请求/响应模型（中文 Schema 说明）
  database.py   SQLite 连接、显式事务、整批原子提交
  lineage.py    谱系评估（缺失/退役/环检测与引用路径）
  inventory.py  异地备份盘点（开单快照、分页接收、封单报告）
tests/
  test_api.py        谱系与批次端到端自测
  test_inventory.py  盘点对账端到端自测（含重启与并发交错）
Dockerfile
docker-compose.yml
requirements.txt
```

## Docker Compose 启动（推荐）

```bash
docker compose up --build
# 服务: http://localhost:8000
# 可操作文档(Swagger UI): http://localhost:8000/docs
# 备用文档: http://localhost:8000/redoc
```

SQLite 文件位于容器内 `/data/lineage.db`，通过命名卷 `lineage-data` 持久化。
健康检查：`GET /health`。

停止并保留数据：`docker compose down`；连同数据卷一起清除：
`docker compose down -v`。

## 本地直接运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 数据库路径可用环境变量 IMAGE_LINEAGE_DB 覆盖，默认 data/lineage.db
```

## 自测

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/batches` | 分批提交清单（accepted / duplicate / rejected） |
| GET | `/api/batches` | 批次审计列表 |
| GET | `/api/batches/{id}` | 单批次记录 |
| GET | `/api/products/{id}/status` | 现役（或 `?revision=N` 指定）修订的可交付状态与阻断路径 |
| GET | `/api/mosaics` | 全部现役拼图，支持 `?deliverable=true/false` 过滤 |
| GET | `/api/products/{id}/revisions` | 成果修订历史与现役标记 |
| POST | `/api/inventory/orders` | 开盘点单：固定本地现役目录快照与预定页数 |
| GET | `/api/inventory/orders` | 盘点单列表（含分页进度） |
| GET | `/api/inventory/orders/{id}` | 单盘点单进度（不含对账结论） |
| POST | `/api/inventory/orders/{id}/pages` | 上传一页异地目录（同页同内容重试幂等） |
| POST | `/api/inventory/orders/{id}/seal` | 页数齐全后封单，原子生成对账报告（幂等） |
| GET | `/api/inventory/orders/{id}/report` | 获取已定稿报告（未封单 409） |

## 快速示例

```bash
# 1) 拼图先到（父瓦片晚到）
curl -X POST localhost:8000/api/batches -H 'Content-Type: application/json' -d '{
  "items":[{"product_id":"M-100","revision":1,"summary":"年度拼图",
            "parent_product_id":"R-1","parent_revision":1}]}'

# 2) 查看受阻路径
curl localhost:8000/api/products/M-100/status

# 3) 补传父瓦片后自动转为可交付
curl -X POST localhost:8000/api/batches -H 'Content-Type: application/json' -d '{
  "items":[{"product_id":"R-1","revision":1,"summary":"原始瓦片"}]}'
curl localhost:8000/api/products/M-100/status
```

阻断路径元素形如 `M-100@1`、`R-1@1`，从被查询成果逐级上行，末位即问题节点。

## 盘点快速示例

```bash
# 1) 开盘点单：预定 2 页，开单瞬间快照本地现役目录
curl -X POST localhost:8000/api/inventory/orders -H 'Content-Type: application/json' -d '{
  "expected_pages": 2, "note": "异地档案 2026-11 导出"}'
# → {"order_id": 1, "status": "open", "snapshot_size": N, ...}

# 2) 逐页上传异地目录（每页：成果号 + 修订号 + 摘要）
curl -X POST localhost:8000/api/inventory/orders/1/pages -H 'Content-Type: application/json' -d '{
  "page_no": 1, "items": [{"product_id":"R-1","revision":2,"summary":"瓦片v2"}]}'
curl -X POST localhost:8000/api/inventory/orders/1/pages -H 'Content-Type: application/json' -d '{
  "page_no": 2, "items": [{"product_id":"M-100","revision":1,"summary":"年度拼图"}]}'

# 3) 页数齐全后封单，原子生成报告（重复调用幂等）
curl -X POST localhost:8000/api/inventory/orders/1/seal

# 4) 随时取已定稿报告（未封单时 409）
curl localhost:8000/api/inventory/orders/1/report
```

报告中的 `missing`（本地现役但备份缺失）与 `mismatched`（修订/摘要不符）
即需要安排补拷的清单；`extra` 为备份多出的成果。

## 数据模型要点

- `revisions` 表以 `(product_id, revision)` 为主键，父引用为固定的
  `(parent_product_id, parent_revision)`；每条修订不可变（冲突即整批拒收）。
- `batches` 记录每次提交的状态、条目数、实际写入数与时间。
- 盘点五表：`inventory_orders`（单，open/sealed）、`inventory_snapshots`
  （开单时的本地现役快照）、`inventory_pages` + `inventory_page_items`
  （已收目录页，页内容哈希识别幂等重试）、`inventory_reports`
  （封单时与状态同事务写入，此后只读）。
- WAL 模式 + `busy_timeout=30s`，支持读写并发、写事务排队串行化。
