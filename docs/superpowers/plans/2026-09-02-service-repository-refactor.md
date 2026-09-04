# Service and Repository Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with review checkpoints.

**Goal:** Finish the current service/repository split, remove application-to-infrastructure coupling, and standardize the directly related long modules without changing public behavior.

**Architecture:** Keep `NeuroAgentService` and `SqliteRepository` as composition facades over focused mixins. Introduce a small application-owned `SecretWriterPort`; wire the infrastructure implementation only at the composition root. Extract only directly related validation, preparation, persistence, and conversion helpers.

**Tech Stack:** Python 3.11, Pydantic, SQLAlchemy, pytest, Ruff, mypy, PowerShell, React/Vitest.

---

### Task 1: Establish regression tests for the application boundary

**Files:**
- Modify: `tests/backend/test_application_import_boundaries.py`
- Test: `tests/backend/test_application_import_boundaries.py`

- [ ] **Step 1: Add the focused failing assertion**

Extend the existing boundary test so it scans every `*.py` under `neuroagent/application` and rejects both `neuroagent.infrastructure` imports and the concrete `write_env_secret` symbol.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `uv run pytest tests/backend/test_application_import_boundaries.py -q`

Expected: FAIL, identifying `service_mixins/models.py` as importing `neuroagent.infrastructure.secrets`.

- [ ] **Step 3: Keep the test as the regression contract**

Do not weaken the assertion or exclude the new mixin directory.

### Task 2: Introduce and inject `SecretWriterPort`

**Files:**
- Modify: `neuroagent/application/ports.py`
- Modify: `neuroagent/application/services.py`
- Modify: `neuroagent/application/service_mixins/_base.py`
- Modify: `neuroagent/application/service_mixins/models.py`
- Modify: `neuroagent/bootstrap.py`
- Modify: `tests/backend/test_model_profiles.py` or the existing model-profile test module

- [ ] **Step 1: Add a failing injection test**

Construct `NeuroAgentService` with a recording secret writer, create a model profile with an API key, and assert the writer receives `(settings.secrets_file, profile.api_key_env, api_key)` exactly once. The test must not import `neuroagent.infrastructure.secrets`.

- [ ] **Step 2: Run the test and verify RED**

Run: `uv run pytest tests/backend -k "secret_writer or model_profile" -q`

Expected: FAIL because the service has no injected writer and the mixin calls the infrastructure function directly.

- [ ] **Step 3: Implement the minimal port and composition-root wiring**

Define a protocol such as:

```python
class SecretWriterPort(Protocol):
    def write(self, secrets_file: Path, key: str, value: str) -> None: ...
```

Add it to the service constructor/base mixin dependency contract, replace `write_env_secret(...)` with `self.secret_writer.write(...)`, and inject an adapter around the existing infrastructure function from `bootstrap.py` or the application factory. Preserve existing settings and error behavior.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `uv run pytest tests/backend/test_application_import_boundaries.py tests/backend -k "secret_writer or model_profile" -q`

Expected: PASS.

### Task 3: Standardize directly related service mixins

**Files:**
- Modify: `neuroagent/application/service_mixins/models.py`
- Modify: `neuroagent/application/service_mixins/projects.py`
- Modify: `neuroagent/application/service_mixins/runs.py`
- Modify: `neuroagent/application/service_mixins/statistics.py`
- Modify: `neuroagent/application/service_mixins/_base.py`
- Test: existing backend tests covering these services

- [ ] **Step 1: Identify extraction seams without changing behavior**

Use `rg -n "^    (async )?def |^    [A-Za-z_].*=$"` and line counts to locate methods over roughly 80 lines. Group repeated logic into four categories: input/path preparation, revision/state validation, repository write/event append, and response conversion.

- [ ] **Step 2: Add one behavior-preserving regression test per extracted seam**

Cover model profile creation, project/dataset preparation, run action validation, and statistical result registration using existing fixtures and public service methods. Assert returned contracts and repository/event effects, not private helper calls.

- [ ] **Step 3: Extract focused helpers**

Move each category into the owning mixin or `_base.py`; keep helpers typed, side-effect boundaries explicit, and public method signatures unchanged. Do not introduce a generic service framework or alter idempotency semantics.

- [ ] **Step 4: Run targeted tests and style checks**

Run: `uv run pytest tests/backend tests/science -q`

Run: `uv run ruff check neuroagent/application`

Expected: all targeted tests pass and Ruff reports no errors.

### Task 4: Standardize directly related repository mixins

**Files:**
- Modify: `neuroagent/infrastructure/persistence/repository.py`
- Modify: `neuroagent/infrastructure/persistence/repository_mixins/_base.py`
- Modify: `neuroagent/infrastructure/persistence/repository_mixins/models.py`
- Modify: `neuroagent/infrastructure/persistence/repository_mixins/projects.py`
- Modify: `neuroagent/infrastructure/persistence/repository_mixins/runs.py`
- Test: `tests/backend` repository and service tests

- [ ] **Step 1: Add regression coverage for model deletion and repository composition**

Verify deleting an existing profile succeeds, deleting a missing profile raises the existing not-found error, and `SqliteRepository` exposes all split methods exactly once.

- [ ] **Step 2: Run the focused tests and verify RED where applicable**

Run: `uv run pytest tests/backend -k "model_profile or repository" -q`

Expected: the new deletion regression fails only if the current implementation does not preserve the contract; otherwise record it as a characterization test and proceed.

- [ ] **Step 3: Consolidate only duplicate infrastructure helpers**

Keep `repository.py` as a class declaration/composition file. Move shared session, JSON, UTC, ID, and version helpers to `_base.py`; keep resource-specific queries in the corresponding mixin. Preserve transaction boundaries and exception types.

- [ ] **Step 4: Run the AST split verifier and targeted tests**

Run: `uv run python _verify_split.py`

Run: `uv run pytest tests/backend -q`

Expected: AST verifier passes; backend results contain no new failures beyond the known PowerShell restore environment issue.

### Task 5: Final verification and hygiene

**Files:**
- Review: all modified files and `git diff`
- Modify only if needed: `CHANGELOG.md`, relevant README or architecture documentation

- [ ] **Step 1: Run Python quality gates**

Run: `uv run ruff format --check neuroagent tests`

Run: `uv run ruff check neuroagent tests`

Run: `uv run mypy neuroagent`

Run: `uv run pytest -q`

- [ ] **Step 2: Run frontend quality gates**

Run from `web`: `npm run lint; npm run typecheck; npm run test -- --run; npm run build`

- [ ] **Step 3: Review known environment failures**

Run the restore-script tests independently and capture whether `powershell.exe -NoProfile` resolves `Get-FileHash`. Do not change production restore behavior unless a reproducible script defect is demonstrated outside the environment mismatch.

- [ ] **Step 4: Review the final diff**

Run: `git diff --check; git status --short; git diff --stat`

Confirm no secrets, runtime files, synthetic data, or generated artifacts are included. Do not commit or push the implementation unless explicitly requested.

