(function registerEvolutionWorldAssistant() {
  const runtime = window.PlotPilotPlugins;
  if (!runtime) {
    console.warn('[EvolutionWorld] PlotPilotPlugins runtime missing');
    return;
  }

  const pluginName = 'world_evolution_core';
  const frontendBuild = 'observability-v2-20260428';
  const state = {
    activeTab: 'characters',
    viewMode: 'novel',
    selectedCharacterId: null,
    lastPayload: null,
    agentSettingsMessage: '',
    agentModelFetchMessage: '',
    agentModels: [],
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
        <button type="button" data-tab="review">审核</button>
        <button type="button" data-tab="agent">智能体</button>
        <button type="button" data-tab="diagnostics">风险审查</button>
        <button type="button" data-tab="status">运行态</button>
        <button type="button" data-tab="settings">设置</button>
      </nav>
      <main data-content class="ewa-content">加载中...</main>
      <footer class="ewa-footer">
        <span>Phase 1 · ${frontendBuild}</span>
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
      const [characters, status, runs, snapshots, importedFlows, settings, routeMap, agentStatus, diagnostics, reviewCandidates] = await Promise.all([
        runtime.fetchJson(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(novelId)}/characters`),
        runtime.fetchJson('/api/v1/plugins/evolution-world/status'),
        runtime.fetchJson(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(novelId)}/runs?limit=8`),
        runtime.fetchJson(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(novelId)}/snapshots`),
        runtime.fetchJson(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(novelId)}/imported-flows`),
        runtime.fetchJson('/api/v1/plugins/evolution-world/settings'),
        runtime.fetchJson(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(novelId)}/routes/global`),
        runtime.fetchJson(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(novelId)}/agent/status`),
        runtime.fetchJson(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(novelId)}/diagnostics`),
        runtime.fetchJson(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(novelId)}/review-candidates?status=pending&limit=50`),
      ]);
      state.lastPayload = { novelId, characters, status, runs, snapshots, importedFlows, settings, routeMap, agentStatus, diagnostics, reviewCandidates };
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
    if (state.activeTab === 'review') return renderReview(drawer, state.lastPayload);
    if (state.activeTab === 'agent') return renderAgent(drawer, state.lastPayload);
    if (state.activeTab === 'diagnostics') return renderDiagnostics(drawer, state.lastPayload);
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

  function renderAgent(drawer, payload) {
    const content = drawer.querySelector('[data-content]');
    const agent = payload.agentStatus || {};
    const counts = agent.asset_counts || {};
    const selection = agent.latest_selection || {};
    const genes = Array.isArray(selection.selected_genes) ? selection.selected_genes : [];
    const topGenes = Array.isArray(agent.top_genes) ? agent.top_genes : [];
    const capsules = Array.isArray(agent.top_capsules) ? agent.top_capsules : [];
    const memoryLayers = agent.memory_layers || {};
    const hostContext = agent.host_context_summary || {};
    const plotpilotUsage = agent.plotpilot_context_usage || hostContext.plotpilot_context_usage || {};
    const semanticRecall = agent.semantic_recall_summary || {};
    const agentApiUsage = agent.agent_api_usage || {};
    const agentApiAggregate = agentApiUsage.aggregate || {};
    const planningAlignment = agent.planning_alignment || {};
    const nativeAlignment = agent.native_context_alignment || {};
    const orchestration = agent.agent_orchestration || {};
    const knowledgeBase = agent.knowledge_base || {};
    const autoEvolution = agent.auto_evolution || {};
    const activeGeneVersions = Array.isArray(agent.active_gene_versions) ? agent.active_gene_versions : [];
    const observabilityNotes = [];
    if (hostContext.observability_normalized) observabilityNotes.push('旧版原生资料摘要已按当前 schema 兼容显示');
    if (!plotpilotUsage.mode) observabilityNotes.push('PlotPilot 原生资料策略模式暂未写入，已按 strategy_only 展示');
    const diagnostics = payload.diagnostics || {};
    const budget = diagnostics.context_budget_summary || {};
    const gate = diagnostics.injection_gate_summary || {};
    const review = diagnostics.review_candidate_summary || {};
    const freshness = diagnostics.knowledge_freshness || {};
    const injectionSummary = agent.context_injection_summary || {};
    const t0Blocks = budget.t0_block_count ?? injectionSummary.t0_block_count ?? 0;
    const t1Blocks = budget.t1_block_count ?? injectionSummary.t1_block_count ?? 0;
    const t0Chars = budget.t0_chars ?? injectionSummary.t0_chars ?? 0;
    const t1Chars = budget.t1_chars ?? injectionSummary.t1_chars ?? 0;
    const diagnosticRisks = Array.isArray(diagnostics.risks) ? diagnostics.risks : [];
    const degradedRisks = diagnosticRisks.filter((item) => item.source === 'host_context' || item.source === 'semantic_recall' || item.source === 'agent_events').slice(0, 4);
    const reflections = Array.isArray(agent.latest_reflections) ? agent.latest_reflections.slice().reverse() : [];
    const candidates = Array.isArray(agent.gene_candidates) ? agent.gene_candidates.slice().reverse() : [];
    const learning = Array.isArray(agent.latest_learning) ? agent.latest_learning.slice().reverse() : [];
    const events = Array.isArray(agent.recent_events) ? agent.recent_events.slice().reverse() : [];
    content.innerHTML = `
      <section class="ewa-summary-grid">
        <article><b>${counts.genes || 0}</b><span>Gene</span></article>
        <article><b>${counts.capsules || 0}</b><span>Capsule</span></article>
        <article><b>${counts.reflections || 0}</b><span>Reflection</span></article>
        <article><b>${counts.gene_candidates || 0}</b><span>候选</span></article>
        <article><b>${counts.events || 0}</b><span>Event</span></article>
      </section>
      <section class="ewa-section">
        <div class="ewa-section-head">
          <h3>记忆层</h3>
          <p>事件、语义、策略与反思资产</p>
        </div>
        <dl class="ewa-status-list">
          <div><dt>Episodic</dt><dd>${escapeHtml(memoryLayers.episodic || 0)}</dd></div>
          <div><dt>Semantic</dt><dd>${escapeHtml(memoryLayers.semantic || 0)}</dd></div>
          <div><dt>Procedural</dt><dd>${escapeHtml(memoryLayers.procedural || 0)}</dd></div>
          <div><dt>Reflective</dt><dd>${escapeHtml(memoryLayers.reflective || 0)}</dd></div>
        </dl>
      </section>
      <section class="ewa-section">
        <div class="ewa-section-head">
          <h3>规划锁适配</h3>
          <p>宏观规划前的 premise、题材与主线硬约束</p>
        </div>
        <dl class="ewa-status-list">
          <div><dt>Premise 命中</dt><dd>${planningAlignment.premise_received ? '是' : '否'}</dd></div>
          <div><dt>规划锁</dt><dd>${planningAlignment.planning_lock_generated ? '已生成' : '未生成'}</dd></div>
          <div><dt>Bible 空表回退</dt><dd>${planningAlignment.bible_empty_fallback ? '是' : '否'}</dd></div>
          <div><dt>前史辅助</dt><dd>${planningAlignment.prehistory_available ? '有' : '无'}</dd></div>
          <div><dt>类型</dt><dd>${escapeHtml(planningAlignment.genre || '-')}</dd></div>
          <div><dt>世界观</dt><dd>${escapeHtml(planningAlignment.world_preset || '-')}</dd></div>
          <div><dt>目标章数</dt><dd>${escapeHtml(planningAlignment.target_chapters || 0)}</dd></div>
        </dl>
      </section>
      <section class="ewa-section">
        <div class="ewa-section-head">
          <h3>外部信息源</h3>
          <p>PlotPilot 内置模块只读召回状态</p>
        </div>
        <dl class="ewa-status-list">
          <div><dt>Bible</dt><dd>${escapeHtml(hostContext.counts?.bible || 0)}</dd></div>
          <div><dt>世界观</dt><dd>${escapeHtml(hostContext.counts?.world || 0)}</dd></div>
          <div><dt>知识库</dt><dd>${escapeHtml(hostContext.counts?.knowledge || 0)}</dd></div>
          <div><dt>章后同步</dt><dd>${escapeHtml(hostContext.counts?.story_knowledge || 0)}</dd></div>
          <div><dt>故事线</dt><dd>${escapeHtml(hostContext.counts?.storyline || 0)}</dd></div>
          <div><dt>时间线</dt><dd>${escapeHtml(hostContext.counts?.timeline || hostContext.counts?.chronicle || 0)}</dd></div>
          <div><dt>伏笔</dt><dd>${escapeHtml(hostContext.counts?.foreshadow || 0)}</dd></div>
          <div><dt>对白</dt><dd>${escapeHtml(hostContext.counts?.dialogue || 0)}</dd></div>
          <div><dt>Triples</dt><dd>${escapeHtml(hostContext.counts?.triples || 0)}</dd></div>
          <div><dt>MemoryEngine</dt><dd>${escapeHtml(hostContext.counts?.memory_engine || 0)}</dd></div>
        </dl>
        <div class="ewa-chip-row">
          <em>${escapeHtml(plotpilotUsage.mode || 'strategy_only')}</em>
          ${Object.entries(plotpilotUsage.hit_counts_by_tier || {}).map(([key, value]) => `<em>${escapeHtml(key)}:${escapeHtml(value)}</em>`).join('')}
          ${(hostContext.active_sources || []).map((item) => `<em>${escapeHtml(item)}</em>`).join('') || '<em>暂无外部命中</em>'}
          ${(hostContext.degraded_sources || []).map((item) => `<em>降级:${escapeHtml(item)}</em>`).join('')}
          ${(hostContext.empty_sources || []).map((item) => `<em>空:${escapeHtml(item)}</em>`).join('')}
          <em>短策略:${nativeAlignment.strategy_only === false ? '否' : '是'}</em>
          <em>T0:${escapeHtml(t0Blocks)}块/${escapeHtml(t0Chars)}字</em>
          <em>T1:${escapeHtml(t1Blocks)}块/${escapeHtml(t1Chars)}字</em>
          <em>重复源:${escapeHtml(nativeAlignment.duplicated_source_count || 0)}</em>
          <em>向量:${semanticRecall.vector_enabled ? '启用' : '未启用'}</em>
          <em>召回:${escapeHtml(semanticRecall.item_count || 0)}</em>
        </div>
        ${observabilityNotes.length ? `<p class="ewa-muted">${observabilityNotes.map(escapeHtml).join('；')}。若看不到 Agent API 成本或实验护栏，请刷新工作台以重新加载 ${escapeHtml(frontendBuild)}。</p>` : ''}
      </section>
      <section class="ewa-section">
        <div class="ewa-section-head">
          <h3>降级与失败摘要</h3>
          <p>来自风险审查的最近可观测信号</p>
        </div>
        <ol class="ewa-agent-list">
          ${degradedRisks.map((risk) => `
            <li>
              <strong>${escapeHtml(risk.severity || 'info')} · ${escapeHtml(risk.affected_feature || risk.source || '-')}</strong>
              <p>${escapeHtml(risk.message || '')}</p>
            </li>
          `).join('') || '<li><p>暂无降级或失败风险。</p></li>'}
        </ol>
      </section>
      <section class="ewa-section">
        <div class="ewa-section-head">
          <h3>Agent API 成本</h3>
          <p>Evolution 额外模型调用，与正文生成分开统计</p>
        </div>
        <dl class="ewa-status-list">
          <div><dt>调用</dt><dd>${escapeHtml(agentApiAggregate.call_count || 0)}</dd></div>
          <div><dt>输入</dt><dd>${escapeHtml(agentApiAggregate.input_tokens || 0)}</dd></div>
          <div><dt>输出</dt><dd>${escapeHtml(agentApiAggregate.output_tokens || 0)}</dd></div>
          <div><dt>总 token</dt><dd>${escapeHtml(agentApiAggregate.total_tokens || 0)}</dd></div>
        </dl>
      </section>
      <section class="ewa-section">
        <div class="ewa-section-head">
          <h3>Agent 接管</h3>
          <p>Orchestrator 决策、全文知识库与自动 Gene 更新</p>
        </div>
        <dl class="ewa-status-list">
          <div><dt>决策记录</dt><dd>${escapeHtml(orchestration.decision_count || 0)}</dd></div>
          <div><dt>降级决策</dt><dd>${escapeHtml(orchestration.degraded_decision_count || 0)}</dd></div>
          <div><dt>知识文档</dt><dd>${escapeHtml(knowledgeBase.document_count || 0)}</dd></div>
          <div><dt>知识切块</dt><dd>${escapeHtml(knowledgeBase.chunk_count || 0)}</dd></div>
          <div><dt>自进化</dt><dd>${escapeHtml(autoEvolution.mode || 'immediate')} · ${escapeHtml(autoEvolution.gene_version_count || 0)} 版</dd></div>
          <div><dt>Agent Gene</dt><dd>${escapeHtml(activeGeneVersions.filter((item) => item.created_by_agent).length)}</dd></div>
        </dl>
        <div class="ewa-chip-row">
          ${Object.entries(knowledgeBase.chunk_counts_by_source || {}).slice(0, 10).map(([key, value]) => `<em>${escapeHtml(key)}:${escapeHtml(value)}</em>`).join('') || '<em>暂无全文知识索引</em>'}
        </div>
      </section>
      <section class="ewa-section">
        <div class="ewa-section-head">
          <h3>最近选择</h3>
          <p>根据章节信号选择注入策略</p>
        </div>
        <div class="ewa-chip-row">
          ${(selection.signals || []).map((item) => `<em>${escapeHtml(item)}</em>`).join('') || '<em>暂无信号</em>'}
        </div>
        <ol class="ewa-agent-list">
          ${genes.map((gene) => `
            <li>
              <strong>${escapeHtml(gene.title || gene.id || 'Gene')}</strong>
              <p>${escapeHtml((gene.strategy || []).slice(0, 2).join('；') || '')}</p>
            </li>
          `).join('') || '<li><p>暂无策略选择。生成上下文后会记录。</p></li>'}
        </ol>
      </section>
      <section class="ewa-section">
        <div class="ewa-section-head">
          <h3>策略贡献</h3>
          <p>正向记录 Gene 对章节稳定性的保护</p>
        </div>
        <ol class="ewa-agent-list">
          ${topGenes.map((gene) => `
            <li>
              <strong>${escapeHtml(gene.title || gene.id || 'Gene')}</strong>
              <span>命中 ${escapeHtml(gene.hit_count || 0)} · 保护 ${escapeHtml(gene.protected_count || 0)} · 有效 ${escapeHtml(gene.helpful_count || 0)} · 待改进 ${escapeHtml(gene.failure_count || 0)}</span>
              <p>${escapeHtml(gene.last_positive_reason || gene.last_improvement_advice || (gene.signals_match || []).join('、') || '')}</p>
            </li>
          `).join('') || '<li><p>暂无策略贡献。章节审查后会更新。</p></li>'}
        </ol>
      </section>
      <section class="ewa-section ewa-run-section">
        <div class="ewa-section-head">
          <h3>经验胶囊</h3>
          <p>从审查问题中保守固化</p>
        </div>
        <ol class="ewa-agent-list">
          ${capsules.map((capsule) => `
            <li>
              <strong>${escapeHtml(capsule.title || capsule.id || 'Capsule')}</strong>
              <span>第${escapeHtml(capsule.last_seen_chapter || capsule.chapter_number || '-')}章 · ${escapeHtml(capsule.category || '-')} · 成功 ${escapeHtml(capsule.success_count || 0)} · 失败 ${escapeHtml(capsule.failure_count || 0)}</span>
              <p>${escapeHtml(capsule.guidance || capsule.summary || '')}</p>
            </li>
          `).join('') || '<li><p>暂无固化经验。审查发现高置信问题后会出现。</p></li>'}
        </ol>
      </section>
      <section class="ewa-section ewa-run-section">
        <div class="ewa-section-head">
          <h3>反思记录</h3>
          <p>从审查与 Capsule 中沉淀的下一章约束</p>
        </div>
        <ol class="ewa-agent-list">
          ${reflections.map((reflection) => `
            <li>
              <strong>${escapeHtml(reflection.problem_pattern || reflection.id || 'Reflection')}</strong>
              <span>第${escapeHtml(reflection.chapter_number || '-')}章 · ${escapeHtml(reflection.source || '-')}</span>
              <p>${escapeHtml((reflection.next_chapter_constraints || []).join('；') || reflection.root_cause || reflection.content || '')}</p>
            </li>
          `).join('') || '<li><p>暂无反思记录。固化 Capsule 后会出现。</p></li>'}
        </ol>
      </section>
      <section class="ewa-section ewa-run-section">
        <div class="ewa-section-head">
          <h3>候选 Gene</h3>
          <p>只读待审，不会自动替换正式策略</p>
        </div>
        <ol class="ewa-agent-list">
          ${candidates.map((candidate) => `
            <li>
              <strong>${escapeHtml(candidate.title || candidate.id || 'GeneCandidate')}</strong>
              <span>${escapeHtml(candidate.status || 'pending_review')} · 第${escapeHtml(candidate.last_seen_chapter || candidate.created_chapter || '-')}章</span>
              <p>${escapeHtml((candidate.strategy_draft || []).join('；') || candidate.trigger_reason || '')}</p>
            </li>
          `).join('') || '<li><p>暂无候选 Gene。重复问题或连续待改进后会生成。</p></li>'}
        </ol>
      </section>
      <section class="ewa-section ewa-run-section">
        <div class="ewa-section-head">
          <h3>最近学习</h3>
          <p>固化、反思与策略评估</p>
        </div>
        <ol class="ewa-timeline">
          ${learning.map((event) => `
            <li>
              <span>${escapeHtml(event.intent || '-')} · 第${escapeHtml(event.chapter_number || '-')}章</span>
              <strong>${escapeHtml(event.hook_name || event.type || 'EvolutionEvent')}</strong>
              <p>${escapeHtml((event.outcome?.protected || event.outcome?.helpful || event.outcome?.needs_improvement || event.outcome?.failures || event.outcome?.successes || event.signals || []).join('、') || event.outcome?.status || '')}</p>
            </li>
          `).join('') || '<li><p>暂无学习记录</p></li>'}
        </ol>
      </section>
      <section class="ewa-section ewa-run-section">
        <div class="ewa-section-head">
          <h3>智能体事件</h3>
          <p>观察、注入、固化闭环</p>
        </div>
        <ol class="ewa-timeline">
          ${events.map((event) => `
            <li>
              <span>${escapeHtml(event.intent || '-')} · 第${escapeHtml(event.chapter_number || '-')}章</span>
              <strong>${escapeHtml(event.hook_name || event.type || 'EvolutionEvent')}</strong>
              <p>${escapeHtml((event.signals || []).join('、') || event.outcome?.status || '')}</p>
            </li>
          `).join('') || '<li><p>暂无智能体事件</p></li>'}
        </ol>
      </section>
    `;
  }

  function renderDiagnostics(drawer, payload) {
    const content = drawer.querySelector('[data-content]');
    const diagnostics = payload.diagnostics || {};
    const summary = diagnostics.summary || {};
    const runtime = diagnostics.runtime || {};
    const risks = Array.isArray(diagnostics.risks) ? diagnostics.risks : [];
    const hostContext = diagnostics.host_context_summary || {};
    const alignment = diagnostics.host_feature_alignment || {};
    const semanticRecall = diagnostics.semantic_recall_summary || {};
    const dependencies = diagnostics.dependency_status || {};
    const counts = diagnostics.agent_asset_counts || {};
    const leakage = diagnostics.plugin_leakage_check || {};
    const budget = diagnostics.context_budget_summary || {};
    const planningAlignment = diagnostics.planning_alignment || {};
    const nativeAlignment = diagnostics.native_context_alignment || {};
    const takeover = diagnostics.agent_takeover_health || {};
    const coverage = diagnostics.knowledge_coverage || {};
    const mutationAudit = diagnostics.gene_mutation_audit || {};
    const degradedAgentTools = Array.isArray(diagnostics.degraded_agent_tools) ? diagnostics.degraded_agent_tools : [];
    const observabilityNotes = [];
    if (hostContext.observability_normalized) observabilityNotes.push('旧版 host context 摘要已兼容补齐');
    if (budget.legacy_record_normalized) observabilityNotes.push('历史 context injection 记录已兼容统计');
    content.innerHTML = `
      <section class="ewa-summary-grid">
        <article><b>${escapeHtml(summary.critical || 0)}</b><span>Critical</span></article>
        <article><b>${escapeHtml(summary.warning || 0)}</b><span>Warning</span></article>
        <article><b>${escapeHtml(summary.info || 0)}</b><span>Info</span></article>
        <article><b>${escapeHtml(summary.total || 0)}</b><span>总风险</span></article>
      </section>
      <section class="ewa-section">
        <div class="ewa-section-head">
          <h3>运行边界</h3>
          <p>插件平台、Hook 与只读宿主能力</p>
        </div>
        <dl class="ewa-status-list">
          <div><dt>插件启用</dt><dd>${runtime.enabled ? '是' : '否'}</dd></div>
          <div><dt>已注册 Hook</dt><dd>${escapeHtml(Object.keys(runtime.registered_hooks || {}).length)}</dd></div>
          <div><dt>缺失 Hook</dt><dd>${escapeHtml((runtime.missing_hooks || []).join('、') || '无')}</dd></div>
          <div><dt>重复 Hook</dt><dd>${escapeHtml((runtime.duplicate_hooks || []).join('、') || '无')}</dd></div>
        </dl>
      </section>
      <section class="ewa-section">
        <div class="ewa-section-head">
          <h3>数据源状态</h3>
          <p>外部信息源、向量召回与智能体资产</p>
        </div>
        <dl class="ewa-status-list">
          <div><dt>外部命中</dt><dd>${escapeHtml((hostContext.active_sources || []).join('、') || '无')}</dd></div>
          <div><dt>外部降级</dt><dd>${escapeHtml((hostContext.degraded_sources || []).join('、') || '无')}</dd></div>
          <div><dt>字段缺失</dt><dd>${escapeHtml((hostContext.field_missing_sources || []).join('、') || '无')}</dd></div>
          <div><dt>向量</dt><dd>${semanticRecall.vector_enabled ? '启用' : '未启用'} · ${escapeHtml(semanticRecall.item_count || 0)} 条</dd></div>
          <div><dt>向量依赖</dt><dd>${escapeHtml(formatDependencyStatus(dependencies))}</dd></div>
          <div><dt>Agent资产</dt><dd>Gene ${escapeHtml(counts.genes || 0)} · Capsule ${escapeHtml(counts.capsules || 0)} · Event ${escapeHtml(counts.events || 0)}</dd></div>
        </dl>
        ${observabilityNotes.length ? `<p class="ewa-muted">${observabilityNotes.map(escapeHtml).join('；')}。如页面字段与接口不一致，请刷新工作台以重新加载 ${escapeHtml(frontendBuild)}。</p>` : ''}
      </section>
      <section class="ewa-section">
        <div class="ewa-section-head">
          <h3>规划锁适配</h3>
          <p>宏观规划前是否收到 premise 与题材硬约束</p>
        </div>
        <dl class="ewa-status-list">
          <div><dt>Premise</dt><dd>${planningAlignment.premise_received ? '已收到' : '未收到'}</dd></div>
          <div><dt>规划锁</dt><dd>${planningAlignment.planning_lock_generated ? '已生成' : '未生成'}</dd></div>
          <div><dt>Bible 空表回退</dt><dd>${planningAlignment.bible_empty_fallback ? '是' : '否'}</dd></div>
          <div><dt>前史辅助</dt><dd>${planningAlignment.prehistory_available ? '有' : '无'}</dd></div>
          <div><dt>渲染长度</dt><dd>${escapeHtml(planningAlignment.rendered_chars || 0)}</dd></div>
          <div><dt>短策略</dt><dd>${nativeAlignment.strategy_only === false ? '否' : '是'}</dd></div>
          <div><dt>重复源</dt><dd>${escapeHtml(nativeAlignment.duplicated_source_count || 0)}</dd></div>
        </dl>
      </section>
      <section class="ewa-section">
        <div class="ewa-section-head">
          <h3>原生资料适配</h3>
          <p>Bible、章后同步、故事线、伏笔、时间线、对白、Triples 与 MemoryEngine</p>
        </div>
        <dl class="ewa-status-list">
          ${Object.entries(alignment.native_sources || {}).map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>`).join('') || '<div><dt>状态</dt><dd>暂无摘要</dd></div>'}
          <div><dt>模式</dt><dd>${escapeHtml(alignment.mode || 'strategy_only')}</dd></div>
          <div><dt>空源</dt><dd>${escapeHtml((alignment.empty_sources || []).join('、') || '无')}</dd></div>
          <div><dt>字段缺失</dt><dd>${escapeHtml((alignment.field_missing_sources || []).join('、') || '无')}</dd></div>
          <div><dt>降级</dt><dd>${escapeHtml((alignment.degraded_sources || []).join('、') || '无')}</dd></div>
        </dl>
      </section>
      <section class="ewa-section">
        <div class="ewa-section-head">
          <h3>实验护栏</h3>
          <p>泄露检查与上下文预算摘要</p>
        </div>
        <dl class="ewa-status-list">
          <div><dt>Evolution 活动</dt><dd>${leakage.has_evolution_activity ? '有' : '无'}</dd></div>
          <div><dt>注入记录</dt><dd>${escapeHtml(leakage.context_injection_records || 0)}</dd></div>
          <div><dt>学习资产</dt><dd>${escapeHtml(leakage.agent_learning_assets || 0)}</dd></div>
          <div><dt>上下文块</dt><dd>${escapeHtml(budget.block_count || 0)} · budget ${escapeHtml(budget.token_budget || 0)}</dd></div>
          <div><dt>T0 硬约束</dt><dd>${escapeHtml(budget.t0_block_count || 0)} 块 · ${escapeHtml(budget.t0_chars || 0)} 字</dd></div>
          <div><dt>T1 软策略</dt><dd>${escapeHtml(budget.t1_block_count || 0)} 块 · ${escapeHtml(budget.t1_chars || 0)} 字</dd></div>
          <div><dt>门控</dt><dd>${gate.has_decision ? (gate.should_inject ? '注入' : '跳过') : '暂无'} · pending ${escapeHtml(gate.pending_review_count || review.pending || 0)}</dd></div>
          <div><dt>门控原因</dt><dd>${escapeHtml([...(gate.reasons || []), ...(gate.skipped_reasons || [])].join('、') || '无')}</dd></div>
          <div><dt>知识新鲜度</dt><dd>${freshness.is_stale ? '落后' : '同步'} · facts ${escapeHtml(freshness.latest_fact_chapter || 0)} / knowledge ${escapeHtml(freshness.latest_knowledge_chapter || 0)}</dd></div>
          <div><dt>未分层</dt><dd>${escapeHtml(budget.tier_unknown_count || 0)} 块</dd></div>
          <div><dt>重复块</dt><dd>${escapeHtml((budget.duplicate_block_ids || []).join('、') || '无')}</dd></div>
          <div><dt>短策略模式</dt><dd>${budget.strategy_only ? '是' : '否'}</dd></div>
        </dl>
      </section>
      <section class="ewa-section">
        <div class="ewa-section-head">
          <h3>Agent 接管护栏</h3>
          <p>全文知识、自进化与工具降级</p>
        </div>
        <dl class="ewa-status-list">
          <div><dt>健康</dt><dd>${takeover.healthy ? '是' : '待观察'}</dd></div>
          <div><dt>决策</dt><dd>${escapeHtml(takeover.decision_count || 0)} · 降级 ${escapeHtml(takeover.degraded_decision_count || 0)}</dd></div>
          <div><dt>知识覆盖</dt><dd>${escapeHtml(coverage.document_count || 0)} docs · ${escapeHtml(coverage.chunk_count || 0)} chunks</dd></div>
          <div><dt>Gene 版本</dt><dd>${escapeHtml(mutationAudit.gene_version_count || 0)}</dd></div>
          <div><dt>降级工具</dt><dd>${escapeHtml(degradedAgentTools.map((item) => item.tool).join('、') || '无')}</dd></div>
        </dl>
      </section>
      <section class="ewa-section ewa-run-section">
        <div class="ewa-section-head">
          <h3>风险列表</h3>
          <p>只读诊断，不会自动修改数据</p>
        </div>
        <ol class="ewa-agent-list">
          ${risks.map((risk) => `
            <li class="ewa-risk-${escapeAttr(risk.severity || 'info')}">
              <strong>${escapeHtml(risk.severity || 'info')} · ${escapeHtml(risk.affected_feature || risk.source || '-')}</strong>
              <span>${escapeHtml(risk.source || '-')}</span>
              <p>${escapeHtml(risk.message || '')}</p>
              <p>${escapeHtml(risk.suggestion || '')}</p>
            </li>
          `).join('') || '<li><p>暂无风险。</p></li>'}
        </ol>
      </section>
    `;
  }

  function renderReview(drawer, payload) {
    const content = drawer.querySelector('[data-content]');
    const candidates = Array.isArray(payload.reviewCandidates?.items) ? payload.reviewCandidates.items : [];
    if (!candidates.length) {
      setEmpty(drawer, '暂无待审核状态', '低置信或高风险的 Evolution 状态投影会在这里等待批准。');
      return;
    }
    content.innerHTML = `
      <section class="ewa-summary-grid">
        <article><b>${escapeHtml(candidates.length)}</b><span>待审核</span></article>
        <article><b>${escapeHtml(candidates.filter((item) => item.risk_level === 'high').length)}</b><span>高风险</span></article>
        <article><b>${escapeHtml(payload.reviewCandidates?.pending_count || candidates.length)}</b><span>Pending</span></article>
      </section>
      <section class="ewa-section ewa-run-section">
        <div class="ewa-section-head">
          <h3>状态审核收件箱</h3>
          <p>批准后才写入长期角色卡、约束或 Agent 资产</p>
        </div>
        <div class="ewa-run-list">
          ${candidates.map(renderReviewCandidate).join('')}
        </div>
      </section>
    `;
    bindReviewButtons(content);
  }

  function renderReviewCandidate(candidate) {
    const summary = summarizeCandidatePayload(candidate.payload || {});
    const evidence = Array.isArray(candidate.evidence) ? candidate.evidence : [];
    return `
      <article class="ewa-run-card">
        <div class="ewa-run-head">
          <strong>${escapeHtml(candidate.candidate_type || 'candidate')}</strong>
          <span>${escapeHtml(candidate.risk_level || 'unknown')} · 第${escapeHtml(candidate.chapter_number || '-')}章</span>
        </div>
        <p>${escapeHtml(summary || candidate.reason || '无摘要')}</p>
        <dl class="ewa-mini-list">
          <div><dt>原因</dt><dd>${escapeHtml(candidate.reason || '-')}</dd></div>
          <div><dt>证据</dt><dd>${escapeHtml(evidence.map((item) => item.content_hash || item.source_type || item.chapter_number).filter(Boolean).join('、') || '-')}</dd></div>
        </dl>
        <div class="ewa-inline-actions">
          <button type="button" class="ewa-mini-action" data-review-approve="${escapeAttr(candidate.id)}">批准</button>
          <button type="button" class="ewa-mini-action is-danger" data-review-reject="${escapeAttr(candidate.id)}">拒绝</button>
        </div>
      </article>
    `;
  }

  function summarizeCandidatePayload(payload) {
    if (!payload || typeof payload !== 'object') return '';
    return payload.summary || payload.name || payload.rule || payload.title || JSON.stringify(payload).slice(0, 160);
  }

  function bindReviewButtons(root) {
    root.querySelectorAll('[data-review-approve], [data-review-reject]').forEach((button) => {
      if (button.dataset.boundReview === 'true') return;
      button.dataset.boundReview = 'true';
      button.addEventListener('click', async () => {
        const candidateId = button.dataset.reviewApprove || button.dataset.reviewReject;
        const action = button.dataset.reviewApprove ? 'approve' : 'reject';
        if (!candidateId || !state.lastPayload?.novelId) return;
        button.disabled = true;
        button.textContent = action === 'approve' ? '批准中...' : '拒绝中...';
        try {
          const response = await fetch(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(state.lastPayload.novelId)}/review-candidates/${encodeURIComponent(candidateId)}/${action}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
          if (!response.ok) throw new Error(await response.text());
          await openPanel();
          state.activeTab = 'review';
          renderPanel(document.getElementById('ewa-drawer'));
        } catch (error) {
          const drawer = document.getElementById('ewa-drawer');
          setEmpty(drawer, '审核操作失败', String(error));
        }
      });
    });
  }

  function formatDependencyStatus(status) {
    const entries = Object.entries(status || {});
    if (!entries.length) return '-';
    return entries.map(([key, value]) => `${key}:${value ? 'ok' : '缺失'}`).join(' · ');
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
    const agentApi = settings.agent_api || {};
    const agentCustom = agentApi.custom_profile || {};
    const agentProviderMode = agentApi.provider_mode || 'same_as_main';
    content.innerHTML = `
      <section class="ewa-section ewa-settings-section ewa-run-section">
        <div class="ewa-section-head">
          <h3>智能体 API</h3>
          <p>统一接管上下文控制卡、审查反思和策略固化，可独立于正文 API</p>
        </div>
        <form class="ewa-settings-form" data-agent-settings-form>
          <label class="ewa-switch-row">
            <input type="checkbox" name="enabled" ${agentApi.enabled ? 'checked' : ''}>
            <span>启用 Evolution 智能体专用 API</span>
          </label>
          <fieldset class="ewa-fieldset">
            <legend>调用方式</legend>
            <label><input type="radio" name="provider_mode" value="same_as_main" ${agentProviderMode !== 'custom' ? 'checked' : ''}> 与主 API 使用同一配置</label>
            <label><input type="radio" name="provider_mode" value="custom" ${agentProviderMode === 'custom' ? 'checked' : ''}> 使用智能体自定义 API</label>
          </fieldset>
          <div class="ewa-form-grid" data-agent-custom-fields>
            <label>协议
              <select name="protocol">
                ${['openai', 'anthropic', 'gemini'].map((item) => `<option value="${item}" ${agentCustom.protocol === item ? 'selected' : ''}>${item}</option>`).join('')}
              </select>
            </label>
            <label>Base URL
              <input name="base_url" value="${escapeAttr(agentCustom.base_url || '')}" placeholder="https://api.example.com/v1">
            </label>
            <div class="ewa-model-field">
              <span>模型名</span>
              <div class="ewa-model-picker">
                <input name="model" data-agent-model-input list="ewa-agent-model-options" value="${escapeAttr(agentCustom.model || '')}" placeholder="用于智能体反思的模型">
                <button type="button" class="ewa-mini-action" data-fetch-agent-models>获取模型</button>
              </div>
              <datalist id="ewa-agent-model-options">
                ${state.agentModels.map((item) => `<option value="${escapeAttr(item.id || item.name || item)}"></option>`).join('')}
              </datalist>
              <select data-agent-model-select>
                <option value="">选择已获取模型</option>
                ${state.agentModels.map((item) => {
                  const modelId = item.id || item.name || item;
                  return `<option value="${escapeAttr(modelId)}" ${modelId === agentCustom.model ? 'selected' : ''}>${escapeHtml(modelId)}</option>`;
                }).join('')}
              </select>
            </div>
            <label>API Key
              <input name="api_key" type="password" value="" placeholder="${agentCustom.api_key_configured ? '已保存，留空则继续使用' : '输入智能体 API Key'}">
            </label>
            <label>温度
              <input name="temperature" type="number" min="0" max="2" step="0.1" value="${escapeAttr(agentCustom.temperature ?? agentApi.temperature ?? 0.1)}">
            </label>
            <label>最大输出 Token
              <input name="max_tokens" type="number" min="128" max="2048" step="1" value="${escapeAttr(agentCustom.max_tokens ?? agentApi.max_tokens ?? 800)}">
            </label>
            <label>超时秒数
              <input name="timeout_seconds" type="number" min="10" max="900" step="10" value="${escapeAttr(agentCustom.timeout_seconds ?? 180)}">
            </label>
          </div>
          <div class="ewa-action-row">
            <button type="submit" class="ewa-mini-action">保存智能体 API</button>
            <button type="button" class="ewa-mini-action" data-test-agent-connection>测试连接</button>
            <span class="ewa-import-message" data-agent-settings-message>${escapeHtml(state.agentSettingsMessage || '')}</span>
            <span class="ewa-import-message" data-agent-model-fetch-message>${escapeHtml(state.agentModelFetchMessage || '')}</span>
          </div>
        </form>
      </section>
    `;
    bindAgentSettingsInteractions(content);
  }

  function bindAgentSettingsInteractions(root) {
    const form = root.querySelector('[data-agent-settings-form]');
    if (!form) return;
    const syncCustomVisibility = () => {
      const mode = form.querySelector('input[name="provider_mode"]:checked')?.value || 'same_as_main';
      form.querySelector('[data-agent-custom-fields]')?.classList.toggle('is-muted', mode !== 'custom');
    };
    form.querySelectorAll('input[name="provider_mode"]').forEach((item) => item.addEventListener('change', syncCustomVisibility));
    syncCustomVisibility();
    form.querySelector('[data-agent-model-select]')?.addEventListener('change', (event) => {
      const value = event.currentTarget.value || '';
      if (value && form.elements.model) form.elements.model.value = value;
    });
    form.querySelector('[data-fetch-agent-models]')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      const message = form.querySelector('[data-agent-model-fetch-message]');
      button.disabled = true;
      button.textContent = '获取中...';
      state.agentModelFetchMessage = '';
      if (message) message.textContent = '';
      try {
        const response = await fetch('/api/v1/plugins/evolution-world/settings/agent/models', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(buildAgentSettingsPayload(form)),
        });
        if (!response.ok) throw new Error(await readErrorDetail(response, `模型拉取失败：${response.status}`));
        const result = await response.json();
        state.agentModels = Array.isArray(result.items) ? result.items : [];
        state.agentModelFetchMessage = state.agentModels.length ? `已获取 ${state.agentModels.length} 个模型。` : '未获取到模型。';
        updateModelChoices(form, state.agentModels, form.elements.model?.value || '', '#ewa-agent-model-options', '[data-agent-model-select]');
        if (message) message.textContent = state.agentModelFetchMessage;
      } catch (error) {
        state.agentModelFetchMessage = String(error);
        if (message) message.textContent = state.agentModelFetchMessage;
      } finally {
        button.disabled = false;
        button.textContent = '获取模型';
      }
    });
    form.querySelector('[data-test-agent-connection]')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      const message = form.querySelector('[data-agent-settings-message]');
      button.disabled = true;
      button.textContent = '测试中...';
      state.agentSettingsMessage = '';
      if (message) message.textContent = '';
      try {
        const response = await fetch('/api/v1/plugins/evolution-world/settings/agent/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(buildAgentSettingsPayload(form)),
        });
        if (!response.ok) throw new Error(await readErrorDetail(response, `连接测试失败：${response.status}`));
        const result = await response.json();
        if (!result.ok) throw new Error(result.error || '连接测试失败');
        state.agentSettingsMessage = `连接成功：${result.model || '当前模型'} · ${result.latency_ms}ms`;
      } catch (error) {
        state.agentSettingsMessage = String(error);
      } finally {
        button.disabled = false;
        button.textContent = '测试连接';
        if (message) message.textContent = state.agentSettingsMessage;
      }
    });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      const message = form.querySelector('[data-agent-settings-message]');
      const payload = buildAgentSettingsPayload(form);
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
        state.agentSettingsMessage = '已保存。下一次上下文控制卡与审查固化生效。';
      } catch (error) {
        state.agentSettingsMessage = String(error);
      } finally {
        button.disabled = false;
        button.textContent = '保存智能体 API';
        if (message) message.textContent = state.agentSettingsMessage;
      }
    });
  }

  function buildAgentSettingsPayload(form) {
    const mode = form.querySelector('input[name="provider_mode"]:checked')?.value || 'same_as_main';
    return {
      agent_api: {
        enabled: Boolean(form.elements.enabled?.checked),
        provider_mode: mode,
        temperature: Number(form.elements.temperature?.value || 0.1),
        max_tokens: Number(form.elements.max_tokens?.value || 800),
        custom_profile: {
          protocol: form.elements.protocol?.value || 'openai',
          base_url: form.elements.base_url?.value || '',
          api_key: form.elements.api_key?.value || '',
          model: form.elements.model?.value || '',
          temperature: Number(form.elements.temperature?.value || 0.1),
          max_tokens: Number(form.elements.max_tokens?.value || 800),
          timeout_seconds: Number(form.elements.timeout_seconds?.value || 180),
        },
      },
      timeout_ms: 30000,
    };
  }

  function updateModelChoices(form, models, selectedModel, datalistSelector, selectSelector) {
    const scopedDatalist = form.querySelector(datalistSelector);
    const scopedSelect = form.querySelector(selectSelector);
    const options = models
      .map((item) => item.id || item.name || item)
      .filter(Boolean);
    if (scopedDatalist) {
      scopedDatalist.innerHTML = options.map((modelId) => `<option value="${escapeAttr(modelId)}"></option>`).join('');
    }
    if (scopedSelect) {
      scopedSelect.innerHTML = `
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
          if (!response.ok) throw new Error(await response.text());
          await refreshStatusTab();
        } catch (error) {
          setEmpty(document.getElementById('ewa-drawer'), '回滚失败', String(error));
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
    return String(value == null ? '' : value).replace(/[&<>"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
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
