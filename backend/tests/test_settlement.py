"""月度扫描结算与审计对账集成测试。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.query_audit import QueryAuditService
from app.services.settlement import (
    BATCH_MODULE,
    DETAIL_MODULE,
    REPORT_MODULE,
    service as settlement,
)
from app.store import store

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_store():
    """每个用例从种子数据重放，避免月结状态互相污染。"""
    store.reset()
    yield
    store.reset()


def _details_month(month: str, entry: str | None = None) -> list[dict]:
    return [
        d for d in store.rows(DETAIL_MODULE)
        if d.get("业务月") == month and (entry is None or d.get("入账月") == entry)
    ]


class TestIncrementalSettlement:
    def test_settle_reads_only_new_audits(self):
        """第二次结算只消费游标之后的审计，游标不倒退。"""
        first = settlement.settle_month("2026-09")
        assert first["ok"] and first["new_details"] == 3
        cursor = int(first["batch"]["审计游标"])
        assert cursor > 0

        second = settlement.run_batch(first["batch"]["id"])
        assert second["ok"]
        assert second["new_details"] == 0
        assert int(second["batch"]["审计游标"]) >= cursor

        settlement.register_audit({
            "记录编号": "AUDIT-0904", "扫描行数": "1200", "查询时间": "2026-09-04 08:00:00",
        })
        third = settlement.run_batch(first["batch"]["id"])
        assert third["new_details"] == 1

    def test_batch_records_business_and_entry_month(self):
        result = settlement.settle_month("2026-09")
        batch = result["batch"]
        assert batch["业务月"] == "2026-09"
        assert batch["入账月"] == "2026-09"
        assert batch["口径版本"] == "CAL-2026.09-v1"
        assert all(d["业务月"] == "2026-09" for d in _details_month("2026-09"))

    def test_closed_batch_is_not_recomputed(self):
        result = settlement.settle_month("2026-09")
        assert settlement.close_batch(result["batch"]["id"])["ok"]
        retry = settlement.run_batch(result["batch"]["id"])
        assert retry["ok"] is False
        assert "已封账" in retry["message"]


class TestTwoMonthSettlement:
    def test_two_months_then_late_cross_month_audit(self):
        september = settlement.settle_month("2026-09")
        october = settlement.settle_month("2026-10")
        assert september["new_details"] == 3
        assert october["new_details"] == 3
        for batch in store.rows(BATCH_MODULE):
            assert settlement.close_batch(batch["id"])["ok"]

        # 两个月的额度用量互不串月。
        sept_quota = settlement.quota_sheet("2026-09")
        oct_quota = settlement.quota_sheet("2026-10")
        assert sept_quota["total_used"] == 28000
        assert oct_quota["total_used"] == 29000

        late = settlement.backfill_audit(audit={
            "记录编号": "AUDIT-LATE-0928",
            "查询语句": "SELECT * FROM federated(sales) WHERE dt='2026-09-28'",
            "扫描行数": "5000",
            "查询时间": "2026-09-28 23:40:00",
            "数据来源": "query_federation",
        })
        assert late["ok"]
        assert late["business_month"] == "2026-09"
        assert late["entry_month"] == "2026-11"
        detail = late["detail"]
        assert detail["是否迟到"] is True
        assert detail["入账月"] == "2026-11"
        assert detail["业务月"] == "2026-09"

        # 已封账的九月批次保持原样，补记落到新的入账批次。
        september_batch = store.find(BATCH_MODULE, september["batch"]["id"])
        assert september_batch["status"] == "已封账"
        assert september_batch["扫描行数"] == "28000"
        assert settlement.quota_sheet("2026-09")["total_used"] == 28000
        assert settlement.quota_sheet("2026-10")["total_used"] == 29000
        assert settlement.quota_sheet("2026-11")["total_used"] == 5000

        # 额度清单能从补记记录关联回原审计执行。
        november = settlement.quota_sheet("2026-11")
        linked = next(
            line["details"][0] for line in november["lines"] if line["补记笔数"] == 1
        )
        assert linked["审计编号"] == "AUDIT-LATE-0928"
        assert linked["原月结批次"] == september["batch"]["批次编号"]

        # 旧报告保留原快照，但被标记为待复查。
        report = next(r for r in store.rows(REPORT_MODULE) if r["业务月"] == "2026-09")
        assert report["口径版本"] == "CAL-2026.09-v1"
        assert report["扫描行数"] == "28000"
        assert report["复查状态"] == "待复查"
        reviewed = settlement.review_report(report["id"])
        assert reviewed["needs_review"] is True
        assert reviewed["late_details"][0]["审计编号"] == "AUDIT-LATE-0928"

    def test_reconcile_covers_every_execution_once(self):
        settlement.settle_month("2026-09")
        settlement.settle_month("2026-10")
        for batch in store.rows(BATCH_MODULE):
            settlement.close_batch(batch["id"])
        settlement.backfill_audit(audit={
            "记录编号": "AUDIT-LATE-0928", "扫描行数": "5000",
            "查询时间": "2026-09-28 23:40:00",
        })

        september = settlement.reconcile_month("2026-09")
        assert september["审计总数"] == 4
        assert september["已计入"] == 4
        assert september["未入账"] == 0
        assert september["重复入账"] == 0
        assert september["迟到补记"] == 1
        assert september["计入扫描量"] == 33000

        october = settlement.reconcile_month("2026-10")
        assert october["已计入"] == 3 and october["未入账"] == 0

    def test_backfill_is_idempotent_per_audit(self):
        settlement.settle_month("2026-09")
        batch = settlement.settle_month("2026-10")["batch"]
        settlement.close_batch(batch["id"])
        audit_id = settlement.register_audit({
            "记录编号": "AUDIT-LATE-0928", "扫描行数": "5000",
            "查询时间": "2026-09-28 23:40:00",
        })
        first = settlement.backfill_audit(audit=None)
        assert first["audit_id"] == audit_id
        before = len(store.rows(DETAIL_MODULE))
        # 同一审计被重复触发时直接返回既有明细，不再写第二条。
        settlement._stage_late(store.find("query_audit", audit_id), "2026-09")
        settlement.run_batch(first["batch"]["id"])
        assert len(store.rows(DETAIL_MODULE)) == before
        assert len([d for d in store.rows(DETAIL_MODULE) if d["审计记录id"] == audit_id]) == 1


class TestConcurrency:
    def test_cross_month_settlement_does_not_duplicate(self):
        errors: list[str] = []

        def work(month: str) -> None:
            try:
                settlement.settle_month(month)
            except Exception as exc:  # pragma: no cover - 失败时给出原因
                errors.append(repr(exc))

        threads = [
            threading.Thread(target=work, args=(month,))
            for month in ("2026-09", "2026-10") for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors
        details = store.rows(DETAIL_MODULE)
        assert len(details) == len({d["审计记录id"] for d in details}) == 6
        batches = store.rows(BATCH_MODULE)
        assert len(batches) == 2
        assert {b["业务月"]: b["执行笔数"] for b in batches} == {"2026-09": "3", "2026-10": "3"}

    def test_duplicate_execution_written_once(self):
        settlement.settle_month("2026-09")
        audit_id = settlement.register_audit({
            "记录编号": "AUDIT-DUP", "扫描行数": "800", "查询时间": "2026-09-12 10:00:00",
        })
        batch = next(b for b in store.rows(BATCH_MODULE) if b["业务月"] == "2026-09")
        threads = [
            threading.Thread(target=settlement.run_batch, args=(batch["id"],))
            for _ in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        hits = [d for d in store.rows(DETAIL_MODULE) if d["审计记录id"] == audit_id]
        assert len(hits) == 1


class TestAuditReconcileApi:
    def test_reconcile_writes_back_and_prepares_unbooked(self):
        settlement.settle_month("2026-09")
        settlement.settle_month("2026-10")
        for batch in store.rows(BATCH_MODULE):
            settlement.close_batch(batch["id"])
        settlement.register_audit({
            "记录编号": "AUDIT-UNBOOKED", "扫描行数": "900",
            "查询时间": "2026-09-20 12:00:00",
        })

        result = QueryAuditService().reconcile_against_settlement("2026-09")
        assert result["ok"]
        assert result["reconcile"]["未入账"] == 1
        assert result["unbooked_prepared"] == 1
        assert any(item["审计编号"] == "AUDIT-UNBOOKED" for item in store.rows("settlement_staging"))

        audit = next(a for a in store.rows("query_audit") if a["记录编号"] == "AUDIT-UNBOOKED")
        assert audit["结算核对"] == "未入账"
        prepared = QueryAuditService().prepare_unbooked("2026-09")
        assert prepared["unbooked"] == 1

    def test_api_monthly_settlement_and_quota(self):
        response = client.post("/api/pipelines/monthly-settlement",
                               params={"business_month": "2026-09"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["settlement"]["batch"]["业务月"] == "2026-09"
        assert payload["monthly_quota"]["total_used"] == 28000

        quota = client.get("/api/settlement/quota-sheet", params={"entry_month": "2026-09"})
        assert quota.status_code == 200
        assert {line["额度键"] for line in quota.json()["lines"]} == {
            "query_federation@2026-09", "data_lake_read@2026-09",
        }

    def test_api_backfill_and_review(self):
        for month in ("2026-09", "2026-10"):
            assert client.post("/api/pipelines/monthly-settlement",
                               params={"business_month": month}).status_code == 200
        late = client.post("/api/pipelines/audit-backfill", params={
            "record_no": "AUDIT-LATE-0928",
            "scanned_rows": 5000,
            "executed_at": "2026-09-28 23:40:00",
        })
        assert late.status_code == 200
        assert late.json()["entry_month"] == "2026-11"

        reports = client.get("/api/settlement_report").json()["items"]
        report = next(r for r in reports if r["业务月"] == "2026-09")
        viewed = client.get(f"/api/settlement/reports/{report['id']}/review")
        assert viewed.status_code == 200
        assert viewed.json()["late_details"][0]["审计编号"] == "AUDIT-LATE-0928"

        reconcile = client.post("/api/pipelines/audit-settlement-reconcile",
                                params={"business_month": "2026-09"})
        assert reconcile.status_code == 200
        assert reconcile.json()["reconcile"]["已计入"] == 4


class TestMonthlyQuota:
    def test_quota_check_uses_settlement_detail(self):
        from app.services.cost_govern import CostGovernService

        settlement.settle_month("2026-09")
        result = CostGovernService().check_monthly_quota("2026-09")
        assert result["source"] == "settlement_detail"
        # 九月联邦查询 21000/25000=84% 触发告警，湖仓 70% 不触发。
        roles = {item["角色"] for item in result["overused"]}
        assert roles == {"query_federation"}

    def test_caliber_version_does_not_drift(self):
        settlement.settle_month("2026-09")
        september = settlement.settle_month("2026-09")["batch"]
        october = settlement.settle_month("2026-10")["batch"]
        assert september["口径版本"] == october["口径版本"] == "CAL-2026.09-v1"
        november = settlement.settle_month("2026-11")["batch"]
        assert november["口径版本"] == "CAL-2026.11-v2"
