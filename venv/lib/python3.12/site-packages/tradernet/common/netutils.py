from __future__ import annotations

from logging import getLogger
from typing import Any

from requests import Response, Session as HTTPSession


class NetUtils:
    __slots__ = ('session', 'timeout', 'logger')

    def __init__(self) -> None:
        self.session: HTTPSession | None = None
        self.timeout = 300
        self.logger = getLogger(self.__class__.__name__)

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        params: str | dict[str, Any] | None = None,
        data: bytes | str | dict[str, Any] | None = None
    ) -> Response:
        if not self.session:
            self.session = HTTPSession()

        response = self.session.request(
            method,
            url,
            headers=headers,
            params=params,
            data=data,
            timeout=self.timeout or None
        )
        self.logger.debug('Response url %s', response.url)

        try:
            response.raise_for_status()
        except Exception:
            self.logger.exception('Got request exception')
            raise

        return response
