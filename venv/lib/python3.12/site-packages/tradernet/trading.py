from __future__ import annotations

from typing import Any
from warnings import warn

from .client import Tradernet


class Trading(Tradernet):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warn(
            f"{self.__class__.__name__} is deprecated, use Core instead",
            DeprecationWarning,
            stacklevel=2
        )
        super().__init__(*args, **kwargs)
