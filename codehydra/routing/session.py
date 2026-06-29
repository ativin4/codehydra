import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from codehydra.routing.constants import Role

_SESSION_ID_RE = re.compile(r'^[\w\-]{1,80}$')
_VALID_ROLES = set(Role)


class SessionManager:
    """Persists chat sessions (history + routing state) to .codehydra/sessions/."""

    def __init__(self, root_dir: str = "."):
        self.sessions_dir = Path(root_dir).resolve() / ".codehydra" / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def new_session_id(self) -> str:
        return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    def _path(self, session_id: str) -> Path:
        if not _SESSION_ID_RE.match(session_id):
            raise ValueError(f"Invalid session ID: {session_id!r}")
        return self.sessions_dir / f"{session_id}.json"

    def save(self, session_id: str, state: Dict[str, Any]) -> None:
        payload = {**state, "updated_at": time.time()}
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        with open(self._path(session_id), "w") as f:
            json.dump(payload, f, indent=2)

    def load(self, session_id: str) -> Optional[Dict[str, Any]]:
        try:
            path = self._path(session_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            with open(path, "r") as f:
                data = json.load(f)
            history = data.get("history", [])
            if not isinstance(history, list):
                return None
            for msg in history:
                if not isinstance(msg, dict):
                    return None
                if msg.get("role") not in _VALID_ROLES:
                    return None
                if not isinstance(msg.get("content", ""), str):
                    return None
            return data
        except Exception:
            return None

    def list_sessions(self) -> List[Dict[str, Any]]:
        """Returns session summaries, newest first."""
        sessions = []
        for path in self.sessions_dir.glob("*.json"):
            data = self.load(path.stem)
            if not data:
                continue
            preview = ""
            for msg in data.get("history", []):
                if msg.get("role") == "user":
                    preview = msg["content"][:60]
                    break
            sessions.append({
                "id": path.stem,
                "updated_at": data.get("updated_at", 0),
                "preview": preview,
            })
        return sorted(sessions, key=lambda s: s["updated_at"], reverse=True)

    def latest_session_id(self, exclude: Optional[str] = None) -> Optional[str]:
        for s in self.list_sessions():
            if s["id"] != exclude:
                return s["id"]
        return None
