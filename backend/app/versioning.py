"""版本化通用工具：输入摘要与版本命名。

独立于 store/service，避免 seed 与 service 之间的循环导入。
"""
from __future__ import annotations

import hashlib
from typing import Any


def payload_hash(dimension_code: str, members: list[dict[str, Any]]) -> str:
    """输入摘要：维度 + 每个成员的「编码->父级编码」有序拼接后取哈希。

    同一份清洗结果无论提交次序如何，摘要都相同，用于幂等拦截重复写入。
    """
    basis = [dimension_code, *[
        f"{m['成员编码']}->{m.get('父级编码', '')}"
        for m in sorted(members, key=lambda x: x["成员编码"])
    ]]
    return hashlib.sha256("|".join(basis).encode("utf-8")).hexdigest()[:16]


def version_name(dimension_code: str, seq: int) -> str:
    """映射版本命名：内置区域维度沿用 REGION 系列，其他维度按自身编号派生。"""
    if dimension_code == "DIME-0001":
        return f"MAP-REGION-{seq:04d}"
    return f"MAP-{dimension_code}-{seq:04d}"
