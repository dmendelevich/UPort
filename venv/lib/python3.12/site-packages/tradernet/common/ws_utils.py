from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from logging import getLogger
from ssl import SSLContext, PROTOCOL_TLS, CERT_REQUIRED, create_default_context
from platform import system
from types import TracebackType
from typing import Type, TypeVar

from aiohttp import (  # pylint: disable=unused-import
    ClientSession,
    ClientTimeout,
    TCPConnector,
    WSMsgType,
    WSMessage
)
from certifi import where as certifi_where


Self = TypeVar('Self', bound='WSUtils')


class WSUtils:
    __slots__ = (
        'logger',
        'websocket_timeout',
        'websocket_session',
        'connector'
    )

    def __init__(self) -> None:
        self.logger = getLogger(self.__class__.__name__)
        self.websocket_timeout = ClientTimeout(
            total=None,
            sock_connect=None,
            sock_read=None
        )
        self.websocket_session: ClientSession | None = None

        self.connector = TCPConnector(
            limit_per_host=5,
            ssl=self.__ssl_context('windows')  # type: ignore
        )

    async def __aenter__(self) -> Self:
        if not self.websocket_session or self.websocket_session.closed:
            self.logger.debug('Created a new session')

            self.websocket_session = ClientSession(
                timeout=self.websocket_timeout,
                connector=self.connector
            )
        return self  # type: ignore

    async def __aexit__(
        self,
        exception_type: Type[BaseException] | None,
        exception_value: BaseException | None,
        traceback: TracebackType | None
    ) -> None:
        if self.websocket_session and not self.websocket_session.closed:
            await self.websocket_session.close()

    async def get_stream(
        self,
        query: str | Sequence[str],
        url: str,
        params: dict[str, str] | None = None
    ) -> AsyncIterator[str]:
        if not self.websocket_session:
            raise SyntaxError('API has to be used as a context manager only')

        if isinstance(query, str):
            query = [query]

        async with self.websocket_session.ws_connect(
            url,
            params=params,
            ssl_context=self.__ssl_context('darwin')
        ) as websocket:
            for message in query:
                await websocket.send_str(message)

            self.logger.debug('Message sent to websocket: %s', query)

            async for response in websocket:  # type: WSMessage
                if response.type == WSMsgType.TEXT:
                    self.logger.debug('Got message: %s', response.data)

                    yield response.data

    @staticmethod
    def __ssl_context(operating_system: str) -> SSLContext | None:
        if operating_system == system().lower() == 'darwin':
            ssl_context = SSLContext(PROTOCOL_TLS)
            ssl_context.verify_mode = CERT_REQUIRED
            ssl_context.check_hostname = True

            ssl_context.load_default_certs()
            ssl_context.load_verify_locations(cafile=certifi_where())

            return ssl_context

        if operating_system == system().lower() == 'windows':
            return create_default_context(cafile=certifi_where())

        return None
