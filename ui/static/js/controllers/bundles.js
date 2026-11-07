/**
 * Lake of Tears - Bundles Controller (Alpine.js)
 * Declarative state management for:
 * 1. Bundles Gallery (filtering by status/target, search, card stats)
 * 2. Details & Topology slide-over drawer (resources tree, manifest viewer, live drift check, pipeline run trigger)
 * 3. Pre-Deploy Dry-Run modal (diff summary table, syntax check, DAG validation)
 * 4. Conversational AI Prompt-to-Bundle generator (natural language to compact TOON manifest)
 * 5. Teardown / Destroy modal with typed confirmation and cascade safeguards
 */

document.addEventListener('alpine:init', () => {

  Alpine.data('bundlesManager', () => ({
    workspaceSlug: window.CURRENT_WORKSPACE_SLUG || '',
    workspaceId: window.CURRENT_WORKSPACE_ID || '',
    myRole: window.USER_ROLE || 'member',

    // Gallery State
    bundles: [],
    totalBundles: 0,
    isLoading: false,
    searchQuery: '',
    statusFilter: 'all',
    targetFilter: 'all',

    // Slide-over Drawer State
    drawerOpen: false,
    selectedBundle: null,
    activeDrawerTab: 'overview', // 'overview' | 'resources' | 'manifest' | 'topology'
    manifestFormatTab: 'toon',  // 'toon' | 'yaml' | 'json'
    manifestFormatted: '',
    driftReport: null,
    isRunningPipeline: false,
    activeRunResult: null,

    // Pre-Deploy / Dry-Run Modal
    deployModalOpen: false,
    deployManifest: '',
    deployFormat: 'toon',
    deployTarget: 'dev',
    dryRunResult: null,
    isValidating: false,
    isDeploying: false,
    deploySuccessMessage: null,
    deployErrorMessage: null,

    // Conversational AI Generator Modal
    aiModalOpen: false,
    aiPrompt: '',
    isGeneratingAI: false,
    generatedManifest: '',
    aiError: null,

    // Destroy Modal
    destroyModalOpen: false,
    destroyTarget: null,
    destroyConfirmInput: '',
    purgeStorageCheck: false,
    isDestroying: false,
    destroyError: null,

    init() {
      // Expose globally for legacy bridges, external inline handlers, and test assertions
      window.bundlesManagerInstance = this;
      window.openBundleDrawer = (id) => this.openDrawer(id);
      window.closeBundleDrawer = () => this.closeDrawer();
      window.openDeployModal = () => this.openDeployModal();
      window.closeDeployModal = () => this.closeDeployModal();
      window.openAIModal = () => this.openAIModal();
      window.closeAIModal = () => this.closeAIModal();
      window.openDestroyModal = (bundle) => this.openDestroyModal(bundle);
      window.closeDestroyModal = () => this.closeDestroyModal();
      window.validateBundleManifest = () => this.validateManifest();
      window.executeBundleDeploy = () => this.executeDeploy();
      window.generateBundleAI = () => this.generateWithAI();
      window.triggerBundlePipeline = (id, pipe) => this.triggerPipeline(id, pipe);

      // Keyboard shortcuts
      window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          if (this.drawerOpen) this.closeDrawer();
          if (this.deployModalOpen) this.closeDeployModal();
          if (this.aiModalOpen) this.closeAIModal();
          if (this.destroyModalOpen) this.closeDestroyModal();
        }
      });

      // Initial load
      this.fetchBundles();
    },

    // ── Gallery Actions ────────────────────────────────────────────────────────

    async fetchBundles() {
      this.isLoading = true;
      try {
        const queryParams = new URLSearchParams();
        if (this.workspaceSlug) queryParams.set('workspace_slug', this.workspaceSlug);
        if (this.statusFilter !== 'all') queryParams.set('status', this.statusFilter);

        const resp = await fetch(`/api/bundles?${queryParams.toString()}`);
        if (resp.ok) {
          const data = await resp.json();
          this.bundles = data.bundles || [];
          this.totalBundles = data.total || this.bundles.length;
        } else {
          console.error('Failed to load bundles:', resp.statusText);
        }
      } catch (err) {
        console.error('Error fetching bundles:', err);
      } finally {
        this.isLoading = false;
      }
    },

    get filteredBundles() {
      return this.bundles.filter(b => {
        // Target filter
        if (this.targetFilter !== 'all' && b.target !== this.targetFilter) {
          return false;
        }
        // Status filter
        if (this.statusFilter !== 'all' && b.status !== this.statusFilter) {
          return false;
        }
        // Search query
        if (this.searchQuery) {
          const q = this.searchQuery.toLowerCase();
          const matchName = b.bundle_name && b.bundle_name.toLowerCase().includes(q);
          const matchTarget = b.target && b.target.toLowerCase().includes(q);
          const matchVersion = b.version && b.version.toLowerCase().includes(q);
          return matchName || matchTarget || matchVersion;
        }
        return true;
      });
    },

    getTargetBadgeClass(target) {
      switch ((target || '').toLowerCase()) {
        case 'prod':
        case 'production':
          return 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20';
        case 'staging':
        case 'stage':
          return 'bg-amber-500/10 text-amber-400 border border-amber-500/20';
        case 'dev':
        case 'development':
        default:
          return 'bg-blue-500/10 text-blue-400 border border-blue-500/20';
      }
    },

    getStatusBadgeClass(status) {
      switch ((status || '').toLowerCase()) {
        case 'active':
        case 'deployed':
          return 'bg-emerald-900/40 text-emerald-400 border border-emerald-800/50';
        case 'deploying':
          return 'bg-blue-900/40 text-blue-400 border border-blue-800/50';
        case 'failed':
          return 'bg-red-900/40 text-red-400 border border-red-800/50';
        case 'destroyed':
          return 'bg-gray-800 text-gray-400 border border-gray-700';
        case 'pending_drift_sync':
          return 'bg-amber-900/40 text-amber-400 border border-amber-800/50';
        default:
          return 'bg-gray-800 text-gray-300 border border-gray-700';
      }
    },

    formatDate(isoStr) {
      if (!isoStr) return 'Never';
      try {
        const d = new Date(isoStr);
        return d.toLocaleDateString(undefined, {
          month: 'short',
          day: 'numeric',
          hour: '2-digit',
          minute: '2-digit'
        });
      } catch (e) {
        return isoStr;
      }
    },

    // ── Drawer Actions ─────────────────────────────────────────────────────────

    async openDrawer(bundleId) {
      this.drawerOpen = true;
      this.selectedBundle = null;
      this.driftReport = null;
      this.activeDrawerTab = 'overview';
      this.activeRunResult = null;

      try {
        const queryParams = new URLSearchParams();
        if (this.workspaceSlug) queryParams.set('workspace_slug', this.workspaceSlug);
        queryParams.set('include_drift', 'true');

        const resp = await fetch(`/api/bundles/${bundleId}?${queryParams.toString()}`);
        if (resp.ok) {
          this.selectedBundle = await resp.json();
          this.driftReport = this.selectedBundle.drift;
          this.manifestFormatted = this.selectedBundle.manifest_raw || '';
        }
      } catch (err) {
        console.error('Error opening bundle drawer:', err);
      }
    },

    closeDrawer() {
      this.drawerOpen = false;
      this.selectedBundle = null;
      this.driftReport = null;
      this.activeRunResult = null;
    },

    setDrawerTab(tab) {
      this.activeDrawerTab = tab;
    },

    setManifestFormat(fmt) {
      this.manifestFormatTab = fmt;
    },

    async triggerPipeline(bundleId, pipelineName = null) {
      if (!bundleId) return;
      this.isRunningPipeline = true;
      this.activeRunResult = null;

      try {
        const queryParams = new URLSearchParams();
        if (this.workspaceSlug) queryParams.set('workspace_slug', this.workspaceSlug);

        const resp = await fetch(`/api/bundles/${bundleId}/run?${queryParams.toString()}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pipeline_name: pipelineName })
        });

        if (resp.ok) {
          this.activeRunResult = await resp.json();
          // Update last deployed / refresh bundle
          this.fetchBundles();
        } else {
          const err = await resp.json();
          alert('Failed to trigger pipeline: ' + (err.detail || err.error || 'Unknown error'));
        }
      } catch (err) {
        console.error('Error triggering pipeline:', err);
        alert('Pipeline trigger error: ' + err.message);
      } finally {
        this.isRunningPipeline = false;
      }
    },

    // ── Pre-Deploy / Dry-Run Modal Actions ──────────────────────────────────────

    openDeployModal(initialManifest = null) {
      this.deployModalOpen = true;
      this.deploySuccessMessage = null;
      this.deployErrorMessage = null;
      this.dryRunResult = null;
      this.deployTarget = 'dev';
      this.deployFormat = 'toon';

      if (initialManifest) {
        this.deployManifest = initialManifest;
      } else if (!this.deployManifest) {
        // Starter TOON manifest
        this.deployManifest = `# Lake Asset Bundle (TOON specification)
bundle:
  name: customer-analytics
  version: 0.1.0

targets:
  dev:
    workspace: ${this.workspaceSlug || 'dev-workspace'}
    default_catalog: main_lake
  prod:
    workspace: ${this.workspaceSlug || 'prod-workspace'}
    default_catalog: main_lake

resources:
  catalogs[1]{name,description}:
    main_lake,Primary analytics lakehouse catalog

  schemas[3]{catalog,name,tier,description}:
    main_lake,bronze,bronze,Raw ingested events
    main_lake,silver,silver,Cleaned dimensional models
    main_lake,gold,gold,Aggregated revenue marts

  tables[1]{name,catalog,schema,format,s3_path_pattern}:
    customer_orders,main_lake,silver,parquet,"s3://datalake/orders/*.parquet"

  pipelines:
    orders_etl:
      schedule: "0 2 * * *"
      tasks[1]{name,type,entrypoint,upstream_tasks}:
        process_orders,duckdb_sql,sql/orders.sql,[]

  dashboards:
    orders_overview:
      widgets:
        total_orders:
          type: metric
          query: "SELECT count(*) FROM main_lake.silver.customer_orders"
`;
      }
    },

    closeDeployModal() {
      this.deployModalOpen = false;
      this.dryRunResult = null;
      this.deploySuccessMessage = null;
      this.deployErrorMessage = null;
    },

    async validateManifest() {
      this.isValidating = true;
      this.deployErrorMessage = null;
      this.dryRunResult = null;

      try {
        const queryParams = new URLSearchParams();
        if (this.workspaceSlug) queryParams.set('workspace_slug', this.workspaceSlug);

        const resp = await fetch(`/api/bundles/validate?${queryParams.toString()}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            manifest_content: this.deployManifest,
            manifest_format: this.deployFormat,
            target: this.deployTarget
          })
        });

        const data = await resp.json();
        if (resp.ok && data.valid) {
          this.dryRunResult = data;
        } else {
          this.deployErrorMessage = (data.errors && data.errors.length)
            ? data.errors.join('; ')
            : (data.detail || 'Validation failed');
          this.dryRunResult = data;
        }
      } catch (err) {
        this.deployErrorMessage = 'Network error during validation: ' + err.message;
      } finally {
        this.isValidating = false;
      }
    },

    async executeDeploy() {
      this.isDeploying = true;
      this.deployErrorMessage = null;
      this.deploySuccessMessage = null;

      try {
        const queryParams = new URLSearchParams();
        if (this.workspaceSlug) queryParams.set('workspace_slug', this.workspaceSlug);

        const resp = await fetch(`/api/bundles/deploy?${queryParams.toString()}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            manifest_content: this.deployManifest,
            manifest_format: this.deployFormat,
            target: this.deployTarget
          })
        });

        const data = await resp.json();
        if (resp.ok && (data.status === 'active' || data.status === 'deploying')) {
          this.deploySuccessMessage = data.message || `Bundle '${data.bundle_name}' deployed successfully!`;
          await this.fetchBundles();
          setTimeout(() => {
            if (this.deploySuccessMessage) this.closeDeployModal();
          }, 1800);
        } else {
          this.deployErrorMessage = data.detail || data.error_message || 'Deployment failed';
        }
      } catch (err) {
        this.deployErrorMessage = 'Network error during deployment: ' + err.message;
      } finally {
        this.isDeploying = false;
      }
    },

    // ── Conversational AI Generator Actions ───────────────────────────────────

    openAIModal() {
      this.aiModalOpen = true;
      this.aiPrompt = '';
      this.aiError = null;
      this.generatedManifest = '';
    },

    closeAIModal() {
      this.aiModalOpen = false;
      this.aiPrompt = '';
      this.aiError = null;
      this.generatedManifest = '';
    },

    async generateWithAI() {
      if (!this.aiPrompt.trim()) return;
      this.isGeneratingAI = true;
      this.aiError = null;

      try {
        const queryParams = new URLSearchParams();
        if (this.workspaceSlug) queryParams.set('workspace_slug', this.workspaceSlug);

        const resp = await fetch(`/api/bundles/generate-ai?${queryParams.toString()}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            prompt: this.aiPrompt,
            workspace_slug: this.workspaceSlug
          })
        });

        const data = await resp.json();
        if (resp.ok && data.manifest) {
          this.generatedManifest = data.manifest;
        } else {
          this.aiError = data.error || data.detail || 'Failed to generate bundle from prompt';
        }
      } catch (err) {
        this.aiError = 'Error communicating with AI service: ' + err.message;
      } finally {
        this.isGeneratingAI = false;
      }
    },

    applyAIGeneratedToDeploy() {
      if (!this.generatedManifest) return;
      const manifest = this.generatedManifest;
      this.closeAIModal();
      this.openDeployModal(manifest);
    },

    // ── Destroy Modal Actions ──────────────────────────────────────────────────

    openDestroyModal(bundle) {
      this.destroyTarget = bundle;
      this.destroyConfirmInput = '';
      this.purgeStorageCheck = false;
      this.destroyError = null;
      this.destroyModalOpen = true;
    },

    closeDestroyModal() {
      this.destroyModalOpen = false;
      this.destroyTarget = null;
      this.destroyConfirmInput = '';
      this.purgeStorageCheck = false;
      this.destroyError = null;
    },

    async confirmDestroy() {
      if (!this.destroyTarget) return;
      if (this.destroyConfirmInput.trim().toUpperCase() !== 'DESTROY') {
        this.destroyError = "Please type 'DESTROY' to confirm.";
        return;
      }

      this.isDestroying = true;
      this.destroyError = null;

      try {
        const queryParams = new URLSearchParams();
        if (this.workspaceSlug) queryParams.set('workspace_slug', this.workspaceSlug);
        if (this.purgeStorageCheck) queryParams.set('purge_storage', 'true');

        const resp = await fetch(`/api/bundles/${this.destroyTarget.id}?${queryParams.toString()}`, {
          method: 'DELETE'
        });

        if (resp.ok) {
          this.closeDestroyModal();
          if (this.drawerOpen) this.closeDrawer();
          await this.fetchBundles();
        } else {
          const data = await resp.json();
          this.destroyError = data.detail || 'Failed to destroy bundle';
        }
      } catch (err) {
        this.destroyError = 'Error destroying bundle: ' + err.message;
      } finally {
        this.isDestroying = false;
      }
    }

  }));

});
