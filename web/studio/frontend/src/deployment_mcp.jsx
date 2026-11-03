import React,{useEffect,useState} from 'react';
import {Rocket,ShieldCheck,FileCode,Layers,Hammer,CheckCircle2,AlertTriangle,FolderOpen,RefreshCw,Lock} from 'lucide-react';
import {useStudio,changed,Field} from './state';

export function DeploymentMcpExplorer(){
  useStudio();
  const [mcpStatus,setMcpStatus]=useState(A.mcpStatus||null);
  useEffect(()=>{
    api('/mcp/status').then(r=>{A.mcpStatus=r;setMcpStatus(r);changed()}).catch(()=>{});
  },[]);
  const targetDir=A.env?.promotedTargetDir||(A.env?.root?A.env.root+'/models':'');
  return (
    <div className="inspector-document">
      <h2>ROS 2 Deployment &amp; MCP</h2>
      <p className="mini">
        Coupled directly to <code>rostooling-mcp</code> over headless stdio JSON-RPC 2.0.
      </p>
      <div className="section">
        <strong>MCP Server</strong>
        <p className="mini">
          {mcpStatus?.available
            ? <span className="tag good">Connected · rostooling-mcp</span>
            : <span className="tag warn">Checking rostooling-mcp…</span>}
        </p>
        {mcpStatus?.catalogueDir&&(
          <p className="mini muted">Shared catalogue:<br/><code>{mcpStatus.catalogueDir}</code></p>
        )}
      </div>
      <div className="section">
        <strong>Target Workspace Models</strong>
        <p className="mini"><code style={{wordBreak:'break-all'}}>{targetDir}</code></p>
      </div>
      <div className="section">
        <strong><Lock size={13}/> Domain Logic Protection Shield</strong>
        <p className="mini muted">
          Existing <code>*Algorithm.hpp</code> and <code>*_logic.py</code> files are cryptographically verified and never overwritten during wrapper or system code regeneration.
        </p>
      </div>
    </div>
  );
}

export function DeploymentMcpView(){
  useStudio();
  const [language,setLanguage]=useState(A.session?.mcpLanguage||'cpp');
  const [rosDistro,setRosDistro]=useState(A.session?.mcpDistro||'humble');
  const [workspaceDir,setWorkspaceDir]=useState(A.session?.mcpWorkspaceDir||'');
  const [packageName,setPackageName]=useState('');
  const [busyStep,setBusyStep]=useState('');
  const [lastOutput,setLastOutput]=useState(A.lastMcpOutput||null);
  const [errorText,setErrorText]=useState('');

  async function runStep(stepKey,endpoint,payload={}){
    if(!await flush())return;
    setBusyStep(stepKey);
    setErrorText('');
    try{
      const pid=A.env.projectId;
      const body={...payload};
      if(workspaceDir.trim())body.workspaceDir=workspaceDir.trim();
      const res=await api(`/projects/${pid}/${endpoint}`,{method:'POST',body});
      const out={step:stepKey,timestamp:new Date().toISOString(),response:res};
      A.lastMcpOutput=out;
      setLastOutput(out);
      await poll();
      changed();
    }catch(e){
      setErrorText(e.message||String(e));
    }finally{
      setBusyStep('');
    }
  }

  const defaultWs=A.env?.promotedTargetDir
    ? A.env.promotedTargetDir.replace(/\/models$/,'')
    : (A.env?.root||'');

  return (
    <div className="intent-document">
      <div className="page-heading">
        <div>
          <h1>Stage 5 · ROS 2 Codegen &amp; Workspace Build</h1>
          <p>Materialize grammar-compliant DSL models, generate node wrappers &amp; launch compositions into <code>src-gen/</code>, and run <code>colcon build</code> via <code>rostooling-mcp</code>.</p>
        </div>
        <button
          className="primary"
          disabled={!!busyStep}
          onClick={()=>runStep('promote','generate/models',workspaceDir.trim()?{targetDir:workspaceDir.trim()+'/models'}:{})}
        >
          <FolderOpen size={15}/> {busyStep==='promote'?'Promoting…':'Promote to Models'}
        </button>
      </div>

      <div className="grid2">
        <Field label="Target ROS 2 workspace directory (optional override)" hint={`Defaults to ${defaultWs}`}>
          <input
            value={workspaceDir}
            placeholder={defaultWs}
            onChange={e=>{setWorkspaceDir(e.target.value);A.session.mcpWorkspaceDir=e.target.value;sessionEdit()}}
          />
        </Field>
        <div className="grid2 compact">
          <Field label="Wrapper language">
            <select
              value={language}
              onChange={e=>{setLanguage(e.target.value);A.session.mcpLanguage=e.target.value;sessionEdit()}}
            >
              <option value="cpp">C++ (rclcpp)</option>
              <option value="python">Python (rclpy)</option>
              <option value="both">Hybrid (C++ &amp; Python)</option>
            </select>
          </Field>
          <Field label="ROS 2 distribution">
            <select
              value={rosDistro}
              onChange={e=>{setRosDistro(e.target.value);A.session.mcpDistro=e.target.value;sessionEdit()}}
            >
              <option value="humble">Humble</option>
              <option value="jazzy">Jazzy</option>
              <option value="iron">Iron</option>
              <option value="rolling">Rolling</option>
            </select>
          </Field>
        </div>
      </div>

      <div className="grid2" style={{marginTop:'1rem',gap:'0.9rem'}}>
        <section className="panel">
          <h3><ShieldCheck size={16}/> 1. Validate Models (LSP)</h3>
          <p className="mini">Run Xtext Language Server diagnostics over materialized <code>.ros</code>, <code>.ros2</code>, and <code>.rossystem</code> artifacts.</p>
          <button disabled={!!busyStep} onClick={()=>runStep('validate','mcp/validate')}>
            {busyStep==='validate'?<><RefreshCw size={14}/> Validating…</>:'Run validate_ros_model'}
          </button>
        </section>

        <section className="panel">
          <h3><FileCode size={16}/> 2. Generate Node Wrappers</h3>
          <p className="mini">Emit ROS 2 node shells, CMakeLists/setup.py, and starter algorithm files into <code>src-gen/</code> without touching existing domain logic.</p>
          <button disabled={!!busyStep} onClick={()=>runStep('wrappers','mcp/generate-wrappers',{language,rosDistro})}>
            {busyStep==='wrappers'?<><RefreshCw size={14}/> Generating wrappers…</>:'Run generate_ros2_wrappers'}
          </button>
        </section>

        <section className="panel">
          <h3><Layers size={16}/> 3. Generate System Launch</h3>
          <p className="mini">Emit <code>&lt;system&gt;.launch.py</code>, ComposableNodeContainer wiring, and parameter YAML configs into <code>src-gen/</code>.</p>
          <button disabled={!!busyStep} onClick={()=>runStep('system','mcp/generate-system')}>
            {busyStep==='system'?<><RefreshCw size={14}/> Generating launch…</>:'Run generate_rossystem_code'}
          </button>
        </section>

        <section className="panel">
          <h3><Hammer size={16}/> 4. Build ROS 2 Workspace</h3>
          <p className="mini">Compile generated packages with <code>colcon build --symlink-install</code>.</p>
          <div className="row" style={{gap:'0.5rem',marginTop:'0.35rem'}}>
            <input
              placeholder="All packages (or package name)"
              value={packageName}
              onChange={e=>setPackageName(e.target.value)}
              style={{flex:1}}
            />
            <button className="primary" disabled={!!busyStep} onClick={()=>runStep('build','mcp/build',{packageName})}>
              {busyStep==='build'?<><RefreshCw size={14}/> Building…</>:'Run colcon build'}
            </button>
          </div>
        </section>
      </div>

      {errorText&&(
        <section className="panel" style={{marginTop:'1rem',borderColor:'#c94a4a'}}>
          <h3><AlertTriangle size={16}/> Stage 5 Error</h3>
          <pre style={{whiteSpace:'pre-wrap'}}>{errorText}</pre>
        </section>
      )}

      {lastOutput&&(
        <section className="panel" style={{marginTop:'1rem'}}>
          <div className="row between">
            <h3><CheckCircle2 size={16}/> Last MCP Execution · <code>{lastOutput.step}</code></h3>
            <small className="muted">{lastOutput.timestamp}</small>
          </div>
          {Array.isArray(lastOutput.response?.protectedDomainFiles)&&lastOutput.response.protectedDomainFiles.length>0&&(
            <p className="mini">
              <Lock size={12}/> Protected domain logic files preserved untouched ({lastOutput.response.protectedDomainFiles.length}):{' '}
              <code>{lastOutput.response.protectedDomainFiles.join(', ')}</code>
            </p>
          )}
          <pre style={{maxHeight:'340px',overflow:'auto',whiteSpace:'pre-wrap'}}>
            {JSON.stringify(lastOutput.response,null,2)}
          </pre>
        </section>
      )}
    </div>
  );
}
