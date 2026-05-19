from __future__ import annotations

from typing import Any
from warnings import warn

from .tradernet_websocket import TradernetWebsocket


class TraderNetWSAPI(TradernetWebsocket):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warn(
            f"{self.__class__.__name__} is deprecated, "
            "use TradernetWebsocket instead",
            DeprecationWarning,
            stacklevel=2
        )
        super().__init__(*args, **kwargs)
