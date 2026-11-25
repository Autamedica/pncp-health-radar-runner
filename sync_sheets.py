#!/usr/bin/env python3
"""
PNCP & AR Public Health Procurement Radar Sync (Code-Only Runner)
Polls live public procurement APIs (Brasil PNCP, TCE-RS CKAN, Boletin Oficial AR)
and syncs updates/new tenders directly into a private Google Sheet via Secrets.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import html
import json
import os
import re
import subprocess
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any

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


def fmt_dt(val: str | None) -> str:
    return val[:16].replace("T", " ") if val else ""


def build_pncp_url(item: dict[str, Any]) -> str:
    return f"https://pncp.gov.br/app/editais/{item.get('orgao_cnpj')}/{item.get('ano')}/{item.get('numero_sequencial')}"


def sync_google_sheet(
    now_utc: str,
    candidates: dict[str, dict[str, Any]],
    pncp_stats: dict[str, Any],
    sec_stats: dict[str, Any],
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

    # Append 1 audit row to Log_Sincronizacion
    if "Log_Sincronizacion" not in tabs:
        ws_log = sh.add_worksheet(title="Log_Sincronizacion", rows=500, cols=9)
        ws_log.update(
            range_name="A1:I1",
            values=[["timestamp_utc", "pncp_ok", "total_rows", "refreshed", "new_items", "status_changes", "tce_rs_http", "bo_ar_http", "bo_ar_hits"]],
            value_input_option="USER_ENTERED",
        )
    else:
        ws_log = tabs["Log_Sincronizacion"]

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
        ],
        value_input_option="USER_ENTERED",
    )

    return True, {
        "message": f"Synced private Google Sheet {sheet_id}",
        "total_rows": last_row - 4,
        "refreshed": refreshed,
        "new_items": new_items,
        "status_changes": status_changes,
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
    now_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    candidates, pncp_stats = poll_pncp()
    sec_stats = check_secondary()
    synced, res = sync_google_sheet(now_utc, candidates, pncp_stats, sec_stats)

    lines = [
        f"## PNCP & AR Health Radar Sync ({now_utc})",
        f"- **PNCP Search:** {pncp_stats['ok_calls']}/{pncp_stats['total_calls']} OK ({pncp_stats['unique_relevant']} relevant active tenders)",
        f"- **TCE-RS CKAN:** HTTP {sec_stats['tce_rs_http']} | **Boletin Oficial AR:** HTTP {sec_stats['bo_ar_http']} (hits={len(sec_stats['bo_ar_hits'])})",
        f"- **Google Sheets:** {res['message']}",
    ]
    if synced:
        lines.append(
            f"- **Sheet stats:** total_rows={res['total_rows']}, refreshed={res['refreshed']}, "
            f"new={len(res['new_items'])}, status_changes={len(res['status_changes'])}"
        )

    summary_md = "\n".join(lines)
    print(summary_md, flush=True)

    step_sum = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_sum:
        with open(step_sum, "a", encoding="utf-8") as f:
            f.write(summary_md + "\n")

    if synced and (res["new_items"] or res["status_changes"]):
        msg = f"PNCP B2G Radar ({now_utc})\nNuevas: {len(res['new_items'])} | Cambios: {len(res['status_changes'])}\n"
        for it in res["new_items"][:5]:
            msg += f"- [{it['uf']}] {it['orgao']} | {it['title']} ({it['enc']}) {it['url']}\n"
        send_telegram(msg)


if __name__ == "__main__":
    main()
