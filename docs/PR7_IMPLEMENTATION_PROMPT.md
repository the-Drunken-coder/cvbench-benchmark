# PR 7 implementation prompt

You are the implementation owner for a focused CVBench benchmark pacing and compute-accounting PR. The parent task is orchestration-only; own design verification, implementation, validation, branch/commit/push, and open a ready PR. Start from current origin/main containing merged PR #6. Do not merge or deploy production.

User intent: preserve native source time and causal online evaluation, while allowing submitted whole vision systems to process slower when necessary without gaining an unpriced compute advantage. CVBench already has online_replay, playback_rate, container CPU/RAM limits, CPU-time sampling, latency, and source timestamps, but it does not make the source-time/compute-time contract or normalized resource cost clear enough.

Required contract:
- Keep immutable native source metadata (e.g. 30 FPS and source duration) separate from delivery pace and system processing time. Never rewrite truth so a slow run appears to be a lower-FPS camera.
- Preserve ordered progressive frames, no future frames, no ground-truth hints, and temporal state support.
- Native real-time replay remains a standard capability measurement. If a slower allowed replay pace is used, record the exact rate as part of run/benchmark provenance and never compare it as if it were native real time.
- Measure from the container/cgroup, not self-report: wall duration, stream delivery duration, completion/drain duration, CPU time, CPU-seconds per native source-second, real-time factor, average/peak CPU, peak RAM, disk I/O, output rate, per-frame latency/backlog/deadline misses, and GPU/VRAM only when genuinely available/isolated. Account for startup separately from steady-state where possible.
- Define fairness without an arbitrary hidden blend. Keep accuracy metrics intact and expose efficiency as first-class axes. Add transparent compute tiers or Pareto comparison so higher accuracy bought with more CPU-seconds/source-second or slower-than-real-time completion is visible and cannot silently win an equal-budget category. If a composite is necessary for current winner selection, document/version the exact formula and retain raw axes.
- Fixed Docker limits (currently 4 CPU/2048 MiB/no network) remain enforced. Add hard overall safety timeouts and output limits. A system may finish after stream end, but late outputs and causal timestamp rules must be handled explicitly and tested.
- Clarify socket/backpressure semantics: delivery pacing must use an independent source clock; a slow reader must not silently redefine source FPS. Detect/report sender blocking, accumulated backlog, and dropped/deadline-missed frames according to a versioned policy.
- Decide and document the minimal secure API/config surface for allowed replay-rate selection. Use an allowlisted versioned enum/range and bind it into benchmark identity/provenance; do not allow arbitrary shell/config injection or unequal runs to share one leaderboard class.
- Update public docs/UI/results/JSON/OpenAPI/operator evidence and machine-readable schemas compatibly. Preserve /api/v1 compatibility where required.
- Add adversarial tests for slow readers, bursty output, buffering, post-stream output, timestamp spoofing, CPU saturation, idle/wait-heavy systems, multi-process accounting, crashes/timeouts, and replay at native/slower rates. Prove resource accounting cannot be improved merely by sleeping or hiding child processes.
- Run exact Docker-scored E2E with at least a fast baseline and deliberately slow CPU-heavy/idle systems; publish evidence showing native source duration, delivery rate, CPU-seconds/source-second, real-time factor, accuracy, and category assignment.
- Keep the solution simple and elegant. Preserve the exact implementation prompt in the PR description/repo history per project practice.

Return PR URL, exact head, the versioned timing/compute contract, leaderboard semantics, compatibility notes, Docker evidence, tests, and blockers. Do not invoke final Greptile/CodeRabbit/Codex reviews; parent control room owns review follow-through.
