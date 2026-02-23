from __future__ import annotations

import math
import random
import uuid
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord

from repositories.casino_core import CasinoCoreRepository
from repositories.users import UsersRepository
from services.casino_core.settings import get_house_config
from utils import GuildSettingsRepository, get_database

SLOTS_POOL_KEY = "slots_jackpot"

DEFAULT_SLOTS_CONFIG = {
    "enabled": True,
    "min_bet": 1,
    "max_bet": 2,
    "cooldown_seconds": 2,
    "jackpot_contrib_bps": 300,
    "jackpot_seed_tokens": 10,
    "jackpot_seed_millis": 0,
    "announce_jackpot": True,
    "announce_threshold_tokens": 50,
    "animate": True,
    "animation_frames": 6,
    "emoji_map": {
        "394": "<:Brick:1474962776312250712>",
        "274": "<:PandaPlushie:1474955807211917503>",
        "281": "<:LionPlushie:1474955767462494249>",
        "865": "<:PoisonMistletoe:1474956245948563456>",
        "707": "<:Lumpofcole:1474956040398311445>",
        "197": "<:Ecstacy:1474962567251099770>",
        "366": "<:Edvd:1474962843861389374>",
        "206": "<:xanax:1474963040880431156>",
    },
    "animation_total_frames": 15,
    "animation_delay_ms": 120,
    "animation_lock_left": 5,
    "animation_lock_mid": 9,
    "animation_lock_right": 12,
    "symbols": [
        {"item_id": 9090, "name": "Happy Jumper Bot", "weight": 2},
        {"item_id": 366, "name": "Erotic DVD", "weight": 30},
        {"item_id": 281, "name": "Lion Plushie", "weight": 18},
        {"item_id": 865, "name": "Poison Mistletoe", "weight": 12},
        {"item_id": 197, "name": "Ecstasy", "weight": 10},
        {"item_id": 206, "name": "Xanax", "weight": 6},
    ],
    "payouts": {
        "triple": {"9090": 1.0, "206": 3.0, "281": 4.0, "197": 6.0, "366": 8.0, "865": 10.0},
        "pair": {"394": 0.2, "707": 0.25, "274": 0.35, "281": 0.35, "197": 0.5, "366": 0.75, "865": 1.5, "206": 2.0},
        "xanax_tease": 0.1,
    },
}


@dataclass(frozen=True)
class SlotsPayoutConfig:
    triple: dict[int, float]


@dataclass(frozen=True)
class SlotsDisplayConfig:
    payouts: SlotsPayoutConfig


SLOT_CONFIG = SlotsDisplayConfig(
    payouts=SlotsPayoutConfig(
        triple={
            int(symbol_id): float(multiplier)
            for symbol_id, multiplier in (DEFAULT_SLOTS_CONFIG.get("payouts") or {}).get("triple", {}).items()
        }
    )
)


class SlotsError(Exception):
    pass


@dataclass
class SlotsCooldownError(SlotsError):
    remaining_seconds: int


class CasinoSlotsService:
    def __init__(self, pool, settings_repo=None, users_repo=None, casino_repo=None, torn_client_factory=None):
        self.pool = pool
        self.settings_repo = settings_repo or GuildSettingsRepository(get_database())
        self.users_repo = users_repo or UsersRepository(pool)
        self.casino_repo = casino_repo or CasinoCoreRepository(pool)
        self.torn_client_factory = torn_client_factory

    async def ensure_slots_config(self, guild_id: int) -> dict:
        row = await self.settings_repo.get_or_create(int(guild_id))
        games = self._coerce_config_dict((row or {}).get("casino_games"))
        current = self._coerce_config_dict(games.get("slots"))
        merged = self._merge_defaults(DEFAULT_SLOTS_CONFIG, current)
        if merged != current:
            games["slots"] = merged
            await self.settings_repo.upsert_settings(int(guild_id), casino_games=games)
        return merged

    async def get_balance_and_pool(self, guild_id: int, discord_id: int) -> dict:
        cfg = await self.ensure_slots_config(guild_id)
        user_row = await self.users_repo.get_user_api_key(int(discord_id))
        wallet = await self.casino_repo.get_or_create_wallet(
            guild_id=int(guild_id),
            discord_id=int(discord_id),
            torn_user_id=int((user_row or {}).get("torn_user_id") or 0),
            torn_name=(user_row or {}).get("torn_name") or "",
        )
        async with self.casino_repo.acquire() as conn:
            pool = await self.casino_repo.get_or_create_pool(
                conn,
                guild_id=int(guild_id),
                pool_key=SLOTS_POOL_KEY,
                seed_tokens=int(cfg.get("jackpot_seed_tokens") or 0),
                seed_millis=int(cfg.get("jackpot_seed_millis") or 0),
            )
        return {
            "balance": int(wallet.get("balance_tokens") or 0),
            "pool_tokens": int(pool.get("tokens") or 0),
            "pool_millis": int(pool.get("millis") or 0),
            "config": cfg,
        }

    async def spin(self, guild_id: int, discord_id: int, bet: int) -> dict:
        bet = int(bet)
        now = datetime.now(tz=timezone.utc)
        announce_payload: dict | None = None

        async with self.casino_repo.acquire() as conn:
            async with conn.transaction():
                settings = await self.settings_repo.get_or_create(int(guild_id))
                house = get_house_config(settings)
                if not settings.get("casino_enabled"):
                    raise SlotsError("Casino is disabled. Ask admins to run /back_of_house and enable casino.")

                games = self._coerce_config_dict((settings or {}).get("casino_games"))
                cfg = self._merge_defaults(DEFAULT_SLOTS_CONFIG, self._coerce_config_dict(games.get("slots")))
                if not cfg.get("enabled", True):
                    raise SlotsError("Slots is disabled in this server.")

                min_bet = int(cfg.get("min_bet") or 1)
                max_bet = int(cfg.get("max_bet") or min_bet)
                if bet < min_bet or bet > max_bet:
                    raise SlotsError(f"Bet must be between {min_bet} and {max_bet} tokens.")

                user_row = await self.users_repo.get_user_api_key(int(discord_id))
                wallet = await self.casino_repo.get_or_create_wallet(
                    guild_id=int(guild_id),
                    discord_id=int(discord_id),
                    torn_user_id=int((user_row or {}).get("torn_user_id") or 0),
                    torn_name=(user_row or {}).get("torn_name") or "",
                )

                available_at = await self.casino_repo.get_cooldown(
                    conn,
                    guild_id=int(guild_id),
                    discord_id=int(discord_id),
                    game_key="slots",
                )
                if available_at and available_at > now:
                    remaining = max(1, math.ceil((available_at - now).total_seconds()))
                    raise SlotsCooldownError(remaining_seconds=remaining)

                pool_before = await self.casino_repo.get_or_create_pool(
                    conn,
                    guild_id=int(guild_id),
                    pool_key=SLOTS_POOL_KEY,
                    seed_tokens=int(cfg.get("jackpot_seed_tokens") or 0),
                    seed_millis=int(cfg.get("jackpot_seed_millis") or 0),
                )

                nonce = str(uuid.uuid4())
                round_id = await self.casino_repo.create_round(
                    conn,
                    guild_id=int(guild_id),
                    wallet_id=int(wallet["id"]),
                    game_key="slots",
                    bet_tokens=bet,
                    result_json={"status": "pending", "nonce": nonce},
                )

                wallet = await self.casino_repo.apply_ledger_entry_atomic(
                    conn,
                    guild_id=int(guild_id),
                    wallet_id=int(wallet["id"]),
                    entry_type="wager_debit",
                    amount_tokens=-bet,
                    idempotency_key=f"slots:{round_id}:bet",
                    ref_type="casino_game_rounds",
                    ref_id=round_id,
                    metadata={"game": "slots"},
                )

                contrib_millis = int((bet * int(cfg.get("jackpot_contrib_bps") or 0) * 1000) // 10000)
                pool_after_contrib = await self.casino_repo.add_to_pool(
                    conn,
                    guild_id=int(guild_id),
                    pool_key=SLOTS_POOL_KEY,
                    add_tokens=0,
                    add_millis=contrib_millis,
                )

                reels = self._roll_reels(cfg)
                payout, win_type, pair_item = self._calculate_payout(cfg, bet, reels)
                final_pool_tokens = int(pool_after_contrib["tokens"])
                final_pool_millis = int(pool_after_contrib["millis"])

                if win_type == "jackpot":
                    claim_tokens, _claim_millis, reset_tokens, reset_millis = await self.casino_repo.claim_pool(
                        conn,
                        guild_id=int(guild_id),
                        pool_key=SLOTS_POOL_KEY,
                        reset_seed_tokens=int(cfg.get("jackpot_seed_tokens") or 0),
                        reset_seed_millis=int(cfg.get("jackpot_seed_millis") or 0),
                    )
                    payout = int(claim_tokens)
                    final_pool_tokens = int(reset_tokens)
                    final_pool_millis = int(reset_millis)

                if payout > 0:
                    wallet = await self.casino_repo.apply_ledger_entry_atomic(
                        conn,
                        guild_id=int(guild_id),
                        wallet_id=int(wallet["id"]),
                        entry_type="payout_credit",
                        amount_tokens=int(payout),
                        idempotency_key=f"slots:{round_id}:payout",
                        ref_type="casino_game_rounds",
                        ref_id=round_id,
                        metadata={"game": "slots", "win_type": win_type, "pair_item": pair_item},
                    )

                cooldown_seconds = max(0, int(cfg.get("cooldown_seconds") or 0))
                await self.casino_repo.set_cooldown(
                    conn,
                    guild_id=int(guild_id),
                    discord_id=int(discord_id),
                    game_key="slots",
                    available_at=now + timedelta(seconds=cooldown_seconds),
                )

                result_json = {
                    "round_id": round_id,
                    "timestamp": now.isoformat(),
                    "reels": reels,
                    "win_type": win_type,
                    "bet": bet,
                    "payout": payout,
                    "pool_before": {"tokens": int(pool_before["tokens"]), "millis": int(pool_before["millis"])},
                    "contrib_added": {"tokens": 0, "millis": contrib_millis},
                    "pool_after": {"tokens": final_pool_tokens, "millis": final_pool_millis},
                }
                await self.casino_repo.update_round(conn, round_id=round_id, payout_tokens=payout, result_json=result_json)

                spin_result = {
                    "bet": bet,
                    "payout": int(payout),
                    "net": int(payout) - bet,
                    "balance_after": int(wallet.get("balance_tokens") or 0),
                    "reels": reels,
                    "win_type": win_type,
                    "pool_after_tokens": final_pool_tokens,
                    "pool_after_millis": final_pool_millis,
                    "pool_before_tokens": int(pool_before["tokens"]),
                    "pool_before_millis": int(pool_before["millis"]),
                    "contrib_millis": contrib_millis,
                    "round_id": int(round_id),
                    "config": cfg,
                }

                if cfg.get("announce_jackpot") and win_type == "jackpot":
                    announce_payload = {
                        "channel_id": int((house or {}).get("announce_channel_id") or 0),
                        "payout": int(payout),
                        "discord_id": int(discord_id),
                    }

        if announce_payload and announce_payload.get("channel_id"):
            spin_result["announce"] = announce_payload
        return spin_result

    async def post_jackpot_announce(self, interaction: discord.Interaction, result: dict) -> None:
        announce = result.get("announce") or {}
        channel_id = int(announce.get("channel_id") or 0)
        if not channel_id:
            return
        channel = interaction.client.get_channel(channel_id)
        if not channel:
            channel = await interaction.client.fetch_channel(channel_id)
        if channel:
            await channel.send(f"🎰 JACKPOT! <@{announce['discord_id']}> won **{announce['payout']}** tokens in Slots!")

    def _roll_reels(self, config: dict) -> list[int]:
        symbols = list(config.get("symbols") or [])
        ids = [int(s.get("item_id")) for s in symbols]
        weights = [max(0, int(s.get("weight") or 0)) for s in symbols]
        if not ids or sum(weights) <= 0:
            raise SlotsError("Slots symbol configuration is invalid.")
        return [random.choices(ids, weights=weights, k=1)[0] for _ in range(3)]

    def _calculate_payout(self, config: dict, bet: int, reels: list[int]) -> tuple[int, str, int | None]:
        payouts = dict(config.get("payouts") or {})
        triple = dict(payouts.get("triple") or {})
        pair = dict(payouts.get("pair") or {})
        xanax_tease = float(payouts.get("xanax_tease") or 0)

        if reels[0] == reels[1] == reels[2] == 9090:
            return 0, "jackpot", 9090
        if reels[0] == reels[1] == reels[2]:
            item = reels[0]
            mult = float(triple.get(str(item), 0))
            return int(math.floor(bet * mult)), "triple", item

        counts: dict[int, int] = {}
        for rid in reels:
            counts[rid] = counts.get(rid, 0) + 1
        pair_item = next((k for k, v in counts.items() if v == 2), None)
        if pair_item is not None:
            mult = float(pair.get(str(pair_item), 0))
            payout = int(math.floor(bet * mult))
            if bet > 0 and payout <= 0:
                payout = 1
            return payout, "pair", pair_item

        if 206 in reels:
            return int(math.floor(bet * xanax_tease)), "xanax_tease", 206
        return 0, "loss", None

    def _merge_defaults(self, defaults: dict, current: dict) -> dict:
        out: dict = {}
        for key, dv in defaults.items():
            if isinstance(dv, dict):
                out[key] = self._merge_defaults(dv, dict(current.get(key) or {}))
            elif isinstance(dv, list):
                out[key] = current.get(key) if isinstance(current.get(key), list) else dv
            else:
                out[key] = current.get(key, dv)
        for key, value in current.items():
            if key not in out:
                out[key] = value
        return out

    def _coerce_config_dict(self, value) -> dict:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except Exception:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}
