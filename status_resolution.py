"""Classify resolution using validated status evidence from one report window."""


def resolved_in_window(transitions: list[dict]) -> bool:
    """Require a resolved final state and an entry from outside the resolved set.

    Callers supply validated transitions in chronological order from the selected
    window, after checking continuity, timestamps and the current task state.
    An internal move preserves an earlier entry but cannot supply one itself.
    """
    return bool(transitions and transitions[-1]["resolved"] is True
                and any(change["entered_resolved"] is True for change in transitions))
