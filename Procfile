web: SERVICE_MODE=API RUN_BOT=false START_COMMAND='uvicorn web.app:app --host 0.0.0.0 --port $PORT' bash -lc 'exec "$SHELL" -lc "$START_COMMAND"'
bot: SERVICE_MODE=BOT RUN_WEB=false RUN_MIGRATIONS=false START_COMMAND='python bot.py' bash -lc 'exec "$SHELL" -lc "$START_COMMAND"'
