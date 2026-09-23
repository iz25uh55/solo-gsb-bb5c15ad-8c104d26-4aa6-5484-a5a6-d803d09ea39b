"""SQLite 持久化层。

存储三张表：
* ``invoices``      —— 发票头（含原始请求 JSON，用于复算）与汇总金额；
* ``invoice_lines`` —— 逐行结果（税额残差分配后的最终值，含换算列）；
* ``payments``      —— 收款登记（每笔收款的金额、币种与登记时间）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    currency        TEXT NOT NULL,
    currency_digits INTEGER NOT NULL,
    target_currency TEXT,
    exchange_rate   TEXT,
    total_net       TEXT NOT NULL,
    total_tax       TEXT NOT NULL,
    total_gross     TEXT NOT NULL,
    conv_total_net   TEXT,
    conv_total_tax   TEXT,
    conv_total_gross TEXT,
    residual_units  INTEGER NOT NULL,
    rule_id         TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_lines (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id   INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    line_no      INTEGER NOT NULL,
    description  TEXT,
    tax_rate     TEXT NOT NULL,
    net          TEXT NOT NULL,
    exact_tax    TEXT NOT NULL,
    tax          TEXT NOT NULL,
    gross        TEXT NOT NULL,
    conv_net     TEXT,
    conv_tax     TEXT,
    conv_gross   TEXT,
    UNIQUE(invoice_id, line_no)
);

CREATE INDEX IF NOT EXISTS idx_invoices_created_at ON invoices(created_at);

CREATE TABLE IF NOT EXISTS payments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    amount     TEXT NOT NULL,
    currency   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payments_invoice_id ON payments(invoice_id);
"""


class InvoiceNotFoundError(Exception):
    """收款登记/查询时发票不存在。"""

    def __init__(self, invoice_id: int) -> None:
        self.invoice_id = invoice_id
        super().__init__(f"发票 {invoice_id} 不存在")


def utc_now_iso() -> str:
    """UTC 时间，ISO 8601 带 Z 后缀，秒精度。"""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def db_path() -> str:
    return os.getenv("DB_PATH", "/data/invoices.db")


def connect(database: str | None = None) -> sqlite3.Connection:
    path = database or db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # check_same_thread=False：FastAPI 的依赖注入与端点可能运行在线程池的
    # 不同线程；连接始终是每请求一条、单请求内串行使用，跨线程创建/使用是安全的
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def save_invoice(conn: sqlite3.Connection, payload: dict[str, Any], result: dict[str, Any]) -> int:
    """在单个事务内写入发票头与行项目，返回发票 id。"""

    with conn:
        cur = conn.execute(
            """
            INSERT INTO invoices (
                currency, currency_digits, target_currency, exchange_rate,
                total_net, total_tax, total_gross,
                conv_total_net, conv_total_tax, conv_total_gross,
                residual_units, rule_id, request_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result["currency"],
                result["currency_digits"],
                result.get("fx", {}).get("target_currency"),
                result.get("fx", {}).get("exchange_rate"),
                result["totals"]["total_net"],
                result["totals"]["total_tax"],
                result["totals"]["total_gross"],
                (result.get("fx") or {}).get("total_net"),
                (result.get("fx") or {}).get("total_tax"),
                (result.get("fx") or {}).get("total_gross"),
                result["rounding"]["residual_units"],
                result["rule_id"],
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                utc_now_iso(),
            ),
        )
        invoice_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO invoice_lines (
                invoice_id, line_no, description, tax_rate,
                net, exact_tax, tax, gross,
                conv_net, conv_tax, conv_gross
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    invoice_id,
                    line["line_no"],
                    line.get("description"),
                    line["tax_rate"],
                    line["net"],
                    line["exact_tax"],
                    line["tax"],
                    line["gross"],
                    (line.get("converted") or {}).get("net"),
                    (line.get("converted") or {}).get("tax"),
                    (line.get("converted") or {}).get("gross"),
                )
                for line in result["lines"]
            ],
        )
    return invoice_id


def get_invoice(conn: sqlite3.Connection, invoice_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
    ).fetchone()


def get_lines(conn: sqlite3.Connection, invoice_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY line_no",
            (invoice_id,),
        ).fetchall()
    )


def list_invoices(
    conn: sqlite3.Connection, page: int, page_size: int
) -> tuple[list[sqlite3.Row], int]:
    total = int(conn.execute("SELECT COUNT(*) AS c FROM invoices").fetchone()["c"])
    offset = (page - 1) * page_size
    rows = list(
        conn.execute(
            "SELECT * FROM invoices ORDER BY id DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
    )
    return rows, total


# ---- 收款登记 ----


def _minor_unit(digits: int) -> Decimal:
    """某币种的 1 个最小单位，例如 digits=2 -> Decimal('0.01')。"""

    return Decimal(1).scaleb(-digits)


def _paid_total(conn: sqlite3.Connection, invoice_id: int) -> Decimal:
    """已收金额合计。用 Decimal 逐笔求和，不走 SQLite 的浮点 SUM 聚合。"""

    rows = conn.execute(
        "SELECT amount FROM payments WHERE invoice_id = ?", (invoice_id,)
    ).fetchall()
    return sum((Decimal(r["amount"]) for r in rows), Decimal("0"))


def register_payment(
    conn: sqlite3.Connection, invoice_id: int, amount: Decimal
) -> dict[str, Any]:
    """登记一笔收款：余额校验与写入在 **同一个事务** 中完成。

    使用 ``BEGIN IMMEDIATE`` 在事务开始即取得写锁，把并发的收款请求串行化，
    「读已收合计 -> 校验未收余额 -> 写入」之间不会被另一笔收款插队，避免超收。

    * 发票不存在 -> :class:`InvoiceNotFoundError`
    * 金额不是币种最小单位整数倍、或累计收款将超过含税总额 -> ``ValueError``
      （事务回滚，不留下任何记录；由 API 层映射为 422）
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        inv = conn.execute(
            "SELECT currency, currency_digits, total_gross FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        if inv is None:
            raise InvoiceNotFoundError(invoice_id)

        digits = int(inv["currency_digits"])
        unit = _minor_unit(digits)
        if (amount / unit) != (amount / unit).to_integral_value():
            raise ValueError(
                f"收款金额 {amount} 不是 {inv['currency']} 最小单位 {unit} 的整数倍"
            )
        amount = amount.quantize(unit)

        gross = Decimal(inv["total_gross"])
        paid = _paid_total(conn, invoice_id)
        outstanding = gross - paid
        if amount > outstanding:
            raise ValueError(
                f"收款 {amount} 超出未收余额 {outstanding}"
                f"（含税总额 {gross}，已收 {paid}），未登记任何记录"
            )

        created_at = utc_now_iso()
        cur = conn.execute(
            """
            INSERT INTO payments (invoice_id, amount, currency, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (invoice_id, format(amount, "f"), inv["currency"], created_at),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    new_paid = paid + amount
    return {
        "id": int(cur.lastrowid),
        "invoice_id": invoice_id,
        "amount": format(amount, "f"),
        "currency": inv["currency"],
        "created_at": created_at,
        "paid_total": format(new_paid, "f"),
        "outstanding": format(gross - new_paid, "f"),
    }


def payment_overview(conn: sqlite3.Connection, invoice: sqlite3.Row) -> dict[str, Any]:
    """某张发票的收款记录列表与未收余额（供 GET 使用）。"""

    rows = list(
        conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ? ORDER BY id",
            (invoice["id"],),
        ).fetchall()
    )
    unit = _minor_unit(int(invoice["currency_digits"]))
    paid = sum((Decimal(r["amount"]) for r in rows), Decimal("0")).quantize(unit)
    gross = Decimal(invoice["total_gross"])
    return {
        "invoice_id": invoice["id"],
        "currency": invoice["currency"],
        "total_gross": invoice["total_gross"],
        "paid_total": format(paid, "f"),
        "outstanding": format(gross - paid, "f"),
        "payment_count": len(rows),
        "payments": [
            {
                "id": r["id"],
                "invoice_id": r["invoice_id"],
                "amount": r["amount"],
                "currency": r["currency"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }
