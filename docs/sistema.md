# TileClass — Arquitetura e fluxos

Descrição do sistema para quem quer entender **o que** o TileClass é, **como** está organizado, e **por que** algumas decisões foram tomadas. Para regras acionáveis ao editar código, ver [`CLAUDE.md`](../CLAUDE.md). Para o catálogo exaustivo de funcionalidades (endpoints, códigos de erro, atalhos), ver [`FUNCIONALIDADES.md`](FUNCIONALIDADES.md). Schema canônico em `backend/database.py` (constante `SCHEMA`); endpoints em `backend/routers/`.

## 1. O que é

TileClass é uma aplicação web para anotação de tiles de imagens de satélite. Um operador recebe um tile por vez de uma fila e produz um dataset de treinamento (máscara pixel-a-pixel, linhas vetoriais, classe global, ou bounding boxes, dependendo do tipo de projeto). O resultado é exportado em formatos geoespaciais (GeoTIFF, GeoJSON, CSV) para alimentar modelos de visão computacional.

**Escopo alvo:** ~1000 tiles por projeto, até 10 operadores simultâneos, intranet (sem deploy multi-tenant em nuvem). Single-process. SQLite WAL como banco único.

**Por projeto:** classes, paleta de cores, mbtiles de imagem (primária + opcionais), duas máscaras de referência, geometria do tile (`tile_px` × `meters_per_pixel`), flag `mask_complete_required`, e o conjunto de membros (operadores + revisores). Tiles, dashboard e fila são todos escopados por `project_id`. **Uma instalação nova começa vazia** — sem projeto, sem classes. O admin cria e configura o primeiro projeto pela UI admin ou via `POST /api/admin/projects`.

A fonte de verdade do domínio são as tabelas `projects` / `project_classes` / `project_attributes` / `project_members`. O `config.yaml` carrega **apenas infra/segredos** (`database.path`, `auth`, `mask_overlay`) — nada de domínio, porque dado de domínio é configurável pela UI admin.

## 2. Stack

- **Backend:** Python 3.11+, FastAPI, Uvicorn, SQLite (sqlite3 nativo — sem ORM), PyJWT, bcrypt, Pillow, NumPy, PyYAML, rasterio (export), pyproj (geodésica WGS84).
- **Frontend:** Vanilla JS (sem framework), Canvas HTML5, fetch API. MapLibre GL JS (vendored localmente em `frontend/vendor/maplibre/`) para renderização georreferenciada dos tiles XYZ. Servido como estático pelo FastAPI.
- **Testes:** pytest + httpx (backend); Vitest + jsdom (frontend unit); Puppeteer + uvicorn real (E2E).
- **Override de config por env:** `TILECLASS_CONFIG=<path>` (E2E). Rate limit desligável via `TILECLASS_DISABLE_RATE_LIMIT=1` (E2E apenas — nunca em produção).

## 3. Estrutura de pastas

```
tileclass/
├── backend/
│   ├── main.py                 # FastAPI app + lifespan + middleware + static SPA
│   ├── routers/                # FastAPI APIRouters (registrados no main.py)
│   │   ├── auth.py             # /api/auth/{login,refresh,me,logout}
│   │   ├── projects.py         # /api/projects + /api/projects/{id} + XYZ
│   │   │                       # + admin /api/admin/projects[...] CRUD/classes/atributos/membros/export/tiles
│   │   ├── operator.py         # /api/tiles/* + /api/me/stats-today
│   │   └── admin.py            # /api/admin/* (dashboard, distribuições, tiles, mapa, problemas, users, manutenção, bulk, export-jobs)
│   ├── auth.py                 # JWT, bcrypt, rate limit, blacklist, role middleware
│   ├── models.py               # Schemas Pydantic
│   ├── database.py             # Conexão SQLite (WAL), schema, transaction(), log_action
│   ├── config.py               # Carrega config.yaml (singleton cache)
│   ├── project_service.py      # CRUD de projetos + classes + membros, cache + path validation, require_membership
│   ├── tile_ingest.py          # add_points(): centros (lat,lon) → tiles pending — compartilhado entre UI admin e CLI
│   ├── export_service.py       # export sync (export_zip) + jobs assíncronos
│   ├── tile_service.py         # Fila, atribuição atômica, submit, problema, pause/resume — escopados por project_id
│   ├── admin_service.py        # Fachada — re-exporta de backend/admin/
│   ├── admin/
│   │   ├── dashboard.py        # /admin/dashboard + _cycle_durations (CTE pause/resume aware)
│   │   ├── tiles_query.py      # list_tiles, count_tiles, list_tiles_map, list_problems
│   │   ├── tiles_mutations.py  # reset/problem/re-review/assign/unassign/pause/delete/block/unblock (atômicos)
│   │   ├── users.py            # list/create + can_review/role/active toggles
│   │   └── thumbnails.py       # tile_thumbnail (mask colorizada) + tile_satellite_thumbnail
│   ├── mask_utils.py           # Uint8Array ↔ PNG "L" + validação
│   ├── mask_tile_service.py    # Cache mbtiles do overlay admin (rasteriza máscaras → XYZ)
│   ├── mbtiles_service.py      # Pool LRU de readers por (project_id, layer)
│   ├── geo.py                  # bbox_from_center, offset_center (pyproj.Geod WGS84)
│   ├── tile_grid.py            # Helpers Web Mercator (build_mbtiles, import_cq)
│   ├── vector_utils.py         # parse/validate de GeoJSON + topologia (vector)
│   ├── detection_utils.py      # parse/validate de bboxes (detection)
│   └── scripts/                # create_admin, import_points, export_*, build_mbtiles, merge_db, verify_db, backup_db, recolor_mbtiles, recompute_class_counts
├── data_external/              # MBTiles grandes (gitignored)
├── frontend/
│   ├── index.html              # SPA (login / editor / admin)
│   ├── css/style.css           # Tokens em :root + breakpoints
│   ├── vendor/maplibre/        # CDN local
│   └── js/
│       ├── app.js              # Router SPA por role
│       ├── api.js              # Fetch wrapper + JWT refresh (reativo + proativo)
│       ├── editor.js           # Raster: canvas, ferramentas, undo/redo, submit
│       ├── editor-vector.js    # Vector: MapLibre interactive + caneta + atributos
│       ├── editor-classification.js  # Classification: botões de classe
│       ├── editor-detection.js # Detection: arrastar retângulo
│       ├── mask-core.js        # Lógica PURA raster (testável sem DOM)
│       ├── vector-core.js      # Lógica PURA vector
│       ├── detection-core.js   # Lógica PURA detection
│       ├── backup.js           # localStorage backup
│       ├── admin.js            # Orquestrador admin (7 abas)
│       ├── admin/              # Submódulos: dashboard, modals, projects
│       ├── maplib.js           # Helpers MapLibre (createLockedMap, setMapBbox)
│       ├── utils.js            # hexToRgb, blobToImage, escapeHtml
│       └── toast.js            # Notificações
├── docs/
│   ├── sistema.md              # Este arquivo
│   └── FUNCIONALIDADES.md      # Catálogo exaustivo (endpoints, errors, atalhos)
├── tests/
│   ├── (backend)               # pytest + TestClient
│   ├── frontend/               # Vitest + jsdom
│   └── e2e/runner.mjs          # Puppeteer + uvicorn real
└── requirements.txt
```

**Convenção de paths grandes:** `.mbtiles` (GBs) ficam em `data_external/` na raiz; o `config.yaml` aponta com `../data_external/<arquivo>.mbtiles` (relativo a `backend/` por convenção do `_open_optional` no `main.py`). Mantém o pacote `backend/` enxuto.

## 4. O modelo de projetos

Cada projeto define seu próprio mundo de classificação. As tabelas-chave são `projects`, `project_classes` (PK `(project_id, class_id)`) e `project_members` (PK `(project_id, user_id)` com role `operator|reviewer|admin`). `tiles.project_id` é `NOT NULL` e referencia `projects(id)`. Schema completo em `backend/database.py` (constante `SCHEMA`).

### 4.1 Layers (background do editor)

Cada slot aceita path mbtiles OU URL de tile-server remoto:

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
- **Esquema `bingmaps://{z}/{x}/{y}`**: Bing Maps usa quadkeys em vez de z/x/y, então o frontend (`maplib.js` → `tileTransformRequest`) reescreve `bingmaps://` para o endpoint quadkey do `virtualearth.net` on-the-fly. O backend trata como remoto (pass-through, sem proxy).

`project_service.is_remote_layer(value)` é o ponto único que classifica remoto vs. arquivo (prefixos `http://`, `https://`, `bingmaps://`); `layer_path(proj, layer)` é o único ponto de leitura. Layers ausentes não registram atalho nem aparecem na sidebar/cheat-sheet — `refreshShortcutsBadges()` esconde os badges via `data-overlay-key` no HTML. `ref_*` são overlays de referência (raster categorizado, não-editável); ao serem segurados, escondem a máscara do operador (`HIDE_MASK_OVERLAYS`).

### 4.2 Membership e bloqueio

- Operador só vê tiles do projeto onde é membro. Admins globais veem todos.
- `users.can_review` virou um veto temporário: `False` impede review queue mesmo se a membership for `reviewer`. Será removido quando a UI completar a migração para roles por projeto.
- **Soft-disable:** `PATCH /api/admin/projects/{id}` com `active=False` mantém todos os dados mas `/next` e `/next-preview` retornam 409 `project_inactive`. Stats/dashboard continuam respondendo. Operador termina o tile que já tem antes do bloqueio.
- **Hard-delete:** `DELETE /api/admin/projects/{id}` só funciona se o projeto não tem tiles. Caso contrário 409 `project_has_tiles` — desative em vez de excluir.

### 4.3 Fluxo do editor

1. `apiGet("/api/projects")` ao login. Único projeto → seleção automática; múltiplos → dropdown `#project-picker` no header (persistido em `localStorage["tileclass_active_project_id"]`).
2. `apiGet("/api/projects/{id}")` carrega `classes`, `mask_complete_required`, e `layers` com URLs prontas (`/api/projects/{id}/xyz/{layer}/{z}/{x}/{y}.{ext}`).
3. Toda chamada de fila (`/api/tiles/next`, `/next-preview`, `/queue-stats`, `/me/stats-today`, `/tiles/assigned`) passa `?project_id=<id>`. Quando o usuário tem só uma membership o param pode ser omitido; com múltiplas, o backend devolve 400 `project_id_required`.

### 4.4 Migração de DBs pré-projetos

A migração (idempotente, em `database.py`) cria um projeto "default" hardcoded — nome `default`, paleta padrão `_MIGRATION_CLASSES` de 6 classes, layers vazios — **não** lido do `config.yaml`. Serve só para dar um lar aos tiles órfãos (cujas máscaras podem referenciar os ids 1..6) e é totalmente editável depois pela UI. Todos os tiles existentes recebem `project_id=<default>`; usuários ativos viram membros com role derivada de `role+can_review`. A coluna é tornada `NOT NULL` via tabela espelho — o rebuild roda com `foreign_keys=OFF` numa transação (a tabela `action_log` referencia `tiles` por FK, então o `DROP TABLE` falharia com FKs ligadas) e é resumível após falha parcial (guard checa `project_id` ser NOT NULL, não só existir).

**DBs novos NÃO são semeados** — o `SCHEMA` já cria `tiles.project_id NOT NULL`, então o branch de migração não roda e a instalação fica vazia (admin cria o 1º projeto pela UI).

### 4.5 Pool de mbtiles readers

`mbtiles_service` mantém um LRU de até 32 readers, chaveado por `(project_id, layer)`. Editar paths via admin invalida o reader correspondente. Arquivos inválidos/corruptos são tolerados (reader fica `closed`, layer some do payload). URLs remotos não consomem slots do pool — só paths mbtiles. Lifespan pré-aquece o reader `primary` de cada projeto ativo (apenas mbtiles locais) para evitar latência na primeira requisição.

### 4.6 Mask overlay (admin)

`mask_tile_service` é per-projeto. Endpoint `/api/admin/mask-tiles/{project_id}/{z}/{x}/{y}.png`. Cache em arquivo separado `<base>_p<project_id>.mbtiles` para garantir que paletas/conjuntos de tiles de projetos diferentes não compartilhem rows. LUT vem de `project_classes[project_id]`. Invalidações (`safe_invalidate_tile`/`safe_invalidate_bbox(project_id, ...)`/`safe_invalidate_tiles`) buscam o `project_id` da tile e direcionam para o cache file correto. `mask_tile_service.get_tile()` despacha por kind (raster colore pixel-a-pixel; vector rasteriza linhas; classification desenha retângulo + texto; detection desenha caixas por classe).

## 5. Os quatro kinds

Projetos têm `kind ∈ {raster, vector, classification, detection}` (default raster, **imutável após criação**). Cada kind define corpo do tile, editor, validação e export próprios; compartilham o mesmo shell de UI e a mesma fila/máquina de estados.

### 5.1 Raster (default)

Máscara pixel-a-pixel. Operador pinta cada pixel com uma classe de `project_classes`. Body é PNG single-band uint8 (`tile_px²` bytes), validado contra a paleta + flag `mask_complete_required` (rejeita 255 quando True). Caso de uso: cobertura de solo, semantic segmentation.

Wire protocol: frontend envia **raw bytes** (Uint8Array, `tile_px²`) com `Content-Type: application/octet-stream`; backend resolve `tile_px` via `project_for_tile()` antes de validar tamanho.

### 5.2 Vector

Anotações de linhas com atributos — drenagem (com direção/conectividade), rodovias (com pavimento/faixas), etc. Body é GeoJSON FeatureCollection com LineStrings + properties. Schema de domínio em `project_attributes` (key+label+type+required+options); tipos `text`/`number`/`enum`/`boolean`.

| Camada | Raster | Vector |
|---|---|---|
| Body do tile | `tiles.data_png` (PNG) | `tiles.data_geojson` + `feature_count` |
| Schema de domínio | `project_classes` | `project_attributes` |
| Validação no submit | `mask_complete_required` + IDs em paleta | GeoJSON parsável + attributes + `topology_required` |
| Submit body | `application/octet-stream` (tile_px² bytes) | `application/json` (FeatureCollection) |
| Endpoint de leitura | `GET /api/tiles/{id}/image` (PNG) | `GET /api/tiles/{id}/features` (JSON) |
| Editor | `editor.js` (canvas) | `editor-vector.js` (MapLibre interactive) |
| Overlay admin | rasteriza máscaras | rasteriza LineStrings (`_render_vector_tile`) |
| Distribuição (dashboard) | `class_distribution` (pixel counts) | `feature_distribution` (counts por enum/boolean) |
| Export CLI | `export_tiles.py` (GeoTIFF) | `export_features.py` (GeoJSON) |

**Atributos:** keys snake_case; `enum` precisa ≥2 `options`. Adicionar/renomear/relabel é livre; remover só quando nenhuma feature do projeto referencia a chave (`attribute_in_use` 409). Para drenagem com `topology_required=True`, a chave `direction ∈ {forward, reverse, both}` é obrigatória por feature.

**Validação de topologia** (`vector_utils.validate_topology`): só roda quando `topology_required=True`. Cada LineString precisa ter `direction`; endpoints próximos (≤ `SNAP_TOLERANCE_DEG ≈ 1.5e-5`, ~1m) snapam ao mesmo nó; ciclos via DFS (forward/reverse/both definem a direção das arestas) são rejeitados. Validação é **por tile** — drenagens cruzando bordas viram features distintas em tiles distintos; reconexão cross-tile é responsabilidade do consumer.

**Editor vetorial:**
- **Pen (P):** clique adiciona vértice; duplo-clique encerra; ESC cancela. Snap automático no primeiro/último vértice se estiver perto de um endpoint existente.
- **Select (V):** clique numa feature → painel de atributos à direita. Edição de vértices: arraste um handle p/ mover, clique no ponto-médio (amarelo) p/ inserir, Del remove o vértice destacado (ou a feature inteira se nenhum vértice estiver selecionado; recusa abaixo de 2). Lógica pura em `vector-core.js` (`moveVertex`/`insertVertex`/`removeVertex`).
- **Undo/redo:** Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z. 50 frames.
- O painel de atributos é schema-driven; campo extra "direção" aparece quando `topology_required`. Required marcado com `*`; submit bloqueia se faltar.

**Pause vs submit:** pause persiste o body do jeito que está (qualquer GeoJSON parsável, mesmo com required ausente ou ciclos), submit valida tudo. Operador pode pausar parcial sem perder trabalho.

### 5.3 Classification

Operador escolhe **uma classe** do projeto pra todo o tile — sem pintura, sem desenho. Caso de uso: rotular tiles inteiros (ex.: cobertura predominante).

| Camada | Raster | Classification |
|---|---|---|
| Body do tile | `tiles.data_png` | `tiles.data_class_id` (INTEGER) |
| Validação no submit | máscara completa + IDs em paleta | `class_id ∈ project_classes` |
| Submit body | `application/octet-stream` (tile_px² bytes) | `application/json` (`{"class_id": int}`) |
| Endpoint de leitura | `GET .../image` (PNG) | `GET .../classification` (`{class_id}` ou 204) |
| Editor | `editor.js` (canvas) | `editor-classification.js` (botões de classe) |
| Pause | salva máscara parcial | **409 `pause_not_supported`** — fluxo single-click |
| Overlay admin | colore pixel a pixel | retângulo translúcido + nome da classe centrado |
| Distribuição | `class_distribution` (pixel counts) | `tile_class_distribution` (1 tile = 1 contagem) |
| Export CLI | `export_tiles.py` (GeoTIFF) | `export_classifications.py` (CSV único) |

**Editor:** sidebar mostra um botão por classe (cor + nome). Operador clica → highlight; rodapé Submit habilitado → modal confirm → POST. Sem undo/redo (escolha é atômica). Heartbeat e state machine iguais aos outros.

**Tile_px em classification:** afeta só thumbnail/overlay rasterizado (não há body de pixels). `tile_meters = tile_px × meters_per_pixel` continua definindo a bbox.

### 5.4 Detection

Operador desenha **caixas (bounding boxes)** ao redor de objetos de interesse, cada caixa com **uma classe** do projeto. Caso de uso: dataset de detecção de objetos. Reaproveita a infra vetorial (corpo em `data_geojson`), mas com geometria de retângulo + classe por caixa em vez de linha + atributos.

| Camada | Vector | Detection |
|---|---|---|
| Geometria | `LineString` (≥2 vértices) | `Polygon` retângulo alinhado aos eixos |
| Schema de domínio | `project_attributes` (por feature) | `project_classes` — `properties.class_id` por caixa |
| Validação no submit | attributes + `topology_required` | `class_id ∈ project_classes` + `box_required` (≥1 caixa) |
| Flag por projeto | `topology_required` | `box_required` (0 = tile vazio é negative sample válido) |
| Caches | `feature_count` | `feature_count` + `class_counts` (caixas por classe) |
| Editor | `editor-vector.js` (caneta) | `editor-detection.js` (arrastar retângulo; B/V toggles) |
| Overlay admin | rasteriza linhas | `_render_detection_tile` (retângulos coloridos) |
| Distribuição | `feature_distribution` | `class_distribution` (reusa `class_counts`) |
| Export CLI | `export_features.py` | `export_detections.py` (GeoJSON; `box_count` no manifest) |

**Compartilha com vector:** leitura em `GET /api/tiles/{id}/features` (FeatureCollection), body JSON até 1.5MB, pause salva parcial (só checa parse), `detection-core.js` reusa os helpers de undo/redo do `vector-core.js`.

**`box_required`:** quando `True`, submit rejeita 0 caixas (`invalid_boxes`); quando `False`, tile vazio classifica normalmente (negative sample). Editor espelha o gate em `validateForSubmit`.

**Validação (`detection_utils`):** `parse_detection` (estrutural — cada feature é Polygon retângulo alinhado), `validate_class_ids` (membership na paleta), `validate_submission` (estrutural → class_ids → gate box_required). Tudo puro/sem DB, espelhado em `detection-core.js`.

### 5.5 Mutual exclusion no payload

`POST /api/admin/projects` rejeita `attributes` em projeto raster/classification/detection (400 `attributes_not_supported`) e `classes` em projeto vector (400 `classes_on_vector`). Tentar mudar `kind` via PATCH é silenciosamente ignorado (campo fora do allow-list — invariante de imutabilidade).

## 6. Geometria do tile

`projects.tile_px` (qualquer inteiro 1..4096) × `projects.meters_per_pixel > 0`; `tile_meters = tile_px × meters_per_pixel`. Default seed = 256 × 2.5 = 640 m. Admin escolhe livre — `tile_px=226` é tão válido quanto 256.

Cada tile é definido **pelo centro geodésico**; `geo.bbox_from_center(lat, lon, tile_meters)` usa `pyproj.Geod` (WGS84) para calcular ±tile_meters/2 em cada direção cardeal. Schema de `tiles` tem apenas `bbox_*`; `zoom`/`tile_x`/`tile_y`/`context_tiles` foram removidos.

**Imutável após o primeiro tile** — `update_project` rejeita mudanças com 409 `tile_geometry_locked`. Backend e scripts (import/export) lêem da row do projeto via `project_service.get_project()`.

**Adjacência sem gap:** `offset_center(lat, lon, dx, dy, tile_meters)` caminha `dx*tile_meters` e `dy*tile_meters` por geodésica, garantindo que tiles vizinhos do `--block NxN` compartilhem arestas exatamente (gap < 1 mm, validado por teste em `test_geo.py`).

## 7. Máquina de estados do tile

`pending → in_progress → classified → in_review → reviewed`. Qualquer estado pode ir para `problem`. `problem → pending` e `reviewed → in_review` são transições de admin. Admin pode também bloquear via `pending|classified|reviewed → blocked` (guardando o status original em `blocked_from`) e desbloquear via `blocked → <blocked_from>`. `in_progress`/`in_review`/`problem` **não podem** ser bloqueados. Tiles bloqueados são naturalmente excluídos das filas porque os SELECTs de `/next` filtram por `status='pending'` ou `'classified'`.

Detalhes de cada transição (autoria, timestamp, lock otimista) em `backend/tile_service.py` e nos testes `test_state_machine.py`.

## 8. Editor — três camadas sobrepostas

O editor compõe três camadas no mesmo espaço:

1. **MapLibre GL** renderiza o satélite XYZ como `div` georreferenciado ao fundo (`interactive: false`, bbox via `fitBounds`). MapLibre resolve composição de tiles automaticamente: quando a bbox não alinha com um XYZ único, carrega múltiplos tiles e recorta.
2. **Canvas da máscara** com `globalAlpha` controlável.
3. **Canvas de cursor/interação** no topo.

Eventos de mouse vão na camada 3 (`pointer-events: auto`); as outras têm `pointer-events: none`.

**Uint8Array como fonte de verdade:** o canvas é só visualização. Mudar o array, depois chamar `writeMaskPixels(x0,y0,x1,y1)` + `blitMask()`.

**Mapeamento de coordenadas (ponto mais frágil):** `getBoundingClientRect()` → fração → `Math.floor(frac * tile_px)` com clamp em `[0..tile_px-1]`. O canto inferior-direito deve dar `(tile_px-1, tile_px-1)`.

## 9. GT Extractor (interface canônica)

Os 4 exportadores são a interface para outras aplicações/agentes consumirem dados finalizados — um por kind, todos com a mesma assinatura `run(out_dir, *, status, project_id, **opts)` (o `main()`/CLI é wrapper fino sobre ela):

- `export_tiles` (raster → GeoTIFF)
- `export_features` (vector → GeoJSON)
- `export_classifications` (classification → CSV)
- `export_detections` (detection → GeoJSON bboxes)

**Formato raster** (cada tile: `<out_dir>/gt_<tile.name>.tif`):
- GeoTIFF single-band uint8, `tile_px × tile_px`, **EPSG:4326**, compressão `deflate`, **NODATA = 255**.
- Transform via `rasterio.transform.from_bounds(west, south, east, north, tile_px, tile_px)` — pixel ≈ `meters_per_pixel` na latitude do centro.
- Reprojeção para 3857/UTM fica a cargo do consumer (`gdalwarp` / `rasterio.warp`).

**Multi-projeto:** `--project <id|name>` filtra a exportação. Sem o flag, todos os projetos vão para o mesmo `out_dir` (manifest distingue via coluna `project_id`). O remap EDGV foi pensado para o projeto seed de 6 classes — projetos com paletas diferentes devem usar `--raw` para preservar IDs nativos.

**Remap canônico de classes** (default no projeto seed; `--raw` desativa):

| TileClass id | Nome (seed) | EDGV id | Nome EDGV (treinamento_6c) |
|:-:|---|:-:|---|
| 1 | Massa d'água | **0** | agua |
| 2 | Área edificada | **1** | edif |
| 3 | Floresta | **4** | floresta |
| 4 | Campo | **3** | campo |
| 5 | Cultivo | **5** | veg_cultivada |
| 6 | Terreno exposto | **2** | terr_exp |
| 255 | NODATA | **255** | NODATA (ignore_index) |

A LUT (`EDGV_REMAP_LUT`) é constante de módulo no topo do `export_tiles.py` — único ponto de verdade para o mapeamento. Indexes não usados (0, 7..254) passam como identidade.

**Filtros de status** (`--status`, idênticos nos 4 exportadores e no endpoint da UI):
- `reviewed` (padrão) — só tiles que passaram pela revisão (GT estritamente aceito).
- `classified` — **só** tiles classificados ainda **não** revisados (o lote pendente de revisão).
- `reviewed+classified` — ambos. Útil para dataset preliminar/maior, ciente do risco de inconsistência.

**Export pela UI (admin):** `GET /api/admin/projects/{id}/export?status=reviewed|classified|reviewed_classified` (admin-only) despacha pelo `kind` do projeto, gera num tempdir, **zipa** e faz stream como download. `backend/export_service.py` é o ponto único que mapeia o status da API (`reviewed_classified` com underscore) para a chave dos scripts (`reviewed+classified`) e chama o `run()` certo. Painel "Exportar dados" no detalhe do projeto.

**Export assíncrono (muitos dados):** `POST /api/admin/projects/{id}/export-jobs?status=...` cria um job (tabela `export_jobs`), constrói o ZIP em disco num worker em thread (baixa memória, sem segurar a requisição), e o admin faz poll em `GET /api/admin/export-jobs/{job_id}` e baixa via `.../download`. A UI usa esse fluxo. Limitação: o worker é uma thread no processo que criou o job — em multi-worker o download deve ir ao mesmo worker (ok para deploy single-process/intranet).

**Manifest (`<out_dir>/manifest.csv`):** uma linha por tile exportado com `filename, tile_id, project_id, name, status, classified_by, reviewed_by, classified_at, reviewed_at, bbox_*`. É o ponto de entrada para outros agentes saberem o que receberam (status, autoria, projeto, geometria) sem precisar abrir o GeoTIFF.

**Quando estender:** se um consumer precisar de outro CRS, formato (PNG/Zarr), ou subset (por bbox/operador/data), adicione flags ao mesmo script — não crie novo extractor. Manter um único ponto de saída evita drift entre consumers.

## 10. Ingestão de tiles

`POST /api/admin/projects/{id}/tiles` com `{points:[{lat,lon,name?}], block}` cria tiles `pending` a partir de centros geodésicos (geometria do projeto, bloco NxN, dedup por bbox). `backend/tile_ingest.add_points` é o ponto único, **compartilhado com o CLI `import_points`**. Painel "Adicionar tiles" (ponto + bloco + CSV parseado client-side) no detalhe do projeto.

## 11. UX — decisões de design

- **Login → pintar em < 5s.** Evitar modais/confirmações desnecessárias.
- **Pré-carregar próximo tile:** `/api/tiles/next-preview` (peek sem atribuir) enquanto operador pinta o atual. Cache em `preloadedNext` é invalidado se o tile realmente atribuído em `/next` for diferente.
- **Após submeter:** tela limpa imediatamente (mask zerada, canvas coberto pela `idle-screen` "Tile enviado ✓"), flash verde fica por baixo da overlay. Operador clica para pedir o próximo tile — sem auto-avanço, pra ele ter um respiro entre cartas.
- **Pixels faltantes ao submeter:** pisca vermelho 2s sobre o canvas. Sempre visível na sidebar: `⚠ Faltam N pixels` enquanto incompleto; some em 0.
- **Botão de submit auto-explicativo:** quando incompleto, troca para label `"Faltam N px"` com estilo `.incomplete`. Em tile `in_review`, label vira `"Aprovar revisão"`.
- **Modo sempre visível:** pill `#mode-pill` no header — azul `CLASSIFICAR` ou laranja `REVISAR`.
- **Barra de progresso:** `#progress-bar-fill` (gradiente azul→verde) reflete `filledCount/tile_px²`.
- **Atalhos visíveis:** badges `<span class="kbd">` inline nos botões + seção `"Atalhos"` persistente no sidebar + modal "Ver todos".
- **Backup do `Uint8Array` em `localStorage`** durante edição. No F5, `tryRestoreBackup()` compara com a máscara do servidor e, se diferirem, sobrescreve a in-memory silenciosamente. Backup é apagado no submit.
- **JWT refresh proativo:** `api.js` decodifica o `exp` do token e agenda refresh 60s antes de expirar; também re-tenta automaticamente no 401 (reativo). O usuário não deve ver logout por expiração durante uso contínuo.

## 12. Atalhos do editor (raster)

| Tecla | Ação |
|---|---|
| 1–6 | Classe ativa |
| Q / W / E | Pincel / Borracha / Balde |
| A / S | Pincel menor / maior |
| Z / X | Opacidade − / + |
| C | Pular para próximo pixel faltante |
| F | Highlight pixels faltantes (toggle) |
| D / R | Imagem secundária / terciária (segurar) |
| T / Y | Máscara de referência 1ª / 2ª (segurar) |
| Espaço (segurar) | Esconder máscara |
| Ctrl+Z | Desfazer |
| Ctrl+Y / Ctrl+Shift+Z | Refazer |

Atalhos não disparam quando há input/modal em foco. Vector/classification/detection têm atalhos próprios — ver [`FUNCIONALIDADES.md`](FUNCIONALIDADES.md).

## 13. Notas operacionais

- **`venv` no Windows:** sempre ative antes de rodar `uvicorn`/`pytest`/scripts. Alternativa sem ativar: `.\.venv\Scripts\python -m uvicorn ...`. Se o PowerShell bloquear `Activate.ps1`: `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`.
- **PROJ no Windows:** existem 3 instalações concorrentes (PostgreSQL/PostGIS, pyproj, rasterio) com versões diferentes de `proj.db`. `test_raster_worldwide.py` força `PROJ_LIB=PROJ_DATA=<rasterio>/proj_data` no topo do arquivo **antes** de qualquer op que toque CRS.
- **404 de `maplibre-gl.js.map` é benigno.** O vendor inclui apenas `.js` e `.css`, não o `.map`. A diretiva `sourceMappingURL` no fim do `.js` faz o Chrome DevTools buscar o source map automaticamente (só com DevTools aberto). Ignorar no log.
- **Onde colocar mbtiles grandes:** `data_external/` na raiz, apontado pelo `config.yaml` como `../data_external/<nome>.mbtiles`.
