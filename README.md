# ElevenLabs Agent Migrator

Copy an ElevenLabs agent branch to another workspace. The migrator recreates
the agent's configuration, including its prompt, voice settings, workflow,
free-form procedures, simulation tests, text knowledge documents, and supported
tools. SIP and post-call callback migration are optional.

## Quick start

Requires Python 3.11 or newer on macOS or Linux. This is an alpha release.

```sh
pipx install elevenlabs-agent-migrator==0.1.2a0

elevenlabs-agent-migrator export \
  --agent-id AGENT_ID --branch-id BRANCH_ID --output "$HOME/agent-export.json"
elevenlabs-agent-migrator plan "$HOME/agent-export.json"
elevenlabs-agent-migrator copy "$HOME/agent-export.json" \
  --state "$HOME/.agent-migration"
```

The CLI prompts for each workspace's API key if
`ELEVENLABS_SOURCE_API_KEY` or `ELEVENLABS_TARGET_API_KEY` is unset.
`plan` runs offline. You can try it with `examples/synthetic-bundle.json` if
you have cloned this repository. The copied agent gets the original name
with a `(copy)` suffix unless you pass `--name`.

## Tools and credentials

By default, the copy uses the original tool URLs. To redirect tools and
callbacks on a specific host, use `--replace-origin OLD_HTTPS_ORIGIN=NEW_HTTPS_ORIGIN`.
The option is repeatable and preserves paths. Use the same options for later
commands in the same migration.

`plan` lists the tool credentials to re-enter for the destination. For each
entry, supply the full header value, including a `Bearer ` prefix if required.
For automation, pass `--secrets-file` with a JSON object mapping those IDs
to their values. Give the file mode `0600`. Keep exports and migration state
private. Exports may include sensitive text from prompts, documents or tests.
The journal records resource IDs so confirmed writes can be resumed. If a
write has an uncertain outcome, check the destination before retrying.

Client tools and webhook tools with HTTPS URLs are supported. URL credentials,
queries, fragments, redirecting tools, OAuth and mTLS need manual setup.
Custom voices must already be accessible in the destination. MCP servers,
custom models and other workspace-specific resources may need manual mapping.

## Post-call callbacks and SIP

Use `--copy-post-call-webhook` to recreate a source HMAC callback. Configure
the receiving server with the new signing secret saved in `webhook-secret.json`
in the private state directory. Without the flag, the destination workspace's
own callback settings may still apply.

`sip-import` creates an unassigned number from a single SIP trunk configuration.
`sip-assign` connects it to the copied agent. Both take the same export and
state directory as `copy`, plus `--phone-number` in E.164 format. If you used
`--name` or `--replace-origin`, pass the same options again. Use a separate
number for testing. Reusing the original number requires
`--allow-source-number` and coordination with the phone provider.

The migration copies a selected branch. Conversation history and agent version
history stay in the source workspace. Destination writes are covered by
simulated API tests. Validate the copy in your destination before switching
live calls.

## Development

```sh
uv run --locked --extra test pytest
uv run --locked --extra test ruff check .
```

The **Publish to PyPI** workflow takes a release tag matching the version in
`pyproject.toml`. By default it only tests and builds. Enable the publish input
to upload to PyPI through Trusted Publishing.
