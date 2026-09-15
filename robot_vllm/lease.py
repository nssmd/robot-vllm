"""Goal-scoped liveness for trusted ROS deployments, using a local monotonic clock.

This is not authorization or an emergency stop. A commissioned controller must
implement the hold behavior; ordinary FollowJointTrajectory servers do not.
"""

from __future__ import annotations

import math


class GoalLease:
    def __init__(self, timeout_s):
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("lease timeout must be positive and finite")
        self.timeout_s = timeout_s
        self.goal_id = None
        self.deadline = 0.0

    def start(self, goal_id, now):
        self.goal_id, self.deadline = goal_id, now + self.timeout_s

    def renew(self, goal_id, now):
        # Delayed heartbeats cannot revive an expired lease or a different goal.
        if self.goal_id != goal_id or self.expired(now):
            return False
        self.deadline = now + self.timeout_s
        return True

    def expired(self, now):
        return self.goal_id is not None and now >= self.deadline

    def clear(self):
        self.goal_id = None
