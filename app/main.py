"""发票税额计算 API（FastAPI）。

启动：uvicorn app.main:app
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import db as database
from .calc import compute_invoice, recompute_matches, to_decimal
from .currency import UnknownCurrencyError
from .schemas import (
    Health,
    InvoiceCreate,
    InvoiceResult,
    PagedInvoices,
    PaymentCreate,
    PaymentOverview,
    PaymentResult,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # 启动时确保表结构存在
    with database.connect() as conn:
        database.init_db(conn)
    yield


app = FastAPI(
    title="发票税额计算 API",
    version="1.1.0",
    description=(
        "基于 Decimal 的发票税额计算服务：按币种最小单位舍入、"
        "最大余数法分配残差、可复算，支持跨币种换算、收款登记与未收余额查询，"
        "SQLite 持久化。"
    ),
    lifespan=lifespan,
)


def get_db() -> Iterator[sqlite3.Connection]:
    """每请求一个 SQLite 连接，请求结束关闭。"""

    conn = database.connect()
    try:
        yield conn
    finally:
        conn.close()


@app.exception_handler(ValueError)
async def _value_error_handler(_request: Any, exc: ValueError) -> JSONResponse:
    """计算/校验错误统一返回 422（UnknownCurrencyError 也是 ValueError 子类）。"""

    return JSONResponse(status_code=422, content={"detail": str(exc)})


def _row_to_result(row: sqlite3.Row, line_rows: list[sqlite3.Row]) -> dict[str, Any]:
    """用数据库行重建与 compute_invoice 同构的结果，并用原始请求做复算校验。"""

    stored_request = json.loads(row["request_json"])
    fx_target = row["target_currency"]

    lines: list[dict[str, Any]] = []
    for lr in line_rows:
        line: dict[str, Any] = {
            "line_no": lr["line_no"],
            "description": lr["description"],
            "tax_rate": lr["tax_rate"],
            "currency": row["currency"],
            "net": lr["net"],
            "exact_tax": lr["exact_tax"],
            "tax": lr["tax"],
            "gross": lr["gross"],
        }
        if fx_target is not None and lr["conv_net"] is not None:
            line["converted"] = {
                "currency": fx_target,
                "net": lr["conv_net"],
                "tax": lr["conv_tax"],
                "gross": lr["conv_gross"],
            }
        lines.append(line)

    # 残差分配明细不单独建列，复算一次取回（纯函数，结果确定）
    fresh = compute_invoice(stored_request)
    result: dict[str, Any] = {
        "rule_id": row["rule_id"],
        "currency": row["currency"],
        "currency_digits": row["currency_digits"],
        "lines": lines,
        "totals": {
            "currency": row["currency"],
            "total_net": row["total_net"],
            "total_tax": row["total_tax"],
            "total_gross": row["total_gross"],
        },
        "rounding": fresh["rounding"],
    }
    if fx_target is not None:
        result["fx"] = {
            "target_currency": fx_target,
            "target_digits": fresh["fx"]["target_digits"],
            "exchange_rate": row["exchange_rate"],
            "rate_direction": fresh["fx"]["rate_direction"],
            "total_net": row["conv_total_net"],
            "total_tax": row["conv_total_tax"],
            "total_gross": row["conv_total_gross"],
            "residual_units": fresh["fx"]["residual_units"],
            "residual_unit": fresh["fx"]["residual_unit"],
            "allocations": fresh["fx"]["allocations"],
        }

    core = {k: v for k, v in result.items() if k not in ("id", "created_at", "recompute_matched")}
    result["id"] = row["id"]
    result["created_at"] = row["created_at"]
    result["recompute_matched"] = recompute_matches(core, stored_request)
    return result


# ---- 端点 ----

@app.get("/health", response_model=Health, tags=["health"])
def health(conn: sqlite3.Connection = Depends(get_db)) -> Health:
    try:
        conn.execute("SELECT 1").fetchone()
    except Exception:  # pragma: no cover
        raise HTTPException(status_code=503, detail="database unavailable")
    return Health(status="ok", database="up")


@app.post(
    "/api/v1/invoices",
    response_model=InvoiceResult,
    status_code=201,
    tags=["invoices"],
)
def create_invoice(
    payload: InvoiceCreate, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any]:
    data = payload.model_dump()
    try:
        result = compute_invoice(data)
    except UnknownCurrencyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    invoice_id = database.save_invoice(conn, data, result)
    row = database.get_invoice(conn, invoice_id)
    line_rows = database.get_lines(conn, invoice_id)
    return _row_to_result(row, line_rows)


@app.get(
    "/api/v1/invoices/{invoice_id}",
    response_model=InvoiceResult,
    tags=["invoices"],
)
def read_invoice(
    invoice_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any]:
    row = database.get_invoice(conn, invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"发票 {invoice_id} 不存在")
    return _row_to_result(row, database.get_lines(conn, invoice_id))


@app.get(
    "/api/v1/invoices",
    response_model=PagedInvoices,
    tags=["invoices"],
)
def list_invoices(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数，1~100"),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    rows, total = database.list_invoices(conn, page, page_size)
    items: list[dict[str, Any]] = []
    for row in rows:
        count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM invoice_lines WHERE invoice_id = ?",
                (row["id"],),
            ).fetchone()["c"]
        )
        items.append(
            {
                "id": row["id"],
                "currency": row["currency"],
                "target_currency": row["target_currency"],
                "total_net": row["total_net"],
                "total_tax": row["total_tax"],
                "total_gross": row["total_gross"],
                "line_count": count,
                "created_at": row["created_at"],
            }
        )
    pages = (total + page_size - 1) // page_size if total else 0
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
    }


@app.post(
    "/api/v1/invoices/{invoice_id}/payments",
    response_model=PaymentResult,
    status_code=201,
    tags=["payments"],
)
def register_payment(
    invoice_id: int,
    payload: PaymentCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    amount = to_decimal(payload.amount, "amount")
    if amount <= 0:
        # 走全局 ValueError -> 422 处理器
        raise ValueError(f"收款金额必须大于 0，收到 {payload.amount!r}")
    try:
        return database.register_payment(conn, invoice_id, amount)
    except database.InvoiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/api/v1/invoices/{invoice_id}/payments",
    response_model=PaymentOverview,
    tags=["payments"],
)
def read_payments(
    invoice_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any]:
    row = database.get_invoice(conn, invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"发票 {invoice_id} 不存在")
    return database.payment_overview(conn, row)
