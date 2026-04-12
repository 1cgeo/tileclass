# TileClass

Aplicação web para classificação pixel-a-pixel de tiles de satélite (256×256, 6 classes).
Backend FastAPI + SQLite; frontend Vanilla JS com Canvas HTML5. Imagem de fundo via TileServer-GL (XYZ).

Spec completa: `docs/requirements.md`. Em caso de dúvida, o requirements manda.

## Stack

- **Backend:** Python 3.11+, FastAPI, Uvicorn, SQLite (sqlite3 nativo — **sem ORM**), PyJWT, bcrypt, Pillow, NumPy, PyYAML, rasterio (export)
- **Frontend:** Vanilla JS (sem framework), Canvas HTML5, fetch API. MapLibre GL JS (via CDN) para renderização georreferenciada dos tiles XYZ. Servido como estático pelo FastAPI.
- **Testes:** pytest + httpx (via FastAPI TestClient)
- **Config:** `backend/config.yaml` (classes, TileServer URL, JWT secret, DB path)

## Comandos

```bash
# Backend
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

# Scripts CLI
python backend/scripts/create_admin.py
python backend/scripts/import_tiles.py <csv>
python backend/scripts/export_tiles.py <out_dir>
```

## Estrutura

```
tileclass/
├── backend/
│   ├── main.py              # FastAPI app + rotas + static files do frontend
│   ├── auth.py              # JWT, bcrypt, middleware role-check
│   ├── models.py            # Schemas Pydantic (request/response)
│   ├── database.py          # Conexão SQLite (WAL), init de tabelas
│   ├── tile_service.py      # Fila, atribuição atômica, salvamento PNG
│   ├── admin_service.py     # Dashboard, estatísticas, gestão
│   ├── config.yaml
│   └── scripts/             # import_tiles / create_admin / export_tiles
├── frontend/
│   ├── index.html
│   ├── css/style.css
│   └── js/{app,auth,api,editor,minimap,admin}.js
├── docs/requirements.md
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

- **Sem ORM.** `sqlite3` puro com queries SQL parametrizadas (`?`). Ativar `PRAGMA journal_mode=WAL`.
- **`/api/tiles/next` é transacional:** `BEGIN IMMEDIATE`, `SELECT ... LIMIT 1`, `UPDATE status, assigned_to`, `COMMIT`. Dois operadores nunca recebem o mesmo tile.
- **Prioridade:** `in_review` não atribuído > `pending`. Revisor nunca revisa tile que ele classificou (`classified_by != current_user`).
- **Autorização em submit:** `assigned_to == current_user` é pré-condição para `classify`/`review`.
- **Rate limit:** `/api/auth/login` — 5/min por IP.
- **bcrypt:** cost ≥ 12. **JWT:** HS256, access 8h, refresh 24h.

## Regras críticas de frontend

- **Três camadas sobrepostas no editor:** (1) **MapLibre GL** renderiza o satélite XYZ como `div` georreferenciado ao fundo (interação desabilitada, bbox fitada via `fitBounds`), (2) canvas da máscara com `globalAlpha` controlável, (3) canvas de cursor/interação transparente no topo. Eventos de mouse vão na camada 3 (`pointer-events: auto`); as demais recebem `pointer-events: none` exceto o map, que está com `interactive: false`.
- **MapLibre resolve composição de tiles:** quando a bbox de um tile não alinha perfeitamente com um XYZ, MapLibre carrega múltiplos tiles e recorta pela bbox automaticamente. Não compor manualmente.
- **Mapeamento de coordenadas (ponto mais frágil):** CSS pixels → canvas pixels (considerando `devicePixelRatio`) → coordenada lógica 0-255 via `Math.floor`. Testar que o canto inferior-direito bate em `(255, 255)`, não `(256, 256)` ou `(254, 254)`.
- **Dirty rectangle:** redesenhar apenas a região afetada pelo brush a cada evento. Não redesenhar o canvas inteiro.
- **Undo/redo:** cada ciclo mousedown→mouseup é uma entrada. Armazenar **apenas os pixels alterados** (posição + valor anterior), nunca o array inteiro. Mínimo 50 níveis.
- **Interpolação no drag:** interpolar entre eventos `mousemove` para não deixar buracos em movimentos rápidos.
- **`Space` segurado = esconde máscara** (mecanismo mais importante de produtividade). Atalhos desabilitados quando modal/input está ativo.
- **Paleta de classes:** contrastante entre si e visível sobre imagens de satélite. Evitar verde e tons escuros.

## UX — inegociáveis

- Login → pintar em < 5s. Evitar modais/confirmações desnecessárias.
- Pré-carregar o próximo tile enquanto o operador pinta o atual.
- Após submeter: próximo tile carrega automaticamente (sem clique extra).
- Backup do `Uint8Array` em `localStorage` durante edição; restaurar em caso de F5.

## Segurança

- Senhas: bcrypt cost ≥ 12.
- Validar no backend: 65536 bytes exatos, valores em `{1..6, 255}`, submetedor == `assigned_to`.
- Endpoints admin atrás de middleware que verifica `role == 'admin'`.
- Nunca `innerHTML` com dado de usuário no frontend; usar `textContent`.

## Git

**Nunca commitar sem o usuário pedir.** Revisão manual de todas as mudanças.
