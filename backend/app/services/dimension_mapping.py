"""维度成员映射版本化服务。

处理链路（与需求逐条对应）：

1. 清洗现有输入：规整字符串、丢弃缺成员编码的脏行、对入参自身重复编码去重；
2. 按映射版本稳定更新：以「维度 + 成员编码」为键做成员级差异合并，只写新增/父级
   变更/失效的成员，未变化成员不重复写入，不做整表替换；
3. 同一提交边界：映射写入、刷新记录、维度刷新对照文件在 ``with store.lock`` 一个
   临界区内完成，页面读取走 ``store.snapshot``，持久化前后不会出现新旧父级混合；
4. 并发只落一个有效版本：版本序号严格递增 + 输入摘要幂等 + 前置版本乐观校验，
   冲突不生效，落「待核对」对照文件等待人工核对；
5. 对照文件保留来源输入（原文 + 快照 + 提交边界标识），支持主版本边界核对、
   分析接口消费持久化以及按历史版本回退，回退文件沿用同一来源段可追溯。
"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from app.store import store
from app.versioning import payload_hash, version_name

MAPPING_MODULE = "dimension_member_mapping"
REFRESH_MODULE = "dataset_refresh"
CHECKPOINT_MODULE = "dimension_refresh_checkpoint"

# 对照文件状态机：待核对 -> 已核对 / 已驳回；回退文件生成后直接进入已核对。
CP_PENDING = "待核对"
CP_VERIFIED = "已核对"
CP_REJECTED = "已驳回"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def clean_members(raw_members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """清洗入参：补默认字段、去空白、丢脏行、按成员编码稳定去重（保留首次出现）。"""
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in raw_members or []:
        code = _clean_text(row.get("成员编码"))
        if not code:
            # 没有成员编码的行无法参与版本合并，直接丢弃而不是写成脏数据
            continue
        if code in seen:
            # 同一次提交里的重复行视为重复写入，忽略后续副本，保持既有数据次序
            continue
        seen.add(code)
        cleaned.append({
            "成员编码": code,
            "成员名称": _clean_text(row.get("成员名称")) or code,
            "父级编码": _clean_text(row.get("父级编码")),
        })
    return cleaned


def _resolve_hierarchy(members: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """补全父级名称与层级；父级缺失时按根节点处理，层级沿父链向上计算。"""
    by_code = {m["成员编码"]: dict(m) for m in members}
    for member in by_code.values():
        member.setdefault("父级名称", "")
    for member in by_code.values():
        parent_code = member["父级编码"]
        member["父级名称"] = by_code.get(parent_code, {}).get("成员名称", "") if parent_code else ""

    def level_of(code: str, trail: set[str]) -> int:
        parent = by_code.get(code, {}).get("父级编码", "")
        if not parent or parent not in by_code or parent in trail:
            return 1
        return 1 + level_of(parent, trail | {code})

    for code, member in by_code.items():
        member["层级"] = level_of(code, set())
    return by_code


def _payload_hash(dimension_code: str, members: list[dict[str, Any]]) -> str:
    """输入摘要：同一份清洗结果重复提交时幂等，不产生第二个有效版本。"""
    return payload_hash(dimension_code, members)


def _effective_mappings(dimension_code: str) -> list[dict[str, Any]]:
    return [
        row for row in store.rows(MAPPING_MODULE)
        if row.get("维度编号") == dimension_code and row.get("status") == "生效"
    ]


def _head_refresh(dataset_code: str) -> dict[str, Any] | None:
    records = [r for r in store.rows(REFRESH_MODULE) if r.get("数据集编号") == dataset_code]
    return max(records, key=lambda r: int(r.get("刷新序号", 0)), default=None)


def _head_checkpoint(dimension_code: str) -> dict[str, Any] | None:
    items = [c for c in store.rows(CHECKPOINT_MODULE) if c.get("维度编号") == dimension_code]
    return max(items, key=lambda c: int(c.get("主版本序号", 0)), default=None)


class DimensionMappingService:
    # ------------------------------------------------------------------ 查询
    def list_mappings(self, dimension_code: str | None = None) -> list[dict[str, Any]]:
        # 快照在锁内拷贝：页面任何一次读取都对应一个完整提交边界
        rows = store.snapshot(MAPPING_MODULE)
        if dimension_code:
            rows = [row for row in rows if row.get("维度编号") == dimension_code]
        return sorted(rows, key=lambda r: (str(r.get("维度编号")), int(r.get("层级", 0)), str(r.get("成员编码"))))

    def list_refresh_records(self, dataset_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.snapshot(REFRESH_MODULE)
        if dataset_code:
            rows = [row for row in rows if row.get("数据集编号") == dataset_code]
        return sorted(rows, key=lambda r: int(r.get("刷新序号", 0)))

    def list_checkpoints(self, dimension_code: str | None = None,
                         state: str | None = None) -> list[dict[str, Any]]:
        rows = store.snapshot(CHECKPOINT_MODULE)
        if dimension_code:
            rows = [row for row in rows if row.get("维度编号") == dimension_code]
        if state:
            rows = [row for row in rows if row.get("对照状态") == state]
        return sorted(rows, key=lambda c: int(c.get("主版本序号", 0)))

    def get_checkpoint(self, checkpoint_id: int) -> dict[str, Any] | None:
        return store.find(CHECKPOINT_MODULE, checkpoint_id)

    # ------------------------------------------------------------------ 刷新主链路
    def refresh_dimension(
        self,
        *,
        dimension_code: str,
        dimension_name: str,
        dataset_code: str,
        dataset_name: str,
        raw_members: list[dict[str, Any]],
        expected_previous_version: str | None = None,
        operator: str = "维度分析接口",
    ) -> dict[str, Any]:
        """数据集刷新后按映射版本稳定更新维度成员，返回刷新结果与对照文件。

        所有写操作共享同一把锁、同一个提交边界标识。
        """
        dimension_code = _clean_text(dimension_code)
        dataset_code = _clean_text(dataset_code)
        # 前端留空输入会传空字符串：空串等价于「不校验前置版本」，不能当成显式版本号
        expected_previous_version = _clean_text(expected_previous_version) or None
        members = clean_members(raw_members)
        if not dimension_code or not dataset_code:
            return {"ok": False, "message": "维度编号与数据集编号不能为空", "conflict": False}
        if not members:
            return {"ok": False, "message": "清洗后没有有效成员输入，本次刷新未写入任何数据", "conflict": False}

        incoming = _resolve_hierarchy(members)
        digest = _payload_hash(dimension_code, members)

        with store.lock:
            head_refresh = _head_refresh(dataset_code)
            head_version = str(head_refresh.get("映射版本", "")) if head_refresh else ""

            # 乐观并发控制优先：调用方显式声明了前置版本时，边界不匹配必须先判冲突，
            # 不能让随后的幂等判断把并发冲突短路成「跳过写入」。
            if expected_previous_version is not None and expected_previous_version != head_version:
                conflict = self._write_conflict(
                    dimension_code=dimension_code, dataset_code=dataset_code,
                    dataset_name=dataset_name, head_version=head_version,
                    expected=expected_previous_version, digest=digest,
                    incoming=incoming, operator=operator)
                return {
                    "ok": False, "idempotent": False, "conflict": True,
                    "message": "刷新并发导致前置版本不匹配，冲突结果已写入对照文件等待核对",
                    "冲突对照文件编号": conflict["对照文件编号"],
                }

            # 幂等：同输入摘要已经落在当前版本上，直接返回既有结果，绝不重复写入
            if head_refresh and head_refresh.get("输入摘要") == digest:
                checkpoint = _head_checkpoint(dimension_code)
                return {
                    "ok": True, "idempotent": True, "conflict": False,
                    "message": "输入与当前生效版本一致，跳过重复写入",
                    "映射版本": head_version,
                    "提交边界标识": head_refresh.get("提交边界标识"),
                    "对照文件": checkpoint,
                }

            next_seq = int(head_refresh.get("刷新序号", 0)) + 1 if head_refresh else 1
            new_version = version_name(dimension_code, next_seq)
            commit_id = f"COMMIT-{new_version}"
            now = _now()

            existing = {row["成员编码"]: row for row in _effective_mappings(dimension_code)}
            diffs: list[dict[str, Any]] = []
            snapshot_rows: list[dict[str, Any]] = []

            for code, incoming_member in sorted(incoming.items()):
                old = existing.get(code)
                parent_code = incoming_member["父级编码"]
                parent_name = incoming_member["父级名称"]
                level = incoming_member["层级"]
                if old is None:
                    row = {
                        "id": store.next_id(MAPPING_MODULE), "status": "生效",
                        "pending": False, "abnormal": False,
                        "维度编号": dimension_code, "维度名称": dimension_name,
                        "成员编码": code, "成员名称": incoming_member["成员名称"],
                        "父级编码": parent_code, "父级名称": parent_name, "层级": level,
                        "来源数据集": dataset_code, "映射版本": new_version,
                        "首次写入版本": new_version, "最后更新版本": new_version, "提交时间": now,
                    }
                    store.rows(MAPPING_MODULE).append(row)
                    diffs.append(self._diff_row(code, incoming_member["成员名称"], "—",
                                                parent_name or "（根节点）", "新增"))
                elif old.get("父级编码") != parent_code:
                    before = old.get("父级名称") or "（根节点）"
                    old["父级编码"] = parent_code
                    old["父级名称"] = parent_name
                    old["层级"] = level
                    old["映射版本"] = new_version
                    old["最后更新版本"] = new_version
                    old["提交时间"] = now
                    diffs.append(self._diff_row(code, incoming_member["成员名称"], before,
                                                parent_name or "（根节点）", "父级变更"))
                # 未变化成员：不动行、不换版本号，避免重复写入
                snapshot_rows.append(code)

            # 输入中消失的成员不物理删除：标记失效并保留版本痕迹，保证可回退/可追溯
            for code, old in sorted(existing.items()):
                if code not in incoming and old.get("status") == "生效":
                    old["status"] = "已停用"
                    old["pending"] = True
                    old["映射版本"] = new_version
                    old["最后更新版本"] = new_version
                    old["提交时间"] = now
                    diffs.append(self._diff_row(code, old.get("成员名称", code),
                                                old.get("父级名称") or "（根节点）", "—", "失效"))

            effective = _effective_mappings(dimension_code)
            version_snapshot = [self._snapshot_member(row) for row in
                                sorted(effective, key=lambda r: (int(r.get("层级", 0)), r["成员编码"]))]

            refresh_record = {
                "id": store.next_id(REFRESH_MODULE), "status": "已生效",
                "pending": False, "abnormal": False,
                "刷新记录编号": f"REF-2026-{next_seq:04d}",
                "数据集编号": dataset_code, "数据集名称": dataset_name,
                "刷新序号": next_seq, "映射版本": new_version,
                "刷新前版本": head_version, "刷新状态": "当前版本",
                "刷新时间": now, "成员数": len(effective),
                "提交边界标识": commit_id, "输入摘要": digest,
                "变更摘要": f"新增 {sum(d['变更类型'] == '新增' for d in diffs)} 个、"
                            f"父级变更 {sum(d['变更类型'] == '父级变更' for d in diffs)} 个、"
                            f"失效 {sum(d['变更类型'] == '失效' for d in diffs)} 个",
            }
            if head_refresh is not None:
                head_refresh["刷新状态"] = "历史版本"
            store.rows(REFRESH_MODULE).append(refresh_record)

            checkpoint = {
                "id": store.next_id(CHECKPOINT_MODULE), "status": "已核对",
                "pending": False, "abnormal": False,
                "对照文件编号": f"CHK-2026-{next_seq:04d}",
                "刷新记录编号": refresh_record["刷新记录编号"],
                "维度编号": dimension_code, "映射版本": new_version,
                "主版本序号": next_seq,
                "来源输入": f"维度成员映射-{head_version or '空'} + 关联数据集刷新记录 {refresh_record['刷新记录编号']}",
                "来源输入快照": [self._snapshot_member(m) for m in
                              sorted(members, key=lambda x: x["成员编码"])],
                "版本成员快照": version_snapshot,
                "提交边界标识": commit_id, "对照状态": CP_VERIFIED,
                "生成时间": now, "核对时间": now, "回退自版本": "",
                "成员数": len(effective), "对照明细": diffs,
                "输入摘要": digest, "回退文件": False,
                "分析消费时间": "", "分析消费方": "",
            }
            store.rows(CHECKPOINT_MODULE).append(checkpoint)

            return {
                "ok": True, "idempotent": False, "conflict": False,
                "message": f"映射版本 {new_version} 已在提交边界 {commit_id} 内稳定更新",
                "映射版本": new_version, "提交边界标识": commit_id,
                "变更数": len(diffs), "对照文件": checkpoint, "刷新记录": refresh_record,
            }

    def _write_conflict(self, *, dimension_code: str, dataset_code: str, dataset_name: str,
                        head_version: str, expected: str, digest: str,
                        incoming: dict[str, dict[str, Any]], operator: str) -> dict[str, Any]:
        """冲突不允许改映射：只追加一份「待核对」对照文件，等核对主版本边界后处理。"""
        seq = store.next_id(CHECKPOINT_MODULE)
        now = _now()
        conflict = {
            "id": seq, "status": "待核对", "pending": True, "abnormal": True,
            "对照文件编号": f"CHK-CONFLICT-{seq:04d}",
            "刷新记录编号": "", "维度编号": dimension_code,
            "映射版本": f"冲突(期望前置 {expected or '空'} / 实际主版本 {head_version or '空'})",
            "主版本序号": 0,
            "来源输入": f"并发刷新冲突输入，数据集 {dataset_code}（{dataset_name}），操作方 {operator}",
            "来源输入快照": [
                {"成员编码": m["成员编码"], "成员名称": m["成员名称"],
                 "父级编码": m["父级编码"], "父级名称": m["父级名称"], "层级": m["层级"]}
                for m in sorted(incoming.values(), key=lambda x: x["成员编码"])
            ],
            "版本成员快照": [], "提交边界标识": "",
            "对照状态": CP_PENDING, "生成时间": now, "核对时间": "",
            "回退自版本": "", "成员数": len(incoming),
            "对照明细": [], "输入摘要": digest, "回退文件": False,
            "分析消费时间": "", "分析消费方": "",
        }
        store.rows(CHECKPOINT_MODULE).append(conflict)
        return conflict

    # ------------------------------------------------------------------ 核对
    def verify_checkpoint(self, checkpoint_id: int) -> dict[str, Any]:
        """核对主版本边界：提交边界标识、版本序号、来源快照与对照明细的一致性。"""
        with store.lock:
            checkpoint = store.find(CHECKPOINT_MODULE, checkpoint_id)
            if checkpoint is None:
                return {"ok": False, "message": f"对照文件 {checkpoint_id} 不存在"}
            if checkpoint.get("对照状态") != CP_PENDING:
                return {"ok": True, "message": f"对照文件已处于{checkpoint.get('对照状态')}，无需重复核对",
                        "对照文件": checkpoint}

            problems: list[str] = []
            snapshot = checkpoint.get("来源输入快照", [])
            if len({m.get("成员编码") for m in snapshot}) != len(snapshot):
                problems.append("来源输入快照存在重复成员编码")
            for detail in checkpoint.get("对照明细", []):
                matched = next((m for m in snapshot if m.get("成员编码") == detail.get("成员编码")), None)
                if matched is None:
                    problems.append(f"成员 {detail.get('成员编码')} 在来源快照中缺失")
                elif matched.get("父级名称", "") and matched.get("父级名称") != detail.get("刷新后父级"):
                    problems.append(
                        f"成员 {detail.get('成员编码')} 父级与来源快照不一致："
                        f"{detail.get('刷新后父级')} != {matched.get('父级名称')}")
            # 冲突文件没有主版本序号，核对结论只能是驳回（不能让冲突版本变成有效版本）
            if int(checkpoint.get("主版本序号", 0)) <= 0:
                problems.append("不存在有效主版本序号，属于并发冲突输入")

            now = _now()
            if problems:
                checkpoint["对照状态"] = CP_REJECTED
                checkpoint["status"] = "已驳回"
                checkpoint["abnormal"] = True
                checkpoint["核对时间"] = now
                return {"ok": False, "message": "核对未通过：" + "；".join(problems), "对照文件": checkpoint}
            checkpoint["对照状态"] = CP_VERIFIED
            checkpoint["status"] = "已核对"
            checkpoint["pending"] = False
            checkpoint["abnormal"] = False
            checkpoint["核对时间"] = now
            return {"ok": True, "message": "主版本边界核对通过", "对照文件": checkpoint}

    # ------------------------------------------------------------------ 分析消费
    def analyze(self, checkpoint_id: int | None, consumer: str) -> dict[str, Any]:
        """维度分析接口消费对照文件，并把消费事实持久化回对照文件（刷新页面仍可回查）。"""
        with store.lock:
            checkpoint = (store.find(CHECKPOINT_MODULE, checkpoint_id)
                          if checkpoint_id is not None else _head_checkpoint("DIME-0001"))
            if checkpoint is None:
                return {"ok": False, "message": "没有可消费的维度刷新对照文件"}
            now = _now()
            checkpoint["分析消费时间"] = now
            checkpoint["分析消费方"] = consumer or "维度分析接口"
            snapshot = checkpoint.get("版本成员快照") or checkpoint.get("来源输入快照", [])
            level_counts: dict[str, int] = {}
            for member in snapshot:
                key = f"L{member.get('层级', '?')}"
                level_counts[key] = level_counts.get(key, 0) + 1
            return {
                "ok": True, "对照文件编号": checkpoint.get("对照文件编号"),
                "映射版本": checkpoint.get("映射版本"),
                "提交边界标识": checkpoint.get("提交边界标识"),
                "成员数": len(snapshot), "层级分布": level_counts,
                "变更明细": checkpoint.get("对照明细", []),
                "消费持久化时间": now, "消费方": checkpoint["分析消费方"],
            }

    # ------------------------------------------------------------------ 回退
    def rollback(self, target_version: str, operator: str = "维度分析接口") -> dict[str, Any]:
        """按历史映射版本回退：用该版本对照文件里的完整成员快照重建当前映射。

        回退产生新版本与新对照文件，但 ``来源输入`` 沿用目标版本同一段来源，
        重新打开对照文件即可追溯到同一段来源输入。
        """
        target_version = _clean_text(target_version)
        with store.lock:
            target = next((c for c in store.rows(CHECKPOINT_MODULE)
                           if c.get("映射版本") == target_version
                           and c.get("对照状态") == CP_VERIFIED), None)
            if target is None:
                return {"ok": False, "message": f"未找到映射版本 {target_version} 的已核对对照文件"}
            target_snapshot = target.get("版本成员快照")
            if not target_snapshot:
                return {"ok": False, "message": "目标版本缺少完整成员快照，无法回退"}

            dimension_code = target["维度编号"]
            dataset_code = next((r.get("数据集编号", "") for r in store.rows(REFRESH_MODULE)
                                 if r.get("映射版本") == target_version), "")
            dataset_name = next((r.get("数据集名称", "") for r in store.rows(REFRESH_MODULE)
                                 if r.get("映射版本") == target_version), "")
            head_refresh = _head_refresh(dataset_code)
            next_seq = int(head_refresh.get("刷新序号", 0)) + 1 if head_refresh else 1
            new_version = version_name(dimension_code, next_seq)
            commit_id = f"COMMIT-{new_version}"
            now = _now()

            current = {row["成员编码"]: row for row in _effective_mappings(dimension_code)}
            wanted = {m["成员编码"]: m for m in target_snapshot}
            diffs: list[dict[str, Any]] = []

            # 先把当前生效但目标版本不存在的成员置失效
            for code, row in sorted(current.items()):
                if code not in wanted:
                    row["status"] = "已停用"
                    row["pending"] = True
                    row["映射版本"] = new_version
                    row["最后更新版本"] = new_version
                    row["提交时间"] = now
                    diffs.append(self._diff_row(code, row.get("成员名称", code),
                                                row.get("父级名称") or "（根节点）", "—", "失效"))
            # 再按目标快照恢复/新增成员，保留首次写入版本，延续版本血缘
            for member in sorted(target_snapshot, key=lambda m: (int(m.get("层级", 0)), m["成员编码"])):
                code = member["成员编码"]
                row = current.get(code)
                if row is None:
                    row = {
                        "id": store.next_id(MAPPING_MODULE), "status": "生效",
                        "pending": False, "abnormal": False,
                        "维度编号": dimension_code,
                        "维度名称": next((m.get("维度名称", "") for m in target_snapshot
                                          if m.get("成员编码") == code), ""),
                        "成员编码": code, "成员名称": member["成员名称"],
                        "父级编码": member["父级编码"], "父级名称": member["父级名称"],
                        "层级": member["层级"], "来源数据集": dataset_code,
                        "映射版本": new_version, "首次写入版本": new_version,
                        "最后更新版本": new_version, "提交时间": now,
                    }
                    store.rows(MAPPING_MODULE).append(row)
                    diffs.append(self._diff_row(code, member["成员名称"], "—",
                                                member["父级名称"] or "（根节点）", "新增"))
                else:
                    before = row.get("父级名称") or "（根节点）"
                    changed = (row.get("父级编码") != member["父级编码"]
                               or row.get("status") != "生效")
                    row["status"] = "生效"
                    row["pending"] = False
                    row["父级编码"] = member["父级编码"]
                    row["父级名称"] = member["父级名称"]
                    row["层级"] = member["层级"]
                    row["映射版本"] = new_version
                    row["提交时间"] = now
                    if changed:
                        row["最后更新版本"] = new_version
                        diffs.append(self._diff_row(code, member["成员名称"], before,
                                                    member["父级名称"] or "（根节点）", "父级变更"))

            effective = _effective_mappings(dimension_code)
            version_snapshot = [self._snapshot_member(row) for row in
                                sorted(effective, key=lambda r: (int(r.get("层级", 0)), r["成员编码"]))]
            if head_refresh is not None:
                head_refresh["刷新状态"] = "历史版本"
            refresh_record = {
                "id": store.next_id(REFRESH_MODULE), "status": "已生效",
                "pending": False, "abnormal": False,
                "刷新记录编号": f"REF-2026-{next_seq:04d}",
                "数据集编号": dataset_code, "数据集名称": dataset_name,
                "刷新序号": next_seq, "映射版本": new_version,
                "刷新前版本": head_refresh.get("映射版本", "") if head_refresh else "",
                "刷新状态": "当前版本", "刷新时间": now, "成员数": len(effective),
                "提交边界标识": commit_id, "输入摘要": "",
                "变更摘要": f"回退至 {target_version}，恢复其 {len(target_snapshot)} 个成员归属",
            }
            store.rows(REFRESH_MODULE).append(refresh_record)

            checkpoint = {
                "id": store.next_id(CHECKPOINT_MODULE), "status": "已核对",
                "pending": False, "abnormal": False,
                "对照文件编号": f"CHK-2026-{next_seq:04d}",
                "刷新记录编号": refresh_record["刷新记录编号"],
                "维度编号": dimension_code, "映射版本": new_version,
                "主版本序号": next_seq,
                # 回退文件沿用目标版本同一段来源输入，保证可追溯
                "来源输入": f"[回退] {target.get('来源输入', '')}",
                "来源输入快照": copy.deepcopy(target.get("来源输入快照", [])),
                "版本成员快照": version_snapshot,
                "提交边界标识": commit_id, "对照状态": CP_VERIFIED,
                "生成时间": now, "核对时间": now,
                "回退自版本": target_version, "成员数": len(effective),
                "对照明细": diffs, "输入摘要": "", "回退文件": True,
                "分析消费时间": "", "分析消费方": "",
            }
            store.rows(CHECKPOINT_MODULE).append(checkpoint)
            return {
                "ok": True, "message": f"已回退至 {target_version} 的成员归属，新版本 {new_version}",
                "映射版本": new_version, "提交边界标识": commit_id,
                "回退自版本": target_version, "对照文件": checkpoint,
            }

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _diff_row(code: str, name: str, before: str, after: str, kind: str) -> dict[str, str]:
        return {"成员编码": code, "成员名称": name,
                "刷新前父级": before, "刷新后父级": after, "变更类型": kind}

    @staticmethod
    def _snapshot_member(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "维度编号": row.get("维度编号", ""), "维度名称": row.get("维度名称", ""),
            "成员编码": row.get("成员编码", ""), "成员名称": row.get("成员名称", ""),
            "父级编码": row.get("父级编码", ""), "父级名称": row.get("父级名称", ""),
            "层级": row.get("层级", 0),
        }


service = DimensionMappingService()
