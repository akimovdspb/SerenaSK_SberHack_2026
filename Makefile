SHELL := /bin/bash
.DEFAULT_GOAL := help

LOCAL_COMPOSE := docker compose --env-file .env.local -f compose.local.yml

.PHONY: help init setup up down engineering-init engineering-up engineering-down doctor backup backup-check backup-prune bootstrap skill-contract spec-drift architecture source-audit gate0-evidence verify-gate0 verify-gate1 verify-gate3 verify-gate4 budget-status test-budget live-probe-preflight live-probe gate2-live-preflight gate2-live-pilot live-readiness runtime-patch-assessment seed demo-reset demo-canary demo-check lint format-check typecheck test test-contract build smoke e2e e2e-controlled-retry test-chaos license security eval-replay eval-live-preflight eval-live evidence verify-core verify-implementation verify verify-submission clean-clone-rehearsal package-submission-dry-run package-submission

help:
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "%-28s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

init: ## Create portable local configuration and securely register one OpenRouter key
	uv run python -m scripts.local_init

setup: ## Install locked Python and Node dependencies
	uv sync --frozen
	npm ci

up: ## Build and start the judge-ready local OpenRouter/GLM-5.2 stack
	uv run python -m scripts.local_init --check
	CF_BUILD_REVISION="$$(git rev-parse HEAD)" $(LOCAL_COMPOSE) up --build --detach factory
	uv run python -m scripts.wait_local_ready

down: ## Stop only the judge-ready local stack (persistent state is preserved)
	$(LOCAL_COMPOSE) down

engineering-init: ## Create the legacy deterministic verification environment
	uv run python -m scripts.init_project

engineering-up: ## Start the legacy multi-container verification stack
	uv run python -m scripts.preflight up
	docker compose up --build --detach gateway app ouroboros
	$(MAKE) bootstrap

engineering-down: ## Stop the legacy multi-container verification stack
	docker compose down

doctor: ## Run read-only local/runtime diagnostics
	uv run python -m scripts.doctor

backup: ## Create a checksummed online SQLite/rules/evidence backup with a unique BACKUP_ID
	uv run python -m scripts.backup

backup-check: ## Restore-readability and checksum validation of BACKUP_PATH or latest backup
	uv run python -m scripts.backup --validate

backup-prune: ## Dry-run retention; deletion additionally requires ALLOW_BACKUP_PRUNE=true
	uv run python -m scripts.backup --prune

bootstrap: ## Configure pinned runtime and create the atomic contract lock
	uv run python -m scripts.bootstrap_runtime
	CONTRACT_IMAGE_ID="$$(docker image inspect communication-factory/ouroboros:$${OUROBOROS_VERSION:-v6.61.4} --format '{{.Id}}')" docker compose --profile tools run --rm contract-probe

skill-contract: ## Verify skill manifest, marker, hashes and byte-equal prompt projection
	uv run python -m scripts.skill_contract

spec-drift: ## Compare machine spec constants with runtime and domain contracts
	uv run python -m scripts.spec_drift

architecture: ## Verify the backend has no direct provider or LLM path
	uv run python -m scripts.architecture_scan

source-audit: ## Verify pinned source, license discrepancy and private tracking boundaries
	uv run python -m scripts.source_audit

gate0-evidence: ## Validate preserved Gate 0 live evidence without provider calls
	uv run python -m scripts.gate0_evidence

verify-gate0: ## Run every Gate 0 check without creating live evidence
	uv run python -m scripts.preflight bootstrap
	$(MAKE) skill-contract spec-drift architecture source-audit gate0-evidence budget-status test-budget
	uv run python -m scripts.image_security
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pytest -q

verify-gate1: ## Verify deterministic Gate 1 cases, QA, state, approval and export
	uv run python -m scripts.gate1
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pytest -q

verify-gate3: ## Verify feedback, targeted revision, governed rule, B03 and rollback
	uv run python -m scripts.gate3
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pytest -q

verify-gate4: ## Verify backend read models, production frontend and browser matrix
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pytest -q
	npm run lint
	npm run typecheck
	npm run build
	$(MAKE) e2e

budget-status: ## Print redacted read-only project budget status
	uv run python -m scripts.budget_control status

test-budget: ## Run deterministic live-run budget guard tests
	uv run pytest -q tests/unit/test_budget_control.py

live-probe-preflight: ## Validate one paid Gate 0 probe without making a provider call
	uv run python -m scripts.live_probe --check-only

live-probe: ## Run one guarded synthetic two-tool Gate 0 provider probe
	uv run python -m scripts.live_probe

gate2-live-preflight: ## Validate one selected B04/B07/B08 pilot without a provider call
	uv run python -m scripts.gate2_live --check-only

gate2-live-pilot: ## Run one guarded B04/B07/B08 campaign through managed Ouroboros
	uv run python -m scripts.gate2_live

live-readiness: ## Bind reviewed warmup, smoke and distinct pilots into the live manifest
	uv run python -m scripts.live_readiness

runtime-patch-assessment: ## Report the no-call strict-tool compatibility blockers
	uv run python -m scripts.runtime_patch_assessment

seed: ## Seed deterministic synthetic fixtures
	uv run python -m scripts.seed_data

demo-reset: ## Reset mutable demo state only
	uv run python -m scripts.demo reset

demo-canary: ## Run one separately opted-in and capped live B04 demo canary
	uv run python -m scripts.demo canary

demo-check: ## Read-only validate strict runtime, frozen evidence and a current live canary
	uv run python -m scripts.demo check

lint: ## Lint Python and frontend sources
	uv run ruff check .
	npm run lint

format-check: ## Check formatting without modifying files
	uv run ruff format --check .

typecheck: ## Typecheck Python and TypeScript
	uv run mypy
	npm run typecheck

test: ## Run deterministic unit and integration tests without provider calls
	uv run pytest -m 'not contract and not live'

test-contract: ## Run pinned/runtime/API contract tests without provider calls
	uv run pytest -m contract

build: ## Build backend, frontend and Compose images
	npm run build
	docker compose build

smoke: ## Run deterministic API/Compose smoke tests
	uv run python -m scripts.smoke

e2e: ## Run the repository-pinned Playwright matrix on an isolated no-provider stack
	uv run python -m scripts.e2e

e2e-controlled-retry: ## Run enabled providerless retry success/failure browser profiles
	uv run python -m scripts.controlled_retry_e2e

test-chaos: ## Run isolated X01-X05 chaos suite
	uv run pytest -q tests/chaos
	uv run python -m scripts.chaos_cases

license: ## Generate and validate the dependency/license inventory without provider calls
	uv run python -m scripts.license_scan

security: license ## Run architecture, secret, PII, network, artifact and license scans
	uv run python -m scripts.security_scan

eval-replay: ## Evaluate immutable replay fixtures only
	uv run python -m scripts.evaluation replay

eval-live-preflight: ## Validate the full paid basket guard without provider calls or new state
	uv run python -m scripts.live_evaluation --check-only

eval-live: ## Run the separately guarded sequential paid live basket
	uv run python -m scripts.live_evaluation

evidence: ## Build immutable implementation evidence from existing runs
	uv run python -m scripts.evidence

verify-core: ## Run every deterministic machine gate; never starts live evaluation
	uv run python -m scripts.verify core

verify-implementation: ## Read-only validation of deterministic gates and frozen live evidence
	uv run python -m scripts.verify implementation

verify: verify-implementation ## Compatibility alias for verify-implementation

verify-submission: ## Add real human review/approval/sign-off/video gates
	uv run python -m scripts.verify submission

clean-clone-rehearsal: ## Rehearse README plus one separately capped live B04 smoke in a clean checkout
	uv run python -m scripts.clean_clone

package-submission: ## Fail closed until Submission DoD, then package checksummed artifacts
	uv run python -m scripts.package_submission

package-submission-dry-run: ## Validate packaging schemas using explicit non-human fixtures
	uv run python -m scripts.package_submission --dry-run
