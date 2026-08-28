// ZettaBrain Lite — Frontend Application

let ws = null;
let currentSkill = null;

// ── Tab Navigation ──────────────────────────────────────────────────────────
function showTab(tab) {
  document.querySelectorAll('[id^="tab-"]').forEach(el => el.classList.add('hidden'));
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  document.getElementById(`tab-${tab}`).classList.remove('hidden');
  document.getElementById(`nav-${tab}`).classList.add('active');

  if (tab === 'storage') loadStorage();
  if (tab === 'ingest') loadIngest();
  if (tab === 'settings') loadSettings();
  if (tab === 'generate') loadSkills();
}

// ── Status Check ────────────────────────────────────────────────────────────
async function checkStatus() {
  try {
    const r = await fetch('/api/status');
    const data = await r.json();
    const dot = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    document.getElementById('chunk-count').textContent = data.vectorstore.chunks;

    if (data.ollama.running) {
      dot.className = 'dot dot-green';
      text.textContent = `Ollama OK | ${data.vectorstore.chunks} chunks`;
    } else {
      dot.className = 'dot dot-red';
      text.textContent = 'Ollama offline';
    }
  } catch {
    document.getElementById('status-dot').className = 'dot dot-red';
    document.getElementById('status-text').textContent = 'Server unreachable';
  }
}

// ── Models Dropdown ─────────────────────────────────────────────────────────
async function loadModels() {
  try {
    const r = await fetch('/api/models');
    const data = await r.json();
    const select = document.getElementById('model-select');
    select.innerHTML = '';
    if (data.models.length === 0) {
      select.innerHTML = '<option value="">No models available</option>';
      return;
    }
    data.models.forEach(m => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.label;
      select.appendChild(opt);
    });
  } catch {
    document.getElementById('model-select').innerHTML = '<option value="">Error loading models</option>';
  }
}

// ── Chat (WebSocket streaming) ──────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws/chat`);
  ws.onclose = () => { setTimeout(connectWS, 2000); };
}

function sendChat() {
  const input = document.getElementById('chat-input');
  const question = input.value.trim();
  if (!question) return;

  const model = document.getElementById('model-select').value;
  appendMsg('user', question);
  input.value = '';

  if (!ws || ws.readyState !== WebSocket.OPEN) {
    appendMsg('assistant', 'Connection lost. Reconnecting...');
    connectWS();
    return;
  }

  ws.send(JSON.stringify({ question, model }));

  const msgDiv = appendMsg('assistant', '<span class="spinner"></span>');
  const bubble = msgDiv.querySelector('.bubble');
  let fullAnswer = '';
  let sources = [];

  const handler = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === 'sources') {
      sources = data.sources;
    } else if (data.type === 'token') {
      if (bubble.querySelector('.spinner')) bubble.innerHTML = '';
      fullAnswer += data.token;
      bubble.innerHTML = renderMarkdown(fullAnswer);
    } else if (data.type === 'done') {
      ws.removeEventListener('message', handler);
      bubble.innerHTML = renderMarkdown(fullAnswer);
      if (sources.length) {
        const bar = document.createElement('div');
        bar.className = 'sources-bar';
        sources.forEach(s => {
          const chip = document.createElement('span');
          chip.className = 'source-chip';
          chip.textContent = s.filename;
          bar.appendChild(chip);
        });
        msgDiv.appendChild(bar);
      }
      if (data.timing) {
        const meta = document.createElement('div');
        meta.className = 'meta';
        meta.textContent = `${data.timing.retrieve_ms}ms retrieval | ${data.timing.generate_ms}ms generation | ${data.chunks_searched} chunks`;
        msgDiv.appendChild(meta);
      }
    } else if (data.type === 'error') {
      ws.removeEventListener('message', handler);
      bubble.innerHTML = `<span style="color:var(--error)">${data.message}</span>`;
    }
  };
  ws.addEventListener('message', handler);
}

function appendMsg(role, content) {
  const container = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = `chat-msg ${role}`;
  div.innerHTML = `<div class="bubble">${content}</div>`;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
}

function renderMarkdown(text) {
  return text
    .replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/\n/g, '<br>');
}

// ── Storage Management ──────────────────────────────────────────────────────
async function loadStorage() {
  try {
    const r = await fetch('/api/storage');
    const data = await r.json();
    const tbody = document.getElementById('storage-table');
    tbody.innerHTML = '';
    data.sources.forEach((s, i) => {
      tbody.innerHTML += `<tr>
        <td>${s.type.toUpperCase()}</td>
        <td>${s.label}</td>
        <td style="font-family:var(--mono);font-size:0.78rem;">${s.path}</td>
        <td>${s.role}</td>
        <td><button class="btn btn-sm btn-danger" onclick="removeStorage(${i})">Remove</button></td>
      </tr>`;
    });
  } catch (e) {
    document.getElementById('storage-table').innerHTML = '<tr><td colspan="5">Error loading sources</td></tr>';
  }
}

async function addStorage() {
  const type = document.getElementById('storage-type').value;
  const label = document.getElementById('storage-label').value.trim();
  const path = document.getElementById('storage-path').value.trim();
  if (!label || !path) { toast('Fill in label and path', 'error'); return; }

  const r = await fetch('/api/storage', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ type, label, path, role: 'secondary' }),
  });
  if (r.ok) {
    toast('Storage source added');
    document.getElementById('storage-label').value = '';
    document.getElementById('storage-path').value = '';
    loadStorage();
  } else {
    const err = await r.json();
    toast(err.detail || 'Error', 'error');
  }
}

async function removeStorage(index) {
  if (!confirm('Remove this storage source?')) return;
  const r = await fetch(`/api/storage/${index}`, { method: 'DELETE' });
  if (r.ok) { toast('Removed'); loadStorage(); }
}

// ── Ingestion ───────────────────────────────────────────────────────────────
async function loadIngest() {
  const r = await fetch('/api/storage');
  const data = await r.json();
  const container = document.getElementById('ingest-sources');
  container.innerHTML = '';
  data.sources.forEach((s, i) => {
    container.innerHTML += `<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">
      <span style="font-size:0.82rem;">${s.type.toUpperCase()} - ${s.label} (${s.path})</span>
      <button class="btn btn-sm btn-secondary" onclick="ingestSource(${i})">Ingest</button>
    </div>`;
  });

  const sr = await fetch('/api/sources');
  const sd = await sr.json();
  document.getElementById('ingested-files').innerHTML = sd.sources.length
    ? sd.sources.map(f => `<div style="padding:3px 0;">${f}</div>`).join('')
    : '<em>No files ingested yet</em>';
}

async function ingestSource(index) {
  const output = document.getElementById('ingest-output');
  output.style.display = 'block';
  output.textContent = 'Ingesting...';
  const r = await fetch(`/api/ingest/source/${index}`, { method: 'POST' });
  const data = await r.json();
  output.textContent = data.success ? data.output : (data.error || 'Failed');
  checkStatus();
  loadIngest();
}

async function ingestAll() {
  const output = document.getElementById('ingest-output');
  output.style.display = 'block';
  output.textContent = 'Ingesting all sources...';
  const r = await fetch('/api/ingest', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' });
  const data = await r.json();
  output.textContent = data.success ? data.output : (data.error || 'Failed');
  checkStatus();
  loadIngest();
}

async function clearVectorstore() {
  if (!confirm('Clear the entire vector store? You will need to re-ingest.')) return;
  await fetch('/api/vectorstore', { method: 'DELETE' });
  toast('Vector store cleared');
  checkStatus();
}

// ── Settings ────────────────────────────────────────────────────────────────
async function loadSettings() {
  const r = await fetch('/api/settings');
  const data = await r.json();
  const s = data.settings;
  document.getElementById('set-ollama-host').value = s.ollama_host || '';
  document.getElementById('set-llm-model').value = s.llm_model || '';
  document.getElementById('set-embed-model').value = s.embed_model || '';
  document.getElementById('set-groq-key').value = s.groq_api_key || '';
  document.getElementById('set-openai-key').value = s.openai_api_key || '';
  document.getElementById('set-anthropic-key').value = s.anthropic_api_key || '';
  document.getElementById('set-together-key').value = s.together_api_key || '';
  document.getElementById('set-cerebras-key').value = s.cerebras_api_key || '';
  document.getElementById('set-openrouter-key').value = s.openrouter_api_key || '';
  document.getElementById('set-fireworks-key').value = s.fireworks_api_key || '';
}

async function saveSettings() {
  const settings = {};
  const fields = [
    ['set-ollama-host', 'ollama_host'],
    ['set-llm-model', 'llm_model'],
    ['set-embed-model', 'embed_model'],
    ['set-groq-key', 'groq_api_key'],
    ['set-openai-key', 'openai_api_key'],
    ['set-anthropic-key', 'anthropic_api_key'],
    ['set-together-key', 'together_api_key'],
    ['set-cerebras-key', 'cerebras_api_key'],
    ['set-openrouter-key', 'openrouter_api_key'],
    ['set-fireworks-key', 'fireworks_api_key'],
  ];
  fields.forEach(([id, key]) => {
    const val = document.getElementById(id).value.trim();
    if (val) settings[key] = val;
  });

  const r = await fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ settings }),
  });
  if (r.ok) {
    toast('Settings saved');
    loadModels();
  } else {
    toast('Error saving settings', 'error');
  }
}

async function pullModel() {
  const name = document.getElementById('pull-model-name').value.trim();
  if (!name) return;
  const output = document.getElementById('pull-output');
  output.style.display = 'block';
  output.textContent = 'Pulling...';

  const r = await fetch('/api/pull', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ model: name }),
  });

  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    output.textContent += decoder.decode(value);
    output.scrollTop = output.scrollHeight;
  }
  toast('Model pull complete');
  loadModels();
}

// ── Skills / Generate ───────────────────────────────────────────────────────
async function loadSkills() {
  const r = await fetch('/api/skills');
  const data = await r.json();
  const grid = document.getElementById('skills-list');
  grid.innerHTML = '';
  document.getElementById('generate-form').classList.add('hidden');
  grid.classList.remove('hidden');

  if (data.skills.length === 0) {
    grid.innerHTML = '<div class="card"><p style="color:var(--muted)">No skills installed. Upload a skill to get started.</p></div>';
    return;
  }

  data.skills.forEach(s => {
    const tags = (s.tags || []).map(t => `<span class="skill-tag">${t}</span>`).join('');
    grid.innerHTML += `<div class="skill-card" onclick="selectSkill('${s.name}', '${s.description.replace(/'/g, "\\'")}')">
      <h4>${s.name}</h4>
      <p>${s.description}</p>
      ${tags}
      ${s.requires_corpus ? '<span class="skill-tag" style="color:var(--primary)">uses docs</span>' : ''}
    </div>`;
  });
}

function selectSkill(name, desc) {
  currentSkill = name;
  document.getElementById('skills-list').classList.add('hidden');
  document.getElementById('generate-form').classList.remove('hidden');
  document.getElementById('gen-skill-name').textContent = name;
  document.getElementById('gen-skill-desc').textContent = desc;
  document.getElementById('gen-output').classList.add('hidden');
}

function backToSkills() {
  document.getElementById('generate-form').classList.add('hidden');
  document.getElementById('skills-list').classList.remove('hidden');
}

async function runGenerate() {
  const input = document.getElementById('gen-input').value.trim();
  if (!input) { toast('Enter a request', 'error'); return; }

  const outputDiv = document.getElementById('gen-output');
  const contentDiv = document.getElementById('gen-content');
  outputDiv.classList.remove('hidden');
  contentDiv.innerHTML = '<span class="spinner"></span> Generating...';

  const r = await fetch('/api/generate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ input, skill_name: currentSkill }),
  });

  if (r.ok) {
    const data = await r.json();
    contentDiv.textContent = data.content;
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = `Generated in ${data.generation_time_ms}ms`;
    contentDiv.appendChild(meta);
  } else {
    const err = await r.json();
    contentDiv.innerHTML = `<span style="color:var(--error)">${err.detail || 'Generation failed'}</span>`;
  }
}

function showUploadSkill() { document.getElementById('upload-modal').classList.remove('hidden'); }
function hideUploadSkill() { document.getElementById('upload-modal').classList.add('hidden'); }

async function uploadSkill() {
  const filename = document.getElementById('upload-filename').value.trim();
  const content = document.getElementById('upload-content').value;
  if (!filename || !content) { toast('Fill in filename and content', 'error'); return; }

  const r = await fetch('/api/skills/upload', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ filename, content }),
  });

  if (r.ok) {
    toast('Skill uploaded');
    hideUploadSkill();
    loadSkills();
  } else {
    const err = await r.json();
    toast(err.detail || 'Upload failed', 'error');
  }
}

// ── Toast Notification ──────────────────────────────────────────────────────
function toast(msg, type = 'success') {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

// ── Init ────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  checkStatus();
  loadModels();
  connectWS();
  setInterval(checkStatus, 30000);
});
