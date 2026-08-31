from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import threading
import zipfile
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, request

def load_dotenv() -> None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        return


load_dotenv()

API_URL = "https://api.monday.com/v2"
PORT = int(os.getenv("PORT", "8765"))
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()
IS_HOSTED = bool(os.getenv("RENDER"))
SESSIONS: set[str] = set()


def monday_request(token: str, query: str, variables: dict | None = None) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    http_request = request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": token,
            "Content-Type": "application/json",
            "API-Version": "2025-10",
        },
        method="POST",
    )
    try:
        with request.urlopen(http_request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"monday retornou HTTP {exc.code}: {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Não foi possível conectar à monday: {exc.reason}") from exc

    if payload.get("errors"):
        messages = "; ".join(item.get("message", "Erro GraphQL") for item in payload["errors"])
        raise RuntimeError(messages)
    return payload.get("data", {})


def list_boards(token: str) -> list[dict]:
    query = """
    query {
      boards(limit: 100, state: active, hierarchy_types: [classic, multi_level]) {
        id
        name
        state
        url
        workspace_id
        items_count
      }
    }
    """
    return monday_request(token, query).get("boards", [])


def extract_board(token: str, board_id: str) -> tuple[dict, list[dict]]:
    query = """
    query ($board_id: ID!, $cursor: String) {
      boards(ids: [$board_id]) {
        id
        name
        url
        columns { id title type }
        items_page(limit: 250, cursor: $cursor) {
          cursor
          items {
            id
            name
            url
            group { id title }
            created_at
            updated_at
            column_values {
              id
              text
              value
              type
              ... on BoardRelationValue { display_value linked_item_ids }
              ... on MirrorValue { display_value }
              ... on LinkValue { url }
            }
          }
        }
      }
    }
    """
    items: list[dict] = []
    cursor = None
    board = None
    while True:
        data = monday_request(token, query, {"board_id": board_id, "cursor": cursor})
        boards = data.get("boards", [])
        if not boards:
            raise RuntimeError("Board não encontrado ou sem permissão de leitura.")
        board = boards[0]
        page = board.get("items_page") or {}
        items.extend(page.get("items", []))
        cursor = page.get("cursor")
        if not cursor:
            break
    return board, items


DETAILS_QUERY = """
query ($ids: [ID!]) {
  items(ids: $ids) {
    id
    assets(assets_source: all) {
      id
      name
      file_extension
      file_size
      public_url
      created_at
      uploaded_by { name }
    }
    updates(limit: 100) {
      id
      text_body
      created_at
      creator { name }
      assets { id name public_url file_size }
      replies { text_body created_at creator { name } }
    }
    subitems {
      id
      name
      url
      column_values { id text type }
    }
  }
}
"""


def fetch_item_details(token: str, item_ids: list[str]) -> dict[str, dict]:
    details: dict[str, dict] = {}
    for start in range(0, len(item_ids), 20):
        batch = item_ids[start:start + 20]
        data = monday_request(token, DETAILS_QUERY, {"ids": batch})
        for item in data.get("items", []):
            details[str(item.get("id"))] = item
    return details


def merge_details(items: list[dict], details: dict[str, dict]) -> None:
    for item in items:
        found = details.get(str(item.get("id"))) or {}
        item["assets"] = found.get("assets") or []
        item["updates"] = found.get("updates") or []
        item["subitems"] = found.get("subitems") or []


def column_display(value: dict) -> str:
    for key in ("text", "display_value", "url"):
        content = value.get(key)
        if content:
            return str(content)
    return ""


def format_update(update: dict) -> str:
    body = " ".join((update.get("text_body") or "").split())
    author = (update.get("creator") or {}).get("name", "?")
    when = (update.get("created_at") or "")[:10]
    files = update.get("assets") or []
    suffix = f" [anexos: {', '.join(a.get('name', '') for a in files)}]" if files else ""
    return f"[{when}] {author}: {body}{suffix}"


def collect_assets(items: list[dict]) -> list[dict]:
    collected = []
    for item in items:
        for asset in item.get("assets") or []:
            collected.append({
                "id": asset.get("id"),
                "item_id": item.get("id"),
                "item_name": item.get("name"),
                "name": asset.get("name"),
                "file_size": asset.get("file_size"),
                "public_url": asset.get("public_url"),
            })
    return collected


def sanitize_name(raw: str, fallback: str = "arquivo", limit: int = 100) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(raw or "")).strip(" .")
    return cleaned[:limit] or fallback


def download_assets_to_disk(board_name: str, assets: list[dict]) -> dict:
    base = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "anexos", sanitize_name(board_name, "board", 60)
    )
    saved = 0
    errors: list[str] = []
    for asset in assets:
        url = str(asset.get("public_url") or "")
        if not url.startswith("https://"):
            errors.append(f"{asset.get('name')}: sem URL pública")
            continue
        folder = os.path.join(
            base, sanitize_name(f"{asset.get('item_id')} - {asset.get('item_name')}", "item", 60)
        )
        os.makedirs(folder, exist_ok=True)
        stem, extension = os.path.splitext(str(asset.get("name") or ""))
        extension = re.sub(r"[^A-Za-z0-9.]", "", extension)[:10]
        filename = sanitize_name(stem, f"asset-{asset.get('id')}", 80) + extension
        target = os.path.join(folder, filename)
        if os.path.exists(target):
            target = os.path.join(folder, f"{sanitize_name(stem, 'arquivo', 80)}-{asset.get('id')}{extension}")
        try:
            with request.urlopen(url, timeout=120) as response, open(target, "wb") as file_handle:
                shutil.copyfileobj(response, file_handle)
            saved += 1
        except OSError as exc:
            errors.append(f"{asset.get('name')}: {exc}")
    return {"saved": saved, "total": len(assets), "folder": base, "errors": errors[:10]}


def build_assets_zip(assets: list[dict]) -> tuple[bytes, int, list[str]]:
    buffer = io.BytesIO()
    saved = 0
    errors: list[str] = []
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for asset in assets:
            url = str(asset.get("public_url") or "")
            if not url.startswith("https://"):
                errors.append(f"{asset.get('name')}: sem URL pública")
                continue
            folder = sanitize_name(f"{asset.get('item_id')} - {asset.get('item_name')}", "item", 60)
            stem, extension = os.path.splitext(str(asset.get("name") or ""))
            extension = re.sub(r"[^A-Za-z0-9.]", "", extension)[:10]
            fallback = f"asset-{asset.get('id')}"
            entry = f"{folder}/{sanitize_name(stem, fallback, 80)}{extension}"
            if entry in used:
                entry = f"{folder}/{sanitize_name(stem, 'arquivo', 80)}-{asset.get('id')}{extension}"
            used.add(entry)
            try:
                with request.urlopen(url, timeout=120) as response:
                    archive.writestr(entry, response.read())
                saved += 1
            except OSError as exc:
                errors.append(f"{asset.get('name')}: {exc}")
    return buffer.getvalue(), saved, errors


def flatten_items(board: dict, items: list[dict], include_details: bool = False) -> list[dict]:
    columns = board.get("columns", [])
    rows = []
    for item in items:
        row = {
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "group": (item.get("group") or {}).get("title", ""),
            "created_at": item.get("created_at", ""),
            "updated_at": item.get("updated_at", ""),
            "item_url": item.get("url", ""),
        }
        values = {value.get("id"): column_display(value) for value in item.get("column_values", [])}
        for column in columns:
            row[column.get("title", column.get("id", ""))] = values.get(column.get("id"), "")
        if include_details:
            row["anexos"] = "; ".join(a.get("name", "") for a in item.get("assets") or [])
            row["subitens"] = "; ".join(s.get("name", "") for s in item.get("subitems") or [])
            parts = []
            for update in item.get("updates") or []:
                parts.append(format_update(update))
                for reply in update.get("replies") or []:
                    parts.append("↳ " + format_update(reply))
            row["comentarios"] = " | ".join(part for part in parts if part)
        rows.append(row)
    return rows


URL_PATTERN = re.compile(r"^https?://[^\s\"'<>]+$")

STYLES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border/></borders>'
    '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
    '<cellXfs count="2"><xf xfId="0"/><xf fontId="1" xfId="0" applyFont="1"/></cellXfs>'
    "</styleSheet>"
)


def xml_escape(value) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value))
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def column_letter(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def worksheet_xml(headers: list, rows: list[list]) -> tuple[str, list[str]]:
    widths = []
    for position, header in enumerate(headers):
        longest = len(str(header))
        for row in rows[:300]:
            value = row[position]
            if value not in (None, ""):
                longest = max(longest, min(len(str(value)), 60))
        widths.append(min(60, max(10, longest + 2)))
    cols = "".join(
        f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>'
        for i, width in enumerate(widths, 1)
    )
    header_cells = "".join(
        f'<c r="{column_letter(i)}1" t="inlineStr" s="1"><is><t xml:space="preserve">{xml_escape(header)}</t></is></c>'
        for i, header in enumerate(headers, 1)
    )
    lines = [f'<row r="1">{header_cells}</row>']
    targets: list[str] = []
    hyperlink_refs: list[str] = []
    for row_number, row in enumerate(rows, 2):
        cells = []
        for col_number, value in enumerate(row, 1):
            if value in (None, ""):
                continue
            ref = f"{column_letter(col_number)}{row_number}"
            if isinstance(value, bool):
                text = "true" if value else "false"
            elif isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
                continue
            else:
                text = str(value)[:32700]
            if URL_PATTERN.match(text):
                targets.append(text)
                hyperlink_refs.append(f'<hyperlink ref="{ref}" r:id="rIdh{len(targets)}"/>')
            cells.append(
                f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{xml_escape(text)}</t></is></c>'
            )
        lines.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    autofilter = (
        f'<autoFilter ref="A1:{column_letter(max(len(headers), 1))}{len(rows) + 1}"/>' if rows else ""
    )
    hyperlinks = f'<hyperlinks>{"".join(hyperlink_refs)}</hyperlinks>' if hyperlink_refs else ""
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
        f"<cols>{cols}</cols><sheetData>{''.join(lines)}</sheetData>{autofilter}{hyperlinks}</worksheet>"
    )
    return xml, targets


def build_xlsx(sheets: list[tuple[str, list, list[list]]]) -> bytes:
    buffer = io.BytesIO()
    relationship_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        overrides = "".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(1, len(sheets) + 1)
        )
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            f"{overrides}</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{relationship_ns}/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        sheet_tags = "".join(
            f'<sheet name="{xml_escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
            for i, (name, _, _) in enumerate(sheets, 1)
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            f'xmlns:r="{relationship_ns}"><sheets>{sheet_tags}</sheets></workbook>',
        )
        workbook_rels = "".join(
            f'<Relationship Id="rId{i}" Type="{relationship_ns}/worksheet" Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(sheets) + 1)
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{workbook_rels}"
            f'<Relationship Id="rId{len(sheets) + 1}" Type="{relationship_ns}/styles" Target="styles.xml"/>'
            "</Relationships>",
        )
        archive.writestr("xl/styles.xml", STYLES_XML)
        for i, (name, headers, rows) in enumerate(sheets, 1):
            xml, targets = worksheet_xml(headers, rows)
            archive.writestr(f"xl/worksheets/sheet{i}.xml", xml)
            if targets:
                hyperlink_rels = "".join(
                    f'<Relationship Id="rIdh{j}" Type="{relationship_ns}/hyperlink" '
                    f'Target="{xml_escape(target)}" TargetMode="External"/>'
                    for j, target in enumerate(targets, 1)
                )
                archive.writestr(
                    f"xl/worksheets/_rels/sheet{i}.xml.rels",
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    f"{hyperlink_rels}</Relationships>",
                )
    return buffer.getvalue()


def build_comment_rows(items: list[dict]) -> tuple[list[str], list[list]]:
    headers = ["item_id", "item", "tipo", "autor", "data", "texto", "anexos"]
    rows = []
    for item in items:
        for update in item.get("updates") or []:
            rows.append([
                item.get("id"), item.get("name"), "comentário",
                (update.get("creator") or {}).get("name", ""),
                (update.get("created_at") or "")[:19].replace("T", " "),
                " ".join((update.get("text_body") or "").split()),
                "; ".join(a.get("name", "") for a in update.get("assets") or []),
            ])
            for reply in update.get("replies") or []:
                rows.append([
                    item.get("id"), item.get("name"), "resposta",
                    (reply.get("creator") or {}).get("name", ""),
                    (reply.get("created_at") or "")[:19].replace("T", " "),
                    " ".join((reply.get("text_body") or "").split()),
                    "",
                ])
    return headers, rows


def build_asset_rows(items: list[dict]) -> tuple[list[str], list[list]]:
    headers = ["item_id", "item", "arquivo", "extensão", "tamanho_bytes", "enviado_por", "criado_em"]
    rows = []
    for item in items:
        for asset in item.get("assets") or []:
            rows.append([
                item.get("id"), item.get("name"), asset.get("name"),
                asset.get("file_extension"), asset.get("file_size"),
                (asset.get("uploaded_by") or {}).get("name", ""),
                (asset.get("created_at") or "")[:19].replace("T", " "),
            ])
    return headers, rows


def build_subitem_rows(items: list[dict]) -> tuple[list[str], list[list]]:
    column_ids: list[str] = []
    for item in items:
        for subitem in item.get("subitems") or []:
            for value in subitem.get("column_values") or []:
                if value.get("id") not in column_ids:
                    column_ids.append(value.get("id"))
    headers = ["item_id", "item", "subitem_id", "subitem", "url"] + column_ids
    rows = []
    for item in items:
        for subitem in item.get("subitems") or []:
            values = {v.get("id"): v.get("text") or "" for v in subitem.get("column_values") or []}
            rows.append(
                [item.get("id"), item.get("name"), subitem.get("id"), subitem.get("name"), subitem.get("url", "")]
                + [values.get(column_id, "") for column_id in column_ids]
            )
    return headers, rows


def build_workbook(board: dict, rows: list[dict], items: list[dict]) -> bytes:
    headers = list(rows[0].keys()) if rows else ["sem dados"]
    sheets: list[tuple[str, list, list[list]]] = [
        ("Itens", headers, [[row.get(header, "") for header in headers] for row in rows])
    ]
    comment_headers, comment_rows = build_comment_rows(items)
    if comment_rows:
        sheets.append(("Comentários", comment_headers, comment_rows))
    asset_headers, asset_rows = build_asset_rows(items)
    if asset_rows:
        sheets.append(("Anexos", asset_headers, asset_rows))
    subitem_headers, subitem_rows = build_subitem_rows(items)
    if subitem_rows:
        sheets.append(("Subitens", subitem_headers, subitem_rows))
    return build_xlsx(sheets)


LOGIN_HTML = r'''<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Monday Extractor — Acesso</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=Space+Mono:wght@400;700&display=swap');
:root{--ink:#17221d;--muted:#6b776f;--paper:#f4f1e8;--panel:#fffdf7;--line:#d8d9ce;--green:#1f6b4d;--lime:#c7e36f}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:'DM Sans',sans-serif;min-height:100vh;display:grid;place-items:center;padding:24px}
.card{background:var(--panel);border:1px solid var(--line);padding:34px;width:100%;max-width:380px}
.kicker{font:700 12px 'Space Mono';letter-spacing:1px;text-transform:uppercase;color:var(--green)}
h1{font-size:30px;letter-spacing:-1px;margin:12px 0 8px}
p{color:var(--muted);margin:0 0 22px;line-height:1.5;font-size:14px}
label{display:block;font:700 11px 'Space Mono';text-transform:uppercase;margin-bottom:8px;color:var(--muted)}
input{width:100%;border:1px solid var(--line);background:#fff;padding:12px;font:14px 'DM Sans';color:var(--ink)}
input:focus{outline:2px solid var(--lime);outline-offset:1px}
button{width:100%;border:0;margin-top:16px;padding:13px 16px;background:var(--green);color:#fff;font:700 12px 'Space Mono';cursor:pointer}
button:disabled{opacity:.45;cursor:wait}
.error{margin-top:14px;padding:12px;background:#fff0eb;color:#a3442e;font-size:13px;display:none}
</style></head>
<body><form class="card" id="form">
<div class="kicker">Data utility / monday.com</div>
<h1>Monday Extractor</h1>
<p>Informe a senha de acesso para continuar.</p>
<label for="password">Senha</label>
<input id="password" type="password" autocomplete="current-password" autofocus>
<button id="enter">Entrar</button>
<div id="error" class="error"></div>
</form><script>
const form=document.getElementById('form'),error=document.getElementById('error'),button=document.getElementById('enter');
form.onsubmit=async e=>{e.preventDefault();error.style.display='none';button.disabled=true;button.textContent='Verificando...';
try{const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('password').value})});
const d=await r.json();if(!r.ok)throw Error(d.error||'Falha no login.');location.reload()}
catch(err){error.textContent=err.message;error.style.display='block';button.disabled=false;button.textContent='Entrar'}};
</script></body></html>'''


HTML = r'''<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Monday Extractor</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=Space+Mono:wght@400;700&display=swap');
:root{--ink:#17221d;--muted:#6b776f;--paper:#f4f1e8;--panel:#fffdf7;--line:#d8d9ce;--green:#1f6b4d;--lime:#c7e36f;--coral:#e97658}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:'DM Sans',sans-serif}main{max-width:1120px;margin:auto;padding:42px 24px 64px}.top{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid var(--line);padding-bottom:26px}.kicker{font:700 12px 'Space Mono';letter-spacing:1px;text-transform:uppercase;color:var(--green)}h1{font-size:clamp(34px,5vw,66px);line-height:.98;margin:14px 0 10px;max-width:680px;letter-spacing:-2px}p{color:var(--muted);margin:0;line-height:1.5}.badge{background:var(--lime);padding:10px 13px;font:700 12px 'Space Mono';color:var(--ink)}.layout{display:grid;grid-template-columns:320px 1fr;gap:24px;margin-top:28px}.panel{background:var(--panel);border:1px solid var(--line);padding:22px}.panel h2{font-size:18px;margin:0 0 20px}.field{margin-bottom:17px}label{display:block;font:700 11px 'Space Mono';text-transform:uppercase;margin-bottom:8px;color:var(--muted)}input,select{width:100%;border:1px solid var(--line);background:#fff;padding:12px;font:14px 'DM Sans';color:var(--ink)}input:focus,select:focus{outline:2px solid var(--lime);outline-offset:1px}.actions{display:flex;gap:10px;flex-wrap:wrap}button{border:0;padding:13px 16px;background:var(--green);color:#fff;font:700 12px 'Space Mono';cursor:pointer}button.secondary{background:var(--ink)}button:disabled{opacity:.45;cursor:wait}.status{margin-top:18px;padding:13px;background:#eef1e8;color:var(--muted);font-size:13px;min-height:45px}.status.error{background:#fff0eb;color:#a3442e}.summary{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}.stat{border:1px solid var(--line);padding:12px 15px;background:var(--panel);min-width:120px}.stat strong{font-size:24px;display:block}.stat span{font:11px 'Space Mono';color:var(--muted)}.table-wrap{overflow:auto;border:1px solid var(--line);background:var(--panel);max-height:540px}table{width:100%;border-collapse:collapse;font-size:13px;white-space:nowrap}th,td{text-align:left;border-bottom:1px solid var(--line);padding:12px 14px}th{position:sticky;top:0;background:var(--ink);color:#fff;font:11px 'Space Mono';text-transform:uppercase}tr:last-child td{border-bottom:0}.empty{display:grid;place-items:center;min-height:360px;text-align:center;padding:30px}.empty strong{font-size:20px;display:block;margin-bottom:8px}.mono{font-family:'Space Mono';font-size:12px}@media(max-width:760px){main{padding:26px 16px 44px}.top{display:block}.badge{display:inline-block;margin-top:20px}.layout{grid-template-columns:1fr}.panel{padding:18px}}
.board-search{margin-bottom:10px}.board-grid{display:grid;gap:8px;max-height:300px;overflow:auto;padding:2px}.board-card{width:100%;text-align:left;background:#fff;border:1px solid var(--line);color:var(--ink);padding:12px 13px;font-family:'DM Sans';font-weight:500}.board-card:hover{border-color:var(--green);background:#f4f8ed}.board-card.selected{border:2px solid var(--green);padding:11px 12px;background:#eef5df}.board-card strong{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.board-card small{display:block;color:var(--muted);font:10px 'Space Mono';margin-top:5px}.no-boards{padding:16px 8px;color:var(--muted);font-size:13px}label.check{display:flex;align-items:center;gap:8px;text-transform:none;font:500 13px 'DM Sans';color:var(--ink);cursor:pointer}label.check input{width:auto;accent-color:var(--green)}</style></head>
<body><main>
<header class="top"><div><div class="kicker">Data utility / monday.com</div><h1>Traga os projetos para fora do monday.</h1><p>Leia um board inteiro — incluindo anexos, comentários e subitens — e baixe tudo em XLSX, JSON ou arquivos, mantendo o token apenas nesta sessão.</p></div><div class="badge">LOCAL · READ ONLY</div></header>
<section class="layout"><aside class="panel"><h2>Conexão</h2><div class="field"><label for="token">Personal API token</label><input id="token" type="password" placeholder="Cole seu token V2"></div><div class="actions"><button id="connect">Carregar boards</button></div><div class="field" style="margin-top:22px"><label for="board">Board para extrair</label><select id="board" disabled><option>Conecte primeiro</option></select></div><div class="field" style="margin-top:14px"><label class="check"><input id="details" type="checkbox" checked> Incluir anexos, comentários e subitens</label></div><div class="actions"><button id="extract" class="secondary" disabled>Extrair dados</button><button id="xlsx" disabled>XLSX</button><button id="json" disabled>JSON</button><button id="assets" disabled>Baixar anexos</button></div><div id="status" class="status">O token não é salvo em disco.</div></aside>
<section><div id="summary" class="summary"></div><div id="result" class="empty"><div><strong>Pronto para começar</strong><p>Informe um token com acesso de leitura aos boards.</p></div></div></section></section>
</main><script>
const $=id=>document.getElementById(id); let state={board:null,boards:[],selectedBoardId:null,items:[],rows:[],assets:[]};
function status(text,error=false){$('status').textContent=text;$('status').className='status'+(error?' error':'')}
function busy(button,value){button.disabled=value;button.textContent=value?'Carregando...':button.dataset.label}
async function api(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(r.status===401){location.reload();throw Error('Sessão expirada.')}return r}
const boardSelect=$('board');const boardSearch=document.createElement('input');boardSearch.id='board-search';boardSearch.className='board-search';boardSearch.type='search';boardSearch.placeholder='Nome ou ID';boardSearch.disabled=true;boardSelect.before(boardSearch);const boardGrid=document.createElement('div');boardGrid.id='board-grid';boardGrid.className='board-grid';boardSelect.replaceWith(boardGrid);
function renderBoards(){const query=boardSearch.value.trim().toLowerCase();const boards=state.boards.filter(board=>`${board.name} ${board.id}`.toLowerCase().includes(query));boardGrid.innerHTML=boards.length?boards.map(board=>`<button class="board-card${board.id===state.selectedBoardId?' selected':''}" data-board-id="${board.id}"><strong>${board.name}</strong><small>ID ${board.id} · ${board.items_count??0} itens</small></button>`).join(''):'<div class="no-boards">Nenhum board encontrado.</div>';document.querySelectorAll('.board-card').forEach(card=>card.onclick=()=>{state.selectedBoardId=card.dataset.boardId;renderBoards();$('extract').disabled=false})}
boardSearch.oninput=renderBoards;
$('connect').dataset.label='Carregar boards';$('extract').dataset.label='Extrair dados';$('assets').dataset.label='Baixar anexos';$('xlsx').dataset.label='XLSX';
$('connect').onclick=async()=>{const token=$('token').value.trim();if(!token)return status('Cole seu token para continuar.',true);busy($('connect'),true);try{const r=await api('/api/boards',{token});const d=await r.json();if(!r.ok)throw Error(d.error);state.boards=d.boards;state.selectedBoardId=d.boards[0]?.id||null;boardSearch.disabled=false;renderBoards();$('extract').disabled=!state.selectedBoardId;status(`${d.boards.length} board(s) carregado(s).`)}catch(e){status(e.message,true)}finally{busy($('connect'),false)}};
$('extract').onclick=async()=>{const token=$('token').value.trim(),boardId=state.selectedBoardId;if(!token||!boardId)return;busy($('extract'),true);try{const r=await api('/api/extract',{token,board_id:boardId,include_details:$('details').checked});const d=await r.json();if(!r.ok)throw Error(d.error);state={...state,...d,rows:d.rows};render(d);$('xlsx').disabled=false;$('json').disabled=false;$('assets').disabled=!(d.assets&&d.assets.length);status(`${d.rows.length} item(ns) extraído(s) de “${d.board.name}”.`)}catch(e){status(e.message,true)}finally{busy($('extract'),false)}};
$('assets').onclick=async()=>{if(!state.assets||!state.assets.length)return;busy($('assets'),true);try{const r=await api('/api/assets',{board_name:state.board.name,assets:state.assets});const ct=r.headers.get('Content-Type')||'';if(ct.includes('zip')){const saved=+r.headers.get('X-Saved'),total=+r.headers.get('X-Total');if(!saved){status('Nenhum anexo pôde ser baixado — as URLs podem ter expirado; extraia novamente.',true)}else{saveBlob(await r.blob(),exportName()+'-anexos.zip');status(`ZIP gerado com ${saved}/${total} anexo(s).`)}}else{const d=await r.json();if(!r.ok)throw Error(d.error);const failures=d.errors&&d.errors.length?` · ${d.errors.length} falha(s): ${d.errors[0]}`:'';status(`${d.saved}/${d.total} anexo(s) salvos em ${d.folder}${failures}`,d.saved===0&&d.total>0)}}catch(e){status(e.message,true)}finally{busy($('assets'),false)}};
function render(d){const keys=d.rows.length?Object.keys(d.rows[0]):[];const extra=d.assets&&d.assets.length!==undefined&&$('details').checked?`<div class="stat"><strong>${d.assets.length}</strong><span>ANEXOS</span></div><div class="stat"><strong>${d.updates_total??0}</strong><span>COMENTÁRIOS</span></div>`:'';$('summary').innerHTML=`<div class="stat"><strong>${d.rows.length}</strong><span>ITENS</span></div><div class="stat"><strong>${d.board.columns.length}</strong><span>COLUNAS</span></div>${extra}<div class="stat"><strong>${d.board.name}</strong><span>BOARD</span></div>`;$('result').className='table-wrap';$('result').innerHTML=d.rows.length?`<table><thead><tr>${keys.map(k=>`<th>${k}</th>`).join('')}</tr></thead><tbody>${d.rows.map(row=>`<tr>${keys.map(k=>`<td>${String(row[k]??'').replaceAll('&','&amp;').replaceAll('<','&lt;')}</td>`).join('')}</tr>`).join('')}</tbody></table>`:'<div class="empty"><p>Este board não tem itens ativos.</p></div>'}
function exportName(){return state.board.name.replace(/[^a-z0-9]+/gi,'-').replace(/^-|-$/g,'').toLowerCase()||'monday-export'}
function saveBlob(blob,filename){const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=filename;a.click();URL.revokeObjectURL(a.href)}
$('json').onclick=()=>{saveBlob(new Blob([JSON.stringify({board:state.board,rows:state.rows,items:state.items||[]},null,2)],{type:'application/json'}),exportName()+'.json')};
$('xlsx').onclick=async()=>{if(!state.board)return;busy($('xlsx'),true);try{const r=await api('/api/xlsx',{board:state.board,rows:state.rows,items:state.items||[]});if(!r.ok){const d=await r.json();throw Error(d.error)}saveBlob(await r.blob(),exportName()+'.xlsx');status('XLSX gerado com abas de itens, coment\u00e1rios, anexos e subitens.')}catch(e){status(e.message,true)}finally{busy($('xlsx'),false)}};
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def session_token(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "session":
                return value
        return ""

    def is_authenticated(self) -> bool:
        return not APP_PASSWORD or self.session_token() in SESSIONS

    def handle_login(self, payload: dict):
        password = str(payload.get("password", ""))
        if APP_PASSWORD and not secrets.compare_digest(password.encode(), APP_PASSWORD.encode()):
            self.send_json({"error": "Senha incorreta."}, 401)
            return
        token = secrets.token_urlsafe(32)
        if len(SESSIONS) >= 500:
            SESSIONS.clear()
        SESSIONS.add(token)
        data = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Set-Cookie", f"session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict, status: int = 200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, data: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        page = HTML if self.is_authenticated() else LOGIN_HTML
        if IS_HOSTED:
            page = page.replace("LOCAL · READ ONLY", "ONLINE · READ ONLY")
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length))
            if self.path == "/login":
                self.handle_login(payload)
                return
            if not self.is_authenticated():
                self.send_json({"error": "Sessão expirada. Recarregue a página."}, 401)
                return
            if self.path == "/api/xlsx":
                workbook = build_workbook(
                    payload.get("board") or {}, payload.get("rows") or [], payload.get("items") or []
                )
                self.send_bytes(
                    workbook, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
                return
            if self.path == "/api/assets":
                assets = payload.get("assets") or []
                if IS_HOSTED:
                    data, saved, _ = build_assets_zip(assets)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("X-Saved", str(saved))
                    self.send_header("X-Total", str(len(assets)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_json(
                        download_assets_to_disk(str(payload.get("board_name", "")), assets)
                    )
                return
            token = str(payload.get("token", "")).strip()
            if not token:
                raise ValueError("Token não informado.")
            if self.path == "/api/boards":
                self.send_json({"boards": list_boards(token)})
            elif self.path == "/api/extract":
                board, items = extract_board(token, str(payload.get("board_id", "")))
                include_details = bool(payload.get("include_details"))
                if include_details and items:
                    merge_details(items, fetch_item_details(token, [str(item.get("id")) for item in items]))
                self.send_json({
                    "board": board,
                    "rows": flatten_items(board, items, include_details),
                    "items": items,
                    "assets": collect_assets(items) if include_details else [],
                    "updates_total": sum(len(item.get("updates") or []) for item in items),
                })
            else:
                self.send_error(404)
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    if IS_HOSTED and not APP_PASSWORD:
        raise SystemExit("Defina a variável de ambiente APP_PASSWORD antes de publicar a aplicação.")
    host = "0.0.0.0" if IS_HOSTED else "127.0.0.1"
    server = ThreadingHTTPServer((host, PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Monday Extractor disponível em {url}")
    if APP_PASSWORD:
        print("Tela de senha ativa (APP_PASSWORD).")
    if not IS_HOSTED:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado.")
        server.server_close()
