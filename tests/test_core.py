"""Core behaviour: the module registry, the spec's core endpoints, envelope."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jetty.config import Config
from jetty.modules.registry import UnknownModuleError
from jetty.server import SPEC_VERSION, create_app


def build(**modules: dict) -> TestClient:
    cfg = Config.model_validate(
        {"listener": {"uds": "/tmp/jetty-test.sock"}, "modules": modules}
    )
    return TestClient(create_app(cfg))


# --------------------------------------------------------------- core endpoints


def test_healthz_is_liveness_only():
    with build() as c:
        r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["spec_version"] == SPEC_VERSION








def test_meta_advertises_enabled_modules():
    with build(reference={"enabled": True}) as c:
        body = c.get("/v1/meta").json()
    assert body["spec_version"] == SPEC_VERSION
    assert [m["name"] for m in body["modules"]] == ["reference"]
    assert body["modules"][0]["mount"] == "/reference"
    # No limits block and no readiness flag — both removed from the protocol.
    assert "limits" not in body
    assert "required" not in body["modules"][0]


# ------------------------------------------------------------ enable / disable


def test_nothing_is_enabled_by_default():
    with build() as c:
        assert c.get("/v1/meta").json()["modules"] == []
        # SPEC.md §4.3: a disabled module's route does not exist, and says so
        # with `module_disabled` rather than a bare not_found.
        r = c.post("/reference/v1/echo", json={"message": "hi"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "module_disabled"
    assert r.json()["error"]["retryable"] is False


def test_unknown_route_is_not_found_not_module_disabled():
    with build(reference={"enabled": True}) as c:
        r = c.get("/nope/v1/whatever")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_every_404_uses_the_spec_envelope():
    """Starlette's default {"detail": ...} must never reach a client."""
    with build(reference={"enabled": True}) as c:
        for path in ("/nope", "/reference/v1/nope", "/auth/v1/identify"):
            body = c.get(path).json()
            assert "error" in body, f"{path} returned {body}"
            assert set(body["error"]) == {"code", "message", "retryable"}


def test_module_absent_from_config_is_disabled():
    with build(reference={"enabled": False}) as c:
        assert c.get("/v1/meta").json()["modules"] == []


def test_enabled_module_serves_its_routes():
    with build(reference={"enabled": True}) as c:
        r = c.post("/reference/v1/echo", json={"message": "hi"})
    assert r.status_code == 200
    assert r.json() == {"message": "hi"}


def test_unknown_module_in_config_fails_boot():
    """A typo must not silently leave a security module disabled."""
    with pytest.raises(UnknownModuleError) as e:
        build(ath={"enabled": True})
    assert "ath" in str(e.value)


def test_auth_and_llmproxy_are_not_yet_registered():
    """Until the real module lands, enabling it must fail closed, not stub."""
    with pytest.raises(UnknownModuleError):
        build(auth={"enabled": True})


# ---------------------------------------------------------------- error envelope


def test_error_envelope_shape():
    with build(reference={"enabled": True}) as c:
        r = c.get("/reference/v1/boom")
    assert r.status_code == 503
    assert r.json() == {
        "error": {
            "code": "upstream_unavailable",
            "message": "reference module: deliberate failure",
            "retryable": True,
        }
    }


def test_unknown_request_field_is_rejected_not_ignored():
    """SPEC.md §6 — the one place strictness beats tolerance."""
    with build(reference={"enabled": True}) as c:
        r = c.post("/reference/v1/echo", json={"message": "hi", "groups": ["admin"]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"



# ------------------------------------------------------------------ transport




def test_tcp_listener_refuses_non_loopback_without_explicit_optin():
    with pytest.raises(ValueError, match="allow_remote"):
        Config.model_validate(
            {"listener": {"uds": None, "tcp": "0.0.0.0:7241"}}
        )
    ok = Config.model_validate(
        {
            "listener": {
                "uds": None,
                "tcp": "0.0.0.0:7241",
                "allow_remote": True,
            }
        }
    )
    assert ok.listener.tcp == "0.0.0.0:7241"


def test_uds_mode_may_not_grant_other_users():
    with pytest.raises(ValueError, match="0660 or tighter"):
        Config.model_validate({"listener": {"uds": "/tmp/j.sock", "uds_mode": 0o666}})


def test_exactly_one_listener():
    with pytest.raises(ValueError, match="exactly one"):
        Config.model_validate({"listener": {"uds": "/tmp/j.sock", "tcp": "127.0.0.1:1"}})
    with pytest.raises(ValueError, match="exactly one"):
        Config.model_validate({"listener": {"uds": None}})




def test_unknown_config_key_is_rejected():
    with pytest.raises(ValueError):
        Config.model_validate({"listener": {"uds": "/tmp/j.sock"}, "lisener": {}})
