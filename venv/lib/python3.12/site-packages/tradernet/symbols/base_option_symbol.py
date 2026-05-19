from __future__ import annotations

from abc import ABCMeta, abstractmethod
from datetime import date, datetime
from decimal import Decimal
from functools import total_ordering

from .option_properties import OptionProperties


@total_ordering
class BaseOptionSymbol(metaclass=ABCMeta):
    """
    Parsing class for option names.
    """
    __slots__ = ('symbol', '__properties')

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.__properties = self.decode_notation(self.symbol)

    def __repr__(self) -> str:
        if self.right == -1:
            right = 'Put'
        else:
            right = 'Call'
        underlying = self.underlying
        return f'{underlying} @ {self.strike} {right} {self.maturity_date}'

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BaseOptionSymbol):
            raise TypeError('Trying to compare different types')

        return (
            self.underlying, self.maturity_date, self.strike, self.right
        ) == (
            other.underlying, other.maturity_date, other.strike, other.right
        )

    def __ne__(self, other: object) -> bool:
        return not self == other

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, BaseOptionSymbol):
            raise TypeError('Trying to compare different types')

        return (
            self.underlying, self.maturity_date, self.strike, self.right
        ) < (
            other.underlying, other.maturity_date, other.strike, other.right
        )

    @property
    def ticker(self) -> str:
        return self.__properties.ticker

    @property
    def location(self) -> str | None:
        return self.__properties.location

    @property
    def right(self) -> int:
        return self.__properties.right

    @property
    def strike(self) -> Decimal:
        return self.__properties.strike

    @property
    def maturity_date(self) -> date:
        return self.__properties.maturity_date

    @property
    def symbolic_expiration(self) -> str:
        return self.__properties.symbolic_expiration

    @property
    def underlying(self) -> str:
        """
        Actually, this is only a guess of the underlying symbol.

        Returns
        -------
        str
            Underlying symbol.
        """
        if not self.location:
            return self.ticker
        return f'{self.ticker}.{self.location}'

    @property
    def symbolic_right(self) -> str:
        return 'C' if self.right == 1 else 'P'

    @staticmethod
    def numeric_right(is_call: bool) -> int:
        if not isinstance(is_call, bool):
            raise TypeError('Wrong type of argument')

        return (-1)**(not is_call)

    def osi(self) -> str:
        """
        Converting option name to OSI format.

        Returns
        -------
        str
            Resulting name.
        """
        expiration = self.maturity_date.strftime('%y%m%d')

        strike = str(self.strike).split('.')
        dollar = strike[0].zfill(5)
        if len(strike) == 2:
            cent = strike[1]
        else:
            cent = '0'
        cent = cent.zfill(3)

        return f'{self.ticker}{expiration}{self.symbolic_right}{dollar}{cent}'

    @staticmethod
    @abstractmethod
    def decode_date(symbolic_date: str) -> date:
        """
        Decoding date from string.

        Parameters
        ----------
        symbolic_date : str
            String with date.

        Returns
        -------
        date
            Decoded date.
        """
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def encode_date(conventional_date: str | date | datetime) -> str:
        """
        Encoding date to a concrete notation.

        Parameters
        ----------
        conventional_date : str | date | datetime | Date
            A date.

        Returns
        -------
        str
            Converted date.
        """
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def decode_notation(symbol: str) -> OptionProperties:
        """
        Decoding a particular option notation.
        """
        raise NotImplementedError
