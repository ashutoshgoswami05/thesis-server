"""
Shared pytest fixtures for the API Fidelity test suite (thesis Ch. 5.1).

The list of backends to test comes from tests/config.yaml -- only entries
marked `enabled: true` there get tested, so this file doesn't need editing
as new backends are deployed later.

Test pass/fail results are already shown by pytest itself (run with -v,
or add --junit-xml=results.xml if you want a saved copy). The one thing
pytest can't do on its own is record *which optional S3 features* each
backend supports (e.g. versioning, ACLs) -- that's not pass/fail, it's a
capability comparison for the thesis. That's what `record_capability`
and `save_capability_results` below are for.

Every fixture below logs what it's doing, so running `pytest -v` (with
log_cli enabled in pytest.ini) prints a full trace of the setup/teardown
flow, not just which tests passed or failed.
"""

import csv
import logging
import uuid
from pathlib import Path

import boto3
import botocore.exceptions
import pytest
import yaml

logger = logging.getLogger("thesis")

CONFIG_PATH = Path(__file__).parent / "config.yaml"
RESULTS_DIR = Path(__file__).parent / "results"


def load_enabled_backends():
    logger.info(f"Reading backend list from {CONFIG_PATH}")
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)
    all_backends = config["backends"]
    enabled = {
        name: info for name, info in all_backends.items() if info and info.get("enabled")
    }
    logger.info(f"Enabled backends: {list(enabled.keys())}")
    return enabled


ENABLED_BACKENDS = load_enabled_backends()


@pytest.fixture(scope="session", params=list(ENABLED_BACKENDS.keys()))
def backend_name(request):
    """Every test using this fixture runs once per enabled backend."""
    logger.info(f"Testing backend: {request.param}")
    return request.param


@pytest.fixture(scope="session")
def backend_config(backend_name):
    config = ENABLED_BACKENDS[backend_name]
    logger.info(f"[{backend_name}] endpoint = {config['endpoint']}")
    return config


@pytest.fixture(scope="session")
def s3_client(backend_config):
    logger.info(f"Creating boto3 S3 client for {backend_config['endpoint']}")
    client = boto3.client(
        "s3",
        endpoint_url=backend_config["endpoint"],
        aws_access_key_id=backend_config["access_key"],
        aws_secret_access_key=backend_config["secret_key"],
        region_name=backend_config.get("region", "us-east-1"),
    )
    logger.info("S3 client ready")
    return client


@pytest.fixture
def bucket(s3_client, backend_config):
    """Creates a fresh bucket with a random name for one test, and deletes
    it (and anything left inside it) afterwards.

    A backend that cannot create buckets on demand sets `fixed_bucket` in
    config.yaml, and this fixture hands out that one bucket instead,
    emptying it afterwards rather than deleting it. No backend in the
    reported campaign needs it: JuiceFS did before its gateway was given
    --multi-buckets, and the branch is kept for any backend with the same
    restriction. It deliberately does not rescue
    TestBucketOperations::test_create_list_delete_bucket, which calls
    create_bucket itself and so still fails for such a backend; that result
    is recorded rather than hidden. The branch exists only so the remaining
    tests can produce comparable data instead of erroring out in setup."""
    fixed_bucket = backend_config.get("fixed_bucket")
    if fixed_bucket:
        logger.info(f"Backend uses a fixed bucket, reusing {fixed_bucket}")
        yield fixed_bucket
        empty_bucket(s3_client, fixed_bucket)
        return

    bucket_name = f"fidelity-{uuid.uuid4().hex[:12]}"
    logger.info(f"Creating bucket {bucket_name}")
    try:
        created = s3_client.create_bucket(Bucket=bucket_name)
    except botocore.exceptions.ClientError as e:
        # A backend that cannot create buckets on demand fails here, in
        # setup, where pytest reports an error without showing what the
        # service said. Log that first, then let it fail as before.
        error = e.response["Error"]
        logger.info(
            f"  create_bucket refused: HTTP "
            f"{e.response['ResponseMetadata']['HTTPStatusCode']} "
            f"{error['Code']} {error.get('Message', '')!r}"
        )
        raise
    logger.info(
        f"  bucket {bucket_name} created: HTTP "
        f"{created['ResponseMetadata']['HTTPStatusCode']}"
    )

    yield bucket_name

    empty_bucket(s3_client, bucket_name)
    deleted = s3_client.delete_bucket(Bucket=bucket_name)
    logger.info(
        f"  bucket {bucket_name} deleted: HTTP "
        f"{deleted['ResponseMetadata']['HTTPStatusCode']}"
    )


def empty_bucket(s3_client, bucket_name):
    """Deletes everything inside a bucket: every version of every object,
    every delete marker, and any unfinished multipart uploads."""
    logger.info(f"Cleaning up bucket {bucket_name}")

    # A bucket must be completely empty before S3 will let you delete it.
    # Use list_object_versions (not list_objects_v2) because it returns
    # EVERY version of EVERY key, not just the current one -- a test that
    # enables versioning and creates multiple versions of the same key would
    # otherwise leave old versions behind, and delete_bucket would fail with
    # BucketNotEmpty. This also works fine on a bucket that never had
    # versioning enabled (each object just has VersionId="null").
    #
    # Not every backend implements this API call at all, though -- Garage
    # returns a hard "NotImplemented" error for ListObjectVersions (not just
    # "no versions exist"). Fall back to the plain, universally-supported
    # list_objects_v2 in that case.
    try:
        versions = s3_client.list_object_versions(Bucket=bucket_name)
        object_versions = versions.get("Versions", [])
        delete_markers = versions.get("DeleteMarkers", [])
        logger.info(
            f"  list_object_versions: HTTP "
            f"{versions['ResponseMetadata']['HTTPStatusCode']}, "
            f"{len(object_versions)} version(s), "
            f"{len(delete_markers)} delete marker(s)"
        )
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] != "NotImplemented":
            raise
        logger.info(
            f"  list_object_versions refused: HTTP "
            f"{e.response['ResponseMetadata']['HTTPStatusCode']} "
            f"{e.response['Error']['Code']}, falling back to list_objects_v2"
        )
        listing = s3_client.list_objects_v2(Bucket=bucket_name)
        objects = listing.get("Contents", [])
        logger.info(
            f"  list_objects_v2: HTTP "
            f"{listing['ResponseMetadata']['HTTPStatusCode']}, "
            f"{len(objects)} object(s)"
        )
        object_versions = [{"Key": obj["Key"], "VersionId": None} for obj in objects]
        delete_markers = []

    logger.info(f"  deleting {len(object_versions)} object version(s)")
    for version in object_versions:
        if version["VersionId"] is None:
            s3_client.delete_object(Bucket=bucket_name, Key=version["Key"])
        else:
            s3_client.delete_object(
                Bucket=bucket_name, Key=version["Key"], VersionId=version["VersionId"]
            )

    logger.info(f"  deleting {len(delete_markers)} delete marker(s)")
    for marker in delete_markers:
        s3_client.delete_object(
            Bucket=bucket_name, Key=marker["Key"], VersionId=marker["VersionId"]
        )

    multipart = s3_client.list_multipart_uploads(Bucket=bucket_name)
    uploads = multipart.get("Uploads", [])
    logger.info(
        f"  list_multipart_uploads: HTTP "
        f"{multipart['ResponseMetadata']['HTTPStatusCode']}, "
        f"aborting {len(uploads)} unfinished upload(s)"
    )
    for upload in uploads:
        s3_client.abort_multipart_upload(
            Bucket=bucket_name, Key=upload["Key"], UploadId=upload["UploadId"]
        )


@pytest.fixture(autouse=True)
def log_test_boundaries(request):
    """Runs for every single test automatically (autouse=True). Logs when
    a test starts and when it ends, so the log output shows the complete
    order of execution, not just fixture setup/teardown."""
    logger.info(f">>> START {request.node.nodeid}")
    yield
    logger.info(f"<<< END   {request.node.nodeid}")


# --- Recording which optional S3 features each backend supports ---
# One list, shared by every test in the session, holding rows like:
# {"backend": "rustfs", "feature": "bucket_versioning", "supported": True, "detail": "..."}


@pytest.fixture(scope="session")
def capability_results():
    """One empty list, created once per test session and handed out to
    every test that asks for it -- so they all add rows to the SAME list."""
    return []


@pytest.fixture
def record_capability(backend_name, capability_results):
    """Gives a test a `record_capability("feature", True/False)` function
    to call, instead of writing to the list directly."""

    def record(feature_name: str, is_supported: bool, detail: str = ""):
        logger.info(
            f"[{backend_name}] capability '{feature_name}' supported={is_supported} "
            f"({detail})"
        )
        capability_results.append(
            {
                "backend": backend_name,
                "feature": feature_name,
                "supported": is_supported,
                "detail": detail,
            }
        )

    return record


@pytest.fixture(scope="session", autouse=True)
def save_capability_results(capability_results):
    """Runs automatically for every test session (autouse=True). Does
    nothing until all tests have finished (that's what the `yield` does),
    then saves everything `record_capability` collected to a CSV file.

    Results from previous runs are kept and merged, rather than the file
    being rewritten from scratch. This matters because backends are
    deliberately tested one at a time here (only one fits in RAM
    comfortably), often with `pytest -k <backend>` -- rewriting the file
    would silently wipe every other backend's results and leave the
    comparison table looking empty. A row is replaced only when the same
    (backend, feature) is measured again, so re-running a backend updates
    its own numbers without touching anyone else's."""
    yield

    logger.info("All tests finished, saving capability results")
    RESULTS_DIR.mkdir(exist_ok=True)
    output_path = RESULTS_DIR / "compatibility_matrix.csv"

    merged = {}
    if output_path.exists():
        with open(output_path, newline="") as f:
            for row in csv.DictReader(f):
                merged[(row["backend"], row["feature"])] = row
        logger.info(f"  loaded {len(merged)} existing result(s) to merge into")

    for result in capability_results:
        merged[(result["backend"], result["feature"])] = result

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["backend", "feature", "supported", "detail"])
        writer.writeheader()
        writer.writerows(merged.values())

    logger.info(
        f"Saved {len(capability_results)} new result(s), "
        f"{len(merged)} total, to {output_path}"
    )
