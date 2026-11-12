"use strict";
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const url = value => encodeURIComponent(value);
const S = {token:"", labs:[], lab:null, topology:null, status:{}, events:[], after:0, deliveries:[], selected:null,
  snapshot:null, view:"overview", inspectorTab:"ports", dirty:false, busy:false, refreshing:false, zoom:1, sequence:0, selectionSequence:0};
const views = {overview:["拓扑总览","从设备到端口，让每一条连接清晰可见。"],devices:["设备与接口","查看设备资产、接口状态与网络配置。"],
  links:["链路管理","调整接线、VLAN 与聚合链路，验证网络变化。"],diagnostics:["网络诊断","通过 IP、MAC 和端口定位问题，检查双向可达性。"],
  events:["事件与告警","跟踪配置、链路事件与通知投递结果。"],config:["实验配置","导出拓扑、运行场景，管理你的仿真实验。"]};
const fabrics = [{key:"inference",prefix:"infer",title:"推理网络",vlan:100,color:"#72b6a6"},
  {key:"storage",prefix:"storage",title:"存储网络",vlan:200,color:"#7a9dc4"},
  {key:"inband",prefix:"inband",title:"带内网络",vlan:300,color:"#a596c0"},
  {key:"oob",prefix:"oob",title:"带外网络",vlan:400,color:"#c8ae7f"}];
const deviceRole = d => ({"gpu-server":"GPU 服务器","storage-server":"存储服务器","infer-switch":"推理交换机","storage-switch":"存储交换机","inband-switch":"带内交换机","oob-switch":"带外交换机"}[d.role] || (d.forwarding === false ? "服务器" : "网络设备"));
const pill = (text, kind = "neutral") => `<span class="pill ${esc(kind)}">${esc(text)}</span>`;
const speed = value => value >= 1e9 ? `${+(value / 1e9).toFixed(2)} Gbps` : value >= 1e6 ? `${+(value / 1e6).toFixed(2)} Mbps` : `${value || 0} bps`;
const selectedDevice = () => S.topology?.devices.find(d => d.id === S.selected?.id && S.selected.kind === "device");
const labEvents = () => S.events.filter(e => e.lab_id === S.lab);
function toast(message, error = false) {
  const node = $("#toast"); node.textContent = message; node.classList.toggle("error",error); node.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => node.hidden = true,error ? 8500 : 4000);
}
function guard(work, label) {
  try { return work(); }
  catch (error) { console.error(`[ui] ${label}`, error); try { toast(`${label}失败：${error?.message || error}`, true); } catch {} return undefined; }
}
window.addEventListener("error", event => {
  console.error("[ui] uncaught", event.error || event.message);
  try { toast(`界面异常：${event.error?.message || event.message || "未知错误"}，可刷新页面恢复。`, true); } catch {}
});
window.addEventListener("unhandledrejection", event => {
  const error = event.reason;
  if (error?.status === 401) return;
  console.error("[ui] rejected", error);
  try { toast(`请求失败：${error?.message || error}`, true); } catch {}
});
async function api(path, {method = "GET", body, revision, key} = {}) {
  const headers = {"Accept":"application/json"};
  if (S.token) headers.Authorization = `Bearer ${S.token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (revision !== undefined) headers["If-Match"] = `"${revision}"`;
  if (method !== "GET") headers["Idempotency-Key"] = key || (crypto.randomUUID ? crypto.randomUUID() : `web-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  const response = await fetch(path,{method,headers,body:body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(response.status === 409 ? "配置版本已变化，请先刷新右侧配置，再重新提交。" : data.error || `请求失败 (${response.status})`);
    error.status = response.status;
    if (response.status === 401) { $("#connection-label").textContent = "需要 API 访问令牌"; if (!$("#auth-dialog").open) $("#auth-dialog").showModal(); }
    throw error;
  }
  return data;
}
function setBusy(value) {
  S.busy = value;
  $$("button[type=submit], #start-button, #stop-button, [data-mutate]").forEach(b => b.disabled = value);
  if (!value) updateProtocolControls();
}
async function mutate(work, message = "配置已更新") {
  if (S.busy) return;
  setBusy(true);
  try { await work(); S.dirty = false; await refreshLab(); await refreshSelection(); toast(message); }
  catch (error) { toast(error.message,true); }
  finally { setBusy(false); }
}
function showView(view) {
  if (!views[view]) return;
  S.view = view;
  $$(".view").forEach(node => node.hidden = node.id !== `view-${view}`);
  $$(".nav-item").forEach(node => node.classList.toggle("active",node.dataset.view === view));
  $("#breadcrumb").textContent = $("#page-title").textContent = views[view][0];
  $("#page-description").textContent = views[view][1];
  guard(renderTables, "表格渲染");
}
async function loadLabs(preferred) {
  const [status,labs] = await Promise.all([api("/v1/status"),api("/v1/labs")]);
  S.status = status; S.labs = labs;
  $("#runtime-mode").textContent = status.model_only ? "仅模型模式" : status.mode === "namespace" ? "Linux namespace" : "本地协议模式";
  $("#version-label").textContent = status.version ? `IDC Simulator / v${status.version}` : "IDC Simulator";
  $("#version-label").title = status.version ? `控制服务版本 ${status.version}` : "";
  $("#footer-mode").textContent = status.model_only ? "模型 / Web / REST" : "Web / REST / SNMP / SSH";
  $("#connection-label").textContent = "控制服务已连接";
  $("#lab-select").innerHTML = labs.length ? labs.map(l => `<option value="${esc(l.id)}">${esc(l.id)}</option>`).join("") : '<option value="">暂无实验</option>';
  const next = labs.find(l => l.id === (preferred || S.lab))?.id || labs[0]?.id || null;
  if (next !== S.lab) await changeLab(next); else if (next) await refreshLab();
  $("#empty-state").hidden = !!next; $("#lab-content").hidden = !next; updateProtocolControls();
}
async function changeLab(lab) {
  S.lab = lab; S.selected = null; S.snapshot = null; S.dirty = false; S.selectionSequence++;
  $("#lab-select").value = lab || "";
  ["#locate-result","#ping-result","#scenario-result"].forEach(id => $(id).replaceChildren());
  renderInspector();
  if (lab) await refreshLab();
  $("#empty-state").hidden = !!lab; $("#lab-content").hidden = !lab;
}
async function refreshLab() {
  if (!S.lab) return;
  const sequence = ++S.sequence, lab = S.lab;
  const [topology,eventData,labs] = await Promise.all([api(`/v1/labs/${url(lab)}/topology`),api(`/v1/events?after=${S.after}&limit=1000`),api("/v1/labs")]);
  if (sequence !== S.sequence || lab !== S.lab) return;
  S.topology = topology; S.deliveries = eventData.deliveries;
  S.labs = labs;
  const seen = new Set(S.events.map(e => e.id));
  S.events.push(...eventData.events.filter(e => !seen.has(e.id))); S.events = S.events.slice(-5000);
  S.after = Math.max(S.after,...eventData.events.map(e => e.id));
  $("#revision-label").textContent = `revision ${topology.revision}`;
  $("#updated-label").textContent = `${new Date().toLocaleTimeString("zh-CN",{hour12:false})} 更新`;
  const labState = S.labs.find(l => l.id === lab);
  $("#lab-status").innerHTML = pill(S.status.model_only ? "模型已载入" : ({running:"协议运行中",degraded:"协议服务降级",stopped:"协议已停止"}[labState?.status] || labState?.status),labState?.status === "running" ? "good" : "neutral");
  const devices = topology.devices, links = topology.links, switches = devices.filter(d => d.forwarding !== false).length;
  $("#stat-devices").textContent = devices.length; $("#stat-device-detail").textContent = `${switches} 台网络设备 · ${devices.length - switches} 台服务器`;
  $("#stat-links").textContent = links.length; $("#stat-link-detail").textContent = `${links.filter(l => l.source.members.length).length} 条聚合链路`;
  const up = links.filter(l => l.oper).length;
  $("#stat-health").textContent = links.length ? `${Math.round(up / links.length * 100)}%` : "—";
  $("#stat-health-detail").textContent = `${up} UP · ${links.length - up} DOWN`;
  $("#stat-alerts").textContent = labEvents().filter(e => e.type === "linkDown").length;
  $("#config-summary").textContent = `实验 ${lab} · ${devices.length} 台设备 · ${links.length} 条逻辑链路 · revision ${topology.revision}。${S.status.model_only ? "当前仅运行状态模型，可编辑拓扑和诊断；协议 Worker 未启动。" : "协议服务可通过上方按钮启动或停止。"}`;
  const options = devices.map(d => `<option value="${esc(d.id)}">${esc(d.id)}</option>`).join("");
  ["#locate-device","#notification-device"].forEach(id => { const value = $(id).value; $(id).innerHTML = options; if (devices.some(d => d.id === value)) $(id).value = value; });
  guard(renderGraph, "拓扑渲染"); guard(renderTables, "表格渲染"); updateProtocolControls();
}
function updateProtocolControls() {
  const unavailable = S.status.model_only || !S.lab || S.busy;
  $("#start-button").disabled = unavailable;
  $("#stop-button").disabled = unavailable;
  $("#start-button").title = S.status.model_only ? "仅模型模式不启动 SNMP/SSH Worker" : "启动此实验的 SNMP/SSH 服务";
}
function renderTables() {
  if (!S.topology) return;
  const query = $("#device-search").value.toLowerCase();
  $("#device-rows").innerHTML = S.topology.devices.filter(d => `${d.id} ${d.hostname} ${d.role} ${d.management_ip} ${deviceRole(d)}`.toLowerCase().includes(query)).map(d => {
    const ports = Object.values(d.interfaces), up = ports.filter(p => p.oper).length;
    return `<tr class="clickable ${S.selected?.id === d.id ? "selected-row" : ""}" data-device="${esc(d.id)}"><td><strong>${esc(d.hostname)}</strong><small>${esc(d.id)}</small></td><td>${esc(deviceRole(d))}</td><td>${esc(d.management_ip)}</td><td>${up} / ${ports.length} UP</td><td>${pill(up ? "接口在线" : "无活动接口",up ? "good" : "neutral")}</td></tr>`;
  }).join("") || '<tr><td colspan="5">没有匹配的设备</td></tr>';
  const lq = $("#link-search").value.toLowerCase();
  $("#link-rows").innerHTML = S.topology.links.filter(l => `${l.id} ${l.endpoints.join(" ")}`.toLowerCase().includes(lq)).map(l => `<tr class="clickable ${S.selected?.id === l.id ? "selected-row" : ""}" data-link="${esc(l.id)}"><td><strong>${esc(l.id)}</strong><small>${esc(l.endpoints[0])} ↔ ${esc(l.endpoints[1])}</small></td><td>${l.source.members.length ? "Eth-Trunk" : esc(l.mode)}</td><td>${esc(l.allowed_vlans.join(", ")) || "—"}</td><td>${pill(l.oper ? "UP" : "DOWN",l.oper ? "good" : "bad")}${l.blocked ? pill("阻塞","warn") : ""}</td><td>${l.lldp_discovered.filter(Boolean).length} / 2</td><td><button class="text-button" data-link="${esc(l.id)}">调整</button></td></tr>`).join("") || '<tr><td colspan="6">没有匹配的链路</td></tr>';
  const events = labEvents().slice().reverse(), filter = $("#event-filter").value;
  const shown = events.filter(e => filter === "all" || filter === "link" && ["linkUp","linkDown"].includes(e.type) || filter === "config" && e.type.startsWith("config") || filter === "notification" && ["coldStart","warmStart","authenticationFailure"].includes(e.type));
  $("#event-rows").innerHTML = shown.slice(0,500).map(e => {
    const delivery = S.deliveries.filter(d => d.event_id === e.id);
    const delivered = delivery.length ? [...new Set(delivery.map(d => ({confirmed:"已确认",sent:"已发送",failed:"发送失败",dropped:"已丢弃",processed:"已处理"}[d.status] || d.status)))].join(" / ") : "未记录投递";
    const detail = e.payload.ifname || e.payload.sensor || (e.payload.operations ? `${e.payload.operations.length} 项操作` : "—");
    return `<tr><td><strong>${esc(e.type)}</strong><small>#${e.id} · revision ${e.revision}</small></td><td>${esc(e.device_id || "实验")}</td><td>${esc(detail)}</td><td>${esc(delivered)}</td></tr>`;
  }).join("") || '<tr><td colspan="4">暂无此类事件</td></tr>';
  $("#event-note").textContent = `已载入本实验 ${events.length} 条事件，列表最多展示 500 条。Trap 已发送不等于接收成功；INFORM 已确认表示收到确认。`;
  $("#recent-events").innerHTML = events.slice(0,4).map(e => `<div class="recent-event"><span class="event-icon">${e.type === "linkDown" ? "↓" : e.type === "linkUp" ? "↑" : "◷"}</span><div><strong>${esc(e.type)}</strong><small>${esc(e.device_id || S.lab)} · ${esc(e.payload.ifname || `revision ${e.revision}`)}</small></div>${pill(`#${e.id}`,e.type === "linkDown" ? "bad" : "neutral")}</div>`).join("") || '<div class="panel-footnote">暂无事件。调整端口或链路后，变更会在此记录。</div>';
}

function renderGraph() {
  if (!S.topology) return;
  const focus = $("#fabric-filter").value, positions = new Map(), t = S.topology;
  const recognized = t.devices.some(d => /^(infer|storage|inband|oob)-sw-/.test(d.id));
  let devices = t.devices, links = t.links, labels = "", width = 1000, height;
  if (recognized) {
    const active = fabrics.filter(f => focus === "all" || focus === f.key);
    const includedLinks = t.links.filter(l => active.some(f => l.allowed_vlans.includes(f.vlan)));
    const included = new Set(includedLinks.flatMap(l => l.endpoints.map(ep => ep.split(":")[0])));
    devices = t.devices.filter(d => included.has(d.id)); links = includedLinks;
    active.forEach((f,row) => {
      const y = 67 + row * 120;
      labels += `<text x="30" y="${y-24}" class="fabric-label">${esc(f.title)}</text><text x="890" y="${y-24}" class="fabric-subtitle">VLAN ${f.vlan}</text>`;
      devices.filter(d => d.id.startsWith(f.prefix+"-sw-")).sort((a,b) => a.id.localeCompare(b.id)).forEach((d,i) => positions.set(d.id,{x:40+i*245,y,w:185,h:60}));
    });
    const bottom = active.length * 120 + 70, gpus = devices.filter(d => d.role === "gpu-server").sort((a,b) => a.id.localeCompare(b.id));
    labels += `<text x="30" y="${bottom-22}" class="fabric-label">GPU 计算节点</text><text x="785" y="${bottom-22}" class="fabric-label">存储节点</text>`;
    gpus.forEach((d,i) => positions.set(d.id,{x:35+(i%8)*88,y:bottom+Math.floor(i/8)*35,w:75,h:25,gpu:true}));
    devices.filter(d => d.role === "storage-server").forEach((d,i) => positions.set(d.id,{x:790,y:bottom+i*92,w:180,h:62}));
    height = bottom+Math.max(310,Math.ceil(gpus.length/8)*35)+25;
  } else {
    const cols = Math.min(5,Math.max(1,Math.ceil(Math.sqrt(devices.length))));
    devices.forEach((d,i) => positions.set(d.id,{x:40+i%cols*(920/cols),y:60+Math.floor(i/cols)*120,w:Math.min(175,850/cols),h:65}));
    height = Math.max(400,Math.ceil(devices.length/cols)*120+60);
  }
  const selectedId = S.selected?.kind === "device" ? S.selected.id : null;
  let edges = "";
  for (const l of links) {
    const a = positions.get(l.source.device), b = positions.get(l.target.device);
    if (!a || !b) continue;
    const endpointServer = t.devices.find(d => d.id === l.source.device)?.forwarding === false || t.devices.find(d => d.id === l.target.device)?.forwarding === false;
    if (recognized && endpointServer && l.source.device !== selectedId && l.target.device !== selectedId && l.id !== S.selected?.id) continue;
    const sameRow = a.y === b.y, ax = a.x+a.w/2, ay = a.y+a.h/2, bx = b.x+b.w/2, by = b.y+b.h/2;
    const path = sameRow ? `M ${a.x+a.w} ${ay} L ${b.x} ${by}` : `M ${ax} ${ay} C ${ax} ${(ay+by)/2}, ${bx} ${(ay+by)/2}, ${bx} ${by}`;
    const color = !l.oper ? "#db9696" : l.blocked ? "#c7b381" : "#85bbae";
    edges += `<g class="graph-edge" data-link="${esc(l.id)}" tabindex="0" role="button" aria-label="链路 ${esc(l.id)}"><title>${esc(l.id)}: ${esc(l.endpoints.join(" ↔ "))}</title><path d="${path}" fill="none" stroke="transparent" stroke-width="18"/><path class="edge-visible" d="${path}" fill="none" stroke="${color}" stroke-width="${l.id === S.selected?.id ? 4 : 2}" ${l.blocked || !l.oper ? 'stroke-dasharray="6 4"' : ""}/>${sameRow ? `<text x="${(a.x+a.w+b.x)/2}" y="${ay-7}" text-anchor="middle" class="edge-label">${l.source.members.length ? "LAG" : esc(l.mode)}</text>` : ""}</g>`;
  }
  const nodes = devices.map(d => {
    const p = positions.get(d.id); if (!p) return "";
    const up = Object.values(d.interfaces).some(p => p.oper), chosen = selectedId === d.id;
    return `<g class="graph-node ${chosen ? "selected" : ""}" data-device="${esc(d.id)}" tabindex="0" role="button" aria-label="设备 ${esc(d.id)}" transform="translate(${p.x},${p.y})"><title>${esc(d.id)} · ${esc(deviceRole(d))} · ${esc(d.management_ip)}</title><rect width="${p.w}" height="${p.h}" rx="${p.gpu ? 4 : 7}" fill="#ffffff" stroke="#dbe5ea"/><circle cx="${p.gpu ? 9 : 14}" cy="${p.gpu ? 12 : 18}" r="3" fill="${up ? "#69bda0" : "#d1a0a0"}"/>${p.gpu ? `<text class="gpu-label" x="20" y="16">${esc(d.id.replace("gpu-","GPU "))}</text>` : `<text class="node-label" x="24" y="23">${esc(d.id)}</text><text class="node-detail" x="14" y="44">${esc(deviceRole(d))} · ${Object.values(d.interfaces).filter(p => !p.members).length} 口</text>`}</g>`;
  }).join("");
  const svg = $("#topology-svg"); svg.setAttribute("viewBox",`0 0 ${width} ${height}`); svg.style.width = `${100*S.zoom}%`;
  svg.innerHTML = labels+edges+nodes;
  $("#graph-caption").textContent = `${devices.length} 台设备 · ${links.length} 条声明链路${recognized ? " · 接入线按选中节点展示" : ""}`;
}

async function selectDevice(id) {
  if (S.busy) return;
  S.selected = {kind:"device",id}; S.dirty = false; S.inspectorTab = "ports"; S.snapshot = null;
  renderGraph(); renderTables(); renderInspector(); await refreshSelection();
  if (matchMedia("(max-width:900px)").matches) $("#inspector").scrollIntoView({behavior:"smooth",block:"start"});
}
function selectLink(id) {
  if (S.busy) return;
  S.selectionSequence++; S.selected = {kind:"link",id}; S.snapshot = null; S.dirty = false;
  renderGraph(); renderTables(); renderInspector();
  if (matchMedia("(max-width:900px)").matches) $("#inspector").scrollIntoView({behavior:"smooth",block:"start"});
}
async function refreshSelection() {
  if (!S.selected || S.dirty) return;
  if (S.selected.kind === "link") { renderInspector(); return; }
  const id = S.selected.id, sequence = ++S.selectionSequence;
  const snapshot = await api(`/v1/devices/${url(id)}/state`);
  if (sequence !== S.selectionSequence || S.selected?.id !== id || S.dirty) return;
  S.snapshot = snapshot; renderInspector();
}
const options = (ports, selected) => ports.map(p => `<option value="${esc(p.id)}" ${p.id === selected ? "selected" : ""}>${esc(p.name)}${p.members ? " · 聚合口" : ""}</option>`).join("");
function renderInspector() {
  const root = $("#inspector");
  if (!S.selected) { root.innerHTML = '<div class="inspector-placeholder"><div class="empty-icon">⌘</div><h3>选择一个设备或链路</h3><p>查看端口状态、邻居与配置，或调整网络连接。</p></div>'; return; }
  if (S.selected.kind === "link") { renderLinkInspector(); return; }
  if (!S.snapshot) { root.innerHTML = '<div class="inspector-placeholder">加载设备详情…</div>'; return; }
  const d = S.snapshot.device, ports = Object.values(d.interfaces), selectedPort = S.port && ports.some(p => p.id === S.port) ? S.port : ports.find(p => p.oper)?.id || ports[0]?.id;
  S.port = selectedPort;
  root.innerHTML = `<div class="inspector-heading"><div><h2>${esc(d.id)}</h2><p>${esc(deviceRole(d))} · revision ${S.snapshot.revision}</p></div><button class="icon-button" data-action="reload-inspector" title="刷新配置；未提交编辑会被清除" aria-label="刷新设备配置">↻</button></div><div class="inspector-body"><div class="detail-grid"><div><span>管理 IP</span><strong>${esc(d.management_ip)}</strong></div><div><span>设备 MAC</span><strong>${esc(d.chassis_mac)}</strong></div><div><span>GPU / 资产</span><strong>${d.hardware?.gpu_count ? `${esc(d.hardware.gpu_count)} × ${esc(d.hardware.gpu_model)}` : esc(d.profile)}</strong></div><div><span>转发</span><strong>${d.forwarding === false ? "服务器不转发" : "网络设备"}</strong></div></div><div class="inspector-tabs"><button data-tab="ports" class="${S.inspectorTab === "ports" ? "active" : ""}">接口</button><button data-tab="network" class="${S.inspectorTab === "network" ? "active" : ""}">网络配置</button><button data-tab="tables" class="${S.inspectorTab === "tables" ? "active" : ""}">邻居与表</button></div><div id="inspector-tab"></div><div class="divider"></div><div class="form-row"><button class="button secondary compact" data-action="save-device" data-mutate>保存 startup</button><button class="button secondary compact" data-action="reboot-device" data-mutate>从 startup 重启</button></div><p class="muted">应用配置修改 running；保存后才会在模拟重启时保留。</p></div>`;
  renderInspectorTab();
}
function renderInspectorTab() {
  if (!S.snapshot) return;
  const root = $("#inspector-tab"), d = S.snapshot.device, revision = S.snapshot.revision;
  if (S.inspectorTab === "ports") {
    const p = d.interfaces[S.port];
    if (!p) { root.textContent = "此设备没有接口"; return; }
    const counters = S.snapshot.runtime.counters[p.id] || {};
    root.innerHTML = `<label>选择接口<select id="port-select">${options(Object.values(d.interfaces),p.id)}</select></label><div class="port-status"><span>运行状态</span>${pill(p.oper ? "UP" : "DOWN",p.oper ? "good" : "bad")}</div><div class="detail-grid"><div><span>接口 MAC</span><strong>${esc(p.mac)}</strong></div><div><span>ifIndex / bridge port</span><strong>${p.ifindex} / ${p.bridge_port}</strong></div><div><span>入方向流量</span><strong>${speed(counters.in_bytes_rate)}</strong></div><div><span>出方向流量</span><strong>${speed(counters.out_bytes_rate)}</strong></div></div><form id="port-form" data-revision="${revision}" data-port="${esc(p.id)}"><label class="toggle-label"><input name="admin" type="checkbox" ${p.admin ? "checked" : ""}>启用接口（admin UP）</label><label>描述<input name="description" value="${esc(p.description)}" maxlength="255"></label><div class="form-row"><label>速率（Gbps）<input name="speed" type="number" min="0.001" step="0.001" value="${p.speed_bps/1e9}" required></label><label>MTU<input name="mtu" type="number" min="576" max="9216" value="${p.mtu}" required></label></div><div class="form-row"><label>PVID<input name="pvid" type="number" min="1" max="4094" value="${p.pvid}" required></label><label>端口模式<select name="mode"><option value="switchport" ${p.mode === "switchport" ? "selected" : ""}>二层</option><option value="routed" ${p.mode === "routed" ? "selected" : ""}>三层</option></select></label></div><label>允许 VLAN（逗号分隔）<input name="allowed_vlans" value="${esc(p.allowed_vlans.join(","))}" placeholder="100,200"></label><label class="toggle-label"><input name="lldp" type="checkbox" ${p.lldp ? "checked" : ""}>启用 LLDP</label><label class="toggle-label"><input name="trap_enabled" type="checkbox" ${p.trap_enabled ? "checked" : ""}>启用链路变化告警</label>${p.members ? `<label>聚合成员（接口 ID，逗号分隔）<input name="members" value="${esc(p.members.join(","))}" required></label><label>最少活动成员<input name="min_links" type="number" min="1" max="${p.members.length}" value="${p.min_links || 1}" required></label>` : ""}<button type="submit" class="button primary full-width">应用接口配置</button></form>`;
  } else if (S.inspectorTab === "network") {
    root.innerHTML = `<h3>设备名称</h3><form id="hostname-form" data-revision="${revision}"><label>Hostname<input name="hostname" value="${esc(d.hostname)}" required></label><button type="submit" class="button secondary full-width">修改名称</button></form><div class="divider"></div><h3>VLAN</h3><div>${Object.values(d.vlans).map(v => pill(`${v.vlan_id} · ${v.name}`)).join(" ")}</div><form id="vlan-form" data-revision="${revision}"><div class="form-row"><label>VLAN ID<input name="vlan" type="number" min="1" max="4094" required></label><label>名称<input name="name" placeholder="VLAN100"></label></div><div class="form-row"><button class="button secondary" type="submit">新增 / 更新</button><button class="button danger" type="submit" name="delete" value="true">删除 VLAN</button></div></form><p class="muted">删除前需移除接口和三层地址对 VLAN 的引用。</p><div class="divider"></div><h3>三层接口</h3>${d.l3_interfaces.map(l => `<div class="data-list"><strong>${esc(l.id)}</strong> ${pill(l.oper ? "UP" : "DOWN",l.oper ? "good" : "neutral")}<div>${esc(l.address)} · ${esc(l.vrf)} · ${l.interface ? esc(l.interface) : `VLAN ${l.vlan}`}</div></div>`).join("") || '<p class="muted">暂无三层地址</p>'}<form id="l3-form" data-revision="${revision}"><label>接口 ID<input name="id" placeholder="vlan100 或 p48" required></label><label>绑定方式<select name="binding"><option value="vlan">VLAN 三层接口</option><option value="interface">物理 / 聚合接口</option></select></label><label>绑定 VLAN 或接口 ID<input name="target" placeholder="100 或 p48" required></label><label>地址 / 前缀<input name="address" placeholder="10.10.0.1/24" required></label><label>VRF<input name="vrf" value="default" required></label><div class="form-row"><button class="button secondary" type="submit">新增 / 更新</button><button class="button danger" type="submit" name="delete" value="true">删除</button></div></form><div class="divider"></div><h3>静态路由</h3>${d.routes.map(r => `<div class="data-list">${esc(r.prefix)} → ${esc(r.next_hop)}<br>VRF ${esc(r.vrf || "default")} · distance ${r.distance || 1}</div>`).join("") || '<p class="muted">暂无静态路由</p>'}<form id="route-form" data-revision="${revision}"><label>目标前缀<input name="prefix" placeholder="10.20.0.0/24" required></label><label>下一跳<input name="next_hop" placeholder="203.0.113.2" required></label><div class="form-row"><label>VRF<input name="vrf" value="default" required></label><label>Distance<input name="distance" type="number" min="1" max="255" value="1" required></label></div><div class="form-row"><button class="button secondary" type="submit">新增路由</button><button class="button danger" type="submit" name="delete" value="true">删除</button></div></form>`;
  } else {
    const t = S.snapshot.tables;
    root.innerHTML = `<h3>LLDP 邻居 · ${t.lldp.length}</h3>${t.lldp.map(n => `<div class="data-list"><strong>${esc(n.remote_device)}</strong><div>${esc(n.local_name)} ↔ ${esc(n.remote_port)}</div><div>设备 MAC ${esc(n.chassis_mac)}</div><div>端口 MAC ${esc(n.remote_port_mac)}</div></div>`).join("") || '<p class="muted">暂无 LLDP 邻居</p>'}<div class="divider"></div><h3>ARP · ${t.arp.length}</h3>${t.arp.slice(0,100).map(n => `<div class="data-list"><strong>${esc(n.ip)}</strong> ${pill(n.state,n.state === "reachable" ? "good" : "warn")}<div>${esc(n.mac)} · ${esc(n.vrf)}</div></div>`).join("") || '<p class="muted">暂无 ARP 记录</p>'}<div class="divider"></div><h3>FDB / MAC · ${t.fdb.length}</h3>${t.fdb.slice(0,100).map(n => `<div class="data-list">${esc(n.mac)}<div>VLAN ${n.vlan} · ifIndex ${n.ifindex} · ${esc(n.state)}</div></div>`).join("") || '<p class="muted">暂无 MAC 记录</p>'}<p class="muted">各表最多显示 100 条；完整记录可通过 API 读取。</p>`;
  }
}
function renderLinkInspector() {
  const l = S.topology.links.find(l => l.id === S.selected.id); if (!l) { S.selected = null; renderInspector(); return; }
  const opts = value => S.topology.devices.map(d => `<option value="${esc(d.id)}" ${d.id === value ? "selected" : ""}>${esc(d.id)}</option>`).join("");
  $("#inspector").innerHTML = `<div class="inspector-heading"><div><h2>${esc(l.id)}</h2><p>链路配置 · revision ${S.topology.revision}</p></div><button class="icon-button" data-action="reload-inspector" title="刷新配置；未提交编辑会被清除" aria-label="刷新链路配置">↻</button></div><div class="inspector-body"><div class="port-status"><span>运行状态</span>${pill(l.oper ? "UP" : "DOWN",l.oper ? "good" : "bad")}</div><form id="link-form" data-revision="${S.topology.revision}" data-link="${esc(l.id)}"><label>源设备<select name="source_device" id="source-device">${opts(l.source.device)}</select></label><label>源接口<select name="source_port" id="source-port">${options(Object.values(S.topology.devices.find(d => d.id === l.source.device).interfaces),l.source.port)}</select></label><label>目标设备<select name="target_device" id="target-device">${opts(l.target.device)}</select></label><label>目标接口<select name="target_port" id="target-port">${options(Object.values(S.topology.devices.find(d => d.id === l.target.device).interfaces),l.target.port)}</select></label><label>链路模式<select name="mode"><option value="access" ${l.mode === "access" ? "selected" : ""}>Access · 单 VLAN</option><option value="trunk" ${l.mode === "trunk" ? "selected" : ""}>Trunk · 多 VLAN</option><option value="routed" ${l.mode === "routed" ? "selected" : ""}>Routed · 三层</option></select></label><label>允许 VLAN（Access 只填写一个）<input name="allowed_vlans" value="${esc(l.allowed_vlans.join(","))}" placeholder="100 或 100,200"></label><label>LLDP 展示范围<select name="lldp_scope"><option value="logical" ${l.lldp_scope === "logical" ? "selected" : ""}>逻辑接口</option><option value="members" ${l.lldp_scope === "members" ? "selected" : ""}>聚合成员接口</option></select></label><label class="toggle-label"><input name="up" type="checkbox" ${l.up ? "checked" : ""}>启用链路</label><label class="toggle-label"><input name="lldp" type="checkbox" ${l.lldp ? "checked" : ""}>允许 LLDP</label><label class="toggle-label"><input name="blocked" type="checkbox" ${l.blocked ? "checked" : ""}>阻塞二层转发</label><button type="submit" class="button primary full-width">应用链路配置</button></form><div class="divider"></div><h3>两端发现状态</h3><p class="muted">${esc(l.source.device)} ${l.lldp_discovered[0] ? "已发现对端" : "未发现对端"}<br>${esc(l.target.device)} ${l.lldp_discovered[1] ? "已发现对端" : "未发现对端"}</p><p class="muted">新接入端口需先启用。VLAN、三层地址和聚合成员应与两端接口配置一致。</p></div>`;
}

function vlanList(value) {
  if (!value.trim()) return [];
  const result = value.split(",").map(v => Number(v.trim()));
  if (result.some(v => !Number.isInteger(v) || v < 1 || v > 4094)) throw new Error("VLAN 请使用 1–4094 的整数，以逗号分隔。");
  return [...new Set(result)];
}
const formData = form => Object.fromEntries(new FormData(form));
const transaction = (device,operations,revision) => api(`/v1/devices/${url(device)}/transactions`,{method:"POST",body:{operations},revision});

document.addEventListener("click",async event => {
  const view = event.target.closest("[data-view]"); if (view) showView(view.dataset.view);
  const device = event.target.closest("[data-device]"); if (device) { try { await selectDevice(device.dataset.device); } catch (error) { toast(error.message,true); } }
  const link = event.target.closest("[data-link]"); if (link && !link.matches("form")) selectLink(link.dataset.link);
  const close = event.target.closest(".close-dialog"); if (close) close.closest("dialog").close();
  const tab = event.target.closest("[data-tab]"); if (tab) { S.inspectorTab = tab.dataset.tab; S.dirty = false; renderInspector(); }
  const action = event.target.closest("[data-action]")?.dataset.action;
  if (action === "reload-inspector") { S.dirty = false; try { await refreshLab(); await refreshSelection(); } catch(error) { toast(error.message,true); } }
  if (action === "save-device" || action === "reboot-device") {
    const id = S.snapshot.device.id, op = action === "save-device" ? "save" : "reboot";
    if (op === "reboot" && !confirm(`从 startup 配置重启 ${id}？未保存的 running 修改会被替换。`)) return;
    await mutate(() => api(`/v1/devices/${url(id)}/${op}`,{method:"POST",body:{},revision:S.snapshot.revision}),op === "save" ? "已保存到 startup" : "设备已模拟重启");
  }
});
document.addEventListener("keydown",event => { const node = event.target.closest("svg [role=button]"); if (node && ["Enter"," "].includes(event.key)) { event.preventDefault(); node.dispatchEvent(new MouseEvent("click",{bubbles:true})); } });
$("#inspector").addEventListener("input",event => { if (event.target.closest("form")) S.dirty = true; });
$("#inspector").addEventListener("change",event => {
  if (event.target.id === "port-select") { S.port = event.target.value; S.dirty = false; renderInspectorTab(); }
  if (["source-device","target-device"].includes(event.target.id)) {
    const side = event.target.id.split("-")[0], d = S.topology.devices.find(d => d.id === event.target.value);
    $(`#${side}-port`).innerHTML = options(Object.values(d.interfaces)); S.dirty = true;
  }
});
$("#inspector").addEventListener("submit",async event => {
  event.preventDefault(); const form = event.target, f = formData(form), revision = Number(form.dataset.revision), d = S.snapshot?.device;
  const deleting = event.submitter?.value === "true";
  try {
    let ops, device = d?.id;
    if (form.id === "port-form") {
      const fields = {admin:!!form.elements.admin.checked,description:f.description,mtu:Number(f.mtu),speed_bps:Math.round(Number(f.speed)*1e9),
        pvid:Number(f.pvid),allowed_vlans:vlanList(f.allowed_vlans),lldp:form.elements.lldp.checked,trap_enabled:form.elements.trap_enabled.checked,mode:f.mode};
      ops = [{kind:"interface",port:form.dataset.port,fields}];
      if (f.members) ops.push({kind:"lag",port:form.dataset.port,fields:{members:f.members.split(",").map(v => v.trim()),min_links:Number(f.min_links)}});
    } else if (form.id === "hostname-form") ops = [{kind:"system",fields:{hostname:f.hostname}}];
    else if (form.id === "vlan-form") ops = [{kind:"vlan",vlan:Number(f.vlan),name:f.name || `VLAN${f.vlan}`,delete:deleting}];
    else if (form.id === "l3-form") {
      const value = {id:f.id,address:f.address,vrf:f.vrf}; value[f.binding] = f.binding === "vlan" ? Number(f.target) : f.target;
      ops = [{kind:"l3",value,delete:deleting}];
    } else if (form.id === "route-form") ops = [{kind:"route",value:{prefix:f.prefix,next_hop:f.next_hop,vrf:f.vrf,distance:Number(f.distance)},delete:deleting}];
    else if (form.id === "link-form") {
      const vlans = vlanList(f.allowed_vlans);
      if (f.mode === "access" && vlans.length !== 1) throw new Error("Access 链路需要一个 VLAN。");
      if (f.mode === "routed" && vlans.length) throw new Error("三层链路的允许 VLAN 应为空。");
      const fields = {endpoints:[`${f.source_device}:${f.source_port}`,`${f.target_device}:${f.target_port}`],mode:f.mode,allowed_vlans:vlans,
        lldp:form.elements.lldp.checked,blocked:form.elements.blocked.checked,lldp_scope:f.lldp_scope};
      if (f.mode === "access") fields.vlan = vlans[0];
      device = f.source_device; ops = [{kind:"link",id:form.dataset.link,up:form.elements.up.checked,fields}];
    }
    if (ops) await mutate(() => transaction(device,ops,revision));
  } catch(error) { toast(error.message,true); }
});

$("#lab-select").addEventListener("change",event => changeLab(event.target.value).catch(e => toast(e.message,true)));
$("#refresh-button").addEventListener("click",() => loadLabs().then(refreshSelection).catch(e => toast(e.message,true)));
["device-search","link-search"].forEach(id => $(`#${id}`).addEventListener("input",renderTables));
$("#event-filter").addEventListener("change",renderTables);
$("#fabric-filter").addEventListener("change",renderGraph);
[["zoom-in",1.25],["zoom-out",.8]].forEach(([id,factor]) => $(`#${id}`).addEventListener("click",() => { S.zoom = Math.max(.6,Math.min(3,S.zoom*factor)); renderGraph(); }));
$("#zoom-reset").addEventListener("click",() => { S.zoom = 1; renderGraph(); });
$("#start-button").addEventListener("click",() => mutate(async () => { await api(`/v1/labs/${url(S.lab)}/start`,{method:"POST",body:{}}); S.labs = await api("/v1/labs"); },"协议服务已启动"));
$("#stop-button").addEventListener("click",() => mutate(async () => { await api(`/v1/labs/${url(S.lab)}/stop`,{method:"POST",body:{}}); S.labs = await api("/v1/labs"); },"协议服务已停止，模型可继续编辑"));

$("#locate-kind").addEventListener("change",event => {
  const port = event.target.value === "port"; $("#address-field").hidden = port; $("#locate-port-fields").hidden = !port;
  const form = $("#locate-form"); form.elements.address.required = !port; form.elements.port.required = port;
  form.elements.address.placeholder = event.target.value === "mac" ? "例如 02:ab:cd:12:34:56" : "例如 10.10.0.74";
});
$("#locate-form").addEventListener("submit",async event => {
  event.preventDefault(); const form = event.target, f = formData(form), button = $("button[type=submit]",form); button.disabled = true;
  try {
    const query = new URLSearchParams(f.kind === "port" ? {device:f.device,port:f.port} : {[f.kind]:f.address.trim()});
    if (f.vrf) query.set("vrf",f.vrf.trim()); if (f.vlan) query.set("vlan",f.vlan);
    const r = await api(`/v1/labs/${url(S.lab)}/locate?${query}`);
    $("#locate-result").innerHTML = `<div class="result-banner ${!r.found ? "error" : ""}">${r.found ? `找到 ${r.ports.length} 条交换机端口记录` : "未找到 ARP / FDB 对应端口"}${r.ambiguous ? " · 存在跨 VRF 歧义，请限定 VRF" : ""}</div><div class="table-scroll"><table><thead><tr><th>交换机 / 接口</th><th>MAC / IP</th><th>VLAN</th><th>证据</th></tr></thead><tbody>${r.ports.map(p => `<tr class="clickable" data-device="${esc(p.device)}"><td><strong>${esc(p.device)} · ${esc(p.interface)}</strong><small>${esc(p.name)} · ifIndex ${p.ifindex}</small></td><td>${esc(p.mac)}<small>${esc(p.ips.join(", ")) || "无 ARP 对应 IP"}</small></td><td>${p.vlan}</td><td>${pill(p.role === "attachment" ? "接入口" : "转发路径")}${pill(p.fresh ? "当前学习" : "缓存 stale",p.fresh ? "good" : "warn")}</td></tr>`).join("")}</tbody></table></div><p class="muted">基于 ARP / FDB 记录；缓存保留期间查到端口不表示当前可达。</p>`;
  } catch(error) { $("#locate-result").textContent = error.message; toast(error.message,true); } finally { button.disabled = false; }
});
$("#ping-form").addEventListener("submit",async event => {
  event.preventDefault(); const form = event.target, f = formData(form), button = $("button[type=submit]",form); button.disabled = true;
  try {
    const r = await api(`/v1/labs/${url(S.lab)}/ping`,{method:"POST",body:{source:f.source.trim(),destination:f.destination.trim(),count:Number(f.count),...(f.vrf ? {vrf:f.vrf.trim()} : {})}});
    const path = edges => edges.map(e => `${esc(e[0])} → ${esc(e[1])}`).join("<br>") || "本设备 / 无转发路径";
    $("#ping-result").innerHTML = `<div class="result-banner ${r.reachable ? "" : "error"}">${r.reachable ? "双向可达" : "不可达"} · ${r.transmitted} 次检查 / ${r.received} 次成功 · 丢包 ${r.loss_percent}%</div><p class="muted">${esc(r.source)} → ${esc(r.destination)} · VRF ${esc(r.vrf)} · ${esc(r.reason)}</p><h3>请求路径</h3><div class="path-list">${path(r.forward.path)}</div><h3>返回路径</h3><div class="path-list">${path(r.reverse.path)}</div>`;
  } catch(error) { $("#ping-result").textContent = error.message; toast(error.message,true); } finally { button.disabled = false; }
});

function updateGeneratorScale() {
  const form = $("#create-form");
  $("#generator-scale-warning").hidden = Number(form.elements.devices.value)*Number(form.elements.ports.value) <= 60000;
  $("#generator-scale-confirm").checked = false;
}
function openCreate() { $("#create-error").textContent = ""; updateGeneratorScale(); $("#create-dialog").showModal(); }
for (const name of ['devices','ports']) $("#create-form").elements[name].addEventListener("input",updateGeneratorScale);
$("#create-button").addEventListener("click",openCreate); $("#empty-create").addEventListener("click",openCreate);
$("#template-select").addEventListener("change",event => {
  const imported = event.target.value === "import", generated = event.target.value === "generated";
  $("#template-fields").hidden = imported; $("#import-fields").hidden = !imported; $("#generator-fields").hidden = !generated;
  $("#new-lab-name").required = !imported; $("#new-lab-name").value = event.target.value === "local" ? "local-web" : "idc-64gpu";
  if (generated) $("#new-lab-name").value = "generated-lab";
  $("#template-help").textContent = generated ? "生成器会自动创建设备、接口、接线、VLAN、主机和基础流量。" : "GPU 模板包含推理、存储、带内、带外四个网络；Mac 可使用仅模型模式管理。";
});
$("#import-file").addEventListener("change",async event => { try { $("#create-form").elements.content.value = await event.target.files[0]?.text() || ""; } catch(error) { $("#create-error").textContent = error.message; } });
$("#create-form").addEventListener("submit",async event => {
  event.preventDefault(); if (S.busy) return; const f = formData(event.target); setBusy(true); $("#create-error").textContent = "";
  try {
    let created;
    if (f.template === "import") created = await api("/v1/labs/import",{method:"POST",body:{content:f.content}});
    else if (f.template === "generated") {
      if (Number(f.devices)*Number(f.ports) > 60000 && !$("#generator-scale-confirm").checked) throw new Error("请先确认大规模实验的创建开销。");
      const query = new URLSearchParams({name:f.name,devices:f.devices,ports:f.ports,layout:f.layout});
      const config = await api(`/v1/templates/generated?${query}`);
      created = await api("/v1/labs",{method:"POST",body:config});
    } else { const config = await api(`/v1/templates/${url(f.template)}`); config.name = f.name; created = await api("/v1/labs",{method:"POST",body:config}); }
    $("#create-dialog").close(); await loadLabs(created.id); toast("实验已创建，状态模型可以立即查看与调整");
  } catch(error) { $("#create-error").textContent = error.message; } finally { setBusy(false); }
});
$("#auth-button").addEventListener("click",() => $("#auth-dialog").showModal());
$("#auth-form").addEventListener("submit",async event => {
  event.preventDefault(); S.token = formData(event.target).token.trim(); $("#auth-error").textContent = "";
  try { await loadLabs(); $("#auth-dialog").close(); event.target.reset(); toast("已连接控制服务"); }
  catch(error) { $("#auth-error").textContent = error.message; }
});
$("#export-button").addEventListener("click",async () => {
  try {
    const config = await api(`/v1/labs/${url(S.lab)}/config`), blob = new Blob([JSON.stringify(config,null,2)],{type:"application/json"});
    const href = URL.createObjectURL(blob), a = document.createElement("a"); a.href = href; a.download = `${S.lab}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(href),1000);
  } catch(error) { toast(error.message,true); }
});
let replacementDraft = null, deletionDraft = null;
function invalidateReplacement() { replacementDraft = null; $("#replace-apply").disabled = true; $("#replace-preview").replaceChildren(); }
$("#replace-content").addEventListener("input",invalidateReplacement);
$("#replace-startup").addEventListener("change",invalidateReplacement);
$("#replace-file").addEventListener("change",async event => {
  try { if (event.target.files[0]) { $("#replace-content").value = await event.target.files[0].text(); invalidateReplacement(); } }
  catch(error) { $("#replace-error").textContent = error.message; }
});
const editorLayout = new Map();
let editorSelectedLink = null, editorDraft = null, editorDragging = null;
function editorPorts(device) { return Object.values(device.interfaces || {}).filter(port => !port.members); }
function readEditorDraft() {
  try {
    const draft = JSON.parse($("#replace-content").value);
    if (!draft || !Array.isArray(draft.devices) || !Array.isArray(draft.links) || draft.devices.some(d=>!d || !d.id) || draft.links.some(l=>!l || !Array.isArray(l.endpoints) || l.endpoints.length!==2 || l.endpoints.some(e=>typeof e!=="string"))) throw new Error("需要 devices 数组和 links 数组，链路必须包含两个端点。");
    $("#editor-error").textContent = ""; editorDraft = draft; return draft;
  }
  catch(error) { $("#editor-error").textContent = `拓扑 JSON 无效：${error.message}`; editorDraft = null; return null; }
}
function fillEditorPorts(side) {
  const draft = editorDraft || readEditorDraft(); if (!draft) return;
  const device = draft.devices.find(item => item.id === $(`#editor-${side}-device`).value);
  const select = $(`#editor-${side}-port`), before = select.value;
  select.innerHTML = editorPorts(device || {}).map(port => `<option value="${esc(port.id)}">${esc(port.id)} · ${esc(port.name)}</option>`).join("");
  if ([...select.options].some(option => option.value === before)) select.value = before;
}
function renderTopologyEditor() {
  const draft = editorDraft || readEditorDraft();
  $("#editor-add-link").disabled = !draft;
  if (!draft) {
    $("#topology-editor-svg").replaceChildren(); $("#editor-remove-link").disabled = true;
    for (const side of ['a','b']) { $(`#editor-${side}-device`).replaceChildren(); $(`#editor-${side}-port`).replaceChildren(); }
    return;
  }
  const svg = $("#topology-editor-svg"), width = 920, cellW = 174, cellH = 88;
  const cols = Math.max(1,Math.min(5,Math.ceil(Math.sqrt(draft.devices.length))));
  const rows = Math.ceil(draft.devices.length / cols), height = Math.max(180,rows*cellH+40);
  const positions = new Map();
  draft.devices.forEach((device,index) => {
    if (!editorLayout.has(device.id)) editorLayout.set(device.id,{x:22+(index%cols)*cellW,y:20+Math.floor(index/cols)*cellH});
    const position = editorLayout.get(device.id);
    position.x = Math.min(width-140,Math.max(0,position.x)); position.y = Math.min(height-48,Math.max(0,position.y));
    positions.set(device.id,editorLayout.get(device.id));
  });
  const parallel = new Map();
  draft.links.forEach(link => {
    const [a,b] = link.endpoints, ad=a.split(":")[0], bd=b.split(":")[0];
    if (!positions.has(ad) || !positions.has(bd)) return;
    const key=[ad,bd].sort().join("|");
    if (!parallel.has(key)) parallel.set(key,[]);
    parallel.get(key).push(link.id);
  });
  const edges = draft.links.map(link => {
    const [a,b] = link.endpoints, ad = a.split(":")[0], bd = b.split(":")[0], pa=positions.get(ad), pb=positions.get(bd);
    if (!pa || !pb) return "";
    const group=parallel.get([ad,bd].sort().join("|")) || [link.id];
    const slot=group.indexOf(link.id)-(group.length-1)/2;
    const ax=pa.x+70, ay=pa.y+24, bx=pb.x+70, by=pb.y+24, dx=bx-ax, dy=by-ay, len=Math.hypot(dx,dy)||1;
    const shift=(group.length>1 ? slot*28 : 0), nx=-dy/len*shift, ny=dx/len*shift;
    const labelX=(ax+bx)/2+nx, labelY=(ay+by)/2-4+ny;
    const coords=`x1="${ax+nx}" y1="${ay+ny}" x2="${bx+nx}" y2="${by+ny}"`;
    return `<g class="editor-edge ${editorSelectedLink===link.id?"selected":""}" data-editor-link="${esc(link.id)}"><line class="editor-edge-hit" ${coords}/><line class="editor-edge-visible" ${coords}/><text x="${labelX}" y="${labelY}">${esc(link.id)}</text></g>`;
  }).join("");
  const nodes = draft.devices.map(device => {
    const p=positions.get(device.id);
    const label = device.id.length > 16 ? device.id.slice(0,15)+"…" : device.id;
    return `<g class="editor-node" data-editor-device="${esc(device.id)}" transform="translate(${p.x},${p.y})" tabindex="0" role="button"><title>${esc(device.id)}</title><rect width="140" height="48" rx="6"/><text x="10" y="20">${esc(label)}</text><text class="editor-node-detail" x="10" y="37">${esc(device.management_ip)}</text></g>`;
  }).join("");
  svg.setAttribute("viewBox",`0 0 ${width} ${height}`); svg.innerHTML=edges+nodes;
  $("#editor-remove-link").disabled = !editorSelectedLink || !draft.links.some(link=>link.id===editorSelectedLink);
  const choices = draft.devices.map(device=>`<option value="${esc(device.id)}">${esc(device.id)}</option>`).join("");
  for (const side of ['a','b']) {
    const select=$(`#editor-${side}-device`), previous=select.value;
    select.innerHTML=choices;
    if ([...select.options].some(option=>option.value===previous)) select.value=previous;
    else if (side==='b' && select.options.length>1) select.selectedIndex=1;
    fillEditorPorts(side);
  }
}
function syncEditorDraft() {
  $("#replace-content").value=JSON.stringify(editorDraft,null,2);
  invalidateReplacement(); renderTopologyEditor();
}
$("#editor-reset-layout").addEventListener("click",()=>{ editorLayout.clear(); editorSelectedLink=null; editorDragging=null; renderTopologyEditor(); });
$("#visual-editor-toggle").addEventListener("click",() => {
  const panel=$("#visual-editor"); panel.hidden=!panel.hidden;
  if (!panel.hidden) { editorDraft=readEditorDraft(); editorSelectedLink=null; renderTopologyEditor(); }
});
$("#replace-content").addEventListener("input",() => { if (!$("#visual-editor").hidden) { editorDraft=readEditorDraft(); renderTopologyEditor(); } });
for (const side of ['a','b']) $("#editor-"+side+"-device").addEventListener("change",()=>fillEditorPorts(side));
$("#editor-add-link").addEventListener("click",() => {
  if (!editorDraft && !readEditorDraft()) return;
  const a=`${$("#editor-a-device").value}:${$("#editor-a-port").value}`, b=`${$("#editor-b-device").value}:${$("#editor-b-port").value}`;
  if (!a.split(':')[0] || !b.split(':')[0] || a.split(':')[0]===b.split(':')[0]) { $("#replace-error").textContent="请选择两台不同设备和有效接口。"; return; }
  if (editorDraft.links.some(link=>link.endpoints.includes(a)||link.endpoints.includes(b))) { $("#replace-error").textContent="所选接口已有链路，请先选择未连接的接口。"; return; }
  const mode=$("#editor-link-mode").value, vlan=Number($("#editor-vlan").value), idBase=`link-${editorDraft.links.length+1}`;
  let id=idBase,n=2; while(editorDraft.links.some(link=>link.id===id)) id=`${idBase}-${n++}`;
  editorDraft.links.push({id,endpoints:[a,b],mode,allowed_vlans:mode==='routed'?[]:[vlan],lldp:true,blocked:$("#editor-blocked").checked});
  $("#replace-error").textContent=""; editorSelectedLink=id; syncEditorDraft();
});
$("#editor-remove-link").addEventListener("click",() => {
  if (!editorDraft || !editorSelectedLink) return;
  editorDraft.links=editorDraft.links.filter(link=>link.id!==editorSelectedLink); editorSelectedLink=null; syncEditorDraft();
});
$("#topology-editor-svg").addEventListener("pointerdown",event=>{
  const node=event.target.closest('[data-editor-device]');
  if (!node) return;
  const point=editorLayout.get(node.dataset.editorDevice), svg=event.currentTarget, rect=svg.getBoundingClientRect();
  const vb=svg.viewBox.baseVal; editorDragging={id:node.dataset.editorDevice,dx:(event.clientX-rect.left)*vb.width/rect.width-point.x,dy:(event.clientY-rect.top)*vb.height/rect.height-point.y};
  svg.setPointerCapture(event.pointerId);
});
$("#topology-editor-svg").addEventListener("pointermove",event=>{
  if (!editorDragging) return;
  const svg=event.currentTarget, rect=svg.getBoundingClientRect(), vb=svg.viewBox.baseVal;
  editorLayout.set(editorDragging.id,{x:Math.min(vb.width-140,Math.max(0,(event.clientX-rect.left)*vb.width/rect.width-editorDragging.dx)),y:Math.min(vb.height-48,Math.max(0,(event.clientY-rect.top)*vb.height/rect.height-editorDragging.dy))});
  renderTopologyEditor();
});
$("#topology-editor-svg").addEventListener("pointerup",()=>{editorDragging=null;});
$("#topology-editor-svg").addEventListener("pointercancel",()=>{editorDragging=null;});
$("#topology-editor-svg").addEventListener("click",event=>{
  const edge=event.target.closest('[data-editor-link]');
  if (edge) { editorSelectedLink=edge.dataset.editorLink; renderTopologyEditor(); }
});
$("#replace-button").addEventListener("click",async () => {
  try {
    const lab = S.lab; await refreshLab();
    const revision = S.topology.revision, config = await api(`/v1/labs/${url(lab)}/config`);
    if (S.lab !== lab) return;
    invalidateReplacement();
    $("#replace-dialog").dataset.revision = revision;
    $("#replace-dialog").dataset.lab = lab;
    $("#replace-content").value = JSON.stringify(config,null,2);
    editorDraft=null; editorSelectedLink=null;
    if (!$("#visual-editor").hidden) renderTopologyEditor();
    $("#replace-startup").checked = false; $("#replace-error").textContent = "";
    $("#replace-dialog").showModal();
  } catch(error) { toast(error.message,true); }
});
$("#replace-form").addEventListener("submit",async event => {
  event.preventDefault(); invalidateReplacement(); $("#replace-error").textContent = "";
  const button = $("button[type=submit]",event.target); button.disabled = true;
  try {
    const lab = $("#replace-dialog").dataset.lab, revision = Number($("#replace-dialog").dataset.revision);
    const body = {topology:JSON.parse($("#replace-content").value),save_startup:$("#replace-startup").checked};
    const summary = await api(`/v1/labs/${url(lab)}/config/preview`,{method:"POST",body,revision});
    $("#replace-preview").textContent = `新增设备：${summary.added_devices.join(", ") || "无"}；删除设备：${summary.removed_devices.join(", ") || "无"}；修改设备：${summary.modified_devices.join(", ") || "无"}。链路变化：${summary.links_changed ? "是" : "否"}；主机变化：${summary.hosts_changed ? "是" : "否"}；身份变化：${summary.identity_changes.join(", ") || "无"}。${body.save_startup ? "保存新启动配置。" : "保留已有启动配置。"}`;
    replacementDraft = {lab,revision,body};
    $("#replace-apply").disabled = S.labs.find(item => item.id === lab)?.status !== "stopped";
    if ($("#replace-apply").disabled) $("#replace-error").textContent = "请停止实验后再应用配置。";
  } catch(error) { $("#replace-error").textContent = error.message; }
  finally { button.disabled = false; }
});
$("#replace-apply").addEventListener("click",async () => {
  if (!replacementDraft || S.busy) return;
  const draft = replacementDraft; setBusy(true); $("#replace-apply").disabled = true;
  try {
    await api(`/v1/labs/${url(draft.lab)}/config`,{method:"PUT",body:draft.body,revision:draft.revision});
    $("#replace-dialog").close(); S.dirty = false; S.selected = null; S.snapshot = null;
    await loadLabs(draft.lab); renderInspector(); toast("实验配置已替换");
  } catch(error) { $("#replace-error").textContent = error.status === 409 ? "实验状态或版本已变化；草稿已保留。关闭窗口并刷新后重新预览。" : error.message; }
  finally { invalidateReplacement(); setBusy(false); }
});
$("#delete-button").addEventListener("click",() => {
  deletionDraft = {lab:S.lab,revision:S.topology.revision};
  $("#delete-description").textContent = `即将删除实验 ${S.lab}。`;
  $("#delete-name").value = ""; $("#delete-error").textContent = ""; $("#delete-dialog").showModal();
});
$("#delete-form").addEventListener("submit",async event => {
  event.preventDefault();
  if (!deletionDraft || S.busy) return;
  if ($("#delete-name").value !== deletionDraft.lab) { $("#delete-error").textContent = "实验名称不匹配。"; return; }
  setBusy(true);
  try {
    await api(`/v1/labs/${url(deletionDraft.lab)}`,{method:"DELETE",revision:deletionDraft.revision});
    $("#delete-dialog").close(); await loadLabs(); toast("实验已删除");
  } catch(error) { $("#delete-error").textContent = error.status === 409 ? "实验版本已变化，请关闭窗口并刷新后重试。" : error.message; }
  finally { setBusy(false); }
});

$("#scenario-form").addEventListener("submit",async event => {
  event.preventDefault(); const f = formData(event.target), lab = S.lab;
  await mutate(async () => {
    const run = await api(`/v1/scenarios/${url(f.scenario)}/run`,{method:"POST",body:{lab}});
    $("#scenario-result").textContent = `操作 ${run.operation_id} · ${run.status}`;
    async function poll() {
      try { const current = await api(`/v1/operations/${url(run.operation_id)}`); if (S.lab !== lab) return;
        $("#scenario-result").textContent = `操作 ${run.operation_id} · ${current.status}${current.error ? ` · ${current.error}` : ""}`;
        if (current.status === "running") setTimeout(poll,1000); else { await refreshLab(); await refreshSelection(); }
      } catch(error) { $("#scenario-result").textContent = error.message; }
    }
    setTimeout(poll,1000);
  },"场景已启动");
});
$("#notification-form").addEventListener("submit",async event => {
  event.preventDefault(); const f = formData(event.target);
  let payload = {};
  if (String(f.payload || "").trim()) {
    try { payload = JSON.parse(f.payload); } catch (error) { toast("参数不是合法 JSON"); return; }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) { toast("参数必须是 JSON 对象"); return; }
  }
  await mutate(async () => {
    const result = await api(`/v1/labs/${url(S.lab)}/notifications`,{method:"POST",body:{devices:[f.device],type:f.type,count:Number(f.count),payload}});
    const unmapped = result.unmapped_devices || [];
    toast(unmapped.length ? `事件已写入，但 ${unmapped.join("、")} 的 profile 未声明该告警 OID，不会发出 trap` : "通知事件已注入；投递结果请查看事件列表");
  },"通知事件已注入");
});
setInterval(async () => {
  if (document.hidden || S.busy || S.refreshing || !S.lab || $$("dialog").some(dialog => dialog.open)) return;
  S.refreshing = true;
  try { await refreshLab(); await refreshSelection(); $("#connection-label").textContent = "控制服务已连接"; }
  catch(error) { $("#connection-label").textContent = "控制服务连接中断"; }
  finally { S.refreshing = false; }
},5000);
loadLabs().catch(error => { if (error.status !== 401) toast(error.message,true); });
