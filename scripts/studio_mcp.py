"""Headless stdio JSON-RPC 2.0 client coupling CoreSense Studio Stage 5 to rostooling-mcp."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

import rosmodel_lint as L
from studio_store import fail


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _scan_domain_logic_files(workspace_dir: Path) -> dict[str, str]:
    """Snapshot existing domain logic files (*Algorithm.hpp, *_logic.py) under workspace_dir."""
    protected: dict[str, str] = {}
    if not workspace_dir.is_dir():
        return protected
    for root, dirs, files in os.walk(workspace_dir):
        dirs[:] = [d for d in dirs if d not in {'.git', 'build', 'install', 'log', 'node_modules', '__pycache__'}]
        for fname in files:
            if fname.endswith('Algorithm.hpp') or fname.endswith('_logic.py'):
                fpath = Path(root) / fname
                try:
                    protected[str(fpath.resolve())] = _sha256_file(fpath)
                except OSError:
                    continue
    return protected


def resolve_mcp_command(repo_root: Path) -> list[str] | None:
    """Locate the rostooling-mcp executable or sibling bin/rostooling-mcp.js script."""
    env_bin = os.environ.get('ROSTOOLING_MCP_BIN', '').strip()
    if env_bin:
        cand = Path(env_bin).expanduser().resolve()
        if cand.is_file():
            if cand.suffix == '.js':
                node = shutil.which('node')
                return [node, str(cand)] if node else None
            return [str(cand)]

    on_path = shutil.which('rostooling-mcp')
    if on_path:
        return [on_path]

    node = shutil.which('node')
    if not node:
        return None

    sibling_candidates = [
        repo_root.parent / 'RosTooling_Extension' / 'rostooling-languages' / 'bin' / 'rostooling-mcp.js',
        repo_root / 'RosTooling_Extension' / 'rostooling-languages' / 'bin' / 'rostooling-mcp.js',
    ]
    for cand in sibling_candidates:
        if cand.is_file():
            return [node, str(cand.resolve())]
    return None


class StudioMcpBridge:
    """Thread-safe JSON-RPC 2.0 stdio client for rostooling-mcp."""

    def __init__(self, store, repo_root: Path):
        self.store = store
        self.repo_root = Path(repo_root).resolve()
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._inbox: queue.Queue = queue.Queue()
        self._stderr_tail: list[str] = []
        self._next_id = 0
        self._initialized = False

    def status(self) -> dict:
        argv = resolve_mcp_command(self.repo_root)
        return {
            'ok': True,
            'available': bool(argv),
            'argv': argv,
            'catalogueDir': L.get_shared_catalogue_dir(),
        }

    def close(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
            self._initialized = False
            if proc is not None:
                try:
                    if proc.stdin:
                        proc.stdin.close()
                except OSError:
                    pass
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.poll() is None and self._initialized:
            return
        self.close()
        argv = resolve_mcp_command(self.repo_root)
        if not argv:
            fail(
                'rostooling-mcp was not found. Set ROSTOOLING_MCP_BIN or ensure RosTooling_Extension is compiled.',
                'mcp_unavailable',
                503,
            )
        env = dict(os.environ)
        env.setdefault('ROSMODEL_CATALOGUE_DIR', L.get_shared_catalogue_dir())
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            fail(f'Failed to start rostooling-mcp: {exc}', 'mcp_unavailable', 503)

        self._proc = proc
        self._inbox = queue.Queue()
        self._stderr_tail = []

        def _read_stdout():
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    self._inbox.put(json.loads(line))
                except ValueError:
                    continue
            self._inbox.put(None)

        def _read_stderr():
            assert proc.stderr is not None
            for raw_line in proc.stderr:
                self._stderr_tail.append(raw_line.rstrip())
                if len(self._stderr_tail) > 80:
                    self._stderr_tail.pop(0)

        threading.Thread(target=_read_stdout, daemon=True).start()
        threading.Thread(target=_read_stderr, daemon=True).start()

        init_res = self._request(
            'initialize',
            {
                'protocolVersion': '2024-11-05',
                'capabilities': {},
                'clientInfo': {'name': 'coresense-studio', 'version': '1.0.0'},
            },
            timeout=30,
        )
        if 'error' in init_res:
            self.close()
            fail('rostooling-mcp initialization failed: ' + json.dumps(init_res['error']), 'mcp_error', 502)
        self._notify('notifications/initialized', {})
        self._initialized = True

    def _notify(self, method: str, params: dict) -> None:
        if not self._proc or not self._proc.stdin:
            fail('rostooling-mcp is not running.', 'mcp_unavailable', 503)
        payload = json.dumps({'jsonrpc': '2.0', 'method': method, 'params': params}) + '\n'
        self._proc.stdin.write(payload)
        self._proc.stdin.flush()

    def _request(self, method: str, params: dict, timeout: float = 180.0) -> dict:
        if not self._proc or not self._proc.stdin:
            fail('rostooling-mcp is not running.', 'mcp_unavailable', 503)
        self._next_id += 1
        req_id = self._next_id
        payload = json.dumps({'jsonrpc': '2.0', 'id': req_id, 'method': method, 'params': params}) + '\n'
        try:
            self._proc.stdin.write(payload)
            self._proc.stdin.flush()
        except OSError as exc:
            self.close()
            fail(f'Lost connection to rostooling-mcp: {exc}', 'mcp_error', 502)

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                fail(
                    f'rostooling-mcp timed out after {int(timeout)}s during {method}.',
                    'mcp_timeout',
                    504,
                    {'stderr': '\n'.join(self._stderr_tail[-20:])},
                )
            try:
                msg = self._inbox.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if self._proc.poll() is not None:
                    err_tail = '\n'.join(self._stderr_tail[-20:])
                    self.close()
                    fail('rostooling-mcp exited unexpectedly. ' + err_tail, 'mcp_error', 502)
                continue
            if msg is None:
                err_tail = '\n'.join(self._stderr_tail[-20:])
                self.close()
                fail('rostooling-mcp closed stdout unexpectedly. ' + err_tail, 'mcp_error', 502)
            if isinstance(msg, dict) and msg.get('id') == req_id:
                return msg

    def call_tool(self, tool_name: str, arguments: dict, timeout: float = 180.0) -> dict:
        with self._lock:
            self._ensure_started()
            resp = self._request('tools/call', {'name': tool_name, 'arguments': arguments}, timeout=timeout)
            if 'error' in resp:
                fail(
                    f'MCP tool {tool_name} failed: {resp["error"].get("message", resp["error"])}',
                    'mcp_tool_error',
                    502,
                    {'mcpError': resp['error']},
                )
            result = resp.get('result') or {}
            content = result.get('content') or []
            parsed_items = []
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'text':
                    text = item.get('text', '')
                    try:
                        parsed_items.append(json.loads(text))
                    except ValueError:
                        parsed_items.append({'text': text})
            primary = parsed_items[0] if len(parsed_items) == 1 and isinstance(parsed_items[0], dict) else {'items': parsed_items}
            return {'ok': not bool(result.get('isError')) and 'error' not in primary, 'tool': tool_name, 'result': primary}

    def _prepare_project_models(self, pid: str, body: dict) -> tuple[Path, Path, list[Path]]:
        """Ensure project models exist on disk and return (workspace_dir, models_dir, written_paths)."""
        root = self.store.root(pid)
        state = self.store.state(root)
        ws_raw = body.get('workspaceDir')
        if isinstance(ws_raw, str) and ws_raw.strip():
            workspace_dir = Path(ws_raw).expanduser().resolve()
            models_dir = workspace_dir / 'models' if not any(workspace_dir.glob('*.rossystem')) else workspace_dir
        elif state.get('promotedTargetDir'):
            models_dir = Path(state['promotedTargetDir']).resolve()
            workspace_dir = models_dir.parent if models_dir.name == 'models' else models_dir
        else:
            workspace_dir = root.resolve()
            models_dir = (root / 'models').resolve()

        promote_first = body.get('promoteFirst', True)
        existing = sorted(x for x in models_dir.glob('*') if x.is_file() and x.suffix in {'.ros', '.ros2', '.rossystem'}) if models_dir.is_dir() else []
        if promote_first or not existing:
            mat = self.store.materialize_models(pid, {'targetDir': str(models_dir)})
            written = [Path(p) for p in mat.get('writtenFiles', [])]
            return workspace_dir, models_dir, written
        return workspace_dir, models_dir, existing

    def validate_models(self, pid: str, body: dict | None = None) -> dict:
        body = body or {}
        explicit = body.get('modelPath')
        if isinstance(explicit, str) and explicit.strip():
            model_paths = [Path(explicit).expanduser().resolve()]
            workspace_dir = Path(body['workspaceDir']).expanduser().resolve() if body.get('workspaceDir') else model_paths[0].parent
        else:
            workspace_dir, _, written = self._prepare_project_models(pid, body)
            rossystems = [p for p in written if p.suffix == '.rossystem']
            model_paths = rossystems or written

        reports = []
        all_valid = True
        for mp in model_paths:
            res = self.call_tool('validate_ros_model', {'model_path': str(mp), 'workspace_dir': str(workspace_dir)})
            payload = res['result']
            if not payload.get('isValid', False) or 'error' in payload:
                all_valid = False
            reports.append(payload)
        return {
            'ok': True,
            'isValid': all_valid,
            'workspaceDir': str(workspace_dir),
            'reports': reports,
        }

    def generate_wrappers(self, pid: str, body: dict | None = None) -> dict:
        body = body or {}
        language = body.get('language') if body.get('language') in ('cpp', 'python', 'both') else 'cpp'
        ros_distro = body.get('rosDistro') if body.get('rosDistro') in ('humble', 'jazzy', 'iron', 'rolling') else 'humble'
        target_nodes = body.get('targetNodes') if isinstance(body.get('targetNodes'), list) else None

        explicit = body.get('modelPath')
        if isinstance(explicit, str) and explicit.strip():
            ros2_paths = [Path(explicit).expanduser().resolve()]
            workspace_dir = Path(body['workspaceDir']).expanduser().resolve() if body.get('workspaceDir') else ros2_paths[0].parent
        else:
            workspace_dir, _, written = self._prepare_project_models(pid, body)
            ros2_paths = [p for p in written if p.suffix == '.ros2']

        if not ros2_paths:
            return {
                'ok': True,
                'workspaceDir': str(workspace_dir),
                'packages': [],
                'protectedDomainFiles': [],
                'message': 'No project-owned .ros2 package models to generate wrappers for (all nodes use off-the-shelf catalogue contracts).',
            }

        before_protected = _scan_domain_logic_files(workspace_dir)
        packages = []
        for mp in ros2_paths:
            args = {
                'model_path': str(mp),
                'language': language,
                'ros_distro': ros_distro,
                'workspace_dir': str(workspace_dir),
            }
            if target_nodes:
                args['target_nodes'] = target_nodes
            res = self.call_tool('generate_ros2_wrappers', args, timeout=240.0)
            packages.append(res['result'])

        after_protected = _scan_domain_logic_files(workspace_dir)
        preserved = [
            path for path, digest_before in before_protected.items()
            if after_protected.get(path) == digest_before
        ]
        violated = [
            path for path, digest_before in before_protected.items()
            if path in after_protected and after_protected[path] != digest_before
        ]
        if violated:
            fail(
                'Domain Logic Protection Shield violation detected!',
                'domain_shield_violation',
                500,
                {'violatedFiles': violated},
            )
        return {
            'ok': True,
            'workspaceDir': str(workspace_dir),
            'language': language,
            'rosDistro': ros_distro,
            'packages': packages,
            'protectedDomainFiles': preserved,
        }

    def generate_system(self, pid: str, body: dict | None = None) -> dict:
        body = body or {}
        explicit = body.get('modelPath')
        if isinstance(explicit, str) and explicit.strip():
            sys_paths = [Path(explicit).expanduser().resolve()]
            workspace_dir = Path(body['workspaceDir']).expanduser().resolve() if body.get('workspaceDir') else sys_paths[0].parent
        else:
            workspace_dir, _, written = self._prepare_project_models(pid, body)
            sys_paths = [p for p in written if p.suffix == '.rossystem']

        if not sys_paths:
            fail('No .rossystem model found for system launch generation.', 'invalid_model', 422)

        before_protected = _scan_domain_logic_files(workspace_dir)
        systems = []
        for sp in sys_paths:
            res = self.call_tool(
                'generate_rossystem_code',
                {'model_path': str(sp), 'workspace_dir': str(workspace_dir)},
                timeout=240.0,
            )
            systems.append(res['result'])

        after_protected = _scan_domain_logic_files(workspace_dir)
        preserved = [
            path for path, digest_before in before_protected.items()
            if after_protected.get(path) == digest_before
        ]
        return {
            'ok': True,
            'workspaceDir': str(workspace_dir),
            'systems': systems,
            'protectedDomainFiles': preserved,
        }

    def build_workspace(self, pid: str, body: dict | None = None) -> dict:
        body = body or {}
        root = self.store.root(pid)
        state = self.store.state(root)
        if isinstance(body.get('workspaceDir'), str) and body['workspaceDir'].strip():
            workspace_dir = Path(body['workspaceDir']).expanduser().resolve()
        elif state.get('promotedTargetDir'):
            pdir = Path(state['promotedTargetDir']).resolve()
            workspace_dir = pdir.parent if pdir.name == 'models' else pdir
        else:
            workspace_dir = root.resolve()

        args: dict = {
            'workspace_dir': str(workspace_dir),
            'symlink_install': bool(body.get('symlinkInstall', True)),
        }
        pkg = body.get('packageName')
        if isinstance(pkg, str) and pkg.strip():
            args['package_name'] = pkg.strip()

        res = self.call_tool('build_ros2_package', args, timeout=300.0)
        return {
            'ok': True,
            'workspaceDir': str(workspace_dir),
            'build': res['result'],
        }
