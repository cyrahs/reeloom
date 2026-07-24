from __future__ import annotations

import pytest

from reeloom.observability.pricing import TokenPricing


def test_explicit_token_rates_produce_stable_microusd_estimate() -> None:
    pricing = TokenPricing.from_strings(
        input_usd_per_million="2.50",
        output_usd_per_million="10",
    )

    assert pricing.estimate_cost_microusd(
        input_tokens=1_000,
        output_tokens=200,
    ) == 4_500


@pytest.mark.parametrize("rate", ("-1", "nan", "inf", "not-a-price"))
def test_token_rates_are_strict(rate: str) -> None:
    with pytest.raises(ValueError):
        TokenPricing.from_strings(
            input_usd_per_million=rate,
            output_usd_per_million="1",
        )
