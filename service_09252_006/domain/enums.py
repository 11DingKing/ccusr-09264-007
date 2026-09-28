"""领域枚举：角色、材料类型、敏感度、状态机取值。"""
from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    INSTITUTION_ADMIN = "institution_admin"
    INSTITUTION_SUBMITTER = "institution_submitter"
    REVIEWER = "reviewer"
    QUALITY_AUTHORITY = "quality_authority"
    AUDITOR = "auditor"


class MaterialKind(str, Enum):
    SYLLABUS = "syllabus"               # 课程大纲
    FACULTY = "faculty"                 # 师资材料
    ASSESSMENT = "assessment"           # 考核材料
    ENTERPRISE_FEEDBACK = "enterprise_feedback"  # 企业反馈


class Sensitivity(str, Enum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"  # 敏感企业反馈等，按机构与角色最小披露


class PackageStatus(str, Enum):
    DRAFT = "draft"                # 组包中，可追加材料
    SEALED = "sealed"              # 已封存，清单指纹固定
    UNDER_REVIEW = "under_review"  # 已分配评审
    DECIDED = "decided"            # 结论已签发，不可再改
    # 后补材料永远进入新的复审包，旧包不复活


class RequestStatus(str, Enum):
    PENDING = "pending"      # 已分配，等待评审人响应
    ACCEPTED = "accepted"
    DECLINED = "declined"
    COMPLETED = "completed"  # 评审人已提交结论
    CANCELLED = "cancelled"  # 被重新分配


class Verdict(str, Enum):
    APPROVE = "approve"
    OBJECT = "object"  # 有异议


class Decision(str, Enum):
    APPROVED = "approved"
    NEEDS_REVISION = "needs_revision"
    REJECTED = "rejected"


class CasePriority(str, Enum):
    """案件优先级：决定评审服务时限预算（按工作时间计）。"""

    URGENT = "urgent"  # 特急
    HIGH = "high"      # 加急
    NORMAL = "normal"  # 常规
    LOW = "low"        # 普通


class SlaTimerStatus(str, Enum):
    RUNNING = "running"  # 计时中
    PAUSED = "paused"    # 暂停中（如已取消、等待转交，不累计时长）
    CLOSED = "closed"    # 评审完成/拒绝/已转出，不再计时


class SlaEventKind(str, Enum):
    TIMER_STARTED = "timer_started"      # 时限开始计时（转交后接手为新计时器，携带结转秒数）
    TIMER_PAUSED = "timer_paused"        # 转交期间暂停
    TIMER_CLOSED = "timer_closed"        # 评审终结/已转出，计时关闭
    ESCALATED = "escalated"              # 超时升级
