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

    def test_08_payment_flow(self) -> None:
        # 含税总额 113.00 USD（100 净额 × 13%）
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "100.00", "tax_rate": "0.13"}]},
        )
        self.assertEqual(status, 201, body)
        invoice_id = body["id"]
        self.assertEqual(body["totals"]["total_gross"], "113.00")

        # 初始：无收款记录，余额即含税总额
        status, ledger = self._request(
            "GET", f"/api/v1/invoices/{invoice_id}/payments"
        )
        self.assertEqual(status, 200)
        self.assertEqual(ledger["currency"], "USD")
        self.assertEqual(ledger["total_gross"], "113.00")
        self.assertEqual(ledger["paid_amount"], "0.00")
        self.assertEqual(ledger["balance_due"], "113.00")
        self.assertEqual(ledger["payments"], [])

        # 登记第一笔 100
        status, body = self._request(
            "POST",
            f"/api/v1/invoices/{invoice_id}/payments",
            {"amount": "100", "note": "定金"},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["payment"]["amount"], "100.00")
        self.assertEqual(body["payment"]["registered_at"][-1], "Z")
        self.assertEqual(body["paid_amount"], "100.00")
        self.assertEqual(body["balance_due"], "13.00")

        # 结清尾款 13
        status, body = self._request(
            "POST",
            f"/api/v1/invoices/{invoice_id}/payments",
            {"amount": "13.00"},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["paid_amount"], "113.00")
        self.assertEqual(body["balance_due"], "0.00")

        # 台账两条记录，按登记顺序
        status, ledger = self._request(
            "GET", f"/api/v1/invoices/{invoice_id}/payments"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(ledger["payments"]), 2)
        self.assertEqual([p["amount"] for p in ledger["payments"]], ["100.00", "13.00"])
        self.assertEqual(ledger["balance_due"], "0.00")

    def test_09_payment_overpayment_rejected(self) -> None:
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "CNY", "lines": [{"net": "100.00", "tax_rate": "0.13"}]},
        )
        self.assertEqual(status, 201, body)
        invoice_id = body["id"]  # 含税 113.00

        # 单笔超额 -> 422，不留记录
        status, body = self._request(
            "POST",
            f"/api/v1/invoices/{invoice_id}/payments",
            {"amount": "200.00"},
        )
        self.assertEqual(status, 422, body)
        self.assertIn("detail", body)

        status, ledger = self._request(
            "GET", f"/api/v1/invoices/{invoice_id}/payments"
        )
        self.assertEqual(status, 200)
        self.assertEqual(ledger["payments"], [])
        self.assertEqual(ledger["balance_due"], "113.00")

        # 先收 100，再收 14（累计 114 > 113）-> 422，只保留第一笔
        status, _ = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "100.00"}
        )
        self.assertEqual(status, 201)
        status, body = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "14.00"}
        )
        self.assertEqual(status, 422, body)
        status, ledger = self._request(
            "GET", f"/api/v1/invoices/{invoice_id}/payments"
        )
        self.assertEqual(len(ledger["payments"]), 1)
        self.assertEqual(ledger["paid_amount"], "100.00")
        self.assertEqual(ledger["balance_due"], "13.00")

        # 恰好等于余额 -> 成功
        status, body = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "13.00"}
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["balance_due"], "0.00")

    def test_10_payment_validation_errors(self) -> None:
        status, invoice = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "100.00", "tax_rate": "0.13"}]},
        )
        self.assertEqual(status, 201)
        invoice_id = invoice["id"]

        # 金额必须大于 0
        for bad in ("0", "0.00", "-1.00"):
            status, body = self._request(
                "POST",
                f"/api/v1/invoices/{invoice_id}/payments",
                {"amount": bad},
            )
            self.assertEqual(status, 422, bad)

        # 不是十进制字符串
        status, _ = self._request(
            "POST",
            f"/api/v1/invoices/{invoice_id}/payments",
            {"amount": "abc"},
        )
        self.assertEqual(status, 422)

        # 数字类型而非字符串 -> 422
        status, _ = self._request(
            "POST",
            f"/api/v1/invoices/{invoice_id}/payments",
            {"amount": 10},
        )
        self.assertEqual(status, 422)

        # 非币种最小单位整数倍（USD 下 0.001）
        status, body = self._request(
            "POST",
            f"/api/v1/invoices/{invoice_id}/payments",
            {"amount": "10.001"},
        )
        self.assertEqual(status, 422, body)

        # 发票不存在：查询/登记均 404
        status, _ = self._request("GET", "/api/v1/invoices/999999/payments")
        self.assertEqual(status, 404)
        status, _ = self._request(
            "POST", "/api/v1/invoices/999999/payments", {"amount": "1.00"}
        )
        self.assertEqual(status, 404)

    def test_11_payment_jpy_minor_unit(self) -> None:
        # JPY 为 0 位小数币种：含税 110 日元
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "JPY", "lines": [{"net": "100", "tax_rate": "0.10"}]},
        )
        self.assertEqual(status, 201, body)
        invoice_id = body["id"]

        status, body = self._request(
            "POST",
            f"/api/v1/invoices/{invoice_id}/payments",
            {"amount": "50.5"},
        )
        self.assertEqual(status, 422)  # 不是 1 日元的整数倍

        status, body = self._request(
            "POST",
            f"/api/v1/invoices/{invoice_id}/payments",
            {"amount": "50"},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["payment"]["amount"], "50")
        self.assertEqual(body["balance_due"], "60")

    def test_12_concurrent_payments_no_overpay(self) -> None:
        # 含税总额 100.00；20 笔各 10.00 的并发登记，恰好 10 笔成功、10 笔 422
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "100.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, body)
        invoice_id = body["id"]

        results: list[int] = []
        barrier = threading.Barrier(21)
        results_lock = threading.Lock()

        def pay() -> None:
            barrier.wait()
            req = urllib.request.Request(
                f"{self.base}/api/v1/invoices/{invoice_id}/payments",
                data=json.dumps({"amount": "10.00"}).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    code = resp.status
            except urllib.error.HTTPError as exc:
                code = exc.code
            with results_lock:
                results.append(code)

        threads = [threading.Thread(target=pay) for _ in range(20)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()

        self.assertEqual(sorted(results), [201] * 10 + [422] * 10)
        status, ledger = self._request(
            "GET", f"/api/v1/invoices/{invoice_id}/payments"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(ledger["payments"]), 10)
        self.assertEqual(ledger["paid_amount"], "100.00")
        self.assertEqual(ledger["balance_due"], "0.00")


if __name__ == "__main__":
    unittest.main()
