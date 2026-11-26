# Robin

Assistente pessoal de desktop para Windows: conversa com o ChatGPT ou o Gemini, guarda um núcleo fixo sobre você em `memory.md` (vai nas instruções de cada sessão) e tem um **cérebro**: o que você conta e as coisas a fazer, que o próprio Robin grava, busca por significado, atualiza e conclui pelas ferramentas (function calling).

A janela é **PyWebView** (WebView2). O Python cuida da lógica (provedores, memória, configuração, janela) e a interface é HTML, CSS e JavaScript puro com ES modules, sem etapa de build e sem CDN.

## O que o Robin é (e não é)

- **É** um parceiro pessoal de memória e conversa: lembra do que você conta (pessoas, datas, planos, ideias, coisas a fazer), conversa com esse contexto e aconselha.
- **É** um revisor: cole código, um comando, um log, um texto ou um plano no chat e ele analisa, aponta a causa raiz e sugere a correção.
- **Não é** um agente de código: não executa comandos, não edita arquivos, não faz commit, push nem nada no Git, e não mexe no seu PC. Quando a solução é uma ação, ele mostra o comando e você confere e roda. Isso está na diretiva (bloco SEGURANÇA) e no próprio código: as únicas ferramentas do modelo leem e alteram a memória dele.
- Cada pessoa conecta a **própria conta do ChatGPT** ou configura a **própria chave do Gemini**. Para sincronizar entre PCs, use o **próprio projeto do Supabase**. Nada pessoal fica no repositório: memória, núcleo, chaves e conversas ficam na sua pasta de dados.

## Requisitos e limites

- **Só Windows 10 ou 11.** O código usa `winreg` e Win32 (moldura da janela, atalho global, bandeja, instância única, iniciar com o Windows); não roda em macOS nem Linux.
- **Python 3.13** (as versões das dependências são fixas para ele).
- **WebView2 Runtime** (já vem no Windows 11; no 10, instale pelo site da Microsoft se faltar).
- Uma **conta do ChatGPT elegível** ou uma **chave da Gemini API**. O Supabase é opcional.

## ChatGPT no Robin

Em **Configurações → ChatGPT**, escolha **Entrar com o ChatGPT**. O navegador abre a autorização oficial. Volte ao Robin após autorizar o uso da cota de uma conta elegível. O Robin mantém a conexão protegida para o usuário do Windows, na própria pasta de dados.

Nas configurações você pode escolher o **provedor para novas conversas**. O provedor de uma conversa existente acompanha o histórico quando você a reabre. O Robin usa sua própria memória e seus próprios arquivos de conversa, inclusive quando a conta é a mesma que você usa no ChatGPT. A memória do Robin é compartilhada entre provedores. O contexto de conversas anteriores fica restrito ao provedor que as gerou. Nesta versão, as conversas do ChatGPT ficam neste PC. A sincronização existente de memória e de conversas do Gemini continua disponível no Supabase.

Quando o ChatGPT é o provedor ativo, a busca na memória usa texto. A busca semântica existente continua disponível no Gemini quando configurado. [Como funciona o uso da cota do ChatGPT](https://help.openai.com/pt-br/articles/20001542-using-your-chatgpt-plan-in-other-apps-and-sites)

## Chave do Gemini e plano gratuito

Crie a chave no Google AI Studio e cole em **Configurações** na primeira execução (passo a passo em [Rodar](#rodar)). Ela fica no `.env` da sua pasta de dados, nunca no repositório.

No plano gratuito, os limites que medi em **nov/2026** foram **5 pedidos por minuto e 20 por dia por modelo**. O Google pode mudá-los a qualquer momento: confira os valores atuais na documentação da API. O Robin se adapta a isso: um limitador local espera a vaga do minuto (`ROBIN_RPM` no `.env` troca o limite) e, com a cota diária esgotada, passa para o modelo reserva até ela voltar (detalhes em [Como funciona](#como-funciona)). Uma mensagem comum gasta 1 pedido; guardar ou riscar algo também (a resposta vem junto com a ação).

## Privacidade

- **O provedor ativo recebe contexto da sua memória:** o núcleo e as notas e tarefas dentro do limite de contexto entram nas instruções de cada mensagem, junto com trechos das conversas recentes do mesmo provedor. Quando o Gemini está ativo, a busca por significado também pode enviar texto ao Google para calcular embeddings.
- O tratamento desses dados pelo Google depende do plano e dos [termos adicionais da Gemini API](https://ai.google.dev/gemini-api/terms). Consulte os termos aplicáveis à sua conta antes de enviar dados pessoais.
- A cópia local da memória e das conversas fica em `%APPDATA%\Robin`. Ao ativar a sincronização, o Robin também guarda esses dados no **seu** projeto Supabase.
- A chave do Gemini e a do Supabase ficam em texto puro no `.env` da pasta de dados. O log (`%LOCALAPPDATA%\Robin\robin.log`) não grava chaves.

---

## Instalar

Requisitos: veja [Requisitos e limites](#requisitos-e-limites).

```bat
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
```

O `requirements.txt` tem só o que o app usa; o `requirements-dev.txt` acrescenta o PyInstaller e o pytest. Todas as versões são fixas.

## Rodar

```bat
.venv\Scripts\python app.py
```

Na primeira execução, abra **Configurações** e conecte o ChatGPT ou cole a chave da API do Gemini e clique em **Salvar chave**. A chave do Gemini é validada antes de salvar, e a tela mostra os 4 últimos caracteres.

Variáveis de controle (no ambiente ou no `.env` da pasta de dados):

| Variável | Para quê |
|---|---|
| `ROBIN_MOCK=1` | modo simulação (abaixo) |
| `ROBIN_DEBUG=1` | abre o DevTools e libera "Medir fluidez (debug)" na palette |
| `ROBIN_FRAME=native` | moldura nativa escura do Windows no lugar da barra própria |
| `ROBIN_DATA_DIR=<pasta>` | outra pasta de dados (testes, uma segunda cópia do app); só pelo ambiente |
| `ROBIN_HOTKEY` | atalho global (padrão `Ctrl+Alt+R`); gravado por Configurações |

Argumentos: `--hidden` abre escondido (é o que o "iniciar com o Windows" usa), `--quit` pede para o Robin aberto sair e `--replace` fecha o Robin aberto e abre este no lugar.

## Gerar o executável

```bat
scripts\build.bat
```

Saída em `dist\Robin\Robin.exe` (`--onedir`, sem console). Para levar para outro PC, copie a pasta `dist\Robin\` inteira.

- Com o Robin aberto, o build pergunta **Fechar o Robin agora? (S/N)**. Com `S`, pede para ele sair (`Robin.exe --quit`; depois de ~10s, `taskkill /F`) e segue; com `N`, sai sem tocar no `dist`.
- No fim, cria ou atualiza o atalho **Robin** no menu Iniciar (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Robin.lnk`), com o ícone e o AppUserModelID da janela (`scripts\shortcut.ps1`).

Nenhum dado vai para o `dist`: o exe e o código-fonte usam a mesma pasta de dados.

---

## Dados

Tudo o que é seu fica em **`%APPDATA%\Robin`** (Configurações mostra o caminho e tem o botão **Abrir pasta**):

| Arquivo | Conteúdo |
|---|---|
| `memory.md` | o núcleo fixo da memória. Na primeira vez nasce do `memory.example.md` do projeto |
| `brain.json` | o cérebro (notas e tarefas com embeddings). Com o Supabase, é a cópia local e o backup. Fora do Git da pasta de dados |
| `sync.json` | só com o Supabase: a fila de alterações ainda não enviadas e a marca da migração |
| `sync-conversas.json` | só com o Supabase: a fila das conversas, a marca da migração e até onde o último pull leu |
| `brain.lixeira.json` | só com o Supabase: cópia dos itens apagados na nuvem (por outra máquina ou pela limpeza) |
| `conversas\` | as conversas guardadas, um arquivo `.json` por conversa. Fora do Git da pasta de dados |
| `.env` | `GEMINI_API_KEY`, `GEMINI_MODEL` (último modelo escolhido), `ROBIN_GIT_SYNC`, tema, paleta, atalho e, com a nuvem, `ROBIN_SUPABASE_URL`/`ROBIN_SUPABASE_KEY` (texto puro) |
| `.gitignore` | deixa o `.env`, a pasta `mock\`, o `brain.json`, o `sync.json`, o `sync-conversas.json` e as `conversas\` fora do Git |
| `mock\` | dados do modo simulação, separados dos reais |

O perfil do WebView2 e o log (`robin.log`, com avisos, erros e o erro original do Gemini, sem a chave) ficam em `%LOCALAPPDATA%\Robin`.

**Migração (só de versões antigas do Robin):** na primeira execução, se a pasta de dados ainda não tiver `.env`, o Robin copia o `.env` mais recente que tenha `GEMINI_API_KEY` entre `dist\Robin2\` e a raiz do projeto. O `memory.md` vem do mais recente desses dois lugares. Os arquivos antigos não são apagados, e a cópia fica registrada no log.

### Núcleo

A aba **Núcleo** da lateral edita o `memory.md` (ou o núcleo na nuvem): o que vale acima de tudo sobre você. Ele nasce do `memory.example.md`, um modelo para preencher: escreva o seu nome na linha **Nome** e o Robin passa a usá-lo (sem nome, ele não inventa um), e mantenha, mude ou apague as regras sugeridas.

- **Salvar** (`Ctrl+S`) grava o `memory.md` mantendo o fim de linha original e recria a sessão do Gemini **mantendo o histórico da conversa**.
- Salvar só existe depois de uma leitura bem-sucedida do arquivo; se a leitura falhar, o editor fica somente leitura com **Tentar de novo**. Salvar a memória vazia por cima de um arquivo com conteúdo pede confirmação.

### Cérebro

Além do `memory.md`, o Robin guarda sozinho o que vale lembrar, mesmo sem você pedir "anota": pessoas, datas, planos, preferências, ideias e coisas a fazer. "Lembre de comprar uva amanhã" fica marcado como a fazer (com a data absoluta), "comprei a uva" risca (continua guardado, com a data, para "o que eu já fiz?") e "tive uma ideia: mudar a foto do LinkedIn" é guardado sem pedido. Cada ação aparece numa linha discreta na resposta ("Salvou tarefa: comprar uva", "Concluiu: ..."). A aba **Memória** da lateral mostra tudo o que ele guardou, com busca, e deixa riscar, editar e apagar (dois cliques).

- **Memória inteira em toda mensagem:** tudo o que ele guardou vai nas instruções de cada turno, das coisas mais novas para as mais antigas. "O que pedi pra você anotar?" traz tudo, e ler a memória não gasta pedido. Se um dia passar de ~120 mil caracteres (mais de mil coisas), o que é a fazer entra sempre, o resto por recência, e só então ele ganha a busca para o que ficou de fora.
- **Personalidade:** o Robin é um parceiro próximo (informal, caloroso, humor leve, afiado no técnico) e não fala da própria engrenagem ("nota", "tarefa", "cérebro"...). O núcleo vale acima da diretiva: uma linha de estilo nele (ex.: "direto, cirúrgico, técnico") muda o tom.
- **Resposta junto com a ação:** ao guardar ou riscar algo, o modelo manda o que diria a você junto com a própria ação (um argumento `resposta`), então "lembre de X" e "comprei X" gastam 1 pedido. A resposta só aparece se a ação deu certo; com ressalva ("já existe parecido", recusa, ficou só neste PC), um 2º pedido explica. Não há frase pronta do app.
- Atualizar edita no lugar; apagar o que não é coisa a fazer pede sua confirmação na conversa antes.
- A busca (só com a memória cortada) e a conferência de parecidos usam embeddings do `gemini-embedding-001` (768 dimensões). No plano gratuito eles podem falhar com 429: a coisa é salva mesmo assim e o embedding é gerado depois; a busca cai para texto (sem acento e caixa).
- O que foi feito nunca é apagado: o Robin lembra de tudo.
- Data, hora e fuso (America/Sao_Paulo) vão nas instruções de cada turno.

### Anexar arquivos

Use o clipe junto ao campo de mensagem para selecionar até quatro arquivos de texto em UTF-8. Código, logs, Markdown e outros formatos de texto compatíveis podem ter até 32 KB por arquivo e 64 KB por mensagem no total. Confira a lista no campo de mensagem e use o botão de remover antes de enviar.

Ao enviar, o provedor ativo recebe o conteúdo dos arquivos. O histórico guarda os nomes, a mensagem e a resposta. Os arquivos originais continuam onde estavam. O conteúdo anexado fica disponível como contexto enquanto a conversa está aberta. Durante essa consulta, o modelo pode usar a memória já presente no contexto. As ferramentas de busca e edição da memória ficam suspensas enquanto o conteúdo dos anexos estiver disponível. Para gravar na memória pelo chat, inicie uma conversa sem anexos. Para usá-lo novamente depois de reabrir a conversa, anexe o arquivo em uma nova mensagem. Trechos incluídos na resposta entram no histórico da conversa e seguem as regras de sincronização do provedor.

### Consultar uma pasta no chat

Use o ícone de pasta ao lado do clipe, escolha uma pasta local e escreva o que quer investigar. O ChatGPT ou o Gemini ativo pode listar subpastas e consultar trechos de código e arquivos UTF-8 dentro da pasta escolhida. A operação é somente leitura. Links do sistema de arquivos, arquivos ocultos, pastas de dados do Robin e alguns nomes comuns de arquivos privados são excluídos. Escolha uma pasta adequada para compartilhar com o provedor, pois arquivos de código também podem conter dados sensíveis.

Por mensagem, a leitura fica limitada a 12 arquivos, até 16 KB por arquivo e 64 KB no total. Arquivos acima de 32 KB ficam indisponíveis. Durante essa consulta, as ferramentas de memória ficam suspensas. Remova a pasta pelo campo de mensagem. Escolha a pasta de novo se trocar de conversa, reabrir o histórico ou mudar de provedor.

O provedor recebe os nomes listados e os trechos consultados. O Robin guarda a mensagem e a resposta no histórico. Se a resposta citar um trecho, ele segue as regras de armazenamento e sincronização da conversa.

### Conversas

Toda conversa fica guardada neste PC, em `conversas\`, um arquivo por conversa: a mensagem na hora de enviar e a resposta no fim (com as ações, parada ou erro; regenerar troca a última resposta).

- A aba **Conversas** lista por data (Hoje, Ontem, Últimos 7 dias, Mais antigas), busca no título e no texto, reabre para continuar e apaga (dois cliques). **Nova conversa** (`Ctrl+L`, lateral ou ícone do cabeçalho) não apaga nada.
- O Robin lê trechos das conversas mais recentes em toda mensagem (até ~8 mil caracteres, sem pedido extra). Quando você fala de algo mais antigo, ele busca nas conversas antigas (`buscar_conversas`, 1 pedido a mais), e essa busca só aparece quando há conversa fora dos trechos.
- Reabrir uma conversa: a sessão recebe o texto das mensagens mais recentes (até ~60 mil caracteres); as ações antigas não voltam, porque a memória já vai inteira nas instruções.
- **Na nuvem:** com o Supabase configurado e as tabelas das conversas criadas (veja abaixo), as conversas do Gemini também aparecem nos seus outros PCs. Apagar uma conversa sincronizada num PC apaga nos outros.

### Memória na nuvem (Supabase)

Opcional: a mesma memória e as mesmas conversas nos seus PCs, num projeto do Supabase **seu**.

**Configurar (uma vez):**

1. Crie um projeto novo no [Supabase](https://supabase.com), **dedicado ao Robin** (não misture com outro app: a chave que o Robin usa tem acesso total ao banco).
2. No **SQL Editor** do projeto, rode **nesta ordem**, uma vez cada:
   1. `scripts/supabase_memorias.sql`: extensão `vector`, tabela `memories`, busca por significado (`match_memories`) e por texto;
   2. `scripts/supabase_conversas.sql`: tabelas `conversations` e `conversation_messages`.

   Os scripts foram feitos para um projeto dedicado novo. Eles ativam a segurança por linha (RLS) e concedem à função `service_role` as permissões necessárias na Data API. O Robin usa a chave secreta do seu projeto para acessar essas tabelas.
3. Nas configurações do projeto, copie a URL (`https://<projeto>.supabase.co`) e, na parte de chaves de API, uma **chave secreta** (`sb_secret_...`; não a publicável).
4. No Robin, em **Configurações → Memória na nuvem**, cole a URL e a chave e clique em **Conectar**. O Robin testa a tabela `memories` e a função `match_memories` antes de salvar; se faltar algo, a mensagem diz quais SQLs rodar.

**Avisos:**

- A chave `sb_secret_...` **ignora o RLS**: quem tiver a chave lê e apaga tudo no projeto. Ela fica em **texto puro** no `.env` da pasta de dados (`ROBIN_SUPABASE_KEY`); a tela mostra só os 4 últimos caracteres. Não compartilhe esse arquivo.
- O plano gratuito do Supabase **pausa o projeto depois de 7 dias sem uso** (veja "Projeto pausado" abaixo).

- **Sincronização:** ao abrir e sempre que a janela aparece (atalho, bandeja, abrir de novo), o Robin puxa o que mudou na nuvem. Toda alteração grava na nuvem na hora e na cópia local (`brain.json`).
- **Sem internet:** a alteração fica na cópia local e numa fila (`sync.json`), a lateral mostra "nuvem offline • N na fila" e o Robin avisa que **não** salvou na nuvem. A fila é reenviada sozinha (a cada minuto, ao aparecer a janela ou na próxima alteração), sem duplicar.
- **Duas máquinas:** antes de alterar um item, o Robin confere a versão na nuvem; se outra máquina mudou o item depois, ele recarrega e avisa em vez de sobrescrever.
- **Núcleo:** o conteúdo do `memory.md` vira um item fixo na nuvem e vai inteiro nas instruções de cada sessão. Na primeira conexão, se a nuvem ainda não tiver núcleo, o `memory.md` sobe; se já tiver (da outra máquina), a nuvem vence e o `memory.md` local fica como backup, sem mudanças. Com a nuvem, o editor da aba Núcleo edita esse núcleo (o Robin nunca o altera pelas ferramentas) e o sync Git do `memory.md` fica desligado.
- **Migração:** na primeira conexão, os itens do `brain.json` que ainda não estão na nuvem sobem (registrado no log).
- **Projeto pausado:** o Supabase gratuito pausa depois de 7 dias sem uso. O Robin mostra "Banco pausado: reative no painel do Supabase" e segue com a cópia local só para leitura até você reativar.
- **Desconectar** (Configurações) volta para o `brain.json` e o `memory.md` locais; a fila continua no `sync.json` para quando reconectar.
- **Conversas:** o `scripts/supabase_conversas.sql` (passo 2, depois do das memórias) cria as tabelas `conversations` e `conversation_messages` (com RLS ligado e sem políticas, então só a chave secreta lê e grava) e não mexe na `memories`. As mensagens do Gemini entram na fila de sincronização sem atrasar a conversa. Na primeira conexão, sobem as conversas do Gemini que já estavam no PC. Apagar uma conversa tira as mensagens dela da nuvem e deixa só uma marca (sem o título) para os outros PCs apagarem também. Se a conversa aberta for apagada em outro PC, o Robin começa outra. Sem as tabelas, as conversas ficam só no PC e o Robin avisa uma vez.

### Sync Git da memória

Opcional e desligado por padrão. Funciona quando a **própria pasta de dados** é um repositório Git com remoto (por exemplo, um repositório privado só da memória):

```bat
cd /d "%APPDATA%\Robin"
git init
git remote add origin <url-do-repositorio-privado>
git add memory.md .gitignore
git commit -m "Memória do Robin"
git branch -M main
git push -u origin main
```

Depois, ligue **Sincronizar memória com Git** em Configurações. A cada salvamento o Robin faz `git add memory.md`, `commit` e `push` em segundo plano. Sem repositório, a opção fica indisponível e Configurações explica como ativar.

---

## Sempre no PC

- **Atalho global `Ctrl+Alt+R`**, de qualquer lugar: com o Robin escondido, minimizado ou atrás de outras janelas, ele aparece com o cursor no composer; já na frente, ele se esconde. Se outro programa já usa a combinação, o log registra e Configurações mostram o aviso e o campo para trocar (clique no campo e pressione a nova combinação).
- **Bandeja:** clique abre ou esconde; o menu tem **Abrir Robin**, **Iniciar com o Windows** e **Sair**.
- **O X esconde** (Alt+F4 também). Na primeira vez, uma notificação avisa que o Robin continua na bandeja. Só **Sair** encerra de verdade (o "Finalizar tarefa" do Gerenciador de Tarefas e o desligamento do Windows também fecham).
- **Iniciar com o Windows:** o `Robin.exe` se registra em `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` (`"<pasta>\Robin.exe" --hidden`), ligado por padrão na primeira execução do exe; dá para desligar em Configurações ou na bandeja. Ele abre escondido, com a interface já carregada, para o atalho mostrar na hora. Se o exe mudar de pasta, o caminho é corrigido na próxima abertura. Pelo código-fonte o Robin nunca se registra.
- **Tema claro e escuro:** segue o Windows por padrão. O botão de sol/lua no cabeçalho, o comando "Alternar tema" da palette e Configurações (Sistema, Claro ou Escuro) trocam na hora. A escolha fica no `.env` (`ROBIN_THEME`), e a janela já abre no tema certo, sem piscar.
- **Paletas de cor:** em Configurações (ou na palette, "Paleta …"), quatro conjuntos prontos, todos com versão clara e escura e contraste AA: **Brasa** (grafite com toque de vinho e acento vermelho escuro, o padrão), **Caderno** (papel e musgo), **Ninho** (palha e ocre escuro) e **Grafite** (preto e branco neutros). Só as cores mudam. A escolha fica no `.env` (`ROBIN_PALETTE`).
- **Uma instância por pasta de dados:** abrir o Robin de novo (menu Iniciar, exe) só traz a janela aberta para a frente. Se a aberta for de **outro build** (ex.: o `Robin.exe` antigo que inicia com o Windows, e você roda o código-fonte novo), ela não vem para a frente: aparece a notificação "Já existe um Robin aberto (Robin.exe de 27/10). Use --replace para trocar." e o aviso vai para o log. O rodapé das Configurações mostra a origem e o build da janela.

## Atalhos da janela

| Atalho | Ação |
|---|---|
| `Ctrl+K` | command palette |
| `Ctrl+B` | mostrar/ocultar o painel lateral (abas Conversas, Memória e Núcleo; setas trocam a aba) |
| `Ctrl+S` | salvar o núcleo (aba Núcleo) |
| `Ctrl+L` | nova conversa (a atual continua guardada) |
| `Esc` | fecha popover, modal ou palette; sem nenhum aberto, para a geração |
| `/` | foca no composer |
| `Enter` / `Shift+Enter` | envia / quebra linha |

---

## Modo simulação

Sem API Key e sem rede, com dados próprios em `%APPDATA%\Robin\mock` (modelo, chave e memória reais não são tocados):

```bat
set ROBIN_MOCK=1
.venv\Scripts\python app.py
```

É o próprio serviço do Gemini com só a "rede" simulada: histórico, parar, regenerar, watchdog, nova tentativa, fallback e streaming seguem o código real. Gatilhos pela mensagem:

| Mensagem contém | Resposta |
|---|---|
| `tabela` | tabela markdown |
| `codigo` / `código` | bloco Python e bloco bash |
| `longo` | ~60 linhas (para rolar e parar no meio) |
| `erro` | falha de rede simulada |
| `fallback` | modelo indisponível → troca para a reserva (`gemini-3.6-flash`) e grava no `.env` da simulação |
| `lento` | 100s só raciocinando: o watchdog abandona, tenta de novo e cai para a reserva nesta sessão (~3 min) |
| `cota` | 429 com `limit: 0` (chave sem cota no modelo): tenta de novo, cai para a reserva nesta sessão e o seletor marca o modelo como "sem cota nesta chave" |
| "o que eu disse antes?" | cita a mensagem anterior a partir do histórico real |
| "lembre de X" / "lembre que X" | guarda algo a fazer / um fato, com a resposta no argumento da ação (1 pedido) |
| "o que pedi pra você anotar" | tudo o que está na memória do prompt |
| "o que tenho pra fazer" / "quais tarefas cumpri" | o que está a fazer / o que foi feito, da memória do prompt |
| "comprei X" | acha o que está a fazer e risca |
| "fruta roxa" / "busque X" | responde da memória do prompt (o embedding falso aproxima "uva" de "fruta roxa pequena"); com a memória cortada, busca por significado |
| "esqueça X" e depois "confirmo" | apagar com confirmação |
| "conversamos sobre X" / "falamos sobre X" | responde com os trechos das conversas anteriores; com conversa antiga fora deles, busca com `buscar_conversas` |
| "sem embedding" | os embeddings falham (429) por 30s: salva sem vetor |
| "sem internet" / "pausar banco" / "reativar banco" | com a nuvem simulada conectada (Configurações, qualquer URL e chave `sb_secret_...`): 30s offline / banco pausado / volta ao normal |

### Interface no navegador, sem o Python

```bat
.venv\Scripts\python scripts\serve_ui.py
```

Abra `http://127.0.0.1:5180/index.html?mock&key`. Os parâmetros (`&error=mid`, `&fallback`, `&retry`, `&memerror`, `&git`, `&noquota`, `&hotkeybusy`, `&exe`, `&fast`, `&history`...) estão no topo de `ui\js\mock.js`; `&history` começa com 3 conversas guardadas e algumas coisas na memória.

---

## Testes

```bat
.venv\Scripts\python -m pytest
```

Os testes rodam o serviço real com a rede simulada e nunca tocam a pasta de dados real. Para conferir a chave de verdade (modelos disponíveis e envios curtos, sem imprimir a chave):

```bat
.venv\Scripts\python scripts\smoke_real.py
.venv\Scripts\python scripts\smoke_real.py --no-list --variants brain --models gemini-3.5-flash-lite
```

A variante `brain` passa pelo cérebro de verdade num `brain.json` temporário e com um núcleo neutro (o seu não é tocado nem enviado): pede para lembrar de algo a fazer, conta uma ideia sem pedir para anotar, pergunta o que pediu para anotar e risca o que fez. Mostra a resposta e quantos pedidos ao modelo cada mensagem gastou (1 = a resposta veio junto com a ação); com um modelo só, gasta de 4 a 8 pedidos do plano gratuito e espera entre as mensagens por causa do limite por minuto.

```bat
.venv\Scripts\python scripts\smoke_real.py --no-list --variants supabase
```

A variante `supabase` usa o seu banco de verdade: valida a tabela e a RPC, cria itens com a etiqueta `smoke-test`, busca pela RPC e pelo texto, testa a proteção de versão com duas "máquinas" e, no fim, **apaga tudo o que tem a etiqueta `smoke-test`**. Os seus itens não são alterados. Depois confere as tabelas das conversas (o `updated_at` do servidor, o reenvio sem duplicar e a marca de apagada) com uma conversa `smoke-test`, que também é apagada no fim.

---

## Estrutura

```
app.py                  ponto de entrada: janela pywebview e servidor local da ui/
memory.example.md       modelo do núcleo (preencha o Nome e as suas regras)
robin\
  config.py             caminhos (pasta de dados, recursos), .env, diretiva, modelos, prazos
  datadir.py            cria a pasta de dados e migra os dados antigos
  gemini.py             sessão, streaming, parar, regenerar, watchdog, nova tentativa, fallback, laço das ferramentas
  brain.py              cérebro: interface MemoryStore e LocalStore (brain.json), busca por significado/texto
  supabase.py           cérebro no Supabase: cliente REST, fila offline, versão, pull, núcleo e migração
  conversations_cloud.py conversas no Supabase: fila, envio em segundo plano, pull incremental e migração
  cloud_mock.py         Supabase simulado (testes e modo simulação)
  tools.py              ferramentas do cérebro (function calling) e idempotência por turno
  conversations.py      conversas guardadas no PC: arquivos, lista, trechos para o prompt, busca e gravação
  clock.py              data e hora em America/Sao_Paulo
  mock.py               modo simulação (inclusive function calls e embedding falso)
  memory.py             memory.md e sync Git
  window.py             moldura própria (Win32/DWM), mostrar/esconder/trazer para a frente, área de transferência
  desktop.py            sempre no PC: atalho, bandeja, X que esconde, Sair, iniciar com o Windows
  instance.py           instância única (mutex) e janela de mensagens (atalho global, pedidos de outra execução)
  build.py              origem e build (exe ou código-fonte) e o instancia.json
  notice.py             notificação da bandeja numa segunda execução (Win32 puro)
  ratelimit.py          limitador local de pedidos por minuto por modelo
  timing.py             logs "tempo:" por turno (onde o tempo vai e onde para)
  hotkey.py             texto do atalho ("Ctrl+Alt+R") ↔ RegisterHotKey
  tray.py               ícone da bandeja (NotifyIcon)
  startup.py            chave Run do Windows
  api.py                RobinAPI exposta ao JavaScript
ui\
  index.html
  css\                  tokens.css (única fonte de cor, tipografia, espaço, raio, tamanho e
                        movimento; temas claro e escuro), base, components, animations
  js\                   main, bridge, chat, stream-render, markdown, composer, sidebar,
                        conversations, memory-list, confirm, theme, models, modal, cloud,
                        palette, toast, overlay, intro, titlebar, icons, perf, mock
  vendor\               marked, DOMPurify, highlight.js (versões em vendor\VERSIONS.md)
  assets\fonts\         Geologica e Martian Mono; Geist Mono só para símbolos (OFL)
  assets\brand\         logo: robin.ico, robin-symbol.svg, PNGs e SVGs originais
scripts\
  build.bat, robin.spec build do exe
  shortcut.ps1          atalho do menu Iniciar com o AppUserModelID
  serve_ui.py           serve a ui/ sem cache para o mock no navegador
  smoke_real.py         diagnóstico com a chave real
  supabase_memorias.sql  tabela das memórias e busca no Supabase (roda primeiro, uma vez, no SQL Editor)
  supabase_conversas.sql tabelas das conversas no Supabase (roda depois do das memórias)
tests\
```

### Como funciona

- **Ponte Python ↔ JS:** o JS chama `window.pywebview.api.*`; o Python responde com eventos `window.robin.onChunk/onDone/onError/...`. Cada geração tem um `gen_id`, e eventos de uma geração parada ou limpa são ignorados.
- **Streaming:** o Python junta os pedaços e envia a cada ~40ms; a interface re-renderiza só o bloco final do markdown, no máximo uma vez por frame, com fade só no texto novo.
- **Modelos:** padrão `gemini-3.5-flash-lite` com raciocínio `LOW` (no benchmark de 31/10/2026, 1º texto em ~1-3s; o 3.5/3.6-flash levava 7-27s), reserva `gemini-3.6-flash` (outra cota diária). O seletor lista só modelos estáveis de chat (a partir do 3.x e `gemini-4-*`); um modelo que deu 429 com `limit: 0` fica marcado como "sem cota nesta chave" até reabrir o app (continua selecionável). Sem `temperature`; com `include_thoughts`, a interface mostra "Raciocinando" enquanto só chega raciocínio, e os segundos desde o envio ("Pensando · 12s").
- **Cota do plano gratuito:** 5 pedidos por minuto e **20 por dia** por modelo (o `gemini-3.5-flash` conta na cota do `gemini-3.6-flash`). Um limitador local espera a vaga do minuto em vez de tomar 429 ("Aguardando o limite por minuto"; `ROBIN_RPM` no `.env` troca o limite, 0 desliga). Cota diária esgotada: troca direto para a reserva até ela voltar.
- **Resposta junto com a ação:** as ações que alteram a memória levam o argumento `resposta` (o que o modelo diria a você); com as ações limpas, ela aparece e o turno termina ali (1 pedido). "Já existe parecido", recusas, fila sem conexão ou uma chamada sem resposta levam ao 2º pedido. Medido com a chave real (nov/2026): o flash-lite não escrevia texto junto com a chamada (2 pedidos por ação); com o argumento, 1.
- **Rede e falhas:** timeout de conexão (10s) e de leitura (60s) no httpx, nunca o `HttpOptions.timeout` (vira prazo total no servidor e corta respostas longas). Um watchdog abandona a tentativa sem nenhum pedaço por 90s. Antes de qualquer texto na tela: modelo inexistente (404) troca para a reserva e grava no `.env` (403 vira o erro "Sem permissão", sem trocar); falha passageira (5xx, 429, timeout) tenta de novo após 2s e, se persistir, usa a reserva por 5 minutos; depois o próximo envio volta sozinho ao modelo escolhido.
- **Ferramentas:** o laço é do Robin (o automatic function calling do SDK fica desligado). Cada rodada parte do histórico anterior mais as rodadas já concluídas, então uma nova tentativa ou a troca de modelo nunca repete uma ação; regenerar reaproveita as ações do mesmo turno. Parar durante uma ação espera ela terminar.
- **Moldura:** a barra de título é da interface; redimensionar, snap, Win+setas e duplo clique continuam nativos do Windows. O R ao lado do nome mostra o estado (traço na cor de destaque quando pronto; pulsa enquanto gera). A borda nativa e o fundo da janela seguem o tema.
- **Visual (identidade "Brasa"):** grafite com um toque de vinho, um único acento vermelho escuro puxado para o vinho e o traço inclinado do R como gesto: o "pensando" são três traços acendendo, o cursor do streaming é inclinado e o item ativo leva o traço. Tudo vem dos tokens do `tokens.css` (os testes barram cor e tamanho fixos fora dele), com contraste AA nos dois temas e animações só de `transform` e `opacity`.
- **Ícone:** o `robin.ico` vai no exe (`robin.spec`) e na janela (`Form.Icon`), e o processo usa o AppUserModelID `mxndex7.Robin`: a barra de tarefas e o Alt+Tab mostram o R também rodando pelo código-fonte.

---

Criado por [mxndex7](https://github.com/mxndex7).
