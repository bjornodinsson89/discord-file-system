from __future__ import annotations

import logging
import re
from typing import Any

import discord

from utils import GuildSettingsRepository
from repositories.audit import AuditRepository
from utils.database import MissingDatabaseColumnError
from utils.discord_channels import resolve_guild_channel
from utils.embeds import create_error_embed, create_info_embed, create_success_embed
from repositories.jumps import JumpsRepository

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
    if interaction.response.is_done():
        await interaction.followup.send(embed=create_error_embed('Setup failed', msg), ephemeral=True)
    else:
        await interaction.response.send_message(embed=create_error_embed('Setup failed', msg), ephemeral=True)


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
            values = getattr(item, "values") or []
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
        if interaction.response.is_done():
            await interaction.followup.send(embed=create_error_embed("Setup failed", message), ephemeral=True)
        else:
            await interaction.response.send_message(embed=create_error_embed("Setup failed", message), ephemeral=True)


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


class SetupPanelView(OwnerView):
    @staticmethod
    def _resolve_bot_member(interaction: discord.Interaction) -> discord.Member | None:
        if not interaction.guild:
            return None
        me = interaction.guild.me or interaction.guild.get_member(interaction.client.user.id)
        return me

    def _build_embed(self) -> discord.Embed:
        s = self.settings
        guild = self.guild
        def channel_name(key: str):
            cid = s.get(key)
            ch = guild.get_channel(cid) if cid else None
            return ch.mention if ch else "Not set"

        def role_name(rid: int | None):
            role = guild.get_role(rid) if rid else None
            return role.mention if role else "Not set"

        admin_mentions = []
        for rid in (s.get("admin_role_ids") or []):
            role = guild.get_role(int(rid)) if str(rid).isdigit() else None
            if role:
                admin_mentions.append(role.mention)
        embed = create_info_embed("Setup Panel", "Configure channels, roles, templates, toggles, and tests from one place.")
        embed.add_field(name="Channels", value=(
            f"Jump: {channel_name('jump_99k_channel_id')}\n"
            f"Jump announcement: {channel_name('jump_announce_channel_id')}\n"
            f"Raffle announcement: {channel_name('raffle_announcement_channel_id')}\n"
            f"Raffle purchase panel: {channel_name('raffle_purchase_channel_id')}\n"
            f"Raffle giveaway purchase panel: {channel_name('raffle_giveaway_purchase_channel_id')}\n"
            f"Insurance: {channel_name('insurance_channel_id')}\n"
            f"Applications category: {channel_name('applications_category_id')}\n"
            f"Applications admin inbox: {channel_name('applications_admin_inbox_channel_id')}\n"
            f"Welcome: {channel_name('welcome_channel_id')}\n"
            f"Pools management: {channel_name('pool_channel_id')}\n"
            f"Pools purchase panel: {channel_name('pools_post_channel_id')}"
        ), inline=False)
        jump_ping_mentions = []
        for rid in (s.get("jump_ping_role_ids") or []):
            role = guild.get_role(int(rid)) if str(rid).isdigit() else None
            if role:
                jump_ping_mentions.append(role.mention)
        embed.add_field(name="Roles", value=(
            f"Admin roles: {', '.join(admin_mentions) if admin_mentions else 'Not set'}\n"
            f"99k_Jump_Host role: {role_name(s.get('host99k_role_id'))}\n"
            f"HJ_Insureance_provider role: {role_name(s.get('insurer_role_id'))}\n"
            f"Jump ping roles: {', '.join(jump_ping_mentions) if jump_ping_mentions else 'None selected'}"
        ), inline=False)
        host_tax_type = str(s.get('host_tax_type') or '').strip().lower()
        host_tax_enabled = bool(s.get('host_tax_enabled'))
        if host_tax_type == 'cash':
            host_tax_label = f"Torn Cash (${int(s.get('host_tax_cash_amount') or 0):,})" if s.get('host_tax_cash_amount') else 'Torn Cash (not set)'
        elif host_tax_type == 'item' and int(s.get('host_tax_item_id') or 0) == 206:
            host_tax_label = f"Xanax 💊 x{int(s.get('host_tax_quantity') or 0)}" if s.get('host_tax_quantity') else 'Xanax 💊 (quantity not set)'
        elif host_tax_type == 'item' and int(s.get('host_tax_item_id') or 0) == 366:
            host_tax_label = f"Erotic DvD 📀 x{int(s.get('host_tax_quantity') or 0)}" if s.get('host_tax_quantity') else 'Erotic DvD 📀 (quantity not set)'
        else:
            host_tax_label = 'Not configured'

        embed.add_field(name="Feature Toggles", value=(
            f"Welcome enabled: `{bool(s.get('welcome_enabled'))}`\n"
            f"Raffle announcement enabled: `{bool(s.get('raffle_announce_enabled', True))}`\n"
            f"Auto complete: `{bool(s.get('auto_complete_enabled', True))}`\n"
            f"Reservation timeout: `{s.get('reservation_timeout_minutes', 5)}` minutes\n"
            f"Default 99k max slots: `{s.get('default_max_slots', 5)}`\n"
            f"99k Host Tax enabled: `{host_tax_enabled}`\n"
            f"99k Host Tax recipient Torn ID: `{s.get('host_tax_recipient_torn_id') or 'Not set'}`\n"
            f"99k Host Tax payment: `{host_tax_label}`"
        ), inline=False)
        embed.add_field(name="Session placeholders", value=SUPPORTED_PLACEHOLDERS, inline=False)
        embed.add_field(name="Welcome placeholders", value=WELCOME_SUPPORTED_PLACEHOLDERS, inline=False)
        return embed

    def __init__(self, *, owner_id: int, db, settings: dict[str, Any], guild: discord.Guild):
        super().__init__(owner_id=owner_id, db=db, settings=settings)
        self.guild = guild

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
                        "Missing applications admin inbox",
                        "Set Applications admin inbox channel to a text channel (or use Set inbox by ID/mention).",
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

    @discord.ui.button(label="Channels", style=discord.ButtonStyle.primary)
    async def channels_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await _send_or_edit(interaction, _channels_embed(), ChannelsViewPage1(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self))
        except Exception as error:
            await _respond_callback_error(interaction, error)

    @discord.ui.button(label="Roles", style=discord.ButtonStyle.primary)
    async def roles_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await _send_or_edit(interaction, create_info_embed("Roles", "Configure admin, host, and insurer roles."), RolesView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self))
        except Exception as error:
            await _respond_callback_error(interaction, error)

    @discord.ui.button(label="Welcome", style=discord.ButtonStyle.primary)
    async def welcome_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await _send_or_edit(
                interaction,
                create_info_embed("Welcome", f"Configure welcome behavior. Supported: {WELCOME_SUPPORTED_PLACEHOLDERS}"),
                WelcomeView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self),
            )
        except Exception as error:
            await _respond_callback_error(interaction, error)

    @discord.ui.button(label="Feature Toggles", style=discord.ButtonStyle.primary)
    async def feature_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await _send_or_edit(interaction, create_info_embed("Feature Toggles", "Change runtime behavior toggles."), FeatureTogglesView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self))
        except Exception as error:
            await _respond_callback_error(interaction, error)

    @discord.ui.button(label="99k Host Tax", style=discord.ButtonStyle.primary)
    async def host_tax_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await _send_or_edit(interaction, create_info_embed("99k Host Tax", "Optional fee required for a host to start a jump."), HostTaxView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self))
        except Exception as error:
            await _respond_callback_error(interaction, error)

    @discord.ui.button(label="Test", style=discord.ButtonStyle.secondary)
    async def test_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await _send_or_edit(interaction, create_info_embed("Test", "Send test announcements to configured channels."), TestView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self))
        except Exception as error:
            await _respond_callback_error(interaction, error)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
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
        "Use buttons below to pick each channel.\n\n"
        "- **Pools management channel**: where pool management and commands live.\n"
        "- **Pools purchase panel channel**: where the pool buy-in panel is posted.\n"
        "- **Raffle announcement channel**: where new raffle announcements are posted.\n"
        "- **Raffle purchase panel channel**: where paid raffle purchase panels are posted.\n"
        "- **Raffle giveaway purchase panel channel**: where giveaway raffle panels are posted (falls back to raffle purchase panel channel).",
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
        self.add_item(ChannelSelect(self.panel, "raffle_giveaway_purchase_channel_id", "Set giveaway purchase panel channel (Free/Giveaway)", row=1))

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
        self.add_item(ChannelSelect(self.panel, "insurance_channel_id", "Set insurance requests channel", row=0))

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
                    "Configure channels for the applications system.\n\n"
                    "- **Applications category (optional)**: where private application channels are created.\n"
                    "- **Applications admin inbox (required)**: where Host + Insurance applications are sent for approval.\n"
                    "- Set admin inbox using the channel picker or **Set inbox by ID/mention**.",
                ),
                ChannelsViewApplications(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self.panel),
            )
        except Exception as error:
            await _respond_callback_error(interaction, error, "setup_roles_next_error")




class ChannelsViewApplications(BackView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.remove_item(self.back_btn)
        self.add_item(ChannelSelect(self.panel, "applications_category_id", "Set applications category (optional)", row=0, channel_types=[discord.ChannelType.category]))
        self.add_item(
            ChannelSelect(
                self.panel,
                "applications_admin_inbox_channel_id",
                "Set applications admin inbox channel",
                row=1,
                channel_types=[discord.ChannelType.text],
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

    @discord.ui.button(label="Set inbox by ID/mention", style=discord.ButtonStyle.secondary, row=2, custom_id="setup:applications:set_inbox_by_id")
    async def set_inbox_by_id_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await interaction.response.send_modal(ApplicationsInboxChannelModal(self.panel))
        except Exception as error:
            await _respond_callback_error(interaction, error, "setup_applications_inbox_by_id_button_error")

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=3)
    async def save_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        applications_inbox = self.guild.get_channel(int(self.settings.get("applications_admin_inbox_channel_id") or 0))
        if not isinstance(applications_inbox, discord.TextChannel):
            await interaction.response.send_message(
                embed=create_error_embed(
                    "Missing applications admin inbox",
                    "Set Applications admin inbox channel using the picker or **Set inbox by ID/mention**.",
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

    def __init__(self, panel: "SetupPanelView" | None = None, *, db=None):
        super().__init__(title="Insurer Profile")
        self.panel = panel
        self.db = db if db is not None else (panel.db if panel is not None else None)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.db is None:
            await interaction.response.send_message(
                embed=create_error_embed("Setup unavailable", "Could not save insurer profile right now."),
                ephemeral=True,
            )
            return
        repo = JumpsRepository(self.db.pool)
        await repo.create_insurer_profile(
            guild_id=interaction.guild_id,
            insurer_discord_id=interaction.user.id,
            display_name=str(self.display_name.value).strip(),
            policy_summary=str(self.policy_summary.value).strip(),
            contact_instructions=str(self.contact_instructions.value).strip(),
            metadata={},
        )
        await interaction.response.send_message(embed=create_success_embed("Insurer profile saved", "Your insurer profile was updated."), ephemeral=True)
class WelcomeView(BackView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_item(ChannelSelect(self.panel, "welcome_channel_id", "Set welcome channel (clear to set none)"))

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

    panel = SetupPanelView(owner_id=interaction.user.id, db=db, settings=settings, guild=interaction.guild)
    await interaction.response.send_message(embed=panel._build_embed(), view=panel, ephemeral=True)
