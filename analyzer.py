import argparse
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def extract_pdf_text(pdf_path: str) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except ImportError:
        pass

    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except ImportError:
        pass

    raise RuntimeError(
        "Nenhuma biblioteca de PDF encontrada. Instale: pip install pdfplumber"
    )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@dataclass
class JobFile:
    path: Path
    company: str
    title: str
    content: str
    location: str = ""


def _parse_location(content: str) -> str:
    match = re.search(r"\*\*Localização:\*\*\s*(.+)", content)
    return match.group(1).strip() if match else ""


def load_jobs(output_dir: str = "output") -> list[JobFile]:
    jobs = []
    for md_file in Path(output_dir).rglob("*.md"):
        content = md_file.read_text(encoding="utf-8")
        company = md_file.parent.name
        title = md_file.stem.replace("_", " ")
        location = _parse_location(content)
        jobs.append(JobFile(path=md_file, company=company, title=title, content=content, location=location))
    return jobs


# ---------------------------------------------------------------------------
# Filtros de pré-seleção (antes da IA)
# ---------------------------------------------------------------------------

WORK_TYPE_KEYWORDS: dict[str, list[str]] = {
    "remoto":     ["remote", "remoto"],
    "hibrido":    ["hybrid", "híbrido", "hibrido"],
    "presencial": ["on-site", "on site", "presencial"],
}


def _filter_by_work_type(jobs: list[JobFile], work_type: str) -> list[JobFile]:
    keywords = WORK_TYPE_KEYWORDS.get(work_type.lower(), [work_type.lower()])
    pattern = re.compile("|".join(re.escape(kw) for kw in keywords), re.IGNORECASE)
    return [j for j in jobs if pattern.search(j.location) or pattern.search(j.title)]


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "Você é um recrutador sênior especializado em análise de perfis e vagas de emprego. "
    "Analise o currículo e a vaga fornecidos e responda SOMENTE com um JSON válido, sem markdown."
)

SCORE_PROMPT = """\
Currículo:
{resume}

---

Vaga: {title} ({company})
{job}

---

Avalie o quanto este currículo se encaixa nesta vaga e responda SOMENTE com JSON no formato abaixo:
{{
  "score": <inteiro de 0 a 100>,
  "matches": [<lista de até 5 pontos fortes do candidato para esta vaga>],
  "gaps": [<lista de até 5 lacunas ou pontos fracos>],
  "summary": "<resumo em 1 frase>"
}}
"""


def _build_prompt(resume: str, job: JobFile) -> str:
    return SCORE_PROMPT.format(
        resume=resume[:6000],
        title=job.title,
        company=job.company,
        job=job.content[:4000],
    )


def _parse_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


# --- OpenAI ---

def score_openai(resume: str, job: JobFile, model: str) -> dict:
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(resume, job)},
        ],
        temperature=0,
    )
    return _parse_response(resp.choices[0].message.content)


# --- Groq ---

def score_groq(resume: str, job: JobFile, model: str) -> dict:
    from groq import Groq
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(resume, job)},
        ],
        temperature=0,
    )
    return _parse_response(resp.choices[0].message.content)


# --- Gemini ---

def score_gemini(resume: str, job: JobFile, model: str) -> dict:
    import google.generativeai as genai
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    gmodel = genai.GenerativeModel(model)
    prompt = f"{SYSTEM_PROMPT}\n\n{_build_prompt(resume, job)}"
    resp = gmodel.generate_content(prompt)
    return _parse_response(resp.text)


# --- Ollama ---

def score_ollama(resume: str, job: JobFile, model: str) -> dict:
    import requests
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(resume, job)},
        ],
        "stream": False,
        "options": {"temperature": 0},
    }
    resp = requests.post("http://localhost:11434/api/chat", json=payload, timeout=120)
    resp.raise_for_status()
    return _parse_response(resp.json()["message"]["content"])


PROVIDERS = {
    "openai": score_openai,
    "groq": score_groq,
    "gemini": score_gemini,
    "ollama": score_ollama,
}

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "groq": "llama-3.3-70b-versatile",
    "gemini": "gemini-2.0-flash",
    "ollama": "llama3.2",
}


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class ScoredJob:
    job: JobFile
    score: int
    matches: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    summary: str = ""
    error: str = ""


def _filter_jobs(jobs: list[JobFile], keywords: list[str]) -> list[JobFile]:
    pattern = re.compile("|".join(re.escape(kw) for kw in keywords), re.IGNORECASE)
    return [j for j in jobs if pattern.search(j.title)]


def analyze(
    resume_path: str,
    output_dir: str = "output",
    provider: str = "groq",
    model: str | None = None,
    top: int = 10,
    min_score: int = 0,
    delay: float = 0.5,
    max_consecutive_errors: int = 3,
    keywords: list[str] | None = None,
    work_type: str | None = None,
) -> list[ScoredJob]:
    resume = extract_pdf_text(resume_path)
    jobs = load_jobs(output_dir)

    if not jobs:
        print(f"[WARN] Nenhuma vaga encontrada em '{output_dir}'")
        return []

    if work_type:
        before = len(jobs)
        jobs = _filter_by_work_type(jobs, work_type)
        print(f"[INFO] Filtro de modalidade '{work_type}': {len(jobs)} vaga(s) de {before}")
        if not jobs:
            print(f"[WARN] Nenhuma vaga encontrada com modalidade '{work_type}'")
            return []

    if keywords:
        before = len(jobs)
        jobs = _filter_jobs(jobs, keywords)
        print(f"[INFO] Filtro de cargo {keywords}: {len(jobs)} vaga(s) de {before}")
        if not jobs:
            print(f"[WARN] Nenhuma vaga encontrada com os termos: {', '.join(keywords)}")
            return []

    scorer = PROVIDERS[provider]
    effective_model = model or DEFAULT_MODELS[provider]

    print(f"[INFO] Provider: {provider} | Modelo: {effective_model}")
    print(f"[INFO] Currículo: {resume_path}")
    print(f"[INFO] Vagas a analisar: {len(jobs)}")
    print()

    results: list[ScoredJob] = []
    consecutive_errors = 0

    for i, job in enumerate(jobs, 1):
        print(f"  [{i:>3}/{len(jobs)}] {job.company} — {job.title} ... ", end="", flush=True)
        try:
            data = scorer(resume, job, effective_model)
            scored = ScoredJob(
                job=job,
                score=int(data.get("score", 0)),
                matches=data.get("matches", []),
                gaps=data.get("gaps", []),
                summary=data.get("summary", ""),
            )
            print(f"score: {scored.score}")
            consecutive_errors = 0
        except Exception as e:
            scored = ScoredJob(job=job, score=-1, error=str(e))
            print(f"ERRO: {e}")
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                print(f"\n[FATAL] {consecutive_errors} erros consecutivos — abortando.")
                print(f"[FATAL] Último erro: {e}")
                break

        results.append(scored)
        time.sleep(delay)

    results = [r for r in results if r.score >= min_score]
    results.sort(key=lambda r: r.score, reverse=True)
    return results[:top]


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_results(results: list[ScoredJob], show_details: bool = True) -> None:
    if not results:
        print("Nenhuma vaga acima do score mínimo.")
        return

    print(f"\n{'='*65}")
    print(f"  TOP {len(results)} VAGAS — MELHOR MATCH PARA O PERFIL")
    print(f"{'='*65}\n")

    for rank, r in enumerate(results, 1):
        bar = "█" * (r.score // 5) + "░" * (20 - r.score // 5)
        print(f"  #{rank:>2}  [{bar}] {r.score:>3}/100")
        print(f"       {r.job.company} — {r.job.title}")
        if r.job.location:
            print(f"       📍 {r.job.location}")
        print(f"       {r.job.path}")
        if r.summary:
            print(f"       {r.summary}")

        if show_details and r.matches:
            print(f"       ✔ Pontos fortes:")
            for m in r.matches:
                print(f"         • {m}")

        if show_details and r.gaps:
            print(f"       ✘ Lacunas:")
            for g in r.gaps:
                print(f"         • {g}")

        print()


def save_json(results: list[ScoredJob], path: str) -> None:
    data = []
    for r in results:
        data.append({
            "rank": results.index(r) + 1,
            "score": r.score,
            "company": r.job.company,
            "title": r.job.title,
            "location": r.job.location,
            "path": str(r.job.path),
            "summary": r.summary,
            "matches": r.matches,
            "gaps": r.gaps,
        })
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[INFO] Resultado salvo em {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analisa currículo e ranqueia vagas por compatibilidade")
    parser.add_argument("resume", help="Caminho para o currículo em PDF")
    parser.add_argument(
        "--provider", "-p",
        choices=list(PROVIDERS),
        default="groq",
        help="Provider de IA (padrão: groq)",
    )
    parser.add_argument(
        "--model", "-m",
        help="Modelo a usar (padrão depende do provider)",
    )
    parser.add_argument(
        "--output-dir", "-o",
        default="output",
        help="Diretório com as vagas em markdown (padrão: output)",
    )
    parser.add_argument(
        "--top", "-n",
        type=int,
        default=10,
        help="Quantas vagas exibir no ranking (padrão: 10)",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=0,
        help="Score mínimo para incluir no resultado (padrão: 0)",
    )
    parser.add_argument(
        "--no-details",
        action="store_true",
        help="Exibir apenas score e título, sem pontos fortes/lacunas",
    )
    parser.add_argument(
        "--save-json",
        metavar="FILE",
        help="Salvar resultado em JSON (ex: resultado.json)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Delay entre requisições em segundos (padrão: 0.5)",
    )
    parser.add_argument(
        "--filter", "-f",
        nargs="+",
        metavar="KEYWORD",
        help="Filtrar vagas pelo cargo/título (ex: --filter DevOps SRE 'Tech Lead')",
    )
    parser.add_argument(
        "--work-type", "-w",
        choices=["remoto", "hibrido", "presencial"],
        metavar="MODALIDADE",
        help="Filtrar por modalidade de trabalho: remoto, hibrido ou presencial",
    )
    args = parser.parse_args()

    results = analyze(
        resume_path=args.resume,
        output_dir=args.output_dir,
        provider=args.provider,
        model=args.model,
        top=args.top,
        min_score=args.min_score,
        delay=args.delay,
        keywords=args.filter,
        work_type=args.work_type,
    )

    print_results(results, show_details=not args.no_details)

    if args.save_json:
        save_json(results, args.save_json)
