# TileClass

Aplicação web para classificação pixel-a-pixel de tiles de satélite (256×256, 6 classes — config.yaml).
FastAPI + SQLite (sem ORM) + Vanilla JS.

## Setup

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
npm install   # opcional, só pros testes frontend
```

`backend/config.yaml` controla:
- `tileserver` / `tileserver_secondary` / `tileserver_tertiary` — fontes de imagem
- `worldcover` / `mapbiomas` — overlays mbtiles opcionais
- `mask_overlay` — cache do overlay admin
- `auth.jwt_secret` — troque em produção (ou via env `TILECLASS_JWT_SECRET`)
- `classes` — IDs/nomes/cores

**Onde colocar os `.mbtiles` grandes:** convenção do repo é `data_external/` na raiz (gitignored). Caminhos no `config.yaml` são `../data_external/<arquivo>.mbtiles` — resolvidos relativos a `backend/`. Mantém o pacote `backend/` enxuto sem GBs de raster.

## Inicialização

```bash
# 1) Cria admin inicial (interativo)
python -m backend.scripts.create_admin

# 2) Importa tiles a partir de pontos centrais (lat,lon).
#    Tile = 256×256 @ 2.5m/pixel = 640m × 640m no chão (geodésico).
python -m backend.scripts.import_points --point -23.550 -46.633 centro_sp
python -m backend.scripts.import_points --csv pontos.csv          # CSV: lat,lon,name
python -m backend.scripts.import_points --csv pontos.csv --block 3 # NxN ao redor
```

Outros importadores (uso especializado, ver docstring do arquivo):
- `import_cq_tiles.py` — tiles de CQ vindos de `cq_selection.geoparquet`
- `import_qc_tiles.py` — tiles do BDF com seed mask do argmax

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

## Exportar resultados (GT extractor)

```bash
# Padrão: tiles revisados, IDs remapeados para EDGV (compatível com treinamento_6c)
python -m backend.scripts.export_tiles ./exports

# Inclui classificados não revisados — dataset preliminar
python -m backend.scripts.export_tiles ./exports --status reviewed+classified

# Mantém IDs nativos do TileClass (1..6) sem remapear
python -m backend.scripts.export_tiles ./exports --raw

# Também grava gt_mosaic.tif unindo tudo
python -m backend.scripts.export_tiles ./exports --mosaic
```

GeoTIFF single-band uint8 256×256, EPSG:4326, NODATA=255, deflate. Cada export gera também `manifest.csv` com status/autoria/bbox por tile.

## Scripts utilitários

- `build_mbtiles.py` — converte raster grande → mbtiles XYZ servíveis
- `build_xyz_pyramid.py` — pirâmide XYZ em disco (alternativa)
- `merge_db.py` — funde dois `tileclass.db` (junção de equipes/sessões)

## Estrutura

```
backend/
  main.py              FastAPI app + rotas + static
  auth.py              JWT (HS256) + bcrypt + rate limit + blacklist
  models.py            Pydantic schemas
  database.py          SQLite WAL, schema, transaction(), log_action
  config.py            Loader de config.yaml (singleton)
  geo.py               TILE_PX/METERS_PER_PX/TILE_METERS, bbox_from_center (pyproj.Geod)
  tile_grid.py         Helpers Web Mercator (build_mbtiles e import_cq)
  tile_service.py      Fila /next, submit, problem, pause, resume, history
  admin_service.py     Dashboard, listagem, bulk actions, users, thumbnails
  mask_utils.py        encode/decode PNG ↔ Uint8Array, validação
  mask_tile_service.py Cache mbtiles do overlay admin (rasteriza máscaras → XYZ)
  mbtiles_service.py   Reader read-only (singletons primary/wc/mb)
  config.yaml
  scripts/             create_admin, import_*, export_tiles, build_*, merge_db

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
  implementation_plan.md   Plano em fases
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
| T / Y | Overlay WorldCover / MapBiomas (segurar) |
| Espaço (segurar) | Esconder máscara |
| Ctrl+Z | Desfazer |
| Ctrl+Y / Ctrl+Shift+Z | Refazer |

Atalhos não disparam quando há input/modal em foco.
