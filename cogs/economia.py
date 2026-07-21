import discord
from datetime import datetime, timezone
from discord.ext import commands
from config import MONGO_URI, colecao_pontos


def _usando_mongo():
    return MONGO_URI is not None and colecao_pontos is not None


async def salvar_pontos(resultados, conteudo):
    if not _usando_mongo():
        return
    for user_id, dados in resultados.items():
        doc = await colecao_pontos.find_one({"_id": user_id})
        total = doc.get("total", 0) + dados["pontos"] if doc else dados["pontos"]
        historico = doc.get("historico", []) if doc else []
        historico.append({
            "conteudo": conteudo,
            "minutos": dados["minutos"],
            "pontos": dados["pontos"],
            "data": datetime.now(timezone.utc).replace(tzinfo=None),
        })
        await colecao_pontos.update_one(
            {"_id": user_id},
            {"$set": {"total": total, "historico": historico}},
            upsert=True
        )


class Economia(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="pontos")
    async def pontos(self, ctx, membro: discord.Member = None):
        if not _usando_mongo():
            return await ctx.send("❌ Sistema de pontos requer MongoDB.")

        if membro is None:
            membro = ctx.author

        doc = await colecao_pontos.find_one({"_id": str(membro.id)})
        if not doc:
            return await ctx.send(f"❌ {membro.mention} não tem pontos registrados.")

        total = doc.get("total", 0)
        historico = doc.get("historico", [])
        ultimos = historico[-5:] if historico else []

        embed = discord.Embed(
            title=f"🏆 Pontos — {membro.display_name}",
            description=f"**Total:** {total} pts",
            color=discord.Color.gold()
        )

        if ultimos:
            linhas = []
            for h in reversed(ultimos):
                data = h.get("data")
                if isinstance(data, datetime):
                    data_str = data.strftime("%d/%m/%y")
                else:
                    data_str = "?"
                linhas.append(f"• {h['conteudo']} — {h['pontos']} pts ({h['minutos']} min) [{data_str}]")
            embed.add_field(name="Últimos conteúdos", value="\n".join(linhas), inline=False)

        embed.set_footer(text=f"Histórico: {len(historico)} conteúdos participados")
        await ctx.send(embed=embed)

    @commands.command(name="ranking")
    async def ranking(self, ctx):
        if not _usando_mongo():
            return await ctx.send("❌ Sistema de pontos requer MongoDB.")

        ranking_docs = []
        async for doc in colecao_pontos.find({}):
            ranking_docs.append(doc)

        ranking_docs.sort(key=lambda x: x.get("total", 0), reverse=True)
        top15 = ranking_docs[:15]

        if not top15:
            return await ctx.send("❌ Nenhum ponto registrado ainda.")

        medalhas = ["🥇", "🥈", "🥉"]
        linhas = []
        for i, doc in enumerate(top15):
            membro = ctx.guild.get_member(int(doc["_id"]))
            nome = membro.display_name if membro else doc["_id"]
            medalha = medalhas[i] if i < 3 else f"**{i+1}.**"
            linhas.append(f"{medalha} {nome} — {doc.get('total', 0)} pts")

        embed = discord.Embed(
            title="🏆 Ranking — Die Hard",
            description="\n".join(linhas),
            color=discord.Color.gold()
        )
        await ctx.send(embed=embed)

    @commands.command(name="adicionarpontos")
    async def adicionar_pontos_manual(self, ctx, membro: discord.Member, quantidade: int):
        await ctx.send("⚠️ Sistema de pontos desativado no momento.")

    @commands.command(name="removerpontos")
    async def remover_pontos_manual(self, ctx, membro: discord.Member, quantidade: int):
        await ctx.send("⚠️ Sistema de pontos desativado no momento.")

    @commands.command(name="relatorio")
    async def gerar_relatorio_pontos(self, ctx):
        await ctx.send("⚠️ Sistema de pontos desativado no momento.")


async def setup(bot):
    await bot.add_cog(Economia(bot))
