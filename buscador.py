"""Vigilancia diaria de vuelos: puente de diciembre, esquí y ofertas sorpresa.

Fuentes:
  - Google Flights (protocolo interno, vía la librería fast-flights; sin API key)
  - Tarifas públicas de Ryanair (ofertas sorpresa desde Bilbao y alrededores)

Salidas (carpeta data/):
  - ultimo.md     : resumen legible de hoy (lo lee la tarea diaria de Claude)
  - ultimo.json   : lo mismo, estructurado
  - historico.csv : precios día a día (últimos N días) para detectar bajadas
  - sin_ruta.json : rutas/días de la semana sin vuelos, para no repetir consultas inútiles

Diseño "a prueba de fallos":
  - Ningún fallo de una consulta, fuente o campaña detiene al resto.
  - Siempre se escribe data/ultimo.md (aunque sea solo con avisos).
  - "No hay vuelos" (respuesta válida de Google) se distingue de "error de red/bloqueo";
    solo lo segundo se reintenta.
  - Límite de tiempo y de consultas propios, por debajo del límite de GitHub.
  - Si Google cambia el formato (0 resultados válidos en muchas consultas), se detecta,
    se avisa y NO se contamina la memoria de rutas sin vuelos.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

try:  # si la librería falla al importar, seguimos con Ryanair y lo indicamos en el informe
    from fast_flights import FlightQuery, Passengers, create_query
    from fast_flights.parser import parse as parse_html
    ERROR_IMPORT_GOOGLE = None
except Exception as _e:  # pragma: no cover
    ERROR_IMPORT_GOOGLE = f"no se pudo importar fast-flights ({type(_e).__name__}: {_e})"

try:
    import primp
except Exception:  # pragma: no cover
    primp = None

RAIZ = Path(__file__).resolve().parent
DATA = RAIZ / "data"
HISTORICO = DATA / "historico.csv"
SIN_RUTA = DATA / "sin_ruta.json"
URL_GOOGLE = "https://www.google.com/travel/flights"
URL_RYANAIR = "https://services-api.ryanair.com/farfnd/v4/roundTripFares"
CAMPOS_HIST = [
    "fecha_consulta", "campana", "viajero", "origen", "destino", "fecha",
    "tramo", "precio", "aerolineas", "salida", "llegada", "escalas",
    "duracion_vuelo_min", "fuente", "enlace",
]

# Valores por defecto (copia de config.yaml): si config.yaml falta, está mal escrito
# o le falta alguna clave, se usa esto y el programa sigue funcionando completo.
DEFECTO = {'moneda': 'EUR',
 'idioma': 'es',
 'pausa_entre_peticiones_s': 1.0,
 'max_peticiones_google': 900,
 'tiempo_max_google_min': 70,
 'timeout_peticion_s': 30,
 'max_escalas': 1,
 'historico_dias': 60,
 'umbral_aviso_ida_vuelta_eur': 150,
 'bajada_relevante_pct': 20,
 'viajeros': {'A': {'nombre': 'Stuttgart',
                    'aeropuertos': ['STR', 'FKB', 'FMM', 'FDH', 'FRA', 'MUC', 'ZRH', 'BSL'],
                    'principales_esqui': ['STR', 'FMM', 'MUC', 'ZRH', 'BSL']},
              'B': {'nombre': 'Bilbao',
                    'aeropuertos': ['BIO', 'EAS', 'VIT', 'SDR', 'BIQ', 'PNA'],
                    'principales_esqui': ['BIO', 'SDR', 'BIQ']}},
 'destinos_esqui': [{'zona': 'Tirol (Innsbruck, Zillertal, Stubai)',
                     'aeropuertos': ['INN', 'MUC', 'SZG'],
                     'vuela_A': False},
                    {'zona': 'Salzburgerland (Ski Amadé, Saalbach)',
                     'aeropuertos': ['SZG', 'MUC'],
                     'vuela_A': False},
                    {'zona': 'Valais / Alpes franceses (Verbier, Chamonix, Tres Valles)',
                     'aeropuertos': ['GVA', 'LYS', 'CMF', 'GNB'],
                     'vuela_A': False},
                    {'zona': 'Dolomitas (Val Gardena, Alta Badia, Cortina)',
                     'aeropuertos': ['VRN', 'BGY', 'VCE', 'TSF', 'INN'],
                     'vuela_A': False},
                    {'zona': 'Valle de Aosta / Piamonte (Cervinia, Courmayeur, Vía Láctea)',
                     'aeropuertos': ['TRN', 'MXP', 'GVA'],
                     'vuela_A': False},
                    {'zona': 'Pirineos / Andorra (Baqueira, Grandvalira)',
                     'aeropuertos': ['BCN', 'TLS', 'ZAZ'],
                     'vuela_A': True},
                    {'zona': 'Sierra Nevada', 'aeropuertos': ['GRX', 'AGP'], 'vuela_A': True},
                    {'zona': 'Bulgaria (Bansko)', 'aeropuertos': ['SOF'], 'vuela_A': True}],
 'temporada_esqui': {'desde': '2026-12-10', 'hasta': '2027-03-31', 'fines_de_semana_por_dia': 1},
 'puente': {'hasta': '2026-12-08',
            'combinaciones': [['2026-12-04', '2026-12-08'],
                              ['2026-12-04', '2026-12-09'],
                              ['2026-12-05', '2026-12-09']],
            'aeropuertos_destino': ['MXP', 'BGY', 'GVA', 'MUC', 'VCE', 'TRN', 'LYS', 'INN', 'SZG'],
            'destinos_extra_ryanair': 5},
 'sorpresa': {'semanas_vista': 10, 'duracion_min_dias': 2, 'duracion_max_dias': 4},
 'sin_ruta': {'fallos_para_saltar': 2, 'dias_memoria': 10}}


# ---------------------------------------------------------------- utilidades
def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def hoy() -> date:
    return datetime.now(timezone.utc).date()


def fusionar(defecto: dict, usuario: dict | None) -> dict:
    """Mezcla config del usuario sobre los valores por defecto (recursivo)."""
    res = dict(defecto)
    for k, v in (usuario or {}).items():
        if isinstance(v, dict) and isinstance(res.get(k), dict):
            res[k] = fusionar(res[k], v)
        elif v is not None:
            res[k] = v
    return res


def cargar_config() -> tuple[dict, list[str]]:
    avisos: list[str] = []
    try:
        usuario = yaml.safe_load((RAIZ / "config.yaml").read_text(encoding="utf-8")) or {}
        if not isinstance(usuario, dict):
            raise ValueError("config.yaml no es un diccionario")
    except Exception as e:
        avisos.append(f"config.yaml ilegible ({type(e).__name__}: {e}); se usan valores por defecto")
        usuario = {}
    cfg = fusionar(DEFECTO, usuario)

    def lista_iata(x) -> list[str]:
        return [str(a).strip().upper() for a in (x or []) if str(a).strip()]

    for v in ("A", "B"):
        cfg["viajeros"][v]["aeropuertos"] = lista_iata(cfg["viajeros"][v].get("aeropuertos"))
        pe = cfg["viajeros"][v].get("principales_esqui")
        cfg["viajeros"][v]["principales_esqui"] = lista_iata(pe) or cfg["viajeros"][v]["aeropuertos"]
    zonas = []
    for z in cfg.get("destinos_esqui") or []:
        if isinstance(z, dict) and z.get("zona") and lista_iata(z.get("aeropuertos")):
            zonas.append({"zona": str(z["zona"]), "aeropuertos": lista_iata(z["aeropuertos"]),
                          "vuela_A": bool(z.get("vuela_A"))})
        else:
            avisos.append(f"Zona de esquí mal definida en config.yaml, ignorada: {z}")
    cfg["destinos_esqui"] = zonas
    combis = []
    for c in cfg["puente"].get("combinaciones") or []:
        try:
            ida, vta = date.fromisoformat(str(c[0])), date.fromisoformat(str(c[1]))
            if vta > ida:
                combis.append((ida.isoformat(), vta.isoformat()))
        except Exception:
            avisos.append(f"Combinación de puente mal definida, ignorada: {c}")
    cfg["puente"]["combinaciones"] = combis
    cfg["puente"]["aeropuertos_destino"] = lista_iata(cfg["puente"].get("aeropuertos_destino"))
    return cfg, avisos


# ------------------------------------------------------------------- modelos
@dataclass
class Tramo:
    origen: str
    destino: str
    fecha: str
    precio: float
    aerolineas: str
    salida: str
    llegada: str
    escalas: int
    duracion_vuelo_min: int
    fuente: str
    enlace: str


@dataclass
class IdaVuelta:
    ida: Tramo
    vuelta: Tramo

    @property
    def precio(self) -> float:
        return self.ida.precio + self.vuelta.precio

    def a_dict(self) -> dict:
        return asdict(self.ida) | {"vuelta": asdict(self.vuelta), "total": round(self.precio, 2)}


def fmt_dt(d) -> str:
    """SimpleDatetime de fast-flights -> 'YYYY-MM-DD HH:MM'."""
    y, m, dd = (list(d.date) + [0, 0, 0])[:3]
    hh, mm = (list(d.time or []) + [0, 0])[:2]
    return f"{int(y or 0):04d}-{int(m or 0):02d}-{int(dd or 0):02d} {int(hh or 0):02d}:{int(mm or 0):02d}"


# ------------------------------------------------------------ Google Flights
class SinVuelos(Exception):
    """Google respondió bien pero no hay vuelos para esa ruta/fecha."""


class Bloqueo(Exception):
    """Error de red, timeout, 429/5xx o página sin datos (captcha/consentimiento)."""


def con_timeout(fn, segundos: float):
    """Ejecuta fn() en un hilo daemon; si tarda más de `segundos`, lanza Bloqueo.
    (Un hilo colgado no impide que el programa termine.)"""
    caja: dict = {}

    def objetivo():
        try:
            caja["ok"] = fn()
        except BaseException as e:  # noqa: BLE001
            caja["err"] = e

    h = threading.Thread(target=objetivo, daemon=True)
    h.start()
    h.join(segundos)
    if h.is_alive():
        raise Bloqueo(f"sin respuesta en {segundos:.0f} s")
    if "err" in caja:
        raise caja["err"]
    return caja.get("ok")


class Google:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.presupuesto = int(cfg["max_peticiones_google"])
        self.limite_tiempo = time.monotonic() + 60 * float(cfg["tiempo_max_google_min"])
        self.timeout = float(cfg["timeout_peticion_s"])
        self.peticiones = self.exitos = self.errores = self.sin_vuelos = self.saltadas = 0
        self.errores_seguidos = 0
        self.enfriamientos = 0
        self.desactivado: str | None = ERROR_IMPORT_GOOGLE
        self.formato_sospechoso = False
        self.agotado: str | None = None
        self.cache: dict[tuple, list[Tramo]] = {}
        sr = cfg["sin_ruta"]
        self.fallos_para_saltar = max(1, int(sr["fallos_para_saltar"]))
        self.dias_memoria = max(1, int(sr["dias_memoria"]))
        self.sin_ruta = self._cargar_sin_ruta()
        self._cliente = None

    # --- memoria de rutas sin vuelos, por día de la semana -------------------
    # clave "BIO-MXP-4" (4 = viernes) -> {"dias_fallo": [fechas de consulta], "visto": fecha}
    def _cargar_sin_ruta(self) -> dict[str, dict]:
        try:
            datos = json.loads(SIN_RUTA.read_text(encoding="utf-8"))
            limite = (hoy() - timedelta(days=self.dias_memoria)).isoformat()
            return {k: v for k, v in datos.items()
                    if isinstance(v, dict) and isinstance(v.get("dias_fallo"), list)
                    and str(v.get("visto", "")) >= limite}
        except Exception:
            return {}

    @staticmethod
    def _clave_ruta(origen: str, destino: str, fecha: str) -> str:
        return f"{origen}-{destino}-{date.fromisoformat(fecha).weekday()}"

    def _ruta_descartada(self, clave: str) -> bool:
        dias = self.sin_ruta.get(clave, {}).get("dias_fallo", [])
        return len(set(dias)) >= self.fallos_para_saltar

    def _anotar_sin_vuelos(self, clave: str) -> None:
        e = self.sin_ruta.setdefault(clave, {"dias_fallo": [], "visto": ""})
        h = hoy().isoformat()
        if h not in e["dias_fallo"]:
            e["dias_fallo"] = (e["dias_fallo"] + [h])[-5:]
        e["visto"] = h

    def guardar_sin_ruta(self) -> None:
        if self.formato_sospechoso:
            return  # no guardamos "rutas sin vuelos" que podrían ser fallos de lectura
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = SIN_RUTA.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.sin_ruta, indent=0, sort_keys=True), encoding="utf-8")
        os.replace(tmp, SIN_RUTA)

    # --- descarga y lectura ---------------------------------------------------
    def _nuevo_cliente(self):
        if primp is None:
            return None
        for imp in ("chrome_145", "chrome_133", "chrome_131", None):
            try:
                kw = dict(impersonate=imp, impersonate_os="macos") if imp else {}
                return primp.Client(referer=True, cookie_store=True, timeout=self.timeout, **kw)
            except Exception:
                continue
        return None

    def _get_html(self, q) -> str:
        if self._cliente is None:
            self._cliente = self._nuevo_cliente()
        if self._cliente is not None:
            cli = self._cliente
            res = con_timeout(lambda: cli.get(URL_GOOGLE, params=q.params()), self.timeout + 10)
            codigo = getattr(res, "status_code", 200)
            if codigo == 429 or codigo >= 500:
                raise Bloqueo(f"HTTP {codigo}")
            return res.text
        # último recurso: la función de la propia librería
        from fast_flights import fetch_flights_html
        return con_timeout(lambda: fetch_flights_html(q), self.timeout + 10)

    def _descargar(self, q) -> str:
        """Descarga con reintentos solo ante errores de red/bloqueo."""
        ultimo: Exception = Bloqueo("desconocido")
        for intento in range(3):
            try:
                html = self._get_html(q)
                if html and "ds:1" in html:
                    return html
                ultimo = Bloqueo("página sin datos de vuelos (¿captcha o consentimiento?)")
            except Bloqueo as e:
                ultimo = e
            except Exception as e:
                ultimo = Bloqueo(f"{type(e).__name__}: {e}")
            self._cliente = None  # nueva sesión en el siguiente intento
            if intento < 2:
                time.sleep(5 * (intento + 1))
        raise ultimo

    @staticmethod
    def _parsear(html: str) -> list:
        """Un fallo al interpretar una página válida = no hay vuelos para esa ruta/fecha."""
        try:
            return list(parse_html(html) or [])
        except Exception as e:
            raise SinVuelos(type(e).__name__) from e

    def _extraer(self, vuelos: list, origen: str, destino: str, fecha: str, enlace: str) -> list[Tramo]:
        tramos = []
        for f in vuelos:
            try:
                if not f.flights or not f.price or float(f.price) <= 0:
                    continue
                tramos.append(Tramo(
                    origen=origen, destino=destino, fecha=fecha,
                    precio=float(f.price),
                    aerolineas=", ".join(str(a) for a in (f.airlines or [])) or "¿?",
                    salida=fmt_dt(f.flights[0].departure),
                    llegada=fmt_dt(f.flights[-1].arrival),
                    escalas=len(f.flights) - 1,
                    duracion_vuelo_min=int(sum((s.duration or 0) for s in f.flights)),
                    fuente="Google Flights",
                    enlace=enlace,
                ))
            except Exception:
                continue  # un resultado raro no tumba la consulta
        tramos.sort(key=lambda t: t.precio)
        return tramos[:3]

    def _registrar_error(self, e: Exception, origen: str, destino: str, fecha: str) -> None:
        self.errores += 1
        self.errores_seguidos += 1
        log(f"  ! Google {origen}->{destino} {fecha}: {e}"[:220])
        if self.errores_seguidos >= 8:
            if self.enfriamientos < 2:
                self.enfriamientos += 1
                log(f"  .. {self.errores_seguidos} errores seguidos: pausa de 3 min y reintento")
                time.sleep(180)
                self.errores_seguidos = 0
                self._cliente = None
            else:
                self.desactivado = f"errores de red/bloqueo persistentes (último: {e})"[:300]
                log("  !! Google desactivado: " + self.desactivado)

    def tramo(self, origen: str, destino: str, fecha: str) -> list[Tramo]:
        """Hasta 3 opciones (las más baratas) de un vuelo de ida, ordenadas por precio."""
        clave = (origen, destino, fecha)
        if clave in self.cache:
            return self.cache[clave]
        if self.desactivado or origen == destino:
            return []
        clave_ruta = self._clave_ruta(origen, destino, fecha)
        if self._ruta_descartada(clave_ruta):
            self.saltadas += 1
            self.cache[clave] = []
            return []
        if self.peticiones >= self.presupuesto:
            self.agotado = self.agotado or f"tope de {self.presupuesto} consultas"
            return []
        if time.monotonic() > self.limite_tiempo:
            self.agotado = self.agotado or f"límite de {self.cfg['tiempo_max_google_min']} min"
            return []

        self.peticiones += 1
        resultado: list[Tramo] = []
        try:
            q = create_query(
                flights=[FlightQuery(date=fecha, from_airport=origen, to_airport=destino)],
                trip="one-way",
                passengers=Passengers(adults=1),
                language=self.cfg["idioma"],
                currency=self.cfg["moneda"],
                max_stops=int(self.cfg["max_escalas"]),
                hide_separate_and_self_transfer=True,
            )
            html = self._descargar(q)
            self.errores_seguidos = 0
            resultado = self._extraer(self._parsear(html), origen, destino, fecha, q.url())
            if not resultado:
                raise SinVuelos("0 resultados")
            self.exitos += 1
            self.sin_ruta.pop(clave_ruta, None)
        except SinVuelos:
            self.sin_vuelos += 1
            self._anotar_sin_vuelos(clave_ruta)
            # Muchas consultas "válidas" y ninguna con vuelos => Google cambió el formato
            if self.peticiones >= 60 and self.exitos == 0:
                self.formato_sospechoso = True
                self.desactivado = ("ninguna de las primeras 60 consultas devolvió vuelos legibles: "
                                    "probable cambio de formato en Google Flights "
                                    "(prueba a actualizar fast-flights)")
                log("  !! " + self.desactivado)
        except Exception as e:
            self._registrar_error(e, origen, destino, fecha)
            resultado = []
            self.cache[clave] = resultado  # no reintentar esta misma consulta hoy
            time.sleep(float(self.cfg["pausa_entre_peticiones_s"]))
            return resultado
        self.cache[clave] = resultado
        time.sleep(float(self.cfg["pausa_entre_peticiones_s"]))
        return resultado

    def ida_vuelta(self, origen: str, destinos: list[str], f_ida: str, f_vuelta: str) -> IdaVuelta | None:
        """Mejor ida+vuelta desde `origen` a cualquiera de `destinos` (se puede volar a un
        aeropuerto de la zona y volver desde otro). Solo consulta la vuelta si hay ida."""
        idas = [t for d in destinos for t in self.tramo(origen, d, f_ida)[:1]]
        if not idas:
            return None
        vueltas = [t for d in destinos for t in self.tramo(d, origen, f_vuelta)[:1]]
        if not vueltas:
            return None
        return IdaVuelta(min(idas, key=lambda t: t.precio), min(vueltas, key=lambda t: t.precio))

    def mejor_ida_vuelta(self, origenes: list[str], destinos: list[str], f_ida: str, f_vuelta: str):
        opciones = [iv for o in origenes if (iv := self.ida_vuelta(o, destinos, f_ida, f_vuelta))]
        return min(opciones, key=lambda iv: iv.precio, default=None)


# ------------------------------------------------------------------ Ryanair
def ryanair_sorpresa(cfg: dict, origenes: list[str]) -> tuple[list[dict], list[str]]:
    s = cfg["sorpresa"]
    ofertas: list[dict] = []
    fallos: list[str] = []
    inicio = hoy() + timedelta(days=3)
    fin = hoy() + timedelta(weeks=int(s["semanas_vista"]))
    for o in origenes:
        params = {
            "departureAirportIataCode": o,
            "outboundDepartureDateFrom": inicio.isoformat(),
            "outboundDepartureDateTo": fin.isoformat(),
            "inboundDepartureDateFrom": inicio.isoformat(),
            "inboundDepartureDateTo": (fin + timedelta(days=int(s["duracion_max_dias"]))).isoformat(),
            "durationFrom": int(s["duracion_min_dias"]),
            "durationTo": int(s["duracion_max_dias"]),
            "currency": cfg["moneda"],
            "market": "es-es",
            "language": "es",
        }
        datos = None
        for intento in range(3):
            try:
                r = requests.get(URL_RYANAIR, params=params, timeout=30,
                                 headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
                if r.status_code == 200:
                    datos = r.json()
                    break
                if r.status_code in (400, 404):  # aeropuerto sin Ryanair: no reintentar
                    datos = {"fares": []}
                    break
                err = f"HTTP {r.status_code}"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            time.sleep(3 * (intento + 1))
        if datos is None:
            fallos.append(f"{o}: {err}"[:150])
            log(f"  ! Ryanair {o}: {err}"[:200])
            continue
        for fare in (datos.get("fares") or []) if isinstance(datos, dict) else []:
            try:
                ida, vuelta = fare.get("outbound") or {}, fare.get("inbound") or {}
                total = ((fare.get("summary") or {}).get("price") or {}).get("value")
                dest = ida.get("arrivalAirport") or {}
                iata = dest.get("iataCode")
                if total is None or not iata:
                    continue
                f_ida = str(ida.get("departureDate") or "")
                f_vta = str(vuelta.get("departureDate") or "")
                ofertas.append({
                    "origen": o,
                    "destino": iata,
                    "ciudad": (dest.get("city") or {}).get("name") or dest.get("name") or iata,
                    "pais": dest.get("countryName") or "",
                    "salida_ida": f_ida.replace("T", " ")[:16],
                    "llegada_ida": str(ida.get("arrivalDate") or "").replace("T", " ")[:16],
                    "salida_vuelta": f_vta.replace("T", " ")[:16],
                    "llegada_vuelta": str(vuelta.get("arrivalDate") or "").replace("T", " ")[:16],
                    "precio_ida_vuelta": round(float(total), 2),
                    "enlace": ("https://www.ryanair.com/es/es/trip/flights/select?adults=1"
                               f"&dateOut={f_ida[:10]}&dateIn={f_vta[:10]}&originIata={o}"
                               f"&destinationIata={iata}&isReturn=true"),
                })
            except Exception:
                continue
    ofertas.sort(key=lambda x: x["precio_ida_vuelta"])
    return ofertas, fallos


# ---------------------------------------------------------------- campañas
def campana_puente(cfg: dict, g: Google, sorpresa: list[dict]) -> list[dict]:
    p = cfg["puente"]
    try:
        if hoy() > date.fromisoformat(str(p["hasta"])):
            return []
    except Exception:
        pass
    if not p["combinaciones"]:
        return []
    destinos = list(p["aeropuertos_destino"])
    # Añade los destinos más baratos que Ryanair haya encontrado hoy (lo pidió el usuario:
    # "si encuentras alguna oferta, mira también el puente a ese destino")
    extra = int(p.get("destinos_extra_ryanair") or 0)
    for o in sorpresa:
        if extra <= 0:
            break
        if o["destino"] not in destinos:
            destinos.append(o["destino"])
            extra -= 1
    origenes = cfg["viajeros"]["B"]["aeropuertos"]
    resultados = []
    for destino in destinos:
        fila = {"destino": destino, "combinaciones": {}}
        for ida, vuelta in p["combinaciones"]:
            log(f"Puente {destino} {ida}->{vuelta}")
            try:
                mejor = g.mejor_ida_vuelta(origenes, [destino], ida, vuelta)
            except Exception as e:
                log(f"  ! puente {destino}: {e}")
                mejor = None
            fila["combinaciones"][f"{ida}_{vuelta}"] = mejor.a_dict() if mejor else None
        resultados.append(fila)
    return resultados


def fines_de_semana(cfg: dict) -> list[tuple[str, str]]:
    """Viernes y lunes de cada fin de semana largo dentro de la temporada."""
    t = cfg["temporada_esqui"]
    try:
        desde, hasta = date.fromisoformat(str(t["desde"])), date.fromisoformat(str(t["hasta"]))
    except Exception:
        return []
    d = max(desde, hoy() + timedelta(days=3))
    while d.weekday() != 4:
        d += timedelta(days=1)
    lista = []
    while d <= hasta:
        lista.append((d.isoformat(), (d + timedelta(days=3)).isoformat()))
        d += timedelta(days=7)
    return lista


def campana_esqui(cfg: dict, g: Google) -> list[dict]:
    findes = fines_de_semana(cfg)
    if not findes or not cfg["destinos_esqui"]:
        return []
    n = max(1, int(cfg["temporada_esqui"].get("fines_de_semana_por_dia") or 1))
    inicio = (hoy().toordinal() * n) % len(findes)
    hoy_findes = [findes[(inicio + i) % len(findes)] for i in range(min(n, len(findes)))]
    origenes_A = cfg["viajeros"]["A"]["principales_esqui"]
    origenes_B = cfg["viajeros"]["B"]["principales_esqui"]
    resultados = []
    for viernes, lunes in hoy_findes:
        jueves = (date.fromisoformat(viernes) - timedelta(days=1)).isoformat()
        domingo = (date.fromisoformat(lunes) - timedelta(days=1)).isoformat()
        for ida, vuelta in [(jueves, domingo), (viernes, lunes)]:
            for z in cfg["destinos_esqui"]:
                log(f"Esquí {z['zona']} {ida}->{vuelta}")
                try:
                    b = g.mejor_ida_vuelta(origenes_B, z["aeropuertos"], ida, vuelta)
                    a = g.mejor_ida_vuelta(origenes_A, z["aeropuertos"], ida, vuelta) if z["vuela_A"] else None
                except Exception as e:
                    log(f"  ! esquí {z['zona']}: {e}")
                    a = b = None
                resultados.append({
                    "zona": z["zona"], "ida": ida, "vuelta": vuelta,
                    "B": b.a_dict() if b else None,
                    "A": a.a_dict() if a else None,
                    "A_por_tierra": not z["vuela_A"],
                })
    return resultados


# --------------------------------------------------------- histórico/bajadas
def leer_historico() -> list[dict]:
    try:
        with HISTORICO.open(newline="", encoding="utf-8") as f:
            return [r for r in csv.DictReader(f) if r.get("fecha_consulta")]
    except FileNotFoundError:
        return []
    except Exception as e:
        log(f"  ! histórico ilegible ({e}); se empieza de cero")
        return []


def guardar_historico(previo: list[dict], nuevas: list[dict], dias: int) -> None:
    """Reescribe el histórico conservando solo los últimos `dias` días (evita que crezca sin fin)."""
    limite = (hoy() - timedelta(days=int(dias))).isoformat()
    filas = [r for r in previo if r.get("fecha_consulta", "") >= limite] + nuevas
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = HISTORICO.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS_HIST, extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)
    os.replace(tmp, HISTORICO)


def detectar_bajadas(previo: list[dict], cache: dict, pct: float) -> list[dict]:
    """Mejor precio de hoy por (origen, destino, fecha) frente al mínimo de días anteriores."""
    minimos: dict[tuple, float] = {}
    hoy_iso = hoy().isoformat()
    for r in previo:
        if r.get("campana") != "google" or r.get("fecha_consulta", "") >= hoy_iso:
            continue
        try:
            k = (r["origen"], r["destino"], r["fecha"])
            p = float(r["precio"])
        except (KeyError, ValueError, TypeError):
            continue
        minimos[k] = min(p, minimos.get(k, p))
    bajadas = []
    for (o, d, f), tramos in cache.items():
        if not tramos or (o, d, f) not in minimos or f < hoy_iso:
            continue
        hoy_p, antes = tramos[0].precio, minimos[(o, d, f)]
        if antes > 0 and hoy_p < antes * (1 - pct / 100):
            bajadas.append({"origen": o, "destino": d, "fecha": f, "precio_hoy": hoy_p,
                            "minimo_anterior": antes,
                            "bajada_pct": round(100 * (antes - hoy_p) / antes, 1),
                            "salida": tramos[0].salida, "aerolineas": tramos[0].aerolineas,
                            "enlace": tramos[0].enlace})
    return sorted(bajadas, key=lambda b: -b["bajada_pct"])


# ------------------------------------------------------------------ informe
def eur(x) -> str:
    try:
        return f"{float(x):,.0f} €".replace(",", ".")
    except Exception:
        return "—"


def hora(s: str) -> str:
    return s[11:16] if s and len(s) >= 16 else "?"


def dia_hora(s: str) -> str:
    return f"{s[8:10]}/{s[5:7]} {s[11:16]}" if s and len(s) >= 16 else "?"


def linea_iv(d: dict | None) -> str:
    if not d:
        return "sin vuelos encontrados"
    v = d["vuelta"]
    return (f"ida {d['origen']}→{d['destino']} {dia_hora(d['salida'])}–{hora(d['llegada'])} "
            f"({d['aerolineas']}, {d['escalas']} esc., {eur(d['precio'])}) · "
            f"vuelta {v['origen']}→{v['destino']} {dia_hora(v['salida'])}–{hora(v['llegada'])} "
            f"({v['aerolineas']}, {v['escalas']} esc., {eur(v['precio'])}) = **{eur(d['total'])}** "
            f"[ida]({d['enlace']}) [vuelta]({v['enlace']})")


def nombre_combi(clave: str) -> str:
    ida, vta = clave.split("_")
    return f"{ida[8:10]}/{ida[5:7]}–{vta[8:10]}/{vta[5:7]}"


def informe_md(res: dict) -> str:
    out = [f"# Vigilancia de vuelos — {res['fecha_consulta']}", ""]
    out.append(f"Google Flights: {res.get('peticiones_google', 0)} consultas "
               f"({res.get('exitos_google', 0)} con vuelos, {res.get('sin_vuelos_google', 0)} sin vuelos, "
               f"{res.get('errores_google', 0)} errores, {res.get('rutas_saltadas', 0)} rutas saltadas por no "
               f"tener vuelos). Ryanair: {len(res.get('sorpresa', []))} tarifas.")
    out.append("Precios **por persona** en €, tarifa básica sin maleta de cabina grande. "
               "Ida y vuelta de Google = suma de dos billetes de ida. Horas locales.")
    out.append("")
    if res.get("avisos"):
        out.append("## ⚠️ Avisos de la ejecución")
        out += [f"- {a}" for a in res["avisos"]]
        out.append("")

    out.append("## Bajadas de precio (≥ "
               f"{res.get('bajada_pct', 20)} % frente al mínimo de días anteriores)")
    if res.get("bajadas"):
        for b in res["bajadas"][:15]:
            out.append(f"- {b['origen']}→{b['destino']} {dia_hora(b['salida'])} ({b['aerolineas']}): "
                       f"{eur(b['precio_hoy'])} (antes mín. {eur(b['minimo_anterior'])}, "
                       f"-{b['bajada_pct']} %) [ver]({b['enlace']})")
    else:
        out.append("- Ninguna hoy (o aún no hay histórico suficiente).")
    out.append("")

    if res.get("puente"):
        claves = list(res["puente"][0]["combinaciones"].keys())
        out.append("## Puente de diciembre — viajero B desde aeropuertos de Bilbao (solo vuelo, por persona)")
        out.append("| Destino | " + " | ".join(nombre_combi(c) for c in claves) + " | Mejor |")
        out.append("|---|" + "---|" * (len(claves) + 1))
        detalle = []
        for fila in res["puente"]:
            c = fila["combinaciones"]
            celdas = []
            for k in claves:
                d = c.get(k)
                celdas.append(f"{eur(d['total'])} ({d['origen']} {hora(d['salida'])}/{hora(d['vuelta']['salida'])})"
                              if d else "—")
            validas = {k: v for k, v in c.items() if v}
            mejor = nombre_combi(min(validas, key=lambda k: validas[k]["total"])) if validas else "—"
            out.append(f"| {fila['destino']} | " + " | ".join(celdas) + f" | {mejor} |")
            for k, d in validas.items():
                detalle.append(f"- {fila['destino']} {nombre_combi(k)}: {linea_iv(d)}")
        out.append("")
        # Diferencias de salir un día antes / volver un día después
        comp = res.get("puente_comparativas") or []
        if comp:
            out.append("**Efecto de cambiar un día (solo vuelo; la noche extra de alojamiento la suma Claude):**")
            out += [f"- {x}" for x in comp]
            out.append("")
        out.append("<details><summary>Detalle de vuelos del puente</summary>\n")
        out += detalle
        out.append("\n</details>\n")

    out.append("## Esquí — fines de semana largos revisados hoy (ordenado por precio de B)")
    esq = sorted(res.get("esqui") or [], key=lambda r: (r["B"] or {}).get("total", 1e9))
    if esq:
        for r in esq[:16]:
            a = "va por tierra (tren/coche desde Stuttgart: lo calcula Claude)" if r["A_por_tierra"] else linea_iv(r["A"])
            out.append(f"- **{r['zona']}** · {r['ida']} → {r['vuelta']}\n"
                       f"  - B (Bilbao): {linea_iv(r['B'])}\n  - A (Stuttgart): {a}")
    else:
        out.append("- Sin datos hoy.")
    out.append("")

    umbral = res.get("umbral", 150)
    out.append(f"## Ofertas sorpresa Ryanair desde Bilbao y alrededores (ida y vuelta < {umbral} €)")
    baratas = [o for o in res.get("sorpresa") or [] if o["precio_ida_vuelta"] < umbral]
    if baratas:
        vistos: set = set()
        for o in baratas:
            k = (o["origen"], o["destino"])
            if k in vistos:
                continue
            vistos.add(k)
            out.append(f"- {o['origen']}→{o['destino']} ({o['ciudad']}, {o['pais']}): "
                       f"**{eur(o['precio_ida_vuelta'])}** · ida {dia_hora(o['salida_ida'])}–{hora(o['llegada_ida'])}"
                       f" · vuelta {dia_hora(o['salida_vuelta'])}–{hora(o['llegada_vuelta'])} [ver]({o['enlace']})")
            if len(vistos) >= 20:
                break
    else:
        out.append("- Sin ofertas bajo el umbral hoy (o Ryanair no respondió; ver avisos).")
    return "\n".join(out) + "\n"


def comparativas_puente(puente: list[dict]) -> list[str]:
    """Salir el 4 vs el 5 (vuelta el 9) y volver el 9 vs el 8 (salida el 4), por destino."""
    lineas = []
    for fila in puente:
        c = fila["combinaciones"]
        claves = {k: v for k, v in c.items() if v}

        def total(ida_dia: str, vta_dia: str):
            for k, v in claves.items():
                if k[8:10] == ida_dia and k[-2:] == vta_dia:
                    return v["total"]
            return None

        partes = []
        t48, t49, t59 = total("04", "08"), total("04", "09"), total("05", "09")
        if t49 is not None and t59 is not None:
            dif = t59 - t49
            partes.append(f"salir el 4 en vez del 5: {'ahorras' if dif > 0 else 'pagas'} "
                          f"{eur(abs(dif))} ({abs(dif) / t59 * 100:.0f} %)")
        if t48 is not None and t49 is not None:
            dif = t48 - t49
            partes.append(f"volver el 9 en vez del 8: {'ahorras' if dif > 0 else 'pagas'} "
                          f"{eur(abs(dif))} ({abs(dif) / t48 * 100:.0f} %)")
        if partes:
            lineas.append(f"{fila['destino']}: " + "; ".join(partes))
    return lineas


# --------------------------------------------------------------------- main
def escribir_salidas(res: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        js = json.dumps(res, ensure_ascii=False, indent=1, default=str)
    except Exception as e:
        js = json.dumps({"error": f"no serializable: {e}"})
    (DATA / "ultimo.json").write_text(js, encoding="utf-8")
    try:
        md = informe_md(res)
    except Exception as e:
        traceback.print_exc()
        md = (f"# Vigilancia de vuelos — {res.get('fecha_consulta')}\n\n"
              f"## ⚠️ Avisos de la ejecución\n- Error al generar el informe: {type(e).__name__}: {e}\n"
              "- Los datos en bruto están en data/ultimo.json\n")
    (DATA / "ultimo.md").write_text(md, encoding="utf-8")


def ejecutar() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    cfg, avisos = cargar_config()
    previo = leer_historico()
    g = Google(cfg)
    fecha_consulta = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    res: dict = {"fecha_consulta": fecha_consulta, "umbral": cfg["umbral_aviso_ida_vuelta_eur"],
                 "bajada_pct": cfg["bajada_relevante_pct"], "avisos": avisos,
                 "puente": [], "puente_comparativas": [], "esqui": [], "sorpresa": [], "bajadas": []}

    # 1) Ryanair primero (rápido); sus destinos baratos alimentan el puente
    try:
        res["sorpresa"], fallos = ryanair_sorpresa(cfg, cfg["viajeros"]["B"]["aeropuertos"])
        if fallos:
            res["avisos"].append("Ryanair falló para: " + "; ".join(fallos))
        elif not res["sorpresa"]:
            res["avisos"].append("Ryanair respondió pero sin tarifas legibles (¿cambio en su API?).")
    except Exception as e:
        traceback.print_exc()
        res["avisos"].append(f"Fallo en ofertas Ryanair: {type(e).__name__}: {e}")

    # 2) Puente y 3) esquí con Google Flights
    try:
        res["puente"] = campana_puente(cfg, g, res["sorpresa"])
        res["puente_comparativas"] = comparativas_puente(res["puente"])
    except Exception as e:
        traceback.print_exc()
        res["avisos"].append(f"Fallo en campaña puente: {type(e).__name__}: {e}")
    try:
        res["esqui"] = campana_esqui(cfg, g)
    except Exception as e:
        traceback.print_exc()
        res["avisos"].append(f"Fallo en campaña esquí: {type(e).__name__}: {e}")

    if g.desactivado:
        res["avisos"].append(f"Google Flights desactivado: {g.desactivado}")
    if g.agotado:
        res["avisos"].append(f"Búsqueda en Google cortada por {g.agotado}; lo pendiente se revisa otro día.")
    res.update(peticiones_google=g.peticiones, exitos_google=g.exitos, sin_vuelos_google=g.sin_vuelos,
               errores_google=g.errores, rutas_saltadas=g.saltadas)

    try:
        g.guardar_sin_ruta()
    except Exception as e:
        res["avisos"].append(f"No se pudo guardar sin_ruta.json: {e}")
    try:
        res["bajadas"] = detectar_bajadas(previo, g.cache, float(cfg["bajada_relevante_pct"]))
    except Exception as e:
        res["avisos"].append(f"No se pudieron calcular bajadas: {e}")

    dia = hoy().isoformat()
    nuevas = [{"fecha_consulta": dia, "campana": "google", "viajero": "", "tramo": "ida", **asdict(t)}
              for tramos in g.cache.values() for t in tramos[:1]]
    nuevas += [{"fecha_consulta": dia, "campana": "sorpresa", "viajero": "B", "origen": o["origen"],
                "destino": o["destino"], "fecha": o["salida_ida"][:10], "tramo": "ida-vuelta",
                "precio": o["precio_ida_vuelta"], "aerolineas": "Ryanair", "salida": o["salida_ida"],
                "llegada": o["llegada_ida"], "escalas": 0, "duracion_vuelo_min": "",
                "fuente": "Ryanair", "enlace": o["enlace"]} for o in res["sorpresa"]]
    try:
        guardar_historico(previo, nuevas, int(cfg["historico_dias"]))
    except Exception as e:
        res["avisos"].append(f"No se pudo guardar el histórico: {e}")

    escribir_salidas(res)
    log(f"Hecho: Google {g.peticiones} consultas ({g.exitos} con vuelos, {g.sin_vuelos} sin vuelos, "
        f"{g.errores} errores, {g.saltadas} saltadas); Ryanair {len(res['sorpresa'])} tarifas; "
        f"{len(res['bajadas'])} bajadas.")
    if g.exitos == 0 and not res["sorpresa"]:
        log("Ninguna fuente devolvió datos hoy.")
        return 1
    return 0


def main() -> int:
    try:
        return ejecutar()
    except BaseException as e:  # último recurso: dejar siempre un informe
        traceback.print_exc()
        try:
            escribir_salidas({"fecha_consulta": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                              "avisos": [f"Error general: {type(e).__name__}: {e}"],
                              "puente": [], "esqui": [], "sorpresa": [], "bajadas": []})
        except Exception:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
