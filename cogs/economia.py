import discord
from discord.ext import commands

# Importa as variáveis do seu arquivo de configuração
class Economia(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="adicionarpontos")
    async def adicionar_pontos_manual(self, ctx, membro: discord.Member, quantidade: int):
        await ctx.send("⚠️ Sistema de pontos desativado no momento.")


    @commands.command(name="removerpontos")
    async def remover_pontos_manual(self, ctx, membro: discord.Member, quantidade: int):
        await ctx.send("⚠️ Sistema de pontos desativado no momento.")


    @commands.command(name="relatorio")
    async def gerar_relatorio_pontos(self, ctx):
        await ctx.send("⚠️ Sistema de pontos desativado no momento.")

# Função obrigatória para o main.py carregar este arquivo
async def setup(bot):
    await bot.add_cog(Economia(bot))