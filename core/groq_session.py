import os

_GROQ_EXHAUSTED_SESSIONS = set()


def get_session_id() -> str | None:
    """Zwraca identyfikator sesji. Jeśli nie ma, zwraca None."""
    session_id = os.getenv("RENDER_INSTANCE_ID", "").strip()
    return session_id if session_id else None


def is_groq_exhausted(session_id: str | None = None) -> bool:
    sid = session_id or get_session_id()
    return bool(sid and sid in _GROQ_EXHAUSTED_SESSIONS)


def mark_groq_exhausted(session_id: str | None = None) -> None:
    sid = session_id or get_session_id()
    if sid:
        _GROQ_EXHAUSTED_SESSIONS.add(sid)


def clear_groq_exhausted(session_id: str | None = None) -> None:
    sid = session_id or get_session_id()
    if sid and sid in _GROQ_EXHAUSTED_SESSIONS:
        _GROQ_EXHAUSTED_SESSIONS.discard(sid)
