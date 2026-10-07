"""Guard that makes it hard to read real market-response data during synthetic calibration.

Inside `real_data_guard(repo_root)`:
  * a process-wide audit hook (sys.addaudithook) refuses `open`, `os.listdir` and `os.scandir`
    on any path that RESOLVES (os.path.realpath: symlinks followed) under <repo>/data/processed,
    <repo>/data/interim or <repo>/data/raw (themselves realpath-resolved), and on any file whose
    given or resolved name ends in .parquet / .feather / .arrow;
  * the same hook refuses the `import` event for columnar data-reader libraries (pyarrow,
    fastparquet, polars, duckdb and their submodules), so a reader imported AFTER the guard is
    installed cannot open files natively behind the audit hook;
  * pandas.read_parquet / pyarrow.parquet.read_table / pyarrow.parquet.ParquetFile are replaced
    by refusing stubs IF those modules were already imported before the guard;
  * dir_fd (Amendment 04): the `open` audit event does not carry `dir_fd`, so a relative path
    cannot be resolved safely. The guard FAILS CLOSED: any relative path (open / listdir /
    scandir) is refused, any integer descriptor that refers to a regular file or a directory is
    refused (pipes / sockets / character devices stay usable for multiprocessing), and every
    dir_fd-capable os function refuses a non-None dir_fd. Calibration code uses absolute paths only.
The calibration package itself depends on numpy only; the guard is a second line of defence.
Audit hooks cannot be removed, so the hook is installed once and toggled by a module flag.
"""
from __future__ import annotations

import contextlib
import functools
import os
import stat
import sys
from pathlib import Path
from typing import Iterator, List, Tuple

FORBIDDEN_SUBDIRS = ("data/processed", "data/interim", "data/raw")
FORBIDDEN_SUFFIXES = (".parquet", ".feather", ".arrow")
_GUARDED_EVENTS = ("open", "os.listdir", "os.scandir")
BLOCKED_IMPORT_ROOTS = ("pyarrow", "fastparquet", "polars", "duckdb")


class RealDataAccessError(PermissionError):
    """Raised when calibration code touches real market / research data."""


_state = {"active": False, "roots": (), "hook": False}


def _fd_forbidden(fd: int) -> bool:
    """An integer path re-opens an existing descriptor whose path cannot be checked: refuse
    regular files and directories (fail closed); allow pipes, sockets and devices."""
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        return True
    return stat.S_ISREG(mode) or stat.S_ISDIR(mode)


def _forbidden(path) -> bool:
    if path is None:
        return True  # implicit "." -- relative to an unknown base
    if isinstance(path, int):
        return _fd_forbidden(path)
    try:
        raw = os.fsdecode(path)
    except TypeError:
        return True
    if not os.path.isabs(raw):
        return True  # may be relative to a dir_fd the audit event does not report: fail closed
    given = os.path.abspath(raw)
    resolved = os.path.realpath(given)
    for p in (given, resolved):
        if p.lower().endswith(FORBIDDEN_SUFFIXES):
            return True
        if any(p == r or p.startswith(r + os.sep) for r in _state["roots"]):
            return True
    return False


def _blocked_module(name) -> bool:
    if not isinstance(name, str):
        return False
    root = name.split(".", 1)[0]
    return root in BLOCKED_IMPORT_ROOTS


def _hook(event: str, args) -> None:
    if not _state["active"] or not args:
        return
    if event == "import":
        if _blocked_module(args[0]):
            raise RealDataAccessError(f"synthetic calibration may not import data reader {args[0]!r}")
        return
    if event not in _GUARDED_EVENTS:
        return
    target = args[0]
    if _forbidden(target):
        raise RealDataAccessError(f"synthetic calibration may not access real data: {target!r} ({event})")


def _refuse(*_a, **_k):
    raise RealDataAccessError("synthetic calibration may not read parquet/arrow data")


DIR_FD_FUNCTIONS = ("open", "stat", "lstat", "access", "readlink", "mkdir", "rmdir", "unlink", "remove",
                    "rename", "replace", "chmod", "chown", "utime", "link", "symlink", "mknod", "mkfifo")


def _deny_dir_fd(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        if any(key.endswith("dir_fd") and val is not None for key, val in k.items()):
            raise RealDataAccessError(f"os.{fn.__name__} with dir_fd is refused while the real-data guard is active")
        return fn(*a, **k)
    wrapper.__wrapped_by_real_data_guard__ = True
    return wrapper


@contextlib.contextmanager
def real_data_guard(repo_root) -> Iterator[None]:
    root = Path(repo_root).resolve()
    roots = tuple(sorted({str(root / s) for s in FORBIDDEN_SUBDIRS}
                         | {os.path.realpath(str(root / s)) for s in FORBIDDEN_SUBDIRS}))
    if not _state["hook"]:
        sys.addaudithook(_hook)
        _state["hook"] = True
    patched: List[Tuple[object, str, object]] = []
    for mod_name, attrs in (("pandas", ("read_parquet", "read_feather")),
                            ("pyarrow", ("memory_map", "OSFile", "input_stream", "ipc")),
                            ("pyarrow.parquet", ("read_table", "ParquetFile", "ParquetDataset", "read_schema")),
                            ("pyarrow.feather", ("read_table", "read_feather")),
                            ("pyarrow.csv", ("read_csv", "open_csv")),
                            ("pyarrow.json", ("read_json",)),
                            ("pyarrow.dataset", ("dataset",))):
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        for a in attrs:
            if hasattr(mod, a):
                patched.append((mod, a, getattr(mod, a)))
                setattr(mod, a, _refuse)
    for name in DIR_FD_FUNCTIONS:
        fn = getattr(os, name, None)
        if fn is not None and not getattr(fn, "__wrapped_by_real_data_guard__", False):
            patched.append((os, name, fn))
            setattr(os, name, _deny_dir_fd(fn))
    prev = (_state["active"], _state["roots"])
    _state["active"], _state["roots"] = True, roots
    try:
        yield
    finally:
        _state["active"], _state["roots"] = prev
        for mod, a, orig in patched:
            setattr(mod, a, orig)


def guard_active() -> bool:
    return bool(_state["active"])
