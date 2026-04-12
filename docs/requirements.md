# TileClass - Aplicação Web de Classificação de Tiles de Satélite

## 1. Visão Geral

Aplicação web para classificação pixel-a-pixel de tiles de imagens de satélite. Operadores recebem tiles 256×256 pixels (resolução 2.5m) e atribuem uma das 6 classes a cada pixel usando ferramentas de pintura. O sistema gerencia a fila de trabalho, revisão por pares e acompanhamento de progresso.

**Stack:** Python (FastAPI) + Vanilla JS + SQLite

**Escopo:** 1000 tiles, até 10 operadores simultâneos, uso em rede local.

---

## 2. Arquitetura Geral

### 2.1 Backend (Python / FastAPI)

- API REST com FastAPI
- Autenticação JWT (access token + refresh token)
- SQLite como banco único (controle + armazenamento de dados classificados)
- Tiles classificados armazenados como PNG de banda única dentro do SQLite (coluna BLOB)
- Servir arquivos estáticos do frontend (HTML, JS, CSS)

### 2.2 Frontend (Vanilla JS)

- SPA simples sem framework
- Canvas HTML5 para o editor de classificação
- Comunicação com backend via fetch API
- Duas views principais: editor (operador) e painel admin

### 2.3 Imagem de Fundo

- Consumida diretamente de um TileServer-GL já configurado na rede via protocolo XYZ
- O frontend carrega os tiles XYZ como imagem de fundo do canvas usando a URL template configurável
- O backend fornece os metadados de georreferência de cada tile (bounding box, zoom, coordenadas XYZ do TileServer) para o frontend saber quais tiles XYZ carregar como fundo

### 2.4 Armazenamento

Tudo em SQLite, um único arquivo .db:

- Tabela de usuários (credenciais, role)
- Tabela de tiles (metadados, status, atribuição, PNG classificado como BLOB)
- Tabela de log de ações (quem fez o quê, quando)

---

## 3. Modelo de Dados (SQLite)

### 3.1 Tabela `users`

| Coluna       | Tipo    | Descrição                              |
|-------------|---------|----------------------------------------|
| id          | INTEGER | PK autoincrement                       |
| username    | TEXT    | Único, login do operador               |
| password_hash | TEXT  | Hash bcrypt da senha                   |
| role        | TEXT    | "operator" ou "admin"                  |
| created_at  | TEXT    | ISO 8601                               |

### 3.2 Tabela `tiles`

| Coluna         | Tipo    | Descrição                                             |
|---------------|---------|-------------------------------------------------------|
| id            | INTEGER | PK autoincrement                                      |
| name          | TEXT    | Identificador legível do tile (ex: "tile_0042")       |
| bbox_west     | REAL    | Bounding box - longitude oeste                        |
| bbox_south    | REAL    | Bounding box - latitude sul                           |
| bbox_east     | REAL    | Bounding box - longitude leste                        |
| bbox_north    | REAL    | Bounding box - latitude norte                         |
| zoom          | INTEGER | Nível de zoom do TileServer para este tile            |
| tile_x        | INTEGER | Coordenada X no grid XYZ                              |
| tile_y        | INTEGER | Coordenada Y no grid XYZ                              |
| status        | TEXT    | Ver seção 3.4                                         |
| assigned_to   | INTEGER | FK users.id - operador atualmente atribuído           |
| classified_by | INTEGER | FK users.id - quem classificou                        |
| reviewed_by   | INTEGER | FK users.id - quem revisou                            |
| classified_at | TEXT    | ISO 8601                                              |
| reviewed_at   | TEXT    | ISO 8601                                              |
| data_png      | BLOB    | PNG de banda única 256×256, valores 0-6 e 255         |
| problem_note  | TEXT    | Descrição do problema (se reportado)                  |
| context_tiles | TEXT    | JSON com os tile_x/tile_y vizinhos para contexto      |

### 3.3 Tabela `action_log`

| Coluna    | Tipo    | Descrição                                      |
|----------|---------|------------------------------------------------|
| id       | INTEGER | PK autoincrement                               |
| user_id  | INTEGER | FK users.id                                    |
| tile_id  | INTEGER | FK tiles.id                                    |
| action   | TEXT    | "classify", "review", "report_problem", "reset", "request_review" |
| detail   | TEXT    | Detalhes adicionais (JSON livre)               |
| created_at | TEXT  | ISO 8601                                       |

### 3.4 Status do Tile (máquina de estados)

```
pending ──> in_progress ──> classified ──> in_review ──> reviewed
                │                │              │
                v                v              v
            problem          problem        problem
```

Estados possíveis:

- **pending**: nenhum operador pegou ainda
- **in_progress**: atribuído a um operador, em edição
- **classified**: operador submeteu a classificação
- **in_review**: atribuído a um revisor
- **reviewed**: revisor aprovou
- **problem**: operador ou revisor reportou problema

Transições importantes:

- `pending -> in_progress`: sistema atribui ao operador que pediu próximo tile
- `in_progress -> classified`: operador submete
- `in_progress -> problem`: operador reporta problema
- `classified -> in_review`: sistema atribui a um revisor (diferente do classificador original)
- `in_review -> reviewed`: revisor aprova
- `in_review -> problem`: revisor reporta problema
- `problem -> pending`: admin reseta o tile (limpa dados, volta pra fila)
- `reviewed -> in_review`: admin manda para nova revisão

---

## 4. API REST

### 4.1 Autenticação

| Endpoint             | Método | Descrição                    |
|---------------------|--------|------------------------------|
| /api/auth/login     | POST   | Recebe username/password, retorna JWT |
| /api/auth/refresh   | POST   | Renova token                 |
| /api/auth/me        | GET    | Retorna dados do usuário logado |

O JWT deve conter: user_id, username, role, exp. Expiração de 8 horas (turno de trabalho). O refresh token expira em 24h.

### 4.2 Operador

| Endpoint                        | Método | Descrição                                |
|--------------------------------|--------|------------------------------------------|
| /api/tiles/next                | GET    | Retorna próximo tile disponível (revisão tem prioridade sobre classificação) |
| /api/tiles/{id}                | GET    | Retorna metadados do tile + PNG classificado atual |
| /api/tiles/{id}/classify       | POST   | Recebe PNG classificado, muda status para "classified" |
| /api/tiles/{id}/review         | POST   | Revisor aprova, muda status para "reviewed" |
| /api/tiles/{id}/report-problem | POST   | Reporta problema com nota, muda status   |
| /api/tiles/{id}/image          | GET    | Retorna o PNG de classificação atual (para renderizar no canvas) |

**Regra do endpoint /api/tiles/next:**

1. Primeiro, buscar tiles com status `in_review` não atribuídos (prioridade para revisão)
2. Se não houver, buscar tiles com status `pending`
3. Um revisor nunca recebe um tile que ele mesmo classificou
4. Ao atribuir, mudar status para `in_progress` ou `in_review` e registrar assigned_to
5. Retornar os metadados necessários para o frontend montar a visualização (bbox, coordenadas XYZ, tile_x, tile_y, zoom)

**Regra de classificação:**

- O body do POST /api/tiles/{id}/classify deve ser o array binário (Uint8Array, 65536 bytes) com valores de cada pixel (0-6 para classes, 255 para não preenchido)
- O backend converte para PNG de banda única e salva no campo data_png
- Validação: rejeitar se ainda houver pixels com valor 255 (não preenchidos). Retornar erro com a contagem de pixels faltantes.

### 4.3 Admin

| Endpoint                         | Método | Descrição                                |
|---------------------------------|--------|------------------------------------------|
| /api/admin/dashboard            | GET    | Estatísticas gerais de progresso         |
| /api/admin/tiles                | GET    | Lista tiles com filtros (status, user, etc) |
| /api/admin/tiles/{id}/reset     | POST   | Reseta tile: limpa data_png (volta tudo pra 255), status -> pending |
| /api/admin/tiles/{id}/re-review | POST   | Manda tile reviewed de volta pra revisão |
| /api/admin/tiles/problems       | GET    | Lista tiles com status "problem"         |
| /api/admin/users                | GET    | Lista usuários e suas estatísticas       |
| /api/admin/users                | POST   | Cria novo usuário                        |

### 4.4 Configurações

| Endpoint                | Método | Descrição                                  |
|------------------------|--------|--------------------------------------------|
| /api/config/classes    | GET    | Retorna as 6 classes (id, nome, cor)       |
| /api/config/tileserver | GET    | Retorna URL template do TileServer-GL      |

A configuração das classes e do TileServer deve ser feita via um arquivo de configuração no backend (JSON ou YAML), não hardcoded.

---

## 5. Frontend - Editor de Classificação

### 5.1 Layout da Tela do Editor

```
┌──────────────────────────────────────────────────────────────┐
│  Header: logo, username, tiles feitos hoje, botão logout     │
├──────────────┬───────────────────────────┬───────────────────┤
│              │                           │                   │
│  Painel de   │    Canvas Principal       │   Mini-mapa de    │
│  Classes     │    (tile 256×256          │   Contexto        │
│              │     renderizado em        │   (imagem maior   │
│  [1] Classe  │     ~640×640 na tela)     │   com tile        │
│  [2] Classe  │                           │   destacado)      │
│  [3] Classe  │                           │                   │
│  [4] Classe  │                           │                   │
│  [5] Classe  │                           │                   │
│  [6] Classe  │                           │                   │
│              │                           │                   │
│  ─────────── │                           │                   │
│  Ferramentas │                           │                   │
│  [Brush]     │                           │                   │
│  [Borracha]  │                           │                   │
│  Tamanho: ── │                           │                   │
│              │                           │                   │
│  ─────────── │                           │                   │
│  Opacidade   │                           │                   │
│  da máscara  │                           │                   │
│  ──────────  │                           │                   │
│              │                           │                   │
├──────────────┴───────────────────────────┴───────────────────┤
│  Footer: [Undo] [Redo] | [Reportar Problema] | [Submeter]   │
└──────────────────────────────────────────────────────────────┘
```

### 5.2 Canvas Principal

O canvas principal é composto por **três camadas sobrepostas**, todas do tamanho de exibição (ex: 640×640 ou 768×768 pixels na tela):

1. **Camada de fundo (imagem de satélite):** canvas que renderiza os tiles XYZ do TileServer-GL correspondentes à área do tile de trabalho. Fica sempre embaixo.

2. **Camada de classificação (máscara):** canvas onde o operador pinta. Cada "pixel lógico" (correspondente a 1 pixel do tile 256×256) é renderizado como um quadrado de N×N pixels na tela (ex: se o canvas tem 640px de lado, cada pixel lógico ocupa ~2.5px). As cores das classes devem ser aplicadas com transparência controlável pelo operador. Este canvas tem `globalAlpha` ajustável.

3. **Camada de interação (cursor):** canvas transparente por cima dos outros, usado apenas para renderizar o cursor do brush (círculo indicando tamanho) e receber eventos de mouse. Nenhum conteúdo persistente.

**Mapeamento de coordenadas:** cada evento de mouse na camada de interação deve ser convertido para coordenadas lógicas (0-255, 0-255) do tile 256×256. Isso é o que determina qual pixel está sendo pintado.

**Renderização da máscara:** ao pintar, o operador modifica um array interno `Uint8Array(65536)` com os valores de classe (0-6, 255). A renderização visual no canvas de classificação converte cada valor do array para a cor correspondente da classe. Pixels com valor 255 (não preenchido) devem ser renderizados como totalmente transparentes.

### 5.3 Problema da Visibilidade (Pintura sobre Imagem)

Esta é uma preocupação central de UX. Quando o operador pinta classes sobre a imagem de satélite, as cores da classificação podem dificultar a visualização dos detalhes da imagem abaixo. Soluções obrigatórias:

1. **Slider de opacidade da máscara:** permite ao operador ajustar a transparência da camada de classificação de 0% (invisível, só vê o satélite) a 100% (opaco, só vê as classes). Valor padrão: 50%. Atalho de teclado: roda do mouse com Ctrl pressionado, ou teclas `[` e `]`.

2. **Toggle rápido de visibilidade:** tecla `Space` (barra de espaço) esconde completamente a máscara enquanto pressionada. Ao soltar, a máscara volta. Isso permite ao operador "espiar" a imagem de satélite sem alterar o slider. Esse é o mecanismo mais importante para produtividade.

3. **Cores das classes com boa diferenciação:** as 6 cores das classes devem ser altamente contrastantes entre si E razoavelmente visíveis sobre imagens de satélite (que tendem a ter tons de verde, marrom e cinza). Sugestão de paleta: vermelho vivo, azul royal, amarelo, magenta, ciano, laranja. Evitar verde (confunde com vegetação) e tons escuros (somem sobre sombras).

4. **Contorno de classe:** cada pixel classificado deve ter um contorno fino (1px na escala lógica) ligeiramente mais escuro que a cor de preenchimento. Isso ajuda a distinguir os limites entre classes adjacentes mesmo com opacidade baixa.

5. **Modo "só contornos":** atalho de teclado `O` alterna entre modo preenchido (com opacidade) e modo só contornos (sem preenchimento, apenas bordas das regiões classificadas). Útil para verificar limites sem obstruir a imagem.

### 5.4 Ferramentas

#### 5.4.1 Brush (Pincel)

- Pinta pixels com a classe selecionada
- Tamanho ajustável: 1×1, 3×3, 5×5, 7×7, 11×11 pixels lógicos (slider ou teclas `+`/`-`)
- Formato: quadrado (mais previsível em grid de pixels do que círculo)
- Ao clicar: pinta os pixels dentro da área do brush
- Ao arrastar: pinta continuamente ao longo do caminho do mouse (interpolar pontos entre eventos mousemove para não deixar buracos)

#### 5.4.2 Borracha

- Funciona como o brush, mas atribui valor 255 (não preenchido) aos pixels
- Mesmo controle de tamanho do brush
- Atalho: tecla `E` para alternar entre brush e borracha

#### 5.4.3 Undo/Redo

- Cada ação de pintura (mousedown até mouseup) é uma entrada no histórico
- Armazenar no mínimo 50 níveis de undo
- Cada entrada do histórico guarda: cópia dos pixels alterados (posição + valor anterior), não o array inteiro, para economia de memória
- Atalhos: `Ctrl+Z` (undo), `Ctrl+Shift+Z` ou `Ctrl+Y` (redo)
- Ao fazer nova pintura após um undo, descartar o histórico de redo

#### 5.4.4 Flood Fill (Balde de Tinta) - Opcional mas Recomendado

- Preenche região contígua de mesma classe (ou não preenchida) com a classe selecionada
- Conectividade 4 (cima, baixo, esquerda, direita)
- Atalho: tecla `G`
- Extremamente útil para classificar regiões grandes homogêneas rapidamente
- Se implementado, conta como uma única ação para undo

### 5.5 Seleção de Classe

- Painel lateral com as 6 classes, cada uma com cor e nome
- Atalhos numéricos: teclas `1` a `6` selecionam a classe correspondente
- A classe ativa deve ter destaque visual claro (borda, tamanho maior, indicador)
- Ao passar o mouse sobre um pixel classificado, mostrar o nome da classe em um tooltip ou na barra de status

### 5.6 Mini-mapa de Contexto

- Posicionado à direita do canvas principal
- Mostra uma área maior ao redor do tile atual (ex: 3×3 tiles) usando o TileServer-GL
- O tile atual é destacado com uma borda vermelha semitransparente
- Não é editável, serve apenas para referência visual
- Deve ser menor que o canvas principal (ex: 300×300 px)

### 5.7 Indicadores de Progresso no Editor

- Barra ou número mostrando "X/65536 pixels classificados (Y%)" no tile atual
- Mudar a cor do indicador quando atingir 100%
- Mostrar "Tile N de 1000" e "Seus tiles hoje: X" no header

### 5.8 Fluxo de Submissão

1. Operador clica "Submeter"
2. Frontend verifica se todos os 65536 pixels foram classificados (nenhum com valor 255)
3. Se houver pixels não preenchidos: mostrar alerta com a contagem e destacar os pixels faltantes no canvas (piscar os pixels 255 em vermelho por 2 segundos)
4. Se completo: enviar o Uint8Array para o backend via POST
5. Backend valida novamente, salva como PNG, registra no log
6. Frontend automaticamente carrega o próximo tile (sem interação extra)
7. Se não houver mais tiles: mostrar mensagem "Todos os tiles foram processados!"

### 5.9 Fluxo de Revisão

Quando o operador recebe um tile para revisão (status `in_review`):

1. O editor carrega normalmente, mas com a classificação anterior já pintada
2. Um banner visível indica "MODO REVISÃO - Classificado por [username]"
3. O revisor pode editar a classificação livremente (corrigir erros)
4. Ao submeter, o tile vai para status "reviewed"
5. O revisor também pode reportar problema

### 5.10 Fluxo de Reportar Problema

1. Operador clica "Reportar Problema"
2. Abre um modal com campo de texto para descrever o problema (ex: "imagem com nuvens", "tile fora da área de interesse", "artefato na imagem")
3. Ao confirmar: o tile vai para status "problem", a nota é salva, o operador recebe o próximo tile
4. Qualquer classificação parcial feita até o momento é descartada (volta para 255)

---

## 6. Frontend - Painel Admin

### 6.1 Dashboard de Progresso

- Total de tiles por status (pending, in_progress, classified, in_review, reviewed, problem) com gráfico de barras ou pizza
- Percentual geral de conclusão (reviewed / total)
- Gráfico de evolução diária (tiles concluídos por dia)
- Tiles por operador (tabela: nome, classificados, revisados, problemas reportados, média de tempo por tile)
- Estimativa de conclusão baseada no ritmo atual

### 6.2 Lista de Tiles com Problema

- Tabela com: id do tile, quem reportou, quando, nota do problema
- Para cada tile: botão "Resetar" (limpa a classificação, volta para pending) e botão "Visualizar" (abre a imagem de satélite do tile para o admin avaliar)

### 6.3 Gestão de Tiles

- Filtro por status, por operador, por data
- Visualização em lista e em grade (thumbnails mostrando a classificação)
- Ações em lote: selecionar múltiplos tiles e resetar ou mandar para revisão
- Ao clicar num tile: ver detalhes (quem classificou, quem revisou, histórico de ações, preview da classificação sobre a imagem)

### 6.4 Gestão de Usuários

- Criar novos usuários (username, senha temporária, role)
- Desativar usuários
- Ver estatísticas individuais

---

## 7. Atalhos de Teclado (Referência Completa)

| Tecla            | Ação                                   |
|-----------------|-----------------------------------------|
| 1-6             | Selecionar classe                       |
| E               | Alternar para borracha                  |
| B               | Alternar para brush                     |
| G               | Alternar para flood fill (se implementado) |
| +/-             | Aumentar/diminuir tamanho do brush      |
| Ctrl+Z          | Undo                                    |
| Ctrl+Shift+Z    | Redo                                    |
| Space (segurar) | Esconder máscara temporariamente        |
| O               | Alternar modo só contornos              |
| [ / ]           | Diminuir/aumentar opacidade da máscara  |
| Ctrl+S          | Submeter tile (prevenir save do browser)|

Todos os atalhos devem ser desabilitados quando um modal ou campo de texto estiver ativo.

---

## 8. Arquivo de Configuração

O backend deve carregar configurações de um arquivo `config.yaml` (ou `config.json`):

```yaml
tileserver:
  url_template: "http://192.168.1.100:8080/styles/satellite/{z}/{x}/{y}.png"

classes:
  - id: 1
    name: "Classe A"
    color: "#FF3B30"
  - id: 2
    name: "Classe B"
    color: "#007AFF"
  - id: 3
    name: "Classe C"
    color: "#FFCC00"
  - id: 4
    name: "Classe D"
    color: "#FF2D92"
  - id: 5
    name: "Classe E"
    color: "#00CED1"
  - id: 6
    name: "Classe F"
    color: "#FF9500"

tiles:
  source: "tiles_index.csv"  # CSV com id, bbox_west, bbox_south, bbox_east, bbox_north, zoom, tile_x, tile_y

auth:
  jwt_secret: "trocar-em-producao"
  token_expiry_hours: 8

database:
  path: "tileclass.db"
```

---

## 9. Inicialização e Setup

### 9.1 Script de Importação de Tiles

Um script CLI (`import_tiles.py`) que:

1. Lê o CSV de índice de tiles
2. Cria registros na tabela `tiles` com status "pending"
3. Inicializa o campo `data_png` com um PNG 256×256 todo preenchido com valor 255
4. Reporta quantos tiles foram importados

### 9.2 Script de Criação de Admin

Um script CLI (`create_admin.py`) que cria o primeiro usuário admin.

### 9.3 Script de Exportação

Um script CLI (`export_tiles.py`) que:

1. Lê todos os tiles com status "reviewed" do banco
2. Exporta cada um como GeoTIFF de banda única com georreferência correta (a partir do bbox)
3. Opcionalmente, cria um mosaico único com gdal_merge ou rasterio

---

## 10. Cuidados Técnicos

### 10.1 Concorrência

- O endpoint `/api/tiles/next` deve usar transação com lock para evitar que dois operadores recebam o mesmo tile ao mesmo tempo
- SQLite suporta WAL mode, ativar para melhor performance com leituras concorrentes
- Com até 10 operadores, SQLite é suficiente se as escritas forem serializadas via transaction

### 10.2 Performance do Canvas

- Não redesenhar o canvas inteiro a cada evento de mouse. Apenas redesenhar a região afetada pelo brush (dirty rectangle)
- O array de classificação (Uint8Array 65536) é a fonte de verdade. O canvas é só a visualização.
- Ao carregar um tile com classificação existente (revisão), decodificar o PNG recebido para preencher o Uint8Array, depois renderizar
- A imagem de satélite de fundo é carregada uma vez e não muda durante a edição do tile

### 10.3 Mapeamento de Coordenadas

O ponto mais crítico de implementação é o mapeamento correto entre:

- Posição do mouse na tela (pixels CSS)
- Posição no canvas (pixels do canvas, atenção ao devicePixelRatio)
- Coordenada lógica no tile (0-255, 0-255)

Usar `Math.floor()` para converter coordenada de canvas para coordenada lógica. Testar que pintar no canto inferior-direito do canvas resulta na coordenada (255, 255) e não (256, 256) ou (254, 254).

### 10.4 Integridade dos Dados

- Antes de salvar, validar que o Uint8Array recebido tem exatamente 65536 bytes
- Validar que todos os valores estão no range permitido (1-6 para classes, 255 não deveria chegar no submit mas validar mesmo assim)
- O PNG armazenado no SQLite deve ser de banda única (grayscale), 8 bits, 256×256, sem compressão com perda
- Ao recarregar um tile (revisão ou retomada), decodificar o PNG e verificar que resulta em 65536 pixels

### 10.5 Segurança

- Senhas armazenadas com bcrypt (custo mínimo 12)
- JWT assinado com HS256 e secret do config
- Endpoints admin protegidos por verificação de role no middleware
- Rate limiting no endpoint de login (máximo 5 tentativas por minuto por IP)
- Validar que o operador que submete um tile é o mesmo que foi atribuído a ele

### 10.6 Encoding do PNG no SQLite

Usar a biblioteca Pillow (PIL) no backend:

- Para salvar: converter Uint8Array para imagem PIL mode "L" (grayscale), salvar como PNG em BytesIO, gravar o bytes no campo BLOB
- Para carregar: ler BLOB, abrir com PIL, converter para array numpy, serializar como bytes para enviar ao frontend
- O frontend envia raw bytes (Uint8Array), não PNG. A conversão para PNG é responsabilidade do backend.

---

## 11. Critérios de Aceitação

### 11.1 Autenticação

- [ ] Operador consegue fazer login com username e senha
- [ ] Token JWT é renovado automaticamente antes de expirar durante o uso
- [ ] Operador é redirecionado para login se token expirar
- [ ] Admin e operador veem interfaces diferentes após login

### 11.2 Fila de Tiles

- [ ] Ao clicar "Próximo tile", o operador recebe um tile que ninguém mais está editando
- [ ] Dois operadores nunca recebem o mesmo tile simultaneamente
- [ ] Tiles de revisão têm prioridade sobre tiles novos
- [ ] Operador nunca recebe para revisão um tile que ele mesmo classificou
- [ ] Quando não há mais tiles disponíveis, o sistema informa claramente

### 11.3 Editor de Classificação

- [ ] A imagem de satélite carrega corretamente como fundo via TileServer-GL
- [ ] O operador consegue pintar com brush em todos os 6 classes
- [ ] O tamanho do brush é ajustável e o cursor reflete o tamanho atual
- [ ] A borracha remove classificação (volta para 255 / transparente)
- [ ] Undo desfaz a última ação de pintura completa (mousedown-mouseup)
- [ ] Redo refaz a ação desfeita
- [ ] O slider de opacidade ajusta a transparência da máscara de classificação em tempo real
- [ ] Segurar Space esconde a máscara completamente e soltar restaura
- [ ] Os atalhos de teclado funcionam conforme documentado
- [ ] O indicador mostra quantos pixels foram classificados
- [ ] Não é possível submeter com pixels não preenchidos (valor 255)
- [ ] Pixels não preenchidos são destacados visualmente ao tentar submeter incompleto
- [ ] Ao submeter com sucesso, o próximo tile carrega automaticamente
- [ ] O mini-mapa de contexto mostra a área ao redor do tile atual

### 11.4 Revisão

- [ ] O editor carrega a classificação anterior para edição
- [ ] Banner de revisão é visível e mostra quem classificou
- [ ] Revisor pode editar e submeter a revisão
- [ ] Revisor pode reportar problema

### 11.5 Reportar Problema

- [ ] Modal abre com campo de texto para descrição
- [ ] Ao confirmar, tile vai para status "problem" e operador recebe próximo tile
- [ ] Classificação parcial é descartada

### 11.6 Admin

- [ ] Dashboard mostra totais corretos por status
- [ ] Dashboard mostra gráfico de evolução diária
- [ ] Dashboard mostra estatísticas por operador
- [ ] Lista de problemas mostra todos os tiles com problema e suas notas
- [ ] Admin consegue resetar tile (volta pra pending com classificação limpa)
- [ ] Admin consegue mandar tile reviewed para nova revisão
- [ ] Admin consegue criar novos usuários
- [ ] Ações em lote funcionam (selecionar múltiplos, resetar)

### 11.7 Performance

- [ ] Pintar com brush não apresenta lag perceptível (< 16ms por frame)
- [ ] Carregar próximo tile leva menos de 2 segundos
- [ ] O sistema suporta 10 operadores simultâneos sem degradação

### 11.8 Dados

- [ ] O PNG armazenado no SQLite reconstrói corretamente o array de classificação
- [ ] O script de exportação gera GeoTIFFs georreferenciados corretos
- [ ] O log de ações registra toda atividade relevante com timestamps

---

## 12. Estrutura de Diretórios do Projeto

```
tileclass/
├── backend/
│   ├── main.py              # FastAPI app, rotas, startup
│   ├── auth.py              # JWT, login, middleware
│   ├── models.py            # Schemas Pydantic
│   ├── database.py          # Conexão SQLite, inicialização de tabelas
│   ├── tile_service.py      # Lógica de fila, atribuição, salvamento
│   ├── admin_service.py     # Lógica do painel admin
│   ├── config.yaml          # Configuração
│   └── scripts/
│       ├── import_tiles.py  # Importação do CSV
│       ├── create_admin.py  # Criação do admin inicial
│       └── export_tiles.py  # Exportação para GeoTIFF
├── frontend/
│   ├── index.html           # SPA principal
│   ├── css/
│   │   └── style.css
│   └── js/
│       ├── app.js           # Roteamento SPA, inicialização
│       ├── auth.js          # Login, gestão de token
│       ├── editor.js        # Canvas, ferramentas, pintura
│       ├── minimap.js       # Mini-mapa de contexto
│       ├── admin.js         # Painel admin
│       └── api.js           # Fetch wrapper com auth headers
├── requirements.txt
└── README.md
```

---

## 13. Dependências Python

```
fastapi
uvicorn
pyjwt
bcrypt
pillow
pyyaml
numpy
python-multipart
```

Não usar ORM. Usar sqlite3 nativo do Python com queries SQL diretas.

---

## 14. Notas de UX Adicionais

### 14.1 Produtividade

- O fluxo principal deve ser: abrir o browser, logar, e já estar pintando em menos de 5 segundos
- Transição entre tiles deve ser instantânea (pré-carregar o próximo tile enquanto o operador pinta o atual, se possível)
- Evitar modais e confirmações desnecessárias. A única confirmação deve ser no "Reportar Problema"
- O botão "Submeter" deve ser grande e acessível, posicionado de forma que não exija scroll

### 14.2 Feedback Visual

- Ao mudar de classe, dar feedback visual imediato (flash na borda do canvas ou mudança do cursor)
- Ao submeter com sucesso, mostrar um breve flash verde no canvas (200ms) antes de carregar o próximo tile
- Ao reportar problema, mostrar feedback de confirmação

### 14.3 Responsividade

- O layout deve funcionar em monitores 1920×1080 (mais comum) e 1366×768
- O canvas principal deve ser o maior possível, o mini-mapa e as ferramentas ocupam o espaço restante
- Não é necessário suportar mobile

### 14.4 Estado Local

- Se o operador recarregar a página (F5) enquanto edita um tile, ao logar novamente ele deve receber o mesmo tile que estava editando (não perder o trabalho se já não submeteu)
- Considerar salvar o estado atual do Uint8Array no localStorage como backup. Ao reconectar, comparar com o estado no servidor e oferecer restaurar.

### 14.5 Tecla de Atalho de Referência

- Incluir um botão "?" que abre um overlay mostrando todos os atalhos de teclado disponíveis
- Este overlay deve ser fechável com Escape ou clicando fora
