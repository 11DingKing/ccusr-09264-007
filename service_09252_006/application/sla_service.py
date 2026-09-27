"""评审服务时限（SLA）用例：按优先级计时、转交暂停、超时升级通知。

- 计时只累计案件日历的工作时间；转交（暂停）区间不累计——
  转交期间计时不会继续累加；
- 暂停区间与升级事件写入 SQLite，可审计、崩溃可恢复；
- 超时由 sweep_escalations 生成升级事件并通知当前负责人；
  升级标记用条件 UPDATE 抢占，并发扫描只生效一次；
- recalculate 为只读的重算视图：已用/剩余工作秒与推算截止时刻。
"""
from __future__ import annotations

from ..domain.enums import RequestStatus, Role, SlaPriority
from ..domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from ..domain.models import (
    EscalationEvent,
    Notification,
    SlaCase,
    SlaPause,
    User,
)
from ..domain.sla import CaseCalendar, PauseSpan, parse_moment
from .base import Service, require_roles, require_user
from .ports import Clock, IdGenerator
from .repository import Repository


class SlaService(Service):
    def __init__(
        self,
        repo: Repository,
        clock: Clock,
        ids: IdGenerator,
        calendar: CaseCalendar,
    ) -> None:
        super().__init__(repo, clock, ids)
        self.calendar = calendar

    # ------------------------------------------------------------- 立案
    def open_case(
        self,
        actor: User,
        *,
        request_id: str,
        priority: str,
        owner_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """为进行中的评审请求立案计时；同一请求重复立案回放既有案件。"""
        require_roles(actor, Role.QUALITY_AUTHORITY, Role.INSTITUTION_ADMIN)
        if priority not in {p.value for p in SlaPriority}:
            raise ValidationError("未知优先级", details={"priority": priority})
        try:
            self.calendar.limit_seconds(priority)
        except ValueError as exc:
            raise ValidationError(str(exc))

        def work() -> dict:
            req = self.repo.get_request(request_id)
            if req is None:
                raise NotFoundError("评审请求不存在")
            self._check_institution(actor, req.institution_id)
            if req.status not in (
                RequestStatus.PENDING.value,
                RequestStatus.ACCEPTED.value,
            ):
                raise ConflictError(
                    "只有进行中的评审请求可以立案计时",
                    details={"status": req.status},
                )
            existing = self.repo.get_open_sla_case_by_request(request_id)
            if existing is not None:
                return self._case_dict(existing, replayed=True)
            owner = owner_id or req.reviewer_id
            if self.repo.get_user(owner) is None:
                raise ValidationError("负责人不存在", details={"owner_id": owner})
            case = SlaCase(
                case_id=self.ids.new_id("sla"),
                request_id=request_id,
                package_id=req.package_id,
                institution_id=req.institution_id,
                priority=priority,
                owner_id=owner,
                opened_at=self.clock.now_iso(),
                closed_at=None,
                escalated_at=None,
            )
            self.repo.insert_sla_case(case)
            self.audit(
                actor.user_id, "sla.case_opened",
                package_id=case.package_id, institution_id=case.institution_id,
                detail={
                    "case_id": case.case_id,
                    "request_id": request_id,
                    "priority": priority,
                    "owner_id": owner,
                },
            )
            return self._case_dict(case)

        return self.idempotent(idempotency_key, work)

    # ------------------------------------------------------------- 转交
    def begin_transfer(
        self,
        actor: User,
        *,
        case_id: str,
        reason: str = "",
        idempotency_key: str | None = None,
    ) -> dict:
        """开始转交：打开暂停区间，计时暂停。"""
        require_roles(actor, Role.QUALITY_AUTHORITY, Role.INSTITUTION_ADMIN)

        def work() -> dict:
            case = self._require_open_case(actor, case_id)
            if self.repo.get_open_sla_pause(case_id) is not None:
                raise ConflictError("案件已在转交中，计时已暂停")
            pause = SlaPause(
                pause_id=self.ids.new_id("pause"),
                case_id=case_id,
                reason=reason.strip(),
                started_at=self.clock.now_iso(),
                ended_at=None,
            )
            self.repo.insert_sla_pause(pause)
            self.audit(
                actor.user_id, "sla.transfer_started",
                package_id=case.package_id, institution_id=case.institution_id,
                detail={
                    "case_id": case_id,
                    "pause_id": pause.pause_id,
                    "reason": pause.reason,
                },
            )
            return self._pause_dict(pause)

        return self.idempotent(idempotency_key, work)

    def complete_transfer(
        self,
        actor: User,
        *,
        case_id: str,
        new_owner_id: str,
        idempotency_key: str | None = None,
    ) -> dict:
        """完成转交：关闭暂停区间（恢复计时），负责人切换为新负责人。"""
        require_roles(actor, Role.QUALITY_AUTHORITY, Role.INSTITUTION_ADMIN)

        def work() -> dict:
            case = self._require_open_case(actor, case_id)
            pause = self.repo.get_open_sla_pause(case_id)
            if pause is None:
                raise ConflictError("没有进行中的转交")
            if self.repo.get_user(new_owner_id) is None:
                raise ValidationError("新负责人不存在", details={"owner_id": new_owner_id})
            now = self.clock.now_iso()
            self.repo.close_sla_pause(pause.pause_id, now)
            self.repo.update_sla_case_owner(case_id, new_owner_id)
            self.audit(
                actor.user_id, "sla.transfer_completed",
                package_id=case.package_id, institution_id=case.institution_id,
                detail={
                    "case_id": case_id,
                    "pause_id": pause.pause_id,
                    "new_owner_id": new_owner_id,
                },
            )
            pause.ended_at = now
            return {**self._pause_dict(pause), "new_owner_id": new_owner_id}

        return self.idempotent(idempotency_key, work)

    def close_case(
        self,
        actor: User,
        *,
        case_id: str,
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(actor, Role.QUALITY_AUTHORITY, Role.INSTITUTION_ADMIN)

        def work() -> dict:
            case = self._require_open_case(actor, case_id)
            if self.repo.get_open_sla_pause(case_id) is not None:
                raise ConflictError("转交进行中，请先完成转交再结案")
            now = self.clock.now_iso()
            if not self.repo.close_sla_case(case_id, now):
                raise ConflictError("案件已被并发关闭")
            self.audit(
                actor.user_id, "sla.case_closed",
                package_id=case.package_id, institution_id=case.institution_id,
                detail={"case_id": case_id},
            )
            return {"case_id": case_id, "closed_at": now, "replayed": False}

        return self.idempotent(idempotency_key, work)

    # --------------------------------------------------------- 重算/查询
    def recalculate(self, actor: User, *, case_id: str) -> dict:
        """重新计算时限：已用/剩余工作秒、推算截止时刻、是否暂停/超时。"""
        require_user(actor)
        case = self._get_case(case_id)
        self._check_can_view(actor, case)
        now = self.clock.now_utc()
        pauses = self.repo.list_sla_pauses(case_id)
        end = parse_moment(case.closed_at) if case.closed_at else now
        spans = self._spans(pauses, end)
        opened = parse_moment(case.opened_at)
        limit = self.calendar.limit_seconds(case.priority)
        elapsed = self.calendar.elapsed_seconds(opened, end, spans)
        paused = case.closed_at is None and any(p.ended_at is None for p in pauses)
        due = None if paused else self.calendar.due_at(opened, spans, limit)
        return {
            "case_id": case.case_id,
            "request_id": case.request_id,
            "package_id": case.package_id,
            "priority": case.priority,
            "owner_id": case.owner_id,
            "status": (
                "closed" if case.closed_at else ("paused" if paused else "open")
            ),
            "opened_at": case.opened_at,
            "closed_at": case.closed_at,
            "elapsed_business_seconds": int(elapsed),
            "limit_business_seconds": limit,
            "remaining_business_seconds": int(max(limit - elapsed, 0)),
            "due_at_utc": due.isoformat() if due is not None else None,
            "paused": paused,
            "breached": elapsed >= limit,
            "escalated_at": case.escalated_at,
            "calendar_timezone": self.calendar.timezone,
        }

    def list_escalations(self, actor: User, *, case_id: str) -> list[dict]:
        require_user(actor)
        case = self._get_case(case_id)
        self._check_can_view(actor, case)
        return [self._escalation_dict(e) for e in self.repo.list_escalations(case_id)]

    def list_notifications(self, actor: User) -> list[dict]:
        require_user(actor)
        return [
            {
                "notification_id": n.notification_id,
                "kind": n.kind,
                "message": n.message,
                "created_at": n.created_at,
            }
            for n in self.repo.list_notifications(actor.user_id)
        ]

    # ------------------------------------------------------------- 升级
    def sweep_escalations(self, actor: User) -> list[dict]:
        """扫描所有未结案件：超时且未升级的生成升级事件并通知当前负责人。"""
        require_roles(actor, Role.QUALITY_AUTHORITY, Role.INSTITUTION_ADMIN)
        created: list[dict] = []
        with self.repo.transaction():
            now = self.clock.now_utc()
            for case in self.repo.list_open_sla_cases():
                if (
                    not actor.has_role(Role.QUALITY_AUTHORITY)
                    and case.institution_id != actor.institution_id
                ):
                    continue
                spans = self._spans(self.repo.list_sla_pauses(case.case_id), now)
                elapsed = self.calendar.elapsed_seconds(
                    parse_moment(case.opened_at), now, spans
                )
                limit = self.calendar.limit_seconds(case.priority)
                if elapsed < limit:
                    continue
                escalated_at = self.clock.now_iso()
                if not self.repo.mark_sla_case_escalated(case.case_id, escalated_at):
                    continue  # 并发扫描已被对方升级
                event = EscalationEvent(
                    escalation_id=self.ids.new_id("esc"),
                    case_id=case.case_id,
                    request_id=case.request_id,
                    owner_id=case.owner_id,
                    elapsed_business_seconds=int(elapsed),
                    limit_business_seconds=limit,
                    created_at=escalated_at,
                )
                self.repo.insert_escalation(event)
                self.repo.insert_notification(
                    Notification(
                        notification_id=self.ids.new_id("ntf"),
                        user_id=case.owner_id,
                        kind="sla_escalation",
                        message=(
                            f"评审服务时限超时：案件 {case.case_id}"
                            f"（优先级 {case.priority}）已用工作时间"
                            f" {int(elapsed)} 秒，超过时限 {limit} 秒，请尽快处理"
                        ),
                        created_at=escalated_at,
                    )
                )
                self.audit(
                    actor.user_id, "sla.escalated",
                    package_id=case.package_id, institution_id=case.institution_id,
                    detail={
                        "case_id": case.case_id,
                        "escalation_id": event.escalation_id,
                        "owner_id": case.owner_id,
                    },
                )
                created.append(self._escalation_dict(event))
        return created

    # ------------------------------------------------------------- 内部
    @staticmethod
    def _spans(pauses: list[SlaPause], open_end) -> list[PauseSpan]:
        """暂停区间化为绝对时刻对；未结束的暂停按 open_end（通常是现在）截断。"""
        return [
            (
                parse_moment(p.started_at),
                parse_moment(p.ended_at) if p.ended_at else open_end,
            )
            for p in pauses
        ]

    def _get_case(self, case_id: str) -> SlaCase:
        case = self.repo.get_sla_case(case_id)
        if case is None:
            raise NotFoundError("时限案件不存在")
        return case

    def _require_open_case(self, actor: User, case_id: str) -> SlaCase:
        case = self._get_case(case_id)
        self._check_institution(actor, case.institution_id)
        if case.closed_at is not None:
            raise ConflictError("案件已关闭")
        return case

    @staticmethod
    def _check_institution(actor: User, institution_id: str) -> None:
        if (
            not actor.has_role(Role.QUALITY_AUTHORITY)
            and actor.institution_id != institution_id
        ):
            raise PermissionDeniedError("只能操作本机构的时限案件")

    @staticmethod
    def _check_can_view(actor: User, case: SlaCase) -> None:
        if actor.has_role(Role.QUALITY_AUTHORITY) or actor.has_role(Role.AUDITOR):
            return
        if actor.user_id == case.owner_id:
            return
        if (
            actor.has_role(Role.INSTITUTION_ADMIN)
            and actor.institution_id == case.institution_id
        ):
            return
        raise PermissionDeniedError("不能查看该时限案件")

    @staticmethod
    def _case_dict(case: SlaCase, *, replayed: bool = False) -> dict:
        return {
            "case_id": case.case_id,
            "request_id": case.request_id,
            "package_id": case.package_id,
            "institution_id": case.institution_id,
            "priority": case.priority,
            "owner_id": case.owner_id,
            "opened_at": case.opened_at,
            "closed_at": case.closed_at,
            "escalated_at": case.escalated_at,
            "replayed": replayed,
        }

    @staticmethod
    def _pause_dict(pause: SlaPause) -> dict:
        return {
            "pause_id": pause.pause_id,
            "case_id": pause.case_id,
            "reason": pause.reason,
            "started_at": pause.started_at,
            "ended_at": pause.ended_at,
            "replayed": False,
        }

    @staticmethod
    def _escalation_dict(event: EscalationEvent) -> dict:
        return {
            "escalation_id": event.escalation_id,
            "case_id": event.case_id,
            "request_id": event.request_id,
            "owner_id": event.owner_id,
            "elapsed_business_seconds": event.elapsed_business_seconds,
            "limit_business_seconds": event.limit_business_seconds,
            "created_at": event.created_at,
        }
