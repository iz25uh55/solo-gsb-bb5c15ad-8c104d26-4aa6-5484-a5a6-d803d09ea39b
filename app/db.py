"""SQLite 持久化层。

存储三张表：
* ``invoices``    —— 发票头（含原始请求 JSON，用于复算）与汇总金额；
* ``invoice_lines`` —— 逐行结果（税额残差分配后的最终值，含换算列）；
* ``invoice_payments`` —— 收款登记记录（金额字符串 + 登记时间）。
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

CREATE TABLE IF NOT EXISTS invoice_payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id   INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    amount       TEXT NOT NULL,
    note         TEXT,
    registered_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payments_invoice_id ON invoice_payments(invoice_id);
"""


class OverpaymentError(ValueError):
    """本次收款会使累计收款超过发票含税总额（调用方应回滚事务并返回 422）。"""


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
    # check_same_thread=False：每个请求拥有独立连接（get_db 依赖），但 FastAPI
    # 的依赖 __enter__ 与端点函数在线程池中可能落在不同工作线程；连接生命周期内
    # 不会被两个线程同时使用，故可安全关闭该检查。
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


def list_payments(conn: sqlite3.Connection, invoice_id: int) -> list[sqlite3.Row]:
    """按登记顺序（id 升序）返回某发票的全部收款记录。"""

    return list(
        conn.execute(
            "SELECT * FROM invoice_payments WHERE invoice_id = ? ORDER BY id",
            (invoice_id,),
        ).fetchall()
    )


def register_payment(
    conn: sqlite3.Connection, invoice_id: int, amount: Decimal, note: str | None
) -> tuple[sqlite3.Row, Decimal, Decimal]:
    """登记一笔收款，并在**同一事务**内完成余额校验与写入。

    事务以 ``BEGIN IMMEDIATE`` 开始，立即获取 RESERVED 写锁，使并发收款请求
    串行化：后到的事务必须等前一个事务提交后才能读到最新累计收款，
    因此不会出现两个请求都看到「余额还够」从而超收的情况。

    返回 ``(收款行, 累计已收, 未收余额)``；发票不存在返回 ``None``；
    累计将超过含税总额时抛 :class:`OverpaymentError` 且事务回滚、不留记录。
    """

    previous_isolation = conn.isolation_level
    conn.isolation_level = None  # 切到 autocommit 模式，由我们手动控制事务
    tx_active = True
    try:
        conn.execute("BEGIN IMMEDIATE")
        invoice = conn.execute(
            "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
        ).fetchone()
        if invoice is None:
            conn.rollback()
            tx_active = False
            return None  # type: ignore[return-value]

        total_gross = Decimal(invoice["total_gross"])
        existing = conn.execute(
            "SELECT amount FROM invoice_payments WHERE invoice_id = ? ORDER BY id",
            (invoice_id,),
        ).fetchall()
        already_paid = sum(
            (Decimal(row["amount"]) for row in existing), Decimal("0")
        )
        new_paid = already_paid + amount
        if new_paid > total_gross:
            conn.rollback()
            tx_active = False
            raise OverpaymentError(
                f"发票 {invoice_id} 收款超额：含税总额 {invoice['total_gross']}，"
                f"已收 {format(already_paid, 'f')}，本次申请 {format(amount, 'f')}"
            )

        registered_at = utc_now_iso()
        cur = conn.execute(
            """
            INSERT INTO invoice_payments (invoice_id, amount, note, registered_at)
            VALUES (?, ?, ?, ?)
            """,
            (invoice_id, format(amount, "f"), note, registered_at),
        )
        payment = conn.execute(
            "SELECT * FROM invoice_payments WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        conn.commit()
        tx_active = False
    except Exception:
        if tx_active:
            conn.rollback()
        raise
    finally:
        conn.isolation_level = previous_isolation
    return payment, new_paid, total_gross - new_paid
