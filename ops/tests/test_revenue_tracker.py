from unittest.mock import MagicMock
from revenue_tracker import check_revenue


def test_flags_when_approaching_threshold():
    webhook = MagicMock()
    triggered = check_revenue(current_annual_revenue=9_000_000, threshold=10_000_000, webhook=webhook)
    assert triggered is True
    webhook.send.assert_called_once()


def test_no_flag_when_well_under_threshold():
    webhook = MagicMock()
    triggered = check_revenue(current_annual_revenue=1_000_000, threshold=10_000_000, webhook=webhook)
    assert triggered is False
    webhook.send.assert_not_called()
