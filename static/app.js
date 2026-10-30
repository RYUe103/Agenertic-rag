const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const appBasePath = new URL('.', document.currentScript.src).pathname.replace(/\/$/, '');
const appUrl = path => `${appBasePath}${path.startsWith('/') ? path : `/${path}`}`;
const sourceUrl = value => {
  if (!value) return '';
  const url = new URL(value, window.location.origin);
  if (url.origin === window.location.origin && url.pathname.startsWith('/api/')) {
    url.pathname = appUrl(url.pathname);
  }
  return url.href;
};
const traceLabels = {route:'路由', decision:'决策', tool:'工具', evaluation:'评分', rewrite:'改写', generation:'生成', verification:'校验', stop:'停止', observation:'观察'};
let agents = [];
let activeAgentId = '';
let knowledgeBases = [];
let agentConfigs = [];
let modelConnections = [];
let editingKnowledgeBaseId = '';
let editingAgentId = '';
let editingModelConnectionId = '';
let glossaryDraft = [];
let editingSourceId = '';
let previewSourceId = '';
let currentIngestJobId = '';
let ingestPollVersion = 0;
let metadataFieldDraft = [];
let sourcePage = 1;
let sourcePageSize = 30;
let sourceTotalPages = 1;
let markdownPreviewSourceId = '';
let markdownPreviewText = '';
let clearKnowledgeBaseName = '';
let activeDeleteJobId = '';
let deletePollVersion = 0;
const defaultQwenShortPrompt = `你会一次看到完整 PDF 的全部页面。阅读所有页面并直接生成最终 Markdown。
按页面顺序理解全文，保留标题层级、日期、编号、人员、金额、术语、决议、待办和表格。
不得遗漏或猜测。无法辨认的内容标记为 [无法辨认]。只输出 Markdown。`;
const defaultQwenLongPrompt = `你正在处理长 PDF 的第 {{page_start}} 至 {{page_end}} 页。
只输出当前批次可直接合并的 Markdown 片段，不重复生成整份文档的总结、封面或目录。
保留标题层级、编号、日期、人员、金额、术语和表格，并衔接上下文提示。
无法辨认的内容标记为 [无法辨认]。只输出 Markdown。`;
let conversationId = localStorage.getItem('agentic-rag-conversation-id') || (crypto.randomUUID ? crypto.randomUUID() : `conv_${Date.now()}`);
localStorage.setItem('agentic-rag-conversation-id', conversationId);

function submitButton(event) {
  return event.submitter || event.currentTarget.querySelector('button[type="submit"]');
}

function safeLink(value) {
  try {
    const url = new URL(sourceUrl(value.replace(/^<|>$/g, '')));
    return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
  } catch { return ''; }
}

function appendInlineMarkdown(parent, text) {
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|~~[^~]+~~|\[(?:\\.|[^\]])+\]\((?:<[^>]+>|[^)]+)\))/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(cursor, match.index)));
    const token = match[0];
    if (token.startsWith('`')) {
      const code = document.createElement('code'); code.textContent = token.slice(1, -1); parent.append(code);
    } else if (token.startsWith('**')) {
      const strong = document.createElement('strong'); strong.textContent = token.slice(2, -2); parent.append(strong);
    } else if (token.startsWith('~~')) {
      const del = document.createElement('del'); del.textContent = token.slice(2, -2); parent.append(del);
    } else {
      const parts = token.match(/^\[((?:\\.|[^\]])+)\]\((.*)\)$/);
      const href = parts ? safeLink(parts[2]) : '';
      if (parts && href) {
        const link = document.createElement('a'); link.textContent = parts[1].replace(/\\([\\[\]])/g, '$1'); link.href = href;
        link.target = '_blank'; link.rel = 'noopener'; parent.append(link);
      } else parent.append(document.createTextNode(parts?.[1] || token));
    }
    cursor = match.index + token.length;
  }
  parent.append(document.createTextNode(text.slice(cursor)));
}

function markdownCells(line) {
  return line.trim().replace(/^\||\|$/g, '').split('|').map(value => value.trim());
}

function renderMarkdown(markdown, container) {
  const lines = markdown.replace(/\r\n?/g, '\n').split('\n');
  const isTableDivider = line => /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line || '');
  const startsBlock = (line, next) => !line.trim() || /^#{1,6}\s|^```|^>\s?|^[-*+]\s+|^\d+[.)]\s+|^---+$/.test(line.trim()) || isTableDivider(next);
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }
    const fence = line.match(/^```([^\s]*)\s*$/);
    if (fence) {
      const values = []; index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index])) values.push(lines[index++]);
      if (index < lines.length) index += 1;
      const pre = document.createElement('pre'); const code = document.createElement('code');
      if (fence[1]) code.className = `language-${fence[1].replace(/[^a-z0-9_-]/gi, '')}`;
      code.textContent = values.join('\n'); pre.append(code); container.append(pre); continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const node = document.createElement(`h${heading[1].length}`); appendInlineMarkdown(node, heading[2]);
      container.append(node); index += 1; continue;
    }
    if (index + 1 < lines.length && line.includes('|') && isTableDivider(lines[index + 1])) {
      const table = document.createElement('table'); const thead = document.createElement('thead');
      const headRow = document.createElement('tr');
      markdownCells(line).forEach(value => { const cell = document.createElement('th'); appendInlineMarkdown(cell, value); headRow.append(cell); });
      thead.append(headRow); table.append(thead); index += 2;
      const tbody = document.createElement('tbody');
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        const row = document.createElement('tr');
        markdownCells(lines[index++]).forEach(value => { const cell = document.createElement('td'); appendInlineMarkdown(cell, value); row.append(cell); });
        tbody.append(row);
      }
      table.append(tbody); const wrap = document.createElement('div'); wrap.className = 'markdown-table'; wrap.append(table); container.append(wrap); continue;
    }
    const listMatch = line.match(/^\s*([-*+]|\d+[.)])\s+(.+)$/);
    if (listMatch) {
      const ordered = /^\d/.test(listMatch[1]); const list = document.createElement(ordered ? 'ol' : 'ul');
      while (index < lines.length) {
        const itemMatch = lines[index].match(/^\s*([-*+]|\d+[.)])\s+(.+)$/);
        if (!itemMatch || /^\d/.test(itemMatch[1]) !== ordered) break;
        const item = document.createElement('li'); appendInlineMarkdown(item, itemMatch[2]); list.append(item); index += 1;
      }
      container.append(list); continue;
    }
    if (/^>\s?/.test(line)) {
      const quote = document.createElement('blockquote');
      while (index < lines.length && /^>\s?/.test(lines[index])) {
        appendInlineMarkdown(quote, lines[index++].replace(/^>\s?/, ''));
        if (index < lines.length && /^>\s?/.test(lines[index])) quote.append(document.createElement('br'));
      }
      container.append(quote); continue;
    }
    if (/^---+$/.test(line.trim())) { container.append(document.createElement('hr')); index += 1; continue; }
    const paragraph = document.createElement('p');
    while (index < lines.length && lines[index].trim()) {
      if (paragraph.childNodes.length && startsBlock(lines[index], lines[index + 1])) break;
      if (paragraph.childNodes.length) paragraph.append(document.createElement('br'));
      appendInlineMarkdown(paragraph, lines[index++]);
    }
    container.append(paragraph);
  }
}

function renderAnswer(markdown) {
  const container = $('#answer-text'); container.replaceChildren();
  const marker = '\n\n### 引用来源\n'; const markerIndex = markdown.lastIndexOf(marker);
  const visibleBody = (markerIndex >= 0 ? markdown.slice(0, markerIndex) : markdown)
    .replace(/\s*\[(?:ev|EV)_\d{3,}\]/g, '');
  const body = document.createElement('div'); body.className = 'answer-body markdown-body';
  renderMarkdown(visibleBody, body);
  body.querySelectorAll('a').forEach(link => {
    if (/^\[\d+\]$/.test(link.textContent.trim())) link.classList.add('citation-link');
  });
  container.append(body);
  if (markerIndex < 0) return;
  const heading = document.createElement('h3'); heading.textContent = '引用来源'; container.append(heading);
  const sources = document.createElement('div'); sources.className = 'answer-sources markdown-body';
  renderMarkdown(markdown.slice(markerIndex + marker.length), sources); container.append(sources);
}
const availableTools = [
  ['vector_search', '语义向量检索'], ['grep_files', '全库精确检索'], ['keyword_search', '关键词+时间排序'],
  ['find_files', '文件定位'], ['search_within_document', '文档内精确检索'],
  ['metadata_search', '本地元数据筛选'], ['dify_metadata_search', 'Dify 向量+元数据过滤'], ['read_full_document', '读取完整 Markdown'],
  ['get_adjacent_chunks', '相邻向量块'], ['get_document_outline', '文档目录'],
];

const agentPath = suffix => appUrl(`/api/agents/${encodeURIComponent(activeAgentId)}${suffix}`);

async function loadAgents() {
  agents = await fetch(appUrl('/api/agents')).then(response => response.json());
  const selected = agents.find(agent => agent.agent_id === activeAgentId) || agents.find(agent => agent.is_default) || agents[0];
  if (!selected) throw new Error('没有可用的 Agent');
  activeAgentId = selected.agent_id;
  $('#agent-select').innerHTML = agents.map(agent =>
    `<option value="${escapeHtml(agent.agent_id)}">${escapeHtml(agent.name)}</option>`
  ).join('');
  $('#agent-select').value = activeAgentId;
  $('#agent-select').disabled = false;
  renderAgent();
}

function renderAgent() {
  const agent = agents.find(item => item.agent_id === activeAgentId);
  $('#agent-description').textContent = agent?.description || '';
  $('#answer').hidden = true;
  $('#ask-state').textContent = '';
  $('#job').textContent = '';
  sourcePage = 1;
  activeDeleteJobId = '';
  deletePollVersion += 1;
  $('#delete-progress').hidden = true;
  $('#markdown-preview').hidden = true;
  if ($('#sources').classList.contains('active')) loadSources();
}

function renderTrace(data) {
  const trace = data.trace?.length ? data.trace : (data.process || []).map((detail, index) => ({
    sequence: index + 1, kind: 'observation', title: '执行步骤', detail, status: 'success'
  }));
  $('#trace-summary').textContent = `${trace.length} 步 · ${data.tool_calls} 次工具调用`;
  $('#trace').innerHTML = trace.map(item => {
    const metadata = [
      item.round ? `第 ${item.round} 轮` : '',
      item.tool_name ? `工具 ${item.tool_name}` : '',
      Number.isInteger(item.evidence_count) ? `累计 ${item.evidence_count} 条证据` : ''
    ].filter(Boolean);
    return `<div class="trace-item trace-${escapeHtml(item.status)}">
      <div class="trace-marker">${escapeHtml(item.sequence)}</div>
      <div class="trace-content">
        <div class="trace-head"><span class="trace-kind kind-${escapeHtml(item.kind)}">${escapeHtml(traceLabels[item.kind] || item.kind)}</span><strong>${escapeHtml(item.title)}</strong></div>
        ${metadata.length ? `<div class="trace-meta">${metadata.map(value => `<span>${escapeHtml(value)}</span>`).join('')}</div>` : ''}
        ${item.query ? `<div class="trace-query"><span>查询</span><code>${escapeHtml(item.query)}</code></div>` : ''}
        <p>${escapeHtml(item.detail)}</p>
      </div>
    </div>`;
  }).join('');
}

document.querySelectorAll('.tab').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('.tab,.panel').forEach(node => node.classList.remove('active'));
  button.classList.add('active'); $(`#${button.dataset.panel}`).classList.add('active');
  if (button.dataset.panel === 'sources') loadSources();
  if (button.dataset.panel === 'model-admin') loadModelConnections();
  if (button.dataset.panel === 'agent-admin') loadAgentAdmin();
  if (button.dataset.panel === 'kb-admin') loadKnowledgeBaseAdmin();
}));

async function enterApplication(username) {
  $('#login-screen').hidden = true;
  $('#app-shell').hidden = false;
  $('#session-user').textContent = username;
  fetch(appUrl('/api/health')).then(r => r.json()).then(data => {
    $('#health').textContent = data.mock_external_services ? '本地演示模式' : '外部服务已连接';
  }).catch(() => $('#health').textContent = '服务异常');
  try { await loadAgents(); }
  catch (error) { $('#agent-description').textContent = error.message; }
}

async function bootstrapAuth() {
  try {
    const session = await apiJson('/api/auth/session');
    if (session.authenticated) await enterApplication(session.username);
    else { $('#login-screen').hidden = false; $('#app-shell').hidden = true; }
  } catch (error) { setFormState('#login-state', error.message, true); }
}

$('#login-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = submitButton(event); button.disabled = true;
  setFormState('#login-state', '正在登录…');
  try {
    const session = await apiJson('/api/auth/login', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({username: $('#login-username').value.trim(), password: $('#login-password').value})
    });
    $('#login-password').value = ''; setFormState('#login-state', ''); await enterApplication(session.username);
  } catch (error) { setFormState('#login-state', error.message, true); }
  finally { button.disabled = false; }
});

$('#logout').addEventListener('click', async () => {
  await apiJson('/api/auth/logout', {method:'POST'});
  $('#app-shell').hidden = true; $('#login-screen').hidden = false;
  $('#login-password').value = ''; $('#login-username').focus();
});

bootstrapAuth();

$('#agent-select').addEventListener('change', event => {
  activeAgentId = event.target.value;
  renderAgent();
});

$('#ask-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = submitButton(event); button.disabled = true;
  $('#ask-state').textContent = '正在定位文件并检索证据…'; $('#answer').hidden = true;
  try {
    if (!activeAgentId) throw new Error('Agent 尚未加载');
    const response = await fetch(agentPath('/ask'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({question:$('#question').value, conversation_id: conversationId})});
    const data = await response.json(); if (!response.ok) throw new Error(data.detail || '检索失败');
    renderAnswer(data.answer);
    renderTrace(data);
    $('#evidences').innerHTML = data.evidences.map(item => `<div class="evidence"><strong>${escapeHtml(data.agent_name)} · ${escapeHtml(item.filename)}</strong><small>${escapeHtml(item.page ? `第 ${item.page} 页` : item.location || '位置未知')} · ${escapeHtml(item.retrieval_type)}</small><p>${escapeHtml(item.content)}</p>${item.pdf_url ? `<a href="${escapeHtml(sourceUrl(item.pdf_url))}" target="_blank" rel="noopener">查看原始文件</a>` : ''}</div>`).join('');
    $('#ask-state').textContent = `${escapeHtml(data.agent_name)} · 调用 ${data.tool_calls} 次检索工具，获得 ${data.evidences.length} 条证据`; $('#answer').hidden = false;
  } catch (error) { $('#ask-state').textContent = error.message; } finally { button.disabled = false; }
});

$('#upload-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = submitButton(event); button.disabled = true;
  try {
    if (!activeAgentId) throw new Error('Agent 尚未加载');
    const files = selectedUploadFiles();
    if (!files.length) throw new Error('请至少选择一个文件或目录');
    if (files.length > 2000) throw new Error(`一次最多选择 2000 个文件，当前为 ${files.length} 个`);
    const unsupported = files.find(file => !/\.(pdf|doc|docx|txt)$/i.test(file.name));
    if (unsupported) throw new Error(`不支持的文件格式：${unsupported.name}`);
    const job = await apiJson(agentPath('/ingest-jobs'), {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
        filenames: files.map(file => file.name),
        rewrite_enabled: $('#rewrite-enabled').checked,
        rewrite_prompt: $('#rewrite-prompt').value,
        rewrite_review_required: $('#rewrite-review-required').checked,
        workflow_enabled: $('#workflow-enabled').checked,
      })
    });
    currentIngestJobId = job.job_id;
    const batches = buildUploadBatches(files);
    let uploaded = 0;
    for (const batch of batches) {
      await uploadIngestBatch(job.job_id, batch);
      uploaded += batch.length;
      renderUploadProgress(uploaded, files.length);
    }
    await apiJson(`/api/jobs/${encodeURIComponent(job.job_id)}/start`, {method:'POST'});
    pollJob(job.job_id);
  } catch (error) { $('#job').textContent = error.message; button.disabled = false; }
});

function selectedUploadFiles() {
  const folder = [...$('#folder-files').files];
  return folder.length ? folder : [...$('#files').files];
}

function updateFileSelectionState() {
  const files = selectedUploadFiles();
  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
  $('#file-selection-state').textContent = files.length
    ? `已选择 ${files.length} 个文件，约 ${(totalBytes / 1024 / 1024).toFixed(1)} MB`
    : '尚未选择文件';
}

$('#files').addEventListener('change', () => {
  if ($('#files').files.length) $('#folder-files').value = '';
  updateFileSelectionState();
});
$('#folder-files').addEventListener('change', () => {
  if ($('#folder-files').files.length) $('#files').value = '';
  updateFileSelectionState();
});

function buildUploadBatches(files, maxFiles = 25, maxBytes = 120 * 1024 * 1024) {
  const batches = [];
  let batch = [];
  let bytes = 0;
  files.forEach((file, position) => {
    if (batch.length && (batch.length >= maxFiles || bytes + file.size > maxBytes)) {
      batches.push(batch);
      batch = [];
      bytes = 0;
    }
    batch.push({file, position});
    bytes += file.size;
  });
  if (batch.length) batches.push(batch);
  return batches;
}

async function uploadIngestBatch(jobId, batch) {
  let lastError;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const body = new FormData();
    batch.forEach(item => body.append('files', item.file));
    body.append('positions', JSON.stringify(batch.map(item => item.position)));
    try {
      return await apiJson(`/api/jobs/${encodeURIComponent(jobId)}/upload-batch`, {method:'POST', body});
    } catch (error) {
      lastError = error;
      if (attempt < 3) await new Promise(resolve => setTimeout(resolve, attempt * 1000));
    }
  }
  throw lastError;
}

function renderUploadProgress(uploaded, total) {
  $('#job').innerHTML = `<div class="job-summary"><strong>正在分批上传</strong><span>${uploaded}/${total}</span></div><p class="job-note">文件将在全部上传完成后进入后台队列。</p>`;
}
$('#rewrite-enabled').addEventListener('change', event => {
  $('#rewrite-prompt-wrap').hidden = !event.target.checked;
  $('#rewrite-review-required').disabled = !event.target.checked;
  if (!event.target.checked) $('#rewrite-review-required').checked = false;
});

async function pollJob(id) {
  const version = ++ingestPollVersion;
  const job = await apiJson(`/api/jobs/${encodeURIComponent(id)}`);
  if (version !== ingestPollVersion) return;
  renderIngestJob(job);
  if (job.status !== 'completed') setTimeout(() => { if (version === ingestPollVersion) pollJob(id); }, 1200);
  else $('#upload-form button[type="submit"]').disabled = false;
}

function renderIngestJob(job) {
  const priority = {failed:0, retrying:1, running:2, preview_ready:3, queued:4, uploaded:5, pending:6, success:7, duplicate:8};
  const visibleItems = [...job.items].sort((a, b) => (priority[a.status] ?? 9) - (priority[b.status] ?? 9) || a.position - b.position).slice(0, 100);
  const hiddenCount = Math.max(0, job.items.length - visibleItems.length);
  const labels = {uploading:'上传中', pending:'等待启动', running:'处理中', paused:'已暂停', completed:'已完成'};
  $('#job').innerHTML = `
    <div class="job-summary"><strong>${escapeHtml(labels[job.status] || job.status)}</strong><span>总计 ${job.total}</span><span>已上传 ${job.uploaded}</span><span>已处理 ${job.completed}</span><span>成功 ${job.succeeded}</span><span>待确认 ${job.preview_ready}</span><span>失败 ${job.failed}</span></div>
    <div class="job-controls">
      ${job.status === 'running' ? '<button type="button" data-job-action="pause">暂停任务</button>' : ''}
      ${job.status === 'paused' ? '<button type="button" data-job-action="resume">恢复任务</button>' : ''}
      ${job.failed ? '<button type="button" data-job-action="retry-failed">重试失败文件</button>' : ''}
    </div>
    ${visibleItems.map(item => `<div class="job-item"><span>${escapeHtml(item.filename)}</span><strong>${escapeHtml(item.status)}</strong><span>${escapeHtml(item.message)}${item.status === 'preview_ready' ? ` <button type="button" data-open-preview="${escapeHtml(item.source_id)}">查看预览</button>` : ''}</span></div>`).join('')}
    ${hiddenCount ? `<p class="job-note">为保证页面流畅，仅显示优先级最高的 100 项，另有 ${hiddenCount} 项未展开。</p>` : ''}`;
  document.querySelectorAll('[data-open-preview]').forEach(button => button.addEventListener('click', () => loadRewritePreview(button.dataset.openPreview)));
  document.querySelectorAll('[data-job-action]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const updated = await apiJson(`/api/jobs/${encodeURIComponent(job.job_id)}/${button.dataset.jobAction}`, {method:'POST'});
      ingestPollVersion += 1; renderIngestJob(updated);
      if (updated.status !== 'completed' && updated.status !== 'paused') pollJob(updated.job_id);
    } catch (error) { $('#job').prepend(document.createTextNode(error.message)); }
  }));
}

async function loadSources() {
  if (!activeAgentId) return;
  const query = $('#source-query').value.trim();
  const data = await apiJson(agentPath(`/sources/page?page=${sourcePage}&page_size=${sourcePageSize}&query=${encodeURIComponent(query)}`));
  const sources = data.items;
  sourcePage = data.page; sourceTotalPages = data.total_pages;
  $('#source-count').textContent = query ? `共 ${data.total} 份匹配文件` : `共 ${data.total} 份文件`;
  $('#source-page-state').textContent = `第 ${data.page} / ${data.total_pages} 页`;
  $('#source-prev').disabled = data.page <= 1; $('#source-next').disabled = data.page >= data.total_pages;
  $('#source-list').innerHTML = sources.map(item => `<tr><td><input type="checkbox" data-source-select="${escapeHtml(item.source_id)}"></td><td>${escapeHtml(item.filename)}${item.rewrite_status === 'preview_ready' ? '<br><small>待确认改写</small>' : ''}</td><td>${escapeHtml(item.document_type.toUpperCase())}</td><td>${escapeHtml(item.standard_no || '—')}</td><td>${escapeHtml(item.standard_year || '—')}</td><td><button type="button" data-source-id="${escapeHtml(item.source_id)}">${escapeHtml(item.metadata_status || 'pending')}</button></td><td>${escapeHtml(item.parse_status)}</td><td>${escapeHtml(item.dify_status)}</td><td>${escapeHtml(item.pdf_platform_status)}</td><td><button type="button" data-markdown-preview="${escapeHtml(item.source_id)}">Markdown</button> <button type="button" data-reparse-source="${escapeHtml(item.source_id)}">重新解析</button> ${item.rewrite_status === 'preview_ready' ? `<button type="button" data-open-preview="${escapeHtml(item.source_id)}">改写预览</button>` : ''} ${item.pdf_url ? `<a href="${escapeHtml(sourceUrl(item.pdf_url))}" target="_blank">打开</a>` : ''} <button type="button" class="danger" data-delete-source="${escapeHtml(item.source_id)}">删除</button></td></tr>`).join('');
  document.querySelectorAll('[data-source-id]').forEach(button => button.addEventListener('click', () => editSourceMetadata(button.dataset.sourceId)));
  document.querySelectorAll('[data-delete-source]').forEach(button => button.addEventListener('click', () => deleteSource(button.dataset.deleteSource)));
  document.querySelectorAll('[data-open-preview]').forEach(button => button.addEventListener('click', () => loadRewritePreview(button.dataset.openPreview)));
  document.querySelectorAll('[data-markdown-preview]').forEach(button => button.addEventListener('click', () => openMarkdownPreview(button.dataset.markdownPreview)));
  document.querySelectorAll('[data-reparse-source]').forEach(button => button.addEventListener('click', () => reparseSource(button.dataset.reparseSource)));
  $('#select-all-sources').checked = false;
  restoreActiveDeleteJob();
}

async function reparseSource(sourceId) {
  const source = await apiJson(agentPath(`/sources/${encodeURIComponent(sourceId)}`));
  if (!confirm(`重新解析“${source.filename}”？系统会从原文件重建 Markdown，并刷新 Dify 中的文档。`)) return;
  try {
    $('#batch-metadata-state').textContent = `正在重新解析 ${source.filename}…`;
    const updated = await apiJson(agentPath(`/sources/${encodeURIComponent(sourceId)}/reparse`), {method:'POST'});
    if (updated.parse_status !== 'success') throw new Error(updated.error_message || '重新解析未成功');
    $('#batch-metadata-state').textContent = `重新解析完成：${source.filename}`;
    await loadSources();
  } catch (error) { $('#batch-metadata-state').textContent = error.message; }
}

async function openMarkdownPreview(sourceId, version = 'document') {
  try {
    const data = await apiJson(agentPath(`/sources/${encodeURIComponent(sourceId)}/markdown?version=${encodeURIComponent(version)}`));
    markdownPreviewSourceId = sourceId; markdownPreviewText = data.markdown;
    $('#markdown-preview').hidden = false;
    $('#markdown-preview-title').textContent = `${data.filename} · Markdown`;
    $('#markdown-version').innerHTML = data.available_versions.map(name => `<option value="${escapeHtml(name)}" ${name === data.version ? 'selected' : ''}>${escapeHtml(name)}</option>`).join('');
    const parserLabel = {mineru:'MinerU',local:'本地解析',local_fallback:'MinerU失败后本地回退',qwenvl:'Qwen-VL'}[data.stats.parser_engine] || data.stats.parser_engine;
    $('#markdown-preview-stats').textContent = `${data.stats.characters} 字符 · ${data.stats.lines} 行${parserLabel ? ` · ${parserLabel}` : ''}${data.stats.mineru_response_field ? ` · ${data.stats.mineru_response_field}` : ''}`;
    const warnings = [];
    if (data.stats.empty) warnings.push('内容为空');
    if (data.stats.unreadable_marks) warnings.push(`${data.stats.unreadable_marks} 处无法辨认`);
    if (data.stats.replacement_characters) warnings.push(`${data.stats.replacement_characters} 个异常替换字符`);
    if (data.stats.mineru_error) warnings.push(`MinerU失败：${data.stats.mineru_error}`);
    $('#markdown-warning').hidden = !warnings.length; $('#markdown-warning').textContent = warnings.join(' · ');
    showMarkdownMode('rendered');
    $('#markdown-preview').scrollIntoView({behavior:'smooth', block:'start'});
  } catch (error) { $('#batch-metadata-state').textContent = error.message; }
}

function showMarkdownMode(mode) {
  const rendered = mode === 'rendered';
  $('#markdown-preview-body').hidden = !rendered; $('#markdown-preview-source').hidden = rendered;
  $('#markdown-rendered').classList.toggle('active', rendered); $('#markdown-source').classList.toggle('active', !rendered);
  if (rendered) {
    $('#markdown-preview-body').replaceChildren();
    renderMarkdown(markdownPreviewText.replace(/<!--\s*(?:line|paragraph|table-row|page|Qwen-VL pages):[^>]*-->\s*/gi, ''), $('#markdown-preview-body'));
  }
  else $('#markdown-preview-source').textContent = markdownPreviewText;
}

$('#source-prev').addEventListener('click', () => { if (sourcePage > 1) { sourcePage -= 1; loadSources(); } });
$('#source-next').addEventListener('click', () => { if (sourcePage < sourceTotalPages) { sourcePage += 1; loadSources(); } });
$('#source-page-size').addEventListener('change', event => { sourcePageSize = Number(event.target.value); sourcePage = 1; loadSources(); });
$('#source-search').addEventListener('click', () => { sourcePage = 1; loadSources(); });
$('#source-query').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); sourcePage = 1; loadSources(); } });
$('#markdown-version').addEventListener('change', event => openMarkdownPreview(markdownPreviewSourceId, event.target.value));
$('#markdown-rendered').addEventListener('click', () => showMarkdownMode('rendered'));
$('#markdown-source').addEventListener('click', () => showMarkdownMode('source'));
$('#markdown-close').addEventListener('click', () => { $('#markdown-preview').hidden = true; markdownPreviewSourceId = ''; });

$('#select-all-sources').addEventListener('change', event => {
  document.querySelectorAll('[data-source-select]').forEach(input => { input.checked = event.target.checked; });
});

$('#batch-metadata').addEventListener('click', async () => {
  const sourceIds = [...document.querySelectorAll('[data-source-select]:checked')].map(input => input.dataset.sourceSelect);
  try {
    $('#batch-metadata-state').textContent = '正在批量 AI 抽取…';
    const result = await apiJson(agentPath('/sources/extract-metadata-batch'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({source_ids: sourceIds})});
    $('#batch-metadata-state').textContent = `批量抽取完成：${result.success} 个成功，${result.failed} 个失败，共 ${result.total} 个`;
    await loadSources();
  } catch (error) { $('#batch-metadata-state').textContent = error.message; }
});

async function deleteSource(sourceId) {
  const source = await apiJson(agentPath(`/sources/${encodeURIComponent(sourceId)}`));
  if (!confirm(`确定删除“${source.filename}”？这会同时删除 Dify 文档和本地文件，且不可恢复。`)) return;
  try {
    await apiJson(agentPath(`/sources/${encodeURIComponent(sourceId)}`), {method:'DELETE'});
    await loadSources();
  } catch (error) { alert(error.message); }
}
$('#refresh').addEventListener('click', () => { sourcePage = 1; loadSources(); });

async function openClearSourcesDialog() {
  const button = $('#clear-sources'); button.disabled = true;
  try {
    const [configs, bases, page] = await Promise.all([
      apiJson('/api/admin/agents'),
      apiJson('/api/admin/knowledge-bases'),
      apiJson(agentPath('/sources/page?page=1&page_size=1')),
    ]);
    const config = configs.find(item => item.agent_id === activeAgentId);
    const knowledgeBase = bases.find(item => item.knowledge_base_id === config?.knowledge_base_id);
    if (!knowledgeBase) throw new Error('找不到当前 Agent 绑定的知识库');
    clearKnowledgeBaseName = knowledgeBase.name;
    $('#clear-kb-name').textContent = knowledgeBase.name;
    $('#clear-source-total').textContent = `${page.total} 份`;
    $('#clear-confirmation').value = '';
    $('#clear-confirm').disabled = true;
    setFormState('#clear-dialog-state', '');
    $('#clear-sources-dialog').showModal();
    $('#clear-confirmation').focus();
  } catch (error) {
    setFormState('#batch-metadata-state', error.message, true);
  } finally { button.disabled = false; }
}

function renderDeleteJob(job) {
  activeDeleteJobId = job.status === 'running' ? job.job_id : '';
  $('#delete-progress').hidden = false;
  $('#delete-progress-title').textContent = job.status === 'completed' ? '资料库清空任务已完成' : `正在清空 ${job.knowledge_base_name}`;
  $('#delete-progress-count').textContent = `${job.completed} / ${job.total}`;
  $('#delete-progress-bar').max = Math.max(job.total, 1);
  $('#delete-progress-bar').value = job.completed;
  $('#delete-progress-note').textContent = `成功 ${job.succeeded} 份，失败 ${job.failed} 份。失败文件会保留在本地和资料库中。`;
  const failures = job.items.filter(item => item.status === 'failed');
  $('#delete-failures').innerHTML = failures.length
    ? `<strong>失败清单</strong><ul class="delete-failure-list">${failures.slice(0, 100).map(item => `<li>${escapeHtml(item.filename)}：${escapeHtml(item.message || '删除失败')}</li>`).join('')}</ul>${failures.length > 100 ? `<p class="section-copy">仅显示前 100 项，共 ${failures.length} 项失败。</p>` : ''}`
    : '';
}

async function pollDeleteJob(jobId, version) {
  try {
    const job = await apiJson(`/api/delete-jobs/${encodeURIComponent(jobId)}`);
    if (version !== deletePollVersion) return;
    renderDeleteJob(job);
    if (job.status === 'running') setTimeout(() => pollDeleteJob(jobId, version), 1000);
    else { sourcePage = 1; await loadSources(); }
  } catch (error) {
    if (version === deletePollVersion) setFormState('#batch-metadata-state', error.message, true);
  }
}

async function restoreActiveDeleteJob() {
  if (!activeAgentId || activeDeleteJobId) return;
  try {
    const job = await apiJson(agentPath('/delete-job/active'));
    if (!job || activeDeleteJobId) return;
    renderDeleteJob(job);
    const version = ++deletePollVersion;
    pollDeleteJob(job.job_id, version);
  } catch (error) {
    setFormState('#batch-metadata-state', error.message, true);
  }
}

$('#clear-sources').addEventListener('click', openClearSourcesDialog);
$('#clear-cancel').addEventListener('click', () => $('#clear-sources-dialog').close());
$('#clear-confirmation').addEventListener('input', event => {
  $('#clear-confirm').disabled = event.target.value.trim() !== clearKnowledgeBaseName;
});
$('#clear-sources-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = $('#clear-confirm'); button.disabled = true;
  try {
    const job = await apiJson(agentPath('/sources/delete-all'), {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({confirmation: $('#clear-confirmation').value.trim()}),
    });
    $('#clear-sources-dialog').close();
    renderDeleteJob(job);
    const version = ++deletePollVersion;
    if (job.status === 'running') pollDeleteJob(job.job_id, version);
    else await loadSources();
  } catch (error) {
    setFormState('#clear-dialog-state', error.message, true);
    button.disabled = false;
  }
});

async function loadRewritePreview(sourceId) {
  try {
    const data = await apiJson(agentPath(`/sources/${encodeURIComponent(sourceId)}/rewrite-preview`));
    previewSourceId = sourceId; $('#rewrite-preview').hidden = false;
    $('#rewrite-preview-title').textContent = `${data.filename} · AI 改写预览`;
    $('#preview-rewrite-prompt').value = data.rewrite_prompt || '';
    $('#rewrite-original').textContent = data.original_markdown;
    $('#rewrite-result').textContent = data.preview_markdown;
    setFormState('#rewrite-preview-state', '尚未上传 Dify，请确认最终版本');
    $('#rewrite-preview').scrollIntoView({behavior:'smooth', block:'start'});
  } catch (error) { $('#job').textContent = error.message; }
}

$('#regenerate-rewrite').addEventListener('click', async event => {
  if (!previewSourceId) return; event.currentTarget.disabled = true;
  setFormState('#rewrite-preview-state', '正在重新改写…');
  try {
    const data = await apiJson(agentPath(`/sources/${encodeURIComponent(previewSourceId)}/rewrite-preview`), {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({rewrite_prompt: $('#preview-rewrite-prompt').value})
    });
    $('#rewrite-result').textContent = data.preview_markdown; setFormState('#rewrite-preview-state', '已生成新预览，尚未上传 Dify');
  } catch (error) { setFormState('#rewrite-preview-state', error.message, true); }
  finally { event.currentTarget.disabled = false; }
});

async function finishRewrite(path, message) {
  if (!previewSourceId) return;
  setFormState('#rewrite-preview-state', message);
  try {
    await apiJson(agentPath(`/sources/${encodeURIComponent(previewSourceId)}/${path}`), {method:'POST'});
    $('#rewrite-preview').hidden = true; previewSourceId = ''; $('#job').textContent = '最终 Markdown 已提交入库';
    await loadSources();
  } catch (error) { setFormState('#rewrite-preview-state', error.message, true); }
}

$('#confirm-rewrite').addEventListener('click', () => finishRewrite('confirm-rewrite', '正在确认并上传最终 Markdown…'));
$('#use-original').addEventListener('click', () => finishRewrite('use-original', '正在使用原始 Markdown 入库…'));

async function editSourceMetadata(sourceId) {
  const source = await apiJson(agentPath(`/sources/${encodeURIComponent(sourceId)}`));
  const [adminAgents, adminKnowledgeBases] = await Promise.all([
    apiJson('/api/admin/agents'), apiJson('/api/admin/knowledge-bases')
  ]);
  const activeConfig = adminAgents.find(item => item.agent_id === activeAgentId);
  const activeKnowledgeBase = adminKnowledgeBases.find(item => item.knowledge_base_id === activeConfig?.knowledge_base_id);
  editingSourceId = sourceId;
  $('#metadata-form').hidden = false;
  $('#metadata-title').textContent = `${source.filename} · 元数据`;
  $('#system-metadata-fields').innerHTML = [
    ['文件名', source.filename], ['上传时间', new Date(source.created_at).toLocaleString()],
    ['文件类型', source.document_type.toUpperCase()], ['文件 Hash', source.file_hash],
    ['解析状态', source.parse_status], ['Dify 状态', source.dify_status],
  ].map(([label, value]) => `<label>${label}<input value="${escapeHtml(value)}" readonly></label>`).join('');
  const definitions = activeKnowledgeBase?.metadata_fields || [];
  $('#metadata-fields').innerHTML = definitions.length
    ? definitions.map(field => {
      const value = source.custom_metadata?.[field.key]?.value ?? '';
      return `<label>${escapeHtml(field.label)}<input data-custom-key="${escapeHtml(field.key)}" value="${escapeHtml(value)}"><small>${escapeHtml(field.description || field.value_type)}</small></label>`;
    }).join('')
    : '<p class="secret-state">暂无自定义元数据字段。请先在“知识库连接 → 自定义字段”中添加字段，保存后再进行 AI 抽取。</p>';
  const custom = source.custom_metadata || {};
  $('#metadata-custom-fields').innerHTML = Object.entries(custom).length
    ? Object.entries(custom).map(([key, item]) => `<div class="metadata-value"><small>${escapeHtml(key)} · 方法：${escapeHtml(item?.extraction_method || '—')} · 置信度：${item?.confidence == null ? '—' : escapeHtml(item.confidence)} · 位置：${escapeHtml(item?.location || '—')}<br>证据：${escapeHtml(item?.evidence || '—')}</small></div>`).join('')
    : '';
  setFormState('#metadata-form-state', `抽取状态：${source.metadata_status || 'pending'}${source.metadata_error ? ` · ${source.metadata_error}` : ''}`);
  $('#metadata-form').scrollIntoView({behavior:'smooth', block:'start'});
}

$('#metadata-cancel').addEventListener('click', () => { $('#metadata-form').hidden = true; editingSourceId = ''; });
$('#metadata-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const custom = {};
    document.querySelectorAll('#metadata-fields [data-custom-key]').forEach(input => {
      custom[input.dataset.customKey] = {value: input.value, confidence: 1, evidence: '人工修改', location: '', extraction_method: 'manual'};
    });
    const payload = {custom_metadata: custom};
    await apiJson(agentPath(`/sources/${encodeURIComponent(editingSourceId)}/metadata`), {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    setFormState('#metadata-form-state', '元数据已保存'); await loadSources();
  } catch (error) { setFormState('#metadata-form-state', error.message, true); }
});
$('#metadata-reextract').addEventListener('click', async () => {
  try { setFormState('#metadata-form-state', '正在重新抽取…'); await apiJson(agentPath(`/sources/${encodeURIComponent(editingSourceId)}/extract-metadata`), {method:'POST'}); await editSourceMetadata(editingSourceId); }
  catch (error) { setFormState('#metadata-form-state', error.message, true); }
});

async function apiJson(url, options = {}) {
  let response;
  try {
    response = await fetch(url.startsWith('/api/') ? appUrl(url) : url, options);
  } catch {
    throw new Error('无法连接服务，请检查 Nginx、后端进程和网络连接');
  }
  if (response.status === 204) return null;
  const contentType = response.headers.get('content-type') || '';
  const data = contentType.includes('application/json')
    ? await response.json()
    : {detail: await response.text()};
  if (!response.ok) {
    if (response.status === 413) throw new Error('上传批次超过 Nginx 大小限制；系统已按体积分批，若仍出现此错误，请提高 client_max_body_size');
    if (!contentType.includes('application/json')) {
      throw new Error(`服务网关返回错误（HTTP ${response.status}），请查看 Nginx error.log`);
    }
    const detail = Array.isArray(data.detail)
      ? data.detail.map(item => `${item.loc?.slice(-1)?.[0] || '字段'}：${item.msg || '格式错误'}`).join('；')
      : (data.detail || '操作失败');
    throw new Error(detail);
  }
  if (!contentType.includes('application/json')) throw new Error('服务返回了非 JSON 响应');
  return data;
}

function setFormState(selector, message, error = false) {
  const node = $(selector); node.textContent = message; node.classList.toggle('error', error);
}

async function loadKnowledgeBaseAdmin(selectId = '') {
  knowledgeBases = await apiJson('/api/admin/knowledge-bases');
  $('#kb-admin-list').innerHTML = knowledgeBases.length ? knowledgeBases.map(item => `
    <button type="button" class="admin-list-item ${item.knowledge_base_id === (selectId || editingKnowledgeBaseId) ? 'selected' : ''}" data-kb-id="${escapeHtml(item.knowledge_base_id)}">
      <strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.dataset_id)}</span>
      <small>${item.enabled ? '已启用' : '已停用'} · ${escapeHtml(item.api_key_hint)}</small>
    </button>`).join('') : '<p class="empty-state">尚未配置知识库</p>';
  document.querySelectorAll('[data-kb-id]').forEach(button => button.addEventListener('click', () => editKnowledgeBase(button.dataset.kbId)));
  if (selectId) editKnowledgeBase(selectId);
}

function editKnowledgeBase(id) {
  const item = knowledgeBases.find(value => value.knowledge_base_id === id); if (!item) return;
  editingKnowledgeBaseId = id;
  $('#kb-config-form').hidden = false;
  $('#kb-id').value = item.knowledge_base_id; $('#kb-id').disabled = true;
  $('#kb-name').value = item.name; $('#kb-description').value = item.description;
  $('#kb-answer-prompt').value = item.answer_prompt || '';
  $('#kb-base-url').value = item.base_url; $('#kb-dataset-id').value = item.dataset_id;
  $('#kb-api-key').value = ''; $('#kb-search-method').value = item.search_method;
  $('#kb-chunking-mode').value = item.chunking_mode || 'automatic';
  $('#kb-chunk-separator').value = item.chunk_separator || '\n__SPLIT__\n';
  $('#kb-chunk-max-tokens').value = item.chunk_max_tokens || 800;
  $('#kb-pdf-parser').value = item.pdf_parser || 'default';
  $('#kb-qwenvl-base-url').value = item.qwenvl_base_url || '';
  $('#kb-qwenvl-api-key').value = '';
  $('#kb-qwenvl-model').value = item.qwenvl_model || 'qwen-vl-max';
  $('#kb-qwenvl-short-prompt').value = item.qwenvl_short_prompt || defaultQwenShortPrompt;
  $('#kb-qwenvl-long-prompt').value = item.qwenvl_long_prompt || defaultQwenLongPrompt;
  $('#kb-qwenvl-threshold').value = item.qwenvl_page_threshold || 10;
  $('#kb-qwenvl-batch-pages').value = item.qwenvl_batch_pages || 3;
  $('#kb-qwenvl-dpi').value = item.qwenvl_dpi || 200;
  $('#kb-qwenvl-state').textContent = `Qwen-VL API Key：${item.qwenvl_api_key_configured ? '已配置' : '未配置'}。视觉解析结果直接作为最终 Markdown。`;
  renderPdfParserFields();
  $('#kb-source-platform-mode').value = item.source_platform_mode || 'inherit';
  $('#kb-source-platform-base-url').value = item.source_platform_base_url || '';
  $('#kb-source-platform-api-key').value = '';
  $('#kb-source-platform-command').value = JSON.stringify(item.source_platform_command || []);
  $('#kb-source-platform-url-template').value = item.source_platform_url_template || '';
  $('#kb-source-platform-state').textContent = `平台 API Key：${item.source_platform_api_key_configured ? '已配置' : '未配置'}。输入新值才会替换。`;
  renderSourcePlatformFields();
  $('#kb-enabled').checked = item.enabled;
  $('#kb-metadata-ai').checked = item.metadata_ai_enabled;
  $('#kb-metadata-prompt').value = item.metadata_extraction_prompt || '';
  metadataFieldDraft = (item.metadata_fields || []).map(field => ({...field}));
  renderMetadataFieldBuilder();
  $('#kb-workflow-enabled').checked = item.workflow_enabled;
  $('#kb-workflow-base-url').value = item.workflow_base_url || '';
  $('#kb-workflow-api-key').value = '';
  $('#kb-workflow-app-id').value = item.workflow_app_id || '';
  $('#kb-workflow-markdown-input').value = item.workflow_markdown_input || 'markdown';
  $('#kb-workflow-file-input').value = item.workflow_file_input || 'file';
  $('#kb-workflow-output-field').value = item.workflow_output_field || 'markdown';
  $('#kb-secret-state').textContent = `API Key：${item.api_key_hint}。输入新值才会替换，服务不会回显明文。`;
  $('#delete-kb').hidden = false; setFormState('#kb-form-state', '');
  loadKnowledgeBaseAdminSelection();
}

function loadKnowledgeBaseAdminSelection() {
  document.querySelectorAll('[data-kb-id]').forEach(button => button.classList.toggle('selected', button.dataset.kbId === editingKnowledgeBaseId));
}

function newKnowledgeBase() {
  editingKnowledgeBaseId = '';
  $('#kb-config-form').reset(); $('#kb-config-form').hidden = false;
  $('#kb-id').disabled = false; $('#kb-base-url').value = 'https://api.dify.ai/v1';
  $('#kb-enabled').checked = true; $('#delete-kb').hidden = true;
  $('#kb-answer-prompt').value = '';
  $('#kb-chunking-mode').value = 'automatic'; $('#kb-chunk-separator').value = '\n__SPLIT__\n'; $('#kb-chunk-max-tokens').value = 800;
  $('#kb-pdf-parser').value = 'default'; $('#kb-qwenvl-base-url').value = ''; $('#kb-qwenvl-api-key').value = '';
  $('#kb-qwenvl-model').value = 'qwen-vl-max'; $('#kb-qwenvl-short-prompt').value = defaultQwenShortPrompt;
  $('#kb-qwenvl-long-prompt').value = defaultQwenLongPrompt; $('#kb-qwenvl-threshold').value = 10;
  $('#kb-qwenvl-batch-pages').value = 3; $('#kb-qwenvl-dpi').value = 200; renderPdfParserFields();
  $('#kb-source-platform-mode').value = 'inherit'; $('#kb-source-platform-base-url').value = '';
  $('#kb-source-platform-api-key').value = ''; $('#kb-source-platform-command').value = '[]';
  $('#kb-source-platform-url-template').value = ''; renderSourcePlatformFields();
  $('#kb-metadata-ai').checked = true; $('#kb-metadata-prompt').value = '';
  metadataFieldDraft = []; renderMetadataFieldBuilder();
  $('#kb-workflow-enabled').checked = false; $('#kb-workflow-base-url').value = '';
  $('#kb-workflow-api-key').value = ''; $('#kb-workflow-app-id').value = '';
  $('#kb-workflow-markdown-input').value = 'markdown'; $('#kb-workflow-file-input').value = 'file'; $('#kb-workflow-output-field').value = 'markdown';
  $('#kb-secret-state').textContent = 'API Key 将保存到 macOS 系统钥匙串，数据库和接口不保存明文。';
  setFormState('#kb-form-state', ''); loadKnowledgeBaseAdminSelection();
}

function knowledgeBasePayload() {
  const existing = knowledgeBases.find(item => item.knowledge_base_id === editingKnowledgeBaseId);
  const metadataFields = metadataFieldDraft.map(field => ({...field}));
  let sourcePlatformCommand = [];
  try { sourcePlatformCommand = JSON.parse($('#kb-source-platform-command').value || '[]'); }
  catch { throw new Error('上传命令必须是 JSON 数组'); }
  if (!Array.isArray(sourcePlatformCommand) || sourcePlatformCommand.some(item => typeof item !== 'string')) throw new Error('上传命令必须是字符串 JSON 数组');
  return {
    knowledge_base_id: $('#kb-id').value.trim(), name: $('#kb-name').value.trim(),
    description: $('#kb-description').value.trim(), base_url: $('#kb-base-url').value.trim(),
    answer_prompt: $('#kb-answer-prompt').value.trim(),
    dataset_id: $('#kb-dataset-id').value.trim(), api_key: $('#kb-api-key').value || null,
    search_method: $('#kb-search-method').value, enabled: $('#kb-enabled').checked,
    chunking_mode: $('#kb-chunking-mode').value, chunk_separator: $('#kb-chunk-separator').value || '\n__SPLIT__\n',
    chunk_max_tokens: Number($('#kb-chunk-max-tokens').value || 800),
    pdf_parser: $('#kb-pdf-parser').value,
    qwenvl_base_url: $('#kb-qwenvl-base-url').value.trim(),
    qwenvl_api_key: $('#kb-qwenvl-api-key').value || null,
    qwenvl_model: $('#kb-qwenvl-model').value.trim() || 'qwen-vl-max',
    qwenvl_short_prompt: $('#kb-qwenvl-short-prompt').value.trim(),
    qwenvl_long_prompt: $('#kb-qwenvl-long-prompt').value.trim(),
    qwenvl_page_threshold: Number($('#kb-qwenvl-threshold').value || 10),
    qwenvl_batch_pages: Number($('#kb-qwenvl-batch-pages').value || 3),
    qwenvl_dpi: Number($('#kb-qwenvl-dpi').value || 200),
    source_platform_mode: $('#kb-source-platform-mode').value,
    source_platform_base_url: $('#kb-source-platform-base-url').value.trim(),
    source_platform_api_key: $('#kb-source-platform-api-key').value || null,
    source_platform_command: sourcePlatformCommand,
    source_platform_url_template: $('#kb-source-platform-url-template').value.trim(),
    use_legacy_data: existing?.use_legacy_data || false,
    metadata_ai_enabled: $('#kb-metadata-ai').checked,
    metadata_extraction_prompt: $('#kb-metadata-prompt').value.trim(), metadata_fields: metadataFields,
    workflow_enabled: $('#kb-workflow-enabled').checked, workflow_base_url: $('#kb-workflow-base-url').value.trim(),
    workflow_api_key: $('#kb-workflow-api-key').value || null, workflow_app_id: $('#kb-workflow-app-id').value.trim(),
    workflow_markdown_input: $('#kb-workflow-markdown-input').value.trim() || 'markdown',
    workflow_file_input: $('#kb-workflow-file-input').value.trim() || 'file', workflow_output_field: $('#kb-workflow-output-field').value.trim() || 'markdown',
  };
}

function renderSourcePlatformFields() {
  const mode = $('#kb-source-platform-mode').value;
  document.querySelectorAll('.source-platform-http').forEach(item => { item.hidden = mode !== 'http'; });
  document.querySelectorAll('.source-platform-command').forEach(item => { item.hidden = mode !== 'command'; });
}

function renderPdfParserFields() {
  const visible = $('#kb-pdf-parser').value === 'qwenvl';
  document.querySelectorAll('.qwenvl-field').forEach(item => { item.hidden = !visible; });
}

$('#kb-source-platform-mode').addEventListener('change', renderSourcePlatformFields);
$('#kb-pdf-parser').addEventListener('change', renderPdfParserFields);

function renderMetadataFieldBuilder() {
  $('#kb-metadata-fields').value = JSON.stringify(metadataFieldDraft, null, 2);
  $('#kb-metadata-fields-builder').innerHTML = metadataFieldDraft.length ? metadataFieldDraft.map((field, index) => `
    <div class="metadata-field-row" data-field-index="${index}">
      <input data-field-label value="${escapeHtml(field.label || '')}" placeholder="显示名称，例如：会议时间">
      <input data-field-key value="${escapeHtml(field.key || '')}" placeholder="内部 key，例如 meeting_time">
      <select data-field-type><option value="string" ${field.value_type === 'string' ? 'selected' : ''}>文本</option><option value="number" ${field.value_type === 'number' ? 'selected' : ''}>数字</option><option value="date" ${field.value_type === 'date' ? 'selected' : ''}>日期</option><option value="boolean" ${field.value_type === 'boolean' ? 'selected' : ''}>布尔</option><option value="list" ${field.value_type === 'list' ? 'selected' : ''}>列表</option></select>
      <input data-field-description value="${escapeHtml(field.description || '')}" placeholder="抽取说明">
      <button type="button" class="danger" data-remove-field="${index}">删除</button>
    </div>`).join('') : '<p class="secret-state">尚未定义自定义字段。</p>';
  document.querySelectorAll('.metadata-field-row').forEach(row => {
    row.querySelectorAll('input,select').forEach(input => input.addEventListener('change', () => {
      const index = Number(row.dataset.fieldIndex); const field = metadataFieldDraft[index];
      const label = row.querySelector('[data-field-label]').value.trim();
      const keyInput = row.querySelector('[data-field-key]');
      if (input === keyInput) keyInput.dataset.manual = 'true';
      if (!keyInput.dataset.manual && (!keyInput.value || keyInput.value.startsWith('custom_field_'))) keyInput.value = `custom_field_${index + 1}`;
      field.key = keyInput.value.trim(); field.label = label;
      field.value_type = row.querySelector('[data-field-type]').value; field.description = row.querySelector('[data-field-description]').value.trim();
      $('#kb-metadata-fields').value = JSON.stringify(metadataFieldDraft, null, 2);
    }));
  });
  document.querySelectorAll('[data-remove-field]').forEach(button => button.addEventListener('click', () => {
    metadataFieldDraft.splice(Number(button.dataset.removeField), 1); renderMetadataFieldBuilder();
  }));
}

$('#add-metadata-field').addEventListener('click', () => {
  const index = metadataFieldDraft.length + 1;
  metadataFieldDraft.push({key: `custom_field_${index}`, label: `自定义字段 ${index}`, value_type: 'string', description: ''});
  renderMetadataFieldBuilder();
});

$('#new-kb').addEventListener('click', newKnowledgeBase);
$('#cancel-kb').addEventListener('click', () => { $('#kb-config-form').hidden = true; editingKnowledgeBaseId = ''; loadKnowledgeBaseAdminSelection(); });
$('#kb-config-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = submitButton(event); button.disabled = true;
  try {
    const payload = knowledgeBasePayload(); const updating = Boolean(editingKnowledgeBaseId);
    await apiJson(updating ? `/api/admin/knowledge-bases/${encodeURIComponent(editingKnowledgeBaseId)}` : '/api/admin/knowledge-bases', {
      method: updating ? 'PUT' : 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload)
    });
    editingKnowledgeBaseId = payload.knowledge_base_id; setFormState('#kb-form-state', '知识库已保存并立即生效');
    await loadKnowledgeBaseAdmin(editingKnowledgeBaseId); await loadAgents();
  } catch (error) { setFormState('#kb-form-state', error.message, true); } finally { button.disabled = false; }
});
$('#test-kb').addEventListener('click', async event => {
  event.currentTarget.disabled = true; setFormState('#kb-form-state', '正在测试 Dify 连接…');
  try {
    const result = await apiJson('/api/admin/knowledge-bases/test', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(knowledgeBasePayload())});
    setFormState('#kb-form-state', `${result.message}${Number.isInteger(result.document_count) ? ` · 当前页 ${result.document_count} 份文档` : ''}`, !result.ok);
  } catch (error) { setFormState('#kb-form-state', error.message, true); } finally { event.currentTarget.disabled = false; }
});
$('#delete-kb').addEventListener('click', async () => {
  if (!editingKnowledgeBaseId || !confirm('删除这个知识库连接配置？本地原文件、Markdown 和索引文件会保留。')) return;
  try {
    await apiJson(`/api/admin/knowledge-bases/${encodeURIComponent(editingKnowledgeBaseId)}`, {method:'DELETE'});
    editingKnowledgeBaseId = ''; $('#kb-config-form').hidden = true; await loadKnowledgeBaseAdmin();
  } catch (error) { setFormState('#kb-form-state', error.message, true); }
});

async function loadModelConnections(selectId = '') {
  modelConnections = await apiJson('/api/admin/model-connections');
  $('#model-admin-list').innerHTML = modelConnections.length ? modelConnections.map(item => `
    <button type="button" class="admin-list-item ${item.model_connection_id === (selectId || editingModelConnectionId) ? 'selected' : ''}" data-model-id="${escapeHtml(item.model_connection_id)}">
      <strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.model)}</span>
      <small>优先级 ${item.priority} · ${item.enabled ? '已启用' : '已停用'} · ${item.api_key_configured ? 'Key已配置' : 'Key未配置'}</small>
    </button>`).join('') : '<p class="empty-state">尚未配置模型连接</p>';
  document.querySelectorAll('[data-model-id]').forEach(button => button.addEventListener('click', () => editModelConnection(button.dataset.modelId)));
  if (selectId) editModelConnection(selectId);
}

function editModelConnection(id) {
  const item = modelConnections.find(value => value.model_connection_id === id); if (!item) return;
  editingModelConnectionId = id; $('#model-config-form').hidden = false;
  $('#model-id').value = item.model_connection_id; $('#model-id').disabled = true;
  $('#model-name').value = item.name; $('#model-base-url').value = item.base_url;
  $('#model-name-api').value = item.model; $('#model-api-style').value = item.api_style;
  $('#model-timeout').value = item.timeout_seconds; $('#model-priority').value = item.priority;
  $('#model-api-key').value = ''; $('#model-enabled').checked = item.enabled;
  $('#model-secret-state').textContent = `API Key：${item.api_key_configured ? '已配置' : '未配置'}。输入新值才会替换。`;
  $('#delete-model').hidden = item.model_connection_id === 'default';
  setFormState('#model-form-state', '');
}

function newModelConnection() {
  editingModelConnectionId = ''; $('#model-config-form').reset(); $('#model-config-form').hidden = false;
  $('#model-id').disabled = false; $('#model-timeout').value = 30; $('#model-priority').value = 100;
  $('#model-api-style').value = 'chat_completions'; $('#model-enabled').checked = true;
  $('#delete-model').hidden = true; $('#model-secret-state').textContent = '新连接需要配置 API Key。';
  setFormState('#model-form-state', '');
}

function modelConnectionPayload() {
  return {
    model_connection_id: $('#model-id').value.trim(), name: $('#model-name').value.trim(),
    base_url: $('#model-base-url').value.trim(), api_key: $('#model-api-key').value || null,
    model: $('#model-name-api').value.trim(), api_style: $('#model-api-style').value,
    timeout_seconds: Number($('#model-timeout').value || 30), priority: Number($('#model-priority').value || 100),
    enabled: $('#model-enabled').checked,
  };
}

$('#new-model').addEventListener('click', newModelConnection);
$('#cancel-model').addEventListener('click', () => { $('#model-config-form').hidden = true; editingModelConnectionId = ''; });
$('#model-config-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = submitButton(event); button.disabled = true;
  try {
    const payload = modelConnectionPayload(); const updating = Boolean(editingModelConnectionId);
    await apiJson(updating ? `/api/admin/model-connections/${encodeURIComponent(editingModelConnectionId)}` : '/api/admin/model-connections', {
      method: updating ? 'PUT' : 'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)
    });
    editingModelConnectionId = payload.model_connection_id;
    await loadModelConnections(editingModelConnectionId);
    setFormState('#model-form-state', '模型连接已保存并立即生效');
  } catch (error) { setFormState('#model-form-state', error.message, true); } finally { button.disabled = false; }
});
$('#test-model').addEventListener('click', async event => {
  event.currentTarget.disabled = true; setFormState('#model-form-state', '正在请求模型…');
  try {
    const result = await apiJson('/api/admin/model-connections/test', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(modelConnectionPayload())});
    setFormState('#model-form-state', `${result.message}${Number.isInteger(result.latency_ms) ? ` · ${result.latency_ms} ms` : ''}`, !result.ok);
  } catch (error) { setFormState('#model-form-state', error.message, true); } finally { event.currentTarget.disabled = false; }
});
$('#delete-model').addEventListener('click', async () => {
  if (!editingModelConnectionId || !confirm('删除这个模型连接？')) return;
  try {
    await apiJson(`/api/admin/model-connections/${encodeURIComponent(editingModelConnectionId)}`, {method:'DELETE'});
    editingModelConnectionId = ''; $('#model-config-form').hidden = true; await loadModelConnections();
  } catch (error) { setFormState('#model-form-state', error.message, true); }
});

async function loadAgentAdmin(selectId = '') {
  [agentConfigs, knowledgeBases, modelConnections] = await Promise.all([
    apiJson('/api/admin/agents'), apiJson('/api/admin/knowledge-bases'), apiJson('/api/admin/model-connections')
  ]);
  $('#agent-admin-list').innerHTML = agentConfigs.map(item => `
    <button type="button" class="admin-list-item ${item.agent_id === (selectId || editingAgentId) ? 'selected' : ''}" data-agent-id="${escapeHtml(item.agent_id)}">
      <strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(knowledgeBases.find(kb => kb.knowledge_base_id === item.knowledge_base_id)?.name || item.knowledge_base_id)}</span>
      <small>${item.enabled ? '已启用' : '已停用'}${item.is_default ? ' · 默认' : ''}</small>
    </button>`).join('');
  document.querySelectorAll('[data-agent-id]').forEach(button => button.addEventListener('click', () => editAgent(button.dataset.agentId)));
  renderKnowledgeBaseOptions(); renderToolOptions([]); renderAgentModelOptions([], []);
  if (selectId) editAgent(selectId);
}

function renderKnowledgeBaseOptions(selected = '') {
  $('#agent-kb').innerHTML = knowledgeBases.map(item => `<option value="${escapeHtml(item.knowledge_base_id)}" ${item.knowledge_base_id === selected ? 'selected' : ''}>${escapeHtml(item.name)}${item.enabled ? '' : '（已停用）'}</option>`).join('');
}

function renderToolOptions(selected) {
  const enabled = new Set(selected);
  $('#agent-tools').innerHTML = availableTools.map(([id, label]) => `<label><input type="checkbox" value="${id}" ${enabled.has(id) ? 'checked' : ''}> <span><strong>${id}</strong><small>${label}</small></span></label>`).join('');
}

function renderAgentModelOptions(plannerSelected = [], answerSelected = []) {
  const enabled = modelConnections.filter(item => item.enabled).sort((a, b) => a.priority - b.priority || a.name.localeCompare(b.name));
  const render = (selector, selected, items) => {
    const selectedIds = new Set(selected.length ? selected : ['default']);
    $(selector).innerHTML = items.map(item => `<option value="${escapeHtml(item.model_connection_id)}" ${selectedIds.has(item.model_connection_id) ? 'selected' : ''}>${escapeHtml(item.name)} · ${escapeHtml(item.model)} · P${item.priority}</option>`).join('');
  };
  render('#agent-planner-models', plannerSelected, enabled.filter(item => item.api_style === 'chat_completions'));
  render('#agent-answer-models', answerSelected, enabled);
}

function renderGlossaryDocuments() {
  const list = $('#agent-glossary-list');
  list.innerHTML = glossaryDraft.length ? glossaryDraft.map((item, index) => `
    <div class="glossary-item" data-glossary-index="${index}">
      <div><strong>${escapeHtml(item.name)}</strong><small>${item.entries?.length ? `${item.entries.length.toLocaleString()} 条词条` : `${(item.markdown || '').length.toLocaleString()} 字符`}</small><label><input type="checkbox" data-glossary-enabled="${index}" ${item.enabled !== false ? 'checked' : ''}>启用</label></div>
      <div class="glossary-actions">
        <button type="button" data-glossary-up="${index}" title="上移" aria-label="上移" ${index === 0 ? 'disabled' : ''}>↑</button>
        <button type="button" data-glossary-down="${index}" title="下移" aria-label="下移" ${index === glossaryDraft.length - 1 ? 'disabled' : ''}>↓</button>
        <button type="button" data-glossary-remove="${index}" title="删除" aria-label="删除">×</button>
      </div>
    </div>`).join('') : '<p class="empty-state">尚未配置行业术语字典</p>';
  list.querySelectorAll('[data-glossary-up]').forEach(button => button.addEventListener('click', () => {
    const index = Number(button.dataset.glossaryUp); [glossaryDraft[index - 1], glossaryDraft[index]] = [glossaryDraft[index], glossaryDraft[index - 1]]; renderGlossaryDocuments();
  }));
  list.querySelectorAll('[data-glossary-enabled]').forEach(input => input.addEventListener('change', () => { glossaryDraft[Number(input.dataset.glossaryEnabled)].enabled = input.checked; }));
  list.querySelectorAll('[data-glossary-down]').forEach(button => button.addEventListener('click', () => {
    const index = Number(button.dataset.glossaryDown); [glossaryDraft[index + 1], glossaryDraft[index]] = [glossaryDraft[index], glossaryDraft[index + 1]]; renderGlossaryDocuments();
  }));
  list.querySelectorAll('[data-glossary-remove]').forEach(button => button.addEventListener('click', () => {
    glossaryDraft.splice(Number(button.dataset.glossaryRemove), 1); renderGlossaryDocuments();
  }));
}

$('#agent-glossary-files').addEventListener('change', async event => {
  const files = [...event.target.files];
  if (glossaryDraft.length + files.length > 20) { setFormState('#agent-form-state', '行业字典最多 20 份', true); event.target.value = ''; return; }
  for (const file of files) {
    if (file.name.toLowerCase().endsWith('.json')) {
      try {
        const value = JSON.parse(await file.text());
        if (!Array.isArray(value.entries) || !value.entries.length) throw new Error('需要非空 entries 数组');
        for (const [index, entry] of value.entries.entries()) {
          if (typeof entry.term !== 'string' || !entry.term.trim() || !Array.isArray(entry.aliases) || entry.aliases.some(alias => typeof alias !== 'string' || !alias.trim())) throw new Error(`第 ${index + 1} 条需要 term 和字符串数组 aliases`);
        }
        const document = {name: file.name, entries: value.entries, enabled: true};
        const existing = glossaryDraft.findIndex(item => item.name === file.name);
        if (existing >= 0) glossaryDraft[existing] = document; else glossaryDraft.push(document);
      } catch (error) { setFormState('#agent-form-state', `${file.name}：${error.message}`, true); }
      continue;
    }
    if (!file.name.toLowerCase().endsWith('.md')) { setFormState('#agent-form-state', `${file.name} 不是 Markdown 文件`, true); continue; }
    const markdown = await file.text();
    if (!markdown.trim()) { setFormState('#agent-form-state', `${file.name} 内容为空`, true); continue; }
    if (markdown.length > 100000) { setFormState('#agent-form-state', `${file.name} 超过 100000 字符`, true); continue; }
    const existing = glossaryDraft.findIndex(item => item.name === file.name);
    const value = {name: file.name, markdown};
    if (existing >= 0) glossaryDraft[existing] = value; else glossaryDraft.push(value);
  }
  event.target.value = ''; renderGlossaryDocuments();
});

function editAgent(id) {
  const item = agentConfigs.find(value => value.agent_id === id); if (!item) return;
  editingAgentId = id; $('#agent-config-form').hidden = false;
  $('#agent-id').value = item.agent_id; $('#agent-id').disabled = true;
  $('#agent-name').value = item.name; $('#agent-config-description').value = item.description;
  renderKnowledgeBaseOptions(item.knowledge_base_id); renderToolOptions(item.allowed_tools);
  $('#agent-max-tools').value = item.max_tool_calls; $('#agent-max-rounds').value = item.max_retrieval_rounds;
  $('#agent-max-evidence').value = item.max_evidence_for_answer; $('#agent-enabled').checked = item.enabled;
  $('#agent-default').checked = item.is_default; $('#agent-prompt').value = item.planner_prompt;
  renderAgentModelOptions(item.planner_model_ids || [], item.answer_model_ids || []);
  glossaryDraft = (item.glossary_documents || []).map(value => ({...value})); renderGlossaryDocuments();
  $('#delete-agent').hidden = item.is_default; setFormState('#agent-form-state', ''); loadAgentAdminSelection();
}

function loadAgentAdminSelection() {
  document.querySelectorAll('[data-agent-id]').forEach(button => button.classList.toggle('selected', button.dataset.agentId === editingAgentId));
}

function newAgent() {
  editingAgentId = ''; $('#agent-config-form').reset(); $('#agent-config-form').hidden = false;
  $('#agent-id').disabled = false; $('#agent-enabled').checked = true; $('#delete-agent').hidden = true;
  glossaryDraft = []; renderGlossaryDocuments();
  renderAgentModelOptions(['default'], ['default']);
  renderKnowledgeBaseOptions(); renderToolOptions(['vector_search','grep_files','keyword_search','find_files','search_within_document','metadata_search','dify_metadata_search','read_full_document','get_adjacent_chunks','get_document_outline']);
  setFormState('#agent-form-state', ''); loadAgentAdminSelection();
}

function agentPayload() {
  return {
    agent_id: $('#agent-id').value.trim(), name: $('#agent-name').value.trim(),
    description: $('#agent-config-description').value.trim(), knowledge_base_id: $('#agent-kb').value,
    planner_prompt: $('#agent-prompt').value.trim(),
    planner_model_ids: [...$('#agent-planner-models').selectedOptions].map(option => option.value),
    answer_model_ids: [...$('#agent-answer-models').selectedOptions].map(option => option.value),
    glossary_documents: glossaryDraft.map(value => ({...value})),
    allowed_tools: [...document.querySelectorAll('#agent-tools input:checked')].map(input => input.value),
    max_tool_calls: Number($('#agent-max-tools').value), max_retrieval_rounds: Number($('#agent-max-rounds').value),
    max_evidence_for_answer: Number($('#agent-max-evidence').value), enabled: $('#agent-enabled').checked,
    is_default: $('#agent-default').checked,
  };
}

$('#new-agent').addEventListener('click', newAgent);
$('#cancel-agent').addEventListener('click', () => { $('#agent-config-form').hidden = true; editingAgentId = ''; loadAgentAdminSelection(); });
$('#agent-config-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = submitButton(event); button.disabled = true;
  try {
    const payload = agentPayload(); const updating = Boolean(editingAgentId);
    await apiJson(updating ? `/api/admin/agents/${encodeURIComponent(editingAgentId)}` : '/api/admin/agents', {
      method: updating ? 'PUT' : 'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)
    });
    editingAgentId = payload.agent_id; setFormState('#agent-form-state', 'Agent 已保存并立即生效');
    await loadAgents(); await loadAgentAdmin(editingAgentId);
  } catch (error) { setFormState('#agent-form-state', error.message, true); } finally { button.disabled = false; }
});
$('#delete-agent').addEventListener('click', async () => {
  if (!editingAgentId || !confirm('删除这个 Agent 配置？知识库与本地资料会保留。')) return;
  try {
    await apiJson(`/api/admin/agents/${encodeURIComponent(editingAgentId)}`, {method:'DELETE'});
    editingAgentId = ''; $('#agent-config-form').hidden = true; await loadAgents(); await loadAgentAdmin();
  } catch (error) { setFormState('#agent-form-state', error.message, true); }
});
