/* GrokBot front-end: SSE streaming chat, conversation list, tiny Markdown renderer. */
'use strict';

const state = {
  config: null,
  conversations: [],
  currentId: null,
  persona: 'grok',
  streaming: false,
  controller: null,
  menuOpen: false,
};

const el = {
  convList: document.getElementById('convList'),
  thread: document.getElementById('thread'),
  messages: document.getElementById('messages'),
  emptyState: document.getElementById('emptyState'),
  emptyNote: document.getElementById('emptyNote'),
  suggestions: document.getElementById('suggestions'),
  input: document.getElementById('input'),
  composer: document.getElementById('composer'),
  sendBtn: document.getElementById('sendBtn'),
  stopBtn: document.getElementById('stopBtn'),
  chatTitle: document.getElementById('chatTitle'),
  chatSub: document.getElementById('chatSub'),
  personaPills: document.getElementById('personaPills'),
  providerBadge: document.getElementById('providerBadge'),
  footHint: document.getElementById('footHint'),
  modeTag: document.getElementById('modeTag'),
  newChat: document.getElementById('newChat'),
  sidebar: document.getElementById('sidebar'),
  menuBtn: document.getElementById('menuBtn'),
  brandVersion: document.getElementById('brandVersion'),
};

/* ------------------------------------------------------------------ utils */

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function inline(text) {
  return text
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
}

/** Renders a safe subset of Markdown: fences, headings, lists, tables, quotes, inline. */
function renderMarkdown(source) {
  const codeBlocks = [];
  const stash = (lang, code) => {
    codeBlocks.push({ lang: (lang || '').trim(), code });
    return '@@CODE' + (codeBlocks.length - 1) + '@@';
  };

  let text = source.replace(/```([^\n`]*)\n?([\s\S]*?)```/g, (m, lang, code) => stash(lang, code));
  text = text.replace(/```([^\n`]*)\n?([\s\S]*)$/, (m, lang, code) => stash(lang, code));
  text = escapeHtml(text);

  const lines = text.split('\n');
  const out = [];
  let index = 0;

  const isTableRow = (line) => /^\s*\|.*\|\s*$/.test(line);
  const isDivider = (line) => /^\s*\|([\s:|-]+\|)+\s*$/.test(line);

  while (index < lines.length) {
    const line = lines[index];
    const trimmed = line.trim();

    if (!trimmed) { index += 1; continue; }

    const codeRef = /^@@CODE(\d+)@@$/.exec(trimmed);
    if (codeRef) {
      const block = codeBlocks[Number(codeRef[1])] || { code: '' };
      out.push('<pre><code>' + escapeHtml(block.code.replace(/\n$/, '')) + '</code></pre>');
      index += 1;
      continue;
    }

    const heading = /^(#{1,3})\s+(.*)$/.exec(trimmed);
    if (heading) {
      const level = heading[1].length;
      out.push('<h' + level + '>' + inline(heading[2]) + '</h' + level + '>');
      index += 1;
      continue;
    }

    if (/^(-{3,}|\*{3,})$/.test(trimmed)) {
      out.push('<hr />');
      index += 1;
      continue;
    }

    if (isTableRow(line) && index + 1 < lines.length && isDivider(lines[index + 1])) {
      const header = splitRow(line);
      index += 2;
      const rows = [];
      while (index < lines.length && isTableRow(lines[index])) {
        rows.push(splitRow(lines[index]));
        index += 1;
      }
      let html = '<table><thead><tr>' + header.map((c) => '<th>' + inline(c) + '</th>').join('') + '</tr></thead><tbody>';
      rows.forEach((row) => {
        html += '<tr>' + row.map((c) => '<td>' + inline(c) + '</td>').join('') + '</tr>';
      });
      out.push(html + '</tbody></table>');
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      const items = [];
      while (index < lines.length && /^\s*[-*+]\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*[-*+]\s+/, ''));
        index += 1;
      }
      out.push('<ul>' + items.map((item) => '<li>' + inline(item) + '</li>').join('') + '</ul>');
      continue;
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (index < lines.length && /^\s*\d+\.\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*\d+\.\s+/, ''));
        index += 1;
      }
      out.push('<ol>' + items.map((item) => '<li>' + inline(item) + '</li>').join('') + '</ol>');
      continue;
    }

    if (/^&gt;\s?/.test(trimmed)) {
      const quoted = [];
      while (index < lines.length && /^\s*&gt;\s?/.test(lines[index])) {
        quoted.push(lines[index].replace(/^\s*&gt;\s?/, ''));
        index += 1;
      }
      out.push('<blockquote>' + inline(quoted.join(' ')) + '</blockquote>');
      continue;
    }

    const paragraph = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() &&
           !/^#\s/.test(lines[index]) && !/^@@CODE\d+@@$/.test(lines[index].trim()) &&
           !/^\s*[-*+]\s+/.test(lines[index]) && !/^\s*\d+\.\s+/.test(lines[index]) &&
           !isTableRow(lines[index])) {
      paragraph.push(lines[index]);
      index += 1;
    }
    out.push('<p>' + inline(paragraph.join('<br />')) + '</p>');
  }

  return out.join('\n');
}

function splitRow(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
}

/* --------------------------------------------------------------- rendering */

function scrollIfNearBottom(force) {
  const threshold = 140;
  const distance = el.thread.scrollHeight - el.thread.scrollTop - el.thread.clientHeight;
  if (force || distance < threshold) {
    el.thread.scrollTop = el.thread.scrollHeight;
  }
}

function createBubble(role) {
  const node = document.createElement('div');
  node.className = 'msg ' + role;

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = role === 'user' ? '你' : '✳';

  const body = document.createElement('div');
  body.className = 'body';

  const roleLabel = document.createElement('div');
  roleLabel.className = 'role';
  roleLabel.textContent = role === 'user' ? 'YOU' : 'GROKBOT';

  const content = document.createElement('div');
  content.className = 'md';

  body.appendChild(roleLabel);
  body.appendChild(content);
  node.appendChild(avatar);
  node.appendChild(body);
  el.messages.appendChild(node);
  scrollIfNearBottom(true);

  let text = '';
  let rafId = null;

  const paint = (streaming) => {
    if (role === 'user') {
      content.innerHTML = '<p>' + escapeHtml(text).replace(/\n/g, '<br />') + '</p>';
    } else {
      content.innerHTML = renderMarkdown(text) + (streaming ? '<span class="caret"></span>' : '');
    }
    scrollIfNearBottom(false);
  };

  const bubble = {
    node,
    body,
    raw: () => text,
    setThinking() {
      content.innerHTML = '<div class="thinking"><span></span><span></span><span></span></div>';
    },
    setContent(next, streaming) {
      text = next;
      if (rafId) return;
      rafId = requestAnimationFrame(() => {
        rafId = null;
        paint(streaming);
      });
    },
    flush(streaming) {
      if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
      paint(streaming);
    },
    setError(message, hint) {
      const box = document.createElement('div');
      box.className = 'err';
      box.textContent = message;
      if (hint) {
        const hintNode = document.createElement('span');
        hintNode.className = 'hint';
        hintNode.textContent = hint;
        box.appendChild(hintNode);
      }
      body.appendChild(box);
      scrollIfNearBottom(true);
    },
    addCopy() {
      if (role !== 'assistant') return;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'copy';
      button.textContent = '复制';
      button.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(text);
          button.textContent = '已复制';
          setTimeout(() => { button.textContent = '复制'; }, 1400);
        } catch (_) {
          button.textContent = '复制失败';
        }
      });
      body.appendChild(button);
    },
  };

  return bubble;
}

function addMessage(role, text) {
  const bubble = createBubble(role);
  bubble.setContent(text, false);
  bubble.flush(false);
  if (role === 'assistant') bubble.addCopy();
  return bubble;
}

/* ----------------------------------------------------------- conversations */

function showEmpty() {
  el.emptyState.style.display = '';
  el.messages.innerHTML = '';
}

function hideEmpty() {
  el.emptyState.style.display = 'none';
}

function renderConversations() {
  el.convList.innerHTML = '';
  if (!state.conversations.length) {
    const note = document.createElement('div');
    note.className = 'conv-empty';
    note.textContent = '还没有会话，直接发一句话就开始。';
    el.convList.appendChild(note);
    return;
  }
  state.conversations.forEach((conv) => {
    const item = document.createElement('div');
    item.className = 'conv-item' + (conv.id === state.currentId ? ' active' : '');
    item.dataset.id = conv.id;

    const title = document.createElement('span');
    title.className = 'conv-title';
    title.textContent = conv.title || '新对话';

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'conv-del';
    del.title = '删除会话';
    del.textContent = '×';
    del.addEventListener('click', async (event) => {
      event.stopPropagation();
      if (!confirm('删除这个会话？')) return;
      try {
        await apiDelete('/api/conversations/' + conv.id);
        state.conversations = state.conversations.filter((c) => c.id !== conv.id);
        if (state.currentId === conv.id) {
          state.currentId = null;
          el.chatTitle.textContent = '新对话';
          showEmpty();
        }
        renderConversations();
      } catch (error) {
        alert(error.message);
      }
    });

    item.appendChild(title);
    item.appendChild(del);
    item.addEventListener('click', () => openConversation(conv.id));
    el.convList.appendChild(item);
  });
}

async function apiDelete(path) {
  const response = await fetch(path, { method: 'DELETE' });
  if (!response.ok) {
    let detail = '删除失败';
    try { detail = (await response.json()).detail || detail; } catch (_) { /* ignore */ }
    throw new Error(detail);
  }
  return response.json();
}

async function openConversation(id) {
  try {
    const response = await fetch('/api/conversations/' + id);
    if (!response.ok) throw new Error('会话不存在');
    const data = await response.json();
    state.currentId = data.id;
    state.persona = data.persona || state.persona;
    renderPersonas();
    el.chatTitle.textContent = data.title || '新对话';
    hideEmpty();
    el.messages.innerHTML = '';
    data.messages.forEach((message) => addMessage(message.role, message.content));
    renderConversations();
    scrollIfNearBottom(true);
  } catch (error) {
    console.warn(error);
  }
}

async function refreshConversations() {
  try {
    const response = await fetch('/api/conversations');
    const data = await response.json();
    state.conversations = data.conversations || [];
    renderConversations();
  } catch (error) {
    console.warn(error);
  }
}

/* ------------------------------------------------------------------- config */

function renderPersonas() {
  if (!state.config) return;
  el.personaPills.innerHTML = '';
  state.config.personas.forEach((persona) => {
    const pill = document.createElement('button');
    pill.type = 'button';
    pill.className = 'pill' + (persona.id === state.persona ? ' active' : '');
    pill.textContent = persona.label;
    pill.title = persona.description;
    pill.addEventListener('click', () => {
      state.persona = persona.id;
      renderPersonas();
    });
    el.personaPills.appendChild(pill);
  });
}

async function loadConfig() {
  const response = await fetch('/api/config');
  const config = await response.json();
  state.config = config;
  state.persona = config.default_persona || 'grok';

  el.providerBadge.classList.add(config.demo_mode ? 'offline' : 'live');
  el.providerBadge.querySelector('.provider-text').textContent =
    config.display_name + ' · ' + config.model;
  el.footHint.textContent = config.hint;
  el.modeTag.textContent = config.demo_mode ? 'demo' : config.provider;
  el.brandVersion.textContent = 'v1.0';
  if (config.demo_mode) {
    el.emptyNote.textContent = '当前是离线演示引擎 —— 会算数，复杂问题只给骨架。填 OPENAI_API_KEY 或启动 Ollama 即可换成真实模型。';
  } else {
    el.emptyNote.textContent = '已连接 ' + config.display_name + '。直说，别客气。';
  }
  el.chatSub.textContent = config.display_name + ' · 流式输出';
  renderPersonas();
}

/* --------------------------------------------------------------------- chat */

function parseFrame(frame) {
  let event = 'message';
  const dataLines = [];
  frame.split('\n').forEach((line) => {
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
  });
  if (!dataLines.length) return null;
  try {
    return { event, data: JSON.parse(dataLines.join('\n')) };
  } catch (_) {
    return null;
  }
}

function setStreaming(active) {
  state.streaming = active;
  el.sendBtn.hidden = active;
  el.stopBtn.hidden = !active;
  el.sendBtn.disabled = active;
  el.input.disabled = false;
  if (!active) state.controller = null;
}

async function sendMessage(rawText) {
  if (state.streaming) return;
  const message = (rawText !== undefined ? rawText : el.input.value).trim();
  if (!message) return;

  hideEmpty();
  addMessage('user', message);
  el.input.value = '';
  autosize();

  const bubble = createBubble('assistant');
  bubble.setThinking();
  setStreaming(true);
  state.controller = new AbortController();

  let answer = '';
  let settled = false;

  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, conversation_id: state.currentId, persona: state.persona }),
      signal: state.controller.signal,
    });

    if (!response.ok || !response.body) {
      let detail = '请求失败（HTTP ' + response.status + '）';
      try { detail = (await response.json()).detail || detail; } catch (_) { /* ignore */ }
      bubble.setError(detail, '检查消息长度或服务端日志。');
      setStreaming(false);
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let firstDelta = true;

    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      const frames = buffer.split('\n\n');
      buffer = frames.pop();
      for (const frame of frames) {
        const parsed = parseFrame(frame);
        if (!parsed) continue;
        const data = parsed.data;

        if (parsed.event === 'meta') {
          if (!state.currentId) {
            state.currentId = data.conversation_id;
            el.chatTitle.textContent = data.title || '新对话';
            refreshConversations();
          }
        } else if (parsed.event === 'delta') {
          if (firstDelta) { firstDelta = false; }
          answer += data.text;
          bubble.setContent(answer, true);
        } else if (parsed.event === 'done') {
          settled = true;
          bubble.flush(false);
          bubble.addCopy();
          if (data.title) el.chatTitle.textContent = data.title;
          refreshConversations();
        } else if (parsed.event === 'error') {
          settled = true;
          if (answer) { bubble.flush(false); bubble.addCopy(); }
          bubble.setError(data.message || '模型调用失败', data.hint);
        }
      }
    }

    if (!settled) {
      if (answer) { bubble.flush(false); bubble.addCopy(); }
      else bubble.setContent('（没有收到内容）', false);
    }
  } catch (error) {
    if (error.name === 'AbortError') {
      if (answer) {
        bubble.flush(false);
        bubble.addCopy();
      } else {
        bubble.setContent('（已停止）', false);
      }
    } else {
      bubble.setError('网络错误：' + error.message, '确认服务是否在运行。');
    }
  } finally {
    setStreaming(false);
    el.input.focus();
  }
}

/* ---------------------------------------------------------------- composer */

function autosize() {
  el.input.style.height = 'auto';
  el.input.style.height = Math.min(el.input.scrollHeight, 190) + 'px';
}

el.input.addEventListener('input', autosize);
el.input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendMessage();
  }
});

el.composer.addEventListener('submit', (event) => {
  event.preventDefault();
  sendMessage();
});

el.stopBtn.addEventListener('click', () => {
  if (state.controller) state.controller.abort();
});

el.newChat.addEventListener('click', () => {
  state.currentId = null;
  el.chatTitle.textContent = '新对话';
  showEmpty();
  renderConversations();
  el.input.focus();
  closeMenu();
});

el.suggestions.addEventListener('click', (event) => {
  const target = event.target.closest('button[data-prompt]');
  if (target) sendMessage(target.dataset.prompt);
});

/* -------------------------------------------------------------- mobile menu */

function openMenu() { state.menuOpen = true; el.sidebar.classList.add('open'); }
function closeMenu() { state.menuOpen = false; el.sidebar.classList.remove('open'); }
el.menuBtn.addEventListener('click', () => (state.menuOpen ? closeMenu() : openMenu()));
el.thread.addEventListener('click', () => { if (state.menuOpen) closeMenu(); });

/* -------------------------------------------------------------------- boot */

(async function boot() {
  try {
    await loadConfig();
  } catch (error) {
    el.providerBadge.querySelector('.provider-text').textContent = '服务未就绪';
  }
  await refreshConversations();
  if (state.conversations.length) {
    await openConversation(state.conversations[0].id);
  } else {
    showEmpty();
  }
  autosize();
  el.input.focus();
})();
