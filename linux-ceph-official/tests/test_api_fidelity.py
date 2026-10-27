"""
API Fidelity test suite (thesis Ch. 5.1).

Assesses structural consistency of responses, handling of non-native
payloads (custom metadata), and compliance with standardized S3 errors,
across whichever backends are `enabled: true` in tests/config.yaml.

Run: pytest tests/ -v
"""

import base64
import email.utils
import hashlib
import hmac
import json
import logging
import time
import uuid

import boto3
import botocore.exceptions
import pytest
import requests
from botocore.config import Config

logger = logging.getLogger("thesis")


def unique_bucket() -> str:
    return f"fidelity-{uuid.uuid4().hex[:12]}"


def sigv2_list_objects(backend_config, bucket):
    """Lists a bucket with a request signed by hand in the legacy SigV2
    scheme, and returns the response.

    A second signer is needed because a SignatureDoesNotMatch answer to
    botocore's SigV2 client cannot distinguish "this store does not accept
    SigV2" from "this store signs SigV2 differently than botocore does".
    """
    endpoint = backend_config["endpoint"].rstrip("/")
    date = email.utils.formatdate(usegmt=True)
    string_to_sign = f"GET\n\n\n{date}\n/{bucket}/"
    signature = base64.b64encode(
        hmac.new(
            backend_config["secret_key"].encode(),
            string_to_sign.encode(),
            hashlib.sha1,
        ).digest()
    ).decode()
    headers = {
        "Date": date,
        "Authorization": f"AWS {backend_config['access_key']}:{signature}",
    }
    return requests.get(f"{endpoint}/{bucket}/", headers=headers, timeout=10)


def tamper_signature(url: str) -> str:
    """Changes one character of a presigned URL's signature, so the URL is
    well-formed but no longer valid."""
    for marker in ("X-Amz-Signature=", "Signature="):
        if marker in url:
            head, signature = url.split(marker, 1)
            flipped = ("0" if signature[0] != "0" else "1") + signature[1:]
            return head + marker + flipped
    return url


def describe_metadata(metadata) -> str:
    """The identifying fields of a response, whichever of them the backend
    sent.

    RequestId and HostId are conventions of the reference implementation, not
    requirements, so they are reported when present rather than assumed. The
    status is printed by the callers and the headers are logged separately, so
    both are left out here."""
    reported = {
        name: value
        for name, value in metadata.items()
        if name not in ("HTTPHeaders", "HTTPStatusCode") and value not in (None, "")
    }
    # A request that was not retried says nothing; one that was is worth seeing.
    if reported.get("RetryAttempts") == 0:
        del reported["RetryAttempts"]

    # botocore fills RequestId from the x-amz-request-id header. A backend that
    # names the header differently still sends the identifier, so look for it
    # before reporting that there is none: RustFS uses x-request-id.
    if "RequestId" not in reported:
        for name, value in metadata.get("HTTPHeaders", {}).items():
            if "request-id" in name.lower() or "request_id" in name.lower():
                reported[name] = value

    if not reported:
        return "no identifying metadata"
    return " ".join(f"{name}={value}" for name, value in sorted(reported.items()))


def log_client_error(error) -> None:
    """Logs what the service actually returned for a refused call.

    The capability matrix keeps the error code and message. The log keeps the
    HTTP status and whatever the response identified itself with, which are
    what separate a refusal by the service from a transport failure, if a
    recorded result is questioned later.

    A numeric code here means the response carried no error document: botocore
    then names the error after the status and the reason phrase, so '501' and
    'Not Implemented' are its words rather than the service's."""
    details = error.response.get("Error", {})
    metadata = error.response.get("ResponseMetadata", {})
    logger.info(
        f"  refused: HTTP {metadata.get('HTTPStatusCode')} "
        f"{details.get('Code')} {details.get('Message', '')!r} "
        f"| {describe_metadata(metadata)}"
    )
    logger.debug(f"  response headers: {metadata.get('HTTPHeaders', {})}")


def log_response(operation: str, response) -> None:
    """Logs the status of a call that succeeded, so a result recorded
    as supported can be traced back to the response it was read from."""
    metadata = response.get("ResponseMetadata", {})
    logger.info(
        f"  {operation}: HTTP {metadata.get('HTTPStatusCode')} "
        f"| {describe_metadata(metadata)}"
    )
    logger.debug(f"  response headers: {metadata.get('HTTPHeaders', {})}")


def remove_locked_bucket(s3_client, bucket_name):
    """Best-effort cleanup after the Object Lock test. A retained object can
    only be removed by bypassing governance retention, and a backend may
    refuse that as well, so failures are logged and ignored."""
    try:
        versions = s3_client.list_object_versions(Bucket=bucket_name)
        entries = versions.get("Versions", []) + versions.get("DeleteMarkers", [])
        for entry in entries:
            try:
                s3_client.delete_object(
                    Bucket=bucket_name,
                    Key=entry["Key"],
                    VersionId=entry["VersionId"],
                    BypassGovernanceRetention=True,
                )
            except botocore.exceptions.ClientError as e:
                log_client_error(e)
                logger.warning(f"  could not delete {entry['Key']}: {e}")
        s3_client.delete_bucket(Bucket=bucket_name)
    except botocore.exceptions.ClientError as e:
        # The listing above is the usual culprit: a backend without
        # ListObjectVersions cannot be enumerated here, so the bucket stays.
        log_client_error(e)
        logger.warning(f"  bucket {bucket_name} left behind: {e}")


class TestSignatureVerification:
    """Every capability result below assumes the backend checks who is
    calling. A deployment that accepts any signature accepts every probe as
    well, so this runs first and records whether that assumption holds."""

    def test_wrong_secret_key_is_rejected(self, backend_config, record_capability):
        wrong_client = boto3.client(
            "s3",
            endpoint_url=backend_config["endpoint"],
            aws_access_key_id=backend_config["access_key"],
            aws_secret_access_key="wrong-secret-key-for-the-negative-control",
            region_name=backend_config.get("region", "us-east-1"),
        )
        try:
            wrong_client.list_buckets()
            rejected = False
            detail = "list_buckets succeeded with a wrong secret key: signatures are not verified"
        except botocore.exceptions.ClientError as e:
            log_client_error(e)
            rejected = True
            detail = f"rejected with {e.response['Error']['Code']}"
        logger.info(f"  signature verification: {detail}")
        record_capability("signature_verification", rejected, detail)
        assert rejected, detail


class TestBucketOperations:
    def test_create_list_delete_bucket(self, s3_client):
        name = unique_bucket()
        s3_client.create_bucket(Bucket=name)
        listing = s3_client.list_buckets()
        assert name in [b["Name"] for b in listing["Buckets"]]

        s3_client.delete_bucket(Bucket=name)
        listing2 = s3_client.list_buckets()
        assert name not in [b["Name"] for b in listing2["Buckets"]]


class TestObjectCRUD:
    def test_put_get_delete_object(self, s3_client, bucket, record_capability):
        key = "hello.txt"
        body = b"hello world"
        s3_client.put_object(Bucket=bucket, Key=key, Body=body)

        resp = s3_client.get_object(Bucket=bucket, Key=key)
        assert resp["Body"].read() == body

        s3_client.delete_object(Bucket=bucket, Key=key)
        with pytest.raises(botocore.exceptions.ClientError) as exc:
            s3_client.get_object(Bucket=bucket, Key=key)
        assert exc.value.response["Error"]["Code"] == "NoSuchKey"

        record_capability(
            "object_put_get_delete", True,
            "put/get/delete round-trip and post-delete NoSuchKey both correct",
        )

    def test_empty_object(self, s3_client, bucket, record_capability):
        key = "empty.txt"
        s3_client.put_object(Bucket=bucket, Key=key, Body=b"")
        resp = s3_client.get_object(Bucket=bucket, Key=key)
        assert resp["Body"].read() == b""
        assert resp["ContentLength"] == 0

        record_capability(
            "empty_object_support", True, "zero-byte object uploaded and read back correctly"
        )

    def test_special_character_key(self, s3_client, bucket, record_capability):
        key = "folder/sub folder/file name (v1) #2 !@.txt"
        body = b"special characters in key"
        s3_client.put_object(Bucket=bucket, Key=key, Body=body)
        resp = s3_client.get_object(Bucket=bucket, Key=key)
        assert resp["Body"].read() == body

        record_capability(
            "special_character_keys", True,
            "key with spaces/parentheses/#/! handled correctly",
        )

    def test_custom_metadata_roundtrip(self, s3_client, bucket, record_capability):
        key = "meta.txt"
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=b"data",
            Metadata={"test-metadata": "ashutosh", "experiment-id": "rustfs"},
        )
        resp = s3_client.head_object(Bucket=bucket, Key=key)
        assert resp["Metadata"].get("test-metadata") == "ashutosh"
        assert resp["Metadata"].get("experiment-id") == "rustfs"

        record_capability(
            "custom_metadata_roundtrip", True,
            "custom x-amz-meta-* headers round-tripped correctly",
        )


class TestMultipartUpload:
    def test_multipart_upload_lifecycle(self, s3_client, bucket, record_capability):
        key = "large-object.bin"
        part_size = 5 * 1024 * 1024  # 5MB = S3's minimum part size (except last part)

        create = s3_client.create_multipart_upload(Bucket=bucket, Key=key)
        upload_id = create["UploadId"]
        logger.info(f"started multipart upload {upload_id}")

        parts = []
        for part_number, fill in ((1, b"a"), (2, b"b")):
            resp = s3_client.upload_part(
                Bucket=bucket,
                Key=key,
                PartNumber=part_number,
                UploadId=upload_id,
                Body=fill * part_size,
            )
            parts.append({"ETag": resp["ETag"], "PartNumber": part_number})
            logger.info(f"  uploaded part {part_number} ({part_size} bytes)")

        logger.info("completing multipart upload")
        s3_client.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )

        logger.info("downloading the assembled object to verify both parts landed correctly")
        resp = s3_client.get_object(Bucket=bucket, Key=key)
        content = resp["Body"].read()
        assert len(content) == part_size * 2
        assert content[:part_size] == b"a" * part_size
        assert content[part_size:] == b"b" * part_size

        record_capability(
            "multipart_upload", True,
            "create/upload-parts/complete produced a correctly assembled object",
        )

    def test_multipart_upload_abort(self, s3_client, bucket, record_capability):
        key = "aborted-object.bin"
        create = s3_client.create_multipart_upload(Bucket=bucket, Key=key)
        upload_id = create["UploadId"]
        logger.info(f"started multipart upload {upload_id}, uploading 1 part")
        s3_client.upload_part(
            Bucket=bucket,
            Key=key,
            PartNumber=1,
            UploadId=upload_id,
            Body=b"a" * (5 * 1024 * 1024),
        )
        logger.info(f"aborting multipart upload {upload_id} before completing it")
        s3_client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)

        logger.info("confirming the object does not exist after the abort")
        with pytest.raises(botocore.exceptions.ClientError) as exc:
            s3_client.get_object(Bucket=bucket, Key=key)
        assert exc.value.response["Error"]["Code"] == "NoSuchKey"

        record_capability(
            "multipart_upload_abort", True,
            "aborted upload correctly left no object behind",
        )


class TestErrorCompliance:
    def test_get_nonexistent_bucket(self, s3_client, record_capability):
        with pytest.raises(botocore.exceptions.ClientError) as exc:
            s3_client.list_objects_v2(Bucket="thesis-bucket-does-not-exist-xyz")
        err = exc.value.response["Error"]
        assert err["Code"] == "NoSuchBucket"
        assert exc.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404

        record_capability(
            "error_nosuchbucket_compliance", True,
            "NoSuchBucket + HTTP 404 returned for a nonexistent bucket",
        )

    def test_get_nonexistent_key(self, s3_client, bucket, record_capability):
        with pytest.raises(botocore.exceptions.ClientError) as exc:
            s3_client.get_object(Bucket=bucket, Key="does-not-exist.txt")
        err = exc.value.response["Error"]
        assert err["Code"] == "NoSuchKey"
        assert exc.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404

        record_capability(
            "error_nosuchkey_compliance", True,
            "NoSuchKey + HTTP 404 returned for a nonexistent key",
        )

    def test_delete_nonexistent_bucket(self, s3_client, record_capability):
        with pytest.raises(botocore.exceptions.ClientError) as exc:
            s3_client.delete_bucket(Bucket="thesis-bucket-does-not-exist-xyz")
        assert exc.value.response["Error"]["Code"] == "NoSuchBucket"

        record_capability(
            "error_delete_nonexistent_bucket_compliance", True,
            "NoSuchBucket returned when deleting a bucket that doesn't exist",
        )



class TestOptionalCapabilities:
    """
    These document capability, they don't assert a specific outcome is
    correct or required -- an S3-compatible store is allowed to not support
    versioning/ACLs. Results land in compatibility_matrix.csv for Ch. 5.1's
    comparison table.
    """

    def test_bucket_versioning_capability(self, s3_client, bucket, record_capability):
        key = "versioned.txt"
        try:
            s3_client.put_bucket_versioning(
                Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
            )
            status = s3_client.get_bucket_versioning(Bucket=bucket)
            log_response("get_bucket_versioning", status)
            if status.get("Status") != "Enabled":
                record_capability(
                    "bucket_versioning", False,
                    f"get_bucket_versioning returned Status={status.get('Status')!r}",
                )
                return

            # Accepting the configuration is not the same as keeping old
            # versions, so the check is whether superseded content can still
            # be read back by its version id.
            first = s3_client.put_object(Bucket=bucket, Key=key, Body=b"version one")
            s3_client.put_object(Bucket=bucket, Key=key, Body=b"version two")
            version_id = first.get("VersionId")
            if not version_id:
                record_capability(
                    "bucket_versioning", False,
                    "versioning reported enabled but put_object returned no VersionId",
                )
                return
            old = s3_client.get_object(Bucket=bucket, Key=key, VersionId=version_id)
            supported = old["Body"].read() == b"version one"
            detail = f"superseded version retrievable by id: {supported}"
        except botocore.exceptions.ClientError as e:
            log_client_error(e)
            supported = False
            detail = f"{e.response['Error']['Code']}: {e.response['Error'].get('Message', '')}"
        record_capability("bucket_versioning", supported, detail)

    def test_object_tagging_capability(self, s3_client, bucket, record_capability):
        key = "tagged.txt"
        s3_client.put_object(Bucket=bucket, Key=key, Body=b"data")
        try:
            s3_client.put_object_tagging(
                Bucket=bucket,
                Key=key,
                Tagging={"TagSet": [{"Key": "project", "Value": "thesis"}]},
            )
            tags = s3_client.get_object_tagging(Bucket=bucket, Key=key)
            log_response("get_object_tagging", tags)
            tagset = {t["Key"]: t["Value"] for t in tags.get("TagSet", [])}
            supported = tagset.get("project") == "thesis"
            detail = f"get_object_tagging returned {tagset}"
        except botocore.exceptions.ClientError as e:
            log_client_error(e)
            supported = False
            detail = f"{e.response['Error']['Code']}: {e.response['Error'].get('Message', '')}"
        record_capability("object_tagging", supported, detail)

    def test_bucket_policy_capability(
        self, s3_client, bucket, backend_config, record_capability
    ):
        key = "public.txt"
        body = b"readable by anyone"
        policy = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": "*",
                        "Action": "s3:GetObject",
                        "Resource": f"arn:aws:s3:::{bucket}/*",
                    }
                ],
            }
        )
        s3_client.put_object(Bucket=bucket, Key=key, Body=body)
        try:
            s3_client.put_bucket_policy(Bucket=bucket, Policy=policy)
            result = s3_client.get_bucket_policy(Bucket=bucket)
            log_response("get_bucket_policy", result)
            if "GetObject" not in result.get("Policy", ""):
                record_capability("bucket_policy", False, "policy did not round-trip")
                return

            # Storing a policy is not the same as applying one. The policy
            # above makes the object world-readable, so an unauthenticated
            # request for it should now succeed.
            endpoint = backend_config["endpoint"].rstrip("/")
            anonymous = requests.get(f"{endpoint}/{bucket}/{key}", timeout=10)
            supported = anonymous.status_code == 200 and anonymous.content == body
            detail = (
                f"policy round-tripped; anonymous GET returned HTTP {anonymous.status_code}"
            )
        except botocore.exceptions.ClientError as e:
            log_client_error(e)
            supported = False
            detail = f"{e.response['Error']['Code']}: {e.response['Error'].get('Message', '')}"
        record_capability("bucket_policy", supported, detail)

    def test_object_lock_capability(self, s3_client, record_capability):
        # Object Lock can only be enabled at bucket-creation time, so this
        # test can't use the shared `bucket` fixture -- it needs its own
        # bucket, created with a different parameter, and cleans that
        # bucket up itself.
        bucket_name = unique_bucket()
        bucket_created = False
        try:
            s3_client.create_bucket(Bucket=bucket_name, ObjectLockEnabledForBucket=True)
            bucket_created = True
            s3_client.put_object_lock_configuration(
                Bucket=bucket_name,
                ObjectLockConfiguration={
                    "ObjectLockEnabled": "Enabled",
                    "Rule": {"DefaultRetention": {"Mode": "GOVERNANCE", "Days": 1}},
                },
            )
            config = s3_client.get_object_lock_configuration(Bucket=bucket_name)
            log_response("get_object_lock_configuration", config)
            lock_config = config.get("ObjectLockConfiguration", {})
            if lock_config.get("ObjectLockEnabled") != "Enabled":
                record_capability(
                    "object_lock", False,
                    f"get_object_lock_configuration returned {lock_config}",
                )
                return

            # A retention configuration only means something if the store
            # refuses to delete a retained version.
            put = s3_client.put_object(
                Bucket=bucket_name, Key="locked.txt", Body=b"protected"
            )
            try:
                s3_client.delete_object(
                    Bucket=bucket_name, Key="locked.txt", VersionId=put["VersionId"]
                )
                supported = False
                detail = "retention configured, but the retained version was deleted anyway"
            except botocore.exceptions.ClientError as e:
                log_client_error(e)
                supported = True
                detail = f"delete of the retained version refused with {e.response['Error']['Code']}"
        except botocore.exceptions.ClientError as e:
            log_client_error(e)
            supported = False
            detail = f"{e.response['Error']['Code']}: {e.response['Error'].get('Message', '')}"
        finally:
            if bucket_created:
                remove_locked_bucket(s3_client, bucket_name)
        record_capability("object_lock", supported, detail)

    def test_cors_configuration_capability(
        self, s3_client, bucket, backend_config, record_capability
    ):
        origin = "https://example.org"
        try:
            s3_client.put_bucket_cors(
                Bucket=bucket,
                CORSConfiguration={
                    "CORSRules": [{"AllowedMethods": ["GET"], "AllowedOrigins": [origin]}]
                },
            )
            cors = s3_client.get_bucket_cors(Bucket=bucket)
            log_response("get_bucket_cors", cors)
            if not cors.get("CORSRules"):
                record_capability("cors_configuration", False, "no rule returned after storing one")
                return

            # A stored rule is only useful if the store answers a browser
            # preflight with it.
            endpoint = backend_config["endpoint"].rstrip("/")
            preflight = requests.options(
                f"{endpoint}/{bucket}/cors-probe.txt",
                headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
                timeout=10,
            )
            allowed = preflight.headers.get("Access-Control-Allow-Origin", "")
            supported = allowed in (origin, "*")
            detail = (
                f"rule stored; preflight HTTP {preflight.status_code}, "
                f"Access-Control-Allow-Origin={allowed!r}"
            )
        except botocore.exceptions.ClientError as e:
            log_client_error(e)
            supported = False
            detail = f"{e.response['Error']['Code']}: {e.response['Error'].get('Message', '')}"
        record_capability("cors_configuration", supported, detail)

    def test_sse_s3_capability(self, s3_client, bucket, record_capability):
        key = "encrypted.txt"
        try:
            s3_client.put_object(
                Bucket=bucket, Key=key, Body=b"secret", ServerSideEncryption="AES256"
            )
            resp = s3_client.head_object(Bucket=bucket, Key=key)
            log_response("head_object", resp)
            supported = resp.get("ServerSideEncryption") == "AES256"
            detail = f"SSE-S3 (AES256): head_object ServerSideEncryption={resp.get('ServerSideEncryption')!r}"
        except botocore.exceptions.ClientError as e:
            log_client_error(e)
            supported = False
            detail = f"{e.response['Error']['Code']}: {e.response['Error'].get('Message', '')}"
        record_capability("sse_s3", supported, detail)

    def test_presigned_url_download(self, s3_client, bucket, record_capability):
        key = "presigned.txt"
        body = b"presigned url content"
        s3_client.put_object(Bucket=bucket, Key=key, Body=body)

        url = s3_client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=60
        )
        resp = requests.get(url, timeout=10)
        served = resp.status_code == 200 and resp.content == body

        # A store that ignores the signature would serve a tampered or an
        # expired URL just as readily, which is the opposite of what a
        # presigned URL is for.
        tampered = requests.get(tamper_signature(url), timeout=10)
        short_lived = s3_client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=1
        )
        time.sleep(2)
        expired = requests.get(short_lived, timeout=10)

        supported = served and tampered.status_code >= 400 and expired.status_code >= 400
        detail = (
            f"valid HTTP {resp.status_code}, tampered HTTP {tampered.status_code}, "
            f"expired HTTP {expired.status_code}"
        )
        record_capability("presigned_url", supported, detail)

    def test_legacy_sigv2_signing_capability(
        self, backend_config, bucket, record_capability
    ):
        # A second client, signing requests with the old SigV2 scheme
        # instead of SigV4 -- AWS itself deprecated SigV2 in 2019, but
        # self-hosted S3-compatible stores vary on whether they still
        # accept it.
        sigv2_client = boto3.client(
            "s3",
            endpoint_url=backend_config["endpoint"],
            aws_access_key_id=backend_config["access_key"],
            aws_secret_access_key=backend_config["secret_key"],
            region_name=backend_config.get("region", "us-east-1"),
            config=Config(signature_version="s3"),
        )
        try:
            sigv2_client.list_objects(Bucket=bucket)
            botocore_result = "accepted"
        except botocore.exceptions.ClientError as e:
            log_client_error(e)
            botocore_result = e.response["Error"]["Code"]

        # The same request signed by hand. If one signer is accepted and the
        # other is not, the disagreement is about how the request was signed
        # rather than about whether the scheme is supported.
        try:
            response = sigv2_list_objects(backend_config, bucket)
            manual_result = f"HTTP {response.status_code}"
            manual_accepted = response.status_code == 200
        except requests.RequestException as e:
            manual_result = f"request failed: {e}"
            manual_accepted = False

        supported = botocore_result == "accepted" or manual_accepted
        detail = f"botocore signer: {botocore_result}; independent signer: {manual_result}"
        logger.info(f"  legacy SigV2: {detail}")
        record_capability("legacy_sigv2_auth", supported, detail)


class TestListingAndPagination:
    def test_list_objects_v2_pagination(self, s3_client, bucket):
        for i in range(15):
            s3_client.put_object(Bucket=bucket, Key=f"page-object-{i:02d}.txt", Body=b"x")

        page1 = s3_client.list_objects_v2(Bucket=bucket, MaxKeys=10)
        assert page1["IsTruncated"] is True
        assert len(page1["Contents"]) == 10

        page2 = s3_client.list_objects_v2(
            Bucket=bucket, MaxKeys=10, ContinuationToken=page1["NextContinuationToken"]
        )
        assert len(page2["Contents"]) == 5
        assert page2["IsTruncated"] is False

        all_keys = {o["Key"] for o in page1["Contents"]} | {o["Key"] for o in page2["Contents"]}
        assert all_keys == {f"page-object-{i:02d}.txt" for i in range(15)}


class TestObjectCopyAndRange:
    def test_copy_object_same_bucket(self, s3_client, bucket):
        src_key, dst_key = "source.txt", "copied.txt"
        body = b"content to be copied server-side"
        s3_client.put_object(Bucket=bucket, Key=src_key, Body=body)

        s3_client.copy_object(
            Bucket=bucket,
            Key=dst_key,
            CopySource={"Bucket": bucket, "Key": src_key},
        )
        resp = s3_client.get_object(Bucket=bucket, Key=dst_key)
        assert resp["Body"].read() == body

    def test_content_type_preservation(self, s3_client, bucket):
        key = "page.html"
        s3_client.put_object(
            Bucket=bucket, Key=key, Body=b"<html></html>", ContentType="text/html"
        )
        resp = s3_client.head_object(Bucket=bucket, Key=key)
        assert resp["ContentType"] == "text/html"

    def test_range_get_request(self, s3_client, bucket):
        key = "range.bin"
        body = bytes(range(256)) * 4  # 1024 deterministic bytes
        s3_client.put_object(Bucket=bucket, Key=key, Body=body)

        resp = s3_client.get_object(Bucket=bucket, Key=key, Range="bytes=10-19")
        chunk = resp["Body"].read()
        assert chunk == body[10:20]
        assert resp["ContentLength"] == 10
