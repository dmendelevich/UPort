from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from json import loads as json_loads
from typing import Any

from .common import WSUtils, StringUtils
from .core import Core


class TradernetWebsocket(WSUtils, StringUtils):
    __slots__ = ('url',)

    def __init__(self, core: Core) -> None:
        super().__init__()
        self.core = core

    async def quotes(
        self,
        symbols: str | Sequence[str]
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Subscribing quotes updates.

        Parameters
        ----------
        symbols : str | Sequence[str]
            A sequence of symbols or a single symbol.

        Yields
        ------
        AsyncIterator[dict[str, Any]]
            Quote updates.
        """
        if isinstance(symbols, str):
            symbols = [symbols]
        query = self.stringify(['quotes', symbols])

        async for message in self.get_stream(
            query,
            self.core.websocket_url,
            self.core.websocket_auth()
        ):
            event, data, _ = json_loads(message)
            self.logger.debug('event: %s, data: %s', event, data)

            if event in ('q', 'error'):
                yield data

    async def market_depth(self, symbol: str) -> AsyncIterator[dict[str, Any]]:
        """
        Subscribe to market depth updates.

        Parameters
        ----------
        symbol : str
            A Tradernet symbol.

        Yields
        ------
        AsyncIterator[dict[str, Any]]
            Market depth updates.
        """
        query = self.stringify(['orderBook', [symbol]])

        async for message in self.get_stream(
            query,
            self.core.websocket_url,
            self.core.websocket_auth()
        ):
            event, data, _ = json_loads(message)
            self.logger.debug('event: %s, data: %s', event, data)

            if event in ('b', 'error'):
                yield data

    async def portfolio(self) -> AsyncIterator[dict[str, Any]]:
        """
        Subscribe to portfolio updates.

        Yields
        -------
        AsyncIterator[dict[str, Any]]
            Portfolio updates.
        """
        query = self.stringify(['portfolio'])

        async for message in self.get_stream(
            query,
            self.core.websocket_url,
            self.core.websocket_auth()
        ):
            event, data, _ = json_loads(message)
            self.logger.debug('event: %s, data: %s', event, data)

            if event in ('portfolio', 'error'):
                yield data

    async def orders(self) -> AsyncIterator[dict[str, Any]]:
        """
        Subscribing orders updates.

        Yields
        -------
        AsyncIterator[dict[str, Any]]
            Orders updates.
        """
        query = self.stringify(['orders'])

        async for message in self.get_stream(
            query,
            self.core.websocket_url,
            self.core.websocket_auth()
        ):
            event, data, _ = json_loads(message)
            self.logger.debug('event: %s, data: %s', event, data)

            if event in ('orders', 'error'):
                yield data

    async def markets(self) -> AsyncIterator[dict[str, Any]]:
        """
        Subscribing markets statuses.

        Yields
        -------
        AsyncIterator[dict[str, Any]]
            Markets statuses.
        """
        query = self.stringify(['markets'])

        async for message in self.get_stream(
            query,
            self.core.websocket_url,
            self.core.websocket_auth()
        ):
            event, data, _ = json_loads(message)
            self.logger.debug('event: %s, data: %s', event, data)

            if event in ('markets', 'error'):
                yield data
