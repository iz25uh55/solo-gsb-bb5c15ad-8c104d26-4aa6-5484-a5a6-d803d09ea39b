"""API 数据模型。金额一律以 *字符串* 收发，杜绝 float 精度问题。"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_DECIMAL_RE = re.compile(r"^-?\d+(\.\d+)?$|^-?\.\d+$")


class LineIn(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    net: str = Field(description="不含税净额，币种最小单位的整数倍，如 '100.00'")
    tax_rate: str = Field(description="税率，0~1 的小数字符串，如 '0.13'")

    @field_validator("net", "tax_rate")
    @classmethod
    def _check_decimal(cls, v: str, info: Any) -> str:
        v = v.strip()
        if not _DECIMAL_RE.match(v):
            raise ValueError(f"{info.field_name} 必须是十进制数字字符串，收到 {v!r}")
        return v


class InvoiceCreate(BaseModel):
    currency: str = Field(description="发票币种 ISO 4217 代码，如 USD / CNY / JPY")
    lines: list[LineIn] = Field(min_length=1)
    target_currency: str | None = Field(
        default=None, description="可选：换算目标币种；提供时必须同时给 exchange_rate"
    )
    exchange_rate: str | None = Field(
        default=None, description="汇率，1 单位发票币种 = exchange_rate 单位目标币种"
    )

    @field_validator("currency", "target_currency")
    @classmethod
    def _upper_currency(cls, v: str | None) -> str | None:
        return v.upper().strip() if v is not None else None

    @field_validator("exchange_rate")
    @classmethod
    def _check_rate(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not _DECIMAL_RE.match(v):
            raise ValueError(f"exchange_rate 必须是十进制数字字符串，收到 {v!r}")
        return v


# ---- 出参（宽松模型，extra 允许，主要服务于 OpenAPI 文档）----

class AllocationOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    line_no: int
    delta_units: int
    remainder: str
    rank: int


class RoundingOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    mode: str
    minor_unit: str
    residual_units: int
    residual_amount: str
    method: str
    allocations: list[AllocationOut]


class ConvertedLineOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    currency: str
    net: str
    tax: str
    gross: str


class LineOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    line_no: int
    description: str | None = None
    tax_rate: str
    currency: str
    net: str
    exact_tax: str
    tax: str
    gross: str
    converted: ConvertedLineOut | None = None


class TotalsOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    currency: str
    total_net: str
    total_tax: str
    total_gross: str


class FxOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    target_currency: str
    target_digits: int
    exchange_rate: str
    rate_direction: str
    total_net: str
    total_tax: str
    total_gross: str
    residual_units: int
    residual_unit: str
    allocations: list[AllocationOut]


class InvoiceResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    rule_id: str
    currency: str
    currency_digits: int
    lines: list[LineOut]
    totals: TotalsOut
    rounding: RoundingOut
    fx: FxOut | None = None
    created_at: str
    recompute_matched: bool


class InvoiceSummary(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    currency: str
    target_currency: str | None = None
    total_net: str
    total_tax: str
    total_gross: str
    line_count: int
    created_at: str


class PagedInvoices(BaseModel):
    items: list[InvoiceSummary]
    page: int
    page_size: int
    total: int
    pages: int


class Health(BaseModel):
    status: str
    database: str


# ---- 收款登记 ----

class PaymentCreate(BaseModel):
    amount: str = Field(
        description="收款金额，发票币种的十进制字符串，必须大于 0 且为币种最小单位的整数倍，如 '33.11'"
    )

    @field_validator("amount")
    @classmethod
    def _check_amount(cls, v: str) -> str:
        v = v.strip()
        if not _DECIMAL_RE.match(v):
            raise ValueError(f"amount 必须是十进制数字字符串，收到 {v!r}")
        return v


class PaymentOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    invoice_id: int
    amount: str
    currency: str
    created_at: str


class PaymentResult(PaymentOut):
    """登记收款成功后的响应：收款记录 + 最新余额。"""

    paid_total: str
    outstanding: str


class PaymentOverview(BaseModel):
    """按发票查询的收款记录与未收余额。"""

    model_config = ConfigDict(extra="allow")
    invoice_id: int
    currency: str
    total_gross: str
    paid_total: str
    outstanding: str
    payment_count: int
    payments: list[PaymentOut]
