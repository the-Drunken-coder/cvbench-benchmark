# Control plane implementation input

This file preserves the exact implementation prompt supplied for the public Cloudflare control plane.

> You are the lead implementation task for CVBench's public Cloudflare control plane. Work autonomously in this isolated worktree and do the implementation; the parent task is orchestration-only.
>
> Authoritative product intent:
> - Existing public repo: the-Drunken-coder/cvbench-system-runner. Preserve the existing benchmark engine and PROJECT_SPEC_VERBATIM.md.
> - Add a polished public documentation/submission/results website and an API that an AI agent can use to submit a model with the goal "win this benchmark."
> - Prefer one Cloudflare Worker with Static Assets and D1 over Pages-only.
> - Cloudflare is the control plane only. Never execute untrusted model code in a Worker.
> - Actual jobs must run on ephemeral GitHub-hosted Linux Actions runners using the repository's existing Docker-isolated CVBench engine.
> - Keep the solution simple and elegant.
>
> Required architecture and scope:
> 1. Worker serves the static site plus /api/v1/*.
> 2. Site explains what CVBench measures, model container contract, temporal/multi-frame support, exact standard layout, how to submit, status/results, security constraints, and includes copy-paste examples for humans and AI agents.
> 3. API supports at minimum health, benchmark/system contract metadata, OpenAPI JSON, create submission, fetch submission/status/result, and a protected internal lease/result callback used by the trusted runner.
> 4. Submission accepts only a prebuilt OCI image pinned by sha256 digest plus strictly validated metadata/argv; no arbitrary source checkout, build step, shell string, or Docker socket.
> 5. Use D1 migrations for durable queue/job/result state. Add sensible auth and abuse controls: public reads, API-key-protected submissions (or a clearly documented bootstrap key), constant-time token comparison, idempotency, payload limits, and status transition validation.
> 6. Add a scheduled and manually dispatchable trusted GitHub Actions runner that leases queued jobs, pulls the pinned image, runs the existing Docker isolation, posts scored report JSON back, never exposes callback secrets inside the tested container, and uses least privilege.
> 7. Add a Cloudflare deployment workflow if credentials can be provisioned cleanly, but do not invent credentials.
> 8. Include complete local Docker-free developer commands for the Worker/site where possible and comprehensive tests.
> 9. Run unit/integration tests and a baseline end-to-end submission lifecycle with a scored result using a safe baseline container.
> 10. Create a codex/cloudflare-control-plane branch, commit focused changes, push, and open a draft PR if GitHub auth is available. Preserve the full original product spec in the PR body by linking the verbatim file and include the exact implementation prompt in a collapsible section or committed design input.
> 11. Do not merge. Report exact evidence, remaining secrets/account steps, branch, commits, PR URL, deployment URL, and any blockers.
>
> Important: inspect current main/default branch rather than assuming the parent checkout. Do not use Cloudflare Pages if Worker Static Assets suffices. Do not add a broad framework unless it materially simplifies the result.
