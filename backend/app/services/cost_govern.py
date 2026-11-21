"""成本治理领域服务：查询配额管理、资源消耗追踪与跨模块成本归因。"""
from __future__ import annotations

import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class CostGovernService(BaseModuleService):
    MODULE = "cost_govern"
    ENTITY = "配额策略"
    OPERATION_TERM = "查询"
    LIST_FIELDS = [
        "策略编号", "适用角色", "查询上限", "并发上限", "月度配额",
        "已用扫描量", "用量占比", "结算口径", "告警阈值", "限流动作", "生效时段",
    ]
    REQUIRED_FIELDS = ["策略编号", "适用角色"]
    STATUS_ORDER = ["草稿", "已生效", "已告警", "已停用"]
    ACTION_RULES = {"启效策略": "已生效", "触发限流": "已告警", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["触发限流", "停用策略"]

    def aggregate_module_costs(self) -> dict[str, Any]:
        """汇总各模块成本：读取 query_audit、result_cache、query_federation，写入 cost_govern。"""
        # 统计各模块查询审计
        audits = store.rows("query_audit")
        module_costs: dict[str, dict[str, float]] = {}
        for a in audits:
            source = a.get("数据来源", "unknown")
            elapsed_str = a.get("执行耗时", "0ms")
            try:
                elapsed = float(elapsed_str.replace("ms", ""))
            except (ValueError, AttributeError):
                elapsed = 0
            scanned_str = a.get("扫描行数", "0")
            try:
                scanned = int(scanned_str)
            except (ValueError, TypeError):
                scanned = 0

            if source not in module_costs:
                module_costs[source] = {"total_time_ms": 0, "total_scanned": 0, "query_count": 0}
            module_costs[source]["total_time_ms"] += elapsed
            module_costs[source]["total_scanned"] += scanned
            module_costs[source]["query_count"] += 1

        # 生成汇总策略
        generated_policies = []
        for module, costs in module_costs.items():
            if costs["total_scanned"] > 1000:
                rows = store.rows(self.MODULE)
                cost_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
                alert_level = "已告警" if costs["total_scanned"] > 100000 else "已生效"
                cost = {
                    "id": cost_id,
                    "策略编号": f"COST-MC{uuid.uuid4().hex[:8]}",
                    "适用角色": f"module_{module}",
                    "查询上限": str(costs["total_scanned"]),
                    "并发上限": str(costs["query_count"]),
                    "月度配额": str(costs["total_scanned"] * 30),
                    "告警阈值": "100000",
                    "限流动作": "monitor",
                    "生效时段": time.strftime("%Y-%m-%d"),
                    "status": alert_level,
                    "pending": alert_level == "已告警",
                    "abnormal": costs["total_scanned"] > 500000,
                }
                rows.append(cost)
                generated_policies.append({
                    "policy_id": cost_id,
                    "module": module,
                    "status": alert_level,
                })

        return {
            "ok": True,
            "modules_analyzed": len(module_costs),
            "total_scanned_rows": sum(c["total_scanned"] for c in module_costs.values()),
            "total_time_ms": sum(c["total_time_ms"] for c in module_costs.values()),
            "policies_generated": len(generated_policies),
            "module_breakdown": {k: v for k, v in sorted(module_costs.items())},
        }

    def check_monthly_quota(self, month: str | None = None) -> dict[str, Any]:
        """检查月度配额：直接读取结算明细回写的 ``已用扫描量``，不再重扫审计。

        月度用量由 ``settlement`` 批次增量归集后写回策略行；这里只做阈值判断，
        因此查询成本不再随审计表增长而线性上升。
        """
        from app.services.settlement import service as settlement

        month = month or time.strftime("%Y-%m")
        policies = [p for p in store.rows(self.MODULE) if p.get("生效时段") == month]
        sheet = settlement.quota_sheet(month)
        usage_by_key = {line["额度键"]: line for line in sheet["lines"]}
        overused: list[dict[str, Any]] = []

        for policy in policies:
            key = f"{policy.get('适用角色', '')}@{policy.get('生效时段', '')}"
            line = usage_by_key.get(key)
            usage = line["已用扫描量"] if line else int(policy.get("已用扫描量") or 0)
            try:
                quota = int(policy.get("月度配额", "0"))
            except (ValueError, TypeError):
                continue
            if quota <= 0:
                continue
            ratio = usage / quota
            policy["已用扫描量"] = str(usage)
            policy["用量占比"] = f"{ratio:.1%}"
            if ratio > 0.8:
                policy["status"] = "已告警"
                policy["pending"] = True
                policy["abnormal"] = ratio > 1.0
                overused.append({
                    "policy_id": policy["id"],
                    "角色": policy.get("适用角色"),
                    "quota": quota,
                    "usage": usage,
                    "ratio": round(ratio, 2),
                })
                # 触发 freshness_mon 通知
                _notify_quota_alert(policy["id"], policy.get("适用角色", ""), ratio)

        return {
            "ok": True,
            "month": month,
            "source": "settlement_detail",
            "total_policies": len(policies),
            "overused_count": len(overused),
            "overused": overused[:10],
        }


def _notify_quota_alert(policy_id: int, role: str, ratio: float) -> None:
    rows = store.rows("freshness_mon")
    mon_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    mon = {
        "id": mon_id,
        "规则编号": f"FRES-Q{policy_id:04d}",
        "目标数据集": f"cost_govern:{policy_id}",
        "SLA秒数": "86400",
        "检测频率": "monthly",
        "延迟阈值": f"quota_overuse_{ratio:.0%}",
        "告警级别": "CRITICAL" if ratio > 1.0 else "WARNING",
        "触发次数": "1",
        "最后检查": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "已告警",
        "pending": True,
        "abnormal": ratio > 1.0,
    }
    rows.append(mon)