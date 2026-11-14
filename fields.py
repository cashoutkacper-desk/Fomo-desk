"""Exact JSON key paths found in the Step 2 raw samples under logs/raw/.

A path is listed only if that key existed in the saved file. None means the
field was not present in any of the five raw samples (never guessed).

Sources:
  pumpportal_migration.json  — first subscribeMigration event (not the ack)
  dexscreener_pair.json      — array of pairs; this sample has one element [0]
  rugcheck_report.json       — GET /v1/tokens/{mint}/report
  rugcheck_insiders.json     — GET /v1/tokens/{mint}/insiders/graph
  rpc_getaccountinfo.json    — JSON-RPC getAccountInfo, encoding jsonParsed

Notes that are facts about these files, not inferred schema:
  - PumpPortal `pool` in this sample is the string "pump-amm", not a pool address.
  - PumpPortal has no timestamp / migration-time key.
  - rugcheck_insiders.json is an empty JSON array (`[]`), so it has no keys.
  - Some RugCheck / RPC keys are present with JSON value null (authority fields,
    insiderNetworks). Those keys are listed; the value is not invented.
  - rugcheck_report.json `markets` has two entries in this sample:
    markets[0].marketType == "pump_fun_amm",
    markets[1].marketType == "meteora_damm_v2".
  - rpc_getaccountinfo.json extensions is a list; in this sample
    [0] is metadataPointer and [1] is tokenMetadata. Prefer matching on
    the `extension` field rather than a hard-coded index.
"""

SAMPLE_MINT = "4Ye6Y6Vaic8cyPdfW2f9PcLhXkysBtSmV84G8X7spump"

# field name -> list of {source, path, ...} or None if absent from all raw files
FIELD_MAP = {
    "mint": [
        {"source": "pumpportal_migration.json", "path": "mint"},
        {"source": "dexscreener_pair.json", "path": "[0].baseToken.address"},
        {"source": "rugcheck_report.json", "path": "mint"},
        {"source": "rugcheck_report.json", "path": "token_extensions.tokenMetadata.mint"},
        {"source": "rugcheck_report.json", "path": "markets[0].mintA"},
        {
            "source": "rpc_getaccountinfo.json",
            "path": "result.value.data.parsed.info.extensions[i].state.mint",
            "where": "extension == 'tokenMetadata'",
        },
    ],
    "name": [
        {"source": "dexscreener_pair.json", "path": "[0].baseToken.name"},
        {"source": "rugcheck_report.json", "path": "tokenMeta.name"},
        {"source": "rugcheck_report.json", "path": "token_extensions.tokenMetadata.name"},
        {"source": "rugcheck_report.json", "path": "fileMeta.name"},
        {
            "source": "rpc_getaccountinfo.json",
            "path": "result.value.data.parsed.info.extensions[i].state.name",
            "where": "extension == 'tokenMetadata'",
        },
    ],
    "symbol": [
        {"source": "dexscreener_pair.json", "path": "[0].baseToken.symbol"},
        {"source": "rugcheck_report.json", "path": "tokenMeta.symbol"},
        {"source": "rugcheck_report.json", "path": "token_extensions.tokenMetadata.symbol"},
        {"source": "rugcheck_report.json", "path": "fileMeta.symbol"},
        {
            "source": "rpc_getaccountinfo.json",
            "path": "result.value.data.parsed.info.extensions[i].state.symbol",
            "where": "extension == 'tokenMetadata'",
        },
    ],
    "signature": [
        {"source": "pumpportal_migration.json", "path": "signature"},
    ],
    "tx_type": [
        {"source": "pumpportal_migration.json", "path": "txType"},
    ],
    # Present, but in this sample the value is the DEX id "pump-amm", not an address.
    "pumpportal_pool": [
        {"source": "pumpportal_migration.json", "path": "pool"},
    ],
    "pool_address": [
        {"source": "dexscreener_pair.json", "path": "[0].pairAddress"},
        {"source": "rugcheck_report.json", "path": "markets[0].pubkey"},
        {"source": "rugcheck_report.json", "path": "markets[1].pubkey"},
    ],
    "dex_id": [
        {"source": "dexscreener_pair.json", "path": "[0].dexId"},
        {"source": "rugcheck_report.json", "path": "markets[0].marketType"},
        {"source": "rugcheck_report.json", "path": "markets[1].marketType"},
    ],
    "chain_id": [
        {"source": "dexscreener_pair.json", "path": "[0].chainId"},
    ],
    "quote_token_address": [
        {"source": "dexscreener_pair.json", "path": "[0].quoteToken.address"},
        {"source": "rugcheck_report.json", "path": "markets[0].mintB"},
    ],
    "migration_time": None,
    "pair_created": [
        {"source": "dexscreener_pair.json", "path": "[0].pairCreatedAt"},
    ],
    "price_usd": [
        {"source": "dexscreener_pair.json", "path": "[0].priceUsd"},
        {"source": "rugcheck_report.json", "path": "price"},
        {"source": "rugcheck_report.json", "path": "markets[0].lp.basePrice"},
    ],
    "price_native": [
        {"source": "dexscreener_pair.json", "path": "[0].priceNative"},
    ],
    "liquidity_usd": [
        {"source": "dexscreener_pair.json", "path": "[0].liquidity.usd"},
        {"source": "rugcheck_report.json", "path": "totalMarketLiquidity"},
        {"source": "rugcheck_report.json", "path": "markets[0].lp.quoteUSD"},
        {"source": "rugcheck_report.json", "path": "markets[0].lp.baseUSD"},
    ],
    "liquidity_base": [
        {"source": "dexscreener_pair.json", "path": "[0].liquidity.base"},
        {"source": "rugcheck_report.json", "path": "markets[0].lp.base"},
    ],
    "liquidity_quote": [
        {"source": "dexscreener_pair.json", "path": "[0].liquidity.quote"},
        {"source": "rugcheck_report.json", "path": "markets[0].lp.quote"},
    ],
    "fdv": [
        {"source": "dexscreener_pair.json", "path": "[0].fdv"},
    ],
    "mcap": [
        {"source": "dexscreener_pair.json", "path": "[0].marketCap"},
    ],
    "volume_m5": [
        {"source": "dexscreener_pair.json", "path": "[0].volume.m5"},
    ],
    "volume_h1": [
        {"source": "dexscreener_pair.json", "path": "[0].volume.h1"},
    ],
    "volume_h6": [
        {"source": "dexscreener_pair.json", "path": "[0].volume.h6"},
    ],
    "volume_h24": [
        {"source": "dexscreener_pair.json", "path": "[0].volume.h24"},
    ],
    "txns_m5_buys": [
        {"source": "dexscreener_pair.json", "path": "[0].txns.m5.buys"},
    ],
    "txns_m5_sells": [
        {"source": "dexscreener_pair.json", "path": "[0].txns.m5.sells"},
    ],
    "txns_h1_buys": [
        {"source": "dexscreener_pair.json", "path": "[0].txns.h1.buys"},
    ],
    "txns_h1_sells": [
        {"source": "dexscreener_pair.json", "path": "[0].txns.h1.sells"},
    ],
    "txns_h6_buys": [
        {"source": "dexscreener_pair.json", "path": "[0].txns.h6.buys"},
    ],
    "txns_h6_sells": [
        {"source": "dexscreener_pair.json", "path": "[0].txns.h6.sells"},
    ],
    "txns_h24_buys": [
        {"source": "dexscreener_pair.json", "path": "[0].txns.h24.buys"},
    ],
    "txns_h24_sells": [
        {"source": "dexscreener_pair.json", "path": "[0].txns.h24.sells"},
    ],
    "price_change_m5": [
        {"source": "dexscreener_pair.json", "path": "[0].priceChange.m5"},
    ],
    "price_change_h1": [
        {"source": "dexscreener_pair.json", "path": "[0].priceChange.h1"},
    ],
    "price_change_h6": [
        {"source": "dexscreener_pair.json", "path": "[0].priceChange.h6"},
    ],
    "price_change_h24": [
        {"source": "dexscreener_pair.json", "path": "[0].priceChange.h24"},
    ],
    "mint_authority": [
        {"source": "rugcheck_report.json", "path": "mintAuthority"},
        {"source": "rugcheck_report.json", "path": "token.mintAuthority"},
        {"source": "rpc_getaccountinfo.json", "path": "result.value.data.parsed.info.mintAuthority"},
    ],
    "freeze_authority": [
        {"source": "rugcheck_report.json", "path": "freezeAuthority"},
        {"source": "rugcheck_report.json", "path": "token.freezeAuthority"},
        {"source": "rpc_getaccountinfo.json", "path": "result.value.data.parsed.info.freezeAuthority"},
    ],
    "token_supply": [
        {"source": "rugcheck_report.json", "path": "token.supply"},
        {"source": "rpc_getaccountinfo.json", "path": "result.value.data.parsed.info.supply"},
    ],
    "decimals": [
        {"source": "rugcheck_report.json", "path": "token.decimals"},
        {"source": "rpc_getaccountinfo.json", "path": "result.value.data.parsed.info.decimals"},
    ],
    "token_program": [
        {"source": "rugcheck_report.json", "path": "tokenProgram"},
        {"source": "rpc_getaccountinfo.json", "path": "result.value.owner"},
        {"source": "rpc_getaccountinfo.json", "path": "result.value.data.program"},
    ],
    "holder_count": [
        {"source": "rugcheck_report.json", "path": "totalHolders"},
    ],
    "top_holders": [
        {"source": "rugcheck_report.json", "path": "topHolders"},
    ],
    "top_holders_address": [
        {"source": "rugcheck_report.json", "path": "topHolders[].address"},
    ],
    "top_holders_owner": [
        {"source": "rugcheck_report.json", "path": "topHolders[].owner"},
    ],
    "top_holders_amount": [
        {"source": "rugcheck_report.json", "path": "topHolders[].amount"},
    ],
    "top_holders_pct": [
        {"source": "rugcheck_report.json", "path": "topHolders[].pct"},
    ],
    "top_holders_insider": [
        {"source": "rugcheck_report.json", "path": "topHolders[].insider"},
    ],
    # rugcheck_insiders.json (Step 2) was []. A second raw sample of the same
    # endpoint, saved in Step 5 as rugcheck_insiders_nonempty.json, is a list of
    # networks. The spec's "edges" are the key `links` in that sample.
    "insiders_graph": [
        {"source": "rugcheck_insiders_nonempty.json", "path": "[]"},
    ],
    "insiders_graph_nodes": [
        {"source": "rugcheck_insiders_nonempty.json", "path": "[].nodes"},
    ],
    "insiders_graph_node_id": [
        {"source": "rugcheck_insiders_nonempty.json", "path": "[].nodes[].id"},
    ],
    "insiders_graph_node_holdings": [
        {"source": "rugcheck_insiders_nonempty.json", "path": "[].nodes[].holdings"},
    ],
    "insiders_graph_links": [
        {"source": "rugcheck_insiders_nonempty.json", "path": "[].links"},
    ],
    "insiders_graph_link_source": [
        {"source": "rugcheck_insiders_nonempty.json", "path": "[].links[].source"},
    ],
    "insiders_graph_link_target": [
        {"source": "rugcheck_insiders_nonempty.json", "path": "[].links[].target"},
    ],
    "insiders_graph_net_id": [
        {"source": "rugcheck_insiders_nonempty.json", "path": "[].net_id"},
    ],
    "insiders_graph_network_type": [
        {"source": "rugcheck_insiders_nonempty.json", "path": "[].network_type"},
    ],
    "graph_insiders_detected": [
        {"source": "rugcheck_report.json", "path": "graphInsidersDetected"},
    ],
    "insider_networks": [
        {"source": "rugcheck_report.json", "path": "insiderNetworks"},
    ],
    "lp_locked": [
        {"source": "rugcheck_report.json", "path": "markets[].lp.lpLocked"},
    ],
    "lp_unlocked": [
        {"source": "rugcheck_report.json", "path": "markets[].lp.lpUnlocked"},
    ],
    "lp_locked_pct": [
        {"source": "rugcheck_report.json", "path": "markets[].lp.lpLockedPct"},
    ],
    "lp_locked_usd": [
        {"source": "rugcheck_report.json", "path": "markets[].lp.lpLockedUSD"},
    ],
    "lp_current_supply": [
        {"source": "rugcheck_report.json", "path": "markets[].lp.lpCurrentSupply"},
    ],
    "lp_total_supply": [
        {"source": "rugcheck_report.json", "path": "markets[].lp.lpTotalSupply"},
    ],
    "lp_mint": [
        {"source": "rugcheck_report.json", "path": "markets[].mintLP"},
        {"source": "rugcheck_report.json", "path": "markets[].lp.lpMint"},
    ],
    "lockers": [
        {"source": "rugcheck_report.json", "path": "lockers"},
    ],
    "locker_owners": [
        {"source": "rugcheck_report.json", "path": "lockerOwners"},
    ],
    "locker_scan_status": [
        {"source": "rugcheck_report.json", "path": "lockerScanStatus"},
    ],
    "total_lp_providers": [
        {"source": "rugcheck_report.json", "path": "totalLPProviders"},
    ],
    "total_market_liquidity": [
        {"source": "rugcheck_report.json", "path": "totalMarketLiquidity"},
    ],
    "total_stable_liquidity": [
        {"source": "rugcheck_report.json", "path": "totalStableLiquidity"},
    ],
    "rugcheck_score": [
        {"source": "rugcheck_report.json", "path": "score"},
    ],
    "rugcheck_score_normalised": [
        {"source": "rugcheck_report.json", "path": "score_normalised"},
    ],
    "rugcheck_risks": [
        {"source": "rugcheck_report.json", "path": "risks"},
    ],
    "rugged": [
        {"source": "rugcheck_report.json", "path": "rugged"},
    ],
    "token_type": [
        {"source": "rugcheck_report.json", "path": "tokenType"},
    ],
    "token_extensions": [
        {"source": "rugcheck_report.json", "path": "token_extensions"},
        {"source": "rpc_getaccountinfo.json", "path": "result.value.data.parsed.info.extensions"},
    ],
    "transfer_fee_pct": [
        {"source": "rugcheck_report.json", "path": "transferFee.pct"},
    ],
    "transfer_fee_max_amount": [
        {"source": "rugcheck_report.json", "path": "transferFee.maxAmount"},
    ],
    "transfer_fee_authority": [
        {"source": "rugcheck_report.json", "path": "transferFee.authority"},
    ],
    "creator": [
        {"source": "rugcheck_report.json", "path": "creator"},
    ],
    "creator_balance": [
        {"source": "rugcheck_report.json", "path": "creatorBalance"},
    ],
    "creator_tokens": [
        {"source": "rugcheck_report.json", "path": "creatorTokens"},
    ],
    "launchpad_name": [
        {"source": "rugcheck_report.json", "path": "launchpad.name"},
    ],
    "launchpad_platform": [
        {"source": "rugcheck_report.json", "path": "launchpad.platform"},
    ],
    "deploy_platform": [
        {"source": "rugcheck_report.json", "path": "deployPlatform"},
    ],
    "detected_at": [
        {"source": "rugcheck_report.json", "path": "detectedAt"},
    ],
    "metadata_uri": [
        {"source": "rugcheck_report.json", "path": "tokenMeta.uri"},
        {"source": "rugcheck_report.json", "path": "token_extensions.tokenMetadata.uri"},
        {
            "source": "rpc_getaccountinfo.json",
            "path": "result.value.data.parsed.info.extensions[i].state.uri",
            "where": "extension == 'tokenMetadata'",
        },
    ],
    "metadata_mutable": [
        {"source": "rugcheck_report.json", "path": "tokenMeta.mutable"},
    ],
    "metadata_update_authority": [
        {"source": "rugcheck_report.json", "path": "tokenMeta.updateAuthority"},
        {
            "source": "rpc_getaccountinfo.json",
            "path": "result.value.data.parsed.info.extensions[i].state.updateAuthority",
            "where": "extension == 'tokenMetadata'",
        },
    ],
    "known_accounts": [
        {"source": "rugcheck_report.json", "path": "knownAccounts"},
    ],
    "verification": [
        {"source": "rugcheck_report.json", "path": "verification"},
    ],
    "validation_validated": [
        {"source": "rugcheck_report.json", "path": "validation.validated"},
    ],
    "events": [
        {"source": "rugcheck_report.json", "path": "events"},
    ],
    "markets": [
        {"source": "rugcheck_report.json", "path": "markets"},
    ],
}

# Convenience: flatten to a single primary path (first found) or None.
FIELDS = {
    name: (paths[0] if isinstance(paths, list) and paths else None)
    for name, paths in FIELD_MAP.items()
}


# ---------------------------------------------------------------------------
# Resolver used by the agents. Agents never hard-code a raw key: they ask for
# a field by name + source file, and the path comes from FIELD_MAP above.
# ---------------------------------------------------------------------------
import re as _re

_TOKEN = _re.compile(r"([^.\[\]]+)|\[(\d*|i)\]")


def path_for(field: str, source: str) -> str | None:
    entries = FIELD_MAP.get(field)
    if not entries:
        return None
    for entry in entries:
        if entry["source"] == source:
            return entry["path"]
    return None


def resolve(obj, path: str):
    """Walk a FIELD_MAP path. `[n]` indexes, `[]` maps over a list, a missing
    key gives None. `[i]` (match-by-field lists) is not supported here."""
    if path is None:
        return None
    tokens = [(m.group(1), m.group(2)) for m in _TOKEN.finditer(path)]

    def walk(cur, i):
        if i == len(tokens):
            return cur
        key, idx = tokens[i]
        if cur is None:
            return None
        if key is not None:
            if not isinstance(cur, dict) or key not in cur:
                return None
            return walk(cur[key], i + 1)
        if idx == "":
            if not isinstance(cur, list):
                return None
            return [walk(x, i + 1) for x in cur]
        if idx == "i":
            raise ValueError("[i] paths need a match rule; resolve manually")
        n = int(idx)
        if not isinstance(cur, list) or n >= len(cur):
            return None
        return walk(cur[n], i + 1)

    return walk(obj, 0)


def get(obj, field: str, source: str, element: bool = False):
    """Read `field` from a raw object of `source`.

    element=True: `obj` is ONE element of a top-level array (a DexScreener
    pair from the /tokens/v1 array), so a leading `[0].` is dropped.
    """
    path = path_for(field, source)
    if path is None:
        return None
    if element and path.startswith("[0]."):
        path = path[4:]
    return resolve(obj, path)


def subpath(field: str, source: str, list_key: str) -> str | None:
    """For a path like `markets[].lp.lpLockedPct`, return `lp.lpLockedPct`."""
    path = path_for(field, source)
    prefix = f"{list_key}[]."
    if path and path.startswith(prefix):
        return path[len(prefix):]
    return None
