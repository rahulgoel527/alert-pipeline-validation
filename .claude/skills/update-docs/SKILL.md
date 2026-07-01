---
name: update-docs
description: Update lab_setup.md, tests/Tests.md, and README.md when services/ or tests/ code changes. Invoked manually or automatically by the PostToolUse hook after a file edit.
---

# update-docs

You are the authoritative updater for the three project documentation files. When invoked — either manually via `/update-docs` or automatically after a file edit — follow this process exactly.

## Step 1: Identify what changed

If you were invoked by the hook, you have the edited file path in context. If invoked manually, read the git diff or ask the user which file changed.

## Step 2: Apply the rules below

Each rule tells you: when to act, what sections to read, and what to update. Make **surgical edits only** — change the stale content, preserve everything else. Do not rewrite for style.

---

## Rule 1: `services/lab_setup.md`

**When to run:** Edited file is anywhere under `services/` AND is not `services/lab_setup.md` itself.

**Read first:** the edited file in full, then `services/lab_setup.md`.

**Check these sections for staleness:**
The
| Section in lab_setup.md | Stale when |
|--------------------------|-----------|
| Architecture Overview / Services | a service is added, removed, or renamed |
| API Endpoints (port 8000) | routes change, request/response shapes change in `services/api/main.py` |
| Stats Response | fields in the `/api/stats` response change |
| Processor Behavior | failure thresholds change (currently 2% stuck / 5% fail / 10% slow in `services/event_processor/main.py`) |
| Stuck-Alert Reaper | reaper interval (300s) or stuck timeout (60 min) changes |
| Generator Behavior | generation rate or duplicate injection rate changes in `services/event_generator/main.py` |
| Design Tradeoffs / Known Limitations | architectural decisions change |

**Skip if:** the change is infrastructure-only (Dockerfile, pip requirements, `.env`) with no observable behavior change.

---

## Rule 2: `tests/Tests.md`

**When to run:** Edited file is anywhere under `tests/` AND is not `tests/Tests.md` itself.

**Read first:** the edited file in full, then `tests/Tests.md`.

**Check these sections for staleness:**

| Section in Tests.md | Stale when |
|---------------------|-----------|
| Run Tests → expected counts | test count changes (currently "43 unit + 19 E2E") |
| Unit Tests → What's covered table | a unit test file is added/removed, or a test class changes its purpose |
| E2E Tests → What's covered table | an E2E test file is added/removed, or test intent changes |
| By marker table | markers in `tests/pytest.ini` change |
| Prerequisites | new service dependencies required before running tests |
| Load Tests → What to observe | Locust performance targets change in `tests/load/locust.conf` or `locustfile.py` |

**Skip if:** the change is to `tests/helpers/`, `tests/conftest.py`, or fixture internals that don't affect visible test behavior or counts.

---

## Rule 3: `README.md`

**When to run:** Edited file is anywhere under `services/` or `tests/`.

**Read first:** `README.md` (it is intentionally short — ~41 lines).

**Check these sections for staleness:**

| Section in README.md | Stale when |
|----------------------|-----------|
| Services table | a service port or description changes |
| Testing section | the top-level pytest command changes |
| See Also links | a referenced doc filename changes |

**Bias toward no-op:** README is an entry-point summary. Only update it if the quick-start workflow or services table is genuinely wrong. Do not expand it.

---

## Step 3: Report what you did

After making edits (or deciding no edits are needed), output a one-line summary per doc:

```
lab_setup.md  — updated: Processor Behavior (failure thresholds)
Tests.md      — no change needed
README.md     — no change needed
```

If invoked manually with no file context, read `git diff HEAD` first and infer which rules apply.
