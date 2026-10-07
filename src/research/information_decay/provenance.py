"""Scientific implementation fingerprint (Amendment 04 §6).

Pattern-based, so a new estimator / qualification / revalidation script cannot be silently left
out of the fingerprint:

  src/research/information_decay/**/*.py
  scripts/*information_decay*.py
  config/information_decay*.yaml

Paths are repository-relative and sorted; each file contributes its SHA-256; the aggregate is the
SHA-256 of the canonical JSON list of (path, sha256). Excluded: __pycache__, caches, generated
reports (data/), prose (docs/, README) and machine-specific paths. Protocol identity
(methodology / amendment commits) is recorded separately by the runners.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Sequence

SCIENTIFIC_PATTERNS = ("src/research/information_decay/**/*.py",
                       "scripts/*information_decay*.py",
                       "config/information_decay*.yaml")


def scientific_files(repo_root, patterns: Sequence[str] = SCIENTIFIC_PATTERNS) -> List[str]:
    root = Path(repo_root)
    found = set()
    for pat in patterns:
        for p in root.glob(pat):
            if p.is_file() and "__pycache__" not in p.parts:
                found.add(p.relative_to(root).as_posix())
    return sorted(found)


def scientific_inventory(repo_root, patterns: Sequence[str] = SCIENTIFIC_PATTERNS) -> Dict[str, Any]:
    root = Path(repo_root)
    files = [{"path": rel, "sha256": hashlib.sha256((root / rel).read_bytes()).hexdigest()}
             for rel in scientific_files(root, patterns)]
    aggregate = hashlib.sha256(json.dumps([[f["path"], f["sha256"]] for f in files],
                                          separators=(",", ":")).encode()).hexdigest()
    return {"patterns": list(patterns), "files": files, "file_count": len(files), "aggregate_sha256": aggregate}


def implementation_fingerprint(repo_root) -> Dict[str, Any]:
    """Scientific inventory plus git identity of the working tree that produced an artifact."""
    inv = scientific_inventory(repo_root)

    def git(*a):
        return subprocess.run(["git", *a], cwd=str(repo_root), capture_output=True, text=True).stdout.strip()
    status = git("status", "--porcelain", "--", *[f["path"] for f in inv["files"]]) if inv["files"] else ""
    return dict(inv, head_commit=git("rev-parse", "HEAD"),
                uncommitted_scientific_files=sorted(l[3:] for l in status.splitlines()))
