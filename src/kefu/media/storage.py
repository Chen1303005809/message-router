"""Object-storage seam for images that must outlive WeCom's temporary URL."""

from __future__ import annotations

import hashlib
import mimetypes
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from kefu.persistence.models import StoredMedia

if TYPE_CHECKING:
    from kefu.config import Settings
    from kefu.wecom.transport import InboundImagePart

MAX_IMAGE_BYTES = 20 * 1024 * 1024


class ObjectStorage(Protocol):
    def put(self, object_key: str, data: bytes, *, mime_type: str) -> None:
        """Store private bytes under a non-public object key."""

    def get(self, object_key: str) -> bytes:
        """Read private bytes after the caller has performed event authorization."""

    def delete_prefix(self, prefix: str) -> None:
        """Delete all private objects below a non-empty key prefix."""


class MediaIngestError(RuntimeError):
    pass


class InMemoryObjectStorage:
    """Small fake object store for relay tests."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(self, object_key: str, data: bytes, *, mime_type: str) -> None:
        self.objects[object_key] = (data, mime_type)

    def get(self, object_key: str) -> bytes:
        return self.objects[object_key][0]

    def delete_prefix(self, prefix: str) -> None:
        for object_key in tuple(self.objects):
            if object_key.startswith(prefix):
                del self.objects[object_key]


class LocalObjectStorage:
    """Development-only private filesystem storage with path traversal guards."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def put(self, object_key: str, data: bytes, *, mime_type: str) -> None:
        del mime_type  # Kept for parity with S3-compatible implementations.
        destination = (self._root / object_key).resolve()
        if self._root not in destination.parents:
            raise MediaIngestError("非法对象存储路径")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)

    def get(self, object_key: str) -> bytes:
        source = (self._root / object_key).resolve()
        if self._root not in source.parents:
            raise MediaIngestError("非法对象存储路径")
        try:
            return source.read_bytes()
        except FileNotFoundError as error:
            raise MediaIngestError("图片对象不存在") from error

    def delete_prefix(self, prefix: str) -> None:
        if not prefix.strip("/\\"):
            raise MediaIngestError("对象清理必须指定非空前缀")
        destination = (self._root / prefix).resolve()
        if destination == self._root or self._root not in destination.parents:
            raise MediaIngestError("非法对象存储路径")
        if destination.is_dir():
            shutil.rmtree(destination)
        elif destination.exists():
            destination.unlink()


class S3ObjectStorage:
    """S3-compatible private-object adapter, including local MinIO."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        region_name: str = "us-east-1",
    ) -> None:
        try:
            import boto3
        except ImportError as error:  # pragma: no cover - dependency is declared in pyproject.
            raise MediaIngestError("S3 存储需要安装 boto3") from error
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region_name,
        )

    def put(self, object_key: str, data: bytes, *, mime_type: str) -> None:
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=object_key,
                Body=data,
                ContentType=mime_type,
            )
        except Exception as error:
            raise MediaIngestError("S3 对象写入失败") from error

    def get(self, object_key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=object_key)
            return response["Body"].read()
        except Exception as error:
            raise MediaIngestError("S3 对象读取失败") from error

    def delete_prefix(self, prefix: str) -> None:
        if not prefix.strip("/\\"):
            raise MediaIngestError("对象清理必须指定非空前缀")
        try:
            paginator = self._client.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                versions = list(page.get("Versions", ())) + list(
                    page.get("DeleteMarkers", ())
                )
                objects = []
                for item in versions:
                    if not isinstance(item, dict) or not isinstance(item.get("Key"), str):
                        continue
                    object_key = {"Key": item["Key"]}
                    if item.get("VersionId") is not None:
                        object_key["VersionId"] = item["VersionId"]
                    objects.append(object_key)
                for offset in range(0, len(objects), 1000):
                    response = self._client.delete_objects(
                        Bucket=self._bucket,
                        Delete={
                            "Objects": objects[offset : offset + 1000],
                            "Quiet": True,
                        },
                    )
                    if response.get("Errors"):
                        raise MediaIngestError("S3 图片对象清理失败")
        except MediaIngestError:
            raise
        except Exception as error:
            raise MediaIngestError("S3 图片对象清理失败") from error


def object_storage_from_settings(settings: Settings) -> ObjectStorage:
    if settings.object_storage_backend == "local":
        return LocalObjectStorage(settings.object_storage_root)
    if settings.object_storage_backend == "s3":
        return S3ObjectStorage(
            bucket=settings.s3_bucket,
            endpoint_url=settings.s3_endpoint_url,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
        )
    raise MediaIngestError(f"不支持的对象存储后端：{settings.object_storage_backend}")


class MediaIngestor:
    """Persist a decrypted image and write its immutable metadata record."""

    def __init__(self, session_factory: Callable[[], Session], storage: ObjectStorage) -> None:
        self._session_factory = session_factory
        self._storage = storage

    def ingest(self, image: InboundImagePart) -> UUID:
        self._validate(image)
        media_id = uuid4()
        sha256 = hashlib.sha256(image.data).hexdigest()
        extension = mimetypes.guess_extension(image.mime_type, strict=False) or ".bin"
        object_key = f"media/{sha256[:2]}/{media_id}{extension}"
        try:
            self._storage.put(object_key, image.data, mime_type=image.mime_type)
        except Exception as error:
            raise MediaIngestError("图片转存失败") from error
        with self._session_factory() as session, session.begin():
            session.add(
                StoredMedia(
                    id=media_id,
                    object_key=object_key,
                    mime_type=image.mime_type,
                    byte_size=len(image.data),
                    sha256=sha256,
                )
            )
        return media_id

    def _validate(self, image: InboundImagePart) -> None:
        if not image.mime_type.startswith("image/"):
            raise MediaIngestError("MVP 只接收图片媒体")
        if not image.data:
            raise MediaIngestError("图片内容为空")
        if len(image.data) > MAX_IMAGE_BYTES:
            raise MediaIngestError("图片超过 20 MiB 上限")
