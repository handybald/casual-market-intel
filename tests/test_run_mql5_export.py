"""Unit tests for scripts/run_mql5_export.py. No Wine/MetaTrader required."""
import datetime as dt
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_mql5_export as r  # noqa: E402

from src.data.fetch.mql5 import CSV_COLUMNS, window_content_digest  # noqa: E402

D = dt.date


def make_install(tmp_path, terminal=True, editor=True, wine=True, portable=True):
    app = tmp_path / "MetaTrader 5.app"
    wine_bin = app / "Contents/SharedSupport/wine/bin/wine"
    if wine:
        wine_bin.parent.mkdir(parents=True)
        wine_bin.write_text("#!/bin/sh\n")
        wine_bin.chmod(0o755)
    prefix = tmp_path / "prefix"
    inst = prefix / "drive_c/Program Files/MetaTrader 5"
    inst.mkdir(parents=True)
    if terminal:
        (inst / "terminal64.exe").write_text("")
    if editor:
        (inst / "MetaEditor64.exe").write_text("")
    if portable:
        (inst / "MQL5/Scripts").mkdir(parents=True)
        (inst / "MQL5/Files").mkdir()
    return app, prefix, inst


def install_obj(tmp_path, **kw):
    app, prefix, _ = make_install(tmp_path, **kw)
    return r.discover_install(app, prefix)


# ---- dates ----
def test_validate_range_ok():
    assert r.validate_range("2025-09-01", "2025-09-30") == (D(2025, 9, 1), D(2025, 9, 30))


@pytest.mark.parametrize("s,e", [("2025-13-01", "2025-09-30"), ("2025-09-01", "nope"),
                                 ("2025-09-30", "2025-09-01"), ("1999-01-01", "2025-01-01")])
def test_validate_range_rejects(s, e):
    with pytest.raises(r.RunnerError):
        r.validate_range(s, e)


def test_date_to_epoch():
    assert r.date_to_epoch(D(2025, 9, 1)) == 1756684800


# ---- discovery ----
def test_default_prefix_uses_home_not_username():
    assert r.default_wine_prefix(Path("/h")) == Path("/h/Library/Application Support/net.metaquotes.wine.metatrader5")


def test_discover_install_ok(tmp_path):
    inst = install_obj(tmp_path)
    assert inst.wine.name == "wine" and inst.terminal.name == "terminal64.exe"
    assert inst.metaeditor.name == "MetaEditor64.exe"  # case-insensitive match


def test_missing_wine(tmp_path):
    app, prefix, _ = make_install(tmp_path, wine=False)
    with pytest.raises(r.RunnerError, match="Wine binary not found"):
        r.discover_install(app, prefix)


def test_missing_terminal(tmp_path):
    app, prefix, _ = make_install(tmp_path, terminal=False)
    with pytest.raises(r.RunnerError, match="terminal64.exe not found"):
        r.discover_install(app, prefix)


def test_missing_metaeditor(tmp_path):
    app, prefix, _ = make_install(tmp_path, editor=False)
    with pytest.raises(r.RunnerError, match="metaeditor64.exe not found"):
        r.discover_install(app, prefix)


def test_missing_prefix(tmp_path):
    app, _, _ = make_install(tmp_path)
    with pytest.raises(r.RunnerError, match="Wine prefix not found"):
        r.discover_install(app, tmp_path / "nope")


def _hash_dir(inst_obj, user, name, origin=r"C:\Program Files\MetaTrader 5"):
    d = inst_obj.prefix / f"drive_c/users/{user}/AppData/Roaming/MetaQuotes/Terminal/{name}"
    (d / "MQL5").mkdir(parents=True)
    (d / "origin.txt").write_bytes(origin.encode("utf-16"))
    return d


def test_data_folder_portable(tmp_path):
    inst = install_obj(tmp_path)
    assert r.discover_data_folder(inst) == inst.install_dir


def test_data_folder_hash_dir_only(tmp_path):
    inst = install_obj(tmp_path, portable=False)
    d = _hash_dir(inst, "u", "ABC")
    assert r.discover_data_folder(inst) == d


def test_data_folder_ignores_other_installs_and_dirs_without_mql5(tmp_path):
    inst = install_obj(tmp_path, portable=False)
    _hash_dir(inst, "u", "OTHER", origin=r"C:\Other Broker MT5")
    (inst.prefix / "drive_c/users/u/AppData/Roaming/MetaQuotes/Terminal/Common").mkdir(parents=True)
    d = _hash_dir(inst, "u", "MINE")
    assert r.discover_data_folder(inst) == d


def test_ambiguous_data_folders_listed(tmp_path):
    inst = install_obj(tmp_path)  # portable dir + a hash dir
    other = _hash_dir(inst, "u", "ABC")
    with pytest.raises(r.RunnerError) as ei:
        r.discover_data_folder(inst)
    assert str(inst.install_dir) in str(ei.value) and str(other) in str(ei.value)
    assert r.discover_data_folder(inst, override=other) == other


def test_no_data_folder(tmp_path):
    inst = install_obj(tmp_path, portable=False)
    with pytest.raises(r.RunnerError, match="No MT5 data folder"):
        r.discover_data_folder(inst)


def test_windows_path(tmp_path):
    inst = install_obj(tmp_path)
    p = inst.install_dir / "MQL5/Scripts/EconomicCalendarExporter.mq5"
    assert r.windows_path(inst, p) == r"C:\Program Files\MetaTrader 5\MQL5\Scripts\EconomicCalendarExporter.mq5"
    assert r.windows_path(inst, Path("/Users/x/y.ini")) == r"Z:\Users\x\y.ini"


# ---- script install / compile ----
def fake_repo(tmp_path, body="a"):
    d = tmp_path / "repo/mql5_exporter"
    d.mkdir(parents=True)
    for n in r.SOURCE_FILES:
        (d / n).write_bytes(f"{body}-{n}\r\n\xff".encode("latin-1"))
    return tmp_path / "repo"


def test_install_sources_copies_exactly_and_is_idempotent(tmp_path):
    inst = install_obj(tmp_path)
    repo = fake_repo(tmp_path)
    assert sorted(r.install_sources(repo, inst.install_dir)) == sorted(r.SOURCE_FILES)
    for n in r.SOURCE_FILES:
        assert (inst.install_dir / "MQL5/Scripts" / n).read_bytes() == (repo / "mql5_exporter" / n).read_bytes()
    assert r.install_sources(repo, inst.install_dir) == []
    (repo / "mql5_exporter/CalendarExporterCore.mqh").write_bytes(b"new")
    assert r.install_sources(repo, inst.install_dir) == ["CalendarExporterCore.mqh"]


def test_install_sources_missing_repo_file(tmp_path):
    inst = install_obj(tmp_path)
    (tmp_path / "repo/mql5_exporter").mkdir(parents=True)
    with pytest.raises(r.RunnerError, match="Repository source missing"):
        r.install_sources(tmp_path / "repo", inst.install_dir)


def test_needs_compile(tmp_path):
    inst = install_obj(tmp_path)
    scripts = inst.install_dir / "MQL5/Scripts"
    r.install_sources(fake_repo(tmp_path), inst.install_dir)
    assert r.needs_compile(inst.install_dir, False, False)  # no ex5
    ex5 = scripts / "EconomicCalendarExporter.ex5"
    ex5.write_text("x")
    import os, time
    future = time.time() + 100
    os.utime(ex5, (future, future))
    assert not r.needs_compile(inst.install_dir, False, False)
    assert r.needs_compile(inst.install_dir, False, True)   # forced
    assert r.needs_compile(inst.install_dir, True, False)   # sources changed
    old = time.time() - 1000
    os.utime(ex5, (old, old))
    assert r.needs_compile(inst.install_dir, False, False)  # stale ex5


def test_compile_command_construction(tmp_path):
    inst = install_obj(tmp_path)
    cmd = r.build_compile_command(inst, inst.install_dir)
    assert cmd[0].endswith("SharedSupport/wine/bin/wine") and "wine64" not in cmd[0]
    assert cmd[1].endswith("MetaEditor64.exe")
    assert cmd[2] == r"/compile:MQL5\Scripts\EconomicCalendarExporter.mq5"  # relative: no spaces
    assert cmd[3] == "/log"


def test_compile_success_requires_fresh_ex5_and_ignores_fixme(tmp_path):
    inst = install_obj(tmp_path)
    ex5 = inst.install_dir / "MQL5/Scripts/EconomicCalendarExporter.ex5"

    def ok_run(cmd, **kw):
        ex5.write_text("compiled")
        return subprocess.CompletedProcess(cmd, 1, stdout=b"fixme:ole:something not implemented")

    r.compile_exporter(inst, inst.install_dir, run=ok_run)  # exit code 1 + fixme is fine

    ex5.unlink()
    with pytest.raises(r.RunnerError, match="Compilation failed"):
        r.compile_exporter(inst, inst.install_dir, run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))


def test_compile_rejects_stale_ex5(tmp_path):
    import os, time
    inst = install_obj(tmp_path)
    ex5 = inst.install_dir / "MQL5/Scripts/EconomicCalendarExporter.ex5"
    ex5.write_text("old")
    os.utime(ex5, (time.time() - 500, time.time() - 500))
    with pytest.raises(r.RunnerError, match="Compilation failed"):
        r.compile_exporter(inst, inst.install_dir, run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))


def test_compile_timeout(tmp_path):
    inst = install_obj(tmp_path)

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    with pytest.raises(r.RunnerError, match="timed out"):
        r.compile_exporter(inst, inst.install_dir, run=boom)


# ---- config generation ----
def test_set_file():
    s = r.build_set_file(D(2025, 9, 1), D(2025, 9, 30), "US", "USD", "us_macro_calendar.csv")
    assert s.splitlines() == ["StartDate=1756684800", "EndDate=1759190400", "CountryCode=US",
                              "CurrencyCode=USD", "OutputFile=us_macro_calendar.csv"]


def test_set_file_encoding_bom_and_roundtrip():
    text = r.build_set_file(D(2025, 9, 1), D(2025, 9, 30), "US", "USD", "us_macro_calendar.csv")
    raw = r.encode_set_file(text)
    assert raw[:2] == b"\xff\xfe"
    assert raw.decode("utf-16") == text  # BOM-aware decode round-trips
    params = dict(l.split("=", 1) for l in raw.decode("utf-16").splitlines() if l)
    assert params == {"StartDate": "1756684800", "EndDate": "1759190400", "CountryCode": "US",
                      "CurrencyCode": "USD", "OutputFile": "us_macro_calendar.csv"}


def test_startup_ini():
    ini = r.build_startup_ini("x.set", keep_open=False).splitlines()
    assert ini[0] == "[StartUp]"
    assert {"Script=EconomicCalendarExporter", "ScriptParameters=x.set", "Symbol=EURUSD",
            "Period=H1", "ShutdownTerminal=1"} <= set(ini)
    assert "ShutdownTerminal=0" in r.build_startup_ini("x.set", keep_open=True)


def test_launch_command_never_adds_portable(tmp_path):
    inst = install_obj(tmp_path)  # portable-layout install: data folder == install dir
    assert "/portable" not in r.build_launch_command(inst, inst.install_dir / "config/cli.ini")


def test_launch_command(tmp_path):
    inst = install_obj(tmp_path)
    cmd = r.build_launch_command(inst, inst.install_dir / "config/cli.ini")
    assert cmd[1].endswith("terminal64.exe")
    assert cmd[2] == r"/config:C:\Program Files\MetaTrader 5\config\cli.ini"


def test_terminal_process_matching_ignores_shells_mentioning_the_name():
    ps = "\n".join(["/bin/zsh", "/usr/bin/pkill",
                    "/Users/u/Library/Application Support/x/drive_c/Program Files/MetaTrader 5/terminal64.exe"])
    assert len(r.terminal_process_lines(ps)) == 1
    assert r.terminal_process_lines("/bin/zsh\n/usr/bin/vim notes-terminal64.exe.txt") == []


# ---- journal ----
def write_journal(data, name, lines, mode="wb"):
    logs = data / "MQL5/logs"
    logs.mkdir(parents=True, exist_ok=True)
    text = "".join(l + "\r\n" for l in lines)
    with open(logs / name, mode) as fh:
        fh.write(text.encode("utf-16-le") if mode == "ab" else b"\xff\xfe" + text.encode("utf-16-le"))


def jl(msg):
    return f"XX\t0\t12:00:00.000\tEconomicCalendarExporter (EURUSD,H1)\t[MQL5 Exporter] {msg}"


def test_journal_reads_only_new_bytes(tmp_path):
    write_journal(tmp_path, "d.log", [jl("DONE. old run")])
    off = r.journal_offsets(tmp_path)
    assert r.read_journal_since(tmp_path, off) == []
    write_journal(tmp_path, "d.log", [jl("DONE. new run")], mode="ab")
    write_journal(tmp_path, "e.log", [jl("other day")])
    lines = r.read_journal_since(tmp_path, off)
    assert len(lines) == 2 and "new run" in lines[0]


def test_journal_verdict():
    assert r.journal_verdict([jl("DONE. Total rows")]) == (True, None)
    assert r.journal_verdict([]) == (False, None)
    done, why = r.journal_verdict([jl("FAILED WINDOWS (re-run): x"), jl("DONE. Total")])
    assert done and "FAILED" in why
    assert r.journal_verdict([jl("ERROR: could not open x -- ABORTING")])[1]


# ---- terminal run ----
class FakeProc:
    def __init__(self, rc_seq, pid=99999999):
        self.rc_seq, self.pid, self.terminated = list(rc_seq), pid, False

    def poll(self):
        return self.rc_seq.pop(0) if len(self.rc_seq) > 1 else self.rc_seq[0]

    def terminate(self):
        self.terminated = True
        self.rc_seq = [-15]

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.rc_seq[0]


def test_run_terminal_timeout(tmp_path, monkeypatch):
    inst = install_obj(tmp_path)
    proc = FakeProc([None])
    monkeypatch.setattr(r, "_terminate", lambda p: setattr(p, "terminated", True))
    with pytest.raises(r.RunnerError, match="Timed out"):
        r.run_terminal(inst, inst.install_dir, ["x"], {}, timeout=0, keep_open=False, poll=0,
                       popen=lambda *a, **k: proc)
    assert proc.terminated


def test_run_terminal_nonzero_exit_without_done(tmp_path):
    inst = install_obj(tmp_path)
    with pytest.raises(r.RunnerError, match="exited with status 3"):
        r.run_terminal(inst, inst.install_dir, ["x"], {}, timeout=5, keep_open=False, poll=0,
                       popen=lambda *a, **k: FakeProc([3]))


def test_run_terminal_nonzero_exit_with_clean_done_is_accepted(tmp_path):
    inst = install_obj(tmp_path)
    write_journal(inst.install_dir, "d.log", [jl("DONE. Total rows this run: 1")])
    r.run_terminal(inst, inst.install_dir, ["x"], {}, timeout=5, keep_open=False, poll=0,
                   popen=lambda *a, **k: FakeProc([4]))


def test_run_terminal_nonzero_exit_with_failed_windows(tmp_path):
    inst = install_obj(tmp_path)
    write_journal(inst.install_dir, "d.log", [jl("FAILED WINDOWS (re-run): a"), jl("DONE. x")])
    with pytest.raises(r.RunnerError):
        r.run_terminal(inst, inst.install_dir, ["x"], {}, timeout=5, keep_open=False, poll=0,
                       popen=lambda *a, **k: FakeProc([4]))


def test_run_terminal_fails_fast_when_config_cannot_load(tmp_path, monkeypatch):
    inst = install_obj(tmp_path)
    write_journal(inst.install_dir / "x", "z.log", [])  # unrelated dir, ignored
    logs = inst.install_dir / "logs"
    logs.mkdir()
    text = 'XX\t2\t23:40:05.039\tTerminal\tcannot load config "C:\\a.ini"" at start\r\n'
    (logs / "d.log").write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    proc = FakeProc([None])
    monkeypatch.setattr(r, "_terminate", lambda p: setattr(p, "terminated", True))
    with pytest.raises(r.RunnerError, match="did not start the exporter: .*cannot load config"):
        r.run_terminal(inst, inst.install_dir, ["x"], {}, timeout=60, keep_open=False, poll=0,
                       popen=lambda *a, **k: proc, terminal_offsets={})
    assert proc.terminated


def test_terminal_startup_problem_ignores_old_lines(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    text = 'XX\t2\t1\tTerminal\tcannot load config "a" at start\r\n'
    (logs / "d.log").write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    assert r.terminal_startup_problem(tmp_path, {}) is not None
    assert r.terminal_startup_problem(tmp_path, r.terminal_log_offsets(tmp_path)) is None


def test_run_terminal_keep_open_returns_on_done(tmp_path):
    inst = install_obj(tmp_path)
    write_journal(inst.install_dir, "d.log", [jl("DONE. x")])
    proc = FakeProc([None])
    r.run_terminal(inst, inst.install_dir, ["x"], {}, timeout=5, keep_open=True, poll=0,
                   popen=lambda *a, **k: proc)
    assert not proc.terminated


# ---- validation ----
def row(vid, when, actual="1.0"):
    vals = {c: "" for c in CSV_COLUMNS}
    vals.update(value_id=vid, event_id="E" + vid, event_name="Evt", country_code="US", currency_code="USD",
                importance="HIGH", event_time=when, unit="UNKNOWN", multiplier="NONE", actual_value=actual,
                source_timezone="UNKNOWN")
    return vals


def write_export(files_dir, rows, header=None, sidecar=True, digest_override=None, out="us_macro_calendar.csv",
                 window=("2025.09.01 00:00:00", "2025.10.01 00:00:00")):
    files_dir.mkdir(parents=True, exist_ok=True)
    lines = [header if header is not None else ",".join(CSV_COLUMNS)]
    lines += [",".join(v[c] for c in CSV_COLUMNS) for v in rows]
    csv_p = files_dir / out
    csv_p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if sidecar:
        n, dg = window_content_digest(rows)
        side = files_dir / (out + ".windows.v5.csv")
        side.write_text(f"{window[0]},{window[1]},US,USD,5,OK,{n},1,2026.09.20 14:50:55,{digest_override or dg}\n")
    return csv_p


GOOD_ROWS = [row("1", "2025.09.02 12:30:00"), row("2", "2025.09.15 12:30:00")]
DONE = [jl("DONE. Total rows this run: 2. Skipped (already OK): 0")]
NOBEFORE = {"csv": r.Fingerprint(False), "sidecar": r.Fingerprint(False)}


def validate(files_dir, before=NOBEFORE, journal=DONE, start=D(2025, 9, 1), end=D(2025, 9, 30)):
    return r.validate_export(files_dir, "us_macro_calendar.csv", start, end, "US", "USD", before, journal)


def test_valid_export(tmp_path):
    f = tmp_path / "Files"
    write_export(f, GOOD_ROWS)
    res = validate(f)
    assert res.csv_rows == 2 and res.windows_verified == 1 and res.csv_changed and res.sidecar_changed


def test_missing_csv(tmp_path):
    f = tmp_path / "Files"
    write_export(f, GOOD_ROWS)
    (f / "us_macro_calendar.csv").unlink()
    with pytest.raises(r.RunnerError, match="CSV was not produced"):
        validate(f)


def test_missing_sidecar(tmp_path):
    f = tmp_path / "Files"
    write_export(f, GOOD_ROWS, sidecar=False)
    with pytest.raises(r.RunnerError, match="sidecar was not produced"):
        validate(f)


def test_empty_csv(tmp_path):
    f = tmp_path / "Files"
    write_export(f, GOOD_ROWS)
    (f / "us_macro_calendar.csv").write_text("")
    with pytest.raises(r.RunnerError, match="empty"):
        validate(f)


def test_wrong_header(tmp_path):
    f = tmp_path / "Files"
    write_export(f, GOOD_ROWS, header="event_id,event_name")
    with pytest.raises(r.RunnerError, match="header"):
        validate(f)


def test_truncated_csv(tmp_path):
    f = tmp_path / "Files"
    p = write_export(f, GOOD_ROWS)
    p.write_text(p.read_text().rstrip("\n") + "")  # drop trailing newline
    with pytest.raises(r.RunnerError, match="truncated"):
        validate(f)


def test_header_only_csv(tmp_path):
    f = tmp_path / "Files"
    write_export(f, [])
    res = validate(f)
    assert res.csv_rows == 0 and res.windows_verified == 1


def test_sidecar_missing_requested_window(tmp_path):
    f = tmp_path / "Files"
    write_export(f, GOOD_ROWS, window=("2025.08.01 00:00:00", "2025.09.01 00:00:00"))
    with pytest.raises(r.RunnerError, match="no entry for requested window"):
        validate(f)


def test_sidecar_digest_mismatch_is_rejected(tmp_path):
    f = tmp_path / "Files"
    write_export(f, GOOD_ROWS, digest_override="deadbeefdeadbeef")
    with pytest.raises(r.RunnerError, match="digest"):
        validate(f)


def test_stale_output_rejected_when_exporter_never_ran(tmp_path):
    """Old, perfectly valid files + no DONE in this run's journal must NOT pass."""
    f = tmp_path / "Files"
    write_export(f, GOOD_ROWS)
    before = {"csv": r.fingerprint(f / "us_macro_calendar.csv"),
              "sidecar": r.fingerprint(f / "us_macro_calendar.csv.windows.v5.csv")}
    with pytest.raises(r.RunnerError, match="never reported completion"):
        validate(f, before=before, journal=[])


def test_unchanged_files_accepted_only_with_done(tmp_path):
    f = tmp_path / "Files"
    write_export(f, GOOD_ROWS)
    before = {"csv": r.fingerprint(f / "us_macro_calendar.csv"),
              "sidecar": r.fingerprint(f / "us_macro_calendar.csv.windows.v5.csv")}
    res = validate(f, before=before)
    assert not res.csv_changed and not res.sidecar_changed and res.notes


def test_exporter_failure_line_rejected(tmp_path):
    f = tmp_path / "Files"
    write_export(f, GOOD_ROWS)
    with pytest.raises(r.RunnerError, match="reported a failure"):
        validate(f, journal=[jl("FAILED WINDOWS (re-run to retry): 2025.09"), *DONE])


# ---- copy + next command ----
def test_copy_outputs(tmp_path):
    f = tmp_path / "Files"
    write_export(f, GOOD_ROWS)
    res = validate(f)
    out = r.copy_outputs(res, tmp_path / "data/raw/mql5")
    assert [p.name for p in out] == ["us_macro_calendar.csv", "us_macro_calendar.csv.windows.v5.csv"]
    assert out[0].read_bytes() == (f / "us_macro_calendar.csv").read_bytes()
    assert not list((tmp_path / "data/raw/mql5").glob("*.tmp"))


def test_next_command():
    import shlex
    import fetch_historical_data as ingest
    argv = shlex.split(r.next_command(D(2025, 9, 1), D(2025, 9, 30), "custom calendar.csv", "GB", "GBP"))
    args = ingest.parse_args(argv[2:])
    assert argv[:2] == [sys.executable, str(r.REPO_ROOT / "scripts/fetch_historical_data.py")]
    assert args.mql5_input_csv == str(r.REPO_ROOT / "data/raw/mql5/custom calendar.csv")
    assert (args.macro_country, args.macro_currency) == ("GB", "GBP")
    assert (args.start, args.end, args.sources) == ("2025-09-01", "2025-09-30", "mql5")


# ---- CLI exit codes ----
def test_main_returns_nonzero_on_error(capsys):
    assert r.main(["--start", "2025-09-30", "--end", "2025-09-01"]) == 1
    assert "ERROR" in capsys.readouterr().err


def test_main_missing_wine(tmp_path, capsys):
    assert r.main(["--start", "2025-09-01", "--end", "2025-09-30", "--mt5-app", str(tmp_path / "none.app")]) == 1
    assert "Wine binary not found" in capsys.readouterr().err


@pytest.mark.parametrize("age", [0, -100])
def test_failed_compile_rejects_recent_or_future_binary(tmp_path, age):
    import os, time
    inst = install_obj(tmp_path)
    ex5 = inst.install_dir / "MQL5/Scripts/EconomicCalendarExporter.ex5"
    ex5.write_bytes(b"previous binary")
    stamp = time.time() - age
    os.utime(ex5, (stamp, stamp))
    with pytest.raises(r.RunnerError, match="Compilation failed"):
        r.compile_exporter(inst, inst.install_dir,
                           run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
    assert not ex5.exists()
    assert r.needs_compile(inst.install_dir, False, False)


def test_empty_compiler_artifact_rejected(tmp_path):
    inst = install_obj(tmp_path)
    def compile_empty(cmd, **kw):
        (inst.install_dir / "MQL5/Scripts/EconomicCalendarExporter.ex5").touch()
        return subprocess.CompletedProcess(cmd, 0)
    with pytest.raises(r.RunnerError, match="Compilation failed"):
        r.compile_exporter(inst, inst.install_dir, run=compile_empty)


def test_launch_never_portable_and_data_folder_validation(tmp_path):
    inst = install_obj(tmp_path)
    assert "/portable" not in r.build_launch_command(inst, inst.install_dir / "config/cli.ini")
    data = _hash_dir(inst, "u", "ABC")
    assert "/portable" not in r.build_launch_command(inst, data / "config/cli.ini")
    arbitrary = tmp_path / "arbitrary"
    (arbitrary / "MQL5").mkdir(parents=True)
    with pytest.raises(r.RunnerError, match="arbitrary data directory"):
        r.discover_data_folder(inst, arbitrary)


@pytest.mark.parametrize("problem", ["no_done", "bad_digest", "missing_window", "no_sidecar"])
def test_empty_export_still_requires_completion_evidence(tmp_path, problem):
    kwargs = {}
    if problem == "bad_digest": kwargs["digest_override"] = "deadbeefdeadbeef"
    if problem == "missing_window": kwargs["window"] = ("2025.08.01 00:00:00", "2025.09.01 00:00:00")
    if problem == "no_sidecar": kwargs["sidecar"] = False
    write_export(tmp_path, [], **kwargs)
    with pytest.raises(r.RunnerError):
        validate(tmp_path, journal=[] if problem == "no_done" else DONE)
