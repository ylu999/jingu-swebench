"""JinguModel — LitellmModel subclass with structured phase extraction.

Extends LitellmModel with a `structured_extract()` method that makes an
independent LLM call with grammar-constrained sampling (response_format
json_schema). This guarantees schema-valid JSON output — no regex needed.

Normal step queries continue to use bash tool calls unmodified.

Usage:
    model = JinguModel(model_name="bedrock/...", model_kwargs={...})

    # Normal step (bash tool call, unchanged):
    message = model.query(messages)

    # Phase extraction (constrained structured output):
    result = model.structured_extract(
        accumulated_text="... agent's ANALYZE phase reasoning ...",
        phase="ANALYZE",
        schema=ANALYZE_EXTRACTION_SCHEMA,
    )
    # result is a dict guaranteed to match schema
"""

import json
import logging
from typing import Any

import litellm

from minisweagent.models.litellm_model import LitellmModel, LitellmModelConfig
from minisweagent.models.utils.retry import retry

logger = logging.getLogger("jingu_model")


class JinguModel(LitellmModel):
    """LitellmModel with structured phase extraction capability."""

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

        messages = [
            {"role": "user", "content": extraction_prompt},
        ]

        # Use response_format for grammar-constrained sampling.
        # litellm translates this to Bedrock's outputConfig or tool-use trick.
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": f"{phase.lower()}_extraction",
                "schema": schema,
            },
        }

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

            # Extract JSON from response
            content = response.choices[0].message.content
            if not content:
                logger.warning("structured_extract: empty response content for phase=%s", phase)
                return None

            parsed = json.loads(content)
            logger.info(
                "structured_extract: phase=%s fields=%s",
                phase,
                list(parsed.keys()),
            )
            return parsed

        except Exception as e:
            logger.error("structured_extract failed for phase=%s: %s", phase, e)
            return None
