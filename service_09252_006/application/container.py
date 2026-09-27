"""应用装配：把仓库、端口与各服务组合成一个 ApplicationContext。"""
from __future__ import annotations

from ..application.evidence_service import EvidenceService
from ..application.package_service import PackageService
from ..application.review_service import ReviewService
from ..application.sla_service import SlaService
from ..application.ports import Clock, IdGenerator, SystemClock, Uuid4IdGenerator
from ..application.repository import Repository
from ..domain.sla import CaseCalendar
from ..persistence.sqlite_repo import SqliteRepository


class ApplicationContext:
    def __init__(
        self,
        db_path: str,
        *,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        calendar: CaseCalendar | None = None,
        calendar_path: str | None = None,
    ) -> None:
        self.db_path = db_path
        self.repo: Repository = SqliteRepository(db_path)
        self.clock: Clock = clock or SystemClock()
        self.ids: IdGenerator = ids or Uuid4IdGenerator()
        # 案件日历：显式实例 > JSON 文件 > 内置默认（周一至周五 09:00-18:00 上海）
        if calendar is not None:
            self.calendar = calendar
        elif calendar_path:
            self.calendar = CaseCalendar.load(calendar_path)
        else:
            self.calendar = CaseCalendar.default()
        self.evidence = EvidenceService(self.repo, self.clock, self.ids)
        self.packages = PackageService(self.repo, self.clock, self.ids)
        self.reviews = ReviewService(self.repo, self.clock, self.ids)
        self.sla = SlaService(self.repo, self.clock, self.ids, self.calendar)

    def close(self) -> None:
        self.repo.close()

    def __enter__(self) -> "ApplicationContext":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
