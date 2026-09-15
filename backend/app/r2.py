import boto3


class R2Client:
    def __init__(self, settings):
        self._bucket = settings.r2_bucket
        self._write_client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint,
            aws_access_key_id=settings.r2_write_key,
            aws_secret_access_key=settings.r2_write_secret,
        )
        self._read_client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint,
            aws_access_key_id=settings.r2_read_key,
            aws_secret_access_key=settings.r2_read_secret,
        )

    def upload(self, key: str, data: bytes, content_type: str) -> str:
        self._write_client.put_object(
            Bucket=self._bucket, Key=key, Body=data, ContentType=content_type,
        )
        return key

    def signed_url(self, key: str, expires_in: int = 3600) -> str:
        return self._read_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )
