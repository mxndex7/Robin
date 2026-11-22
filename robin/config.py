"""Configuração: caminhos, .env, diretiva do sistema e constantes do app."""

import os
import sys
import winreg
from pathlib import Path

from dotenv import dotenv_values, set_key

APP_NAME = "Robin"
APP_VERSION = "2.0.0"
# Identidade do processo na barra de tarefas (agrupamento e ícone, inclusive rodando pelo python.exe).
APP_USER_MODEL_ID = "mxndex7.Robin"

FROZEN = bool(getattr(sys, "frozen", False))
# O Robin.exe (None no código-fonte) e a pasta dele ou do app.py.
EXE_FILE = Path(sys.executable).resolve() if FROZEN else None
APP_DIR = EXE_FILE.parent if EXE_FILE else Path(__file__).resolve().parent.parent
# --hidden: nasce escondido, com o WebView carregado (iniciar com o Windows). --quit: pede para a
# execução aberta sair (build.bat). --replace: fecha a execução aberta e abre esta no lugar.
START_HIDDEN = "--hidden" in sys.argv[1:]
QUIT_REQUESTED = "--quit" in sys.argv[1:]
REPLACE_REQUESTED = "--replace" in sys.argv[1:]
# Recursos empacotados (ui/, memory.example.md): _internal/ no exe --onedir, raiz do projeto no código-fonte.
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
UI_DIR = RESOURCE_DIR / "ui"
# Ícone da janela (16 a 256px); o exe recebe o mesmo arquivo no robin.spec.
ICON_FILE = UI_DIR / "assets" / "brand" / "robin.ico"
MEMORY_SEED = RESOURCE_DIR / "memory.example.md"

# Pasta de dados (memory.md e .env), a mesma para o exe e o código-fonte. ROBIN_DATA_DIR troca a
# pasta (testes, uma segunda cópia do app); a simulação usa a subpasta mock\ (fim deste arquivo).
DATA_DIR_OVERRIDDEN = bool(os.getenv("ROBIN_DATA_DIR", "").strip())
DATA_ROOT = Path(os.getenv("ROBIN_DATA_DIR", "").strip() or Path(os.getenv("APPDATA") or Path.home()) / "Robin").resolve()
DATA_DIR = DATA_ROOT
ENV_FILE = DATA_DIR / ".env"
MEMORY_FILE = DATA_DIR / "memory.md"
# Cérebro (brain.py): itens que o próprio Robin grava e busca pelas ferramentas. Com o Supabase,
# é a cópia local e o backup; sync.json guarda a fila de pendências e a marca da migração.
BRAIN_FILE = DATA_DIR / "brain.json"
SYNC_FILE = DATA_DIR / "sync.json"
# Conversas guardadas (conversations.py): um JSON por conversa. Com o Supabase, sync-conversas.json
# guarda a fila das conversas, a marca da migração e até onde o pull já leu (conversations_cloud.py).
CONVERSATIONS_DIR = DATA_DIR / "conversas"
CONVERSATION_SYNC_FILE = DATA_DIR / "sync-conversas.json"
# Onde ficavam os dados antes da pasta única: ao lado do Robin2.exe (dist\Robin2) e na raiz do
# projeto. O exe novo fica em dist\Robin, dois níveis abaixo da raiz.
_PROJECT_DIR = APP_DIR.parent.parent if FROZEN else APP_DIR
LEGACY_DIRS = (_PROJECT_DIR / "dist" / "Robin2", _PROJECT_DIR)

# Perfil do WebView2 e log (avisos e erros, inclusive os originais do Gemini: o exe não tem console).
WEBVIEW_DATA_DIR = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "Robin"
LOG_FILE = WEBVIEW_DATA_DIR / "robin.log"

# Porta fixa do servidor local da interface: mantém a mesma origem entre execuções
# (localStorage persiste). Se estiver ocupada, o app usa uma porta livre qualquer.
HTTP_PORT = 47822

# Chaves do .env
ENV_API_KEY = "GEMINI_API_KEY"
ENV_MODEL = "GEMINI_MODEL"
ENV_GIT_SYNC = "ROBIN_GIT_SYNC"
ENV_HOTKEY = "ROBIN_HOTKEY"  # atalho global (padrão Ctrl+Alt+R)
ENV_AUTOSTART = "ROBIN_AUTOSTART"  # 1/0: escolha de iniciar com o Windows (vazio: exe ainda não abriu)
ENV_TRAY_HINT = "ROBIN_TRAY_HINT"  # 1: o aviso "continua na bandeja" já apareceu
ENV_SUPABASE_URL = "ROBIN_SUPABASE_URL"  # https://<projeto>.supabase.co
ENV_SUPABASE_KEY = "ROBIN_SUPABASE_KEY"  # chave secreta (sb_secret_...): passa por cima do RLS
ENV_RPM = "ROBIN_RPM"  # pedidos por minuto por modelo no limitador local (0 desliga)
ENV_THEME = "ROBIN_THEME"  # system (padrão: segue o Windows), light ou dark
ENV_PALETTE = "ROBIN_PALETTE"  # brasa (padrão), caderno, ninho ou grafite

# Os 2.5 devolvem 404 para chaves novas desde set/2026. O 3.8-flash fica no seletor, mas não é o
# padrão: no diagnóstico de out/2026 ele falhou em todos os envios (503 por demanda ou sem resposta).
# Padrão flash-lite (benchmark de 31/10/2026: 1º texto em ~1-3s; o 3.5/3.6-flash levava 7-27s). A
# reserva é de outra cota diária (plano gratuito: 20 pedidos/dia por modelo; o 3.5-flash conta na do
# 3.6-flash) e precisa ser diferente do padrão, senão não há para onde trocar.
DEFAULT_MODEL = "gemini-3.5-flash-lite"
FALLBACK_MODEL = "gemini-3.6-flash"
# Reserva para quando models.list() não estiver disponível (sem chave, sem rede).
FALLBACK_MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
# Sem temperature: nos 3.x o Google recomenda o padrão (1.0); valores menores causam loops.
# Raciocínio nos flash e flash-lite 3.x (sem nível, o flash-lite usaria o padrão dele, minimal).
THINKING_LEVEL = "LOW"

# Rede. O timeout do httpx vale para conectar e para cada leitura (não é um prazo total:
# o HttpOptions.timeout do SDK vira prazo do servidor e corta respostas longas).
HTTP_CONNECT_TIMEOUT = 10.0
HTTP_READ_TIMEOUT = 60.0
# Sem nenhum pedaço do stream (nem de raciocínio) por este tempo, a tentativa é abandonada.
WATCHDOG_SECONDS = 90.0
# Espera antes da nova tentativa automática (5xx, 429, timeout).
RETRY_DELAY = 2.0
# Limitador local: pedidos por minuto por modelo (o plano gratuito recusa o 6º com 429). ROBIN_RPM no
# .env troca (0 desliga, ex.: chave paga). Uma espera maior que RATE_WAIT_MAX usa o modelo de reserva,
# se ele tiver vaga; um 429 por minuto com retryDelay até RATE_WAIT_MAX espera esse tempo.
RATE_LIMIT_PER_MINUTE = 5
RATE_WAIT_MAX = 20.0
# Troca temporária para o modelo de reserva (falhas seguidas, limite): volta sozinha ao modelo
# escolhido depois disto. Com a cota diária esgotada, vale até ela voltar (retryDelay do 429).
FALLBACK_EXPIRY_SECONDS = 300.0

# Embeddings do cérebro: o mesmo modelo e dimensão da coluna `embedding` do Supabase (fase 2).
# Conferido com models.list em out/2026: a chave tem gemini-embedding-001 e gemini-embedding-2.
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSIONS = 768
# Prazo total de um pedido de embedding (ms; aqui o prazo do servidor é o que se quer).
EMBEDDING_TIMEOUT_MS = 8000
# Rodadas de ferramentas por turno; a última é enviada com as ferramentas desligadas.
MAX_TOOL_ROUNDS = 6
# salvar_memoria não grava se já houver item com esta similaridade (cosseno entre documentos).
SIMILAR_THRESHOLD = 0.9
# A memória inteira vai no system_instruction de cada turno (sem busca para ler). Acima deste orçamento
# (~30 mil tokens, mais de mil itens), o que é a fazer entra sempre, o resto por recência, e só então
# buscar_memoria é oferecida ao modelo.
MAX_PROMPT_MEMORY_CHARS = 120_000
# O modelo manda a resposta junto com as ações de memória (no argumento resposta ou em texto na mesma
# mensagem): se todas deram certo sem ressalva, o turno termina sem o 2º pedido. Desligado, a resposta
# sempre vem num 2º pedido, depois dos resultados.
REPLY_WITH_ACTION = True
# Tarefas concluídas há mais que isto são apagadas ao abrir o Robin. None: nunca (o Robin lembra de tudo).
DONE_RETENTION_DAYS = None
# Conversas anteriores no system_instruction: as mensagens mais recentes das outras conversas, até este
# total (cada uma cortada em PROMPT_MESSAGE_CHARS). O que ficar de fora, só por buscar_conversas.
MAX_PROMPT_CONVERSATION_CHARS = 8_000
PROMPT_MESSAGE_CHARS = 300
# Conversa reaberta: o histórico da sessão leva as mensagens mais recentes até este total (só texto).
MAX_REOPEN_HISTORY_CHARS = 60_000
# Supabase: puxar da nuvem ao aparecer a janela no máximo uma vez a cada tantos segundos.
PULL_MIN_INTERVAL = 10.0
# request_id fixo do núcleo (o memory.md na nuvem, pinned=true).
CORE_REQUEST_ID = "robin:nucleo"
# Fuso das datas que o modelo recebe a cada turno ("amanhã", "sexta").
TIMEZONE = "America/Sao_Paulo"

# Intervalo de envio dos pedaços do stream para a interface (segundos).
STREAM_FLUSH_INTERVAL = 0.04

# Tema da interface: "system" segue o Windows (AppsUseLightTheme). A página guarda a mesma escolha
# no localStorage para a primeira pintura; o .env vale para a janela, antes de a página carregar.
THEMES = ("system", "light", "dark")
# Paletas de cor (tokens.css, data-palette): a primeira é o padrão.
PALETTES = ("brasa", "caderno", "ninho", "grafite")
# Cores da janela por paleta e tema (fundo antes de a página carregar, barra da moldura nativa).
# Iguais a --bg e --text em ui/css/tokens.css (tests/test_tema.py confere).
WINDOW_COLORS = {
    "brasa": {"dark": {"bg": "#110e0f", "text": "#f3ebe9"}, "light": {"bg": "#f3efed", "text": "#1d1414"}},
    "caderno": {"dark": {"bg": "#181614", "text": "#ece5d5"}, "light": {"bg": "#ece8e0", "text": "#24211b"}},
    "ninho": {"dark": {"bg": "#100f0c", "text": "#f1ecdc"}, "light": {"bg": "#efece3", "text": "#211d12"}},
    "grafite": {"dark": {"bg": "#121212", "text": "#ededed"}, "light": {"bg": "#f5f5f5", "text": "#171717"}},
}
_PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"

# Persona "parceiro próximo" (escolhida em nov/2026). O texto fala de "a pessoa" ou "você", sem supor
# nome, pronome nem gênero: o nome, se houver, vem do núcleo ({core}, escrito pela própria pessoa).
# {memory} é a memória inteira (tools.memory_for_prompt) e {conversations}, os trechos das conversas
# anteriores (conversations.ConversationStore.excerpt), congelados no início do turno.
ROBIN_SYSTEM_DIRECTIVE = """Você é o Robin, um parceiro pessoal que mora no PC e conhece a vida da pessoa que você ajuda pelo que já ouviu em conversa.

QUEM VOCÊ É
- Um amigo de confiança, não um sistema. Informal, caloroso, com humor leve e um pouco de ironia quando cabe. Tem opinião e dá quando ajuda ("eu faria assim"), sem sermão e sem bajular.
- Você lembra. Usa o que sabe com naturalidade: relaciona uma coisa com outra, cobra com jeito o que está parado, puxa o assunto certo na hora certa. Nunca anuncia "segundo minha memória" ou "nos meus registros": você simplesmente sabe.
- Quando o assunto é técnico (código, Git, arquitetura, bugs), você é afiado: vai na causa raiz, mostra código em blocos com a linguagem e não enrola.
- Trate a pessoa pelo nome que aparecer no bloco "SOBRE A PESSOA QUE VOCÊ AJUDA"; se não houver nome lá, não invente nome nem apelido. Não suponha pronome nem gênero que a pessoa não tenha dito.

COMO VOCÊ FALA
- Português do Brasil falado, como gente: frases naturais, "você", "tá" e "pra" quando soar natural.
- Curto em conversa. Lista, título e negrito só quando organizam de verdade (uma lista de compras, um passo a passo); resposta de uma frase é uma frase.
- Nunca fale da sua engrenagem: nada de "nota", "tarefa pendente", "item", "registro", "cérebro", "núcleo", "banco", "ferramenta", "função" ou "id". Para a pessoa é tudo "o que você me contou", "o que você me pediu pra lembrar", "o que falta fazer".
- Errou ou esqueceu algo? Admita numa frase ("foi mal, deixei a foto do LinkedIn de fora") e siga. Nunca se justifique com as suas regras internas.
- Sem "Como posso ajudar?", sem "Se precisar de algo, é só chamar" e sem resumir o que acabou de dizer.

SEGURANÇA (inegociável)
- Você aconselha e revisa o que a pessoa colar no chat (código, comandos, textos, planos), mas não é um agente de código: não executa nada, não edita arquivos e não mexe no PC nem em repositórios.
- Nunca diga que fez commit, push, reset, delete, que rodou um comando ou que mudou um arquivo: para ações assim, mostre o comando e espere a pessoa conferir e rodar.

AGORA: {now}

SOBRE A PESSOA QUE VOCÊ AJUDA (escrito por ela; vale acima do resto):
{core}

TUDO O QUE A PESSOA JÁ TE CONTOU OU PEDIU PRA LEMBRAR (a sua memória inteira, das coisas mais novas para as mais antigas; "a fazer" e "feito em" marcam o que é coisa a fazer; o #código serve só para as ações, nunca o mostre):
{memory}

CONVERSAS ANTERIORES COM A PESSOA (trechos das mais recentes, para você lembrar do que já falaram; a conversa de agora é a deste chat; "Pessoa" é quem você ajuda):
{conversations}

COMO VOCÊ CUIDA DA MEMÓRIA
1. "O que pedi pra anotar", "o que você sabe de mim", "o que falta fazer" e afins: responda olhando a memória inteira acima, com tudo o que for do assunto, não um tipo só de coisa.
2. Guarde sozinho, sem perguntar, o que valer lembrar depois: pessoas, datas, planos, compromissos, preferências, decisões, ideias, coisas a fazer. Não guarde papo passageiro nem o que já está na memória; se algo que já está lá mudou, atualize em vez de guardar de novo.
3. Ao guardar, mudar ou riscar algo, o que você diria à pessoa vai no argumento resposta da própria ação, com naturalidade ("Anotado: …", "Boa, risquei o pão."), como se já estivesse feito: não espere o resultado para falar. Se der algo errado, você fica sabendo e corrige.
4. Coisa a fazer vai com tarefa=true (fica marcada até a pessoa contar que fez); o resto vai com tarefa=false. Datas relativas ("amanhã", "sexta") viram data absoluta, a partir de AGORA. Etiquetas curtas.
5. A pessoa contou que fez algo que estava a fazer? Risque com concluir_tarefa (não apague).
6. Duas coisas parecidas e não dá pra saber qual? Pergunte antes de mexer.
7. Para apagar o que não é coisa a fazer: chame apagar_memoria sem confirmado e sem escrever nada; com a resposta, pergunte se pode; só quando a pessoa responder que sim, chame de novo com confirmado=true.
8. O bloco "SOBRE A PESSOA QUE VOCÊ AJUDA" só a pessoa muda, pelo editor: não tente alterar.
9. Se a ação avisar que ficou só neste PC (sem conexão com a nuvem), diga isso. Nunca diga que guardou o que não guardou.
10. Algo que vocês já conversaram e não está nos trechos nem na memória: se os trechos avisarem que há conversas mais antigas, procure com buscar_conversas antes de dizer que não lembra.
"""


def read_env() -> dict[str, str]:
    """Lê o .env da pasta de dados a cada chamada (os valores mudam em tempo de execução)."""
    if not ENV_FILE.exists():
        return {}
    return {key: (value or "") for key, value in dotenv_values(ENV_FILE).items()}


def write_env(key: str, value: str) -> None:
    """Grava uma única chave no .env, preservando as outras variáveis e os comentários."""
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.touch(exist_ok=True)
    set_key(str(ENV_FILE), key, value, quote_mode="never")


def _flag(name: str) -> str:
    """Variável de controle: ambiente do processo primeiro, .env como reserva."""
    return os.getenv(name) or read_env().get(name, "")


def theme_preference() -> str:
    """Tema escolhido (ROBIN_THEME): system, light ou dark; qualquer outro valor vale system."""
    value = read_env().get(ENV_THEME, "").strip().lower()
    return value if value in THEMES else "system"


def palette_preference() -> str:
    """Paleta escolhida (ROBIN_PALETTE); qualquer outro valor vale a padrão."""
    value = read_env().get(ENV_PALETTE, "").strip().lower()
    return value if value in PALETTES else PALETTES[0]


def system_theme() -> str:
    """Tema dos apps no Windows; escuro se o registro não puder ser lido."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PERSONALIZE_KEY) as key:
            light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
    except OSError:
        return "dark"
    return "light" if light else "dark"


def resolve_theme(preference: str) -> str:
    """O tema que a janela mostra: o escolhido ou, com "system", o do Windows."""
    return preference if preference in ("light", "dark") else system_theme()


DEBUG = _flag("ROBIN_DEBUG") == "1"
# Modo simulação: respostas locais, sem API Key nem rede. Desligado sem a variável (inclusive no exe).
MOCK = _flag("ROBIN_MOCK") == "1"
# custom: barra própria (WM_NCCALCSIZE). native: moldura nativa escura via DWM (fallback).
FRAME_MODE = "native" if _flag("ROBIN_FRAME").lower() == "native" else "custom"

# A simulação tem a própria pasta de dados (mock\): modelo, chave e memória dela nunca tocam os reais.
if MOCK:
    DATA_DIR = DATA_ROOT / "mock"
    ENV_FILE = DATA_DIR / ".env"
    MEMORY_FILE = DATA_DIR / "memory.md"
    BRAIN_FILE = DATA_DIR / "brain.json"
    SYNC_FILE = DATA_DIR / "sync.json"
    CONVERSATIONS_DIR = DATA_DIR / "conversas"
    CONVERSATION_SYNC_FILE = DATA_DIR / "sync-conversas.json"
