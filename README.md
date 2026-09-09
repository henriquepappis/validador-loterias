# Validador de Loterias

Aplicação web local (FastAPI + Jinja2 + Tailwind) que lê bilhetes físicos da
**Mega-Sena** e da **Quina** a partir de fotos, guarda os jogos em PostgreSQL,
busca o resultado oficial na Caixa e apura os acertos automaticamente.

A especificação completa está em [`spec.md`](spec.md).

## Requisitos

- Python 3.10+
- Docker (para o PostgreSQL de desenvolvimento)
- Chave da NVIDIA NIM (modelo `meta/llama-3.2-11b-vision-instruct`)

## Setup

```bash
# 1. Banco de dados
docker compose up -d

# 2. Ambiente Python
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # opcional: fallback do scraper da Caixa

# 3. Configuração
cp .env.example .env
# edite .env e preencha NIM_API_KEY

# 4. Subir a aplicação
uvicorn main:app --reload
```

Acesse http://localhost:8000

## Estrutura

| Caminho | Responsabilidade |
| --- | --- |
| `main.py` | App FastAPI, rotas `GET /`, `POST /upload`, `GET /historico` |
| `config.py` | Variáveis de ambiente (`pydantic-settings`) |
| `database.py` | Engine/sessão SQLAlchemy e `init_db()` |
| `models/` | Tabelas `tickets`, `games`, `draws` |
| `services/nim_vision.py` | OCR do bilhete via NVIDIA NIM |
| `services/caixa_scraper.py` | Resultado oficial (API da Caixa + fallback Playwright), com cache |
| `services/validator.py` | Cruzamento aposta × sorteio e faixas de premiação |
| `templates/` | `base.html`, `index.html`, `historico.html` |
| `storage/tickets/` | Imagens dos bilhetes enviados |

## Fluxo do `POST /upload`

1. Salva a imagem em `storage/tickets/` com nome único.
2. `nim_vision.extract_ticket()` devolve `lottery_type`, `draw_number` e `games`.
3. Persiste `Ticket` + `Game`s.
4. `caixa_scraper.fetch_draw()` busca (ou reaproveita do cache) o sorteio.
5. Redireciona para `/historico`, onde `validator.evaluate()` calcula os acertos.

## Tabelas de premiação

- **Mega-Sena:** 6 = Sena, 5 = Quina, 4 = Quadra
- **Quina:** 5 = Quina, 4 = Quadra, 3 = Terno, 2 = Duque
