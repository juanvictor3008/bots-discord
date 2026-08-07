import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

from config import (
    CARGOS, GUILDA_ALBION_ID, ALIANCA_ALBION_ID,
    MONGO_URI, colecao_tempo_call,
    MINIMO_PESSOAS_CALL, PONTOS_POR_CICLO,
    MULTIPLICADOR_CALLER, MENSAGEM_CLASSES_ID, REACOES_CLASSES, CANAIS_GERADORES_IDS,
    CANAL_LOGS_ID
)
from cogs.lfg import _eh_staff
from utils import log_discord


# Arquivo JSON para tempo de call (fallback local)
ARQUIVO_TEMPO = "data/tempo_call.json"

def _carregar_tempo():
    """Carrega os dados de tempo do arquivo JSON."""
    if not os.path.exists(ARQUIVO_TEMPO):
        return {}
    try:
        with open(ARQUIVO_TEMPO, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def _salvar_tempo(dados):
    """Salva os dados de tempo no arquivo JSON."""
    os.makedirs(os.path.dirname(ARQUIVO_TEMPO), exist_ok=True)
    with open(ARQUIVO_TEMPO, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=4, ensure_ascii=False, default=str)

def _usando_mongo():
    return MONGO_URI is not None and colecao_tempo_call is not None

async def _ler_tempo_mongo(id_str):
    """Lê tempo do MongoDB. Retorna dict com minutos_acumulados e ultima_entrada (aware)."""
    doc = await colecao_tempo_call.find_one({"_id": id_str})
    if not doc:
        return {"minutos_acumulados": 0, "ultima_entrada": None}
    ultima = doc.get("ultima_entrada")
    if ultima and ultima.tzinfo is None:
        ultima = ultima.replace(tzinfo=timezone.utc)
    return {"minutos_acumulados": doc.get("minutos_acumulados", 0), "ultima_entrada": ultima}

async def _salvar_entrada_mongo(id_str, agora):
    """Salva horário de entrada no MongoDB."""
    await colecao_tempo_call.update_one(
        {"_id": id_str},
        {"$set": {"ultima_entrada": agora.replace(tzinfo=None)}},
        upsert=True
    )

async def _atualizar_tempo_mongo(id_str, minutos_novos, nova_entrada=None):
    """Atualiza tempo acumulado e opcionalmente zera a entrada."""
    update = {"$inc": {"minutos_acumulados": minutos_novos}}
    if nova_entrada is None:
        update["$set"] = {"ultima_entrada": None}
    else:
        update["$set"] = {"ultima_entrada": nova_entrada.replace(tzinfo=None)}
    await colecao_tempo_call.update_one({"_id": id_str}, update, upsert=True)

async def _resetar_tempo_mongo():
    """Zera todos os tempos (usado após sorteio)."""
    await colecao_tempo_call.update_many({}, {"$set": {"minutos_acumulados": 0, "ultima_entrada": None}})

class Automacoes(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.calls_temporarias = set()  # rastreia por ID, não por nome
        self.auditoria_stop = False
        self.tarefa_auditoria = None

    @commands.Cog.listener()
    async def on_ready(self):
        try:
            if not self.auditoria_guilda.is_running():
                self.auditoria_guilda.start()
        except Exception as e:
            print(f"⚠️ Erro ao iniciar auditoria_guilda: {e}")
        try:
            if not self.farm_de_pontos.is_running():
                self.farm_de_pontos.start()
        except Exception as e:
            print(f"⚠️ Erro ao iniciar farm_de_pontos: {e}")
        try:
            if not self.atualizar_tempo_call.is_running():
                self.atualizar_tempo_call.start()
        except Exception as e:
            print(f"⚠️ Erro ao iniciar atualizar_tempo_call: {e}")

    # ==========================================
    # SISTEMA DE AUDITORIA DE MEMBROS
    # ==========================================
    @tasks.loop(hours=24)
    async def auditoria_guilda(self):
        await self._rodar_auditoria()

    @auditoria_guilda.before_loop
    async def _antes_auditoria(self):
        # Primeira execução só 24h após o boot (evita correr na inicialização)
        await asyncio.sleep(86400)

    async def _rodar_auditoria(self):
        self.auditoria_stop = False
        print("🔍 Iniciando ronda de auditoria na API do Albion...")

        if not self.bot.guilds:
            return

        guilda_discord = self.bot.guilds[0]
        id_cargo_dh = CARGOS.get("DIE HARD")
        cargo_dh = None

        for g in self.bot.guilds:
            cargo_dh = g.get_role(id_cargo_dh)
            if cargo_dh:
                guilda_discord = g
                break

        roster_nomes = set()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://gameinfo.albiononline.com/api/gameinfo/guilds/{GUILDA_ALBION_ID}/members") as resp:
                    if resp.status != 200:
                        await log_discord(self.bot, f"❌ **Auditoria:** API de membros da guilda retornou status {resp.status} — auditoria abortada.", "erro")
                        return
                    dados = await resp.json()
                    roster_nomes = {m.get("Name", "").lower() for m in dados if m.get("Name")}
                    print(f"📊 Roster da guilda: {len(roster_nomes)} jogadores.")
        except Exception as e:
            await log_discord(self.bot, f"❌ **Auditoria:** erro ao buscar roster da guilda: {type(e).__name__}: {e}", "erro")
            return

        demovidos = []
        falhas = []

        nomes_imunes = ["lider", "recrutador", "moderador", "caller", "SUB-LIDER"]
        ids_imunes = [CARGOS.get(nome) for nome in nomes_imunes if CARGOS.get(nome)]

        for membro in guilda_discord.members:
            if self.auditoria_stop:
                print("🛑 Auditoria interrompida pelo usuário.")
                break
            if membro.bot:
                continue

            if not cargo_dh or cargo_dh not in membro.roles:
                continue

            if any(c.id in ids_imunes for c in membro.roles):
                continue

            nick = membro.display_name
            if " " in nick:
                nick = nick.split(" ", 1)[1]

            if nick.lower() in roster_nomes:
                continue

            # Não está no roster da guilda → remove SÓ o cargo DIE HARD (preserva recém chegado e outros)
            try:
                await membro.remove_roles(cargo_dh)
                demovidos.append(membro)
                print(f"⚠️ {membro.display_name} foi rebaixado.")

                # Remove a tag [DH] do apelido
                nick_atual = membro.display_name
                novo_nick = None
                if nick_atual.lower().startswith("[dh] "):
                    novo_nick = nick_atual[5:]
                elif nick_atual.lower() == "[dh]":
                    novo_nick = nick_atual
                if novo_nick is not None and novo_nick.strip():
                    try:
                        await membro.edit(nick=novo_nick[:32])
                        await asyncio.sleep(2)
                    except discord.Forbidden:
                        await log_discord(self.bot, f"⚠️ **Auditoria:** não consegui remover a tag `[DH]` do apelido de **{membro.display_name}** (hierarquia).", "aviso")

                ganhou_recem_chegado = False
                if not any(c.id in CARGOS.values() for c in membro.roles):
                    cargo_recem = guilda_discord.get_role(CARGOS.get("recém chegado"))
                    if cargo_recem:
                        try:
                            await membro.add_roles(cargo_recem)
                            ganhou_recem_chegado = True
                        except discord.Forbidden:
                            await log_discord(self.bot, f"⚠️ **Auditoria:** não consegui dar o cargo recém chegado pra **{membro.display_name}** (hierarquia).", "aviso")

                try:
                    aviso = "⚠️ **Aviso Automático:** Seu cargo de **Die Hard** foi removido porque nosso sistema detectou que você não está mais na guilda no jogo. Se isso for um erro, use o comando `!registrar` novamente!"
                    if ganhou_recem_chegado:
                        aviso += "\n🆕 Você recebeu o cargo **recém chegado** enquanto não estiver registrado na guilda."
                    await membro.send(aviso)
                except discord.Forbidden:
                    pass
            except discord.Forbidden:
                falhas.append(f"• {membro.mention} (`{nick}`) — **sem permissão**: o cargo do bot precisa ficar ACIMA do cargo dele")
                await log_discord(self.bot, f"❌ **Auditoria:** sem permissão pra remover cargo de **{membro.display_name}** — verifica a hierarquia de cargos do bot.", "erro")
                print(f"❌ Sem permissão pra rebaixar {membro.display_name}")
            except Exception as e:
                falhas.append(f"• {membro.mention} (`{nick}`) — erro ao remover cargo: {type(e).__name__}: {e}")
                await log_discord(self.bot, f"❌ **Auditoria:** erro ao remover cargo de **{membro.display_name}**: {type(e).__name__}: {e}", "erro")

            await asyncio.sleep(5)

        canal_logs = self.bot.get_channel(CANAL_LOGS_ID)

        if demovidos or falhas:
            if canal_logs:
                desc = ""
                if demovidos:
                    nomes = "\n".join(f"• {m.mention} (`{m.display_name}`)" for m in demovidos)
                    desc += f"**{len(demovidos)}** membro(s) tiveram o cargo DIE HARD removido:\n{nomes}\n\n"
                if falhas:
                    desc += f"**{len(falhas)}** falha(s):\n" + "\n".join(falhas)
                embed = discord.Embed(
                    title="🔍 Auditoria — Relatório",
                    description=desc,
                    color=discord.Color.red() if demovidos else discord.Color.gold(),
                    timestamp=datetime.now(timezone.utc)
                )
                try:
                    await canal_logs.send(embed=embed)
                except Exception as e:
                    print(f"⚠️ Erro ao enviar relatório de auditoria: {e}")
        else:
            if canal_logs:
                embed = discord.Embed(
                    title="🔍 Auditoria — Sem problemas",
                    description="Todos os membros com cargo DIE HARD estão na guilda.",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc)
                )
                try:
                    await canal_logs.send(embed=embed)
                except Exception as e:
                    print(f"⚠️ Erro ao enviar relatório de auditoria: {e}")

        print("✅ Base concluída.")

    # ==========================================
    # SISTEMA DE FARM DE PONTOS EM CALL
    # ==========================================
    @tasks.loop(minutes=10)
    async def farm_de_pontos(self):
        pass

    # ==========================================
    # SISTEMA DE RASTREAMENTO DE TEMPO INDIVIDUAL EM CALL
    # ==========================================
    @tasks.loop(minutes=1)
    async def atualizar_tempo_call(self):
        try:
            for guilda in self.bot.guilds:
                for canal_voz in guilda.voice_channels:
                    for membro in canal_voz.members:
                        if membro.bot:
                            continue
                        id_str = str(membro.id)

                        if _usando_mongo():
                            user_tempo = await _ler_tempo_mongo(id_str)
                            if user_tempo["ultima_entrada"]:
                                agora = datetime.now(timezone.utc)
                                minutos_desde = (agora - user_tempo["ultima_entrada"]).total_seconds() / 60
                                if minutos_desde >= 1:
                                    await _atualizar_tempo_mongo(id_str, int(minutos_desde), agora)
                            else:
                                await _salvar_entrada_mongo(id_str, datetime.now(timezone.utc))
                        else:
                            dados_tempo = _carregar_tempo()
                            user_tempo = dados_tempo.get(id_str, {})
                            if user_tempo.get("ultima_entrada"):
                                try:
                                    ultima = datetime.fromisoformat(user_tempo["ultima_entrada"])
                                    if ultima.tzinfo is None:
                                        ultima = ultima.replace(tzinfo=timezone.utc)
                                    agora = datetime.now(timezone.utc)
                                    minutos_desde = (agora - ultima).total_seconds() / 60
                                    if minutos_desde >= 1:
                                        user_tempo["minutos_acumulados"] = user_tempo.get("minutos_acumulados", 0) + int(minutos_desde)
                                        user_tempo["ultima_entrada"] = agora.isoformat()
                                        dados_tempo[id_str] = user_tempo
                                except (ValueError, TypeError):
                                    pass
                            else:
                                agora = datetime.now(timezone.utc)
                                user_tempo["ultima_entrada"] = agora.isoformat()
                                dados_tempo[id_str] = user_tempo
                            _salvar_tempo(dados_tempo)
        except Exception as e:
            print(f"⚠️ Erro no loop de tempo de call: {e}")

    # ==========================================
    # EVENTOS DE CARGO POR REAÇÃO (REACTION ROLES)
    # ==========================================
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.message_id != MENSAGEM_CLASSES_ID:
            return

        if payload.member.bot:
            return

        emoji = str(payload.emoji)
        id_cargo = REACOES_CLASSES.get(emoji)

        if id_cargo:
            guild = self.bot.get_guild(payload.guild_id)
            cargo = guild.get_role(id_cargo)
            if cargo:
                try:
                    await payload.member.add_roles(cargo)
                    print(f"✅ {payload.member.display_name} pegou a classe de {cargo.name}.")
                except discord.Forbidden:
                    print("❌ Erro: O cargo do bot precisa estar acima do cargo da classe.")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        if payload.message_id != MENSAGEM_CLASSES_ID:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        membro = guild.get_member(payload.user_id)
        if not membro or membro.bot:
            return

        emoji = str(payload.emoji)
        id_cargo = REACOES_CLASSES.get(emoji)

        if id_cargo:
            cargo = guild.get_role(id_cargo)
            if cargo:
                try:
                    await membro.remove_roles(cargo)
                    print(f"🔴 {membro.display_name} removeu a classe de {cargo.name}.")
                except discord.Forbidden:
                    pass

    # ==========================================
    # CRIADOR DE CALLS DINÂMICAS
    # ==========================================
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):

        print(f"👀 ALERTA: O bot detectou movimentação de voz do membro {member.name}!")

        # --- RASTREAMENTO DE TEMPO INDIVIDUAL ---
        id_str = str(member.id)
        agora = datetime.now(timezone.utc)

        try:
            if _usando_mongo():
                user_tempo = await _ler_tempo_mongo(id_str)

                # ENTRou em call
                if after.channel and not before.channel:
                    await _salvar_entrada_mongo(id_str, agora)

                # SAIU de call
                elif before.channel and not after.channel:
                    if user_tempo["ultima_entrada"]:
                        minutos = int((agora - user_tempo["ultima_entrada"]).total_seconds() / 60)
                        await _atualizar_tempo_mongo(id_str, minutos, None)

                # MUDOU de canal
                elif before.channel and after.channel and before.channel.id != after.channel.id:
                    if user_tempo["ultima_entrada"]:
                        minutos = int((agora - user_tempo["ultima_entrada"]).total_seconds() / 60)
                        await _atualizar_tempo_mongo(id_str, minutos, agora)
            else:
                dados_tempo = _carregar_tempo()
                user_tempo = dados_tempo.get(id_str, {"minutos_acumulados": 0, "ultima_entrada": None})

                # ENTRou em call
                if after.channel and not before.channel:
                    user_tempo["ultima_entrada"] = agora.isoformat()
                    dados_tempo[id_str] = user_tempo
                    _salvar_tempo(dados_tempo)

                # SAIU de call
                elif before.channel and not after.channel:
                    if user_tempo.get("ultima_entrada"):
                        try:
                            ultima = datetime.fromisoformat(user_tempo["ultima_entrada"])
                            if ultima.tzinfo is None:
                                ultima = ultima.replace(tzinfo=timezone.utc)
                            minutos = int((agora - ultima).total_seconds() / 60)
                            user_tempo["minutos_acumulados"] = user_tempo.get("minutos_acumulados", 0) + minutos
                            user_tempo["ultima_entrada"] = None
                            dados_tempo[id_str] = user_tempo
                            _salvar_tempo(dados_tempo)
                        except (ValueError, TypeError):
                            pass

                # MUDOU de canal
                elif before.channel and after.channel and before.channel.id != after.channel.id:
                    if user_tempo.get("ultima_entrada"):
                        try:
                            ultima = datetime.fromisoformat(user_tempo["ultima_entrada"])
                            if ultima.tzinfo is None:
                                ultima = ultima.replace(tzinfo=timezone.utc)
                            minutos = int((agora - ultima).total_seconds() / 60)
                            user_tempo["minutos_acumulados"] = user_tempo.get("minutos_acumulados", 0) + minutos
                            user_tempo["ultima_entrada"] = agora.isoformat()
                            dados_tempo[id_str] = user_tempo
                            _salvar_tempo(dados_tempo)
                        except (ValueError, TypeError):
                            pass
        except Exception as e:
            print(f"⚠️ Erro no rastreamento de voz para {member.name}: {e}")

        if after.channel:
            print(f"➡️ Canal destino: {after.channel.name} | ID: {after.channel.id}")
            print(f"📋 IDs permitidos no config.py: {CANAIS_GERADORES_IDS}")

            if after.channel.id in CANAIS_GERADORES_IDS:
                print("✅ SUCESSO: O ID bateu com o gerador! Iniciando criação da call...")
            else:
                print("❌ FALHA: O ID do canal não está na lista de geradores.")

        # --- 1. ENTROU NUM GERADOR ---
        if after.channel and after.channel.id in CANAIS_GERADORES_IDS:
            guilda = member.guild
            categoria = after.channel.category

            permissoes = dict(after.channel.overwrites)
            permissoes[member] = discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)

            try:
                novo_canal = await guilda.create_voice_channel(
                    name=f"🎮 {member.display_name}",
                    category=categoria,
                    overwrites=permissoes
                )

                # Rastreia por ID — funciona mesmo se o membro renomear a call
                self.calls_temporarias.add(novo_canal.id)
                print("✅ Sala temporária criada no Discord!")

                if member.voice and member.voice.channel:
                    await member.move_to(novo_canal)
                    print(f"✅ {member.name} movido para a sala temporária!")
                else:
                    await novo_canal.delete()
                    self.calls_temporarias.discard(novo_canal.id)

            except Exception as e:
                print(f"⚠️ Erro na criação de call temporária: {e}")

        # --- 2. SAIU DE UMA CALL TEMPORÁRIA ---
        if before.channel and before.channel.id in self.calls_temporarias:
            if len(before.channel.members) == 0:
                try:
                    await before.channel.delete()
                    self.calls_temporarias.discard(before.channel.id)
                except discord.NotFound:
                    self.calls_temporarias.discard(before.channel.id)
                except Exception as e:
                    print(f"⚠️ Erro ao apagar call temporária: {e}")

        # --- 3. TIMER DE CALL VAZIA + PRESENÇA ---
        try:
            lfg_cog = self.bot.get_cog("LFG")
            if lfg_cog:
                call_ids_ativos = set()
                for evento in lfg_cog.eventos_ativos:
                    cid = evento.get("call_id")
                    if cid and evento.get("status", "formando") not in ("encerrado", "encerrando"):
                        call_ids_ativos.add(cid)

                print(f"📋 Call IDs ativos: {call_ids_ativos}")

                def _painel_do_call(cid):
                    for p in lfg_cog.paineis_ativos.values():
                        if p.call_id == cid and p.status not in ("encerrado", "encerrando"):
                            return p
                    return None

                if after.channel and not before.channel:
                    if after.channel.id in call_ids_ativos:
                        lfg_cog._cancelar_timer_vazio(after.channel.id)
                        painel = _painel_do_call(after.channel.id)
                        if painel and painel.status == "em_andamento":
                            await lfg_cog.registrar_entrada_call(after.channel.id, id_str)

                elif before.channel and not after.channel:
                    if before.channel.id in call_ids_ativos:
                        await lfg_cog.registrar_saida_call(before.channel.id, id_str)
                        canal = member.guild.get_channel(before.channel.id)
                        membros_restantes = len(canal.members) if canal else -1
                        print(f"🚪 {member.name} saiu de call ativa. Membros restantes: {membros_restantes}")
                        if canal and len(canal.members) == 0:
                            print(f"⏳ Iniciando timer de {lfg_cog.TEMPO_TOLERANCIA_VAZIA}s para call {before.channel.id}")
                            lfg_cog._iniciar_timer_vazio(before.channel.id)

                elif before.channel and after.channel and before.channel.id != after.channel.id:
                    if before.channel.id in call_ids_ativos:
                        await lfg_cog.registrar_saida_call(before.channel.id, id_str)
                        canal = member.guild.get_channel(before.channel.id)
                        membros_restantes = len(canal.members) if canal else -1
                        print(f"🔀 {member.name} trocou de call. Membros restantes na origem: {membros_restantes}")
                        if canal and len(canal.members) == 0:
                            print(f"⏳ Iniciando timer de {lfg_cog.TEMPO_TOLERANCIA_VAZIA}s para call {before.channel.id}")
                            lfg_cog._iniciar_timer_vazio(before.channel.id)
                    if after.channel.id in call_ids_ativos:
                        lfg_cog._cancelar_timer_vazio(after.channel.id)
                        painel = _painel_do_call(after.channel.id)
                        if painel and painel.status == "em_andamento":
                            await lfg_cog.registrar_entrada_call(after.channel.id, id_str)
        except Exception as e:
            print(f"⚠️ Erro no timer de call vazia: {type(e).__name__}: {e}")

    @commands.command(name="limpar")
    async def limpar(self, ctx):
        if not _eh_staff(ctx.author):
            return await ctx.send("❌ Apenas staff pode usar esse comando.", delete_after=10)

        def verificar_mensagem(msg):
            return not msg.pinned

        try:
            deletadas = await ctx.channel.purge(limit=None, check=verificar_mensagem)
            await ctx.send(f"🧹 Canal limpo! {len(deletadas)} mensagens apagadas.", delete_after=10)
        except Exception as e:
            await ctx.send(f"⚠️ Erro ao limpar o canal: {e}", delete_after=10)

    @commands.command(name="auditar")
    async def auditar(self, ctx):
        if not _eh_staff(ctx.author):
            return await ctx.send("❌ Apenas staff pode usar esse comando.", delete_after=10)

        if self.tarefa_auditoria and not self.tarefa_auditoria.done():
            return await ctx.send("⚠️ Já existe uma auditoria em andamento. Use `!pararauditoria` pra interromper.")

        msg = await ctx.send("🔍 Iniciando auditoria manual... Isso pode levar alguns minutos.")
        self.tarefa_auditoria = asyncio.create_task(self._rodar_auditoria())
        try:
            await self.tarefa_auditoria
            await msg.edit(content="✅ Auditoria concluída! Confira o relatório no canal de logs.")
        except asyncio.CancelledError:
            await msg.edit(content="🛑 Auditoria interrompida.")
        except Exception as e:
            await msg.edit(content=f"❌ Erro durante a auditoria: `{type(e).__name__}: {e}`")

    @commands.command(name="pararauditoria")
    async def parar_auditoria(self, ctx):
        if not _eh_staff(ctx.author):
            return await ctx.send("❌ Apenas staff pode usar esse comando.", delete_after=10)

        self.auditoria_stop = True
        if self.tarefa_auditoria and not self.tarefa_auditoria.done():
            self.tarefa_auditoria.cancel()
            await ctx.send("🛑 Auditoria sendo interrompida...")
        else:
            await ctx.send("🛑 Auditoria marcada pra parar (vale também pro loop agendado de 24h).")

    @commands.command(name="conferir")
    async def conferir(self, ctx, membro: discord.Member):
        if not _eh_staff(ctx.author):
            return await ctx.send("❌ Apenas staff pode usar esse comando.", delete_after=10)

        guild = ctx.guild
        id_cargo_dh = CARGOS.get("DIE HARD")
        cargo_dh = guild.get_role(id_cargo_dh)

        nomes_imunes = ["lider", "recrutador", "moderador", "caller", "SUB-LIDER"]
        ids_imunes = [CARGOS.get(nome) for nome in nomes_imunes if CARGOS.get(nome)]
        tem_imune = [c.name for c in membro.roles if c.id in ids_imunes]

        tem_cargo_die_hard = bool(cargo_dh) and cargo_dh in membro.roles

        linhas = [
            f"👤 {membro.mention} (`{membro.display_name}`)",
            f"🛡️ Tem DIE HARD: {'sim' if tem_cargo_die_hard else 'não'}",
            f"🚫 Cargos imunes: {', '.join(tem_imune) or 'nenhum'}",
        ]

        if not tem_cargo_die_hard:
            linhas.append("ℹ️ Sem cargo DIE HARD — a auditoria não mexe com ele.")
            return await ctx.send("\n".join(linhas))

        if tem_imune:
            linhas.append("❌ Membro é imune (staff) — a auditoria pula ele.")
            return await ctx.send("\n".join(linhas))

        nick = membro.display_name
        if " " in nick:
            nick = nick.split(" ", 1)[1]
        linhas.append(f"🔍 Nick extraído: `{nick}`")

        msg = await ctx.send("\n".join(linhas) + "\n\n⏳ Consultando roster da guilda...")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://gameinfo.albiononline.com/api/gameinfo/guilds/{GUILDA_ALBION_ID}/members") as resp:
                    if resp.status != 200:
                        await msg.edit(content="\n".join(linhas) + f"\n❌ API retornou status {resp.status}")
                        return
                    dados = await resp.json()
        except Exception as e:
            await msg.edit(content="\n".join(linhas) + f"\n❌ Erro na API: `{type(e).__name__}: {e}`")
            return

        roster_nomes = {m.get("Name", "").lower() for m in dados if m.get("Name")}

        if nick.lower() in roster_nomes:
            linhas.append(f"✅ `{nick}` está no roster da guilda ({len(roster_nomes)} jogadores) — nada a fazer.")
        else:
            linhas.append(f"❌ `{nick}` NÃO está no roster da guilda ({len(roster_nomes)} jogadores) — seria rebaixado (nada removido no debug).")

        await msg.edit(content="\n".join(linhas))


async def setup(bot):
    await bot.add_cog(Automacoes(bot))
