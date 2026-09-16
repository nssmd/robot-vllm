# Formal robot evaluation requirements / 正式机器人实验要求

Recorded from the user's correction on 2026-09-16. This protocol supersedes the
small diagnostic comparisons as the standard for new robot-performance claims.

## 必须执行的规模与口径

用户原话：

> 每个任务你至少应该有 10 个 seed，然后你至少应该有 8 到 10 个任务。
> 你现在的分布都不一样。

执行标准设为 **10 个任务 × 每任务 10 个 seed × 每个对照条件**。
基线与优化两组至少计划 200 回合。所有条件使用同一份冻结的任务、seed 与
初始状态清单，不能各选一批任务、拿不同分布的结果比较。

使用 LIBERO 短任务中的实际机械臂操作，包括抓取、容器放置、抽屉或物体间空间
关系。选取任务必须在看见本次成绩之前完成；一旦冻结，不按成功率删换任务。
当前候选是 `libero_goal_task` 全部 10 个任务，seed 0–9。**这是待核对候选，
不是已冻结、已开始或已完成的正式矩阵。** 必须先核对实际环境中的每个任务
指令、资产版本和初始化映射再创建最终 manifest。

当前的两任务 seed 12031 运行属于集成调试，不能并入正式矩阵。其 Planner
日志里的物体名称与先前按任务表描述的名称不同；这是待查的任务身份/提示词
问题，不可据此声称已经正确运行了所描述的任务。

## Matched experimental design

- One exact simulator/assets/skill release and one task catalog for all conditions.
- At least ten seeds per task; record seed and initial-state index separately.
  Use the same evaluator-only initial state in matched conditions.
- Same model, cameras, tools, reasoning/output budgets, action budget, horizon,
  success predicate and hardware allocation policy. Declare any intentional change.
- Cache baseline and candidate receive equivalent current/historical information.
  Never replace current observations with old frames to manufacture cache hits.
- Freeze task selection, primary metrics, run order and missing-case handling before
  formal evaluation. Interleave condition order to reduce load/time confounding.
- Archive config, source revision, per-episode manifest, requests, measured usage,
  actions, frames/videos and final harness verdict. Do not expose hidden truth to
  Planner, Engineer, Reviewer or prompts.

## Completion and reporting

The planned denominator remains 100 episodes per condition. Report completed
success/failure verdicts, infrastructure interruptions, implementation errors and
missing matched cases separately. Infrastructure is excluded from success-rate
denominators but cannot silently shrink the target matrix. Resolve interruptions
before retrying affected cases and preserve earlier attempts; successful cases
must not be rerun. No cherry-picking replacement seeds.

Report task-by-task results plus aggregate paired comparisons. Include actual
prompt/completion/total tokens, cached reads/writes and unmetered calls, wall time,
model/perception/action/recovery time, robot wait and observation age at dispatch.
Use matched pairs for efficiency and show success regressions as well as gains.
A completed finite matrix is still limited evidence; uncertainty and its scope
must be stated. A pilot, API protocol pass or model-readout correctness is not a
robot task verdict or an established acceleration result.

Current formal progress at protocol creation: **0/100 per condition**. Two existing
diagnostic workers were running; formal task identity and initialization checks
remain outstanding. No formal success, failure or cache-speedup result is claimed.
