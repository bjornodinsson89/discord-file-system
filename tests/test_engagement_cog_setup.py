from pathlib import Path


def test_engagement_setup_does_not_manually_register_groups_twice():
    src = Path("cogs/engagement.py").read_text(encoding="utf-8")
    assert "await bot.add_cog(EngagementCog(bot))" in src
    assert "bot.tree.add_command(cog.profile)" not in src
    assert "bot.tree.add_command(cog.tokens)" not in src
    assert "bot.tree.add_command(cog.engagement)" not in src
    assert "bot.tree.add_command(cog.leaderboard)" not in src
