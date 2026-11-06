(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const state = { user: null, convs: [], currentId: null, sending: false, mode: 'login' };
  const SUGGESTIONS = [
    'Explain Docker like I\'m five',
    'Write a haiku about the cloud',
    'What does an AWS security group do?',
    'Give me 3 tips for writing clean Python',
  ];

  // ---------------------------------------------------------------- helpers

  const esc = s => s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function inline(s) {
    return esc(s)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  }

  function blocks(text) {
    let out = '', list = null, para = [];
    const flushPara = () => { if (para.length) { out += `<p>${para.map(inline).join('<br>')}</p>`; para = []; } };
    const flushList = () => { if (list) { out += `<${list.tag}>${list.items.map(i => `<li>${inline(i)}</li>`).join('')}</${list.tag}>`; list = null; } };
    for (const line of text.split('\n')) {
      let m;
      if ((m = line.match(/^\s*[-*•]\s+(.*)/))) { flushPara(); if (list?.tag !== 'ul') { flushList(); list = { tag: 'ul', items: [] }; } list.items.push(m[1]); }
      else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { flushPara(); if (list?.tag !== 'ol') { flushList(); list = { tag: 'ol', items: [] }; } list.items.push(m[1]); }
      else if ((m = line.match(/^#{1,6}\s+(.*)/))) { flushPara(); flushList(); out += `<h4>${inline(m[1])}</h4>`; }
      else if (!line.trim()) { flushPara(); flushList(); }
      else { flushList(); para.push(line); }
    }
    flushPara(); flushList();
    return out;
  }

  // Minimal, XSS-safe Markdown: everything is escaped before our own tags are added.
  function md(src) {
    return src.split('```').map((part, i) => {
      if (i % 2 === 0) return blocks(part);
      const nl = part.indexOf('\n');
      const code = nl >= 0 && /^[\w+#.-]*$/.test(part.slice(0, nl).trim()) ? part.slice(nl + 1) : part;
      return `<pre><code>${esc(code.replace(/\n$/, ''))}</code></pre>`;
    }).join('');
  }

  function toast(message, kind = 'error', ms = 4200) {
    const t = document.createElement('div');
    t.className = `toast glass ${kind}`;
    t.innerHTML = `<span>${esc(message)}</span>`;
    $('#toasts').appendChild(t);
    setTimeout(() => { t.classList.add('out'); t.addEventListener('animationend', () => t.remove()); }, ms);
  }

  async function api(path, { method = 'GET', body } = {}) {
    const res = await fetch(path, {
      method,
      credentials: 'same-origin',
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { /* empty body */ }
    if (!res.ok) {
      let msg = typeof data?.detail === 'string' ? data.detail
        : Array.isArray(data?.detail) ? data.detail.map(d => d.msg.replace(/^Value error, /, '')).join('; ')
        : res.statusText;
      if (res.status === 429) {
        const ra = res.headers.get('Retry-After');
        if (ra) msg += ` (retry in ${ra}s)`;
      }
      const err = new Error(msg);
      err.status = res.status;
      if (res.status === 401 && !path.startsWith('/api/auth')) showAuth();
      throw err;
    }
    return data;
  }

  // ---------------------------------------------------------------- auth view

  function setMode(mode) {
    state.mode = mode;
    $('.tabs').dataset.mode = mode;
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.mode === mode));
    $('#auth-submit .label').textContent = mode === 'login' ? 'Sign in' : 'Create account';
    $('#password').autocomplete = mode === 'login' ? 'current-password' : 'new-password';
    $('#pw-hint').hidden = mode === 'login';
    $('#auth-error').textContent = '';
  }

  function showAuth() {
    state.user = null;
    $('#app').hidden = true;
    $('#auth').hidden = false;
    $('#splash').classList.add('out');
    setTimeout(() => $('#email').focus(), 50);
  }

  document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => setMode(t.dataset.mode)));

  $('#auth-form').addEventListener('submit', async e => {
    e.preventDefault();
    const btn = $('#auth-submit'), err = $('#auth-error');
    const email = $('#email').value.trim(), password = $('#password').value;
    err.textContent = '';
    if (!email || !password) { err.textContent = 'Enter your email and password.'; return; }
    if (state.mode === 'signup' && password.length < 8) { err.textContent = 'Password must be at least 8 characters.'; return; }
    btn.disabled = true; btn.classList.add('loading');
    try {
      const data = await api(`/api/auth/${state.mode}`, { method: 'POST', body: { email, password } });
      $('#password').value = '';
      await showApp(data.email);
    } catch (ex) {
      err.textContent = ex.message;
      const card = $('.auth-card');
      card.classList.remove('shake'); void card.offsetWidth; card.classList.add('shake');
    } finally {
      btn.disabled = false; btn.classList.remove('loading');
    }
  });

  // ---------------------------------------------------------------- app view

  async function showApp(email) {
    state.user = email;
    $('#user-email').textContent = email;
    $('#avatar').textContent = email[0].toUpperCase();
    $('#auth').hidden = true;
    $('#app').hidden = false;
    $('#splash').classList.add('out');
    await loadConversations();
    if (state.convs.length) await openConversation(state.convs[0].id);
    else renderEmpty();
  }

  async function loadConversations() {
    state.convs = await api('/api/conversations');
    renderConvList();
  }

  function renderConvList() {
    const list = $('#conv-list');
    list.innerHTML = '';
    if (!state.convs.length) {
      list.innerHTML = '<div class="conv-empty">No chats yet.<br>Start one above ✦</div>';
      return;
    }
    state.convs.forEach((c, i) => {
      const el = document.createElement('div');
      el.className = 'conv' + (c.id === state.currentId ? ' active' : '');
      el.style.setProperty('--i', i);
      el.dataset.id = c.id;
      el.innerHTML = `<span class="ellipsis">${esc(c.title)}</span>
        <button class="icon-btn" title="Delete chat" aria-label="Delete chat"><svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg></button>`;
      el.addEventListener('click', () => { openConversation(c.id); closeSidebar(); });
      el.querySelector('button').addEventListener('click', ev => { ev.stopPropagation(); deleteConversation(c.id, el); });
      list.appendChild(el);
    });
  }

  function markActive() {
    document.querySelectorAll('.conv').forEach(el => el.classList.toggle('active', +el.dataset.id === state.currentId));
  }

  function renderEmpty() {
    state.currentId = null;
    markActive();
    $('#chat-title').textContent = 'New chat';
    const log = $('#log');
    log.className = 'log';
    log.innerHTML = `<div class="empty">
        <div class="orb"></div>
        <h2>How can I help today?</h2>
        <p class="muted">Ask anything. I'm Claude Haiku running on Amazon Bedrock.</p>
        <div class="chips">${SUGGESTIONS.map((s, i) => `<button class="chip" style="--i:${i}">${esc(s)}</button>`).join('')}</div>
      </div>`;
    log.querySelectorAll('.chip').forEach(ch => ch.addEventListener('click', () => { $('#input').value = ch.textContent; autoGrow(); send(); }));
    $('#input').focus();
  }

  function addRow(role, content, { typing = false } = {}) {
    $('.empty')?.remove();
    const row = document.createElement('div');
    row.className = `row ${role === 'user' ? 'user' : 'bot'}`;
    if (role !== 'user') row.innerHTML = '<div class="orb"></div>';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    if (typing) bubble.innerHTML = '<div class="typing"><i></i><i></i><i></i></div>';
    else if (role === 'user') bubble.textContent = content;
    else bubble.innerHTML = md(content);
    row.appendChild(bubble);
    $('#log').appendChild(row);
    scrollDown();
    return row;
  }

  function addDivider(summary) {
    const d = document.createElement('details');
    d.className = 'divider';
    d.innerHTML = '<summary>✦ Earlier messages compacted into a summary · click to view</summary><div class="summary"></div>';
    d.querySelector('.summary').textContent = summary || '';
    $('#log').appendChild(d);
  }

  const scrollDown = () => { const log = $('#log'); log.scrollTop = log.scrollHeight; };

  async function openConversation(id, { animate = true } = {}) {
    const conv = await api(`/api/conversations/${id}`);
    state.currentId = id;
    markActive();
    $('#chat-title').textContent = conv.title;
    const log = $('#log');
    log.innerHTML = '';
    log.className = animate ? 'log' : 'log no-anim';
    if (!conv.messages.length) { renderEmpty(); state.currentId = id; markActive(); return; }
    for (const m of conv.messages) {
      addRow(m.role, m.content);
      if (conv.compacted_upto && m.id === conv.compacted_upto) addDivider(conv.summary);
    }
    requestAnimationFrame(() => { log.className = 'log'; scrollDown(); });
  }

  async function deleteConversation(id, el) {
    try {
      await api(`/api/conversations/${id}`, { method: 'DELETE' });
    } catch (ex) { toast(ex.message); return; }
    el.classList.add('removing');
    setTimeout(async () => {
      state.convs = state.convs.filter(c => c.id !== id);
      renderConvList();
      if (state.currentId === id) {
        if (state.convs.length) await openConversation(state.convs[0].id); else renderEmpty();
      }
    }, 280);
  }

  async function send() {
    const input = $('#input');
    const text = input.value.trim();
    if (!text || state.sending) return;
    state.sending = true;
    input.value = ''; autoGrow();

    const userRow = addRow('user', text);
    const typingRow = addRow('assistant', '', { typing: true });
    try {
      if (!state.currentId) {
        const conv = await api('/api/conversations', { method: 'POST' });
        state.convs.unshift(conv);
        state.currentId = conv.id;
        renderConvList();
      }
      const id = state.currentId;
      const data = await api(`/api/conversations/${id}/messages`, { method: 'POST', body: { content: text } });
      typingRow.remove();
      // Bump this chat to the top of the sidebar with its (possibly new) title.
      state.convs = [{ id, title: data.title, updated_at: Date.now() / 1000 }, ...state.convs.filter(c => c.id !== id)];
      renderConvList();
      $('#chat-title').textContent = data.title;
      $('#model-badge').title = `${data.model} · ${data.input_tokens} in / ${data.output_tokens} out tokens`;
      if (data.compacted) {
        await openConversation(id, { animate: false });
        toast('This chat got long, so older messages were compacted into a summary.', 'info');
      } else {
        addRow('assistant', data.reply);
      }
    } catch (ex) {
      typingRow.remove();
      userRow.remove();
      if (!input.value) { input.value = text; autoGrow(); }
      if (ex.status !== 401) toast(ex.message);
    } finally {
      state.sending = false;
      updateSendButton();
      input.focus();
    }
  }

  // ---------------------------------------------------------------- composer & chrome

  function autoGrow() {
    const t = $('#input');
    t.style.height = 'auto';
    t.style.height = Math.min(t.scrollHeight, 180) + 'px';
    updateSendButton();
  }
  const updateSendButton = () => { $('#send').disabled = !$('#input').value.trim() || state.sending; };

  $('#input').addEventListener('input', autoGrow);
  $('#input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  $('#composer').addEventListener('submit', e => { e.preventDefault(); send(); });
  $('#new-chat').addEventListener('click', () => { renderEmpty(); closeSidebar(); });

  $('#logout').addEventListener('click', async () => {
    try { await api('/api/auth/logout', { method: 'POST' }); } catch { /* ignore */ }
    state.convs = []; state.currentId = null;
    setMode('login');
    showAuth();
  });

  const openSidebar = () => { $('#sidebar').classList.add('open'); $('#scrim').classList.add('show'); };
  const closeSidebar = () => { $('#sidebar').classList.remove('open'); $('#scrim').classList.remove('show'); };
  $('#open-sidebar').addEventListener('click', openSidebar);
  $('#close-sidebar').addEventListener('click', closeSidebar);
  $('#scrim').addEventListener('click', closeSidebar);

  // ---------------------------------------------------------------- boot

  (async () => {
    try {
      const me = await api('/api/me');
      await showApp(me.email);
    } catch (ex) {
      if (ex.status === 403) { showAuth(); $('#auth-error').textContent = ex.message; }
      else showAuth();
    }
  })();
})();
