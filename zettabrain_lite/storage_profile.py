"""
Detect the storage protocol backing a docs folder and return ingestion parameters
tuned for that protocol's reliability characteristics.

Supported: local disk, NFS, SMB/CIFS, S3-FUSE mounts.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path


# ── Per-protocol defaults ─────────────────────────────────────────────────────
# batch_size = files processed before the hash cache is checkpointed to disk.
# Smaller batches on unreliable/slow protocols limit re-work after a crash.

_PROFILES: dict[str, dict] = {
    "local": {"batch_size": 20},
    "nfs":   {"batch_size": 10},
    "smb":   {"batch_size":  5},
    "s3":    {"batch_size":  3},
}

# If a "local" path shows higher-than-expected stat() latency, apply NFS-tier
# batch size (catches USB drives, Docker volume mounts, etc.).
_LOCAL_LATENCY_THRESHOLD_MS = 10.0


def detect_storage_type(docs_path: str) -> str:
    """Return the storage protocol type: 'local', 'nfs', 'smb', or 's3'."""
    path = Path(docs_path).resolve()

    # ── Linux: findmnt is authoritative ──────────────────────────────────────
    try:
        result = subprocess.run(
            ["findmnt", "--noheadings", "--output", "FSTYPE", "--target", str(path)],
            capture_output=True, text=True, timeout=5, check=False,
        )
        fstype = result.stdout.strip().lower()
        if fstype in ("fuse.s3fs", "fuse.goofys", "fuse.mountpoint-s3", "fuse.rclone"):
            return "s3"
        if "nfs" in fstype:
            return "nfs"
        if fstype in ("cifs", "smbfs"):
            return "smb"
        if fstype:  # recognised something local (ext4, xfs, apfs, …)
            return "local"
    except FileNotFoundError:
        pass  # findmnt not present (macOS)
    except Exception:
        pass

    # ── macOS / Linux fallback: parse `mount` output ─────────────────────────
    try:
        result = subprocess.run(
            ["mount"], capture_output=True, text=True, timeout=5, check=False,
        )
        best_match_len = 0
        best_fstype = ""
        for line in result.stdout.splitlines():
            # "device on /mount/point (fstype, options)" — or macOS variant
            m = re.match(r".+? on (.+?) \((\w+)", line)
            if not m:
                continue
            mpoint, fstype = m.group(1), m.group(2).lower()
            try:
                if str(path).startswith(mpoint) and len(mpoint) > best_match_len:
                    best_match_len = len(mpoint)
                    best_fstype = fstype
            except Exception:
                continue
        if best_fstype:
            if "nfs" in best_fstype:
                return "nfs"
            if best_fstype in ("smbfs", "cifs"):
                return "smb"
            if "s3fs" in best_fstype or "goofys" in best_fstype:
                return "s3"
    except Exception:
        pass

    return "local"


def measure_latency_ms(docs_path: str) -> float:
    """Measure average stat() latency to the docs folder in milliseconds."""
    path = Path(docs_path)
    if not path.exists():
        return 0.0
    try:
        t0 = time.perf_counter()
        for _ in range(5):
            path.stat()
        return (time.perf_counter() - t0) / 5 * 1000
    except Exception:
        return 0.0


def get_chunk_profile(docs_path: str) -> dict:
    """
    Detect storage type and latency for *docs_path* and return a profile dict::

        {
            "batch_size":   int,  # files to process per hash-cache checkpoint
            "storage_type": str,  # "local" | "nfs" | "smb" | "s3"
        }
    """
    stype = detect_storage_type(docs_path)
    profile = dict(_PROFILES[stype])

    # Reclassify unexpectedly slow "local" paths (USB drives, Docker volumes)
    if stype == "local":
        latency = measure_latency_ms(docs_path)
        if latency > _LOCAL_LATENCY_THRESHOLD_MS:
            profile = dict(_PROFILES["nfs"])

    profile["storage_type"] = stype
    return profile
