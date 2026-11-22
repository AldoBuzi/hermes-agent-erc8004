"""ERC-8004 support for the A2A client tools: on-chain discovery, reputation and ratings.

ERC-8004 (https://eips.ethereum.org/EIPS/eip-8004) puts two registries on a blockchain. The
Identity Registry gives every agent an id and the URI of a registration file, which names the
agent's A2A endpoint. The Reputation Registry stores the ratings that clients give an agent
after a task. With an ``erc8004`` section in config.yaml:

- the client tools find peers in the Identity Registry, with the skills that each peer's own
  Agent Card declares, in addition to the hand-written ``a2a_agents`` list;
- ``a2a_delegate`` sends a task to the one peer that the selection strategy trusts most for
  that skill;
- ``a2a_feedback`` writes a rating on-chain, signed with this agent's own wallet.

    erc8004:
      rpc_url: "https://base-sepolia-rpc.publicnode.com"
      identity_registry: "0x8004A818BFB912233c491871b3d84c89A494BD9e"
      reputation_registry: "0x8004B663056A597Dffe9eCcC1965A193B7388713"
      start_block: 54282581           # agents registered before this block are ignored
      owners: []                      # optional: only agents registered by these addresses
      reviewers: []                   # whose ratings count; empty means only this agent's own
      key_env: "ERC8004_PRIVATE_KEY"  # environment variable with the wallet's private key
      seed: 42                        # optional: repeatable random choices
      strategy: S2                    # S2 (the default) or S3, see below
      delegate_only: false            # true hides a2a_call and a2a_orchestrate: every task uses the strategy
      trust:                          # optional, S3 only: each key overrides one default
        other_weight: 0.1             # the weight of a rating from a reviewer not in reviewers
        half_life: 5                  # after this many newer ratings, a rating counts half; null: off
        owner_weight: 0.5             # how much of its owner's record a new agent inherits; 0: off
        choice: sample                # sample (Thompson sampling) or mean (no exploration)

The two strategies:

- S2, plain reputation: the best average of the reviewers' ratings (getSummary). Peers that
  nobody has rated yet go first.
- S3, trust-aware: every rating, read from the NewFeedback events, adds to a Beta
  distribution of the peer's chance to answer correctly. Ratings from the reviewers count
  fully and the others only a little (against fake reviewers); newer ratings count more
  (against an agent that turns bad); and a new agent starts from the record of its owner's
  other agents (against clone farms). The peer with the highest sample wins, so peers with few
  ratings are still tried now and then.

web3 is imported only when the section is used, so installs without it are unaffected.
"""

from __future__ import annotations

import json
import logging
import random
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Optional

from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret

logger = logging.getLogger(__name__)

LOG_CHUNK_BLOCKS = 50_000  # public RPC servers refuse log queries over more blocks
FEEDBACK_TAG = "task"      # tag1 of every rating; tag2 is the capability (the skill id)
CARD_PATH = "/.well-known/agent-card.json"
_HTTP_TIMEOUT = 10
_DEFAULT_KEY_ENV = "ERC8004_PRIVATE_KEY"


def _function(name: str, inputs: list[tuple[str, str]], outputs: list[tuple[str, str]],
              mutability: str = "view") -> dict:
    """One ABI entry. The types follow the official ABIs (github.com/erc-8004/erc-8004-contracts, abis/)."""
    return {"type": "function", "name": name, "stateMutability": mutability,
            "inputs": [{"name": n, "type": t} for n, t in inputs],
            "outputs": [{"name": n, "type": t} for n, t in outputs]}


def _event(name: str, inputs: list[tuple[str, str, bool]]) -> dict:
    """One ABI event, from (name, type, indexed) for each of its inputs."""
    return {"type": "event", "name": name, "anonymous": False,
            "inputs": [{"name": n, "type": t, "indexed": i} for n, t, i in inputs]}


# Only the parts of the two registries that the tools use.
_IDENTITY_ABI = [
    {"type": "event", "name": "Registered", "anonymous": False, "inputs": [
        {"name": "agentId", "type": "uint256", "indexed": True},
        {"name": "agentURI", "type": "string", "indexed": False},
        {"name": "owner", "type": "address", "indexed": True}]},
    _function("tokenURI", [("tokenId", "uint256")], [("", "string")]),
    _function("ownerOf", [("tokenId", "uint256")], [("", "address")]),
]
_REPUTATION_ABI = [
    _function("getSummary",
              [("agentId", "uint256"), ("clientAddresses", "address[]"), ("tag1", "string"), ("tag2", "string")],
              [("count", "uint64"), ("summaryValue", "int128"), ("summaryValueDecimals", "uint8")]),
    _function("giveFeedback",
              [("agentId", "uint256"), ("value", "int128"), ("valueDecimals", "uint8"), ("tag1", "string"),
               ("tag2", "string"), ("endpoint", "string"), ("feedbackURI", "string"), ("feedbackHash", "bytes32")],
              [], "nonpayable"),
    # The events give each rating with its reviewer and its block, which S3 needs.
    _event("NewFeedback",
           [("agentId", "uint256", True), ("clientAddress", "address", True), ("feedbackIndex", "uint64", False),
            ("value", "int128", False), ("valueDecimals", "uint8", False), ("indexedTag1", "string", True),
            ("tag1", "string", False), ("tag2", "string", False), ("endpoint", "string", False),
            ("feedbackURI", "string", False), ("feedbackHash", "bytes32", False)]),
    _event("FeedbackRevoked",
           [("agentId", "uint256", True), ("clientAddress", "address", True), ("feedbackIndex", "uint64", True)]),
]

_rng: Optional[random.Random] = None


def random_source(cfg: dict) -> random.Random:
    """One generator per process, seeded once: ties are broken at random, but repeatably with a seed."""
    global _rng
    if _rng is None:
        _rng = random.Random(cfg.get("seed"))
    return _rng


def _web3(cfg: dict) -> Any:
    from web3 import Web3  # imported here, so that installs without ERC-8004 do not need web3
    return Web3(Web3.HTTPProvider(str(cfg["rpc_url"]), request_kwargs={"timeout": 60}))


def _contract(w3: Any, address: str, abi: list) -> Any:
    return w3.eth.contract(address=w3.to_checksum_address(address), abi=abi)


def _get_json(url: str) -> dict:
    """GET a JSON document. The URIs come from strangers' registrations, so only http(s) is fetched."""
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"unsupported URI {url[:40]!r}")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _registered_ids(identity: Any, start_block: int, owners: Optional[list[str]]) -> list[int]:
    """Ids of the agents registered since start_block, read from the Registered events in chunks."""
    latest = identity.w3.eth.block_number
    filters = {"owner": owners} if owners else None
    ids: list[int] = []
    for first in range(start_block, latest + 1, LOG_CHUNK_BLOCKS):
        last = min(first + LOG_CHUNK_BLOCKS - 1, latest)
        events = identity.events.Registered().get_logs(argument_filters=filters, from_block=first, to_block=last)
        ids.extend(event["args"]["agentId"] for event in events)
    return ids


def _peer(identity: Any, agent_id: int, registry_id: str) -> dict:
    """Follow agentId -> agentURI -> registration file -> A2A endpoint -> Agent Card to a peer entry."""
    registration = _get_json(identity.functions.tokenURI(agent_id).call())
    # The file must point back to this agent; otherwise an agent could publish another agent's file.
    if not any(entry.get("agentId") == agent_id and str(entry.get("agentRegistry", "")).lower() == registry_id
               for entry in registration.get("registrations") or []):
        raise ValueError("its registration file names another agent")
    endpoints = [s.get("endpoint") for s in registration.get("services") or [] if s.get("name") == "A2A"]
    if not endpoints or not isinstance(endpoints[0], str):
        raise ValueError("its registration file has no A2A service")
    # The endpoint is usually the Agent Card's URL; a bare base URL is accepted too.
    endpoint = endpoints[0]
    base_url = endpoint[: -len(CARD_PATH)] if endpoint.endswith(CARD_PATH) else endpoint.rstrip("/")
    card = _get_json(base_url + CARD_PATH)
    return {
        "url": base_url,
        "capabilities": [skill["id"] for skill in card.get("skills") or [] if skill.get("id")],
        "agent_id": agent_id,
        "owner": identity.functions.ownerOf(agent_id).call(),
        "card_name": str(card.get("name") or "agent"),
    }


def discover_peers(cfg: dict) -> dict[str, dict]:
    """Peers from the Identity Registry, shaped like the entries of config.yaml's ``a2a_agents``.

    A peer's name is "<name on its Agent Card>#<agentId>", because two agents may use the same
    name. Agents whose chain of documents is broken, or that are offline, are skipped.
    """
    w3 = _web3(cfg)
    identity = _contract(w3, cfg["identity_registry"], _IDENTITY_ABI)
    registry_id = f"eip155:{w3.eth.chain_id}:{identity.address}".lower()
    owners = [w3.to_checksum_address(owner) for owner in cfg.get("owners") or []] or None
    peers: dict[str, dict] = {}
    for agent_id in _registered_ids(identity, int(cfg.get("start_block") or 0), owners):
        try:
            peer = _peer(identity, agent_id, registry_id)
        except Exception as e:  # one broken or offline agent must not hide the others
            logger.info("ERC-8004 agent %s skipped: %s", agent_id, e)
            continue
        peers[f"{peer['card_name']}#{agent_id}"] = peer
    return peers


def _account(cfg: dict) -> Any:
    from eth_account import Account
    name = str(cfg.get("key_env") or _DEFAULT_KEY_ENV)
    key = _get_scoped_secret(name)
    if not key:
        raise ValueError(f"{name} is not set: it must hold the private key of a test wallet")
    return Account.from_key(key)


def reviewers(cfg: dict) -> list[str]:
    """The addresses whose ratings count. getSummary refuses an empty list."""
    return [str(address) for address in cfg.get("reviewers") or []] or [_account(cfg).address]


def ratings(cfg: dict, agent_ids: list[int], capability: str) -> dict[int, tuple[int, float]]:
    """For each agent: how many ratings the reviewers gave it for this capability, and their average (0-100)."""
    w3 = _web3(cfg)
    reputation = _contract(w3, cfg["reputation_registry"], _REPUTATION_ABI)
    clients = [w3.to_checksum_address(address) for address in reviewers(cfg)]
    result: dict[int, tuple[int, float]] = {}
    for agent_id in agent_ids:
        count, value, decimals = reputation.functions.getSummary(agent_id, clients, FEEDBACK_TAG, capability).call()
        result[agent_id] = (count, value / 10**decimals if count else 0.0)
    return result


def choose(rated: dict[str, tuple[int, float]], rng: random.Random) -> str:
    """The peer to delegate to, from {name: (number of ratings, average)}: the plain average (strategy S2).

    Peers that nobody has rated yet go first, so every peer gets at least one chance. After that,
    the highest average wins. Ties are broken at random, never by name.
    """
    unrated = sorted(name for name, (count, _) in rated.items() if count == 0)
    if unrated:
        return rng.choice(unrated)
    best = max(average for _, average in rated.values())
    return rng.choice(sorted(name for name, (_, average) in rated.items() if average == best))


class Rating(NamedTuple):
    """One rating that S3 uses: one NewFeedback event that was not revoked."""

    agent_id: int
    reviewer: str
    value: float  # from 0 (wrong or failed) to 100 (correct)
    block: int    # the block where it was stored, which says how old it is


@dataclass
class TrustSettings:
    """The settings of S3. Each signal can be switched off, to measure what it is worth."""

    trusted: set[str] = field(default_factory=set)  # lower-case addresses whose ratings count fully
    other_weight: float = 0.1        # the weight of a rating from any other reviewer; 1: off
    half_life: Optional[float] = 5   # after this many newer ratings of the agent, a rating counts half; None: off
    owner_weight: float = 0.5        # how much of the owner's other agents' record a new agent inherits; 0: off
    choice: str = "sample"           # "sample": Thompson sampling; "mean": the highest mean, no exploration
    prior: tuple[float, float] = (1.0, 1.0)  # the Beta prior (successes, failures) of an agent with no ratings


def trust_settings(cfg: dict) -> TrustSettings:
    """S3's settings: the reviewers are the trusted ones, and the ``trust`` part overrides the defaults."""
    values = dict(cfg.get("trust") or {})
    if "prior" in values:
        values["prior"] = tuple(values["prior"])
    return TrustSettings(trusted={address.lower() for address in reviewers(cfg)}, **values)


def ratings_from_logs(new_logs: list, revoked_logs: list, capability: str) -> list[Rating]:
    """The ratings for this capability in NewFeedback events, without those that FeedbackRevoked events cancel."""
    revoked = {(e["args"]["agentId"], e["args"]["clientAddress"], e["args"]["feedbackIndex"]) for e in revoked_logs}
    found = []
    for event in new_logs:
        args = event["args"]
        if (args["tag1"], args["tag2"]) != (FEEDBACK_TAG, capability):
            continue
        if (args["agentId"], args["clientAddress"], args["feedbackIndex"]) in revoked:
            continue
        found.append(Rating(args["agentId"], args["clientAddress"], args["value"] / 10 ** args["valueDecimals"],
                            event["blockNumber"]))
    return found


def all_ratings(cfg: dict, agent_ids: list[int], capability: str) -> list[Rating]:
    """Every rating of these agents for this capability since start_block, from any reviewer.

    Unlike getSummary, the events give each rating with its reviewer and its block. They are read
    in chunks, because public RPC servers refuse log queries over too many blocks.
    """
    w3 = _web3(cfg)
    reputation = _contract(w3, cfg["reputation_registry"], _REPUTATION_ABI)
    filters = {"agentId": list(agent_ids)}
    latest = w3.eth.block_number
    new_logs: list = []
    revoked_logs: list = []
    for first in range(int(cfg.get("start_block") or 0), latest + 1, LOG_CHUNK_BLOCKS):
        last = min(first + LOG_CHUNK_BLOCKS - 1, latest)
        for logs, event in ((new_logs, reputation.events.NewFeedback), (revoked_logs, reputation.events.FeedbackRevoked)):
            logs += event().get_logs(argument_filters=filters, from_block=first, to_block=last)
    return ratings_from_logs(new_logs, revoked_logs, capability)


def _evidence(found: list[Rating], settings: TrustSettings) -> dict[int, tuple[float, float]]:
    """For each rated agent: its weighted successes and failures, after the reviewer and recency weights."""
    by_agent: dict[int, list[Rating]] = defaultdict(list)
    for rating in found:
        by_agent[rating.agent_id].append(rating)
    evidence = {}
    for agent_id, agent_ratings in by_agent.items():
        successes = failures = 0.0
        newest_first = sorted(agent_ratings, key=lambda rating: rating.block, reverse=True)
        for newer, rating in enumerate(newest_first):  # newer: how many of its ratings are more recent
            weight = 1.0 if rating.reviewer.lower() in settings.trusted else settings.other_weight
            if settings.half_life:
                weight *= 0.5 ** (newer / settings.half_life)
            successes += weight * rating.value / 100
            failures += weight * (1 - rating.value / 100)
        evidence[agent_id] = (successes, failures)
    return evidence


def beliefs(peers: dict[str, dict], found: list[Rating], settings: TrustSettings) -> dict[str, tuple[float, float]]:
    """For each peer: the Beta distribution (alpha, beta) of its chance to answer correctly.

    It is the prior, plus the record of the owner's other peers (weighted by owner_weight), plus
    the peer's own evidence.
    """
    evidence = _evidence(found, settings)
    by_owner: dict[str, list[int]] = defaultdict(list)
    for peer in peers.values():
        by_owner[str(peer.get("owner") or "").lower()].append(peer["agent_id"])
    result = {}
    for name, peer in peers.items():
        alpha, beta = settings.prior
        owner = str(peer.get("owner") or "").lower()
        if settings.owner_weight and owner:
            for sibling in by_owner[owner]:
                if sibling != peer["agent_id"]:
                    sibling_successes, sibling_failures = evidence.get(sibling, (0.0, 0.0))
                    alpha += settings.owner_weight * sibling_successes
                    beta += settings.owner_weight * sibling_failures
        successes, failures = evidence.get(peer["agent_id"], (0.0, 0.0))
        result[name] = (alpha + successes, beta + failures)
    return result


def choose_trust_aware(belief: dict[str, tuple[float, float]], rng: random.Random, choice: str = "sample") -> str:
    """S3: the peer with the highest sample of its Beta distribution, or with choice="mean" the highest mean."""
    names = sorted(belief)
    if choice == "mean":
        mean = {name: alpha / (alpha + beta) for name, (alpha, beta) in belief.items()}
        best = max(mean.values())
        return rng.choice([name for name in names if mean[name] == best])
    samples = {name: rng.betavariate(*belief[name]) for name in names}
    return max(names, key=samples.__getitem__)


_s3_choices = 0  # how many S3 choices this process has made


def _draws(cfg: dict, capability: str, found: list[Rating]) -> random.Random:
    """The random generator for one S3 choice.

    Every Hermes session is a new process, and inside a session the ratings do not change until
    someone rates an answer. A generator seeded once per process would repeat the same numbers in
    every session, so a peer whose first sample was low would never be tried. One seeded only by
    the ratings would repeat them on every retry inside a session, so a failing peer would be
    chosen again and again. With a seed, the numbers therefore depend on the seed, the capability,
    the number of ratings and the number of S3 choices that this process made before: every choice
    gets new numbers, and a rerun on the same chain repeats them.
    """
    global _s3_choices
    _s3_choices += 1
    if cfg.get("seed") is None:
        return random_source(cfg)
    return random.Random(f"{cfg['seed']}:{capability}:{len(found)}:{_s3_choices}")


STRATEGIES = ("S2", "S3")


def pick(cfg: dict, peers: dict[str, dict], capability: str) -> tuple[str, str]:
    """The peer to delegate to, by the strategy of the config (S2 by default), and a short note on its rating."""
    strategy = str(cfg.get("strategy") or "S2").upper()
    agent_ids = [peer["agent_id"] for peer in peers.values()]
    if strategy == "S2":
        by_id = ratings(cfg, agent_ids, capability)
        rated = {name: by_id[peer["agent_id"]] for name, peer in peers.items()}
        name = choose(rated, random_source(cfg))
        count, average = rated[name]
        return name, (f"rated {average:g}/100 by {count} rating(s)" if count else "not rated yet")
    if strategy == "S3":
        settings = trust_settings(cfg)
        found = all_ratings(cfg, agent_ids, capability)
        belief = beliefs(peers, found, settings)
        name = choose_trust_aware(belief, _draws(cfg, capability, found), settings.choice)
        alpha, beta = belief[name]
        count = sum(rating.agent_id == peers[name]["agent_id"] for rating in found)
        return name, f"trust {alpha / (alpha + beta):.2f} from {count} rating(s)"
    raise ValueError(f"unknown strategy {strategy!r}; known: {', '.join(STRATEGIES)}")


def give_feedback(cfg: dict, agent_id: int, value: int, capability: str) -> str:
    """Sign and send a rating (0-100, no decimals) from this agent's wallet; return the transaction hash.

    The registry refuses a rating from the agent's own owner or operator.
    """
    account = _account(cfg)
    w3 = _web3(cfg)
    reputation = _contract(w3, cfg["reputation_registry"], _REPUTATION_ABI)
    call = reputation.functions.giveFeedback(agent_id, value, 0, FEEDBACK_TAG, capability, "", "", bytes(32))
    nonce = w3.eth.get_transaction_count(account.address, "pending")
    transaction = call.build_transaction({"from": account.address, "nonce": nonce})
    tx_hash = w3.eth.send_raw_transaction(account.sign_transaction(transaction).raw_transaction)
    if w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)["status"] != 1:
        raise RuntimeError(f"transaction {w3.to_hex(tx_hash)} reverted")
    return w3.to_hex(tx_hash)


def agent_id_from_name(agent: str) -> Optional[int]:
    """The agentId in a peer name such as "flights-honest-0#10582", or in a bare number."""
    tail = agent.strip().rsplit("#", 1)[-1]
    return int(tail) if tail.isdigit() else None
