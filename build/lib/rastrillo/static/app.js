/* eslint-disable */
const META = window.__RASTRILLO_BOOT__.META;
const $=id=>document.getElementById(id);

/* ── Estado global de la UI ───────────────────────────────── */
let CURRENT_FILTER="all";

/* Tono visual de cada estado (clases CSS, desacoplado de colores backend). */
const TONE={
  found:"", queued:"info", in_progress:"warn",
  awaiting_user:"warn", deleted:"success", anonymized:"indigo",
  user_done:"success",
  semi_auto:"accent", email_draft:"indigo",
  manual:"warn", skipped:"", failed:"danger",
  not_mine:"", dry_run:"indigo",
};
/* Tono del badge de confianza. */
const CONF_TONE={ high:"success", medium:"warn", low:"danger" };
const CONF_LABEL={ high:"Confianza alta", medium:"Confianza media", low:"Confianza baja" };

/* Agrupación de estados por intención del usuario.
 *   triage = found sin confirmar dueño (de discovery, esperando triage)
 *   pending/action/done/kept/discarded son los otros apartados
 */
const GROUPS={
  pending  :["queued","in_progress"],
  your_turn:["semi_auto","email_draft","awaiting_user"],   /* 1 clic / 1 envío / 1 confirmación */
  action   :["semi_auto","email_draft","awaiting_user","manual","failed"],
  done     :["deleted","anonymized","user_done","dry_run"],
  kept     :["skipped"],
  discarded:["not_mine"],
};
/* Helper para distinguir filas de exposición (HIBP no confirmada). El filtro
 * "exposure" se basa en SOURCE, no en STATUS, así que tiene su propio camino. */
function isExposure(a){
  return (a.source === "hibp") && (a.status === "found");
}
const SHOW_MSG=new Set(["awaiting_user","manual","failed","skipped","semi_auto",
                        "email_draft","user_done","dry_run","not_mine"]);

/* ── Auth: token del proceso ─────────────────────────────────────
 * El backend genera un token aleatorio en cada arranque. Lo recibimos en la
 * URL (?token=...) al primer load y lo guardamos en sessionStorage. Todos
 * los POST lo mandan en X-Rastrillo-Token. Sin token mostramos una página
 * explicativa con un input para pegarlo manualmente. */
let _TOKEN=(function(){
  try{
    const u=new URL(location.href);
    const t=u.searchParams.get("token");
    if(t){
      sessionStorage.setItem("rastrillo-token", t);
      // limpiamos la URL para no dejarlo expuesto en el historial
      history.replaceState({}, "", u.pathname);
    }
    return sessionStorage.getItem("rastrillo-token") || "";
  }catch(e){ return ""; }
})();

/* ── Iconos SVG (lucide-like, 1.8px stroke) ───────────────── */
const ICONS={
  refresh:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/></svg>',
  sparkles:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/></svg>',
  external:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6"/><path d="M20 4 10 14"/><path d="M14 14v6H4V4h6"/></svg>',
  mail:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/></svg>',
  copy:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
  check:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 5 5L20 7"/></svg>',
  x:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M6 18 18 6"/></svg>',
  inbox:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v7"/><path d="M3 12h6l2 3h2l2-3h6"/><path d="M3 12v6a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-6"/></svg>',
  filter:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h18M6 12h12M10 20h4"/></svg>',
  sun:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>',
  moon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
};
const ic=n=>ICONS[n]||"";

/* ── Utils ─────────────────────────────────────────────── */
function toast(msg, ms, kind){
  const t=$("toast"); t.textContent=msg;
  t.classList.add("on"); t.classList.toggle("err", kind==="err");
  clearTimeout(toast._t); toast._t=setTimeout(()=>t.classList.remove("on"), ms||2400);
}
function splitInput(s){return s.split(/[,\n]/).map(x=>x.trim()).filter(Boolean)}
function escapeHtml(s){return (s||"").replace(/[&<>"']/g,c=>(
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function escapeAttr(s){return (s||"").replace(/"/g,"&quot;").replace(/</g,"&lt;")}
function linkify(s){
  return (s||"").replace(/(https?:\/\/[^\s<>"']+)/g,
    '<a href="$1" target="_blank" rel="noreferrer">$1</a>');
}
async function postJSON(url, body){
  const r=await fetch(url,{method:"POST",headers:{
      "Content-Type":"application/json",
      "X-Rastrillo-Token": _TOKEN,
    },
    body:JSON.stringify(body||{})});
  const data=await r.json().catch(()=>({}));
  if(!r.ok){
    // 412: el server pide confirmación de propiedad. Subimos `detail` como
    // objeto para que el caller decida si abre el modal pre-vuelo.
    if(r.status===412 && data && typeof data.detail==="object"){
      const err=new Error(data.detail.message||"Confirma propiedad");
      err.preflight=data.detail;
      throw err;
    }
    const msg = (typeof data.detail==="string") ? data.detail : (data.detail||r.statusText);
    throw new Error(typeof msg==="string"?msg:JSON.stringify(msg));
  }
  return data;
}
/* GET autenticado a /api/*: TODOS los GET de la API exigen token desde
 * Tarea 3 (no solo POST). Centralizamos el fetch para que ningún caller
 * olvide el header X-Rastrillo-Token. */
async function getAPI(url){
  return fetch(url, {headers:{"X-Rastrillo-Token": _TOKEN}});
}
async function getJSON(url){
  const r = await getAPI(url);
  return r.json();
}

/* ── Filtros ───────────────────────────────────────────── */
function setFilter(name){
  CURRENT_FILTER=name;
  document.querySelectorAll(".filter").forEach(el=>{
    el.classList.toggle("on", el.dataset.f===name);
    el.setAttribute("aria-selected", el.dataset.f===name ? "true" : "false");
  });
  load();
}
function filterAccounts(acc){
  if(CURRENT_FILTER==="all")
    return acc.filter(a=>a.status!=="not_mine" && !isExposure(a));
  if(CURRENT_FILTER==="triage")
    return acc.filter(a=>a.status==="found" && !a.owned && !isExposure(a));
  if(CURRENT_FILTER==="exposure")
    return acc.filter(isExposure);
  const set=new Set(GROUPS[CURRENT_FILTER]||[]);
  return acc.filter(a=>set.has(a.status));
}

/* ── Render: avatar / badge / row / actions ─────────── */
function avatarFor(a){
  const label=(a.display_name||a.platform||"?").trim();
  const letter=label.charAt(0).toUpperCase()||"?";
  return `<div class="avatar" aria-hidden="true">${escapeHtml(letter)}</div>`;
}
function badgeFor(a){
  const meta=META[a.status]||["?","#888"];
  const tone=TONE[a.status]||"";
  const attn=a.status==="awaiting_user"?" attn":"";
  return `<span class="badge ${tone}${attn}">${escapeHtml(meta[0])}</span>`;
}
function btnHtml(label, action, id, opts){
  opts=opts||{};
  const cls="btn btn-sm "+(opts.cls||"");
  const icon=opts.icon?ic(opts.icon):"";
  const safeLabel=String(label).replace(/'/g,"&#39;");
  return `<button class="${cls}" onclick="doAction(${id},'${action}','${safeLabel}')">${icon}${escapeHtml(label)}</button>`;
}
function actionsFor(a){
  const id=a.id;
  if(a.status==="awaiting_user"){
    return btnHtml("Continuar","continue",id,{cls:"btn-warn",icon:"check"});
  }
  if(a.status==="not_mine"){
    return `<button class="btn btn-sm btn-ghost" onclick="markMine(${id})">${ic("check")}Era mía</button>`;
  }
  if(isExposure(a)){
    // Brecha HIBP no confirmada: el usuario decide si tiene cuenta activa
    // allí. Solo entonces el resolver / borrado actúa.
    return `<button class="btn btn-sm btn-primary" onclick="confirmAccount(${id})">${ic("check")}Sí, tengo cuenta</button>`
      + `<button class="btn btn-sm btn-danger" onclick="markNotMine(${id})">${ic("x")}No tengo</button>`;
  }
  if(a.status==="found"){
    // Triage: cuando aún no se confirmó propiedad, ofrecemos el camino rápido
    // (Es mía / No es mía) además de las acciones (que pasarán por modal
    // pre-vuelo si no se ha hecho ownership).
    if(!a.owned){
      return `<button class="btn btn-sm btn-accent" onclick="markMine(${id})">${ic("check")}Es mía</button>`
        + `<button class="btn btn-sm btn-danger" onclick="markNotMine(${id})">${ic("x")}No es mía</button>`
        + btnHtml("Eliminar","delete",id,{cls:"btn-primary"})
        + btnHtml("Conservar","keep",id,{cls:"btn-ghost"});
    }
    return btnHtml("Eliminar","delete",id,{cls:"btn-primary"})
         + btnHtml("Anonimizar","anonymize",id)
         + btnHtml("Conservar","keep",id,{cls:"btn-ghost"});
  }
  if(a.status==="semi_auto"){
    const link=a.profile_url
      ? `<a class="btn btn-sm btn-accent" href="${escapeAttr(a.profile_url)}" target="_blank" rel="noreferrer">${ic("external")}Abrir enlace</a>`
      : "";
    return link
      + `<button class="btn btn-sm" onclick="markSent(${a.id})">${ic("check")}Hecho</button>`
      + btnHtml("Reintentar","retry",id,{cls:"btn-ghost"});
  }
  if(a.status==="email_draft"){
    return `<button class="btn btn-sm btn-accent" onclick="openDraft(${a.id})">${ic("mail")}Ver borrador</button>`
      + `<button class="btn btn-sm" onclick="markSent(${a.id})">Enviado</button>`
      + btnHtml("Reintentar","retry",id,{cls:"btn-ghost"});
  }
  if(a.status==="failed"||a.status==="manual"){
    return btnHtml("Reintentar","retry",id)
         + btnHtml("Conservar","keep",id,{cls:"btn-ghost"});
  }
  if(a.status==="user_done"){
    // Si pasaron 30+ días desde el envío sin respuesta, ofrecemos un
    // borrador de seguimiento (GDPR Art. 12.3: respuesta exigible en 30 días).
    const days = a.sent_at ? Math.floor((Date.now()/1000 - a.sent_at)/86400) : 0;
    const followup = (a.sent_at && days >= 30)
      ? `<button class="btn btn-sm btn-warn" onclick="openFollowup(${a.id})">${ic("mail")}Seguimiento</button>`
      : "";
    return followup
      + btnHtml("Reintentar","retry",id,{cls:"btn-ghost"});
  }
  if(a.status==="deleted"||a.status==="anonymized"||a.status==="skipped"){
    return btnHtml("Reintentar","retry",id,{cls:"btn-ghost"});
  }
  // queued / in_progress: no interrumpimos al motor.
  return "";
}
function renderRow(a){
  const cls=a.status==="awaiting_user"?"account-row attn":"account-row";
  const id=a.identifier?escapeHtml(a.identifier):"";
  const site=a.source_site && a.source_site.toLowerCase()!==(a.display_name||"").toLowerCase()
    ? `<span class="dot">·</span>${escapeHtml(a.source_site)}` : "";
  const link=a.profile_url
    ? `<span class="dot">·</span><a href="${escapeAttr(a.profile_url)}" target="_blank" rel="noreferrer">perfil</a>`
    : "";
  // Seguimiento GDPR: si la cuenta tiene sent_at, calculamos días + estado
  let sent="";
  if(a.sent_at){
    const days=Math.floor((Date.now()/1000 - a.sent_at)/86400);
    const overdue = days >= 30;
    const tone = overdue ? "danger" : "info";
    const label = overdue
      ? `Vencida (${days}d)`
      : `Enviada hace ${days}d`;
    sent = `<span class="dot">·</span><span class="badge ${tone}" title="GDPR: respuesta exigible en 30 días">${label}</span>`;
  }
  const msg=SHOW_MSG.has(a.status) && a.last_message
    ? `<div class="row-msg">${linkify(escapeHtml(a.last_message))}</div>` : "";
  // Indicador de confianza: solo lo mostramos cuando NO está confirmada como
  // propia, porque es ahí donde sirve (filtrar falsos positivos).
  const conf = a.confidence && !a.owned
    ? `<span class="badge ${CONF_TONE[a.confidence]||""}" title="${escapeAttr(CONF_LABEL[a.confidence]||"")} · source=${escapeAttr(a.source||"")}">${escapeHtml(a.confidence)}</span>`
    : "";
  // Tilde verde: la cuenta se confirmó como tuya.
  const ownedMark = a.owned
    ? `<span class="owned-tick" title="Confirmada como tuya">${ic("check")}</span>`
    : "";
  return `<div class="${cls}">
    ${avatarFor(a)}
    <div class="row-main">
      <div class="row-title">${escapeHtml(a.display_name||a.platform)}${ownedMark}</div>
      <div class="row-meta">${id}${site}${link}${sent}</div>
    </div>
    ${conf}
    ${badgeFor(a)}
    <div class="row-actions">${actionsFor(a)}</div>
    ${msg}
  </div>`;
}

/* ── Triage rápido ─────────────────────────────────── */
async function markMine(id){
  try{
    await postJSON(`/api/accounts/${id}/own`,{owned:true});
    toast("Confirmada como tuya");
  } catch(e){ toast(e.message, 7000, "err"); }
  load();
}
async function confirmAccount(id){
  /* "Sí, tengo cuenta aquí": promueve un hit HIBP (exposición) a cuenta
   * normal. Después pasa a Triage y al flujo de borrado habitual. */
  try{
    await postJSON(`/api/accounts/${id}/confirm-account`,{});
    toast("Cuenta confirmada — entra al flujo normal");
  } catch(e){ toast(e.message, 7000, "err"); }
  load();
}
async function markNotMine(id){
  try{
    await postJSON(`/api/accounts/${id}/own`,{owned:false});
    toast("Descartada");
  } catch(e){ toast(e.message, 7000, "err"); }
  load();
}
async function discardLowConfidence(){
  showConfirm({
    title:"Descartar todo low-confidence",
    body:"Vas a marcar como 'No es mía' todas las cuentas en estado 'Pendiente' "
        +"con confianza baja. Útil para limpiar el ruido típico de Sherlock "
        +"con usernames cortos. Puedes recuperar cada una individualmente desde "
        +"el filtro 'Descartadas'.",
    danger:false, confirmLabel:"Descartar low",
    onYes: async ()=>{
      try{
        const r=await postJSON("/api/accounts/discard-low",{});
        toast(`${r.discarded} cuenta(s) descartadas`);
      } catch(e){ toast(e.message, 7000, "err"); }
      load();
    },
  });
}

function askProcessAllAuto(){
  showConfirm({
    title:"Procesar todo lo automatizable",
    body:"Vas a encolar TODAS las cuentas confirmadas como tuyas que tengan "
        +"receta o resolver kind=auto. Las que necesiten tu input (semi_auto, "
        +"email_draft) se prepararán para que las gestiones desde 'Tu turno'.\n\n"
        +"Si Simulación está activada, ninguna acción destructiva ocurrirá: solo "
        +"verás qué habría pasado.",
    danger:false, confirmLabel:"Procesar",
    onYes: async ()=>{
      try{
        const r=await postJSON("/api/accounts/process-all-auto",{});
        const parts = [];
        if(r.queued)      parts.push(`${r.queued} encoladas`);
        if(r.semi_auto)   parts.push(`${r.semi_auto} a "Tu turno" (1 clic)`);
        if(r.email_draft) parts.push(`${r.email_draft} a "Tu turno" (correo)`);
        if(!parts.length) parts.push("nada nuevo");
        const dr = r.dry_run ? " · [simulación]" : "";
        toast(`Procesado: ${parts.join(", ")}${dr}`, 4500);
      } catch(e){ toast(e.message, 7000, "err"); }
      load();
    },
  });
}

/* ── Stats ───────────────────────────────────────────── */
function computeStats(stats, accounts){
  const sum=k=>k.reduce((n,x)=>n+(stats[x]||0),0);
  const acc = accounts || [];
  const exposure = acc.filter(isExposure).length;
  /* Triage no-exposure: cuentas activas pendientes de ownership */
  const triage = acc.filter(a=>a.status==="found" && !a.owned && !isExposure(a)).length;
  const owned_found = acc.filter(
    a=>a.status==="found" && a.owned && (a.source||"")!=="hibp").length;
  return {
    total:     sum(Object.keys(stats)),
    exposure:  exposure,
    triage:    triage,
    pending:   sum(GROUPS.pending) + owned_found,
    your_turn: sum(GROUPS.your_turn),
    action:    sum(GROUPS.action),
    done:      sum(GROUPS.done),
    kept:      sum(GROUPS.kept),
    discarded: sum(GROUPS.discarded),
    /* Candidatas a "Procesar todo automatizable": owned=1 y status=found,
     * excluyendo HIBP no confirmadas. */
    automatable: owned_found,
  };
}
function renderStats(s){
  return `
    <div class="stat">
      <div class="stat-l">Total detectadas</div>
      <div class="stat-n">${s.total}</div>
      <div class="stat-sub">en la sesión actual</div>
    </div>
    <div class="stat ${s.triage>0?"warn":""}">
      <div class="stat-l">Por triar</div>
      <div class="stat-n">${s.triage}</div>
      <div class="stat-sub">${s.triage===0?"todo confirmado":"confirma propiedad"}</div>
    </div>
    <div class="stat ${s.action>0?"accent":""}">
      <div class="stat-l">Acción requerida</div>
      <div class="stat-n">${s.action}</div>
      <div class="stat-sub">${s.action===0?"todo controlado":"necesitan tu input"}</div>
    </div>
    <div class="stat ${s.done>0?"success":""}">
      <div class="stat-l">Completadas</div>
      <div class="stat-n">${s.done}</div>
      <div class="stat-sub">eliminadas o tramitadas</div>
    </div>
    <div class="stat">
      <div class="stat-l">Conservadas</div>
      <div class="stat-n">${s.kept}</div>
      <div class="stat-sub">que no quieres borrar</div>
    </div>
  `;
}

/* ── Empty & skeleton ──────────────────────────────── */
function skeletonRows(n){
  let html="";
  for(let i=0;i<n;i++){
    html+=`<div class="skeleton-row">
      <div class="sk avatar"></div>
      <div><div class="sk bar"></div><div class="sk bar s"></div></div>
      <div class="sk pill"></div>
    </div>`;
  }
  return html;
}
function emptyState(){
  return `<div class="empty">
    <div class="empty-icon">${ic("inbox")}</div>
    <div class="empty-title">Aún no hay cuentas</div>
    <div class="empty-body">Introduce un username o un correo arriba y pulsa
      <b>Escanear</b>. Rastrillo descubrirá tus cuentas y propondrá una acción
      para cada una.</div>
  </div>`;
}
function emptyFilter(){
  return `<div class="empty">
    <div class="empty-icon">${ic("filter")}</div>
    <div class="empty-title">Nada en este filtro</div>
    <div class="empty-body">Cambia el filtro o pulsa <b>Todas</b> para ver el resto.</div>
  </div>`;
}

/* ── Acciones de UI ────────────────────────────────── */
async function doAction(id, action, label){
  await doActionImpl(id, action, label, false);
}
async function doActionImpl(id, action, label, confirmOwned){
  try{
    await postJSON(`/api/accounts/${id}/action`,{action, confirm_owned: confirmOwned});
    toast(label||"OK");
  } catch(e){
    if(e.preflight){
      askOwnership(e.preflight.account, action, label);
      return;
    }
    toast(e.message, 7000, "err");
  }
  load();
}

/* Modal pre-vuelo: el server respondió 412 pidiendo confirmación de propiedad.
 * Le mostramos al usuario la cuenta concreta y le damos 3 caminos:
 *   - Sí, es mía → reenvía la acción con confirm_owned=true
 *   - No es mía  → marca not_mine y cancela la acción
 *   - Cancelar   → no hace nada */
function askOwnership(account, action, label){
  const confLabel = CONF_LABEL[account.confidence] || "Confianza desconocida";
  const confTone  = CONF_TONE[account.confidence] || "warn";
  const idLine = account.identifier ? `<div class="kv"><b>Identificador:</b>${escapeHtml(account.identifier)}</div>`:"";
  const urlLine = account.profile_url ? `<div class="kv"><b>URL:</b><a href="${escapeAttr(account.profile_url)}" target="_blank" rel="noreferrer">${escapeHtml(account.profile_url)}</a></div>`:"";
  const m=$("modal");
  m.innerHTML=`<div class="modal" role="document" style="max-width:520px">
    <div class="modal-head">
      <h3 id="modal-title">¿Esta cuenta es tuya?</h3>
      <button class="btn btn-sm btn-icon btn-ghost modal-close"
              onclick="closeModal()" aria-label="Cerrar">${ic("x")}</button>
    </div>
    <div class="modal-body">
      <div style="font-size:12.5px;color:var(--text-2);margin-bottom:12px">
        Antes de proceder con <b>${escapeHtml(label||action)}</b> necesitamos
        que confirmes que esta cuenta es tuya. Sherlock genera falsos positivos
        para usernames cortos o muy comunes.
      </div>
      <div class="kv"><b>Plataforma:</b>${escapeHtml(account.display_name||account.platform||"")}</div>
      ${idLine}
      ${urlLine}
      <div class="kv" style="margin-top:8px">
        <span class="badge ${confTone}">${escapeHtml(confLabel)}</span>
        <span class="badge" style="margin-left:6px">source: ${escapeHtml(account.source||"")}</span>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">Cancelar</button>
      <button class="btn btn-danger" onclick="closeModal(); discardFromPreflight(${account.id})">
        No es mía
      </button>
      <button class="btn btn-primary"
              onclick="closeModal(); doActionImpl(${account.id},'${escapeAttr(action)}','${escapeAttr(label||action)}', true)">
        Sí, es mía y procede
      </button>
    </div>
  </div>`;
  m.classList.add("on");
  m.onclick=(e)=>{ if(e.target===m) closeModal(); };
}

async function discardFromPreflight(id){
  try{
    await postJSON(`/api/accounts/${id}/own`,{owned:false});
    toast("Descartada");
  } catch(e){ toast(e.message, 7000, "err"); }
  load();
}
async function markSent(id){
  try{
    await postJSON(`/api/accounts/${id}/mark-sent`,{});
    toast("Marcada como hecha");
  } catch(e){ toast(e.message, 7000, "err"); }
  load();
}
async function openDraft(id){
  let meta;
  try{
    const r=await getAPI(`/api/accounts/${id}/resolution`);
    meta=(await r.json()).meta;
  } catch(e){ toast("No pude cargar el borrador", 5000, "err"); return; }
  if(!meta || !meta.email_to){ toast("No hay borrador para esta cuenta", 5000, "err"); return; }
  const mailto="mailto:"+encodeURIComponent(meta.email_to)
    + "?subject="+encodeURIComponent(meta.email_subject||"")
    + "&body="   +encodeURIComponent(meta.email_body||"");
  showDraftModal(meta, mailto);
}

async function openFollowup(id){
  let data;
  try{
    const r = await getAPI(`/api/accounts/${id}/followup-draft`);
    if(!r.ok){
      const d=await r.json().catch(()=>({}));
      toast(d.detail || "No pude generar el seguimiento", 6000, "err");
      return;
    }
    data = await r.json();
  } catch(e){ toast("Error generando seguimiento", 5000, "err"); return; }
  // Reusamos el mismo modal. data ya viene con email_to/subject/body listos.
  const mailto="mailto:"+encodeURIComponent(data.email_to)
    + "?subject="+encodeURIComponent(data.email_subject||"")
    + "&body="   +encodeURIComponent(data.email_body||"");
  showDraftModal({
    title: `Seguimiento GDPR · ${data.host} (${data.days_since_sent}d sin respuesta)`,
    email_to: data.email_to,
    email_subject: data.email_subject,
    email_body: data.email_body,
    notes: "Envío de seguimiento basado en la solicitud original. "
         + "GDPR Art. 12.3: respuesta exigible en un mes.",
  }, mailto);
}
function showDraftModal(meta, mailto){
  const m=$("modal");
  m.innerHTML=`<div class="modal" role="document">
    <div class="modal-head">
      <h3 id="modal-title">${escapeHtml(meta.title||"Solicitud de baja")}</h3>
      <button class="btn btn-sm btn-icon btn-ghost modal-close"
              onclick="closeModal()" aria-label="Cerrar">${ic("x")}</button>
    </div>
    <div class="modal-body">
      <div class="kv"><b>Para:</b>${escapeHtml(meta.email_to||"")}</div>
      <div class="kv"><b>Asunto:</b>${escapeHtml(meta.email_subject||"")}</div>
      <textarea id="draft-body" rows="14">${escapeHtml(meta.email_body||"")}</textarea>
      <div class="hint">${escapeHtml(meta.notes||"")}</div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="copyDraft()">${ic("copy")}Copiar todo</button>
      <a class="btn btn-accent" href="${escapeAttr(mailto)}" target="_blank">${ic("mail")}Abrir en correo</a>
    </div>
  </div>`;
  m.classList.add("on");
  m.onclick=(e)=>{ if(e.target===m) closeModal(); };   // cerrar al clicar el backdrop
}
function copyDraft(){
  const txt=$("draft-body").value;
  navigator.clipboard.writeText(txt).then(
    ()=>toast("Borrador copiado"),
    ()=>toast("No pude copiar", 3000, "err"));
}
function closeModal(){ $("modal").classList.remove("on"); }

/* Confirmación reutilizable: abre el mismo modal con un cuerpo conciso y
 * dos botones (cancelar / acción peligrosa). onYes recibe ninguna arg. */
let _confirmYes=null;
function showConfirm({title, body, danger, confirmLabel, onYes}){
  _confirmYes = onYes;
  const m=$("modal");
  m.innerHTML=`<div class="modal" role="document" style="max-width:480px">
    <div class="modal-head">
      <h3 id="modal-title">${escapeHtml(title)}</h3>
      <button class="btn btn-sm btn-icon btn-ghost modal-close"
              onclick="closeModal()" aria-label="Cerrar">${ic("x")}</button>
    </div>
    <div class="modal-body">
      <div style="font-size:13px;color:var(--text-2);line-height:1.55">${escapeHtml(body)}</div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">Cancelar</button>
      <button class="btn ${danger?'btn-danger':'btn-primary'}" onclick="confirmYes()">${escapeHtml(confirmLabel||"Confirmar")}</button>
    </div>
  </div>`;
  m.classList.add("on");
  m.onclick=(e)=>{ if(e.target===m) closeModal(); };
}
function confirmYes(){
  const cb=_confirmYes; _confirmYes=null;
  closeModal();
  if(cb) cb();
}

function askClear(){
  showConfirm({
    title:"Limpiar todas las cuentas",
    body:"Vas a borrar TODAS las cuentas detectadas de esta sesión, junto con su "
        +"historial. El directorio cacheado, los hallazgos del resolver y tu "
        +"perfil de Chromium no se tocan.\n\nDespués podrás escanear de nuevo "
        +"sin acumulado previo.",
    danger:true, confirmLabel:"Borrar todo",
    onYes: async ()=>{
      try{
        await postJSON("/api/accounts/clear",{});
        toast("Sesión limpia");
      } catch(e){ toast(e.message, 7000, "err"); }
      load();
    },
  });
}

/* ── Loop principal ────────────────────────────────── */
async function load(){
  try{
    const [accRes, scanRes, dirRes] = await Promise.all([
      getJSON("/api/accounts"),
      getJSON("/api/scan/status").catch(()=>({})),
      getJSON("/api/directory").catch(()=>null),
    ]);
    const stats=computeStats(accRes.stats||{}, accRes.accounts||[]);
    // Stats
    $("stats").innerHTML=renderStats(stats);

    // Filtros: contadores
    $("f-all").textContent=`Todas · ${stats.total - stats.discarded - stats.exposure}`;
    $("f-triage").textContent=`Triage · ${stats.triage}`;
    $("f-exposure").textContent=`Brechas · ${stats.exposure}`;
    $("f-pending").textContent=`Pendientes · ${stats.pending}`;
    $("f-your_turn").textContent=`Tu turno · ${stats.your_turn}`;
    $("f-action").textContent=`Acción · ${stats.action}`;
    $("f-done").textContent=`Completadas · ${stats.done}`;
    $("f-kept").textContent=`Conservadas · ${stats.kept}`;
    $("f-discarded").textContent=`Descartadas · ${stats.discarded}`;
    // El botón "Limpiar" solo tiene sentido si hay algo que limpiar.
    $("clear-btn").disabled = stats.total === 0;
    // El bulk "descartar low" aparece SOLO en triage.
    $("bulk-low-btn").style.display = (CURRENT_FILTER==="triage" && stats.triage>0) ? "" : "none";
    // Botón "Procesar automáticas" muestra el contador y se deshabilita en cero.
    $("process-all-label").textContent = `Procesar automáticas · ${stats.automatable}`;
    $("process-all-btn").disabled = stats.automatable === 0;

    // Lista
    const filtered=filterAccounts(accRes.accounts||[]);
    const list=$("list");
    if(!accRes.accounts || !accRes.accounts.length){
      list.innerHTML=emptyState();
    } else if(!filtered.length){
      list.innerHTML=emptyFilter();
    } else {
      const banner = (CURRENT_FILTER==="exposure")
        ? `<div class="row-msg" style="background:var(--info-bg);color:var(--info);grid-column:1 / -1;margin:0 0 8px;padding:12px 14px;line-height:1.55;border-radius:var(--r-md)">
             <b>Estas no son cuentas confirmadas.</b> Tu correo apareció en un volcado de
             datos de estos dominios. Eso no quiere decir que tengas cuenta activa allí.
             Marca "Sí, tengo cuenta" solo en los sitios donde sepas que abriste una;
             el resto, "No tengo" — así no mandas solicitudes GDPR a sitios sin cuenta tuya.
           </div>` : "";
      list.innerHTML = banner + filtered.map(renderRow).join("");
    }

    // Scan status: mostramos la fase (Descubriendo / Resolviendo N/total) en
    // curso, y al terminar el resumen "Último: N detectadas, M resueltas".
    const scanEl=$("scan-status");
    if(scanRes && scanRes.running){
      let label;
      const ph = scanRes.phase;
      if(ph === "resolving"){
        const tot = scanRes.total||0, done = scanRes.resolved||0;
        label = tot>0 ? `Resolviendo ${done}/${tot}…` : "Resolviendo…";
      } else {
        // discovery o phase desconocida
        label = "Descubriendo…";
      }
      scanEl.innerHTML=
        '<span class="chip-dot warn pulse" style="display:inline-block;margin-right:6px"></span>'
        + escapeHtml(label);
      scanEl.classList.add("busy");
    } else {
      scanEl.classList.remove("busy");
      if(scanRes && scanRes.last && !scanRes.last.error){
        const e=scanRes.last.errors||[];
        const resolved = (scanRes.resolved!=null) ? scanRes.resolved : null;
        scanEl.textContent=
          `Último escaneo: ${scanRes.last.found||0} detectadas`
          + (resolved!=null ? `, ${resolved} resueltas`:"")
          + `, ${scanRes.last.kept||0} conservadas`
          + (e.length?`, ${e.length} errores`:"");
      } else if(scanRes && scanRes.last && scanRes.last.error){
        scanEl.textContent="Último escaneo falló: "+scanRes.last.error;
      } else {
        scanEl.textContent="";
      }
    }

    // Chips: directorio + IA
    if(dirRes){
      const dotCls=dirRes.source==="upstream"?"on":(dirRes.source==="cache"?"warn":"off");
      const src=dirRes.source==="upstream"?"online":(dirRes.source==="cache"?"caché":"fallback");
      $("chip-dir").innerHTML=
        `<span class="chip-dot ${dotCls}"></span>Directorio · ${dirRes.entries} · ${src}`;
      $("chip-ai").innerHTML=
        `${ic("sparkles")}<span class="chip-dot ${dirRes.ai_enabled?"on":""}"></span>IA ${dirRes.ai_enabled?"activa":"desactivada"}`;
    }
    // Dry-run state
    try{
      const dr=await getJSON("/api/dry-run");
      const btn=$("dry-toggle");
      btn.classList.toggle("on", !!dr.enabled);
      btn.setAttribute("aria-pressed", dr.enabled ? "true" : "false");
      btn.querySelector(".dry-label").textContent =
        dr.enabled ? "Simulación: ON" : "Simulación";
    } catch(_) {}
  } catch(e){
    console.error("load() falló:", e);
  }
}

/* Toggle del modo simulación */
$("dry-toggle").onclick = async () => {
  const cur = $("dry-toggle").classList.contains("on");
  try{
    const r = await postJSON("/api/dry-run", {enabled: !cur});
    toast(r.enabled ? "Simulación activada (nada se ejecuta de verdad)" : "Simulación desactivada");
  } catch(e){ toast(e.message, 7000, "err"); }
  load();
};

/* ── Handlers ──────────────────────────────────────── */
$("scan-btn").onclick=async()=>{
  const usernames=splitInput($("users").value);
  const emails=splitInput($("mails").value);
  if(!usernames.length && !emails.length){
    toast("Pon al menos un username o un correo", 3000); return;
  }
  $("scan-btn").disabled=true;
  try{
    await postJSON("/api/scan",{usernames,emails});
    toast("Escaneo lanzado");
  } catch(e){ toast(e.message, 7000, "err"); }
  finally{ $("scan-btn").disabled=false; }
};

$("dir-refresh").innerHTML=ic("refresh");
$("dir-refresh").onclick=async()=>{
  const b=$("dir-refresh"); b.disabled=true;
  try{
    const r=await postJSON("/api/directory/refresh",{});
    toast(`Directorio: ${r.entries} sitios actualizados`);
  } catch(e){ toast(e.message, 7000, "err"); }
  finally{ b.disabled=false; load(); }
};

/* ── Toggle de tema ───────────────────────────────────────── */
function applyTheme(theme, save){
  document.documentElement.dataset.theme = theme;
  if (save) {
    try { localStorage.setItem("rastrillo-theme", theme); } catch(e) {}
  }
  // El botón muestra el icono del tema *contrario* (lo que cambias al pulsar).
  const btn = $("theme-toggle");
  btn.innerHTML = theme === "dark" ? ic("sun") : ic("moon");
  btn.setAttribute("aria-label",
    theme === "dark" ? "Cambiar a modo claro" : "Cambiar a modo oscuro");
}
applyTheme(document.documentElement.dataset.theme || "light", false);

$("theme-toggle").onclick = () => {
  const cur = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  applyTheme(cur === "dark" ? "light" : "dark", true);
};

/* Si el SO cambia su preferencia Y el usuario no ha tocado el toggle, seguimos. */
const _mql = window.matchMedia("(prefers-color-scheme: dark)");
if (_mql.addEventListener) {
  _mql.addEventListener("change", (e) => {
    let saved=null;
    try { saved = localStorage.getItem("rastrillo-theme"); } catch(_) {}
    if (!saved) applyTheme(e.matches ? "dark" : "light", false);
  });
}

/* Teclado: Ctrl/⌘+Enter dispara scan; Esc cierra modal */
document.addEventListener("keydown",(e)=>{
  if((e.metaKey||e.ctrlKey) && e.key==="Enter"){
    e.preventDefault(); $("scan-btn").click();
  } else if(e.key==="Escape" && $("modal").classList.contains("on")){
    closeModal();
  }
});

/* ── Onboarding + token prompt ────────────────────────────────── */
function showTokenPrompt(){
  /* Sin token = la web acaba de cargarse sin ?token=. Mostramos un panel
   * explicativo con un input para pegarlo manualmente. */
  const m=$("modal");
  m.innerHTML=`<div class="modal" role="document" style="max-width:520px">
    <div class="modal-head">
      <h3 id="modal-title">Necesitas el token de auth</h3>
    </div>
    <div class="modal-body">
      <div style="font-size:13px;color:var(--text-2);line-height:1.55">
        Rastrillo arrancó pero esta pestaña no sabe el token de auth del proceso.
        Por seguridad, todas las acciones POST exigen ese token (te protege de
        que otro proceso local te dispare un borrado).
      </div>
      <div style="font-size:13px;color:var(--text-2);line-height:1.55;margin-top:10px">
        <b>Forma rápida:</b> en la consola donde arrancaste Rastrillo verás una
        URL del tipo <code>http://127.0.0.1:8765/?token=...</code>. Cierra esta
        pestaña y abre esa URL.
      </div>
      <div style="font-size:13px;color:var(--text-2);line-height:1.55;margin-top:10px">
        <b>O pega el token aquí:</b>
      </div>
      <input id="token-input" class="input" type="text" autocomplete="off"
             placeholder="pega el token (la parte después de ?token=)"
             style="margin-top:8px;font-family:var(--font-mono);font-size:12.5px" />
      <div class="hint">Si reiniciaste el servidor, el token cambió. Vuelve a abrir desde la URL nueva.</div>
    </div>
    <div class="modal-foot">
      <button class="btn btn-accent" onclick="connectWithToken()">Conectar</button>
    </div>
  </div>`;
  m.classList.add("on");
  m.onclick=null;   /* este modal NO se cierra clicando fuera */
  setTimeout(()=>{ const i=$("token-input"); if(i) i.focus(); }, 50);
}

function connectWithToken(){
  const v=($("token-input").value||"").trim();
  if(!v){ toast("Pega un token primero", 3000, "err"); return; }
  try{ sessionStorage.setItem("rastrillo-token", v); }catch(_) {}
  _TOKEN=v;
  closeModal();
  bootstrap();   /* re-arranca init con el token nuevo */
}

function showWelcomePanel(){
  const m=$("modal");
  m.innerHTML=`<div class="modal" role="document" style="max-width:560px">
    <div class="modal-head">
      <h3 id="modal-title">Bienvenida a Rastrillo</h3>
    </div>
    <div class="modal-body">
      <div style="font-size:13px;color:var(--text-2);line-height:1.55">
        Antes de empezar, cuatro cosas que te van a pasar:
      </div>
      <ul style="font-size:13px;color:var(--text-2);line-height:1.6;
                 padding-left:18px;margin-top:10px">
        <li>Verás <b>dos ventanas</b>: este panel y, cuando proceses una cuenta,
            un Chromium que conduce el flujo de borrado.</li>
        <li>Te <b>logueas tú una vez por sitio</b> en ese Chromium. Rastrillo
            <b>nunca</b> guarda contraseñas; viven solo en el perfil de
            navegador local.</li>
        <li>Si una plataforma exige <b>CAPTCHA, 2FA o confirmación final</b>,
            el panel se queda esperando a que tú lo resuelvas en el Chromium
            y pulses "Continuar".</li>
        <li>Esto es <b>solo para tus propias cuentas</b>. Antes de cualquier
            acción destructiva te pedimos confirmar que la cuenta es tuya.</li>
      </ul>
      <div class="hint" style="margin-top:12px">
        Puedes activar el modo <b>Simulación</b> (chip en la barra superior)
        para ver qué haría Rastrillo sin tocar nada.
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn btn-accent" onclick="dismissWelcome()">Entendido</button>
    </div>
  </div>`;
  m.classList.add("on");
  m.onclick=null;
}

async function dismissWelcome(){
  try{ await postJSON("/api/onboarding/dismiss", {}); }
  catch(e){ /* sin token todavía? igual cerramos */ }
  closeModal();
}

/* ── Bootstrap: token -> onboarding -> polling ───────────────── */
let _bootstrapped=false;
async function bootstrap(){
  if(!_TOKEN){ showTokenPrompt(); return; }
  /* Comprobamos onboarding (GET libre, sin token). */
  if(!_bootstrapped){
    try{
      const ob=await getJSON("/api/onboarding");
      if(!ob.onboarded) showWelcomePanel();
    }catch(_){}
    _bootstrapped=true;
    /* Skeleton inicial + arranque del polling (solo la primera vez). */
    $("list").innerHTML=skeletonRows(3);
    load();
    setInterval(load, 2000);
  } else {
    /* Si re-bootstrapeamos por entrada manual de token, basta un load. */
    load();
  }
}
bootstrap();
