from __future__ import annotations

import logging
import re
from typing import Any

import discord

from utils import GuildSettingsRepository
from repositories.audit import AuditRepository
from repositories.engagement import EngagementRepository
from repositories.store import StoreRepository
from repositories.prize_tokens import PrizeTokensRepository
from services.role_reward_service import RoleRewardService
from services.engagement_service import level_from_total_xp
from cogs.store import AddStoreItemModal, UpdateItemModal, StockAdjustModal, RedemptionActionModal
from utils.database import MissingDatabaseColumnError
from utils.discord_channels import resolve_guild_channel
from utils.embeds import create_error_embed, create_info_embed, create_success_embed
from constants.insurers import INSURER_CATEGORIES, normalize_insurer_categories
from repositories.applications import ApplicationsRepository

log = logging.getLogger("happy_jumper.setup_panel")

SUPPORTED_PLACEHOLDERS = "`{user}` `{mention}` `{guild}` `{channel}` `{timestamp}`"
WELCOME_SUPPORTED_PLACEHOLDERS = (
    "`{user_mention}` `{user_name}` `{guild_name}` `{rules_channel_mention}` `{rules_channel_name}`"
)
DEFAULT_WELCOME_TEMPLATE = (
    "👋 Welcome {user_mention}!\n\n"
    "Please make sure you read {rules_channel_mention} so you know how everything works.\n\n"
    "Glad to have you here — enjoy your stay!"
)


def has_setup_permission(
    member_id: int,
    guild_owner_id: int,
    is_administrator: bool,
    can_manage_guild: bool,
    member_role_ids: set[int] | set[str],
    admin_role_ids: list[int] | list[str],
) -> bool:
    if member_id == guild_owner_id:
        return True
    if is_administrator or can_manage_guild:
        return True

    normalized_member_roles: set[int] = set()
    for role_id in (member_role_ids or set()):
        try:
            normalized_member_roles.add(int(role_id))
        except (TypeError, ValueError):
            continue

    for role_id in (admin_role_ids or []):
        try:
            if int(role_id) in normalized_member_roles:
                return True
        except (TypeError, ValueError):
            continue
    return False


async def ensure_setup_permission(interaction: discord.Interaction, db) -> tuple[bool, dict[str, Any] | None]:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False, None

    repo = GuildSettingsRepository(db)
    settings = await repo.get_or_create(interaction.guild.id)
    member = interaction.user
    allowed = has_setup_permission(
        member_id=member.id,
        guild_owner_id=interaction.guild.owner_id,
        is_administrator=member.guild_permissions.administrator,
        can_manage_guild=member.guild_permissions.manage_guild,
        member_role_ids={role.id for role in member.roles},
        admin_role_ids=GuildSettingsRepository.resolve_admin_role_ids(settings),
    )
    return allowed, settings


def detect_rules_channel(guild: discord.Guild) -> discord.TextChannel | None:
    exact = discord.utils.find(lambda channel: channel.name.lower() == "rules", guild.text_channels)
    if exact:
        return exact
    return discord.utils.find(lambda channel: "rule" in channel.name.lower(), guild.text_channels)


def render_welcome_template(template: str, member: discord.Member, rules_channel: discord.TextChannel | None) -> str:
    channel_mention = rules_channel.mention if rules_channel else "the rules channel"
    channel_name = rules_channel.name if rules_channel else "rules"
    rendered = template
    replacements = {
        "{user_mention}": member.mention,
        "{user_name}": member.display_name,
        "{guild_name}": member.guild.name,
        "{rules_channel_mention}": channel_mention,
        "{rules_channel_name}": channel_name,
    }
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    return rendered[:1900]


def _can_manage_welcome_settings(member: discord.Member, settings: dict[str, Any]) -> bool:
    allowed_role_ids = set(GuildSettingsRepository.resolve_admin_role_ids(settings))
    member_role_ids = {role.id for role in member.roles}
    return bool(allowed_role_ids.intersection(member_role_ids))


async def _ensure_welcome_permission(interaction: discord.Interaction, settings: dict[str, Any]) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    if _can_manage_welcome_settings(interaction.user, settings):
        return True
    await interaction.response.send_message("No permission", ephemeral=True)
    return False


def _render_template(template: str, interaction: discord.Interaction) -> str:
    channel = interaction.channel.mention if interaction.channel else "#unknown"
    replacements = {
        "{user}": interaction.user.display_name,
        "{mention}": interaction.user.mention,
        "{guild}": interaction.guild.name if interaction.guild else "Guild",
        "{channel}": channel,
        "{timestamp}": discord.utils.format_dt(discord.utils.utcnow(), style="F"),
    }
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace(key, value)
    return rendered[:1800]


async def _send_or_edit(interaction: discord.Interaction, embed: discord.Embed, view: discord.ui.View | None = None):
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.response.edit_message(embed=embed, view=view)


def _missing_channel_perms(channel: discord.abc.GuildChannel, me: discord.Member) -> list[str]:
    perms = channel.permissions_for(me)
    missing: list[str] = []
    if not perms.view_channel:
        missing.append('View Channel')
    if not perms.send_messages:
        missing.append('Send Messages')
    if not perms.embed_links:
        missing.append('Embed Links')
    return missing


def _interaction_response_is_done(interaction: discord.Interaction) -> bool:
    is_done = getattr(interaction.response, "is_done", None)
    return bool(is_done()) if callable(is_done) else False


async def _maybe_defer_setup_response(interaction: discord.Interaction) -> None:
    defer = getattr(interaction.response, "defer", None)
    if callable(defer) and not _interaction_response_is_done(interaction):
        await defer(ephemeral=True)


async def _send_setup_response(interaction: discord.Interaction, content: str, *, ephemeral: bool = True) -> None:
    if _interaction_response_is_done(interaction):
        await interaction.followup.send(content, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(content, ephemeral=ephemeral)




def _parse_friendly_int(raw_value: str, *, label: str) -> int:
    cleaned = str(raw_value or "").strip().replace(",", "")
    if not cleaned or not re.fullmatch(r"\d+", cleaned):
        raise ValueError(f"{label} must be a whole number.")
    return int(cleaned)

async def _send_setup_error_message(interaction: discord.Interaction, message: str) -> None:
    embed = create_error_embed("Setup failed", message)
    if _interaction_response_is_done(interaction):
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def _respond_callback_error(interaction: discord.Interaction, error: Exception, error_code: str = "setup_callback_error"):
    log.exception(
        'Setup callback error guild_id=%s user_id=%s',
        interaction.guild_id,
        interaction.user.id if interaction.user else None,
        exc_info=(type(error), error, error.__traceback__),
    )
    msg = (
        "Unexpected setup error. Please try again, or rerun /setup if this continues. "
        f"Check bot logs for: {error_code}"
    )
    await _send_setup_error_message(interaction, msg)


class OwnerView(discord.ui.View):
    def __init__(self, *, owner_id: int, db, settings: dict[str, Any], timeout: float = 300):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.db = db
        self.settings = settings

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=create_error_embed("Not your panel", "Only the user who ran `/setup` can use this panel."),
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any]) -> None:
        selected = None
        if hasattr(item, "values"):
            values = item.values or []
            if values:
                selected = values[0]
        log.exception(
            "Setup callback failed guild_id=%s user_id=%s selected_id=%s selected_type=%s item_type=%s",
            interaction.guild_id,
            interaction.user.id if interaction.user else None,
            getattr(selected, "id", None),
            type(selected).__name__ if selected else None,
            type(item).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
        message = (
            "Something went wrong while saving this setup option. "
            "Please try again. If this keeps happening, re-run `/setup`."
        )
        await _send_setup_error_message(interaction, message)


class TemplateModal(discord.ui.Modal):
    def __init__(self, panel: "SetupPanelView", field_name: str, label: str, current_value: str | None):
        super().__init__(title=label)
        self.panel = panel
        self.field_name = field_name
        self.template_input = discord.ui.TextInput(
            label=label,
            style=discord.TextStyle.paragraph,
            max_length=1500,
            required=False,
            default=current_value or "",
        )
        self.add_item(self.template_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            await self.panel.save_changes(interaction, {self.field_name: str(self.template_input.value).strip() or None})
        except Exception as error:
            await _respond_callback_error(interaction, error, "setup_channels_save_error")


class TimeoutModal(discord.ui.Modal):
    timeout_minutes = discord.ui.TextInput(label="Reservation timeout minutes (1-60)", required=True, max_length=2)

    def __init__(self, panel: "SetupPanelView", current: int | None):
        super().__init__(title="Set Reservation Timeout")
        self.panel = panel
        self.timeout_minutes.default = str(current or 5)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            raw = str(self.timeout_minutes.value).strip()
            if not raw.isdigit() or not 1 <= int(raw) <= 60:
                await interaction.response.send_message(embed=create_error_embed("Invalid value", "Reservation timeout must be between 1 and 60."), ephemeral=True)
                return
            await self.panel.save_changes(interaction, {"reservation_timeout_minutes": int(raw)})
        except Exception as error:
            await _respond_callback_error(interaction, error)


class ApplicationsInboxChannelModal(discord.ui.Modal):
    def __init__(self, panel: "SetupPanelView"):
        super().__init__(title="Set applications inbox")
        self.panel = panel
        self.channel_input = discord.ui.TextInput(
            label="Channel",
            placeholder="#applications or 123456789012345678",
            required=True,
            max_length=64,
        )
        self.add_item(self.channel_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            guild = interaction.guild
            if guild is None:
                await interaction.response.send_message(
                    embed=create_error_embed("Invalid context", "This action can only be used in a server."),
                    ephemeral=True,
                )
                return

            raw = str(self.channel_input.value).strip()
            match = re.search(r"(\d{15,25})", raw)
            if not match:
                await interaction.response.send_message(
                    embed=create_error_embed(
                        "Invalid channel",
                        "Enter a channel mention like `#applications` or a raw channel ID.",
                    ),
                    ephemeral=True,
                )
                return

            channel = guild.get_channel(int(match.group(1)))
            if not isinstance(channel, discord.TextChannel):
                await interaction.response.send_message(
                    embed=create_error_embed(
                        "Invalid channel",
                        "That channel must exist in this server and be a text channel.",
                    ),
                    ephemeral=True,
                )
                return

            await self.panel.save_changes(interaction, {"applications_admin_inbox_channel_id": channel.id})
        except Exception as error:
            await _respond_callback_error(interaction, error, "setup_applications_inbox_modal_error")


class RaffleHostRoleModal(discord.ui.Modal):
    def __init__(self, panel: "SetupPanelView"):
        super().__init__(title="Set Raffle Host Role")
        self.panel = panel
        self.role_input = discord.ui.TextInput(
            label="Role",
            placeholder="@Role, role ID, or 'clear'",
            required=True,
            max_length=64,
        )
        self.add_item(self.role_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            guild = interaction.guild
            if guild is None:
                await interaction.response.send_message(
                    embed=create_error_embed("Invalid context", "This action can only be used in a server."),
                    ephemeral=True,
                )
                return

            raw = str(self.role_input.value).strip()
            if raw.lower() in {"clear", "none", "null", "remove"}:
                await self.panel.save_changes(interaction, {"raffle_host_role_id": None})
                await interaction.followup.send("Raffle Host Role cleared.", ephemeral=True)
                return

            match = re.search(r"(\d{15,25})", raw)
            if not match:
                await interaction.response.send_message(
                    embed=create_error_embed("Invalid role", "Enter a role mention like `@Raffle Host` or a raw role ID."),
                    ephemeral=True,
                )
                return

            role = guild.get_role(int(match.group(1)))
            if role is None or role.is_default():
                await interaction.response.send_message(
                    embed=create_error_embed("Invalid role", "That role must exist in this server and cannot be @everyone."),
                    ephemeral=True,
                )
                return

            await self.panel.save_changes(interaction, {"raffle_host_role_id": int(role.id)})
            await interaction.followup.send(f"Raffle Host Role set to {role.mention}", ephemeral=True)
        except Exception as error:
            await _respond_callback_error(interaction, error)


class SetupPanelView(OwnerView):
    SECTION_ORDER = [
        "Channels",
        "Roles",
        "Engagement",
        "Raffles",
        "Giveaways",
        "Store",
        "Welcome",
        "Maintenance",
    ]

    @staticmethod
    def _resolve_bot_member(interaction: discord.Interaction) -> discord.Member | None:
        if not interaction.guild:
            return None
        me = interaction.guild.me or interaction.guild.get_member(interaction.client.user.id)
        return me

    def _channel_status(self, key: str) -> str:
        cid = self.settings.get(key)
        ch = self.guild.get_channel(cid) if cid else None
        return ch.mention if ch else "Not set"

    def _role_status(self, key: str) -> str:
        rid = self.settings.get(key)
        role = self.guild.get_role(rid) if rid else None
        return role.mention if role else "Not set"

    def _format_admin_roles(self) -> str:
        mentions = []
        for rid in (self.settings.get("admin_role_ids") or []):
            role = self.guild.get_role(int(rid)) if str(rid).isdigit() else None
            if role:
                mentions.append(role.mention)
        return ", ".join(mentions) if mentions else "Not set"

    def _reward_role_health(self) -> str:
        reward_roles = self.engagement_settings.get("reward_roles") or self.engagement_settings.get("reward_role_ids") or []
        return "Healthy" if reward_roles else "Needs review"

    def _storefront_health(self) -> str:
        return "Live" if self.store_settings.get("store_channel_id") and self.store_settings.get("enabled", False) else "Missing"

    def _panel_health(self, key: str) -> str:
        return "Live" if self.settings.get(key) else "Missing"

    def _welcome_health(self) -> str:
        return "Healthy" if self.settings.get("welcome_channel_id") else "Missing"

    def _system_health_lines(self) -> list[str]:
        return [
            f"Reward Roles: **{self._reward_role_health()}**",
            f"Storefront: **{self._storefront_health()}**",
            f"Giveaway Panels: **{self._panel_health('raffle_giveaway_purchase_channel_id')}**",
            f"Raffle Panels: **{self._panel_health('raffle_purchase_channel_id')}**",
            f"Welcome / Onboarding: **{self._welcome_health()}**",
        ]

    def _dashboard_summary_lines(self) -> list[str]:
        return [
            f"Jump Channel: **{self._channel_status('jump_99k_channel_id')}**",
            f"Store Channel: **{self.store_settings.get('store_channel_id') or 'Not set'}**",
            f"Welcome Channel: **{self._channel_status('welcome_channel_id')}**",
            f"Admin Roles: **{self._format_admin_roles()}**",
            f"Reward Roles: **{self._reward_role_health()}**",
            f"Storefront: **{self._storefront_health()}**",
        ]

    def _build_embed(self) -> discord.Embed:
        embed = create_info_embed(
            "Admin Control Center",
            "Quick status first. Pick a section below to configure this server.",
        )
        embed.add_field(name="Server Status", value="\n".join(self._dashboard_summary_lines()), inline=False)
        embed.add_field(
            name="Channels",
            value=(
                f"Jump Channel: {self._channel_status('jump_99k_channel_id')}\n"
                f"Raffle Channel: {self._channel_status('raffle_purchase_channel_id')}\n"
                f"Giveaway Channel: {self._channel_status('raffle_giveaway_purchase_channel_id')}\n"
                f"Who Can Jump: {self._channel_status('who_can_jump_channel_id')}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Store",
            value=f"Store channel: `{self.store_settings.get('store_channel_id') or 'Not set'}`\nStorefront: `{'Live' if self.store_settings.get('enabled', False) else 'Off'}`",
            inline=False,
        )
        embed.add_field(
            name="Sections",
            value=" • ".join(self.SECTION_ORDER),
            inline=False,
        )
        embed.set_footer(text="Compact pages, plain-English labels, and Discord-safe controls.")
        return embed

    def __init__(self, *, owner_id: int, db, settings: dict[str, Any], guild: discord.Guild, engagement_settings: dict[str, Any] | None = None):
        super().__init__(owner_id=owner_id, db=db, settings=settings)
        self.guild = guild
        self.engagement_repo = EngagementRepository(self.db.pool) if getattr(self.db, "pool", None) is not None else None
        self.store_repo = StoreRepository(self.db.pool) if getattr(self.db, "pool", None) is not None else None
        self.engagement_settings = engagement_settings or {}
        self.store_settings: dict[str, Any] = {}

    async def save_changes(self, interaction: discord.Interaction, changes: dict[str, Any]) -> None:
        old_values = {k: self.settings.get(k) for k in changes}
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=False)
        try:
            settings_repo = GuildSettingsRepository(self.db)
            await settings_repo.insert_or_get_guild_settings(interaction.guild_id)
            await settings_repo.upsert_settings(interaction.guild_id, **changes)
        except MissingDatabaseColumnError as exc:
            await interaction.followup.send(
                embed=create_error_embed(
                    "Database Update Required",
                    f"{exc}\n\n"
                    "Apply the latest repository migrations, then retry setup.",
                ),
                ephemeral=True,
            )
            return
        self.settings.update(changes)
        audit_repo = AuditRepository(self.db.pool)
        try:
            await audit_repo.log_audit(
                actor_discord_id=interaction.user.id,
                action="setup_panel_updated",
                target_type="guild",
                target_id=interaction.guild_id,
                payload={"changes": {k: {"old": old_values[k], "new": changes[k]} for k in changes}},
                guild_id=interaction.guild_id,
                source="discord",
            )
        except Exception:
            log.exception(
                "Audit logging failed during setup save guild_id=%s user_id=%s",
                interaction.guild_id,
                interaction.user.id,
            )
        await _send_or_edit(interaction, self._build_embed(), self)

    async def save_engagement_changes(self, interaction: discord.Interaction, changes: dict[str, Any]) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=False)
        if self.engagement_repo is None:
            return
        self.engagement_settings = await self.engagement_repo.upsert_guild_settings(interaction.guild_id, **changes)
        await _send_or_edit(interaction, self._build_embed(), self)

    async def save_store_changes(self, interaction: discord.Interaction, changes: dict[str, Any]) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=False)
        if self.store_repo is None:
            return
        self.store_settings = await self.store_repo.upsert_guild_settings(interaction.guild_id, **changes)
        store_cog = interaction.client.get_cog("StoreCog") if getattr(interaction, "client", None) else None
        if store_cog is not None and interaction.guild is not None and any(key in changes for key in {"store_channel_id", "enabled", "torn_item_store_enabled", "discord_perk_store_enabled"}):
            await store_cog.sync_storefront(interaction.guild)
        await _send_or_edit(interaction, self._build_embed(), self)

    async def _resolve_real_channel(self, interaction: discord.Interaction, selected: Any) -> discord.abc.GuildChannel | None:
        resolved = await resolve_guild_channel(interaction, selected)
        if resolved is None:
            log.warning(
                "Failed to resolve selected setup channel guild_id=%s channel_id=%s selected_type=%s",
                interaction.guild_id,
                getattr(selected, "id", None),
                type(selected).__name__,
            )
        return resolved

    async def _set_channel(self, interaction: discord.Interaction, key: str, channel: Any):
        if key == "welcome_channel_id":
            if not isinstance(interaction.user, discord.Member) or not _can_manage_welcome_settings(interaction.user, self.settings):
                await interaction.response.send_message("No permission", ephemeral=True)
                return
        log.info(
            "Setup channel selection guild_id=%s key=%s selected_channel_id=%s selected_type=%s",
            interaction.guild_id,
            key,
            getattr(channel, "id", None),
            type(channel).__name__,
        )
        resolved = await self._resolve_real_channel(interaction, channel)
        if resolved is None:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "Channel unavailable",
                    "Couldn't resolve that channel. Try again, or check bot permissions.",
                ),
                ephemeral=True,
            )
            return

        if key == "applications_admin_inbox_channel_id":
            if not isinstance(resolved, discord.TextChannel):
                await interaction.response.send_message(
                    embed=create_error_embed(
                        "Missing applications inbox",
                        "Set Applications inbox to a text channel.",
                    ),
                    ephemeral=True,
                )
                return

        me = self._resolve_bot_member(interaction)
        if me is None:
            await interaction.response.send_message(
                embed=create_error_embed("Bot member unavailable", "Unable to validate my permissions right now. Please try again."),
                ephemeral=True,
            )
            return
        mention = getattr(resolved, "mention", f"<#{resolved.id}>")
        missing = [] if key == 'applications_category_id' else _missing_channel_perms(resolved, me)
        if missing:
            await interaction.response.send_message(
                embed=create_error_embed("Missing permissions", f"Missing in {mention}: **{', '.join(missing)}**."),
                ephemeral=True,
            )
            return
        updates = {key: resolved.id}
        if key == "jump_99k_channel_id":
            updates["announce_channel_id"] = resolved.id
        await self.save_changes(interaction, updates)
        log.info("Setup channel updated guild_id=%s key=%s channel_id=%s", interaction.guild_id, key, resolved.id)

    @discord.ui.button(label="Channels", style=discord.ButtonStyle.primary, row=0)
    async def channels_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, build_section_embed("Channels", "Choose where each system posts.", [
            f"Jump Channel: **{self._channel_status('jump_99k_channel_id')}**",
            f"Raffle Channel: **{self._channel_status('raffle_purchase_channel_id')}**",
            f"Giveaway Channel: **{self._channel_status('raffle_giveaway_purchase_channel_id')}**",
            f"Store Channel: **{self.store_settings.get('store_channel_id') or 'Not set'}**",
        ]), ChannelsHubView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self))

    @discord.ui.button(label="Roles", style=discord.ButtonStyle.primary, row=0)
    async def roles_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, build_section_embed("Roles", "Set admin, host, and reward-role access.", [
            f"Admin Roles: **{self._format_admin_roles()}**",
            f"Host Role: **{self._role_status('raffle_host_role_id')}**",
            f"Insurance Role: **{self._role_status('insurer_role_id')}**",
            f"Reward Roles: **{self._reward_role_health()}**",
        ]), RolesHubView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self))

    @discord.ui.button(label="Engagement", style=discord.ButtonStyle.primary, row=0)
    async def engagement_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, build_section_embed("Engagement", "Tune XP, levels, HJD, and auto entry.", [
            f"XP: **{'Enabled' if self.engagement_settings.get('enabled', False) else 'Off'}**",
            "Coins: **Enabled**",
            "HJD: **Enabled**",
            f"Auto Entry: **{'Enabled' if self.engagement_settings.get('auto_entry_giveaways_enabled', True) else 'Off'}**",
        ]), EngagementHubView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self))

    @discord.ui.button(label="Raffles", style=discord.ButtonStyle.primary, row=0)
    async def raffles_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, build_section_embed("Raffles", "Set raffle channels and admin tools.", [
            f"Panel Channel: **{self._channel_status('raffle_purchase_channel_id')}**",
            "Active Raffles: **Live data in panel tools**",
            "Recovery Tools: **Available**",
        ]), RafflesHubView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self))

    @discord.ui.button(label="Giveaways", style=discord.ButtonStyle.primary, row=0)
    async def giveaways_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, build_section_embed("Giveaways", "Set giveaway posting and entry behavior.", [
            f"Giveaway Channel: **{self._channel_status('raffle_giveaway_purchase_channel_id')}**",
            f"Auto Entry: **{'Enabled' if self.engagement_settings.get('auto_entry_giveaways_enabled', True) else 'Off'}**",
            "Weighted Mode: **Available**",
        ]), GiveawaysHubView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self))

    @discord.ui.button(label="Store", style=discord.ButtonStyle.primary, row=1)
    async def store_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, build_section_embed("Store", "Manage the storefront and item catalog.", [
            f"Store Channel: **{self.store_settings.get('store_channel_id') or 'Not set'}**",
            "Currency: **HJD**",
            f"Storefront: **{'Live' if self.store_settings.get('enabled', False) else 'Off'}**",
            "Active Items: **Live list in Inventory**",
        ]), StoreHubView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self))

    @discord.ui.button(label="Welcome", style=discord.ButtonStyle.primary, row=1)
    async def welcome_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, build_section_embed("Welcome", "Handle onboarding and first-run messages.", [
            f"Welcome Channel: **{self._channel_status('welcome_channel_id')}**",
            f"API Key Onboarding: **{'Enabled' if self.settings.get('applications_admin_inbox_channel_id') else 'Needs setup'}**",
            f"Applications: **{'Enabled' if self.settings.get('applications_admin_inbox_channel_id') else 'Off'}**",
        ]), WelcomeHubView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self))

    @discord.ui.button(label="Maintenance", style=discord.ButtonStyle.secondary, row=1)
    async def maintenance_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(
            interaction,
            build_section_embed("Maintenance", "Check health, repairs, and rebuild tools.", self._system_health_lines()),
            MaintenanceHubView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self),
        )

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, row=1)
    async def close_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            for child in self.children:
                child.disabled = True
            await _send_or_edit(interaction, create_success_embed("Setup closed", "You can run `/setup` again anytime."), self)
        except Exception as error:
            await _respond_callback_error(interaction, error)


class BackView(OwnerView):
    def __init__(self, *, owner_id: int, db, settings: dict[str, Any], guild: discord.Guild, panel: SetupPanelView):
        super().__init__(owner_id=owner_id, db=db, settings=settings)
        self.guild = guild
        self.panel = panel

    # Select menus consume full rows; pin Back to row 4 to stay within Discord's 5-row view limit.
    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=4)
    async def back_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await _send_or_edit(interaction, self.panel._build_embed(), self.panel)
        except Exception as error:
            await _respond_callback_error(interaction, error)


class ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: SetupPanelView, key: str, placeholder: str, *, row: int | None = None, channel_types: list[discord.ChannelType] | None = None):
        super().__init__(
            placeholder=placeholder,
            min_values=0,
            max_values=1,
            channel_types=channel_types or [discord.ChannelType.text, discord.ChannelType.news],
            row=row,
        )
        self.panel = panel
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        try:
            log.info(
                "Setup channel select callback guild_id=%s key=%s selected_count=%s",
                interaction.guild_id,
                self.key,
                len(self.values),
            )
            if not self.values:
                await self.panel.save_changes(interaction, {self.key: None})
                return
            await self.panel._set_channel(interaction, self.key, self.values[0])
        except Exception as error:
            await _respond_callback_error(interaction, error, "setup_channels_save_error")


def _channels_embed() -> discord.Embed:
    return create_info_embed(
        "Channels",
        "Pick the channels each system uses.",
    )


def _log_channels_view_navigation_error(interaction: discord.Interaction, error: Exception, error_code: str) -> None:
    log.exception(
        "Setup channels view navigation failed guild_id=%s user_id=%s error_code=%s",
        interaction.guild_id,
        interaction.user.id if interaction.user else None,
        error_code,
        exc_info=(type(error), error, error.__traceback__),
    )


class ChannelsViewPage1(BackView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_item(ChannelSelect(self.panel, "welcome_channel_id", "Set welcome channel", row=0))
        self.add_item(ChannelSelect(self.panel, "pool_channel_id", "Set pools management channel", row=1))
        self.add_item(ChannelSelect(self.panel, "pools_post_channel_id", "Set pools purchase panel channel", row=2))

    @discord.ui.button(label="Next →", style=discord.ButtonStyle.primary, row=4)
    async def next_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            log.info("Setup channels next callback guild_id=%s user_id=%s", interaction.guild_id, interaction.user.id)
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
            await _send_or_edit(interaction, _channels_embed(), ChannelsViewPage2(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))
        except Exception as error:
            await _respond_callback_error(interaction, error, "setup_channels_next_error")


class ChannelsViewPage2(BackView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.remove_item(self.back_btn)
        self.add_item(ChannelSelect(self.panel, "jump_99k_channel_id", "Set jump (99k) channel", row=0))
        self.add_item(ChannelSelect(self.panel, "jump_announce_channel_id", "Set jump announcement channel", row=1))
        self.add_item(ChannelSelect(self.panel, "raffle_announcement_channel_id", "Set raffle announcement channel", row=2))

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=4)
    async def channels_back_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            log.info("Setup channels back callback guild_id=%s user_id=%s", interaction.guild_id, interaction.user.id)
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
            await _send_or_edit(interaction, _channels_embed(), ChannelsViewPage1(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))
        except Exception as error:
            await _respond_callback_error(interaction, error, "setup_channels_next_error")

    @discord.ui.button(label="Next →", style=discord.ButtonStyle.primary, row=4)
    async def channels_next_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            log.info("Setup channels next callback guild_id=%s user_id=%s", interaction.guild_id, interaction.user.id)
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
            await _send_or_edit(interaction, _channels_embed(), ChannelsViewPage3(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))
        except Exception as error:
            await _respond_callback_error(interaction, error, "setup_channels_next_error")


class ChannelsViewPage3(BackView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.remove_item(self.back_btn)
        self.add_item(ChannelSelect(self.panel, "raffle_purchase_channel_id", "Set paid raffle purchase panel channel", row=0))
        self.add_item(ChannelSelect(self.panel, "raffle_giveaway_purchase_channel_id", "Set giveaway purchase panel channel", row=1))

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=4)
    async def channels_back_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            log.info("Setup channels back callback guild_id=%s user_id=%s", interaction.guild_id, interaction.user.id)
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
            await _send_or_edit(interaction, _channels_embed(), ChannelsViewPage2(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))
        except Exception as error:
            await _respond_callback_error(interaction, error, "setup_channels_next_error")

    @discord.ui.button(label="Next →", style=discord.ButtonStyle.primary, row=4)
    async def channels_next_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            log.info("Setup channels next callback guild_id=%s user_id=%s", interaction.guild_id, interaction.user.id)
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
            await _send_or_edit(interaction, _channels_embed(), ChannelsViewPage4(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))
        except Exception as error:
            await _respond_callback_error(interaction, error, "setup_channels_next_error")


class ChannelsViewPage4(BackView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.remove_item(self.back_btn)
        self.add_item(ChannelSelect(self.panel, "insurance_channel_id", "Insurance Channel", row=0))
        self.add_item(ChannelSelect(self.panel, "jewelry_alert_channel_id", "Jewelry Alerts", row=1))
        self.add_item(ChannelSelect(self.panel, "who_can_jump_channel_id", "Who Can Jump", row=2))

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=4)
    async def channels_back_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            log.info("Setup channels back callback guild_id=%s user_id=%s", interaction.guild_id, interaction.user.id)
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
            await _send_or_edit(interaction, _channels_embed(), ChannelsViewPage3(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))
        except Exception as error:
            await _respond_callback_error(interaction, error, "setup_channels_next_error")

    @discord.ui.button(label="Next →", style=discord.ButtonStyle.primary, row=4)
    async def channels_next_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            log.info("Setup channels to roles callback guild_id=%s user_id=%s", interaction.guild_id, interaction.user.id)
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
            await _send_or_edit(
                interaction,
                create_info_embed(
                    "Applications",
                    "Pick where application reviews are created and sent.",
                ),
                ChannelsViewApplications(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel),
            )
        except Exception as error:
            await _respond_callback_error(interaction, error, "setup_roles_next_error")




class ChannelsViewApplications(BackView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.remove_item(self.back_btn)
        self.add_item(ChannelSelect(self.panel, "applications_category_id", "Applications Category", row=0, channel_types=[discord.ChannelType.category]))
        self.add_item(
            ChannelSelect(
                self.panel,
                "applications_admin_inbox_channel_id",
                "Applications Inbox",
                row=1,
                channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            )
        )

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=4)
    async def channels_back_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
            await _send_or_edit(interaction, _channels_embed(), ChannelsViewPage4(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))
        except Exception as error:
            await _respond_callback_error(interaction, error, "setup_channels_next_error")

    @discord.ui.button(label="Inbox by ID", style=discord.ButtonStyle.secondary, row=2, custom_id="setup:applications:set_inbox_by_id")
    async def set_inbox_by_id_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await interaction.response.send_modal(ApplicationsInboxChannelModal(self.panel))
        except Exception as error:
            await _respond_callback_error(interaction, error, "setup_applications_inbox_by_id_button_error")

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=3)
    async def save_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        me = self.guild.me
        applications_inbox = self.guild.get_channel(int(self.settings.get("applications_admin_inbox_channel_id") or 0))
        if not isinstance(applications_inbox, discord.TextChannel) or me is None:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "Missing applications admin inbox",
                    "Set Applications admin inbox channel using the picker or **Set inbox by ID/mention**.",
                ),
                ephemeral=True,
            )
            return
        perms = applications_inbox.permissions_for(me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                embed=create_error_embed(
                    "Can't access selected inbox",
                    "I can’t see/send in that channel. The channel selector only shows channels both you and the bot can access. Grant the bot View Channel + Send Messages + Embed Links in that channel/category.",
                ),
                ephemeral=True,
            )
            return
        await _send_or_edit(
            interaction,
            create_info_embed("Roles", "Configure admin, host, and insurer roles."),
            RolesViewPage1(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel),
        )

    @discord.ui.button(label="Next →", style=discord.ButtonStyle.primary, row=4)
    async def channels_next_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
            await _send_or_edit(
                interaction,
                create_info_embed("Roles", "Configure admin, host, and insurer roles."),
                RolesViewPage1(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel),
            )
        except Exception as error:
            await _respond_callback_error(interaction, error, "setup_roles_next_error")

class AdminRoleSelect(discord.ui.RoleSelect):
    def __init__(self, panel: SetupPanelView, setting_key: str = "admin_role_ids", placeholder: str = "Set admin role(s)", *, row: int | None = None):
        super().__init__(placeholder=placeholder, min_values=0, max_values=10, row=row)
        self.panel = panel
        self.setting_key = setting_key

    async def callback(self, interaction: discord.Interaction):
        try:
            log.info(
                "Setup role selection guild_id=%s key=%s selected_count=%s",
                interaction.guild_id,
                self.setting_key,
                len(self.values),
            )
            role_ids = [int(role.id) for role in self.values if not role.is_default()]
            if self.setting_key == "admin_role_ids":
                log.info(
                    "setup_next_roles_view guild_id=%s selected_admin_roles=%s",
                    interaction.guild_id,
                    role_ids,
                )
            await self.panel.save_changes(interaction, {self.setting_key: role_ids})
            if self.setting_key == "admin_role_ids":
                log.info(
                    "setup_next_roles_view_db_update_ok guild_id=%s selected_admin_roles=%s",
                    interaction.guild_id,
                    role_ids,
                )
        except Exception as error:
            if self.setting_key == "admin_role_ids":
                log.exception(
                    "setup_next_roles_view_error guild_id=%s selected_admin_roles=%s",
                    interaction.guild_id,
                    [int(role.id) for role in self.values if not role.is_default()],
                    exc_info=(type(error), error, error.__traceback__),
                )
            await _respond_callback_error(interaction, error)




class RaffleHostRoleSelect(discord.ui.RoleSelect):
    def __init__(self, panel: SetupPanelView, *, row: int | None = None):
        super().__init__(placeholder="Set Raffle Host Role (optional)", min_values=0, max_values=1, row=row)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        try:
            selected = [role for role in self.values if not role.is_default()]
            role_id = int(selected[0].id) if selected else None
            await self.panel.save_changes(interaction, {"raffle_host_role_id": role_id})
            if role_id is None:
                await interaction.followup.send("Raffle Host Role cleared.", ephemeral=True)
            else:
                await interaction.followup.send(f"Raffle Host Role set to <@&{role_id}>", ephemeral=True)
        except Exception as error:
            await _respond_callback_error(interaction, error)


class SingleRoleSelect(discord.ui.RoleSelect):
    def __init__(self, panel: SetupPanelView, key: str, placeholder: str, *, row: int | None = None):
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, row=row)
        self.panel = panel
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        try:
            log.info(
                "Setup single role selection guild_id=%s key=%s selected_count=%s",
                interaction.guild_id,
                self.key,
                len(self.values),
            )
            role = self.values[0]
            if role.is_default():
                await interaction.response.send_message(embed=create_error_embed("Invalid role", "Cannot use @everyone for this setting."), ephemeral=True)
                return
            await self.panel.save_changes(interaction, {self.key: int(role.id)})
        except Exception as error:
            await _respond_callback_error(interaction, error)


class RolesViewPage1(BackView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_item(AdminRoleSelect(self.panel, row=0))
        self.add_item(SingleRoleSelect(self.panel, "host99k_role_id", "Set 99k_Jump_Host role", row=1))
        self.add_item(SingleRoleSelect(self.panel, "insurer_role_id", "Set HJ_Insureance_provider role", row=2))
        self.add_item(RaffleHostRoleSelect(self.panel, row=3))

    @discord.ui.button(label="Set Raffle Host Role by ID/mention", style=discord.ButtonStyle.secondary, row=4, custom_id="setup:roles:set_raffle_host_by_id")
    async def set_raffle_host_by_id_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await interaction.response.send_modal(RaffleHostRoleModal(self.panel))
        except Exception as error:
            await _respond_callback_error(interaction, error)

    @discord.ui.button(label="Next →", style=discord.ButtonStyle.primary, row=4)
    async def next_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
            await _send_or_edit(
                interaction,
                create_info_embed("Roles", "Configure admin, host, and insurer roles."),
                RolesViewPage2(
                    owner_id=self.owner_id,
                    db=self.db,
                    settings=self.settings,
                    guild=self.guild,
                    panel=self.panel,
                ),
            )
        except Exception as error:
            await _respond_callback_error(interaction, error)


class RolesViewPage2(BackView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.remove_item(self.back_btn)
        self.add_item(AdminRoleSelect(self.panel, setting_key="jump_ping_role_ids", placeholder="Set jump ping role(s)", row=0))
        self.add_item(AdminRoleSelect(self.panel, setting_key="jewelry_alert_role_ids", placeholder="Set jewelry alert role(s)", row=1))

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=4)
    async def roles_back_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
            await _send_or_edit(
                interaction,
                create_info_embed("Roles", "Configure admin, host, and insurer roles."),
                RolesViewPage1(
                    owner_id=self.owner_id,
                    db=self.db,
                    settings=self.settings,
                    guild=self.guild,
                    panel=self.panel,
                ),
            )
        except Exception as error:
            await _respond_callback_error(interaction, error)


RolesView = RolesViewPage1




class InsurerProfileModal(discord.ui.Modal):
    display_name = discord.ui.TextInput(label="Display name / callsign", required=True, max_length=80)
    policy_summary = discord.ui.TextInput(label="Policy summary", required=True, style=discord.TextStyle.paragraph, max_length=1200)
    contact_instructions = discord.ui.TextInput(label="Contact instructions", required=True, style=discord.TextStyle.paragraph, max_length=1200)

    def __init__(self, panel: "SetupPanelView" | None = None, *, db=None, existing_profile: dict[str, Any] | None = None):
        super().__init__(title="Insurer Profile")
        self.panel = panel
        self.db = db if db is not None else (panel.db if panel is not None else None)
        existing = existing_profile or {}
        self.display_name.default = str(existing.get("display_name") or "")[:80]
        self.policy_summary.default = str(existing.get("coverage_summary") or "")[:1200]
        self.contact_instructions.default = str(existing.get("pricing_text") or "")[:1200]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.db is None:
            await interaction.response.send_message(
                embed=create_error_embed("Setup unavailable", "Could not save insurer profile right now."),
                ephemeral=True,
            )
            return
        repo = ApplicationsRepository(self.db.pool)
        existing = await repo.get_insurer_profile(guild_id=interaction.guild_id, user_id=interaction.user.id)
        profile = await repo.upsert_insurer_profile(
            guild_id=interaction.guild_id,
            user_id=interaction.user.id,
            data={
                "display_name": str(self.display_name.value).strip(),
                "coverage_summary": str(self.policy_summary.value).strip(),
                "pricing_text": str(self.contact_instructions.value).strip(),
                "rules_exclusions": str((existing or {}).get("rules_exclusions") or "Not provided.").strip(),
                "response_time_text": (existing or {}).get("response_time_text"),
                "contact_notes": (existing or {}).get("contact_notes"),
                "image_url": (existing or {}).get("image_url"),
                "activation_delay_minutes": int((existing or {}).get("activation_delay_minutes") or 0),
                "coverage_duration_minutes": int((existing or {}).get("coverage_duration_minutes") or 120),
                "categories": (existing or {}).get("categories") or [],
            },
        )
        await interaction.response.send_message(
            embed=create_success_embed("Insurer profile saved", "Now choose your insurer categories."),
            view=InsurerCategoryPickerView(db=self.db, guild_id=interaction.guild_id, user_id=interaction.user.id, current_categories=profile.get("categories") or []),
            ephemeral=True,
        )


class InsurerCategoryMultiSelect(discord.ui.Select):
    def __init__(self, *, current_categories: list[str]):
        defaults = set(normalize_insurer_categories(current_categories))
        options = [
            discord.SelectOption(label=category, value=category, default=(category in defaults))
            for category in INSURER_CATEGORIES
        ]
        super().__init__(
            placeholder="Select insurer categories",
            min_values=0,
            max_values=len(INSURER_CATEGORIES),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert isinstance(self.view, InsurerCategoryPickerView)
        self.view.selected_categories = normalize_insurer_categories(self.values)
        await interaction.response.defer()


class InsurerCategoryPickerView(discord.ui.View):
    def __init__(self, *, db, guild_id: int, user_id: int, current_categories: list[str]):
        super().__init__(timeout=300)
        self.db = db
        self.guild_id = guild_id
        self.user_id = user_id
        self.selected_categories = normalize_insurer_categories(current_categories)
        self.add_item(InsurerCategoryMultiSelect(current_categories=self.selected_categories))

    @discord.ui.button(label="Save categories", style=discord.ButtonStyle.success)
    async def save_categories(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        repo = ApplicationsRepository(self.db.pool)
        existing = await repo.get_insurer_profile(guild_id=self.guild_id, user_id=self.user_id)
        if not existing:
            await interaction.response.send_message(
                embed=create_error_embed("Profile missing", "Save your insurer profile text first with /insurer_profile."),
                ephemeral=True,
            )
            return

        await repo.upsert_insurer_profile(
            guild_id=self.guild_id,
            user_id=self.user_id,
            data={
                "display_name": existing.get("display_name") or "Insurer",
                "coverage_summary": existing.get("coverage_summary") or "Not provided.",
                "pricing_text": existing.get("pricing_text") or "Not provided.",
                "rules_exclusions": existing.get("rules_exclusions") or "Not provided.",
                "response_time_text": existing.get("response_time_text"),
                "contact_notes": existing.get("contact_notes"),
                "image_url": existing.get("image_url"),
                "activation_delay_minutes": int(existing.get("activation_delay_minutes") or 0),
                "coverage_duration_minutes": int(existing.get("coverage_duration_minutes") or 120),
                "categories": self.selected_categories,
            },
        )
        await interaction.response.send_message(
            embed=create_success_embed(
                "Categories saved",
                f"Saved categories: {', '.join(self.selected_categories) if self.selected_categories else 'None'}",
            ),
            ephemeral=True,
        )
class WelcomeView(BackView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_item(ChannelSelect(self.panel, "welcome_channel_id", "Welcome Channel"))

    @discord.ui.button(label="Toggle Welcome", style=discord.ButtonStyle.primary)
    async def toggle_welcome(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            if not await _ensure_welcome_permission(interaction, self.settings):
                return
            next_enabled = not bool(self.settings.get("welcome_enabled"))
            updates = {"welcome_enabled": next_enabled}
            if next_enabled and not (self.settings.get("welcome_message_template") or "").strip():
                updates["welcome_message_template"] = DEFAULT_WELCOME_TEMPLATE
            await self.panel.save_changes(interaction, updates)
        except Exception as error:
            await _respond_callback_error(interaction, error)

    @discord.ui.button(label="Edit Welcome Message", style=discord.ButtonStyle.secondary)
    async def edit_welcome(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            if not await _ensure_welcome_permission(interaction, self.settings):
                return
            current_template = self.settings.get("welcome_message_template") or DEFAULT_WELCOME_TEMPLATE
            await interaction.response.send_modal(TemplateModal(self.panel, "welcome_message_template", "Welcome message template", current_template))
        except Exception as error:
            await _respond_callback_error(interaction, error)

    @discord.ui.button(label="Preview Welcome Message", style=discord.ButtonStyle.success)
    async def preview_welcome(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            if not await _ensure_welcome_permission(interaction, self.settings):
                return
            if not isinstance(interaction.user, discord.Member) or not interaction.guild:
                await interaction.response.send_message(embed=create_error_embed("Preview unavailable", "This action can only be used in a server."), ephemeral=True)
                return
            rules_channel = detect_rules_channel(interaction.guild)
            template = self.settings.get("welcome_message_template") or DEFAULT_WELCOME_TEMPLATE
            preview_message = render_welcome_template(template, interaction.user, rules_channel)
            preview_embed = create_info_embed("Welcome Preview", preview_message)
            preview_embed.add_field(
                name="Rules channel",
                value=rules_channel.mention if rules_channel else "the rules channel",
                inline=False,
            )
            await interaction.response.send_message(embed=preview_embed, ephemeral=True)
        except Exception as error:
            await _respond_callback_error(interaction, error)




class DefaultMaxSlotsModal(discord.ui.Modal):
    max_slots = discord.ui.TextInput(label="Default max slots (1-7)", required=True, max_length=1)

    def __init__(self, panel: "SetupPanelView", current: int | None):
        super().__init__(title="Set 99k Default Max Slots")
        self.panel = panel
        self.max_slots.default = str(current or 5)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.max_slots.value).strip()
        if not raw.isdigit() or not 1 <= int(raw) <= 7:
            await interaction.response.send_message(embed=create_error_embed("Invalid value", "Default max slots must be between 1 and 7."), ephemeral=True)
            return
        await self.panel.save_changes(interaction, {"default_max_slots": int(raw)})



class FeatureTogglesView(BackView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._sync_99k_button_label()

    def _sync_99k_button_label(self) -> None:
        is_disabled = bool(self.settings.get("disable_99k_announcements", False))
        label = "99k ping announcements: OFF (click to enable)" if is_disabled else "99k ping announcements: ON (click to disable)"
        for child in self.children:
            if isinstance(child, discord.ui.Button) and getattr(child.callback, "__name__", "") == "toggle_99k_announcements":
                child.label = label
                break

    @discord.ui.button(label="Toggle Auto Complete", style=discord.ButtonStyle.primary)
    async def toggle_auto_complete(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await self.panel.save_changes(interaction, {"auto_complete_enabled": not bool(self.settings.get("auto_complete_enabled", True))})
        except Exception as error:
            await _respond_callback_error(interaction, error)

    @discord.ui.button(label="Toggle Raffle Announcement", style=discord.ButtonStyle.primary)
    async def toggle_raffle_announce(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await self.panel.save_changes(interaction, {"raffle_announce_enabled": not bool(self.settings.get("raffle_announce_enabled", True))})
        except Exception as error:
            await _respond_callback_error(interaction, error)

    @discord.ui.button(label="99k ping announcements: OFF (click to enable)", style=discord.ButtonStyle.primary, row=1)
    async def toggle_99k_announcements(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            disable_announcements = bool(self.settings.get("disable_99k_announcements", False))
            await self.panel.save_changes(interaction, {"disable_99k_announcements": not disable_announcements})
        except Exception as error:
            await _respond_callback_error(interaction, error)

    @discord.ui.button(label="Set Reservation Timeout", style=discord.ButtonStyle.secondary)
    async def reservation_timeout(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await interaction.response.send_modal(TimeoutModal(self.panel, self.settings.get("reservation_timeout_minutes")))
        except Exception as error:
            await _respond_callback_error(interaction, error)

    @discord.ui.button(label="Set 99k Default Max Slots", style=discord.ButtonStyle.secondary)
    async def default_slots(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await interaction.response.send_modal(DefaultMaxSlotsModal(self.panel, self.settings.get("default_max_slots")))
        except Exception as error:
            await _respond_callback_error(interaction, error)


class HostTaxConfigModal(discord.ui.Modal):
    recipient_torn_id = discord.ui.TextInput(label="Recipient Torn ID", required=False, max_length=12, placeholder="4051872")
    amount_value = discord.ui.TextInput(label="Amount", required=False, max_length=20, placeholder="1")

    def __init__(self, panel: "SetupPanelView", payment_option: str):
        super().__init__(title="99k Host Tax")
        self.panel = panel
        self.payment_option = payment_option
        self.amount_value.label = "Cash amount" if payment_option == "cash" else "Item quantity"
        self.amount_value.placeholder = "5000000" if payment_option == "cash" else "1"
        self.recipient_torn_id.default = str(panel.settings.get("host_tax_recipient_torn_id") or "")
        if payment_option == "cash":
            self.amount_value.default = str(panel.settings.get("host_tax_cash_amount") or "")
        else:
            self.amount_value.default = str(panel.settings.get("host_tax_quantity") or "")

    async def on_submit(self, interaction: discord.Interaction):
        enabled = bool(self.panel.settings.get("host_tax_enabled"))
        recipient_raw = str(self.recipient_torn_id.value or "").strip()
        amount_raw = str(self.amount_value.value or "").strip()

        if not enabled:
            await self.panel.save_changes(
                interaction,
                {
                    "host_tax_recipient_torn_id": int(recipient_raw) if recipient_raw.isdigit() else None,
                    "host_tax_cash_amount": int(amount_raw) if amount_raw.isdigit() else None,
                    "host_tax_quantity": int(amount_raw) if amount_raw.isdigit() else None,
                },
            )
            return

        if not recipient_raw.isdigit():
            await interaction.response.send_message(embed=create_error_embed("Invalid recipient", "Recipient Torn ID must be numbers only (example: 4051872)."), ephemeral=True)
            return
        if not amount_raw.isdigit() or int(amount_raw) < 1:
            example = "5000000" if self.payment_option == "cash" else "1"
            await interaction.response.send_message(embed=create_error_embed("Invalid amount", f"Enter a number of at least 1 (example: {example})."), ephemeral=True)
            return

        amount = int(amount_raw)
        updates = {
            "host_tax_recipient_torn_id": int(recipient_raw),
        }
        if self.payment_option == "cash":
            updates.update(
                {
                    "host_tax_type": "cash",
                    "host_tax_item_id": None,
                    "host_tax_quantity": None,
                    "host_tax_cash_amount": amount,
                }
            )
        else:
            item_id = 206 if self.payment_option == "xanax" else 366
            updates.update(
                {
                    "host_tax_type": "item",
                    "host_tax_item_id": item_id,
                    "host_tax_quantity": amount,
                    "host_tax_cash_amount": None,
                }
            )
        await self.panel.save_changes(interaction, updates)


class HostTaxView(BackView):
    @discord.ui.button(label="Toggle Enabled", style=discord.ButtonStyle.primary)
    async def toggle_enabled(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            next_enabled = not bool(self.settings.get("host_tax_enabled"))
            await self.panel.save_changes(interaction, {"host_tax_enabled": next_enabled})
        except Exception as error:
            await _respond_callback_error(interaction, error)

    @discord.ui.button(label="Set Torn Cash", style=discord.ButtonStyle.secondary)
    async def set_cash(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await interaction.response.send_modal(HostTaxConfigModal(self.panel, "cash"))
        except Exception as error:
            await _respond_callback_error(interaction, error)

    @discord.ui.button(label="Set Xanax 💊", style=discord.ButtonStyle.secondary)
    async def set_xanax(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await interaction.response.send_modal(HostTaxConfigModal(self.panel, "xanax"))
        except Exception as error:
            await _respond_callback_error(interaction, error)

    @discord.ui.button(label="Set Erotic DvD 📀", style=discord.ButtonStyle.secondary)
    async def set_dvd(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await interaction.response.send_modal(HostTaxConfigModal(self.panel, "dvd"))
        except Exception as error:
            await _respond_callback_error(interaction, error)


class TestView(BackView):
    @discord.ui.button(label="Test session announcement", style=discord.ButtonStyle.success)
    async def test_session(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            channel_id = self.settings.get("jump_99k_channel_id") or self.settings.get("announce_channel_id")
            channel = interaction.guild.get_channel(channel_id or 0)
            if channel is None:
                await interaction.response.send_message(embed=create_error_embed("Missing channel", "Configure jump channel first."), ephemeral=True)
                return
            me = self.panel._resolve_bot_member(interaction)
            if me is None:
                await interaction.response.send_message(embed=create_error_embed("Bot member unavailable", "Unable to validate my permissions right now. Please try again."), ephemeral=True)
                return
            missing = _missing_channel_perms(channel, me)
            if missing:
                await interaction.response.send_message(embed=create_error_embed("Missing permissions", f"Missing in {channel.mention}: **{', '.join(missing)}**."), ephemeral=True)
                return
            template = "Session test for {guild} in {channel} at {timestamp}."
            await channel.send(embed=create_info_embed("Session Announcement Test", _render_template(template, interaction)))
            await interaction.response.send_message(embed=create_success_embed("Test sent", f"Posted in {channel.mention}."), ephemeral=True)
        except Exception as error:
            await _respond_callback_error(interaction, error)

    @discord.ui.button(label="Test welcome", style=discord.ButtonStyle.success)
    async def test_welcome(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            channel_id = self.settings.get("welcome_channel_id") or self.settings.get("announce_channel_id")
            channel = interaction.guild.get_channel(channel_id or 0)
            if channel is None:
                await interaction.response.send_message(embed=create_error_embed("Missing channel", "Configure welcome channel first."), ephemeral=True)
                return
            me = self.panel._resolve_bot_member(interaction)
            if me is None:
                await interaction.response.send_message(embed=create_error_embed("Bot member unavailable", "Unable to validate my permissions right now. Please try again."), ephemeral=True)
                return
            missing = _missing_channel_perms(channel, me)
            if missing:
                await interaction.response.send_message(embed=create_error_embed("Missing permissions", f"Missing in {channel.mention}: **{', '.join(missing)}**."), ephemeral=True)
                return
            template = self.settings.get("welcome_message_template") or "Welcome {mention} to {guild}!"
            await channel.send(embed=create_success_embed("Welcome Test", _render_template(template, interaction)))
            await interaction.response.send_message(embed=create_success_embed("Test sent", f"Posted in {channel.mention}."), ephemeral=True)
        except Exception as error:
            await _respond_callback_error(interaction, error)


class EngagementLevelupChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: SetupPanelView):
        super().__init__(placeholder="Set level-up announcement channel", min_values=0, max_values=1, row=0)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        try:
            selected = self.values[0] if self.values else None
            channel_id = getattr(selected, "id", None)
            await self.panel.save_engagement_changes(interaction, {"levelup_channel_id": channel_id})
        except Exception as error:
            await _respond_callback_error(interaction, error)


class IgnoredChannelsSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: SetupPanelView):
        super().__init__(placeholder="Ignored channels", min_values=0, max_values=25, row=0)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        try:
            await self.panel.save_engagement_changes(interaction, {"ignored_channel_ids_json": [int(v.id) for v in self.values]})
        except Exception as error:
            await _respond_callback_error(interaction, error)


class IgnoredCategoriesSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: SetupPanelView):
        super().__init__(placeholder="Ignored categories", min_values=0, max_values=25, row=1, channel_types=[discord.ChannelType.category])
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        try:
            await self.panel.save_engagement_changes(interaction, {"ignored_category_ids_json": [int(v.id) for v in self.values]})
        except Exception as error:
            await _respond_callback_error(interaction, error)


class IgnoredRolesSelect(discord.ui.RoleSelect):
    def __init__(self, panel: SetupPanelView):
        super().__init__(placeholder="Ignored roles", min_values=0, max_values=25, row=2)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        try:
            await self.panel.save_engagement_changes(interaction, {"ignored_role_ids_json": [int(v.id) for v in self.values]})
        except Exception as error:
            await _respond_callback_error(interaction, error)


class EngagementIgnoredView(BackView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_item(IgnoredChannelsSelect(self.panel))
        self.add_item(IgnoredCategoriesSelect(self.panel))
        self.add_item(IgnoredRolesSelect(self.panel))


class EngagementCoreView(BackView):
    @discord.ui.button(label="Toggle interaction XP", style=discord.ButtonStyle.primary, row=1)
    async def toggle_enabled(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.panel.save_engagement_changes(interaction, {"enabled": not bool(self.panel.engagement_settings.get("enabled"))})

    @discord.ui.button(label="Toggle leaderboard enabled", style=discord.ButtonStyle.primary, row=1)
    async def toggle_leaderboard(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.panel.save_engagement_changes(interaction, {"leaderboard_enabled": not bool(self.panel.engagement_settings.get("leaderboard_enabled", True))})

    @discord.ui.button(label="Toggle profile cards enabled", style=discord.ButtonStyle.primary, row=2)
    async def toggle_profile_cards(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.panel.save_engagement_changes(interaction, {"profile_cards_enabled": not bool(self.panel.engagement_settings.get("profile_cards_enabled", True))})

    @discord.ui.button(label="Next →", style=discord.ButtonStyle.secondary, row=4)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Engagement Setup", "Chat/Voice/Reaction settings"), EngagementChatVoiceReactionView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_item(EngagementLevelupChannelSelect(self.panel))


class EngagementChatVoiceReactionView(BackView):
    @discord.ui.button(label="Toggle message XP", style=discord.ButtonStyle.primary, row=0)
    async def toggle_message_xp(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.panel.save_engagement_changes(interaction, {"message_xp_enabled": not bool(self.panel.engagement_settings.get("message_xp_enabled", True))})

    @discord.ui.button(label="Toggle reaction XP", style=discord.ButtonStyle.primary, row=1)
    async def toggle_reaction_xp(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.panel.save_engagement_changes(interaction, {"reaction_xp_enabled": not bool(self.panel.engagement_settings.get("reaction_xp_enabled", True))})

    @discord.ui.button(label="Toggle voice XP", style=discord.ButtonStyle.primary, row=2)
    async def toggle_voice_xp(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.panel.save_engagement_changes(interaction, {"voice_xp_enabled": not bool(self.panel.engagement_settings.get("voice_xp_enabled", True))})

    @discord.ui.button(label="Manage ignored channels/categories/roles", style=discord.ButtonStyle.secondary, row=3)
    async def manage_ignored(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Ignored Targets", "Ignored channels/categories/roles for engagement XP."), EngagementIgnoredView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="Next →", style=discord.ButtonStyle.secondary, row=4)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Engagement Setup", "Event XP settings"), EngagementEventXPView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=4)
    async def custom_back(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Engagement Setup", "Core engagement settings"), EngagementCoreView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))


class PaidRaffleXPModal(discord.ui.Modal):
    base = discord.ui.TextInput(label="Paid raffle base XP", required=True, default="15", max_length=4)
    per_ticket = discord.ui.TextInput(label="Paid raffle XP per ticket", required=True, default="2", max_length=4)
    cap = discord.ui.TextInput(label="Paid raffle XP cap", required=True, default="50", max_length=4)

    def __init__(self, panel: SetupPanelView):
        super().__init__(title="Paid raffle XP config")
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self.panel.save_engagement_changes(interaction, {
                "paid_raffle_purchase_xp_base": _parse_friendly_int(str(self.base.value), label="Paid raffle base XP"),
                "paid_raffle_purchase_xp_per_ticket": _parse_friendly_int(str(self.per_ticket.value), label="Paid raffle XP per ticket"),
                "paid_raffle_purchase_xp_cap": _parse_friendly_int(str(self.cap.value), label="Paid raffle XP cap"),
            })
        except ValueError as error:
            await interaction.response.send_message(embed=create_error_embed("Invalid value", str(error)), ephemeral=True)


class JumpXPModal(discord.ui.Modal):
    value = discord.ui.TextInput(label="XP", required=True, default="40", max_length=4)

    def __init__(self, panel: SetupPanelView, key: str, title: str, default: int):
        super().__init__(title=title)
        self.panel = panel
        self.key = key
        self.value.default = str(default)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self.panel.save_engagement_changes(interaction, {self.key: _parse_friendly_int(str(self.value.value), label=self.value.label or self.title)})
        except ValueError as error:
            await interaction.response.send_message(embed=create_error_embed("Invalid value", str(error)), ephemeral=True)


class EngagementRewardAmountModal(discord.ui.Modal):
    value = discord.ui.TextInput(label="Amount", required=True, max_length=10)

    def __init__(self, panel: SetupPanelView, *, key: str, title: str, label: str, default: int):
        super().__init__(title=title)
        self.panel = panel
        self.key = key
        self.value.label = label
        self.value.default = f"{int(default):,}"

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self.panel.save_engagement_changes(interaction, {self.key: _parse_friendly_int(str(self.value.value), label=self.value.label)})
        except ValueError as error:
            await interaction.response.send_message(embed=create_error_embed("Invalid value", str(error)), ephemeral=True)


class EngagementRewardConfigView(BackView):
    REWARD_FIELDS = [
        ("level_up_coin_reward", "Level-up Coin Reward"),
        ("level_up_hjd_reward", "Level-up HJD Reward"),
        ("paid_raffle_purchase_coin_reward", "Raffle Purchase Coin Reward"),
        ("paid_raffle_purchase_hjd_reward", "Raffle Purchase HJD Reward"),
        ("jump_purchase_coin_reward", "Jump Purchase Coin Reward"),
        ("jump_purchase_hjd_reward", "Jump Purchase HJD Reward"),
        ("jump_completion_coin_reward", "Jump Completion Coin Reward"),
        ("jump_completion_hjd_reward", "Jump Completion HJD Reward"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        for index, (key, label) in enumerate(self.REWARD_FIELDS):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, row=index // 2)
            async def _callback(interaction: discord.Interaction, *, reward_key=key, reward_label=label):
                await interaction.response.send_modal(
                    EngagementRewardAmountModal(
                        self.panel,
                        key=reward_key,
                        title=reward_label,
                        label=reward_label,
                        default=int(self.panel.engagement_settings.get(reward_key) or 0),
                    )
                )
            button.callback = _callback
            self.add_item(button)

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=4)
    async def custom_back(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Engagement Setup", "Event XP settings"), EngagementEventXPView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

class EngagementEventXPView(BackView):
    @discord.ui.button(label="Paid raffle purchase XP config", style=discord.ButtonStyle.primary, row=0)
    async def paid_raffle_config(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(PaidRaffleXPModal(self.panel))

    @discord.ui.button(label="Paid jump purchase XP config", style=discord.ButtonStyle.primary, row=1)
    async def jump_purchase_config(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(JumpXPModal(self.panel, "jump_purchase_xp", "Paid jump purchase XP", int(self.panel.engagement_settings.get("jump_purchase_xp") or 40)))

    @discord.ui.button(label="Jump completion XP config", style=discord.ButtonStyle.primary, row=2)
    async def jump_completion_config(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(JumpXPModal(self.panel, "jump_completion_xp", "Jump completion XP", int(self.panel.engagement_settings.get("jump_completion_xp") or 75)))

    @discord.ui.button(label="Reward amount controls", style=discord.ButtonStyle.primary, row=3)
    async def reward_amount_controls(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, build_section_embed("Reward Amounts", "Adjust Coin and HJD payouts for the activities this bot already rewards.", [
            f"Level-up Coin Reward: **{int(self.panel.engagement_settings.get('level_up_coin_reward') or 1):,}**",
            f"Level-up HJD Reward: **{int(self.panel.engagement_settings.get('level_up_hjd_reward') or 100):,}**",
            f"Raffle Purchase Coin Reward: **{int(self.panel.engagement_settings.get('paid_raffle_purchase_coin_reward') or 0):,}**",
            f"Raffle Purchase HJD Reward: **{int(self.panel.engagement_settings.get('paid_raffle_purchase_hjd_reward') or 0):,}**",
            f"Jump Purchase Coin Reward: **{int(self.panel.engagement_settings.get('jump_purchase_coin_reward') or 0):,}**",
            f"Jump Purchase HJD Reward: **{int(self.panel.engagement_settings.get('jump_purchase_hjd_reward') or 0):,}**",
            f"Jump Completion Coin Reward: **{int(self.panel.engagement_settings.get('jump_completion_coin_reward') or 0):,}**",
            f"Jump Completion HJD Reward: **{int(self.panel.engagement_settings.get('jump_completion_hjd_reward') or 0):,}**",
        ]), EngagementRewardConfigView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="Toggle auto-entry giveaways enabled", style=discord.ButtonStyle.secondary, row=3)
    async def auto_entry_toggle(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.panel.save_engagement_changes(interaction, {"auto_entry_giveaways_enabled": not bool(self.panel.engagement_settings.get("auto_entry_giveaways_enabled", True))})

    @discord.ui.button(label="Next →", style=discord.ButtonStyle.secondary, row=4)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Engagement Setup", "Reward roles"), EngagementRolesView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=4)
    async def custom_back(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Engagement Setup", "Chat/Voice/Reaction settings"), EngagementChatVoiceReactionView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))


async def send_setup_panel(interaction: discord.Interaction, db) -> None:
    repo = GuildSettingsRepository(db)
    if interaction.guild is not None:
        await repo.ensure_guild_exists(interaction.guild.id)
    else:
        await repo.ensure_guild_exists(interaction.guild_id)
    allowed, settings = await ensure_setup_permission(interaction, db)
    if not allowed:
        embed = create_error_embed(
            "Setup permission denied",
            "You must be the guild owner, have Administrator, have Manage Guild, or have a configured setup admin role.",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    engagement_settings = await EngagementRepository(db.pool).get_or_create_guild_settings(interaction.guild.id)
    panel = SetupPanelView(owner_id=interaction.user.id, db=db, settings=settings, guild=interaction.guild, engagement_settings=engagement_settings)
    panel.store_settings = await StoreRepository(db.pool).get_or_create_guild_settings(interaction.guild.id)
    await interaction.response.send_message(embed=panel._build_embed(), view=panel, ephemeral=True)


class DebugMemberEngagementModal(discord.ui.Modal, title="Debug Member Engagement"):
    member_id = discord.ui.TextInput(label="Member mention or ID")

    def __init__(self, panel: SetupPanelView):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        match = re.search(r"(\d{15,25})", str(self.member_id.value))
        member_id = int(match.group(1)) if match else interaction.user.id
        repo = EngagementRepository(self.panel.db.pool)
        token_repo = PrizeTokensRepository(self.panel.db.pool)
        profile = await repo.get_or_create_profile(interaction.guild.id, member_id)
        message_state = await repo.get_message_state(interaction.guild.id, member_id)
        events = await repo.get_recent_event_rows(interaction.guild.id, member_id, limit=5)
        txs = await token_repo.get_recent_transactions(interaction.guild.id, member_id, limit=5)
        await interaction.response.send_message(
            f"Profile: {profile}\nMessage state: {message_state}\nRecent events: {events}\nRecent token tx: {txs}",
            ephemeral=True,
        )


class RebuildMemberProfileModal(discord.ui.Modal, title="Rebuild Member Profile"):
    member_id = discord.ui.TextInput(label="Member mention or ID")

    def __init__(self, panel: SetupPanelView):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        match = re.search(r"(\d{15,25})", str(self.member_id.value))
        if not match:
            await interaction.response.send_message("Enter a valid member mention or ID.", ephemeral=True)
            return
        member_id = int(match.group(1))
        repo = EngagementRepository(self.panel.db.pool)
        rebuilt = await repo.rebuild_profile_from_ledgers(interaction.guild.id, member_id)
        await repo.update_level(interaction.guild.id, member_id, level_from_total_xp(int(rebuilt.get("xp_total") or 0)))
        service = RoleRewardService(repo)
        await service.seed_default_ladders_if_missing(interaction.guild.id)
        await service.ensure_reward_roles(interaction.guild)
        member = interaction.guild.get_member(member_id)
        sync_result = {"granted": 0, "removed": 0, "failed": 0}
        if member is not None:
            sync_result = await service.sync_member_roles(interaction.guild, member, rebuilt)
        await interaction.response.send_message(f"Rebuilt profile for <@{member_id}>. Role sync: {sync_result}", ephemeral=True)


class ReverseEventModal(discord.ui.Modal, title="Reverse Event"):
    dedupe_key = discord.ui.TextInput(label="Dedupe key", max_length=200)

    def __init__(self, panel: SetupPanelView):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        repo = EngagementRepository(self.panel.db.pool)
        row = await repo.reverse_event_by_dedupe_key(interaction.guild.id, str(self.dedupe_key.value).strip())
        if not row:
            await interaction.response.send_message("No unreversed event found for that dedupe key.", ephemeral=True)
            return
        user_id = int(row.get("user_id") or 0)
        rebuilt = await repo.rebuild_profile_from_ledgers(interaction.guild.id, user_id)
        await repo.update_level(interaction.guild.id, user_id, level_from_total_xp(int(rebuilt.get("xp_total") or 0)))
        service = RoleRewardService(repo)
        await service.seed_default_ladders_if_missing(interaction.guild.id)
        await service.ensure_reward_roles(interaction.guild)
        member = interaction.guild.get_member(user_id)
        if member is not None:
            await service.sync_member_roles(interaction.guild, member, rebuilt)
        await interaction.response.send_message(f"Reversed event for <@{user_id}> and rebuilt profile.", ephemeral=True)


class EngagementRolesView(BackView):
    @discord.ui.button(label="Create/Repair Reward Roles", style=discord.ButtonStyle.primary, row=0)
    async def create_repair_reward_roles(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _maybe_defer_setup_response(interaction)
        repo = EngagementRepository(self.db.pool)
        service = RoleRewardService(repo)
        await service.seed_default_ladders_if_missing(interaction.guild.id)
        created, repaired = await service.ensure_reward_roles(interaction.guild)
        status = await service.rewards_status(interaction.guild.id, interaction.guild)
        await _send_setup_response(
            interaction,
            f"Reward roles checked. Created: {created}. Repaired: {repaired}. Linked and present: {status['linked']}/{status['total']}. Missing: {status['missing']}",
        )

    @discord.ui.button(label="Sync Reward Roles", style=discord.ButtonStyle.primary, row=1)
    async def sync_reward_roles(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _maybe_defer_setup_response(interaction)
        repo = EngagementRepository(self.db.pool)
        service = RoleRewardService(repo)
        await service.seed_default_ladders_if_missing(interaction.guild.id)
        created, repaired = await service.ensure_reward_roles(interaction.guild)
        profiles = await repo.list_profiles_for_guild(interaction.guild.id)
        totals = {"granted": 0, "removed": 0, "failed": 0}
        for profile in profiles:
            member = interaction.guild.get_member(int(profile["user_id"]))
            if member is None:
                continue
            result = await service.sync_member_roles(interaction.guild, member, profile)
            for key in totals:
                totals[key] += int(result.get(key, 0))
        await _send_setup_response(
            interaction,
            f"Reward role sync completed. Created: {created}. Repaired: {repaired}. Member sync totals: {totals}",
        )

    @discord.ui.button(label="View Engagement Config", style=discord.ButtonStyle.primary, row=2)
    async def view_engagement_config(self, interaction: discord.Interaction, _: discord.ui.Button):
        s = await EngagementRepository(self.db.pool).get_or_create_guild_settings(interaction.guild.id)
        await interaction.response.send_message(
            "\n".join([
                "**Engagement Config**",
                f"Enabled: `{bool(s.get('enabled'))}`",
                f"Level-up channel: `{s.get('levelup_channel_id') or 'Not set'}`",
                f"Leaderboards enabled: `{bool(s.get('leaderboard_enabled'))}`",
                f"Profile cards enabled: `{bool(s.get('profile_cards_enabled'))}`",
                f"Message XP: `{bool(s.get('message_xp_enabled'))}`",
                f"Reaction XP: `{bool(s.get('reaction_xp_enabled'))}`",
                f"Voice XP: `{bool(s.get('voice_xp_enabled'))}`",
                f"Ignored channels: `{s.get('ignored_channel_ids_json') or []}`",
                f"Ignored categories: `{s.get('ignored_category_ids_json') or []}`",
                f"Ignored roles: `{s.get('ignored_role_ids_json') or []}`",
            ]),
            ephemeral=True,
        )

    @discord.ui.button(label="View Reward Role Status", style=discord.ButtonStyle.primary, row=3)
    async def view_reward_role_status(self, interaction: discord.Interaction, _: discord.ui.Button):
        service = RoleRewardService(EngagementRepository(self.db.pool))
        status = await service.rewards_status(interaction.guild.id, interaction.guild)
        await interaction.response.send_message(
            f"Configured roles: {status['total']}\nLinked and present: {status['linked']}\nMissing/deleted: {status['missing']}",
            ephemeral=True,
        )

    @discord.ui.button(label="Next →", style=discord.ButtonStyle.secondary, row=4)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Engagement Setup", "Maintenance actions"), EngagementMaintenanceView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=4)
    async def custom_back(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Engagement Setup", "Event XP settings"), EngagementEventXPView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))


# Backwards-compatible alias for older imports/tests that referenced the original page name.
EngagementRolesStatusView = EngagementRolesView


class EngagementMaintenanceView(BackView):
    @discord.ui.button(label="Debug Member Engagement", style=discord.ButtonStyle.primary, row=0)
    async def debug_member(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(DebugMemberEngagementModal(self.panel))

    @discord.ui.button(label="Rebuild Member Profile", style=discord.ButtonStyle.primary, row=1)
    async def rebuild_member(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(RebuildMemberProfileModal(self.panel))

    @discord.ui.button(label="Reverse Event", style=discord.ButtonStyle.primary, row=2)
    async def reverse_event(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(ReverseEventModal(self.panel))

    @discord.ui.button(label="Reseed Reward Definitions", style=discord.ButtonStyle.primary, row=3)
    async def reseed_reward_definitions(self, interaction: discord.Interaction, _: discord.ui.Button):
        repo = EngagementRepository(self.db.pool)
        await repo.seed_default_reward_ladders(interaction.guild.id)
        await interaction.response.send_message("Reward role definitions reseeded.", ephemeral=True)

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=4)
    async def custom_back(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Engagement Setup", "Roles and status"), EngagementRolesView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))


class StoreFulfillmentChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: SetupPanelView):
        super().__init__(placeholder="Fulfillment Channel", min_values=0, max_values=1, channel_types=[discord.ChannelType.text, discord.ChannelType.news], row=1)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0].id if self.values else None
        await self.panel.save_store_changes(interaction, {"fulfillment_channel_id": value})


class StoreChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: SetupPanelView):
        super().__init__(
            placeholder="Store Channel",
            min_values=0,
            max_values=1,
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            row=0,
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        if not self.values:
            await self.panel.save_store_changes(interaction, {"store_channel_id": None})
            return

        resolved = await self.panel._resolve_real_channel(interaction, self.values[0])
        if resolved is None:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "Channel unavailable",
                    "Couldn't resolve that channel. Try again, or check bot permissions.",
                ),
                ephemeral=True,
            )
            return

        me = self.panel._resolve_bot_member(interaction)
        if me is None:
            await interaction.response.send_message(
                embed=create_error_embed("Bot member unavailable", "Unable to validate my permissions right now. Please try again."),
                ephemeral=True,
            )
            return

        missing = _missing_channel_perms(resolved, me)
        if missing:
            mention = getattr(resolved, "mention", f"<#{resolved.id}>")
            await interaction.response.send_message(
                embed=create_error_embed("Missing permissions", f"Missing in {mention}: **{', '.join(missing)}**."),
                ephemeral=True,
            )
            return

        await self.panel.save_store_changes(interaction, {"store_channel_id": resolved.id})


class StoreSetupView(BackView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_item(StoreChannelSelect(self.panel))
        self.add_item(StoreFulfillmentChannelSelect(self.panel))

    @discord.ui.button(label="Store On/Off", style=discord.ButtonStyle.primary, row=2)
    async def toggle_store(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.panel.save_store_changes(interaction, {"enabled": not bool(self.panel.store_settings.get("enabled", False))})

    @discord.ui.button(label="Torn Items", style=discord.ButtonStyle.primary, row=3)
    async def toggle_torn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.panel.save_store_changes(interaction, {"torn_item_store_enabled": not bool(self.panel.store_settings.get("torn_item_store_enabled", True))})

    @discord.ui.button(label="Discord Perks", style=discord.ButtonStyle.primary, row=3)
    async def toggle_discord_perk(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.panel.save_store_changes(interaction, {"discord_perk_store_enabled": not bool(self.panel.store_settings.get("discord_perk_store_enabled", True))})

    @discord.ui.button(label="Next →", style=discord.ButtonStyle.secondary, row=4)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Store Setup", "Store admin actions"), StoreAdminPageView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))


class StoreAdminPageView(BackView):
    @discord.ui.button(label="Add Item", style=discord.ButtonStyle.primary, row=0)
    async def add_item(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(AddStoreItemModal(interaction.client.get_cog("StoreCog")))

    @discord.ui.button(label="Edit Item", style=discord.ButtonStyle.primary, row=1)
    async def edit_item(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(UpdateItemModal(interaction.client.get_cog("StoreCog")))

    @discord.ui.button(label="Restock Item", style=discord.ButtonStyle.primary, row=2)
    async def restock_item(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(StockAdjustModal(interaction.client.get_cog("StoreCog"), "Restock Item"))

    @discord.ui.button(label="Disable Item", style=discord.ButtonStyle.primary, row=3)
    async def disable_item(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(StockAdjustModal(interaction.client.get_cog("StoreCog"), "Disable Item", disable=True))

    @discord.ui.button(label="Next →", style=discord.ButtonStyle.secondary, row=4)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Store Setup", "Store fulfillment actions"), StoreFulfillmentPageView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=4)
    async def custom_back(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Store Setup", "Configure prize token store settings."), StoreSetupView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))


class StoreFulfillmentPageView(BackView):
    @discord.ui.button(label="View Pending Redemptions", style=discord.ButtonStyle.primary, row=0)
    async def pending(self, interaction: discord.Interaction, _: discord.ui.Button):
        rows = await StoreRepository(self.db.pool).list_pending_redemptions(interaction.guild_id, limit=10)
        desc = "\n".join(f"#{r['id']} · {r['item_name']} · <@{r['user_id']}> · {r['token_cost']}" for r in rows) or "No pending redemptions."
        await interaction.response.send_message(embed=discord.Embed(title="Pending Redemptions", description=desc), ephemeral=True)

    @discord.ui.button(label="Fulfill Redemption", style=discord.ButtonStyle.primary, row=1)
    async def fulfill(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(RedemptionActionModal(interaction.client.get_cog("StoreCog"), action="fulfill"))

    @discord.ui.button(label="Refund Redemption", style=discord.ButtonStyle.primary, row=2)
    async def refund(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(RedemptionActionModal(interaction.client.get_cog("StoreCog"), action="refund"))

    @discord.ui.button(label="View Store Status", style=discord.ButtonStyle.primary, row=3)
    async def view_status(self, interaction: discord.Interaction, _: discord.ui.Button):
        store_repo = StoreRepository(self.db.pool)
        settings = await store_repo.get_or_create_guild_settings(interaction.guild_id)
        pending = await store_repo.list_pending_redemptions(interaction.guild_id, limit=1)
        await interaction.response.send_message(
            f"Store enabled: `{bool(settings.get('enabled', False))}`\nStore channel: `{settings.get('store_channel_id') or 'Not set'}`\nTorn item store enabled: `{bool(settings.get('torn_item_store_enabled', True))}`\nDiscord perk store enabled: `{bool(settings.get('discord_perk_store_enabled', True))}`\nFulfillment channel: `{settings.get('fulfillment_channel_id') or 'Not set'}`\nPending redemptions: `{len(pending)}`",
            ephemeral=True,
        )

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=4)
    async def custom_back(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Store Setup", "Store admin actions"), StoreAdminPageView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))


def build_section_embed(title: str, description: str, status_lines: list[str]) -> discord.Embed:
    embed = create_info_embed(title, description)
    embed.add_field(name="Current Status", value="\n".join(status_lines[:8]), inline=False)
    return embed


class DashboardSectionView(BackView):
    @discord.ui.button(label="Home", style=discord.ButtonStyle.primary, row=4)
    async def home_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, self.panel._build_embed(), self.panel)


class SingleChannelConfigView(DashboardSectionView):
    def __init__(self, *, channel_key: str, placeholder: str, title: str, description: str, status_lines: list[str], channel_types: list[discord.ChannelType] | None = None, **kwargs):
        super().__init__(**kwargs)
        self.title = title
        self.description = description
        self.status_lines = status_lines
        self.remove_item(self.back_btn)
        self.add_item(ChannelSelect(self.panel, channel_key, placeholder, row=0, channel_types=channel_types))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=4)
    async def local_back_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, build_section_embed(self.title, self.description, self.status_lines), self._build_parent())

    def _build_parent(self):
        raise NotImplementedError


class ChannelsHubView(DashboardSectionView):
    @discord.ui.button(label="Main Channels", style=discord.ButtonStyle.primary, row=0)
    async def main_channels(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, build_section_embed("Main Channels", "Set the core posting channels.", [
            f"Jump Channel: **{self.panel._channel_status('jump_99k_channel_id')}**",
            f"Raffle Channel: **{self.panel._channel_status('raffle_purchase_channel_id')}**",
            f"Giveaway Channel: **{self.panel._channel_status('raffle_giveaway_purchase_channel_id')}**",
            f"Store Channel: **{self.panel.store_settings.get('store_channel_id') or 'Not set'}**",
        ]), ChannelsCoreView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="Welcome & Apps", style=discord.ButtonStyle.primary, row=0)
    async def welcome_apps(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, build_section_embed("Welcome & Apps", "Set welcome and application destinations.", [
            f"Welcome Channel: **{self.panel._channel_status('welcome_channel_id')}**",
            f"Applications Inbox: **{self.panel._channel_status('applications_admin_inbox_channel_id')}**",
            f"Applications Category: **{self.panel._channel_status('applications_category_id')}**",
        ]), ChannelsWelcomeAppsView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="Alerts & Access", style=discord.ButtonStyle.primary, row=0)
    async def alerts_access(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, build_section_embed("Alerts & Access", "Set supporting alert and access channels. Bank calculator and jewelry alerts now use stored Torn API keys from eligible admins in this server.", [
            f"Insurance Channel: **{self.panel._channel_status('insurance_channel_id')}**",
            f"Jewelry Alerts: **{self.panel._channel_status('jewelry_alert_channel_id')}**",
            f"Who Can Jump: **{self.panel._channel_status('who_can_jump_channel_id')}**",
        ]), ChannelsAlertsAccessView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))


class ChannelsCoreView(DashboardSectionView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_item(ChannelSelect(self.panel, "jump_99k_channel_id", "Jump Channel", row=0))
        self.add_item(ChannelSelect(self.panel, "raffle_purchase_channel_id", "Raffle Channel", row=1))
        self.add_item(ChannelSelect(self.panel, "raffle_giveaway_purchase_channel_id", "Giveaway Channel", row=2))
        self.add_item(StoreChannelSelect(self.panel))


class ChannelsWelcomeAppsView(DashboardSectionView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_item(ChannelSelect(self.panel, "welcome_channel_id", "Welcome Channel", row=0))
        self.add_item(ChannelSelect(self.panel, "applications_admin_inbox_channel_id", "Applications Inbox", row=1))
        self.add_item(ChannelSelect(self.panel, "applications_category_id", "Applications Category", row=2, channel_types=[discord.ChannelType.category]))


class ChannelsAlertsAccessView(DashboardSectionView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_item(ChannelSelect(self.panel, "insurance_channel_id", "Insurance Channel", row=0))
        self.add_item(ChannelSelect(self.panel, "jewelry_alert_channel_id", "Jewelry Alerts", row=1))
        self.add_item(ChannelSelect(self.panel, "who_can_jump_channel_id", "Who Can Jump", row=2))


class RolesHubView(DashboardSectionView):
    @discord.ui.button(label="Admin Roles", style=discord.ButtonStyle.primary, row=0)
    async def admin_roles(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, build_section_embed("Admin Roles", "Choose who can use admin tools.", [f"Admin Roles: **{self.panel._format_admin_roles()}**"]), RolesAdminView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="Host Role", style=discord.ButtonStyle.primary, row=0)
    async def host_role(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, build_section_embed("Host Role", "Set the raffle host role.", [f"Host Role: **{self.panel._role_status('raffle_host_role_id')}**"]), RolesHostView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="Insurance Role", style=discord.ButtonStyle.primary, row=0)
    async def insurance_role(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, build_section_embed("Insurance Role", "Set the insurer role.", [f"Insurance Role: **{self.panel._role_status('insurer_role_id')}**"]), RolesInsuranceView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="Role Health", style=discord.ButtonStyle.secondary, row=1)
    async def reward_roles(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(
            interaction,
            build_section_embed(
                "Role Health",
                "Review reward-role status and use Maintenance for repairs.",
                [f"Reward Roles: **{self.panel._reward_role_health()}**"],
            ),
            RewardRoleHealthView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel),
        )


class RolesAdminView(DashboardSectionView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_item(AdminRoleSelect(self.panel, row=0))


class RolesHostView(DashboardSectionView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_item(RaffleHostRoleSelect(self.panel, row=0))


class RolesInsuranceView(DashboardSectionView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_item(SingleRoleSelect(self.panel, "insurer_role_id", "Insurance Role", row=0))


class RewardRoleHealthView(DashboardSectionView):
    @discord.ui.button(label="View Status", style=discord.ButtonStyle.primary, row=0)
    async def view_status(self, interaction: discord.Interaction, _: discord.ui.Button):
        service = RoleRewardService(EngagementRepository(self.db.pool))
        status = await service.rewards_status(interaction.guild.id, interaction.guild)
        await interaction.response.send_message(
            f"Reward Roles: `{status['linked']}/{status['total']}` linked and present.\nMissing: `{status['missing']}`.",
            ephemeral=True,
        )

    @discord.ui.button(label="Open Maintenance", style=discord.ButtonStyle.secondary, row=0)
    async def open_maintenance(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(
            interaction,
            build_section_embed("Maintenance", "Check health, repairs, and rebuild tools.", self.panel._system_health_lines()),
            MaintenanceHubView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel),
        )


class EngagementHubView(DashboardSectionView):
    @discord.ui.button(label="XP & Levels", style=discord.ButtonStyle.primary, row=0)
    async def xp_levels(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("XP & Levels", "Adjust XP flow and level rewards."), EngagementChatVoiceReactionView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="Coins", style=discord.ButtonStyle.primary, row=0)
    async def coins(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Coins stay enabled through the current engagement system.", ephemeral=True)

    @discord.ui.button(label="HJD", style=discord.ButtonStyle.primary, row=0)
    async def hjd(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, build_section_embed("HJD", "Happy Jump Dollars are used by the store.", ["Currency: **HJD**", f"Storefront: **{'Live' if self.panel.store_settings.get('enabled', False) else 'Off'}**"]), StoreHubView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="Auto Entry Rules", style=discord.ButtonStyle.secondary, row=1)
    async def auto_entry(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Auto Entry Rules", "Adjust giveaway auto-entry settings."), EngagementEventXPView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))


class RafflesHubView(DashboardSectionView):
    @discord.ui.button(label="Panel Channel", style=discord.ButtonStyle.primary, row=0)
    async def panel_channel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, build_section_embed("Raffle Channel", "Choose where raffle panels are posted.", [f"Raffle Channel: **{self.panel._channel_status('raffle_purchase_channel_id')}**"]), RaffleChannelView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="Controls", style=discord.ButtonStyle.primary, row=0)
    async def controls(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Use `/raffle controls` for live raffle controls and entry handling.", ephemeral=True)

    @discord.ui.button(label="Recovery", style=discord.ButtonStyle.primary, row=0)
    async def recovery(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Raffle recovery stays in the live raffle controls so current recovery behavior is unchanged.", ephemeral=True)

    @discord.ui.button(label="Display", style=discord.ButtonStyle.secondary, row=1)
    async def display(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Raffle displays refresh through the existing raffle panel workflow.", ephemeral=True)


class RaffleChannelView(DashboardSectionView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_item(ChannelSelect(self.panel, "raffle_purchase_channel_id", "Raffle Channel", row=0))


class GiveawaysHubView(DashboardSectionView):
    @discord.ui.button(label="Giveaway Channel", style=discord.ButtonStyle.primary, row=0)
    async def giveaway_channel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, build_section_embed("Giveaway Channel", "Choose where giveaway panels are posted.", [f"Giveaway Channel: **{self.panel._channel_status('raffle_giveaway_purchase_channel_id')}**"]), GiveawayChannelView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="Auto Entry Rules", style=discord.ButtonStyle.primary, row=0)
    async def entry_rules(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Entry Rules", "Adjust button entry, weighted mode, and auto entry."), EngagementEventXPView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="Host Tools", style=discord.ButtonStyle.primary, row=0)
    async def host_tools(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Use the giveaway host controls on the live giveaway message for View Entries, End Giveaway, and refresh actions.", ephemeral=True)

    @discord.ui.button(label="Panel Tools", style=discord.ButtonStyle.secondary, row=1)
    async def refresh_panel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Repairs and refresh actions stay on the live giveaway host tools.", ephemeral=True)


class GiveawayChannelView(DashboardSectionView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_item(ChannelSelect(self.panel, "raffle_giveaway_purchase_channel_id", "Giveaway Channel", row=0))


class StoreHubView(DashboardSectionView):
    @discord.ui.button(label="Store Channel", style=discord.ButtonStyle.primary, row=0)
    async def store_channel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Store Channel", "Choose where the storefront is published."), StoreSetupView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="Inventory", style=discord.ButtonStyle.primary, row=0)
    async def inventory(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Inventory", "Add, edit, restock, or disable store items."), StoreAdminPageView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="Currency Info", style=discord.ButtonStyle.primary, row=1)
    async def currency_info(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("The store uses HJD as its currency.", ephemeral=True)


class ConfirmStorefrontRebuildView(DashboardSectionView):
    @discord.ui.button(label="Confirm Rebuild", style=discord.ButtonStyle.danger, row=0)
    async def confirm_rebuild(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _maybe_defer_setup_response(interaction)
        store_cog = interaction.client.get_cog("StoreCog") if getattr(interaction, "client", None) else None
        if store_cog is None:
            await _send_setup_response(interaction, "Store tools are unavailable right now.")
            return
        await store_cog.sync_storefront(interaction.guild)
        await _send_setup_response(interaction, "Storefront rebuilt.")


class WelcomeHubView(DashboardSectionView):
    @discord.ui.button(label="Welcome Channel", style=discord.ButtonStyle.primary, row=0)
    async def welcome_channel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Welcome Channel", "Choose where welcome messages are posted."), WelcomeView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="API Key Onboarding", style=discord.ButtonStyle.primary, row=0)
    async def api_key_onboarding(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("API key onboarding stays in the current welcome and application flow.", ephemeral=True)

    @discord.ui.button(label="Applications", style=discord.ButtonStyle.primary, row=0)
    async def applications(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Applications", "Choose where applications are received."), ChannelsViewApplications(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="Messages", style=discord.ButtonStyle.secondary, row=1)
    async def messages(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Messages", "Edit and preview the welcome message."), WelcomeView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))


class MaintenanceHubView(DashboardSectionView):
    @discord.ui.button(label="System Health", style=discord.ButtonStyle.primary, row=0)
    async def system_health(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(
            interaction,
            build_section_embed("System Health", "Quick health checks for your admin systems.", self.panel._system_health_lines()),
            SystemHealthView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel),
        )

    @discord.ui.button(label="Repair Tools", style=discord.ButtonStyle.primary, row=0)
    async def repair_tools(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Repair Tools", "Repair reward roles and store publishing safely."), RepairToolsView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="Rebuild Tools", style=discord.ButtonStyle.secondary, row=0)
    async def rebuild_tools(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Rebuild Tools", "Confirm before running rebuild actions."), RebuildToolsView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))

    @discord.ui.button(label="Debug", style=discord.ButtonStyle.primary, row=1)
    async def debug(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Debug", "Inspect member engagement data."), DebugToolsView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))


class SystemHealthView(DashboardSectionView):
    @discord.ui.button(label="Refresh Health", style=discord.ButtonStyle.primary, row=0)
    async def refresh_health(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(
            interaction,
            build_section_embed("System Health", "Quick health checks for your admin systems.", self.panel._system_health_lines()),
            SystemHealthView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel),
        )

    @discord.ui.button(label="Repair Roles", style=discord.ButtonStyle.primary, row=0)
    async def repair_roles(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = RepairToolsView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel)
        button = next(child for child in view.children if getattr(child, "label", None) == "Repair Roles")
        await button.callback(interaction)

    @discord.ui.button(label="Rebuild Storefront", style=discord.ButtonStyle.secondary, row=0)
    async def rebuild_storefront(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = RepairToolsView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel)
        button = next(child for child in view.children if getattr(child, "label", None) == "Rebuild Storefront")
        await button.callback(interaction)

    @discord.ui.button(label="Repair Panels", style=discord.ButtonStyle.secondary, row=1)
    async def repair_panels(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "Use live raffle and giveaway host tools to refresh or repair panels without changing setup behavior.",
            ephemeral=True,
        )


class RepairToolsView(DashboardSectionView):
    @discord.ui.button(label="Repair Roles", style=discord.ButtonStyle.primary, row=0)
    async def repair_roles(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = EngagementRolesView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel)
        button = next(child for child in view.children if getattr(child, 'label', None) == 'Create/Repair Reward Roles')
        await button.callback(interaction)

    @discord.ui.button(label="Sync Reward Roles", style=discord.ButtonStyle.primary, row=0)
    async def sync_roles(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = EngagementRolesView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel)
        button = next(child for child in view.children if getattr(child, 'label', None) == 'Sync Reward Roles')
        await button.callback(interaction)

    @discord.ui.button(label="Rebuild Storefront", style=discord.ButtonStyle.secondary, row=1)
    async def rebuild_store(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Confirm Rebuild", "Rebuilding republishes storefront content."), ConfirmStorefrontRebuildView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel))


class DebugToolsView(DashboardSectionView):
    @discord.ui.button(label="Debug Member", style=discord.ButtonStyle.primary, row=0)
    async def debug_member(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(DebugMemberEngagementModal(self.panel))


class RebuildToolsView(DashboardSectionView):
    @discord.ui.button(label="Rebuild Profile", style=discord.ButtonStyle.danger, row=0)
    async def rebuild_profile(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Confirm Rebuild", "This rebuild updates a member profile and role state."), ConfirmActionView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel, modal_factory=lambda: RebuildMemberProfileModal(self.panel)))

    @discord.ui.button(label="Reverse Event", style=discord.ButtonStyle.danger, row=0)
    async def reverse_event(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_or_edit(interaction, create_info_embed("Confirm Reverse", "This reverses one saved event and then rebuilds the profile."), ConfirmActionView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel, modal_factory=lambda: ReverseEventModal(self.panel)))


class ConfirmActionView(DashboardSectionView):
    def __init__(self, *, modal_factory, **kwargs):
        super().__init__(**kwargs)
        self.modal_factory = modal_factory

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.danger, row=0)
    async def continue_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self.modal_factory())
