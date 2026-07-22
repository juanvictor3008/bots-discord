# Bot da Guilda: I DieHard I (Albion Online)

Bot multifuncional para Discord desenvolvido em Python (`discord.py`). Projetado para automatizar o gerenciamento da guilda **I DieHard I** no Albion Online.

---

## Conteúdo / LFG

| Comando | Descrição |
|:---|:---|
| `/content` | Cria painel interativo de vagas para conteúdo em grupo |
| `/template` | Gerencia templates de conteúdo (criar, listar, remover) |
| `!agenda` | Lista todos os pings ativos e agendados |

### Painel de Vagas

- Dropdown com classes (Tank, Suporte, DPS, etc.) — limite de 25 classes
- Fila de espera automática quando a vaga está cheia
- Call voice criada automaticamente na hora ou agendada (30min antes do horário)
- Botões disponíveis no painel:

| Botão | Quem pode usar | O que faz |
|:---|:---|:---|
| ❌ **Sair da Lista** | Qualquer inscrito | Remove o próprio da lista |
| 🛑 **Encerrar PT** | Líder / Staff | Encerra conteúdo, deleta call, salva pontos |
| ✏️ **Editar** | Líder / Staff | Altera título, descrição, vagas e renomeia a call |
| ▶️ **Iniciar Conteúdo** | Líder / Staff | Ativa contagem de presença e pontos |
| 🛠️ **Gerenciar Vagas** | Líder / Staff | Remove qualquer inscrito da lista ou fila |

---

## Sistema de Pontos

- 1 food = 30 minutos, teto = foods x 10 pontos (ex: 4 foods → máx 40 pts)
- Presença rastreada a partir do clique em "▶️ Iniciar Conteúdo"
- Encerramento automático: call vazia por 5 minutos → fecha, salva pontos e DM pro líder

| Comando | Descrição |
|:---|:---|
| `!pontos` | Mostra saldo total e últimos conteúdos participados |
| `!ranking` | Top 15 jogadores com mais pontos |

---

## Sorteio

| Comando | Descrição |
|:---|:---|
| `!sorteio` | Cria inscrição para o sorteio |
| `!sorteio rodar [premio]` | Encerra inscrições e sorteia vencedor |
| `!sorteio listar` | Lista todos os inscritos |
| `!sorteio tempo` | Mostra tempo acumulado em call |
| `!sorteio config [min]` | Altera tempo mínimo de call para participar |
| `!sorteio premio [nome]` | Define o prêmio atual |
| `!sorteio status` | Mostra status do sorteio |

---

## Enquete

| Comando | Descrição |
|:---|:---|
| `!enquete [pergunta]` | Cria enquete com reações (✅/❌), expiração automática |
| `!encerrarenquete` | Encerra manualmente a enquete ativa |

---

## Log de Mensagens

| Comando | Descrição |
|:---|:---|
| `!log canal` | Define canal onde logs de mensagens editadas/deletadas são enviados |
| `!log desativar` | Desativa o sistema de logs |

---

## Funcionalidades Automáticas

- **Calls Dinâmicas:** Membro entra no canal gerador → bot cria sala temporária. Quando vazia, deleta.
- **Auditoria Diária:** Bot verifica se membros ainda estão na guilda/aliança no jogo via API do Albion Online. Remove cargo automaticamente se não estiverem.
- **Limpeza Automática:** Mensagens antigas são removidas periodicamente do canal de limpeza.
- **Resiliência MongoDB:** Ping a cada 30 minutos para manter conexão ativa. Fallback para JSON local quando MongoDB não está configurado.

---

Desenvolvido por: Juan Victor Dias Claros
