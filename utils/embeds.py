"""Discord embed utilities for all features."""

import discord
from typing import Optional, List, Dict
from datetime import datetime
import config


def create_base_embed(title: str, description: Optional[str] = None, color: int = config.COLOR_PRIMARY) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.utcnow())
    embed.set_footer(text="Happy Jumper Bot")
    return embed


def create_success_embed(title: str, description: Optional[str] = None) -> discord.Embed:
    return create_base_embed(f"{config.EMOJI_CHECK} {title}", description, config.COLOR_SUCCESS)


def create_error_embed(title: str, description: Optional[str] = None) -> discord.Embed:
    return create_base_embed(f"{config.EMOJI_CROSS} {title}", description, config.COLOR_ERROR)


def create_warning_embed(title: str, description: Optional[str] = None) -> discord.Embed:
    return create_base_embed(f"{config.EMOJI_WARNING} {title}", description, config.COLOR_WARNING)


def create_info_embed(title: str, description: Optional[str] = None) -> discord.Embed:
    return create_base_embed(f"{config.EMOJI_INFO} {title}", description, config.COLOR_INFO)


def create_jump_session_embed(session: Dict, signups: List[Dict], readiness: List[Dict] = None) -> discord.Embed:
    status = session['status']
    status_emoji = {'open': config.EMOJI_UNLOCK, 'locked': config.EMOJI_LOCK,
                    'completed': config.EMOJI_CHECK, 'cancelled': config.EMOJI_CROSS}.get(status, '')
    
    embed = create_base_embed(
        f"{status_emoji} Happy Jump #{session['id']} - {status.title()}",
        color=config.COLOR_PRIMARY if status == 'open' else config.COLOR_SECONDARY
    )
    
    embed.add_field(name=f"{config.EMOJI_USER} Host",
                    value=f"<@{session['host_discord_id']}>\nTorn: `{session['host_torn_id']}`", inline=True)
    embed.add_field(name=f"{config.EMOJI_PILL} Xanax", value=f"`{session['xanax_count']}`", inline=True)
    
    paid = sum(1 for s in signups if s['status'] == 'paid')
    reserved = sum(1 for s in signups if s['status'] == 'reserved')
    available = session['max_spots'] - len(signups)
    
    embed.add_field(name=f"{config.EMOJI_JUMP} Spots",
                    value=f"**Paid:** {paid}\n**Reserved:** {reserved}\n**Available:** {available}", inline=True)
    
    if session['payment_type'] == 'cash':
        payment = f"${session['payment_amount']:,}"
    else:
        item = "DVD" if session.get('payment_item_id') == config.DVD_ITEM_ID else "Item"
        payment = f"{session['payment_amount']}x {item}"
    embed.add_field(name=f"{config.EMOJI_MONEY} Payment", value=payment, inline=True)
    
    if session.get('start_in_hours', 0) > 0:
        from datetime import timedelta
        start = session['created_at'] + timedelta(hours=session['start_in_hours'])
        embed.add_field(name=f"{config.EMOJI_CLOCK} Starts", value=f"<t:{int(start.timestamp())}:R>", inline=True)
    
    if signups:
        readiness_map = {r['discord_id']: r for r in (readiness or [])}
        parts = []
        for i, s in enumerate(signups[:15], 1):
            emoji = config.EMOJI_CHECK if s['status'] == 'paid' else config.EMOJI_CLOCK
            r = readiness_map.get(s['discord_id'])
            status_text = ""
            if r:
                status_text = f" | E:{r['energy']}/{r['energy_max']} CD:{r['drug_cooldown']}s"
            parts.append(f"{i}. {emoji} <@{s['discord_id']}>{status_text}")
        if len(signups) > 15:
            parts.append(f"*...and {len(signups) - 15} more*")
        embed.add_field(name=f"Participants ({len(signups)}/{session['max_spots']})",
                        value="\n".join(parts), inline=False)
    
    return embed


def create_insurance_policy_embed(policy: Dict) -> discord.Embed:
    embed = create_base_embed(f"{config.EMOJI_SHIELD} {policy['name']}", policy.get('description'), config.COLOR_INSURANCE)
    embed.add_field(name="Provider", value=f"<@{policy['provider_discord_id']}> ({policy['company_name']})", inline=True)
    embed.add_field(name="Coverage Types", value=", ".join(policy.get('covered_jump_types', ['99k'])), inline=True)
    embed.add_field(name=f"{config.EMOJI_PILL} Max Coverage", value=f"{policy['max_coverage_xanax']} Xanax", inline=True)
    embed.add_field(name=f"{config.EMOJI_MONEY} Premium", value=f"${policy['premium_per_xanax']:,}/Xanax", inline=True)
    embed.add_field(name=f"{config.EMOJI_TROPHY} Payout", value=f"${policy['payout_per_xanax']:,}/Xanax", inline=True)
    embed.add_field(name=f"{config.EMOJI_CLOCK} Duration", value=f"{policy['duration_hours']} hours", inline=True)
    return embed


def create_coverage_embed(coverage: Dict) -> discord.Embed:
    embed = create_base_embed(f"{config.EMOJI_SHIELD} Your Insurance Coverage", color=config.COLOR_INSURANCE)
    embed.add_field(name="Policy", value=f"{coverage['policy_name']} ({coverage['company_name']})", inline=False)
    embed.add_field(name="Xanax Covered", value=f"{coverage['xanax_covered']}", inline=True)
    embed.add_field(name="Premium Paid", value=f"${coverage['premium_paid']:,}", inline=True)
    embed.add_field(name="Payout Amount", value=f"${coverage['payout_amount']:,}", inline=True)
    embed.add_field(name="Status", value=coverage['status'].title(), inline=True)
    embed.add_field(name="Expires", value=f"<t:{int(coverage['expires_at'].timestamp())}:R>", inline=True)
    return embed


def create_claim_embed(claim: Dict) -> discord.Embed:
    status_colors = {'pending': config.COLOR_WARNING, 'approved': config.COLOR_INFO,
                     'paid': config.COLOR_SUCCESS, 'denied': config.COLOR_ERROR}
    embed = create_base_embed(f"{config.EMOJI_SHIELD} Claim #{claim['claim_id']}",
                              color=status_colors.get(claim['status'], config.COLOR_SECONDARY))
    embed.add_field(name="Policy", value=f"{claim['policy_name']} ({claim['company_name']})", inline=False)
    embed.add_field(name="User", value=f"<@{claim['user_discord_id']}> (Torn: {claim['user_torn_id']})", inline=True)
    embed.add_field(name="Payout", value=f"${claim['payout_amount']:,}", inline=True)
    embed.add_field(name="Status", value=claim['status'].upper(), inline=True)
    if claim.get('denial_reason'):
        embed.add_field(name="Denial Reason", value=claim['denial_reason'], inline=False)
    return embed


def create_raffle_embed(raffle: Dict, entries: List[Dict]) -> discord.Embed:
    status_emoji = {'open': config.EMOJI_TICKET, 'drawing': config.EMOJI_DICE,
                    'completed': config.EMOJI_TROPHY, 'cancelled': config.EMOJI_CROSS}.get(raffle['status'], '')
    
    embed = create_base_embed(f"{status_emoji} Raffle #{raffle['raffle_id']} - {raffle['status'].title()}",
                              raffle['prize_description'], config.COLOR_RAFFLE)
    embed.add_field(name=f"{config.EMOJI_USER} Host", value=f"<@{raffle['admin_discord_id']}>", inline=True)
    
    paid_entries = [e for e in entries if e['status'] == 'paid']
    total_tickets = sum(e['tickets'] for e in paid_entries)
    embed.add_field(name=f"{config.EMOJI_TICKET} Tickets Sold", value=f"{total_tickets}/{raffle['max_tickets']}", inline=True)
    embed.add_field(name=f"{config.EMOJI_PILL} Ticket Price", value=f"{raffle['ticket_price_xanax']} Xanax", inline=True)
    embed.add_field(name=f"{config.EMOJI_CLOCK} Ends", value=f"<t:{int(raffle['ends_at'].timestamp())}:R>", inline=True)
    embed.add_field(name="Participants", value=f"{len(paid_entries)} users", inline=True)
    embed.add_field(name="Max Per User", value=f"{raffle['max_tickets_per_user']} tickets", inline=True)
    
    if raffle['status'] == 'completed' and raffle.get('winner_discord_id'):
        embed.add_field(name=f"{config.EMOJI_TROPHY} Winner",
                        value=f"<@{raffle['winner_discord_id']}> ({raffle['winner_tickets']} tickets)", inline=False)
    
    return embed


def create_host_reputation_embed(discord_id: int, rep: Dict) -> discord.Embed:
    avg = rep['average_rating']
    if avg >= 4.0:
        color, emoji = config.COLOR_SUCCESS, config.EMOJI_STAR
    elif avg >= 3.0:
        color, emoji = config.COLOR_WARNING, config.EMOJI_INFO
    else:
        color, emoji = config.COLOR_ERROR, config.EMOJI_WARNING
    
    embed = create_base_embed(f"{emoji} Host Reputation", color=color)
    embed.add_field(name="Host", value=f"<@{discord_id}>", inline=False)
    embed.add_field(name="Average Rating", value=f"{'⭐' * round(avg)} ({avg:.1f}/5)", inline=True)
    embed.add_field(name="Total Ratings", value=str(rep['total_ratings']), inline=True)
    embed.add_field(name=f"{config.EMOJI_CHECK} Positive", value=str(rep['positive_ratings']), inline=True)
    embed.add_field(name=f"{config.EMOJI_CROSS} Negative", value=str(rep['negative_ratings']), inline=True)
    embed.add_field(name="Sessions Hosted", value=str(rep['total_sessions']), inline=True)
    embed.add_field(name="Successful", value=str(rep['successful_sessions']), inline=True)
    embed.add_field(name="Cancelled", value=str(rep['cancelled_sessions']), inline=True)
    return embed


def create_statistics_embed(stats: Dict, title: str = "Statistics") -> discord.Embed:
    embed = create_base_embed(f"{config.EMOJI_CHART} {title}", color=config.COLOR_INFO)
    
    if 'sessions' in stats:
        s = stats['sessions']
        embed.add_field(name=f"{config.EMOJI_JUMP} Jump Sessions",
                        value=f"Total: {s['total']}\nCompleted: {s['completed']}\nActive: {s['active']}\nCancelled: {s['cancelled']}",
                        inline=True)
    
    if 'raffles' in stats:
        r = stats['raffles']
        embed.add_field(name=f"{config.EMOJI_TICKET} Raffles",
                        value=f"Total: {r['total']}\nCompleted: {r['completed']}", inline=True)
    
    if 'registered_users' in stats:
        embed.add_field(name=f"{config.EMOJI_USER} Registered Users", value=str(stats['registered_users']), inline=True)
    
    if 'sessions_hosted' in stats:
        h = stats['sessions_hosted']
        embed.add_field(name="Sessions Hosted", value=f"Total: {h['total']}\nSuccessful: {h['successful']}", inline=True)
    
    if 'sessions_joined' in stats:
        j = stats['sessions_joined']
        embed.add_field(name="Sessions Joined", value=f"Total: {j['total']}\nCompleted: {j['completed']}", inline=True)
    
    if 'raffles' in stats and 'wins' in stats['raffles']:
        r = stats['raffles']
        embed.add_field(name="Raffle Stats", value=f"Entered: {r['entered']}\nTickets: {r['tickets']}\nWins: {r['wins']}", inline=True)
    
    if 'insurance_claims' in stats:
        c = stats['insurance_claims']
        embed.add_field(name="Insurance Claims", value=f"Total: {c['total']}\nPaid: {c['paid']}", inline=True)
    
    return embed


def create_api_key_guide_embed() -> discord.Embed:
    embed = create_info_embed("Set Up Your Torn API Key",
                              "Register your API key to use Happy Jumper features.")
    embed.add_field(
        name="Get an API Key",
        value=f"Create a custom key here: {config.TORN_API_KEY_LINK}\n"
              "Or use a full access key if you already have one.",
        inline=False,
    )
    embed.add_field(name=f"{config.EMOJI_LOCK} Security",
                    value="Your API key is encrypted and stored securely. We only use it for eligibility and verification.",
                    inline=False)
    return embed


def create_blacklist_embed(blacklist: List[Dict]) -> discord.Embed:
    embed = create_base_embed(f"{config.EMOJI_CROSS} Blacklist", color=config.COLOR_ERROR)
    if not blacklist:
        embed.description = "No blacklisted users."
        return embed
    
    lines = []
    for entry in blacklist[:20]:
        exp = f" (until <t:{int(entry['expires_at'].timestamp())}:R>)" if entry.get('expires_at') else " (permanent)"
        lines.append(f"<@{entry['discord_id']}> - {entry['reason']}{exp}")
    
    embed.description = "\n".join(lines)
    if len(blacklist) > 20:
        embed.description += f"\n*...and {len(blacklist) - 20} more*"
    return embed


def create_session_announcement_embed(session: Dict, guild) -> discord.Embed:
    """Create announcement embed for a new 99k session (dashboard-created)."""
    xanax_label = session.get('xanax_stack', f"{session['xanax_count']} xanax").replace('_', ' ')
    
    embed = create_base_embed(
        f"{config.EMOJI_JUMP} New 99k Jump Session",
        f"A new jump session has been created!",
        config.COLOR_PRIMARY
    )
    
    embed.add_field(
        name=f"{config.EMOJI_USER} Host",
        value=f"<@{session['host_discord_id']}>\nTorn: `{session['host_torn_id']}`",
        inline=True
    )
    
    embed.add_field(
        name=f"{config.EMOJI_PILL} Xanax Stack",
        value=xanax_label.title(),
        inline=True
    )
    
    embed.add_field(
        name=f"{config.EMOJI_JUMP} Spots",
        value=f"**0/{session['max_spots']}**\nAvailable: {session['max_spots']}",
        inline=True
    )
    
    # Payment display
    if session['payment_type'] == 'xanax':
        payment = f"{session['payment_amount']}x Xanax"
    elif session['payment_type'] == 'erotic_dvd':
        payment = f"{session['payment_amount']}x Erotic DVD"
    else:
        payment = f"{session['payment_amount']}x {session['payment_type']}"
    
    embed.add_field(
        name=f"{config.EMOJI_MONEY} Payment Required",
        value=payment,
        inline=True
    )
    
    if session.get('start_in_hours', 0) > 0:
        from datetime import timedelta
        start_time = session.get('created_at', datetime.utcnow())
        if isinstance(start_time, str):
            start_time = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        start = start_time + timedelta(hours=session['start_in_hours'])
        embed.add_field(
            name=f"{config.EMOJI_CLOCK} Starts",
            value=f"<t:{int(start.timestamp())}:R>",
            inline=True
        )
    
    embed.add_field(
        name="Status",
        value=f"{config.EMOJI_UNLOCK} Open",
        inline=True
    )
    
    embed.set_footer(text=f"Session #{session['id']} • Click buttons below to join")
    return embed


def create_raffle_announcement_embed(raffle: Dict, guild) -> discord.Embed:
    """Create announcement embed for a new raffle (dashboard-created)."""
    embed = create_base_embed(
        f"{config.EMOJI_TICKET} New Raffle!",
        raffle['prize'],
        config.COLOR_RAFFLE
    )
    
    embed.add_field(
        name=f"{config.EMOJI_USER} Created By",
        value=f"<@{raffle['creator_discord_id']}>",
        inline=True
    )
    
    # Ticket price display
    if raffle['ticket_payment_type'] == 'xanax':
        price = f"{raffle['ticket_price']}x Xanax"
    elif raffle['ticket_payment_type'] == 'erotic_dvd':
        price = f"{raffle['ticket_price']}x Erotic DVD"
    else:
        price = f"{raffle['ticket_price']}x {raffle['ticket_payment_type']}"
    
    embed.add_field(
        name=f"{config.EMOJI_MONEY} Ticket Price",
        value=price,
        inline=True
    )
    
    embed.add_field(
        name=f"{config.EMOJI_TICKET} Tickets",
        value=f"0/{raffle['tickets_available']} sold",
        inline=True
    )
    
    if raffle.get('max_tickets_per_user', 0) > 0:
        embed.add_field(
            name="Max Per User",
            value=f"{raffle['max_tickets_per_user']} tickets",
            inline=True
        )
    else:
        embed.add_field(
            name="Max Per User",
            value="Unlimited",
            inline=True
        )
    
    end_time = raffle['end_time']
    if isinstance(end_time, str):
        end_time = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
    
    embed.add_field(
        name=f"{config.EMOJI_CLOCK} Ends",
        value=f"<t:{int(end_time.timestamp())}:F>\n(<t:{int(end_time.timestamp())}:R>)",
        inline=True
    )
    
    embed.add_field(
        name="Status",
        value=f"{config.EMOJI_UNLOCK} Open",
        inline=True
    )
    
    embed.set_footer(text=f"Raffle #{raffle['raffle_id']} • Click buttons below to buy tickets")
    return embed


def create_policy_announcement_embed(policy: Dict, guild) -> discord.Embed:
    """Create announcement embed for a new insurance policy (dashboard-created)."""
    embed = create_base_embed(
        f"{config.EMOJI_SHIELD} New Insurance Policy",
        policy.get('description', 'A new insurance policy is now available!'),
        config.COLOR_INSURANCE
    )
    
    embed.add_field(
        name="Policy Name",
        value=policy['name'],
        inline=True
    )
    
    if policy.get('company_name'):
        embed.add_field(
            name="Provider",
            value=policy['company_name'],
            inline=True
        )
    
    # Cost display
    cost_type = policy.get('cost_type', 'xanax')
    cost_amount = policy.get('cost_amount', 0)
    if cost_type == 'xanax':
        cost = f"{cost_amount}x Xanax"
    elif cost_type == 'erotic_dvd':
        cost = f"{cost_amount}x Erotic DVD"
    else:
        cost = f"{cost_amount}x {cost_type}"
    
    embed.add_field(
        name=f"{config.EMOJI_MONEY} Cost",
        value=cost,
        inline=True
    )
    
    coverage_labels = {
        'xanax_stack': 'Xanax Stack Only',
        'ecstasy_after_stack': 'Ecstasy After Stack',
        'all_drugs': 'All Drug-Related Losses'
    }
    coverage_type = policy.get('coverage_type', 'xanax_stack')
    
    embed.add_field(
        name="Coverage Type",
        value=coverage_labels.get(coverage_type, coverage_type),
        inline=True
    )
    
    embed.add_field(
        name=f"{config.EMOJI_CLOCK} Duration",
        value=f"{policy.get('duration_hours', 24)} hours",
        inline=True
    )
    
    if policy.get('payout_description'):
        embed.add_field(
            name=f"{config.EMOJI_TROPHY} Payout",
            value=policy['payout_description'],
            inline=False
        )
    
    embed.set_footer(text=f"Policy #{policy['policy_id']} • Click button below to purchase coverage")
    return embed


def create_raffle_winner_embed(raffle: Dict, winner: Dict) -> discord.Embed:
    """Create embed announcing raffle winner."""
    embed = create_base_embed(
        f"{config.EMOJI_TROPHY} Raffle Winner!",
        f"**Prize:** {raffle['prize']}",
        config.COLOR_SUCCESS
    )
    
    embed.add_field(
        name="Winner",
        value=f"<@{winner['discord_id']}>\nTorn: `{winner.get('torn_user_id', 'N/A')}`",
        inline=True
    )
    
    embed.add_field(
        name="Winning Ticket",
        value=f"#{winner.get('ticket_number', '?')} of {winner.get('total_tickets', '?')}",
        inline=True
    )
    
    embed.set_footer(text=f"Raffle #{raffle['raffle_id']} • Congratulations!")
    return embed


def create_claim_notification_embed(claim: Dict, coverage: Dict) -> discord.Embed:
    """Create embed for notifying provider of a new claim."""
    embed = create_base_embed(
        f"{config.EMOJI_WARNING} New Insurance Claim",
        f"A claim has been filed that requires your review.",
        config.COLOR_WARNING
    )
    
    embed.add_field(
        name="User",
        value=f"<@{claim['user_discord_id']}>\nTorn: `{coverage.get('user_torn_id', 'N/A')}`",
        inline=True
    )
    
    embed.add_field(
        name="Claim Type",
        value=claim['claim_type'].replace('_', ' ').title(),
        inline=True
    )
    
    embed.add_field(
        name="Payout Amount",
        value=f"{claim['payout_amount']}x items",
        inline=True
    )
    
    embed.set_footer(text=f"Claim #{claim['claim_id']} • Review in your claims queue")
    return embed
