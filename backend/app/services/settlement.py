"""月度扫描结算领域服务：增量归集、月结批次、迟报补记与对账。

调用链：

    query_audit ──审计游标──▶ settlement_staging ──按批次聚合──▶ settlement_detail
                                                                    │
                              cost_govern（月度额度） ◀──额度回写──────┤
                                                                    ▼
                                                        settlement_batch（月结版本）
                                                                    │
                              query_audit ◀──计入范围回写────────────┘
                                    │
                              settlement_report（可复查旧报告）

设计要点：

- **增量**：每个批次持有 ``审计游标``（已消费的最大审计 id）。结算只读取
  ``store.rows_after("query_audit", cursor)``，不再全表重扫审计。
- **批次化**：批次未封账时继续消费增量；封账后冻结明细与口径版本，历史月结
  只读不重算。迟到数据走补记批次，业务月不变、入账月记为发现月。
- **双月份**：明细同时记录 ``业务月``（审计执行时间归属）和 ``入账月``（实际
  计入的结算批次月份）。跨月并发结算时，已入账的执行靠 ``审计记录id`` 唯一键
  去重，不会被写第二次。
- **唯一归属**：``settlement_detail`` 一条执行一条明细；查询审计记录上的
  ``结算明细id`` 指回唯一明细，可核对每次执行的计入范围。
- **口径版本**：每次结算写入当期的 ``settlement_caliber`` 版本，历史报告保留
  生成时的口径，不随新版本漂移。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

BATCH_MODULE = "settlement_batch"
DETAIL_MODULE = "settlement_detail"
REPORT_MODULE = "settlement_report"
STAGING_MODULE = "settlement_staging"
RECONCILE_MODULE = "settlement_reconcile"
CALIBER_MODULE = "settlement_caliber"

BATCH_STATUS_OPEN = "结算中"
BATCH_STATUS_CLOSED = "已封账"
BATCH_STATUS_VOID = "已作废"
DETAIL_STATUS_INCLUDED = "已计入"
DETAIL_STATUS_REVERSED = "已冲销"

# 跨月并发结算共用一把锁；真实项目里会换成数据库唯一索引与行锁。
_SETTLE_LOCK = threading.RLock()


class SettlementService(BaseModuleService):
    MODULE = BATCH_MODULE
    ENTITY = "月结批次"
    OPERATION_TERM = "结算"
    LIST_FIELDS = [
        "批次编号", "业务月", "入账月", "口径版本", "审计游标",
        "执行笔数", "扫描行数", "状态说明",
    ]
    REQUIRED_FIELDS = ["批次编号", "业务月"]
    STATUS_ORDER = ["草稿", BATCH_STATUS_OPEN, BATCH_STATUS_CLOSED, BATCH_STATUS_VOID]
    ACTION_RULES = {"开始结算": BATCH_STATUS_OPEN, "封账批次": BATCH_STATUS_CLOSED,
                    "作废批次": BATCH_STATUS_VOID}
    NEGATIVE_ACTIONS = ["作废批次"]

    # ---------------------------------------------------------------- 批次

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """手工登记批次时补齐编号、入账月、口径版本与游标，避免半成品批次。"""
        business = str(values.get("业务月") or "").strip()
        entry_month = str(values.get("入账月") or "").strip() or business
        values = {**values, "业务月": business, "入账月": entry_month}
        if not values.get("批次编号"):
            values["批次编号"] = f"SETTLE-{entry_month.replace('-', '') or 'MANUAL'}"
        values.setdefault("口径版本", _caliber_for(entry_month)["版本编号"] if _is_month(entry_month) else "CAL-DEFAULT")
        values.setdefault("审计游标", str(self._cursor_for(business, entry_month)))
        values.setdefault("执行笔数", "0")
        values.setdefault("扫描行数", "0")
        entry, missing = super().create_entry(values)
        if entry is not None:
            # 手工建立的批次直接进入结算中，后续可继续增量归集。
            entry["status"] = BATCH_STATUS_OPEN
        return entry, missing

    def open_batch(self, business_month: str, entry_month: str | None = None,
                   remark: str = "") -> tuple[dict[str, Any] | None, str]:
        """建立月度结算批次；同业务月/入账月只允许一个在途批次。"""
        with _SETTLE_LOCK:
            return self._open_batch_locked(business_month, entry_month, remark)

    def _open_batch_locked(self, business_month: str, entry_month: str | None,
                           remark: str) -> tuple[dict[str, Any] | None, str]:
        if not _is_month(business_month):
            return None, f"业务月格式应为 YYYY-MM：{business_month}"
        entry_month = entry_month or business_month
        if not _is_month(entry_month):
            return None, f"入账月格式应为 YYYY-MM：{entry_month}"
        if entry_month < business_month:
            return None, "入账月不能早于业务月"

        existing = self._open_batch(business_month, entry_month)
        if existing is not None:
            return existing, f"批次 {existing['批次编号']} 已存在，继续增量结算"

        closed = self._closed_batch_for(business_month, entry_month)
        if closed is not None:
            return None, (
                f"{business_month} 的 {entry_month} 入账批次已封账；"
                "迟到审计请使用补报批次（业务月不变、入账月用发现月）"
            )

        rows = store.rows(BATCH_MODULE)
        batch_id = _next_id(rows)
        batch = {
            "id": batch_id,
            "批次编号": f"SETTLE-{entry_month.replace('-', '')}-{batch_id:04d}",
            "业务月": business_month,
            "入账月": entry_month,
            "口径版本": _caliber_for(entry_month)["版本编号"],
            "审计游标": str(self._cursor_for(business_month, entry_month)),
            "执行笔数": "0",
            "扫描行数": "0",
            "状态说明": remark or "待结算",
            "status": BATCH_STATUS_OPEN,
            "pending": True,
            "abnormal": business_month != entry_month,
        }
        rows.append(batch)
        return batch, f"已建立 {business_month} → {entry_month} 入账批次"

    def settle_month(self, business_month: str, entry_month: str | None = None,
                     remark: str = "") -> dict[str, Any]:
        """执行一次增量归集，可重复调用；封账批次默认拒绝重算。"""
        entry_month = entry_month or business_month
        batch, message = self.open_batch(business_month, entry_month, remark)
        if batch is None:
            return {"ok": False, "message": message}
        result = self.run_batch(batch["id"])
        result["message"] = f"{message}；{result.get('message', '')}"
        return result

    def run_batch(self, batch_id: int, reopen: bool = False) -> dict[str, Any]:
        """按批次游标增量消费审计，生成唯一明细并回写计入范围。"""
        with _SETTLE_LOCK:
            batch = store.find(BATCH_MODULE, batch_id)
            if batch is None:
                return {"ok": False, "message": "结算批次不存在"}
            if batch.get("status") == BATCH_STATUS_VOID:
                return {"ok": False, "message": "批次已作废，不能继续结算"}
            if batch.get("status") == BATCH_STATUS_CLOSED and not reopen:
                return {
                    "ok": False,
                    "message": (
                        f"批次 {batch['批次编号']} 已封账，历史月结不重算；"
                        "迟到数据请走补报批次"
                    ),
                    "batch": batch,
                }

            business_month = str(batch.get("业务月") or "")
            if not _is_month(business_month):
                return {"ok": False, "message": "批次缺少有效的业务月"}
            entry_month = str(batch.get("入账月") or business_month)
            if not batch.get("入账月"):
                batch["入账月"] = entry_month
            cursor = _to_int(batch.get("审计游标"))
            caliber = _caliber_for(entry_month)

            fresh = store.rows_after("query_audit", cursor)
            candidates: list[tuple[dict[str, Any], str, bool]] = []
            consumed = cursor
            skipped = 0

            for audit in fresh:
                audit_id = int(audit.get("id", 0))
                # 游标越过已读过的所有审计（含其它月份）：新审计 id 永远更大，
                # 迟到数据仍会落在游标之后，不会漏读；重复结算也不会反复扫旧行。
                consumed = max(consumed, audit_id)
                month = _audit_month(audit)
                if month is None:
                    skipped += 1
                    continue
                # 只消费本批业务月的执行；其它月份由各自批次或补报批次负责。
                if month != business_month:
                    continue
                if _detail_for_audit(audit_id) is not None:
                    skipped += 1
                    continue
                # 入账月晚于业务月即为跨月补记。
                candidates.append((
                    audit,
                    month,
                    str(batch["入账月"]) > business_month,
                ))

            # 已进入暂存区的迟到审计（游标之前就已发现）参与本次补记；
            # 同一审计可能同时出现在增量游标内，按 audit id 去重。
            seen = {int(audit.get("id", 0)) for audit, _, _ in candidates}
            for staged in _pending_staging(business_month):
                audit = store.find("query_audit", _to_int(staged.get("审计记录id")))
                if audit is None or int(audit["id"]) in seen:
                    continue
                if _detail_for_audit(int(audit["id"])) is not None:
                    continue
                seen.add(int(audit["id"]))
                candidates.append((audit, business_month, True))

            late = sum(1 for _, _, is_late in candidates if is_late)
            if candidates:
                self._write_details(batch, candidates, caliber)
            self._refresh_batch_metrics(batch)
            batch["审计游标"] = str(consumed)
            batch["pending"] = False
            batch["状态说明"] = (
                f"增量消费 {len(candidates)} 条审计"
                + (f"，其中迟到补记 {late} 条" if late else "")
            )
            if late:
                _mark_prior_report_for_review(business_month, batch)
            quota = self.refresh_monthly_quota(str(batch["入账月"]))

            return {
                "ok": True,
                "message": batch["状态说明"],
                "batch": batch,
                "business_month": business_month,
                "entry_month": batch["入账月"],
                "consumed_through": consumed,
                "new_details": len(candidates),
                "late_audits": late,
                "skipped_audits": skipped,
                "caliber": caliber["版本编号"],
                "monthly_quota": quota,
            }

    def close_batch(self, batch_id: int) -> dict[str, Any]:
        """封账：冻结明细与口径版本，刷新月度额度清单并生成可复查报告。"""
        with _SETTLE_LOCK:
            batch = store.find(BATCH_MODULE, batch_id)
            if batch is None:
                return {"ok": False, "message": "结算批次不存在"}
            if batch.get("status") == BATCH_STATUS_CLOSED:
                return {"ok": True, "message": "批次已封账", "batch": batch}
            details = _details_for_batch(batch_id)
            if not details:
                return {"ok": False, "message": "批次没有可封账的明细"}

            batch["status"] = BATCH_STATUS_CLOSED
            batch["pending"] = False
            batch["abnormal"] = any(bool(d.get("是否迟到")) for d in details)
            batch["封账时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._refresh_batch_metrics(batch)
            for detail in details:
                detail["pending"] = False

            quota = self.refresh_monthly_quota(str(batch["入账月"]))
            report = self.build_report(batch_id)
            return {
                "ok": True,
                "message": f"批次 {batch['批次编号']} 已封账",
                "batch": batch,
                "monthly_quota": quota,
                "report": report.get("report"),
            }

    # ---------------------------------------------------------------- 明细

    def _write_details(self, batch: dict[str, Any],
                       items: list[tuple[dict[str, Any], str, bool]],
                       caliber: dict[str, Any]) -> None:
        rows = store.rows(DETAIL_MODULE)
        coefficient = _to_float(caliber.get("扫描行数系数"), 1.0)
        rounding = str(caliber.get("舍入规则", "round"))
        for audit, audit_month, is_late in items:
            audit_id = int(audit.get("id", 0))
            # 唯一归属：同一执行无论被多少并发批次看到，只写一条明细。
            if _detail_for_audit(audit_id) is not None:
                continue
            scanned = _to_int(audit.get("扫描行数"))
            detail_id = _next_id(rows)
            detail = {
                "id": detail_id,
                "明细编号": f"SDET-{batch['批次编号']}-{audit_id:06d}",
                "批次id": batch["id"],
                "批次编号": batch["批次编号"],
                "审计记录id": audit_id,
                "审计编号": audit.get("记录编号"),
                "业务月": audit_month,
                "入账月": batch["入账月"],
                "来源": audit.get("数据来源", "unknown"),
                "额度键": _quota_key(audit.get("数据来源", "unknown"), batch["入账月"]),
                "扫描行数": str(scanned),
                "计费行数": str(_round_amount(scanned * coefficient, rounding)),
                "口径版本": caliber["版本编号"],
                "是否迟到": is_late,
                "入账类型": "补记" if is_late else "正常",
                "状态说明": (
                    f"业务月{audit_month}迟到，{batch['入账月']}补记"
                    if is_late else "本期正常入账"
                ),
                "核对状态": "待核对",
                "status": DETAIL_STATUS_INCLUDED,
                "pending": batch.get("status") != BATCH_STATUS_CLOSED,
                "abnormal": is_late,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            rows.append(detail)
            # 回写审计的计入范围，查询审计据此核对。
            audit["结算明细id"] = detail_id
            audit["结算批次"] = batch["批次编号"]
            audit["业务月"] = audit_month
            audit["入账月"] = batch["入账月"]
            audit["计入扫描行数"] = detail["计费行数"]
            audit["结算状态"] = DETAIL_STATUS_INCLUDED
            audit["结算口径"] = caliber["版本编号"]

        # 暂存区里的迟到行标记为已入账。
        for staged in store.rows(STAGING_MODULE):
            if _detail_for_audit(_to_int(staged.get("审计记录id"))) is not None:
                staged["已入账"] = True
                staged["status"] = "已补记"

    def _refresh_batch_metrics(self, batch: dict[str, Any]) -> None:
        details = _details_for_batch(batch["id"])
        batch["执行笔数"] = str(len(details))
        batch["扫描行数"] = str(sum(_to_int(row.get("计费行数")) for row in details))

    # ---------------------------------------------------------------- 额度

    def refresh_monthly_quota(self, entry_month: str) -> dict[str, Any]:
        """把已入账明细增量累加到 ``cost_govern`` 的月度额度清单。

        只按明细的额度键重算当月用量，不重扫审计；重复调用结果一致。
        """
        policies = store.rows("cost_govern")
        included = [
            row for row in store.rows(DETAIL_MODULE)
            if row.get("入账月") == entry_month
            and row.get("status") == DETAIL_STATUS_INCLUDED
        ]
        totals: dict[str, dict[str, Any]] = {}
        for detail in included:
            key = str(detail.get("额度键") or "")
            bucket = totals.setdefault(key, {"amount": 0, "count": 0})
            bucket["amount"] += _to_int(detail.get("计费行数"))
            bucket["count"] += 1

        # 当月已无有效明细的策略要清零，避免冲销后沿用旧用量。
        for policy in policies:
            if policy.get("生效时段") == entry_month and _policy_key(policy) not in totals:
                policy["已用扫描量"] = "0"
                policy["入账笔数"] = "0"
                policy["用量占比"] = "0.0%"
                policy["abnormal"] = False

        updated = []
        for key, bucket in sorted(totals.items()):
            policy = next((p for p in policies if _policy_key(p) == key), None)
            if policy is None:
                policy = _create_policy(policies, key, entry_month)
            policy["已用扫描量"] = str(bucket["amount"])
            policy["入账笔数"] = str(bucket["count"])
            policy["结算口径"] = _caliber_for(entry_month)["版本编号"]
            policy["最后结算"] = time.strftime("%Y-%m-%d %H:%M:%S")
            quota = _to_int(policy.get("月度配额"))
            ratio = bucket["amount"] / quota if quota > 0 else 0
            policy["用量占比"] = f"{ratio:.1%}"
            if ratio > 1.0:
                policy["status"] = "已告警"
                policy["abnormal"] = True
                policy["pending"] = True
            else:
                policy["abnormal"] = False
            updated.append({
                "policy_id": policy["id"],
                "策略编号": policy.get("策略编号"),
                "额度键": key,
                "月度配额": quota,
                "已用扫描量": bucket["amount"],
                "入账笔数": bucket["count"],
                "用量占比": policy["用量占比"],
                "status": policy.get("status"),
            })

        return {
            "ok": True,
            "入账月": entry_month,
            "policies_updated": len(updated),
            "items": updated,
            "total_included": sum(bucket["amount"] for bucket in totals.values()),
        }

    def quota_sheet(self, entry_month: str) -> dict[str, Any]:
        """月度额度清单：策略额度 + 计入明细 + 补记与原审计的关联。

        额度行按 ``来源@入账月`` 建键，即使当月尚未发生扫描也会列出额度本身。
        """
        included = [
            row for row in store.rows(DETAIL_MODULE)
            if row.get("入账月") == entry_month
            and row.get("status") == DETAIL_STATUS_INCLUDED
        ]
        by_key: dict[str, list[dict[str, Any]]] = {}
        for detail in included:
            by_key.setdefault(str(detail.get("额度键")), []).append(detail)

        policies = [
            p for p in store.rows("cost_govern")
            if p.get("生效时段") == entry_month
        ]
        keys = sorted(set(by_key) | {_policy_key(p) for p in policies})

        lines = []
        for key in keys:
            items = by_key.get(key, [])
            policy = next((p for p in policies if _policy_key(p) == key), None)
            if policy is None:
                policy = next((p for p in store.rows("cost_govern") if _policy_key(p) == key), None)
            quota = _to_int(policy.get("月度配额")) if policy else 0
            used = sum(_to_int(item.get("计费行数")) for item in items)
            lines.append({
                "额度键": key,
                "策略编号": policy.get("策略编号") if policy else None,
                "适用角色": key.split("@", 1)[0],
                "月度配额": quota,
                "已用扫描量": used,
                "剩余额度": max(quota - used, 0),
                "用量占比": f"{(used / quota if quota else 0):.1%}",
                "入账笔数": len(items),
                "补记笔数": sum(1 for item in items if item.get("是否迟到")),
                "结算口径": (policy or {}).get("结算口径") or _caliber_for(entry_month)["版本编号"],
                "details": [
                    _quota_detail(item)
                    for item in sorted(items, key=lambda d: _to_int(d.get("审计记录id")))
                ],
            })

        return {
            "ok": True,
            "入账月": entry_month,
            "total_quota": sum(line["月度配额"] for line in lines),
            "total_used": sum(line["已用扫描量"] for line in lines),
            "late_entries": sum(line["补记笔数"] for line in lines),
            "lines": lines,
        }

    # ---------------------------------------------------------------- 报告

    def build_report(self, batch_id: int) -> dict[str, Any]:
        """基于批次快照生成/刷新月度对账报告；旧报告保留生成时的口径版本。"""
        batch = store.find(BATCH_MODULE, batch_id)
        if batch is None:
            return {"ok": False, "message": "结算批次不存在"}
        details = _details_for_batch(batch_id)
        late = [row for row in details if row.get("是否迟到")]

        by_source: dict[str, int] = {}
        by_business_month: dict[str, int] = {}
        for row in details:
            amount = _to_int(row.get("计费行数"))
            source = str(row.get("来源"))
            by_source[source] = by_source.get(source, 0) + amount
            month = str(row.get("业务月"))
            by_business_month[month] = by_business_month.get(month, 0) + amount

        reports = store.rows(REPORT_MODULE)
        existing = next((r for r in reports if r.get("批次id") == batch_id), None)
        report_id = existing["id"] if existing else _next_id(reports)
        payload = {
            "batch": batch["批次编号"],
            "business_month": batch["业务月"],
            "entry_month": batch["入账月"],
            "caliber": batch["口径版本"],
            "executions": len(details),
            "scanned_rows": sum(_to_int(row.get("计费行数")) for row in details),
            "late_executions": len(late),
            "late_rows": sum(_to_int(row.get("计费行数")) for row in late),
            "by_source": by_source,
            "by_business_month": by_business_month,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        report = {
            "id": report_id,
            "报告编号": existing["报告编号"] if existing else f"SRPT-{report_id:04d}",
            "批次id": batch_id,
            "批次编号": batch["批次编号"],
            "业务月": batch["业务月"],
            "入账月": batch["入账月"],
            "口径版本": batch["口径版本"],
            "执行笔数": str(len(details)),
            "扫描行数": str(payload["scanned_rows"]),
            "迟到笔数": str(len(late)),
            "迟到行数": str(payload["late_rows"]),
            "来源分布": ", ".join(f"{k}:{v}" for k, v in sorted(by_source.items())),
            "业务月分布": ", ".join(f"{k}:{v}" for k, v in sorted(by_business_month.items())),
            "复查状态": "待复查" if late else "无需复查",
            "生成时间": payload["generated_at"],
            "status": BATCH_STATUS_CLOSED if batch.get("status") == BATCH_STATUS_CLOSED else "草稿",
            "pending": batch.get("status") != BATCH_STATUS_CLOSED or bool(late),
            "abnormal": bool(late),
        }
        if existing is None:
            reports.append(report)
        else:
            existing.update(report)
        return {"ok": True, "report": report, "snapshot": payload}

    def review_report(self, report_id: int) -> dict[str, Any]:
        """复查旧报告：返回快照、迟到明细与原批次/补记批次关系。"""
        report = store.find(REPORT_MODULE, report_id)
        if report is None:
            return {"ok": False, "message": "结算报告不存在"}
        batch = store.find(BATCH_MODULE, int(report.get("批次id", 0)))
        late_details = []
        for detail in store.rows(DETAIL_MODULE):
            if detail.get("业务月") != report.get("业务月"):
                continue
            if not detail.get("是否迟到"):
                continue
            audit = store.find("query_audit", _to_int(detail.get("审计记录id")))
            late_details.append({
                "明细编号": detail["明细编号"],
                "审计记录id": detail["审计记录id"],
                "审计编号": detail.get("审计编号"),
                "业务月": detail.get("业务月"),
                "入账月": detail.get("入账月"),
                "计费行数": _to_int(detail.get("计费行数")),
                "批次编号": detail.get("批次编号"),
                "查询语句": audit.get("查询语句") if audit else None,
                "查询时间": audit.get("查询时间") if audit else None,
            })
        supplements = [
            {"批次编号": b["批次编号"], "业务月": b["业务月"], "入账月": b["入账月"],
             "状态": b.get("status"), "扫描行数": b.get("扫描行数")}
            for b in store.rows(BATCH_MODULE)
            if b.get("业务月") == report.get("业务月") and b["id"] != report.get("批次id")
        ]
        return {
            "ok": True,
            "report": report.get("report") or {
                "batch": report.get("批次编号"),
                "business_month": report.get("业务月"),
                "entry_month": report.get("入账月"),
                "caliber": report.get("口径版本"),
                "scanned_rows": _to_int(report.get("扫描行数")),
                "late_executions": _to_int(report.get("迟到笔数")),
            },
            "report_row": report,
            "batch": batch,
            "supplements": supplements,
            "late_details": late_details,
            "needs_review": bool(late_details) or report.get("复查状态") == "待复查",
        }

    # ---------------------------------------------------------------- 对账

    def reconcile_month(self, business_month: str) -> dict[str, Any]:
        """核对业务月每次执行是否有且仅有一条有效明细归属。"""
        audits = [a for a in store.rows("query_audit") if _audit_month(a) == business_month]
        details = [d for d in store.rows(DETAIL_MODULE)
                   if d.get("业务月") == business_month
                   or d.get("入账月") == business_month]
        by_audit: dict[int, list[dict[str, Any]]] = {}
        for detail in details:
            by_audit.setdefault(_to_int(detail.get("审计记录id")), []).append(detail)

        batches = [b for b in store.rows(BATCH_MODULE)
                   if b.get("业务月") == business_month or b.get("入账月") == business_month]
        batch_ids = {b["id"] for b in batches}

        covered: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        duplicated: list[dict[str, Any]] = []
        late: list[dict[str, Any]] = []
        for audit in audits:
            audit_id = int(audit.get("id", 0))
            hits = [d for d in by_audit.get(audit_id, [])
                    if d.get("status") == DETAIL_STATUS_INCLUDED]
            if len(hits) > 1:
                duplicated.append({
                    "audit_id": audit_id,
                    "记录编号": audit.get("记录编号"),
                    "明细": [dict(d) for d in hits],
                })
                continue
            if not hits:
                missing.append({
                    "audit_id": audit_id,
                    "记录编号": audit.get("记录编号"),
                    "业务月": _audit_month(audit),
                    "扫描行数": _to_int(audit.get("扫描行数")),
                    "审计状态": audit.get("status"),
                })
                continue
            detail = hits[0]
            item = {
                "audit_id": audit_id,
                "记录编号": audit.get("记录编号"),
                "明细编号": detail["明细编号"],
                "业务月": detail.get("业务月"),
                "入账月": detail.get("入账月"),
                "计费行数": _to_int(detail.get("计费行数")),
                "批次编号": detail.get("批次编号"),
                "是否迟到": bool(detail.get("是否迟到")),
            }
            covered.append(item)
            if detail.get("是否迟到"):
                late.append(item)

        return {
            "ok": True,
            "业务月": business_month,
            "审计总数": len(audits),
            "已计入": len(covered),
            "未入账": len(missing),
            "重复入账": len(duplicated),
            "迟到补记": len(late),
            "计入扫描量": sum(item["计费行数"] for item in covered),
            "批次范围": [
                {"批次编号": b["批次编号"], "业务月": b["业务月"], "入账月": b["入账月"],
                 "状态": b.get("status"), "扫描行数": b.get("扫描行数"),
                 "明细数": len(_details_for_batch(b["id"]))}
                for b in batches if b["id"] in batch_ids
            ],
            "missing": missing[:50],
            "duplicated": duplicated[:20],
            "late": late[:50],
        }

    def record_reconcile(self, business_month: str, operator: str = "settlement") -> dict[str, Any]:
        """执行对账并把结果落成对账记录，供查询审计复查。"""
        result = self.reconcile_month(business_month)
        rows = store.rows(RECONCILE_MODULE)
        row = {
            "id": _next_id(rows),
            "对账编号": f"SREC-{business_month.replace('-', '')}-{len(rows) + 1:03d}",
            "业务月": business_month,
            "审计总数": str(result["审计总数"]),
            "已计入": str(result["已计入"]),
            "未入账": str(result["未入账"]),
            "重复入账": str(result["重复入账"]),
            "迟到补记": str(result["迟到补记"]),
            "计入扫描量": str(result["计入扫描量"]),
            "核对状态": "有差异" if result["未入账"] or result["重复入账"] else "已核对",
            "核对时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "操作人": operator,
            "status": "有差异" if result["未入账"] or result["重复入账"] else "已核对",
            "pending": bool(result["未入账"] or result["重复入账"]),
            "abnormal": bool(result["重复入账"]),
        }
        rows.append(row)
        result["record"] = row
        return result

    # ---------------------------------------------------------------- 补报

    def register_audit(self, values: dict[str, Any]) -> int:
        """登记一条待补报的审计执行记录；返回审计 id。

        记录编号是执行的外部唯一键：补报重放时复用同一审计，不新增执行。
        """
        rows = store.rows("query_audit")
        record_no = values.get("记录编号")
        if record_no:
            existing = next(
                (a for a in rows if str(a.get("记录编号")) == str(record_no)), None)
            if existing is not None:
                return int(existing["id"])
        audit_id = _next_id(rows)
        row = {
            "id": audit_id,
            "记录编号": record_no or f"AUDIT-LATE-{audit_id:04d}",
            "查询语句": values.get("查询语句", ""),
            "执行耗时": str(values.get("执行耗时", "0ms")),
            "扫描行数": str(_to_int(values.get("扫描行数"))),
            "返回行数": str(_to_int(values.get("返回行数"))),
            "数据来源": values.get("数据来源", "query_federation"),
            "操作用户": values.get("操作用户", "audit_ingest"),
            "查询时间": values.get("查询时间") or time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": values.get("status", "正常"),
            "pending": bool(values.get("pending", True)),
            "abnormal": False,
        }
        rows.append(row)
        return audit_id

    def backfill_audit(self, audit: dict[str, Any] | None = None,
                       business_month: str | None = None,
                       entry_month: str | None = None) -> dict[str, Any]:
        """迟到审计补报。

        - 业务月取审计执行时间；原月已封账时，业务月保持不变，另开一个
          ``入账月=发现月`` 的补记批次，已封账的旧批次不被改写。
        - 原月仍在途时，直接在当前批次增量归集。
        - 同一执行已入账时直接返回既有明细，不会写第二遍。
        """
        with _SETTLE_LOCK:
            if audit is not None:
                audit_id = self.register_audit(audit)
            else:
                # 没指定审计时，优先取最早业务月里尚未归属的执行作为补报对象。
                unbooked = [
                    a for a in store.rows("query_audit")
                    if _detail_for_audit(int(a.get("id", 0))) is None and _audit_month(a)
                ]
                pending = min(
                    unbooked,
                    key=lambda a: (_audit_month(a) or "", int(a.get("id", 0))),
                    default=None,
                )
                if pending is None:
                    return {"ok": False, "message": "没有待补报的审计执行"}
                audit_id = int(pending["id"])

            row = store.find("query_audit", audit_id)
            if row is None:
                return {"ok": False, "message": "审计记录不存在"}
            month = business_month or _audit_month(row)
            if month is None:
                return {"ok": False, "message": "审计记录缺少可识别的查询时间，无法确定业务月"}

            existing = _detail_for_audit(audit_id)
            if existing is not None:
                return {
                    "ok": True,
                    "message": "该执行已入账，未重复写入",
                    "audit_id": audit_id,
                    "detail": existing,
                    "duplicate": True,
                }

            entry = entry_month or _current_month()
            # 原业务月仍在途时，迟到审计直接并入原批次，不再另开补记期。
            open_batch = next(
                (b for b in store.rows(BATCH_MODULE)
                 if b.get("业务月") == month and b.get("status") == BATCH_STATUS_OPEN
                 and b.get("入账月") == month),
                None,
            )
            if open_batch is None:
                open_batch = self._open_batch(month, entry)
            if open_batch is None:
                if entry <= month:
                    entry = _current_month()
                closed = self._closed_batch_for(month, None)
                remark = (
                    f"{month} 迟到审计补记（原批次 {closed['批次编号']} 已封账）"
                    if closed else f"{month} 迟到审计补记"
                )
                batch, message = self.open_batch(month, entry, remark)
                if batch is None:
                    return {"ok": False, "message": message}
            else:
                batch = open_batch
                message = f"复用在途批次 {batch['批次编号']}"

            self._stage_late(row, month)
            result = self.run_batch(batch["id"])
            detail = _detail_for_audit(audit_id)
            return {
                "ok": bool(result.get("ok")),
                "message": f"{message}；{result.get('message', '')}",
                "audit_id": audit_id,
                "business_month": month,
                "entry_month": batch["入账月"],
                "batch": batch,
                "detail": detail,
                "late": True,
                "duplicate": False,
            }

    def _stage_late(self, audit: dict[str, Any], month: str) -> dict[str, Any]:
        rows = store.rows(STAGING_MODULE)
        existing = next(
            (s for s in rows if _to_int(s.get("审计记录id")) == int(audit["id"])), None)
        if existing is not None:
            existing["已入账"] = False
            existing["status"] = "待补记"
            return existing
        caliber = _caliber_for(_current_month())
        staged = {
            "id": _next_id(rows),
            "审计记录id": audit["id"],
            "审计编号": audit.get("记录编号"),
            "业务月": month,
            "扫描行数": str(_to_int(audit.get("扫描行数"))),
            "计费行数": str(_round_amount(
                _to_int(audit.get("扫描行数")),
                str(caliber.get("舍入规则", "round")),
            )),
            "口径版本": caliber["版本编号"],
            "已入账": False,
            "status": "待补记",
            "pending": True,
            "abnormal": True,
        }
        rows.append(staged)
        return staged

    def recheck_unbooked(self, business_month: str) -> dict[str, Any]:
        """为未入账记录重新准备数据：重建暂存区并返回可补报清单。"""
        with _SETTLE_LOCK:
            missing = []
            staged = []
            for audit in store.rows("query_audit"):
                month = _audit_month(audit)
                if month is None or month > business_month:
                    continue
                if _detail_for_audit(int(audit.get("id", 0))) is not None:
                    continue
                missing.append({
                    "audit_id": audit["id"],
                    "记录编号": audit.get("记录编号"),
                    "业务月": month,
                    "扫描行数": _to_int(audit.get("扫描行数")),
                })
                staged.append(self._stage_late(audit, month))
            return {
                "ok": True,
                "business_month": business_month,
                "unbooked": len(missing),
                "staged": len(staged),
                "items": missing,
            }

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """批次动作走封账逻辑，保证额度与报告同步刷新。"""
        if action == "封账批次":
            result = self.close_batch(entry_id)
            return (result.get("batch"), result.get("message", "")) if result.get("ok") else (None, result.get("message", ""))
        return super().run_action(entry_id, action)

    # ---------------------------------------------------------------- 内部

    def _open_batch(self, business_month: str, entry_month: str) -> dict[str, Any] | None:
        return next(
            (b for b in store.rows(BATCH_MODULE)
             if b.get("业务月") == business_month
             and b.get("入账月") == entry_month
             and b.get("status") == BATCH_STATUS_OPEN),
            None,
        )

    def _closed_batch_for(self, business_month: str, entry_month: str | None) -> dict[str, Any] | None:
        return next(
            (b for b in store.rows(BATCH_MODULE)
             if b.get("业务月") == business_month
             and b.get("status") == BATCH_STATUS_CLOSED
             and (entry_month is None or b.get("入账月") == entry_month)),
            None,
        )

    def _cursor_for(self, business_month: str, entry_month: str) -> int:
        """新批次起点：同业务月已入账/已消费的最大审计 id，避免重复消费。"""
        known = 0
        for detail in store.rows(DETAIL_MODULE):
            if detail.get("业务月") == business_month:
                known = max(known, _to_int(detail.get("审计记录id")))
        for batch in store.rows(BATCH_MODULE):
            if batch.get("业务月") == business_month:
                known = max(known, _to_int(batch.get("审计游标")))
        return known


# -------------------------------------------------------------------- 辅助

def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _to_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError, AttributeError):
        return 0


def _to_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (ValueError, TypeError, AttributeError):
        return default


def _round_amount(value: float, rule: str) -> int:
    if rule == "floor":
        return int(value // 1)
    if rule == "ceil":
        return int(-(-value // 1))
    return int(round(value))


def _is_month(value: str) -> bool:
    try:
        year, month = str(value).split("-")
        return len(year) == 4 and 1 <= int(month) <= 12
    except (ValueError, AttributeError):
        return False


def _current_month() -> str:
    return time.strftime("%Y-%m")


def _audit_month(audit: dict[str, Any]) -> str | None:
    raw = str(audit.get("查询时间") or "")
    candidate = raw[:7]
    return candidate if _is_month(candidate) else None


def _details_for_batch(batch_id: int) -> list[dict[str, Any]]:
    return [
        row for row in store.rows(DETAIL_MODULE)
        if row.get("批次id") == batch_id and row.get("status") == DETAIL_STATUS_INCLUDED
    ]


def _detail_for_audit(audit_id: int) -> dict[str, Any] | None:
    return next(
        (d for d in store.rows(DETAIL_MODULE)
         if _to_int(d.get("审计记录id")) == audit_id
         and d.get("status") == DETAIL_STATUS_INCLUDED),
        None,
    )


def _pending_staging(month: str) -> list[dict[str, Any]]:
    return [
        row for row in store.rows(STAGING_MODULE)
        if row.get("业务月") == month and not row.get("已入账")
    ]


def _quota_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """额度清单明细：把补记记录关联回原审计执行，便于复查。"""
    audit = store.find("query_audit", _to_int(detail.get("审计记录id")))
    original_batch = next(
        (b for b in store.rows(BATCH_MODULE)
         if b.get("业务月") == detail.get("业务月")
         and b.get("入账月") == detail.get("业务月")
         and b.get("status") == BATCH_STATUS_CLOSED),
        None,
    )
    return {
        "明细编号": detail["明细编号"],
        "审计记录id": detail["审计记录id"],
        "审计编号": detail.get("审计编号"),
        "查询语句": audit.get("查询语句") if audit else None,
        "查询时间": audit.get("查询时间") if audit else None,
        "操作用户": audit.get("操作用户") if audit else None,
        "业务月": detail.get("业务月"),
        "入账月": detail.get("入账月"),
        "扫描行数": _to_int(detail.get("扫描行数")),
        "计费行数": _to_int(detail.get("计费行数")),
        "入账类型": detail.get("入账类型"),
        "是否迟到": bool(detail.get("是否迟到")),
        "批次编号": detail.get("批次编号"),
        "原月结批次": original_batch["批次编号"] if original_batch else None,
        "核对状态": detail.get("核对状态"),
        "status": detail.get("status"),
    }


def _quota_key(source: str, entry_month: str) -> str:
    return f"{source}@{entry_month}"


def _policy_key(policy: dict[str, Any]) -> str:
    return f"{policy.get('适用角色', '')}@{policy.get('生效时段', '')}"


def _create_policy(rows: list[dict[str, Any]], key: str, entry_month: str) -> dict[str, Any]:
    role = key.split("@", 1)[0] or "unknown"
    policy_id = _next_id(rows)
    policy = {
        "id": policy_id,
        "策略编号": f"COST-{entry_month.replace('-', '')}-{policy_id:04d}",
        "适用角色": role,
        "查询上限": "0",
        "并发上限": "0",
        "月度配额": "0",
        "告警阈值": "80",
        "限流动作": "monitor",
        "生效时段": entry_month,
        "status": "已生效",
        "pending": False,
        "abnormal": False,
    }
    rows.append(policy)
    return policy


def _caliber_for(month: str) -> dict[str, Any]:
    """按入账月匹配口径版本。

    只接受 ``生效月份 <= 入账月 < 失效月份`` 的版本；历史月份在版本失效后
    也不会被新口径覆盖，找不到时回落到默认口径。
    """
    versions = []
    for version in store.rows(CALIBER_MODULE):
        if version.get("status") == "已作废":
            continue
        start = str(version.get("生效月份", ""))
        end = str(version.get("失效月份", "") or "")
        if start <= month and (not end or month < end):
            versions.append(version)
    if not versions:
        return {"版本编号": "CAL-DEFAULT", "扫描行数系数": "1.0", "舍入规则": "round"}
    # 生效月份最新者优先；同月多版本按 id 取最新发布的一条。
    return max(versions, key=lambda v: (str(v.get("生效月份", "")), _to_int(v.get("id"))))


def _mark_prior_report_for_review(month: str, supplement: dict[str, Any]) -> None:
    """迟报补记后，把原月已封账报告标记为待复查，保留原快照不重算。"""
    for report in store.rows(REPORT_MODULE):
        if report.get("业务月") != month or report.get("批次id") == supplement["id"]:
            continue
        report["复查状态"] = "待复查"
        report["pending"] = True
        report["补记批次"] = supplement["批次编号"]


service = SettlementService()


# ---------------------------------------------------------------- 资源服务

class SettlementCaliberService(BaseModuleService):
    MODULE = CALIBER_MODULE
    ENTITY = "结算口径"
    LIST_FIELDS = ["版本编号", "版本序号", "生效月份", "失效月份", "扫描行数系数",
                   "计入口径", "舍入规则", "发布人", "发布时间"]
    REQUIRED_FIELDS = ["版本编号", "生效月份"]
    STATUS_ORDER = ["草稿", "已生效", "已停用"]
    ACTION_RULES = {"启用版本": "已生效", "停用版本": "已停用"}
    NEGATIVE_ACTIONS = ["停用版本"]


class SettlementDetailService(BaseModuleService):
    MODULE = DETAIL_MODULE
    ENTITY = "结算明细"
    LIST_FIELDS = ["明细编号", "批次编号", "审计编号", "业务月", "入账月", "来源",
                   "扫描行数", "计费行数", "口径版本", "入账类型", "核对状态"]
    REQUIRED_FIELDS = ["明细编号"]
    STATUS_ORDER = [DETAIL_STATUS_INCLUDED, DETAIL_STATUS_REVERSED]
    ACTION_RULES = {"冲销明细": DETAIL_STATUS_REVERSED}
    NEGATIVE_ACTIONS = ["冲销明细"]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is not None and action == "冲销明细":
            # 冲销后重算当月额度清单与批次汇总，避免沿用旧明细。
            from app.services.settlement import service as settlement
            settlement.refresh_monthly_quota(str(entry.get("入账月")))
            batch = store.find(BATCH_MODULE, int(entry.get("批次id", 0)))
            if batch is not None:
                settlement._refresh_batch_metrics(batch)
        return entry, message


class SettlementReportService(BaseModuleService):
    """对账报告：确认复查时同步清除待复查标记。"""

    MODULE = REPORT_MODULE
    ENTITY = "对账报告"
    LIST_FIELDS = ["报告编号", "批次编号", "业务月", "入账月", "口径版本", "执行笔数",
                   "扫描行数", "迟到笔数", "复查状态", "生成时间"]
    REQUIRED_FIELDS = ["报告编号"]
    STATUS_ORDER = ["草稿", "已封账", "已复查"]
    ACTION_RULES = {"确认复查": "已复查"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is not None and action == "确认复查":
            entry["复查状态"] = "已复查"
            entry["pending"] = False
        return entry, message


class SettlementStagingService(BaseModuleService):
    MODULE = STAGING_MODULE
    ENTITY = "待补记审计"
    LIST_FIELDS = ["审计编号", "业务月", "扫描行数", "计费行数", "口径版本", "已入账"]
    REQUIRED_FIELDS = ["审计编号"]
    STATUS_ORDER = ["待补记", "已补记", "已作废"]
    ACTION_RULES = {"标记补记": "已补记", "作废暂存": "已作废"}
    NEGATIVE_ACTIONS = ["作废暂存"]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is not None and action == "标记补记":
            entry["已入账"] = True
        return entry, message


class SettlementReconcileService(BaseModuleService):
    MODULE = RECONCILE_MODULE
    ENTITY = "对账记录"
    LIST_FIELDS = ["对账编号", "业务月", "审计总数", "已计入", "未入账", "重复入账",
                   "迟到补记", "计入扫描量", "核对状态", "核对时间"]
    REQUIRED_FIELDS = ["对账编号", "业务月"]
    STATUS_ORDER = ["待核对", "已核对", "有差异"]
    ACTION_RULES = {"确认核对": "已核对", "标记差异": "有差异"}
    NEGATIVE_ACTIONS = ["标记差异"]
