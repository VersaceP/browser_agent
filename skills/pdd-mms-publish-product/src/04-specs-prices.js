import {observe,click,attr,sub,norm,state} from './common.js';
export function skuRows(rows){
 const si=rows.findIndex(r=>attr(r.attrs,'id')==='sku');if(si<0)return [];const area=sub(rows,si),head=area.filter(r=>r.role==='columnHeader');const columns=head.map(r=>({name:norm(r.name).replace(/\*/g,'').replace(/\(元\)/g,''),x:+r.rect.split(',')[0]}));let inherited={};const out=[];
 for(let i=0;i<area.length;i++){if(area[i].role!=='row')continue;const rr=sub(area,i);if(rr.some(r=>r.role==='columnHeader'))continue;const cells=rr.filter(r=>r.role==='cell');if(!cells.length)continue;const rec={key:{},cells:{},id:area[i].id};for(const c of cells){const ci=area.indexOf(c),members=sub(area,ci),col=columns.find(h=>Math.abs(h.x-(+c.rect.split(',')[0]))<3);if(!col)continue;const inp=members.find(r=>r.role==='textField');rec.cells[col.name]={id:c.id,input:inp,value:inp?attr(inp.state,'value'):c.name,rows:members};if(!['库存','拼单价','单买价','预览图','规格编码','状态'].includes(col.name))inherited[col.name]=c.name;}rec.key={...inherited};out.push(rec);}
 return out;
}
export default async function main(api,input){
 const pageId=api.context.pageId,a=api.actions,out={ready:false,specs:[],rows:[],blockers:[]};
 try{
  for(let g=0;g<input.specTypes.length;g++){
   const req=input.specTypes[g];let o=await observe(api,pageId,'定位第'+(g+1)+'种规格类型');let t=o.rows.find(r=>r.role==='textField'&&r.name==='规格类型'+(g+1));
   if(!t)return {...out,blocked:'spec-type-control-missing',group:g};
   if(norm(attr(t.state,'value'))!==norm(req.name)){
    await click(api,pageId,t.id,'选择规格类型'+req.name);o=await observe(api,pageId,'匹配规格类型候选');const option=o.rows.find(r=>r.role==='listBoxOption'&&norm(r.name)===norm(req.name));if(!option)return {...out,blocked:'spec-type-option-missing',requested:req.name};await click(api,pageId,option.id,'应用规格类型'+req.name);
   }
   for(const value of req.values){
    o=await observe(api,pageId,'按当前规格组填写尚未存在的值');const start=o.rows.findIndex(r=>r.role==='textField'&&r.name==='规格类型'+(g+1));let end=o.rows.findIndex((r,i)=>i>start&&((r.role==='textField'&&/^规格类型/.test(r.name))||attr(r.attrs,'id')==='sku'));if(end<0)end=o.rows.length;
    const fields=o.rows.slice(start,end).filter(r=>r.role==='textField'&&r.name==='请输入规格名称');
    if(fields.some(r=>norm(attr(r.state,'value'))===norm(value)))continue;
    let slot=fields.find(r=>attr(r.state,'value')==='');if(!slot)return {...out,blocked:'spec-empty-slot-missing',type:req.name,requested:value};
    await click(api,pageId,slot.id,'聚焦待填规格值');o=await observe(api,pageId,'聚焦后绑定当前规格空输入框');slot=o.rows.find(r=>r.id===slot.id)||o.rows.slice(start,end).find(r=>r.role==='textField'&&r.name==='请输入规格名称'&&attr(r.state,'value')==='');
    await a.Input.type({pageId,id:slot.id,text:value,clear:true,delayMs:25,purpose:'填写'+req.name+'规格值'+value});await a.Input.press({pageId,id:slot.id,key:'Tab',purpose:'提交本次规格值并更新SKU组合'});
   }
   out.specs.push({type:req.name,values:req.values});
  }
  let o=await observe(api,pageId,'读取规格组合与批量库存价格输入');let rows=skuRows(o.rows);const count=input.specTypes.reduce((n,s)=>n*s.values.length,1);
  if(rows.length!==count)return {...out,blocked:'sku-count-mismatch',expected:count,actual:rows.length,observedKeys:rows.map(r=>r.key)};
  const correct=rows.every(r=>String(r.cells['库存']?.value)===String(input.stock)&&String(r.cells['拼单价']?.value)===String(input.groupPrice)&&String(r.cells['单买价']?.value)===String(input.singlePrice));
  if(!correct){
   for(const [name,value] of [['库存',input.stock],['拼单价',input.groupPrice],['单买价',input.singlePrice]]){await a.Input.type({pageId,selector:'#sku input[placeholder="'+name+'"]',text:String(value),clear:true,delayMs:10,purpose:'设置所有组合的'+name});await a.Input.press({pageId,selector:'#sku input[placeholder="'+name+'"]',key:'Tab',purpose:'完成批量值编辑'});}
   o=await observe(api,pageId,'定位价格库存批量设置按钮');const si=o.rows.findIndex(r=>attr(r.attrs,'id')==='sku'),apply=sub(o.rows,si).find(r=>r.role==='button'&&norm(r.name)==='批量设置');if(!apply)return {...out,blocked:'batch-apply-missing'};await click(api,pageId,apply.id,'将库存500及两种价格应用到所有已核对的规格组合');
  }
  await a.Input.type({pageId,selector:'input[placeholder="应大于商品最大单买价"]',text:String(input.referencePrice),clear:true,delayMs:10,purpose:'填写商品参考价'});await a.Input.press({pageId,selector:'input[placeholder="应大于商品最大单买价"]',key:'Tab',purpose:'完成参考价编辑'});
  o=await observe(api,pageId,'验证全部规格组合的真实库存与价格');rows=skuRows(o.rows);out.rows=rows.map(r=>({key:r.key,stock:r.cells['库存']?.value,group:r.cells['拼单价']?.value,single:r.cells['单买价']?.value}));
  out.reference=await state(api,pageId,[{selector:'input[placeholder="应大于商品最大单买价"]'}],'验证参考价实际值');out.ready=rows.length===count&&out.rows.every(r=>String(r.stock)===String(input.stock)&&String(r.group)===String(input.groupPrice)&&String(r.single)===String(input.singlePrice));return out;
 }catch(e){return {...out,error:{message:e.message,code:e.code,actionCode:e.actionCode}};}
}
