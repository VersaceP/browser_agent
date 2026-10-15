import {observe,attr,sub,norm,state} from './common.js';
import {skuRows} from './sku.js';
function imageSrc(n){if(n.tag==='IMG')return n.attributes?.src;return (String(n.attributes?.style||'').match(/background-image:\s*url\(["']?(https?:[^"')]+)/)||[])[1];}
export default async function main(api,input){
 const pageId=api.context.pageId,a=api.actions,o=await observe(api,pageId,'独立核对标题、图片、属性、规格、库存与价格');
 const facts=await a.DOM.getDetails({pageId,query:{view:'text',targets:['[id="basic.carousel_gallery"]','#detail_pic','#property','[id="basic.explanation_video"]'].map(selector=>({selector})),textMode:'textContent'},purpose:'读取媒体计数、属性与页面上传反馈'});
 const vals=await state(api,pageId,[{selector:'input[placeholder^="商品标题组成"]'},{selector:'input[placeholder="应大于商品最大单买价"]'}],'核对标题和参考价实际值');
 const rows=skuRows(o.rows),report=rows.map(r=>({key:r.key,stock:r.cells['库存']?.value,group:r.cells['拼单价']?.value,single:r.cells['单买价']?.value}));
 const pd=await a.DOM.getDetails({pageId,query:{view:'dom',targets:rows.map(r=>({id:r.cells['预览图'].id})),maxDepth:12,includeShadowDom:false},purpose:'独立核对每个规格组合的实际预览图'});
 const needed=pd.details.filter(d=>d.ok).flatMap(d=>d.value.nodes.filter(n=>String(n.attributes?.class||'').includes('MaterialModalButton_imgContainer')));
 const pa=needed.length?await a.DOM.getDetails({pageId,query:{view:'attributes',targets:needed.map(n=>({id:n.id})),attributes:['style']},purpose:'读取全部规格预览图片的完整背景地址'}):{details:[]};
 const previews=pd.details.map((d,i)=>{if(!d.ok)return {key:rows[i].key,error:d.error};for(const n of d.value.nodes){const v=pa.details.find(v=>v.ok&&v.target.resolvedId===n.id);if(v)n.attributes={...n.attributes,...v.value};}return {key:rows[i].key,images:d.value.nodes.map(imageSrc).filter(Boolean)};});
 const video=await a.DOM.getDetails({pageId,query:{view:'dom',targets:[{selector:'[id="basic.explanation_video"]'}],maxDepth:16,includeShadowDom:false},purpose:'核对上传生成的视频封面与对应播放控件'});
 const videoMedia=(video.details[0]?.value?.nodes||[]).filter(n=>n.tag==='IMG'&&n.attributes?.['data-tracking-click-viewid']==='el_explain_video_preview_play').map(n=>n.attributes.src).filter(Boolean);
 const attributes=input.attributes.map(item=>{for(let i=0;i<o.rows.length;i++){if(!/^basic\.propertys\./.test(attr(o.rows[i].attrs,'id')||''))continue;const rr=sub(o.rows,i);if(rr.some(r=>['labelText','staticText'].includes(r.role)&&norm(r.name)===norm(item.label))){const t=rr.find(r=>r.role==='textField'),value=t?attr(t.state,'value'):null;return {label:item.label,requested:item.value,value,matches:norm(value)===norm(item.value)};}}return {label:item.label,requested:item.value,value:null,matches:false};});
 const count=d=>{const m=String(d?.value).match(/已上传\s*(\d*)\s*\/\s*\d+/);return m?+(m[1]||0):null;};
 const errors=o.rows.filter(r=>r.role==='staticText'&&/不能为空|请上传第|格式.*错误|上传失败/.test(r.name)).map(r=>r.name);
 const expected=input.specTypes.reduce((n,s)=>n*s.values.length,1);
 const checks={title:vals[0]?.value?.state?.value===input.title,reference:String(vals[1]?.value?.state?.value)===String(input.referencePrice),carousel:count(facts.details[0])===input.carouselImages.length,detail:count(facts.details[1])===input.detailImages.length,video:!input.videos?.length||videoMedia.length===input.videos.length,skuCount:rows.length===expected,skuPrices:report.every(r=>String(r.stock)===String(input.stock)&&String(r.group)===String(input.groupPrice)&&String(r.single)===String(input.singlePrice)),previewCount:previews.length===expected&&previews.every(p=>p.images?.length===1),attributes:attributes.every(x=>x.matches)};
 return {ready:Object.values(checks).every(Boolean)&&errors.length===0,checks,pageState:await a.Page.getState({pageId,purpose:'记录最终表单身份与URL'}),values:vals,regions:facts.details,videoMedia,skuRows:report,previews,attributes,errors};
}