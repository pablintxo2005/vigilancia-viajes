"""Vigilancia diaria de viajes: puente de diciembre, esquí (3-6 días) y ofertas sorpresa.

Fuentes:
  - Google Flights (protocolo interno, vía la librería fast-flights; sin API key)
  - Tarifas públicas de Ryanair (ofertas sorpresa desde Bilbao y alrededores)
  - Airbnb (pisos, apartamentos y hoteles; precio real para 2 personas y fechas exactas)

Salidas (carpeta data/):
  - ultimo.md     : resumen legible de hoy (lo lee la tarea diaria de Claude)
  - ultimo.json   : lo mismo, estructurado
  - historico.csv : precios de vuelos día a día (últimos N días) para detectar bajadas
  - sin_ruta.json : rutas/días de la semana sin vuelos, para no repetir consultas inútiles

Los viajeros NO tienen coche: el transporte por tierra siempre es tren/bus/lanzadera.

Diseño "a prueba de fallos":
  - Ningún fallo de una consulta, fuente o campaña detiene al resto.
  - Siempre se escribe data/ultimo.md (aunque sea solo con avisos).
  - "No hay vuelos" (respuesta válida de Google) se distingue de "error de red/bloqueo";
    solo lo segundo se reintenta.
  - Límites de tiempo y de consultas propios, por debajo del límite de GitHub.
  - Si Google cambia el formato (0 resultados válidos en muchas consultas), se detecta,
    se avisa y NO se contamina la memoria de rutas sin vuelos.
"""

from __future__ import annotations

import csv
import json
import math
import os
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

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

try:
    import pyairbnb
    ERROR_IMPORT_AIRBNB = None
except Exception as _e:  # pragma: no cover
    pyairbnb = None
    ERROR_IMPORT_AIRBNB = f"no se pudo importar pyairbnb ({type(_e).__name__}: {_e})"

try:
    import airportsdata
    AEROPUERTOS = airportsdata.load("IATA")
except Exception:  # pragma: no cover
    AEROPUERTOS = {}

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
DIAS_SEMANA = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]

# Valores por defecto (copia de config.yaml): si config.yaml falta, está mal escrito
# o le falta alguna clave, se usa esto y el programa sigue funcionando completo.
DEFECTO = {'moneda': 'EUR',
 'idioma': 'es',
 'pausa_entre_peticiones_s': 1.0,
 'max_peticiones_google': 1400,
 'tiempo_max_google_min': 85,
 'timeout_peticion_s': 30,
 'max_escalas': 1,
 'historico_dias': 60,
 'umbral_aviso_ida_vuelta_eur': 150,
 'bajada_relevante_pct': 20,
 'viajeros': {'A': {'nombre': 'Stuttgart',
                    'aeropuertos': ['STR', 'FKB', 'FMM', 'FDH', 'FRA', 'MUC', 'ZRH', 'BSL'],
                    'principales_esqui': ['STR', 'FMM', 'MUC', 'ZRH']},
              'B': {'nombre': 'Bilbao',
                    'aeropuertos': ['BIO', 'EAS', 'VIT', 'SDR', 'BIQ', 'PNA'],
                    'principales_esqui': ['BIO', 'SDR', 'BIQ']}},
 'destinos_esqui': [{'zona': 'Tirol (Innsbruck: Stubai, Axamer Lizum, Nordkette)',
                     'aeropuertos': ['INN', 'MUC', 'SZG'],
                     'vuela_A': False,
                     'base': {'nombre': 'Innsbruck', 'lat': 47.2654, 'lon': 11.3928},
                     'transporte': 'Tren directo MUC/SZG→Innsbruck; ski-bus gratis con el forfait '
                                   'a las estaciones'},
                    {'zona': 'Salzburgerland (Schladming-Dachstein, Ski Amadé)',
                     'aeropuertos': ['SZG', 'MUC'],
                     'vuela_A': False,
                     'base': {'nombre': 'Schladming', 'lat': 47.3925, 'lon': 13.6872},
                     'transporte': 'Tren SZG→Schladming (~1h30); estación a pie del telecabina '
                                   'Planai'},
                    {'zona': 'Graubünden (Davos-Klosters, Laax)',
                     'aeropuertos': ['ZRH'],
                     'vuela_A': False,
                     'base': {'nombre': 'Davos', 'lat': 46.8027, 'lon': 9.836},
                     'transporte': 'Tren ZRH→Davos (~2h20, SBB/RhB); A llega en tren '
                                   'Stuttgart→Zúrich'},
                    {'zona': 'Valais / Mont Blanc (Chamonix, Verbier)',
                     'aeropuertos': ['GVA', 'LYS'],
                     'vuela_A': False,
                     'base': {'nombre': 'Chamonix', 'lat': 45.9237, 'lon': 6.8694},
                     'transporte': 'Lanzadera/bus GVA→Chamonix (~1h15); tren Mont-Blanc Express'},
                    {'zona': 'Dolomitas (Val Gardena, Alta Badia)',
                     'aeropuertos': ['VRN', 'BGY', 'VCE', 'TSF', 'INN'],
                     'vuela_A': False,
                     'base': {'nombre': 'Ortisei (Val Gardena)', 'lat': 46.5747, 'lon': 11.6714},
                     'transporte': 'Tren a Bolzano/Ponte Gardena + bus 350 a Val Gardena'},
                    {'zona': 'Valle de Aosta (Pila, Cervinia, Courmayeur)',
                     'aeropuertos': ['TRN', 'MXP', 'GVA'],
                     'vuela_A': False,
                     'base': {'nombre': 'Aosta', 'lat': 45.737, 'lon': 7.315},
                     'transporte': 'Tren/bus a Aosta; telecabina Aosta→Pila desde la ciudad'},
                    {'zona': 'Andorra (Grandvalira, Pal-Arinsal)',
                     'aeropuertos': ['BCN', 'TLS'],
                     'vuela_A': True,
                     'base': {'nombre': 'Andorra la Vella', 'lat': 42.5063, 'lon': 1.5218},
                     'transporte': 'Bus directo BCN/TLS→Andorra (~3h, Andbus/Alsa); ski-bus a '
                                   'pistas'},
                    {'zona': 'Sierra Nevada',
                     'aeropuertos': ['GRX', 'AGP'],
                     'vuela_A': True,
                     'base': {'nombre': 'Granada', 'lat': 37.1773, 'lon': -3.5986},
                     'transporte': 'Bus aeropuerto→Granada y bus diario Granada→Sierra Nevada '
                                   '(Alsa, ~45 min)'},
                    {'zona': 'Bansko (Bulgaria)',
                     'aeropuertos': ['SOF'],
                     'vuela_A': True,
                     'base': {'nombre': 'Bansko', 'lat': 41.8383, 'lon': 23.4885},
                     'transporte': 'Transfer/bus Sofía→Bansko (~2h30); telecabina desde el '
                                   'pueblo'}],
 'temporada_esqui': {'desde': '2026-12-10',
                     'hasta': '2027-03-31',
                     'dias_min': 3,
                     'dias_max': 6,
                     'ventana_dias_salida': 6},
 'puente': {'hasta': '2026-12-08',
            'combinaciones': [['2026-12-04', '2026-12-08'],
                              ['2026-12-04', '2026-12-09'],
                              ['2026-12-05', '2026-12-09']],
            'aeropuertos_destino': ['MXP', 'BGY', 'GVA', 'MUC', 'VCE', 'TRN', 'LYS', 'INN', 'SZG'],
            'destinos_extra_ryanair': 5},
 'sorpresa': {'semanas_vista': 10,
              'duracion_min_dias': 2,
              'duracion_max_dias': 4,
              'max_ofertas_informe': 8},
 'alojamiento': {'activo': True,
                 'huespedes': 2,
                 'radio_km': 6,
                 'valoracion_min': 4.5,
                 'resenas_min': 5,
                 'opciones_por_destino': 3,
                 'max_busquedas': 50,
                 'tiempo_max_min': 15},
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


def coords_aeropuerto(iata: str) -> tuple[float, float, str, str] | None:
    """(lat, lon, ciudad, código país) de un aeropuerto, o None si no se conoce."""
    a = AEROPUERTOS.get(iata)
    if not a:
        return None
    try:
        return float(a["lat"]), float(a["lon"]), a.get("city") or iata, a.get("country") or ""
    except Exception:
        return None


def bandera(pais: str) -> str:
    """'IT' -> 🇮🇹"""
    if len(pais or "") != 2 or not pais.isalpha():
        return "✈️"
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in pais.upper())


def cargar_config() -> tuple[dict, list[str]]:
    avisos: list[str] = []
    try:
        usuario = yaml.safe_load((RAIZ / "config.yaml").read_text(encoding="utf-8")) or {}
        if not isinstance(usuario, dict):
            raise ValueError("config.yaml no es un diccionario")
    except Exception as e:
        avisos.append(f"config.yaml ilegible ({type(e).__name__}: {e}); se usa la configuración original")
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
        try:
            aer = lista_iata(z.get("aeropuertos"))
            if not z.get("zona") or not aer:
                raise ValueError("falta zona o aeropuertos")
            base = z.get("base") or {}
            if not isinstance(base, dict) or "lat" not in base or "lon" not in base:
                c = coords_aeropuerto(aer[0])
                base = {"nombre": c[2] if c else aer[0], "lat": c[0] if c else None, "lon": c[1] if c else None}
            zonas.append({"zona": str(z["zona"]), "aeropuertos": aer, "vuela_A": bool(z.get("vuela_A")),
                          "base": {"nombre": str(base.get("nombre") or aer[0]),
                                   "lat": base.get("lat"), "lon": base.get("lon")},
                          "transporte": str(z.get("transporte") or "")})
        except Exception as e:
            avisos.append(f"Zona de esquí mal definida en config.yaml, ignorada ({e}): {z}")
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
    t = cfg["temporada_esqui"]
    try:
        t["dias_min"] = max(2, int(t.get("dias_min", 3)))
        t["dias_max"] = max(t["dias_min"], int(t.get("dias_max", 6)))
        t["ventana_dias_salida"] = max(1, int(t.get("ventana_dias_salida", 6)))
    except Exception:
        avisos.append("temporada_esqui mal definida; se usan 3-6 días y ventana de 6")
        t.update(dias_min=3, dias_max=6, ventana_dias_salida=6)
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


def noches(ida: str, vuelta: str) -> int:
    return (date.fromisoformat(vuelta[:10]) - date.fromisoformat(ida[:10])).days


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


# --------------------------------------------------------------- Alojamiento
class Alojamiento:
    """Precios reales de Airbnb (pisos, apartamentos y hoteles) para N huéspedes y fechas exactas."""

    def __init__(self, cfg: dict):
        a = cfg["alojamiento"]
        self.activo = bool(a.get("activo", True))
        self.huespedes = int(a.get("huespedes", 2))
        self.radio_km = float(a.get("radio_km", 6))
        self.val_min = float(a.get("valoracion_min", 4.5))
        self.res_min = int(a.get("resenas_min", 5))
        self.n_opciones = int(a.get("opciones_por_destino", 3))
        self.max_busquedas = int(a.get("max_busquedas", 50))
        self.limite_tiempo = None
        self.tiempo_max_s = 60 * float(a.get("tiempo_max_min", 15))
        self.moneda = cfg["moneda"]
        self.busquedas = self.errores = self.errores_seguidos = 0
        self.desactivado: str | None = ERROR_IMPORT_AIRBNB if self.activo else "desactivado en config.yaml"
        self.cache: dict[tuple, dict] = {}

    @staticmethod
    def enlace_booking(nombre: str, ci: str, co: str, huespedes: int) -> str:
        return (f"https://www.booking.com/searchresults.es.html?ss={quote_plus(nombre)}"
                f"&checkin={ci}&checkout={co}&group_adults={huespedes}&no_rooms=1&order=price")

    def buscar(self, nombre: str, lat, lon, check_in: str, check_out: str) -> dict:
        """Devuelve {"opciones": [...], "booking": url, "airbnb_busqueda": url, "nota": str}."""
        ci, co = check_in[:10], check_out[:10]
        n = max(1, noches(ci, co))
        base = {"lugar": nombre, "check_in": ci, "check_out": co, "noches": n, "opciones": [],
                "booking": self.enlace_booking(nombre, ci, co, self.huespedes),
                "airbnb_busqueda": (f"https://www.airbnb.es/s/{quote_plus(nombre)}/homes?checkin={ci}"
                                    f"&checkout={co}&adults={self.huespedes}"),
                "nota": ""}
        clave = (round(float(lat or 0), 3), round(float(lon or 0), 3), ci, co)
        if clave in self.cache:
            return self.cache[clave]
        if lat is None or lon is None:
            base["nota"] = "sin coordenadas para buscar alojamiento"
            return base
        if self.desactivado:
            base["nota"] = f"Airbnb no consultado: {self.desactivado}"
            return base
        if self.limite_tiempo is None:
            self.limite_tiempo = time.monotonic() + self.tiempo_max_s
        if self.busquedas >= self.max_busquedas or time.monotonic() > self.limite_tiempo:
            base["nota"] = "límite diario de búsquedas de alojamiento alcanzado"
            return base

        self.busquedas += 1
        dlat = self.radio_km / 111.0
        dlon = self.radio_km / (111.0 * max(0.2, math.cos(math.radians(float(lat)))))
        try:
            def consulta():
                return pyairbnb.search_first_page(
                    check_in=ci, check_out=co,
                    ne_lat=float(lat) + dlat, ne_long=float(lon) + dlon,
                    sw_lat=float(lat) - dlat, sw_long=float(lon) - dlon,
                    zoom_value=12, price_min=0, price_max=0, adults=self.huespedes,
                    currency=self.moneda, language="en", timeout=30)
            crudos = con_timeout(consulta, 60) or []
            opciones = []
            for r in crudos:
                try:
                    total = float(((r.get("price") or {}).get("total") or {}).get("amount") or 0)
                    if total <= 0:
                        unidad = float(((r.get("price") or {}).get("unit") or {}).get("amount") or 0)
                        total = unidad * n if unidad > 0 else 0
                    rating = r.get("rating") or {}
                    val = float(rating.get("value") or 0)
                    resenas = int(str(rating.get("reviewCount") or "0").replace(".", "").replace(",", "") or 0)
                    rid = r.get("room_id")
                    if total <= 0 or not rid or val < self.val_min or resenas < self.res_min:
                        continue
                    opciones.append({
                        "nombre": (r.get("name") or r.get("title") or "Alojamiento")[:70],
                        "tipo": (r.get("title") or "")[:60],
                        "precio_total": round(total, 0),
                        "precio_noche": round(total / n, 0),
                        "valoracion": val,
                        "resenas": resenas,
                        "enlace": (f"https://www.airbnb.es/rooms/{rid}?check_in={ci}&check_out={co}"
                                   f"&adults={self.huespedes}"),
                    })
                except Exception:
                    continue
            opciones.sort(key=lambda o: o["precio_total"])
            base["opciones"] = opciones[: self.n_opciones]
            if not base["opciones"]:
                base["nota"] = (f"Airbnb respondió ({len(crudos)} anuncios) pero ninguno cumple "
                                f"valoración ≥ {self.val_min} y ≥ {self.res_min} reseñas")
            self.errores_seguidos = 0
        except Exception as e:
            self.errores += 1
            self.errores_seguidos += 1
            base["nota"] = f"error consultando Airbnb ({type(e).__name__})"
            log(f"  ! Airbnb {nombre} {ci}->{co}: {type(e).__name__}: {e}"[:220])
            if self.errores_seguidos >= 5:
                self.desactivado = f"5 errores seguidos (último: {type(e).__name__}: {e})"[:250]
                log("  !! Airbnb desactivado: " + self.desactivado)
        self.cache[clave] = base
        time.sleep(1.0)
        return base


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
    # Añade los destinos más baratos que Ryanair haya encontrado hoy
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
        c = coords_aeropuerto(destino)
        fila = {"destino": destino, "ciudad": c[2] if c else destino, "pais": c[3] if c else "",
                "combinaciones": {}}
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


def ventana_esqui(cfg: dict) -> list[str]:
    """Días de salida a revisar hoy: una ventana de N días consecutivos que rota por la temporada."""
    t = cfg["temporada_esqui"]
    try:
        desde, hasta = date.fromisoformat(str(t["desde"])), date.fromisoformat(str(t["hasta"]))
    except Exception:
        return []
    inicio = max(desde, hoy() + timedelta(days=3))
    ultimo = hasta - timedelta(days=t["dias_min"] - 1)
    if ultimo < inicio:
        return []
    todos = [(inicio + timedelta(days=i)).isoformat() for i in range((ultimo - inicio).days + 1)]
    v = t["ventana_dias_salida"]
    ventanas = [todos[i:i + v] for i in range(0, len(todos), v)]
    return ventanas[hoy().toordinal() % len(ventanas)]


def combinaciones_vuelo(g: Google, origenes: list[str], aeropuertos: list[str],
                        salidas: list[str], noches_validas: list[int]) -> dict[tuple, IdaVuelta]:
    """Mejor ida+vuelta (por persona) para cada par (fecha ida, fecha vuelta) con la duración pedida,
    probando todos los orígenes y aeropuertos de la zona (se puede volver desde otro aeropuerto)."""
    fechas_vuelta = sorted({(date.fromisoformat(d) + timedelta(days=n)).isoformat()
                            for d in salidas for n in noches_validas})
    mejor: dict[tuple, IdaVuelta] = {}
    for o in origenes:
        idas: dict[str, Tramo] = {}
        con_ida: set[str] = set()
        for a in aeropuertos:
            for d in salidas:
                t = g.tramo(o, a, d)
                if t:
                    con_ida.add(a)
                    if d not in idas or t[0].precio < idas[d].precio:
                        idas[d] = t[0]
        if not idas:
            continue
        vueltas: dict[str, Tramo] = {}
        for a in aeropuertos:
            if a not in con_ida:
                continue  # si no hay vuelos de ida a ese aeropuerto, tampoco buscamos la vuelta
            for r in fechas_vuelta:
                t = g.tramo(a, o, r)
                if t and (r not in vueltas or t[0].precio < vueltas[r].precio):
                    vueltas[r] = t[0]
        for d in salidas:
            for n in noches_validas:
                r = (date.fromisoformat(d) + timedelta(days=n)).isoformat()
                if d in idas and r in vueltas:
                    iv = IdaVuelta(idas[d], vueltas[r])
                    if (d, r) not in mejor or iv.precio < mejor[(d, r)].precio:
                        mejor[(d, r)] = iv
    return mejor


def campana_esqui(cfg: dict, g: Google) -> tuple[list[dict], list[str]]:
    salidas = ventana_esqui(cfg)
    zonas = list(cfg["destinos_esqui"])
    if not salidas or not zonas:
        return [], salidas
    t = cfg["temporada_esqui"]
    noches_validas = list(range(t["dias_min"] - 1, t["dias_max"]))
    # Rotamos el orden de las zonas cada día: si se acaba el tiempo, no siempre se quedan fuera las mismas
    k = hoy().toordinal() % len(zonas)
    zonas = zonas[k:] + zonas[:k]
    origenes_A = cfg["viajeros"]["A"]["principales_esqui"]
    origenes_B = cfg["viajeros"]["B"]["principales_esqui"]
    resultados = []
    for z in zonas:
        log(f"Esquí {z['zona']} · salidas {salidas[0]}…{salidas[-1]} · {t['dias_min']}-{t['dias_max']} días")
        try:
            b = combinaciones_vuelo(g, origenes_B, z["aeropuertos"], salidas, noches_validas)
            a = combinaciones_vuelo(g, origenes_A, z["aeropuertos"], salidas, noches_validas) if z["vuela_A"] else {}
        except Exception as e:
            log(f"  ! esquí {z['zona']}: {e}")
            a, b = {}, {}
        opciones = []
        for (ida, vta), ivb in b.items():
            iva = a.get((ida, vta)) if z["vuela_A"] else None
            if z["vuela_A"] and iva is None:
                continue  # si A tiene que volar, solo valen fechas en las que vuelan los dos
            total = ivb.precio + (iva.precio if iva else 0)
            opciones.append({"ida": ida, "vuelta": vta, "dias": noches(ida, vta) + 1,
                             "B": ivb.a_dict(), "A": iva.a_dict() if iva else None,
                             "total_vuelos": round(total, 2)})
        # Mejor opción para cada duración (3, 4, 5, 6 días)
        por_duracion: dict[int, dict] = {}
        for op in sorted(opciones, key=lambda x: x["total_vuelos"]):
            por_duracion.setdefault(op["dias"], op)
        resultados.append({"zona": z["zona"], "base": z["base"], "transporte": z["transporte"],
                           "aeropuertos": z["aeropuertos"], "A_en_tren": not z["vuela_A"],
                           "opciones": [por_duracion[d] for d in sorted(por_duracion)],
                           "alojamiento": None})
    return resultados, salidas


def anadir_alojamiento(res: dict, aloj: Alojamiento) -> None:
    """Busca alojamiento real para las mejores opciones de cada campaña (por orden de interés)."""
    # 1) Esquí: para la opción más barata por día de cada zona
    for z in res.get("esqui") or []:
        if not z["opciones"]:
            continue
        op = min(z["opciones"], key=lambda o: o["total_vuelos"] / o["dias"])
        b = z["base"]
        log(f"Alojamiento esquí {b['nombre']} {op['ida']}->{op['vuelta']}")
        z["alojamiento"] = aloj.buscar(b["nombre"], b.get("lat"), b.get("lon"), op["ida"], op["vuelta"])
        z["alojamiento"]["para_opcion"] = f"{op['ida']}_{op['vuelta']}"
    # 2) Ofertas sorpresa
    for o in res.get("sorpresa_top") or []:
        c = coords_aeropuerto(o["destino"])
        log(f"Alojamiento sorpresa {o['ciudad']} {o['salida_ida'][:10]}->{o['salida_vuelta'][:10]}")
        o["alojamiento"] = aloj.buscar(o["ciudad"], c[0] if c else None, c[1] if c else None,
                                       o["salida_ida"][:10], o["salida_vuelta"][:10])
    # 3) Puente: para las 3 combinaciones de los 4 destinos con el vuelo más barato
    filas = [f for f in res.get("puente") or [] if any(f["combinaciones"].values())]
    filas.sort(key=lambda f: min(v["total"] for v in f["combinaciones"].values() if v))
    for f in filas[:4]:
        c = coords_aeropuerto(f["destino"])
        f["alojamiento"] = {}
        for k, v in f["combinaciones"].items():
            if not v:
                continue
            ida, vta = k.split("_")
            log(f"Alojamiento puente {f['ciudad']} {ida}->{vta}")
            f["alojamiento"][k] = aloj.buscar(f["ciudad"], c[0] if c else None, c[1] if c else None, ida, vta)


def seleccionar_sorpresa(sorpresa: list[dict], umbral: float, maximo: int) -> list[dict]:
    """La oferta más barata de cada destino, por debajo del umbral."""
    vistos, top = set(), []
    for o in sorpresa:
        if o["precio_ida_vuelta"] >= umbral or o["destino"] in vistos:
            continue
        vistos.add(o["destino"])
        c = coords_aeropuerto(o["destino"])
        top.append(dict(o, pais_codigo=c[3] if c else "", alojamiento=None))
        if len(top) >= maximo:
            break
    return top


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


def fecha_corta(s: str) -> str:
    """'2026-12-04...' -> 'vie 04/12'"""
    try:
        d = date.fromisoformat(s[:10])
        return f"{DIAS_SEMANA[d.weekday()]} {d:%d/%m}"
    except Exception:
        return s or "?"


def linea_vuelo(t: dict) -> str:
    return (f"{fecha_corta(t['salida'])} · {t['origen']} {hora(t['salida'])} → {t['destino']} {hora(t['llegada'])} "
            f"· {t['aerolineas']}{' · ' + str(t['escalas']) + ' escala' if t['escalas'] else ' · directo'} "
            f"· {eur(t['precio'])} [ver]({t['enlace']})")


def bloque_iv(etiqueta: str, d: dict | None) -> list[str]:
    if not d:
        return [f"  - {etiqueta}: sin vuelos encontrados"]
    return [f"  - {etiqueta} — **{eur(d['total'])}** por persona",
            f"    - Ida: {linea_vuelo(d)}",
            f"    - Vuelta: {linea_vuelo(d['vuelta'])}"]


def bloque_alojamiento(a: dict | None, sangria: str = "  ") -> list[str]:
    if not a:
        return []
    out = [f"{sangria}- 🏠 Alojamiento 2 personas en {a['lugar']} ({a['noches']} noches, "
           f"{fecha_corta(a['check_in'])} → {fecha_corta(a['check_out'])}):"]
    if a["opciones"]:
        for o in a["opciones"]:
            out.append(f"{sangria}  - **{eur(o['precio_total'])} total** ({eur(o['precio_noche'])}/noche) · "
                       f"{o['nombre']} · ★ {o['valoracion']:.2f} ({o['resenas']} reseñas) · "
                       f"[Airbnb]({o['enlace']}) ✅ precio real")
    else:
        out.append(f"{sangria}  - Sin precio verificado: {a.get('nota') or 'sin resultados'}")
    out.append(f"{sangria}  - Comparar: [Booking con estas fechas]({a['booking']}) · "
               f"[más pisos en Airbnb]({a['airbnb_busqueda']})")
    return out


def nombre_combi(clave: str) -> str:
    ida, vta = clave.split("_")
    return f"{fecha_corta(ida)} → {fecha_corta(vta)}"


def comparativas_puente(puente: list[dict]) -> list[str]:
    """Salir el 4 vs el 5 (vuelta el 9) y volver el 9 vs el 8 (salida el 4), por destino (solo vuelos)."""
    lineas = []
    for fila in puente:
        claves = {k: v for k, v in fila["combinaciones"].items() if v}

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
            lineas.append(f"{fila['ciudad']} ({fila['destino']}): " + "; ".join(partes))
    return lineas


def informe_md(res: dict) -> str:
    out = [f"# Vigilancia de vuelos — {res['fecha_consulta']}", ""]
    out.append(f"Google Flights: {res.get('peticiones_google', 0)} consultas "
               f"({res.get('exitos_google', 0)} con vuelos, {res.get('sin_vuelos_google', 0)} sin vuelos, "
               f"{res.get('errores_google', 0)} errores, {res.get('rutas_saltadas', 0)} rutas saltadas). "
               f"Ryanair: {len(res.get('sorpresa', []))} tarifas. Airbnb: {res.get('busquedas_airbnb', 0)} búsquedas.")
    out.append("Vuelos: precio **por persona**, tarifa básica (sin maleta de cabina grande); ida y vuelta de "
               "Google = suma de dos billetes de ida; horas locales. Alojamiento: precio **total para 2 personas** "
               "con tasas de Airbnb, consultado hoy para esas fechas exactas. Sin coche: todo en tren/bus.")
    out.append("")
    if res.get("avisos"):
        out.append("## ⚠️ Avisos de la ejecución")
        out += [f"- {a}" for a in res["avisos"]]
        out.append("")

    # ---------- Ofertas sorpresa
    umbral = res.get("umbral", 150)
    out.append(f"## 🎁 Ofertas sorpresa desde Bilbao y alrededores (Ryanair, ida y vuelta < {eur(umbral)})")
    top = res.get("sorpresa_top") or []
    if not top:
        out.append("- Sin ofertas bajo el umbral hoy (o Ryanair no respondió; ver avisos).")
    for o in top:
        n = noches(o["salida_ida"], o["salida_vuelta"])
        out.append("")
        out.append(f"### {bandera(o.get('pais_codigo', ''))} {o['ciudad']} ({o['destino']}) — "
                   f"{eur(o['precio_ida_vuelta'])} ida y vuelta por persona")
        out.append(f"- ✈️ Ida: {fecha_corta(o['salida_ida'])} · {o['origen']} {hora(o['salida_ida'])} → "
                   f"{o['destino']} {hora(o['llegada_ida'])}")
        out.append(f"- ✈️ Vuelta: {fecha_corta(o['salida_vuelta'])} · {o['destino']} {hora(o['salida_vuelta'])} → "
                   f"{o['origen']} {hora(o['llegada_vuelta'])}")
        out.append(f"- 🗓️ {n} noches · [ver vuelo en Ryanair]({o['enlace']})")
        a = o.get("alojamiento")
        out += bloque_alojamiento(a, sangria="")
        if a and a["opciones"]:
            barato = a["opciones"][0]["precio_total"]
            total2 = 2 * o["precio_ida_vuelta"] + barato
            out.append(f"- 💶 **Total 2 personas: {eur(total2)}** (vuelos {eur(2 * o['precio_ida_vuelta'])} + "
                       f"alojamiento {eur(barato)}) → **{eur(total2 / 2)} por persona**")
    out.append("")

    # ---------- Esquí
    sal = res.get("esqui_salidas") or []
    rango = f"salidas entre {fecha_corta(sal[0])} y {fecha_corta(sal[-1])}" if sal else "sin fechas en temporada"
    out.append(f"## ⛷️ Esquí — viajes de {res.get('esqui_dias', '3-6')} días ({rango})")
    out.append("Mejor opción de vuelos para cada duración. B = Bilbao, A = Stuttgart. "
               "Cuando A va en tren, el tren lo calcula Claude (DB/SBB/ÖBB).")
    esq = res.get("esqui") or []
    esq_ord = sorted(esq, key=lambda z: min([o["total_vuelos"] / o["dias"] for o in z["opciones"]] or [1e9]))
    if not any(z["opciones"] for z in esq):
        out.append("- Sin vuelos encontrados hoy para esta ventana de fechas.")
    for z in esq_ord:
        if not z["opciones"]:
            continue
        out.append("")
        out.append(f"### {z['zona']}")
        out.append(f"- 🚆 Sin coche: {z['transporte']}" if z["transporte"] else "- 🚆 Transporte público a confirmar")
        out.append(f"- A (Stuttgart): {'va en tren' if z['A_en_tren'] else 'vuela (incluido abajo)'}")
        for op in z["opciones"]:
            cab = (f"- **{op['dias']} días · {fecha_corta(op['ida'])} → {fecha_corta(op['vuelta'])} · "
                   f"vuelos {eur(op['total_vuelos'])}**" + ("" if z["A_en_tren"] else " (A + B)"))
            out.append(cab)
            out += bloque_iv("B desde Bilbao", op["B"])
            if not z["A_en_tren"]:
                out += bloque_iv("A desde Stuttgart", op["A"])
        if z.get("alojamiento"):
            out += bloque_alojamiento(z["alojamiento"], sangria="")
    out.append("")

    # ---------- Puente
    if res.get("puente"):
        claves = list(res["puente"][0]["combinaciones"].keys())
        out.append("## 🎄 Puente de diciembre — B desde aeropuertos de Bilbao (vuelo por persona)")
        out.append("| Destino | " + " | ".join(nombre_combi(c) for c in claves) + " | Mejor |")
        out.append("|---|" + "---|" * (len(claves) + 1))
        for fila in res["puente"]:
            c = fila["combinaciones"]
            celdas = [f"{eur(d['total'])} ({d['origen']} {hora(d['salida'])}/{hora(d['vuelta']['salida'])})"
                      if (d := c.get(k)) else "—" for k in claves]
            validas = {k: v for k, v in c.items() if v}
            mejor = nombre_combi(min(validas, key=lambda k: validas[k]["total"])) if validas else "—"
            out.append(f"| {fila['ciudad']} ({fila['destino']}) | " + " | ".join(celdas) + f" | {mejor} |")
        out.append("")
        comp = res.get("puente_comparativas") or []
        if comp:
            out.append("**Efecto de cambiar un día (solo vuelos, por persona):**")
            out += [f"- {x}" for x in comp]
            out.append("")
        for fila in res["puente"]:
            if not fila.get("alojamiento"):
                continue
            out.append(f"### {bandera(fila.get('pais', ''))} {fila['ciudad']} ({fila['destino']}) — detalle")
            for k, v in fila["combinaciones"].items():
                if not v:
                    continue
                out.append(f"- **{nombre_combi(k)}**")
                out += bloque_iv("Vuelo B", v)
                out += bloque_alojamiento(fila["alojamiento"].get(k), sangria="  ")
            out.append("")

    # ---------- Bajadas
    out.append(f"## 📉 Bajadas de precio (≥ {res.get('bajada_pct', 20)} % frente al mínimo de días anteriores)")
    if res.get("bajadas"):
        for b in res["bajadas"][:15]:
            out.append(f"- {b['origen']}→{b['destino']} {fecha_corta(b['salida'])} {hora(b['salida'])} "
                       f"({b['aerolineas']}): {eur(b['precio_hoy'])} (antes mín. {eur(b['minimo_anterior'])}, "
                       f"-{b['bajada_pct']} %) [ver]({b['enlace']})")
    else:
        out.append("- Ninguna hoy (o aún no hay histórico suficiente).")
    return "\n".join(out) + "\n"


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
    aloj = Alojamiento(cfg)
    fecha_consulta = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    t = cfg["temporada_esqui"]
    res: dict = {"fecha_consulta": fecha_consulta, "umbral": cfg["umbral_aviso_ida_vuelta_eur"],
                 "bajada_pct": cfg["bajada_relevante_pct"], "avisos": avisos,
                 "esqui_dias": f"{t['dias_min']}-{t['dias_max']}",
                 "puente": [], "puente_comparativas": [], "esqui": [], "esqui_salidas": [],
                 "sorpresa": [], "sorpresa_top": [], "bajadas": []}

    # 1) Ryanair primero (rápido); sus destinos baratos alimentan el puente
    try:
        res["sorpresa"], fallos = ryanair_sorpresa(cfg, cfg["viajeros"]["B"]["aeropuertos"])
        if fallos:
            res["avisos"].append("Ryanair falló para: " + "; ".join(fallos))
        elif not res["sorpresa"]:
            res["avisos"].append("Ryanair respondió pero sin tarifas legibles (¿cambio en su API?).")
        res["sorpresa_top"] = seleccionar_sorpresa(res["sorpresa"], float(res["umbral"]),
                                                   int(cfg["sorpresa"].get("max_ofertas_informe", 8)))
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
        res["esqui"], res["esqui_salidas"] = campana_esqui(cfg, g)
    except Exception as e:
        traceback.print_exc()
        res["avisos"].append(f"Fallo en campaña esquí: {type(e).__name__}: {e}")

    # 4) Alojamiento real para las mejores opciones
    try:
        anadir_alojamiento(res, aloj)
    except Exception as e:
        traceback.print_exc()
        res["avisos"].append(f"Fallo buscando alojamiento: {type(e).__name__}: {e}")

    if g.desactivado:
        res["avisos"].append(f"Google Flights desactivado: {g.desactivado}")
    if g.agotado:
        res["avisos"].append(f"Búsqueda en Google cortada por {g.agotado}; lo pendiente se revisa otro día.")
    if aloj.desactivado and aloj.activo:
        res["avisos"].append(f"Alojamiento (Airbnb) no disponible: {aloj.desactivado}. "
                             "Claude lo buscará por la web y los enlaces de Booking siguen en el informe.")
    res.update(peticiones_google=g.peticiones, exitos_google=g.exitos, sin_vuelos_google=g.sin_vuelos,
               errores_google=g.errores, rutas_saltadas=g.saltadas, busquedas_airbnb=aloj.busquedas)

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
        f"Airbnb {aloj.busquedas} búsquedas ({aloj.errores} errores); {len(res['bajadas'])} bajadas.")
    if g.exitos == 0 and not res["sorpresa"]:
        log("Ninguna fuente de vuelos devolvió datos hoy.")
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
                              "puente": [], "esqui": [], "sorpresa": [], "sorpresa_top": [], "bajadas": []})
        except Exception:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
