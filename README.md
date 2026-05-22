# TileClass

Aplicação web para classificação de tiles de satélite — máscara pixel-a-pixel (raster), anotação vetorial (linhas) ou rótulo único por tile (classification). **Tudo é por projeto** e configurado pela UI admin. FastAPI + SQLite (sem ORM) + Vanilla JS.

## Setup

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
npm install   # opcional, só pros testes frontend

# Crie seu config.yaml a partir do template e gere um segredo forte:
cp backend/config.example.yaml backend/config.yaml
python -c "import secrets; print(secrets.token_urlsafe(48))"   # cole em auth.jwt_secret
```

`backend/config.yaml` (gitignored) carrega **apenas configuração de nível de aplicação/infra** — não há dado de domínio:
- `auth.jwt_secret` — **obrigatório trocar** o placeholder; o app recusa autenticar com o valor padrão (ou defina via env `TILECLASS_JWT_SECRET`). `access_token_expiry_hours` / `refresh_token_expiry_hours`.
- `mask_overlay` — cache do overlay admin (`cache_path`, `min_zoom`, `max_zoom`); o diretório é criado automaticamente.
- `database.path` — caminho do SQLite (relativo a `backend/`).

**Projetos, classes, layers (imagens/máscaras), geometria de tile e membros NÃO ficam no config.yaml** — vivem no banco e são criados/editados pela UI admin. Cada layer de um projeto aceita: path `.mbtiles` local, URL de tile-server (`http(s)://…/{z}/{x}/{y}`), ou `bingmaps://{z}/{x}/{y}`.

**`.mbtiles` grandes:** convenção do repo é `data_external/` na raiz (gitignored); aponte os layers do projeto com `../data_external/<arquivo>.mbtiles` (resolvido relativo a `backend/`).

**Tile-servers remotos e CSP:** o `Content-Security-Policy` em `backend/main.py` libera por padrão `server.arcgisonline.com` e `*.virtualearth.net` (Bing). Se você configurar um layer remoto em outro host, adicione-o a `img-src`/`connect-src` na CSP.

## Inicialização (do zero)

Uma instalação nova começa **vazia** — sem projeto, sem classes. O fluxo:

```bash
# 1) Cria o admin global inicial (interativo). Roda init_db() — schema vazio.
python -m backend.scripts.create_admin

# 2) Suba o app (ver "Executar") e faça login como admin.

# 3) Na aba "Projetos" da UI admin, crie o 1º projeto e configure tudo:
#    nome, kind (raster/vector/classification), classes (cores), os layers
#    (path .mbtiles / URL / bingmaps://), geometria do tile (tile_px ×
#    meters_per_pixel) e os membros. (Equivalente: POST /api/admin/projects.)

# 4) Crie os usuários operadores/revisores na aba "Usuários" e adicione-os
#    como membros do projeto.

# 5) Importe tiles para o projeto (--project é id ou nome; obrigatório quando
#    há mais de um projeto). Geometria vem do projeto (default 256 @ 2.5m = 640m).
python -m backend.scripts.import_points --point -23.550 -46.633 centro_sp --project meu-projeto
python -m backend.scripts.import_points --csv pontos.csv --project meu-projeto           # CSV: lat,lon,name
python -m backend.scripts.import_points --csv pontos.csv --block 3 --project meu-projeto  # NxN ao redor
```

Outros importadores (uso especializado, ver docstring do arquivo):
- `import_cq_tiles.py` — tiles de CQ vindos de `cq_selection.geoparquet`
- `import_qc_tiles.py` — tiles do BDF com seed mask do argmax

> Não há projeto "default" pré-criado. Os scripts de import recusam rodar sem um projeto — crie-o primeiro pela UI.

## Executar

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Abrir `http://localhost:8000`.

## Testes

```bash
# Backend (pytest + TestClient)
python -m pytest tests/ --ignore=tests/e2e --ignore=tests/frontend -v

# Frontend unit (Vitest + jsdom)
npx vitest run

# E2E (Puppeteer + uvicorn real)
node tests/e2e/runner.mjs

# Tudo em sequência
npm run test:all
```

Cobertura: auth (rate limit, roles, refresh, JTI revoke), fila de tiles (atribuição atômica, resume, prioridade revisão, anti auto-revisão), submissão (tamanho, valores, autorização, versão), estado (todas transições + `blocked`/`paused`), concorrência (10 ops paralelos), admin (dashboard com durações pareadas, bulk reset/assign/block, thumbnail), geometria (640m em qualquer latitude, raster mundial em 20 pontos), mask-core puro (paint/Bresenham/flood/undo).

## Exportar resultados

Quatro exportadores (um por kind), todos com `--status reviewed | classified | reviewed+classified`
e `--project <id|name>`:

```bash
python -m backend.scripts.export_tiles ./exports            # raster → GeoTIFF (default: revisados, remap EDGV)
python -m backend.scripts.export_features ./exports         # vector → GeoJSON
python -m backend.scripts.export_classifications ./exports  # classification → CSV
python -m backend.scripts.export_detections ./exports       # detection → GeoJSON (bboxes)

# Filtros de status:
python -m backend.scripts.export_tiles ./exports --status classified            # só classificados (não revisados)
python -m backend.scripts.export_tiles ./exports --status reviewed+classified   # ambos (preliminar)

# Raster: IDs nativos 1..6 (sem remap EDGV) / mosaico
python -m backend.scripts.export_tiles ./exports --raw
python -m backend.scripts.export_tiles ./exports --mosaic
```

GeoTIFF single-band uint8, EPSG:4326, NODATA=255, deflate. Todo export gera um manifest (CSV) com status/autoria/bbox por tile.

**Pela UI:** admins exportam pelo painel "Exportar dados" no detalhe do projeto (aba Projetos) — baixa um ZIP no formato do kind, com o mesmo filtro de status. Endpoint: `GET /api/admin/projects/{id}/export?status=...`.

## Scripts utilitários

- `build_mbtiles.py` — converte raster grande → mbtiles XYZ servíveis
- `build_xyz_pyramid.py` — pirâmide XYZ em disco (alternativa)
- `merge_db.py` — funde dois `tileclass.db` (junção de equipes/sessões)

## Estrutura

```
backend/
  main.py              FastAPI app + lifespan (init_db) + middleware + static
  routers/             auth, projects (+admin CRUD/classes/members), operator, admin
  auth.py              JWT (HS256) + bcrypt + rate limit + blacklist
  models.py            Pydantic schemas
  database.py          SQLite WAL, schema, migração, transaction(), log_action
  config.py            Loader de config.yaml (singleton; só infra/segredos)
  project_service.py   CRUD de projetos/classes/membros, cache, layer_path
  geo.py               bbox_from_center (pyproj.Geod); geometria é por projeto
  tile_service.py      Fila /next, submit (raster/vector/classification), pause, resume
  admin_service.py     Fachada → admin/ (dashboard, tiles_query/mutations, users, thumbnails)
  mask_utils.py        encode/decode PNG ↔ Uint8Array, validação
  mask_tile_service.py Cache mbtiles do overlay admin (per-projeto)
  mbtiles_service.py   Pool LRU de readers por (project_id, layer)
  config.example.yaml  Template (copie para config.yaml, gitignored)
  scripts/             create_admin, import_*, export_*, build_*, merge_db, verify_db, backup_db

frontend/
  index.html           SPA (login / editor / admin)
  css/style.css        Tokens em :root + breakpoints
  js/
    app.js             Router por role
    api.js             fetch wrapper + JWT refresh (proativo + reativo)
    editor.js          Canvas, ferramentas, undo/redo, submit
    mask-core.js       Lógica pura (paint, flood, screenToLogical, validate)
    admin.js           Dashboard, tiles (lista+grade+mapa), users, viewer
    minimap.js         MapLibre 3×3 com highlight do tile atual
    maplib.js          Helpers MapLibre (createLockedMap, setMapBbox)
    utils.js           hexToRgb, blobToImage, escapeHtml
    toast.js           Notificações
  vendor/maplibre/     CDN local
docs/
  requirements.md          Spec completa
tests/
  conftest.py, test_*.py   Backend (pytest + TestClient)
  frontend/*.test.js       Frontend unit (Vitest + jsdom)
  e2e/runner.mjs           E2E (Puppeteer + uvicorn real)
```

## Atalhos do editor

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

Atalhos não disparam quando há input/modal em foco.
