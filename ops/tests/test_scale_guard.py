from unittest.mock import MagicMock
from scale_guard import check_and_fix_workers, check_queue_depth


def test_corrects_drifted_max_workers():
    runpod_api = MagicMock()
    runpod_api.get_endpoint_config.side_effect = lambda eid: {"max_workers": 2} if eid == "video-ep" else {"max_workers": 10}
    webhook = MagicMock()

    result = check_and_fix_workers(
        runpod_api, endpoint_ids=["video-ep", "image-ep"],
        target_max_workers={"video-ep": 15, "image-ep": 10}, webhook=webhook,
    )

    assert result["video-ep"]["corrected"] is True
    assert result["image-ep"]["corrected"] is False
    runpod_api.set_max_workers.assert_called_once_with("video-ep", 15)
    webhook.send.assert_called_once()


def test_no_correction_when_config_matches():
    runpod_api = MagicMock()
    runpod_api.get_endpoint_config.return_value = {"max_workers": 10}
    webhook = MagicMock()

    result = check_and_fix_workers(
        runpod_api, endpoint_ids=["image-ep"],
        target_max_workers={"image-ep": 10}, webhook=webhook,
    )

    assert result["image-ep"]["corrected"] is False
    runpod_api.set_max_workers.assert_not_called()
    webhook.send.assert_not_called()


def test_corrects_drifted_min_workers():
    runpod_api = MagicMock()
    runpod_api.get_endpoint_config.return_value = {"max_workers": 10, "min_workers": 0}
    webhook = MagicMock()

    result = check_and_fix_workers(
        runpod_api, endpoint_ids=["video-ep"],
        target_max_workers={"video-ep": 10}, webhook=webhook,
        target_min_workers={"video-ep": 1},
    )

    assert result["video-ep"]["corrected"] is True
    runpod_api.set_min_workers.assert_called_once_with("video-ep", 1)
    webhook.send.assert_called_once()


def test_no_min_workers_correction_when_not_specified():
    runpod_api = MagicMock()
    runpod_api.get_endpoint_config.return_value = {"max_workers": 10, "min_workers": 0}
    webhook = MagicMock()

    result = check_and_fix_workers(
        runpod_api, endpoint_ids=["image-ep"],
        target_max_workers={"image-ep": 10}, webhook=webhook,
    )

    assert result["image-ep"]["corrected"] is False
    runpod_api.set_min_workers.assert_not_called()


def test_check_queue_depth_alerts_when_over_threshold():
    runpod_api = MagicMock()
    runpod_api.get_queue_depth.side_effect = lambda eid: 25 if eid == "video-ep" else 2
    webhook = MagicMock()

    result = check_queue_depth(runpod_api, endpoint_ids=["video-ep", "image-ep"], threshold=10, webhook=webhook)

    assert result["video-ep"]["over_threshold"] is True
    assert result["image-ep"]["over_threshold"] is False
    webhook.send.assert_called_once()
    assert "video-ep" in webhook.send.call_args[0][0]


def test_check_queue_depth_no_alert_when_under_threshold():
    runpod_api = MagicMock()
    runpod_api.get_queue_depth.return_value = 3
    webhook = MagicMock()

    result = check_queue_depth(runpod_api, endpoint_ids=["image-ep"], threshold=10, webhook=webhook)

    assert result["image-ep"]["over_threshold"] is False
    webhook.send.assert_not_called()
