"""Dashboard local. Lee la DB y muestra el estado de cada cuenta en vivo.
Corre aparte del motor: `python -m rastrillo.server` y abre http://127.0.0.1:8765
"""
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import db

app = FastAPI(title="Rastrillo")

STATUS_META = {
    "found": ("Pendiente", "#888780"),
    "queued": ("En cola", "#185FA5"),
    "in_progress": ("En proceso", "#BA7517"),
    "awaiting_user": ("Esperándote", "#D85A30"),
    "deleted": ("Eliminada", "#0F6E56"),
    "anonymized": ("Anonimizada", "#534AB7"),
    "manual": ("Manual", "#854F0B"),
    "skipped": ("Conservada", "#5F5E5A"),
    "failed": ("Error", "#A32D2D"),
}


@app.get("/api/accounts")
def api_accounts():
    rows = [dict(r) for r in db.list_accounts()]
    return JSONResponse({"accounts": rows, "stats": db.stats()})


@app.get("/", response_class=HTMLResponse)
def index():
    return """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rastrillo</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#faf9f5;color:#1a1a18}
 header{padding:20px 28px;border-bottom:1px solid #e5e3da}
 h1{font-size:20px;font-weight:500;margin:0}
 .wrap{padding:24px 28px;max-width:900px;margin:0 auto}
 .stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:24px}
 .stat{background:#fff;border:1px solid #e5e3da;border-radius:10px;padding:12px 16px;min-width:90px}
 .stat .n{font-size:24px;font-weight:500}.stat .l{font-size:12px;color:#666}
 .row{display:flex;align-items:center;gap:14px;background:#fff;border:1px solid #e5e3da;border-radius:10px;padding:12px 16px;margin-bottom:8px}
 .row .name{flex:1;font-weight:500}
 .badge{font-size:12px;padding:3px 10px;border-radius:8px;color:#fff}
 .id{font-size:12px;color:#888}
 a{color:#185FA5}
</style></head><body>
<header><h1>Rastrillo · estado en vivo</h1></header>
<div class="wrap"><div class="stats" id="stats"></div><div id="list"></div></div>
<script>
const META=__META__;
async function load(){
 const r=await fetch('/api/accounts');const d=await r.json();
 const s=document.getElementById('stats');s.innerHTML='';
 for(const [k,m] of Object.entries(META)){
   const c=d.stats[k]||0;if(!c&&k!=='deleted'&&k!=='awaiting_user')continue;
   s.innerHTML+=`<div class="stat"><div class="n">${c}</div><div class="l">${m[0]}</div></div>`;
 }
 const l=document.getElementById('list');l.innerHTML='';
 for(const a of d.accounts){
   const m=META[a.status]||['?','#888'];
   const link=a.profile_url?`<a href="${a.profile_url}" target="_blank">perfil</a>`:'';
   l.innerHTML+=`<div class="row"><span class="name">${a.display_name||a.platform}</span>
     <span class="id">${a.identifier||''} ${link}</span>
     <span class="badge" style="background:${m[1]}">${m[0]}</span></div>`;
 }
}
load();setInterval(load,2000);
</script></body></html>""".replace("__META__", _meta_json())


def _meta_json():
    import json
    return json.dumps(STATUS_META)


if __name__ == "__main__":
    import uvicorn
    db.init()
    print("Dashboard en http://127.0.0.1:8765")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
