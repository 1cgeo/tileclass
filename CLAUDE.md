# TileClass

Aplicação web para classificação pixel-a-pixel de tiles de satélite (256×256 @ **2.5 m/pixel** = **640 m × 640 m** no chão, 6 classes).
Backend FastAPI + SQLite; frontend Vanilla JS com Canvas HTML5. Imagem de fundo via TileServer-GL (XYZ).

**Classes (config.yaml):** Massa d'água (#377eb8 azul), Área edificada (#e41a1c vermelho), Floresta (#4daf4a verde), Campo (#ffff33 amarelo), Cultivo (#984ea3 roxo), Terreno exposto (#ff7f00 laranja). Paleta ColorBrewer Set1 — contrastantes entre si e visíveis sobre Sentinel-2.

Spec completa: `docs/requirements.md`. Em caso de dúvida, o requirements manda.

## Stack

- **Backend:** Python 3.11+, FastAPI, Uvicorn, SQLite (sqlite3 nativo — **sem ORM**), PyJWT, bcrypt, Pillow, NumPy, PyYAML, rasterio (export), pyproj (geodésica WGS84)
- **Frontend:** Vanilla JS (sem framework), Canvas HTML5, fetch API. MapLibre GL JS (via CDN) para renderização georreferenciada dos tiles XYZ. Servido como estático pelo FastAPI.
- **Testes:** pytest + httpx (backend), Vitest + jsdom (frontend unit), Puppeteer + uvicorn real (E2E)
- **Config:** `backend/config.yaml` (classes, TileServer URL, JWT secret, DB path). Override por env: `TILECLASS_CONFIG=<path>` (usado nos E2E). Rate limit desligável via `TILECLASS_DISABLE_RATE_LIMIT=1` (apenas E2E — nunca em produção).

## Comandos

```bash
# Setup
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt

# Dev server
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

# Scripts CLI (rodar como módulo para imports relativos funcionarem)
python -m backend.scripts.create_admin
python -m backend.scripts.import_points --point <lat> <lon> <name>          # um ponto
python -m backend.scripts.import_points --csv pontos.csv [--block 3]        # CSV lat,lon,name; block NxN
python -m backend.scripts.import_cq_tiles --geoparquet cq_selection.geoparquet [--seed empty|raw]
python -m backend.scripts.import_qc_tiles --csv qc_tiles.csv --bdf-dir <dir>
python -m backend.scripts.export_tiles <out_dir> [--status reviewed|reviewed+classified] [--raw] [--mosaic] [--manifest <path>]
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
│   │   ├── operator.py         # /api/tiles/* + /api/me/stats-today + helpers de body de máscara
│   │   ├── admin.py            # /api/admin/* (dashboard, tiles, users, mask overlay)
│   │   └── config.py           # /api/config/{classes,tileserver} + /api/{xyz,dsg,mb}/{z}/{x}/{y}.<ext>
│   ├── auth.py                 # JWT, bcrypt, rate limit, blacklist, role middleware
│   ├── models.py               # Schemas Pydantic
│   ├── database.py             # Conexão SQLite (WAL), schema, transaction(), log_action
│   ├── config.py               # Carrega config.yaml (singleton cache)
│   ├── tile_service.py         # Fila, atribuição atômica, submit, problema, pause/resume, histórico
│   ├── admin_service.py        # Fachada — re-exporta de backend/admin/
│   ├── admin/                  # Submódulos do admin (split de admin_service.py)
│   │   ├── dashboard.py        # /admin/dashboard + _cycle_durations (CTE pause/resume aware)
│   │   ├── tiles_query.py      # list_tiles, count_tiles, list_tiles_map, list_problems + _build_tiles_filter
│   │   ├── tiles_mutations.py  # reset/problem/re-review/assign/unassign/pause/delete/block/unblock (atômicos)
│   │   ├── users.py            # list/create + can_review/role/active toggles
│   │   └── thumbnails.py       # tile_thumbnail (mask colorizada) + tile_satellite_thumbnail
│   ├── mask_utils.py           # Uint8Array ↔ PNG "L" + validação (IDs derivados do config)
│   ├── mask_tile_service.py    # Cache mbtiles do overlay admin (rasteriza máscaras → XYZ)
│   ├── mbtiles_service.py      # Reader read-only (singletons primary/dsg/mb, conn por thread)
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
│       ├── admin/               # Submódulos: dashboard.js, modals.js
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

## Invariantes do domínio

- **Geometria do tile (backend/geo.py):** `TILE_PX=256`, `METERS_PER_PX=2.5`, `TILE_METERS=640`. Cada tile é definido **pelo centro geodésico**. `bbox_from_center(lat, lon)` usa `pyproj.Geod` (WGS84) para calcular ±320 m em cada direção cardeal — precisão < 1 mm em qualquer latitude. Schema de `tiles` tem apenas `bbox_*`; zoom/tile_x/tile_y/context_tiles foram removidos.
- **Adjacência sem gap:** `offset_center(lat, lon, dx, dy)` caminha `dx*640m` e `dy*640m` por geodésica, garantindo que tiles vizinhos do `--block NxN` compartilhem arestas exatamente (gap < 1 mm, validado por teste).
- **Fonte de verdade da máscara:** `Uint8Array(65536)` no cliente. Valores válidos: IDs definidos em `config.yaml` (`classes[].id`, hoje `1..6`) + `255` (não preenchido). `mask_utils.validate_partial` deriva os IDs do config — adicionar/remover classe é só edição YAML, sem código. O canvas é apenas visualização.
- **PNG do backend:** banda única (grayscale "L"), 8 bits, 256×256, sem compressão com perda. Pillow faz a conversão `bytes ↔ PNG`.
- **Protocolo wire:** frontend envia **raw bytes** (Uint8Array, 65536 bytes) no body do classify/review; nunca PNG. Backend converte.
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
```

**Formato de saída** (cada tile: `<out_dir>/gt_<tile.name>.tif`):
- GeoTIFF single-band uint8, 256×256, **EPSG:4326**, compressão `deflate`, **NODATA = 255**
- Transform via `rasterio.transform.from_bounds(west, south, east, north, 256, 256)` — pixel ≈ 2.5 m na latitude do centro
- Reprojeção para 3857/UTM fica a cargo do consumer (`gdalwarp` / `rasterio.warp`).

**Remap canônico de classes** (default; `--raw` desativa):

| TileClass id | Nome (config.yaml) | EDGV id | Nome EDGV (treinamento_6c) |
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

**Manifest (`<out_dir>/manifest.csv`):** uma linha por tile exportado com `filename, tile_id, name, status, classified_by, reviewed_by, classified_at, reviewed_at, bbox_*`. É o ponto de entrada pra outros agentes saberem o que receberam (status, autoria, geometria) sem precisar abrir o GeoTIFF.

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
