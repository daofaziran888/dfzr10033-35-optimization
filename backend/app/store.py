"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from bisect import bisect_right
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._ordered: dict[str, dict[str, Any]] = {}

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])


    def rows_after(self, module: str, cursor: int) -> list[dict[str, Any]]:
        """按主键游标读取增量行，避免月度结算每次重扫全量审计。

        业务表是只追加的（新行 id 单调递增），这里为每个模块缓存一份
        有序 id 视图；追加发生时只扩展尾部，之后的游标读取是二分查找。
        """
        rows = self.rows(module)
        cache = self._ordered.setdefault(module, {"len": 0, "ids": [], "rows": []})
        if len(rows) < cache["len"]:
            # 仓库被整体替换（测试或重载），重建缓存
            cache = {"len": 0, "ids": [], "rows": []}
            self._ordered[module] = cache
        if len(rows) != cache["len"]:
            tail = rows[cache["len"]:]
            ids = [int(row.get("id", 0)) for row in tail]
            cache["ids"].extend(ids)
            cache["rows"].extend(tail)
            if any(a > b for a, b in zip(cache["ids"], cache["ids"][1:])):
                paired = sorted(zip(cache["ids"], cache["rows"]), key=lambda item: item[0])
                cache["ids"] = [item[0] for item in paired]
                cache["rows"] = [item[1] for item in paired]
            cache["len"] = len(rows)
        index = bisect_right(cache["ids"], cursor)
        return [row for row in cache["rows"][index:] if int(row.get("id", 0)) > cursor]

    def reset(self, module: str | None = None) -> None:
        """把模块恢复到种子状态，主要供测试和本地演示重放使用。"""
        names = [module] if module else list(SEED_ROWS)
        for name in names:
            self._tables[name] = [dict(row) for row in SEED_ROWS.get(name, [])]
        self.invalidate_cursor_cache(module)

    def invalidate_cursor_cache(self, module: str | None = None) -> None:
        """数据结构被原地重排时由调用方显式失效游标缓存。"""
        if module is None:
            self._ordered.clear()
        else:
            self._ordered.pop(module, None)

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
