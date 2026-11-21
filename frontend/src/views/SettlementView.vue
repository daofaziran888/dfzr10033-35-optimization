<template>
  <section class="page settlement-page">
    <header class="page-head">
      <div>
        <h2>月度结算</h2>
        <p class="page-desc">
          按批次增量归集查询审计扫描量，分别记录业务月与入账月；封账后生成对账报告，
          迟到审计走补记批次，历史月结不重算。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="settle-run" type="button" :disabled="busy" @click="runSettlement">
          {{ busy ? '结算中…' : '执行月结' }}
        </button>
      </div>
    </header>

    <p v-if="message" class="settlement-banner" data-testid="settlement-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="settlement-error">{{ errorMessage }}</p>

    <form class="analysis-form" @submit.prevent="loadQuota">
      <label>
        <span>业务月</span>
        <input v-model="form.businessMonth" data-testid="settle-business-month" placeholder="YYYY-MM" required />
      </label>
      <label>
        <span>入账月</span>
        <input v-model="form.entryMonth" data-testid="settle-entry-month" placeholder="默认同业务月" />
      </label>
      <label>
        <span>封账后直接出报告</span>
        <select v-model="form.close">
          <option :value="true">是</option>
          <option :value="false">否，先归集</option>
        </select>
      </label>
      <button class="btn" data-testid="quota-load" type="submit">打开月度额度清单</button>
      <button class="btn" data-testid="reconcile-run" type="button" :disabled="busy" @click="runReconcile">
        核对计入范围
      </button>
    </form>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">额度总量</span>
        <strong class="stat-value" data-testid="quota-total">{{ quota.total_quota }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已用扫描量</span>
        <strong class="stat-value" data-testid="quota-used">{{ quota.total_used }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">补记笔数</span>
        <strong class="stat-value" data-testid="quota-late">{{ quota.late_entries }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">额度来源</span>
        <strong class="stat-value">{{ quota.lines.length }} 个额度键</strong>
      </article>
    </div>

    <section v-if="lastReconcile" class="settlement-panel" data-testid="reconcile-panel">
      <h3>查询审计 × 月结批次对账 · {{ lastReconcile.business_month }}</h3>
      <p>
        审计 {{ lastReconcile.reconcile["审计总数"] }} 条，已计入 {{ lastReconcile.reconcile["已计入"] }}，
        未入账 {{ lastReconcile.reconcile["未入账"] }}，重复入账 {{ lastReconcile.reconcile["重复入账"] }}，
        迟到补记 {{ lastReconcile.reconcile["迟到补记"] }}，计入扫描量
        {{ lastReconcile.reconcile["计入扫描量"] }}。
      </p>
      <p v-if="lastReconcile.reconcile.missing.length" class="error-text">
        未入账执行：{{ lastReconcile.reconcile.missing.map((item) => item["记录编号"]).join('、') }}
        （已重新准备 {{ lastReconcile.unbooked_prepared }} 条暂存数据）
      </p>
    </section>

    <div class="settlement-columns">
      <section class="settlement-panel">
        <h3>月度额度清单 · {{ quota["入账月"] || form.entryMonth || form.businessMonth }}</h3>
        <table class="data-table" data-testid="quota-table">
          <thead>
            <tr>
              <th>额度键</th><th>策略编号</th><th>月度配额</th><th>已用扫描量</th>
              <th>剩余额度</th><th>用量占比</th><th>入账笔数</th><th>补记笔数</th><th>口径</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="line in quota.lines" :key="String(line['额度键'])">
              <tr class="quota-line" :data-testid="`quota-${line['额度键']}`">
                <td>{{ line["额度键"] }}</td>
                <td>{{ line["策略编号"] ?? '—' }}</td>
                <td>{{ line["月度配额"] }}</td>
                <td>{{ line["已用扫描量"] }}</td>
                <td>{{ line["剩余额度"] }}</td>
                <td>{{ line["用量占比"] }}</td>
                <td>{{ line["入账笔数"] }}</td>
                <td>{{ line["补记笔数"] }}</td>
                <td>{{ line["结算口径"] }}</td>
              </tr>
              <tr v-for="detail in line.details" :key="String(detail['明细编号'])" class="quota-detail" :data-testid="`detail-${detail['明细编号']}`">
                <td colspan="9">
                  <span class="tag" :class="detail['是否迟到'] ? 'tag-late' : 'tag-normal'">
                    {{ detail["入账类型"] }}
                  </span>
                  {{ detail["明细编号"] }} · 审计 {{ detail["审计编号"] }}（{{ detail["查询时间"] }}）
                  · 业务月 {{ detail["业务月"] }} → 入账月 {{ detail["入账月"] }}
                  · {{ detail["计费行数"] }} 行 · 批次 {{ detail["批次编号"] }}
                  <template v-if="detail['原月结批次']">· 原批次 {{ detail["原月结批次"] }}</template>
                </td>
              </tr>
            </template>
            <tr v-if="!quota.lines.length">
              <td colspan="9" class="empty-state">该入账月还没有结算明细，先执行月结</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="settlement-panel">
        <h3>迟到审计补报</h3>
        <form class="analysis-form vertical" @submit.prevent="submitBackfill">
          <label>
            <span>记录编号</span>
            <input v-model="backfill.recordNo" data-testid="backfill-record" placeholder="AUDIT-LATE-0928" />
          </label>
          <label>
            <span>扫描行数</span>
            <input v-model="backfill.scannedRows" data-testid="backfill-scanned" type="number" required />
          </label>
          <label>
            <span>执行时间（决定业务月）</span>
            <input v-model="backfill.executedAt" data-testid="backfill-executed-at" placeholder="2026-09-28 23:40:00" required />
          </label>
          <label>
            <span>数据来源</span>
            <input v-model="backfill.source" placeholder="query_federation" />
          </label>
          <button class="btn primary" data-testid="backfill-submit" type="submit" :disabled="busy">
            补报迟到审计
          </button>
        </form>
        <p class="settlement-hint">
          补报保留业务月，入账月取发现月；同一执行重复补报只返回既有明细。
        </p>
      </section>
    </div>

    <section class="settlement-panel">
      <h3>月结批次与报告</h3>
      <div class="settlement-columns">
        <table class="data-table" data-testid="batch-table">
          <thead>
            <tr><th>批次编号</th><th>业务月</th><th>入账月</th><th>口径</th><th>执行笔数</th><th>扫描行数</th><th>状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="batch in batches" :key="String(batch.id)" :data-testid="`batch-${batch.id}`">
              <td>{{ batch["批次编号"] }}</td>
              <td>{{ batch["业务月"] }}</td>
              <td>{{ batch["入账月"] }}</td>
              <td>{{ batch["口径版本"] }}</td>
              <td>{{ batch["执行笔数"] }}</td>
              <td>{{ batch["扫描行数"] }}</td>
              <td>{{ batch.status }}</td>
              <td>
                <button
                  v-if="batch.status === '结算中'"
                  class="link"
                  type="button"
                  :data-testid="`close-${batch.id}`"
                  @click="closeBatch(batch)"
                >
                  封账
                </button>
                <button
                  v-if="batch.status === '结算中'"
                  class="link"
                  type="button"
                  :data-testid="`settle-again-${batch.id}`"
                  @click="settleAgain(batch)"
                >
                  增量归集
                </button>
              </td>
            </tr>
            <tr v-if="!batches.length"><td colspan="8" class="empty-state">暂无月结批次</td></tr>
          </tbody>
        </table>

        <table class="data-table" data-testid="report-table">
          <thead>
            <tr><th>报告编号</th><th>业务月</th><th>入账月</th><th>口径</th><th>扫描行数</th><th>迟到笔数</th><th>复查状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="report in reports" :key="String(report.id)" :data-testid="`report-${report.id}`">
              <td>{{ report["报告编号"] }}</td>
              <td>{{ report["业务月"] }}</td>
              <td>{{ report["入账月"] }}</td>
              <td>{{ report["口径版本"] }}</td>
              <td>{{ report["扫描行数"] }}</td>
              <td>{{ report["迟到笔数"] }}</td>
              <td>{{ report["复查状态"] }}</td>
              <td>
                <button class="link" type="button" :data-testid="`review-${report.id}`" @click="openReview(report)">
                  复查
                </button>
              </td>
            </tr>
            <tr v-if="!reports.length"><td colspan="8" class="empty-state">暂无对账报告</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="review" class="settlement-panel" data-testid="review-panel">
      <h3>复查旧报告 {{ (review.report_row as Record<string, unknown>)["报告编号"] }}</h3>
      <p>
        业务月 {{ (review.report_row as Record<string, unknown>)["业务月"] }}，
        当期口径 {{ (review.report_row as Record<string, unknown>)["口径版本"] }}，
        快照扫描行数 {{ (review.report_row as Record<string, unknown>)["扫描行数"] }}，
        迟到 {{ (review.report_row as Record<string, unknown>)["迟到笔数"] }} 笔。
      </p>
      <table v-if="reviewLate.length" class="data-table">
        <thead>
          <tr><th>明细编号</th><th>原审计</th><th>查询时间</th><th>业务月</th><th>入账月</th><th>计费行数</th><th>补记批次</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in reviewLate" :key="String(item['明细编号'])">
            <td>{{ item["明细编号"] }}</td>
            <td>{{ item["审计编号"] }}</td>
            <td>{{ item["查询时间"] }}</td>
            <td>{{ item["业务月"] }}</td>
            <td>{{ item["入账月"] }}</td>
            <td>{{ item["计费行数"] }}</td>
            <td>{{ item["批次编号"] }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="settlement-hint">该报告没有迟到补记，快照与当期报告一致。</p>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  backfillAudit,
  closeSettlementBatch,
  listEntries,
  loadQuotaSheet,
  reconcileAuditSettlement,
  reviewReport,
  settleBatchAgain,
  settleMonth,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'
import type { QuotaSheet, ReconcileResult } from '@/api/backend-service'

const emptyQuota: QuotaSheet = {
  ok: true,
  "入账月": "",
  total_quota: 0,
  total_used: 0,
  late_entries: 0,
  lines: [],
}

const form = ref({ businessMonth: "2026-09", entryMonth: "", close: true })
const pendingReports = computed(() => reports.value.filter((row) => row["复查状态"] === "待复查"))
const backfill = ref({
  recordNo: "",
  scannedRows: "5000",
  executedAt: "2026-09-28 23:40:00",
  source: "query_federation",
})
const quota = ref<QuotaSheet>({ ...emptyQuota })
const batches = ref<EntryRow[]>([])
const reports = ref<EntryRow[]>([])
const lastReconcile = ref<ReconcileResult | null>(null)
const review = ref<Record<string, unknown> | null>(null)
const message = ref("")
const errorMessage = ref("")
const busy = ref(false)

const reviewLate = computed(() => {
  const items = review.value?.["late_details"]
  return Array.isArray(items) ? (items as Record<string, unknown>[]) : []
})

function reportError(error: unknown, fallback: string) {
  errorMessage.value = error instanceof Error ? error.message : fallback
}

async function refreshTables() {
  const [batchPage, reportPage] = await Promise.all([
    listEntries("settlement_batch"),
    listEntries("settlement_report"),
  ])
  batches.value = batchPage.items
  reports.value = reportPage.items
}

async function loadQuota() {
  errorMessage.value = ""
  try {
    const entry = form.value.entryMonth || form.value.businessMonth
    quota.value = await loadQuotaSheet(entry)
    await refreshTables()
  } catch (error) {
    reportError(error, "月度额度清单读取失败")
  }
}

async function runReconcile() {
  busy.value = true
  errorMessage.value = ""
  try {
    lastReconcile.value = await reconcileAuditSettlement(form.value.businessMonth)
    message.value = `已核对 ${form.value.businessMonth} 的计入范围，未入账 ` +
      `${lastReconcile.value.reconcile["未入账"]} 条`
    await loadQuota()
  } catch (error) {
    reportError(error, "结算对账失败")
  } finally {
    busy.value = false
  }
}

async function runSettlement() {
  busy.value = true
  message.value = ""
  errorMessage.value = ""
  try {
    const result = await settleMonth(
      form.value.businessMonth,
      form.value.entryMonth || undefined,
      form.value.close,
    )
    message.value = [
      result.settlement.message,
      result.close?.message ?? "尚未封账",
      `额度用量 ${result.monthly_quota.total_used}`,
    ].join("；")
    await loadQuota()
  } catch (error) {
    reportError(error, "月结失败")
  } finally {
    busy.value = false
  }
}

async function submitBackfill() {
  busy.value = true
  message.value = ""
  errorMessage.value = ""
  try {
    const result = await backfillAudit({
      recordNo: backfill.value.recordNo,
      scannedRows: backfill.value.scannedRows,
      executedAt: backfill.value.executedAt,
      source: backfill.value.source,
    })
    message.value = `补报审计 ${result.audit_id}：业务月 ${result.business_month} → 入账月 ${result.entry_month}；${result.message}`
    form.value.entryMonth = result.entry_month
    await loadQuota()
  } catch (error) {
    reportError(error, "补报失败")
  } finally {
    busy.value = false
  }
}

async function closeBatch(batch: EntryRow) {
  errorMessage.value = ""
  try {
    const result = await closeSettlementBatch(Number(batch.id))
    message.value = result.message
    await loadQuota()
  } catch (error) {
    reportError(error, "封账失败")
  }
}

async function settleAgain(batch: EntryRow) {
  errorMessage.value = ""
  try {
    const result = await settleBatchAgain(Number(batch.id))
    message.value = result.message
    await loadQuota()
  } catch (error) {
    reportError(error, "增量归集失败")
  }
}

async function openReview(report: EntryRow) {
  errorMessage.value = ""
  try {
    review.value = await reviewReport(Number(report.id))
  } catch (error) {
    reportError(error, "报告复查失败")
  }
}

onMounted(async () => {
  try {
    await loadQuota()
  } catch (error) {
    reportError(error, "月度结算数据读取失败")
  }
})
</script>
