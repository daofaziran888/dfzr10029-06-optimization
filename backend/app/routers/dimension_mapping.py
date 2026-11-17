"""维度成员映射接口：版本化刷新、对照文件核对/消费/回退与导出。

路径与维度管理共用 /api/dimension 前缀，本路由必须先于 dimension 路由注册，
否则 /mappings、/refresh 等会被 /{entry_id} 当成条目 id 解析。
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services.dimension_mapping import service

router = APIRouter(prefix="/api/dimension", tags=["维度成员映射"])


class MemberInput(BaseModel):
    成员编码: str
    成员名称: str | None = None
    父级编码: str | None = None


class RefreshPayload(BaseModel):
    dimension_code: str = Field(description="维度编号，如 DIME-0001")
    dimension_name: str | None = None
    dataset_code: str = Field(description="关联数据集编号，如 DATA-0003")
    dataset_name: str | None = None
    members: list[MemberInput] = Field(default_factory=list)
    expected_previous_version: str | None = None
    operator: str | None = None


class AnalyzePayload(BaseModel):
    checkpoint_id: int | None = None
    consumer: str | None = None


class RollbackPayload(BaseModel):
    target_version: str
    operator: str | None = None


def _csv_response(filename: str, header: list[str], rows: list[list[object]]) -> StreamingResponse:
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        cells = []
        for value in row:
            text = "" if value is None else str(value)
            cells.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(cells)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/mappings")
def list_mappings(维度编号: str | None = Query(default=None, alias="维度编号")) -> dict[str, object]:
    """数据刷新后的维度成员映射表；读取走提交边界快照，不会出现新旧父级混合。"""
    items = service.list_mappings(维度编号)
    return {"items": items, "total": len(items), "page": 1, "size": len(items) or 1}


@router.get("/mappings/export")
def export_mappings(维度编号: str | None = Query(default=None, alias="维度编号")) -> StreamingResponse:
    """导出当前生效的维度成员映射表，供离线对照刷新前后差异。"""
    items = service.list_mappings(维度编号)
    header = ["维度编号", "成员编码", "成员名称", "父级编码", "父级名称", "层级", "映射版本",
              "首次写入版本", "最后更新版本", "提交时间"]
    rows = [[item.get(key, "") for key in header] for item in items]
    return _csv_response("dimension_member_mapping.csv", header, rows)


@router.get("/refresh-records")
def list_refresh_records(数据集编号: str | None = Query(default=None, alias="数据集编号")) -> dict[str, object]:
    """关联数据集刷新记录，按刷新序号升序，标注当前/历史版本与提交边界标识。"""
    items = service.list_refresh_records(数据集编号)
    return {"items": items, "total": len(items), "page": 1, "size": len(items) or 1}


@router.get("/checkpoints")
def list_checkpoints(
    维度编号: str | None = Query(default=None, alias="维度编号"),
    对照状态: str | None = Query(default=None, alias="对照状态"),
) -> dict[str, object]:
    """维度刷新对照文件列表：保留来源输入、提交边界与核对状态，冲突文件也在此等待核对。"""
    items = service.list_checkpoints(维度编号, 对照状态)
    return {"items": items, "total": len(items), "page": 1, "size": len(items) or 1}


@router.get("/checkpoints/{checkpoint_id}")
def get_checkpoint(checkpoint_id: int) -> dict[str, object]:
    """打开单份对照文件：回退后仍可沿「来源输入」追溯同一段来源。"""
    checkpoint = service.get_checkpoint(checkpoint_id)
    if checkpoint is None:
        return {"ok": False, "message": f"对照文件 {checkpoint_id} 不存在"}
    return checkpoint


@router.get("/checkpoints/{checkpoint_id}/export")
def export_checkpoint(checkpoint_id: int) -> StreamingResponse:
    """导出对照文件：逐条成员对照刷新前后父级差异与变更类型。"""
    checkpoint = service.get_checkpoint(checkpoint_id)
    header = ["对照文件编号", "映射版本", "成员编码", "成员名称", "刷新前父级", "刷新后父级", "变更类型"]
    rows = []
    if checkpoint is not None:
        for detail in checkpoint.get("对照明细", []):
            rows.append([
                checkpoint.get("对照文件编号", ""), checkpoint.get("映射版本", ""),
                detail.get("成员编码", ""), detail.get("成员名称", ""),
                detail.get("刷新前父级", ""), detail.get("刷新后父级", ""),
                detail.get("变更类型", ""),
            ])
    return _csv_response(f"dimension_refresh_checkpoint_{checkpoint_id}.csv", header, rows)


@router.post("/refresh")
def refresh_dimension(payload: RefreshPayload) -> dict[str, object]:
    """数据集刷新后按映射版本稳定更新维度成员（清洗→成员级差异合并→同一提交边界落库）。"""
    return service.refresh_dimension(
        dimension_code=payload.dimension_code,
        dimension_name=payload.dimension_name or "",
        dataset_code=payload.dataset_code,
        dataset_name=payload.dataset_name or "",
        raw_members=[member.model_dump() for member in payload.members],
        expected_previous_version=payload.expected_previous_version,
        operator=payload.operator or "维度分析接口",
    )


@router.post("/checkpoints/{checkpoint_id}/verify")
def verify_checkpoint(checkpoint_id: int) -> dict[str, object]:
    """核对主版本边界：提交边界、来源快照与对照明细一致才可标记已核对。"""
    return service.verify_checkpoint(checkpoint_id)


@router.post("/analyze")
def analyze(payload: AnalyzePayload) -> dict[str, object]:
    """维度分析接口消费并持久化对照文件；刷新页面后持久结果仍可回查。"""
    return service.analyze(payload.checkpoint_id, payload.consumer or "")


@router.post("/rollback")
def rollback(payload: RollbackPayload) -> dict[str, object]:
    """按历史映射版本回退成员归属，回退对照文件沿用同一来源段可追溯。"""
    return service.rollback(payload.target_version, payload.operator or "维度分析接口")
