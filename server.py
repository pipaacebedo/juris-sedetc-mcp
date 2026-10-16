#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tc-juris-mcp: servidor MCP para la Sistematizacion Jurisprudencial del Tribunal Constitucional del Peru (jurisprudencia.sedetc.gob.pe, API jurisbackend.sedetc.gob.pe/api).

Busca en el TEXTO COMPLETO (Elasticsearch) y en los filtros avanzados (SQL) del portal
del TC: expediente, demandante/demandado, fecha (o rango), sentidos, distritos, salas,
tipos y palabras claves sistematizadas; resultados por SISTEMATIZACION (precedentes
vinculantes, tipos de proceso...) y por TESAURO de conceptos (sentencias/autos/
interlocutorias); ficha completa por slug con fundamentos COMPLETOS; y descarga del PDF
publicado por el TC.

Conexion directa (httpx), sin navegador y sin credenciales: todo el contenido
consultado es publico. El servidor nunca guarda archivos (salvo descargar_pdf) y
NUNCA trunca datos. Proyecto NO oficial, sin afiliacion con el TC ni el Estado peruano.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

logging.getLogger("httpx").setLevel(logging.WARNING)

BASE_API = os.getenv("TCJ_BASE", "https://jurisbackend.sedetc.gob.pe/api").rstrip("/")
HTTP_TIMEOUT = float(os.getenv("TCJ_TIMEOUT", "60"))
TOOL_TIMEOUT = float(os.getenv("TCJ_TOOL_TIMEOUT", "300"))
CACHE_TTL = float(os.getenv("TCJ_CACHE_TTL", "1800"))
UPDATE_CHECK = os.getenv("TCJ_UPDATE_CHECK", "1").strip() not in ("0", "false", "no")
DOWNLOADS_DIR = Path(os.getenv("TCJ_DOWNLOADS", str(Path.home() / "Downloads")))
PDF_CAP_BYTES = int(os.getenv("TCJ_PDF_CAP_MB", "20")) * 1024 * 1024

TAM_PAGINA = 10  # el backend pagina a 10 filas (verificado)

_REPO_GITHUB = "pipaacebedo/tc-juris-mcp"
_VERSION = "0.0.0"
try:
    _VERSION = str(json.loads(
        Path(__file__).resolve().parent.joinpath("manifest.json").read_text(encoding="utf-8")
    ).get("version", _VERSION))
except Exception:
    pass


def _semver_mayor(remota: str, instalada: str) -> bool:
    def partes(v: str) -> list[int]:
        return [int(x) for x in re.findall(r"\d+", v)[:3]] or [0]
    return partes(remota) > partes(instalada)


def _update_remota() -> str:
    """tag semver de un release mas nuevo que el instalado; '' si no hay, si esta
    desactivado (TCJ_UPDATE_CHECK=0) o si el chequeo falla (NUNCA lanza)."""
    if not UPDATE_CHECK:
        return ""
    try:
        r = httpx.get(f"https://api.github.com/repos/{_REPO_GITHUB}/releases?per_page=5",
                      headers={"Accept": "application/vnd.github+json", "User-Agent": "tc-juris-mcp"},
                      timeout=3.0)
        for rel in (r.json() or []):
            tag = str(rel.get("tag_name", ""))
            if re.fullmatch(r"v?\d+\.\d+(\.\d+)?", tag) and _semver_mayor(tag, _VERSION):
                return tag.lstrip("v")
        return ""
    except Exception:
        return ""


def _linea_update() -> str:
    remota = _update_remota()
    if not remota:
        return ""
    return (f"UPDATE DISPONIBLE - existe la version v{remota} (instalada v{_VERSION}): pide al usuario "
            f"descargar el .mcpb nuevo y reinstalarlo (https://github.com/{_REPO_GITHUB}/releases/latest); "
            "la cache de catalogos no se traslada entre versiones.\n")


INSTRUCCIONES = (
    "Servidor de la SISTEMATIZACION JURISPRUDENCIAL del Tribunal Constitucional del Peru (sentencias, "
    "autos e interlocutorias sistematizadas por el TC con su tesauro de conceptos y buscador avanzado). "
    "Lee en 4 niveles:\n"
    "0 DESCUBRIR - tres buses complementarios:\n"
    "  a) buscar_sentencias(search): TEXTO COMPLETO del contenido de las resoluciones (indice Elasticsearch "
    "del TC). El server NORMALIZA (quita tildes) porque el INDICE NO COINCIDE CON ACENTOS ('pension' si "
    "matchea; 'Pension' no). Giros verificados del backend: 1 palabra funciona mejor; el comodin '*' "
    "funciona ('funcionarios*'); frases con comillas devuelven 0; search multi-palabra puede colapsar "
    "en 0 o en el cap ES '>= 10000' (hay cap de 1000 paginas en este buscador).\n"
    "  b) busqueda_avanzada: filtros EXACTOS de la UI 'avanzado': numero_expediente, "
    "nombre_demandante/demandado, fecha_publicacion (exacta 'YYYY-MM-DD' O RANGO '2020-01-01,2020-12-31'), "
    "sentencia_sentido (id o nombre de listar_filtros categoria='sentidos'), "
    "id_sentencia_distrito/sala/tipo (id o nombre de listar_filtros), palabras (ids de listar_palabras, "
    "separados por coma). Sin cap practico de paginas (74k+ resultados).\n"
    "  c) resultados_sistematizacion(ids) y resultados_tesauro(ids, tipo): las sistematizaciones TEMATICAS "
    "del TC (6 ramas: precedentes vinculantes, tipos de procesos...) y el TESAURO de conceptos "
    "(derechos fundamentales/procesos de tutela...) con sub-arboles 'sentencias'. Obtén los 'ids' "
    "EXACTOS en listar_sistematizaciones y listar_tesauro (arbol NAVEGABLE: raices + parent_id/depth; "
    "nunca entrega el arbol entero de un bote).\n"
    "1 FILTROS EXACTOS - listar_filtros (tipo/distritos/salas/sentidos/tipos_tesauro), listar_tesauro "
    "(arboles con counts), listar_sistematizaciones (arbol) y listar_palabras (catalogo de 29 603 palabras). "
    "En busqueda_avanzada puedes pasar NOMBRE o ID; el server resuelve el id contra el catalogo y si no "
    "matchea devuelve error con ejemplos (JAMAS un filtro silencioso).\n"
    "2 FICHA - detalle_sentencia(slug) trae el expediente, sentencia, demandantes, fechas, sentidos, "
    "relacion de tesaurio, y los FUNDAMENTOS COMPLETOS (la decision; NUNCA se trunca) + la URL del PDF.\n"
    "3 DOCUMENTOS - descargar_pdf(url|slug) guarda el PDF publicado por el TC en la carpeta de descargas "
    "(cap 20 MB, NUNCA trunca; se puede subir con TCJ_PDF_CAP_MB).\n"
    "BUGS VERIFICADOS del backend del TC (no son del server): (i) search+id_sentencia_distrito juntos "
    "devuelven 0 aunque por separado funcionen; (ii) el buscador simple a veces devuelve el cap "
    "'>= 10000' para busquedas amplias: afina con busqueda_avanzada; (iii) 'tesauro_inter' "
    "(interlocutorias) esta vacio en el backend (counts 0); (iv) COMILLAS NO: una frase entre comillas "
    "en el buscador simple dispara el cap '>= 10000' (el server quitara las comillas y buscara la frase "
    "sin ellas; el indice NO soporta frases exactas); (v) FECHA EXACTA = ANIO: '2020-01-01' degrada al "
    "ano 2020 COMPLETO (136 = rango '2020-01-01,2020-12-31'); para un dia usa rango 'D,D'; (vi) "
    "numero_expediente funciona PARCIAL por subcadena ('00148' agrupa 00148-2026-HC, 00148-2025-AA...); "
    "(vii) 'palabras' multiples son OR (union), no AND; (viii) 'count_subarbol' de listar_tesauro es "
    "acumulado del subarbol y NO es el 'total' directo de resultados_tesauro (hoja si cuadra; nodo "
    "agregado difiere: ej raiz 63493 vs 7); (ix) el 'search' de resultados_sistematizacion opera sobre "
    "el TEXTO del fallo, no sobre nombres de partes (usa busqueda_avanzada con nombre_demandante); "
    "(x) el corpus contiene fechas espurias '1969-12-31' (epoch 0 del backend): desconfia de fechas de "
    "sentencia de 1969; (xi) el 'code' de los nodos del tesauro puede no cuadrar con su posicion en el "
    "arbol (dato upstream del TC): navega por 'id'; (xii) el filtro id_sentencia_distrito SOLO puede NO "
    "aplicarse en el backend (estres v3: id 3 devolvio filas de otros distritos): combina con "
    "fecha_publicacion/search/tipo/sala o confirma el campo 'distrito' de cada fila; (xiii) el catalogo "
    "de distritos incluye filas ESPURIAS upstream (nombres tipo '09-06-2003') y algunos nodos de "
    "sistematizacion no tienen contenido (total 0 con nota). El server NO oculta nada: trae 'total', "
    "'pagina', 'tamano_pagina' (10), 'index' GLOBAL 0-based continuo entre paginas, y 'nota' cuando "
    "aplica. Las filas con fecha_sentencia < 1990 viajan COMPLETAS marcadas 'fecha_sentencia_sospechosa': true.\n"
    "Politica: cada tool responde dentro de TCJ_TOOL_TIMEOUT (300 s) o devuelve {ok:false, error, pista}. "
    "Toda respuesta exitosa trae 'ms' (milisegundos). Fuente publica del TC: "
    "https://jurisprudencia.sedetc.gob.pe/sistematizacion-jurisprudencial/busqueda (NO oficial).\n"
)

INSTRUCCIONES += _linea_update()

mcp = FastMCP("Jurisprudencia del Tribunal Constitucional (No oficial)",
              instructions=INSTRUCCIONES, stateless_http=True)


# ------------------------------------------------------------------ errores

class ErrorSite(RuntimeError):
    pass


class ErrorValidacion(RuntimeError):
    pass


def _ms_desde(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _sin_acentos(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", str(s or ""))
                   if unicodedata.category(ch) != "Mn")


def _plain(s: str) -> str:
    s = _sin_acentos(s).strip().casefold()
    return re.sub(r"\s+", " ", s)


def _limpia(s: str | None) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip())


def _envolver(e: Exception, t0: float | None = None) -> dict[str, Any]:
    msg = str(e)
    out: dict[str, Any] = {"ok": False, "error": msg}
    if t0 is not None:
        out["ms"] = _ms_desde(t0)
    if "watchdog" in msg or "no respondio" in msg:
        out["pista"] = "el backend del TC respondio lento: reintenta la llamada; si persiste reduce filtros"
    elif "no encontrad" in msg or "no tiene la ficha" in msg:
        out["pista"] = "usa el 'slug' EXACTO de una fila (con guiones); los buses del server nunca omiten el slug"
    elif "HTTP 5" in msg or "no respondio" in msg:
        out["pista"] = "error del lado del TC: reintenta en unos segundos"
    elif "no matchea el catalogo" in msg or "del catalogo" in msg:
        out["pista"] = "usa listar_filtros / listar_tesauro / listar_sistematizaciones / listar_palabras y manda el valor EXACTO"
    elif "fuera de rango" in msg or ">= 1" in msg:
        out["pista"] = "revisa 'total' y 'paginas' de la ultima busqueda; el 'index' es GLOBAL 0-based"
    elif "cap" in msg.lower():
        out["pista"] = "NADA se trunca: acota la busqueda o pide el resto en otra llamada"
    elif "vacio" in msg:
        out["pista"] = "claves obligatorias: revisa los argumentos de la tool y reintenta"
    else:
        out["pista"] = "revisa los argumentos con listar_filtros/listar_tesauro y reintenta"
    if out.get("pista") and len(out["pista"]) > 240:
        out["pista"] = out["pista"][:239].rsplit(" ", 1)[0] + "…"
    return out


async def con_watchdog(coro: Any, tool: str) -> Any:
    try:
        return await asyncio.wait_for(coro, timeout=TOOL_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise ErrorSite(f"{tool} supero el watchdog de {TOOL_TIMEOUT:.0f}s: el sitio no respondio") from exc


# ---------------------------------------------------------------- motor HTTP

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                base_url=BASE_API,
                headers={
                    "User-Agent": f"tc-juris-mcp/{_VERSION} (MCP local; no oficial)",
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://jurisprudencia.sedetc.gob.pe/",
                },
                follow_redirects=True,
                timeout=httpx.Timeout(HTTP_TIMEOUT, connect=25.0),
            )
        return _client


async def _api(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET con 1 reintento ante red/HTTP 5xx; valida el JSON de la API del TC."""
    c = await _get_client()
    r: httpx.Response | None = None
    last: Exception | None = None
    for _i in range(2):
        try:
            r = await c.get(path, params=params)
            if r.status_code < 500:
                break
            last = ErrorSite(f"HTTP {r.status_code} en {path}")
        except httpx.HTTPError as exc:
            last = exc
    if r is None:
        raise ErrorSite(f"el backend del TC no respondio ({type(last).__name__})")
    if r.status_code >= 400:
        m = re.search(r'"message"\s*:\s*"([^"]{3,180})', r.text)
        det = m.group(1) if m else r.text[:160]
        raise ErrorSite(f"HTTP {r.status_code} en {path}: {det}")
    try:
        b = r.json()
    except Exception as exc:
        raise ErrorSite(f"respuesta no-JSON del backend en {path}: {r.text[:120]!r}") from exc
    if not isinstance(b, dict):
        raise ErrorSite(f"respuesta inesperada del backend en {path}")
    return b


# ------------------------------------------------------- catalogos en cache

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = asyncio.Lock()

_CAT_EPOINTS = {
    "tipo": "tipo",
    "distritos": "distrito",
    "salas": "salas",
    "sentidos": "sentido",
    "tipos_tesauro": "tipostesaurio",
    "sistematizaciones": "sistematizacion",
    "palabras": "palabras",
    "tesaurio": "tesaurio",
    "tesaurio_auto": "tesaurio_auto",
    "tesaurio_inter": "tesaurio_inter",
}


async def _catalogo(clave: str) -> Any:
    async with _CACHE_LOCK:
        hit = _CACHE.get(clave)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        b = await _api("/visitor/sentencia/" + _CAT_EPOINTS[clave])
        data = b.get("data", b)
        _CACHE[clave] = (time.time(), data)
        return data


def _flatten_nodos(data: Any) -> list[dict[str, Any]]:
    """aplana arboles con children (sistematizaciones/tesaurio*) en nodos planos."""
    out: list[dict[str, Any]] = []
    if not isinstance(data, list):
        return out
    for it in data:
        if not isinstance(it, dict):
            continue
        out.append(it)
        hijos = it.get("children")
        if isinstance(hijos, list):
            out.extend(_flatten_nodos(hijos))
    return out


async def _resolver(clave: str, val: str, campo: str) -> int:
    """id numerico directo, o nombre/slug EXACTO (sin acentos) del catalogo (los arboles se aplanan: matchea cualquier nivel)."""
    v = _limpia(val)
    if not v:
        raise ErrorValidacion(f"{campo} vacio: pasa el id o nombre exacto del catalogo '{clave}'")
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    p = _plain(v)
    data = _flatten_nodos(await _catalogo(clave))
    matcheos: list[int] = []
    for it in data:
        if (_plain(str(it.get("nombre") or "")) == p
                or _plain(str(it.get("slug") or "")) == p
                or _plain(str(it.get("descripcion") or "")) == p):
            matcheos.append(int(it["id"]))
    if len(matcheos) == 1:
        return matcheos[0]
    lista_ids = ", ".join(str(x) for x in matcheos[:12]) + ("..." if len(matcheos) > 12 else "")
    ejemplos = "; ".join(
        f"{i.get('id')}: {i.get('nombre') or i.get('descripcion')}" for i in data[:8]
    )
    if len(matcheos) > 1:
        raise ErrorValidacion(
            f"{campo}={v!r} matchea {len(matcheos)} ids ({lista_ids}): usa el id EXACTO (ejemplos: {ejemplos})")
    # coincidencia por subcadena (>= 3 caracteres); si matchea UNO, resuelve
    if len(p) >= 3:
        subs: list[int] = []
        for it in data:
            nm = _plain(str(it.get("nombre") or ""))
            sg = _plain(str(it.get("slug") or ""))
            if (p in nm or p in sg or p in _plain(str(it.get("descripcion") or ""))):
                subs.append(int(it["id"]))
        if len(subs) == 1:
            return subs[0]
        if len(subs) > 1:
            lista_sub = ", ".join(str(x) for x in subs[:12]) + ("..." if len(subs) > 12 else "")
            raise ErrorValidacion(
                f"{campo}={v!r} matchea {len(subs)} ids ({lista_sub}): usa el id EXACTO (ejemplos: {ejemplos})")
    raise ErrorValidacion(
        f"{campo}={v!r} no matchea el catalogo '{clave}': usa el id o el nombre/slug EXACTO "
        f"(ejemplos: {ejemplos})")


# filas y resumen de busquedas (todas las tools devuelven filas equivalentes)

def _fila_de(src: dict[str, Any], index: int) -> dict[str, Any]:
    def sub(fila: dict[str, Any], key: str, campo: str = "nombre") -> Any:
        v = fila.get(key)
        return v.get(campo) if isinstance(v, dict) else v

    arch = src.get("archivo")
    url = src.get("url_archivo")
    if not url and isinstance(arch, dict):
        url = arch.get("url") or arch.get("path")
    f: dict[str, Any] = {
        "index": index,
        "slug": src.get("slug"),
        "numero_expediente": src.get("numero_expediente"),
        "numero_sentencia": src.get("numero_sentencia"),
        "demandante": src.get("nombre_demandante"),
        "demandado": src.get("nombre_demandado"),
        "fecha_publicacion": src.get("fecha_publicacion"),
        "fecha_sentencia": src.get("fecha_sentencia"),
        "sentido": sub(src, "sentido") or sub(src, "sentencia_sentido"),
        "tipo": sub(src, "tipo"),
        "distrito": sub(src, "sentencia_distrito") or sub(src, "distrito"),
        "sala": sub(src, "sentencia_sala") or sub(src, "sala"),
        "sistematizacion": sub(src, "sistematizacion"),
        "tesaurio": [{"id": t.get("id"), "nombre": t.get("nombre")}
                     for t in (src.get("tesaurio") or []) if isinstance(t, dict)],
        "tipo_archivo": src.get("tipo_archivo"),
        "url_pdf": url,
        "extractos": src.get("fundamentos") or None,  # fragmentos ES que matchean (texto integro: detalle_sentencia)
    }
    # estres v3 (MAYOR): fechas espurias epoch-0 del corpus del TC (dato VIAJA COMPLETO + aviso)
    fs = str(src.get("fecha_sentencia") or "")
    if fs[:4].isdigit() and int(fs[:4]) < 1990:
        f["fecha_sentencia_sospechosa"] = True
    return {k: v for k, v in f.items() if v not in (None, "", [], {})}


def _resumen(data: dict[str, Any], pagina: int, filas: list[dict[str, Any]],
             extra: dict[str, Any] | None = None) -> dict[str, Any]:
    total = data.get("total")
    rel = "eq"
    if isinstance(total, dict):
        rel = str(total.get("relation") or "eq")
        total = total.get("value")
    total = int(total or 0)
    paginas = max(1, -(-total // TAM_PAGINA)) if total else 1
    if rel == "gte" and paginas > 1000:
        paginas = 1000
    if rel == "gte" and total > 9999:
        total = 10000
    out: dict[str, Any] = {
        "ok": True,
        "total": total,
        "pagina": pagina,
        "tamano_pagina": TAM_PAGINA,
        "paginas": paginas,
        "filas": filas,
    }
    if total and pagina * TAM_PAGINA < total:
        out["siguiente_pagina"] = pagina + 1
    # F1 (estres v2): page valida (<=1000) pero fuera del total real -> nota
    # especifica con PRECEDENCIA sobre las notas genericas de "sin resultados"
    fuera = bool(total and not filas and pagina > 1 and (pagina - 1) * TAM_PAGINA >= total)
    if fuera:
        out["nota"] = (f"page {pagina} fuera de rango: la busqueda tiene {paginas} paginas "
                       f"(total {total}); pide page <= {paginas}")
    elif rel == "gte":
        out["nota"] = f"total capado por el backend TC en '>= {total}' (indice Elasticsearch)"
    if extra and not fuera:
        out.update(extra)
    return out


async def _filas_de(data: dict[str, Any], pagina: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    i = (max(1, pagina) - 1) * TAM_PAGINA
    for item in data.get("data") or []:
        src = item.get("_source", item) if isinstance(item, dict) else item
        if isinstance(src, dict):
            out.append(_fila_de(src, i))
        i += 1
    return out


# -------------------------------------------------------------------- tools

@mcp.tool(title="Buscando sentencias")
async def buscar_sentencias(search: str, page: int = 1) -> dict[str, Any]:
    """Busca en el TEXTO COMPLETO de todas las resoluciones del TC (indice Elasticsearch del portal). El server QUITA TILDES de 'search' (el indice del TC NO matchea acentos: 'pension' si, 'Pension' no).

    Args:
        search: texto a buscar en el contenido (1 palabra ideal; el comodin '*' funciona: 'funcionarios*'; frases con comillas dan 0). Con total 0 prueba con otra palabra clave o pasa a busqueda_avanzada.
        page: pagina 1-based (cap del backend ES: 1000).

    Devuelve:
        {'ok', 'total' (puede venir capado en 10000/gte), 'pagina', 'tamano_pagina', 'paginas',
        'siguiente_pagina', 'filas': [{index, slug, numero_expediente, numero_sentencia, demandante,
        demandado, fecha_publicacion, fecha_sentencia, sentido, tipo, distrito, sala, tesaurio, url_pdf,
        extractos (fragmentos del texto que matchean; el texto INTEGRO de cada resolucion se obtiene
        con detalle_sentencia)}], 'ms', 'nota'?}: 'index' es GLOBAL; usa 'slug' con detalle_sentencia.
    """
    t0 = time.perf_counter()
    q = _limpia(search)
    if not q:
        return {"ok": False, "error": "search vacio",
                "pista": "palabra clave o frase corta, ej search='prescripcion adquisitiva'",
                "ms": _ms_desde(t0)}
    habia_comillas = ('"' in q or "'" in q)
    q = q.replace('"', "").replace("'", "").strip()
    if not q:
        return {"ok": False, "error": "solo comillas en search",
                "pista": "el indice del TC no soporta frases exactas: pasa palabras SIN comillas",
                "ms": _ms_desde(t0)}
    page = int(page)
    if page < 1:
        return {"ok": False, "error": "page debe ser >= 1", "pista": "el backend rechaza pagina 0/negativa",
                "ms": _ms_desde(t0)}
    if page > 1000:
        return {"ok": False, "error": "page > 1000: el backend ES del TC cap las busquedas simples a 1000 paginas",
                "pista": "usa busqueda_avanzada con filtros (ese path SQL si pagina mas a fondo)",
                "ms": _ms_desde(t0)}
    try:
        async def _impl() -> dict[str, Any]:
            data = await _api("/visitor/sentencia/busqueda", params={"search": _plain(q), "page": page})
            filas = await _filas_de(data, page)
            extra: dict[str, Any] = {}
            if habia_comillas:
                extra["nota"] = ("el indice del TC NO soporta frases exactas (las comillas disparan el "
                                 "peor caso: cap ES '>= 10000'): se busco la frase SIN comillas; el texto "
                                 "integro por resolucion esta en detalle_sentencia")
            elif not filas:
                if len(_plain(q).split()) > 1:
                    extra["nota"] = ("total 0 con search multi-palabra: el indice del TC matchea mejor con UNA "
                                     "palabra clave (o 'palabra*' con comodin); prueba variando la palabra")
                else:
                    extra["nota"] = ("sin resultados con esa palabra; prueba otra clave de la misma raiz "
                                     "o pasa a busqueda_avanzada con filtros")
            out = _resumen(data, page, filas, extra)
            out["ms"] = _ms_desde(t0)
            return out
        return await con_watchdog(_impl(), "buscar_sentencias")
    except (ErrorSite, ErrorValidacion) as e:
        return _envolver(e, t0)


@mcp.tool(title="Buscando con filtros avanzados")
async def busqueda_avanzada(search: str | None = None,
                            numero_expediente: str | None = None,
                            nombre_demandante: str | None = None,
                            nombre_demandado: str | None = None,
                            fecha_publicacion: str | None = None,
                            sentencia_sentido: str | None = None,
                            id_sentencia_distrito: str | None = None,
                            id_sentencia_sala: str | None = None,
                            id_sentencia_tipo: str | None = None,
                            palabras: str | None = None,
                            page: int = 1) -> dict[str, Any]:
    """Busqueda AVANZADA del TC (la pestaña 'avanzado' del portal): texto, expediente, partes, fecha o RANGO, sentido, distrito, sala, tipo y palabras claves por id. Los nombres/slug del catalogo son aceptados y el server resuelve el id.

    Args:
        search: texto completo (server quita tildes).
        numero_expediente: ej '00148-2026-HC'.
        nombre_demandante: nombre EXACTO de la parte (ej 'Fernando Catalan Contreras').
        nombre_demandado: nombre EXACTO de la entidad demandada.
        fecha_publicacion: 'YYYY-MM-DD' o RANGO con comas ('2020-01-01,2020-12-31'). VERIFICADO: una fecha EXACTA opera como el ANIO COMPLETO (el backend la degrada); para un dia exacto usa rango '2020-01-01,2020-01-01'.
        sentencia_sentido: id o nombre EXACTO ('Improcedente', 'Fundada en parte'; catalogo listar_filtros categoria='sentidos').
        id_sentencia_distrito: id o nombre ('Lima'; listar_filtros categoria='distritos').
        id_sentencia_sala: id o nombre ('Sala 1'; categoria='salas').
        id_sentencia_tipo: id o nombre ('Sentencia'; categoria='tipo').
        palabras: ids (o nombres exactos) del catalogo de palabras (listar_palabras), separados por coma ('1,3').
        page: pagina 1-based (sin cap practico en este path SQL).

    Devuelve:
        {'ok', 'total', 'pagina', 'tamano_pagina', 'paginas', 'siguiente_pagina', 'filas': [{index, slug,
        numero_expediente, numero_sentencia, demandante, demandado, fecha_publicacion, fecha_sentencia,
        sentido, tipo, distrito, sala, sistematizacion, tesaurio, tipo_archivo, url_pdf}], 'ms', 'nota'?}.
    """
    t0 = time.perf_counter()
    page = int(page)
    if page < 1:
        return {"ok": False, "error": "page debe ser >= 1", "pista": "paginas validas desde 1",
                "ms": _ms_desde(t0)}
    fecha = _limpia(fecha_publicacion)
    if fecha and not re.fullmatch(r"\d{4}-\d{2}-\d{2}(,\d{4}-\d{2}-\d{2})*", fecha):
        return {"ok": False, "error": f"fecha_publicacion={fecha_publicacion!r} mal formada",
                "pista": "'YYYY-MM-DD' exacta o rango con comas '2020-01-01,2020-12-31'",
                "ms": _ms_desde(t0)}
    try:
        async def _impl() -> dict[str, Any]:
            params: dict[str, Any] = {"page": page}
            if _limpia(search):
                params["search"] = _plain(search)
            if _limpia(numero_expediente):
                params["numero_expediente"] = _limpia(numero_expediente)
            if _limpia(nombre_demandante):
                params["nombre_demandante"] = _limpia(nombre_demandante)
            if _limpia(nombre_demandado):
                params["nombre_demandado"] = _limpia(nombre_demandado)
            if fecha:
                params["fecha_publicacion"] = fecha
            if _limpia(sentencia_sentido):
                params["sentencia_sentido"] = await _resolver("sentidos", sentencia_sentido, "sentencia_sentido")
            if _limpia(id_sentencia_distrito):
                params["id_sentencia_distrito"] = await _resolver("distritos", id_sentencia_distrito, "id_sentencia_distrito")
            if _limpia(id_sentencia_sala):
                params["id_sentencia_sala"] = await _resolver("salas", id_sentencia_sala, "id_sentencia_sala")
            if _limpia(id_sentencia_tipo):
                params["id_sentencia_tipo"] = await _resolver("tipo", id_sentencia_tipo, "id_sentencia_tipo")
            if _limpia(palabras):
                ids: list[str] = []
                for p in re.split(r"[;,]\s*", _limpia(palabras)):
                    if p.strip():
                        ids.append(p.strip() if re.fullmatch(r"-?\d+", p.strip())
                                   else str(await _resolver("palabras", p, "palabras")))
                params["palabras"] = ",".join(ids)
            data = await _api("/visitor/sentencia/busqueda/avanzada", params=params)
            filas = await _filas_de(data, page)
            extra: dict[str, Any] = {}
            if not filas:
                has_search = "search" in params
                has_dist = "id_sentencia_distrito" in params
                nfiltros = len(params) - 1
                if has_search and has_dist:
                    extra["nota"] = ("BUG verificado del backend TC: search+id_sentencia_distrito juntos dan 0 "
                                     "aunque por separado si encuentren; usa search sin distrito o distrito sin search")
                elif nfiltros >= 2:
                    extra["nota"] = "sin resultados con esa combinacion; prueba quitar un filtro o acotar palabras"
            elif list(params.keys()) == ["page", "id_sentencia_distrito"]:
                # estres v3 (MAYOR): el backend puede NO aplicar el filtro de distrito aislado
                extra = {"nota": ("VERIFICADO en estres: el backend del TC puede NO aplicar el filtro de "
                                  "distrito cuando va SOLO (filas de otros distritos en la respuesta): "
                                  "agrega un segundo filtro (fecha_publicacion/search/tipo/sala) o confirma "
                                  "el campo 'distrito' de cada fila antes de citar")}
            out = _resumen(data, page, filas, extra)
            out["ms"] = _ms_desde(t0)
            return out
        return await con_watchdog(_impl(), "busqueda_avanzada")
    except (ErrorSite, ErrorValidacion) as e:
        return _envolver(e, t0)


@mcp.tool(title="Buscando por sistematizacion")
async def resultados_sistematizacion(sistematizaciones: str, search: str | None = None,
                                     numero_expediente: str | None = None,
                                     page: int = 1) -> dict[str, Any]:
    """Resultados por concepto SISTEMATIZADO del TC (la vista 'sistematizacion' del portal): precedentes vinculantes, tipos de proceso (habeas corpus, amparo, cumplimiento...), y las sub-categorias del arbol. Los 'id' EXACTOS vienen de listar_sistematizaciones.

    Args:
        sistematizaciones: id(s) separados por coma ('268' = Habeas corpus; '1' = Precedentes vinculantes; varios: '268,269').
        search: texto adicional a filtrar dentro de esa sistematizacion (server quita tildes).
        numero_expediente: filtra por expediente adicional (ej '00148-2026-HC').
        page: pagina 1-based.

    Devuelve:
        {'ok', 'total', 'pagina', 'tamano_pagina', 'paginas', 'siguiente_pagina', 'filas': [filas comunes],
        'ms', 'nota'?}: 'index' GLOBAL y 'slug' para detalle_sentencia.
    """
    t0 = time.perf_counter()
    page = int(page)
    if page < 1:
        return {"ok": False, "error": "page debe ser >= 1", "pista": "paginas validas desde 1",
                "ms": _ms_desde(t0)}
    if not _limpia(sistematizaciones):
        return {"ok": False, "error": "sistematizaciones vacio",
                "pista": "id(s) del arbol: usa listar_sistematizaciones (ej '268')",
                "ms": _ms_desde(t0)}
    try:
        async def _impl() -> dict[str, Any]:
            ids = []
            for p in re.split(r"[;,]\s*", _limpia(sistematizaciones)):
                if p.strip():
                    ids.append(str(await _resolver("sistematizaciones", p, "sistematizaciones")))
            params: dict[str, Any] = {"page": page, "sistematizaciones": ",".join(ids)}
            if _limpia(search):
                params["search"] = _plain(search)
            if _limpia(numero_expediente):
                params["numero_expediente"] = _limpia(numero_expediente)
            data = await _api("/visitor/sentencia/sistematizacion/resultados", params=params)
            filas = await _filas_de(data, page)
            extra: dict[str, Any] = {}
            if not filas:
                if "search" in params:
                    extra["nota"] = ("sin resultados: el 'search' de este path opera sobre el TEXTO del "
                                     "fallo (fragmentos), NO sobre nombres de partes; para partes usa "
                                     "busqueda_avanzada(nombre_demandante=/nombre_demandado=)")
                else:
                    extra["nota"] = ("sin resultados con ese id: confirma que venga de listar_sistematizaciones "
                                     "y que el nodo sea hoja relevante (el backend del TC solo responde ids "
                                     "con contenido)")
            out = _resumen(data, page, filas, extra)
            out["ms"] = _ms_desde(t0)
            return out
        return await con_watchdog(_impl(), "resultados_sistematizacion")
    except (ErrorSite, ErrorValidacion) as e:
        return _envolver(e, t0)


@mcp.tool(title="Buscando por tesaurio")
async def resultados_tesauro(tesaurios: str, tipo: str = "sentencias", page: int = 1) -> dict[str, Any]:
    """Resultados por concepto del TESAURO del TC (acumulados con 'count' por nodo; el id viene del arbol de listar_tesauro). 'Derecho a la vida' (id de tesauro=sentencias), autos de tutela de derechos, interlocutorias.

    Args:
        tesaurios: id(s) separados por coma del arbol ELEGIDO (ej '4' = Derecho a la vida; '1,2' = multi).
        tipo: 'sentencias' (default), 'autos' o 'interlocutorias' (EXACTO).
        page: pagina 1-based.

    Devuelve:
        {'ok', 'total', 'pagina', 'tamano_pagina', 'paginas', 'siguiente_pagina', 'filas': [filas comunes],
        'ms', 'nota'?}: ATENTO: el 'total' cuenta los documentos asignados DIRECTAMENTE al nodo; el
        'count_subarbol' de listar_tesauro es el ACUMULADO de todo el subarbol (publicado por el TC) y
        NO coincide en nodos agregados (ej: raiz 63493 vs total directo 7; hoja '4' si cuadra con 50).
    """
    t0 = time.perf_counter()
    tint = _plain(tipo or "sentencias")
    knowns = {"sentencias": "tesaurio", "autos": "tesaurio_auto", "interlocutorias": "tesaurio_inter"}
    if tint not in knowns:
        return {"ok": False, "error": f"tipo {tipo!r} desconocido",
                "pista": "usa: " + ", ".join(sorted(knowns)), "ms": _ms_desde(t0)}
    page = int(page)
    if page < 1:
        return {"ok": False, "error": "page debe ser >= 1", "pista": "paginas validas desde 1",
                "ms": _ms_desde(t0)}
    if not _limpia(tesaurios):
        return {"ok": False, "error": "tesaurios vacio",
                "pista": "id(s) del arbol: usa listar_tesauro (ej '4')", "ms": _ms_desde(t0)}
    try:
        async def _impl() -> dict[str, Any]:
            ids = []
            for p in re.split(r"[;,]\s*", _limpia(tesaurios)):
                if p.strip():
                    ids.append(str(await _resolver(knowns[tint], p, "tesaurios")))
            data = await _api(f"/visitor/sentencia/{knowns[tint]}/resultados",
                              params={"page": page, "tesaurios": ",".join(ids)})
            filas = await _filas_de(data, page)
            extra: dict[str, Any] = {}
            if not filas and tint == "interlocutorias":
                extra["nota"] = ("el tesaurio de interlocutorias esta VACIO en el backend (counts en 0): "
                                 "usa 'sentencias' o 'autos'")
            elif not filas:
                extra["nota"] = ("sin resultados con ese id: confirma que venga de listar_tesauro y que "
                                 "el nodo tenga count>0")
            out = _resumen(data, page, filas, extra)
            out["ms"] = _ms_desde(t0)
            return out
        return await con_watchdog(_impl(), "resultados_tesauro")
    except (ErrorSite, ErrorValidacion) as e:
        return _envolver(e, t0)


@mcp.tool(title="Obteniendo ficha de sentencia")
async def detalle_sentencia(slug: str) -> dict[str, Any]:
    """FICHA COMPLETA de una sentencia/auto/interlocutoria del TC por slug: expediente, sentencia, demandante/demandado, fechas, sentido, contexto de sala, tesaurio de conceptos y fundamentos COMPLETOS (la decision integral; NUNCA truncada) y URL del PDF del TC.

    Args:
        slug: el 'slug' de filas de los buses (ej '00148-2026-hc-1440-2026').

    Devuelve:
        {'ok', 'slug', 'detalle': {todo el contenido publicado por el TC para esa resolucion (con
        'fundamentos' COMPLETOS y 'url_archivo'/PDF)}, 'ms', 'nota'?}.
    """
    t0 = time.perf_counter()
    slug = _limpia(slug)
    if not slug:
        return {"ok": False, "error": "slug vacio", "pista": "usa el 'slug' de una fila de los buses",
                "ms": _ms_desde(t0)}
    slug = slug.lstrip("/")
    try:
        async def _impl() -> dict[str, Any]:
            d = await _api("/visitor/sentencia/" + slug)
            data = d.get("data")
            if not data:
                raise ErrorValidacion(f"el backend no tiene la ficha de {slug!r}")
            src = data.get("_source", data) if isinstance(data, dict) else data
            out: dict[str, Any] = {"ok": True, "slug": slug, "detalle": src}
            if isinstance(src, dict) and not src.get("url_archivo"):
                out["nota"] = "la ficha no expone url del PDF (numero de archivo del TC sin registro en uploads)"
            out["ms"] = _ms_desde(t0)
            return out
        return await con_watchdog(_impl(), "detalle_sentencia")
    except (ErrorSite, ErrorValidacion) as e:
        return _envolver(e, t0)


@mcp.tool(title="Descargando PDF de sentencia")
async def descargar_pdf(url: str | None = None, slug: str | None = None) -> dict[str, Any]:
    """Descarga el PDF de una resolucion del TC a la carpeta de descargas (env TCJ_DOWNLOADS; default ~/Downloads). Acepta 'url' (url_pdf de la fila/ficha) o 'slug' (resuelve la ficha y toma su url_archivo). Cap 20 MB (env TCJ_PDF_CAP_MB); NUNCA trunca: si excede, error con el tamano.

    Args:
        url: URL del PDF (preferente).
        slug: alternativa para resolver la URL desde la ficha.

    Devuelve:
        {'ok', 'ruta', 'bytes', 'sha256', 'url', 'ms'}: archivo guardado. {'ok': False, 'error', 'pista'}
        frente a 404/movido/cap.
    """
    t0 = time.perf_counter()
    if not _limpia(url) and not _limpia(slug):
        return {"ok": False, "error": "debes pasar 'url' o 'slug'",
                "pista": "usa la 'url_pdf'/'url_archivo' de las filas o el 'slug' de la ficha",
                "ms": _ms_desde(t0)}
    try:
        async def _impl() -> dict[str, Any]:
            if _limpia(slug) and not _limpia(url):
                d = await _api("/visitor/sentencia/" + _limpia(slug).lstrip("/"))
                data = d.get("data")
                if not data:
                    raise ErrorValidacion(f"el backend no tiene la ficha de {slug!r}")
                src = data.get("_source", data) if isinstance(data, dict) else data
                u = (src or {}).get("url_archivo") or ""
            else:
                u = _limpia(url)
            if not u:
                return {"ok": False, "error": f"la ficha {slug!r} no expone url del PDF",
                        "pista": "usa la fila/ficha con url_pdf; si no, descarga manual desde el portal del TC",
                        "ms": _ms_desde(t0)}
            if not u.lower().startswith("http"):
                return {"ok": False, "error": "url malformada",
                        "pista": "debe ser 'https://...'", "ms": _ms_desde(t0)}
            c = await _get_client()
            r = await c.get(u)
            ctype = r.headers.get("content-type", "")
            if r.status_code != 200 or "pdf" not in ctype.lower():
                return {"ok": False,
                        "error": f"la URL no sirvio un PDF (HTTP {r.status_code}, content-type {ctype!r})",
                        "pista": "el PDF pudo moverse; prueba la URL en el navegador o revisa la ficha",
                        "ms": _ms_desde(t0)}
            raw = r.content
            if len(raw) > PDF_CAP_BYTES:
                return {"ok": False, "error": f"el PDF pesa {len(raw)} bytes y supera el cap de {PDF_CAP_BYTES}",
                        "pista": "NADA fue truncado; sube TCJ_PDF_CAP_MB o descarga manual; la URL viene en la respuesta",
                        "url": u, "tamano_bytes": len(raw), "cap_bytes": PDF_CAP_BYTES,
                        "ms": _ms_desde(t0)}
            DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
            parte = u.rstrip("/").split("/")[-1].split("?")[0] or "sentencia.pdf"
            if not parte.lower().endswith(".pdf"):
                parte += ".pdf"
            parte = re.sub(r"[^A-Za-z0-9_.\-]+", "_", parte)
            ruta = DOWNLOADS_DIR / parte
            n = 1
            while ruta.exists():
                ruta = DOWNLOADS_DIR / (parte[:-4] + f"_{n}.pdf")
                n += 1
            ruta.write_bytes(raw)
            return {"ok": True, "ruta": str(ruta), "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(), "url": u,
                    "ms": _ms_desde(t0)}
        return await con_watchdog(_impl(), "descargar_pdf")
    except (ErrorSite, ErrorValidacion) as e:
        return _envolver(e, t0)


@mcp.tool(title="Listando arboles del tesaurio")
async def listar_tesauro(tipo: str = "sentencias", parent_id: str | None = None,
                         depth: int = 1) -> dict[str, Any]:
    """Arbol de conceptos del TESAURO del TC NAVEGABLE (el arbol completo puede exceder el limite de contexto del cliente: NUNCA se entrega de un bote; se navega). SIN parent_id devuelve las RAICES (nivel 1) con su 'count_subarbol' (acumulado de todo el subarbol). Con parent_id devuelve el SUBARBUS del nodo hasta 'depth' niveles.

    Args:
        tipo: 'sentencias' (default), 'autos' o 'interlocutorias'.
        parent_id: id del nodo padre (de una respuesta previa); omitido/'' = raices del arbol.
        depth: niveles de children a incluir bajo el parent (1..4; default 1; cap local a 4).

    Devuelve:
        {'ok', 'tipo', 'parent_id', 'depth', 'nodos': [{id, nombre, slug, code, count_subarbol,
        children? (si depth>1)}], 'ms', 'nota'?}: cada nodo trae 'count_subarbol' (el TOTAL ACUMULADO
        del subarbol, publicado por el TC) y 'nota' cuando el nodo no existe o no tiene children.
        IMPORTANTISIMO: los 'count_subarbol' del arbol NO son el 'total' de resultados_tesauro (que
        cuenta solo asignaciones DIRECTAS al nodo): el acumulado suma todo el subarbol. El 'code'
        del TC puede NO cuadrar con la posicion del nodo (dato upstream inconsistente, ej nodo 458
        con code 6.6.7 colgando de la rama 1.1.2): NAVEGA por 'id', nunca reconstruyas la ruta por 'code'.
    """
    t0 = time.perf_counter()
    tint = _plain(tipo or "sentencias")
    knowns = {"sentencias": "tesaurio", "autos": "tesaurio_auto", "interlocutorias": "tesaurio_inter"}
    if tint not in knowns:
        return {"ok": False, "error": f"tipo {tipo!r} desconocido",
                "pista": "usa: " + ", ".join(sorted(knowns)), "ms": _ms_desde(t0)}
    depth = max(1, min(int(depth), 4))
    pid = _limpia(parent_id)
    try:
        tree = await con_watchdog(_catalogo(knowns[tint]), "listar_tesauro")

        def _strip(n: dict[str, Any], d: int) -> dict[str, Any]:
            """nodo con children hasta la depth pedida; renombra count -> count_subarbol."""
            out = {"count_subarbol": n.get("count")} if n.get("count") is not None else {}
            for k, v in n.items():
                if k in ("children", "count"):
                    continue
                out[k] = v
            sub = n.get("children")
            if d > 0 and isinstance(sub, list) and sub:
                out["children"] = [_strip(c, d - 1) for c in sub]
            return out

        def _find(nodes: list[dict[str, Any]], want: int) -> dict[str, Any] | None:
            for nd in nodes:
                if int(nd.get("id") or -1) == want:
                    return nd
                hit = _find(nd.get("children") or [], want)
                if hit is not None:
                    return hit
            return None

        nodos: list[dict[str, Any]] = []
        if not pid:
            roots = tree if isinstance(tree, list) else []
            nodos = [_strip(n, depth - 1) for n in roots]
            nota_nav = "raices del arbol (nivel 1); pide parent_id=<id> para profundizar en un nodo"
        else:
            if not re.fullmatch(r"-?\d+", pid):
                return {"ok": False, "error": f"parent_id {parent_id!r} mal formado",
                        "pista": "usa el 'id' numerico de un nodo previo (o '' para las raices)",
                        "ms": _ms_desde(t0)}
            parent = _find(tree if isinstance(tree, list) else [], int(pid))
            if parent is None:
                return {"ok": False, "error": f"parent_id {pid} no existe en el arbol '{tint}'",
                        "pista": "usa un 'id' de las respencias previas de listar_tesauro (o '' para raices)",
                        "ms": _ms_desde(t0)}
            hijos = parent.get("children")
            if not isinstance(hijos, list):
                hijos = []
            nodos = [_strip(c, depth - 1) for c in hijos]
            nota_nav = (f"subarbol de {pid} hasta depth {depth}: {len(nodos)} children directos; "
                        "repite con parent_id de un child para seguir bajando")
        out: dict[str, Any] = {"ok": True, "tipo": tint, "parent_id": pid, "depth": depth,
                               "nodos": nodos, "nota": nota_nav}
        if tint == "interlocutorias" and pid == "":
            out["nota"] = (nota_nav + "; el backend no expone datos de interlocutorias (counts en 0)")
        out["ms"] = _ms_desde(t0)
        return out
    except (ErrorSite, ErrorValidacion) as e:
        return _envolver(e, t0)


@mcp.tool(title="Listando sistematizaciones")
async def listar_sistematizaciones() -> dict[str, Any]:
    """Arbol COMPLETO de la SISTEMATIZACION del TC (6 ramas tematicas con children por tipo/nodo). Usa los 'id' en resultados_sistematizacion (ej '1' = Precedentes vinculantes, '268' = Habeas corpus).

    Devuelve:
        {'ok', 'sistematizacion': [{id, nombre, slug, children: [...]}], 'ms'}.
    """
    t0 = time.perf_counter()
    try:
        async def _impl() -> dict[str, Any]:
            tree = await _catalogo("sistematizaciones")
            out: dict[str, Any] = {"ok": True, "sistematizacion": tree}
            out["nota"] = ("algunos nodos del arbol pueden NO tener contenido en el backend (su "
                           "resultados_sistematizacion devuelve total 0 con nota); el error de nombre "
                           "ambiguo lista los ids candidatos")
            out["ms"] = _ms_desde(t0)
            return out
        return await con_watchdog(_impl(), "listar_sistematizaciones")
    except (ErrorSite, ErrorValidacion) as e:
        return _envolver(e, t0)


@mcp.tool(title="Listando palabras clave")
async def listar_palabras(letra: str | None = None, contiene: str | None = None,
                          page: int = 1, paginas: int = 1) -> dict[str, Any]:
    """Catalogo de PALABRAS CLAVES del TC (29 603 exactas) con filtrado LOCAL por letra inicial o por contenido (sin acentos). Usa los 'id' en busqueda_avanzada(palabras='1,3').

    Args:
        letra: letra inicial (ej 'a'); filtrado LOCAL (el backend no filtra).
        contiene: sub-cadena a buscar (LOCAL; sin acentos: la comparacion es case-insensitive).
        page: pagina del resultado FILTRADO (100 palabras por pagina).
        paginas: paginas consecutivas a traer en UNA llamada (1..3; si buscas mas hay 'nota' de recorte).

    Devuelve:
        {'ok', 'total_filtradas', 'pagina', 'paginas_filtradas', 'tamano_pagina': 100, 'siguiente_pagina',
        'palabras': [{id, nombre, slug}], 'ms', 'nota'?}.
    """
    t0 = time.perf_counter()
    try:
        all_w = await con_watchdog(_catalogo("palabras"), "listar_palabras")
    except (ErrorSite, ErrorValidacion) as e:
        return _envolver(e, t0)
    letraP = _plain(letra or "")
    contP = _plain(contiene or "")
    filtradas: list[dict[str, Any]] = []
    for w in all_w:
        nombre = str(w.get("nombre") or "")
        nm = _plain(nombre)
        slug = _plain(str(w.get("slug") or ""))
        if letraP and not nm.startswith(letraP) and not slug.startswith(letraP):
            continue
        if contP and contP not in nm and contP not in slug:
            continue
        filtradas.append({"id": w.get("id"), "nombre": nombre, "slug": w.get("slug")})
    total = len(filtradas)
    page = int(page)
    paginas_real = max(1, min(int(paginas), 3))
    TAM = 100
    if page < 1:
        return {"ok": False, "error": "page debe ser >= 1",
                "pista": "1..paginas_filtradas de 100 palabras por pagina", "ms": _ms_desde(t0)}
    paginas_f = max(1, -(-total // TAM))
    if total == 0:
        return {"ok": True, "total_filtradas": 0, "pagina": page, "paginas_filtradas": 0,
                "tamano_pagina": TAM, "palabras": [],
                "nota": "sin coincidencias con letra/contiene; prueba menos caracteres (el catalogo es EXACTO, sin tildes al filtrar)",
                "ms": _ms_desde(t0)}
    if (page - 1) * TAM >= total:
        return {"ok": False, "error": f"page {page} fuera de rango: hay {paginas_f} paginas filtradas",
                "pista": "rebaja page o usa pagina 1", "ms": _ms_desde(t0)}
    ini = (page - 1) * TAM
    troza = filtradas[ini: ini + TAM * paginas_real]
    out: dict[str, Any] = {
        "ok": True, "total_filtradas": total, "pagina": page, "paginas_filtradas": paginas_f,
        "tamano_pagina": TAM, "palabras": troza,
    }
    if ini + len(troza) < total:
        out["siguiente_pagina"] = page + paginas_real
    recort = int(paginas) - paginas_real
    if recort > 0:
        out["nota"] = _nota(3, recort)
    out["ms"] = _ms_desde(t0)
    return out


def _nota(cap: int, recort: int, unidad: str = "paginas") -> str:
    return f"se recortaron {recort} {unidad} por el cap de {cap}; pide el resto en otra llamada"


@mcp.tool(title="Listando filtros")
async def listar_filtros(categoria: str = "todos", page: int = 1, paginas: int = 1) -> dict[str, Any]:
    """Catálogo EXACTO para busqueda_avanzada: 'tipo' (tipos de resolucion, 224 registros), 'distritos' (115), 'salas' (5), 'sentidos' (417 con patron ES 'search_elastic'), 'tipos_tesauro' (3). 'todos' trae SOLO el recuento de cada catalogo. Las categorias de detalle se PAGINAN localmente (100 valores por pagina; la respuesta de sentidos completa pesa ~86 KB y puede ser truncada por el cliente: usa page).

    Args:
        categoria: 'todos' (resumen de counts) o 'tipo', 'distritos', 'salas', 'sentidos', 'tipos_tesauro'.
        page: pagina del detalle (1-based; 100 valores por pagina; solo aplica a categorias de detalle).
        paginas: paginas consecutivas a traer en UNA llamada (1..3; si pides mas hay 'nota' de recorte).

    Devuelve:
        {'ok', 'categoria', 'valores': [{id, nombre, slug, search_elastic?(sentidos)}], 'total',
        'pagina', 'paginas_filtradas', 'siguiente_pagina'?, 'ms', 'nota'?}: los 'id' alimentan
        busqueda_avanzada; los nombres son EXACTOS (el server resuelve id con nombre/slug).
    """
    t0 = time.perf_counter()
    cat = _plain(categoria or "todos")
    known = {"todos", "tipo", "distritos", "salas", "sentidos", "tipos_tesauro"}
    if cat not in known:
        return {"ok": False, "error": f"categoria {categoria!r} desconocida",
                "pista": "usa: " + ", ".join(sorted(known)), "ms": _ms_desde(t0)}
    out: dict[str, Any] = {"ok": True, "categoria": cat}
    if cat == "todos":
        resumen: dict[str, Any] = {}
        for c in ("tipo", "distritos", "salas", "sentidos", "tipos_tesauro"):
            try:
                resumen[c] = {"total": len(await con_watchdog(_catalogo(c), "listar_filtros"))}
            except (ErrorSite, ErrorValidacion) as ee:
                resumen[c] = {"error": str(ee)}
        resumen["palabras"] = {"nota": "usa listar_palabras (catalogo EXACTO de 29 603)"}
        resumen["tesauro"] = {"nota": "usa listar_tesauro (arboles con counts)"}
        resumen["sistematizaciones"] = {"nota": "usa listar_sistematizaciones (arbol)"}
        out["catalogos"] = resumen
        out["nota"] = "'todos' trae solo counts; pide la categoria EXACTA para el detalle (ej 'sentidos')"
        out["ms"] = _ms_desde(t0)
        return out
    try:
        data = await con_watchdog(_catalogo(cat), "listar_filtros")
        vals: list[dict[str, Any]] = []
        for it in data:
            v: dict[str, Any] = {"id": it.get("id"),
                                 "nombre": it.get("nombre") or it.get("descripcion"),
                                 "slug": it.get("slug")}
            if cat == "sentidos" and it.get("search_elastic"):
                v["search_elastic"] = it.get("search_elastic")
            vals.append(v)
        total = len(vals)
        page = int(page)
        paginas_real = max(1, min(int(paginas), 3))
        TAM = 100
        if page < 1:
            return {"ok": False, "error": "page debe ser >= 1",
                    "pista": "1..paginas_filtradas de 100 valores por pagina", "ms": _ms_desde(t0)}
        paginas_f = max(1, -(-total // TAM))
        if (page - 1) * TAM >= total:
            return {"ok": False, "error": f"page {page} fuera de rango: hay {paginas_f} paginas de {total} valores",
                    "pista": "rebaja page o usa pagina 1", "ms": _ms_desde(t0)}
        ini = (page - 1) * TAM
        troza = vals[ini: ini + TAM * paginas_real]
        out["total"] = total
        out["pagina"] = page
        out["paginas_filtradas"] = paginas_f
        out["tamano_pagina"] = TAM
        out["valores"] = troza
        if ini + len(troza) < total:
            out["siguiente_pagina"] = page + paginas_real
        recort = int(paginas) - paginas_real
        if recort > 0:
            out["nota"] = _nota(3, recort)
        if cat == "distritos":
            out.setdefault("nota", ("el catalogo de distritos del TC incluye filas ESPURIAS upstream "
                                    "(nombres tipo '09-06-2003'); usa las de distrito real"))
        out["ms"] = _ms_desde(t0)
        return out
    except (ErrorSite, ErrorValidacion) as e:
        return _envolver(e, t0)


# --------------------------------------------------------- canal de updates

# (la linea UPDATE se inserto en las INSTRUCCIONES al arranque: _linea_update())


# ---------------------------------------------------------- contrato cerrado
# El esquema sin 'additionalProperties' (default true) permite que un cliente
# envie argumentos desconocidos que el SDK descarta en silencio antes de la
# tool. Cerramos el contrato: el esquema publica SOLO los args soportados.
for _t in mcp._tool_manager.list_tools():
    _t.parameters["additionalProperties"] = False


# ---------------------------------------------------------------------- main

def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
