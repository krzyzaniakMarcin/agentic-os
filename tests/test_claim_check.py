# tests/test_claim_check.py — the T16 guard: exactly one claim.made per task.assigned.
import pytest

from kernel.claim_check import assert_one_claim_per_task


def _ev(id, type, agent="worker-1", correlation=None):
    return {"id": id, "agent": agent, "type": type, "payload": {}, "correlation": correlation}


def _assigned(id):
    return _ev(id, "task.assigned", agent="supervisor")


def _claim(id, corr, agent="worker-1"):
    return _ev(id, "claim.made", agent=agent, correlation=str(corr))


def _claimed(id, corr, agent="worker-1"):
    return _ev(id, "task.claimed", agent=agent, correlation=str(corr))


def test_accepts_one_claim_per_task():
    events = [
        _ev(1, "run.start", agent="kernel"),
        _assigned(2), _assigned(3),
        _claimed(5, 2, "worker-1"), _claimed(6, 2, "worker-2"),
        _claim(10, 2, "worker-1"),
        _claimed(7, 3, "worker-2"),
        _claim(11, 3, "worker-2"),
    ]
    assert_one_claim_per_task(events)  # must not raise


def test_rejects_duplicate_claims_for_one_task():
    events = [_assigned(2), _claim(10, 2, "worker-1"), _claim(11, 2, "worker-2")]
    with pytest.raises(AssertionError, match="2 claims"):
        assert_one_claim_per_task(events)


def test_rejects_unclaimed_task():
    events = [_assigned(2), _assigned(3), _claimed(9, 2), _claim(10, 2)]
    with pytest.raises(AssertionError, match="0 claims"):
        assert_one_claim_per_task(events)


def test_rejects_claim_with_no_matching_task():
    events = [_assigned(2), _claimed(9, 2), _claim(10, 2), _claim(11, 99)]
    with pytest.raises(AssertionError, match="no task.assigned"):
        assert_one_claim_per_task(events)


def test_normalizes_int_correlation():
    # correlation is a TEXT column, but a fake/replayed event may carry an int.
    events = [_assigned(2), _claimed(9, 2), {**_claim(10, 2), "correlation": 2}]
    assert_one_claim_per_task(events)  # must not raise


def test_no_tasks_is_vacuously_ok():
    assert_one_claim_per_task([_ev(1, "run.start", agent="kernel")])  # must not raise


def test_claim_backed_by_lowest_task_claimed():
    # The winner-claimed happy path: two task.claimed race, the lower one's
    # agent is the one that goes on to emit claim.made.
    events = [
        _assigned(2),
        _claimed(5, 2, "worker-1"), _claimed(6, 2, "worker-2"),
        _claim(10, 2, "worker-1"),
    ]
    assert_one_claim_per_task(events)  # must not raise


def test_rejects_claim_with_no_task_claimed():
    # False-green guard: an agent that skipped the claim protocol entirely but
    # still emitted claim.made must not pass.
    events = [_assigned(2), _claim(10, 2, "worker-1")]
    with pytest.raises(AssertionError, match="no matching task.claimed"):
        assert_one_claim_per_task(events)


def test_rejects_claim_not_backed_by_lowest_task_claimed():
    # worker-2's task.claimed has the lower id, so worker-2 should have won —
    # worker-1 answering anyway is a protocol violation, not just a miscount.
    events = [
        _assigned(2),
        _claimed(5, 2, "worker-2"),
        _claimed(6, 2, "worker-1"),
        _claim(10, 2, "worker-1"),
    ]
    with pytest.raises(AssertionError, match="lowest task.claimed"):
        assert_one_claim_per_task(events)
