import discord
from discord.ext import commands
import aiohttp
import asyncio

# Importa as variáveis EXATAMENTE como estão no seu config.py
from config import GUILDA_ALBION_ID, ALIANCA_ALBION_ID, CARGOS

API_BUSCA = 'https://gameinfo.albiononline.com/api/gameinfo/search?q={}'

class RegistrarCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.fila = asyncio.Queue()
        self.worker_task = None

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.worker_task:
            self.worker_task = self.bot.loop.create_task(self.processar_fila())

    def cog_unload(self):
        if self.worker_task:
            self.worker_task.cancel()

    # ——— WORKER QUE PROCESSA UM POR VEZ (evita B.O na API) ———
    async def processar_fila(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            item = await self.fila.get()
            membro, nick, msg_status, guild = item

            try:
                await self.buscar_e_registrar(membro, nick, msg_status, guild)
            except Exception as e:
                print(f'❌ Erro ao registrar {nick}: {e}')
                try:
                    await msg_status.edit(content=f'❌ Erro ao consultar a API pra **{nick}**. Tenta novamente.')
                except:
                    pass

            await asyncio.sleep(2)  # intervalo entre requests
            self.fila.task_done()

    # ——— BUSCA NA API E REGISTRA ———
    async def buscar_e_registrar(self, membro: discord.Member, nick: str, msg_status, guild: discord.Guild):
        async with aiohttp.ClientSession() as session:
            url = API_BUSCA.format(nick)
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return await msg_status.edit(content=f'❌ API retornou erro ({resp.status}) pra **{nick}**.')
                dados = await resp.json()

        jogadores = dados.get('players', [])

        # Filtra todos com o nick exato
        candidatos = [p for p in jogadores if p.get('Name', '').lower() == nick.lower()]
 
        # Prioriza quem tem GuildId (evita pegar perfil errado quando há duplicatas)
        jogador = next((p for p in candidatos if p.get('GuildId')), None)
 
        # Se nenhum tiver guilda, pega o primeiro
        if not jogador:
            jogador = candidatos[0] if candidatos else None

        if not jogador:
            return await msg_status.edit(content=f'❌ Personagem **{nick}** não encontrado no Albion Online.')

        nome_real      = jogador.get('Name')
        guild_id       = jogador.get('GuildId')
        guild_name     = jogador.get('GuildName')
        alliance_id    = jogador.get('AllianceId')

        # ——— É DA DIE HARD ———
        if guild_id == GUILDA_ALBION_ID:
            cargo = guild.get_role(CARGOS.get("DIE HARD"))
            cargo_recem_chegado = guild.get_role(CARGOS.get("recém chegado"))
            novo_nick = f'[DH] {nome_real}'

            try:
                await membro.edit(nick=novo_nick[:32])
                if cargo:
                    await membro.add_roles(cargo)
                if cargo_recem_chegado and cargo_recem_chegado in membro.roles:
                    await membro.remove_roles(cargo_recem_chegado)
                await msg_status.edit(
                    content=f'✅ **{nome_real}** registrado como membro da **Die Hard**!\n'
                            f'👤 Apelido alterado para `{novo_nick}`\n'
                            f'🛡️ Cargo <@&{cargo.id}> atribuído a {membro.mention}.'
                )
            except discord.Forbidden:
                await msg_status.edit(content=f'❌ Sem permissão pra alterar apelido/cargo de {membro.mention}. Verifica se meu cargo está acima do dele.')
            return

        # ——— É DA ALIANÇA (mas não da Die Hard) ———
        if ALIANCA_ALBION_ID and alliance_id == ALIANCA_ALBION_ID:
            cargo = guild.get_role(CARGOS.get("aliado"))
            novo_nick = f'[ALLY] {nome_real}'

            try:
                await membro.edit(nick=novo_nick[:32])
                if cargo:
                    await membro.add_roles(cargo)
                await msg_status.edit(
                    content=f'✅ **{nome_real}** registrado como **Aliado** (guilda: {guild_name})!\n'
                            f'👤 Apelido alterado para `{novo_nick}`\n'
                            f'🤝 Cargo <@&{cargo.id}> atribuído a {membro.mention}.'
                )
            except discord.Forbidden:
                await msg_status.edit(content=f'❌ Sem permissão pra alterar apelido/cargo de {membro.mention}.')
            return

        # ——— NÃO É NEM DIE HARD NEM ALIANÇA ———
        await msg_status.edit(
            content=f'⚠️ **{nome_real}** não pertence à guilda principal nem à aliança.\n'
                    f'Guilda atual: **{guild_name or "Sem guilda"}**'
        )

    # ——— COMANDO !registrar NICK ou !registrar @membro NICK ———
    @commands.command()
    async def registrar(self, ctx, alvo: discord.Member = None, *, nick: str = None):
        """Uso: !registrar Zezinho OU !registrar @membro Zezinho"""
        
        # Se a pessoa não marcou ninguém, o alvo é ela mesma e a primeira palavra é o nick
        if nick is None and alvo is None:
             return await ctx.send('❌ Uso correto: `!registrar SeuNick` ou `!registrar @membro Nick`', delete_after=10)
             
        if nick is None and isinstance(alvo, discord.Member) == False:
             pass 

        # Lógica para permitir !registrar Zezinho (onde alvo vira o próprio autor da msg)
        membro_final = ctx.author
        nick_final = ""

        partes = ctx.message.content.split()
        if len(partes) >= 2:
            if ctx.message.mentions:
                membro_final = ctx.message.mentions[0]
                nick_final = " ".join(partes[2:])
            else:
                nick_final = " ".join(partes[1:])
        
        if not nick_final:
             return await ctx.send('❌ Você esqueceu de informar o Nick!', delete_after=5)

        posicao = self.fila.qsize() + 1
        msg_status = await ctx.send(f'🔍 Buscando **{nick_final}** na API do Albion... (posição na fila: {posicao})')

        await self.fila.put((membro_final, nick_final, msg_status, ctx.guild))

async def setup(bot):
    await bot.add_cog(RegistrarCog(bot))
