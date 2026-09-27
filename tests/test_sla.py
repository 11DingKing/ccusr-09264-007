"""评审服务时限（SLA）：优先级计时、转交暂停、超时升级与通知。

核心断言：转交（暂停）期间计时不会继续累加。
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from service_09252_006.domain.enums import Role
from service_09252_006.domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from service_09252_006.domain.sla import CaseCalendar
from tests.flow import seal_new_package
from tests.support import Harness

SH = ZoneInfo("Asia/Shanghai")
MON_0900 = datetime(2026, 9, 28, 9, 0, tzinfo=SH)   # 周一 09:00 上海
FRI_0900 = datetime(2026, 10, 2, 9, 0, tzinfo=SH)   # 周五 09:00 上海


class SlaTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(moment=MON_0900)
        self.admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)
        self.authority = self.h.user(
            "auth", Role.QUALITY_AUTHORITY, institution_id=None
        )
        self.reviewer = self.h.user("rev-1", Role.REVIEWER, institution_id="inst-ext")
        self.reviewer2 = self.h.user("rev-2", Role.REVIEWER, institution_id="inst-ext2")
        self.sealed = seal_new_package(self.h, self.admin)
        self.pid = self.sealed.package_id

    def tearDown(self) -> None:
        self.h.close()

    def _request_id(self, reviewer=None) -> str:
        reviewer = reviewer or self.reviewer
        req = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid, reviewer_id=reviewer.user_id
        )
        return req["request_id"]

    def _open_case(self, priority="P2", owner_id=None, reviewer=None) -> dict:
        return self.h.ctx.sla.open_case(
            self.authority,
            request_id=self._request_id(reviewer),
            priority=priority,
            owner_id=owner_id,
        )

    def _recalc(self, case_id: str) -> dict:
        return self.h.ctx.sla.recalculate(self.authority, case_id=case_id)


class TransferPauseTests(SlaTestBase):
    def test_transfer_pause_stops_elapsed_accumulation(self) -> None:
        """转交期间计时不会继续累加：暂停多久，已用时长都保持不变。"""
        case = self._open_case("P2")
        cid = case["case_id"]

        self.h.clock.advance(hours=2)  # 周一 09:00 -> 11:00，正常计时
        self.assertEqual(self._recalc(cid)["elapsed_business_seconds"], 2 * 3600)

        self.h.ctx.sla.begin_transfer(self.authority, case_id=cid, reason="改派评审人")
        self.h.clock.advance(hours=5)  # 转交 5 小时
        during = self._recalc(cid)
        self.assertTrue(during["paused"])
        self.assertEqual(during["status"], "paused")
        self.assertIsNone(during["due_at_utc"])  # 暂停中截止时刻挂起
        self.assertEqual(
            during["elapsed_business_seconds"], 2 * 3600,
            "转交期间已用时长不得累加",
        )

        done = self.h.ctx.sla.complete_transfer(
            self.authority, case_id=cid, new_owner_id=self.reviewer2.user_id
        )
        self.assertEqual(done["new_owner_id"], self.reviewer2.user_id)

        self.h.clock.advance(hours=1)  # 恢复计时 1 小时
        after = self._recalc(cid)
        self.assertFalse(after["paused"])
        self.assertEqual(after["elapsed_business_seconds"], 3 * 3600)
        self.assertEqual(after["owner_id"], self.reviewer2.user_id)

        # 暂停区间已写入 SQLite：一条 [11:00, 16:00) 的已关闭区间
        pauses = self.h.repo.list_sla_pauses(cid)
        self.assertEqual(len(pauses), 1)
        self.assertEqual(pauses[0].reason, "改派评审人")
        self.assertIsNotNone(pauses[0].ended_at)

    def test_pause_spanning_weekend_never_counts(self) -> None:
        """跨周末的转交：无论墙上时间过多久，工作时间都不累计。"""
        self.h.clock.set(FRI_0900)
        case = self._open_case("P2")
        cid = case["case_id"]
        self.h.clock.advance(hours=2)  # 周五 09:00 -> 11:00
        self.h.ctx.sla.begin_transfer(self.authority, case_id=cid)

        self.h.clock.set(datetime(2026, 10, 5, 11, 0, tzinfo=SH))  # 下周一 11:00
        during = self._recalc(cid)
        self.assertEqual(during["elapsed_business_seconds"], 2 * 3600)

        self.h.ctx.sla.complete_transfer(
            self.authority, case_id=cid, new_owner_id=self.reviewer2.user_id
        )
        self.h.clock.advance(hours=1)
        self.assertEqual(self._recalc(cid)["elapsed_business_seconds"], 3 * 3600)

    def test_due_at_shifts_by_pause_length(self) -> None:
        """重算截止时刻：转交 1 小时，推算截止时刻顺延 1 小时。"""
        case = self._open_case("P1")  # 4 工作小时
        cid = case["case_id"]
        due = self._recalc(cid)["due_at_utc"]
        self.assertEqual(
            due, datetime(2026, 9, 28, 13, 0, tzinfo=SH).astimezone(timezone.utc).isoformat()
        )

        self.h.clock.advance(hours=2)  # 11:00
        self.h.ctx.sla.begin_transfer(self.authority, case_id=cid)
        self.h.clock.advance(hours=1)  # 转交 1 小时
        self.h.ctx.sla.complete_transfer(
            self.authority, case_id=cid, new_owner_id=self.reviewer2.user_id
        )
        shifted = self._recalc(cid)
        self.assertEqual(shifted["elapsed_business_seconds"], 2 * 3600)
        self.assertEqual(
            shifted["due_at_utc"],
            datetime(2026, 9, 28, 14, 0, tzinfo=SH).astimezone(timezone.utc).isoformat(),
        )

    def test_second_begin_transfer_conflicts(self) -> None:
        case = self._open_case("P2")
        cid = case["case_id"]
        self.h.ctx.sla.begin_transfer(self.authority, case_id=cid)
        with self.assertRaises(ConflictError):
            self.h.ctx.sla.begin_transfer(self.authority, case_id=cid)

    def test_complete_transfer_without_begin_conflicts(self) -> None:
        case = self._open_case("P2")
        with self.assertRaises(ConflictError):
            self.h.ctx.sla.complete_transfer(
                self.authority, case_id=case["case_id"],
                new_owner_id=self.reviewer2.user_id,
            )


class CalendarTests(SlaTestBase):
    def test_business_time_excludes_nights_and_weekends(self) -> None:
        """案件日历只累计工作窗口：夜间与周末不计时。"""
        self.h.clock.set(datetime(2026, 10, 2, 16, 0, tzinfo=SH))  # 周五 16:00
        case = self._open_case("P2")
        self.h.clock.set(datetime(2026, 10, 5, 10, 0, tzinfo=SH))  # 周一 10:00
        view = self._recalc(case["case_id"])
        # 周五 16:00-18:00（2h）+ 周一 09:00-10:00（1h），周末不计
        self.assertEqual(view["elapsed_business_seconds"], 3 * 3600)

    def test_priority_determines_limit(self) -> None:
        p1 = self._open_case("P1")
        self.assertEqual(self._recalc(p1["case_id"])["limit_business_seconds"], 4 * 3600)
        p4 = self._open_case("P4", reviewer=self.reviewer2)
        self.assertEqual(self._recalc(p4["case_id"])["limit_business_seconds"], 72 * 3600)
        with self.assertRaises(ValidationError):
            self._open_case("P9")

    def test_calendar_loaded_from_json_file(self) -> None:
        """Python 从 JSON 读取案件日历：节假日与自定义优先级时限生效。"""
        calendar_doc = {
            "timezone": "Asia/Shanghai",
            "work_windows": {
                day: [["09:00", "18:00"]]
                for day in ("mon", "tue", "wed", "thu", "fri")
            },
            "holidays": ["2026-10-01"],
            "priority_limits_hours": {"P1": 2},
        }
        fd, path = tempfile.mkstemp(prefix="qe-calendar-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(calendar_doc, fh)
            calendar = CaseCalendar.load(path)
        finally:
            os.unlink(path)

        self.assertEqual(calendar.limit_seconds("P1"), 2 * 3600)
        h = Harness(moment=datetime(2026, 9, 30, 17, 0, tzinfo=SH), calendar=calendar)
        try:
            admin = h.user("admin-b", Role.INSTITUTION_ADMIN)
            authority = h.user("auth-b", Role.QUALITY_AUTHORITY, institution_id=None)
            reviewer = h.user("rev-b", Role.REVIEWER, institution_id="inst-ext")
            sealed = seal_new_package(h, admin)
            req = h.ctx.reviews.assign_reviewer(
                authority, package_id=sealed.package_id, reviewer_id=reviewer.user_id
            )
            case = h.ctx.sla.open_case(
                authority, request_id=req["request_id"], priority="P1"
            )
            # 周三 17:00 -> 周五 10:00：周三 1h + 周四节假日 0h + 周五 1h = 2h
            h.clock.set(datetime(2026, 10, 2, 10, 0, tzinfo=SH))
            view = h.ctx.sla.recalculate(authority, case_id=case["case_id"])
            self.assertEqual(view["elapsed_business_seconds"], 2 * 3600)
            self.assertTrue(view["breached"])
        finally:
            h.close()

    def test_calendar_rejects_bad_definition(self) -> None:
        with self.assertRaises(ValueError):
            CaseCalendar.from_dict({"work_windows": {"mon": [["18:00", "09:00"]]}})
        with self.assertRaises(ValueError):
            CaseCalendar.from_dict({"timezone": "Nowhere/Special"})
        with self.assertRaises(ValueError):
            CaseCalendar.from_dict({"work_windows": {"funday": [["09:00", "18:00"]]}})


class EscalationTests(SlaTestBase):
    def test_sweep_escalates_and_notifies_current_owner(self) -> None:
        """超时生成升级事件并通知当前负责人（转交后的新负责人）。"""
        case = self._open_case("P1")
        cid = case["case_id"]
        # 立案即转交给 rev-2，随后计时走满 P1 的 4 工作小时
        self.h.ctx.sla.begin_transfer(self.authority, case_id=cid, reason="改派")
        self.h.ctx.sla.complete_transfer(
            self.authority, case_id=cid, new_owner_id=self.reviewer2.user_id
        )
        self.h.clock.advance(hours=5)  # 周一 09:00 -> 14:00，全在工作窗口内

        created = self.h.ctx.sla.sweep_escalations(self.authority)
        self.assertEqual(len(created), 1)
        event = created[0]
        self.assertEqual(event["case_id"], cid)
        self.assertEqual(event["owner_id"], self.reviewer2.user_id)
        self.assertEqual(event["limit_business_seconds"], 4 * 3600)
        self.assertGreaterEqual(event["elapsed_business_seconds"], 4 * 3600)

        # 升级事件已写入 SQLite
        stored = self.h.repo.list_escalations(cid)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].escalation_id, event["escalation_id"])

        # 通知发给当前负责人 rev-2，而不是原负责人 rev-1
        notices_new = self.h.ctx.sla.list_notifications(self.reviewer2)
        self.assertEqual(len(notices_new), 1)
        self.assertEqual(notices_new[0]["kind"], "sla_escalation")
        self.assertIn(cid, notices_new[0]["message"])
        self.assertEqual(self.h.ctx.sla.list_notifications(self.reviewer), [])

        # 案件标记升级时刻
        self.assertIsNotNone(self._recalc(cid)["escalated_at"])

    def test_sweep_is_idempotent(self) -> None:
        case = self._open_case("P1")
        self.h.clock.advance(hours=5)
        first = self.h.ctx.sla.sweep_escalations(self.authority)
        second = self.h.ctx.sla.sweep_escalations(self.authority)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(len(self.h.repo.list_escalations(case["case_id"])), 1)

    def test_no_escalation_while_transfer_paused(self) -> None:
        """转交期间计时暂停：墙上时间再久也不触发升级。"""
        case = self._open_case("P1")
        cid = case["case_id"]
        self.h.clock.advance(hours=3)  # 已用 3h，距 P1 上限还差 1h
        self.h.ctx.sla.begin_transfer(self.authority, case_id=cid)
        self.h.clock.advance(hours=10)  # 转交 10 小时
        self.assertEqual(self.h.ctx.sla.sweep_escalations(self.authority), [])

        self.h.ctx.sla.complete_transfer(
            self.authority, case_id=cid, new_owner_id=self.reviewer2.user_id
        )
        # 恢复后推进到次日工作时间内 1 小时，累计 4h 达到上限
        self.h.clock.set(datetime(2026, 9, 29, 10, 0, tzinfo=SH))
        created = self.h.ctx.sla.sweep_escalations(self.authority)
        self.assertEqual(len(created), 1)

    def test_closed_case_not_escalated(self) -> None:
        case = self._open_case("P1")
        cid = case["case_id"]
        self.h.clock.advance(hours=1)
        self.h.ctx.sla.close_case(self.authority, case_id=cid)
        self.h.clock.advance(hours=10)
        self.assertEqual(self.h.ctx.sla.sweep_escalations(self.authority), [])
        view = self._recalc(cid)
        self.assertEqual(view["status"], "closed")
        self.assertEqual(view["elapsed_business_seconds"], 3600)  # 结案后冻结


class GuardTests(SlaTestBase):
    def test_open_case_replays_for_same_request(self) -> None:
        rid = self._request_id()
        first = self.h.ctx.sla.open_case(
            self.authority, request_id=rid, priority="P2"
        )
        second = self.h.ctx.sla.open_case(
            self.authority, request_id=rid, priority="P1"
        )
        self.assertEqual(first["case_id"], second["case_id"])
        self.assertTrue(second["replayed"])

    def test_open_case_requires_active_request(self) -> None:
        rid = self._request_id()
        self.h.ctx.reviews.cancel_request(self.authority, request_id=rid)
        with self.assertRaises(ConflictError):
            self.h.ctx.sla.open_case(self.authority, request_id=rid, priority="P2")

    def test_close_guards(self) -> None:
        case = self._open_case("P2")
        cid = case["case_id"]
        self.h.ctx.sla.begin_transfer(self.authority, case_id=cid)
        with self.assertRaises(ConflictError):
            self.h.ctx.sla.close_case(self.authority, case_id=cid)  # 转交中
        self.h.ctx.sla.complete_transfer(
            self.authority, case_id=cid, new_owner_id=self.reviewer2.user_id
        )
        self.h.ctx.sla.close_case(self.authority, case_id=cid)
        with self.assertRaises(ConflictError):
            self.h.ctx.sla.begin_transfer(self.authority, case_id=cid)  # 已结案

    def test_unknown_case_raises_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self._recalc("sla_404")

    def test_permissions(self) -> None:
        case = self._open_case("P2")
        cid = case["case_id"]
        # 评审人不能立案/转交/扫描
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.sla.open_case(
                self.reviewer, request_id=self._request_id(), priority="P2"
            )
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.sla.begin_transfer(self.reviewer, case_id=cid)
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.sla.sweep_escalations(self.reviewer)
        # 他机构管理员不能操作本机构案件
        other_admin = self.h.user("admin-x", Role.INSTITUTION_ADMIN, institution_id="inst-x")
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.sla.begin_transfer(other_admin, case_id=cid)
        # 无关人员不能查看；当前负责人可以查看
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.sla.recalculate(other_admin, case_id=cid)
        view = self.h.ctx.sla.recalculate(self.reviewer, case_id=cid)
        self.assertEqual(view["case_id"], cid)


if __name__ == "__main__":
    unittest.main()
