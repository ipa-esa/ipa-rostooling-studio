#!/usr/bin/env python3
"""Run the local, token-protected CoreSense Studio workspace service.

This is the architecture/modeling/validation slice of the tool: project authoring,
the Architecture graph, extracting components from existing ROS source repositories,
and checking a model (the generation gate, the linter, and -- when Java is available
-- the real language-server oracle). Deployment/containerization, live ROS telemetry,
robot-asset simulation and the review/release workflow are a separate, confidential
tool and are not part of this one.
"""
import argparse, json, mimetypes, os, re, secrets, sys, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from studio_store import StudioStore, StudioError, MAX_BYTES, fail, atomic, digest, encode, safe_path, uid

# Extractor flag block -> interface kind (see extract_ros2_interfaces.py).
_FLAG_KIND={'publishers':'pub','subscribers':'sub','services':'ss','serviceservers':'ss','clients':'sc','serviceclients':'sc','actionservers':'as','actionclients':'ac'}
def apply_agent_resolutions(catalogue,pid,plan_id,job):
    """Write a finished resolve-flags job's traced/inferred names into a catalogue preview
    plan BEFORE it is published. Each changed or added interface carries 'agentResolved'
    (resolution, citation, note, flag location, provider) so the UI can mark it "resolved by
    agent -- review"; anything unresolved, ambiguous or not an interface name is left as it was
    and counted as skipped. The plan gets a new version so its catalogue entry ids change too."""
    if job.get('kind')!='resolve-flags' or job.get('state')!='passed' or not isinstance(job.get('resolutions'),list): fail('The agent run has no usable result.','job_not_done',409)
    if not isinstance(plan_id,str) or not re.fullmatch('[a-f0-9]{32}',plan_id): fail('Select a catalogue conversion preview.')
    path=safe_path(catalogue.directory,'plans/'+plan_id+'.json')
    if not path.is_file(): fail('Catalogue preview does not exist.','not_found',404)
    plan=json.loads(path.read_text())
    if plan['projectId']!=pid: fail('This preview belongs to another project.','catalogue_scope',403)
    if plan.get('agentApplied'): fail('Agent results were already applied to this preview.','already_applied',409)
    # The job's flags came from the client: only trust them for the source this plan was read
    # from, and only flags this plan itself reported.
    if not job.get('sourceRoot') or str(Path(job['sourceRoot']).resolve())!=str(Path(plan['sourceRoot']).resolve()): fail('The agent run read a different source folder than this preview.','job_scope',409)
    planned={(f.get('at'),f.get('reason')) for f in plan.get('flags',[])+[f for e in plan['entries'] for f in e.get('flags',[])]}
    flags,applied,skipped=job.get('flags') or [],[],[]
    for r in job['resolutions']:
        i=r.get('index') if isinstance(r,dict) else None; flag=flags[i] if isinstance(i,int) and 0<=i<len(flags) else None
        kind=_FLAG_KIND.get((flag or {}).get('kind')); value=r.get('value') if isinstance(r,dict) else None
        why=None if flag else 'no matching flag'
        if not why and (flag.get('at'),flag.get('reason')) not in planned: why='flag is not in this preview'
        if not why and not kind: why='not a topic/service/action name'
        if not why and (r.get('resolution') not in ('traced','inferred') or not isinstance(value,str) or not re.fullmatch(r'~?/?[A-Za-z_]\w*(/[A-Za-z_]\w*)*',value)): why='agent left it unresolved' if r.get('resolution')=='unresolved' else 'agent value is not a single ROS name'
        file=((flag or {}).get('at') or '').rsplit(':',1)[0]
        entries=[e for e in plan['entries'] if e['package']==(flag or {}).get('package')] if not why else []
        if (flag or {}).get('artifact'): entries=[e for e in entries if flag['artifact'] in (e.get('artifact'),e['name'])]
        entries=[e for e in entries if any(p!='.' and (file.endswith(p) or p.endswith(file)) for p in e.get('sourcePaths',[]))] or (entries if len(entries)==1 else [])
        if not why and len(entries)!=1: why='could not tell which node it belongs to'
        if why: skipped.append({'at':(flag or {}).get('at'),'reason':why}); continue
        entry=entries[0]; node=entry['node']; typ=r.get('type') if isinstance(r.get('type'),str) and re.fullmatch(r'[A-Za-z_]\w*/(msg/|srv/|action/)?\w+',r.get('type') or '') else None
        mark={'resolution':r['resolution'],'citation':r.get('citation'),'note':r.get('note'),'at':flag.get('at'),'by':job.get('provider')}
        # The extractor keeps a runtime-built name whose type is literal as an interface named
        # with {expression} placeholders and says so in the flag reason ("Kept as '<name>'"):
        # replace that interface's name instead of adding a duplicate.
        kept=re.search(r"Kept as '([^']+)'",flag.get('reason') or '')
        target=next((f for f in node['ifaces'] if f['kind']==kind and ((kept and f['name']==kept.group(1)) or (flag.get('at') and f.get('at')==flag.get('at')))),None)
        if any(f is not target and f['kind']==kind and f['name'].lstrip('/')==value.lstrip('/') for f in node['ifaces']): skipped.append({'at':flag.get('at'),'reason':'node already has a '+kind+' named '+value}); continue
        if target: target['name']=value; target['agentResolved']=mark; target['type']=target.get('type') or typ
        else: node['ifaces'].append({'id':'agent_'+uid()[:20],'name':value,'kind':kind,'type':typ,'qos':None,'label':None,'exposed':True,'agentResolved':mark})
        for f in entry.get('flags',[])+plan.get('flags',[]):
            if f.get('at')==flag.get('at') and f.get('reason')==flag.get('reason'): f['agentResolution']={**mark,'value':value}
        entry['interfaceKinds']=sorted({f['kind'] for f in node['ifaces']}); entry['messageTypes']=sorted({f['type'] for f in node['ifaces'] if f.get('type')})
        applied.append({'package':entry['package'],'node':entry['name'],'kind':kind,'name':value,'type':target.get('type') if target else typ,'resolution':r['resolution']})
    if applied:
        version=digest([plan['version'],applied])[:24]
        for e in plan['entries']: e['id']=e['id'].replace(':'+plan['version']+':',':'+version+':')
        plan['version']=version
    plan['agentApplied']={'jobId':job['jobId'],'applied':applied,'skipped':skipped}
    atomic(path,encode(plan))
    return {'ok':True,'applied':applied,'skipped':skipped,'version':plan['version']}

def _session_token(storage_root, rotate=False):
    """Reuse the same session token across restarts instead of minting a new one every time.

    A browser tab keeps its token fine across an ordinary reload (app.js caches it in
    sessionStorage), but a brand-new random token on every server restart made any
    previously bookmarked or saved URL stop working -- the tab would 401 with no way back
    in short of finding wherever the new URL was printed. Persisting it here means the
    same URL keeps working indefinitely; --rotate-token invalidates it on purpose.
    """
    path = Path(storage_root).expanduser().resolve() / '.studio-token'
    if not rotate:
        try:
            existing = path.read_text().strip()
            if re.fullmatch(r'[A-Za-z0-9_-]{20,}', existing):
                return existing
        except OSError:
            pass
    token = secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token)
        os.chmod(path, 0o600)
    except OSError:
        pass  # Falls back to an ephemeral token for this run; still works, just won't persist.
    return token

class StudioServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,store,runtime=None,token=None):
        self.store=store; self.runtime=runtime; store.runtime=runtime; self.token=token or secrets.token_urlsafe(32)
        from studio_source import StudioSource
        self.source=StudioSource(store.repo,store.storage)
        if runtime:
            from studio_repositories import StudioRepositories
            self.repositories=StudioRepositories(runtime)
            runtime.resolve_repository_path=self.repositories.resolve_repository_path
            # submit_flag_resolution confines the sourceRoot a request can point an agent's
            # working directory at to the project's own root or an actually-registered
            # repository's real path -- this is what it uses to enumerate the latter.
            runtime.list_repositories=self.repositories.repositories
            # studio_store.py's file/artifact reads look for `self.deployment.resolve_repository_path`
            # (guarded with getattr, so harmless if absent) to resolve a path that lives inside a
            # linked repository rather than the project directory itself -- StudioRepositories
            # implements that same method, so this alias keeps that resolution working without
            # store.py needing to know this app dropped deployment/containers.
            store.deployment=self.repositories
        from studio_catalogue import StudioCatalogue
        self.catalogue=StudioCatalogue(store,self.source,getattr(self,'repositories',None))
        self.static=store.repo/'web/studio'; super().__init__(address,Handler)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def _json(self,status,value):
        data=json.dumps(value,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(data))); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.end_headers(); self.wfile.write(data)
    def _body(self):
        try: size=int(self.headers.get('Content-Length','0'))
        except ValueError: fail('Invalid Content-Length.')
        if size<0 or size>MAX_BYTES: fail('Request body too large.','size',413)
        try: body=json.loads(self.rfile.read(size) or b'{}')
        except (ValueError,UnicodeError): fail('Malformed JSON.')
        if not isinstance(body,dict): fail('Expected JSON object.')
        return body
    def _route(self):
        parsed=urlsplit(self.path); path=unquote(parsed.path); query=parse_qs(parsed.query)
        host=self.headers.get('Host',''); allowed={'127.0.0.1:'+str(self.server.server_port),'localhost:'+str(self.server.server_port)}
        if host not in allowed: fail('Foreign host is not allowed.','origin',403)
        origin=self.headers.get('Origin')
        if origin and origin not in {'http://'+h for h in allowed}: fail('Foreign origin is not allowed.','origin',403)
        if not path.startswith('/api/'):
            if self.command!='GET': fail('Not found.','not_found',404)
            rel=path.lstrip('/') or 'index.html'; target=(self.server.static/rel).resolve()
            if not target.is_relative_to(self.server.static.resolve()) or not target.is_file(): fail('Page not found.','not_found',404)
            data=target.read_bytes(); self.send_response(200); self.send_header('Content-Type',mimetypes.guess_type(str(target))[0] or 'application/octet-stream'); self.send_header('Content-Length',str(len(data))); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.end_headers(); self.wfile.write(data); return None
        if not secrets.compare_digest(self.headers.get('X-Studio-Token',''),self.server.token): fail('Studio session token required.','session',401)
        s=self.server.store; rt=self.server.runtime; body=self._body() if self.command in ['POST','PUT'] else {}; method=self.command
        if path=='/api/bootstrap': return {'ok':True,'roots':{'storage':str(s.storage),'repository':str(s.repo)},'projects':s.recent(),'recentProjects':s.recent(),'capabilities':rt.capabilities() if rt else {},**s.catalogue()}
        if path=='/api/projects' and method=='POST': return s.create_project(body)
        if path=='/api/projects/open' and method=='POST': return s.open_project(body)
        if path=='/api/projects/source' and method=='POST': return s.open_source_workspace(body)
        if path=='/api/tools': return {'ok':True,**(rt.capabilities() if rt else {})}
        if path=='/api/plugins':
            entries=json.loads((self.server.static/'tools.json').read_text())
            for manifest in (self.server.static/'plugins').glob('*/plugin.json'):
                try:
                    item=json.loads(manifest.read_text()); entry=(manifest.parent/item['entry']).resolve()
                    if not entry.is_relative_to(manifest.parent.resolve()) or not entry.is_file(): continue
                    if any(e['id']==item.get('id') for e in entries): continue
                    item['entry']=entry.relative_to(self.server.static).as_posix();item.pop('action',None);entries.append(item)
                except (ValueError,KeyError,TypeError): continue
            return {'ok':True,'tools':entries}
        if path=='/api/settings' and method=='PUT':
            if not rt: fail('Runtime adapter unavailable.','unavailable',503)
            return {'ok':True,**rt.update_settings(body)}
        bits=path.split('/')[3:]
        if not path.startswith('/api/projects/') or not bits: fail('Route not found.','not_found',404)
        pid=bits[0]; sub='/'.join(bits[1:]); root=s.root(pid)
        if sub in ('catalogue','catalogue/templates','catalogue/inspect','catalogue/preview','catalogue/publish','catalogue/instantiate'):
            catalogue=self.server.catalogue
            if sub in ('catalogue','catalogue/templates') and method=='GET': return catalogue.list(pid,{k:v[0] for k,v in query.items()})
            if sub=='catalogue/inspect' and method=='GET': return catalogue.inspect(pid,{k:v[0] for k,v in query.items()})
            if sub=='catalogue/preview' and method=='POST': return catalogue.preview(pid,body)
            if sub=='catalogue/publish' and method=='POST': return catalogue.publish(pid,body)
            if sub=='catalogue/instantiate' and method=='POST': return catalogue.instantiate(pid,body)
            fail('Unsupported catalogue method.','method',405)
        if sub=='source/inspect': return self.server.source.inspect(root)
        if sub=='source/preview' and method=='POST': return self.server.source.preview(root,body)
        if sub=='source/import' and method=='POST':
            with s.lock(pid):
                current=s.get_project(pid)
                if current['revision']!=body.get('expectedRevision'): fail('Project changed during source review.','revision_conflict',409)
                imported=self.server.source.import_plan(root,body)
                project=imported['project']; metadata=current['project'].get('studio',{})
                project['studio']={**metadata,**project.get('studio',{})}
                for key in ['brief','behavior','criteria','deployment']: project['studio'][key]=metadata.get(key,project['studio'].get(key,{} if key in ['brief','deployment'] else []))
                project['studio']['projectId']=pid
                return s.save_project(pid,{'expectedRevision':current['revision'],'project':project})
        if sub=='repositories':
            return {'ok':True,**(self.server.repositories.fetch_repository(root,body) if method=='POST' else self.server.repositories.repositories(root))}
        if sub=='repositories/dependencies':
            return {'ok':True,**self.server.repositories.repository_dependencies(root,query.get('repositoryId',[''])[0])}
        if sub=='repositories/submodules' and method=='POST':
            return {'ok':True,**self.server.repositories.init_submodules(root,body.get('repositoryId'),body.get('paths'))}
        if not sub:
            if method=='GET': return s.get_project(pid)
            if method=='PUT': return s.save_project(pid,body)
        if sub=='session' and method=='PUT': return s.save_session(pid,body)
        if sub=='changes': return s.changes(pid,query.get('after',['0'])[0])
        if sub=='reconcile' and method=='POST': return s.reconcile(pid,body)
        if sub=='artifacts': return s.artifacts(pid)
        if sub=='file': return s.read_file(pid,query.get('path',[''])[0])
        if sub=='generation/preview' and method=='POST': return s.preview_generation(pid,body)
        if sub=='generation/apply' and method=='POST': return s.apply_generation(pid,body)
        if sub=='validate' and method=='POST': return s.validate(pid,body)
        if sub=='evidence':
            if method=='POST':
                if any(k.startswith('_') for k in body) or body.get('kind','design')!='design': fail('Only validation can create check evidence.')
                return s.capture_evidence(pid,body)
            return s.evidence(pid)
        if sub=='evidence/compare': return s.compare_evidence(pid,query.get('a',[''])[0],query.get('b',[''])[0])
        if sub.startswith('evidence/'): return s.evidence(pid,sub.split('/')[1])
        if sub.startswith('recovery/') and method=='GET':
            revision=sub.split('/')[-1]
            if not revision.isdigit(): fail('Invalid recovery revision.')
            return s.recovery_preview(pid,int(revision))
        if sub=='recovery': return s.recovery(pid,body if method=='POST' else None)
        if not rt: fail('Runtime adapter unavailable.','unavailable',503)
        if sub=='handoff' and method=='POST': return {'ok':True,**rt.prepare_handoff(str(root),body)}
        if sub=='tools/open' and method=='POST': return {'ok':True,**rt.open_tool(str(root),body)}
        if sub=='runtime': return {'ok':True,**rt.runtime_status(str(root))}
        if sub=='catalogue/resolve-flags' and method=='POST': return {'ok':True,**rt.submit_flag_resolution(str(root),body)}
        if sub=='catalogue/apply-agent' and method=='POST':
            job=rt.get_job(body.get('jobId'))
            if Path(job.get('projectRoot','')).resolve()!=root.resolve(): fail('Job belongs to another project.','forbidden',403)
            return apply_agent_resolutions(self.server.catalogue,pid,body.get('planId'),job)
        if sub=='packages/suggest' and method=='POST': return {'ok':True,**rt.submit_package_suggestions(str(root),body)}
        if sub=='jobs' and method=='POST': return {'ok':True,**rt.submit_job(str(root),body)}
        if sub.startswith('jobs/'):
            job=sub.split('/')[1]
            record=rt.get_job(job)
            if Path(record.get('projectRoot','')).resolve()!=root.resolve(): fail('Job belongs to another project.','forbidden',403)
            if sub.endswith('/evidence') and method=='POST':
                if record.get('state') not in ['passed','failed','cancelled','interrupted']: fail('Wait for the job to finish before recording its result.','job_running',409)
                hashes,missing=rt._tutorial_hashes(root)
                if missing or hashes!=record.get('inputHashes'): fail('Source changed since this job ran. Rerun it before attaching a result to this candidate.','stale_job',409)
                return s.capture_evidence(pid,{'expectedRevision':body.get('expectedRevision'),'label':record.get('kind','Docker job')+' · '+record['state'],'kind':'check','_checks':[{'name':record.get('kind','Docker job'),'status':record['state'],'tool':'docker','job':record}]})
            if sub.endswith('/cancel') and method=='POST': return {'ok':True,**rt.cancel_job(job)}
            return {'ok':True,**rt.get_job(job)}
        fail('Route not found.','not_found',404)
    def _handle(self):
        try:
            result=self._route()
            if result is not None: self._json(200,result)
        except Exception as e:
            if hasattr(e,'status') and hasattr(e,'code'): self._json(e.status,{'ok':False,'error':{'code':e.code,'message':str(e),'details':getattr(e,'details',{})}})
            else:
                traceback.print_exc(); self._json(500,{'ok':False,'error':{'code':'internal','message':'Workspace operation failed. Prior recovery revisions were retained.','details':{}}})
    do_GET=_handle
    do_POST=_handle
    do_PUT=_handle

def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--port',type=int,default=8765); parser.add_argument('--storage-root',default=str(Path.home()/'ros-studio'),help='Where projects, cloned repositories and job output live. Defaults to ~/ros-studio; point it anywhere with real disk space.'); parser.add_argument('--rotate-token',action='store_true',help='Invalidate the previous session URL and issue a new one.'); args=parser.parse_args()
    repo=Path(__file__).resolve().parent.parent; store=StudioStore(args.storage_root,repo)
    try:
        from studio_runtime import StudioRuntime
        runtime=StudioRuntime(repo,args.storage_root)
    except ImportError: runtime=None
    token=_session_token(args.storage_root,rotate=args.rotate_token)
    server=StudioServer(('127.0.0.1',args.port),store,runtime,token=token)
    print('CoreSense Studio: http://127.0.0.1:%d/#token=%s'%(server.server_port,server.token),flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
if __name__=='__main__': main()
