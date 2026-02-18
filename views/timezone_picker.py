from __future__ import annotations

import discord
import asyncpg
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from repositories.users import UsersRepository
from utils import get_database
from utils.embeds import create_error_embed
from utils.timezones import REGION_LABELS, get_region_timezones

PAGE_SIZE = 25

log = logging.getLogger("happy_jumper")


async def send_timezone_picker(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="Set Timezone",
        description="Select your region, then choose your timezone.",
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, view=RegionSelectView(owner_discord_id=interaction.user.id), ephemeral=True)


class RegionDropdown(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Choose a region",
            custom_id="tz_region",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=REGION_LABELS[key], value=key)
                for key in ("americas", "europe", "africa", "asia", "oceania", "utc_offsets")
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, RegionSelectView):
            await interaction.response.send_message("Unexpected state.", ephemeral=True)
            return
        if int(interaction.user.id) != view.owner_discord_id:
            await interaction.response.send_message("This picker belongs to a different user.", ephemeral=True)
            return

        selected_region = str(self.values[0])
        await interaction.response.edit_message(
            embed=timezone_embed(selected_region, page=0),
            view=TimezoneSelectView(owner_discord_id=view.owner_discord_id, selected_region=selected_region, current_page=0),
        )


class RegionSelectView(discord.ui.View):
    def __init__(self, *, owner_discord_id: int):
        super().__init__(timeout=900)
        self.owner_discord_id = int(owner_discord_id)
        self.add_item(RegionDropdown())


class TimezoneDropdown(discord.ui.Select):
    def __init__(self, *, region: str, page: int):
        zones = get_region_timezones(region)
        start = page * PAGE_SIZE
        page_values = zones[start:start + PAGE_SIZE]
        options = [discord.SelectOption(label=zone, value=zone) for zone in page_values]
        super().__init__(
            placeholder="Choose timezone",
            custom_id=f"tz_pick:{region}:{page}",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, TimezoneSelectView):
            await interaction.response.send_message("Unexpected state.", ephemeral=True)
            return
        if int(interaction.user.id) != view.owner_discord_id:
            await interaction.response.send_message("This picker belongs to a different user.", ephemeral=True)
            return

        timezone_name = str(self.values[0])
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            await interaction.response.send_message(
                embed=create_error_embed("Invalid timezone", "Selected timezone is not valid."),
                ephemeral=True,
            )
            return

        db = get_database()
        users_repo = UsersRepository(db.pool)
        try:
            await users_repo.update_timezone(int(interaction.user.id), timezone_name)
        except asyncpg.UndefinedColumnError:
            log.exception("Failed saving timezone because user_api_keys.timezone_name column is missing")
            await interaction.response.send_message(
                embed=create_error_embed(
                    "Timezone unavailable",
                    "Database migration missing: user_api_keys.timezone_name. Apply migrations and redeploy.",
                ),
                ephemeral=True,
            )
            return
        except RuntimeError as exc:
            if "user_api_keys.timezone_name" not in str(exc):
                raise
            log.exception("Failed saving timezone because user_api_keys.timezone_name column is missing")
            await interaction.response.send_message(
                embed=create_error_embed(
                    "Timezone unavailable",
                    "Database migration missing: user_api_keys.timezone_name. Apply migrations and redeploy.",
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Set Timezone",
            description=f"✅ Timezone set to {timezone_name}",
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(
            embed=embed,
            view=TimezoneChangeAgainView(owner_discord_id=view.owner_discord_id),
        )


class TimezoneSelectView(discord.ui.View):
    def __init__(self, *, owner_discord_id: int, selected_region: str, current_page: int):
        super().__init__(timeout=900)
        self.owner_discord_id = int(owner_discord_id)
        self.selected_region = selected_region
        self.current_page = int(current_page)

        zones = get_region_timezones(selected_region)
        total_pages = max(1, (len(zones) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.current_page = max(0, min(self.current_page, total_pages - 1))

        self.add_item(TimezoneDropdown(region=self.selected_region, page=self.current_page))

        has_prev = self.current_page > 0
        has_next = self.current_page < (total_pages - 1)

        prev_button = discord.ui.Button(
            label="Prev",
            style=discord.ButtonStyle.secondary,
            custom_id=f"tz_prev:{self.selected_region}:{self.current_page}",
            disabled=not has_prev,
        )
        next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            custom_id=f"tz_next:{self.selected_region}:{self.current_page}",
            disabled=not has_next,
        )
        back_button = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            custom_id="tz_back",
        )

        prev_button.callback = self.prev_page
        next_button.callback = self.next_page
        back_button.callback = self.back_to_regions

        self.add_item(prev_button)
        self.add_item(next_button)
        self.add_item(back_button)

    async def prev_page(self, interaction: discord.Interaction):
        if int(interaction.user.id) != self.owner_discord_id:
            await interaction.response.send_message("This picker belongs to a different user.", ephemeral=True)
            return
        next_page = max(0, self.current_page - 1)
        await interaction.response.edit_message(
            embed=timezone_embed(self.selected_region, page=next_page),
            view=TimezoneSelectView(
                owner_discord_id=self.owner_discord_id,
                selected_region=self.selected_region,
                current_page=next_page,
            ),
        )

    async def next_page(self, interaction: discord.Interaction):
        if int(interaction.user.id) != self.owner_discord_id:
            await interaction.response.send_message("This picker belongs to a different user.", ephemeral=True)
            return
        zones = get_region_timezones(self.selected_region)
        total_pages = max(1, (len(zones) + PAGE_SIZE - 1) // PAGE_SIZE)
        next_page = min(total_pages - 1, self.current_page + 1)
        await interaction.response.edit_message(
            embed=timezone_embed(self.selected_region, page=next_page),
            view=TimezoneSelectView(
                owner_discord_id=self.owner_discord_id,
                selected_region=self.selected_region,
                current_page=next_page,
            ),
        )

    async def back_to_regions(self, interaction: discord.Interaction):
        if int(interaction.user.id) != self.owner_discord_id:
            await interaction.response.send_message("This picker belongs to a different user.", ephemeral=True)
            return
        embed = discord.Embed(
            title="Set Timezone",
            description="Select your region, then choose your timezone.",
            color=discord.Color.blurple(),
        )
        await interaction.response.edit_message(
            embed=embed,
            view=RegionSelectView(owner_discord_id=self.owner_discord_id),
        )


class TimezoneChangeAgainView(discord.ui.View):
    def __init__(self, *, owner_discord_id: int):
        super().__init__(timeout=900)
        self.owner_discord_id = int(owner_discord_id)

    @discord.ui.button(label="Change again", style=discord.ButtonStyle.primary, custom_id="tz_again")
    async def change_again(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if int(interaction.user.id) != self.owner_discord_id:
            await interaction.response.send_message("This picker belongs to a different user.", ephemeral=True)
            return
        embed = discord.Embed(
            title="Set Timezone",
            description="Select your region, then choose your timezone.",
            color=discord.Color.blurple(),
        )
        await interaction.response.edit_message(embed=embed, view=RegionSelectView(owner_discord_id=self.owner_discord_id))


class TimezonePromptView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Set Timezone", style=discord.ButtonStyle.primary, custom_id="tz_open")
    async def open_picker(self, interaction: discord.Interaction, _button: discord.ui.Button):
        db = get_database()
        users_repo = UsersRepository(db.pool)
        existing = await users_repo.get_user_api_key(interaction.user.id)
        if not existing:
            await interaction.response.send_message(
                "Register your Torn API key first using /set_api_key.",
                ephemeral=True,
            )
            return

        await send_timezone_picker(interaction)


def timezone_embed(region: str, *, page: int) -> discord.Embed:
    zones = get_region_timezones(region)
    total_pages = max(1, (len(zones) + PAGE_SIZE - 1) // PAGE_SIZE)
    label = REGION_LABELS.get(region, region)
    start = page * PAGE_SIZE
    end = min(len(zones), start + PAGE_SIZE)
    return discord.Embed(
        title="Set Timezone",
        description=(
            f"Region: **{label}**\n"
            f"Choose your timezone (Page {page + 1}/{total_pages}, {start + 1}-{end} of {len(zones)})."
        ),
        color=discord.Color.blurple(),
    )
