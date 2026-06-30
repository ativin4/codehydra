from typing import Dict, Any

# Keywords that signal a heavy architectural task — use the highest-effort model.
_HIGH_KEYWORDS = frozenset({
    "refactor", "architect", "rewrite", "redesign", "migrate",
    "entire codebase", "whole codebase", "from scratch",
})

# Keywords that keep a prompt on "low" (quick, no-op interactions).
# Everything else defaults to medium so coding tasks get sonnet, not haiku.
_LOW_KEYWORDS = frozenset({
    "hi", "hello", "hey", "thanks", "thank you", "ok", "okay", "cool",
    "bye", "exit", "quit", "yes", "no", "sure",
})


class Classifier:
    def evaluate(self, prompt: str) -> str:
        """Maps a prompt to an effort tier (low / medium / high)."""
        words = prompt.strip().split()
        lower = prompt.lower().strip()

        # High: explicit heavy-lifting keywords.
        if any(kw in lower for kw in _HIGH_KEYWORDS):
            return "high"

        # Low: very short single-word / greeting interactions.
        if len(words) <= 3 and lower in _LOW_KEYWORDS:
            return "low"

        # Medium: everything else — the sensible default for coding tasks.
        # Haiku is fast but too weak for planning, explaining, or writing code.
        return "medium"


if __name__ == "__main__":
    c = Classifier()
    cases = [
        "hi",
        "thanks",
        "plan a new auth flow for the app",
        "fix this bug",
        "what is this code doing",
        "explain the gateway module",
        "refactor the entire auth system",
        "architect a new microservice",
    ]
    for p in cases:
        print(f"{c.evaluate(p):8}  {p}")
