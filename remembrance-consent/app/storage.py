"""Object storage for voice models, audio artifacts and exports.

Contract every backend honours:
  - delete() is idempotent: deleting a missing object succeeds, so the
    revocation cascade can be retried after any partial failure;
  - on S3, delete() removes every version and delete marker, because a
    plain DeleteObject on a versioned bucket only hides biometric data;
  - signed_url() returns a time-limited bearer link.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlencode

KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_.=-]{0,1023}$")


class StorageError(Exception):
    pass


def validate_key(key: str) -> str:
    if not KEY_PATTERN.match(key) or ".." in key.split("/") or "//" in key:
        raise StorageError(f"invalid object key {key!r}")
    return key


class ObjectStorage(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def signed_url(self, key: str, ttl_seconds: int, now: datetime) -> str: ...


class UrlSigner:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("URL signing secret must be at least 32 bytes")
        self._secret = secret

    def signature(self, key: str, expires: int) -> str:
        return hmac.new(self._secret, f"{key}\n{expires}".encode(), hashlib.sha256).hexdigest()

    def verify(self, key: str, expires: int, signature: str, now: datetime) -> bool:
        if now.timestamp() > expires:
            return False
        return hmac.compare_digest(self.signature(key, expires), signature)


class LocalFileStorage:
    """Filesystem backend; downloads go through GET /export/download."""

    def __init__(self, root: Path, signer: UrlSigner, public_base_url: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.signer = signer
        self.public_base_url = public_base_url.rstrip("/")

    def _path(self, key: str) -> Path:
        path = (self.root / validate_key(key)).resolve()
        if self.root not in path.parents:
            raise StorageError(f"key {key!r} escapes the storage root")
        return path

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.chmod(temp, 0o600)
            os.replace(temp, path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError as exc:
            raise StorageError(f"object {key!r} not found") from exc

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def signed_url(self, key: str, ttl_seconds: int, now: datetime) -> str:
        expires = int((now + timedelta(seconds=ttl_seconds)).timestamp())
        query = urlencode({"key": validate_key(key), "expires": expires, "sig": self.signer.signature(key, expires)}, quote_via=quote)
        return f"{self.public_base_url}/export/download?{query}"


class S3Storage:
    def __init__(self, bucket: str, *, region: str | None = None, endpoint_url: str | None = None, client=None) -> None:
        if client is None:
            import boto3  # optional dependency: pip install .[s3]

            client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url)
        self.client = client
        self.bucket = bucket

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(
            Bucket=self.bucket, Key=validate_key(key), Body=data, ContentType=content_type, ServerSideEncryption="AES256"
        )

    def get(self, key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=validate_key(key))["Body"].read()
        except self.client.exceptions.NoSuchKey as exc:
            raise StorageError(f"object {key!r} not found") from exc

    def delete(self, key: str) -> None:
        validate_key(key)
        paginator = self.client.get_paginator("list_object_versions")
        found = False
        for page in paginator.paginate(Bucket=self.bucket, Prefix=key):
            for entry in page.get("Versions", []) + page.get("DeleteMarkers", []):
                if entry["Key"] == key:
                    found = True
                    self.client.delete_object(Bucket=self.bucket, Key=key, VersionId=entry["VersionId"])
        if not found:
            self.client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=validate_key(key))
            return True
        except self.client.exceptions.ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def signed_url(self, key: str, ttl_seconds: int, now: datetime) -> str:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": validate_key(key)}, ExpiresIn=ttl_seconds
        )
