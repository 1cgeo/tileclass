# TileClass

Aplicação web para classificação pixel-a-pixel de tiles de satélite (256×256, 6 classes).
FastAPI + SQLite + Vanilla JS.

## Setup

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/Mac
source .venv/bin/activate

pip install -r requirements.txt
```

Edite `backend/config.yaml`:
- `tileserver.url_template` — URL do TileServer-GL na LAN
- `auth.jwt_secret` — troque em produção
- `classes` — nomes e cores

## Inicialização

```bash
# 1. Criar admin inicial
python -m backend.scripts.create_admin

# 2. Importar tiles (CSV com header: name,bbox_west,bbox_south,bbox_east,bbox_north,zoom,tile_x,tile_y)
python -m backend.scripts.import_tiles caminho/do/tiles.csv
```

## Executar

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Abrir `http://localhost:8000`.

## Testes

```bash
python -m pytest tests/ -v
```

Cobre: autenticação (incluindo rate limit e roles), fila de tiles (atribuição atômica, resume, prioridade revisão, bloqueio de auto-revisão), submissão (validação de tamanho, pixels faltantes, autorização), concorrência (10 chamadas paralelas em `/next`), admin (dashboard, bulk reset, desativar usuário, thumbnail).

## Exportar resultados

```bash
python -m backend.scripts.export_tiles ./exports --mosaic
```

Gera um GeoTIFF por tile revisado (EPSG:4326) e, com `--mosaic`, um mosaico único.

## Estrutura

```
backend/           FastAPI + SQLite (sem ORM)
  main.py          App e rotas
  auth.py          JWT + bcrypt
  tile_service.py  Fila atômica, submit, problema
  admin_service.py Dashboard, gestão
  mask_utils.py    Uint8Array <-> PNG
  scripts/         CLIs (create_admin, import_tiles, export_tiles)
frontend/          Vanilla JS, sem framework
  index.html
  css/style.css
  js/{app,editor,admin,minimap,api,toast}.js
docs/
  requirements.md        Especificação completa
  implementation_plan.md Plano em fases
```

## Atalhos principais

| Tecla | Ação |
|---|---|
| 1–6 | Classe ativa |
| B / E / G | Pincel / Borracha / Balde |
| + / − | Tamanho do pincel |
| Ctrl+Z / Ctrl+Shift+Z | Desfazer / Refazer |
| Espaço (segurar) | Esconder máscara |
| O | Modo só contornos |
| [ / ] | Opacidade |
| Ctrl+S | Submeter |
