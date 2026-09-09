# Especificação Técnica: Validador de Loterias (Monolito Modular Web)

## 1. Visão Geral do Projeto

Aplicação web local em Python (monolito modular) projetada para automatizar a leitura de bilhetes físicos de loteria (**Mega-Sena** e **Quina**) através de imagens, armazenar o histórico e os jogos em banco de dados **PostgreSQL**, buscar os resultados oficiais dos concursos e realizar a apuração automática de acertos e premiações.

---

## 2. Pilha Tecnológica (Tech Stack)

* **Linguagem:** Python 3.10+
* **Framework Web:** FastAPI (com suporte a rotas assíncronas e templates)
* **Template Engine & UI:** Jinja2 + Tailwind CSS (via CDN para estilização rápida e moderna)
* **Banco de Dados:** PostgreSQL
* **ORM / Conexão:** SQLAlchemy ou Psycopg (com padrão Repository/Services)
* **Processamento de Imagem / IA:** NVIDIA NIM Vision API (modelo `meta/llama-3.2-11b-vision-instruct`) via SDK compatível com OpenAI.
* **Coleta de Resultados:** Playwright (para extração no site oficial da Caixa) ou requisições HTTP diretas com fallback.
* **Gerenciamento de Arquivos:** Armazenamento local estruturado na pasta `storage/tickets/`.

---

## 3. Estrutura de Diretórios do Projeto

```text
validador-loterias/
├── main.py                  # Ponto de entrada do FastAPI (Uvicorn app)
├── config.py                # Gerenciamento de variáveis de ambiente (.env)
├── database.py              # Configuração da sessão do PostgreSQL
├── models/                  # Definições das tabelas do banco de dados
│   ├── ticket.py            # Bilhetes cadastrados e imagens
│   ├── game.py              # Jogos individuais (A, B, C...) e dezenas
│   └── draw.py              # Resultados oficiais dos concursos da Caixa
├── services/
│   ├── nim_vision.py        # Integração com a API da NVIDIA NIM para OCR inteligente
│   ├── caixa_scraper.py     # Raspagem/Busca de resultados da Caixa
│   └── validator.py         # Lógica de cruzamento de apostas vs. sorteio
├── static/                  # Arquivos estáticos (se necessário)
├── templates/               # Arquivos HTML (Jinja2 + Tailwind)
│   ├── base.html            # Layout padrão com barra de navegação e Tailwind
│   ├── index.html           # Tela principal com o formulário de upload
│   └── historico.html       # Tela de listagem de bilhetes, jogos e apuração
├── storage/                 # Diretório local para salvar as fotos enviadas
└── requirements.txt         # Lista de dependências do Python
```

---

## 4. Requisitos de Banco de Dados (PostgreSQL)

### Tabela: `tickets`

* `id`: SERIAL (PK)
* `filename`: VARCHAR (Nome único do arquivo salvo em `storage/`)
* `lottery_type`: VARCHAR (Ex: "Mega-Sena" ou "Quina")
* `draw_number`: INTEGER (Número do concurso extraído do bilhete)
* `created_at`: TIMESTAMP (Data/hora do upload)

### Tabela: `games`

* `id`: SERIAL (PK)
* `ticket_id`: INTEGER (FK -> `tickets.id`)
* `game_identifier`: VARCHAR (Identificador da aposta no bilhete, ex: "A", "B", "C"...)
* `numbers`: JSON ou ARRAY (Lista de inteiros jogados, ex: `[4, 10, 24, 36, 40, 54]`)

### Tabela: `draws`

* `id`: SERIAL (PK)
* `lottery_type`: VARCHAR ("Mega-Sena" ou "Quina")
* `draw_number`: INTEGER (Unique)
* `drawn_numbers`: JSON ou ARRAY (Lista de inteiros sorteados pela Caixa)
* `fetched_at`: TIMESTAMP

---

## 5. Requisitos Funcionais por Módulo

### 5.1. Módulo de Leitura (`services/nim_vision.py`)

* Recebe o caminho da imagem salva.
* Converte a imagem para Base64 e envia para o endpoint da NVIDIA NIM (`https://integrate.api.nvidia.com/v1`) usando o modelo multimodal (`meta/llama-3.2-11b-vision-instruct`).
* **Prompt estruturado para a IA:** Solicitar a extração estrita em formato JSON contendo:
  * `lottery_type` ("Mega-Sena" ou "Quina")
  * `draw_number` (inteiro)
  * `games` (lista de objetos com `identifier` e uma lista de inteiros `numbers`).

### 5.2. Módulo de Resultados (`services/caixa_scraper.py`)

* Consulta o resultado oficial do concurso correspondente cadastrado no bilhete.
* Utiliza o Playwright para buscar e extrair as dezenas sorteadas do portal oficial da Caixa, garantindo independência de APIs de terceiros.
* Salva o resultado na tabela `draws` para cache e consultas futuras.

### 5.3. Módulo de Validação (`services/validator.py`)

* Compara as dezenas de cada jogo (`games.numbers`) com as dezenas sorteadas (`draws.drawn_numbers`).
* Calcula a quantidade de acertos e define o status da premiação:
  * **Mega-Sena:** Sena (6), Quina (5), Quadra (4), ou Sem Premiação.
  * **Quina:** Quina (5), Quadra (4), Terno (3), Duque (2), ou Sem Premiação.

### 5.4. Interface Web (FastAPI + Jinja2)

* **Rota `GET /` (Index):** Tela limpa com um formulário de upload de imagem (suporte a arquivos PNG/JPEG) e botão de envio.
* **Rota `POST /upload`:** Salva o arquivo em `storage/`, dispara o serviço da NVIDIA NIM, salva no PostgreSQL, busca o resultado oficial da Caixa, roda a apuração e redireciona para a página de histórico ou detalhes.
* **Rota `GET /historico`:** Exibe uma tabela consolidada com todos os bilhetes enviados, miniaturas/links das imagens, os números jogados, o resultado oficial do concurso e o status dos acertos destacados visualmente.

---

## 6. Dependências Iniciais (`requirements.txt`)

```text
fastapi>=0.110.0
uvicorn>=0.28.0
sqlalchemy>=2.0.0
psycopg2-binary>=2.9.9
jinja2>=3.1.3
python-multipart>=0.0.9
openai>=1.12.0
playwright>=1.42.0
pydantic-settings>=2.2.0
```
