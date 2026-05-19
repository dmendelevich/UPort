from __future__ import annotations

from configparser import ConfigParser
from json import dumps as json_dumps
from logging import getLogger
from time import time
from typing import Any, ClassVar, Type, TypeVar
from warnings import warn

from .common import NetUtils, StringUtils


Self = TypeVar('Self', bound='Core')


class Core(NetUtils, StringUtils):
    """
    Core tools to interact Tradernet API.

    Parameters
    ----------
    public : str, optional
        A Tradernet public key.
    private: str, optional
        A Tradernet private key.

    Attributes
    ----------
    logger : Logger
        Handling errors and warnings.
    """
    DOMAIN: ClassVar[str] = 'freedom24.com'  # Tradernet server
    SESSION_TIME: ClassVar[int] = 18000      # 18000 seconds == 5 hours
    CHUNK_SIZE: ClassVar[int] = 7000         # Instruments per request
    MAX_EXPORT_SIZE: ClassVar[int] = 100     # Max instruments per export
    DURATION: ClassVar[dict[str, int]] = {
        'day': 1,  # The order will be valid until the end of the trading day.
        'ext': 2,  # Extended day order.
        'gtc': 3   # A.k.a. "Good Till Cancelled"
    }

    __slots__ = (
        'public',
        '_private'
    )

    def __init__(
        self,
        public: str | None = None,
        private: str | None = None
    ) -> None:
        super().__init__()
        self.logger = getLogger(self.__class__.__name__)

        # Setting authorization data
        self.public = public
        self._private = private

        # Checking input
        if not self.public or not self._private:
            self.logger.warning(
                'A keypair was not set. It can be generated here: '
                '%s/tradernet-api/auth-api',
                self.url
            )

    @classmethod
    def from_config(cls: Type[Self], config_file: str) -> Self:
        """
        Getting a session ID with the use of the login-password
        authorization.

        Parameters
        ----------
        config_file : str
            A path to the configuration file.

        Returns
        -------
        Self
            A new instance.
        """
        config = ConfigParser()
        config.read(config_file)

        auth = config['auth'] if 'auth' in config else {}

        instance = cls(
            auth['public'] if 'public' in auth else None,
            auth['private'] if 'private' in auth else None
        )

        return instance

    @property
    def url(self) -> str:
        return f'https://{self.DOMAIN}'

    @property
    def websocket_url(self) -> str:
        return f'wss://wss.{self.DOMAIN}'

    def websocket_auth(self) -> dict[str, str]:
        current_timestamp = str(int(time()))
        return {
            'X-NtApi-PublicKey': self.public or '',
            'X-NtApi-Timestamp': current_timestamp,
            'X-NtApi-Sig': self.sign(self._private or '', current_timestamp)
        }

    def plain_request(
        self,
        cmd: str,
        params: dict[str, Any] | None = None
    ) -> Any:
        """
        Unencoded GET request to Tradernet. It could use either use
        authorization or not (if the session ID is not set).

        Parameters
        ----------
        cmd : str
            A command.
        params : dict[str, Any] | None, optional
            Set of parameters in the request.

        Returns
        -------
        Any
            JSON-decoded answer from Tradernet.
        """
        self.logger.debug('Making a simple request to API')

        message = self.__compose_message(cmd, params)

        url = f'{self.url}/api'
        query = {'q': json_dumps(message)}

        self.logger.debug('Message: %s', message)
        self.logger.debug('Query: %s', query)

        response = self.request('get', url, params=query)
        return response.json()

    def authorized_request(
        self,
        cmd: str,
        params: dict[str, Any] | None = None,
        version: int | None = 2
    ) -> Any:
        """
        Sending formatted and encoded request to Tradernet using keypair
        authorization.

        Parameters
        ----------
        cmd : str
            A command.
        params : dict, optional
            Set of parameters in the request.
        version : int, optional
            API version, by default 2

        Returns
        -------
        Answer from Tradernet.
        """
        if self.public is None or self._private is None:
            raise ValueError('Keypair is not valid')

        headers = {'Content-Type': 'application/json'}
        params = params or {}

        url = f'{self.url}/api/{cmd}'
        self.logger.debug(
            'Making an authorized request to APIv%s: %s',
            version or 0,
            url
        )

        if version in (2, 3):
            payload = self.stringify(params)
            timestamp = str(int(time()))
            message = payload + timestamp

            self.logger.debug('Message: %s', message)

            # Signing the body of the request
            headers['X-NtApi-PublicKey'] = self.public
            headers['X-NtApi-Timestamp'] = timestamp
            headers['X-NtApi-Sig'] = self.sign(self._private, message)

        else:
            raise ValueError(f'Unsupported API version {version}')

        self.logger.debug(
            'Sending POST to %s, headers: %s, query: %s',
            url,
            headers,
            message
        )

        response = self.request(
            'post',
            url,
            headers=headers,
            data=payload
        )
        result = response.json()

        if 'errMsg' in result:
            self.logger.error('Error: %s', result['errMsg'])

        return result

    def list_security_sessions(self) -> dict[str, Any]:
        """
        Getting a list of open security sessions.

        Notes
        -----
        https://freedom24.com/tradernet-api/security-get-list
        """
        return self.authorized_request('getSecuritySessions')

    @staticmethod
    def __compose_message(
        cmd: str,
        params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        message: dict[str, Any] = {'cmd': cmd}
        if params:
            message['params'] = params

        return message



class TraderNetCore(Core):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warn(
            "TraderNetCore is deprecated, use Core instead",
            DeprecationWarning,
            stacklevel=2
        )
        super().__init__(*args, **kwargs)
