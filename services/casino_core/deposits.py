from __future__ import annotations

from datetime import datetime, timezone

import discord

from repositories.casino_core import CasinoCoreRepository
from repositories.users import UsersRepository
from services.casino_core.ledger import trunc_json
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
        logs = await get_torn_api().get_item_send_receive_logs(api_key, limit=200)

        wallet = await self.repo.get_or_create_wallet(
            guild_id=guild_id,
            discord_id=discord_id,
            torn_user_id=int(user_row.get("torn_user_id") or 0),
            torn_name=user_row.get("torn_name"),
        )

        credited_total = 0
        credited_count = 0
        async with self.repo.acquire() as conn:
            async with conn.transaction():
                for log in logs:
                    data = log.get("data") or {}
                    details = log.get("details") or {}
                    if int(details.get("id") or 0) != 4102:
                        continue
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

                    house_user = interaction.client.get_user(int(house["house_discord_id"])) or await interaction.client.fetch_user(int(house["house_discord_id"]))
                    if house_user:
                        em = discord.Embed(title="Casino Deposit Credited", color=discord.Color.green())
                        em.description = f"Player: <@{discord_id}>\nQty: **{qty}**\nLog: `{log_id}`"
                        em.add_field(name="Player Torn", value=f"{user_row.get('torn_name') or 'Unknown'} ({int(user_row.get('torn_user_id') or 0)})", inline=False)
                        em.add_field(name="Timestamp", value=f"<t:{int(log.get('timestamp') or int(datetime.now(tz=timezone.utc).timestamp()))}:f>", inline=False)
                        em.add_field(name="Log Excerpt", value=f"```json\n{trunc_json(log, 300)}\n```", inline=False)
                        await house_user.send(embed=em)

        return {"credited_total": credited_total, "count": credited_count, "new_balance": int(wallet.get("balance_tokens") or 0)}
