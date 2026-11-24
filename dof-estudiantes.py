import os
import sys
import json
from html import escape as html_escape
import logging
import argparse
import requests
import urllib3
import locale
import re
import time
import glob
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    locale.setlocale(locale.LC_TIME, 'es_ES.UTF-8')
except:
    try:
        locale.setlocale(locale.LC_TIME, 'es_MX.utf8')
    except:
        pass

# ==========================================
# 1. CONFIGURACIÓN Y LOGGING
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def ahora_mexico():
    tz_name = os.environ.get("TZ", "America/Cancun")
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now(timezone(timedelta(hours=-5)))

def cargar_env():
    env_path = os.path.join(BASE_DIR, '.env')
    if os.path.exists(env_path):
        try:
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, v = line.split('=', 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except Exception:
            pass

cargar_env()

logger = logging.getLogger("DOF_Estudiantes")
logger.setLevel(logging.INFO)
logger.handlers = []
fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt="%Y-%m-%d %H:%M:%S")
sh  = logging.StreamHandler()
sh.setFormatter(fmt)
logger.addHandler(sh)
fh = logging.FileHandler(os.path.join(BASE_DIR, "dof-estudiantes.log"), encoding="utf-8")
fh.setFormatter(fmt)
logger.addHandler(fh)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'es-MX,es;q=0.9',
}

DOF_PRIMARY_BASE = "https://dof.gob.mx"
DOF_FALLBACK_BASE = "https://www.dof.gob.mx"
SECCIONES_GENERICAS = {
    "UNICA SECCION", "PRIMERA SECCION", "SEGUNDA SECCION",
    "TERCERA SECCION", "CUARTA SECCION", "QUINTA SECCION",
}

DEFAULT_FALLBACK_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
]
MODELOS_NO_DISPONIBLES = set()

# ==========================================
# 2. SCRAPING — DOF FEDERAL
# ==========================================
def _get_dof_html(path_query):
    """Descarga una ruta del DOF priorizando https://dof.gob.mx (SSL válido) con fallback a www.dof.gob.mx."""
    ruta = path_query.lstrip("/")
    intentos = [
        (f"{DOF_PRIMARY_BASE}/{ruta}", True, DOF_PRIMARY_BASE),
        (f"{DOF_PRIMARY_BASE}/{ruta}", True, DOF_PRIMARY_BASE),
        (f"{DOF_FALLBACK_BASE}/{ruta}", False, DOF_FALLBACK_BASE),
    ]
    ultimo_error = None
    for idx, (url, verify_ssl, base_used) in enumerate(intentos):
        try:
            r = requests.get(url, timeout=25, headers=HEADERS, verify=verify_ssl)
            r.raise_for_status()
            return r.text, base_used
        except Exception as e:
            ultimo_error = e
            if idx < len(intentos) - 1:
                time.sleep(1.5)
    raise ultimo_error


def fetch_dof_portada(fecha_objetivo=None):
    hoy = fecha_objetivo or ahora_mexico()
    url_info = []  # (path_query, edicion_label, fecha_label)

    # Hoy (si es hábil)
    hoy_str = f"{hoy.day:02d}/{hoy.month:02d}/{hoy.year}"
    if hoy.weekday() < 5:
        url_info.extend([
            (f"index_111.php?year={hoy.year}&month={hoy.month:02d}&day={hoy.day:02d}&edicion=MAT", "Matutina", hoy_str),
            (f"index_111.php?year={hoy.year}&month={hoy.month:02d}&day={hoy.day:02d}&edicion=VES", "Vespertina", hoy_str),
        ])

    # Ayer hábil (Vespertina)
    dias_restar = 1 if hoy.weekday() not in (0, 6) else (3 if hoy.weekday() == 0 else 2)
    ayer = hoy - timedelta(days=dias_restar)
    ayer_str = f"{ayer.day:02d}/{ayer.month:02d}/{ayer.year}"
    url_info.append((f"index_111.php?year={ayer.year}&month={ayer.month:02d}&day={ayer.day:02d}&edicion=VES", "Vespertina", ayer_str))

    todas_las_publicaciones = []
    consultas_exitosas = 0
    errores_red = 0

    for path_query, edicion, fecha_pub in url_info:
        logger.info(f"[DOF FEDERAL] Consultando: {DOF_PRIMARY_BASE}/{path_query}")
        try:
            html_text, base_used = _get_dof_html(path_query)
            consultas_exitosas += 1

            if "No se encontraron publicaciones" in html_text or "no se publicó" in html_text:
                logger.info(f"[DOF FEDERAL] Sin publicaciones en {edicion} ({fecha_pub})")
                continue

            items = _parsear_dof(BeautifulSoup(html_text, 'html.parser'), base_url=base_used)
            logger.info(f"[DOF FEDERAL] {len(items)} publicaciones encontradas en {edicion} ({fecha_pub})")
            if items:
                for it in items:
                    it['fuente'] = 'DOF Federal'
                    it['edicion'] = edicion
                    it['fecha_edicion'] = fecha_pub
                todas_las_publicaciones.extend(items)
        except Exception as e:
            errores_red += 1
            logger.warning(f"[DOF FEDERAL] Error consultando {path_query}: {e}")

    if consultas_exitosas == 0 and errores_red > 0:
        raise RuntimeError(f"No se pudo conectar con el DOF en ninguna de las {errores_red} ediciones consultadas.")

    unicas = list({it['url']: it for it in todas_las_publicaciones}.values())
    return unicas


def _parsear_dof(soup, base_url=DOF_PRIMARY_BASE):
    items = []
    dep_actual = "General"
    base_clean = base_url.rstrip("/")

    for el in soup.find_all(["td", "a"]):
        if el.name == "td":
            classes = el.get("class") or []
            txt = " ".join(el.get_text(" ", strip=True).split())
            if not txt:
                continue
            if "subtitle_azul" in classes and txt.upper() not in SECCIONES_GENERICAS:
                dep_actual = txt
            elif "txt_blanco2" in classes and txt.upper() not in SECCIONES_GENERICAS:
                dep_actual = txt
        elif el.name == "a":
            href = el.get('href') or ''
            if "nota_detalle" not in href:
                continue
            titulo = " ".join(el.get_text(" ", strip=True).split())
            if not titulo or len(titulo) < 15:
                continue
            url = f"{base_clean}/{href.lstrip('/')}" if not href.startswith('http') else href
            dep_item = dep_actual
            if dep_item == "General":
                dep_tag = el.find_previous('td', class_='subtitle_azul') or el.find_previous('td', class_='txt_blanco2')
                if dep_tag:
                    cand = " ".join(dep_tag.get_text(" ", strip=True).split())
                    if cand and cand.upper() not in SECCIONES_GENERICAS:
                        dep_item = cand
            items.append({
                'titulo':      titulo,
                'url':         url,
                'dependencia': dep_item,
            })

    if not items:
        for a in soup.find_all("a", href=True):
            href = a.get('href', '')
            titulo = " ".join(a.get_text(" ", strip=True).split())
            if '/DOF/' in href and len(titulo) > 20:
                url = f"{base_clean}/{href.lstrip('/')}" if not href.startswith('http') else href
                items.append({'titulo': titulo, 'url': url, 'dependencia': 'General'})
    return list({it['url']: it for it in items}.values())

# ==========================================
# 3. PRE-FILTRO REGEX
# ==========================================
def pre_filtro_regex(publicaciones, config):
    patterns = [
        re.compile(p, re.IGNORECASE)
        for p in config.get('pre_filter', {}).get('exclude_keywords_regex', [])
    ]
    if not patterns:
        return publicaciones
    filtradas = []
    for p in publicaciones:
        texto = f"{p.get('titulo', '')} {p.get('dependencia', '')}"
        if any(pat.search(texto) for pat in patterns):
            continue
        filtradas.append(p)
    return filtradas


def obtener_gemini_client(config):
    api_key = (os.environ.get("GEMINI_API_KEY") or config.get('ai_classifier', {}).get('gemini_api_key') or "").strip()
    if not api_key:
        raise RuntimeError("No se encontró GEMINI_API_KEY en variables de entorno ni en la configuración.")
    timeout_sec = int(config.get('ai_classifier', {}).get('timeout', 90))
    timeout_ms = max(20000, min(timeout_sec * 1000, 90000))
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))


def _extraer_json_limpio(resp_text):
    txt = (resp_text or "").strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?\s*", "", txt, flags=re.IGNORECASE)
        txt = re.sub(r"\s*```$", "", txt)
    return json.loads(txt.strip())


def llamar_gemini_con_reintentos(client, model_name, prompt, config=None, response_schema=None, reintentos=10):
    cfg_fallbacks = []
    if isinstance(config, dict):
        cfg_fallbacks = config.get('ai_classifier', {}).get('fallback_models', []) or []

    candidatos = [model_name] + list(cfg_fallbacks) + list(DEFAULT_FALLBACK_MODELS)
    modelos = []
    for m in candidatos:
        if m and m not in modelos and m not in MODELOS_NO_DISPONIBLES:
            modelos.append(m)

    if not modelos:
        raise RuntimeError("No quedan modelos de Gemini disponibles (todos devolvieron 404 NOT_FOUND).")

    gen_cfg_kwargs = {
        "temperature": 0.0,
        "response_mime_type": "application/json",
    }
    if response_schema is not None:
        gen_cfg_kwargs["response_schema"] = response_schema
    gen_cfg = types.GenerateContentConfig(**gen_cfg_kwargs)

    ultimo_error = None
    idx = 0
    intentos_transitorios = 0

    while modelos and intentos_transitorios < reintentos:
        modelo_actual = modelos[idx % len(modelos)]
        try:
            resp = client.models.generate_content(
                model=modelo_actual,
                contents=prompt,
                config=gen_cfg,
            )
            if not resp.text:
                raise RuntimeError(f"Respuesta vacía del modelo {modelo_actual}")
            return resp.text
        except Exception as e:
            ultimo_error = e
            err_str = str(e)
            err_upper = err_str.upper()

            if "400" in err_str and ("API_KEY_INVALID" in err_upper or "API KEY NOT VALID" in err_upper):
                logger.error(f"[IA] Clave de API inválida: {err_str[:200]}")
                raise
            if "403" in err_str and ("PERMISSION_DENIED" in err_upper or "LEAKED" in err_upper):
                logger.error(f"[IA] Permiso denegado / clave bloqueada: {err_str[:200]}")
                raise

            if "404" in err_str or "NOT_FOUND" in err_upper:
                logger.warning(f"[IA] Modelo {modelo_actual} no disponible (404), descartándolo: {err_str[:120]}")
                MODELOS_NO_DISPONIBLES.add(modelo_actual)
                modelos = [m for m in modelos if m not in MODELOS_NO_DISPONIBLES]
                if not modelos:
                    break
                idx = idx % len(modelos)
                continue

            intentos_transitorios += 1
            espera = 3 * ((intentos_transitorios % 3) + 1)
            logger.warning(
                f"[IA] Intento transitorio {intentos_transitorios}/{reintentos} falló con {modelo_actual} "
                f"({err_str[:90]}...). Rotando modelo y reintentando en {espera}s..."
            )
            idx = (idx + 1) % len(modelos)
            time.sleep(espera)

    raise ultimo_error if ultimo_error is not None else RuntimeError("No se pudo obtener respuesta de Gemini.")

# ==========================================
# 4. FILTRO FASE 1 — IA SOBRE TITULOS
# ==========================================
def primer_filtro_ia(publicaciones, config, fuente_label=""):
    if not publicaciones:
        return []
    client = obtener_gemini_client(config)
    titulos = [
        {"id": i, "titulo": p['titulo'], "dependencia": p.get('dependencia', '')}
        for i, p in enumerate(publicaciones)
    ]
    logger.info(f"[Fase 1] Clasificando {len(titulos)} documentos...")

    prompt = f"""
Eres un jurista y profesor universitario filtrando el Diario Oficial de la Federación (DOF) EXCLUSIVAMENTE para ESTUDIANTES DE DERECHO.
Tu objetivo es seleccionar en este primer filtro los documentos con VERDADERO VALOR O TRASCENDENCIA JURÍDICA (Constitucional, Amparo, Civil, Penal, Administrativo, Laboral, Fiscal, Mercantil, Electoral, Procesal o Judicial) para analizarlos a profundidad en la Fase 2.

PASA A FASE 2 SI CUMPLE AL MENOS UNO:
1. REFORMAS FEDERALES que modifiquen Leyes, Códigos, Reglamentos o la Constitución.
2. Sentencias de la SCJN (Acciones de Inconstitucionalidad, Controversias Constitucionales o declaratorias generales) sobre LEYES FEDERALES o de Quintana Roo.
3. Acuerdos Generales, Lineamientos, Reglas o Declaratorias del PODER JUDICIAL FEDERAL (SCJN, CJF, TEPJF, Órgano de Administración Judicial, IFECOM), del TFJA o de la Fiscalía General de la República (FGR, ej. plataformas de denuncia digital o litigio como FISC@NET), así como acuerdos de días inhábiles en actos y procedimientos administrativos federales (CNBV, SABG, SAT, etc.) que afecten plazos o trámites procesales.
4. TODA publicación con impacto normativo, jurídico o de coordinación sobre QUINTANA ROO (incluyendo Convenios Marco o específicos celebrados con el Estado de Quintana Roo o sus municipios).
5. Publicaciones sobre PERITOS TRADUCTORES y auxiliares de administración de justicia.
6. MODIFICACIONES a Reglas o Disposiciones de Carácter General de organismos reguladores, fiscales, financieros, de comercio exterior o electorales (ej. Miscelánea Fiscal, Reglas Generales de Comercio Exterior / RGCE, disposiciones de la CNBV/SHCP sobre mercados o proveedores de precios, IMPI, o lineamientos electorales generales del INE sobre cargos de elección popular).
7. NORMAS OFICIALES MEXICANAS (NOM) definitivas en materia LABORAL o de seguridad y salud en los centros de trabajo (STPS), o que regulen relaciones jurídicas sustantivas.
8. Decretos presidenciales de trascendencia jurídica general.

DESCARTA ESTRICTAMENTE EN FASE 1:
- Avisos de solicitud de registro constitutivo de asociaciones religiosas.
- Tipo de cambio, TIIE, tasas de interés interbancarias, UDI, INPC.
- NORMAS OFICIALES MEXICANAS (NOM) médicas, clínicas, sanitarias, de salud, farmacéuticas, alimentos, pesca o metrología (NO laborales), actualizaciones de compendios de medicamentos/insumos para la salud, proyectos PROY-NOM y avisos de consulta pública.
- Manuales de organización, procedimientos, operación o códigos de conducta internos de dependencias; cambios de domicilio oficial o delegación interna de facultades administrativas.
- Licitaciones públicas, convocatorias de empleo, avisos de deslinde, nombramientos o expropiaciones aisladas.
- Fijación de cuotas, volúmenes de captura pesquera, vedas, precios de combustibles o estímulos fiscales particulares.
- Programas institucionales o reglas de operación de subsidios sociales/infraestructura en estados distintos a Quintana Roo.
- Circulares de inhabilitación/sanción, multas, autorizaciones o revocaciones a empresas o proveedores particulares.
- Convenios de coordinación o sentencias de un ESTADO que NO sea Quintana Roo.
- Resoluciones del INE sobre un partido político específico o fiscalización individual (pero SÍ pasan lineamientos electorales de carácter general).

Devuelve SOLO una lista JSON de números enteros con los 'id' que PASAN (ej. [0, 5, 17]). Si ninguno pasa devuelve [].

TITULOS:
{json.dumps(titulos, ensure_ascii=False)}
"""
    model_name = config.get('ai_classifier', {}).get('gemini_model', 'gemini-3.5-flash-lite')
    schema_ids = types.Schema(
        type=types.Type.ARRAY,
        items=types.Schema(type=types.Type.INTEGER),
    )
    resp_text = llamar_gemini_con_reintentos(
        client, model_name, prompt, config=config, response_schema=schema_ids
    )
    raw_ids = _extraer_json_limpio(resp_text)

    if isinstance(raw_ids, dict):
        lista_cruda = []
        for v in raw_ids.values():
            if isinstance(v, list):
                lista_cruda = v
                break
        raw_ids = lista_cruda

    if not isinstance(raw_ids, list):
        raise ValueError(f"[Fase 1] Respuesta JSON inesperada de Gemini: {resp_text[:200]}")

    ids_aprobados = set()
    for item in raw_ids:
        if isinstance(item, dict) and 'id' in item:
            try:
                ids_aprobados.add(int(item['id']))
            except Exception:
                pass
        elif isinstance(item, (int, str)):
            try:
                ids_aprobados.add(int(item))
            except Exception:
                pass

    sobrevivientes = [p for i, p in enumerate(publicaciones) if i in ids_aprobados]
    logger.info(f"[Fase 1] Pasan a Fase 2: {len(sobrevivientes)}")
    return sobrevivientes

# ==========================================
# 5. EXTRACCION
# ==========================================
def extraer_contenido_nota(url, reintentos=2, max_chars=12000):
    url_primaria = url.replace("https://www.dof.gob.mx", DOF_PRIMARY_BASE)
    url_alterna = url_primaria.replace(DOF_PRIMARY_BASE, DOF_FALLBACK_BASE)
    candidatas = [(url_primaria, True), (url_primaria, True), (url_alterna, False)]

    for intento in range(min(reintentos + 1, len(candidatas))):
        target_url, verify_ssl = candidatas[intento]
        try:
            r = requests.get(target_url, timeout=20, headers=HEADERS, verify=verify_ssl)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, 'html.parser')
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            cuerpo = (
                soup.find('div', id='DivDetalleNota')
                or soup.find('div', id='nota_detalle')
                or soup.find('div', class_='nota_detalle')
                or soup.find('article')
            )
            if cuerpo:
                return " ".join(cuerpo.get_text(separator=' ', strip=True).split())[:max_chars]
            body = soup.find('body') or soup
            return " ".join(body.get_text(separator=' ', strip=True).split())[:max_chars]
        except Exception as e:
            logger.warning(f"[Extracción] Intento {intento+1} falló para {target_url}: {e}")
            time.sleep(1)
    return ""

# ==========================================
# 6. ANALISIS PROFUNDO — FASE 2
# ==========================================
def analizar_profundo_uno_por_uno(sobrevivientes, config):
    if not sobrevivientes:
        return []
    logger.info(f"[Fase 2] Analizando {len(sobrevivientes)} textos en profundidad...")
    client = obtener_gemini_client(config)
    alertas = []
    errores_ia = 0

    schema_fase2 = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "titulo_corto": types.Schema(type=types.Type.STRING),
            "relevante": types.Schema(type=types.Type.BOOLEAN),
            "nivel_alerta": types.Schema(type=types.Type.STRING, enum=["Alta", "Media"]),
            "etiqueta_especial": types.Schema(
                type=types.Type.STRING,
                enum=[
                    "Reforma Federal", "SCJN", "Poder Judicial", "Quintana Roo",
                    "Peritos", "Regulatorio", "NOM", "Decreto", "General"
                ],
            ),
            "resumen_estudiantil": types.Schema(type=types.Type.STRING),
            "materias_relacionadas": types.Schema(
                type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING)
            ),
            "puntos_clave": types.Schema(
                type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING)
            ),
        },
        required=[
            "titulo_corto", "relevante", "nivel_alerta", "etiqueta_especial",
            "resumen_estudiantil", "materias_relacionadas", "puntos_clave"
        ],
    )

    for idx, p in enumerate(sobrevivientes, 1):
        texto = extraer_contenido_nota(p['url'])
        texto_prompt = texto if texto else "(Contenido completo no disponible; evalúa con base en el título oficial y la dependencia.)"

        prompt = f"""
Evalúa esta publicación del Diario Oficial (DOF) EXCLUSIVAMENTE para ESTUDIANTES DE DERECHO.
El objetivo es seleccionar únicamente publicaciones con VERDADERO VALOR FORMATIVO O PRÁCTICO EN DERECHO.

REGLAS — Marca "relevante": true si cumple AL MENOS UNO:
1. Es una REFORMA FEDERAL que modifica una Ley, Código, Reglamento o la Constitución.
2. Es una SENTENCIA de la SCJN SOLO SI es sobre una LEY FEDERAL o de Quintana Roo. Si es sobre una ley de otro estado, marca "relevante": false.
3. Es un Acuerdo, Lineamiento o Declaratoria del PODER JUDICIAL FEDERAL (SCJN, CJF, TEPJF, Órgano de Administración Judicial, IFECOM), TFJA o FGR (ej. plataformas digitales de denuncia/litigio como FISC@NET, interconexión tecnológica, plazos, competencias o reglas procesales), o un acuerdo de días inhábiles en procedimientos administrativos ante reguladores federales (CNBV, SABG, SAT).
4. Es un tema o convenio específico de QUINTANA ROO (ej. Convenio Marco de Coordinación SEGOB–Quintana Roo o SECTUR–Quintana Roo). Los convenios con otros estados que NO son Quintana Roo llevan "relevante": false.
5. Trata sobre PERITOS TRADUCTORES y auxiliares de la justicia.
6. Es una MODIFICACIÓN A REGLAS O DISPOSICIONES DE CARÁCTER GENERAL (ej. Resolución Miscelánea Fiscal, Reglas Generales de Comercio Exterior / RGCE, disposiciones CNBV/SHCP, reglas de comercio exterior, o lineamientos electorales generales del INE sobre cargos de elección) que creen o modifiquen obligaciones o trámites jurídicos relevantes.
7. Es una NORMA OFICIAL MEXICANA (NOM) definitiva ÚNICAMENTE SI regula materias de Derecho laboral / seguridad y salud en los centros de trabajo (ej. NOM de la STPS) o Derecho sustantivo general. Si es técnica médica/sanitaria/industrial pura, marca "relevante": false.
8. Es un DECRETO presidencial relevante jurídicamente (no administrativos menores).

MARCA "relevante": false ESTRICTAMENTE SI (NO ES MATERIA DE DERECHO):
- Son Normas Oficiales Mexicanas (NOM) médicas, clínicas, hospitalarias, sanitarias, farmacéuticas, alimentos, pesca o metrología (NO laborales).
- Son Actualizaciones de compendios médicos (Insumos para la salud), cuadros básicos o catálogos administrativos.
- Son Manuales de organización, operación, servicios, códigos de conducta internos o acuerdos de delegación de facultades internas.
- Son Avisos de asociaciones religiosas, licitaciones públicas, convocatorias de empleo o nombramientos.
- Fijan estímulos fiscales particulares, tarifas, precios de combustibles, cuotas de captura pesquera o vedas.
- Son Programas maestros, sectoriales, institucionales o reglas de operación de programas sociales/subsidios en estados distintos a Quintana Roo.
- Son Proyectos de normas, avisos de consulta pública (PROY-NOM) o respuestas a comentarios.
- Son resoluciones particulares o antidumping contra empresas o países, circulares de inhabilitación de proveedores, autorizaciones/revocaciones particulares.
- Son resoluciones del INE sobre un partido político específico o fiscalización individual.
- La publicación es sobre leyes o convenios de un ESTADO que NO sea Quintana Roo.
- Son tasas de interés, tipo de cambio, UDI, INPC.

NIVEL DE ALERTA:
- "Alta" SOLO para cambios jurídicos de impacto amplio y directo: reformas a la Constitución o a leyes/códigos federales, sentencias de la SCJN sobre leyes federales, o reglas generales que creen o cambien obligaciones sustantivas para particulares. Un tema relacionado con Quintana Roo o una regla fiscal/administrativa no es automáticamente de alta prioridad.
- "Media" para medidas administrativas, operativas o procedimentales que no cambien derechos u obligaciones sustantivas: convenios de coordinación (como SEGOB/SECTUR–Quintana Roo), días inhábiles ante autoridades, interconexión tecnológica del Poder Judicial, plataformas digitales de denuncia/trámites, NOM laborales definitivas y acuerdos regulatorios de menor impacto.
- Si la publicación es relevante pero no satisface claramente los criterios estrictos de "Alta", asigna "Media".

ETIQUETA:
- "Reforma Federal" si modifica una ley/código federal
- "SCJN" si es sentencia de Acción de Inconstitucionalidad o Controversia Constitucional
- "Poder Judicial" si es reforma, acuerdo o declaratoria del Poder Judicial Federal (SCJN, CJF, TEPJF, OAJ, IFECOM)
- "Quintana Roo" si es específico de ese estado
- "Peritos" si trata de peritos/traductores
- "Regulatorio" si es miscelánea fiscal, RGCE, CNBV/SHCP, días inhábiles o regla general
- "NOM" si es una Norma Oficial Mexicana laboral definitiva
- "Decreto" si es un decreto presidencial
- "General" para otros temas jurídicos relevantes (FGR, INE general, TFJA, etc.)

Dependencia: {p.get('dependencia', 'General')}
Publicacion: {p['titulo']}
Texto: {texto_prompt}

Devuelve JSON exacto:
{{
  "titulo_corto": "titulo conciso",
  "relevante": true/false,
  "nivel_alerta": "Alta" o "Media",
  "etiqueta_especial": "Reforma Federal", "SCJN", "Poder Judicial", "Quintana Roo", "Peritos", "Regulatorio", "NOM", "Decreto", o "General",
  "resumen_estudiantil": "Max 3 lineas: de qué trata.",
  "materias_relacionadas": ["Materia 1"],
  "puntos_clave": ["punto 1"]
}}
"""
        try:
            model_name = config.get('ai_classifier', {}).get('gemini_model', 'gemini-3.5-flash-lite')
            resp_text = llamar_gemini_con_reintentos(
                client, model_name, prompt, config=config, response_schema=schema_fase2
            )
            data = _extraer_json_limpio(resp_text)
            if isinstance(data, list):
                data = data[0] if data else {}
            if data.get('relevante') is True:
                data['dependencia'] = p.get('dependencia', 'General')
                data['url']         = p['url']
                data['edicion']     = p.get('edicion', '')
                data['fecha_edicion'] = p.get('fecha_edicion', '')
                alertas.append(data)
            time.sleep(1.5)
        except Exception as e:
            errores_ia += 1
            logger.error(f"[Fase 2] Error al procesar publicación {idx}: {e}")

    if len(alertas) == 0 and errores_ia > 0:
        raise RuntimeError(
            f"[Fase 2] Fallaron {errores_ia} de {len(sobrevivientes)} análisis en Gemini; se aborta para no guardar un reporte vacío falso."
        )

    return alertas

# ==========================================
# 7. GENERACIÓN DEL DASHBOARD Y REPORTES
# ==========================================
MESES_NOMBRES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]

def obtener_etiqueta_mes(mes_key):
    try:
        partes = mes_key.split('-')
        y, m = int(partes[0]), int(partes[1])
        return f"{MESES_NOMBRES[m]} {y}"
    except Exception:
        return mes_key

def migrar_reportes_a_carpetas_mensuales(resumenes_dir):
    """Mueve los reportes antiguos sueltos en Resumenes_Semanales/ a su carpeta mensual YYYY-MM/ y ajusta sus rutas relativas."""
    archivos_sueltos = glob.glob(os.path.join(resumenes_dir, "Resumen_DOF_*.html"))
    for arch in archivos_sueltos:
        nombre_base = os.path.basename(arch)
        fecha_str = nombre_base.replace("Resumen_DOF_", "").replace(".html", "")
        mes_key = fecha_str[:7] if len(fecha_str) >= 7 else "Otros"
        mes_dir = os.path.join(resumenes_dir, mes_key)
        os.makedirs(mes_dir, exist_ok=True)
        destino = os.path.join(mes_dir, nombre_base)

        try:
            with open(arch, 'r', encoding='utf-8') as f:
                html_content = f.read()

            # Ajustar rutas relativas para que funcionen desde la subcarpeta YYYY-MM/
            reemplazos = [
                ("url('cursor/", "url('../cursor/"),
                ('href="index.html"', 'href="../index.html"'),
                ("href='index.html'", "href='../index.html'"),
                ('src="corner_gif.gif"', 'src="../corner_gif.gif"'),
                ('src="rofa_cat.gif"', 'src="../rofa_cat.gif"'),
                ('src="jellyfish.png"', 'src="../jellyfish.png"'),
                ('src="bg_music.mp3"', 'src="../bg_music.mp3"'),
                ("src = '../blue-fish.webp'", "src = '../../blue-fish.webp'"),
            ]
            for viejo, nuevo in reemplazos:
                html_content = html_content.replace(viejo, nuevo)

            with open(destino, 'w', encoding='utf-8') as f:
                f.write(html_content)
            os.remove(arch)
            logger.info(f"Reporte migrado a carpeta mensual: {mes_key}/{nombre_base}")
        except Exception as e:
            logger.error(f"Error al migrar reporte {arch}: {e}")

def actualizar_dashboard(resumenes_dir):
    migrar_reportes_a_carpetas_mensuales(resumenes_dir)

    archivos_sub = glob.glob(os.path.join(resumenes_dir, "*", "Resumen_DOF_*.html"))
    archivos_raiz = glob.glob(os.path.join(resumenes_dir, "Resumen_DOF_*.html"))
    todos_archivos = archivos_sub + archivos_raiz

    # Ordenar por fecha (extraída del nombre de archivo) de más reciente a más antiguo
    def extraer_fecha(ruta):
        nb = os.path.basename(ruta)
        return nb.replace("Resumen_DOF_", "").replace(".html", "")

    archivos_por_fecha = {}
    for ruta in todos_archivos:
        f_str = extraer_fecha(ruta)
        archivos_por_fecha[f_str] = ruta

    fechas_ordenadas = sorted(archivos_por_fecha.keys(), reverse=True)
    archivos = [archivos_por_fecha[f] for f in fechas_ordenadas]
    
    fechas_con_reporte = []
    alertas_por_fecha = {}
    reportes_dict = {}
    reportes_urls = {}
    grupos_por_mes = {}

    for idx, arch in enumerate(archivos):
        nombre_base = os.path.basename(arch)
        fecha_str = nombre_base.replace("Resumen_DOF_", "").replace(".html", "")
        rel_url = os.path.relpath(arch, resumenes_dir).replace("\\", "/")
        mes_key = fecha_str[:7] if len(fecha_str) >= 7 else "Otros"

        fechas_con_reporte.append({"date": fecha_str, "url": rel_url})
        reportes_urls[fecha_str] = rel_url
        
        # Leer el reporte para contar alertas y extraer el contenido para navegación instantánea
        try:
            with open(arch, 'r', encoding='utf-8') as f:
                contenido = f.read()
            soup_rep = BeautifulSoup(contenido, 'html.parser')
            c_area = soup_rep.find('div', class_='content-area')
            if c_area:
                for back in c_area.find_all('a', class_='back-link'):
                    back['onclick'] = "showDashboard(); return false;"
                    back['href'] = "#inicio"
                reportes_dict[fecha_str] = c_area.decode_contents()

            n_alertas = contenido.count('class="accordion"')
            if n_alertas == 0:
                n_alertas = contenido.count('<details')
                
            alertas_por_fecha[fecha_str] = {
                'url': rel_url,
                'count': n_alertas,
                '🎀': (n_alertas > 0) and ("Reforma Federal" in contenido),
                '🔋': (n_alertas > 0) and ("Quintana Roo" in contenido),
                '⏰': (n_alertas > 0) and ("SCJN" in contenido or "Poder Judicial" in contenido)
            }
        except:
            n_alertas = 0
        
        es_nuevo = idx == 0
        badge_nuevo = '<span class="badge-new">NUEVO</span>' if es_nuevo else ''
        
        card_html = f"""
        <a href="{rel_url}" class="report-card" data-date="{fecha_str}" data-month="{obtener_etiqueta_mes(mes_key).lower()} {mes_key}" onclick="showReport('{fecha_str}'); return false;">
            <div class="report-icon"><div style="background-image: url('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAc0lEQVR4nGNkwA7+4xBnRBdgwaGQITMzE4U/ffp0rOpYiLQZXZ4Rm5P+n221BzOMqw9i1Y0mD9bLhNWa//+xYmK8AAerVq1CcBgYGMLCwhhIMiAMhwaiDVg1clzAxEAhYMEmeK7NgWgDUFIiiZZjZKyBAQBm6i2zNPw6pQAAAABJRU5ErkJggg=='); background-size:contain; width:24px; height:24px; image-rendering:pixelated; display:inline-block;"></div></div>
            <div class="report-info">
                <h3>Reporte Diario {badge_nuevo}</h3>
                <p class="report-date">{fecha_str}</p>
                <p class="report-count">{n_alertas} publicaciones relevantes</p>
            </div>
        </a>
        """
        if mes_key not in grupos_por_mes:
            grupos_por_mes[mes_key] = {
                'cards': [],
                'total_reportes': 0,
                'total_alertas': 0
            }
        grupos_por_mes[mes_key]['cards'].append(card_html)
        grupos_por_mes[mes_key]['total_reportes'] += 1
        grupos_por_mes[mes_key]['total_alertas'] += n_alertas

    lista_html = ""
    for m_idx, mes_key in enumerate(sorted(grupos_por_mes.keys(), reverse=True)):
        info_mes = grupos_por_mes[mes_key]
        etiqueta_mes = obtener_etiqueta_mes(mes_key)
        open_attr = "open" if m_idx == 0 else ""
        n_rep = info_mes['total_reportes']
        n_pub = info_mes['total_alertas']
        rep_label = "reporte" if n_rep == 1 else "reportes"
        pub_label = "publicación" if n_pub == 1 else "publicaciones"
        cards_joined = "\n".join(info_mes['cards'])

        lista_html += f"""
        <details class="month-group" data-month-key="{mes_key}" {open_attr}>
            <summary class="month-header">
                <div class="month-header-left">
                    <span class="month-folder-icon">📁</span>
                    <span class="month-title">{etiqueta_mes}</span>
                    <span class="month-code">({mes_key})</span>
                </div>
                <div class="month-stats">
                    <span class="month-badge">{n_rep} {rep_label}</span>
                    <span class="month-badge month-badge-pubs">{n_pub} {pub_label}</span>
                </div>
            </summary>
            <div class="month-cards">
                {cards_joined}
            </div>
        </details>
        """

    import calendar
    calendar.setfirstweekday(6)  # Sunday first
    now = ahora_mexico()
    year, month = now.year, now.month
    
    # Previous month
    if month == 1:
        prev_month, prev_year = 12, year - 1
    else:
        prev_month, prev_year = month - 1, year
    
    meses = MESES_NOMBRES
    
    def build_cal_table(cal_year, cal_month, alertas):
        month_label = f"{meses[cal_month]} {cal_year}"
        html = f'<table class="cute-calendar" style="width:100%; border-collapse:collapse; font-size:12px; font-weight:bold; color:#0055ea; font-family: Tahoma, sans-serif;">\n'
        html += f'<tr><th colspan="7" style="background:#ffb2e2; color:#fff; border-radius:5px 5px 0 0; padding:2px;">{month_label}</th></tr>\n'
        html += '<tr style="background:#f0f8ff;"><th>Do</th><th>Lu</th><th>Ma</th><th>Mi</th><th>Ju</th><th>Vi</th><th>Sa</th></tr>\n'
        cal_matrix = calendar.monthcalendar(cal_year, cal_month)
        for week in cal_matrix:
            html += '<tr>'
            for day in week:
                if day == 0:
                    html += '<td style="border:1px solid transparent;"></td>'
                else:
                    f_str = f"{cal_year}-{cal_month:02d}-{day:02d}"
                    if f_str in alertas:
                        cnt = alertas[f_str].get('count', 0)
                        target_url = alertas[f_str].get('url', f"{cal_year}-{cal_month:02d}/Resumen_DOF_{f_str}.html")
                        
                        if cnt > 0:
                            emojis = ""
                            if alertas[f_str].get('🎀'): emojis += "<div class='pixel-art icon-bow' title='Reforma Federal'></div>"
                            if alertas[f_str].get('🔋'): emojis += "<div class='pixel-art icon-battery' title='Quintana Roo'></div>"
                            if alertas[f_str].get('⏰'): emojis += "<div class='pixel-art icon-clock' title='SCJN / Poder Judicial'></div>"
                            if not emojis:
                                emojis = "<div class='pixel-art icon-star' title='Avisos relevantes'></div>"
                            
                            title_info = f"Reporte del {day} de {meses[cal_month]}: {cnt} publicaciones"
                            html += f'<td class="cal-day has-report" title="{title_info}">'
                            html += f'<a href="{target_url}" class="cal-day-link" onclick="showReport(\'{f_str}\'); return false;">'
                            html += f'<span>{day}</span>'
                            html += f'<div class="cal-emojis">{emojis}</div>'
                            html += '</a></td>'
                        else:
                            # Monitoreado pero sin avisos: NO poner figuritas
                            title_info = f"Reporte del {day} de {meses[cal_month]}: sin avisos relevantes"
                            html += f'<td class="cal-day cal-day-no-avisos" title="{title_info}">'
                            html += f'<a href="{target_url}" class="cal-day-link" onclick="showReport(\'{f_str}\'); return false;">'
                            html += f'<span>{day}</span>'
                            html += '</a></td>'
                    else:
                        html += f'<td class="cal-day cal-day-empty"><div>{day}</div></td>'
            html += '</tr>\n'
        html += '</table>'
        return html
    
    cal_html = build_cal_table(year, month, alertas_por_fecha)
    cal_html += '<div style="margin-top:8px;"></div>'
    cal_html += build_cal_table(prev_year, prev_month, alertas_por_fecha)
    
    total_reportes = len(archivos)

    dashboard_html = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DOF Monitor - Monitoreo de DOF y POE</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=VT323&display=swap');
        
        * {{ margin: 0; padding: 0; box-sizing: border-box; cursor: url('cursor/roundynormal.cur'), auto !important; }}
        
        .pixel-art {{
            display: inline-block;
            width: 16px;
            height: 16px;
            background-size: contain;
            background-repeat: no-repeat;
            image-rendering: pixelated;
            vertical-align: middle;
        }}
        .icon-star {{ background-image: url('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAdElEQVR4nNWSSw6AIAxEOw2X8f6H8TglUWuQ/jCunAUE6BuGD1EuKdaJM1j22oQLmCoTpo9quuNC7VwDbUTjvhG2s4Pn7hlegEnQxkEQ1UDZJUp0nOg1EMFjZGf+5pDAlTncfwALH2PnEp8JyD/WrNW6P6kDjVkmlUQItCIAAAAASUVORK5CYII='); }}
        .icon-mail {{ background-image: url('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAYElEQVR4nOWRQQ7AIAgEd4n//zINiTbEIlgvPXQvijATEoGvw37qKU+DVc94khBfvAFHZFxsix2JzfiNxTcryQw/BJkkgkNBJFnBlha+TpLsl9qyU4D3duiz5WTO48+5ACLhJxUzj6CjAAAAAElFTkSuQmCC'); }}
        .icon-music {{ background-image: url('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAW0lEQVR4nGNkwA/+4xBnhDFYCBjA8D9zC6rO6T4ofCZCBuDTTJIBjBDNcKeT5QIoYKTUAIbBZQALifFP0ID/yHGPLdrwGfAfPdGQ4wIMgOQKjDSATfA/AfnhCABLtQ/rxeyyjAAAAABJRU5ErkJggg=='); }}
        .icon-bow {{ background-image: url('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAdklEQVR4nO1SQQ6AMAgD4r+8+2DvvgyzOB2Dippd7WVJ1zI6IBoE11MBd4dOy4XQdWvMMmdFglaC4hDokzlEUHdZO/GFQ6dyZXGG0wRfNTHF8qhIZi6YXohTiO0YtMt+Iv6TBZnBKLt4tgiP7gGDmX/ZRPpBtAN6wDiJ/D2ftgAAAABJRU5ErkJggg=='); }}
        .icon-battery {{ background-image: url('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAWklEQVR4nGNgoBAw4hD/T6xaRmyajc4aYQieMz6HVT0TMZpBACqO4TIWbIqhtmEzgIEoA9A1YDMQlxdIBkwMFAIWXBL4nD24woCFJl4wwhHn2ADWpEyi+gEGAM5xGFO0y+ueAAAAAElFTkSuQmCC'); }}
        .icon-clock {{ background-image: url('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAdElEQVR4nGNgoBAw4hD/T6xaRmwa28+9xlBYaSSKVQ8jsmZsGnEYBNfHhEszSCGSrXAAVfcf3QCsgBgXMRHrdFyuYGKgELDgk0QOA1yuZMGlmVhvMYGiBFtoExuVTCTpxOECBlJcgZ6QqJqUMQxCA7jUUgYAzE4nHD8rGUkAAAAASUVORK5CYII='); }}
        
        a, button, summary, input, .menu-bar span, .footer-box, .report-card, .acc-title, .back-link, .btn-win, .btn-green, .xp-button, .btn-close, .btn-min, .cute-calendar td.has-report, .cute-calendar td.cal-day-no-avisos {{
            cursor: url('cursor/roundyhand.cur'), pointer !important;
        }}

        .cursor-bubble {{
            position: absolute;
            border: 1px solid rgba(255, 255, 255, 0.9);
            background: rgba(173, 216, 230, 0.5);
            border-radius: 50%;
            pointer-events: none;
            z-index: 999999;
            box-shadow: inset 0 0 4px rgba(255,255,255,0.8);
            animation: floatBubble 0.8s ease-out forwards;
        }}
        @keyframes floatBubble {{
            0% {{ transform: translate(-50%, -50%) scale(1); opacity: 0.8; }}
            100% {{ transform: translate(-50%, -60px) scale(1.5); opacity: 0; }}
        }}
        
        body {{
            font-family: 'Tahoma', 'Verdana', sans-serif;
            min-height: 100vh;
            display: flex;
            background: url('https://i.imgur.com/U7RICZA.gif') no-repeat center center fixed;
            background-size: cover;
            color: #000;
            justify-content: center;
            align-items: center;
            padding: 20px;
        }}
        
        .corner-gif {{
            position: fixed;
            bottom: 5px;
            left: 5px;
            z-index: 1000;
            max-width: 150px;
            height: auto;
            transition: transform 0.2s ease-in-out;
        }}
        .corner-gif:hover {{
            transform: translateY(-10px);
        }}
        .corner-gif-right {{
            position: fixed;
            bottom: 5px;
            right: 5px;
            z-index: 1000;
            max-width: 150px;
            height: auto;
            transition: transform 0.2s ease-in-out;
        }}
        .corner-gif-right:hover {{
            transform: translateY(-10px);
        }}
        .floating-jellyfish {{
            position: fixed;
            top: 0;
            left: 0;
            width: 58px;
            height: auto;
            image-rendering: pixelated;
            pointer-events: none;
            z-index: 9998;
            filter: drop-shadow(0 0 8px rgba(80, 210, 255, 0.55));
            will-change: transform;
            transform-origin: 50% 38%;
        }}
        
        /* XP Window Frame */
        .window-frame {{
            /* zoom removed */
            width: 850px;
            max-width: 100%;
            background: #ece9d8;
            border: 2px solid #0055ea;
            border-radius: 8px 8px 0 0;
            box-shadow: 2px 2px 10px rgba(0,0,0,0.5);
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }}
        
        /* Title Bar */
        .title-bar {{
            background: linear-gradient(180deg, #0058ee 0%, #3593ff 8%, #288eff 40%, #127dff 88%, #0366e8 100%);
            padding: 4px 8px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #000;
        }}
        .title-bar-text {{
            color: white;
            font-family: 'VT323', monospace;
            font-size: 24px;
            text-shadow: 1px 1px #000;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .title-bar-controls {{
            display: flex;
            gap: 4px;
        }}
        .btn-win {{
            width: 24px;
            height: 24px;
            border: 1px solid #fff;
            border-radius: 4px;
            color: white;
            font-weight: bold;
            cursor: pointer;
            box-shadow: inset -1px -1px 2px rgba(0,0,0,0.5), inset 1px 1px 2px rgba(255,255,255,0.8);
            display: flex;
            align-items: center;
            justify-content: center;
            font-family: Arial;
        }}
        .btn-close {{ background: linear-gradient(180deg, #e3685e, #c33a30); border-color: #f7a8a1; }}
        .btn-min {{ background: linear-gradient(180deg, #74aef1, #367bd8); border-color: #a4cff7; }}
        
        /* Menu Bar */
        .menu-bar {{
            background: #ece9d8;
            padding: 2px 10px;
            border-bottom: 1px solid #aca899;
            font-family: 'VT323', monospace;
            font-size: 18px;
            display: flex;
            gap: 15px;
        }}
        .menu-bar span {{ cursor: pointer; padding: 2px 6px; }}
        .menu-bar span:hover {{ background: #316ac5; color: white; }}
        
        /* Address Bar Area */
        .address-bar-container {{
            background: #ece9d8;
            padding: 6px 10px;
            border-bottom: 1px solid #aca899;
            display: flex;
            align-items: center;
            gap: 10px;
            font-family: 'VT323', monospace;
            font-size: 18px;
        }}
        .address-bar {{
            flex: 1;
            background: white;
            border: 1px solid #7f9db9;
            padding: 3px 6px;
            display: flex;
            align-items: center;
            box-shadow: inset 1px 1px 2px #ccc;
        }}
        
        /* Main Content Split */
        .content-area {{
            display: flex;
            background: white;
            height: 540px;
            max-height: 80vh;
        }}
        
        /* Sidebar (Left) */
        .sidebar {{
            width: 250px;
            background: linear-gradient(180deg, #7baaff 0%, #b8d4ff 100%);
            border-right: 2px solid #5a8bdc;
            padding: 15px;
            display: flex;
            flex-direction: column;
            gap: 15px;
            align-items: center;
            overflow-y: auto;
            max-height: 100%;
            box-sizing: border-box;
            scrollbar-width: thin;
            scrollbar-color: #ffb2e2 rgba(255, 255, 255, 0.4);
        }}
        .sidebar > * {{
            flex-shrink: 0;
        }}
        .sidebar::-webkit-scrollbar {{
            width: 8px;
        }}
        .sidebar::-webkit-scrollbar-track {{
            background: rgba(255, 255, 255, 0.3);
            border-radius: 4px;
        }}
        .sidebar::-webkit-scrollbar-thumb {{
            background: #ffb2e2;
            border-radius: 4px;
            border: 1px solid #ff88d0;
        }}
        .sidebar::-webkit-scrollbar-thumb:hover {{
            background: #ff88d0;
        }}
        .sidebar-title {{
            font-family: 'VT323', monospace;
            font-size: 40px;
            color: #fffaaa;
            text-shadow: 2px 2px #ff88d0;
            text-align: center;
            line-height: 1;
        }}
        .sidebar-subtitle {{
            background: rgba(255,255,255,0.4);
            border: 2px solid white;
            border-radius: 12px;
            padding: 4px 15px;
            font-size: 14px;
            color: #0055ea;
            font-weight: bold;
        }}
        
        /* Cute Box */
        .cute-box {{
            background: #fff;
            border: 3px solid #ffb2e2;
            border-radius: 15px;
            padding: 10px;
            width: 100%;
            text-align: center;
            box-shadow: 2px 2px 0px #ff88d0;
        }}
        
        .cute-calendar td {{
            text-align: center;
            padding: 2px;
            border-radius: 4px;
        }}
        .cute-calendar td.cal-day-empty {{
            color: #555;
            border: 1px solid transparent;
        }}
        .cute-calendar td.cal-day-no-avisos {{
            color: #666;
            border: 1px solid transparent;
            transition: all 0.15s ease-in-out;
        }}
        .cute-calendar td.cal-day-no-avisos a {{
            color: #666;
            text-decoration: none;
        }}
        .cute-calendar td.cal-day-no-avisos:hover {{
            background: #eef4ff;
            border: 1px dashed #7baaff;
            border-radius: 4px;
        }}
        .cute-calendar td.has-report {{
            background: #fff5fa;
            border: 1px solid #ffb2e2;
            cursor: url('cursor/roundyhand.cur'), pointer !important;
            transition: all 0.15s ease-in-out;
        }}
        .cute-calendar td.has-report:hover {{
            background: #ffe0f0 !important;
            border-color: #ff69b4 !important;
            transform: scale(1.1);
            box-shadow: 0 2px 6px rgba(255, 105, 180, 0.4);
            z-index: 5;
            position: relative;
        }}
        .cal-day-link {{
            text-decoration: none;
            color: #0055ea;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            width: 100%;
            height: 100%;
        }}
        .cal-emojis {{
            font-size: 10px;
            line-height: 1;
            margin-top: 1px;
            display: flex;
            gap: 1px;
            justify-content: center;
        }}
        
        .cute-list {{
            width: 100%;
            display: flex;
            flex-direction: column;
            gap: 10px;
            font-size: 13px;
            color: #003399;
            font-weight: bold;
        }}
        .cute-list-item {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .icon-pixel {{
            width: 24px;
            height: 24px;
            background: #fff;
            border: 2px dotted #ff88d0;
            display: inline-flex;
            justify-content: center;
            align-items: center;
            font-size: 14px;
        }}
        
        /* Main Content (Right) */
        .main-content {{
            flex: 1;
            padding: 20px;
            overflow-y: auto;
            position: relative;
            scrollbar-width: thin;
            scrollbar-color: #ffb2e2 #f0f8ff;
        }}
        .main-content::-webkit-scrollbar, #reportView::-webkit-scrollbar {{
            width: 8px;
        }}
        .main-content::-webkit-scrollbar-track, #reportView::-webkit-scrollbar-track {{
            background: #f0f8ff;
            border-radius: 4px;
        }}
        .main-content::-webkit-scrollbar-thumb, #reportView::-webkit-scrollbar-thumb {{
            background: #ffb2e2;
            border-radius: 4px;
            border: 1px solid #ff88d0;
        }}
        .main-content::-webkit-scrollbar-thumb:hover, #reportView::-webkit-scrollbar-thumb:hover {{
            background: #ff88d0;
        }}
        
        .main-content::after {{
            content: "text text text text text text text text text text text text text";
            position: absolute;
            top: 60px; left: 20px; right: 20px;
            color: #999;
            font-size: 11px;
            z-index: 0;
            pointer-events: none;
            opacity: 0.3;
            word-break: break-all;
        }}
        
        .hello-header {{
            font-family: 'VT323', monospace;
            font-size: 26px;
            color: #000080;
            display: flex;
            align-items: center;
            gap: 10px;
            position: relative;
            z-index: 1;
        }}
        
        /* Buttons area */
        .button-area {{
            display: flex;
            gap: 20px;
            justify-content: center;
            margin: 30px 0;
            position: relative;
            z-index: 1;
        }}
        .xp-button {{
            background: linear-gradient(180deg, #f4f5f5 0%, #dfdfdf 100%);
            border: 1px solid #707070;
            border-radius: 3px;
            padding: 6px 20px;
            font-family: 'VT323', monospace;
            font-size: 18px;
            cursor: pointer;
            box-shadow: inset 1px 1px 1px #fff;
            color: #000;
        }}
        .xp-button:active {{
            background: #dfdfdf;
            box-shadow: inset 1px 1px 2px #888;
        }}
        .btn-green {{
            background: linear-gradient(180deg, #dcf0b2 0%, #bce070 100%);
            border-color: #8bb82d;
            color: #0055ea;
            font-family: 'VT323', monospace;
            font-size: 20px;
            font-weight: bold;
            border-radius: 20px;
            padding: 8px 30px;
            box-shadow: 2px 2px 0 #7ca429;
        }}
        
        /* Report Cards Y2K Style */
        .reports-list {{
            position: relative;
            z-index: 2;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }}
        .month-group {{
            border: 2px solid #7baaff;
            border-radius: 10px;
            background: #f5f9ff;
            box-shadow: 2px 2px 0px #5a8bdc;
            overflow: hidden;
        }}
        .month-header {{
            background: linear-gradient(180deg, #ffb2e2 0%, #ff88d0 100%);
            color: #fff;
            padding: 8px 12px;
            font-family: 'VT323', monospace;
            font-size: 22px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            cursor: url('cursor/roundyhand.cur'), pointer !important;
            list-style: none;
            user-select: none;
            text-shadow: 1px 1px 0px #b3006b;
        }}
        .month-header::-webkit-details-marker {{ display: none; }}
        .month-header-left {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .month-code {{
            font-size: 16px;
            color: #fff5fa;
            opacity: 0.9;
        }}
        .month-stats {{
            display: flex;
            gap: 6px;
            align-items: center;
        }}
        .month-badge {{
            background: rgba(255, 255, 255, 0.9);
            color: #003399;
            font-family: 'Tahoma', sans-serif;
            font-size: 11px;
            font-weight: bold;
            padding: 2px 8px;
            border-radius: 10px;
            text-shadow: none;
            border: 1px solid #ff69b4;
        }}
        .month-badge-pubs {{
            background: #e6f2ff;
            border-color: #5a8bdc;
        }}
        .month-cards {{
            padding: 10px;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }}
        .report-card {{
            background: #fdfdfd;
            border: 2px solid #ffb2e2;
            padding: 10px;
            text-decoration: none;
            color: #000;
            display: flex;
            align-items: center;
            gap: 10px;
            box-shadow: 2px 2px 0px #ffb2e2;
            transition: all 0.1s;
        }}
        .report-card:hover {{
            background: #fff5fa;
            border-color: #ff88d0;
            box-shadow: 2px 2px 0px #ff88d0;
            transform: translate(-1px, -1px);
        }}
        .report-icon {{
            font-size: 24px;
        }}
        .report-info h3 {{
            font-size: 13px;
            color: #003399;
            margin-bottom: 2px;
        }}
        .report-info p {{
            font-size: 11px;
            color: #666;
        }}
        .badge-new {{
            background: #ff0000;
            color: white;
            font-size: 9px;
            padding: 1px 4px;
            vertical-align: middle;
            animation: blink 1s step-start 0s infinite;
        }}
        @keyframes blink {{ 50% {{ opacity: 0; }} }}
        
        /* Estilos de la Vista de Reporte (dentro del dashboard) */
        .back-link {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            margin-bottom: 20px;
            color: #0055ea;
            text-decoration: underline;
            font-family: 'VT323', monospace;
            font-size: 22px;
            cursor: pointer;
        }}
        .back-link:hover {{
            color: #ff3399;
        }}
        .page-header {{
            background: #fff;
            border: 3px solid #ffb2e2;
            border-radius: 15px;
            padding: 15px 20px;
            margin-bottom: 25px;
            text-align: center;
            box-shadow: 2px 2px 0px #ff88d0;
        }}
        .page-header h1 {{
            font-family: 'VT323', monospace;
            font-size: 34px;
            color: #003399;
            line-height: 1.1;
        }}
        .page-header .date {{
            font-size: 14px;
            color: #ff88d0;
            font-weight: bold;
            margin-top: 5px;
        }}
        .stats-row {{
            display: flex;
            justify-content: center;
            gap: 15px;
            margin-top: 15px;
            flex-wrap: wrap;
        }}
        .stat {{
            padding: 4px 15px;
            border: 2px dotted #ffb2e2;
            border-radius: 10px;
            font-size: 12px;
            font-weight: bold;
            background: #fff5fa;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }}
        .stat.alta {{ color: #ff0000; }}
        .stat.media {{ color: #0055ea; }}
        .accordion {{
            margin-bottom: 15px;
            border: 2px solid #a2e8ff;
            border-radius: 8px;
            box-shadow: 2px 2px 0px #7baaff;
            overflow: hidden;
        }}
        .accordion summary {{
            padding: 10px;
            cursor: pointer;
            list-style: none;
            outline: none;
            font-size: 14px;
        }}
        .accordion summary::-webkit-details-marker {{ display: none; }}
        .acc-header {{
            display: flex;
            align-items: flex-start;
            gap: 10px;
        }}
        .acc-title-area {{
            flex: 1;
        }}
        .pixel-circle {{
            width: 12px;
            height: 12px;
            margin-top: 4px;
            box-shadow: 
                2px 0 0 inset, -2px 0 0 inset,
                0 -2px 0 inset, 0 2px 0 inset;
            border: 2px solid #000;
            display: inline-block;
            flex-shrink: 0;
        }}
        .pixel-circle.red {{ background: #ff0000; }}
        .pixel-circle.blue {{ background: #0055ea; }}
        .acc-badge {{
            color: white;
            font-size: 10px;
            padding: 2px 6px;
            border-radius: 4px;
            font-weight: bold;
            display: inline-block;
            margin-bottom: 4px;
        }}
        .acc-title {{
            font-size: 13px;
        }}
        .acc-body {{
            padding: 15px;
            margin: 0 10px 10px 30px;
            background: white;
            border: 1px solid #ccc;
            font-size: 12px;
        }}
        .acc-meta {{
            background: #fdfdfd;
            padding: 8px;
            border: 1px dotted #ccc;
            margin-bottom: 10px;
            color: #666;
        }}
        .acc-summary {{
            margin-bottom: 10px;
            font-weight: bold;
            color: #333;
        }}
        .acc-body ul {{
            padding-left: 20px;
            color: #444;
            margin-bottom: 10px;
        }}
        .acc-body li {{
            margin-bottom: 4px;
        }}
        
        /* Footer Bar */
        .footer-bar {{
            background: linear-gradient(180deg, #5c96ff 0%, #4682ff 100%);
            padding: 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-top: 2px solid #fff;
            font-family: 'VT323', monospace;
            font-size: 20px;
            color: white;
        }}
        .footer-box {{
            border: 2px solid #fff;
            border-radius: 15px;
            padding: 4px 15px;
            color: white;
            font-family: 'VT323', monospace;
            font-size: 20px;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        
        .cute-face {{
            font-size: 24px;
        }}

        /* Responsive Mobile Styles */
        @media (max-width: 768px) {{
            .window-frame {{
                zoom: 1;
                width: 100% !important;
                max-width: 100% !important;
                margin-bottom: 20px !important;
                border-radius: 6px;
            }}
            .content-area {{
                flex-direction: column !important;
                height: auto !important;
                max-height: none !important;
            }}
            .sidebar {{
                width: 100% !important;
                border-right: none !important;
                border-top: 2px solid #5a8bdc !important;
                order: 2 !important;
                overflow-y: visible !important;
                max-height: none !important;
            }}
            .main-content {{
                order: 1 !important;
                padding: 12px !important;
                max-height: none !important;
            }}
            #reportView {{
                height: auto !important;
                max-height: none !important;
            }}
            .corner-gif, .corner-gif-right {{
                max-width: 65px !important;
                opacity: 0.85;
                pointer-events: none;
            }}
            .title-bar-text {{ font-size: 18px !important; }}
            .menu-bar {{ display: none !important; }}
            body {{ padding: 6px !important; }}
            .button-area {{
                margin: 15px 0 !important;
                gap: 10px !important;
            }}
            .btn-green {{
                padding: 6px 18px !important;
                font-size: 16px !important;
            }}
            #flappyModal {{
                width: 95vw !important;
                max-width: 95vw !important;
                height: 85vh !important;
            }}
            #flappyCanvas {{
                width: 100% !important;
                height: 100% !important;
            }}
        }}
        
        @media (hover: none) and (pointer: coarse) {{
            * {{ cursor: auto !important; }}
            a, button, summary, input, .menu-bar span, .footer-box, .report-card, .btn-win, .btn-green, .xp-button, .cute-calendar td.has-report, .cute-calendar td.cal-day-no-avisos {{
                cursor: pointer !important;
            }}
        }}
        
        input, textarea {{
            cursor: text !important;
        }}
    </style>
</head>
<body>
    <div class="window-frame">
        <div class="title-bar">
            <div class="title-bar-text" id="mainTitleBarText">
                <span class="pixel-art icon-star"></span> Monitoreo de DOF y POE
            </div>
            <div class="title-bar-controls">
                <button class="btn-win btn-min">_</button>
                <button class="btn-win btn-close" id="windowCloseBtn" onclick="showDashboard()">X</button>
            </div>
        </div>
        <div class="menu-bar">
            <span onclick="showDashboard()">Inicio</span>
            <span>Ajustes</span>
            <span>Ventana</span>
            <span>Ayuda</span>
        </div>
        <div class="address-bar-container">
            Dirección
            <div class="address-bar" id="addressBarText">
                https://dof-estudiantes-BLOOMS.com
            </div>
            <button class="xp-button" onclick="showDashboard()">Ir</button>
        </div>
        
        <div class="content-area" id="dashboardView">
            <div class="sidebar">
                <div class="sidebar-title">DOF MONITOR</div>
                <div class="sidebar-subtitle">estudiantes</div>
                
                <div class="cute-box">
                    {cal_html}
                </div>
                
                <div class="cute-list">
                    <div class="cute-list-item">
                        <div class="icon-pixel"><div class="pixel-art icon-bow"></div></div>
                        reformas federales
                    </div>
                    <div class="cute-list-item">
                        <div class="icon-pixel"><div class="pixel-art icon-battery"></div></div>
                        quintana roo
                    </div>
                    <div class="cute-list-item">
                        <div class="icon-pixel"><div class="pixel-art icon-clock"></div></div>
                        scjn & poder judicial
                    </div>
                    <div class="cute-list-item">
                        <div class="icon-pixel"><div class="pixel-art icon-star"></div></div>
                        total: {total_reportes} reportes
                    </div>
                </div>
            </div>
            
            <div class="main-content">
                <div class="hello-header">
                    <span class="pixel-art icon-mail"></span> Analizador automático, se actualiza diario a las 2pm
                </div>
                
                <div class="button-area">
                    <button class="btn-green" onclick="document.getElementById('searchInput').focus()">Buscar</button>
                    <button class="btn-green" style="filter: hue-rotate(90deg);" onclick="location.reload()">Actualizar</button>
                </div>
                
                <input type="text" id="searchInput" placeholder="Buscar por fecha o mes (ej. Noviembre, 2026-11)..." oninput="filterReports()" style="width: 100%; margin-bottom: 15px; padding: 5px; border: 2px solid #ffb2e2; border-radius: 5px; outline:none; font-family: 'VT323', monospace; font-size: 18px;">
                
                <div class="reports-list" id="reportsList">
                    {lista_html if lista_html else '<div style="color:red; font-weight:bold; text-align:center;">¡Aún no hay reportes! >_&lt;</div>'}
                </div>
                
                <div style="text-align: center; margin-top: 30px; font-size: 11px; color: #3593ff;">
                    CARGANDO... POR FAVOR ESPERA<br>
                    creado por: Valeria Jimenez Tello
                </div>
            </div>
        </div>
        
        <div class="content-area" id="reportView" style="display:none; padding:20px; overflow-y:auto; flex:1; background:white; position:relative; flex-direction:column;">
        </div>
        
        <div class="footer-bar">
            <div class="footer-box" style="cursor:pointer;" onclick="toggleMusic()" id="musicBtn">
                ▶ música <span class="pixel-art icon-music"></span>
            </div>
            <div class="footer-box" style="cursor:pointer;" onclick="openFlappy()">
                Esperar publicación
            </div>
            <div class="cute-face">
                ૮ ˶ ˆ ᴥ ˆ ˶ ა
            </div>
        </div>
    </div>

    <a href="https://bonaisdemole.neocities.org" target="_blank">
        <img src="corner_gif.gif" class="corner-gif" alt="cute corner anim">
    </a>
    <a href="https://bonaisdemole.neocities.org" target="_blank">
        <img src="rofa_cat.gif" class="corner-gif-right" alt="cute cat anim">
    </a>
    <img src="jellyfish.png" id="floatingJellyfish" class="floating-jellyfish" alt="medusa flotante">

    <audio id="bgMusic" src="bg_music.mp3" loop preload="auto"></audio>

    <script>
        const REPORTS_DATA = {json.dumps(reportes_dict, ensure_ascii=False)};
        const REPORTS_URLS = {json.dumps(reportes_urls, ensure_ascii=False)};
        
        function showReport(dateStr) {{
            const relUrl = (REPORTS_URLS && REPORTS_URLS[dateStr]) ? REPORTS_URLS[dateStr] : (dateStr.slice(0, 7) + '/Resumen_DOF_' + dateStr + '.html');
            if (!REPORTS_DATA || !REPORTS_DATA[dateStr]) {{
                window.location.href = relUrl;
                return;
            }}
            const dash = document.getElementById('dashboardView');
            const rep = document.getElementById('reportView');
            if (dash) dash.style.display = 'none';
            if (rep) {{
                rep.innerHTML = REPORTS_DATA[dateStr];
                rep.style.display = 'block';
                rep.scrollTop = 0;
            }}
            
            const titleEl = document.getElementById('mainTitleBarText');
            if (titleEl) titleEl.innerHTML = '<span class="pixel-art icon-star"></span> Reporte DOF — ' + dateStr;
            const addrEl = document.getElementById('addressBarText');
            if (addrEl) addrEl.innerText = 'https://dof-estudiantes-BLOOMS.com/' + relUrl;
            
            document.title = 'Reporte DOF — ' + dateStr;
            if (window.location.hash !== '#reporte-' + dateStr) {{
                history.pushState(null, '', '#reporte-' + dateStr);
            }}
        }}

        function showDashboard() {{
            const rep = document.getElementById('reportView');
            const dash = document.getElementById('dashboardView');
            if (rep) {{
                rep.style.display = 'none';
                rep.innerHTML = '';
            }}
            if (dash) dash.style.display = 'flex';
            
            const titleEl = document.getElementById('mainTitleBarText');
            if (titleEl) titleEl.innerHTML = '<span class="pixel-art icon-star"></span> Monitoreo de DOF y POE';
            const addrEl = document.getElementById('addressBarText');
            if (addrEl) addrEl.innerText = 'https://dof-estudiantes-BLOOMS.com';
            
            document.title = 'DOF Monitor - Monitoreo de DOF y POE';
            if (window.location.hash && window.location.hash.startsWith('#reporte-')) {{
                history.pushState(null, '', window.location.pathname + window.location.search);
            }}
        }}

        window.addEventListener('popstate', () => {{
            const h = window.location.hash;
            if (h && h.startsWith('#reporte-')) {{
                const d = h.replace('#reporte-', '');
                if (REPORTS_DATA && REPORTS_DATA[d]) showReport(d);
            }} else {{
                showDashboard();
            }}
        }});

        const audio = document.getElementById('bgMusic');
        const btn = document.getElementById('musicBtn');

        function updateMusicBtn(playing) {{
            if (!btn) return;
            btn.innerHTML = playing ? '⏸ música <span class="pixel-art icon-music"></span>' : '▶ música <span class="pixel-art icon-music"></span>';
        }}

        function playMusic() {{
            if (!audio.paused) return;
            audio.play().then(() => {{
                updateMusicBtn(true);
                localStorage.setItem('dof_bgm_playing', 'true');
            }}).catch(() => {{
                updateMusicBtn(false);
                const onFirstClick = () => {{
                    document.removeEventListener('click', onFirstClick);
                    if (localStorage.getItem('dof_bgm_playing') !== 'false') {{
                        audio.play().then(() => updateMusicBtn(true)).catch(() => {{}});
                    }}
                }};
                document.addEventListener('click', onFirstClick, {{ once: true }});
            }});
        }}

        function pauseMusic() {{
            audio.pause();
            updateMusicBtn(false);
            localStorage.setItem('dof_bgm_playing', 'false');
        }}

        function toggleMusic() {{
            if (audio.paused) {{
                playMusic();
            }} else {{
                pauseMusic();
            }}
        }}

        document.addEventListener('DOMContentLoaded', () => {{
            const shouldPlay = localStorage.getItem('dof_bgm_playing');
            if (shouldPlay !== 'false') {{
                playMusic();
            }} else {{
                updateMusicBtn(false);
            }}

            const h = window.location.hash;
            if (h && h.startsWith('#reporte-')) {{
                const d = h.replace('#reporte-', '');
                if (REPORTS_DATA && REPORTS_DATA[d]) showReport(d);
            }}
        }});
        function filterReports() {{
            const query = document.getElementById('searchInput').value.toLowerCase().trim();
            const groups = document.querySelectorAll('.month-group');
            if (groups.length > 0) {{
                groups.forEach((group, idx) => {{
                    const cards = group.querySelectorAll('.report-card');
                    let visibleCount = 0;
                    cards.forEach(card => {{
                        const text = (card.textContent + ' ' + (card.dataset.month || '')).toLowerCase();
                        const match = !query || text.includes(query);
                        card.style.display = match ? 'flex' : 'none';
                        if (match) visibleCount++;
                    }});
                    if (!query) {{
                        group.style.display = 'block';
                        group.open = (idx === 0);
                    }} else if (visibleCount > 0) {{
                        group.style.display = 'block';
                        group.open = true;
                    }} else {{
                        group.style.display = 'none';
                    }}
                }});
            }} else {{
                const cards = document.querySelectorAll('.report-card');
                cards.forEach(card => {{
                    const text = card.textContent.toLowerCase();
                    card.style.display = text.includes(query) ? 'flex' : 'none';
                }});
            }}
        }}

        document.addEventListener('mousemove', function(e) {{
            if (Math.random() > 0.15) return;
            const bubble = document.createElement('div');
            bubble.className = 'cursor-bubble';
            bubble.style.left = e.pageX + 'px';
            bubble.style.top = e.pageY + 'px';
            const size = Math.random() * 8 + 4; 
            bubble.style.width = size + 'px';
            bubble.style.height = size + 'px';
            document.body.appendChild(bubble);
            setTimeout(() => bubble.remove(), 800);
        }});

        // === MEDUSA FLOTANTE (MOVIMIENTO TIPO JELLYFISH) ===
        (function initFloatingJellyfish() {{
            const jelly = document.getElementById('floatingJellyfish');
            if (!jelly) return;

            let jx = Math.max(60, window.innerWidth * 0.18);
            let jy = Math.max(100, window.innerHeight * 0.65);
            let jvx = 0.4;
            let jvy = -0.8;
            let targetX = window.innerWidth * 0.75;
            let targetY = window.innerHeight * 0.25;
            let pulseTimer = 0;
            let pulseDuration = 135;
            let currentAngle = 0;
            let targetAngle = 0;
            let lastJellyTime = performance.now();

            function pickNewTarget() {{
                const pad = 70;
                const w = Math.max(300, window.innerWidth);
                const h = Math.max(300, window.innerHeight);
                targetX = pad + Math.random() * (w - pad * 2);
                targetY = pad + Math.random() * (h - pad * 2);
            }}

            function spawnJellyBubble(x, y) {{
                if (document.hidden) return;
                const b = document.createElement('div');
                b.className = 'cursor-bubble';
                b.style.left = (x + 26 + (Math.random() * 14 - 7)) + 'px';
                b.style.top = (y + 48 + (Math.random() * 8 - 4)) + 'px';
                const size = Math.random() * 6 + 4;
                b.style.width = size + 'px';
                b.style.height = size + 'px';
                document.body.appendChild(b);
                setTimeout(() => b.remove(), 800);
            }}

            function animateJellyfish(now) {{
                let dt = (now - lastJellyTime) / 16.666;
                lastJellyTime = now;
                if (dt > 2.5 || dt <= 0 || isNaN(dt)) dt = 1.0;

                pulseTimer += dt;
                if (pulseTimer >= pulseDuration) {{
                    pulseTimer = 0;
                    pulseDuration = 125 + Math.random() * 35;

                    const dist = Math.hypot(targetX - jx, targetY - jy);
                    if (dist < 140 || Math.random() < 0.25) {{
                        pickNewTarget();
                    }}

                    const dx = targetX - jx;
                    const dy = targetY - jy;
                    const horizDir = Math.max(-1.6, Math.min(1.6, dx * 0.009)) + (Math.random() - 0.5) * 0.5;
                    const vertThrust = dy < 0 ? -(1.9 + Math.random() * 0.55) : -(0.85 + Math.random() * 0.35);

                    jvx += horizDir;
                    jvy = vertThrust;
                    targetAngle = Math.max(-25, Math.min(25, jvx * 14));

                    if (Math.random() < 0.65) {{
                        spawnJellyBubble(jx, jy);
                    }}
                }}

                const phase = pulseTimer / pulseDuration;
                let scaleX = 1.0;
                let scaleY = 1.0;
                if (phase < 0.28) {{
                    // Contracción de la campana al impulsarse
                    const s = Math.sin((phase / 0.28) * Math.PI);
                    scaleX = 1.0 - 0.16 * s;
                    scaleY = 1.0 + 0.18 * s;
                }} else if (phase < 0.68) {{
                    // Expansión suave al abrirse y planear en el agua
                    const s2 = Math.sin(((phase - 0.28) / 0.40) * Math.PI);
                    scaleX = 1.0 + 0.09 * s2;
                    scaleY = 1.0 - 0.08 * s2;
                }} else {{
                    // Flotación tranquila antes del siguiente pulso
                    const s3 = Math.sin(((phase - 0.68) / 0.32) * Math.PI);
                    scaleX = 1.0 - 0.02 * s3;
                    scaleY = 1.0 + 0.02 * s3;
                }}

                jvx *= Math.pow(0.985, dt);
                jvy *= Math.pow(0.975, dt);
                jvy += 0.018 * dt;
                if (jvy > 0.75) jvy = 0.75;

                const waveX = Math.sin(now * 0.0022) * 0.25;
                jx += (jvx + waveX) * dt;
                jy += jvy * dt;

                const maxW = Math.max(100, window.innerWidth - 70);
                const maxH = Math.max(100, window.innerHeight - 75);
                if (jx < 15) {{ jx = 15; jvx = Math.abs(jvx) * 0.8 + 0.4; targetX = window.innerWidth * 0.7; }}
                if (jx > maxW) {{ jx = maxW; jvx = -Math.abs(jvx) * 0.8 - 0.4; targetX = window.innerWidth * 0.3; }}
                if (jy < 15) {{ jy = 15; jvy = 0.4; targetY = window.innerHeight * 0.75; }}
                if (jy > maxH) {{ jy = maxH; jvy = -1.6; targetY = window.innerHeight * 0.25; }}

                const wobble = Math.sin(now * 0.003) * 4;
                currentAngle += ((targetAngle + wobble) - currentAngle) * 0.08 * dt;

                jelly.style.transform = "translate3d(" + jx.toFixed(1) + "px, " + jy.toFixed(1) + "px, 0) rotate(" + currentAngle.toFixed(1) + "deg) scale(" + scaleX.toFixed(3) + ", " + scaleY.toFixed(3) + ")";

                requestAnimationFrame(animateJellyfish);
            }}

            requestAnimationFrame(animateJellyfish);
        }})();
    
        
        // === FLAPPY FISH (MODO TRANQUILO & FRUTIGER AERO) ===
        let fishImg = new Image();
        fishImg.src = '../blue-fish.webp'; 

        let canvas, ctx;
        let gameLoop = null;
        let isGameOver = false;
        let isGameStarted = false;
        let lastTime = 0;
        let frameCount = 0;
        let score = 0;
        let bestScore = parseInt(localStorage.getItem('flappyFishBest') || '0', 10);
        let bubbles = [];
        let particles = [];

        function initBubbles() {{
            bubbles = [];
            let w = canvas ? canvas.width : 376;
            let h = canvas ? canvas.height : 446;
            for (let i = 0; i < 16; i++) {{
                bubbles.push({{
                    x: Math.random() * w,
                    y: Math.random() * h,
                    r: Math.random() * 3.5 + 1.5,
                    speed: Math.random() * 0.35 + 0.2,
                    sway: Math.random() * Math.PI * 2,
                    swaySpeed: Math.random() * 0.02 + 0.01,
                    alpha: Math.random() * 0.35 + 0.15
                }});
            }}
        }}

        function updateAndDrawBubbles(dt) {{
            for (let i = 0; i < bubbles.length; i++) {{
                let b = bubbles[i];
                b.y -= b.speed * dt;
                b.sway += b.swaySpeed * dt;
                let drawX = b.x + Math.sin(b.sway) * 5;
                if (b.y < -10) {{
                    b.y = canvas.height + 10;
                    b.x = Math.random() * canvas.width;
                }}
                ctx.beginPath();
                ctx.arc(drawX, b.y, b.r, 0, Math.PI * 2);
                ctx.fillStyle = "rgba(255, 255, 255, " + b.alpha + ")";
                ctx.fill();
                ctx.strokeStyle = "rgba(255, 255, 255, " + (b.alpha + 0.2) + ")";
                ctx.lineWidth = 1;
                ctx.stroke();
            }}
        }}

        function addSwimParticles(x, y) {{
            for (let i = 0; i < 3; i++) {{
                particles.push({{
                    x: x + (Math.random() * 4 - 2),
                    y: y + (Math.random() * 6 - 3),
                    vx: -(Math.random() * 0.7 + 0.3),
                    vy: (Math.random() - 0.5) * 0.5,
                    r: Math.random() * 2 + 1.2,
                    life: 1.0
                }});
            }}
        }}

        function updateAndDrawParticles(dt) {{
            for (let i = particles.length - 1; i >= 0; i--) {{
                let p = particles[i];
                p.x += p.vx * dt;
                p.y += p.vy * dt;
                p.life -= 0.028 * dt;
                if (p.life <= 0) {{
                    particles.splice(i, 1);
                    continue;
                }}
                ctx.beginPath();
                ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
                ctx.fillStyle = "rgba(255, 255, 255, " + (p.life * 0.5) + ")";
                ctx.fill();
            }}
        }}

        let bird = {{
            x: 55,
            y: 160,
            width: 48,
            height: 34,
            gravity: 0.16,    // Gravedad sincronizada con la nueva velocidad
            jump: 3.5,        // Salto más reactivo
            maxFall: 3.8,     // Caída máxima controlada
            maxRise: -4.0,
            velocity: 0,
            angle: 0,
            draw: function() {{
                ctx.save();
                ctx.translate(this.x + this.width / 2, this.y + this.height / 2);
                ctx.rotate(this.angle);
                if (fishImg.complete && fishImg.naturalWidth !== 0) {{
                    ctx.drawImage(fishImg, -this.width / 2, -this.height / 2, this.width, this.height);
                }} else {{
                    ctx.fillStyle = "#38bdf8";
                    ctx.beginPath();
                    ctx.ellipse(0, 0, this.width / 2, this.height / 2, 0, 0, Math.PI * 2);
                    ctx.fill();
                }}
                ctx.restore();
            }},
            update: function(dt) {{
                this.velocity += this.gravity * dt;
                if (this.velocity > this.maxFall) this.velocity = this.maxFall;
                if (this.velocity < this.maxRise) this.velocity = this.maxRise;
                this.y += this.velocity * dt;

                let targetAngle = Math.max(-0.35, Math.min(0.35, this.velocity * 0.12));
                this.angle += (targetAngle - this.angle) * (0.15 * dt);

                let floorY = canvas.height - 24;
                if (this.y + this.height >= floorY) {{
                    this.y = floorY - this.height;
                    isGameOver = true;
                }}
                if (this.y <= 0) {{
                    this.y = 0;
                    this.velocity = 0;
                }}
            }},
            flap: function() {{
                this.velocity = -this.jump;
                addSwimParticles(this.x, this.y + this.height / 2);
            }}
        }};

        let pipes = {{
            position: [],
            width: 46,
            gap: 148,         // Espacio equilibrado
            dx: 1.95,         // Velocidad aumentada en 0.4 (+0.4)
            draw: function() {{
                for (let i = 0; i < this.position.length; i++) {{
                    let p = this.position[i];
                    let topYPos = p.y;
                    let bottomYPos = p.y + this.gap;
                    let floorY = canvas.height - 24;

                    drawColumn(p.x, 0, this.width, topYPos, true);
                    drawColumn(p.x, bottomYPos, this.width, floorY - bottomYPos, false);
                }}
            }},
            update: function(dt) {{
                frameCount += dt;
                // Cada ~115 ticks (aprox 1.9 segundos) aparece un nuevo pilar
                if (frameCount >= 115) {{
                    frameCount = 0;
                    let minH = 35;
                    let maxH = canvas.height - 24 - this.gap - minH;
                    let randY = Math.random() * (maxH - minH) + minH;
                    this.position.push({{
                        x: canvas.width,
                        y: randY,
                        passed: false
                    }});
                }}

                for (let i = 0; i < this.position.length; i++) {{
                    let p = this.position[i];
                    p.x -= this.dx * dt;

                    // Margen indulgente de impacto (hitbox cómodo)
                    let hit = {{
                        x: bird.x + 8,
                        y: bird.y + 6,
                        w: bird.width - 16,
                        h: bird.height - 12
                    }};

                    if (hit.x + hit.w > p.x && hit.x < p.x + this.width) {{
                        if (hit.y < p.y || hit.y + hit.h > p.y + this.gap) {{
                            isGameOver = true;
                        }}
                    }}

                    if (!p.passed && p.x + this.width < bird.x) {{
                        p.passed = true;
                        score++;
                        if (score > bestScore) {{
                            bestScore = score;
                            localStorage.setItem('flappyFishBest', bestScore);
                        }}
                    }}

                    if (p.x + this.width < -10) {{
                        this.position.splice(i, 1);
                        i--;
                    }}
                }}
            }}
        }};

        function drawColumn(x, y, w, h, isTop) {{
            if (h <= 0) return;
            ctx.save();
            let grad = ctx.createLinearGradient(x, 0, x + w, 0);
            grad.addColorStop(0, '#58cca0');
            grad.addColorStop(0.5, '#7ef0c8');
            grad.addColorStop(1, '#3ca87e');
            ctx.fillStyle = grad;
            ctx.strokeStyle = '#227855';
            ctx.lineWidth = 2;

            ctx.fillRect(x, y, w, h);
            ctx.strokeRect(x, y, w, h);

            // Reflejo satinado vertical
            ctx.fillStyle = 'rgba(255, 255, 255, 0.45)';
            ctx.fillRect(x + 5, y, 6, h);

            // Corona/Tapa decorativa
            let capH = 14;
            let capOverhang = 4;
            let capX = x - capOverhang;
            let capW = w + (capOverhang * 2);
            let capY = isTop ? y + h - capH : y;

            if (h >= capH) {{
                let capGrad = ctx.createLinearGradient(capX, 0, capX + capW, 0);
                capGrad.addColorStop(0, '#6ee2b6');
                capGrad.addColorStop(0.5, '#9bf8d8');
                capGrad.addColorStop(1, '#4ab58b');
                ctx.fillStyle = capGrad;
                ctx.fillRect(capX, capY, capW, capH);
                ctx.strokeRect(capX, capY, capW, capH);

                ctx.fillStyle = 'rgba(255, 255, 255, 0.6)';
                ctx.fillRect(capX + 4, capY + 2, 6, capH - 4);
            }}
            ctx.restore();
        }}

        function drawEnvironment() {{
            let bgGrad = ctx.createLinearGradient(0, 0, 0, canvas.height);
            bgGrad.addColorStop(0, '#bceffa');
            bgGrad.addColorStop(0.45, '#7ed4f5');
            bgGrad.addColorStop(1, '#48a8dc');
            ctx.fillStyle = bgGrad;
            ctx.fillRect(0, 0, canvas.width, canvas.height);

            let floorH = 24;
            let floorY = canvas.height - floorH;
            let floorGrad = ctx.createLinearGradient(0, floorY, 0, canvas.height);
            floorGrad.addColorStop(0, '#e8dc9f');
            floorGrad.addColorStop(1, '#cfc17e');
            ctx.fillStyle = floorGrad;
            ctx.fillRect(0, floorY, canvas.width, floorH);
            
            ctx.strokeStyle = '#a49852';
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.moveTo(0, floorY);
            ctx.lineTo(canvas.width, floorY);
            ctx.stroke();

            // Pequeños retoques de algas marinas en el fondo
            ctx.fillStyle = '#48b888';
            for (let gx = 18; gx < canvas.width; gx += 45) {{
                ctx.beginPath();
                ctx.ellipse(gx, floorY, 5, 4, 0, 0, Math.PI);
                ctx.fill();
            }}
        }}

        function drawScoreHUD() {{
            let hudText = "PUNTOS: " + score + "   •   RÉCORD: " + bestScore;
            ctx.save();
            ctx.font = "22px 'VT323', monospace";
            let textWidth = ctx.measureText(hudText).width;
            let boxW = textWidth + 24;
            let boxH = 28;
            let boxX = (canvas.width - boxW) / 2;
            let boxY = 12;

            ctx.fillStyle = 'rgba(0, 0, 0, 0.35)';
            ctx.strokeStyle = 'rgba(255, 255, 255, 0.85)';
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            if (ctx.roundRect) {{
                ctx.roundRect(boxX, boxY, boxW, boxH, 8);
            }} else {{
                ctx.rect(boxX, boxY, boxW, boxH);
            }}
            ctx.fill();
            ctx.stroke();

            ctx.fillStyle = '#ffffff';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(hudText, canvas.width / 2, boxY + (boxH / 2) + 1);
            ctx.restore();
        }}

        function initFlappy() {{
            canvas = document.getElementById('flappyCanvas');
            ctx = canvas.getContext('2d');
            initBubbles();
        }}

        function openFlappy() {{
            document.getElementById('flappyModal').style.display = 'block';
            if (!canvas) initFlappy();
            if (!isGameStarted) {{
                drawEnvironment();
                updateAndDrawBubbles(1.0);
                bird.y = 160;
                bird.velocity = 0;
                bird.angle = 0;
                bird.draw();
                drawScoreHUD();
            }}
        }}

        function closeFlappy() {{
            document.getElementById('flappyModal').style.display = 'none';
            if (gameLoop) {{
                cancelAnimationFrame(gameLoop);
                gameLoop = null;
            }}
            isGameStarted = false;
            document.getElementById('flappyStart').style.display = 'block';
            document.getElementById('flappyGameOver').style.display = 'none';
        }}

        function startFlappy() {{
            document.getElementById('flappyStart').style.display = 'none';
            document.getElementById('flappyGameOver').style.display = 'none';

            bird.y = 160;
            bird.velocity = 0;
            bird.angle = 0;
            pipes.position = [];
            particles = [];
            score = 0;
            frameCount = 0;
            isGameOver = false;
            isGameStarted = true;
            lastTime = performance.now();

            if (gameLoop) cancelAnimationFrame(gameLoop);
            loop(performance.now());
        }}

        function handlePlayerAction() {{
            if (document.getElementById('flappyModal').style.display === 'none') return;
            if (!isGameStarted || isGameOver) {{
                startFlappy();
            }} else {{
                bird.flap();
            }}
        }}

        function loop(timestamp) {{
            if (!lastTime) lastTime = timestamp;
            let elapsed = timestamp - lastTime;
            lastTime = timestamp;

            // Normalización delta time a 60 FPS estándar (16.666ms = 1.0)
            let dt = elapsed / 16.666;
            if (dt > 2.0) dt = 1.0;
            if (dt < 0.1) dt = 1.0;

            if (isGameOver) {{
                document.getElementById('flappyGameOver').style.display = 'block';
                document.getElementById('flappyScore').innerText = "Puntos: " + score;
                let recSpan = document.getElementById('flappyBestScore');
                if (recSpan) recSpan.innerText = "Récord: " + bestScore;
                let newRecordBadge = document.getElementById('flappyNewRecord');
                if (newRecordBadge) {{
                    newRecordBadge.style.display = (score === bestScore && score > 0) ? 'block' : 'none';
                }}
                gameLoop = null;
                return;
            }}

            drawEnvironment();
            updateAndDrawBubbles(dt);
            updateAndDrawParticles(dt);

            pipes.draw();
            pipes.update(dt);

            bird.draw();
            bird.update(dt);

            drawScoreHUD();

            gameLoop = requestAnimationFrame(loop);
        }}

        document.addEventListener('keydown', function(e) {{
            if (document.getElementById('flappyModal').style.display !== 'none') {{
                if (e.code === 'Space' || e.key === ' ' || e.code === 'ArrowUp' || e.key === 'ArrowUp' || e.key === 'w' || e.key === 'W') {{
                    e.preventDefault();
                    handlePlayerAction();
                }}
            }}
        }}, {{passive: false}});

    </script>

    <!-- Flappy Bird Modal -->
    <div id="flappyModal" style="display:none; position:fixed; top:50%; left:50%; transform:translate(-50%, -50%); width: 400px; height: 500px; background:#ece9d8; border: 2px solid #0055ea; border-radius: 8px 8px 0 0; z-index: 1000; box-shadow: 2px 2px 10px rgba(0,0,0,0.5);">
        <div class="title-bar" style="cursor: move;">
            <div class="title-bar-text">Flappy Fish - Esperando publicación...</div>
            <div class="title-bar-controls">
                <button class="btn-win btn-close" onclick="closeFlappy()">X</button>
            </div>
        </div>
        <div style="padding: 10px; height: calc(100% - 30px); box-sizing: border-box;">
            <div style="border: 2px inset #fff; width: 100%; height: 100%; background: #71c5cf; position: relative; overflow: hidden;" id="gameArea">
                <canvas id="flappyCanvas" width="376" height="446" onmousedown="handlePlayerAction()" ontouchstart="handlePlayerAction(); event.preventDefault();"></canvas>
                
                <div id="flappyStart" style="position:absolute; top:50%; left:50%; transform:translate(-50%, -50%); text-align:center; font-family:'VT323', monospace; color:white; text-shadow:2px 2px 0 #000; cursor:pointer; user-select:none; background:rgba(0,0,0,0.45); padding:16px 26px; border-radius:8px; border:2px solid rgba(255,255,255,0.7); box-shadow:0 6px 20px rgba(0,0,0,0.35);" onclick="handlePlayerAction()" ontouchstart="handlePlayerAction()">
                    <div style="font-size:32px; letter-spacing:1px; margin-bottom:4px;">CLICK PARA JUGAR</div>
                    <div style="font-size:20px; color:#fffae0;">(Presiona ESPACIO o haz click)</div>
                </div>
                
                <div id="flappyGameOver" style="display:none; position:absolute; top:50%; left:50%; transform:translate(-50%, -50%); text-align:center; font-family:'VT323', monospace; color:#ff5555; text-shadow:2px 2px 0 #000; cursor:pointer; user-select:none; background:rgba(0,0,0,0.55); padding:16px 26px; border-radius:8px; border:2px solid rgba(255,255,255,0.7); box-shadow:0 8px 25px rgba(0,0,0,0.45);" onclick="handlePlayerAction()" ontouchstart="handlePlayerAction()">
                    <div style="font-size:36px; letter-spacing:1px; margin-bottom:4px;">¡PERDISTE!</div>
                    <div id="flappyNewRecord" style="display:none; font-size:24px; color:gold; text-shadow:2px 2px 0 #000; margin-bottom:4px;">★ ¡NUEVO RÉCORD! ★</div>
                    <div style="font-size:28px; color:white; margin:4px 0;" id="flappyScore">Puntos: 0</div>
                    <div style="font-size:22px; color:gold; margin-bottom:8px;" id="flappyBestScore">Récord: 0</div>
                    <div style="font-size:22px; color:#7ef0c8;">Click para reintentar</div>
                </div>
            </div>
        </div>
    </div>

</body>
</html>"""

    with open(os.path.join(resumenes_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(dashboard_html)
    
    root_redirect_html = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="0; url=Resumenes_Semanales/index.html">
    <title>DOF Estudiantes - Redirigiendo...</title>
    <script>
        window.location.replace("Resumenes_Semanales/index.html");
    </script>
</head>
<body>
    <p>Redirigiendo al <a href="Resumenes_Semanales/index.html">Dashboard del DOF para Estudiantes</a>...</p>
</body>
</html>
"""
    with open(os.path.join(BASE_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(root_redirect_html)
    logger.info("Dashboard actualizado.")

def _extraer_alertas_existentes(file_path):
    """Extrae alertas existentes de un reporte HTML previo para no perder publicaciones del mismo día."""
    if not os.path.exists(file_path):
        return []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        prev_alertas = []
        for det in soup.find_all("details", class_="accordion"):
            badges = det.find_all("span", class_="acc-badge")
            etiqueta = badges[0].get_text(strip=True) if len(badges) >= 1 else "General"
            ed_full = badges[1].get_text(strip=True) if len(badges) >= 2 else ""
            ed_parts = ed_full.split(" ", 1) if ed_full else []
            edicion = ed_parts[0] if ed_parts else ""
            fecha_ed = ed_parts[1] if len(ed_parts) > 1 else ""

            title_el = det.find("strong", class_="acc-title")
            titulo_corto = title_el.get_text(strip=True) if title_el else ""
            dot_el = det.find("div", class_="pixel-circle")
            nivel_alerta = "Alta" if (dot_el and "red" in (dot_el.get("class") or [])) else "Media"

            dependencia = "General"
            materias = []
            meta_el = det.find("div", class_="acc-meta")
            if meta_el:
                for sp in meta_el.find_all("span"):
                    t = sp.get_text(" ", strip=True)
                    if t.startswith("Dependencia:"):
                        dependencia = t.replace("Dependencia:", "", 1).strip() or "General"
                    elif t.startswith("Materias:"):
                        raw_m = t.replace("Materias:", "", 1).strip()
                        materias = [m.strip() for m in raw_m.split(",") if m.strip()]

            sum_el = det.find("p", class_="acc-summary")
            resumen = sum_el.get_text(" ", strip=True) if sum_el else ""
            puntos = [li.get_text(" ", strip=True) for li in det.find_all("li")]
            link_el = det.find("a", href=True)
            url = link_el["href"].strip() if link_el else ""
            if url:
                prev_alertas.append({
                    "titulo_corto": titulo_corto,
                    "nivel_alerta": nivel_alerta,
                    "etiqueta_especial": etiqueta,
                    "edicion": edicion,
                    "fecha_edicion": fecha_ed,
                    "dependencia": dependencia,
                    "materias_relacionadas": materias,
                    "resumen_estudiantil": resumen,
                    "puntos_clave": puntos,
                    "url": url,
                })
        return prev_alertas
    except Exception as e:
        logger.warning(f"No se pudieron leer alertas previas de {file_path}: {e}")
        return []


def _normalizar_url_alerta(url):
    return (url or "").replace("https://www.dof.gob.mx", DOF_PRIMARY_BASE).replace(".gob.mx//", ".gob.mx/").strip()


def guardar_resumen_estudiante(config, alertas, fecha_corta=None, fecha_larga=None, regenerar=False):
    hoy_mx = ahora_mexico()
    if not fecha_larga:
        fecha_larga = f"{hoy_mx.day} de {MESES_NOMBRES[hoy_mx.month].lower()} de {hoy_mx.year}"
    if not fecha_corta:
        fecha_corta = hoy_mx.strftime('%Y-%m-%d')

    resumenes_dir = os.path.join(BASE_DIR, "Resumenes_Semanales")
    mes_key = fecha_corta[:7] if len(fecha_corta) >= 7 else hoy_mx.strftime('%Y-%m')
    mes_dir = os.path.join(resumenes_dir, mes_key)
    os.makedirs(mes_dir, exist_ok=True)
    file_path = os.path.join(mes_dir, f"Resumen_DOF_{fecha_corta}.html")

    if not regenerar and os.path.exists(file_path):
        prev_alertas = _extraer_alertas_existentes(file_path)
        if prev_alertas:
            if not alertas:
                logger.warning(
                    f"Se omitió sobrescribir {mes_key}/Resumen_DOF_{fecha_corta}.html porque ya contenía "
                    f"{len(prev_alertas)} alertas previas y la corrida actual produjo 0."
                )
                actualizar_dashboard(resumenes_dir)
                return
            urls_nuevas = {_normalizar_url_alerta(a.get('url')) for a in alertas}
            agregadas = 0
            for pa in prev_alertas:
                if _normalizar_url_alerta(pa.get('url')) not in urls_nuevas:
                    alertas.append(pa)
                    agregadas += 1
            if agregadas:
                logger.info(f"Se conservaron {agregadas} alertas previas del mismo día en {fecha_corta}.")

    # Ordenar: Prioridad Alta primero
    orden_prioridad = {"Alta": 1, "Media": 2}
    alertas.sort(key=lambda x: orden_prioridad.get(x.get('nivel_alerta', 'Media'), 3))

    n_alta = sum(1 for a in alertas if a.get('nivel_alerta') == 'Alta')
    n_media = len(alertas) - n_alta

    if not alertas:
        lista_html = '<div class="empty">No se encontraron publicaciones jurídicas relevantes en esta edición.</div>'
    else:
        lista_html = ""
        for i, a in enumerate(alertas, 1):
            materias = html_escape(", ".join(a.get('materias_relacionadas', [])))
            puntos = "".join([f"<li>{html_escape(str(p))}</li>" for p in a.get('puntos_clave', [])])

            nivel = a.get('nivel_alerta', 'Media')
            etiqueta = html_escape(str(a.get('etiqueta_especial', 'General')))
            edicion = html_escape(str(a.get('edicion', '')))
            fecha_ed = html_escape(str(a.get('fecha_edicion', '')))

            # Badge para mostrar "Vespertina 08/09/2026" directamente en el titulo
            badge_edicion = ""
            if edicion:
                badge_edicion = f'<span class="acc-badge" style="background:#555; margin-left:5px;">{edicion} {fecha_ed}</span>'

            if nivel == "Alta":
                accent = "#ff0000"
                bg = "#fff0f0"
                dot_cls = "red"
            else:
                accent = "#0055ea"
                bg = "#f0f8ff"
                dot_cls = "blue"

            titulo_corto = html_escape(str(a.get('titulo_corto', '')))
            dependencia = html_escape(str(a.get('dependencia', 'General')))
            resumen_est = html_escape(str(a.get('resumen_estudiantil', '')))
            nota_url = html_escape(str(a.get('url', '#')), quote=True)

            lista_html += f"""
            <details class="accordion" style="background: {bg};">
              <summary>
                <div class="acc-header">
                    <div class="pixel-circle {dot_cls}"></div>
                    <div class="acc-title-area">
                        <span class="acc-badge" style="background:{accent};">{etiqueta}</span>{badge_edicion}
                        <strong class="acc-title" style="color: {accent}; display: block; margin-top: 4px;">{titulo_corto}</strong>
                    </div>
                </div>
              </summary>
              <div class="acc-body" style="border-left: 2px dashed {accent}; font-family: 'VT323', monospace; font-size: 18px;">
                <div class="acc-meta">
                    <span><strong>Dependencia:</strong> {dependencia}</span><br>
                    <span><strong>Materias:</strong> {materias}</span>
                </div>
                <p class="acc-summary">{resumen_est}</p>
                <ul>{puntos}</ul>
                <a href="{nota_url}" target="_blank" class="xp-button" style="display:inline-block; margin-top:10px; text-decoration:none; font-family:'VT323', monospace; font-size: 16px;">Leer en el DOF →</a>
              </div>
            </details>
            """

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Reporte DOF — {fecha_corta}</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=VT323&display=swap');
        
        * {{ margin: 0; padding: 0; box-sizing: border-box; cursor: url('../cursor/roundynormal.cur'), auto !important; }}
        
        a, button, summary, .menu-bar span, .acc-title, .back-link, .btn-win, .xp-button {{
            cursor: url('../cursor/roundyhand.cur'), pointer !important;
        }}

        .cursor-bubble {{
            position: absolute;
            border: 1px solid rgba(255, 255, 255, 0.9);
            background: rgba(173, 216, 230, 0.5);
            border-radius: 50%;
            pointer-events: none;
            z-index: 999999;
            box-shadow: inset 0 0 4px rgba(255,255,255,0.8);
            animation: floatBubble 0.8s ease-out forwards;
        }}
        @keyframes floatBubble {{
            0% {{ transform: translate(-50%, -50%) scale(1); opacity: 0.8; }}
            100% {{ transform: translate(-50%, -60px) scale(1.5); opacity: 0; }}
        }}
        
        body {{
            font-family: 'Tahoma', 'Verdana', sans-serif;
            min-height: 100vh;
            background: url('https://i.imgur.com/U7RICZA.gif') no-repeat center center fixed;
            background-size: cover;
            color: #000;
            display: flex;
            justify-content: center;
            padding: 20px;
        }}
        
        .pixel-art {{
            display: inline-block;
            width: 16px;
            height: 16px;
            background-size: contain;
            background-repeat: no-repeat;
            image-rendering: pixelated;
            vertical-align: middle;
        }}
        .icon-star {{ background-image: url('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAdElEQVR4nNWSSw6AIAxEOw2X8f6H8TglUWuQ/jCunAUE6BuGD1EuKdaJM1j22oQLmCoTpo9quuNC7VwDbUTjvhG2s4Pn7hlegEnQxkEQ1UDZJUp0nOg1EMFjZGf+5pDAlTncfwALH2PnEp8JyD/WrNW6P6kDjVkmlUQItCIAAAAASUVORK5CYII='); }}
        .icon-mail {{ background-image: url('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAYElEQVR4nOWRQQ7AIAgEd4n//zINiTbEIlgvPXQvijATEoGvw37qKU+DVc94khBfvAFHZFxsix2JzfiNxTcryQw/BJkkgkNBJFnBlha+TpLsl9qyU4D3duiz5WTO48+5ACLhJxUzj6CjAAAAAElFTkSuQmCC'); }}
        .icon-music {{ background-image: url('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAW0lEQVR4nGNkwA/+4xBnhDFYCBjA8D9zC6rO6T4ofCZCBuDTTJIBjBDNcKeT5QIoYKTUAIbBZQALifFP0ID/yHGPLdrwGfAfPdGQ4wIMgOQKjDSATfA/AfnhCABLtQ/rxeyyjAAAAABJRU5ErkJggg=='); }}
        .icon-bow {{ background-image: url('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAdklEQVR4nO1SQQ6AMAgD4r+8+2DvvgyzOB2Dippd7WVJ1zI6IBoE11MBd4dOy4XQdWvMMmdFglaC4hDokzlEUHdZO/GFQ6dyZXGG0wRfNTHF8qhIZi6YXohTiO0YtMt+Iv6TBZnBKLt4tgiP7gGDmX/ZRPpBtAN6wDiJ/D2ftgAAAABJRU5ErkJggg=='); }}
        .icon-battery {{ background-image: url('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAWklEQVR4nGNgoBAw4hD/T6xaRmyajc4aYQieMz6HVT0TMZpBACqO4TIWbIqhtmEzgIEoA9A1YDMQlxdIBkwMFAIWXBL4nD24woCFJl4wwhHn2ADWpEyi+gEGAM5xGFO0y+ueAAAAAElFTkSuQmCC'); }}
        .icon-clock {{ background-image: url('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAdElEQVR4nGNgoBAw4hD/T6xaRmwa28+9xlBYaSSKVQ8jsmZsGnEYBNfHhEszSCGSrXAAVfcf3QCsgBgXMRHrdFyuYGKgELDgk0QOA1yuZMGlmVhvMYGiBFtoExuVTCTpxOECBlJcgZ6QqJqUMQxCA7jUUgYAzE4nHD8rGUkAAAAASUVORK5CYII='); }}
        
        .corner-gif {{
            position: fixed;
            bottom: 5px;
            left: 5px;
            z-index: 1000;
            max-width: 150px;
            height: auto;
            transition: transform 0.2s ease-in-out;
        }}
        .corner-gif:hover {{
            transform: translateY(-10px);
        }}
        .corner-gif-right {{
            position: fixed;
            bottom: 5px;
            right: 5px;
            z-index: 1000;
            max-width: 150px;
            height: auto;
            transition: transform 0.2s ease-in-out;
        }}
        .corner-gif-right:hover {{
            transform: translateY(-10px);
        }}
        .floating-jellyfish {{
            position: fixed;
            top: 0;
            left: 0;
            width: 58px;
            height: auto;
            image-rendering: pixelated;
            pointer-events: none;
            z-index: 9998;
            filter: drop-shadow(0 0 8px rgba(80, 210, 255, 0.55));
            will-change: transform;
            transform-origin: 50% 38%;
        }}
        
        /* XP Window Frame */
        .window-frame {{
            /* zoom removed */
            width: 850px;
            max-width: 100%;
            background: #ece9d8;
            border: 2px solid #0055ea;
            border-radius: 8px 8px 0 0;
            box-shadow: 2px 2px 10px rgba(0,0,0,0.5);
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }}
        
        /* Title Bar */
        .title-bar {{
            background: linear-gradient(180deg, #0058ee 0%, #3593ff 8%, #288eff 40%, #127dff 88%, #0366e8 100%);
            padding: 4px 8px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #000;
        }}
        .title-bar-text {{
            color: white;
            font-family: 'VT323', monospace;
            font-size: 24px;
            text-shadow: 1px 1px #000;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .title-bar-controls {{ display: flex; gap: 4px; }}
        .btn-win {{
            width: 24px; height: 24px; border: 1px solid #fff; border-radius: 4px; color: white; font-weight: bold; cursor: pointer;
            box-shadow: inset -1px -1px 2px rgba(0,0,0,0.5), inset 1px 1px 2px rgba(255,255,255,0.8);
            display: flex; align-items: center; justify-content: center; font-family: Arial;
        }}
        .btn-close {{ background: linear-gradient(180deg, #e3685e, #c33a30); border-color: #f7a8a1; }}
        
        /* Menu Bar */
        .menu-bar {{
            background: #ece9d8; padding: 2px 10px; border-bottom: 1px solid #aca899; font-family: 'VT323', monospace; font-size: 18px; display: flex; gap: 15px;
        }}
        
        .content-area {{
            background: white;
            padding: 20px;
            flex: 1;
            position: relative;
        }}
        
        .back-link {{
            display: inline-block;
            margin-bottom: 20px;
            color: #0055ea;
            text-decoration: underline;
            font-size: 13px;
            cursor: pointer;
        }}
        
        .page-header {{
            background: #fff;
            border: 3px solid #ffb2e2;
            border-radius: 15px;
            padding: 20px;
            margin-bottom: 25px;
            text-align: center;
            box-shadow: 2px 2px 0px #ff88d0;
        }}
        .page-header h1 {{
            font-family: 'VT323', monospace;
            font-size: 36px;
            color: #003399;
        }}
        .page-header .date {{
            font-size: 14px;
            color: #ff88d0;
            font-weight: bold;
            margin-top: 5px;
        }}
        
        .stats-row {{
            display: flex;
            justify-content: center;
            gap: 15px;
            margin-top: 15px;
        }}
        .stat {{
            padding: 4px 15px;
            border: 2px dotted #ffb2e2;
            border-radius: 10px;
            font-size: 12px;
            font-weight: bold;
            background: #fff5fa;
        }}
        .stat.alta {{ color: #ff0000; }}
        .stat.media {{ color: #0055ea; }}
        
        .accordion {{
            margin-bottom: 15px;
            border: 2px solid #a2e8ff;
            border-radius: 8px;
            box-shadow: 2px 2px 0px #7baaff;
        }}
        .accordion summary {{
            padding: 10px;
            cursor: pointer;
            list-style: none;
            outline: none;
            font-size: 14px;
        }}
        .accordion summary::-webkit-details-marker {{ display: none; }}
        
        .acc-header {{ display: flex; align-items: flex-start; gap: 10px; }}
        .pixel-circle {{
            width: 12px;
            height: 12px;
            margin-top: 4px;
            box-shadow: 
                2px 0 0 inset, -2px 0 0 inset,
                0 -2px 0 inset, 0 2px 0 inset;
            border: 2px solid #000;
        }}
        .pixel-circle.red {{ background: #ff0000; }}
        .pixel-circle.blue {{ background: #0055ea; }}
        
        .acc-badge {{
            color: white; font-size: 10px; padding: 2px 6px; border-radius: 4px; font-weight: bold; display: inline-block; margin-bottom: 4px;
        }}
        .acc-title {{ font-size: 13px; }}
        
        .acc-body {{
            padding: 15px;
            margin: 0 10px 10px 30px;
            background: white;
            border: 1px solid #ccc;
            font-size: 12px;
        }}
        .acc-meta {{
            background: #fdfdfd; padding: 8px; border: 1px dotted #ccc; margin-bottom: 10px; color: #666;
        }}
        .acc-summary {{ margin-bottom: 10px; font-weight: bold; color: #333; }}
        ul {{ padding-left: 20px; color: #444; margin-bottom: 10px; }}
        li {{ margin-bottom: 4px; }}
        
        .xp-button {{
            background: linear-gradient(180deg, #f4f5f5 0%, #dfdfdf 100%);
            border: 1px solid #707070;
            border-radius: 3px;
            padding: 4px 15px;
            font-family: 'VT323', monospace;
            font-size: 16px;
            cursor: pointer;
            color: #000;
            box-shadow: inset 1px 1px 1px #fff;
        }}
        
        .footer-bar {{
            background: linear-gradient(180deg, #5c96ff 0%, #4682ff 100%);
            padding: 8px 10px;
            display: flex;
            justify-content: flex-end;
            border-top: 2px solid #fff;
            color: white;
            font-family: 'VT323', monospace;
            font-size: 20px;
        }}

        /* Responsive Mobile Styles */
        @media (max-width: 768px) {{
            .window-frame {{
                zoom: 1;
                width: 100% !important;
                max-width: 100% !important;
                margin-bottom: 20px !important;
                border-radius: 6px;
            }}
            .content-area {{
                padding: 12px !important;
            }}
            .page-header {{
                padding: 12px !important;
                margin-bottom: 15px !important;
            }}
            .page-header h1 {{
                font-size: 26px !important;
            }}
            .acc-body {{
                margin: 0 0 10px 10px !important;
                padding: 10px !important;
            }}
            .corner-gif, .corner-gif-right {{
                display: none !important;
            }}
            .title-bar-text {{ font-size: 18px !important; }}
            .menu-bar {{ display: none !important; }}
            body {{ padding: 6px !important; }}
            #flappyModal {{
                width: 95vw !important;
                max-width: 95vw !important;
                height: 85vh !important;
            }}
            #flappyCanvas {{
                width: 100% !important;
                height: 100% !important;
            }}
        }}
        
        @media (hover: none) and (pointer: coarse) {{
            * {{ cursor: auto !important; }}
            a, button, summary, input, .menu-bar span, .acc-title, .back-link, .btn-win, .xp-button {{
                cursor: pointer !important;
            }}
        }}
        
        input, textarea {{
            cursor: text !important;
        }}
    </style>
</head>
<body>
    <div class="window-frame">
        <div class="title-bar">
            <div class="title-bar-text">
                <span class="pixel-art icon-star"></span> Reporte DOF — {fecha_corta}
            </div>
            <div class="title-bar-controls">
                <button class="btn-win btn-close" onclick="window.location.href='../index.html'">X</button>
            </div>
        </div>
        <div class="menu-bar">
            <span>File</span>
            <span>Edit</span>
            <span>View</span>
        </div>
        
        <div class="content-area">
            <a href="../index.html" class="back-link"><span class="pixel-art icon-star"></span> Regresar</a>
            <div class="page-header">
                <h1>Reporte Diario</h1>
                <div class="date">{fecha_larga}</div>
                <div style="font-family: 'VT323', monospace; font-size: 16px; color: #666; text-align: center; margin-bottom: 10px;">
                    * Incluye edición Matutina y Vespertina de hoy, y Vespertina del día hábil anterior.
                </div>
                <div class="stats-row">
                    <div class="stat alta"><div class="pixel-circle red"></div> {n_alta} alta</div>
                    <div class="stat media"><div class="pixel-circle blue"></div> {n_media} media</div>
                    <div class="stat total"><div class="pixel-art icon-star"></div> {len(alertas)} total</div>
                </div>
            </div>
            
            {lista_html}
            
        </div>
        <div class="footer-bar" style="display:flex; justify-content:space-between; align-items:center;">
            <div style="cursor:pointer;" onclick="toggleMusic()" id="musicBtn">▶ música <span class="pixel-art icon-music"></span></div>
            <div>૮ ˶ ˆ ᴥ ˆ ˶ ა ¡adiós!</div>
        </div>
    </div>
    
    <a href="https://bonaisdemole.neocities.org" target="_blank">
        <img src="../corner_gif.gif" class="corner-gif" alt="cute corner anim">
    </a>
    <a href="https://bonaisdemole.neocities.org" target="_blank">
        <img src="../rofa_cat.gif" class="corner-gif-right" alt="cute cat anim">
    </a>
    <img src="../jellyfish.png" id="floatingJellyfish" class="floating-jellyfish" alt="medusa flotante">

    <audio id="bgMusic" src="../bg_music.mp3" loop preload="auto"></audio>
    
    <script>
        const audio = document.getElementById('bgMusic');
        const btn = document.getElementById('musicBtn');

        function updateMusicBtn(playing) {{
            if (!btn) return;
            btn.innerHTML = playing ? '⏸ música <span class="pixel-art icon-music"></span>' : '▶ música <span class="pixel-art icon-music"></span>';
        }}

        function playMusic() {{
            if (!audio.paused) return;
            audio.play().then(() => {{
                updateMusicBtn(true);
                localStorage.setItem('dof_bgm_playing', 'true');
            }}).catch(() => {{
                updateMusicBtn(false);
                const onFirstClick = () => {{
                    document.removeEventListener('click', onFirstClick);
                    if (localStorage.getItem('dof_bgm_playing') !== 'false') {{
                        audio.play().then(() => updateMusicBtn(true)).catch(() => {{}});
                    }}
                }};
                document.addEventListener('click', onFirstClick, {{ once: true }});
            }});
        }}

        function pauseMusic() {{
            audio.pause();
            updateMusicBtn(false);
            localStorage.setItem('dof_bgm_playing', 'false');
        }}

        function toggleMusic() {{
            if (audio.paused) {{
                playMusic();
            }} else {{
                pauseMusic();
            }}
        }}

        document.addEventListener('DOMContentLoaded', () => {{
            const shouldPlay = localStorage.getItem('dof_bgm_playing');
            if (shouldPlay !== 'false') {{
                playMusic();
            }} else {{
                updateMusicBtn(false);
            }}
        }});
        
        document.addEventListener('mousemove', function(e) {{
            if (Math.random() > 0.15) return;
            const bubble = document.createElement('div');
            bubble.className = 'cursor-bubble';
            bubble.style.left = e.pageX + 'px';
            bubble.style.top = e.pageY + 'px';
            const size = Math.random() * 8 + 4; 
            bubble.style.width = size + 'px';
            bubble.style.height = size + 'px';
            document.body.appendChild(bubble);
            setTimeout(() => bubble.remove(), 800);
        }});

        // === MEDUSA FLOTANTE (MOVIMIENTO TIPO JELLYFISH) ===
        (function initFloatingJellyfish() {{
            const jelly = document.getElementById('floatingJellyfish');
            if (!jelly) return;

            let jx = Math.max(60, window.innerWidth * 0.18);
            let jy = Math.max(100, window.innerHeight * 0.65);
            let jvx = 0.4;
            let jvy = -0.8;
            let targetX = window.innerWidth * 0.75;
            let targetY = window.innerHeight * 0.25;
            let pulseTimer = 0;
            let pulseDuration = 135;
            let currentAngle = 0;
            let targetAngle = 0;
            let lastJellyTime = performance.now();

            function pickNewTarget() {{
                const pad = 70;
                const w = Math.max(300, window.innerWidth);
                const h = Math.max(300, window.innerHeight);
                targetX = pad + Math.random() * (w - pad * 2);
                targetY = pad + Math.random() * (h - pad * 2);
            }}

            function spawnJellyBubble(x, y) {{
                if (document.hidden) return;
                const b = document.createElement('div');
                b.className = 'cursor-bubble';
                b.style.left = (x + 26 + (Math.random() * 14 - 7)) + 'px';
                b.style.top = (y + 48 + (Math.random() * 8 - 4)) + 'px';
                const size = Math.random() * 6 + 4;
                b.style.width = size + 'px';
                b.style.height = size + 'px';
                document.body.appendChild(b);
                setTimeout(() => b.remove(), 800);
            }}

            function animateJellyfish(now) {{
                let dt = (now - lastJellyTime) / 16.666;
                lastJellyTime = now;
                if (dt > 2.5 || dt <= 0 || isNaN(dt)) dt = 1.0;

                pulseTimer += dt;
                if (pulseTimer >= pulseDuration) {{
                    pulseTimer = 0;
                    pulseDuration = 125 + Math.random() * 35;

                    const dist = Math.hypot(targetX - jx, targetY - jy);
                    if (dist < 140 || Math.random() < 0.25) {{
                        pickNewTarget();
                    }}

                    const dx = targetX - jx;
                    const dy = targetY - jy;
                    const horizDir = Math.max(-1.6, Math.min(1.6, dx * 0.009)) + (Math.random() - 0.5) * 0.5;
                    const vertThrust = dy < 0 ? -(1.9 + Math.random() * 0.55) : -(0.85 + Math.random() * 0.35);

                    jvx += horizDir;
                    jvy = vertThrust;
                    targetAngle = Math.max(-25, Math.min(25, jvx * 14));

                    if (Math.random() < 0.65) {{
                        spawnJellyBubble(jx, jy);
                    }}
                }}

                const phase = pulseTimer / pulseDuration;
                let scaleX = 1.0;
                let scaleY = 1.0;
                if (phase < 0.28) {{
                    const s = Math.sin((phase / 0.28) * Math.PI);
                    scaleX = 1.0 - 0.16 * s;
                    scaleY = 1.0 + 0.18 * s;
                }} else if (phase < 0.68) {{
                    const s2 = Math.sin(((phase - 0.28) / 0.40) * Math.PI);
                    scaleX = 1.0 + 0.09 * s2;
                    scaleY = 1.0 - 0.08 * s2;
                }} else {{
                    const s3 = Math.sin(((phase - 0.68) / 0.32) * Math.PI);
                    scaleX = 1.0 - 0.02 * s3;
                    scaleY = 1.0 + 0.02 * s3;
                }}

                jvx *= Math.pow(0.985, dt);
                jvy *= Math.pow(0.975, dt);
                jvy += 0.018 * dt;
                if (jvy > 0.75) jvy = 0.75;

                const waveX = Math.sin(now * 0.0022) * 0.25;
                jx += (jvx + waveX) * dt;
                jy += jvy * dt;

                const maxW = Math.max(100, window.innerWidth - 70);
                const maxH = Math.max(100, window.innerHeight - 75);
                if (jx < 15) {{ jx = 15; jvx = Math.abs(jvx) * 0.8 + 0.4; targetX = window.innerWidth * 0.7; }}
                if (jx > maxW) {{ jx = maxW; jvx = -Math.abs(jvx) * 0.8 - 0.4; targetX = window.innerWidth * 0.3; }}
                if (jy < 15) {{ jy = 15; jvy = 0.4; targetY = window.innerHeight * 0.75; }}
                if (jy > maxH) {{ jy = maxH; jvy = -1.6; targetY = window.innerHeight * 0.25; }}

                const wobble = Math.sin(now * 0.003) * 4;
                currentAngle += ((targetAngle + wobble) - currentAngle) * 0.08 * dt;

                jelly.style.transform = "translate3d(" + jx.toFixed(1) + "px, " + jy.toFixed(1) + "px, 0) rotate(" + currentAngle.toFixed(1) + "deg) scale(" + scaleX.toFixed(3) + ", " + scaleY.toFixed(3) + ")";

                requestAnimationFrame(animateJellyfish);
            }}

            requestAnimationFrame(animateJellyfish);
        }})();
    
        
        let fishImg = new Image();
        fishImg.src = '../../blue-fish.webp'; 

        let canvas, ctx;
        let gameLoop;
        let isGameOver = false;
        let frames = 0;
        let score = 0;

        let bird = {{
            x: 50, y: 150, width: 51, height: 36,
            gravity: 0.20, jump: 4.0, velocity: 0,
            draw: function() {{
                ctx.drawImage(fishImg, this.x, this.y, this.width, this.height);
            }},
            update: function() {{
                this.velocity += this.gravity;
                this.y += this.velocity;
                
                if(this.y + this.height >= canvas.height) {{
                    this.y = canvas.height - this.height;
                    isGameOver = true;
                }}
                if(this.y <= 0) {{
                    this.y = 0;
                    this.velocity = 0;
                }}
            }},
            flap: function() {{
                this.velocity = -this.jump;
            }}
        }};

        let pipes = {{
            position: [],
            width: 40,
            gap: 130, 
            dx: 2,
            draw: function() {{
                for(let i=0; i<this.position.length; i++) {{
                    let p = this.position[i];
                    let topYPos = p.y;
                    let bottomYPos = p.y + this.gap;
                    
                    ctx.fillStyle = "#a8e67a";
                    ctx.strokeStyle = "#72c43a";
                    ctx.lineWidth = 4;
                    
                    ctx.fillRect(p.x, 0, this.width, topYPos);
                    ctx.strokeRect(p.x, 0, this.width, topYPos);
                    
                    ctx.fillRect(p.x, bottomYPos, this.width, canvas.height - bottomYPos);
                    ctx.strokeRect(p.x, bottomYPos, this.width, canvas.height - bottomYPos);
                }}
            }},
            update: function() {{
                if(frames % 100 == 0) {{
                    this.position.push({{
                        x: canvas.width,
                        y: Math.random() * (canvas.height - this.gap - 40) + 20
                    }});
                }}
                for(let i=0; i<this.position.length; i++) {{
                    let p = this.position[i];
                    p.x -= this.dx;
                    
                    if(p.x + this.width <= 0) {{
                        this.position.shift();
                        score++;
                        i--;
                        continue;
                    }}
                    
                    if(bird.x + bird.width > p.x && bird.x < p.x + this.width) {{
                        if(bird.y < p.y || bird.y + bird.height > p.y + this.gap) {{
                            isGameOver = true;
                        }}
                    }}
                }}
            }}
        }};

        function initFlappy() {{
            canvas = document.getElementById('flappyCanvas');
            ctx = canvas.getContext('2d');
            ctx.lineWidth = 2;
            ctx.strokeStyle = "#543847";
        }}

        function openFlappy() {{
            document.getElementById('flappyModal').style.display = 'block';
            if(!canvas) initFlappy();
        }}

        function closeFlappy() {{
            document.getElementById('flappyModal').style.display = 'none';
            if (gameLoop) cancelAnimationFrame(gameLoop);
            document.getElementById('flappyStart').style.display = 'block';
            document.getElementById('flappyGameOver').style.display = 'none';
        }}

        function startFlappy() {{
            document.getElementById('flappyStart').style.display = 'none';
            document.getElementById('flappyGameOver').style.display = 'none';
            
            bird.y = 150;
            bird.velocity = 0;
            pipes.position = [];
            score = 0;
            frames = 0;
            isGameOver = false;
            
            loop();
        }}

        function loop() {{
            if(isGameOver) {{
                document.getElementById('flappyGameOver').style.display = 'block';
                document.getElementById('flappyScore').innerText = "Score: " + score;
                return;
            }}
            
            ctx.fillStyle = "#bae4f5";
            ctx.fillRect(0, 0, canvas.width, canvas.height);
            
            bird.draw();
            bird.update();
            
            pipes.draw();
            pipes.update();
            
            ctx.fillStyle = "white";
            ctx.font = "30px 'VT323', monospace";
            ctx.fillText(score, canvas.width/2 - 10, 40);
            ctx.strokeText(score, canvas.width/2 - 10, 40);
            
            frames++;
            gameLoop = requestAnimationFrame(loop);
        }}

        document.addEventListener('keydown', function(e) {{
            if((e.code === 'Space' || e.key === ' ') && document.getElementById('flappyModal').style.display !== 'none') {{
                e.preventDefault(); 
                if(!isGameOver && document.getElementById('flappyStart').style.display === 'none') {{
                    bird.flap();
                }} else if (document.getElementById('flappyStart').style.display !== 'none') {{
                    startFlappy();
                }} else if (isGameOver) {{
                    startFlappy();
                }}
            }}
        }}, {{passive: false}});

    </script>

    <!-- Flappy Bird Modal -->
    <div id="flappyModal" style="display:none; position:fixed; top:50%; left:50%; transform:translate(-50%, -50%); width: 400px; height: 500px; background:#ece9d8; border: 2px solid #0055ea; border-radius: 8px 8px 0 0; z-index: 1000; box-shadow: 2px 2px 10px rgba(0,0,0,0.5);">
        <div class="title-bar" style="cursor: move;">
            <div class="title-bar-text">Flappy Fish - Esperando publicación...</div>
            <div class="title-bar-controls">
                <button class="btn-win btn-close" onclick="closeFlappy()">X</button>
            </div>
        </div>
        <div style="padding: 10px; height: calc(100% - 30px); box-sizing: border-box;">
            <div style="border: 2px inset #fff; width: 100%; height: 100%; background: #71c5cf; position: relative; overflow: hidden;" id="gameArea">
                <canvas id="flappyCanvas" width="376" height="446" onmousedown="if(!isGameOver && document.getElementById('flappyStart').style.display == 'none') bird.flap();" ontouchstart="if(!isGameOver && document.getElementById('flappyStart').style.display == 'none') {{ bird.flap(); event.preventDefault(); }}"></canvas>
                
                <div id="flappyStart" style="position:absolute; top:50%; left:50%; transform:translate(-50%, -50%); text-align:center; font-family:'VT323',monospace; font-size:24px; color:white; text-shadow:2px 2px 0 #000; cursor:pointer;" onclick="startFlappy()" ontouchstart="startFlappy()">
                    CLICK PARA JUGAR<br><span style="font-size:16px;">(Presiona ESPACIO o TOCA para saltar)</span>
                </div>
                
                <div id="flappyGameOver" style="display:none; position:absolute; top:50%; left:50%; transform:translate(-50%, -50%); text-align:center; font-family:'VT323',monospace; font-size:32px; color:#ff4444; text-shadow:2px 2px 0 #000; cursor:pointer;" onclick="startFlappy()" ontouchstart="startFlappy()">
                    ¡PERDISTE!<br><span style="font-size:20px; color:white;">Click para reintentar</span><br><span style="font-size:18px; color:gold;" id="flappyScore">Score: 0</span>
                </div>
            </div>
        </div>
    </div>

</body>
</html>"""
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"Resumen guardado en carpeta mensual: {mes_key}/Resumen_DOF_{fecha_corta}.html")
    actualizar_dashboard(resumenes_dir)

def main():
    parser = argparse.ArgumentParser(description="Monitor DOF para Estudiantes de Derecho")
    parser.add_argument("--fecha", help="Fecha objetivo YYYY-MM-DD (por defecto: hoy hora México)")
    parser.add_argument("--regenerar", action="store_true", help="Reemplazar el reporte de esa fecha sin fusionar con el anterior")
    args = parser.parse_args()

    fecha_dt = datetime.strptime(args.fecha, "%Y-%m-%d") if args.fecha else ahora_mexico()
    fecha_corta = fecha_dt.strftime('%Y-%m-%d')
    fecha_larga = f"{fecha_dt.day} de {MESES_NOMBRES[fecha_dt.month].lower()} de {fecha_dt.year}"

    logger.info(f"=== INICIANDO MONITOREO DOF (ESTUDIANTES) | Fecha objetivo: {fecha_corta} ===")
    config_path = os.path.join(BASE_DIR, 'dof-estudiantes-config.json')
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"No se encontró el archivo de configuración: {config_path}")
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    pubs_dof = fetch_dof_portada(fecha_objetivo=fecha_dt)
    if not pubs_dof:
        logger.info("No se encontraron publicaciones en el DOF para la fecha consultada.")
        return
    pubs_dof_filtradas = pre_filtro_regex(pubs_dof, config)
    sobrevivientes = primer_filtro_ia(pubs_dof_filtradas, config, "DOF Federal")

    if not sobrevivientes:
        guardar_resumen_estudiante(config, [], fecha_corta=fecha_corta, fecha_larga=fecha_larga, regenerar=args.regenerar)
        logger.info("=== PROCESO FINALIZADO (SIN PUBLICACIONES RELEVANTES) ===")
        return

    analisis_final = analizar_profundo_uno_por_uno(sobrevivientes, config)
    guardar_resumen_estudiante(config, analisis_final, fecha_corta=fecha_corta, fecha_larga=fecha_larga, regenerar=args.regenerar)
    logger.info("=== PROCESO FINALIZADO EXITOSAMENTE ===")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception(f"Error fatal durante el monitoreo del DOF: {e}")
        sys.exit(1)
