from __future__ import annotations

from datetime import datetime, date
from logging import getLogger
from typing import Any
from warnings import warn

from numpy import (
    array,
    datetime64,
    float64,
    int64,
    timedelta64
)
from numpy.typing import NDArray

from ..client import Tradernet


def np_to_date(value: datetime64) -> date:
    dt = datetime.fromtimestamp(value.astype('O')/1e9)
    return date(dt.year, dt.month, dt.day)


class TradernetSymbol:
    """
    Acquiring and processing data from Tradernet.

    Parameters
    ----------
    symbol : str
        A symbol name on a remote service.
    start : datetime
        The first date of the period market data to be acquired within.
    end : datetime
        The last date of the period.

    Attributes
    ----------
    symbol : str
        A symbol name on a remote service.
    api : API class instance, optional
        API is to be used to get market data.
    start : datetime
        The first date of the period market data to be acquired within.
    end : datetime
        The last date of the period.
    logger : Logger
        Saving info and debugging.
    timestamps : array_like
        Timestamps of candles.
    candles : array_like
        High, low, open and close prices of candles.
    volumes : array_like
        Volumes of trades.
    """
    __slots__ = (
        'symbol',
        'api',
        'start',
        'end',
        'logger',
        'timestamps',
        'candles',
        'volumes',
        'timeframe'
    )

    def __init__(
        self,
        symbol: str,
        api: Tradernet | None = None,
        start: datetime = datetime(1970, 1, 1),
        end: datetime = datetime.now()
    ) -> None:
        self.symbol = symbol
        self.api = api

        # Dates interval
        self.start = start
        self.end = end

        self.logger = getLogger(self.__class__.__name__)

        self.timestamps: NDArray[datetime64] = array([], dtype=datetime64)
        self.candles: NDArray[float64] = array([], dtype=float64)
        self.volumes: NDArray[int64] = array([], dtype=int64)

        self.timeframe = 86400

    def get_data(self) -> TradernetSymbol:
        if not self.api or not isinstance(self.api, Tradernet):
            self.api = Tradernet()

        candles = self.api.get_candles(
            self.symbol,
            timeframe=self.timeframe,
            start=self.start,
            end=self.end
        )

        if 'hloc' in candles:
            self.timestamps = array(
                candles['xSeries'][self.symbol],
                dtype='datetime64[s]'
            )
            self.timestamps += timedelta64(3, 'h')  # UTC adjustment

            self.candles = array(candles['hloc'][self.symbol], dtype=float64)
            self.volumes = array(candles['vl'][self.symbol], dtype=int64)

        return self


class TraderNetSymbol(TradernetSymbol):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warn(
            f"{self.__class__.__name__} is deprecated, "
            "use TradernetSymbol instead",
            DeprecationWarning,
            stacklevel=2
        )
        super().__init__(*args, **kwargs)
