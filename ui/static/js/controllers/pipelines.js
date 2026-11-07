/**
 * Lake of Tears - Pipelines Controller (Alpine.js)
 * Declarative state management for:
 * 1. Pipeline Run Inspector slide-over drawer (runs list, real-time log polling, task graph timeline, cancel run)
 * 2. Schedule Notebook drawer & cron expression editor
 * 3. DAG trigger confirmation modal & parameter bindings
 * 4. Deletion modal with typed confirmation
 * 5. Timeline swimlanes, date navigation, zoom controls, and palette picker
 */

document.addEventListener('alpine:init', () => {

  Alpine.data('pipelinesManager', () => ({
    workspaceSlug: window.CURRENT_WORKSPACE_SLUG || '',
    workspaceId: window.CURRENT_WORKSPACE_ID || '',
    myRole: window.USER_ROLE || 'member',
    allPipelines: [],
    currentFilter: 'all',
    searchQuery: '',
    currentPage: 1,
    pageSize: parseInt(localStorage.getItem('pipelines_page_size') || '10', 10) || 10,
    totalPipelines: 0,

    // Inspector Drawer State
    inspectorOpen: false,
    currentDrawerDagId: null,
    activeDrawerTab: 'details',
    currentSelectedRunId: null,
    currentSelectedTaskId: null,
    activeLogPollingTimer: null,
    activeRunBanner: null,

    // Trigger Modal State
    triggerModalOpen: false,
    triggerDagId: null,
    triggerConfigJson: '{}',
    isTriggering: false,

    // Delete Modal State
    deleteModalOpen: false,
    deleteDagId: null,
    deleteConfirmInput: '',
    isDeleting: false,

    // Timeline State
    timelineCollapsed: false,
    timelineDate: new Date().toISOString().split('T')[0],
    timelineZoom: '24h',
    timelineData: null,
    activeColorPickerDagId: null,

    init() {
      // Expose globally for legacy bridges, external inline handlers, and test assertions
      window.openInspector = (dagId, targetRunId = null) => this.openInspector(dagId, targetRunId);
      window.closeInspector = () => this.closeInspector();
      window.setDrawerTab = (tab) => this.setDrawerTab(tab);
      window.openTriggerModal = (dagId) => this.openTriggerModal(dagId);
      window.closeTriggerModal = () => this.closeTriggerModal();
      window.submitPipelineTrigger = () => this.submitPipelineTrigger();
      window.openDeletePipelineModal = (dagId) => this.openDeletePipelineModal(dagId);
      window.closeDeletePipelineModal = () => this.closeDeletePipelineModal();
      window.confirmDeletePipeline = () => this.confirmDeletePipeline();
      window.togglePipelinePause = (dagId, newPausedState) => this.togglePipelinePause(dagId, newPausedState);
      window.togglePipelinePauseFromDrawer = () => this.togglePipelinePauseFromDrawer();
      window.savePipelineMetadata = (dagId) => this.savePipelineMetadata(dagId);
      window.cancelPipelineRun = (dagId, runId) => this.cancelPipelineRun(dagId, runId);
      window.loadDrawerRuns = (dagId, targetRunId = null) => this.loadDrawerRuns(dagId, targetRunId);
      window.inspectRunTasks = (dagId, runId, isRunning) => this.inspectRunTasks(dagId, runId, isRunning);
      window.loadTaskLogs = (dagId, runId, taskId, tryNum) => this.loadTaskLogs(dagId, runId, taskId, tryNum);
      window.startLogPolling = (dagId, runId, taskId, tryNum) => this.startLogPolling(dagId, runId, taskId, tryNum);
      window.stopLogPolling = () => this.stopLogPolling();
      window.refreshCurrentTaskLogs = () => this.refreshCurrentTaskLogs();
      window.scrollLogsToBottom = () => this.scrollLogsToBottom();
      window.setFilter = (filter) => this.setFilter(filter);
      window.changePipelinesPageSize = (newSize) => this.changePageSize(newSize);
      window.goToPipelinesPage = (page) => this.goToPage(page);

      // Keyboard shortcuts
      window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          if (this.triggerModalOpen) this.closeTriggerModal();
          if (this.deleteModalOpen) this.closeDeletePipelineModal();
          if (this.inspectorOpen) this.closeInspector();
        }
      });
    },

    // ── Inspector Drawer ─────────────────────────────────────────────────────
    async openInspector(dagId, targetRunId = null) {
      this.currentDrawerDagId = dagId;
      window.currentDrawerDagId = dagId;
      this.inspectorOpen = true;

      const drawer = document.getElementById('inspector-drawer');
      if (drawer) {
        drawer.classList.remove('hidden');
      }

      // If existing DOM function renderDrawerDetails exists, call it for full compatibility
      if (typeof window.renderDrawerDetails === 'function') {
        const found = (this.allPipelines || window.allPipelines || []).find(x => x.dag_id === dagId);
        window.renderDrawerDetails(dagId, found || {});
      }

      this.setDrawerTab(targetRunId ? 'runs' : 'details');

      if (targetRunId) {
        this.loadDrawerRuns(dagId, targetRunId);
      }
    },

    closeInspector() {
      this.stopLogPolling();
      this.inspectorOpen = false;
      this.currentDrawerDagId = null;
      window.currentDrawerDagId = null;
      const drawer = document.getElementById('inspector-drawer');
      if (drawer) {
        drawer.classList.add('hidden');
      }
    },

    setDrawerTab(tab) {
      this.activeDrawerTab = tab;
      const tabs = ['details', 'runs', 'logs'];
      tabs.forEach(t => {
        const btn = document.getElementById(`drawer-tab-btn-${t}`);
        const content = document.getElementById(`drawer-tab-${t}`);
        if (!btn || !content) return;
        if (t === tab) {
          btn.className = 'px-4 py-3 text-xs font-semibold border-b-2 border-blue-500 text-blue-400 transition-colors whitespace-nowrap';
          content.classList.remove('hidden');
        } else {
          btn.className = 'px-4 py-3 text-xs font-semibold border-b-2 border-transparent text-gray-400 hover:text-gray-200 transition-colors whitespace-nowrap';
          content.classList.add('hidden');
        }
      });

      if (tab === 'runs' && this.currentDrawerDagId) {
        this.loadDrawerRuns(this.currentDrawerDagId, this.currentSelectedRunId);
      }
    },

    // ── Trigger Modal ────────────────────────────────────────────────────────
    openTriggerModal(dagId) {
      this.triggerDagId = dagId;
      this.triggerModalOpen = true;
      const modal = document.getElementById('trigger-modal');
      const dagNameEl = document.getElementById('trigger-dag-name');
      const confInput = document.getElementById('trigger-conf-input');
      const errEl = document.getElementById('trigger-error');

      if (dagNameEl) dagNameEl.textContent = dagId;
      if (confInput) confInput.value = '{}';
      if (errEl) errEl.classList.add('hidden');
      if (modal) modal.classList.remove('hidden');
    },

    closeTriggerModal() {
      this.triggerModalOpen = false;
      this.triggerDagId = null;
      const modal = document.getElementById('trigger-modal');
      if (modal) modal.classList.add('hidden');
    },

    async submitPipelineTrigger() {
      if (!this.triggerDagId) return;
      const confInput = document.getElementById('trigger-conf-input');
      const submitBtn = document.getElementById('btn-submit-trigger');
      const errEl = document.getElementById('trigger-error');

      let conf = {};
      try {
        conf = JSON.parse((confInput ? confInput.value : '{}') || '{}');
      } catch (e) {
        if (errEl) {
          errEl.textContent = 'Invalid JSON configuration: ' + e.message;
          errEl.classList.remove('hidden');
        }
        return;
      }

      this.isTriggering = true;
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="animate-spin">⏳</span> Triggering...';
      }

      try {
        const resp = await fetch(`/api/pipelines/${encodeURIComponent(this.triggerDagId)}/trigger`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ conf: conf })
        });
        const res = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          throw new Error(res.error || res.detail || 'Failed to trigger pipeline');
        }
        if (window.showToast) window.showToast(`Triggered ${this.triggerDagId} successfully!`, 'success');
        this.closeTriggerModal();
        if (typeof window.fetchPipelines === 'function') window.fetchPipelines();
        if (typeof window.fetchTimeline === 'function') window.fetchTimeline();
      } catch (err) {
        if (errEl) {
          errEl.textContent = err.message;
          errEl.classList.remove('hidden');
        }
      } finally {
        this.isTriggering = false;
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.innerHTML = 'Trigger Pipeline';
        }
      }
    },

    // ── Delete Pipeline Modal ────────────────────────────────────────────────
    openDeletePipelineModal(dagId) {
      this.deleteDagId = dagId;
      this.deleteModalOpen = true;
      this.deleteConfirmInput = '';

      const modal = document.getElementById('pipeline-delete-modal');
      const nameEl = document.getElementById('delete-pipeline-name');
      const input = document.getElementById('delete-pipeline-confirm-input');
      const btn = document.getElementById('btn-confirm-delete-pipeline');

      if (nameEl) nameEl.textContent = dagId;
      if (input) input.value = '';
      if (btn) btn.disabled = true;
      if (modal) modal.classList.remove('hidden');
    },

    closeDeletePipelineModal() {
      this.deleteModalOpen = false;
      this.deleteDagId = null;
      const modal = document.getElementById('pipeline-delete-modal');
      if (modal) modal.classList.add('hidden');
    },

    async confirmDeletePipeline() {
      if (!this.deleteDagId) return;
      this.isDeleting = true;
      const btn = document.getElementById('btn-confirm-delete-pipeline');
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = 'Deleting...';
      }

      try {
        const resp = await fetch(`/api/pipelines/${encodeURIComponent(this.deleteDagId)}`, {
          method: 'DELETE',
          credentials: 'include'
        });
        const res = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          throw new Error(res.error || res.detail || 'Failed to delete pipeline');
        }
        if (window.showToast) window.showToast(`Pipeline ${this.deleteDagId} deleted`, 'success');
        this.closeDeletePipelineModal();
        this.closeInspector();
        if (typeof window.fetchPipelines === 'function') window.fetchPipelines();
        if (typeof window.fetchTimeline === 'function') window.fetchTimeline();
      } catch (err) {
        if (window.showToast) window.showToast(`Delete failed: ${err.message}`, 'error');
      } finally {
        this.isDeleting = false;
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = 'Delete Pipeline';
        }
      }
    },

    // ── Pause / Unpause ──────────────────────────────────────────────────────
    async togglePipelinePause(dagId, newPausedState) {
      try {
        const resp = await fetch(`/api/pipelines/${encodeURIComponent(dagId)}/pause`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ is_paused: newPausedState })
        });
        const res = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          throw new Error(res.error || res.detail || 'Failed to toggle pipeline pause');
        }
        if (window.showToast) window.showToast(`Pipeline ${dagId} ${newPausedState ? 'paused' : 'unpaused'}`, 'success');
        if (typeof window.fetchPipelines === 'function') window.fetchPipelines();
      } catch (err) {
        if (window.showToast) window.showToast(err.message, 'error');
      }
    },

    async togglePipelinePauseFromDrawer() {
      if (!this.currentDrawerDagId) return;
      const p = (this.allPipelines || window.allPipelines || []).find(x => x.dag_id === this.currentDrawerDagId);
      const isPaused = p ? p.is_paused : false;
      await this.togglePipelinePause(this.currentDrawerDagId, !isPaused);
      if (p) p.is_paused = !isPaused;
      if (typeof window.renderDrawerDetails === 'function') {
        window.renderDrawerDetails(this.currentDrawerDagId, p || {});
      }
    },

    // ── Log Polling ──────────────────────────────────────────────────────────
    startLogPolling(dagId, runId, taskId, tryNum = 1) {
      this.stopLogPolling();
      this.activeLogPollingTimer = setInterval(() => {
        if (!this.inspectorOpen || this.activeDrawerTab !== 'logs') {
          this.stopLogPolling();
          return;
        }
        this.loadTaskLogs(dagId, runId, taskId, tryNum);
      }, 3000);
    },

    stopLogPolling() {
      if (this.activeLogPollingTimer) {
        clearInterval(this.activeLogPollingTimer);
        this.activeLogPollingTimer = null;
      }
    },

    async loadTaskLogs(dagId, runId, taskId, tryNum = 1) {
      if (typeof window.loadTaskLogs === 'function' && window.loadTaskLogs !== this.loadTaskLogs) {
        return window.loadTaskLogs(dagId, runId, taskId, tryNum);
      }
    },

    refreshCurrentTaskLogs() {
      if (this.currentDrawerDagId && this.currentSelectedRunId && this.currentSelectedTaskId) {
        this.loadTaskLogs(this.currentDrawerDagId, this.currentSelectedRunId, this.currentSelectedTaskId, 1);
      }
    },

    scrollLogsToBottom() {
      const container = document.getElementById('task-logs-container');
      if (container) {
        container.scrollTop = container.scrollHeight;
      }
    },

    // ── Pagination & Filter Controls ─────────────────────────────────────────
    setFilter(filter) {
      this.currentFilter = filter;
      if (typeof window.setFilter === 'function' && window.setFilter !== this.setFilter) {
        window.setFilter(filter);
      }
    },

    changePageSize(newSize) {
      this.pageSize = parseInt(newSize, 10);
      localStorage.setItem('pipelines_page_size', String(this.pageSize));
      if (typeof window.applyFilters === 'function') {
        window.applyFilters(true);
      }
    },

    goToPage(page) {
      this.currentPage = page;
      if (typeof window.applyFilters === 'function') {
        window.applyFilters(false);
      }
    }
  }));

});
