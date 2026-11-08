/**
 * Lake of Tears - Catalog Explorer Controller (Alpine.js)
 * Declarative state management for:
 * 1. Trash Slide-Over Drawer & Dynamic Badge (soft-delete inventory, TTL countdown, 1-click UNDROP, conflict resolution, permanent purge)
 * 2. Table Details Slide-Over Drawer (partition/columns viewer, sample data, vector index configuration & live polling, AI enrichment)
 * 3. Creation Wizards & Modals (Create Schema with AI, File Upload wizard with column grid, AI Prompt-to-table, Export TOON, CASCADE Delete Schema, Delete Table)
 * 4. Inline Description Editing & Drag-and-Drop Reordering (per-user workspace preference persistence)
 */

document.addEventListener('alpine:init', () => {

  // ── 1. Trash Slide-Over Drawer Manager ──────────────────────────────────────
  Alpine.data('trashDrawerManager', () => ({
    isOpen: false,
    items: [],
    isLoading: false,
    error: null,
    trashCount: 0,
    purgeModalOpen: false,
    conflictModalOpen: false,
    pendingPurgeTarget: { assetId: '', name: '', isSubmitting: false },
    conflictTarget: { assetId: '', name: '', errorMsg: '', newName: '', isSubmitting: false },

    init() {
      // Expose globally for legacy bridges and inline calls
      window.openTrashDrawer = () => this.open();
      window.closeTrashDrawer = () => this.close();
      window.initTrashBadge = () => this.initBadge();
      window.restoreTrashAsset = (assetId, name, payload) => this.restoreTrashAsset(assetId, name, payload);
      window.purgeTrashAsset = (assetId, name) => this.openPurgeModal(assetId, name);
      window.closePurgeTrashModal = () => this.closePurgeModal();
      window.executePurgeTrashAsset = () => this.executePurgeTrashAsset();
      window.openRestoreConflictModal = (assetId, name, msg) => this.openConflictModal(assetId, name, msg);
      window.closeRestoreConflictModal = () => this.closeConflictModal();
      window.submitRestoreConflictResolution = (strategy) => this.submitConflictResolution(strategy);

      this.initBadge();

      // Escape key listener for trash drawer
      window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          if (this.conflictModalOpen) {
            this.closeConflictModal();
          } else if (this.purgeModalOpen) {
            this.closePurgeModal();
          } else if (this.isOpen) {
            this.close();
          }
        }
      });
    },

    open() {
      if (window.closePanel) window.closePanel();
      this.isOpen = true;
      const drawer = document.getElementById('trash-drawer');
      const overlay = document.getElementById('trash-overlay');
      if (drawer) {
        drawer.classList.remove('hidden');
        drawer.style.display = 'flex';
      }
      if (overlay) overlay.classList.remove('hidden');
      this.loadTrashItems();
    },

    close() {
      this.isOpen = false;
      const drawer = document.getElementById('trash-drawer');
      const overlay = document.getElementById('trash-overlay');
      if (drawer) {
        drawer.classList.add('hidden');
        drawer.style.display = 'none';
      }
      if (overlay) overlay.classList.add('hidden');
    },

    async initBadge() {
      const btn = document.getElementById('trash-drawer-btn');
      const badge = document.getElementById('trash-count-badge');
      try {
        const resp = await fetch('/api/catalog/trash', { credentials: 'include' });
        if (resp.ok) {
          const items = await resp.json();
          this.trashCount = Array.isArray(items) ? items.length : 0;
          if (this.trashCount > 0) {
            if (btn) {
              btn.classList.remove('hidden');
              btn.classList.add('flex');
            }
            if (badge) {
              badge.textContent = this.trashCount;
              badge.classList.remove('hidden');
            }
          } else {
            if (btn) {
              btn.classList.add('hidden');
              btn.classList.remove('flex');
            }
            if (badge) badge.classList.add('hidden');
          }
        }
      } catch (err) {
        console.debug('Failed to fetch trash badge count', err);
      }
    },

    async loadTrashItems() {
      this.isLoading = true;
      this.error = null;
      const container = document.getElementById('trash-items-container');
      if (container) {
        container.innerHTML = '<div class="py-12 text-center text-gray-500 text-xs">Loading deleted assets...</div>';
      }

      try {
        const resp = await fetch('/api/catalog/trash', { credentials: 'include' });
        if (!resp.ok) {
          throw new Error('Failed to load trash assets');
        }
        const items = await resp.json();
        this.items = Array.isArray(items) ? items : [];
        this.trashCount = this.items.length;
        this.initBadge();

        if (!container) return;

        if (this.items.length === 0) {
          container.innerHTML = `
            <div class="py-16 text-center space-y-2">
              <div class="w-10 h-10 rounded-full bg-white/[0.04] text-gray-600 flex items-center justify-center mx-auto">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
                </svg>
              </div>
              <p class="text-sm font-medium text-gray-300">Trash is empty</p>
              <p class="text-xs text-gray-500">Deleted tables and schemas will be held here for 7 days before permanent purge.</p>
            </div>`;
          return;
        }

        container.innerHTML = this.items.map(item => {
          const isTable = item.asset_type === 'table';
          const qualified = item.schema_name
            ? `${item.catalog_name || 'main'}.${item.schema_name}.${item.name}`
            : `${item.catalog_name || 'main'}.${item.name}`;
          const rem = item.days_remaining !== null ? `${item.days_remaining}d remaining` : '7d TTL';
          const safeName = (item.name || '').replace(/'/g, "\\'");

          return `
            <div class="p-3.5 rounded-lg border border-white/[0.06] bg-[#1a1a1e] space-y-2.5 group hover:border-white/[0.12] transition-colors">
              <div class="flex items-start justify-between gap-2">
                <div class="min-w-0">
                  <div class="flex items-center gap-1.5 flex-wrap">
                    <span class="text-[10px] font-mono px-1.5 py-0.5 rounded ${isTable ? 'bg-blue-950/60 text-blue-400 border border-blue-800/40' : 'bg-purple-950/60 text-purple-400 border border-purple-800/40'} font-semibold uppercase">
                      ${item.asset_type}
                    </span>
                    <span class="font-mono text-xs font-semibold text-gray-200 truncate" title="${qualified}">${qualified}</span>
                    ${item.batch_id ? '<span class="text-[9px] px-1 py-0.2 rounded bg-gray-800 text-gray-400 font-mono" title="Cascade batch drop">cascade</span>' : ''}
                  </div>
                  <div class="text-[11px] text-gray-500 mt-1 flex items-center gap-2">
                    <span title="Deleted timestamp">${item.deleted_at ? new Date(item.deleted_at).toLocaleString() : ''}</span>
                    <span>&bull;</span>
                    <span class="text-amber-400/90 font-mono font-medium">${rem}</span>
                  </div>
                </div>
              </div>
              <div class="flex items-center justify-between pt-1 border-t border-white/[0.04] text-[11px] text-gray-500">
                <span class="truncate max-w-[200px]">${item.dropped_by_email ? 'by ' + item.dropped_by_email : ''}</span>
                <div class="flex items-center gap-1.5">
                  <button type="button" onclick="purgeTrashAsset('${item.id}', '${safeName}')"
                          class="px-2 py-1 text-xs text-gray-400 hover:text-red-400 hover:bg-red-500/10 rounded transition-colors"
                          title="Permanently purge now">
                    Purge
                  </button>
                  <button type="button" onclick="restoreTrashAsset('${item.id}', '${safeName}')"
                          class="px-2.5 py-1 text-xs bg-emerald-600/30 hover:bg-emerald-600 text-emerald-300 hover:text-white border border-emerald-500/40 rounded transition-all font-medium flex items-center gap-1"
                          title="Restore to active catalog">
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
                    Restore
                  </button>
                </div>
              </div>
            </div>`;
        }).join('');
      } catch (err) {
        this.error = err.message;
        if (container) {
          container.innerHTML = `<div class="py-12 text-center text-red-400 text-xs">Error: ${err.message}</div>`;
        }
      } finally {
        this.isLoading = false;
      }
    },

    async restoreTrashAsset(assetId, name, resolutionPayload = null) {
      try {
        const opts = {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' }
        };
        if (resolutionPayload) {
          opts.body = JSON.stringify(resolutionPayload);
        }
        const resp = await fetch(`/api/catalog/trash/${assetId}/restore`, opts);
        const res = await resp.json();
        if (!resp.ok || !res.ok) {
          if (resp.status === 409) {
            const errorMsg = res.error || res.detail || (typeof res === 'string' ? res : 'An active table with this name already exists.');
            this.openConflictModal(assetId, name, errorMsg);
            return;
          }
          if (window.showCatalogToast) window.showCatalogToast(res.error || res.detail || 'Failed to restore asset', 'error');
          return;
        }
        this.closeConflictModal();
        if (window.showCatalogToast) window.showCatalogToast(res.message || `Restored ${name}`, 'success');
        this.loadTrashItems();
        setTimeout(() => window.location.reload(), 1200);
      } catch (err) {
        if (window.showCatalogToast) window.showCatalogToast(`Restore error: ${err.message}`, 'error');
      }
    },

    openConflictModal(assetId, name, errorMsg) {
      this.conflictTarget = {
        assetId,
        name,
        errorMsg: errorMsg || 'An active table with this name already exists.',
        newName: `${name} (restored)`,
        isSubmitting: false
      };
      this.conflictModalOpen = true;
      const modal = document.getElementById('restore-conflict-modal');
      const errorEl = document.getElementById('conflict-error-msg');
      const inputEl = document.getElementById('conflict-new-name-input');
      if (errorEl) errorEl.textContent = this.conflictTarget.errorMsg;
      if (inputEl) inputEl.value = this.conflictTarget.newName;
      if (modal) modal.classList.remove('hidden');
    },

    closeConflictModal() {
      this.conflictModalOpen = false;
      const modal = document.getElementById('restore-conflict-modal');
      if (modal) modal.classList.add('hidden');
    },

    submitConflictResolution(strategy) {
      if (!this.conflictTarget || !this.conflictTarget.assetId) return;
      const { assetId, name } = this.conflictTarget;
      let payload = { strategy: strategy };
      if (strategy === 'rename') {
        const inputEl = document.getElementById('conflict-new-name-input');
        const newName = inputEl ? inputEl.value.trim() : '';
        payload.new_name = newName || `${name} (restored)`;
      }
      this.restoreTrashAsset(assetId, name, payload);
    },

    openPurgeModal(assetId, name) {
      this.pendingPurgeTarget = { assetId, name, isSubmitting: false };
      this.purgeModalOpen = true;
      const modal = document.getElementById('purge-trash-modal');
      const nameEl = document.getElementById('purge-modal-asset-name');
      if (nameEl) nameEl.textContent = name;
      if (modal) modal.classList.remove('hidden');
    },

    closePurgeModal() {
      this.purgeModalOpen = false;
      const modal = document.getElementById('purge-trash-modal');
      if (modal) modal.classList.add('hidden');
      this.pendingPurgeTarget = { assetId: '', name: '', isSubmitting: false };
    },

    async executePurgeTrashAsset() {
      if (!this.pendingPurgeTarget.assetId) return;
      this.pendingPurgeTarget.isSubmitting = true;
      const btn = document.getElementById('purge-modal-confirm-btn');
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<svg class="w-3.5 h-3.5 animate-spin inline mr-1" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg> Purging...`;
      }
      const { assetId, name } = this.pendingPurgeTarget;
      try {
        const resp = await fetch(`/api/catalog/trash/${assetId}/purge`, {
          method: 'POST',
          credentials: 'include'
        });
        const res = await resp.json();
        this.closePurgeModal();
        if (!resp.ok || !res.ok) {
          if (window.showCatalogToast) window.showCatalogToast(res.error || res.detail || 'Failed to purge asset', 'error');
          return;
        }
        if (window.showCatalogToast) window.showCatalogToast(`Permanently deleted ${name}`, 'info');
        this.loadTrashItems();
      } catch (err) {
        this.closePurgeModal();
        if (window.showCatalogToast) window.showCatalogToast(`Purge error: ${err.message}`, 'error');
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = `<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg><span>Purge Permanently</span>`;
        }
      }
    }
  }));

  // ── 2. Table Details Slide-Over Drawer Manager ─────────────────────────────
  Alpine.data('tableDetailsDrawerManager', () => ({
    isOpen: false,
    isMaximized: false,
    activeTab: 'columns',
    currentCatIdent: '',
    currentSchIdent: '',
    currentTableIdent: '',
    currentCatId: '',
    currentSchId: '',
    currentTableId: '',
    currentFullNamespace: '',
    currentTableData: null,
    sampleDataCache: {},
    vectorPollingTimer: null,
    enrichInFlight: new Set(),

    init() {
      window._alpineTableDetailsDrawer = this;
      window.showTable = (catIdent, schIdent, tableIdent, table) => this.showTable(catIdent, schIdent, tableIdent, table);
      window.openTablePanel = (el) => this.openFromElement(el);
      window.closePanel = () => this.close();
      window.toggleMaximizePanel = () => this.toggleMaximize();
      window.switchDrawerTab = (tab) => this.switchTab(tab);
      window.copyFullNamespace = (btn) => this.copyFullNamespace(btn);
      window.copySnippet = (text, btn) => this.copySnippet(text, btn);
      window.triggerVectorSync = (btn, force = false) => this.triggerVectorSync(btn, force);
      window.saveVectorConfig = () => this.saveVectorConfig();
      window.enrichCurrentTableAi = () => this.enrichCurrentTableAi();
      window.openCurrentTableToon = () => this.openCurrentTableToon();

      window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && this.isOpen) {
          this.close();
        }
      });
    },

    openFromElement(el) {
      if (!el) return;
      const catIdent = el.getAttribute('data-cat') || 'default';
      const schIdent = el.getAttribute('data-sch') || 'default';
      const tableIdent = el.getAttribute('data-table-ident') || '';
      this.currentCatId = el.getAttribute('data-cat-id') || '';
      this.currentSchId = el.getAttribute('data-sch-id') || '';
      this.currentTableId = el.getAttribute('data-table-id') || '';
      let table = {};
      try {
        table = JSON.parse(el.getAttribute('data-table') || '{}');
      } catch (err) {
        console.error('Failed to parse table data:', err);
      }
      this.showTable(catIdent, schIdent, tableIdent, table);
    },

    showTable(catIdent, schIdent, tableIdent, table) {
      if (window.closeTrashDrawer) window.closeTrashDrawer();
      this.isOpen = true;
      const panel = document.getElementById('table-panel');
      const overlay = document.getElementById('panel-overlay');
      if (panel) {
        panel.classList.remove('hidden');
        panel.style.display = 'flex';
      }
      if (overlay) overlay.classList.remove('hidden');

      this.currentTableData = table;
      this.currentCatIdent = catIdent;
      this.currentSchIdent = schIdent;
      this.currentTableIdent = tableIdent;

      // Sync window globals for legacy script compatibility
      window.currentTableData = table;
      window.currentCatId = this.currentCatId;
      window.currentSchId = this.currentSchId;
      window.currentTableId = this.currentTableId;
      window.currentCatIdent = catIdent;
      window.currentSchIdent = schIdent;
      window.currentTableIdent = tableIdent;

      const displayName = table.name || tableIdent;
      const titleEl = document.getElementById('panel-title');
      if (titleEl) titleEl.textContent = displayName;

      const fullNamespace = `${catIdent}.${schIdent}.${tableIdent}`;
      this.currentFullNamespace = fullNamespace;
      const nsEl = document.getElementById('panel-namespace');
      if (nsEl) nsEl.textContent = fullNamespace;

      // Drift badge
      const driftBadge = document.getElementById('panel-drift-badge');
      if (driftBadge) {
        driftBadge.classList.toggle('hidden', !table.schema_drift);
      }

      // Top action links
      const viewName = `${schIdent}.${tableIdent}`;
      const sqlPath = table.s3_path_pattern || '';
      const querySQL = encodeURIComponent(`SELECT * FROM ${viewName} LIMIT 100`);
      const parquetSQL = sqlPath ? encodeURIComponent(`SELECT * FROM read_parquet('${sqlPath}') LIMIT 100`) : '';

      const queryBtn = document.getElementById('panel-query-btn');
      if (queryBtn) queryBtn.href = `/sql?q=${querySQL}`;

      const parquetBtn = document.getElementById('panel-parquet-btn');
      if (parquetBtn) {
        if (parquetSQL) {
          parquetBtn.href = `/sql?q=${parquetSQL}`;
          parquetBtn.classList.remove('hidden');
        } else {
          parquetBtn.classList.add('hidden');
        }
      }

      const aiBtn = document.getElementById('panel-ai-btn');
      const hasAi = window.HAS_AI_CONFIGURED !== undefined ? window.HAS_AI_CONFIGURED : true;
      if (aiBtn && !hasAi) {
        aiBtn.disabled = true;
        aiBtn.title = 'No AI provider configured in Admin Panel (Settings → Admin Panel)';
        aiBtn.classList.add('opacity-50', 'cursor-not-allowed', 'pointer-events-none');
      }

      this.syncEnrichButton();

      // Render tab contents
      this.renderColumnsTab(table);
      this.renderDetailsTab(table, catIdent, schIdent, tableIdent);
      this.renderVectorTab(table);

      this.switchTab(this.activeTab || 'columns');
    },

    close() {
      this.stopVectorPolling();
      this.isOpen = false;
      const panel = document.getElementById('table-panel');
      const overlay = document.getElementById('panel-overlay');
      if (panel) {
        panel.classList.add('hidden');
        panel.style.display = 'none';
      }
      if (overlay) overlay.classList.add('hidden');
    },

    toggleMaximize() {
      this.isMaximized = !this.isMaximized;
      const panel = document.getElementById('table-panel');
      if (!panel) return;
      if (this.isMaximized) {
        panel.classList.remove('w-[600px]');
        panel.classList.add('w-[92vw]');
      } else {
        panel.classList.remove('w-[92vw]');
        panel.classList.add('w-[600px]');
      }
    },

    switchTab(tab) {
      this.activeTab = tab;
      ['columns', 'sample', 'details', 'vector'].forEach(t => {
        const btn = document.getElementById('tab-btn-' + t);
        const content = document.getElementById('drawer-tab-' + t);
        if (!btn || !content) return;
        if (t === tab) {
          btn.className = 'px-3.5 py-2 text-xs font-semibold border-b-2 border-blue-500 text-blue-400 transition-colors';
          content.classList.remove('hidden');
        } else {
          btn.className = 'px-3.5 py-2 text-xs font-semibold border-b-2 border-transparent text-gray-400 hover:text-gray-200 transition-colors';
          content.classList.add('hidden');
        }
      });

      if (tab === 'sample') {
        this.loadSampleData();
      }
    },

    copyFullNamespace(btn) {
      if (!this.currentFullNamespace) return;
      navigator.clipboard.writeText(this.currentFullNamespace).then(() => {
        const origHtml = btn.innerHTML;
        btn.innerHTML = `<svg class="w-3.5 h-3.5 text-emerald-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>`;
        setTimeout(() => { btn.innerHTML = origHtml; }, 1500);
      });
    },

    copySnippet(text, btn) {
      if (!text) return;
      navigator.clipboard.writeText(text).then(() => {
        const origText = btn.textContent;
        btn.textContent = 'Copied!';
        btn.classList.add('text-emerald-400');
        setTimeout(() => {
          btn.textContent = origText;
          btn.classList.remove('text-emerald-400');
        }, 1500);
      });
    },

    renderColumnsTab(t) {
      const container = document.getElementById('drawer-tab-columns');
      if (!container) return;
      const cols = (t.column_defs || []).map(c => `
        <tr class="${c.deprecated ? 'opacity-40' : ''} hover:bg-white/[0.02] transition-colors">
          <td class="py-2 px-3 font-mono text-xs text-gray-200">${c.name || ''}${c.deprecated ? ' <span class="text-gray-600 text-[10px]">(deprecated)</span>' : ''}</td>
          <td class="py-2 px-3 font-mono text-xs text-blue-400">${c.type || 'VARCHAR'}</td>
          <td class="py-2 px-3 text-xs text-gray-400">${c.description || '<span class="text-gray-600 italic">—</span>'}</td>
        </tr>`).join('');

      const noSchema = (!t.column_defs || !t.column_defs.length)
        ? `<div class="text-center py-8 text-gray-500 text-xs italic">Schema not yet registered — columns will auto-populate upon first ingest.</div>` : '';

      container.innerHTML = `
        ${t.description ? `<p class="text-xs text-gray-400 leading-relaxed">${t.description}</p>` : ''}
        ${noSchema}
        ${cols ? `
          <div class="border border-white/[0.08] rounded-lg overflow-hidden">
            <table class="w-full text-left text-xs">
              <thead class="bg-[#202020] border-b border-white/[0.08] text-gray-400 text-[11px]">
                <tr>
                  <th class="py-2 px-3 font-medium">Column</th>
                  <th class="py-2 px-3 font-medium">Type</th>
                  <th class="py-2 px-3 font-medium">Description</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-white/[0.04]">
                ${cols}
              </tbody>
            </table>
          </div>
        ` : ''}
      `;
    },

    loadSampleData() {
      const container = document.getElementById('drawer-tab-sample');
      if (!container) return;
      const viewName = `${this.currentSchIdent}.${this.currentTableIdent}`;
      if (this.sampleDataCache[viewName]) {
        this.renderSampleDataGrid(this.sampleDataCache[viewName]);
        return;
      }

      container.innerHTML = `
        <div class="flex flex-col items-center justify-center py-12 text-gray-500 space-y-3">
          <svg class="animate-spin h-6 w-6 text-blue-500" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path>
          </svg>
          <span class="text-xs">Fetching sample data from DuckDB...</span>
        </div>
      `;

      fetch(`/catalog/preview?view=${encodeURIComponent(viewName)}&limit=15`)
        .then(r => r.json())
        .then(data => {
          this.sampleDataCache[viewName] = data;
          this.renderSampleDataGrid(data);
        })
        .catch(err => {
          container.innerHTML = `
            <div class="p-4 rounded-lg bg-red-950/20 border border-red-800/40 text-red-400 text-xs">
              Failed to fetch preview: ${err}
            </div>
          `;
        });
    },

    renderSampleDataGrid(data) {
      const container = document.getElementById('drawer-tab-sample');
      if (!container) return;
      if (!data.ok || !data.rows || data.rows.length === 0) {
        container.innerHTML = `
          <div class="text-center py-10 px-4 rounded-lg border border-dashed border-white/[0.08]">
            <svg class="w-8 h-8 text-gray-600 mx-auto mb-2" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" d="M3 10h18M3 14h18m-9-4v8m-7 4h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"/>
            </svg>
            <p class="text-xs text-gray-300 font-medium">No sample data available</p>
            <p class="text-[11px] text-gray-500 mt-1 max-w-sm mx-auto">
              ${data.error || "Data has not been ingested yet or the underlying Parquet path is empty."}
            </p>
          </div>
        `;
        return;
      }

      const columns = data.columns || [];
      const rows = data.rows || [];

      container.innerHTML = `
        <div class="flex items-center justify-between text-xs text-gray-400 mb-2">
          <span>Showing first ${rows.length} rows</span>
          ${data.execution_ms ? `<span class="tabular-nums text-gray-500">${data.execution_ms} ms</span>` : ''}
        </div>
        <div class="border border-white/[0.08] rounded-lg overflow-x-auto max-h-[60vh]">
          <table class="w-full text-left text-xs border-collapse">
            <thead class="sticky top-0 bg-[#202020] border-b border-white/[0.08] text-gray-400 text-[11px]">
              <tr>
                <th class="py-2 px-3 font-mono font-medium text-gray-500 border-r border-white/[0.06] w-10 text-center">#</th>
                ${columns.map(col => `<th class="py-2 px-3 font-mono whitespace-nowrap font-medium text-gray-300 border-r border-white/[0.06] last:border-0">${col}</th>`).join('')}
              </tr>
            </thead>
            <tbody class="divide-y divide-white/[0.04] font-mono text-[11px]">
              ${rows.map((row, idx) => `
                <tr class="hover:bg-white/[0.02] transition-colors">
                  <td class="py-1.5 px-3 text-gray-600 border-r border-white/[0.06] text-center select-none">${idx + 1}</td>
                  ${row.map(cell => `<td class="py-1.5 px-3 text-gray-300 whitespace-nowrap max-w-[240px] truncate border-r border-white/[0.06] last:border-0" title="${cell !== null ? String(cell) : 'NULL'}">${cell !== null ? String(cell) : '<span class="text-gray-600 italic">NULL</span>'}</td>`).join('')}
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      `;
    },

    renderDetailsTab(t, catIdent, schIdent, tableIdent) {
      const container = document.getElementById('drawer-tab-details');
      if (!container) return;
      const viewName = `${schIdent}.${tableIdent}`;
      const sqlPath = t.s3_path_pattern || '';

      container.innerHTML = `
        <div class="space-y-4">
          ${t.description ? `
          <div>
            <p class="text-[10px] text-gray-500 uppercase tracking-wider font-semibold mb-1">Description</p>
            <p class="text-xs text-gray-300 leading-relaxed">${t.description}</p>
          </div>` : ''}

          <div class="grid grid-cols-2 gap-3 pt-1">
            <div class="bg-black/30 border border-white/[0.06] rounded-md p-3">
              <p class="text-[10px] text-gray-500 uppercase tracking-wider font-semibold mb-1">Medallion Tier</p>
              <span class="text-xs font-medium text-gray-200 capitalize">${schIdent}</span>
            </div>
            <div class="bg-black/30 border border-white/[0.06] rounded-md p-3">
              <p class="text-[10px] text-gray-500 uppercase tracking-wider font-semibold mb-1">Storage Format</p>
              <span class="text-xs font-mono text-gray-200">DELTA / PARQUET</span>
            </div>
          </div>

          <div>
            <p class="text-[10px] text-gray-500 uppercase tracking-wider font-semibold mb-1.5">DuckDB View Identifier</p>
            <div class="flex items-center gap-2 bg-black/30 border border-white/[0.06] rounded-md p-2">
              <code class="text-xs font-mono text-blue-400 flex-1 truncate">${viewName}</code>
              <button type="button" onclick="copySnippet('${viewName}', this)" class="text-gray-400 hover:text-white text-xs px-2 py-0.5 bg-white/[0.05] rounded shrink-0">
                Copy
              </button>
            </div>
          </div>

          ${sqlPath ? `
          <div>
            <p class="text-[10px] text-gray-500 uppercase tracking-wider font-semibold mb-1.5">S3 Storage URI</p>
            <div class="flex items-center gap-2 bg-black/30 border border-white/[0.06] rounded-md p-2">
              <code class="text-xs font-mono text-gray-300 flex-1 break-all">${sqlPath}</code>
              <button type="button" onclick="copySnippet('${sqlPath}', this)" class="text-gray-400 hover:text-white text-xs px-2 py-0.5 bg-white/[0.05] rounded shrink-0">
                Copy
              </button>
            </div>
          </div>` : ''}

          <div class="pt-2 border-t border-white/[0.06] flex items-center justify-between text-xs text-gray-500">
            <span>Created</span>
            <span>${t.created_at ? new Date(t.created_at).toLocaleDateString() : '—'}</span>
          </div>
        </div>
      `;
    },

    stopVectorPolling() {
      if (this.vectorPollingTimer) {
        clearInterval(this.vectorPollingTimer);
        this.vectorPollingTimer = null;
      }
    },

    startVectorPolling(catId, schId, tblId) {
      this.stopVectorPolling();
      if (!catId || !schId || !tblId) return;
      this.vectorPollingTimer = setInterval(() => {
        if (!this.isOpen || this.currentTableId !== tblId) {
          this.stopVectorPolling();
          return;
        }

        fetch(`/catalog/table-status?catalog_id=${encodeURIComponent(catId)}&schema_id=${encodeURIComponent(schId)}&table_id=${encodeURIComponent(tblId)}`)
          .then(r => r.json())
          .then(res => {
            if (res.ok && res.table && this.currentTableId === tblId) {
              this.currentTableData = res.table;
              const el = document.querySelector(`[data-table-id="${tblId}"]`);
              if (el) {
                el.setAttribute('data-table', JSON.stringify(res.table));
              }
              this.renderVectorTab(this.currentTableData);
              const curStatus = (this.currentTableData.vector_index || {}).status;
              if (curStatus !== 'syncing') {
                this.stopVectorPolling();
              }
            }
          })
          .catch(err => {
            console.warn('Vector polling error:', err);
          });
      }, 3000);
    },

    renderVectorTab(t) {
      const container = document.getElementById('drawer-tab-vector');
      if (!container) return;
      const v = t.vector_index || {};
      const status = v.status || 'not_configured';
      const totalVectors = v.total_vectors !== undefined ? v.total_vectors : 0;
      const sourceCol = v.source_column || 'overview';
      const model = v.model || 'gemini-embedding-001';
      const dim = v.dimension || 768;
      const lastSynced = v.last_synced_at ? new Date(v.last_synced_at).toLocaleString() : 'Never';
      const formatTpl = v.format_template || '';
      const chunking = v.chunking || 'recursive';
      const chunkSize = v.chunk_size !== undefined ? v.chunk_size : 512;
      const chunkOverlap = v.chunk_overlap !== undefined ? v.chunk_overlap : 64;

      const cols = (t.column_defs || []).map(c => c.name).filter(Boolean);

      let statusBadge = `<span class="px-2 py-0.5 rounded text-xs bg-gray-800 text-gray-400 border border-gray-700">Not Configured</span>`;
      if (status === 'synced') {
        statusBadge = `<span class="px-2 py-0.5 rounded text-xs bg-emerald-950/60 text-emerald-400 border border-emerald-800/60 flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>Synced</span>`;
      } else if (status === 'syncing') {
        statusBadge = `<span class="px-2 py-0.5 rounded text-xs bg-amber-950/60 text-amber-400 border border-amber-800/60 flex items-center gap-1.5"><svg class="animate-spin h-3 w-3 text-amber-400" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>Syncing</span>`;
      } else if (status === 'error') {
        statusBadge = `<span class="px-2 py-0.5 rounded text-xs bg-red-950/60 text-red-400 border border-red-800/60 flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-red-400"></span>Error</span>`;
      }

      const processedRows = v.processed_rows !== undefined ? v.processed_rows : 0;
      const totalRows = v.total_rows !== undefined && v.total_rows !== null ? v.total_rows : null;
      const percent = v.percent !== undefined ? v.percent : 0;

      container.innerHTML = `
        <div class="space-y-4">
          <!-- Status Card -->
          <div class="bg-black/30 border border-white/[0.08] rounded-xl p-4 space-y-3">
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="text-xs font-semibold text-gray-200">Vector Search Index</span>
                ${statusBadge}
              </div>
              <button type="button" id="btn-vector-sync" onclick="triggerVectorSync(this)" ${status === 'syncing' ? 'disabled' : ''}
                      class="btn-primary text-xs py-1.5 px-3 flex items-center gap-1.5 ${status === 'syncing' ? 'opacity-50 cursor-not-allowed pointer-events-none' : ''}">
                ${status === 'syncing' ? `
                  <svg class="animate-spin h-3.5 w-3.5 text-white inline" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>
                  Syncing...
                ` : `
                  <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                  Index Now
                `}
              </button>
            </div>

            ${status === 'syncing' ? `
            <div class="p-3 rounded-lg bg-blue-950/30 border border-blue-800/40 space-y-2">
              <div class="flex items-center justify-between text-xs">
                <span class="text-blue-300 font-medium flex items-center gap-1.5">
                  <svg class="animate-spin h-3.5 w-3.5 text-blue-400" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>
                  Indexing in background...
                </span>
                <span class="text-blue-300 font-mono text-xs font-semibold">
                  ${processedRows.toLocaleString()} / ${totalRows !== null ? totalRows.toLocaleString() : '?'} rows (${percent}%)
                </span>
              </div>
              <div class="w-full bg-gray-800/80 rounded-full h-2 overflow-hidden border border-white/[0.05]">
                <div class="bg-gradient-to-r from-blue-500 to-indigo-500 h-2 rounded-full transition-all duration-300 ease-out" style="width: ${percent}%"></div>
              </div>
            </div>
            ` : ''}

            ${v.last_error ? `
            <div class="p-2.5 rounded bg-red-950/30 border border-red-800/40 text-red-300 text-xs">
              <strong>Sync Error:</strong> ${v.last_error}
            </div>` : ''}

            <div class="grid grid-cols-2 gap-2 text-xs">
              <div class="bg-white/[0.02] border border-white/[0.04] rounded p-2.5">
                <span class="text-[10px] text-gray-500 uppercase font-semibold block mb-0.5">Total Vectors</span>
                <span class="text-sm font-semibold text-gray-100 font-mono">${totalVectors.toLocaleString()}</span>
              </div>
              <div class="bg-white/[0.02] border border-white/[0.04] rounded p-2.5">
                <span class="text-[10px] text-gray-500 uppercase font-semibold block mb-0.5">Embedding Model</span>
                <span class="text-xs font-mono text-blue-400 block truncate" title="${model}">${model}</span>
              </div>
              <div class="bg-white/[0.02] border border-white/[0.04] rounded p-2.5">
                <span class="text-[10px] text-gray-500 uppercase font-semibold block mb-0.5">Dimension</span>
                <span class="text-xs font-mono text-gray-300">${dim}</span>
              </div>
              <div class="bg-white/[0.02] border border-white/[0.04] rounded p-2.5">
                <span class="text-[10px] text-gray-500 uppercase font-semibold block mb-0.5">Strategy</span>
                <span class="text-xs font-mono text-indigo-400 capitalize">${chunking}</span>
              </div>
              <div class="bg-white/[0.02] border border-white/[0.04] rounded p-2.5 col-span-2">
                <span class="text-[10px] text-gray-500 uppercase font-semibold block mb-0.5">Last Synced</span>
                <span class="text-xs text-gray-400">${lastSynced}</span>
              </div>
            </div>
          </div>

          <!-- Configuration Form -->
          <div class="bg-black/20 border border-white/[0.06] rounded-xl p-4 space-y-3">
            <h4 class="text-xs font-semibold text-gray-300">Index Configuration</h4>
            <div>
              <label class="block text-gray-400 mb-1">Source Column for Embeddings</label>
              <select id="vector-cfg-column" class="input-dark text-xs w-full py-1.5 font-mono" ${status === 'syncing' ? 'disabled' : ''}>
                ${cols.map(c => `<option value="${c}" ${c === sourceCol ? 'selected' : ''}>${c}</option>`).join('')}
              </select>
            </div>

            <div>
              <label class="block text-gray-400 mb-1">Chunking Strategy</label>
              <select id="vector-cfg-chunking" onchange="const el = document.getElementById('vector-cfg-chunk-params'); if (el) el.classList.toggle('hidden', this.value !== 'recursive')"
                      class="input-dark text-xs w-full py-1.5 font-mono" ${status === 'syncing' ? 'disabled' : ''}>
                <option value="recursive" ${chunking === 'recursive' ? 'selected' : ''}>Recursive (Multi-chunk character split)</option>
                <option value="document" ${chunking === 'document' ? 'selected' : ''}>Document (Whole row per vector)</option>
                <option value="agentic" ${chunking === 'agentic' ? 'selected' : ''}>Agentic (Heading & paragraph boundaries)</option>
              </select>
            </div>

            <div id="vector-cfg-chunk-params" class="grid grid-cols-2 gap-2 ${chunking === 'recursive' ? '' : 'hidden'}">
              <div>
                <label class="block text-gray-400 mb-1">Chunk Size (chars)</label>
                <input type="number" id="vector-cfg-chunk-size" value="${chunkSize}" min="32" max="8192"
                       class="input-dark text-xs w-full py-1.5 font-mono" ${status === 'syncing' ? 'disabled' : ''}/>
              </div>
              <div>
                <label class="block text-gray-400 mb-1">Chunk Overlap (chars)</label>
                <input type="number" id="vector-cfg-chunk-overlap" value="${chunkOverlap}" min="0" max="1024"
                       class="input-dark text-xs w-full py-1.5 font-mono" ${status === 'syncing' ? 'disabled' : ''}/>
              </div>
            </div>

            <div>
              <label class="block text-gray-400 mb-1">Format Template <span class="text-gray-600">(optional)</span></label>
              <input type="text" id="vector-cfg-template" value="${formatTpl ? formatTpl.replace(/"/g, '&quot;') : ''}"
                     placeholder="e.g. Movie: {title} | Overview: {overview}"
                     class="input-dark text-xs w-full py-1.5 font-mono" ${status === 'syncing' ? 'disabled' : ''}/>
              <p class="text-[11px] text-gray-500 mt-1">Use <code>{column_name}</code> placeholders to include contextual metadata.</p>
            </div>

            <button type="button" onclick="saveVectorConfig()" ${status === 'syncing' ? 'disabled' : ''}
                    class="btn-ghost text-xs py-1 px-3 border border-white/[0.08] text-gray-300 hover:text-white ${status === 'syncing' ? 'opacity-50 cursor-not-allowed pointer-events-none' : ''}">
              Save Configuration
            </button>
          </div>
        </div>
      `;

      if (status === 'syncing') {
        this.startVectorPolling(this.currentCatId, this.currentSchId, this.currentTableId);
      } else {
        this.stopVectorPolling();
      }
    },

    triggerVectorSync(btn, force = false) {
      if (!this.currentCatId || !this.currentSchId || !this.currentTableId) {
        if (window.showCatalogToast) window.showCatalogToast('Table identifiers are not available for sync.', 'error');
        return;
      }
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<svg class="animate-spin h-3.5 w-3.5 text-white inline mr-1" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg> Syncing...`;
      }

      const colEl = document.getElementById('vector-cfg-column');
      const tplEl = document.getElementById('vector-cfg-template');
      const chunkEl = document.getElementById('vector-cfg-chunking');
      const sizeEl = document.getElementById('vector-cfg-chunk-size');
      const overlapEl = document.getElementById('vector-cfg-chunk-overlap');

      const payload = {
        catalog_id: this.currentCatId,
        schema_id: this.currentSchId,
        table_id: this.currentTableId,
        source_column: colEl ? colEl.value : (this.currentTableData.vector_index || {}).source_column || 'overview',
        format_template: tplEl ? tplEl.value : (this.currentTableData.vector_index || {}).format_template || '',
        chunking: chunkEl ? chunkEl.value : (this.currentTableData.vector_index || {}).chunking || 'recursive',
        chunk_size: sizeEl ? parseInt(sizeEl.value, 10) : (this.currentTableData.vector_index || {}).chunk_size || 512,
        chunk_overlap: overlapEl ? parseInt(overlapEl.value, 10) : (this.currentTableData.vector_index || {}).chunk_overlap || 64,
        force: force,
      };

      fetch('/catalog/vector-sync', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      })
      .then(async r => {
        const res = await r.json().catch(() => ({}));
        return { status: r.status, ok: r.ok, data: res };
      })
      .then(result => {
        if (result.ok && result.data && result.data.table) {
          this.currentTableData = result.data.table;
          const el = document.querySelector(`[data-table-id="${this.currentTableId}"]`);
          if (el) {
            el.setAttribute('data-table', JSON.stringify(this.currentTableData));
          }
          this.renderVectorTab(this.currentTableData);
        } else if (result.status === 409 || (result.data && result.data.conflict)) {
          if (!this.currentTableData.vector_index) {
            this.currentTableData.vector_index = {};
          }
          this.currentTableData.vector_index.status = 'syncing';
          this.renderVectorTab(this.currentTableData);
          if (window.showCatalogToast) window.showCatalogToast(result.data.error || 'A vector sync is already in progress for this table.', 'warning');
        } else {
          if (!this.currentTableData.vector_index) {
            this.currentTableData.vector_index = {};
          }
          this.currentTableData.vector_index.status = 'error';
          this.currentTableData.vector_index.last_error = (result.data && result.data.error) || 'Unknown error';
          this.renderVectorTab(this.currentTableData);
          if (window.showCatalogToast) window.showCatalogToast('Vector sync failed: ' + ((result.data && result.data.error) || 'Unknown error'), 'error');
        }
      })
      .catch(err => {
        this.renderVectorTab(this.currentTableData);
        if (window.showCatalogToast) window.showCatalogToast('Network error: ' + err, 'error');
      });
    },

    saveVectorConfig() {
      const btn = document.getElementById('btn-vector-sync');
      if (btn) this.triggerVectorSync(btn);
    },

    syncEnrichButton() {
      const btn = document.getElementById('panel-ai-btn');
      const hasAi = window.HAS_AI_CONFIGURED !== undefined ? window.HAS_AI_CONFIGURED : true;
      if (!btn || !hasAi) return;
      if (!btn.dataset.origHtml) btn.dataset.origHtml = btn.innerHTML;
      const busy = this.enrichInFlight.has(this.currentTableId);
      btn.disabled = busy;
      btn.innerHTML = busy ? '<span class="animate-spin">⏳</span> AI...' : btn.dataset.origHtml;
    },

    async enrichCurrentTableAi() {
      const hasAi = window.HAS_AI_CONFIGURED !== undefined ? window.HAS_AI_CONFIGURED : true;
      if (!hasAi) {
        if (window.showCatalogToast) window.showCatalogToast('No AI provider configured. Configure an LLM in Settings → Admin Panel.', 'warning');
        return;
      }
      const tableData = this.currentTableData || window.currentTableData;
      const tableId = this.currentTableId || window.currentTableId;
      if (!tableData || this.enrichInFlight.has(tableId)) return;

      const ctx = {
        catId: this.currentCatId || window.currentCatId || '',
        schId: this.currentSchId || window.currentSchId || '',
        tableId: tableId,
        catIdent: this.currentCatIdent || window.currentCatIdent || 'default',
        schIdent: this.currentSchIdent || window.currentSchIdent || 'default',
        tableIdent: this.currentTableIdent || window.currentTableIdent || '',
        table: tableData
      };

      this.enrichInFlight.add(ctx.tableId);
      this.syncEnrichButton();

      const sampleRows = (this.sampleDataCache[`${ctx.schIdent}.${ctx.tableIdent}`] || {}).rows || [];
      const tableName = ctx.table.name || ctx.tableIdent;
      const payload = JSON.stringify({
        asset_type: 'table',
        name: tableName,
        table_name: tableName,
        catalog_name: ctx.catIdent,
        schema_name: ctx.schIdent,
        columns: ctx.table.column_defs || [],
        sample_rows: sampleRows.slice(0, 5)
      });

      const wsSlug = (document.cookie.match(/(?:^|;\s*)lake_workspace_slug=([^;]+)/) || [])[1] || 'default';
      const reqHeaders = {
        'Content-Type': 'application/json',
        'X-Workspace-Slug': wsSlug
      };

      try {
        let resp = await fetch('/api/catalog/ai/generate-description', {
          method: 'POST',
          credentials: 'include',
          headers: reqHeaders,
          body: payload
        });
        if (!resp.ok && resp.status === 404) {
          resp = await fetch('/api/ai/generate-description', {
            method: 'POST',
            credentials: 'include',
            headers: reqHeaders,
            body: payload
          });
        }
        if (!resp.ok && resp.status === 404) {
          resp = await fetch('/api/catalog/ai/enrich-table', {
            method: 'POST',
            credentials: 'include',
            headers: reqHeaders,
            body: payload
          });
        }
        if (!resp.ok && resp.status === 404) {
          resp = await fetch('/api/ai/enrich-table', {
            method: 'POST',
            credentials: 'include',
            headers: reqHeaders,
            body: payload
          });
        }
        const data = await resp.json();
        if (data.ok) {
          if (data.description) ctx.table.description = data.description;
          if (data.column_descriptions && ctx.table.column_defs) {
            ctx.table.column_defs.forEach(col => {
              if (data.column_descriptions[col.name]) {
                col.description = data.column_descriptions[col.name];
              }
            });
          }

          if (this.currentTableId === ctx.tableId) {
            this.currentTableData = ctx.table;
            this.renderColumnsTab(ctx.table);
            this.renderDetailsTab(ctx.table, ctx.catIdent, ctx.schIdent, ctx.tableIdent);
          }
          const rowEl = document.querySelector(`[data-table-id="${ctx.tableId}"]`);
          if (rowEl) rowEl.setAttribute('data-table', JSON.stringify(ctx.table));

          let saveResp = await fetch(
            `/catalog/${ctx.catId}/schemas/${ctx.schId}/tables/${ctx.tableId}`,
            {
              method: 'PATCH',
              credentials: 'include',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                description: ctx.table.description,
                column_defs: ctx.table.column_defs || null
              })
            }
          );
          if (!saveResp.ok && saveResp.status === 404) {
            saveResp = await fetch(
              `/api/catalogs/${ctx.catId}/schemas/${ctx.schId}/tables/${ctx.tableId}`,
              {
                method: 'PATCH',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                  description: ctx.table.description,
                  column_defs: ctx.table.column_defs || null
                })
              }
            );
          }
          const saved = await saveResp.json().catch(() => ({}));
          const label = ctx.tableIdent ? ` (${ctx.tableIdent})` : '';
          if (saveResp.ok && saved.ok) {
            if (window.showCatalogToast) window.showCatalogToast('Table enriched with AI and saved' + label, 'success');
          } else {
            if (window.showCatalogToast) window.showCatalogToast('Generated, but saving failed' + label + ': ' + (saved.error || saveResp.status), 'error');
          }
        } else {
          if (window.showCatalogToast) window.showCatalogToast(data.detail || data.error || 'Failed to enrich with AI', 'error');
        }
      } catch (err) {
        if (window.showCatalogToast) window.showCatalogToast('Network error: ' + err, 'error');
      } finally {
        this.enrichInFlight.delete(ctx.tableId);
        this.syncEnrichButton();
      }
    },

    openCurrentTableToon() {
      if (!this.currentTableData) return;
      if (window.openExportToonModal) {
        window.openExportToonModal('table', this.currentCatId, this.currentSchId, this.currentTableIdent, this.currentTableId);
      }
    }
  }));

  // ── 3. Creation Wizards & Modals Manager ───────────────────────────────────
  Alpine.data('catalogWizardsManager', () => ({
    // Schema creation
    createSchema: {
      catId: '',
      name: '',
      desc: '',
      isAiGenerating: false,
      isSubmitting: false,
      error: null
    },

    // File upload table creation
    createUpload: {
      catId: '',
      schId: '',
      tableName: '',
      file: null,
      columns: [],
      isPreviewing: false,
      isSubmitting: false,
      statusHint: 'Select a file to preview schema',
      error: null
    },

    // Prompt to table
    createPrompt: {
      catId: '',
      schId: '',
      prompt: '',
      generatedSpec: null,
      isAiGenerating: false,
      isSubmitting: false,
      statusHint: 'Enter prompt and click Generate',
      error: null
    },

    // Export TOON
    exportToon: {
      assetType: 'schema',
      targetName: '',
      toonText: '',
      filename: 'asset.toon',
      isLoading: false
    },

    // Delete Table
    deleteTable: {
      catId: '',
      schId: '',
      tableId: '',
      name: '',
      qual: '',
      confirmInput: '',
      isManaged: true,
      s3Path: '',
      dependencies: [],
      isSubmitting: false
    },

    // Delete Schema
    deleteSchema: {
      catId: '',
      schId: '',
      slug: '',
      name: '',
      childCount: 0,
      qual: '',
      confirmInput: '',
      cascade: false,
      dependencies: [],
      isSubmitting: false
    },

    init() {
      window.openCreateSchemaModal = (catId, catName) => this.openCreateSchema(catId, catName);
      window.closeCreateSchemaModal = () => this.closeCreateSchema();
      window.generateSchemaDescriptionAi = () => this.generateSchemaDescriptionAi();
      window.submitCreateSchema = (e) => this.submitCreateSchema(e);

      window.toggleCreateDropdown = (e, ddId) => this.toggleCreateDropdown(e, ddId);
      window.closeAllDropdowns = () => this.closeAllDropdowns();

      window.openCreateTableUploadModal = (catId, schId, catSlug, schSlug) => this.openCreateTableUpload(catId, schId, catSlug, schSlug);
      window.closeCreateTableUploadModal = () => this.closeCreateTableUpload();
      window.onUploadCatChanged = () => this.onUploadCatChanged();
      window.handleFileSelected = (e) => this.handleFileSelected(e);
      window.resetUploadFile = () => this.resetUploadFile();
      window.toggleAllUploadCols = (checked) => this.toggleAllUploadCols(checked);
      window.submitCreateTableUpload = () => this.submitCreateTableUpload();

      window.openCreateTablePromptModal = (catId, schId, catSlug, schSlug) => this.openCreateTablePrompt(catId, schId, catSlug, schSlug);
      window.closeCreateTablePromptModal = () => this.closeCreateTablePrompt();
      window.onPromptCatChanged = () => this.onPromptCatChanged();
      window.generateTableFromPromptAi = () => this.generateTableFromPromptAi();
      window.copyPromptToon = () => this.copyPromptToon();
      window.submitCreateTableFromToon = () => this.submitCreateTableFromToon();

      window.openExportToonModal = (assetType, catId, schId, name, tableId) => this.openExportToon(assetType, catId, schId, name, tableId);
      window.closeExportToonModal = () => this.closeExportToon();
      window.copyExportToonContent = () => this.copyExportToonContent();
      window.downloadExportToonFile = () => this.downloadExportToonFile();

      window.openDeleteTableModal = () => this.openDeleteTableModal();
      window.closeDeleteTableModal = () => this.closeDeleteTableModal();
      window.checkDeleteTableConfirm = () => this.checkDeleteTableConfirm();
      window.submitDeleteTable = () => this.submitDeleteTable();

      window.openDeleteSchemaModal = (catId, schId, slug, name, childCount) => this.openDeleteSchemaModal(catId, schId, slug, name, childCount);
      window.closeDeleteSchemaModal = () => this.closeDeleteSchemaModal();
      window.checkDeleteSchemaConfirm = () => this.checkDeleteSchemaConfirm();
      window.submitDeleteSchema = () => this.submitDeleteSchema();

      window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          this.closeAllModals();
        }
      });

      document.addEventListener('click', (e) => {
        if (!e.target.closest('[id^="dropdown-schema-"]')) {
          this.closeAllDropdowns();
        }
      });
    },

    closeAllModals() {
      this.closeCreateSchema();
      this.closeCreateTableUpload();
      this.closeCreateTablePrompt();
      this.closeExportToon();
      this.closeDeleteTableModal();
      this.closeDeleteSchemaModal();
      this.closeAllDropdowns();
    },

    // --- Dropdowns ---
    toggleCreateDropdown(e, dropdownId) {
      if (e) e.stopPropagation();
      const dd = document.getElementById(dropdownId);
      if (!dd) return;
      const isHidden = dd.classList.contains('hidden');
      this.closeAllDropdowns();
      if (isHidden) {
        dd.classList.remove('hidden');
        const btn = document.querySelector(`[data-dropdown-btn="${dropdownId}"]`);
        if (btn) {
          btn.classList.add('bg-emerald-500/20', 'border-emerald-500/60');
          const chev = btn.querySelector('.dropdown-chev');
          if (chev) chev.classList.add('rotate-180');
        }
      }
    },

    closeAllDropdowns() {
      document.querySelectorAll('[id^="dropdown-schema-"]').forEach(el => el.classList.add('hidden'));
      document.querySelectorAll('[data-dropdown-btn]').forEach(btn => {
        btn.classList.remove('bg-emerald-500/20', 'border-emerald-500/60');
        const chev = btn.querySelector('.dropdown-chev');
        if (chev) chev.classList.remove('rotate-180');
      });
    },

    // --- Schema Creation Wizard ---
    openCreateSchema(catId, catName) {
      this.closeAllDropdowns();
      const modal = document.getElementById('create-schema-modal');
      const catSelect = document.getElementById('create-schema-catalog-select');
      if (catSelect && catId) {
        catSelect.value = catId;
      }
      const nameInput = document.getElementById('create-schema-name-input');
      const descInput = document.getElementById('create-schema-desc-input');
      const errDiv = document.getElementById('create-schema-error');
      if (nameInput) nameInput.value = '';
      if (descInput) descInput.value = '';
      if (errDiv) errDiv.classList.add('hidden');
      this.updateSchemaSlugPreview();
      if (modal) modal.classList.remove('hidden');
    },

    closeCreateSchema() {
      const modal = document.getElementById('create-schema-modal');
      if (modal) modal.classList.add('hidden');
    },

    updateSchemaSlugPreview() {
      const nameInput = document.getElementById('create-schema-name-input');
      const name = nameInput ? nameInput.value : '';
      const slug = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'schema';
      const prefix = document.getElementById('schema-prefix-preview');
      if (prefix) {
        const ws = window.CURRENT_WORKSPACE_SLUG || 'default';
        prefix.textContent = `workspaces/${ws}/.../${slug}/`;
      }
    },

    async generateSchemaDescriptionAi() {
      const hasAi = window.HAS_AI_CONFIGURED !== undefined ? window.HAS_AI_CONFIGURED : true;
      if (!hasAi) {
        if (window.showCatalogToast) window.showCatalogToast('No AI provider configured. Configure an LLM in Settings → Admin Panel.', 'warning');
        return;
      }
      const nameInput = document.getElementById('create-schema-name-input');
      const name = nameInput ? nameInput.value.trim() : '';
      if (!name) {
        if (window.showCatalogToast) window.showCatalogToast('Please enter a schema name first.', 'warning');
        return;
      }
      const btn = document.getElementById('btn-ai-schema-desc');
      const origText = btn ? btn.innerHTML : '';
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="animate-spin">⏳</span> Generating...';
      }

      try {
        const catSelect = document.getElementById('create-schema-catalog-select');
        const catName = catSelect ? catSelect.options[catSelect.selectedIndex].text : 'main';
        const payload = JSON.stringify({
          asset_type: 'schema',
          name: name,
          catalog_name: catName
        });

        let resp = await fetch('/api/ai/generate-description', {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: payload
        });
        if (!resp.ok && resp.status === 404) {
          resp = await fetch('/api/catalog/ai/generate-description', {
            method: 'POST',
            credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: payload
          });
        }
        const data = await resp.json();
        if (data.ok && data.description) {
          const descInput = document.getElementById('create-schema-desc-input');
          if (descInput) descInput.value = data.description;
        } else {
          if (window.showCatalogToast) window.showCatalogToast(data.detail || data.error || 'Failed to generate description', 'error');
        }
      } catch (err) {
        if (window.showCatalogToast) window.showCatalogToast('Network error: ' + err, 'error');
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = origText;
        }
      }
    },

    async submitCreateSchema(e) {
      if (e && e.preventDefault) e.preventDefault();
      const catSelect = document.getElementById('create-schema-catalog-select');
      const nameInput = document.getElementById('create-schema-name-input');
      const descInput = document.getElementById('create-schema-desc-input');
      const errDiv = document.getElementById('create-schema-error');
      const submitBtn = document.getElementById('btn-submit-schema');

      const catId = catSelect ? catSelect.value : '';
      const name = nameInput ? nameInput.value.trim() : '';
      const desc = descInput ? descInput.value.trim() : '';

      if (errDiv) errDiv.classList.add('hidden');
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span>Creating...</span>';
      }

      try {
        const payload = JSON.stringify({ name: name, description: desc || null });
        let resp = await fetch(`/api/catalogs/${catId}/schemas`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: payload
        });
        if (!resp.ok && resp.status === 404) {
          resp = await fetch(`/api/catalog/${catId}/schemas`, {
            method: 'POST',
            credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: payload
          });
        }
        const data = await resp.json();
        if (resp.ok && (data.id || (data.ok !== false && data.ok !== undefined))) {
          this.closeCreateSchema();
          window.location.reload();
        } else {
          if (errDiv) {
            errDiv.textContent = data.detail || data.error || 'Failed to create schema';
            errDiv.classList.remove('hidden');
          }
        }
      } catch (err) {
        if (errDiv) {
          errDiv.textContent = 'Network error: ' + err;
          errDiv.classList.remove('hidden');
        }
      } finally {
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.innerHTML = '<span>Create Schema</span>';
        }
      }
    },

    // --- File Upload Wizard ---
    populateSchemaOptions(catSelectId, schSelectId, selectedCatId, selectedSchId) {
      const catSelect = document.getElementById(catSelectId);
      const schSelect = document.getElementById(schSelectId);
      if (!catSelect || !schSelect) return;

      const currentCatId = selectedCatId || catSelect.value;
      const catalogs = window.LOT_CATALOGS_DATA || [];
      const cat = catalogs.find(c => String(c.id) === String(currentCatId));
      schSelect.innerHTML = '';

      if (cat && cat.schemas && cat.schemas.length > 0) {
        cat.schemas.forEach(s => {
          const opt = document.createElement('option');
          opt.value = s.id;
          opt.textContent = `${s.slug} (${s.name || s.tier || 'custom'})`;
          if (selectedSchId && String(s.id) === String(selectedSchId)) {
            opt.selected = true;
          }
          schSelect.appendChild(opt);
        });
      } else {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = 'No schemas available';
        schSelect.appendChild(opt);
      }
    },

    openCreateTableUpload(catId, schId, catSlug, schSlug) {
      this.closeAllDropdowns();
      const modal = document.getElementById('create-table-upload-modal');
      const catSelect = document.getElementById('upload-table-cat-select');
      if (catSelect && catId) {
        catSelect.value = catId;
      }
      this.populateSchemaOptions('upload-table-cat-select', 'upload-table-sch-select', catId, schId);

      this.resetUploadFile();
      const nameInput = document.getElementById('upload-table-name-input');
      const errDiv = document.getElementById('upload-error');
      if (nameInput) nameInput.value = '';
      if (errDiv) errDiv.classList.add('hidden');
      if (modal) modal.classList.remove('hidden');
    },

    closeCreateTableUpload() {
      const modal = document.getElementById('create-table-upload-modal');
      if (modal) modal.classList.add('hidden');
    },

    onUploadCatChanged() {
      this.populateSchemaOptions('upload-table-cat-select', 'upload-table-sch-select');
    },

    resetUploadFile() {
      this.createUpload.file = null;
      this.createUpload.columns = [];
      const fileInput = document.getElementById('upload-file-input');
      const dropzone = document.getElementById('upload-dropzone');
      const fileInfo = document.getElementById('upload-file-info');
      const colsSection = document.getElementById('upload-columns-section');
      const submitBtn = document.getElementById('btn-submit-upload-table');
      const hint = document.getElementById('upload-status-hint');

      if (fileInput) fileInput.value = '';
      if (dropzone) dropzone.classList.remove('hidden');
      if (fileInfo) fileInfo.classList.add('hidden');
      if (colsSection) colsSection.classList.add('hidden');
      if (submitBtn) submitBtn.disabled = true;
      if (hint) hint.textContent = 'Select a file to preview schema';
    },

    async handleFileSelected(e) {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      this.createUpload.file = file;

      const nameEl = document.getElementById('upload-file-name');
      const sizeEl = document.getElementById('upload-file-size');
      const formatBadge = document.getElementById('upload-format-badge');
      const dropzone = document.getElementById('upload-dropzone');
      const fileInfo = document.getElementById('upload-file-info');
      const nameInput = document.getElementById('upload-table-name-input');
      const hint = document.getElementById('upload-status-hint');

      if (nameEl) nameEl.textContent = file.name;
      if (sizeEl) sizeEl.textContent = (file.size / 1024).toFixed(1) + ' KB';
      const ext = file.name.split('.').pop().toUpperCase();
      if (formatBadge) formatBadge.textContent = ext;
      if (dropzone) dropzone.classList.add('hidden');
      if (fileInfo) fileInfo.classList.remove('hidden');

      if (nameInput && !nameInput.value.trim()) {
        nameInput.value = file.name.split('.')[0].toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
      }

      if (hint) hint.textContent = 'Parsing schema and sampling rows...';

      const formData = new FormData();
      formData.append('file', file);

      try {
        let resp = await fetch('/catalog/preview-file', {
          method: 'POST',
          credentials: 'include',
          body: formData
        });
        if (!resp.ok && resp.status === 404) {
          resp = await fetch('/api/ingest/preview-file', {
            method: 'POST',
            credentials: 'include',
            body: formData
          });
        }
        const data = await resp.json();
        if (data.ok && data.columns) {
          this.createUpload.columns = data.columns;
          this.renderUploadColumnsGrid(data.columns, data.rows || []);
          if (hint) hint.textContent = `Inferred ${data.columns.length} columns from ${file.name}`;
          const submitBtn = document.getElementById('btn-submit-upload-table');
          if (submitBtn) submitBtn.disabled = false;
        } else {
          if (window.showCatalogToast) window.showCatalogToast(data.error || 'Failed to infer schema from file', 'error');
          this.resetUploadFile();
        }
      } catch (err) {
        if (window.showCatalogToast) window.showCatalogToast('Failed to preview file: ' + err, 'error');
        this.resetUploadFile();
      }
    },

    renderUploadColumnsGrid(columns, sampleRows) {
      const tbody = document.getElementById('upload-cols-tbody');
      const countEl = document.getElementById('upload-col-count');
      const section = document.getElementById('upload-columns-section');
      if (!tbody) return;
      tbody.innerHTML = '';
      if (countEl) countEl.textContent = `${columns.length} columns inferred`;

      const supportedTypes = ['VARCHAR', 'BIGINT', 'INTEGER', 'DOUBLE', 'BOOLEAN', 'TIMESTAMP', 'DATE', 'JSON'];

      columns.forEach((col, idx) => {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-white/[0.02] transition-colors';
        const colTypeUpper = (col.type || 'VARCHAR').toUpperCase();

        tr.innerHTML = `
          <td class="p-2 text-center">
            <input type="checkbox" class="col-include-cb" data-idx="${idx}" checked/>
          </td>
          <td class="p-2">
            <input type="text" class="input-dark w-full text-xs py-1 px-2 font-mono col-name-input" data-idx="${idx}" value="${col.name || 'col_' + idx}" data-orig="${col.name || ''}"/>
          </td>
          <td class="p-2">
            <select class="input-dark w-full text-xs py-1 px-1.5 font-mono col-type-select" data-idx="${idx}">
              ${supportedTypes.map(t => `<option value="${t}" ${t === colTypeUpper || (colTypeUpper.startsWith(t)) ? 'selected' : ''}>${t}</option>`).join('')}
            </select>
          </td>
          <td class="p-2 text-center">
            <input type="checkbox" class="col-null-cb" data-idx="${idx}" ${col.nullable !== false ? 'checked' : ''}/>
          </td>
          <td class="p-2">
            <input type="text" class="input-dark w-full text-xs py-1 px-2 col-desc-input" data-idx="${idx}" placeholder="column description" value="${col.description || ''}"/>
          </td>
        `;
        tbody.appendChild(tr);
      });

      if (section) section.classList.remove('hidden');
    },

    toggleAllUploadCols(checked) {
      document.querySelectorAll('.col-include-cb').forEach(cb => cb.checked = checked);
    },

    async submitCreateTableUpload() {
      if (!this.createUpload.file) return;

      const catSelect = document.getElementById('upload-table-cat-select');
      const schSelect = document.getElementById('upload-table-sch-select');
      const nameInput = document.getElementById('upload-table-name-input');
      const errDiv = document.getElementById('upload-error');
      const submitBtn = document.getElementById('btn-submit-upload-table');

      const catId = catSelect ? catSelect.value : '';
      const schId = schSelect ? schSelect.value : '';
      const tableName = nameInput ? nameInput.value.trim() : '';

      if (!catId || !schId || !tableName) {
        if (window.showCatalogToast) window.showCatalogToast('Please fill in Catalog, Schema, and Table Name.', 'warning');
        return;
      }

      const columnOverrides = [];
      const rows = document.querySelectorAll('#upload-cols-tbody tr');
      rows.forEach((tr, idx) => {
        const include = tr.querySelector('.col-include-cb').checked;
        const colNameInput = tr.querySelector('.col-name-input');
        const typeSelect = tr.querySelector('.col-type-select');
        const nullCb = tr.querySelector('.col-null-cb');
        const descInput = tr.querySelector('.col-desc-input');

        columnOverrides.push({
          include: include,
          source_name: colNameInput.getAttribute('data-orig') || colNameInput.value,
          name: colNameInput.value.trim() || 'col_' + idx,
          type: typeSelect.value,
          nullable: nullCb.checked,
          description: descInput.value.trim() || null
        });
      });

      if (errDiv) errDiv.classList.add('hidden');
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="animate-spin">⏳</span> Creating Table...';
      }

      const formData = new FormData();
      formData.append('file', this.createUpload.file);
      formData.append('catalog_id', catId);
      formData.append('schema_id', schId);
      formData.append('table_name', tableName);
      formData.append('columns_override', JSON.stringify(columnOverrides));

      try {
        let resp = await fetch('/catalog/create-table-upload', {
          method: 'POST',
          credentials: 'include',
          body: formData
        });
        if (!resp.ok && resp.status === 404) {
          resp = await fetch('/api/catalog/create-table-upload', {
            method: 'POST',
            credentials: 'include',
            body: formData
          });
        }
        const data = await resp.json();
        if (resp.ok && (data.id || (data.ok !== false && data.ok !== undefined))) {
          this.closeCreateTableUpload();
          window.location.reload();
        } else {
          if (errDiv) {
            errDiv.textContent = data.detail || data.error || 'Failed to create table from file';
            errDiv.classList.remove('hidden');
          }
        }
      } catch (err) {
        if (errDiv) {
          errDiv.textContent = 'Network error: ' + err;
          errDiv.classList.remove('hidden');
        }
      } finally {
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.innerHTML = '<span>Apply Schema & Create Table</span>';
        }
      }
    },

    // --- Prompt to Table Wizard ---
    openCreateTablePrompt(catId, schId, catSlug, schSlug) {
      this.closeAllDropdowns();
      const modal = document.getElementById('create-table-prompt-modal');
      const catSelect = document.getElementById('prompt-table-cat-select');
      if (catSelect && catId) {
        catSelect.value = catId;
      }
      this.populateSchemaOptions('prompt-table-cat-select', 'prompt-table-sch-select', catId, schId);

      this.createPrompt.generatedSpec = null;
      const promptInput = document.getElementById('prompt-table-input');
      const previewContainer = document.getElementById('prompt-preview-container');
      const submitBtn = document.getElementById('btn-submit-prompt-table');
      const hint = document.getElementById('prompt-status-hint');
      const errDiv = document.getElementById('prompt-error');

      if (promptInput) promptInput.value = '';
      if (previewContainer) previewContainer.classList.add('hidden');
      if (submitBtn) submitBtn.disabled = true;
      if (errDiv) errDiv.classList.add('hidden');
      if (hint) hint.textContent = 'Enter prompt and click Generate';
      if (modal) modal.classList.remove('hidden');
    },

    closeCreateTablePrompt() {
      const modal = document.getElementById('create-table-prompt-modal');
      if (modal) modal.classList.add('hidden');
    },

    onPromptCatChanged() {
      this.populateSchemaOptions('prompt-table-cat-select', 'prompt-table-sch-select');
    },

    async generateTableFromPromptAi() {
      const hasAi = window.HAS_AI_CONFIGURED !== undefined ? window.HAS_AI_CONFIGURED : true;
      if (!hasAi) {
        if (window.showCatalogToast) window.showCatalogToast('No AI provider configured. Configure an LLM in Settings → Admin Panel.', 'warning');
        return;
      }
      const promptInput = document.getElementById('prompt-table-input');
      const prompt = promptInput ? promptInput.value.trim() : '';
      if (!prompt) {
        if (window.showCatalogToast) window.showCatalogToast('Please enter a natural language prompt describing your table.', 'warning');
        return;
      }

      const catSelect = document.getElementById('prompt-table-cat-select');
      const schSelect = document.getElementById('prompt-table-sch-select');
      const catName = catSelect ? catSelect.options[catSelect.selectedIndex].text : 'main';
      const schName = schSelect && schSelect.selectedIndex >= 0 ? schSelect.options[schSelect.selectedIndex].text.split(' ')[0] : 'bronze';

      const btn = document.getElementById('btn-generate-toon');
      const origHtml = btn ? btn.innerHTML : '';
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="animate-spin">⏳</span> AI...';
      }

      const errDiv = document.getElementById('prompt-error');
      if (errDiv) errDiv.classList.add('hidden');

      try {
        const payload = JSON.stringify({
          prompt: prompt,
          catalog: catName,
          schema: schName
        });
        let resp = await fetch('/api/ai/table-from-prompt', {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: payload
        });
        if (!resp.ok && resp.status === 404) {
          resp = await fetch('/api/catalog/ai/prompt-to-table', {
            method: 'POST',
            credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: payload
          });
        }
        const data = await resp.json();
        if (data.ok) {
          this.createPrompt.generatedSpec = data;
          const toonPreview = document.getElementById('prompt-toon-preview');
          const colCards = document.getElementById('prompt-cols-preview');
          const colCount = document.getElementById('prompt-col-count');
          const previewContainer = document.getElementById('prompt-preview-container');
          const submitBtn = document.getElementById('btn-submit-prompt-table');
          const hint = document.getElementById('prompt-status-hint');

          if (toonPreview) toonPreview.textContent = data.toon;
          if (colCards) {
            colCards.innerHTML = '';
            (data.columns || []).forEach(c => {
              const item = document.createElement('div');
              item.className = 'p-2 rounded bg-white/[0.03] border border-white/[0.04] flex items-center justify-between text-xs';
              item.innerHTML = `
                <div class="flex items-center gap-2">
                  <span class="font-mono text-cyan-400 font-medium">${c.name}</span>
                  <span class="px-1.5 py-0.5 rounded bg-blue-950/40 text-blue-400 font-mono text-[10px]">${c.type}</span>
                  ${c.nullable === false ? '<span class="text-[10px] text-amber-400 font-mono">NOT NULL</span>' : ''}
                </div>
                <span class="text-[11px] text-gray-500 truncate max-w-[200px]">${c.description || ''}</span>
              `;
              colCards.appendChild(item);
            });
          }
          if (colCount) colCount.textContent = `${(data.columns || []).length} columns`;
          if (previewContainer) previewContainer.classList.remove('hidden');
          if (submitBtn) submitBtn.disabled = false;
          if (hint) hint.textContent = `Ready to create table "${data.table_name}"`;
        } else {
          if (errDiv) {
            errDiv.textContent = data.detail || data.error || 'Failed to generate table specification';
            errDiv.classList.remove('hidden');
          }
        }
      } catch (err) {
        if (errDiv) {
          errDiv.textContent = 'Network error: ' + err;
          errDiv.classList.remove('hidden');
        }
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = origHtml;
        }
      }
    },

    copyPromptToon() {
      const code = document.getElementById('prompt-toon-preview');
      const text = code ? code.textContent : '';
      if (!text) return;
      navigator.clipboard.writeText(text).then(() => {
        if (window.showCatalogToast) window.showCatalogToast('Copied TOON specification to clipboard', 'success');
      });
    },

    async submitCreateTableFromToon() {
      if (!this.createPrompt.generatedSpec) return;

      const catSelect = document.getElementById('prompt-table-cat-select');
      const schSelect = document.getElementById('prompt-table-sch-select');
      const errDiv = document.getElementById('prompt-error');
      const submitBtn = document.getElementById('btn-submit-prompt-table');

      const catId = catSelect ? catSelect.value : '';
      const schId = schSelect ? schSelect.value : '';

      if (errDiv) errDiv.classList.add('hidden');
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="animate-spin">⏳</span> Creating Table...';
      }

      const spec = this.createPrompt.generatedSpec;
      const formData = new FormData();
      formData.append('catalog_id', catId);
      formData.append('schema_id', schId);
      formData.append('table_name', spec.table_name);
      formData.append('description', spec.description || `Managed table ${spec.table_name}`);
      formData.append('toon', spec.toon);
      formData.append('columns_json', JSON.stringify(spec.columns));

      try {
        let resp = await fetch('/catalog/create-table-from-toon', {
          method: 'POST',
          credentials: 'include',
          body: formData
        });
        if (!resp.ok && resp.status === 404) {
          resp = await fetch('/api/catalog/create-table-from-toon', {
            method: 'POST',
            credentials: 'include',
            body: formData
          });
        }
        const data = await resp.json();
        if (resp.ok && (data.id || (data.ok !== false && data.ok !== undefined))) {
          this.closeCreateTablePrompt();
          window.location.reload();
        } else {
          if (errDiv) {
            errDiv.textContent = data.detail || data.error || 'Failed to create table from TOON';
            errDiv.classList.remove('hidden');
          }
        }
      } catch (err) {
        if (errDiv) {
          errDiv.textContent = 'Network error: ' + err;
          errDiv.classList.remove('hidden');
        }
      } finally {
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.innerHTML = '<span>Apply TOON & Create Table</span>';
        }
      }
    },

    // --- Export TOON Modal ---
    async openExportToon(assetType, catId, schId, name, tableId) {
      this.closeAllDropdowns();
      this.exportToon = {
        assetType: assetType,
        targetName: name,
        toonText: '',
        filename: `${name}.toon`,
        isLoading: true
      };

      const modal = document.getElementById('export-toon-modal');
      const targetBadge = document.getElementById('export-toon-target');
      const codeBlock = document.getElementById('export-toon-code');

      if (targetBadge) targetBadge.textContent = `${assetType}: ${name}`;
      if (codeBlock) codeBlock.textContent = '# Fetching TOON specification...';
      if (modal) modal.classList.remove('hidden');

      const endpoint = assetType === 'schema'
        ? `/api/catalogs/${catId}/schemas/${schId}/toon`
        : `/api/catalogs/${catId}/schemas/${schId}/tables/${tableId}/toon`;

      try {
        let resp = await fetch(endpoint, { credentials: 'include' });
        if (!resp.ok && resp.status === 404) {
          const altEndpoint = assetType === 'schema'
            ? `/api/catalog/${catId}/schemas/${schId}/toon`
            : `/api/catalog/${catId}/schemas/${schId}/tables/${tableId}/toon`;
          resp = await fetch(altEndpoint, { credentials: 'include' });
        }
        const data = await resp.json();
        if (data.ok && data.toon) {
          this.exportToon.toonText = data.toon;
          this.exportToon.filename = data.filename || `${name}.toon`;
          if (codeBlock) codeBlock.textContent = data.toon;
        } else {
          if (codeBlock) codeBlock.textContent = '# Error: ' + (data.detail || data.error || 'Failed to generate TOON');
        }
      } catch (err) {
        if (codeBlock) codeBlock.textContent = '# Network error: ' + err;
      } finally {
        this.exportToon.isLoading = false;
      }
    },

    closeExportToon() {
      const modal = document.getElementById('export-toon-modal');
      if (modal) modal.classList.add('hidden');
    },

    copyExportToonContent() {
      if (!this.exportToon.toonText) return;
      navigator.clipboard.writeText(this.exportToon.toonText).then(() => {
        const btnText = document.getElementById('copy-toon-btn-text');
        if (btnText) {
          const orig = btnText.textContent;
          btnText.textContent = 'Copied!';
          setTimeout(() => { btnText.textContent = orig; }, 2000);
        }
      });
    },

    downloadExportToonFile() {
      if (!this.exportToon.toonText) return;
      const blob = new Blob([this.exportToon.toonText], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = this.exportToon.filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    },

    // --- Delete Table Modal ---
    async openDeleteTableModal() {
      const detailsMgr = Alpine.$data(document.getElementById('table-panel')) || {};
      const currentTableData = detailsMgr.currentTableData || window.currentTableData;
      if (!currentTableData) return;

      this.deleteTable = {
        catId: detailsMgr.currentCatId || window.currentCatId || '',
        schId: detailsMgr.currentSchId || window.currentSchId || '',
        tableId: detailsMgr.currentTableId || window.currentTableId || '',
        name: detailsMgr.currentTableIdent || window.currentTableIdent || '',
        qual: `${detailsMgr.currentCatIdent || window.currentCatIdent}.${detailsMgr.currentSchIdent || window.currentSchIdent}.${detailsMgr.currentTableIdent || window.currentTableIdent}`,
        confirmInput: '',
        isManaged: true,
        s3Path: (currentTableData.s3_path_pattern || '').trim(),
        dependencies: [],
        isSubmitting: false
      };

      const modal = document.getElementById('delete-table-modal');
      const qual = document.getElementById('delete-table-qualified-name');
      const targetLabel = document.getElementById('delete-table-confirm-target');
      const input = document.getElementById('delete-table-confirm-input');
      const submitBtn = document.getElementById('delete-table-submit-btn');
      const banner = document.getElementById('delete-table-storage-banner');
      const depsContainer = document.getElementById('delete-table-deps-container');
      const depsList = document.getElementById('delete-table-deps-list');

      if (qual) qual.textContent = this.deleteTable.qual;
      if (targetLabel) targetLabel.textContent = this.deleteTable.name;
      if (input) input.value = '';
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.classList.add('opacity-50', 'cursor-not-allowed');
      }

      const s3Pat = this.deleteTable.s3Path;
      const isManaged = s3Pat.includes('/workspaces/') || !s3Pat;
      this.deleteTable.isManaged = isManaged;

      if (banner) {
        if (isManaged) {
          banner.className = 'p-3 rounded-lg text-xs flex items-start gap-2.5 bg-blue-950/30 border border-blue-800/40 text-blue-200';
          banner.innerHTML = `
            <svg class="w-4 h-4 text-blue-400 shrink-0 mt-0.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
            <div>
              <span class="font-semibold text-blue-300">Managed Lakehouse Table</span>
              <p class="text-[11px] text-blue-400/80 mt-0.5">Parquet data and vector embeddings will be moved to the workspace trash and permanently purged after 7 days (UNDROP recoverable).</p>
            </div>`;
        } else {
          banner.className = 'p-3 rounded-lg text-xs flex items-start gap-2.5 bg-gray-900 border border-gray-700 text-gray-300';
          banner.innerHTML = `
            <svg class="w-4 h-4 text-gray-400 shrink-0 mt-0.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
            <div>
              <span class="font-semibold text-gray-200">External Table</span>
              <p class="text-[11px] text-gray-400 mt-0.5">Only catalog metadata will be dropped. Raw data files at <code class="font-mono text-gray-300">${s3Pat}</code> remain untouched.</p>
            </div>`;
        }
      }

      if (depsContainer) depsContainer.classList.add('hidden');
      if (depsList) depsList.innerHTML = '';
      if (modal) modal.classList.remove('hidden');

      try {
        const dResp = await fetch(`/api/catalog/${this.deleteTable.catId}/schemas/${this.deleteTable.schId}/tables/${this.deleteTable.tableId}/dependencies`, { credentials: 'include' });
        if (dResp.ok) {
          const dData = await dResp.json();
          if (dData.has_dependencies && dData.dependencies && dData.dependencies.length > 0) {
            this.deleteTable.dependencies = dData.dependencies;
            if (depsContainer) depsContainer.classList.remove('hidden');
            if (depsList) depsList.innerHTML = dData.dependencies.map(d => `<li>${d.type.toUpperCase()}: ${d.name} ${d.match_snippet ? `— "${d.match_snippet}"` : ''}</li>`).join('');
          }
        }
      } catch (err) {
        console.debug('Failed loading table dependencies', err);
      }
    },

    closeDeleteTableModal() {
      const modal = document.getElementById('delete-table-modal');
      if (modal) modal.classList.add('hidden');
    },

    checkDeleteTableConfirm() {
      const input = document.getElementById('delete-table-confirm-input');
      const val = (input ? input.value : '').trim();
      const btn = document.getElementById('delete-table-submit-btn');
      const match = val.toLowerCase() === this.deleteTable.name.toLowerCase();
      if (btn) {
        btn.disabled = !match;
        btn.classList.toggle('opacity-50', !match);
        btn.classList.toggle('cursor-not-allowed', !match);
      }
    },

    async submitDeleteTable() {
      const btn = document.getElementById('delete-table-submit-btn');
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = 'Deleting...';
      }

      try {
        const resp = await fetch(`/api/catalog/${this.deleteTable.catId}/schemas/${this.deleteTable.schId}/tables/${this.deleteTable.tableId}`, {
          method: 'DELETE',
          credentials: 'include'
        });
        const res = await resp.json();
        if (!resp.ok || !res.ok) {
          if (window.showCatalogToast) window.showCatalogToast(res.error || res.detail || 'Failed to delete table', 'error');
          if (btn) {
            btn.disabled = false;
            btn.innerHTML = 'Delete Table';
          }
          return;
        }
        if (window.showCatalogToast) window.showCatalogToast(`Table '${this.deleteTable.name}' moved to trash (7-day UNDROP recovery available)`, 'success');
        this.closeDeleteTableModal();
        if (window.closePanel) window.closePanel();
        if (window.initTrashBadge) window.initTrashBadge();
        setTimeout(() => window.location.reload(), 1000);
      } catch (err) {
        if (window.showCatalogToast) window.showCatalogToast(`Delete failed: ${err.message}`, 'error');
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = 'Delete Table';
        }
      }
    },

    // --- Delete Schema Modal ---
    async openDeleteSchemaModal(catId, schId, slug, name, childCount) {
      this.closeAllDropdowns();
      this.deleteSchema = {
        catId: catId,
        schId: schId,
        slug: slug,
        name: name || slug,
        childCount: childCount || 0,
        qual: `schema: ${slug}`,
        confirmInput: '',
        cascade: false,
        dependencies: [],
        isSubmitting: false
      };

      const modal = document.getElementById('delete-schema-modal');
      const qual = document.getElementById('delete-schema-qualified-name');
      const targetLabel = document.getElementById('delete-schema-confirm-target');
      const input = document.getElementById('delete-schema-confirm-input');
      const cascadeBox = document.getElementById('delete-schema-cascade-box');
      const cascadeCb = document.getElementById('delete-schema-cascade-cb');
      const childMsg = document.getElementById('delete-schema-child-msg');
      const submitBtn = document.getElementById('delete-schema-submit-btn');
      const depsContainer = document.getElementById('delete-schema-deps-container');
      const depsList = document.getElementById('delete-schema-deps-list');

      if (qual) qual.textContent = this.deleteSchema.qual;
      if (targetLabel) targetLabel.textContent = slug;
      if (input) input.value = '';
      if (cascadeCb) cascadeCb.checked = false;
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.classList.add('opacity-50', 'cursor-not-allowed');
      }

      if (childCount > 0) {
        if (cascadeBox) cascadeBox.classList.remove('hidden');
        if (childMsg) childMsg.textContent = `Contains ${childCount} table(s). Without CASCADE, dropping non-empty schemas is blocked.`;
      } else {
        if (cascadeBox) cascadeBox.classList.add('hidden');
      }

      if (depsContainer) depsContainer.classList.add('hidden');
      if (depsList) depsList.innerHTML = '';
      if (modal) modal.classList.remove('hidden');

      try {
        const dResp = await fetch(`/api/catalog/${catId}/schemas/${schId}/dependencies`, { credentials: 'include' });
        if (dResp.ok) {
          const dData = await dResp.json();
          if (dData.has_dependencies && dData.dependencies && dData.dependencies.length > 0) {
            this.deleteSchema.dependencies = dData.dependencies;
            if (depsContainer) depsContainer.classList.remove('hidden');
            if (depsList) depsList.innerHTML = dData.dependencies.map(d => `<li>${d.type.toUpperCase()}: ${d.name} ${d.match_snippet ? `— "${d.match_snippet}"` : ''}</li>`).join('');
          }
        }
      } catch (err) {
        console.debug('Failed loading schema dependencies', err);
      }
    },

    closeDeleteSchemaModal() {
      const modal = document.getElementById('delete-schema-modal');
      if (modal) modal.classList.add('hidden');
    },

    checkDeleteSchemaConfirm() {
      const input = document.getElementById('delete-schema-confirm-input');
      const val = (input ? input.value : '').trim();
      const btn = document.getElementById('delete-schema-submit-btn');
      const cascadeCb = document.getElementById('delete-schema-cascade-cb');

      const nameMatch = (
        val.toLowerCase() === this.deleteSchema.slug.toLowerCase() ||
        val.toLowerCase() === this.deleteSchema.name.toLowerCase()
      );
      const cascadeOk = this.deleteSchema.childCount === 0 || (cascadeCb && cascadeCb.checked);

      const valid = nameMatch && cascadeOk;
      if (btn) {
        btn.disabled = !valid;
        btn.classList.toggle('opacity-50', !valid);
        btn.classList.toggle('cursor-not-allowed', !valid);
      }
    },

    async submitDeleteSchema() {
      const btn = document.getElementById('delete-schema-submit-btn');
      const cascadeCb = document.getElementById('delete-schema-cascade-cb');
      const cascade = cascadeCb ? cascadeCb.checked : false;

      if (btn) {
        btn.disabled = true;
        btn.innerHTML = 'Deleting...';
      }

      try {
        const resp = await fetch(`/api/catalog/${this.deleteSchema.catId}/schemas/${this.deleteSchema.schId}?cascade=${cascade ? 'true' : 'false'}`, {
          method: 'DELETE',
          credentials: 'include'
        });
        const res = await resp.json();
        if (!resp.ok || !res.ok) {
          if (window.showCatalogToast) window.showCatalogToast(res.error || res.detail || 'Failed to delete schema', 'error');
          if (btn) {
            btn.disabled = false;
            btn.innerHTML = 'Delete Schema';
          }
          return;
        }
        if (window.showCatalogToast) window.showCatalogToast(`Schema '${this.deleteSchema.slug}' moved to trash`, 'success');
        this.closeDeleteSchemaModal();
        if (window.initTrashBadge) window.initTrashBadge();
        setTimeout(() => window.location.reload(), 1000);
      } catch (err) {
        if (window.showCatalogToast) window.showCatalogToast(`Delete failed: ${err.message}`, 'error');
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = 'Delete Schema';
        }
      }
    }
  }));

  // ── 4. Inline Description Editing & Drag-and-Drop Reorder Manager ──────────
  Alpine.data('catalogReorderManager', (workspaceId = '') => ({
    workspaceId: workspaceId || window.CURRENT_WORKSPACE_ID || '',
    editingCatId: null,
    draggedCard: null,
    wasDragging: false,

    init() {
      window.startCatalogDescEdit = (catId) => this.startEdit(catId);
      window.cancelCatalogDescEdit = (catId) => this.cancelEdit(catId);
      window.handleCatalogDescKeyDown = (e, catId) => this.handleKeyDown(e, catId);
      window.saveCatalogDesc = (catId) => this.saveEdit(catId);
      window.persistCatalogOrder = () => this.persistOrder();
      window.initCatalogDragAndDrop = () => this.initDragAndDrop();

      this.initDragAndDrop();
    },

    startEdit(catId) {
      this.editingCatId = catId;
      const view = document.getElementById('desc-view-' + catId);
      const edit = document.getElementById('desc-edit-' + catId);
      const input = document.getElementById('desc-input-' + catId);
      const text = document.getElementById('desc-text-' + catId);
      if (!view || !edit || !input) return;

      const currentDesc = text.getAttribute('data-prev-desc') || (text.textContent.trim() === 'No description' ? '' : text.textContent.trim());
      input.value = currentDesc;
      view.classList.add('hidden');
      edit.classList.remove('hidden');
      input.focus();
      input.select();
    },

    cancelEdit(catId) {
      this.editingCatId = null;
      const view = document.getElementById('desc-view-' + catId);
      const edit = document.getElementById('desc-edit-' + catId);
      const input = document.getElementById('desc-input-' + catId);
      const text = document.getElementById('desc-text-' + catId);
      if (!view || !edit) return;

      const prev = text.getAttribute('data-prev-desc') || '';
      if (input) input.value = prev;
      edit.classList.add('hidden');
      view.classList.remove('hidden');
    },

    handleKeyDown(e, catId) {
      if (e.key === 'Enter') {
        e.preventDefault();
        e.stopPropagation();
        this.saveEdit(catId);
      } else if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        this.cancelEdit(catId);
      }
    },

    async saveEdit(catId) {
      const view = document.getElementById('desc-view-' + catId);
      const edit = document.getElementById('desc-edit-' + catId);
      const input = document.getElementById('desc-input-' + catId);
      const text = document.getElementById('desc-text-' + catId);
      if (!input || !text) return;

      const newVal = input.value.trim();
      const oldVal = text.getAttribute('data-prev-desc') || (text.textContent.trim() === 'No description' ? '' : text.textContent.trim());

      // Optimistic update
      text.setAttribute('data-prev-desc', newVal);
      if (newVal) {
        text.textContent = newVal;
        text.className = 'text-xs truncate cursor-pointer hover:text-cyan-300 transition-colors text-gray-400';
      } else {
        text.textContent = 'No description';
        text.className = 'text-xs truncate cursor-pointer hover:text-cyan-300 transition-colors text-gray-500 italic';
      }
      if (edit) edit.classList.add('hidden');
      if (view) view.classList.remove('hidden');
      this.editingCatId = null;

      try {
        let resp = await fetch(`/api/catalogs/${catId}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify({ description: newVal })
        });
        if (!resp.ok) {
          resp = await fetch(`/api/catalog/${catId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ description: newVal })
          });
        }
        if (!resp.ok) {
          const errData = await resp.json().catch(() => ({}));
          throw new Error(errData.error || errData.detail || 'Failed to update catalog description');
        }
        if (window.showCatalogToast) window.showCatalogToast('Catalog description updated', 'success');
      } catch (err) {
        text.setAttribute('data-prev-desc', oldVal);
        if (oldVal) {
          text.textContent = oldVal;
          text.className = 'text-xs truncate cursor-pointer hover:text-cyan-300 transition-colors text-gray-400';
        } else {
          text.textContent = 'No description';
          text.className = 'text-xs truncate cursor-pointer hover:text-cyan-300 transition-colors text-gray-500 italic';
        }
        if (window.showCatalogToast) window.showCatalogToast(err.message || 'Error updating description', 'error');
      }
    },

    clearDropIndicators() {
      document.querySelectorAll('.catalog-drop-target-top, .catalog-drop-target-bottom').forEach(el => {
        el.classList.remove('catalog-drop-target-top', 'catalog-drop-target-bottom');
      });
    },

    initDragAndDrop() {
      const headers = document.querySelectorAll('.catalog-header[draggable="true"]');
      headers.forEach(header => {
        header.addEventListener('dragstart', (e) => {
          const card = header.closest('.native-catalog-card, .shared-catalog-card');
          if (!card) return;
          this.wasDragging = true;
          this.draggedCard = card;
          card.classList.add('is-dragging');
          const catId = card.getAttribute('data-catalog-id');
          const section = card.getAttribute('data-section');
          e.dataTransfer.effectAllowed = 'move';
          e.dataTransfer.setData('text/plain', JSON.stringify({ id: catId, section: section }));
        });

        header.addEventListener('dragend', () => {
          if (this.draggedCard) {
            this.draggedCard.classList.remove('is-dragging');
          }
          this.clearDropIndicators();
          this.draggedCard = null;
          setTimeout(() => { this.wasDragging = false; }, 100);
        });
      });

      const cards = document.querySelectorAll('.native-catalog-card, .shared-catalog-card');
      cards.forEach(card => {
        card.addEventListener('dragover', (e) => {
          e.preventDefault();
          if (!this.draggedCard || this.draggedCard === card) return;

          const targetSection = card.getAttribute('data-section');
          const draggedSection = this.draggedCard.getAttribute('data-section');

          if (targetSection !== draggedSection) {
            e.dataTransfer.dropEffect = 'none';
            this.clearDropIndicators();
            return;
          }

          if (draggedSection === 'shared') {
            const targetWs = card.getAttribute('data-shared-workspace-id');
            const draggedWs = this.draggedCard.getAttribute('data-shared-workspace-id');
            if (targetWs && draggedWs && targetWs !== draggedWs) {
              e.dataTransfer.dropEffect = 'none';
              this.clearDropIndicators();
              return;
            }
          }

          e.dataTransfer.dropEffect = 'move';
          const rect = card.getBoundingClientRect();
          const isTop = (e.clientY - rect.top) < (rect.height / 2);

          this.clearDropIndicators();
          if (isTop) {
            card.classList.add('catalog-drop-target-top');
          } else {
            card.classList.add('catalog-drop-target-bottom');
          }
        });

        card.addEventListener('dragleave', (e) => {
          if (!card.contains(e.relatedTarget)) {
            card.classList.remove('catalog-drop-target-top', 'catalog-drop-target-bottom');
          }
        });

        card.addEventListener('drop', (e) => {
          e.preventDefault();
          this.clearDropIndicators();
          if (!this.draggedCard || this.draggedCard === card) return;

          const targetSection = card.getAttribute('data-section');
          const draggedSection = this.draggedCard.getAttribute('data-section');
          if (targetSection !== draggedSection) return;

          if (draggedSection === 'shared') {
            const targetWs = card.getAttribute('data-shared-workspace-id');
            const draggedWs = this.draggedCard.getAttribute('data-shared-workspace-id');
            if (targetWs && draggedWs && targetWs !== draggedWs) return;
          }

          const rect = card.getBoundingClientRect();
          const isTop = (e.clientY - rect.top) < (rect.height / 2);

          const parent = card.parentNode;
          if (isTop) {
            parent.insertBefore(this.draggedCard, card);
          } else {
            parent.insertBefore(this.draggedCard, card.nextSibling);
          }

          this.persistOrder();
        });
      });
    },

    async persistOrder() {
      const wsId = this.workspaceId || window.CURRENT_WORKSPACE_ID;
      if (!wsId) return;

      const nativeIds = Array.from(document.querySelectorAll('#catalog-list .native-catalog-card'))
        .map(el => el.getAttribute('data-catalog-id'))
        .filter(Boolean);

      const sharedIds = Array.from(document.querySelectorAll('.shared-catalog-card'))
        .map(el => el.getAttribute('data-catalog-id'))
        .filter(Boolean);

      try {
        const resp = await fetch(`/api/workspaces/${wsId}/user-preferences/catalog-order`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify({
            native_catalog_order: nativeIds,
            shared_catalog_order: sharedIds
          })
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          throw new Error(err.error || err.detail || 'Failed to persist catalog order');
        }
        if (window.showCatalogToast) window.showCatalogToast('Catalog order saved', 'success');
      } catch (err) {
        if (window.showCatalogToast) window.showCatalogToast(err.message || 'Error saving catalog order', 'error');
      }
    }
  }));

});
