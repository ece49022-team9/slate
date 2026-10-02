import argparse
import json
from pathlib import Path

from tau2.domains.airline.environment import get_environment, get_tasks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    results = []
    for task in get_tasks("base"):
        environment = get_environment()
        tools = {tool.name: tool for tool in environment.get_tools()}
        initial = task.initial_state
        row = {"task_id": task.id, "actions": [], "errors": []}
        try:
            environment.set_state(
                initialization_data=initial.initialization_data if initial else None,
                initialization_actions=initial.initialization_actions
                if initial
                else None,
                message_history=initial.message_history or [] if initial else [],
                strict=True,
            )
            criteria = task.evaluation_criteria
            for action in criteria.actions or [] if criteria else []:
                if action.requestor != "assistant":
                    raise ValueError("Airline gold unexpectedly requests a user tool")
                tool = tools[action.name]
                tool.params.model_validate(action.arguments)
                unknown = set(action.arguments) - set(tool.params.model_fields)
                if unknown:
                    raise ValueError(f"Unknown gold tool parameters: {sorted(unknown)}")
                result = environment.make_tool_call(
                    action.name, action.requestor, **action.arguments
                )
                if isinstance(result, dict) and "error" in result:
                    raise RuntimeError(
                        f"Gold tool returned an error: {result['error']}"
                    )
                if isinstance(result, str) and result.lower().startswith("error"):
                    raise RuntimeError(f"Gold tool returned an error: {result}")
                row["actions"].append(
                    {
                        "name": action.name,
                        "arguments": action.arguments,
                        "result": result.model_dump()
                        if hasattr(result, "model_dump")
                        else result,
                    }
                )
            for assertion in criteria.env_assertions or [] if criteria else []:
                if not environment.run_env_assertion(
                    assertion, raise_assertion_error=False
                ):
                    raise RuntimeError(
                        "Gold reference trajectory fails an environment assertion"
                    )
            row["final_db_hash"] = environment.get_db_hash()
            row["passed"] = True
        except Exception as exc:
            row["passed"] = False
            row["errors"].append({"type": type(exc).__name__, "message": str(exc)})
        results.append(row)
    receipt = {
        "scope": "reference-audit-only-no-agent-score",
        "domain": "airline",
        "split": "base",
        "tasks": len(results),
        "passed": sum(row["passed"] for row in results),
        "results": results,
    }
    args.output.write_text(json.dumps(receipt, indent=2, default=str))
    print(f"slate.eval: airline gold audit {receipt['passed']}/{receipt['tasks']}")


if __name__ == "__main__":
    main()
