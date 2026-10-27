"""
Performance benchmark harness, built on boto3 rather than shelling out to
ossperf. The methodology follows ossperf
(github.com/christianbaun/ossperf): for a given (num_files, size_bytes)
workload, create a bucket, upload N files, list them, download them back,
verify an md5 checksum per object, delete the objects and delete the
bucket, timing every phase and deriving upload/download bandwidth.

Usage: python bench/benchmark.py [backend_name] [output_csv_name]
Reads tests/config.yaml for enabled backends, runs the workload matrix
defined in WORKLOAD_MATRIX below against each (REPEATS times per workload,
for statistical variance), appends rows to bench/results/benchmark_results.csv.

An optional backend_name argument restricts the run to just that one
backend -- e.g. `python bench/benchmark.py rustfs` -- so backends can be
benchmarked one at a time instead of competing for the same CPU cores.

An optional second argument overrides the output CSV filename (still saved
under bench/results/). Use this for runs made under different conditions
than the normal baseline -- e.g. `python bench/benchmark.py rustfs
monitoring_benchmark_results.csv` for the instrumented pass, so that data
(measured with the monitoring stack's own CPU and scraping overhead also
running) doesn't get mixed into the plain baseline numbers.
"""

import csv
import hashlib
import logging
import os
import random
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import boto3
import yaml

logger = logging.getLogger("thesis")

REPO_ROOT = Path(__file__).parent.parent
CONFIG_PATH = REPO_ROOT / "tests" / "config.yaml"
RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_CSV = RESULTS_DIR / "benchmark_results.csv"

# How many times to repeat each workload point, so that a median and a
# measure of dispersion can be reported instead of a single noisy sample.
#
# Raised from 5 to 20: with 5 repeats the 16MB parallel workload varied too
# much to order the backends, and two full campaigns disagreed on the
# middle of the field. The variation comes from the environment (a 2-core,
# 4-thread host running Docker Desktop on WSL2), so it cannot be removed,
# only sampled often enough to describe.
REPEATS = int(os.environ.get("BENCH_REPEATS", "20"))
# The instrumented pass overrides this with BENCH_REPEATS, because its
# purpose is a resource measurement rather than a throughput sample.

# Workloads are shuffled within each repeat rather than run in blocks, so
# that drift over a campaign -- background activity on the host, thermal
# state, data accumulating inside a backend -- is spread across all
# workloads instead of being charged to whichever one happened to run last.
# The seed keeps a campaign reproducible.
SHUFFLE_SEED = 20260912

# Upper bound on concurrent requests in the parallel variants. The actual
# count is min(MAX_WORKERS, num_files), so the 16MB workload (2 files) runs
# two requests at once and the 1KB workload (10 files) runs eight.
MAX_WORKERS = 8

CSV_FIELDS = [
    "timestamp",
    "backend",
    "repeat_index",
    "num_files",
    "size_bytes",
    "parallel",
    "threads",
    "time_create_bucket_s",
    "time_upload_s",
    "time_list_s",
    "time_download_s",
    "time_delete_objects_s",
    "time_delete_bucket_s",
    "time_total_s",
    "bandwidth_upload_Bps",
    "bandwidth_download_Bps",
    "integrity_ok",
    "error",
]

# Resource-appropriate for an 8GB/2-core machine -- small counts/sizes, run
# both sequentially and in parallel so the two can be compared. The 16MB
# point matches ossperf's own file-size cap.
WORKLOAD_MATRIX = [
    # (num_files, size_bytes, parallel)
    (10, 1024, False),  # 1KB x 10, sequential
    (10, 1024, True),  # 1KB x 10, parallel
    (10, 100 * 1024, False),  # 100KB x 10, sequential
    (10, 100 * 1024, True),  # 100KB x 10, parallel
    (5, 1024 * 1024, False),  # 1MB x 5, sequential
    (5, 1024 * 1024, True),  # 1MB x 5, parallel
    (2, 16 * 1024 * 1024, False),  # 16MB x 2, sequential
    (2, 16 * 1024 * 1024, True),  # 16MB x 2, parallel
]


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


def make_client(backend_config):
    logger.info(f"Creating boto3 S3 client for {backend_config['endpoint']}")
    return boto3.client(
        "s3",
        endpoint_url=backend_config["endpoint"],
        aws_access_key_id=backend_config["access_key"],
        aws_secret_access_key=backend_config["secret_key"],
        region_name=backend_config.get("region", "us-east-1"),
    )


def time_it(func):
    """Runs func() with no arguments and returns (result, seconds_taken)."""
    start = time.perf_counter()
    result = func()
    seconds_taken = time.perf_counter() - start
    return result, seconds_taken


def upload_file(s3_client, bucket_name, name, data):
    s3_client.put_object(Bucket=bucket_name, Key=name, Body=data)


def upload_all_files(s3_client, bucket_name, files, parallel):
    logger.info(f"  uploading {len(files)} file(s), parallel={parallel}")
    if not parallel:
        for name, data in files.items():
            upload_file(s3_client, bucket_name, name, data)
        return

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(files))) as pool:
        futures = [
            pool.submit(upload_file, s3_client, bucket_name, name, data)
            for name, data in files.items()
        ]
        for future in futures:
            future.result()  # re-raises if any upload failed


def download_file(s3_client, bucket_name, name):
    """Downloads one object and returns its bytes. Checksums are compared
    afterwards, outside the timed phase, so that the download figure
    measures the transfer rather than the client's hashing speed."""
    response = s3_client.get_object(Bucket=bucket_name, Key=name)
    return response["Body"].read()


def download_all_files(s3_client, bucket_name, names, parallel):
    """Downloads every file and returns {name: bytes}."""
    logger.info(f"  downloading {len(names)} file(s), parallel={parallel}")

    if not parallel:
        return {name: download_file(s3_client, bucket_name, name) for name in names}

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(names))) as pool:
        downloaded = list(pool.map(lambda name: download_file(s3_client, bucket_name, name), names))
    return dict(zip(names, downloaded))


def verify_checksums(downloaded, checksums):
    """Returns True only if every downloaded object matches its checksum."""
    for name, data in downloaded.items():
        if hashlib.md5(data).hexdigest() != checksums[name]:
            logger.warning(f"  integrity mismatch on {name}!")
            return False
    return True


def run_workload(
    s3_client, num_files: int, size_bytes: int, parallel: bool, fixed_bucket: str = None
) -> dict:
    """Runs one benchmark workload against s3_client and returns timing/
    bandwidth/integrity results as a dict, ready to be written as a CSV row.

    Normally each run gets its own fresh bucket. A backend that cannot
    create buckets on demand passes fixed_bucket
    instead: the run then reuses that one bucket and puts its objects under
    a unique key prefix, so runs still don't interfere with each other.
    Those runs report 0.0 for the bucket create/delete phases, since no
    such call is made -- their upload/download numbers are still directly
    comparable, which is the point."""
    run_id = uuid.uuid4().hex[:12]
    logger.info(f"Workload: {num_files} file(s) x {size_bytes} bytes, parallel={parallel}")

    if fixed_bucket:
        bucket_name = fixed_bucket
        key_prefix = f"bench-{run_id}/"
    else:
        bucket_name = f"bench-{run_id}"
        key_prefix = ""

    files = {
        f"{key_prefix}object-{i:04d}.bin": os.urandom(size_bytes) for i in range(num_files)
    }
    checksums = {name: hashlib.md5(data).hexdigest() for name, data in files.items()}

    if fixed_bucket:
        logger.info(f"  reusing fixed bucket {bucket_name} with key prefix {key_prefix}")
        time_create_bucket = 0.0
    else:
        logger.info(f"  creating bucket {bucket_name}")
        _, time_create_bucket = time_it(lambda: s3_client.create_bucket(Bucket=bucket_name))

    _, time_upload = time_it(lambda: upload_all_files(s3_client, bucket_name, files, parallel))
    logger.info(f"  upload phase took {time_upload:.4f}s")

    def list_uploaded_keys():
        response = s3_client.list_objects_v2(Bucket=bucket_name, Prefix=key_prefix)
        return {obj["Key"] for obj in response.get("Contents", [])}

    logger.info("  listing uploaded objects to confirm they're all there")
    listed_keys, time_list = time_it(list_uploaded_keys)
    assert listed_keys == set(files.keys()), (
        f"listing mismatch: expected {set(files.keys())}, got {listed_keys}"
    )

    downloaded, time_download = time_it(
        lambda: download_all_files(s3_client, bucket_name, list(files), parallel)
    )
    integrity_ok = verify_checksums(downloaded, checksums)
    logger.info(f"  download phase took {time_download:.4f}s, integrity_ok={integrity_ok}")

    def delete_all_objects():
        for name in files:
            s3_client.delete_object(Bucket=bucket_name, Key=name)

    logger.info(f"  deleting {len(files)} object(s)")
    _, time_delete_objects = time_it(delete_all_objects)

    if fixed_bucket:
        time_delete_bucket = 0.0
    else:
        logger.info(f"  deleting bucket {bucket_name}")
        _, time_delete_bucket = time_it(lambda: s3_client.delete_bucket(Bucket=bucket_name))

    total_bytes = num_files * size_bytes
    total_time = (
        time_create_bucket
        + time_upload
        + time_list
        + time_download
        + time_delete_objects
        + time_delete_bucket
    )
    logger.info(f"  workload finished in {total_time:.4f}s total")

    return {
        "threads": min(MAX_WORKERS, num_files) if parallel else 1,
        "time_create_bucket_s": round(time_create_bucket, 4),
        "time_upload_s": round(time_upload, 4),
        "time_list_s": round(time_list, 4),
        "time_download_s": round(time_download, 4),
        "time_delete_objects_s": round(time_delete_objects, 4),
        "time_delete_bucket_s": round(time_delete_bucket, 4),
        "time_total_s": round(total_time, 4),
        "bandwidth_upload_Bps": round(total_bytes / time_upload, 2) if time_upload > 0 else 0,
        "bandwidth_download_Bps": round(total_bytes / time_download, 2)
        if time_download > 0
        else 0,
        "integrity_ok": integrity_ok,
    }


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    RESULTS_DIR.mkdir(exist_ok=True)
    backends = load_enabled_backends()

    backend_filter = sys.argv[1] if len(sys.argv) > 1 else None
    if backend_filter:
        if backend_filter not in backends:
            raise SystemExit(f"'{backend_filter}' is not an enabled backend in {CONFIG_PATH}")
        backends = {backend_filter: backends[backend_filter]}

    output_csv = RESULTS_DIR / sys.argv[2] if len(sys.argv) > 2 else RESULTS_CSV
    write_header = not output_csv.exists()

    with open(output_csv, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()

        for backend_name, backend_config in backends.items():
            logger.info(f"=== Backend: {backend_name} ===")
            client = make_client(backend_config)

            # One unrecorded warm-up run per workload: the first run against a
            # freshly started backend pays for cold caches and for data
            # structures the backend creates lazily.
            for num_files, size_bytes, parallel in WORKLOAD_MATRIX:
                try:
                    run_workload(client, num_files, size_bytes, parallel,
                                 backend_config.get("fixed_bucket"))
                except Exception as e:
                    logger.warning(f"  warm-up failed for {size_bytes}B: {e}")

            shuffler = random.Random(SHUFFLE_SEED)
            for repeat_index in range(REPEATS):
                logger.info(f"  repeat {repeat_index + 1}/{REPEATS}")
                workloads = list(WORKLOAD_MATRIX)
                shuffler.shuffle(workloads)
                for num_files, size_bytes, parallel in workloads:
                    base_row = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "backend": backend_name,
                        "repeat_index": repeat_index,
                        "num_files": num_files,
                        "size_bytes": size_bytes,
                        "parallel": parallel,
                        "error": "",
                    }

                    # A single workload/backend failing (e.g. JuiceFS's
                    # documented single-bucket limitation rejecting an
                    # arbitrary bucket name) shouldn't lose every other
                    # backend's data for the rest of this run -- record it
                    # as a real result row and move on, same "don't hide
                    # the finding" approach used throughout this project.
                    try:
                        result = run_workload(
                            client,
                            num_files,
                            size_bytes,
                            parallel,
                            backend_config.get("fixed_bucket"),
                        )
                        row = {**base_row, **result}
                        status = "OK" if result["integrity_ok"] else "INTEGRITY MISMATCH"
                        logger.info(
                            f"  RESULT upload={result['time_upload_s']}s "
                            f"download={result['time_download_s']}s [{status}]"
                        )
                    except Exception as e:
                        logger.error(f"  workload failed: {e}")
                        row = {**base_row, "integrity_ok": False, "error": str(e)}

                    writer.writerow(row)
                    f.flush()

    logger.info(f"Results written to {output_csv}")


if __name__ == "__main__":
    main()
