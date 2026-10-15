import {observe,click,attr,sub,norm} from './common.js';
function findRow(rows,label){for(let i=0;i<rows.length;i++){if(/^basic\.propertys\./.test(attr(rows[i].attrs,'id')||'')){const rr=sub(rows,i);if(rr.some(r=>(r.role==='labelText'||r.role==='staticText')&&norm(r.name)===norm(label)))return {root:rows[i],rows:rr,input:rr.find(r=>r.role==='textField'),header:rr.find(r=>attr(r.attrs,'data-testid')==='beast-core-select-header')};}}return null;}
export default async function main(api,input){
 const pageId=api.context.pageId,a=api.actions,out={ready:false,fields:[],blockers:[]};
 let initial=await observe(api,pageId,'恢复属性阶段：识别上轮主动打开的候选弹层');
 if(initial.rows.some(r=>r.role==='listBox')){const title=initial.rows.find(r=>r.role==='textField'&&/商品标题组成/.test(r.name));if(title)await click(api,pageId,title.id,'关闭上轮属性候选，保持已有字段值');}
 for(const item of input.attributes){
  let o=await observe(api,pageId,'绑定当前商品属性标签与对应控件'),row=findRow(o.rows,item.label);
  if(!row?.input){out.fields.push({label:item.label,status:'control-missing'});out.blockers.push(item.label);continue;}
  let val=attr(row.input.state,'value');
  if(norm(val)===norm(item.value)){out.fields.push({label:item.label,status:'already-present',value:val});continue;}
  try{
   if(!row.header){await a.Input.type({pageId,id:row.input.id,text:item.value,clear:true,delayMs:15,purpose:'填写商品属性'+item.label});await a.Input.press({pageId,id:row.input.id,key:'Tab',purpose:'完成属性文本编辑'});}
   else{
    await click(api,pageId,row.header.id,'展开'+item.label+'属性候选');
    if(/请输入.*搜索/.test(row.input.name)){o=await observe(api,pageId,'搜索属性选项前绑定当前输入框');row=findRow(o.rows,item.label);await a.Input.type({pageId,id:row.input.id,text:item.value,clear:true,delayMs:20,purpose:'搜索属性值'+item.value});}
    let option;
    let last='',visibleAnchor=false;
    for(let k=0;k<16;k++){o=await observe(api,pageId,'在当前候选窗口匹配所需属性值');option=o.rows.find(r=>r.role==='listBoxOption'&&(norm(r.name)===norm(item.value)||norm(r.name)===norm(item.value)+'季'));if(option)break;const li=o.rows.findIndex(r=>r.role==='listBox'&&r.rect);const list=li>=0?sub(o.rows,li):[];const scroll=list.find(r=>r.raw.includes('scroll{can=')&&r.rect);const signature=list.filter(r=>r.role==='listBoxOption').map(r=>r.name).join('|');if(!scroll||signature===last)break;last=signature;const coordinateScroll=async()=>{const first=list.find(r=>r.role==='listBoxOption'&&r.rect&&!/vis=/.test(r.raw));if(!first)throw new Error('候选列表缺少已确认可见锚点');const b=first.rect.split(',').map(Number);await a.Input.scroll({pageId,x:b[0]+b[2]/2,y:b[1]+b[3]/2,deltaY:196,purpose:'使用当前可见候选内的真实滚轮锚点'});};
     if(visibleAnchor){await coordinateScroll();}else{try{await a.Input.scroll({pageId,container:{id:scroll.id},deltaY:196,purpose:'沿当前候选列表查找尚未挂载的属性值'});}catch(e){if(!String(e.message).includes('scroll-anchor-unavailable'))throw e;visibleAnchor=true;out.recoveries=(out.recoveries||[]).concat({label:item.label,action:'Input.scroll',error:e.message,anchor:'current-first-visible-option'});await coordinateScroll();}}}

    if(!option){out.fields.push({label:item.label,status:'option-not-found',requested:item.value,options:o.rows.filter(r=>r.role==='listBoxOption').map(r=>r.name)});out.blockers.push(item.label);const title=o.rows.find(r=>r.role==='textField'&&/商品标题组成/.test(r.name));if(title)await click(api,pageId,title.id,'关闭本段未匹配的属性候选并保留原有值');continue;}
    await click(api,pageId,option.id,'选择'+item.label+'='+item.value);
   }
   out.fields.push({label:item.label,status:'written',requested:item.value});
  }catch(e){out.fields.push({label:item.label,status:'error',message:e.message});out.blockers.push(item.label);return out;}
 }
 const shot=await a.Page.screenshot({pageId,selector:'#property',fullPage:false,options:{format:'file'},purpose:'确认已选择的商品属性在所属表单行中的实际展示'});out.screenshot=shot;out.ready=out.blockers.length===0;return out;
}
