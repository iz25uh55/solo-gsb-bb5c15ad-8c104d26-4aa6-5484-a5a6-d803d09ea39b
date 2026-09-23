"""端到端测试：真实 uvicorn 起服务 + stdlib urllib 调用，覆盖
创建 / 查询 / 分页 / 健康检查 / 422 / 复算标志 / SQLite 持久化。

运行：先 pip install -r requirements.txt，再
    python -m unittest tests.test_api -v
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerThread(threading.Thread):
    def __init__(self, db_file: str, port: int) -> None:
        super().__init__(daemon=True)
        self.db_file = db_file
        self.port = port

    def run(self) -> None:
        uvicorn.run(
            "app.main:app",
            host="127.0.0.1",
            port=self.port,
            log_level="error",
        )


class ApiTests(unittest.TestCase):
    server: ServerThread
    tmpdir: tempfile.TemporaryDirectory
    base: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.TemporaryDirectory()
        db_file = str(Path(cls.tmpdir.name) / "test.db")
        os.environ["DB_PATH"] = db_file
        port = _free_port()
        cls.base = f"http://127.0.0.1:{port}"
        cls.server = ServerThread(db_file, port)
        cls.server.start()
        cls._wait_ready()

    @classmethod
    def _wait_ready(cls, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{cls.base}/health", timeout=1) as resp:
                    if resp.status == 200:
                        return
            except Exception as exc:  # 启动期间连接被拒是正常的
                last = exc
                time.sleep(0.2)
        raise RuntimeError(f"服务未在 {timeout}s 内就绪: {last}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmpdir.cleanup()
        os.environ.pop("DB_PATH", None)

    def _request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_01_health(self) -> None:
        status, body = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "database": "up"})

    def test_02_create_and_get(self) -> None:
        payload = {
            "currency": "USD",
            "lines": [
                {"description": "books", "net": "10.05", "tax_rate": "0.10"},
                {"description": "pens", "net": "20.05", "tax_rate": "0.10"},
            ],
        }
        status, body = self._request("POST", "/api/v1/invoices", payload)
        self.assertEqual(status, 201, body)
        self.assertIn("id", body)
        self.assertEqual(body["totals"]["total_tax"], "3.01")
        self.assertEqual(body["rounding"]["residual_units"], -1)
        self.assertTrue(body["recompute_matched"])

        invoice_id = body["id"]
        status2, fetched = self._request("GET", f"/api/v1/invoices/{invoice_id}")
        self.assertEqual(status2, 200)
        self.assertEqual(fetched["totals"], body["totals"])
        self.assertTrue(fetched["recompute_matched"])
        self.assertEqual(len(fetched["lines"]), 2)

    def test_03_create_fx(self) -> None:
        payload = {
            "currency": "USD",
            "target_currency": "JPY",
            "exchange_rate": "150.25",
            "lines": [{"net": "1.00", "tax_rate": "0.10"}],
        }
        status, body = self._request("POST", "/api/v1/invoices", payload)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["fx"]["target_currency"], "JPY")
        # 150.25 -> 150 JPY；税 15.025 -> 15 JPY
        self.assertEqual(body["lines"][0]["converted"]["net"], "150")
        self.assertEqual(body["lines"][0]["converted"]["tax"], "15")
        # 原币保留
        self.assertEqual(body["lines"][0]["net"], "1.00")
        self.assertEqual(body["lines"][0]["tax"], "0.10")

    def test_04_validation_error(self) -> None:
        # 净额不是最小单位整数倍
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "1.005", "tax_rate": "0.1"}]},
        )
        self.assertEqual(status, 422)
        self.assertIn("detail", body)

        # 未知币种
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "ZZZ", "lines": [{"net": "1.00", "tax_rate": "0.1"}]},
        )
        self.assertEqual(status, 422)

        # pydantic 校验：金额必须是字符串
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": 1.0, "tax_rate": "0.1"}]},
        )
        self.assertEqual(status, 422)

    def test_05_not_found(self) -> None:
        status, body = self._request("GET", "/api/v1/invoices/999999")
        self.assertEqual(status, 404)

    def test_06_pagination_and_persistence(self) -> None:
        # 再建 3 张，配合前两张共 5 张
        for i in range(3):
            status, body = self._request(
                "POST",
                "/api/v1/invoices",
                {"currency": "CNY", "lines": [{"net": "100.00", "tax_rate": "0.13"}]},
            )
            self.assertEqual(status, 201)

        status, body = self._request("GET", "/api/v1/invoices?page=1&page_size=2")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 5)
        self.assertEqual(body["pages"], 3)
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["page_size"], 2)
        self.assertEqual(len(body["items"]), 2)
        # 按 id 倒序
        self.assertGreater(body["items"][0]["id"], body["items"][1]["id"])
        self.assertEqual(body["items"][0]["line_count"], 1)
        self.assertEqual(body["items"][0]["currency"], "CNY")

        status, body = self._request("GET", "/api/v1/invoices?page=3&page_size=2")
        self.assertEqual(len(body["items"]), 1)

        # 分页参数越界 -> 422
        status, _ = self._request("GET", "/api/v1/invoices?page=0")
        self.assertEqual(status, 422)

    def test_07_persisted_db_file(self) -> None:
        # SQLite 文件确实落盘
        self.assertTrue(os.path.exists(os.environ["DB_PATH"]))
        self.assertGreater(os.path.getsize(os.environ["DB_PATH"]), 0)

    def test_08_payments(self) -> None:
        # 建一张 USD 发票：total_gross = 33.11
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {
                "currency": "USD",
                "lines": [
                    {"net": "10.05", "tax_rate": "0.10"},
                    {"net": "20.05", "tax_rate": "0.10"},
                ],
            },
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        self.assertEqual(inv["totals"]["total_gross"], "33.11")

        # 初始：无收款，未收余额 = 含税总额
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(ov["total_gross"], "33.11")
        self.assertEqual(ov["paid_total"], "0.00")
        self.assertEqual(ov["outstanding"], "33.11")
        self.assertEqual(ov["payment_count"], 0)
        self.assertEqual(ov["payments"], [])

        # 登记第一笔收款
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "20"}
        )
        self.assertEqual(status, 201, pay)
        self.assertEqual(pay["invoice_id"], invoice_id)
        self.assertEqual(pay["amount"], "20.00")  # 按币种最小单位规范化
        self.assertEqual(pay["currency"], "USD")
        self.assertIn("created_at", pay)
        self.assertEqual(pay["paid_total"], "20.00")
        self.assertEqual(pay["outstanding"], "13.11")

        # 第二笔：恰好收满
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "13.11"}
        )
        self.assertEqual(status, 201, pay)
        self.assertEqual(pay["paid_total"], "33.11")
        self.assertEqual(pay["outstanding"], "0.00")

        # 查询：两笔记录，按登记顺序返回，均带登记时间
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(ov["payment_count"], 2)
        self.assertEqual([p["amount"] for p in ov["payments"]], ["20.00", "13.11"])
        for p in ov["payments"]:
            self.assertIn("created_at", p)
        self.assertEqual(ov["paid_total"], "33.11")
        self.assertEqual(ov["outstanding"], "0.00")

        # 已收满，再收 0.01 -> 422，且不留下记录
        status, body = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "0.01"}
        )
        self.assertEqual(status, 422)
        self.assertIn("detail", body)
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(ov["payment_count"], 2)
        self.assertEqual(ov["paid_total"], "33.11")

    def test_09_payment_validation(self) -> None:
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "10.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        path = f"/api/v1/invoices/{invoice_id}/payments"

        # 超额（累计 > 含税总额 10.00）
        status, _ = self._request("POST", path, {"amount": "10.01"})
        self.assertEqual(status, 422)
        # 零 / 负数
        status, _ = self._request("POST", path, {"amount": "0"})
        self.assertEqual(status, 422)
        status, _ = self._request("POST", path, {"amount": "-5.00"})
        self.assertEqual(status, 422)
        # 非十进制字符串 / 非字符串
        status, _ = self._request("POST", path, {"amount": "abc"})
        self.assertEqual(status, 422)
        status, _ = self._request("POST", path, {"amount": 1.5})
        self.assertEqual(status, 422)
        # 小于币种最小单位
        status, _ = self._request("POST", path, {"amount": "0.001"})
        self.assertEqual(status, 422)

        # 以上全部失败，不得留下任何记录
        status, ov = self._request("GET", path)
        self.assertEqual(ov["payment_count"], 0)
        self.assertEqual(ov["paid_total"], "0.00")
        self.assertEqual(ov["outstanding"], "10.00")

        # 不存在的发票：登记 404、查询 404
        status, _ = self._request(
            "POST", "/api/v1/invoices/999999/payments", {"amount": "1.00"}
        )
        self.assertEqual(status, 404)
        status, _ = self._request("GET", "/api/v1/invoices/999999/payments")
        self.assertEqual(status, 404)

    def test_10_concurrent_payments_no_overpay(self) -> None:
        # total_gross = 100.00，10 个并发请求各付 20.00：
        # 同事务校验+写入，恰好 5 笔成功、5 笔 422，最终不超收
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "100.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]

        codes: list[int] = []
        lock = threading.Lock()

        def pay() -> None:
            code, _ = self._request(
                "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "20.00"}
            )
            with lock:
                codes.append(code)

        threads = [threading.Thread(target=pay) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(codes), [201] * 5 + [422] * 5)
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(ov["payment_count"], 5)
        self.assertEqual(ov["paid_total"], "100.00")
        self.assertEqual(ov["outstanding"], "0.00")


if __name__ == "__main__":
    unittest.main()
