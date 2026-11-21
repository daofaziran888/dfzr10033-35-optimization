"""统一资源接口：CRUD + 跨模块管线 API。"""
from __future__ import annotations

import csv
import io
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for
from app.store import store

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


# -- CRUD --

@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str, request: Request,
    keyword: str | None = Query(default=None), status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1), size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {k: v.strip() for k, v in request.query_params.items()
               if k in service.LIST_FIELDS and v.strip()}
    items, total = service.list_entries(filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str, request: Request,
    keyword: str | None = Query(default=None), status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {k: v.strip() for k, v in request.query_params.items()
               if k in service.LIST_FIELDS and v.strip()}
    items, _ = service.list_entries(filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(f, "") for f in service.LIST_FIELDS), item.get("status", "")]
        writer.writerow(["'" + str(v) if str(v or "").startswith(("=", "+", "-", "@")) else v for v in values])
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{module}.csv"'})


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)


# -- 8 条跨模块管道 API --

from app.services.source_registry import SourceRegistryService
from app.services.query_federation import QueryFederationService
from app.services.olap_cube import OlapCubeService
from app.services.data_exporter import DataExporterService
from app.services.result_cache import ResultCacheService
from app.services.aggregate_cache import AggregateCacheService
from app.services.data_lake_read import DataLakeReadService
from app.services.query_rewrite import QueryRewriteService
from app.services.query_audit import QueryAuditService
from app.services.cost_govern import CostGovernService
from app.services.data_contract import DataContractService
from app.services.semantic_model import SemanticModelService
from app.services.settlement import service as settlement_svc

source_svc = SourceRegistryService()
semantic_svc = SemanticModelService()
federation_svc = QueryFederationService()
cube_svc = OlapCubeService()
exporter_svc = DataExporterService()
result_cache_svc = ResultCacheService()
agg_cache_svc = AggregateCacheService()
lake_svc = DataLakeReadService()
rewrite_svc = QueryRewriteService()
audit_svc = QueryAuditService()
cost_svc = CostGovernService()
contract_svc = DataContractService()


@router.post("/pipelines/source-to-federated")
def pipeline_source_to_federated(
    source_no: str = Query(...), source_type: str = Query(default="mysql"),
    source_addr: str = Query(default="db://demo/mydb"),
) -> dict[str, Any]:
    entry, msg = source_svc.register_source({
        "源编号": source_no, "源类型": source_type, "连接地址": source_addr,
        "驱动配置": f"{source_type}_driver", "数据库名": "demo_db", "认证方式": "token"})
    if entry is None:
        raise HTTPException(status_code=400, detail=msg)
    sid = entry["id"]
    schema_r = source_svc.detect_schema_changes(sid)
    plan = federation_svc.build_federation_plan([source_no])
    fe, missing = federation_svc.create_entry({
        "计划编号": f"QUER-P{sid:04d}", "参与来源": source_no,
        "关联键": plan["关联键"], "下推策略": plan["下推策略"], "数据量估算": plan["数据量估算"],
        "并行度": plan["并行度"], "超时上限": plan["超时上限"], "结果合并": plan["结果合并"]})
    if fe is None:
        raise HTTPException(status_code=400, detail=f"创建计划失败: {missing}")
    exec_r = federation_svc.execute_federation_query(fe["id"])
    return {"ok": True, "pipeline": "source_to_federated", "source": {"id": sid, "no": source_no},
            "schema": schema_r, "federation_plan": plan, "execution": exec_r}


@router.post("/pipelines/cube-build-cache")
def pipeline_cube_build_cache(
    cube_no: str = Query(...), cube_name: str = Query(...),
    dims: str = Query(default="region,category"), measures: str = Query(default="revenue,orders"),
    agg_type: str = Query(default="SUM"),
) -> dict[str, Any]:
    entry, missing = cube_svc.create_entry({
        "立方编号": cube_no, "立方名称": cube_name, "维度列表": dims, "度量列表": measures,
        "聚合类型": agg_type, "分区策略": "dt", "刷新周期": "3600", "数据行数": "0"})
    if entry is None:
        raise HTTPException(status_code=400, detail=f"创建立方失败: {missing}")
    cid = entry["id"]
    return {"ok": True, "pipeline": "cube_build_cache", "cube_id": cid,
            "build": cube_svc.build_cube(cid),
            "prewarm": agg_cache_svc.prewarm_cube_cache(cid),
            "consistency": agg_cache_svc.check_cache_consistency(cid)}


@router.post("/pipelines/rewrite-estimate-execute")
def pipeline_rewrite_estimate_execute(
    query: str = Query(default="SELECT * FROM source_registry"),
    model_ids: str = Query(default=""),
) -> dict[str, Any]:
    mids = [int(m) for m in model_ids.split(",") if m.strip()] if model_ids else None
    rr = rewrite_svc.rewrite_query(query, mids)
    cr = federation_svc.estimate_query_cost(["source_registry"], "SELECT")
    return {"ok": True, "pipeline": "rewrite_estimate_execute", "rewrite": rr, "cost_estimate": cr,
            "execution_summary": {"original": query, "rewritten": rr["rewritten"],
                                  "rules_applied": rr["rules_applied"],
                                  "estimated_savings": rr["estimated_savings_rows"]}}


@router.post("/pipelines/query-audit-profile")
def pipeline_query_audit_profile(
    query_stmt: str = Query(default="FEDERATED_QUERY plan_id=1"),
) -> dict[str, Any]:
    audits = store.rows("query_audit")
    slow = audit_svc.analyze_slow_queries(threshold_ms=1000)
    profile = audit_svc.get_query_profile(audits[-1]["id"]) if audits else None
    report = audit_svc.generate_performance_report(time.strftime("%Y-%m-%d"), time.strftime("%Y-%m-%d"))
    return {"ok": True, "pipeline": "query_audit_profile", "slow_analysis": slow,
            "profile": profile, "report": report}


@router.post("/pipelines/schema-drift-detect")
def pipeline_schema_drift_detect(source_id: int = Query(...)) -> dict[str, Any]:
    schema_r = source_svc.detect_schema_changes(source_id)
    conflicts = semantic_svc.detect_schema_conflicts(source_id)
    affected = [{"contract_id": c["id"], "消费者": c.get("消费者"),
                 "violations": contract_svc.verify_contract_compliance(c["id"]).get("violations", [])}
                for c in store.rows("data_contract")
                if c.get("status") == "已承诺" and contract_svc.verify_contract_compliance(c["id"]).get("violations")]
    return {"ok": True, "pipeline": "schema_drift_detect", "source_id": source_id,
            "schema_changes": schema_r, "model_conflicts": conflicts, "affected_contracts": affected}


@router.post("/pipelines/export-to-mart")
def pipeline_export_to_mart(
    export_no: str = Query(...), fmt: str = Query(default="csv"), mart_id: int = Query(default=0),
) -> dict[str, Any]:
    entry, missing = exporter_svc.create_entry({
        "任务编号": export_no, "数据来源": "result_cache,result_mart", "筛选条件": "all",
        "输出格式": fmt, "字段列表": "*", "排序规则": "id", "导出上限": "10000",
        "接收地址": "local://exports"})
    if entry is None:
        raise HTTPException(status_code=400, detail=f"创建导出任务失败: {missing}")
    return {"ok": True, "pipeline": "export_to_mart",
            "export": exporter_svc.export_query_results(entry["id"]),
            "mart": exporter_svc.batch_export_to_mart(mart_id) if mart_id > 0 else None}


@router.post("/pipelines/lake-scan-register")
def pipeline_lake_scan_register(path_id: int = Query(...)) -> dict[str, Any]:
    return {"ok": True, "pipeline": "lake_scan_register",
            "scan": lake_svc.scan_and_register(path_id),
            "drift": lake_svc.detect_schema_drift(path_id),
            "cost": lake_svc.estimate_partition_scan_cost(path_id, ("2026-11-01", "2026-11-17"))}


@router.post("/pipelines/cost-aggregate-alert")
def pipeline_cost_aggregate_alert() -> dict[str, Any]:
    return {"ok": True, "pipeline": "cost_aggregate_alert",
            "cost_aggregation": cost_svc.aggregate_module_costs(),
            "quota_check": cost_svc.check_monthly_quota(),
            "cache_report": result_cache_svc.cache_hit_rate_report()}
