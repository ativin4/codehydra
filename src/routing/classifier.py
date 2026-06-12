from typing import Dict, Any

class Classifier:
    def __init__(self):
        pass

    def evaluate(self, prompt: str) -> str:
        """Evaluates the complexity of the prompt and returns an effort tier."""
        prompt_lower = prompt.lower()
        
        # Heuristics for complexity
        # This is a placeholder for a more advanced classifier
        if any(word in prompt_lower for word in ["refactor", "complex", "architect", "rewrite", "debug this large file"]):
            return "high"
        
        if len(prompt.split()) > 50 or any(word in prompt_lower for word in ["implement", "create", "test", "fix"]):
            return "medium"
        
        return "low"

if __name__ == "__main__":
    classifier = Classifier()
    print(f" 'hi' -> {classifier.evaluate('hi')}")
    print(f" 'fix this bug in the login flow' -> {classifier.evaluate('fix this bug in the login flow')}")
    print(f" 'refactor the entire auth system for better security' -> {classifier.evaluate('refactor the entire auth system for better security')}")
