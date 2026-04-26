(function registerEvolutionWorldAssistant() {
  const runtime = window.PlotPilotPlugins;
  if (!runtime) {
    console.warn('[EvolutionWorld] PlotPilotPlugins runtime missing');
    return;
  }

  const pluginName = 'world_evolution_core';
  const state = {
    activeTab: 'characters',
    viewMode: 'novel',
    selectedCharacterId: null,
    lastPayload: null,
    settingsMessage: '',
    modelFetchMessage: '',
    api2Models: [],
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
        <button type="button" data-tab="routes">路线图</button>
        <button type="button" data-tab="status">运行态</button>
        <button type="button" data-tab="settings">设置</button>
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
      const [characters, status, runs, snapshots, importedFlows, settings, routeMap] = await Promise.all([
        runtime.fetchJson(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(novelId)}/characters`),
        runtime.fetchJson('/api/v1/plugins/evolution-world/status'),
        runtime.fetchJson(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(novelId)}/runs?limit=8`),
        runtime.fetchJson(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(novelId)}/snapshots`),
        runtime.fetchJson(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(novelId)}/imported-flows`),
        runtime.fetchJson('/api/v1/plugins/evolution-world/settings'),
        runtime.fetchJson(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(novelId)}/routes/global`),
      ]);
      state.lastPayload = { novelId, characters, status, runs, snapshots, importedFlows, settings, routeMap };
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
    if (state.activeTab === 'routes') return renderRoutes(drawer, state.lastPayload);
    if (state.activeTab === 'status') return renderStatus(drawer, state.lastPayload);
    if (state.activeTab === 'settings') return renderSettings(drawer, state.lastPayload);
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
    const palette = item.personality_palette || {};
    const paletteChips = [
      palette.base ? `底色 ${palette.base}` : '',
      ...(Array.isArray(palette.main_tones) ? palette.main_tones.slice(0, 2) : []),
    ].filter(Boolean);
    return `
      <button type="button" class="ewa-game-card" data-character-id="${escapeAttr(item.character_id || item.name)}">
        <div class="ewa-game-card-art"><span>${escapeHtml((item.name || '?').slice(0, 1))}</span></div>
        <div class="ewa-game-card-body">
          <div class="ewa-role-topline"><h4>${escapeHtml(item.name || item.character_id)}</h4><span>${escapeHtml(item.status || 'active')}</span></div>
          <p class="ewa-role-meta">首次第${item.first_seen_chapter || '-'}章 · 最近第${item.last_seen_chapter || '-'}章</p>
          <p class="ewa-role-event">${escapeHtml(intro)}</p>
          ${locations.length || paletteChips.length ? `<div class="ewa-chip-row">${[...paletteChips, ...locations].slice(0, 5).map((loc) => `<em>${escapeHtml(loc)}</em>`).join('')}</div>` : ''}
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
        ${renderCharacterProfile(item)}
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

  function renderCharacterProfile(item) {
    return `
      <div class="ewa-profile-grid">
        ${renderAppearanceCard(item.appearance)}
        ${renderRecordsCard('属性', item.attributes, '随小说世界观自由定义')}
        ${renderWorldProfileCard(item.world_profile)}
        ${renderPaletteCard(item.personality_palette)}
      </div>
    `;
  }

  function renderAppearanceCard(appearance) {
    const data = appearance && typeof appearance === 'object' ? appearance : {};
    const features = Array.isArray(data.features) ? data.features : [];
    const style = Array.isArray(data.style) ? data.style : [];
    const marks = Array.isArray(data.marks) ? data.marks : [];
    const hasRealContent = [data.summary, data.current_outfit, ...features, ...style, ...marks].some((value) => value && !String(value).includes('待从正文补充'));
    return `
      <article class="ewa-profile-card">
        <div class="ewa-profile-card-head"><h4>外貌</h4><span>${hasRealContent ? '已记录' : '待补充'}</span></div>
        <p>${escapeHtml(data.summary || '待从正文补充外貌描写。')}</p>
        ${data.current_outfit ? `<dl class="ewa-mini-list"><div><dt>当前装束</dt><dd>${escapeHtml(data.current_outfit)}</dd></div></dl>` : ''}
        ${renderTagBlock('特征', features)}
        ${renderTagBlock('风格', style)}
        ${renderTagBlock('标记', marks)}
      </article>
    `;
  }

  function renderRecordsCard(title, records, emptyText) {
    const items = normalizeRecords(records);
    return `
      <article class="ewa-profile-card">
        <div class="ewa-profile-card-head"><h4>${escapeHtml(title)}</h4><span>${items.length || 'open'}</span></div>
        ${items.length ? renderRecordList(items) : `<p>${escapeHtml(emptyText || '暂无记录。')}</p>`}
      </article>
    `;
  }

  function renderWorldProfileCard(profile) {
    const data = profile && typeof profile === 'object' ? profile : {};
    const schemaName = data.schema_name || '通用角色档案';
    return `
      <article class="ewa-profile-card">
        <div class="ewa-profile-card-head"><h4>世界观字段</h4><span>${escapeHtml(schemaName)}</span></div>
        ${normalizeRecords(data.fields).length ? renderRecordList(normalizeRecords(data.fields)) : '<p>本书可自定义修为、职业、阵营、关系、危险等级等字段。</p>'}
      </article>
    `;
  }

  function renderPaletteCard(palette) {
    const data = palette && typeof palette === 'object' ? palette : {};
    const mainTones = Array.isArray(data.main_tones) ? data.main_tones : [];
    const accents = Array.isArray(data.accents) ? data.accents : [];
    const derivatives = Array.isArray(data.derivatives) ? data.derivatives : [];
    return `
      <article class="ewa-profile-card ewa-palette-card">
        <div class="ewa-profile-card-head"><h4>性格调色盘</h4><span>${derivatives.length} 衍生</span></div>
        <p>${escapeHtml(data.metaphor || '人的性格像调色盘：底色、主色调与点缀共同驱动行为。')}</p>
        <div class="ewa-palette-strip">
          ${data.base ? `<div><b>底色</b><span>${escapeHtml(data.base)}</span></div>` : ''}
          ${mainTones.length ? `<div><b>主色调</b><span>${mainTones.map(escapeHtml).join('、')}</span></div>` : ''}
          ${accents.length ? `<div><b>点缀</b><span>${accents.map(escapeHtml).join('、')}</span></div>` : ''}
        </div>
        ${derivatives.length ? `
          <ol class="ewa-derivative-list">
            ${derivatives.map((item) => `
              <li>
                <span>${escapeHtml(item.tone || '衍生')}${item.future ? ' · 未来' : ''}</span>
                <strong>${escapeHtml(item.title || item.tone || '行为衍生')}</strong>
                <p>${escapeHtml(item.description || '')}</p>
                ${item.trigger || item.visibility ? `<em>${escapeHtml([item.trigger, item.visibility].filter(Boolean).join(' · '))}</em>` : ''}
              </li>
            `).join('')}
          </ol>
        ` : '<p>暂无性格衍生记录。</p>'}
      </article>
    `;
  }

  function renderTagBlock(label, items) {
    if (!Array.isArray(items) || !items.length) return '';
    return `<div class="ewa-tag-block"><b>${escapeHtml(label)}</b><div class="ewa-chip-row">${items.map((item) => `<em>${escapeHtml(item)}</em>`).join('')}</div></div>`;
  }

  function renderRecordList(items) {
    return `
      <dl class="ewa-mini-list">
        ${items.map((item) => `
          <div>
            <dt>${escapeHtml(item.name)}</dt>
            <dd>${escapeHtml(item.value)}${item.description ? `<small>${escapeHtml(item.description)}</small>` : ''}</dd>
          </div>
        `).join('')}
      </dl>
    `;
  }

  function normalizeRecords(records) {
    if (!Array.isArray(records)) return [];
    return records
      .map((item) => {
        if (typeof item === 'string') return { name: '属性', value: item, description: '' };
        if (!item || typeof item !== 'object') return null;
        return {
          name: item.name || item.category || '属性',
          value: item.value || '',
          description: item.description || '',
        };
      })
      .filter((item) => item && item.name && item.value);
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

  function renderRoutes(drawer, payload) {
    const content = drawer.querySelector('[data-content]');
    const routeMap = payload.routeMap || {};
    const nodes = Array.isArray(routeMap.nodes) ? routeMap.nodes : [];
    const edges = Array.isArray(routeMap.edges) ? routeMap.edges : [];
    const characters = Array.isArray(routeMap.characters) ? routeMap.characters : [];
    const meetings = Array.isArray(routeMap.meetings) ? routeMap.meetings : [];
    const conflicts = Array.isArray(routeMap.conflicts) ? routeMap.conflicts : [];
    const aggregate = routeMap.aggregate || {};
    content.innerHTML = `
      <section class="ewa-summary-grid">
        <article><b>${aggregate.location_count || nodes.length}</b><span>地点</span></article>
        <article><b>${aggregate.route_edge_count || edges.length}</b><span>路线</span></article>
        <article><b>${aggregate.conflict_count || conflicts.length}</b><span>风险</span></article>
      </section>
      <section class="ewa-section">
        <div class="ewa-section-head">
          <h3>全局人物路线图</h3>
          <p>不同颜色代表不同人物，交汇点代表同章同地相遇</p>
        </div>
        ${renderRouteCanvas(nodes, edges, characters, meetings)}
      </section>
      <section class="ewa-section ewa-route-meta">
        <div class="ewa-section-head">
          <h3>路线风险</h3>
          <p>用于审查重复进入、缺少转场和位置跳跃</p>
        </div>
        <ol class="ewa-timeline">
          ${conflicts.slice(-8).reverse().map((item) => `
            <li>
              <span>${escapeHtml(item.severity || 'warning')} · 第${escapeHtml(item.chapter_current || '-')}章</span>
              <strong>${escapeHtml(item.character || item.type || '路线')}</strong>
              <p>${escapeHtml(item.message || '')}</p>
            </li>
          `).join('') || '<li><p>暂无路线风险</p></li>'}
        </ol>
      </section>
      <section class="ewa-section ewa-route-meta">
        <div class="ewa-section-head">
          <h3>向量胶囊</h3>
          <p>压缩事实索引，后续可挂接真实 embedding</p>
        </div>
        <dl class="ewa-status-list">
          <div><dt>模式</dt><dd>${escapeHtml(routeMap.vector_index?.mode || '-')}</dd></div>
          <div><dt>条目</dt><dd>${escapeHtml(routeMap.vector_index?.count || 0)}</dd></div>
        </dl>
      </section>
    `;
  }

  function renderRouteCanvas(nodes, edges, characters, meetings) {
    if (!nodes.length) {
      return '<p class="ewa-empty-inline">暂无路线图数据。提交或重跑章节后会生成。</p>';
    }
    const colorByCharacter = new Map(characters.map((item) => [item.name, item.color]));
    const nodeById = new Map(nodes.map((item) => [item.location_id, item]));
    const lines = edges
      .map((edge) => {
        const from = nodeById.get(edge.from_location_id);
        const to = nodeById.get(edge.to_location_id);
        if (!from || !to) return '';
        const color = colorByCharacter.get(edge.character) || '#64748b';
        const x1 = Number(from.x || 0.5) * 100;
        const y1 = Number(from.y || 0.5) * 100;
        const x2 = Number(to.x || 0.5) * 100;
        const y2 = Number(to.y || 0.5) * 100;
        return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${escapeAttr(color)}" stroke-width="2.5" stroke-linecap="round"><title>${escapeHtml(edge.character || '')} 第${escapeHtml(edge.chapter_start || '-')}章 ${escapeHtml(edge.from_location || '')} -> ${escapeHtml(edge.to_location || '')}</title></line>`;
      })
      .join('');
    const circles = nodes.map((node) => {
      const x = Number(node.x || 0.5) * 100;
      const y = Number(node.y || 0.5) * 100;
      return `<g><circle cx="${x}" cy="${y}" r="3.8" fill="#f8fafc" stroke="#0f172a" stroke-width="1.2"></circle><text x="${x + 1.8}" y="${y - 1.8}" class="ewa-route-label">${escapeHtml(node.name || '')}</text></g>`;
    }).join('');
    const meetingMarks = meetings.map((meeting) => {
      const node = nodeById.get(meeting.location_id);
      if (!node) return '';
      const x = Number(node.x || 0.5) * 100;
      const y = Number(node.y || 0.5) * 100;
      return `<circle cx="${x}" cy="${y}" r="6.4" fill="none" stroke="#f59e0b" stroke-width="2"><title>${escapeHtml((meeting.characters || []).join('、'))}</title></circle>`;
    }).join('');
    const legend = characters.map((item) => `<span><i style="background:${escapeAttr(item.color || '#64748b')}"></i>${escapeHtml(item.name || '')}</span>`).join('');
    return `
      <div class="ewa-route-map">
        <svg viewBox="0 0 100 100" role="img" aria-label="Evolution route map">
          ${lines}
          ${meetingMarks}
          ${circles}
        </svg>
      </div>
      <div class="ewa-route-legend">${legend || '<span>暂无人物路线</span>'}</div>
    `;
  }

  function renderStatus(drawer, payload) {
    const content = drawer.querySelector('[data-content]');
    const status = payload.status || {};
    const capabilities = Array.isArray(status.capabilities) ? status.capabilities : [];
    const runs = Array.isArray(payload.runs?.items) ? payload.runs.items.slice().reverse() : [];
    const snapshots = Array.isArray(payload.snapshots?.items) ? payload.snapshots.items : [];
    const importedFlows = Array.isArray(payload.importedFlows?.flows) ? payload.importedFlows.flows : [];
    const unsupported = Array.isArray(payload.importedFlows?.unsupported) ? payload.importedFlows.unsupported : [];
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
          <div><dt>快照</dt><dd>${snapshots.length} 章</dd></div>
          <div><dt>导入流</dt><dd>${importedFlows.length} 个</dd></div>
        </dl>
        <div class="ewa-chip-row ewa-capabilities">
          ${capabilities.map((item) => `<em>${escapeHtml(item)}</em>`).join('')}
        </div>
      </section>
      <section class="ewa-section ewa-run-section">
        <div class="ewa-section-head">
          <h3>ST 预设导入</h3>
          <p>转换为 PlotPilot 声明式流程；EJS 与世界书写入只标记不执行</p>
        </div>
        <textarea class="ewa-preset-input" data-st-preset-input spellcheck="false" placeholder='粘贴 SillyTavern / Evolution preset JSON，例如 {"name":"Flow","prompts":[...]}'></textarea>
        <div class="ewa-action-row">
          <button type="button" class="ewa-mini-action" data-import-st-preset>转换并保存</button>
          <span class="ewa-import-message" data-import-message></span>
        </div>
        <div class="ewa-flow-list">
          ${importedFlows.map((flow) => renderImportedFlowCard(flow)).join('') || '<p class="ewa-empty-inline">暂无导入流程</p>'}
        </div>
        ${unsupported.length ? `<div class="ewa-warning-box"><b>全局不兼容项</b><p>${unsupported.map((item) => escapeHtml(item)).join('、')}</p></div>` : ''}
      </section>
      <section class="ewa-section ewa-run-section">
        <div class="ewa-section-head">
          <h3>运行记录</h3>
          <p>最近 ${runs.length} 次 · 点击查看详情</p>
        </div>
        <ol class="ewa-run-list">
          ${runs.map((run, index) => `
            <li>
              <button type="button" class="ewa-run-card" data-run-index="${index}">
                <strong>${escapeHtml(run.hook_name || '-')}</strong>
                <span>${escapeHtml(run.status || '-')} · 第${escapeHtml(run.chapter_number || '-')}章 · ${escapeHtml(run.trigger_type || '-')}</span>
                <em>${escapeHtml(run.output?.extraction_source || 'audited')}</em>
              </button>
            </li>
          `).join('') || '<li><span>暂无运行记录</span></li>'}
        </ol>
        <div data-run-detail class="ewa-detail-box"></div>
      </section>
      <section class="ewa-section ewa-run-section">
        <div class="ewa-section-head">
          <h3>章节快照</h3>
          <p>查看详情、回滚后重建人物卡</p>
        </div>
        <div class="ewa-action-row">
          <button type="button" class="ewa-mini-action" data-rebuild-derived>重建派生人物卡</button>
        </div>
        <div class="ewa-snapshot-grid">
          ${snapshots.map((snapshot, index) => `
            <button type="button" class="ewa-snapshot-card" data-snapshot-index="${index}">
              <b>第${escapeHtml(snapshot.chapter_number)}章</b>
              <span>${escapeHtml((snapshot.characters || []).join('、') || '无角色')}</span>
            </button>
          `).join('') || '<p class="ewa-empty-inline">暂无快照</p>'}
        </div>
        <div data-snapshot-detail class="ewa-detail-box"></div>
      </section>
    `;
    bindStatusInteractions(content, runs, snapshots);
  }

  function renderSettings(drawer, payload) {
    const content = drawer.querySelector('[data-content]');
    const settings = payload.settings?.settings || {};
    const api2 = settings.api2_control_card || {};
    const custom = api2.custom_profile || {};
    const providerMode = api2.provider_mode || 'same_as_main';
    content.innerHTML = `
      <section class="ewa-section ewa-settings-section">
        <div class="ewa-section-head">
          <h3>API2 控制卡</h3>
          <p>压缩 Evolution 上下文，减轻正文 API 负担</p>
        </div>
        <form class="ewa-settings-form" data-api2-settings-form>
          <label class="ewa-switch-row">
            <input type="checkbox" name="enabled" ${api2.enabled ? 'checked' : ''}>
            <span>启用 API2 写作控制卡</span>
          </label>
          <fieldset class="ewa-fieldset">
            <legend>调用方式</legend>
            <label><input type="radio" name="provider_mode" value="same_as_main" ${providerMode !== 'custom' ? 'checked' : ''}> 与主 API 使用同一配置</label>
            <label><input type="radio" name="provider_mode" value="custom" ${providerMode === 'custom' ? 'checked' : ''}> 使用 Evolution 自定义 API</label>
          </fieldset>
          <div class="ewa-form-grid" data-api2-custom-fields>
            <label>协议
              <select name="protocol">
                ${['openai', 'anthropic', 'gemini'].map((item) => `<option value="${item}" ${custom.protocol === item ? 'selected' : ''}>${item}</option>`).join('')}
              </select>
            </label>
            <label>Base URL
              <input name="base_url" value="${escapeAttr(custom.base_url || '')}" placeholder="https://api.example.com/v1">
            </label>
            <div class="ewa-model-field">
              <span>模型名</span>
              <div class="ewa-model-picker">
                <input name="model" data-api2-model-input list="ewa-api2-model-options" value="${escapeAttr(custom.model || '')}" placeholder="用于压缩控制卡的模型">
                <button type="button" class="ewa-mini-action" data-fetch-api2-models>获取模型</button>
              </div>
              <datalist id="ewa-api2-model-options">
                ${state.api2Models.map((item) => `<option value="${escapeAttr(item.id || item.name || item)}"></option>`).join('')}
              </datalist>
              <select data-api2-model-select>
                <option value="">选择已获取模型</option>
                ${state.api2Models.map((item) => {
                  const modelId = item.id || item.name || item;
                  return `<option value="${escapeAttr(modelId)}" ${modelId === custom.model ? 'selected' : ''}>${escapeHtml(modelId)}</option>`;
                }).join('')}
              </select>
            </div>
            <label>API Key
              <input name="api_key" type="password" value="" placeholder="${custom.api_key_configured ? '已保存，留空则继续使用' : '输入自定义 API Key'}">
            </label>
            <label>温度
              <input name="temperature" type="number" min="0" max="2" step="0.1" value="${escapeAttr(custom.temperature ?? api2.temperature ?? 0.2)}">
            </label>
            <label>最大输出 Token
              <input name="max_tokens" type="number" min="256" max="4096" step="1" value="${escapeAttr(custom.max_tokens ?? api2.max_tokens ?? 1400)}">
            </label>
            <label>超时秒数
              <input name="timeout_seconds" type="number" min="10" max="900" step="10" value="${escapeAttr(custom.timeout_seconds ?? 180)}">
            </label>
          </div>
          <div class="ewa-action-row">
            <button type="submit" class="ewa-mini-action">保存设置</button>
            <button type="button" class="ewa-mini-action" data-test-api2-connection>测试连接</button>
            <span class="ewa-import-message" data-settings-message>${escapeHtml(state.settingsMessage || '')}</span>
            <span class="ewa-import-message" data-model-fetch-message>${escapeHtml(state.modelFetchMessage || '')}</span>
          </div>
        </form>
      </section>
    `;
    bindSettingsInteractions(content);
  }

  function bindSettingsInteractions(root) {
    const form = root.querySelector('[data-api2-settings-form]');
    if (!form) return;
    const syncCustomVisibility = () => {
      const mode = form.querySelector('input[name="provider_mode"]:checked')?.value || 'same_as_main';
      form.querySelector('[data-api2-custom-fields]')?.classList.toggle('is-muted', mode !== 'custom');
    };
    form.querySelectorAll('input[name="provider_mode"]').forEach((item) => item.addEventListener('change', syncCustomVisibility));
    syncCustomVisibility();
    form.querySelector('[data-api2-model-select]')?.addEventListener('change', (event) => {
      const value = event.currentTarget.value || '';
      if (value && form.elements.model) form.elements.model.value = value;
    });
    form.querySelector('[data-fetch-api2-models]')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      const message = form.querySelector('[data-model-fetch-message]');
      button.disabled = true;
      button.textContent = '获取中...';
      state.modelFetchMessage = '';
      if (message) message.textContent = '';
      try {
        const response = await fetch('/api/v1/plugins/evolution-world/settings/models', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(buildApi2SettingsPayload(form)),
        });
        if (!response.ok) throw new Error(await readErrorDetail(response, `模型拉取失败：${response.status}`));
        const result = await response.json();
        state.api2Models = Array.isArray(result.items) ? result.items : [];
        state.modelFetchMessage = state.api2Models.length ? `已获取 ${state.api2Models.length} 个模型。` : '未获取到模型。';
        const currentModel = form.elements.model?.value || '';
        updateApi2ModelChoices(form, currentModel);
        if (message) message.textContent = state.modelFetchMessage;
      } catch (error) {
        state.modelFetchMessage = String(error);
        if (message) message.textContent = state.modelFetchMessage;
      } finally {
        button.disabled = false;
        button.textContent = '获取模型';
      }
    });
    form.querySelector('[data-test-api2-connection]')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      const message = form.querySelector('[data-settings-message]');
      button.disabled = true;
      button.textContent = '测试中...';
      state.settingsMessage = '';
      if (message) message.textContent = '';
      try {
        const response = await fetch('/api/v1/plugins/evolution-world/settings/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(buildApi2SettingsPayload(form)),
        });
        if (!response.ok) throw new Error(await readErrorDetail(response, `连接测试失败：${response.status}`));
        const result = await response.json();
        if (!result.ok) throw new Error(result.error || '连接测试失败');
        state.settingsMessage = `连接成功：${result.model || '当前模型'} · ${result.latency_ms}ms`;
      } catch (error) {
        state.settingsMessage = String(error);
      } finally {
        button.disabled = false;
        button.textContent = '测试连接';
        if (message) message.textContent = state.settingsMessage;
      }
    });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      const message = form.querySelector('[data-settings-message]');
      const payload = buildApi2SettingsPayload(form);
      button.disabled = true;
      button.textContent = '保存中...';
      try {
        const response = await fetch('/api/v1/plugins/evolution-world/settings', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (!response.ok) throw new Error(`Settings save failed: ${response.status}`);
        const saved = await response.json();
        state.lastPayload.settings = saved;
        state.settingsMessage = '已保存。下一次上下文注入生效。';
      } catch (error) {
        state.settingsMessage = String(error);
      } finally {
        button.disabled = false;
        button.textContent = '保存设置';
        message.textContent = state.settingsMessage;
        window.setTimeout(() => {
          state.settingsMessage = '';
          if (message) message.textContent = '';
        }, 3200);
      }
    });
  }

  function buildApi2SettingsPayload(form) {
    const mode = form.querySelector('input[name="provider_mode"]:checked')?.value || 'same_as_main';
    return {
      api2_control_card: {
        enabled: Boolean(form.elements.enabled?.checked),
        provider_mode: mode,
        temperature: Number(form.elements.temperature?.value || 0.2),
        max_tokens: Number(form.elements.max_tokens?.value || 1400),
        custom_profile: {
          protocol: form.elements.protocol?.value || 'openai',
          base_url: form.elements.base_url?.value || '',
          api_key: form.elements.api_key?.value || '',
          model: form.elements.model?.value || '',
          temperature: Number(form.elements.temperature?.value || 0.2),
          max_tokens: Number(form.elements.max_tokens?.value || 1400),
          timeout_seconds: Number(form.elements.timeout_seconds?.value || 180),
        },
      },
      timeout_ms: 30000,
    };
  }

  function updateApi2ModelChoices(form, selectedModel) {
    const datalist = form.querySelector('#ewa-api2-model-options');
    const select = form.querySelector('[data-api2-model-select]');
    const options = state.api2Models
      .map((item) => item.id || item.name || item)
      .filter(Boolean);
    if (datalist) {
      datalist.innerHTML = options.map((modelId) => `<option value="${escapeAttr(modelId)}"></option>`).join('');
    }
    if (select) {
      select.innerHTML = `
        <option value="">${options.length ? '选择已获取模型' : '暂无已获取模型'}</option>
        ${options.map((modelId) => `<option value="${escapeAttr(modelId)}" ${modelId === selectedModel ? 'selected' : ''}>${escapeHtml(modelId)}</option>`).join('')}
      `;
    }
  }

  function renderImportedFlowCard(flow) {
    const unsupported = Array.isArray(flow.unsupported) ? flow.unsupported : [];
    const prompts = Array.isArray(flow.prompt_order) ? flow.prompt_order : [];
    const regexRules = Array.isArray(flow.regex_rules) ? flow.regex_rules : [];
    return `
      <article class="ewa-flow-card">
        <div class="ewa-role-topline"><h4>${escapeHtml(flow.name || flow.id || 'Imported Flow')}</h4><span>${flow.enabled === false ? 'disabled' : 'enabled'}</span></div>
        <p class="ewa-role-meta">${escapeHtml(flow.trigger || 'after_commit')} · prompts ${prompts.length} · regex ${regexRules.length}</p>
        <div class="ewa-chip-row">
          ${(prompts.slice(0, 5)).map((item) => `<em>${escapeHtml(item.name || item.identifier || 'prompt')}</em>`).join('')}
        </div>
        ${unsupported.length ? `<p class="ewa-flow-warning">不兼容：${unsupported.map((item) => escapeHtml(item)).join('、')}</p>` : ''}
      </article>
    `;
  }

  function bindStatusInteractions(root, runs, snapshots) {
    root.querySelectorAll('[data-run-index]').forEach((button) => {
      button.addEventListener('click', () => {
        const run = runs[Number(button.dataset.runIndex)] || {};
        const output = run.output || {};
        root.querySelector('[data-run-detail]').innerHTML = `
          <article class="ewa-inspector-card">
            <div class="ewa-role-topline"><h4>${escapeHtml(run.hook_name || '运行记录')}</h4><span>${escapeHtml(run.status || '-')}</span></div>
            <p class="ewa-role-meta">触发：${escapeHtml(run.trigger_type || '-')} · 第${escapeHtml(run.chapter_number || '-')}章 · ${escapeHtml(run.duration_ms ?? '-')}ms</p>
            <dl class="ewa-status-list">
              <div><dt>抽取来源</dt><dd>${escapeHtml(output.extraction_source || '-')}</dd></div>
              <div><dt>角色</dt><dd>${escapeHtml((output.characters || []).join('、') || '-')}</dd></div>
              <div><dt>地点</dt><dd>${escapeHtml((output.locations || []).join('、') || '-')}</dd></div>
              <div><dt>Warnings</dt><dd>${escapeHtml((output.warnings || []).join('；') || '无')}</dd></div>
            </dl>
            ${(output.world_events || []).length ? `<ol class="ewa-timeline">${output.world_events.map((event) => `<li><p>${escapeHtml(event)}</p></li>`).join('')}</ol>` : ''}
          </article>
        `;
      });
    });
    root.querySelectorAll('[data-snapshot-index]').forEach((button) => {
      button.addEventListener('click', () => {
        const snapshot = snapshots[Number(button.dataset.snapshotIndex)] || {};
        root.querySelector('[data-snapshot-detail]').innerHTML = `
          <article class="ewa-inspector-card">
            <div class="ewa-role-topline"><h4>第${escapeHtml(snapshot.chapter_number || '-')}章快照</h4><span>${escapeHtml(snapshot.content_hash || '-')}</span></div>
            <p class="ewa-character-intro">${escapeHtml(snapshot.summary || '暂无摘要')}</p>
            <div class="ewa-chip-row">
              ${(snapshot.characters || []).map((item) => `<em>${escapeHtml(item)}</em>`).join('')}
              ${(snapshot.locations || []).map((item) => `<em>${escapeHtml(item)}</em>`).join('')}
            </div>
            <div class="ewa-action-row">
              <button type="button" class="ewa-mini-action is-danger" data-rollback-chapter="${escapeAttr(snapshot.chapter_number)}">回滚第${escapeHtml(snapshot.chapter_number)}章</button>
            </div>
          </article>
        `;
        bindRollbackButtons(root);
      });
    });
    root.querySelector('[data-rebuild-derived]')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      if (!state.lastPayload?.novelId) return;
      button.disabled = true;
      button.textContent = '重建中...';
      try {
        const response = await fetch(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(state.lastPayload.novelId)}/rebuild`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
        if (!response.ok) throw new Error(`Rebuild failed: ${response.status}`);
      } finally {
        await refreshStatusTab();
      }
    });
    root.querySelector('[data-import-st-preset]')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      const input = root.querySelector('[data-st-preset-input]');
      const message = root.querySelector('[data-import-message]');
      if (!state.lastPayload?.novelId || !input) return;
      let payload;
      try {
        payload = JSON.parse(input.value || '{}');
      } catch (error) {
        showTransientMessage(message, 'JSON 格式错误，未导入。');
        return;
      }
      button.disabled = true;
      button.textContent = '转换中...';
      try {
        const response = await fetch(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(state.lastPayload.novelId)}/import/st-preset`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        if (!response.ok) throw new Error(`Import failed: ${response.status}`);
        showTransientMessage(message, '已转换为声明式流程。');
        input.value = '';
      } catch (error) {
        showTransientMessage(message, String(error));
      } finally {
        button.disabled = false;
        button.textContent = '转换并保存';
        await refreshStatusTab();
      }
    });
    bindRollbackButtons(root);
  }

  function showTransientMessage(element, text) {
    if (!element) return;
    element.textContent = text;
    window.setTimeout(() => {
      element.textContent = '';
    }, 3000);
  }

  async function readErrorDetail(response, fallback) {
    try {
      const data = await response.json();
      if (typeof data.detail === 'string' && data.detail.trim()) return data.detail.trim();
      if (Array.isArray(data.detail)) {
        return data.detail.map((item) => item?.msg || JSON.stringify(item)).join('; ');
      }
    } catch (error) {
      try {
        const text = await response.text();
        if (text.trim()) return text.trim().slice(0, 300);
      } catch (ignored) {
        // Keep the caller's fallback if the body has already been consumed.
      }
    }
    return fallback;
  }

  function bindRollbackButtons(root) {
    root.querySelectorAll('[data-rollback-chapter]').forEach((button) => {
      if (button.dataset.boundRollback === 'true') return;
      button.dataset.boundRollback = 'true';
      button.addEventListener('click', async () => {
        const chapterNumber = button.dataset.rollbackChapter;
        if (!chapterNumber || !state.lastPayload?.novelId) return;
        button.disabled = true;
        button.textContent = `回滚第${chapterNumber}章中...`;
        try {
          const response = await fetch(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(state.lastPayload.novelId)}/chapters/${encodeURIComponent(chapterNumber)}/rollback`, { method: 'POST' });
          if (!response.ok) throw new Error(`Rollback failed: ${response.status}`);
        } finally {
          await refreshStatusTab();
        }
      });
    });
  }

  async function refreshStatusTab() {
    await openPanel();
    state.activeTab = 'status';
    renderPanel(document.getElementById('ewa-drawer'));
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
