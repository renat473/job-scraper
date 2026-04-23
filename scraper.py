import json
import re
import unicodedata
import yaml
import requests
from bs4 import BeautifulSoup
from dataclasses import dataclass
from pathlib import Path
import time


@dataclass
class Job:
    title: str
    location: str = ""
    department: str = ""
    url: str = ""
    company: str = ""
    description: str = ""


# ---------------------------------------------------------------------------
# Filtro por área
# ---------------------------------------------------------------------------

AREA_KEYWORDS: dict[str, list[str]] = {
    "tech": [
        "engineer", "developer", "dev", "software", "backend", "frontend",
        "fullstack", "devops", "sre", "data", "machine learning", "ml", "ia",
        "android", "ios", "mobile", "cloud", "infra", "arquiteto", "architect",
        "qa", "quality", "teste", "test", "python", "java", "node", "react",
        "flutter", "platform", "security", "tech lead", "cto", "engenharia",
        "desenvolvimento", "programador",
    ],
    "marketing": [
        "marketing", "growth", "seo", "aso", "social media", "influenc",
        "branding", "conteúdo", "content", "copywriter", "performance",
        "crm", "mídias", "mídia", "tráfego", "ads",
    ],
    "product": [
        "product", "produto", "product manager", "product owner", "po", "pm",
        "ux", "ui", "design", "designer", "pesquisa", "research",
    ],
    "sales": [
        "sales", "vendas", "account", "comercial", "revenue", "business",
        "negócios", "negocio", "parceria", "partnership", "executivo",
    ],
    "people": [
        "people", "rh", "recursos humanos", "talent", "talentos", "recrut",
        "hiring", "cultura", "treinamento", "development", "hrbp",
    ],
    "finance": [
        "finance", "finanças", "financeiro", "contab", "fiscal", "controladoria",
        "treasury", "tesouraria", "audit", "auditoria", "fpa",
    ],
    "operations": [
        "operations", "operações", "operacional", "logistics", "logística",
        "supply", "analista", "suporte", "support", "customer", "cs", "cx",
        "success", "onboarding",
    ],
    "legal": [
        "legal", "jurídico", "compliance", "counsel", "advogad", "regulatório",
        "privacy", "privacidade",
    ],
}


def _matches_area(job: "Job", area: str) -> bool:
    keywords = AREA_KEYWORDS.get(area.lower(), [area.lower()])
    haystack = f"{job.title} {job.department}".lower()
    return any(re.search(rf"\b{re.escape(kw)}\b", haystack) for kw in keywords)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def _safe_filename(text: str) -> str:
    text = text.strip()
    text = re.sub(r'[\\/*?:"<>|]', "", text)
    text = re.sub(r"\s+", "_", text)
    return text[:80]


def save_job_markdown(job: "Job", output_dir: str = "output") -> Path:
    company_dir = Path(output_dir) / _safe_filename(job.company)
    company_dir.mkdir(parents=True, exist_ok=True)

    filename = _safe_filename(job.title) + ".md"
    filepath = company_dir / filename

    lines = [
        f"# {job.title}",
        "",
        f"**Empresa:** {job.company}",
    ]
    if job.department:
        lines.append(f"**Departamento:** {job.department}")
    if job.location:
        lines.append(f"**Localização:** {job.location}")
    lines += [
        f"**URL:** {job.url}",
        "",
        "---",
        "",
        "## Descrição",
        "",
        job.description or "_Sem descrição disponível._",
    ]

    filepath.write_text("\n".join(lines), encoding="utf-8")
    return filepath


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------

def _sibling_text(icon_span) -> str:
    if not icon_span:
        return ""
    p = icon_span.find_next_sibling("p")
    return p.get_text(strip=True) if p else ""


def _fetch_description(url: str, session: requests.Session) -> str:
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        container = soup.select_one("div.ATS_htmlPreview")
        if not container:
            return ""
        lines = []
        for el in container.find_all(["p", "li", "h1", "h2", "h3", "h4"]):
            text = el.get_text(separator=" ", strip=True)
            if text:
                lines.append(text)
        return "\n".join(lines)
    except Exception as e:
        return f"[Erro ao carregar descrição: {e}]"


def _to_slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    lines = []
    for el in soup.find_all(["p", "li", "h1", "h2", "h3", "h4"]):
        text = el.get_text(separator=" ", strip=True)
        if text:
            lines.append(text)
    return "\n".join(lines)


def _fetch_description_ashby(url: str, session: requests.Session) -> str:
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for script in soup.find_all("script"):
            text = script.string or ""
            if "window.__appData" in text:
                raw = text.split("window.__appData = ", 1)[1]
                data, _ = json.JSONDecoder().raw_decode(raw)
                html = data.get("posting", {}).get("descriptionHtml", "")
                return _html_to_text(html)
        return ""
    except Exception as e:
        return f"[Erro ao carregar descrição: {e}]"


def _fetch_description_lever(url: str, session: requests.Session) -> str:
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        container = soup.select_one('[data-qa="job-description"]')
        if not container:
            return ""
        lines = []
        for el in container.find_all(["p", "li", "h1", "h2", "h3", "h4", "div"]):
            if el.find(["p", "li", "h1", "h2", "h3", "h4", "div"]):
                continue
            text = el.get_text(separator=" ", strip=True)
            if text:
                lines.append(text)
        return "\n".join(lines)
    except Exception as e:
        return f"[Erro ao carregar descrição: {e}]"


def scrape_rippling(domain: dict, fetch_description: bool = True) -> list[Job]:
    url = domain["url"]
    company = domain["name"]
    jobs = []

    session = requests.Session()
    response = session.get(url, timeout=15)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    title_links = soup.select("a[href*='/jobs/'][class]")

    seen = set()
    for link in title_links:
        href = link.get("href", "")
        if not href or href in seen:
            continue
        seen.add(href)

        full_url = href if href.startswith("http") else f"https://ats.rippling.com{href}"
        title = link.get_text(strip=True)
        if not title:
            continue

        container = link.parent.parent.parent

        dept_icon = container.select_one('[data-icon="DEPARTMENTS_OUTLINE"]')
        department = _sibling_text(dept_icon)

        loc_icon = container.select_one('[data-icon="LOCATION_OUTLINE"]')
        location = _sibling_text(loc_icon)

        description = ""
        if fetch_description:
            print(f"  [→] Carregando descrição: {title}")
            description = _fetch_description(full_url, session)
            time.sleep(0.5)

        jobs.append(Job(
            title=title,
            location=location,
            department=department,
            url=full_url,
            company=company,
            description=description,
        ))

    return jobs


def scrape_ashbyhq(domain: dict, fetch_description: bool = True) -> list[Job]:
    url = domain["url"]
    company = domain["name"]
    base_url = url.rstrip("/")
    jobs = []

    session = requests.Session()
    response = session.get(url, timeout=15)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    app_data = None
    for script in soup.find_all("script"):
        text = script.string or ""
        if "window.__appData" in text:
            raw = text.split("window.__appData = ", 1)[1]
            app_data, _ = json.JSONDecoder().raw_decode(raw)
            break

    if not app_data:
        return jobs

    postings = app_data.get("jobBoard", {}).get("jobPostings", [])

    for posting in postings:
        if not posting.get("isListed", True):
            continue

        title = posting.get("title", "").strip()
        if not title:
            continue

        job_id = posting.get("id", "")
        job_url = f"{base_url}/{job_id}"

        department = posting.get("teamName") or posting.get("departmentName") or ""
        location_name = posting.get("locationName") or ""
        workplace = posting.get("workplaceType") or ""
        location = f"{workplace} ({location_name})" if workplace and location_name else location_name or workplace

        description = ""
        if fetch_description:
            print(f"  [→] Carregando descrição: {title}")
            description = _fetch_description_ashby(job_url, session)
            time.sleep(0.5)

        jobs.append(Job(
            title=title,
            location=location,
            department=department,
            url=job_url,
            company=company,
            description=description,
        ))

    return jobs


def scrape_lever(domain: dict, fetch_description: bool = True) -> list[Job]:
    url = domain["url"]
    company = domain["name"]
    jobs = []

    session = requests.Session()
    response = session.get(url, timeout=15)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    for group in soup.select(".postings-group"):
        department = group.select_one(".posting-category-title")
        department_text = department.get_text(strip=True) if department else ""

        for posting in group.select(".posting"):
            title_link = posting.select_one("a.posting-title")
            if not title_link:
                continue

            title = title_link.select_one("[data-qa='posting-name']")
            title_text = title.get_text(strip=True) if title else title_link.get_text(strip=True)
            if not title_text:
                continue

            job_url = title_link.get("href", "")

            location_el = title_link.select_one(".location")
            location = location_el.get_text(strip=True) if location_el else ""

            workplace_el = title_link.select_one(".workplaceTypes")
            workplace = workplace_el.get_text(strip=True).rstrip(" —").strip() if workplace_el else ""
            if workplace and location:
                location = f"{workplace} — {location}"
            elif workplace:
                location = workplace

            description = ""
            if fetch_description:
                print(f"  [→] Carregando descrição: {title_text}")
                description = _fetch_description_lever(job_url, session)
                time.sleep(0.5)

            jobs.append(Job(
                title=title_text,
                location=location,
                department=department_text,
                url=job_url,
                company=company,
                description=description,
            ))

    return jobs


def scrape_inhire(domain: dict, fetch_description: bool = True) -> list[Job]:
    url = domain["url"]
    company = domain["name"]
    tenant = url.split("//")[1].split(".")[0]

    api_base = "https://api.inhire.app"
    headers = {"Origin": url, "X-Tenant": tenant}
    jobs = []

    session = requests.Session()
    r = session.get(f"{api_base}/job-posts/public/pages/lean", headers=headers, timeout=15)
    r.raise_for_status()
    postings = r.json()

    for posting in postings:
        title = posting.get("displayName", "").strip()
        job_id = posting.get("jobId", "")

        if not title or not job_id:
            continue

        location = ""
        department = ""
        description = ""

        if fetch_description:
            print(f"  [→] Carregando descrição: {title}")
            try:
                r2 = session.get(f"{api_base}/job-posts/public/pages/{job_id}", headers=headers, timeout=15)
                r2.raise_for_status()
                detail = r2.json()

                location_name = detail.get("location", "")
                workplace = detail.get("workplaceType", "")
                if workplace and location_name:
                    location = f"{workplace} — {location_name}"
                else:
                    location = location_name or workplace

                desc_html = detail.get("description", "")
                description = _html_to_text(desc_html)
            except Exception as e:
                description = f"[Erro ao carregar descrição: {e}]"
            time.sleep(0.5)

        base = url.rstrip("/")
        job_url = f"{base}/{job_id}/{_to_slug(title)}"

        jobs.append(Job(
            title=title,
            location=location,
            department=department,
            url=job_url,
            company=company,
            description=description,
        ))

    return jobs


def scrape_greenhouse(domain: dict, fetch_description: bool = True) -> list[Job]:
    url = domain["url"]
    company = domain["name"]
    # extrai o slug da empresa da URL (ex: quintoandar de .../quintoandar)
    slug = url.rstrip("/").split("/")[-1]

    api_base = "https://boards-api.greenhouse.io/v1/boards"
    jobs = []

    session = requests.Session()
    r = session.get(f"{api_base}/{slug}/jobs", timeout=15)
    r.raise_for_status()
    data = r.json()

    for posting in data.get("jobs", []):
        title = posting.get("title", "").strip()
        if not title:
            continue

        job_id = posting.get("id")
        job_url = posting.get("absolute_url", "")
        location = posting.get("location", {}).get("name", "")
        departments = posting.get("departments", [])
        department = departments[0].get("name", "") if departments else ""

        description = ""
        if fetch_description and job_id:
            print(f"  [→] Carregando descrição: {title}")
            try:
                r2 = session.get(f"{api_base}/{slug}/jobs/{job_id}", timeout=15)
                r2.raise_for_status()
                detail = r2.json()
                description = _html_to_text(detail.get("content", ""))
            except Exception as e:
                description = f"[Erro ao carregar descrição: {e}]"
            time.sleep(0.5)

        jobs.append(Job(
            title=title,
            location=location,
            department=department,
            url=job_url,
            company=company,
            description=description,
        ))

    return jobs


SCRAPERS = {
    "rippling": scrape_rippling,
    "ashbyhq": scrape_ashbyhq,
    "lever": scrape_lever,
    "inhire": scrape_inhire,
    "greenhouse": scrape_greenhouse,
}


# ---------------------------------------------------------------------------
# Orquestrador
# ---------------------------------------------------------------------------

def load_domains(path: str = "domain.yaml") -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("domains", [])


def scrape_all(
    domains_path: str = "domain.yaml",
    fetch_description: bool = True,
    domain_filter: str | None = None,
    type_filter: str | None = None,
    save_markdown: bool = False,
) -> list[Job]:
    domains = load_domains(domains_path)
    all_jobs: list[Job] = []

    if domain_filter:
        domains = [d for d in domains if d["name"].lower() == domain_filter.lower()]
        if not domains:
            print(f"[WARN] Nenhum domínio encontrado com o nome '{domain_filter}'")
            return all_jobs

    if type_filter:
        domains = [d for d in domains if d.get("type", "").lower() == type_filter.lower()]
        if not domains:
            print(f"[WARN] Nenhum domínio encontrado com o tipo '{type_filter}'")
            return all_jobs

    for domain in domains:
        domain_type = domain.get("type", "generic")
        scraper = SCRAPERS.get(domain_type)
        job_area = domain.get("job_area")

        if not scraper:
            print(f"[WARN] Sem scraper para o tipo '{domain_type}' ({domain['name']})")
            continue

        print(f"[INFO] Coletando vagas de {domain['name']} ({domain['url']}) ...")
        if job_area:
            print(f"[INFO] Filtro de área ativo: {job_area}")

        try:
            jobs = scraper(domain, fetch_description=fetch_description)

            if job_area:
                before = len(jobs)
                jobs = [j for j in jobs if _matches_area(j, job_area)]
                print(f"[INFO] {len(jobs)} vaga(s) após filtro '{job_area}' (de {before})")
            else:
                print(f"[INFO] {len(jobs)} vaga(s) encontrada(s) em {domain['name']}")

            if save_markdown:
                for job in jobs:
                    save_job_markdown(job)
                print(f"[INFO] {len(jobs)} arquivo(s) markdown salvos em output/{domain['name']}/")

            all_jobs.extend(jobs)
        except Exception as e:
            print(f"[ERROR] Falha ao coletar {domain['name']}: {e}")

        time.sleep(1)

    return all_jobs


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_jobs(jobs: list[Job], show_description: bool = True) -> None:
    if not jobs:
        print("Nenhuma vaga encontrada.")
        return

    print(f"\n{'='*60}")
    print(f"Total de vagas encontradas: {len(jobs)}")
    print(f"{'='*60}\n")

    for i, job in enumerate(jobs, 1):
        print(f"{i:>3}. [{job.company}] {job.title}")
        if job.department:
            print(f"       Departamento : {job.department}")
        if job.location:
            print(f"       Localização  : {job.location}")
        print(f"       URL          : {job.url}")
        if show_description and job.description:
            print(f"       --- Descrição ---")
            for line in job.description.splitlines():
                print(f"       {line}")
        print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Scraper de vagas de emprego")
    parser.add_argument("--domain", "-d", help="Nome do domínio definido no domain.yaml (ex: Skeelo)")
    parser.add_argument("--type", "-t", help="Tipo de ATS para filtrar (ex: rippling, ashbyhq, lever, inhire)")
    parser.add_argument("--no-description", action="store_true", help="Pular extração de descrição")
    parser.add_argument("--save-markdown", action="store_true", help="Salvar cada vaga como arquivo .md individual em output/")
    args = parser.parse_args()

    jobs = scrape_all(
        domain_filter=args.domain,
        type_filter=args.type,
        fetch_description=not args.no_description,
        save_markdown=args.save_markdown,
    )
    print_jobs(jobs)
