import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---- 月度扫描结算 ----

export type SettlementBatch = {
  id: number
  "批次编号": string
  "业务月": string
  "入账月": string
  "口径版本": string
  "执行笔数": string
  "扫描行数": string
  status: string
}

export type QuotaDetail = {
  "明细编号": string
  "审计记录id": number
  "审计编号": string | null
  "查询语句": string | null
  "查询时间": string | null
  "业务月": string
  "入账月": string
  "扫描行数": number
  "计费行数": number
  "入账类型": string
  "是否迟到": boolean
  "批次编号": string
  "原月结批次": string | null
  "核对状态": string
  status: string
  [key: string]: unknown
}

export type QuotaLine = {
  "额度键": string
  "策略编号": string | null
  "适用角色": string
  "月度配额": number
  "已用扫描量": number
  "剩余额度": number
  "用量占比": string
  "入账笔数": number
  "补记笔数": number
  "结算口径": string
  details: QuotaDetail[]
  [key: string]: unknown
}

export type QuotaSheet = {
  ok: boolean
  "入账月": string
  total_quota: number
  total_used: number
  late_entries: number
  lines: QuotaLine[]
}

export type ReconcileResult = {
  ok: boolean
  business_month: string
  reconcile: Record<string, unknown> & {
    "审计总数": number
    "已计入": number
    "未入账": number
    "重复入账": number
    "迟到补记": number
    "计入扫描量": number
    missing: { audit_id: number; 记录编号: string; 业务月: string; 扫描行数: number }[]
  }
  details_checked: number
  unbooked_prepared: number
  staged: number
}

export async function settleMonth(
  businessMonth: string,
  entryMonth?: string,
  close = true,
): Promise<{ ok: boolean; settlement: { message: string; batch: SettlementBatch }; close: { message: string } | null; monthly_quota: QuotaSheet }> {
  const params = new URLSearchParams({ business_month: businessMonth, close: String(close) })
  if (entryMonth) params.set("entry_month", entryMonth)
  const response = await request(`/api/pipelines/monthly-settlement?${params.toString()}`, { method: "POST" })
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail ?? "月结失败")
  return await response.json()
}

export async function backfillAudit(values: {
  recordNo?: string
  queryStmt?: string
  scannedRows: string
  executedAt: string
  source?: string
  operator?: string
}): Promise<{
  ok: boolean
  message: string
  audit_id: number
  business_month: string
  entry_month: string
  detail: QuotaDetail | null
}> {
  const params = new URLSearchParams({
    scanned_rows: values.scannedRows,
    executed_at: values.executedAt,
    query_stmt: values.queryStmt ?? "SELECT * FROM federated(late_audit)",
    audit_source: values.source ?? "query_federation",
    operator: values.operator ?? "audit_ingest",
  })
  if (values.recordNo) params.set("record_no", values.recordNo)
  const response = await request(`/api/pipelines/audit-backfill?${params.toString()}`, { method: "POST" })
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail ?? "补报失败")
  return await response.json()
}

export async function reconcileAuditSettlement(businessMonth: string): Promise<ReconcileResult> {
  const params = new URLSearchParams({ business_month: businessMonth })
  const response = await request(`/api/pipelines/audit-settlement-reconcile?${params.toString()}`, { method: "POST" })
  if (!response.ok) throw new Error("结算对账失败")
  return await response.json()
}

export async function loadQuotaSheet(entryMonth: string): Promise<QuotaSheet> {
  const params = new URLSearchParams({ entry_month: entryMonth })
  const response = await request(`/api/settlement/quota-sheet?${params.toString()}`)
  if (!response.ok) throw new Error("月度额度清单读取失败")
  return await response.json()
}

export async function reviewReport(reportId: number): Promise<Record<string, unknown>> {
  const response = await request(`/api/settlement/reports/${reportId}/review`)
  if (!response.ok) throw new Error("对账报告复查失败")
  return await response.json()
}

export async function prepareUnbooked(businessMonth: string): Promise<Record<string, unknown>> {
  const params = new URLSearchParams({ business_month: businessMonth })
  const response = await request(`/api/query-audit/prepare-unbooked?${params.toString()}`, { method: "POST" })
  if (!response.ok) throw new Error("未入账数据准备失败")
  return await response.json()
}

export async function closeSettlementBatch(batchId: number): Promise<{ ok: boolean; message: string }> {
  const response = await request(`/api/settlement/batches/${batchId}/close`, { method: "POST" })
  if (!response.ok) throw new Error("批次封账失败")
  return await response.json()
}

export async function settleBatchAgain(batchId: number): Promise<{ ok: boolean; message: string }> {
  const response = await request(`/api/settlement/batches/${batchId}/run`, { method: "POST" })
  if (!response.ok) throw new Error("增量归集失败")
  return await response.json()
}
