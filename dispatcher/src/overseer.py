"""Overseer agent — commit-driven orchestration.

Analyzes each human commit, spawns a planning agent to decide what work
is needed, then executes the resulting plan with concurrent/sequential
task agents.
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import git

from src.config import (
    AGENT_TIMEOUT_PER_TURN,
    MAX_CONCURRENT_AGENTS,
    OVERSEER_MAX_TURNS,
    OVERSEER_TIMEOUT_SECONDS,
    VAULT_PATH,
)
from src.container_runner import run_raw, run_raw_streaming, run_task
from src.context_resolver import resolve, resolve_batch
from src.git_watcher import get_changed_md_files, get_commit_diff_text
from src.models import DispatchPayload, TaskResult
from src.overseer_prompt import CommitData, build as build_overseer_prompt
from src.plan_models import OverseerPlan, PlannedTask, topological_levels, validate_plan
from src.status_updater import update_status
from src.task_builder import build as build_task_payload
from src.vault_rules import load_vault_rules

logger = logging.getLogger(__name__)


def gather_commit_data(
    repo: git.Repo, from_sha: str, to_sha: str
) -> CommitData:
    """Collect everything the overseer needs to analyze a commit range."""
    changed_files = get_changed_md_files(repo, from_sha, to_sha)
    diff_text = get_commit_diff_text(repo, from_sha, to_sha)

    # Build accumulated lightweight context for all changed files
    context = resolve_batch(VAULT_PATH, changed_files)

    return CommitData(
        changed_files=changed_files,
        context=context,
        diff_text=diff_text,
    )


def _fix_json_newlines(text: str) -> str:
    """Replace literal newlines with \\n only inside JSON string values.

    JSON strings cannot contain raw newlines — they must be escaped as \\n.
    After extracting inner JSON from the Claude CLI wrapper, string values
    may contain real newlines that need re-escaping.
    """
    result = []
    in_string = False
    i = 0
    while i < len(text):
        ch = text[i]

        if in_string and ch == '\\' and i + 1 < len(text):
            # Escape sequence — keep as-is and skip next char
            result.append(ch)
            result.append(text[i + 1])
            i += 2
            continue

        if ch == '"':
            in_string = not in_string
            result.append(ch)
        elif ch == '\n' and in_string:
            result.append('\\n')
        elif ch == '\r' and in_string:
            result.append('\\r')
        elif ch == '\t' and in_string:
            result.append('\\t')
        else:
            result.append(ch)
        i += 1

    return ''.join(result)


def _try_parse_json(text: str) -> dict | None:
    """Try to parse JSON text, fixing common issues like raw newlines."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fix raw newlines inside string values and retry
    try:
        return json.loads(_fix_json_newlines(text))
    except json.JSONDecodeError:
        return None


def extract_plan(text: str) -> dict | None:
    """Extract and parse the overseer's JSON plan from Claude CLI output.

    Handles multiple layers: Claude CLI JSON wrapper → overseer text → JSON plan.
    Returns parsed dict or None.
    """
    # Layer 1: Try parsing as Claude CLI JSON output
    try:
        output = json.loads(text)
        if isinstance(output, dict):
            if "tasks" in output:
                return output  # Already the plan
            if "result" in output:
                return extract_plan_from_text(output["result"])
        if isinstance(output, list):
            for block in output:
                if isinstance(block, dict) and block.get("type") == "text":
                    return extract_plan_from_text(block.get("text", ""))
    except json.JSONDecodeError:
        pass

    # Fallback: treat as plain text
    return extract_plan_from_text(text)


def extract_plan_from_text(text: str) -> dict | None:
    """Extract JSON plan from the overseer's text output (code fences, bare JSON, etc.)."""
    # Direct parse
    parsed = _try_parse_json(text)
    if parsed and isinstance(parsed, dict) and "tasks" in parsed:
        return parsed

    # Extract from code fences
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        parsed = _try_parse_json(fence_match.group(1).strip())
        if parsed:
            return parsed

    # Find a JSON object directly
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        parsed = _try_parse_json(brace_match.group(0))
        if parsed:
            return parsed

    return None


def run_overseer(commit_data: CommitData) -> OverseerPlan | None:
    """Spawn the overseer container to analyze a commit and return a plan.

    Returns OverseerPlan on success, None on failure (caller should fallback).
    """
    if not commit_data.changed_files:
        logger.info("No changed files — skipping overseer")
        return OverseerPlan(tasks=[])

    vault_rules = load_vault_rules()
    prompt = build_overseer_prompt(commit_data, vault_rules=vault_rules)
    logger.info("Running overseer for %d changed files", len(commit_data.changed_files))
    logger.debug("Overseer prompt (first 500 chars): %s", prompt[:500])

    try:
        exit_code, stdout, stderr = run_raw(
            prompt=prompt,
            max_turns=OVERSEER_MAX_TURNS,
            timeout=OVERSEER_TIMEOUT_SECONDS,
        )
    except RuntimeError as e:
        logger.error("Overseer credential error: %s", e)
        return None

    logger.info("Overseer raw output (first 500 chars): %s", (stdout or "")[:500])

    if exit_code != 0:
        logger.error("Overseer container failed (exit=%d): %s",
                     exit_code, (stderr or stdout)[:500])
        return None

    # Parse the JSON plan from stdout
    try:
        plan_data = extract_plan(stdout)
        if plan_data is None:
            raise ValueError("Could not extract JSON plan from output")
        plan = OverseerPlan(**plan_data)
    except (TypeError, ValueError) as e:
        logger.error("Failed to parse overseer plan: %s\nRaw output: %s",
                     e, stdout[:1000])
        return None

    # Validate
    errors = validate_plan(plan)
    if errors:
        logger.error("Invalid overseer plan: %s", errors)
        return None

    # Log the plan with level structure
    levels = topological_levels(plan)
    logger.info("Overseer plan: %d tasks across %d levels", len(plan.tasks), len(levels))
    for level_idx, level_tasks in enumerate(levels):
        task_descs = ", ".join(f"{t.id} [{t.description}] (turns={t.max_turns})" for t in level_tasks)
        mode = "concurrent" if len(level_tasks) > 1 else "single"
        logger.info("  Level %d (%s): %s", level_idx, mode, task_descs)

    return plan


MAX_RETRIES_ON_MAX_TURNS = 1  # retry once with doubled turns


def _execute_single_task(
    task: PlannedTask,
    auto_commit_fn,
    dependency_outputs: dict[str, str] | None = None,
    on_output=None,
) -> TaskResult:
    """Execute a single planned task in an agent container.

    If the agent hits max_turns, automatically retries with a continuation
    prompt and doubled turn budget (up to MAX_RETRIES_ON_MAX_TURNS times).

    Status updates and result writing are handled by execute_plan() after
    all tasks for a file complete — this function just runs the agent.

    Args:
        dependency_outputs: map of dependency task_id → output_text from
            completed predecessors. Injected into the prompt so the agent
            can use results from earlier tasks without redoing work.
    """
    logger.info("Executing task %s: %s (max_turns=%d)", task.id, task.description, task.max_turns)

    # Build context from the vault hierarchy
    full_path = os.path.join(VAULT_PATH, task.file_path)
    context = ""
    if os.path.isfile(full_path):
        context = resolve(VAULT_PATH, full_path)

    current_turns = task.max_turns
    current_prompt = task.prompt

    # Inject dependency results into prompt so agent can build on previous work
    if dependency_outputs:
        dep_sections = []
        for dep_id, dep_output in dependency_outputs.items():
            if dep_output:
                dep_sections.append(f"### Results from {dep_id}\n{dep_output.strip()}")
        if dep_sections:
            dep_context = "\n\n".join(dep_sections)
            current_prompt = (
                f"{task.prompt}\n\n"
                f"## Results from previous tasks\n\n"
                f"The following tasks completed before yours. Use their results — "
                f"do NOT redo any work they already did.\n\n"
                f"{dep_context}"
            )

    previous_output = None

    for attempt in range(1 + MAX_RETRIES_ON_MAX_TURNS):
        timeout = max(current_turns * AGENT_TIMEOUT_PER_TURN, 120)

        payload = DispatchPayload(
            task_file_path=task.file_path,
            prompt=current_prompt,
            context=context,
            metadata={
                "type": "task",
                "max_iterations": current_turns,
                "is_subtask": True,
            },
        )

        result = run_task(payload, timeout_override=timeout, on_output=on_output)

        if not result.hit_max_turns:
            # Completed within budget — merge with any previous output
            if previous_output and result.output_text:
                result.output_text = previous_output + "\n" + result.output_text
            elif previous_output:
                result.output_text = previous_output
            logger.info("Task %s finished: %s", task.id, result.status)
            return result

        # Hit max_turns — retry with continuation
        logger.warning(
            "Task %s hit max_turns (%d) on attempt %d, retrying with %d turns",
            task.id, current_turns, attempt + 1, current_turns * 2,
        )
        previous_output = result.output_text or ""
        current_turns *= 2
        current_prompt = (
            f"You are continuing a task that ran out of turns.\n\n"
            f"## Original Task\n{task.prompt}\n\n"
            f"## What was accomplished so far\n{previous_output}\n\n"
            f"## Instructions\n"
            f"Pick up where the previous agent left off. Do NOT redo work that is "
            f"already done. Complete the remaining work."
        )

    # Exhausted retries — return whatever we have
    logger.warning("Task %s exhausted retries, returning partial result", task.id)
    if previous_output and result.output_text:
        result.output_text = previous_output + "\n" + result.output_text
    elif previous_output:
        result.output_text = previous_output
    return result


def _collect_dependency_outputs(
    task: PlannedTask,
    results: dict[str, TaskResult],
) -> dict[str, str] | None:
    """Collect output_text from completed dependency tasks.

    Returns a dict of dep_id → output_text for completed deps with output,
    or None if there are no dependency outputs to forward.
    """
    if not task.depends_on:
        return None

    dep_outputs: dict[str, str] = {}
    for dep_id in task.depends_on:
        dep_result = results.get(dep_id)
        if dep_result and dep_result.output_text:
            dep_outputs[dep_id] = dep_result.output_text

    return dep_outputs if dep_outputs else None


def execute_plan(
    plan: OverseerPlan,
    auto_commit_fn,
    on_task_start=None,
    on_task_finish=None,
    on_task_output=None,
) -> dict[str, TaskResult]:
    """Execute all tasks in a plan, respecting dependency ordering.

    Uses topological levels: tasks within a level run concurrently,
    levels execute sequentially. After all tasks complete, results are
    aggregated per file_path and written once.

    Optional callbacks (all default to None for backward compat):
      on_task_start(task_id)           — called before each task runs
      on_task_finish(task_id, result)  — called after each task completes
      on_task_output(task_id, chunk)   — stderr chunks streamed in real-time

    Returns map of task_id → TaskResult.
    """
    if not plan.tasks:
        return {}

    levels = topological_levels(plan)
    logger.info("Executing plan: %d tasks across %d levels", len(plan.tasks), len(levels))
    for level_idx, level_tasks in enumerate(levels):
        task_descs = ", ".join(f"{t.id} [{t.description}]" for t in level_tasks)
        mode = "concurrent" if len(level_tasks) > 1 else "sequential"
        logger.info("  Level %d (%s): %s", level_idx, mode, task_descs)

    # Mark task files as in_progress (commit workflow only — for frontmatter status)
    files_in_plan: set[str] = {t.file_path for t in plan.tasks}
    for file_path in files_in_plan:
        full_path = os.path.join(VAULT_PATH, file_path)
        if os.path.isfile(full_path):
            update_status(file_path, "in_progress")
            auto_commit_fn(file_path, "in_progress")

    # Execute tasks level by level
    results: dict[str, TaskResult] = {}
    failed_ids: set[str] = set()

    for level_idx, level_tasks in enumerate(levels):
        logger.info("Level %d: %d tasks", level_idx, len(level_tasks))

        # Filter out tasks whose dependencies failed
        runnable: list[PlannedTask] = []
        for task in level_tasks:
            failed_deps = [dep for dep in task.depends_on if dep in failed_ids]
            if failed_deps:
                logger.warning("Skipping task %s: dependency %s failed",
                             task.id, failed_deps)
                results[task.id] = TaskResult(
                    status="failed",
                    error=f"Dependency failed: {', '.join(failed_deps)}",
                )
                failed_ids.add(task.id)
            else:
                runnable.append(task)

        if not runnable:
            continue

        def _run_one(task):
            if on_task_start:
                try:
                    on_task_start(task.id)
                except Exception:
                    pass
            output_cb = (lambda chunk: on_task_output(task.id, chunk)) if on_task_output else None
            dep_outputs = _collect_dependency_outputs(task, results)
            result = _execute_single_task(task, auto_commit_fn, dep_outputs, on_output=output_cb)
            if on_task_finish:
                try:
                    on_task_finish(task.id, result)
                except Exception:
                    pass
            return result

        if len(runnable) == 1:
            task = runnable[0]
            result = _run_one(task)
            results[task.id] = result
            if result.status == "failed":
                failed_ids.add(task.id)
        else:
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_AGENTS) as pool:
                future_to_task = {
                    pool.submit(_run_one, task): task
                    for task in runnable
                }
                for future in as_completed(future_to_task):
                    task = future_to_task[future]
                    try:
                        result = future.result()
                    except Exception as e:
                        logger.exception("Task %s raised exception", task.id)
                        result = TaskResult(status="failed", error=str(e))
                    results[task.id] = result
                    if result.status == "failed":
                        failed_ids.add(task.id)

    # Update task file frontmatter status (commit workflow — marks tasks completed/failed)
    _update_task_statuses(plan, results, auto_commit_fn)

    succeeded = sum(1 for r in results.values() if r.status == "completed")
    failed = sum(1 for r in results.values() if r.status == "failed")
    logger.info("Plan execution complete: %d succeeded, %d failed", succeeded, failed)

    return results


def _update_task_statuses(
    plan: OverseerPlan,
    results: dict[str, TaskResult],
    auto_commit_fn,
) -> None:
    """Update frontmatter status on task files (commit workflow).

    For direct tasks (.davyjones-direct-task), the file doesn't exist so
    this is a no-op. For commit-triggered task notes, marks them as
    completed/failed so they don't re-trigger.
    """
    from collections import defaultdict

    file_tasks: dict[str, list[PlannedTask]] = defaultdict(list)
    for task in plan.tasks:
        file_tasks[task.file_path].append(task)

    for file_path, tasks in file_tasks.items():
        full_path = os.path.join(VAULT_PATH, file_path)
        if not os.path.isfile(full_path):
            continue

        task_results = [results.get(t.id) for t in tasks]
        all_succeeded = all(r and r.status == "completed" for r in task_results)
        final_status = "completed" if all_succeeded else "failed"
        error = None if all_succeeded else "One or more sub-tasks failed"

        update_status(file_path, final_status, TaskResult(status=final_status, error=error))
        auto_commit_fn(file_path, final_status)

        ok_count = sum(1 for r in task_results if r and r.status == "completed")
        logger.info("Status updated for %s: %d/%d tasks succeeded",
                    file_path, ok_count, len(tasks))
