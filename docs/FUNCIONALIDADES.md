# TileClass — Compêndio Exaustivo de Funcionalidades

> Documento de referência interno. Lista **tudo** que a aplicação faz —
> via UI web (operador e admin) e via scripts CLI — compilado a partir de uma
> varredura completa do código (backend + frontend). Convenção: caminhos e
> identificadores de código verbatim; prosa em pt-BR.
>
> Aplicação web para anotação de tiles de satélite. **Backend** FastAPI + SQLite
> (sem ORM). **Frontend** Vanilla JS (sem framework) + Canvas HTML5 + MapLibre GL.
> Tudo é **por projeto**; um projeto tem um `kind` que define o tipo de anotação.

---

## Índice

1. [Visão geral e arquitetura](#1-visão-geral-e-arquitetura)
2. [Autenticação e segurança](#2-autenticação-e-segurança)
3. [Modelo de dados (tabelas)](#3-modelo-de-dados)
4. [Projetos e os 4 kinds](#4-projetos-e-os-4-kinds)
5. [Fluxo do operador (fila, submit, ciclo de vida)](#5-fluxo-do-operador)
6. [Máquina de estados do tile](#6-máquina-de-estados-do-tile)
7. [Editor web — operador](#7-editor-web--operador)
8. [Painel administrativo (web)](#8-painel-administrativo-web)
9. [Métricas do dashboard](#9-métricas-do-dashboard)
10. [Renderização, overlays e cache](#10-renderização-overlays-e-cache)
11. [Exportação de dados (UI + CLI)](#11-exportação-de-dados)
12. [Scripts CLI](#12-scripts-cli)
13. [Catálogo de endpoints HTTP](#13-catálogo-de-endpoints-http)
14. [Catálogo de códigos de erro](#14-catálogo-de-códigos-de-erro)
15. [Atalhos de teclado (consolidado)](#15-atalhos-de-teclado)

---

## 1. Visão geral e arquitetura

- **Três views SPA** (`frontend/index.html`): `#view-login`, `#view-editor`, `#view-admin`. Navegação por `classList.toggle("hidden")`.
- **Roteamento por papel** (`app.js`): operador/revisor → sempre o editor; admin → última view salva (`localStorage["tileclass_admin_last_view"]`), com botões para alternar entre painel admin e "trabalhar como operador".
- **4 tipos de projeto** (`projects.kind`): `raster`, `vector`, `classification`, `detection`. Cada um define corpo do tile, editor, validação, overlay e export próprios; compartilham o mesmo shell de UI e a mesma fila/máquina de estados.
- **Backend:** FastAPI; SQLite com `PRAGMA journal_mode=WAL` + `foreign_keys=ON`; transações explícitas (`transaction("IMMEDIATE")`). Sem ORM (SQL parametrizado).
- **Estático servido pelo FastAPI** (`main.py` monta `/static` e serve a SPA). Middleware de **CSP** libera `server.arcgisonline.com` e `*.virtualearth.net` (Bing).
- **Config** (`config.yaml`, gitignored): **somente infra** — `mask_overlay` (cache do overlay admin), `auth` (jwt_secret + TTLs), `database.path`. Nenhum dado de domínio (projetos/classes/layers/membros vivem no banco). Instalação nova começa **vazia**; o admin cria o 1º projeto pela UI.

---

## 2. Autenticação e segurança

### 2.1 Endpoints (`/api/auth`)

| Método | Path | Body | Resposta | Regras |
|---|---|---|---|---|
| POST | `/api/auth/login` | `{username, password}` | `{access_token, refresh_token, token_type:"bearer"}` | rate-limit por IP → `authenticate` → 401 `invalid credentials` |
| POST | `/api/auth/refresh` | `{refresh_token}` | novo par de tokens | valida `typ=="refresh"`; recarrega user do DB (pega role/desativação atuais); 401 `user inactive` se inativo |
| GET | `/api/auth/me` | (Bearer) | `{id, username, role}` | valida via `get_current_user` |
| POST | `/api/auth/logout` | (Bearer) | `{ok:true}` | blacklista o `jti` do token apresentado (revoga acesso) |

### 2.2 JWT, bcrypt, papéis

- **JWT** HS256. Access TTL 8h, refresh TTL 24h (config). Claims: `sub`, `typ` (`access`/`refresh`), `iat`, `exp`, `jti` (`secrets.token_urlsafe(12)`); access carrega `username`+`role`. Refresh **não** carrega role (re-lê do DB).
- **`_secret()` fail-closed:** recusa subir se `jwt_secret` é placeholder (`trocar-em-producao-...`, `""`, `changeme`, `secret`), salvo `TILECLASS_ALLOW_DEFAULT_SECRET=1` (só testes).
- **bcrypt** cost = 12.
- **Rate limit:** 5 tentativas / 60 s por IP, **persistido em SQLite** (`rate_limit`); cleanup amortizado (2%); desligável com `TILECLASS_DISABLE_RATE_LIMIT=1` (só E2E). `reset_rate_limits()` para testes.
- **Blacklist/revogação:** tabela `token_blacklist` (jti, user_id, expires_at) + cache em processo; `logout` insere o jti; `get_current_user` rejeita jti blacklistado (`401 token revoked`).
- **Papel global** (`users.role`): `operator` | `admin`. Flag global `users.can_review` (veto temporário de revisão). **Papel por projeto** (`project_members.role`): `operator` | `reviewer` | `admin` (tiers 0/1/2).
- **`get_current_user`** valida em ordem: token presente → assinatura/exp → `typ=="access"` → não-blacklistado → usuário existe e `active=1` → **role efetiva vem do DB** (token com role adulterado é ignorado — guard de escalação).
- **`require_admin`** → 403 `admin only`. **`require_membership(project_id, user, min_role)`**: admin global sempre passa; não-membro → 403 `not_project_member`; abaixo do tier → 403 `insufficient_project_role`.

---

## 3. Modelo de dados

Tabelas (em `backend/database.py` SCHEMA):

- **`users`** — `id`, `username` (unique), `password_hash`, `role` (CHECK operator/admin), `active` (def 1), `can_review` (def 0), `created_at`.
- **`projects`** — ver §4.2 (todas as colunas).
- **`project_classes`** — PK `(project_id, class_id)`; `class_id` CHECK 1..254; `name`, `color`, `ordering`. FK→projects ON DELETE CASCADE.
- **`project_attributes`** — PK `(project_id, key)`; `label`, `type` (CHECK text/number/enum/boolean), `required`, `options_json`, `ordering`. (Só vector.)
- **`project_members`** — PK `(project_id, user_id)`; `role` (CHECK operator/reviewer/admin). Índice por user.
- **`tiles`** — `id`, `project_id` (NOT NULL FK), `name`, `bbox_west/south/east/north`, `status` (def `pending`), `assigned_to`, `classified_by`, `reviewed_by`, `classified_at`, `reviewed_at`, `data_png` (corpo raster), `problem_note`, `version` (optimistic lock), `paused_at`, `blocked_from`, `class_counts` (JSON pixels/caixas por classe), `last_heartbeat_at`, `data_geojson` (vector/detection), `feature_count`, `data_class_id` (classification). Índices em status/assigned/by-fields/at-fields + `(project_id, status)`, `(project_id, assigned_to)`, parcial `paused_at`.
- **`action_log`** — `id`, `user_id`, `tile_id` (nullable), `action`, `detail`, `created_at`. Índices incl. composto `(user_id, tile_id, action)` (pareamento de durações).
- **`rate_limit`** — `ip`, `attempted_at`.
- **`token_blacklist`** — `jti` PK, `user_id`, `expires_at`.

### 3.1 Migrações (idempotentes, `_migrate`)

- **DB novo:** schema completo (`project_id NOT NULL`, 4 kinds no CHECK); fica **vazio** (admin cria o 1º projeto).
- **DB legado:** ALTERs faltantes em `tiles` (version, paused_at, blocked_from, class_counts, last_heartbeat_at, data_geojson, feature_count, data_class_id) e `projects` (kind, topology_required, tile_px, meters_per_pixel, box_required); `users.can_review` (admins viram can_review=1).
- **Widen do CHECK de kind** para incluir `detection`: rebuild da tabela `projects` por tabela-espelho com `foreign_keys=OFF` + re-check FK (idempotente).
- **`project_id NOT NULL`:** adiciona coluna nullable, cria projeto "default" com 6 classes históricas (`_MIGRATION_CLASSES`), backfill, rebuild da `tiles` para NOT NULL (FK-off + re-check), semeia membros (users ativos → role derivada de role/can_review). Resumível após falha parcial.

---

## 4. Projetos e os 4 kinds

### 4.1 Os 4 kinds

| kind | Anotação | Corpo do tile | Schema de domínio | Flag específica | Editor | Export |
|---|---|---|---|---|---|---|
| `raster` | máscara pixel-a-pixel | `data_png` (PNG L 8-bit, tile_px²) | `classes` | `mask_complete_required` | canvas (mask-core) | GeoTIFF |
| `vector` | LineStrings com atributos | `data_geojson` + `feature_count` | `attributes` | `topology_required` | MapLibre (vector-core) | GeoJSON |
| `classification` | 1 classe para o tile inteiro | `data_class_id` (int) | `classes` | — | MapLibre + botões | CSV |
| `detection` | bounding boxes (1 classe/caixa) | `data_geojson` + `feature_count` + `class_counts` | `classes` | `box_required` | MapLibre desenhar retângulo | GeoJSON (Polygons) |

**Exclusão mútua:** raster/classification/detection usam `classes` (rejeitam `attributes` → 400 `attributes_not_supported`); vector usa `attributes` (rejeita `classes` → 400 `classes_on_vector`).

### 4.2 Campos do projeto

`id`, `name` (único), `description`, `kind` (imutável), `topology_required` (vector), `box_required` (detection), `tile_px` (1..4096, def 256), `meters_per_pixel` (>0, def 2.5), `mask_complete_required` (raster, def 1), `primary_mbtiles` (obrigatório), `secondary_mbtiles`, `tertiary_mbtiles`, `ref_mask_primary_mbtiles`, `ref_mask_secondary_mbtiles`, `active`, `created_by`, `created_at`. Derivado: `tile_meters = tile_px × meters_per_pixel`.

### 4.3 Layers (5 slots)

| Coluna | Layer (frontend) | Atalho (hold) | Obrigatório |
|---|---|---|---|
| `primary_mbtiles` | `primary` | — (fundo) | **sim** |
| `secondary_mbtiles` | `secondary` | `D` | não |
| `tertiary_mbtiles` | `tertiary` | `R` | não |
| `ref_mask_primary_mbtiles` | `ref_primary` | `T` | não |
| `ref_mask_secondary_mbtiles` | `ref_secondary` | `Y` | não |

Cada slot aceita **3 formatos** (`is_remote_layer` detecta): path `.mbtiles` (local, servido por `/api/projects/{id}/xyz/...`); URL `http(s)://…/{z}/{x}/{y}.ext` (tile-server remoto, MapLibre busca direto); `bingmaps://{z}/{x}/{y}` (Bing quadkey, reescrito no frontend). Validação exige placeholders `{z}/{x}/{y}` em URLs; mbtiles local precisa existir em disco.

### 4.4 Invariantes de projeto

- **`kind` imutável** após criação (fora do allow-list do PATCH).
- **Geometria travada** após o 1º tile (`tile_px`/`meters_per_pixel` → 409 `tile_geometry_locked`).
- **Remover classe bloqueado** quando há tiles (409 `class_in_use`); renomear/recolorir/adicionar é livre.
- **Remover atributo bloqueado** quando em uso por alguma feature (409 `attribute_in_use`).
- **Soft-disable** (`active=False`): some das filas (`/next`/`/next-preview` → 409 `project_inactive`); dados preservados; reversível.
- **Hard-delete** só sem tiles (409 `project_has_tiles`); cascateia classes/attributes/members.
- **Clone:** copia config + classes + attributes; **não** copia membros; começa ativo.
- **Membership:** operador só vê tiles de projetos onde é membro; admin global vê todos.

---

## 5. Fluxo do operador

### 5.1 Fila de tiles — `GET /api/tiles/next`

Transação `BEGIN IMMEDIATE` única, ordem:
1. **Auto-pause sweep** (`_auto_pause_stale`): pausa tiles `in_progress`/`in_review` com `last_heartbeat_at` mais velho que 300s (log `pause` detail=`auto`).
2. **Resume (prioridade 1):** tile do próprio usuário em `in_progress`/`in_review` (escopado por projeto). Pause de sistema (`queue`/`auto`) → auto-resume; pause manual (`detail=NULL`) → mantém pausado. Ordem `_RESUME_ORDER_BY`: não-pausado > pause manual > pause de fila, FIFO por id.
3. **Fila de revisão (prioridade 2):** se pode revisar — `status='classified'` e `classified_by != user` (anti-self-review), FIFO por `classified_at`. → `in_review`, log `assign_review`.
4. **Fila pending (prioridade 3):** `status='pending'` FIFO por id → `in_progress`, log `assign_classify`.
5. Nada → `204`.

Atomicidade garante que dois operadores nunca recebem o mesmo tile. **Pode revisar** sse admin global, OU `can_review=1` E membro com role reviewer/admin.

### 5.2 Endpoints de fila/leitura

| Método | Path | Função |
|---|---|---|
| GET | `/api/tiles/next` | atribui o próximo (resume>review>pending); 204 vazio; gate `project_inactive` |
| GET | `/api/tiles/next-preview` | peek sem atribuir (pré-carga do editor) |
| GET | `/api/tiles/assigned` | tile em aberto do usuário (decide pular idle no login) |
| GET | `/api/tiles/queue-stats` | `{total, reviewed, classified}` |
| GET | `/api/me/stats-today` | `{count}` de classify+review do usuário hoje (UTC) |
| GET | `/api/tiles/{id}` | dict do tile (`filled_pixels`, usernames) |
| GET | `/api/tiles/{id}/history` | log de ações do tile |
| GET | `/api/tiles/{id}/review-note` | nota de "pedir ajustes" viva, ou 204 |
| GET | `/api/tiles/{id}/image` | PNG (só raster; 415 outros) |
| GET | `/api/tiles/{id}/features` | GeoJSON (vector/detection; 415 outros) |
| GET | `/api/tiles/{id}/classification` | `{class_id}` ou 204 (só classification; 415 outros) |
| GET | `/api/tiles/{id}/satellite-thumbnail?size=` | PNG satélite |

`?project_id=` resolve membership; quando o usuário tem 1 só projeto pode omitir (senão 400 `project_id_required`).

### 5.3 Submit (classify/review)

`classify` e `review` são o **mesmo endpoint** — a transição é decidida pelo **status** (`in_progress`→classify; `in_review`→review), não pelo path. Dispatch por kind via `project_for_tile`. Precondições compartilhadas (`_lock_tile_for_user`): tile existe; `X-Tile-Version` casa (optimistic lock → 409 `tile_modified`); `assigned_to == user` (→ 403).

| kind | body | validação | escreve |
|---|---|---|---|
| raster | raw `tile_px²` bytes (octet-stream) | `validate_submission` (tamanho, IDs em paleta+255, completude se estrito) | `data_png`, `class_counts`, autor/data |
| vector | JSON FeatureCollection LineStrings (≤1.5MB) | `vector_utils.validate_submission` (estrutura, atributos, topologia opcional) | `data_geojson`, `feature_count` |
| detection | JSON FeatureCollection Polygons (≤1.5MB) | `detection_utils.validate_submission` (retângulo eixos-alinhados, class_id na paleta, box_required) | `data_geojson`, `feature_count`, `class_counts` |
| classification | JSON `{"class_id":int}` (≤1KB) | `classify_utils.parse_class_id` (int na paleta) | `data_class_id` |

Em sucesso: `assigned_to=NULL`, `version+1`, log `classify`/`review`, invalida cache de overlay (pós-commit).

### 5.4 Outras operações do tile

- **Pause** (`POST /pause`): salva corpo parcial e congela timer. Raster valida só tamanho/valores; vector/detection só parse estrutural (atributos/topologia/box adiados); **classification → 409 `pause_not_supported`**. Log `pause` (detail NULL = manual).
- **Resume** (`POST /resume`): limpa `paused_at`. 409 se não-pausado / estado inválido.
- **Heartbeat** (`POST /heartbeat`): a cada 60s pelo editor; bump `last_heartbeat_at`. No-op `{ok:false, reason:"not_active"}` se não-ativo/pausado.
- **Report problem** (`POST /report-problem`): `status='problem'`, **limpa todos os corpos** (png/geojson/class_id/counts), nota (≤2000). Sai da fila.
- **Request changes** (`POST /request-changes`): revisor devolve à fila (`status='pending'`) **preservando o corpo** e `classified_by`; nota obrigatória. A nota fica "viva" só enquanto `classified_at` existe e é anterior à nota (reset do admin a aposenta).

---

## 6. Máquina de estados do tile

```
pending → in_progress → classified → in_review → reviewed
   ↑           │             │            │
   │           └──── report_problem ──────┴──→ problem
   │                                            │
   └────────────── reset (admin) ───────────────┘
reviewed → in_review/classified  (re-review, admin)
in_review → pending              (request_changes, revisor)
{pending|classified|reviewed} → blocked → (restaura blocked_from)  (block/unblock, admin)
```

- **Transições do operador/sistema:** `/next` (pending→in_progress, classified→in_review), submit (in_progress→classified, in_review→reviewed), report_problem (→problem), request_changes (in_review→pending), pause/resume (não muda status; seta/limpa `paused_at`).
- **Admin-only:** reset (qualquer→pending, limpa corpo), re-review (reviewed→classified/in_review), block/unblock (guarda `blocked_from`), unassign (in_progress→pending, in_review→classified, **preserva máscara**), admin-pause, delete (só `problem`).
- **`in_progress`/`in_review`/`problem` não podem ser bloqueados.** Tiles `blocked` saem das filas naturalmente.

---

## 7. Editor web — operador

### 7.1 Shell comum (todos os kinds)

- **Header:** mode-pill (`CLASSIFICAR` azul / `REVISAR` laranja), nome do tile, progresso da fila, banner de revisão ("Classificado por X"), **project-picker** (dropdown se >1 projeto; persiste em `localStorage`), contador "hoje", usuário, botões alternar painel / sair.
- **Footer:** Desfazer, Refazer, Reportar Problema, **Pedir ajustes** (só em review), Pausar, **Submeter** (estados: normal / `.incomplete` "Faltam N px" / "Aprovar revisão" / "Enviando...").
- **Telas:** idle ("Pronto para começar" / "Tile enviado ✓" / "Tile pausado ⏸" / "Ajuste solicitado ✓", com preview de thumbnail satélite e dica do próximo), paused-resume (retomar tile pausado), no-tiles ("Todos processados 🎉"), map-warning ("⚠ Satélite indisponível").
- **Modais:** confirmação de submit/pause, reportar problema (textarea ≤2000 com contador, Ctrl+Enter), pedir ajustes.
- **Heartbeat** 60s; **backup** do trabalho em `localStorage` (restaura no F5 com toast "Trabalho local restaurado").
- **api.js:** anexa Bearer; refresh proativo (60s antes do exp) + reativo no 401 com coalescing; aviso de sessão 5min antes do refresh expirar; timeouts (15s; login 10s).

### 7.2 Editor raster (`editor.js` + `mask-core.js`)

- **Stack de 3 camadas:** MapLibre satélite (fundo, contexto 7×7 tiles), canvas da máscara (`globalAlpha`), canvas de cursor (recebe eventos). Grade a cada 2.5 m.
- **Ferramentas:** Pincel (Q), Borracha (W, pinta 255), Balde/flood (E). Tamanhos `[1,3,5,7,11]` (A/S).
- **Pintura:** `paintAt` (pincel quadrado), `paintLine` (Bresenham sem buracos), `floodFill` (4-way). Fonte de verdade = `Uint8Array(tile_px²)`; canvas é só visualização.
- **Undo/redo:** 50 frames, armazena **só pixels alterados** (Uint32 positions + Uint8 prevValues). Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z.
- **Zoom/pan:** roda = zoom no cursor; Ctrl+roda = opacidade; botão direito arrasta (pan); botões +/−/reset.
- **Overlays hold:** D (secondary), R (tertiary), T (ref_primary), Y (ref_secondary) — T/Y escondem a máscara do operador. Space (hold) esconde a máscara.
- **Faltantes:** linha "⚠ Faltam N pixels", barra de progresso, C (pular para próximo faltante), F (highlight magenta toggle), flash vermelho ao submeter incompleto.
- **Atalhos de classe:** 1–6 (mapeado para `classes[N-1].id`).
- **Extras:** botões Google Maps / Google Earth (abrem o centro do tile).

### 7.3 Editor vetorial (`editor-vector.js` + `vector-core.js`)

- Mapa MapLibre interativo + painel de atributos. Ferramentas: **Caneta (P)** (clique adiciona vértice com snap a endpoint; duplo-clique encerra; ESC cancela) e **Selecionar (V)** (clique → painel de atributos; Del remove).
- **Painel schema-driven:** campos text/number/enum(select)/boolean(checkbox); required com `*`; campo "direção" (forward→/reverse←/both↔) quando `topology_required`.
- **Validação de topologia** (opcional): cada linha precisa de `direction`; endpoints próximos (~1m) snapam ao mesmo nó; ciclos rejeitados (DFS); validação por tile.
- Undo/redo (Ctrl+Z/Y); camadas committed/selected/draft/handles.

### 7.4 Editor de classificação (`editor-classification.js`)

- Mapa satélite + um botão por classe (cor + nome). Clique → highlight; Submeter (rodapé) → modal → POST. **Single-click, sem undo/redo, sem pause.**

### 7.5 Editor de detecção (`editor-detection.js` + `detection-core.js`)

- Mapa MapLibre + picker de classe. Ferramentas: **Desenhar (B)** (arrastar cria retângulo da classe ativa; pan desabilitado) e **Selecionar (V)** (clique = topmost; trocar classe; Del remove). ESC cancela desenho. Undo/redo (Ctrl+Z/Y).
- Caixas coloridas por `class_id` (fill 0.18 + outline). `box_required` espelhado na validação cliente.

---

## 8. Painel administrativo (web)

7 abas (`<nav class="admin-nav">`): **Dashboard, Projetos, Tiles, Mapa, Problemas, Usuários, Manutenção**. Todo endpoint admin exige role admin; muitos aceitam `?project_id=` para escopar.

### 8.1 Aba Tiles

- **Filtros:** busca (id ou nome, debounce 250ms), status (incl. os **virtuais de UI** `paused` e `paused_review`, traduzidos client-side para o query param `paused` — o backend não tem esses status, só o enum real + a flag `paused`), data de/até. Modos **Tabela** e **Grade** (thumbnails). Paginação `PAGE_SIZE=100` (header `X-Total-Count`). Ordenação por id/nome/status/classified_by/reviewed_by/classified_at/reviewed_at.
- **Ações por tile:** Ver (viewer), Re-revisar (reviewed), Atribuir (pending/classified), Pausar (admin-pause), Liberar operador (unassign), Bloquear/Desbloquear, Resetar, Excluir.
- **Bulk** (barra ao selecionar): Atribuir a operador, Liberar, Resetar, Re-revisar, Reportar problema (motivo obrigatório), Bloquear/Desbloquear.
- **Viewer modal:** status, ações contextuais, histórico de ações, e stack visual (MapLibre satélite + canvas 512² com a máscara colorizada).

### 8.2 Aba Mapa

- MapLibre (basemap Esri) com polígono+ponto por tile (cor por status). **Seleção retangular** (arrastar; Shift add, Alt remove, Esc sai). Toggles: satélite primário, "Mostrar classificações" (overlay `/api/admin/mask-tiles/...` zoom 8–18 com legenda). Legenda de status clicável (filtra). **Bulk no mapa:** Atribuir, Liberar, Re-revisar, Bloquear, Desbloquear (recolor otimista). Clique em tile → painel lateral (reusa o viewer).

### 8.3 Aba Problemas

Lista tiles `problem` (id, nome, nota, reportado em). Ações: Ver, Resetar, Excluir.

### 8.4 Aba Usuários

- Form criar usuário (username, senha ≥6, role). Tabela com toggles: Ativar/Desativar, Permitir/Revogar revisão (não-admin), Tornar admin/operador. **Guard do último admin** (não demover o último admin ativo via mudança de role).

### 8.5 Aba Projetos

- Lista com chip de tipo (matricial/vetorial/classificação/detecção). **+ Novo projeto** (form com kind selector imutável, flags por kind, geometria tile_px×mpp com label "tile no chão" ao vivo, 5 layers, classes ou atributos iniciais). **Editar** (config, geometria travada após 1º tile, ativo, layers, classes/atributos, membros). **Clonar**, **Excluir**.
- **Membros:** adicionar (id + role), trocar role inline, remover.
- **Seção Exportar dados:** label de formato por kind; select de status (Somente revisados / Somente classificados / Revisados + classificados); botão **Baixar export (ZIP)**.

### 8.6 Aba Manutenção

- **Imagens (MBTiles):** estado de cada layer (aberto/não-configurado/remoto/corrompido, formato, zooms, path).
- **Cache do overlay:** estatísticas (tiles renderizados, vazios, tamanho em disco, zooms, arquivo) + botões **Limpar cache** e **Atualizar**.

---

## 9. Métricas do dashboard

`GET /api/admin/dashboard` (escopável por `project_id`) retorna:

| Métrica | Cálculo |
|---|---|
| `totals_by_status` | COUNT por status |
| `total_tiles` | soma |
| `completion_percent` | 100·(classified+in_review+reviewed)/total |
| `daily_completed` | tiles revisados por dia (últimos 30) |
| `per_operator[]` | por usuário: classified, reviewed, problems, avg_classify_seconds, avg_review_seconds, avg_seconds_per_tile |
| `avg_classify_seconds` / `avg_review_seconds` | média global das durações de ciclo |
| `rate_per_day` | tiles classificados nos últimos 7 dias ÷ 7 |
| `eta_days` | remaining ÷ rate_per_day (ou `null`) |
| `paused_count` | tiles com `paused_at` |
| `paused_by_status` | pausados por status |

**Durações de ciclo** (`_cycle_durations`): pareia `assign_classify→classify` e `assign_review→review` via `MAX` por `(user,tile)` no `action_log`; **subtrai** intervalos `pause→resume` dentro do ciclo (`julianday·86400`). Cards extras no front: Em andamento (ativos = in_progress/in_review menos pausados), Bloqueados, Problemas.

**Distribuições** (endpoints separados):
- `class-distribution` (raster): pixels por classe (lê `class_counts`); detection também alimenta (contagem de caixas).
- `feature-distribution` (vector): ocorrências por valor de atributo enum/boolean.
- `tile-class-distribution` (classification): tiles por classe.

---

## 10. Renderização, overlays e cache

- **Overlay admin** (`mask_tile_service`): renderiza overlays coloridos em Web Mercator, cacheado em MBTiles **por projeto** (`<base>_p<id>.mbtiles`). Despacha por kind: raster (reprojeção NN das máscaras, last-write-wins), vector (linhas 2px coloridas por direção/enum), classification (retângulo translúcido + nome), detection (retângulos por classe). Só `classified`/`in_review`/`reviewed` aparecem. Endpoint `GET /api/admin/mask-tiles/{project_id}/{z}/{x}/{y}.png` (zoom validado contra min/max). Invalidação best-effort pós-commit; `clear_cache()`; `cache_stats()`.
- **Thumbnails** (`thumbnails.py`): `tile_thumbnail` despacha por kind (raster colorido via LUT ou satélite se máscara vazia; vector com linhas; classification com borda+nome). `tile_satellite_thumbnail` sempre satélite (compõe do primary mbtiles).
- **Pool de readers** (`mbtiles_service`): LRU de até 32 readers por `(project_id, layer)`, conexão por thread, read-only/immutable. URLs remotas não consomem slots. `invalidate_project`, `close_all`, prewarm no lifespan.
- **Geometria** (`geo.py`): `bbox_from_center` (pyproj.Geod WGS84, tile uniforme em qualquer latitude), `offset_center` (adjacência sem gap). `tile_grid.py`: helpers Web Mercator para builders/import.

---

## 11. Exportação de dados

Formato segue o **kind**: raster→GeoTIFF, vector/detection→GeoJSON, classification→CSV. Sempre acompanha um **manifest**. Filtros de status: `reviewed` (só revisados), `classified` (só classificados, não revisados), `reviewed+classified` (ambos).

### 11.1 Pela UI (admin)

`GET /api/admin/projects/{id}/export?status=reviewed|classified|reviewed_classified` (admin-only) → despacha por kind via `export_service`, gera num tempdir, **zipa**, faz stream como download (`Content-Disposition`, header `X-Tile-Count`). Painel "Exportar dados" no detalhe do projeto.

### 11.2 Via CLI (agentes)

Quatro exportadores, todos com `--status reviewed|classified|reviewed+classified` e `--project <id|name>` (omitir = todos do kind). Núcleo compartilhado `run(out_dir, *, status, project_id, **opts)`; `main()` é wrapper fino:
- `export_tiles` (raster→GeoTIFF EPSG:4326 single-band uint8 NODATA=255 deflate; `--raw` mantém IDs 1..6 sem remap EDGV; `--mosaic`).
- `export_features` (vector→GeoJSON; `--mosaic`).
- `export_classifications` (classification→CSV único).
- `export_detections` (detection→GeoJSON de caixas; `--mosaic`).

**Remap EDGV** (raster, default; `--raw` desativa): TileClass 1→0 (água), 2→1 (edif), 3→4 (floresta), 4→3 (campo), 5→5 (cultivo), 6→2 (terreno exposto), 255→255.

---

## 12. Scripts CLI

Rodar como módulo (`python -m backend.scripts.X`). Helpers em `_common.py` (`resolve_project_arg`, `load_tile_geometry`, `insert_tile_dedup`).

| Script | Propósito | Flags principais |
|---|---|---|
| `create_admin` | cria admin inicial (interativo) | — (prompts username/senha) |
| `import_points` | importa tiles de centros lat,lon | `--point lat lon [nome]` / `--csv` / `--block N` / `--project` |
| `export_tiles` | export raster → GeoTIFF | `--status` / `--raw` / `--mosaic` / `--manifest` / `--project` |
| `export_features` | export vector → GeoJSON | `--status` / `--mosaic` / `--manifest` / `--project` |
| `export_classifications` | export classification → CSV | `--status` / `--manifest` / `--project` |
| `export_detections` | export detection → GeoJSON | `--status` / `--mosaic` / `--manifest` / `--project` |
| `build_mbtiles` | GeoTIFFs 3857 → MBTiles WebP | `--zmin/--zmax/--quality/--workers/--batch-size/--name/--resume` |
| `merge_db` | mescla outro tileclass.db | `--primary/--secondary/--dry-run` (backup automático) |
| `backup_db` | backup WAL-safe do banco | `output` ou `--auto-name <dir>/` |
| `verify_db` | varredura de integridade (cron/CI) | `--quick` (exit 0 ok / 2 falha) |
| `recompute_class_counts` | backfill de `class_counts` | `--force` / `--project` |
| `recolor_mbtiles` | recoloriza MBTiles PNG por RGB | `--map AABBCC:XXYYZZ` (repetível) / `--workers/--batch-size` |

---

## 13. Catálogo de endpoints HTTP

**Auth** (`/api/auth`): `POST /login`, `POST /refresh`, `GET /me`, `POST /logout`.

**Health:** `GET /api/health`.

**Operador** (`/api`): `GET /tiles/next`, `/tiles/next-preview`, `/tiles/assigned`, `/tiles/queue-stats`, `/me/stats-today`, `/tiles/{id}`, `/tiles/{id}/history`, `/tiles/{id}/review-note`, `/tiles/{id}/image`, `/tiles/{id}/features`, `/tiles/{id}/classification`, `/tiles/{id}/satellite-thumbnail`; `POST /tiles/{id}/classify`, `/review`, `/report-problem`, `/request-changes`, `/pause`, `/resume`, `/heartbeat`.

**Projetos** (`/api/projects`): `GET ""`, `GET /{id}`, `GET /{id}/xyz/{layer}/{z}/{x}/{y}.{ext}`.

**Admin projetos** (`/api/admin/projects`): `POST ""`, `PATCH /{id}`, `DELETE /{id}`, `GET /{id}/export`, `POST /{id}/clone`, `PUT /{id}/classes`, `PUT /{id}/attributes`, `GET/POST /{id}/members`, `DELETE /{id}/members/{uid}`.

**Admin geral** (`/api/admin`, todos require_admin): `GET /dashboard`, `/class-distribution`, `/feature-distribution`, `/tile-class-distribution`, `/tiles` (+ `X-Total-Count`), `/tiles/{id}/thumbnail`, `/tiles/{id}/satellite-thumbnail`, `/tiles/problems`, `/tiles/map`, `/mask-tiles/{project_id}/{z}/{x}/{y}.png`; `POST /tiles/bulk/{reset,re-review,report-problem,unassign,block,unblock}`, `/tiles/assign`, `/tiles/{id}/{reset,assign,unassign,admin-pause,re-review,block,unblock}`, `DELETE /tiles/{id}`; `GET/POST /users`, `PATCH /users/{id}/{active,can-review,role}`; `GET /maintenance/overview`, `POST /maintenance/overlay-cache/clear`.

---

## 14. Catálogo de códigos de erro

**Auth (texto):** `missing token`, `invalid token`, `not an access token`, `token revoked`, `user not found or inactive` (401); `admin only` (403); `invalid credentials` (401); `too many login attempts` (429); `invalid refresh`, `not a refresh token`, `user inactive` (401 refresh).

**Tiles/submit (estruturado):** `invalid_mask_size`, `invalid_mask`, `unfilled_pixels`, `mask_too_large`, `body_too_large`, `invalid_utf8`, `invalid_features`, `invalid_boxes`, `invalid_class`, `invalid_geojson`, `tile_modified` (409 optimistic lock), `pause_not_supported`, `empty_note`, `wrong_kind` (415), `project_id_required`, `project_inactive`; estados ilegais → 409 texto ("invalid state for submit/pause/request_changes", "cannot pause/resume from state").

**Projetos:** 400 `invalid_name/invalid_kind/invalid_tile_px/invalid_meters_per_pixel/attributes_not_supported/no_classes/invalid_classes/classes_on_vector/missing_primary/missing_url_placeholders/mbtiles_not_found/unknown_fields/invalid_role/not_vector_project/invalid_attribute_*/enum_needs_options/invalid_status`; 403 `not_project_member/insufficient_project_role`; 404 `project_not_found/user_not_found`; 409 `name_taken/tile_geometry_locked/class_in_use/attribute_in_use/project_has_tiles`.

---

## 15. Atalhos de teclado

**Editor raster:** `1`–`6` classe; `Q`/`W`/`E` pincel/borracha/balde; `A`/`S` tamanho −/+; `Z`/`X` opacidade −/+; `C` próximo faltante; `F` highlight faltantes; `D`/`R`/`T`/`Y` (hold) overlays; `Space` (hold) esconde máscara; `Ctrl+Z` desfazer; `Ctrl+Y`/`Ctrl+Shift+Z` refazer; roda zoom; `Ctrl`+roda opacidade; botão direito arrasta.

**Editor vetorial:** `P` caneta; `V` selecionar; `Esc` cancela; `Del`/`Backspace` remove; `Ctrl+Z`/`Ctrl+Y` undo/redo; clique adiciona vértice; duplo-clique encerra.

**Editor detecção:** `B` desenhar; `V` selecionar; `Esc` cancela; `Del`/`Backspace` remove; `Ctrl+Z`/`Ctrl+Y` undo/redo; arrastar cria caixa; clique seleciona.

**Editor classificação:** sem atalhos (clique nos botões de classe + Submeter).

**Geral (modais):** `Ctrl+Enter` confirma textarea (problema/ajustes); `Esc` fecha modal.

---

*Gerado a partir de varredura completa do código-fonte (backend + frontend). Para o contrato detalhado de invariantes, ver `CLAUDE.md` e `docs/sistema.md`.*
