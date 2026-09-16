# Contributing

Robot-vLLM is under active development, and contributions are welcome. You can
help with model integrations, robot adapters, perception and inference efficiency,
reproducible experiments, bug fixes, tests or documentation. Open an
[issue](https://github.com/nssmd/robot-vllm/issues) to share a use case or discuss a
larger change, or submit a pull request for a focused improvement. Issues and pull
requests in English or Chinese are welcome; hardware is not required to contribute.

Treat multiple arms and multiple robots as normal deployments. A single arm is
a topology with one device, not a different architecture. Put embodiment-specific
dimensions, joint order, units, limits and transforms in adapters/capabilities.

The extension points are `ModelEndpoint`, `NodePolicy.propose(context)`,
`Capability`, and `Driver`. Models generate validated DAGs or capability arguments;
they do not select arbitrary Python factories, rewrite transport protocols, or
access evaluator internals. Operator-owned configuration may load an explicit
driver plugin via `module:factory`.

Changes to execution semantics should exercise:

- 1-, 2-, and 4-arm configurations; independent and coordinated capabilities.
- All-member validation and atomic resource reservation before dispatch.
- DAG dependency ordering, cycles, stale inference and request deduplication.
- Peer cancellation after a member failure, delayed native cancellation and
  unknown settlement. A canceled coroutine is not a stopped robot.
- Replanning without modifying or replaying completed nodes.
- Array provenance: missing, empty, non-finite and legitimate all-zero samples
  are distinct; do not fill missing sensor/action arrays with zeros.

Run `python -m pytest -q` and `ruff check robot_vllm tests scripts examples`.
With ROS 2 Jazzy sourced, run the real-transport integration validator using the
explicit synthetic controllers in `robot_vllm.system_validation`. These tests
do not require robot hardware or paid model inference. The checked-in CI
template defines Python and ROS jobs; activate it as described in `docs/ci.md`
and check remote results on the published commit.

Keep synthetic protocol results, real model inference, physical device tests,
and final simulator task verdicts separate. Do not infer task success from an
action/DAG completing. Preserve every diagnostic run and successful-seed resume
protection in the LIBERO path. New public APIs must document error, deadline,
cancellation and compatibility behavior, not just function signatures.

State transitions that can reach a controller require crash-boundary tests. Persist
intent and native locators before side effects; never infer settlement from a
canceled Python task or an absent ROS server. Do not add a model-facing force-unlock
operation. Keep completed-node checkpoints immutable and reject ambiguous replay.

Configuration keys and deployment examples must remain synchronized with offline
validation. Model adapters must declare action spaces explicitly. Include errors,
overload, timeouts and shutdown behavior in API changes. Container/CI examples
should stay small and CPU-only unless an experiment specifically requires more.
