"""Trusted Docker verifier; separate reference collection and candidate execution.

This driver is image-owned. Same-container reports are not hostile-code proof.
"""
import argparse, base64, hashlib, json, os, pathlib, subprocess, sys, tempfile, time, tomllib
CHILD = b"HPH_PYTEST_OBSERVATION_V2 "
DRIVER = b"HPH_VERIFIER_RESULT_V2 "
def emit(prefix, value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    sys.stdout.buffer.write(b"\n" + prefix + base64.b64encode(data) + b"\n")
    sys.stdout.buffer.flush()
def observation(raw):
    lines = [line[len(CHILD):] for line in raw.splitlines() if line.startswith(CHILD)]
    if len(lines) != 1:
        return {"capture_error": "PYTEST_OBSERVATION_MISSING_OR_AMBIGUOUS"}
    try:
        return json.loads(base64.b64decode(lines[0], validate=True))
    except Exception as error:
        return {"capture_error": type(error).__name__}
def inner(args):
    import pytest
    class Observer:
        def __init__(self):
            self.data = {"schema": "pytest-observation-v2", "collection_complete": False,
                         "session_finished": False, "collected_node_ids": [],
                         "collection_events": [], "reports": []}
        def pytest_collection_finish(self, session):
            self.data["collection_complete"] = True
            self.data["collected_node_ids"] = [item.nodeid for item in session.items]
            for item in session.items:
                item.user_properties.append(("hph_node_id", item.nodeid))
        def pytest_collectreport(self, report):
            if report.outcome != "passed":
                self.data["collection_events"].append({"node_id": report.nodeid,
                    "outcome": report.outcome, "detail": str(report.longrepr)})
        def pytest_runtest_logreport(self, report):
            item = {"node_id": report.nodeid, "when": report.when, "outcome": report.outcome}
            if report.outcome != "passed":
                item["detail"] = str(report.longrepr)
            if hasattr(report, "wasxfail"):
                item["wasxfail"] = str(report.wasxfail)
            self.data["reports"].append(item)
        def pytest_sessionfinish(self, session, exitstatus):
            self.data["session_finished"] = True
            self.data["exit_code"] = int(exitstatus)
    observer = Observer()
    # This selection belongs to the independently enrolled baseline, outside
    # the candidate's writable source. Both children collect the same profile.
    # Archives still contain all historical tests; normal self-updates validate
    # the shipped runtime, not the retired per-operation ceremonial engine.
    config_path = pathlib.Path(args.config)
    config = tomllib.loads(config_path.read_text(encoding='utf-8'))
    profile = config.get('tool', {}).get('oif', {}).get('verification', {}).get('paths')
    targets = [args.tests]
    if profile is not None:
        if not isinstance(profile, list) or not profile or len(set(profile)) != len(profile):
            raise ValueError('Invalid fixed verification profile')
        targets = []
        root = pathlib.Path(args.tests).resolve()
        for name in profile:
            path = (config_path.parent / name).resolve()
            path.relative_to(root)
            if not path.is_file() or not path.name.startswith('test_') or path.suffix != '.py':
                raise ValueError('Fixed verification target missing')
            targets.append(str(path))
    command = [*targets, "-c", args.config, "--confcutdir=" + args.tests,
               "--import-mode=importlib", "-p", "no:cacheprovider", "-q"]
    # A private tmpfs file works on Docker and on benign native driver fixtures.
    # Its bytes are captured before cleanup. This is not a tamper-proof channel.
    with tempfile.TemporaryDirectory(prefix="hph-junit-") as temporary:
        report = pathlib.Path(temporary) / "report.xml"
        command += ["--collect-only"] if args.mode == "collect" else ["--junitxml=" + str(report)]
        code = int(pytest.main(command, plugins=[observer]))
        if args.mode == "run" and report.is_file():
            sys.stdout.buffer.write(b"\n" + report.read_bytes() + b"\n")
            sys.stdout.buffer.flush()
    observer.data["returned_exit_code"] = code
    emit(CHILD, observer.data)
    return code
def main(args):
    phases = {}
    for name, source in (("reference", args.reference_src), ("candidate", args.candidate_src)):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = source
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        command = [sys.executable, "-B", __file__, "--mode",
                   "collect" if name == "reference" else "run",
                   "--tests", args.tests, "--config", args.config]
        started = time.monotonic()
        run = subprocess.run(command, env=environment, cwd=str(pathlib.Path(args.config).parent),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Complete original streams remain present; labels distinguish their source.
        sys.stdout.buffer.write(("\nHPH_PHASE " + name + "\n").encode() + run.stdout)
        sys.stdout.buffer.flush()
        sys.stderr.buffer.write(("\nHPH_PHASE " + name + "\n").encode() + run.stderr)
        sys.stderr.buffer.flush()
        phases[name] = {"exit_code": run.returncode, "elapsed_seconds": time.monotonic() - started,
                        "stdout_sha256": hashlib.sha256(run.stdout).hexdigest(),
                        "stderr_sha256": hashlib.sha256(run.stderr).hexdigest(),
                        "observation": observation(run.stdout)}
        seen = phases[name]["observation"]
        if name == "reference" and (run.returncode != 0 or not seen.get("session_finished")
                or not seen.get("collection_complete") or not seen.get("collected_node_ids")):
            break
    emit(DRIVER, {"schema": "verifier-driver-v2",
        "driver_sha256": hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
        "phases": phases})
    return 0 if len(phases) == 2 and all(p["exit_code"] == 0 for p in phases.values()) else 1
parser = argparse.ArgumentParser()
parser.add_argument("--mode", choices=["driver", "collect", "run"], default="driver")
parser.add_argument("--reference-src", default="/reference/src")
parser.add_argument("--candidate-src", default="/candidate/src")
parser.add_argument("--tests", default="/baseline/tests")
parser.add_argument("--config", default="/baseline/pyproject.toml")
if __name__ == "__main__":
    args = parser.parse_args()
    raise SystemExit(main(args) if args.mode == "driver" else inner(args))
