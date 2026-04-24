# Roadmap — Hora Extra Job Scraper

## Visão Geral do Sistema

Pipeline de coleta e análise de vagas de emprego com extração de múltiplos ATSs,
filtragem por área, análise de compatibilidade com currículo via IA e exportação.

### Estado Atual

```
domain.yaml
    │
    ▼
scraper.py          ← coleta sequencial (1 empresa de cada vez)
    │  Salva .md em output/
    ▼
filter_tech.py      ← lê output/, copia vagas TI para vagas-tech/
    │  Exporta vagas.csv
    ▼
organizar.py        ← lê vagas.csv, gera vagas_organizado.csv + .xlsx
    │
    ▼
analyzer.py         ← lê output/, pontua vagas vs. currículo PDF
```

**Problemas do estado atual:**
- Scraping sequencial: ~150 empresas × 0.5 s/vaga = dezenas de minutos
- Zero persistência entre execuções (tudo re-extrai do zero)
- Sem controle de duplicatas entre runs
- Sem proxy (risco de bloqueio em escala)
- Cada etapa é executada manualmente e em processos separados

---

## Arquitetura Proposta

```
                      ┌─────────────────────────────────┐
                      │         domain.yaml              │
                      └────────────┬────────────────────┘
                                   │ carrega domínios
                                   ▼
                      ┌─────────────────────────────────┐
                      │      Scrape Dispatcher          │
                      │  ThreadPoolExecutor / asyncio   │
                      │  max_workers = N (configurável) │
                      └────────────┬────────────────────┘
                     ┌─────────────┼──────────────┐
                     ▼             ▼              ▼
               Worker 1      Worker 2       Worker N
             (rippling)    (ashbyhq)      (inhire ...)
                     └─────────────┼──────────────┘
                                   │ Job raw
                                   ▼
                      ┌─────────────────────────────────┐
                      │       Fila de Ingestão          │
                      │  queue.Queue (in-process) ou    │
                      │  Redis + RQ (multi-processo)    │
                      └────────────┬────────────────────┘
                                   │
              ┌────────────────────┼──────────────────────┐
              ▼                    ▼                       ▼
    ┌─────────────────┐  ┌─────────────────┐   ┌─────────────────┐
    │  Worker Filter  │  │  Worker Analyze │   │  Worker Export  │
    │  filter_tech.py │  │  analyzer.py    │   │  organizar.py   │
    └────────┬────────┘  └────────┬────────┘   └────────┬────────┘
             │                    │                      │
             └────────────────────┼──────────────────────┘
                                   │
                                   ▼
                      ┌─────────────────────────────────┐
                      │          Banco de Dados         │
                      │   SQLite (local) / PostgreSQL   │
                      │   Tabelas: jobs, runs, scores   │
                      └─────────────────────────────────┘
```

---

## Fases de Implementação

### Fase 1 — Paralelismo no Scraping

**Objetivo:** reduzir o tempo de extração de N×sequencial para ~N/workers.

#### 1.1 Paralelismo por empresa (ThreadPoolExecutor)

Cada empresa é um unit of work independente. O dispatcher despacha todas
as empresas para um pool de threads, respeitando o rate limit por domínio.

```python
# scraper.py — ponto de entrada paralelo
from concurrent.futures import ThreadPoolExecutor, as_completed

def scrape_all_parallel(
    domains: list[dict],
    fetch_description: bool = True,
    max_workers: int = 8,
    proxy_pool: "ProxyPool | None" = None,
) -> list[Job]:
    results: list[Job] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_scrape_domain, domain, fetch_description, proxy_pool): domain
            for domain in domains
        }
        for future in as_completed(futures):
            domain = futures[future]
            try:
                jobs = future.result()
                results.extend(jobs)
            except Exception as e:
                print(f"[ERROR] {domain['name']}: {e}")
    return results
```

**Regras de negócio:**
- `max_workers` padrão = 8; ajustável via CLI (`--workers N`)
- Cada worker tem sua própria `requests.Session` — sem compartilhamento de estado HTTP
- Rate limit por domínio (não por worker): semáforo com `threading.Semaphore` ou `asyncio.Semaphore`
- Timeout por worker = 60 s; se exceder, loga e continua (não bloqueia o pool)
- Domínios marcados com `rate_limit: strict` no `domain.yaml` são executados com delay mínimo de 2 s entre requests

#### 1.2 Paralelismo de descrições (por vaga dentro do worker)

Cada scraper pode paralelizar também a extração de descrições individuais:

```python
# dentro de scrape_ashbyhq, scrape_lever etc.
with ThreadPoolExecutor(max_workers=4) as desc_pool:
    futures = {desc_pool.submit(_fetch_description, url, session): job
               for job, url in pending}
    for future in as_completed(futures):
        job = futures[future]
        job.description = future.result()
```

**Regras:**
- Máximo 4 threads de descrição por empresa (não sobrecarregar um único ATS)
- Sleep mínimo de 200 ms entre requests ao mesmo host (via lock por hostname)

#### 1.3 Novos campos em `domain.yaml`

```yaml
- name: Exemplo
  url: https://...
  type: ashbyhq
  job_area: tech          # já existe
  rate_limit: strict      # novo: aplica delay extra
  max_desc_workers: 2     # novo: limita threads de descrição
  enabled: true           # novo: permite desativar sem remover
```

---

### Fase 2 — Sistema de Filas

**Objetivo:** desacoplar coleta → filtragem → análise → exportação em estágios
que podem ser executados em paralelo, retomados após falha e monitorados.

#### 2.1 Modo In-Process (padrão, sem dependências externas)

Usa `queue.Queue` da stdlib. Todos os stages rodam em threads no mesmo processo.

```
Scrape Thread(s)
      │ put(Job)
      ▼
  job_queue (Queue)
      │ get(Job)
      ▼
Filter Thread(s)          → descarta não-TI ou popula campo `is_tech`
      │ put(Job) se aprovado
      ▼
  filtered_queue (Queue)
      │ get(Job)
      ▼
DB Writer Thread          → persiste no banco
      │
      ├── put(Job) em analyze_queue (se currículo fornecido)
      ▼
Analyze Thread(s)         → pontua vs. currículo
      │ put(ScoredJob)
      ▼
Export Thread             → gera CSV / xlsx
```

**Regras de negócio:**
- Cada stage tem um `sentinel = None` para shutdown gracioso
- Backpressure: `job_queue` com `maxsize=200`; scraper bloqueia se fila cheia
- Erro em um job não interrompe o pipeline; job é marcado como `error` no banco
- `analyze_queue` só é criada se `--resume` for passado no CLI

#### 2.2 Modo Distribuído (opcional — Redis + RQ)

Para execução em múltiplas máquinas ou agendamento recorrente:

```
python pipeline.py --mode redis --redis-url redis://localhost:6379
```

Workers sobem com `rq worker scrape filter analyze export`.

---

### Fase 3 — Banco de Dados

**Objetivo:** evitar re-extração de vagas já coletadas, rastrear histórico e
viabilizar análise incremental.

#### 3.1 Schema (SQLite por padrão; PostgreSQL via `DATABASE_URL`)

```sql
-- vagas coletadas
CREATE TABLE jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT NOT NULL,          -- id externo (job_id do ATS)
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    location    TEXT,
    department  TEXT,
    url         TEXT,
    description TEXT,
    is_tech     INTEGER,                -- NULL=não classificado, 1=TI, 0=não-TI
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(company, source_id)          -- deduplicação por empresa+id externo
);

-- histórico de execuções
CREATE TABLE runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    finished_at DATETIME,
    domains     INTEGER,                -- quantos domínios processados
    scraped     INTEGER,                -- total de vagas coletadas
    new_jobs    INTEGER,                -- novas vs. já existentes
    status      TEXT DEFAULT 'running' -- running | done | error
);

-- scores de análise (por execução de analyzer)
CREATE TABLE scores (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER REFERENCES jobs(id),
    resume_hash TEXT NOT NULL,          -- sha256 do PDF do currículo
    score       INTEGER,
    matches     TEXT,                   -- JSON array
    gaps        TEXT,                   -- JSON array
    summary     TEXT,
    model       TEXT,
    scored_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(job_id, resume_hash)
);
```

#### 3.2 Comportamento de deduplicação

- Na inserção, usa `INSERT OR IGNORE` (SQLite) / `ON CONFLICT DO NOTHING` (PG)
- Se o título ou descrição mudarem para uma vaga já existente → `UPDATE` com `updated_at`
- `--full-refresh` force-atualiza todas as vagas (ignora cache)
- `--since 7d` recoleta apenas empresas cujo último run tem mais de 7 dias

#### 3.3 Camada de acesso (`db.py`)

```python
class JobRepository:
    def upsert(self, job: Job) -> tuple[int, bool]: ...   # (id, is_new)
    def mark_tech(self, job_id: int, is_tech: bool): ...
    def save_score(self, job_id: int, scored: ScoredJob, resume_hash: str): ...
    def get_unclassified(self) -> list[Job]: ...
    def get_unscored(self, resume_hash: str) -> list[Job]: ...
    def export_csv(self, path: str, tech_only: bool = True): ...
```

---

### Fase 4 — Proxy

**Objetivo:** evitar bloqueios por IP em runs frequentes com muitas empresas.

#### 4.1 Interface `ProxyPool`

```python
@dataclass
class Proxy:
    url: str          # http://user:pass@host:port
    failures: int = 0
    last_used: float = 0.0

class ProxyPool:
    def get(self) -> Proxy | None: ...       # round-robin ou least-recently-used
    def report_failure(self, proxy: Proxy):  # incrementa failures; descarta se > threshold
    def report_success(self, proxy: Proxy):  # reseta failures
```

#### 4.2 Configuração

Via `.env` ou `domain.yaml`:

```env
# .env
PROXY_LIST=http://p1:pass@host1:8080,http://p2:pass@host2:8080
PROXY_MODE=round_robin   # round_robin | random | least_used
PROXY_MAX_FAILURES=3     # descarta proxy após N falhas consecutivas
```

Ou por domínio:

```yaml
- name: binance
  url: https://jobs.lever.co/binance
  type: lever
  use_proxy: true         # este domínio sempre usa proxy
```

#### 4.3 Integração com scrapers

Cada scraper recebe um `proxy_pool` opcional. Se fornecido, a `requests.Session`
é configurada com o proxy antes de cada request:

```python
def _apply_proxy(session: requests.Session, pool: ProxyPool | None):
    if pool:
        proxy = pool.get()
        if proxy:
            session.proxies = {"http": proxy.url, "https": proxy.url}
```

#### 4.4 Regras de proxy

- Proxy nunca é obrigatório; se `PROXY_LIST` estiver vazio, roda sem proxy
- Retry automático com próximo proxy se `HTTPError 403/429` ou `ConnectionError`
- Máximo de 3 retries por vaga antes de marcar como erro
- Workday e Greenhouse têm proteção Cloudflare — recomendado usar proxies residenciais para eles

---

## Pipeline Completo — Fluxo de Negócio

```
┌─────────────────────────────────────────────────────────────────┐
│  ENTRADA                                                        │
│  domain.yaml  +  .env (chaves de IA, proxies, DB URL)          │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  ETAPA 1 — SCRAPING PARALELO                                    │
│                                                                 │
│  1. Carrega domínios do domain.yaml                             │
│  2. Filtra por --domain / --type / --enabled                    │
│  3. Para cada domínio (em paralelo):                            │
│     a. Instancia o scraper correto (rippling, ashbyhq, etc.)    │
│     b. Coleta lista de vagas (títulos, departamentos, URLs)     │
│     c. Aplica filtro job_area se configurado no domínio         │
│     d. Paraleliza extração de descrições (até 4 threads)        │
│     e. Coloca cada Job na job_queue                             │
│  4. Ao finalizar, envia sentinel para a fila                    │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  ETAPA 2 — FILTRAGEM TI (filter_tech.py)                        │
│                                                                 │
│  1. Consome job_queue                                           │
│  2. Para cada Job:                                              │
│     a. Verifica EXCLUSION_PATTERNS → descarta imediatamente     │
│     b. Verifica CERTAIN_IT_PATTERNS → aprova sem IA            │
│     c. Verifica PRODUCT_PATTERNS → aprova sem IA               │
│     d. Demais: acumula em lote (AI_BATCH_SIZE = 15)             │
│        → envia para OpenAI classify_batch()                     │
│        → aprova ou descarta conforme resposta                   │
│  3. Job aprovado: seta is_tech=True, coloca em filtered_queue   │
│  4. Job reprovado: persiste no banco com is_tech=False          │
│                                                                 │
│  Regra: se API da IA falhar → mantém o lote (conservador)       │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  ETAPA 3 — PERSISTÊNCIA (db.py)                                 │
│                                                                 │
│  1. Consome filtered_queue                                      │
│  2. Para cada Job:                                              │
│     a. Tenta INSERT; se UNIQUE conflict → UPDATE campos         │
│     b. Retorna (job_id, is_new)                                 │
│     c. Se is_new=True: incrementa contador do run               │
│  3. Salva .md em output/ (retrocompatibilidade)                 │
│  4. Atualiza run com progresso                                  │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  ETAPA 4 — ANÁLISE DE CURRÍCULO (analyzer.py) [opcional]        │
│                                                                 │
│  Ativa quando --resume <pdf> é fornecido                        │
│                                                                 │
│  1. Calcula sha256 do PDF                                       │
│  2. Consulta banco: quais jobs ainda não foram pontuados        │
│     com este currículo?                                         │
│  3. Para cada job não pontuado:                                 │
│     a. Envia (currículo, job) para o provider de IA escolhido   │
│     b. Recebe {score, matches, gaps, summary}                   │
│     c. Persiste em scores com (job_id, resume_hash)             │
│  4. Ordena por score desc, retorna top N                        │
│                                                                 │
│  Regra: jobs já pontuados (mesmo currículo) são pulados         │
│  Regra: --reanalyze força reanálise mesmo se já pontuado        │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  ETAPA 5 — EXPORTAÇÃO (organizar.py)                            │
│                                                                 │
│  1. Consulta banco: SELECT jobs WHERE is_tech=1                 │
│  2. Gera vagas.csv (título, empresa, link, local)               │
│  3. Gera vagas_organizado.xlsx (ordenado por empresa+título)    │
│  4. Se scores disponíveis: adiciona coluna score ao xlsx        │
└─────────────────────────────────────────────────────────────────┘
```

---

## CLI Unificado (`pipeline.py`)

Substitui a execução manual de cada script. Aceita flags para controlar quais
etapas executar.

```bash
# Execução completa com currículo
python pipeline.py --resume Profile.pdf --workers 10 --provider groq

# Só coleta (sem análise)
python pipeline.py --scrape-only --workers 8

# Só analisa vagas já no banco
python pipeline.py --analyze-only --resume Profile.pdf --top 20

# Coleta uma empresa específica
python pipeline.py --domain Skeelo --resume Profile.pdf

# Com proxy
python pipeline.py --workers 12 --proxy-list proxies.txt

# Re-coleta forçada (ignora cache do banco)
python pipeline.py --full-refresh --workers 8

# Dry-run: mostra o que faria sem executar
python pipeline.py --dry-run
```

### Flags principais

| Flag | Padrão | Descrição |
|------|--------|-----------|
| `--workers N` | 8 | Threads paralelas de scraping |
| `--resume PATH` | — | PDF do currículo; ativa etapa de análise |
| `--provider` | groq | Provider de IA para análise |
| `--model` | padrão do provider | Modelo específico |
| `--top N` | 10 | Top N vagas no ranking final |
| `--min-score N` | 0 | Score mínimo para aparecer no ranking |
| `--domain NAME` | — | Filtra por empresa |
| `--type TYPE` | — | Filtra por tipo de ATS |
| `--job-area AREA` | — | Filtra por área (tech, product, etc.) |
| `--no-filter` | false | Pula etapa de filtragem TI |
| `--no-analyze` | false | Pula etapa de análise |
| `--full-refresh` | false | Recoleta mesmo vagas já no banco |
| `--scrape-only` | false | Só coleta e persiste |
| `--analyze-only` | false | Só analisa vagas já no banco |
| `--db-path PATH` | jobs.db | Caminho do SQLite |
| `--proxy-list FILE` | — | Arquivo com proxies (uma por linha) |
| `--dry-run` | false | Simula sem persistir ou chamar IA |
| `--save-markdown` | false | Salva .md em output/ (retrocompat.) |

---

## Regras de Negócio Consolidadas

### Coleta

1. **Deduplicação dentro do run:** a mesma URL não é coletada duas vezes mesmo
   se aparecer em domínios duplicados no `domain.yaml`.

2. **Deduplicação entre runs:** vagas já no banco com o mesmo `(company, source_id)`
   não geram novo arquivo .md nem nova linha no CSV, a menos que `--full-refresh`.

3. **Falha isolada:** erro em um domínio nunca interrompe os demais; é logado e
   o run continua. Após 3 falhas seguidas no mesmo domínio, ele é marcado como
   `error` e pulado.

4. **Rate limit respeitado:** delay mínimo de 500 ms entre requests ao mesmo host.
   Domínios com `rate_limit: strict` usam 2 s. `429 Too Many Requests` dobra o
   delay até o máximo de 30 s.

5. **Timeout de vaga:** extração de descrição tem timeout de 15 s. Se exceder,
   a vaga é incluída sem descrição (não descartada).

### Filtragem

6. **Conservadorismo em caso de erro de IA:** se o batch de classificação falhar,
   todas as vagas do lote são consideradas TI (não descartadas). Melhor falso
   positivo do que perder vagas relevantes.

7. **Prioridade das regras:** EXCLUSION_PATTERNS > PRODUCT_PATTERNS > CERTAIN_IT_PATTERNS > IA.
   Ou seja, se o título corresponde a uma exclusão explícita, a IA não é consultada.

8. **Campo `job_area` no domínio** aplica filtro de palavras-chave antes de qualquer
   chamada de IA, reduzindo custo quando só se quer vagas de tech numa empresa grande.

### Análise

9. **Cache por currículo:** o score de uma vaga não é recalculado para o mesmo PDF
   (identificado por sha256). Para forçar reanálise, usar `--reanalyze`.

10. **Erros consecutivos de IA:** após 3 erros seguidos no analyzer, a execução
    é interrompida com mensagem de diagnóstico (pode ser cota esgotada).

11. **Score -1** indica erro na análise (vaga existe no banco mas score não foi
    obtido). Não entra no ranking final.

### Proxy

12. **Proxy opcional:** sem `PROXY_LIST`, o sistema funciona normalmente sem proxy.

13. **Fallback sem proxy:** se todos os proxies do pool falharem, a request é
    tentada sem proxy uma última vez antes de marcar o job como erro.

14. **Rotação automática:** após cada request bem-sucedida, o próximo proxy da
    lista é usado (round-robin). Proxies com 3+ falhas consecutivas são removidos
    temporariamente do pool.

---

## Estrutura de Arquivos Proposta

```
hora-extra/
├── pipeline.py          ← novo ponto de entrada unificado
├── scraper.py           ← adapters existentes + suporte a ThreadPoolExecutor
├── filter_tech.py       ← lógica de filtragem (sem alteração de lógica)
├── analyzer.py          ← lógica de scoring (sem alteração de lógica)
├── organizar.py         ← exportação (sem alteração de lógica)
├── db.py                ← novo: camada de banco de dados
├── queue_pipeline.py    ← novo: orquestrador de filas
├── proxy.py             ← novo: ProxyPool
├── domain.yaml          ← existente (novos campos opcionais)
├── requirements.txt     ← adicionar: sqlalchemy (ou sqlite3), redis (opcional)
├── .env                 ← adicionar: DATABASE_URL, PROXY_LIST, PROXY_MODE
├── jobs.db              ← banco SQLite (gerado em runtime)
├── output/              ← .md individuais (retrocompat.)
├── vagas-tech/          ← .md filtrados (retrocompat.)
├── vagas.csv
└── vagas_organizado.xlsx
```

---

## Dependências a Adicionar

```
# requirements.txt — adições
sqlalchemy>=2.0        # ORM para SQLite/PostgreSQL
alembic                # migrações de schema (opcional)
redis                  # modo distribuído (opcional)
rq                     # task queue sobre Redis (opcional)
tenacity               # retry com backoff (proxy + IA)
```

---

## Ordem de Implementação Sugerida

| # | Entregável | Impacto | Esforço |
|---|-----------|---------|---------|
| 1 | `db.py` + schema + upsert | Deduplicação, histórico | Médio |
| 2 | Paralelismo por empresa (`ThreadPoolExecutor`) | Velocidade ×3–8 | Baixo |
| 3 | `proxy.py` + integração nos scrapers | Estabilidade em escala | Médio |
| 4 | `queue_pipeline.py` (in-process) | Pipeline contínuo | Alto |
| 5 | `pipeline.py` CLI unificado | UX / automação | Médio |
| 6 | Paralelismo de descrições por vaga | Velocidade extra | Baixo |
| 7 | Modo Redis/RQ (distribuído) | Escala horizontal | Alto |
