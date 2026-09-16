# ROS 2 multi-arm control with fewer model requests

This example connects the existing shared-perception service to native ROS 2
action execution. Four independent controller processes publish `JointState` and
execute `FollowJointTrajectory`. An actual language model proposes two joint
targets per arm from a shared state snapshot. All commands pass through the
runtime's observation-ticket validation, joint limits and resource reservation.

Controllers are explicitly synthetic. This checks transport, control integration
and actual provider usage, not physical hardware or manipulation success. The
simple negate-joints instruction is an integration fixture, not a claim that a
language model is needed to compute such a trajectory.

## Run

With ROS 2 Jazzy installed, use a Python 3.12 environment that can import rclpy:

```bash
source /opt/ros/jazzy/setup.bash
python -m pip install -e '.[serve]'
export GP6_ENDPOINT='https://YOUR_GATEWAY/v1/responses'
export GP6_API_KEY='YOUR_KEY'
ROS_DOMAIN_ID=197 ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST \
  python scripts/ros_shared_control.py --model gpt-5.5 \
  --rounds 3 --output runs/my-ros-sharing-test
```

See [ROS environment installation](QUICKSTART_DEPLOYMENT.md) if necessary.
These commands make paid model calls. The example starts and stops its own
controllers with unique ROS namespaces. It never connects to hardware endpoints.
Run in a trusted local ROS domain. Original logs and failed attempts remain intact.

## Comparison

Each pair uses the same joint-state payload and matched initial joint positions:

- `independent`: four concurrent requests, each returns one arm's targets.
- `shared`: one request returns all four targets; four subscribers consume it.

The result schema and control rule are the same. Initial states change between
rounds, and condition order alternates. Both variants have access to the complete
four-arm state snapshot, so the comparison does not remove context from one arm.
It is suitable for applications where all arms need that shared context; independent
tasks needing only local state may have less potential for token savings.

Before every condition the controllers return to the same initial positions.
Each action uses a newly captured ticket. Shared results expire after 120 seconds,
are invalidated between conditions and never transfer an action ticket between
arms. Incorrect or out-of-bounds model targets are rejected before dispatch.
Native results must be completed and settled, and reported joint positions must
reach the targets within 0.001 rad. Synthetic controller feedback is not a physical
motion guarantee.

## What is measured

The report includes actual prompt/completion/total tokens, provider cache reads,
unmetered calls, concurrent executions, commanded/observed joint positions,
perception wait and combined perception/action duration. Reset actions are
retained in runtime journals but excluded from measured model/control rounds.
All four motions are dispatched together after the perception barrier.

Sharing here merges concurrent requests for one snapshot. It does not demonstrate
provider-side prompt-cache acceleration. `joined_inflight` counts subscribers
sharing active work; `cache_hits` counts completed-result reuse. Neither is an
input-token billing counter. Use provider usage to substantiate token reductions.

The separate `python -m robot_vllm.ros_validation validate` check verifies native
cancellation, quarantine until a late terminal result, no duplicate dispatch,
stale-ticket rejection and joint limits without paid model calls.

## Measured integration result (2026-09-16)

Actual GPT-5.5 calls with native ROS 2 transport, four synthetic two-joint arm
controllers and three changing initial states; condition order alternated.

| Metric, three rounds | Independent | Shared |
| --- | ---: | ---: |
| Completed tested arm actions | 12 | 12 |
| Model calls | 12 | 3 |
| Prompt tokens | 2,096 | 587 |
| Completion tokens | 796 | 267 |
| Total tokens | 2,892 | 854 |
| Provider cached input tokens | 0 | 0 |
| Unmetered calls | 0 | 0 |
| Median sensing wait | 2.59 s | 2.18 s |
| Median sensing + action duration | 2.94 s | 2.52 s |

Both conditions produced identical targets in all three matched rounds and
reached them within 0.001 rad according to controller feedback. Peak simultaneous
executions was four throughout. All 24 measured actions completed, with zero
failures or infrastructure interruptions. Additional reset motions are excluded
from that count. Nine subscribers joined three shared computations; no completed
cache lookup was used. The 70.5% total-token reduction comes from consolidating
requests, not from provider KV-cache hits.

Median paired reductions were 11.6% for sensing wait, 10.2% for sensing + action,
and 4.5% for the sum of individual waits. These differ from ratios of unpaired
medians. Three pairs are preliminary observations, not a stable latency guarantee.
The model was asked to negate joints solely to exercise the complete integration;
a code policy should perform this arithmetic in a real application.

The native lifecycle test separately passed duplicate-request protection, stale
ticket and limit rejection, delayed-cancellation quarantine and resource release
after the native terminal result: three completed actions and one intentional
cancellation, zero remaining uncertain executions. Both validations cleaned up
their own controller processes; no test workers remain running.

Local evidence: `runs/ros-shared-control-varying-20260916/result.json` and
`runs/ros-control-lifecycle-20260916/`. The earlier constant-start development run
remains separate. Seventeen shared-sensing/control unit tests and Ruff passed.
