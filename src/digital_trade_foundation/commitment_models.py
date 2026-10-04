"""承诺治理平台读取侧返回的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Project:
    project_id: str
    name: str
    status: str
    created_by: str
    created_at: str
    parties: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Commitment:
    commitment_id: str
    project_id: str
    commitment_type: str
    provider_organization_id: str
    title: str
    terms: dict[str, Any]
    original_amount_minor: int | None
    currency: str | None
    status: str
    sealed_at: str | None
    created_by: str
    created_at: str
    version: int
    stages: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ConditionView:
    condition_id: str
    stage_id: str
    sequence: int
    label: str
    due_at: str | None
    status: str
    accepted_evidence_id: str | None
    evidences: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class EscrowView:
    project_id: str
    currency: str
    deposited_total: int
    disbursed_total: int
    balance: int
    updated_at: str


@dataclass(frozen=True)
class ResponsibilityView:
    """一份承诺的责任视图：原始责任 + 历次调整 + 尚未兑现余额。"""

    commitment_id: str
    project_id: str
    commitment_type: str
    provider_organization_id: str
    status: str
    original_amount_minor: int | None
    currency: str | None
    adjustments: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    disbursed_minor: int = 0
    outstanding_minor: int | None = None
    replaced_by: str | None = None


@dataclass(frozen=True)
class DisputeView:
    dispute_id: str
    project_id: str
    commitment_id: str | None
    status: str
    title: str
    detail: dict[str, Any]
    opened_by: str
    opened_at: str
    conclusion: dict[str, Any] | None
    concluded_by: str | None
    concluded_at: str | None
