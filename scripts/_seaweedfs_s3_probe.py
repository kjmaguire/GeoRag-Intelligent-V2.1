"""Probe SeaweedFS S3 with various aioboto3 Config combinations.

The bug we're chasing: list_buckets succeeds but ListObjectsV2 + PutObject
fail with SignatureDoesNotMatch. So per-bucket operations need a different
Config than the global ones.
"""
import asyncio
import os

import aioboto3
from botocore.config import Config


async def try_ops(label: str, cfg: Config) -> None:
    sess = aioboto3.Session(
        aws_access_key_id=os.environ["MRU"],
        aws_secret_access_key=os.environ["MRP"],
        region_name="us-east-1",
    )
    async with sess.client("s3", endpoint_url="http://minio:8333", config=cfg) as s3:
        steps = []
        try:
            await s3.list_buckets()
            steps.append("list_buckets:OK")
        except Exception as e:
            steps.append(f"list_buckets:FAIL({type(e).__name__})")
        try:
            r = await s3.list_objects_v2(Bucket="tier-hot")
            steps.append(f"list_objects_v2:OK(n={r.get('KeyCount', 0)})")
        except Exception as e:
            steps.append(f"list_objects_v2:FAIL({type(e).__name__}:{str(e)[:80]})")
        try:
            await s3.put_object(Bucket="tier-hot", Key="phase0-probe.txt", Body=b"hello-from-probe")
            steps.append("put_object:OK")
        except Exception as e:
            steps.append(f"put_object:FAIL({type(e).__name__}:{str(e)[:80]})")
        # Cleanup
        try:
            await s3.delete_object(Bucket="tier-hot", Key="phase0-probe.txt")
        except Exception:
            pass
        print(f"  [{label}]")
        for s in steps:
            print(f"      {s}")


async def main() -> None:
    await try_ops("default-virtual-v4", Config(signature_version="s3v4"))
    await try_ops("path-style-v4", Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
    ))
    await try_ops("path-style-v2", Config(
        signature_version="s3",
        s3={"addressing_style": "path"},
    ))
    await try_ops("path-style-v4-unsigned-payload", Config(
        signature_version="s3v4",
        s3={"addressing_style": "path", "payload_signing_enabled": False},
    ))
    await try_ops("virtual-v4-unsigned-payload", Config(
        signature_version="s3v4",
        s3={"payload_signing_enabled": False},
    ))


asyncio.run(main())
