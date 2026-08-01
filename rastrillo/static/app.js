/* eslint-disable */
const META = window.__RASTRILLO_BOOT__.META;
/* Verificabilidad: eje SEPARADO de la confianza (Paso 2C, Entrega 1). Dice si
 * el sitio sirve para comprobar algo, nunca si la cuenta es tuya. */
const VERIF_META = window.__RASTRILLO_BOOT__.VERIF_META || {};
const $=id=>document.getElementById(id);

/* ── Estado global de la UI ───────────────────────────────── */
let CURRENT_FILTER="all";

/* Operaciones en vuelo. El polling (setInterval(load, 2000)) repinta la lista
 * por innerHTML, lo que destruiría el botón que el usuario acaba de pulsar y su
 * estado "Enviando…". Mientras haya una acción en curso (_inFlight>0), load()
 * salta el repintado para no pisar la interacción. Se vuelve a refrescar en
 * cuanto la operación termina (cada handler llama a load() al cerrar). */
let _inFlight=0;

/* Tono visual de cada estado (clases CSS, desacoplado de colores backend). */
const TONE={
  found:"", queued:"info", in_progress:"warn",
  awaiting_user:"warn", deleted:"success", anonymized:"indigo",
  user_done:"success",
  semi_auto:"accent", email_draft:"indigo",
  pending_deletion:"warn",
  manual:"warn", skipped:"", failed:"danger",
  not_mine:"", dry_run:"indigo",
};
/* Tono del badge de confianza. */
const CONF_TONE={ high:"success", medium:"warn", low:"danger" };
const CONF_LABEL={ high:"Confianza alta", medium:"Confianza media", low:"Confianza baja" };
/* Qué significa cada tramo y, sobre todo, qué NO significa.
 *
 * `low` es el que más falta hacía: la etiqueta roja se lee como "no es tuya" y
 * no es eso. Confianza baja = evidencia DÉBIL de que sea tuya, que es lo mismo
 * que decir "no lo sé". Quien decide sigue siendo el usuario. */
const CONF_TIP={
  high:"Evidencia fuerte de que la cuenta es tuya (email confirmado, username "
      +"distintivo o dos fuentes independientes). No es una comprobación: "
      +"revísala igual antes de borrar.",
  medium:"Evidencia intermedia. La señal apunta a ti pero no es concluyente.",
  low:"Evidencia DÉBIL de que sea tuya, normalmente un username corto o común. "
     +"No significa que no sea tuya: significa que aquí no hay con qué "
     +"decidirlo. Míralo tú.",
};

/* Etiqueta corta de cada motivo de confianza (los `code` que persiste
 * discovery en accounts.confidence_reasons). La descripción larga que manda el
 * server va en el `title` del chip. Un code desconocido se muestra tal cual:
 * mejor un código crudo que un chip vacío. */
const REASON_LABEL={
  tramo_distintivo:"username distintivo",
  tramo_corto:"username corto",
  tramo_muy_corto:"username muy corto",
  id_vacio:"sin identificador",
  bump_path:"coincide en la ruta",
  bump_subdominio:"coincide en subdominio",
  corrob_misma_fila:"2 buscadores",
  corrob_cruzada:"email + username",
  canario_indiscriminado:"sitio no verificable",
  canario_discrimina:"el sitio verifica usuarios",
  canario_bloqueado:"el sitio nos bloquea",
  canario_sin_respuesta:"el sitio no respondió",
  fuente_holehe:"email confirmado",
  fuente_hibp:"brecha de datos",
  hibp_no_sitio:"volcado sin verificar",
  fuente_manual:"añadida a mano",
  descartado_antes:"ya la descartaste",
};

/* Paso 3, Entrega 3: una frase por motivo — qué señal lo produjo y qué NO
 * significa. Los chips explicaban de dónde salía la confianza, pero no qué
 * concluir de ellos, y varios se leen más fuerte de lo que son (un chip del
 * canario habla del SITIO, no de la cuenta).
 *
 * Texto plano: va al atributo `title` y el render lo escapa. Nada de HTML.
 * Hay un test que falla si se registra un motivo nuevo sin su texto aquí. */
const REASON_TIP={
  tramo_distintivo:"El username es largo o lleva dígitos y separadores, así que "
    +"es difícil que otra persona lo comparta. Es una heurística sobre la "
    +"cadena, no una comprobación de que la cuenta exista o sea tuya.",
  tramo_corto:"El username es corto y poco distintivo, así que puede haber "
    +"homónimos. No dice nada en contra de que sea tuya.",
  tramo_muy_corto:"El username es muy corto y común: casi seguro que hay más "
    +"gente usándolo. Es la fuente habitual de falsos positivos de Sherlock, "
    +"pero no descarta que la cuenta sea tuya.",
  id_vacio:"El hallazgo llegó sin identificador con el que contrastar nada. "
    +"Es una limitación del dato, no un juicio sobre la cuenta.",
  bump_path:"El username aparece como segmento completo en la ruta de la URL "
    +"del hallazgo. Refuerza que la URL va de ese username; no confirma que la "
    +"persona detrás seas tú.",
  bump_subdominio:"El username es exactamente el subdominio del sitio "
    +"(usuario.ejemplo.com). Misma lectura que la ruta: refuerza, no confirma.",
  corrob_misma_fila:"Dos buscadores de username (Sherlock y Maigret) vieron el "
    +"mismo sitio. Señal débil: sus catálogos se solapan, así que no son "
    +"independientes — por eso no sube el tramo de confianza.",
  corrob_cruzada:"Dos caminos independientes llevan al mismo sitio: tu email "
    +"por un lado y tu username por otro. Es la corroboración más fuerte que "
    +"maneja Rastrillo, pero sigue sin ser una comprobación en el sitio.",
  canario_indiscriminado:"El SITIO responde igual para usuarios inventados, "
    +"así que su respuesta no sirve para comprobar nada. Habla del sitio, no "
    +"de tu cuenta: no dice que la cuenta no sea tuya.",
  canario_discrimina:"El SITIO distingue entre usuarios que existen y que no, "
    +"así que su respuesta es informativa. Eso valida el sitio como fuente; no "
    +"confirma que la cuenta sea tuya.",
  canario_bloqueado:"El sitio nos bloqueó (403/429) al comprobarlo. No sabemos "
    +"si discrimina. No se cambian cabeceras para esquivarlo, así que se queda "
    +"sin veredicto.",
  canario_sin_respuesta:"El sitio no respondió (timeout, DNS o conexión). "
    +"Distinto de un bloqueo y distinto de no haberlo mirado: se intentó y no "
    +"se pudo concluir.",
  fuente_holehe:"Holehe confirmó que ese email está registrado en el sitio. Es "
    +"la señal más directa que hay, pero confirma el EMAIL, no que la cuenta "
    +"siga activa.",
  fuente_hibp:"Tu email apareció en un volcado de datos de este dominio. Eso "
    +"es EXPOSICIÓN, no una cuenta confirmada: puede ser una cuenta antigua o "
    +"que la brecha no te afecte. Por eso pide confirmación antes de actuar.",
  hibp_no_sitio:"La brecha es un volcado agregado, una lista de spam o no está "
    +"verificada, así que no respalda una cuenta en ningún sitio concreto. "
    +"Sigue en la lista para que la revises, pero no corrobora nada.",
  fuente_manual:"La añadiste tú a mano, así que se toma como tuya. Rastrillo "
    +"no ha comprobado nada por su cuenta.",
  descartado_antes:"Ya descartaste este par (sitio + identificador) en un "
    +"escaneo anterior y Rastrillo lo recordó. Es tu decisión de siempre, no "
    +"una inferencia nueva: si te equivocaste, "
    +"«Era mía» la borra y vuelve al triage.",
};

/* Texto del tooltip de un chip: la frase fija del motivo + el detalle concreto
 * que persistió discovery para ESA fila (p.ej. "username de 7 caracteres").
 * Si el motivo no tiene frase registrada, cae al detalle, y si tampoco lo hay,
 * a la etiqueta: mejor poco que un tooltip vacío. */
function reasonTip(code, desc){
  const fija = REASON_TIP[code] || "";
  const detalle = desc ? String(desc) : "";
  if(fija && detalle) return `${detalle} — ${fija}`;
  return fija || detalle || (REASON_LABEL[code] || code || "");
}

/* Agrupación de estados por intención del usuario.
 *   triage = found sin confirmar dueño (de discovery, esperando triage)
 *   pending/action/done/kept/discarded son los otros apartados
 */
const GROUPS={
  pending  :["queued","in_progress"],
  your_turn:["semi_auto","email_draft","awaiting_user"],   /* 1 clic / 1 envío / 1 confirmación */
  action   :["semi_auto","email_draft","awaiting_user","manual","failed"],
  deadlines:["pending_deletion"],   /* FASE 4: cuentas con cuenta regresiva */
  done     :["deleted","anonymized","user_done","dry_run"],
  kept     :["skipped"],
  discarded:["not_mine"],
};
/* Helper para distinguir filas de exposición (HIBP no confirmada). El filtro
 * "exposure" se basa en SOURCE, no en STATUS, así que tiene su propio camino. */
function isExposure(a){
  return (a.source === "hibp") && (a.status === "found");
}
/* Igual que `exposure`: el filtro "unverifiable" es ORTOGONAL al status (mira
 * `verifiability`, que rellena el canario), así que va por su propio camino y
 * no entra en GROUPS. Es un filtro para VER agrupados los sitios donde no se
 * puede comprobar nada — deliberadamente sin ninguna acción en lote asociada:
 * inverificable no significa "no es mía". */
function isUnverifiable(a){
  return a.verifiability === "indiscriminado";
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
  clock:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  copy:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
  check:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 5 5L20 7"/></svg>',
  x:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M6 18 18 6"/></svg>',
  inbox:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v7"/><path d="M3 12h6l2 3h2l2-3h6"/><path d="M3 12v6a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-6"/></svg>',
  filter:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h18M6 12h12M10 20h4"/></svg>',
  sun:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>',
  moon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
  chevron:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 6 6 6-6 6"/></svg>',
  trash:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>',
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

/* Descarga de informes (/api/report?format=...).
 *
 * TIENE que ir por fetch. Antes esto eran dos <a href="/api/report?..." download>
 * y no funcionaba: una navegación de ancla NO puede llevar cabeceras propias, así
 * que la petición salía sin X-Rastrillo-Token, el auth_middleware la cortaba con
 * un 401 (cuerpo JSON) y el atributo `download` guardaba ESE JSON de error en un
 * fichero con extensión .pdf. Es decir: nunca se descargaba un PDF.
 *
 * La solución es pedir el binario con el token, recibirlo como Blob y disparar
 * la descarga desde un objeto local. NO se toca el middleware ni se permite el
 * token por query: el endpoint sigue exigiendo cabecera como todo /api/*. */
function _nombreDeContentDisposition(cd, fmt){
  /* El server manda `attachment; filename="rastrillo-<ts>.pdf"`. Si falta o no
   * parsea, componemos uno equivalente en vez de dejar que el navegador invente
   * un nombre a partir de la URL (saldría "report"). */
  const m = /filename="?([^";]+)"?/i.exec(cd || "");
  if(m && m[1]) return m[1];
  const ts = new Date().toISOString().slice(0,19).replace(/[-:T]/g,"");
  return `rastrillo-${ts}.${fmt}`;
}

async function descargarInforme(fmt, btn){
  if(btn) btn.disabled = true;
  let objUrl = null;
  try{
    const r = await getAPI(`/api/report?format=${encodeURIComponent(fmt)}`);
    if(!r.ok){
      /* Un error NUNCA se guarda como fichero: se cuenta. Ese era justo el
       * síntoma viejo (un .pdf que por dentro era el JSON del 401). */
      const d = await r.json().catch(()=>({}));
      toast(d.detail || `No pude generar el informe (${r.status})`, 7000, "err");
      return;
    }
    const blob = await r.blob();
    const nombre = _nombreDeContentDisposition(r.headers.get("Content-Disposition"), fmt);
    objUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objUrl; a.download = nombre;
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast(`Informe descargado (${nombre})`);
  }catch(e){
    toast(e.message || "No pude descargar el informe", 7000, "err");
  }finally{
    /* revoke tras un tick: Firefox necesita que el click haya cursado. */
    if(objUrl) setTimeout(()=>URL.revokeObjectURL(objUrl), 10000);
    if(btn) btn.disabled = false;
  }
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
  if(CURRENT_FILTER==="unverifiable")
    return acc.filter(a=>isUnverifiable(a) && a.status!=="not_mine");
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
      + `<button class="btn btn-sm" onclick="markSent(${a.id}, this)">${ic("check")}Hecho</button>`
      + schedBtn(id)
      + btnHtml("Reintentar","retry",id,{cls:"btn-ghost"});
  }
  if(a.status==="email_draft"){
    return `<button class="btn btn-sm btn-accent" onclick="openDraft(${a.id})">${ic("mail")}Ver borrador</button>`
      + `<button class="btn btn-sm" onclick="markSent(${a.id}, this)">Enviado</button>`
      + btnHtml("Reintentar","retry",id,{cls:"btn-ghost"});
  }
  if(a.status==="pending_deletion"){
    // Cuenta regresiva en curso. No interrumpimos: Verificar (solo si venció,
    // reusa engine.revisit_profile), Reprogramar y Cancelar el plazo.
    const overdue = a.deletion && a.deletion.overdue;
    const verify = overdue
      ? `<button class="btn btn-sm btn-primary" onclick="verifyDeletion(${id}, this)">${ic("check")}Verificar</button>`
      : "";
    return verify
      + `<button class="btn btn-sm btn-ghost" onclick="openScheduleModal(${id})">Reprogramar</button>`
      + `<button class="btn btn-sm btn-ghost" onclick="cancelDeletion(${id})">Cancelar plazo</button>`;
  }
  if(a.status==="failed"||a.status==="manual"){
    // "Programar plazo" solo en manual (estado de reposo); failed no.
    return btnHtml("Reintentar","retry",id)
         + (a.status==="manual" ? schedBtn(id) : "")
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
      + schedBtn(id)
      + btnHtml("Reintentar","retry",id,{cls:"btn-ghost"});
  }
  if(a.status==="deleted"||a.status==="anonymized"||a.status==="skipped"){
    return btnHtml("Reintentar","retry",id,{cls:"btn-ghost"});
  }
  // queued / in_progress: no interrumpimos al motor.
  return "";
}
/* Botón de entrada al flujo de eliminación programada (FASE 4). Aparece en los
 * estados de reposo donde es plausible que la plataforma haya dado un plazo:
 * user_done, manual y semi_auto. */
function schedBtn(id){
  return `<button class="btn btn-sm btn-accent" onclick="openScheduleModal(${id})">${ic("clock")}Programar plazo</button>`;
}
function renderRow(a){
  const cls=a.status==="awaiting_user"?"account-row attn":"account-row";
  const id=a.identifier?escapeHtml(a.identifier):"";
  const site=a.source_site && a.source_site.toLowerCase()!==(a.display_name||"").toLowerCase()
    ? `<span class="dot">·</span>${escapeHtml(a.source_site)}` : "";
  /* "perfil" = la URL DEL HIT (la que produjo el descubrimiento y sobre la que
   * se calcularon los chips de motivo). La URL de borrado que trae el resolver
   * es otra cosa y va con su propia etiqueta: mezclarlas hacía que un chip
   * como "coincide en la ruta" pareciese mentir. */
  const link=a.profile_url
    ? `<span class="dot">·</span><a href="${escapeAttr(a.profile_url)}" target="_blank" rel="noreferrer">perfil</a>`
    : "";
  const delLink=a.deletion_url
    ? `<span class="dot">·</span><a href="${escapeAttr(a.deletion_url)}" target="_blank" rel="noreferrer" title="Página de baja según el directorio o el resolver">cómo darse de baja</a>`
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
  // FASE 4: cuenta regresiva de eliminación. `a.deletion` lo computa el server
  // (días restantes / overdue / pct); aquí solo renderizamos badge + fecha
  // final + barra de progreso. No auto-marcamos deleted al vencer.
  let deadlineMeta="", deadlineBar="";
  if(a.status==="pending_deletion" && a.deletion){
    const dd=a.deletion;
    const fecha = a.deletion_eta ? new Date(a.deletion_eta*1000).toLocaleDateString() : "";
    deadlineMeta = dd.overdue
      ? `<span class="dot">·</span><span class="badge danger" title="El plazo venció el ${escapeAttr(fecha)}">Plazo vencido · presunta eliminación</span>`
      : `<span class="dot">·</span><span class="badge warn" title="Fecha objetivo: ${escapeAttr(fecha)}">Eliminación en ${dd.days_left} d · ${escapeHtml(fecha)}</span>`;
    const pct = Math.max(0, Math.min(100, dd.pct||0));
    deadlineBar = `<div class="deadline-bar${dd.overdue?' overdue':''}" role="progressbar" aria-valuenow="${Math.round(pct)}" aria-valuemin="0" aria-valuemax="100" title="${dd.overdue?'Plazo vencido':'Progreso del plazo'}"><div class="deadline-fill" style="width:${pct}%"></div></div>`;
  }
  const msg=SHOW_MSG.has(a.status) && a.last_message
    ? `<div class="row-msg">${linkify(escapeHtml(a.last_message))}</div>` : "";
  // Motivos de la confianza: mismo criterio que el badge (solo mientras no
  // esté confirmada como propia, que es cuando sirven para el triage).
  // Texto plano en el title: el render escapa todo, no metemos HTML ahí.
  // El title lleva la explicación completa del tramo: la etiqueta sola ("low")
  // se lee como "no es tuya" y no es eso (ver CONF_TIP).
  const confTip = a.confidence
    ? `${CONF_LABEL[a.confidence]||""} · fuente: ${a.source||"?"}. `
      + (CONF_TIP[a.confidence]||"")
    : "";
  const conf = a.confidence && !a.owned
    ? `<span class="badge ${CONF_TONE[a.confidence]||""}" title="${escapeAttr(confTip)}">${escapeHtml(a.confidence)}</span>`
    : "";
  // Verificabilidad: badge propio, al lado del de confianza pero SIN mezclarse
  // con él. Solo pintamos "no verificable": que un sitio sí discrimine es lo
  // normal y no merece ruido, y "no evaluado" (verifiability nulo) no es un
  // juicio. El title deja claro que no habla de propiedad.
  const verifMeta = VERIF_META[a.verifiability];
  const verif = (a.verifiability==="indiscriminado" && verifMeta && !a.owned)
    ? `<span class="badge warn" title="${escapeAttr(a.source_site||"Este sitio")} responde igual a usuarios inventados, así que no se puede comprobar nada aquí. No dice que la cuenta no sea tuya.">${escapeHtml(verifMeta[0])}</span>`
    : "";
  // Tilde verde: la cuenta se confirmó como tuya.
  const ownedMark = a.owned
    ? `<span class="owned-tick" title="Confirmada como tuya">${ic("check")}</span>`
    : "";
  return `<div class="${cls}">
    ${avatarFor(a)}
    <div class="row-main">
      <div class="row-title">${escapeHtml(a.display_name||a.platform)}${ownedMark}</div>
      <div class="row-meta">${id}${site}${link}${delLink}${sent}${deadlineMeta}</div>
      ${breachDetail(a)}
      ${reasonChips(a)}
    </div>
    ${verif}
    ${conf}
    ${badgeFor(a)}
    <div class="row-actions">${actionsFor(a)}</div>
    ${msg}
    ${deadlineBar}
  </div>`;
}

/* ── Detalle de la brecha (HIBP) ───────────────────────────────────────────
 *
 * HIBP devuelve por cada brecha la fecha, cuánta gente afectó y QUÉ TIPOS DE
 * DATO se expusieron. Hasta el Paso 5 esos tres campos llegaban y se tiraban.
 * El tercero es el que de verdad ayuda a decidir: no es lo mismo que se
 * filtrara tu email que que se filtraran tus contraseñas.
 *
 * Es CONTEXTO, no una señal nueva: no toca la confianza (HIBP sigue entrando
 * como `medium` por política) ni dispara ninguna acción. Solo informa. */

/* Traducción de las categorías de dato. HIBP las manda en inglés y con nombres
 * fijos, así que una tabla curada cubre la inmensa mayoría. Lo que NO esté en
 * la tabla se muestra TAL CUAL en inglés: traducir a ojo una categoría que no
 * conocemos sería inventarse qué se expuso, y eso es peor que un término en
 * otro idioma. Mismo criterio que `pdf_fuentes.sanear`: no alterar el dato en
 * silencio. */
const DATA_CLASSES_ES = {
  "Email addresses":"Direcciones de correo",
  "Passwords":"Contraseñas",
  "Usernames":"Nombres de usuario",
  "IP addresses":"Direcciones IP",
  "Names":"Nombres",
  "Phone numbers":"Teléfonos",
  "Physical addresses":"Direcciones postales",
  "Dates of birth":"Fechas de nacimiento",
  "Geographic locations":"Ubicación geográfica",
  "Genders":"Género",
  "Password hints":"Pistas de contraseña",
  "Security questions and answers":"Preguntas de seguridad",
  "Credit cards":"Tarjetas de crédito",
  "Partial credit card data":"Datos parciales de tarjeta",
  "Bank account numbers":"Números de cuenta bancaria",
  "Social security numbers":"Números de seguridad social",
  "Government issued IDs":"Documentos de identidad",
  "Job titles":"Puestos de trabajo",
  "Employers":"Empleadores",
  "Website activity":"Actividad en el sitio",
  "Purchases":"Compras",
  "Private messages":"Mensajes privados",
  "Chat logs":"Historiales de chat",
  "Browser user agent details":"Datos del navegador",
  "Device information":"Información del dispositivo",
  "Spoken languages":"Idiomas",
  "Time zones":"Zonas horarias",
  "Avatars":"Avatares",
  "Profile photos":"Fotos de perfil",
  "Biographies":"Biografías",
  "Nationalities":"Nacionalidades",
  "Salutations":"Tratamientos",
  "Age groups":"Grupos de edad",
  "Instant messenger identities":"Identidades de mensajería",
  "Social media profiles":"Perfiles de redes sociales",
  "Account balances":"Saldos de cuenta",
  "Auth tokens":"Tokens de autenticación",
  "Security questions":"Preguntas de seguridad",
  "Historical passwords":"Contraseñas antiguas",
  "Recovery email addresses":"Correos de recuperación",
  "Physical attributes":"Atributos físicos",
  "Sexual orientations":"Orientación sexual",
  "Religions":"Religión",
  "Political views":"Opiniones políticas",
};
function dataClassLabel(c){
  return DATA_CLASSES_ES[c] || c;   /* sin traducción inventada: el original */
}

/* Fecha de la brecha: HIBP la manda como "YYYY-MM-DD". Se pinta en formato
 * local. Si no parsea, se muestra el original antes que un "Invalid Date". */
function fmtBreachDate(s){
  if(!s) return "";
  const d = new Date(s + "T00:00:00Z");
  if(isNaN(d.getTime())) return s;
  return d.toLocaleDateString(undefined, {year:"numeric", month:"long", day:"numeric"});
}

/* Magnitud con separador de millares. `0` es un dato (una brecha registrada
 * con recuento cero), así que se distingue de "no viene el campo". */
function fmtPwnCount(n){
  if(n === null || n === undefined || n === "") return "";
  const num = Number(n);
  if(!isFinite(num)) return "";
  return num.toLocaleString();
}

function breachDetail(a){
  const m = a.breach_meta;
  if(!m || typeof m !== "object") return "";
  const fecha = fmtBreachDate(m.breach_date);
  const cuantos = fmtPwnCount(m.pwn_count);
  const clases = Array.isArray(m.data_classes) ? m.data_classes : [];
  if(!fecha && !cuantos && !clases.length) return "";

  const meta = [];
  if(fecha)   meta.push(`<span title="Fecha de la brecha según HIBP">Brecha de ${escapeHtml(fecha)}</span>`);
  if(cuantos) meta.push(`<span title="Cuentas afectadas en total, no solo la tuya">${escapeHtml(cuantos)} cuentas afectadas</span>`);
  const metaHtml = meta.length
    ? `<div class="breach-meta">${meta.join('<span class="dot">·</span>')}</div>` : "";

  /* Los tipos de dato como chips y no como volcado: es lo que se lee de un
   * vistazo para decidir si esto importa o no. */
  const chips = clases.map(c=>
    `<span class="chip chip-xs chip-data" title="Tipo de dato expuesto en esta brecha">${escapeHtml(dataClassLabel(String(c)))}</span>`
  ).join("");
  const chipsHtml = chips
    ? `<div class="breach-classes"><span class="breach-classes-h">Datos expuestos:</span>${chips}</div>`
    : "";

  return `<div class="breach-detail">${metaHtml}${chipsHtml}</div>`;
}

/* Chip de la señal agregada del sitio (Paso 3, Entrega 2).
 *
 * INFORMATIVO Y NADA MÁS: no mueve la confianza ni dispara ninguna acción. El
 * server ya aplica el umbral (mínimo 2 identificadores distintos) y manda
 * `site_discards` solo cuando lo supera; aquí no hay lógica de decisión, solo
 * se pinta lo que llegue. */
function siteSignalChip(a){
  const n = a.site_discards;
  if(!n) return "";
  const label = `sitio descartado ${n} veces`;
  const tip = `Ya descartaste ${n} identificadores distintos en `
            + `${a.source_site||"este sitio"}. Es una observación sobre tus `
            + `propias decisiones, sobre una muestra pequeña: no cambia la `
            + `confianza de esta cuenta ni dispara nada.`;
  return `<span class="chip chip-xs" title="${escapeAttr(tip)}">${escapeHtml(label)}</span>`;
}

function reasonChips(a){
  if(a.owned) return "";
  const rs = a.confidence_reasons || [];
  const chips = rs.map(r=>{
    const code = (r && r.code) ? String(r.code) : "";
    const label = REASON_LABEL[code] || code;
    if(!label) return "";
    const tip = reasonTip(code, r && r.desc);
    return `<span class="chip chip-xs" title="${escapeAttr(tip)}">${escapeHtml(label)}</span>`;
  }).join("") + siteSignalChip(a);
  return chips ? `<div class="row-reasons">${chips}</div>` : "";
}

/* ── Triage rápido ─────────────────────────────────── */
/* "Es mía" y, sobre una fila descartada, "Era mía" (el DESHACER del Paso 3).
 * El server borra la entrada de la memoria de descartes y devuelve la fila a
 * `found`; avisamos de lo primero porque es lo que no se ve en la lista. */
async function markMine(id){
  try{
    const r = await postJSON(`/api/accounts/${id}/own`,{owned:true});
    toast(r && r.discard_memory_forgotten
      ? "Recuperada — ya no la descartaré en próximos escaneos"
      : "Confirmada como tuya");
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
/* Descarte en lote. Antes de abrir el modal preguntamos al server EXACTAMENTE
 * qué filas va a tocar: pulsar un botón que escribe `not_mine` sin saber sobre
 * cuántas cuentas era el problema. El preview solo lee y comparte el criterio
 * de selección con el endpoint que escribe, así que el número no puede mentir. */
async function discardLowConfidence(){
  let prev;
  try{
    prev = await getJSON("/api/accounts/discard-low/preview");
  } catch(e){ toast(e.message, 7000, "err"); return; }

  /* `getJSON` no lanza en respuestas no-OK: devuelve el cuerpo del error. Sin
   * este chequeo, un 401 se leería como "0 cuentas" y el usuario creería que
   * no hay nada que descartar. Mejor decir que no se pudo contar. */
  if(!prev || typeof prev.count !== "number"){
    toast((prev && prev.detail) || "No pude consultar qué se descartaría", 7000, "err");
    return;
  }
  const n = prev.count;
  if(!n){ toast("No hay cuentas de confianza baja pendientes", 3500); return; }

  /* Lista corta para que se reconozca lo que se va a descartar; con muchas,
   * un resumen en vez de un muro de texto. */
  const muestra = (prev.accounts||[]).slice(0,8)
    .map(a=>`· ${a.display_name||a.source_site||"?"} (${a.identifier||"?"})`)
    .join("\n");
  const resto = n>8 ? `\n· …y ${n-8} más` : "";

  showConfirm({
    title:`Descartar ${n} ${n===1?"cuenta":"cuentas"} de confianza baja`,
    body:`Vas a marcar como "No es mía" ${n===1?"esta cuenta":`estas ${n} cuentas`} `
        +`en estado "Pendiente":\n\n${muestra}${resto}\n\n`
        +`Confianza baja significa evidencia DÉBIL de que sean tuyas —no que no `
        +`lo sean—, así que revisa la lista.\n\n`
        +`La acción es REVERSIBLE: cada una se recupera desde el filtro `
        +`"Descartadas" con "Era mía". Rastrillo recordará el descarte entre `
        +`escaneos, y ese "Era mía" también lo deshace.`,
    danger:false, confirmLabel:`Descartar ${n}`,
    onYes: async ()=>{
      try{
        const r=await postJSON("/api/accounts/discard-low",{});
        toast(`${r.discarded} cuenta(s) descartadas · recordadas para próximos escaneos`, 4500);
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
    deadlines: sum(GROUPS.deadlines),
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
/* Pone un botón en estado "ocupado": lo deshabilita y le pinta un spinner con
 * etiqueta. Devuelve una función que restaura el estado original. Tolera btn
 * nulo (cuando la acción se reintenta desde un modal y ya no hay botón). */
function setBtnBusy(btn, busyLabel){
  if(!btn) return ()=>{};
  const prevHtml=btn.innerHTML;
  const prevDisabled=btn.disabled;
  btn.disabled=true;
  btn.classList.add("is-busy");
  btn.innerHTML=`<span class="spinner" aria-hidden="true"></span>${escapeHtml(busyLabel||"")}`;
  return ()=>{
    btn.disabled=prevDisabled;
    btn.classList.remove("is-busy");
    btn.innerHTML=prevHtml;
  };
}
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
/* Callback opcional para "Sí, es mía": cuando una acción lleva payload que
 * retryOwned no puede transportar (p.ej. schedule-deletion con days/eta), se
 * pasa aquí y confirmOwnership lo invoca en vez de enrutar por retryOwned. */
let _ownershipConfirm=null;
function askOwnership(account, action, label, onConfirm){
  _ownershipConfirm = onConfirm || null;
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
              onclick="closeModal(); confirmOwnership(${account.id},'${escapeAttr(action)}','${escapeAttr(label||action)}')">
        Sí, es mía y procede
      </button>
    </div>
  </div>`;
  m.classList.add("on");
  m.onclick=(e)=>{ if(e.target===m) closeModal(); };
}

/* Dispatcher de "Sí, es mía": si quien abrió el modal dejó un callback
 * (_ownershipConfirm), lo invocamos (lleva su propio payload); si no, caemos
 * al reintento estándar por acción. */
function confirmOwnership(id, action, label){
  if(_ownershipConfirm){
    const cb=_ownershipConfirm; _ownershipConfirm=null; cb();
    return;
  }
  retryOwned(id, action, label);
}
/* Reintento tras confirmar propiedad desde el modal pre-vuelo. La acción
 * "mark-sent" no la entiende el endpoint /action (daría 400 "Acción
 * desconocida"): tiene su propio endpoint, así que la enrutamos a markSent.
 * El resto de acciones (delete/anonymize/retry) van por doActionImpl. */
function retryOwned(id, action, label){
  if(action==="mark-sent"){
    markSent(id, null, true);
  } else {
    doActionImpl(id, action, label, true);
  }
}
async function discardFromPreflight(id){
  try{
    await postJSON(`/api/accounts/${id}/own`,{owned:false});
    toast("Descartada");
  } catch(e){ toast(e.message, 7000, "err"); }
  load();
}
async function markSent(id, btn, confirmOwned){
  const restore=setBtnBusy(btn, "Enviando…");
  _inFlight++;
  try{
    // El endpoint valida el body contra ActionBody, donde `action` es
    // obligatorio: sin él, FastAPI responde 422 (no 412) y el botón parecía
    // "no hacer nada". Mandamos action aunque el server no lo use para decidir.
    await postJSON(`/api/accounts/${id}/mark-sent`,
                   {action:"mark-sent", confirm_owned: !!confirmOwned});
    toast("Marcada como hecha");
  } catch(e){
    // 412: la cuenta no estaba confirmada como tuya. Mismo flujo que las
    // acciones destructivas: abrimos el modal de propiedad. Si el usuario
    // confirma, se reintenta markSent con confirm_owned=true. NO recargamos
    // aquí (el modal queda abierto); el reintento o el cierre llamarán a load.
    if(e.preflight){
      restore();
      _inFlight--;
      askOwnership(e.preflight.account, "mark-sent", "Enviado");
      return;
    }
    toast(e.message, 7000, "err");
  }
  restore();
  _inFlight--;
  load();
}

/* ── FASE 4: eliminación programada con cuenta regresiva ───────────────────── */
function openScheduleModal(id){
  const t=new Date();
  const todayStr=`${t.getFullYear()}-${String(t.getMonth()+1).padStart(2,"0")}-${String(t.getDate()).padStart(2,"0")}`;
  const m=$("modal");
  m.innerHTML=`<div class="modal" role="document" style="max-width:480px">
    <div class="modal-head">
      <h3 id="modal-title">Programar eliminación</h3>
      <button class="btn btn-sm btn-icon btn-ghost modal-close" onclick="closeModal()" aria-label="Cerrar">${ic("x")}</button>
    </div>
    <div class="modal-body">
      <div style="font-size:12.5px;color:var(--text-2);margin-bottom:14px">
        Muchas plataformas eliminan tras un plazo ("en 30 días"). Registra ese
        plazo y verás la cuenta regresiva. Al vencer <b>no se marca nada solo</b>:
        te ofrecerá <b>Verificar</b>.
      </div>
      <label class="sched-opt">
        <input type="radio" name="sched-mode" value="days" checked onchange="_schedMode('days')">
        <span>Días restantes</span>
        <input id="sched-days" class="input" type="number" min="1" max="3650" value="30">
      </label>
      <label class="sched-opt">
        <input type="radio" name="sched-mode" value="date" onchange="_schedMode('date')">
        <span>Fecha exacta</span>
        <input id="sched-date" class="input" type="date" min="${todayStr}" disabled>
      </label>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">Cancelar</button>
      <button class="btn btn-primary" onclick="submitSchedule(${id}, this)">Programar</button>
    </div>
  </div>`;
  m.classList.add("on");
  m.onclick=(e)=>{ if(e.target===m) closeModal(); };
}
function _schedMode(mode){
  $("sched-days").disabled = mode!=="days";
  $("sched-date").disabled = mode!=="date";
}
function submitSchedule(id, btn){
  const mode=(document.querySelector('input[name="sched-mode"]:checked')||{}).value || "days";
  let payload;
  if(mode==="days"){
    const days=parseInt($("sched-days").value,10);
    if(!days || days<1){ toast("Indica un número de días válido", 3000, "err"); return; }
    payload={days};
  } else {
    const v=$("sched-date").value;
    if(!v){ toast("Elige una fecha", 3000, "err"); return; }
    // Fin de día en hora LOCAL del usuario -> timestamp UNIX (segundos).
    const eta=Math.floor(new Date(v+"T23:59:59").getTime()/1000);
    if(!eta || eta<=Date.now()/1000){ toast("La fecha debe ser futura", 3000, "err"); return; }
    payload={eta};
  }
  scheduleDeletion(id, payload, false, btn);
}
async function scheduleDeletion(id, payload, confirmOwned, btn){
  const restore=setBtnBusy(btn, "Programando…");
  _inFlight++;
  try{
    await postJSON(`/api/accounts/${id}/schedule-deletion`,
                   Object.assign({}, payload, {confirm_owned: !!confirmOwned}));
    toast("Plazo de eliminación programado");
    closeModal();
  } catch(e){
    restore(); _inFlight--;
    if(e.preflight){
      // El payload (days/eta) no cabe en retryOwned; lo reintentamos vía callback.
      askOwnership(e.preflight.account, "schedule-deletion", "Programar eliminación",
                   ()=>scheduleDeletion(id, payload, true));
    } else {
      toast(e.message, 7000, "err");   // dejamos el modal abierto para corregir
    }
    return;
  }
  restore(); _inFlight--;
  load();
}
async function verifyDeletion(id, btn){
  const restore=setBtnBusy(btn, "Verificando…");
  _inFlight++;
  try{
    const r=await postJSON(`/api/accounts/${id}/verify-deletion`, {});
    if(r.dry_run){
      toast("[simulación] verificación: " +
        (r.result===true?"eliminada":r.result===false?"sigue activa":"inconcluso"));
    } else if(r.result===true){ toast("Verificada: la cuenta ya no existe");
    } else if(r.result===false){ toast("La cuenta sigue activa; revísala", 6000, "err");
    } else { toast("No pude verificar; reintenta más tarde", 6000); }
  } catch(e){ toast(e.message, 7000, "err"); }
  restore(); _inFlight--;
  load();
}
async function cancelDeletion(id){
  try{
    await postJSON(`/api/accounts/${id}/cancel-deletion`, {});
    toast("Plazo cancelado");
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
 * dos botones (cancelar / acción peligrosa). onYes recibe ninguna arg.
 *
 * `body` es TEXTO PLANO. Se escapa entero y solo DESPUÉS se convierten los
 * saltos de línea en <br>, así que ningún HTML del cuerpo llega vivo al DOM.
 * Ese orden (escapar → romper líneas) es el que hace segura la conversión; no
 * lo inviertas. Antes los `\n` se comían y los párrafos salían pegados. */
let _confirmYes=null;
function confirmBodyHtml(body){
  return escapeHtml(String(body==null?"":body)).replace(/\n/g, "<br>");
}
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
      <div style="font-size:13px;color:var(--text-2);line-height:1.55">${confirmBodyHtml(body)}</div>
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

/* "Limpiar todo" es irreversible desde la UI (queda el snapshot en disco), así
 * que el cuerpo dice el número exacto y qué NO se lleva por delante — en
 * particular la memoria de descartes, que es justo lo que antes se perdía. */
async function askClear(){
  let n = null;
  try{
    const d = await getJSON("/api/accounts");
    // Igual que en el descarte masivo: `getJSON` no lanza en no-OK. Si no
    // viene la lista, seguimos sin número en vez de decir "0".
    if(d && Array.isArray(d.accounts)) n = d.accounts.length;
  } catch(e){ /* si no se puede contar, seguimos sin número */ }
  if(n === 0){ toast("No hay cuentas que limpiar", 3000); return; }
  const cuantas = n===null ? "TODAS las cuentas detectadas"
                           : `las ${n} cuentas detectadas`;
  showConfirm({
    title:"Limpiar todas las cuentas",
    body:`Vas a borrar ${cuantas} de esta sesión, junto con su historial.\n\n`
        +"NO se tocan: el directorio cacheado, los hallazgos del resolver, tu "
        +"perfil de Chromium ni las decisiones de triage que ya tomaste — los "
        +"hallazgos que descartaste seguirán descartados en el próximo escaneo "
        +"y no tendrás que volver a triarlos.\n\n"
        +"Esto NO se deshace desde la interfaz, pero antes de borrar se guarda "
        +"una copia de la base de datos en ~/.rastrillo/backups/.",
    danger:true, confirmLabel: n===null ? "Borrar todo" : `Borrar ${n}`,
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
  // Hay una acción del usuario en vuelo (p.ej. "Enviado" con su spinner): no
  // repintamos la lista para no destruir el botón ni su estado de carga. El
  // handler de la acción llamará a load() en cuanto termine.
  if(_inFlight>0) return;
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
    $("f-deadlines").textContent=`Plazos · ${stats.deadlines}`;
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
      } else if(ph === "canario"){
        label = "Comprobando sitios…";
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

/* Descarga de informes. Van por fetch+Blob para poder mandar el token; ver
 * `descargarInforme`. */
$("report-csv-btn").onclick=(e)=>descargarInforme("csv", e.currentTarget);
$("report-xlsx-btn").onclick=(e)=>descargarInforme("xlsx", e.currentTarget);
$("report-pdf-btn").onclick=(e)=>descargarInforme("pdf", e.currentTarget);

/* ── Domain Intelligence ──────────────────────────────────────
 * Recon OSINT defensivo sobre un dominio (WHOIS + DNS + correlación).
 * Separado del flujo de borrado: solo para infra propia o autorizada. */
function domField(label, value){
  if(value===null || value===undefined || value==="") return "";
  return `<div class="kv"><b>${escapeHtml(label)}:</b>${escapeHtml(String(value))}</div>`;
}
function domList(label, arr){
  if(!arr || !arr.length) return "";
  const items = arr.map(x=>`<li>${escapeHtml(String(x))}</li>`).join("");
  return `<div class="dom-block"><div class="dom-block-h">${escapeHtml(label)}</div><ul class="dom-ul">${items}</ul></div>`;
}
/* Cuerpo del informe (WHOIS + DNS + correlaciones). La cabecera con el
 * dominio vive ahora en la tarjeta colapsable, no aquí. */
function renderDomainBody(rep){
  if(!rep) return "";
  const w = rep.whois||{}, d = rep.dns||{}, cor = rep.correlations||[];
  /* WHOIS */
  const whoisRows = [
    domField("Registrador", w.registrar),
    domField("Creado", w.created),
    domField("Expira", w.expires),
    domField("Actualizado", w.updated),
    domField("Registrant", w.registrant),
  ].join("");
  const whoisErr = w.error ? `<div class="row-msg" style="color:var(--danger)">WHOIS: ${escapeHtml(w.error)}</div>` : "";
  const whois = `<div class="dom-block"><div class="dom-block-h">WHOIS</div>${whoisRows||'<div class="hint">Sin campos extraídos.</div>'}${domList("Estados", w.status)}${domList("Nameservers", w.nameservers)}${whoisErr}</div>`;
  /* DNS */
  const dnsErr = d.error ? `<div class="row-msg" style="color:var(--danger)">DNS: ${escapeHtml(d.error)}</div>` : "";
  const dns = `<div class="dom-block"><div class="dom-block-h">DNS${d.incomplete?' <span class="badge warn">parcial</span>':''}</div>`
    + domList("A", d.a) + domList("MX", d.mx) + domList("NS", d.ns) + domList("TXT", d.txt)
    + ((d.a&&d.a.length)||(d.mx&&d.mx.length)||(d.ns&&d.ns.length)||(d.txt&&d.txt.length)?"":'<div class="hint">Sin registros.</div>')
    + dnsErr + `</div>`;
  /* Correlaciones */
  let corHtml;
  if(cor.length){
    corHtml = cor.map(c=>{
      const tone = CONF_TONE[c.confidence]||"";
      return `<div class="dom-cor">
        <span class="badge ${tone}" title="Confianza ${escapeHtml(c.confidence||"")}">${escapeHtml(c.confidence||"?")}</span>
        <span class="dom-cor-prov">${escapeHtml(c.proveedor||"")}</span>
        <span class="dom-cor-tipo">${escapeHtml(c.tipo||"")}</span>
        <span class="dom-cor-ev">${escapeHtml(c.evidencia||"")}</span>
      </div>`;
    }).join("");
  } else {
    corHtml = '<div class="hint">Sin correlaciones detectadas.</div>';
  }
  const cors = `<div class="dom-block"><div class="dom-block-h">Correlaciones <span class="hint">(candidatos heurísticos, no hechos confirmados)</span></div>${corHtml}</div>`;
  /* Errores globales */
  const errs = (rep.errors&&rep.errors.length)
    ? `<div class="row-msg" style="color:var(--warn)">${rep.errors.map(e=>escapeHtml(e.source+": "+e.error)).join(" · ")}</div>`
    : "";
  return `<div class="dom-report">${whois}${dns}${cors}${errs}</div>`;
}
/* ── Inteligencia de dominio: histórico (persistido en domain_reports) ──────
 * El backend persiste un informe por dominio y expone /history y /report. Aquí
 * los consumimos y los pintamos como una lista de tarjetas COLAPSABLES: la más
 * reciente abierta y el resto plegadas, para que la sección no crezca sin
 * límite a base de análisis acumulados. Nada de esto vuelve a la red a los
 * sitios: solo lee lo ya guardado. */

/* Informes cargados en la vista actual (recientes primero). Lo usamos para
 * saber cuántos hay sin volver a preguntar al server (p.ej. en la
 * confirmación de "Limpiar historial"). */
let DOM_REPORTS = [];

function fmtDomDate(ts){
  if(!ts) return "";
  try{ return new Date(ts*1000).toLocaleDateString(); }catch(e){ return ""; }
}

/* Estado colapsado por dominio, persistido entre recargas. Guardamos un mapa
 * {dominio: true|false}; un dominio AUSENTE del mapa usa el default (el más
 * reciente abierto, el resto plegados). localStorage puede fallar (modo
 * privado, cuota): degradamos a "sin memoria" sin romper la vista. */
const DOM_COLLAPSE_KEY = "rastrillo.domain.collapsed";
function domCollapseRead(){
  try{
    const v = JSON.parse(localStorage.getItem(DOM_COLLAPSE_KEY) || "{}");
    return (v && typeof v === "object") ? v : {};
  }catch(e){ return {}; }
}
function domCollapseWrite(state){
  try{ localStorage.setItem(DOM_COLLAPSE_KEY, JSON.stringify(state)); }catch(e){}
}
function domCollapseSet(domain, collapsed){
  const st = domCollapseRead();
  st[domain] = !!collapsed;
  domCollapseWrite(st);
}

async function fetchDomainHistory(){
  try{
    const r=await getAPI("/api/domain/history");
    if(!r.ok) return [];
    return (await r.json()).domains || [];
  }catch(e){ return []; }
}

/* Trae UN informe ya guardado (no re-analiza). Devuelve el objeto o null. */
async function fetchDomainReport(domain){
  if(!domain) return null;
  try{
    const r=await getAPI(`/api/domain/report?domain=${encodeURIComponent(domain)}`);
    if(!r.ok) return null;
    const data=await r.json();
    return (data && data.report) ? data.report : null;
  }catch(e){ return null; }   // conveniencia, no crítico: el usuario re-analiza
}

/* Resumen de una línea para la cabecera (visible también plegada). */
function domSummaryLine(rep){
  const d = rep.dns||{};
  const n = (d.a||[]).length + (d.mx||[]).length + (d.ns||[]).length + (d.txt||[]).length;
  const c = (rep.correlations||[]).length;
  const parts = [
    `${n} ${n===1?"registro":"registros"} DNS`,
    `${c} ${c===1?"correlación":"correlaciones"}`,
  ];
  if(rep.registrar) parts.push(rep.registrar);
  return parts.join(" · ");
}

/* Una tarjeta colapsable. El toggle es un <button> de verdad para que el
 * teclado funcione sin JS extra (Enter/Espacio) y lleva aria-expanded +
 * aria-controls; el cuerpo se oculta con el atributo `hidden`. */
function renderDomainCard(entry, idx, collapsed){
  const rep = entry.report, dom = entry.domain;
  const bodyId = `dom-body-${idx}`;
  return `<div class="dom-card" data-domain="${escapeAttr(dom)}">
    <div class="dom-card-h">
      <button class="dom-toggle" type="button" data-act="toggle"
              aria-expanded="${collapsed?"false":"true"}" aria-controls="${bodyId}">
        <span class="dom-chev" aria-hidden="true">${ic("chevron")}</span>
        <span class="dom-card-dom">${escapeHtml(dom)}</span>
        <span class="dom-card-date">${escapeHtml(fmtDomDate(entry.created_at))}</span>
        <span class="dom-card-sum">${escapeHtml(domSummaryLine(rep))}</span>
      </button>
      <button class="btn btn-sm btn-ghost dom-del" type="button" data-act="del"
              title="Eliminar este informe del histórico"
              aria-label="Eliminar el informe de ${escapeAttr(dom)}">${ic("trash")}</button>
    </div>
    <div class="dom-card-b" id="${bodyId}"${collapsed?" hidden":""}>${renderDomainBody(rep)}</div>
  </div>`;
}

/* Estado vacío de la sección: sin informes (nunca analizado nada, o justo
 * después de "Limpiar historial") explicamos qué va aquí en vez de dejar un
 * hueco. Mismo patrón que emptyState() de la lista de cuentas. */
function emptyDomainHistory(){
  return `<div class="empty empty-sm">
    <div class="empty-icon">${ic("inbox")}</div>
    <div class="empty-title">Todavía no has analizado ningún dominio</div>
    <div class="empty-body">Escribe un dominio tuyo (o uno que tengas permiso
      de auditar) arriba y pulsa <b>Analizar</b>. El informe se guarda aquí y
      podrás volver a consultarlo sin repetir las consultas.</div>
  </div>`;
}

/* Pinta la lista completa. `expand` (opcional) fuerza abierto ese dominio
 * (lo usamos tras analizar: lo recién pedido se ve sin un clic extra). */
function renderDomainHistory(entries, expand){
  DOM_REPORTS = entries || [];
  const wrap=$("dom-history-wrap"), list=$("dom-history-list");
  const tools=$("dom-history-tools");
  if(!DOM_REPORTS.length){
    // La sección se queda visible con el estado vacío; los controles globales
    // (colapsar / expandir / limpiar) no tienen sobre qué actuar, así que fuera.
    list.innerHTML=emptyDomainHistory();
    $("dom-history-count").textContent="Informes guardados";
    tools.style.display="none";
    wrap.style.display="";
    return;
  }
  tools.style.display="";
  const st = domCollapseRead();
  const fresh = {};   // reconstruimos el mapa: purga dominios ya borrados
  const html = DOM_REPORTS.map((e, i)=>{
    // Default: solo el más reciente abierto. La preferencia guardada manda,
    // salvo que `expand` pida explícitamente abrir este.
    let collapsed = (e.domain in st) ? !!st[e.domain] : (i !== 0);
    if(expand && e.domain === expand) collapsed = false;
    fresh[e.domain] = collapsed;
    return renderDomainCard(e, i, collapsed);
  }).join("");
  domCollapseWrite(fresh);
  list.innerHTML = html;
  $("dom-history-count").textContent =
    `Informes guardados · ${DOM_REPORTS.length}`;
  wrap.style.display="";
}

/* Recarga histórico + informes y repinta. Los informes son locales (SQLite) y
 * pequeños: los pedimos en paralelo y montamos la vista de una vez. */
async function refreshDomainHistory(expand){
  const hist = await fetchDomainHistory();
  const reps = await Promise.all(hist.map(h=>fetchDomainReport(h.domain)));
  const entries = [];
  hist.forEach((h, i)=>{
    if(reps[i]) entries.push({domain:h.domain, created_at:h.created_at, report:reps[i]});
  });
  renderDomainHistory(entries, expand);
}

/* Abre/cierra una tarjeta. `force` (true=abrir, false=cerrar) para los
 * controles globales; sin él, alterna. */
function toggleDomCard(card, force){
  const btn=card.querySelector(".dom-toggle"), body=card.querySelector(".dom-card-b");
  if(!btn || !body) return;
  const open = (force===undefined) ? body.hidden : !!force;
  body.hidden = !open;
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  domCollapseSet(card.dataset.domain, !open);
}
function setAllDomCards(open){
  document.querySelectorAll("#dom-history-list .dom-card")
    .forEach(card=>toggleDomCard(card, open));
}

function askDeleteDomainReport(domain){
  showConfirm({
    title:"Eliminar informe de dominio",
    body:`Vas a borrar el informe guardado de ${domain}. Solo desaparece del `
        +`histórico local: son datos públicos (WHOIS/DNS) y puedes volver a `
        +`analizar el dominio cuando quieras.`,
    danger:true, confirmLabel:"Eliminar",
    onYes: async ()=>{
      try{
        await postJSON("/api/domain/report/delete", {domain});
        toast("Informe eliminado");
      }catch(e){ toast(e.message||"No pude eliminar el informe", 7000, "err"); }
      await refreshDomainHistory();   // refresco sin recargar la página
    },
  });
}

function askClearDomainHistory(){
  const n = DOM_REPORTS.length;
  if(!n){ toast("No hay informes guardados", 3000); return; }
  showConfirm({
    title:"Limpiar histórico de dominios",
    body:`Vas a borrar ${n} ${n===1?"informe":"informes"} de dominio. Antes se `
        +`guarda una copia de la base de datos en ~/.rastrillo/backups/. No `
        +`afecta a tus cuentas detectadas.`,
    danger:true, confirmLabel:`Borrar ${n===1?"el informe":"los "+n+" informes"}`,
    onYes: async ()=>{
      try{
        const r=await postJSON("/api/domain/history/clear", {});
        toast(`Histórico limpio (${r.deleted||0})`);
      }catch(e){ toast(e.message||"No pude limpiar el histórico", 7000, "err"); }
      await refreshDomainHistory();
    },
  });
}

async function analyzeDomain(){
  const raw=($("dom-input").value||"").trim().toLowerCase();
  if(!raw){ toast("Escribe un dominio", 3000, "err"); return; }
  const btn=$("dom-btn"), st=$("dom-status");
  btn.disabled=true;
  st.innerHTML='<span class="chip-dot warn pulse" style="display:inline-block;margin-right:6px"></span>Analizando…';
  st.classList.add("busy");
  try{
    const rep=await postJSON("/api/domain/analyze",{domain:raw});
    st.textContent="Análisis completado";
    // El informe recién hecho ya está persistido: repintamos el histórico y
    // dejamos su tarjeta abierta.
    await refreshDomainHistory(rep.domain||raw);
  } catch(e){
    st.textContent="";
    toast(e.message||"No pude analizar el dominio", 7000, "err");
  } finally {
    btn.disabled=false; st.classList.remove("busy");
  }
}
$("dom-btn").onclick=analyzeDomain;
$("dom-input").addEventListener("keydown",(e)=>{
  if(e.key==="Enter"){ e.preventDefault(); analyzeDomain(); }
});
/* Delegación: las tarjetas se repintan enteras, así que un solo listener en el
 * contenedor evita re-enganchar handlers en cada refresco. */
$("dom-history-list").addEventListener("click",(e)=>{
  const t=e.target.closest("[data-act]");
  if(!t) return;
  const card=t.closest(".dom-card");
  if(!card) return;
  if(t.dataset.act==="toggle") toggleDomCard(card);
  else if(t.dataset.act==="del") askDeleteDomainReport(card.dataset.domain);
});
$("dom-collapse-all").onclick=()=>setAllDomCards(false);
$("dom-expand-all").onclick=()=>setAllDomCards(true);
$("dom-clear-btn").onclick=askClearDomainHistory;
refreshDomainHistory();

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
