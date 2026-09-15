from unittest.mock import MagicMock
from scale_guard import check_and_fix_workers


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
