from __future__ import annotations

from datetime import datetime, timezone
import logging

import discord

from repositories.casino_core import CasinoCoreRepository
from repositories.users import UsersRepository
from services.casino_core.settings import get_house_config
from utils import GuildSettingsRepository, get_database, get_security_manager, get_torn_api
from utils.database import get_pool


class CasinoDepositService:
    def __init__(self):
        self.repo = CasinoCoreRepository(get_pool())
        self.users = UsersRepository(get_pool())

    async def verify_and_credit(self, interaction: discord.Interaction, guild_id: int, discord_id: int) -> dict:
        settings = await GuildSettingsRepository(get_database()).get_or_create(guild_id)
        house = get_house_config(settings)
        if not settings.get("casino_enabled") or not house.get("house_discord_id") or not house.get("house_torn_id"):
            raise ValueError("Casino house settings are incomplete.")

        user_row = await self.users.get_user_api_key(discord_id)
        if not user_row or not user_row.get("encrypted_key"):
            raise ValueError("You must register API key first.")

        api_key = get_security_manager().decrypt_api_key(user_row["encrypted_key"])
        logs = await get_torn_api().get_item_send_receive_logs(
            api_key,
            limit=200,
            audit_discord_id=int(discord_id),
            audit_torn_id=int(user_row.get("torn_user_id") or 0) or None,
            audit_context="payment_verify_logs",
            audit_query_meta={"cat": 85, "limit": 200},
        )

        wallet = await self.repo.get_or_create_wallet(
            guild_id=guild_id,
            discord_id=discord_id,
            torn_user_id=int(user_row.get("torn_user_id") or 0),
            torn_name=user_row.get("torn_name"),
        )

        credited_total = 0
        credited_count = 0
        proof_payloads: list[dict] = []
        async with self.repo.acquire() as conn:
            async with conn.transaction():
                for log in logs:
                    data = log.get("data") or {}
                    if int(data.get("receiver") or 0) != int(house["house_torn_id"]):
                        continue
                    qty = sum(int(it.get("qty") or 0) for it in (data.get("items") or []) if int(it.get("id") or 0) == 206)
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
                        torn_log_ts=int(log.get("timestamp") or 0),
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
                            "torn_name": user_row.get("torn_name") or "Unknown",
                            "torn_user_id": int(user_row.get("torn_user_id") or 0),
                            "timestamp": int(log.get("timestamp") or int(datetime.now(tz=timezone.utc).timestamp())),
                        }
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
                logging.warning("Casino deposit credited but house DM failed for guild_id=%s discord_id=%s: %s", guild_id, discord_id, exc)

        return {"credited_total": credited_total, "count": credited_count, "new_balance": int(wallet.get("balance_tokens") or 0)}
