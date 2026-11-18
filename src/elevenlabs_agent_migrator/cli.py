"""Copy one ElevenLabs branch and its supported dependencies to another workspace."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from copy import deepcopy
from getpass import getpass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

API = "https://api.elevenlabs.io"

ID = re.compile(r"[A-Za-z0-9_-]+")
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
REDACTED = "[REDACTED]"
CONFIG_KEYS = ("name", "conversation_config", "platform_settings", "workflow", "tags")
SECRET_FIELD = re.compile(
    r"(?:password|(?:api|auth)[-_]?key|authorization|token|credential|secret|cookie|signature)",
    re.I,
)
TEST_FIELDS = {
    "name",
    "dynamic_variables",
    "chat_history",
    "conversation_initiation_source",
    "success_condition",
    "success_conditions",
    "simulation_scenario",
    "simulation_max_turns",
    "simulation_environment",
    "tool_mock_config",
    "tool_mock_overrides",
    "evaluation_model",
    "simulated_user_model",
}
TRUNK_FIELDS = {
    "inbound": {
        "allowed_addresses",
        "allowed_numbers",
        "attributes_to_headers",
        "media_encryption",
        "remote_domains",
    },
    "outbound": {
        "address",
        "attributes_to_headers",
        "enabled_codecs",
        "headers",
        "media_encryption",
        "transport",
    },
}


class MigrationError(Exception):
    pass


class ProviderError(MigrationError):
    def __init__(self, method, path, status):
        self.status = status
        super().__init__(f"ElevenLabs {method} {path}: HTTP {status}")


def safe_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise MigrationError("Missing or invalid ElevenLabs resource ID.")
    return value


class Provider:
    def __init__(self, key, transport=None):
        self.client = httpx.Client(
            base_url=API,
            headers={"xi-api-key": key},
            timeout=20,
            transport=transport,
        )

    def __enter__(self):
        self.client.__enter__()
        return self

    def __exit__(self, *args):
        return self.client.__exit__(*args)

    def __call__(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise MigrationError(f"ElevenLabs {method} {path}: {type(exc).__name__}") from None
        if not response.is_success:
            raise ProviderError(method, path, response.status_code)
        return response.json() if response.content else {}


def scrub(value, key, *, headers=False):
    if isinstance(value, dict):
        return {
            name: REDACTED
            if (
                headers
                and SECRET_FIELD.search(name)
                and not (isinstance(item, dict) and set(item) in ({"secret_id"}, {"variable_name"}))
            )
            or (isinstance(item, str) and item and SECRET_FIELD.fullmatch(name))
            else scrub(item, key, headers=name in {"request_headers", "headers"})
            for name, item in value.items()
            if name != "access_info"
        }
    if isinstance(value, list):
        return [scrub(item, key) for item in value]
    if isinstance(value, str):
        value = value.replace(key, REDACTED)
        if value.startswith(("https://", "http://")):
            url = urlsplit(value)
            if url.username or url.password or url.query:
                return REDACTED
        return value
    return value


def objects(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from objects(item)


def rewrite(value, mapping, *, embedded=False):
    if isinstance(value, dict):
        return {mapping.get(k, k): rewrite(v, mapping, embedded=embedded) for k, v in value.items()}
    if isinstance(value, list):
        return [rewrite(item, mapping, embedded=embedded) for item in value]
    if isinstance(value, str):
        if value in mapping:
            return mapping[value]
        if embedded:
            for old, new in mapping.items():
                value = re.sub(
                    rf"(?<![A-Za-z0-9_-]){re.escape(old)}(?![A-Za-z0-9_-])",
                    lambda _, replacement=new: replacement,
                    value,
                )
    return value


def knowledge_refs(config):
    return {
        safe_id(ref.get("id")): ref
        for obj in objects(config)
        if isinstance(obj.get("knowledge_base"), list)
        for ref in obj["knowledge_base"]
        if isinstance(ref, dict)
    }


def tool_refs(config):
    ids = set()
    for obj in objects(config):
        if isinstance(obj.get("tool_id"), str) and obj["tool_id"].startswith("tool_"):
            ids.add(safe_id(obj["tool_id"]))
        for name in ("tool_ids", "referenced_tool_ids", "mocked_tool_ids"):
            if isinstance(obj.get(name), list):
                ids.update(safe_id(item) for item in obj[name] if isinstance(item, str))
        if isinstance(obj.get("tool_mock_overrides"), dict):
            ids.update(safe_id(item) for item in obj["tool_mock_overrides"])
    return ids


def sip_snapshot(number):
    result = {
        k: number[k]
        for k in (
            "phone_number",
            "phone_number_id",
            "provider",
            "label",
            "livekit_stack",
            "store_sip_messages",
        )
        if k in number
    }
    for direction, fields in TRUNK_FIELDS.items():
        source = number.get(f"{direction}_trunk")
        if source:
            result[f"{direction}_trunk"] = {
                k: v
                for k, v in source.items()
                if k in fields | {"username", "has_auth_credentials", "has_outbound_trunk"}
            }
    return result


def export_bundle(api, agent_id, branch_id, key):
    agent_id, branch_id = safe_id(agent_id), safe_id(branch_id)
    base = f"/v1/convai/agents/{agent_id}"
    agent = api(
        "GET",
        base,
        params={
            "branch_id": branch_id,
            "include_procedure_generated_workflow_elements": "false",
        },
    )
    if agent.get("branch_id") != branch_id:
        raise MigrationError("The API returned a different branch.")
    version = safe_id(agent.get("version_id"))
    config = {k: agent[k] for k in CONFIG_KEYS if k in agent}
    procedures = {
        proc_id: api(
            "GET",
            f"{base}/branches/{branch_id}/procedures/{safe_id(proc_id)}",
            params={"agent_version_id": version},
        )
        for proc_id in (agent.get("procedures") or {})
    }
    testing = (config.get("platform_settings") or {}).get("testing") or {}
    test_ids = set(testing.get("referenced_tests_ids") or []) | {
        item["test_id"] for item in testing.get("attached_tests") or []
    }
    tests = {
        ident: api("GET", f"/v1/convai/agent-testing/{safe_id(ident)}")
        for ident in sorted(test_ids)
    }
    refs = knowledge_refs([config, procedures])
    if any(ref.get("type") != "text" for ref in refs.values()):
        raise MigrationError("This release only exports text knowledge documents.")
    tools = {}
    for ident in sorted(tool_refs([config, agent.get("procedures"), tests])):
        item = api("GET", f"/v1/convai/tools/{ident}")
        tools[ident] = {k: item[k] for k in ("tool_config", "response_mocks") if k in item}
    workspace = api("GET", "/v1/convai/settings")
    try:
        workspace_id = api("GET", "/v1/user").get("workspace_id")
    except ProviderError as exc:
        if exc.status not in {401, 403}:
            raise
        workspace_id = None
    webhook_settings = (config.get("platform_settings", {}).get("workspace_overrides") or {}).get(
        "webhooks"
    ) or {}
    default_webhooks = workspace.get("webhooks") or {}
    webhook_id = webhook_settings.get("post_call_webhook_id") or default_webhooks.get(
        "post_call_webhook_id"
    )
    hooks = api("GET", "/v1/workspace/webhooks").get("webhooks", []) if webhook_id else []
    hooks = [
        {"id": item["webhook_id"], "url": item["webhook_url"], "auth_type": item["auth_type"]}
        for item in hooks
        if item["webhook_id"] == webhook_id
    ]
    if webhook_id and not hooks:
        raise MigrationError("The configured webhook definition could not be read.")
    return scrub(
        {
            "source": {
                "agent_id": agent_id,
                "branch_id": branch_id,
                "version_id": version,
                "workspace_id": workspace_id,
            },
            "agent_config": config,
            "procedures": procedures,
            "procedure_references": agent.get("procedures") or {},
            "tools": tools,
            "tests": tests,
            "knowledge_text_documents": {ident: content(api, ident) for ident in refs},
            "source_workspace_dependencies": {
                "post_call_webhooks": hooks,
                "agent_phone_bindings": [
                    sip_snapshot(number) for number in agent.get("phone_numbers") or []
                ],
            },
            "export_errors": {},
        },
        key,
    )


def content(api, ident):
    try:
        response = api.client.get(f"/v1/convai/knowledge-base/{ident}/content")
    except httpx.HTTPError:
        raise MigrationError("Could not read the knowledge document.") from None
    if not response.is_success:
        raise ProviderError("GET", "knowledge-base/content", response.status_code)
    if len(response.content) > 1_000_000:
        raise MigrationError("The knowledge document exceeds 1 MB.")
    return response.text


def validated_url(value):
    if not isinstance(value, str) or re.search(r"[\s\\]", value):
        raise MigrationError("Webhook URLs must be absolute HTTPS URLs.")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "{" in parsed.netloc
        or "}" in parsed.netloc
        or "%" in parsed.netloc
    ):
        raise MigrationError("Use HTTPS webhook URLs without credentials, queries or fragments.")
    try:
        _ = parsed.port
    except ValueError:
        raise MigrationError("Invalid webhook port.") from None
    return parsed


def origin(value):
    parsed = validated_url(value)
    hostname = parsed.hostname
    if ":" in hostname:
        hostname = f"[{hostname}]"
    if parsed.port not in (None, 443):
        hostname += f":{parsed.port}"
    return f"https://{hostname}"


def parse_origin_map(entries):
    result = {}
    for entry in entries:
        old, sep, new = entry.partition("=")
        if not sep or any(validated_url(url).path not in ("", "/") for url in (old, new)):
            raise MigrationError("Use --replace-origin OLD_HTTPS_ORIGIN=NEW_HTTPS_ORIGIN.")
        if origin(old) in result:
            raise MigrationError("Provide only one replacement for each origin.")
        result[origin(old)] = origin(new)
    return result


def mapped_url(value, origins):
    parsed = validated_url(value)
    if mapped := origins.get(origin(value)):
        target = urlsplit(mapped)
        return urlunsplit((target.scheme, target.netloc, parsed.path, "", ""))
    return value


def imported_hooks(dependencies):
    if "post_call_webhooks" in dependencies:
        return dependencies["post_call_webhooks"] or []
    # Accept the single webhook bucket in exports made before the generic format.
    values = [v for k, v in dependencies.items() if k.endswith("_webhooks") and v]
    if len(values) > 1:
        raise MigrationError("The export contains multiple webhook lists.")
    return values[0] if values else []


def prepare(bundle, origins=None):
    origins = origins or {}
    required_secrets = {}
    if bundle.get("export_errors") or bundle.get("knowledge_export_errors"):
        raise MigrationError(
            "The bundle is incomplete. Export the missing resources before copying."
        )
    config = deepcopy(bundle["agent_config"])
    deps = bundle.get("source_workspace_dependencies") or {}
    safe_id(bundle["source"]["agent_id"])
    procedures = bundle.get("procedures") or {}
    refs = knowledge_refs([config, procedures])
    if set(refs) != set(bundle.get("knowledge_text_documents") or {}):
        raise MigrationError("One or more knowledge documents are missing their content.")
    if any(item.get("type") != "text" for item in refs.values()):
        raise MigrationError("Only text knowledge documents are supported.")
    if any(
        not isinstance(item.get("content"), str)
        or item.get("type") != "free_form"
        or item.get("folder_parent_id")
        or item.get("guardrails")
        or item.get("visibility")
        for item in procedures.values()
    ):
        raise MigrationError("The bundle contains procedures that require a separate migration.")
    for obj in objects(config):
        if any(
            obj.get(k)
            for k in (
                "mcp_server_ids",
                "native_mcp_server_ids",
                "pronunciation_dictionary_locators",
                "destination_id",
                "custom_llm",
                "evaluation_criteria",
                "app_connector_ids",
            )
        ):
            raise MigrationError("Additional workspace resources require manual mapping.")
        if any(obj.get(k) == bundle["source"]["agent_id"] for k in ("agent_id", "target_agent_id")):
            raise MigrationError("The workflow references the source agent.")
    testing = config["platform_settings"].get("testing") or {}
    attached = {item["test_id"] for item in testing.get("attached_tests") or []}
    if attached != set(bundle.get("tests") or {}):
        raise MigrationError("One or more attached test definitions are missing.")
    if any(test.get("type") != "simulation" for test in (bundle.get("tests") or {}).values()):
        raise MigrationError("Only simulation tests are supported.")
    for info in (bundle.get("procedure_references") or {}).values():
        if not set(info.get("referenced_procedure_ids") or []) <= set(procedures):
            raise MigrationError("A referenced procedure is missing.")
        if not set(info.get("referenced_kb_ids") or []) <= set(refs):
            raise MigrationError("A knowledge document referenced by a procedure is missing.")
    tool_ids = tool_refs([config, bundle.get("procedure_references"), bundle.get("tests")])
    if not tool_ids <= set(bundle.get("tools") or {}):
        raise MigrationError("A tool definition from the source branch is missing.")
    platform = config["platform_settings"]
    for k in ("app_connectors", "simulation_library", "bi_reports"):
        item = platform.pop(k, None)
        if item and item.get("enabled"):
            raise MigrationError(f"The {k} setting requires a separate migration.")
    for k in ("summary_schedule", "smb_metadata", "overview_dashboard_template_id"):
        if platform.pop(k, None):
            raise MigrationError(f"The {k} setting requires a separate migration.")
    for k in ("analysis_llm_billed", "analysis_bedrock_llm", "safety", "ban"):
        platform.pop(k, None)
    testing.pop("referenced_tests_ids", None)
    overrides = platform.setdefault("workspace_overrides", {})
    conversation = config["conversation_config"]
    voice = conversation.get("tts", {})
    prompt = conversation["agent"]["prompt"]
    inline = prompt.pop("tools", None) or []
    known_names = {entry["tool_config"]["name"] for entry in bundle.get("tools", {}).values()}
    built_ins = {name for name, value in (prompt.get("built_in_tools") or {}).items() if value}
    if any(
        (item.get("type") == "system" and item.get("name") not in built_ins)
        or (item.get("type") != "system" and item.get("name") not in known_names)
        for item in inline
    ):
        raise MigrationError("An embedded tool is missing its resource definition.")
    for obj in objects(config):
        if any(obj.get(k) for k in ("target_agent_id", "transfer_to_agent", "run_subagent")):
            raise MigrationError("Agent transfers require a destination agent mapping.")
    if voice.get("speech_engine"):
        raise MigrationError("Custom speech engines require manual mapping.")
    if voice.get("supported_voices"):
        raise MigrationError("Additional voices require manual mapping.")
    auth = platform.get("auth") or {}
    auth.pop("shareable_token", None)  # issued by ElevenLabs for the destination agent
    if (platform.get("queueing_config") or {}).pop("hold_audio", None):
        raise MigrationError("Custom hold audio requires a separate export.")
    hookset = overrides.get("webhooks") or {}
    hooks = imported_hooks(deps)
    hook_id = hookset.get("post_call_webhook_id")
    candidates = [
        item
        for item in hooks
        if (not hook_id or item.get("id") == hook_id) and item.get("auth_type") == "hmac"
    ]
    if (hook_id or hooks) and len(candidates) != 1:
        raise MigrationError("The source post-call HMAC webhook is missing or ambiguous.")
    postcall = mapped_url(candidates[0]["url"], origins) if candidates else None
    if postcall:
        hookset["events"] = hookset.get("events") or ["transcript", "call_initiation_failure"]
        hookset.pop("post_call_webhook_id", None)
        overrides["webhooks"] = hookset

    def headers_for(original, owner):
        headers = original or {}
        if not isinstance(headers, dict):
            raise MigrationError("Unsupported webhook header format.")
        result = {}
        for header, value in headers.items():
            if not HEADER_NAME.fullmatch(header):
                raise MigrationError("Invalid webhook header name.")
            if isinstance(value, dict) and set(value) == {"secret_id"}:
                ident = safe_id(value["secret_id"])
            elif value == REDACTED or (isinstance(value, str) and SECRET_FIELD.search(header)):
                digest = hashlib.sha256(f"{owner}\x00{header.casefold()}".encode()).hexdigest()[:16]
                ident = f"header-{digest}"
            elif isinstance(value, str) or (
                isinstance(value, dict) and set(value) == {"variable_name"}
            ):
                result[header] = value
                continue
            else:
                raise MigrationError("Unsupported webhook header value.")
            required_secrets.setdefault(ident, []).append(f"{owner}: {header}")
            result[header] = {"secret_id": ident}
        return result

    tools = {}
    for ident, entry in bundle.get("tools", {}).items():
        safe_id(ident)
        tool = deepcopy(entry["tool_config"])
        if tool.get("type") == "client":
            tools[ident] = entry | {"tool_config": tool}
            continue
        if tool.get("type") != "webhook":
            raise MigrationError("Only webhook and client tools are supported.")
        schema = tool["api_schema"]
        if schema.get("auth_connection") or schema.get("mtls_auth_connection"):
            raise MigrationError("OAuth and mTLS connections require manual mapping.")
        schema["url"] = mapped_url(schema["url"], origins)
        paths = schema.get("path_params_schema")
        if paths == []:
            schema["path_params_schema"] = {}
        elif paths is not None and not isinstance(paths, dict):
            raise MigrationError("Unsupported path parameter schema.")
        schema.pop("kind", None)
        if tool.get("follow_redirects"):
            raise MigrationError("Redirecting webhook tools need to be reviewed separately.")
        if "request_headers" in schema:
            schema["request_headers"] = headers_for(schema["request_headers"], ident)
        for k in ("force_pre_tool_speech", "disable_interruptions"):
            tool.pop(k, None)
        tools[ident] = entry | {"tool_config": tool}
    initialization = overrides.get("conversation_initiation_client_data_webhook")
    if initialization:
        initialization["url"] = mapped_url(initialization["url"], origins)
        if "request_headers" in initialization:
            initialization["request_headers"] = headers_for(
                initialization["request_headers"], "conversation initiation"
            )
    if REDACTED in json.dumps(
        [tools, procedures, config, bundle.get("tests"), bundle.get("knowledge_text_documents")],
        ensure_ascii=False,
    ):
        raise MigrationError("The bundle contains other redacted values that must be resolved.")
    return config, procedures, refs, tools, postcall, required_secrets


def write_private(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
        temp = file.name
    os.replace(temp, path)


class Journal:
    def __init__(self, directory, signature):
        self.directory = directory
        self.path = directory / "migration.json"
        self.state = (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {
                "signature": signature,
                "ids": {},
                "pending": None,
            }
        )
        if self.state["signature"] != signature:
            raise MigrationError(
                "The state directory belongs to a different bundle or destination."
            )
        if self.state["pending"]:
            raise MigrationError(
                f"Uncertain result for step {self.state['pending']}. "
                "Check the destination workspace before resuming. "
                "The CLI will not repeat this write."
            )

    def save(self):
        write_private(self.path, self.state)

    def run(self, label, attempt):
        if label in self.state["ids"]:
            return self.state["ids"][label]
        self.state["pending"] = label
        self.save()
        try:
            result = attempt()
        except ProviderError as exc:
            if exc.status in {400, 401, 403, 404, 405, 422}:
                self.state["pending"] = None
                self.save()
            raise
        self.state["ids"][label] = result
        self.state["pending"] = None
        self.save()
        return result


def target_guard(api, source, voice_id):
    source_id = source["agent_id"]
    workspace_id = source.get("workspace_id")
    if workspace_id:
        destination_id = api("GET", "/v1/user").get("workspace_id")
        if not destination_id or workspace_id == destination_id:
            raise MigrationError("The destination key must belong to a different workspace.")
    try:
        api("GET", f"/v1/convai/agents/{safe_id(source_id)}")
    except ProviderError as exc:
        if exc.status != 404:
            raise
    else:
        raise MigrationError(
            "The destination key can access the source agent. Check the target workspace."
        )
    if voice_id:
        api("GET", f"/v1/voices/{safe_id(voice_id)}")


def copy_bundle(api, journal, bundle, origins, name, values, *, copy_post_call=False):
    config, procedures, refs, tools, postcall, required = prepare(bundle, origins)
    if values.keys() != required.keys() or any(
        not isinstance(value, str) or not value or "\r" in value or "\n" in value
        for value in values.values()
    ):
        raise MigrationError("Supply the complete value for every secret shown by plan.")
    if copy_post_call and postcall is None:
        raise MigrationError("The export has no post-call HMAC webhook.")
    if journal.state.get("copy_post_call", copy_post_call) != copy_post_call:
        raise MigrationError("Keep the same post-call webhook choice when resuming.")
    if not copy_post_call:
        workspace = config["platform_settings"].get("workspace_overrides") or {}
        workspace.pop("webhooks", None)
        postcall = None
    voice_id = config["conversation_config"].get("tts", {}).get("voice_id")
    target_guard(api, bundle["source"], voice_id)
    markers = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in values.items()}
    if journal.state.get("secret_markers", markers) != markers:
        raise MigrationError("Use the same secret values when resuming this copy.")
    journal.state.update({"secret_markers": markers, "copy_post_call": copy_post_call})
    journal.save()

    def create(label, path, body, response_key="id"):
        return journal.run(label, lambda: safe_id(api("POST", path, json=body)[response_key]))

    secret_ids = {
        ident: create(
            f"secret:{ident}",
            "/v1/convai/secrets",
            {"type": "new", "name": f"Migrated {ident}", "value": value},
            "secret_id",
        )
        for ident, value in values.items()
    }
    ids = {}
    for ident, ref in refs.items():
        ids[ident] = create(
            f"knowledge:{ident}",
            "/v1/convai/knowledge-base/text",
            {
                "name": ref.get("name") or f"Document {name}",
                "text": bundle["knowledge_text_documents"][ident],
            },
        )
    for ident, tool in tools.items():
        schema = rewrite(tool["tool_config"], secret_ids)
        payload = {"tool_config": schema}
        if tool.get("response_mocks"):
            payload["response_mocks"] = tool["response_mocks"]
        ids[ident] = create(f"tool:{ident}", "/v1/convai/tools", payload)
    hook = None
    if postcall:

        def create_hook():
            result = api(
                "POST",
                "/v1/workspace/webhooks",
                json={
                    "settings": {
                        "name": f"Migrated {name}",
                        "auth_type": "hmac",
                        "webhook_url": postcall,
                    }
                },
            )
            webhook_id = safe_id(result["webhook_id"])
            value = result["webhook_secret"]
            if not value:
                raise MigrationError(
                    "ElevenLabs did not return a signing secret for the new webhook."
                )
            write_private(journal.directory / "webhook-secret.json", {"webhook_secret": value})
            return webhook_id

        hook = journal.run("webhook", create_hook)

    for ident, test in bundle.get("tests", {}).items():
        test_body = {k: v for k, v in test.items() if k in TEST_FIELDS and v is not None}
        ids[ident] = create(
            f"test:{ident}",
            "/v1/convai/agent-testing/create",
            rewrite(test_body, ids, embedded=True),
        )

    # The new agent starts detached. Procedures can only be created on its destination branch.
    agent_id = create(
        "agent",
        "/v1/convai/agents/create",
        {
            "name": name,
            "conversation_config": {},
        },
        "agent_id",
    )
    root = f"/v1/convai/agents/{agent_id}"
    target = api("GET", root)
    branch_id = safe_id(target.get("main_branch_id") or target.get("branch_id"))
    proc_path = f"{root}/branches/{branch_id}/procedures"
    for ident, procedure in procedures.items():
        ids[ident] = create(
            f"procedure:{ident}",
            proc_path,
            {"name": procedure["name"], "type": procedure["type"]},
            "procedure_id",
        )
    for ident, procedure in procedures.items():
        body = rewrite(
            {k: procedure[k] for k in ("name", "type", "content", "trigger")}, ids, embedded=True
        )
        journal.run(
            f"procedure-draft:{ident}",
            lambda identifier=ids[ident], payload=body: (
                api("PATCH", f"{proc_path}/{identifier}/draft", json=payload),
                True,
            )[1],
        )
    config = rewrite(config, ids | secret_ids, embedded=True)
    config["name"] = name
    if hook:
        config["platform_settings"]["workspace_overrides"]["webhooks"]["post_call_webhook_id"] = (
            hook
        )
    journal.run(
        "publish",
        lambda: (api("PATCH", root, params={"branch_id": branch_id}, json=config), True)[1],
    )
    verified = api("GET", root, params={"branch_id": branch_id})
    expected_tools = {ids[ident] for ident in tools}
    expected_procedures = {ids[ident] for ident in procedures}
    if not expected_tools <= tool_refs(verified) or not expected_procedures <= set(
        verified.get("procedures") or {}
    ):
        raise MigrationError("Could not verify the copied tools and procedures.")
    for ident, source in procedures.items():
        published = api("GET", f"{proc_path}/{ids[ident]}")
        expected = rewrite(source["content"], ids, embedded=True)
        if published.get("content") != expected:
            raise MigrationError("Published procedure content differs from the prepared copy.")
    verified_platform = verified.get("platform_settings") or {}
    attached_tests = {
        x["test_id"] for x in (verified_platform.get("testing") or {}).get("attached_tests", [])
    }
    if attached_tests != {ids[ident] for ident in bundle.get("tests", {})}:
        raise MigrationError("Could not verify the attached tests.")
    if set(knowledge_refs(verified)) != {ids[ident] for ident in refs}:
        raise MigrationError("Could not verify the attached knowledge documents.")
    if hook and (
        ((verified_platform.get("workspace_overrides") or {}).get("webhooks") or {}).get(
            "post_call_webhook_id"
        )
        != hook
    ):
        raise MigrationError("Could not verify the post-call webhook.")
    target_cfg = verified.get("conversation_config") or {}
    source_prompt = config["conversation_config"]["agent"]["prompt"]["prompt"]
    copied_prompt = target_cfg.get("agent", {}).get("prompt", {}).get("prompt")
    if source_prompt != copied_prompt:
        raise MigrationError("The published prompt differs from the export.")
    tts = config["conversation_config"].get("tts") or {}
    if any(
        target_cfg.get("tts", {}).get(field) != tts[field]
        for field in ("voice_id", "model_id")
        if tts.get(field)
    ):
        raise MigrationError("The published voice or speech model differs from the export.")
    if target_cfg.get("agent", {}).get("language") != config["conversation_config"]["agent"].get(
        "language"
    ):
        raise MigrationError("The published agent language differs from the export.")
    return {"agent_id": agent_id, "branch_id": branch_id, "webhook_id": hook}


def sip_body(source, number, credentials):
    if source.get("provider") != "sip_trunk":
        raise MigrationError("Only SIP trunk numbers are supported.")
    if not re.fullmatch(r"\+[1-9][0-9]{6,14}", number):
        raise MigrationError("Enter the destination number in E.164 format.")
    if not source.get("inbound_trunk"):
        raise MigrationError("Inbound SIP settings are missing. Export the agent again.")
    result = {
        "provider": "sip_trunk",
        "phone_number": number,
        "label": source.get("label") or "Migrated SIP trunk",
    }
    for key in ("livekit_stack",):
        if source.get(key) is not None:
            result[key] = source[key]
    for direction, fields in TRUNK_FIELDS.items():
        original = source.get(f"{direction}_trunk")
        if not original or (
            direction == "outbound" and original.get("has_outbound_trunk") is False
        ):
            continue
        if original.get("custom_from_domain"):
            raise MigrationError(
                "A custom SIP From domain requires manual setup in the destination."
            )
        trunk = {k: original[k] for k in fields if original.get(k) is not None}
        username = credentials.get(f"{direction}_username") or original.get("username")
        password = credentials.get(f"{direction}_password")
        if original.get("has_auth_credentials") or username or password:
            if not username or not password:
                raise MigrationError(f"Missing SIP username or password for {direction}.")
            trunk["credentials"] = {"username": username, "password": password}
        if direction == "inbound" and not (
            trunk.get("allowed_addresses") or trunk.get("credentials")
        ):
            raise MigrationError("Inbound SIP requires credentials or an explicit IP allowlist.")
        if REDACTED in json.dumps(trunk, ensure_ascii=False):
            raise MigrationError("The SIP configuration has redacted settings.")
        result[f"{direction}_trunk_config"] = trunk
    return result


def single_phone(bundle):
    phones = (bundle.get("source_workspace_dependencies") or {}).get("agent_phone_bindings") or []
    if len(phones) != 1:
        raise MigrationError("SIP migration requires exactly one source number.")
    return phones[0]


def import_sip(api, journal, bundle, number, allow_source_number, creds):
    source = single_phone(bundle)
    if number == source.get("phone_number") and not allow_source_number:
        raise MigrationError(
            "Use a separate test number. Reusing the source number requires --allow-source-number."
        )
    body = sip_body(source, number, creds)
    if not journal.state["ids"].get("publish"):
        raise MigrationError("Run copy before importing a number.")
    label = f"sip:{number}"
    state = journal.state["ids"]
    if label not in state:
        rows = api("GET", "/v1/convai/phone-numbers")
        if not isinstance(rows, list):
            raise MigrationError("Unexpected response from the phone number list.")
        if any(row.get("phone_number") == number for row in rows):
            raise MigrationError("The number already exists in the destination.")
    phone_id = journal.run(
        label,
        lambda: safe_id(api("POST", "/v1/convai/phone-numbers", json=body)["phone_number_id"]),
    )
    if source.get("store_sip_messages") is not None:
        journal.run(
            f"sip-messages:{number}",
            lambda: (
                api(
                    "PATCH",
                    f"/v1/convai/phone-numbers/{phone_id}",
                    json={
                        "store_sip_messages": source["store_sip_messages"],
                    },
                ),
                True,
            )[1],
        )
    return phone_id


def assign_sip(api, journal, number):
    ids = journal.state["ids"]
    phone_id = safe_id(ids.get(f"sip:{number}"))
    agent_id = safe_id(ids.get("agent"))
    path = f"/v1/convai/phone-numbers/{phone_id}"
    phone = api("GET", path)
    if phone.get("phone_number") != number or phone.get("provider") != "sip_trunk":
        raise MigrationError("The imported SIP number changed.")
    assigned = (phone.get("assigned_agent") or {}).get("agent_id")
    if assigned and assigned != agent_id:
        raise MigrationError("The number is assigned to another agent.")
    if assigned != agent_id:
        journal.run(
            f"assign:{number}", lambda: (api("PATCH", path, json={"agent_id": agent_id}), True)[1]
        )
    result = api("GET", path)
    if (result.get("assigned_agent") or {}).get("agent_id") != agent_id:
        raise MigrationError("ElevenLabs did not confirm the number assignment.")
    return {"agent_id": agent_id, "phone_number_id": phone_id}


def secret(name, label):
    value = os.environ.get(name) or getpass(label + ": ")
    if not value:
        raise MigrationError(f"Missing value for {name}.")
    return value


def read_secret_values(path, required):
    if path:
        if path.stat().st_mode & 0o077:
            raise MigrationError("The secrets file must be private (0600).")
        value = json.loads(path.read_text())
        if not isinstance(value, Mapping):
            raise MigrationError("Expected a JSON object in the secrets file.")
        return value
    return {
        key: getpass(f"Value for {', '.join(uses)} ({key}): ") for key, uses in required.items()
    }


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="Export an agent branch from the source workspace.")
    export.add_argument("--agent-id", required=True)
    export.add_argument("--branch-id", required=True)
    export.add_argument("--output", type=Path, required=True)
    plan = sub.add_parser("plan", help="Inspect a local bundle without contacting ElevenLabs.")
    plan.add_argument("bundle", type=Path)
    plan.add_argument("--replace-origin", action="append", default=[], metavar="OLD=NEW")
    for command in ("copy", "sip-import", "sip-assign"):
        action = sub.add_parser(command)
        action.add_argument("bundle", type=Path)
        action.add_argument("--state", type=Path, required=True)
        action.add_argument("--replace-origin", action="append", default=[], metavar="OLD=NEW")
        if command == "copy":
            action.add_argument(
                "--secrets-file", type=Path, help="JSON mapping of input IDs to full header values"
            )
            action.add_argument("--copy-post-call-webhook", action="store_true")
        action.add_argument("--name", help="Override the destination agent name")
        if command != "copy":
            action.add_argument("--phone-number", required=True)
        if command == "sip-import":
            action.add_argument("--allow-source-number", action="store_true")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "export":
            if args.output.exists():
                raise MigrationError("The export file already exists.")
            source_key = secret("ELEVENLABS_SOURCE_API_KEY", "Source workspace API key")
            with Provider(source_key) as api:
                bundle = export_bundle(api, args.agent_id, args.branch_id, source_key)
            write_private(args.output, bundle)
            print(json.dumps({"exported": str(args.output)}, ensure_ascii=False))
            return

        bundle = json.loads(args.bundle.read_text())
        origins = parse_origin_map(args.replace_origin)
        _, procedures, docs, tools, postcall, required = prepare(bundle, origins)
        if args.command == "plan":
            phones = (bundle.get("source_workspace_dependencies") or {}).get(
                "agent_phone_bindings"
            ) or []
            print(
                json.dumps(
                    {
                        "procedures": len(procedures),
                        "documents": len(docs),
                        "tools": len(tools),
                        "tests": len(bundle.get("tests") or {}),
                        "webhook": bool(postcall),
                        "required_secrets": required,
                        "sip_numbers": len(phones),
                        "sip_has_full_source_settings": all(
                            "has_auth_credentials" in (phone.get("inbound_trunk") or {})
                            for phone in phones
                        )
                        if phones
                        else False,
                    }
                )
            )
            return

        name = args.name or f"{bundle['agent_config']['name']} (copy)"
        target_key = secret("ELEVENLABS_TARGET_API_KEY", "Destination workspace API key")
        signature = hashlib.sha256(
            json.dumps(
                [
                    hashlib.sha256(target_key.encode()).hexdigest(),
                    bundle,
                    name,
                    origins,
                ],
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if args.state.stat().st_mode & 0o077:
            raise MigrationError("The state directory must be private (0700).")
        with (args.state / "migration.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MigrationError(
                    "A migration is already running in this state directory."
                ) from None
            journal = Journal(args.state, signature)
            with Provider(target_key) as api:
                if args.command == "copy":
                    values = read_secret_values(args.secrets_file, required)
                    result = copy_bundle(
                        api,
                        journal,
                        bundle,
                        origins,
                        name,
                        values,
                        copy_post_call=args.copy_post_call_webhook,
                    )
                elif args.command == "sip-import":
                    src = single_phone(bundle)
                    if not all(
                        "has_auth_credentials" in src[k]
                        for k in ("inbound_trunk", "outbound_trunk")
                        if src.get(k)
                    ):
                        raise MigrationError(
                            "The exported SIP settings are incomplete. Export the agent again."
                        )
                    creds = {}
                    for direction in TRUNK_FIELDS:
                        trunk = src.get(f"{direction}_trunk") or {}
                        namespace = f"ELEVENLABS_SIP_{direction.upper()}"
                        username = os.environ.get(f"{namespace}_USERNAME")
                        if username:
                            creds[f"{direction}_username"] = username
                        if trunk.get("has_auth_credentials") or username:
                            creds[f"{direction}_password"] = secret(
                                f"{namespace}_PASSWORD",
                                f"SIP password for {direction}",
                            )
                    result = {
                        "phone_number_id": import_sip(
                            api,
                            journal,
                            bundle,
                            args.phone_number,
                            args.allow_source_number,
                            creds,
                        )
                    }
                else:
                    result = assign_sip(api, journal, args.phone_number)
        print(json.dumps(result, ensure_ascii=False))
    except (MigrationError, ValueError, KeyError, TypeError, OSError) as exc:
        # Provider response bodies and input values may contain credentials.
        detail = str(exc) if isinstance(exc, MigrationError) else type(exc).__name__
        raise SystemExit(detail) from None


if __name__ == "__main__":
    main()
