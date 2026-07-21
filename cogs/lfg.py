import discord
import re
import json
import os
import asyncio
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta, timezone

from config import CARGOS, MONGO_URI, colecao_templates, colecao_eventos, CANAIS_GERADORES_IDS, colecao_pontos
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
        if (e["unix_timestamp"] is None or e["unix_timestamp"] > agora_ts - 3600)
        and not e.get("encerrado", False)  # Filtra para não listar os encerrados
    ]


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
    def __init__(self, conteudo, definicao_vagas, autor_id, unix_timestamp=None, descricao=None, call_id=None, foods=1):
        super().__init__(timeout=None)
        self.conteudo = conteudo
        self.max_vagas = definicao_vagas
        self.autor_id = autor_id
        self.unix_timestamp = unix_timestamp
        self.descricao = descricao
        self.call_id = call_id
        self.foods = foods
        self.teto_pontos = foods * 10
        self.jogadores = {classe: [] for classe in definicao_vagas}
        self.fila_espera = {classe: [] for classe in definicao_vagas}
        self.encerrado = False

        # Select dropdown para classes (suporta até 25)
        self.select_classes = SelectClasses(self)
        self.add_item(self.select_classes)

        # Botão para Sair da Lista
        botao_sair = discord.ui.Button(label="Sair da Lista", style=discord.ButtonStyle.danger, emoji="❌", row=1)
        botao_sair.callback = self.sair_callback
        self.add_item(botao_sair)

        # Botão de Encerrar Conteúdo (Call Out)
        botao_encerrar = discord.ui.Button(label="Encerrar PT", style=discord.ButtonStyle.secondary, emoji="🛑", row=1)
        botao_encerrar.callback = self.encerrar_callback
        self.add_item(botao_encerrar)

        # Botão de Editar Conteúdo
        botao_editar = discord.ui.Button(label="Editar", style=discord.ButtonStyle.primary, emoji="✏️", row=1)
        botao_editar.callback = self.editar_callback
        self.add_item(botao_editar)

    def _atualizar_select(self):
        novas_opcoes = []
        for classe, vagas_totais in self.max_vagas.items():
            inscritos = self.jogadores.get(classe, [])
            texto = f"{len(inscritos)}/{vagas_totais} inscritos"
            novas_opcoes.append(discord.SelectOption(label=classe[:100], value=classe, description=texto))
        self.select_classes.options = novas_opcoes[:25]

    def gerar_embed(self):
        titulo_destaque = f"💥 {self.conteudo.upper()} 💥"

        if self.encerrado:
            status_texto = "🔴 Conteúdo Encerrado / Call Out"
            cor_embed = discord.Color.dark_gray()
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

        if self.encerrado:
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
        if self.encerrado:
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

    async def sair_callback(self, interaction: discord.Interaction):
        if self.encerrado:
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
        else:
            await interaction.response.send_message("Você não está inscrito em nenhuma vaga.", ephemeral=True)

    async def editar_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.autor_id and not interaction.user.guild_permissions.administrator:
            ids_staff = [CARGOS.get(n) for n in ["lider", "SUB-LIDER", "moderador"] if CARGOS.get(n)]
            if not any(c.id in ids_staff for c in interaction.user.roles):
                return await interaction.response.send_message("❌ Apenas o líder da PT ou a staff pode editar.", ephemeral=True)

        vagas_str = "\n".join(f"{c}:{q}" for c, q in self.max_vagas.items())
        modal = ModalEditarConteudo(self, interaction.user)
        modal.titulo.default = self.conteudo[:100]
        modal.descricao_input.default = self.descricao or ""
        modal.vagas_input.default = vagas_str
        await interaction.response.send_modal(modal)

    async def encerrar_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.autor_id and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Acesso Negado: Apenas o líder da PT ou a Staff pode fazer o call out!", ephemeral=True)

        self.encerrer_painel()

        call_id = None
        eventos_atuais = await carregar_eventos()
        for evento in eventos_atuais:
            if evento.get("jump_url") == interaction.message.jump_url:
                evento["encerrado"] = True
                call_id = evento.get("call_id")
                break
        await salvar_eventos(eventos_atuais)

        if call_id:
            lfg_cog = interaction.client.get_cog("LFG")
            if lfg_cog:
                lfg_cog._cancelar_timer_vazio(call_id)
                resultados = lfg_cog.calcular_pontos(call_id, self)
                if resultados:
                    await salvar_pontos(resultados, self.conteudo)
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

        if call_id:
            await interaction.response.edit_message(embed=self.gerar_embed(), view=self)
            await interaction.followup.send(f"🛑 **{interaction.user.display_name}** deu Call Out e encerrou o conteúdo: **{self.conteudo}**!")

            try:
                canal = interaction.guild.get_channel(call_id)
                if canal:
                    await canal.delete()
            except Exception:
                pass
        else:
            await interaction.response.edit_message(embed=self.gerar_embed(), view=self)
            await interaction.followup.send(f"🛑 **{interaction.user.display_name}** deu Call Out e encerrou o conteúdo: **{self.conteudo}**!")

    def encerrer_painel(self):
        self.encerrado = True
        for item in self.children:
            item.disabled = True


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

        eventos_atuais = await carregar_eventos()
        for evento in eventos_atuais:
            if evento.get("jump_url") == interaction.message.jump_url:
                evento["conteudo"] = conteudo
                break
        await salvar_eventos(eventos_atuais)

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
        if self.usuario.guild_permissions.administrator:
            return True
        ids_staff = [CARGOS.get(n) for n in ["lider", "SUB-LIDER", "moderador"] if CARGOS.get(n)]
        return any(c.id in ids_staff for c in self.usuario.roles)

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
        self.eventos_ativos = eventos_validos(await carregar_eventos())
        await salvar_eventos(self.eventos_ativos)
        self.templates = await carregar_templates()

    def _iniciar_timer_vazio(self, call_id):
        if call_id in self.timers_vazio:
            return
        self.timers_vazio[call_id] = asyncio.create_task(self._auto_encerrar(call_id))

    def _cancelar_timer_vazio(self, call_id):
        task = self.timers_vazio.pop(call_id, None)
        if task and not task.done():
            task.cancel()

    def registrar_entrada_call(self, call_id, user_id):
        if call_id not in self.presenca_calls:
            self.presenca_calls[call_id] = {}
        self.presenca_calls[call_id][user_id] = {
            "entrada": datetime.now(timezone.utc),
            "minutos": 0,
        }

    def registrar_saida_call(self, call_id, user_id):
        dados = self.presenca_calls.get(call_id, {}).get(user_id)
        if not dados:
            return
        agora = datetime.now(timezone.utc)
        entrada = dados["entrada"]
        if entrada.tzinfo is None:
            entrada = entrada.replace(tzinfo=timezone.utc)
        minutos = int((agora - entrada).total_seconds() / 60)
        dados["minutos"] += minutos
        del self.presenca_calls[call_id][user_id]

    def calcular_pontos(self, call_id, painel):
        presencas = self.presenca_calls.pop(call_id, {})
        for user_id, dados in presencas.items():
            agora = datetime.now(timezone.utc)
            entrada = dados["entrada"]
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

    async def _auto_encerrar(self, call_id):
        await asyncio.sleep(self.TEMPO_TOLERANCIA_VAZIA)
        evento = next((e for e in self.eventos_ativos if e.get("call_id") == call_id and not e.get("encerrado")), None)
        if not evento:
            self.timers_vazio.pop(call_id, None)
            return
        guilda = self.bot.guilds[0] if self.bot.guilds else None
        if not guilda:
            self.timers_vazio.pop(call_id, None)
            return
        canal = guilda.get_channel(call_id)
        if canal and len(canal.members) > 0:
            self.timers_vazio.pop(call_id, None)
            return
        painel = None
        msg_id = None
        for mid, p in self.paineis_ativos.items():
            if p.call_id == call_id and not p.encerrado:
                painel = p
                msg_id = mid
                break
        if not painel:
            self.timers_vazio.pop(call_id, None)
            return
        painel.encerrer_painel()
        evento["encerrado"] = True
        await salvar_eventos(self.eventos_ativos)
        resultados = self.calcular_pontos(call_id, painel)
        if resultados:
            await salvar_pontos(resultados, painel.conteudo)
        autor = guilda.get_member(painel.autor_id)
        if canal:
            try:
                await canal.delete()
            except Exception:
                pass
        self.timers_vazio.pop(call_id, None)
        if msg_id:
            for ch in guilda.text_channels:
                try:
                    msg = await ch.fetch_message(msg_id)
                    await msg.edit(embed=painel.gerar_embed())
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
                    break
                except Exception:
                    continue
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

    async def _criar_call_conteudo(self, guilda, autor, conteudo):
        """Cria a voice channel de conteúdo. Retorna call_id ou None."""
        try:
            print(f"🎮 Tentando criar call: guild={guilda.name}, author={autor.display_name}")
            categoria = None
            for canal_id in CANAIS_GERADORES_IDS:
                canal_gerador = guilda.get_channel(canal_id)
                if canal_gerador and canal_gerador.category:
                    categoria = canal_gerador.category
                    break

            permissoes = {
                guilda.default_role: discord.PermissionOverwrite(view_channel=True, connect=True),
                autor: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True),
            }

            for nome, id_cargo in CARGOS.items():
                cargo_obj = guilda.get_role(id_cargo)
                if cargo_obj:
                    permissoes[cargo_obj] = discord.PermissionOverwrite(view_channel=True, connect=True)

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
            if evento.get("jump_url") == jump_url and not evento.get("encerrado"):
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

        painel = PainelVagas(conteudo, definicao_vagas, interaction.user.id, unix_timestamp, descricao, call_id, foods)
        embed_inicial = painel.gerar_embed()

        id_cargo_membro = CARGOS.get("DIE HARD")
        mencao_cargo = f"<@&{id_cargo_membro}>" if id_cargo_membro else "@everyone"

        await interaction.response.send_message(content=f"📢 {mencao_cargo}", embed=embed_inicial, view=painel)
        mensagem_painel = await interaction.original_response()
        self.paineis_ativos[mensagem_painel.id] = painel

        self.eventos_ativos.append({
            "conteudo": conteudo,
            "autor_id": interaction.user.id,
            "unix_timestamp": unix_timestamp,
            "jump_url": mensagem_painel.jump_url,
            "encerrado": False,
            "call_id": call_id,
            "foods": foods,
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
        self.eventos_ativos = eventos_validos(await carregar_eventos())
        await salvar_eventos(self.eventos_ativos)

        if not self.eventos_ativos:
            return await ctx.send("📭 Nenhum Ping agendada no momento. Crie um com `/content`.", delete_after=30)

        eventos_ordenados = sorted(
            self.eventos_ativos,
            key=lambda e: (e["unix_timestamp"] is None, e["unix_timestamp"] or 0)
        )

        embed = discord.Embed(
            title="📅 Agenda de Pings — Die Hard",
            description="Confira abaixo todos os pings agendadas no servidor:",
            color=discord.Color.gold()
        )

        for evento in eventos_ordenados:
            if evento["unix_timestamp"]:
                quando = f"<t:{evento['unix_timestamp']}:R> — <t:{evento['unix_timestamp']}:f>"
            else:
                quando = "🟢 Sem horário definido (imediata)"

            embed.add_field(
                name=f"💥 {evento['conteudo']}",
                value=f"**Líder:** <@{evento['autor_id']}>\n**Quando:** {quando}\n[Ir para a PT]({evento['jump_url']})",
                inline=False
            )

        await ctx.send(embed=embed)


# Função para inicializar e plugar essa engrenagem no main.py
async def setup(bot):
    await bot.add_cog(LFG(bot))
