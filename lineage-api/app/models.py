"""Pydantic request / response models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

NodeKind = Literal["source", "derived", "report"]


class NodeIn(BaseModel):
    name: str = Field(..., min_length=1, description="全局唯一字段名")
    kind: NodeKind
    # 候选输入组：组内字段全部可用时该组可用；任一组可用即可产出本列。
    # source 节点必须为空或省略。
    candidate_groups: list[list[str]] = Field(default_factory=list)


class RegisterRequest(BaseModel):
    nodes: list[NodeIn] = Field(..., min_length=1)


class RegisterResponse(BaseModel):
    registered: int
    nodes: list[str]
    message: str


class ImpactRequest(BaseModel):
    retiring: list[str] = Field(
        ..., min_length=1, description="拟下线的字段名（任意类型节点）"
    )


class MissingDependency(BaseModel):
    name: str
    kind: NodeKind
    root_causes: list[str] = Field(
        description="导致该输入不可用的最深层退役字段"
    )


class FailedGroup(BaseModel):
    group_index: int
    inputs: list[str]
    missing: list[MissingDependency]


class FailedReportColumn(BaseModel):
    name: str
    failed_groups: list[FailedGroup]


class ImpactResponse(BaseModel):
    retiring: list[str]
    failed_report_columns: list[FailedReportColumn]
    unavailable_count: int
    unavailable_nodes: list[str] = Field(
        description="所有不可用节点（含派生列，便于递归排查）"
    )


class NodeOut(BaseModel):
    name: str
    kind: NodeKind
    candidate_groups: list[list[str]]


class GraphResponse(BaseModel):
    nodes: list[NodeOut]
    counts: dict[str, int]


# --------------------------------------------------------------------------- #
# Supply-outage attribution
# --------------------------------------------------------------------------- #


class AttributionRequest(BaseModel):
    stopped_sources: list[str] = Field(
        default_factory=list,
        description="已确认停供的源字段名",
    )
    successful_reports: list[str] = Field(
        default_factory=list,
        description="实际成功产出的报表列名",
    )
    failed_reports: list[str] = Field(
        default_factory=list,
        description="实际产出失败的报表列名",
    )


class AttributionResponse(BaseModel):
    stopped_sources: list[str] = Field(description="已确认停供源字段（去重排序）")
    successful_reports: list[str]
    failed_reports: list[str]
    relevant_sources: list[str] = Field(
        description="观测报表列的全部上游源字段（推断作用域）"
    )
    minimal_extra_stopped: int = Field(
        description="能同时解释全部观测的最少额外停供源字段数"
    )
    consistent_solution_count: int = Field(
        description="达到最少额外停供数的一致方案数量"
    )
    must_check_sources: list[str] = Field(
        description="每种最少方案都新增停供的必查源字段"
    )
    to_check_sources: list[str] = Field(
        description="只在部分最少方案中停供的待查源字段"
    )
    minimal_solutions: list[list[str]] = Field(
        description="全部最少方案，每项为该方案新增停供的源字段"
    )
