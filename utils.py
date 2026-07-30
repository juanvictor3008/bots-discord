import discord
from datetime import datetime, timezone

from config import CANAL_LOGS_ID

async def log_discord(bot, mensagem, nivel="info"):
    if CANAL_LOGS_ID == 0:
        return
    canal = bot.get_channel(CANAL_LOGS_ID)
    if not canal:
        return

    cores = {
        "info": discord.Color.blue(),
        "aviso": discord.Color.gold(),
        "erro": discord.Color.red(),
    }
    icones = {
        "info": "ℹ️",
        "aviso": "⚠️",
        "erro": "❌",
    }

    embed = discord.Embed(
        description=f"{icones.get(nivel, 'ℹ️')} {mensagem}",
        color=cores.get(nivel, discord.Color.blue()),
        timestamp=datetime.now(timezone.utc)
    )
    try:
        await canal.send(embed=embed)
    except Exception:
        pass
