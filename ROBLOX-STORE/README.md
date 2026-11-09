# ROBLOX STORE

Bot independente para criar lojas de itens Roblox dentro do Discord. Cada servidor administra suas próprias categorias, produtos e painel.

## O que esta primeira versão entrega

- Banner configurável no painel público da loja.

- Categorias personalizadas por servidor.
- Cadastro de item pelo ID do catálogo Roblox.
- Consulta automática de nome, descrição, preço, favoritos e thumbnail.
- Painel com total de favoritos da loja e totais por categoria.
- Navegação privada por categoria e produto, com paginação.
- Detalhes privados do item com imagem, preço e favoritos.
- Botões para abrir o item e comprar no site oficial Roblox.
- Verificação do favorito na lista pública do username informado.
- Atualização manual e automática do catálogo.
- PostgreSQL em produção e SQLite para testes locais.

> As APIs públicas de catálogo e favoritos são classificadas como experimentais pelo Roblox. Elas podem mudar, sofrer limitação ou ficar temporariamente indisponíveis. Quando uma consulta falha, o bot preserva o último dado válido.

## Requisitos

- Python 3.12 ou superior.
- Bot criado no Discord Developer Portal.
- PostgreSQL recomendado para produção.
- Permissão de administrador no servidor Discord.


## Segurança administrativa (V1.1.1)

- `/configcatalogo` é restrito a administradores por permissão padrão e validação em tempo de execução.
- Cada painel privado fica vinculado ao administrador que o abriu e ao servidor de origem.
- Cada clique administrativo revalida a permissão de administrador e o servidor.
- Cada formulário revalida o administrador, o usuário proprietário do painel e o servidor no momento do envio.
- Consultas e alterações de categorias/produtos são limitadas ao `guild_id` do servidor.
- A publicação valida que o canal selecionado pertence ao mesmo servidor.
- Atualizações do painel bloqueiam IDs de canal que não pertençam ao servidor salvo.
- Tentativas de uso não autorizado são registradas no log, sem expor tokens.
- O bot não precisa de Administrator nem de intents privilegiadas para as funções de catálogo.

## Pacote completo consolidado

Este ZIP reúne o projeto-base e a atualização do painel administrativo `/configcatalogo`.
O arquivo `roblox_store.db` foi incluído para preservar os dados do banco SQLite enviados
com o projeto. O `.env` real não é incluído por segurança; copie `.env.example` para `.env`
e informe um token novo do Discord após redefini-lo no Developer Portal.

## Instalação local

1. Extraia o ZIP e abra a pasta `ROBLOX-STORE`.
2. Crie um ambiente virtual:

   ```bash
   python -m venv .venv
   ```

3. Ative o ambiente:

   Windows PowerShell:
   ```powershell
   .venv\Scripts\Activate.ps1
   ```

   Linux/macOS:
   ```bash
   source .venv/bin/activate
   ```

4. Instale as dependências:

   ```bash
   pip install -r requirements.txt
   ```

5. Copie `.env.example` para `.env` e configure `DISCORD_TOKEN`.
6. Para testar em um servidor específico, preencha `DEV_GUILD_ID` com o ID desse servidor.
7. Inicie:

   ```bash
   python -m src.main
   ```

## Discord Developer Portal

1. Crie uma Application e adicione um Bot.
2. Copie o token do bot para `DISCORD_TOKEN`.
3. Convide o bot usando os escopos `bot` e `applications.commands`.
4. Permissões recomendadas: View Channels, Send Messages, Embed Links, Read Message History e Use External Emojis.
5. Não é necessário habilitar intents privilegiadas nesta versão.
6. Convide o bot para seu servidor.

## Administração pelo Discord

A administração do catálogo é feita por um único comando: `/configcatalogo`. Ele abre um painel privado e interativo com botões, menus e formulários. Não é necessário usar comandos separados para criar, editar ou excluir categorias e produtos.

O comando e todas as interações do painel exigem a permissão **Administrador** do Discord. O painel é ephemeral (visível somente ao administrador que o abriu) e as interações também verificam o usuário e o servidor de origem.

No painel, os administradores podem:

- **Categorias:** adicionar, editar nome/emoji/descrição, excluir categorias vazias e organizar a ordem.
- **Produtos:** adicionar pelo ID do catálogo Roblox; o bot consulta nome, descrição, preço, favoritos e imagem automaticamente; renomear, mover de categoria, ativar/desativar, sincronizar ou excluir.
- **Personalização:** alterar título, descrição e cor do painel público; enviar banner diretamente da galeria/arquivos pelo formulário do Discord e removê-lo pelo painel.
- **Publicação:** escolher um canal de texto para publicar o catálogo ou atualizar a mensagem já publicada.
- **Sincronização:** atualizar os dados dos produtos ativos com o Roblox.
- **Estatísticas:** consultar categorias, produtos ativos e soma dos favoritos.

As categorias e os produtos têm paginação para respeitar o limite de 25 opções por menu do Discord. A loja pública continua disponível para todos os membros; somente a administração fica restrita.

Após atualizar o código e reiniciar o bot, os antigos comandos administrativos `/loja`, `/categoria` e `/item` deixam de ser registrados. Use somente `/configcatalogo`.

## Railway

1. Crie um projeto Railway e conecte o repositório GitHub.
2. Adicione um serviço PostgreSQL.
3. Configure `DATABASE_URL` com a variável de conexão do PostgreSQL.
4. Configure `DISCORD_TOKEN`.
5. Opcionalmente configure `DEV_GUILD_ID` durante os testes.
6. O comando de inicialização é `python -m src.main` (também definido no `Procfile` e `railway.json`).
7. Faça o deploy.

Nunca publique `.env` nem compartilhe o token do bot. Se um token for enviado em um chat ou arquivo compartilhado, redefina-o no Discord Developer Portal e atualize a variável `DISCORD_TOKEN`.

## Verificação de favorito

1. O cliente abre o item no Roblox e favorita manualmente.
2. Clica em “Já favoritei — verificar”.
3. Informa o username Roblox.
4. O bot resolve o username e procura o item na lista pública de favoritos do tipo de ativo correspondente.

Limitações:
- A lista de favoritos é experimental e pode estar indisponível ou limitada.
- A verificação confirma que o item aparece nos favoritos públicos daquele username; não prova, por si só, que o username pertence ao usuário do Discord.
- Para evitar confirmações falsas, o bot pode responder que não conseguiu confirmar.
- O bot não solicita senha, cookie `.ROBLOSECURITY` nem credenciais Roblox.

## Cálculo dos totais

- Total da loja = soma dos favoritos dos produtos ativos.
- Total da categoria = soma dos favoritos dos produtos ativos daquela categoria.
- Total do produto = contagem global informada pelo Roblox.

Os totais são favoritos acumulados por produto, não pessoas únicas. Uma mesma pessoa pode favoritar diversos itens e contar em cada produto.

## Limitações da V1.1

- O cadastro inicial aceita ativos (`Asset`); suporte dedicado a bundles poderá ser adicionado depois.
- Menus do Discord têm limite de 25 opções por componente; o bot usa paginação.
- A atualização automática usa `REFRESH_INTERVAL_MINUTES` (mínimo de 5 minutos).
- Se uma chamada à API falhar, o último valor válido é preservado.
- A compra é finalizada exclusivamente no site/aplicativo oficial do Roblox. O bot não processa Robux nem confirma uma compra concluída.

## Testes

```bash
python -m unittest discover -s tests
```


## Banner do painel público

No `/configcatalogo`, abra **Personalização** e use **Definir banner** para colar a URL da imagem que aparecerá no painel público da loja. Use **Remover banner** para apagar a imagem do painel.


### Banner do painel

Em `/configcatalogo` > **Personalização** > **Enviar imagem**, selecione uma imagem diretamente do celular ou computador no formulário de upload do Discord. São aceitos JPG, PNG, WEBP e GIF, com limite de 8 MB. A imagem é armazenada no banco de dados e anexada novamente ao painel público nas atualizações, sem alterar as imagens individuais dos produtos. Use **Remover banner** para retirar a imagem.

A interface de upload nativa em modal exige `discord.py` 2.7 ou superior; o `requirements.txt` foi atualizado para instalar essa versão.


## Campanhas de favoritos e sorteios

O módulo `src/commands/campaigns.py` adiciona um sistema de campanhas ao catálogo existente.

### Comandos de administrador

- `/campanha_criar`: cria uma campanha a partir do ID de um item Roblox, busca automaticamente nome, imagem e tipo do item, define meta, prêmio, quantidade de ganhadores, prazo de resposta e canal do painel.
- `/campanha_editar`: altera o item, meta, prêmio, quantidade de ganhadores e prazo da campanha ativa.
- `/campanha_participantes`: lista os participantes que tiveram o favorito confirmado.
- `/campanha_sortear`: realiza o sorteio quando a contagem de favoritos atinge a meta.
- `/campanha_substituir`: sorteia outra pessoa para uma posição cujo ganhador deixou o prazo expirar.
- `/campanha_encerrar`: encerra inscrições da campanha ativa.

### Fluxo público

1. O botão **Favoritar no Roblox** abre o item correto.
2. Depois de favoritar, o usuário clica em **Já favoritei — participar** e informa o username Roblox.
3. O bot resolve o username para um Roblox User ID e consulta a lista pública de favoritos do tipo de ativo correspondente.
4. Somente após confirmação positiva, grava a inscrição. Discord e Roblox IDs são únicos por campanha.
5. O painel exibe a quantidade atual de favoritos acima da meta, sem barra de progresso, e atualiza a mensagem existente a cada 5 minutos.
6. O botão **Ver participantes** exibe a lista de inscritos.
7. O botão **Fazer sorteio** só funciona para administradores e só libera o sorteio depois de atingir a meta.
8. Cada ganhador recebe uma DM com botão para confirmar o prêmio. O prazo é configurável; após expirar, o administrador pode usar `/campanha_substituir`.

### Banco de dados

As tabelas `campaigns`, `campaign_participants` e `campaign_winners` são criadas automaticamente por `Base.metadata.create_all`; o banco SQLite existente é preservado. Nenhuma variável nova de ambiente é obrigatória.

### Limitação importante do Roblox

A consulta individual de favoritos utiliza endpoints públicos documentados pelo Roblox, mas a própria documentação os classifica como experimentais. A confirmação depende de o perfil e a lista de favoritos estarem acessíveis publicamente; se a API não permitir confirmar, o bot não registra a participação. A contagem global pode sofrer atraso ou limitação temporária do serviço Roblox.
