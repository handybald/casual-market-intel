#!/usr/bin/env python3
"""Run the MQL5 economic-calendar exporter headlessly under MetaTrader 5 / Wine on macOS.

    python3 scripts/run_mql5_export.py --start 2025-09-01 --end 2025-09-30

Steps: validate dates -> locate the Wine-hosted MT5 install -> install the
exporter sources into MQL5/Scripts -> compile with MetaEditor if needed ->
launch terminal64.exe with a generated `/config:` startup .ini that runs the
script with a generated `.set` preset -> wait -> verify -> copy the CSV and
its window-log sidecar into data/raw/mql5/.

HOW PARAMETERS REACH THE SCRIPT: the startup .ini's `[StartUp]` section
names `Script=EconomicCalendarExporter` and `ScriptParameters=<name>.set`;
the preset lives in `MQL5/Presets/`. The exporter source is never edited.

COMPLETION IS NOT "the process exited". The exporter deliberately skips
windows already verified OK, so a re-run over an already-exported range
legitimately leaves both files byte-identical -- file mtimes alone cannot
prove the script ran. Success therefore requires ALL of:
  1. the MT5 journal (MQL5/logs/*.log), bytes appended since launch,
     contains the exporter's own `DONE.` line and no ABORTING/FAILED line;
  2. CSV + sidecar exist, are readable, non-empty, well-formed;
  3. the CSV header is exactly the v5 header;
  4. EVERY requested month window has an OK sidecar entry whose recorded
     content digest matches the CSV's current content (same logic as
     ingestion: src/data/fetch/mql5.py).
Pre-run file fingerprints are recorded and reported (changed / unchanged).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import os
import shutil
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.data.dates import iter_date_chunks  # noqa: E402
from src.data.fetch.mql5 import (  # noqa: E402
    CSV_COLUMNS,
    MQL5_EXPORT_SCHEMA_VERSION,
    _read_raw_csv_rows,
    _row_date_from_raw,
    _window_key_for_chunk,
    read_window_log,
    window_content_digest,
)

TAG = "[MQL5 runner]"
SCRIPT_NAME = "EconomicCalendarExporter"
SOURCE_FILES = ("EconomicCalendarExporter.mq5", "CalendarExporterCore.mqh")
EXPECTED_HEADER = ",".join(CSV_COLUMNS)
DEFAULT_MT5_APP = Path("/Applications/MetaTrader 5.app")
DEFAULT_TIMEOUT_SECONDS = 1800
TERMINAL_GRACE_SECONDS = 20  # after the exporter says DONE but the terminal is still alive
SET_FILE_NAME = "EconomicCalendarExporter_cli.set"


class RunnerError(Exception):
    """Any failure the CLI reports as an actionable message and exit code 1."""


def log(msg: str) -> None:
    print(f"{TAG} {msg}", flush=True)


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------
def parse_date(raw: str, label: str) -> dt.date:
    try:
        return dt.datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise RunnerError(f"{label} {raw!r} is not a valid YYYY-MM-DD date")


def validate_range(start: str, end: str) -> Tuple[dt.date, dt.date]:
    s, e = parse_date(start, "--start"), parse_date(end, "--end")
    if e < s:
        raise RunnerError(f"--end {e} is before --start {s}")
    if s.year < 2000:
        raise RunnerError(f"--start {s} is implausibly early for the MQL5 calendar")
    return s, e


def date_to_epoch(d: dt.date) -> int:
    """MT5 `datetime` (seconds since 1970-01-01, no timezone) for midnight of `d`."""
    return int((dt.datetime(d.year, d.month, d.day) - dt.datetime(1970, 1, 1)).total_seconds())


# --------------------------------------------------------------------------
# MetaTrader / Wine discovery
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Mt5Install:
    wine: Path
    prefix: Path
    install_dir: Path
    terminal: Path
    metaeditor: Path


def _find_ci(directory: Path, name: str) -> Optional[Path]:
    """Case-insensitive child lookup (the install ships `MetaEditor64.exe`)."""
    if not directory.is_dir():
        return None
    for child in directory.iterdir():
        if child.name.lower() == name.lower():
            return child
    return None


def default_wine_prefix(home: Optional[Path] = None) -> Path:
    return (home or Path.home()) / "Library" / "Application Support" / "net.metaquotes.wine.metatrader5"


def discover_install(mt5_app: Path, wine_prefix: Path) -> Mt5Install:
    wine = mt5_app / "Contents" / "SharedSupport" / "wine" / "bin" / "wine"
    if not wine.is_file():
        raise RunnerError(f"Wine binary not found at {wine}. Pass --mt5-app if MetaTrader 5.app lives elsewhere.")
    if not os.access(wine, os.X_OK):
        raise RunnerError(f"Wine binary {wine} is not executable")
    if not wine_prefix.is_dir():
        raise RunnerError(f"Wine prefix not found at {wine_prefix}. Pass --wine-prefix.")
    install_dir = wine_prefix / "drive_c" / "Program Files" / "MetaTrader 5"
    terminal = _find_ci(install_dir, "terminal64.exe")
    if terminal is None:
        raise RunnerError(f"terminal64.exe not found in {install_dir}. Is MetaTrader 5 installed in this prefix?")
    metaeditor = _find_ci(install_dir, "metaeditor64.exe")
    if metaeditor is None:
        raise RunnerError(f"metaeditor64.exe not found in {install_dir}.")
    return Mt5Install(wine, wine_prefix, install_dir, terminal, metaeditor)


def _origin_of(hash_dir: Path) -> Optional[str]:
    try:
        raw = (hash_dir / "origin.txt").read_bytes()
    except OSError:
        return None
    for enc in ("utf-16", "utf-8"):
        try:
            return raw.decode(enc).strip().strip("\x00")
        except UnicodeError:
            continue
    return None


def _same_windows_path(a: str, b: str) -> bool:
    return a.replace("/", "\\").rstrip("\\").lower() == b.replace("/", "\\").rstrip("\\").lower()


def find_data_folder_candidates(install: Mt5Install) -> List[Path]:
    """Every plausible MT5 data folder (a directory that contains `MQL5/`).

    - the install directory itself, when it holds an `MQL5/` folder (portable layout);
    - each `.../AppData/Roaming/MetaQuotes/Terminal/<hash>/` under the prefix that has an
      `MQL5/` folder AND whose `origin.txt` names this install.
    """
    found: List[Path] = []
    if (install.install_dir / "MQL5").is_dir():
        found.append(install.install_dir)
    users = install.prefix / "drive_c" / "users"
    if users.is_dir():
        for hash_dir in sorted(users.glob("*/AppData/Roaming/MetaQuotes/Terminal/*")):
            if not (hash_dir / "MQL5").is_dir():
                continue
            origin = _origin_of(hash_dir)
            if origin and _same_windows_path(origin, r"C:\Program Files\MetaTrader 5"):
                found.append(hash_dir)
    return found


def discover_data_folder(install: Mt5Install, override: Optional[Path] = None) -> Path:
    if override is not None:
        if not (override / "MQL5").is_dir():
            raise RunnerError(f"--data-folder {override} has no MQL5/ subdirectory")
        candidates = find_data_folder_candidates(install)
        if override.resolve() not in {p.resolve() for p in candidates}:
            raise RunnerError("--data-folder must be this installation's portable folder or an "
                              "origin-matched MT5 data folder; MT5 cannot launch with an arbitrary data directory")
        normal = [p for p in candidates if p.resolve() != install.install_dir.resolve()]
        if override.resolve() != install.install_dir.resolve() and len(normal) != 1:
            raise RunnerError("Cannot determine which user data folder MT5 will use; use the portable installation folder")
        return override.resolve()
    candidates = find_data_folder_candidates(install)
    if not candidates:
        raise RunnerError(
            f"No MT5 data folder (a directory containing MQL5/) found for {install.install_dir}. "
            "Start MetaTrader 5 once so it creates its data folder, or pass --data-folder."
        )
    if len(candidates) > 1:
        listing = "\n  ".join(str(c) for c in candidates)
        raise RunnerError(
            "Multiple candidate MT5 data folders; refusing to pick one arbitrarily. Candidates:\n  "
            f"{listing}\nPass --data-folder <one of these>."
        )
    return candidates[0]


def windows_path(install: Mt5Install, path: Path) -> str:
    """Wine path for a host path: C:\\... inside drive_c, else Z:\\<posix path>."""
    drive_c = install.prefix / "drive_c"
    try:
        rel = path.relative_to(drive_c)
        return "C:\\" + "\\".join(rel.parts)
    except ValueError:
        return "Z:" + str(path).replace("/", "\\")


# --------------------------------------------------------------------------
# Script installation + compilation
# --------------------------------------------------------------------------
def install_sources(repo_dir: Path, data_folder: Path) -> List[str]:
    """Copy exporter sources into MQL5/Scripts byte-for-byte; return names actually (re)written."""
    scripts = data_folder / "MQL5" / "Scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    changed = []
    for name in SOURCE_FILES:
        src = repo_dir / "mql5_exporter" / name
        if not src.is_file():
            raise RunnerError(f"Repository source missing: {src}")
        dst = scripts / name
        data = src.read_bytes()
        if not dst.is_file() or dst.read_bytes() != data:
            shutil.copyfile(src, dst)
            changed.append(name)
    return changed


def needs_compile(data_folder: Path, sources_changed: bool, force: bool) -> bool:
    scripts = data_folder / "MQL5" / "Scripts"
    ex5 = scripts / f"{SCRIPT_NAME}.ex5"
    if force or sources_changed or not ex5.is_file():
        return True
    newest_src = max((scripts / n).stat().st_mtime for n in SOURCE_FILES)
    return ex5.stat().st_mtime < newest_src


def wine_env(install: Mt5Install, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ if base is None else base)
    env["WINEPREFIX"] = str(install.prefix)
    env.setdefault("WINEDEBUG", "-all")
    return env


def build_compile_command(install: Mt5Install, data_folder: Path) -> List[str]:
    # Relative to cwd=data_folder (see compile_exporter): MetaEditor silently does nothing when
    # /compile: carries an absolute path containing a space ("Program Files"), verified on the
    # real install; the relative form works.
    return [str(install.wine), str(install.metaeditor), f"/compile:MQL5\\Scripts\\{SCRIPT_NAME}.mq5", "/log"]


def compile_exporter(install: Mt5Install, data_folder: Path, timeout: int = 300,
                     run: Callable[..., "subprocess.CompletedProcess"] = subprocess.run) -> None:
    ex5 = data_folder / "MQL5" / "Scripts" / f"{SCRIPT_NAME}.ex5"
    # Remove the previous artifact before invoking the compiler: timestamps cannot
    # distinguish a failed rapid retry from a successful rebuild.
    ex5.unlink(missing_ok=True)
    cmd = build_compile_command(install, data_folder)
    try:
        # MetaEditor's exit code is not a reliable success signal (it encodes the
        # result count); a fresh .ex5 is. Wine `fixme:` chatter is ignored.
        proc = run(cmd, env=wine_env(install), cwd=str(data_folder), timeout=timeout,
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except subprocess.TimeoutExpired:
        ex5.unlink(missing_ok=True)
        raise RunnerError(f"MetaEditor compile timed out after {timeout}s")
    if not ex5.is_file() or ex5.stat().st_size == 0:
        ex5.unlink(missing_ok=True)
        compile_log = ex5.with_suffix(".log")
        hint = f" See {compile_log}." if compile_log.exists() else ""
        raise RunnerError(f"Compilation failed: {ex5.name} was not (re)generated.{hint}")


# --------------------------------------------------------------------------
# Startup config
# --------------------------------------------------------------------------
def build_set_file(start: dt.date, end: dt.date, country: str, currency: str, output_file: str) -> str:
    return "\r\n".join([
        f"StartDate={date_to_epoch(start)}",
        f"EndDate={date_to_epoch(end)}",
        f"CountryCode={country}",
        f"CurrencyCode={currency}",
        f"OutputFile={output_file}",
        "",
    ])


def encode_set_file(text: str) -> bytes:
    """MetaTrader presets are Windows Unicode: UTF-16 LE with a BOM."""
    return b"\xff\xfe" + text.encode("utf-16-le")


def build_startup_ini(set_name: str, keep_open: bool) -> str:
    return "\r\n".join([
        "[StartUp]",
        f"Script={SCRIPT_NAME}",
        f"ScriptParameters={set_name}",
        "Symbol=EURUSD",
        "Period=H1",
        f"ShutdownTerminal={0 if keep_open else 1}",
        "",
    ])


def build_launch_command(install: Mt5Install, ini_path: Path) -> List[str]:
    # Deliberately NO `/portable`: verified on the real install that appending it makes MT5
    # log `cannot load config "...ini""` at start, ignore the startup script and idle forever.
    return [str(install.wine), str(install.terminal), f"/config:{windows_path(install, ini_path)}"]


# --------------------------------------------------------------------------
# File fingerprints + journal
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Fingerprint:
    exists: bool
    size: int = 0
    mtime_ns: int = 0
    sha256: str = ""


def fingerprint(path: Path) -> Fingerprint:
    if not path.is_file():
        return Fingerprint(False)
    st = path.stat()
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return Fingerprint(True, st.st_size, st.st_mtime_ns, h.hexdigest())


def journal_offsets(data_folder: Path) -> Dict[str, int]:
    logs = data_folder / "MQL5" / "logs"
    return {p.name: p.stat().st_size for p in logs.glob("*.log")} if logs.is_dir() else {}


def read_journal_since(data_folder: Path, offsets: Dict[str, int]) -> List[str]:
    """Exporter lines (`[MQL5 Exporter] ...`) appended to the MT5 journal since `offsets`."""
    logs = data_folder / "MQL5" / "logs"
    lines: List[str] = []
    if not logs.is_dir():
        return lines
    for p in sorted(logs.glob("*.log")):
        start = offsets.get(p.name, 0)
        if start % 2:  # UTF-16 code-unit alignment
            start -= 1
        try:
            with open(p, "rb") as fh:
                fh.seek(start)
                chunk = fh.read()
        except OSError:
            continue
        if not chunk:
            continue
        if start == 0 and chunk[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = chunk.decode("utf-16", errors="replace")
        else:
            text = chunk[: len(chunk) - (len(chunk) % 2)].decode("utf-16-le", errors="replace")
        lines.extend(l for l in text.splitlines() if "[MQL5 Exporter]" in l)
    return lines


def terminal_log_offsets(data_folder: Path) -> Dict[str, int]:
    logs = data_folder / "logs"
    return {p.name: p.stat().st_size for p in logs.glob("*.log")} if logs.is_dir() else {}


def terminal_startup_problem(data_folder: Path, offsets: Dict[str, int]) -> Optional[str]:
    """A terminal-journal (`logs/*.log`) line, new since launch, saying the startup config or
    script could not be loaded. Without this the runner would wait for the full timeout on a
    terminal that came up idle."""
    logs = data_folder / "logs"
    if not logs.is_dir():
        return None
    for p in sorted(logs.glob("*.log")):
        start = offsets.get(p.name, 0)
        start -= start % 2
        try:
            with open(p, "rb") as fh:
                fh.seek(start)
                chunk = fh.read()
        except OSError:
            continue
        text = chunk[: len(chunk) - (len(chunk) % 2)].decode("utf-16-le", errors="replace")
        for line in text.splitlines():
            low = line.lower()
            if "cannot load config" in low or "failed to load script" in low or "cannot load script" in low:
                return line.split("\t")[-1].strip()
    return None


def journal_verdict(lines: Sequence[str]) -> Tuple[bool, Optional[str]]:
    """(done, failure_reason). `done` means the exporter printed its terminal DONE line."""
    done = any("[MQL5 Exporter] DONE" in l for l in lines)
    for l in lines:
        if "ABORTING" in l or "FAILED WINDOWS" in l or "] ERROR:" in l:
            return done, l.split("\t")[-1].strip()
    return done, None


# --------------------------------------------------------------------------
# Terminal launch / wait
# --------------------------------------------------------------------------
def terminal_process_lines(ps_output: str) -> List[str]:
    """`ps -axo comm=` lines naming a terminal64.exe executable.

    Matches the process EXECUTABLE (Wine reports the full exe path as `comm`), never a
    command line that merely mentions the name -- `pgrep -f` also matched unrelated shells
    whose text contained "terminal64.exe" and made the runner refuse to start.
    """
    return [l for l in ps_output.splitlines() if l.strip().lower().endswith("terminal64.exe")]


def terminal_running() -> bool:
    try:
        out = subprocess.run(["ps", "-axo", "comm="], stdout=subprocess.PIPE, text=True).stdout
    except OSError:
        return False
    return bool(terminal_process_lines(out))


def _terminate(proc: "subprocess.Popen") -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        proc.wait()


def run_terminal(install: Mt5Install, data_folder: Path, cmd: List[str], offsets: Dict[str, int],
                 timeout: int, keep_open: bool, poll: float = 1.0,
                 popen: Callable[..., "subprocess.Popen"] = subprocess.Popen,
                 terminal_offsets: Optional[Dict[str, int]] = None) -> None:
    """Launch the terminal and block until the exporter finishes; raise RunnerError otherwise."""
    proc = popen(cmd, env=wine_env(install), cwd=str(install.install_dir), start_new_session=True,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout
    done_at: Optional[float] = None
    while True:
        rc = proc.poll()
        if rc is not None:
            if rc != 0:
                # MT5's ShutdownTerminal reports the script's leftover _LastError as the exit
                # code (observed: 4 after a fully successful run), so a nonzero status is only
                # fatal when the journal does not show a clean exporter DONE.
                done, failure = journal_verdict(read_journal_since(data_folder, offsets))
                if not done or failure:
                    raise RunnerError(f"terminal64.exe exited with status {rc} and the exporter did not "
                                      "report a clean DONE in the MT5 journal")
                log(f"terminal64.exe exit status {rc} ignored: the exporter reported a clean DONE "
                    "(MT5 returns the script's last error code on shutdown)")
            return
        if terminal_offsets is not None:
            problem = terminal_startup_problem(data_folder, terminal_offsets)
            if problem:
                _terminate(proc)
                raise RunnerError(f"MetaTrader did not start the exporter: {problem}")
        done, _ = journal_verdict(read_journal_since(data_folder, offsets))
        if done and done_at is None:
            done_at = time.monotonic()
        if done and keep_open:
            log("Export finished; leaving MetaTrader 5 running (--keep-terminal-open)")
            return
        if done_at is not None and time.monotonic() - done_at > TERMINAL_GRACE_SECONDS:
            log(f"Exporter finished but the terminal is still running after {TERMINAL_GRACE_SECONDS}s; terminating it")
            _terminate(proc)
            return
        if time.monotonic() > deadline:
            _terminate(proc)
            raise RunnerError(f"Timed out after {timeout}s waiting for the export (terminal killed). "
                              "Raise --timeout-seconds for large ranges.")
        time.sleep(poll)


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------
@dataclass
class ExportResult:
    csv_path: Path
    sidecar_path: Path
    csv_rows: int
    windows_verified: int
    csv_changed: bool
    sidecar_changed: bool
    notes: List[str] = field(default_factory=list)


def validate_export(files_dir: Path, output_file: str, start: dt.date, end: dt.date,
                    country: str, currency: str, before: Dict[str, Fingerprint],
                    journal_lines: Sequence[str]) -> ExportResult:
    csv_path = files_dir / output_file
    sidecar = csv_path.with_name(csv_path.name + f".windows.v{MQL5_EXPORT_SCHEMA_VERSION}.csv")

    done, failure = journal_verdict(journal_lines)
    if failure:
        raise RunnerError(f"The exporter reported a failure: {failure}")

    for label, p in (("CSV", csv_path), ("window-log sidecar", sidecar)):
        if not p.is_file():
            raise RunnerError(f"{label} was not produced: {p}")
        try:
            with open(p, "rb") as fh:
                fh.read(1)
        except OSError as exc:
            raise RunnerError(f"{label} is not readable ({exc}): {p}")
        if p.stat().st_size == 0:
            raise RunnerError(f"{label} is empty: {p}")

    after = {"csv": fingerprint(csv_path), "sidecar": fingerprint(sidecar)}
    csv_changed = after["csv"] != before.get("csv", Fingerprint(False))
    sidecar_changed = after["sidecar"] != before.get("sidecar", Fingerprint(False))

    if not done:
        # No DONE line: the script demonstrably did not finish (or the journal is unreadable).
        # Never accept pre-existing files on that basis.
        raise RunnerError(
            "The exporter never reported completion in the MT5 journal (no '[MQL5 Exporter] DONE' line); "
            "the script may not have started (bad ScriptParameters/startup config, terminal not logged in) "
            f"or was interrupted. Existing files are NOT accepted as proof. Check {files_dir.parent / 'logs'}."
        )

    try:
        raw = csv_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RunnerError(f"CSV is not valid UTF-8 ({exc})")
    header = raw.split("\n", 1)[0].rstrip("\r")
    if header != EXPECTED_HEADER:
        raise RunnerError(f"CSV header is not the v{MQL5_EXPORT_SCHEMA_VERSION} schema.\n  expected: "
                          f"{EXPECTED_HEADER}\n  actual:   {header}")
    if not raw.endswith("\n"):
        raise RunnerError("CSV does not end with a newline; output looks truncated (interrupted write)")

    ncols = len(CSV_COLUMNS)
    data_rows = 0
    for row in csv.reader(io.StringIO(raw, newline="")):
        data_rows += 1
        if len(row) != ncols and data_rows > 1:
            raise RunnerError(f"CSV row {data_rows} has {len(row)} fields, expected {ncols}; output is corrupt/partial")
    data_rows -= 1  # header

    entries = read_window_log(csv_path, country, currency)
    if not entries:
        raise RunnerError(f"Sidecar {sidecar.name} has no well-formed v{MQL5_EXPORT_SCHEMA_VERSION} entries "
                          f"for {country}/{currency}")
    raw_rows = _read_raw_csv_rows(csv_path)
    verified = 0
    for chunk in iter_date_chunks(start, end, "month"):
        key = _window_key_for_chunk(chunk.start, chunk.end)
        entry = entries.get(key)
        where = f"{chunk.label} (window {key[0]} -> {key[1]})"
        if entry is None:
            raise RunnerError(f"Sidecar has no entry for requested window {where}")
        if entry.status != "OK":
            raise RunnerError(f"Sidecar's latest status for {where} is {entry.status}, not OK")
        rows_in = [r for r in raw_rows
                   if (d := _row_date_from_raw(r)) is not None and chunk.start <= d <= chunk.end]
        count, digest = window_content_digest(rows_in)
        if (count, digest) != (entry.canonical_row_count, entry.content_digest):
            raise RunnerError(f"CSV content for {where} does not match the sidecar's recorded digest "
                              f"(CSV: {count} rows/{digest}; sidecar: {entry.canonical_row_count} rows/"
                              f"{entry.content_digest}); output is stale, partial, or altered")
        verified += 1

    notes = []
    if not csv_changed and not sidecar_changed:
        notes.append("Output files were unchanged: the exporter reported DONE and every requested window "
                     "was already verified OK, so nothing needed fetching.")
    return ExportResult(csv_path, sidecar, data_rows, verified, csv_changed, sidecar_changed, notes)


# --------------------------------------------------------------------------
# Copy + next command
# --------------------------------------------------------------------------
def copy_outputs(result: ExportResult, dest_dir: Path) -> List[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for src in (result.csv_path, result.sidecar_path):
        dst = dest_dir / src.name
        tmp = dst.with_name(dst.name + ".tmp")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        out.append(dst)
    return out


def next_command(start: dt.date, end: dt.date, output_file: str = "us_macro_calendar.csv",
                 country: str = "US", currency: str = "USD") -> str:
    args = [sys.executable, str(REPO_ROOT / "scripts/fetch_historical_data.py"),
            "--sources", "mql5", "--start", start.isoformat(), "--end", end.isoformat(),
            "--mql5-input-csv", str(REPO_ROOT / "data/raw/mql5" / output_file),
            "--macro-country", country, "--macro-currency", currency]
    return shlex.join(args)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    start, end = validate_range(args.start, args.end)
    if end > dt.date.today():
        log(f"Warning: --end {end} is in the future; unreleased events will have no actual values")

    install = discover_install(Path(args.mt5_app).expanduser(), Path(args.wine_prefix).expanduser())
    data_folder = discover_data_folder(install, Path(args.data_folder).expanduser() if args.data_folder else None)
    files_dir = data_folder / "MQL5" / "Files"
    log(f"Wine: {install.wine}")
    log(f"Wine prefix: {install.prefix}")
    log(f"MetaEditor: {install.metaeditor}")
    log(f"Terminal: {install.terminal}")
    log(f"MT5 data folder: {data_folder}")

    if terminal_running():
        raise RunnerError("terminal64.exe is already running; quit MetaTrader 5 first (a second launch would "
                          "hand the config to the running instance and its outcome cannot be tracked).")

    changed = install_sources(REPO_ROOT, data_folder)
    log("Installed/updated: " + ", ".join(changed) if changed else "Exporter sources already up to date in MQL5/Scripts")

    if needs_compile(data_folder, bool(changed), args.force_compile):
        log("Compiling exporter...")
        compile_exporter(install, data_folder)
        log("Compiled " + f"{SCRIPT_NAME}.ex5")
    else:
        log("Exporter .ex5 is up to date; skipping compile")

    presets = data_folder / "MQL5" / "Presets"
    presets.mkdir(exist_ok=True)
    set_path = presets / SET_FILE_NAME
    ini_path = data_folder / "config" / "cli_export_startup.ini"
    ini_path.parent.mkdir(exist_ok=True)
    set_path.write_bytes(encode_set_file(build_set_file(start, end, args.country, args.currency, args.output_file)))
    ini_path.write_bytes(build_startup_ini(SET_FILE_NAME, args.keep_terminal_open).encode("ascii"))

    csv_path = files_dir / args.output_file
    sidecar = csv_path.with_name(csv_path.name + f".windows.v{MQL5_EXPORT_SCHEMA_VERSION}.csv")
    before = {"csv": fingerprint(csv_path), "sidecar": fingerprint(sidecar)}
    offsets = journal_offsets(data_folder)
    terminal_offsets = terminal_log_offsets(data_folder)

    log(f"Preset: {set_path} (UTF-16 LE + BOM)")
    log(f"Requested inputs: StartDate={start} (epoch {date_to_epoch(start)}), EndDate={end} "
        f"(epoch {date_to_epoch(end)}), CountryCode={args.country}, CurrencyCode={args.currency}, "
        f"OutputFile={args.output_file}")
    log(f"Launching export {start} -> {end}")
    try:
        run_terminal(install, data_folder, build_launch_command(install, ini_path), offsets,
                     args.timeout_seconds, args.keep_terminal_open, terminal_offsets=terminal_offsets)
    finally:
        ini_path.unlink(missing_ok=True)
    log("Waiting for output...")
    result = validate_export(files_dir, args.output_file, start, end, args.country, args.currency, before,
                             read_journal_since(data_folder, offsets))
    log("Export complete")
    log(f"CSV rows: {result.csv_rows}; windows verified against sidecar: {result.windows_verified}")
    for note in result.notes:
        log(note)

    dest = REPO_ROOT / "data" / "raw" / "mql5"
    copied = copy_outputs(result, dest)
    log("Copied to data/raw/mql5/: " + ", ".join(p.name for p in copied))
    print("\nNext ingestion command:\n" + next_command(start, end, args.output_file, args.country, args.currency))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--start", required=True, help="inclusive start date YYYY-MM-DD")
    p.add_argument("--end", required=True, help="inclusive end date YYYY-MM-DD")
    p.add_argument("--country", default="US")
    p.add_argument("--currency", default="USD")
    p.add_argument("--output-file", default="us_macro_calendar.csv")
    p.add_argument("--wine-prefix", default=str(default_wine_prefix()))
    p.add_argument("--mt5-app", default=str(DEFAULT_MT5_APP))
    p.add_argument("--data-folder", default=None, help="override MT5 data folder discovery")
    p.add_argument("--force-compile", action="store_true")
    p.add_argument("--keep-terminal-open", action="store_true", help="do not shut MT5 down after the export")
    p.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except (RunnerError, OSError, UnicodeError, csv.Error) as exc:
        print(f"{TAG} ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
