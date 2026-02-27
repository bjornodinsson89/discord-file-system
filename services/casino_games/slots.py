from __future__ import annotations

import hashlib
import json
import logging
import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord

from repositories.casino_core import CasinoCoreRepository
from repositories.users import UsersRepository
from services.casino_core.settings import get_house_config
from utils import GuildSettingsRepository, get_database

JACKPOT_SYMBOL_ID = 9090
SLOTS_JACKPOT_POOL_KEY = "slots_jackpot"

log = logging.getLogger(__name__)

DEFAULT_SLOTS_CONFIG = {
    "config_version": 5,
    "enabled": True,
    "min_bet": 1,
    "max_bet": 3,
    "cooldown_seconds": 2,
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
    "reel_strip_order": [9090, 281, 197, 865, 366, 206],
    "reel_stop_counts": {
        "9090": 20,
        "281": 23,
        "197": 44,
        "865": 24,
        "366": 120,
        "206": 25,
    },
    "jackpot_multiplier": 50.0,
    "jackpot_contrib_bps": 300,
    "jackpot_safe_min": 200,
    "jackpot_safe_max": 2000,
    "jackpot_enforce_floor": False,
    "jackpot_display_mode": "max_bet_scaled",
    "rtp_target": 0.90,
    "payouts": {
        "triple": {"206": 40.0, "281": 20.0, "197": 10.0, "865": 6.0, "366": 4.0},
        "pair": {
            "281": 0.35,
            "197": 0.5,
            "366": 0.75,
            "865": 1.5,
            "206": 2.0,
            "9090": 0.0,
            "394": 0.2,
            "707": 0.25,
            "274": 0.35,
        },
    },
    "pair_left_bonus_mult": 1.10,
}

CANONICAL_SLOTS_CONFIG_KEYS = {
    "reel_strip_order",
    "reel_stop_counts",
    "jackpot_multiplier",
    "jackpot_contrib_bps",
    "jackpot_safe_min",
    "jackpot_safe_max",
    "jackpot_enforce_floor",
    "jackpot_display_mode",
    "rtp_target",
    "payouts",
}

EXPECTED_TRIPLE_PAYOUTS = {
    206: 40.0,
    281: 20.0,
    197: 10.0,
    865: 6.0,
    366: 4.0,
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


class ProvablyFairRNG:
    def __init__(self, server_seed: str, client_seed: str, spin_nonce: int):
        self.server_seed = str(server_seed)
        self.client_seed = str(client_seed)
        self.spin_nonce = int(spin_nonce)
        self.counter = 0

    def _digest(self) -> bytes:
        payload = f"{self.server_seed}:{self.client_seed}:{self.spin_nonce}:{self.counter}"
        digest = hashlib.sha256(payload.encode("utf-8")).digest()
        self.counter += 1
        return digest

    def randbelow(self, n: int) -> int:
        if int(n) <= 0:
            raise ValueError("n must be positive")
        return int.from_bytes(self._digest(), "big") % int(n)


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
        current_version = int(current.get("config_version") or 0)
        default_version = int(DEFAULT_SLOTS_CONFIG.get("config_version") or 0)
        merged = self._merge_defaults(DEFAULT_SLOTS_CONFIG, current)

        if current_version < default_version:
            for key in CANONICAL_SLOTS_CONFIG_KEYS:
                merged[key] = deepcopy(DEFAULT_SLOTS_CONFIG[key])
            merged["config_version"] = default_version

        if merged != current:
            games["slots"] = merged
            await self.settings_repo.upsert_settings(int(guild_id), casino_games=games)
        rtp = self._compute_theoretical_rtp(merged)
        log.debug(
            "slots_config_rtp guild_id=%s theoretical_rtp=%.6f target=%.4f",
            int(guild_id),
            float(rtp),
            float(merged.get("rtp_target") or 0.0),
        )
        return merged

    async def get_balance_and_pool(self, guild_id: int, discord_id: int) -> dict:
        cfg = await self.ensure_slots_config(guild_id)
        settings = await self.settings_repo.get_or_create(int(guild_id))
        house = get_house_config(settings)
        user_row = await self.users_repo.get_user_api_key(int(discord_id))
        wallet = await self.casino_repo.get_or_create_wallet(
            guild_id=int(guild_id),
            discord_id=int(discord_id),
            torn_user_id=int((user_row or {}).get("torn_user_id") or 0),
            torn_name=(user_row or {}).get("torn_name") or "",
        )
        jackpot_multiplier = float(cfg.get("jackpot_multiplier") or 0.0)
        async with self.casino_repo.acquire() as conn:
            pool_row = await self.casino_repo.get_or_create_pool(
                conn,
                guild_id=int(guild_id),
                pool_key=SLOTS_JACKPOT_POOL_KEY,
                seed_tokens=0,
                seed_millis=0,
            )
        pool_tokens = int(pool_row.get("tokens") or 0)
        return {
            "balance": int(wallet.get("balance_tokens") or 0),
            "jackpot_multiplier": jackpot_multiplier,
            "pool_tokens": int(pool_tokens),
            "pool_millis": 0,
            "config": cfg,
            "house_discord_id": int((house or {}).get("house_discord_id") or 0),
            "house_torn_id": int((house or {}).get("house_torn_id") or 0),
            "payout_proof_channel_id": int((house or {}).get("payout_proof_channel_id") or 0),
        }

    async def get_fairness_state(self, guild_id: int, discord_id: int) -> dict:
        async with self.casino_repo.acquire() as conn:
            server_seed = await self.casino_repo.get_or_create_slots_server_seed(conn, int(guild_id), for_update=False)
            player_state = await self.casino_repo.get_or_create_slots_player_state(
                conn,
                int(guild_id),
                int(discord_id),
                for_update=False,
            )
        return {
            "server_seed_hash": str(server_seed.get("server_seed_hash") or ""),
            "client_seed": str(player_state.get("client_seed") or ""),
            "nonce": int(player_state.get("nonce") or 0),
            "previous_server_seed": server_seed.get("previous_server_seed"),
            "previous_server_seed_hash": server_seed.get("previous_server_seed_hash"),
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

                if not self._matches_expected_triple_payouts(cfg):
                    log.warning(
                        "slots_config_invalid_triple_payouts guild_id=%s triple=%s; forcing default payouts",
                        int(guild_id),
                        (dict(cfg.get("payouts") or {}).get("triple") or {}),
                    )
                    cfg["payouts"] = deepcopy(DEFAULT_SLOTS_CONFIG["payouts"])
                    cfg["config_version"] = int(DEFAULT_SLOTS_CONFIG.get("config_version") or 0)
                    games["slots"] = cfg
                    await self.settings_repo.upsert_settings(int(guild_id), casino_games=games)

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
                current_balance = int(wallet.get("balance_tokens") or 0)
                if bet > current_balance:
                    raise SlotsError(f"Not enough tokens. Balance is {current_balance}, bet is {bet}.")

                available_at = await self.casino_repo.get_cooldown(
                    conn,
                    guild_id=int(guild_id),
                    discord_id=int(discord_id),
                    game_key="slots",
                )
                if available_at and available_at > now:
                    remaining = max(1, math.ceil((available_at - now).total_seconds()))
                    raise SlotsCooldownError(remaining_seconds=remaining)

                server_seed_row = await self.casino_repo.get_or_create_slots_server_seed(
                    conn,
                    int(guild_id),
                    for_update=True,
                )
                player_state = await self.casino_repo.get_or_create_slots_player_state(
                    conn,
                    int(guild_id),
                    int(discord_id),
                    for_update=True,
                )
                spin_nonce = int(player_state.get("nonce") or 0)
                server_seed = str(server_seed_row.get("server_seed") or "")
                server_seed_hash = str(server_seed_row.get("server_seed_hash") or "")
                client_seed = str(player_state.get("client_seed") or "")
                rng = ProvablyFairRNG(server_seed=server_seed, client_seed=client_seed, spin_nonce=spin_nonce)

                round_id = await self.casino_repo.create_round(
                    conn,
                    guild_id=int(guild_id),
                    wallet_id=int(wallet["id"]),
                    game_key="slots",
                    bet_tokens=bet,
                    result_json={
                        "status": "pending",
                        "provably_fair": {
                            "server_seed_hash": server_seed_hash,
                            "client_seed": client_seed,
                            "nonce": spin_nonce,
                        },
                    },
                )

                try:
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
                except ValueError as exc:
                    if "Insufficient wallet balance" in str(exc):
                        raise SlotsError("Not enough tokens for that bet. Deposit more or lower your bet.") from exc
                    raise

                pool_row = await self.casino_repo.get_or_create_pool(
                    conn,
                    guild_id=int(guild_id),
                    pool_key=SLOTS_JACKPOT_POOL_KEY,
                    seed_tokens=0,
                    seed_millis=0,
                )
                bps = int(cfg.get("jackpot_contrib_bps") or 0)
                jackpot_pool_contrib_tokens = max(0, (int(bet) * bps) // 10000)
                pool_before_contrib = int(pool_row.get("tokens") or 0)
                pool_tokens_post_contrib = int(pool_before_contrib)
                jackpot_overflow_to_house_tokens = 0
                if jackpot_pool_contrib_tokens > 0:
                    safe_max = max(0, int(cfg.get("jackpot_safe_max") or 0))
                    pool_after_add = int(pool_before_contrib) + int(jackpot_pool_contrib_tokens)
                    if safe_max > 0 and pool_after_add > safe_max:
                        jackpot_overflow_to_house_tokens = int(pool_after_add - safe_max)
                        pool_after_add = int(safe_max)
                    await conn.execute(
                        """
                        UPDATE casino_pools
                        SET tokens = $3, updated_at = NOW()
                        WHERE guild_id = $1 AND pool_key = $2
                        """,
                        int(guild_id),
                        SLOTS_JACKPOT_POOL_KEY,
                        int(pool_after_add),
                    )
                    pool_tokens_post_contrib = int(pool_after_add)

                virtual_reel = self._build_virtual_reel(cfg)
                total_stops = len(virtual_reel)
                reels = [virtual_reel[rng.randbelow(total_stops)] for _ in range(3)]
                payout, win_type, hit_symbol = self._calculate_payout(cfg, bet, reels)
                log.debug(
                    "slots_spin audit bet=%s reels=%s payout=%s win_type=%s hit_symbol=%s",
                    bet,
                    reels,
                    payout,
                    win_type,
                    hit_symbol,
                )
                jackpot_pool_before_tokens = 0
                jackpot_pool_after_tokens = 0
                jackpot_pool_display_tokens = 0
                triple_mult_used = None
                pair_mult_used = None
                payouts = dict(cfg.get("payouts") or {})
                triple = {int(k): float(v) for k, v in dict(payouts.get("triple") or {}).items()}
                pair = {int(k): float(v) for k, v in dict(payouts.get("pair") or {}).items()}
                if win_type == "jackpot":
                    triple_mult_used = float(cfg.get("jackpot_multiplier") or 0.0)
                elif win_type == "triple" and hit_symbol is not None:
                    triple_mult_used = float(triple.get(int(hit_symbol), 0.0))
                elif hit_symbol is not None:
                    pair_mult_used = float(pair.get(int(hit_symbol), 0.0))
                log.debug(
                    "slots_spin payout_debug bet=%s reels=%s win_type=%s hit=%s payout=%s triple_mult=%s pair_mult=%s",
                    bet,
                    reels,
                    win_type,
                    hit_symbol,
                    payout,
                    triple_mult_used,
                    pair_mult_used,
                )

                if win_type == "jackpot":
                    payout, jackpot_pool_before_tokens, jackpot_pool_after_tokens = await self.casino_repo.claim_pool_scaled(
                        conn,
                        guild_id=int(guild_id),
                        pool_key=SLOTS_JACKPOT_POOL_KEY,
                        bet=int(bet),
                        max_bet=int(cfg.get("max_bet") or 1),
                    )
                    jackpot_pool_display_tokens = int(jackpot_pool_after_tokens)
                    log.info(
                        "slots_result guild_id=%s discord_id=%s result_type=jackpot bet=%s reels=%s payout=%s pool_before=%s pool_after=%s",
                        int(guild_id),
                        int(discord_id),
                        int(bet),
                        reels,
                        int(payout),
                        int(jackpot_pool_before_tokens),
                        int(jackpot_pool_after_tokens),
                    )
                else:
                    jackpot_pool_before_tokens = int(pool_tokens_post_contrib)
                    jackpot_pool_after_tokens = int(pool_tokens_post_contrib)
                    jackpot_pool_display_tokens = int(pool_tokens_post_contrib)

                await conn.execute(
                    """
                    INSERT INTO casino_slots_accounting (
                        guild_id,
                        actor_discord_id,
                        round_id,
                        bet,
                        payout,
                        win_type,
                        jackpot_contrib,
                        jackpot_payout,
                        jackpot_overflow_to_house,
                        jackpot_pool_before,
                        jackpot_pool_after
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
                    )
                    """,
                    int(guild_id),
                    int(discord_id),
                    int(round_id),
                    int(bet),
                    int(payout),
                    str(win_type),
                    int(jackpot_pool_contrib_tokens),
                    int(payout if win_type == "jackpot" else 0),
                    int(jackpot_overflow_to_house_tokens),
                    int(jackpot_pool_before_tokens),
                    int(jackpot_pool_after_tokens),
                )

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
                        metadata={"game": "slots", "win_type": win_type, "hit_symbol": hit_symbol},
                    )

                new_nonce = spin_nonce + 1
                await conn.execute(
                    """
                    UPDATE casino_slots_player_state
                    SET nonce = $3,
                        updated_at = NOW()
                    WHERE guild_id = $1 AND discord_id = $2
                    """,
                    int(guild_id),
                    int(discord_id),
                    int(new_nonce),
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
                    "bet": bet,
                    "payout": payout,
                    "win_type": win_type,
                    "provably_fair": {
                        "server_seed_hash": server_seed_hash,
                        "client_seed": client_seed,
                        "nonce": spin_nonce,
                    },
                    "jackpot_pool_before_tokens": int(jackpot_pool_before_tokens),
                    "jackpot_pool_after_tokens": int(jackpot_pool_after_tokens),
                    "jackpot_pool_contrib_tokens": int(jackpot_pool_contrib_tokens),
                    "jackpot_pool_display_tokens": int(jackpot_pool_display_tokens),
                    "jackpot_overflow_to_house_tokens": int(jackpot_overflow_to_house_tokens),
                }
                if hit_symbol is not None:
                    result_json["hit_symbol"] = int(hit_symbol)
                await self.casino_repo.update_round(conn, round_id=round_id, payout_tokens=payout, result_json=result_json)

                spin_result = {
                    "bet": bet,
                    "payout": int(payout),
                    "net": int(payout) - bet,
                    "balance_after": int(wallet.get("balance_tokens") or 0),
                    "reels": reels,
                    "win_type": win_type,
                    "round_id": int(round_id),
                    "config": cfg,
                    "jackpot_multiplier": float(cfg.get("jackpot_multiplier") or 0.0),
                    "pool_tokens": int(jackpot_pool_display_tokens),
                    "pool_millis": 0,
                    "jackpot_pool_before_tokens": int(jackpot_pool_before_tokens),
                    "jackpot_pool_after_tokens": int(jackpot_pool_after_tokens),
                    "jackpot_pool_contrib_tokens": int(jackpot_pool_contrib_tokens),
                    "jackpot_pool_display_tokens": int(jackpot_pool_display_tokens),
                    "jackpot_overflow_to_house_tokens": int(jackpot_overflow_to_house_tokens),
                    "house_discord_id": int((house or {}).get("house_discord_id") or 0),
                    "house_torn_id": int((house or {}).get("house_torn_id") or 0),
                    "payout_proof_channel_id": int((house or {}).get("payout_proof_channel_id") or 0),
                    "server_seed_hash": server_seed_hash,
                    "client_seed": client_seed,
                    "nonce": spin_nonce,
                }

                big_wins_channel_id = int((house or {}).get("big_wins_channel_id") or 0)
                if big_wins_channel_id and win_type in {"jackpot", "triple"}:
                    announce_payload = {
                        "channel_id": big_wins_channel_id,
                        "payout": int(payout),
                        "discord_id": int(discord_id),
                        "win_type": str(win_type),
                        "reels": list(reels),
                        "round_id": int(round_id),
                    }
        if announce_payload and announce_payload.get("channel_id"):
            spin_result["announce"] = announce_payload
        return spin_result

    async def post_big_win_announce(self, interaction: discord.Interaction, result: dict) -> None:
        announce = result.get("announce") or {}
        channel_id = int(announce.get("channel_id") or 0)
        if not channel_id:
            return
        try:
            channel = interaction.client.get_channel(channel_id)
            if not channel:
                channel = await interaction.client.fetch_channel(channel_id)
            if not channel:
                return
            win_type = str(announce.get("win_type") or "")
            label = "BIG WIN"
            if win_type == "jackpot":
                label = "JACKPOT (9090×3)"
            elif win_type == "triple":
                label = "TRIPLE"
            reels = announce.get("reels") or []
            await channel.send(
                f"🎰 {label}! <@{announce['discord_id']}> won **{announce['payout']}** tokens "
                f"(round #{announce.get('round_id')}, reels: {reels})."
            )
        except Exception as exc:
            log.warning("Big win announcement failed channel_id=%s round_id=%s: %s", channel_id, announce.get("round_id"), exc)

    def _build_virtual_reel(self, cfg: dict) -> list[int]:
        order = list(cfg.get("reel_strip_order") or [])
        counts = dict(cfg.get("reel_stop_counts") or {})
        if not order:
            raise SlotsError("Slots reel_strip_order is empty.")

        virtual_reel: list[int] = []
        for raw_symbol in order:
            if not isinstance(raw_symbol, int):
                raise SlotsError("Slots reel_strip_order must contain integer item IDs.")
            count = counts.get(str(raw_symbol), counts.get(raw_symbol))
            try:
                count_int = int(count)
            except Exception as exc:
                raise SlotsError(f"Invalid reel stop count for symbol {raw_symbol}.") from exc
            if count_int <= 0:
                raise SlotsError(f"Reel stop count must be > 0 for symbol {raw_symbol}.")
            virtual_reel.extend([int(raw_symbol)] * count_int)

        if len(virtual_reel) < 2:
            raise SlotsError("Slots virtual reel must contain at least two stops.")
        return virtual_reel

    def _compute_theoretical_rtp(self, cfg: dict) -> float:
        counts = {int(k): int(v) for k, v in dict(cfg.get("reel_stop_counts") or {}).items()}
        total = sum(max(0, c) for c in counts.values())
        if total <= 0:
            raise SlotsError("Invalid reel_stop_counts configuration.")

        payouts = dict(cfg.get("payouts") or {})
        triple_cfg = {int(k): float(v) for k, v in dict(payouts.get("triple") or {}).items()}
        pair_cfg = {int(k): float(v) for k, v in dict(payouts.get("pair") or {}).items()}
        jackpot_mult = float(cfg.get("jackpot_multiplier") or 0.0)

        rtp = 0.0
        for symbol_id, stop_count in counts.items():
            p_i = float(stop_count) / float(total)
            triple_mult = jackpot_mult if symbol_id == JACKPOT_SYMBOL_ID else float(triple_cfg.get(symbol_id, 0.0))
            pair_mult = float(pair_cfg.get(symbol_id, 0.0))
            rtp += (p_i**3) * triple_mult
            rtp += (3.0 * (p_i**2) * (1.0 - p_i)) * pair_mult
        return rtp

    def _round_half_up(self, x: float) -> int:
        return int(math.floor(float(x) + 0.5))

    def _calculate_payout(self, config: dict, bet: int, reels: list[int]) -> tuple[int, str, int | None]:
        payouts = dict(config.get("payouts") or {})
        triple = {int(k): float(v) for k, v in dict(payouts.get("triple") or {}).items()}
        pair = {int(k): float(v) for k, v in dict(payouts.get("pair") or {}).items()}
        if reels[0] == reels[1] == reels[2]:
            item = int(reels[0])
            if item == JACKPOT_SYMBOL_ID:
                return 0, "jackpot", item
            mult = float(triple.get(item, 0.0))
            return int(math.floor(bet * mult)), "triple", item

        left_pair = reels[0] == reels[1] and reels[2] != reels[0]
        right_pair = reels[1] == reels[2] and reels[0] != reels[1]
        split_pair = reels[0] == reels[2] and reels[1] != reels[0]
        if left_pair or right_pair or split_pair:
            if left_pair:
                pair_item = int(reels[0])
                pair_kind = "left"
            elif right_pair:
                pair_item = int(reels[1])
                pair_kind = "right"
            else:
                pair_item = int(reels[0])
                pair_kind = "split"

            base_mult = float(pair.get(pair_item, 0.0))
            left_bonus_mult = float(config.get("pair_left_bonus_mult") or 1.10)
            effective_mult = base_mult * (left_bonus_mult if pair_kind == "left" else 1.0)

            payout = self._round_half_up(bet * effective_mult)
            if effective_mult > 0:
                payout = max(1, int(payout))
            else:
                payout = int(payout)
            if payout == int(bet):
                return payout, "push", pair_item
            return payout, "small", pair_item

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

    def _matches_expected_triple_payouts(self, cfg: dict) -> bool:
        triple = {int(k): float(v) for k, v in dict((dict(cfg.get("payouts") or {}).get("triple") or {})).items()}
        for symbol_id, expected_multiplier in EXPECTED_TRIPLE_PAYOUTS.items():
            if float(triple.get(int(symbol_id), 0.0)) != float(expected_multiplier):
                return False
        return True
