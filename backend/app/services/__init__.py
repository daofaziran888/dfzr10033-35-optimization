"""领域服务注册表：通用资源路由按模块 key 分派到独立业务服务。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.source_registry import SourceRegistryService
from app.services.semantic_model import SemanticModelService
from app.services.query_federation import QueryFederationService
from app.services.olap_cube import OlapCubeService
from app.services.metric_factory import MetricFactoryService
from app.services.dimension_tree import DimensionTreeService
from app.services.aggregate_cache import AggregateCacheService
from app.services.query_rewrite import QueryRewriteService
from app.services.time_window import TimeWindowService
from app.services.dataset_slice import DatasetSliceService
from app.services.drill_config import DrillConfigService
from app.services.result_mart import ResultMartService
from app.services.benchmark_set import BenchmarkSetService
from app.services.query_audit import QueryAuditService
from app.services.cost_govern import CostGovernService
from app.services.freshness_mon import FreshnessMonService
from app.services.data_exporter import DataExporterService
from app.services.schedule_job import ScheduleJobService
from app.services.data_lake_read import DataLakeReadService
from app.services.result_cache import ResultCacheService
from app.services.data_contract import DataContractService
from app.services.ab_config import AbConfigService
from app.services.settlement import (
    SettlementService,
    SettlementCaliberService,
    SettlementDetailService,
    SettlementReportService,
    SettlementStagingService,
    SettlementReconcileService,
)

SERVICES: dict[str, BaseModuleService] = {
    "source_registry": SourceRegistryService(),
    "semantic_model": SemanticModelService(),
    "query_federation": QueryFederationService(),
    "olap_cube": OlapCubeService(),
    "metric_factory": MetricFactoryService(),
    "dimension_tree": DimensionTreeService(),
    "aggregate_cache": AggregateCacheService(),
    "query_rewrite": QueryRewriteService(),
    "time_window": TimeWindowService(),
    "dataset_slice": DatasetSliceService(),
    "drill_config": DrillConfigService(),
    "result_mart": ResultMartService(),
    "benchmark_set": BenchmarkSetService(),
    "query_audit": QueryAuditService(),
    "cost_govern": CostGovernService(),
    "freshness_mon": FreshnessMonService(),
    "data_exporter": DataExporterService(),
    "schedule_job": ScheduleJobService(),
    "data_lake_read": DataLakeReadService(),
    "result_cache": ResultCacheService(),
    "data_contract": DataContractService(),
    "ab_config": AbConfigService(),
    "settlement_batch": SettlementService(),
    "settlement_detail": SettlementDetailService(),
    "settlement_report": SettlementReportService(),
    "settlement_caliber": SettlementCaliberService(),
    "settlement_staging": SettlementStagingService(),
    "settlement_reconcile": SettlementReconcileService(),
}


def service_for(module: str) -> BaseModuleService | None:
    return SERVICES.get(module)
