/**
 * Lake of Tears - Pipelines Controller (Alpine.js)
 * Declarative state management for:
 * 1. Pipeline Run Inspector slide-over drawer (runs list, real-time log polling, task graph timeline, cancel run)
 * 2. Schedule & settings metadata management (display name, description, cron schedule)
 * 3. DAG trigger confirmation modal & parameter bindings
 * 4. Deletion modal with confirmation
 * 5. Pause / Unpause toggle
 * 6. Timeline swimlanes, date navigation, zoom controls, and palette picker
 */

document.addEventListener('alpine:init', () => {

  // Capture existing global functions defined in inline scripts if already loaded
  const _origOpenInspector = window.openInspector;
  const _origCloseInspector = window.closeInspector;
  const _origSetDrawerTab = window.setDrawerTab;
  const _origOpenTriggerModal = window.openTriggerModal;
  const _origCloseTriggerModal = window.closeTriggerModal;
  const _origSubmitPipelineTrigger = window.submitPipelineTrigger;
  const _origOpenDeletePipelineModal = window.openDeletePipelineModal;
  const _origCloseDeletePipelineModal = window.closeDeletePipelineModal;
  const _origConfirmDeletePipeline = window.confirmDeletePipeline;
  const _origTogglePipelinePause = window.togglePipelinePause;
  const _origTogglePipelinePauseFromDrawer = window.togglePipelinePauseFromDrawer;
  const _origSavePipelineMetadata = window.savePipelineMetadata;
  const _origCancelPipelineRun = window.cancelPipelineRun;
  const _origLoadDrawerRuns = window.loadDrawerRuns;
  const _origInspectRunTasks = window.inspectRunTasks;
  const _origLoadTaskLogs = window.loadTaskLogs;
  const _origStartLogPolling = window.startLogPolling;
  const _origStopLogPolling = window.stopLogPolling;
  const _origRefreshCurrentTaskLogs = window.refreshCurrentTaskLogs;
  const _origScrollLogsToBottom = window.scrollLogsToBottom;
  const _origSetFilter = window.setFilter;
  const _origChangePipelinesPageSize = window.changePipelinesPageSize;
  const _origGoToPipelinesPage = window.goToPipelinesPage;

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
    activeDrawerTab: 'runs',
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
      if (typeof _origOpenInspector === 'function') {
        this.currentDrawerDagId = dagId;
        this.inspectorOpen = true;
        return _origOpenInspector(dagId, targetRunId);
      }

      this.currentDrawerDagId = dagId;
      window.currentDrawerDagId = dagId;
      this.inspectorOpen = true;

      const drawer = document.getElementById('inspector-drawer');
      if (drawer) {
        drawer.classList.remove('hidden');
      }

      let p = (window.allPipelines || this.allPipelines || []).find(item => item.dag_id === dagId);
      if (!p) {
        try {
          const resp = await fetch(`/api/pipelines/${encodeURIComponent(dagId)}`);
          if (resp.ok) {
            p = await resp.json();
          }
        } catch (e) {}
      }

      if (typeof window.renderDrawerDetails === 'function') {
        window.renderDrawerDetails(dagId, p || {});
      }

      this.setDrawerTab('runs');
      await this.loadDrawerRuns(dagId, targetRunId);
    },

    closeInspector() {
      if (typeof _origCloseInspector === 'function') {
        this.inspectorOpen = false;
        this.currentDrawerDagId = null;
        return _origCloseInspector();
      }

      this.stopLogPolling();
      this.inspectorOpen = false;
      this.currentDrawerDagId = null;
      window.currentDrawerDagId = null;
      const drawer = document.getElementById('inspector-drawer');
      if (drawer) {
        drawer.classList.add('hidden');
      }
      const runDropdown = document.getElementById('drawer-run-dropdown');
      if (runDropdown) runDropdown.classList.add('hidden');
      const deleteBtn = document.getElementById('btn-delete-pipeline-drawer');
      if (deleteBtn) deleteBtn.classList.add('hidden');
    },

    setDrawerTab(tab) {
      this.activeDrawerTab = tab;
      if (typeof _origSetDrawerTab === 'function') {
        return _origSetDrawerTab(tab);
      }

      const tabRuns = document.getElementById('drawer-tab-runs');
      const tabLogs = document.getElementById('drawer-tab-logs');
      const paneRuns = document.getElementById('drawer-pane-runs');
      const paneLogs = document.getElementById('drawer-pane-logs');

      if (tab === 'runs') {
        if (tabRuns) tabRuns.className = 'px-3 py-2.5 border-b-2 border-blue-500 text-blue-400 font-medium';
        if (tabLogs) tabLogs.className = 'px-3 py-2.5 border-b-2 border-transparent text-gray-400 hover:text-gray-200 font-medium';
        if (paneRuns) paneRuns.classList.remove('hidden');
        if (paneLogs) paneLogs.classList.add('hidden');
        this.stopLogPolling();
      } else {
        if (tabLogs) tabLogs.className = 'px-3 py-2.5 border-b-2 border-blue-500 text-blue-400 font-medium';
        if (tabRuns) tabRuns.className = 'px-3 py-2.5 border-b-2 border-transparent text-gray-400 hover:text-gray-200 font-medium';
        if (paneLogs) paneLogs.classList.remove('hidden');
        if (paneRuns) paneRuns.classList.add('hidden');
      }
    },

    // ── Save Pipeline Metadata (Display Name, Description, Schedule) ──────────
    async savePipelineMetadata(dagId) {
      if (typeof _origSavePipelineMetadata === 'function') {
        return _origSavePipelineMetadata(dagId);
      }

      const targetDagId = dagId || this.currentDrawerDagId || window.currentDrawerDagId;
      if (!targetDagId || this.myRole === 'viewer') return;

      const nameInput = document.getElementById('drawer-name-input');
      const descInput = document.getElementById('drawer-desc-input');
      const display_label = nameInput ? nameInput.value.trim() : null;
      const description = descInput ? descInput.value.trim() : null;

      let selectedSchedule = 'None';
      if (typeof window.getDrawerSelectedSchedule === 'function') {
        selectedSchedule = window.getDrawerSelectedSchedule();
      } else {
        const preset = document.getElementById('drawer-schedule-preset');
        if (preset) {
          if (preset.value === 'custom') {
            const customInput = document.getElementById('drawer-custom-cron-input');
            selectedSchedule = customInput && customInput.value.trim() ? customInput.value.trim() : 'None';
          } else {
            selectedSchedule = preset.value;
          }
        }
      }

      const originalSched = window.originalDrawerSchedule || 'None';
      const canEdit = typeof window.canEditSchedule === 'function' ? window.canEditSchedule(targetDagId) : true;
      const scheduleChanged = canEdit && selectedSchedule !== originalSched;

      const btn = document.getElementById('btn-save-drawer-meta');
      if (btn) {
        btn.disabled = true;
        btn.textContent = 'Saving...';
      }

      try {
        if (scheduleChanged) {
          const schedRes = await fetch(`/api/pipelines/${encodeURIComponent(targetDagId)}/schedule`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ schedule_interval: selectedSchedule }),
          });
          const schedData = await schedRes.json();
          if (!schedRes.ok) {
            throw new Error(schedData.detail || 'Failed to update pipeline schedule');
          }
          window.originalDrawerSchedule = schedData.schedule_interval;
        }

        const webhookToggle = document.getElementById('drawer-webhook-toggle');
        const webhook_enabled = webhookToggle ? webhookToggle.checked : undefined;

        const res = await fetch(`/api/pipelines/${encodeURIComponent(targetDagId)}/settings`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ display_label, description, webhook_enabled }),
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.detail || 'Failed to update pipeline settings');
        }

        if (window.showToast) {
          window.showToast(scheduleChanged ? 'Pipeline settings and schedule updated' : 'Pipeline name and description updated', 'success');
        }

        const pipelines = window.allPipelines || this.allPipelines || [];
        const p = pipelines.find(item => item.dag_id === targetDagId);
        if (p) {
          if (display_label) {
            p.display_label = display_label;
            p.pipeline_name = display_label;
          }
          if (description !== null) p.description = description;
          if (scheduleChanged) {
            p.schedule_interval = window.originalDrawerSchedule;
          }
          if (webhook_enabled !== undefined) {
            p.webhook_enabled = webhook_enabled;
          }
          if (typeof window.renderDrawerDetails === 'function') {
            window.renderDrawerDetails(targetDagId, p);
          }
        }

        if (typeof window.applyFilters === 'function') window.applyFilters(false);
        if (typeof window.fetchTimeline === 'function') window.fetchTimeline();
      } catch (err) {
        if (window.showToast) window.showToast(err.message, 'error');
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = 'Save';
        }
      }
    },

    // ── Runs and Task Inspector ──────────────────────────────────────────────
    async loadDrawerRuns(dagId, targetRunId = null) {
      if (typeof _origLoadDrawerRuns === 'function') {
        return _origLoadDrawerRuns(dagId, targetRunId);
      }

      const targetDagId = dagId || this.currentDrawerDagId;
      if (!targetDagId) return;

      const listEl = document.getElementById('drawer-runs-list');
      if (listEl) {
        listEl.innerHTML = '<div class="py-8 text-center text-gray-500">Loading historical runs...</div>';
      }

      try {
        const wsQuery = this.workspaceSlug || window.currentWorkspaceSlug ? `?workspace_slug=${encodeURIComponent(this.workspaceSlug || window.currentWorkspaceSlug)}` : '';
        const res = await fetch(`/api/pipelines/${encodeURIComponent(targetDagId)}/runs${wsQuery}`);
        if (!res.ok) throw new Error('Failed to load runs');
        const data = await res.json();
        const runs = data.runs || [];

        const countEl = document.getElementById('drawer-runs-count');
        if (countEl) countEl.textContent = runs.length;

        if (listEl) {
          if (runs.length === 0) {
            listEl.innerHTML = '<div class="py-8 text-center text-gray-500 text-xs">No historical runs recorded for this pipeline.</div>';
            return;
          }

          listEl.innerHTML = runs.map(r => {
            const isRunning = (r.state === 'running');
            const isTarget = Boolean(targetRunId && r.dag_run_id === targetRunId);
            return `
              <div id="drawer-run-${r.dag_run_id}" class="p-3 bg-white/[0.02] border ${isTarget ? 'border-blue-500 ring-2 ring-blue-500/40 bg-blue-500/[0.05]' : 'border-white/[0.06]'} rounded-lg hover:border-white/[0.12] transition-colors space-y-2">
                <div class="flex items-center justify-between text-xs">
                  <span class="font-mono text-gray-200 font-medium truncate max-w-xs ${isTarget ? 'text-blue-300 font-bold' : ''}">${r.dag_run_id}</span>
                  <span class="px-2 py-0.5 rounded text-[10px] font-medium ${
                    r.state === 'success' ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' :
                    isRunning             ? 'bg-blue-500/10 text-blue-400 border border-blue-500/20 animate-pulse' :
                    r.state === 'failed'  ? 'bg-rose-500/10 text-rose-400 border border-rose-500/20' :
                                            'bg-gray-500/10 text-gray-400 border border-gray-500/20'
                  }">${r.state}</span>
                </div>
                <div class="flex items-center justify-between text-[11px] text-gray-500 font-mono">
                  <span>Started: ${r.start_date ? new Date(r.start_date).toLocaleString() : (r.state === 'queued' ? 'queued' : '—')}</span>
                  <span>Duration: ${r.duration_seconds !== null ? `${Math.round(r.duration_seconds)}s` : '—'}</span>
                </div>
                <div class="pt-2 border-t border-white/[0.04] flex items-center justify-between">
                  <button onclick="inspectRunTasks('${targetDagId}', '${r.dag_run_id}', ${isRunning})"
                          class="text-xs text-blue-400 hover:text-blue-300 font-medium flex items-center gap-1">
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-linecap="round" stroke-linejoin="round" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/></svg>
                    Inspect Tasks & Logs
                  </button>
                  ${(isRunning || r.state === 'queued') && this.myRole !== 'viewer' ? `
                    <button onclick="cancelPipelineRun('${targetDagId}', '${r.dag_run_id}')"
                            class="text-xs text-rose-400 hover:text-rose-300 font-medium flex items-center gap-1 px-2 py-0.5 rounded bg-rose-500/10 border border-rose-500/20 hover:bg-rose-500/20 transition-colors">
                      <svg class="w-3 h-3" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
                      Cancel Run
                    </button>
                  ` : ''}
                </div>
              </div>
            `;
          }).join('');
        }
      } catch (err) {
        if (listEl) {
          listEl.innerHTML = `<div class="py-8 text-center text-rose-400 text-xs">Error loading runs: ${err.message}</div>`;
        }
      }
    },

    async inspectRunTasks(dagId, runId, isRunning) {
      if (typeof _origInspectRunTasks === 'function') {
        return _origInspectRunTasks(dagId, runId, isRunning);
      }

      this.currentSelectedRunId = runId;
      this.setDrawerTab('logs');

      const tasksList = document.getElementById('drawer-runs-list');
      if (tasksList) {
        tasksList.innerHTML = '<div class="p-3 text-xs text-gray-500 text-center">Loading task instances...</div>';
      }

      try {
        const wsQuery = this.workspaceSlug || window.currentWorkspaceSlug ? `?workspace_slug=${encodeURIComponent(this.workspaceSlug || window.currentWorkspaceSlug)}` : '';
        const res = await fetch(`/api/pipelines/${encodeURIComponent(dagId)}/runs/${encodeURIComponent(runId)}/tasks${wsQuery}`);
        if (!res.ok) throw new Error('Failed to load task instances');
        const data = await res.json();
        const tasks = data.tasks || [];

        if (tasksList) {
          if (tasks.length === 0) {
            tasksList.innerHTML = '<div class="p-3 text-xs text-gray-500 text-center">No task instances found.</div>';
            return;
          }

          tasksList.innerHTML = tasks.map((t, idx) => `
            <button onclick="loadTaskLogs('${dagId}', '${runId}', '${t.task_id}', ${t.try_number || 1})"
                    id="btn-task-${t.task_id}"
                    class="w-full text-left p-2.5 rounded-lg border border-white/[0.06] hover:border-white/20 bg-white/[0.02] flex items-center justify-between text-xs transition-colors">
              <span class="font-mono text-gray-200 truncate">${t.task_id}</span>
              <span class="px-1.5 py-0.5 rounded text-[10px] font-medium font-mono ${
                t.state === 'success' ? 'bg-emerald-500/10 text-emerald-400' :
                t.state === 'running' ? 'bg-blue-500/10 text-blue-400 animate-pulse' :
                t.state === 'failed'  ? 'bg-rose-500/10 text-rose-400' : 'bg-gray-500/10 text-gray-400'
              }">${t.state || 'none'}</span>
            </button>
          `).join('');
        }

        if (tasks.length > 0) {
          this.currentSelectedTaskId = tasks[0].task_id;
          await this.loadTaskLogs(dagId, runId, tasks[0].task_id, tasks[0].try_number || 1);
          if (isRunning) {
            this.startLogPolling(dagId, runId, tasks[0].task_id, tasks[0].try_number || 1);
          }
        }
      } catch (err) {
        if (tasksList) {
          tasksList.innerHTML = `<div class="p-3 text-xs text-rose-400 text-center">Error: ${err.message}</div>`;
        }
      }
    },

    async cancelPipelineRun(dagId, runId) {
      if (typeof _origCancelPipelineRun === 'function') {
        return _origCancelPipelineRun(dagId, runId);
      }

      if (!confirm(`Are you sure you want to cancel run ${runId}?`)) return;

      try {
        const wsQuery = this.workspaceSlug || window.currentWorkspaceSlug ? `?workspace_slug=${encodeURIComponent(this.workspaceSlug || window.currentWorkspaceSlug)}` : '';
        const res = await fetch(`/api/pipelines/${encodeURIComponent(dagId)}/runs/${encodeURIComponent(runId)}/cancel${wsQuery}`, {
          method: 'POST',
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || data.error || 'Failed to cancel DAG run');
        if (window.showToast) window.showToast(`Run ${runId} cancelled successfully`, 'success');
        await this.loadDrawerRuns(dagId);
        if (typeof window.fetchPipelines === 'function') window.fetchPipelines();
        if (typeof window.fetchTimeline === 'function') window.fetchTimeline();
      } catch (err) {
        if (window.showToast) window.showToast(err.message, 'error');
      }
    },

    // ── Trigger Modal ────────────────────────────────────────────────────────
    openTriggerModal(dagId) {
      if (typeof _origOpenTriggerModal === 'function') {
        this.triggerDagId = dagId;
        this.triggerModalOpen = true;
        return _origOpenTriggerModal(dagId);
      }

      this.triggerDagId = dagId;
      this.triggerModalOpen = true;
      const modal = document.getElementById('trigger-modal');
      const dagNameEl = document.getElementById('trigger-dag-id');
      const confInput = document.getElementById('trigger-params');

      if (dagNameEl) dagNameEl.textContent = dagId;
      if (confInput) confInput.value = '{}';
      if (modal) modal.classList.remove('hidden');
    },

    closeTriggerModal() {
      if (typeof _origCloseTriggerModal === 'function') {
        this.triggerModalOpen = false;
        this.triggerDagId = null;
        return _origCloseTriggerModal();
      }

      this.triggerModalOpen = false;
      this.triggerDagId = null;
      const modal = document.getElementById('trigger-modal');
      if (modal) modal.classList.add('hidden');
    },

    async submitPipelineTrigger() {
      if (typeof _origSubmitPipelineTrigger === 'function') {
        return _origSubmitPipelineTrigger();
      }

      const targetDagId = this.triggerDagId || window.targetTriggerDagId;
      if (!targetDagId) return;

      const confInput = document.getElementById('trigger-params');
      const submitBtn = document.getElementById('btn-submit-trigger');

      let conf = {};
      try {
        conf = JSON.parse((confInput ? confInput.value : '{}') || '{}');
      } catch (e) {
        if (window.showToast) window.showToast('Invalid JSON in parameters field', 'error');
        return;
      }

      this.isTriggering = true;
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = 'Triggering...';
      }

      try {
        const resp = await fetch(`/api/pipelines/${encodeURIComponent(targetDagId)}/trigger`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ conf })
        });
        const res = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          throw new Error(res.error || res.detail || 'Failed to trigger pipeline');
        }
        if (window.showToast) window.showToast(`Run queued! ID: ${res.dag_run_id || targetDagId}`, 'success');
        this.closeTriggerModal();
        if (typeof window.fetchPipelines === 'function') window.fetchPipelines();
        if (typeof window.fetchTimeline === 'function') window.fetchTimeline();
        this.openInspector(targetDagId, res.dag_run_id);
      } catch (err) {
        if (window.showToast) window.showToast(err.message, 'error');
      } finally {
        this.isTriggering = false;
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = 'Trigger Run';
        }
      }
    },

    // ── Delete Pipeline Modal ────────────────────────────────────────────────
    openDeletePipelineModal(dagId) {
      if (typeof _origOpenDeletePipelineModal === 'function') {
        this.deleteDagId = dagId;
        this.deleteModalOpen = true;
        return _origOpenDeletePipelineModal(dagId);
      }

      if (!dagId || this.myRole === 'viewer') return;
      this.deleteDagId = dagId;
      window.pipelinePendingDeleteDagId = dagId;
      this.deleteModalOpen = true;

      const p = (window.allPipelines || this.allPipelines || []).find(item => item.dag_id === dagId);
      const displayName = p ? (p.display_label || p.pipeline_name || dagId) : dagId;

      const nameEl = document.getElementById('delete-pipeline-name');
      if (nameEl) nameEl.textContent = displayName;
      const dagEl = document.getElementById('delete-dag-id');
      if (dagEl) dagEl.textContent = dagId;

      const btn = document.getElementById('btn-confirm-delete-pipeline');
      if (btn) btn.disabled = false;

      const modal = document.getElementById('pipeline-delete-modal');
      if (modal) modal.classList.remove('hidden');
    },

    closeDeletePipelineModal() {
      if (typeof _origCloseDeletePipelineModal === 'function') {
        this.deleteModalOpen = false;
        this.deleteDagId = null;
        return _origCloseDeletePipelineModal();
      }

      this.deleteModalOpen = false;
      this.deleteDagId = null;
      window.pipelinePendingDeleteDagId = null;
      const modal = document.getElementById('pipeline-delete-modal');
      if (modal) modal.classList.add('hidden');
    },

    async confirmDeletePipeline() {
      if (typeof _origConfirmDeletePipeline === 'function') {
        return _origConfirmDeletePipeline();
      }

      const targetDagId = this.deleteDagId || window.pipelinePendingDeleteDagId || this.currentDrawerDagId;
      if (!targetDagId || this.myRole === 'viewer') return;

      this.isDeleting = true;
      const btn = document.getElementById('btn-confirm-delete-pipeline');
      const spinner = document.getElementById('delete-pipeline-btn-spinner');
      const btnText = document.getElementById('delete-pipeline-btn-text');

      if (btn) btn.disabled = true;
      if (spinner) spinner.classList.remove('hidden');
      if (btnText) btnText.textContent = 'Deleting...';

      try {
        const resp = await fetch(`/api/pipelines/${encodeURIComponent(targetDagId)}`, {
          method: 'DELETE',
          credentials: 'include'
        });
        const res = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          throw new Error(res.error || res.detail || 'Failed to delete pipeline');
        }
        if (window.showToast) window.showToast(`Pipeline ${targetDagId} deleted`, 'success');
        this.closeDeletePipelineModal();
        this.closeInspector();
        if (typeof window.fetchPipelines === 'function') window.fetchPipelines();
        if (typeof window.fetchTimeline === 'function') window.fetchTimeline();
      } catch (err) {
        if (window.showToast) window.showToast(`Delete failed: ${err.message}`, 'error');
      } finally {
        this.isDeleting = false;
        if (btn) btn.disabled = false;
        if (spinner) spinner.classList.add('hidden');
        if (btnText) btnText.textContent = 'Delete Pipeline';
      }
    },

    // ── Pause / Unpause ──────────────────────────────────────────────────────
    async togglePipelinePause(dagId, newPausedState) {
      if (typeof _origTogglePipelinePause === 'function') {
        return _origTogglePipelinePause(dagId, newPausedState);
      }

      if (this.myRole === 'viewer' || !dagId) return;

      try {
        const resp = await fetch(`/api/pipelines/${encodeURIComponent(dagId)}`, {
          method: 'PATCH',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ is_paused: newPausedState })
        });
        const res = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          throw new Error(res.error || res.detail || 'Failed to toggle pipeline pause');
        }
        if (window.showToast) window.showToast(`Pipeline ${dagId} ${newPausedState ? 'paused' : 'unpaused'}`, 'success');

        const pipelines = window.allPipelines || this.allPipelines || [];
        const p = pipelines.find(x => x.dag_id === dagId);
        if (p) p.is_paused = newPausedState;

        if (typeof window.fetchPipelines === 'function') window.fetchPipelines();
      } catch (err) {
        if (window.showToast) window.showToast(err.message, 'error');
      }
    },

    async togglePipelinePauseFromDrawer() {
      if (typeof _origTogglePipelinePauseFromDrawer === 'function') {
        return _origTogglePipelinePauseFromDrawer();
      }

      const targetDagId = this.currentDrawerDagId || window.currentDrawerDagId;
      if (!targetDagId || this.myRole === 'viewer') return;

      const pipelines = window.allPipelines || this.allPipelines || [];
      const p = pipelines.find(x => x.dag_id === targetDagId);
      const isPaused = p ? p.is_paused : false;
      await this.togglePipelinePause(targetDagId, !isPaused);
      if (p) {
        p.is_paused = !isPaused;
        if (typeof window.renderDrawerDetails === 'function') {
          window.renderDrawerDetails(targetDagId, p);
        }
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
      if (typeof _origLoadTaskLogs === 'function') {
        return _origLoadTaskLogs(dagId, runId, taskId, tryNum);
      }

      try {
        const wsParam = this.workspaceSlug || window.currentWorkspaceSlug ? `&workspace_slug=${encodeURIComponent(this.workspaceSlug || window.currentWorkspaceSlug)}` : '';
        const res = await fetch(`/api/pipelines/${encodeURIComponent(dagId)}/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(taskId)}/logs?try_number=${tryNum}${wsParam}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const pre = document.getElementById('log-pre');
        if (pre) {
          pre.textContent = data.content || 'No log output captured.';
        }
        const activeTaskId = document.getElementById('log-target-task');
        if (activeTaskId) activeTaskId.textContent = `${taskId} (${runId})`;
      } catch (err) {
        const pre = document.getElementById('log-pre');
        if (pre) pre.textContent = `Failed to load logs: ${err.message}`;
      }
    },

    refreshCurrentTaskLogs() {
      if (typeof _origRefreshCurrentTaskLogs === 'function') {
        return _origRefreshCurrentTaskLogs();
      }
      if (this.currentDrawerDagId && this.currentSelectedRunId && this.currentSelectedTaskId) {
        this.loadTaskLogs(this.currentDrawerDagId, this.currentSelectedRunId, this.currentSelectedTaskId, 1);
      }
    },

    scrollLogsToBottom() {
      if (typeof _origScrollLogsToBottom === 'function') {
        return _origScrollLogsToBottom();
      }
      const container = document.getElementById('log-terminal');
      if (container) {
        container.scrollTop = container.scrollHeight;
      }
    },

    // ── Pagination & Filter Controls ─────────────────────────────────────────
    setFilter(filter) {
      this.currentFilter = filter;
      if (typeof _origSetFilter === 'function') {
        return _origSetFilter(filter);
      }
      if (typeof window.applyFilters === 'function') {
        window.applyFilters(true);
      }
    },

    changePageSize(newSize) {
      this.pageSize = parseInt(newSize, 10);
      localStorage.setItem('pipelines_page_size', String(this.pageSize));
      if (typeof _origChangePipelinesPageSize === 'function') {
        return _origChangePipelinesPageSize(newSize);
      }
      if (typeof window.applyFilters === 'function') {
        window.applyFilters(true);
      }
    },

    goToPage(page) {
      this.currentPage = page;
      if (typeof _origGoToPipelinesPage === 'function') {
        return _origGoToPipelinesPage(page);
      }
      if (typeof window.applyFilters === 'function') {
        window.applyFilters(false);
      }
    }
  }));

});
