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
      const form = document.getElementById('create-dashboard-form');
      if (form) form.reset();
      if (modal) modal.classList.remove('hidden');
    },

    closeCreateModal() {
      this.createModalOpen = false;
      const modal = document.getElementById('create-modal');
      if (modal) modal.classList.add('hidden');
    },

    async handleCreateDashboard(e) {
      if (e && e.preventDefault) e.preventDefault();
      const form = document.getElementById('create-dashboard-form');
      const submitBtn = document.getElementById('btn-submit-create');
      if (!form) return;

      const title = (document.getElementById('dash-title') || {}).value || '';
      const description = (document.getElementById('dash-desc') || {}).value || '';
      const dataset = (document.getElementById('dash-dataset') || {}).value || '';

      if (!title.trim()) {
        if (window.showToast) window.showToast('Dashboard title is required', 'error');
        return;
      }

      this.newDashboard.isSubmitting = true;
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="animate-spin">⏳</span> Creating...';
      }

      try {
        const payload = {
          title: title.trim(),
          description: description.trim(),
          dataset_id: dataset || null
        };
        const resp = await fetch('/api/dashboards', {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const res = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          throw new Error(res.error || res.detail || 'Failed to create dashboard');
        }
        if (window.showToast) window.showToast('Dashboard created successfully', 'success');
        this.closeCreateModal();
        if (res.id) {
          window.location.href = `/dashboards/${res.id}`;
        } else if (typeof window.loadDashboardGallery === 'function') {
          window.loadDashboardGallery();
        }
      } catch (err) {
        if (window.showToast) window.showToast(err.message, 'error');
      } finally {
        this.newDashboard.isSubmitting = false;
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.innerHTML = 'Create Dashboard';
        }
      }
    },

    confirmDelete(id, title) {
      this.deleteTarget = { id: id, title: title, isSubmitting: false };
      this.deleteModalOpen = true;
      const modal = document.getElementById('delete-modal');
      const titleEl = document.getElementById('delete-modal-title');
      if (titleEl) titleEl.textContent = `Are you sure you want to delete "${title}"? This action cannot be undone.`;
      if (modal) modal.classList.remove('hidden');
    },

    closeDeleteModal() {
      this.deleteModalOpen = false;
      this.deleteTarget = { id: null, title: '', isSubmitting: false };
      const modal = document.getElementById('delete-modal');
      if (modal) modal.classList.add('hidden');
    },

    async executeDelete() {
      if (!this.deleteTarget.id) return;
      this.deleteTarget.isSubmitting = true;
      const btn = document.getElementById('btn-confirm-delete');
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = 'Deleting...';
      }

      try {
        const resp = await fetch(`/api/dashboards/${this.deleteTarget.id}`, {
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
          btn.innerHTML = 'Delete';
        }
      }
    },

    setFilterStatus(status) {
      this.currentFilter = status;
      if (typeof window.setFilterStatus === 'function' && window.setFilterStatus !== this.setFilterStatus) {
        window.setFilterStatus(status);
      }
    },

    handleFilter() {
      if (typeof window.handleFilter === 'function' && window.handleFilter !== this.handleFilter) {
        window.handleFilter();
      }
    }
  }));

});
