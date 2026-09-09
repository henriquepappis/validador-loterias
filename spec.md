# Especificação Técnica: Validador de Loterias (Monolito Modular Web)

## 1. Visão Geral do Projeto

Aplicação web local em Python (monolito modular) que automatiza a leitura de
**comprovantes físicos** de loteria (**Mega-Sena** e **Quina**) a partir de
fotos, com **conferência humana** das dezenas antes da apuração. Armazena o
histórico em **PostgreSQL**, busca os resultados oficiais dos concursos e
realiza a apuração automática de acertos e premiações.

Diferenças em relação ao rascunho inicial (implementadas):

* **Um upload aceita várias imagens**, e **cada imagem pode conter vários
  comprovantes** (caso comum: foto de 6 tiras juntas).
* Entre a leitura por IA e a gravação existe uma **tela de revisão/edição**
  (`/revisar/{batch_id}`): o usuário corrige modalidade, concurso e dezenas,
  adiciona comprovantes/apostas que a IA não pegou, e só então confirma.
* A leitura da IA passa por **reparo de JSON** e **validação de faixa/quantidade
  de dezenas** por modalidade; inconsistências viram avisos e bloqueiam a
  apuração até serem resolvidas.

---

## 2. Pilha Tecnológica (Tech Stack)

* **Linguagem:** Python 3.10+
* **Framework Web:** FastAPI (rotas assíncronas + templates)
* **Template Engine & UI:** Jinja2 + Tailwind CSS (via CDN)
* **Banco de Dados:** PostgreSQL (docker-compose para desenvolvimento)
* **ORM:** SQLAlchemy 2.0 (mapeamento declarativo tipado) + padrão de serviços
* **Processamento de Imagem / IA:** NVIDIA NIM Vision API
  (`meta/llama-3.2-11b-vision-instruct`) via SDK compatível com OpenAI.
* **Coleta de Resultados:** API pública do Portal de Loterias da Caixa
  (`servicebus2.caixa.gov.br`) como fonte primária, com **fallback em
  Playwright** (import preguiçoso; browsers opcionais).
* **Gerenciamento de Arquivos:** armazenamento local em `storage/tickets/`.

---

## 3. Estrutura de Diretórios do Projeto

```text
validador-loterias/
├── main.py                  # App FastAPI: rotas / , /upload , /revisar , /historico
├── config.py                # Variáveis de ambiente (pydantic-settings, .env)
├── database.py              # Engine/sessão SQLAlchemy + init_db()
├── models/
│   ├── batch.py             # Lote de upload (pending | confirmed)
│   ├── image.py             # Imagem enviada (pertence a um batch)
│   ├── ticket.py            # Comprovante lido de uma imagem (pending | confirmed)
│   ├── game.py              # Apostas individuais (A, B, C...) e dezenas
│   └── draw.py              # Resultados oficiais dos concursos da Caixa
├── services/
│   ├── lottery.py           # Regras das modalidades: normalização e validação
│   ├── nim_vision.py        # OCR via NVIDIA NIM (extrai N comprovantes por imagem)
│   ├── caixa_scraper.py     # Resultado oficial (API da Caixa + fallback Playwright, com cache)
│   └── validator.py         # Cruzamento aposta × sorteio e faixas de premiação
├── templates/
│   ├── base.html            # Layout + navbar + Tailwind
│   ├── index.html           # Upload de 1+ imagens
│   ├── revisar.html         # Revisão/edição das leituras antes de apurar
│   └── historico.html       # Lotes confirmados (agrupados por imagem) + pendências
├── static/
├── storage/tickets/         # Imagens enviadas
├── docker-compose.yml       # PostgreSQL 16 para desenvolvimento
├── .env.example
└── requirements.txt
```

---

## 4. Requisitos de Banco de Dados (PostgreSQL)

O schema é criado por `init_db()` (`Base.metadata.create_all`) no startup. Não há
migrations; em desenvolvimento, mudanças de schema exigem `drop_all` + `create_all`.

### Tabela: `batches` (lote de upload)

| Coluna       | Tipo                    | Observações                          |
|--------------|-------------------------|-------------------------------------|
| `id`         | SERIAL (PK)             |                                     |
| `status`     | VARCHAR                 | `pending` \| `confirmed`             |
| `created_at` | TIMESTAMP               |                                     |

### Tabela: `images`

| Coluna       | Tipo                    | Observações                          |
|--------------|-------------------------|-------------------------------------|
| `id`         | SERIAL (PK)             |                                     |
| `batch_id`   | INTEGER (FK → batches)  | `ON DELETE CASCADE`                  |
| `filename`   | VARCHAR (unique)        | Arquivo salvo em `storage/tickets/` |
| `created_at` | TIMESTAMP               |                                     |

### Tabela: `tickets` (um comprovante)

| Coluna         | Tipo                   | Observações                                   |
|----------------|------------------------|----------------------------------------------|
| `id`           | SERIAL (PK)            |                                              |
| `image_id`     | INTEGER (FK → images)  | `ON DELETE CASCADE`                           |
| `lottery_type` | VARCHAR NULL           | "Mega-Sena" \| "Quina"; nulo enquanto pendente |
| `draw_number`  | INTEGER NULL           | Concurso; nulo enquanto pendente             |
| `status`       | VARCHAR                | `pending` \| `confirmed`                      |
| `notes`        | VARCHAR NULL           | Avisos de leitura/validação para a revisão   |
| `created_at`   | TIMESTAMP              |                                              |

### Tabela: `games` (uma aposta do comprovante)

| Coluna            | Tipo                    | Observações                              |
|-------------------|-------------------------|----------------------------------------|
| `id`              | SERIAL (PK)             |                                        |
| `ticket_id`       | INTEGER (FK → tickets)  | `ON DELETE CASCADE`                     |
| `game_identifier` | VARCHAR                 | "A", "B", "C"...                        |
| `numbers`         | JSON                    | Lista de inteiros, ex: `[4,10,24,36,40,54]` |

### Tabela: `draws` (resultado oficial, cache)

| Coluna          | Tipo                   | Observações                             |
|-----------------|------------------------|---------------------------------------|
| `id`            | SERIAL (PK)            |                                       |
| `lottery_type`  | VARCHAR                | "Mega-Sena" \| "Quina"                 |
| `draw_number`   | INTEGER                | Único junto com `lottery_type`         |
| `drawn_numbers` | JSON                   | Dezenas sorteadas (ordenadas)          |
| `fetched_at`    | TIMESTAMP              |                                       |

Restrição: `UNIQUE (lottery_type, draw_number)`.

---

## 5. Requisitos Funcionais por Módulo

### 5.1. Regras das Modalidades (`services/lottery.py`)

* `LOTTERY_RULES`: por modalidade, faixa válida das dezenas e quantidade mínima/
  máxima por aposta.
  * **Mega-Sena:** dezenas 1–60; 6 a 20 dezenas por aposta.
  * **Quina:** dezenas 1–80; 5 a 15 dezenas por aposta.
* `normalize_lottery_type(texto)` → `"Mega-Sena"`, `"Quina"` ou `None`.
* `clean_numbers(bruto)` → lista de inteiros, tolerando `"04"`, `"5a"`, strings
  separadas por qualquer caractere; remove duplicatas preservando ordem.
* `validate_numbers(numeros, modalidade)` → lista de avisos (vazia = consistente).

### 5.2. Módulo de Leitura (`services/nim_vision.py`)

* `extract_tickets(image_path)` recebe o caminho da imagem, converte para
  Base64 e envia ao endpoint da NVIDIA NIM
  (`https://integrate.api.nvidia.com/v1`) com o modelo multimodal.
* **Prompt estruturado:** a imagem pode conter vários comprovantes; a IA deve
  retornar JSON estrito:

  ```json
  {
    "tickets": [
      {
        "lottery_type": "Mega-Sena" | "Quina",
        "draw_number": 3056,
        "games": [{ "identifier": "A", "numbers": [4, 10, 24, 36, 40, 54] }]
      }
    ]
  }
  ```

* **Robustez:** `_repair_json` conserta zero à esquerda (`01` → `1`), sufixos
  alfabéticos (`5a` → `5`) e vírgula sobrando antes de fechar. Cada comprovante
  e cada aposta recebem `warnings` de `validate_numbers`.
* Retorno: lista de dicts `{lottery_type|None, draw_number|None, games[], warnings[]}`.

### 5.3. Módulo de Resultados (`services/caixa_scraper.py`)

* `fetch_draw(lottery_type, draw_number, db)`:
  1. Consulta o cache na tabela `draws`; se existir, retorna.
  2. **Fonte primária:** `GET servicebus2.caixa.gov.br/portaldeloterias/api/
     {megasena|quina}/{concurso}` (headers de browser, `verify=False` por conta
     da cadeia de certificados da Caixa).
  3. **Fallback:** Playwright (Chromium headless) atingindo a mesma API por outra
     rota de rede; import preguiçoso — ausência dos browsers não quebra a app.
  4. Persiste em `draws` e retorna.
* `DrawNotFoundError` quando nenhuma fonte responde; o chamador trata como aviso
  (não bloqueia o cadastro).

### 5.4. Módulo de Validação (`services/validator.py`)

* `evaluate(numbers, drawn_numbers, lottery_type)` → `{hits, hit_count, prize,
  is_winner}`.
* Faixas de premiação:
  * **Mega-Sena:** 6 = Sena, 5 = Quina, 4 = Quadra; senão "Sem Premiação".
  * **Quina:** 5 = Quina, 4 = Quadra, 3 = Terno, 2 = Duque; senão "Sem Premiação".

### 5.5. Interface Web (FastAPI + Jinja2)

* **`GET /`** — formulário de upload com `input[type=file] multiple` (PNG/JPEG).
* **`POST /upload`** — para cada imagem: salva em `storage/tickets/` com nome
  único, roda `nim_vision.extract_tickets`, cria `Image` + `Ticket`s/`Game`s com
  `status = pending`. Falha de OCR numa imagem não aborta o lote (gera
  comprovante em branco com `notes`). Redireciona para `/revisar/{batch_id}`.
* **`GET /revisar/{batch_id}`** — formulário editável por comprovante
  (modalidade, concurso, apostas), com os avisos da leitura. Botões para
  adicionar comprovantes e apostas (clonagem via `<template>` + JS mínimo).
* **`POST /revisar/{batch_id}`** — parseia o formulário, valida
  (`validate_numbers`), grava. Comprovantes válidos viram `confirmed`;
  inválidos permanecem `pending` e a página é re-renderizada com os erros (HTTP
  400) sem finalizar. Sem erros: busca o resultado de cada concurso distinto
  (`caixa_scraper.fetch_draw`), marca o `batch` como `confirmed` e redireciona
  para `/historico#batch-{id}`.
* **`GET /historico`** — lotes confirmados agrupados por imagem, com miniatura/
  link da foto, dezenas jogadas (acertos destacados), sorteio oficial e faixa
  de premiação. Seção no topo lista **revisões pendentes** (link para `/revisar`).

---

## 6. Dependências (`requirements.txt`)

```text
fastapi>=0.110.0
uvicorn[standard]>=0.28.0
sqlalchemy>=2.0.0
psycopg2-binary>=2.9.9
jinja2>=3.1.3
python-multipart>=0.0.9
openai>=1.12.0
playwright>=1.42.0
pydantic-settings>=2.2.0
httpx>=0.27.0
```

Setup resumido:

```bash
docker compose up -d
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # opcional (fallback do scraper)
cp .env.example .env                  # preencher NIM_API_KEY
uvicorn main:app --reload
```
