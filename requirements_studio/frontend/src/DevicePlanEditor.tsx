import {useState,type FormEvent} from 'react';
import {Check,Loader2} from 'lucide-react';
import {intentLabels,type DeviceReference,type DomainAction} from './types';

export function DevicePlanEditor({reference,onSubmit,onCancel}:{reference:DeviceReference;onSubmit:(action:DomainAction)=>Promise<boolean>;onCancel:()=>void}){
 const [model,setModel]=useState(reference.mention),[intent,setIntent]=useState(reference.intent),[quantity,setQuantity]=useState(reference.quantity===null?'':String(reference.quantity)),[purpose,setPurpose]=useState(reference.purpose),[sending,setSending]=useState(false);
 const action:DomainAction={operation:'revise_reference',reference_id:reference.id};
 if(model.trim()!==reference.mention)action.mention=model.trim();
 if(intent!==reference.intent)action.intent=intent;
 if(quantity!==(reference.quantity===null?'':String(reference.quantity)))action.quantity=quantity===''?null:Number(quantity);
 if(purpose.trim()!==reference.purpose)action.purpose=purpose.trim();
 const changed=Object.keys(action).length>2;
 async function save(event:FormEvent){event.preventDefault();if(sending||!changed||!model.trim())return;setSending(true);try{if(await onSubmit(action))onCancel();}finally{setSending(false);}}
 return <form className="device-plan-editor" onSubmit={e=>void save(e)} aria-label={'修改器件方案 '+reference.mention}>
  <p>修改当前方案，AI 会同步核对说明书、模块、接口与器件清单。</p>
  <label>器件型号<input name="mention" value={model} maxLength={160} required disabled={sending} onChange={e=>setModel(e.target.value)}/></label>
  <div className="device-edit-grid"><label>使用意图<select name="intent" value={intent} disabled={sending} onChange={e=>setIntent(e.target.value as DeviceReference['intent'])}>{Object.entries(intentLabels).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label><label>本版实际用量<input name="quantity" type="number" min="1" max="100000" step="1" placeholder="待核定" value={quantity} disabled={sending} onChange={e=>setQuantity(e.target.value)}/></label></div>
  <label>在本版中的用途<textarea name="purpose" rows={3} maxLength={1000} value={purpose} disabled={sending} onChange={e=>setPurpose(e.target.value)}/></label>
  <footer><button type="button" className="text-button" disabled={sending} onClick={onCancel}>取消</button><button type="submit" className="button primary small save-device-plan" disabled={sending||!changed||!model.trim()}>{sending?<Loader2 size={13} className="spin"/>:<Check size={13}/>}提交修改并同步</button></footer>
 </form>;
}
