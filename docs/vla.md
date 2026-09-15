# VLA model bridge

The task system invokes VLA nodes through `robot_runtime.vla_request.v1`, using the
same observation tickets and capability validation as code/GP6 nodes. A trained
model need not generate JSON itself: `create_vla_app` wraps an operator-owned
`async predict(context)` adapter returning `{actions, done}`.

`examples/vla_server.py` loads a configured factory and action codec. It has no
synthetic fallback and does not bundle a checkpoint. Install the selected model's
own dependencies and implement its image/state preprocessing inside that adapter.
A blocking or GPU model can run in its own worker/server; do not block the ASGI
loop inside an async adapter.

For the included `JointActionCodec`, the operator declares:

- Exact joint order and lower/upper limits in radians.
- Absolute position or incremental joint-delta mode.
- Seconds per action step and maximum chunk length.

Array rank, dimensions, finite numbers, measured state, joint order, chunk length
and cumulative joint limits are validated. Deltas accumulate from measured qpos;
arrays are never padded, clipped, reordered, or interpreted as normalized actions
without an explicit embodiment implementation. Cartesian poses, gripper modes,
force targets and normalized action spaces require their own declared codecs and
capabilities. A generic six- or seven-element vector does not identify those units.

Example launch, after installing your actual policy adapter:

```bash
export ROBOT_VLA_FACTORY=your_policy_adapter:load_policy
export ROBOT_VLA_MODEL=your_checkpoint_name
export ROBOT_VLA_CODEC=configs/vla-joint-codec.example.json
uvicorn examples.vla_server:create_app --factory --host 127.0.0.1 --port 8766
```

Set `ROBOT_VLA_TOKEN` to protect this service. Configure the runtime VLA endpoint
as `http://127.0.0.1:8766/predict` and a matching model name/key environment.
The server bounds concurrent predictions and request size; overload returns 429.
It only proposes actions. The coordinator must still check the observation epoch,
reserve resources, and dispatch through the actual robot driver.

A terminal policy prediction may return `actions: null, done: true`; a nonterminal
empty prediction is rejected. The response echoes the request observation ticket,
capability and model identity. No tokenizer usage is fabricated for a VLA without
reported usage. Recorded unmetered calls remain distinct from measured token totals.

Tests use explicitly labeled NumPy policy fixtures. They validate conversions and
the model-serving contract; they do not establish learned-policy task performance.

## Pi 0.5 provider

A concrete provider is now included using the official third-party OpenPI client.
See [Pi 0.5 integration](pi05.md) and the main README for complete commands.
The DROID profile uses seven velocity channels plus a separate gripper command;
it is distinct from the generic absolute/delta joint codec above.
