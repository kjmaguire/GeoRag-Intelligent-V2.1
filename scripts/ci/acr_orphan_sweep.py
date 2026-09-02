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
A manifest is deletable iff it is ALL of:

  1. untagged, AND
  2. not listed in the `manifests[]` array of any *surviving* index manifest, AND
  3. of a media type this script positively recognises. A null or unrecognised
     media type may be an index format this code predates, so such a manifest is
     never deleted and any children it declares are protected as if it were live.

Condition 2 is the one `acr purge` cannot express, and it is the one that makes
this safe: the untagged children of a live tag are exactly what a naive
"delete everything untagged" sweep would destroy, taking production down with it.
As a hard corollary, an index manifest whose `manifests[]` comes back empty is
treated as a corrupt listing and aborts the sweep — acting on it would silently
unprotect every child of that index.

Deletion is opt-in
------------------
Without --delete this script only prints its plan. A CI invocation that passes
no flag — scheduled or otherwise — is therefore a dry run by construction, and
a workflow-expression bug fails safe. Just before each real delete the
manifest's tags are re-read and anything that has gained a tag since the
listing is kept: `az acr repository delete --image <repo>@<digest>` removes all
tags on the manifest along with it, and a CD build finishing mid-sweep moves
`buildcache` onto content-addressed digests that can match a listed orphan.
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

# Media types known to be single images with no children of their own. Anything
# outside this set and INDEX_MEDIA_TYPES is out of contract: kept, never deleted.
LEAF_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}


# On Windows `az` is a .cmd shim, which subprocess will not find by bare name.
# Resolving it once keeps the call sites free of shell=True (which would re-parse
# arguments, and digests are the last thing that should be re-parsed).
AZ = shutil.which("az") or "az"


def az(*args: str) -> str:
    """Run an `az` command and return stdout, raising with stderr on failure."""
    proc = subprocess.run(
        [AZ, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL,
        check=False,
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


def declared_children(registry: str, repo: str, digest: str) -> list[str]:
    out = az("acr", "manifest", "show", "-r", registry, "-n", f"{repo}@{digest}", "-o", "json")
    return [child["digest"] for child in json.loads(out).get("manifests") or []]


def index_children(registry: str, repo: str, digest: str) -> list[str]:
    children = declared_children(registry, repo, digest)
    if not children:
        # A real index always lists children (a buildx index has at least the
        # platform image and an attestation). Zero children means the listing
        # cannot be trusted, and proceeding would unprotect every child of
        # this index — so nothing gets deleted at all.
        raise RuntimeError(
            f"{repo}@{digest} has an index media type but declares no children; "
            f"refusing to sweep {repo} on a listing that would unprotect them"
        )
    return children


def current_tags(registry: str, repo: str, digest: str) -> list[str]:
    out = az("acr", "manifest", "show-metadata", "-r", registry, "-n", f"{repo}@{digest}", "-o", "json")
    return json.loads(out).get("tags") or []


def sweep(registry: str, repo: str, delete: bool) -> int:
    manifests = list_manifests(registry, repo)

    referenced: set[str] = set()
    unknown: set[str] = set()
    for manifest in manifests:
        media_type = manifest.get("mediaType")
        if media_type in INDEX_MEDIA_TYPES:
            referenced.update(index_children(registry, repo, manifest["digest"]))
        elif media_type not in LEAF_MEDIA_TYPES:
            # Null, or a format this script predates. It may itself be an
            # index, so keep it and protect whatever children it declares.
            unknown.add(manifest["digest"])
            referenced.update(declared_children(registry, repo, manifest["digest"]))

    keep = {m["digest"] for m in manifests if m["tags"]} | referenced | unknown
    orphans = [m for m in manifests if m["digest"] not in keep]

    reclaimed = sum(m["size"] or 0 for m in orphans)
    print(
        f"{repo}: {len(manifests)} manifests, "
        f"{len({m['digest'] for m in manifests if m['tags']})} tagged, "
        f"{len(referenced)} referenced by a live index, "
        f"{len(unknown)} of unknown media type (kept), "
        f"{len(orphans)} orphaned ({reclaimed / 1e9:.2f} GB nominal)"
    )

    failures = 0
    for manifest in sorted(orphans, key=lambda m: -(m["size"] or 0)):
        label = (
            f"{manifest['digest'][:19]}  {(manifest['size'] or 0) / 1e9:6.2f} GB  "
            f"{(manifest['created'] or '')[:19]}"
        )
        if not delete:
            print(f"  would delete  {label}")
            continue
        try:
            # A CD run can retag or re-push between the listing and this point
            # — most plausibly `buildcache`, which every build rewrites — and
            # the delete below takes a manifest's tags down with it. Re-read
            # the tags at the last moment and keep anything no longer orphaned.
            tags = current_tags(registry, repo, manifest["digest"])
            if tags:
                print(f"  kept          {label}  (now tagged: {', '.join(tags)})")
                continue
            az("acr", "repository", "delete", "-n", registry,
               "--image", f"{repo}@{manifest['digest']}", "--yes")
            print(f"  deleted       {label}")
        except RuntimeError as exc:
            print(f"  FAILED        {label}\n                {exc}", file=sys.stderr)
            failures += 1

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="georagacrcc")
    parser.add_argument("--repository", action="append", default=None,
                        help="repeatable; defaults to georag/fastapi and georag/laravel")
    parser.add_argument("--delete", action="store_true",
                        help="actually delete; without this flag only the plan is printed")
    args = parser.parse_args()

    repos = args.repository or ["georag/fastapi", "georag/laravel"]

    failures = 0
    for repo in repos:
        failures += sweep(args.registry, repo, args.delete)

    if failures:
        print(f"\n{failures} manifest(s) could not be deleted.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
