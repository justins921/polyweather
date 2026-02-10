"""
Claude-powered market analysis.

Sends market question + weather forecast data to Claude and asks it
to estimate the true probability of each outcome.
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
Your job is to estimate the TRUE probability that a weather-related prediction
market outcome will occur, given real forecast data from official weather services.

You will receive:
1. The market question and possible outcomes
2. Current market prices (implied probabilities)
3. Real weather forecast data from NOAA/NWS or Open-Meteo

Your task:
- Analyze the forecast data carefully
- Consider forecast uncertainty, model accuracy, and historical error rates
- Estimate the fair probability that the YES outcome occurs
- Be well-calibrated: if you say 70%, events like this should happen ~70% of the time
- Do NOT anchor to the market price — derive your estimate independently from the data
- If the forecast data is insufficient to form a strong view, say so and lower your confidence

Respond ONLY with valid JSON (no markdown, no code fences) in this exact format:
{
  "fair_value_yes": 0.65,
  "confidence": 0.7,
  "reasoning": "Brief explanation of your probability estimate",
  "key_data_points": ["data point 1", "data point 2"],
  "uncertainty_factors": ["factor 1", "factor 2"]
}

Where:
- fair_value_yes: your estimate of the true probability of YES (0.0 to 1.0)
- confidence: how confident you are in your estimate (0.0 to 1.0)
  - 0.0-0.3 = very uncertain, insufficient data
  - 0.3-0.6 = moderate confidence, some uncertainty
  - 0.6-0.8 = fairly confident
  - 0.8-1.0 = very confident, strong data support
- reasoning: 1-3 sentences explaining your estimate
- key_data_points: the specific forecast data points driving your estimate
- uncertainty_factors: what could make your estimate wrong
"""


class AnalysisResult:
    """Structured result from Claude analysis."""

    def __init__(
        self,
        fair_value_yes: float,
        confidence: float,
        reasoning: str,
        key_data_points: list[str],
        uncertainty_factors: list[str],
        input_tokens: int = 0,
        output_tokens: int = 0,
    ):
        self.fair_value_yes = fair_value_yes
        self.confidence = confidence
        self.reasoning = reasoning
        self.key_data_points = key_data_points
        self.uncertainty_factors = uncertainty_factors
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "fair_value_yes": self.fair_value_yes,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "key_data_points": self.key_data_points,
            "uncertainty_factors": self.uncertainty_factors,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


def analyze_market(
    market: dict[str, Any],
    weather_data: dict[str, Any],
) -> AnalysisResult | None:
    """
    Send market + weather data to Claude for probability estimation.

    Returns an AnalysisResult or None on failure.
    """
    if not config.CLAUDE_API_KEY:
        logger.error("CLAUDE_API_KEY not set in config.py")
        return None

    client = anthropic.Anthropic(api_key=config.CLAUDE_API_KEY)

    # Build the user prompt
    user_prompt = _build_prompt(market, weather_data)

    try:
        start = time.time()
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        elapsed = time.time() - start
        logger.info(
            "Claude analysis took %.1fs (%d input, %d output tokens)",
            elapsed,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
    except anthropic.APIError as e:
        logger.error("Claude API error: %s", e)
        return None

    # Parse the response
    raw_text = response.content[0].text.strip()

    # Strip markdown code fences if present
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        # Remove first and last lines (the fences)
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw_text = "\n".join(lines)

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse Claude response as JSON: %s\nRaw: %s", e, raw_text[:500])
        return None

    # Validate required fields
    fair_value = parsed.get("fair_value_yes")
    confidence = parsed.get("confidence")
    if fair_value is None or confidence is None:
        logger.error("Claude response missing required fields: %s", parsed)
        return None

    fair_value = float(fair_value)
    confidence = float(confidence)

    # Clamp to valid range
    fair_value = max(0.0, min(1.0, fair_value))
    confidence = max(0.0, min(1.0, confidence))

    return AnalysisResult(
        fair_value_yes=fair_value,
        confidence=confidence,
        reasoning=parsed.get("reasoning", ""),
        key_data_points=parsed.get("key_data_points", []),
        uncertainty_factors=parsed.get("uncertainty_factors", []),
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


def _build_prompt(market: dict[str, Any], weather_data: dict[str, Any]) -> str:
    """Build the user prompt for Claude analysis."""
    parts = []

    parts.append("## Market Information")
    parts.append(f"**Question:** {market.get('question', 'N/A')}")
    parts.append(f"**Event:** {market.get('event_title', 'N/A')}")
    parts.append(f"**Outcomes:** {market.get('outcomes', [])}")
    parts.append(f"**Current market prices:** {market.get('outcome_prices', [])}")

    if market.get("clob_prices"):
        parts.append(f"**CLOB live prices:** {json.dumps(market['clob_prices'], indent=2)}")

    if market.get("end_date"):
        parts.append(f"**Resolves:** {market['end_date']}")
    if market.get("days_to_resolution") is not None:
        parts.append(f"**Days to resolution:** {market['days_to_resolution']:.1f}")

    parts.append(f"**Liquidity:** ${market.get('liquidity', 0):,.0f}")
    parts.append(f"**Volume:** ${market.get('volume', 0):,.0f}")

    parts.append("\n## Weather Forecast Data")
    parts.append(f"**Source:** {weather_data.get('source', 'Unknown')}")
    parts.append(f"**City:** {weather_data.get('city', 'Unknown')}")

    # Include forecast data — trim to keep under token limits
    weather_for_prompt = _trim_weather_data(weather_data)
    parts.append(f"\n```json\n{json.dumps(weather_for_prompt, indent=2, default=str)}\n```")

    parts.append("\nAnalyze the forecast data and estimate the true probability of the YES outcome.")
    parts.append("Remember: derive your estimate from the data, do NOT anchor to the market price.")

    return "\n".join(parts)


def _trim_weather_data(weather_data: dict[str, Any]) -> dict[str, Any]:
    """Trim weather data to keep the Claude prompt within token limits."""
    trimmed = {}

    # Always include metadata
    for key in ("source", "city", "lat", "lon", "country_code"):
        if key in weather_data:
            trimmed[key] = weather_data[key]

    # NWS data
    if "forecast" in weather_data:
        trimmed["forecast"] = weather_data["forecast"][:10]  # 5 days
    if "hourly" in weather_data:
        trimmed["hourly"] = weather_data["hourly"][:24]  # 24 hours
    if "grid_data" in weather_data:
        grid = {}
        for k, v in weather_data["grid_data"].items():
            if v:
                grid[k] = v[:14]  # ~7 days of twice-daily
        trimmed["grid_data"] = grid
    if "current_observation" in weather_data:
        trimmed["current_observation"] = weather_data["current_observation"]

    # Open-Meteo data
    if "daily" in weather_data:
        trimmed["daily"] = weather_data["daily"]
    if "hourly" in weather_data and "hourly" not in trimmed:
        trimmed["hourly"] = weather_data["hourly"][:48]

    return trimmed
