"""List prices, for cost estimates on every LLM call.

On Groq's free tier the real bill is $0; the estimate says what the same run would cost on a paid
plan, which is the number that matters when comparing team mode with a single agent.
Prices are USD per million tokens (input, output), from console.groq.com/docs/models on 2026-10-13.
"""

PRICES_PER_MILLION: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
    "qwen/qwen3.8-27b": (0.80, 4.00),
}
# Gemini, from ai.google.dev/gemini-api/docs/pricing on 2026-10-14 (paid tier; the free tier is $0).
PRICES_PER_MILLION.update({
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.8-flash": (0.75, 3.75),
})
# The same open models served by Cerebras, under Cerebras's names. Cerebras's own prices weren't
# available to check, so these use Groq's list price for the same model, which keeps cost
# comparisons between runs consistent whichever provider served them.
PRICES_PER_MILLION["gpt-oss-120b"] = PRICES_PER_MILLION["openai/gpt-oss-120b"]
PRICES_PER_MILLION["qwen-3.8-27b"] = PRICES_PER_MILLION["qwen/qwen3.8-27b"]


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float | None:
    """Cost of one call in USD, or None for a model without a known price (e.g. the scripted demo)."""
    prices = PRICES_PER_MILLION.get(model)
    if prices is None:
        return None
    return (tokens_in * prices[0] + tokens_out * prices[1]) / 1_000_000
