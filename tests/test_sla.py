"""评审服务时限（SLA）：按优先级与案件日历计时、转交暂停、超时升级。

核心保证（对应业务要求）：
- 时限只在工作时段累计，周末/节假日不消耗预算；
- 取消/拒绝后等待改派即暂停，【转交期间计时不会继续累加】；
- 转交给新负责人时继承此前已用工作时间，转交窗口本身不计时；
- 超时生成升级事件（落 SQLite）并通知【当前】负责人，重复扫描幂等；
- 暂停/恢复/关闭/升级事件全部可从仓库读回。
"""
from __future__ import annotations

import unittest

from service_09252_006.application.ports import CollectingNotifier
from service_09252_006.domain.enums import (
    CasePriority,
    RequestStatus,
    Role,
    SlaEventKind,
    SlaTimerStatus,
)
from service_09252_006.domain.models import CaseCalendar
from tests.flow import seal_new_package
from tests.support import Harness


def _work_calendar() -> CaseCalendar:
    # 与默认兜底一致：上海 09:00-17:00，周一至周五
    return CaseCalendar(
        calendar_id="cal-a",
        institution_id="inst-a",
        timezone="Asia/Shanghai",
        work_start="09:00",
        work_end="17:00",
        working_weekdays=(1, 2, 3, 4, 5),
        holidays=("2026-09-28",),  # 周一放假
    )


class SlaTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()  # 固定 2026-09-25 09:00 上海（周五）
        self.admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)
        self.authority = self.h.user(
            "auth", Role.QUALITY_AUTHORITY, institution_id=None
        )
        self.rev1 = self.h.user("rev-1", Role.REVIEWER, institution_id="inst-ext")
        self.rev2 = self.h.user("rev-2", Role.REVIEWER, institution_id="inst-ext2")
        self.h.repo.upsert_calendar(_work_calendar())
        self.pid = seal_new_package(self.h, self.admin).package_id
        self.sla = self.h.ctx.sla

    def tearDown(self) -> None:
        self.h.close()

    def _timer(self, request_id: str):
        return self.h.repo.get_sla_timer_by_request(request_id)

    def test_paused_timer_does_not_accumulate_during_transfer(self) -> None:
        # 特急 4 工作小时；周五 09:00 起，11:00 时已用 2 小时
        r1 = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.rev1.user_id,
            priority=CasePriority.URGENT.value,
        )
        t1 = self._timer(r1["request_id"])
        self.assertEqual(t1.status, SlaTimerStatus.RUNNING.value)
        self.assertEqual(t1.budget_seconds, 4 * 3600)

        self.h.clock.advance(hours=2)  # 周五 11:00（工作时段内）
        view = self.sla.get_timer(self.authority, r1["request_id"])
        self.assertAlmostEqual(view["elapsed_work_seconds"], 2 * 3600, delta=1)

        # 取消分配 => 等待改派，暂停计时并把 2 小时结算落库
        self.h.ctx.reviews.cancel_request(
            self.authority, request_id=r1["request_id"], reason="转交他人"
        )
        t1 = self._timer(r1["request_id"])
        self.assertEqual(t1.status, SlaTimerStatus.PAUSED.value)
        self.assertAlmostEqual(t1.consumed_work_seconds, 2 * 3600, delta=1)
        self.assertIsNone(t1.segment_started_at)

        # 关键断言：即使在【同一工作日】内跨过 4 个工作小时，再加整个周末，
        # 暂停的计时器已用时间必须原样冻结，不继续累加。
        self.h.clock.advance(hours=4)        # 周五 15:00
        self.h.clock.advance(days=3)         # 跨周六日 -> 周一 15:00
        t1 = self._timer(r1["request_id"])
        self.assertEqual(t1.status, SlaTimerStatus.PAUSED.value)
        self.assertAlmostEqual(t1.consumed_work_seconds, 2 * 3600, delta=1)

        # 暂停事件已写 SQLite
        kinds = [e.kind for e in self.h.repo.list_sla_events(t1.timer_id)]
        self.assertIn(SlaEventKind.TIMER_PAUSED.value, kinds)

    def test_transfer_carries_consumed_time_and_gap_is_ignored(self) -> None:
        # rev1 工作 2 小时后被取消（暂停）；改派窗口落在同一工作日内
        r1 = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.rev1.user_id, priority=CasePriority.URGENT.value,
        )
        self.h.clock.advance(hours=2)  # 周五 11:00，已用 2h
        self.h.ctx.reviews.cancel_request(
            self.authority, request_id=r1["request_id"]
        )
        self.h.clock.advance(hours=4)  # 周五 15:00 —— 转交窗口 4 个工作小时

        # 改派 rev2：新计时器继承 2h；若错误地把窗口计入会是 6h
        r2 = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.rev2.user_id, priority=CasePriority.URGENT.value,
        )
        t2 = self._timer(r2["request_id"])
        self.assertEqual(t2.status, SlaTimerStatus.RUNNING.value)
        self.assertEqual(t2.current_owner_id, self.rev2.user_id)
        self.assertAlmostEqual(t2.consumed_work_seconds, 2 * 3600, delta=1)
        # 接手瞬间的视图已用时间必须恰好等于结转值（窗口未计入）
        view = self.sla.get_timer(self.authority, r2["request_id"])
        self.assertAlmostEqual(view["elapsed_work_seconds"], 2 * 3600, delta=1)
        self.assertAlmostEqual(view["remaining_work_seconds"], 2 * 3600, delta=1)
        # 剩余 2h：周五 15:00 + 2 工作小时 = 周五 17:00（09:00 UTC）
        self.assertEqual(t2.due_at, "2026-09-25T09:00:00+00:00")

        # 旧计时器已关闭（转出），事件含 started/paused/closed
        t1 = self._timer(r1["request_id"])
        self.assertEqual(t1.status, SlaTimerStatus.CLOSED.value)
        kinds1 = [e.kind for e in self.h.repo.list_sla_events(t1.timer_id)]
        self.assertEqual(
            kinds1,
            [
                SlaEventKind.TIMER_STARTED.value,
                SlaEventKind.TIMER_PAUSED.value,
                SlaEventKind.TIMER_CLOSED.value,
            ],
        )
        kinds2 = [e.kind for e in self.h.repo.list_sla_events(t2.timer_id)]
        self.assertEqual(kinds2, [SlaEventKind.TIMER_STARTED.value])

    def test_weekend_not_counted_without_transfer(self) -> None:
        # 周五 09:00 起，常规预算 24h；跨过整个周末不消耗预算
        r1 = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.rev1.user_id, priority=CasePriority.NORMAL.value,
        )
        self.h.clock.advance(days=3)  # 周一 09:00 上海
        view = self.sla.get_timer(self.authority, r1["request_id"])
        # 仅周五 09:00-17:00 的 8 小时被计入
        self.assertAlmostEqual(view["elapsed_work_seconds"], 8 * 3600, delta=1)

    def test_declined_assignment_pauses_timer(self) -> None:
        r1 = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.rev1.user_id, priority=CasePriority.HIGH.value,
        )
        self.h.clock.advance(hours=1)
        self.h.ctx.reviews.respond_assignment(
            self.rev1, request_id=r1["request_id"], accept=False
        )
        t1 = self._timer(r1["request_id"])
        self.assertEqual(t1.status, SlaTimerStatus.PAUSED.value)
        # 拒绝后再等 8 小时（含工作时段），不继续累加
        self.h.clock.advance(hours=8)
        t1 = self._timer(r1["request_id"])
        self.assertAlmostEqual(t1.consumed_work_seconds, 1 * 3600, delta=1)

    def test_parallel_reviewers_keep_independent_budgets(self) -> None:
        # rev1、rev2 并行评审：两人同时在计时，各自独立预算、互不结转
        r1 = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.rev1.user_id, priority=CasePriority.URGENT.value,
        )
        r2 = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.rev2.user_id, priority=CasePriority.URGENT.value,
        )
        self.h.clock.advance(hours=2)
        t1, t2 = self._timer(r1["request_id"]), self._timer(r2["request_id"])
        self.assertEqual(t1.status, SlaTimerStatus.RUNNING.value)
        self.assertEqual(t2.status, SlaTimerStatus.RUNNING.value)
        self.assertAlmostEqual(t1.consumed_work_seconds, 0, delta=1)
        self.assertAlmostEqual(t2.consumed_work_seconds, 0, delta=1)
        v1 = self.sla.get_timer(self.authority, r1["request_id"])
        v2 = self.sla.get_timer(self.authority, r2["request_id"])
        self.assertAlmostEqual(v1["elapsed_work_seconds"], 2 * 3600, delta=1)
        self.assertAlmostEqual(v2["elapsed_work_seconds"], 2 * 3600, delta=1)

    def test_additional_reviewer_while_another_runs_gets_fresh_budget(self) -> None:
        # rev1 用 2h 后被取消（暂停）；rev2 在 rev1 暂停期间接手（转交，继承2h）；
        # rev2 仍在运行时又加派 rev3 -> rev3 不应继承 rev1 的 2h。
        r1 = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.rev1.user_id, priority=CasePriority.URGENT.value,
        )
        self.h.clock.advance(hours=2)
        self.h.ctx.reviews.cancel_request(
            self.authority, request_id=r1["request_id"]
        )
        self.h.clock.advance(hours=1)
        r2 = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.rev2.user_id, priority=CasePriority.URGENT.value,
        )
        t2 = self._timer(r2["request_id"])
        self.assertAlmostEqual(t2.consumed_work_seconds, 2 * 3600, delta=1)

        # rev3 在 rev2 运行中加派：全新预算
        r3 = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.h.user(
                "rev-3", Role.REVIEWER, institution_id="inst-ext3"
            ).user_id,
            priority=CasePriority.URGENT.value,
        )
        t3 = self._timer(r3["request_id"])
        self.assertEqual(t3.consumed_work_seconds, 0)
        self.assertEqual(t3.current_owner_id, "rev-3")


class SlaCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)
        self.authority = self.h.user(
            "auth", Role.QUALITY_AUTHORITY, institution_id=None
        )
        self.rev1 = self.h.user("rev-1", Role.REVIEWER, institution_id="inst-ext")
        self.pid = seal_new_package(self.h, self.admin).package_id

    def tearDown(self) -> None:
        self.h.close()

    def test_completing_review_closes_timer(self) -> None:
        r1 = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.rev1.user_id, priority=CasePriority.LOW.value,
        )
        self.h.ctx.reviews.respond_assignment(
            self.rev1, request_id=r1["request_id"], accept=True
        )
        self.h.clock.advance(hours=2)
        self.h.ctx.reviews.submit_verdict(
            self.rev1, request_id=r1["request_id"], verdict="approve"
        )
        t1 = self.h.repo.get_sla_timer_by_request(r1["request_id"])
        self.assertEqual(t1.status, SlaTimerStatus.CLOSED.value)
        self.assertIsNone(t1.segment_started_at)
        # 关闭后扫描不再升级它
        self.h.clock.advance(days=30)
        self.assertEqual(self.h.ctx.sla.scan_due(), [])


class SlaEscalationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        # Harness 使用默认收集器；这里替换成可断言的通知收集器
        self.notifier = CollectingNotifier()
        self.h.ctx.sla.notifier = self.notifier
        self.admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)
        self.authority = self.h.user(
            "auth", Role.QUALITY_AUTHORITY, institution_id=None
        )
        self.rev1 = self.h.user("rev-1", Role.REVIEWER, institution_id="inst-ext")
        self.rev2 = self.h.user("rev-2", Role.REVIEWER, institution_id="inst-ext2")
        self.pid = seal_new_package(self.h, self.admin).package_id

    def tearDown(self) -> None:
        self.h.close()

    def test_timeout_escalates_once_and_notifies_current_owner(self) -> None:
        # rev1 用掉 2h 后转交给 rev2（剩余 2h，周五 17:00 到期）
        r1 = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.rev1.user_id, priority=CasePriority.URGENT.value,
        )
        self.h.clock.advance(hours=2)
        self.h.ctx.reviews.cancel_request(
            self.authority, request_id=r1["request_id"]
        )
        self.h.clock.advance(hours=4)  # 周五 15:00 改派 rev2
        r2 = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.rev2.user_id, priority=CasePriority.URGENT.value,
        )
        # 到期前扫描：无升级
        self.assertEqual(self.h.ctx.sla.scan_due(), [])

        # 走到下周一 10:00：rev2 段周五 15-17(2h)+周一 09-10(1h)，共 5h，超 1h
        self.h.clock.advance(hours=4)   # 周五 19:00
        self.h.clock.advance(days=2)    # 周日 19:00
        self.h.clock.advance(hours=15)  # 周一 10:00 上海
        escalations = self.h.ctx.sla.scan_due()
        self.assertEqual(len(escalations), 1)
        esc = escalations[0]
        # 通知的是【当前负责人】rev2，而不是原负责人 rev1
        self.assertEqual(esc["owner_id"], self.rev2.user_id)
        self.assertEqual(esc["request_id"], r2["request_id"])
        self.assertAlmostEqual(esc["overdue_seconds"], 3600, delta=1)
        self.assertTrue(esc["notified"])
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertEqual(self.notifier.messages[0]["owner_id"], self.rev2.user_id)

        # 升级事件已落 SQLite，计时器已标记
        rows = self.h.repo.list_escalations(self.pid)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].notified)
        t2 = self.h.repo.get_sla_timer_by_request(r2["request_id"])
        self.assertTrue(t2.escalated)
        self.assertEqual(t2.escalation_event_id, esc["escalation_id"])
        kinds = [
            e.kind
            for e in self.h.repo.list_sla_events(t2.timer_id)
        ]
        self.assertIn("escalated", kinds)

        # 重复扫描幂等：不再生成第二条升级、不重复通知
        again = self.h.ctx.sla.scan_due()
        self.assertEqual(again, [])
        self.assertEqual(len(self.notifier.messages), 1)

    def test_failed_notification_is_retried(self) -> None:
        class FlakyNotifier(CollectingNotifier):
            def __init__(self):
                super().__init__()
                self.fail_once = True

            def notify_escalation(self, **kwargs):
                if self.fail_once:
                    self.fail_once = False
                    return False
                return super().notify_escalation(**kwargs)

        flaky = FlakyNotifier()
        self.h.ctx.sla.notifier = flaky

        r1 = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.rev1.user_id, priority=CasePriority.URGENT.value,
        )
        self.h.clock.advance(hours=5)  # 超过 4h 预算
        first = self.h.ctx.sla.scan_due()
        self.assertEqual(len(first), 1)
        self.assertFalse(first[0]["notified"])
        self.assertFalse(self.h.repo.list_escalations(self.pid)[0].notified)

        # 下次扫描补发成功（不会新建升级事件）
        second = self.h.ctx.sla.scan_due()
        self.assertEqual(len(second), 1)
        self.assertTrue(second[0]["notified"])
        self.assertEqual(len(self.h.repo.list_escalations(self.pid)), 1)


class SlaCalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)
        self.authority = self.h.user(
            "auth", Role.QUALITY_AUTHORITY, institution_id=None
        )
        self.rev1 = self.h.user("rev-1", Role.REVIEWER, institution_id="inst-ext")

    def tearDown(self) -> None:
        self.h.close()

    def test_holiday_is_skipped(self) -> None:
        # 周一(09-28)放假；常规 24h = 周五8 + 周二8 + 周三8
        self.h.repo.upsert_calendar(_work_calendar())
        pid = seal_new_package(self.h, self.admin).package_id
        r1 = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=pid,
            reviewer_id=self.rev1.user_id, priority=CasePriority.NORMAL.value,
        )
        t1 = self.h.repo.get_sla_timer_by_request(r1["request_id"])
        # 周五 09:00 + 24 工作小时，跳过周一假期 => 周三(09-30) 17:00 上海
        self.assertEqual(t1.due_at, "2026-09-30T09:00:00+00:00")

    def test_configure_calendar_validates_inputs(self) -> None:
        from service_09252_006.domain.errors import ValidationError

        with self.assertRaises(ValidationError):
            self.h.ctx.sla.configure_calendar(
                self.authority, calendar_id="bad", timezone="Not/AZone"
            )
        with self.assertRaises(ValidationError):
            self.h.ctx.sla.configure_calendar(
                self.authority, calendar_id="bad", timezone="Asia/Shanghai",
                work_start="17:00", work_end="09:00",
            )


if __name__ == "__main__":
    unittest.main()
