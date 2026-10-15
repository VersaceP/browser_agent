import {observe,ready,click,sub,norm} from './common.js';
export default async function main(api,input){
 const pageId=api.context.pageId,fleetId=api.context.fleetId,a=api.actions,out={ready:false,dismissed:[],phase:'home'};
 try{
  let form=input.resumePageId; let s,o;
  if(!form){
  s=await ready(api,pageId);if(s.status!=='ready')return {...out,state:s};
  if(!/mms\.pinduoduo\.com\/home(?:\/|$)/.test(s.url)){await a.Page.navigate({pageId,url:input.homeUrl,purpose:'打开用户指定商家后台首页'});await ready(api,pageId);}
  o=await observe(api,pageId,'识别登录与首页弹窗，并直接执行已知发布入口');
  if(o.rows.some(r=>r.role==='textField'&&/password/.test(r.attrs)))return {...out,blocked:'login-required'};
  out.loggedIn=/主账号/.test(o.txt);
  if(!out.loggedIn)return {...out,blocked:'login-unconfirmed',evidence:o.txt.slice(0,1500)};
  for(let k=0;k<4;k++){
   const close=o.rows.find(r=>/actionable/.test(r.flags)&&((r.role==='button'&&['稍后再说','我知道了','知道了','跳过','暂不体验'].includes(r.name.trim()))||/icon-close/.test(r.attrs)));
   if(!close)break;await click(api,pageId,close.id,'关闭首页提示弹窗');out.dismissed.push(close.name||close.id);o=await observe(api,pageId,'确认关闭后剩余弹窗与入口');
  }
  const entry=o.rows.find(r=>r.role==='link'&&norm(r.name)==='发布新商品'&&/actionable/.test(r.flags));
  if(!entry)return {...out,blocked:'entry-missing'};
  const before=await a.Page.list({fleetId});
  await click(api,pageId,entry.id,'在侧边栏点击发布新商品');
  let p;
  for(let k=0;k<20;k++){const ps=await a.Page.list({fleetId});p=ps.find(p=>!before.some(q=>q.pageId===p.pageId)&&/goods\/category/.test(p.currentUrl||''));if(p)break;await api.sleep(500);}
  if(!p)return {...out,blocked:'new-page-unconfirmed'};
  form=p.pageId;
  }
  out.pageId=form;out.phase='category';s=await ready(api,form);
  if(/\/login\//.test(s.url||''))return {...out,blocked:'login-required',pageState:s};
  if(input.resumePageId&&/goods_add/.test(s.url||'')){o=await observe(api,form,'恢复已有表单并保留已完成的类目动作');return {...out,ready:o.rows.some(r=>r.role==='textField'&&/商品标题组成/.test(r.name)),phase:'form',resumed:true,formUrl:s.url};}
  let box;
  for(let k=0;k<12;k++){o=await observe(api,form,'等待类目业务区域加载并定位关键词输入框');box=o.rows.find(r=>r.role==='textField'&&/关键词搜索/.test(r.name+r.attrs));if(box)break;if(/加载中/.test(o.txt)){await api.sleep(1000);continue;}break;}
  box=box||o.rows.find(r=>r.role==='textField'&&/关键词搜索/.test(r.name+r.attrs));if(!box)return {...out,blocked:'category-input-missing'};
  await a.Input.type({pageId:form,id:box.id,text:input.categoryKeyword,clear:true,delayMs:20,purpose:'搜索用户要求的商品类目'});
  await click(api,form,box.id,'展开分类搜索候选');
  for(let k=0;k<6;k++){o=await observe(api,form,'匹配分类搜索候选');if(o.rows.some(r=>r.role==='listItem'))break;await api.sleep(500);}
  const pi=o.rows.findIndex(r=>r.role==='list'&&/search-panel/.test(r.name));const items=(pi>=0?sub(o.rows,pi):o.rows).filter(r=>r.role==='listItem');
  const ds=await a.DOM.getDetails({pageId:form,query:{view:'text',targets:items.slice(0,32).map(r=>({id:r.id})),textMode:'textContent'},purpose:'匹配完整分类路径'});
  const chosen=ds.details.find(d=>d.ok&&input.categoryPath.every(x=>norm(d.value).includes(norm(x))));
  if(!chosen)return {...out,blocked:'category-not-found',candidates:ds.details};
  out.categoryText=chosen.value;await click(api,form,chosen.target.requestedId,'选择女装/汉服/汉服套装');
  o=await observe(api,form,'定位确认发布该类商品');const btn=o.rows.find(r=>r.role==='button'&&norm(r.name)==='确认发布该类商品');
  if(!btn)return {...out,blocked:'category-confirm-missing'};
  await click(api,form,btn.id,'确认发布该类商品');
  for(let k=0;k<20;k++){s=await ready(api,form);if(/goods_add/.test(s.url||''))break;await api.sleep(500);}
  for(let k=0;k<12;k++){o=await observe(api,form,'确认表单业务控件已加载');if(o.rows.some(r=>r.role==='textField'&&/商品标题组成/.test(r.name)))break;if(!o.observation.pendingChanges&&!/加载中/.test(o.txt))break;await api.sleep(700);}out.ready=/goods_add/.test(s.url||'')&&o.rows.some(r=>r.role==='textField'&&/商品标题组成/.test(r.name));out.formUrl=s.url;out.formObservation=o.observation;
  out.formSummary=o.rows.filter(r=>r.role==='textField'||/type="file"/.test(r.attrs)||r.role==='heading').map(r=>({id:r.id,role:r.role,name:r.name,attrs:r.attrs,state:r.state}));
  return out;
 }catch(e){return {...out,error:{message:e.message,code:e.code,actionCode:e.actionCode}};}
}
