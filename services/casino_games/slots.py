from __future__ import annotations

import math
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord

from repositories.casino_core import CasinoCoreRepository
from repositories.users import UsersRepository
from services.casino_core.settings import get_house_config
from utils import GuildSettingsRepository, get_database

SLOTS_POOL_KEY = "slots_jackpot"
JACKPOT_SYMBOL_ID = 9090

ODDS_PUSH = 0.45
ODDS_SMALL = 0.10
ODDS_TRIPLE = 0.02
ODDS_JACKPOT = 0.005
ODDS_DOUBLE_JP = 0.02
ODDS_LOSS = 1.0 - (ODDS_PUSH + ODDS_SMALL + ODDS_TRIPLE + ODDS_JACKPOT + ODDS_DOUBLE_JP)

RETENTION_GAME_KEY = "slots"
FIRST_FIVE_PLAYS_DOUBLE_BOOST = 0.06
LOSS_STREAK_PUSH_THRESHOLD = 3
MIN_POOL_FOR_DOUBLE = 10

TRIPLE_TIER_WEIGHTS = [0.525, 0.275, 0.125, 0.05, 0.025]

if ODDS_LOSS < 0.3:
    raise ValueError("Slots odds configuration is invalid: ODDS_LOSS must be at least 0.3.")


log = logging.getLogger(__name__)

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

    def random_float(self) -> float:
        return int.from_bytes(self._digest(), "big") / float(1 << 256)

    def choice(self, seq):
        if not seq:
            raise IndexError("Cannot choose from an empty sequence")
        return seq[self.randbelow(len(seq))]

    def shuffle(self, values: list) -> None:
        for i in range(len(values) - 1, 0, -1):
            j = self.randbelow(i + 1)
            values[i], values[j] = values[j], values[i]

    def sample(self, seq, k: int):
        if k < 0 or k > len(seq):
            raise ValueError("Sample larger than population or is negative")
        copied = list(seq)
        self.shuffle(copied)
        return copied[:k]


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
        settings = await self.settings_repo.get_or_create(int(guild_id))
        house = get_house_config(settings)
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

    async def set_client_seed(self, guild_id: int, discord_id: int, client_seed: str) -> dict:
        cleaned = str(client_seed).strip()
        if len(cleaned) < 6 or len(cleaned) > 64:
            raise SlotsError("Client seed must be between 6 and 64 characters.")
        if "\n" in cleaned or "\r" in cleaned:
            raise SlotsError("Client seed cannot contain newlines.")
        if not all(ch.isprintable() for ch in cleaned):
            raise SlotsError("Client seed must use printable characters only.")

        async with self.casino_repo.acquire() as conn:
            async with conn.transaction():
                row = await self.casino_repo.set_slots_client_seed(
                    conn,
                    int(guild_id),
                    int(discord_id),
                    cleaned,
                )
                server_seed = await self.casino_repo.get_or_create_slots_server_seed(conn, int(guild_id), for_update=False)
        return {
            "server_seed_hash": str(server_seed.get("server_seed_hash") or ""),
            "client_seed": str(row.get("client_seed") or cleaned),
            "nonce": int(row.get("nonce") or 0),
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

                pool_before = await self.casino_repo.get_or_create_pool(
                    conn,
                    guild_id=int(guild_id),
                    pool_key=SLOTS_POOL_KEY,
                    seed_tokens=int(cfg.get("jackpot_seed_tokens") or 0),
                    seed_millis=int(cfg.get("jackpot_seed_millis") or 0),
                )
                retention_state = await self.casino_repo.get_or_create_retention_state(
                    guild_id=int(guild_id),
                    discord_id=int(discord_id),
                    game=RETENTION_GAME_KEY,
                    for_update=True,
                    conn=conn,
                )
                plays = int(retention_state.get("plays") or 0)
                loss_streak = int(retention_state.get("loss_streak") or 0)

                forced_outcome: str | None = None
                forced_by_loss_streak = False
                if plays == 0:
                    forced_outcome = "small_win"
                elif loss_streak >= LOSS_STREAK_PUSH_THRESHOLD:
                    forced_outcome = "push"
                    forced_by_loss_streak = True

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

                contrib_millis = int((bet * int(cfg.get("jackpot_contrib_bps") or 0) * 1000) // 10000)
                pool_after_contrib = await self.casino_repo.add_to_pool(
                    conn,
                    guild_id=int(guild_id),
                    pool_key=SLOTS_POOL_KEY,
                    add_tokens=0,
                    add_millis=contrib_millis,
                )

                if forced_outcome == "small_win":
                    outcome_bucket = "small"
                elif forced_outcome == "push":
                    outcome_bucket = "push"
                else:
                    outcome_bucket = self._choose_outcome_bucket(
                        plays=plays,
                        pool_tokens=int(pool_after_contrib["tokens"]),
                        rng=rng,
                    )
                reels = self._roll_reels(cfg, outcome_bucket, rng)
                payout, win_type, pair_item = self._calculate_payout(
                    cfg,
                    bet,
                    reels,
                    outcome_bucket,
                    pool_tokens=int(pool_after_contrib["tokens"]),
                )
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

                if win_type == "double_jp" and payout > 0:
                    pool_after_double = await self.casino_repo.add_to_pool(
                        conn,
                        guild_id=int(guild_id),
                        pool_key=SLOTS_POOL_KEY,
                        add_tokens=-int(payout),
                        add_millis=0,
                    )
                    final_pool_tokens = int(pool_after_double["tokens"])
                    final_pool_millis = int(pool_after_double["millis"])
                    log.info(
                        "slots_result guild_id=%s discord_id=%s result_type=double_jp bet=%s reels=%s payout=%s pool_before=%s pool_after=%s",
                        int(guild_id),
                        int(discord_id),
                        int(bet),
                        reels,
                        int(payout),
                        int(pool_before["tokens"]),
                        int(final_pool_tokens),
                    )

                if win_type == "jackpot":
                    log.info(
                        "slots_result guild_id=%s discord_id=%s result_type=jackpot bet=%s reels=%s payout=%s pool_before=%s pool_after=%s",
                        int(guild_id),
                        int(discord_id),
                        int(bet),
                        reels,
                        int(payout),
                        int(pool_before["tokens"]),
                        int(final_pool_tokens),
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
                        metadata={"game": "slots", "win_type": win_type, "pair_item": pair_item},
                    )

                next_plays = plays + 1
                next_loss_streak = 0
                if forced_by_loss_streak:
                    next_loss_streak = 0
                elif win_type == "loss":
                    next_loss_streak = loss_streak + 1
                await self.casino_repo.update_retention_state(
                    guild_id=int(guild_id),
                    discord_id=int(discord_id),
                    game=RETENTION_GAME_KEY,
                    plays=next_plays,
                    loss_streak=next_loss_streak,
                    conn=conn,
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
                    "win_type": win_type,
                    "outcome_bucket": outcome_bucket,
                    "forced_outcome": forced_outcome,
                    "bet": bet,
                    "payout": payout,
                    "pool_before": {"tokens": int(pool_before["tokens"]), "millis": int(pool_before["millis"])},
                    "contrib_added": {"tokens": 0, "millis": contrib_millis},
                    "pool_after": {"tokens": final_pool_tokens, "millis": final_pool_millis},
                    "provably_fair": {
                        "server_seed_hash": server_seed_hash,
                        "client_seed": client_seed,
                        "nonce": spin_nonce,
                    },
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
                    "house_discord_id": int((house or {}).get("house_discord_id") or 0),
                    "house_torn_id": int((house or {}).get("house_torn_id") or 0),
                    "payout_proof_channel_id": int((house or {}).get("payout_proof_channel_id") or 0),
                    "server_seed_hash": server_seed_hash,
                    "client_seed": client_seed,
                    "nonce": spin_nonce,
                }

                big_wins_channel_id = int((house or {}).get("big_wins_channel_id") or 0)
                if big_wins_channel_id and win_type in {"jackpot", "double_jp", "triple"}:
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
            elif win_type == "double_jp":
                label = "MINI JACKPOT / DOUBLE ROBOT"
            elif win_type == "triple":
                label = "TRIPLE"
            reels = announce.get("reels") or []
            await channel.send(
                f"🎰 {label}! <@{announce['discord_id']}> won **{announce['payout']}** tokens "
                f"(round #{announce.get('round_id')}, reels: {reels})."
            )
        except Exception as exc:
            log.warning("Big win announcement failed channel_id=%s round_id=%s: %s", channel_id, announce.get("round_id"), exc)

    def _roll_reels(self, config: dict, outcome_bucket: str, rng: ProvablyFairRNG) -> list[int]:
        all_symbols = self._get_symbol_ids(config)
        if not all_symbols:
            raise SlotsError("Slots symbol configuration is invalid.")

        non_jackpot_symbols = [item_id for item_id in all_symbols if item_id != JACKPOT_SYMBOL_ID]
        if len(non_jackpot_symbols) < 3:
            raise SlotsError("Slots symbol configuration requires at least three non-jackpot symbols.")

        if outcome_bucket == "jackpot":
            return [JACKPOT_SYMBOL_ID, JACKPOT_SYMBOL_ID, JACKPOT_SYMBOL_ID]
        if outcome_bucket == "double_jp":
            replacement = rng.choice(non_jackpot_symbols)
            reels = [JACKPOT_SYMBOL_ID, JACKPOT_SYMBOL_ID, JACKPOT_SYMBOL_ID]
            reels[rng.randbelow(3)] = replacement
            return reels
        if outcome_bucket == "triple":
            symbol = self._choose_non_jackpot_triple_symbol(config, rng)
            return [symbol, symbol, symbol]
        if outcome_bucket == "small":
            pair_symbol = self._choose_small_pair_symbol(config, rng)
            alternatives = [symbol for symbol in non_jackpot_symbols if symbol != pair_symbol]
            odd_symbol = rng.choice(alternatives)
            reels = [pair_symbol, pair_symbol, pair_symbol]
            reels[rng.randbelow(3)] = odd_symbol
            return reels
        if outcome_bucket in {"push", "loss"}:
            reels = rng.sample(non_jackpot_symbols, 3)
            rng.shuffle(reels)
            return reels
        raise SlotsError(f"Unknown slots outcome bucket: {outcome_bucket}")

    def _choose_outcome_bucket(self, *, plays: int, pool_tokens: int, rng: ProvablyFairRNG) -> str:
        odds_jackpot = ODDS_JACKPOT
        odds_double_jp = ODDS_DOUBLE_JP
        odds_triple = ODDS_TRIPLE
        odds_small = ODDS_SMALL
        odds_push = ODDS_PUSH
        odds_loss = ODDS_LOSS

        if int(plays) < 5 and int(pool_tokens) >= MIN_POOL_FOR_DOUBLE:
            boost = min(FIRST_FIVE_PLAYS_DOUBLE_BOOST, odds_loss)
            odds_double_jp += boost
            odds_loss -= boost

        r = rng.random_float()
        if r < odds_jackpot:
            return "jackpot"
        if r < odds_jackpot + odds_double_jp:
            return "double_jp"
        if r < odds_jackpot + odds_double_jp + odds_triple:
            return "triple"
        if r < odds_jackpot + odds_double_jp + odds_triple + odds_small:
            return "small"
        if r < odds_jackpot + odds_double_jp + odds_triple + odds_small + odds_push:
            return "push"
        return "loss"

    def _get_symbol_ids(self, config: dict) -> list[int]:
        return [int(s.get("item_id")) for s in list(config.get("symbols") or []) if s.get("item_id") is not None]

    def _choose_small_pair_symbol(self, config: dict, rng: ProvablyFairRNG) -> int:
        payouts = dict(config.get("payouts") or {})
        pair = dict(payouts.get("pair") or {})
        symbols = self._get_symbol_ids(config)
        eligible = [item_id for item_id in symbols if item_id != JACKPOT_SYMBOL_ID and str(item_id) in pair]
        if not eligible:
            raise SlotsError("No eligible symbols for small win pair payout.")
        return rng.choice(eligible)

    def _choose_non_jackpot_triple_symbol(self, config: dict, rng: ProvablyFairRNG) -> int:
        payouts = dict(config.get("payouts") or {})
        triple = dict(payouts.get("triple") or {})
        ranked = sorted(
            (
                (int(symbol_id), float(multiplier))
                for symbol_id, multiplier in triple.items()
                if int(symbol_id) != JACKPOT_SYMBOL_ID
            ),
            key=lambda kv: kv[1],
        )
        if len(ranked) < 1:
            raise SlotsError("No non-jackpot triple payout symbols configured.")

        by_tier: list[list[int]] = [[symbol] for symbol, _ in ranked]
        while len(by_tier) < len(TRIPLE_TIER_WEIGHTS):
            by_tier.append(by_tier[-1])

        roll = rng.random_float()
        cumulative = 0.0
        for index, weight in enumerate(TRIPLE_TIER_WEIGHTS):
            cumulative += weight
            if roll <= cumulative:
                return rng.choice(by_tier[index])
        return rng.choice(by_tier[-1])

    def _calculate_payout(
        self,
        config: dict,
        bet: int,
        reels: list[int],
        outcome_bucket: str,
        pool_tokens: int,
    ) -> tuple[int, str, int | None]:
        payouts = dict(config.get("payouts") or {})
        triple = dict(payouts.get("triple") or {})
        pair = dict(payouts.get("pair") or {})
        xanax_tease = float(payouts.get("xanax_tease") or 0)

        if outcome_bucket == "push":
            return int(bet), "push", None
        if outcome_bucket == "double_jp":
            if int(pool_tokens) < 2:
                return int(bet), "push", None
            payout = int(math.floor(int(pool_tokens) * 0.10))
            payout = max(1, payout)
            payout = min(payout, int(pool_tokens) - 1)
            return payout, "double_jp", JACKPOT_SYMBOL_ID

        if reels[0] == reels[1] == reels[2] == JACKPOT_SYMBOL_ID:
            return 0, "jackpot", JACKPOT_SYMBOL_ID
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


if __name__ == "__main__":
    service = CasinoSlotsService.__new__(CasinoSlotsService)
    spins = 100000
    counts: dict[str, int] = {"jackpot": 0, "double_jp": 0, "triple": 0, "small": 0, "push": 0, "loss": 0}
    for _ in range(spins):
        rng = ProvablyFairRNG(secrets.token_hex(8), "demo-client", _)
        bucket = service._choose_outcome_bucket(plays=0, pool_tokens=100, rng=rng)
        counts[bucket] = counts.get(bucket, 0) + 1
    for name in ["push", "small", "triple", "jackpot", "double_jp", "loss"]:
        pct = (counts.get(name, 0) / spins) * 100
        print(f"{name}: {pct:.3f}%")
