# A2A — Agent-to-Agent protocol for Hermes

Talk to other agents, and let other agents talk to you, over the open
[A2A protocol](https://a2a-protocol.org) **v1.0**. Works with any A2A-compliant
peer (another Hermes, LangChain, CrewAI, Google ADK, OpenClaw, …). Stdlib only —
no `a2a-sdk` dependency.

## Enable

```bash
hermes gateway setup      # pick A2A, or:
```

```yaml
# ~/.hermes/config.yaml
gateway:
  platforms:
    a2a:
      enabled: true
      extra:
        port: 9900

# peers you want to call (outbound):
a2a_agents:
  researcher:
    url: "http://localhost:9999"
    auth: { type: bearer, token: "sk-..." }
    timeout: 120
    capabilities: [web_search, research]
```

## Outbound — call other agents

The agent gets five tools:

- `a2a_discover(url)` — what can this agent do?
- `a2a_call(agent, message, context_id?)` — send it a task, get the reply.
- `a2a_list()` — configured peers, saved conversations, metrics.
- `a2a_history(context_id)` — recall a saved A2A conversation.
- `a2a_orchestrate(capability, message, mode?)` — fan-out a task to every
  peer advertising a capability (`all` / `first` / `best`).

## ERC-8004 — find peers on-chain, choose them by reputation

With an `erc8004` section in `config.yaml`, peers also come from the
[ERC-8004](https://eips.ethereum.org/EIPS/eip-8004) Identity Registry: every
registered agent points to a registration file, which points to its Agent
Card, and the card's skills become the peer's capabilities. The file must name
the same agent id, so an agent cannot borrow another agent's file. Two more
tools appear:

- `a2a_delegate(capability, message)` — send the task to the one peer with the
  best average rating for that capability in the Reputation Registry. Peers
  that nobody has rated yet go first; ties are broken at random.
- `a2a_feedback(agent, value, capability)` — write a rating from 0 to 100
  on-chain, signed with this agent's wallet, after checking the answer.

```yaml
erc8004:
  rpc_url: "https://base-sepolia-rpc.publicnode.com"
  identity_registry: "0x8004A818BFB912233c491871b3d84c89A494BD9e"
  reputation_registry: "0x8004B663056A597Dffe9eCcC1965A193B7388713"
  start_block: 54282581          # agents registered before this block are ignored
  owners: []                     # optional: only agents registered by these addresses
  reviewers: []                  # whose ratings count; empty: only this agent's own
  key_env: "ERC8004_PRIVATE_KEY" # env var with the private key of a TEST wallet
```

It needs `web3` in Hermes's environment. Ratings use `tag1 = "task"` and
`tag2 = <capability>`.

### Try it

Status: the ERC-8004 tools pass their tests and work when they are called directly, but no
session driven by an LLM has used them yet.

Hermes's lock file covers only Python 3.14. A plain `pip install -e .` stops on a Pillow
version conflict that only the lock file settles, so install from the lock with
[uv](https://docs.astral.sh/uv/), and then add `web3`. Any Python 3.14 environment works;
with conda:

```bash
conda create -n hermes -c conda-forge python=3.14
conda activate hermes
git clone --depth 1 --branch erc8004 https://github.com/AldoBuzi/hermes-agent-erc8004.git
cd hermes-agent-erc8004
uv export --frozen --no-dev --no-hashes --no-emit-project -o /tmp/hermes-requirements.txt
uv pip install --python "$(which python)" --no-deps -r /tmp/hermes-requirements.txt
uv pip install --python "$(which python)" --no-deps -e .
uv pip install --python "$(which python)" -c /tmp/hermes-requirements.txt web3==8.0.0
```

Give this setup its own home folder, so that it does not touch `~/.hermes`, and choose a
model there. `hermes model` also asks for the provider's API key.

```bash
export HERMES_HOME=~/hermes-erc8004-home
hermes model
```

Then add the `erc8004` section above to `$HERMES_HOME/config.yaml` (create the file if it does
not exist), and put the private key of a test wallet in the environment. `read -s` keeps the
key off the screen and out of the shell history.

```bash
read -s ERC8004_PRIVATE_KEY && export ERC8004_PRIVATE_KEY
```

The `a2a` toolset is off by default, so turn it on for the session with `-t a2a`. The model
is then offered only the A2A tools.

```bash
hermes chat -t a2a --oneshot -q 'Use a2a_delegate to send this task to an agent with the capability flight-search: ... Check the answer, then rate the agent with a2a_feedback.'
```

## Inbound — be callable

When the `a2a` platform is enabled, Hermes serves a v1.0 Agent Card at
`http://<host>:<port>/.well-known/agent-card.json` (the legacy
`/.well-known/agent.json` path is also answered for pre-1.0 clients) and
accepts JSON-RPC
`message/send`, `message/stream` (SSE), `tasks/get|list|cancel|subscribe`,
and push notification configs (inline or via
`tasks/pushNotificationConfig/create`). Incoming tasks are injected into your
**live** agent session — the same agent that's talking to you, with full
memory — and the reply is returned over A2A. Completed tasks stay queryable
via `tasks/get`.

## Security

- **No token ⇒ localhost only.** The server binds `127.0.0.1` and refuses to
  widen unless you configure a token *and* set `A2A_HOST`.
  Without a token it also refuses browser-originated POSTs (`Origin`,
  cross-site `Sec-Fetch-Site`, a missing or non-JSON `Content-Type`).
- **No token ⇒ loopback `Host` only.** Every route refuses a `Host` that is
  not a loopback name (`localhost`, `127.x.x.x`, `[::1]`) or the
  `A2A_PUBLIC_URL` host, so a DNS-rebound page cannot reach it. Only set
  `A2A_PUBLIC_URL` to a name you control.
- **Per-peer tokens**: `A2A_PEER_TOKENS="alice:tok1,bob:tok2"` gives each
  remote agent its own credential; that authenticated name (never anything
  in the request body) drives rate limiting, trust, and audit.
- Inbound text — including `/`-prefixed text — is run through
  prompt-injection filters and framed as untrusted peer input; remote peers
  cannot invoke operator slash commands.
- Outbound text is scrubbed of credential-shaped strings.
- Push callbacks are SSRF-guarded and HMAC-SHA256 signed (`X-A2A-Signature`).
- Every exchange is logged to `~/.hermes/a2a_audit.jsonl`.
- Conversations persist to `~/.hermes/a2a_conversations/` — they survive context
  compaction and restarts (`a2a_history` recalls them).

## Env vars

| Var | Default | Meaning |
|---|---|---|
| `A2A_PEER_TOKENS` | _(unset)_ | Per-peer credentials `name:token,…` (preferred). |
| `A2A_BEARER_TOKEN` | _(unset)_ | Shared token; identity falls back to caller IP. |
| `A2A_HOST` | `127.0.0.1` | Bind host. Only widens with a token set. |
| `A2A_PORT` | `9900` | Inbound port. |
| `A2A_AGENT_NAME` | hostname-derived | Name on the Agent Card. |
| `A2A_PUBLIC_URL` | _(unset)_ | Routable URL advertised on the card (reverse proxies). |
| `A2A_TRUSTED_PEERS` | _(unset)_ | Allow-list of authenticated identities. Required for a non-loopback bind (with a token set) unless `A2A_ALLOW_ALL_USERS=true`; otherwise dispatch is refused. |
| `A2A_ALLOW_ALL_USERS` | `false` | Allow any authed peer (dev only). |
| `A2A_RATE_LIMIT` | `60` | Requests/minute per identity. |
| `A2A_MAX_PINGPONG_TURNS` | `5` | Anti-loop turn cap per context (max 20). |
| `A2A_REPLY_TIMEOUT` | `300` | Seconds to wait for the agent's reply; the orphan sweep never fails a task before this window (floor 300s) or while a request still waits on it. |
| `A2A_PUSH_SECRET` | bearer token | HMAC secret for push signing. |
| `A2A_ADVERTISED_TOOLSETS` | all registered | Restrict skills on the Agent Card. |

See `DESIGN.md` for architecture and the requirement-tracing table.
