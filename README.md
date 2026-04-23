# Job Scraper + Analyzer

Scraper de vagas de emprego que coleta título, departamento, localização e descrição completa de múltiplos ATSs (Applicant Tracking Systems), configurados via arquivo YAML.

Inclui também um analisador de currículo que compara um PDF com as vagas coletadas e gera um ranking de compatibilidade com score via IA.

---

## Requisitos

```bash
pip install beautifulsoup4 requests pyyaml pdfplumber python-dotenv openai groq google-generativeai
```

---

## Estrutura do projeto

```
hora-extra/
├── scraper.py      # Coleta vagas e salva em markdown
├── analyzer.py     # Analisa currículo e ranqueia vagas por score
├── domain.yaml     # Lista de domínios/empresas
├── .env            # Chaves de API (não versionar)
└── output/         # Arquivos markdown gerados (criado automaticamente)
```

---

## Configuração — .env

Copie o `.env` e preencha as chaves dos providers que for usar:

```env
# OpenAI
OPENAI_API_KEY=

# Groq
GROQ_API_KEY=

# Gemini
GEMINI_API_KEY=

# Ollama roda localmente — sem chave necessária
```

> O `analyzer.py` carrega o `.env` automaticamente via `python-dotenv`.

---

## scraper.py — Coletar vagas

### Configuração — domain.yaml

Cada entrada representa uma empresa e seus parâmetros de scraping:

```yaml
domains:

  - name: nome-da-empresa
    url: https://empresa.ats.com/vagas
    type: rippling        # tipo do ATS (ver tabela abaixo)
    job_area: tech        # opcional — filtra vagas por área
```

### Tipos de ATS suportados (`type`)

| Valor      | Plataforma   | Exemplo de URL                          |
|------------|--------------|-----------------------------------------|
| `rippling` | Rippling ATS | `https://ats.rippling.com/empresa/jobs` |
| `ashbyhq`  | Ashby        | `https://jobs.ashbyhq.com/empresa`      |
| `lever`    | Lever        | `https://jobs.lever.co/empresa`         |
| `inhire`   | InHire       | `https://empresa.inhire.app/vagas`      |

### Filtro por área (`job_area`)

| Valor        | Exemplos de vagas filtradas                              |
|--------------|----------------------------------------------------------|
| `tech`       | Developer, Engineer, DevOps, SRE, QA, Backend, Frontend  |
| `marketing`  | Growth, SEO, ASO, Social Media, Performance, Ads         |
| `product`    | Product Manager, Product Owner, UX, UI, Designer         |
| `sales`      | Account Executive, Comercial, Revenue, Business          |
| `people`     | RH, Talent Acquisition, Cultura, Treinamento             |
| `finance`    | Financeiro, Contabilidade, Controladoria, Auditoria      |
| `operations` | Operações, Customer Success, Suporte, Logística          |
| `legal`      | Jurídico, Compliance, Counsel, Privacidade               |

> Qualquer outro valor é tratado como palavra-chave direta (ex: `job_area: flutter`).

### Uso

```bash
# Coletar todas as empresas
python scraper.py

# Coletar apenas uma empresa
python scraper.py --domain kto
python scraper.py -d Skeelo

# Filtrar por tipo de ATS
python scraper.py --type lever
python scraper.py -t inhire

# Pular extração de descrição (mais rápido)
python scraper.py --domain kto --no-description

# Salvar cada vaga como arquivo Markdown
python scraper.py --domain kto --save-markdown

# Combinando flags
python scraper.py -t ashbyhq --no-description --save-markdown
```

Os arquivos são salvos em `output/{nome-da-empresa}/Titulo_da_Vaga.md`.

### Formato dos arquivos Markdown gerados

```markdown
# Título da Vaga

**Empresa:** nome
**Departamento:** Engineering
**Localização:** Remote — São Paulo
**URL:** https://...

---

## Descrição

Texto completo da vaga...
```

---

## analyzer.py — Analisar currículo

Lê um currículo em PDF, compara com todas as vagas em `output/` usando IA e gera um ranking com score de 0–100.

### Providers e modelos padrão

| Flag `--provider` | Modelo padrão              | Chave necessária   |
|-------------------|----------------------------|--------------------|
| `groq`            | `llama-3.3-70b-versatile`  | `GROQ_API_KEY`     |
| `openai`          | `gpt-4o-mini`              | `OPENAI_API_KEY`   |
| `gemini`          | `gemini-2.0-flash`         | `GEMINI_API_KEY`   |
| `ollama`          | `llama3.2`                 | _(local, sem chave)_ |

### Uso

```bash
# Básico (usa Groq por padrão)
python analyzer.py meu_curriculo.pdf

# Escolher provider e modelo
python analyzer.py meu_curriculo.pdf --provider openai
python analyzer.py meu_curriculo.pdf --provider openai --model gpt-4o
python analyzer.py meu_curriculo.pdf --provider groq --model llama-3.3-70b-versatile
python analyzer.py meu_curriculo.pdf --provider gemini
python analyzer.py meu_curriculo.pdf --provider ollama --model llama3.2

# Exibir apenas top 5 com score mínimo de 60
python analyzer.py meu_curriculo.pdf --top 5 --min-score 60

# Salvar resultado em JSON
python analyzer.py meu_curriculo.pdf --save-json resultado.json

# Exibir apenas score e título (sem detalhes)
python analyzer.py meu_curriculo.pdf --no-details

# Diretório de vagas customizado
python analyzer.py meu_curriculo.pdf --output-dir output/

# Delay entre requisições (útil para evitar rate limit)
python analyzer.py meu_curriculo.pdf --delay 1.0
```

### Parâmetros

| Flag | Atalho | Padrão | Descrição |
|------|--------|--------|-----------|
| `--provider` | `-p` | `groq` | Provider de IA |
| `--model` | `-m` | _(por provider)_ | Modelo a usar |
| `--output-dir` | `-o` | `output` | Diretório com as vagas |
| `--top` | `-n` | `10` | Quantas vagas exibir |
| `--min-score` | | `0` | Score mínimo para incluir |
| `--no-details` | | — | Só score e título |
| `--save-json` | | — | Salvar resultado em JSON |
| `--delay` | | `0.5` | Delay entre requisições (segundos) |

### Exemplo de saída

```
  # 1  [████████████████░░░░]  82/100
       nomadglobal — Senior Site Reliability Engineer (SRE)
       output/nomadglobal/Senior_Site_Reliability_Engineer_(SRE).md
       Perfil sênior com foco em cloud AWS e Kubernetes, bem alinhado à vaga.
       ✔ Pontos fortes:
         • Experiência com AWS (EKS, RDS, IAM)
         • Conhecimento em Terraform e IaC
         • Familiaridade com Prometheus e Grafana
       ✘ Lacunas:
         • Inglês intermediário não mencionado
         • Sem experiência explícita com Chaos Engineering
```

---

## Adicionando novos domínios

1. Identifique o tipo do ATS da empresa
2. Adicione a entrada no `domain.yaml`:

```yaml
  - name: minhaempresa
    url: https://minhaempresa.inhire.app/vagas
    type: inhire
```

3. Execute o scraper e depois o analyzer:

```bash
python scraper.py --domain minhaempresa --save-markdown
python analyzer.py meu_curriculo.pdf
```

> Para ATSs não suportados, implemente um novo scraper em `scraper.py` e registre no dicionário `SCRAPERS`.
