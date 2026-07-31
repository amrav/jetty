"""The chat module: emulated surface, translation, and the mock driver.

Assertions here are against spec/chat-v1.md, not against implementation
details: URL layout, the emulated error shape, the threading contract, the
mandatory list queries (§3.2), and read fidelity (§3.6).
"""

from __future__ import annotations

import os

from absl.testing import absltest
from fastapi.testclient import TestClient

from jetty.config import Config
from jetty.server import create_app

#: Two spaces and a DM, one seeded message — the fixture every test reads.
SEED = {
    "spaces": [
        {
            "id": "sandbox",
            "display_name": "Sandbox",
            "space_type": "SPACE",
            "members": ["users/operator"],
        },
        {
            "id": "dm1",
            "display_name": "",
            "space_type": "DIRECT_MESSAGE",
            "members": ["users/operator", "users/jetty"],
        },
    ],
    "messages": [{"space": "sandbox", "sender": "users/operator", "text": "hello seed"}],
}


class ChatTestCase(absltest.TestCase):

    def setUp(self):
        super().setUp()
        self.socket_path = os.path.join(self.create_tempdir().full_path, "jetty.sock")

    def build(self, **chat_settings) -> TestClient:
        settings = {"enabled": True, "seed": SEED, **chat_settings}
        cfg = Config.model_validate(
            {"listener": {"uds": self.socket_path}, "modules": {"chat": settings}}
        )
        self.app = create_app(cfg)
        return TestClient(self.app)

    # -- helpers -----------------------------------------------------------
    def post_message(self, c, space="sandbox", text="hi", **kwargs):
        r = c.post(f"/chat/v1/spaces/{space}/messages", json={"text": text, **kwargs})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def assert_chat_error(self, r, http, status):
        """The emulated shape (chat-v1 §1), never the SPEC.md §3.1 envelope."""
        self.assertEqual(r.status_code, http, r.text)
        body = r.json()["error"]
        self.assertEqual(body["code"], http)
        self.assertEqual(body["status"], status)
        self.assertNotIn("retryable", body)


class ModuleLifecycleTest(ChatTestCase):

    def test_disabled_by_default_is_module_disabled(self):
        cfg = Config.model_validate({"listener": {"uds": self.socket_path}})
        with TestClient(create_app(cfg)) as c:
            r = c.get("/chat/v1/spaces")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "module_disabled")

    def test_meta_advertises_chat_without_listener(self):
        with self.build() as c:
            modules = c.get("/v1/meta").json()["modules"]
        self.assertEqual([m["name"] for m in modules], ["chat"])
        self.assertEqual(modules[0]["mount"], "/chat")
        self.assertNotIn("listener", modules[0])

    def test_unimplemented_driver_fails_boot(self):
        with self.assertRaisesRegex(ValueError, "passthrough"):
            self.build(driver="passthrough")

    def test_unknown_seed_space_fails_boot(self):
        bad = {"spaces": [], "messages": [{"space": "ghost", "text": "x"}]}
        with self.assertRaisesRegex(ValueError, "ghost"):
            self.build(seed=bad)

    def test_unknown_config_key_fails_boot(self):
        with self.assertRaises(Exception):
            self.build(uplaod_max_bytes=5)


class SpacesTest(ChatTestCase):

    def test_list_spaces_carries_read_fidelity_fields(self):
        with self.build() as c:
            body = c.get("/chat/v1/spaces").json()
        self.assertEqual(
            body["spaces"],
            [
                {"name": "spaces/dm1", "displayName": "", "spaceType": "DIRECT_MESSAGE"},
                {"name": "spaces/sandbox", "displayName": "Sandbox", "spaceType": "SPACE"},
            ],
        )
        self.assertNotIn("nextPageToken", body)

    def test_list_spaces_paginates(self):
        with self.build() as c:
            first = c.get("/chat/v1/spaces", params={"pageSize": 1}).json()
            self.assertLen(first["spaces"], 1)
            second = c.get(
                "/chat/v1/spaces",
                params={"pageSize": 1, "pageToken": first["nextPageToken"]},
            ).json()
        self.assertLen(second["spaces"], 1)
        self.assertNotIn("nextPageToken", second)
        self.assertNotEqual(first["spaces"], second["spaces"])

    def test_get_space(self):
        with self.build() as c:
            body = c.get("/chat/v1/spaces/sandbox").json()
            self.assertEqual(body["name"], "spaces/sandbox")
            self.assert_chat_error(c.get("/chat/v1/spaces/ghost"), 404, "NOT_FOUND")

    def test_create_space(self):
        with self.build() as c:
            created = c.post(
                "/chat/v1/spaces", json={"displayName": "New", "spaceType": "SPACE"}
            ).json()
            fetched = c.get(f"/chat/v1/{created['name']}").json()
        self.assertEqual(fetched["displayName"], "New")

    def test_find_direct_message(self):
        with self.build() as c:
            hit = c.get(
                "/chat/v1/spaces:findDirectMessage", params={"name": "users/operator"}
            )
            self.assertEqual(hit.json()["name"], "spaces/dm1")
            miss = c.get(
                "/chat/v1/spaces:findDirectMessage", params={"name": "users/nobody"}
            )
            self.assert_chat_error(miss, 404, "NOT_FOUND")
            bare = c.get("/chat/v1/spaces:findDirectMessage")
            self.assert_chat_error(bare, 400, "INVALID_ARGUMENT")

    def test_list_members(self):
        with self.build() as c:
            body = c.get("/chat/v1/spaces/dm1/members").json()
        users = [m["member"]["name"] for m in body["memberships"]]
        self.assertEqual(users, ["users/operator", "users/jetty"])
        self.assertTrue(
            all(m["name"].startswith("spaces/dm1/members/") for m in body["memberships"])
        )


class CreateMessageTest(ChatTestCase):

    def test_create_starts_a_new_thread_and_attributes_the_identity(self):
        with self.build(identity="users/starbot") as c:
            body = self.post_message(c, text="first")
        self.assertRegex(body["name"], r"^spaces/sandbox/messages/")
        self.assertRegex(body["thread"]["name"], r"^spaces/sandbox/threads/")
        self.assertEqual(body["sender"], {"name": "users/starbot", "type": "HUMAN"})
        self.assertEqual(body["text"], "first")
        self.assertRegex(body["createTime"], r"Z$")
        self.assertNotIn("lastUpdateTime", body)

    def test_reply_fallback_joins_the_existing_thread(self):
        with self.build() as c:
            root = self.post_message(c)
            reply = c.post(
                "/chat/v1/spaces/sandbox/messages",
                params={"messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"},
                json={"text": "reply", "thread": {"name": root["thread"]["name"]}},
            ).json()
        self.assertEqual(reply["thread"]["name"], root["thread"]["name"])

    def test_reply_fallback_to_new_thread_when_thread_is_gone(self):
        with self.build() as c:
            body = c.post(
                "/chat/v1/spaces/sandbox/messages",
                params={"messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"},
                json={"text": "x", "thread": {"name": "spaces/sandbox/threads/ghost"}},
            ).json()
        self.assertNotEqual(body["thread"]["name"], "spaces/sandbox/threads/ghost")

    def test_reply_or_fail_fails_on_a_missing_thread(self):
        with self.build() as c:
            r = c.post(
                "/chat/v1/spaces/sandbox/messages",
                params={"messageReplyOption": "REPLY_MESSAGE_OR_FAIL"},
                json={"text": "x", "thread": {"name": "spaces/sandbox/threads/ghost"}},
            )
        self.assert_chat_error(r, 404, "NOT_FOUND")

    def test_thread_without_reply_option_starts_a_new_thread(self):
        """The emulated default ignores the requested thread (chat-v1 §3.1)."""
        with self.build() as c:
            root = self.post_message(c)
            second = self.post_message(
                c, text="y", thread={"name": root["thread"]["name"]}
            )
        self.assertNotEqual(second["thread"]["name"], root["thread"]["name"])

    def test_thread_key_is_stable_across_creates(self):
        with self.build() as c:
            args = {
                "params": {
                    "messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
                },
            }
            first = c.post(
                "/chat/v1/spaces/sandbox/messages",
                json={"text": "a", "thread": {"threadKey": "k1"}},
                **args,
            ).json()
            second = c.post(
                "/chat/v1/spaces/sandbox/messages",
                json={"text": "b", "thread": {"threadKey": "k1"}},
                **args,
            ).json()
        self.assertEqual(first["thread"]["name"], second["thread"]["name"])

    def test_create_rejections(self):
        with self.build() as c:
            no_text = c.post("/chat/v1/spaces/sandbox/messages", json={})
            self.assert_chat_error(no_text, 400, "INVALID_ARGUMENT")
            unknown = c.post(
                "/chat/v1/spaces/sandbox/messages", json={"text": "x", "cards": []}
            )
            self.assert_chat_error(unknown, 400, "INVALID_ARGUMENT")
            self.assertIn("cards", unknown.json()["error"]["message"])
            bad_option = c.post(
                "/chat/v1/spaces/sandbox/messages",
                params={"messageReplyOption": "REPLY_HARDER"},
                json={"text": "x"},
            )
            self.assert_chat_error(bad_option, 400, "INVALID_ARGUMENT")
            no_space = c.post("/chat/v1/spaces/ghost/messages", json={"text": "x"})
            self.assert_chat_error(no_space, 404, "NOT_FOUND")

    def test_authorization_header_is_accepted_and_ignored(self):
        """chat-v1 §2: stock clients insist on attaching one."""
        with self.build() as c:
            r = c.post(
                "/chat/v1/spaces/sandbox/messages",
                json={"text": "x"},
                headers={"Authorization": "Bearer not-a-real-token"},
            )
        self.assertEqual(r.status_code, 200)


class QuoteReplyTest(ChatTestCase):

    def quote_meta(self, message: dict) -> dict:
        return {
            "name": message["name"],
            "lastUpdateTime": message.get("lastUpdateTime", message["createTime"]),
        }

    def test_quote_in_same_thread_carries_the_snapshot(self):
        with self.build() as c:
            root = self.post_message(c, text="quotable")
            reply = c.post(
                "/chat/v1/spaces/sandbox/messages",
                params={"messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"},
                json={
                    "text": "quoting",
                    "thread": {"name": root["thread"]["name"]},
                    "quotedMessageMetadata": self.quote_meta(root),
                },
            ).json()
        meta = reply["quotedMessageMetadata"]
        self.assertEqual(meta["name"], root["name"])
        self.assertEqual(meta["quotedMessageSnapshot"]["text"], "quotable")

    def test_quote_with_stale_timestamp_fails(self):
        with self.build() as c:
            root = self.post_message(c, text="v1")
            c.patch(
                f"/chat/v1/{root['name']}",
                params={"updateMask": "text"},
                json={"text": "v2"},
            )
            r = c.post(
                "/chat/v1/spaces/sandbox/messages",
                params={"messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"},
                json={
                    "text": "quoting",
                    "thread": {"name": root["thread"]["name"]},
                    "quotedMessageMetadata": self.quote_meta(root),  # pre-edit time
                },
            )
        self.assert_chat_error(r, 400, "INVALID_ARGUMENT")
        self.assertIn("lastUpdateTime", r.json()["error"]["message"])

    def test_quote_of_a_root_message_is_allowed_across_threads(self):
        with self.build() as c:
            root = self.post_message(c, text="root")
            body = self.post_message(
                c, text="new thread", quotedMessageMetadata=self.quote_meta(root)
            )
        self.assertEqual(body["quotedMessageMetadata"]["name"], root["name"])

    def test_quote_of_a_non_root_message_across_threads_fails(self):
        with self.build() as c:
            root = self.post_message(c)
            reply = c.post(
                "/chat/v1/spaces/sandbox/messages",
                params={"messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"},
                json={"text": "non-root", "thread": {"name": root["thread"]["name"]}},
            ).json()
            r = c.post(
                "/chat/v1/spaces/sandbox/messages",
                json={"text": "x", "quotedMessageMetadata": self.quote_meta(reply)},
            )
        self.assert_chat_error(r, 400, "INVALID_ARGUMENT")

    def test_quote_without_timestamp_fails(self):
        with self.build() as c:
            root = self.post_message(c)
            r = c.post(
                "/chat/v1/spaces/sandbox/messages",
                json={"text": "x", "quotedMessageMetadata": {"name": root["name"]}},
            )
        self.assert_chat_error(r, 400, "INVALID_ARGUMENT")


class ListMessagesTest(ChatTestCase):
    """chat-v1 §3.2: filter and orderBy are mandatory, and this is the proof."""

    def three(self, c):
        return [self.post_message(c, text=t) for t in ("one", "two", "three")]

    def texts(self, body):
        return [m["text"] for m in body["messages"]]

    def test_filter_create_time_window(self):
        with self.build() as c:
            a, b, three = self.three(c)
            after = c.get(
                "/chat/v1/spaces/sandbox/messages",
                params={"filter": f'createTime > "{a["createTime"]}"'},
            ).json()
            self.assertEqual(self.texts(after), ["two", "three"])
            window = c.get(
                "/chat/v1/spaces/sandbox/messages",
                params={
                    "filter": f'createTime > "{a["createTime"]}" '
                    f'AND createTime < "{three["createTime"]}"'
                },
            ).json()
        self.assertEqual(self.texts(window), ["two"])

    def test_filter_thread_name(self):
        with self.build() as c:
            root = self.post_message(c, text="in thread")
            self.post_message(c, text="other thread")
            body = c.get(
                "/chat/v1/spaces/sandbox/messages",
                params={"filter": f'thread.name = {root["thread"]["name"]}'},
            ).json()
        self.assertEqual(self.texts(body), ["in thread"])

    def test_order_by_descending(self):
        with self.build() as c:
            self.three(c)
            body = c.get(
                "/chat/v1/spaces/sandbox/messages",
                params={"orderBy": "createTime DESC"},
            ).json()
        self.assertEqual(self.texts(body), ["three", "two", "one", "hello seed"])

    def test_newest_n_since_t_is_one_query(self):
        """The polling client's whole cycle, as one request."""
        with self.build() as c:
            a, _, _ = self.three(c)
            body = c.get(
                "/chat/v1/spaces/sandbox/messages",
                params={
                    "filter": f'createTime > "{a["createTime"]}"',
                    "orderBy": "createTime DESC",
                    "pageSize": 1,
                },
            ).json()
        self.assertEqual(self.texts(body), ["three"])
        self.assertIn("nextPageToken", body)

    def test_pagination_walks_the_space(self):
        with self.build() as c:
            self.three(c)
            texts, token = [], ""
            for _ in range(10):
                params = {"pageSize": 2}
                if token:
                    params["pageToken"] = token
                body = c.get("/chat/v1/spaces/sandbox/messages", params=params).json()
                texts += self.texts(body)
                token = body.get("nextPageToken", "")
                if not token:
                    break
        self.assertEqual(texts, ["hello seed", "one", "two", "three"])

    def test_rejections(self):
        with self.build() as c:
            for params in (
                {"filter": "text = x"},
                {"filter": "createTime >"},
                {"orderBy": "text DESC"},
                {"orderBy": "createTime SIDEWAYS"},
                {"pageSize": "-1"},
                {"pageSize": "abc"},
                {"pageToken": "not-a-token"},
            ):
                r = c.get("/chat/v1/spaces/sandbox/messages", params=params)
                self.assert_chat_error(r, 400, "INVALID_ARGUMENT")
            r = c.get("/chat/v1/spaces/ghost/messages")
            self.assert_chat_error(r, 404, "NOT_FOUND")


class GetPatchDeleteTest(ChatTestCase):

    def test_get_message(self):
        with self.build() as c:
            created = self.post_message(c)
            body = c.get(f"/chat/v1/{created['name']}").json()
            self.assertEqual(body, created)
            miss = c.get("/chat/v1/spaces/sandbox/messages/ghost")
            self.assert_chat_error(miss, 404, "NOT_FOUND")

    def test_patch_text(self):
        with self.build() as c:
            created = self.post_message(c, text="before")
            body = c.patch(
                f"/chat/v1/{created['name']}",
                params={"updateMask": "text"},
                json={"text": "after"},
            ).json()
            self.assertEqual(body["text"], "after")
            self.assertIn("lastUpdateTime", body)
            fetched = c.get(f"/chat/v1/{created['name']}").json()
        self.assertEqual(fetched["text"], "after")

    def test_patch_rejections(self):
        with self.build() as c:
            created = self.post_message(c)
            no_mask = c.patch(f"/chat/v1/{created['name']}", json={"text": "x"})
            self.assert_chat_error(no_mask, 400, "INVALID_ARGUMENT")
            # chat-v1 §3.4/§3.5: an unsupported mask path is named, not dropped.
            other = c.patch(
                f"/chat/v1/{created['name']}",
                params={"updateMask": "cardsV2"},
                json={"text": "x"},
            )
            self.assert_chat_error(other, 400, "INVALID_ARGUMENT")
            self.assertIn("cardsV2", other.json()["error"]["message"])
            miss = c.patch(
                "/chat/v1/spaces/sandbox/messages/ghost",
                params={"updateMask": "text"},
                json={"text": "x"},
            )
            self.assert_chat_error(miss, 404, "NOT_FOUND")

    def test_delete_message(self):
        with self.build() as c:
            created = self.post_message(c)
            self.assertEqual(c.delete(f"/chat/v1/{created['name']}").json(), {})
            self.assert_chat_error(c.get(f"/chat/v1/{created['name']}"), 404, "NOT_FOUND")
            again = c.delete(f"/chat/v1/{created['name']}")
            self.assert_chat_error(again, 404, "NOT_FOUND")


class SearchTest(ChatTestCase):

    def test_search_spans_spaces(self):
        with self.build() as c:
            body = c.post(
                "/chat/v1/spaces/-/messages:search", json={"filter": "SEED"}
            ).json()
        self.assertEqual([m["text"] for m in body["messages"]], ["hello seed"])

    def test_search_parent_must_be_dash(self):
        with self.build() as c:
            r = c.post(
                "/chat/v1/spaces/sandbox/messages:search", json={"filter": "x"}
            )
        self.assert_chat_error(r, 400, "INVALID_ARGUMENT")

    def test_search_requires_filter(self):
        with self.build() as c:
            r = c.post("/chat/v1/spaces/-/messages:search", json={})
        self.assert_chat_error(r, 400, "INVALID_ARGUMENT")


class ReactionsTest(ChatTestCase):

    def test_reaction_lifecycle(self):
        with self.build() as c:
            message = self.post_message(c)
            created = c.post(
                f"/chat/v1/{message['name']}/reactions",
                json={"emoji": {"unicode": "🚀"}},
            ).json()
            self.assertRegex(created["name"], r"/reactions/")
            self.assertEqual(created["emoji"]["unicode"], "🚀")
            listed = c.get(f"/chat/v1/{message['name']}/reactions").json()
            self.assertEqual(listed["reactions"], [created])
            self.assertEqual(c.delete(f"/chat/v1/{created['name']}").json(), {})
            self.assert_chat_error(
                c.delete(f"/chat/v1/{created['name']}"), 404, "NOT_FOUND"
            )

    def test_reactions_on_a_missing_message_are_not_found(self):
        with self.build() as c:
            r = c.post(
                "/chat/v1/spaces/sandbox/messages/ghost/reactions",
                json={"emoji": {"unicode": "🚀"}},
            )
        self.assert_chat_error(r, 404, "NOT_FOUND")


class UploadTest(ChatTestCase):

    def test_upload_then_attach(self):
        with self.build() as c:
            uploaded = c.post(
                "/chat/upload/v1/spaces/sandbox/attachments:upload",
                params={"filename": "notes.txt"},
                content=b"file body",
                headers={"Content-Type": "text/plain"},
            ).json()
            ref = uploaded["attachmentDataRef"]["resourceName"]
            self.assertRegex(ref, r"^spaces/sandbox/attachments/")
            message = self.post_message(
                c, attachment=[{"attachmentDataRef": {"resourceName": ref}}]
            )
        self.assertEqual(
            message["attachment"],
            [{"contentName": "notes.txt", "contentType": "text/plain"}],
        )

    def test_oversized_upload_is_rejected_not_truncated(self):
        with self.build(upload_max_bytes=4) as c:
            r = c.post(
                "/chat/upload/v1/spaces/sandbox/attachments:upload",
                content=b"way past the cap",
            )
        self.assert_chat_error(r, 400, "INVALID_ARGUMENT")

    def test_attach_unknown_ref_is_rejected(self):
        with self.build() as c:
            r = c.post(
                "/chat/v1/spaces/sandbox/messages",
                json={
                    "text": "x",
                    "attachment": [
                        {"attachmentDataRef": {"resourceName": "spaces/s/attachments/no"}}
                    ],
                },
            )
        self.assert_chat_error(r, 400, "INVALID_ARGUMENT")


class SurfaceEdgesTest(ChatTestCase):

    def test_unimplemented_layout_paths_get_the_emulated_501(self):
        with self.build() as c:
            for method, path in (
                ("GET", "/chat/v1/spaces/sandbox/spaceEvents"),
                ("PUT", "/chat/v1/spaces/sandbox"),
                ("GET", "/chat/v1/media/spaces/sandbox/attachments/x"),
            ):
                r = c.request(method, path)
                self.assert_chat_error(r, 501, "UNIMPLEMENTED")

    def test_unrecognisable_paths_get_the_emulated_404(self):
        with self.build() as c:
            for path in ("/chat/v1/bogus", "/chat/nonsense", "/chat/upload/v1/bogus"):
                self.assert_chat_error(c.get(path), 404, "NOT_FOUND")

    def test_driver_failure_is_the_emulated_unavailable(self):
        """SPEC.md §1.2 in the foreign shape: upstream down is 5xx, never 200."""

        class Down:
            def __getattr__(self, _name):
                async def boom(*args, **kwargs):
                    raise ConnectionError("chat backend is down")

                return boom

        with self.build() as c:
            self.app.state.jetty.modules[0].driver = Down()
            r = c.get("/chat/v1/spaces")
        self.assert_chat_error(r, 503, "UNAVAILABLE")
        self.assertNotIn("backend is down", r.text)  # no upstream detail leaks


if __name__ == "__main__":
    absltest.main()
