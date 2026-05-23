# TileClass — guia para agentes

Aplicação de classificação de tiles de satélite (raster/vector/classification/detection). Domínio (projetos, classes, layers, geometria) vive **no banco**, não em `config.yaml` (que só carrega infra/segredos).

Ponteiros:

- [`docs/sistema.md`](docs/sistema.md) — arquitetura, kinds, fluxos, ingestão, export, UX
- [`docs/FUNCIONALIDADES.md`](docs/FUNCIONALIDADES.md) — catálogo exaustivo (endpoints, errors, atalhos)
- [`README.md`](README.md) — setup e primeira execução
- Schema canônico: `backend/database.py` (constante `SCHEMA`). Endpoints: `backend/routers/`. Critérios de aceitação: a suite de testes.

## Stack

Python 3.11+ (FastAPI, Uvicorn, sqlite3 nativo **sem ORM**, PyJWT, bcrypt, Pillow, NumPy, PyYAML, rasterio, pyproj). Vanilla JS + Canvas + MapLibre (vendored). pytest + httpx, Vitest + jsdom, Puppeteer.

## Comandos

```bash
# Setup
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
cp backend/config.example.yaml backend/config.yaml   # config.yaml é gitignored
# troque auth.jwt_secret (o app recusa subir login com o placeholder)

# Dev server (init_db roda no lifespan → schema vazio; crie admin + 1º projeto via UI)
python -m backend.run                                                                                    # wrapper recomendado (Ctrl+C em <1s)
# Equivalente direto — sem as flags abaixo, Ctrl+C demora 5-10s aguardando keep-alive HTTP fechar.
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 1 --timeout-keep-alive 2

# Scripts CLI (rodar como módulo para imports relativos funcionarem)
python -m backend.scripts.create_admin
python -m backend.scripts.import_points --point <lat> <lon> <name> [--project <id|name>]   # um ponto
python -m backend.scripts.import_points --csv pontos.csv [--block 3] [--project <id|name>] # CSV lat,lon,name; block NxN
python -m backend.scripts.export_tiles <out_dir> [--status reviewed|classified|reviewed+classified] [--raw] [--mosaic] [--manifest <path>] [--project <id|name>]   # raster
python -m backend.scripts.export_features <out_dir> [--status ...] [--mosaic] [--manifest <path>] [--project <id|name>]                                  # vector
python -m backend.scripts.export_classifications <out_dir> [--status ...] [--manifest <path>] [--project <id|name>]                                      # classification
python -m backend.scripts.export_detections <out_dir> [--status ...] [--mosaic] [--manifest <path>] [--project <id|name>]                                # detection
python -m backend.scripts.build_mbtiles <raster_in> <out.mbtiles>
python -m backend.scripts.merge_db <other.db>

# Testes
python -m pytest tests/ --ignore=tests/e2e --ignore=tests/frontend -v   # backend
npx vitest run                                                           # frontend unit
node tests/e2e/runner.mjs                                                # E2E (sobe uvicorn + Puppeteer)
npm run test:all                                                         # tudo em sequência
```

**venv no Windows:** ative antes de rodar `uvicorn`/`pytest`/scripts. Sem ativar, o terminal não acha `uvicorn` no PATH. Alternativa: `.\.venv\Scripts\python -m uvicorn ...`. Se o PowerShell bloquear `Activate.ps1`: `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`.

## Estrutura

Mapa completo em [`docs/sistema.md` §3](docs/sistema.md). Pontos de entrada que importam ao navegar:

- `backend/main.py` — FastAPI app + lifespan + static SPA
- `backend/routers/{auth,projects,operator,admin}.py` — APIRouters
- `backend/{project_service,tile_service,export_service,tile_ingest}.py` — serviços de domínio
- `backend/admin/{dashboard,tiles_query,tiles_mutations,users,thumbnails}.py` — submódulos admin
- `backend/database.py` — schema + `transaction()` + `log_action`
- `frontend/js/{editor,editor-vector,editor-classification,editor-detection}.js` — orquestração DOM/MapLibre por kind
- `frontend/js/{mask-core,vector-core,detection-core}.js` — lógica PURA (testável sem DOM)
- `frontend/js/{api,app,admin}.js` — fetch/JWT, router SPA, admin com 7 abas
- `tests/` (pytest), `tests/frontend/` (Vitest), `tests/e2e/runner.mjs` (Puppeteer)

## Regras de idioma

- **UI (labels, mensagens, tooltips):** Português pt-BR com acentuação correta.
- **Comentários, docstrings, nomes de variável/função:** Inglês.
- **Campos de banco e API JSON:** Inglês (`status`, `assigned_to`, `classified_at`).

## Invariantes do domínio

- **Geometria do tile (per-projeto):** `projects.tile_px` (qualquer inteiro 1..4096) × `projects.meters_per_pixel > 0`; `tile_meters = tile_px × meters_per_pixel`. Default seed = 256 × 2.5 = 640 m. Admin escolhe livre. Cada tile é definido **pelo centro geodésico**; `geo.bbox_from_center(lat, lon, tile_meters)` usa `pyproj.Geod` (WGS84). Schema de `tiles` tem apenas `bbox_*`. **Imutável após o primeiro tile** — `update_project` rejeita com 409 `tile_geometry_locked`.
- **Adjacência sem gap:** `offset_center(lat, lon, dx, dy, tile_meters)` caminha `dx*tile_meters`/`dy*tile_meters` por geodésica, garantindo que tiles vizinhos do `--block NxN` compartilhem arestas (gap < 1 mm, validado por teste).
- **Fonte de verdade da máscara:** `Uint8Array(tile_px²)` no cliente (re-alocado em `setTileGeometry()`). Valores válidos: IDs em `project_classes[project_id]` + `255` (não preenchido). `mask_utils.validate_partial(raw, allowed_ids, tile_px=...)` recebe os IDs e o tamanho explicitamente; `validate_submission(...)` usa o `mask_complete_required` do projeto.
- **PNG do backend:** banda única (grayscale "L"), 8 bits, `tile_px × tile_px`. `encode_mask`/`decode_mask` recebem `tile_px` explicitamente.
- **Protocolo wire:** frontend envia **raw bytes** (Uint8Array, `tile_px²` bytes) no body do classify/review; nunca PNG. Backend resolve `tile_px` via `project_for_tile()` antes de validar tamanho.
- **Submissão raster:** rejeitar se houver `255` no array (quando `mask_complete_required`). Resposta de erro traz a contagem.
- **Máquina de estados de tile:** `pending → in_progress → classified → in_review → reviewed`; qualquer estado `→ problem`; `problem → pending` e `reviewed → in_review` são admin. Admin pode bloquear via `pending|classified|reviewed → blocked` (guarda original em `blocked_from`) e desbloquear. `in_progress`/`in_review`/`problem` **não** podem ser bloqueados.
- **Export GeoTIFF:** `rasterio.transform.from_bounds(west, south, east, north, tile_px, tile_px)` com `crs=EPSG:4326`. Pixel = `meters_per_pixel` na latitude do centro (validado para 20 pontos mundiais em `test_raster_worldwide.py`).

## Regras críticas de backend

- **Sem ORM.** `sqlite3` puro com queries SQL parametrizadas (`?`). `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000` ligados em `connect()`.
- **Transações explícitas:** use `from backend.database import transaction` como context manager (`BEGIN IMMEDIATE` + COMMIT/ROLLBACK). Nunca misture `conn.execute("BEGIN")` com transações aninhadas.
- **`/api/tiles/next` é transacional:** `BEGIN IMMEDIATE`, `SELECT ... LIMIT 1`, `UPDATE status, assigned_to`, `COMMIT`. Dois operadores nunca recebem o mesmo tile. Garantido por teste de concorrência (10 operadores paralelos).
- **Fila de revisão:** tiles com `status='classified'` aguardam revisor; ao atribuir, vão para `in_review`. Revisor nunca revisa tile que ele classificou (`classified_by != current_user`).
- **Resume:** primeira coisa em `/next` é buscar `assigned_to=user AND status IN ('in_progress','in_review')` — se achar, devolve o mesmo tile (F5 não perde trabalho).
- **Autorização em submit:** `assigned_to == current_user` é pré-condição para `classify`/`review`.
- **`HTTPException` é importado no topo do módulo**, não dentro de funções. Serviços podem levantar diretamente — FastAPI propaga.
- **Bulk vs single:** as operações admin (`reset_many`, `re_review_many`) fazem o trabalho real; `reset_tile`/`re_review_tile` são wrappers de 1 linha. Não duplicar SQL.
- **Rate limit:** `/api/auth/login` — 5/min por IP. Use `auth.reset_rate_limits()` nos testes (não acesse `_login_attempts` direto).
- **bcrypt:** cost ≥ 12. **JWT:** HS256, access 8h, refresh 24h (valores em `config.yaml`).
- **Índices:** `status`, `assigned_to`, `classified_by`, `reviewed_by`, `classified_at`, `reviewed_at` — manter quando adicionar filtros no admin.
- **Métricas de duração (dashboard):** derivadas do `action_log` via pares `assign_classify→classify` e `assign_review→review` (não há schema novo). `dashboard()` retorna `avg_classify_seconds`/`avg_review_seconds` globais + por operador.

## Regras críticas de frontend

- **Três camadas sobrepostas no editor:** (1) MapLibre GL renderiza o satélite XYZ como `div` georreferenciado ao fundo (`interactive: false`, bbox via `fitBounds`), (2) canvas da máscara com `globalAlpha` controlável, (3) canvas de cursor/interação no topo. Eventos de mouse vão na camada 3 (`pointer-events: auto`); as outras têm `pointer-events: none`.
- **MapLibre resolve composição de tiles:** quando a bbox não alinha com um XYZ único, MapLibre carrega múltiplos tiles e recorta pela bbox. Não compor manualmente.
- **Mapeamento de coordenadas (ponto mais frágil):** `getBoundingClientRect()` → fração → `Math.floor(frac * tile_px)` com clamp em `[0..tile_px-1]`. O canto inferior-direito deve dar `(tile_px-1, tile_px-1)`.
- **Uint8Array como fonte de verdade:** canvas é só visualização. Mudar o array, depois chamar `writeMaskPixels(x0,y0,x1,y1)` + `blitMask()`.
- **Outlines no hot-path:** `drawOutlines` itera por todos os pixels — **não chamar durante o gesto de pintura**. O `blitMask` só desenha outlines quando `!drawing`; o `onMouseUp` re-chama `blitMask`.
- **Undo/redo:** cada ciclo mousedown→mouseup é uma entrada. Armazenar **só pixels alterados** (`Uint32Array positions` + `Uint8Array prevValues`), nunca o array inteiro. 50 níveis.
- **Interpolação no drag:** Bresenham entre `mousemove` para não deixar buracos em movimentos rápidos.
- **`Space` segurado esconde máscara** (keydown/keyup, não toggle). Atalhos desabilitados quando modal/input ativo — usar `isTextFocused() || isModalOpen()`.
- **Helpers compartilhados:** `hexToRgb`, `blobToImage`, `escapeHtml` vivem em `utils.js`. Não redefinir.
- **Paleta de classes:** contrastante entre si e visível sobre imagens de satélite. Evitar verde e tons escuros.
- **Sem `innerHTML` com dados de usuário.** Usar `textContent` ou `escapeHtml` de `utils.js`.
- **Lógica PURA em `*-core.js`** (`mask-core`, `vector-core`, `detection-core`) com testes Vitest. `editor*.js` fica com orquestração DOM/canvas/MapLibre — não testável; portanto, qualquer regra nova vai no `*-core.js`.
- **404 de `maplibre-gl.js.map` é benigno.** O vendor inclui apenas `.js` e `.css`. A diretiva `sourceMappingURL` faz o Chrome DevTools buscar o source map automaticamente. Ignorar no log.

## Dispatch por kind — regras acionáveis

- **`tile_service.project_for_tile()`** resolve a kind antes de cada submit/pause; `_submit_raster`/`_submit_vector`/`_submit_classification`/`_submit_detection` (e `_pause_*`) ficam isolados.
- **`routers/operator._read_body_for_kind`** lê o body certo (raster: `tile_px²` bytes exatos; vector/detection: até 1.5 MB JSON; classification: até 1 KB JSON).
- **`report_problem`** limpa `data_png`, `data_geojson` **e** `data_class_id` para que mudanças futuras não vazem corpo de tipo errado.
- **`mask_tile_service.get_tile()`** despacha por kind. Cache mbtiles é per-projeto (`<base>_p<id>.mbtiles`); kinds diferentes em projetos diferentes não compartilham linhas.
- **Mutual exclusion no payload:** `POST /api/admin/projects` rejeita `attributes` em projeto raster/classification/detection (400 `attributes_not_supported`) e `classes` em projeto vector (400 `classes_on_vector`). `kind` é imutável; PATCH ignora silenciosamente.
- **`classification` não suporta pause** (409 `pause_not_supported`); **`detection` espelha `box_required`** no `validateForSubmit` do front.

## Regra inegociável de classes/atributos

- Renomear/recolorir IDs de `project_classes` é livre; **remover** uma classe é rejeitado quando o projeto tem qualquer tile (bytes da máscara são imutáveis e podem referenciar o ID removido). Adicionar IDs é livre.
- Atributos (`project_attributes`, vector): renomear/relabel livre; remover só se nenhuma feature do projeto usa a key (`attribute_in_use` 409).
- `tile_px`/`meters_per_pixel` imutáveis após o primeiro tile (409 `tile_geometry_locked`).
- `kind` imutável após criação (PATCH ignora o campo).

## GT Extractor — regras acionáveis

- 4 exportadores (um por kind), todos com a mesma assinatura `run(out_dir, *, status, project_id, **opts)` — o `main()`/CLI é wrapper fino sobre ela.
- Status idênticos em todos: `reviewed` (default), `classified`, `reviewed+classified`.
- **Estender adicionando flags ao mesmo script** — nunca criar novo extractor. Manter um único ponto de saída evita drift entre consumers.
- `EDGV_REMAP_LUT` no topo de `export_tiles.py` é a fonte única do remap (raster apenas; `--raw` desativa).
- `backend/export_service.py` é o ponto único que mapeia o status da API (`reviewed_classified`) para a chave dos scripts (`reviewed+classified`) e despacha pelo kind do projeto.

## UX — inegociáveis

- Login → pintar em < 5s. Evitar modais/confirmações desnecessárias.
- **Pré-carregar próximo tile:** `/api/tiles/next-preview` (peek sem atribuir) enquanto operador pinta o atual. Cache em `preloadedNext` invalidado se o tile real em `/next` for diferente.
- Após submeter: tela limpa imediatamente (`idle-screen` "Tile enviado ✓"), sem auto-avanço — operador clica para pedir o próximo (respiro entre cartas).
- Pixels faltantes ao submeter: pisca vermelho 2s sobre o canvas. Sempre visível na sidebar: `⚠ Faltam N pixels`.
- Botão de submit auto-explicativo: incompleto → label `"Faltam N px"` + estilo `.incomplete`; tile `in_review` → label `"Aprovar revisão"`.
- Modo sempre visível: pill `#mode-pill` azul `CLASSIFICAR` ou laranja `REVISAR`.
- Backup do `Uint8Array` em `localStorage` durante edição. No F5, `tryRestoreBackup()` compara com a máscara do servidor e, se diferirem, sobrescreve silenciosamente — toast `"Trabalho local restaurado."`. Backup apagado no submit.
- **JWT refresh proativo:** `api.js` decodifica o `exp` e agenda refresh 60s antes de expirar; também re-tenta no 401 (reativo). O usuário não deve ver logout por expiração durante uso contínuo.

## Design system (CSS)

- **Tokens em `:root`** (`style.css`): `--bg-0…bg-4`, `--accent`, `--ok`, `--warn`, `--err`, `--info`, `--space-1…6`, `--radius*`, `--shadow*`, `--header-h`, `--sidebar-w`. Sempre via `var()`, nunca hex hardcoded em componentes.
- **Responsive breakpoints:** 1180px (encolhe sidebar), 720px (single-column, sidebar vira scroll-snap horizontal), 480px (stats grid 1-col). `prefers-reduced-motion` respeitado.
- **Status chips** (`.chip.pending/in_progress/classified/in_review/reviewed/problem`) disponíveis para tabelas admin.
- **Spinner de loading** (`.loading`) e `.loading-text` padrão.

## Segurança

- Senhas: bcrypt cost ≥ 12.
- Validar no backend: `tile_px²` bytes exatos no body raster, valores em paleta do projeto + `255`, submetedor == `assigned_to`.
- Endpoints admin atrás de `Depends(auth.require_admin)` que verifica `role == 'admin'`.
- Nunca `innerHTML` com dado de usuário — `textContent` ou `escapeHtml` de `utils.js`.

## Testes

Três camadas. Todas devem passar antes de commitar:

### Backend (`tests/*.py`, pytest + TestClient)
- Fixture `app_env` em `conftest.py` monkey-patcha o DB para arquivo temporário. Precisa patchar `get_config` em **cada módulo** que fez `from .config import get_config` (referência é copiada no import).
- Fixtures: `admin_user`, `operators` (3), `operators_10`, `tiles` (10 pending), `tiles_many` (100).
- Rate limit entre logins: `auth.reset_rate_limits()` antes de cada login quando >5 no mesmo teste.
- Concorrência crítica: `test_concurrent_10_operators_no_duplicates` (10 threads pending), `test_10_reviewers_race_on_classified_queue` (9 reviewers simultâneos na fila de revisão).
- Invariantes de domínio com teste próprio: `test_auth_invariants` (bcrypt cost≥12, JWT TTL 8h/24h, forgery, typ access↔refresh, escalação via claim), `test_authz_crossuser` (bypass de `/next`, POST cross-user, gate admin), `test_state_machine` (todas transições + ilegais), `test_rate_limit_real` (sem reset), `test_dashboard_real` (atua antes de conferir — não-tautológico, inclui pareamento assign→classify/review), `test_mask_roundtrip_strong` (padrões não-uniformes).
- **Invariantes geométricas (`test_geo.py`):** tile sempre 640m×640m em qualquer latitude (equador → -70°), pixel = 2.5m em ambos eixos, bbox simétrica, adjacência sem gap, bloco 3x3 cobre exatamente 1920m, `from_bounds` do rasterio bate com 2.5m.
- **Rasters mundiais (`test_raster_worldwide.py`):** 20 pontos (equador, trópicos, NY, Tóquio, Moscou, Tromsø, McMurdo/Antártica) geram GeoTIFF real pela mesma pipeline do `export_tiles._write_geotiff`, reabrem com `rasterio` e validam CRS=EPSG:4326, bounds, transform, pixel em metros, e `dataset.xy()` retornando ao centro. Um teste reverso prova que **span em graus encolhe com a latitude**.
- **PROJ no Windows:** existem 3 instalações concorrentes (PostgreSQL/PostGIS, pyproj, rasterio) com versões diferentes de `proj.db`. `test_raster_worldwide.py` força `PROJ_LIB=PROJ_DATA=<rasterio>/proj_data` no topo do arquivo **antes** de qualquer op que toque CRS.

### Frontend unit (`tests/frontend/*.test.js`, Vitest + jsdom)
- `mask-core.test.js` testa `frontend/js/mask-core.js` (paint, Bresenham sem gaps, floodFill com fronteira, undo/redo byte-exato com Uint32Array, `screenToLogical` com rect deslocado/esticado, `validateSubmission` espelhando backend).
- `api-refresh.test.js` mocka `fetch`, usa fake timers: refresh proativo 60s antes de expirar, retry reativo no 401, Bearer header, `apiPostBytes` octet-stream, erro traz `status`+`body`.
- `vector-core.test.js` cobre vertex editing (move/insert/remove, imutabilidade, bounds, refusa abaixo de 2 vértices).
- `utils.test.js`, `localstorage-backup.test.js` (round-trip Uint8Array↔base64↔JSON, rejeita tile diferente).
- **Regra:** toda lógica pura nova vai em `*-core.js` e tem teste — NÃO no `editor*.js` (que fica com orquestração DOM/canvas/MapLibre, não testável).

### E2E (`tests/e2e/runner.mjs`, Puppeteer + uvicorn real)
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
