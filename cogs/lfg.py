import discord
import re
import json
import os
import asyncio
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta, timezone

from config import CARGOS, MONGO_URI, colecao_templates, colecao_eventos, CANAIS_GERADORES_IDS, colecao_pontos, colecao_presencas
from cogs.economia import salvar_pontos


FUSO = timezone(timedelta(hours=-3))

DIAS_SEMANA = {
    "seg": 0, "ter": 1, "qua": 2, "qui": 3,
    "sex": 4, "sab": 5, "dom": 6,
}

TEMPLATES_PATH = "data/templates.json"
EVENTOS_PATH = "data/eventos.json"

def _usando_mongo():
    return MONGO_URI is not None and colecao_templates is not None

def _usando_mongo_presencas():
    return MONGO_URI is not None and colecao_presencas is not None

def _eh_staff(usuario):
    if usuario.guild_permissions.administrator:
        return True
    ids_staff = [CARGOS.get(n) for n in ["lider", "SUB-LIDER", "moderador"] if CARGOS.get(n)]
    return any(c.id in ids_staff for c in usuario.roles)

def _eh_lider_ou_staff(usuario, autor_id):
    if usuario.id == autor_id:
        return True
    return _eh_staff(usuario)

# ==========================================
# PERSISTÊNCIA DE TEMPLATES
# ==========================================

async def carregar_templates():
    if _usando_mongo():
        templates = {}
        async for doc in colecao_templates.find():
            templates[doc["_id"]] = {"vagas": doc.get("vagas", {}), "descricao": doc.get("descricao"), "criador_id": doc.get("criador_id")}
        return templates
    if os.path.exists(TEMPLATES_PATH):
        try:
            with open(TEMPLATES_PATH, "r", encoding="utf-8") as f:
                dados = json.load(f)
            for nome, t in dados.items():
                if "criador_id" not in t:
                    t["criador_id"] = None
            return dados
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


async def salvar_templates(templates):
    if _usando_mongo():
        await colecao_templates.delete_many({})
        if templates:
            docs = [{"_id": nome, "vagas": dados["vagas"], "descricao": dados.get("descricao"), "criador_id": dados.get("criador_id")} for nome, dados in templates.items()]
            await colecao_templates.insert_many(docs)
    else:
        with open(TEMPLATES_PATH, "w", encoding="utf-8") as f:
            json.dump(templates, f, ensure_ascii=False, indent=2)


# ==========================================
# PERSISTÊNCIA DA AGENDA (eventos_ativos)
# ==========================================

async def carregar_eventos():
    if _usando_mongo():
        eventos = []
        async for doc in colecao_eventos.find():
            eventos.append(doc)
        return eventos
    if os.path.exists(EVENTOS_PATH):
        try:
            with open(EVENTOS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _migrar_evento(evento):
    """Migra evento do formato antigo (encerrado: bool) pro novo (status: str)."""
    if "status" not in evento:
        if evento.get("encerrado", False):
            evento["status"] = "encerrado"
        else:
            evento["status"] = "formando"
        evento.pop("encerrado", None)
    return evento


async def salvar_eventos(eventos):
    eventos = eventos_validos(eventos)
    if _usando_mongo():
        await colecao_eventos.delete_many({})
        if eventos:
            await colecao_eventos.insert_many(eventos)
    else:
        with open(EVENTOS_PATH, "w", encoding="utf-8") as f:
            json.dump(eventos, f, ensure_ascii=False, indent=2)


def eventos_validos(eventos):
    """Remove da lista os eventos expirados ou que já foram encerrados."""
    agora_ts = int(datetime.now(FUSO).timestamp())
    return [
        e for e in eventos
        if (e.get("unix_timestamp") is None or e.get("unix_timestamp", 0) > agora_ts - 3600)
        and e.get("status", "formando") != "encerrado"
    ]


# ==========================================
# PERSISTÊNCIA DE PRESENÇAS
# ==========================================

async def _salvar_presencas_mongo(call_id, presencas_dict):
    """Salva o dict de presenças de uma call no MongoDB."""
    if not _usando_mongo_presencas():
        return
    usuarios = {}
    for uid, dados in presencas_dict.items():
        entrada = dados["entrada"]
        if hasattr(entrada, "isoformat"):
            entrada = entrada.isoformat()
        usuarios[uid] = {"entrada": entrada, "minutos": dados.get("minutos", 0)}
    await colecao_presencas.update_one(
        {"_id": str(call_id)},
        {"$set": {"usuarios": usuarios}},
        upsert=True
    )

async def _remover_presencas_mongo(call_id):
    """Remove as presenças de uma call do MongoDB (usado ao encerrar)."""
    if not _usando_mongo_presencas():
        return
    await colecao_presencas.delete_one({"_id": str(call_id)})

async def _carregar_presencas_mongo():
    """Carrega todas as presenças ativas do MongoDB. Retorna {call_id: {user_id: {...}}}."""
    if not _usando_mongo_presencas():
        return {}
    resultado = {}
    async for doc in colecao_presencas.find():
        call_id = doc["_id"]
        presencas = {}
        dados_u = doc.get("usuarios", {})
        for uid, d in dados_u.items():
            entrada = d.get("entrada")
            if entrada is not None:
                if isinstance(entrada, str):
                    try:
                        entrada = datetime.fromisoformat(entrada)
                    except (ValueError, TypeError):
                        entrada = datetime.now(timezone.utc)
                if entrada.tzinfo is None:
                    entrada = entrada.replace(tzinfo=timezone.utc)
            presencas[uid] = {
                "entrada": entrada,
                "minutos": d.get("minutos", 0),
            }
        if presencas:
            resultado[call_id] = presencas
    return resultado


# ==========================================
# PARSER DE DATA/HORÁRIO FLEXÍVEL
# ==========================================

def interpretar_horario(texto: str):
    """
    Retorna o unix_timestamp de um texto de horário, ou None se não for reconhecido.

    Formatos aceitos:
      "20:30"             -> hoje às 20:30 (ou amanhã se já passou)
      "hoje 20:30"        -> hoje às 20:30
      "amanha 20:30"      -> amanhã às 20:30
      "sex 20:30"         -> próxima sexta-feira às 20:30
      "25-12 20:30"       -> dia 25/12 às 20:30
    """
    t = texto.strip().lower()
    agora = datetime.now(FUSO)

    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            data_evento = agora.replace(hour=h, minute=mi, second=0, microsecond=0)
            if data_evento < agora:
                data_evento += timedelta(days=1)
            return int(data_evento.timestamp())
        return None

    m = re.fullmatch(r"(hoje|amanha|amanhã)\s+(\d{1,2}):(\d{2})", t)
    if m:
        dia, h, mi = m.group(1), int(m.group(2)), int(m.group(3))
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            return None
        data_evento = agora.replace(hour=h, minute=mi, second=0, microsecond=0)
        if dia in ("amanha", "amanhã"):
            data_evento += timedelta(days=1)
        return int(data_evento.timestamp())

    m = re.fullmatch(r"(seg|ter|qua|qui|sex|sab|dom)\s+(\d{1,2}):(\d{2})", t)
    if m:
        dia_abrev, h, mi = m.group(1), int(m.group(2)), int(m.group(3))
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            return None
        alvo_weekday = DIAS_SEMANA[dia_abrev]
        dias_ate_alvo = (alvo_weekday - agora.weekday()) % 7
        data_evento = agora.replace(hour=h, minute=mi, second=0, microsecond=0)
        data_evento += timedelta(days=dias_ate_alvo)
        if data_evento < agora:
            data_evento += timedelta(days=7)
        return int(data_evento.timestamp())

    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", t)
    if m:
        dia, mes, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        try:
            data_evento = agora.replace(month=mes, day=dia, hour=h, minute=mi, second=0, microsecond=0)
        except ValueError:
            return None
        if data_evento < agora:
            try:
                data_evento = data_evento.replace(year=data_evento.year + 1)
            except ValueError:
                return None
        return int(data_evento.timestamp())

    return None


def eh_texto_de_horario(texto: str) -> bool:
    t = texto.strip().lower()
    padroes = [
        r"^\d{1,2}:\d{2}$",
        r"^(hoje|amanha|amanhã)\s+\d{1,2}:\d{2}$",
        r"^(seg|ter|qua|qui|sex|sab|dom)\s+\d{1,2}:\d{2}$",
        r"^\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}$",
    ]
    return any(re.fullmatch(p, t) for p in padroes)


def _extrair_message_id(jump_url):
    if jump_url:
        parts = jump_url.rstrip("/").split("/")
        if len(parts) >= 1:
            return parts[-1]
    return None


# ==========================================
# CLASSES DE INTERFACE (BOTÕES E PAINÉIS)
# ==========================================

class SelectClasses(discord.ui.Select):
    def __init__(self, view_pai):
        self.view_pai = view_pai
        opcoes = []
        for classe, vagas_totais in view_pai.max_vagas.items():
            inscritos = view_pai.jogadores.get(classe, [])
            texto = f"{len(inscritos)}/{vagas_totais} inscritos"
            opcoes.append(discord.SelectOption(label=classe[:100], value=classe, description=texto))
        super().__init__(
            placeholder="Escolha uma classe para entrar...",
            options=opcoes[:25],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        classe = self.values[0]
        await self.view_pai.processar_clique(interaction, classe)


class SelectGerenciarVagas(discord.ui.Select):
    """Select para líder/staff remover inscritos do painel."""
    def __init__(self, view_pai):
        self.view_pai = view_pai
        opcoes = []
        for classe, lista in view_pai.jogadores.items():
            for jogador in lista:
                label = f"{classe} - {jogador}"
                opcoes.append(discord.SelectOption(label=label[:100], value=f"{classe}|||{jogador}", description=f"Remover {jogador} de {classe}"))
        for classe, lista in view_pai.fila_espera.items():
            for jogador in lista:
                label = f"{classe} - {jogador} (Fila)"
                opcoes.append(discord.SelectOption(label=label[:100], value=f"fila|||{classe}|||{jogador}", description=f"Remover {jogador} da fila de {classe}"))
        if not opcoes:
            opcoes.append(discord.SelectOption(label="Nenhum inscrito", value="__vazio__", description="Não há jogadores para remover"))
        super().__init__(
            placeholder="Selecione um jogador para remover...",
            options=opcoes[:25],
        )

    async def callback(self, interaction: discord.Interaction):
        valor = self.values[0]
        if valor == "__vazio__":
            return await interaction.response.send_message("Nenhum inscrito para remover.", ephemeral=True)

        if valor.startswith("fila|||"):
            partes = valor.split("|||")
            classe = partes[1]
            jogador = partes[2]
            if jogador in self.view_pai.fila_espera.get(classe, []):
                self.view_pai.fila_espera[classe].remove(jogador)
                self.view_pai._atualizar_select()
                await interaction.response.edit_message(embed=self.view_pai.gerar_embed(), view=self.view_pai)
                await interaction.followup.send(f"✅ {jogador} removido da fila de **{classe}**.", ephemeral=True)
                await self.view_pai._sync_state(interaction)
            else:
                return await interaction.response.send_message("Jogador não encontrado na fila.", ephemeral=True)
        else:
            partes = valor.split("|||")
            classe = partes[0]
            jogador = partes[1]
            if jogador in self.view_pai.jogadores.get(classe, []):
                self.view_pai.jogadores[classe].remove(jogador)
                self.view_pai._atualizar_select()
                await interaction.response.edit_message(embed=self.view_pai.gerar_embed(), view=self.view_pai)
                await self.view_pai.promover_da_fila(interaction, classe)
                await interaction.followup.send(f"✅ {jogador} removido de **{classe}**.", ephemeral=True)
                await self.view_pai._sync_state(interaction)
            else:
                return await interaction.response.send_message("Jogador não encontrado nessa classe.", ephemeral=True)


class ViewGerenciarVagas(discord.ui.View):
    def __init__(self, painel):
        super().__init__(timeout=120)
        self.add_item(SelectGerenciarVagas(painel))


class ModalPuxarMembro(discord.ui.Modal, title="👑 Puxar Membro para a PT"):
    jogador = discord.ui.TextInput(
        label="Nome, @Nick ou ID numérico",
        style=discord.TextStyle.short,
        placeholder="Ex: @[DH] Zezinho",
        required=True
    )
    classe = discord.ui.TextInput(
        label="Nome da Vaga (Exatamente como no painel)",
        style=discord.TextStyle.short,
        placeholder="Ex: Tank, Suporte, DPS",
        required=True
    )

    def __init__(self, view_pai):
        super().__init__()
        self.view_pai = view_pai

    async def on_submit(self, interaction: discord.Interaction):
        usuario_input = self.jogador.value.strip()
        classe_escolhida = self.classe.value.strip()

        if classe_escolhida not in self.view_pai.max_vagas:
            return await interaction.response.send_message(f"❌ A classe `{classe_escolhida}` não existe.", ephemeral=True)

        usuario_final = None

        if usuario_input.startswith("<@") and usuario_input.endswith(">"):
            usuario_final = usuario_input
        elif usuario_input.isdigit():
            usuario_final = f"<@{usuario_input}>"
        else:
            nome_busca = usuario_input.lstrip('@').strip().lower()
            for membro in interaction.guild.members:
                if membro.display_name.lower() == nome_busca or membro.name.lower() == nome_busca:
                    usuario_final = membro.mention
                    break

            if not usuario_final:
                return await interaction.response.send_message(
                    f"❌ Não encontrei ninguém com o nome `{usuario_input}` no servidor.\n"
                    "*Dica: Digite exatamente igual ao apelido do Discord ou cole o ID numérico.*",
                    ephemeral=True
                )

        await self.view_pai.forcar_insercao(interaction, usuario_final, classe_escolhida)


class PainelVagas(discord.ui.View):
    def __init__(self, conteudo, definicao_vagas, autor_id, unix_timestamp=None, descricao=None, call_id=None, foods=1, message_id=None, jump_url=None):
        super().__init__(timeout=None)
        self.conteudo = conteudo
        self.max_vagas = definicao_vagas
        self.autor_id = autor_id
        self.unix_timestamp = unix_timestamp
        self.descricao = descricao
        self.call_id = call_id
        self.foods = foods
        self.teto_pontos = foods * 10
        self.message_id = message_id
        self.jump_url = jump_url
        self.jogadores = {classe: [] for classe in definicao_vagas}
        self.fila_espera = {classe: [] for classe in definicao_vagas}
        self.status = "formando"

        # Row 0: Select dropdown para classes
        self.select_classes = SelectClasses(self)
        self.add_item(self.select_classes)

        # Row 1: Botões principais
        self.botao_sair = discord.ui.Button(label="Sair da Lista", style=discord.ButtonStyle.danger, emoji="❌", row=1)
        self.botao_sair.callback = self.sair_callback
        self.add_item(self.botao_sair)

        self.botao_encerrar = discord.ui.Button(label="Encerrar PT", style=discord.ButtonStyle.secondary, emoji="🛑", row=1)
        self.botao_encerrar.callback = self.encerrar_callback
        self.add_item(self.botao_encerrar)

        self.botao_editar = discord.ui.Button(label="Editar", style=discord.ButtonStyle.primary, emoji="✏️", row=1)
        self.botao_editar.callback = self.editar_callback
        self.add_item(self.botao_editar)

        # Row 2: Botões de gestão
        self.botao_iniciar = discord.ui.Button(label="Iniciar Conteúdo", style=discord.ButtonStyle.success, emoji="▶️", row=2)
        self.botao_iniciar.callback = self.iniciar_callback
        self.add_item(self.botao_iniciar)

        self.botao_gerenciar = discord.ui.Button(label="Gerenciar Vagas", style=discord.ButtonStyle.secondary, emoji="🛠️", row=2)
        self.botao_gerenciar.callback = self.gerenciar_callback
        self.add_item(self.botao_gerenciar)

        self._aplicar_custom_ids()

    def _definir_status(self, novo_status):
        self.status = novo_status
        if novo_status == "encerrado":
            for item in self.children:
                item.disabled = True
        elif novo_status == "em_andamento":
            self.botao_iniciar.disabled = True

    def _atualizar_select(self):
        novas_opcoes = []
        for classe, vagas_totais in self.max_vagas.items():
            inscritos = self.jogadores.get(classe, [])
            texto = f"{len(inscritos)}/{vagas_totais} inscritos"
            novas_opcoes.append(discord.SelectOption(label=classe[:100], value=classe, description=texto))
        self.select_classes.options = novas_opcoes[:25]

    def _aplicar_custom_ids(self):
        prefix = f"pnl_{self.message_id or 'new'}"
        self.select_classes.custom_id = f"{prefix}_sel"
        self.botao_sair.custom_id = f"{prefix}_sair"
        self.botao_encerrar.custom_id = f"{prefix}_enc"
        self.botao_editar.custom_id = f"{prefix}_edit"
        self.botao_iniciar.custom_id = f"{prefix}_init"
        self.botao_gerenciar.custom_id = f"{prefix}_ger"

    async def _sync_state(self, interaction):
        lfg_cog = interaction.client.get_cog("LFG")
        if lfg_cog:
            for evento in lfg_cog.eventos_ativos:
                if evento.get("jump_url") == self.jump_url:
                    evento["jogadores"] = dict(self.jogadores)
                    evento["fila_espera"] = dict(self.fila_espera)
                    evento["status"] = self.status
                    evento["max_vagas"] = dict(self.max_vagas)
                    if self.call_id:
                        evento["call_id"] = self.call_id
                    break
            await salvar_eventos(lfg_cog.eventos_ativos)

    def gerar_embed(self):
        titulo_destaque = f"💥 {self.conteudo.upper()} 💥"

        if self.status == "encerrado":
            status_texto = "🔴 Conteúdo Encerrado / Call Out"
            cor_embed = discord.Color.dark_gray()
        elif self.status == "em_andamento":
            status_texto = "🔵 Em Andamento"
            if self.unix_timestamp:
                status_texto += f" | ⏱️ **Começa:** <t:{self.unix_timestamp}:R> (<t:{self.unix_timestamp}:f>)"
            cor_embed = discord.Color.blue()
        else:
            status_texto = "🟢 Formando Grupo"
            if self.unix_timestamp:
                status_texto += f" | ⏱️ **Começa:** <t:{self.unix_timestamp}:R> (<t:{self.unix_timestamp}:f>)"
            cor_embed = discord.Color.brand_red()

        desc_embed = f"**Líder da PT:** <@{self.autor_id}>\n**Status:** {status_texto}\n"
        desc_embed += f"🍕 **Foods:** {self.foods}\n"
        if self.call_id:
            desc_embed += f"🔊 **Call:** <#{self.call_id}>\n"
        if self.descricao:
            desc_embed += f"{self.descricao}\n"
        desc_embed += "━━━━━━━━━━━━━━━━━━━━━━\n"

        embed = discord.Embed(
            title=titulo_destaque,
            description=desc_embed,
            color=cor_embed
        )

        campos = 0
        for classe, vagas_totais in self.max_vagas.items():
            if campos >= 25:
                break
            inscritos = self.jogadores[classe]
            reserva = self.fila_espera[classe]
            texto_jogadores = "\n".join(inscritos) if inscritos else "*Vazio*"

            if reserva:
                texto_reserva = "\n".join([f"⏳ *{r} (Fila)*" for r in reserva])
                texto_final = f"{texto_jogadores}\n\n**⏱️ Fila:**\n{texto_reserva}"
            else:
                texto_final = texto_jogadores

            embed.add_field(name=f"🛡️ {classe} ({len(inscritos)}/{vagas_totais})", value=texto_final, inline=True)
            campos += 1

        if self.status == "encerrado":
            embed.set_footer(text="Esta PT foi encerrada pelo líder e não aceita mais inscrições.")
        else:
            embed.set_footer(text="Clique nos botões abaixo para entrar ou sair da fila.")

        return embed

    async def promover_da_fila(self, interaction: discord.Interaction, classe: str):
        if len(self.jogadores[classe]) < self.max_vagas[classe] and len(self.fila_espera[classe]) > 0:
            proximo_jogador = self.fila_espera[classe].pop(0)
            self.jogadores[classe].append(proximo_jogador)
            await interaction.channel.send(f"🎉 {proximo_jogador}, uma vaga abriu e você assumiu como **{classe}**!")

    async def processar_clique(self, interaction: discord.Interaction, classe: str):
        if self.status == "encerrado":
            return await interaction.response.send_message("❌ Esta PT já foi encerrada.", ephemeral=True)

        usuario = interaction.user.mention
        classe_antiga = None

        for c in self.jogadores:
            if usuario in self.jogadores[c]:
                self.jogadores[c].remove(usuario)
                classe_antiga = c
            if usuario in self.fila_espera[c]:
                self.fila_espera[c].remove(usuario)

        if len(self.jogadores[classe]) < self.max_vagas[classe]:
            self.jogadores[classe].append(usuario)
            self._atualizar_select()
            await interaction.response.edit_message(embed=self.gerar_embed(), view=self)
        else:
            if usuario not in self.fila_espera[classe]:
                self.fila_espera[classe].append(usuario)
                self._atualizar_select()
                await interaction.response.edit_message(embed=self.gerar_embed(), view=self)
                await interaction.followup.send(f"📋 Fila de Espera para {classe}!", ephemeral=True)

        if classe_antiga and classe_antiga != classe:
            await self.promover_da_fila(interaction, classe_antiga)
            self._atualizar_select()
            await interaction.message.edit(embed=self.gerar_embed(), view=self)

        await self._sync_state(interaction)

    async def sair_callback(self, interaction: discord.Interaction):
        if self.status == "encerrado":
            return await interaction.response.send_message("❌ Esta PT já foi encerrada.", ephemeral=True)

        usuario = interaction.user.mention
        removido = False
        classe_abandonada = None

        for c in self.jogadores:
            if usuario in self.jogadores[c]:
                self.jogadores[c].remove(usuario)
                removido = True
                classe_abandonada = c
            if usuario in self.fila_espera[c]:
                self.fila_espera[c].remove(usuario)
                removido = True

        if removido:
            self._atualizar_select()
            await interaction.response.edit_message(embed=self.gerar_embed(), view=self)
            if classe_abandonada:
                await self.promover_da_fila(interaction, classe_abandonada)
                self._atualizar_select()
                await interaction.message.edit(embed=self.gerar_embed(), view=self)
            await self._sync_state(interaction)
        else:
            await interaction.response.send_message("Você não está inscrito em nenhuma vaga.", ephemeral=True)

    async def iniciar_callback(self, interaction: discord.Interaction):
        if not _eh_lider_ou_staff(interaction.user, self.autor_id):
            return await interaction.response.send_message("❌ Apenas o líder da PT ou a Staff pode iniciar o conteúdo.", ephemeral=True)

        if self.status != "formando":
            return await interaction.response.send_message("❌ O conteúdo já foi iniciado ou encerrado.", ephemeral=True)

        self._definir_status("em_andamento")

        if not self.jump_url:
            self.jump_url = interaction.message.jump_url

        lfg_cog = interaction.client.get_cog("LFG")
        if lfg_cog:
            for evento in lfg_cog.eventos_ativos:
                if evento.get("jump_url") == interaction.message.jump_url:
                    evento["status"] = "em_andamento"
                    evento["jogadores"] = dict(self.jogadores)
                    evento["fila_espera"] = dict(self.fila_espera)
                    break
            await salvar_eventos(lfg_cog.eventos_ativos)

            if self.call_id:
                canal = interaction.guild.get_channel(self.call_id)
                if canal:
                    for membro in canal.members:
                        if not membro.bot:
                            await lfg_cog.registrar_entrada_call(self.call_id, str(membro.id))

        await interaction.response.edit_message(embed=self.gerar_embed(), view=self)

    async def gerenciar_callback(self, interaction: discord.Interaction):
        if not _eh_lider_ou_staff(interaction.user, self.autor_id):
            return await interaction.response.send_message("❌ Apenas o líder da PT ou a Staff pode gerenciar vagas.", ephemeral=True)

        if self.status == "encerrado":
            return await interaction.response.send_message("❌ Esta PT já foi encerrada.", ephemeral=True)

        await interaction.response.send_message(
            "🛠️ **Gerenciar Vagas** — Selecione um jogador para remover da lista:",
            view=ViewGerenciarVagas(self),
            ephemeral=True
        )

    async def editar_callback(self, interaction: discord.Interaction):
        if not _eh_lider_ou_staff(interaction.user, self.autor_id):
            return await interaction.response.send_message("❌ Apenas o líder da PT ou a staff pode editar.", ephemeral=True)

        vagas_str = "\n".join(f"{c}:{q}" for c, q in self.max_vagas.items())
        modal = ModalEditarConteudo(self, interaction.user)
        modal.titulo.default = self.conteudo[:100]
        modal.descricao_input.default = self.descricao or ""
        modal.vagas_input.default = vagas_str
        await interaction.response.send_modal(modal)

    async def encerrar_callback(self, interaction: discord.Interaction):
        if not _eh_lider_ou_staff(interaction.user, self.autor_id):
            return await interaction.response.send_message("❌ Acesso Negado: Apenas o líder da PT ou a Staff pode fazer o call out!", ephemeral=True)

        lfg_cog = interaction.client.get_cog("LFG")
        resultados = {}
        if lfg_cog:
            resultados = await lfg_cog._encerrar_conteudo(self, interaction.guild, interaction.message.jump_url)

        await interaction.response.edit_message(embed=self.gerar_embed(), view=self)
        await interaction.followup.send(f"🛑 **{interaction.user.display_name}** deu Call Out e encerrou o conteúdo: **{self.conteudo}**!")

        if resultados:
            linhas = []
            for uid, dados in sorted(resultados.items(), key=lambda x: -x[1]["pontos"]):
                linhas.append(f"<@{uid}> — {dados['pontos']} pts ({dados['minutos']} min)")
            embed_pts = discord.Embed(
                title="🏆 Pontos do Conteúdo",
                description="\n".join(linhas),
                color=discord.Color.gold()
            )
            embed_pts.set_footer(text=f"Teto: {self.teto_pontos} pts ({self.foods} foods)")
            await interaction.followup.send(embed=embed_pts)


class ModalEditarConteudo(discord.ui.Modal, title="✏️ Editar Conteúdo"):
    def __init__(self, painel: PainelVagas, usuario: discord.Member):
        super().__init__()
        self.painel = painel
        self.usuario = usuario

        self.titulo = discord.ui.TextInput(
            label="Título do Conteúdo",
            style=discord.TextStyle.short,
            max_length=100,
            required=True,
        )
        self.descricao_input = discord.ui.TextInput(
            label="Descrição (Opcional)",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=False,
        )
        self.vagas_input = discord.ui.TextInput(
            label="Vagas (uma por linha: Classe:Qtd)",
            style=discord.TextStyle.paragraph,
            required=True,
        )
        self.add_item(self.titulo)
        self.add_item(self.descricao_input)
        self.add_item(self.vagas_input)

    async def on_submit(self, interaction: discord.Interaction):
        conteudo = self.titulo.value.strip()
        descricao = self.descricao_input.value.strip() or None

        definicao_vagas = {}
        for linha in self.vagas_input.value.splitlines():
            linha_limpa = linha.strip().rstrip(',').strip()
            if not linha_limpa:
                continue
            if ":" not in linha_limpa:
                return await interaction.response.send_message(
                    f"❌ Formato inválido em `{linha_limpa}`. Use `Classe:Qtd`.", ephemeral=True
                )
            nome_classe, qtd = linha_limpa.split(":", 1)
            qtd = qtd.strip()
            if not qtd.isdigit():
                return await interaction.response.send_message(
                    f"❌ Quantidade inválida em `{linha_limpa}`. Use um número.", ephemeral=True
                )
            definicao_vagas[nome_classe.strip()] = int(qtd)

        if not definicao_vagas:
            return await interaction.response.send_message(
                "❌ Você precisa adicionar pelo menos uma vaga.", ephemeral=True
            )

        antigo_conteudo = self.painel.conteudo
        self.painel.conteudo = conteudo
        self.painel.descricao = descricao
        self.painel.max_vagas = definicao_vagas

        novas_classes = set(definicao_vagas.keys())
        antigas_classes = set(self.painel.jogadores.keys())

        for classe in antigas_classes - novas_classes:
            del self.painel.jogadores[classe]
            del self.painel.fila_espera[classe]
        for classe in novas_classes - antigas_classes:
            self.painel.jogadores[classe] = []
            self.painel.fila_espera[classe] = []

        novas_opcoes = []
        for classe, vagas_totais in definicao_vagas.items():
            inscritos = self.painel.jogadores.get(classe, [])
            texto = f"{len(inscritos)}/{vagas_totais} inscritos"
            novas_opcoes.append(discord.SelectOption(label=classe[:100], value=classe, description=texto))
        self.painel.select_classes.options = novas_opcoes[:25]

        lfg_cog = interaction.client.get_cog("LFG")
        if lfg_cog:
            for evento in lfg_cog.eventos_ativos:
                if evento.get("jump_url") == interaction.message.jump_url:
                    evento["conteudo"] = conteudo
                    evento["descricao"] = descricao
                    evento["max_vagas"] = dict(self.painel.max_vagas)
                    evento["jogadores"] = dict(self.painel.jogadores)
                    evento["fila_espera"] = dict(self.painel.fila_espera)
                    break
            await salvar_eventos(lfg_cog.eventos_ativos)

        if self.painel.call_id:
            try:
                canal = interaction.guild.get_channel(self.painel.call_id)
                if canal:
                    await canal.edit(name=f"🎮 [DH] {conteudo[:50]}")
            except Exception:
                pass

        await interaction.response.edit_message(embed=self.painel.gerar_embed(), view=self.painel)


# ==========================================
# TEMPLATES — SELEÇÃO E CRIAÇÃO
# ==========================================

class ItemSelecionarTemplate(discord.ui.Select):
    """Usado pelo /content: escolhe um template pra criar o painel, ou vai criar do zero."""
    def __init__(self, cog: "LFG"):
        self.cog = cog
        opcoes = [discord.SelectOption(label="🆕 Criar do zero", value="__novo__", emoji="🆕")]
        for nome in cog.templates.keys():
            opcoes.append(discord.SelectOption(label=nome[:100], value=nome))
        super().__init__(placeholder="Escolha um template ou crie do zero...", options=opcoes[:25])

    async def callback(self, interaction: discord.Interaction):
        escolha = self.values[0]
        if escolha == "__novo__":
            await interaction.response.send_modal(ModalCriarConteudo(self.cog))
            return

        template = self.cog.templates.get(escolha)
        if not template:
            return await interaction.response.send_message(
                "❌ Esse template não existe mais (pode ter sido removido).", ephemeral=True
            )

        await interaction.response.send_modal(ModalUsarTemplate(self.cog, escolha, template))


class ViewEscolherTemplate(discord.ui.View):
    def __init__(self, cog: "LFG"):
        super().__init__(timeout=60)
        self.add_item(ItemSelecionarTemplate(cog))


class ItemMenuTemplate(discord.ui.Select):
    """Usado pelo /template: menu de gerenciamento (criar, listar, remover)."""
    def __init__(self, cog: "LFG"):
        self.cog = cog
        opcoes = [
            discord.SelectOption(label="➕ Criar novo template", value="__criar__", emoji="➕"),
            discord.SelectOption(label="📋 Listar todos os templates", value="__listar__", emoji="📋"),
        ]
        for nome in cog.templates.keys():
            opcoes.append(discord.SelectOption(label=f"⚙️ Gerenciar: {nome}"[:100], value=nome))
        super().__init__(placeholder="O que você quer fazer?", options=opcoes[:25])

    async def callback(self, interaction: discord.Interaction):
        escolha = self.values[0]

        if escolha == "__criar__":
            return await interaction.response.send_modal(ModalCriarTemplate(self.cog))

        if escolha == "__listar__":
            embed = self.cog.montar_embed_templates()
            return await interaction.response.edit_message(content=None, embed=embed, view=None)

        template = self.cog.templates.get(escolha)
        if not template:
            return await interaction.response.edit_message(content="❌ Esse template não existe mais.", embed=None, view=None)

        embed = discord.Embed(title=f"🛡️ {escolha}", color=discord.Color.blurple())
        vagas_str = ", ".join(f"{c}:{q}" for c, q in template["vagas"].items())
        embed.add_field(name="Vagas", value=vagas_str, inline=False)
        if template.get("descricao"):
            embed.add_field(name="Descrição", value=template["descricao"], inline=False)

        await interaction.response.edit_message(content=None, embed=embed, view=ViewConfirmarRemocaoTemplate(self.cog, escolha, interaction.user))


class ViewMenuTemplate(discord.ui.View):
    def __init__(self, cog: "LFG"):
        super().__init__(timeout=60)
        self.add_item(ItemMenuTemplate(cog))


class ViewConfirmarRemocaoTemplate(discord.ui.View):
    def __init__(self, cog: "LFG", nome: str, usuario: discord.Member = None):
        super().__init__(timeout=60)
        self.cog = cog
        self.nome = nome
        self.usuario = usuario

    def _pode_editar(self):
        if self.usuario is None:
            return False
        template = self.cog.templates.get(self.nome)
        if not template:
            return False
        criador_id = template.get("criador_id")
        if criador_id and self.usuario.id == criador_id:
            return True
        return _eh_staff(self.usuario)

    @discord.ui.button(label="✏️ Editar Template", style=discord.ButtonStyle.primary)
    async def editar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._pode_editar():
            return await interaction.response.send_message("❌ Você não tem permissão para editar este template.", ephemeral=True)
        template = self.cog.templates.get(self.nome)
        if not template:
            return await interaction.response.edit_message(content="❌ Template não encontrado.", embed=None, view=None)
        await interaction.response.send_modal(ModalEditarTemplate(self.cog, self.nome, template))

    @discord.ui.button(label="🗑️ Remover Template", style=discord.ButtonStyle.danger)
    async def remover(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._pode_editar():
            return await interaction.response.send_message("❌ Você não tem permissão para remover este template.", ephemeral=True)
        if self.nome in self.cog.templates:
            del self.cog.templates[self.nome]
            await salvar_templates(self.cog.templates)
            await interaction.response.edit_message(content=f"🗑️ Template **{self.nome}** removido.", embed=None, view=None)
        else:
            await interaction.response.edit_message(content="❌ Esse template já tinha sido removido.", embed=None, view=None)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Ação cancelada.", embed=None, view=None)


class ModalEditarTemplate(discord.ui.Modal, title="✏️ Editar Template"):
    def __init__(self, cog: "LFG", nome_template: str, template: dict):
        super().__init__()
        self.cog = cog
        self.nome_original = nome_template

        vagas_str = "\n".join(f"{c}:{q}" for c, q in template["vagas"].items())

        self.nome_input = discord.ui.TextInput(
            label="Nome do Template",
            style=discord.TextStyle.short,
            default=nome_template[:50],
            max_length=50,
            required=True,
        )
        self.descricao_input = discord.ui.TextInput(
            label="Descrição (Opcional)",
            style=discord.TextStyle.paragraph,
            default=template.get("descricao") or "",
            max_length=500,
            required=False,
        )
        self.vagas_input = discord.ui.TextInput(
            label="Vagas (uma por linha: Classe:Qtd)",
            style=discord.TextStyle.paragraph,
            default=vagas_str,
            required=True,
        )
        self.add_item(self.nome_input)
        self.add_item(self.descricao_input)
        self.add_item(self.vagas_input)

    async def on_submit(self, interaction: discord.Interaction):
        novo_nome = self.nome_input.value.strip()
        descricao = self.descricao_input.value.strip() or None

        definicao_vagas = {}
        for linha in self.vagas_input.value.splitlines():
            linha_limpa = linha.strip().rstrip(',').strip()
            if not linha_limpa:
                continue
            if ":" not in linha_limpa:
                return await interaction.response.send_message(
                    f"❌ Formato inválido em `{linha_limpa}`. Use `Classe:Qtd`.", ephemeral=True
                )
            nome_classe, qtd = linha_limpa.split(":", 1)
            qtd = qtd.strip()
            if not qtd.isdigit():
                return await interaction.response.send_message(
                    f"❌ Quantidade inválida em `{linha_limpa}`. Use um número.", ephemeral=True
                )
            definicao_vagas[nome_classe.strip()] = int(qtd)

        if not definicao_vagas:
            return await interaction.response.send_message(
                "❌ Você precisa adicionar pelo menos uma vaga.", ephemeral=True
            )

        template_antigo = self.cog.templates.get(self.nome_original)
        criador_id = template_antigo.get("criador_id") if template_antigo else interaction.user.id

        if novo_nome != self.nome_original:
            del self.cog.templates[self.nome_original]

        self.cog.templates[novo_nome] = {
            "vagas": definicao_vagas,
            "descricao": descricao,
            "criador_id": criador_id
        }
        await salvar_templates(self.cog.templates)

        await interaction.response.edit_message(
            content=f"✅ Template **{novo_nome}** atualizado com sucesso!",
            embed=None, view=None
        )


class ModalUsarTemplate(discord.ui.Modal, title="🎮 Criar Conteúdo (Template)"):
    def __init__(self, cog: "LFG", nome_template: str, template: dict):
        super().__init__()
        self.cog = cog
        self.template = template

        self.titulo = discord.ui.TextInput(
            label="Título do Conteúdo",
            style=discord.TextStyle.short,
            default=nome_template[:100],
            max_length=100,
            required=True,
            )
        self.horario_texto = discord.ui.TextInput(
            label="Horário (opcional)",
            style=discord.TextStyle.short,
            placeholder="20:30 | amanha 20:30 | sex 20:30",
            required=False,
        )
        self.foods_texto = discord.ui.TextInput(
            label="Foods (1 food = 30min, teto de pontos)",
            style=discord.TextStyle.short,
            placeholder="Ex: 2 (pontos máx = 20)",
            max_length=3,
            required=True,
        )
        self.add_item(self.titulo)
        self.add_item(self.horario_texto)
        self.add_item(self.foods_texto)

    async def on_submit(self, interaction: discord.Interaction):
        conteudo = self.titulo.value.strip()
        horario_input = self.horario_texto.value.strip()

        if horario_input and not eh_texto_de_horario(horario_input):
            return await interaction.response.send_message(
                "❌ Horário em formato inválido. Use `20:30`, `amanha 20:30`, `sex 20:30` ou `25-12 20:30`.",
                ephemeral=True
            )

        foods_str = self.foods_texto.value.strip()
        if not foods_str.isdigit():
            return await interaction.response.send_message(
                "❌ Foods precisa ser um número (mínimo 1).", ephemeral=True
            )
        foods = int(foods_str)
        if foods < 1 or foods > 20:
            return await interaction.response.send_message(
                "❌ Foods deve ser entre 1 e 20.", ephemeral=True
            )

        await self.cog.publicar_painel(
            interaction,
            conteudo,
            dict(self.template["vagas"]),
            self.template.get("descricao"),
            horario_input,
            foods,
        )


class ModalCriarTemplate(discord.ui.Modal, title="📋 Criar Template"):
    nome_template = discord.ui.TextInput(
        label="Nome do Template",
        style=discord.TextStyle.short,
        placeholder="Ex: Avalon Trio",
        max_length=50,
        required=True,
    )
    descricao_texto = discord.ui.TextInput(
        label="Descrição (Opcional)",
        style=discord.TextStyle.paragraph,
        placeholder="Ex: requisito t8 ou equivalente, foco em pve.",
        max_length=500,
        required=False,
    )
    vagas_texto = discord.ui.TextInput(
        label="Vagas (uma por linha: Classe:Qtd)",
        style=discord.TextStyle.paragraph,
        placeholder="Tank:1\nSuporte:2\nDPS:5",
        required=True,
    )

    def __init__(self, cog: "LFG"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        nome = self.nome_template.value.strip()
        descricao = self.descricao_texto.value.strip() or None

        definicao_vagas = {}
        for linha in self.vagas_texto.value.splitlines():
            linha_limpa = linha.strip().rstrip(',').strip()
            if not linha_limpa:
                continue
            if ":" not in linha_limpa:
                return await interaction.response.send_message(
                    f"❌ Formato inválido em `{linha_limpa}`. Use `Classe:Qtd`, ex: `Tank:2`.",
                    ephemeral=True
                )
            nome_classe, qtd = linha_limpa.split(":", 1)
            qtd = qtd.strip()
            if not qtd.isdigit():
                return await interaction.response.send_message(
                    f"❌ Quantidade inválida em `{linha_limpa}`. Use um número, ex: `Tank:2`.",
                    ephemeral=True
                )
            definicao_vagas[nome_classe.strip()] = int(qtd)

        if not definicao_vagas:
            return await interaction.response.send_message(
                "❌ Você precisa adicionar pelo menos uma vaga (ex: `Tank:2`).", ephemeral=True
            )

        self.cog.templates[nome] = {"vagas": definicao_vagas, "descricao": descricao, "criador_id": interaction.user.id}
        await salvar_templates(self.cog.templates)

        await interaction.response.send_message(f"✅ Template **{nome}** salvo com sucesso! Já aparece no `/content`.", ephemeral=True)


# ==========================================
# MODAL DE CRIAÇÃO (/content — do zero)
# ==========================================

class ModalCriarConteudo(discord.ui.Modal, title="🎮 Criar Conteúdo"):
    titulo = discord.ui.TextInput(
        label="Título do Conteúdo",
        style=discord.TextStyle.short,
        placeholder="Ex: Gank na Red",
        max_length=100,
        required=True,
    )
    descricao_texto = discord.ui.TextInput(
        label="Descrição do Conteúdo (Opcional)",
        style=discord.TextStyle.paragraph,
        placeholder="Ex: requisito t8 ou equivalente, foco em pve nao vamos lutar.",
        max_length=500,
        required=False,
    )
    vagas_texto = discord.ui.TextInput(
        label="Vagas (uma por linha: Classe:Qtd)",
        style=discord.TextStyle.paragraph,
        placeholder="Tank:1\nSuporte:2\nDPS:5",
        required=True,
    )
    horario_texto = discord.ui.TextInput(
        label="Horário (opcional)",
        style=discord.TextStyle.short,
        placeholder="20:30 | amanha 20:30 | sex 20:30",
        required=False,
    )
    foods_texto = discord.ui.TextInput(
        label="Foods (1 food = 30min, teto de pontos)",
        style=discord.TextStyle.short,
        placeholder="Ex: 2 (pontos máx = 20)",
        max_length=3,
        required=True,
    )

    def __init__(self, cog: "LFG"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        conteudo = self.titulo.value.strip()
        descricao = self.descricao_texto.value.strip() or None

        definicao_vagas = {}
        for linha in self.vagas_texto.value.splitlines():
            linha_limpa = linha.strip().rstrip(',').strip()
            if not linha_limpa:
                continue
            if ":" not in linha_limpa:
                return await interaction.response.send_message(
                    f"❌ Formato inválido em `{linha_limpa}`. Use `Classe:Qtd`, ex: `Tank:2`.",
                    ephemeral=True
                )
            nome_classe, qtd = linha_limpa.split(":", 1)
            qtd = qtd.strip()
            if not qtd.isdigit():
                return await interaction.response.send_message(
                    f"❌ Quantidade inválida em `{linha_limpa}`. Use um número, ex: `Tank:2`.",
                    ephemeral=True
                )
            definicao_vagas[nome_classe.strip()] = int(qtd)

        if not definicao_vagas:
            return await interaction.response.send_message(
                "❌ Você precisa adicionar pelo menos uma vaga (ex: `Tank:2`).", ephemeral=True
            )

        horario_input = self.horario_texto.value.strip()
        if horario_input and not eh_texto_de_horario(horario_input):
            return await interaction.response.send_message(
                "❌ Horário em formato inválido. Use `20:30`, `amanha 20:30`, `sex 20:30` ou `25-12 20:30`.",
                ephemeral=True
            )

        foods_str = self.foods_texto.value.strip()
        if not foods_str.isdigit():
            return await interaction.response.send_message(
                "❌ Foods precisa ser um número (mínimo 1).", ephemeral=True
            )
        foods = int(foods_str)
        if foods < 1 or foods > 20:
            return await interaction.response.send_message(
                "❌ Foods deve ser entre 1 e 20.", ephemeral=True
            )

        await self.cog.publicar_painel(interaction, conteudo, definicao_vagas, descricao, horario_input, foods)


# ==========================================
# CLASSE COG (COMANDOS)
# ==========================================

class LFG(commands.Cog):
    TEMPO_TOLERANCIA_VAZIA = 300  # 5 minutos

    def __init__(self, bot):
        self.bot = bot
        self.eventos_ativos = []
        self.templates = {}
        self.paineis_ativos = {}
        self.timers_vazio = {}  # {call_id: asyncio.Task}
        self.presenca_calls = {}  # {call_id: {user_id: {"entrada": datetime, "minutos": 0}}}

    @commands.Cog.listener()
    async def on_ready(self):
        eventos_brutos = await carregar_eventos()
        self.eventos_ativos = [_migrar_evento(e) for e in eventos_brutos]
        self.eventos_ativos = eventos_validos(self.eventos_ativos)
        self.templates = await carregar_templates()
        self.paineis_ativos = {}
        self.presenca_calls = await _carregar_presencas_mongo()

        guilda = self.bot.guilds[0] if self.bot.guilds else None
        eventos_para_remover = []

        for evento in self.eventos_ativos:
            if evento.get("status") == "encerrado":
                continue

            if evento.get("status") == "formando" and evento.get("unix_timestamp"):
                agora_ts = int(datetime.now(FUSO).timestamp())
                if agora_ts > evento["unix_timestamp"] + 3600:
                    await self._auto_expirar(evento, guilda)
                    eventos_para_remover.append(evento)
                    continue

            message_id_str = evento.get("message_id") or _extrair_message_id(evento.get("jump_url"))
            if not message_id_str:
                continue

            max_vagas_raw = evento.get("max_vagas", {})
            max_vagas = {k: int(v) if isinstance(v, str) else v for k, v in max_vagas_raw.items()}
            if not max_vagas:
                continue

            painel = PainelVagas(
                conteudo=evento["conteudo"],
                definicao_vagas=max_vagas,
                autor_id=int(evento["autor_id"]),
                unix_timestamp=evento.get("unix_timestamp"),
                descricao=evento.get("descricao"),
                call_id=evento.get("call_id"),
                foods=evento.get("foods", 1),
                message_id=message_id_str,
                jump_url=evento.get("jump_url"),
            )
            painel.status = evento.get("status", "formando")
            painel.jogadores = evento.get("jogadores", {c: [] for c in max_vagas})
            painel.fila_espera = evento.get("fila_espera", {c: [] for c in max_vagas})
            painel._aplicar_custom_ids()

            if painel.status == "encerrado":
                painel._definir_status("encerrado")

            self.paineis_ativos[int(message_id_str)] = painel
            self.bot.add_view(painel)

            if painel.call_id and painel.status == "em_andamento" and guilda:
                try:
                    canal = guilda.get_channel(painel.call_id)
                    if canal and len(canal.members) == 0:
                        self._iniciar_timer_vazio(painel.call_id)
                    elif not canal:
                        eventos_para_remover.append(evento)
                except Exception:
                    pass

        for evt in eventos_para_remover:
            if evt in self.eventos_ativos:
                self.eventos_ativos.remove(evt)

        await salvar_eventos(self.eventos_ativos)

    def _iniciar_timer_vazio(self, call_id):
        if call_id in self.timers_vazio:
            return
        self.timers_vazio[call_id] = asyncio.create_task(self._auto_encerrar(call_id))

    def _cancelar_timer_vazio(self, call_id):
        task = self.timers_vazio.pop(call_id, None)
        if task and not task.done():
            task.cancel()

    async def registrar_entrada_call(self, call_id, user_id):
        if call_id not in self.presenca_calls:
            self.presenca_calls[call_id] = {}
        dados_existentes = self.presenca_calls[call_id].get(user_id)
        if dados_existentes:
            dados_existentes["entrada"] = datetime.now(timezone.utc)
        else:
            self.presenca_calls[call_id][user_id] = {
                "entrada": datetime.now(timezone.utc),
                "minutos": 0,
            }
        await _salvar_presencas_mongo(call_id, self.presenca_calls[call_id])

    async def registrar_saida_call(self, call_id, user_id):
        dados = self.presenca_calls.get(call_id, {}).get(user_id)
        if not dados or dados["entrada"] is None:
            return
        agora = datetime.now(timezone.utc)
        entrada = dados["entrada"]
        if entrada.tzinfo is None:
            entrada = entrada.replace(tzinfo=timezone.utc)
        minutos = int((agora - entrada).total_seconds() / 60)
        dados["minutos"] += minutos
        dados["entrada"] = None
        await _salvar_presencas_mongo(call_id, self.presenca_calls[call_id])

    def calcular_pontos(self, call_id, painel):
        presencas = self.presenca_calls.pop(call_id, {})
        asyncio.create_task(_remover_presencas_mongo(call_id))
        for user_id, dados in presencas.items():
            entrada = dados["entrada"]
            if entrada is not None:
                agora = datetime.now(timezone.utc)
                if entrada.tzinfo is None:
                    entrada = entrada.replace(tzinfo=timezone.utc)
                dados["minutos"] += int((agora - entrada).total_seconds() / 60)

        teto = painel.teto_pontos
        inscritos = set()
        for lista in painel.jogadores.values():
            for mencao in lista:
                uid = mencao.strip("<@!>")
                if uid.isdigit():
                    inscritos.add(uid)

        resultados = {}
        todos_users = set(inscritos) | set(presencas.keys())
        for user_id in todos_users:
            if user_id not in inscritos:
                continue
            dados = presencas.get(user_id, {"minutos": 0})
            minutos = dados.get("minutos", 0)
            if minutos <= 0:
                continue
            pontos = (minutos // 30) * 10
            if pontos <= 0:
                continue
            pontos = min(pontos, teto)
            resultados[user_id] = {"minutos": minutos, "pontos": pontos}

        return resultados

    async def _encerrar_conteudo(self, painel, guilda, jump_url=None):
        """Método centralizado de encerramento. Usado tanto pelo botão manual quanto pelo auto."""
        print(f"🛑 _encerrar_conteudo chamado: conteudo={painel.conteudo}, call_id={painel.call_id}, jump_url={painel.jump_url}")
        painel._definir_status("encerrado")

        evento = None
        if painel.jump_url:
            evento = next((e for e in self.eventos_ativos
                          if e.get("jump_url") == painel.jump_url
                          and e.get("status", "formando") != "encerrado"), None)
        if not evento and painel.call_id:
            evento = next((e for e in self.eventos_ativos
                          if e.get("call_id") == painel.call_id
                          and e.get("status", "formando") != "encerrado"), None)
        if not evento and jump_url:
            evento = next((e for e in self.eventos_ativos
                          if e.get("jump_url") == jump_url
                          and e.get("status", "formando") != "encerrado"), None)
        if not evento:
            evento = next((e for e in self.eventos_ativos
                          if e.get("conteudo") == painel.conteudo
                          and e.get("autor_id") == painel.autor_id
                          and e.get("status", "formando") != "encerrado"), None)

        print(f"🛑 Evento encontrado: {evento is not None} (status={evento.get('status') if evento else 'N/A'})")

        if evento:
            evento["status"] = "encerrado"

        if painel.call_id:
            self._cancelar_timer_vazio(painel.call_id)

        resultados = {}
        if painel.call_id:
            print(f"🛑 Calculando pontos para call_id={painel.call_id}, presencas={self.presenca_calls.get(str(painel.call_id), self.presenca_calls.get(painel.call_id, {}))}")
            resultados = self.calcular_pontos(painel.call_id, painel)
            print(f"🛑 Resultados: {resultados}")
            if resultados:
                await salvar_pontos(resultados, painel.conteudo)
                print(f"✅ Pontos salvos no MongoDB: {len(resultados)} usuarios")

        if painel.call_id and guilda:
            try:
                canal = await guilda.fetch_channel(painel.call_id)
                await canal.delete()
                print(f"✅ Call {painel.call_id} deletada com sucesso")
            except discord.NotFound:
                print(f"⚠️ Call {painel.call_id} já não existe (deletada por outro meio?)")
            except discord.Forbidden:
                print(f"❌ Sem permissão pra deletar call {painel.call_id}")
            except Exception as e:
                print(f"⚠️ Erro ao deletar call {painel.call_id}: {type(e).__name__}: {e}")

        self.timers_vazio.pop(painel.call_id, None)
        await salvar_eventos(self.eventos_ativos)
        return resultados

    async def _auto_encerrar(self, call_id):
        print(f"🔁 _auto_encerrar iniciado para call_id={call_id} (tipo={type(call_id).__name__})")
        await asyncio.sleep(self.TEMPO_TOLERANCIA_VAZIA)
        print(f"🔁 Timer expirou para call_id={call_id}. Verificando estado...")

        evento = next((e for e in self.eventos_ativos if e.get("call_id") == call_id and e.get("status", "formando") != "encerrado"), None)
        if not evento:
            print(f"⚠️ _auto_encerrar: evento não encontrado para call_id={call_id}. call_ids_ativos={[e.get('call_id') for e in self.eventos_ativos]}")
            self.timers_vazio.pop(call_id, None)
            return

        guilda = self.bot.guilds[0] if self.bot.guilds else None
        if not guilda:
            print("⚠️ _auto_encerrar: nenhuma guilda encontrada")
            self.timers_vazio.pop(call_id, None)
            return

        canal = guilda.get_channel(call_id)
        if canal:
            membros = len(canal.members)
            print(f"🔊 _auto_encerrar: call {call_id} tem {membros} membro(s)")
            if membros > 0:
                self.timers_vazio.pop(call_id, None)
                return
        else:
            print(f"⚠️ _auto_encerrar: canal {call_id} não encontrado (pode ter sido deletado)")

        painel = None
        msg_id = None
        for mid, p in self.paineis_ativos.items():
            if p.call_id == call_id and p.status != "encerrado":
                painel = p
                msg_id = mid
                break
        if not painel:
            print(f"⚠️ _auto_encerrar: painel não encontrado para call_id={call_id}. paineis_keys={list(self.paineis_ativos.keys())}, paineis_call_ids={[(p.call_id, type(p.call_id).__name__) for p in self.paineis_ativos.values()]}")
            self.timers_vazio.pop(call_id, None)
            return

        print(f"✅ _auto_encerrando: call_id={call_id}, painel.msg_id={msg_id}, conteudo={painel.conteudo}")
        resultados = await self._encerrar_conteudo(painel, guilda)

        if msg_id:
            encontrou = False
            for ch in guilda.text_channels:
                try:
                    msg = await ch.fetch_message(msg_id)
                    await msg.edit(embed=painel.gerar_embed(), view=painel)
                    await ch.send(f"⏳ Call ficou vazia por {self.TEMPO_TOLERANCIA_VAZIA // 60} min. Conteúdo **{painel.conteudo}** encerrado automaticamente.")
                    if resultados:
                        linhas = []
                        for uid, dados in sorted(resultados.items(), key=lambda x: -x[1]["pontos"]):
                            linhas.append(f"<@{uid}> — {dados['pontos']} pts ({dados['minutos']} min)")
                        embed_pts = discord.Embed(
                            title="🏆 Pontos do Conteúdo",
                            description="\n".join(linhas),
                            color=discord.Color.gold()
                        )
                        embed_pts.set_footer(text=f"Teto: {painel.teto_pontos} pts ({painel.foods} foods)")
                        await ch.send(embed=embed_pts)
                    encontrou = True
                    break
                except Exception:
                    continue
            if not encontrou:
                print(f"⚠️ _auto_encerrar: mensagem {msg_id} não encontrada em nenhum canal de texto")
        autor = guilda.get_member(painel.autor_id)
        if autor:
            try:
                await autor.send(
                    f"⏳ Sua PT **{painel.conteudo}** foi encerrada automaticamente por call vazia "
                    f"(inatividade de {self.TEMPO_TOLERANCIA_VAZIA // 60} min)."
                )
            except Exception:
                pass

    def montar_embed_templates(self):
        embed = discord.Embed(title="📋 Templates Salvos — Die Hard", color=discord.Color.blurple())
        if not self.templates:
            embed.description = "Nenhum template salvo ainda."
            return embed
        for nome, dados in self.templates.items():
            vagas_str = ", ".join(f"{c}:{q}" for c, q in dados["vagas"].items())
            valor = vagas_str
            if dados.get("descricao"):
                valor += f"\n*{dados['descricao']}*"
            embed.add_field(name=f"🛡️ {nome}", value=valor, inline=False)
        return embed

    async def _auto_expirar(self, evento, guilda):
        message_id_str = evento.get("message_id") or _extrair_message_id(evento.get("jump_url"))
        if message_id_str and guilda:
            for ch in guilda.text_channels:
                try:
                    msg = await ch.fetch_message(int(message_id_str))
                    if msg.embeds:
                        embed_novo = msg.embeds[0].copy()
                        embed_novo.color = discord.Color.dark_gray()
                        embed_novo.set_footer(text="❌ Este conteúdo expirou (não foi iniciado a tempo).")
                        await msg.edit(embed=embed_novo, view=None)
                    break
                except Exception:
                    continue
        if guilda:
            autor = guilda.get_member(int(evento["autor_id"]))
            if autor:
                try:
                    await autor.send(
                        f"❌ Sua PT **{evento['conteudo']}** foi automaticamente expirada "
                        f"(passou mais de 1h do horário agendado sem ser iniciada)."
                    )
                except Exception:
                    pass

    async def _criar_call_conteudo(self, guilda, autor, conteudo):
        """Cria a voice channel de conteúdo. Retorna call_id ou None."""
        try:
            print(f"🎮 Tentando criar call: guild={guilda.name}, author={autor.display_name}")
            categoria = None
            canal_gerador = None
            for canal_id in CANAIS_GERADORES_IDS:
                c = guilda.get_channel(canal_id)
                if c and c.category:
                    canal_gerador = c
                    categoria = c.category
                    break

            permissoes = dict(canal_gerador.overwrites) if canal_gerador else {
                guilda.default_role: discord.PermissionOverwrite(view_channel=False),
            }
            permissoes[autor] = discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)

            call = await guilda.create_voice_channel(
                name=f"🎮 [DH] {conteudo[:50]}",
                category=categoria,
                overwrites=permissoes,
            )
            print(f"✅ Call criada com sucesso! ID: {call.id}")
            return call.id
        except Exception as e:
            print(f"⚠️ Erro ao criar call de conteúdo: {type(e).__name__}: {e}")
            return None

    async def _agendar_criacao_call(self, guilda, autor_id, conteudo, mensagem_id, jump_url, unix_timestamp):
        """Aguarda até 30 min antes do horário e cria a call + lembrete."""
        from datetime import datetime, timezone
        agora = datetime.now(timezone.utc)
        momento_call = unix_timestamp - (30 * 60)
        segundos_ate = momento_call - int(agora.timestamp())

        if segundos_ate <= 0:
            segundos_ate = 1

        print(f"⏰ Call agendada para {conteudo} em {segundos_ate}s ({segundos_ate // 60} min)")
        await asyncio.sleep(segundos_ate)

        for evento in self.eventos_ativos:
            if evento.get("jump_url") == jump_url and evento.get("status") != "encerrado":
                autor = guilda.get_member(autor_id)
                if not autor:
                    return

                call_id = await self._criar_call_conteudo(guilda, autor, conteudo)
                evento["call_id"] = call_id
                await salvar_eventos(self.eventos_ativos)

                if call_id:
                    try:
                        canal_msg = guilda.get_channel(int(jump_url.split("/")[-2]))
                        if canal_msg:
                            msg = await canal_msg.fetch_message(int(jump_url.split("/")[-1]))
                            if msg and msg.embeds:
                                embed_antigo = msg.embeds[0]
                                desc = embed_antigo.description
                                if "🔊 **Call:**" not in desc:
                                    desc = desc.replace("━━━━━━━━━━━━━━━━━━━━━━", f"🔊 **Call:** <#{call_id}>\n━━━━━━━━━━━━━━━━━━━━━━")
                                embed_novo = embed_antigo.copy()
                                embed_novo.description = desc
                                await msg.edit(embed=embed_novo)
                                painel = self.paineis_ativos.get(msg.id)
                                if painel:
                                    painel.call_id = call_id
                                    if not painel.jump_url:
                                        painel.jump_url = jump_url
                                    inscritos = set()
                                    for lista in painel.jogadores.values():
                                        for mencao in lista:
                                            uid = mencao.strip("<@!>")
                                            if uid.isdigit():
                                                inscritos.add(int(uid))
                                    inscritos.add(autor_id)
                                    for uid in inscritos:
                                        membro = guilda.get_member(uid)
                                        if membro:
                                            try:
                                                await membro.send(
                                                    f"🎮 **Call pronta!** A call do conteúdo **{conteudo}** foi criada!\n"
                                                    f"Entre aqui: <#{call_id}>"
                                                )
                                            except Exception:
                                                pass
                    except Exception:
                        pass

                    agora_ts = int(datetime.now(timezone.utc).timestamp())
                    segundos_ate_hora = unix_timestamp - agora_ts
                    if segundos_ate_hora > 0:
                        print(f"⏰ Timer de call vazia para {conteudo} adiado para o horário agendado (daqui {segundos_ate_hora}s)")
                        await asyncio.sleep(segundos_ate_hora)
                        canal = guilda.get_channel(call_id)
                        if canal and len(canal.members) == 0:
                            print(f"⏰ Call {call_id} ainda vazia no horário agendado, iniciando timer de auto-encerramento")
                            self._iniciar_timer_vazio(call_id)
                    else:
                        self._iniciar_timer_vazio(call_id)
                break

    async def publicar_painel(self, interaction: discord.Interaction, conteudo, definicao_vagas, descricao, horario_input, foods=1):
        """Cria e posta o painel de vagas — usado tanto pelo fluxo 'do zero' quanto por template."""
        unix_timestamp = interpretar_horario(horario_input) if horario_input else None

        call_id = None
        if unix_timestamp:
            from datetime import datetime, timezone
            agora_ts = int(datetime.now(timezone.utc).timestamp())
            segundos_ate = unix_timestamp - agora_ts
            if segundos_ate > 30 * 60:
                call_id = None
            else:
                call_id = await self._criar_call_conteudo(interaction.guild, interaction.user, conteudo)
        else:
            call_id = await self._criar_call_conteudo(interaction.guild, interaction.user, conteudo)

        if call_id:
            self._iniciar_timer_vazio(call_id)

        painel = PainelVagas(conteudo, definicao_vagas, interaction.user.id, unix_timestamp, descricao, call_id, foods)
        embed_inicial = painel.gerar_embed()

        id_cargo_membro = CARGOS.get("DIE HARD")
        mencao_cargo = f"<@&{id_cargo_membro}>" if id_cargo_membro else "@everyone"

        await interaction.response.send_message(content=f"📢 {mencao_cargo}", embed=embed_inicial, view=painel)
        mensagem_painel = await interaction.original_response()

        painel.message_id = str(mensagem_painel.id)
        painel.jump_url = mensagem_painel.jump_url
        painel._aplicar_custom_ids()
        try:
            await mensagem_painel.edit(view=painel)
        except Exception:
            pass

        self.paineis_ativos[mensagem_painel.id] = painel

        self.eventos_ativos.append({
            "conteudo": conteudo,
            "autor_id": interaction.user.id,
            "unix_timestamp": unix_timestamp,
            "jump_url": mensagem_painel.jump_url,
            "message_id": str(mensagem_painel.id),
            "status": "formando",
            "call_id": call_id,
            "foods": foods,
            "descricao": descricao,
            "max_vagas": definicao_vagas,
            "jogadores": {classe: [] for classe in definicao_vagas},
            "fila_espera": {classe: [] for classe in definicao_vagas},
        })
        await salvar_eventos(self.eventos_ativos)

        if unix_timestamp and not call_id:
            asyncio.create_task(self._agendar_criacao_call(
                interaction.guild, interaction.user.id, conteudo,
                mensagem_painel.id, mensagem_painel.jump_url, unix_timestamp
            ))

    @app_commands.command(name="content", description="Criar um painel de vagas para organizar conteúdo em grupo")
    async def content(self, interaction: discord.Interaction):
        if self.templates:
            await interaction.response.send_message(
                "Escolha um template salvo ou crie do zero:",
                view=ViewEscolherTemplate(self),
                ephemeral=True,
            )
        else:
            await interaction.response.send_modal(ModalCriarConteudo(self))

    @app_commands.command(name="template", description="Gerenciar templates de conteúdo (criar, listar ou remover)")
    async def template(self, interaction: discord.Interaction):
        if not self.templates:
            return await interaction.response.send_modal(ModalCriarTemplate(self))

        await interaction.response.send_message(
            "O que você quer fazer?", view=ViewMenuTemplate(self), ephemeral=True
        )

    @commands.command(name="agenda")
    async def agenda(self, ctx):
        self.eventos_ativos = [_migrar_evento(e) for e in await carregar_eventos()]
        self.eventos_ativos = eventos_validos(self.eventos_ativos)
        await salvar_eventos(self.eventos_ativos)

        if not self.eventos_ativos:
            return await ctx.send("📭 Nenhum Ping agendada no momento. Crie um com `/content`.", delete_after=30)

        eventos_ordenados = sorted(
            self.eventos_ativos,
            key=lambda e: (e.get("unix_timestamp") is None, e.get("unix_timestamp") or 0)
        )

        embed = discord.Embed(
            title="📅 Agenda de Pings — Die Hard",
            description="Confira abaixo todos os pings agendadas no servidor:",
            color=discord.Color.gold()
        )

        for evento in eventos_ordenados:
            status_evt = evento.get("status", "formando")
            if status_evt == "em_andamento":
                indicador = "🔵"
                texto_status = "Em andamento"
            elif evento.get("unix_timestamp"):
                indicador = "🟡"
                texto_status = "Agendado"
            else:
                indicador = "🟢"
                texto_status = "Formando"

            if evento.get("unix_timestamp"):
                quando = f"<t:{evento['unix_timestamp']}:R> — <t:{evento['unix_timestamp']}:f>"
            else:
                quando = "Sem horário definido (imediata)"

            embed.add_field(
                name=f"{indicador} {evento['conteudo']}",
                value=f"**Líder:** <@{evento['autor_id']}>\n**Quando:** {quando}\n**Status:** {texto_status}\n[Ir para a PT]({evento['jump_url']})",
                inline=False
            )

        await ctx.send(embed=embed)


# Função para inicializar e plugar essa engrenagem no main.py
async def setup(bot):
    await bot.add_cog(LFG(bot))
