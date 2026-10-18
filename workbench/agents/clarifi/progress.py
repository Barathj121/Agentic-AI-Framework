"""Request-scoped UI progress, without sharing data or callbacks between visitors."""
from contextlib import contextmanager
from contextvars import ContextVar

_listener = ContextVar('clarifi_progress_listener', default=None)


@contextmanager
def track_progress(listener):
    token = _listener.set(listener)
    try:
        yield
    finally:
        _listener.reset(token)


def progress_listener():
    return _listener.get()


def draft_progress(batch, total):
    # LangGraph carries custom events out of its worker to the caller's stream.
    # Streamlit updates happen in the caller thread, never in a model worker.
    from langgraph.config import get_stream_writer
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer({'stage': 'draft', 'message': f'Drafting analyst explanations · batch {batch} of {total}'})
