import discord
from discord.ext import commands, tasks
import os
import sys
import asyncio
from dotenv import load_dotenv
from keep_alive import keep_alive

sys.stdout.reconfigure(encoding='utf-8')

# Carrega as senhas do arquivo .env
load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
intents.presences = True

# Cria a classe do Bot para usar o setup_hook (Padrão Oficial)
class MeuBot(commands.Bot):
    async def setup_hook(self):
        # Carrega todos os arquivos .py dentro da pasta "cogs" ANTES do bot ficar online
        for filename in os.listdir('./cogs'):
            if filename.endswith('.py'):
                try:
                    await self.load_extension(f'cogs.{filename[:-3]}')
                    print(f"📦 Módulo carregado: {filename}")
                except Exception as e:
                    print(f"❌ Erro ao carregar {filename}: {e}")

bot = MeuBot(command_prefix="!", intents=intents, help_command=None)

@tasks.loop(minutes=30)
async def manter_mongo_vivo():
    try:
        from config import mongo_client
        await mongo_client.admin.command('ping')
    except Exception:
        print("⚠️ Ping MongoDB falhou, reconectando...")
        try:
            from config import mongo_client
            mongo_client.close()
        except Exception:
            pass

@bot.event
async def on_ready():
    guild_id = 1519158547881922601
    for tentativa in range(3):
        try:
            guild_obj = discord.Object(id=guild_id)
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            print(f'🔥 Sistema Mestre online! Operando como {bot.user}. Sincronizados {len(synced)} comandos slash na guild {guild_id}.')
            break
        except Exception as e:
            print(f"⚠️ Sync por guild falhou (tentativa {tentativa + 1}/3): {e}")
            if tentativa < 2:
                await asyncio.sleep(5)
            else:
                try:
                    synced = await bot.tree.sync()
                    print(f'🔥 Sistema Mestre online! Operando como {bot.user}. Sync global: {len(synced)} comandos.')
                except Exception as e2:
                    print(f"❌ Sync global também falhou: {e2}. O bot segue online; comandos slash podem demorar.")
    if not manter_mongo_vivo.is_running():
        manter_mongo_vivo.start()

keep_alive()

token = os.getenv('TOKEN_DO_BOT')
espera = 30
while True:
    try:
        bot.run(token)
    except discord.LoginFailure:
        print("❌ Token inválido — encerrando. Confira TOKEN_DO_BOT no .env da Render.")
        break
    except discord.HTTPException as e:
        if e.status == 429:
            print(f"⚠️ 429 do Discord (bloqueio global). Aguardando {espera}s pra tentar logar de novo...")
            import time
            time.sleep(espera)
            espera = min(espera * 2, 900)
            continue
        raise
    else:
        break
