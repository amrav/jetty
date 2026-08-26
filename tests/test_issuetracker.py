"""The issuetracker module: emulated surface, translation, and the mock driver.

Assertions here are against spec/issuetracker-v1.md, not implementation
details: URL layout, the emulated error shape, the masked modify (§4.3), the
query/orderBy contract (§4.2), int64-as-string wire encoding, translation
fidelity (§4.6), and the no-auth posture (§3).
"""

from __future__ import annotations

import os

from absl.testing import absltest
from fastapi.testclient import TestClient

from jetty.config import Config
from jetty.server import create_app

#: Two components, four issues — the fixture every test reads. Mock ids are
#: deterministic: seeded issues are 1001..1004 in seed order.
SEED = {
    "components": [{"component_id": 100, "name": "widget"}, {"component_id": 200}],
    "issues": [
        {
            "component_id": 100,
            "title": "Crash on save",
            "status": "NEW",
            "priority": "P0",
            "severity": "S1",
            "assignee": "ada@example.com",
            "description": "stack trace attached",
        },
        {
            "component_id": 100,
            "title": "Slow startup",
            "status": "ASSIGNED",
            "priority": "P2",
            "assignee": "grace@example.com",
        },
        {
            "component_id": 100,
            "title": "Crash on load",
            "status": "FIXED",
            "priority": "P1",
        },
        {"component_id": 200, "title": "Other component", "status": "NEW"},
    ],
}
CRASH_SAVE, SLOW_START, CRASH_LOAD, OTHER = "1001", "1002", "1003", "1004"


class TrackerTestCase(absltest.TestCase):

    def setUp(self):
        super().setUp()
        self.socket_path = os.path.join(self.create_tempdir().full_path, "jetty.sock")

    def build(self, **settings) -> TestClient:
        merged = {"enabled": True, "seed": SEED, **settings}
        cfg = Config.model_validate(
            {"listener": {"uds": self.socket_path}, "modules": {"issuetracker": merged}}
        )
        self.app = create_app(cfg)
        return TestClient(self.app)

    # -- helpers -----------------------------------------------------------
    def list_issues(self, c, query, **params):
        r = c.get("/issuetracker/v1/issues", params={"query": query, **params})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def modify(self, c, issue_id, body):
        return c.post(f"/issuetracker/v1/issues/{issue_id}:modify", json=body)

    def assert_tracker_error(self, r, http, status):
        """The emulated shape (issuetracker-v1 §2), never SPEC.md §3.1."""
        self.assertEqual(r.status_code, http, r.text)
        body = r.json()["error"]
        self.assertEqual(body["code"], http)
        self.assertEqual(body["status"], status)
        self.assertNotIn("retryable", body)


class ModuleLifecycleTest(TrackerTestCase):

    def test_disabled_by_default_is_module_disabled(self):
        cfg = Config.model_validate({"listener": {"uds": self.socket_path}})
        with TestClient(create_app(cfg)) as c:
            r = c.get("/issuetracker/v1/issues", params={"query": "componentid:100"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "module_disabled")

    def test_meta_advertises_issuetracker_without_listener(self):
        with self.build() as c:
            modules = c.get("/v1/meta").json()["modules"]
        self.assertEqual([m["name"] for m in modules], ["issuetracker"])
        self.assertEqual(modules[0]["mount"], "/issuetracker")
        self.assertNotIn("listener", modules[0])

    def test_unimplemented_driver_fails_boot(self):
        with self.assertRaisesRegex(ValueError, "passthrough"):
            self.build(driver="passthrough")

    def test_seed_issue_in_unknown_component_fails_boot(self):
        bad = {"components": [], "issues": [{"component_id": 7, "title": "x"}]}
        with self.assertRaisesRegex(ValueError, "unknown component 7"):
            self.build(seed=bad)

    def test_seed_issue_with_invalid_status_fails_boot(self):
        bad = {
            "components": [{"component_id": 1}],
            "issues": [{"component_id": 1, "title": "x", "status": "OPEN"}],
        }
        with self.assertRaisesRegex(ValueError, "invalid status"):
            self.build(seed=bad)

    def test_unknown_config_key_fails_boot(self):
        with self.assertRaises(Exception):
            self.build(identiy="x@example.com")

    def test_credentials_are_accepted_and_ignored(self):
        # issuetracker-v1 §3: stock clients attach these; none is required,
        # none is rejected.
        with self.build() as c:
            r = c.get(
                "/issuetracker/v1/issues",
                params={"query": "componentid:100", "key": "AIza-fake"},
                headers={"authorization": "Bearer fake", "x-goog-api-key": "fake"},
            )
        self.assertEqual(r.status_code, 200, r.text)


class ComponentsTest(TrackerTestCase):

    def test_get_component(self):
        with self.build() as c:
            r = c.get("/issuetracker/v1/components/100")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"componentId": "100", "isArchived": False})

    def test_unknown_component_is_emulated_404(self):
        with self.build() as c:
            self.assert_tracker_error(
                c.get("/issuetracker/v1/components/404404"), 404, "NOT_FOUND"
            )

    def test_non_numeric_component_id_is_invalid(self):
        with self.build() as c:
            self.assert_tracker_error(
                c.get("/issuetracker/v1/components/widget"), 400, "INVALID_ARGUMENT"
            )


class ListIssuesTest(TrackerTestCase):

    def test_query_is_required(self):
        with self.build() as c:
            self.assert_tracker_error(
                c.get("/issuetracker/v1/issues"), 400, "INVALID_ARGUMENT"
            )

    def test_componentid_query_scopes_to_the_component(self):
        with self.build() as c:
            body = self.list_issues(c, "componentid:100")
        self.assertEqual(body["totalSize"], 3)
        self.assertEqual(
            [i["issueId"] for i in body["issues"]], [CRASH_SAVE, SLOW_START, CRASH_LOAD]
        )

    def test_int64_fields_are_strings_and_enums_are_names(self):
        with self.build() as c:
            issue = self.list_issues(c, "componentid:100")["issues"][0]
        self.assertEqual(issue["issueId"], CRASH_SAVE)  # string, not number
        state = issue["issueState"]
        self.assertEqual(state["componentId"], "100")
        self.assertEqual(state["status"], "NEW")
        self.assertEqual(state["priority"], "P0")
        self.assertEqual(state["severity"], "S1")
        self.assertEqual(state["title"], "Crash on save")
        self.assertEqual(state["assignee"], {"emailAddress": "ada@example.com"})
        self.assertIn("createdTime", issue)
        self.assertIn("modifiedTime", issue)

    def test_basic_view_omits_description_and_full_carries_it(self):
        with self.build() as c:
            basic = self.list_issues(c, "componentid:100")["issues"][0]
            full = self.list_issues(c, "componentid:100", view="FULL")["issues"][0]
        self.assertNotIn("description", basic)
        self.assertEqual(full["description"]["comment"], "stack trace attached")
        self.assertEqual(full["description"]["commentNumber"], 1)

    def test_query_grammar_floor(self):
        with self.build() as c:
            open_ids = [
                i["issueId"] for i in self.list_issues(c, "componentid:100 status:open")["issues"]
            ]
            p0 = [i["issueId"] for i in self.list_issues(c, "p:p0")["issues"]]
            ada = [i["issueId"] for i in self.list_issues(c, "assignee:ada")["issues"]]
            crash = [i["issueId"] for i in self.list_issues(c, "componentid:100 crash")["issues"]]
        self.assertEqual(open_ids, [CRASH_SAVE, SLOW_START])
        self.assertEqual(p0, [CRASH_SAVE])
        self.assertEqual(ada, [CRASH_SAVE])
        self.assertEqual(crash, [CRASH_SAVE, CRASH_LOAD])

    def test_unsupported_query_term_is_rejected_not_dropped(self):
        # issuetracker-v1 §4.2/§4.6: a term the driver cannot evaluate must
        # not silently vanish from the query.
        with self.build() as c:
            self.assert_tracker_error(
                c.get("/issuetracker/v1/issues", params={"query": "hotlistid:5"}),
                400,
                "INVALID_ARGUMENT",
            )

    def test_order_by_with_direction_and_secondary_sort(self):
        with self.build() as c:
            by_priority = self.list_issues(c, "componentid:100", orderBy="priority asc")
            newest = self.list_issues(c, "componentid:100", orderBy="created desc")
        self.assertEqual(
            [i["issueState"]["priority"] for i in by_priority["issues"]],
            ["P0", "P1", "P2"],
        )
        self.assertEqual(
            [i["issueId"] for i in newest["issues"]], [CRASH_LOAD, SLOW_START, CRASH_SAVE]
        )

    def test_unsupported_order_by_is_rejected(self):
        with self.build() as c:
            self.assert_tracker_error(
                c.get(
                    "/issuetracker/v1/issues",
                    params={"query": "componentid:100", "orderBy": "votes desc"},
                ),
                400,
                "INVALID_ARGUMENT",
            )

    def test_forward_headers_reach_the_driver_and_default_to_none(self):
        # issuetracker-v1 §3: only the configured header names are bound for
        # the driver, verbatim; with no configuration nothing ever is, even
        # when the client sends credentials.
        from jetty.modules.issuetracker.driver import forwarded_headers

        seen: list[dict] = []

        def spy(module):
            inner = module.driver.create_comment

            async def create_comment(issue_id, text):
                seen.append(dict(forwarded_headers.get()))
                return await inner(issue_id, text)

            module.driver.create_comment = create_comment

        with self.build(forward_headers=["authorization"]) as c:
            spy(self.app.state.jetty.modules[0])
            r = c.post(
                f"/issuetracker/v1/issues/{CRASH_SAVE}/comments",
                json={"comment": "as the caller"},
                headers={"Authorization": "Bearer caller-token", "x-api-key": "noise"},
            )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen, [{"authorization": "Bearer caller-token"}])

        seen.clear()
        with self.build() as c:  # default: nothing forwarded
            spy(self.app.state.jetty.modules[0])
            r = c.post(
                f"/issuetracker/v1/issues/{CRASH_SAVE}/comments",
                json={"comment": "service identity"},
                headers={"Authorization": "Bearer caller-token"},
            )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen, [{}])

    def test_order_by_sorts_on_modified_time_not_modified(self):
        # The tracker's sort field is `modified_time` (the response *key* is
        # `modifiedTime`); `modified` is not a field there. Rejecting it is
        # the point: the nicer spelling once let a client bug survive the
        # mock and fail only against the real tracker.
        with self.build() as c:
            newest = self.list_issues(c, "componentid:100", orderBy="modified_time desc")
            stamps = [i["modifiedTime"] for i in newest["issues"]]
            self.assertEqual(stamps, sorted(stamps, reverse=True))
            self.assert_tracker_error(
                c.get(
                    "/issuetracker/v1/issues",
                    params={"query": "componentid:100", "orderBy": "modified desc"},
                ),
                400,
                "INVALID_ARGUMENT",
            )

    def test_pagination_pages_through_with_stable_total(self):
        with self.build() as c:
            first = self.list_issues(c, "componentid:100", pageSize=2)
            second = self.list_issues(
                c, "componentid:100", pageSize=2, pageToken=first["nextPageToken"]
            )
        self.assertEqual(len(first["issues"]), 2)
        self.assertEqual(first["totalSize"], 3)
        self.assertEqual([i["issueId"] for i in second["issues"]], [CRASH_LOAD])
        self.assertNotIn("nextPageToken", second)

    def test_batch_get_omits_unknown_ids(self):
        with self.build() as c:
            r = c.get(
                "/issuetracker/v1/issues:batchGet",
                params=[("issueIds", CRASH_SAVE), ("issueIds", "424242")],
            )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([i["issueId"] for i in r.json()["issues"]], [CRASH_SAVE])

    def test_get_unknown_issue_is_emulated_404(self):
        with self.build() as c:
            self.assert_tracker_error(
                c.get("/issuetracker/v1/issues/424242"), 404, "NOT_FOUND"
            )


class CreateIssueTest(TrackerTestCase):

    MINIMAL = {
        "issue": {
            "issueState": {
                "componentId": "100",
                "title": "New bug",
                "status": "NEW",
                "type": "BUG",
                "priority": "P1",
                "severity": "S2",
            },
            "description": {"comment": "first comment"},
        }
    }

    def test_create_returns_full_issue(self):
        with self.build() as c:
            r = c.post("/issuetracker/v1/issues", json=self.MINIMAL)
            self.assertEqual(r.status_code, 200, r.text)
            issue = r.json()
            listed = self.list_issues(c, "componentid:100")["totalSize"]
        self.assertEqual(issue["issueState"]["title"], "New bug")
        self.assertEqual(issue["description"]["comment"], "first comment")
        self.assertEqual(listed, 4)

    def test_missing_required_field_is_rejected(self):
        body = {"issue": {"issueState": {"componentId": "100", "title": "x"}}}
        with self.build() as c:
            r = c.post("/issuetracker/v1/issues", json=body)
        self.assert_tracker_error(r, 400, "INVALID_ARGUMENT")
        self.assertIn("status", r.json()["error"]["message"])

    def test_unknown_component_is_rejected(self):
        body = {"issue": {"issueState": {**self.MINIMAL["issue"]["issueState"], "componentId": "9"}}}
        with self.build() as c:
            self.assert_tracker_error(
                c.post("/issuetracker/v1/issues", json=body), 400, "INVALID_ARGUMENT"
            )

    def test_template_options_are_rejected_not_ignored(self):
        body = {**self.MINIMAL, "templateOptions": {"applyTemplate": True}}
        with self.build() as c:
            self.assert_tracker_error(
                c.post("/issuetracker/v1/issues", json=body), 400, "INVALID_ARGUMENT"
            )


class ModifyIssueTest(TrackerTestCase):

    def test_masked_scalar_set_applies_and_bumps_modified_time(self):
        with self.build() as c:
            before = c.get(f"/issuetracker/v1/issues/{CRASH_SAVE}").json()
            r = self.modify(
                c,
                CRASH_SAVE,
                {"add": {"status": "ACCEPTED", "priority": "P1"}, "addMask": "status,priority"},
            )
            self.assertEqual(r.status_code, 200, r.text)
            after = r.json()
        self.assertEqual(after["issueState"]["status"], "ACCEPTED")
        self.assertEqual(after["issueState"]["priority"], "P1")
        self.assertGreater(after["modifiedTime"], before["modifiedTime"])
        # untouched fields stay (issuetracker-v1 §4.3)
        self.assertEqual(after["issueState"]["title"], "Crash on save")

    def test_modify_records_issue_updates_history(self):
        with self.build() as c:
            self.modify(c, CRASH_SAVE, {"add": {"status": "FIXED"}, "addMask": "status"})
            r = c.get(f"/issuetracker/v1/issues/{CRASH_SAVE}/issueUpdates")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["totalSize"], 1)
        update = body["issueUpdates"][0]
        self.assertEqual(update["author"], {"emailAddress": "jetty@example.com"})
        self.assertEqual(
            update["fieldUpdates"],
            [{"field": "status", "singleValueUpdate": {"oldValue": "NEW", "newValue": "FIXED"}}],
        )

    def test_collection_add_and_remove_via_masks(self):
        with self.build() as c:
            r = self.modify(
                c,
                CRASH_SAVE,
                {"add": {"ccs": [{"emailAddress": "cc@example.com"}]}, "addMask": "ccs"},
            )
            self.assertEqual(
                r.json()["issueState"]["ccs"], [{"emailAddress": "cc@example.com"}]
            )
            r = self.modify(
                c,
                CRASH_SAVE,
                {"remove": {"ccs": [{"emailAddress": "cc@example.com"}]}, "removeMask": "ccs"},
            )
            self.assertNotIn("ccs", r.json()["issueState"])

    def test_empty_modify_is_rejected(self):
        with self.build() as c:
            self.assert_tracker_error(
                self.modify(c, CRASH_SAVE, {}), 400, "INVALID_ARGUMENT"
            )

    def test_masked_field_missing_from_add_is_rejected(self):
        with self.build() as c:
            self.assert_tracker_error(
                self.modify(c, CRASH_SAVE, {"add": {}, "addMask": "status"}),
                400,
                "INVALID_ARGUMENT",
            )

    def test_unsupported_mask_field_is_rejected_not_dropped(self):
        # issuetracker-v1 §4.6.
        with self.build() as c:
            r = self.modify(
                c, CRASH_SAVE, {"add": {"votes": 5}, "addMask": "votes"}
            )
        self.assert_tracker_error(r, 400, "INVALID_ARGUMENT")
        self.assertIn("votes", r.json()["error"]["message"])

    def test_modify_with_comment_appends_a_comment(self):
        with self.build() as c:
            self.modify(
                c,
                CRASH_SAVE,
                {
                    "add": {"status": "FIXED"},
                    "addMask": "status",
                    "issueComment": {"comment": "fixed in r42"},
                },
            )
            comments = c.get(f"/issuetracker/v1/issues/{CRASH_SAVE}/comments").json()
        self.assertEqual(comments["totalSize"], 2)
        self.assertEqual(comments["issueComments"][1]["comment"], "fixed in r42")
        self.assertEqual(comments["issueComments"][1]["commentNumber"], 2)

    def test_modify_unknown_issue_is_emulated_404(self):
        with self.build() as c:
            self.assert_tracker_error(
                self.modify(c, "424242", {"add": {"status": "FIXED"}, "addMask": "status"}),
                404,
                "NOT_FOUND",
            )


class CommentsTest(TrackerTestCase):

    def test_description_is_comment_one(self):
        with self.build() as c:
            body = c.get(f"/issuetracker/v1/issues/{CRASH_SAVE}/comments").json()
        self.assertEqual(body["issueComments"][0]["commentNumber"], 1)
        self.assertEqual(body["issueComments"][0]["comment"], "stack trace attached")

    def test_create_then_update_comment(self):
        with self.build() as c:
            r = c.post(
                f"/issuetracker/v1/issues/{CRASH_SAVE}/comments",
                json={"comment": "me too"},
            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["commentNumber"], 2)
            r = c.put(
                f"/issuetracker/v1/issues/{CRASH_SAVE}/comments/2",
                json={"comment": "edited"},
            )
            self.assertEqual(r.json()["comment"], "edited")

    def test_updating_comment_one_updates_the_description(self):
        with self.build() as c:
            c.put(
                f"/issuetracker/v1/issues/{CRASH_SAVE}/comments/1",
                json={"comment": "new description"},
            )
            issue = c.get(
                f"/issuetracker/v1/issues/{CRASH_SAVE}", params={"view": "FULL"}
            ).json()
        self.assertEqual(issue["description"]["comment"], "new description")

    def test_unknown_comment_number_is_emulated_404(self):
        with self.build() as c:
            self.assert_tracker_error(
                c.put(
                    f"/issuetracker/v1/issues/{CRASH_SAVE}/comments/9",
                    json={"comment": "x"},
                ),
                404,
                "NOT_FOUND",
            )


class RelationshipsTest(TrackerTestCase):

    def test_create_and_list_by_type(self):
        with self.build() as c:
            r = c.post(
                f"/issuetracker/v1/issues/{CRASH_SAVE}/relationships",
                params={"relationshipType": "CHILD"},
                json={"targetIssueId": SLOW_START},
            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json(), {"targetIssueId": SLOW_START})
            children = c.get(
                f"/issuetracker/v1/issues/{CRASH_SAVE}/relationships",
                params={"relationshipType": "CHILD"},
            ).json()
            linked = c.get(
                f"/issuetracker/v1/issues/{CRASH_SAVE}/relationships",
                params={"relationshipType": "LINKED"},
            ).json()
        self.assertEqual(children["issueRelationships"], [{"targetIssueId": SLOW_START}])
        self.assertEqual(linked["issueRelationships"], [])

    def test_relationship_type_is_required_and_closed(self):
        with self.build() as c:
            self.assert_tracker_error(
                c.get(f"/issuetracker/v1/issues/{CRASH_SAVE}/relationships"),
                400,
                "INVALID_ARGUMENT",
            )
            self.assert_tracker_error(
                c.post(
                    f"/issuetracker/v1/issues/{CRASH_SAVE}/relationships",
                    params={"relationshipType": "PARENT"},
                    json={"targetIssueId": SLOW_START},
                ),
                400,
                "INVALID_ARGUMENT",
            )

    def test_unknown_target_is_rejected(self):
        with self.build() as c:
            self.assert_tracker_error(
                c.post(
                    f"/issuetracker/v1/issues/{CRASH_SAVE}/relationships",
                    params={"relationshipType": "DEPENDENCY"},
                    json={"targetIssueId": "424242"},
                ),
                400,
                "INVALID_ARGUMENT",
            )


class HotlistsTest(TrackerTestCase):

    def test_entry_lifecycle_reflects_on_the_issue(self):
        with self.build() as c:
            r = c.post(
                "/issuetracker/v1/hotlists/55/entries", json={"issueId": CRASH_SAVE}
            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json(), {"issueId": CRASH_SAVE})
            on = c.get(f"/issuetracker/v1/issues/{CRASH_SAVE}").json()
            r = c.delete(f"/issuetracker/v1/hotlists/55/entries/{CRASH_SAVE}")
            self.assertEqual(r.status_code, 200, r.text)
            off = c.get(f"/issuetracker/v1/issues/{CRASH_SAVE}").json()
        self.assertEqual(on["issueState"]["hotlistIds"], ["55"])
        self.assertNotIn("hotlistIds", off["issueState"])

    def test_deleting_an_absent_entry_is_emulated_404(self):
        with self.build() as c:
            self.assert_tracker_error(
                c.delete(f"/issuetracker/v1/hotlists/55/entries/{CRASH_SAVE}"),
                404,
                "NOT_FOUND",
            )


class AttachmentsTest(TrackerTestCase):

    def test_attachments_list_is_metadata_only_and_empty_for_the_mock(self):
        with self.build() as c:
            r = c.get(f"/issuetracker/v1/issues/{CRASH_SAVE}/attachments")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"attachments": []})


class LayoutTest(TrackerTestCase):

    def test_in_layout_but_not_in_subset_is_unimplemented(self):
        with self.build() as c:
            self.assert_tracker_error(
                c.get(f"/issuetracker/v1/issues/{CRASH_SAVE}/votes"),
                501,
                "UNIMPLEMENTED",
            )

    def test_outside_the_layout_is_not_found(self):
        with self.build() as c:
            self.assert_tracker_error(
                c.get("/issuetracker/v1/wombats"), 404, "NOT_FOUND"
            )


class SyncClientContractTest(TrackerTestCase):
    """The request shapes a poll-based sync client sends, verbatim.

    The expected external consumer of this surface is a tracker-sync client
    that polls a component (`componentid:` query, FULL view, max page size)
    and pushes status/priority back through the masked modify. These pin
    those exact shapes so a surface change that would break such a client
    fails here first.
    """

    def test_component_poll_round_trip(self):
        with self.build() as c:
            r = c.get(
                "/issuetracker/v1/issues",
                params={"query": "componentid:100", "view": "FULL", "pageSize": "500"},
            )
        self.assertEqual(r.status_code, 200, r.text)
        for issue in r.json()["issues"]:
            # every field a polling sync client reads, present and typed as parsed
            int(issue["issueId"])
            self.assertIn(issue["issueState"]["status"], {"NEW", "ASSIGNED", "FIXED"})
            self.assertTrue(issue["issueState"]["priority"].startswith("P"))
            self.assertIsInstance(issue["issueState"]["title"], str)
            self.assertIsInstance(issue["description"]["comment"], str)
            self.assertIsInstance(issue["modifiedTime"], str)

    def test_masked_status_priority_push(self):
        with self.build() as c:
            r = c.post(
                f"/issuetracker/v1/issues/{SLOW_START}:modify",
                json={"add": {"status": "INFEASIBLE", "priority": "P3"}, "addMask": "status,priority"},
            )
            self.assertEqual(r.status_code, 200, r.text)
            polled = c.get(
                "/issuetracker/v1/issues",
                params={"query": "componentid:100", "view": "FULL", "pageSize": "500"},
            ).json()
        slow = next(i for i in polled["issues"] if i["issueId"] == SLOW_START)
        self.assertEqual(slow["issueState"]["status"], "INFEASIBLE")
        self.assertEqual(slow["issueState"]["priority"], "P3")


if __name__ == "__main__":
    absltest.main()
