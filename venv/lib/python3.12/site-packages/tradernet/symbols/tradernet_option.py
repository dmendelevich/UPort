from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from re import match as re_match
from typing import Any
from warnings import warn

from .base_option_symbol import BaseOptionSymbol
from .option_properties import OptionProperties


class TradernetOption(BaseOptionSymbol):
    @staticmethod
    def decode_notation(symbol: str) -> OptionProperties:
        """
        +AAPL.18MAR2022.P150 -> (AAPL, '', -1, 150, 2022-03-18, 18MAR2022)
        """
        match = re_match(
            r'^\+(\D+(\d+)?)\.(\d{2}\D{3}\d{4})\.([CP])(\d+(\.\d*)?)$',
            symbol
        )

        if not match:
            raise ValueError(f'Invalid Tradernet option symbol: {symbol}')

        return OptionProperties(
            match.group(1),                               # ticker
            '',                                           # no location
            -1 if match.group(4) == 'P' else 1,           # right
            Decimal(match.group(5)),                      # strike
            TradernetOption.decode_date(match.group(3)),  # maturity_date
            match.group(3)                                # symbolic expiration
        )

    @staticmethod
    def encode_date(conventional_date: str | date | datetime) -> str:
        if isinstance(conventional_date, str):
            conventional_date = date.fromisoformat(conventional_date)

        return conventional_date.strftime('%d%b%Y').upper()

    @staticmethod
    def decode_date(symbolic_date: str) -> date:
        return datetime.strptime(symbolic_date, '%d%b%Y').date()


class TraderNetOption(TradernetOption):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warn(
            f"{self.__class__.__name__} is deprecated, "
            "use TradernetOption instead",
            DeprecationWarning,
            stacklevel=2
        )
        super().__init__(*args, **kwargs)
