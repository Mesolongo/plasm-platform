#!/usr/bin/env python3
"""Phase 0 AI pipeline prototype: variable dictionary in -> formal PLS-SEM model spec out.

The spec is a structured object (validated by the API against the schema below) that maps
1:1 to seminr syntax, so the engine can estimate exactly what the researcher approved.

Usage:
    python3 model_architect.py corp_rep_variable_dictionary.json \
        --study "Drivers of corporate reputation and customer loyalty" \
        --out model_spec.json

Requires Anthropic credentials (ANTHROPIC_API_KEY, or an `ant auth login` profile).
"""
import argparse
import json
from pathlib import Path
from typing import List, Literal, Optional

import anthropic
from pydantic import BaseModel

PROMPT_PATH = Path(__file__).parent / "prompts" / "model_architect.md"


class Construct(BaseModel):
    name: str
    indicators: List[str]
    measurement: Literal["reflective", "formative", "single_item"]
    rationale: str


class StructuralPath(BaseModel):
    from_construct: str
    to_construct: str
    rationale: str


class ExcludedVariable(BaseModel):
    variable: str
    reason: str


class ModelSpec(BaseModel):
    constructs: List[Construct]
    paths: List[StructuralPath]
    mediators: List[str]
    moderator: Optional[str]
    excluded_variables: List[ExcludedVariable]
    summary: str


def propose_model(variable_dictionary: dict, study_description: str) -> ModelSpec:
    client = anthropic.Anthropic()
    response = client.messages.parse(
        model="claude-opus-4-8",
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=PROMPT_PATH.read_text(),
        messages=[{
            "role": "user",
            "content": (
                f"Study description: {study_description or 'not provided'}\n\n"
                f"Variable dictionary:\n{json.dumps(variable_dictionary, indent=2)}"
            ),
        }],
        output_format=ModelSpec,
    )
    return response.parsed_output


def render_seminr(spec: ModelSpec) -> str:
    """Render the approved spec as seminr R code — the contract with the engine."""
    lines = ["measurement <- constructs("]
    for c in spec.constructs:
        if c.measurement == "single_item":
            lines.append(f'  composite("{c.name}", single_item("{c.indicators[0]}")),')
        else:
            items = ", ".join(f'"{i}"' for i in c.indicators)
            mode = ", weights = mode_B" if c.measurement == "formative" else ""
            lines.append(f'  composite("{c.name}", c({items}){mode}),')
    lines[-1] = lines[-1].rstrip(",")
    lines.append(")")
    lines.append("structural <- relationships(")
    for p in spec.paths:
        lines.append(f'  paths(from = "{p.from_construct}", to = "{p.to_construct}"),')
    lines[-1] = lines[-1].rstrip(",")
    lines.append(")")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dictionary", help="variable dictionary JSON file")
    ap.add_argument("--study", default="", help="study description")
    ap.add_argument("--out", default="model_spec.json", help="output spec path")
    args = ap.parse_args()

    variable_dictionary = json.loads(Path(args.dictionary).read_text())
    spec = propose_model(variable_dictionary, args.study)

    Path(args.out).write_text(spec.model_dump_json(indent=2))
    print(f"Model spec written to {args.out}\n")
    print("--- seminr rendering (what the engine would estimate) ---")
    print(render_seminr(spec))


if __name__ == "__main__":
    main()
