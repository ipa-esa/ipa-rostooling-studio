"""Static ROS source inspection and reviewable model import. Never runs launch code."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

import ros_studio as adapter


IGNORED = {'.git', '.studio', '.work', 'build', 'install', 'log', '__pycache__',
           'node_modules', '.venv', 'venv', '.pytest_cache', '.mypy_cache'}
SUFFIXES = {'.py', '.cpp', '.cc', '.cxx', '.c', '.hpp', '.hh', '.hxx', '.h', '.xml',
            '.yaml', '.yml', '.json', '.cfg', '.toml', '.msg', '.srv', '.action', '.idl',
            '.ros2', '.rossystem', '.ros', '.repos'}
MAX_FILE = 4 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024


class SourceError(Exception):
    def __init__(self, code, message, status=400, details=None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details or {}


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _project_hash(root):
    path = root / 'project.json'
    if path.is_symlink():
        raise SourceError('source_symlink', 'The project file must not be a symbolic link.', 403)
    try:
        return _hash(path.read_bytes())
    except FileNotFoundError:
        return None


def _relative(root, value, directory=False):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise SourceError('source_path', 'Choose a local source path.')
    raw = Path(value)
    if '..' in raw.parts:
        raise SourceError('source_path', 'Parent-directory paths are not allowed.', 403)
    target = raw if raw.is_absolute() else root / raw
    if not target.resolve().is_relative_to(root):
        raise SourceError('source_path', 'Source path leaves the selected workspace.', 403)
    if (directory and not target.is_dir()) or (not directory and not target.is_file()):
        raise SourceError('source_missing', 'The selected source path does not exist.', 404)
    return target.resolve()


def _json(path, default=None):
    if not path.is_file():
        return copy.deepcopy(default)
    try:
        return json.loads(path.read_text(encoding='utf8'))
    except (ValueError, UnicodeError):
        return copy.deepcopy(default)


class StudioSource:
    def __init__(self, repo_root, storage_root):
        self.repo = Path(repo_root).resolve()
        self.storage = Path(storage_root).resolve() / 'source-plans'
        self.storage.mkdir(parents=True, exist_ok=True)

    def _root(self, root):
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise SourceError('source_missing', 'Workspace directory does not exist.', 404)
        return root

    @staticmethod
    def _default_source(root):
        # A linked folder that is itself a package owns its src/ (C++ sources), so scan the
        # package root; only a workspace without its own package.xml is scanned at src/.
        return root / 'src' if (root / 'src').is_dir() and not (root / 'package.xml').is_file() else root

    def _inventory(self, root, source):
        records, total, seen = [], 0, set()
        for base, dirs, files in os.walk(source, followlinks=True):
            real = os.path.realpath(base)
            if real in seen:
                dirs[:] = []
                continue
            seen.add(real)
            kept = []
            for directory in dirs:
                if directory in IGNORED:
                    continue
                path = Path(base) / directory
                # A symlink that stays inside the workspace we were asked to scan (e.g. a vendored
                # monorepo referencing its own subtrees) is not a risk; only an escape needs a real checkout.
                if path.is_symlink() and not path.resolve().is_relative_to(root):
                    raise SourceError('source_symlink', 'Linked source directories need an explicit checkout.',
                                      403, {'path': str(path.relative_to(root))})
                kept.append(directory)
            dirs[:] = sorted(kept)
            for name in sorted(files):
                path = Path(base) / name
                if path.suffix not in SUFFIXES and name not in {'CMakeLists.txt', 'Dockerfile'}:
                    continue
                if path.is_symlink() and not path.resolve().is_relative_to(root):
                    raise SourceError('source_symlink', 'Source files through links need an explicit checkout.',
                                      403, {'path': str(path.relative_to(root))})
                # Generated model files and Studio metadata are not source extraction inputs.
                if path.name == 'project.json' or path.suffix in {'.ros', '.ros2', '.rossystem'}:
                    continue
                size = path.stat().st_size
                total += size
                if size > MAX_FILE or total > MAX_TOTAL or len(records) >= 10000:
                    raise SourceError('source_size', 'Choose a smaller source directory; scan limits were reached.', 413)
                data = path.read_bytes()
                if len(data) > MAX_FILE:
                    raise SourceError('source_size', 'A source file grew beyond the scan limit.', 413)
                rel = path.relative_to(root).as_posix()
                records.append({'path': rel, 'hash': _hash(data), 'size': len(data)})
        return records

    def inspect(self, root):
        root = self._root(root)
        source = self._default_source(root)
        records = self._inventory(root, source)
        project = _json(root / 'project.json', {})
        packages, diagnostics = [], []
        for entry in records:
            if Path(entry['path']).name != 'package.xml':
                continue
            try:
                element = ET.fromstring((root / entry['path']).read_bytes())
                name = element.findtext('name')
                if not name:
                    raise ValueError('package name is absent')
                packages.append({'name': name, 'path': str(Path(entry['path']).parent),
                                 'buildType': element.findtext('export/build_type') or 'unknown'})
            except (ET.ParseError, ValueError) as exc:
                diagnostics.append({'severity': 'error', 'path': entry['path'], 'message': str(exc)})
        for entry in records:
            package = next((p for p in sorted(packages, key=lambda p: -len(p['path']))
                            if Path(entry['path']).is_relative_to(Path(p['path']))), None)
            entry['package'] = package['name'] if package else None
            rel = Path(entry['path'])
            entry['kind'] = ('launch' if rel.name.endswith('.launch.py') or 'launch' in rel.parts
                             else 'manifest' if rel.name in {'package.xml', 'setup.py', 'setup.cfg', 'CMakeLists.txt'}
                             else 'interface' if rel.suffix in {'.msg', '.srv', '.action', '.idl'}
                             else 'test' if 'test' in rel.parts or 'tests' in rel.parts
                             else 'configuration' if rel.suffix in {'.yaml', '.yml', '.xml', '.json'} else 'source')
            entry['moduleIds'] = [n['id'] for n in project.get('nodes', [])
                                  if n.get('pkg') == entry['package']]
        return {'ok': True, 'root': str(root), 'sourcePath': source.relative_to(root).as_posix(),
                'files': records, 'packages': packages,
                'launchFiles': [r['path'] for r in records if r['kind'] == 'launch' and r['path'].endswith('.py')],
                'diagnostics': diagnostics,
                'coverage': {'mode': 'static-source-inventory', 'fileCount': len(records),
                             'limitations': ['No source or launch code was executed.',
                                             'Dynamic runtime behavior and missing dependencies need review.']}}

    def _run(self, script, argv, cwd):
        try:
            result = subprocess.run([sys.executable, str(self.repo / 'scripts' / script), *argv],
                                    cwd=str(cwd), capture_output=True, text=True, timeout=90, shell=False)
        except subprocess.TimeoutExpired as exc:
            raise SourceError('source_timeout', 'Static extraction timed out; source files were not changed.', 503) from exc
        return {'tool': script, 'exitCode': result.returncode,
                'stdout': result.stdout[-65536:], 'stderr': result.stderr[-16384:]}

    def _current_hashes(self, root, source_path):
        source = _relative(root, source_path, directory=True)
        return {r['path']: r['hash'] for r in self._inventory(root, source)}

    def preview(self, root, request, *, purpose='import'):
        if purpose not in ('import', 'catalogue'):
            raise SourceError('source_plan', 'Unknown extraction purpose.')
        root = self._root(root)
        base_project_hash = _project_hash(root) if purpose == 'import' else None
        source = _relative(root, request.get('sourcePath') or self._default_source(root).relative_to(root).as_posix(), directory=True)
        launch = _relative(root, request['launchPath']) if request.get('launchPath') else None
        if launch and not launch.name.endswith('.py'):
            raise SourceError('launch_type', 'The static launch extractor supports Python launch files.', 422)
        if launch and not launch.is_relative_to(source):
            raise SourceError('launch_scope', 'Choose a source directory containing the launch file.', 422)
        name = request.get('systemName') or root.name.replace('-', '_')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,95}', name):
            raise SourceError('system_name', 'System name must use letters, numbers and underscores.')
        controllers = _relative(root, request['controllersPath']) if request.get('controllersPath') else None
        if controllers and (not controllers.is_relative_to(source) or controllers.suffix not in {'.yaml', '.yml'}):
            raise SourceError('controllers_scope', 'Choose a YAML controller configuration inside the source directory.', 422)
        inputs = self._inventory(root, source)
        if not any(Path(r['path']).name == 'package.xml' for r in inputs):
            raise SourceError('no_packages', 'No ROS package.xml was found in the selected source directory.', 422)
        ident = uuid.uuid4().hex
        stage = self.storage / ident
        snapshot, models = stage / 'source', stage / 'models'
        models.mkdir(parents=True)
        hashes = {r['path']: r['hash'] for r in inputs}
        for entry in inputs:
            data = (root / entry['path']).read_bytes()
            if _hash(data) != entry['hash']:
                raise SourceError('source_changed', 'Source changed while preparing the scan; scan again.', 409)
            target = snapshot / entry['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        selected = snapshot / source.relative_to(root)
        package_record, system_record = stage / 'packages.json', stage / 'system.json'
        tools = [self._run('extract_ros2_interfaces.py', [str(selected), '-o', str(models),
                            '--emit-msgs', str(models), '--json', str(package_record)], stage)]
        if launch:
            # Controller paths in imported launch text cannot authorize arbitrary host reads.
            # Explicit selected YAML is staged; otherwise an empty config keeps spawners unresolved.
            ctrl = snapshot / controllers.relative_to(root) if controllers else stage / 'no-controllers.yaml'
            if not controllers:
                ctrl.write_text('{}\n')
            tools.append(self._run('extract_rossystem.py', [str(snapshot / launch.relative_to(root)),
                              '--models', str(models), '--workspace', str(selected), '--system-name', name,
                              '--controllers-file', str(ctrl), '-o', str(models / (name + '.rossystem')),
                              '--json', str(system_record)], stage))
        packages, system = _json(package_record, {'packages': []}), _json(system_record, {})
        report_root = selected.parent if (selected / 'package.xml').is_file() else selected
        flags = []
        for package in packages['packages']:
            for flag in package.get('flags', []):
                item = dict(flag, package=package['package'])
                path, line = item['at'].rsplit(':', 1)
                item['at'] = (report_root / path).relative_to(snapshot).as_posix() + ':' + line
                flags.append(item)
        flags += [dict(flag, at=flag.get('at', '').replace(str(snapshot) + '/', '')) for flag in system.get('flags', [])]
        diagnostics = [{'severity': 'warning' if run['exitCode'] else 'info', 'tool': run['tool'],
                        'message': (run['stdout'] + '\n' + run['stderr']).strip(), 'exitCode': run['exitCode']}
                       for run in tools]
        output = models / (name + '.rossystem')
        project = None
        if output.exists():
            try:
                project = adapter.seed_from_rossystem(str(output))
            except Exception as exc:
                diagnostics.append({'severity': 'error', 'message': 'Extracted model could not be loaded: ' + str(exc)})
        elif not launch:
            project = self._package_draft(models, name)
            diagnostics.append({'severity': 'warning', 'message': 'No launch file selected: package contracts are a draft inventory; no startup composition or connections were inferred.'})
        if project:
            self._link_project(project, root, snapshot, selected, packages, preserve_instance=purpose == 'import')
            project = json.loads(json.dumps(project).replace(str(snapshot) + '/', str(root) + '/'))
        candidates = []
        for i, item in enumerate(system.get('connection_candidates', [])):
            candidate = dict(item, id='candidate-' + str(i + 1), accepted=False)
            if project:
                candidate['fromEndpoint'] = self._endpoint(project, item['from_node'], item['from'])
                candidate['toEndpoint'] = self._endpoint(project, item['to_node'], item['to'])
            candidates.append(candidate)
        model_paths = [path for path in sorted(models.iterdir()) if path.suffix in {'.ros', '.ros2', '.rossystem'}]
        errors, _, _, findings = adapter.run_lint([str(path) for path in model_paths]) if model_paths else ([], [], [], [])
        diagnostics.extend({'severity': finding.severity.lower(), 'message': finding.message,
                            'finding': finding.as_dict()} for finding in findings)
        files = []
        for path in model_paths:
            content = path.read_text(encoding='utf8').replace(str(snapshot) + '/', str(root) + '/')
            files.append({'path': 'models/' + path.name, 'content': content, 'hash': _hash(content.encode())})
        baseline = _json(root / 'project.json', {}) if purpose == 'import' else {}
        changes = adapter.diff_facts(adapter.project_facts(baseline), adapter.project_facts(project)) if baseline.get('formatVersion') == 5 and project else []
        incomplete = bool(flags) or any(run['exitCode'] or 'INCOMPLETE:' in run['stdout'] for run in tools)
        record = {'ok': True, 'planId': ident, 'sourceRoot': str(root), 'sourcePath': source.relative_to(root).as_posix(),
                  'launchPath': launch.relative_to(root).as_posix() if launch else None,
                  'createdAt': time.time(), 'inputHashes': hashes, 'baseProjectHash': base_project_hash, 'purpose': purpose,
                  # A message-only package has no nodes but its types are still a reusable catalogue contract.
                  'project': project, 'canImport': bool(project and (project['nodes'] or purpose == 'catalogue' and project['types']) and not errors),
                  'files': files, 'diagnostics': diagnostics, 'flags': flags, 'candidateConnections': candidates,
                  'changes': changes, 'coverage': {'mode': 'static', 'complete': not incomplete,
                     'packages': len(packages['packages']), 'files': len(inputs), 'launchExecuted': False,
                     'controllersPath': request.get('controllersPath'),
                     'limitations': ['Dynamic expressions and skipped inputs remain unresolved.',
                                     'Candidate connections require individual acceptance.',
                                     'Without explicit controller YAML, controller spawners remain unresolved.',
                                     'ROS source was parsed, not built or run.']},
                  'extraction': {'packages': packages, 'system': system}}
        if self._current_hashes(root, record['sourcePath']) != hashes:
            raise SourceError('source_changed', 'Source changed during extraction; scan again.', 409)
        if purpose == 'import' and _project_hash(root) != base_project_hash:
            raise SourceError('source_model_changed', 'The model changed during extraction; scan again to review the current model.', 409)
        record = json.loads(json.dumps(record).replace(str(snapshot) + '/', str(root) + '/'))
        (stage / 'plan.json').write_text(json.dumps(record, indent=2), encoding='utf8')
        return record

    def _package_draft(self, models, name):
        project = adapter.blank_project(name)
        seen = set()
        for file in sorted(models.glob('*.ros2')):
            index, git = adapter.parse_ros2(str(file))
            for record in index.values():
                if not isinstance(record, dict):
                    continue
                key = (record['package'], record['artifact'])
                if key in seen:
                    continue
                seen.add(key)
                node_id = 'source_' + str(len(seen))
                project['nodes'].append({'id': node_id, 'label': record['node'] + ('_' + str(len(seen)) if any(n['label'] == record['node'] for n in project['nodes']) else ''),
                    'backing': 'hand', 'pkg': key[0], 'node': record['node'], 'artifact': key[1],
                    'namespace': None, 'x': 40 + (len(seen) - 1) % 3 * 300, 'y': 60 + (len(seen) - 1) // 3 * 250,
                    'ifaces': [dict(f, id=node_id + '_i' + str(i), exposed=True, label=None) for i, f in enumerate(record['interfaces'])],
                    'params': [dict(p, id=node_id + '_p' + str(i), exposed=False, label=None) for i, p in enumerate(record['params'])]})
                project['packages'][key[0]] = {'fromGitRepo': git.get(key[0])}
        for file in models.glob('*.ros'):
            types, _ = adapter.parse_ros(str(file))
            project['types'].update(types)
        return project

    @staticmethod
    def _endpoint(project, node_label, iface_label):
        for node in project['nodes']:
            if node['label'] == node_label:
                for iface in node['ifaces']:
                    if iface.get('label') == iface_label or iface['name'] == iface_label:
                        return {'n': node['id'], 'i': iface['id']}
        return None

    def _link_project(self, project, root, snapshot, selected, packages, *, preserve_instance=True):
        old = _json(root / 'project.json', {}) if preserve_instance else {}
        previous = {(n.get('label'), n.get('pkg'), n.get('node'), n.get('artifact')): n for n in old.get('nodes', [])}
        node_ids, iface_ids = {}, {}
        for node in project['nodes']:
            key = (node['label'], node['pkg'], node['node'], node['artifact'])
            before = previous.get(key, {})
            original = node['id']
            node['id'] = before.get('id') or 'source_' + uuid.uuid5(uuid.NAMESPACE_URL, str(root) + repr(key)).hex[:20]
            node_ids[original] = node['id']
            node['x'], node['y'] = before.get('x', node['x']), before.get('y', node['y'])
            old_ifaces = {(i['kind'], i['name']): i for i in before.get('ifaces', [])}
            for iface in node['ifaces']:
                original_iface = iface['id']
                iface['id'] = old_ifaces.get((iface['kind'], iface['name']), {}).get('id') or 'iface_' + uuid.uuid5(uuid.NAMESPACE_URL, node['id'] + iface['kind'] + iface['name']).hex[:20]
                iface_ids[(original, original_iface)] = iface['id']
        for connection in project['connections']:
            for side in ('from', 'to'):
                endpoint = connection[side]
                endpoint['i'] = iface_ids[(endpoint['n'], endpoint['i'])]
                endpoint['n'] = node_ids[endpoint['n']]
        project['studio'] = copy.deepcopy(old.get('studio', {}))
        project['studio'].setdefault('brief', {})
        project['studio'].setdefault('behavior', [])
        project['studio'].setdefault('deployment', {'units': []})
        modules = project['studio'].setdefault('modules', {})
        report_root = selected.parent if (selected / 'package.xml').is_file() else selected
        mapping = {p['package']: p for p in packages['packages']}
        for node in project['nodes']:
            record = mapping.get(node['pkg'])
            paths = []
            if record:
                package_path = (report_root / record['path']).relative_to(snapshot).as_posix()
                paths.append(package_path)
                for found in record.get('nodes', []):
                    if found['node'] == node['node'] or found['artifact'] == node['artifact']:
                        for fact in found.get('interfaces', []) + found.get('parameters', []):
                            rel = (report_root / fact['at'].rsplit(':', 1)[0]).relative_to(snapshot).as_posix()
                            if rel not in paths:
                                paths.append(rel)
            modules[node['id']] = {**modules.get(node['id'], {}), 'role': modules.get(node['id'], {}).get('role', 'Unassigned'),
                                   'unit': modules.get(node['id'], {}).get('unit', 'unassigned'), 'sourcePaths': paths}
        retained = {n['id'] for n in project['nodes']}
        for unit in project['studio']['deployment'].get('units', []):
            removed = [ident for ident in unit.get('nodeIds', []) if ident not in retained]
            if removed:
                unit['orphanedNodeIds'] = sorted(set(unit.get('orphanedNodeIds', []) + removed))
                unit['nodeIds'] = [ident for ident in unit['nodeIds'] if ident in retained]
        project['seededFrom'] = 'models/' + project['system']['name'] + '.rossystem'

    def validate_plan(self, root, ident, *, purpose='import'):
        """Validate the frozen plan's intended use and its actual extraction inputs."""
        root = self._root(root)
        if not isinstance(ident, str) or not re.fullmatch('[a-f0-9]{32}', ident):
            raise SourceError('source_plan', 'Invalid source preview ID.')
        record = _json(self.storage / ident / 'plan.json')
        if not record or record['sourceRoot'] != str(root):
            raise SourceError('source_plan', 'Preview belongs to another workspace or no longer exists.', 404)
        if time.time() - record['createdAt'] > 3600:
            raise SourceError('source_plan_expired', 'Source preview expired; scan again.', 409)
        if record.get('purpose', 'import') != purpose:
            raise SourceError('source_plan_purpose', 'This preview was created for a different action. Extract again for catalogue publication or system import.', 409)
        if purpose == 'import' and ('baseProjectHash' not in record or _project_hash(root) != record['baseProjectHash']):
            raise SourceError('source_model_changed', 'The model changed since preview; scan again before importing.', 409)
        if self._current_hashes(root, record['sourcePath']) != record['inputHashes']:
            raise SourceError('source_changed', 'Source changed since preview; extract again before using these contracts.', 409)
        if not record['canImport']:
            raise SourceError('source_invalid', 'The extracted model has errors or no nodes; review extraction diagnostics.', 422)
        return record

    def import_plan(self, root, request):
        record = self.validate_plan(root, request.get('planId'))
        selected = request.get('acceptedConnections', [])
        available = {c['id']: c for c in record['candidateConnections']}
        if not isinstance(selected, list) or any(not isinstance(i, str) or i not in available for i in selected):
            raise SourceError('source_connections', 'Choose connections from this preview.')
        for ident in dict.fromkeys(selected):
            candidate = available[ident]
            if not candidate.get('fromEndpoint') or not candidate.get('toEndpoint'):
                raise SourceError('source_connections', 'A candidate endpoint could not be resolved.', 422)
            record['project']['connections'].append({'id': 'import_' + ident, 'from': candidate['fromEndpoint'], 'to': candidate['toEndpoint']})
            candidate['accepted'] = True
        # The caller owns the revision-guarded save and generation review. These extracted
        # files stay untouched; selected connections are only an explicit graph proposal.
        record['requiresGenerationReview'] = bool(selected)
        return record

    apply = import_plan
