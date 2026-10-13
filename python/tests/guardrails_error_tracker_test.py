from datetime import timedelta

from haikeilabs.agentware.middleware.guardrails.error_tracker import (
    ErrorCategory,
    ErrorTracker,
)


def test_record_error():
    et = ErrorTracker()
    et.record_error(
        "session1", "tool1", {"key": "value"}, Exception("test error"), ErrorCategory.UNKNOWN
    )

    count = et.get_error_count("session1", "tool1")
    assert count == 1


def test_get_error_count():
    et = ErrorTracker()
    et.record_error("session1", "tool1", None, Exception("error1"), ErrorCategory.TIMEOUT)
    et.record_error("session1", "tool1", None, Exception("error2"), ErrorCategory.TIMEOUT)
    et.record_error("session1", "tool2", None, Exception("error3"), ErrorCategory.NOT_FOUND)

    assert et.get_error_count("session1", "tool1") == 2
    assert et.get_error_count("session1", "tool2") == 1


def test_get_recent_errors():
    et = ErrorTracker()
    et.record_error("session1", "tool1", None, Exception("error1"), ErrorCategory.UNKNOWN)

    errors = et.get_recent_errors("session1")
    assert len(errors) == 1


def test_get_errors_by_category():
    et = ErrorTracker()
    et.record_error("session1", "tool1", None, Exception("timeout"), ErrorCategory.TIMEOUT)
    et.record_error("session1", "tool1", None, Exception("not found"), ErrorCategory.NOT_FOUND)

    timeout_errors = et.get_errors_by_category("session1", ErrorCategory.TIMEOUT)
    assert len(timeout_errors) == 1


def test_is_error_rate_exceeded():
    et = ErrorTracker(max_errors_per_tool=3)

    for _ in range(3):
        et.record_error("session1", "tool1", None, Exception("error"), ErrorCategory.UNKNOWN)

    assert et.is_error_rate_exceeded("session1", "tool1") is True


def test_reset_session():
    et = ErrorTracker()
    et.record_error("session1", "tool1", None, Exception("error"), ErrorCategory.UNKNOWN)

    et.reset_session("session1")
    count = et.get_error_count("session1", "tool1")
    assert count == 0


def test_should_block_tool():
    et = ErrorTracker(max_errors_per_tool=2)

    et.record_error("session1", "tool1", None, Exception("error1"), ErrorCategory.UNKNOWN)
    et.record_error("session1", "tool1", None, Exception("error2"), ErrorCategory.UNKNOWN)

    assert et.should_block_tool("session1", "tool1") is True
    assert et.should_block_tool("session1", "tool2") is False


def test_set_thresholds():
    et = ErrorTracker()
    et.set_thresholds(10, 10)

    assert et._max_errors_per_tool == 10
    assert et._window_duration == timedelta(minutes=10)
