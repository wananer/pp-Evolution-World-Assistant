(function registerEvolutionWorldAssistant() {
  const runtime = window.PlotPilotPlugins;
  if (!runtime) {
    console.warn('[EvolutionWorld] PlotPilotPlugins runtime missing');
    return;
  }

  const pluginName = 'evolution_world_assistant';
  const state = {
    activeTab: 'characters',
    viewMode: 'novel',
    selectedCharacterId: null,
    lastPayload: null,
  };

  function ensurePanel() {
    if (document.getElementById('ewa-panel-button')) return;
    const button = document.createElement('button');
    button.id = 'ewa-panel-button';
    button.type = 'button';
    button.innerHTML = '<span class="ewa-fab-mark">✦</span><span class="ewa-fab-text">EW</span>';
    button.title = 'Evolution World Assistant';
    button.addEventListener('click', openPanel);
    document.body.appendChild(button);
  }

  function ensureDrawer() {
    let drawer = document.getElementById('ewa-drawer');
    if (drawer) return drawer;
    drawer = document.createElement('aside');
    drawer.id = 'ewa-drawer';
    drawer.innerHTML = `
      <div class="ewa-orb ewa-orb-a"></div>
      <div class="ewa-orb ewa-orb-b"></div>
      <header class="ewa-header">
        <div>
          <div class="ewa-kicker">PlotPilot Plugin</div>
          <strong>Evolution World</strong>
          <p>动态角色卡 / 世界演化核心</p>
        </div>
        <button type="button" class="ewa-close" data-close>×</button>
      </header>
      <nav class="ewa-tabs" aria-label="Evolution World tabs">
        <button type="button" data-tab="characters" class="active">角色卡</button>
        <button type="button" data-tab="events">世界线</button>
        <button type="button" data-tab="status">运行态</button>
      </nav>
      <main data-content class="ewa-content">加载中...</main>
      <footer class="ewa-footer">
        <span>Phase 1 · Fact-driven rolecards</span>
        <button type="button" data-refresh>刷新</button>
      </footer>
    `;
    drawer.querySelector('[data-close]').addEventListener('click', () => drawer.classList.remove('open'));
    drawer.querySelector('[data-refresh]').addEventListener('click', openPanel);
    drawer.querySelectorAll('[data-tab]').forEach((tab) => {
      tab.addEventListener('click', () => {
        state.activeTab = tab.dataset.tab;
        drawer.querySelectorAll('[data-tab]').forEach((item) => item.classList.toggle('active', item === tab));
        renderPanel(drawer);
      });
    });
    document.body.appendChild(drawer);
    return drawer;
  }

  async function openPanel() {
    const drawer = ensureDrawer();
    drawer.classList.add('open');
    setLoading(drawer, '正在读取 Evolution World 状态...');
    const novelId = runtime.context.getNovelId();
    if (!novelId) {
      setEmpty(drawer, '未检测到当前小说 ID', '请从带 novel 参数的工作台页面打开，或等待宿主事件同步当前小说。');
      return;
    }
    try {
      const [characters, status] = await Promise.all([
        runtime.fetchJson(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(novelId)}/characters`),
        runtime.fetchJson('/api/v1/plugins/evolution-world/status'),
      ]);
      state.lastPayload = { novelId, characters, status };
      renderPanel(drawer);
    } catch (error) {
      console.warn('[EvolutionWorld] panel request failed:', error);
      setEmpty(drawer, '加载失败', String(error));
    }
  }

  function renderPanel(drawer) {
    if (!state.lastPayload) {
      setLoading(drawer, '暂无数据');
      return;
    }
    if (state.activeTab === 'events') return renderEvents(drawer, state.lastPayload);
    if (state.activeTab === 'status') return renderStatus(drawer, state.lastPayload);
    return renderCharacters(drawer, state.lastPayload);
  }

  function renderCharacters(drawer, payload) {
    const content = drawer.querySelector('[data-content]');
    const items = Array.isArray(payload.characters.items) ? payload.characters.items : [];
    if (!items.length) {
      setEmpty(drawer, '暂无动态角色状态', '提交或重跑章节后，这里会显示角色当前状态、最近动态与关联地点。');
      return;
    }
    content.innerHTML = `
      <section class="ewa-summary-grid">
        <article><b>${items.length}</b><span>角色卡</span></article>
        <article><b>${countEvents(items)}</b><span>动态记录</span></article>
        <article><b>${escapeHtml(payload.novelId)}</b><span>当前小说</span></article>
      </section>
      ${state.viewMode === 'detail' ? renderCharacterDetail(items) : state.viewMode === 'roster' ? renderCharacterRoster(items) : renderNovelCard(payload, items)}
    `;
    bindCharacterInteractions(content);
  }

  function renderNovelCard(payload, items) {
    const coverNames = items.slice(0, 3).map((item) => escapeHtml((item.name || '?').slice(0, 1))).join('');
    return `
      <section class="ewa-section ewa-novel-stage">
        <div class="ewa-section-head">
          <h3>小说卡片</h3>
          <p>先进入小说，再查看人物卡册</p>
        </div>
        <article class="ewa-novel-card" data-open-roster="1">
          <div class="ewa-novel-cover">
            <span>${coverNames || 'EW'}</span>
            <i>Evolution</i>
          </div>
          <div class="ewa-novel-info">
            <p class="ewa-card-label">当前小说</p>
            <h4>${escapeHtml(payload.novelId)}</h4>
            <p>已生成 ${items.length} 张人物卡，记录 ${countEvents(items)} 条章节动态。</p>
            <div class="ewa-chip-row">
              <em>角色卡册</em><em>事实驱动</em><em>点击进入</em>
            </div>
          </div>
        </article>
      </section>
    `;
  }

  function renderCharacterRoster(items) {
    return `
      <section class="ewa-section ewa-roster-stage">
        <button type="button" class="ewa-back" data-back-novel>← 返回小说卡片</button>
        <div class="ewa-section-head">
          <h3>人物卡册</h3>
          <p>选择人物卡查看详情</p>
        </div>
        <div class="ewa-card-roster">
          ${items.map(renderCharacterGameCard).join('')}
        </div>
      </section>
    `;
  }

  function renderCharacterGameCard(item) {
    const latest = (item.recent_events || []).at(-1) || {};
    const locations = Array.isArray(latest.locations) ? latest.locations.slice(0, 3) : [];
    const intro = latest.summary || `${item.name || '角色'}在第${item.last_seen_chapter || '-'}章出现。`;
    return `
      <button type="button" class="ewa-game-card" data-character-id="${escapeAttr(item.character_id || item.name)}">
        <div class="ewa-game-card-art"><span>${escapeHtml((item.name || '?').slice(0, 1))}</span></div>
        <div class="ewa-game-card-body">
          <div class="ewa-role-topline"><h4>${escapeHtml(item.name || item.character_id)}</h4><span>${escapeHtml(item.status || 'active')}</span></div>
          <p class="ewa-role-meta">首次第${item.first_seen_chapter || '-'}章 · 最近第${item.last_seen_chapter || '-'}章</p>
          <p class="ewa-role-event">${escapeHtml(intro)}</p>
          ${locations.length ? `<div class="ewa-chip-row">${locations.map((loc) => `<em>${escapeHtml(loc)}</em>`).join('')}</div>` : ''}
        </div>
      </button>
    `;
  }

  function renderCharacterDetail(items) {
    const item = items.find((entry) => entry.character_id === state.selectedCharacterId) || items[0];
    if (!item) return '';
    const events = item.recent_events || [];
    const latest = events.at(-1) || {};
    const locations = Array.isArray(latest.locations) ? latest.locations.slice(0, 6) : [];
    return `
      <section class="ewa-section ewa-character-detail">
        <button type="button" class="ewa-back" data-back-roster>← 返回人物卡册</button>
        <article class="ewa-character-hero">
          <div class="ewa-character-portrait"><span>${escapeHtml((item.name || '?').slice(0, 1))}</span></div>
          <div>
            <p class="ewa-card-label">人物卡</p>
            <h3>${escapeHtml(item.name || item.character_id)}</h3>
            <p class="ewa-character-intro">${escapeHtml(latest.summary || '暂无简介。')}</p>
            <div class="ewa-chip-row">
              <em>${escapeHtml(item.status || 'active')}</em>
              <em>首次第${item.first_seen_chapter || '-'}章</em>
              <em>最近第${item.last_seen_chapter || '-'}章</em>
              ${locations.map((loc) => `<em>${escapeHtml(loc)}</em>`).join('')}
            </div>
          </div>
        </article>
        <div class="ewa-section-head ewa-detail-head">
          <h3>角色动态</h3>
          <p>按章节倒序显示</p>
        </div>
        <ol class="ewa-timeline">
          ${events.slice().reverse().map((event) => `
            <li><span>第${event.chapter_number || '-'}章</span><strong>${escapeHtml(item.name || '')}</strong><p>${escapeHtml(event.summary || '')}</p></li>
          `).join('') || '<li><p>暂无动态</p></li>'}
        </ol>
      </section>
    `;
  }

  function bindCharacterInteractions(root) {
    root.querySelector('[data-open-roster]')?.addEventListener('click', () => {
      state.viewMode = 'roster';
      renderPanel(document.getElementById('ewa-drawer'));
    });
    root.querySelector('[data-back-novel]')?.addEventListener('click', () => {
      state.viewMode = 'novel';
      state.selectedCharacterId = null;
      renderPanel(document.getElementById('ewa-drawer'));
    });
    root.querySelectorAll('[data-character-id]').forEach((card) => {
      card.addEventListener('click', () => {
        state.viewMode = 'detail';
        state.selectedCharacterId = card.dataset.characterId;
        renderPanel(document.getElementById('ewa-drawer'));
      });
    });
    root.querySelector('[data-back-roster]')?.addEventListener('click', () => {
      state.viewMode = 'roster';
      state.selectedCharacterId = null;
      renderPanel(document.getElementById('ewa-drawer'));
    });
  }

  function renderEvents(drawer, payload) {
    const content = drawer.querySelector('[data-content]');
    const events = (payload.characters.items || [])
      .flatMap((item) => (item.recent_events || []).map((event) => ({ ...event, character: item.name })))
      .sort((a, b) => (b.chapter_number || 0) - (a.chapter_number || 0));
    content.innerHTML = `
      <section class="ewa-section">
        <div class="ewa-section-head">
          <h3>世界线动态</h3>
          <p>按角色活动折叠展示，后续将升级为 WorldEvent</p>
        </div>
        <ol class="ewa-timeline">
          ${events.map((event) => `
            <li>
              <span>第${event.chapter_number || '-'}章</span>
              <strong>${escapeHtml(event.character || '未知角色')}</strong>
              <p>${escapeHtml(event.summary || '')}</p>
            </li>
          `).join('') || '<li><p>暂无事件</p></li>'}
        </ol>
      </section>
    `;
  }

  function renderStatus(drawer, payload) {
    const content = drawer.querySelector('[data-content]');
    const status = payload.status || {};
    const capabilities = Array.isArray(status.capabilities) ? status.capabilities : [];
    content.innerHTML = `
      <section class="ewa-section">
        <div class="ewa-section-head">
          <h3>运行状态</h3>
          <p>插件平台能力与当前阶段</p>
        </div>
        <dl class="ewa-status-list">
          <div><dt>状态</dt><dd>${escapeHtml(status.status || 'unknown')}</dd></div>
          <div><dt>阶段</dt><dd>${escapeHtml(status.phase || '-')}</dd></div>
          <div><dt>版本</dt><dd>${escapeHtml(status.version || '-')}</dd></div>
          <div><dt>Novel</dt><dd>${escapeHtml(payload.novelId)}</dd></div>
        </dl>
        <div class="ewa-chip-row ewa-capabilities">
          ${capabilities.map((item) => `<em>${escapeHtml(item)}</em>`).join('')}
        </div>
      </section>
    `;
  }

  function setLoading(drawer, message) {
    drawer.querySelector('[data-content]').innerHTML = `<div class="ewa-loading"><span></span>${escapeHtml(message)}</div>`;
  }

  function setEmpty(drawer, title, detail) {
    drawer.querySelector('[data-content]').innerHTML = `<div class="ewa-empty"><b>${escapeHtml(title)}</b><p>${escapeHtml(detail || '')}</p></div>`;
  }

  function countEvents(items) {
    return items.reduce((total, item) => total + ((item.recent_events || []).length), 0);
  }

  function escapeHtml(value) {
    return String(value || '').replace(/[&<>"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
  }

  function escapeAttr(value) {
    return escapeHtml(value).replace(/'/g, '&#39;');
  }

  runtime.plugins.register({
    name: pluginName,
    display_name: 'Evolution World Assistant',
    version: '0.1.1',
    async init(ctx) {
      ensurePanel();
      ctx.events.on('chapter:committed', (payload) => {
        ctx.events.emit('evolution-world:chapter_committed_seen', payload);
      });
    },
    async dispose() {
      document.getElementById('ewa-panel-button')?.remove();
      document.getElementById('ewa-drawer')?.remove();
    },
  });
})();
