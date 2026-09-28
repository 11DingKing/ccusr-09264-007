"""评审服务时限（SLA）用例服务。

- 时限按案件优先级给预算（工作时间），按机构案件日历计时；
- 分配即开始计时；取消分配（等待改派）暂停计时；转交给新负责人时
  新计时器继承此前已用工作秒数，转交窗口本身不计时；
- 评审人拒绝或提交结论后关闭计时；
- scan_due() 扫描运行中的计时器，超时则在同一事务内生成升级事件与
  SLA 事件，事务提交后通知【当前】负责人；重复扫描幂等，不重复升级。
"""
from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain.enums import CasePriority, Role, SlaEventKind, SlaTimerStatus
from ..domain.errors import (
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from ..domain.models import (
    CaseCalendar,
    EscalationEvent,
    ReviewRequest,
    SlaEvent,
    SlaTimer,
    User,
)
from ..domain.sla import (
    budget_seconds_for,
    compute_due_at,
    elapsed_work_seconds,
    working_seconds_between,
)
from .base import Service, require_roles
from .ports import CollectingNotifier, Notifier

DEFAULT_CALENDAR_ID = "default"
_DEFAULT_TIMEZONE = "Asia/Shanghai"


def default_calendar() -> CaseCalendar:
    """未配置机构日历时的兜底：上海时间 09:00–17:00，周一至周五。"""
    return CaseCalendar(
        calendar_id=DEFAULT_CALENDAR_ID,
        institution_id=None,
        timezone=_DEFAULT_TIMEZONE,
        work_start="09:00",
        work_end="17:00",
        working_weekdays=(1, 2, 3, 4, 5),
        holidays=(),
    )


class SlaService(Service):
    def __init__(self, repo, clock, ids, notifier: Notifier | None = None) -> None:
        super().__init__(repo, clock, ids)
        self.notifier: Notifier = notifier or CollectingNotifier()

    # ------------------------------------------------------------ 日历配置
    def configure_calendar(
        self,
        actor: User,
        *,
        calendar_id: str,
        timezone: str,
        work_start: str = "09:00",
        work_end: str = "17:00",
        working_weekdays=(1, 2, 3, 4, 5),
        holidays=(),
        institution_id: str | None = None,
    ) -> dict:
        require_roles(actor, Role.QUALITY_AUTHORITY, Role.INSTITUTION_ADMIN)
        if not calendar_id.strip():
            raise ValidationError("日历标识不能为空")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            raise ValidationError(f"未知时区: {timezone}")
        self._validate_hm(work_start)
        self._validate_hm(work_end)
        if work_end <= work_start:
            raise ValidationError("工作结束时刻必须晚于开始时刻")
        weekdays = tuple(sorted({int(d) for d in working_weekdays}))
        if not weekdays or any(d < 1 or d > 7 for d in weekdays):
            raise ValidationError("工作日必须是 1(周一)..7(周日) 的非空集合")
        holiday_tuple = tuple(sorted({str(d) for d in holidays}))

        calendar = CaseCalendar(
            calendar_id=calendar_id.strip(),
            institution_id=institution_id,
            timezone=timezone,
            work_start=work_start,
            work_end=work_end,
            working_weekdays=weekdays,
            holidays=holiday_tuple,
        )
        with self.repo.transaction():
            self.repo.upsert_calendar(calendar)
        return self._calendar_dict(calendar)

    @staticmethod
    def _validate_hm(text: str) -> None:
        try:
            hour_str, _, minute_str = text.partition(":")
            hour, minute = int(hour_str), int(minute_str or 0)
        except (ValueError, AttributeError):
            raise ValidationError(f"非法时刻: {text}")
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValidationError(f"非法时刻: {text}")

    def _resolve_calendar(self, institution_id: str) -> CaseCalendar:
        return self.repo.find_calendar_for(institution_id) or default_calendar()

    # --------------------------------------------------------- 计时生命周期
    def start_for_request(
        self, request: ReviewRequest, priority: str
    ) -> dict:
        """为一次新分配开始计时。

        若同包存在此前因取消（转交）而暂停的计时器，其已用工作秒数
        结转到新计时器，并把旧计时器关闭——转交窗口不计入，已用不丢失。
        幂等：同一请求重复调用直接回放。
        """
        if priority not in {p.value for p in CasePriority}:
            raise ValidationError("未知案件优先级", details={"priority": priority})
        with self.repo.transaction():
            existing = self.repo.get_sla_timer_by_request(request.request_id)
            if existing is not None:
                return self._timer_dict(existing)

            now = self.clock.now_utc()
            now_iso = now.isoformat()
            cal = self._resolve_calendar(request.institution_id)
            budget = budget_seconds_for(priority)

            # 仅当包内没有其他运行中计时器时，才视为“转交/改派”并继承
            # 此前已用工作时间；若仍有评审人在计时，则本次是加派独立评审人，
            # 不把他人耗时分摊给新计时器。
            existing_timers = self.repo.list_sla_timers_by_package(request.package_id)
            has_running = any(
                t.status == SlaTimerStatus.RUNNING.value for t in existing_timers
            )
            prior_paused = (
                [
                    t
                    for t in existing_timers
                    if t.status == SlaTimerStatus.PAUSED.value
                ]
                if not has_running
                else []
            )
            carried = max(
                (t.consumed_work_seconds for t in prior_paused), default=0.0
            )
            remaining = max(0.0, budget - carried)
            timer = SlaTimer(
                timer_id=self.ids.new_id("sla"),
                request_id=request.request_id,
                package_id=request.package_id,
                institution_id=request.institution_id,
                priority=priority,
                budget_seconds=budget,
                calendar_id=cal.calendar_id,
                status=SlaTimerStatus.RUNNING.value,
                current_owner_id=request.reviewer_id,
                consumed_work_seconds=carried,
                segment_started_at=now_iso,
                due_at=compute_due_at(now, remaining, cal).isoformat(),
                created_at=now_iso,
                updated_at=now_iso,
            )
            self.repo.insert_sla_timer(timer)
            self._event(
                timer,
                SlaEventKind.TIMER_STARTED,
                detail={
                    "priority": priority,
                    "budget_seconds": budget,
                    "carried_work_seconds": carried,
                    "due_at": timer.due_at,
                },
            )
            for old in prior_paused:
                old.status = SlaTimerStatus.CLOSED.value
                old.updated_at = now_iso
                self.repo.update_sla_timer(old)
                self._event(
                    old,
                    SlaEventKind.TIMER_CLOSED,
                    detail={"reason": "transferred", "carried_to": timer.timer_id},
                )
            return self._timer_dict(timer)

    def pause_for_request(self, request_id: str, *, reason: str = "") -> dict | None:
        """转交期间暂停：把当前段结算进已用时间，随后停止累加。"""
        with self.repo.transaction():
            timer = self.repo.get_sla_timer_by_request(request_id)
            if timer is None or timer.status != SlaTimerStatus.RUNNING.value:
                return None if timer is None else self._timer_dict(timer)
            cal = self._load_timer_calendar(timer)
            now_iso = self.clock.now_iso()
            self._settle(timer, now_iso, cal)
            timer.status = SlaTimerStatus.PAUSED.value
            timer.segment_started_at = None
            timer.updated_at = now_iso
            self.repo.update_sla_timer(timer)
            self._event(
                timer, SlaEventKind.TIMER_PAUSED, detail={"reason": reason}
            )
            return self._timer_dict(timer)

    def close_for_request(self, request_id: str, *, reason: str = "") -> dict | None:
        """评审终结（拒绝/完成）：结算后关闭，不再计时。"""
        with self.repo.transaction():
            timer = self.repo.get_sla_timer_by_request(request_id)
            if timer is None:
                return None
            if timer.status == SlaTimerStatus.CLOSED.value:
                return self._timer_dict(timer)
            cal = self._load_timer_calendar(timer)
            now_iso = self.clock.now_iso()
            self._settle(timer, now_iso, cal)
            timer.status = SlaTimerStatus.CLOSED.value
            timer.segment_started_at = None
            timer.updated_at = now_iso
            self.repo.update_sla_timer(timer)
            self._event(
                timer, SlaEventKind.TIMER_CLOSED, detail={"reason": reason}
            )
            return self._timer_dict(timer)

    # --------------------------------------------------------------- 扫描升级
    def scan_due(self) -> list[dict]:
        """扫描运行中计时器；对超时者生成升级事件并通知当前负责人。

        升级事件与计时器标记在一个写事务内落库；通知在提交后发出，
        发送失败保留 notified=False，由后续扫描重试。
        """
        new_escalations: list[EscalationEvent] = []
        with self.repo.transaction():
            now = self.clock.now_utc()
            now_iso = now.isoformat()
            for timer in self.repo.list_running_sla_timers():
                if timer.escalated:
                    continue
                cal = self._load_timer_calendar(timer)
                elapsed = elapsed_work_seconds(
                    timer.consumed_work_seconds,
                    timer.segment_started_at,
                    now,
                    cal,
                )
                if elapsed < timer.budget_seconds:
                    continue

                # 结算到当前时刻并另起一段，超时量随时间继续可观测
                timer.consumed_work_seconds = elapsed
                timer.segment_started_at = now_iso
                timer.due_at = now_iso
                escalation = EscalationEvent(
                    escalation_id=self.ids.new_id("esc"),
                    timer_id=timer.timer_id,
                    request_id=timer.request_id,
                    package_id=timer.package_id,
                    institution_id=timer.institution_id,
                    priority=timer.priority,
                    owner_id=timer.current_owner_id,
                    overdue_seconds=elapsed - timer.budget_seconds,
                    occurred_at=now_iso,
                    notified=False,
                )
                self.repo.insert_escalation(escalation)
                timer.escalated = True
                timer.escalation_event_id = escalation.escalation_id
                timer.updated_at = now_iso
                self.repo.update_sla_timer(timer)
                self._event(
                    timer,
                    SlaEventKind.ESCALATED,
                    detail={
                        "escalation_id": escalation.escalation_id,
                        "owner_id": escalation.owner_id,
                        "overdue_seconds": escalation.overdue_seconds,
                    },
                )
                new_escalations.append(escalation)

        # 提交后再外发通知；失败不回滚升级事件，等待重试。
        # 先发送再构造返回，使 notified 反映本次实际送达结果。
        self._notify(new_escalations)
        retried = self._retry_unnotified(
            skip={e.escalation_id for e in new_escalations}
        )
        return [self._escalation_dict(e) for e in new_escalations] + retried

    def _retry_unnotified(self, *, skip: set[str]) -> list[dict]:
        pending = [
            e
            for e in self.repo.list_escalations()
            if not e.notified and e.escalation_id not in skip
        ]
        self._notify(pending)
        return [self._escalation_dict(e) for e in pending]

    def _notify(self, escalations: list[EscalationEvent]) -> None:
        for esc in escalations:
            try:
                delivered = self.notifier.notify_escalation(
                    owner_id=esc.owner_id,
                    package_id=esc.package_id,
                    request_id=esc.request_id,
                    priority=esc.priority,
                    overdue_seconds=esc.overdue_seconds,
                    occurred_at=esc.occurred_at,
                )
            except Exception:
                delivered = False
            if delivered:
                with self.repo.transaction():
                    self.repo.mark_escalation_notified(esc.escalation_id)
                esc.notified = True

    # ----------------------------------------------------------------- 查询
    def get_timer(self, actor: User, request_id: str) -> dict:
        req = self.repo.get_request(request_id)
        if req is None:
            raise NotFoundError("评审请求不存在")
        timer = self.repo.get_sla_timer_by_request(request_id)
        if timer is None:
            raise NotFoundError("该评审请求没有服务时限计时器")
        self._require_visibility(actor, req)
        return self._timer_dict(timer)

    def list_timers_for_package(self, actor: User, package_id: str) -> list[dict]:
        package = self.repo.get_package(package_id)
        if package is None:
            raise NotFoundError("评审包不存在")
        if (
            actor.institution_id != package.institution_id
            and not actor.has_role(Role.QUALITY_AUTHORITY)
            and not actor.has_role(Role.AUDITOR)
        ):
            reviewer_ids = {
                r.reviewer_id
                for r in self.repo.list_requests_by_package(package_id)
            }
            if actor.user_id not in reviewer_ids:
                raise PermissionDeniedError("不能查看该评审包的服务时限")
        return [self._timer_dict(t) for t in self.repo.list_sla_timers_by_package(package_id)]

    def list_events(self, actor: User, request_id: str) -> list[dict]:
        req = self.repo.get_request(request_id)
        if req is None:
            raise NotFoundError("评审请求不存在")
        timer = self.repo.get_sla_timer_by_request(request_id)
        if timer is None:
            raise NotFoundError("该评审请求没有服务时限计时器")
        self._require_visibility(actor, req)
        return [self._event_dict(e) for e in self.repo.list_sla_events(timer.timer_id)]

    def _require_visibility(self, actor: User, req: ReviewRequest) -> None:
        if actor.has_role(Role.QUALITY_AUTHORITY) or actor.has_role(Role.AUDITOR):
            return
        if actor.institution_id == req.institution_id:
            return
        if req.reviewer_id == actor.user_id:
            return
        raise PermissionDeniedError("不能查看该评审请求的服务时限")

    # ----------------------------------------------------------------- 内部
    def _load_timer_calendar(self, timer: SlaTimer) -> CaseCalendar:
        return (
            self.repo.get_calendar(timer.calendar_id)
            or self._resolve_calendar(timer.institution_id)
        )

    def _settle(self, timer: SlaTimer, at_iso: str, cal: CaseCalendar) -> None:
        if timer.segment_started_at is not None:
            timer.consumed_work_seconds += working_seconds_between(
                timer.segment_started_at, at_iso, cal
            )

    def _event(
        self,
        timer: SlaTimer,
        kind: SlaEventKind,
        *,
        detail: dict | None = None,
        actor_id: str | None = None,
    ) -> None:
        self.repo.insert_sla_event(
            SlaEvent(
                event_id=self.ids.new_id("sev"),
                timer_id=timer.timer_id,
                request_id=timer.request_id,
                package_id=timer.package_id,
                institution_id=timer.institution_id,
                kind=kind.value,
                at=self.clock.now_iso(),
                actor_id=actor_id,
                detail=detail or {},
            )
        )

    def _timer_dict(self, timer: SlaTimer) -> dict:
        cal = self._load_timer_calendar(timer)
        now = self.clock.now_utc()
        segment = (
            timer.segment_started_at
            if timer.status == SlaTimerStatus.RUNNING.value
            else None
        )
        elapsed = elapsed_work_seconds(
            timer.consumed_work_seconds, segment, now, cal
        )
        remaining = timer.budget_seconds - elapsed
        return {
            "timer_id": timer.timer_id,
            "request_id": timer.request_id,
            "package_id": timer.package_id,
            "priority": timer.priority,
            "calendar_id": timer.calendar_id,
            "status": timer.status,
            "current_owner_id": timer.current_owner_id,
            "budget_seconds": timer.budget_seconds,
            "elapsed_work_seconds": round(elapsed, 3),
            "remaining_work_seconds": round(max(0.0, remaining), 3),
            "overdue": timer.escalated or remaining < 0,
            "due_at": timer.due_at,
            "escalated": timer.escalated,
            "escalation_event_id": timer.escalation_event_id,
        }

    @staticmethod
    def _escalation_dict(e: EscalationEvent) -> dict:
        return {
            "escalation_id": e.escalation_id,
            "timer_id": e.timer_id,
            "request_id": e.request_id,
            "package_id": e.package_id,
            "priority": e.priority,
            "owner_id": e.owner_id,
            "overdue_seconds": round(e.overdue_seconds, 3),
            "occurred_at": e.occurred_at,
            "notified": e.notified,
        }

    @staticmethod
    def _event_dict(e: SlaEvent) -> dict:
        return {
            "event_id": e.event_id,
            "kind": e.kind,
            "at": e.at,
            "actor_id": e.actor_id,
            "detail": e.detail,
        }

    @staticmethod
    def _calendar_dict(c: CaseCalendar) -> dict:
        return {
            "calendar_id": c.calendar_id,
            "institution_id": c.institution_id,
            "timezone": c.timezone,
            "work_start": c.work_start,
            "work_end": c.work_end,
            "working_weekdays": list(c.working_weekdays),
            "holidays": list(c.holidays),
        }
