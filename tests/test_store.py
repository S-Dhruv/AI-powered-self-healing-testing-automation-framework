import pytest

from framework.store.backend import ElementStore
from framework.store.models import Fingerprint, Strategy
from framework.store.review import ReviewError, ReviewQueue


@pytest.fixture
def store(tmp_path):
    s = ElementStore(tmp_path / "element_store.db")
    yield s
    s.close()


def _fp(name="Login button"):
    return Fingerprint(tag="button", role="button", accessible_name=name, visible_text=name)


def _strategies():
    return [Strategy(kind="test_id", value="login-btn")]


def test_insert_and_get_active_roundtrip(store):
    rec = store.insert(
        application="demo",
        environment="test",
        page="Login",
        description="Login button",
        strategies=_strategies(),
        fingerprint=_fp(),
        status="active",
        source="AI",
        run_id="run-1",
    )
    fetched = store.get_active("demo", "test", "Login", "Login button")
    assert fetched.id == rec.id
    assert fetched.version == 1
    assert fetched.strategies[0].value == "login-btn"


def test_versioning_increments_on_new_insert(store):
    store.insert(
        application="demo", environment="test", page="Login", description="Login button",
        strategies=_strategies(), fingerprint=_fp(), status="active", source="AI", run_id="run-1",
    )
    second = store.insert(
        application="demo", environment="test", page="Login", description="Login button",
        strategies=_strategies(), fingerprint=_fp(), status="pending", source="Healed", run_id="run-2",
    )
    assert second.version == 2
    history = store.history("demo", "test", "Login", "Login button")
    assert [r.version for r in history] == [1, 2]


def test_heal_pending_then_manual_approve_supersedes_old(store):
    old = store.insert(
        application="demo", environment="test", page="Login", description="Login button",
        strategies=_strategies(), fingerprint=_fp(), status="active", source="AI", run_id="run-1",
    )
    new = store.insert(
        application="demo", environment="test", page="Login", description="Login button",
        strategies=_strategies(), fingerprint=_fp("New Login"), status="pending",
        source="Healed", run_id="run-2",
    )
    # Old version stays active until approved — key healing-policy rule.
    assert store.get_active("demo", "test", "Login", "Login button").id == old.id

    queue = ReviewQueue(store)
    approved = queue.approve(new.id)
    assert approved.status == "active"
    assert store.get_by_id(old.id).status == "superseded"
    assert store.get_active("demo", "test", "Login", "Login button").id == new.id


def test_reject_leaves_old_active(store):
    old = store.insert(
        application="demo", environment="test", page="Login", description="Login button",
        strategies=_strategies(), fingerprint=_fp(), status="active", source="AI", run_id="run-1",
    )
    new = store.insert(
        application="demo", environment="test", page="Login", description="Login button",
        strategies=_strategies(), fingerprint=_fp("New Login"), status="pending",
        source="Healed", run_id="run-2",
    )
    queue = ReviewQueue(store)
    rejected = queue.reject(new.id)
    assert rejected.status == "rejected"
    assert store.get_active("demo", "test", "Login", "Login button").id == old.id


def test_cannot_approve_non_pending(store):
    rec = store.insert(
        application="demo", environment="test", page="Login", description="Login button",
        strategies=_strategies(), fingerprint=_fp(), status="active", source="AI", run_id="run-1",
    )
    queue = ReviewQueue(store)
    with pytest.raises(ReviewError):
        queue.approve(rec.id)


def test_auto_approve_policy_only_fires_when_both_flags_true(store):
    rec = store.insert(
        application="demo", environment="test", page="Login", description="Login button",
        strategies=_strategies(), fingerprint=_fp(), status="pending", source="Healed", run_id="run-1",
    )
    queue = ReviewQueue(store)
    still_pending = queue.auto_approve_if_configured(rec, auto_approve_enabled=True, min_confidence_met=False)
    assert still_pending.status == "pending"
    approved = queue.auto_approve_if_configured(still_pending, auto_approve_enabled=True, min_confidence_met=True)
    assert approved.status == "active"


def test_invalidate_by_page(store):
    store.insert(
        application="demo", environment="test", page="Login", description="Login button",
        strategies=_strategies(), fingerprint=_fp(), status="active", source="AI", run_id="run-1",
    )
    count = store.invalidate(application="demo", page="Login")
    assert count == 1
    assert store.get_active("demo", "test", "Login", "Login button") is None
