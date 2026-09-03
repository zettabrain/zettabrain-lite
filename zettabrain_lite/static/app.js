// ── Theme ────────────────────────────────────────────
function initTheme() {
  const saved = localStorage.getItem('zb_theme');
  if (saved === 'light') {
    document.documentElement.classList.add('light');
  }
  updateThemeIcon();
}

function toggleTheme() {
  const isLight = document.documentElement.classList.toggle('light');
  localStorage.setItem('zb_theme', isLight ? 'light' : 'dark');
  updateThemeIcon();
}

function updateThemeIcon() {
  const btn = document.getElementById('theme-toggle');
  if (!btn) return;
  const isLight = document.documentElement.classList.contains('light');
  btn.innerHTML = isLight ? '&#9728;' : '&#9790;';
  btn.title = isLight ? 'Switch to dark mode' : 'Switch to light mode';
}

initTheme();

// ── State ────────────────────────────────────────────
let ws = null;
let isStreaming = false;
let currentModel = '';
let statusData = {};
let currentSources = [];
let currentAnswer = '';
let currentMsgId = null;
let selectedSkill = null;

// ── Auth ─────────────────────────────────────────────
function checkAuth() {
  const user = localStorage.getItem('zb_user');
  if (user) {
    document.getElementById('signin-overlay').style.display = 'none';
    showUserInfo(JSON.parse(user));
  } else {
    initGoogleSignIn();
  }
}

async function initGoogleSignIn() {
  try {
    const r = await fetch('/api/settings');
    const data = await r.json();
    const clientId = (data.settings || {}).google_client_id;

    if (clientId && window.google) {
      google.accounts.id.initialize({
        client_id: clientId,
        callback: handleGoogleSignIn,
      });
      google.accounts.id.renderButton(
        document.getElementById('google-signin-btn'),
        { theme: 'filled_black', size: 'large', shape: 'pill', text: 'sign_in_with', width: 300 }
      );
    } else {
      const gBtn = document.getElementById('google-signin-btn');
      if (gBtn) gBtn.style.display = 'none';
      const divider = document.getElementById('signin-divider');
      if (divider) divider.style.display = 'none';
    }
  } catch(e) {
    console.error('Failed to init Google Sign-In', e);
    const gBtn = document.getElementById('google-signin-btn');
    if (gBtn) gBtn.style.display = 'none';
    const divider = document.getElementById('signin-divider');
    if (divider) divider.style.display = 'none';
  }
}

function handleGoogleSignIn(response) {
  const payload = JSON.parse(atob(response.credential.split('.')[1]));
  const user = {
    name: payload.name,
    email: payload.email,
    picture: payload.picture,
    given_name: payload.given_name,
  };
  localStorage.setItem('zb_user', JSON.stringify(user));
  document.getElementById('signin-overlay').style.display = 'none';
  showUserInfo(user);
}

function continueWithoutSignIn() {
  localStorage.setItem('zb_user', JSON.stringify({ name: 'Guest', email: '' }));
  document.getElementById('signin-overlay').style.display = 'none';
  showUserInfo({ name: 'Guest' });
}

function showUserInfo(user) {
  const container = document.getElementById('user-info');
  if (!container) return;

  if (user.picture) {
    container.innerHTML = `
      <img class="user-avatar" src="${user.picture}" referrerpolicy="no-referrer">
      <div style="flex:1;min-width:0;">
        <div class="user-name">${user.name}</div>
        ${user.email ? `<div class="user-email">${user.email}</div>` : ''}
      </div>
      ${user.email ? '<button class="btn btn-ghost btn-sm" onclick="signOut()" style="flex-shrink:0;">Sign out</button>' : ''}
    `;
  } else {
    const initial = (user.name || 'G')[0].toUpperCase();
    container.innerHTML = `
      <div class="user-avatar-placeholder">${initial}</div>
      <div style="flex:1;min-width:0;">
        <div class="user-name">${user.name}</div>
      </div>
    `;
  }
}

function signOut() {
  localStorage.removeItem('zb_user');
  location.reload();
}

// ── Init ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  checkAuth();
  refreshStatus();
  loadModels();
  loadSkills();
  loadWelcome();
  loadHistory();
  connectWS();
  checkTrialStatus();
  setInterval(refreshStatus, 30000);
});

// ── WebSocket ─────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/chat`);
  ws.onmessage = (e) => handleWSMessage(JSON.parse(e.data));
  ws.onclose = () => setTimeout(connectWS, 3000);
  ws.onerror = () => ws.close();
}

function handleWSMessage(data) {
  if (data.type === 'sources') {
    currentSources = data.sources || [];
  } else if (data.type === 'token') {
    appendToken(data.token);
  } else if (data.type === 'done') {
    finishStreaming(data);
  } else if (data.type === 'error') {
    finishStreaming(null);
    toast(data.message, 'error');
    if (data.message && (data.message.includes('trial') || data.message.includes('Trial') || data.message.includes('free trial'))) {
      showFreeGuide();
    }
  }
}

// ── Status ────────────────────────────────────────────
async function refreshStatus() {
  try {
    const r = await fetch('/api/status');
    statusData = await r.json();

    setDot('dot-ollama', statusData.ollama.running ? 'green' : 'red');
    setDot('dot-vector', statusData.vectorstore.chunks > 0 ? 'green' : statusData.vectorstore.exists ? 'yellow' : 'red');
    const hasSources = statusData.storage && statusData.storage.sources && statusData.storage.sources.length > 0;
    setDot('dot-storage', hasSources ? 'green' : 'yellow');

    document.getElementById('stat-chunks').textContent = fmtNum(statusData.vectorstore.chunks);
    document.getElementById('stat-docs').textContent = fmtNum(statusData.storage.doc_count || 0);
    document.getElementById('ingest-chunks').textContent = fmtNum(statusData.vectorstore.chunks);
    document.getElementById('ingest-files').textContent = fmtNum(statusData.sources.length);
    document.getElementById('ingest-docs').textContent = fmtNum(statusData.storage.doc_count || 0);

    // Sources list
    const list = document.getElementById('sources-list');
    if (statusData.sources.length === 0) {
      list.innerHTML = '<li style="color:var(--text3)">No files ingested yet</li>';
    } else {
      list.innerHTML = statusData.sources.map(s => `<li title="${s}">${s}</li>`).join('');
    }
  } catch(e) {
    setDot('dot-ollama', 'red');
  }
}

// ── Models (provider-prefixed) ────────────────────────
async function loadModels() {
  try {
    const r = await fetch('/api/models');
    const data = await r.json();
    const models = data.models || [];

    const sel = document.getElementById('model-select');
    const settingsSel = document.getElementById('settings-model-select');
    sel.innerHTML = '';
    settingsSel.innerHTML = '';

    if (models.length === 0) {
      sel.innerHTML = '<option value="">No models available</option>';
      settingsSel.innerHTML = '<option value="">No models available</option>';
      return;
    }

    models.forEach(m => {
      const opt1 = document.createElement('option');
      opt1.value = m.id;
      opt1.textContent = m.label;
      sel.appendChild(opt1);

      const opt2 = document.createElement('option');
      opt2.value = m.id;
      opt2.textContent = m.label;
      settingsSel.appendChild(opt2);
    });

    currentModel = sel.value;
  } catch(e) {
    console.error('Failed to load models', e);
  }
}

function setDot(id, cls) {
  const el = document.getElementById(id);
  if (el) el.className = 'dot ' + cls;
}

function fmtNum(n) {
  if (n >= 1000) return (n/1000).toFixed(1) + 'k';
  return String(n || 0);
}

// ── Chat ──────────────────────────────────────────────
function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

function sendSuggestion(el) {
  document.getElementById('chat-input').value = el.textContent;
  sendMessage();
}

async function sendMessage() {
  const input = document.getElementById('chat-input');
  const question = input.value.trim();
  if (!question || isStreaming) return;

  const welcome = document.getElementById('welcome');
  if (welcome) welcome.style.display = 'none';
  document.getElementById('panel-chat').classList.remove('welcome-active');

  input.value = '';
  input.style.height = 'auto';

  currentModel = document.getElementById('model-select').value;

  if (selectedSkill) {
    renderMessage('user', question);
    const assistantId = 'msg-' + Date.now();
    renderMessage('assistant', '', assistantId);
    isStreaming = true;
    setSendDisabled(true);

    try {
      const r = await fetch('/api/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ input: question, skill_name: selectedSkill, model: currentModel })
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || 'Generation failed');

      currentAnswer = data.content || '';
      const bubble = document.querySelector(`#${assistantId} .msg-bubble`);
      if (bubble) bubble.innerHTML = mdToHtml(currentAnswer);

      const meta = document.getElementById(assistantId + '-meta');
      if (meta) {
        meta.innerHTML = `${selectedSkill} · ${data.model || currentModel}`;
        if (data.id) {
          meta.innerHTML += ` <span class="export-btns">` +
            `<button class="btn-export" onclick="exportDoc(${data.id}, 'pdf')" title="Download PDF">PDF</button>` +
            `<button class="btn-export" onclick="exportDoc(${data.id}, 'docx')" title="Download Word">Word</button>` +
            `</span>`;
        }
      }

      loadHistory();
      currentAnswer = '';
      isStreaming = false;
      setSendDisabled(false);
      input.focus();
    } catch(e) {
      const bubble = document.querySelector(`#${assistantId} .msg-bubble`);
      if (bubble) bubble.innerHTML = '<span style="color:var(--red)">Error: ' + e.message + '</span>';
      if (e.message && (e.message.includes('trial') || e.message.includes('Trial'))) showFreeGuide();
      isStreaming = false;
      setSendDisabled(false);
      toast(e.message, 'error');
    }
    return;
  }

  renderMessage('user', question);
  const assistantId = 'msg-' + Date.now();
  renderMessage('assistant', '', assistantId);

  isStreaming = true;
  setSendDisabled(true);

  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ question, model: currentModel }));
  } else {
    try {
      const r = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, model: currentModel })
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || 'Request failed');
      currentAnswer = data.answer || '';
      currentSources = data.sources || [];
      finishStreaming(data);
    } catch(e) {
      const bubble = document.querySelector(`#${assistantId} .msg-bubble`);
      if (bubble) bubble.textContent = 'Error: ' + e.message;
      finishStreaming(null);
    }
  }
}

function renderMessage(role, content, id) {
  const area = document.getElementById('chat-area');
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  if (id) div.id = id;

  const avatar = role === 'user' ? '&#128100;' : '&#9889;';
  div.innerHTML = `
    <div class="msg-avatar">${avatar}</div>
    <div class="msg-body">
      <div class="msg-bubble">${content}${role === 'assistant' && !content ? '<span class="cursor"></span>' : ''}</div>
      ${role === 'assistant' ? `<div class="msg-meta" id="${id ? id+'-meta' : ''}"></div>` : ''}
    </div>
  `;

  area.appendChild(div);
  area.scrollTop = area.scrollHeight;
  if (id) currentMsgId = id;
}

function appendToken(token) {
  if (!currentMsgId) return;
  const bubble = document.querySelector(`#${currentMsgId} .msg-bubble`);
  if (!bubble) return;

  currentAnswer += token;
  const cursor = bubble.querySelector('.cursor');
  if (cursor) cursor.remove();
  bubble.textContent = currentAnswer;
  const cur = document.createElement('span');
  cur.className = 'cursor';
  bubble.appendChild(cur);

  document.getElementById('chat-area').scrollTop = 999999;
}

function finishStreaming(data) {
  isStreaming = false;
  setSendDisabled(false);

  if (currentMsgId) {
    const bubble = document.querySelector(`#${currentMsgId} .msg-bubble`);
    if (bubble) bubble.innerHTML = mdToHtml(currentAnswer);

    if (currentSources.length > 0) renderSources(currentSources, currentMsgId);

    if (data && data.model) {
      const meta = document.getElementById(currentMsgId + '-meta');
      if (meta) {
        let timingStr = '';
        if (data.timing) {
          const rMs = data.timing.retrieve_ms;
          const gMs = data.timing.generate_ms;
          const rStr = rMs >= 1000 ? (rMs/1000).toFixed(1)+'s' : rMs+'ms';
          const gStr = gMs >= 1000 ? (gMs/1000).toFixed(1)+'s' : gMs+'ms';
          timingStr = ` · ${rStr} retrieve · ${gStr} generate`;
        }
        meta.textContent = `${data.model} · ${data.chunks_searched || 0} chunks${timingStr}`;
      }
    }
  }

  currentAnswer = '';
  currentSources = [];
  document.getElementById('chat-input').focus();
}

function renderSources(sources, msgId) {
  if (!sources || sources.length === 0) return;
  const id = msgId || currentMsgId;
  if (!id) return;

  const seen = new Set();
  const unique = sources.filter(s => { if (seen.has(s.filename)) return false; seen.add(s.filename); return true; });

  const chips = document.createElement('div');
  chips.className = 'source-chips';
  unique.forEach(s => {
    const chip = document.createElement('div');
    chip.className = 'chip';
    chip.textContent = s.filename;
    chip.onclick = () => showSourceModal(s);
    chips.appendChild(chip);
  });

  const bubble = document.querySelector(`#${id} .msg-bubble`);
  if (bubble) bubble.after(chips);
}

function setSendDisabled(disabled) {
  const btn = document.getElementById('send-btn');
  const input = document.getElementById('chat-input');
  btn.disabled = disabled;
  input.disabled = disabled;
  btn.innerHTML = disabled ? '<div class="spinner" style="width:16px;height:16px;border-width:2px;border-top-color:#fff"></div>' : '&#10148;';
}

// ── Markdown renderer ──────────────────────────────────
function mdToHtml(text) {
  if (!text) return '';
  let s = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  s = s.replace(/```[\s\S]*?```/g, m => { const inner = m.slice(3, -3).replace(/^\w*\n/, ''); return `<pre><code>${inner}</code></pre>`; });
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  s = s.replace(/((?:^[ \t]*[-*][ \t].+\n?)+)/gm, block => {
    const items = block.trim().split('\n').map(l => `<li>${l.replace(/^[ \t]*[-*][ \t]/, '').trim()}</li>`).join('');
    return `<ul>${items}</ul>`;
  });
  s = s.replace(/((?:^[ \t]*\d+\.[ \t].+\n?)+)/gm, block => {
    const items = block.trim().split('\n').map(l => `<li>${l.replace(/^[ \t]*\d+\.[ \t]/, '').trim()}</li>`).join('');
    return `<ol>${items}</ol>`;
  });
  s = s.split(/\n{2,}/).map(para => {
    para = para.trim();
    if (!para) return '';
    if (/^<(ul|ol|pre|li)/.test(para)) return para;
    return `<p>${para.replace(/\n/g, '<br>')}</p>`;
  }).join('');
  return s;
}

// ── Ingest ────────────────────────────────────────────
async function runIngest() {
  const folder = document.getElementById('ingest-folder').value.trim() || null;
  const rebuild = document.getElementById('ingest-rebuild').checked;
  const btn = document.getElementById('ingest-btn');
  const log = document.getElementById('ingest-log');

  btn.disabled = true; btn.textContent = 'Ingesting...';
  log.textContent = `Starting ingestion${folder ? ' from ' + folder : ''}...\n`;

  try {
    const r = await fetch('/api/ingest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder, rebuild })
    });
    const data = await r.json();
    if (!r.ok) {
      log.textContent += '\nError: ' + (data.detail || r.statusText);
      toast('Ingestion failed', 'error');
    } else if (data.success) {
      log.textContent += data.output || 'Done.';
      log.textContent += `\n\nIngestion complete. Vector store: ${data.chunks} chunks.`;
      toast('Ingestion complete!', 'success');
    } else {
      log.textContent += '\nError: ' + (data.error || 'Unknown error');
      toast('Ingestion failed', 'error');
    }
    refreshStatus();
  } catch(e) {
    log.textContent += '\nError: ' + e.message;
    toast('Request failed', 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Start Ingest';
    log.scrollTop = log.scrollHeight;
  }
}

// ── Vector Store ──────────────────────────────────────
async function clearVectorStore() {
  if (!confirm('Clear the entire vector store? You will need to re-ingest.')) return;
  try {
    const r = await fetch('/api/vectorstore', { method: 'DELETE' });
    const data = await r.json();
    toast(data.message, 'success');
    refreshStatus();
  } catch(e) {
    toast('Failed to clear', 'error');
  }
}

// ── Storage ───────────────────────────────────────────
function toggleStorageFields() {
  const type = document.getElementById('storage-type').value;
  document.querySelectorAll('.storage-fields').forEach(el => el.classList.remove('active'));
  const fields = document.getElementById(`fields-${type}`);
  if (fields) fields.classList.add('active');
}

async function loadStorage() {
  try {
    const r = await fetch('/api/storage');
    const data = await r.json();
    const tbody = document.getElementById('storage-tbody');
    if (!data.sources || data.sources.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" style="color:var(--text3);text-align:center;padding:20px;">No storage sources configured</td></tr>';
      return;
    }
    tbody.innerHTML = data.sources.map((s, i) => `
      <tr>
        <td><span class="type-badge type-${s.type}">${s.type.toUpperCase()}</span></td>
        <td>${s.label}</td>
        <td style="font-family:monospace;font-size:12px;">${s.path}</td>
        <td>${s.role}</td>
        <td><button class="btn btn-danger btn-sm" onclick="removeStorage(${i})">Remove</button></td>
      </tr>
    `).join('');
  } catch(e) {
    console.error('Failed to load storage', e);
  }
}

async function addStorage() {
  const type = document.getElementById('storage-type').value;
  const label = document.getElementById('storage-label').value.trim();
  if (!label) { toast('Label is required', 'error'); return; }

  const body = { type, label };

  if (type === 'local') {
    body.path = document.getElementById('storage-local-path').value.trim();
    if (!body.path) { toast('Path is required', 'error'); return; }
  } else if (type === 'nfs') {
    body.server_ip = document.getElementById('storage-nfs-ip').value.trim();
    body.export_path = document.getElementById('storage-nfs-export').value.trim();
    body.mount_point = document.getElementById('storage-nfs-mount').value.trim();
    body.nfs_version = document.getElementById('storage-nfs-version').value;
    if (!body.server_ip || !body.export_path || !body.mount_point) {
      toast('All NFS fields are required', 'error'); return;
    }
  } else if (type === 'smb') {
    body.server_ip = document.getElementById('storage-smb-ip').value.trim();
    body.share_name = document.getElementById('storage-smb-share').value.trim();
    body.username = document.getElementById('storage-smb-user').value.trim() || 'guest';
    body.password = document.getElementById('storage-smb-pass').value;
    body.domain = document.getElementById('storage-smb-domain').value.trim();
    body.mount_point = document.getElementById('storage-smb-mount').value.trim();
    if (!body.server_ip || !body.share_name || !body.mount_point) {
      toast('Server IP, Share Name, and Mount Point are required', 'error'); return;
    }
  } else if (type === 's3') {
    body.bucket = document.getElementById('storage-s3-bucket').value.trim();
    body.region = document.getElementById('storage-s3-region').value.trim();
    body.access_key = document.getElementById('storage-s3-key').value.trim();
    body.secret_key = document.getElementById('storage-s3-secret').value;
    body.mount_point = document.getElementById('storage-s3-mount').value.trim();
    if (!body.bucket || !body.mount_point) {
      toast('Bucket and Mount Point are required', 'error'); return;
    }
  }

  const resultEl = document.getElementById('storage-result');
  resultEl.textContent = 'Adding storage source...';
  resultEl.style.color = 'var(--text2)';

  try {
    const r = await fetch('/api/storage', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const data = await r.json();
    if (!r.ok) {
      resultEl.textContent = 'Error: ' + (data.detail || 'Failed');
      resultEl.style.color = 'var(--red)';
      toast('Failed to add storage', 'error');
    } else {
      resultEl.textContent = 'Storage source added successfully!';
      resultEl.style.color = 'var(--green)';
      toast('Storage added!', 'success');
      loadStorage();
      refreshStatus();
    }
  } catch(e) {
    resultEl.textContent = 'Error: ' + e.message;
    resultEl.style.color = 'var(--red)';
  }
}

async function testStorage() {
  const type = document.getElementById('storage-type').value;
  const body = { type, label: 'test' };

  if (type === 'local') {
    body.path = document.getElementById('storage-local-path').value.trim();
  } else if (type === 'nfs') {
    body.server_ip = document.getElementById('storage-nfs-ip').value.trim();
  } else if (type === 'smb') {
    body.server_ip = document.getElementById('storage-smb-ip').value.trim();
  }

  const resultEl = document.getElementById('storage-result');
  resultEl.textContent = 'Testing...';
  resultEl.style.color = 'var(--text2)';

  try {
    const r = await fetch('/api/storage/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const data = await r.json();
    resultEl.textContent = data.message;
    resultEl.style.color = data.reachable ? 'var(--green)' : 'var(--red)';
  } catch(e) {
    resultEl.textContent = 'Test failed: ' + e.message;
    resultEl.style.color = 'var(--red)';
  }
}

async function removeStorage(index) {
  if (!confirm('Remove this storage source?')) return;
  try {
    await fetch(`/api/storage/${index}`, { method: 'DELETE' });
    toast('Storage source removed', 'success');
    loadStorage();
    refreshStatus();
  } catch(e) {
    toast('Failed to remove', 'error');
  }
}

// ── Skills ────────────────────────────────────────────
let _allSkills = [];

async function loadSkills() {
  try {
    const r = await fetch('/api/skills');
    const data = await r.json();
    const skills = data.skills || [];
    _allSkills = skills;

    // Populate skill-select dropdown (used by mode toggle)
    const sel = document.getElementById('skill-select');
    if (sel) {
      const current = sel.value;
      sel.innerHTML = '';
      skills.forEach(s => {
        const opt = document.createElement('option');
        opt.value = s.name;
        opt.textContent = s.name;
        opt.title = s.description || '';
        sel.appendChild(opt);
      });
      if (current && skills.some(s => s.name === current)) {
        sel.value = current;
      }
    }

    // Populate sidebar skills
    const sidebar = document.getElementById('sidebar-skills');
    if (sidebar) {
      if (skills.length === 0) {
        sidebar.innerHTML = '<div style="color:var(--text3); font-size:12px;">No skills yet</div>';
      } else {
        sidebar.innerHTML = skills.map(s => `
          <div class="sidebar-skill-item${selectedSkill === s.name ? ' active' : ''}"
               onclick="selectSkillFromSidebar('${s.name.replace(/'/g, "\\'")}')"
               title="${(s.description || '').replace(/"/g, '&quot;')}">
            <span class="skill-icon">${s.source === 'user' ? '&#9998;' : '&#9881;'}</span>
            <span class="skill-name">${s.name}</span>
          </div>
        `).join('');
      }
    }

    // Populate skills management page
    const list = document.getElementById('skills-list');
    if (list) {
      if (skills.length === 0) {
        list.innerHTML = '<p style="color:var(--text3);">No skills yet. Create or upload one to get started.</p>';
      } else {
        list.innerHTML = skills.map(s => `
          <div class="skills-list-item">
            <div style="flex:1; min-width:0;">
              <h4>${s.name} <span class="skill-source-badge ${s.source || 'builtin'}">${s.source === 'user' ? 'Custom' : 'Built-in'}</span></h4>
              <p>${s.description || 'No description'}</p>
              <div class="skill-tags">
                ${(s.tags || []).map(t => `<span class="skill-tag">${t}</span>`).join('')}
              </div>
            </div>
            <div class="skill-actions">
              <button class="btn btn-ghost btn-sm" onclick="viewSkillMarkdown('${s.name.replace(/'/g, "\\'")}')">View</button>
              <button class="btn btn-danger btn-sm" onclick="deleteSkill('${s.name.replace(/'/g, "\\'")}')">Delete</button>
            </div>
          </div>
        `).join('');
      }
    }
  } catch(e) {
    console.error('Failed to load skills', e);
  }
}

function selectSkillFromSidebar(name) {
  if (selectedSkill === name) {
    selectedSkill = null;
  } else {
    selectedSkill = name;
    setChatMode('skill');
  }
  updateSkillUI();
  switchTab('chat');
}

async function deleteSkill(name) {
  if (!confirm(`Delete skill "${name}"?`)) return;
  try {
    const r = await fetch(`/api/skills/${encodeURIComponent(name)}`, { method: 'DELETE' });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Delete failed');
    toast(`Skill "${name}" deleted`, 'success');
    loadSkills();
  } catch(e) {
    toast(e.message, 'error');
  }
}

let chatMode = 'chat';

function onSkillChange() {
  const sel = document.getElementById('skill-select');
  if (sel && sel.value) {
    selectedSkill = sel.value;
  }
  updateSkillUI();
}

function setChatMode(mode) {
  chatMode = mode;
  const chatBtn = document.getElementById('mode-chat');
  const skillBtn = document.getElementById('mode-skill');
  const picker = document.getElementById('skill-picker-wrap');
  const input = document.getElementById('chat-input');

  if (chatBtn) chatBtn.classList.toggle('active', mode === 'chat');
  if (skillBtn) skillBtn.classList.toggle('active', mode === 'skill');
  if (picker) picker.style.display = mode === 'skill' ? 'flex' : 'none';

  if (mode === 'chat') {
    selectedSkill = null;
    if (input) input.placeholder = 'Ask a question about your documents...';
  } else {
    if (!selectedSkill && _allSkills.length > 0) {
      selectedSkill = _allSkills[0].name;
      const sel = document.getElementById('skill-select');
      if (sel) sel.value = selectedSkill;
    }
    if (input && selectedSkill) input.placeholder = 'Describe what you want to generate with "' + selectedSkill + '"...';
  }
  updateSkillUI();
}

function updateSkillUI() {
  const input = document.getElementById('chat-input');
  const sel = document.getElementById('skill-select');

  if (chatMode === 'skill' && selectedSkill) {
    if (sel) sel.value = selectedSkill;
    if (input) input.placeholder = 'Describe what you want to generate with "' + selectedSkill + '"...';
  } else if (chatMode === 'chat') {
    selectedSkill = null;
    if (input) input.placeholder = 'Ask a question about your documents...';
  }

  // Update sidebar active state
  document.querySelectorAll('.sidebar-skill-item').forEach(el => {
    const name = el.querySelector('.skill-name');
    if (name) el.classList.toggle('active', name.textContent === selectedSkill);
  });
}

// ── Welcome (dynamic) ────────────────────────────────
async function loadWelcome() {
  try {
    const r = await fetch('/api/corpus/summary');
    const data = await r.json();

    const statsBar = document.getElementById('welcome-stats-bar');
    if (!data.has_docs) {
      if (statsBar) statsBar.innerHTML = '';
      return;
    }

    const titleEl = document.getElementById('welcome-title');
    const descEl = document.getElementById('welcome-desc');

    titleEl.textContent = 'Your Knowledge Base is Ready';
    descEl.textContent = 'Ask questions, generate documents, or explore insights from your ' + data.doc_count + ' indexed file' + (data.doc_count !== 1 ? 's' : '') + '.';

    const skillCount = (data.skills || []).length;
    if (statsBar) {
      statsBar.innerHTML =
        '<span><span class="ws-num">' + data.doc_count + '</span> document' + (data.doc_count !== 1 ? 's' : '') + ' indexed</span>' +
        '<span><span class="ws-num">' + skillCount + '</span> skill' + (skillCount !== 1 ? 's' : '') + ' ready</span>' +
        '<span><span class="ws-num">' + (data.chunk_count || 0) + '</span> searchable chunks</span>';
    }
  } catch(e) {
    console.error('Failed to load welcome', e);
  }
}

// ── Export ────────────────────────────────────────────
function exportDoc(recordId, format) {
  window.open(`/api/export/${recordId}/${format}`, '_blank');
}

// ── History ──────────────────────────────────────────
async function loadHistory() {
  try {
    const r = await fetch('/api/history/generation?limit=15');
    const data = await r.json();
    const list = document.getElementById('history-list');

    if (!data || data.length === 0) {
      list.innerHTML = '<div style="padding:6px 16px; font-size:11px; color:var(--text3);">No generations yet</div>';
      return;
    }

    list.innerHTML = data.map(h => {
      const date = new Date(h.created_at);
      const dateStr = date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
      const preview = h.input_text.slice(0, 40) + (h.input_text.length > 40 ? '...' : '');
      return `<div class="history-item" onclick="loadGeneration(${h.id})" title="${h.input_text.replace(/"/g, '&quot;')}">
        <span class="hist-skill">${h.skill_name}</span>
        <span>${preview}</span>
        <span class="hist-date">${dateStr}</span>
      </div>`;
    }).join('');
  } catch(e) {
    console.error('Failed to load history', e);
  }
}

async function loadGeneration(id) {
  try {
    const r = await fetch(`/api/history/generation/${id}`);
    const data = await r.json();

    switchTab('chat');

    const welcome = document.getElementById('welcome');
    if (welcome) welcome.style.display = 'none';
    document.getElementById('panel-chat').classList.remove('welcome-active');

    renderMessage('user', data.input_text);
    const assistantId = 'msg-' + Date.now();
    renderMessage('assistant', '', assistantId);

    const bubble = document.querySelector(`#${assistantId} .msg-bubble`);
    if (bubble) bubble.innerHTML = mdToHtml(data.output_content);

    const meta = document.getElementById(assistantId + '-meta');
    if (meta) {
      const date = new Date(data.created_at);
      meta.innerHTML = `${data.skill_name} · ${date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}` +
        ` <span class="export-btns">` +
        `<button class="btn-export" onclick="exportDoc(${data.id}, 'pdf')" title="Download PDF">PDF</button>` +
        `<button class="btn-export" onclick="exportDoc(${data.id}, 'docx')" title="Download Word">Word</button>` +
        `</span>`;
    }
  } catch(e) {
    toast('Failed to load generation', 'error');
  }
}

// ── Logo ─────────────────────────────────────────────
async function uploadLogo(event) {
  const file = event.target.files[0];
  if (!file) return;

  const formData = new FormData();
  formData.append('logo', file);

  try {
    const r = await fetch('/api/settings/logo', {
      method: 'POST',
      body: formData,
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Upload failed');
    toast('Logo uploaded!', 'success');
    loadLogoPreview();
  } catch(e) {
    toast('Logo upload failed: ' + e.message, 'error');
  }
}

async function loadLogoPreview() {
  const img = document.getElementById('logo-preview');
  const status = document.getElementById('logo-status');
  try {
    const r = await fetch('/api/settings/logo');
    if (r.ok) {
      const blob = await r.blob();
      img.src = URL.createObjectURL(blob);
      img.style.display = 'inline';
      status.textContent = 'Logo set';
      status.style.color = 'var(--green)';
    } else {
      img.style.display = 'none';
      status.textContent = 'No logo';
    }
  } catch(e) {
    img.style.display = 'none';
    status.textContent = 'No logo';
  }
}

function showUploadSkillModal() {
  document.getElementById('upload-modal').classList.add('open');
  document.getElementById('upload-file-picker').value = '';
  document.getElementById('upload-filename').value = '';
  document.getElementById('upload-content').value = '';
}

function closeUploadModal() {
  document.getElementById('upload-modal').classList.remove('open');
}

function handleSkillFileSelect(event) {
  const file = event.target.files[0];
  if (!file) return;

  document.getElementById('upload-filename').value = file.name;

  const reader = new FileReader();
  reader.onload = function(e) {
    document.getElementById('upload-content').value = e.target.result;
  };
  reader.onerror = function() {
    toast('Failed to read file', 'error');
  };
  reader.readAsText(file);
}

async function uploadSkill() {
  const filename = document.getElementById('upload-filename').value.trim();
  const content = document.getElementById('upload-content').value;
  if (!filename || !content) { toast('Filename and content required', 'error'); return; }

  try {
    const r = await fetch('/api/skills/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename, content })
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Upload failed');
    toast('Skill uploaded! Select it from the Chat dropdown to use it.', 'success');
    closeUploadModal();
    loadSkills();
    switchTab('chat');
  } catch(e) {
    toast(e.message, 'error');
  }
}

// ── Settings ──────────────────────────────────────────
async function loadSettings() {
  try {
    const r = await fetch('/api/settings');
    const data = await r.json();
    const cfg = data.settings || {};

    document.getElementById('set-org-name').value = cfg.org_name || '';
    document.getElementById('set-ollama-host').value = cfg.ollama_host || '';
    document.getElementById('set-embed-model').value = cfg.embed_model || '';
    document.getElementById('set-gemini-key').value = cfg.gemini_api_key || '';
    document.getElementById('set-groq-key').value = cfg.groq_api_key || '';
    document.getElementById('set-openai-key').value = cfg.openai_api_key || '';
    document.getElementById('set-anthropic-key').value = cfg.anthropic_api_key || '';
    document.getElementById('set-together-key').value = cfg.together_api_key || '';
    document.getElementById('set-cerebras-key').value = cfg.cerebras_api_key || '';
    document.getElementById('set-openrouter-key').value = cfg.openrouter_api_key || '';
    document.getElementById('set-fireworks-key').value = cfg.fireworks_api_key || '';
    document.getElementById('set-google-client-id').value = cfg.google_client_id || '';
    loadLogoPreview();
  } catch(e) {
    console.error('Failed to load settings', e);
  }
}

async function saveSettings() {
  const settings = {};
  const fields = [
    ['set-org-name', 'org_name'],
    ['set-ollama-host', 'ollama_host'],
    ['set-embed-model', 'embed_model'],
    ['set-gemini-key', 'gemini_api_key'],
    ['set-groq-key', 'groq_api_key'],
    ['set-openai-key', 'openai_api_key'],
    ['set-anthropic-key', 'anthropic_api_key'],
    ['set-together-key', 'together_api_key'],
    ['set-cerebras-key', 'cerebras_api_key'],
    ['set-openrouter-key', 'openrouter_api_key'],
    ['set-fireworks-key', 'fireworks_api_key'],
    ['set-google-client-id', 'google_client_id'],
  ];

  fields.forEach(([elId, key]) => {
    const val = document.getElementById(elId).value.trim();
    if (val && !val.startsWith('***')) settings[key] = val;
  });

  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings })
    });
    const data = await r.json();
    if (data.success) {
      toast('Settings saved!', 'success');
      loadModels();
    } else {
      toast('Failed to save settings', 'error');
    }
  } catch(e) {
    toast('Request failed: ' + e.message, 'error');
  }
}

async function applyModel() {
  const model = document.getElementById('settings-model-select').value;
  if (!model) { toast('Select a model first', 'error'); return; }

  const modelName = model.includes(':') ? model.split(':', 2)[1] : model;
  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings: { llm_model: modelName } })
    });
    const data = await r.json();
    if (data.success) {
      currentModel = model;
      document.getElementById('model-select').value = model;
      toast(`Model set to ${model}`, 'success');
    }
  } catch(e) { toast('Failed: ' + e.message, 'error'); }
}

// ── Model Pull ────────────────────────────────────────
async function pullModel(type) {
  const inputId = type === 'embed' ? 'pull-embed-name' : 'pull-llm-name';
  const btnId = type === 'embed' ? 'pull-embed-btn' : 'pull-llm-btn';
  const logId = type === 'embed' ? 'pull-embed-log' : 'pull-llm-log';

  const modelName = document.getElementById(inputId).value.trim();
  if (!modelName) { toast('Enter a model name', 'error'); return; }

  const btn = document.getElementById(btnId);
  const log = document.getElementById(logId);
  btn.disabled = true; btn.textContent = 'Pulling...';
  log.style.display = 'block';
  log.textContent = `Pulling ${modelName}...\n`;

  try {
    const r = await fetch('/api/pull', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: modelName })
    });
    if (!r.ok) throw new Error(`Server error ${r.status}`);
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      log.textContent += decoder.decode(value, { stream: true });
      log.scrollTop = log.scrollHeight;
    }
    log.textContent += '\nDone!';
    toast(`${modelName} pulled successfully`, 'success');
    loadModels();

    if (type === 'embed') {
      document.getElementById('set-embed-model').value = modelName;
    }
  } catch(e) {
    log.textContent += '\nError: ' + e.message;
    toast('Pull failed', 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Pull';
    log.scrollTop = log.scrollHeight;
  }
}

// ── Tabs ──────────────────────────────────────────────
function switchTab(tab) {
  const tabs = ['chat', 'skills', 'ingest', 'storage', 'settings'];
  tabs.forEach(t => {
    const panel = document.getElementById(`panel-${t}`);
    const btn = document.getElementById(`tab-${t}`);
    if (panel) { panel.style.display = t === tab ? 'flex' : 'none'; panel.classList.toggle('active', t === tab); }
    if (btn) btn.classList.toggle('active', t === tab);
  });

  if (tab === 'skills') loadSkills();
  if (tab === 'storage') loadStorage();
  if (tab === 'settings') { loadSettings(); loadModels(); }
  if (tab === 'ingest') refreshStatus();
}

// ── Source Modal ──────────────────────────────────────
function showSourceModal(source) {
  document.getElementById('modal-title').textContent = source.filename + (source.page ? ` — Page ${source.page}` : '');
  document.getElementById('modal-body').textContent = source.preview;
  document.getElementById('source-modal').classList.add('open');
}

function closeModal() {
  document.getElementById('source-modal').classList.remove('open');
}

document.getElementById('source-modal').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeModal();
});
document.getElementById('upload-modal').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeUploadModal();
});

// ── Trial Status & Free Guide ─────────────────────────
async function checkTrialStatus() {
  try {
    const r = await fetch('/api/trial');
    const data = await r.json();
    if (data.exhausted) {
      showFreeGuide();
    }
  } catch(e) {
    // Silently ignore — trial check is non-critical
  }
}

let _guideTimer = null;
let _guideCountdown = 30;

function showFreeGuide() {
  const guide = document.getElementById('free-guide');
  if (guide && !sessionStorage.getItem('guide-dismissed')) {
    guide.classList.add('show');
    _guideCountdown = 30;
    const cdEl = document.getElementById('guide-countdown');
    if (cdEl) cdEl.textContent = '(' + _guideCountdown + 's)';
    if (_guideTimer) clearInterval(_guideTimer);
    _guideTimer = setInterval(() => {
      _guideCountdown--;
      if (cdEl) cdEl.textContent = '(' + _guideCountdown + 's)';
      if (_guideCountdown <= 0) {
        dismissGuide();
      }
    }, 1000);
  }
}

function dismissGuide() {
  if (_guideTimer) { clearInterval(_guideTimer); _guideTimer = null; }
  const guide = document.getElementById('free-guide');
  if (guide) {
    guide.style.transition = 'opacity 0.4s ease';
    guide.style.opacity = '0';
    setTimeout(() => {
      guide.classList.remove('show');
      guide.style.opacity = '';
      guide.style.transition = '';
    }, 400);
  }
  sessionStorage.setItem('guide-dismissed', '1');
}

function downloadGuideAsPDF() {
  const content = [
    'ZettaBrain Lite - Setup Guide',
    '=============================',
    '',
    'Your free trial has ended. Here are free ways to keep going:',
    '',
    '1. GROQ (Fastest Free Option - 30 sec setup)',
    '   - Go to console.groq.com and sign up',
    '   - Click "API Keys" and create one',
    '   - Paste it in Settings > Cloud Providers > Groq API Key',
    '   - Select a Groq model from the dropdown',
    '',
    '2. GOOGLE GEMINI (Free Tier - 30 sec setup)',
    '   - Go to aistudio.google.com/apikey',
    '   - Click "Create API Key"',
    '   - Paste it in Settings > Cloud Providers > Gemini API Key',
    '',
    '3. LOCAL MODEL (Fully Offline, Unlimited - 5 min setup)',
    '   - Go to Settings > Pull LLM Model',
    '   - Type "phi4-mini" (lightweight) and click Pull',
    '   - For better quality with a GPU: "llama3.1:8b"',
    '',
    'Generated by ZettaBrain Lite',
  ].join('\n');

  const blob = new Blob([content], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'zettabrain-setup-guide.txt';
  a.click();
  URL.revokeObjectURL(a.href);
  toast('Setup guide downloaded', 'success');
}

// ── Skill Wizard State ───────────────────────────────
let wizStep = 1;
let wizSelectedTones = [];
let wizDraftQuality = null;

// ── Skill Markdown Viewer ─────────────────────────────
let _viewSkillName = '';
let _viewSkillFilename = '';

async function viewSkillMarkdown(name) {
  try {
    const r = await fetch(`/api/skills/${encodeURIComponent(name)}/content`);
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Failed to load skill');
    _viewSkillName = name;
    _viewSkillFilename = data.filename || (name + '.md');
    document.getElementById('skill-view-title').textContent = data.filename || name;
    document.getElementById('skill-view-content').value = data.content;
    document.getElementById('skill-view-modal').classList.add('open');
  } catch(e) {
    toast(e.message, 'error');
  }
}

function closeSkillView() {
  document.getElementById('skill-view-modal').classList.remove('open');
}

function copySkillContent() {
  const content = document.getElementById('skill-view-content').value;
  navigator.clipboard.writeText(content).then(() => toast('Copied to clipboard', 'success'));
}

function downloadSkillFile() {
  const content = document.getElementById('skill-view-content').value;
  const blob = new Blob([content], { type: 'text/markdown' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = _viewSkillFilename;
  a.click();
  URL.revokeObjectURL(a.href);
}

document.getElementById('skill-view-modal').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeSkillView();
});

// ── Skill Creation Wizard ────────────────────────────
const SKILL_TEMPLATES = {
  proposal: {
    name: 'Client Proposal',
    desc: 'Generate a professional client proposal that persuades the reader with our approach, team, timeline, and past work — without pricing details.',
    sections: ['Executive Summary', 'Understanding of Requirements', 'Proposed Approach', 'Timeline & Milestones', 'Team & Credentials', 'Why Us', 'Next Steps'],
    corpus: true, citations: true, tones: ['Professional', 'Persuasive'],
  },
  quote: {
    name: 'Quote',
    desc: 'Generate a pricing quote with line-item breakdown, rates, totals, validity period, and payment terms based on our rate cards and pricing documents.',
    sections: ['Quote Summary', 'Line Items & Pricing', 'Subtotals by Phase', 'Discounts & Adjustments', 'Total Investment', 'Payment Terms', 'Validity & Conditions'],
    corpus: true, citations: true, tones: ['Professional', 'Concise'],
  },
  contract: {
    name: 'Contract',
    desc: 'Generate a contract or agreement covering scope, deliverables, obligations, liability, payment, termination, and signatures based on our standard terms.',
    sections: ['Parties', 'Scope of Work', 'Deliverables & Acceptance', 'Timeline', 'Fees & Payment', 'Intellectual Property', 'Confidentiality', 'Liability & Indemnification', 'Termination', 'General Provisions', 'Signatures'],
    corpus: true, citations: true, tones: ['Formal', 'Concise'],
  },
  report: {
    name: 'Status Report',
    desc: 'Generate a status report covering progress, issues, risks, and next steps for a project or team.',
    sections: ['Summary', 'Progress This Period', 'Key Issues & Risks', 'Metrics', 'Next Steps'],
    corpus: false, citations: false, tones: ['Professional', 'Concise'],
  },
  email: {
    name: 'Professional Email',
    desc: 'Draft a professional email based on the topic and context provided.',
    sections: ['Subject Line', 'Email Body'],
    corpus: false, citations: false, tones: ['Professional', 'Friendly'],
  },
  sop: {
    name: 'Standard Operating Procedure',
    desc: 'Generate a step-by-step procedure document with prerequisites, steps, troubleshooting, and revision history.',
    sections: ['Purpose', 'Scope', 'Prerequisites', 'Procedure Steps', 'Troubleshooting', 'Revision History'],
    corpus: true, citations: false, tones: ['Technical', 'Concise'],
  },
  summary: {
    name: 'Executive Summary',
    desc: 'Generate a concise executive summary of the provided information, highlighting key decisions and action items.',
    sections: ['Overview', 'Key Points', 'Decisions', 'Action Items'],
    corpus: true, citations: true, tones: ['Professional', 'Concise'],
  },
  custom: {
    name: '', desc: '', sections: [],
    corpus: false, citations: false, tones: ['Professional'],
  },
};

let wizSelectedTemplate = '';

function selectTemplate(el, key) {
  document.querySelectorAll('.template-card').forEach(c => c.classList.remove('selected'));
  el.classList.add('selected');
  wizSelectedTemplate = key;

  const tmpl = SKILL_TEMPLATES[key];
  if (key !== 'custom') {
    document.getElementById('wiz-name').value = tmpl.name;
    document.getElementById('wiz-desc').value = tmpl.desc;
    document.getElementById('wiz-corpus').checked = !!tmpl.corpus;
    document.getElementById('wiz-citations').checked = !!tmpl.citations;
    document.querySelectorAll('.tone-chip').forEach(c => c.classList.remove('selected'));
    (tmpl.tones || ['Professional']).forEach(tone => {
      document.querySelectorAll('.tone-chip').forEach(c => {
        if (c.textContent.trim() === tone) c.classList.add('selected');
      });
    });
    wizSelectedTones = tmpl.tones || ['Professional'];
  }
}

let wizDocuments = [];
let wizSelectedDoc = null;
let wizUploadedFileContent = '';

function openSkillWizard() {
  wizStep = 1;
  wizSelectedTones = ['Professional'];
  wizSelectedTemplate = '';
  wizDraftQuality = null;
  wizSelectedDoc = null;
  wizUploadedFileContent = '';

  document.getElementById('wiz-name').value = '';
  document.getElementById('wiz-desc').value = '';
  document.getElementById('wiz-corpus').checked = false;
  document.getElementById('wiz-maxlen').value = '2000';
  document.getElementById('wiz-citations').checked = false;
  document.getElementById('wiz-example').value = '';
  document.getElementById('wiz-preview').value = '';
  document.getElementById('example-upload-filename').textContent = '';

  document.querySelectorAll('.tone-chip').forEach(c => c.classList.remove('selected'));
  document.querySelectorAll('.template-card').forEach(c => c.classList.remove('selected'));
  const toneChips = document.querySelectorAll('.tone-chip');
  if (toneChips.length > 0) toneChips[0].classList.add('selected');

  const banner = document.getElementById('wiz-quality-banner');
  banner.style.display = 'none';
  banner.className = 'wiz-quality-banner';

  switchExampleTab('picker');
  loadDocPickerList();
  updateWizSteps();
  document.getElementById('wizard-modal').classList.add('open');
}

function closeSkillWizard() {
  document.getElementById('wizard-modal').classList.remove('open');
}

const TONE_CONFLICTS = {
  'Formal': 'Friendly',
  'Friendly': 'Formal',
  'Technical': 'Concise',
  'Concise': 'Technical',
};

function selectTone(el) {
  const tone = el.textContent.trim();
  const selected = el.classList.contains('selected');

  if (!selected) {
    const currentCount = document.querySelectorAll('.tone-chip.selected').length;
    if (currentCount >= 3) {
      toast('Pick up to 3 tones', 'info');
      return;
    }
    const conflict = TONE_CONFLICTS[tone];
    if (conflict) {
      const chips = document.querySelectorAll('.tone-chip');
      chips.forEach(c => {
        if (c.textContent.trim() === conflict && c.classList.contains('selected')) {
          c.classList.remove('selected');
          toast(`Removed "${conflict}" — it conflicts with "${tone}"`, 'info');
        }
      });
    }
  }

  el.classList.toggle('selected');
  wizSelectedTones = Array.from(document.querySelectorAll('.tone-chip.selected')).map(c => c.textContent.trim());
}

function updateWizSteps() {
  for (let i = 1; i <= 3; i++) {
    const stepEl = document.getElementById(`wiz-step-${i}`);
    const pageEl = document.getElementById(`wiz-page-${i}`);
    stepEl.className = 'wizard-step' + (i === wizStep ? ' active' : '') + (i < wizStep ? ' done' : '');
    pageEl.className = 'wizard-page' + (i === wizStep ? ' active' : '');
  }
  document.getElementById('wiz-prev').style.visibility = wizStep === 1 ? 'hidden' : 'visible';
  const nextBtn = document.getElementById('wiz-next');
  nextBtn.textContent = wizStep === 3 ? 'Save Skill' : 'Next';
}

async function wizNext() {
  const btn = document.getElementById('wiz-next');

  if (wizStep === 1) {
    const name = document.getElementById('wiz-name').value.trim();
    const desc = document.getElementById('wiz-desc').value.trim();
    if (!name) { toast('Give your skill a name', 'error'); return; }
    if (!desc) { toast('Describe what the skill should generate', 'error'); return; }
  }

  if (wizStep === 2) {
    const name = document.getElementById('wiz-name').value.trim();
    const desc = document.getElementById('wiz-desc').value.trim();
    const corpus = document.getElementById('wiz-corpus').checked;
    const maxTokens = document.getElementById('wiz-maxlen').value;
    const citations = document.getElementById('wiz-citations').checked;
    const tones = wizSelectedTones.length > 0 ? wizSelectedTones : ['Professional'];
    const tmpl = SKILL_TEMPLATES[wizSelectedTemplate] || SKILL_TEMPLATES.custom;
    const sections = tmpl.sections.length > 0 ? tmpl.sections : [];

    btn.disabled = true;
    btn.textContent = 'Generating skill...';
    document.getElementById('wiz-prev').disabled = true;

    let example = await getWizardExampleContent();

    try {
      const r = await fetch('/api/skills/draft', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          goal: desc,
          name: name,
          sections: sections,
          tone: tones,
          requires_corpus: corpus,
          citations: citations,
          max_tokens: parseInt(maxTokens),
          example_output: example,
          model: currentModel || undefined,
        })
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || 'Draft failed');

      document.getElementById('wiz-preview').value = data.content;
      wizDraftQuality = data.quality;
      showWizQuality(data.quality, data.rules_found || 0);
    } catch (e) {
      toast('AI draft failed, using local builder: ' + e.message, 'info');
      document.getElementById('wiz-preview').value = buildSkillMarkdown();
      wizDraftQuality = null;
      const banner = document.getElementById('wiz-quality-banner');
      banner.style.display = 'none';
    } finally {
      btn.disabled = false;
      btn.textContent = 'Next';
      document.getElementById('wiz-prev').disabled = false;
    }
  }

  if (wizStep === 3) {
    saveWizardSkill();
    return;
  }
  wizStep++;
  updateWizSteps();
}

function showWizQuality(quality, rulesFound) {
  const banner = document.getElementById('wiz-quality-banner');
  if (!quality) { banner.style.display = 'none'; return; }

  const passed = quality.passed;
  banner.className = 'wiz-quality-banner ' + (passed ? 'pass' : 'warn');

  let html = `<span class="score">Score: ${quality.score}/100</span>`;
  if (rulesFound > 0) html += ` &middot; ${rulesFound} rule${rulesFound !== 1 ? 's' : ''} extracted from your documents`;

  const issues = [...(quality.errors || []), ...(quality.warnings || [])].slice(0, 3);
  if (issues.length > 0) {
    html += '<div class="issues">' + issues.map(i => '&bull; ' + i).join('<br>') + '</div>';
  }

  banner.innerHTML = html;
  banner.style.display = 'block';
}

function wizPrev() {
  if (wizStep > 1) {
    wizStep--;
    updateWizSteps();
  }
}

const SECTION_GUIDANCE = {
  'executive summary': 'Open with the client\'s problem in their own numbers and their deadline, not with who we are. State the proposed approach in three sentences and give the total figure here rather than deferring it to the pricing table. One page maximum.',
  'understanding of requirements': 'Restate the client\'s requirements in your own words, demonstrating that you have read and understood their brief. Reference specific constraints, deadlines, or success criteria they mentioned. Do not add requirements they did not ask for.',
  'proposed approach': 'Describe the methodology, phases, and key activities. Each phase must have a deliverable and an estimated duration. Reference similar past engagements if the corpus provides them.',
  'timeline & milestones': 'A table or phased timeline with start and end dates for each phase. Flag dependencies between phases. Include buffer periods for review cycles.',
  'team': 'Name the lead and key team members with their role, relevant experience, and one case-study reference each where available.',
  'investment': 'A pricing table with role, rate, estimated hours, and line total. Show subtotals per phase and a grand total. If a discount applies, show pre-discount and post-discount on separate rows.',
  'terms & conditions': 'Pull standard terms from the corpus. Include payment terms, validity period, IP ownership, and termination provisions. Cite the source document for each clause.',
  'summary': 'Three to five sentences covering the overall status, the single biggest achievement, and the single biggest risk. No filler phrasing.',
  'progress this period': 'A table: Task, Owner, Status, % Complete, Notes. Every row must cite a source. Do not estimate percentages without tracker data.',
  'key issues & risks': 'Numbered list. Each entry: what the issue or risk is, who owns resolution, target date, and likelihood/impact assessment.',
  'metrics': 'A table of KPIs with target, actual, and variance. Flag any metric that is more than 10% off target.',
  'next steps': 'Bulleted list of planned actions. Each must have an owner and a target date. Do not list vague intentions.',
  'subject line': 'A clear, specific subject line under 60 characters. Include the key action or topic, not a generic greeting.',
  'email body': 'Open with context (one sentence of why you are writing), then the ask or information, then the next step. Three paragraphs maximum.',
  'purpose': 'One paragraph stating what this procedure accomplishes and when it should be used. Include the triggering event or condition.',
  'scope': 'Who this procedure applies to, what systems or processes it covers, and any explicit exclusions.',
  'prerequisites': 'A numbered checklist of what must be in place before starting. Include access permissions, tools, and approvals.',
  'procedure steps': 'Numbered steps with one action per step. Each step must be independently verifiable — the person can confirm they did it correctly before moving on.',
  'troubleshooting': 'A table: Symptom, Likely Cause, Resolution. Cover the three most common failure modes for this procedure.',
  'revision history': 'A table: Date, Version, Author, Change Summary. Include the current version.',
  'overview': 'Summarize the key points in three to five sentences. State the conclusion or recommendation first, then the supporting context.',
  'key points': 'Bulleted list of the most important findings or decisions. Each bullet must be a complete, self-contained statement.',
  'decisions': 'A table: Decision, Made By, Date, Rationale. Include only decisions that were actually made, not pending ones.',
  'action items': 'A table: Action, Owner, Due Date, Status. Every action must have a named owner and a specific date.',
  'details': 'Present the core content organized by topic. Use subheadings if the section exceeds one page. Include specific data, figures, or references rather than general statements.',
  'conclusion': 'Restate the key recommendation or finding. List the one to three most important next steps. Do not introduce new information here.',
  'quote summary': 'State the client name, project name, quote date, validity period, and the total quoted amount upfront. One paragraph maximum.',
  'line items & pricing': 'A table with: Item/Service, Unit, Quantity, Unit Rate, Line Total. Every rate must come from the rate card or corpus. Do not invent rates.',
  'subtotals by phase': 'Group line items by project phase or workstream. Show subtotal per phase. Use a table format.',
  'discounts & adjustments': 'List any volume discounts, early-payment discounts, or adjustments. Show the discount percentage and the dollar impact. If no discounts apply, state "No discounts applied."',
  'total investment': 'Show the grand total with and without tax. If tax treatment is unknown, state the assumption. Bold the final number.',
  'payment terms': 'State the payment schedule (e.g., 50% upfront, 50% on delivery), accepted methods, currency, and late-payment terms. Pull from standard terms if available.',
  'validity & conditions': 'State how long this quote is valid, any conditions that would change the pricing, and what happens after expiry.',
  'why us': 'Highlight differentiators — past results, certifications, team depth, unique methodology. Reference specific case studies or metrics from the corpus if available.',
  'parties': 'Full legal names, addresses, and registration numbers of all parties. Use [NEEDS INPUT] for any missing party details.',
  'scope of work': 'Define exactly what will be delivered, the standards it must meet, and any assumptions. Be specific enough that both parties can agree whether the scope was fulfilled.',
  'deliverables & acceptance': 'List each deliverable with acceptance criteria and the review period. State what happens if acceptance is not given within the review period.',
  'fees & payment': 'Total contract value, payment milestones tied to deliverables, invoicing procedures, and late-payment interest rate.',
  'intellectual property': 'State who owns the work product, any licenses granted, and when ownership transfers. Reference standard IP terms from the corpus.',
  'confidentiality': 'Define what constitutes confidential information, the obligations of each party, the duration of confidentiality, and permitted disclosures.',
  'liability & indemnification': 'Cap on liability, exclusions, indemnification obligations of each party. Pull from standard terms. Never invent liability caps.',
  'termination': 'Conditions for termination by either party, notice period, obligations on termination (payment for work done, return of materials).',
  'general provisions': 'Governing law, dispute resolution, force majeure, amendment process, severability, entire agreement clause.',
  'signatures': 'Signature blocks for each party with name, title, date, and signature line. Use [NEEDS INPUT] for names not provided.',
  'team & credentials': 'Name the lead and key team members. For each, include their role on this project, relevant experience, and one or two specific accomplishments. Reference case studies from the corpus.',
};

// ── Example Source: tabs, picker, upload ──────────────
function switchExampleTab(tab) {
  document.querySelectorAll('.example-source-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.example-source-panel').forEach(p => p.classList.remove('active'));
  const tabEl = document.querySelector(`.example-source-tab[onclick*="'${tab}'"]`);
  if (tabEl) tabEl.classList.add('active');
  const panel = document.getElementById('example-panel-' + tab);
  if (panel) panel.classList.add('active');
}

async function loadDocPickerList() {
  const list = document.getElementById('doc-picker-list');
  try {
    const r = await fetch('/api/documents');
    const data = await r.json();
    wizDocuments = data.documents || [];
  } catch (e) {
    wizDocuments = [];
  }
  renderDocPickerList(wizDocuments);
}

function renderDocPickerList(docs) {
  const list = document.getElementById('doc-picker-list');
  if (docs.length === 0) {
    list.innerHTML = '<div class="doc-picker-empty">No ingested documents found. Ingest files first, or use Upload or Paste instead.</div>';
    return;
  }
  list.innerHTML = docs.map((d, i) => `
    <div class="doc-picker-item${wizSelectedDoc && wizSelectedDoc.path === d.path ? ' selected' : ''}" onclick="selectDocForExample(${i})">
      <span class="doc-ext">${d.ext}</span>
      <span class="doc-name" title="${d.name}">${d.name}</span>
      <span class="doc-size">${d.size_kb > 0 ? d.size_kb + ' KB' : ''}</span>
    </div>
  `).join('');
}

function filterDocPicker() {
  const q = document.getElementById('doc-picker-search').value.toLowerCase();
  const filtered = q ? wizDocuments.filter(d => d.name.toLowerCase().includes(q)) : wizDocuments;
  renderDocPickerList(filtered);
}

function selectDocForExample(idx) {
  const q = document.getElementById('doc-picker-search').value.toLowerCase();
  const filtered = q ? wizDocuments.filter(d => d.name.toLowerCase().includes(q)) : wizDocuments;
  const doc = filtered[idx];
  if (!doc) return;

  if (wizSelectedDoc && wizSelectedDoc.path === doc.path) {
    wizSelectedDoc = null;
  } else {
    wizSelectedDoc = doc;
  }
  renderDocPickerList(filtered);
}

function handleExampleFileUpload(event) {
  const file = event.target.files[0];
  if (!file) return;
  const nameEl = document.getElementById('example-upload-filename');
  nameEl.textContent = file.name;

  const reader = new FileReader();
  reader.onload = function(e) {
    wizUploadedFileContent = e.target.result;
  };
  reader.readAsText(file);
}

async function getWizardExampleContent() {
  const activeTab = document.querySelector('.example-source-tab.active');
  const tab = activeTab ? activeTab.textContent.trim() : 'Paste Text';

  if (tab === 'From Library' && wizSelectedDoc) {
    try {
      const r = await fetch('/api/documents/content?path=' + encodeURIComponent(wizSelectedDoc.path));
      if (r.ok) {
        const data = await r.json();
        return data.content || '';
      }
    } catch (e) { /* fall through */ }
    return '';
  }

  if (tab === 'Upload File' && wizUploadedFileContent) {
    return wizUploadedFileContent;
  }

  return document.getElementById('wiz-example').value.trim();
}

function buildSkillMarkdown() {
  const name = document.getElementById('wiz-name').value.trim();
  const desc = document.getElementById('wiz-desc').value.trim();
  const corpus = document.getElementById('wiz-corpus').checked;
  const maxTokens = document.getElementById('wiz-maxlen').value;
  const citations = document.getElementById('wiz-citations').checked;
  const tones = wizSelectedTones.length > 0 ? wizSelectedTones : ['Professional'];
  const tmpl = SKILL_TEMPLATES[wizSelectedTemplate] || SKILL_TEMPLATES.custom;
  const sections = tmpl.sections.length > 0 ? tmpl.sections : ['Overview', 'Details', 'Conclusion'];
  const nameSlug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');

  let description = desc;
  if (description.length < 120) description += '. Use this when you need to generate this type of document.';
  if (description.length < 120) description += ' Retrieve relevant corpus documents and apply organizational rules.';

  let md = '---\n';
  md += `name: ${nameSlug}\n`;
  md += `version: 1.0.0\n`;
  md += `description: ${description.split('\n')[0]}\n`;
  md += `skill_type: document\n`;
  md += `requires_corpus: ${corpus}\n`;
  md += `temperature: 0.4\n`;
  md += `max_tokens: ${maxTokens}\n`;
  if (citations) md += `citation_required: true\n`;
  md += '---\n\n';

  md += `# ${name}\n\n`;
  md += `${desc}\n\n`;

  md += `## Retrieval Order\n\n`;
  if (corpus) {
    md += `1. Query the corpus for documents most relevant to the user's request.\n`;
    md += `2. Query for organizational policies, standards, or templates that apply.\n`;
    md += `3. Query for past examples of this document type.\n\n`;
  } else {
    md += `1. Use only the information provided in the user's request.\n`;
    md += `2. If specific data is needed but not provided, mark it with [NEEDS INPUT].\n\n`;
  }

  md += `## Rules\n\n`;
  md += `- Every claim must be traceable to a source document or the user's input. Do not invent facts.\n`;
  md += `- Quantify wherever possible — use numbers, percentages, and dates instead of vague qualifiers.\n`;
  md += `- If a figure, threshold, or approval authority is not available, do not guess — add it to the Gaps section.\n`;
  md += `- Never round or approximate a number from a source document. "$4,750" stays "$4,750".\n`;
  md += `- Every section must add information the reader does not already have. Delete any section that only restates its heading.\n`;
  if (corpus) md += `- Only use facts from the provided corpus documents. Cite sources in brackets when referencing specific data.\n`;
  md += `- Match the level of detail to the user's request and stated audience.\n`;
  md += `- If information is missing for a required field, use [NEEDS INPUT: description of what is needed].\n`;
  md += `- Do not include placeholder language, lorem ipsum, or template markers in the final output.\n`;
  md += `- The output must be complete and ready to use — no "insert X here" instructions to the reader.\n\n`;

  md += `## Boundaries\n\n`;
  md += `- Never fabricate data, statistics, quotes, or source references.\n`;
  md += `- Do not include confidential internal information (margins, cost structures, internal rates) unless the user explicitly requests it.\n`;
  md += `- Must not make promises, guarantees, or commitments on behalf of the organization.\n`;
  md += `- Do not reference competitors by name unless the user's input explicitly discusses them.\n`;
  md += `- Prohibited: generating content that contradicts information in the corpus documents.\n\n`;

  md += `## Output Structure\n\n`;
  sections.forEach(s => {
    const key = s.toLowerCase();
    const guidance = SECTION_GUIDANCE[key] || `Provide specific, actionable content for this section. Include concrete data, names, dates, or figures rather than general statements. If the necessary information is not available, state what is missing using [NEEDS INPUT].`;
    md += `### ${s}\n`;
    md += `${guidance}\n\n`;
  });

  md += `## Self-Check\n\n`;
  md += `Before returning the final output, verify:\n`;
  md += `- [ ] No section restates its heading without adding information.\n`;
  md += `- [ ] Every factual claim traces to a source or the user's input.\n`;
  md += `- [ ] No fabricated figures, names, or dates appear anywhere.\n`;
  md += `- [ ] All [NEEDS INPUT] placeholders identify specifically what is missing.\n`;
  md += `- [ ] The document is complete — no "insert here" instructions remain.\n`;
  if (corpus) md += `- [ ] Source citations are present for corpus-derived information.\n`;
  md += `- [ ] The tone is consistent throughout (no mix of formal and casual).\n\n`;

  md += `## Style\n\n`;
  tones.forEach(t => {
    const guidelines = {
      'Professional': '- Use clear, business-appropriate language.',
      'Formal': '- Use formal tone, avoid contractions and colloquialisms.',
      'Friendly': '- Use warm, approachable language while maintaining credibility.',
      'Technical': '- Use precise technical terminology appropriate to the audience.',
      'Concise': '- Keep sentences short, eliminate filler words, favor tables over paragraphs.',
      'Persuasive': '- Lead with benefits, use active voice, include clear calls to action.',
    };
    md += (guidelines[t] || `- Maintain a ${t.toLowerCase()} tone.`) + '\n';
  });
  md += '- Use tables for any data with three or more comparable items.\n';

  if (corpus) {
    md += `\n## Abstention\n\n`;
    md += `If the corpus contains no documents relevant to the user's request, do not generate the document. Instead output: "[INSUFFICIENT DATA] No relevant documents found in the corpus for this request. Upload the relevant source documents and re-run this skill."\n`;
  }

  md += `\n## Gaps\n\n`;
  md += `List any information this skill needs but does not yet have:\n`;
  md += `- Organization-specific thresholds, approval authorities, and compliance requirements (upload your policy documents to fill these automatically).\n`;
  md += `- Named roles and individuals who approve or sign off on this document type.\n`;

  return md;
}

async function saveWizardSkill() {
  const content = document.getElementById('wiz-preview').value;
  const name = document.getElementById('wiz-name').value.trim();
  const filename = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '') + '.md';

  const btn = document.getElementById('wiz-next');
  btn.disabled = true;
  btn.textContent = 'Saving...';

  try {
    const r = await fetch('/api/skills/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename, content })
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Failed to save skill');

    toast(`Skill "${name}" created! Select it from the Chat dropdown.`, 'success');
    closeSkillWizard();
    loadSkills();
    switchTab('chat');
  } catch(e) {
    toast(e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save Skill';
  }
}

document.getElementById('wizard-modal').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeSkillWizard();
});

// ── Toasts ────────────────────────────────────────────
function toast(msg, type = 'info') {
  const container = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  const icon = type === 'success' ? '&#10003;' : type === 'error' ? '&#10007;' : '&#8505;';
  el.innerHTML = `<span>${icon}</span><span>${msg}</span>`;
  container.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}
