from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any

import config
import discord

from repositories.casino_core import CasinoCoreRepository
from repositories.users import UsersRepository
from services.casino_core.settings import get_house_config
from services.torn_identity import resolve_casino_deposit_identity
from utils import GuildSettingsRepository, get_database, get_security_manager, get_torn_api
from utils.database import get_pool


logger = logging.getLogger(__name__)


def _to_int(value: object) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _torn_extract_sender_id(entry: dict) -> int:
    data = entry.get("data") or {}
    return _to_int(
        data.get("sender")
        or data.get("sender_id")
        or data.get("from")
        or data.get("from_id")
        or entry.get("sender")
        or entry.get("sender_id")
        or 0
    )


def _torn_extract_receiver_id(entry: dict) -> int:
    data = entry.get("data") or {}
    return _to_int(
        data.get("receiver")
        or data.get("receiver_id")
        or data.get("to")
        or data.get("to_id")
        or entry.get("receiver")
        or entry.get("receiver_id")
        or 0
    )


def _torn_extract_item_qty(entry: dict, item_id: int) -> int:
    data = entry.get("data") or {}
    items = data.get("items") or data.get("item_list") or entry.get("items") or []
    total = 0
    if isinstance(items, list):
        for it in items:
            try:
                if int(it.get("id") or it.get("item_id") or 0) == int(item_id):
                    total += int(it.get("qty") or it.get("quantity") or it.get("amount") or 0)
            except Exception:
                continue
    elif isinstance(items, dict):
        for k, v in items.items():
            try:
                if int(k) == int(item_id):
                    total += int(v)
            except Exception:
                continue
    return total


class CasinoDepositService:
    def __init__(self):
        self.repo = CasinoCoreRepository(get_pool())
        self.users = UsersRepository(get_pool())

    def _find_matching_house_log(
        self,
        *,
        logs: list[dict[str, Any]],
        sender_torn_id: int,
        house_torn_id: int,
        required_item_id: int,
        required_qty: int,
        since_ts: int,
        until_ts: int,
    ) -> dict[str, Any] | None:
        for entry in logs:
            ts = _to_int(entry.get("timestamp"))
            if ts < since_ts or ts > until_ts:
                continue

            sender = _torn_extract_sender_id(entry)
            if sender != int(sender_torn_id):
                continue

            receiver = _torn_extract_receiver_id(entry)
            if receiver not in (0, int(house_torn_id)):
                continue

            qty = _torn_extract_item_qty(entry, required_item_id)
            if qty >= int(required_qty):
                return entry
        return None

    async def verify_and_credit(self, interaction: discord.Interaction, guild_id: int, discord_id: int) -> dict:
        settings = await GuildSettingsRepository(get_database()).get_or_create(guild_id)
        house = get_house_config(settings)
        if not settings.get("casino_enabled") or not house.get("house_discord_id") or not house.get("house_torn_id"):
            raise ValueError("Casino house settings are incomplete.")

        house_row = await self.users.get_user_api_key(int(house["house_discord_id"]))
        if not house_row or not house_row.get("encrypted_key"):
            raise ValueError("Casino house API key is not configured.")

        if not interaction.guild:
            raise ValueError("Casino deposits can only be verified in a server.")

        identity, identity_error = await resolve_casino_deposit_identity(
            guild=interaction.guild,
            buyer_discord_id=int(discord_id),
            db=get_database(),
        )
        if not identity:
            raise ValueError(identity_error or "Could not resolve your Torn identity.")

        house_api_key = get_security_manager().decrypt_api_key(house_row["encrypted_key"])
        house_torn_id = int(house["house_torn_id"])
        required_item_id = int(config.XANAX_ITEM_ID)

        now_utc = datetime.now(timezone.utc)
        since_ts = int((now_utc - timedelta(days=3)).timestamp())
        until_ts = int((now_utc + timedelta(minutes=2)).timestamp())

        logs = await get_torn_api().get_item_send_receive_logs(
            house_api_key,
            limit=200,
            audit_discord_id=int(house["house_discord_id"]),
            audit_torn_id=house_torn_id,
            audit_context="casino_deposit_verify_house_logs",
            audit_query_meta={"cat": 85, "limit": 200},
        )

        wallet = await self.repo.get_or_create_wallet(
            guild_id=guild_id,
            discord_id=discord_id,
            torn_user_id=int(identity.torn_user_id),
            torn_name=identity.torn_name,
        )

        credited_total = 0
        credited_count = 0
        proof_payloads: list[dict] = []
        async with self.repo.acquire() as conn:
            async with conn.transaction():
                pending_logs = sorted(logs, key=lambda x: _to_int(x.get("timestamp")))
                for log in pending_logs:
                    ts = _to_int(log.get("timestamp"))
                    if ts < since_ts or ts > until_ts:
                        continue
                    sender = _torn_extract_sender_id(log)
                    if sender != int(identity.torn_user_id):
                        continue
                    receiver = _torn_extract_receiver_id(log)
                    if receiver not in (0, house_torn_id):
                        continue
                    qty = _torn_extract_item_qty(log, required_item_id)
                    if qty <= 0:
                        continue

                    log_id = str(log.get("id") or log.get("log_id") or log.get("log") or "")
                    if not log_id:
                        continue
                    deposit_id = await self.repo.insert_deposit_if_new(
                        conn,
                        guild_id=guild_id,
                        wallet_id=int(wallet["id"]),
                        torn_log_id=log_id,
                        torn_log_ts=ts,
                        qty_xanax=qty,
                        raw_log=log,
                    )
                    if not deposit_id:
                        continue
                    wallet = await self.repo.apply_ledger_entry_atomic(
                        conn,
                        guild_id=guild_id,
                        wallet_id=int(wallet["id"]),
                        entry_type="deposit_credit",
                        amount_tokens=qty,
                        idempotency_key=f"deposit:{deposit_id}:credit",
                        ref_type="casino_deposits",
                        ref_id=deposit_id,
                        metadata={"torn_log_id": log_id},
                    )
                    await self.repo.append_house_ledger(
                        conn,
                        guild_id=guild_id,
                        entry_type="deposit_in",
                        amount_tokens=qty,
                        ref_type="casino_deposits",
                        ref_id=deposit_id,
                        metadata={"discord_id": discord_id},
                    )
                    credited_total += qty
                    credited_count += 1

                    proof_payloads.append(
                        {
                            "qty": qty,
                            "log_id": log_id,
                            "torn_name": identity.torn_name or "Unknown",
                            "torn_user_id": int(identity.torn_user_id or 0),
                            "timestamp": ts or int(datetime.now(tz=timezone.utc).timestamp()),
                        }
                    )

        if credited_total <= 0:
            _ = self._find_matching_house_log(
                logs=logs,
                sender_torn_id=int(identity.torn_user_id),
                house_torn_id=house_torn_id,
                required_item_id=required_item_id,
                required_qty=1,
                since_ts=since_ts,
                until_ts=until_ts,
            )
            logger.info(
                "casino.deposit.verify.no_match guild_id=%s discord_user_id=%s buyer_torn_id=%s house_torn_id=%s required_item_id=%s required_qty=%s since_ts=%s until_ts=%s logs_scanned=%s identity_source=%s",
                guild_id,
                discord_id,
                int(identity.torn_user_id),
                house_torn_id,
                required_item_id,
                1,
                since_ts,
                until_ts,
                len(logs),
                identity.source,
            )
            raise ValueError(
                "Deposit not found in house logs yet. Make sure you sent the correct item to the house, then try again."
            )

        if proof_payloads:
            try:
                house_user = interaction.client.get_user(int(house["house_discord_id"]))
                if house_user is None:
                    house_user = await interaction.client.fetch_user(int(house["house_discord_id"]))
                if house_user:
                    for payload in proof_payloads:
                        em = discord.Embed(title="Casino Deposit Credited", color=discord.Color.green())
                        em.description = f"Player: <@{discord_id}>\nQty: **{payload['qty']}**\nLog: `{payload['log_id']}`"
                        em.add_field(name="Player Torn", value=f"{payload['torn_name']} ({payload['torn_user_id']})", inline=False)
                        em.add_field(name="Timestamp", value=f"<t:{payload['timestamp']}:f>", inline=False)
                        await house_user.send(embed=em)
            except Exception as exc:
                logger.warning("Casino deposit credited but house DM failed for guild_id=%s discord_id=%s: %s", guild_id, discord_id, exc)

        return {"credited_total": credited_total, "count": credited_count, "new_balance": int(wallet.get("balance_tokens") or 0)}
