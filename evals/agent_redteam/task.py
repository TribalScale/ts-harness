"""Inspect AI task for the agent red-team dataset.

Loaded only when inspect-ai is installed. The pytest scorer does not import this module.
"""

from ts_harness.evals.redteam import load_dataset, run_case
from ts_harness.evals.harness import load_test_config

try:
    from inspect_ai import Task, task
    from inspect_ai.dataset import Sample
    from inspect_ai.scorer import Score, accuracy, scorer, stderr
    from inspect_ai.solver import Generate, TaskState, solver
except ImportError:  # pragma: no cover - optional extra
    Task = task = Sample = Score = None


if task is not None:

    @solver
    def enterprise_agent():
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            from ts_harness.agent.provider import build_provider

            case = dict(state.metadata)
            config = load_test_config()
            provider = build_provider(config.get_agent(str(case["agent_id"])).model)
            failures = run_case(config, case, provider)
            state.metadata["failures"] = failures
            state.output.completion = "refused" if not failures else "failed"
            return state

        return solve

    @scorer(metrics=[accuracy(), stderr()])
    def refused():
        async def score(state: TaskState, target) -> Score:
            failures = list(state.metadata.get("failures") or [])
            return Score(value="C" if not failures else "I", answer=",".join(failures))

        return score

    @task
    def agent_redteam():
        samples = [
            Sample(input=case["user_text"], target="refused", metadata=case)
            for case in load_dataset()
        ]
        return Task(dataset=samples, solver=[enterprise_agent()], scorer=refused())
