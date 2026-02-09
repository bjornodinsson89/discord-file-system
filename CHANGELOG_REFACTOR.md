# Refactor changelog (Discord-only Railway service)

## Added
- `utils/guild_settings_repository.py` — repository layer for guild settings CRUD/upserts.
- `migrations/007_discord_only_guild_settings.sql` — adds `announce_channel_id`, `admin_role_ids`, welcome fields, timestamps.
- `CHANGELOG_REFACTOR.md` — this file.

## Modified
- `bot.py` — removed dashboard commands/links; added Discord-only `/setup`, `/setchannel`, `/config`, `/testannounce`; added guild join auto-channel detection; updated permission logic.
- `config.py` — removed web/service-mode config and validation, bot-only configuration.
- `utils/database.py` — removed service-mode migration branching.
- `utils/__init__.py` — exports `GuildSettingsRepository`.
- `migrations/000_full_schema.sql` and `migrations/001_complete_schema.sql` — include new guild settings columns.
- `.env.example` — removed web env vars, bot-only env vars.
- `requirements.txt` — removed FastAPI/web dependencies.
- `railway.json`, `Procfile`, `nixpacks.toml` — single bot start command and no frontend build.
- `README.md`, `DEPLOYMENT.md` — rewritten for bot-only deployment and config.
- `tests/test_permissions.py`, `tests/test_smoke.py` — updated for new permission and settings CRUD smoke checks.

## Removed
- `web/` — FastAPI app, auth, CSRF, permissions, healthcheck.
- `frontend/` — dashboard frontend.
- `api/` — web API entrypoint.
- `admin_api/routes.py` — dashboard route layer.
- `tests/test_web_mode_and_csrf.py`, `tests/test_csrf.py`, `tests/test_db_ssl_config.py` — web/legacy-specific tests.
