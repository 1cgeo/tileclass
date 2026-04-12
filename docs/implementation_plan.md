# Plano de Implementação — TileClass

Ordem proposta. Cada fase entrega algo testável de ponta-a-ponta.

---

## Fase 0 — Andaime do projeto

**Objetivo:** esqueleto que roda, serve um "hello" e tem config carregada.

- [ ] Criar estrutura de diretórios (`backend/`, `frontend/`, `backend/scripts/`).
- [ ] `requirements.txt` com: fastapi, uvicorn, pyjwt, bcrypt, pillow, pyyaml, numpy, python-multipart.
- [ ] `backend/config.yaml` com TileServer URL, 6 classes (paleta sugerida), JWT secret placeholder, DB path.
- [ ] `backend/main.py` mínimo: carrega config, monta FastAPI, serve `frontend/` como estáticos, rota `GET /api/health`.
- [ ] `frontend/index.html` mínimo com `<canvas>` placeholder.
- [ ] README curto com instruções de setup.

**Critério:** `uvicorn backend.main:app` sobe; `GET /api/health` responde; `/` serve o index.

---

## Fase 1 — Banco + modelos

**Objetivo:** schema SQLite criado e scripts de seed funcionando.

- [ ] `backend/database.py`: conexão com `PRAGMA journal_mode=WAL`, `foreign_keys=ON`. Função `init_db()` que cria `users`, `tiles`, `action_log` (idempotente).
- [ ] `backend/models.py`: Pydantic para request/response (LoginIn, TileOut, ClassifyIn, etc.).
- [ ] `scripts/create_admin.py`: CLI (prompt username/senha, bcrypt cost 12, INSERT).
- [ ] `scripts/import_tiles.py`: lê CSV (`id, bbox_*, zoom, tile_x, tile_y`), insere `status='pending'` e inicializa `data_png` com PNG 256×256 cheio de 255.
- [ ] Helper central: `encode_mask(np.ndarray) -> bytes` e `decode_mask(bytes) -> np.ndarray` (Pillow mode "L", 256×256, validação de tamanho).

**Critério:** rodar os dois scripts resulta em DB populado; `sqlite3` CLI confirma registros.

---

## Fase 2 — Autenticação

**Objetivo:** login JWT funcional, middleware de autorização por role.

- [ ] `backend/auth.py`: bcrypt verify, gerar access/refresh tokens (HS256, 8h/24h), decodificar, dependency `get_current_user`, dependency `require_admin`.
- [ ] Endpoints: `POST /api/auth/login`, `POST /api/auth/refresh`, `GET /api/auth/me`.
- [ ] Rate limit simples (dict IP→timestamps, janela 1min, 5 tentativas) — suficiente para LAN de 10 usuários.
- [ ] `frontend/js/auth.js` + `api.js`: fetch wrapper que anexa `Authorization: Bearer`, trata 401 com refresh, redireciona se refresh também falhar.
- [ ] Tela de login mínima (`frontend/index.html` com form) e roteamento SPA básico no `app.js` (editor vs admin conforme role).

**Critério:** login válido retorna JWT; inválido recebe 401; operador vê tela de editor, admin vê tela de admin.

---

## Fase 3 — Fila de tiles (atribuição atômica)

**Objetivo:** `/api/tiles/next` devolve tiles sem conflito entre operadores.

- [ ] `tile_service.get_next_tile(user)` — **transação `BEGIN IMMEDIATE`**:
  1. Buscar `in_review` não atribuído e `classified_by != user.id` → se achar, atribuir, `status=in_review`, `assigned_to=user.id`, commit, retornar.
  2. Caso contrário, buscar `pending` → atribuir, `status=in_progress`, `assigned_to=user.id`, commit, retornar.
  3. Se nada: commit, retornar 204/mensagem.
- [ ] `GET /api/tiles/{id}` — metadados + raw bytes da máscara atual (endpoint separado `/image` retorna bytes binários).
- [ ] Teste de concorrência: 10 threads chamando `/next` em paralelo sobre 5 tiles — cada tile deve ser entregue uma única vez.

**Critério:** teste concorrente passa; prioridade revisão > pending verificada; auto-review bloqueado.

---

## Fase 4 — Editor (canvas + ferramentas)

**Objetivo:** operador consegue abrir tile, pintar e submeter.

Subfases:

- [ ] **4.1 Três camadas canvas**: satélite (XYZ), máscara, interação. Posicionamento absoluto, mesmo tamanho de display.
- [ ] **4.2 Satélite XYZ**: a partir de `tile_x/tile_y/zoom` do tile, compor a imagem de fundo buscando do TileServer-GL. Pode ser um único tile (se alinhado) ou composição de 4 tiles quando a bbox atravessa.
- [ ] **4.3 Mapeamento de coordenadas** (crítico): mouse CSS → canvas → lógico 0-255. Escrever teste manual: clicar nos 4 cantos e imprimir a coordenada lógica.
- [ ] **4.4 Uint8Array 65536 + renderização**: função que redesenha a máscara usando `ImageData`. Aplicar cor de classe, transparência para 255.
- [ ] **4.5 Brush**: tamanhos 1/3/5/7/11, interpolação entre mousemove (Bresenham ou amostragem densa), dirty rectangle.
- [ ] **4.6 Borracha** (valor 255, mesma lógica do brush).
- [ ] **4.7 Undo/Redo**: stack de patches `{positions: Uint16Array, prevValues: Uint8Array}` por gesture. 50 níveis.
- [ ] **4.8 Slider de opacidade + Space** (hold para esconder) + modo contornos (`O`).
- [ ] **4.9 Seleção de classe** (1-6, destaque visual, painel lateral).
- [ ] **4.10 Indicador de pixels** classificados (contador incremental, não varrer array a cada pintura).
- [ ] **4.11 Submissão**: checa 255; destaca pixels faltantes piscando; envia `Uint8Array` como `application/octet-stream`; backend valida, salva, retorna próximo.
- [ ] **4.12 Flood fill** (`G`, conectividade 4) — recomendado.
- [ ] **4.13 Mini-mapa 3×3** com tile atual destacado.
- [ ] **4.14 Cursor do brush** (círculo/quadrado proporcional ao tamanho).
- [ ] **4.15 Backup em localStorage** durante edição.
- [ ] **4.16 Pré-carregamento** do próximo tile.

**Critério:** operador loga, recebe tile, pinta 100%, submete, recebe próximo — sem recarregar.

---

## Fase 5 — Fluxo de revisão e problema

- [ ] Editor reconhece `status=in_review` e mostra banner "MODO REVISÃO — Classificado por X".
- [ ] `POST /api/tiles/{id}/review` valida `assigned_to == user`, salva, `status=reviewed`.
- [ ] Modal de reportar problema (textarea + confirmar + cancelar); `POST /api/tiles/{id}/report-problem` salva nota, limpa `data_png` para 255, `status=problem`.
- [ ] Log de todas as ações em `action_log`.

**Critério:** ciclo completo classificar → revisar → aprovar funciona; reportar problema descarta classificação parcial.

---

## Fase 6 — Painel Admin

- [ ] `GET /api/admin/dashboard`: contagens por status, percentual, série diária (group by date), estatísticas por operador (contagens + tempo médio via diffs de `action_log`).
- [ ] `GET /api/admin/tiles` com filtros (status, user, range de data) + paginação.
- [ ] `GET /api/admin/tiles/problems`.
- [ ] `POST /api/admin/tiles/{id}/reset` (limpa máscara, status=pending, log).
- [ ] `POST /api/admin/tiles/{id}/re-review` (reviewed → in_review).
- [ ] `POST /api/admin/users` (criar), `GET /api/admin/users` (listar com stats).
- [ ] UI: dashboard (Chart.js ou canvas simples), lista/grade de tiles com thumbnails, ações em lote (checkboxes), gestão de usuários.

**Critério:** admin consegue monitorar progresso, resetar tiles problemáticos, criar operadores.

---

## Fase 7 — Exportação

- [ ] `scripts/export_tiles.py`: SELECT dos `reviewed`, gerar GeoTIFF banda única georreferenciado a partir do bbox (usar `rasterio` — adicionar dependência).
- [ ] Flag opcional `--mosaic` para merge.

**Critério:** GeoTIFF abre no QGIS com georreferência correta.

---

## Fase 8 — Polimento

- [ ] Overlay de atalhos (tecla `?`).
- [ ] Feedback visual (flash verde ao submeter, flash na borda ao trocar classe).
- [ ] Tooltip com nome da classe ao passar mouse.
- [ ] Revisar critérios de aceitação da seção 11 do requirements, fechar pendências.
- [ ] Testes de carga: 10 operadores simulados, latência `/next` < 2s, pintura < 16ms/frame.

---

## Pontos de atenção (do requirements, em ordem de risco)

1. **Mapeamento de coordenadas (10.3)** — testar cantos sempre que o canvas redimensionar.
2. **Concorrência em `/next` (10.1)** — `BEGIN IMMEDIATE` não é opcional.
3. **Performance de pintura (10.2)** — dirty rectangle + incrementar contador de pixels em vez de varrer.
4. **Undo por patches, não snapshots (5.4.3)** — memória estoura rápido com 50 snapshots de 65KB.
5. **Validação dupla no submit (10.4)** — cliente e servidor validam 65536 bytes e valores.
6. **Space hold (5.3)** — UX essencial; não é toggle, é keydown/keyup.

---

## Dependências entre fases

```
0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8
            └────────┘
                 fase 6 pode começar em paralelo após 3
```

A fase 4 é a mais longa; subfases 4.1–4.7 são bloqueantes, 4.8–4.16 podem ser paralelizadas ou adiadas.
