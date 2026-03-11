from __future__ import annotations

import logging
from collections import defaultdict, deque

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class PlannedTask(BaseModel):
    id: str
    description: str
    file_path: str  # vault-relative path to the note this task relates to
    prompt: str  # full instructions for the agent
    depends_on: list[str] = []
    max_turns: int = 10  # overseer estimates per-task complexity


class OverseerPlan(BaseModel):
    tasks: list[PlannedTask] = []


def validate_plan(plan: OverseerPlan) -> list[str]:
    """Validate an overseer plan. Returns list of errors (empty = valid)."""
    errors: list[str] = []
    task_ids = {t.id for t in plan.tasks}

    # Check for duplicate IDs
    if len(task_ids) != len(plan.tasks):
        seen: set[str] = set()
        for t in plan.tasks:
            if t.id in seen:
                errors.append(f"Duplicate task ID: {t.id}")
            seen.add(t.id)

    # Check for unknown dependencies
    for t in plan.tasks:
        for dep in t.depends_on:
            if dep not in task_ids:
                errors.append(f"Task '{t.id}' depends on unknown task '{dep}'")

    # Check for circular dependencies (Kahn's algorithm — if we can't
    # fully sort, there's a cycle)
    if not errors:
        in_degree: dict[str, int] = {t.id: 0 for t in plan.tasks}
        graph: dict[str, list[str]] = defaultdict(list)
        for t in plan.tasks:
            for dep in t.depends_on:
                graph[dep].append(t.id)
                in_degree[t.id] += 1

        queue: deque[str] = deque(
            tid for tid, deg in in_degree.items() if deg == 0
        )
        sorted_count = 0
        while queue:
            node = queue.popleft()
            sorted_count += 1
            for neighbor in graph[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if sorted_count != len(plan.tasks):
            errors.append("Circular dependency detected in task plan")

    return errors


def topological_levels(plan: OverseerPlan) -> list[list[PlannedTask]]:
    """Group tasks into execution levels using topological sort.

    Level 0 = tasks with no dependencies (run concurrently).
    Level 1 = tasks depending only on level-0 tasks (run after level 0).
    etc.

    Returns list of levels, each level is a list of tasks to run concurrently.
    """
    if not plan.tasks:
        return []

    task_map = {t.id: t for t in plan.tasks}
    in_degree: dict[str, int] = {t.id: 0 for t in plan.tasks}
    graph: dict[str, list[str]] = defaultdict(list)

    for t in plan.tasks:
        for dep in t.depends_on:
            graph[dep].append(t.id)
            in_degree[t.id] += 1

    levels: list[list[PlannedTask]] = []
    current_level = [
        tid for tid, deg in in_degree.items() if deg == 0
    ]

    while current_level:
        levels.append([task_map[tid] for tid in current_level])
        next_level: list[str] = []
        for tid in current_level:
            for neighbor in graph[tid]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    next_level.append(neighbor)
        current_level = next_level

    return levels
