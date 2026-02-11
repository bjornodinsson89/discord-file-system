from __future__ import annotations

import logging
from typing import Any

import discord

from utils import GuildSettingsRepository
from utils.discord_channels import resolve_guild_channel
from utils.embeds import create_error_embed, create_info_embed, create_success_embed

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


async def _respond_callback_error(interaction: discord.Interaction, error: Exception):
    log.exception('Setup callback error guild_id=%s user_id=%s', interaction.guild_id, interaction.user.id if interaction.user else None, exc_info=error)
    msg = "Unexpected setup error. Please try again, or rerun /setup if this continues."
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
            exc_info=error,
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
            await _respond_callback_error(interaction, error)


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
            f"Raffle (legacy): {channel_name('raffle_channel_id')}\n"
            f"Raffle announcement: {channel_name('raffle_announcement_channel_id')}\n"
            f"Raffle purchase panel: {channel_name('raffle_purchase_channel_id')}\n"
            f"Insurance: {channel_name('insurance_channel_id')}\n"
            f"Welcome: {channel_name('welcome_channel_id')}"
        ), inline=False)
        embed.add_field(name="Roles", value=(
            f"Admin roles: {', '.join(admin_mentions) if admin_mentions else 'Not set'}\n"
            f"Host role: {role_name(s.get('host99k_role_id'))}\n"
            f"Insurer role: {role_name(s.get('insurer_role_id'))}"
        ), inline=False)
        embed.add_field(name="Feature Toggles", value=(
            f"Welcome enabled: `{bool(s.get('welcome_enabled'))}`\n"
            f"Raffle announcement enabled: `{bool(s.get('raffle_announce_enabled', True))}`\n"
            f"Auto complete: `{bool(s.get('auto_complete_enabled', True))}`\n"
            f"Reservation timeout: `{s.get('reservation_timeout_minutes', 5)}` minutes"
        ), inline=False)
        embed.add_field(name="Session placeholders", value=SUPPORTED_PLACEHOLDERS, inline=False)
        embed.add_field(name="Welcome placeholders", value=WELCOME_SUPPORTED_PLACEHOLDERS, inline=False)
        return embed

    def __init__(self, *, owner_id: int, db, settings: dict[str, Any], guild: discord.Guild):
        super().__init__(owner_id=owner_id, db=db, settings=settings)
        self.guild = guild

    async def save_changes(self, interaction: discord.Interaction, changes: dict[str, Any]) -> None:
        old_values = {k: self.settings.get(k) for k in changes}
        await self.db.update_guild_settings(interaction.guild_id, **changes)
        self.settings.update(changes)
        await self.db.log_audit(
            actor_id=interaction.user.id,
            action="setup_panel_updated",
            target_type="guild",
            target_id=interaction.guild_id,
            payload={"changes": {k: {"old": old_values[k], "new": changes[k]} for k in changes}},
            guild_id=interaction.guild_id,
            source="discord",
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

        me = self._resolve_bot_member(interaction)
        if me is None:
            await interaction.response.send_message(
                embed=create_error_embed("Bot member unavailable", "Unable to validate my permissions right now. Please try again."),
                ephemeral=True,
            )
            return
        mention = getattr(resolved, "mention", f"<#{resolved.id}>")
        missing = _missing_channel_perms(resolved, me)
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
            await _send_or_edit(interaction, create_info_embed("Channels", "Use buttons below to pick each channel."), ChannelsView(owner_id=self.owner_id, db=self.db, settings=self.settings, guild=self.guild, panel=self))
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

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await _send_or_edit(interaction, self.panel._build_embed(), self.panel)
        except Exception as error:
            await _respond_callback_error(interaction, error)


class ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: SetupPanelView, key: str, placeholder: str):
        super().__init__(
            placeholder=placeholder,
            min_values=0,
            max_values=1,
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        )
        self.panel = panel
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        try:
            if not self.values:
                await self.panel.save_changes(interaction, {self.key: None})
                return
            await self.panel._set_channel(interaction, self.key, self.values[0])
        except Exception as error:
            await _respond_callback_error(interaction, error)


class ChannelsView(BackView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_item(ChannelSelect(self.panel, "jump_99k_channel_id", "Set jump (99k) channel"))
        self.add_item(ChannelSelect(self.panel, "raffle_channel_id", "Set raffle channel (legacy fallback)"))
        self.add_item(ChannelSelect(self.panel, "raffle_announcement_channel_id", "Set raffle announcement channel"))
        self.add_item(ChannelSelect(self.panel, "raffle_purchase_channel_id", "Set raffle purchase panel channel"))
        self.add_item(ChannelSelect(self.panel, "insurance_channel_id", "Set insurance channel"))
        self.add_item(ChannelSelect(self.panel, "welcome_channel_id", "Set welcome channel"))


class AdminRoleSelect(discord.ui.RoleSelect):
    def __init__(self, panel: SetupPanelView):
        super().__init__(placeholder="Set admin role(s)", min_values=0, max_values=10)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        try:
            role_ids = [int(role.id) for role in self.values if not role.is_default()]
            await self.panel.save_changes(interaction, {"admin_role_ids": role_ids})
        except Exception as error:
            await _respond_callback_error(interaction, error)


class SingleRoleSelect(discord.ui.RoleSelect):
    def __init__(self, panel: SetupPanelView, key: str, placeholder: str):
        super().__init__(placeholder=placeholder, min_values=1, max_values=1)
        self.panel = panel
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        try:
            role = self.values[0]
            if role.is_default():
                await interaction.response.send_message(embed=create_error_embed("Invalid role", "Cannot use @everyone for this setting."), ephemeral=True)
                return
            await self.panel.save_changes(interaction, {self.key: int(role.id)})
        except Exception as error:
            await _respond_callback_error(interaction, error)


class RolesView(BackView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_item(AdminRoleSelect(self.panel))
        self.add_item(SingleRoleSelect(self.panel, "host99k_role_id", "Set host role"))
        self.add_item(SingleRoleSelect(self.panel, "insurer_role_id", "Set insurer role"))


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
