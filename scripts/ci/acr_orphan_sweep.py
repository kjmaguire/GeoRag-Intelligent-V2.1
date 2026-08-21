#!/usr/bin/env python3
"""
Delete ACR manifests that nothing can pull, which `acr purge --untagged` misses.

Why this exists
---------------
The daily `purge-old-images` ACR task runs

    acr purge --filter 'georag/<repo>:^[0-9a-f]{7}$' --untagged --ago 0d --keep N

and that handles *tags* correctly. Its `--untagged` pass, however, only ever
removes manifests in the legacy `application/vnd.docker.distribution.manifest.v2+json`
format. Every untagged OCI manifest is left behind, permanently.

That became a leak on 2026-08-20, when CD switched to `docker/build-push-action`
with buildx. Buildx pushes:

  * an `application/vnd.oci.image.index.v1+json` INDEX, which carries the tag,
    and whose children (the real platform image + an attestation manifest) are
    UNTAGGED but very much alive;
  * a separate ~4 GB registry cache manifest under the `buildcache` tag
    (`cache-to: type=registry,...,mode=max`). Each build moves that tag, and the
    previous generation becomes an untagged OCI orphan referenced by nothing.

So the registry grew by roughly one image + one cache generation per CD run with
nothing reclaiming either. Measured 2026-08-20: 59.05 GB in a Basic registry
whose included quota is 10 GB, of which 37.9 GB was unreachable garbage.

The rule this script implements
-------------------------------
A manifest is deletable iff it is BOTH:

  1. untagged, AND
  2. not listed in the `manifests[]` array of any *surviving* index manifest.

Condition 2 is the one `acr purge` cannot express, and it is the one that makes
this safe: the untagged children of a live tag are exactly what a naive
"delete everything untagged" sweep would destroy, taking production down with it.

Run with --dry-run (the default in CI on a pull request) to print the plan
without touching anything.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}


# On Windows `az` is a .cmd shim, which subprocess will not find by bare name.
# Resolving it once keeps the call sites free of shell=True (which would re-parse
# arguments, and digests are the last thing that should be re-parsed).
AZ = shutil.which("az") or "az"


def az(*args: str) -> str:
    """Run an `az` command and return stdout, raising with stderr on failure."""
    proc = subprocess.run(
        [AZ, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if proc.returncode != 0:
        raise RuntimeError(f"az {' '.join(args)} failed:\n{proc.stderr.strip()}")
    return proc.stdout


def list_manifests(registry: str, repo: str) -> list[dict]:
    out = az(
        "acr", "manifest", "list-metadata",
        "-r", registry, "-n", repo,
        "--query", "[].{digest:digest,mediaType:mediaType,tags:tags,size:imageSize,created:createdTime}",
        "-o", "json",
    )
    return json.loads(out or "[]")


def index_children(registry: str, repo: str, digest: str) -> list[str]:
    out = az("acr", "manifest", "show", "-r", registry, "-n", f"{repo}@{digest}", "-o", "json")
    return [child["digest"] for child in json.loads(out).get("manifests", [])]


def sweep(registry: str, repo: str, dry_run: bool) -> int:
    manifests = list_manifests(registry, repo)

    referenced: set[str] = set()
    for manifest in manifests:
        if manifest["mediaType"] in INDEX_MEDIA_TYPES:
            referenced.update(index_children(registry, repo, manifest["digest"]))

    keep = {m["digest"] for m in manifests if m["tags"]} | referenced
    orphans = [m for m in manifests if m["digest"] not in keep]

    reclaimed = sum(m["size"] or 0 for m in orphans)
    print(
        f"{repo}: {len(manifests)} manifests, "
        f"{len({m['digest'] for m in manifests if m['tags']})} tagged, "
        f"{len(referenced)} referenced by a live index, "
        f"{len(orphans)} orphaned ({reclaimed / 1e9:.2f} GB nominal)"
    )

    failures = 0
    for manifest in sorted(orphans, key=lambda m: -(m["size"] or 0)):
        label = f"{manifest['digest'][:19]}  {(manifest['size'] or 0) / 1e9:6.2f} GB  {manifest['created'][:19]}"
        if dry_run:
            print(f"  would delete  {label}")
            continue
        try:
            az("acr", "repository", "delete", "-n", registry,
               "--image", f"{repo}@{manifest['digest']}", "--yes")
            print(f"  deleted       {label}")
        except RuntimeError as exc:
            # A concurrent CD run can retag or re-push between the listing and
            # the delete. Report and keep going rather than failing the sweep.
            print(f"  FAILED        {label}\n                {exc}", file=sys.stderr)
            failures += 1

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="georagacrcc")
    parser.add_argument("--repository", action="append", default=None,
                        help="repeatable; defaults to georag/fastapi and georag/laravel")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan without deleting anything")
    args = parser.parse_args()

    repos = args.repository or ["georag/fastapi", "georag/laravel"]

    failures = 0
    for repo in repos:
        failures += sweep(args.registry, repo, args.dry_run)

    if failures:
        print(f"\n{failures} manifest(s) could not be deleted.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
