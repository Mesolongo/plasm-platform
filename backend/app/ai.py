"""AI layer gate. All live Claude calls pass through here.

Without credentials the endpoints stay wired but return a clear 503, so the
frontend can show "connect an API key" instead of breaking. Credentials come
from ANTHROPIC_API_KEY or an `ant auth login` profile.
"""
import json
import os
from pathlib import Path

from .storage import ROOT

ARCHITECT_PROMPT = ROOT / "ai" / "prompts" / "model_architect.md"
INTERPRETER_PROMPT = ROOT / "ai" / "prompts" / "interpreter.md"
ASSISTANT_PROMPT = ROOT / "ai" / "prompts" / "assistant.md"
PUBLISHER_PROMPT = ROOT / "ai" / "prompts" / "publisher.md"

NOT_CONFIGURED = (
    "AI features need Anthropic API credentials. Set ANTHROPIC_API_KEY "
    "(key from https://platform.claude.com) or run `ant auth login`, then restart the server."
)


def is_configured() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    # ant auth login stores profiles under ~/.config/anthropic/credentials/
    cred_dir = Path.home() / ".config" / "anthropic" / "credentials"
    return cred_dir.is_dir() and any(cred_dir.glob("*.json"))


def interpret(request: dict, assessment: dict, study_description: str) -> dict:
    """Report writer: assessment (all numbers + verdicts) -> narrative sections.

    The model only sees engine-computed values; the prompt forbids inventing numbers.
    """
    from typing import Optional
    import anthropic
    from pydantic import BaseModel

    class Interpretation(BaseModel):
        results_narrative: str
        discussion: str
        conclusion: str
        managerial_implications: str
        limitations: str

    payload = {
        "study_description": study_description or "not provided",
        "model": {
            "constructs": [
                {"name": c["name"], "measurement": c["measurement"],
                 "n_indicators": len(c.get("indicators") or c.get("dimensions") or [])}
                for c in request["constructs"]
            ],
            "paths": [f"{p['from_construct']} -> {p['to_construct']}" for p in request["paths"]],
            "interactions": [f"{i['iv']} x {i['moderator']} (two-stage)"
                             for i in request.get("interactions") or []],
        },
        "assessment": assessment,
    }

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model="claude-opus-4-8",
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=INTERPRETER_PROMPT.read_text(),
        messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
        output_format=Interpretation,
    )
    return json.loads(response.parsed_output.model_dump_json())


def chat(request: dict, assessment: dict, history: list[dict], message: str,
         mga: dict | None = None) -> str:
    """Research assistant: answers questions grounded in this analysis only.

    History is client-held (stateless server); the assessment travels in the
    system prompt so every turn is grounded in the same computed statistics.
    """
    import anthropic

    grounding = {
        "model": {
            "constructs": [{"name": c["name"], "measurement": c["measurement"]}
                           for c in request["constructs"]],
            "paths": [f"{p['from_construct']} -> {p['to_construct']}"
                      for p in request["paths"]],
        },
        "assessment": assessment,
        "mga": mga,
    }
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=2000,
        system=ASSISTANT_PROMPT.read_text()
        + "\n\nAnalysis on the table:\n" + json.dumps(grounding, indent=2),
        messages=[*history[-20:], {"role": "user", "content": message}],
    )
    return "".join(b.text for b in response.content if b.type == "text")


def draft_manuscript(request: dict, assessment: dict, reviewer_concerns: list[dict],
                     study_description: str, target_journal: str, mode: str) -> dict:
    """Publishing assistant: assessment + anticipated reviewer concerns -> submission
    front matter (title options, abstract, keywords, highlights, contribution) plus a
    pre-emptive response to each concern, in the register of the target outlet.

    Like interpret(), the model only sees engine-computed values and the deterministic
    reviewer concerns; the prompt forbids inventing numbers, verdicts, or references.
    """
    import anthropic
    from pydantic import BaseModel

    class ReviewerResponse(BaseModel):
        concern: str
        response: str

    class Manuscript(BaseModel):
        title_options: list[str]
        abstract: str
        keywords: list[str]
        highlights: list[str]
        contribution_statement: str
        reviewer_responses: list[ReviewerResponse]

    payload = {
        "mode": mode,
        "target_journal": target_journal or "not specified",
        "study_description": study_description or "not provided",
        "model": {
            "constructs": [{"name": c["name"], "measurement": c["measurement"]}
                           for c in request["constructs"]],
            "paths": [f"{p['from_construct']} -> {p['to_construct']}" for p in request["paths"]],
            "interactions": [f"{i['iv']} x {i['moderator']} (two-stage)"
                             for i in request.get("interactions") or []],
        },
        "assessment": assessment,
        "anticipated_reviewer_concerns": reviewer_concerns,
    }

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model="claude-opus-4-8",
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=PUBLISHER_PROMPT.read_text(),
        messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
        output_format=Manuscript,
    )
    return json.loads(response.parsed_output.model_dump_json())


def propose_model(variables: list[dict], study_description: str) -> dict:
    """Model architect: variable dictionary -> model spec (schema-validated)."""
    import sys
    sys.path.insert(0, str(ROOT / "ai"))
    from model_architect import ModelSpec  # reuse the pipeline's schema
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model="claude-opus-4-8",
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=ARCHITECT_PROMPT.read_text(),
        messages=[{
            "role": "user",
            "content": (
                f"Study description: {study_description or 'not provided'}\n\n"
                f"Variable dictionary:\n{json.dumps({'variables': variables}, indent=2)}"
            ),
        }],
        output_format=ModelSpec,
    )
    return json.loads(response.parsed_output.model_dump_json())
