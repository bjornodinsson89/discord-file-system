from __future__ import annotations

from datetime import datetime, timezone
import logging

import discord

from repositories.casino_core import CasinoCoreRepository
from repositories.users import UsersRepository
from services.casino_core.settings import get_house_config
from utils import GuildSettingsRepository, get_database, get_security_manager, get_torn_api
from utils.database import get_pool
from views.casino_core.permissions import is_casino_admin


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


class CasinoCashoutService:
    def __init__(self):
        self.repo = CasinoCoreRepository(get_pool())
        self.users = UsersRepository(get_pool())

    async def _get_proof_channel(
        self, interaction: discord.Interaction, payout_proof_channel_id: int
    ) -> discord.abc.Messageable | None:
        if payout_proof_channel_id <= 0:
            return None
        try:
            payouts_channel = (
                interaction.guild.get_channel(payout_proof_channel_id) if interaction.guild else None
            )
            if payouts_channel is None:
                payouts_channel = await interaction.client.fetch_channel(payout_proof_channel_id)
            if isinstance(payouts_channel, discord.abc.Messageable):
                return payouts_channel
        except Exception as exc:
            logger.warning(
                "Cashout proof channel load failed for guild_id=%s channel_id=%s: %s",
                interaction.guild_id,
                payout_proof_channel_id,
                exc,
            )
        return None

    async def _notify_verifier_ephemeral(self, interaction: discord.Interaction, message: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
        except Exception as exc:
            logger.warning("Verifier ephemeral notify failed: %s", exc)

    async def request_cashout(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        discord_id: int,
        qty_tokens: int,
        note: str | None,
    ) -> int:
        settings = await GuildSettingsRepository(get_database()).get_or_create(guild_id)
        house = get_house_config(settings)
        required = ("house_discord_id", "house_torn_id")
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
                cashout = await self.repo.create_cashout_request(
                    conn,
                    guild_id=guild_id,
                    wallet_id=int(wallet["id"]),
                    qty_tokens=qty_tokens,
                    note=note,
                )
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
        requested_at = datetime.now(tz=timezone.utc).isoformat()
        em.description = (
            f"Cashout #{cashout['id']}\n"
            f"Player: <@{discord_id}>\n"
            f"Qty: **{qty_tokens}**\n"
            f"Requested: `{requested_at}`"
        )
        view = HouseCashoutActionView(int(guild_id), int(cashout["id"]))

        try:
            house_user_id = int(house["house_discord_id"])
            house_member = interaction.guild.get_member(house_user_id) if interaction.guild else None
            house_user = house_member or interaction.client.get_user(house_user_id)
            if house_user is None:
                house_user = await interaction.client.fetch_user(house_user_id)
            await house_user.send(embed=em, view=view)
        except discord.Forbidden as exc:
            logger.warning(
                "Cashout request DM forbidden for guild_id=%s cashout_id=%s: %s",
                guild_id,
                int(cashout["id"]),
                exc,
            )
            raise ValueError("House DMs are closed; your cashout request could not be delivered.") from exc
        except Exception as exc:
            logger.warning(
                "Cashout request DM failed for guild_id=%s cashout_id=%s: %s",
                guild_id,
                int(cashout["id"]),
                exc,
            )
            raise ValueError("Cashout request could not be delivered to house via DM.") from exc

        return int(cashout["id"])

    async def verify_payout(self, interaction: discord.Interaction, guild_id: int, cashout_id: int) -> bool:
        settings = await GuildSettingsRepository(get_database()).get_or_create(guild_id)
        house = get_house_config(settings)
        if not await is_casino_admin(interaction, guild_id):
            raise ValueError("Admin only.")

        house_row = await self.users.get_user_api_key(int(house["house_discord_id"]))
        if not house_row:
            raise ValueError("House API key not configured.")
        house_api = get_security_manager().decrypt_api_key(house_row["encrypted_key"])

        async with self.repo.acquire() as conn:
            async with conn.transaction():
                cashout = await self.repo.fetch_cashout(conn, guild_id=guild_id, cashout_id=cashout_id)
                if not cashout or cashout.get("status") != "requested":
                    return False
                wallet = await self.repo.get_wallet_by_id_for_guild(
                    int(guild_id), int(cashout["wallet_id"])
                )
                if not wallet:
                    raise ValueError("Cashout wallet not found for guild")
                requested_at_ts = int(cashout["requested_at"].astimezone(timezone.utc).timestamp())

                logger.info(
                    "cashout.verify.start guild_id=%s cashout_id=%s wallet_discord_id=%s qty=%s",
                    guild_id,
                    cashout_id,
                    wallet.get("discord_id"),
                    cashout.get("qty_tokens"),
                )

                logs: list[dict] = []
                log_fetch_failed = False
                try:
                    logs = await get_torn_api().get_item_send_receive_logs(house_api, limit=200)
                except Exception as exc:
                    log_fetch_failed = True
                    logger.warning(
                        "Payout log fetch failed for guild_id=%s cashout_id=%s: %s",
                        guild_id,
                        cashout_id,
                        exc,
                    )

                match = None
                for log_entry in logs:
                    sender = _torn_extract_sender_id(log_entry)
                    receiver = _torn_extract_receiver_id(log_entry)
                    qty = _torn_extract_item_qty(log_entry, 206)
                    ts = _to_int(log_entry.get("timestamp"))
                    if sender != int(house["house_torn_id"]):
                        continue
                    if receiver != int(wallet.get("torn_user_id") or 0):
                        continue
                    if ts < requested_at_ts - 300:
                        continue
                    if qty != int(cashout["qty_tokens"]):
                        continue
                    match = log_entry
                    break

                if not match:
                    logger.info(
                        "cashout.verify.no_match guild_id=%s cashout_id=%s requested_at=%s log_fetch_failed=%s",
                        guild_id,
                        cashout_id,
                        requested_at_ts,
                        log_fetch_failed,
                    )
                    proof_channel_id = int(house.get("payout_proof_channel_id") or 0)
                    if proof_channel_id and log_fetch_failed:
                        try:
                            payouts_channel = await self._get_proof_channel(interaction, proof_channel_id)
                            if payouts_channel is not None:
                                await payouts_channel.send(
                                    f"⚠️ Payout proof for request #{cashout_id}: log fetch failed."
                                )
                        except Exception as exc:
                            logger.warning(
                                "Payout proof failure notice post failed for guild_id=%s cashout_id=%s: %s",
                                guild_id,
                                cashout_id,
                                exc,
                            )
                    return False

                log_id = str(match.get("id") or match.get("log_id") or match.get("log") or "")
                payouts_message_id = None
                proof_channel_id = int(house.get("payout_proof_channel_id") or 0)
                if proof_channel_id:
                    proof_embed = discord.Embed(title="Casino Payout Verified", color=discord.Color.green())
                    proof_embed.description = (
                        f"Requester: <@{wallet['discord_id']}>\n"
                        f"Amount: **{cashout['qty_tokens']}**\n"
                        f"Cashout ID: **#{cashout['id']}**\n"
                        f"Timestamp: <t:{requested_at_ts}:f>\n"
                        f"Torn Log ID: `{log_id}`"
                    )
                    try:
                        payouts_channel = await self._get_proof_channel(interaction, proof_channel_id)
                        if payouts_channel is not None:
                            msg = await payouts_channel.send(embed=proof_embed)
                            payouts_message_id = int(msg.id)
                    except Exception as exc:
                        logger.warning(
                            "Payout proof post failed for guild_id=%s cashout_id=%s: %s",
                            guild_id,
                            cashout_id,
                            exc,
                        )

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

        logger.info(
            "cashout.verify.success guild_id=%s cashout_id=%s requester_id=%s",
            guild_id,
            cashout_id,
            wallet.get("discord_id"),
        )

        dm_failed = False
        proof_channel_mention = (
            f"<#{int(house.get('payout_proof_channel_id') or 0)}>"
            if int(house.get("payout_proof_channel_id") or 0)
            else "Not configured"
        )
        requester_id = int(wallet.get("discord_id") or 0)
        try:
            requester_user = interaction.client.get_user(requester_id)
            if requester_user is None:
                requester_user = await interaction.client.fetch_user(requester_id)
            if requester_user is None:
                raise ValueError("requester user not found")
            dm_embed = discord.Embed(title="✅ Cashout processed", color=discord.Color.green())
            dm_embed.description = "Your cashout has been processed."
            dm_embed.add_field(name="Amount", value=f"**{int(cashout['qty_tokens'])}**", inline=False)
            dm_embed.add_field(name="Cashout ID", value=f"#{int(cashout_id)}", inline=False)
            dm_embed.add_field(name="Verified by", value=f"<@{int(interaction.user.id)}>", inline=False)
            dm_embed.add_field(
                name="House",
                value=f"<@{int(house['house_discord_id'])}> (Torn ID: {int(house['house_torn_id'])})",
                inline=False,
            )
            dm_embed.add_field(name="Proof channel", value=proof_channel_mention, inline=False)
            dm_embed.add_field(name="Log ID", value=f"`{log_id}`", inline=False)
            await requester_user.send(embed=dm_embed)
        except Exception as exc:
            dm_failed = True
            logger.warning(
                "Cashout verified but requester DM failed for guild_id=%s cashout_id=%s requester_id=%s: %s",
                guild_id,
                cashout_id,
                requester_id,
                exc,
            )

        if dm_failed:
            proof_channel_id = int(house.get("payout_proof_channel_id") or 0)
            if proof_channel_id:
                payouts_channel = await self._get_proof_channel(interaction, proof_channel_id)
                if payouts_channel is not None:
                    await payouts_channel.send(
                        f"⚠️ Could not DM <@{requester_id}> that cashout #{cashout_id} was processed (DMs closed)."
                    )
            await self._notify_verifier_ephemeral(
                interaction,
                f"⚠️ Could not DM <@{requester_id}> about verified cashout #{cashout_id} (DMs closed).",
            )

        return True

    async def deny_cashout(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        cashout_id: int,
        denied_by: int,
        reason: str,
    ) -> None:
        settings = await GuildSettingsRepository(get_database()).get_or_create(guild_id)
        house = get_house_config(settings)
        wallet_discord_id = 0
        qty_tokens = 0
        async with self.repo.acquire() as conn:
            async with conn.transaction():
                cashout = await self.repo.fetch_cashout(conn, guild_id=guild_id, cashout_id=cashout_id)
                if not cashout or cashout.get("status") != "requested":
                    return
                qty_tokens = int(cashout["qty_tokens"])
                wallet = await self.repo.get_wallet_by_id_for_guild(int(guild_id), int(cashout["wallet_id"]))
                wallet_discord_id = int((wallet or {}).get("discord_id") or 0)
                await self.repo.mark_cashout_denied(
                    conn,
                    guild_id=guild_id,
                    cashout_id=cashout_id,
                    denied_by=denied_by,
                    reason=reason,
                )
                await self.repo.apply_ledger_entry_atomic(
                    conn,
                    guild_id=int(guild_id),
                    wallet_id=int(cashout["wallet_id"]),
                    entry_type="cashout_refund_credit",
                    amount_tokens=qty_tokens,
                    idempotency_key=f"cashout:{int(cashout_id)}:refund",
                    ref_type="casino_cashouts",
                    ref_id=int(cashout_id),
                    metadata={"reason": reason},
                )

        dm_failed = False
        proof_channel_mention = (
            f"<#{int(house.get('payout_proof_channel_id') or 0)}>"
            if int(house.get("payout_proof_channel_id") or 0)
            else "Not configured"
        )
        try:
            requester_user = interaction.client.get_user(wallet_discord_id)
            if requester_user is None:
                requester_user = await interaction.client.fetch_user(wallet_discord_id)
            if requester_user is None:
                raise ValueError("requester user not found")
            dm_embed = discord.Embed(title="❌ Cashout denied", color=discord.Color.red())
            dm_embed.description = "Your cashout request was denied and refunded."
            dm_embed.add_field(name="Amount refunded", value=f"**{qty_tokens}**", inline=False)
            dm_embed.add_field(name="Cashout ID", value=f"#{int(cashout_id)}", inline=False)
            dm_embed.add_field(name="Denied by", value=f"<@{int(denied_by)}>", inline=False)
            dm_embed.add_field(name="Reason", value=(reason or "No reason provided")[:300], inline=False)
            dm_embed.add_field(
                name="House",
                value=f"<@{int(house['house_discord_id'])}>",
                inline=False,
            )
            dm_embed.add_field(name="Proof channel", value=proof_channel_mention, inline=False)
            await requester_user.send(embed=dm_embed)
        except Exception as exc:
            dm_failed = True
            logger.warning(
                "Cashout denied/refunded but requester DM failed for guild_id=%s cashout_id=%s requester_id=%s: %s",
                guild_id,
                cashout_id,
                wallet_discord_id,
                exc,
            )

        if dm_failed:
            proof_channel_id = int(house.get("payout_proof_channel_id") or 0)
            if proof_channel_id:
                payouts_channel = await self._get_proof_channel(interaction, proof_channel_id)
                if payouts_channel is not None:
                    await payouts_channel.send(
                        f"⚠️ Could not DM <@{wallet_discord_id}> that cashout #{cashout_id} was denied/refunded (DMs closed)."
                    )
            await self._notify_verifier_ephemeral(
                interaction,
                f"⚠️ Could not DM <@{wallet_discord_id}> about denied/refunded cashout #{cashout_id} (DMs closed).",
            )
