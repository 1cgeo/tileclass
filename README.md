# TileClass

Aplicação web para anotação de tiles de satélite — máscara pixel-a-pixel (raster), linhas vetoriais (vector), rótulo único por tile (classification) ou bounding boxes (detection). Tudo é por projeto e configurado pela UI admin. FastAPI + SQLite (sem ORM) + Vanilla JS + MapLibre.

> 📖 **Arquitetura, kinds e fluxos:** [`docs/sistema.md`](docs/sistema.md). **Catálogo de funcionalidades:** [`docs/FUNCIONALIDADES.md`](docs/FUNCIONALIDADES.md). **Guia para agentes/Claude:** [`CLAUDE.md`](CLAUDE.md).

## Stack

- **Backend:** Python 3.11+, FastAPI, Uvicorn, SQLite (WAL), PyJWT, bcrypt, Pillow, NumPy, rasterio, pyproj
- **Frontend:** Vanilla JS, Canvas HTML5, MapLibre GL JS (vendored localmente)
- **Testes:** pytest + httpx (backend), Vitest + jsdom (frontend), Puppeteer + uvicorn real (E2E)

## Pré-requisitos

- Python 3.11+
- Node.js 20+ (apenas para a suite de testes JS — opcional em produção)
- Git

## Setup

```bash
git clone <url-do-repo> tileclass
cd tileclass

python -m venv .venv

# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1
# Windows (cmd)
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
npm install   # opcional, só pros testes frontend
```

Se o PowerShell bloquear `Activate.ps1`, rode uma vez:
`Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`.

## Configuração

```bash
# Windows
copy backend\config.example.yaml backend\config.yaml
# Linux/macOS
cp backend/config.example.yaml backend/config.yaml

# Gere um segredo JWT forte e cole em auth.jwt_secret no config.yaml
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

`backend/config.yaml` (gitignored) carrega **apenas configuração de infra/segredos** — não há dado de domínio:

- `auth.jwt_secret` — **obrigatório trocar** o placeholder; o app recusa autenticar com o valor padrão (alternativa: env `TILECLASS_JWT_SECRET`). Também: `access_token_expiry_hours`, `refresh_token_expiry_hours`.
- `database.path` — caminho do SQLite (relativo a `backend/`).
- `mask_overlay` — cache do overlay admin (`cache_path`, `min_zoom`, `max_zoom`). O diretório é criado automaticamente.

Projetos, classes, layers (imagens/máscaras), geometria de tile e membros **não** ficam no `config.yaml` — vivem no banco e são criados/editados pela UI admin.

**`.mbtiles` grandes:** convenção é `data_external/` na raiz (gitignored); aponte os layers do projeto com `../data_external/<arquivo>.mbtiles` (relativo a `backend/`).

**Tile-servers remotos e CSP:** o `Content-Security-Policy` em `backend/main.py` libera por padrão `server.arcgisonline.com` e `*.virtualearth.net` (Bing). Se você usar layer remoto em outro host, adicione-o a `img-src`/`connect-src` na CSP.

## Primeira execução (do zero)

Uma instalação nova começa **vazia** — sem projeto, sem classes.

```bash
# 1) Cria o admin global inicial (interativo). Inicializa o banco vazio.
python -m backend.scripts.create_admin

# 2) Suba o app
python -m backend.run                                # wrapper recomendado (Ctrl+C em <1s)
# ou: uvicorn backend.main:app --host 0.0.0.0 --port 8000

# 3) Abra http://localhost:8000, faça login como admin.

# 4) Na aba "Projetos" da UI admin, crie o 1º projeto:
#    nome, kind (raster/vector/classification/detection), classes (cores),
#    layers (path .mbtiles / URL / bingmaps://), geometria do tile
#    (tile_px × meters_per_pixel) e os membros.
#    Equivalente HTTP: POST /api/admin/projects.

# 5) Crie os operadores/revisores na aba "Usuários" e adicione-os
#    como membros do projeto.

# 6) Importe tiles ao projeto. --project aceita id ou nome
#    (obrigatório quando há mais de um projeto).
python -m backend.scripts.import_points --point -23.550 -46.633 centro_sp --project meu-projeto
python -m backend.scripts.import_points --csv pontos.csv --project meu-projeto          # CSV: lat,lon,name
python -m backend.scripts.import_points --csv pontos.csv --block 3 --project meu-projeto # NxN ao redor
```

A adição de tiles é exclusivamente via CLI (`backend.scripts.import_points`) — a UI admin foca em curadoria, visualização e configuração.

## Executar

```bash
python -m backend.run                                # wrapper com --reload + Ctrl+C rápido
# ou
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 1 --timeout-keep-alive 2
```

Abra `http://localhost:8000`.

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

## Exportar dados

Quatro exportadores (um por kind), todos com `--status reviewed | classified | reviewed+classified` e `--project <id|name>`:

```bash
python -m backend.scripts.export_tiles ./exports            # raster        → GeoTIFF (default: revisados, remap EDGV)
python -m backend.scripts.export_features ./exports         # vector        → GeoJSON
python -m backend.scripts.export_classifications ./exports  # classification → CSV
python -m backend.scripts.export_detections ./exports       # detection     → GeoJSON (bboxes)

# Raster: IDs nativos 1..6 (sem remap EDGV) / mosaico
python -m backend.scripts.export_tiles ./exports --raw
python -m backend.scripts.export_tiles ./exports --mosaic
```

Cada export gera um `manifest.csv` com status/autoria/bbox por tile.

**Pela UI:** admins exportam pelo painel "Exportar dados" no detalhe do projeto — baixa ZIP no formato do kind. Endpoints: `GET /api/admin/projects/{id}/export?status=...` (sync) e `POST /api/admin/projects/{id}/export-jobs` (async, para grandes volumes — ver `docs/sistema.md` §9).

## Scripts utilitários

- `build_mbtiles.py` — converte raster grande → mbtiles XYZ servíveis
- `recolor_mbtiles.py` — recoloriza um mbtiles PNG por substituição exata de RGB
- `merge_db.py` — funde dois `tileclass.db` (junção de equipes/sessões)
- `verify_db.py` / `backup_db.py` — sanidade e backup do banco

## Licença

A definir.
