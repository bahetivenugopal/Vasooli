"""Preflight doctor - every check `run.bat` needs, with the fix for each.

    python scripts/preflight.py                  # check and repair
    python scripts/preflight.py --no-fix         # report only, change nothing
    python scripts/preflight.py --stage interpreter

Two rules govern this file:

**Standard library only.** It runs *before* dependencies are installed - that is
most of its job - so importing anything from `requirements.txt` would make the
doctor fail on exactly the machine that needs it most.

**ASCII output only.** The Windows console is cp1252 until something changes it,
and a preflight report that renders as mojibake is worse than no report. The
rupee signs and em-dashes belong to `unified_demo.py`, which reconfigures its own
stdout before printing any of them.

Every failure prints a `Problem:` line and a `Fix:` line. A check that cannot
say what to do about itself is a check that wastes the reader's time.

Exit codes, which `run.bat` branches on:

    0   ready
    1   blocked - something needs a human
    2   ready, but without the dashboard (node/npm absent)
"""

from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
API_DIR = REPO_ROOT / "apps" / "api"
WEB_DIR = REPO_ROOT / "apps" / "web"

REQUIRED_PYTHON = (3, 12)
API_PORT = 8000
WEB_PORT = 3000

EXIT_READY = 0
EXIT_BLOCKED = 1
EXIT_READY_NO_WEB = 2

# (import name, the distribution that provides it) - the runtime floor. If any
# one of these is missing the venv is not usable, so they are checked together
# and repaired with a single install rather than one at a time.
RUNTIME_IMPORTS = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn[standard]"),
    ("pydantic", "pydantic"),
    ("pydantic_settings", "pydantic-settings"),
    ("sqlalchemy", "SQLAlchemy"),
    ("razorpay", "razorpay"),
    ("google.genai", "google-genai"),
    ("dotenv", "python-dotenv"),
]

SAMPLE_DATASETS = [
    ("payments", "payments.jsonl"),
    ("mandates", "mandates.jsonl"),
    ("invoices", "invoices.jsonl"),
]


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


class Report:
    """Accumulates check results and decides the exit code."""

    def __init__(self, fix: bool) -> None:
        self.fix = fix
        self.blocked = False
        self.skip_web = False

    def ok(self, label: str, detail: str = "") -> None:
        suffix = "  " + detail if detail else ""
        print("  [ OK ] " + label + suffix)

    def fixed(self, label: str, detail: str = "") -> None:
        suffix = "  " + detail if detail else ""
        print("  [FIXED] " + label + suffix)

    def warn(self, label: str, problem: str, fix: str) -> None:
        print("  [WARN] " + label)
        self._detail(problem, fix)

    def fail(self, label: str, problem: str, fix: str) -> None:
        print("  [FAIL] " + label)
        self._detail(problem, fix)
        self.blocked = True

    @staticmethod
    def _detail(problem: str, fix: str) -> None:
        """Print the problem and its fix, wrapping continuation lines under the label."""
        print("         Problem: " + problem)
        head, _, rest = fix.partition("\n")
        print("         Fix:     " + head)
        for line in rest.splitlines():
            print("                  " + line)

    def exit_code(self) -> int:
        if self.blocked:
            return EXIT_BLOCKED
        if self.skip_web:
            return EXIT_READY_NO_WEB
        return EXIT_READY


def run(cmd: list[str], cwd: Path) -> bool:
    """Run a repair command with its output visible.

    Deliberately not captured: `pip install` and `npm install` take long enough
    that silence reads as a hang, and a judge who thinks the launcher froze
    kills it before it finishes.
    """
    print("         > " + " ".join(cmd))
    try:
        completed = subprocess.run(cmd, cwd=str(cwd))
    except OSError as exc:
        print("         ! could not run: " + str(exc))
        return False
    return completed.returncode == 0


# --------------------------------------------------------------------------
# Stage 1 - the interpreter itself
# --------------------------------------------------------------------------


def check_interpreter(report: Report) -> None:
    """Verify the interpreter that will *create the venv* is 3.12.

    Checked before the venv exists, because a venv built on the wrong
    interpreter fails later and much less legibly.
    """
    found = sys.version_info[:2]
    version = f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}"

    if found == REQUIRED_PYTHON:
        report.ok("Python " + version, "(" + sys.executable + ")")
        return

    report.fail(
        "Python " + version + " - 3.12 required",
        "This project is pinned to Python 3.12 (.python-version, ADR 0001). "
        "razorpay<=1.4.2 imports pkg_resources, which setuptools removed in 81.",
        "Install Python 3.12 from https://www.python.org/downloads/release/python-3120/ "
        "and re-run. If 3.12 is already installed alongside another version, "
        "the py launcher will find it: py -3.12 scripts\\preflight.py",
    )


# --------------------------------------------------------------------------
# Stage 2 - the environment
# --------------------------------------------------------------------------


def check_repo_layout(report: Report) -> None:
    """Confirm this really is the Vasooli repo and it is complete."""
    missing = [
        str(p.relative_to(REPO_ROOT))
        for p in (API_DIR / "app", REPO_ROOT / "scripts", REPO_ROOT / "data" / "generators")
        if not p.is_dir()
    ]
    if missing:
        report.fail(
            "Repository layout",
            "Missing: " + ", ".join(missing),
            "Run this from a complete clone of the repository. If the clone is "
            "partial or a download of a subfolder, re-clone: "
            "git clone https://github.com/bahetivenugopal/Vasooli.git",
        )
        return
    report.ok("Repository layout", "(" + str(REPO_ROOT) + ")")


def check_dependencies(report: Report) -> None:
    """Import-probe the runtime floor, and install the lot if any is absent."""
    import importlib.util

    missing = []
    for module, distribution in RUNTIME_IMPORTS:
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(distribution)
        except (ImportError, ValueError):
            # A missing parent package (google.genai) raises rather than
            # returning None. Same conclusion either way.
            missing.append(distribution)

    if not missing:
        report.ok("Python dependencies", "(" + str(len(RUNTIME_IMPORTS)) + " checked)")
        return

    requirements = API_DIR / "requirements.txt"
    fix_command = "pip install -r apps/api/requirements.txt"

    if not report.fix:
        report.fail(
            "Python dependencies - missing: " + ", ".join(missing),
            "The virtual environment exists but is not populated.",
            fix_command,
        )
        return

    print("  [ .. ] Installing Python dependencies - first run takes a minute or two")
    ok = run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            str(requirements),
            "--disable-pip-version-check",
            "--quiet",
        ],
        cwd=REPO_ROOT,
    )
    if ok:
        report.fixed("Python dependencies installed")
    else:
        report.fail(
            "Python dependencies could not be installed",
            "pip exited non-zero. The most common cause is no network access.",
            "Check your connection and run: " + fix_command,
        )


def check_env_file(report: Report) -> None:
    """`.env` must exist. Its committed defaults need no keys, so copying is safe."""
    env = REPO_ROOT / ".env"
    example = REPO_ROOT / ".env.example"

    if env.is_file():
        report.ok(".env present")
        return

    if not example.is_file():
        report.fail(
            ".env missing, and so is .env.example",
            "Neither file exists, so there is nothing to copy from.",
            "Re-clone the repository - .env.example is committed.",
        )
        return

    if not report.fix:
        report.fail(
            ".env missing",
            "The application reads configuration from the repo-root .env.",
            "copy .env.example .env   (the committed defaults work with no keys)",
        )
        return

    shutil.copyfile(str(example), str(env))
    report.fixed(".env created from .env.example", "(no keys needed - see docs/RUNNING.md)")


def read_env_value(key: str) -> str:
    """Read one key out of `.env` without python-dotenv.

    Deliberately hand-rolled: this runs in the same pass that installs
    dependencies, so it cannot depend on one of them being present.
    """
    env = REPO_ROOT / ".env"
    if not env.is_file():
        return ""
    try:
        lines = env.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return ""


def check_cors(report: Report) -> None:
    """The dashboard origin must be allowed, or every page shows its error state.

    Not auto-repaired. Rewriting a `.env` the user has edited is intrusive, and
    this only fails when they have edited it - a fresh copy of `.env.example`
    already carries the right value.
    """
    origins = read_env_value("CORS_ORIGINS")
    expected = f"http://127.0.0.1:{WEB_PORT}"

    if not origins:
        report.warn(
            "CORS_ORIGINS not set in .env",
            "The API will refuse the dashboard's requests and every page will "
            "render its error state against a perfectly healthy API.",
            f"Add to .env:  CORS_ORIGINS=http://localhost:{WEB_PORT},{expected}",
        )
        return

    if expected not in origins:
        report.warn(
            "CORS_ORIGINS does not include " + expected,
            "The launcher opens the dashboard on 127.0.0.1, which is not in the "
            "allowed origins. Every page will show its error state.",
            "Add " + expected + " to CORS_ORIGINS in .env",
        )
        return

    report.ok("CORS_ORIGINS allows " + expected)


def check_datasets(report: Report) -> None:
    """The seeded sample batches are committed; regenerate them if absent."""
    missing = [
        name
        for name, filename in SAMPLE_DATASETS
        if not (REPO_ROOT / "data" / "samples" / name / filename).is_file()
    ]

    if not missing:
        report.ok("Sample datasets", "(payments, mandates, invoices)")
        return

    fix_command = "python -m data.generators.cli all --seed 42 --out data/samples"

    if not report.fix:
        report.fail(
            "Sample datasets missing: " + ", ".join(missing),
            "The engines have nothing to run against.",
            fix_command,
        )
        return

    print("  [ .. ] Regenerating seeded sample datasets")
    ok = run(
        [
            sys.executable,
            "-m",
            "data.generators.cli",
            "all",
            "--seed",
            "42",
            "--out",
            "data/samples",
        ],
        cwd=REPO_ROOT,
    )
    if ok:
        report.fixed("Sample datasets regenerated", "(seed 42 - byte-identical to committed)")
    else:
        report.fail(
            "Sample datasets could not be regenerated",
            "The generator exited non-zero.",
            "Run it directly to see the error: " + fix_command,
        )


def check_node(report: Report) -> None:
    """Node and npm are needed for the dashboard only - everything else runs without them."""
    node = shutil.which("node")
    npm = shutil.which("npm")

    if node and npm:
        report.ok("Node and npm present", "(" + node + ")")
        return

    report.skip_web = True
    report.warn(
        "Node.js not found - dashboard will be skipped",
        "The batch run, the audit and the API do not need it. Only the "
        "Next.js control tower does.",
        "Install Node 18+ from https://nodejs.org/ and re-run to get the dashboard.",
    )


def check_node_modules(report: Report) -> None:
    """`npm install` on first run - slow enough that it needs announcing."""
    if report.skip_web:
        return

    if (WEB_DIR / "node_modules").is_dir():
        report.ok("Dashboard dependencies")
        return

    if not report.fix:
        report.fail(
            "Dashboard dependencies not installed",
            "apps/web/node_modules is absent.",
            "cd apps/web && npm install",
        )
        return

    print("  [ .. ] Installing dashboard dependencies - first run takes several minutes")
    npm = shutil.which("npm")
    if npm is None:  # pragma: no cover - guarded by check_node
        report.skip_web = True
        return
    ok = run([npm, "install"], cwd=WEB_DIR)
    if ok:
        report.fixed("Dashboard dependencies installed")
    else:
        report.skip_web = True
        report.warn(
            "npm install failed - dashboard will be skipped",
            "The rest of the product is unaffected.",
            "cd apps/web && npm install   (to see the full error)",
        )


def port_owner(port: int) -> str:
    """Return a human-readable owner of a listening port, or an empty string."""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    needle = f":{port}"
    for line in out.splitlines():
        parts = line.split()
        if (
            len(parts) >= 5
            and parts[0].startswith("TCP")
            and parts[1].endswith(needle)
            and parts[3].upper() == "LISTENING"
        ):
            return parts[4]
    return ""


def check_port(report: Report, port: int, what: str) -> None:
    """A port already in use is not auto-repairable - killing a stranger's process is not ours to do."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            pass
    except OSError:
        report.ok(f"Port {port} free", "(" + what + ")")
        return

    pid = port_owner(port)
    if pid:
        problem = f"Something is already listening on port {port} (PID {pid})."
        fix = (
            f"Stop it, or free the port:  taskkill /PID {pid} /F\n"
            "If that is another copy of Vasooli, close its window instead."
        )
    else:
        problem = f"Something is already listening on port {port}."
        fix = f"Find and stop it:  netstat -ano | findstr :{port}"

    report.fail(f"Port {port} in use" + " (" + what + ")", problem, fix)


# --------------------------------------------------------------------------
# Stopping the services
# --------------------------------------------------------------------------


def image_name(pid: str) -> str:
    """The executable behind a PID, or an empty string."""
    try:
        out = subprocess.run(
            ["tasklist", "/fi", "PID eq " + pid, "/nh", "/fo", "csv"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    if not out.startswith('"'):
        return ""
    return out.split('","')[0].lstrip('"')


def stop_services() -> int:
    """Stop whatever is serving Vasooli, identified by port rather than window title.

    Window titles looked like the obvious handle and are not one: a launcher
    started without a console spawns processes that have no title to match. The
    listening port is the thing that actually matters to the next run anyway,
    since a busy port is what blocks it.

    Only our own runtimes are killed. Anything else holding the port is named
    and left alone - it is not this script's to terminate.
    """
    ours = {"python.exe", "pythonw.exe", "node.exe"}
    stopped = 0

    for port, label in ((API_PORT, "API"), (WEB_PORT, "Dashboard")):
        pid = port_owner(port)
        if not pid:
            print("  [ -- ] " + label + " was not running")
            continue

        image = image_name(pid)
        if image.lower() not in ours:
            print(
                f"  [SKIP] Port {port} is held by {image or 'an unknown process'} "
                f"(PID {pid}), which this launcher did not start"
            )
            print("         Stop it yourself if you meant to:  taskkill /PID " + pid + " /F")
            continue

        try:
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            print("  [FAIL] Could not stop " + label + " (PID " + pid + ")")
            continue
        print("  [ OK ] " + label + " stopped  (" + image + ", PID " + pid + ")")
        stopped += 1

    if stopped == 0:
        print()
        print(f"  Nothing was listening on {API_PORT} or {WEB_PORT}.")
    return 0


# --------------------------------------------------------------------------
# Run planning - the repeat-run problem
# --------------------------------------------------------------------------


def run_plan(seed: int) -> int:
    """Print the extra `unified_demo.py` arguments this database needs.

    The unified run id is derived from the seed alone, deliberately: two people
    comparing "seed 42" must be comparing the same run. The consequence is that
    a second run of the same seed against a database that already holds it is
    refused - correct, and also exactly what a reviewer hits on their second
    launch. `--salt` exists for this, so detect the case and use it.

    The canonical id is *imported*, never recomputed here. Two places deriving
    one id is the same defect as two places computing one metric.

    stdout carries the arguments and nothing else, so the caller can capture it.
    Everything a human reads goes to stderr.
    """
    sys.path.insert(0, str(API_DIR))
    sys.path.insert(0, str(REPO_ROOT))
    try:
        import sqlite3

        from app.core.config import settings
        from app.services.unified_run import unified_run_id

        canonical = unified_run_id(seed)
        url = settings.resolved_database_url
        prefix = "sqlite:///"
        if not url.startswith(prefix):
            return 0  # Not SQLite - let the run speak for itself.

        db_path = Path(url[len(prefix) :])
        if not db_path.is_file():
            return 0  # Fresh database; the canonical id is free.

        connection = sqlite3.connect(str(db_path))
        try:
            rows = connection.execute(
                "SELECT 1 FROM batch_runs WHERE batch_id LIKE ? LIMIT 1", (canonical + "%",)
            ).fetchall()
        finally:
            connection.close()

        if not rows:
            return 0

        # A salt that is stable within a calendar second is plenty to separate
        # two launches, and stays readable in the audit trail.
        import time

        salt = time.strftime("%Y%m%dT%H%M%S")
        sys.stderr.write(
            "  [NOTE] Run " + canonical + " is already in this database.\n"
            "         Re-running seed 42 with salt " + salt + " so it gets a\n"
            "         distinct id. The metrics are identical - only the id moves.\n"
            "         To reproduce the canonical run id instead, delete\n"
            "         apps/api/vasooli.db and run again.\n"
        )
        sys.stdout.write("--salt " + salt)
        return 0
    except Exception as exc:
        sys.stderr.write("  [NOTE] Could not inspect prior runs (" + str(exc) + ").\n")
        return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/preflight.py",
        description="Check - and optionally repair - everything Vasooli needs to run.",
    )
    parser.add_argument(
        "--stage",
        choices=["interpreter", "environment"],
        default="environment",
        help="interpreter: version gate only, run before the venv exists. "
        "environment: the full check, run inside the venv.",
    )
    parser.add_argument(
        "--no-fix",
        action="store_true",
        help="report problems without repairing anything",
    )
    parser.add_argument(
        "--skip-ports",
        action="store_true",
        help="skip the port checks (nothing is going to be served)",
    )
    parser.add_argument(
        "--run-plan",
        action="store_true",
        help="print the extra unified_demo.py arguments this database needs, and exit",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="stop the API and dashboard, identified by the ports they hold",
    )
    parser.add_argument("--seed", type=int, default=42, help="seed --run-plan reasons about")
    args = parser.parse_args(argv)

    if args.run_plan:
        return run_plan(args.seed)

    if args.stop:
        return stop_services()

    report = Report(fix=not args.no_fix)

    if args.stage == "interpreter":
        print("Checking interpreter")
        check_interpreter(report)
        return report.exit_code()

    print("Checking environment")
    check_repo_layout(report)
    if report.blocked:
        # Every later check reads paths this one just proved absent.
        return report.exit_code()

    check_dependencies(report)
    check_env_file(report)
    check_cors(report)
    check_datasets(report)
    check_node(report)
    check_node_modules(report)

    if not args.skip_ports:
        check_port(report, API_PORT, "API")
        if not report.skip_web:
            check_port(report, WEB_PORT, "dashboard")

    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
