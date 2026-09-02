#!/usr/bin/env python3
"""Behavioural pins for scripts/ci/acr_orphan_sweep.py.

The sweeper's failure mode is irreversible deletion from the registry
production pulls from, so the safety properties fixed after the 2026-08-24
audit are pinned here:

  * dry run is the default — deletion happens only with an explicit --delete,
    so a scheduled workflow run that passes no flag can never delete;
  * an index manifest that declares no children aborts the sweep before any
    deletion, instead of silently unprotecting every child of a live index;
  * a manifest with a null or unrecognised media type is never deleted, and
    the children it declares are protected as if it were live;
  * a null createdTime cannot crash the loop after deletions have started;
  * a manifest that gained a tag between the listing and the delete is kept
    (the buildcache retag race — `az acr repository delete --image` removes
    the manifest's tags along with it).

Stdlib only; run directly:  python scripts/ci/tests/test_acr_orphan_sweep.py
"""

from __future__ import annotations

import importlib.util
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import ClassVar
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "acr_orphan_sweep", Path(__file__).resolve().parents[1] / "acr_orphan_sweep.py"
)
sweeper = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sweeper)

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_IMAGE = "application/vnd.oci.image.manifest.v1+json"


def entry(digest, media_type=OCI_IMAGE, tags=None, size=1_000_000_000,
          created="2026-08-20T08:00:00Z"):
    """One row as `az acr manifest list-metadata` projects it (nulls included)."""
    return {"digest": digest, "mediaType": media_type, "tags": tags,
            "size": size, "created": created}


class FakeAz:
    """Answers the az() calls the sweeper makes from an in-memory registry.

    `children` maps digest -> child digests for `az acr manifest show`; a
    digest mapped to [] yields {"manifests": []}, an unmapped digest yields a
    body with no `manifests` key at all. `tags_now` is what show-metadata
    reports at delete time, i.e. AFTER the listing was taken.
    """

    def __init__(self, listing, children=None, tags_now=None, fail_deletes=()):
        self.listing = listing
        self.children = children or {}
        self.tags_now = tags_now or {}
        self.fail_deletes = set(fail_deletes)
        self.deleted = []

    @staticmethod
    def _digest(args, flag):
        return args[args.index(flag) + 1].split("@", 1)[1]

    def __call__(self, *args):
        args = list(args)
        if args[:3] == ["acr", "manifest", "list-metadata"]:
            return json.dumps(self.listing)
        if args[:3] == ["acr", "manifest", "show-metadata"]:
            return json.dumps({"tags": self.tags_now.get(self._digest(args, "-n"), [])})
        if args[:3] == ["acr", "manifest", "show"]:
            digest = self._digest(args, "-n")
            if digest in self.children:
                return json.dumps({"manifests": [{"digest": d} for d in self.children[digest]]})
            return json.dumps({"mediaType": "whatever"})
        if args[:3] == ["acr", "repository", "delete"]:
            digest = self._digest(args, "--image")
            if digest in self.fail_deletes:
                raise RuntimeError("simulated delete failure")
            self.deleted.append(digest)
            return ""
        raise AssertionError(f"unexpected az call: {args}")


def run_sweep(fake, delete):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sweeper, "az", fake), \
            redirect_stdout(out), redirect_stderr(err):
        failures = sweeper.sweep("reg", "georag/fastapi", delete)
    return failures, out.getvalue(), err.getvalue()


class DeleteIsOptIn(unittest.TestCase):
    """A run without --delete is a plan; only an explicit --delete deletes."""

    LISTING: ClassVar[list[dict]] = [entry("sha256:orphan")]

    def test_sweep_without_delete_touches_nothing(self):
        fake = FakeAz(self.LISTING)
        failures, out, _ = run_sweep(fake, delete=False)
        self.assertEqual(fake.deleted, [])
        self.assertEqual(failures, 0)
        self.assertIn("would delete", out)

    def test_cli_default_is_dry_run(self):
        # The workflow's scheduled invocation passes no flag at all — this is
        # the exact argv it produces, and it must never reach a delete call.
        fake = FakeAz(self.LISTING)
        argv = ["acr_orphan_sweep.py", "--repository", "georag/fastapi"]
        with mock.patch.object(sweeper, "az", fake), \
                mock.patch("sys.argv", argv), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(sweeper.main(), 0)
        self.assertEqual(fake.deleted, [])

    def test_cli_delete_flag_deletes(self):
        fake = FakeAz(self.LISTING)
        argv = ["acr_orphan_sweep.py", "--repository", "georag/fastapi", "--delete"]
        with mock.patch.object(sweeper, "az", fake), \
                mock.patch("sys.argv", argv), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(sweeper.main(), 0)
        self.assertEqual(fake.deleted, ["sha256:orphan"])


class LiveIndexChildrenSurvive(unittest.TestCase):
    def test_untagged_children_of_a_tagged_index_are_kept(self):
        listing = [
            entry("sha256:idx", OCI_INDEX, tags=["abc1234"]),
            entry("sha256:img"),      # platform image, untagged but referenced
            entry("sha256:att"),      # attestation, untagged but referenced
            entry("sha256:orphan"),   # referenced by nothing
        ]
        fake = FakeAz(listing, children={"sha256:idx": ["sha256:img", "sha256:att"]})
        failures, _, _ = run_sweep(fake, delete=True)
        self.assertEqual(fake.deleted, ["sha256:orphan"])
        self.assertEqual(failures, 0)


class ChildlessIndexAbortsTheSweep(unittest.TestCase):
    """An index that declares no children is a listing we refuse to act on."""

    def _assert_aborts(self, children):
        listing = [
            entry("sha256:idx", OCI_INDEX, tags=["abc1234"]),
            entry("sha256:orphan"),
        ]
        fake = FakeAz(listing, children=children)
        with self.assertRaises(RuntimeError):
            run_sweep(fake, delete=True)
        self.assertEqual(fake.deleted, [])

    def test_empty_manifests_array(self):
        self._assert_aborts({"sha256:idx": []})

    def test_missing_manifests_key(self):
        self._assert_aborts({})


class UnknownMediaTypesAreOutOfContract(unittest.TestCase):
    def test_unrecognised_type_is_kept_and_its_children_protected(self):
        listing = [
            entry("sha256:mystery", "application/vnd.future.artifact.v9+json"),
            entry("sha256:mystery-child"),
            entry("sha256:orphan"),
        ]
        fake = FakeAz(listing, children={"sha256:mystery": ["sha256:mystery-child"]})
        failures, out, _ = run_sweep(fake, delete=True)
        self.assertEqual(fake.deleted, ["sha256:orphan"])
        self.assertEqual(failures, 0)
        self.assertIn("1 of unknown media type (kept)", out)

    def test_null_media_type_is_kept_even_with_no_children(self):
        listing = [entry("sha256:mystery", None), entry("sha256:orphan")]
        fake = FakeAz(listing)  # probe finds no manifests[] — still kept
        _, _, _ = run_sweep(fake, delete=True)
        self.assertEqual(fake.deleted, ["sha256:orphan"])


class NullFieldsDoNotCrashMidLoop(unittest.TestCase):
    def test_null_created_and_size_survive_both_paths(self):
        listing = [
            entry("sha256:orphan-a", size=None, created=None),
            entry("sha256:orphan-b"),
        ]
        failures, out, _ = run_sweep(FakeAz(listing), delete=False)
        self.assertEqual(failures, 0)
        self.assertEqual(out.count("would delete"), 2)

        fake = FakeAz(listing)
        failures, _, _ = run_sweep(fake, delete=True)
        self.assertEqual(failures, 0)
        self.assertEqual(sorted(fake.deleted), ["sha256:orphan-a", "sha256:orphan-b"])


class RetagRaceIsClosed(unittest.TestCase):
    def test_manifest_tagged_after_listing_is_kept(self):
        # A CD build finishing mid-sweep re-pushes identical cache content and
        # moves `buildcache` onto a digest the listing saw as orphaned.
        listing = [entry("sha256:cache-gen"), entry("sha256:orphan")]
        fake = FakeAz(listing, tags_now={"sha256:cache-gen": ["buildcache"]})
        failures, out, _ = run_sweep(fake, delete=True)
        self.assertEqual(fake.deleted, ["sha256:orphan"])
        self.assertEqual(failures, 0)
        self.assertIn("now tagged: buildcache", out)


class FailuresAreCountedNotFatal(unittest.TestCase):
    def test_one_failed_delete_reports_and_continues(self):
        listing = [
            entry("sha256:orphan-a", size=2_000_000_000),
            entry("sha256:orphan-b", size=1_000_000_000),
        ]
        fake = FakeAz(listing, fail_deletes={"sha256:orphan-a"})
        failures, _, err = run_sweep(fake, delete=True)
        self.assertEqual(failures, 1)
        self.assertEqual(fake.deleted, ["sha256:orphan-b"])
        self.assertIn("FAILED", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
