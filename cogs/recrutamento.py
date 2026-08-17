import discord
from discord.ext import commands

from config import MONGO_URI, colecao_recruitment_points, CARGOS, CARGOS_PERMITIDOS_REGISTRAR


def _usando_mongo():
    return MONGO_URI is not None and colecao_recruitment_points is not None


def _eh_staff(usuario):
    if usuario.guild_permissions.administrator:
        return True
    ids = [CARGOS.get(nome) for nome in CARGOS_PERMITIDOS_REGISTRAR if CARGOS.get(nome)]
    return any(c.id in ids for c in usuario.roles)


class Recrutamento(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="pontosrecrutamento")
    async def pontos_recrutamento(self, ctx, membro: discord.Member = None):
        """Mostra os pontos de recrutamento. Uso: !pontosrecrutamento ou !pontosrecrutamento @membro"""
        if not _usando_mongo():
            return await ctx.send("❌ Sistema de pontos de recrutamento indisponível (MongoDB desconectado).")

        if membro is not None and membro != ctx.author:
            if not _eh_staff(ctx.author):
                return await ctx.send("❌ Apenas staff pode consultar os pontos de outros membros.")
            alvo = membro
        else:
            alvo = ctx.author

        doc = await colecao_recruitment_points.find_one({"_id": str(alvo.id)})
        pontos = doc.get("pontos", 0) if doc else 0

        embed = discord.Embed(
            title=f"📋 Pontos de Recrutamento — {alvo.display_name}",
            description=f"**Total:** {pontos} registro(s)",
            color=discord.Color.green(),
        )
        embed.set_thumbnail(url=alvo.display_avatar.url)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Recrutamento(bot))
