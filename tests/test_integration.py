"""Integration tests against a real Nextcloud CalDAV instance.

Skipped by default - see README.md for how to enable these locally.
"""

from __future__ import annotations

import os

import pytest

from nextcloud_task_mcp.caldav_client import CalDavService

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "1",
    reason="Set RUN_INTEGRATION_TESTS=1 (plus real credentials) to run these tests.",
)


@pytest.fixture
def live_service() -> CalDavService:
    return CalDavService(
        url=os.environ["NEXTCLOUD_CALDAV_URL"],
        username=os.environ["NEXTCLOUD_USERNAME"],
        password=os.environ["NEXTCLOUD_APP_PASSWORD"],
    )


@pytest.fixture
def test_list_name() -> str:
    return os.environ["INTEGRATION_TEST_LIST"]


def test_list_task_lists_returns_at_least_the_test_list(live_service, test_list_name):
    lists = live_service.list_task_lists()
    assert any(entry["name"] == test_list_name for entry in lists)


def test_full_task_lifecycle(live_service, test_list_name):
    uid = live_service.create_task(
        test_list_name,
        titel="nextcloud-task-mcp integration test task",
        notizen="Created by the automated integration test suite; safe to delete.",
    )
    try:
        tasks = live_service.list_tasks(test_list_name, only_open=True)
        assert any(task["uid"] == uid for task in tasks)

        live_service.update_task(test_list_name, uid, notizen="updated notes")
        updated = next(t for t in live_service.list_tasks(test_list_name) if t["uid"] == uid)
        assert updated["notizen"] == "updated notes"

        live_service.complete_task(test_list_name, uid)
        all_tasks = live_service.list_tasks(test_list_name, only_open=False)
        completed = next(t for t in all_tasks if t["uid"] == uid)
        assert completed["status"] == "erledigt"
    finally:
        live_service.delete_task(test_list_name, uid)
