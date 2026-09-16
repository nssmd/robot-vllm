# Evaluation scope correction / 评测范围修正

The user clarified on 2026-09-16 that the earlier 8–10 tasks × 10 seeds instruction
was intended for another task. It is withdrawn for this project.

本项目只验证两件事：

1. ROS 2 能协调控制多个机械臂。
2. 共享感知、缓存等方式能否减少模型开销和 token 消耗。

只在 `robot-vllm` 内开展工作，不查看或改动 RoboRSI，不扩展到 UR5 专项适配。
无需为此建立 10×10 操作任务矩阵。实际验证优先覆盖多臂并发、资源互斥、
取消与恢复，以及匹配输入下的调用次数、实际 token、缓存读写与等待时间。

Synthetic ROS controllers are suitable for transport/execution integration.
Real provider usage is required for actual token/cache claims. These are separate
from learned-policy manipulation success; report their scope explicitly.
Existing diagnostic artifacts remain retained and do not become formal results.
