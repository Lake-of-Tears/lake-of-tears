/**
 * Lake of Tears - SQL Editor Controller (Alpine.js)
 * Declarative state management for:
 * 1. Sidebar catalog/schema tree collapse toggles & tab switching (catalog, snippets)
 * 2. Query execution state, export format dropdowns, error alerts
 * 3. AI query generation, AI error fixing, and Explain Query modal
 */

document.addEventListener('alpine:init', () => {

  const _origToggleSqlSidebar = window.toggleSqlSidebar;
  const _origSwitchSidebarTab = window.switchSidebarTab;
  const _origExecuteRunQuery = window.executeRunQuery;
  const _origExplainCurrentQuery = window.explainCurrentQuery;
  const _origCloseExplainModal = window.closeExplainModal;
  const _origGenerateSqlWithAi = window.generateSqlWithAi;
  const _origFixQueryWithAi = window.fixQueryWithAi;
  const _origApplyFixedSql = window.applyFixedSql;

  Alpine.data('sqlEditorManager', () => ({
    sidebarOpen: true,
    activeSidebarTab: 'schema',

    // Query Execution State
    isRunning: false,
    queryError: null,
    errorInfo: null,

    // Explain Modal State
    explainModalOpen: false,
    explainText: '',
    isExplaining: false,

    // AI Generation State
    isAiGenerating: false,
    isAiFixing: false,

    init() {
      // Expose globally for legacy bridges, external inline handlers, and test assertions
      window.toggleSqlSidebar = () => this.toggleSidebar();
      window.switchSidebarTab = (tab) => this.switchSidebarTab(tab);
      window.executeRunQuery = () => this.executeRunQuery();
      window.triggerFormatSql = () => this.triggerFormatSql();
      window.explainCurrentQuery = () => this.explainCurrentQuery();
      window.closeExplainModal = () => this.closeExplainModal();
      window.generateSqlWithAi = () => this.generateSqlWithAi();
      window.fixQueryWithAi = () => this.fixQueryWithAi();
      window.applyFixedSql = (andRun = false) => this.applyFixedSql(andRun);
      window.openQueryInLakeset = () => this.openQueryInLakeset();

      // Keyboard shortcuts
      window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && this.explainModalOpen) {
          this.closeExplainModal();
        }
      });
    },

    toggleSidebar() {
      this.sidebarOpen = !this.sidebarOpen;
      if (typeof _origToggleSqlSidebar === 'function') {
        return _origToggleSqlSidebar();
      }
      const sidebar = document.getElementById('sqlReferenceSidebar');
      if (sidebar) {
        sidebar.classList.toggle('hidden', !this.sidebarOpen);
      }
    },

    switchSidebarTab(tab) {
      this.activeSidebarTab = tab;
      if (typeof _origSwitchSidebarTab === 'function') {
        return _origSwitchSidebarTab(tab);
      }
      const catalogBtn = document.getElementById('tabBtnSchema');
      const snippetsBtn = document.getElementById('tabBtnFunctions');
      const catalogContent = document.getElementById('sidebarTabSchema');
      const snippetsContent = document.getElementById('sidebarTabFunctions');

      if (tab === 'schema' || tab === 'catalog') {
        if (catalogBtn) {
          catalogBtn.className = 'flex-1 py-2.5 text-xs font-semibold text-blue-400 border-b-2 border-blue-500 bg-gray-900/60';
        }
        if (snippetsBtn) {
          snippetsBtn.className = 'flex-1 py-2.5 text-xs font-semibold text-gray-500 hover:text-gray-300 border-b-2 border-transparent';
        }
        if (catalogContent) catalogContent.classList.remove('hidden');
        if (snippetsContent) snippetsContent.classList.add('hidden');
      } else {
        if (catalogBtn) {
          catalogBtn.className = 'flex-1 py-2.5 text-xs font-semibold text-gray-500 hover:text-gray-300 border-b-2 border-transparent';
        }
        if (snippetsBtn) {
          snippetsBtn.className = 'flex-1 py-2.5 text-xs font-semibold text-blue-400 border-b-2 border-blue-500 bg-gray-900/60';
        }
        if (catalogContent) catalogContent.classList.add('hidden');
        if (snippetsContent) snippetsContent.classList.remove('hidden');
      }
    },

    executeRunQuery() {
      if (typeof _origExecuteRunQuery === 'function') {
        return _origExecuteRunQuery();
      }
      const form = document.getElementById('sqlEditorForm');
      if (form) form.submit();
    },

    triggerFormatSql() {
      if (typeof window.formatSqlText === 'function' && typeof window.getEditorSql === 'function' && typeof window.setEditorSql === 'function') {
        const current = window.getEditorSql();
        const formatted = window.formatSqlText(current);
        window.setEditorSql(formatted, false);
      }
    },

    async explainCurrentQuery() {
      this.explainModalOpen = true;
      this.isExplaining = true;

      if (typeof _origExplainCurrentQuery === 'function') {
        try {
          return await _origExplainCurrentQuery();
        } finally {
          this.isExplaining = false;
        }
      }

      const modal = document.getElementById('explainQueryModal');
      const content = document.getElementById('explainModalBody');
      if (modal) modal.classList.remove('hidden');
      if (content) content.textContent = 'Generating EXPLAIN execution plan...';
      this.isExplaining = false;
    },

    closeExplainModal() {
      this.explainModalOpen = false;
      if (typeof _origCloseExplainModal === 'function') {
        return _origCloseExplainModal();
      }
      const modal = document.getElementById('explainQueryModal');
      if (modal) modal.classList.add('hidden');
    },

    async generateSqlWithAi() {
      if (typeof _origGenerateSqlWithAi === 'function') {
        return _origGenerateSqlWithAi();
      }
    },

    async fixQueryWithAi() {
      if (typeof _origFixQueryWithAi === 'function') {
        return _origFixQueryWithAi();
      }
    },

    applyFixedSql(andRun = false) {
      if (typeof _origApplyFixedSql === 'function') {
        return _origApplyFixedSql(andRun);
      }
    },

    openQueryInLakeset() {
      let currentSql = '';
      if (typeof window.getEditorSql === 'function') {
        currentSql = window.getEditorSql();
      } else {
        const ta = document.getElementById('sqlTextarea');
        if (ta) currentSql = ta.value;
      }
      const encoded = encodeURIComponent((currentSql || '').trim());
      window.location.href = `/lakeset?mode=explore&sql=${encoded}`;
    }
  }));

});
