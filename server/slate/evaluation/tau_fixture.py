import argparse
import json
import random
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from tau2.gym.gym_agent import AgentGymEnv


class SeededAgentGymEnv(AgentGymEnv):
    def _get_orchestrator(self):
        orchestrator = super()._get_orchestrator()
        orchestrator.seed = self.np_random_seed
        return orchestrator


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--user-model", required=True)
    parser.add_argument("--max-steps", type=int, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    random.seed(args.seed)
    environment = SeededAgentGymEnv(
        domain="airline",
        task_id=args.task_id,
        max_steps=args.max_steps,
        user_llm="openai/" + args.user_model,
        user_llm_args={"seed": args.seed, "reasoning_effort": "low"},
    )
    app = FastAPI()
    lock = threading.Lock()
    state = {"initialized": False, "terminated": False, "steps": []}

    @app.get("/health")
    def health():
        return {"ready": True}

    @app.post("/reset")
    def reset():
        with lock:
            if state["initialized"]:
                raise HTTPException(409, "Fixture already initialized")
            observation, info = environment.reset(seed=args.seed)
            if not observation:
                raise HTTPException(
                    500,
                    "No official initial observation; inspect fixture.log",
                )
            state["initialized"] = True
            metadata = {
                "observation": observation,
                "policy": info["policy"],
                "tools": [tool.openai_schema["function"] for tool in info["tools"]],
            }
            (args.output / "public-fixture.json").write_text(
                json.dumps(metadata, indent=2)
            )
            return metadata

    @app.post("/step")
    def step(body: dict):
        with lock:
            if state["terminated"]:
                return {
                    "observation": "The official simulation has ended.",
                    "terminated": True,
                }
            if "tool" in body:
                action = json.dumps(
                    {"name": body["tool"], "arguments": body["arguments"]}
                )
            else:
                action = body["message"]
            observation, reward, terminated, truncated, info = environment.step(action)
            result = {
                "observation": observation,
                "reward": reward,
                "terminated": terminated or truncated,
                "truncated": truncated,
            }
            state["steps"].append({"action": body, **result})
            state["terminated"] = terminated or truncated
            state["last"] = result
            if terminated or truncated:
                simulation = json.loads(info["simulation_run"])
                if not simulation:
                    raise HTTPException(
                        500,
                        "Official orchestrator failed; inspect fixture.log",
                    )
                state["simulation"] = simulation
                state["reward_info"] = json.loads(info["reward_info"])
            (args.output / "trajectory.json").write_text(json.dumps(state, indent=2))
            return result

    @app.get("/status")
    def status():
        with lock:
            return state

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
