"""月度扫描结算与审计对账 API。

单独成一个路由模块并在通用 ``/api/{module}`` 资源路由之前注册，避免
``/api/settlement/quota-sheet`` 被 ``/api/{module}/{entry_id}`` 抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.services.query_audit import QueryAuditService
from app.services.settlement import service as settlement_svc

router = APIRouter(prefix="/api", tags=["月度结算"])
audit_svc = QueryAuditService()



@router.post("/pipelines/monthly-settlement")
def pipeline_monthly_settlement(
    business_month: str = Query(..., description="业务月 YYYY-MM"),
    entry_month: str | None = Query(default=None, description="入账月，默认同业务月"),
    close: bool = Query(default=True, description="归集后是否直接封账"),
) -> dict[str, Any]:
    """月度归集流水线：增量消费审计 → 生成明细 → 回写额度 → 封账出报告。"""
    settled = settlement_svc.settle_month(business_month, entry_month)
    if not settled.get("ok"):
        raise HTTPException(status_code=400, detail=settled.get("message"))
    closed = None
    if close:
        closed = settlement_svc.close_batch(int(settled["batch"]["id"]))
    quota = settlement_svc.quota_sheet(settled["entry_month"])
    return {
        "ok": True,
        "pipeline": "monthly_settlement",
        "settlement": settled,
        "close": closed,
        "monthly_quota": quota,
    }


@router.post("/pipelines/audit-backfill")
def pipeline_audit_backfill(
    record_no: str = Query(default=""),
    query_stmt: str = Query(default="SELECT * FROM federated(late_audit)"),
    scanned_rows: int = Query(default=5000),
    executed_at: str = Query(..., description="审计执行时间，决定业务月"),
    audit_source: str = Query(default="query_federation"),
    operator: str = Query(default="audit_ingest"),
) -> dict[str, Any]:
    """迟到审计补报：业务月保持不变，入账月落到发现月的新批次。"""
    audit = {
        "记录编号": record_no or None,
        "查询语句": query_stmt,
        "扫描行数": scanned_rows,
        "返回行数": max(scanned_rows // 100, 1),
        "数据来源": audit_source,
        "操作用户": operator,
        "查询时间": executed_at,
    }
    result = settlement_svc.backfill_audit(audit=audit)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return {"ok": True, "pipeline": "audit_backfill", **result}


@router.post("/pipelines/audit-settlement-reconcile")
def pipeline_audit_settlement_reconcile(
    business_month: str = Query(..., description="业务月 YYYY-MM"),
) -> dict[str, Any]:
    """查询审计 × 月结批次对账：回写计入范围并重备未入账数据。"""
    result = audit_svc.reconcile_against_settlement(business_month)
    return {"ok": True, "pipeline": "audit_settlement_reconcile", **result}


@router.post("/settlement/batches/{batch_id}/run")
def settlement_run_batch(batch_id: int, reopen: bool = Query(default=False)) -> dict[str, Any]:
    """继续增量归集指定批次（游标之后的新审计）。"""
    result = settlement_svc.run_batch(batch_id, reopen=reopen)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.post("/settlement/batches/{batch_id}/close")
def settlement_close_batch(batch_id: int) -> dict[str, Any]:
    """封账并刷新月度额度清单与报告。"""
    result = settlement_svc.close_batch(batch_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.get("/settlement/quota-sheet")
def settlement_quota_sheet(entry_month: str = Query(..., description="入账月 YYYY-MM")) -> dict[str, Any]:
    """月度额度清单：额度、已用扫描量、补记明细与关联的原审计执行。"""
    return settlement_svc.quota_sheet(entry_month)


@router.get("/settlement/reports/{report_id}/review")
def settlement_report_review(report_id: int) -> dict[str, Any]:
    """复查旧报告：返回原快照、迟到明细与补记批次关系。"""
    result = settlement_svc.review_report(report_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message"))
    return result


@router.post("/query-audit/prepare-unbooked")
def prepare_unbooked_audits(
    business_month: str = Query(..., description="业务月 YYYY-MM"),
) -> dict[str, Any]:
    """为未入账的审计执行重新准备数据。"""
    return audit_svc.prepare_unbooked(business_month)
