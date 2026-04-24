(function registerEvolutionWorldAssistant() {
  const runtime = window.PlotPilotPlugins;
  if (!runtime) {
    console.warn('[EvolutionWorld] PlotPilotPlugins runtime missing');
    return;
  }

  const pluginName = 'evolution_world_assistant';

  function ensurePanel() {
    if (document.getElementById('ewa-panel-button')) return;
    const button = document.createElement('button');
    button.id = 'ewa-panel-button';
    button.type = 'button';
    button.textContent = 'EW';
    button.title = 'Evolution World Assistant';
    button.addEventListener('click', openPanel);
    document.body.appendChild(button);
  }

  function ensureDrawer() {
    let drawer = document.getElementById('ewa-drawer');
    if (drawer) return drawer;
    drawer = document.createElement('aside');
    drawer.id = 'ewa-drawer';
    drawer.innerHTML = '<header><strong>Evolution World</strong><button type="button" data-close>×</button></header><main data-content>加载中...</main>';
    drawer.querySelector('[data-close]').addEventListener('click', () => drawer.classList.remove('open'));
    document.body.appendChild(drawer);
    return drawer;
  }

  async function openPanel() {
    const drawer = ensureDrawer();
    const content = drawer.querySelector('[data-content]');
    drawer.classList.add('open');
    content.textContent = '加载中...';
    const novelId = runtime.context.getNovelId();
    if (!novelId) {
      content.textContent = '未检测到当前小说 ID。';
      return;
    }
    try {
      const payload = await runtime.fetchJson(`/api/v1/plugins/evolution-world/novels/${encodeURIComponent(novelId)}/characters`);
      const items = Array.isArray(payload.items) ? payload.items : [];
      if (!items.length) {
        content.innerHTML = '<p>暂无动态角色状态。提交或重跑章节后会生成。</p>';
        return;
      }
      content.innerHTML = items.map((item) => `
        <section class="ewa-card">
          <h4>${escapeHtml(item.name || item.character_id)}</h4>
          <p>首次：第${item.first_seen_chapter || '-'}章 / 最近：第${item.last_seen_chapter || '-'}章</p>
          <small>${escapeHtml(((item.recent_events || []).at(-1) || {}).summary || '')}</small>
        </section>
      `).join('');
    } catch (error) {
      console.warn('[EvolutionWorld] panel request failed:', error);
      content.textContent = `加载失败：${error}`;
    }
  }

  function escapeHtml(value) {
    return String(value || '').replace(/[&<>"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
  }

  runtime.plugins.register({
    name: pluginName,
    display_name: 'Evolution World Assistant',
    version: '0.1.0',
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
