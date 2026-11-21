"""查询审计领域服务：查询日志、慢查询分析、执行性能追踪与跨模块审计串联。"""
from __future__ import annotations

import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class QueryAuditService(BaseModuleService):
    MODULE = "query_audit"
    ENTITY = "审计记录"
    OPERATION_TERM = "查询"
    LIST_FIELDS = [
        "记录编号", "查询语句", "执行耗时", "扫描行数", "返回行数",
        "数据来源", "操作用户", "查询时间", "业务月", "入账月", "结算状态",
    ]
    REQUIRED_FIELDS = ["记录编号", "查询语句"]
    STATUS_ORDER = ["正常", "慢查询", "已终止", "已审计"]
    ACTION_RULES = {"标记慢查询": "慢查询", "终止查询": "已终止", "标记审计": "已审计"}
    NEGATIVE_ACTIONS = ["标记慢查询", "终止查询"]

    def analyze_slow_queries(self, threshold_ms: int = 5000) -> dict[str, Any]:
        """分析慢查询：扫描 query_audit，生成缓存预热建议，记录成本浪费。"""
        records = store.rows(self.MODULE)
        slow = []
        by_source: dict[str, int] = {}

        for record in records:
            elapsed_str = record.get("执行耗时", "0ms")
            try:
                elapsed = float(elapsed_str.replace("ms", ""))
            except (ValueError, AttributeError):
                elapsed = 0

            if elapsed > threshold_ms:
                slow.append({
                    "id": record["id"],
                    "编号": record.get("记录编号"),
                    "语句摘要": (record.get("查询语句", ""))[:60],
                    "耗时": elapsed_str,
                    "来源": record.get("数据来源"),
                })
                source = record.get("数据来源", "unknown")
                by_source[source] = by_source.get(source, 0) + 1

        # 生成预热建议
        warmup_recommendations = []
        for source, count in by_source.items():
            if count >= 2:
                warmup_recommendations.append({
                    "source": source,
                    "slow_count": count,
                    "action": "PREWARM_CACHE",
                })

        # 写入成本治理记录
        if slow:
            _record_slow_query_cost(len(slow), threshold_ms)

        return {
            "ok": True,
            "threshold_ms": threshold_ms,
            "total_records": len(records),
            "slow_count": len(slow),
            "by_source": by_source,
            "warmup_recommendations": warmup_recommendations,
            "top_slow_queries": slow[:10],
        }

    def get_query_profile(self, record_id: int) -> dict[str, Any]:
        """获取查询画像：关联 result_cache 和 cost_govern。"""
        record = store.find(self.MODULE, record_id)
        if record is None:
            return {"ok": False, "message": "审计记录不存在"}

        # 检查是否有缓存结果
        cache_hit = None
        caches = store.rows("result_cache")
        for c in caches:
            if record.get("记录编号", "") in str(c.get("查询摘要", "")):
                cache_hit = {
                    "cache_id": c["id"],
                    "status": c.get("status"),
                    "hits": c.get("缓存命中"),
                }
                break

        # 检查成本配额
        cost_records = []
        cost_rows = store.rows("cost_govern")
        for c in cost_rows:
            if record.get("数据来源", "") in str(c.get("适用角色", "")):
                cost_records.append({
                    "cost_id": c["id"],
                    "strategy": c.get("策略编号"),
                    "quota": c.get("查询上限"),
                    "status": c.get("status"),
                })

        # 更新审计记录
        record["status"] = "已审计"
        record["pending"] = False

        return {
            "ok": True,
            "record_id": record_id,
            "query": (record.get("查询语句", ""))[:100],
            "elapsed": record.get("执行耗时"),
            "scanned": record.get("扫描行数"),
            "returned": record.get("返回行数"),
            "cache_hit": cache_hit,
            "cost_records": cost_records,
        }

    def generate_performance_report(self, start_time: str, end_time: str) -> dict[str, Any]:
        """生成时段性能报告：汇总 query_audit、result_cache、cost_govern，写入 result_mart。"""
        records = store.rows(self.MODULE)

        total = len(records)
        slow = sum(1 for r in records if r.get("status") == "慢查询")
        terminated = sum(1 for r in records if r.get("status") == "已终止")

        # 汇总缓存
        caches = store.rows("result_cache")
        cache_valid = sum(1 for c in caches if c.get("status") == "有效")
        cache_total = len(caches)

        # 汇总成本
        costs = store.rows("cost_govern")
        cost_active = sum(1 for c in costs if c.get("status") == "已生效")
        cost_alert = sum(1 for c in costs if c.get("status") == "已告警")

        report = {
            "period": f"{start_time} ~ {end_time}",
            "total_queries": total,
            "slow_queries": slow,
            "terminated_queries": terminated,
            "cache_hit_rate": round(cache_valid / max(cache_total, 1), 2),
            "active_cost_policies": cost_active,
            "alert_cost_policies": cost_alert,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        # 写入 result_mart
        _write_report_to_mart(report)

        return {"ok": True, "report": report}



    # ---------------------------------------------------------- 月结对账

    def reconcile_against_settlement(self, business_month: str) -> dict[str, Any]:
        """用月度扫描对账批次核对各次执行的计入范围。

        查询审计是执行事实源，结算明细是入账事实源；这里按 ``审计记录id``
        一一比对。核对结果回写到结算明细的 ``核对状态`` 与审计记录，
        未入账记录重新送入暂存区，等待补报批次。
        """
        from app.services.settlement import (
            DETAIL_STATUS_INCLUDED,
            DETAIL_MODULE,
            service as settlement,
        )

        result = settlement.record_reconcile(business_month, operator="query_audit")
        missing_ids = {item["audit_id"] for item in result["missing"]}
        duplicated_ids = {item["audit_id"] for item in result["duplicated"]}

        details = [d for d in store.rows(DETAIL_MODULE) if d.get("业务月") == business_month]
        for detail in details:
            audit_id = int(detail.get("审计记录id", 0))
            if detail.get("status") != DETAIL_STATUS_INCLUDED:
                detail["核对状态"] = "已冲销"
            elif audit_id in duplicated_ids:
                detail["核对状态"] = "重复入账"
                detail["abnormal"] = True
                detail["pending"] = True
            elif audit_id in missing_ids:
                detail["核对状态"] = "未入账"
                detail["pending"] = True
            else:
                detail["核对状态"] = "已核对"
                detail["pending"] = False

        covered_ids = {
            int(d.get("审计记录id", 0)) for d in details
            if d.get("核对状态") == "已核对"
        }
        for audit in store.rows(self.MODULE):
            audit_id = int(audit.get("id", 0))
            if audit_id in missing_ids:
                audit["结算核对"] = "未入账"
                audit["核对月份"] = business_month
            elif audit_id in covered_ids:
                audit["结算核对"] = "已核对"
                audit["核对月份"] = business_month

        prepared = settlement.recheck_unbooked(business_month)
        if result.get("record"):
            result["record"]["未入账清单"] = ", ".join(
                str(item.get("记录编号")) for item in prepared["items"][:10])
        return {
            "ok": True,
            "business_month": business_month,
            "reconcile": result,
            "record": result.get("record"),
            "details_checked": len(details),
            "unbooked_prepared": prepared["unbooked"],
            "staged": prepared["staged"],
            "unbooked": prepared["items"][:50],
        }

    def prepare_unbooked(self, business_month: str) -> dict[str, Any]:
        """为未入账的审计执行重新准备数据，写入待补记暂存区。"""
        from app.services.settlement import service as settlement

        prepared = settlement.recheck_unbooked(business_month)
        status = "有差异" if prepared["unbooked"] else "已核对"
        for audit in store.rows(self.MODULE):
            audit_id = int(audit.get("id", 0))
            if any(item["audit_id"] == audit_id for item in prepared["items"]):
                audit["结算核对"] = "待补记"
                audit["核对月份"] = business_month
            else:
                audit.setdefault("结算核对", status)
        return {
            "ok": True,
            "business_month": business_month,
            "unbooked": prepared["unbooked"],
            "staged": prepared["staged"],
            "items": prepared["items"][:50],
            "message": (
                f"{prepared['unbooked']} 条执行未入账，已重新准备暂存数据"
                if prepared["unbooked"] else "该业务月执行均已入账"
            ),
        }


def _record_slow_query_cost(slow_count: int, threshold_ms: int) -> None:
    rows = store.rows("cost_govern")
    cost_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    cost = {
        "id": cost_id,
        "策略编号": f"COST-SQ{uuid.uuid4().hex[:8]}",
        "适用角色": "slow_query_penalty",
        "查询上限": str(slow_count),
        "并发上限": str(threshold_ms),
        "月度配额": str(slow_count * 30),
        "告警阈值": "5",
        "限流动作": "throttle" if slow_count > 10 else "monitor",
        "生效时段": time.strftime("%Y-%m-%d"),
        "status": "已告警" if slow_count > 5 else "已生效",
        "pending": slow_count > 5,
        "abnormal": slow_count > 20,
    }
    rows.append(cost)


def _write_report_to_mart(report: dict[str, Any]) -> None:
    rows = store.rows("result_mart")
    mart_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    mart = {
        "id": mart_id,
        "集市编号": f"RESU-RP{mart_id:04d}",
        "集市名称": f"性能报告_{report['period']}",
        "数据来源": "query_audit",
        "主题域": "性能分析",
        "行数统计": str(report["total_queries"]),
        "刷新周期": "daily",
        "下游消费": "monitoring",
        "最后刷新": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "可用",
        "pending": False,
        "abnormal": False,
    }
    rows.append(mart)