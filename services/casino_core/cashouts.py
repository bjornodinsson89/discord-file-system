from __future__ import annotations

from datetime import timezone
import logging

import discord

from repositories.casino_core import CasinoCoreRepository
from repositories.users import UsersRepository
from services.casino_core.ledger import trunc_json
from services.casino_core.settings import get_house_config
from utils import GuildSettingsRepository, get_database, get_security_manager, get_torn_api
from utils.database import get_pool


class CasinoCashoutService:
    def __init__(self):
        self.repo = CasinoCoreRepository(get_pool())
        self.users = UsersRepository(get_pool())

    async def request_cashout(self, interaction: discord.Interaction, guild_id: int, discord_id: int, qty_tokens: int, note: str | None) -> int:
        settings = await GuildSettingsRepository(get_database()).get_or_create(guild_id)
        house = get_house_config(settings)
        required = ("house_discord_id", "house_torn_id", "payouts_channel_id")
        if not settings.get("casino_enabled") or any(not house.get(k) for k in required):
            raise ValueError("Casino house settings incomplete.")

        user_row = await self.users.get_user_api_key(discord_id)
        wallet = await self.repo.get_or_create_wallet(
            guild_id=guild_id,
            discord_id=discord_id,
            torn_user_id=int((user_row or {}).get("torn_user_id") or 0),
            torn_name=(user_row or {}).get("torn_name"),
        )
        if int(wallet.get("balance_tokens") or 0) < qty_tokens:
            raise ValueError("Insufficient tokens.")

        async with self.repo.acquire() as conn:
            async with conn.transaction():
                cashout = await self.repo.create_cashout_request(conn, guild_id=guild_id, wallet_id=int(wallet["id"]), qty_tokens=qty_tokens, note=note)
                await self.repo.apply_ledger_entry_atomic(
                    conn,
                    guild_id=guild_id,
                    wallet_id=int(wallet["id"]),
                    entry_type="cashout_escrow_debit",
                    amount_tokens=-int(qty_tokens),
                    idempotency_key=f"cashout:{int(cashout['id'])}:escrow",
                    ref_type="casino_cashouts",
                    ref_id=int(cashout["id"]),
                    metadata={"note": note or ""},
                )

        from views.casino_core.cashout_panel import HouseCashoutActionView

        em = discord.Embed(title="Cashout Requested", color=discord.Color.orange())
        em.description = f"Cashout #{cashout['id']}\nPlayer: <@{discord_id}>\nQty: **{qty_tokens}**"
        view = HouseCashoutActionView(int(guild_id), int(cashout["id"]))

        try:
            house_user = interaction.client.get_user(int(house["house_discord_id"]))
            if house_user is None:
                house_user = await interaction.client.fetch_user(int(house["house_discord_id"]))
            if house_user:
                await house_user.send(embed=em, view=view)
        except Exception as exc:
            logging.warning("Cashout request DM failed for guild_id=%s cashout_id=%s: %s", guild_id, int(cashout["id"]), exc)

        if house.get("cashout_inbox_channel_id"):
            try:
                channel = interaction.guild.get_channel(int(house["cashout_inbox_channel_id"])) if interaction.guild else None
                if channel is None:
                    channel = await interaction.client.fetch_channel(int(house["cashout_inbox_channel_id"]))
                if isinstance(channel, discord.abc.Messageable):
                    await channel.send(embed=em, view=view)
            except Exception as exc:
                logging.warning("Cashout request inbox post failed for guild_id=%s cashout_id=%s: %s", guild_id, int(cashout["id"]), exc)

        return int(cashout["id"])

    async def verify_payout(self, interaction: discord.Interaction, guild_id: int, cashout_id: int) -> bool:
        settings = await GuildSettingsRepository(get_database()).get_or_create(guild_id)
        house = get_house_config(settings)
        if int(interaction.user.id) != int(house.get("house_discord_id") or 0):
            raise ValueError("Only house user can verify payouts.")

        house_row = await self.users.get_user_api_key(int(house["house_discord_id"]))
        if not house_row:
            raise ValueError("House API key not configured.")
        house_api = get_security_manager().decrypt_api_key(house_row["encrypted_key"])

        async with self.repo.acquire() as conn:
            async with conn.transaction():
                cashout = await self.repo.fetch_cashout(conn, guild_id=guild_id, cashout_id=cashout_id)
                if not cashout or cashout.get("status") != "requested":
                    return False
                wallet = await self.repo.get_wallet_by_id(int(cashout["wallet_id"]))
                logs = await get_torn_api().get_item_send_receive_logs(house_api, limit=200)
                match = None
                requested_at_ts = int(cashout["requested_at"].astimezone(timezone.utc).timestamp())
                for log in logs:
                    data = log.get("data") or {}
                    details = log.get("details") or {}
                    if int(details.get("id") or 0) != 4102:
                        continue
                    if int(data.get("sender") or data.get("user") or 0) != int(house["house_torn_id"]):
                        continue
                    if int(data.get("receiver") or 0) != int(wallet.get("torn_user_id") or 0):
                        continue
                    if int(log.get("timestamp") or 0) < requested_at_ts - 300:
                        continue
                    qty = sum(int(it.get("qty") or 0) for it in (data.get("items") or []) if int(it.get("id") or 0) == 206)
                    if qty == int(cashout["qty_tokens"]):
                        match = log
                        break
                if not match:
                    return False

                log_id = str(match.get("id") or match.get("log_id") or match.get("log"))
                payouts_message_id = None
                proof_embed = discord.Embed(title="Casino Payout Verified", color=discord.Color.green())
                proof_embed.description = f"Cashout #{cashout['id']} | <@{wallet['discord_id']}>\nQty: **{cashout['qty_tokens']}**\nLog: `{log_id}`"
                proof_embed.add_field(name="Log excerpt", value=f"```json\n{trunc_json(match, 500)}\n```", inline=False)
                try:
                    payouts_channel = interaction.guild.get_channel(int(house["payouts_channel_id"])) if interaction.guild else None
                    if payouts_channel is None:
                        payouts_channel = await interaction.client.fetch_channel(int(house["payouts_channel_id"]))
                    if isinstance(payouts_channel, discord.abc.Messageable):
                        msg = await payouts_channel.send(embed=proof_embed)
                        payouts_message_id = int(msg.id)
                except Exception as exc:
                    logging.warning("Payout proof post failed for guild_id=%s cashout_id=%s: %s", guild_id, cashout_id, exc)

                await self.repo.mark_cashout_verified_sent(
                    conn,
                    guild_id=guild_id,
                    cashout_id=cashout_id,
                    verified_by=int(interaction.user.id),
                    payout_torn_log_id=log_id,
                    payout_raw_log=match,
                    payouts_channel_message_id=payouts_message_id,
                )
                await self.repo.append_house_ledger(
                    conn,
                    guild_id=guild_id,
                    entry_type="payout_out",
                    amount_tokens=-int(cashout["qty_tokens"]),
                    ref_type="casino_cashouts",
                    ref_id=int(cashout_id),
                    metadata={"wallet_id": int(wallet["id"])},
                )
                return True

    async def deny_cashout(self, guild_id: int, cashout_id: int, denied_by: int, reason: str) -> None:
        async with self.repo.acquire() as conn:
            async with conn.transaction():
                cashout = await self.repo.fetch_cashout(conn, guild_id=guild_id, cashout_id=cashout_id)
                if not cashout or cashout.get("status") != "requested":
                    return
                await self.repo.mark_cashout_denied(conn, guild_id=guild_id, cashout_id=cashout_id, denied_by=denied_by, reason=reason)
                await self.repo.apply_ledger_entry_atomic(
                    conn,
                    guild_id=int(guild_id),
                    wallet_id=int(cashout["wallet_id"]),
                    entry_type="cashout_refund_credit",
                    amount_tokens=int(cashout["qty_tokens"]),
                    idempotency_key=f"cashout:{int(cashout_id)}:refund",
                    ref_type="casino_cashouts",
                    ref_id=int(cashout_id),
                    metadata={"reason": reason},
                )
