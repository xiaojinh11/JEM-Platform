# NAW · AI-Driven Operations

> Phase A–E rollout: turn NAW into a change-intelligence platform. Every
> write is now gated by AI risk, every run produces an AI-verified diff,
> and every rollback has a concrete plan the operator can execute.

---

## 1. Three new capabilities at a glance

| Surface | Endpoint | Purpose |
| --- | --- | --- |
| Dry-Run preview | `POST /api/v1/jobs/dry-run` | Render the template + scan for high-risk commands *before* a job is created. Returns per-device commands, rule-based verdict, AI commentary (only when rules fire), and a rough ETA. |
| Rollback plan | `POST /api/v1/jobs/{id}/rollback-plan` | Build a structured rollback plan from the snapshot files the worker took (`auto_backup_restore` / `config_patch` / `manual`). |
| AI comparison | `POST /api/v1/jobs/{id}/ai-compare` | Diff before/after snapshots after the job finishes. Returns a verdict ladder (`as_expected` / `minor_diff` / `major_diff` / `failed`) plus an LLM-written Chinese summary suitable for an audit log entry. |

The **Job** row now carries four new JSON columns:

- `risk_assessment` — `{level, factors, ai_comment, blast_radius, warning}`
- `rollback_plan` — `{strategy, steps, estimated_minutes, dependencies}`
- `ai_comparison` — `{verdict, deltas, warnings, ai_summary}`
- `risk_assessed_at`, `ai_comparison_at` — timestamps

The migration is `alembic upgrade head` → head `be2c1929b6cc`.

---

## 2. End-to-end flow

```
┌──────────────────────────────────────────────────────────────────────┐
│  OPERATOR                                                            │
│  1. Picks devices + writes a template (or inline commands)           │
│  2. Clicks "Dry-Run 预览" in JobsPage                               │
└──────────────────────────────────────────────────────────────────────┘
                              │ POST /jobs/dry-run
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  BACKEND                                                             │
│  • Jinja2 render (StrictUndefined) → per-device command list         │
│  • Dedupe across devices → rule scan (15 dangerous patterns)         │
│  • If rules fire → LLM call for short Chinese rationale (≤3 sent.)   │
│  • Graph query: which neighbours share a credential (blast radius)   │
└──────────────────────────────────────────────────────────────────────┘
                              │ 200 { risk, commands_by_device, notes }
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  OPERATOR reviews preview, hits "创建任务"                            │
└──────────────────────────────────────────────────────────────────────┘
                              │ POST /jobs
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  BACKEND (create_job)                                               │
│  • Save risk_assessment + rollback_plan + requires_approval flag    │
│  • NEVER block job creation if AI is unavailable                     │
│    (risk_assessment_error persists the failure message instead)      │
└──────────────────────────────────────────────────────────────────────┘
                              │ 201 { id, risk_assessment, rollback_plan }
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  OPERATOR → "启动"                                                   │
└──────────────────────────────────────────────────────────────────────┘
                              │ POST /jobs/{id}/start
                              ▼ (gated if requires_approval && !approved)
┌──────────────────────────────────────────────────────────────────────┐
│  WORKER (Celery · batch_apply)                                       │
│  per device:                                                        │
│    1. collect_config → BackupSnapshot(trigger="before")              │
│    2. apply_config → JobStep(payload.log_preview)                    │
│    3. collect_config → BackupSnapshot(trigger="after")               │
│  after job:                                                          │
│    4. summarise_comparison (LLM, ≤3 sentences in Chinese)            │
│    5. compute_ai_comparison (line-diff + verdict ladder)             │
│    6. Persist ai_comparison + ai_comparison_at on the Job row        │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  OPERATOR opens the job, sees "回滚计划" + "AI 对比" buttons         │
│  Both return persisted JSON; UI shows summary in a dialog.          │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. Risk model

The rule layer (`app/utils/command_validator.py`) is the single source of
truth for `is_dangerous`. It scans each rendered command line against 15
patterns covering Cisco IOS-XE / Huawei VRP / Ruijie RGOS — `shutdown`,
`no ip route`, `reload`, `write erase`, `format`, `delete /recursive`,
`undo interface`, `clear configuration`, `reset saved-configuration`,
etc. The exception `shutdown vlan N` is *not* dangerous because it just
disables a SVI without affecting trunks.

The LLM **never** decides the verdict — it only translates the
rule-based findings into a short Chinese explanation suitable for a
human reviewer. This is deliberate: an LLM-only verdict would be
non-deterministic and impossible to audit. If the AI call fails or is
disabled, the rule-based report still stands and `risk_assessment_error`
records the cause.

### Risk levels

- **safe** — no dangerous patterns matched. Job can start without admin approval.
- **dangerous** — at least one dangerous pattern matched. `requires_approval=true`
  on the Job; the `POST /jobs/{id}/start` route refuses to enqueue until
  an admin sets `approved_by` (UI button not yet wired — the field
  exists for auditability and future automation).

### Blast radius

`_affected_devices` queries the Kuzu graph: "which other devices share
the same credential as this one?" If a write fails halfway and the SSH
credential leaks, only the neighbour set is potentially affected —
network operators use this to size the blast radius before pressing
"start". Graph outage is non-fatal: `_graph_neighbour_ids` catches and
returns `[]` with a logger warning.

---

## 4. Rollback plan

`build_rollback_plan(db, job_id)` walks the Job's `BackupSnapshot`s and
classifies by coverage:

| Strategy | Condition | Steps generated |
| --- | --- | --- |
| `auto_backup_restore` | every target device has a `before` snapshot | one `restore_backup` step per device, pointing at the snapshot file |
| `config_patch` | some `before` snapshots exist, some don't | `auto_backup_restore` for the covered set + a "manual re-collect" note for the missing ones |
| `manual` | no `before` snapshots at all | empty `steps` + dependency note pointing at git / config centre |

Estimated minutes is a heuristic (`5 + 2 × covered + 15 × missing`) so the
operator can decide whether to roll back now or wait for a maintenance
window.

The plan is recomputed on demand (idempotent) and persisted on the Job
row every time it is fetched, so the audit log always has the plan as
of *that moment* even if the underlying snapshot files later disappear.

---

## 5. AI comparison verdict ladder

```
            ┌─────────────┐
            │  succeeded  │  (from Job.status — driver for the next step)
            └──────┬──────┘
                   │
       for every target device:
                   │
   ┌───────────────┼───────────────┬──────────────┐
   ▼               ▼               ▼              ▼
no after     hash equal      small diff     huge diff
snapshot     before/after    (<=50 lines)   (>50 lines)
   │               │               │              │
   ▼               ▼               ▼              ▼
"failed"    "as_expected"   "minor_diff"  "major_diff"
```

`compute_ai_comparison` is a pure function (no I/O outside the file
read), so it's both cheap to call from the endpoint and re-callable from
tests. `summarise_comparison` adds the LLM-written narrative *only* when
the verdict isn't `as_expected` (an empty summary means AI is disabled
or the call failed — caller should treat `None`/`""` equivalently).

---

## 6. Frontend touch points

`frontend/src/pages/JobsPage.vue`:

- New "AI 风险" column on the job list (three-tone tag).
- New "Dry-Run 预览" button next to "创建任务"; opens a panel showing:
  - estimated minutes
  - matched-command count + expandable list
  - AI commentary in a coloured callout (only when present)
  - advisory notes (large fleet, missing devices, etc.)
- New "回滚计划" + "AI 对比" buttons in the per-row action menu. Both
  open an `ElMessageBox.alert` with the full JSON. The AI 对比 button is
  disabled until `status === 'succeeded'`.

`frontend/src/api.ts` adds three thin wrappers:
`dryRunJob(payload)`, `rollbackPlan(id)`, `aiCompareJob(id)`.

---

## 7. Testing

| Layer | File | What it covers |
| --- | --- | --- |
| Backend unit | `tests/test_ai_risk.py` | rule-based scan (5 cases) + import shape |
| Backend unit | `tests/test_dry_run.py` | dry-run preview (5) + rollback plan (3) + AI comparison (3) + diff-line counter (3) |
| Backend e2e | `tests/test_jobs_dry_run_e2e.py` | HTTP roundtrip through `TestClient` (8 cases — login, dry-run safe/dangerous/empty, create-job persists risk+rollback, rollback endpoint, ai-compare endpoint, 401 path) |
| Frontend | `src/jobs.api.spec.ts` | wire shape pins (5 cases) |

Total coverage after A–E: **66 backend tests + 5 frontend tests**.

Run them with:
```bash
# backend
cd backend && python -m unittest discover -s tests

# frontend
cd frontend && npx vitest run
```

---

## 8. Operational caveats

1. **AI disabled** — every code path tolerates `cfg.enabled = False`.
   `ai_comment` / `ai_summary` come back as `null` / empty string and
   the UI hides those sections.
2. **Graph outage** — Kuzu file lock contention between workers and
   API processes is logged at WARNING and returns an empty blast
   radius. Never blocks job creation or start.
3. **Worker timeout** — the LLM call has a 25-second timeout
   (`RISK_COMMENT_TIMEOUT`). Anything slower than that just means the
   AI commentary will be missing; the rule-based verdict remains.
4. **Plan idempotency** — calling `/rollback-plan` twice produces the
   same steps (deterministic), so re-fetches after a long-running job
   don't surprise the operator.
5. **Audit trail** — the persisted JSON columns are the audit trail.
   We do *not* duplicate them into the audit_events table; the Job row
   itself is the source of truth.

---

## 9. Future work (suggested, not started)

- Wire the "审批通过 / 拒绝" admin UI button — `requires_approval` and
  `approved_by` fields exist; only the UI affordance is missing.
- Graph "shares config baseline" relationship — currently we only walk
  the credential edge. Adding `DEVICE_HAS_CONFIG` to the blast-radius
  query would catch the case where two devices sync from the same
  template.
- Streaming LLM responses for the AI commentary so the user sees the
  sentence appear as it's generated, instead of waiting the full
  timeout. Today it's a hard sync call.
