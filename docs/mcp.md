# MCP client and governed tool ingress

Status: implemented 0.12 preview for MCP tools; broader MCP extensions remain planned

Last verified: 2026-08-09 against MCP Python SDK 2.0.0 and protocol `2026-07-28`

## The deliberately small model surface

Configured MCP servers add exactly two stable Agno tools:

- `search_mcp_tools(query, server, limit, refresh)` discovers bounded metadata and a
  digest of the selected schema;
- `call_mcp_tool(server, tool_name, arguments, tool_digest)` refreshes discovery,
  verifies that digest, and only then dispatches the remote call.

Remote schemas are not copied into the base prompt. This keeps a harness with hundreds
of remote functions almost as cheap to start as one with none, while still letting the
model progressively disclose the exact schema it needs. It also gives policy one small,
stable ingress instead of a changing provider function set.

All remote annotations are returned as `annotation_trust = "untrusted_hint"`.
`call_mcp_tool` is conservatively `non_repeatable` even when a server claims
`readOnlyHint` or `idempotentHint`; an untrusted remote server cannot grant itself safer
retry semantics.

## Install

The MCP SDK is optional and does not enlarge the provider-neutral core:

```bash
pip install "agnoclaw[mcp]"
```

The supported range is `mcp>=2.0.0,<3`. Missing or incompatible SDKs fail on use with
`MCP_SDK_REQUIRED` and the exact extra to install.

## Configure a server

Use one command or one URL per named server. Server names must be unique and
provider-safe.

Local stdio:

```python
from agnoclaw import AgentHarness, HarnessConfig

config = HarnessConfig.quick(
    mcp_servers=[
        {
            "name": "repository",
            "command": ["uvx", "example-mcp-server"],
            "env": {"EXAMPLE_MODE": "read-only"},
        }
    ]
)
harness = AgentHarness("anthropic:claude-sonnet-4-6", config=config)
```

Remote Streamable HTTP:

```python
config = HarnessConfig.quick(
    network_allowed_hosts=["mcp.example.com"],
    mcp_servers=[
        {
            "name": "catalog",
            "url": "https://mcp.example.com/mcp",
            "transport": "streamable_http",
        }
    ],
)
```

Equivalent TOML:

```toml
network_enabled = true
network_enforce_https = true
network_block_private_hosts = true
network_allowed_hosts = ["mcp.example.com"]

[[mcp_servers]]
name = "catalog"
url = "https://mcp.example.com/mcp"
transport = "streamable_http"
```

Remote URLs default to Streamable HTTP. For compatibility, an omitted transport on a
URL whose path ends in `/sse` infers legacy SSE, including direct
`MCPToolkit(url=".../sse")` construction. An explicit transport always wins. New remote
deployments should use Streamable HTTP. Redirects are disabled on the owned Streamable
HTTP client.

Static HTTP headers are supported, but do not commit credentials to TOML. Resolve a
short-lived token in trusted host code and construct `HarnessConfig` there:

```python
config = HarnessConfig.quick(
    network_allowed_hosts=["mcp.example.com"],
    mcp_servers=[
        {
            "name": "catalog",
            "url": "https://mcp.example.com/mcp",
            "headers": {"Authorization": f"Bearer {token_from_broker}"},
        }
    ],
)
```

This is header injection, not an OAuth client. Interactive OAuth, Client ID Metadata
Documents, issuer validation, enterprise-managed authorization, refresh, and
identity-assertion brokering are not implemented yet.

## Runtime behavior

The SDK v2 `Client` negotiates the current stateless `server/discover` path and falls
back when it reaches an older supported server. Agnoclaw preserves the negotiated
protocol version and content-minimized server information in search results.

Discovery is deterministic and bounded:

| Limit | Value |
|---|---:|
| Servers per harness | 32 |
| Search return limit | 1–100 |
| Tools read from one server | 1,000 |
| Pages read from one server | 100 |
| Input schema | 65,536 bytes per tool |
| Output schema | 65,536 bytes per tool |
| Catalog metadata | 1,048,576 bytes per server |
| Search result | 1,048,576 bytes |
| Call arguments | 65,536 bytes |
| Tool result | 4,194,304 bytes |
| JSON nesting | 64 levels |

Search orders matches by token relevance, server, and tool name. A call always refreshes
the server's tool list and requires the exact SHA-256 digest over server, name, input
schema, and output schema. Removal produces `MCP_TOOL_NOT_FOUND`; any schema change
produces `MCP_TOOL_DRIFT` before remote dispatch. The model must search again instead of
silently calling a changed contract. Search reports `budget_truncated` when its byte
ceiling, rather than the requested count, bounds the result. Duplicate/invalid remote
names and oversized server metadata fail instead of creating ambiguous selection.

For direct stdio use, refresh and call share one client context so the server process
cannot change between schema verification and effect dispatch.

An all-server search isolates failures: healthy catalogs remain in `tools` and each
failed server appears in content-minimized `server_errors` with only server, code, and
retryability. Searching one explicitly named server raises its typed failure instead of
returning a partial answer.

Tool results retain every public MCP content item and `structured_content`. Private
`_meta` is removed recursively before the value can enter model context or artifact
storage; `private_metadata_withheld` reports only whether result-level private metadata
was present. Non-finite or otherwise non-JSON payloads fail rather than acquiring an
unstable digest.

## Lifecycle, effects, and cleanup

For eligible explicit-profile and legacy runs, configured MCP toolkits are rebuilt and closed as
run-owned built-ins. `search_mcp_tools` enters the lifecycle ledger as read-only;
`call_mcp_tool` enters as non-repeatable. Intent, lease renewal, policy, result
redaction, optional lossless spill, and settlement therefore use the same first-party
OperationGateway path as local tools. Failed or cancelled calls are never blindly
replayed.

The implementation is async-first. `await toolkit.aconnect()` and
`await toolkit.aclose()` are the explicit direct-host lifecycle. `connect()` is only
for synchronous code with no running event loop. A connected toolkit is owned by its
event loop; crossing loops or calling sync close on a live async client fails with a
typed lifecycle error.

Explicit-profile `run/arun` now enters the same lifecycle as `start()` and therefore
uses the declared configured-MCP operation ingress. Caller-supplied mutable MCP
toolkits remain raw extension ingress and are rejected before run creation rather than
having trust, effects, or recovery guessed. Only the named `legacy` profile retains the
serialized direct compatibility path, which claims no durable operation settlement.

## Network and trust boundary

Configured remote URLs pass the harness network posture before tool construction.
HTTPS, host allow/deny lists, literal private/loopback/link-local address blocking, and
`network_enabled` apply. Both search and call fail closed with `NETWORK_DISABLED` when
network use is off. Remote descriptions, schemas, annotations, content, resource links,
and errors are untrusted data, never authority or policy.

Application-level hostname checks are not a complete egress sandbox: DNS and routing
can change after configuration. Service deployments still need a network-isolated
worker or outbound proxy/firewall that permits only the intended MCP endpoints. Stdio
servers execute as host-configured local processes and need the same package provenance,
sandbox, filesystem, secret, and process controls as any other local extension.

## Error contract

Errors are content-minimized: remote exception messages, URLs, headers, arguments, and
results are not copied into the public details.

| Area | Codes |
|---|---|
| Dependency/configuration | `MCP_SDK_REQUIRED`, `MCP_SERVER_NAME_INVALID`, `MCP_SERVER_NAME_CONFLICT`, `MCP_SERVER_COMMAND_INVALID`, `MCP_SERVER_CONFIGURATION_INVALID`, `MCP_SERVER_TRANSPORT_INVALID`, `MCP_SERVER_BUDGET_EXCEEDED`, `MCP_SERVER_URL_DENIED` |
| Discovery/bounds | `MCP_SERVER_NOT_FOUND`, `MCP_SEARCH_QUERY_INVALID`, `MCP_SEARCH_LIMIT_INVALID`, `MCP_SEARCH_REFRESH_INVALID`, `MCP_DISCOVERY_FAILED`, `MCP_TOOL_NAME_INVALID`, `MCP_TOOL_NAME_CONFLICT`, `MCP_SCHEMA_BUDGET_EXCEEDED`, `MCP_CATALOG_BUDGET_EXCEEDED`, `MCP_SERVER_INFO_BUDGET_EXCEEDED`, `MCP_TOOL_BUDGET_EXCEEDED`, `MCP_PAGINATION_BUDGET_EXCEEDED` |
| Selection/call | `MCP_TOOL_SELECTION_INVALID`, `MCP_ARGUMENTS_INVALID`, `MCP_ARGUMENT_BUDGET_EXCEEDED`, `MCP_JSON_VALUE_REQUIRED`, `MCP_JSON_BUDGET_EXCEEDED`, `MCP_TOOL_NOT_FOUND`, `MCP_TOOL_DRIFT`, `MCP_CALL_FAILED`, `MCP_RESULT_BUDGET_EXCEEDED` |
| Transport/lifecycle | `MCP_CONNECTION_FAILED`, `MCP_LOOP_OWNERSHIP_VIOLATION`, `MCP_ASYNC_CONNECT_REQUIRED`, `MCP_ASYNC_CLOSE_REQUIRED`, `MCP_CLOSE_FAILED` |

`MCP_CONNECTION_FAILED`, `MCP_DISCOVERY_FAILED`, and `MCP_CALL_FAILED` are distinct so
retry and incident handling can identify the failed boundary without exposing remote
content.

## What is intentionally not claimed yet

The current preview implements tools, stdio, Streamable HTTP, legacy SSE through
explicit selection or `/sse` compatibility inference, pagination, structured content,
schema drift protection, conservative effects, and async ownership. It does not yet
expose MCP resources, prompts, subscriptions,
extension negotiation, MCP Apps, the Tasks extension, multi-round-trip user input, or
full authorization. Real network stdio/HTTP reference-server, reconnect, expired-auth,
malicious-server, and soak certification remain release gates.

The `2026-07-28` core deprecated Roots, Sampling, and Logging and moved Tasks from the
experimental core into `io.modelcontextprotocol/tasks`. New work follows the current
extension/stateless architecture rather than implementing those older core features as
mandatory parity.

## Primary sources

- [MCP 2026-07-28 release](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- [MCP Python SDK v2 client](https://py.sdk.modelcontextprotocol.io/client/)
- [MCP Python SDK transports](https://py.sdk.modelcontextprotocol.io/client/transports/)
- [MCP tool annotation trust](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/)
- [MCP Python SDK v2.0.0 release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0)
