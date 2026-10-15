export function parseAX(txt){
 return String(txt).split(/\r?\n/).flatMap(line=>{
  const m=line.match(/^\s*(\d+) \[(n_[0-9a-f]{16})\] (\S+)(.*)$/);if(!m)return [];
  const r=m[4],q=r.match(/"(?:\\.|[^"\\])*"/);
  let name='';if(q){try{name=JSON.parse(q[0]);}catch(e){name=q[0].slice(1,-1);}}
  return [{depth:+m[1],id:m[2],role:m[3],name,raw:line,attrs:(r.match(/attrs\{(.*?)\}/)||[])[1]||'',state:(r.match(/state\{(.*?)\}/)||[])[1]||'',flags:(r.match(/\[([^\]]+)\]/)||[])[1]||'',rect:(r.match(/@([\d.,-]+)/)||[])[1]||''}];
 });
}
export function attr(s,k){const m=String(s).match(new RegExp('(?:^|[, ])'+k+'=("(?:\\\\.|[^"\\\\])*"|[^,} ]+)'));if(!m)return null;try{return JSON.parse(m[1]);}catch(e){return m[1];}}
export function sub(rows,i){const d=rows[i].depth;let j=i+1;while(j<rows.length&&rows[j].depth>d)j++;return rows.slice(i+1,j);}
export function norm(x){return String(x==null?'':x).replace(/\s/g,'');}
export async function observe(api,pageId,purpose){const o=await api.actions.DOM.getAXTree({pageId,purpose});const txt=await api.readText(o.artifact);return {rows:parseAX(txt),txt,observation:o};}
export async function ready(api,pageId){for(let i=0;i<20;i++){const s=await api.actions.Page.getState({pageId,purpose:'确认当前阶段页面可操作'});if(s.status==='ready')return s;if(s.status==='crashed'||s.blockingInteractions?.hasPendingDialog||s.hitl?.isPaused)return s;await api.sleep(500);}return {status:'not-ready'};}
export async function click(api,pageId,id,purpose){return api.actions.Input.click({pageId,id,force:false,clickCount:1,purpose});}
export async function state(api,pageId,targets,purpose){const d=await api.actions.DOM.getDetails({pageId,query:{view:'state',targets},purpose});return d.details;}
