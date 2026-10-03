# TileClass — guia para agentes

Anotação de tiles de satélite em 2 kinds: `raster` (máscara pixel-a-pixel) e `classification` (uma classe por tile). O domínio (projetos, classes, layers, geometria, membros) fica **no banco**. O `config.yaml` só tem infra e segredos. Setup: [`README.md`](README.md). Schema: `backend/database.py` (`SCHEMA`). Endpoints: `backend/routers/`. Os critérios de aceitação são a suite de testes.

## Comandos

```bash
.venv\Scripts\activate                                                   # Windows: sem isso uvicorn/pytest não estão no PATH
python -m backend.run                                                    # dev server com reload
python -m pytest tests/ --ignore=tests/e2e --ignore=tests/frontend -v    # backend
npx vitest run                                                           # frontend unit
node tests/e2e/runner.mjs                                                # E2E (uvicorn + Puppeteer)
npm run test:all
```

Scripts CLI: `python -m backend.scripts.<nome>` (rodar sempre como módulo).

## Idioma

UI em pt-BR, com acentuação. Código, comentários, banco e JSON da API em inglês.

## Invariantes do domínio

- **Geometria:** `tile_meters = projects.tile_px (1..4096) × meters_per_pixel`. O tile é definido pelo centro geodésico (`geo.bbox_from_center`, pyproj WGS84) e a tabela `tiles` guarda só `bbox_*`. `offset_center` garante adjacência sem gap no `--block NxN`.
- **Imutáveis:** `kind` (o PATCH ignora o campo); `tile_px`/`meters_per_pixel` após o 1º tile (409 `tile_geometry_locked`). Remover uma classe é bloqueado se o projeto tem tiles (`class_in_use`). Adicionar ou renomear é livre.
- **Kinds removidos:** `vector` e `detection` foram retirados. O schema ainda os aceita no CHECK (e mantém `topology_required`, `box_required`, `project_attributes`, `data_geojson`, `feature_count`) só para bancos legados e `merge_db`. A criação é barrada por `project_service.PROJECT_KINDS`. Não reintroduza código lendo essas colunas.
- **Raster:** a fonte de verdade é um `Uint8Array(tile_px²)` com valores = IDs da paleta + `255` (vazio). O wire envia **raw bytes**, nunca PNG. O backend resolve `tile_px` via `project_for_tile()` e grava PNG "L" 8-bit. Com `mask_complete_required`, rejeita `255` e informa a contagem.
- **Estados:** `pending → in_progress → classified → in_review → reviewed`. Qualquer estado pode ir para `problem`. Só o admin faz `problem → pending` e `reviewed → in_review`. Block/unblock (admin) só a partir de `pending|classified|reviewed`, guardando `blocked_from`.
- **Revisão:** só membro do projeto com role `reviewer` revisa (ser admin global não basta; `users.can_review` está morta). Ninguém revisa tile que classificou.
- **Dispatch por kind:** `tile_service.project_for_tile()` → `_submit_raster`/`_submit_classification`. `operator._read_body_for_kind` define o limite do body (raster: `tile_px²` bytes exatos; classification: JSON `{"class_id": int}` ≤1 KB). Classification não tem pause (409 `pause_not_supported`). `report_problem` limpa `data_png` e `data_class_id`.

## Backend

- `sqlite3` puro, sem ORM, SQL parametrizado. Use transações com `from backend.database import transaction` (`BEGIN IMMEDIATE`) e nunca `conn.execute("BEGIN")` manual.
- `/api/tiles/next` é atômico (uma transação: retomar o tile do próprio usuário → fila de revisão → pending). Dois usuários nunca recebem o mesmo tile. Submit exige `assigned_to == current_user`.
- **`version` (lock otimista):** sobe em toda escrita de máscara e em toda ação de admin. Pausa automática (heartbeat) e retomada pelo próprio dono **não** sobem, senão o editor ainda aberto leva 409 e o backup local é descartado no F5. Retomar sempre renova `last_heartbeat_at`.
- Endpoints de leitura de tile (`/tiles/{id}`, `/image`, `/history`, …) exigem ser membro do projeto do tile (`_require_tile_access`).
- `HTTPException` é importado no topo do módulo e os serviços podem levantá-lo.
- Operações bulk (`reset_many`, `re_review_many`) fazem o trabalho; as versões single são wrappers de 1 linha.
- Índices em `status`, `assigned_to`, `*_by`, `*_at`: mantenha-os ao adicionar filtros admin.
- Exportadores: um por kind (`export_tiles`, `export_classifications`), ambos com `run(out_dir, *, status, project_id, **opts)`. Para estender, **adicione flags** ao script existente em vez de criar outro. `export_service.py` mapeia o status `reviewed_classified` da API para o `reviewed+classified` da CLI. `EDGV_REMAP_LUT` (topo de `export_tiles.py`) é a fonte única do remap; `resolve_remap` aplica a regra `auto` (EDGV só quando os IDs do projeto são exatamente {1..6}, senão raw), sobrescrevível por `--edgv`/`--raw` e pelo param `remap` da API. Nomes de arquivo passam por `safe_name` + desambiguação `_t<id>`.
- **PROJ no Windows:** quem importa rasterio chama `proj_env.configure_proj_data()` **antes** do import (há instalações concorrentes de `proj.db`).

## Frontend

- Lógica pura em `*-core.js` (`mask-core`, `classification-core`), sempre com teste Vitest. `editor.js`/`editor-classification.js` só orquestram DOM/canvas/MapLibre.
- O editor raster tem 3 camadas: MapLibre (fundo, não interativo) → canvas da máscara → canvas de interação (o único com `pointer-events`). O canvas é só visualização: altere o array e depois chame `writeMaskPixels` + `blitMask`.
- Coordenadas: `Math.floor(frac * tile_px)` com clamp em `[0, tile_px-1]`.
- Nada de `drawOutlines` durante o gesto (é O(n) pixels). Undo guarda só os pixels alterados (`Uint32Array` + `Uint8Array`), com 50 níveis. Bresenham entre `mousemove`.
- Atalhos desligados com `isTextFocused() || isModalOpen()`.
- Nunca use `innerHTML` com dado de usuário; use `textContent`/`escapeHtml` (`utils.js`, junto de `hexToRgb`, `blobToImage`, que não devem ser redefinidos).
- **CSS/tema:** `tokens.css` (cores, temas claro/escuro via `<html data-theme>`) → `base.css` (primitivos: botões, chips de status, cards, tabelas, modais…) → `editor.css` / `admin.css`. Nunca hex em CSS/JS (exceto cores de classe, que são dado); use `var()`. Cores pintadas fora do CSS (canvas, MapLibre, SVG) leem variáveis via `getComputedStyle` e repintam no evento `tc-themechange`. Ícones: sprite Lucide (`icon()`/`iconHtml()` em `utils.js`). Paleta de classes contrastante, evitando verde e tons escuros.
- **Revisão visual:** `node tests/e2e/screenshots.mjs <pasta> --demo` gera as telas nos dois temas (seed `tests/e2e/seed_demo.py`).
- UX inegociável: login → pintar em < 5s; pré-carga via `/next-preview`; sem auto-avanço após submit; backup da máscara em `localStorage`; refresh proativo do JWT 60s antes do `exp`.
- O 404 de `maplibre-gl.js.map` é benigno.

## Testes

- **Nunca mocke o DB**: use a fixture `app_env` (ela patcha `get_config` em cada módulo que o importou).
- Valide o efeito colateral (SELECT, `decode_mask`), não só o status code.
- Concorrência sempre com `threading.Barrier`.
- Use `auth.reset_rate_limits()` quando houver mais de 5 logins num teste.
- E2E: um BrowserContext incógnito por cenário; requests externas bloqueadas; `?test=1` expõe `window.__tcTest__`, e nenhuma lógica de produção pode depender dele.

## Git

Nunca commitar sem o usuário pedir.
