"""
zap_tokens.py — USDT/USDC deposit & withdraw for Zap_lnBot
via Breez SDK Spark cross-chain routes (Orchestra/Flashnet provider).

Docs: sdk-doc-spark.breez.technology/guide/cross_chain.html
- Deposit: user sends real USDT/USDT (EVM/Solana/Tron) TO a provider deposit
  address; the SDK's background receive-monitor lands it on Spark (BTC sats
  or USDB per route's SparkAsset destination). receive_payment() with
  ReceivePaymentMethod.CROSS_CHAIN returns the deposit address.
- Withdraw: prepare_send_payment with PaymentRequest.INPUT(cross-chain
  address), amount in the EXTERNAL asset base units, token_identifier of the
  Spark-side funding token (USDB or None→BTC) — then send_payment.
- Routes: sdk.get_cross_chain_routes(CrossChainRouteFilter()) → list of
  CrossChainRoutePair (provider, chain, asset, accepted_assets w/ limits).

All functions are wallet-agnostic: they take the sdk instance + user_id.
"""
import logging
import time

import breez_sdk_spark as bss

# ---------------------------------------------------------------------------
# Token identifiers (mainnet constants — verified from official docs:
# config.html stable-balance section documents the USDB btkn1… identifier)
USDB_TOKEN_ID = "btkn1xgrvjwey5ngcagvap2dzzvsy4uk8ua9x69k82dwvt5e7ef9drm9qztux87"

# Friendly chain names
_CHAIN_FA = {
    "tron": "ترون (TRC20)",
    "solana": "سولانا",
    "base": "بیس",
    "arbitrum one": "آربیتروم",
    "ethereum": "اتریوم",
}


# ---------------------------------------------------------------------------
# Routes

async def get_routes(sdk, direction="receive", contract_address=None, address_details=None):
    """Cross-chain routes, refreshed each call (cheap RPC).

    direction: "receive" → CrossChainRouteFilter.RECEIVE(contract_address)
               "send"    → CrossChainRouteFilter.SEND(address_details)
    (uniffi enum: always construct a VARIANT, never the parent class)
    NOTE: every BreezSdk method is async in the python binding (0.26.1).
    """
    if direction == "send":
        flt = bss.CrossChainRouteFilter.SEND(address_details=address_details)
    else:
        flt = bss.CrossChainRouteFilter.RECEIVE(contract_address=contract_address)
    return await sdk.get_cross_chain_routes(filter=flt)


def pick_routes(routes, asset_symbol: str):
    """Routes serving a given external asset (USDT/USDC), grouped by chain.

    Returns [{pair, chain, asset, decimals}] — first entry per chain is the
    provider Breez offers for it. Only mainnet routes exist (per docs).
    """
    out = {}
    for r in routes or []:
        try:
            asset = (getattr(r, "asset", "") or "").upper()
            if asset != asset_symbol.upper():
                continue
            chain = getattr(r, "chain", "") or "?"
            if chain.lower() == "unknown":
                continue
            if chain not in out:
                out[chain] = {
                    "pair": r,
                    "chain": chain,
                    "asset": asset,
                    "decimals": int(getattr(r, "decimals", 6) or 6),
                }
        except Exception as e:
            logging.debug(f"route skip: {e}")
    return list(out.values())


def route_limits(pair, spark_asset):
    """Published (min_amount, max_amount, min_usd_cents, max_usd_cents) or None."""
    try:
        lim = pair.limits_for(spark_asset)
        if lim is None:
            return None
        return {
            "min_amount": getattr(lim, "min_amount", None),
            "max_amount": getattr(lim, "max_amount", None),
            "min_usd_cents": getattr(lim, "min_usd_cents", None),
            "max_usd_cents": getattr(lim, "max_usd_cents", None),
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Balances

RECEIVE_MIN_USDT = 3.0     # observed live: 2 rejected, 3 accepted (TRON/Orchestra)
SEND_MIN_USD = 1.0         # token-funded sends: practical floor (0.5% fee still applies)


async def route_bounds(sdk, direction, asset_symbol="USDT", chain=None, address_details=None):
    """Live min/max (usd, sats) for a route. Falls back to observed constants.

    Returns {"min_usd": float|None, "max_usd": float|None,
             "min_sats": int|None, "max_sats": int|None}
    """
    usd = {"min_usd": None, "max_usd": None}
    try:
        routes = pick_routes(await get_routes(sdk, direction=direction,
                                              address_details=address_details), asset_symbol)
        pair = next((r["pair"] for r in routes
                     if not chain or chain.lower() in r["chain"].lower()), routes[0]["pair"])
        for sa in (bss.SparkAsset.BITCOIN(),
                   bss.SparkAsset.TOKEN(token_identifier=USDB_TOKEN_ID)):
            lims = route_limits(pair, sa)
            if lims:
                if lims.get("min_usd_cents") is not None:
                    usd["min_usd"] = max(usd["min_usd"] or 0, lims["min_usd_cents"] / 100)
                if lims.get("max_usd_cents") is not None:
                    cur = usd["max_usd"]
                    usd["max_usd"] = lims["max_usd_cents"] / 100 if cur is None else min(cur, lims["max_usd_cents"] / 100)
    except Exception:
        pass
    if usd["min_usd"] is None and direction == "receive":
        usd["min_usd"] = RECEIVE_MIN_USDT
    if usd["min_usd"] is None and direction == "send":
        usd["min_usd"] = SEND_MIN_USD
    usd["min_sats"] = None
    usd["max_sats"] = None
    return usd


async def token_balances(sdk, ensure_synced=False):
    """{ticker: (human_amount, token_metadata)} for every token in the wallet."""
    info = await sdk.get_info(request=bss.GetInfoRequest(ensure_synced=ensure_synced))
    out = {}
    for tid, tb in (getattr(info, "token_balances", None) or {}).items():
        md = getattr(tb, "token_metadata", None)
        if md is None:
            continue
        decimals = int(getattr(md, "decimals", 0) or 0)
        raw = int(getattr(tb, "balance", 0) or 0)
        human = raw / (10 ** decimals) if decimals else float(raw)
        out[(getattr(md, "ticker", "") or "?").upper()] = (human, md)
    return out


async def usdb_raw_balance(sdk) -> int:
    """USDB balance in base units (6 decimals)."""
    info = await sdk.get_info(request=bss.GetInfoRequest(ensure_synced=False))
    tb = (getattr(info, "token_balances", None) or {}).get(USDB_TOKEN_ID)
    return int(getattr(tb, "balance", 0) or 0) if tb is not None else 0


async def usdb_human(sdk) -> float:
    return await usdb_raw_balance(sdk) / 1_000_000


# ---------------------------------------------------------------------------
# Deposit (external USDT/USDC → Spark wallet)

async def make_deposit(sdk, asset_symbol: str, chain: str, amount_external_base: int):
    """Create a cross-chain receive: returns (deposit_address, fee_info, route).

    amount_external_base is in the EXTERNAL asset's base units
    (USDT-TRON/Solana: 6 decimals → 10 USDT = 10_000_000).
    """
    routes = pick_routes(await get_routes(sdk), asset_symbol)
    if not routes:
        raise RuntimeError("no_routes")
    entry = next((r for r in routes if r["chain"].lower() == chain.lower()),
                 routes[0])
    pair = entry["pair"]

    req = bss.ReceivePaymentRequest(
        payment_method=bss.ReceivePaymentMethod.CROSS_CHAIN(
            route=pair,
            amount=amount_external_base,          # u128 as int
            destination=bss.SparkAsset.BITCOIN(),  # land as sats (simplest for users)
            fee_mode=bss.CrossChainFeeMode.FEES_INCLUDED,
            max_slippage_bps=None,
            target_overpay_bps=None,
        )
    )
    resp = await sdk.receive_payment(request=req)
    return (getattr(resp, "payment_request", "") or "",
            getattr(resp, "fee", 0),
            entry)


# ---------------------------------------------------------------------------
# Withdraw (Spark USDB/sats → external USDT/USDC address)

async def parse_external_address(sdk, text: str):
    """Parse a pasted destination; returns dict for cross-chain addrs else None.

    Accepts bare 0x…/base58/T… addresses and EIP-681 / Solana-Pay / Tron URIs.
    The variant's payload rides in `cross_chain_address` (python binding
    exposes variant data via keyword attrs) — fall back to shape detection.
    """
    try:
        parsed = await sdk.parse(input=text.strip())
    except Exception:
        return None
    if not isinstance(parsed, bss.InputType.CROSS_CHAIN_ADDRESS):
        return None
    # python binding stores variant fields in `_values` (verified on 0.26.1)
    details = None
    vals = getattr(parsed, "_values", None)
    if vals:
        details = vals[0]
    if details is None:
        for attr in ("cross_chain_address", "input", "details"):
            d = getattr(parsed, attr, None)
            if d is not None and hasattr(d, "address"):
                details = d
                break
    addr = getattr(details, "address", None) if details else None
    if not addr:
        # Shape fallback on the raw text (bare addresses parse fine anyway)
        a = text.strip()
        low = a.lower()
        if low.startswith("0x") and len(a) == 42:
            addr, fam = a, "evm"
        elif a.startswith("T") and 26 <= len(a) <= 40:
            addr, fam = a, "tron"
        elif low.startswith(("ethereum:", "solana:", "tron:")):
            # URI — take the address part before '?' or path
            raw = a.split(":", 1)[1]
            raw = raw.split("?")[0].split("/")[0]
            addr, fam = raw, "tron" if raw.startswith("T") else ("evm" if raw.lower().startswith("0x") else "solana")
        else:
            return None
        return {"address": addr, "family": fam, "contract": None, "chain_id": None}
    family = getattr(details, "address_family", None)
    fam = ""
    for name in ("EVM", "SOLANA", "TRON"):
        if family == getattr(bss.CrossChainAddressFamily, name, None):
            fam = name.lower()
            break
    return {
        "address": addr,
        "family": fam or "evm",
        "contract": getattr(details, "contract_address", None),
        "chain_id": getattr(details, "chain_id", None),
    }


async def convert_sats_to_usdb(sdk, usdb_base: int):
    """Convert sats to USDB via a self-address spark send funded from bitcoin.

    The SDK's FROM_BITCOIN conversion is not supported on cross-chain sends,
    but IS supported on plain SPARK_ADDRESS sends — so we send the USDB to our
    own spark address, funding it from sats. Blocks until the payment completes.
    """
    resp = await sdk.receive_payment(request=bss.ReceivePaymentRequest(
        payment_method=bss.ReceivePaymentMethod.SPARK_ADDRESS()))
    my_addr = resp.payment_request
    req = bss.PrepareSendPaymentRequest(
        payment_request=bss.PaymentRequest.INPUT(input=my_addr),
        amount=usdb_base,
        token_identifier=USDB_TOKEN_ID,
        conversion_options=bss.ConversionOptions(
            conversion_type=bss.ConversionType.FROM_BITCOIN()),
        fee_policy=None,
    )
    prep = await sdk.prepare_send_payment(request=req)
    pay_resp = await sdk.send_payment(
        request=bss.SendPaymentRequest(prepare_response=prep, options=None))
    payment = getattr(pay_resp, "payment", None)
    status = str(getattr(payment, "status", ""))
    # wait briefly for the self-credit to land
    for _ in range(20):
        if await usdb_raw_balance(sdk) >= usdb_base * 0.98:
            return payment
        await asyncio.sleep(1)
    return payment


async def prepare_withdraw(sdk, asset_symbol: str, chain: str, dest_address: str,
                           amount_external_base: int, address_details=None):
    """Prepare an external USDT/USDC send. Returns (prepare_response, entry).

    Uses PaymentRequest.CROSS_CHAIN {address, route} per the docs — the SDK
    quotes provider fees and auto-converts the Spark-side funding asset.
    Funding priority: USDB token on Spark — falls back to BTC sats when the
    route accepts it or the wallet holds no USDB. Amount is in the EXTERNAL
    asset base units.
    """
    routes = pick_routes(await get_routes(sdk, direction="send", address_details=address_details),
                         asset_symbol)
    if not routes:
        raise RuntimeError("no_routes")
    fam_chain = chain.lower()
    entry = next((r for r in routes if fam_chain in r["chain"].lower()
                  or r["chain"].lower() in fam_chain), routes[0])
    pair = entry["pair"]

    # Funding asset: USDB if this route accepts it, else BTC sats
    spark_asset = bss.SparkAsset.TOKEN(token_identifier=USDB_TOKEN_ID)
    try:
        accepted = getattr(pair, "accepted_assets", None) or []
        tokens_ok = any(
            str(getattr(aa, "asset", "")).startswith("SparkAsset.TOKEN")
            for aa in accepted)
        if not tokens_ok:
            spark_asset = bss.SparkAsset.BITCOIN()
    except Exception:
        spark_asset = bss.SparkAsset.BITCOIN()

    token_identifier = USDB_TOKEN_ID if spark_asset.is_TOKEN() else None
    if token_identifier and await usdb_raw_balance(sdk) <= 0:
        # Nothing in USDB — self-convert sats→USDB first (FROM_BITCOIN conversion
        # is NOT supported on cross-chain sends, but IS on plain spark self-sends)
        await convert_sats_to_usdb(sdk, amount_external_base)

    req = bss.PrepareSendPaymentRequest(
        payment_request=bss.PaymentRequest.CROSS_CHAIN(
            address=dest_address,
            route=pair,
            max_slippage_bps=None,
            target_overpay_bps=None,
        ),
        amount=amount_external_base,
        token_identifier=token_identifier,
        conversion_options=None,
        fee_policy=None,
    )
    try:
        prep = await sdk.prepare_send_payment(request=req)
    except Exception as first_err:
        # TOKEN funding can still be short: the quote's amount_in includes the
        # network fee. Top up USDB from sats and retry ONCE.
        if token_identifier and "insufficient" in str(first_err).lower():
            bal = await usdb_raw_balance(sdk)
            # conservative top-up: the requested amount + 15% for fees/slippage
            need = amount_external_base + int(amount_external_base * 0.15) - bal
            if need > 0:
                await convert_sats_to_usdb(sdk, need)
            prep = await sdk.prepare_send_payment(request=req)
        else:
            raise
    # EXECUTE-side shortfall data for execute_withdraw's retry:
    # the quote's amount_in (amount + network fee) may exceed the USDB balance at
    # send time → convert the shortfall from sats, re-prepare, retry ONCE there.
    pm = getattr(prep, "payment_method", None)
    amount_in_needed = int(getattr(pm, "amount_in", 0) or 0)
    return prep, entry, spark_asset, amount_in_needed, req


async def execute_withdraw(sdk, prep, amount_in_needed=0, req=None):
    """Send a prepared cross-chain withdrawal. Returns the Payment.

    On 'insufficient funds' at send time (quote amount_in > wallet USDB),
    converts the shortfall from sats, re-prepares and retries ONCE.
    """
    try:
        resp = await sdk.send_payment(
            request=bss.SendPaymentRequest(prepare_response=prep, options=None))
        return getattr(resp, "payment", None)
    except Exception as e:
        if "insufficient" not in str(e).lower() or req is None:
            raise
        bal = await usdb_raw_balance(sdk)
        need = max(amount_in_needed, 0) - bal + int(max(amount_in_needed, 1) * 0.05)
        if need > 0:
            await convert_sats_to_usdb(sdk, need)
        prep = await sdk.prepare_send_payment(request=req)
        resp = await sdk.send_payment(
            request=bss.SendPaymentRequest(prepare_response=prep, options=None))
        return getattr(resp, "payment", None)


def fmt_amount(human: float) -> str:
    s = f"{human:,.2f}".rstrip("0").rstrip(".")
    return s or "0"


def clean_deposit_address(raw: str) -> str:
    """Extract the bare destination address from a payment URI.

    Orchestra returns EIP-681 / Solana-Pay / Tron URIs as the deposit
    'address' for token deposits (the deposit is an ERC-20/SPL/TRC-20
    transfer). Users need the bare recipient address in the text; the QR
    still encodes the full URI so compatible wallets auto-fill everything.
    EIP-681:  ethereum:<contract>@<chainId>/transfer?address=<dest>&uint256=…
    Solana:   solana:<dest>?amount=…&spl-token=<mint>
    Tron:     tron:<dest>?amount=…&token=<contract>
    Bare addresses pass through untouched.
    """
    s = (raw or "").strip()
    low = s.lower()
    if low.startswith("ethereum:"):
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(s).query)
        if "address" in q:
            return q["address"][0]
        # native-send form: ethereum:<dest>@<id>?value=…
        seg = s[len("ethereum:"):].split("?", 1)[0].split("@")[0].split("/")[0]
        if seg.lower().startswith("0x"):
            return seg
    if low.startswith("solana:"):
        dest = s[len("solana:"):].split("?", 1)[0].split("/")[0]
        if dest:
            return dest
    if low.startswith("tron:"):
        dest = s[len("tron:"):].split("?", 1)[0].split("/")[0]
        if dest:
            return dest
    return s


def parse_human_amount(text: str):
    """'10' / '10.5' / '10,5' → float or None."""
    t = (text or "").strip().replace(",", ".").replace("٫", ".")
    try:
        v = float(t)
        return v if v > 0 else None
    except ValueError:
        return None


def to_base_units(human: float, decimals: int) -> int:
    return int(round(human * (10 ** decimals)))


def from_base_units(raw: int, decimals: int) -> float:
    return raw / (10 ** decimals) if decimals else float(raw)
