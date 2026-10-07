import json

from framework.config.allowlist import AllowListGuard
from framework.excel.reader import TestCaseRow
from framework.executor import execute_test_case
from framework.llm.usage_logger import UsageLogger
from framework.reporting.console import ConsoleNarrator
from framework.store.backend import ElementStore
from framework.store.review import ReviewQueue
from tests.fakes import FakeLlmClient, ScriptedResponse


def test_llm_call_is_actually_written_to_usage_log(login_page, tmp_path):
    """Spec R7: every LLM call is logged. This proves the log file on disk
    actually receives an entry end-to-end, not just that an in-memory object
    was constructed."""
    store = ElementStore(tmp_path / "store.db")
    queue = ReviewQueue(store)
    guard = AllowListGuard(["localhost"])
    log_path = tmp_path / "llm_usage.jsonl"
    usage_logger = UsageLogger(log_path)

    row = TestCaseRow(
        row_index=2, test_case_id="TC_001", title="t",
        test_step="1. Click 'Login'", test_data="", expected_result="", execute=True,
    )
    llm = FakeLlmClient([ScriptedResponse(
        selector="[data-testid='login-submit-button']", strategy="test_id", confidence=0.95,
        input_tokens=111, output_tokens=22,
    )])

    execute_test_case(
        row, page=login_page, store=store, review_queue=queue, llm_client=llm, allow_guard=guard,
        application="demo", environment="test", page_name="Login", base_url="https://example.test",
        run_id="run-xyz", user_id="qa",
        min_similarity=0.80, min_confidence=0.85, healing_auto_approve=True,
        action_timeout_ms=5000, navigation_timeout_ms=5000, console=ConsoleNarrator(enabled=False),
        usage_logger=usage_logger,
    )

    assert log_path.is_file()
    lines = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    assert lines[0]["run_id"] == "run-xyz"
    assert lines[0]["test_case_id"] == "TC_001"
    assert lines[0]["input_tokens"] == 111
    assert lines[0]["output_tokens"] == 22
    assert lines[0]["status"] == "success"
    assert lines[0]["reason"] == "first_learning"
    store.close()
