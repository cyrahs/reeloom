from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

_MAX_RATE = Decimal("10000")


def _rate(value: object) -> Decimal:
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid token price") from None
    if not rate.is_finite() or not 0 <= rate <= _MAX_RATE:
        raise ValueError("invalid token price")
    return rate


@dataclass(frozen=True, slots=True)
class TokenPricing:
    """Caller-supplied USD rates; no temporally unstable prices are hardcoded."""

    input_usd_per_million: Decimal
    output_usd_per_million: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_usd_per_million",
            _rate(self.input_usd_per_million),
        )
        object.__setattr__(
            self,
            "output_usd_per_million",
            _rate(self.output_usd_per_million),
        )

    @classmethod
    def from_strings(
        cls,
        *,
        input_usd_per_million: str,
        output_usd_per_million: str,
    ) -> TokenPricing:
        return cls(
            input_usd_per_million=_rate(input_usd_per_million),
            output_usd_per_million=_rate(output_usd_per_million),
        )

    def estimate_cost_microusd(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> int:
        if (
            type(input_tokens) is not int
            or input_tokens < 0
            or type(output_tokens) is not int
            or output_tokens < 0
        ):
            raise ValueError("invalid token usage")
        value = (
            self.input_usd_per_million * input_tokens
            + self.output_usd_per_million * output_tokens
        )
        return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
