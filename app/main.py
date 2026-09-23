"""发票税额计算 API（FastAPI）。

启动：uvicorn app.main:app
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import db as database
from .calc import compute_invoice, normalize_money, recompute_matches, to_decimal
from .currency import UnknownCurrencyError
from .schemas import (
    Health,
    InvoiceCreate,
    InvoiceResult,
    PagedInvoices,
    PaymentCreate,
    PaymentLedger,
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
    version="1.0.0",
    description=(
        "基于 Decimal 的发票税额计算服务：按币种最小单位舍入、"
        "最大余数法分配残差、可复算，支持跨币种换算，SQLite 持久化。"
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


# ---- 收款登记 ----

def _money_str(value: Decimal, digits: int) -> str:
    """按币种最小单位规范化输出：USD 下 0 -> '0.00'，JPY 下 0 -> '0'。"""

    return format(value.quantize(Decimal(1).scaleb(-digits)), "f")


def _payment_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "invoice_id": row["invoice_id"],
        "amount": row["amount"],
        "note": row["note"],
        "registered_at": row["registered_at"],
    }


def _ledger(
    conn: sqlite3.Connection, invoice_id: int
) -> tuple[sqlite3.Row | None, dict[str, Any]]:
    """组装某发票的收款台账（含未收余额）。发票不存在时 row 为 None。"""

    row = database.get_invoice(conn, invoice_id)
    if row is None:
        return None, {}
    digits = int(row["currency_digits"])
    total_gross = Decimal(row["total_gross"])
    payments = [_payment_to_dict(r) for r in database.list_payments(conn, invoice_id)]
    paid_amount = sum((Decimal(p["amount"]) for p in payments), Decimal("0"))
    return row, {
        "invoice_id": invoice_id,
        "currency": row["currency"],
        "total_gross": _money_str(total_gross, digits),
        "paid_amount": _money_str(paid_amount, digits),
        "balance_due": _money_str(total_gross - paid_amount, digits),
        "payments": payments,
    }


@app.get(
    "/api/v1/invoices/{invoice_id}/payments",
    response_model=PaymentLedger,
    tags=["payments"],
)
def list_invoice_payments(
    invoice_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any]:
    """按发票 id 查询收款记录与未收余额。"""

    row, ledger = _ledger(conn, invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"发票 {invoice_id} 不存在")
    return ledger


@app.post(
    "/api/v1/invoices/{invoice_id}/payments",
    response_model=PaymentResult,
    status_code=201,
    tags=["payments"],
)
def register_invoice_payment(
    invoice_id: int,
    payload: PaymentCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """登记一笔收款。

    金额使用发票币种的十进制字符串、必须大于 0（且为该币种最小单位整数倍）；
    同一发票累计收款不得超过含税总额，超额返回 422 且不留下记录。
    """

    row = database.get_invoice(conn, invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"发票 {invoice_id} 不存在")

    currency = row["currency"]
    digits = int(row["currency_digits"])
    amount = normalize_money(
        to_decimal(payload.amount, "amount"), currency, digits, "amount"
    )
    if amount <= 0:
        raise ValueError(f"amount 必须大于 0，收到 {payload.amount!r}")

    # 余额校验与写入在同一个 BEGIN IMMEDIATE 事务内完成，避免并发超收
    inserted = database.register_payment(conn, invoice_id, amount, payload.note)
    if inserted is None:
        # 事务内复查时发票已不存在（极端并发删除）
        raise HTTPException(status_code=404, detail=f"发票 {invoice_id} 不存在")
    payment, paid_amount, balance = inserted

    return {
        "invoice_id": invoice_id,
        "currency": currency,
        "total_gross": _money_str(Decimal(row["total_gross"]), digits),
        "paid_amount": _money_str(paid_amount, digits),
        "balance_due": _money_str(balance, digits),
        "payment": _payment_to_dict(payment),
    }
