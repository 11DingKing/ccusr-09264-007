"""应用装配：把仓库、端口与各服务组合成一个 ApplicationContext。"""
from __future__ import annotations

from ..application.evidence_service import EvidenceService
from ..application.package_service import PackageService
from ..application.review_service import ReviewService
from ..application.sla_service import SlaService
from ..application.ports import (
    Clock,
    CollectingNotifier,
    IdGenerator,
    Notifier,
    SystemClock,
    Uuid4IdGenerator,
)
from ..application.repository import Repository
from ..persistence.sqlite_repo import SqliteRepository


class ApplicationContext:
    def __init__(
        self,
        db_path: str,
        *,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.db_path = db_path
        self.repo: Repository = SqliteRepository(db_path)
        self.clock: Clock = clock or SystemClock()
        self.ids: IdGenerator = ids or Uuid4IdGenerator()
        self.notifier: Notifier = notifier or CollectingNotifier()
        self.evidence = EvidenceService(self.repo, self.clock, self.ids)
        self.packages = PackageService(self.repo, self.clock, self.ids)
        self.reviews = ReviewService(self.repo, self.clock, self.ids)
        self.sla = SlaService(self.repo, self.clock, self.ids, self.notifier)
        # 评审状态机联动服务时限：分配开始、取消/拒绝暂停、完成关闭
        self.reviews.sla = self.sla

    def close(self) -> None:
        self.repo.close()

    def __enter__(self) -> "ApplicationContext":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
