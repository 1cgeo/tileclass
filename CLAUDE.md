# TileClass

Aplicação web para classificação pixel-a-pixel de tiles de satélite. **Geometria do tile é por projeto** (`projects.tile_px` × `projects.meters_per_pixel`). Default herdado: 256×256 px @ 2.5 m/pixel = 640 m × 640 m no chão.
Backend FastAPI + SQLite; frontend Vanilla JS com Canvas HTML5. Imagem de fundo via mbtiles XYZ por projeto.

**Projetos:** classes, paleta de cores, mbtiles (imagem primária/secundária/terciária + duas máscaras de referência), a flag `mask_complete_required` e a geometria do tile (`tile_px`, `meters_per_pixel`) vivem por projeto. Tiles, membership de operador/revisor e o dashboard são todos escopados por `project_id`. **Uma instalação nova começa vazia — sem projeto, sem classes.** O admin cria e configura o primeiro projeto pela UI admin (aba Projetos) ou `POST /api/admin/projects`. A fonte de verdade é sempre as tabelas `projects/project_classes/project_members`; o `config.yaml` **não** carrega dado de domínio.

Spec completa: `docs/requirements.md`. Em caso de dúvida, o requirements manda.

## Stack

- **Backend:** Python 3.11+, FastAPI, Uvicorn, SQLite (sqlite3 nativo — **sem ORM**), PyJWT, bcrypt, Pillow, NumPy, PyYAML, rasterio (export), pyproj (geodésica WGS84)
- **Frontend:** Vanilla JS (sem framework), Canvas HTML5, fetch API. MapLibre GL JS (via CDN) para renderização georreferenciada dos tiles XYZ. Servido como estático pelo FastAPI.
- **Testes:** pytest + httpx (backend), Vitest + jsdom (frontend unit), Puppeteer + uvicorn real (E2E)
- **Config:** `backend/config.yaml` carrega **apenas o que NÃO é configurável no nível da aplicação** — infra/segredos: `database.path`, `auth` (jwt_secret + TTLs) e `mask_overlay` (cache do overlay admin). **Nada de domínio** (projetos, classes, layers, geometria, membros) — isso vive no banco e é editado pela UI admin. Override por env: `TILECLASS_CONFIG=<path>` (usado nos E2E). Rate limit desligável via `TILECLASS_DISABLE_RATE_LIMIT=1` (apenas E2E — nunca em produção).

## Comandos

```bash
# Setup
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
cp backend/config.example.yaml backend/config.yaml   # config.yaml é gitignored
# troque auth.jwt_secret (o app recusa subir login com o placeholder)

# Dev server (init_db roda no lifespan → schema vazio; crie admin + 1º projeto via UI)
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

# Scripts CLI (rodar como módulo para imports relativos funcionarem)
python -m backend.scripts.create_admin
python -m backend.scripts.import_points --point <lat> <lon> <name> [--project <id|name>]   # um ponto
python -m backend.scripts.import_points --csv pontos.csv [--block 3] [--project <id|name>] # CSV lat,lon,name; block NxN
python -m backend.scripts.import_cq_tiles --geoparquet cq_selection.geoparquet [--seed empty|raw] [--project <id|name>]
python -m backend.scripts.import_qc_tiles --csv qc_tiles.csv --bdf-dir <dir> [--project <id|name>]
python -m backend.scripts.export_tiles <out_dir> [--status reviewed|reviewed+classified] [--raw] [--mosaic] [--manifest <path>] [--project <id|name>]      # raster (GeoTIFF)
python -m backend.scripts.export_features <out_dir> [--status ...] [--mosaic] [--manifest <path>] [--project <id|name>]                                  # vector (GeoJSON)
python -m backend.scripts.export_classifications <out_dir> [--status ...] [--manifest <path>] [--project <id|name>]                                      # classification (CSV)
python -m backend.scripts.export_detections <out_dir> [--status ...] [--mosaic] [--manifest <path>] [--project <id|name>]                                # detection (GeoJSON bboxes)
python -m backend.scripts.build_mbtiles <raster_in> <out.mbtiles>           # raster grande → tiles XYZ
python -m backend.scripts.build_xyz_pyramid <raster_in> <out_dir>            # alternativa em disco
python -m backend.scripts.merge_db <other.db>                                # funde tileclass.db de outra equipe

# Testes
python -m pytest tests/ --ignore=tests/e2e --ignore=tests/frontend -v   # backend
npx vitest run                                                           # frontend unit
node tests/e2e/runner.mjs                                                # E2E (sobe uvicorn + Puppeteer)
npm run test:all                                                         # tudo em sequência
```

**venv no Windows:** sempre ative antes de rodar `uvicorn`/`pytest`/scripts — `.\.venv\Scripts\Activate.ps1` (PowerShell) ou `.venv\Scripts\activate` (cmd/bash). Sem ativar, o terminal não acha `uvicorn` no PATH. Alternativa sem ativar: `.\.venv\Scripts\python -m uvicorn ...` (idem para `pytest`, scripts). Se o PowerShell bloquear `Activate.ps1`, rode uma vez `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`.

## Estrutura

```
tileclass/
├── backend/
│   ├── main.py                 # FastAPI app + lifespan + middleware + static SPA
│   ├── routers/                # FastAPI APIRouters (registrados no main.py)
│   │   ├── auth.py             # /api/auth/{login,refresh,me,logout}
│   │   ├── projects.py         # /api/projects + /api/projects/{id} + /api/projects/{id}/xyz/{layer}/{z}/{x}/{y}.{ext}
│   │   │                        # + admin /api/admin/projects[...] CRUD/classes/members
│   │   ├── operator.py         # /api/tiles/* + /api/me/stats-today + helpers de body de máscara
│   │   └── admin.py            # /api/admin/* (dashboard, tiles, users, mask overlay) — todos com ?project_id= opcional
│   ├── auth.py                 # JWT, bcrypt, rate limit, blacklist, role middleware
│   ├── models.py               # Schemas Pydantic
│   ├── database.py             # Conexão SQLite (WAL), schema, transaction(), log_action
│   ├── config.py               # Carrega config.yaml (singleton cache)
│   ├── project_service.py      # CRUD de projetos + classes + membros, cache + path validation, require_membership
│   ├── tile_service.py         # Fila, atribuição atômica, submit, problema, pause/resume, histórico — escopados por project_id
│   ├── admin_service.py        # Fachada — re-exporta de backend/admin/
│   ├── admin/                  # Submódulos do admin (split de admin_service.py)
│   │   ├── dashboard.py        # /admin/dashboard + _cycle_durations (CTE pause/resume aware)
│   │   ├── tiles_query.py      # list_tiles, count_tiles, list_tiles_map, list_problems + _build_tiles_filter
│   │   ├── tiles_mutations.py  # reset/problem/re-review/assign/unassign/pause/delete/block/unblock (atômicos)
│   │   ├── users.py            # list/create + can_review/role/active toggles
│   │   └── thumbnails.py       # tile_thumbnail (mask colorizada) + tile_satellite_thumbnail
│   ├── mask_utils.py           # Uint8Array ↔ PNG "L" + validação (allowed_ids do projeto, require_complete por projeto)
│   ├── mask_tile_service.py    # Cache mbtiles do overlay admin (rasteriza máscaras → XYZ)
│   ├── mbtiles_service.py      # Pool LRU de readers por (project_id, layer); 5 layers × N projetos
│   ├── geo.py                  # TILE_PX/METERS_PER_PX/TILE_METERS + bbox_from_center (pyproj.Geod WGS84)
│   ├── tile_grid.py            # Helpers Web Mercator (build_mbtiles e import_cq)
│   ├── config.yaml
│   └── scripts/                # create_admin, import_points, import_cq_tiles, import_qc_tiles,
│                               # export_tiles, build_mbtiles, build_xyz_pyramid, merge_db
├── data_external/              # MBTiles grandes (gitignored) — tiles/wc_teste/mapbiomas
├── frontend/
│   ├── index.html              # SPA (login / editor / admin)
│   ├── css/style.css           # Tokens em :root + breakpoints
│   ├── vendor/maplibre/        # CDN local (.js + .css)
│   └── js/
│       ├── app.js              # Router SPA por role
│       ├── api.js              # Fetch wrapper + JWT refresh (reativo + proativo)
│       ├── editor.js           # Canvas, ferramentas, undo/redo, submit
│       ├── mask-core.js        # Lógica PURA (paint, Bresenham, flood, undo, screenToLogical) — testável sem DOM
│       ├── backup.js           # localStorage backup (round-trip Uint8Array ↔ base64) — testável sem DOM
│       ├── admin.js            # Orquestrador admin (tiles, users, viewer)
│       ├── admin/               # Submódulos: dashboard.js, modals.js, projects.js (CRUD/classes/members)
│       ├── minimap.js          # MapLibre 3×3 com highlight do tile atual
│       ├── maplib.js           # Helpers MapLibre (createLockedMap, setMapBbox)
│       ├── utils.js            # hexToRgb, blobToImage, escapeHtml
│       └── toast.js            # Notificações
├── docs/
│   └── requirements.md          # Spec completa
├── tests/                       # pytest + TestClient (auth, tiles, admin, concorrência, geo, raster)
│   ├── frontend/                # Vitest + jsdom (mask-core, api-refresh, utils, localstorage-backup)
│   └── e2e/runner.mjs           # Puppeteer + uvicorn real
└── requirements.txt
```

**Convenção de paths grandes:** `.mbtiles` (GBs) ficam em `data_external/` na raiz; o `config.yaml` aponta com `../data_external/<arquivo>.mbtiles` (relativo a `backend/` por convenção do `_open_optional` no `main.py`). Mantém o pacote `backend/` enxuto.

## Regras de idioma

- **UI (labels, mensagens, tooltips):** Português pt-BR com acentuação correta.
- **Comentários, docstrings, nomes de variável/função:** Inglês.
- **Campos de banco e API JSON:** Inglês (`status`, `assigned_to`, `classified_at`).

## Projetos

Cada projeto define seu próprio mundo de classificação. As tabelas-chave são `projects`, `project_classes` (PK `(project_id, class_id)`) e `project_members` (PK `(project_id, user_id)` com role `operator|reviewer|admin`). `tiles.project_id` é `NOT NULL` e referencia `projects(id)`.

**Layers (cada slot aceita path mbtiles OU URL de tile-server remoto):**

| Coluna | Layer (frontend) | Atalho | Obrigatório |
|---|---|---|---|
| `primary_mbtiles` | `primary` | — (fundo) | **sim** |
| `secondary_mbtiles` | `secondary` | `D` (hold) | não |
| `tertiary_mbtiles` | `tertiary` | `R` (hold) | não |
| `ref_mask_primary_mbtiles` | `ref_primary` | `T` (hold) | não |
| `ref_mask_secondary_mbtiles` | `ref_secondary` | `Y` (hold) | não |

Cada campo aceita três formatos:
- **Path mbtiles** (relativo a `backend/` ou absoluto): backend abre via pool `mbtiles_service.get_reader(project_id, layer)` e serve em `/api/projects/{id}/xyz/{layer}/{z}/{x}/{y}.{ext}`. JWT exigido (autenticação intra-rede).
- **URL de tile-server remoto** (Martin / TileServer-GL / similares): valor começando com `http(s)://` e contendo `{z}/{x}/{y}`. MapLibre busca direto, sem proxy do backend. Útil para integrar com infraestrutura existente. CSP do servidor pode precisar ajuste (`connect-src` em `main.py`).
- **Esquema `bingmaps://{z}/{x}/{y}`**: Bing Maps usa quadkeys em vez de z/x/y, então o frontend (`maplib.js` → `tileTransformRequest`) reescreve `bingmaps://` para o endpoint quadkey do `virtualearth.net` on-the-fly. O backend trata como remoto (pass-through, sem proxy). É o valor seed do tertiary do projeto default.

`project_service.is_remote_layer(value)` é o ponto único que classifica remoto vs. arquivo (prefixos `http://`, `https://`, `bingmaps://`); `layer_path(proj, layer)` é o único ponto de leitura. Layers ausentes não registram atalho nem aparecem na sidebar/cheat-sheet — `refreshShortcutsBadges()` esconde os badges via `data-overlay-key` no HTML. `ref_*` são overlays de referência (raster categorizado, não-editável); ao serem segurados, escondem a máscara do operador (`HIDE_MASK_OVERLAYS`).

**`mask_complete_required`:** quando `True` (default), submit rejeita pixels=255 com `unfilled_pixels`; quando `False`, aceita. Frontend espelha o gate: `updateSubmitButton` só pinta `incomplete` no projeto estrito.

**Membership e bloqueio:**
- Operador só vê tiles do projeto onde é membro. Admins globais veem todos.
- `users.can_review` virou um veto temporário: `False` impede review queue mesmo se a membership for `reviewer`. Será removido quando a UI completar a migração para roles por projeto.
- Soft-disable: `PATCH /api/admin/projects/{id}` com `active=False` mantém todos os dados mas `/next` e `/next-preview` retornam 409 `project_inactive`. Stats/dashboard continuam respondendo. Operador termina o tile que já tem antes do bloqueio.
- Hard-delete: `DELETE /api/admin/projects/{id}` só funciona se o projeto não tem tiles. Caso contrário 409 `project_has_tiles` — desative em vez de excluir.

**Fluxo do editor:**
1. `apiGet("/api/projects")` ao login. Único projeto → seleção automática; múltiplos → dropdown `#project-picker` no header (persistido em `localStorage["tileclass_active_project_id"]`).
2. `apiGet("/api/projects/{id}")` carrega `classes`, `mask_complete_required`, e `layers` com URLs prontas (`/api/projects/{id}/xyz/{layer}/{z}/{x}/{y}.{ext}`).
3. Toda chamada de fila (`/api/tiles/next`, `/next-preview`, `/queue-stats`, `/me/stats-today`, `/tiles/assigned`) passa `?project_id=<id>`. Quando o usuário tem só uma membership o param pode ser omitido; com múltiplas, o backend devolve 400 `project_id_required`.

**Migração (idempotente, em `database.py`):** DBs pré-projetos ganham um projeto "default" criado por `_migration_seed_project` — **hardcoded** (nome `default`, paleta padrão `_MIGRATION_CLASSES` de 6 classes, layers vazios), **não** lido do `config.yaml`; serve só para dar um lar aos tiles órfãos (cujas máscaras podem referenciar os ids 1..6) e é totalmente editável depois pela UI. Todos os tiles existentes recebem `project_id=<default>`; usuários ativos viram membros com role derivada de `role+can_review`. A coluna é tornada `NOT NULL` via tabela espelho — o rebuild roda com `foreign_keys=OFF` numa transação (a tabela `action_log` referencia `tiles` por FK, então o `DROP TABLE` falharia com FKs ligadas) e é resumível após falha parcial (guard checa `project_id` ser NOT NULL, não só existir). **DBs novos NÃO são semeados** — o `SCHEMA` já cria `tiles.project_id NOT NULL`, então o branch de migração não roda e a instalação fica vazia (admin cria o 1º projeto pela UI).

**Pool de mbtiles readers:** `mbtiles_service` mantém um LRU de até 32 readers, chaveado por `(project_id, layer)`. Editar paths via admin invalida o reader correspondente. Arquivos inválidos/corruptos são tolerados (reader fica `closed`, layer some do payload). URLs remotos não consomem slots do pool — só paths mbtiles. Lifespan pré-aquece o reader `primary` de cada projeto ativo (apenas mbtiles locais) para evitar latência na primeira requisição.

**Mask overlay (admin):** `mask_tile_service` é per-projeto. Endpoint `/api/admin/mask-tiles/{project_id}/{z}/{x}/{y}.png`. Cache em arquivo separado `<base>_p<project_id>.mbtiles` para garantir que paletas/conjuntos de tiles de projetos diferentes não compartilhem rows. LUT vem de `project_classes[project_id]`. Invalidações (`safe_invalidate_tile`/`safe_invalidate_bbox(project_id, ...)`/`safe_invalidate_tiles`) buscam o `project_id` da tile e direcionam para o cache file correto.

**Regra inegociável de classes:** renomear/recolorir IDs é livre; **remover** uma classe é rejeitado quando o projeto tem qualquer tile (bytes da máscara são imutáveis e podem referenciar o ID removido). Adicionar novos IDs é livre.

## Projetos vetoriais (kind=vector)

Projetos têm `kind ∈ {raster, vector, classification, detection}` (default raster, **imutável após criação**). Vector é para anotações de linhas com atributos — drenagem (com direção/conectividade), rodovias (com pavimento/faixas), etc. O dataset é consumido por modelos de IA via export GeoJSON.

**Onde diverge de raster:**

| Camada | Raster | Vector |
|---|---|---|
| Body do tile | `tiles.data_png` (PNG 65536 bytes) | `tiles.data_geojson` (TEXT, FeatureCollection) + `feature_count` cache |
| Schema de domínio | `project_classes` (id, name, color) | `project_attributes` (key, label, type, required, options) |
| Validação no submit | `mask_complete_required` + IDs em `{1..6, 255}` | GeoJSON parsável, attributes do schema, `topology_required` (drenagem) |
| Submit body | `Content-Type: application/octet-stream` (65536 bytes) | `Content-Type: application/json` (FeatureCollection) |
| Endpoint de leitura | `GET /api/tiles/{id}/image` (PNG) | `GET /api/tiles/{id}/features` (JSON) |
| Editor | `editor.js` canvas paint (mask-core.js) | `editor-vector.js` MapLibre interactive (vector-core.js) |
| Overlay admin | rasteriza máscaras → PNG | rasteriza LineStrings → PNG (`_render_vector_tile`) |
| Distribuição no dashboard | `class_distribution` (pixel counts) | `feature_distribution` (counts por enum/boolean attribute) |
| Export CLI | `export_tiles.py` (GeoTIFF) | `export_features.py` (GeoJSON) |

**Atributos** (`project_attributes`): tipos `text`/`number`/`enum`/`boolean`. `enum` precisa ≥2 `options`. Keys são snake_case. Adicionar/renomear/relabel é livre; remover só quando nenhuma feature do projeto referencia a chave (`attribute_in_use` 409). Para drenagem com `topology_required=True`, a chave `direction ∈ {forward, reverse, both}` é obrigatória por feature.

**Validação de topologia** (`vector_utils.validate_topology`): só roda quando `topology_required=True`. Cada LineString precisa ter `direction`; endpoints próximos (≤ `SNAP_TOLERANCE_DEG ≈ 1.5e-5`, ~1m) snapam ao mesmo nó; ciclos via DFS (forward/reverse/both definem a direção das arestas) são rejeitados. **Validação é por tile** — drenagens cruzando bordas viram features distintas em tiles distintos; reconexão cross-tile é responsabilidade do consumer.

**Editor vetorial:**
- **Pen (P):** clique adiciona vértice; duplo-clique encerra; ESC cancela. Snap automático no primeiro/último vértice se estiver perto de um endpoint existente.
- **Select (V):** clique numa feature → painel de atributos à direita; Del/Backspace remove.
- **Undo/redo:** Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z. 50 frames.
- O painel de atributos é schema-driven; campo extra "direção" aparece quando `topology_required`. Required marcado com `*`; submit bloqueia se faltar.

**Pause vs submit:** pause persiste o body do jeito que está (qualquer GeoJSON parsável, mesmo com required ausente ou ciclos), submit valida tudo. Operador pode pausar parcial sem perder trabalho.

**Mutual exclusion no payload:** `POST /api/admin/projects` rejeita `attributes` em projeto raster/classification/detection (400 `attributes_not_supported`) e `classes` em projeto vector (400 `classes_on_vector`). Tentar mudar `kind` via PATCH é silenciosamente ignorado (campo fora do allow-list — invariante de imutabilidade).

**Invariantes críticos do dispatch:**
- `tile_service.project_for_tile()` resolve a kind antes de cada submit/pause; `_submit_raster`/`_submit_vector`/`_submit_classification`/`_submit_detection` (e `_pause_*`) ficam isolados.
- `routers/operator._read_body_for_kind` lê o body certo (raster: tile_px² bytes exatos; vector/detection: até 1.5MB JSON; classification: até 1KB JSON).
- `report_problem` limpa `data_png`, `data_geojson` **e** `data_class_id` para que mudanças futuras não vazem corpo de tipo errado.
- `mask_tile_service.get_tile()` despacha por kind. Cache mbtiles é per-projeto (`<base>_p<id>.mbtiles`); kinds diferentes em projetos diferentes não compartilham linhas.

## Projetos de classificação (kind=classification)

Operador escolhe **uma classe** do projeto pra todo o tile — sem pintura, sem desenho. Caso de uso: rotular tiles inteiros (ex.: cobertura predominante).

**Onde diverge de raster:**

| Camada | Raster | Classification |
|---|---|---|
| Body do tile | `tiles.data_png` (PNG tile_px² bytes) | `tiles.data_class_id` (INTEGER) |
| Schema de domínio | `project_classes` (id, name, color) | mesma tabela, mesmas regras |
| Validação no submit | máscara completa + IDs em `{1..N, 255}` | `class_id ∈ project_classes` |
| Submit body | `Content-Type: application/octet-stream` (tile_px² bytes) | `Content-Type: application/json` (`{"class_id": int}`) |
| Endpoint de leitura | `GET /api/tiles/{id}/image` (PNG) | `GET /api/tiles/{id}/classification` (`{class_id}` ou 204) |
| Editor | `editor.js` canvas paint | `editor-classification.js` MapLibre + botões de classe |
| Pause | salva máscara parcial | **409 `pause_not_supported`** — fluxo single-click |
| Overlay admin | colore pixel a pixel | retângulo translúcido + nome da classe centrado |
| Distribuição no dashboard | `class_distribution` (pixel counts) | `tile_class_distribution` (1 tile = 1 contagem) |
| Export CLI | `export_tiles.py` (GeoTIFF) | `export_classifications.py` (CSV único) |

**Editor:** sidebar mostra um botão por classe (cor + nome). Operador clica → highlight; rodapé Submit habilitado → modal confirm → POST. Sem undo/redo (escolha é atômica). Heartbeat e state machine iguais aos outros.

**Tile_px em classification:** afeta só thumbnail/overlay rasterizado (não há body de pixels). `tile_meters = tile_px × meters_per_pixel` continua definindo a bbox.

## Projetos de detecção (kind=detection)

Operador desenha **caixas (bounding boxes)** ao redor de objetos de interesse, cada caixa com **uma classe** do projeto. Caso de uso: dataset de detecção de objetos (consumido por modelos via export GeoJSON, convertível a COCO/YOLO downstream).

**Reaproveita a infra vetorial** (corpo em `data_geojson`), mas com geometria de retângulo + classe por caixa em vez de linha + atributos.

**Onde diverge de vector:**

| Camada | Vector | Detection |
|---|---|---|
| Geometria | `LineString` (≥2 vértices) | `Polygon` retângulo alinhado aos eixos (2 longitudes × 2 latitudes distintas) |
| Schema de domínio | `project_attributes` (por feature) | `project_classes` — `properties.class_id` por caixa |
| Validação no submit | attributes + `topology_required` | `class_id ∈ project_classes` + `box_required` (≥1 caixa) |
| Flag por projeto | `topology_required` | `box_required` (0 = tile vazio é negative sample válido) |
| Caches | `feature_count` | `feature_count` (nº de caixas) + `class_counts` (caixas por classe) |
| Editor | `editor-vector.js` (caneta) | `editor-detection.js` (arrastar retângulo; tecla B=desenhar, V=selecionar) |
| Overlay admin | rasteriza linhas | `_render_detection_tile` (retângulos coloridos por classe) |
| Distribuição no dashboard | `feature_distribution` | `class_distribution` (reusa `class_counts` — contagem de caixas) |
| Export CLI | `export_features.py` | `export_detections.py` (GeoJSON; `box_count` no manifest) |

**Compartilha com vector:** leitura em `GET /api/tiles/{id}/features` (FeatureCollection), body JSON até 1.5MB, pause salva parcial (só checa parse), `detection-core.js` reusa os helpers de undo/redo do `vector-core.js`.

**`box_required`:** quando `True`, submit rejeita 0 caixas (`invalid_boxes`); quando `False`, tile vazio classifica normalmente (negative sample). Editor espelha o gate em `validateForSubmit`.

**Validação (`detection_utils`):** `parse_detection` (estrutural — cada feature é Polygon retângulo alinhado), `validate_class_ids` (membership na paleta), `validate_submission` (estrutural → class_ids → gate box_required). Tudo puro/sem DB, espelhado em `detection-core.js`.

## Invariantes do domínio

- **Geometria do tile (per-projeto):** `projects.tile_px` (qualquer inteiro 1..4096) × `projects.meters_per_pixel > 0`; `tile_meters = tile_px × meters_per_pixel`. Default seed = 256 × 2.5 = 640 m. Admin escolhe livre — tile_px=226 é tão válido quanto 256. Cada tile é definido **pelo centro geodésico**; `geo.bbox_from_center(lat, lon, tile_meters)` usa `pyproj.Geod` (WGS84) para calcular ±tile_meters/2 em cada direção cardeal. Schema de `tiles` tem apenas `bbox_*`; zoom/tile_x/tile_y/context_tiles foram removidos. **Imutável após o primeiro tile** — `update_project` rejeita mudanças com 409 `tile_geometry_locked`. Backend e scripts (import/export) lêem da row do projeto via `project_service.get_project()`.
- **Adjacência sem gap:** `offset_center(lat, lon, dx, dy, tile_meters)` caminha `dx*tile_meters` e `dy*tile_meters` por geodésica, garantindo que tiles vizinhos do `--block NxN` compartilhem arestas exatamente (gap < 1 mm, validado por teste).
- **Fonte de verdade da máscara:** `Uint8Array(tile_px²)` no cliente (re-alocado em `setTileGeometry()` no editor.js). Valores válidos: IDs definidos em `project_classes[project_id]` + `255` (não preenchido). `mask_utils.validate_partial(raw, allowed_ids, tile_px=...)` recebe os IDs e o tamanho explicitamente; `validate_submission(...)` usa o `mask_complete_required` do projeto para decidir se rejeita 255. O canvas é apenas visualização.
- **PNG do backend:** banda única (grayscale "L"), 8 bits, `tile_px × tile_px`, sem compressão com perda. Pillow faz a conversão `bytes ↔ PNG`. `encode_mask`/`decode_mask` recebem `tile_px` explicitamente.
- **Protocolo wire:** frontend envia **raw bytes** (Uint8Array, `tile_px²` bytes) no body do classify/review; nunca PNG. Backend resolve `tile_px` via `project_for_tile()` antes de validar tamanho do body.
- **Submissão:** rejeitar se houver `255` no array. Resposta de erro traz a contagem.
- **Máquina de estados de tile:** `pending → in_progress → classified → in_review → reviewed`; qualquer estado `→ problem`; `problem → pending` e `reviewed → in_review` são transições de admin. Admin pode também bloquear via `pending|classified|reviewed → blocked` (guardando o status original em `blocked_from`) e desbloquear via `blocked → <blocked_from>`. `in_progress`/`in_review`/`problem` **não podem** ser bloqueados. Tiles bloqueados são naturalmente excluídos das filas porque os SELECTs de `/next` filtram por `status='pending'` ou `'classified'`.
- **Export GeoTIFF:** `backend/scripts/export_tiles.py` usa `rasterio.transform.from_bounds(west, south, east, north, 256, 256)` com `crs=EPSG:4326`. Combinado com a bbox de `bbox_from_center`, o pixel resultante é exatamente 2.5 m na latitude do centro (validado para 20 pontos mundiais em `test_raster_worldwide.py`). Detalhes do contrato (status filter, remap EDGV, manifest) na seção **GT Extractor** abaixo.

## GT Extractor (interface canônica)

`backend/scripts/export_tiles.py` é a **interface padrão** para outras aplicações/agentes consumirem máscaras finalizadas. Output é compatível bit-a-bit com `treinamento_6c/<split>/<split>_masks/` (mesmas classes, mesmo NODATA, mesmo formato GeoTIFF).

**Uso:**

```bash
# Padrão: só tiles revisados, IDs remapeados para EDGV 0..5
python -m backend.scripts.export_tiles <out_dir>

# Inclui classificados (sem revisão) — útil pra dataset preliminar
python -m backend.scripts.export_tiles <out_dir> --status reviewed+classified

# Mantém IDs nativos do TileClass (1..6) sem remapear
python -m backend.scripts.export_tiles <out_dir> --raw

# Também escreve gt_mosaic.tif unindo tudo
python -m backend.scripts.export_tiles <out_dir> --mosaic

# Restringe a um único projeto
python -m backend.scripts.export_tiles <out_dir> --project default
```

**Formato de saída** (cada tile: `<out_dir>/gt_<tile.name>.tif`):
- GeoTIFF single-band uint8, 256×256, **EPSG:4326**, compressão `deflate`, **NODATA = 255**
- Transform via `rasterio.transform.from_bounds(west, south, east, north, 256, 256)` — pixel ≈ 2.5 m na latitude do centro
- Reprojeção para 3857/UTM fica a cargo do consumer (`gdalwarp` / `rasterio.warp`).

**Multi-projeto:** `--project <id|name>` filtra a exportação. Sem o flag, todos os projetos vão para o mesmo `out_dir` (manifest distingue via coluna `project_id`). O remap EDGV foi pensado para o projeto seed de 6 classes — projetos com paletas diferentes devem usar `--raw` para preservar IDs nativos.

**Remap canônico de classes** (default no projeto seed; `--raw` desativa):

| TileClass id | Nome (config.yaml seed) | EDGV id | Nome EDGV (treinamento_6c) |
|:-:|---|:-:|---|
| 1 | Massa d'água | **0** | agua |
| 2 | Área edificada | **1** | edif |
| 3 | Floresta | **4** | floresta |
| 4 | Campo | **3** | campo |
| 5 | Cultivo | **5** | veg_cultivada |
| 6 | Terreno exposto | **2** | terr_exp |
| 255 | NODATA | **255** | NODATA (ignore_index) |

A LUT (`EDGV_REMAP_LUT`) é constante de módulo no topo do script — único ponto de verdade para o mapeamento. Indexes não usados (0, 7..254) passam como identidade.

**Filtros de status:**
- `reviewed` (padrão) — só tiles que passaram pela revisão (GT estritamente aceito).
- `reviewed+classified` — inclui também `classified` (passaram só pela classificação, ainda não revisados). Útil para dataset preliminar/maior, ciente do risco de inconsistência.

**Manifest (`<out_dir>/manifest.csv`):** uma linha por tile exportado com `filename, tile_id, project_id, name, status, classified_by, reviewed_by, classified_at, reviewed_at, bbox_*`. É o ponto de entrada pra outros agentes saberem o que receberam (status, autoria, projeto, geometria) sem precisar abrir o GeoTIFF.

**Quando estender:** se um consumer precisar de outro CRS, formato (PNG/Zarr), ou subset (por bbox/operador/data), adicione flags ao mesmo script — não crie novo extractor. Manter um único ponto de saída evita drift entre consumers.

## Regras críticas de backend

- **Sem ORM.** `sqlite3` puro com queries SQL parametrizadas (`?`). `PRAGMA journal_mode=WAL` e `foreign_keys=ON` ligados em `connect()`.
- **Transações explícitas:** use `from backend.database import transaction` como context manager (`BEGIN IMMEDIATE` + COMMIT/ROLLBACK). Nunca misture `conn.execute("BEGIN")` com transações aninhadas.
- **`/api/tiles/next` é transacional:** `BEGIN IMMEDIATE`, `SELECT ... LIMIT 1`, `UPDATE status, assigned_to`, `COMMIT`. Dois operadores nunca recebem o mesmo tile. Garantido por teste de concorrência (10 operadores paralelos).
- **Fila de revisão:** tiles com `status='classified'` aguardam revisor; ao atribuir, vão para `in_review`. Revisor nunca revisa tile que ele classificou (`classified_by != current_user`).
- **Resume (14.4):** primeira coisa em `/next` é buscar `assigned_to=user AND status IN ('in_progress','in_review')` — se achar, devolve o mesmo tile (F5 não perde trabalho).
- **Autorização em submit:** `assigned_to == current_user` é pré-condição para `classify`/`review`.
- **`HTTPException` é importado no topo do módulo**, não dentro de funções. Serviços podem levantar diretamente — FastAPI propaga.
- **Bulk vs single:** as operações admin (`reset_many`, `re_review_many`) fazem o trabalho real; `reset_tile`/`re_review_tile` são wrappers de 1 linha. Não duplicar SQL.
- **Rate limit:** `/api/auth/login` — 5/min por IP. Use `auth.reset_rate_limits()` nos testes (não acesse `_login_attempts` direto).
- **bcrypt:** cost ≥ 12. **JWT:** HS256, access 8h, refresh 24h (valores em `config.yaml`).
- **Índices:** `status`, `assigned_to`, `classified_by`, `reviewed_by`, `classified_at`, `reviewed_at` — manter quando adicionar filtros no admin.
- **Métricas de duração (dashboard):** derivadas do `action_log` via pares `assign_classify→classify` e `assign_review→review` (não há schema novo). `dashboard()` retorna `avg_classify_seconds`/`avg_review_seconds` globais + por operador em `per_operator[]`. Pareamento usa `MAX(...)` por `(user_id, tile_id)` para suportar tiles re-atribuídos; `julianday()*86400` converte o diff para segundos.

## Regras críticas de frontend

- **Três camadas sobrepostas no editor:** (1) **MapLibre GL** renderiza o satélite XYZ como `div` georreferenciado ao fundo (`interactive: false`, bbox via `fitBounds`), (2) canvas da máscara com `globalAlpha` controlável, (3) canvas de cursor/interação no topo. Eventos de mouse vão na camada 3 (`pointer-events: auto`); as outras têm `pointer-events: none`.
- **MapLibre resolve composição de tiles:** quando a bbox não alinha com um XYZ único, MapLibre carrega múltiplos tiles e recorta pela bbox. Não compor manualmente.
- **Mapeamento de coordenadas (ponto mais frágil):** `getBoundingClientRect()` → fração → `Math.floor(frac * 256)` com clamp em `[0..255]`. O canto inferior-direito deve dar `(255, 255)`.
- **Uint8Array como fonte de verdade:** canvas é só visualização. Mudar o array, depois chamar `writeMaskPixels(x0,y0,x1,y1)` + `blitMask()`.
- **Outlines no hot-path:** `drawOutlines` itera 65k pixels — **não chamar durante o gesto de pintura**. O `blitMask` só desenha outlines quando `!drawing`; o `onMouseUp` re-chama `blitMask` para atualizar.
- **Undo/redo:** cada ciclo mousedown→mouseup é uma entrada. Armazenar **só pixels alterados** (`Uint32Array positions` + `Uint8Array prevValues`), nunca o array inteiro. 50 níveis.
- **Interpolação no drag:** Bresenham entre `mousemove` para não deixar buracos em movimentos rápidos.
- **`Space` segurado esconde máscara** (keydown/keyup, não toggle). Atalhos desabilitados quando modal/input ativo — usar `isTextFocused() || isModalOpen()`.
- **Helpers compartilhados:** `hexToRgb`, `blobToImage`, `escapeHtml` vivem em `utils.js`. Não redefinir.
- **Paleta de classes:** contrastante entre si e visível sobre imagens de satélite. Evitar verde e tons escuros.
- **Sem `innerHTML` com dados de usuário.** Usar `textContent` ou `escapeHtml` de `utils.js`.
- **404 de `maplibre-gl.js.map` é benigno.** O vendor (`frontend/vendor/maplibre/`) inclui apenas `.js` e `.css`, não o `.map`. A diretiva `//# sourceMappingURL=maplibre-gl.js.map` no fim do `.js` faz o Chrome DevTools buscar o source map automaticamente (só quando DevTools está aberto). Chrome às vezes duplica o prefixo (`/static/static/…`) ao tentar caminhos alternativos — ruído do browser, não bug do server. Ignorar no log; ou baixar o `.map` ao lado do `.js` se incomodar.

## UX — inegociáveis

- Login → pintar em < 5s. Evitar modais/confirmações desnecessárias.
- **Pré-carregar próximo tile:** `/api/tiles/next-preview` (peek sem atribuir) enquanto operador pinta o atual. Cache em `preloadedNext` é invalidado se o tile realmente atribuído em `/next` for diferente.
- Após submeter: tela limpa imediatamente (mask zerada, canvas coberto pela `idle-screen` "Tile enviado ✓"), flash verde fica por baixo da overlay. Operador clica para pedir o próximo tile — sem auto-avanço, pra ele ter um respiro entre cartas.
- Pixels faltantes ao submeter: pisca vermelho 2s sobre o canvas.
- **Pixels faltantes sempre visíveis:** linha `⚠ Faltam N pixels` (`#missing-line`) na sidebar enquanto incompleto; some em 0.
- **Botão de submit auto-explicativo:** quando incompleto, troca para label `"Faltam N px"` com estilo `.incomplete` (laranja-aviso). Em tile `in_review`, label vira `"Aprovar revisão"`.
- **Modo sempre visível:** pill `#mode-pill` no header — azul `CLASSIFICAR` ou laranja `REVISAR`. Atualizado em `loadTile()` junto com o `review-banner`.
- **Barra de progresso:** `#progress-bar-fill` (gradiente azul→verde) reflete `filledCount/65536`.
- **Atalhos visíveis:** badges `<span class="kbd">` inline nos botões (Tools/Undo/Redo/Submit) + seção `"Atalhos"` persistente no sidebar com as combinações principais + link "Ver todos" abrindo modal completo.
- Backup do `Uint8Array` em `localStorage` durante edição. No F5 (ou reabrir o tile), `tryRestoreBackup()` compara com a máscara do servidor e, se diferirem, sobrescreve a in-memory silenciosamente — toast rápido `"Trabalho local restaurado."` como feedback. Sem banner de confirmação: o backup só é gravado durante pintura ativa e é apagado no submit, então só difere do servidor exatamente nos casos em que o usuário tinha trabalho não-submetido.
- **JWT refresh proativo:** `api.js` decodifica o `exp` do token e agenda refresh 60s antes de expirar. Além disso, re-tenta automaticamente no 401 (reativo). O usuário não deve ver logout por expiração durante uso contínuo.

## Design system (CSS)

- **Tokens em `:root`** (`style.css`): `--bg-0…bg-4`, `--accent`, `--ok`, `--warn`, `--err`, `--info`, `--space-1…6`, `--radius*`, `--shadow*`, `--header-h`, `--sidebar-w`, `--minimap-w`. Sempre via `var()`, nunca hex hardcoded em componentes.
- **Responsive breakpoints:** 1180px (encolhe sidebar/minimap), 960px (minimap vira faixa horizontal abaixo do canvas), 720px (single-column, sidebar vira scroll-snap horizontal), 480px (stats grid 1-col). `prefers-reduced-motion` respeitado.
- **Status chips** (`.chip.pending/in_progress/classified/in_review/reviewed/problem`) disponíveis para tabelas admin.
- **Spinner de loading** (`.loading`) e `.loading-text` padrão.

## Segurança

- Senhas: bcrypt cost ≥ 12.
- Validar no backend: 65536 bytes exatos, valores em `{1..6, 255}`, submetedor == `assigned_to`.
- Endpoints admin atrás de `Depends(auth.require_admin)` que verifica `role == 'admin'`.
- Nunca `innerHTML` com dado de usuário — `textContent` ou `escapeHtml` de `utils.js`.

## Testes

Três camadas. Todas devem passar antes de commitar:

### Backend (`tests/*.py`, pytest + TestClient) — 175 testes
- Fixture `app_env` em `conftest.py` monkey-patcha o DB para arquivo temporário. Precisa patchar `get_config` em **cada módulo** que fez `from .config import get_config` (referência é copiada no import).
- Fixtures: `admin_user`, `operators` (3), `operators_10`, `tiles` (10 pending), `tiles_many` (100).
- Rate limit entre logins: `auth.reset_rate_limits()` antes de cada login quando >5 no mesmo teste.
- Concorrência crítica: `test_concurrent_10_operators_no_duplicates` (10 threads pending), `test_10_reviewers_race_on_classified_queue` (9 reviewers simultâneos na fila de revisão).
- Invariantes de domínio com teste próprio: `test_auth_invariants` (bcrypt cost≥12, JWT TTL 8h/24h, forgery, typ access↔refresh, escalação via claim), `test_authz_crossuser` (bypass de `/next`, POST cross-user, gate admin), `test_state_machine` (todas transições + ilegais), `test_rate_limit_real` (sem reset), `test_dashboard_real` (atua antes de conferir — não-tautológico, inclui `test_dashboard_avg_durations_split_by_action` que faz backdate do `action_log` e valida pareamento assign→classify/review), `test_mask_roundtrip_strong` (padrões não-uniformes).
- **Invariantes geométricas (`test_geo.py` — 14 testes):** tile sempre 640m×640m em qualquer latitude (equador → -70°), pixel = 2.5m em ambos eixos, bbox simétrica ao centro, adjacência sem gap, bloco 3x3 cobre exatamente 1920m, `from_bounds` do rasterio bate com 2.5m.
- **Rasters mundiais (`test_raster_worldwide.py` — 22 testes):** 20 pontos (equador, trópicos, NY, Tóquio, Moscou, Tromsø, McMurdo/Antártica) geram GeoTIFF real pela mesma pipeline do `export_tiles._write_geotiff`, reabrem com `rasterio` e validam CRS=EPSG:4326, bounds, transform, pixel em metros, e `dataset.xy()` retornando ao centro. Um teste reverso prova que **span em graus encolhe com a latitude** — se voltar alguma aproximação esférica, quebra.
- **PROJ no Windows:** existem 3 instalações concorrentes (PostgreSQL/PostGIS, pyproj, rasterio) com versões diferentes de `proj.db`. `test_raster_worldwide.py` força `PROJ_LIB=PROJ_DATA=<rasterio>/proj_data` no topo do arquivo **antes** de qualquer op que toque CRS.

### Frontend unit (`tests/frontend/*.test.js`, Vitest + jsdom) — 57 testes
- `mask-core.test.js` testa `frontend/js/mask-core.js` (paint, Bresenham sem gaps, floodFill com fronteira, undo/redo byte-exato com Uint32Array, `screenToLogical` com rect deslocado/esticado, `validateSubmission` espelhando backend).
- `api-refresh.test.js` mocka `fetch`, usa fake timers: refresh proativo 60s antes de expirar, retry reativo no 401, Bearer header, `apiPostBytes` octet-stream, erro traz `status`+`body`.
- `utils.test.js`, `localstorage-backup.test.js` (round-trip Uint8Array↔base64↔JSON, rejeita tile diferente).
- **Regra:** toda lógica pura nova vai em `mask-core.js` e tem teste — NÃO no `editor.js` (que fica com orquestração DOM/canvas, não testável).

### E2E (`tests/e2e/runner.mjs`, Puppeteer + uvicorn real) — 7 cenários
- `runner.mjs` sobe uvicorn em porta 18766 com config/DB temporários, faz seed via `seed.py`, roda Puppeteer headless.
- Cada cenário usa **novo BrowserContext incógnito** (`browser.createBrowserContext()`) para isolar localStorage/cookies.
- **Bloqueia requests externas** (MapLibre CDN, TileServer) via `page.setRequestInterception` — senão puppeteer trava esperando tiles inalcançáveis.
- Hook de teste: `?test=1` expõe `window.__tcTest__` (mask, currentTile, filledCount, undo/redo len, maskHidden, preloadedNextId). **Nunca adicione lógica de produção que dependa dele.**
- Cenários: login<5s, paint+submit+next, submit incompleto bloqueado, Space esconde máscara, Ctrl+Z/Y, preview idempotente, login inválido.
- `TILECLASS_DISABLE_RATE_LIMIT=1` passado ao uvicorn (senão 6º login trava a suite).

### Pontos críticos ao editar testes
- **Nunca mocke o DB** — use `app_env`. Mocks mascararam bugs de migração no passado.
- **Não use asserções só de status code** — valide o efeito colateral (decode_mask, SELECT, estado real). Tests tautológicos reduzem cobertura percebida sem segurança.
- **Concorrência sempre com `threading.Barrier`** — sem barreira, threads serializam e a race nunca ocorre.
- **Incognito per-test no E2E** — pages no mesmo contexto compartilham localStorage; sem isolamento, token de um teste vaza para o próximo.

## Git

**Nunca commitar sem o usuário pedir.** Revisão manual de todas as mudanças.
