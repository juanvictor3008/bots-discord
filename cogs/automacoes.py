import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

from config import (
    CANAL_LIMPEZA_ID, CARGOS, GUILDA_ALBION_ID, ALIANCA_ALBION_ID,
    MONGO_URI, colecao_tempo_call,
    MINIMO_PESSOAS_CALL, PONTOS_POR_CICLO,
    MULTIPLICADOR_CALLER, MENSAGEM_CLASSES_ID, REACOES_CLASSES, CANAIS_GERADORES_IDS
)


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

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.auditoria_guilda.is_running():
            self.auditoria_guilda.start()
        if not self.limpeza_automatica.is_running():
            self.limpeza_automatica.start()
        if not self.farm_de_pontos.is_running():
            self.farm_de_pontos.start()
        if not self.atualizar_tempo_call.is_running():
            self.atualizar_tempo_call.start()

    # ==========================================
    # SISTEMA DE LIMPEZA AUTOMÁTICA
    # ==========================================
    @tasks.loop(minutes=30)
    async def limpeza_automatica(self):
        if CANAL_LIMPEZA_ID == 0:
            return

        canal = self.bot.get_channel(CANAL_LIMPEZA_ID)
        if not canal:
            return

        limite_tempo = datetime.now(timezone.utc) - timedelta(hours=6)

        def verificar_mensagem(msg):
            return not msg.pinned

        try:
            deletadas = await canal.purge(limit=None, before=limite_tempo, check=verificar_mensagem)
            if len(deletadas) > 0:
                print(f"🧹 Faxina concluída! {len(deletadas)} mensagens apagadas no canal de limpeza.")
        except Exception as e:
            print(f"⚠️ Erro ao tentar limpar o chat: {e}")

    # ==========================================
    # SISTEMA DE AUDITORIA DE MEMBROS
    # ==========================================
    @tasks.loop(hours=24)
    async def auditoria_guilda(self):
        print("🔍 Iniciando ronda de auditoria diária na API do Albion...")

        if not self.bot.guilds:
            return

        guilda_discord = self.bot.guilds[0]

        cargos_gerenciados = []
        for id_cargo in CARGOS.values():
            cargo = guilda_discord.get_role(id_cargo)
            if cargo:
                cargos_gerenciados.append(cargo)

        for membro in guilda_discord.members:
            if membro.bot:
                continue

            nomes_imunes = ["lider", "DIE HARD", "recrutador", "moderador", "caller", "SUB-LIDER"]
            ids_imunes = [CARGOS.get(nome) for nome in nomes_imunes if CARGOS.get(nome)]

            if any(c.id in ids_imunes for c in membro.roles):
                continue

            cargos_do_membro = [c for c in cargos_gerenciados if c in membro.roles]

            if not cargos_do_membro:
                continue

            nick = membro.display_name
            if " " in nick:
                nick = nick.split(" ", 1)[1]

            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(f"https://gameinfo.albiononline.com/api/gameinfo/search?q={nick}") as resp:
                        if resp.status != 200:
                            await asyncio.sleep(2)
                            continue

                        dados = await resp.json()
                        jogadores = dados.get('players', [])
                        jogador_encontrado = next((p for p in jogadores if p['Name'].lower() == nick.lower()), None)

                        rebaixar = False

                        if not jogador_encontrado:
                            rebaixar = True
                        else:
                            guild_id_jogador = jogador_encontrado.get('GuildId')
                            alliance_id_jogador = jogador_encontrado.get('AllianceId')

                            is_guilda = (guild_id_jogador == GUILDA_ALBION_ID)
                            is_alianca = (ALIANCA_ALBION_ID and alliance_id_jogador == ALIANCA_ALBION_ID)

                            if not is_guilda and not is_alianca:
                                rebaixar = True

                        if rebaixar:
                            await membro.remove_roles(*cargos_do_membro)
                            print(f"⚠️ {membro.display_name} foi rebaixado.")

                            try:
                                await membro.send("⚠️ **Aviso Automático:** Seus cargos no Discord da guilda foram removidos porque nosso sistema detectou que você não está mais na guilda/aliança no jogo. Se isso for um erro, use o comando `!registrar` novamente!")
                            except discord.Forbidden:
                                pass

                except Exception as e:
                    print(f"Erro na auditoria do jogador {nick}: {e}")

            await asyncio.sleep(2)

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

            permissoes = {
                guilda.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
            }

            for nome, id_cargo in CARGOS.items():
                cargo_obj = guilda.get_role(id_cargo)
                if cargo_obj:
                    permissoes[cargo_obj] = discord.PermissionOverwrite(view_channel=True, connect=True)

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
                    if cid and evento.get("status", "formando") != "encerrado":
                        call_ids_ativos.add(cid)

                print(f"📋 Call IDs ativos: {call_ids_ativos}")

                def _painel_do_call(cid):
                    for p in lfg_cog.paineis_ativos.values():
                        if p.call_id == cid and p.status != "encerrado":
                            return p
                    return None

                if after.channel and not before.channel:
                    if after.channel.id in call_ids_ativos:
                        lfg_cog._cancelar_timer_vazio(after.channel.id)
                        painel = _painel_do_call(after.channel.id)
                        if painel and painel.status == "em_andamento":
                            lfg_cog.registrar_entrada_call(after.channel.id, id_str)

                elif before.channel and not after.channel:
                    if before.channel.id in call_ids_ativos:
                        lfg_cog.registrar_saida_call(before.channel.id, id_str)
                        canal = member.guild.get_channel(before.channel.id)
                        membros_restantes = len(canal.members) if canal else -1
                        print(f"🚪 {member.name} saiu de call ativa. Membros restantes: {membros_restantes}")
                        if canal and len(canal.members) == 0:
                            print(f"⏳ Iniciando timer de {lfg_cog.TEMPO_TOLERANCIA_VAZIA}s para call {before.channel.id}")
                            lfg_cog._iniciar_timer_vazio(before.channel.id)

                elif before.channel and after.channel and before.channel.id != after.channel.id:
                    if before.channel.id in call_ids_ativos:
                        lfg_cog.registrar_saida_call(before.channel.id, id_str)
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
                            lfg_cog.registrar_entrada_call(after.channel.id, id_str)
        except Exception as e:
            print(f"⚠️ Erro no timer de call vazia: {type(e).__name__}: {e}")


async def setup(bot):
    await bot.add_cog(Automacoes(bot))
