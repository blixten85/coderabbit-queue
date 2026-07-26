# blixten85/coderabbit-queue Wiki

> This directory is machine-managed by cubic. Edit wiki content through [cubic wiki settings](https://www.cubic.dev/wiki/blixten85/coderabbit-queue) and custom instructions.

Wiki version: 2
Source commit: ad441e798b4b4fb2dc5509a344c8a61a9557054a
Source branch: main
Generated: 2026-07-20T06:52:41.013Z

## Contents

### Overview

- [Introduction to CodeRabbit Queue](01-sec-overview/01-page-intro.md)
- [The Review Quota Problem](01-sec-overview/02-page-problem-statement.md)
- [Managed Target Repositories](01-sec-overview/03-page-target-repos.md)
- [Running the Orchestrator Locally](01-sec-overview/04-page-local-execution.md)
- [Migrating from Per-Repo Workflows](01-sec-overview/05-page-migration-guide.md)

### System Architecture

- [High-Level Architecture](02-sec-architecture/01-page-architecture-overview.md)
- [GitHub Actions Orchestration](02-sec-architecture/02-page-gha-integration.md)

### Backend Systems

- [The Python Orchestrator Script](03-sec-backend/01-page-python-orchestrator.md)
- [Dependency Management](03-sec-backend/02-page-dependencies.md)
- [API Authentication & Tokens](03-sec-backend/03-page-authentication.md)

### Core Features

- [PR Action Priority Logic](04-sec-core-features/01-page-priority-logic.md)
- [Merge Conflict Handling](04-sec-core-features/02-page-merge-conflicts.md)
- [Missing CodeRabbit Review Detection](04-sec-core-features/03-page-missing-reviews.md)
- [Tracking Unresolved Threads](04-sec-core-features/04-page-unresolved-threads.md)
- [Shared Budget Enforcement](04-sec-core-features/05-page-budget-enforcement.md)
- [Per-PR Cooldown Enforcement](04-sec-core-features/06-page-pr-cooldowns.md)
- [Graceful Run Termination](04-sec-core-features/07-page-early-termination.md)

### Data Management & Flow

- [Queue State Schema](05-sec-data-management/01-page-queue-state.md)
- [State Read/Write Lifecycle](05-sec-data-management/02-page-state-persistence.md)

### Model Integration

- [CodeRabbit (@coderabbitai) Integration](06-sec-model-integration/01-page-coderabbit-integration.md)
- [Review Generation & Nudging](06-sec-model-integration/02-page-nudge-execution.md)

### Deployment & Infrastructure

- [Cron Job Setup](07-sec-deployment/01-page-cron-deployment.md)
- [Automated State Git Commits](07-sec-deployment/02-page-automated-commits.md)

### Extensibility & Customization

- [Adding or Removing Repositories](08-sec-extensibility/01-page-modifying-repos.md)
- [Adjusting Rate Limits & Cooldowns](08-sec-extensibility/02-page-tuning-limits.md)
- [Extending Decision Priorities](08-sec-extensibility/03-page-customizing-priority.md)
- [Troubleshooting & Logging](08-sec-extensibility/04-page-troubleshooting.md)
