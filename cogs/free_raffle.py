from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from repositories.free_raffle_repo import FreeRaffleRepository
from repositories.torn_items import TornItemsRepository
from utils import GuildSettingsRepository, get_database
from utils.database import get_pool, is_initialized as db_is_initialized, wait_until_initialized
from utils.advisory_lock import run_with_advisory_lock
from utils.worker_throttle import db_heavy_worker_slot, sleep_startup_jitter
from utils.panel_edit_safety import PANEL_EDIT_SAFETY
from views.free_raffle_views import EnterRaffleView, HostControlsView
from utils.embeds import clamp_percent, format_remaining_time, render_text_progress_bar

AUTO_ENTRY_MESSAGE_STEP = 15
ENTRIES_PROGRESS_WIDTH = 10

log = logging.getLogger("happy_jumper.free_raffle")

FREE_RAFFLE_MIN_DAYS = 1
FREE_RAFFLE_MAX_DAYS = 30
FREE_RAFFLE_EXPIRY_BATCH_SIZE = 10
GIVEAWAY_ENTRY_MODE_CHOICES = {
    "button": {
        "label": "Button join",
        "button_join_enabled": True,
        "auto_entry_enabled": False,
        "weighted_enabled": False,
    },
    "auto": {
        "label": "Auto entry",
        "button_join_enabled": False,
        "auto_entry_enabled": True,
        "weighted_enabled": False,
    },
    "button_weighted": {
        "label": "Button join + weighted",
        "button_join_enabled": True,
        "auto_entry_enabled": False,
        "weighted_enabled": True,
    },
    "auto_weighted": {
        "label": "Auto entry + weighted",
        "button_join_enabled": False,
        "auto_entry_enabled": True,
        "weighted_enabled": True,
    },
}


def _status_label(status: str, winner_id: int | None) -> str:
    normalized = str(status or "").lower()
    if normalized == "active":
        return "Active"
    if normalized == "cancelled":
        return "Cancelled"
    if winner_id:
        return "Ended"
    return "Ended (no entries)"


def _status_color(status: str, winner_id: int | None) -> discord.Color:
    normalized = str(status or "").lower()
    if normalized == "active":
        return discord.Color.from_rgb(46, 204, 113)
    if normalized == "cancelled":
        return discord.Color.from_rgb(231, 76, 60)
    if winner_id:
        return discord.Color.light_grey()
    return discord.Color.dark_grey()


class FreeRaffleModal(discord.ui.Modal, title="Giveaway"):
    prize = discord.ui.TextInput(label="Prize", required=True, max_length=200)
    ends_in_days = discord.ui.TextInput(
        label="Ends in (days)", required=True, min_length=1, max_length=2
    )
    note = discord.ui.TextInput(
        label="Note",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=500,
    )
    allowed_entries_per_user = discord.ui.TextInput(
        label="Allowed Entries Per User", required=False, default="1", max_length=3
    )

    def __init__(self, cog: "FreeRaffleCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.channel is None:
            await interaction.response.send_message(
                "❌ This command can only be used in a server channel.", ephemeral=True
            )
            return

        days_raw = str(self.ends_in_days.value).strip()
        if not days_raw.isdigit():
            await interaction.response.send_message(
                "❌ Ends in (days) must be a whole number from 1 to 30.", ephemeral=True
            )
            return
        duration_days = int(days_raw)
        if duration_days < FREE_RAFFLE_MIN_DAYS or duration_days > FREE_RAFFLE_MAX_DAYS:
            await interaction.response.send_message(
                f"❌ Ends in (days) must be between {FREE_RAFFLE_MIN_DAYS} and {FREE_RAFFLE_MAX_DAYS}.",
                ephemeral=True,
            )
            return

        settings = await GuildSettingsRepository(get_database()).get_or_create(
            int(interaction.guild_id)
        )
        default_channel_id = GuildSettingsRepository.resolve_raffle_giveaway_purchase_channel_id(
            settings
        ) or int(interaction.channel_id)
        allowed_entries_raw = str(self.allowed_entries_per_user.value).strip() or "1"
        if not allowed_entries_raw.isdigit() or int(allowed_entries_raw) < 1:
            await interaction.response.send_message(
                "❌ Allowed Entries Per User must be a whole number of at least 1.",
                ephemeral=True,
            )
            return

        draft = {
            "guild_id": int(interaction.guild_id),
            "request_channel_id": int(interaction.channel_id),
            "default_post_channel_id": int(default_channel_id),
            "host_discord_id": int(interaction.user.id),
            "prize_text": str(self.prize.value).strip(),
            "note_text": (str(self.note.value).strip() or None),
            "duration_days": duration_days,
            "auto_entry_max_per_user": int(allowed_entries_raw),
            "messages_per_entry": AUTO_ENTRY_MESSAGE_STEP,
            "role_bonus_rules": [],
        }
        self.cog.store_create_draft(int(interaction.user.id), draft)
        await interaction.response.defer(ephemeral=True, thinking=False)
        await interaction.followup.send(
            embed=self.cog.build_create_summary_embed(
                draft, mode_key="button", channel_id=int(default_channel_id)
            ),
            ephemeral=True,
            view=GiveawayCreateFlowView(
                self.cog, int(interaction.user.id), int(default_channel_id)
            ),
        )


class GiveawayEntryModeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=choice["label"], value=key)
            for key, choice in GIVEAWAY_ENTRY_MODE_CHOICES.items()
        ]
        super().__init__(
            placeholder="Select giveaway entry mode",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, GiveawayCreateFlowView):
            await view.set_mode(interaction, self.values[0])


class GiveawayPostChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, default_channel_id: int | None):
        super().__init__(
            placeholder="Choose where to post the giveaway",
            min_values=0,
            max_values=1,
            channel_types=[discord.ChannelType.text],
            row=1,
            default_values=[discord.Object(id=default_channel_id)] if default_channel_id else [],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, GiveawayCreateFlowView):
            selected = self.values[0] if self.values else None
            await view.set_channel(interaction, getattr(selected, "id", None))


class GiveawayCreateFlowView(discord.ui.View):
    def __init__(self, cog: "FreeRaffleCog", owner_id: int, default_channel_id: int | None):
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id
        self.mode_key = "button"
        self.post_channel_id = default_channel_id
        self.add_item(GiveawayEntryModeSelect())
        self.add_item(GiveawayPostChannelSelect(default_channel_id))

    async def refresh_message(self, interaction: discord.Interaction) -> None:
        draft = self.cog.get_create_draft(self.owner_id) or {}
        embed = self.cog.build_create_summary_embed(
            draft, mode_key=self.mode_key, channel_id=self.post_channel_id
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the giveaway host can use this flow.", ephemeral=True
            )
            return False
        return True

    async def set_mode(self, interaction: discord.Interaction, mode_key: str) -> None:
        self.mode_key = mode_key if mode_key in GIVEAWAY_ENTRY_MODE_CHOICES else "button"
        await self.refresh_message(interaction)

    async def set_channel(self, interaction: discord.Interaction, channel_id: int | None) -> None:
        if channel_id:
            self.post_channel_id = int(channel_id)
        await self.refresh_message(interaction)

    @discord.ui.button(label="Configure Auto Entry", style=discord.ButtonStyle.secondary, row=2)
    async def configure_auto_entry(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        draft = self.cog.get_create_draft(self.owner_id) or {}
        await interaction.response.send_message(
            embed=self.cog.build_auto_entry_settings_embed(draft, title="Auto Entry Settings"),
            ephemeral=True,
            view=DraftAutoEntrySettingsView(self.cog, owner_id=self.owner_id, draft=draft),
        )

    @discord.ui.button(label="Create Giveaway", style=discord.ButtonStyle.success, row=3)
    async def create(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        await self.cog.finish_giveaway_create(
            interaction,
            owner_id=self.owner_id,
            mode_key=self.mode_key,
            override_channel_id=self.post_channel_id,
        )


class AutoEntrySettingsModal(discord.ui.Modal, title="Auto Entry Settings"):
    messages_per_entry = discord.ui.TextInput(
        label="Messages Per Entry",
        required=True,
        default=str(AUTO_ENTRY_MESSAGE_STEP),
        max_length=4,
    )

    def __init__(self, cog: "FreeRaffleCog", *, owner_id: int, draft: dict):
        super().__init__()
        self.cog = cog
        self.owner_id = owner_id
        self.messages_per_entry.default = str(
            int(draft.get("messages_per_entry") or AUTO_ENTRY_MESSAGE_STEP)
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            messages_per_entry = self.cog.parse_positive_int(
                self.messages_per_entry.value, label="Messages Per Entry"
            )
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        draft = self.cog.get_create_draft(self.owner_id) or {}
        draft["messages_per_entry"] = messages_per_entry
        self.cog.store_create_draft(self.owner_id, draft)
        await interaction.response.send_message(
            "✅ Auto entry settings updated for this giveaway draft.", ephemeral=True
        )


class EditAutoEntrySettingsModal(discord.ui.Modal, title="Edit Auto Entry Settings"):
    messages_per_entry = discord.ui.TextInput(
        label="Messages Per Entry", required=True, max_length=4
    )

    def __init__(self, cog: "FreeRaffleCog", raffle: dict):
        super().__init__()
        self.cog = cog
        self.raffle_id = int(raffle["id"])
        self.messages_per_entry.default = str(
            max(1, int(raffle.get("messages_per_entry") or AUTO_ENTRY_MESSAGE_STEP))
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            messages_per_entry = self.cog.parse_positive_int(
                self.messages_per_entry.value, label="Messages Per Entry"
            )
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        repo = FreeRaffleRepository(get_pool())
        await repo.update_auto_entry_settings(
            self.raffle_id,
            messages_per_entry=messages_per_entry,
        )
        await interaction.response.send_message("✅ Auto entry settings updated.", ephemeral=True)


class BonusAmountSelect(discord.ui.Select):
    def __init__(self, *, current_bonus: int | None = None):
        options = [
            discord.SelectOption(
                label=f"+{amount}", value=str(amount), default=amount == current_bonus
            )
            for amount in range(1, 6)
        ]
        super().__init__(
            placeholder="Select bonus entries",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, BaseRoleBonusConfigView):
            await view.set_bonus_amount(interaction, int(self.values[0]))


class BaseRoleBonusConfigView(discord.ui.View):
    def __init__(
        self,
        cog: "FreeRaffleCog",
        *,
        host_id: int,
        selected_role_id: int | None = None,
        selected_bonus_amount: int = 1,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.host_id = host_id
        self.selected_role_id = selected_role_id
        self.selected_bonus_amount = selected_bonus_amount
        role_select = discord.ui.RoleSelect(
            placeholder="Choose a role",
            min_values=1,
            max_values=1,
            row=0,
        )
        role_select.callback = self._select_role
        self.add_item(role_select)
        self.add_item(BonusAmountSelect(current_bonus=selected_bonus_amount))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.host_id:
            await interaction.response.send_message(
                "Only the giveaway host can manage these settings.", ephemeral=True
            )
            return False
        return True

    async def _select_role(self, interaction: discord.Interaction) -> None:
        role = None
        role_select = next(
            (child for child in self.children if isinstance(child, discord.ui.RoleSelect)), None
        )
        if isinstance(role_select, discord.ui.RoleSelect):
            role = role_select.values[0] if role_select.values else None
        self.selected_role_id = int(role.id) if role is not None else None
        await self.refresh(interaction)

    async def set_bonus_amount(self, interaction: discord.Interaction, amount: int) -> None:
        self.selected_bonus_amount = amount
        await self.refresh(interaction)

    async def refresh(self, interaction: discord.Interaction) -> None:
        raise NotImplementedError


class DraftRoleBonusConfigView(BaseRoleBonusConfigView):
    def __init__(self, cog: "FreeRaffleCog", *, owner_id: int):
        draft = cog.get_create_draft(owner_id) or {}
        bonus_rules = draft.get("role_bonus_rules") or []
        selected_role_id = int(bonus_rules[0]["role_id"]) if bonus_rules else None
        selected_bonus_amount = (
            int(bonus_rules[0].get("bonus_entries_per_qualification") or 1) if bonus_rules else 1
        )
        super().__init__(
            cog,
            host_id=owner_id,
            selected_role_id=selected_role_id,
            selected_bonus_amount=selected_bonus_amount,
        )
        self.owner_id = owner_id

    def _draft(self) -> dict:
        return self.cog.get_create_draft(self.owner_id) or {}

    async def refresh(self, interaction: discord.Interaction) -> None:
        draft = self._draft()
        embed = self.cog.build_role_bonus_embed(
            draft.get("role_bonus_rules") or [],
            title="Role Bonuses",
            guild=interaction.guild,
            selected_role_id=self.selected_role_id,
            selected_bonus_amount=self.selected_bonus_amount,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Save Role Bonus", style=discord.ButtonStyle.primary, row=2)
    async def save(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.selected_role_id is None:
            await interaction.response.send_message("❌ Choose a role first.", ephemeral=True)
            return
        draft = self._draft()
        updated_rules = self.cog.merge_role_bonus_rule(
            draft.get("role_bonus_rules") or [],
            role_id=self.selected_role_id,
            bonus_entries=self.selected_bonus_amount,
        )
        draft["role_bonus_rules"] = updated_rules
        self.cog.store_create_draft(self.owner_id, draft)
        embed = self.cog.build_role_bonus_embed(
            updated_rules,
            title="Role Bonuses",
            guild=interaction.guild,
            selected_role_id=self.selected_role_id,
            selected_bonus_amount=self.selected_bonus_amount,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success, row=2)
    async def done(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        draft = self._draft()
        await interaction.response.edit_message(
            embed=self.cog.build_auto_entry_settings_embed(draft, title="Auto Entry Settings"),
            view=DraftAutoEntrySettingsView(self.cog, owner_id=self.owner_id, draft=draft),
        )


class PersistedRoleBonusConfigView(BaseRoleBonusConfigView):
    def __init__(self, cog: "FreeRaffleCog", raffle: dict, bonus_rules: list[dict]):
        selected_role_id = int(bonus_rules[0]["role_id"]) if bonus_rules else None
        selected_bonus_amount = (
            int(bonus_rules[0].get("bonus_entries_per_qualification") or 1) if bonus_rules else 1
        )
        super().__init__(
            cog,
            host_id=int(raffle["host_discord_id"]),
            selected_role_id=selected_role_id,
            selected_bonus_amount=selected_bonus_amount,
        )
        self.raffle = dict(raffle)
        self.raffle_id = int(raffle["id"])

    async def _bonus_rules(self) -> list[dict]:
        return await FreeRaffleRepository(get_pool()).list_role_bonus_rules(self.raffle_id)

    async def refresh(self, interaction: discord.Interaction) -> None:
        bonus_rules = await self._bonus_rules()
        embed = self.cog.build_role_bonus_embed(
            bonus_rules,
            title=f"Role Bonuses for Giveaway #{self.raffle_id}",
            guild=interaction.guild,
            selected_role_id=self.selected_role_id,
            selected_bonus_amount=self.selected_bonus_amount,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Save Role Bonus", style=discord.ButtonStyle.primary, row=2)
    async def save(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.selected_role_id is None:
            await interaction.response.send_message("❌ Choose a role first.", ephemeral=True)
            return
        await FreeRaffleRepository(get_pool()).upsert_role_bonus_rule(
            self.raffle_id,
            self.selected_role_id,
            self.selected_bonus_amount,
        )
        bonus_rules = await self._bonus_rules()
        embed = self.cog.build_role_bonus_embed(
            bonus_rules,
            title=f"Role Bonuses for Giveaway #{self.raffle_id}",
            guild=interaction.guild,
            selected_role_id=self.selected_role_id,
            selected_bonus_amount=self.selected_bonus_amount,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success, row=2)
    async def done(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        bonus_rules = await self._bonus_rules()
        await interaction.response.edit_message(
            embed=self.cog.build_auto_entry_admin_embed(
                self.raffle,
                bonus_rules,
                title=f"Auto Entry Settings for Giveaway #{self.raffle_id}",
            ),
            view=AutoEntryRoleBonusManageView(self.cog, self.raffle, bonus_rules),
        )


class AutoEntryRoleBonusManageView(discord.ui.View):
    def __init__(self, cog: "FreeRaffleCog", raffle: dict, bonus_rules: list[dict]):
        super().__init__(timeout=300)
        self.cog = cog
        self.raffle = dict(raffle)
        self.raffle_id = int(raffle["id"])
        self.host_id = int(raffle["host_discord_id"])
        self.selected_role_id: int | None = int(bonus_rules[0]["role_id"]) if bonus_rules else None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.host_id:
            await interaction.response.send_message(
                "Only the giveaway host can manage these settings.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Add Role Bonus", style=discord.ButtonStyle.primary, row=1)
    async def add_or_update(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        repo = FreeRaffleRepository(get_pool())
        bonus_rules = await repo.list_role_bonus_rules(self.raffle_id)
        await interaction.response.send_message(
            embed=self.cog.build_role_bonus_embed(
                bonus_rules,
                title=f"Role Bonuses for Giveaway #{self.raffle_id}",
                guild=interaction.guild,
            ),
            ephemeral=True,
            view=PersistedRoleBonusConfigView(self.cog, self.raffle, bonus_rules),
        )

    @discord.ui.button(label="Edit Messages Per Entry", style=discord.ButtonStyle.secondary, row=1)
    async def edit_limits(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(EditAutoEntrySettingsModal(self.cog, self.raffle))

    @discord.ui.button(label="Remove Role Bonus", style=discord.ButtonStyle.danger, row=1)
    async def remove(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        bonus_rules = await FreeRaffleRepository(get_pool()).list_role_bonus_rules(self.raffle_id)
        if not bonus_rules:
            await interaction.response.send_message(
                "❌ No role bonuses are configured yet.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=self.cog.build_role_bonus_embed(
                bonus_rules,
                title=f"Role Bonuses for Giveaway #{self.raffle_id}",
                guild=interaction.guild,
            ),
            ephemeral=True,
            view=PersistedRoleBonusRemovalView(self.cog, self.raffle, bonus_rules),
        )


class DraftAutoEntrySettingsView(discord.ui.View):
    def __init__(self, cog: "FreeRaffleCog", *, owner_id: int, draft: dict):
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id
        self.draft = dict(draft)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the giveaway host can use this flow.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Set Messages Per Entry", style=discord.ButtonStyle.secondary, row=0)
    async def set_messages(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            AutoEntrySettingsModal(
                self.cog,
                owner_id=self.owner_id,
                draft=self.cog.get_create_draft(self.owner_id) or {},
            )
        )

    @discord.ui.button(label="Add Role Bonus", style=discord.ButtonStyle.primary, row=0)
    async def add_role_bonus(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=self.cog.build_role_bonus_embed(
                (self.cog.get_create_draft(self.owner_id) or {}).get("role_bonus_rules") or [],
                title="Role Bonuses",
                guild=interaction.guild,
            ),
            view=DraftRoleBonusConfigView(self.cog, owner_id=self.owner_id),
        )

    @discord.ui.button(label="Remove Role Bonus", style=discord.ButtonStyle.danger, row=0)
    async def remove_role_bonus(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        draft = self.cog.get_create_draft(self.owner_id) or {}
        bonus_rules = draft.get("role_bonus_rules") or []
        if not bonus_rules:
            await interaction.response.send_message(
                "❌ No role bonuses are configured yet.", ephemeral=True
            )
            return
        await interaction.response.edit_message(
            embed=self.cog.build_role_bonus_embed(
                bonus_rules, title="Role Bonuses", guild=interaction.guild
            ),
            view=DraftRoleBonusRemovalView(
                self.cog, owner_id=self.owner_id, bonus_rules=bonus_rules
            ),
        )

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success, row=1)
    async def done(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        draft = self.cog.get_create_draft(self.owner_id) or {}
        await interaction.response.edit_message(
            embed=self.cog.build_auto_entry_settings_embed(draft, title="Auto Entry Settings"),
            view=self,
        )


class RoleBonusRemovalSelect(discord.ui.Select):
    def __init__(self, bonus_rules: list[dict], *, guild: discord.Guild | None = None):
        options = []
        for rule in bonus_rules[:25]:
            role_id = int(rule["role_id"])
            role = guild.get_role(role_id) if guild else None
            options.append(
                discord.SelectOption(
                    label=(role.name if role else f"Role {role_id}")[:100],
                    value=str(role_id),
                    description=f"Remove +{int(rule.get('bonus_entries_per_qualification') or 0)} bonus entries",
                )
            )
        super().__init__(
            placeholder="Select a role bonus to remove", min_values=1, max_values=1, options=options
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, (DraftRoleBonusRemovalView, PersistedRoleBonusRemovalView)):
            await view.remove_selected(interaction, int(self.values[0]))


class DraftRoleBonusRemovalView(discord.ui.View):
    def __init__(self, cog: "FreeRaffleCog", *, owner_id: int, bonus_rules: list[dict]):
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id
        self.add_item(RoleBonusRemovalSelect(bonus_rules))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the giveaway host can manage these settings.", ephemeral=True
            )
            return False
        return True

    async def remove_selected(self, interaction: discord.Interaction, role_id: int) -> None:
        draft = self.cog.get_create_draft(self.owner_id) or {}
        updated_rules = [
            rule for rule in draft.get("role_bonus_rules") or [] if int(rule["role_id"]) != role_id
        ]
        draft["role_bonus_rules"] = updated_rules
        self.cog.store_create_draft(self.owner_id, draft)
        await interaction.response.edit_message(
            embed=self.cog.build_auto_entry_settings_embed(draft, title="Auto Entry Settings"),
            view=DraftAutoEntrySettingsView(self.cog, owner_id=self.owner_id, draft=draft),
        )


class PersistedRoleBonusRemovalView(discord.ui.View):
    def __init__(self, cog: "FreeRaffleCog", raffle: dict, bonus_rules: list[dict]):
        super().__init__(timeout=300)
        self.cog = cog
        self.raffle = dict(raffle)
        self.raffle_id = int(raffle["id"])
        self.host_id = int(raffle["host_discord_id"])
        self.add_item(RoleBonusRemovalSelect(bonus_rules))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.host_id:
            await interaction.response.send_message(
                "Only the giveaway host can manage these settings.", ephemeral=True
            )
            return False
        return True

    async def remove_selected(self, interaction: discord.Interaction, role_id: int) -> None:
        repo = FreeRaffleRepository(get_pool())
        await repo.remove_role_bonus_rule(self.raffle_id, role_id)
        bonus_rules = await repo.list_role_bonus_rules(self.raffle_id)
        await interaction.response.edit_message(
            embed=self.cog.build_auto_entry_admin_embed(
                self.raffle,
                bonus_rules,
                title=f"Auto Entry Settings for Giveaway #{self.raffle_id}",
            ),
            view=AutoEntryRoleBonusManageView(self.cog, self.raffle, bonus_rules),
        )


class FreeRaffleCog(commands.Cog):
    giveaway = app_commands.Group(name="giveaway", description="Giveaway commands")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._views_registered = False
        self._ready_init_lock = asyncio.Lock()
        self._create_drafts: dict[int, dict] = {}
        self._last_host_controls_raffles: dict[int, dict] = {}

    async def cog_load(self) -> None:
        self._views_registered = False
        self._ready_init_lock = asyncio.Lock()
        self._create_drafts = {}
        self._last_host_controls_raffles = {}

    async def cog_unload(self) -> None:
        if self.free_raffle_expiration_worker.is_running():
            self.free_raffle_expiration_worker.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._views_registered:
            if not self.free_raffle_expiration_worker.is_running():
                self.free_raffle_expiration_worker.start()
            return
        async with self._ready_init_lock:
            if self._views_registered:
                if not self.free_raffle_expiration_worker.is_running():
                    self.free_raffle_expiration_worker.start()
                return
            try:
                await wait_until_initialized(timeout=30.0)
                repo = FreeRaffleRepository(get_pool())
                backfilled = await repo.backfill_missing_ends_at()
                if backfilled > 0:
                    log.warning(
                        "Backfilled ends_at for %s active free raffles using created_at + 1 day",
                        backfilled,
                    )
                for raffle in await repo.list_active_raffles():
                    raffle_id = int(raffle["id"])
                    self.bot.add_view(
                        self.build_free_raffle_view(
                            raffle_id,
                            status=str(raffle.get("status") or ""),
                            button_join_enabled=bool(raffle.get("button_join_enabled", False)),
                        )
                    )
                self._views_registered = True
                if not self.free_raffle_expiration_worker.is_running():
                    self.free_raffle_expiration_worker.start()
                await self.process_expired_raffles()
            except Exception:
                log.exception("Failed registering free raffle views")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        data = interaction.data if isinstance(interaction.data, dict) else {}
        custom_id = str(data.get("custom_id") or "")
        if not custom_id.startswith("fr_draw:"):
            return
        await self._send_ephemeral(
            interaction, "This giveaway is now drawn automatically when it ends."
        )

    @giveaway.command(name="start", description="Start a giveaway")
    async def start(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(FreeRaffleModal(self))

    def public_view(self, raffle_id: int, *, disabled: bool = False) -> EnterRaffleView:
        return EnterRaffleView(
            raffle_id=raffle_id,
            on_enter=self.handle_enter,
            on_info=self.handle_info,
            disabled=disabled,
        )

    def build_free_raffle_view(
        self,
        raffle_id: int,
        host_discord_id: int | None = None,
        status: str | None = None,
        button_join_enabled: bool | None = None,
        **_: object,
    ) -> EnterRaffleView:
        disabled = str(status or "").lower() != "active"
        return EnterRaffleView(
            raffle_id=raffle_id,
            on_enter=self.handle_enter,
            on_info=self.handle_info,
            disabled=disabled,
            show_join_button=bool(button_join_enabled),
        )

    def host_controls_view(
        self, raffle_id: int, *, disabled: bool = False, can_reroll: bool = False
    ) -> HostControlsView:
        show_auto_settings = False
        raffle: dict | None = None
        if hasattr(self, "_last_host_controls_raffles"):
            raffle = getattr(self, "_last_host_controls_raffles", {}).get(int(raffle_id))
        if raffle is not None:
            show_auto_settings = bool(raffle.get("auto_entry_enabled", False))
        return HostControlsView(
            raffle_id=raffle_id,
            on_end_now=self.handle_end_now,
            on_cancel=self.handle_cancel,
            on_refresh=self.handle_refresh_controls,
            on_view_entries=self.handle_view_entries,
            on_reroll=self.handle_reroll,
            on_auto_settings=self.handle_auto_settings,
            disabled=disabled,
            can_reroll=can_reroll,
            show_auto_settings=show_auto_settings,
        )

    def store_create_draft(self, user_id: int, draft: dict) -> None:
        self._create_drafts[int(user_id)] = dict(draft)

    def get_create_draft(self, user_id: int) -> dict | None:
        draft = self._create_drafts.get(int(user_id))
        return dict(draft) if draft else None

    def pop_create_draft(self, user_id: int) -> dict | None:
        return self._create_drafts.pop(int(user_id), None)

    def parse_positive_int(self, raw_value: str, *, label: str) -> int:
        cleaned = str(raw_value or "").strip()
        if not cleaned.isdigit() or int(cleaned) < 1:
            raise ValueError(f"{label} must be a whole number of at least 1.")
        return int(cleaned)

    def parse_non_negative_int(self, raw_value: str, *, label: str) -> int:
        cleaned = str(raw_value or "").strip()
        if not cleaned.isdigit() or int(cleaned) < 0:
            raise ValueError(f"{label} must be a whole number of at least 0.")
        return int(cleaned)

    def merge_role_bonus_rule(
        self, rules: list[dict], *, role_id: int, bonus_entries: int
    ) -> list[dict[str, int]]:
        merged = {
            int(rule["role_id"]): max(0, int(rule.get("bonus_entries_per_qualification") or 0))
            for rule in rules
        }
        merged[int(role_id)] = max(1, int(bonus_entries))
        return [
            {"role_id": current_role_id, "bonus_entries_per_qualification": current_bonus}
            for current_role_id, current_bonus in sorted(merged.items())
        ]

    def _role_label(self, role_id: int, guild: discord.Guild | None = None) -> str:
        role = guild.get_role(role_id) if guild else None
        if role is None:
            return f"<@&{role_id}>"
        return getattr(role, "mention", f"<@&{role_id}>")

    def build_role_bonus_embed(
        self,
        rules: list[dict],
        *,
        title: str,
        guild: discord.Guild | None = None,
        selected_role_id: int | None = None,
        selected_bonus_amount: int | None = None,
    ) -> discord.Embed:
        lines = [
            "Choose a role, choose bonus entries, then save the rule.",
            "Matching role bonuses stack for each qualifying auto-entry cycle.",
        ]
        if selected_role_id is not None:
            lines.append(
                f"Current selection: {self._role_label(selected_role_id, guild)} — **+{int(selected_bonus_amount or 1)} Entries**"
            )
        if rules:
            lines.append("")
            lines.append("Configured Role Bonuses:")
            lines.extend(
                f"• {self._role_label(int(rule['role_id']), guild)} — **+{int(rule.get('bonus_entries_per_qualification') or 0)} Entries**"
                for rule in rules
            )
        else:
            lines.append("")
            lines.append("Configured Role Bonuses: **None**")
        return discord.Embed(
            title=title, description="\n".join(lines), color=discord.Color.blurple()
        )

    def build_auto_entry_settings_embed(self, draft: dict, *, title: str) -> discord.Embed:
        bonus_rules = draft.get("role_bonus_rules") or []
        lines = [
            f"Messages Per Entry: **{max(1, int(draft.get('messages_per_entry') or AUTO_ENTRY_MESSAGE_STEP))}**",
            f"Allowed Entries Per User: **{max(1, int(draft.get('auto_entry_max_per_user') or 1))}**",
            "Role Bonuses:",
        ]
        if bonus_rules:
            lines.extend(
                f"• <@&{int(rule['role_id'])}> — **+{int(rule.get('bonus_entries_per_qualification') or 0)} Entries**"
                for rule in bonus_rules
            )
        else:
            lines.append("• None configured")
        return discord.Embed(
            title=title, description="\n".join(lines), color=discord.Color.blurple()
        )

    def build_auto_entry_admin_embed(
        self, raffle: dict, bonus_rules: list[dict], *, title: str
    ) -> discord.Embed:
        draft = {
            "messages_per_entry": raffle.get("messages_per_entry"),
            "auto_entry_max_per_user": raffle.get("auto_entry_max_per_user"),
            "role_bonus_rules": bonus_rules,
        }
        embed = self.build_auto_entry_settings_embed(draft, title=title)
        embed.description = (
            (embed.description or "")
            + "\n\nUse **Add Role Bonus** to add or update a rule, or **Remove Role Bonus** to delete one."
        )
        return embed

    def build_create_summary_embed(
        self, draft: dict, *, mode_key: str, channel_id: int | None
    ) -> discord.Embed:
        mode = GIVEAWAY_ENTRY_MODE_CHOICES.get(mode_key, GIVEAWAY_ENTRY_MODE_CHOICES["button"])
        auto_enabled = bool(mode["auto_entry_enabled"])
        lines = [
            f"Prize: **{str(draft.get('prize_text') or 'Not set')}**",
            f"Ends In: **{int(draft.get('duration_days') or FREE_RAFFLE_MIN_DAYS)} day(s)**",
            f"Mode: **{mode['label']}**",
            f"Channel: {f'<#{int(channel_id)}>' if channel_id else 'Current channel'}",
            f"Allowed Entries Per User: **{max(1, int(draft.get('auto_entry_max_per_user') or 1))}**",
        ]
        note_text = str(draft.get("note_text") or "").strip()
        lines.append(f"Note: **{note_text or 'None'}**")
        if auto_enabled:
            bonus_rules = draft.get("role_bonus_rules") or []
            lines.append(
                f"Messages Per Entry: **{max(1, int(draft.get('messages_per_entry') or AUTO_ENTRY_MESSAGE_STEP))}**"
            )
            if bonus_rules:
                lines.append(
                    "Role Bonuses: "
                    + ", ".join(
                        f"<@&{int(rule['role_id'])}> (+{int(rule.get('bonus_entries_per_qualification') or 0)})"
                        for rule in bonus_rules
                    )
                )
            else:
                lines.append("Role Bonuses: **None**")
        embed = discord.Embed(
            title="Create Giveaway",
            description="Review the giveaway details below, then create it when everything looks right.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Summary", value="\n".join(lines), inline=False)
        embed.set_footer(text="Tip: Configure auto entry only if the selected mode uses it.")
        return embed

    async def _resolve_post_channel(
        self, interaction: discord.Interaction, channel_id: int
    ) -> discord.abc.Messageable | None:
        guild = interaction.guild
        if guild is None:
            return interaction.channel if hasattr(interaction.channel, "send") else None
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            try:
                fetched = await guild.fetch_channel(int(channel_id))
                if hasattr(fetched, "send"):
                    channel = fetched
            except Exception:
                channel = None
        if channel is None or not hasattr(channel, "send"):
            return None
        return channel

    async def finish_giveaway_create(
        self,
        interaction: discord.Interaction,
        *,
        owner_id: int,
        mode_key: str,
        override_channel_id: int | None,
    ) -> None:
        draft = self.pop_create_draft(owner_id)
        if not draft:
            await interaction.followup.send(
                "❌ Giveaway creation expired. Please run `/giveaway start` again.", ephemeral=True
            )
            return
        mode = GIVEAWAY_ENTRY_MODE_CHOICES.get(mode_key, GIVEAWAY_ENTRY_MODE_CHOICES["button"])
        post_channel_id = int(
            override_channel_id or draft["default_post_channel_id"] or draft["request_channel_id"]
        )
        post_channel = await self._resolve_post_channel(interaction, post_channel_id)
        if post_channel is None:
            await interaction.followup.send(
                "❌ The selected giveaway channel is invalid or inaccessible.", ephemeral=True
            )
            return
        now = datetime.now(timezone.utc)
        ends_at = now + timedelta(days=int(draft["duration_days"]))
        try:
            repo = FreeRaffleRepository(get_pool())
            raffle = await repo.create_raffle(
                guild_id=int(draft["guild_id"]),
                channel_id=post_channel_id,
                host_discord_id=int(draft["host_discord_id"]),
                prize_text=str(draft["prize_text"]),
                note_text=draft.get("note_text"),
                ends_at=ends_at,
                button_join_enabled=bool(mode["button_join_enabled"]),
                auto_entry_enabled=bool(mode["auto_entry_enabled"]),
                weighted_enabled=bool(mode["weighted_enabled"]),
                auto_entry_max_per_user=int(draft.get("auto_entry_max_per_user") or 1),
                messages_per_entry=int(draft.get("messages_per_entry") or AUTO_ENTRY_MESSAGE_STEP),
                role_bonus_rules=list(draft.get("role_bonus_rules") or []),
            )
            raffle_id = int(raffle["id"])
            embed = await self.build_raffle_embed(raffle)
            public_view = self.build_free_raffle_view(
                raffle_id,
                status=str(raffle.get("status") or ""),
                button_join_enabled=bool(raffle.get("button_join_enabled", False)),
            )
            public_message = await post_channel.send(embed=embed, view=public_view)
            await repo.set_message_id(raffle_id, int(public_message.id))
            interaction.client.dispatch(
                "giveaway_started",
                {"guild_id": int(draft["guild_id"]), "giveaway_id": raffle_id, "id": raffle_id},
            )
            self._last_host_controls_raffles[raffle_id] = dict(raffle)
            await interaction.followup.send(
                f"✅ Giveaway created in <#{post_channel_id}> with **{mode['label']}** mode.",
                ephemeral=True,
                view=self.host_controls_view(raffle_id),
            )
        except Exception:
            log.exception("Failed creating free raffle")
            await interaction.followup.send(
                "❌ Failed to create giveaway. Please try again.", ephemeral=True
            )

    async def resolve_thumbnail(self, prize_text: str) -> str | None:
        try:
            item = await TornItemsRepository(get_pool()).get_item_meta_by_name(prize_text)
        except Exception:
            log.exception("Failed resolving free raffle thumbnail for prize '%s'", prize_text)
            return None
        if not item:
            return None
        image = str(item.get("image_url") or "").strip()
        return image or None

    def _entry_mode_label(self, raffle: dict) -> str:
        button_join_enabled = bool(raffle.get("button_join_enabled", False))
        auto_entry_enabled = bool(raffle.get("auto_entry_enabled", False))
        weighted_enabled = bool(
            raffle.get("weighted_odds_enabled", raffle.get("weighted_enabled", False))
        )
        if button_join_enabled and weighted_enabled:
            return "Button Join + Weighted"
        if button_join_enabled:
            return "Button Join"
        if auto_entry_enabled and weighted_enabled:
            return "Auto Entry + Weighted"
        if auto_entry_enabled:
            return "Auto Entry"
        return "Unknown"

    def _entries_section(self, entry_count: int, entry_goal: int | None) -> str:
        if entry_goal and entry_goal > 0:
            progress = clamp_percent((entry_count / entry_goal) * 100)
            return f"Entries: **{entry_count} / {entry_goal}**\n`{render_text_progress_bar(progress, width=ENTRIES_PROGRESS_WIDTH)}`"
        return f"Entries: **{entry_count} / Unlimited**"

    def _time_section(
        self, created_at: datetime | None, ends_at: datetime | None, now: datetime
    ) -> str | None:
        if not isinstance(ends_at, datetime):
            return None
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        lines = [
            f"Ends in: **{format_remaining_time(ends_at, now)}**",
            f"Ends at: <t:{int(ends_at.timestamp())}:f>",
        ]
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            total_seconds = int((ends_at - created_at).total_seconds())
            elapsed_seconds = int((now - created_at).total_seconds())
            if total_seconds > 0:
                percent = clamp_percent((elapsed_seconds / total_seconds) * 100)
                lines.append(f"`{render_text_progress_bar(percent, width=ENTRIES_PROGRESS_WIDTH)}`")
        return "\n".join(lines)

    async def _build_personal_info_embed(self, raffle: dict, user_id: int) -> discord.Embed:
        repo = FreeRaffleRepository(get_pool())
        profile = await repo.get_auto_entry_progress(int(raffle["id"]), int(user_id))
        entry_row = await repo.get_entry(int(raffle["id"]), int(user_id))
        list_bonus_rules = getattr(repo, "list_role_bonus_rules", None)
        bonus_rules = (
            await list_bonus_rules(int(raffle["id"])) if callable(list_bonus_rules) else []
        )
        has_entry = entry_row is not None
        entry_weight = max(1, int((entry_row or {}).get("entry_weight") or 1)) if has_entry else 0
        weighted_enabled = bool(
            raffle.get("weighted_odds_enabled", raffle.get("weighted_enabled", False))
        )
        auto_entry_enabled = bool(raffle.get("auto_entry_enabled", False))
        button_join_enabled = bool(raffle.get("button_join_enabled", False))
        auto_max = max(1, int(raffle.get("auto_entry_max_per_user") or 1))
        messages_per_entry = max(
            1, int(raffle.get("messages_per_entry") or AUTO_ENTRY_MESSAGE_STEP)
        )
        progress_count = int(profile.get("qualifying_message_count") or 0)
        auto_entries_granted = int(profile.get("auto_entries_granted") or 0)
        messages_toward_next = min(progress_count, messages_per_entry)
        member = None
        if self.bot is not None:
            guild = self.bot.get_guild(int(raffle.get("guild_id") or 0))
            if guild is not None:
                member = guild.get_member(int(user_id))
        member_role_ids = {int(role.id) for role in getattr(member, "roles", [])}
        matching_bonus_rules = [
            rule for rule in bonus_rules if int(rule.get("role_id") or 0) in member_role_ids
        ]
        matching_bonus_total = sum(
            int(rule.get("bonus_entries_per_qualification") or 0) for rule in matching_bonus_rules
        )

        def _role_label(role_id: int) -> str:
            if member is not None:
                role = discord.utils.get(member.guild.roles, id=role_id)
                if role is not None:
                    return role.mention
            return f"<@&{role_id}>"

        description_lines: list[str] = []
        if auto_entry_enabled:
            description_lines.extend(
                [
                    "You must have at least 1 Coin.",
                    f"Every {messages_per_entry} qualifying messages gives 1 base entry.",
                    f"Bonus role entries stack per qualification: {'Enabled' if bonus_rules else 'None configured'}.",
                    f"Max auto entries: {auto_max}.",
                ]
            )
        if button_join_enabled:
            description_lines.append("Use the Enter Giveaway button to join instantly.")
        description_lines.append(
            f"Weighted entries are {'enabled' if weighted_enabled else 'off'}."
        )

        embed = discord.Embed(
            title=f"Info for Giveaway #{int(raffle['id'])}",
            description="\n".join(description_lines),
            color=_status_color(str(raffle.get("status") or ""), None),
        )
        progress_lines = []
        if auto_entry_enabled:
            coin_eligible = (
                "Eligible"
                if int((await self._get_coin_balance(int(raffle["guild_id"]), int(user_id))) or 0)
                >= 1
                else "Not eligible"
            )
            progress_lines.append(f"Coin Check: **{coin_eligible}**")
            progress_lines.append(
                f"Messages toward next entry: **{messages_toward_next} / {messages_per_entry}**"
            )
            progress_lines.append(f"Your entries: **{auto_entries_granted} / {auto_max}**")
            progress_lines.append(
                f"Your bonus this cycle: **+{matching_bonus_total} Entries**"
                if matching_bonus_total
                else "You have no bonus roles for this giveaway."
            )
        elif button_join_enabled:
            progress_lines.append(f"Your entries: **{1 if has_entry else 0}**")
        if weighted_enabled:
            progress_lines.append("Weighted mode: **Enabled**")
            if has_entry:
                progress_lines.append(f"Your current weight: **{entry_weight}**")
        embed.add_field(name="HOW IT WORKS", value="\n".join(description_lines), inline=False)
        if auto_entry_enabled:
            bonus_lines = [
                f"{_role_label(int(rule['role_id']))}: **+{int(rule.get('bonus_entries_per_qualification') or 0)} {'Entries' if int(rule.get('bonus_entries_per_qualification') or 0) != 1 else 'Entry'}**"
                for rule in bonus_rules
            ]
            if bonus_lines:
                embed.add_field(name="BONUS ROLES", value="\n".join(bonus_lines), inline=False)
            else:
                embed.add_field(
                    name="BONUS ROLES",
                    value="No bonus roles are configured for this giveaway.",
                    inline=False,
                )
        embed.add_field(
            name="YOUR PROGRESS",
            value="\n".join(progress_lines) or "No personal progress yet.",
            inline=False,
        )
        embed.set_footer(text="This panel only shows your personal progress.")
        return embed

    async def _get_coin_balance(self, guild_id: int, user_id: int) -> int:
        from repositories.engagement import EngagementRepository

        profile = await EngagementRepository(get_pool()).get_or_create_profile(guild_id, user_id)
        return int(profile.get("prize_token_balance") or 0)

    async def build_raffle_embed(self, raffle: dict) -> discord.Embed:
        raffle_id = int(raffle["id"])
        repo = FreeRaffleRepository(get_pool())
        winner_id = await repo.get_winner(raffle_id)
        entry_count = await repo.get_entry_count(raffle_id)
        status = _status_label(str(raffle.get("status") or ""), winner_id)
        color = _status_color(str(raffle.get("status") or ""), winner_id)
        prize_text = str(raffle.get("prize_text") or "Unknown Prize").strip() or "Unknown Prize"
        note_text = str(raffle.get("note_text") or "").strip()
        entry_mode = self._entry_mode_label(raffle)
        messages_per_entry = max(
            1, int(raffle.get("messages_per_entry") or AUTO_ENTRY_MESSAGE_STEP)
        )
        list_bonus_rules = getattr(repo, "list_role_bonus_rules", None)
        role_bonus_rules = await list_bonus_rules(raffle_id) if callable(list_bonus_rules) else []
        thumbnail_url = await self.resolve_thumbnail(prize_text)
        now = datetime.now(timezone.utc)
        created_at = raffle.get("created_at")
        ends_at = raffle.get("ends_at")
        is_timer_triggered = isinstance(ends_at, datetime)

        embed = discord.Embed(
            title=f"Giveaway for {prize_text}",
            color=color,
        )
        embed.add_field(name="PRIZE", value=prize_text, inline=False)
        embed.add_field(
            name="STATS",
            value="\n".join(
                [
                    f"Status: **{status}**",
                    self._entries_section(entry_count, None),
                    f"Mode: **{entry_mode}**",
                    f"Auto Entry: **{'Active' if bool(raffle.get('auto_entry_enabled', False)) else 'Off'}**",
                    f"Messages Per Entry: **{messages_per_entry}**"
                    if bool(raffle.get("auto_entry_enabled", False))
                    else "Messages Per Entry: **N/A**",
                    f"Role Bonuses: **{'Enabled' if role_bonus_rules else 'None'}**"
                    if bool(raffle.get("auto_entry_enabled", False))
                    else "Role Bonuses: **N/A**",
                ]
            ),
            inline=False,
        )
        if is_timer_triggered:
            time_value = self._time_section(created_at, ends_at, now)
            if time_value:
                embed.add_field(name="TIME", value=time_value, inline=False)
        if note_text:
            embed.add_field(name="NOTE", value=note_text, inline=False)
        if winner_id:
            embed.add_field(name="WINNER", value=f"<@{winner_id}>", inline=False)
        embed.set_footer(text=f"Last updated: {now.strftime('%Y-%m-%d %H:%M UTC')}")
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        return embed

    async def refresh_public_message(self, raffle_id: int) -> None:
        repo = FreeRaffleRepository(get_pool())
        raffle = await repo.get_raffle(raffle_id)
        if not raffle:
            return

        channel_id = raffle.get("channel_id")
        message_id = raffle.get("message_id")
        if not channel_id:
            return

        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except Exception as exc:
                log.error(
                    "Failed refreshing free raffle message: raffle_id=%s channel_id=%s message_id=%s error_type=%s error=%s",
                    raffle_id,
                    channel_id,
                    message_id,
                    type(exc).__name__,
                    exc,
                )
                return

        if not hasattr(channel, "get_partial_message"):
            log.error(
                "Failed refreshing free raffle message: raffle_id=%s channel_id=%s message_id=%s error_type=%s error=%s",
                raffle_id,
                channel_id,
                message_id,
                "UnsupportedChannel",
                "channel does not support partial messages",
            )
            return

        try:
            embed = await self.build_raffle_embed(raffle)
            view = self.build_free_raffle_view(
                raffle_id=int(raffle["id"]),
                host_discord_id=int(raffle["host_discord_id"]),
                status=str(raffle.get("status") or ""),
                button_join_enabled=bool(raffle.get("button_join_enabled", False)),
            )
            if message_id:
                try:
                    message = await channel.fetch_message(int(message_id))
                except Exception:
                    message = await channel.send(embed=embed, view=view)
                    await repo.set_message_id(raffle_id, int(message.id))
                    return
                await PANEL_EDIT_SAFETY.request_edit(
                    message,
                    embed=embed,
                    view=view,
                    min_interval_seconds=5,
                    force=str(raffle.get("status") or "").lower() != "active",
                )
                return
            message = await channel.send(embed=embed, view=view)
            await repo.set_message_id(raffle_id, int(message.id))
        except Exception as exc:
            log.error(
                "Failed refreshing free raffle message: raffle_id=%s channel_id=%s message_id=%s error_type=%s error=%s",
                raffle_id,
                channel_id,
                message_id,
                type(exc).__name__,
                exc,
            )
            return

    async def _send_ephemeral(self, interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
            return
        await interaction.response.send_message(message, ephemeral=True)

    async def handle_info(self, interaction: discord.Interaction, raffle_id: int) -> None:
        repo = FreeRaffleRepository(get_pool())
        raffle = await repo.get_raffle(raffle_id)
        if not raffle:
            await self._send_ephemeral(interaction, "Giveaway not found.")
            return
        embed = await self._build_personal_info_embed(raffle, int(interaction.user.id))
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def handle_enter(self, interaction: discord.Interaction, raffle_id: int) -> None:
        try:
            repo = FreeRaffleRepository(get_pool())
            raffle = await repo.get_raffle(raffle_id)
            if not raffle:
                await self._send_ephemeral(interaction, "Raffle not found.")
                return
            if str(raffle.get("status") or "").lower() != "active":
                await self._send_ephemeral(interaction, "Raffle is not active.")
                return
            if not bool(raffle.get("button_join_enabled", False)):
                await self._send_ephemeral(interaction, "This giveaway uses auto-entry only.")
                return

            inserted = await repo.add_entry_with_source(
                raffle_id,
                int(interaction.user.id),
                entry_source="button",
                entry_weight=1,
                dedupe_key=None,
            )
            if inserted:
                interaction.client.dispatch(
                    "giveaway_joined",
                    {
                        "guild_id": int(raffle.get("guild_id") or interaction.guild_id or 0),
                        "user_id": int(interaction.user.id),
                        "giveaway_id": int(raffle_id),
                        "entry_id": None,
                        "joined_at": datetime.now(timezone.utc),
                        "dedupe_key": f"giveaway_join:{raffle_id}:{int(interaction.user.id)}",
                    },
                )
                if interaction.message is not None:
                    refreshed_raffle = await repo.get_raffle(raffle_id)
                    if refreshed_raffle:
                        embed = await self.build_raffle_embed(refreshed_raffle)
                        view = self.build_free_raffle_view(
                            raffle_id=int(refreshed_raffle["id"]),
                            host_discord_id=int(refreshed_raffle["host_discord_id"]),
                            status=str(refreshed_raffle.get("status") or ""),
                            button_join_enabled=bool(
                                refreshed_raffle.get("button_join_enabled", False)
                            ),
                        )
                        await PANEL_EDIT_SAFETY.request_edit(
                            interaction.message,
                            embed=embed,
                            view=view,
                            min_interval_seconds=5,
                            force=False,
                        )
                    else:
                        await self.refresh_public_message(raffle_id)
                else:
                    await self.refresh_public_message(raffle_id)
                await self._send_ephemeral(interaction, "✅ You’re entered in the giveaway.")
                return
            await self._send_ephemeral(interaction, "You’re already entered in this giveaway.")
        except Exception:
            log.exception("Failed handling free raffle entry for raffle_id=%s", raffle_id)
            await self._send_ephemeral(interaction, "Failed to enter giveaway. Please try again.")

    async def _assert_host(self, interaction: discord.Interaction, raffle: dict | None) -> bool:
        if not raffle:
            await self._send_ephemeral(interaction, "Raffle not found.")
            return False
        is_host = int(interaction.user.id) == int(raffle["host_discord_id"])
        is_admin = (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.administrator
        )
        if not is_host and not is_admin:
            await self._send_ephemeral(interaction, "Only the host can do that.")
            return False
        return True

    async def _host_controls_response(
        self, interaction: discord.Interaction, message: str, raffle: dict
    ) -> None:
        self._last_host_controls_raffles[int(raffle["id"])] = dict(raffle)
        disabled = str(raffle.get("status") or "").lower() != "active"
        can_reroll = (
            str(raffle.get("status") or "").lower() == "ended"
            and await FreeRaffleRepository(get_pool()).get_winner(int(raffle["id"])) is not None
        )
        if interaction.response.is_done():
            await interaction.followup.send(
                message,
                ephemeral=True,
                view=self.host_controls_view(
                    int(raffle["id"]), disabled=disabled, can_reroll=can_reroll
                ),
            )
            return
        await interaction.response.send_message(
            message,
            ephemeral=True,
            view=self.host_controls_view(
                int(raffle["id"]), disabled=disabled, can_reroll=can_reroll
            ),
        )

    async def handle_cancel(self, interaction: discord.Interaction, raffle_id: int) -> None:
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
            repo = FreeRaffleRepository(get_pool())
            raffle = await repo.get_raffle(raffle_id)
            if not await self._assert_host(interaction, raffle):
                return

            if str(raffle.get("status") or "").lower() != "active":
                await self._send_ephemeral(interaction, "Raffle is not active.")
                return

            await repo.set_status(raffle_id, "cancelled", datetime.now(timezone.utc))
            await self.refresh_public_message(raffle_id)
            raffle = await repo.get_raffle(raffle_id) or raffle
            await self._host_controls_response(interaction, "Raffle cancelled.", raffle)
        except Exception:
            log.exception("Failed cancelling free raffle raffle_id=%s", raffle_id)
            await self._send_ephemeral(interaction, "Failed to cancel raffle. Please try again.")

    async def handle_end_now(self, interaction: discord.Interaction, raffle_id: int) -> None:
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
            repo = FreeRaffleRepository(get_pool())
            raffle = await repo.get_raffle(raffle_id)
            if not await self._assert_host(interaction, raffle):
                return
            result = await repo.draw_raffle_now(raffle_id)
            if result is None:
                await interaction.followup.send("❌ Giveaway is not active.", ephemeral=True)
                return
            ended_raffle = result["raffle"]
            await self.announce_raffle_result(ended_raffle, result["winner_id"])
            await self.refresh_public_message(raffle_id)
            await self._host_controls_response(
                interaction, "✅ Giveaway finalized immediately.", ended_raffle
            )
        except Exception:
            log.exception("Failed ending free raffle raffle_id=%s", raffle_id)
            await interaction.followup.send("❌ Failed to end giveaway right now.", ephemeral=True)

    async def handle_refresh_controls(
        self, interaction: discord.Interaction, raffle_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        repo = FreeRaffleRepository(get_pool())
        raffle = await repo.get_raffle(raffle_id)
        if not await self._assert_host(interaction, raffle):
            return
        await self.refresh_public_message(raffle_id)
        refreshed = await repo.get_raffle(raffle_id) or raffle
        await self._host_controls_response(interaction, "🔄 Giveaway panel refreshed.", refreshed)

    async def handle_view_entries(self, interaction: discord.Interaction, raffle_id: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        repo = FreeRaffleRepository(get_pool())
        raffle = await repo.get_raffle(raffle_id)
        if not await self._assert_host(interaction, raffle):
            return
        entries = await repo.list_entries(raffle_id)
        if not entries:
            await self._send_ephemeral(interaction, "No entries yet.")
            return
        lines = [
            f"<@{int(entry['discord_id'])}> — weight {int(entry.get('entry_weight') or 1)} via {entry.get('entry_source') or 'unknown'}"
            for entry in entries[:50]
        ]
        extra = "" if len(entries) <= 50 else f"\n…and {len(entries) - 50} more."
        await self._send_ephemeral(interaction, "📋 Entries:\n" + "\n".join(lines) + extra)

    async def handle_auto_settings(self, interaction: discord.Interaction, raffle_id: int) -> None:
        repo = FreeRaffleRepository(get_pool())
        raffle = await repo.get_raffle(raffle_id)
        if not await self._assert_host(interaction, raffle):
            return
        if not bool((raffle or {}).get("auto_entry_enabled", False)):
            await self._send_ephemeral(interaction, "This giveaway does not use auto entry.")
            return
        bonus_rules = await repo.list_role_bonus_rules(raffle_id)
        embed = self.build_auto_entry_admin_embed(
            raffle, bonus_rules, title=f"Auto Entry Settings for Giveaway #{raffle_id}"
        )
        embed.color = _status_color(
            str(raffle.get("status") or ""), await repo.get_winner(raffle_id)
        )
        view = AutoEntryRoleBonusManageView(self, raffle, bonus_rules)
        if interaction.response.is_done():
            await interaction.followup.send(
                embed=embed,
                ephemeral=True,
                view=view,
            )
            return
        await interaction.response.send_message(embed=embed, ephemeral=True, view=view)

    async def handle_reroll(self, interaction: discord.Interaction, raffle_id: int) -> None:
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
            repo = FreeRaffleRepository(get_pool())
            raffle = await repo.get_raffle(raffle_id)
            if not await self._assert_host(interaction, raffle):
                return
            if (
                str(raffle.get("status") or "").lower() != "ended"
                or await repo.get_winner(raffle_id) is None
            ):
                await interaction.followup.send(
                    "❌ Reroll is only available after the giveaway has ended with a winner.",
                    ephemeral=True,
                )
                return
            winner_id = await repo.reroll_winner(raffle_id)
            updated = await repo.get_raffle(raffle_id) or raffle
            await self.refresh_public_message(raffle_id)
            await self.announce_raffle_result(updated, winner_id)
            await self._host_controls_response(
                interaction, f"🎲 Winner rerolled to <@{winner_id}>.", updated
            )
        except Exception:
            log.exception("Failed rerolling free raffle raffle_id=%s", raffle_id)
            await interaction.followup.send("❌ Failed to reroll winner right now.", ephemeral=True)

    async def announce_raffle_result(self, raffle: dict, winner_id: int | None) -> None:
        channel = self.bot.get_channel(int(raffle["channel_id"]))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(raffle["channel_id"]))
            except Exception:
                channel = None
        if channel is None or not hasattr(channel, "send"):
            return

        if winner_id is None:
            await channel.send("🎉 Giveaway ended: No entries — no winner.")
            return

        guild = self.bot.get_guild(int(raffle["guild_id"]))
        winner_mention = f"<@{winner_id}>"
        if guild is not None and guild.get_member(winner_id) is None:
            winner_mention = f"<@{winner_id}> (ID: {winner_id})"
        await channel.send(f"🎉 Giveaway winner: {winner_mention}")

    async def process_expired_raffles(self) -> int:
        repo = FreeRaffleRepository(get_pool())
        now = datetime.now(timezone.utc)
        expired = await repo.list_expired_active_raffles(
            now=now, limit=FREE_RAFFLE_EXPIRY_BATCH_SIZE
        )
        processed = 0
        for raffle in expired:
            raffle_id = int(raffle["id"])
            try:
                result = await repo.draw_expired_raffle(raffle_id, now=now)
                if result is None:
                    continue

                ended_raffle = result["raffle"]
                winner_id = result["winner_id"]
                entries_count = int(result["entries_count"])
                log.info(
                    "Auto-drew free raffle id=%s winner_id=%s entries=%s",
                    raffle_id,
                    winner_id,
                    entries_count,
                )
                await self.announce_raffle_result(ended_raffle, winner_id)
                await self.refresh_public_message(raffle_id)
                processed += 1
            except Exception:
                log.exception(
                    "Failed processing automatic free raffle draw raffle_id=%s", raffle_id
                )
        if processed > 0:
            log.info("Free raffle expiration worker processed=%s", processed)
        else:
            log.debug("Free raffle expiration worker processed=0")
        return processed

    @tasks.loop(seconds=60)
    async def free_raffle_expiration_worker(self) -> None:
        if not db_is_initialized():
            return

        db = get_database()
        worker_slot = db_heavy_worker_slot("free_raffle.free_raffle_expiration_worker")
        await worker_slot.__aenter__()

        async def _run_once() -> int:
            return await self.process_expired_raffles()

        try:
            acquired, processed = await run_with_advisory_lock(
                db, "worker:free_raffle:expire", _run_once
            )
            if not acquired:
                return
            # process_expired_raffles already logs appropriately based on processed
            _ = processed
        except Exception:
            log.exception("Free raffle expiration worker error")
        finally:
            await worker_slot.__aexit__(None, None, None)

    @free_raffle_expiration_worker.before_loop
    async def before_free_raffle_expiration_worker(self) -> None:
        await wait_until_initialized(timeout=30.0)
        await self.bot.wait_until_ready()
        await sleep_startup_jitter("free_raffle.free_raffle_expiration_worker")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FreeRaffleCog(bot))
