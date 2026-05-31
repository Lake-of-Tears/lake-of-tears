# Session Inactivity Timeout — Spec

## Overview

Add a configurable session inactivity timeout to the admin panel. When a user's session has been idle
for longer than the configured threshold, they are automatically logged out. Active users are
never interrupted — the timeout window slides forward on each request.

---

## Security Model

### Why not client-side enforcement alone

JavaScript can be paused (tab backgrounded), disabled, or bypassed. Client-side idle timers
**must not** be the only enforcement layer. They are a UX layer only — a warning dialog and a
graceful logout message.

### Server-side enforcement: short-lived JWT + sliding refresh

The current token has a fixed 7-day TTL. The new model:

1. **Short-lived JWT** — `exp` is set to `now + inactivity_timeout` at issue time.
2. **Silent refresh endpoint** (`POST /api/auth/refresh`) — validates the current token and
   issues a new one with a fresh `exp`. Called by the frontend while the user is active.
3. **No DB hit per request** — the `exp` claim in the JWT is the enforcement mechanism.
   The middleware's existing `jwt.decode()` call already rejects expired tokens; no new
   server-side state is needed.
4. **Absolute max session cap** — separate `session_max_hours` setting (default 24 h). Even
   continuously active users are forced to re-authenticate after this hard limit, regardless
   of activity. This is stored as a second claim (`iat`-based check inside the refresh
   endpoint).

### Token rotation on every refresh

Each call to `/api/auth/refresh` issues a **new** signed token. The old token is not explicitly
revoked (stateless JWT), but its `exp` means it will expire at the originally scheduled time
regardless. Because the refresh interval is `timeout / 2`, a stolen short-lived token provides
at most half the inactivity window of useful time before it expires without refresh.

### Cookie flags (unchanged, already correct)

- `HttpOnly` — JavaScript cannot read the token.
- `SameSite=Strict` — CSRF protection.
- `Secure` — HTTPS only (enforced in production).

---

## Admin Setting

### New system setting keys

Added to the existing `SystemSetting` key-value table (no schema migration needed).

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `session_inactivity_timeout_minutes` | int | `0` | 0, 15–480 | Minutes of inactivity before logout. `0` = disabled (use `session_max_hours` as the only cap). |
| `session_max_hours` | int | `24` | 1–168 | Hard maximum session age regardless of activity. |

**Validation rules:**
- `session_inactivity_timeout_minutes` must be `0` or between `15` and `480` (inclusive).
  Values below 15 minutes are rejected — token round-trip time makes sub-15-minute timeouts
  unreliable and frustrating.
- `session_max_hours` must be between `1` and `168` (7 days).
- If inactivity timeout is enabled, it must be ≤ `session_max_hours * 60`.

### Admin panel UI

New section **"Session Security"** added below the existing grace-period settings in
`ui/templates/settings/admin.html`.

```
╔══════════════════════════════════════════════════════════╗
║  Session Security                                        ║
║                                                          ║
║  Inactivity Timeout                                      ║
║  [ Disabled ▼ ]  (or: 15 min / 30 min / 1 hr / 2 hr /  ║
║                        4 hr / 8 hr / Custom…)           ║
║                                                          ║
║  Max Session Length                                      ║
║  [ 24 hours ▼ ]  (or: 4 hr / 8 hr / 24 hr / 48 hr /    ║
║                        7 days)                           ║
║                                                          ║
║  [ Save Session Settings ]                               ║
╚══════════════════════════════════════════════════════════╝
```

Changes take effect for **new tokens issued after the save** — existing sessions are not
immediately revoked. This avoids booting every logged-in user the moment an admin saves the
page. Sessions issued before the change will expire at their original `exp`.

---

## Backend Changes

### `backend/auth.py`

**`create_access_token(data, expires_delta)`**

- Accept an optional `expires_delta` override.
- Default: read `session_inactivity_timeout_minutes` from `SystemSetting`.
  - If `0` or absent, use `session_max_hours * 60` as the TTL.
  - Otherwise use `session_inactivity_timeout_minutes`.
- Embed `iat` (issued-at) in the token payload — already standard in PyJWT.

**`get_current_user()`**

No changes. The existing `jwt.decode()` with `verify_exp=True` already rejects expired tokens
and returns 401, which the frontend handles with a redirect to `/login`.

### `backend/main.py` (API routes)

**`POST /api/auth/refresh`** (new endpoint)

```
Auth:   Requires valid, non-expired lake_token cookie.
Input:  None (reads cookie automatically).
Logic:
  1. Decode the current token (raises 401 if expired).
  2. Check absolute cap: if now > iat + session_max_hours*3600 → 401 "session expired".
  3. Load current inactivity timeout setting.
  4. Issue a new token with exp = now + inactivity_timeout (or max_hours-based TTL).
  5. Set the new lake_token cookie (same flags as login).
Output: 200 { "ok": true }
```

**`GET /api/admin/settings` / `PATCH /api/admin/settings`**

Add `session_inactivity_timeout_minutes` and `session_max_hours` to the settings schema.
Validate per the rules above; return 422 on invalid values.

### `backend/models.py`

No changes needed. `SystemSetting` is already a generic key-value table.

---

## Frontend Changes

### `ui/templates/base.html` — idle detection + refresh loop

Added as a `<script>` block loaded on every authenticated page.

**Idle tracker**

```javascript
// tracks the last time ANY user interaction was observed
let lastActivity = Date.now();

['mousemove', 'keydown', 'mousedown', 'touchstart', 'scroll', 'click'].forEach(evt =>
  document.addEventListener(evt, () => { lastActivity = Date.now(); }, { passive: true })
);
```

**Refresh poller**

```javascript
// INACTIVITY_TIMEOUT_MS and MAX_SESSION_MS are injected by the Jinja template
// from request.state.session_config (set by AuthMiddleware reading SystemSetting)

const REFRESH_INTERVAL_MS = INACTIVITY_TIMEOUT_MS > 0
  ? Math.floor(INACTIVITY_TIMEOUT_MS / 2)
  : 10 * 60 * 1000;  // fallback: refresh every 10 min when timeout disabled

const WARNING_MS = 5 * 60 * 1000;  // warn 5 min before timeout

async function maybeRefresh() {
  const idleMs = Date.now() - lastActivity;

  if (INACTIVITY_TIMEOUT_MS > 0 && idleMs >= INACTIVITY_TIMEOUT_MS) {
    // User is idle — do not refresh, let the token expire naturally
    return;
  }

  if (INACTIVITY_TIMEOUT_MS > 0 && idleMs >= INACTIVITY_TIMEOUT_MS - WARNING_MS) {
    showInactivityWarning(Math.ceil((INACTIVITY_TIMEOUT_MS - idleMs) / 1000));
    return;
  }

  hideInactivityWarning();

  try {
    const res = await fetch('/api/auth/refresh', { method: 'POST', credentials: 'include' });
    if (res.status === 401) {
      window.location.href = '/login?reason=session_expired';
    }
  } catch (_) { /* network error — next tick will retry */ }
}

setInterval(maybeRefresh, REFRESH_INTERVAL_MS);
```

**Warning modal**

```
╔══════════════════════════════════════════════╗
║  You've been inactive                        ║
║                                              ║
║  You'll be logged out in  3:42               ║
║                                              ║
║  [ Stay Logged In ]    [ Log Out Now ]       ║
╚══════════════════════════════════════════════╝
```

"Stay Logged In" calls `/api/auth/refresh` immediately and resets `lastActivity`.
"Log Out Now" calls `/api/auth/logout`.

If the countdown reaches zero without response, call `/api/auth/logout` and redirect
to `/login?reason=inactivity`.

**Session config injection**

`AuthMiddleware` in `ui/main.py` reads the two settings once per request (or from a short
in-process cache, refreshed every 60 s) and sets `request.state.session_config`:

```python
request.state.session_config = {
    "inactivity_timeout_ms": timeout_minutes * 60 * 1000,  # 0 if disabled
    "max_session_ms": max_hours * 3600 * 1000,
}
```

`base.html` renders these into the `<script>` block as JS constants.

### `/login` page

Accept a `reason` query parameter and display a banner:
- `reason=session_expired` → "Your session has expired. Please log in again."
- `reason=inactivity` → "You were logged out due to inactivity."

---

## Data Flow — Active User

```
User clicks something
  └─ lastActivity = now
  
(TIMEOUT/2 ms later)
Poller fires
  ├─ idleMs < TIMEOUT → call POST /api/auth/refresh
  │     └─ Backend: decode, check iat cap, issue new token, set cookie
  │     └─ Frontend: receives 200, continues
  └─ idleMs ≥ (TIMEOUT - WARNING) → show warning modal
```

## Data Flow — Idle User

```
User stops interacting

(TIMEOUT - WARNING_MS later)
Poller fires → show warning modal with countdown

(WARNING_MS later)
Countdown hits 0 → POST /api/auth/logout → redirect /login?reason=inactivity

Alternatively:
  Any API call from another tab → jwt.decode() raises ExpiredSignatureError → 401
  → AuthMiddleware → redirect /login?reason=session_expired
```

---

## Settings Read Caching

Reading `SystemSetting` on every request would hit the DB on every page load.
Use a simple module-level cache in `ui/main.py` (or a shared helper):

```python
_session_config_cache: dict | None = None
_session_config_fetched_at: float = 0.0
SESSION_CONFIG_TTL = 60.0  # seconds

def get_session_config(backend_url: str) -> dict:
    global _session_config_cache, _session_config_fetched_at
    now = time.monotonic()
    if _session_config_cache and now - _session_config_fetched_at < SESSION_CONFIG_TTL:
        return _session_config_cache
    resp = httpx.get(f"{backend_url}/api/admin/settings/public")
    _session_config_cache = resp.json()
    _session_config_fetched_at = now
    return _session_config_cache
```

A new **unauthenticated** endpoint `GET /api/admin/settings/public` returns only the
session config keys (no other admin settings). This allows `AuthMiddleware` to read it
without a superadmin token.

---

## Edge Cases

| Scenario | Handling |
|----------|---------|
| User has multiple tabs open | Any active tab refreshes the cookie; all tabs share the same `HttpOnly` cookie, so an active tab keeps the session alive across all tabs |
| Admin disables timeout while users are logged in | Existing tokens keep their original short `exp`; next refresh issues a new token with the new (longer/disabled) TTL |
| Admin shortens timeout | Existing tokens expire at their original `exp` (no earlier). New tokens use the shorter TTL. Worst-case an existing session lives up to the old timeout before the new one kicks in |
| Refresh endpoint returns 401 (absolute cap hit) | Frontend redirects to `/login?reason=session_expired` |
| Network failure during refresh | Silently skip; next interval will retry. Token expiry is still enforced server-side |
| OAuth users | Same flow — OAuth only affects login, not the JWT or cookie lifecycle |
| `AUTH_ENABLED=false` | Skip all session logic; `AuthMiddleware` already bypasses JWT checks in this mode |

---

## Implementation Order

1. **Backend: settings schema** — add `session_inactivity_timeout_minutes` and `session_max_hours`
   to `GET/PATCH /api/admin/settings`; add `GET /api/admin/settings/public`.
2. **Backend: token issuance** — update `create_access_token` to read the timeout setting.
3. **Backend: refresh endpoint** — `POST /api/auth/refresh` with absolute cap check.
4. **UI middleware: inject session config** — `AuthMiddleware` reads and caches settings,
   sets `request.state.session_config`.
5. **UI base.html: idle detection + poller + warning modal**.
6. **UI admin panel: Session Security section**.
7. **UI login page: reason banner**.

No Alembic migration needed — `SystemSetting` is already a key-value table.

---

## Out of Scope

- Per-user timeout overrides (one global setting, set by superadmin).
- Revoking already-issued tokens before their `exp` (would require a token blocklist / Redis,
  which adds infrastructure complexity not currently in the stack).
- Alerting admins when users are force-logged out.
