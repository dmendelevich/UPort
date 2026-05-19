from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass
class OptionProperties:
    ticker: str
    location: str | None
    right: int
    strike: Decimal
    maturity_date: date
    symbolic_expiration: str
