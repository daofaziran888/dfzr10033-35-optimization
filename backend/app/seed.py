"""种子数据：每个模块一条记录，精简格式。"""
from typing import Any

# 通用种子字段模板
def _seed(id: int, fields: dict[str, str]) -> dict[str, Any]:
    return {"id": id, "status": "草稿", "pending": True, "abnormal": False, **fields}

SEED_ROWS: dict[str, list[dict[str, Any]]] = {
    "source_registry": [_seed(1, {"源编号":"SOUR-0001","源类型":"mysql","连接地址":"db://demo","驱动配置":"mysql_driver","数据库名":"demo_db","认证方式":"token","接入状态":"待验证","最近同步":"2026-11-01"})],
    "semantic_model": [_seed(1, {"模型编号":"SEMA-0001","模型名称":"语义模型样例","源表映射":"source_registry:1","字段别名":"id,name","度量定义":"COUNT(*),SUM(value)","维度定义":"region,category","计算口径":"标准口径","版本序号":"v1.0"})],
    "query_federation": [_seed(1, {"计划编号":"QUER-0001","参与来源":"SOUR-0001","关联键":"id","下推策略":"FULL_PUSHDOWN","数据量估算":"1000","并行度":"4","超时上限":"30s","结果合并":"DIRECT"})],
    "olap_cube": [_seed(1, {"立方编号":"OLAP-0001","立方名称":"多维立方样例","维度列表":"region,category","度量列表":"revenue,orders","聚合类型":"SUM","分区策略":"dt","刷新周期":"3600","数据行数":"0"})],
    "metric_factory": [_seed(1, {"指标编号":"METR-0001","指标名称":"指标工厂样例","数据来源":"olap_cube:1","计算公式":"SUM(revenue)","聚合窗口":"daily","精度模式":"exact","依赖立方":"1","最后产出":"2026-11-01"})],
    "dimension_tree": [_seed(1, {"层级编号":"DIME-0001","维度名称":"维度树样例","层级路径":"region>city","叶子字段":"city","汇总规则":"SUM","缓慢变化策略":"SCD2","行数预估":"1000","版本标签":"v1"})],
    "aggregate_cache": [_seed(1, {"配置编号":"AGGR-0001","目标立方":"olap_cube:1","缓存粒度":"2维x2度","TTL秒数":"3600","预热策略":"LAZY","命中阈值":"0.8","淘汰算法":"LRU","缓存大小":"1024"})],
    "query_rewrite": [_seed(1, {"规则编号":"QUER-0001","原始模式":"SELECT * FROM","替换模式":"SELECT /*optimized*/ FROM","适用模型":"semantic_model:1","改写条件":"always","代价阈值":"1000","命中次数":"0","最后命中":"-"})],
    "time_window": [_seed(1, {"窗口编号":"TIME-0001","窗口类型":"滑动","滑动宽度":"3600","滑动步长":"600","对齐基准":"epoch","缺值策略":"fill_prev","窗口别名":"1h_window","最后计算":"-"})],
    "dataset_slice": [_seed(1, {"切片编号":"DATA-0001","源立方":"1","维度过筛":"region=CN","度量过滤":"revenue>0","切片标签":"active","行数统计":"500","创建人":"admin","最后刷新":"-"})],
    "drill_config": [_seed(1, {"路径编号":"DRIL-0001","起始维度":"region","目标维度":"city","下钻类型":"hierarchy","关联键":"region_id","跳转规则":"exact","展示模板":"table","最后使用":"-"})],
    "result_mart": [_seed(1, {"集市编号":"RESU-0001","集市名称":"结果集市样例","数据来源":"result_cache","主题域":"analytics","行数统计":"0","刷新周期":"manual","下游消费":"dashboard","最后刷新":"-"})],
    "benchmark_set": [_seed(1, {"基线编号":"BENC-0001","基线名称":"基准集样例","数据范围":"2026-09","基线时段":"2026-09-01","对比维度":"region","指标数值":"1000","方差阈值":"0.1","锁定状态":"unlocked"})],
    "query_audit": [
        _seed(1, {"记录编号":"QUER-0001","查询语句":"SELECT * FROM demo","执行耗时":"100ms","扫描行数":"1000","返回行数":"10","数据来源":"query_federation","操作用户":"admin","查询时间":"2026-11-01 08:00:00"}),
        # 两个完整业务月的示例执行记录，供首次月结使用。
        _seed(2, {"记录编号":"AUDIT-202609-01","查询语句":"SELECT region, SUM(revenue) FROM federated(sales)","执行耗时":"820ms","扫描行数":"12000","返回行数":"120","数据来源":"query_federation","操作用户":"analyst_li","查询时间":"2026-09-05 09:15:00"}),
        _seed(3, {"记录编号":"AUDIT-202609-02","查询语句":"SELECT * FROM federated(orders) WHERE dt='2026-09-16'","执行耗时":"1500ms","扫描行数":"9000","返回行数":"90","数据来源":"query_federation","操作用户":"analyst_li","查询时间":"2026-09-18 14:20:00"}),
        _seed(4, {"记录编号":"AUDIT-202609-03","查询语句":"SCAN lakehouse://prod/dt=2026-09-25","执行耗时":"610ms","扫描行数":"7000","返回行数":"70","数据来源":"data_lake_read","操作用户":"ops_wang","查询时间":"2026-09-26 11:05:00"}),
        _seed(5, {"记录编号":"AUDIT-202610-01","查询语句":"SELECT category, COUNT(*) FROM federated(catalog)","执行耗时":"990ms","扫描行数":"15000","返回行数":"150","数据来源":"query_federation","操作用户":"analyst_li","查询时间":"2026-10-08 10:30:00"}),
        _seed(6, {"记录编号":"AUDIT-202610-02","查询语句":"SELECT * FROM federated(sales) WHERE dt='2026-10-18'","执行耗时":"760ms","扫描行数":"8000","返回行数":"80","数据来源":"query_federation","操作用户":"ops_wang","查询时间":"2026-10-19 16:40:00"}),
        _seed(7, {"记录编号":"AUDIT-202610-03","查询语句":"SCAN lakehouse://prod/dt=2026-10-24","执行耗时":"530ms","扫描行数":"6000","返回行数":"60","数据来源":"data_lake_read","操作用户":"ops_wang","查询时间":"2026-10-25 09:10:00"}),
    ],
    "cost_govern": [
        _seed(1, {"策略编号":"COST-0001","适用角色":"analyst","查询上限":"10000","并发上限":"10","月度配额":"100000","告警阈值":"80","限流动作":"monitor","生效时段":"2026-11"}),
        # 月度额度按“来源角色 + 业务月”建账；结算清单只引用这些额度，不再全表重扫审计。
        {**_seed(2, {"策略编号":"COST-202609-FED","适用角色":"query_federation","查询上限":"25000","并发上限":"10","月度配额":"25000","告警阈值":"80","限流动作":"monitor","生效时段":"2026-09"}), "status":"已生效","pending":False},
        {**_seed(3, {"策略编号":"COST-202609-LAKE","适用角色":"data_lake_read","查询上限":"10000","并发上限":"6","月度配额":"10000","告警阈值":"80","限流动作":"monitor","生效时段":"2026-09"}), "status":"已生效","pending":False},
        {**_seed(4, {"策略编号":"COST-202610-FED","适用角色":"query_federation","查询上限":"30000","并发上限":"10","月度配额":"30000","告警阈值":"80","限流动作":"monitor","生效时段":"2026-10"}), "status":"已生效","pending":False},
        {**_seed(5, {"策略编号":"COST-202610-LAKE","适用角色":"data_lake_read","查询上限":"10000","并发上限":"6","月度配额":"10000","告警阈值":"80","限流动作":"monitor","生效时段":"2026-10"}), "status":"已生效","pending":False},
        {**_seed(6, {"策略编号":"COST-202611-FED","适用角色":"query_federation","查询上限":"30000","并发上限":"10","月度配额":"30000","告警阈值":"80","限流动作":"monitor","生效时段":"2026-11"}), "status":"已生效","pending":False},
        {**_seed(7, {"策略编号":"COST-202611-LAKE","适用角色":"data_lake_read","查询上限":"10000","并发上限":"6","月度配额":"10000","告警阈值":"80","限流动作":"monitor","生效时段":"2026-11"}), "status":"已生效","pending":False},
    ],
    "freshness_mon": [_seed(1, {"规则编号":"FRES-0001","目标数据集":"all","SLA秒数":"3600","检测频率":"hourly","延迟阈值":"600","告警级别":"WARNING","触发次数":"0","最后检查":"-"})],
    "data_exporter": [_seed(1, {"任务编号":"DATA-0001","数据来源":"result_cache","筛选条件":"all","输出格式":"csv","字段列表":"*","排序规则":"id","导出上限":"1000","接收地址":"local://"})],
    "schedule_job": [_seed(1, {"任务编号":"SCHE-0001","目标立方":"1","Cron表达式":"0 */1 * * *","时区设置":"Asia/Shanghai","依赖检查":"none","超时上限":"600","失败通知":"email","下次执行":"-"})],
    "data_lake_read": [_seed(1, {"路径编号":"DATA-0001","存储位置":"s3://data-lake/prod","文件格式":"parquet","分区模式":"dt","Schema推导":"","文件列表":"","最后扫描":"","数据量":""})],
    "result_cache": [_seed(1, {"条目编号":"RESU-0001","查询摘要":"sample query","结果哈希":"abc123","缓存命中":"5","过期时间":"2027-01-01","存储大小":"100","创建时间":"2026-11-01","最后命中":"2026-11-01"})],
    "data_contract": [_seed(1, {"契约编号":"DATA-0001","数据集":"analytics","Schema约束":"id,name,value","SLA定义":"每小时更新","更新频率":"3600","负责人":"admin","消费者":"dashboard","生效时间":"2026-11-01"})],
    "ab_config": [_seed(1, {"实验编号":"AB_C-0001","实验名称":"实验配置样例","分流比例":"50:50","指标集合":"revenue","对照组":"baseline","实验组":"variant","置信水平":"0.95","运行期间":"2026-11"})],
    # ===== 月度扫描结算链路 =====
    # 结算口径可以随上线版本演进；历史月结批次与报告保留各自的口径版本。
    "settlement_caliber": [
        {**_seed(1, {"版本编号":"CAL-2026.09-v1","版本序号":"v1.0","生效月份":"2026-09","失效月份":"2026-11","扫描行数系数":"1.0","计入口径":"按查询审计扫描行数全额计入","舍入规则":"round","发布人":"finance_admin","发布时间":"2026-09-01 00:00:00"}), "status":"已生效","pending":False},
        {**_seed(2, {"版本编号":"CAL-2026.11-v2","版本序号":"v1.1","生效月份":"2026-11","失效月份":"","扫描行数系数":"1.0","计入口径":"按查询审计扫描行数全额计入；迟到审计沿用业务月口径","舍入规则":"round","发布人":"finance_admin","发布时间":"2026-11-01 00:00:00"}), "status":"已生效","pending":False},
    ],
    "settlement_batch": [],
    "settlement_detail": [],
    "settlement_report": [],
    "settlement_staging": [],
    "settlement_reconcile": [],
}