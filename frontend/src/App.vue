<template>
  <div class="app-shell">
    <aside class="app-side">
      <h1 class="app-title">湖仓工坊 · 联邦查询与多维自助分析平台</h1>
      <nav class="nav-list">
        <RouterLink v-for="item in navItems" :key="item.path" :to="item.path" class="nav-item">
          {{ item.label }}
        </RouterLink>
      </nav>
    </aside>
    <main class="app-main">
      <header class="app-head">
        <span class="head-desc">面向异构数据源联邦查询、语义模型构建、OLAP多维立方物化、聚合缓存预热、时序窗口分析、查询改写优化与数据集市发布的多维自助分析平台。</span>
        <span class="head-user">当前值班：数据工程组</span>
      </header>
      <RouterView />
    </main>
  </div>
</template>

<script setup lang="ts">
const navItems = [
  { label: "运营概览", path: "/" },
  { label: "月度结算", path: "/settlement" },
  { label: "数据源注册", path: "/source_registry" },
  { label: "语义模型", path: "/semantic_model" },
  { label: "联邦查询", path: "/query_federation" },
  { label: "多维立方", path: "/olap_cube" },
  { label: "指标工厂", path: "/metric_factory" },
  { label: "维度树", path: "/dimension_tree" },
  { label: "聚合缓存", path: "/aggregate_cache" },
  { label: "查询改写", path: "/query_rewrite" },
  { label: "时序窗口", path: "/time_window" },
  { label: "数据切片", path: "/dataset_slice" },
  { label: "下钻配置", path: "/drill_config" },
  { label: "结果集市", path: "/result_mart" },
  { label: "基准集", path: "/benchmark_set" },
  { label: "查询审计", path: "/query_audit" },
  { label: "成本治理", path: "/cost_govern" },
  { label: "新鲜度监控", path: "/freshness_mon" },
  { label: "分析导出", path: "/data_exporter" },
  { label: "定时计算", path: "/schedule_job" },
  { label: "湖仓读取", path: "/data_lake_read" },
  { label: "结果缓存", path: "/result_cache" },
  { label: "数据契约", path: "/data_contract" },
  { label: "实验配置", path: "/ab_config" },
  { label: "月结批次", path: "/settlement_batch" },
  { label: "结算明细", path: "/settlement_detail" },
  { label: "对账报告", path: "/settlement_report" },
  { label: "结算口径", path: "/settlement_caliber" },
  { label: "待补记审计", path: "/settlement_staging" },
  { label: "结算核对", path: "/settlement_reconcile" },
]
</script>
