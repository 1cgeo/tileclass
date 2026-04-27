# TileClass — Especificação

Spec de referência. Contratos de API, modelo de dados e invariantes; detalhes
de UI/UX vivem no código e em `CLAUDE.md`.

## 1. Visão Geral

Aplicação web para classificação pixel-a-pixel de tiles de imagens de satélite.
Operadores recebem tiles 256×256 (resolução 2.5 m/pixel = 640 m × 640 m no
chão) e atribuem uma das 6 classes a cada pixel. O sistema gerencia a fila de
trabalho, revisão por pares e acompanhamento de progresso.

**Stack:** Python 3.11+ (FastAPI, sqlite3 nativo — sem ORM, Pillow, NumPy,
PyJWT, bcrypt, PyYAML, pyproj, rasterio) + Vanilla JS (sem framework, Canvas
HTML5, MapLibre GL JS).

**Escopo alvo:** ~1000 tiles, até 10 operadores simultâneos, intranet.

## 2. Arquitetura

- API REST (FastAPI) servindo o frontend estático.
- SQLite WAL como banco único; máscaras como PNG single-band (BLOB).
- Imagem de fundo via tiles XYZ (MBTiles local servido por
  `/api/xyz/{z}/{x}/{y}.{ext}` ou TileServer-GL externo). Overlays opcionais:
  ArcGIS World Imagery (secundário/terciário), WorldCover, MapBiomas.
- JWT (access 8h, refresh 24h, HS256). Role `operator|admin` + flag
  `can_review`.

## 3. Modelo de Dados (SQLite)

### 3.1 `users`

| Coluna          | Tipo    | Notas                                 |
|-----------------|---------|---------------------------------------|
| id              | INTEGER | PK                                    |
| username        | TEXT    | Único                                 |
| password_hash   | TEXT    | bcrypt (cost ≥ 12)                    |
| role            | TEXT    | `operator` ou `admin`                 |
| active          | INTEGER | 0/1; default 1                        |
| can_review      | INTEGER | 0/1; admins recebem 1 automático      |
| created_at      | TEXT    | ISO 8601                              |

### 3.2 `tiles`

| Coluna         | Tipo    | Notas                                                    |
|----------------|---------|----------------------------------------------------------|
| id             | INTEGER | PK                                                       |
| name           | TEXT    | Identificador legível                                    |
| bbox_west      | REAL    | Longitude oeste (graus)                                  |
| bbox_south     | REAL    | Latitude sul                                             |
| bbox_east      | REAL    | Longitude leste                                          |
| bbox_north     | REAL    | Latitude norte                                           |
| status         | TEXT    | Ver 3.4                                                  |
| assigned_to    | INTEGER | FK users.id                                              |
| classified_by  | INTEGER | FK users.id                                              |
| reviewed_by    | INTEGER | FK users.id                                              |
| classified_at  | TEXT    | ISO 8601                                                 |
| reviewed_at    | TEXT    | ISO 8601                                                 |
| data_png       | BLOB    | PNG single-band 256×256, valores `1..6` e `255`          |
| problem_note   | TEXT    | Nota livre quando `status='problem'`                     |
| version        | INTEGER | Incrementado a cada mutação; usado em CAS                |
| paused_at      | TEXT    | ISO 8601 quando o tile está pausado                      |
| blocked_from   | TEXT    | Status original guardado durante `status='blocked'`      |

**Geometria do tile.** Cada tile é definido pelo **centro geodésico**.
`backend/geo.bbox_from_center(lat, lon)` calcula ±320 m em cada direção
cardeal usando `pyproj.Geod` (WGS84). Pixel = 2.5 m em qualquer latitude.
Não há `zoom`/`tile_x`/`tile_y` no schema — composição XYZ é resolvida no
frontend pelo MapLibre a partir da bbox.

### 3.3 `action_log`

| Coluna     | Tipo    | Notas                                                 |
|------------|---------|-------------------------------------------------------|
| id         | INTEGER | PK                                                    |
| user_id    | INTEGER | FK users.id                                           |
| tile_id    | INTEGER | FK tiles.id (nullable)                                |
| action     | TEXT    | Ver lista abaixo                                      |
| detail     | TEXT    | JSON livre                                            |
| created_at | TEXT    | ISO 8601                                              |

Ações principais: `assign_classify`, `assign_review`, `classify`, `review`,
`pause`, `resume`, `report_problem`, `reset`, `re_review`, `block`,
`unblock`, `assign`, `unassign`, `delete_tile`, `create_user`,
`set_user_active`, `set_user_role`, `set_user_can_review`.

Pareamento `assign_*→classify/review` em pares por `(user_id, tile_id)` é o
que alimenta as métricas de duração no dashboard.

### 3.4 Status do Tile

```
pending ──> in_progress ──> classified ──> in_review ──> reviewed
              │               │              │
              ▼               ▼              ▼
            problem        problem        problem
```

Estados:
- `pending`: nenhum operador atribuído.
- `in_progress`: operador classificando.
- `classified`: aguardando revisão.
- `in_review`: revisor atribuído.
- `reviewed`: aprovado.
- `problem`: classificador ou revisor reportou problema.
- `blocked`: admin removeu da fila temporariamente; o status original fica
  em `blocked_from`.

Transições de admin: `problem → pending` (reset), `reviewed → in_review`
(re-review), `pending|classified|reviewed → blocked` e
`blocked → <blocked_from>`. `in_progress`/`in_review`/`problem` **não**
podem ser bloqueados.

## 4. API REST

Todos os endpoints fora de `/api/auth/login` exigem `Authorization: Bearer
<access_token>`. `403` em rota admin para operador; `401` se token expirou
ou foi revogado.

### 4.1 Auth (`/api/auth`)

| Método | Path        | Descrição                                            |
|--------|-------------|------------------------------------------------------|
| POST   | `/login`    | `{username, password}` → `{access_token, refresh_token}`. Rate-limit 5/min/IP. |
| POST   | `/refresh`  | `{refresh_token}` → novo par.                        |
| GET    | `/me`       | Dados do usuário logado.                             |
| POST   | `/logout`   | Revoga `jti` do access (e do refresh, se enviado).   |

JWT carrega `sub` (user_id), `username`, `role`, `typ` (`access`/`refresh`),
`jti`, `exp`. Tipo errado em refresh → 401.

### 4.2 Operador (`/api/tiles`)

| Método | Path                         | Descrição                                                       |
|--------|------------------------------|-----------------------------------------------------------------|
| GET    | `/next`                      | Próximo tile (resume → revisão → pending). 204 se vazio.        |
| GET    | `/assigned`                  | Tile atualmente atribuído (para resume sem puxar fila).         |
| GET    | `/next-preview`              | Peek sem atribuir (pré-carga).                                  |
| GET    | `/queue-stats`               | Tamanhos de fila por status.                                    |
| GET    | `/{id}`                      | Metadados (`TileOut`).                                          |
| GET    | `/{id}/image`                | PNG da máscara atual.                                           |
| GET    | `/{id}/history`              | Linha do tempo de ações.                                        |
| POST   | `/{id}/classify`             | Body: 65536 bytes raw. Header opcional `X-Tile-Version`.        |
| POST   | `/{id}/review`               | Idem, valida que `assigned_to == user` e tile em `in_review`.   |
| POST   | `/{id}/report-problem`       | `{note}` → status `problem`.                                    |
| POST   | `/{id}/pause`                | Body: 65536 bytes (snapshot). Solta atribuição preservando trabalho. |
| POST   | `/{id}/resume`               | Reatribui um tile pausado pelo próprio usuário.                 |

| Método | Path                  | Descrição                                  |
|--------|-----------------------|--------------------------------------------|
| GET    | `/api/me/stats-today` | `{count}` — classificados hoje pelo user.  |

**Regras de `/next`:**
1. Resume: se o user tem tile em `in_progress`/`in_review`, devolve esse.
2. Tile pausado pelo próprio user (FIFO por `paused_at`).
3. Fila de revisão: `classified` cujo `classified_by != user`.
4. Fila pending.
5. 204 se nada.

Atribuição é transacional (`BEGIN IMMEDIATE` + `SELECT … LIMIT 1` + `UPDATE`)
— dois usuários nunca recebem o mesmo tile.

**Regras de classificação/revisão:**
- Body é **raw bytes** (`Uint8Array` de 65536 bytes), não PNG. Backend
  converte com Pillow.
- Validação: tamanho exato + valores em `{1..6, 255}`. Submit rejeita se
  houver `255`; resposta traz a contagem.
- Autorização: `assigned_to == current_user`.
- Concorrência otimista: header `X-Tile-Version`. Se diferente de
  `tiles.version`, retorna `409` com `error=tile_modified` — o frontend
  decide se sobrescreve.

### 4.3 Admin (`/api/admin`)

Todos atrás de `Depends(auth.require_admin)`.

| Método | Path                                   | Descrição                                              |
|--------|----------------------------------------|--------------------------------------------------------|
| GET    | `/dashboard`                           | Totais, completion%, daily, per-operator, médias de duração, paused. |
| GET    | `/tiles`                               | Lista com filtros `status`, `user_id`, `date_from/to`, `paused`, `q`, `limit`, `offset`. Header `X-Total-Count`. |
| GET    | `/tiles/map`                           | Lista enxuta para o mapa admin.                        |
| GET    | `/tiles/problems`                      | Atalho para `status=problem`.                          |
| GET    | `/tiles/{id}/thumbnail`                | PNG colorizado da máscara (param `size`).              |
| GET    | `/tiles/{id}/satellite-thumbnail`      | PNG do satélite recortado pela bbox.                   |
| GET    | `/mask-tiles/{z}/{x}/{y}.png`          | Overlay XYZ rasterizado on-the-fly + cache mbtiles.    |
| POST   | `/tiles/{id}/reset`                    | Limpa máscara, volta para `pending`.                   |
| POST   | `/tiles/{id}/re-review`                | `reviewed → in_review`.                                |
| POST   | `/tiles/{id}/assign`                   | Atribui a um user específico.                          |
| POST   | `/tiles/{id}/unassign`                 | Solta atribuição.                                      |
| POST   | `/tiles/{id}/admin-pause`              | Pausa em nome do operador ausente.                     |
| POST   | `/tiles/{id}/block`                    | Bloqueia (guarda status em `blocked_from`).            |
| POST   | `/tiles/{id}/unblock`                  | Restaura ao `blocked_from`.                            |
| DELETE | `/tiles/{id}`                          | Apaga tile; só permitido se já está em `problem`.      |
| POST   | `/tiles/bulk/{reset,re-review,report-problem,unassign,block,unblock}` | Ações em massa. |
| POST   | `/tiles/assign`                        | Pré-carga FIFO de fila pessoal (multi-tile).           |
| GET    | `/users`                               | Lista com stats.                                       |
| POST   | `/users`                               | Cria usuário.                                          |
| PATCH  | `/users/{id}/active`                   | Liga/desliga conta.                                    |
| PATCH  | `/users/{id}/role`                     | Promove/rebaixa.                                       |
| PATCH  | `/users/{id}/can-review`               | Habilita/desabilita revisão.                           |

### 4.4 Config (`/api/config`)

| Método | Path           | Descrição                                                  |
|--------|----------------|------------------------------------------------------------|
| GET    | `/classes`     | Lista das classes (id/nome/cor) do `config.yaml`.          |
| GET    | `/tileserver`  | URLs e zoom ranges (primário, secundário, terciário, WC, MB). |

### 4.5 MBTiles passthrough

| Método | Path                          | Descrição                                  |
|--------|-------------------------------|--------------------------------------------|
| GET    | `/api/xyz/{z}/{x}/{y}.{ext}`  | Tile do MBTiles primário.                  |
| GET    | `/api/wc/{z}/{x}/{y}.{ext}`   | WorldCover.                                |
| GET    | `/api/mb/{z}/{x}/{y}.{ext}`   | MapBiomas.                                 |

## 5. Frontend

Detalhes (layout, atalhos, comportamento de canvas, undo, backup local) vivem
em `CLAUDE.md` (seções "Regras críticas de frontend" e "UX — inegociáveis")
e no código (`frontend/js/`). Pontos contratuais:

- Três camadas sobrepostas: MapLibre (satélite, `interactive: false`),
  canvas da máscara (`globalAlpha`), canvas de cursor (recebe eventos).
- `Uint8Array(65536)` é a fonte de verdade da máscara. Canvas é só
  visualização.
- Atalho hold `Space` esconde a máscara (keydown/keyup, não toggle).
- Sem auto-avanço pós-submit: idle screen com "Tile enviado ✓"; o operador
  clica para puxar o próximo (respiro entre cartas).
- Backup do `Uint8Array` em `localStorage` durante edição; restore silencioso
  no F5 com toast "Trabalho local restaurado." (sem banner de confirmação).
- JWT refresh proativo (60s antes de expirar) + reativo no 401.

## 6. Configuração (`backend/config.yaml`)

```yaml
tileserver:
  url_template: "https://server.arcgisonline.com/.../{z}/{y}/{x}"
  mbtiles_path: "../data_external/tiles.mbtiles"   # opcional, override do url_template
tileserver_secondary:
  url_template: "https://server.arcgisonline.com/.../{z}/{y}/{x}"
  max_zoom: 19
tileserver_tertiary:
  url_template: "bingmaps://{z}/{x}/{y}"           # quadkey reescrito no frontend
  max_zoom: 19
worldcover:
  mbtiles_path: "../data_external/wc_teste.mbtiles"
mapbiomas:
  mbtiles_path: "../data_external/mapbiomas.mbtiles"

classes:
  - { id: 1, name: "Massa d'água",     color: "#377eb8" }
  - { id: 2, name: "Área edificada",   color: "#e41a1c" }
  - { id: 3, name: "Floresta",         color: "#4daf4a" }
  - { id: 4, name: "Campo",            color: "#ffff33" }
  - { id: 5, name: "Cultivo",          color: "#984ea3" }
  - { id: 6, name: "Terreno exposto",  color: "#ff7f00" }

mask_overlay:
  cache_path: "data/mask_overlay_cache.mbtiles"
  min_zoom: 8
  max_zoom: 18

auth:
  jwt_secret: "<gerar>"           # ou via env TILECLASS_JWT_SECRET
  access_token_expiry_hours: 8
  refresh_token_expiry_hours: 24

database:
  path: "tileclass.db"
```

**Convenção de paths grandes:** `.mbtiles` (GBs) ficam em `data_external/`
na raiz; o `config.yaml` aponta com `../data_external/<arquivo>.mbtiles`
(relativo a `backend/`). Override do arquivo de config via env
`TILECLASS_CONFIG=<path>` (E2E). Rate limit desligável via
`TILECLASS_DISABLE_RATE_LIMIT=1` (apenas E2E).

## 7. Scripts CLI

Rodar como módulo (`python -m backend.scripts.<name>`) para imports
relativos funcionarem.

- `create_admin` — cria usuário admin inicial (interativo).
- `import_points --point <lat> <lon> <name> | --csv pontos.csv [--block N]` —
  importa tiles a partir de pontos centrais; `--block N` gera bloco N×N
  contíguo (gap < 1 mm validado).
- `import_cq_tiles --geoparquet <file> [--seed empty|raw]` — importa de
  geoparquet do CQ.
- `import_qc_tiles --csv qc_tiles.csv --bdf-dir <dir>` — importa lote do
  QC com seed mask do argmax.
- `export_tiles <out_dir> [--status reviewed|reviewed+classified] [--raw]
  [--mosaic] [--manifest <path>]` — GT extractor (ver §8).
- `build_mbtiles <raster_in> <out.mbtiles>` — converte raster → MBTiles XYZ.
- `build_xyz_pyramid <raster_in> <out_dir>` — pirâmide XYZ em disco.
- `merge_db <other.db>` — funde `tileclass.db` de outra equipe.

## 8. GT Extractor (`export_tiles.py`)

Interface canônica para outras aplicações consumirem máscaras finalizadas.
Output bit-a-bit compatível com `treinamento_6c/<split>/<split>_masks/`.

- Output: `<out_dir>/gt_<tile.name>.tif` — GeoTIFF single-band uint8 256×256,
  EPSG:4326, deflate, NODATA=255. Transform via
  `rasterio.transform.from_bounds(west, south, east, north, 256, 256)`
  (pixel ≈ 2.5 m na latitude do centro).
- `manifest.csv` — uma linha por tile com filename, tile_id, name, status,
  classified_by, reviewed_by, classified_at, reviewed_at, bbox_*.
- Filtros de status: `reviewed` (padrão, GT estrito) ou `reviewed+classified`
  (inclui não-revisados, dataset preliminar).
- Remap canônico TileClass → EDGV (ativo por padrão; `--raw` desativa):

  | TileClass | Nome              | EDGV | Nome EDGV       |
  |-----------|-------------------|------|-----------------|
  | 1         | Massa d'água      | 0    | agua            |
  | 2         | Área edificada    | 1    | edif            |
  | 3         | Floresta          | 4    | floresta        |
  | 4         | Campo             | 3    | campo           |
  | 5         | Cultivo           | 5    | veg_cultivada   |
  | 6         | Terreno exposto   | 2    | terr_exp        |
  | 255       | NODATA            | 255  | (ignore_index)  |

  Constante `EDGV_REMAP_LUT` no topo do script — único ponto de verdade.

## 9. Cuidados Técnicos Críticos

### 9.1 Concorrência
- `/api/tiles/next` é `BEGIN IMMEDIATE` + SELECT + UPDATE atômico. Garantido
  por teste com 10 operadores em paralelo (`threading.Barrier`).
- WAL ligado (`PRAGMA journal_mode=WAL`).

### 9.2 Geometria
- `TILE_PX=256`, `METERS_PER_PX=2.5`, `TILE_METERS=640`.
- `bbox_from_center` usa `pyproj.Geod` (WGS84) — precisão < 1 mm em qualquer
  latitude. `offset_center(lat, lon, dx, dy)` garante adjacência sem gap.
- Validado em `test_geo.py` (14 testes) e `test_raster_worldwide.py` (22
  testes em 20 pontos mundiais, equador → Antártica).

### 9.3 Performance do Canvas
- Não redesenhar inteiro; dirty rectangle por gesture.
- `drawOutlines` itera 65k pixels — só roda quando `!drawing`.
- Undo armazena só pixels alterados (`Uint32Array positions` +
  `Uint8Array prevValues`), 50 níveis.

### 9.4 Mapeamento de Coordenadas
- `getBoundingClientRect()` → fração → `Math.floor(frac * 256)` com clamp
  em `[0..255]`. Canto inferior-direito deve dar `(255, 255)`.

### 9.5 Integridade
- Validar 65536 bytes exatos e valores em `{1..6, 255}` no backend
  (`mask_utils.validate_partial`, IDs derivados do config).
- PNG armazenado: single-band ("L"), 8 bits, 256×256, sem compressão com
  perda. Pillow faz a conversão `bytes ↔ PNG`.

### 9.6 Segurança
- bcrypt cost ≥ 12; JWT HS256; rate limit 5/min/IP em login; endpoints admin
  atrás de `Depends(auth.require_admin)`. Sem `innerHTML` com dados de
  usuário (use `escapeHtml` ou `textContent`).
- Logout revoga `jti` em `token_blacklist`.

## 10. Estrutura de Diretórios

```
tileclass/
├── backend/
│   ├── main.py                 # FastAPI app + lifespan + middleware + static SPA
│   ├── routers/                # auth, operator, admin, config
│   ├── auth.py                 # JWT, bcrypt, rate limit, blacklist, role middleware
│   ├── models.py               # Schemas Pydantic
│   ├── database.py             # Conexão SQLite (WAL), schema, transaction(), log_action
│   ├── config.py               # Loader de config.yaml (singleton)
│   ├── tile_service.py         # Fila, atribuição, submit, problema, pause/resume
│   ├── admin_service.py        # Fachada que re-exporta backend/admin/
│   ├── admin/                  # dashboard, tiles_query, tiles_mutations, users, thumbnails
│   ├── mask_utils.py           # Uint8Array ↔ PNG "L" + validação
│   ├── mask_tile_service.py    # Cache mbtiles do overlay admin
│   ├── mbtiles_service.py      # Reader read-only (singletons primary/wc/mb)
│   ├── geo.py                  # bbox_from_center (pyproj.Geod WGS84)
│   ├── tile_grid.py            # Helpers Web Mercator
│   ├── config.yaml
│   └── scripts/                # create_admin, import_points/cq/qc, export_tiles,
│                               # build_mbtiles, build_xyz_pyramid, merge_db
├── data_external/              # MBTiles grandes (gitignored)
├── frontend/
│   ├── index.html              # SPA (login / editor / admin)
│   ├── css/style.css
│   ├── vendor/maplibre/
│   └── js/
│       ├── app.js              # Router por role
│       ├── api.js              # fetch wrapper + JWT refresh
│       ├── editor.js           # Canvas, ferramentas, undo/redo, submit
│       ├── mask-core.js        # Lógica pura (paint, flood, screenToLogical)
│       ├── backup.js           # localStorage backup
│       ├── admin.js / admin/   # Dashboard, tiles, users, viewer
│       ├── minimap.js          # MapLibre 3×3
│       ├── maplib.js           # Helpers MapLibre
│       ├── utils.js            # hexToRgb, blobToImage, escapeHtml
│       └── toast.js
├── docs/requirements.md        # Este arquivo
├── tests/                      # pytest + Vitest + Puppeteer (ver CLAUDE.md)
└── requirements.txt
```

## 11. Critérios de Aceitação

### 11.1 Auth
- Login válido devolve par `{access, refresh}`; inválido → 401.
- Refresh proativo evita expiração durante uso contínuo.
- Logout revoga o `jti`; chamadas seguintes com o mesmo token → 401.
- Roles enforçados em todos os endpoints admin.

### 11.2 Fila
- Dois operadores nunca recebem o mesmo tile.
- Fila de revisão tem prioridade sobre pending.
- Revisor nunca recebe um tile que ele próprio classificou.
- F5 não perde trabalho: `/next` devolve o tile atribuído.
- 204 quando vazio; UI mostra estado claro.

### 11.3 Editor
- Pintar com brush não tem lag (< 16 ms por frame, sem outline durante
  gesture).
- Borracha volta para 255.
- Undo/Redo byte-exato (50 níveis, patches).
- Slider de opacidade + Space (hold) + modo contornos (`O`).
- Submit rejeita `255` no array; pisca pixels faltantes 2 s.

### 11.4 Revisão
- Banner "MODO REVISÃO" + nome do classificador.
- Botão de submit muda para "Aprovar revisão".
- Revisor pode editar antes de aprovar.

### 11.5 Admin
- Dashboard: totais, completion %, série diária, per-operator, médias de
  duração `assign→classify` e `assign→review`.
- Bulk actions atômicas (`reset_many`, `re_review_many`, `assign_many`,
  `block_many`, `unblock_many`, `unassign_many`, `report_problem_many`).
- Block guarda status original em `blocked_from`; unblock restaura.
- Delete só permitido se `status='problem'`.

### 11.6 Geometria/Export
- Tile sempre 640 m × 640 m em qualquer latitude (pixel = 2.5 m em ambos
  eixos).
- GeoTIFF exportado tem CRS=EPSG:4326, NODATA=255, bbox correta para 20
  pontos mundiais.

### 11.7 Performance
- `/next` < 2 s; pintura < 16 ms/frame; 10 operadores simultâneos sem
  degradação (validado por testes de concorrência).

## 12. Dependências

`requirements.txt`:

```
fastapi
uvicorn
pyjwt
bcrypt
pillow
pyyaml
numpy
python-multipart
pyproj
rasterio
```

Sem ORM. `sqlite3` nativo com queries SQL parametrizadas (`?`).
