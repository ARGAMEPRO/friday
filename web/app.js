'use strict';

/* ============================ helpers ============================ */
const $ = (sel, root = document) => root.querySelector(sel);
const API = (path, options) => fetch(path, options);

const DEFAULTS = {
  voiceURI: '',
  rate: 1.03,
  pitch: 1.02,
  autoSpeak: true,
  wake: false,
  continuous: true,
  lang: 'ru-RU',
  neuralTts: true,
  ttsEngine: 'auto',
};

const state = {
  settings: loadSettings(),
  tasks: new Map(),
  announced: new Set(),
  tasksSignature: '',
  lastEvent: 0,
  firstState: true,
  listening: false,
  wakeActive: false,
  online: null,
  memoryJSON: '',
  recognition: null,
  booted: false,
  ttsAvailable: false,
  ttsEngine: '',
  localTts: '',
  localTtsLabel: '',
  platform: '',
  platformLabel: '',
  telegram: {},
  llm: null,
  audio: null,
  speaking: false,
  speechQueue: [],
  resumeAfterSpeech: false,
  feedbackTask: null,
  profile: { humor: 2, address: 'босс' },
};

function loadSettings() {
  try {
    return { ...DEFAULTS, ...JSON.parse(localStorage.getItem('friday.settings.v1') || '{}') };
  } catch {
    return { ...DEFAULTS };
  }
}

function saveSettings() {
  localStorage.setItem('friday.settings.v1', JSON.stringify(state.settings));
}

function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

function nowTime() {
  return new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function greeting() {
  const hour = new Date().getHours();
  const who = (state.profile && state.profile.address) || 'босс';
  if (hour >= 5 && hour < 12) return `Доброе утро, ${who}.`;
  if (hour >= 12 && hour < 18) return `Добрый день, ${who}.`;
  if (hour >= 18 && hour < 23) return `Добрый вечер, ${who}.`;
  return `Доброй ночи, ${who}.`;
}

function toast(message, ms = 5200) {
  const box = $('#toast');
  box.textContent = message;
  box.classList.add('show');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => box.classList.remove('show'), ms);
}

/* ============================ feed ============================ */
const feed = $('#feed');

function addMessage(role, text, options = {}) {
  const wrap = document.createElement('div');
  wrap.className = `msg msg-${role}`;
  const label = { user: 'ВЫ', friday: 'F.R.I.D.A.Y.', system: 'СИСТЕМА', error: 'ОШИБКА' }[role] || 'СИСТЕМА';
  const meta = document.createElement('div');
  meta.className = 'msg-meta';
  meta.innerHTML = `<b>${label}</b><span>${nowTime()}</span>`;
  wrap.appendChild(meta);

  const body = document.createElement('div');
  body.className = 'msg-text';
  body.textContent = text;
  wrap.appendChild(body);

  if (options.detail) {
    const detail = document.createElement('div');
    detail.className = 'msg-detail';
    detail.textContent = options.detail;
    wrap.appendChild(detail);
  }
  if (options.url) {
    const link = document.createElement('a');
    link.className = 'msg-link';
    link.href = options.url;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = 'Открыть ссылку ↗';
    wrap.appendChild(link);
  }
  if (options.items && options.items.length) {
    const list = document.createElement('div');
    list.className = 'msg-detail';
    options.items.forEach((item) => {
      const row = document.createElement('div');
      const status = item.exists === false ? ' · папка не найдена' : '';
      row.textContent = `${item.index}. ${item.name} — ${item.opened_ago || 'недавно'}${status}\n${item.path}`;
      list.appendChild(row);
    });
    wrap.appendChild(list);
  }

  feed.appendChild(wrap);
  feed.scrollTop = feed.scrollHeight;
  return wrap;
}

function pushFriday(task) {
  const data = task.data || {};
  addMessage(task.status === 'error' ? 'error' : 'friday', task.speech || task.detail || 'Готово.', {
    detail: data.items ? '' : task.detail && task.detail !== task.speech ? task.detail : '',
    url: data.url || '',
    items: data.items || null,
  });
}

/* ============================ speech ============================ */
function allVoices() {
  return window.speechSynthesis ? window.speechSynthesis.getVoices() : [];
}

function pickVoice(text) {
  const voices = allVoices();
  if (!voices.length) return null;
  const isRu = /[а-яё]/i.test(text);
  if (state.settings.voiceURI) {
    const chosen = voices.find((v) => v.voiceURI === state.settings.voiceURI);
    if (chosen && chosen.lang.toLowerCase().slice(0, 2) === (isRu ? 'ru' : 'en')) return chosen;
  }
  const pool = voices.filter((v) => v.lang.toLowerCase().startsWith(isRu ? 'ru' : 'en'));
  const prefs = isRu
    ? [/svetlana|светлана/i, /dariya|дария/i, /elena|елена/i, /irina|ирина/i, /google русский/i, /russian/i, /ru[-_]ru/i]
    : [/emily/i, /en[-_]ie/i, /libby/i, /sonia/i, /hazel/i, /susan/i, /google uk english female/i, /female/i, /en[-_]gb/i];
  for (const re of prefs) {
    const found = pool.find((v) => re.test(v.name) || re.test(v.lang));
    if (found) return found;
  }
  return pool[0] || voices.find((v) => v.default) || voices[0];
}

function voiceLabel() {
  if (state.ttsAvailable && state.settings.neuralTts) {
    if (state.localTtsLabel && state.ttsEngine.includes('edge')) return `FRIDAY · ${state.localTtsLabel} / Emily`;
    if (state.localTtsLabel) return `FRIDAY · ${state.localTtsLabel}`;
    if (state.ttsEngine === 'piper+edge-tts') return 'FRIDAY · Piper / Emily';
    if (state.ttsEngine === 'piper') return 'FRIDAY · Piper';
    return 'FRIDAY · Emily / Светлана';
  }
  const v = state.settings.voiceURI ? allVoices().find((x) => x.voiceURI === state.settings.voiceURI) : null;
  if (v) return `${v.name.replace(/\s*\(.*?\)\s*/g, ' ').trim()} · ${v.lang}`;
  const auto = pickVoice('Привет');
  return auto ? `АВТО · ${auto.name.split('(')[0].trim()}` : 'АВТО';
}

function normalizeSpeech(text) {
  return (text || '')
    .replace(/F\.?\s?R\.?\s?I\.?\s?D\.?\s?A\.?\s?Y\.?/gi, 'Фрайдэй')
    .replace(/\bИИ\b/g, 'искусственный интеллект');
}

function enqueueSpeech(text) {
  text = normalizeSpeech(text);
  if (!text || !state.settings.autoSpeak) return;
  state.speechQueue.push(text);
  drainSpeech();
}

function playBrowser(text) {
  return new Promise((resolve) => {
    if (!window.speechSynthesis) return resolve();
    const utter = new SpeechSynthesisUtterance(text);
    const voice = pickVoice(text);
    if (voice) utter.voice = voice;
    utter.lang = /[а-яё]/i.test(text) ? 'ru-RU' : (voice && voice.lang) || 'en-IE';
    utter.rate = Number(state.settings.rate) || 1.03;
    utter.pitch = Number(state.settings.pitch) || 1.02;
    utter.volume = 1;
    utter.onend = () => resolve();
    utter.onerror = () => resolve();
    window.speechSynthesis.speak(utter);
  });
}

function speechChunks(text, max = 170) {
  const sentences = (text.replace(/\s+/g, ' ').match(/[^.!?…]+[.!?…]?/g) || [text]);
  const chunks = [];
  let current = '';
  for (const sentence of sentences) {
    if ((current + ' ' + sentence).trim().length > max && current) {
      chunks.push(current.trim());
      current = sentence;
    } else {
      current += ' ' + sentence;
    }
  }
  if (current.trim()) chunks.push(current.trim());
  return chunks;
}

const speechCache = new Map();

function ttsUrl(text) {
  const params = new URLSearchParams({
    text,
    rate: String(state.settings.rate || 1.03),
    pitch: String(state.settings.pitch || 1.02),
  });
  return `/api/tts?${params.toString()}`;
}

async function fetchSpeech(text) {
  const cached = speechCache.get(text);
  if (cached) return cached;
  const response = await fetch(ttsUrl(text));
  if (!response.ok) throw new Error('tts');
  const url = URL.createObjectURL(await response.blob());
  speechCache.set(text, url);
  if (speechCache.size > 40) {
    const [oldest, oldestUrl] = speechCache.entries().next().value;
    speechCache.delete(oldest);
    URL.revokeObjectURL(oldestUrl);
  }
  return url;
}

async function speakChunk(text) {
  let src = speechCache.get(text);
  if (!src) src = await fetchSpeech(text).catch(() => null);
  return new Promise((resolve, reject) => {
    const audio = new Audio(src || ttsUrl(text));
    audio.preload = 'auto';
    state.audio = audio;
    audio.onended = () => { state.audio = null; resolve(); };
    audio.onerror = () => { state.audio = null; reject(new Error('tts')); };
    audio.play().catch(reject);
  });
}

async function playNeural(text) {
  // Speak sentence by sentence: the first words start ~0.4 s after the text
  // instead of waiting for the whole paragraph to render.
  const chunks = speechChunks(text.slice(0, 700));
  for (let index = 0; index < chunks.length; index += 1) {
    if (!state.speaking) return;
    fetchSpeech(chunks[index + 1]).catch(() => {});
    await speakChunk(chunks[index]);
  }
}

async function drainSpeech() {
  if (state.speaking || !state.speechQueue.length) return;
  state.speaking = true;
  if (state.listening) {
    state.resumeAfterSpeech = true;
    stopListening();
  }
  document.body.classList.add('speaking');
  const text = state.speechQueue.join(' ').trim();
  state.speechQueue = [];
  if (!text) {
    state.speaking = false;
    document.body.classList.remove('speaking');
    return;
  }
  try {
    if (state.ttsAvailable && state.settings.neuralTts) await playNeural(text);
    else await playBrowser(text);
  } catch {
    try { await playBrowser(text); } catch { /* ignore */ }
  }
  state.speaking = false;
  if (state.speechQueue.length) {
    drainSpeech();
  } else {
    document.body.classList.remove('speaking');
    if (state.resumeAfterSpeech && state.booted && !state.listening) {
      state.resumeAfterSpeech = false;
      setTimeout(() => startListening(false), 350);
    }
  }
}

function stopSpeech() {
  state.speechQueue = [];
  state.speaking = false;
  document.body.classList.remove('speaking');
  if (state.audio) {
    try { state.audio.pause(); } catch { /* ignore */ }
    state.audio = null;
  }
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  state.resumeAfterSpeech = false;
}

/* ============================ boot ============================ */
const BOOT_LINES = [
  'Инициализация ядра F.R.I.D.A.Y. ......... <b>OK</b>',
  'Загрузка памяти и правок ................ <b>OK</b>',
  'Нейромодуль голоса ..................... <b>OK</b>',
  'Связь с локальной моделью .............. <b>ПРОВЕРКА</b>',
  'Все системы готовы ..................... <b>OK</b>',
];

function runBoot() {
  const log = $('#bootLog');
  const button = $('#bootBtn');
  BOOT_LINES.forEach((line, index) => {
    setTimeout(() => {
      const row = document.createElement('div');
      row.innerHTML = line;
      log.appendChild(row);
      if (index === BOOT_LINES.length - 1) {
        button.disabled = false;
        button.innerHTML = 'АКТИВИРОВАТЬ';
      }
    }, 240 * index + 180);
  });
}

function activate() {
  $('#boot').classList.add('hidden');
  state.booted = true;
  addMessage('system', 'Соединение установлено. Локальный контур защищён.');
  addMessage('friday', `${greeting()} Пятница на связи.`);
  enqueueSpeech('Systems online. F.R.I.D.A.Y. at your service, boss.');
  poll();
  setInterval(poll, 900);
  setInterval(refreshLlm, 25000);
  refreshLlm();
}

/* ============================ server config ============================ */
async function loadServerConfig() {
  try {
    const data = await API('/api/settings').then((r) => r.json());
    state.ttsAvailable = Boolean(data.tts_available);
    state.ttsEngine = data.tts_engine || '';
    state.localTts = data.local_tts || '';
    state.localTtsLabel = data.local_tts_label || '';
    state.platform = data.platform || '';
    state.platformLabel = data.platform_label || '';
    if (data.profile) {
      state.profile = data.profile;
      if (data.profile.address) $('#addressInput').value = data.profile.address;
      if (data.profile.humor !== undefined) $('#humor').value = data.profile.humor;
    }
    const ttsConfig = (data.settings && data.settings.tts) || {};
    if (ttsConfig.engine) {
      state.settings.ttsEngine = ttsConfig.engine;
      const select = $('#ttsEngine');
      if (select) select.value = ttsConfig.engine;
    }
    const telegram = (data.settings && data.settings.telegram) || {};
    state.telegram = telegram;
    if ($('#tgToken')) {
      $('#tgToken').placeholder = telegram.token_set ? `сохранён: ${telegram.token_hint}` : '123456789:AA…';
      $('#tgToken').value = '';
    }
    if ($('#tgAllow')) {
      $('#tgAllow').value = (telegram.allow_ids || []).join(', ');
    }
    if ($('#tgEnabled')) {
      $('#tgEnabled').checked = Boolean(telegram.enabled);
    }
    $('#ttsState').textContent = state.ttsAvailable ? (state.ttsEngine || 'готов') : 'не установлен';
    $('#ttsState').className = state.ttsAvailable ? 'ok' : 'bad';
    $('#voiceLabel').textContent = voiceLabel();
    $('#platformInfo').textContent = state.platformLabel
      ? `Платформа: ${state.platformLabel}. Локальный голос: ${state.localTtsLabel || 'браузерный'}.`
      : 'платформа не определена';
    const count = data.correction_count || 0;
    $('#memHint').textContent = count ? `${count} правок · память` : 'правки · предпочтения';
    refreshTelegram();
  } catch {
    $('#ttsState').textContent = 'браузерный голос';
  }
}

async function refreshLlm() {
  try {
    const status = await API('/api/llm/status').then((r) => r.json());
    state.llm = status;
    const pill = $('#llmPill');
    pill.classList.toggle('warn', !status.online);
    $('#llmText').textContent = status.online ? `LLM · ${(status.model || status.provider).slice(0, 18)}` : 'LLM OFFLINE';
    $('#llmStatus').textContent = status.online
      ? `Онлайн · ${status.provider} · ${status.model || 'модель найдена'}`
      : 'Офлайн: модель не отвечает. Запусти Ollama или внешний API.';
    $('#llmStatus').className = `llm-status ${status.online ? 'online' : 'offline'}`;
  } catch {
    $('#llmText').textContent = 'LLM —';
  }
}

/* ============================ polling ============================ */
async function poll() {
  try {
    const res = await API(`/api/state?since=${state.lastEvent}`);
    if (!res.ok) throw new Error('bad status');
    applyState(await res.json());
    setOnline(true);
  } catch {
    setOnline(false);
  }
}

function setOnline(online) {
  if (state.online === online) return;
  state.online = online;
  $('#netPill').classList.toggle('offline', !online);
  $('#netText').textContent = online ? 'ONLINE' : 'OFFLINE';
}

async function warmSpeech(text) {
  if (!state.ttsAvailable || !state.settings.neuralTts || !state.settings.autoSpeak) return;
  const chunks = speechChunks(text);
  if (!chunks.length) return;
  try { await fetchSpeech(chunks[0]); } catch { return; }
  for (const chunk of chunks.slice(1, 3)) {
    fetchSpeech(chunk).catch(() => {});
  }
}

async function applyState(data) {
  for (const ev of data.events || []) {
    if (ev.id <= state.lastEvent) continue;
    state.lastEvent = ev.id;
    if (ev.type === 'user') {
      addMessage('user', ev.speech);
      continue;
    }
    if (ev.type === 'reminder') {
      addMessage('friday', ev.speech);
      enqueueSpeech(ev.speech);
      toast(ev.speech);
    }
  }

  const completed = [];
  for (const task of data.tasks || []) {
    state.tasks.set(task.id, task);
  }
  for (const task of state.tasks.values()) {
    if ((task.status === 'done' || task.status === 'error') && !state.announced.has(task.id)) {
      state.announced.add(task.id);
      if (!state.firstState) completed.push(task);
    }
  }
  state.firstState = false;

  if (state.tasks.size > 60) {
    [...state.tasks.values()]
      .sort((a, b) => a.created - b.created)
      .slice(0, state.tasks.size - 60)
      .forEach((task) => state.tasks.delete(task.id));
  }

  renderTasks();
  renderMemory(data.memory);
  updateCounters(data);

  completed.sort((a, b) => a.created - b.created);
  const spoken = completed.map((task) => task.speech).filter(Boolean).join(' ').trim();
  if (spoken) {
    // Let the first sentence finish buffering, then show the text and start
    // the voice together instead of printing text seconds before audio.
    await warmSpeech(spoken);
    stopSpeech();
    for (const task of completed) pushFriday(task);
    enqueueSpeech(spoken);
  } else {
    for (const task of completed) pushFriday(task);
  }
}

function updateCounters(data) {
  const running = (data.tasks || []).filter((t) => t.status === 'running' || t.status === 'queued').length;
  $('#taskCount').textContent = String(state.tasks.size);
  $('#tasksBadge').textContent = String(running || state.tasks.size);
  document.body.classList.toggle('thinking', running > 0);
  if (running > 0) setCoreState('ВЫПОЛНЯЮ');
  else if (state.listening) setCoreState('СЛУШАЮ');
  else setCoreState('ОЖИДАНИЕ');
}

/* ============================ tasks ============================ */
function renderTasks() {
  const container = $('#tasks');
  const list = [...state.tasks.values()].sort((a, b) => b.created - a.created).slice(0, 14);
  const signature = list
    .map((task) => `${task.id}:${task.status}:${task.label}:${task.speech}:${task.error || ''}`)
    .join('|');
  if (signature === state.tasksSignature) return;
  state.tasksSignature = signature;
  if (!list.length) {
    container.innerHTML = '<div class="empty">Пока пусто. Отдай команду — задачи появятся здесь и выполнятся параллельно.</div>';
    return;
  }
  container.innerHTML = '';
  for (const task of list) {
    const row = document.createElement('article');
    row.className = 'task';
    row.dataset.status = task.status;

    const dot = document.createElement('span');
    dot.className = 'dot';

    const body = document.createElement('div');
    const label = document.createElement('div');
    label.className = 'task-label';
    label.textContent = task.label || task.text;
    body.appendChild(label);

    const sub = document.createElement('div');
    sub.className = 'task-sub';
    const statusText = { queued: 'в очереди', running: 'выполняется', done: 'готово', error: 'ошибка' }[task.status] || task.status;
    sub.textContent = statusText;
    body.appendChild(sub);

    const time = document.createElement('span');
    time.className = 'task-time';
    time.textContent = task.status === 'done' || task.status === 'error' ? `${task.duration}s` : '…';

    row.append(dot, body, time);

    if (task.status === 'done' || task.status === 'error') {
      const edit = document.createElement('button');
      edit.className = 'edit-task';
      edit.title = 'Поправить: я запомню';
      edit.innerHTML = '<svg viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16v4Z"/><path d="M14 6l4 4"/></svg>';
      edit.addEventListener('click', (event) => {
        event.stopPropagation();
        openFeedback(task);
      });
      row.appendChild(edit);
    }

    container.appendChild(row);
  }
}

/* ============================ memory ============================ */
function renderMemory(memory) {
  if (!memory) return;
  const signature = JSON.stringify(memory);
  if (signature === state.memoryJSON) return;
  state.memoryJSON = signature;

  const box = $('#memory');
  const corrections = (memory.corrections || []).slice(-3).reverse();
  const groups = [
    ['ИМЯ', 'name', memory.name ? [memory.name] : []],
    ['ЛЮБИТ', 'likes', memory.likes || []],
    ['НЕ ЛЮБИТ', 'dislikes', memory.dislikes || []],
    ['ФАКТЫ', 'facts', memory.facts || []],
    ['ССЫЛКИ', 'aliases', Object.keys(memory.aliases || {})],
    ['ПРАВКИ', 'corrections', corrections.map((c) => `«${c.original}» → «${c.right}»`)],
  ];
  const total = (memory.likes || []).length + (memory.dislikes || []).length + (memory.facts || []).length;
  $('#memCount').textContent = String(total);

  box.innerHTML = '';
  let shown = 0;
  for (const [title, kind, items] of groups) {
    if (!items.length) continue;
    shown += items.length;
    const group = document.createElement('div');
    group.className = 'mem-group';
    const heading = document.createElement('div');
    heading.className = 'mem-title';
    heading.textContent = title;
    group.appendChild(heading);
    for (const item of items) {
      const chip = document.createElement('div');
      chip.className = `mem-chip ${kind === 'likes' ? 'like' : kind === 'dislikes' ? 'dislike' : ''}`;
      chip.textContent = item;
      group.appendChild(chip);
    }
    box.appendChild(group);
  }
  if (!shown) {
    box.innerHTML = '<div class="empty">Скажи «запомни, что мне нравится ...» — и я не забуду. А если ошибусь, нажми ✎ на задаче.</div>';
  }
}

/* ============================ command ============================ */
async function sendCommand(text) {
  const command = (text || '').trim();
  if (!command) return;
  addMessage('user', command);
  $('#commandInput').value = '';
  stopSpeech();
  try {
    const res = await API('/api/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: command }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    for (const task of data.tasks || []) {
      state.tasks.set(task.id, task);
      state.announced.delete(task.id);
    }
    renderTasks();
    poll();
  } catch {
    addMessage('error', 'Не смогла отправить команду серверу. Проверь, что server.py запущен.');
  }
}

/* ============================ equalizer ============================ */
let eqBars = [];
let eqPhase = 0;

function makeEqualizer() {
  const box = $('#equalizer');
  box.innerHTML = '';
  eqBars = [];
  for (let i = 0; i < 26; i += 1) {
    const bar = document.createElement('span');
    box.appendChild(bar);
    eqBars.push(bar);
  }
  animateEqualizer();
}

function animateEqualizer() {
  eqPhase += 0.09;
  const active = document.body.classList.contains('listening') || document.body.classList.contains('speaking');
  eqBars.forEach((bar, i) => {
    if (!active) {
      bar.style.height = `${3 + Math.abs(Math.sin(eqPhase * 0.5 + i * 0.3)) * 5}px`;
      return;
    }
    const wave = Math.sin(eqPhase * 2 + i * 0.55);
    const noise = Math.sin(eqPhase * 5.3 + i * 1.7) * 0.35;
    bar.style.height = `${(8 + Math.abs(wave + noise) * 26).toFixed(1)}px`;
  });
  requestAnimationFrame(animateEqualizer);
}

/* ============================ microphone ============================ */
function setupRecognition() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const hint = $('#micHint');
  if (!Recognition) {
    $('#micBtn').disabled = true;
    hint.textContent = 'Голос: Chrome / Edge / Safari';
    return;
  }
  const recognition = new Recognition();
  recognition.lang = state.settings.lang || 'ru-RU';
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.maxAlternatives = 1;

  recognition.onstart = () => {
    setListening(true);
  };
  recognition.onerror = (event) => {
    if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
      hint.textContent = 'Нет доступа к микрофону';
      toast('Разреши микрофон в настройках браузера');
      stopListening();
    } else if (event.error === 'no-speech') {
      hint.textContent = state.settings.wake && !state.wakeActive ? 'Скажи «Пятница»…' : 'Слушаю…';
    }
  };
  recognition.onresult = (event) => {
    let interim = '';
    let finalText = '';
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      const result = event.results[i];
      if (result.isFinal) finalText += result[0].transcript;
      else interim += result[0].transcript;
    }
    const input = $('#commandInput');
    if (interim) input.value = interim.trim();
    if (!finalText) return;

    let phrase = finalText.trim();
    if (!phrase) return;

    const wakeMatch = phrase.match(/^(?:эй[,\s]+)?(?:пятница|friday)[,!\s]*(.*)$/i);
    if (wakeMatch) {
      state.wakeActive = true;
      phrase = wakeMatch[1].trim();
    } else if (state.settings.wake && !state.wakeActive) {
      // Не стираем текст: пусть человек видит, что было услышано.
      input.value = phrase;
      hint.textContent = 'Нужно слово «Пятница»';
      return;
    }

    if (!phrase) {
      input.value = '';
      enqueueSpeech('Слушаю, босс.');
      return;
    }

    input.value = '';
    sendCommand(phrase);
    if (!state.settings.continuous) stopListening();
  };
  recognition.onend = () => {
    if (state.listening && state.settings.continuous) {
      setTimeout(() => {
        try { recognition.start(); } catch { /* already started */ }
      }, 350);
    } else {
      state.wakeActive = false;
      setListening(false);
      hint.textContent = 'Нажми и говори';
    }
  };
  state.recognition = recognition;
}

function startListening(manual = false) {
  if (!state.recognition) {
    toast('Распознавание речи доступно в Chrome, Edge или Safari');
    return;
  }
  state.wakeActive = manual || !state.settings.wake;
  try {
    state.recognition.lang = state.settings.lang || 'ru-RU';
    state.recognition.start();
    setListening(true);
  } catch {
    setTimeout(() => {
      try {
        state.recognition.start();
        setListening(true);
      } catch { /* already running */ }
    }, 400);
  }
}

function stopListening() {
  if (!state.recognition) return;
  state.wakeActive = false;
  try { state.recognition.stop(); } catch { /* ignore */ }
  setListening(false);
}

function setListening(value) {
  state.listening = value;
  document.body.classList.toggle('listening', value);
  const waitingForWake = value && state.settings.wake && !state.wakeActive;
  $('#micHint').textContent = value
    ? (waitingForWake ? 'Скажи «Пятница»…' : 'Слушаю…')
    : 'Нажми и говори';
  setCoreState(value ? 'СЛУШАЮ' : 'ОЖИДАНИЕ');
  $('#commandInput').placeholder = value
    ? (waitingForWake ? 'Скажи «Пятница, …»' : 'Говори…')
    : 'Скажи или напиши: «запусти Яндекс Музыку и открой Решу ЕГЭ»';
}

function setCoreState(text) {
  $('#coreState').textContent = text;
}

/* ======================== ollama model setup ===================== */
async function refreshLlmSetup() {
  try {
    const data = await API('/api/llm/setup').then((r) => r.json());
    state.llmSetup = data;
    renderLlmSetup(data);
    if (data.pull && data.pull.running) pollLlmSetup();
  } catch (err) {
    const el = $('#llmSetup');
    if (el) el.textContent = 'не удалось проверить';
  }
}

function renderLlmSetup(data) {
  const el = $('#llmSetup');
  const button = $('#llmInstall');
  if (!el) return;
  const pull = data.pull || {};
  if (pull.running) {
    el.textContent = `загрузка ${pull.percent || 0}% · ${pull.status || 'модель'}`;
    el.className = 'llm-status';
    if (button) button.disabled = true;
    return;
  }
  if (button) button.disabled = false;
  if (data.installed) {
    el.textContent = `модель: ${(data.models || []).join(', ') || 'готово'}`;
    el.className = 'llm-status ok';
  } else {
    el.textContent = `модель не установлена (${data.suggested || 'qwen2.5:7b'})`;
    el.className = 'llm-status bad';
  }
  if (pull.ok) {
    el.textContent = 'модель установлена';
    el.className = 'llm-status ok';
  }
}

let llmTimer = null;
function pollLlmSetup() {
  clearInterval(llmTimer);
  llmTimer = setInterval(async () => {
    try {
      const data = await API('/api/llm/setup').then((r) => r.json());
      state.llmSetup = data;
      renderLlmSetup(data);
      if (!data.pull || !data.pull.running) clearInterval(llmTimer);
    } catch (err) { clearInterval(llmTimer); }
  }, 2000);
}

async function installModel() {
  const model = ($('#llmModel').value || '').trim() || state.llmSetup?.suggested || 'qwen2.5:7b';
  const el = $('#llmSetup');
  if (el) { el.textContent = 'запускаю загрузку…'; el.className = 'llm-status'; }
  try {
    await API('/api/llm/setup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'pull', model }),
    });
    pollLlmSetup();
  } catch (err) {
    if (el) { el.textContent = 'не удалось начать загрузку'; el.className = 'llm-status bad'; }
  }
}

/* ============================ settings ============================ */
async function openSettings() {
  const select = $('#voiceSelect');
  select.innerHTML = '';
  const auto = document.createElement('option');
  auto.value = '';
  auto.textContent = 'Автоматически (женский голос под язык)';
  select.appendChild(auto);
  const voices = allVoices().slice().sort((a, b) => a.lang.localeCompare(b.lang));
  for (const voice of voices) {
    const option = document.createElement('option');
    option.value = voice.voiceURI;
    option.textContent = `${voice.name} — ${voice.lang}${voice.default ? ' · по умолчанию' : ''}`;
    select.appendChild(option);
  }
  select.value = state.settings.voiceURI || '';
  $('#rate').value = state.settings.rate;
  $('#pitch').value = state.settings.pitch;
  $('#rateVal').textContent = Number(state.settings.rate).toFixed(2);
  $('#pitchVal').textContent = Number(state.settings.pitch).toFixed(2);
  $('#neuralTts').checked = state.settings.neuralTts;
  $('#ttsEngine').value = state.settings.ttsEngine || 'auto';
  $('#autoSpeak').checked = state.settings.autoSpeak;
  $('#wakeWord').checked = state.settings.wake;
  $('#continuous').checked = state.settings.continuous;
  $('#langSelect').value = state.settings.lang;
  $('#humor').value = (state.profile && state.profile.humor) ?? 2;
  $('#humorVal').textContent = $('#humor').value;
  $('#addressInput').value = (state.profile && state.profile.address) || 'босс';
  $('#ttsState').textContent = state.ttsAvailable ? 'edge-tts готов' : 'не установлен';
  $('#ttsState').className = state.ttsAvailable ? 'ok' : 'bad';

  try {
    const data = await API('/api/settings').then((r) => r.json());
    const llm = data.settings.llm || {};
    $('#llmProvider').value = llm.provider || 'auto';
    $('#llmBase').value = llm.base_url || 'http://127.0.0.1:11434';
    $('#llmModel').value = llm.model || '';
    $('#llmKey').value = '';
    $('#llmKey').placeholder = llm.api_key_set ? 'ключ сохранён' : 'не задан';
    refreshLlmSetup();
    $('#ttsState').textContent = data.tts_engine || 'не установлен';
    $('#ttsState').className = data.tts_available ? 'ok' : 'bad';
    $('#platformInfo').textContent = `${data.platform_label || 'неизвестно'} · локальный голос: ${data.local_tts_label || 'браузерный'}`;
    const telegram = data.settings.telegram || {};
    if ($('#tgToken')) {
      $('#tgToken').value = '';
      $('#tgToken').placeholder = telegram.token_set ? `сохранён: ${telegram.token_hint}` : '123456789:AA…';
      $('#tgAllow').value = (telegram.allow_ids || []).join(', ');
      $('#tgEnabled').checked = Boolean(telegram.enabled);
    }
    const corrections = data.corrections || [];
    const count = data.correction_count || 0;
    $('#correctionsInfo').textContent = count
      ? `Сохранено правок: ${count}. Последняя: «${(corrections[corrections.length - 1] || {}).right || ''}»`
      : 'Пока правок нет. Скажи «не так, я имела в виду ...» или нажми ✎ на задаче.';
  } catch { /* server may be down */ }

  refreshTelegram();
  refreshLlm();
  $('#modal').classList.add('open');
  $('#modal').setAttribute('aria-hidden', 'false');
}

/* ============================ telegram ============================ */
function renderTelegram(bot, telegramSettings) {
  const status = $('#tgStatus');
  if (!status) return;
  const configured = telegramSettings ? telegramSettings.token_set : false;
  if (bot && bot.running) {
    status.textContent = `онлайн · @${bot.username || 'бот'}`;
    status.className = 'llm-status online';
  } else if (bot && bot.error) {
    status.textContent = `ошибка: ${bot.error}`;
    status.className = 'llm-status offline';
  } else if (configured || (bot && bot.configured)) {
    status.textContent = 'настроен, но не запущен';
    status.className = 'llm-status';
  } else {
    status.textContent = 'не настроен';
    status.className = 'llm-status offline';
  }
  const stateEl = $('#tgState');
  if (stateEl) {
    stateEl.textContent = bot && bot.running ? 'работает' : 'выключен';
    stateEl.className = bot && bot.running ? 'ok' : 'bad';
  }
}

async function refreshTelegram() {
  try {
    const data = await API('/api/telegram').then((r) => r.json());
    renderTelegram(data.bot, data.settings);
  } catch { /* ignore */ }
}

async function telegramAction(action) {
  const payload = { action };
  if (action === 'start' || action === 'save') {
    const token = $('#tgToken').value.trim();
    if (token) payload.token = token;
    payload.allow_ids = $('#tgAllow').value;
    payload.enabled = $('#tgEnabled').checked;
    if (action === 'start' && !token && !(state.telegram && state.telegram.token_set)) {
      toast('Сначала вставь токен от @BotFather');
      return;
    }
  }
  try {
    const res = await API('/api/telegram', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then((r) => r.json());
    if (res.ok) {
      $('#tgToken').value = '';
      toast(action === 'stop' ? 'Бот остановлен.' : 'Бот запущен. Напиши ему /start.');
    } else {
      toast(`Telegram: ${res.error || 'не удалось'}`);
    }
    renderTelegram(res.bot || res.status, (res.settings || state.telegram));
  } catch {
    toast('Сервер не отвечает — бот не запущен.');
  }
}

function closeSettings() {
  $('#modal').classList.remove('open');
  $('#modal').setAttribute('aria-hidden', 'true');
}

async function applySettings() {
  state.settings.voiceURI = $('#voiceSelect').value;
  state.settings.rate = Number($('#rate').value);
  state.settings.pitch = Number($('#pitch').value);
  state.settings.neuralTts = $('#neuralTts').checked;
  state.settings.ttsEngine = $('#ttsEngine').value;
  state.settings.autoSpeak = $('#autoSpeak').checked;
  state.settings.wake = $('#wakeWord').checked;
  state.settings.continuous = $('#continuous').checked;
  state.settings.lang = $('#langSelect').value;
  saveSettings();

  if (state.recognition) state.recognition.lang = state.settings.lang;
  $('#voiceLabel').textContent = voiceLabel();
  $('#modeLabel').textContent = state.settings.wake ? 'ПО СЛОВУ' : 'РУЧНОЙ';

  const llmValues = {
    provider: $('#llmProvider').value,
    base_url: $('#llmBase').value.trim(),
    model: $('#llmModel').value.trim(),
  };
  if ($('#llmKey').value.trim()) llmValues.api_key = $('#llmKey').value.trim();
  const profileValues = { humor: Number($('#humor').value), address: $('#addressInput').value.trim() || 'босс' };

  try {
    await API('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ section: 'tts', values: { engine: state.settings.ttsEngine } }),
    });
    await API('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ section: 'llm', values: llmValues }),
    });
    await API('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ section: 'profile', values: profileValues }),
    });
    if ($('#tgAllow')) {
      const telegramValues = {
        enabled: $('#tgEnabled').checked,
        allow_ids: $('#tgAllow').value,
      };
      const token = $('#tgToken').value.trim();
      if (token) telegramValues.token = token;
      await API('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ section: 'telegram', values: telegramValues }),
      });
    }
    state.profile = { ...(state.profile || {}), ...profileValues };
    toast('Настройки сохранены. Учла характер.');
    refreshLlm();
  } catch {
    toast('Сервер не отвечает — настройки сохранены локально.');
  }
  closeSettings();
  loadServerConfig();
  refreshLlmSetup();
}

async function checkLlm() {
  $('#llmStatus').textContent = 'Проверяю…';
  try {
    const status = await API('/api/llm/status?force=1').then((r) => r.json());
    state.llm = status;
    $('#llmStatus').textContent = status.online
      ? `Онлайн · ${status.provider} · ${status.model || 'ок'}`
      : 'Офлайн: модель не отвечает';
    $('#llmStatus').className = `llm-status ${status.online ? 'online' : 'offline'}`;
    refreshLlm();
  } catch {
    $('#llmStatus').textContent = 'Сервер не отвечает';
  }
}

async function startOllama() {
  toast('Пробую запустить Ollama…');
  try {
    const res = await API('/api/llm/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then((r) => r.json());
    if (!res.ok) toast(`Ollama: ${res.error || 'не удалось'}`);
    setTimeout(checkLlm, 1200);
  } catch {
    toast('Не удалось запустить Ollama');
  }
}

/* ============================ feedback ============================ */
function openFeedback(task) {
  state.feedbackTask = task;
  $('#feedbackTask').textContent = task ? `Задача: ${task.label || task.text}` : 'Что должно было быть?';
  $('#feedbackText').value = '';
  $('#feedbackModal').classList.add('open');
  setTimeout(() => $('#feedbackText').focus(), 80);
}

function closeFeedback() {
  $('#feedbackModal').classList.remove('open');
  state.feedbackTask = null;
}

async function saveFeedback() {
  const correction = $('#feedbackText').value.trim();
  if (!correction) {
    toast('Напиши, как правильно — я запомню.');
    return;
  }
  const task = state.feedbackTask;
  try {
    const res = await API('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: task ? task.id : '', correction, source: 'ui' }),
    }).then((r) => r.json());
    if (res.ok) {
      toast('Записала правку в память.');
      addMessage('system', `Правка сохранена: «${correction}»`);
      closeFeedback();
      poll();
    } else {
      toast('Не получилось сохранить правку.');
    }
  } catch {
    toast('Сервер не отвечает — правка не сохранена.');
  }
}

/* ============================ calibration ============================ */
let calibMarked = 0;

async function openCalibration() {
  calibMarked = 0;
  $('#calibCount').textContent = '0 отмечено';
  const list = $('#calibList');
  list.innerHTML = '<div class="empty">Загружаю сценарии…</div>';
  $('#calibModal').classList.add('open');
  try {
    const data = await API('/api/calibration').then((r) => r.json());
    list.innerHTML = '';
    (data.scenarios || []).forEach((scenario) => list.appendChild(calibRow(scenario)));
  } catch {
    list.innerHTML = '<div class="empty">Сервер недоступен.</div>';
  }
}

function calibRow(scenario) {
  const row = document.createElement('div');
  row.className = 'calib-row';
  row.dataset.id = scenario.id;

  const title = document.createElement('div');
  title.className = 'calib-title';
  title.textContent = scenario.title;
  row.appendChild(title);

  const input = document.createElement('input');
  input.type = 'text';
  input.value = scenario.command;
  row.appendChild(input);

  const actions = document.createElement('div');
  actions.className = 'calib-actions';
  const run = document.createElement('button');
  run.className = 'ghost-btn';
  run.textContent = '▶';
  run.title = 'Выполнить';
  run.addEventListener('click', () => sendCommand(input.value));
  const ok = document.createElement('button');
  ok.className = 'ghost-btn';
  ok.textContent = '✓';
  ok.title = 'Верно';
  ok.addEventListener('click', () => {
    if (!row.classList.contains('right') && !row.classList.contains('saved')) {
      calibMarked += 1;
      $('#calibCount').textContent = `${calibMarked} отмечено`;
    }
    row.classList.add('right');
    row.classList.remove('wrong');
  });
  const bad = document.createElement('button');
  bad.className = 'ghost-btn';
  bad.textContent = '✗';
  bad.title = 'Не так — поправить';
  bad.addEventListener('click', () => {
    row.classList.add('wrong');
    row.classList.remove('right');
    const fix = row.querySelector('.calib-fix textarea');
    if (fix) fix.focus();
  });
  actions.append(run, ok, bad);
  row.appendChild(actions);

  const fix = document.createElement('div');
  fix.className = 'calib-fix';
  const area = document.createElement('textarea');
  area.rows = 2;
  area.placeholder = 'Как правильно? Например: открой в браузере Решу ЕГЭ';
  const save = document.createElement('button');
  save.className = 'ghost-btn accent';
  save.textContent = 'Запомнить';
  save.addEventListener('click', async () => {
    const correction = area.value.trim();
    if (!correction) {
      toast('Напиши правильную формулировку.');
      area.focus();
      return;
    }
    try {
      await API('/api/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ correction, original: input.value, source: 'calibration' }),
      });
      row.classList.add('saved');
      row.classList.remove('wrong');
      calibMarked += 1;
      $('#calibCount').textContent = `${calibMarked} отмечено`;
    } catch {
      toast('Не получилось сохранить.');
    }
  });
  fix.append(area, save);
  row.appendChild(fix);

  const flag = document.createElement('div');
  flag.className = 'calib-flag';
  flag.textContent = 'Правка сохранена — учту.';
  row.appendChild(flag);

  return row;
}

/* ============================ init ============================ */
const CHIPS = [
  ['Поговорим', 'поговорим'],
  ['Запусти музыку', 'запусти яндекс музыку'],
  ['Решу ЕГЭ', 'в браузере открой сайт решу егэ'],
  ['Папки VS Code', 'какие папки я последний раз открывал в vs code'],
  ['Статус', 'статус системы'],
  ['Что умеешь', 'что ты умеешь'],
  ['Запомни: люблю рок', 'запомни, что мне нравится рок'],
];

function buildChips() {
  const box = $('#chips');
  for (const [title, command] of CHIPS) {
    const chip = document.createElement('button');
    chip.className = 'chip';
    chip.type = 'button';
    chip.textContent = title;
    chip.title = command;
    chip.addEventListener('click', () => sendCommand(command));
    box.appendChild(chip);
  }
}

function tickClock() {
  $('#clock').textContent = nowTime();
}

function init() {
  buildChips();
  makeEqualizer();
  setupRecognition();
  runBoot();
  tickClock();
  setInterval(tickClock, 1000);
  $('#voiceLabel').textContent = voiceLabel();
  loadServerConfig();
  refreshLlmSetup();

  if (window.speechSynthesis) {
    window.speechSynthesis.onvoiceschanged = () => {
      $('#voiceLabel').textContent = voiceLabel();
    };
  }

  $('#bootBtn').addEventListener('click', activate);
  $('#composer').addEventListener('submit', (event) => {
    event.preventDefault();
    sendCommand($('#commandInput').value);
  });
  $('#micBtn').addEventListener('click', () => {
    if (state.speaking) {
      stopSpeech();
      return;
    }
    if (state.listening) stopListening();
    else startListening(true);
  });
  $('#settingsBtn').addEventListener('click', openSettings);
  $('#calibBtn').addEventListener('click', openCalibration);
  $('#calibChip').addEventListener('click', openCalibration);
  $('#closeModal').addEventListener('click', closeSettings);
  $('#saveSettings').addEventListener('click', applySettings);
  $('#testVoice').addEventListener('click', () => enqueueSpeech('Пятница на связи, босс. Все системы в норме.'));
  $('#llmCheck').addEventListener('click', checkLlm);
  $('#llmInstall').addEventListener('click', installModel);
  $('#llmStart').addEventListener('click', startOllama);
  if ($('#tgStart')) $('#tgStart').addEventListener('click', () => telegramAction('start'));
  if ($('#tgStop')) $('#tgStop').addEventListener('click', () => telegramAction('stop'));
  $('#rate').addEventListener('input', () => { $('#rateVal').textContent = Number($('#rate').value).toFixed(2); });
  $('#pitch').addEventListener('input', () => { $('#pitchVal').textContent = Number($('#pitch').value).toFixed(2); });
  $('#humor').addEventListener('input', () => { $('#humorVal').textContent = $('#humor').value; });
  $('#clearFeed').addEventListener('click', () => { feed.innerHTML = ''; });

  $('#closeFeedback').addEventListener('click', closeFeedback);
  $('#cancelFeedback').addEventListener('click', closeFeedback);
  $('#saveFeedback').addEventListener('click', saveFeedback);
  $('#closeCalib').addEventListener('click', () => $('#calibModal').classList.remove('open'));
  $('#calibDone').addEventListener('click', () => $('#calibModal').classList.remove('open'));

  for (const id of ['#modal', '#feedbackModal', '#calibModal']) {
    $(id).addEventListener('click', (event) => {
      if (event.target === $(id)) {
        $(id).classList.remove('open');
      }
    });
  }

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeSettings();
      closeFeedback();
      $('#calibModal').classList.remove('open');
    }
    if (event.code === 'Space' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      if (state.listening) stopListening();
      else startListening(true);
    }
  });

  document.addEventListener('visibilitychange', () => {
    if (document.hidden && state.listening) stopListening();
  });
}

init();
