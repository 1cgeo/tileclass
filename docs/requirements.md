# TileClass — Especificação

Spec de referência. Contratos de API, modelo de dados e invariantes; detalhes
de UI/UX vivem no código e em `CLAUDE.md`.

## 1. Visão Geral

Aplicação web para classificação pixel-a-pixel de tiles de imagens de satélite.
Operadores recebem tiles 256×256 (resolução 2.5 m/pixel = 640 m × 640 m no
chão) e atribuem classes a cada pixel. O conjunto de classes, suas cores, e
quais imagens/máscaras de referência aparecem para o operador são definidos
**por projeto**. O sistema gerencia múltiplos projetos simultaneamente — cada
um com sua própria fila, classes, paleta, mbtiles e membros.

**Stack:** Python 3.11+ (FastAPI, sqlite3 nativo — sem ORM, Pillow, NumPy,
PyJWT, bcrypt, PyYAML, pyproj, rasterio) + Vanilla JS (sem framework, Canvas
HTML5, MapLibre GL JS).

**Escopo alvo:** ~1000 tiles, até 10 operadores simultâneos, intranet.

## 2. Arquitetura

- API REST (FastAPI) servindo o frontend estático.
- SQLite WAL como banco único.
- **Quatro tipos de projeto** (coluna `projects.kind`, imutável após criação):
  - `raster` — body do tile é uma **máscara PNG single-band** (BLOB,
    tile_px² bytes). Schema de domínio em `project_classes` (id+name+color).
  - `vector` — body é um **GeoJSON FeatureCollection** (TEXT) com
    LineStrings + properties. Schema de domínio em `project_attributes`
    (key+label+type+required+options). Para drenagem como grafo, o flag
    `topology_required` ativa validação de direção, conectividade
    (snap-tolerância intra-tile) e ausência de ciclos.
  - `classification` — body é um único `data_class_id` (INTEGER). Operador
    rotula o tile inteiro com uma classe de `project_classes`. Sem pause.
  - `detection` — body é um **GeoJSON FeatureCollection** (TEXT) de
    **bounding boxes** (Polygons retângulos alinhados aos eixos), uma classe
    de `project_classes` por caixa (`properties.class_id`). O flag
    `box_required` exige ≥1 caixa no submit (senão tile vazio é negative
    sample válido). Export GeoJSON via `export_detections.py`.
- Imagens e máscaras de referência por projeto. Cada projeto declara até 5
  layers: `primary` (obrigatório), `secondary`, `tertiary` (atalhos D/R),
  `ref_primary`, `ref_secondary` (atalhos T/Y, máscaras categorizadas). Cada
  layer pode apontar para um **mbtiles local** (servido por
  `/api/projects/{id}/xyz/{layer}/{z}/{x}/{y}.{ext}`), para uma **URL
  remota** de tile-server (Martin, TileServer-GL etc., contendo `{z}/{x}/{y}`)
  ou para o esquema **`bingmaps://{z}/{x}/{y}`** (Bing, reescrito para quadkeys
  no frontend). URL remota / bingmaps é fetchada direto pelo MapLibre — backend
  não é proxy.
- JWT (access 8h, refresh 24h, HS256). Role global `operator|admin` + flag
  `can_review`. Por projeto: `project_members.role ∈ {operator,reviewer,admin}`.

## 3. Modelo de Dados (SQLite)

### 3.1 `users`

| Coluna          | Tipo    | Notas                                 |
|-----------------|---------|---------------------------------------|
| id              | INTEGER | PK                                    |
| username        | TEXT    | Único                                 |
| password_hash   | TEXT    | bcrypt (cost ≥ 12)                    |
| role            | TEXT    | `operator` ou `admin`                 |
| active          | INTEGER | 0/1; default 1                        |
| can_review      | INTEGER | 0/1; admins recebem 1 automático      |
| created_at      | TEXT    | ISO 8601                              |

### 3.2 `projects`

| Coluna                       | Tipo    | Notas                                                                |
|------------------------------|---------|----------------------------------------------------------------------|
| id                           | INTEGER | PK                                                                   |
| name                         | TEXT    | Único                                                                |
| description                  | TEXT    | Livre                                                                |
| kind                         | TEXT    | `raster`(default)/`vector`/`classification`/`detection`. **Imutável.** |
| topology_required            | INTEGER | 0/1 — vector only; ativa direction + cycle check no submit           |
| box_required                 | INTEGER | 0/1 — detection only; quando 1, submit exige ≥1 caixa                |
| mask_complete_required       | INTEGER | 0/1; raster only — quando 1, submit rejeita pixels=255               |
| primary_mbtiles              | TEXT    | Path mbtiles **ou** URL remota com `{z}/{x}/{y}`. Obrigatório.       |
| secondary_mbtiles            | TEXT    | Opcional — path ou URL — atalho `D`                                  |
| tertiary_mbtiles             | TEXT    | Opcional — path ou URL — atalho `R`                                  |
| ref_mask_primary_mbtiles     | TEXT    | Opcional — máscara de referência (path ou URL) — atalho `T`          |
| ref_mask_secondary_mbtiles   | TEXT    | Opcional — máscara de referência (path ou URL) — atalho `Y`          |
| active                       | INTEGER | 0/1; quando 0, `/next` e `/next-preview` retornam 409 `project_inactive` |
| created_by                   | INTEGER | FK users.id (admin que criou)                                        |
| created_at                   | TEXT    | ISO 8601                                                             |

**Detecção de URL remota** (`project_service.is_remote_layer`): valores
começando com `http://`, `https://` ou `bingmaps://` são tratados como
tile-server remoto (Martin, TileServer-GL, Bing etc.). O backend valida que o
valor contém os placeholders `{z}/{x}/{y}` mas não baixa nem abre o recurso —
quem fetch é o MapLibre no cliente (`bingmaps://` é reescrito para o endpoint
quadkey do virtualearth.net via `transformRequest`). Valores que não começam
com esses esquemas são resolvidos como arquivo local (relativos a `backend/`
ou absolutos).

### 3.3 `project_classes`

| Coluna     | Tipo    | Notas                                                                |
|------------|---------|----------------------------------------------------------------------|
| project_id | INTEGER | FK projects.id (CASCADE)                                             |
| class_id   | INTEGER | 1..254, único por projeto                                            |
| name       | TEXT    | Nome da classe (livre)                                               |
| color      | TEXT    | `#RRGGBB`                                                            |
| ordering   | INTEGER | Ordem de exibição                                                    |

PK: `(project_id, class_id)`. Renomear/recolorir é livre; **remover** uma
classe é rejeitado quando o projeto tem qualquer tile.

`project_classes` só é usado por projetos com `kind='raster'`. Projetos
vetoriais usam `project_attributes` (próxima seção).

### 3.4 `project_attributes` (vector projects)

| Coluna       | Tipo    | Notas                                                              |
|--------------|---------|--------------------------------------------------------------------|
| project_id   | INTEGER | FK projects.id                                                     |
| key          | TEXT    | snake_case (a-z, 0-9, _) — usada em `feature.properties[key]`      |
| label        | TEXT    | Rótulo legível para o painel do operador                           |
| type         | TEXT    | `text` \| `number` \| `enum` \| `boolean`                          |
| required     | INTEGER | 0/1; submit rejeita features sem este atributo quando 1            |
| options_json | TEXT    | JSON list de strings, obrigatório quando type=`enum`               |
| ordering     | INTEGER | Ordem do form                                                      |

PK: `(project_id, key)`. Renomear/relabel é livre; **remover** uma chave
é rejeitado se alguma feature em `tiles.data_geojson` desse projeto a
referencia (`409 attribute_in_use`).

Para projetos com `topology_required=True`, a chave reservada `direction`
(enum {forward, reverse, both}) é obrigatória por feature, e o backend
roda `vector_utils.validate_topology` no submit (snap-tolerância de
endpoints, sem ciclos via DFS).

### 3.5 `project_members`

| Coluna     | Tipo    | Notas                                                                |
|------------|---------|----------------------------------------------------------------------|
| project_id | INTEGER | FK projects.id (CASCADE)                                             |
| user_id    | INTEGER | FK users.id (CASCADE)                                                |
| role       | TEXT    | `operator`, `reviewer` ou `admin` (escopo do projeto)                |

PK: `(project_id, user_id)`. Admins globais (`users.role='admin'`) ignoram
membership. `users.can_review` permanece como veto temporário.

### 3.6 `tiles`

| Coluna         | Tipo    | Notas                                                    |
|----------------|---------|----------------------------------------------------------|
| id             | INTEGER | PK                                                       |
| project_id     | INTEGER | **NOT NULL** FK projects.id                              |
| name           | TEXT    | Identificador legível                                    |
| bbox_west      | REAL    | Longitude oeste (graus)                                  |
| bbox_south     | REAL    | Latitude sul                                             |
| bbox_east      | REAL    | Longitude leste                                          |
| bbox_north     | REAL    | Latitude norte                                           |
| status         | TEXT    | Ver 3.7                                                  |
| assigned_to    | INTEGER | FK users.id                                              |
| classified_by  | INTEGER | FK users.id                                              |
| reviewed_by    | INTEGER | FK users.id                                              |
| classified_at  | TEXT    | ISO 8601                                                 |
| reviewed_at    | TEXT    | ISO 8601                                                 |
| data_png       | BLOB    | Raster body — PNG single-band 256×256, IDs + `255`. Null em vector. |
| data_geojson   | TEXT    | Vector body — FeatureCollection serializado. Null em raster. |
| feature_count  | INTEGER | Cache do nº de features em data_geojson (vector)         |
| class_counts   | TEXT    | Cache JSON {class_id: pixel_count} em raster             |
| problem_note   | TEXT    | Nota livre quando `status='problem'`                     |
| version        | INTEGER | Incrementado a cada mutação; usado em CAS                |
| paused_at      | TEXT    | ISO 8601 quando o tile está pausado                      |
| blocked_from   | TEXT    | Status original guardado durante `status='blocked'`      |
| last_heartbeat_at | TEXT | Editor pings; auto-pause sweep usa pra liberar zumbis    |

**Geometria do tile.** Cada tile é definido pelo **centro geodésico**.
`backend/geo.bbox_from_center(lat, lon)` calcula ±320 m em cada direção
cardeal usando `pyproj.Geod` (WGS84). Pixel = 2.5 m em qualquer latitude.
Não há `zoom`/`tile_x`/`tile_y` no schema — composição XYZ é resolvida no
frontend pelo MapLibre a partir da bbox.

### 3.7 `action_log`

| Coluna     | Tipo    | Notas                                                 |
|------------|---------|-------------------------------------------------------|
| id         | INTEGER | PK                                                    |
| user_id    | INTEGER | FK users.id                                           |
| tile_id    | INTEGER | FK tiles.id (nullable)                                |
| action     | TEXT    | Ver lista abaixo                                      |
| detail     | TEXT    | JSON livre                                            |
| created_at | TEXT    | ISO 8601                                              |

Ações principais: `assign_classify`, `assign_review`, `classify`, `review`,
`pause`, `resume`, `report_problem`, `reset`, `re_review`, `block`,
`unblock`, `assign`, `unassign`, `delete_tile`, `create_user`,
`set_user_active`, `set_user_role`, `set_user_can_review`,
`project_create`, `project_update`, `project_delete`,
`project_classes_update`, `project_member_set`, `project_member_remove`.

Pareamento `assign_*→classify/review` em pares por `(user_id, tile_id)` é o
que alimenta as métricas de duração no dashboard.

### 3.8 Status do Tile

```
pending ──> in_progress ──> classified ──> in_review ──> reviewed
              │               │              │
              ▼               ▼              ▼
            problem        problem        problem
```

Estados:
- `pending`: nenhum operador atribuído.
- `in_progress`: operador classificando.
- `classified`: aguardando revisão.
- `in_review`: revisor atribuído.
- `reviewed`: aprovado.
- `problem`: classificador ou revisor reportou problema.
- `blocked`: admin removeu da fila temporariamente; o status original fica
  em `blocked_from`.

Transições de admin: `problem → pending` (reset), `reviewed → in_review`
(re-review), `pending|classified|reviewed → blocked` e
`blocked → <blocked_from>`. `in_progress`/`in_review`/`problem` **não**
podem ser bloqueados.

## 4. API REST

Todos os endpoints fora de `/api/auth/login` exigem `Authorization: Bearer
<access_token>`. `403` em rota admin para operador; `401` se token expirou
ou foi revogado.

### 4.1 Auth (`/api/auth`)

| Método | Path        | Descrição                                            |
|--------|-------------|------------------------------------------------------|
| POST   | `/login`    | `{username, password}` → `{access_token, refresh_token}`. Rate-limit 5/min/IP. |
| POST   | `/refresh`  | `{refresh_token}` → novo par.                        |
| GET    | `/me`       | Dados do usuário logado.                             |
| POST   | `/logout`   | Revoga `jti` do access (e do refresh, se enviado).   |

JWT carrega `sub` (user_id), `username`, `role`, `typ` (`access`/`refresh`),
`jti`, `exp`. Tipo errado em refresh → 401.

### 4.2 Projetos (`/api/projects`)

| Método | Path                                            | Descrição                                                      |
|--------|-------------------------------------------------|----------------------------------------------------------------|
| GET    | `/`                                             | Lista projetos visíveis ao user (membership; admin vê todos).  |
| GET    | `/{id}`                                         | Detalhes + classes + map de layers (URLs prontas com extensão). 403 se não-membro. |
| GET    | `/{id}/xyz/{layer}/{z}/{x}/{y}.{ext}`           | Tile bytes do mbtiles do projeto. `layer ∈ {primary, secondary, tertiary, ref_primary, ref_secondary}`. 404 se layer for URL remota (cliente fetch direto) ou não configurada; 403 se não-membro. |

Admin (`/api/admin/projects`):

| Método | Path                                | Descrição                                                                |
|--------|-------------------------------------|--------------------------------------------------------------------------|
| POST   | `/`                                 | Cria projeto. Body inclui paths, classes, `mask_complete_required`. Valida que paths existem. |
| PATCH  | `/{id}`                             | Patch parcial: nome, descrição, paths, `mask_complete_required`, `active`. |
| DELETE | `/{id}`                             | Deleta projeto. 409 `project_has_tiles` se há tiles — desative em vez de excluir. |
| PUT    | `/{id}/classes`                     | Substitui o conjunto de classes. Remoção bloqueada quando o projeto tem tiles. |
| GET    | `/{id}/members`                     | Lista membros + role.                                                    |
| POST   | `/{id}/members`                     | `{user_id, role}` — upsert.                                              |
| DELETE | `/{id}/members/{user_id}`           | Remove membro.                                                           |

### 4.3 Operador (`/api/tiles`)

Todos os endpoints abaixo aceitam `?project_id=<id>`. Quando o user tem
exatamente uma membership, o param é opcional; com múltiplas, omitir
retorna 400 `project_id_required`.

| Método | Path                         | Descrição                                                       |
|--------|------------------------------|-----------------------------------------------------------------|
| GET    | `/next`                      | Próximo tile do projeto. 204 vazio. 409 `project_inactive` se `active=False`. |
| GET    | `/assigned`                  | Tile atualmente atribuído (para resume sem puxar fila).         |
| GET    | `/next-preview`              | Peek sem atribuir (pré-carga). Bloqueado em projeto inativo.    |
| GET    | `/queue-stats`               | Tamanhos de fila por status.                                    |
| GET    | `/{id}`                      | Metadados (`TileOut`).                                          |
| GET    | `/{id}/image`                | PNG da máscara atual.                                           |
| GET    | `/{id}/history`              | Linha do tempo de ações.                                        |
| POST   | `/{id}/classify`             | Body: 65536 bytes raw. Header opcional `X-Tile-Version`.        |
| POST   | `/{id}/review`               | Idem, valida que `assigned_to == user` e tile em `in_review`.   |
| POST   | `/{id}/report-problem`       | `{note}` → status `problem`.                                    |
| POST   | `/{id}/pause`                | Body: 65536 bytes (snapshot). Solta atribuição preservando trabalho. |
| POST   | `/{id}/resume`               | Reatribui um tile pausado pelo próprio usuário.                 |

| Método | Path                  | Descrição                                                              |
|--------|-----------------------|------------------------------------------------------------------------|
| GET    | `/api/me/stats-today` | `{count}` — classificados hoje pelo user (opcionalmente por projeto).  |

**Regras de `/next`:**
1. Resume: se o user tem tile em `in_progress`/`in_review`, devolve esse.
2. Tile pausado pelo próprio user (FIFO por `paused_at`).
3. Fila de revisão: `classified` cujo `classified_by != user`.
4. Fila pending.
5. 204 se nada.

Atribuição é transacional (`BEGIN IMMEDIATE` + `SELECT … LIMIT 1` + `UPDATE`)
— dois usuários nunca recebem o mesmo tile.

**Regras de classificação/revisão:**
- Body é **raw bytes** (`Uint8Array` de 65536 bytes), não PNG. Backend
  converte com Pillow.
- Validação: tamanho exato + valores em `{class_ids do projeto, 255}`.
  Submit rejeita se houver `255` **e** o projeto tem `mask_complete_required=True`;
  resposta traz a contagem.
- Autorização: `assigned_to == current_user`.
- Concorrência otimista: header `X-Tile-Version`. Se diferente de
  `tiles.version`, retorna `409` com `error=tile_modified` — o frontend
  decide se sobrescreve.

### 4.3 Admin (`/api/admin`)

Todos atrás de `Depends(auth.require_admin)`.

| Método | Path                                   | Descrição                                              |
|--------|----------------------------------------|--------------------------------------------------------|
| GET    | `/dashboard`                           | Totais, completion%, daily, per-operator, médias de duração, paused. |
| GET    | `/tiles`                               | Lista com filtros `status`, `user_id`, `date_from/to`, `paused`, `q`, `limit`, `offset`. Header `X-Total-Count`. |
| GET    | `/tiles/map`                           | Lista enxuta para o mapa admin.                        |
| GET    | `/tiles/problems`                      | Atalho para `status=problem`.                          |
| GET    | `/tiles/{id}/thumbnail`                | PNG colorizado da máscara (param `size`).              |
| GET    | `/tiles/{id}/satellite-thumbnail`      | PNG do satélite recortado pela bbox.                   |
| GET    | `/mask-tiles/{project_id}/{z}/{x}/{y}.png` | Overlay XYZ rasterizado on-the-fly + cache mbtiles per-projeto (palette = `project_classes[project_id]`, cache em `<base>_p<project_id>.mbtiles`). |
| POST   | `/tiles/{id}/reset`                    | Limpa máscara, volta para `pending`.                   |
| POST   | `/tiles/{id}/re-review`                | `reviewed → in_review`.                                |
| POST   | `/tiles/{id}/assign`                   | Atribui a um user específico.                          |
| POST   | `/tiles/{id}/unassign`                 | Solta atribuição.                                      |
| POST   | `/tiles/{id}/admin-pause`              | Pausa em nome do operador ausente.                     |
| POST   | `/tiles/{id}/block`                    | Bloqueia (guarda status em `blocked_from`).            |
| POST   | `/tiles/{id}/unblock`                  | Restaura ao `blocked_from`.                            |
| DELETE | `/tiles/{id}`                          | Apaga tile; só permitido se já está em `problem`.      |
| POST   | `/tiles/bulk/{reset,re-review,report-problem,unassign,block,unblock}` | Ações em massa. |
| POST   | `/tiles/assign`                        | Pré-carga FIFO de fila pessoal (multi-tile).           |
| GET    | `/users`                               | Lista com stats.                                       |
| POST   | `/users`                               | Cria usuário.                                          |
| PATCH  | `/users/{id}/active`                   | Liga/desliga conta.                                    |
| PATCH  | `/users/{id}/role`                     | Promove/rebaixa.                                       |
| PATCH  | `/users/{id}/can-review`               | Habilita/desabilita revisão.                           |

### 4.4 Config legada — removida

Os endpoints `/api/config/classes`, `/api/config/tileserver`, `/api/xyz/*`,
`/api/dsg/*` e `/api/mb/*` foram removidos. Configuração e tiles raster
agora vivem sob `/api/projects/{id}` (ver 4.2).

## 5. Frontend

Detalhes (layout, atalhos, comportamento de canvas, undo, backup local) vivem
em `CLAUDE.md` (seções "Regras críticas de frontend" e "UX — inegociáveis")
e no código (`frontend/js/`). Pontos contratuais:

- Três camadas sobrepostas: MapLibre (satélite, `interactive: false`),
  canvas da máscara (`globalAlpha`), canvas de cursor (recebe eventos).
- `Uint8Array(65536)` é a fonte de verdade da máscara. Canvas é só
  visualização.
- Atalho hold `Space` esconde a máscara (keydown/keyup, não toggle).
- Sem auto-avanço pós-submit: idle screen com "Tile enviado ✓"; o operador
  clica para puxar o próximo (respiro entre cartas).
- Backup do `Uint8Array` em `localStorage` durante edição; restore silencioso
  no F5 com toast "Trabalho local restaurado." (sem banner de confirmação).
- JWT refresh proativo (60s antes de expirar) + reativo no 401.

## 6. Configuração (`backend/config.yaml`)

> **Nota:** o `config.yaml` carrega **apenas o que não é configurável no nível
> da aplicação** — infra e segredos. **Não há dado de domínio aqui:** projetos,
> classes, layers, geometria de tile e membros vivem nas tabelas do banco e são
> criados/editados pela UI admin (aba Projetos) ou `/api/admin/projects[...]`.
> Uma instalação nova começa **vazia** — sem projeto, sem classes; o admin cria
> o primeiro projeto e configura tudo pela aplicação.

```yaml
mask_overlay:                       # infra do cache do overlay admin
  cache_path: "data/mask_overlay_cache.mbtiles"
  min_zoom: 8
  max_zoom: 18

auth:
  jwt_secret: "<gerar>"             # ou via env TILECLASS_JWT_SECRET
  access_token_expiry_hours: 8
  refresh_token_expiry_hours: 24

database:
  path: "tileclass.db"
```

**Migração legada:** ao migrar um DB pré-projetos, `database._migration_seed_project`
cria um projeto "default" com paleta **hardcoded** (6 classes, layers vazios) só
para os tiles órfãos não ficarem sem `project_id` — editável depois pela UI. Isso
**não** vem do `config.yaml` e **não** ocorre em DBs novos.

**Convenção de paths grandes:** os layers de um projeto que apontam para
`.mbtiles` (GBs) seguem a convenção `../data_external/<arquivo>.mbtiles`
(relativo a `backend/`), resolvida em `project_service.resolve_mbtiles_path`.
Override do arquivo de config via env
`TILECLASS_CONFIG=<path>` (E2E). Rate limit desligável via
`TILECLASS_DISABLE_RATE_LIMIT=1` (apenas E2E).

## 7. Scripts CLI

Rodar como módulo (`python -m backend.scripts.<name>`) para imports
relativos funcionarem.

- `create_admin` — cria usuário admin inicial (interativo).
- `import_points [--project <id|name>] --point <lat> <lon> <name> | --csv pontos.csv [--block N]` —
  importa tiles a partir de pontos centrais; `--block N` gera bloco N×N
  contíguo (gap < 1 mm validado). `--project` é obrigatório quando há
  mais de um projeto no banco.
- `import_cq_tiles [--project <id|name>] --geoparquet <file> [--seed empty|raw]` — importa de
  geoparquet do CQ.
- `import_qc_tiles [--project <id|name>] --csv qc_tiles.csv --bdf-dir <dir>` — importa lote do
  QC com seed mask do argmax.
- `export_tiles <out_dir> [--status reviewed|reviewed+classified] [--raw]
  [--mosaic] [--manifest <path>] [--project <id|name>]` — GT extractor (ver §8).
- `build_mbtiles <raster_in> <out.mbtiles>` — converte raster → MBTiles XYZ.
- `build_xyz_pyramid <raster_in> <out_dir>` — pirâmide XYZ em disco.
- `merge_db --primary <db> --secondary <other.db>` — funde DBs. Projetos
  com mesmo nome são reutilizados; demais são copiados (classes + membros);
  uniqueness de tiles é por `(project_id, bbox)`.

## 8. GT Extractor (`export_tiles.py`)

Interface canônica para outras aplicações consumirem máscaras finalizadas.
Output bit-a-bit compatível com `treinamento_6c/<split>/<split>_masks/`.

- Output: `<out_dir>/gt_<tile.name>.tif` — GeoTIFF single-band uint8 256×256,
  EPSG:4326, deflate, NODATA=255. Transform via
  `rasterio.transform.from_bounds(west, south, east, north, 256, 256)`
  (pixel ≈ 2.5 m na latitude do centro).
- `manifest.csv` — uma linha por tile com filename, tile_id, name, status,
  classified_by, reviewed_by, classified_at, reviewed_at, bbox_*.
- Filtros de status: `reviewed` (padrão, GT estrito) ou `reviewed+classified`
  (inclui não-revisados, dataset preliminar).
- Remap canônico TileClass → EDGV (ativo por padrão; `--raw` desativa):

  | TileClass | Nome              | EDGV | Nome EDGV       |
  |-----------|-------------------|------|-----------------|
  | 1         | Massa d'água      | 0    | agua            |
  | 2         | Área edificada    | 1    | edif            |
  | 3         | Floresta          | 4    | floresta        |
  | 4         | Campo             | 3    | campo           |
  | 5         | Cultivo           | 5    | veg_cultivada   |
  | 6         | Terreno exposto   | 2    | terr_exp        |
  | 255       | NODATA            | 255  | (ignore_index)  |

  Constante `EDGV_REMAP_LUT` no topo do script — único ponto de verdade.

## 9. Cuidados Técnicos Críticos

### 9.1 Concorrência
- `/api/tiles/next` é `BEGIN IMMEDIATE` + SELECT + UPDATE atômico. Garantido
  por teste com 10 operadores em paralelo (`threading.Barrier`).
- WAL ligado (`PRAGMA journal_mode=WAL`).

### 9.2 Geometria
- `TILE_PX=256`, `METERS_PER_PX=2.5`, `TILE_METERS=640`.
- `bbox_from_center` usa `pyproj.Geod` (WGS84) — precisão < 1 mm em qualquer
  latitude. `offset_center(lat, lon, dx, dy)` garante adjacência sem gap.
- Validado em `test_geo.py` (14 testes) e `test_raster_worldwide.py` (22
  testes em 20 pontos mundiais, equador → Antártica).

### 9.3 Performance do Canvas
- Não redesenhar inteiro; dirty rectangle por gesture.
- `drawOutlines` itera 65k pixels — só roda quando `!drawing`.
- Undo armazena só pixels alterados (`Uint32Array positions` +
  `Uint8Array prevValues`), 50 níveis.

### 9.4 Mapeamento de Coordenadas
- `getBoundingClientRect()` → fração → `Math.floor(frac * 256)` com clamp
  em `[0..255]`. Canto inferior-direito deve dar `(255, 255)`.

### 9.5 Integridade
- Validar 65536 bytes exatos e valores em `{1..6, 255}` no backend
  (`mask_utils.validate_partial`, IDs derivados do config).
- PNG armazenado: single-band ("L"), 8 bits, 256×256, sem compressão com
  perda. Pillow faz a conversão `bytes ↔ PNG`.

### 9.6 Segurança
- bcrypt cost ≥ 12; JWT HS256; rate limit 5/min/IP em login; endpoints admin
  atrás de `Depends(auth.require_admin)`. Sem `innerHTML` com dados de
  usuário (use `escapeHtml` ou `textContent`).
- Logout revoga `jti` em `token_blacklist`.

## 10. Estrutura de Diretórios

```
tileclass/
├── backend/
│   ├── main.py                 # FastAPI app + lifespan + middleware + static SPA
│   ├── routers/                # auth, operator, admin, config
│   ├── auth.py                 # JWT, bcrypt, rate limit, blacklist, role middleware
│   ├── models.py               # Schemas Pydantic
│   ├── database.py             # Conexão SQLite (WAL), schema, transaction(), log_action
│   ├── config.py               # Loader de config.yaml (singleton)
│   ├── tile_service.py         # Fila, atribuição, submit, problema, pause/resume
│   ├── admin_service.py        # Fachada que re-exporta backend/admin/
│   ├── admin/                  # dashboard, tiles_query, tiles_mutations, users, thumbnails
│   ├── mask_utils.py           # Uint8Array ↔ PNG "L" + validação
│   ├── mask_tile_service.py    # Cache mbtiles do overlay admin
│   ├── mbtiles_service.py      # Reader read-only (singletons primary/dsg/mb)
│   ├── geo.py                  # bbox_from_center (pyproj.Geod WGS84)
│   ├── tile_grid.py            # Helpers Web Mercator
│   ├── config.yaml
│   └── scripts/                # create_admin, import_points/cq/qc, export_tiles,
│                               # build_mbtiles, build_xyz_pyramid, merge_db
├── data_external/              # MBTiles grandes (gitignored)
├── frontend/
│   ├── index.html              # SPA (login / editor / admin)
│   ├── css/style.css
│   ├── vendor/maplibre/
│   └── js/
│       ├── app.js              # Router por role
│       ├── api.js              # fetch wrapper + JWT refresh
│       ├── editor.js           # Canvas, ferramentas, undo/redo, submit
│       ├── mask-core.js        # Lógica pura (paint, flood, screenToLogical)
│       ├── backup.js           # localStorage backup
│       ├── admin.js / admin/   # Dashboard, tiles, users, viewer
│       ├── minimap.js          # MapLibre 3×3
│       ├── maplib.js           # Helpers MapLibre
│       ├── utils.js            # hexToRgb, blobToImage, escapeHtml
│       └── toast.js
├── docs/requirements.md        # Este arquivo
├── tests/                      # pytest + Vitest + Puppeteer (ver CLAUDE.md)
└── requirements.txt
```

## 11. Critérios de Aceitação

### 11.1 Auth
- Login válido devolve par `{access, refresh}`; inválido → 401.
- Refresh proativo evita expiração durante uso contínuo.
- Logout revoga o `jti`; chamadas seguintes com o mesmo token → 401.
- Roles enforçados em todos os endpoints admin.

### 11.2 Fila
- Dois operadores nunca recebem o mesmo tile.
- Fila de revisão tem prioridade sobre pending.
- Revisor nunca recebe um tile que ele próprio classificou.
- F5 não perde trabalho: `/next` devolve o tile atribuído.
- 204 quando vazio; UI mostra estado claro.

### 11.3 Editor
- Pintar com brush não tem lag (< 16 ms por frame, sem outline durante
  gesture).
- Borracha volta para 255.
- Undo/Redo byte-exato (50 níveis, patches).
- Slider de opacidade + Space (hold) + modo contornos (`O`).
- Submit rejeita `255` no array; pisca pixels faltantes 2 s.

### 11.4 Revisão
- Banner "MODO REVISÃO" + nome do classificador.
- Botão de submit muda para "Aprovar revisão".
- Revisor pode editar antes de aprovar.

### 11.5 Admin
- Dashboard: totais, completion %, série diária, per-operator, médias de
  duração `assign→classify` e `assign→review`.
- Bulk actions atômicas (`reset_many`, `re_review_many`, `assign_many`,
  `block_many`, `unblock_many`, `unassign_many`, `report_problem_many`).
- Block guarda status original em `blocked_from`; unblock restaura.
- Delete só permitido se `status='problem'`.

### 11.6 Geometria/Export
- Tile sempre 640 m × 640 m em qualquer latitude (pixel = 2.5 m em ambos
  eixos).
- GeoTIFF exportado tem CRS=EPSG:4326, NODATA=255, bbox correta para 20
  pontos mundiais.

### 11.7 Performance
- `/next` < 2 s; pintura < 16 ms/frame; 10 operadores simultâneos sem
  degradação (validado por testes de concorrência).

## 12. Dependências

`requirements.txt`:

```
fastapi
uvicorn
pyjwt
bcrypt
pillow
pyyaml
numpy
python-multipart
pyproj
rasterio
```

Sem ORM. `sqlite3` nativo com queries SQL parametrizadas (`?`).
