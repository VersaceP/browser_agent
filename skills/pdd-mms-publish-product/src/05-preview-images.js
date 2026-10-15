import {observe,norm} from './common.js';
import {skuRows} from './sku.js';
export default async function main(api,input){
 const pageId=api.context.pageId,a=api.actions,out={ready:false,rows:[],blockers:[]};
 async function dom(id){const r=await a.DOM.getDetails({pageId,query:{view:'dom',targets:[{id}],maxDepth:32,includeShadowDom:true},purpose:'读取该规格组合预览图的实际上传输入与完成结果'});if(!r.details[0].ok)throw new Error(r.details[0].error.code);const d=r.details[0].value;const needed=d.nodes.filter(n=>String(n.attributes?.class||'').includes('MaterialModalButton_imgContainer')&&!n.attributes?.style);if(needed.length){const q=await a.DOM.getDetails({pageId,query:{view:'attributes',targets:needed.map(n=>({id:n.id})),attributes:['style']},purpose:'DOM 结构未携带完整背景属性，显式读取图片实际地址'});for(const v of q.details){if(v.ok){const n=d.nodes.find(n=>n.id===v.target.resolvedId);if(n)n.attributes={...n.attributes,...v.value};}}}return d;}
 try{
  let o=await observe(api,pageId,'绑定每个SKU规格组合与所属预览图单元格');let rows=skuRows(o.rows);const keys=rows.map(r=>r.key);
  for(const key of keys){
   o=await observe(api,pageId,'按规格组合重新定位尚未完成的预览图');rows=skuRows(o.rows);const row=rows.find(r=>Object.keys(key).every(k=>norm(r.key[k])===norm(key[k])));
   if(!row)return {...out,blocked:'row-disappeared',key};
   const rules=input.previewImages.filter(rule=>Object.keys(rule.match).every(k=>norm(key[k])===norm(rule.match[k])));
   if(rules.length!==1)return {...out,blocked:'preview-mapping-ambiguous',key,ruleCount:rules.length};
   const cell=row.cells['预览图'];if(!cell)return {...out,blocked:'preview-cell-missing',key};
   await a.Input.scroll({pageId,target:{id:cell.id},purpose:'展示该规格的结果单元格并触发图片懒加载'});
   let d=await dom(cell.id);const images=x=>x.nodes.flatMap(n=>{const src=String(n.tag).toLowerCase()==='img'?n.attributes?.src:(String(n.attributes?.style||'').match(/background-image:\s*url\([\"']?(https?:[^\"')]+)/)||[])[1];return /^(https?:|blob:|data:)/.test(src||'')?[{...n,imageSrc:src}]:[];});
   if(images(d).length){out.rows.push({key,status:'already-present',images:images(d).map(n=>n.imageSrc)});continue;}
   const files=d.nodes.filter(n=>String(n.attributes?.type).toLowerCase()==='file');
   if(files.length!==1)return {...out,blocked:'preview-file-control-unresolved',key,dom:d};
   const assignment=await a.File.handleChooser({pageId,id:files[0].id,files:rules[0].files,purpose:'按实际规格组合上传对应颜色预览图'});
   let matched=false;
   for(let k=0;k<6;k++){await api.sleep(700);o=await observe(api,pageId,'上传后按组合重新绑定已渲染的结果单元格');rows=skuRows(o.rows);const current=rows.find(r=>Object.keys(key).every(j=>norm(r.key[j])===norm(key[j])));if(!current?.cells['预览图'])return {...out,blocked:'preview-row-result-unresolved',key,assignment};await a.Input.scroll({pageId,target:{id:current.cells['预览图'].id},purpose:'上传后的页面可能自动定位错误区，重新展示本行真实图片结果'});d=await dom(current.cells['预览图'].id);if(images(d).length){matched=true;break;}}
   out.rows.push({key,files:rules[0].files,status:matched?'verified':'unconfirmed',assignment});if(!matched)return {...out,blocked:'preview-upload-unconfirmed',key};
  }
  out.ready=out.rows.length===keys.length&&out.rows.every(r=>r.status==='verified'||r.status==='already-present');return out;
 }catch(e){return {...out,error:{message:e.message,code:e.code,actionCode:e.actionCode}};}
}
