from unittest.mock import MagicMock
from cost_alert import check_spend


def test_alerts_when_spend_exceeds_threshold():
    runpod_api = MagicMock()
    runpod_api.get_current_hourly_spend.return_value = 60.0
    webhook = MagicMock()
    triggered = check_spend(runpod_api, webhook, limit_per_hour=80.0, threshold_pct=0.7)
    assert triggered is True
    webhook.send.assert_called_once()


def test_no_alert_when_spend_under_threshold():
    runpod_api = MagicMock()
    runpod_api.get_current_hourly_spend.return_value = 10.0
    webhook = MagicMock()
    triggered = check_spend(runpod_api, webhook, limit_per_hour=80.0, threshold_pct=0.7)
    assert triggered is False
    webhook.send.assert_not_called()
