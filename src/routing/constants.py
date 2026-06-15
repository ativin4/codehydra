from enum import StrEnum


class CLI(StrEnum):
    CLAUDE = "claude"
    GEMINI = "gemini"
    CODEX = "codex"
    OLLAMA = "ollama"


class Tier(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Mode(StrEnum):
    YOLO = "yolo"
    PLAN = "plan"


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
