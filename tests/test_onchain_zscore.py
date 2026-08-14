"""mvrv_z is computed locally from the full realized_price_ratio series —
BRK removed its precomputed *_zscore series (~2026-08), which silently
disabled the onchain_smart_trigger overlay via hourly 404s.
"""
from decimal import Decimal

import pytest

from bitcoiners_dca.core.onchain import (
    ALL_METRIC_NAMES,
    COMPUTED_ZSCORE_METRICS,
    SUPPORTED_METRICS,
    OnchainClient,
    OnchainSignalError,
    zscore_of_latest,
)


def d(*values) -> list[Decimal]:
    return [Decimal(str(v)) for v in values]


class TestZscoreOfLatest:
    def test_latest_at_mean_is_zero(self):
        assert zscore_of_latest(d(1, 3, 2)) == Decimal(0)

    def test_one_population_std_above_mean(self):
        # values 2,4,4,4,5,5,7,9 → mean 5, pstdev 2; latest=9 → z=2
        assert zscore_of_latest(d(2, 4, 4, 4, 5, 5, 7, 9)) == Decimal(2)

    def test_below_mean_is_negative(self):
        assert zscore_of_latest(d(4, 9, 5, 2)) < 0

    def test_too_short_series_raises(self):
        with pytest.raises(OnchainSignalError):
            zscore_of_latest(d(1))

    def test_constant_series_raises(self):
        with pytest.raises(OnchainSignalError):
            zscore_of_latest(d(2, 2, 2))


class TestMetricRegistry:
    def test_mvrv_z_is_computed_not_scalar(self):
        assert "mvrv_z" in COMPUTED_ZSCORE_METRICS
        assert "mvrv_z" not in SUPPORTED_METRICS
        assert COMPUTED_ZSCORE_METRICS["mvrv_z"] == "realized_price_ratio"

    def test_all_metric_names_covers_both(self):
        assert "mvrv_z" in ALL_METRIC_NAMES
        assert "mvrv" in ALL_METRIC_NAMES


class TestClientComputedPath:
    @pytest.mark.asyncio
    async def test_get_mvrv_z_reduces_series_and_caches(self, monkeypatch):
        client = OnchainClient(base_url="http://brk.test")
        calls: list[tuple[str, str]] = []

        async def fake_series(series, index):
            calls.append((series, index))
            return d(2, 4, 4, 4, 5, 5, 7, 9)

        monkeypatch.setattr(client, "_fetch_series", fake_series)
        assert await client.get("mvrv_z") == Decimal(2)
        assert await client.get("mvrv_z") == Decimal(2)
        assert calls == [("realized_price_ratio", "day1")]  # second hit cached

    @pytest.mark.asyncio
    async def test_unknown_metric_lists_computed_names(self):
        client = OnchainClient(base_url="http://brk.test")
        with pytest.raises(OnchainSignalError, match="mvrv_z"):
            await client.get("nope")
