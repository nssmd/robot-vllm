# Robot runtime project instructions

## Active scope — user clarification, 2026-09-16

- Work only in robot-vllm on a usable ROS 2 runtime that controls multiple arms
  and reduces model/token overhead through shared perception, caching and other
  measured optimizations. Do not inspect or modify RoboRSI for this task.
- No UR5-specific research or integration is requested. Use existing generic ROS 2
  topics/actions/services and explicit synthetic controllers for integration tests.
- The previous 8–10 tasks × at least 10 seeds requirement was addressed to another
  task by mistake and was explicitly withdrawn for this project. Do not require
  a 10×10 manipulation campaign or expand scope into one.
- Validate multi-arm execution, resource conflicts, cancellation and recovery.
  Use matched inputs/configurations when comparing efficiency; distinguish
  fixture integration, actual model inference and simulator task verdicts.
- Report logical input/output tokens, provider cache reads/writes and unknown
  usage separately. Cache hits do not themselves reduce logical token counts;
  shared requests may do so. Do not claim speedup without measured evidence.
- Never substitute stale images for current observations to force cache hits.
  Preserve original action tickets, freshness checks and successful-run protection.
- Model inference/performance experiments use GPU or actual remote model APIs,
  not CPU model fallback. CPU unit tests and orchestration are permitted.
- Keep integration simple for open model servers, VLA and agent applications.
  Do not imply a vLLM GPU engine where only API orchestration is implemented.
- Update Chinese and English documentation together. The user authorizes direct
  pushes after appropriate verification.
- Persist user corrections in project memory. Do not claim a Feishu update unless
  the identified document was actually modified.
- Preserve all logs, videos, trajectories and diagnostic artifacts. Do not inspect,
  modify or run externally owned long experiments or OPD.
