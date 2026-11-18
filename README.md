# 随访数据去标识包服务

面向临床随访数据的按队列（cohort）修订受理与去标识包发布服务。数据员上传包含
**受试者号、访视日期、测值** 的完整 CSV 修订，系统在后台生成只含
**稳定假名、平移后日期、测值** 的 zip 包供研究组下载做纵向分析。原始身份与原始
日期永远不会出现在包内，也不会通过任何接口返回。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

启动后浏览器打开 <http://localhost:8000> 即可使用：

1. 填写队列 ID 与修订号，选择完整 CSV 并“受理修订”；
2. 列表每 2.5 秒自动刷新，包状态依次为 `待生成 → 生成中 → 已发布`；
3. 已发布后点击“下载当前包”。API 交互文档见 <http://localhost:8000/docs>。

数据保存在命名卷 `deid-data`（容器内 `/data`，含 SQLite 库、原始上传与生成的包），
容器重启不丢失。

### 不用 Docker 的本地运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DEID_DATA_DIR=./data uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## CSV 格式

UTF-8 编码，表头 + 至少一行数据，恰好 3 列，中英文表头均可：

```csv
受试者号,访视日期,测值
P001,2026-01-10,12.5
P001,2026-02-10,13.0
```

日期必须为 `YYYY-MM-DD`，测值必须是有限数字。空文件、缺列、坏日期/数字、无数据行
一律返回 400 拒绝，不会产生修订。

## 去标识规则

| 项目 | 规则 |
| --- | --- |
| 假名 | 队列内按首次出现顺序分配 `S000001`、`S000002`…，映射只存服务端数据库，永不导出 |
| 跨修订稳定性 | 同一队列同一受试者的假名与日期偏移在所有修订间保持不变（身份表只追加） |
| 日期平移 | 每个受试者一个固定偏移（-180…180 天），由队列随机盐 + 原受试者号 HMAC-SHA256 派生；同一受试者访视间隔精确不变 |
| 队列隔离 | 每个队列独立随机盐，跨队列独立生成，假名与偏移互不相关 |
| 出包内容 | 仅 `pseudonym, visit_date_shifted, value` 加一份结构清单 `manifest.json`；原始 ID、原始日期、盐均不在包内 |

## 修订与包的生命周期

- **受理即撤旧**：新修订受理（HTTP 202）的同一事务内，该队列旧包（含已发布包）立即
  标记为 `superseded`，新包进入 `pending`，旧包立刻失去下载资格。
- **同修订冲突拒绝**：同一 `(队列, 修订号)` 内容哈希不同 → 409，拒绝且不改动任何数据；
  字节级重复上传 → 200 幂等 no-op。
- **完整修订原则**：每次上传必须是该修订的完整 CSV；一个发布的包只来自同一完整修订，
  原始文件按 `队列/revN.csv` 落盘并从该文件构建。
- **失败可重试**：生成失败标记 `failed`（含错误信息、指数退避自动重试），不可下载；
  重启后立即回到 `pending`，界面/接口可手动“立即重试”。
- **重启恢复**：进程崩溃时停留在 `generating` 的包，启动时统一复位为 `pending`，
  绝不下发半成品（包文件采用临时文件 + 原子 rename）。
- **迟到生成不覆盖**：构建完成的落库 UPDATE 带条件——仅当包仍是 `generating` 且该修订
  仍是队列当前修订时才发布；否则丢弃产物，新修订不受影响。
- **队列互不阻塞**：单后台线程串行构建，一个队列待生成/失败不影响其他队列已发布包下载。

包状态机：`pending → generating → published`，失败到 `failed`（可回 `pending`），
被新修订取代到 `superseded`（终态）。只有当前修订且 `published` 的包可下载
（pending/generating/failed 返回 409，文件缺失自动重新排队）。

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/cohorts/{cohort_id}/revisions/{revision}` | 上传完整修订 CSV（multipart 字段 `file`） |
| GET | `/api/cohorts` | 队列总览与当前包状态 |
| GET | `/api/cohorts/{cohort_id}` | 队列详情（全部修订与包状态） |
| GET | `/api/cohorts/{cohort_id}/package` | 下载当前已发布去标识包（zip） |
| POST | `/api/cohorts/{cohort_id}/retry` | 失败包立即重新排队 |
| GET | `/health` | 健康检查 |

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

22 个用例覆盖端到端去标识、跨修订假名/偏移稳定、队列独立、冲突拒绝、幂等上传、
失败重试、重启恢复、迟到构建丢弃、其他队列不受影响与非法输入拒绝。

## 目录结构

```
app/            FastAPI 应用（config/database/csvutil/deid/services/worker/main）
static/         浏览器单页入口
tests/          pytest 测试
data/           运行期数据（.gitignore 排除，不入库）
Dockerfile / docker-compose.yml
```
