import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone

from config import CARGOS, MONGO_URI, colecao_membros, colecao_recruitment_points, CARGOS_PERMITIDOS_REGISTRAR


def _usando_mongo():
    return MONGO_URI is not None and colecao_membros is not None


def _eh_staff(usuario):
    if usuario.guild_permissions.administrator:
        return True
    ids = [CARGOS.get(nome) for nome in CARGOS_PERMITIDOS_REGISTRAR if CARGOS.get(nome)]
    return any(c.id in ids for c in usuario.roles)


class RegistrarCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="registrar", description="Registra um membro com o nick do jogo (somente staff)")
    @app_commands.describe(
        membro="Membro do Discord a ser registrado",
        nick="Nick do personagem no Albion Online"
    )
    async def registrar(self, interaction: discord.Interaction, membro: discord.Member, nick: str):
        if not _eh_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ Apenas recrutadores e staff podem usar este comando.",
                ephemeral=True,
            )

        nick = nick.strip()
        if not nick:
            return await interaction.response.send_message(
                "❌ Informe o nick do jogo.", ephemeral=True,
            )

        if not _usando_mongo():
            return await interaction.response.send_message(
                "❌ Sistema de registro indisponível (MongoDB desconectado).", ephemeral=True,
            )

        await interaction.response.defer()

        guild = interaction.guild
        cargo_dh = guild.get_role(CARGOS.get("DIE HARD"))
        cargo_recem = guild.get_role(CARGOS.get("recém chegado"))

        novo_nick = f"[DH] {nick}"
        erros = []

        try:
            await membro.edit(nick=novo_nick[:32])
        except discord.Forbidden:
            erros.append("apelido (hierarquia)")
        except Exception:
            erros.append("apelido")

        if cargo_dh:
            try:
                await membro.add_roles(cargo_dh)
            except discord.Forbidden:
                erros.append("cargo DIE HARD (hierarquia)")
            except Exception:
                erros.append("cargo DIE HARD")

        if cargo_recem and cargo_recem in membro.roles:
            try:
                await membro.remove_roles(cargo_recem)
            except discord.Forbidden:
                erros.append("remover recém chegado (hierarquia)")
            except Exception:
                erros.append("remover recém chegado")

        await colecao_membros.update_one(
            {"_id": str(membro.id)},
            {"$set": {
                "nick": nick,
                "registrado_por": str(interaction.user.id),
                "data": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

        await colecao_recruitment_points.update_one(
            {"_id": str(interaction.user.id)},
            {"$inc": {"pontos": 1}},
            upsert=True,
        )

        linhas = [
            f"✅ **{nick}** registrado para {membro.mention}!",
            f"👤 Apelido: `{novo_nick[:32]}`",
            f"🛡️ Cargo DIE HARD atribuído.",
        ]
        if erros:
            linhas.append(f"\n⚠️ Alguns passos falharam: {', '.join(erros)}")
            linhas.append("Verifique a hierarquia de cargos do bot.")

        await interaction.followup.send("\n".join(linhas))


async def setup(bot):
    await bot.add_cog(RegistrarCog(bot))
