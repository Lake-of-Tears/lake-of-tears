/**
 * Lake of Tears - SQL Editor Controller (Alpine.js)
 * Declarative state management for:
 * 1. Sidebar catalog/schema tree collapse toggles & tab switching (catalog, snippets)
 * 2. Query execution state, export format dropdowns, error alerts
 * 3. AI query generation, AI error fixing, and Explain Query modal
 */

document.addEventListener('alpine:init', () => {

  Alpine.data('sqlEditorManager', () => ({
    sidebarOpen: true,
    activeSidebarTab: 'catalog',

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

      // Keyboard shortcuts
      window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && this.explainModalOpen) {
          this.closeExplainModal();
        }
      });
    },

    toggleSidebar() {
      this.sidebarOpen = !this.sidebarOpen;
      const sidebar = document.getElementById('sql-reference-sidebar');
      if (sidebar) {
        sidebar.classList.toggle('hidden', !this.sidebarOpen);
      }
    },

    switchSidebarTab(tab) {
      this.activeSidebarTab = tab;
      const catalogBtn = document.getElementById('tab-btn-catalog');
      const snippetsBtn = document.getElementById('tab-btn-snippets');
      const catalogContent = document.getElementById('tab-content-catalog');
      const snippetsContent = document.getElementById('tab-content-snippets');

      if (tab === 'catalog') {
        if (catalogBtn) {
          catalogBtn.className = 'px-3 py-2 text-xs font-semibold border-b-2 border-blue-500 text-blue-400';
        }
        if (snippetsBtn) {
          snippetsBtn.className = 'px-3 py-2 text-xs font-medium border-b-2 border-transparent text-gray-400 hover:text-gray-200';
        }
        if (catalogContent) catalogContent.classList.remove('hidden');
        if (snippetsContent) snippetsContent.classList.add('hidden');
      } else {
        if (catalogBtn) {
          catalogBtn.className = 'px-3 py-2 text-xs font-medium border-b-2 border-transparent text-gray-400 hover:text-gray-200';
        }
        if (snippetsBtn) {
          snippetsBtn.className = 'px-3 py-2 text-xs font-semibold border-b-2 border-blue-500 text-blue-400';
        }
        if (catalogContent) catalogContent.classList.add('hidden');
        if (snippetsContent) snippetsContent.classList.remove('hidden');
      }
    },

    executeRunQuery() {
      if (typeof window.executeRunQuery === 'function' && window.executeRunQuery !== this.executeRunQuery) {
        return window.executeRunQuery();
      }
      const form = document.getElementById('sql-form');
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

      const modal = document.getElementById('explain-modal');
      const content = document.getElementById('explain-content');
      if (modal) modal.classList.remove('hidden');
      if (content) content.textContent = 'Generating EXPLAIN execution plan...';

      if (typeof window.explainCurrentQuery === 'function' && window.explainCurrentQuery !== this.explainCurrentQuery) {
        return window.explainCurrentQuery();
      }
    },

    closeExplainModal() {
      this.explainModalOpen = false;
      const modal = document.getElementById('explain-modal');
      if (modal) modal.classList.add('hidden');
    },

    async generateSqlWithAi() {
      if (typeof window.generateSqlWithAi === 'function' && window.generateSqlWithAi !== this.generateSqlWithAi) {
        return window.generateSqlWithAi();
      }
    },

    async fixQueryWithAi() {
      if (typeof window.fixQueryWithAi === 'function' && window.fixQueryWithAi !== this.fixQueryWithAi) {
        return window.fixQueryWithAi();
      }
    },

    applyFixedSql(andRun = false) {
      if (typeof window.applyFixedSql === 'function' && window.applyFixedSql !== this.applyFixedSql) {
        return window.applyFixedSql(andRun);
      }
    }
  }));

});
