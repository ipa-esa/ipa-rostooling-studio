"""Linking and fetching ROS source repositories for architecture extraction.

This is the subset of what used to live in the deployment module that Architecture's
"Add repository & extract components" actually needs: knowing which repositories are
linked to a project, and letting a user point at a Git URL or an existing local
checkout (Git or not -- see locate_repository) to add one. It does not build, start,
stop or containerize anything -- that is deployment's job, and deployment is a
separate, confidential tool, not part of this one.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import uuid

import yaml
from studio_runtime import RuntimeFailure, UNIT_RE, _end_process_group

CATALOGUE = [
    {'id':'ur-driver','name':'Universal Robots ROS 2 driver','url':'https://github.com/UniversalRobots/Universal_Robots_ROS2_Driver.git','suggestedName':'ur_robot_driver','scope':'Official robot driver; choose the branch matching your ROS distribution.'},
    {'id':'ur-description','name':'Universal Robots descriptions','url':'https://github.com/UniversalRobots/Universal_Robots_ROS2_Description.git','suggestedName':'ur_description','scope':'Robot geometry and descriptions.'},
    {'id':'moveit','name':'MoveIt 2','url':'https://github.com/moveit/moveit2.git','suggestedName':'moveit2','scope':'Motion planning source; dependencies require a suitable image.'},
    {'id':'rviz','name':'RViz','url':'https://github.com/ros2/rviz.git','suggestedName':'rviz','scope':'Desktop visualization source; GUI support requires explicit configuration.'},
    {'id':'ros2-control','name':'ros2_control','url':'https://github.com/ros-controls/ros2_control.git','suggestedName':'ros2_control','scope':'Hardware interfaces and controllers.'},
    {'id':'ros2','name':'ROS 2 workspace recipes','url':'https://github.com/ros2/ros2.git','suggestedName':'ros2','scope':'Upstream workspace manifests; cloning does not fetch every dependency.'},
]


def _hash(data): return hashlib.sha256(data).hexdigest()


def _write(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
    try:
        with temp.open('wb') as stream:
            stream.write(value); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if temp.exists(): temp.unlink()


def _inside(root,relative):
    if not isinstance(relative,str) or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise RuntimeFailure('invalid_path','Expected a relative project path.',403)
    result=(root/relative).resolve()
    if not result.is_relative_to(root): raise RuntimeFailure('invalid_path','Path escapes the project.',403)
    return result


class StudioRepositories:
    def __init__(self,runtime):
        # No longer requires storage under /media/HDD (a leftover assumption from one
        # development machine's disk layout, not a real constraint): it made the server refuse
        # to even start -- an unhandled exception in __init__, before any request-time error
        # handling exists to catch it -- on any machine without that exact mount. The operator
        # already chooses where storage lives via --storage-root; trust that choice.
        self.runtime=runtime; self.storage=Path(runtime.storage_root).resolve()
        self._locks={}; self._guard=threading.Lock()
    def _lock(self,root):
        with self._guard: return self._locks.setdefault(str(root),threading.RLock())
    def _root(self,path):
        root=Path(path).resolve()
        if not root.is_dir(): raise RuntimeFailure('missing_project','Project directory is missing.',404)
        return root
    def _run(self,argv,cwd=None,timeout=45):
        # start_new_session=True + _end_process_group, the same treatment studio_runtime gives its
        # agent jobs: git delegates the real work to helper processes (git-submodule--helper,
        # git-remote-https), and subprocess.run's timeout only kills the git process it launched --
        # the helpers keep writing into .git/modules long after we've reported failure, where they
        # collide with the user's retry. Killing the group keeps a timed-out fetch or submodule
        # init safely retryable instead of leaving a half-written module to trip over.
        try: proc=subprocess.Popen(argv,cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,shell=False,start_new_session=True)
        except OSError as exc: raise RuntimeFailure('command_failed',str(exc),503) from exc
        try: out,err=proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _end_process_group(proc)
            try: proc.communicate(timeout=5)
            except subprocess.TimeoutExpired: pass
            raise RuntimeFailure('command_failed',str(exc),503) from exc
        return {'exitCode':proc.returncode,'stdout':out[-131072:],'stderr':err[-65536:],'ok':proc.returncode==0}

    def repositories(self,project_root):
        root=self._root(project_root); path=_inside(root,'repositories.repos'); records={}
        if path.exists():
            try: document=yaml.safe_load(path.read_text()) or {}; records=document.get('repositories',{})
            except (yaml.YAMLError,AttributeError): raise RuntimeFailure('invalid_repositories','repositories.repos is not a repository manifest.',422)
        if not isinstance(records,dict): raise RuntimeFailure('invalid_repositories','Expected a repositories mapping.',422)
        out=[]
        for rel,entry in records.items():
            if not isinstance(entry,dict): raise RuntimeFailure('invalid_repositories','Repository entries must be mappings.',422)
            try: target=self.resolve_repository_path(str(root),rel)
            except RuntimeFailure as exc:
                if exc.code!='mapping_untrusted': raise
                out.append({'path':rel,'url':entry.get('url'),'revision':entry.get('version'),'exists':False,'type':entry.get('type'),'localPath':str(_inside(root,rel)),'diagnostic':str(exc),'requiresLocate':True}); continue
            state=self._git_state(target) if target.is_dir() else {}
            # A repository that simply isn't a Git checkout (entry['type']=='local') is a
            # normal, expected state, not an error -- drop it rather than leak an unused,
            # misleading 'error' field into every listed local repository.
            state.pop('error',None)
            out.append({'path':rel,'url':entry.get('url'),'revision':entry.get('version'),'exists':target.is_dir(),'type':entry.get('type'),'localPath':str(target),**state})
        return {'repositories':out,'catalogue':CATALOGUE,'manifestPath':'repositories.repos','sourceRoot':str(_inside(root,'src'))}
    def _git_state(self,path):
        git=shutil.which('git')
        if not git: return {'error':'Git is unavailable.'}
        common=[git,'-c','core.hooksPath=/dev/null','-C',str(path)]
        commit=self._run(common+['rev-parse','HEAD'])
        if not commit['ok']: return {'error':'This folder is not a Git checkout.'}
        branch=self._run(common+['symbolic-ref','--short','-q','HEAD'])
        status=self._run(common+['status','--porcelain','--untracked-files=normal'])
        return {'currentCommit':commit['stdout'].strip(),'branch':branch['stdout'].strip() or '(detached)','dirty':bool(status['stdout'].strip())}
    def _mapping_receipt(self,root,prefix):
        return self.storage/'repository-authorizations'/(_hash((str(root)+'\0'+prefix).encode())+'.json')
    def _mapping_authorized(self,root,prefix,target):
        path=self._mapping_receipt(root,prefix)
        try: return json.loads(path.read_text())=={'root':str(root),'prefix':prefix,'path':str(target)}
        except (OSError,ValueError): return False
    def resolve_repository_path(self,project_root,relative):
        root=self._root(project_root); default=_inside(root,relative)
        settings_path=_inside(root,'.studio/local.json')
        settings=json.loads(settings_path.read_text()) if settings_path.is_file() else {}
        mappings=settings.get('repositories',{})
        if not isinstance(mappings,dict): raise RuntimeFailure('invalid_mappings','Local repository mappings must be an object.',422)
        for prefix,record in mappings.items():
            _inside(root,prefix)
            if not isinstance(record,dict) or not isinstance(record.get('path'),str): raise RuntimeFailure('invalid_mappings','Invalid local repository path.',422)
            if relative==prefix or relative.startswith(prefix+'/'):
                base=Path(record['path']).resolve()
                if not self._mapping_authorized(root,prefix,base): raise RuntimeFailure('mapping_untrusted','Locate this checkout explicitly on this machine before using its imported local pointer.',403)
                suffix=relative[len(prefix):].lstrip('/'); actual=(base/suffix).resolve()
                if not actual.is_relative_to(base): raise RuntimeFailure('invalid_path','Repository artifact escapes its mapped checkout.',403)
                return actual
        return default
    def locate_repository(self,project_root,request):
        root=self._root(project_root); name=request.get('name'); value=request.get('path')
        if not name and isinstance(value, str):
            raw = Path(value).name
            derived = re.sub(r'[^a-z0-9_-]+', '_', raw.lower()).strip('_')
            if derived and not derived[0].isalpha(): derived = 'repo_' + derived
            name = (derived or 'repo')[:48]
        if not isinstance(name,str) or not UNIT_RE.fullmatch(name): raise RuntimeFailure('invalid_repository','Choose a lower-case repository name.')
        if not isinstance(value,str) or not Path(value).is_absolute(): raise RuntimeFailure('invalid_path','Locate requires an absolute checkout directory.')
        target=Path(value).resolve()
        if not target.is_dir(): raise RuntimeFailure('missing_repository','Selected checkout does not exist.',404)
        # A checkout is not required to be a Git repository. Testbeds that only hand over a
        # plain directory -- or a Docker image with no accompanying repo at all -- still need to
        # link and extract from it (this was reported as the most important gap for testbed
        # onboarding). Git state and an origin URL are recorded when they exist, purely as
        # provenance metadata; they are never a gate on whether linking is allowed.
        state=self._git_state(target)
        url=None
        if 'error' not in state:
            git=shutil.which('git')
            remote=self._run([git,'-c','core.hooksPath=/dev/null','-C',str(target),'remote','get-url','origin'])
            if remote['ok']:
                candidate=remote['stdout'].strip()
                # Linking an existing local checkout only records its origin for provenance; it
                # is never fetched from, so an SSH remote (the common case for private robotics
                # repos) is as safe here as HTTPS. Cloning a fresh checkout stays HTTPS-only
                # below. An origin with embedded credentials or an unrecognised scheme is simply
                # left unrecorded -- linking still proceeds without it.
                if re.fullmatch(r'https://[A-Za-z0-9.-]+/[A-Za-z0-9_./-]+(?:\.git)?',candidate) or re.fullmatch(r'git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+\.git',candidate) or re.fullmatch(r'ssh://[A-Za-z0-9._-]+@[A-Za-z0-9.-]+(?::\d+)?/[A-Za-z0-9_./-]+(?:\.git)?',candidate):
                    url=candidate
        else:
            state={}
        relative='src/vendor/'+name
        with self._lock(root):
            manifest=_inside(root,'repositories.repos'); local=_inside(root,'.studio/local.json')
            document=yaml.safe_load(manifest.read_text()) if manifest.exists() else {'repositories':{}}
            settings=json.loads(local.read_text()) if local.exists() else {}
            if not isinstance(document,dict) or not isinstance(document.get('repositories'),dict) or not isinstance(settings,dict): raise RuntimeFailure('invalid_repositories','Repository metadata is malformed.',422)
            mappings=settings.setdefault('repositories',{})
            if not isinstance(mappings,dict): raise RuntimeFailure('invalid_mappings','Local repository mappings are malformed.',422)
            already = relative in document['repositories'] or relative in mappings or _inside(root,relative).exists()
            reauthorize = (mappings.get(relative,{}).get('path')==str(target) and not self._mapping_authorized(root,relative,target) and not _inside(root,relative).exists())
            if already and not reauthorize: raise RuntimeFailure('repository_exists','This repository name is already in use.',409)
            _write(self._mapping_receipt(root,relative),json.dumps({'root':str(root),'prefix':relative,'path':str(target)}).encode())
            document['repositories'][relative]={'type':'git' if state.get('currentCommit') else 'local','url':url,'version':state.get('currentCommit')}
            mappings[relative]={'path':str(target)}
            # Store the local pointer first; a failed manifest write leaves a
            # recoverable unused mapping, never overwrites or moves source.
            _write(local,json.dumps(settings,indent=2).encode())
            _write(manifest,yaml.safe_dump(document,sort_keys=True).encode())
        return {'repository':{'path':relative,'localPath':str(target),'url':url,'revision':state.get('currentCommit'),**state},'manifestPath':'repositories.repos','message':'Existing checkout linked. Its files were not moved or overwritten.'}
    def fetch_repository(self,project_root,request):
        if request.get('operation')=='locate': return self.locate_repository(project_root,request)
        root=self._root(project_root)
        name=request.get('name'); url=request.get('url'); revision=request.get('revision') or request.get('branch') or 'HEAD'
        if not name and isinstance(url, str):
            raw = url.rstrip('/').split('/')[-1]
            if raw.endswith('.git'): raw = raw[:-4]
            derived = re.sub(r'[^a-z0-9_-]+', '_', raw.lower()).strip('_')
            if derived and not derived[0].isalpha(): derived = 'repo_' + derived
            name = (derived or 'repo')[:48]
        if not isinstance(name,str) or not UNIT_RE.fullmatch(name): raise RuntimeFailure('invalid_repository','Repository name must use lower-case letters, digits, underscores or hyphens.')
        if not isinstance(url,str) or not re.fullmatch(r'https://[A-Za-z0-9.-]+/[A-Za-z0-9_./-]+(?:\.git)?',url) or '..' in url.split('/'): raise RuntimeFailure('invalid_url','Use an HTTPS repository URL without credentials, query parameters or shell syntax.')
        if not isinstance(revision,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]{0,200}',revision) or '..' in revision: raise RuntimeFailure('invalid_revision','Choose a branch, tag or commit ID.')
        git=shutil.which('git')
        if not git: raise RuntimeFailure('git_unavailable','Git is unavailable.',503)
        with self._lock(root):
            relative='src/vendor/'+name; destination=_inside(root,relative)
            if destination.exists(): raise RuntimeFailure('repository_exists','This checkout already exists; it was not replaced.',409)
            manifest=_inside(root,'repositories.repos'); previous=manifest.read_bytes() if manifest.exists() else None
            document=yaml.safe_load(previous) if previous else {'repositories':{}}
            if not isinstance(document,dict) or not isinstance(document.get('repositories'),dict): raise RuntimeFailure('invalid_repositories','Existing repository manifest is invalid.',422)
            if relative in document['repositories']: raise RuntimeFailure('repository_exists','This repository already has a manifest entry.',409)
            destination.parent.mkdir(parents=True,exist_ok=True)
            stage=destination.with_name('.fetch-'+uuid.uuid4().hex)
            common=[git,'-c','core.hooksPath=/dev/null','-c','protocol.file.allow=never']
            try:
                for argv in [common+['init',str(stage)],common+['-C',str(stage),'remote','add','origin',url],common+['-C',str(stage),'fetch','--depth','1','origin',revision],common+['-C',str(stage),'checkout','--detach','FETCH_HEAD']]:
                    result=self._run(argv,timeout=300)
                    if not result['ok']: raise RuntimeFailure('fetch_failed',result['stderr'] or result['stdout'],422)
                pinned=self._run(common+['-C',str(stage),'rev-parse','HEAD'])['stdout'].strip()
                if not re.fullmatch('[a-f0-9]{40,64}',pinned): raise RuntimeFailure('fetch_failed','Git did not resolve a commit ID.',422)
                # Submodules are part of what this repository actually IS at this commit, not a
                # separate opt-in fetch the way a *.repos sibling dependency is (that becomes its
                # own top-level, independently-named repository; a submodule does not) -- so a
                # plain "fetch this repository" already includes them, matching what `git clone
                # --recurse-submodules` would do. Best-effort: a submodule that fails to init
                # (private, network hiccup) does not lose the checkout that DID succeed, it's
                # just reported so the user knows to run "Initialize submodules" again later.
                submodule_note=None
                if (stage/'.gitmodules').is_file():
                    sub=self._run(common+['-C',str(stage),'submodule','update','--init','--recursive','--depth','1'],timeout=300)
                    if not sub['ok']: submodule_note='Submodules were not fully initialized: '+(sub['stderr'] or sub['stdout'])[-500:]
                current=manifest.read_bytes() if manifest.exists() else None
                if current!=previous: raise RuntimeFailure('manifest_changed','Repository manifest changed during fetch; no checkout was adopted.',409)
                document['repositories'][relative]={'type':'git','url':url,'version':pinned}
                os.rename(stage,destination)
                try: _write(manifest,yaml.safe_dump(document,sort_keys=True).encode())
                except Exception:
                    # Preserve successfully fetched code; report that manifest recovery is needed.
                    raise RuntimeFailure('manifest_write_failed','Checkout is retained at '+relative+'; writing its manifest failed. Retry the manifest update manually.',503)
            finally:
                if stage.exists(): shutil.rmtree(stage)
            message='Source checked out at a pinned commit. Dependencies were not installed and no build was started.'
            if submodule_note: message+=' '+submodule_note
            return {'repository':{'path':relative,'url':url,'revision':pinned,'requestedRevision':revision},'manifestPath':'repositories.repos','message':message}

    def repository_dependencies(self,project_root,relative):
        """Find *.repos manifests -- the standard vcstool format ROS workspaces already use to
        list a package's own sibling dependencies (see fetch_repository, which speaks the same
        format for a single explicit fetch) -- inside a linked repository, and report which of
        the repositories they name are already linked here and which are still missing.
        Fetching one repo (e.g. the UR ROS 2 driver) was never going to also fetch what ITS OWN
        manifest names as dependencies (ur_msgs, control_msgs, ...) -- nothing read this file
        before now, which is why those stayed missing even though the fetch itself was clean.
        """
        root=self._root(project_root)
        target=self.resolve_repository_path(str(root),relative)
        if not target.is_dir(): raise RuntimeFailure('missing_repository','Repository checkout not found.',404)
        existing=self.repositories(project_root)['repositories']
        existing_urls={r['url'] for r in existing if r.get('url')}
        manifests=[]
        for manifest_path in sorted(target.glob('*.repos')):
            try: document=yaml.safe_load(manifest_path.read_text())
            except (yaml.YAMLError,OSError): continue
            entries=document.get('repositories') if isinstance(document,dict) else None
            if not isinstance(entries,dict): continue
            deps=[]
            for name,spec in entries.items():
                if not isinstance(spec,dict) or not isinstance(spec.get('url'),str): continue
                # Exact-URL match only, deliberately: a fuzzy match (ignoring .git, http vs ssh)
                # risks calling something "already linked" that is not actually the same
                # checkout. A missed match just offers a fetch that then reports
                # 'repository_exists' -- annoying, never wrong; a false match would be silently
                # wrong instead.
                deps.append({'name':name,'url':spec['url'],'version':spec.get('version') or 'HEAD',
                             'linked':spec['url'] in existing_urls})
            if deps: manifests.append({'file':manifest_path.name,'dependencies':deps})
        # Submodules are a second, different way a repository declares "there's more to this
        # checkout than what I fetched" -- .gitmodules instead of a *.repos file, and unlike a
        # *.repos sibling, a submodule isn't a separate top-level repository, it's an
        # (uninitialized) part of THIS ONE. fetch_repository now initializes these automatically
        # for a fresh fetch; this covers the two cases it can't: a repo linked via "Existing
        # checkout" (never went through fetch_repository at all) and anything fetched before
        # that existed.
        submodules=[]
        if (target/'.gitmodules').is_file():
            git=shutil.which('git')
            if git:
                status=self._run([git,'-c','core.hooksPath=/dev/null','-C',str(target),'submodule','status'],timeout=30)
                for line in status['stdout'].splitlines():
                    line=line.rstrip()
                    if not line: continue
                    # `git submodule status` prefixes each line with one status character: '-'
                    # not initialized, '+' checked out at a different commit than recorded, 'U'
                    # merge conflicts, or a space when clean/up to date -- see git-submodule(1).
                    marker,rest=(line[0],line[1:]) if line[0] in '-+U' else (' ',line)
                    # The path is everything after the commit, NOT the second whitespace token: a
                    # submodule path may contain spaces (`git submodule add ../inner "my sub"` is
                    # legal and prints ` <sha> my sub (heads/master)`), and git appends an optional
                    # " (describe)" suffix only when the submodule is initialized. Splitting on
                    # spaces reported that one as "my", which both mislabels it in the list and
                    # collides with any sibling sharing its first word.
                    commit,_,remainder=rest.strip().partition(' ')
                    path=re.sub(r'\s+\([^()]*\)$','',remainder).strip()
                    if not path: continue
                    submodules.append({'path':path,'commit':commit,'initialized':marker!='-'})
        return {'manifests':manifests,'submodules':submodules}

    def init_submodules(self,project_root,relative,paths=None):
        root=self._root(project_root)
        target=self.resolve_repository_path(str(root),relative)
        if not target.is_dir(): raise RuntimeFailure('missing_repository','Repository checkout not found.',404)
        if paths is not None and (not isinstance(paths,list) or not paths or not all(isinstance(p,str) and p for p in paths)):
            raise RuntimeFailure('invalid_paths','paths must be a nonempty list of submodule paths, or omitted for all of them.')
        git=shutil.which('git')
        if not git: raise RuntimeFailure('git_unavailable','Git is unavailable.',503)
        with self._lock(target):
            argv=[git,'-c','core.hooksPath=/dev/null','-c','protocol.file.allow=never','-C',str(target),
                  'submodule','update','--init','--recursive','--depth','1']
            if paths: argv+=['--']+paths
            result=self._run(argv,timeout=300)
            if not result['ok']: raise RuntimeFailure('submodule_failed',(result['stderr'] or result['stdout'])[-2000:],422)
        return {'message':'Submodule(s) initialized.','output':result['stdout'][-4000:]}
