"""维度成员映射版本化改造的全链路验证（服务层 + 并发）。

运行：cd /workspace/backend && .venv/bin/python -I tests/verify_mapping.py
"""
from __future__ import annotations

import sys
import threading

sys.path.insert(0, "/workspace/backend")

from app.services.dimension_mapping import service  # noqa: E402
from app.store import store  # noqa: E402

DIM = "DIME-0001"
DS = "DATA-0003"
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" -> {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def effective_rows():
    return [r for r in store.rows("dimension_member_mapping") if r["status"] == "生效"]


def member_map():
    return {r["成员编码"]: r for r in effective_rows()}


def full_members():
    """当前生效成员的完整输入（用于幂等提交）。"""
    return [{"成员编码": r["成员编码"], "成员名称": r["成员名称"], "父级编码": r["父级编码"]}
            for r in sorted(effective_rows(), key=lambda r: r["成员编码"])]


# ---------- 1. 初始种子状态 ----------
rows = effective_rows()
check("初始生效成员数为 10", len(rows) == 10, str(len(rows)))
m = member_map()
check("M009 深圳当前归属广东(版本0002调整)", m["M009"]["父级名称"] == "广东" and
      m["M009"]["最后更新版本"] == "MAP-REGION-0002")
check("M010 苏州当前归属江苏(版本0003新增)", m["M010"]["父级名称"] == "江苏" and
      m["M010"]["首次写入版本"] == "MAP-REGION-0003")
check("成员编码无重复有效行", len({r["成员编码"] for r in rows}) == len(rows))

# ---------- 2. 幂等：同输入重复提交不产生新版本 ----------
refreshes_before = len(store.rows("dataset_refresh"))
result = service.refresh_dimension(
    dimension_code=DIM, dimension_name="区域维度", dataset_code=DS,
    dataset_name="区域销售数据集", raw_members=full_members())
check("相同输入重复刷新判定为幂等", result.get("idempotent") is True, result.get("message", ""))
check("幂等刷新不新增刷新记录", len(store.rows("dataset_refresh")) == refreshes_before)
check("幂等刷新不新增对照文件",
      len(store.rows("dimension_refresh_checkpoint")) == 3)

# ---------- 3. 新版本 v4：成员级差异合并（不整表替换、不重复写入） ----------
v4_input = full_members()
for item in v4_input:
    if item["成员编码"] == "M010":
        item["父级编码"] = "M001"  # 苏州：江苏 -> 华东大区
v4_input.append({"成员编码": "M011", "成员名称": "宁波", "父级编码": "M004"})  # 新增
v4_input.append({"成员编码": "", "成员名称": "脏行", "父级编码": "M001"})  # 应被清洗丢弃
v4_input.append(dict(v4_input[0]))  # 重复编码，应去重

result = service.refresh_dimension(
    dimension_code=DIM, dimension_name="区域维度", dataset_code=DS,
    dataset_name="区域销售数据集", raw_members=v4_input)
check("v4 刷新成功", result.get("ok") is True and result.get("映射版本") == "MAP-REGION-0004",
      result.get("message", ""))
m = member_map()
check("M010 苏州归属切到华东大区", m["M010"]["父级名称"] == "华东大区")
check("M010 首次写入版本保留为0003", m["M010"]["首次写入版本"] == "MAP-REGION-0003")
check("M010 最后更新版本推进到0004", m["M010"]["最后更新版本"] == "MAP-REGION-0004")
check("M011 宁波新增且归属浙江", m["M011"]["父级名称"] == "浙江" and
      m["M011"]["首次写入版本"] == "MAP-REGION-0004")
check("未变化成员不重复写入(M001仍停留在0001)", member_map()["M001"]["最后更新版本"] == "MAP-REGION-0001")
diffs = result["对照文件"]["对照明细"]
check("v4 对照明细只含 2 条变更(1父级变更+1新增)",
      len(diffs) == 2 and {d["变更类型"] for d in diffs} == {"父级变更", "新增"}, str(diffs))
check("脏行与重复行被清洗，生效成员数为 11", len(effective_rows()) == 11)
check("成员编码仍无重复有效行",
      len({r["成员编码"] for r in effective_rows()}) == len(effective_rows()))

# ---------- 4. 读取一致性：快照永远对应完整提交边界 ----------
snap1 = service.list_mappings(DIM)
snap2 = service.list_mappings(DIM)
check("两次快照读取结果一致",
      [(r["成员编码"], r["父级编码"], r["映射版本"]) for r in snap1] ==
      [(r["成员编码"], r["父级编码"], r["映射版本"]) for r in snap2])

# ---------- 5. 并发：连续快速刷新只落有效版本，冲突进待核对 ----------
barrier = threading.Barrier(8)
outcomes: list[dict] = []
outcomes_lock = threading.Lock()


def worker(payload, expected, label):
    barrier.wait()  # 最大化竞争：8 个线程同时进入临界区入口
    res = service.refresh_dimension(
        dimension_code=DIM, dimension_name="区域维度", dataset_code=DS,
        dataset_name="区域销售数据集", raw_members=payload,
        expected_previous_version=expected)
    res["_label"] = label
    with outcomes_lock:
        outcomes.append(res)


# 3 个线程带过期前置版本（期望 0001，实际已到 0004）-> 边界校验优先，必须全部冲突
stale_payload = full_members()
threads = [threading.Thread(target=worker, args=(stale_payload, "MAP-REGION-0001", "stale"))
           for _ in range(3)]
# 3 个线程无条件提交同一份新层级 -> 锁内串行，首个生效，其余幂等
new_payload = full_members()
for item in new_payload:
    if item["成员编码"] == "M011":
        item["父级编码"] = "M001"  # 宁波改为挂华东大区
threads += [threading.Thread(target=worker, args=(new_payload, None, "unconditional"))
            for _ in range(3)]
# 2 个线程带正确前置版本 0004 提交同样的新层级 -> 仅抢在边界推进前的那个生效，其余冲突
threads += [threading.Thread(target=worker, args=(new_payload, "MAP-REGION-0004", "fresh"))
            for _ in range(2)]
for t in threads:
    t.start()
for t in threads:
    t.join()

def labeled(label):
    return [o for o in outcomes if o["_label"] == label]

stale_results = labeled("stale")
check("3 个过期前置版本全部判冲突（边界校验优先于幂等）",
      len(stale_results) == 3 and all(o.get("conflict") for o in stale_results),
      f"conflicts={sum(o.get('conflict') for o in stale_results)}")
uncond_results = labeled("unconditional")
check("无条件提交绝不产生冲突，且最多 1 个生效、其余幂等（与锁获取顺序无关）",
      all(not o.get("conflict") for o in uncond_results) and
      sum(o.get("ok") and not o.get("idempotent") for o in uncond_results) <= 1 and
      all(o.get("idempotent") or (o.get("ok") and not o.get("idempotent")) for o in uncond_results))
fresh_results = labeled("fresh")
check("带正确前置版本的线程：生效与冲突互斥，无静默重复写入",
      all((o.get("conflict") or (o.get("ok") and not o.get("idempotent"))) for o in fresh_results))

conflicts = [o for o in outcomes if o.get("conflict")]
successes = [o for o in outcomes if o.get("ok") and not o.get("idempotent")]
idempotents = [o for o in outcomes if o.get("idempotent")]
check("整批并发只落 1 个有效版本", len(successes) == 1,
      f"success={len(successes)}, idempotent={len(idempotents)}, conflict={len(conflicts)}")
check("幂等拦截 + 冲突拦截覆盖其余全部请求",
      len(idempotents) + len(conflicts) == len(outcomes) - 1)
m = member_map()
check("并发后 M011 归属唯一且为华东大区", m["M011"]["父级名称"] == "华东大区")
check("并发后无重复有效成员行",
      len({r["成员编码"] for r in effective_rows()}) == len(effective_rows()))
versions = {r["映射版本"] for r in effective_rows()}
head = service.list_refresh_records(DS)[-1]["映射版本"]
check("所有读取到的成员归属都落在已提交版本上", all(v <= head for v in versions))
pending_cps = [c for c in service.list_checkpoints(DIM) if c["对照状态"] == "待核对"]
check("冲突结果全部落为待核对对照文件", len(pending_cps) == len(conflicts),
      f"pending={len(pending_cps)}, conflict={len(conflicts)}")

# 冲突文件核对 -> 必须驳回，不能变成有效版本
cp_id = pending_cps[0]["id"]
verify_res = service.verify_checkpoint(cp_id)
check("冲突对照文件核对被驳回", not verify_res["ok"] and
      verify_res["对照文件"]["对照状态"] == "已驳回", verify_res.get("message", ""))

# ---------- 6. 分析接口消费并持久化 ----------
head_cp = service.list_checkpoints(DIM)[-1]
analysis = service.analyze(head_cp["id"], "维度分析接口")
check("分析消费返回提交边界与版本", analysis["ok"] and analysis["提交边界标识"].startswith("COMMIT-"))
reloaded = service.get_checkpoint(head_cp["id"])
check("消费事实持久化，重新读取仍可回查", bool(reloaded["分析消费时间"]) and
      reloaded["分析消费方"] == "维度分析接口", reloaded.get("分析消费时间", ""))

# ---------- 7. 回退到历史版本 0002 ----------
rb = service.rollback("MAP-REGION-0002")
check("回退成功并产生新版本", rb["ok"] and rb["回退自版本"] == "MAP-REGION-0002",
      rb.get("message", ""))
m = member_map()
check("回退后苏州(M010)已失效", "M010" not in m and
      next(r for r in store.rows("dimension_member_mapping") if r["成员编码"] == "M010")["status"] == "已停用")
check("回退后深圳归属仍为广东(与0002一致)", m["M009"]["父级名称"] == "广东")
check("回退后生效成员数恢复为 9", len(effective_rows()) == 9, str(len(effective_rows())))
rb_cp = rb["对照文件"]
check("回退文件沿用同一来源段可追溯",
      rb_cp["来源输入"].startswith("[回退]") and "REF-2026-0002" in rb_cp["来源输入"])
check("回退文件自身标记为已核对", rb_cp["对照状态"] == "已核对")
# 回退后再次读取，成员归属稳定（只对比生效成员）
reloaded_effective = {r["成员编码"]: r for r in service.list_mappings(DIM) if r["status"] == "生效"}
check("回退后重新读取成员归属保持不变",
      all(r["父级名称"] == reloaded_effective[code]["父级名称"] for code, r in m.items()))

print()
if failures:
    print(f"共 {len(failures)} 项失败：{failures}")
    sys.exit(1)
print("全部验证通过")
