"""Where normalised media lives so Meta and LinkedIn can fetch it.

They fetch by URL **from their own servers**, so whatever backs this must be
publicly readable over HTTPS. That is the whole requirement, and it is why the
interface is this small — swapping R2 for anything else is one class.

The key is derived entirely from immutable inputs, which is what makes the
pipeline stateless:

    {brand}/{post_id}/{platform}/{policy_version}-{drive_md5}[-{n}].{ext}

Same source file, same policy, same key. A `HEAD` answers "already prepared?"
with no local cache to keep in sync — so a fresh GitHub Actions runner behaves
exactly like a laptop that has run this a hundred times.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from postpilot.logging import get_logger
from postpilot.media.policies import MediaPolicy
from postpilot.models import Platform

log = get_logger(__name__)


def media_key(
    brand_slug: str,
    post_id: str,
    platform: Platform,
    policy: MediaPolicy,
    drive_md5: str,
    *,
    index: int | None = None,
) -> str:
    """Deterministic object key. `index` distinguishes carousel items.

    The md5 is Drive's, of the *source* file — not of our output. Two posts
    using the same source file still get different keys, because the post ID is
    in the path; that costs a little storage and buys an unambiguous audit
    trail from object back to post.
    """
    suffix = f"-{index}" if index is not None else ""
    return f"{brand_slug}/{post_id}/{platform.value.lower()}/{policy.version}-{drive_md5}{suffix}.{policy.output_ext}"


@runtime_checkable
class MediaStore(Protocol):
    """Deliberately tiny, so it can be swapped without touching callers."""

    def url_for(self, key: str) -> str: ...

    def exists(self, key: str) -> bool: ...

    def put(self, key: str, data: bytes, content_type: str) -> str: ...


class R2Store:
    """Cloudflare R2 over its S3-compatible API."""

    def __init__(
        self,
        *,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        public_base_url: str,
        cache_seconds: int = 86400,
    ) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.public_base_url = public_base_url.rstrip("/")
        self.cache_seconds = cache_seconds
        self._client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(
                region_name="auto",
                signature_version="s3v4",
                # boto3 >= 1.36 sends x-amz-checksum-crc32 on every PUT by
                # default, which R2 rejects with an error that reads like a
                # credential problem. "when_required" turns that off without
                # losing checksums where they genuinely matter.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    @classmethod
    def from_settings(cls, settings) -> R2Store:
        values = settings.r2
        return cls(
            account_id=values["R2_ACCOUNT_ID"],
            access_key_id=values["R2_ACCESS_KEY_ID"],
            secret_access_key=values["R2_SECRET_ACCESS_KEY"],
            bucket=values["R2_BUCKET"],
            public_base_url=values["R2_PUBLIC_BASE_URL"],
        )

    def url_for(self, key: str) -> str:
        return f"{self.public_base_url}/{key}"

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            # An AccessDenied here is a real problem worth surfacing, not a
            # "missing object" to be papered over with a re-upload.
            raise

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            CacheControl=f"public, max-age={self.cache_seconds}",
        )
        log.info("uploaded %s (%.0f KB)", key, len(data) / 1024)
        return self.url_for(key)

    def delete(self, key: str) -> None:
        """Only used by tests and `doctor`; real objects expire by lifecycle."""
        self._client.delete_object(Bucket=self.bucket, Key=key)


class InMemoryStore:
    """A `MediaStore` for tests. Never touches the network."""

    def __init__(self, public_base_url: str = "https://media.test") -> None:
        self.public_base_url = public_base_url.rstrip("/")
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.puts: list[str] = []

    def url_for(self, key: str) -> str:
        return f"{self.public_base_url}/{key}"

    def exists(self, key: str) -> bool:
        return key in self.objects

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self.objects[key] = (data, content_type)
        self.puts.append(key)
        return self.url_for(key)
