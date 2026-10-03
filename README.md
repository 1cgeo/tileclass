# TileClass

Anotação de tiles de satélite por projeto: máscara pixel-a-pixel (segmentação) ou rótulo único por tile (classificação). FastAPI + SQLite + Vanilla JS/MapLibre.

## Instalação

Requer Python 3.11+ (Node 20+ só para os testes JS).

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt

cp backend/config.example.yaml backend/config.yaml
python -c "import secrets; print(secrets.token_urlsafe(48))"   # cole em auth.jwt_secret
```

Se o PowerShell bloquear o `activate`: `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`.

O `config.yaml` só tem infra: `auth.jwt_secret` (obrigatório trocar, ou use a env `TILECLASS_JWT_SECRET`), `database.path` e o cache do overlay admin. Projetos, classes, layers e membros ficam no banco e são configurados pela UI.

## Primeira execução

```bash
python -m backend.scripts.create_admin   # cria o admin (interativo)
python -m backend.run                    # http://localhost:8000  (--port para trocar)
```

Depois, logado como admin:

1. **Projetos → Novo projeto:** nome, tipo (imutável), classes, geometria do tile (`tile_px × meters_per_pixel`, travada após o 1º tile) e layers.
2. **Usuários:** crie operadores e adicione-os como membros do projeto. Para revisar, o membro precisa ter o papel `reviewer`.
3. **Importe os tiles** pela CLI (cada ponto é o centro de um tile; `--block N` cria uma grade N×N ao redor):
   ```bash
   python -m backend.scripts.import_points --csv pontos.csv --block 3 --project meu-projeto   # CSV: lat,lon,name
   python -m backend.scripts.import_points --point -23.55 -46.63 centro --project meu-projeto
   ```

### Layers

Cada layer aceita:

- caminho de um `.mbtiles` (relativo a `backend/`). Arquivos grandes vão em `data_external/`, ex.: `../data_external/img.mbtiles`.
- URL de tile-server `https://host/{z}/{x}/{y}.png`. Hosts diferentes de ArcGIS/Bing precisam ser liberados na CSP em `backend/main.py`.
- `bingmaps://{z}/{x}/{y}` para usar Bing Maps.

Para gerar um `.mbtiles` a partir de uma pasta de GeoTIFFs em EPSG:3857: `python -m backend.scripts.build_mbtiles <pasta> <out.mbtiles>`.

## Exportar

Pela UI (detalhe do projeto → **Exportar dados**, baixa ZIP) ou via CLI:

```bash
python -m backend.scripts.export_tiles ./out --project meu-projeto            # segmentação → GeoTIFF
python -m backend.scripts.export_classifications ./out --project meu-projeto  # classificação → CSV
```

Ambos aceitam `--status reviewed|classified|reviewed+classified` (padrão: `reviewed`). A segmentação gera um GeoTIFF por tile + `manifest.csv`; a classificação gera um único `classifications.csv`.

Remap de classes (segmentação): por padrão é automático — projetos com a paleta de 6 classes (IDs 1–6) saem remapeados para EDGV, os demais com os IDs originais. Force com `--edgv` ou `--raw` (na UI: "Remapeamento de classes"). O manifest e o nome do ZIP baixado pela UI registram o remap aplicado (`..._edgv_...` / `..._raw_...`). `--mosaic` gera um mosaico por projeto.

## Manutenção

`backup_db`, `verify_db`, `merge_db`, `recompute_class_counts`, `recolor_mbtiles` em `backend/scripts/` (`python -m backend.scripts.<nome> --help`).

## Testes

```bash
npm install
npm run test:all   # pytest + vitest + E2E (Puppeteer)
```
