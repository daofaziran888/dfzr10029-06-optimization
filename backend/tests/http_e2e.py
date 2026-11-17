"""维度成员映射 HTTP 端到端验证（仅标准库，可对运行中的 uvicorn 直接打）。

运行：cd /workspace/backend && .venv/bin/python -I tests/http_e2e.py
"""
from __future__ import annotations

import csv
import io
import json
import sys
import threading
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def _encode_path(path: str) -> str:
    if "?" not in path:
        return path
    base, query = path.split("?", 1)
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    return f"{base}?{urllib.parse.urlencode(pairs)}"


def call(method: str, path: str, payload: object = None) -> tuple[int, object]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + _encode_path(path), data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:  # noqa: PERF203
        return exc.code, json.loads(exc.read().decode("utf-8"))


def get(path: str) -> object:
    status, body = call("GET", path)
    assert status == 200, (status, body)
    return body


# ---------- 回归：既有维度管理接口不受影响 ----------
dim_list = get("/api/dimension")
check("既有维度列表接口正常", dim_list["total"] == 3, str(dim_list["total"]))
dim_one = get("/api/dimension/1")
check("既有维度明细接口正常", dim_one["维度编号"] == "DIME-0001")

# ---------- 1. 读取成员映射与刷新记录 ----------
mappings = get("/api/dimension/mappings?维度编号=DIME-0001")
check("成员映射初始 10 行", mappings["total"] == 10, str(mappings["total"]))
records = get("/api/dimension/refresh-records?数据集编号=DATA-0003")
check("刷新记录初始 3 条且当前版本为 0003",
      records["items"][-1]["映射版本"] == "MAP-REGION-0003" and
      records["items"][-1]["刷新状态"] == "当前版本")
checkpoints = get("/api/dimension/checkpoints?维度编号=DIME-0001")
check("对照文件初始 3 份且均已核对",
      checkpoints["total"] == 3 and all(c["对照状态"] == "已核对" for c in checkpoints["items"]))
check("对照文件保留来源输入", all("来源输入" in c and c["来源输入"] for c in checkpoints["items"]))
check("对照文件带版本成员快照(支撑回退)", all(c.get("版本成员快照") for c in checkpoints["items"]))

# ---------- 2. 幂等刷新 ----------
current = [{"成员编码": r["成员编码"], "成员名称": r["成员名称"], "父级编码": r["父级编码"]}
           for r in mappings["items"] if r["status"] == "生效"]
status, body = call("POST", "/api/dimension/refresh", {
    "dimension_code": "DIME-0001", "dimension_name": "区域维度",
    "dataset_code": "DATA-0003", "dataset_name": "区域销售数据集", "members": current})
check("HTTP 同输入刷新幂等", status == 200 and body["idempotent"] is True, body.get("message", ""))

# ---------- 3. 新版本：成员级合并 ----------
new_members = [dict(m) for m in current]
for m in new_members:
    if m["成员编码"] == "M005":
        m["父级编码"] = "M001"  # 南京：江苏 -> 华东大区
new_members.append({"成员编码": "M012", "成员名称": "无锡", "父级编码": "M003"})
new_members.append({"成员编码": "  ", "成员名称": "脏行", "父级编码": "M001"})
new_members.append(dict(new_members[0]))
status, body = call("POST", "/api/dimension/refresh", {
    "dimension_code": "DIME-0001", "dimension_name": "区域维度",
    "dataset_code": "DATA-0003", "dataset_name": "区域销售数据集", "members": new_members})
check("HTTP 新版本刷新成功", status == 200 and body["映射版本"] == "MAP-REGION-0004",
      str(body.get("映射版本")))
check("对照明细仅 2 条变更", len(body["对照文件"]["对照明细"]) == 2)
mappings = get("/api/dimension/mappings?维度编号=DIME-0001")
by_code = {r["成员编码"]: r for r in mappings["items"]}
check("南京归属切到华东大区", by_code["M005"]["父级名称"] == "华东大区")
check("无锡新增归属江苏", by_code["M012"]["父级名称"] == "江苏")
check("脏行未写入", "M012" in by_code and all(r["成员编码"] != "  " for r in mappings["items"]))

# ---------- 4. 并发：8 路同时刷新 ----------
barrier = threading.Barrier(8)
results: list[object] = []
results_lock = threading.Lock()


def concurrent_call(payload: dict) -> None:
    barrier.wait()
    _, body = call("POST", "/api/dimension/refresh", payload)
    with results_lock:
        results.append(body)


base_payload = {
    "dimension_code": "DIME-0001", "dimension_name": "区域维度",
    "dataset_code": "DATA-0003", "dataset_name": "区域销售数据集"}
threads = []
for i in range(4):  # 过期前置版本 -> 冲突
    threads.append(threading.Thread(target=concurrent_call,
                                    args=({**base_payload, "members": current,
                                           "expected_previous_version": "MAP-REGION-0001"},)))
for i in range(4):  # 同输入无条件提交 -> 1 个生效 + 3 个幂等
    threads.append(threading.Thread(target=concurrent_call,
                                    args=({**base_payload, "members": new_members},)))
for t in threads:
    t.start()
for t in threads:
    t.join()
committed = [r for r in results if r.get("ok") and not r.get("idempotent")]
idempotent = [r for r in results if r.get("idempotent")]
conflict = [r for r in results if r.get("conflict")]
check("并发下只落 0 个新增有效版本(输入相同全部幂等)", len(committed) == 0, str(len(committed)))
check("4 路相同输入均幂等", len(idempotent) == 4, str(len(idempotent)))
check("4 路过期版本均冲突且生成对照文件", len(conflict) == 4 and
      all(r.get("冲突对照文件编号") for r in conflict))

# 再用不同输入并发，验证「只落一个有效版本」
barrier2 = threading.Barrier(3)
results2: list[object] = []
lock2 = threading.Lock()
changed = [dict(m) for m in new_members]
for m in changed:
    if m["成员编码"] == "M012":
        m["父级编码"] = "M001"


def concurrent_call2() -> None:
    barrier2.wait()
    _, body = call("POST", "/api/dimension/refresh", {**base_payload, "members": changed})
    with lock2:
        results2.append(body)


threads2 = [threading.Thread(target=concurrent_call2) for _ in range(3)]
for t in threads2:
    t.start()
for t in threads2:
    t.join()
committed2 = [r for r in results2 if r.get("ok") and not r.get("idempotent")]
check("不同输入并发只落 1 个有效版本", len(committed2) == 1,
      f"committed={len(committed2)}, others={[r.get('message') for r in results2]}")
mappings = get("/api/dimension/mappings?维度编号=DIME-0001")
by_code = {r["成员编码"]: r for r in mappings["items"] if r["status"] == "生效"}
check("并发后无锡归属唯一为华东大区", by_code["M012"]["父级名称"] == "华东大区")
check("并发后无重复成员行", len(by_code) == len({r["成员编码"] for r in mappings["items"] if r["status"] == "生效"}))

# ---------- 5. 冲突文件核对 ----------
pending = get("/api/dimension/checkpoints?对照状态=待核对")
check("待核对冲突文件共 4 份", pending["total"] == 4, str(pending["total"]))
cp_id = pending["items"][0]["id"]
status, body = call("POST", f"/api/dimension/checkpoints/{cp_id}/verify")
check("冲突文件核对被驳回", status == 200 and body["ok"] is False and
      body["对照文件"]["对照状态"] == "已驳回", body.get("message", ""))

# ---------- 6. 分析消费并持久化，重新读取可回查 ----------
status, body = call("POST", "/api/dimension/analyze", {"consumer": "维度分析接口"})
check("分析消费成功并返回提交边界", status == 200 and body["ok"] and
      body["提交边界标识"].startswith("COMMIT-"))
cp_id = body  # noop
head_cp = get("/api/dimension/checkpoints?维度编号=DIME-0001")
consumed = [c for c in head_cp["items"] if c.get("分析消费时间")]
check("消费事实已持久化到对照文件", bool(consumed) and consumed[-1]["分析消费方"] == "维度分析接口",
      str(consumed[-1].get("分析消费时间") if consumed else ""))

# ---------- 7. 回退 ----------
status, body = call("POST", "/api/dimension/rollback", {"target_version": "MAP-REGION-0002"})
check("HTTP 回退成功", status == 200 and body["ok"] and body["回退自版本"] == "MAP-REGION-0002",
      body.get("message", ""))
mappings = get("/api/dimension/mappings?维度编号=DIME-0001")
effective = [r for r in mappings["items"] if r["status"] == "生效"]
check("回退后生效成员恢复为 9 个", len(effective) == 9, str(len(effective)))
check("回退后深圳归属广东(M009 的 0002 归属)",
      next(r for r in effective if r["成员编码"] == "M009")["父级名称"] == "广东")
rb_cp = body["对照文件"]
check("回退对照文件沿用同一来源段", rb_cp["来源输入"].startswith("[回退]") and
      "REF-2026-0002" in rb_cp["来源输入"])
# 重新进入页面（重新 GET）归属不变
again = get("/api/dimension/mappings?维度编号=DIME-0001")
check("重新读取成员归属保持一致",
      [(r["成员编码"], r["父级编码"]) for r in again["items"]] ==
      [(r["成员编码"], r["父级编码"]) for r in mappings["items"]])

# ---------- 8. 导出 ----------
export_map = urllib.request.urlopen(BASE + _encode_path(
    "/api/dimension/mappings/export?维度编号=DIME-0001")).read().decode("utf-8")
rows = list(csv.reader(io.StringIO(export_map.lstrip("﻿"))))
check("映射表导出含表头与数据行", rows[0][0] == "维度编号" and len(rows) > 10, str(len(rows)))
cp_export_id = rb_cp["id"]
export_cp = urllib.request.urlopen(
    BASE + f"/api/dimension/checkpoints/{cp_export_id}/export").read().decode("utf-8")
cp_rows = list(csv.reader(io.StringIO(export_cp.lstrip("﻿"))))
check("对照文件导出可对照刷新前后差异", cp_rows[0] == ["对照文件编号", "映射版本", "成员编码",
      "成员名称", "刷新前父级", "刷新后父级", "变更类型"] and len(cp_rows) > 1)

print()
if failures:
    print(f"共 {len(failures)} 项失败：{failures}")
    sys.exit(1)
print("HTTP 端到端全部验证通过")
