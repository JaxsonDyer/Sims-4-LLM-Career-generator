"""
OpenRouter client.

The model's only job is to turn a free-text idea into a CareerSpec. It never
writes XML, never picks instance ids, never touches bytes. If it returns
something invalid we feed the validation errors back and ask for a fix, up to
a few attempts, then give up with a clear message rather than shipping a
broken package.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from .schema import (
    CareerSpec, SpecError, JSON_SCHEMA_HINT, KNOWN_SKILLS, MAX_PAY_PER_HOUR,
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"

RULES = f"""Rules you must follow:
- Output a single JSON object. No prose, no markdown fences, no commentary.
- Exactly one branch must have "branches_at": null. That is the base track.
- If the career branches, add 1 or 2 more branches with "branches_at" set to
  the base track's LAST level number; careers only split after the base
  track ends. Branch levels continue numbering from there.
- Level numbers within a branch must be consecutive with no gaps.
- Pay must increase with level, unless the player explicitly asks for a
  different arc (e.g. a punishing grind that only pays off at the top).
  If the player gives NO pay guidance, default to entry level 12-25/hour and
  a 10-level top of 300-800/hour, scaled smoothly between. If the player DOES
  give pay figures - a target number, "$X a day", "$X a week", a described
  curve like "meager at first, huge at the end" - hit those numbers as
  closely as the schema allows instead of the typical range above, even when
  they land far outside it. Convert any daily or weekly figure to
  pay_per_hour yourself using that level's own hours_per_day and work_days
  count. pay_per_hour must stay under {MAX_PAY_PER_HOUR} - if the player's
  request divides out to more than that, get as close as you can under the
  cap by adjusting hours_per_day/work_days first, then note the shortfall is
  unavoidable rather than silently reverting to a modest, "safe" number.
- work_days and hours_per_day may differ level to level to match a described
  lifestyle arc - e.g. every day of the week and long hours at the bottom of
  a grind, then very few work_days once the player describes reaching the
  top and having free time.
- required_skills may only use these skill names:
{", ".join(sorted(KNOWN_SKILLS))}
- Skill levels are 1-10. Early career levels should need few or no skills;
  a career the player describes as hard to break into can demand several
  high-level skills even at level 1.
- work_days uses lowercase weekday names. start_hour is 0-23. hours_per_day
  is 1-12.
- Titles are short and flavourful. Descriptions are one sentence.
- promotion_message is written in second person, addressed to the player."""

SYSTEM_PROMPT = f"""You design careers for The Sims 4. You output JSON only.

Given a player's idea, produce a complete, balanced career track.

{RULES}

Match the tone of the player's idea. If they ask for something comedic, be
comedic. If they ask for something grounded, stay grounded.

Schema:
{JSON_SCHEMA_HINT}"""

REVISE_SYSTEM_PROMPT = f"""You revise an existing Sims 4 career specification to
match a player's requested change. You output JSON only.

You will be given the current career as JSON and an instruction describing
what the player wants changed. Apply that change and return the COMPLETE
updated career as a single JSON object, in the same schema. Leave everything
the player did not ask to change as close to the original as you reasonably
can - do not regenerate the career from scratch over one small request.

{RULES}

Schema:
{JSON_SCHEMA_HINT}"""


class LLMError(Exception):
    """Raised when the model cannot be reached or cannot produce a valid spec."""


@dataclass
class GenerationResult:
    spec: CareerSpec
    model: str
    attempts: int
    raw_response: str


def _extract_json(text: str) -> str:
    """
    Pull a JSON object out of a model response.

    Models wrap JSON in fences, prepend "Here's your career:", or emit
    reasoning first. Rather than trusting them, find the outermost balanced
    object.
    """
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)

    start = text.find("{")
    if start == -1:
        raise SpecError("no JSON object found in the model's response")

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise SpecError("model's JSON object is unterminated (response cut off?)")


class OpenRouterClient:
    """Minimal OpenRouter chat client. Uses urllib so there are no extra deps."""

    def __init__(self, api_key: str, timeout: int = 180):
        if not api_key or not api_key.strip():
            raise LLMError("no OpenRouter API key set")
        self.api_key = api_key.strip()
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # OpenRouter uses these for attribution on its leaderboards.
            "HTTP-Referer": "https://github.com/local/career-forge",
            "X-Title": "Sims 4 Career Forge",
        }

    def chat(self, model: str, messages: list[dict[str, str]],
             temperature: float = 0.8, max_tokens: int = 8000) -> str:
        """Send a chat completion request and return the assistant's text."""
        payload = json.dumps({
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode("utf-8")

        request = urllib.request.Request(
            OPENROUTER_URL, data=payload, headers=self._headers(), method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            if exc.code == 401:
                raise LLMError("OpenRouter rejected the API key (401)") from exc
            if exc.code == 402:
                raise LLMError(
                    "OpenRouter says this account is out of credit (402)"
                ) from exc
            if exc.code == 429:
                raise LLMError(
                    "Rate limited by OpenRouter (429). Wait a moment and retry."
                ) from exc
            raise LLMError(f"OpenRouter HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"could not reach OpenRouter: {exc.reason}") from exc
        except TimeoutError as exc:
            raise LLMError(
                f"OpenRouter did not respond within {self.timeout}s"
            ) from exc

        if "error" in body and body["error"]:
            raise LLMError(f"OpenRouter error: {body['error']}")
        try:
            return body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise LLMError(f"unexpected response shape: {body}") from exc

    def list_models(self) -> list[str]:
        """Fetch available model ids, for the GUI dropdown."""
        request = urllib.request.Request(MODELS_URL, headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
            return sorted(m["id"] for m in body.get("data", []) if "id" in m)
        except Exception as exc:
            raise LLMError(f"could not list models: {exc}") from exc


def generate_career(client: OpenRouterClient, idea: str, model: str,
                    max_attempts: int = 3, temperature: float = 0.8,
                    on_progress=None) -> GenerationResult:
    """
    Turn a free-text idea into a validated CareerSpec.

    On validation failure the errors go back to the model as a repair request.
    This is what makes "slap in an idea" reliable rather than a coin flip:
    the model gets told exactly what it broke, in its own terms.
    """
    def report(message: str) -> None:
        if on_progress:
            on_progress(message)

    if not idea.strip():
        raise LLMError("no career idea given")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Career idea: {idea.strip()}"},
    ]

    last_error = ""
    raw = ""
    for attempt in range(1, max_attempts + 1):
        report(f"Asking {model} (attempt {attempt}/{max_attempts})...")
        raw = client.chat(model, messages, temperature=temperature)

        try:
            spec = CareerSpec.from_json(_extract_json(raw))
            problems = spec.validate()
        except SpecError as exc:
            problems = [str(exc)]
            spec = None

        if spec is not None and not problems:
            report(f"Valid career spec on attempt {attempt}.")
            return GenerationResult(
                spec=spec, model=model, attempts=attempt, raw_response=raw
            )

        last_error = "\n".join(f"- {p}" for p in problems)
        report(f"Spec had {len(problems)} problem(s); asking for a fix.")

        messages.append({"role": "assistant", "content": raw})
        messages.append({
            "role": "user",
            "content": (
                "That career failed validation. Fix these problems and return "
                "the corrected JSON object only:\n\n" + last_error
            ),
        })

    raise LLMError(
        f"{model} could not produce a valid career in {max_attempts} attempts. "
        f"Last problems:\n{last_error}"
    )


def revise_career(client: OpenRouterClient, spec: CareerSpec, instruction: str,
                  model: str, max_attempts: int = 3, temperature: float = 0.7,
                  on_progress=None) -> GenerationResult:
    """
    Apply a free-text change to an already-drafted CareerSpec.

    Same validate-and-repair loop as generate_career, except the model starts
    from the existing spec instead of a blank idea, so a small ask ("make the
    top level pay more") does not need a full redraft to fix.
    """
    def report(message: str) -> None:
        if on_progress:
            on_progress(message)

    if not instruction.strip():
        raise LLMError("no change requested")

    messages = [
        {"role": "system", "content": REVISE_SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"Current career JSON:\n{spec.to_json()}\n\n"
            f"Requested change: {instruction.strip()}\n\n"
            "Return the complete updated career as a single JSON object."
        )},
    ]

    last_error = ""
    raw = ""
    for attempt in range(1, max_attempts + 1):
        report(f"Asking {model} to revise (attempt {attempt}/{max_attempts})...")
        raw = client.chat(model, messages, temperature=temperature)

        try:
            revised = CareerSpec.from_json(_extract_json(raw))
            problems = revised.validate()
        except SpecError as exc:
            problems = [str(exc)]
            revised = None

        if revised is not None and not problems:
            report("Revision applied.")
            return GenerationResult(
                spec=revised, model=model, attempts=attempt, raw_response=raw
            )

        last_error = "\n".join(f"- {p}" for p in problems)
        report(f"Revised spec had {len(problems)} problem(s); asking for a fix.")

        messages.append({"role": "assistant", "content": raw})
        messages.append({
            "role": "user",
            "content": (
                "That revision failed validation. Fix these problems and "
                "return the corrected JSON object only:\n\n" + last_error
            ),
        })

    raise LLMError(
        f"{model} could not produce a valid revision in {max_attempts} attempts. "
        f"Last problems:\n{last_error}"
    )
