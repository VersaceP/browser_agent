import {observe,click,attr,norm,state,ready} from './common.js';
export default async function main(api,input){
 const pageId=api.context.pageId,a=api.actions,out={ready:false,title:null,uploads:{},blockers:[]};
 async function text(selector){const r=await a.DOM.getDetails({pageId,query:{view:'text',targets:[{selector}],textMode:'textContent'},purpose:'读取上传完成数量与页面反馈'});return r.details[0];}
 async function section(selector){const r=await a.DOM.getDetails({pageId,query:{view:'dom',targets:[{selector}],maxDepth:24,includeShadowDom:true},purpose:'读取目标上传区域的实际文件输入控件'});const d=r.details[0];if(!d.ok)throw new Error(d.error.code);return d.value;}
 async function upload(key,selector,files,max){
  if(!files?.length)return {status:'not-requested'};
  let d=await text(selector);if(!d.ok)return {status:'section-missing',error:d.error};
  const counter=x=>{const m=String(x).match(/已上传\s*(\d*)\s*\/\s*\d+/);return m?(m[1]===''?0:+m[1]):null;};
  const before=counter(d.value);
  const dom=await section(selector);
  const uploadedVideo=x=>x.nodes.filter(n=>((String(n.tag).toLowerCase()==='video')||(String(n.tag).toLowerCase()==='img'&&n.attributes?.['data-tracking-click-viewid']==='el_explain_video_preview_play'))&&/^(https?:|blob:)/.test(n.attributes?.src||'')&&n.visibility!=='not-rendered');
  if(key==='video'&&uploadedVideo(dom).length)return {status:'already-present',beforeText:d.value,media:uploadedVideo(dom).map(n=>({tag:n.tag,src:n.attributes.src}))};
  if(key!=='video'&&before===files.length)return {status:'already-present',count:before};
  if(key!=='video'&&before!==0)return {status:'needs-reconciliation',count:before,text:d.value};
  const controls=dom.nodes.filter(n=>String(n.attributes?.type).toLowerCase()==='file');
  if(controls.length!==1)return {status:'control-ambiguous',controls:controls.map(n=>({id:n.id,attributes:n.attributes})),text:d.value};
  const ctrl=controls[0],receipt=await a.File.handleChooser({pageId,id:ctrl.id,files,purpose:'上传本轮'+key+'文件到所属控件'});
  for(let i=0;i<30;i++){
   d=await text(selector);
   if(key!=='video'&&d.ok&&counter(d.value)===files.length)return {status:'verified',count:counter(d.value),assignment:receipt};
   if(key==='video'&&i%4===0){const v=await section(selector);if(uploadedVideo(v).length)return {status:'verified',text:d.value,media:uploadedVideo(v).map(n=>({tag:n.tag,src:n.attributes.src})),assignment:receipt};}
   if(d.ok&&/上传失败|不支持|格式错误|大小超出/.test(d.value))return {status:'rejected',text:d.value,assignment:receipt};
   await api.sleep(1000);
  }
  return {status:'unconfirmed',text:d.value,assignment:receipt};
 }
 try{
  const ps=await ready(api,pageId);if(!/goods_add/.test(ps.url||''))return {...out,blocked:'form-required',pageState:ps};
  let o=await observe(api,pageId,'按当前表单结构拒绝一键复用并定位商品标题');
  const decline=o.rows.find(r=>r.role==='button'&&norm(r.name)==='不使用');if(decline){await click(api,pageId,decline.id,'拒绝自动匹配与一键复用');o=await observe(api,pageId,'拒绝复用后定位标题');}
  const title=o.rows.find(r=>r.role==='textField'&&/商品标题组成/.test(r.name));if(!title)return {...out,blocked:'title-missing'};
  const before=attr(title.state,'value');
  if(before!==input.title){await a.Input.type({pageId,id:title.id,text:input.title,clear:true,delayMs:10,purpose:'填写商品信息中的完整标题'});await a.Input.press({pageId,id:title.id,key:'Tab',purpose:'完成标题编辑'});}
  const tt=await state(api,pageId,[{selector:'input[placeholder^="商品标题组成"]'}],'验证商品标题实际值');out.title=tt[0];
  for(const [key,selector,files] of [['carousel','[id="basic.carousel_gallery"]',input.carouselImages],['detail','#detail_pic',input.detailImages],['video','[id="basic.explanation_video"]',input.videos]]){
   try{out.uploads[key]=await upload(key,selector,files);}catch(e){out.uploads[key]={status:'error',message:e.message};}
  }
  out.ready=tt[0]?.ok&&tt[0]?.value?.state?.value===input.title&&Object.values(out.uploads).every(v=>['verified','already-present','not-requested'].includes(v.status));return out;
 }catch(e){return {...out,error:{message:e.message,code:e.code,actionCode:e.actionCode}};}
}
