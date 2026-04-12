# TileClass

Aplicação web para classificação pixel-a-pixel de tiles de satélite (256×256, 6 classes).
Backend FastAPI + SQLite; frontend Vanilla JS com Canvas HTML5. Imagem de fundo via TileServer-GL (XYZ).

Spec completa: `docs/requirements.md`. Em caso de dúvida, o requirements manda.

## Stack

- **Backend:** Python 3.11+, FastAPI, Uvicorn, SQLite (sqlite3 nativo — **sem ORM**), PyJWT, bcrypt, Pillow, NumPy, PyYAML, rasterio (export)
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
python -m backend.scripts.import_tiles <csv>
python -m backend.scripts.export_tiles <out_dir> [--mosaic]

# Testes
python -m pytest tests/ --ignore=tests/e2e --ignore=tests/frontend -v   # backend
npx vitest run                                                           # frontend unit
node tests/e2e/runner.mjs                                                # E2E (sobe uvicorn + Puppeteer)
npm run test:all                                                         # tudo em sequência
```

## Estrutura

```
tileclass/
├── backend/
│   ├── main.py              # FastAPI app + rotas + static files
│   ├── auth.py              # JWT, bcrypt, rate limit, role middleware
│   ├── models.py            # Schemas Pydantic
│   ├── database.py          # Conexão SQLite (WAL), schema, log_action
│   ├── config.py            # Carrega config.yaml (singleton cache)
│   ├── tile_service.py      # Fila, atribuição atômica, submit, problema, histórico
│   ├── admin_service.py     # Dashboard, bulk reset/re-review, users, thumbnail
│   ├── mask_utils.py        # Uint8Array ↔ PNG "L" + validação
│   ├── config.yaml
│   └── scripts/             # create_admin / import_tiles / export_tiles
├── frontend/
│   ├── index.html           # SPA (login / editor / admin)
│   ├── js/mask-core.js      # Lógica PURA de máscara (paint, bresenham, flood, undo) — testável sem DOM
│   ├── css/style.css
│   └── js/
│       ├── app.js           # Router SPA por role
│       ├── api.js           # Fetch wrapper + JWT refresh (reativo + proativo)
│       ├── auth.js          # (reservado; auth está em api.js + app.js)
│       ├── editor.js        # Canvas, ferramentas, undo/redo, submit
│       ├── admin.js         # Dashboard, tiles (lista+grade), users, viewer
│       ├── minimap.js       # MapLibre 3×3 com highlight do tile atual
│       ├── maplib.js        # Helpers MapLibre (createLockedMap, setMapBbox)
│       ├── utils.js         # hexToRgb, blobToImage, escapeHtml
│       └── toast.js         # Notificações
├── docs/
│   ├── requirements.md          # Spec completa
│   └── implementation_plan.md   # Plano em fases
├── tests/                   # pytest + TestClient (auth, tiles, admin, concorrência)
└── requirements.txt
```

## Regras de idioma

- **UI (labels, mensagens, tooltips):** Português pt-BR com acentuação correta.
- **Comentários, docstrings, nomes de variável/função:** Inglês.
- **Campos de banco e API JSON:** Inglês (`status`, `assigned_to`, `classified_at`).

## Invariantes do domínio

- **Fonte de verdade da máscara:** `Uint8Array(65536)` no cliente. Valores válidos: `1..6` (classes) e `255` (não preenchido). O canvas é apenas visualização.
- **PNG do backend:** banda única (grayscale "L"), 8 bits, 256×256, sem compressão com perda. Pillow faz a conversão `bytes ↔ PNG`.
- **Protocolo wire:** frontend envia **raw bytes** (Uint8Array, 65536 bytes) no body do classify/review; nunca PNG. Backend converte.
- **Submissão:** rejeitar se houver `255` no array. Resposta de erro traz a contagem.
- **Máquina de estados de tile:** `pending → in_progress → classified → in_review → reviewed`; qualquer estado `→ problem`; `problem → pending` e `reviewed → in_review` são transições de admin.

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

## UX — inegociáveis

- Login → pintar em < 5s. Evitar modais/confirmações desnecessárias.
- **Pré-carregar próximo tile:** `/api/tiles/next-preview` (peek sem atribuir) enquanto operador pinta o atual. Cache em `preloadedNext` é invalidado se o tile realmente atribuído em `/next` for diferente.
- Após submeter: próximo tile carrega automaticamente (sem clique extra); flash verde 200ms antes.
- Pixels faltantes ao submeter: pisca vermelho 2s sobre o canvas.
- Backup do `Uint8Array` em `localStorage` durante edição; restaurar em caso de F5 (confirmação obrigatória antes de sobrescrever estado do servidor).
- **JWT refresh proativo:** `api.js` decodifica o `exp` do token e agenda refresh 60s antes de expirar. Além disso, re-tenta automaticamente no 401 (reativo). O usuário não deve ver logout por expiração durante uso contínuo.

## Segurança

- Senhas: bcrypt cost ≥ 12.
- Validar no backend: 65536 bytes exatos, valores em `{1..6, 255}`, submetedor == `assigned_to`.
- Endpoints admin atrás de `Depends(auth.require_admin)` que verifica `role == 'admin'`.
- Nunca `innerHTML` com dado de usuário — `textContent` ou `escapeHtml` de `utils.js`.

## Testes

Três camadas. Todas devem passar antes de commitar:

### Backend (`tests/*.py`, pytest + TestClient) — 138 testes
- Fixture `app_env` em `conftest.py` monkey-patcha o DB para arquivo temporário. Precisa patchar `get_config` em **cada módulo** que fez `from .config import get_config` (referência é copiada no import).
- Fixtures: `admin_user`, `operators` (3), `operators_10`, `tiles` (10 pending), `tiles_many` (100).
- Rate limit entre logins: `auth.reset_rate_limits()` antes de cada login quando >5 no mesmo teste.
- Concorrência crítica: `test_concurrent_10_operators_no_duplicates` (10 threads pending), `test_10_reviewers_race_on_classified_queue` (9 reviewers simultâneos na fila de revisão).
- Invariantes de domínio com teste próprio: `test_auth_invariants` (bcrypt cost≥12, JWT TTL 8h/24h, forgery, typ access↔refresh, escalação via claim), `test_authz_crossuser` (bypass de `/next`, POST cross-user, gate admin), `test_state_machine` (todas transições + ilegais), `test_rate_limit_real` (sem reset), `test_dashboard_real` (atua antes de conferir — não-tautológico), `test_mask_roundtrip_strong` (padrões não-uniformes).

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
