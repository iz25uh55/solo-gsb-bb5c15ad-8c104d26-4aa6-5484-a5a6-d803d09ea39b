# 发票税额计算 API（Invoice Tax Calculator）

基于 **FastAPI + SQLite + Decimal** 的发票税额计算服务，支持 Docker Compose 一键启动。

- **全程 `Decimal` 高精度运算**：金额入参/出参一律为十进制字符串，绝不使用 `float`。
- **按币种最小单位舍入**：`ROUND_HALF_UP`（四舍五入）。USD/CNY 等按 0.01，JPY/KRW 按 1，KWD/BHD 按 0.001。
- **残差固定分配且可复算**：发票总额与「逐行舍入后税额之和」的差（残差）按**最大余数法**确定性地分配回具体行，任何时候用同一请求复算结果完全一致（响应中带 `recompute_matched` 标志）。
- **跨币种换算**：按请求汇率换算，原币金额完整保留，目标币独立做最小单位舍入与残差分配。
- **收款登记**：按发票登记收款（发票币种字符串、必须大于 0），随时查询收款记录与未收余额；累计收款不得超过含税总额，校验与写入在同一 SQLite 事务中完成，并发不会超收。
- **SQLite 持久化**（WAL 模式），提供创建、按 id 查询、分页列表、健康检查。
- 内置 OpenAPI 交互文档（Swagger UI / ReDoc）。

---

## 1. 快速开始（Docker Compose）

前置条件：已安装 Docker（含 Compose v2：`docker compose`）。

```bash
# 构建镜像并后台启动
docker compose up -d --build

# 查看状态 / 日志
docker compose ps
docker compose logs -f

# 停止
docker compose down

# 停止并同时删除数据卷（清空所有发票数据）
docker compose down -v
```

启动后服务监听 **`http://localhost:8000`**：

| 地址 | 说明 |
| --- | --- |
| <http://localhost:8000/health> | 健康检查 |
| <http://localhost:8000/docs> | Swagger UI（可在线发请求） |
| <http://localhost:8000/redoc> | ReDoc 文档 |
| <http://localhost:8000/openapi.json> | OpenAPI 3.1 Schema |

> 宿主机端口默认 `8000`，被占用时可用 `API_PORT=8080 docker compose up -d` 改到 8080。

### 快速冒烟

```bash
curl -s http://localhost:8000/health
# {"status":"ok","database":"up"}

curl -s -X POST http://localhost:8000/api/v1/invoices \
  -H 'Content-Type: application/json' \
  -d '{
    "currency": "USD",
    "lines": [
      {"description": "笔记本", "net": "10.05", "tax_rate": "0.10"},
      {"description": "签字笔", "net": "20.05", "tax_rate": "0.10"}
    ]
  }'

# 登记一笔收款并查看未收余额（假设上一步返回的发票 id 为 1）
curl -s -X POST http://localhost:8000/api/v1/invoices/1/payments \
  -H 'Content-Type: application/json' \
  -d '{"amount": "20.00"}'

curl -s http://localhost:8000/api/v1/invoices/1/payments
```

该请求每行精确税额为 1.005 / 2.005（逐行 HALF_UP 得 1.01 / 2.01），但总税额
3.01（30.10×10%），行和 3.02，产生 **-0.01 残差**，按最大余数法从零头最小的
第 1 行扣减 1 个最小单位：

```json
{
  "id": 1,
  "rule_id": "half_up_plus_largest_remainder_v1",
  "currency": "USD",
  "currency_digits": 2,
  "lines": [
    {"line_no": 1, "description": "笔记本", "tax_rate": "0.10", "currency": "USD",
     "net": "10.05", "exact_tax": "1.005", "tax": "1.00", "gross": "11.05"},
    {"line_no": 2, "description": "签字笔", "tax_rate": "0.10", "currency": "USD",
     "net": "20.05", "exact_tax": "2.005", "tax": "2.01", "gross": "22.06"}
  ],
  "totals": {"currency": "USD", "total_net": "30.10", "total_tax": "3.01", "total_gross": "33.11"},
  "rounding": {
    "mode": "ROUND_HALF_UP",
    "minor_unit": "0.01",
    "residual_units": -1,
    "residual_amount": "-0.01",
    "method": "largest_remainder",
    "allocations": [{"line_no": 1, "delta_units": -1, "remainder": "0.005", "rank": 1}]
  },
  "created_at": "2026-09-23T01:40:00Z",
  "recompute_matched": true
}
```

注意不变量：**逐行 `tax` 之和恒等于 `totals.total_tax`**（1.00 + 2.01 = 3.01）。

---

## 2. 计算规则（固定，可复算）

规则版本号 `rule_id = half_up_plus_largest_remainder_v1`：

1. 每行 **精确税额 = net × tax_rate**，Decimal 精度 50 位，全程无 float。
2. 每行税额按**币种最小单位**做 `ROUND_HALF_UP`。
3. 发票总税额 = Σ 各行精确税额后再做一次 `ROUND_HALF_UP`；它与逐行舍入税额之和
   的差称为**残差**（单位为最小单位的整数倍，`|残差| ≤ 行数`）。
4. 残差按 **最大余数法（largest remainder / Hare quota）** 分配：
   - **正残差**（总额更大，需补分）：按「精确税额相对其最小单位向下取整的零头」
     **从大到小**选行，每行 **+1 个最小单位**，分完为止；
   - **负残差**（需扣减）：按零头**从小到大**选行，每行 **-1 个最小单位**；
   - **零头相同按行号（请求中的先后顺序）决胜负**，因此结果完全确定、可复算。
   - 分配明细（哪一行、±几个最小单位、零头、名次）在响应的
     `rounding.allocations` 中可见、可审计。
5. **跨币种**：`target_currency` 与 `currency` 不同时必须提供 `exchange_rate`，
   标价方向为 `1 发票币种 = exchange_rate 目标币种`。原币行金额与总额原样保留
   （`lines[].net/tax/gross`、`totals`）；换算结果放在每行的 `converted` 与顶层
   `fx` 段，目标币独立执行一次相同的舍入 + 残差分配，同样有
   `fx.allocations` 审计明细。
6. 每次 `GET` 单张发票时，服务会用库里保存的原始请求重新计算并与存储结果比对，
   `recompute_matched=true` 即表示结果可被同一规则完整复现。

负数净额（冲红/贷记凭证）同样支持，规则不变。

### 支持的币种小数位

- 0 位：JPY、KRW、VND、XAF、XOF、ISK、CLP 等；
- 2 位：USD、EUR、CNY、GBP、HKD、AUD、CAD、SGD 等 ISO 4217 常见币种；
- 3 位：KWD、BHD、JOD、OMR、TND 等；
- 4 位：CLF、UYW。

不在内置 ISO 4217 表内的币种会返回 `422`；可用环境变量 `CURRENCY_DIGITS` 扩展
（见下文配置）。

---

## 3. API 一览

Base URL：`http://localhost:8000`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查（含数据库探活） |
| `POST` | `/api/v1/invoices` | 创建并计算发票（持久化后返回完整结果），`201` |
| `GET` | `/api/v1/invoices/{id}` | 按 id 查询单张发票（含逐行明细与复算标志） |
| `GET` | `/api/v1/invoices?page=1&page_size=20` | 分页列表（按 id 倒序），`page_size` 范围 1~100 |
| `POST` | `/api/v1/invoices/{id}/payments` | 登记一笔收款（发票币种字符串、必须大于 0），`201` |
| `GET` | `/api/v1/invoices/{id}/payments` | 查询该发票的收款记录与未收余额 |
| `GET` | `/docs` `/redoc` `/openapi.json` | 交互式 API 文档 |

### 3.1 创建发票 `POST /api/v1/invoices`

请求体（**所有金额/税率/汇率都用字符串**）：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `currency` | string | 是 | 发票币种 ISO 4217 代码，如 `USD` |
| `lines[].net` | string | 是 | 不含税净额，必须是该币种最小单位的整数倍（如 USD 下 `100.00`，JPY 下 `100`）；不得为 `0`，负数表示冲红 |
| `lines[].tax_rate` | string | 是 | 税率，`0`~`1` 小数，`0.13` 表示 13% |
| `lines[].description` | string | 否 | 行备注，最长 500 字 |
| `target_currency` | string | 否 | 换算目标币种；与 `currency` 不同时必填 `exchange_rate` |
| `exchange_rate` | string | 条件必填 | 汇率：`1 currency = exchange_rate target_currency`，必须为正数 |

跨币种示例：

```bash
curl -s -X POST http://localhost:8000/api/v1/invoices \
  -H 'Content-Type: application/json' \
  -d '{
    "currency": "USD",
    "target_currency": "CNY",
    "exchange_rate": "7.123456",
    "lines": [
      {"description": "咨询服务", "net": "100.00", "tax_rate": "0.13"},
      {"description": "差旅",     "net": "50.00",  "tax_rate": "0.06"}
    ]
  }'
```

要点：原币 `totals.total_tax = 16.00`；换算到 CNY 后逐行税额 92.60/21.37，
精确合计 113.975296 → 113.98，产生 +0.01 残差，分给零头更大的第 1 行
（`fx.allocations`），最终 CNY 税额 92.61 + 21.37 = **113.98**。

### 3.2 查询与列表

```bash
# 单张
curl -s http://localhost:8000/api/v1/invoices/1

# 分页列表
curl -s 'http://localhost:8000/api/v1/invoices?page=1&page_size=20'
```

分页响应：

```json
{
  "items": [
    {"id": 2, "currency": "USD", "target_currency": "CNY",
     "total_net": "150.00", "total_tax": "16.00", "total_gross": "166.00",
     "line_count": 2, "created_at": "2026-09-23T01:41:00Z"}
  ],
  "page": 1, "page_size": 20, "total": 2, "pages": 1
}
```

### 3.3 收款登记与未收余额

**登记收款** `POST /api/v1/invoices/{id}/payments`：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `amount` | string | 是 | 收款金额，**发票币种**的十进制字符串；必须 **大于 0**，且为币种最小单位的整数倍（USD 下 `20.00`，JPY 下 `2000`） |

规则：

- 每张发票的**累计收款不得超过含税总额** `total_gross`；超额请求返回 `422`，
  事务回滚，**不留下任何收款记录**。
- 余额校验与收款写入在**同一个 SQLite 事务**中完成（`BEGIN IMMEDIATE` 串行化
  并发写入），并发登记不会超收。
- 每笔收款保存登记时间 `created_at`（UTC，ISO 8601）。

```bash
# 登记 20.00 USD（发票 total_gross = 33.11）
curl -s -X POST http://localhost:8000/api/v1/invoices/1/payments \
  -H 'Content-Type: application/json' \
  -d '{"amount": "20.00"}'
```

响应 `201`（收款记录 + 最新余额）：

```json
{
  "id": 1,
  "invoice_id": 1,
  "amount": "20.00",
  "currency": "USD",
  "created_at": "2026-09-23T02:10:00Z",
  "paid_total": "20.00",
  "outstanding": "13.11"
}
```

**查询收款记录与未收余额** `GET /api/v1/invoices/{id}/payments`：

```bash
curl -s http://localhost:8000/api/v1/invoices/1/payments
```

```json
{
  "invoice_id": 1,
  "currency": "USD",
  "total_gross": "33.11",
  "paid_total": "20.00",
  "outstanding": "13.11",
  "payment_count": 1,
  "payments": [
    {"id": 1, "invoice_id": 1, "amount": "20.00", "currency": "USD",
     "created_at": "2026-09-23T02:10:00Z"}
  ]
}
```

### 3.4 错误码

| 状态码 | 场景 |
| --- | --- |
| `404` | 发票 id 不存在（含收款登记/查询时） |
| `422` | 参数错误：金额非十进制字符串、净额不满足最小单位、税率越界、跨币种缺汇率、未知币种、收款金额 ≤ 0、收款小于币种最小单位、累计收款超过含税总额等（响应体 `detail` 为中文说明） |

---

## 4. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `/data/invoices.db` | SQLite 文件路径；容器内位于数据卷 `/data`（WAL 旁路文件 `invoices.db-wal` / `-shm` 同目录） |
| `CURRENCY_DIGITS` | 空 | 追加/覆盖币种小数位，逗号分隔，如 `KWD:3,ABC:0`，取值 0~4 |
| `API_PORT`（仅 Compose） | `8000` | 宿主机映射端口，容器内固定 8000 |

容器内服务监听 `0.0.0.0:8000`，SQLite 数据保存在命名卷 `invoice-data` 中，
`docker compose down` 不会删数据；`docker compose down -v` 才会删除。

---

## 5. 本地开发（不用 Docker）

要求 Python 3.11+。

```bash
python -m venv .venv
source .venv/bin/1            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 默认 DB_PATH=/data/invoices.db，本地运行请改到可写目录
export DB_PATH="$(pwd)/data/invoices.db"
mkdir -p data
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## 6. 运行测试

```bash
pip install -r requirements.txt

# 计算核心（舍入 / 残差分配 / 跨币种 / 确定性），仅依赖标准库
python -m unittest tests.test_calc -v

# API 端到端（自动在空闲端口起真实 uvicorn：健康检查、增查、分页、收款登记/余额、
# 超额 422 不留记录、并发不超收、持久化）
python -m unittest tests.test_api -v
```

---

## 7. 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由：健康检查 / 创建 / 查询 / 分页 / 收款登记与余额查询
│   ├── calc.py        # Decimal 计算核心：HALF_UP + 最大余数法残差分配（纯函数）
│   ├── currency.py    # ISO 4217 币种最小单位表 + CURRENCY_DIGITS 扩展
│   ├── schemas.py     # Pydantic 入参/出参模型（金额一律字符串）
│   └── db.py          # SQLite 建表 / 事务写入（发票 + 收款）/ 查询（WAL）
├── tests/
│   ├── test_calc.py   # 20 个计算规则单测
│   └── test_api.py    # 真实 HTTP 端到端测试（含并发收款不超收）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

存储模型：`invoices`（发票头 + 汇总金额 + 原始请求 JSON，供复算）、
`invoice_lines`（逐行最终税额/含税及换算列）与 `payments`（收款登记：金额、
币种、登记时间）三张表，均通过外键级联删除。
