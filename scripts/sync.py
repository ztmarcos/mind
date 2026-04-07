#!/usr/bin/env python3
"""
Sync local Obsidian vault <-> S3 prefixes raw/ and wiki/

  python scripts/sync.py up    # upload all *.md from vault to s3://$S3_BUCKET/raw/
  python scripts/sync.py down  # download s3://$S3_BUCKET/raw/ and wiki/ into vault/_sync/

Environment:
  OBSIDIAN_VAULT_DIR  - absolute path to vault root (required)
  S3_BUCKET           - bucket name from stack Outputs (required)
  AWS_REGION          - optional, default from boto3 session
  AWS_PROFILE         - optional
"""

from __future__ import annotations

import argparse
import os
import sys

import boto3
from botocore.exceptions import ClientError


def _session():
    kwargs = {}
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    profile = os.environ.get("AWS_PROFILE")
    if profile:
        return boto3.Session(profile_name=profile, region_name=region)
    return boto3.Session(region_name=region)


def _iter_local_markdown(vault: str):
    for root, _dirs, files in os.walk(vault):
        # skip sync output tree to avoid re-uploading downloads
        if f"{os.sep}_sync{os.sep}" in root + os.sep:
            continue
        for name in files:
            if not name.lower().endswith(".md"):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, vault)
            yield rel.replace(os.sep, "/")


def cmd_up(bucket: str, vault: str) -> int:
    s3 = _session().client("s3")
    count = 0
    for rel in _iter_local_markdown(vault):
        key = f"raw/{rel}"
        path = os.path.join(vault, rel.replace("/", os.sep))
        with open(path, "rb") as f:
            body = f.read()
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="text/markdown; charset=utf-8",
        )
        print(f"upload s3://{bucket}/{key}")
        count += 1
    print(f"done: {count} files")
    return 0


def _download_prefix(s3, bucket: str, prefix: str, dest_dir: str) -> int:
    os.makedirs(dest_dir, exist_ok=True)
    count = 0
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents") or []:
            key = obj["Key"]
            if key.endswith("/"):
                continue
            rel = key[len(prefix) :].lstrip("/")
            if not rel:
                continue
            local_path = os.path.join(dest_dir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            s3.download_file(bucket, key, local_path)
            print(f"download s3://{bucket}/{key} -> {local_path}")
            count += 1
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break
    return count


def cmd_down(bucket: str, vault: str) -> int:
    s3 = _session().client("s3")
    base = os.path.join(vault, "_sync")
    raw_dir = os.path.join(base, "raw")
    wiki_dir = os.path.join(base, "wiki")
    n1 = _download_prefix(s3, bucket, "raw/", raw_dir)
    n2 = _download_prefix(s3, bucket, "wiki/", wiki_dir)
    print(f"done: raw={n1} wiki={n2} -> {base}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Obsidian vault S3 sync")
    p.add_argument("command", choices=["up", "down"])
    args = p.parse_args()

    vault = os.environ.get("OBSIDIAN_VAULT_DIR", "").strip()
    bucket = os.environ.get("S3_BUCKET", "").strip()
    if not vault or not os.path.isdir(vault):
        print("OBSIDIAN_VAULT_DIR must be set to an existing directory", file=sys.stderr)
        return 1
    if not bucket:
        print("S3_BUCKET must be set to the deployment bucket name", file=sys.stderr)
        return 1

    try:
        if args.command == "up":
            return cmd_up(bucket, vault)
        return cmd_down(bucket, vault)
    except ClientError as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
