/**
 * Lake of Tears - Admin & Settings Controller (Alpine.js)
 * Manages Admin tabs, Platform Services observability & lifecycle drawer,
 * and AI model discovery with zero node/npm build dependencies.
 */
document.addEventListener('alpine:init', () => {
  Alpine.data('adminTabsManager', () => ({
    activeTab: 'services',
    tabs: ['services', 'users', 'workspaces', 'vaults', 'platform'],

    init() {
      // Expose globally for legacy inline calls and programmatic test verification
      window.switchAdminTab = (tab) => this.switchTab(tab);

      // Read initial hash or query parameter
      const hash = (window.location.hash || '').replace('#', '');
      const urlParam = new URLSearchParams(window.location.search).get('tab');
      const target = urlParam || hash;
      this.resolveInitialTab(target);

      // Listen for browser navigation hash change
      window.addEventListener('hashchange', () => {
        const h = (window.location.hash || '').replace('#', '');
        if (h && this.tabs.includes(h)) {
          this.switchTab(h, false);
        }
      });
    },

    resolveInitialTab(target) {
      if (['invitations', 'active-sessions', 'users'].includes(target)) {
        this.switchTab('users');
      } else if (target === 'workspaces') {
        this.switchTab('workspaces');
      } else if (['vault-settings', 'vaults'].includes(target)) {
        this.switchTab('vaults');
      } else if (['platform', 'settings', 'auth-settings', 'ai-settings'].includes(target)) {
        this.switchTab('platform');
      } else {
        this.switchTab('services');
      }
    },

    switchTab(tabName, updateHash = true) {
      if (!this.tabs.includes(tabName)) {
        tabName = 'services';
      }
      this.activeTab = tabName;

      // Update URL hash without full reload
      if (updateHash && window.location.hash !== '#' + tabName) {
        history.replaceState(null, '', '#' + tabName);
      }

      // Synchronize tab panels
      this.tabs.forEach(t => {
        const btn = document.getElementById('admin-tab-btn-' + t);
        const panel = document.getElementById('admin-panel-' + t);
        if (btn) {
          if (t === tabName) {
            btn.className = 'admin-tab-btn flex items-center gap-2 px-4 py-2.5 text-xs font-medium border-b-2 transition-colors whitespace-nowrap border-blue-500 text-blue-400';
          } else {
            btn.className = 'admin-tab-btn flex items-center gap-2 px-4 py-2.5 text-xs font-medium border-b-2 transition-colors whitespace-nowrap border-transparent text-gray-400 hover:text-gray-200';
          }
        }
        if (panel) {
          if (t === tabName) {
            panel.classList.remove('hidden');
          } else {
            panel.classList.add('hidden');
          }
        }
      });

      // Dispatch event for services polling lifecycle
      window.dispatchEvent(new CustomEvent('admin-tab-changed', { detail: { tab: tabName } }));
    },

    tabClass(tabName) {
      return this.activeTab === tabName
        ? 'admin-tab-btn flex items-center gap-2 px-4 py-2.5 text-xs font-medium border-b-2 transition-colors whitespace-nowrap border-blue-500 text-blue-400'
        : 'admin-tab-btn flex items-center gap-2 px-4 py-2.5 text-xs font-medium border-b-2 transition-colors whitespace-nowrap border-transparent text-gray-400 hover:text-gray-200';
    }
  }));

  Alpine.data('servicesManager', (initialServices = []) => ({
    services: Array.isArray(initialServices) ? initialServices : [],
    servicesMap: {},
    selectedService: null,
    isDrawerOpen: false,
    isAutoPoll: true,
    autoPollTimer: null,
    isRefreshing: false,
    lastUpdated: 'Just now',
    healthyCount: 0,
    totalCount: 0,

    confirmModal: {
      isOpen: false,
      serviceId: null,
      serviceName: null,
      action: null,
      title: 'Confirm Action',
      message: '',
      isSubmitting: false,
      color: 'blue'
    },

    init() {
      // Expose globally for legacy inline calls and programmatic test verification
      window.openServiceDrawer = (id) => this.openDrawer(id);
      window.closeServiceDrawer = () => this.closeDrawer();
      window.promptServiceAction = (id, name, act) => this.promptAction(id, name, act);
      window.executeConfirmedServiceAction = () => this.executeAction();
      window.closeServiceConfirmModal = () => this.closeConfirmModal();
      window.toggleAutoPoll = (enabled) => this.toggleAutoPoll(enabled);
      window.refreshServices = (manual) => this.refresh(manual);

      this.indexServices();
      this.computeHealth();

      // Start auto-poll if active
      this.startAutoPoll();

      // Listen for tab changes from adminTabsManager
      window.addEventListener('admin-tab-changed', (e) => {
        if (e.detail && e.detail.tab === 'services') {
          this.startAutoPoll();
        } else {
          this.stopAutoPoll();
        }
      });

      // Handle Escape key to close modal or drawer
      window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          if (this.confirmModal.isOpen) {
            this.closeConfirmModal();
          } else if (this.isDrawerOpen) {
            this.closeDrawer();
          }
        }
      });
    },

    indexServices() {
      this.servicesMap = {};
      if (Array.isArray(this.services)) {
        this.services.forEach(s => {
          if (s && s.id) this.servicesMap[s.id] = s;
        });
      }
      window._servicesData = this.services;
      window._servicesMap = this.servicesMap;
    },

    computeHealth() {
      this.totalCount = this.services.length;
      this.healthyCount = this.services.filter(s => s && s.status === 'running').length;
    },

    startAutoPoll() {
      this.stopAutoPoll();
      const toggle = document.getElementById('services-autopoll-toggle');
      if (toggle && !toggle.checked) return;
      if (!this.isAutoPoll) return;

      this.autoPollTimer = setInterval(() => {
        const servicesPanel = document.getElementById('admin-panel-services');
        if (!document.hidden && servicesPanel && !servicesPanel.classList.contains('hidden')) {
          this.refresh(false);
        }
      }, 10000);
    },

    stopAutoPoll() {
      if (this.autoPollTimer) {
        clearInterval(this.autoPollTimer);
        this.autoPollTimer = null;
      }
    },

    toggleAutoPoll(enabled) {
      this.isAutoPoll = !!enabled;
      const dot = document.getElementById('autopoll-pulse-dot');
      if (this.isAutoPoll) {
        if (dot) dot.className = 'w-1.5 h-1.5 rounded-full bg-blue-500 animate-pulse';
        this.startAutoPoll();
      } else {
        if (dot) dot.className = 'w-1.5 h-1.5 rounded-full bg-gray-600';
        this.stopAutoPoll();
      }
    },

    async refresh(manual = false) {
      const spinner = document.getElementById('services-refresh-spinner');
      if (manual && spinner) {
        spinner.classList.add('animate-spin');
      }
      this.isRefreshing = !!manual;

      try {
        const res = await fetch('/settings/admin/services');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (data) {
          this.services = data.services || [];
          this.totalCount = data.total_services || this.services.length;
          this.healthyCount = data.healthy_count || 0;
          this.indexServices();

          const now = new Date();
          this.lastUpdated = 'Updated: ' + now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

          const updatedEl = document.getElementById('services-last-updated');
          if (updatedEl) updatedEl.textContent = this.lastUpdated;

          const countEl = document.getElementById('services-health-count');
          const badgeEl = document.getElementById('services-health-badge');
          if (countEl && badgeEl) {
            countEl.textContent = this.healthyCount + ' / ' + this.totalCount + ' Healthy';
            if (this.healthyCount === this.totalCount && this.totalCount > 0) {
              badgeEl.className = 'inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] font-medium bg-emerald-950/60 text-emerald-400 border border-emerald-800/60';
            } else {
              badgeEl.className = 'inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] font-medium bg-amber-950/60 text-amber-400 border border-amber-800/60';
            }
          }

          // Sync open drawer if currently viewing a service
          if (this.selectedService && this.servicesMap[this.selectedService.id]) {
            this.selectedService = this.servicesMap[this.selectedService.id];
            this.populateServiceDrawer(this.selectedService);
          }
        }
      } catch (err) {
        console.error('Failed to refresh services:', err);
      } finally {
        if (manual && spinner) {
          setTimeout(() => spinner.classList.remove('animate-spin'), 400);
        }
        this.isRefreshing = false;
      }
    },

    openDrawer(serviceId) {
      const service = this.servicesMap[serviceId] || this.services.find(s => s && s.id === serviceId);
      if (!service) return;
      this.selectedService = service;
      this.isDrawerOpen = true;

      const drawer = document.getElementById('service-drawer');
      const overlay = document.getElementById('service-drawer-overlay');
      if (drawer && overlay) {
        drawer.classList.remove('hidden');
        drawer.style.display = 'flex';
        overlay.classList.remove('hidden');
      }
      this.populateServiceDrawer(service);
    },

    closeDrawer() {
      this.isDrawerOpen = false;
      this.selectedService = null;

      const drawer = document.getElementById('service-drawer');
      const overlay = document.getElementById('service-drawer-overlay');
      if (drawer && overlay) {
        drawer.classList.add('hidden');
        drawer.style.display = 'none';
        overlay.classList.add('hidden');
      }
    },

    populateServiceDrawer(s) {
      if (!s) return;
      const avatar = document.getElementById('drawer-service-avatar');
      if (avatar) avatar.textContent = (s.display_name || s.id || 'S')[0].toUpperCase();

      const title = document.getElementById('drawer-service-title');
      if (title) title.textContent = s.display_name || s.id;

      const coreBadge = document.getElementById('drawer-service-core-badge');
      if (coreBadge) {
        if (s.is_core) coreBadge.classList.remove('hidden');
        else coreBadge.classList.add('hidden');
      }

      const subname = document.getElementById('drawer-service-subname');
      if (subname) subname.textContent = s.name || s.id;

      const cat = document.getElementById('drawer-service-category');
      if (cat) cat.textContent = s.category || 'General';

      const badge = document.getElementById('drawer-service-badge');
      if (badge) badge.innerHTML = this.getStatusBadgeHtml(s.status);

      const replicas = document.getElementById('drawer-service-replicas');
      if (replicas) replicas.textContent = (s.ready_replicas || 0) + ' / ' + (s.desired_replicas || 0);

      const uptime = document.getElementById('drawer-service-uptime');
      if (uptime) uptime.textContent = s.uptime || '—';

      const restarts = document.getElementById('drawer-service-restarts');
      if (restarts) restarts.textContent = s.restarts || 0;

      const lastRestart = document.getElementById('drawer-service-last-restart');
      if (lastRestart) lastRestart.textContent = s.last_restart || '—';

      const desc = document.getElementById('drawer-service-desc');
      if (desc) desc.textContent = s.description || 'Platform component.';

      const depName = document.getElementById('drawer-service-dep-name');
      if (depName) depName.textContent = s.name || '—';

      const ns = document.getElementById('drawer-service-namespace');
      if (ns) ns.textContent = s.namespace || 'lake-of-tears';

      const pod = document.getElementById('drawer-service-pod-name');
      if (pod) pod.textContent = s.pod_name || (s.desired_replicas === 0 ? 'None (scaled to 0)' : '—');

      const node = document.getElementById('drawer-service-node-name');
      if (node) node.textContent = s.node_name || '—';

      const img = document.getElementById('drawer-service-image');
      if (img) img.textContent = s.image || '—';

      const port = document.getElementById('drawer-service-port');
      if (port) port.textContent = s.port || '—';

      const cpu = document.getElementById('drawer-service-cpu-limit');
      if (cpu) cpu.textContent = s.cpu_limit || '—';

      const mem = document.getElementById('drawer-service-mem-limit');
      if (mem) mem.textContent = s.memory_limit || '—';

      const actions = document.getElementById('drawer-service-actions');
      if (actions) {
        actions.innerHTML = this.getDrawerActionsHtml(s);
      }
    },

    getStatusBadgeHtml(status) {
      if (status === 'running') {
        return `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[10px] font-medium bg-emerald-950/60 text-emerald-400 border border-emerald-800/60">
          <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
          Running
        </span>`;
      } else if (status === 'stopped') {
        return `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[10px] font-medium bg-gray-900 text-gray-400 border border-gray-700">
          <span class="w-1.5 h-1.5 rounded-full bg-gray-500"></span>
          Stopped
        </span>`;
      } else if (status === 'restarting') {
        return `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[10px] font-medium bg-amber-950/60 text-amber-400 border border-amber-800/60">
          <span class="w-1.5 h-1.5 rounded-full bg-amber-400 animate-ping"></span>
          Restarting
        </span>`;
      } else {
        return `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[10px] font-medium bg-red-950/60 text-red-400 border border-red-800/60">
          <span class="w-1.5 h-1.5 rounded-full bg-red-400"></span>
          ${status.charAt(0).toUpperCase() + status.slice(1)}
        </span>`;
      }
    },

    getDrawerActionsHtml(s) {
      if (s.status === 'stopped') {
        return `<button type="button" onclick="promptServiceAction('${s.id}', '${s.display_name}', 'start')"
          class="w-full text-xs px-3.5 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-medium transition-colors flex items-center justify-center gap-2 shadow-sm">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/></svg>
          Start Service
        </button>`;
      } else {
        const stopButtonHtml = s.is_core
          ? `<button type="button" disabled title="Core services cannot be stopped to prevent instance lock-out"
              class="text-xs px-3 py-2 rounded-lg border border-gray-800 text-gray-600 cursor-not-allowed flex items-center gap-1.5 opacity-60">
              <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636"/></svg>
              Stop
            </button>`
          : `<button type="button" onclick="promptServiceAction('${s.id}', '${s.display_name}', 'stop')"
              class="text-xs px-3.5 py-2 rounded-lg border border-red-900/60 text-red-400 hover:text-red-300 hover:border-red-700/80 hover:bg-red-950/20 transition-colors flex items-center gap-1.5">
              <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2m7-2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
              Stop
            </button>`;

        return `<button type="button" onclick="promptServiceAction('${s.id}', '${s.display_name}', 'restart')"
          class="flex-1 text-xs px-3.5 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-medium transition-colors flex items-center justify-center gap-1.5 shadow-sm">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
          Restart
        </button>
        ${stopButtonHtml}`;
      }
    },

    promptAction(serviceId, serviceName, action) {
      this.confirmModal.serviceId = serviceId;
      this.confirmModal.serviceName = serviceName || serviceId;
      this.confirmModal.action = action;
      this.confirmModal.isOpen = true;
      this.confirmModal.isSubmitting = false;

      const modal = document.getElementById('service-confirm-modal');
      const titleEl = document.getElementById('service-confirm-title');
      const msgEl = document.getElementById('service-confirm-msg');
      const targetEl = document.getElementById('service-confirm-target');
      const actionEl = document.getElementById('service-confirm-action');
      const submitBtn = document.getElementById('service-confirm-submit-btn');
      const label = document.getElementById('service-confirm-btn-label');
      const iconBox = document.getElementById('service-confirm-icon-box');
      const icon = document.getElementById('service-confirm-icon');

      if (targetEl) targetEl.textContent = serviceName;
      if (actionEl) actionEl.textContent = action.toUpperCase();

      if (action === 'restart') {
        if (titleEl) titleEl.textContent = 'Restart Service';
        if (msgEl) msgEl.textContent = `Perform a zero-downtime rolling restart of deployment '${serviceName}'. Existing pods will be terminated and replaced with fresh instances.`;
        if (submitBtn) submitBtn.className = 'text-xs px-4 py-2 rounded-md font-medium text-white transition-colors flex items-center gap-1.5 bg-blue-600 hover:bg-blue-500';
        if (label) label.textContent = 'Confirm Restart';
        if (iconBox) iconBox.className = 'w-10 h-10 rounded-full flex items-center justify-center shrink-0 bg-blue-950/60 border border-blue-800/60 text-blue-400';
        if (icon) icon.innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>';
      } else if (action === 'stop') {
        if (titleEl) titleEl.textContent = 'Stop Service (Scale to 0)';
        if (msgEl) msgEl.textContent = `Scale '${serviceName}' replicas to 0. Workloads dependent on this service will be temporarily unavailable until restarted.`;
        if (submitBtn) submitBtn.className = 'text-xs px-4 py-2 rounded-md font-medium text-white transition-colors flex items-center gap-1.5 bg-red-600 hover:bg-red-500';
        if (label) label.textContent = 'Confirm Stop';
        if (iconBox) iconBox.className = 'w-10 h-10 rounded-full flex items-center justify-center shrink-0 bg-red-950/60 border border-red-800/60 text-red-400';
        if (icon) icon.innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>';
      } else if (action === 'start') {
        if (titleEl) titleEl.textContent = 'Start Service (Scale to 1)';
        if (msgEl) msgEl.textContent = `Scale '${serviceName}' replicas from 0 to 1 to bring the workload back online.`;
        if (submitBtn) submitBtn.className = 'text-xs px-4 py-2 rounded-md font-medium text-white transition-colors flex items-center gap-1.5 bg-emerald-600 hover:bg-emerald-500';
        if (label) label.textContent = 'Confirm Start';
        if (iconBox) iconBox.className = 'w-10 h-10 rounded-full flex items-center justify-center shrink-0 bg-emerald-950/60 border border-emerald-800/60 text-emerald-400';
        if (icon) icon.innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/>';
      }

      if (modal) modal.classList.remove('hidden');
    },

    closeConfirmModal() {
      this.confirmModal.isOpen = false;
      this.confirmModal.serviceId = null;
      this.confirmModal.serviceName = null;
      this.confirmModal.action = null;
      this.confirmModal.isSubmitting = false;

      const modal = document.getElementById('service-confirm-modal');
      if (modal) modal.classList.add('hidden');
    },

    async executeAction() {
      if (!this.confirmModal.serviceId || !this.confirmModal.action || this.confirmModal.isSubmitting) return;

      const { serviceId, action, serviceName } = this.confirmModal;
      this.confirmModal.isSubmitting = true;

      const btn = document.getElementById('service-confirm-submit-btn');
      const spinner = document.getElementById('service-confirm-btn-spinner');
      const label = document.getElementById('service-confirm-btn-label');
      if (btn) btn.disabled = true;
      if (spinner) spinner.classList.remove('hidden');
      if (label) label.textContent = action === 'restart' ? 'Restarting...' : (action === 'stop' ? 'Stopping...' : 'Starting...');

      try {
        const res = await fetch(`/settings/admin/services/${serviceId}/${action}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' }
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          throw new Error(data.detail || `Failed to ${action} service (${res.status})`);
        }
        this.showToast('success', `✓ Successfully triggered ${action} for ${serviceName}`);
        this.closeConfirmModal();
        await this.refresh(true);
      } catch (err) {
        this.showToast('error', `✕ Action failed: ${err.message}`);
      } finally {
        if (btn) btn.disabled = false;
        if (spinner) spinner.classList.add('hidden');
        this.confirmModal.isSubmitting = false;
      }
    },

    showToast(type, msg) {
      const toast = document.getElementById('service-action-toast');
      if (!toast) return;
      toast.className = type === 'success'
        ? 'px-4 py-3 rounded-md text-sm transition-all duration-300 bg-emerald-950/80 border border-emerald-800/80 text-emerald-300 shadow-lg'
        : 'px-4 py-3 rounded-md text-sm transition-all duration-300 bg-red-950/80 border border-red-800/80 text-red-300 shadow-lg';
      toast.textContent = msg;
      toast.classList.remove('hidden');
      setTimeout(() => {
        toast.classList.add('hidden');
      }, 5000);
    }
  }));

  Alpine.data('aiModelDiscoveryManager', () => ({
    isDiscovering: false,

    init() {
      window.discoverAiModels = (chosen, targetField) => this.discover(chosen, targetField);
      window.dismissDeprecationWarning = () => this.dismissDeprecation();
    },

    async discover(chosen, targetField) {
      chosen = chosen || document.getElementById('ai-provider-select')?.value;
      const statusBox = document.getElementById('ai-test-status');
      const config = {};
      const prefix = 'cfg__' + chosen + '__';
      document.querySelectorAll('.ai-provider-fields[data-provider="' + chosen + '"] [name^="' + prefix + '"]').forEach(function (el) {
        config[el.name.substring(prefix.length)] = el.value;
      });

      const paint = function (kind, text) {
        if (!statusBox) return;
        statusBox.classList.remove('hidden', 'bg-emerald-900/20', 'border-emerald-800/50', 'text-emerald-300', 'bg-red-900/20', 'border-red-800/50', 'text-red-300', 'bg-blue-900/20', 'border-blue-800/50', 'text-blue-300');
        if (kind === 'ok') {
          statusBox.classList.add('bg-emerald-900/20', 'border-emerald-800/50', 'text-emerald-300');
        } else if (kind === 'err') {
          statusBox.classList.add('bg-red-900/20', 'border-red-800/50', 'text-red-300');
        } else {
          statusBox.classList.add('bg-blue-900/20', 'border-blue-800/50', 'text-blue-300');
        }
        statusBox.textContent = text;
      };

      paint('info', 'Discovering available models from ' + chosen + '...');
      this.isDiscovering = true;

      try {
        const resp = await fetch('/settings/admin/ai-provider/discover-models', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ provider: chosen, config: config }),
        });
        const body = await resp.json().catch(function () { return { ok: false, message: 'Invalid response from server' }; });
        if (resp.ok && body.ok) {
          const models = body.models || {};
          const chatModels = models.chat || [];
          const embModels = models.embedding || [];

          if (!window._curatedModels) window._curatedModels = {};
          if (!window._curatedModels[chosen]) window._curatedModels[chosen] = {};

          // Update chat models
          const existingChat = new Set((window._curatedModels[chosen].chat_model || []).map(m => m.id));
          const mergedChat = [...(window._curatedModels[chosen].chat_model || [])];
          chatModels.forEach(m => {
            if (!existingChat.has(m)) {
              mergedChat.push({ id: m, tag: 'API Verified' });
            }
          });
          window._curatedModels[chosen].chat_model = mergedChat;

          // Update embedding models
          const existingEmb = new Set((window._curatedModels[chosen].embedding_model || []).map(m => m.id));
          const mergedEmb = [...(window._curatedModels[chosen].embedding_model || [])];
          embModels.forEach(m => {
            if (!existingEmb.has(m)) {
              mergedEmb.push({ id: m, tag: 'API Verified' });
            }
          });
          window._curatedModels[chosen].embedding_model = mergedEmb;

          // Re-render and open dropdown for target field or chat_model
          const openField = targetField || 'chat_model';
          if (typeof window.openCustomModelDropdown === 'function') {
            window.openCustomModelDropdown(chosen, openField);
          }

          paint('ok', 'Discovered ' + chatModels.length + ' chat models and ' + embModels.length + ' embedding models from ' + chosen + '. Suggestions menu opened.');
        } else {
          paint('err', 'Discovery failed: ' + (body.message || body.detail || 'Could not list models. Check credentials and permissions.'));
        }
      } catch (e) {
        paint('err', 'Network error while discovering models: ' + e);
      } finally {
        this.isDiscovering = false;
      }
    },

    async dismissDeprecation() {
      const alertEl = document.getElementById('ai-deprecation-alert');
      try {
        const resp = await fetch('/settings/admin/ai-provider/dismiss-deprecation-warning', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
        });
        if (resp.ok && alertEl) {
          alertEl.remove();
        }
      } catch (e) {
        if (alertEl) alertEl.remove();
      }
    }
  }));
});
