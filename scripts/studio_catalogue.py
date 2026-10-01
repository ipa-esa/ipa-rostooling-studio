"""Reviewed static ROS source contracts persisted as a local reusable catalogue.

No checkout, source program, launch file, dependency installer or ROS process is
executed here. StudioSource runs the existing static extractor scripts.
"""
import copy
import json
from pathlib import Path
import re
import threading

from studio_store import StudioError, atomic, digest, encode, fail, safe_path, stamp, uid
from studio_runtime import RuntimeFailure


class StudioCatalogue:
    def __init__(self, store, source, deployment):
        self.store, self.source, self.deployment = store, source, deployment
        self.directory = safe_path(store.storage, 'local-catalogue')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.mutex = threading.RLock()

    def _selection(self, pid, repository_id):
        root = self.store.root(pid)
        if repository_id in (None, 'workspace'):
            state = self.deployment._git_state(root) if self.deployment else {}
            return root, {'id': 'workspace', 'name': root.name, 'path': '.',
                          'localPath': str(root), 'url': None, **state}
        if not isinstance(repository_id, str):
            fail('Select a registered repository.')
        if not self.deployment:
            fail('Repository adapter is unavailable.', 'unavailable', 503)
        records = self.deployment.repositories(root)['repositories']
        match = [r for r in records if repository_id in (r['path'], r['path'].split('/')[-1])]
        if len(match) != 1 or not match[0]['exists']:
            fail('Choose an existing registered repository.', 'repository_missing', 404)
        repo = match[0]
        return Path(repo['localPath']), {**repo, 'id': repo['path'], 'name': repo['path'].split('/')[-1]}

    @staticmethod
    def _group_identity(repository, root):
        # One group per linked folder: several folders of one Git checkout share its URL, and
        # a URL-only identity let each publish replace the previous folder's current pointer.
        # The checkout root keeps the URL-only identity, so earlier catalogue groups still match.
        if not repository.get('url'):
            return str(root)
        top = next((p for p in [root, *root.parents] if (p / '.git').exists()), root)
        within = root.relative_to(top).as_posix()
        return repository['url'] if within == '.' else [repository['url'], within]

    def inspect(self, pid, request=None):
        request = request or {}
        root, repository = self._selection(pid, request.get('repositoryId'))
        return {**self.source.inspect(root), 'repository': repository}

    def preview(self, pid, request):
        root, repository = self._selection(pid, request.get('repositoryId'))
        # A catalogue contains node types, not launch-specific deployment instances.
        name = re.sub(r'[^A-Za-z0-9_]', '_', repository['name'])[:80]
        name = name if name and (name[0].isalpha() or name[0] == '_') else 'repo_' + name
        source_request = {'systemName': name}
        if request.get('sourcePath'):
            source_request['sourcePath'] = request['sourcePath']
        converted = self.source.preview(root, source_request, purpose='catalogue')
        if not converted['canImport']:
            nodes = (converted['project'] or {}).get('nodes')
            defined = converted['extraction']['packages'].get('interface_packages') or {}
            known = sum(len(p['catalogue']) for p in defined.values())
            if not nodes and not converted['flags'] and known and known == sum(len(p['defined']) for p in defined.values()):
                fail('This package defines %d message/service/action type(s) and no nodes. All of them are already in the '
                     'built-in type catalogue, so there is nothing new to add: components can use them directly.' % known,
                     'catalogue_empty', 422, {'diagnostics': converted['diagnostics'], 'flags': [], 'interfacePackages': defined})
            fail('No valid reusable node contracts were extracted. Review diagnostics.' if nodes else
                 'No node contracts could be extracted: every ROS declaration found (%d) is not literal in the source and '
                 'needs review. See flags.' % len(converted['flags']) if converted['flags'] else
                 'No ROS nodes were found in this package: its code declares no publishers, subscribers, services, '
                 'actions or parameters. It may be a description, message or configuration-only package, which has '
                 'no node contracts to reuse.', 'catalogue_empty', 422,
                 {'diagnostics': converted['diagnostics'], 'flags': converted['flags']})
        group_id = digest(self._group_identity(repository, root))[:24]
        version = digest({'inputs': converted['inputHashes'], 'project': converted['project'],
                          'files': converted['files'], 'commit': repository.get('currentCommit')})[:24]
        entries = []
        project = converted['project']
        for node in project['nodes']:
            entry_key = digest([node['pkg'], node['node'], node.get('artifact')])[:24]
            # A flag the extractor attributed to one node stays with it; the rest are package-level.
            flags = [f for f in converted['flags'] if f.get('package') in (None, node['pkg'])
                     and f.get('artifact') in (None, node.get('artifact'))]
            entries.append({'id': ':'.join([group_id, version, entry_key]),
                            'package': node['pkg'], 'name': node['node'], 'artifact': node.get('artifact'),
                            'interfaceKinds': sorted({f['kind'] for f in node['ifaces']}),
                            'messageTypes': sorted({f['type'] for f in node['ifaces'] if f.get('type')}),
                            'node': copy.deepcopy(node), 'flags': flags,
                            'sourcePaths': project['studio']['modules'][node['id']].get('sourcePaths', []),
                            'coverage': {'mode': 'static-source', 'complete': not flags,
                                         'built': False, 'runtimeVerified': False}})
        plan = {'ok': True, 'planId': uid(), 'projectId': pid, 'sourcePlanId': converted['planId'],
                'groupId': group_id, 'version': version, 'repository': repository,
                'sourceRoot': str(root), 'coverage': converted['coverage'], 'flags': converted['flags'],
                'diagnostics': converted['diagnostics'], 'entries': entries, 'files': converted['files'],
                'types': project.get('types', {}), 'packages': project.get('packages', {}),
                'inputHashes': converted['inputHashes'], 'createdAt': stamp()}
        atomic(safe_path(self.directory, 'plans/' + plan['planId'] + '.json'), encode(plan))
        return plan

    def publish(self, pid, request):
        ident = request.get('planId')
        if not isinstance(ident, str) or not re.fullmatch('[a-f0-9]{32}', ident):
            fail('Select a catalogue conversion preview.')
        path = safe_path(self.directory, 'plans/' + ident + '.json')
        if not path.is_file():
            fail('Catalogue preview does not exist.', 'not_found', 404)
        plan = json.loads(path.read_text())
        if plan['projectId'] != pid:
            fail('This preview belongs to another project.', 'catalogue_scope', 403)
        root, repository = self._selection(pid, plan['repository']['id'])
        if str(root) != plan['sourceRoot'] or repository.get('currentCommit') != plan['repository'].get('currentCommit'):
            fail('The repository changed since conversion. Inspect it again.', 'catalogue_stale', 409)
        self.source.validate_plan(root, plan['sourcePlanId'], purpose='catalogue')
        group = {k: copy.deepcopy(plan[k]) for k in ('groupId', 'version', 'repository', 'coverage', 'flags',
                 'diagnostics', 'entries', 'types', 'packages', 'inputHashes', 'sourceRoot')}
        group['id'] = group.pop('groupId')
        group['publishedAt'] = stamp()
        result = {'ok': True, 'group': group}
        if not group['entries'] and group['types']:
            # A message-only package has no node to instantiate, which is how a group's types
            # otherwise reach the project: add them now, so the Data types tab and type picker
            # list them. Same rule as instantiate: never overwrite a different definition.
            with self.store.lock(pid):
                current = self.store.get_project(pid)
                project = copy.deepcopy(current['project'])
                for name, value in group['types'].items():
                    if name in project['types'] and project['types'][name] != value:
                        fail('The project already defines a different type: ' + name, 'catalogue_type_conflict', 409)
                    project['types'][name] = copy.deepcopy(value)
                if project['types'] != current['project']['types']:
                    result = {**self.store.save_project(pid, {'expectedRevision': current['revision'], 'project': project}), **result}
        with self.mutex:
            prefix = group['id'] + '/' + group['version'] + '/'
            manifest = safe_path(self.directory, prefix + 'manifest.json')
            if manifest.exists():
                group = json.loads(manifest.read_text())
                atomic(safe_path(self.directory, group['id'] + '/current.json'), encode({'version': group['version']}))
                return {**result, 'group': group}
            group['contractFiles'] = []
            for item in plan['files']:
                rel = prefix + item['path']
                atomic(safe_path(self.directory, rel), item['content'].encode())
                group['contractFiles'].append({'path': rel, 'hash': digest(item['content'].encode())})
            atomic(safe_path(self.directory, prefix + 'manifest.json'), encode(group))
            # Publishing this final pointer makes a complete immutable version visible.
            atomic(safe_path(self.directory, group['id'] + '/current.json'), encode({'version': group['version']}))
        return result

    def list(self, pid, request=None):
        self.store.root(pid)
        request = request or {}
        groups = []
        with self.mutex:
            for folder in sorted(self.directory.iterdir()):
                if not re.fullmatch('[a-f0-9]{24}', folder.name):
                    continue
                pointer = safe_path(self.directory, folder.name + '/current.json')
                if not pointer.is_file():
                    continue
                version = json.loads(pointer.read_text())['version']
                if not re.fullmatch('[a-f0-9]{24}', version):
                    fail('Invalid catalogue version.', 'catalogue_invalid', 422)
                group = json.loads(safe_path(self.directory, folder.name + '/' + version + '/manifest.json').read_text())
                groups.append(group)
        # A folder published before groups were per-folder may also sit in its checkout's
        # URL-keyed group; show only the newest publication of each source folder.
        newest = {}
        for group in groups:
            key = group.get('sourceRoot') or group['id']
            if key not in newest or group.get('publishedAt', '') > newest[key].get('publishedAt', ''):
                newest[key] = group
        query = str(request.get('query', '')).lower()
        kind, message = request.get('kind'), request.get('messageType')
        groups = [g for g in groups if newest[g.get('sourceRoot') or g['id']] is g]
        for group in groups:
            group['entries'] = [e for e in group['entries'] if (not query or query in json.dumps([e['package'], e['name'], e['messageTypes']]).lower())
                                and (not kind or kind in e['interfaceKinds'])
                                and (not message or message in e['messageTypes'])]
        groups = [g for g in groups if g['entries']]
        entries = [{**e, 'repositoryName': g['repository']['name'], 'repositoryId': g['repository']['id'],
                    'groupId': g['id'], 'version': g['version']} for g in groups for e in g['entries']]
        return {'ok': True, 'groups': groups, 'templates': entries, 'filters': {
                'interfaceKinds': sorted({k for e in entries for k in e['interfaceKinds']}),
                'messageTypes': sorted({t for e in entries for t in e['messageTypes']})}}

    def instantiate(self, pid, request):
        ident = request.get('entryId')
        if not isinstance(ident, str) or not re.fullmatch('[a-f0-9]{24}:[a-f0-9]{24}:[a-f0-9]{24}', ident):
            fail('Select a local catalogue contract.')
        group_id, version, _ = ident.split(':')
        path = safe_path(self.directory, group_id + '/' + version + '/manifest.json')
        if not path.is_file():
            fail('Catalogue version is missing.', 'not_found', 404)
        group = json.loads(path.read_text())
        entry = next((e for e in group['entries'] if e['id'] == ident), None)
        if not entry:
            fail('Catalogue contract is missing.', 'not_found', 404)
        with self.store.lock(pid):
            current = self.store.get_project(pid)
            if request.get('expectedRevision') != current['revision']:
                fail('Project changed before this node was added.', 'revision_conflict', 409)
            project = copy.deepcopy(current['project'])
            node = copy.deepcopy(entry['node'])
            node['id'] = uid()
            base = request.get('label', entry['name'])
            if not isinstance(base, str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', base):
                fail('Node instance name must use letters, numbers and underscores.')
            labels = {n['label'] for n in project['nodes']}
            label, suffix = base, 2
            while label in labels:
                label, suffix = base + '_' + str(suffix), suffix + 1
            node['label'] = label
            namespace = request.get('namespace')
            if namespace is not None and not isinstance(namespace, str):
                fail('Namespace must be text.')
            node['namespace'] = namespace
            node['backing'], node['catalogueFile'] = 'hand', None
            position = request.get('position') or {}
            for key, default in [('x', 50 + len(project['nodes']) % 3 * 300), ('y', 70 + len(project['nodes']) // 3 * 260)]:
                value = position.get(key, default) if isinstance(position, dict) else default
                if type(value) not in (float, int) or not -100000 <= value <= 100000:
                    fail('Node position is invalid.')
                node[key] = value
            for item in node.get('ifaces', []) + node.get('params', []):
                item['id'] = uid()
            contract = lambda n: {
                'interfaces': sorted((f['kind'], f['name'], f.get('type'), json.dumps(f.get('qos') or None, sort_keys=True))
                                     for f in n.get('ifaces', [])),
                'parameters': sorted((p['name'], p.get('ptype'), json.dumps(p.get('value'), sort_keys=True))
                                     for p in n.get('params', []))}
            for existing in project['nodes']:
                if (existing['pkg'], existing.get('artifact') or existing['node']) == (node['pkg'], node.get('artifact') or node['node']) and contract(existing) != contract(node):
                    fail("Component '%s' has edited the shared contract %s.%s. All instances use one generated declaration. Restore that component's interfaces, QoS and parameter defaults to add this catalogue version, or create a separately named component/package for a different contract. Instance names, namespaces and parameter overrides may differ." %
                         (existing['label'], node['pkg'], node.get('artifact') or node['node']), 'catalogue_node_conflict', 409)
            for name, value in group['types'].items():
                if name in project['types'] and project['types'][name] != value:
                    fail('The project already defines a different type: ' + name, 'catalogue_type_conflict', 409)
                project['types'][name] = copy.deepcopy(value)
            for name, value in group['packages'].items():
                project['packages'].setdefault(name, copy.deepcopy(value))
            project['nodes'].append(node)
            source_paths = []
            try:
                linked_root, linked_repo = self._selection(pid, group['repository']['id'])
                if str(linked_root) == group['sourceRoot']:
                    prefix = '' if linked_repo['id'] == 'workspace' else linked_repo['path'] + '/'
                    source_paths = [prefix + p for p in entry['sourcePaths']]
            except (StudioError, RuntimeFailure):
                # A reusable contract remains usable without its original checkout.
                source_paths = []
            project['studio']['modules'][node['id']] = {
                'role': 'Unassigned', 'unit': 'unassigned', 'sourcePaths': source_paths,
                'catalogue': {'entryId': ident, 'repository': {k: v for k, v in group['repository'].items() if k != 'localPath'},
                              'version': version, 'coverage': entry['coverage'], 'flags': entry['flags']}}
            result = self.store.save_project(pid, {'expectedRevision': current['revision'], 'project': project})
            result['instantiatedNodeId'] = node['id']
            return result
