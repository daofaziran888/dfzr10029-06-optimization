"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

维度成员映射相关的三张表（dimension_member_mapping / dataset_refresh /
dimension_refresh_checkpoint）通过一把可重入锁保护：映射版本写入与页面读取必须共享
同一提交边界，避免数据集快速连续刷新时页面读到新旧父级混合的中间状态。
"""
from __future__ import annotations

import threading
from typing import Any

from app.seed import EXTRA_SEED_ROWS, SEED_ROWS

# 维度成员映射版本化的内部存储表：参与读写与加锁，但不算前端业务模块，
# 不进入运营概览的业务模块计数。
INTERNAL_TABLES = frozenset({
    "dimension_member_mapping",
    "dataset_refresh",
    "dimension_refresh_checkpoint",
})


class Store:
    def __init__(self) -> None:
        # 可重入锁：服务层一次刷新要在同一个临界区内完成「读-合并-写对照文件」，
        # 重入保证服务内部嵌套调用不会自锁。
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows]
            for name, rows in {**SEED_ROWS, **EXTRA_SEED_ROWS}.items()
        }

    @property
    def lock(self) -> threading.RLock:
        """暴露提交边界锁，服务层用 with store.lock: 保证一次刷新原子可见。"""
        return self._lock

    def module_names(self) -> list[str]:
        """只暴露前端业务模块；内部版本化存储表不参与运营概览与健康检查计数。"""
        return sorted(name for name in self._tables if name not in INTERNAL_TABLES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        """统一取号，避免并发刷新时两条记录拿到同一个 id（重复写入的一个来源）。"""
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def snapshot(self, module: str) -> list[dict[str, Any]]:
        """在锁内拷贝一份只读快照，页面读取拿到的永远是某个完整提交边界上的数据。"""
        with self._lock:
            return [dict(row) for row in self.rows(module)]

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
