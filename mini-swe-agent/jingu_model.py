"""JinguModel — LitellmModel subclass with phase-boundary cognition enforcement.

Two capabilities beyond LitellmModel:

1. **submit_phase_record tool** (Plan B — strong version):
   Every LLM query includes a `submit_phase_record` tool alongside BASH_TOOL.
   The agent MUST call this tool at phase boundaries to submit a structured
   PhaseRecord. This is the ONLY way to complete a phase — fallback extraction
   is diagnostic only, never a substitute for declaration.

2. **structured_extract()** (Plan A — now diagnostic fallback):
   Independent LLM call with response_format=json_schema for post-hoc extraction.
   Used for telemetry/diagnosis when agent fails to submit via tool call.
   Cannot produce an admitted phase record.

Contract (three iron rules):
  - Phase completion = an admitted PhaseRecord exists (from tool submission)
  - Transition = upstream record admitted
  - Fallback extraction = diagnostic only, never admission

Usage:
    model = JinguModel(model_name="bedrock/...", model_kwargs={...})

    # Set current phase (step_sections calls this before each query):
    model.set_current_phase("ANALYZE", schema={...})

    # Normal step (bash + submit_phase_record tools):
    message = model.query(messages)

    # If agent called submit_phase_record, retrieve it:
    submitted = model.pop_submitted_phase_record()
    # submitted is a dict matching the phase schema, or None
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import litellm

from minisweagent.models.litellm_model import LitellmModel, LitellmModelConfig
from minisweagent.models.utils.actions_toolcall import BASH_TOOL
from minisweagent.models.utils.retry import retry

logger = logging.getLogger("jingu_model")


# ---------------------------------------------------------------------------
# submit_phase_record tool definition
# ---------------------------------------------------------------------------

def _build_phase_record_tool(phase: str, schema: dict[str, Any]) -> dict:
    """Build a submit_phase_record tool definition for the current phase.

    The tool's parameters ARE the phase schema — the agent fills in the
    structured fields directly as tool call arguments.
    """
    # Build phase-specific description with field guidance
    field_guidance = ""
    if phase == "ANALYZE":
        field_guidance = (
            " For ANALYZE: put your identified root cause (with file:line) in 'root_cause', "
            "put the step-by-step causal chain in 'causal_chain', "
            "and list evidence file:line references in 'evidence_refs'. "
            "Do NOT put analysis content in 'observations' or 'claims' — "
            "those fields are not checked by the gate."
        )
    elif phase == "EXECUTE":
        field_guidance = (
            " For EXECUTE: put your fix plan (referencing the root cause) in 'plan'."
        )

    return {
        "type": "function",
        "function": {
            "name": "submit_phase_record",
            "description": (
                f"Submit your structured phase record for the {phase} phase. "
                f"You MUST call this tool when you have completed your work in "
                f"the current phase. This is the ONLY way to complete a phase "
                f"and proceed to the next one. Fill in ALL required fields "
                f"based on your reasoning above.{field_guidance}"
            ),
            "parameters": schema,
        },
    }


# Fallback tool when no schema is available (e.g. UNDERSTAND phase)
_SUBMIT_PHASE_RECORD_FALLBACK = {
    "type": "function",
    "function": {
        "name": "submit_phase_record",
        "description": (
            "Submit your structured phase record for the current phase. "
            "Call this when you have completed your work in the current phase."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "phase": {
                    "type": "string",
                    "description": "The phase you are completing (e.g. OBSERVE, ANALYZE, DECIDE, EXECUTE, JUDGE)",
                },
                "summary": {
                    "type": "string",
                    "description": "Brief summary of what you accomplished in this phase",
                },
            },
            "required": ["phase", "summary"],
            "additionalProperties": False,
        },
    },
}


@dataclass
class ExtractRecord:
    """Records a structured_extract call for traj observability (Plan-C)."""
    phase: str
    extraction_prompt: str
    schema: dict
    schema_name: str
    phase_hint: str
    response_raw: str | None = None
    response_parsed: dict | None = None
    response_dump: dict | None = None
    success: bool = False
    error: str | None = None
    cost: float = 0.0
    timestamp_request: float = 0.0
    timestamp_response: float = 0.0


def is_extraction_message(msg: dict) -> bool:
    """Check if a traj message is a structured_extract entry (Plan-C)."""
    return (msg.get("extra", {}).get("type", "")
            .startswith("structured_extract_"))


class JinguModel(LitellmModel):
    """LitellmModel with phase-boundary cognition enforcement.

    Adds submit_phase_record tool to every query. Agent must call it at
    phase boundaries. Fallback extraction (structured_extract) is diagnostic only.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Current phase state — set by step_sections before each query
        self._current_phase: str | None = None
        self._current_phase_schema: dict[str, Any] | None = None
        # Last submitted phase record from tool call (consumed by step_sections)
        self._submitted_phase_record: dict[str, Any] | None = None
        self._submitted_phase_record_phase: str | None = None

    # -- Phase state management (called by step_sections) --

    def set_current_phase(self, phase: str, schema: dict[str, Any] | None = None) -> None:
        """Set the current phase and its schema for submit_phase_record tool."""
        self._current_phase = phase.upper()
        self._current_phase_schema = schema
        logger.info("set_current_phase: phase=%s schema_available=%s",
                     self._current_phase, schema is not None)

    def pop_submitted_phase_record(self) -> dict[str, Any] | None:
        """Pop and return the last tool-submitted phase record, or None.

        This is the ONLY way to get an admitted phase record.
        Calling this clears the stored record.
        """
        record = self._submitted_phase_record
        phase = self._submitted_phase_record_phase
        self._submitted_phase_record = None
        self._submitted_phase_record_phase = None
        if record is not None:
            logger.info("pop_submitted_phase_record: phase=%s fields=%s",
                        phase, list(record.keys()))
        return record

    # -- Override _query to inject submit_phase_record tool --

    def _query(self, messages: list[dict[str, str]], **kwargs):
        """Override to add submit_phase_record tool alongside BASH_TOOL."""
        # Build phase-specific tool if schema available
        if self._current_phase and self._current_phase_schema:
            phase_tool = _build_phase_record_tool(
                self._current_phase, self._current_phase_schema
            )
        elif self._current_phase:
            phase_tool = _SUBMIT_PHASE_RECORD_FALLBACK
        else:
            phase_tool = _SUBMIT_PHASE_RECORD_FALLBACK

        tools = [BASH_TOOL, phase_tool]

        try:
            return litellm.completion(
                model=self.config.model_name,
                messages=messages,
                tools=tools,
                **(self.config.model_kwargs | kwargs),
            )
        except litellm.exceptions.AuthenticationError as e:
            e.message += " You can permanently set your API key with `mini-extra config set KEY VALUE`."
            raise e

    # -- Override _parse_actions to intercept submit_phase_record --

    def _parse_actions(self, response) -> list[dict]:
        """Parse tool calls, intercepting submit_phase_record submissions.

        submit_phase_record calls are stored (not executed as bash).
        bash calls are returned as normal actions.
        """
        from minisweagent.exceptions import FormatError
        from jinja2 import StrictUndefined, Template

        tool_calls = response.choices[0].message.tool_calls or []

        if not tool_calls:
            raise FormatError(
                {
                    "role": "user",
                    "content": Template(
                        self.config.format_error_template,
                        undefined=StrictUndefined,
                    ).render(
                        error=(
                            "No tool calls found in the response. "
                            "Every response MUST include at least one tool call. "
                            "Use the bash tool for commands, or submit_phase_record "
                            "to complete the current phase."
                        )
                    ),
                    "extra": {"interrupt_type": "FormatError"},
                }
            )

        bash_actions = []
        for tc in tool_calls:
            if tc.function.name == "submit_phase_record":
                # Intercept: parse and store, don't execute
                try:
                    args = json.loads(tc.function.arguments)
                    self._submitted_phase_record = args
                    self._submitted_phase_record_phase = self._current_phase
                    logger.info(
                        "submit_phase_record intercepted: phase=%s fields=%s",
                        self._current_phase,
                        list(args.keys()),
                    )
                except Exception as e:
                    logger.error(
                        "submit_phase_record parse error: %s", e
                    )
                continue

            if tc.function.name == "bash":
                try:
                    args = json.loads(tc.function.arguments)
                except Exception as e:
                    raise FormatError(
                        {
                            "role": "user",
                            "content": Template(
                                self.config.format_error_template,
                                undefined=StrictUndefined,
                            ).render(
                                error=f"Error parsing bash tool call arguments: {e}."
                            ),
                            "extra": {"interrupt_type": "FormatError"},
                        }
                    )
                if "command" not in args:
                    raise FormatError(
                        {
                            "role": "user",
                            "content": Template(
                                self.config.format_error_template,
                                undefined=StrictUndefined,
                            ).render(
                                error="Missing 'command' argument in bash tool call."
                            ),
                            "extra": {"interrupt_type": "FormatError"},
                        }
                    )
                bash_actions.append({
                    "command": args["command"],
                    "tool_call_id": tc.id,
                })
                continue

            # Unknown tool
            raise FormatError(
                {
                    "role": "user",
                    "content": Template(
                        self.config.format_error_template,
                        undefined=StrictUndefined,
                    ).render(
                        error=f"Unknown tool '{tc.function.name}'. Use 'bash' or 'submit_phase_record'."
                    ),
                    "extra": {"interrupt_type": "FormatError"},
                }
            )

        # If agent ONLY submitted phase record (no bash), we still need a valid
        # step. Return empty bash_actions — the tool result for submit_phase_record
        # is handled by format_observation_messages override.
        return bash_actions

    def format_observation_messages(
        self, message: dict, outputs: list[dict], template_vars: dict | None = None,
    ) -> list[dict]:
        """Override to inject tool result for submit_phase_record tool call.

        The Anthropic API requires every tool_call to have a corresponding tool
        result message. We synthesize one for submit_phase_record since it's
        intercepted (not executed as bash).
        """
        # Get normal bash tool results from parent
        result_msgs = super().format_observation_messages(message, outputs, template_vars)

        # Check if the raw response had a submit_phase_record tool call
        raw_response = message.get("extra", {}).get("response", {})
        tool_calls = (
            raw_response.get("choices", [{}])[0]
            .get("message", {})
            .get("tool_calls", [])
            or []
        )
        for tc in tool_calls:
            tc_fn = tc.get("function", {})
            if tc_fn.get("name") == "submit_phase_record":
                tc_id = tc.get("id", "")
                if self._submitted_phase_record is not None:
                    ack_content = (
                        f"Phase record for {self._submitted_phase_record_phase or 'unknown'} "
                        f"received. The record will be evaluated by the admission gate."
                    )
                else:
                    ack_content = (
                        "Phase record submission failed to parse. "
                        "Please try again with valid JSON arguments."
                    )
                result_msgs.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": ack_content,
                    "extra": {
                        "type": "phase_record_ack",
                        "phase": self._submitted_phase_record_phase,
                        "success": self._submitted_phase_record is not None,
                        "timestamp": time.time(),
                    },
                })

        return result_msgs

    def structured_extract(
        self,
        accumulated_text: str,
        phase: str,
        schema: dict[str, Any],
        *,
        phase_hint: str = "",
        max_tokens: int = 2048,
    ) -> dict[str, Any] | None:
        """Extract structured phase data from accumulated agent text.

        Makes an independent LLM call with response_format=json_schema,
        which uses grammar-constrained sampling to guarantee schema-valid output.

        Args:
            accumulated_text: All assistant text from the current phase.
            phase: Phase name (e.g. "ANALYZE", "EXECUTE").
            schema: JSON Schema the response must conform to.
            phase_hint: Optional success criteria from cognition spec, prepended to prompt.
            max_tokens: Max tokens for extraction response.

        Returns:
            Parsed dict matching schema, or None on failure.
        """
        self._last_extract_record = None

        if not accumulated_text or not accumulated_text.strip():
            logger.warning("structured_extract: empty accumulated_text for phase=%s", phase)
            return None

        # Build extraction prompt — ask LLM to summarize its own reasoning
        # into the structured format. This is a separate call from the agent loop.
        # p226-03: prepend phase_hint (from cognition success_criteria) when available
        _hint_block = ""
        if phase_hint:
            _hint_block = (
                f"Success criteria for the {phase} phase:\n"
                f"{phase_hint}\n\n"
                f"Focus your extraction on evidence that addresses these criteria.\n\n"
            )
        extraction_prompt = (
            f"You are summarizing your own reasoning from the {phase} phase into structured JSON.\n\n"
            f"{_hint_block}"
            f"Below is everything you wrote during the {phase} phase. "
            f"Extract the key information into the required JSON schema fields.\n\n"
            f"--- BEGIN {phase} PHASE OUTPUT ---\n"
            f"{accumulated_text}\n"
            f"--- END {phase} PHASE OUTPUT ---\n\n"
            f"Output ONLY the JSON object matching the required schema. "
            f"Every field must be grounded in the text above — do not invent information."
        )

        _schema_name = f"{phase.lower()}_extraction"
        messages = [
            {"role": "user", "content": extraction_prompt},
        ]

        # Use response_format for grammar-constrained sampling.
        # litellm translates this to Bedrock's outputConfig or tool-use trick.
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": _schema_name,
                "schema": schema,
            },
        }

        _ts_request = time.time()
        try:
            for attempt in retry(logger=logger, abort_exceptions=self.abort_exceptions):
                with attempt:
                    response = litellm.completion(
                        model=self.config.model_name,
                        messages=messages,
                        response_format=response_format,
                        max_tokens=max_tokens,
                        temperature=0,
                        # Don't pass tools — this is a pure JSON extraction call
                    )
            _ts_response = time.time()

            # Extract JSON from response
            content = response.choices[0].message.content
            if not content:
                logger.warning("structured_extract: empty response content for phase=%s", phase)
                self._last_extract_record = ExtractRecord(
                    phase=phase, extraction_prompt=extraction_prompt,
                    schema=schema, schema_name=_schema_name, phase_hint=phase_hint,
                    response_raw=None, success=False, error="empty_response_content",
                    timestamp_request=_ts_request, timestamp_response=_ts_response,
                )
                return None

            # Compute cost
            _cost = 0.0
            try:
                _cost = litellm.completion_cost(completion_response=response)
            except Exception:
                pass

            parsed = json.loads(content)
            logger.info(
                "structured_extract: phase=%s fields=%s",
                phase,
                list(parsed.keys()),
            )

            # Plan-C: record successful extraction
            _resp_dump = None
            try:
                _resp_dump = response.model_dump()
            except Exception:
                pass
            self._last_extract_record = ExtractRecord(
                phase=phase, extraction_prompt=extraction_prompt,
                schema=schema, schema_name=_schema_name, phase_hint=phase_hint,
                response_raw=content, response_parsed=parsed,
                response_dump=_resp_dump, success=True, cost=_cost,
                timestamp_request=_ts_request, timestamp_response=_ts_response,
            )
            return parsed

        except Exception as e:
            _ts_response = time.time()
            logger.error("structured_extract failed for phase=%s: %s", phase, e)
            self._last_extract_record = ExtractRecord(
                phase=phase, extraction_prompt=extraction_prompt,
                schema=schema, schema_name=_schema_name, phase_hint=phase_hint,
                response_raw=None, success=False, error=str(e),
                timestamp_request=_ts_request, timestamp_response=_ts_response,
            )
            return None
