import argparse
import json
from pathlib import Path

from evaluate_qa import get_anscheck_prompt
from openai import OpenAI


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("references", type=Path)
    parser.add_argument("hypotheses", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    references = {
        row["question_id"]: row for row in json.loads(args.references.read_text())
    }
    client = OpenAI()
    with args.output.open("x") as output:
        for line in args.hypotheses.read_text().splitlines():
            row = json.loads(line)
            reference = references[row["question_id"]]
            prompt = get_anscheck_prompt(
                reference["question_type"],
                reference["question"],
                reference["answer"],
                row["hypothesis"],
                abstention="_abs" in row["question_id"],
            )
            result = client.responses.create(
                model=args.model,
                input=prompt,
                max_output_tokens=512,
                reasoning={"effort": "low"},
            )
            verdict = result.output_text.strip().lower()
            if verdict not in ("yes", "no"):
                raise RuntimeError(f"Judge returned a nonbinary verdict: {verdict!r}")
            row.update(
                judge_model=result.model,
                judge_protocol="pinned-upstream-rubric-custom-sol-responses-judge",
                judge_response_id=result.id,
                correct=verdict == "yes",
                judge_response=verdict,
            )
            output.write(json.dumps(row) + "\n")
            output.flush()
            print(f"slate.eval: graded {row['question_id']} {row['arm']}: {verdict}")


if __name__ == "__main__":
    main()
