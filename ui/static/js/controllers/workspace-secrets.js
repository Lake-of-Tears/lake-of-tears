/**
 * Lake of Tears - Workspace Secret Scopes Controller (Alpine.js)
 * Declarative state management for Databricks-compatible Secret Scopes,
 * Manage Keys slide-over drawer, and granular Scope ACLs modal.
 */
document.addEventListener('alpine:init', () => {
  Alpine.data('workspaceSecretsManager', (workspaceId = '') => ({
    workspaceId: workspaceId || window._activeWsId || '',

    createScope: {
      isOpen: false,
      name: '',
      backendType: 'internal',

      open() {
        this.isOpen = true;
        this.name = '';
        this.backendType = 'internal';
        const modal = document.getElementById('create-scope-modal');
        if (modal) modal.classList.remove('hidden');
      },

      close() {
        this.isOpen = false;
        const modal = document.getElementById('create-scope-modal');
        if (modal) modal.classList.add('hidden');
      }
    },

    keysDrawer: {
      isOpen: false,
      scopeId: null,
      scopeName: '',
      backendType: 'internal',
      permission: 'READ',
      keys: [],
      isLoading: false,
      newKeyName: '',
      newKeyValue: '',
      isMasked: true,
      isSubmitting: false,
      errorMessage: null,
      successMessage: null,

      open(scopeId, scopeName, backendType, permission) {
        this.scopeId = scopeId;
        this.scopeName = scopeName;
        this.backendType = backendType || 'internal';
        this.permission = permission || 'READ';
        this.newKeyName = '';
        this.newKeyValue = '';
        this.isMasked = true;
        this.errorMessage = null;
        this.successMessage = null;
        this.isOpen = true;

        window._activeScopeId = scopeId;
        window._activeScopeName = scopeName;
        window._activePermission = permission;

        // Legacy DOM element updates for backwards compatibility
        const modal = document.getElementById('keys-modal');
        if (modal) modal.classList.remove('hidden');

        const nameEl = document.getElementById('keys-modal-scope-name');
        if (nameEl) nameEl.textContent = scopeName;

        const badge = document.getElementById('keys-modal-backend-badge');
        if (badge) {
          if (this.backendType.includes('azure')) {
            badge.textContent = 'Azure Key Vault';
            badge.className = 'inline-flex items-center px-1.5 py-0.5 rounded text-[10px] bg-sky-950/60 text-sky-300 border border-sky-800/40';
          } else if (this.backendType.includes('aws')) {
            badge.textContent = 'AWS Secrets Manager';
            badge.className = 'inline-flex items-center px-1.5 py-0.5 rounded text-[10px] bg-amber-950/60 text-amber-300 border border-amber-800/40';
          } else if (this.backendType.includes('hashicorp') || this.backendType.includes('vault')) {
            badge.textContent = 'HashiCorp Vault';
            badge.className = 'inline-flex items-center px-1.5 py-0.5 rounded text-[10px] bg-purple-950/60 text-purple-300 border border-purple-800/40';
          } else if (this.backendType.includes('gcp') || this.backendType.includes('google')) {
            badge.textContent = 'GCP Secret Manager';
            badge.className = 'inline-flex items-center px-1.5 py-0.5 rounded text-[10px] bg-teal-950/60 text-teal-300 border border-teal-800/40';
          } else {
            badge.textContent = 'Internal DB';
            badge.className = 'inline-flex items-center px-1.5 py-0.5 rounded text-[10px] bg-gray-800 text-gray-300 border border-gray-700';
          }
        }

        const addSection = document.getElementById('keys-modal-add-section');
        if (addSection) {
          if (permission === 'READ') {
            addSection.classList.add('hidden');
          } else {
            addSection.classList.remove('hidden');
          }
        }

        const errBox = document.getElementById('new-key-error');
        const succBox = document.getElementById('new-key-success');
        if (errBox) errBox.classList.add('hidden');
        if (succBox) succBox.classList.add('hidden');

        this.fetchKeys();
      },

      close() {
        this.isOpen = false;
        this.errorMessage = null;
        this.successMessage = null;
        const modal = document.getElementById('keys-modal');
        if (modal) modal.classList.add('hidden');
      },

      async fetchKeys() {
        if (!this.scopeId) return;
        this.isLoading = true;
        this.errorMessage = null;

        const container = document.getElementById('keys-list-container');
        const countLabel = document.getElementById('keys-count-label');

        try {
          const wsId = this.$data?.workspaceId || window._activeWsId;
          const resp = await fetch(`/api/workspaces/${wsId}/secrets/scopes/${this.scopeId}/keys`);
          if (!resp.ok) {
            throw new Error('Failed to load keys (HTTP ' + resp.status + ')');
          }
          this.keys = await resp.json();
          const countText = `${this.keys.length} key${this.keys.length === 1 ? '' : 's'}`;
          if (countLabel) countLabel.textContent = countText;

          // Real-time dynamic sync: update the scope row key counter in the workspace table
          const scopeCountEl = document.getElementById(`scope-key-count-${this.scopeId}`) ||
                               document.querySelector(`[data-scope-id="${this.scopeId}"]`) ||
                               document.querySelector(`[data-scope-name="${this.scopeName}"]`);
          if (scopeCountEl) {
            scopeCountEl.textContent = countText;
          }

          if (container) {
            if (!this.keys.length) {
              container.innerHTML = '<p class="text-xs text-gray-500 italic py-2">No keys stored in this scope yet.</p>';
            } else {
              const canWrite = this.permission === 'WRITE' || this.permission === 'MANAGE';
              container.innerHTML = this.keys.map(k => {
                const dateStr = k.updated_at ? k.updated_at.slice(0, 10) : '';
                return `
                  <div class="flex items-center justify-between p-2.5 rounded bg-white/[0.02] border border-white/[0.04]">
                    <div class="min-w-0">
                      <span class="font-mono text-xs text-gray-200">${k.key}</span>
                      <div class="text-[10px] text-gray-500">Version: ${k.version || 1} ${dateStr ? '· Updated: ' + dateStr : ''}</div>
                    </div>
                    ${canWrite ? `
                      <button type="button" onclick="deleteKey('${k.key}')" class="text-xs text-gray-500 hover:text-red-400 transition-colors p-1" title="Delete Key">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="1.75" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0"/></svg>
                      </button>
                    ` : ''}
                  </div>
                `;
              }).join('');
            }
          }
        } catch (err) {
          this.errorMessage = err.message || 'Failed to load keys';
          if (container) {
            container.innerHTML = `<p class="text-xs text-red-400 py-2">Connection error: ${err.message}</p>`;
          }
          if (countLabel) countLabel.textContent = '0 keys';
        } finally {
          this.isLoading = false;
        }
      },

      async submitKey() {
        const keyNameInput = document.getElementById('new-key-name');
        const keyValueInput = document.getElementById('new-key-value');
        const keyName = (this.newKeyName || (keyNameInput ? keyNameInput.value : '')).trim();
        const keyValue = this.newKeyValue || (keyValueInput ? keyValueInput.value : '');

        if (!keyName || !keyValue) return;

        this.isSubmitting = true;
        this.errorMessage = null;
        this.successMessage = null;

        const btn = document.getElementById('new-key-btn');
        const errBox = document.getElementById('new-key-error');
        const succBox = document.getElementById('new-key-success');
        if (errBox) errBox.classList.add('hidden');
        if (succBox) succBox.classList.add('hidden');
        if (btn) {
          btn.disabled = true;
          btn.textContent = 'Saving...';
        }

        try {
          const wsId = this.$data?.workspaceId || window._activeWsId;
          const resp = await fetch(`/api/workspaces/${wsId}/secrets/scopes/${this.scopeId}/keys`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: keyName, value: keyValue })
          });
          const data = await resp.json();
          if (!resp.ok) {
            this.errorMessage = data.detail || 'Failed to save secret';
            if (errBox) {
              errBox.textContent = this.errorMessage;
              errBox.classList.remove('hidden');
            }
          } else {
            this.newKeyName = '';
            this.newKeyValue = '';
            if (keyNameInput) keyNameInput.value = '';
            if (keyValueInput) keyValueInput.value = '';

            this.successMessage = `✓ Key '${keyName}' saved successfully.`;
            if (succBox) {
              succBox.textContent = this.successMessage;
              succBox.classList.remove('hidden');
              setTimeout(() => { if (succBox) succBox.classList.add('hidden'); }, 3500);
            }
            await this.fetchKeys();
          }
        } catch (err) {
          this.errorMessage = 'Could not reach server: ' + err.message;
          if (errBox) {
            errBox.textContent = this.errorMessage;
            errBox.classList.remove('hidden');
          }
        } finally {
          this.isSubmitting = false;
          if (btn) {
            btn.disabled = false;
            btn.textContent = 'Save Secret';
          }
        }
      },

      async deleteKey(keyName) {
        if (!confirm(`Delete secret key '${keyName}'? Applications referencing this key will fail to resolve it.`)) {
          return;
        }
        const succBox = document.getElementById('new-key-success');
        try {
          const wsId = this.$data?.workspaceId || window._activeWsId;
          const resp = await fetch(`/api/workspaces/${wsId}/secrets/scopes/${this.scopeId}/keys/${keyName}`, {
            method: 'DELETE'
          });
          if (resp.ok) {
            this.successMessage = `✓ Key '${keyName}' deleted.`;
            if (succBox) {
              succBox.textContent = this.successMessage;
              succBox.classList.remove('hidden');
              setTimeout(() => { if (succBox) succBox.classList.add('hidden'); }, 3500);
            }
            await this.fetchKeys();
          } else {
            alert('Failed to delete key.');
          }
        } catch (err) {
          alert('Error deleting key: ' + err.message);
        }
      }
    },

    aclModal: {
      isOpen: false,
      scopeId: null,
      scopeName: '',
      acls: [],
      isLoading: false,
      principalType: 'user',
      principalIdUser: '',
      principalIdRole: 'user',
      permission: 'READ',
      isSubmitting: false,
      errorMessage: null,

      open(scopeId, scopeName) {
        this.scopeId = scopeId;
        this.scopeName = scopeName;
        this.errorMessage = null;
        this.isOpen = true;

        window._activeScopeId = scopeId;
        window._activeScopeName = scopeName;

        const nameEl = document.getElementById('acl-modal-scope-name');
        if (nameEl) nameEl.textContent = scopeName;
        const errBox = document.getElementById('new-acl-error');
        if (errBox) errBox.classList.add('hidden');
        const modal = document.getElementById('acl-modal');
        if (modal) modal.classList.remove('hidden');

        this.fetchAcls();
      },

      close() {
        this.isOpen = false;
        this.errorMessage = null;
        const modal = document.getElementById('acl-modal');
        if (modal) modal.classList.add('hidden');
      },

      async fetchAcls() {
        if (!this.scopeId) return;
        this.isLoading = true;
        this.errorMessage = null;

        const container = document.getElementById('acl-list-container');
        try {
          const wsId = this.$data?.workspaceId || window._activeWsId;
          const resp = await fetch(`/api/workspaces/${wsId}/secrets/scopes/${this.scopeId}/acls`);
          if (!resp.ok) {
            throw new Error('Failed to load permissions (HTTP ' + resp.status + ')');
          }
          this.acls = await resp.json();
          if (container) {
            if (!this.acls.length) {
              container.innerHTML = '<p class="text-xs text-gray-500 italic py-2">No custom permissions granted yet. Scope creator and Superadmins retain full access.</p>';
            } else {
              container.innerHTML = this.acls.map(a => {
                let permClass = 'text-gray-400 bg-gray-800 border-gray-700';
                if (a.permission === 'MANAGE') permClass = 'text-emerald-400 bg-emerald-950/40 border-emerald-800/40';
                else if (a.permission === 'WRITE') permClass = 'text-blue-400 bg-blue-950/40 border-blue-800/40';

                return `
                  <div class="flex items-center justify-between p-2.5 rounded bg-white/[0.02] border border-white/[0.04]">
                    <div class="min-w-0">
                      <div class="flex items-center gap-1.5">
                        <span class="text-xs text-gray-200">${a.principal_display || a.principal_id}</span>
                        <span class="text-[10px] text-gray-500 uppercase">(${a.principal_type})</span>
                      </div>
                      <div class="mt-0.5">
                        <span class="inline-flex items-center px-1.5 py-0.2 rounded text-[9px] font-mono border ${permClass}">
                          ${a.permission}
                        </span>
                      </div>
                    </div>
                    <button type="button" onclick="deleteAcl('${a.id}')" class="text-xs text-gray-500 hover:text-red-400 transition-colors p-1" title="Revoke Permission">
                      <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="1.75" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0"/></svg>
                    </button>
                  </div>
                `;
              }).join('');
            }
          }
        } catch (err) {
          this.errorMessage = err.message || 'Failed to load permissions';
          if (container) {
            container.innerHTML = `<p class="text-xs text-red-400 py-2">Connection error: ${err.message}</p>`;
          }
        } finally {
          this.isLoading = false;
        }
      },

      async submitAcl() {
        const pTypeEl = document.getElementById('acl-principal-type');
        const pType = pTypeEl ? pTypeEl.value : this.principalType;
        const pId = pType === 'user'
          ? (document.getElementById('acl-principal-id-user')?.value || this.principalIdUser)
          : (pType === 'bundle'
              ? (document.getElementById('acl-principal-id-bundle')?.value || '').trim()
              : (document.getElementById('acl-principal-id-role')?.value || this.principalIdRole));
        const permEl = document.getElementById('acl-permission');
        const perm = permEl ? permEl.value : this.permission;

        if (!pId) return;

        this.isSubmitting = true;
        this.errorMessage = null;

        const btn = document.getElementById('new-acl-btn');
        const errBox = document.getElementById('new-acl-error');
        if (errBox) errBox.classList.add('hidden');
        if (btn) {
          btn.disabled = true;
          btn.textContent = 'Granting...';
        }

        try {
          const wsId = this.$data?.workspaceId || window._activeWsId;
          const resp = await fetch(`/api/workspaces/${wsId}/secrets/scopes/${this.scopeId}/acls`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ principal_type: pType, principal_id: pId, permission: perm })
          });
          const data = await resp.json();
          if (!resp.ok) {
            this.errorMessage = data.detail || 'Failed to grant permission';
            if (errBox) {
              errBox.textContent = this.errorMessage;
              errBox.classList.remove('hidden');
            }
          } else {
            await this.fetchAcls();
          }
        } catch (err) {
          this.errorMessage = 'Could not reach server: ' + err.message;
          if (errBox) {
            errBox.textContent = this.errorMessage;
            errBox.classList.remove('hidden');
          }
        } finally {
          this.isSubmitting = false;
          if (btn) {
            btn.disabled = false;
            btn.textContent = 'Grant Permission';
          }
        }
      },

      async revokeAcl(aclId) {
        if (!confirm('Revoke this scope permission?')) return;
        try {
          const wsId = this.$data?.workspaceId || window._activeWsId;
          const resp = await fetch(`/api/workspaces/${wsId}/secrets/scopes/${this.scopeId}/acls/${aclId}`, {
            method: 'DELETE'
          });
          if (resp.ok) {
            await this.fetchAcls();
          } else {
            alert('Failed to revoke permission.');
          }
        } catch (err) {
          alert('Error revoking permission: ' + err.message);
        }
      }
    },

    init() {
      // Expose globally for legacy inline calls and programmatic test verification
      window.openCreateScopeModal = () => this.createScope.open();
      window.closeCreateScopeModal = () => this.createScope.close();
      window.openKeysModal = (id, name, type, perm) => this.keysDrawer.open(id, name, type, perm);
      window.closeKeysModal = () => this.keysDrawer.close();
      window.fetchKeys = () => this.keysDrawer.fetchKeys();
      window.submitNewKey = (e) => {
        if (e && e.preventDefault) e.preventDefault();
        return this.keysDrawer.submitKey();
      };
      window.deleteKey = (keyName) => this.keysDrawer.deleteKey(keyName);
      window.toggleSecretVisibility = (inputId, btn) => {
        const el = document.getElementById(inputId);
        if (!el) return;
        if (el.type === 'password') {
          el.type = 'text';
          if (btn) btn.classList.add('text-blue-400');
        } else {
          el.type = 'password';
          if (btn) btn.classList.remove('text-blue-400');
        }
      };

      window.openAclModal = (id, name) => this.aclModal.open(id, name);
      window.closeAclModal = () => this.aclModal.close();
      window.fetchAcls = () => this.aclModal.fetchAcls();
      window.submitNewAcl = (e) => {
        if (e && e.preventDefault) e.preventDefault();
        return this.aclModal.submitAcl();
      };
      window.deleteAcl = (aclId) => this.aclModal.revokeAcl(aclId);
      window.aclPrincipalTypeChanged = () => {
        const pTypeEl = document.getElementById('acl-principal-type');
        const pType = pTypeEl ? pTypeEl.value : 'user';
        const userGroup = document.getElementById('acl-principal-id-user');
        const roleGroup = document.getElementById('acl-principal-id-role');
        const bundleGroup = document.getElementById('acl-principal-id-bundle');
        if (userGroup) userGroup.classList.toggle('hidden', pType !== 'user');
        if (roleGroup) roleGroup.classList.toggle('hidden', pType !== 'role');
        if (bundleGroup) bundleGroup.classList.toggle('hidden', pType !== 'bundle');
      };

      // Escape key closes modals
      window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          if (this.createScope.isOpen) this.createScope.close();
          if (this.keysDrawer.isOpen) this.keysDrawer.close();
          if (this.aclModal.isOpen) this.aclModal.close();
        }
      });
    }
  }));
});
