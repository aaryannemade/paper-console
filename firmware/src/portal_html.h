// Setup / status page, served at "/" in both setup-AP and network mode.
#pragma once

static const char kPortalHtml[] = R"HTML(<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Console relay</title>
<style>
body{font:15px/1.4 system-ui,sans-serif;margin:0;background:#f4f1ea;color:#222}
main{max-width:30rem;margin:auto;padding:1rem}
h1{font-size:1.3rem;margin:.2rem 0 1rem}h2{font-size:1rem;margin:1.2rem 0 .4rem}
section,form{background:#fff;border:1px solid #ddd;border-radius:8px;padding:.8rem 1rem;margin-bottom:1rem}
table{width:100%;border-collapse:collapse}td{padding:.2rem 0;vertical-align:top}td:first-child{color:#666;width:38%}
label{display:block;margin:.6rem 0 .2rem;font-weight:600}
input{box-sizing:border-box;width:100%;padding:.5rem;font:inherit;border:1px solid #bbb;border-radius:6px}
.row{display:flex;gap:.5rem}.row input{flex:1}
button{font:inherit;padding:.55rem .9rem;border:0;border-radius:6px;background:#222;color:#fff;margin-top:.8rem}
button.alt{background:#e4e0d6;color:#222}
small{color:#666}.ok{color:#18794e}.bad{color:#b42318}#msg{min-height:1.2em}
#nets{list-style:none;margin:.5rem 0 0;padding:0;border:1px solid #ddd;border-radius:6px;max-height:14rem;overflow-y:auto}
#nets:empty{display:none}#nets li{border-top:1px solid #eee}#nets li:first-child{border-top:0}
#nets button{display:flex;justify-content:space-between;width:100%;margin:0;border-radius:0;background:none;color:#222;text-align:left}
#nets button.sel{background:#e4e0d6}#nets span{color:#666;white-space:nowrap;margin-left:.5rem}
</style></head><body><main>
<h1>Paper Console relay</h1>
<section><table id="st"><tr><td>Status</td><td>loading&hellip;</td></tr></table></section>
<form id="f">
<h2>WiFi network</h2>
<label for="ssid">Network name</label>
<div class="row"><input id="ssid" name="ssid" maxlength="32" required autocomplete="off">
<button type="button" class="alt" id="scan" style="margin-top:0">Scan</button></div>
<ul id="nets"></ul><small id="netmsg"></small>
<label for="pass">Password</label>
<input id="pass" name="pass" type="password" maxlength="63" autocomplete="off">
<small id="passhint">Leave blank for an open network.</small>
<h2>Paper Console server</h2>
<label for="server">Server URL</label>
<input id="server" name="server" placeholder="http://192.168.1.20:8000" required>
<small>Address of the Paper Console container, as reachable from this device.</small>
<label for="token">Relay token</label>
<input id="token" name="token" type="password" maxlength="128" autocomplete="off">
<small id="tokhint">Same value as PC1_RELAY_TOKEN on the server (at least 16 characters). Only used between the server and this relay; it is never shown here.</small>
<button>Save and restart</button>
<p id="msg"></p>
</form>
<section><button type="button" class="alt" id="test" style="margin-top:0">Print test page</button>
<small>Prints directly from this device to check the printer wiring.</small><p id="tmsg"></p></section>
</main><script>
const $=id=>document.getElementById(id);let filled=false;
function row(k,v,c){return `<tr><td>${k}</td><td${c?` class="${c}"`:''}>${v}</td></tr>`}
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function status(){try{const s=await (await fetch('/ui/status')).json();
let h=row('Device',esc(s.device_id)+' &middot; v'+esc(s.firmware));
h+=row('WiFi',s.wifi_connected?esc(s.ssid)+' &middot; '+esc(s.ip):(s.ssid?'connecting to '+esc(s.ssid)+'&hellip;':'not configured'),s.wifi_connected?'ok':'bad');
if(s.ap_active)h+=row('Setup network',esc(s.ap_ssid)+' &middot; '+esc(s.ap_ip));
h+=row('Server',s.server?esc(s.server):'not set',s.server?'':'bad');
h+=row('Link',esc(s.link.message)+(s.link.last_ok_s>=0&&!s.link.ok?' (last ok '+s.link.last_ok_s+' s ago)':''),s.link.ok?'ok':'bad');
h+=row('Printer',esc(s.printer.state)+(s.printer.active_job?' &middot; job '+esc(s.printer.active_job.slice(0,8)):''));
h+=row('Last job',esc(s.printer.last_job));
$('st').innerHTML=h;
if(!filled){filled=true;$('ssid').value=s.ssid||'';$('server').value=s.server||'';
if(s.token_set)$('tokhint').textContent='A token is saved. Leave blank to keep it, or enter a new one (same value as PC1_RELAY_TOKEN on the server).';
if(s.ssid)$('passhint').textContent='Leave blank to keep the saved password (same network) or for an open network.'}
}catch(e){}}
function bars(r){return r>=-55?'▂▄▆█':r>=-67?'▂▄▆':r>=-78?'▂▄':'▂'}
function showNets(s){const ul=$('nets');ul.innerHTML='';
for(const x of s.networks){const li=document.createElement('li'),b=document.createElement('button');
b.type='button';b.textContent=x.ssid;const sp=document.createElement('span');sp.textContent=(x.open?'open ':'')+bars(x.rssi);b.append(sp);
if(x.ssid===$('ssid').value)b.className='sel';
b.onclick=()=>{$('ssid').value=x.ssid;for(const o of ul.querySelectorAll('button'))o.className='';b.className='sel';$('pass').focus()};
li.append(b);ul.append(li)}
$('netmsg').textContent=s.networks.length?'Tap a network to select it.':'No networks found. Tap Scan to try again, or type the name.'}
async function nets(refresh){return (await fetch('/ui/scan'+(refresh?'?refresh=1':''))).json()}
$('scan').onclick=async()=>{const b=$('scan');b.disabled=true;b.textContent='…';$('netmsg').textContent='Scanning…';
try{let s=await nets(true);for(let i=0;i<20&&s.scanning;i++){await new Promise(r=>setTimeout(r,500));s=await nets(false)}showNets(s)}
catch(e){$('netmsg').textContent='Scan interrupted; tap Scan again.'}b.disabled=false;b.textContent='Scan'};
nets(false).then(showNets).catch(()=>{});
$('f').onsubmit=async e=>{e.preventDefault();$('msg').className='';$('msg').textContent='Saving…';
try{const r=await fetch('/ui/save',{method:'POST',body:new URLSearchParams(new FormData($('f')))});const j=await r.json();
$('msg').className=j.ok?'ok':'bad';$('msg').textContent=j.message}catch(e){$('msg').textContent='Device restarting…'}};
$('test').onclick=async()=>{try{const j=await (await fetch('/ui/test-print',{method:'POST'})).json();$('tmsg').textContent=j.message}catch(e){$('tmsg').textContent='Failed'}};
status();setInterval(status,4000);
</script></body></html>
)HTML";
