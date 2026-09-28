// LabForge console - xterm.js + WebSocket to virsh console via PTY.
// xterm.js is vendored under /static/xterm so there is no third-party CDN.

let consoleTerminal = null;
let consoleWs = null;
let consoleFitAddon = null;
let consoleResizeObserver = null;
let consoleOpener = null;

const XTERM_BASE = '/static/xterm';

function openConsole(vmName) {
  // Guard against a double-open leaking a terminal and a WebSocket.
  if (consoleTerminal || consoleWs) {
    closeConsole();
  }
  consoleOpener = document.activeElement;

  const modal = document.getElementById('console-modal');
  if (!modal) {
    const div = document.createElement('div');
    div.id = 'console-modal';
    div.className = 'modal hidden';
    div.setAttribute('role', 'dialog');
    div.setAttribute('aria-modal', 'true');
    div.setAttribute('aria-label', 'Serial console');
    div.innerHTML = `
      <div class="modal-content modal-content-wide">
        <div class="modal-header">
          <h2>Console</h2>
          <button class="btn-icon" type="button" data-console-close aria-label="Close">&times;</button>
        </div>
        <div id="console-terminal"></div>
        <div class="console-footer" id="console-footer">
          <span class="text-muted">Press <kbd>Ctrl+]</kbd> to exit console</span>
        </div>
      </div>`;
    div.querySelector('[data-console-close]').addEventListener('click', closeConsole);
    document.body.appendChild(div);
  }
  const title = document.querySelector('#console-modal .modal-header h2');
  if (title) title.textContent = `Console - ${vmName}`;
  document.getElementById('console-modal').classList.remove('hidden');
  mountTerminal(vmName);
}

function closeConsole() {
  const modal = document.getElementById('console-modal');
  if (modal) modal.classList.add('hidden');
  if (consoleResizeObserver) {
    consoleResizeObserver.disconnect();
    consoleResizeObserver = null;
  }
  if (consoleWs) {
    consoleWs.onclose = null;
    consoleWs.close();
    consoleWs = null;
  }
  if (consoleTerminal) {
    consoleTerminal.dispose();
    consoleTerminal = null;
  }
  consoleFitAddon = null;
  if (consoleOpener && consoleOpener.focus) consoleOpener.focus();
  consoleOpener = null;
}

function consoleError(message) {
  const footer = document.getElementById('console-footer');
  if (footer) {
    footer.innerHTML = `<span class="text-danger">${message}</span>`;
  }
}

async function mountTerminal(vmName) {
  const container = document.getElementById('console-terminal');
  if (!container) return;

  // Load xterm.js lazily on first open, from our own origin.
  try {
    if (typeof Terminal === 'undefined') {
      await loadScript(`${XTERM_BASE}/xterm.js`);
      await loadCss(`${XTERM_BASE}/xterm.css`);
    }
    if (typeof FitAddon === 'undefined') {
      await loadScript(`${XTERM_BASE}/addon-fit.js`);
    }
  } catch (e) {
    consoleError('Could not load the terminal assets. Reload the page and try again.');
    return;
  }

  container.innerHTML = '';

  consoleTerminal = new Terminal({
    cursorBlink: true,
    fontSize: 14,
    fontFamily: 'Menlo, Monaco, "Courier New", monospace',
    theme: {
      background: '#000000',
      foreground: '#e4e7f1',
      cursor: '#4f8cff',
      selectionBackground: '#4f8cff40',
    },
    scrollback: 5000,
  });

  consoleFitAddon = new FitAddon.FitAddon();
  consoleTerminal.loadAddon(consoleFitAddon);
  consoleTerminal.open(container);
  consoleFitAddon.fit();

  // Ctrl+] exits the console instead of being sent to the guest.
  consoleTerminal.attachCustomKeyEventHandler((event) => {
    if (event.type === 'keydown' && event.ctrlKey && event.key === ']') {
      closeConsole();
      return false;
    }
    return true;
  });

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  consoleWs = new WebSocket(`${proto}://${location.host}/ws/console/${encodeURIComponent(vmName)}`);

  consoleWs.onopen = () => {
    consoleTerminal.clear();
    sendResize(consoleWs, consoleTerminal);
  };

  consoleWs.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === 'output') {
        consoleTerminal.write(msg.data);
      } else if (msg.type === 'connected') {
        consoleTerminal.write(`\r\n\x1b[32m${msg.message}\x1b[0m\r\n\r\n`);
      } else if (msg.type === 'error') {
        consoleTerminal.write(`\r\n\x1b[31mError: ${msg.message}\x1b[0m\r\n`);
      }
    } catch (e) {
      consoleTerminal.write(event.data);
    }
  };

  consoleWs.onclose = () => {
    if (consoleTerminal) {
      consoleTerminal.write('\r\n\x1b[33m[console disconnected]\x1b[0m\r\n');
    }
  };

  consoleTerminal.onData((data) => {
    if (consoleWs && consoleWs.readyState === WebSocket.OPEN) {
      consoleWs.send(JSON.stringify({ type: 'input', data }));
    }
  });

  consoleResizeObserver = new ResizeObserver(() => {
    if (!consoleTerminal) return;
    consoleFitAddon.fit();
    sendResize(consoleWs, consoleTerminal);
  });
  consoleResizeObserver.observe(container);

  consoleTerminal.focus();
}

function sendResize(ws, term) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }));
  }
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error(`Failed to load ${src}`));
    document.head.appendChild(s);
  });
}

function loadCss(href) {
  return new Promise((resolve, reject) => {
    const l = document.createElement('link');
    l.rel = 'stylesheet';
    l.href = href;
    l.onload = resolve;
    l.onerror = () => reject(new Error(`Failed to load ${href}`));
    document.head.appendChild(l);
  });
}

// ---------- Graphical console (VNC via vendored noVNC) ----------

let screenOpener = null;

function openScreen(vmName) {
  screenOpener = document.activeElement;
  let modal = document.getElementById('screen-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'screen-modal';
    modal.className = 'modal hidden';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-labelledby', 'screen-title');
    modal.innerHTML = `
      <div class="modal-content modal-content-wide">
        <div class="modal-header">
          <h2>Screen - <span id="screen-title"></span></h2>
          <button class="btn-icon" type="button" data-screen-close aria-label="Close">&times;</button>
        </div>
        <iframe id="screen-frame" src="about:blank" title="VM graphical console"></iframe>
        <div class="console-footer">
          <span class="text-muted">Graphical console (VNC) - noVNC toolbar has extra keys, clipboard &amp; fullscreen</span>
        </div>
      </div>`;
    modal.querySelector('[data-screen-close]').addEventListener('click', closeScreen);
    document.body.appendChild(modal);
  }
  document.getElementById('screen-title').textContent = vmName;
  modal.classList.remove('hidden');
  document.getElementById('screen-frame').src =
    `/static/novnc/vnc.html?autoconnect=true&resize=scale&path=/ws/vnc/${encodeURIComponent(vmName)}`;
}

function closeScreen() {
  const modal = document.getElementById('screen-modal');
  if (modal) modal.classList.add('hidden');
  const frame = document.getElementById('screen-frame');
  if (frame) frame.src = 'about:blank';  // dropping the src closes the WebSocket
  if (screenOpener && screenOpener.focus) screenOpener.focus();
  screenOpener = null;
}

// Escape closes whichever console modal is open.
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  const consoleModal = document.getElementById('console-modal');
  if (consoleModal && !consoleModal.classList.contains('hidden')) {
    closeConsole();
    return;
  }
  const screenModal = document.getElementById('screen-modal');
  if (screenModal && !screenModal.classList.contains('hidden')) {
    closeScreen();
  }
});
