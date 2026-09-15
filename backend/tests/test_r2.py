from unittest.mock import MagicMock, patch
from app.r2 import R2Client


def _fake_settings():
    return MagicMock(
        r2_write_key="wkey", r2_write_secret="wsecret",
        r2_read_key="rkey", r2_read_secret="rsecret",
        r2_bucket="test-bucket", r2_endpoint="https://example.r2.cloudflarestorage.com",
    )


@patch("app.r2.boto3.client")
def test_upload_uses_write_client_and_returns_key(mock_boto_client):
    write_client = MagicMock()
    mock_boto_client.return_value = write_client
    r2 = R2Client(_fake_settings())
    result_key = r2.upload("clips/abc.mp4", b"fake-bytes", "video/mp4")
    assert result_key == "clips/abc.mp4"
    write_client.put_object.assert_called_once()
    _, kwargs = write_client.put_object.call_args
    assert kwargs["Bucket"] == "test-bucket"
    assert kwargs["Key"] == "clips/abc.mp4"
    assert kwargs["Body"] == b"fake-bytes"
    assert kwargs["ContentType"] == "video/mp4"


@patch("app.r2.boto3.client")
def test_signed_url_uses_read_client(mock_boto_client):
    read_client = MagicMock()
    read_client.generate_presigned_url.return_value = "https://signed.example.com/x"
    mock_boto_client.return_value = read_client
    r2 = R2Client(_fake_settings())
    url = r2.signed_url("clips/abc.mp4", expires_in=120)
    assert url == "https://signed.example.com/x"
    read_client.generate_presigned_url.assert_called_once()


@patch("app.r2.boto3.client")
def test_download_uses_read_client_and_returns_bytes(mock_boto_client):
    read_client = MagicMock()
    body = MagicMock()
    body.read.return_value = b"clip-bytes"
    read_client.get_object.return_value = {"Body": body}
    mock_boto_client.return_value = read_client
    r2 = R2Client(_fake_settings())
    data = r2.download("clips/abc.mp4")
    assert data == b"clip-bytes"
    read_client.get_object.assert_called_once_with(Bucket="test-bucket", Key="clips/abc.mp4")
