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
    button.addEventListener('click', async () => {
      try {
        const status = await runtime.fetchJson('/api/v1/plugins/evolution-world/status');
        runtime.events.emit('evolution-world:status', status);
        alert(`Evolution World Assistant\n状态：${status.status}\n阶段：${status.phase}`);
      } catch (error) {
        console.warn('[EvolutionWorld] status request failed:', error);
      }
    });
    document.body.appendChild(button);
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
    },
  });
})();
