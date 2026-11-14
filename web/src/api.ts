export async function api(path:string,method='GET',body?:unknown){
 const r=await fetch('/api'+path,{method,headers:{'Content-Type':'application/json','X-OnQ-Client':'dashboard'},body:body===undefined?undefined:JSON.stringify(body)});
 if(!r.ok){const e=await r.json().catch(()=>({detail:'Request failed'}));throw Error(e.detail||'Request failed');}return r.json();
}
