"""Durable local Studio workspace. Existing ros_studio owns the model language."""
import copy, difflib, hashlib, json, math, os, re, threading, time, uuid
from pathlib import Path
import ros_studio as adapter

MAX_BYTES = 8 * 1024 * 1024
IGNORED = {'.git','.studio','.work','build','install','log','node_modules','__pycache__'}

def uid(): return uuid.uuid4().hex

def stamp(): return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

def digest(value):
    if not isinstance(value, bytes): value = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(value).hexdigest()

def encode(value): return json.dumps(value, indent=2, ensure_ascii=False).encode() + b'\n'

class StudioError(Exception):
    def __init__(self, status, code, message, details=None):
        super().__init__(message); self.status=status; self.code=code; self.message=message; self.details=details or {}

def fail(message, code='schema', status=400, details=None): raise StudioError(status,code,message,details)

def atomic(path, data):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name('.'+path.name+'.'+uid()+'.tmp')
    try:
        with open(tmp,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if tmp.exists(): tmp.unlink()

def read_json(path, default=None):
    if not Path(path).exists(): return copy.deepcopy(default)
    with open(path,encoding='utf8') as f: return json.load(f)

def safe_path(root, rel):
    if not isinstance(rel,str) or not rel or Path(rel).is_absolute() or '..' in Path(rel).parts: fail('Expected a project-relative path.','path',403)
    root=Path(root).resolve(); out=(root/rel).resolve()
    if not out.is_relative_to(root): fail('Path escapes the project.','path',403)
    return out

def schema(p):
    if not isinstance(p,dict) or p.get('formatVersion')!=5: fail('Expected formatVersion 5 project.')
    if len(encode(p))>MAX_BYTES: fail('Project is too large.','size',413)
    def check(v,t,path):
        if not isinstance(v,t): fail('Invalid '+path)
    def tree(v,depth=0):
        if depth>50: fail('Project nesting is too deep.')
        if isinstance(v,float) and not math.isfinite(v): fail('Non-finite numbers are invalid.')
        if isinstance(v,dict):
            for k,item in v.items():
                if not isinstance(k,str): fail('Object keys must be strings.')
                tree(item,depth+1)
        elif isinstance(v,list):
            for item in v: tree(item,depth+1)
        elif not isinstance(v,(str,int,float,bool,type(None))): fail('Unsupported JSON value.')
    tree(p)
    for key in ['system','packages','types']: check(p.get(key),dict,key)
    check(p['system'].get('name'),str,'system.name')
    for key in ['nodes','connections','params','subSystems']: check(p.get(key),list,key)
    ids=set(); endpoints={}
    for n in p['nodes']:
        check(n,dict,'node')
        for k in ['id','label','backing','pkg','node']: check(n.get(k),str,'node.'+k)
        if not n['id'] or n['id'] in ids: fail('Duplicate or empty node ID.')
        ids.add(n['id']); endpoints[n['id']]=set()
        if n['backing'] not in ['hand','cat','sub']: fail('Invalid node backing.')
        for k in ['x','y']:
            if k in n: check(n[k],(int,float),'node.'+k)
        for key in ['ifaces','params']: check(n.get(key),list,'node.'+key)
        for f in n['ifaces']:
            check(f,dict,'interface')
            for key in ['id','name','kind']: check(f.get(key),str,'interface.'+key)
            if f['id'] in endpoints[n['id']] or not f['id']: fail('Duplicate or empty interface ID.')
            endpoints[n['id']].add(f['id'])
            if f['kind'] not in ['pub','sub','ss','sc','as','ac']: fail('Invalid interface kind.')
            if f.get('type') is not None: check(f['type'],str,'interface.type')
            if f.get('qos') is not None: check(f['qos'],dict,'interface.qos')
        param_ids=set()
        for param in n['params']:
            check(param,dict,'node parameter')
            for k in ['id','name']: check(param.get(k),str,'parameter.'+k)
            if param['id'] in param_ids: fail('Duplicate parameter ID.')
            param_ids.add(param['id'])
            for k in ['ptype','label']:
                if param.get(k) is not None: check(param[k],str,'parameter.'+k)
    cids=set()
    for c in p['connections']:
        check(c,dict,'connection'); check(c.get('id'),str,'connection.id')
        if c['id'] in cids: fail('Duplicate connection ID.')
        cids.add(c['id'])
        for k in ['from','to']:
            check(c.get(k),dict,'connection.'+k); end=c[k]
            if end.get('n') not in endpoints or end.get('i') not in endpoints[end['n']]: fail('Connection points to a missing interface.')
    for v in p['params']: check(v,dict,'parameter')
    for k,v in p['packages'].items(): check(v,dict,'package '+k)
    for k,v in p['types'].items():
        check(v,dict,'type '+k); check(v.get('fields',{}),dict,'type fields')
        for part,fields in v.get('fields',{}).items():
            check(fields,list,'message fields')
            for field in fields:
                check(field,dict,'message field')
                for key in ['type','name']: check(field.get(key),str,'message field.'+key)
    for sub in p['subSystems']:
        if not isinstance(sub,(str,dict)): fail('Invalid subsystem reference.')
    s=p.get('studio',{}); check(s,dict,'studio')
    check(s.get('brief',{}),dict,'brief')
    for k,v in s.get('brief',{}).items(): check(v,str,'brief.'+k)
    check(s.get('behavior',[]),list,'behavior'); bids=set()
    for step in s.get('behavior',[]):
        check(step,dict,'behavior step')
        for k,v in step.items():
            if k!='failures': check(v,str,'behavior.'+k)
        failures=step.get('failures',[]); check(failures,list,'behavior.failures'); failure_ids=set()
        for failure in failures:
            check(failure,dict,'behavior failure')
            for key,value in failure.items():
                if key!='recoveries': check(value,str,'failure.'+key)
            if not failure.get('id') or failure['id'] in failure_ids: fail('Failure IDs must be unique within their behavior step.')
            failure_ids.add(failure['id']); check(failure.get('condition',''),str,'failure.condition')
            recoveries=failure.get('recoveries',[]); check(recoveries,list,'failure.recoveries'); recovery_ids=set()
            for recovery in recoveries:
                check(recovery,dict,'failure recovery')
                for key,value in recovery.items(): check(value,str,'recovery.'+key)
                if not recovery.get('id') or recovery['id'] in recovery_ids: fail('Recovery IDs must be unique within their failure.')
                recovery_ids.add(recovery['id']); check(recovery.get('action',''),str,'recovery.action')
        if not step.get('id') or step['id'] in bids: fail('Behavior IDs must be unique.')
        bids.add(step['id'])
    check(s.get('modules',{}),dict,'modules')
    for mid,m in s.get('modules',{}).items():
        check(m,dict,'module')
        for k in ['role','unit']:
            if k in m: check(m[k],str,'module.'+k)
        check(m.get('sourcePaths',[]),list,'sourcePaths')
        for path in m.get('sourcePaths',[]):
            check(path,str,'source path')
            if Path(path).is_absolute() or '..' in Path(path).parts: fail('Source paths must be relative.')
    dep=s.get('deployment',{}); check(dep,dict,'deployment'); check(dep.get('units',[]),list,'units')
    uids=set()
    for unit in dep.get('units',[]):
        check(unit,dict,'unit'); check(unit.get('id'),str,'unit.id')
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}',unit['id']) or unit['id'] in uids: fail('Unit IDs must be safe and unique.')
        uids.add(unit['id'])
        for k in ['image','name','profile']:
            if k in unit: check(unit[k],str,'unit.'+k)
        if 'nodeIds' in unit:
            check(unit['nodeIds'],list,'unit.nodeIds')
            for nid in unit['nodeIds']:
                if nid not in ids: fail('Unit references a missing node.')
    return p

def engineering(p):
    p=copy.deepcopy(p); p.pop('diagnostics',None)
    for n in p.get('nodes',[]):
        n.pop('x',None); n.pop('y',None)
    s=p.get('studio',{})
    for k in ['session','modelState','baseModelHashes','draftBaseRevision','projectId','modelPaths','schemaVersion']: s.pop(k,None)
    return p

def model_identity(p):
    p=engineering(p); p.pop('studio',None); return digest(p)

def differences(a,b,path=''):
    out=[]
    if isinstance(a,dict) and isinstance(b,dict):
        for k in sorted(set(a)|set(b)): out.extend(differences(a.get(k),b.get(k),path+'.'+k if path else k))
    elif isinstance(a,list) and isinstance(b,list):
        for i in range(max(len(a),len(b))): out.extend(differences(a[i] if i<len(a) else None,b[i] if i<len(b) else None,f'{path}[{i}]'))
    elif a!=b: out.append({'path':path,'before':a,'after':b})
    return out

class StudioStore:
    def __init__(self,storage_root,repo_root):
        self.storage=Path(storage_root).resolve(); self.repo=Path(repo_root).resolve()
        (self.storage/'studio').mkdir(parents=True,exist_ok=True)
        self.registry_file=self.storage/'studio/registry.json'; self.registry=read_json(self.registry_file,{})
        self.locks={}; self.guard=threading.RLock(); self.plans={}; self.runtime=None; self.recovery_ready=set(); self.observation_coverage={}; self.observation_hashes={}
    def lock(self,pid):
        with self.guard: return self.locks.setdefault(pid,threading.RLock())
    def root(self,pid):
        if pid not in self.registry: fail('Unknown project.','not_found',404)
        root=Path(self.registry[pid]['root']); self._metadata(root); return root
    def _metadata(self,root):
        meta=Path(root)/'.studio'
        if meta.is_symlink(): fail('Project metadata must not be a symlink.','path',403)
        if meta.exists():
            for base,dirs,files in os.walk(meta,followlinks=False):
                for name in dirs+files:
                    if (Path(base)/name).is_symlink(): fail('Project metadata contains a symlink.','path',403)
                # Avoid enumerating historical revision/journal files on hot paths.
                # Recovery validates each requested historical path before reading it.
                if Path(base)==meta and 'recovery' in dirs:
                    for name in ('pending','transactions'):
                        if (meta/'recovery'/name).is_symlink():fail('Project metadata contains a symlink.','path',403)
                    dirs.remove('recovery')
    def state(self,root): return read_json(root/'.studio/state.json',{})
    def _hashes(self,root,paths): return {p:digest(safe_path(root,p).read_bytes()) if safe_path(root,p).is_file() else None for p in paths}
    def _recovery_dirs(self,root):
        pending=safe_path(root,'.studio/recovery/pending'); archive=safe_path(root,'.studio/recovery/transactions')
        with self.guard:
            if root not in self.recovery_ready:
                pending.mkdir(parents=True,exist_ok=True); archive.mkdir(parents=True,exist_ok=True)
                # Migrate the old journal once per service lifetime. Archived file bodies
                # are never parsed by the normal request/recovery path again.
                for old in (root/'.studio/recovery').glob('tx-*.json'):
                    old=safe_path(root,old.relative_to(root).as_posix()); tx=read_json(old)
                    os.replace(old,(archive if tx.get('status')=='committed' else pending)/old.name)
                self.recovery_ready.add(root)
        return pending,archive
    def _archive_transaction(self,root,manifest,tx):
        tx['status']='committed'; atomic(manifest,encode(tx))
        _,archive=self._recovery_dirs(root); os.replace(manifest,archive/manifest.name)
        for directory in (manifest.parent,archive):
            fd=os.open(directory,os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
    def _transaction(self,root,files,expected=None):
        self._metadata(root)
        tx={'id':uid(),'status':'pending','files':[]}
        for rel,data in files.items():
            path=safe_path(root,rel); before=path.read_bytes() if path.exists() else None
            if expected is not None and rel in expected and (digest(before) if before is not None else None)!=expected[rel]: fail('File changed before transaction.','hash_conflict',409,{'path':rel})
            tx['files'].append({'path':rel,'beforeHash':digest(before) if before is not None else None,'before':before.decode('utf8') if before is not None else None,'afterHash':digest(data),'after':data.decode('utf8')})
        pending,_=self._recovery_dirs(root); manifest=pending/('tx-'+tx['id']+'.json'); atomic(manifest,encode(tx))
        for f in tx['files']:
            target=safe_path(root,f['path']); current=digest(target.read_bytes()) if target.exists() else None
            if current not in [f['beforeHash'],f['afterHash']]: fail('External edit interrupted the transaction; recovery retains both versions.','recovery_conflict',409,{'path':f['path']})
            atomic(target,f['after'].encode())
        self._archive_transaction(root,manifest,tx)
    def _recover(self,root):
        self._metadata(root)
        pending,_=self._recovery_dirs(root)
        for manifest in sorted(pending.glob('tx-*.json')):
            manifest=safe_path(root,manifest.relative_to(root).as_posix()); tx=read_json(manifest)
            if tx['status']=='committed':
                self._archive_transaction(root,manifest,tx); continue
            for f in tx['files']:
                target=safe_path(root,f['path']); current=digest(target.read_bytes()) if target.exists() else None
                if current not in [f['beforeHash'],f['afterHash']]: fail('Recovery found an external edit; no files were overwritten.','recovery_conflict',409,{'path':f['path'],'transaction':tx['id']})
            for f in tx['files']:
                target=safe_path(root,f['path']); current=digest(target.read_bytes()) if target.exists() else None
                if current not in [f['beforeHash'],f['afterHash']]: fail('External edit interrupted recovery; no replacement of that file was made.','recovery_conflict',409,{'path':f['path']})
                atomic(target,f['after'].encode())
            self._archive_transaction(root,manifest,tx)
    def _commit(self,root,p,state,reason,extra=None,expected=None):
        expected=dict(expected or {})
        if state.get('projectHash') and 'project.json' not in expected: expected['project.json']=state['projectHash']
        state['observed']=dict(state.get('observed',{}))
        for rel,data in (extra or {}).items():
            if not rel.startswith(('.studio/','.work/')) and rel!='project.json': state['observed'][rel]=digest(data)
        schema(p); state['revision']=state.get('revision',0)+1; state['updatedAt']=stamp(); state['projectHash']=digest(encode(p)); state.pop('projectInvalid',None)
        snap={'revision':state['revision'],'time':stamp(),'reason':reason,'project':p}
        files={'.studio/recovery/rev-%08d.json'%state['revision']:encode(snap),'project.json':encode(p),'.studio/last-project.json':encode(p),'.studio/state.json':encode(state)}
        files.update(extra or {}); self._transaction(root,files,expected)
    def _project(self,root):
        if self.state(root).get('projectInvalid'): return read_json(root/'.studio/last-project.json')
        return read_json(root/'project.json')
    def _envelope(self,pid):
        root=self.root(pid); self._recover(root); s=self.state(root); p=self._project(root); schema(p)
        return {'ok':True,'projectId':pid,'root':str(root),'revision':s['revision'],'project':p,'session':read_json(root/'.studio/layouts.json',{}),'modelState':s.get('modelState','draft'),'modelHashes':self._hashes(root,s.get('modelPaths',[])),'diagnostics':s.get('diagnostics',[]),'sequence':s.get('sequence',0),'sourceCoverage':self.observation_coverage.get(str(root),{'complete':True,'fileCount':len(s.get('observed',{})),'diagnostics':[]})}
    def recent(self):
        with self.guard: return [{'projectId':k,**v} for k,v in self.registry.items()]
    def _register(self,root,p):
        with self.guard:
            pid=p.get('studio',{}).get('projectId') or uid(); p.setdefault('studio',{})['projectId']=pid
            if pid in self.registry and Path(self.registry[pid]['root'])!=root: pid=uid(); p['studio']['projectId']=pid
            self.registry[pid]={'root':str(root),'name':p['system']['name']}
            atomic(self.registry_file,encode(self.registry)); return pid
    def create_project(self,request):
        name=request.get('name','Untitled system')
        if not isinstance(name,str) or not name.strip(): fail('A project name is required.')
        slug=re.sub('[^a-zA-Z0-9_]+','_',name).strip('_') or 'system'
        root=Path(request['root']).expanduser().resolve() if request.get('root') else self.storage/'projects'/(slug+'-'+uid()[:8])
        if root.exists() and any(root.iterdir()): fail('New project folder must be empty.','exists',409)
        root.mkdir(parents=True,exist_ok=True); p=adapter.blank_project(slug)
        p['studio']={'schemaVersion':1,'brief':{k:'' for k in ['purpose','users','environment','constraints','acceptance']},'behavior':[],'modules':{},'deployment':{'units':[]}}
        if request.get('mode')=='starter' or request.get('starterId'):
            for idx,(label,kind) in enumerate([('publisher','pub'),('subscriber','sub')]):
                p['nodes'].append({'id':label,'label':label,'backing':'hand','pkg':'studio_demo','node':label,'artifact':label,'namespace':None,'x':60+idx*320,'y':80,'ifaces':[{'id':label+'_message','name':'message','kind':kind,'type':'std_msgs/msg/String','qos':None,'label':None,'exposed':True}],'params':[]})
                p['studio']['modules'][label]={'role':'Afferent' if kind=='pub' else 'Core','unit':'development','sourcePaths':[],'portBindings':{label+'_message':'/studio/chatter'},'runtimeNode':'/'+label}
            p['connections']=[{'id':'message_flow','from':{'n':'publisher','i':'publisher_message'},'to':{'n':'subscriber','i':'subscriber_message'}}]
            p['packages']={'studio_demo':{}}
            p['studio']['tutorial']='ros-pubsub'
            p['studio']['deployment']['units']=[{'id':'development','template':'ros-pubsub','image':'ros:jazzy-ros-base','nodeIds':['publisher','subscriber']}]
        pid=self._register(root,p); self._commit(root,p,{'revision':0,'modelState':'draft','modelPaths':[],'baseModelHashes':{},'sequence':0},'Created project')
        atomic(root/'.gitignore',b'.work/\n.studio/local.json\n.studio/recovery/\n')
        initial=self.state(root); initial['observed']=self._observed(root); atomic(root/'.studio/state.json',encode(initial))
        return self._envelope(pid)
    def open_project(self,request):
        path=Path(request.get('path','')).expanduser().resolve()
        if not path.exists(): fail('Project path does not exist.','not_found',404)
        root=path if path.is_dir() else path.parent
        model=path if path.suffix=='.rossystem' else None
        self._metadata(root)
        if model and (root/'project.json').exists(): fail('This folder already contains project.json. Open that project first; importing a model must not replace its draft.','existing_project',409)
        self._recover(root)
        try: p=adapter.seed_from_rossystem(str(model)) if model else read_json(root/'project.json')
        except Exception as e: fail('Could not read project: '+str(e))
        schema(p); self._recover(root); pid=self._register(root,p)
        if not (root/'.studio/state.json').exists():
            paths=[x.relative_to(root).as_posix() for x in root.glob('*') if x.suffix in ['.rossystem','.ros2','.ros'] and x.is_file()] if model else []
            self._commit(root,p,{'revision':0,'modelPaths':paths,'baseModelHashes':self._hashes(root,paths),'modelState':'clean' if model else 'draft','acceptedIdentity':model_identity(p) if model else None},'Opened project')
        return self.get_project(pid)
    def get_project(self,pid):
        with self.lock(pid): self._scan(pid); return self._envelope(pid)
    def open_source_workspace(self,request):
        if not isinstance(request.get('path'),str) or not request['path'].strip(): fail('Choose an explicit source workspace path.')
        root=Path(request.get('path','')).expanduser().resolve()
        if not root.is_dir(): fail('Choose an existing ROS source workspace.','not_found',404)
        if (root/'project.json').exists(): return self.open_project({'path':str(root)})
        self._metadata(root)
        name=re.sub('[^a-zA-Z0-9_]+','_',root.name)
        if not name or name[0].isdigit(): name='robot_'+name
        project=adapter.blank_project(name)
        project['studio']={'schemaVersion':1,'brief':{},'behavior':[],'modules':{},'deployment':{'units':[]}}
        pid=self._register(root,project)
        self._commit(root,project,{'revision':0,'modelPaths':[],'baseModelHashes':{},'modelState':'draft','observed':self._observed(root)},'Opened existing source workspace',expected={'project.json':None})
        return self.get_project(pid)
    def _expected(self,s,r):
        if r.get('expectedRevision')!=s['revision']: fail('Project changed in another session. Your draft is preserved locally.','revision_conflict',409,{'revision':s['revision']})
    def save_project(self,pid,request):
        with self.lock(pid):
            root=self.root(pid); self._scan(pid); self._recover(root); s=self.state(root)
            if s.get('projectInvalid'): fail('External project.json is invalid. Repair or restore it before saving.','external_project_invalid',409)
            mutation=request.get('clientMutationId')
            if mutation and mutation in s.get('mutations',[]): return self._envelope(pid)
            self._expected(s,request); p=copy.deepcopy(request.get('project')); schema(p)
            p.setdefault('studio',{})['projectId']=pid
            if model_identity(p)!=s.get('acceptedIdentity'): s['modelState']='conflict' if s.get('modelState')=='conflict' else 'draft'
            s['mutations']=(s.get('mutations',[])+([mutation] if mutation else []))[-100:]
            extra={'.studio/layouts.json':encode(request['session'])} if 'session' in request else None
            self._commit(root,p,s,'Autosaved draft',extra); out=self._envelope(pid); out['clientMutationId']=mutation; return out
    def save_session(self,pid,request):
        session=request.get('session',{})
        if not isinstance(session,dict) or len(encode(session))>1024*1024: fail('Invalid session.')
        with self.lock(pid): atomic(self.root(pid)/'.studio/layouts.json',encode(session)); return {'ok':True,'session':session}
    def _observation_hash(self,path):
        stat=path.stat(); key=(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
        cached=self.observation_hashes.get(str(path))
        if cached and cached[0]==key:return cached[1]
        value=digest(path.read_bytes()); after=path.stat()
        if key==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            self.observation_hashes[str(path)]=(key,value)
            if len(self.observation_hashes)>20000:self.observation_hashes.pop(next(iter(self.observation_hashes)),None)
        return value
    def _observed(self,root):
        out={}; notes=[]; visited=0
        for base,dirs,files in os.walk(root):
            dirs[:]=sorted(d for d in dirs if d not in IGNORED and not (Path(base)/d).is_symlink())
            for name in sorted(files):
                visited+=1
                if visited>10000:break
                path=Path(base)/name
                if path.name=='project.json':continue
                try:
                    if path.is_symlink():
                        notes.append({'severity':'warning','path':path.relative_to(root).as_posix(),'message':'Project observer does not follow symbolic links.'}); continue
                    if path.stat().st_size>MAX_BYTES:
                        notes.append({'severity':'warning','path':path.relative_to(root).as_posix(),'message':'File exceeds the project observer size limit.'}); continue
                    out[path.relative_to(root).as_posix()]=self._observation_hash(path)
                except OSError as error:notes.append({'severity':'warning','path':str(path),'message':'File could not be observed: '+str(error)})
            if visited>10000:
                notes.append({'severity':'warning','message':'Project observer reached 10,000 files. Additional files are not watched.'}); break
        # repository_source_hashes/source_coverage are about tracking whether a linked
        # repository's files have drifted since the last container build -- deployment-only
        # concerns. This app's `self.deployment` alias only ever points at StudioRepositories
        # (see studio_server.py), which implements resolve_repository_path for the guarded
        # reads elsewhere in this file but not these two build-staleness methods, so check for
        # them specifically rather than assuming every truthy `self.deployment` has them.
        if getattr(self,'deployment',None) and hasattr(self.deployment,'repository_source_hashes'):
            out.update(self.deployment.repository_source_hashes(str(root)))
            notes.extend(self.deployment.source_coverage(str(root)).get('diagnostics',[]))
        self.observation_coverage[str(root)]={'complete':not notes,'fileCount':len(out),'diagnostics':notes[:100]}
        return out
    def _parse(self,root,s,old):
        paths=[safe_path(root,p) for p in s.get('modelPaths',[]) if p.endswith('.rossystem')]
        if not paths: fail('No linked system model.','invalid_model',422)
        p=adapter.seed_from_rossystem(str(paths[0])) if len(paths)==1 else adapter.seed_from_many([str(x) for x in paths])
        schema(p)
        errs,_,_,_=adapter.run_lint([str(safe_path(root,x)) for x in s.get('modelPaths',[])])
        if errs: fail('External model linter found errors.','invalid_model',422,{'findings':[x.as_dict() for x in errs]})
        diag=adapter.validate_project(p)
        if diag['global'] or any(diag['byNode'].values()): fail('External model has unresolved or invalid content.','invalid_model',422,diag)
        previous={(n['label'],n.get('pkg'),n.get('node')):n for n in old['nodes']}; remap={}; iface_map={}
        for n in p['nodes']:
            original_id=n['id']
            before=previous.get((n['label'],n.get('pkg'),n.get('node')))
            if before:
                remap[n['id']]=before['id']; n['id']=before['id']; n['x']=before.get('x',0); n['y']=before.get('y',0)
                orig={(i['kind'],i['name']):i for i in before['ifaces']}
                for f in n['ifaces']:
                    if (f['kind'],f['name']) in orig: iface_map[f['id']]=orig[(f['kind'],f['name'])]['id']; f['id']=iface_map[f['id']]
            if not before:
                n['id']='n_'+uid()[:12]; remap[original_id]=n['id']
        def connection_key(c):
            return (c['from']['n'],c['from']['i'],c['to']['n'],c['to']['i'])
        prior_connections={connection_key(c):c['id'] for c in old['connections']}
        for c in p['connections']:
            for k in ['from','to']: c[k]['n']=remap.get(c[k]['n'],c[k]['n']); c[k]['i']=iface_map.get(c[k]['i'],c[k]['i'])
            c['id']=prior_connections.get(connection_key(c),'c_'+uid()[:12])
        p['studio']=copy.deepcopy(old.get('studio',{}))
        retained={n['id'] for n in p['nodes']}
        for unit in p['studio'].get('deployment',{}).get('units',[]):
            missing=[n for n in unit.get('nodeIds',[]) if n not in retained]
            if missing: unit['orphanedNodeIds']=sorted(set(unit.get('orphanedNodeIds',[])+missing)); unit['nodeIds']=[n for n in unit.get('nodeIds',[]) if n in retained]
        return p
    def _scan(self,pid):
        root=self.root(pid); self._recover(root); s=self.state(root)
        project_file=root/'project.json'; actual_hash=digest(project_file.read_bytes()) if project_file.exists() else None
        if s.get('projectInvalid') and actual_hash==s.get('projectHash'):
            s.pop('projectInvalid',None); s.pop('invalidProjectHash',None); s['diagnostics']=[]
            cached=read_json(root/'.studio/last-project.json'); s['modelState']='clean' if model_identity(cached)==s.get('acceptedIdentity') else 'draft'; atomic(root/'.studio/state.json',encode(s))
        if s.get('projectHash') and actual_hash!=s['projectHash']:
            try:
                incoming=read_json(project_file); schema(incoming)
                incoming.setdefault('studio',{})['projectId']=pid
            except Exception as exc:
                if not s.get('projectInvalid') or s.get('invalidProjectHash')!=actual_hash:
                    s['projectInvalid']=True; s['invalidProjectHash']=actual_hash; s['modelState']='external-invalid'; s['diagnostics']=['project.json is invalid; showing the last saved project. '+str(exc)]; s['sequence']=s.get('sequence',0)+1
                    atomic(root/'.studio/state.json',encode(s))
                return
            s['modelState']='draft' if model_identity(incoming)!=s.get('acceptedIdentity') else 'clean'; s['diagnostics']=[]
            s['sequence']=s.get('sequence',0)+1; self._commit(root,incoming,s,'Accepted external project.json edit',expected={'project.json':actual_hash}); s=self.state(root)
        p=self._project(root); current=self._observed(root); previous=s.get('observed',{})
        changed=[{'path':k,'beforeHash':previous.get(k),'hash':current.get(k)} for k in sorted(set(current)|set(previous)) if current.get(k)!=previous.get(k)]
        if not changed: return
        s['observed']=current; s['sequence']=s.get('sequence',0)+1; s['changes']=changed
        tracked=set(s.get('modelPaths',[]))
        scopes={str(Path(path).parent) for path in tracked if path.endswith('.rossystem')}
        for rel in current:
            if Path(rel).suffix in ['.ros2','.ros'] and str(Path(rel).parent) in scopes: tracked.add(rel)
        s['modelPaths']=sorted(tracked)
        hashes=self._hashes(root,s.get('modelPaths',[]))
        model_changed=hashes!=s.get('baseModelHashes',{}) or (s.get('modelState')=='external-invalid' and not s.get('projectInvalid'))
        if model_changed:
            try:
                incoming=self._parse(root,s,p)
                if hashes!=self._hashes(root,s.get('modelPaths',[])): return
                if model_identity(p)!=s.get('acceptedIdentity'):
                    s['modelState']='conflict'; s['diagnostics']=['External model and saved draft both changed. Review before replacing either.']; s['externalProject']=incoming
                else:
                    p=incoming; s['acceptedIdentity']=model_identity(p); s['baseModelHashes']=hashes; s['modelState']='clean'; s['diagnostics']=[]
            except Exception as e: s['modelState']='external-invalid'; s['diagnostics']=['File invalid; graph shows previous valid version. '+str(e)]
        if model_changed:
            self._commit(root,p,s,'Observed file changes')
        else:
            # Source observations are not edits to the engineering draft. Keep its
            # optimistic-lock revision while advancing the source change sequence.
            atomic(root/'.studio/state.json',encode(s))
    def changes(self,pid,after=0):
        result=self.get_project(pid); s=self.state(self.root(pid)); result['changes']=s.get('changes',[]) if s.get('sequence',0)>int(after) else []; return result
    def _fresh(self,pid):
        self._scan(pid); root=self.root(pid); state=self.state(root)
        if state.get('projectInvalid'): fail('External project.json is invalid; repair or restore it first.','external_project_invalid',409)
        return root,state
    def reconcile(self,pid,request):
        with self.lock(pid):
            root,s=self._fresh(pid); self._expected(s,request)
            hashes=self._hashes(root,s.get('modelPaths',[]))
            if request.get('observedHashes')!=hashes: fail('Files changed again. Refresh before resolving.','hash_conflict',409)
            p=read_json(root/'project.json')
            if request.get('action') in ['use-external','external']:
                p=self._parse(root,s,p); s['acceptedIdentity']=model_identity(p); s['modelState']='clean'
            elif request.get('action') in ['keep-draft','draft']: s['modelState']='draft'
            else: fail('Unknown reconciliation action.')
            s['baseModelHashes']=hashes; s['diagnostics']=[]; s.pop('externalProject',None); self._commit(root,p,s,'Resolved model difference'); return self._envelope(pid)
    def artifacts(self,pid):
        root=self.root(pid); s=self.state(root); paths={'project.json'}|set(self._observed(root)); owned=read_json(root/'.studio/generation.json',{}).get('hashes',{})
        p=read_json(root/'project.json'); items=[]
        for rel in sorted(paths):
            path=self.deployment.resolve_repository_path(str(root),rel) if getattr(self,'deployment',None) else safe_path(root,rel); h=digest(path.read_bytes()) if path.is_file() else None
            items.append({'path':rel,'kind':'model' if path.suffix in ['.rossystem','.ros2','.ros'] else 'project' if rel=='project.json' else 'deployment' if rel.startswith('deploy/') else 'source','exists':path.is_file(),'owner':'generated' if rel in owned else 'user','hash':h,'moduleIds':[k for k,v in p.get('studio',{}).get('modules',{}).items() if rel in v.get('sourcePaths',[])],'actions':['open','preview']})
        return {'ok':True,'artifacts':items}
    def read_file(self,pid,path):
        file=self.deployment.resolve_repository_path(str(self.root(pid)),path) if getattr(self,'deployment',None) else safe_path(self.root(pid),path)
        if not file.is_file(): fail('Artifact does not exist.','not_found',404)
        if file.stat().st_size>MAX_BYTES: fail('File too large to preview.','size',413)
        b=file.read_bytes()
        try: text=b.decode('utf8')
        except UnicodeDecodeError: return {'ok':True,'path':path,'binary':True,'hash':digest(b)}
        return {'ok':True,'path':path,'text':text,'content':text,'hash':digest(b)}
    def preview_generation(self,pid,request):
        with self.lock(pid):
            root,s=self._fresh(pid); self._expected(s,request); p=read_json(root/'project.json'); kind=request.get('kind','models')
            result={}
            if kind=='deployment':
                if not self.runtime: fail('Deployment adapter unavailable.','unavailable',503)
                result=self.runtime.preview_deployment(str(root),p); files=result['files']; unsupported=result.get('unsupported',[])
            elif kind=='models':
                d=adapter.validate_project(p)
                if d['global'] or any(d['byNode'].values()): fail('Model needs corrections before generation.','invalid_model',422,d)
                files={'models/'+k:v for k,v in adapter.generate_files(p).items()}; unsupported=['Model generation does not generate ROS application source or launch files.']
            elif kind=='code':
                from studio_codegen import preview
                result=preview(root,p,request);files=result['files'];unsupported=result['unsupported']
            else: fail('Unsupported generation kind.')
            outputs=[]
            for rel,text in files.items():
                path=safe_path(root,rel); before=path.read_text() if path.exists() else ''
                outputs.append({'path':rel,'content':text,'text':text,'before':before,'after':text,'beforeHash':digest(path.read_bytes()) if path.exists() else None,'status':'unchanged' if before==text else 'change' if path.exists() else 'create','diff':''.join(difflib.unified_diff(before.splitlines(True),text.splitlines(True),fromfile=rel,tofile=rel))})
            plan={'ok':True,'planId':uid(),'projectId':pid,'revision':s['revision'],'kind':kind,'outputs':outputs,'diagnostics':result.get('diagnostics',[]),'conflicts':result.get('conflicts',[]),'blocked':bool(result.get('conflicts')),'modules':result.get('modules',{}),'unsupported':unsupported,'createdAt':time.time(),'inputHashes':self._hashes(root,s.get('modelPaths',[]))}
            plan['files']=outputs; self.plans[plan['planId']]=plan; return plan
    def apply_generation(self,pid,request):
        with self.lock(pid):
            root,s=self._fresh(pid); self._expected(s,request); plan=self.plans.get(request.get('planId'))
            if not plan or plan['projectId']!=pid or plan['revision']!=s['revision'] or time.time()-plan['createdAt']>1800: fail('Generation preview expired. Preview again.','plan_conflict',409)
            if plan.get('blocked'): fail('Existing source conflicts must be resolved before generating a skeleton.','source_conflict',409,plan['conflicts'])
            if self._hashes(root,s.get('modelPaths',[]))!=plan['inputHashes']: fail('Model changed after preview.','hash_conflict',409)
            for o in plan['outputs']:
                path=safe_path(root,o['path']); h=digest(path.read_bytes()) if path.exists() else None
                if h!=o['beforeHash']: fail('File changed after preview.','hash_conflict',409,{'path':o['path']})
            p=read_json(root/'project.json'); files={o['path']:o['content'].encode() for o in plan['outputs']}; ownership=read_json(root/'.studio/generation.json',{'hashes':{}})
            ownership['hashes'].update({k:digest(v) for k,v in files.items()}); ownership['generator']='ros_studio.py formatVersion5'; ownership['appliedAt']=stamp(); files['.studio/generation.json']=encode(ownership)
            if plan['kind']=='models':
                s['modelPaths']=[o['path'] for o in plan['outputs']]; s['baseModelHashes']={k:digest(v) for k,v in files.items() if k in s['modelPaths']}; s['acceptedIdentity']=model_identity(p); s['modelState']='clean'; s['diagnostics']=[]
            if plan['kind']=='code':
                for ident,metadata in plan['modules'].items():
                    p['studio']['modules'][ident]={**p['studio']['modules'].get(ident,{}),**metadata}
            self._commit(root,p,s,'Applied '+plan['kind']+' generation',files,expected={o['path']:o['beforeHash'] for o in plan['outputs']}); return self._envelope(pid)
    def _snapshot(self,pid):
        root=self.root(pid); p=read_json(root/'project.json'); return {'engineering':engineering(p),'files':self._observed(root),'modelHashes':self._hashes(root,self.state(root).get('modelPaths',[]))}
    def capture_evidence(self,pid,request):
        with self.lock(pid):
            root,s=self._fresh(pid); self._expected(s,request); label=request.get('label','Design snapshot')
            if not isinstance(label,str): fail('Evidence label must be text.')
            check_ids=request.get('checkIds',[])
            if not isinstance(check_ids,list) or len(check_ids)>100: fail('Invalid check references.')
            linked=[]
            for ident in check_ids:
                checked=self.evidence(pid,ident)['evidence']
                if checked['kind']!='check': fail('Referenced evidence is not a check result.')
                linked.append({'evidenceId':ident,'stale':digest(checked['snapshot'])!=digest(self._snapshot(pid)),'results':checked['checks']})
            rec={'id':uid(),'projectId':pid,'revision':s['revision'],'capturedAt':stamp(),'label':label,'kind':request.get('kind','design'),'snapshot':self._snapshot(pid),'checks':request.get('_checks',[])+linked,'missingInputs':['No live telemetry captured.','No robot execution or physics evidence.','Oracle not run.']}
            if request.get('_observations'):
                rec['observations']=request['_observations']
                rec['missingInputs']=['No physics evidence.','ROS observations do not establish hardware safety.','Oracle not run.']
            if request.get('_missingInputs'): rec['missingInputs']=request['_missingInputs']
            atomic(root/'.studio/evidence'/(rec['id']+'.json'),encode(rec)); return {'ok':True,'evidence':rec,'evidenceId':rec['id']}
    def evidence(self,pid,record=None):
        root=self.root(pid)
        if record:
            if not isinstance(record,str) or not re.fullmatch('[a-f0-9]{32}',record): fail('Invalid evidence ID.')
            p=root/'.studio/evidence'/(record+'.json')
            if not p.exists(): fail('Evidence not found.','not_found',404)
            return {'ok':True,'evidence':read_json(p)}
        now=digest(self._snapshot(pid)); records=[]
        for path in sorted((root/'.studio/evidence').glob('*.json'),reverse=True):
            r=read_json(path); records.append({k:r[k] for k in ['id','label','kind','revision','capturedAt']}|{'stale':digest(r['snapshot'])!=now,'recordType':'simulation' if r['kind']=='simulation' else 'observations' if r.get('observations') else r['kind']})
        records.sort(key=lambda r:r['capturedAt'],reverse=True)
        return {'ok':True,'evidence':records,'records':records}
    def compare_evidence(self,pid,a,b):
        a=self.evidence(pid,a)['evidence']; b=self.evidence(pid,b)['evidence']; return {'ok':True,'differences':differences(a['snapshot'],b['snapshot'])+differences(a['checks'],b['checks'],'checks')+differences(a.get('observations'),b.get('observations'),'observations'),'missingInputs':list(set(a['missingInputs']+b['missingInputs']))}
    def validate(self,pid,request):
        errs=[]
        with self.lock(pid):
            root,s=self._fresh(pid); self._expected(s,request); p=read_json(root/'project.json'); d=adapter.validate_project(p); checks=[{'name':'Model generation gate','status':'failed' if d['global'] or any(d['byNode'].values()) else 'passed','diagnostics':d,'tool':'ros_studio.validate_project'}]
            if checks[0]['status']=='passed':
                stage=safe_path(root,'.work/checks/'+uid()); stage.mkdir(parents=True)
                paths=[]
                for rel,content in adapter.generate_files(p).items():
                    path=safe_path(stage,rel); atomic(path,content.encode()); paths.append(str(path))
                errs,warns,infos,findings=adapter.run_lint(paths)
                checks.append({'name':'ROS model linter','status':'failed' if errs else 'passed','diagnostics':[x.as_dict() for x in findings],'errors':len(errs),'warnings':len(warns),'tool':'rosmodel_lint'})
            if s.get('modelState') in ['external-invalid','conflict']: checks.append({'name':'Accepted model files','status':'failed','diagnostics':s.get('diagnostics',[])})
        # The language-server oracle can take minutes (a real JVM + LSP handshake -- up to
        # 300s per run_oracle's own timeout, plus up to 60s of preflight), so it must not run
        # while holding this project's lock: every other request against the same project (an
        # autosave, another check, an edit) would block for as long as the oracle does. It only
        # reads the already-staged files under `stage`, so no lock is needed here --
        # capture_evidence() below re-acquires the lock itself and re-checks expectedRevision,
        # so a project edited during this window is still caught, not silently ignored.
        #
        # The linter's RM rules are a deliberate approximation of the real Xtext validator (see
        # ros_studio.py's own --oracle path) -- some errors only the actual language server
        # catches. Ask it for real whenever it can run, instead of the placeholder "not
        # executed" row this used to leave behind: that told the user nothing they could act
        # on. oracle_preflight() is a cheap java-version/jar check, so a missing JRE reports why
        # in milliseconds rather than after a minute-long JVM start that was doomed from the
        # start. Wrapped in try/except: an unexpected failure here must degrade to a failed
        # check row, not wipe out the gate/linter results already computed above with a generic
        # 500 from the server's catch-all error handler.
        if checks[0]['status']=='passed':
            if not errs:
                try:
                    available,why=adapter.oracle_preflight()
                    if not available:
                        checks.append({'name':'Language server oracle','status':'not-run',
                                       'diagnostics':[{'message':'The real language-server check did not run.','hint':why+' Install a Java 19+ runtime, 21 recommended (or set ROSMODEL_JAVA to one) to enable it; the model generation gate and linter above already cover most issues without it.'}],
                                       'tool':'ask_oracle'})
                    else:
                        ok,text,records=adapter.run_oracle(str(stage),paths)
                        odiag=adapter.oracle_diagnostics(records,p)
                        n_err=len(odiag['global'])+sum(len(v) for v in odiag['byNode'].values())
                        if ok and not n_err:
                            checks.append({'name':'Language server oracle','status':'passed','diagnostics':[],'tool':'ask_oracle'})
                        elif n_err:
                            checks.append({'name':'Language server oracle','status':'failed','diagnostics':odiag,'errors':n_err,'tool':'ask_oracle'})
                        else:
                            checks.append({'name':'Language server oracle','status':'failed',
                                           'diagnostics':[{'message':'The language server started but did not finish answering.','hint':'This is a broken or unresponsive server, not a verdict on your model — rerun checks. Output: '+text}],
                                           'tool':'ask_oracle'})
                except Exception as exc:
                    checks.append({'name':'Language server oracle','status':'failed',
                                   'diagnostics':[{'message':'The language-server check could not run.','hint':str(exc)}],
                                   'tool':'ask_oracle'})
            else:
                checks.append({'name':'Language server oracle','status':'not-run','diagnostics':[{'message':'Skipped because the ROS model linter above found errors.','hint':'Fix those findings and run checks again — the language server needs generated files without known errors to check.'}],'tool':'ask_oracle'})
        result=self.capture_evidence(pid,{'expectedRevision':s['revision'],'kind':'check','label':'Model checks','_checks':checks}); result['checks']=checks; result['diagnostics']=d; return result
    def recovery_preview(self,pid,revision):
        with self.lock(pid):
            root=self.root(pid); self._recover(root)
            if not isinstance(revision,int) or isinstance(revision,bool) or revision<1:fail('Invalid recovery revision.')
            snapshot=read_json(safe_path(root,'.studio/recovery/rev-%08d.json'%revision))
            if not snapshot:fail('Recovery revision not found.','not_found',404)
            current=self._project(root)
            return {'ok':True,'projectId':pid,'revision':snapshot['revision'],'time':snapshot['time'],'reason':snapshot['reason'],'project':snapshot['project'],'currentRevision':self.state(root)['revision'],'differences':differences(engineering(current),engineering(snapshot['project']))}
    def recovery(self,pid,request=None):
        root=self.root(pid)
        if request is not None:
            with self.lock(pid):
                self._scan(pid); root=self.root(pid); s=self.state(root); self._expected(s,request); rev=request.get('revisionId',request.get('revision'))
                if not isinstance(rev,int) or rev<1: fail('Invalid recovery revision.')
                snap=read_json(safe_path(root,'.studio/recovery/rev-%08d.json'%rev))
                if not snap: fail('Recovery revision not found.','not_found',404)
                s['modelState']='draft'; current=digest((root/'project.json').read_bytes()) if (root/'project.json').exists() else None; self._commit(root,snap['project'],s,'Restored revision '+str(rev),expected={'project.json':current}); return self._envelope(pid)
        records=[]
        for path in sorted((root/'.studio/recovery').glob('rev-*.json'),reverse=True)[:100]:
            r=read_json(safe_path(root,path.relative_to(root).as_posix())); records.append({k:r[k] for k in ['revision','time','reason']})
        return {'ok':True,'recovery':records}
    def catalogue(self):
        data=adapter.load_autocomplete(); out=[]
        for key,rec in data['catalogue'].items():
            pkg,node=key.split('.',1); typ=data['catalogueTypes'].get(key,{})
            n={'id':key,'label':node,'backing':'cat','pkg':pkg,'node':node,'artifact':rec.get('artifact',node),'catalogueFile':rec.get('file'),'namespace':None,'x':0,'y':0,'ifaces':[],'params':[]}
            for ix,(name,kind) in enumerate(rec.get('interfaces',{}).items()):
                t=typ.get(kind+' '+name) or typ.get(name)
                n['ifaces'].append({'id':key+'_'+str(ix),'name':name,'kind':kind,'type':t,'qos':None,'label':None,'exposed':True})
            out.append({'id':key,'name':node,'package':pkg,'node':n})
        return {'catalogue':out,'types':data['types'],'warnings':data['warnings']}
