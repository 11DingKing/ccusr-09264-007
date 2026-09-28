"""领域实体（贫血数据载体，业务规则在领域服务/应用服务中）。

时间一律以带时区的 UTC ISO-8601 字符串存储；截止时间同时保存原始
IANA 时区用于展示，比较时统一换化为 UTC 时刻，从而正确处理跨时区截止。
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Optional

from .enums import (
    CasePriority,
    Decision,
    MaterialKind,
    PackageStatus,
    RequestStatus,
    Role,
    Sensitivity,
    SlaEventKind,
    SlaTimerStatus,
    Verdict,
)


@dataclass
class User:
    user_id: str
    institution_id: Optional[str]  # 机构用户非空；权威机构/审计可为空
    roles: tuple[str, ...]
    display_name: str = ""

    def has_role(self, role: Role | str) -> bool:
        wanted = role.value if isinstance(role, Role) else role
        return wanted in self.roles


@dataclass
class Material:
    """逻辑材料（课程大纲、师资、考核、企业反馈中的某一份）。"""

    material_id: str
    institution_id: str
    kind: str                      # MaterialKind
    sensitivity: str               # Sensitivity
    title: str
    current_version_id: Optional[str]
    withdrawn: bool
    created_at: str


@dataclass
class MaterialVersion:
    """材料的一次不可变版本。字节内容按 sha256 内容寻址、去重存储。"""

    version_id: str
    material_id: str
    institution_id: str
    sha256: str
    size: int
    media_type: str
    version_no: int
    supersedes_version_id: Optional[str]
    created_by: str
    created_at: str
    withdrawn: bool                # 该版本是否已撤回


@dataclass
class PackageEntry:
    """评审包对材料【具体版本】的固定引用。"""

    entry_id: str
    package_id: str
    material_id: str
    version_id: str
    sha256: str
    kind: str
    sensitivity: str
    added_at: str


@dataclass
class ReviewPackage:
    package_id: str
    institution_id: str
    title: str
    status: str                    # PackageStatus
    created_by: str
    created_at: str
    sealed_at: Optional[str]
    manifest_fingerprint: Optional[str]
    decided_at: Optional[str]
    decision: Optional[str]        # Decision
    decision_note: Optional[str]
    review_fingerprint: Optional[str]
    supersedes_package_id: Optional[str]  # 后补材料触发的复审包指向前序包
    entries: list[PackageEntry] = field(default_factory=list)

    def is_mutable(self) -> bool:
        return self.status == PackageStatus.DRAFT.value


@dataclass
class ReviewRequest:
    request_id: str
    package_id: str
    institution_id: str
    reviewer_id: str
    status: str                    # RequestStatus
    assigned_by: str
    assigned_at: str
    responded_at: Optional[str]
    completed_at: Optional[str]
    verdict: Optional[str]         # Verdict
    comment: Optional[str]
    deadline_at_utc: Optional[str]  # 截止时刻（UTC）
    deadline_timezone: Optional[str]  # 原始 IANA 时区，仅展示用


@dataclass
class Objection:
    objection_id: str
    request_id: str
    package_id: str
    institution_id: str
    reviewer_id: str
    category: str
    detail: str
    created_at: str


@dataclass
class Blob:
    sha256: str
    data: bytes
    media_type: str
    created_at: str


@dataclass
class AuditEntry:
    audit_id: str
    package_id: Optional[str]
    institution_id: Optional[str]
    actor_id: str
    action: str
    at: str
    detail: dict = field(default_factory=dict)


@dataclass
class CaseCalendar:
    """案件日历：按机构工作时段与节假日计时（非 7×24）。

    work_start/work_end 为当地“时:分”；working_weekdays 为 ISO 星期
    （1=周一…7=周日）；holidays 为机构当地日期集合（YYYY-MM-DD）；
    timezone 给出这些墙上时间所属的 IANA 时区。
    """

    calendar_id: str
    institution_id: Optional[str]          # None 表示全局默认日历
    timezone: str
    work_start: str = "09:00"
    work_end: str = "17:00"
    working_weekdays: tuple[int, ...] = (1, 2, 3, 4, 5)
    holidays: tuple[str, ...] = ()


@dataclass
class SlaTimer:
    """单个评审请求的服务时限计时器。

    已用工作时间 = consumed_work_seconds（此前每段累计）
                 + 自 segment_started_at 起、按案件日历流逝的工作秒数。
    暂停时把当前段结算进 consumed_work_seconds 并清空 segment_started_at；
    转交暂停期间 segment 不存在，时间不会继续累加。
    """

    timer_id: str
    request_id: str
    package_id: str
    institution_id: str
    priority: str                          # CasePriority
    budget_seconds: int                    # 该优先级的时限预算（工作秒）
    calendar_id: str
    status: str                            # SlaTimerStatus
    current_owner_id: str                  # 当前负责人（评审人）
    consumed_work_seconds: float = 0.0
    segment_started_at: Optional[str] = None  # 当前连续计时段起点（UTC）；暂停为 None
    due_at: Optional[str] = None           # 最近一次起步时按剩余预算排定的到期时刻
    escalated: bool = False
    escalation_event_id: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class SlaEvent:
    """时限暂停/恢复/关闭与升级等事件，追加写入、不可变。"""

    event_id: str
    timer_id: str
    request_id: str
    package_id: str
    institution_id: str
    kind: str                              # SlaEventKind
    at: str
    actor_id: Optional[str] = None
    detail: dict = field(default_factory=dict)


@dataclass
class EscalationEvent:
    """超时升级事件：超时后生成并通知当前负责人。"""

    escalation_id: str
    timer_id: str
    request_id: str
    package_id: str
    institution_id: str
    priority: str
    owner_id: str                          # 升级发生时的当前负责人
    overdue_seconds: float                 # 超时的工作秒数
    occurred_at: str
    notified: bool = False


def asdict(obj) -> dict:
    return dataclasses.asdict(obj)
