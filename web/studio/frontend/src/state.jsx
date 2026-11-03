import React,{useSyncExternalStore} from 'react';
import {createRoot} from 'react-dom/client';
export const roots=new Map();let version=0;const listeners=new Set();
export function changed(){version++;for(const fn of listeners)fn()}
export function useStudio(){useSyncExternalStore(fn=>{listeners.add(fn);return()=>listeners.delete(fn)},()=>version);return A}
export function mount(id,component){const element=document.getElementById(id);if(!element)return;let entry=roots.get(id);if(entry?.element!==element){entry?.root.unmount();entry={element,root:createRoot(element)};roots.set(id,entry)}entry.root.render(component)}
export function release(...ids){for(const id of ids){const r=roots.get(id);if(r){r.root.unmount();roots.delete(id)}}}
export function mutate(fn){remember();fn();edit();changed()}
let past=[],future=[],owner=null;
function remember(){if(owner!==A.env?.projectId){owner=A.env?.projectId;past=[];future=[]}past.push(clone(A.project));if(past.length>80)past.shift();future=[]}
export function undo(){if(!past.length)return;future.push(clone(A.project));A.project=past.pop();edit();render()}
export function redo(){if(!future.length)return;past.push(clone(A.project));A.project=future.pop();edit();render()}
export const kinds={pub:{name:'Publisher',family:'Topic',side:'source',partner:'sub'},sub:{name:'Subscriber',family:'Topic',side:'target',partner:'pub'},ss:{name:'Service server',family:'Service',side:'source',partner:'sc'},sc:{name:'Service client',family:'Service',side:'target',partner:'ss'},as:{name:'Action server',family:'Action',side:'source',partner:'ac'},ac:{name:'Action client',family:'Action',side:'target',partner:'as'}};
export const roles={Sensing:['Sensing','Acquire raw sensor observations from hardware or simulation.'],Perception:['Perception','Detect, segment, localize, and interpret sensory streams.'],'World Modeling':['World Modeling','Fuse state estimates, maps, scene graphs, and belief state.'],Planning:['Planning','Synthesize tasks, paths, trajectories, and behavior plans.'],Control:['Control','Compute closed-loop tracking and feedback control commands.'],Actuation:['Actuation','Drive motors, grippers, bases, and hardware interfaces.'],Communication:['Communication','Bridge messages, operator UIs, fleet links, and external APIs.'],Unassigned:['Unassigned','Choose a CoreSense pipeline role when it helps explain the architecture.']};
export function moduleName(n){return `${n.label} · ${[...new Set(n.ifaces.map(f=>kinds[f.kind]?.name))].join(', ')||'No interfaces yet'}`}
export function go(view,tab){changeView(view,tab).catch(e=>banner(e.message))}
export function IconButton({icon:Icon,label,onClick,active=false,...props}){return <button type="button" className={'icon-button '+(active?'active':'')} title={label} aria-label={label} onClick={onClick} {...props}><Icon size={17}/></button>}
export function Field({label,children,hint}){return <label className="field"><span>{label}</span>{children}{hint&&<small className="muted">{hint}</small>}</label>}
export function valueText(value){if(value===null||value===undefined||value==='')return 'Not specified';if(typeof value==='boolean')return value?'Yes':'No';return String(value)}
