# Security Notes — Production Readiness Checklist

**Complete these items before deploying to production.** These are security-related changes that were identified during the audit and intentionally deferred from the initial fix pass.

---

## 1. Protect or Remove Debug Endpoints

**Location:** `app.py` lines 575–634

**Issue:** `/api/debug/gmail-test` and `/api/debug/gmail-config` are publicly accessible and expose Gmail configuration details (client ID previews, token lengths, configuration status).

**Action:** Either add `@admin_required` (or `@login_required`) or remove/disable these endpoints in production:

```python
@app.route('/api/debug/gmail-test')
@admin_required
def api_debug_gmail_test():
    ...

@app.route('/api/debug/gmail-config')
@admin_required
def api_debug_gmail_config():
    ...
```

---

## 2. Add `@login_required` to All Sensitive Routes

**Issue:** Many API routes lack authentication. Unauthenticated requests can access/modify data.

**Routes to protect:** Add `@login_required` to at least:

| Route | Purpose |
|-------|---------|
| `/api/email/send` | Send emails |
| `/api/settings` | Get/modify settings |
| `/api/schools`, `/api/schools/search`, `/api/schools/add-to-sheet` | School data |
| `/api/templates` (all template routes) | Email templates |
| `/api/tracking/*` | Tracking stats |
| `/api/email/*` | Email controls, preview, etc. |
| `/api/auto-send/*` | Auto-send toggle, run-now, etc. |
| `/api/dm/*` | DM queue |
| `/api/coach/*` | Coach search, mark replied |
| `/api/analytics/*` | Analytics |
| `/api/responses/*` | Response data |
| `/api/hooks/*` | AI hooks |
| `/api/ai-emails/*` | AI email generation |
| `/api/followups/*` | Follow-up management |
| `/api/crm/*` (in app.py) | CRM contact detail |
| `/api/scraper/*` | Scraper controls |
| `/api/deployment-info` | Deployment info |
| `/api/hudl/views` | Hudl views |

---

## 3. Protect Enterprise Blueprint Routes

**Location:** `enterprise/routes.py`

**Issue:** All enterprise routes (CRM, reminders, reports, followups, templates, Twitter search) have no authentication.

**Action:** Register the blueprint with a `before_request` that requires login, or apply `@login_required` to each route. Consider creating a shared decorator for the blueprint:

```python
# In enterprise/routes.py - add to each route or use blueprint before_request
from flask import g
# Ensure g.athlete_id is set (user logged in) before any enterprise route runs
```

---

## 4. Request-Scoped Database Context (Race Condition)

**Location:** `db/supabase_client.py`

**Issue:** `_athlete_id` is stored on the shared `SupabaseDB` singleton. Concurrent requests can overwrite each other’s context.

**Action:** Use Flask `g` or thread-local storage instead of instance attributes. Pass `athlete_id` explicitly into DB methods where possible, or use a context manager that sets/restores context per request.

---

## 5. Remove Credentials from Documentation

**Location:** `CLAUDE.md`

**Issue:** Contains real email and password (e.g. `underwoodkeelan@gmail.com`, `Keelan2026!`).

**Action:** Replace with placeholders before committing or sharing. Use environment variables or a secrets manager in production.

---

## 6. Settings Loading and Athlete Context

**Location:** `app.py` `load_settings()`

**Issue:** `load_settings()` may run before athlete context is set (e.g. at import or in background threads), potentially returning wrong or default settings.

**Action:** Ensure settings are always loaded in request context, or explicitly pass `athlete_id` into `load_settings()` when called outside request context.

---

## 7. Ensure `FLASK_SECRET_KEY` Is Set

**Issue:** Session cookies depend on `FLASK_SECRET_KEY`. Default `os.urandom(24).hex()` changes on each restart.

**Action:** Set a stable `FLASK_SECRET_KEY` in production environment variables. Use a long random string (e.g. 32+ chars).

---

## 8. Ensure `CREDENTIALS_ENCRYPTION_KEY` Is Set

**Issue:** Per-athlete Gmail credentials are encrypted with Fernet. Missing or weak key risks credential exposure.

**Action:** Set `CREDENTIALS_ENCRYPTION_KEY` in production. Generate with `from cryptography.fernet import Fernet; Fernet.generate_key()`.

---

## Quick Checklist

- [ ] Protect or remove `/api/debug/gmail-test` and `/api/debug/gmail-config`
- [ ] Add `@login_required` to all sensitive API routes
- [ ] Add authentication to all enterprise blueprint routes
- [ ] Fix request-scoped DB context (race condition)
- [ ] Remove real credentials from `CLAUDE.md`
- [ ] Verify `FLASK_SECRET_KEY` is set in production
- [ ] Verify `CREDENTIALS_ENCRYPTION_KEY` is set in production
- [ ] Review `load_settings()` usage in background/import contexts
