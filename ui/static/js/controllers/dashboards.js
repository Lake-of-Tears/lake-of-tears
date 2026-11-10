/**
 * Lake of Tears - Dashboards Controller (Alpine.js)
 * Declarative state management for:
 * 1. Create Dashboard modal (title, description, dataset select, slug)
 * 2. Delete Dashboard modal with confirmation
 * 3. Gallery search and status filtering (all, active, archived)
 */

document.addEventListener('alpine:init', () => {

  Alpine.data('dashboardsGalleryManager', () => ({
    currentFilter: 'all',
    searchQuery: '',

    // Create Modal
    createModalOpen: false,
    newDashboard: {
      title: '',
      description: '',
      dataset: '',
      slug: '',
      isSubmitting: false,
      error: null
    },

    // Delete Modal
    deleteModalOpen: false,
    deleteTarget: {
      id: null,
      title: '',
      isSubmitting: false
    },

    init() {
      const origSetFilterStatus = typeof window.setFilterStatus === 'function' ? window.setFilterStatus : null;
      const origHandleFilter = typeof window.handleFilter === 'function' ? window.handleFilter : null;
      this._origSetFilterStatus = origSetFilterStatus;
      this._origHandleFilter = origHandleFilter;

      // Expose globally for legacy bridges, external inline handlers, and test assertions
      window.openCreateModal = () => this.openCreateModal();
      window.closeCreateModal = () => this.closeCreateModal();
      window.handleCreateDashboard = (e) => this.handleCreateDashboard(e);
      window.confirmDelete = (id, title) => this.confirmDelete(id, title);
      window.closeDeleteModal = () => this.closeDeleteModal();
      window.executeDelete = () => this.executeDelete();
      window.setFilterStatus = (status) => this.setFilterStatus(status);
      window.handleFilter = () => this.handleFilter();

      // Keyboard shortcuts
      window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          if (this.createModalOpen) this.closeCreateModal();
          if (this.deleteModalOpen) this.closeDeleteModal();
        }
      });
    },

    openCreateModal() {
      this.createModalOpen = true;
      this.newDashboard = {
        title: '',
        description: '',
        dataset: '',
        slug: '',
        isSubmitting: false,
        error: null
      };
      const modal = document.getElementById('create-modal');
      const form = document.getElementById('form-create-dashboard');
      if (form && typeof form.reset === 'function') form.reset();
      if (modal) modal.classList.remove('hidden');
      const titleInput = document.getElementById('create-title');
      if (titleInput && typeof titleInput.focus === 'function') titleInput.focus();
    },

    closeCreateModal() {
      this.createModalOpen = false;
      const modal = document.getElementById('create-modal');
      if (modal) modal.classList.add('hidden');
    },

    async handleCreateDashboard(e) {
      if (e && e.preventDefault) e.preventDefault();
      const submitBtn = document.getElementById('btn-submit-create');

      const titleEl = document.getElementById('create-title');
      const descEl = document.getElementById('create-desc');
      const pubEl = document.getElementById('create-published');
      const selectedTemplate = document.querySelector('input[name="template-choice"]:checked')?.value || 'blank';

      const title = (titleEl ? titleEl.value : '') || '';
      const description = (descEl ? descEl.value : '') || '';
      const isPublished = pubEl ? Boolean(pubEl.checked) : true;
      const dataset = this.newDashboard.dataset || '';

      if (!title.trim()) {
        if (window.showToast) window.showToast('Dashboard title is required', 'error');
        return;
      }

      this.newDashboard.isSubmitting = true;
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span>Creating...</span>';
      }

      try {
        const wsSlug = (typeof currentWorkspaceSlug !== 'undefined' && currentWorkspaceSlug) ? currentWorkspaceSlug : '';
        const wsQuery = wsSlug ? `?workspace_slug=${encodeURIComponent(wsSlug)}` : '';
        const payload = {
          title: title.trim(),
          description: description.trim() || null,
          is_published: isPublished,
          template: selectedTemplate,
          dataset_id: dataset || null
        };
        const resp = await fetch(`/api/dashboards${wsQuery}`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const res = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          throw new Error(res.error || res.detail || 'Failed to create dashboard');
        }
        if (window.showToast) window.showToast(`Dashboard "${title.trim()}" created successfully!`, 'success');
        this.closeCreateModal();
        if (res.id) {
          window.location.href = `/dashboards/${res.id}`;
        } else if (typeof window.loadDashboardGallery === 'function') {
          window.loadDashboardGallery();
        }
      } catch (err) {
        if (window.showToast) window.showToast(err.message || 'Connection error while creating dashboard', 'error');
      } finally {
        this.newDashboard.isSubmitting = false;
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.innerHTML = '<span>Create Dashboard</span>';
        }
      }
    },

    confirmDelete(id, title) {
      this.deleteTarget = { id: id, title: title, isSubmitting: false };
      if (typeof pendingDeleteId !== 'undefined') {
        pendingDeleteId = id;
      }
      this.deleteModalOpen = true;
      const modal = document.getElementById('delete-modal');
      const titleEl = document.getElementById('delete-modal-title');
      if (titleEl) titleEl.textContent = title;
      if (modal) modal.classList.remove('hidden');
    },

    closeDeleteModal() {
      this.deleteModalOpen = false;
      this.deleteTarget = { id: null, title: '', isSubmitting: false };
      if (typeof pendingDeleteId !== 'undefined') {
        pendingDeleteId = null;
      }
      const modal = document.getElementById('delete-modal');
      if (modal) modal.classList.add('hidden');
    },

    async executeDelete() {
      const targetId = this.deleteTarget.id || (typeof pendingDeleteId !== 'undefined' ? pendingDeleteId : null);
      if (!targetId) return;
      this.deleteTarget.isSubmitting = true;
      const btn = document.getElementById('btn-confirm-delete');
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = 'Deleting...';
      }

      try {
        const wsSlug = (typeof currentWorkspaceSlug !== 'undefined' && currentWorkspaceSlug) ? currentWorkspaceSlug : '';
        const wsQuery = wsSlug ? `?workspace_slug=${encodeURIComponent(wsSlug)}` : '';
        const resp = await fetch(`/api/dashboards/${targetId}${wsQuery}`, {
          method: 'DELETE',
          credentials: 'include'
        });
        const res = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          throw new Error(res.error || res.detail || 'Failed to delete dashboard');
        }
        if (window.showToast) window.showToast('Dashboard deleted', 'success');
        this.closeDeleteModal();
        if (typeof window.loadDashboardGallery === 'function') {
          window.loadDashboardGallery();
        }
      } catch (err) {
        if (window.showToast) window.showToast(`Delete failed: ${err.message}`, 'error');
      } finally {
        this.deleteTarget.isSubmitting = false;
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = 'Delete Dashboard';
        }
      }
    },

    setFilterStatus(status) {
      this.currentFilter = status;
      if (typeof currentFilter !== 'undefined') {
        currentFilter = status;
      }
      ['all', 'published', 'draft'].forEach(s => {
        const btn = document.getElementById(`filter-${s}`);
        if (!btn) return;
        if (s === status) {
          btn.className = 'px-2.5 py-1 rounded text-xs font-medium transition-colors bg-white/10 text-white';
        } else {
          btn.className = 'px-2.5 py-1 rounded text-xs font-medium transition-colors text-gray-400 hover:text-white';
        }
      });
      if (typeof window.renderDashboards === 'function') {
        window.renderDashboards();
      } else if (typeof this._origSetFilterStatus === 'function') {
        this._origSetFilterStatus(status);
      }
    },

    handleFilter() {
      if (typeof window.renderDashboards === 'function') {
        window.renderDashboards();
      } else if (typeof this._origHandleFilter === 'function') {
        this._origHandleFilter();
      }
    }
  }));

});
