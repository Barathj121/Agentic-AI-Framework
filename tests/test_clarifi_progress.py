"""Progress runs on the UI caller thread and does not change financial results."""
import threading

import pytest

from workbench.agents.clarifi.agent import ClariFiAgent
from workbench.agents.clarifi.ingest import focused_input
from workbench.agents.clarifi.models import RulesReasoner
from workbench.agents.clarifi.progress import draft_progress, progress_listener, track_progress


def test_progress_is_request_scoped_and_preserves_results():
    events, threads = [], []
    caller = threading.get_ident()
    class Reasoner(RulesReasoner):
        def summarize(self, context):
            draft_progress(1, 1)
            return super().summarize(context)
    def update(event):
        threads.append(threading.get_ident())
        events.append(event)
    source = focused_input('combined')
    expected = ClariFiAgent(mode='rules').run(source, approved=True)
    with track_progress(update):
        report = ClariFiAgent(reasoner=Reasoner(), mode='rules').run(source, approved=True)
    assert progress_listener() is None
    assert report['findings'] == expected['findings']
    assert report['narrative'] == expected['narrative']
    assert [e['stage'] for e in events] == ['plan', 'tools', 'verify', 'draft', 'draft', 'complete']
    assert events[-2]['message'].endswith('batch 1 of 1')
    assert all(thread == caller for thread in threads)


def test_progress_context_resets_after_failure():
    with pytest.raises(ValueError):
        with track_progress(lambda event: None):
            raise ValueError('synthetic failure')
    assert progress_listener() is None
    draft_progress(1, 1)  # Standalone reasoner use does not require a graph stream.
