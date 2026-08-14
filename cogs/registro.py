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

    # ——— REGISTRO FALLBACK VIA ROSTER CACHEADO (API instável) ———
    async def _registrar_pelo_cache(self, membro: discord.Member, nick: str, msg_status, guild: discord.Guild):
        """Registra como Die Hard usando o roster sincronizado pela auditoria quando a API ao vivo falha."""
        cargo = guild.get_role(CARGOS.get("DIE HARD"))
        cargo_recem_chegado = guild.get_role(CARGOS.get("recém chegado"))
        novo_nick = f'[DH] {nick}'

        try:
            await membro.edit(nick=novo_nick[:32])
            if cargo:
                await membro.add_roles(cargo)
            if cargo_recem_chegado and cargo_recem_chegado in membro.roles:
                await membro.remove_roles(cargo_recem_chegado)
            cargo_menção = f'<@&{cargo.id}>' if cargo else '**DIE HARD**'
            await msg_status.edit(
                content=f'✅ **{nick}** registrado como membro da **Die Hard** '
                        f'(dados do último roster sincronizado — API instável)!\n'
                        f'👤 Apelido alterado para `{novo_nick}`\n'
                        f'🛡️ Cargo {cargo_menção} atribuído a {membro.mention}.'
            )
        except discord.Forbidden:
            await msg_status.edit(
                content=f'❌ Sem permissão pra alterar apelido/cargo de {membro.mention}. Verifica se meu cargo está acima do dele.'
            )

    # ——— BUSCA NA API E REGISTRA ———
    async def buscar_e_registrar(self, membro: discord.Member, nick: str, msg_status, guild: discord.Guild):
        MAX_TENTATIVAS = 3
        TIMEOUT = 25  # API com picos de resposta lenta (30-40s+); 15s estourava com frequência

        # Cache do roster da guilda sincronizado pela auditoria (fallback quando a API ao vivo falha)
        automacoes_cog = self.bot.get_cog("Automacoes") if self.bot else None
        roster_cache = automacoes_cog.get_roster_cacheado() if automacoes_cog else None
        nick_no_roster = bool(roster_cache) and nick.lower() in roster_cache

        dados = None
        erro_api = None
        async with aiohttp.ClientSession() as session:
            url = API_BUSCA.format(nick)
            for tentativa in range(1, MAX_TENTATIVAS + 1):
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
                        if resp.status == 200:
                            dados = await resp.json()
                            break
                        if resp.status in (502, 503, 504):
                            # Erros temporários: retry com backoff crescente
                            erro_api = f'API do Albion instável ({resp.status})'
                        else:
                            # Erros sem retry (404, 400, ...)
                            return await msg_status.edit(content=f'❌ API retornou erro ({resp.status}) pra **{nick}**.')
                except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                    # Timeout ou falha de conexão: mesma política de retry
                    erro_api = f'API do Albion instável ({type(e).__name__})'

                if tentativa < MAX_TENTATIVAS:
                    await msg_status.edit(
                        content=f'🔍 Buscando **{nick}**... (API lenta, tentativa {tentativa + 1}/{MAX_TENTATIVAS})'
                    )
                    await asyncio.sleep(5 * tentativa)
                else:
                    break

        if dados is None:
            if nick_no_roster:
                # Fallback: completo o registro com os dados do último roster sincronizado
                return await self._registrar_pelo_cache(membro, nick, msg_status, guild)
            return await msg_status.edit(
                content=f'❌ {erro_api or "API do Albion não respondeu"} após {MAX_TENTATIVAS} tentativas. Tenta de novo em alguns minutos.'
            )

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
