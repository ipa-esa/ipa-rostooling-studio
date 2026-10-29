#!/usr/bin/env python3
"""
ask_oracle.py — feed model files to the REAL RosTooling language server and report what it says.

Everything else in this plugin validates against our own reimplementation of RosTooling's rules.
This script asks the actual toolchain. It is the only component here that does.

Usage:
    py ask_oracle.py <case-dir> [<case-dir> ...]
    py ask_oracle.py --all            # every directory under cases/

Each case directory is opened as its own LSP workspace root so cases cannot interfere
(two files declaring the same package would otherwise cross-contaminate).

Requires Java 19+. Temurin 21 lives outside PATH on this machine, so JAVA is pinned below.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent.parent
CODE = PLUGIN.parent / "material" / "code"

# How the java binary is found, in order:
#   1. ROSMODEL_JAVA, the documented override;
#   2. whatever `java` is on PATH -- which is what .lsp.json has always done
#      ("${ROSMODEL_JAVA:-java}") and what this file conspicuously did not, so on any machine
#      that had a perfectly good JDK but had not set the variable, the oracle reported
#      "FATAL: java not found at C:\Users\mae\..." -- a path from someone else's laptop;
#   3. that original hard-coded Adoptium path, kept last so the machine it was written for
#      keeps working even with an empty PATH.
_JAVA_FALLBACK = r"C:\Users\mae\AppData\Local\Programs\Eclipse Adoptium\jdk-21.0.11.10-hotspot\bin\java.exe"


def _resolve_java():
    env = os.environ.get("ROSMODEL_JAVA", "").strip()
    if env:
        return Path(env)
    found = shutil.which("java")
    if found:
        return Path(found)
    return Path(_JAVA_FALLBACK)


JAVA = _resolve_java()

# The server jar is built for Java 21 (its manifest says Build-Jdk-Spec: 21) and the launcher
# needs 19+. NOTHING in this repo checked that -- the requirement lived only in prose, in
# SKILL.md and scripts/README.md -- so an older JVM passed the exists() test, started, and then
# died inside the JVM with UnsupportedClassVersionError. What the caller saw was the 45-second
# initialize wait timing out and NO_INITIALIZE_RESPONSE: a message about the LSP handshake for
# what is really "your Java is too old", which is close to the least actionable way to say it.
JAVA_MIN_MAJOR = 19


def java_major(java_path):
    """Major version of that java binary, or None if it cannot be determined.

    `java -version` prints to STDERR ("openjdk version \"11.0.26\" ..." / "1.8.0_412" for 8),
    which is why this reads stderr and not stdout.
    """
    try:
        proc = subprocess.run([str(java_path), "-version"], capture_output=True, text=True,
                              timeout=30)
    except Exception:
        return None
    text = (proc.stderr or "") + (proc.stdout or "")
    m = re.search(r'version\s+"(\d+)(?:\.(\d+))?', text)
    if not m:
        return None
    major = int(m.group(1))
    # 1.8 -> 8; anything from 9 on states its major directly.
    if major == 1:
        return int(m.group(2) or 0)
    return major


def preflight():
    """(ok, reason). Can the real language server actually be asked, on this machine, now?

    One implementation, used by main() for its own gate AND exposed as `--preflight` so
    ros_studio.py can ask the question cheaply before committing to a 45-second wait. Two copies
    of "where is java, where is the jar" would drift the moment either path moved.
    """
    if not JAVA.exists():
        return False, ("java not found at %s\n"
                       "  set ROSMODEL_JAVA to your Java %d+ binary, or put `java` on PATH"
                       % (JAVA, JAVA_MIN_MAJOR))
    major = java_major(JAVA)
    if major is None:
        return False, ("could not read a version from %s (`java -version` failed)\n"
                       "  set ROSMODEL_JAVA to a working Java %d+ binary"
                       % (JAVA, JAVA_MIN_MAJOR))
    if major < JAVA_MIN_MAJOR:
        return False, ("%s is Java %d, but the language server jar needs Java %d+\n"
                       "  (the jar is built with Build-Jdk-Spec: 21; an older JVM loads it and "
                       "throws UnsupportedClassVersionError, which surfaces only as a timed-out "
                       "handshake)\n"
                       "  set ROSMODEL_JAVA to a Java %d+ binary"
                       % (JAVA, major, JAVA_MIN_MAJOR, JAVA_MIN_MAJOR))
    if JAR_OVERRIDE:
        if not Path(JAR_OVERRIDE).exists():
            return False, "Language server jar override points at %s, which does not exist" % JAR_OVERRIDE
        return True, "java %d, jar %s (override)" % (major, JAR_OVERRIDE)
    if ORACLE_MODE == "legacy":
        for j in (JAR_LEGACY_ROS2, JAR_LEGACY_ROSSYSTEM):
            if not j.exists():
                return False, "legacy jar not found at %s" % j
        return True, "java %d, legacy jars" % major
    if not JAR_CURRENT.exists():
        return False, ("language server jar not found at %s\n"
                       "  build it per build/README.md, or set ROSMODEL_ORACLE=legacy to fall "
                       "back to the 2024 jars" % JAR_CURRENT)
    return True, "java %d, jar %s" % (major, JAR_CURRENT.name)
# ── language server jars ──────────────────────────────────────────────────────
#
# CURRENT (default). Automatically discovers the canonical RosTooling_Extension
# bundled fat jar from installed extensions, settings, or sibling workspace repository.
# Falls back to local build/ if neither is present.

def _cli_jar_override() -> str:
    for i, a in enumerate(sys.argv):
        if a == "--jar" and i + 1 < len(sys.argv):
            return sys.argv[i + 1].strip()
        if a.startswith("--jar="):
            return a.split("=", 1)[1].strip()
    return ""


def _discover_current_jar() -> Path:
    override = _cli_jar_override() or os.environ.get("ROSMODEL_JAR", "").strip()
    if override:
        return Path(override)

    # 1. Studio persistent settings in ~/ros-studio/runtime-settings.json or ~/.rostooling/settings.json
    for s_path in [Path.home() / "ros-studio" / "runtime-settings.json", Path.home() / ".rostooling" / "settings.json"]:
        if s_path.exists():
            try:
                data = json.loads(s_path.read_text(encoding="utf-8"))
                val = data.get("rosmodelJar")
                if val and Path(val).exists():
                    return Path(val)
            except Exception:
                pass

    # 2. Installed VS Code / Antigravity / Cursor extensions (standard for production & agents)
    for base in [
        Path.home() / ".antigravity-ide" / "extensions",
        Path.home() / ".vscode" / "extensions",
        Path.home() / ".cursor" / "extensions",
        Path.home() / ".vscode-server" / "extensions",
        Path.home() / ".vscode-oss" / "extensions"
    ]:
        if base.exists():
            jars = sorted(base.glob("**/server/rostooling_extension-*.jar"), reverse=True)
            if jars:
                return jars[0]

    # 3. Sibling RosTooling_Extension repo (developer workspace fallback)
    sibling_server = PLUGIN.parent / "RosTooling_Extension" / "rostooling-languages" / "server"
    if sibling_server.exists():
        jars = sorted(sibling_server.glob("rostooling_extension-*.jar"), reverse=True)
        if jars:
            return jars[0]

    # 4. Local fallback build jar
    return PLUGIN / "build" / "ros2-ls" / "target" / \
        "de.fraunhofer.ipa.rostooling.ls-3.1.0-SNAPSHOT-ls.jar"


JAR_CURRENT = _discover_current_jar()

# LEGACY, kept selectable for A/B comparison and as a fallback. Read-only reference clone.
JAR_LEGACY_ROS2 = CODE / "vscode-RosTooling" / "resources" / \
    "de.fraunhofer.ipa.ros2.xtext.ide-3.0.0-SNAPSHOT-ls.jar"

# LEGACY rossystem server: the 2024 fat jar with only rossystem recompiled on top, so its
# ros/ros2 grammar is equally stale. Superseded by JAR_CURRENT.
JAR_LEGACY_ROSSYSTEM = PLUGIN / "build" / "rossystem-ls" / "target" / \
    "de.fraunhofer.ipa.rossystem.xtext.ide-3.0.0-SNAPSHOT-ls.jar"

# ROSMODEL_ORACLE selects which server to use:
#   current (default) | legacy   — 'legacy' restores the pre-rebuild behaviour exactly
#                                  (old ros2 jar for .ros/.ros2, old rossystem jar for
#                                  .rossystem), for A/B-ing a diagnostic.
# ROSMODEL_JAR or --jar overrides with an explicit path and wins over ROSMODEL_ORACLE.
ORACLE_MODE = os.environ.get("ROSMODEL_ORACLE", "current").strip().lower()
JAR_OVERRIDE = _cli_jar_override() or os.environ.get("ROSMODEL_JAR", "").strip()

LAUNCHER = "org.eclipse.xtext.ide.server.ServerLauncher"

# Model files we feed the server, in the order we open them. Order matters: a .rossystem
# resolves `from:` and arrow targets into .ros/.ros2, so those must be loaded first.
# NOTE: .ros1 is deliberately absent — no Ros1IdeSetup is registered in any of these jars,
# so .ros1 files cannot be served. See build/README.md.
MODEL_GLOBS = ("*.ros", "*.ros2", "*.rossystem")


def jar_for(files) -> Path:
    """Pick the language server for a case directory."""
    if JAR_OVERRIDE:
        return Path(JAR_OVERRIDE)
    if ORACLE_MODE == "legacy":
        if any(f.suffix == ".rossystem" for f in files):
            return JAR_LEGACY_ROSSYSTEM
        return JAR_LEGACY_ROS2
    # 'current' serves every supported extension from a single jar.
    return JAR_CURRENT

SEVERITY = {1: "ERROR", 2: "WARNING", 3: "INFO", 4: "HINT"}


# ── LSP plumbing ──────────────────────────────────────────────────────────────

class Server:
    def __init__(self, root: Path, jar: Path = None, verbose=False):
        self.root = root
        self.verbose = verbose
        self.jar = jar or JAR_CURRENT

        if "rostooling_extension" in self.jar.name:
            cmd = [
                str(JAVA),
                "--add-opens=java.base/java.lang=ALL-UNNAMED",
                "--add-opens=java.base/java.util=ALL-UNNAMED",
            ]
            env_cat = os.environ.get("ROSMODEL_CATALOGUE_DIR", "").strip()
            cat_path = (
                Path(env_cat).expanduser().resolve()
                if env_cat
                else (Path.home() / ".rostooling" / "catalogue_repos")
            )
            if cat_path.exists():
                cmd.append(f"-Drostooling.catalogue.path={cat_path}")
            cmd.extend(["-jar", str(self.jar)])
        else:
            cmd = [str(JAVA), "-cp", str(self.jar), LAUNCHER]

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.inbox = queue.Queue()
        self.stderr_lines = []
        self._next_id = 0
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        f = self.proc.stdout
        while True:
            header, length = b"", None
            while True:
                line = f.readline()
                if not line:
                    self.inbox.put(None)
                    return
                if line in (b"\r\n", b"\n"):
                    break
                header += line
                low = line.lower()
                if low.startswith(b"content-length:"):
                    length = int(line.split(b":")[1].strip())
            if length is None:
                continue
            body = b""
            while len(body) < length:
                chunk = f.read(length - len(body))
                if not chunk:
                    self.inbox.put(None)
                    return
                body += chunk
            try:
                self.inbox.put(json.loads(body.decode("utf-8")))
            except Exception as exc:  # malformed frame — surface, don't die
                self.inbox.put({"__parse_error__": str(exc), "raw": body[:400].decode("utf-8", "replace")})

    def _read_stderr(self):
        for line in iter(self.proc.stderr.readline, b""):
            s = line.decode("utf-8", "replace").rstrip()
            self.stderr_lines.append(s)
            if self.verbose:
                print(f"    [stderr] {s}", file=sys.stderr)

    def _send(self, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(raw) + raw)
        self.proc.stdin.flush()

    def request(self, method, params):
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        return self._next_id

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def drain(self, seconds, until_uri=None, until_result=False):
        """Collect messages for `seconds`. Stop early once the thing being waited for arrives:
        diagnostics for `until_uri`, or any response carrying a `result` for `until_result`.

        `until_result` exists because the initialize wait was the single largest cost in a run
        and it was a FIXED 45 seconds -- that drain had no early exit, so every invocation slept
        out the whole window even though its caller's only test is `any("result" in m)`. Asking
        the oracle about a four-file model took 65s, 48 of which were this wait and the 3s one
        that follows it.

        Exiting as soon as the answer the caller is looking for has arrived cannot weaken the
        check. A server that never answers still waits the full window and still reports
        NO_INITIALIZE_RESPONSE, and nothing is dropped either way: the inbox is a queue, so a
        message that arrives after this returns is read by the NEXT drain rather than lost --
        which, for a server that publishes diagnostics eagerly during indexing, means those
        diagnostics are now attributed by their own uri instead of being discarded here.
        """
        msgs, deadline = [], time.time() + seconds
        while time.time() < deadline:
            try:
                m = self.inbox.get(timeout=0.2)
            except queue.Empty:
                continue
            if m is None:
                break
            msgs.append(m)
            if until_uri and m.get("method") == "textDocument/publishDiagnostics":
                if m.get("params", {}).get("uri", "").lower() == until_uri.lower():
                    deadline = min(deadline, time.time() + 1.0)  # brief grace for follow-ups
            if until_result and "result" in m:
                deadline = min(deadline, time.time() + 1.0)      # same grace, same reason
        return msgs

    def close(self):
        try:
            self.request("shutdown", {})
            time.sleep(0.3)
            self.notify("exit", {})
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def uri_of(p: Path) -> str:
    return p.resolve().as_uri()


# ── one case ──────────────────────────────────────────────────────────────────

def run_case(case_dir: Path, verbose=False):
    files = []
    for g in MODEL_GLOBS:                 # glob order == open order (deps before dependents)
        files.extend(sorted(case_dir.glob(g)))
    if not files:
        return {"case": case_dir.name, "status": "NO_FILES", "diagnostics": []}

    jar = jar_for(files)
    if not jar.exists():
        return {"case": case_dir.name, "status": f"MISSING_JAR: {jar}", "diagnostics": []}

    srv = Server(case_dir, jar=jar, verbose=verbose)
    result = {"case": case_dir.name, "jar": jar.name,
              "files": [f.name for f in files], "diagnostics": [], "status": "OK"}

    try:
        srv.request("initialize", {
            "processId": os.getpid(),
            "rootUri": uri_of(case_dir),
            "workspaceFolders": [{"uri": uri_of(case_dir), "name": case_dir.name}],
            "capabilities": {
                "workspace": {
                    "workspaceFolders": True,
                    "executeCommand": {"dynamicRegistration": False}
                },
                "textDocument": {"publishDiagnostics": {"relatedInformation": True}}
            },
        })
        init = srv.drain(45, until_result=True)
        if not any("result" in m for m in init if isinstance(m, dict)):
            result["status"] = "NO_INITIALIZE_RESPONSE"
            result["stderr"] = srv.stderr_lines[-25:]
            return result
        srv.notify("initialized", {})
        srv.drain(3)

        for f in files:
            uri = uri_of(f)
            srv.notify("textDocument/didOpen", {
                "textDocument": {
                    "uri": uri,
                    "languageId": f.suffix.lstrip("."),
                    "version": 1,
                    "text": f.read_text(encoding="utf-8"),
                }
            })
            for m in srv.drain(25, until_uri=uri):
                if m.get("method") == "textDocument/publishDiagnostics":
                    p = m["params"]
                    for d in p.get("diagnostics", []):
                        result["diagnostics"].append({
                            "file": Path(p["uri"].replace("file:///", "")).name,
                            "line": d["range"]["start"]["line"] + 1,
                            "severity": SEVERITY.get(d.get("severity", 1), "?"),
                            "message": d.get("message", "").strip(),
                        })
    finally:
        srv.close()

    if srv.stderr_lines:
        result["stderr"] = srv.stderr_lines[-25:]
    return result


def _results_path(is_partial_run=False):
    """Where to write the run's verdict.

    Default is tests/oracle/results.json for full test runs (--all), but NOT when that
    file has uncommitted changes or when running a subset of cases. A partial run writes
    to results.local.json so it never accidentally truncates the 19-case regression baseline.
    Pass --results PATH to force a specific location.
    """
    for i, a in enumerate(sys.argv):
        if a == "--results" and i + 1 < len(sys.argv):
            return Path(sys.argv[i + 1])
        if a.startswith("--results="):
            return Path(a.split("=", 1)[1])

    default = HERE / "results.json"
    if is_partial_run:
        alt = default.with_name("results.local.json")
        print(f"NOTE: Partial test run. Verdict written to {alt.name} to preserve the\n"
              f"      checked-in regression baseline in {default.name}.\n"
              f"      Pass --all to run all cases, or --results to override.",
              file=sys.stderr)
        return alt

    try:
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", str(default)],
            cwd=str(HERE), capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:
        dirty = ""                      # not a git checkout, or git unavailable
    if not dirty:
        return default

    alt = default.with_name("results.local.json")
    print(f"NOTE: {default.name} has uncommitted changes, so it was left alone.\n"
          f"      This run's verdict went to {alt.name} instead.\n"
          f"      Commit or stash that file, or pass --results, to write it directly.",
          file=sys.stderr)
    return alt


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    args = []
    skip_next = False
    for a in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if a == "--jar":
            skip_next = True
            continue
        if a.startswith("--jar="):
            continue
        if not a.startswith("-"):
            args.append(a)

    verbose = "-v" in sys.argv or "--verbose" in sys.argv

    # One gate for both callers. `--preflight` answers "could the oracle run?" without running
    # it -- ros_studio.py asks that before committing to a 45-second initialize wait, and needs
    # the REASON verbatim to put in front of the user rather than a bare exit code.
    ok, reason = preflight()
    if "--preflight" in sys.argv:
        print(reason if ok else ("FATAL: " + reason), file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 3
    if not ok:
        print("FATAL: " + reason, file=sys.stderr)
        return 2
    if JAR_OVERRIDE:
        print(f"oracle language server JAR override -> {JAR_OVERRIDE}", file=sys.stderr)
    elif ORACLE_MODE == "legacy":
        print("oracle mode: LEGACY (stale 2024 grammar — QoS fields will not parse)",
              file=sys.stderr)

    if "--all" in sys.argv or not args:
        dirs = sorted(d for d in (HERE / "cases").iterdir() if d.is_dir())
    else:
        dirs = [Path(a).resolve() for a in args]

    print(f"java   {JAVA}")
    print(f"cases  {len(dirs)}   (jar chosen per case by file extension)\n")

    results = []
    for d in dirs:
        print(f"== {d.name} " + "=" * max(0, 56 - len(d.name)))
        r = run_case(d, verbose=verbose)
        results.append(r)

        if r["status"] != "OK":
            print(f"   STATUS: {r['status']}")
            for line in r.get("stderr", [])[-12:]:
                print(f"   | {line}")
            print()
            continue

        errs = [d_ for d_ in r["diagnostics"] if d_["severity"] == "ERROR"]
        warns = [d_ for d_ in r["diagnostics"] if d_["severity"] == "WARNING"]
        verdict = "REJECTED" if errs else "ACCEPTED"
        print(f"   {verdict}  —  {len(errs)} error(s), {len(warns)} warning(s)   [{r.get('jar','?')}]")
        for d_ in r["diagnostics"][:14]:
            print(f"     {d_['severity']:<8} line {d_['line']:<4} {d_['message'][:100]}")
        if len(r["diagnostics"]) > 14:
            print(f"     ... {len(r['diagnostics']) - 14} more")
        print()

    is_partial = not ("--all" in sys.argv or not args)
    out = _results_path(is_partial_run=is_partial)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"full results: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
