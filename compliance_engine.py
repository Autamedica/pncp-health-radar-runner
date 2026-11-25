#!/usr/bin/env python3
"""
Generic B2G Health-Tech Compliance & Gap Matrix Engine ("Lo que exigen vs. Lo que tengo").
Parses official PNCP Termo de Referência / Edital PDFs and tender objects, caches PDF extracts
in Google Sheets across runs, and builds live formula-driven gap analysis tabs against the
user's private Mi_Perfil_Capacidades sheet. Zero company-specific data lives in this module.
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import re
import subprocess
import unicodedata
from typing import Any

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"

PROFILE_HEADERS = [
    "Codigo_Req",
    "Categoria",
    "Requisito_Estandar",
    "Mi_Estado",
    "Mi_Evidencia_Actual",
    "Accion_Cierre_Brecha",
    "Peso_Bloqueante",
]

DETAIL_HEADERS = [
    "ID_Proceso",
    "Organo_Proceso",
    "Codigo_Req",
    "Categoria",
    "Lo_Que_Exigen",
    "Evidencia_En_Pliego",
    "Mi_Estado",
    "Lo_Que_Tengo",
    "Brecha_y_Accion",
    "Bloqueante",
    "Fuente_Doc",
]

GAP_HEADERS = [
    "ID_Proceso",
    "UF",
    "Organo",
    "Proceso",
    "Valor_Est_BRL",
    "Cierre",
    "Fuente_Analisis",
    "Total_Exigencias",
    "Cumplo_TENGO",
    "En_Curso_o_OEM",
    "Brechas_NO_TENGO",
    "Pct_Cobertura",
    "Veredicto_Auto",
    "Resumen_Lo_Que_Falta",
    "Link_Doc_Oficial",
]

# Each rule: (code, category, label, weight, [regex patterns in folded lowercase])
REQUIREMENT_CATALOG: list[tuple[str, str, str, str, list[str]]] = [
    (
        "REQ_CNPJ_CNAE_SW",
        "Jurídico / Societario",
        "CNPJ activo con CNAE compatible (software/salud)",
        "CRITICO",
        [r"\bcnpj\b", r"\bcnae\b", r"contrato social", r"habilitacao juridica", r"objeto social compativel"],
    ),
    (
        "REQ_CNDS_SICAF",
        "Fiscal / Regularidad",
        "Certidões Negativas (Federal, FGTS, CNDT, Municipal) / SICAF",
        "CRITICO",
        [r"\bsicaf\b", r"\bfgts\b", r"\bcndt\b", r"\bcnd\b", r"regularidade fiscal", r"certidao negativa", r"fazenda nacional"],
    ),
    (
        "REQ_CAPITAL_PL",
        "Económico-Financiero",
        "Patrimônio Líquido / Capital Social mínimo (>= 10% contrato) o Balance",
        "ALTO",
        [r"patrimonio liquido", r"capital social minim", r"balanco patrimonial", r"qualificacao economico", r"liquidez geral"],
    ),
    (
        "REQ_ATESTADO_CAP_TEC",
        "Capacidad Técnica",
        "Atestado de Capacidade Técnica previo (público o privado)",
        "CRITICO",
        [r"atestado(?:s)? de capacidade tecnica", r"comprovacao de aptidao", r"atestado tecnico", r"experiencia anterior", r"desempenho de atividade pertinente"],
    ),
    (
        "REQ_POC_VALIDACION",
        "Validación Técnica",
        "Prova de Conceito (PoC) / Validación funcional obligatoria",
        "ALTO",
        [r"prova de conceito", r"\bpoc\b", r"demonstracao pratica", r"amostra do sistema", r"validacao tecnica"],
    ),
    (
        "REQ_REGISTRO_INPI_ABES",
        "Propiedad / Certificación SW",
        "Registro de Software / Certificación (INPI, ABES o propiedad IP)",
        "MEDIO",
        [r"\babes\b", r"\binpi\b", r"registro do software", r"propriedade intelectual", r"direito de comercializacao"],
    ),
    (
        "REQ_TELEMEDICINA_VIDEO",
        "Producto Core",
        "Plataforma de Telemedicina / Videoconsulta / Teleinterconsulta",
        "CRITICO",
        [r"telemedic", r"telessaud", r"teleconsult", r"teleinterconsult", r"videoconsulta", r"teleatendimento", r"pronto atendimento digital", r"pronto atendimento virtual"],
    ),
    (
        "REQ_PEP_PRONTUARIO",
        "Producto Core",
        "Prontuário Eletrônico do Paciente (PEP / Historia Clínica Digital)",
        "ALTO",
        [r"prontuario eletr", r"\bpep\b", r"historia clinica", r"registro clinico eletr"],
    ),
    (
        "REQ_PRESCRICAO_ICP",
        "Producto / Regulatorio",
        "Prescrição Eletrônica con firma digital ICP-Brasil (Memed/CFM/VIDaaS)",
        "ALTO",
        [r"prescricao eletr", r"receita digital", r"receituario eletr", r"icp-brasil", r"assinatura digital", r"memed"],
    ),
    (
        "REQ_ESUS_RNDS_DATASUS",
        "Interoperabilidad SUS",
        "Integración e-SUS APS / PEC / RNDS / DATASUS / CADSUS / SIGTAP",
        "CRITICO",
        [r"\be-?sus\b", r"\brnds\b", r"\bdatasus\b", r"\bcadsus\b", r"\bsigtap\b", r"\bcnes\b", r"\bsia\b", r"\bbpa\b", r"\braas\b", r"\bhl7\b", r"\bfhir\b"],
    ),
    (
        "REQ_SBIS_CFM_NGS2",
        "Certificación Clínica",
        "Certificación SBIS-CFM (NGS1/NGS2) o resolución CFM 2.314/2022",
        "ALTO",
        [r"\bsbis\b", r"\bngs[12]\b", r"2\.?314/2022", r"resolucao cfm"],
    ),
    (
        "REQ_LGPD_SEGURANCA",
        "Seguridad / Privacidad",
        "Conformidad LGPD, cifrado, logs de auditoría y backups / SLA uptime",
        "ALTO",
        [r"\blgpd\b", r"13\.?709", r"protecao de dados", r"criptograf", r"trilha de auditoria", r"backup"],
    ),
    (
        "REQ_APP_MOBILE",
        "Producto / Canales",
        "App móvil Android/iOS o Portal Web responsivo paciente/médico",
        "MEDIO",
        [r"aplicativo movel", r"\bandroid\b", r"\bios\b", r"portal do paciente", r"app para paciente"],
    ),
    (
        "REQ_CRM_EQUIPE_MEDICA",
        "Red Médica / RQE",
        "Registro CRM PJ/PF y médicos habilitados (Clínica/Pediatría/Especialidades)",
        "CRITICO",
        [r"\bcrm\b", r"\brqe\b", r"responsavel tecnico medic", r"corpo clinico", r"equipe medica", r"prestacao de servicos medicos", r"consultas medicas", r"especialidades medicas", r"credenciamento de clinica", r"clinicas e medicos"],
    ),
    (
        "REQ_PLANTAO_24X7_SLA",
        "Operación Asistencial",
        "Cobertura asistencial 24/7 / turnos programados / SLA de espera",
        "ALTO",
        [r"24\s*x\s*7", r"24 horas", r"plantao", r"tempo de espera", r"nivel de servico", r"\bsla\b"],
    ),
    (
        "REQ_ESCALA_MEDICA",
        "Módulo Específico",
        "Gestión electrónica de escalas médicas y control de ponto/guardias",
        "MEDIO",
        [r"escala(?:s)? medica", r"controle de ponto", r"gestao de plantoes"],
    ),
    (
        "REQ_ANVISA_SAMD",
        "Regulatorio Dispositivos",
        "Registro ANVISA (RDC 657/2022 SaMD) o certificación clínica específica",
        "ALTO",
        [r"\banvisa\b", r"rdc\s*657", r"software como dispositivo medico"],
    ),
    (
        "REQ_CONSORCIO_SUBCONT",
        "Estrategia Contractual",
        "Reglas de Consórcio / Subcontratación / Credenciamento por terceros",
        "MEDIO",
        [r"\bconsorcio\b", r"subcontratac", r"\bcredenciamento\b", r"chamamento publico"],
    ),
]

CATALOG_BY_CODE = {item[0]: item for item in REQUIREMENT_CATALOG}


def fold(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower()


def _cell_str(row: list[Any], idx: int) -> str:
    if len(row) <= idx or row[idx] is None:
        return ""
    return str(row[idx]).strip()


def _parse_money(val: Any) -> Any:
    if val is None or val == "":
        return ""
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return ""
    s_clean = re.sub(r"[^\d.,-]", "", s)
    if "," in s_clean and "." in s_clean:
        s_clean = s_clean.replace(".", "").replace(",", ".") if s_clean.rfind(",") > s_clean.rfind(".") else s_clean.replace(",", "")
    elif "," in s_clean:
        s_clean = s_clean.replace(",", ".")
    try:
        return float(s_clean)
    except ValueError:
        return ""


def _sanitize_cell_text(val: str) -> str:
    s = re.sub(r"\s+", " ", (val or "").strip())
    if s.startswith(("=", "+", "-", "@")):
        s = "'" + s
    return s


def extract_url_from_cell(val: str) -> str:
    s = (val or "").strip()
    if not s:
        return ""
    m = re.search(r'HYPERLINK\(\s*"([^"]+)"', s, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    if s.startswith(("http://", "https://")):
        return s
    return ""


def pncp_url_from_id(pncp_id: str) -> str:
    m = re.match(r"^(\d{14})-1-(\d+)/(\d{4})$", (pncp_id or "").strip())
    if not m:
        return ""
    cnpj, seq, ano = m.group(1), int(m.group(2)), m.group(3)
    return f"https://pncp.gov.br/app/editais/{cnpj}/{ano}/{seq}"


def parse_pncp_id(pncp_id: str) -> tuple[str, int, int] | None:
    m = re.match(r"^(\d{14})-1-(\d+)/(\d{4})$", (pncp_id or "").strip())
    if not m:
        return None
    return m.group(1), int(m.group(3)), int(m.group(2))


def _score_doc_priority(doc: dict[str, Any]) -> tuple[int, int]:
    title = fold(doc.get("titulo") or "")
    tipo = fold(doc.get("tipoDocumentoNome") or "")
    url = (doc.get("url") or doc.get("uri") or "").lower()
    if not doc.get("statusAtivo", True):
        return (99, 0)
    is_pdf = 0 if (".pdf" in title or ".pdf" in url or "termo" in title or "edital" in title or "projeto" in title) else 1
    if "termo de referencia" in title or "termo_de_referencia" in title or re.search(r"\btr\b", title):
        return (is_pdf, 0)
    if "projeto basico" in title:
        return (is_pdf, 1)
    if "edital" in tipo or "edital" in title:
        return (is_pdf, 2)
    return (is_pdf, 3)


def fetch_pncp_tr_text(pncp_id: str) -> tuple[str, str, str]:
    """Fetch and parse highest-priority active TR/Edital PDF for a PNCP process."""
    parsed = parse_pncp_id(pncp_id)
    if not parsed:
        return "", "", ""
    cnpj, ano, seq = parsed
    api_url = f"https://pncp.gov.br/api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/arquivos"
    try:
        raw = subprocess.check_output(
            ["curl", "-sS", "--max-time", "15", "-H", f"User-Agent: {UA}", api_url],
            stderr=subprocess.DEVNULL,
        )
        docs = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(docs, list) or not docs:
            return "", "", ""
    except Exception:
        return "", "", ""

    active_docs = [d for d in docs if isinstance(d, dict) and d.get("statusAtivo", True) and (d.get("url") or d.get("uri"))]
    if not active_docs:
        return "", "", ""
    active_docs.sort(key=_score_doc_priority)

    from pypdf import PdfReader

    for doc in active_docs[:3]:
        doc_url = (doc.get("url") or doc.get("uri") or "").strip()
        title = (doc.get("titulo") or doc.get("tipoDocumentoNome") or "Documento PNCP").strip()
        if not doc_url:
            continue
        try:
            pdf_bytes = subprocess.check_output(
                ["curl", "-sSL", "--max-time", "25", "--max-filesize", "6291456", "-H", f"User-Agent: {UA}", doc_url],
                stderr=subprocess.DEVNULL,
            )
            if not pdf_bytes.startswith(b"%PDF"):
                continue
            reader = PdfReader(io.BytesIO(pdf_bytes))
            parts: list[str] = []
            total_chars = 0
            for page in reader.pages[:35]:
                txt = page.extract_text() or ""
                if txt:
                    parts.append(txt)
                    total_chars += len(txt)
                    if total_chars >= 120000:
                        break
            full_text = "\n".join(parts).strip()
            if len(full_text) >= 120:
                return title, doc_url, full_text
        except Exception:
            continue
    return "", "", ""


def _snippet_around(raw_text: str, folded_text: str, pattern: str) -> str:
    m = re.search(pattern, folded_text)
    if not m:
        return ""
    start = max(0, m.start() - 45)
    end = min(len(raw_text), m.end() + 85)
    snippet = raw_text[start:end] if len(raw_text) == len(folded_text) else folded_text[start:end]
    snippet = re.sub(r"\s+", " ", snippet).strip(" .,:;-")
    if start > 0:
        snippet = "..." + snippet
    if end < len(folded_text):
        snippet = snippet + "..."
    return _sanitize_cell_text(snippet[:165])


def extract_requirements(text: str, is_pncp: bool = True) -> list[dict[str, str]]:
    """Extract standardized B2G requirements + verbatim snippets from TR/Edital or object text."""
    raw_clean = re.sub(r"\s+", " ", text or "").strip()
    folded_chars: list[str] = []
    raw_aligned: list[str] = []
    for ch in raw_clean:
        norm = unicodedata.normalize("NFKD", ch)
        stripped = "".join(c for c in norm if not unicodedata.combining(c))
        if stripped:
            folded_chars.append(stripped[0].lower())
            raw_aligned.append(ch)
    raw_str = "".join(raw_aligned)
    folded_str = "".join(folded_chars)

    found: list[dict[str, str]] = []
    seen_codes: set[str] = set()

    for code, cat, label, weight, patterns in REQUIREMENT_CATALOG:
        for pat in patterns:
            if re.search(pat, folded_str):
                snip = _snippet_around(raw_str, folded_str, pat)
                found.append(
                    {
                        "code": code,
                        "cat": cat,
                        "label": label,
                        "weight": weight,
                        "evidence": snip or _sanitize_cell_text(raw_clean[:140]),
                    }
                )
                seen_codes.add(code)
                break

    if is_pncp:
        for base_code in ("REQ_CNPJ_CNAE_SW", "REQ_CNDS_SICAF"):
            if base_code not in seen_codes:
                _, cat, label, weight, _ = CATALOG_BY_CODE[base_code]
                found.append(
                    {
                        "code": base_code,
                        "cat": cat,
                        "label": label,
                        "weight": weight,
                        "evidence": "Habilitação jurídica/fiscal padrão Lei 14.133/2021 (PNCP)",
                    }
                )
                seen_codes.add(base_code)

    if not found:
        _, cat, label, weight, _ = CATALOG_BY_CODE["REQ_TELEMEDICINA_VIDEO"]
        found.append(
            {
                "code": "REQ_TELEMEDICINA_VIDEO",
                "cat": cat,
                "label": label,
                "weight": weight,
                "evidence": _sanitize_cell_text(raw_clean[:150]) or "Objeto de telessaúde / software sanitário",
            }
        )

    order = {item[0]: idx for idx, item in enumerate(REQUIREMENT_CATALOG)}
    found.sort(key=lambda r: order.get(r["code"], 999))
    return found


def _ensure_profile_tab(sh: Any, tabs: dict[str, Any], now_utc: str) -> dict[str, dict[str, str]]:
    """Ensure Mi_Perfil_Capacidades exists without overwriting user-edited capabilities."""
    if "Mi_Perfil_Capacidades" not in tabs:
        ws_prof = sh.add_worksheet(title="Mi_Perfil_Capacidades", rows=60, cols=7)
        rows = [
            ["MI PERFIL DE CAPACIDADES B2G — 'LO QUE TENGO' (EDITABLE POR EL USUARIO)"],
            [f"Inicializado automáticamente: {now_utc} · Edita Mi_Estado (TENGO / EN_CURSO / VIA_PARCEIRO_OEM / NO_TENGO) y se recalcula toda la matriz."],
            [""],
            PROFILE_HEADERS,
        ]
        for code, cat, label, weight, _ in REQUIREMENT_CATALOG:
            rows.append([code, cat, label, "SIN_EVALUAR", "Completar evidencia real", "Definir acción de cierre", weight])
        ws_prof.update(range_name="A1", values=rows, value_input_option="USER_ENTERED")
        tabs["Mi_Perfil_Capacidades"] = ws_prof
    else:
        ws_prof = tabs["Mi_Perfil_Capacidades"]

    vals = ws_prof.get_all_values()
    if len(vals) < 4:
        ws_prof.update(range_name="A4:G4", values=[PROFILE_HEADERS], value_input_option="USER_ENTERED")
        vals = ws_prof.get_all_values()

    profile: dict[str, dict[str, str]] = {}
    for row in vals[4:]:
        code = (row[0] if len(row) > 0 else "").strip()
        if not code:
            continue
        profile[code] = {
            "cat": (row[1] if len(row) > 1 else "").strip(),
            "label": (row[2] if len(row) > 2 else "").strip(),
            "estado": (row[3] if len(row) > 3 else "").strip() or "SIN_EVALUAR",
            "evidencia": (row[4] if len(row) > 4 else "").strip(),
            "accion": (row[5] if len(row) > 5 else "").strip(),
            "peso": (row[6] if len(row) > 6 else "").strip(),
        }

    missing_rows = []
    for code, cat, label, weight, _ in REQUIREMENT_CATALOG:
        if code not in profile:
            missing_rows.append([code, cat, label, "SIN_EVALUAR", "Completar evidencia real", "Definir acción de cierre", weight])
            profile[code] = {
                "cat": cat,
                "label": label,
                "estado": "SIN_EVALUAR",
                "evidencia": "Completar evidencia real",
                "accion": "Definir acción de cierre",
                "peso": weight,
            }
    if missing_rows:
        ws_prof.append_rows(missing_rows, value_input_option="USER_ENTERED")

    return profile


def _read_pdf_cache(tabs: dict[str, Any]) -> tuple[dict[str, list[dict[str, str]]], dict[str, str]]:
    """Recover already-audited PDF requirements and official document URLs from Sheets."""
    cached_reqs: dict[str, list[dict[str, str]]] = {}
    cached_links: dict[str, str] = {}

    if "Matriz_Gap_Licitaciones" in tabs:
        try:
            gap_formulas = tabs["Matriz_Gap_Licitaciones"].get_all_values(value_render_option="FORMULA")
            for row in gap_formulas[4:]:
                pid = (row[0] if len(row) > 0 else "").strip()
                link = extract_url_from_cell(row[14] if len(row) > 14 else "")
                if pid and link:
                    cached_links[pid] = link
        except Exception:
            pass

    if "Matriz_Requisitos_Detalle" in tabs:
        try:
            det_vals = tabs["Matriz_Requisitos_Detalle"].get_all_values()
            temp: dict[str, list[dict[str, str]]] = {}
            for row in det_vals[4:]:
                pid = (row[0] if len(row) > 0 else "").strip()
                code = (row[2] if len(row) > 2 else "").strip()
                fuente = (row[10] if len(row) > 10 else "").strip()
                if not pid or not code or code not in CATALOG_BY_CODE:
                    continue
                if fuente.startswith("PDF:"):
                    _, cat, label, weight, _ = CATALOG_BY_CODE[code]
                    temp.setdefault(pid, []).append(
                        {
                            "code": code,
                            "cat": cat,
                            "label": label,
                            "weight": weight,
                            "evidence": (row[5] if len(row) > 5 else "").strip(),
                            "fuente": fuente,
                        }
                    )
            cached_reqs = temp
        except Exception:
            pass

    return cached_reqs, cached_links


def _build_gap_summary_text(reqs: list[dict[str, str]], profile: dict[str, dict[str, str]]) -> str:
    missing: list[str] = []
    in_progress: list[str] = []
    oem: list[str] = []
    for r in reqs:
        code = r["code"]
        st = (profile.get(code, {}).get("estado") or "SIN_EVALUAR").upper()
        short_name = code.replace("REQ_", "")
        if st == "NO_TENGO":
            missing.append(short_name)
        elif st in ("EN_CURSO", "SIN_EVALUAR"):
            in_progress.append(short_name)
        elif st == "VIA_PARCEIRO_OEM":
            oem.append(short_name)
    parts: list[str] = []
    if missing:
        parts.append("FALTA: " + ", ".join(missing[:4]))
    if in_progress:
        parts.append("EN CURSO: " + ", ".join(in_progress[:4]))
    if oem:
        parts.append("OEM: " + ", ".join(oem[:3]))
    return " | ".join(parts) if parts else "Cobertura completa con perfil actual"


def _apply_safe_formatting(sh: Any, tabs: dict[str, Any], gap_rows: int, det_rows: int) -> None:
    """Apply clean header formatting, freeze panes, and percentage formatting cleanly."""
    try:
        requests: list[dict[str, Any]] = []
        sheet_specs = [
            ("Mi_Perfil_Capacidades", 7, False),
            ("Matriz_Requisitos_Detalle", 11, False),
            ("Matriz_Gap_Licitaciones", 15, True),
        ]
        for title, col_cnt, pct_col_l in sheet_specs:
            ws = tabs.get(title)
            if not ws:
                continue
            sid = ws.id
            requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 4}},
                        "fields": "gridProperties.frozenRowCount",
                    }
                }
            )
            requests.append(
                {
                    "repeatCell": {
                        "range": {"sheetId": sid, "startRowIndex": 3, "endRowIndex": 4, "startColumnIndex": 0, "endColumnIndex": col_cnt},
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.11, "green": 0.24, "blue": 0.44},
                                "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True},
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat)",
                    }
                }
            )
            if pct_col_l and gap_rows > 4:
                requests.append(
                    {
                        "repeatCell": {
                            "range": {"sheetId": sid, "startRowIndex": 4, "endRowIndex": gap_rows, "startColumnIndex": 11, "endColumnIndex": 12},
                            "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}},
                            "fields": "userEnteredFormat.numberFormat",
                        }
                    }
                )
        if requests:
            sh.batch_update({"requests": requests})
    except Exception:
        pass


def sync_compliance_matrix(
    sh: Any,
    tabs: dict[str, Any],
    now_utc: str,
    op_rows_fmt: list[list[Any]],
    tce_rows_fmt: list[list[Any]],
    op_rows_fml: list[list[Any]] | None = None,
    tce_rows_fml: list[list[Any]] | None = None,
    max_pdf_per_run: int = 18,
) -> dict[str, int]:
    """Build/refresh Matriz_Requisitos_Detalle and Matriz_Gap_Licitaciones from live data."""
    profile = _ensure_profile_tab(sh, tabs, now_utc)
    pdf_cache, link_cache = _read_pdf_cache(tabs)

    tenders: list[dict[str, Any]] = []
    seen_pids: set[str] = set()
    for i, row in enumerate(op_rows_fmt):
        fml = op_rows_fml[i] if (op_rows_fml and i < len(op_rows_fml)) else row
        pid = _cell_str(row, 6)
        if not parse_pncp_id(pid) or pid in seen_pids:
            continue
        seen_pids.add(pid)
        prio = _cell_str(row, 0) or "P2"
        uf = _cell_str(row, 2)
        orgao = _cell_str(row, 4)
        proceso = _cell_str(row, 5)
        val_num = _parse_money(fml[8] if len(fml) > 8 else (row[8] if len(row) > 8 else ""))
        cierre = _cell_str(row, 9)
        motivo = _cell_str(row, 12)
        raw_link = extract_url_from_cell(_cell_str(fml, 13)) or pncp_url_from_id(pid)
        tenders.append(
            {
                "pid": pid,
                "is_pncp": True,
                "prio": prio,
                "uf": uf,
                "orgao": orgao,
                "proceso": proceso,
                "valor": val_num,
                "cierre": cierre,
                "fallback_text": f"{proceso} {motivo}",
                "default_link": raw_link,
            }
        )

    for i, row in enumerate(tce_rows_fmt):
        fml = tce_rows_fml[i] if (tce_rows_fml and i < len(tce_rows_fml)) else row
        rid = _cell_str(row, 0)
        if not rid.startswith("TCERS-") or rid in seen_pids:
            continue
        seen_pids.add(rid)
        orgao = _cell_str(row, 1)
        proceso = f"{_cell_str(row, 2)} {_cell_str(row, 3)}".strip()
        fim_cred = _cell_str(row, 6) or _cell_str(row, 5)
        val_num = _parse_money(fml[7] if len(fml) > 7 else (row[7] if len(row) > 7 else ""))
        objeto = _cell_str(row, 8)
        raw_link = extract_url_from_cell(_cell_str(fml, 10))
        tenders.append(
            {
                "pid": rid,
                "is_pncp": False,
                "prio": "P1",
                "uf": "RS",
                "orgao": orgao,
                "proceso": proceso,
                "valor": val_num,
                "cierre": fim_cred,
                "fallback_text": f"{proceso} {objeto}",
                "default_link": raw_link,
            }
        )

    # Select uncached PNCP tenders for official PDF audit in this run
    uncached_pncp = [t for t in tenders if t["is_pncp"] and t["pid"] not in pdf_cache]
    uncached_pncp.sort(
        key=lambda t: (
            0 if t["prio"] == "P1" else 1,
            -(float(t["valor"]) if isinstance(t["valor"], (int, float)) else 0.0),
        )
    )
    batch_to_audit = uncached_pncp[:max_pdf_per_run]

    fetched_pdfs: dict[str, tuple[str, str, str]] = {}
    if batch_to_audit:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            future_map = {ex.submit(fetch_pncp_tr_text, t["pid"]): t["pid"] for t in batch_to_audit}
            for fut in concurrent.futures.as_completed(future_map):
                pid = future_map[fut]
                try:
                    fetched_pdfs[pid] = fut.result()
                except Exception:
                    fetched_pdfs[pid] = ("", "", "")

    detail_rows: list[list[Any]] = [
        ["MATRIZ DE REQUISITOS EXIGIDOS VS. PERFIL ('LO QUE EXIGEN' VS. 'LO QUE TENGO')"],
        [f"Actualizado: {now_utc} · Columnas G:J usan fórmulas vivas contra Mi_Perfil_Capacidades!$A$5:$G$40."],
        [""],
        DETAIL_HEADERS,
    ]

    gap_rows: list[list[Any]] = [
        ["MATRIZ EJECUTIVA DE GAP B2G — COBERTURA Y DICTAMEN AUTOMÁTICO POR LICITACIÓN"],
        [f"Actualizado: {now_utc} · Edita Mi_Estado en Mi_Perfil_Capacidades y todas las coberturas/veredictos se recalculan al instante."],
        [""],
        GAP_HEADERS,
    ]

    pdfs_audited_now = 0
    pdfs_cached = 0

    for t in tenders:
        pid = t["pid"]
        doc_link = link_cache.get(pid) or t["default_link"]

        if pid in pdf_cache:
            reqs = pdf_cache[pid]
            fuente_analisis = reqs[0].get("fuente", "PDF Cache")
            pdfs_cached += 1
        elif pid in fetched_pdfs and fetched_pdfs[pid][2]:
            doc_title, doc_url, full_txt = fetched_pdfs[pid]
            reqs = extract_requirements(full_txt, is_pncp=True)
            fuente_analisis = f"PDF: {doc_title[:42]}"
            doc_link = doc_url or doc_link
            pdfs_audited_now += 1
            for r in reqs:
                r["fuente"] = fuente_analisis
        else:
            reqs = extract_requirements(t["fallback_text"], is_pncp=t["is_pncp"])
            fuente_analisis = "Objeto PNCP" if t["is_pncp"] else "Objeto TCE-RS"
            for r in reqs:
                r["fuente"] = fuente_analisis

        for req in reqs:
            r_idx = len(detail_rows) + 1
            code = req["code"]
            mi_estado_f = f'=IFERROR(VLOOKUP(C{r_idx},Mi_Perfil_Capacidades!$A$5:$G$40,4,FALSE),"SIN_EVALUAR")'
            lo_que_tengo_f = f'=IFERROR(VLOOKUP(C{r_idx},Mi_Perfil_Capacidades!$A$5:$G$40,5,FALSE),"Definir en Mi_Perfil_Capacidades")'
            brecha_f = (
                f'=IF(G{r_idx}="TENGO","OK — Cubierto",'
                f'IFERROR(VLOOKUP(C{r_idx},Mi_Perfil_Capacidades!$A$5:$G$40,6,FALSE),"Completar acción"))'
            )
            peso_f = f'=IFERROR(VLOOKUP(C{r_idx},Mi_Perfil_Capacidades!$A$5:$G$40,7,FALSE),"{req["weight"]}")'

            detail_rows.append(
                [
                    pid,
                    _sanitize_cell_text(f"{t['orgao']} ({t['proceso']})"[:85]),
                    code,
                    req["cat"],
                    req["label"],
                    req["evidence"],
                    mi_estado_f,
                    lo_que_tengo_f,
                    brecha_f,
                    peso_f,
                    _sanitize_cell_text(req.get("fuente", fuente_analisis)),
                ]
            )

        g_idx = len(gap_rows) + 1
        resumen_falta = _build_gap_summary_text(reqs, profile)
        safe_url = (doc_link or "").replace('"', "")
        link_formula = f'=HYPERLINK("{safe_url}","Documento / Edital")' if safe_url.startswith("http") else ""

        total_f = f"=COUNTIF(Matriz_Requisitos_Detalle!$A$5:$A$3000,A{g_idx})"
        tengo_f = f'=COUNTIFS(Matriz_Requisitos_Detalle!$A$5:$A$3000,A{g_idx},Matriz_Requisitos_Detalle!$G$5:$G$3000,"TENGO")'
        parcial_f = (
            f'=COUNTIFS(Matriz_Requisitos_Detalle!$A$5:$A$3000,A{g_idx},Matriz_Requisitos_Detalle!$G$5:$G$3000,"EN_CURSO")'
            f'+COUNTIFS(Matriz_Requisitos_Detalle!$A$5:$A$3000,A{g_idx},Matriz_Requisitos_Detalle!$G$5:$G$3000,"VIA_PARCEIRO_OEM")'
        )
        notengo_f = f'=COUNTIFS(Matriz_Requisitos_Detalle!$A$5:$A$3000,A{g_idx},Matriz_Requisitos_Detalle!$G$5:$G$3000,"NO_TENGO")'
        pct_f = f"=IF(H{g_idx}=0,0,ROUND((I{g_idx}+0.5*J{g_idx})/H{g_idx},3))"
        veredicto_f = (
            f'=IF(COUNTIFS(Matriz_Requisitos_Detalle!$A$5:$A$3000,A{g_idx},'
            f'Matriz_Requisitos_Detalle!$G$5:$G$3000,"NO_TENGO",Matriz_Requisitos_Detalle!$J$5:$J$3000,"CRITICO")>0,'
            f'"BLOQUEO CRÍTICO (Vía OEM/Consorcio)",'
            f'IF(K{g_idx}>0,"GAP PARCIAL (Cerrar NO_TENGO)",'
            f'IF(COUNTIFS(Matriz_Requisitos_Detalle!$A$5:$A$3000,A{g_idx},Matriz_Requisitos_Detalle!$G$5:$G$3000,"VIA_PARCEIRO_OEM")>0,'
            f'"GO CON PARCEIRO OEM",IF(L{g_idx}>=0.7,"GO / ALTA COBERTURA","EN PREPARACIÓN"))))'
        )

        gap_rows.append(
            [
                pid,
                t["uf"],
                _sanitize_cell_text(t["orgao"][:60]),
                _sanitize_cell_text(t["proceso"][:55]),
                t["valor"],
                t["cierre"],
                _sanitize_cell_text(fuente_analisis),
                total_f,
                tengo_f,
                parcial_f,
                notengo_f,
                pct_f,
                veredicto_f,
                _sanitize_cell_text(resumen_falta),
                link_formula,
            ]
        )

    # Write Matriz_Requisitos_Detalle
    if "Matriz_Requisitos_Detalle" not in tabs:
        ws_det = sh.add_worksheet(title="Matriz_Requisitos_Detalle", rows=max(len(detail_rows) + 100, 600), cols=11)
        tabs["Matriz_Requisitos_Detalle"] = ws_det
    else:
        ws_det = tabs["Matriz_Requisitos_Detalle"]
        if ws_det.row_count < len(detail_rows) + 50:
            ws_det.resize(rows=len(detail_rows) + 200, cols=11)
        ws_det.clear()
    ws_det.update(range_name="A1", values=detail_rows, value_input_option="USER_ENTERED")

    # Write Matriz_Gap_Licitaciones
    if "Matriz_Gap_Licitaciones" not in tabs:
        ws_gap = sh.add_worksheet(title="Matriz_Gap_Licitaciones", rows=max(len(gap_rows) + 80, 300), cols=15)
        tabs["Matriz_Gap_Licitaciones"] = ws_gap
    else:
        ws_gap = tabs["Matriz_Gap_Licitaciones"]
        if ws_gap.row_count < len(gap_rows) + 50:
            ws_gap.resize(rows=len(gap_rows) + 150, cols=15)
        ws_gap.clear()
    ws_gap.update(range_name="A1", values=gap_rows, value_input_option="USER_ENTERED")

    _apply_safe_formatting(sh, tabs, len(gap_rows), len(detail_rows))

    return {
        "tenders_scored": len(gap_rows) - 4,
        "detail_rows": len(detail_rows) - 4,
        "pdfs_audited_now": pdfs_audited_now,
        "pdfs_cached": pdfs_cached,
    }
