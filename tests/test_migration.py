import json
from copy import deepcopy

import pytest

from elevenlabs_agent_migrator.cli import (
    Journal,
    MigrationError,
    ProviderError,
    assign_sip,
    copy_bundle,
    import_sip,
    main,
    parse_origin_map,
    prepare,
    scrub,
)

BASE = "https://api.example.test"


@pytest.fixture
def bundle():
    return {
        "source": {"agent_id": "agent_x", "workspace_id": "workspace_x"},
        "agent_config": {
            "name": "Source",
            "conversation_config": {
                "agent": {
                    "prompt": {
                        "prompt": 'Use [procedure id="proc_1"].',
                        "tool_ids": ["tool_x"],
                        "knowledge_base": [{"id": "kb_x", "name": "Hours", "type": "text"}],
                    }
                },
                "tts": {"voice_id": "voice_1"},
            },
            "platform_settings": {"testing": {"attached_tests": [{"test_id": "test_x"}]}},
            "workflow": {"analysis_settings": {"enabled": True, "strict": False}},
        },
        "procedures": {
            "proc_1": {
                "name": "First",
                "content": '[procedure id="proc_2"]',
                "type": "free_form",
                "trigger": "User asks",
            },
            "proc_2": {
                "name": "Second",
                "content": '[tool id="tool_x"] [kb id="kb_x"]',
                "type": "free_form",
                "trigger": "",
            },
        },
        "tools": {
            "tool_x": {
                "tool_config": {
                    "type": "webhook",
                    "name": "find_slots",
                    "tool_call_sound": "typing",
                    "api_schema": {
                        "method": "POST",
                        "url": "https://api.example.test/slots",
                        "request_headers": {"Authorization": "[REDACTED]"},
                        "path_params_schema": [],
                    },
                }
            },
        },
        "tests": {
            "test_x": {
                "name": "Search",
                "type": "simulation",
                "success_conditions": ["Correct"],
                "tool_mock_overrides": {"tool_x": []},
            },
        },
        "knowledge_text_documents": {"kb_x": "9–17"},
        "source_workspace_dependencies": {
            "post_call_webhooks": [
                {
                    "id": "wh_x",
                    "url": "https://api.example.test/events",
                    "auth_type": "hmac",
                },
            ],
            "agent_phone_bindings": [
                {
                    "provider": "sip_trunk",
                    "phone_number": "+12025550101",
                    "inbound_trunk": {
                        "has_auth_credentials": False,
                        "allowed_addresses": ["192.0.2.1"],
                    },
                    "outbound_trunk": {
                        "has_auth_credentials": True,
                        "username": "line",
                        "transport": "tcp",
                        "address": "sip.example.test",
                    },
                    "store_sip_messages": False,
                }
            ],
        },
    }


class Destination:
    def __init__(self):
        self.calls = []
        self.agent = None
        self.drafts = {}
        self.sip = None

    def __call__(self, method, path, **kwargs):
        body = deepcopy(kwargs.get("json", {}))
        self.calls.append((method, path, body))
        if path == "/v1/user":
            return {"workspace_id": "workspace_y"}
        if path == "/v1/convai/agents/agent_x":
            raise ProviderError(method, path, 404)
        if path.startswith("/v1/voices/"):
            return {}
        if path.endswith("/secrets"):
            return {"secret_id": "secret_y"}
        if path.endswith("/knowledge-base/text"):
            return {"id": "kb_y"}
        if path.endswith("/tools"):
            return {"id": "tool_y"}
        if path.endswith("/webhooks"):
            return {"webhook_id": "wh_y", "webhook_secret": "private-signing-value"}
        if path.endswith("/agent-testing/create"):
            return {"id": "test_y"}
        if path.endswith("/agents/create"):
            return {"agent_id": "agent_y"}
        if path.endswith("/agents/agent_y"):
            if method == "PATCH":
                self.agent = body
            return {"main_branch_id": "branch_y", **(self.agent or {}), "procedures": self.drafts}
        if path.endswith("/procedures") and method == "POST":
            return {"procedure_id": "dest_" + body["name"].lower()}
        if path.endswith("/draft"):
            self.drafts[path.split("/")[-2]] = body
            return {}
        if "/procedures/" in path and method == "GET":
            return self.drafts[path.split("/")[-1]]
        if path.endswith("/phone-numbers"):
            if method == "POST":
                self.sip = {"phone_number_id": "phone_y", **body}
                return self.sip
            return []
        if path.endswith("/phone_y"):
            if "agent_id" in body:
                self.sip["assigned_agent"] = {"agent_id": body["agent_id"]}
            return self.sip
        raise AssertionError((method, path))


def test_copies_dependencies_and_separates_sip_assignment(bundle, tmp_path):
    api, journal = Destination(), Journal(tmp_path, "signature")
    credentials = dict.fromkeys(prepare(bundle)[-1], "Bearer synthetic-test-value")
    opts = (api, journal, bundle, None, "Copy", credentials)
    assert copy_bundle(*opts, copy_post_call=True)["agent_id"] == "agent_y"
    assert api.agent["workflow"] == bundle["agent_config"]["workflow"]
    assert 'id="dest_second"' in api.drafts["dest_first"]["content"]
    assert api.drafts["dest_second"]["content"] == '[tool id="tool_y"] [kb id="kb_y"]'
    assert "private-signing-value" not in journal.path.read_text()
    assert (tmp_path / "webhook-secret.json").stat().st_mode & 0o777 == 0o600
    writes_before = len([m for m, _, _ in api.calls if m != "GET"])
    copy_bundle(*opts, copy_post_call=True)
    assert len([m for m, _, _ in api.calls if m != "GET"]) == writes_before
    for method, path, body in api.calls:
        if method == "POST" and path.endswith("/tools"):
            assert body["tool_config"]["tool_call_sound"] == "typing"
            schema = body["tool_config"]["api_schema"]
            assert schema["url"] == f"{BASE}/slots"
            assert schema["request_headers"]["Authorization"] == {"secret_id": "secret_y"}
        if path.endswith("/agent-testing/create"):
            assert "tool_y" in body["tool_mock_overrides"]
    with pytest.raises(MigrationError, match="separate test number"):
        import_sip(api, journal, bundle, "+12025550101", False, {})
    with pytest.raises(MigrationError, match="Missing SIP username or password"):
        import_sip(api, journal, bundle, "+12025550102", False, {})
    assert (
        import_sip(api, journal, bundle, "+12025550102", False, {"outbound_password": "sip-pass"})
        == "phone_y"
    )
    assert api.sip["inbound_trunk_config"]["allowed_addresses"] == ["192.0.2.1"]
    assert "agent_id" not in api.sip
    assert "sip-pass" not in journal.path.read_text()
    assert assign_sip(api, journal, "+12025550102")["phone_number_id"] == "phone_y"


def test_timeout_cannot_duplicate_external_write(tmp_path):
    state = Journal(tmp_path, "signature")

    def lose_receipt():
        raise MigrationError("timeout")

    with pytest.raises(MigrationError, match="timeout"):
        state.run("new-agent", lose_receipt)
    with pytest.raises(MigrationError, match="Uncertain result"):
        Journal(tmp_path, "signature")


def test_rejects_bad_dependencies_and_source_workspace(bundle, tmp_path):
    broken = deepcopy(bundle)
    broken["tests"] = {}
    with pytest.raises(MigrationError, match="attached test definitions"):
        prepare(broken)
    broken = deepcopy(bundle)
    broken["tools"]["tool_x"]["tool_config"]["api_schema"]["url"] = "http://api.example.test/slots"
    with pytest.raises(MigrationError, match="HTTPS"):
        prepare(broken)

    class WrongWorkspace(Destination):
        def __call__(self, method, path, **kwargs):
            if path == "/v1/user":
                return {"workspace_id": "workspace_x"}
            return super().__call__(method, path, **kwargs)

    with pytest.raises(MigrationError, match="different workspace"):
        copy_bundle(
            WrongWorkspace(),
            Journal(tmp_path, "signature"),
            bundle,
            None,
            "Copy",
            dict.fromkeys(prepare(bundle)[-1], "Bearer synthetic-test-value"),
        )


def test_generic_origin_mapping_and_default_callback_behavior(bundle, tmp_path):
    original = bundle["tools"]["tool_x"]["tool_config"]["api_schema"]
    original["request_headers"] = {
        "X-API-Key": {"secret_id": "key_x"},
        "X-Format": "v1",
    }
    alternate = deepcopy(original)
    alternate["url"] = "https://other.example.test/path/{id}"
    alternate["request_headers"] = {"X-API-Key": {"secret_id": "key_alt"}}
    bundle["tools"]["tool_alt"] = {
        "tool_config": {"type": "webhook", "name": "alternate", "api_schema": alternate}
    }
    origins = parse_origin_map(["https://api.example.test=https://target.example.test"])
    _, _, _, tools, _, required = prepare(bundle, origins)
    assert (
        tools["tool_x"]["tool_config"]["api_schema"]["url"] == "https://target.example.test/slots"
    )
    assert tools["tool_alt"]["tool_config"]["api_schema"]["url"] == alternate["url"]
    assert required == {"key_x": ["tool_x: X-API-Key"], "key_alt": ["tool_alt: X-API-Key"]}

    api, journal = Destination(), Journal(tmp_path, "signature")
    values = {"key_x": "synthetic-one", "key_alt": "synthetic-two"}
    copy_bundle(api, journal, bundle, origins, "Copy", values)
    assert not any(path.endswith("/webhooks") for _, path, _ in api.calls)
    assert all(value not in journal.path.read_text() for value in values.values())


def test_redacts_literal_header_values_but_keeps_references():
    source = {
        "request_headers": {
            "X-API-Key": "literal-header-value",
            "Cookie": {"value": "literal-cookie-value"},
            "Authorization": {"secret_id": "secret_x"},
            "X-Auth-Token": {"variable_name": "token"},
            "Accept": "application/json",
        },
    }
    assert scrub(source, "source-api-key")["request_headers"] == {
        "X-API-Key": "[REDACTED]",
        "Cookie": "[REDACTED]",
        "Authorization": {"secret_id": "secret_x"},
        "X-Auth-Token": {"variable_name": "token"},
        "Accept": "application/json",
    }


def test_plain_agent_needs_no_backend_or_tool_credentials(tmp_path, capsys):
    bundle = {
        "source": {"agent_id": "agent_x", "workspace_id": "workspace_x"},
        "agent_config": {
            "name": "Source",
            "conversation_config": {"agent": {"prompt": {"prompt": "Hello"}}},
            "platform_settings": {},
        },
    }
    bundle_path = tmp_path / "agent.json"
    bundle_path.write_text(json.dumps(bundle))
    main(["plan", str(bundle_path)])
    assert json.loads(capsys.readouterr().out)["required_secrets"] == {}

    api = Destination()
    assert (
        copy_bundle(api, Journal(tmp_path, "signature"), bundle, None, "Copy", {})["agent_id"]
        == "agent_y"
    )
    assert not any(path.endswith(("/secrets", "/webhooks")) for _, path, _ in api.calls)
