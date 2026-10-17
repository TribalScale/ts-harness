"""Data disk identity matching and format-only-when-unformatted safety."""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTER = REPO_ROOT / "scripts" / "cloud" / "adapter.sh"
EBS_PHASE = REPO_ROOT / "scripts" / "phases" / "01-ebs.sh"


class DataDiskSafetyTests(unittest.TestCase):
    def test_ebs_phase_uses_adapter_and_refuses_wrong_identity(self):
        text = EBS_PHASE.read_text()
        self.assertIn("cloud_resolve_data_device", text)
        self.assertIn("DATA_DISK_RESOURCE_ID", text)
        self.assertIn("refusing", text.lower())
        # Must not format when filesystem already present / unexpected content.
        self.assertIn("mkfs.ext4", text)
        self.assertIn("already has filesystem", text)

    def test_resolve_matches_volume_id_via_nvme_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Fake /dev tree under a chroot-like prefix via CLOUD_TEST_DEV_ROOT.
            # Adapter reads real /dev; instead exercise the compact-id helper path
            # through a small harness that mirrors the matching logic.
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export CLOUD_PROVIDER=aws
                export CLOUD_REGION=us-east-1
                export SECRETS_PREFIX=ts-harness
                export DATA_DIR="{root / 'data'}"
                mkdir -p "$DATA_DIR"
                source "{ADAPTER}"
                # Unit: compact vol id helper used for NVMe matching
                compact="$(_cloud_aws_vol_id_compact vol-0abc123def)"
                [[ "$compact" == "vol0abc123def" ]]
                # When resource_id is set and no matching device exists, fail closed.
                set +e
                cloud_resolve_data_device vol-0doesnotexist /dev/sdf >/tmp/out 2>/tmp/err
                ec=$?
                set -e
                [[ "$ec" -ne 0 ]]
                grep -qi 'no block device matched' /tmp/err
                echo OK
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=REPO_ROOT,
                env={**os.environ, "PATH": os.environ.get("PATH", "")},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("OK", result.stdout)

    def test_format_guard_script_aborts_on_formatted_nonmatching(self):
        """Extracted safety check: abort when device has unexpected content."""
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "fake-disk"
            device.write_bytes(b"NOT_EMPTY_CONTENT_XXXX")
            # Simulate the file -sL gate used by 01-ebs.sh
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                EBS_DEVICE="{device}"
                FILE_DESC=$(file -sL "$EBS_DEVICE" 2>/dev/null || true)
                if [[ "$FILE_DESC" != *"data"* && "$FILE_DESC" != *"empty"* && -n "$FILE_DESC" ]]; then
                  echo "ERROR: $EBS_DEVICE has unexpected content; refusing mkfs." >&2
                  exit 1
                fi
                echo WOULD_FORMAT
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing mkfs", result.stderr)


if __name__ == "__main__":
    unittest.main()
