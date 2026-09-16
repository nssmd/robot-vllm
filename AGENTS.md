# Robot runtime project instructions

## User requirements recorded 2026-09-16

- Formal robot experiments must cover 8–10 tasks at minimum, with at least 10
  seeds per task. Default to 10 tasks × 10 seeds = 100 episodes per condition.
  A two-condition comparison therefore requires at least 200 scheduled episodes.
- Use actual robot manipulation with closed-loop simulation or commissioned
  hardware. Image Q&A, colored grids, sliders and protocol fixtures are development
  diagnostics, not substitutes for the requested robot experiment.
- Freeze one shared task/seed/initial-state manifest before formal runs. Every
  condition uses exactly that same distribution, model, skill release, controller,
  observation settings and budgets except the declared experimental variable.
- Verify each task key against the actual runtime's instruction and asset/config
  versions. Do not assume task indices have the same meaning in another checkout.
- Seeds and initial-state indices are distinct. Record both; archive evaluator-only
  initial states and reuse them across conditions without exposing hidden state to
  any agent. Task family and initialization distributions must remain matched.
- A provider or infrastructure interruption does not reduce the planned matrix.
  Keep it separate, preserve diagnostics and report missing matched cases. Retry
  after addressing the interruption; never replay an already successful condition,
  task and seed. Do not replace a difficult seed/task with an easier one.
- Keep pilots, historical diagnostics and the formal frozen evaluation separate.
  Do not claim equal robot performance or acceleration from one seed or a few
  easy examples. Report per-task results and paired efficiency on matched cases.
- Report final simulator verdicts, success/failure/infra counts, completed/planned
  episodes per condition, worker counts, and whether the campaign is preliminary.
  Cache reads, writes, unknown usage, logical tokens, latency and robot blocking
  are separate metrics. Zero cache reads must not be relabeled as acceleration.
- Model inference and performance measurements must run on GPUs, not CPU fallback.
  Read current GPU access documentation and verify availability; do not disrupt
  other experiments. CPU unit tests and orchestration are permitted.
- Keep the framework easy to connect to open model servers and VLA/agent systems.
  Avoid naming/claims that imply a vLLM GPU engine where only API orchestration exists.
- Update Chinese and English documentation together. Changes may be pushed after
  appropriate verification; the user has authorized direct pushes.
- Persist subsequent user corrections in these instructions and project memory.
  A local memory update is not a Feishu document update; do not claim the latter
  unless the identified document has actually been edited.

Workspace restrictions still apply: LIBERO short only; externally owned long
experiments and OPD are outside this session. Preserve all experiment artifacts.
