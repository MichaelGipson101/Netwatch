// shell.js — extracted from core.js (Netwatch 4.0 page split). Code moved verbatim.

// Theme: the inline <head> script resolves auto -> light|dark before first
// paint and exposes window.nwApplyTheme. setTheme() handles button wiring
// and delegates actual theme application to that head script.
function setTheme(mode){
  localStorage.setItem('nw-theme', mode);
  if(window.nwApplyTheme) window.nwApplyTheme();
  document.querySelectorAll('.theme-toggle button').forEach(b => {
    const active = b.dataset.themeBtn === mode;
    b.classList.toggle('active', active);
    b.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

const REFRESH = 5000;

let _firstRender = true;

let lastOk = true;

let lastData = null;

function clockTick(){
  const d = new Date();
  const p = n => String(n).padStart(2,'0');
  document.getElementById('clock').textContent =
    d.getFullYear() + '-' + p(d.getMonth()+1) + '-' + p(d.getDate()) + '  ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}
