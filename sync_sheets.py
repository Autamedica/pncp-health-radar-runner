#!/usr/bin/env python3
"""
PNCP & AR Public Health Procurement Radar Sync (Code-Only Runner)
Polls live public procurement APIs (Brasil PNCP, TCE-RS CKAN, Boletin Oficial AR)
and syncs updates/new tenders directly into a private Google Sheet via Secrets.
"""

from __future__ import annotations

import concurrent.futures
import csv
import datetime as dt
import html
import io
import json
import os
import re
import struct
import subprocess
import unicodedata
import urllib.parse
import zlib
from pathlib import Path
from typing import Any

import compliance_engine

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"

NATIONAL_QUERIES = (
    "telessaude",
    "telemedicina",
    "teleconsulta",
    "teleinterconsulta",
    "prontuario eletronico",
    "software saude",
    "sistema gestao saude",
)
SOUTH_UFS = ("RS", "SC", "PR")
SOUTH_QUERIES = ("telessaude", "telemedicina", "teleconsulta", "software saude")

TECH_HEALTH_KWS = (
    "telessaud",
    "telemedic",
    "teleconsult",
    "teleinterconsult",
    "prontuario eletr",
    "saude digital",
    "teleatendimento",
    "pronto atendimento digital",
    "pronto atendimento virtual",
    "e-sus",
    "teleorienta",
    "teletriagem",
    "plataforma de saude",
    "sistema de saude",
    "sistema de gestao de saude",
    "sistema de gestao em saude",
    "software de gestao",
    "software em saude",
    "licenciamento de sistema",
    "escala medica",
)
HEALTH_CONTEXT_KWS = (
    "saude",
    "medic",
    "clinic",
    "hospital",
    "prontuar",
    "sus",
    "telessaud",
    "telemedic",
    "teleconsult",
    "paciente",
    "ubs",
    "aps",
)
NOISE_KWS = (
    "veterinar",
    "saude financeira",
    "material de expediente",
    "grafic",
    "ambulancia",
    "equipamentos odontologicos",
    "home care",
    "medicamento",
    "reagente",
    "ponto eletronico",
    "registrador eletronico de ponto",
    "gestao de ponto",
)

OP_HEADERS = [
    "Prioridad",
    "Track / Enfoque",
    "UF",
    "Municipio",
    "Órgano / Consórcio",
    "Proceso / Edital",
    "ID PNCP",
    "Modalidad",
    "Valor Estimado (R$)",
    "Cierre / Vigencia (BRT)",
    "Situación Oficial PNCP",
    "Dictamen",
    "Motivo / Brecha Clave",
    "Enlace PNCP",
]

TCERS_HEADERS = [
    "ID TCE-RS",
    "Órgano (RS)",
    "Modalidad",
    "Proceso",
    "Fase Actual",
    "Abertura",
    "Fim Credenciamento",
    "Valor Estimado (R$)",
    "Objeto",
    "Estado / Dictamen",
    "Link LicitaCon",
]

TCERS_TECH_KWS = (
    "telessaud",
    "telemedic",
    "teleconsult",
    "teleinterconsult",
    "prontuario eletr",
    "saude digital",
    "teleatendimento",
    "sistema de gestao de saude",
    "sistema de gestao em saude",
    "software de saude",
    "software em saude",
    "aplicativo de telemedicina",
)

TCERS_NOISE_KWS = (
    "cadeiras ergonomicas",
    "telemedicao",
    "aquisicao de equipamentos",
    "equipamentos medico-hospitalares",
    "veterinar",
    "ambulancia",
    "medicamento",
    "reagente",
    "home care",
)


def fold(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch)).lower()


def curl_get(url: str, accept: str = "application/json", timeout_s: int = 10) -> tuple[int, str]:
    proc = subprocess.run(
        [
            "curl",
            "-sS",
            "--http2",
            "--max-time",
            str(timeout_s),
            "-w",
            "\n__HTTP_CODE__:%{http_code}",
            "-H",
            f"User-Agent: {UA}",
            "-H",
            f"Accept: {accept}",
            url,
        ],
        capture_output=True,
        text=True,
    )
    if "__HTTP_CODE__:" not in proc.stdout:
        return 0, proc.stderr.strip()
    body, code_part = proc.stdout.rsplit("__HTTP_CODE__:", 1)
    try:
        code = int(code_part.strip())
    except ValueError:
        code = 0
    return code, body.strip()


def curl_json(url: str, timeout_s: int = 10) -> tuple[int, Any]:
    code, body = curl_get(url, accept="application/json", timeout_s=timeout_s)
    if code == 200 and body and body[0] in "{[":
        try:
            return code, json.loads(body)
        except json.JSONDecodeError:
            return code, None
    return code, None


def is_relevant(item: dict[str, Any]) -> bool:
    if item.get("cancelado"):
        return False
    blob = fold(f"{item.get('title') or ''} {item.get('description') or ''}")
    if any(n in blob for n in NOISE_KWS):
        return False
    return any(k in blob for k in TECH_HEALTH_KWS) and any(h in blob for h in HEALTH_CONTEXT_KWS)


def classify_item(item: dict[str, Any]) -> tuple[str, str, str]:
    uf = (item.get("uf") or "").upper()
    prio = "P1" if uf in SOUTH_UFS else "P2"
    desc_f = fold(item.get("description") or "")
    has_sw = any(k in desc_f for k in ("software", "saas", "sistema", "prontuario", "licenca", "licenciamento", "plataforma", "aplicativo", "e-sus"))
    has_clin = any(k in desc_f for k in ("medico", "medicos", "plantao", "especialista", "prestacao de servicos de saude", "profissionais", "consultas medicas", "teleconsulta"))
    if has_sw and not has_clin:
        return prio, "Software / SaaS Salud", "NUEVO — Revisar (Software/SaaS)"
    if has_sw and has_clin:
        return prio, "Track 2 OEM / Híbrido SaaS+Med", "NUEVO — Revisar (Track 2 OEM)"
    return prio, "Track 2 OEM / Socio Clínico", "NUEVO — Revisar (Track 2 OEM)"


def fetch_pncp_detail(cid: str) -> dict[str, Any] | None:
    m = re.match(r"^(\d{14})-1-(\d+)/(\d{4})$", (cid or "").strip())
    if not m:
        return None
    cnpj, seq_raw, ano = m.group(1), m.group(2), m.group(3)
    seq = str(int(seq_raw))
    url = f"https://pncp.gov.br/api/consulta/v1/orgaos/{cnpj}/compras/{ano}/{seq}"
    code, data = curl_json(url, timeout_s=7)
    return data if (code == 200 and isinstance(data, dict)) else None


def fetch_details_parallel(cids: list[str], max_workers: int = 8) -> dict[str, dict[str, Any] | None]:
    unique = [c for c in dict.fromkeys(cids) if c]
    if not unique:
        return {}
    out: dict[str, dict[str, Any] | None] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        fmap = {ex.submit(fetch_pncp_detail, cid): cid for cid in unique}
        for fut in concurrent.futures.as_completed(fmap):
            cid = fmap[fut]
            try:
                out[cid] = fut.result()
            except Exception:
                out[cid] = None
    return out


def poll_pncp() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    jobs: list[dict[str, Any]] = [
        {"q": q, "tipos_documento": "edital", "status": "recebendo_proposta", "tam_pagina": 40, "pagina": 1}
        for q in NATIONAL_QUERIES
    ]
    for uf in SOUTH_UFS:
        for q in SOUTH_QUERIES:
            jobs.append({"q": q, "tipos_documento": "edital", "status": "recebendo_proposta", "ufs": uf, "tam_pagina": 25, "pagina": 1})

    def _run(params: dict[str, Any]) -> tuple[int, Any]:
        return curl_json("https://pncp.gov.br/api/search/?" + urllib.parse.urlencode(params), timeout_s=10)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(_run, jobs))

    seen: dict[str, dict[str, Any]] = {}
    ok_calls = 0
    for code, data in results:
        if code == 200 and isinstance(data, dict):
            ok_calls += 1
            for it in data.get("items") or []:
                cid = (it.get("numero_controle_pncp") or "").strip()
                if cid and is_relevant(it) and cid not in seen:
                    seen[cid] = it

    return seen, {"ok_calls": ok_calls, "total_calls": len(jobs), "unique_relevant": len(seen)}


def check_secondary() -> dict[str, Any]:
    code_tce, data_tce = curl_json("https://dados.tce.rs.gov.br/api/3/action/package_search?q=licitacao&rows=2", timeout_s=9)
    tce_ok = bool(code_tce == 200 and isinstance(data_tce, dict) and data_tce.get("success"))

    code_bo, html_bo = curl_get("https://www.boletinoficial.gob.ar/seccion/tercera", accept="text/html", timeout_s=10)
    bo_hits: dict[str, int] = {}
    if code_bo == 200 and html_bo:
        plain = fold(re.sub(r"<[^>]+>", " ", html.unescape(html_bo)))
        for kw in ("telemedicina", "telesalud", "teleconsulta", "historia clinica", "guardia virtual"):
            cnt = plain.count(kw)
            if cnt > 0:
                bo_hits[kw] = cnt

    return {"tce_rs_http": code_tce, "tce_rs_ok": tce_ok, "bo_ar_http": code_bo, "bo_ar_hits": bo_hits}


def poll_tce_rs_2026(today_iso: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    url = "https://dados.tce.rs.gov.br/dados/licitacon/licitacao/ano/2026.csv.zip"
    try:
        hdr_proc = subprocess.run(
            ["curl", "-sSI", "--max-time", "12", "-H", f"User-Agent: {UA}", url],
            capture_output=True,
            text=True,
        )
        if hdr_proc.returncode != 0:
            return [], {"ok": False, "error": f"HEAD rc={hdr_proc.returncode}", "active_hits": 0}
        total_len = 0
        last_mod = ""
        for line in hdr_proc.stdout.splitlines():
            low = line.lower()
            if low.startswith("content-length:"):
                total_len = int(line.split(":", 1)[1].strip())
            elif low.startswith("last-modified:"):
                last_mod = line.split(":", 1)[1].strip()
        if total_len < 16384:
            return [], {"ok": False, "error": "Missing Content-Length", "active_hits": 0}

        tail_start = max(0, total_len - 8192)
        tail = subprocess.check_output(
            ["curl", "-sS", "--max-time", "15", "-r", f"{tail_start}-{total_len - 1}", url]
        )
        eocd = tail.rfind(b"PK\x05\x06")
        if eocd == -1:
            return [], {"ok": False, "error": "ZIP EOCD not found", "active_hits": 0}
        cd_size, cd_offset = struct.unpack("<II", tail[eocd + 12 : eocd + 20])
        cd = subprocess.check_output(
            ["curl", "-sS", "--max-time", "15", "-r", f"{cd_offset}-{cd_offset + cd_size - 1}", url]
        )

        pos = 0
        target: tuple[int, int] | None = None
        while pos < len(cd) and cd[pos : pos + 4] == b"PK\x01\x02":
            comp_sz = struct.unpack("<I", cd[pos + 20 : pos + 24])[0]
            fn_len, ex_len, cm_len = struct.unpack("<HHH", cd[pos + 28 : pos + 34])
            local_off = struct.unpack("<I", cd[pos + 42 : pos + 46])[0]
            fname = cd[pos + 46 : pos + 46 + fn_len].decode("utf-8", errors="replace")
            if fname == "licitacao.csv":
                target = (local_off, comp_sz)
                break
            pos += 46 + fn_len + ex_len + cm_len
        if target is None:
            return [], {"ok": False, "error": "licitacao.csv not found in ZIP", "active_hits": 0}

        local_off, comp_sz = target
        lfh = subprocess.check_output(
            ["curl", "-sS", "--max-time", "12", "-r", f"{local_off}-{local_off + 29}", url]
        )
        l_fn, l_ex = struct.unpack("<HH", lfh[26:30])
        data_start = local_off + 30 + l_fn + l_ex
        data_end = data_start + comp_sz - 1

        n_chunks = 12
        chunk_sz = (comp_sz + n_chunks - 1) // n_chunks
        ranges = [
            (data_start + i * chunk_sz, min(data_end, data_start + (i + 1) * chunk_sz - 1))
            for i in range(n_chunks)
        ]

        def _fetch_chunk(rng: tuple[int, int]) -> bytes:
            return subprocess.check_output(
                [
                    "curl",
                    "-sS",
                    "--http1.1",
                    "--retry",
                    "3",
                    "--retry-delay",
                    "2",
                    "--retry-all-errors",
                    "--max-time",
                    "90",
                    "-r",
                    f"{rng[0]}-{rng[1]}",
                    url,
                ]
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            comp_data = b"".join(ex.map(_fetch_chunk, ranges))

        raw_csv = zlib.decompress(comp_data, -zlib.MAX_WBITS)
        cutoff_30d = (dt.date.fromisoformat(today_iso) - dt.timedelta(days=30)).isoformat()

        reader = csv.DictReader(io.TextIOWrapper(io.BytesIO(raw_csv), encoding="utf-8-sig", errors="replace"))
        dedup: dict[str, dict[str, Any]] = {}
        total_rows = 0
        for row in reader:
            total_rows += 1
            obj_raw = (row.get("DS_OBJETO") or "").strip()
            obj_f = fold(obj_raw)
            if not any(k in obj_f for k in TCERS_TECH_KWS):
                continue
            if any(n in obj_f for n in TCERS_NOISE_KWS):
                continue

            dt_hom = (row.get("DT_HOMOLOGACAO") or "").strip()
            if dt_hom:
                continue

            fase = (row.get("CD_TIPO_FASE_ATUAL") or "").strip().upper()
            dt_ab = (row.get("DT_ABERTURA") or "").strip()
            dt_fim_inscr = (row.get("DT_FIM_INSCR_CRED") or "").strip()
            dt_fim_vig = (row.get("DT_FIM_VIGEN_CRED") or "").strip()

            is_active = (
                fase in ("EPU", "PUB", "HAP", "ABE")
                or dt_fim_inscr >= today_iso
                or dt_fim_vig >= today_iso
                or dt_ab >= cutoff_30d
            )
            if not is_active:
                continue

            cd_orgao = (row.get("CD_ORGAO") or "").strip()
            mod = (row.get("CD_TIPO_MODALIDADE") or "").strip()
            nr = (row.get("NR_LICITACAO") or "").strip()
            ano = (row.get("ANO_LICITACAO") or "").strip()
            rid = f"TCERS-{cd_orgao}-{mod}-{nr}/{ano}"

            vl_raw = (row.get("VL_LICITACAO") or "").strip()
            try:
                vl_num: Any = float(vl_raw) if vl_raw else ""
            except ValueError:
                vl_num = ""

            fim_cred = dt_fim_inscr or dt_fim_vig
            dedup[rid] = {
                "id": rid,
                "orgao": (row.get("NM_ORGAO") or "").strip(),
                "mod": mod,
                "proceso": f"{nr}/{ano}",
                "fase": fase,
                "abertura": dt_ab,
                "fim_cred": fim_cred,
                "valor": vl_num,
                "objeto": re.sub(r"\s+", " ", obj_raw)[:280],
                "link": (row.get("LINK_LICITACON_CIDADAO") or "").strip(),
            }

        items = sorted(dedup.values(), key=lambda x: (x["fim_cred"] or "", x["abertura"] or ""), reverse=True)
        return items, {
            "ok": True,
            "last_modified": last_mod,
            "total_rows_scanned": total_rows,
            "active_hits": len(items),
        }
    except Exception as exc:
        return [], {"ok": False, "error": f"{type(exc).__name__}: {exc}", "active_hits": 0}


def fmt_dt(val: str | None) -> str:
    return val[:16].replace("T", " ") if val else ""


def build_pncp_url(item: dict[str, Any]) -> str:
    return f"https://pncp.gov.br/app/editais/{item.get('orgao_cnpj')}/{item.get('ano')}/{item.get('numero_sequencial')}"


def sync_tce_rs_tab(
    sh: Any,
    tabs: dict[str, Any],
    now_utc: str,
    tce_items: list[dict[str, Any]],
) -> tuple[int, int]:
    if "TCE_RS_2026" not in tabs:
        ws_tce = sh.add_worksheet(title="TCE_RS_2026", rows=300, cols=11)
        ws_tce.update(
            range_name="A1",
            values=[
                ["RIO GRANDE DO SUL — TCE-RS LICITACON 2026 (ACTUALIZACIÓN HTTP-RANGE)"],
                [f"Actualizado automáticamente desde dados.tce.rs.gov.br: {now_utc}"],
                [""],
                TCERS_HEADERS,
            ],
            value_input_option="USER_ENTERED",
        )
        tabs["TCE_RS_2026"] = ws_tce
    else:
        ws_tce = tabs["TCE_RS_2026"]
        ws_tce.update(range_name="A2", values=[[f"Actualizado automáticamente desde dados.tce.rs.gov.br: {now_utc}"]], value_input_option="USER_ENTERED")

    existing_vals = ws_tce.get_all_values()
    if len(existing_vals) < 4:
        ws_tce.update(range_name="A4:K4", values=[TCERS_HEADERS], value_input_option="USER_ENTERED")
        existing_vals = ws_tce.get_all_values()

    existing_map: dict[str, int] = {}
    for idx in range(4, len(existing_vals)):
        rid = (existing_vals[idx][0] if existing_vals[idx] else "").strip()
        if rid:
            existing_map[rid] = idx + 1

    updates: list[dict[str, Any]] = []
    new_rows: list[list[Any]] = []
    for it in tce_items:
        rid = it["id"]
        if rid in existing_map:
            r_num = existing_map[rid]
            updates.append({"range": f"E{r_num}", "values": [[it["fase"]]]})
            updates.append({"range": f"G{r_num}:H{r_num}", "values": [[it["fim_cred"], it["valor"]]]})
        else:
            link = it["link"]
            link_cell = f'=HYPERLINK("{link}","LicitaCon")' if link.startswith("http") else ""
            new_rows.append(
                [
                    rid,
                    it["orgao"],
                    it["mod"],
                    it["proceso"],
                    it["fase"],
                    it["abertura"],
                    it["fim_cred"],
                    it["valor"],
                    it["objeto"],
                    "NUEVO — Revisar (TCE-RS)",
                    link_cell,
                ]
            )

    if updates:
        ws_tce.batch_update(updates, value_input_option="USER_ENTERED")
    if new_rows:
        ws_tce.append_rows(new_rows, value_input_option="USER_ENTERED")

    return len(tce_items), len(new_rows)


def sync_google_sheet(
    now_utc: str,
    candidates: dict[str, dict[str, Any]],
    pncp_stats: dict[str, Any],
    sec_stats: dict[str, Any],
    tce_items: list[dict[str, Any]],
    tce_stats: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    sa_raw = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    sheet_id = (os.environ.get("GOOGLE_SHEET_ID") or "").strip()
    if not sa_raw or not sheet_id:
        details = fetch_details_parallel(list(candidates.keys()), max_workers=8)
        return False, {
            "message": "Google Sheets sync skipped (configure GOOGLE_SERVICE_ACCOUNT_JSON + GOOGLE_SHEET_ID secrets)",
            "details_checked": len(details),
            "total_rows": 0,
            "refreshed": 0,
            "new_items": [],
            "status_changes": [],
            "tce_rs_active": tce_stats.get("active_hits", 0),
            "tce_rs_new": 0,
        }

    import gspread
    from google.oauth2.service_account import Credentials

    info = json.loads(sa_raw) if sa_raw.startswith("{") else json.loads(Path(sa_raw).read_text(encoding="utf-8"))
    creds = Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    tabs = {ws.title: ws for ws in sh.worksheets()}

    if "Oportunidades_PNCP_BR" not in tabs:
        ws_op = sh.add_worksheet(title="Oportunidades_PNCP_BR", rows=300, cols=14)
        ws_op.update(
            range_name="A1",
            values=[
                ["BRASIL — OPORTUNIDADES PNCP (SINCRONIZACIÓN AUTOMÁTICA)"],
                [f"Actualizado automáticamente desde PNCP API: {now_utc}"],
                [""],
                OP_HEADERS,
            ],
            value_input_option="USER_ENTERED",
        )
        tabs["Oportunidades_PNCP_BR"] = ws_op
    else:
        ws_op = tabs["Oportunidades_PNCP_BR"]

    existing_vals = ws_op.get_all_values()
    if len(existing_vals) < 4:
        ws_op.update(range_name="A4:N4", values=[OP_HEADERS], value_input_option="USER_ENTERED")
        existing_vals = ws_op.get_all_values()

    existing_ids: dict[str, tuple[int, list[str]]] = {}
    for idx in range(4, len(existing_vals)):
        row = existing_vals[idx]
        cid = (row[6] if len(row) > 6 else "").strip()
        if cid:
            existing_ids[cid] = (idx + 1, row)

    all_cids = list(existing_ids.keys()) + [c for c in candidates.keys() if c not in existing_ids]
    details = fetch_details_parallel(all_cids, max_workers=8)

    # Refresh cols I, J, K of existing tracked IDs in a single batch_update
    batch_updates: list[dict[str, Any]] = []
    status_changes: list[dict[str, str]] = []
    refreshed = 0
    for cid, (row_num, row) in existing_ids.items():
        det = details.get(cid)
        if not det:
            continue
        refreshed += 1
        old_val = row[8] if len(row) > 8 else ""
        old_enc = row[9] if len(row) > 9 else ""
        old_sit = row[10] if len(row) > 10 else ""

        new_val_num = det.get("valorTotalEstimado")
        new_val: Any = float(new_val_num) if new_val_num is not None else old_val
        new_enc = fmt_dt(det.get("dataEncerramentoProposta")) or old_enc
        new_sit = (det.get("situacaoCompraNome") or "").strip() or old_sit

        if new_sit != old_sit:
            status_changes.append(
                {
                    "cid": cid,
                    "orgao": row[4] if len(row) > 4 else "",
                    "proceso": row[5] if len(row) > 5 else "",
                    "old": old_sit,
                    "new": new_sit,
                }
            )
        batch_updates.append({"range": f"I{row_num}:K{row_num}", "values": [[new_val, new_enc, new_sit]]})

    if batch_updates:
        ws_op.batch_update(batch_updates, value_input_option="USER_ENTERED")

    # Append genuinely NEW PNCP processes
    new_items: list[dict[str, Any]] = []
    for cid, it in candidates.items():
        if cid in existing_ids:
            continue
        det = details.get(cid)
        val = det.get("valorTotalEstimado") if det else it.get("valor_global")
        enc = fmt_dt((det.get("dataEncerramentoProposta") if det else None) or it.get("data_fim_vigencia"))
        sit = ((det.get("situacaoCompraNome") if det else None) or it.get("situacao_nome") or "Divulgada no PNCP").strip()
        if sit.lower() in ("encerrada", "anulada", "revogada"):
            continue
        prio, track, dictamen = classify_item(it)
        desc = re.sub(r"\s+", " ", (it.get("description") or "").strip())[:240]
        url = build_pncp_url(it)
        new_items.append(
            {
                "prio": prio,
                "track": track,
                "uf": it.get("uf") or "",
                "mun": it.get("municipio_nome") or "",
                "orgao": it.get("orgao_nome") or "",
                "title": it.get("title") or "",
                "cid": cid,
                "mod": it.get("modalidade_licitacao_nome") or "",
                "val": float(val) if val is not None else "",
                "enc": enc,
                "sit": sit,
                "dictamen": dictamen,
                "motivo": f"[Auto PNCP {now_utc[:10]}] {desc}",
                "url": url,
            }
        )

    new_items.sort(key=lambda x: (0 if x["prio"] == "P1" else 1, x["enc"] or "9999"))

    if new_items:
        append_rows = [
            [
                it["prio"],
                it["track"],
                it["uf"],
                it["mun"],
                it["orgao"],
                it["title"],
                it["cid"],
                it["mod"],
                it["val"],
                it["enc"],
                it["sit"],
                it["dictamen"],
                it["motivo"],
                f'=HYPERLINK("{it["url"]}","{it["url"]}")',
            ]
            for it in new_items
        ]
        ws_op.append_rows(append_rows, value_input_option="USER_ENTERED")

    last_row = max(5, len(existing_vals) + len(new_items))

    # Update Resumen_Ejecutivo timestamp + dynamic KPI ranges if present
    if "Resumen_Ejecutivo" in tabs:
        ws_res = tabs["Resumen_Ejecutivo"]
        ws_res.batch_update(
            [
                {"range": "A2", "values": [[f"Última sincronización automática: {now_utc} · PNCP API + TCE-RS + BO AR."]]},
                {
                    "range": "B5:B10",
                    "values": [
                        [f"=COUNTA(Oportunidades_PNCP_BR!F5:F{last_row})"],
                        [f'=COUNTIFS(Oportunidades_PNCP_BR!L5:L{last_row},"GO CONDICIONADO (Track 2 OEM)",Oportunidades_PNCP_BR!C5:C{last_row},"PR")+COUNTIFS(Oportunidades_PNCP_BR!L5:L{last_row},"GO CONDICIONADO (Track 2 OEM)",Oportunidades_PNCP_BR!C5:C{last_row},"SC")'],
                        [f'=SUMIFS(Oportunidades_PNCP_BR!I5:I{last_row},Oportunidades_PNCP_BR!L5:L{last_row},"GO CONDICIONADO (Track 2 OEM)",Oportunidades_PNCP_BR!C5:C{last_row},"PR")+SUMIFS(Oportunidades_PNCP_BR!I5:I{last_row},Oportunidades_PNCP_BR!L5:L{last_row},"GO CONDICIONADO (Track 2 OEM)",Oportunidades_PNCP_BR!C5:C{last_row},"SC")'],
                        [f'=SUMIF(Oportunidades_PNCP_BR!L5:L{last_row},"GO CONDICIONADO (Track 2 OEM)",Oportunidades_PNCP_BR!I5:I{last_row})'],
                        [f'=COUNTIF(Oportunidades_PNCP_BR!L5:L{last_row},"NO-GO*")'],
                        [f'=COUNTIF(Oportunidades_PNCP_BR!L5:L{last_row},"DESCARTADO*")'],
                    ],
                },
            ],
            value_input_option="USER_ENTERED",
        )

    # Sync TCE-RS 2026 tab (non-destructive merge)
    tce_active_cnt, tce_new_cnt = sync_tce_rs_tab(sh, tabs, now_utc, tce_items) if tce_items else (0, 0)

    # Build/refresh automated compliance & gap matrix ("Lo que exigen vs. Lo que tengo")
    op_formulas = ws_op.get_all_values(value_render_option="FORMULA")[4:]
    tce_formulas = (
        tabs["TCE_RS_2026"].get_all_values(value_render_option="FORMULA")[4:]
        if "TCE_RS_2026" in tabs
        else []
    )
    comp_stats = compliance_engine.sync_compliance_matrix(sh, tabs, now_utc, op_formulas, tce_formulas)

    # Append 1 audit row to Log_Sincronizacion
    log_headers = [
        "timestamp_utc",
        "pncp_ok",
        "total_rows",
        "refreshed",
        "new_items",
        "status_changes",
        "tce_rs_http",
        "bo_ar_http",
        "bo_ar_hits",
        "tce_rs_active",
        "tce_rs_new",
    ]
    if "Log_Sincronizacion" not in tabs:
        ws_log = sh.add_worksheet(title="Log_Sincronizacion", rows=500, cols=11)
        ws_log.update(range_name="A1:K1", values=[log_headers], value_input_option="USER_ENTERED")
    else:
        ws_log = tabs["Log_Sincronizacion"]
        ws_log.update(range_name="A1:K1", values=[log_headers], value_input_option="USER_ENTERED")

    ws_log.append_row(
        [
            now_utc,
            f"{pncp_stats['ok_calls']}/{pncp_stats['total_calls']}",
            last_row - 4,
            refreshed,
            len(new_items),
            len(status_changes),
            sec_stats["tce_rs_http"],
            sec_stats["bo_ar_http"],
            json.dumps(sec_stats["bo_ar_hits"], ensure_ascii=False),
            tce_active_cnt,
            tce_new_cnt,
        ],
        value_input_option="USER_ENTERED",
    )

    return True, {
        "message": f"Synced private Google Sheet {sheet_id}",
        "total_rows": last_row - 4,
        "refreshed": refreshed,
        "new_items": new_items,
        "status_changes": status_changes,
        "tce_rs_active": tce_active_cnt,
        "tce_rs_new": tce_new_cnt,
        "compliance": comp_stats,
    }


def send_telegram(text: str) -> None:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text[:3900], "disable_web_page_preview": True})
    subprocess.run(
        ["curl", "-sS", "--max-time", "10", "-H", "Content-Type: application/json", "-d", payload, url],
        capture_output=True,
        text=True,
    )


def main() -> None:
    now_dt = dt.datetime.now(dt.timezone.utc)
    now_utc = now_dt.strftime("%Y-%m-%d %H:%M UTC")
    today_iso = now_dt.date().isoformat()
    candidates, pncp_stats = poll_pncp()
    sec_stats = check_secondary()
    tce_items, tce_stats = poll_tce_rs_2026(today_iso)
    synced, res = sync_google_sheet(now_utc, candidates, pncp_stats, sec_stats, tce_items, tce_stats)

    tce_desc = (
        f"OK ({tce_stats['active_hits']} active RS matches of {tce_stats['total_rows_scanned']} rows)"
        if tce_stats.get("ok")
        else f"WARN ({tce_stats.get('error', 'unavailable')})"
    )
    lines = [
        f"## PNCP, TCE-RS & AR Health Radar Sync ({now_utc})",
        f"- **PNCP Search:** {pncp_stats['ok_calls']}/{pncp_stats['total_calls']} OK ({pncp_stats['unique_relevant']} relevant active tenders)",
        f"- **TCE-RS 2026 HTTP-Range:** {tce_desc}",
        f"- **TCE-RS CKAN:** HTTP {sec_stats['tce_rs_http']} | **Boletin Oficial AR:** HTTP {sec_stats['bo_ar_http']} (hits={len(sec_stats['bo_ar_hits'])})",
        f"- **Google Sheets:** {res['message']}",
    ]
    if synced:
        comp = res.get("compliance") or {}
        lines.append(
            f"- **Sheet stats:** pncp_rows={res['total_rows']}, refreshed={res['refreshed']}, "
            f"pncp_new={len(res['new_items'])}, status_changes={len(res['status_changes'])}, "
            f"tce_rs_active={res['tce_rs_active']}, tce_rs_new={res['tce_rs_new']}"
        )
        if comp:
            lines.append(
                f"- **Compliance matrix:** tenders_scored={comp.get('tenders_scored', 0)}, "
                f"detail_rows={comp.get('detail_rows', 0)}, "
                f"pdfs_audited_now={comp.get('pdfs_audited_now', 0)}, "
                f"pdfs_cached={comp.get('pdfs_cached', 0)}"
            )

    summary_md = "\n".join(lines)
    print(summary_md, flush=True)

    step_sum = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_sum:
        with open(step_sum, "a", encoding="utf-8") as f:
            f.write(summary_md + "\n")

    if synced and (res["new_items"] or res["status_changes"] or res.get("tce_rs_new", 0) > 0):
        msg = (
            f"B2G Health Radar ({now_utc})\n"
            f"PNCP nuevas: {len(res['new_items'])} | Cambios: {len(res['status_changes'])} | TCE-RS nuevas: {res.get('tce_rs_new', 0)}\n"
        )
        for it in res["new_items"][:4]:
            msg += f"- [{it['uf']}] {it['orgao']} | {it['title']} ({it['enc']}) {it['url']}\n"
        send_telegram(msg)


if __name__ == "__main__":
    main()
