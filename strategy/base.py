from __future__ import annotations

from abc import ABC, abstractmethod

from core.types import TradeIntent


class BaseStrategy(ABC):
    name: str

    @abstractmethod
    async def on_start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def on_tick(self) -> list[TradeIntent]:
        raise NotImplementedError

    @abstractmethod
    async def on_stop(self) -> None:
        raise NotImplementedError
