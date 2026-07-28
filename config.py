import os
from dotenv import load_dotenv
import motor.motor_asyncio
from datetime import timezone, timedelta
import certifi

# Carrega as senhas do arquivo .env
load_dotenv()

# IDs Principais
GUILDA_ALBION_ID = "oZercpSURfeSz9_7Mpg1-w"  
ALIANCA_ALBION_ID = "mCKNk3EyQ8qLeNMhozFIZg" 
CANAIS_GERADORES_IDS = [1486876393035010148,1522060112020242533] 
CANAL_LIMPEZA_ID = 1519195690503245835
MENSAGEM_CLASSES_ID = 1519507112156463195  

# Tags e Fuso
TAG_GUILDA = "[DH]" 
TAG_ALIANCA = "[ALLY]"
FUSO_HORARIO = timezone(timedelta(hours=-3)) # UTC-4 (Manaus/Amazonas)

# Dicionários de Cargos e Classes
REACOES_CLASSES = {
    "🛡️": 1519187254042693652,  
    "💖": 1519187260774551661,  
    "🏹": 1519187263056122017,  
    "⚔️": 1519187266000523285,  
}

CARGOS = {
    "aliado": 1519187015307104276,
    "friends": 1519186964853555210, 
    "peleco": 1519187046051086497,  
    "DIE HARD": 1519186777838190592, 
    "recrutador": 1519186606836154428,
    "moderador": 1519186575139799061,
    "caller novato" : 1519186854614663208,
    "caller": 1519186818392920064,
    "SUB-LIDER": 1519186609310793728,
    "lider": 1519186611315933345,   
}   

# Configurações de Economia
MINIMO_PESSOAS_CALL = 5
PONTOS_POR_CICLO = 1
MULTIPLICADOR_CALLER = 5
LIMITE_ADICAO_POR_VEZ = 10
MINIMO_JOGADORES_PONTOS = 5
CARGOS_PERMITIDOS_ADICIONAR = ["lider", "SUB-LIDER", "caller", "caller novato"] 
CARGOS_PERMITIDOS_REMOVER = ["lider", "SUB-LIDER"]

# Configurações de Sorteio
MINUTO_MINIMO_CALL_PADRAO = 60
CARGOS_PERMITIDOS_SORTEIO = ["lider", "SUB-LIDER", "moderador"]

# Conexão MongoDB
MONGO_URI = os.getenv('MONGO_URI')
if MONGO_URI:
    mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
        MONGO_URI,
        tlsCAFile=certifi.where(),
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=10000,
        socketTimeoutMS=20000,
        maxPoolSize=50,
        retryWrites=True,
    )
    db = mongo_client["guilda_bot"]
    colecao_pontos = db["pontos"]
    colecao_sorteio_config = db["sorteio_config"]
    colecao_sorteio_inscritos = db["sorteio_inscritos"]
    colecao_tempo_call = db["tempo_call"]
    colecao_templates = db["templates"]
    colecao_eventos = db["eventos"]
    colecao_presencas = db["presencas"]
else:
    colecao_pontos = None
    colecao_sorteio_config = None
    colecao_sorteio_inscritos = None
    colecao_tempo_call = None
    colecao_templates = None
    colecao_eventos = None
    colecao_presencas = None
    print("⚠️ MONGO_URI não encontrada!")