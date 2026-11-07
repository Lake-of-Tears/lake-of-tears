/**
 * Lake of Tears - Session Inactivity Manager (Alpine.js Component)
 * Handles client-side idle tracking, warning countdown, and proactive sliding refresh.
 */
document.addEventListener('alpine:init', () => {
  Alpine.data('inactivityManager', (timeoutMins = 60, maxHours = 24) => {
    const rawMins = Number(timeoutMins);
    const validMins = Number.isFinite(rawMins) ? rawMins : 60;
    const timeoutMs = validMins * 60 * 1000;
    const maxSessMs = (Number(maxHours) || 24) * 3600 * 1000;

    // Adapt warning window: up to 5 minutes, scaled down to 25% of timeout (minimum 15s)
    const warnMs = (timeoutMs > 0)
      ? Math.min(5 * 60 * 1000, Math.max(15 * 1000, Math.floor(timeoutMs * 0.25)))
      : 0;

    return {
      timeoutMs,
      maxSessMs,
      warnMs,
      warningActive: false,
      countdownFormatted: '5:00',
      lastActivity: Date.now(),
      lastRefreshAt: Date.now(),
      countdownInterval: null,
      pollIntervalTimer: null,

      init() {
        // Expose globally for legacy inline calls and programmatic test verification
        window.stayLoggedIn = () => this.stayLoggedIn();
        window.signOutNow = () => this.signOutNow();

        if (this.timeoutMs <= 0) return;

        ['mousemove', 'keydown', 'mousedown', 'touchstart', 'scroll', 'click'].forEach(evt =>
          document.addEventListener(evt, () => {
            // While warning modal is active, require deliberate action (button click)
            if (!this.warningActive) {
              this.lastActivity = Date.now();
            }
          }, { passive: true })
        );

        const pollMs = Math.max(1000, Math.min(5000, Math.floor(this.warnMs / 4) || 2000));
        this.pollIntervalTimer = setInterval(() => this.tick(), pollMs);
      },

      fmtCountdown(ms) {
        const s = Math.max(0, Math.ceil(ms / 1000));
        return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
      },

      showWarning(remainingMs) {
        this.warningActive = true;
        this.countdownFormatted = this.fmtCountdown(remainingMs);

        if (this.countdownInterval) clearInterval(this.countdownInterval);
        const deadline = Date.now() + Math.max(0, remainingMs);
        this.countdownInterval = setInterval(() => {
          const left = deadline - Date.now();
          this.countdownFormatted = this.fmtCountdown(left);
          if (left <= 0) {
            clearInterval(this.countdownInterval);
            this.countdownInterval = null;
            this.signOutNow();
          }
        }, 1000);
      },

      hideWarning() {
        this.warningActive = false;
        if (this.countdownInterval) {
          clearInterval(this.countdownInterval);
          this.countdownInterval = null;
        }
      },

      async stayLoggedIn() {
        this.lastActivity = Date.now();
        this.hideWarning();
        try {
          const res = await fetch('/api/auth/refresh', {
            method: 'POST',
            credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
          });
          if (res.status === 401) {
            window.location.href = '/logout?reason=session_expired';
            return;
          }
          this.lastRefreshAt = Date.now();
        } catch (err) {
          console.warn('Session refresh error:', err);
        }
      },

      async signOutNow() {
        this.hideWarning();
        try {
          await fetch('/api/auth/logout', {
            method: 'POST',
            credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
          });
        } catch (_) {}
        window.location.href = '/logout?reason=inactivity';
      },

      async tick() {
        if (this.warningActive) return;

        const idleMs = Date.now() - this.lastActivity;

        if (idleMs >= this.timeoutMs) {
          this.showWarning(0);
          return;
        }

        if (idleMs >= this.timeoutMs - this.warnMs) {
          this.showWarning(this.timeoutMs - idleMs);
          return;
        }

        // Proactive background session slide when user is active
        const now = Date.now();
        if (now - this.lastRefreshAt >= Math.max(60000, Math.floor(this.timeoutMs / 3))) {
          this.lastRefreshAt = now;
          try {
            const res = await fetch('/api/auth/refresh', { method: 'POST', credentials: 'include' });
            if (res.status === 401) {
              window.location.href = '/logout?reason=session_expired';
            }
          } catch (_) {}
        }
      },
    };
  });
});

// Fallback bindings if Alpine has not loaded yet
if (typeof window.stayLoggedIn === 'undefined') {
  window.stayLoggedIn = () => {};
  window.signOutNow = () => { window.location.href = '/logout?reason=inactivity'; };
}
