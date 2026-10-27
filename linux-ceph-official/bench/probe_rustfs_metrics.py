"""
Probe RustFS's candidate metrics endpoints with a properly SigV4-signed
request, since anonymous probes returned HTTP 403 (route exists, auth
required) rather than 404 (route absent).

RustFS aims for MinIO API compatibility, and MinIO's own admin/metrics
routes require AWS SigV4 signing against the "s3" service just like normal
S3 calls. This script signs GET requests the same way boto3/awscli would.
"""

import logging

import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

logger = logging.getLogger("thesis")

ENDPOINT = "http://localhost:9000"
ACCESS_KEY = "thesisaccesskey"
SECRET_KEY = "thesissecretkey1234"
REGION = "us-east-1"

CANDIDATE_PATHS = [
    "/minio/v2/metrics/cluster",
    "/minio/v2/metrics/node",
    "/metrics",
    "/metrics/cluster",
]


def signed_get(path: str) -> requests.Response:
    url = ENDPOINT + path
    logger.info(f"Signing a GET request for {url}")
    request = AWSRequest(method="GET", url=url)
    credentials = Credentials(ACCESS_KEY, SECRET_KEY)
    S3SigV4Auth(credentials, "s3", REGION).add_auth(request)
    prepared = request.prepare()

    logger.info(f"Sending request to {ENDPOINT}")
    response = requests.get(prepared.url, headers=dict(prepared.headers))
    logger.info(f"Got HTTP {response.status_code}")
    return response


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info(f"Probing {len(CANDIDATE_PATHS)} candidate metrics path(s) on {ENDPOINT}")
    for path in CANDIDATE_PATHS:
        try:
            resp = signed_get(path)
            body_preview = resp.text[:200].replace("\n", " ")
            print(f"{path:35s} -> HTTP {resp.status_code} | {body_preview}")
        except Exception as exc:
            logger.warning(f"Request to {path} failed: {exc}")
            print(f"{path:35s} -> ERROR: {exc}")
