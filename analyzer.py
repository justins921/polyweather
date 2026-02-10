"""
Claude-powered market analysis.

Sends market question + weather forecast data to Claude and asks it
to estimate the true probability of each outcome.

Works at the EVENT level: one Claude call per event, returning
fair probabilities for ALL outcome buckets (e.g. 7 temperature ranges).
"""

import json
import logging
import time
from typing import Any

import anthropic

import config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an expert meteorological analyst and probabilistic forecaster.
Your job is to estimate the TRUE probability distribution across a set of
weather-related prediction market outcomes, given real forecast data.

You will receive:
1. An event with multiple outcome buckets (e.g. temperature ranges, precip ranges)
2. Current market prices (implied probabilities) for each bucket
3. Real weather forecast data from NOAA/NWS or Open-Meteo

Your task:
- Analyze the forecast data carefully
- Consider forecast uncertainty, model accuracy, historical error rates, and the shape of the forecast distribution
- Estimate fair probabilities for EACH bucket, not just one
- Your probabilities should sum to approximately 1.0 across all buckets
- Be well-calibrated: use the forecast distribution, don't just pick the modal outcome
- Do NOT anchor to the market prices — derive your estimates independently from the data
- If the forecast data is insufficient, say so and lower your confidence

Respond ONLY with valid JSON (no markdown, no code fences) in this exact format:
{
  "buckets": [
    {"question": "bucket question text", "fair_value_yes": 0.05},
    {"question": "bucket question text", "fair_value_yes": 0.25},
    ...
  ],
  "confidence": 0.7,
  "reasoning": "Brief explanation of your probability distribution",
  "key_data_points": ["data point 1", "data point 2"],
  "uncertainty_factors": ["factor 1", "factor 2"]
}

Where:
- buckets: one entry per market/bucket with your fair probability estimate
- confidence: how confident you are in the overall distribution (0.0 to 1.0)
- reasoning: 1-3 sentences explaining your distribution
- key_data_points: the specific forecast data points driving your estimate
- uncertainty_factors: what could shift the distribution
"""


class EventAnalysis:
    """Structured result from Claude analysis of an entire event."""

    def __init__(
        self,
        buckets: list[dict[str, Any]],
        confidence: float,
        reasoning: str,
        key_data_points: list[str],
        uncertainty_factors: list[str],
        input_tokens: int = 0,
        output_tokens: int = 0,
    ):
        self.buckets = buckets  # [{"question": ..., "fair_value_yes": ...}, ...]
        self.confidence = confidence
        self.reasoning = reasoning
        self.key_data_points = key_data_points
        self.uncertainty_factors = uncertainty_factors
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "buckets": self.buckets,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "key_data_points": self.key_data_points,
            "uncertainty_factors": self.uncertainty_factors,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


def analyze_event(
    event: dict[str, Any],
    weather_data: dict[str, Any],
    cost_tracker: Any = None,
) -> EventAnalysis | None:
    """
    Analyze an entire event (with all its bucketed markets) in ONE Claude call.

    Returns an EventAnalysis with fair values for each bucket, or None on failure.
    """
    if not config.CLAUDE_API_KEY:
        logger.error("CLAUDE_API_KEY not set in config.py")
        return None

    client = anthropic.Anthropic(api_key=config.CLAUDE_API_KEY)
    user_prompt = _build_event_prompt(event, weather_data)

    try:
        start = time.time()
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        elapsed = time.time() - start

        in_tok = response.usage.input_tokens
        out_tok = response.usage.output_tokens
        call_cost = 0.0
        if cost_tracker:
            call_cost = cost_tracker.record_api_call(in_tok, out_tok)

        logger.info(
            "Claude analysis took %.1fs (%d in + %d out tokens = $%.4f)",
            elapsed, in_tok, out_tok, call_cost,
        )
    except anthropic.APIError as e:
        logger.error("Claude API error: %s", e)
        return None

    # Parse the response
    raw_text = response.content[0].text.strip()

    # Strip markdown code fences if present
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        raw_text = "\n".join(lines)

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse Claude response as JSON: %s\nRaw: %s", e, raw_text[:500])
        return None

    # Validate required fields
    buckets = parsed.get("buckets")
    confidence = parsed.get("confidence")
    if not buckets or confidence is None:
        logger.error("Claude response missing required fields: %s", list(parsed.keys()))
        return None

    confidence = max(0.0, min(1.0, float(confidence)))

    # Clamp bucket values
    for b in buckets:
        b["fair_value_yes"] = max(0.0, min(1.0, float(b.get("fair_value_yes", 0))))

    return EventAnalysis(
        buckets=buckets,
        confidence=confidence,
        reasoning=parsed.get("reasoning", ""),
        key_data_points=parsed.get("key_data_points", []),
        uncertainty_factors=parsed.get("uncertainty_factors", []),
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


def _build_event_prompt(event: dict[str, Any], weather_data: dict[str, Any]) -> str:
    """Build the user prompt for Claude event-level analysis."""
    parts = []

    parts.append(f"## Event: {event.get('event_title', 'N/A')}")

    if event.get("end_date"):
        parts.append(f"**Resolves:** {event['end_date']}")
    if event.get("days_to_resolution") is not None:
        parts.append(f"**Days to resolution:** {event['days_to_resolution']:.1f}")

    parts.append(f"**Total liquidity:** ${event.get('total_liquidity', 0):,.0f}")

    parts.append("\n## Outcome Buckets (with current market prices)")
    parts.append("Each bucket is a separate YES/NO market. The YES prices should roughly sum to 1.0.\n")

    for i, summary in enumerate(event.get("outcome_summary", [])):
        yes_price = summary["outcome_prices"][0] if summary["outcome_prices"] else "?"
        parts.append(f"{i+1}. **{summary['question']}**  →  YES price: {yes_price}")

    parts.append("\n## Weather Forecast Data")
    parts.append(f"**Source:** {weather_data.get('source', 'Unknown')}")
    parts.append(f"**City:** {weather_data.get('city', 'Unknown')}")

    weather_for_prompt = _trim_weather_data(weather_data)
    parts.append(f"\n```json\n{json.dumps(weather_for_prompt, indent=2, default=str)}\n```")

    parts.append("\nEstimate the fair probability for each bucket above.")
    parts.append("Return one entry per bucket in the `buckets` array, in the same order.")
    parts.append("Derive your estimates from the forecast data, do NOT anchor to market prices.")

    return "\n".join(parts)


def _trim_weather_data(weather_data: dict[str, Any]) -> dict[str, Any]:
    """Trim weather data to keep the Claude prompt within token limits."""
    trimmed = {}

    for key in ("source", "city", "lat", "lon", "country_code"):
        if key in weather_data:
            trimmed[key] = weather_data[key]

    # NWS data — aggressively trim to minimize tokens
    if "forecast" in weather_data:
        trimmed["forecast"] = weather_data["forecast"][:6]
    if "hourly" in weather_data:
        trimmed["hourly"] = weather_data["hourly"][:12]
    if "grid_data" in weather_data:
        grid = {}
        for k, v in weather_data["grid_data"].items():
            if v:
                grid[k] = v[:7]
        trimmed["grid_data"] = grid
    if "current_observation" in weather_data:
        trimmed["current_observation"] = weather_data["current_observation"]

    # Open-Meteo data
    if "daily" in weather_data:
        trimmed["daily"] = weather_data["daily"][:5]
    if "hourly" in weather_data and "hourly" not in trimmed:
        trimmed["hourly"] = weather_data["hourly"][:24]

    return trimmed
