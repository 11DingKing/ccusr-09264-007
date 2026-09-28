"""可替换端口：时钟与标识生成。

应用服务只依赖这些抽象，测试可注入固定时钟/可预测 ID，
从而稳定复现状态变化与跨时区场景。
"""
from __future__ import annotations

import abc
import uuid
from datetime import datetime, timezone


class Clock(abc.ABC):
    @abc.abstractmethod
    def now_utc(self) -> datetime:
        """返回带 tzinfo 的当前 UTC 时刻。"""

    def now_iso(self) -> str:
        return self.now_utc().isoformat()


class SystemClock(Clock):
    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock(Clock):
    """测试用：固定在某个时刻，可手动推进。"""

    def __init__(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("FixedClock 需要带时区的时刻")
        self._moment = moment.astimezone(timezone.utc)

    def now_utc(self) -> datetime:
        return self._moment

    def advance(self, seconds: float = 0, **kwargs) -> None:
        from datetime import timedelta

        delta = timedelta(seconds=seconds, **{k: v for k, v in kwargs.items() if k != "seconds"})
        self._moment = self._moment + delta

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("需要带时区的时刻")
        self._moment = moment.astimezone(timezone.utc)


class IdGenerator(abc.ABC):
    @abc.abstractmethod
    def new_id(self, prefix: str) -> str:
        """生成一个带前缀的新标识。"""


class Uuid4IdGenerator(IdGenerator):
    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"


class SequentialIdGenerator(IdGenerator):
    """测试用：prefix_1、prefix_2 …… 便于断言。"""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def new_id(self, prefix: str) -> str:
        self._counts[prefix] = self._counts.get(prefix, 0) + 1
        return f"{prefix}_{self._counts[prefix]}"


class Notifier(abc.ABC):
    """通知外发端口：超时升级后通知当前负责人。

    返回 True 表示已送达；False/抛错表示暂未送达，调用方会保留
    notified=False 并在下次扫描时重试，从而升级事件与“是否已通知”可分别追踪。
    """

    @abc.abstractmethod
    def notify_escalation(
        self,
        *,
        owner_id: str,
        package_id: str,
        request_id: str,
        priority: str,
        overdue_seconds: float,
        occurred_at: str,
    ) -> bool:
        """向当前负责人发送升级通知。"""


class CollectingNotifier(Notifier):
    """默认/测试用：把通知收集在进程内列表，不做真实外发。"""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    def notify_escalation(
        self,
        *,
        owner_id: str,
        package_id: str,
        request_id: str,
        priority: str,
        overdue_seconds: float,
        occurred_at: str,
    ) -> bool:
        self.messages.append(
            {
                "owner_id": owner_id,
                "package_id": package_id,
                "request_id": request_id,
                "priority": priority,
                "overdue_seconds": overdue_seconds,
                "occurred_at": occurred_at,
            }
        )
        return True
