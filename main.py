import discord
from discord.ext import commands, tasks
import os
import sys
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
    synced = await bot.tree.sync()
    print(f'🔥 Sistema Mestre online! Operando como {bot.user}. Sincronizados {len(synced)} comandos slash.')
    if not manter_mongo_vivo.is_running():
        manter_mongo_vivo.start()

keep_alive() 
bot.run(os.getenv('TOKEN_DO_BOT'))
