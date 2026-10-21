"""Vigilancia diaria de viajes: escapadas de fin de semana (próximas y desde enero) y esquí (3-6 días).

Fuentes:
  - Google Flights (protocolo interno, vía la librería fast-flights; sin API key)
  - Tarifas públicas de Ryanair (fines de semana vie→dom desde Bilbao y alrededores)
  - OpenStreetMap/Nominatim (centro de ciudades que no están en la tabla interna)
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
 'tiempo_max_google_min': 75,
 'timeout_peticion_s': 30,
 'max_escalas': 1,
 'historico_dias': 30,
 'umbral_aviso_ida_vuelta_eur': 150,
 'bajada_relevante_pct': 20,
 'excluir_paises_destino': ['ES'],
 'paises_permitidos': ['AL',
                       'AD',
                       'AT',
                       'BE',
                       'BA',
                       'BG',
                       'HR',
                       'CY',
                       'CZ',
                       'DK',
                       'EE',
                       'FI',
                       'FR',
                       'DE',
                       'GR',
                       'HU',
                       'IS',
                       'IE',
                       'IT',
                       'XK',
                       'LV',
                       'LI',
                       'LT',
                       'LU',
                       'MT',
                       'MD',
                       'MC',
                       'ME',
                       'NL',
                       'MK',
                       False,
                       'PL',
                       'PT',
                       'RO',
                       'SM',
                       'RS',
                       'SK',
                       'SI',
                       'SE',
                       'CH',
                       'UA',
                       'GB',
                       'GI',
                       'GG',
                       'JE',
                       'IM',
                       'FO',
                       'TR',
                       'VA',
                       'MA',
                       'DZ',
                       'TN',
                       'LY',
                       'EG'],
 'google_findes': {'activo': True,
                   'origen': 'BIO',
                   'findes_por_dia': 2,
                   'destinos': ['AMS',
                                'CDG',
                                'ORY',
                                'LHR',
                                'LGW',
                                'BRU',
                                'FRA',
                                'MUC',
                                'ZRH',
                                'GVA',
                                'LIS',
                                'OPO',
                                'FCO',
                                'MXP',
                                'VCE',
                                'NCE',
                                'MRS',
                                'LYS',
                                'DUB',
                                'EDI',
                                'CPH',
                                'VIE',
                                'PRG',
                                'BUD',
                                'BER',
                                'RAK',
                                'TNG',
                                'ATH',
                                'MLA',
                                'KRK']},
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
 'sorpresa': {'semanas_vista': 10,
              'duracion_min_dias': 2,
              'duracion_max_dias': 2,
              'dias_salida': ['vie'],
              'dias_vuelta': ['dom'],
              'max_ofertas_informe': 6},
 'desde_enero': {'activo': True,
                 'desde': '2027-01-01',
                 'hasta': '2027-06-30',
                 'mejores_por_mes': 2,
                 'comparar_con_bilbao': True},
 'acceso_desde_bilbao': {'BIO': {'eur': 3,
                                 'min': 30,
                                 'como': 'Bizkaibus A3247 (Moyua/Termibus → aeropuerto)'},
                         'VIT': {'eur': 10,
                                 'min': 80,
                                 'como': 'bus Bilbao → Vitoria + bus al aeropuerto'},
                         'SDR': {'eur': 9,
                                 'min': 85,
                                 'como': 'ALSA Bilbao → Santander + bus al aeropuerto'},
                         'EAS': {'eur': 11,
                                 'min': 100,
                                 'como': 'bus Bilbao → Donostia + bus E21 al aeropuerto'},
                         'BIQ': {'eur': 16,
                                 'min': 150,
                                 'como': 'bus Bilbao → Biarritz (ALSA/FlixBus) + bus al '
                                         'aeropuerto'},
                         'PNA': {'eur': 16,
                                 'min': 130,
                                 'como': 'bus Bilbao → Pamplona + bus/taxi al aeropuerto'}},
 'valor_tiempo_eur_hora': 15,
 'alojamiento': {'activo': True,
                 'huespedes': 2,
                 'radio_centro_km': 1.5,
                 'radio_afueras_km': 12,
                 'radio_aeropuerto_km': 4,
                 'opciones_centro': 2,
                 'opciones_baratas': 2,
                 'valoracion_min': 4.5,
                 'resenas_min': 5,
                 'max_busquedas': 90,
                 'tiempo_max_min': 25},
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
    cfg.pop("puente", None)  # campaña eliminada (por si queda en un config.yaml antiguo)
    if not isinstance(cfg.get("acceso_desde_bilbao"), dict):
        cfg["acceso_desde_bilbao"] = DEFECTO["acceso_desde_bilbao"]
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
def ventanas_campanas(cfg: dict) -> dict[str, tuple[date, date]]:
    """Periodos de búsqueda: 'sorpresa' (próximas semanas) y 'enero' (de enero en adelante),
    sin solaparse."""
    res = {}
    inicio = hoy() + timedelta(days=3)
    fin_s = hoy() + timedelta(weeks=int(cfg["sorpresa"]["semanas_vista"]))
    e = cfg.get("desde_enero") or {}
    if e.get("activo", True):
        try:
            e_desde = max(date.fromisoformat(str(e["desde"])), inicio)
            e_hasta = date.fromisoformat(str(e["hasta"]))
            if e_hasta >= e_desde:
                res["enero"] = (e_desde, e_hasta)
                fin_s = min(fin_s, e_desde - timedelta(days=1))
        except Exception as ex:
            log(f"  ! desde_enero mal configurado: {ex}")
    if fin_s >= inicio:
        res["sorpresa"] = (inicio, fin_s)
    return res


def ryanair_sorpresa(cfg: dict, origenes: list[str], inicio: date | None = None,
                     fin: date | None = None) -> tuple[list[dict], list[str]]:
    s = cfg["sorpresa"]
    ofertas: list[dict] = []
    fallos: list[str] = []
    inicio = inicio or hoy() + timedelta(days=3)
    fin = fin or hoy() + timedelta(weeks=int(s["semanas_vista"]))
    dias_ida = dias_semana(s.get("dias_salida"))
    dias_vta = dias_semana(s.get("dias_vuelta"))
    dmin, dmax = int(s["duracion_min_dias"]), int(s["duracion_max_dias"])
    # Ventanas de consulta: con días fijos (p. ej. viernes→domingo) una consulta por fin de
    # semana con fechas exactas; sin días fijos, una sola consulta para todo el periodo.
    ventanas = []
    if dias_ida:
        d = inicio
        while d <= fin:
            if d.weekday() in dias_ida:
                vueltas = [d + timedelta(days=k) for k in range(dmin, dmax + 1)
                           if not dias_vta or (d + timedelta(days=k)).weekday() in dias_vta]
                if vueltas:
                    ventanas.append((d, d, min(vueltas), max(vueltas)))
            d += timedelta(days=1)
    else:
        ventanas.append((inicio, fin, inicio, fin + timedelta(days=dmax)))
    fallos_seguidos = 0
    for o in origenes:
        fallo_origen = None
        for ida_desde, ida_hasta, vta_desde, vta_hasta in ventanas:
            if fallos_seguidos >= 4:
                break
            params = {
                "departureAirportIataCode": o,
                "outboundDepartureDateFrom": ida_desde.isoformat(),
                "outboundDepartureDateTo": ida_hasta.isoformat(),
                "inboundDepartureDateFrom": vta_desde.isoformat(),
                "inboundDepartureDateTo": vta_hasta.isoformat(),
                "durationFrom": dmin,
                "durationTo": dmax,
                "currency": cfg["moneda"],
                "market": "es-es",
                "language": "es",
            }
            datos, err = None, ""
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
                fallos_seguidos += 1
                fallo_origen = err
                continue
            fallos_seguidos = 0
            ofertas.extend(_fares_ryanair(datos, o))
            time.sleep(0.3)
        if fallo_origen:
            fallos.append(f"{o}: {fallo_origen}"[:150])
            log(f"  ! Ryanair {o}: {fallo_origen}"[:200])
    if fallos_seguidos >= 4:
        fallos.append("Ryanair dejó de responder; se cortó la búsqueda de ofertas sorpresa")
    ofertas = [x for x in ofertas if not destino_excluido(cfg, x["destino"], x.get("pais", ""))]
    # Filtro estricto de días
    ofertas = [x for x in ofertas
               if dia_ok(x["salida_ida"], dias_ida) and dia_ok(x["salida_vuelta"], dias_vta)]
    ofertas.sort(key=lambda x: x["precio_ida_vuelta"])
    return ofertas, fallos


def _fares_ryanair(datos, o: str) -> list[dict]:
    ofertas = []
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
    return ofertas


_ALIAS_DIAS = {"lun": 0, "lunes": 0, "mar": 1, "martes": 1, "mie": 2, "mié": 2, "miercoles": 2,
               "miércoles": 2, "jue": 3, "jueves": 3, "vie": 4, "viernes": 4, "sab": 5, "sáb": 5,
               "sabado": 5, "sábado": 5, "dom": 6, "domingo": 6}


def dias_semana(valor) -> set[int]:
    """Convierte ['vie'] / 'viernes' / [4] en {4}. Vacío = cualquier día."""
    if not valor:
        return set()
    if not isinstance(valor, (list, tuple)):
        valor = [valor]
    res = set()
    for v in valor:
        if isinstance(v, int) and 0 <= v <= 6:
            res.add(v)
        elif str(v).strip().lower() in _ALIAS_DIAS:
            res.add(_ALIAS_DIAS[str(v).strip().lower()])
    return res


def dia_ok(fecha_hora: str, dias: set[int]) -> bool:
    if not dias:
        return True
    try:
        return date.fromisoformat(str(fecha_hora)[:10]).weekday() in dias
    except Exception:
        return False


NOMBRES_PAIS = {"ES": {"españa", "spain", "espana"}}


def destino_excluido(cfg: dict, iata: str, nombre_pais: str = "") -> bool:
    """True si el destino está en un país excluido (por defecto España: solo viajes al extranjero)."""
    try:
        excluidos = {str(p).upper() for p in (cfg.get("excluir_paises_destino") or [])}
        permitidos = {str(p).upper() for p in (cfg.get("paises_permitidos") or [])}
        c = coords_aeropuerto(iata)
        if c and c[3]:
            pais = c[3].upper()
            if pais in excluidos:
                return True
            return bool(permitidos) and pais not in permitidos  # fuera de Europa / norte de África
        if not excluidos:
            return False
        n = (nombre_pais or "").strip().lower()
        return any(n in NOMBRES_PAIS.get(p, set()) for p in excluidos)
    except Exception:
        return False


# --------------------------------------------------------------- Alojamiento
# Centro de la ciudad para cada aeropuerto (dónde se busca "en el centro").
# Si un destino no está aquí, se geocodifica con OpenStreetMap (Nominatim) y se guarda en
# data/centros.json; si eso también falla, se usa el propio aeropuerto como referencia.
CENTROS: dict[str, tuple[str, float, float]] = {
    "STN": ("Londres", 51.5080, -0.1281), "LTN": ("Londres", 51.5080, -0.1281),
    "LGW": ("Londres", 51.5080, -0.1281), "LHR": ("Londres", 51.5080, -0.1281),
    "SEN": ("Londres", 51.5080, -0.1281), "LCY": ("Londres", 51.5080, -0.1281),
    "CRL": ("Bruselas", 50.8467, 4.3525), "BRU": ("Bruselas", 50.8467, 4.3525),
    "BGY": ("Bérgamo", 45.6950, 9.6700), "MXP": ("Milán", 45.4642, 9.1900),
    "LIN": ("Milán", 45.4642, 9.1900), "BVA": ("París", 48.8566, 2.3522),
    "ORY": ("París", 48.8566, 2.3522), "CDG": ("París", 48.8566, 2.3522),
    "DUB": ("Dublín", 53.3498, -6.2603), "EDI": ("Edimburgo", 55.9533, -3.1883),
    "MAN": ("Mánchester", 53.4808, -2.2426), "FCO": ("Roma", 41.9028, 12.4964),
    "CIA": ("Roma", 41.9028, 12.4964), "NAP": ("Nápoles", 40.8518, 14.2681),
    "PMO": ("Palermo", 38.1157, 13.3615), "CTA": ("Catania", 37.5079, 15.0830),
    "BRI": ("Bari", 41.1171, 16.8719), "BLQ": ("Bolonia", 44.4949, 11.3426),
    "PSA": ("Pisa", 43.7228, 10.4017), "FLR": ("Florencia", 43.7731, 11.2558),
    "VCE": ("Venecia", 45.4408, 12.3155), "TSF": ("Venecia", 45.4408, 12.3155),
    "TRN": ("Turín", 45.0703, 7.6869), "GOA": ("Génova", 44.4056, 8.9463),
    "VRN": ("Verona", 45.4384, 10.9916), "CAG": ("Cagliari", 39.2238, 9.1217),
    "RAK": ("Marrakech", 31.6258, -7.9891), "FEZ": ("Fez", 34.0181, -5.0078),
    "TNG": ("Tánger", 35.7595, -5.8340), "AGA": ("Agadir", 30.4278, -9.5981),
    "LIS": ("Lisboa", 38.7223, -9.1393), "OPO": ("Oporto", 41.1579, -8.6291),
    "FAO": ("Faro", 37.0194, -7.9322), "BER": ("Berlín", 52.5200, 13.4050),
    "MUC": ("Múnich", 48.1374, 11.5755), "FRA": ("Fráncfort", 50.1109, 8.6821),
    "NRN": ("Düsseldorf", 51.2277, 6.7735), "DUS": ("Düsseldorf", 51.2277, 6.7735),
    "CGN": ("Colonia", 50.9375, 6.9603), "HAM": ("Hamburgo", 53.5511, 9.9937),
    "AMS": ("Ámsterdam", 52.3731, 4.8926), "EIN": ("Eindhoven", 51.4416, 5.4697),
    "VIE": ("Viena", 48.2082, 16.3738), "BUD": ("Budapest", 47.4979, 19.0402),
    "PRG": ("Praga", 50.0755, 14.4378), "KRK": ("Cracovia", 50.0614, 19.9366),
    "WAW": ("Varsovia", 52.2297, 21.0122), "WMI": ("Varsovia", 52.2297, 21.0122),
    "GDN": ("Gdansk", 54.3520, 18.6466), "CPH": ("Copenhague", 55.6761, 12.5683),
    "ARN": ("Estocolmo", 59.3293, 18.0686), "NYO": ("Estocolmo", 59.3293, 18.0686),
    "OSL": ("Oslo", 59.9139, 10.7522), "TRF": ("Oslo", 59.9139, 10.7522),
    "MRS": ("Marsella", 43.2965, 5.3698), "NCE": ("Niza", 43.7102, 7.2620),
    "LYS": ("Lyon", 45.7640, 4.8357), "TLS": ("Toulouse", 43.6047, 1.4442),
    "BOD": ("Burdeos", 44.8378, -0.5792), "NTE": ("Nantes", 47.2184, -1.5536),
    "GVA": ("Ginebra", 46.2044, 6.1432), "ZRH": ("Zúrich", 47.3769, 8.5417),
    "BSL": ("Basilea", 47.5596, 7.5886), "ATH": ("Atenas", 37.9838, 23.7275),
    "SKG": ("Salónica", 40.6401, 22.9444), "MLA": ("La Valeta", 35.8989, 14.5146),
    "SOF": ("Sofía", 42.6977, 23.3219), "OTP": ("Bucarest", 44.4268, 26.1025),
    "BTS": ("Bratislava", 48.1486, 17.1077), "LJU": ("Liubliana", 46.0569, 14.5058),
    "ZAG": ("Zagreb", 45.8150, 15.9819), "SPU": ("Split", 43.5081, 16.4402),
    "DBV": ("Dubrovnik", 42.6507, 18.0944), "INN": ("Innsbruck", 47.2654, 11.3928),
    "SZG": ("Salzburgo", 47.8095, 13.0550),
}
CENTROS_JSON = DATA / "centros.json"


def distancia_km(lat1, lon1, lat2, lon2) -> float:
    try:
        p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
        dp, dl = p2 - p1, math.radians(float(lon2) - float(lon1))
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, a)))
    except Exception:
        return float("nan")


def tiempo_al_centro(km: float) -> tuple[int, str, float]:
    """(minutos, modo, € por persona y trayecto) ESTIMADOS para ir de un alojamiento al centro."""
    if km != km:  # NaN
        return 0, "?", 0.0
    if km <= 1.3:
        return max(5, int(round(km * 13 / 5.0)) * 5), "andando", 0.0
    if km <= 15:
        return int(round((12 + km * 2.6) / 5.0)) * 5, "transporte público", 2.5
    return int(round((20 + km * 1.3) / 5.0)) * 5, "tren/bus", round(min(25.0, 4 + km * 0.25), 0)


class Centros:
    """Coordenadas del centro de cada destino (tabla fija + OpenStreetMap con caché)."""

    def __init__(self):
        self.cache: dict[str, list] = {}
        try:
            self.cache = json.loads(CENTROS_JSON.read_text(encoding="utf-8"))
        except Exception:
            self.cache = {}
        self.consultas = 0

    def get(self, iata: str, ciudad: str = "") -> tuple[str, float, float, bool]:
        """(nombre, lat, lon, es_centro_real)."""
        if iata in CENTROS:
            n, la, lo = CENTROS[iata]
            return n, la, lo, True
        clave = f"{iata}|{ciudad}"
        if clave in self.cache and len(self.cache[clave]) == 3:
            n, la, lo = self.cache[clave]
            return n, float(la), float(lo), True
        c = coords_aeropuerto(iata)
        nombre = ciudad or (c[2] if c else iata)
        if self.consultas < 15:
            self.consultas += 1
            try:
                pais = c[3] if c else ""
                r = requests.get("https://nominatim.openstreetmap.org/search",
                                 params={"q": nombre, "format": "json", "limit": 1,
                                         **({"countrycodes": pais.lower()} if pais else {})},
                                 headers={"User-Agent": "vigilancia-viajes/1.0 (GitHub Actions)"},
                                 timeout=15)
                js = r.json() if r.status_code == 200 else []
                if js:
                    la, lo = float(js[0]["lat"]), float(js[0]["lon"])
                    # descarta resultados absurdos (a más de 80 km del aeropuerto)
                    if not c or distancia_km(la, lo, c[0], c[1]) < 80:
                        self.cache[clave] = [nombre, la, lo]
                        self._guardar()
                        time.sleep(1.1)
                        return nombre, la, lo, True
                time.sleep(1.1)
            except Exception as e:
                log(f"  ! geocodificando {nombre}: {type(e).__name__}")
        if c:
            return nombre, c[0], c[1], False
        return nombre, None, None, False

    def _guardar(self):
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            CENTROS_JSON.write_text(json.dumps(self.cache, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass


def _precio_total(r: dict, n: int) -> float:
    """Precio TOTAL de la estancia a partir de un resultado de pyairbnb (robusto a sus formatos)."""
    precio = r.get("price") or {}
    total = 0.0
    for item in precio.get("break_down") or []:
        if str(item.get("description", "")).strip().lower().startswith("total"):
            total = float(item.get("amount") or 0)
    if total <= 0:
        total = float((precio.get("total") or {}).get("amount") or 0)
    if total <= 0:
        unidad = precio.get("unit") or {}
        total = float(unidad.get("discount") or 0) or float(unidad.get("amount") or 0)
        # Airbnb (UE) muestra el total de la estancia; solo si dice explícitamente "por noche"
        # ("night"/"noche" en singular, sin número de noches) se multiplica.
        q = str(unidad.get("qualifier") or "").lower().strip()
        if total > 0 and n > 1 and q in ("night", "per night", "/ night", "noche", "por noche", "/ noche"):
            total *= n
    return total


class Alojamiento:
    """Precios reales de Airbnb (pisos, apartamentos y hoteles) para N huéspedes y fechas exactas,
    en tres zonas: centro, afueras y junto al aeropuerto."""

    def __init__(self, cfg: dict):
        a = cfg["alojamiento"]
        self.activo = bool(a.get("activo", True))
        self.huespedes = int(a.get("huespedes", 2))
        self.r_centro = float(a.get("radio_centro_km", 1.5))
        self.r_afueras = float(a.get("radio_afueras_km", a.get("radio_km", 12)))
        self.r_aerop = float(a.get("radio_aeropuerto_km", 4))
        self.n_centro = int(a.get("opciones_centro", 2))
        self.n_baratas = int(a.get("opciones_baratas", 2))
        self.val_min = float(a.get("valoracion_min", 4.5))
        self.res_min = int(a.get("resenas_min", 5))
        self.max_busquedas = int(a.get("max_busquedas", 90))
        self.limite_tiempo = None
        self.tiempo_max_s = 60 * float(a.get("tiempo_max_min", 25))
        self.moneda = cfg["moneda"]
        self.busquedas = self.errores = self.errores_seguidos = 0
        self.desactivado: str | None = ERROR_IMPORT_AIRBNB if self.activo else "desactivado en config.yaml"
        self.cache: dict[tuple, list] = {}
        self.centros = Centros()

    @staticmethod
    def enlace_booking(nombre: str, ci: str, co: str, huespedes: int) -> str:
        return (f"https://www.booking.com/searchresults.es.html?ss={quote_plus(nombre)}"
                f"&checkin={ci}&checkout={co}&group_adults={huespedes}&no_rooms=1&order=price")

    def _disponible(self) -> str | None:
        if self.desactivado:
            return f"Airbnb no consultado: {self.desactivado}"
        if self.limite_tiempo is None:
            self.limite_tiempo = time.monotonic() + self.tiempo_max_s
        if self.busquedas >= self.max_busquedas or time.monotonic() > self.limite_tiempo:
            return "límite diario de búsquedas de alojamiento alcanzado"
        return None

    def _buscar_caja(self, lat: float, lon: float, radio: float, ci: str, co: str) -> list[dict]:
        """Anuncios (ya filtrados por valoración) en un cuadrado de `radio` km alrededor de (lat, lon)."""
        clave = (round(lat, 3), round(lon, 3), radio, ci, co)
        if clave in self.cache:
            return self.cache[clave]
        motivo = self._disponible()
        if motivo:
            raise RuntimeError(motivo)
        self.busquedas += 1
        n = max(1, noches(ci, co))
        dlat = radio / 111.0
        dlon = radio / (111.0 * max(0.2, math.cos(math.radians(lat))))
        zoom = 15 if radio <= 2 else 13 if radio <= 6 else 11
        try:
            def consulta():
                return pyairbnb.search_first_page(
                    check_in=ci, check_out=co, ne_lat=lat + dlat, ne_long=lon + dlon,
                    sw_lat=lat - dlat, sw_long=lon - dlon, zoom_value=zoom, price_min=0, price_max=0,
                    adults=self.huespedes, currency=self.moneda, language="en", timeout=30)
            crudos = con_timeout(consulta, 60) or []
            self.errores_seguidos = 0
        except Exception as e:
            self.errores += 1
            self.errores_seguidos += 1
            log(f"  ! Airbnb {ci}->{co}: {type(e).__name__}: {e}"[:220])
            if self.errores_seguidos >= 5:
                self.desactivado = f"5 errores seguidos (último: {type(e).__name__}: {e})"[:250]
                log("  !! Airbnb desactivado: " + self.desactivado)
            raise
        finally:
            time.sleep(1.0)
        opciones = []
        for r in crudos:
            try:
                total = _precio_total(r, n)
                rating = r.get("rating") or {}
                val = float(rating.get("value") or 0)
                resenas = int(str(rating.get("reviewCount") or "0").replace(".", "").replace(",", "") or 0)
                rid = r.get("room_id")
                coord = r.get("coordinates") or {}
                la = float(coord.get("latitude") or 0) or None
                lo = float(coord.get("longitud") or coord.get("longitude") or 0) or None
                if total <= 0 or not rid or val < self.val_min or resenas < self.res_min:
                    continue
                opciones.append({
                    "id": rid,
                    "nombre": (r.get("name") or r.get("title") or "Alojamiento")[:70],
                    "tipo": (r.get("title") or "")[:60],
                    "precio_total": round(total, 0), "precio_noche": round(total / n, 0),
                    "valoracion": val, "resenas": resenas, "lat": la, "lon": lo,
                    "enlace": (f"https://www.airbnb.es/rooms/{rid}?check_in={ci}&check_out={co}"
                               f"&adults={self.huespedes}"),
                })
            except Exception:
                continue
        self.cache[clave] = opciones
        return opciones

    def buscar(self, nombre: str, lat, lon, check_in: str, check_out: str,
               aeropuerto: tuple | None = None, centro_real: bool = True) -> dict:
        """Busca en el centro, en las afueras y (si está lejos) junto al aeropuerto.
        Devuelve {"centro": [...], "baratas": [...], "ahorro": ..., "opciones": [todas], ...}."""
        ci, co = check_in[:10], check_out[:10]
        n = max(1, noches(ci, co))
        base = {"lugar": nombre, "check_in": ci, "check_out": co, "noches": n, "opciones": [],
                "centro": [], "baratas": [], "centro_real": centro_real,
                "booking": self.enlace_booking(nombre, ci, co, self.huespedes),
                "airbnb_busqueda": (f"https://www.airbnb.es/s/{quote_plus(nombre)}/homes?checkin={ci}"
                                    f"&checkout={co}&adults={self.huespedes}"),
                "nota": ""}
        if lat is None or lon is None:
            base["nota"] = "sin coordenadas para buscar alojamiento"
            return base
        lat, lon = float(lat), float(lon)
        todas: dict = {}
        notas = []

        def añadir(lista, zona):
            for o in lista:
                if o["id"] in todas:
                    continue
                d = distancia_km(o["lat"], o["lon"], lat, lon) if o["lat"] and o["lon"] else float("nan")
                m, modo, eur_tr = tiempo_al_centro(d)
                todas[o["id"]] = dict(o, km_centro=round(d, 1) if d == d else None, zona=zona,
                                      min_centro=m, modo_centro=modo, eur_trayecto=eur_tr)

        zonas = [("centro", self.r_centro), ("afueras", self.r_afueras)]
        if aeropuerto and aeropuerto[0] is not None:
            if distancia_km(aeropuerto[0], aeropuerto[1], lat, lon) > self.r_afueras:
                zonas.append(("aeropuerto", self.r_aerop))
        for zona, radio in zonas:
            try:
                if zona == "aeropuerto":
                    añadir(self._buscar_caja(float(aeropuerto[0]), float(aeropuerto[1]), radio, ci, co), zona)
                else:
                    añadir(self._buscar_caja(lat, lon, radio, ci, co), zona)
            except Exception as e:
                notas.append(f"{zona}: {str(e)[:80] or type(e).__name__}")
                if self.desactivado or "límite" in str(e):
                    break
        lista = sorted(todas.values(), key=lambda o: o["precio_total"])
        lim = self.r_centro * 1.4
        centro = [o for o in lista if o["km_centro"] is not None and o["km_centro"] <= lim]
        fuera = [o for o in lista if o not in centro]
        base["centro"] = centro[: self.n_centro]
        ref = base["centro"][0]["precio_total"] if base["centro"] else None
        candidatas = [o for o in fuera if ref is None or o["precio_total"] < ref]
        # Variedad: la más barata de las afueras y la más barata junto al aeropuerto, y luego el resto
        baratas = []
        for z in ("afueras", "aeropuerto"):
            o = next((x for x in candidatas if x["zona"] == z), None)
            if o:
                baratas.append(o)
        baratas += [o for o in candidatas if o not in baratas]
        baratas = sorted(baratas[: self.n_baratas], key=lambda o: o["precio_total"])
        for o in baratas:
            if ref is not None:
                ahorro = ref - o["precio_total"]
                transporte = 2 * self.huespedes * n * o["eur_trayecto"]  # ida y vuelta al centro cada día
                o["ahorro"] = round(ahorro, 0)
                o["coste_transporte"] = round(transporte, 0)
                o["ahorro_neto"] = round(ahorro - transporte, 0)
        base["baratas"] = baratas
        base["opciones"] = sorted(base["centro"] + baratas, key=lambda o: o["precio_total"])
        if not base["opciones"]:
            base["nota"] = "; ".join(notas) or "Airbnb no devolvió alojamientos que cumplan la valoración mínima"
        elif notas:
            base["nota"] = "; ".join(notas)
        return base


# ---------------------------------------------------------------- campañas
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
    """Busca alojamiento real (centro, afueras y aeropuerto) para las mejores opciones."""
    # 1) Escapadas próximas y 2) fines de semana desde enero (por orden de interés)
    for clave in ("sorpresa_top", "enero_top"):
        for o in res.get(clave) or []:
            nombre, la, lo, real = aloj.centros.get(o["destino"], o.get("ciudad", ""))
            o["ciudad_centro"] = nombre
            c = coords_aeropuerto(o["destino"])
            log(f"Alojamiento {clave} {nombre} {o['salida_ida'][:10]}->{o['salida_vuelta'][:10]}")
            o["alojamiento"] = aloj.buscar(nombre, la, lo, o["salida_ida"][:10], o["salida_vuelta"][:10],
                                           aeropuerto=(c[0], c[1]) if c else None, centro_real=real)
    # 3) Esquí: para la opción más barata por día de cada zona (centro del pueblo + alrededores)
    for z in res.get("esqui") or []:
        if not z["opciones"]:
            continue
        op = min(z["opciones"], key=lambda o: o["total_vuelos"] / o["dias"])
        b = z["base"]
        log(f"Alojamiento esquí {b['nombre']} {op['ida']}->{op['vuelta']}")
        z["alojamiento"] = aloj.buscar(b["nombre"], b.get("lat"), b.get("lon"), op["ida"], op["vuelta"])
        z["alojamiento"]["para_opcion"] = f"{op['ida']}_{op['vuelta']}"


def acceso_bilbao(cfg: dict, iata: str) -> dict:
    """Coste (€/persona/trayecto) y tiempo (min) estimados de Bilbao al aeropuerto `iata`."""
    tabla = cfg.get("acceso_desde_bilbao") or {}
    d = tabla.get(iata) or {}
    try:
        return {"eur": float(d.get("eur", 15)), "min": int(d.get("min", 120)),
                "como": str(d.get("como", "transporte público [a confirmar]"))}
    except Exception:
        return {"eur": 15.0, "min": 120, "como": "transporte público [a confirmar]"}


def enriquecer_oferta(cfg: dict, o: dict) -> dict:
    """Añade coste/tiempo de acceso desde Bilbao y el coste generalizado por persona."""
    acc = acceso_bilbao(cfg, o["origen"])
    vt = float(cfg.get("valor_tiempo_eur_hora", 15))
    o = dict(o)
    o["acceso"] = acc
    o["coste_real_pp"] = round(o["precio_ida_vuelta"] + 2 * acc["eur"], 2)
    o["coste_gen_pp"] = round(o["coste_real_pp"] + 2 * acc["min"] / 60 * vt, 2)
    c = coords_aeropuerto(o["destino"])
    o.setdefault("pais_codigo", c[3] if c else "")
    o.setdefault("alojamiento", None)
    return o


def seleccionar_sorpresa(sorpresa: list[dict], umbral: float, maximo: int, cfg: dict | None = None,
                         por_mes: int = 0) -> list[dict]:
    """Ofertas bajo el umbral, una por destino (o `por_mes` destinos por cada mes), ordenadas por
    coste generalizado (vuelo + llegar al aeropuerto desde Bilbao + tiempo)."""
    cfg = cfg or DEFECTO
    candidatas = [enriquecer_oferta(cfg, o) for o in sorpresa if o["precio_ida_vuelta"] < umbral]
    candidatas.sort(key=lambda o: (o["coste_gen_pp"], o["precio_ida_vuelta"]))
    if por_mes:
        # Por meses, dando prioridad a destinos que aún no han salido en meses anteriores (variedad)
        meses: dict[str, list[dict]] = {}
        for o in candidatas:
            meses.setdefault(o["salida_ida"][:7], []).append(o)
        usados, top = set(), []
        for mes in sorted(meses):
            elegidos, dests = [], set()
            for pasada in (0, 1):
                for o in meses[mes]:
                    if len(elegidos) >= por_mes:
                        break
                    if o["destino"] in dests or (pasada == 0 and o["destino"] in usados):
                        continue
                    elegidos.append(o)
                    dests.add(o["destino"])
            usados |= dests
            top += sorted(elegidos, key=lambda o: o["coste_gen_pp"])
        return top[: max(maximo, 0) or len(top)]
    vistos, top = set(), []
    for o in candidatas:
        if o["destino"] in vistos:
            continue
        vistos.add(o["destino"])
        top.append(o)
        if len(top) >= maximo:
            break
    return top


def campana_google_findes(cfg: dict, g: "Google", ventanas: dict) -> dict[str, list[dict]]:
    """Fines de semana (vie→dom) desde BIO con cualquier aerolínea vía Google Flights, a una lista de
    destinos. Cada día revisa pocos fines de semana distintos (rota), para no gastar el presupuesto."""
    gf = cfg.get("google_findes") or {}
    res: dict[str, list[dict]] = {"sorpresa": [], "enero": []}
    if not gf.get("activo", True) or g.desactivado:
        return res
    origen = str(gf.get("origen") or "BIO").upper()
    destinos = [str(d).upper() for d in gf.get("destinos") or [] if str(d).strip()]
    destinos = [d for d in destinos if not destino_excluido(cfg, d)]
    n_dia = max(1, int(gf.get("findes_por_dia", 2)))
    dias_ida = dias_semana(cfg["sorpresa"].get("dias_salida")) or {4}
    dur = int(cfg["sorpresa"].get("duracion_min_dias", 2))
    # Fines de semana de cada ventana
    findes: dict[str, list[date]] = {}
    for campana, (desde, hasta) in ventanas.items():
        d, lista = desde, []
        while d <= hasta:
            if d.weekday() in dias_ida:
                lista.append(d)
            d += timedelta(days=1)
        findes[campana] = lista
    # Reparto: la mitad para próximos y la mitad para desde enero (rotando cada día)
    plan = []
    cupos = {"sorpresa": (n_dia + 1) // 2, "enero": n_dia // 2} if len(findes) > 1 else {k: n_dia for k in findes}
    for campana, lista in findes.items():
        if not lista:
            continue
        k = hoy().toordinal() % len(lista)
        for i in range(min(cupos.get(campana, 1), len(lista))):
            plan.append((campana, lista[(k + i) % len(lista)]))
    for campana, vie in plan:
        dom = vie + timedelta(days=dur)
        log(f"Google fin de semana {origen} {vie} → {dom} ({len(destinos)} destinos)")
        for dest in destinos:
            if g.desactivado or g.agotado:
                break
            try:
                iv = g.ida_vuelta(origen, [dest], vie.isoformat(), dom.isoformat())
            except Exception as e:
                log(f"  ! google finde {dest}: {e}")
                continue
            if not iv:
                continue
            nombre = CENTROS.get(dest, (None,))[0]
            c = coords_aeropuerto(dest)
            res[campana].append({
                "origen": origen, "destino": dest, "ciudad": nombre or (c[2] if c else dest),
                "pais": c[3] if c else "",
                "salida_ida": iv.ida.salida, "llegada_ida": iv.ida.llegada,
                "salida_vuelta": iv.vuelta.salida, "llegada_vuelta": iv.vuelta.llegada,
                "precio_ida_vuelta": round(iv.precio, 2),
                "aerolineas": iv.ida.aerolineas if iv.ida.aerolineas == iv.vuelta.aerolineas
                else f"{iv.ida.aerolineas} / {iv.vuelta.aerolineas}",
                "escalas": max(iv.ida.escalas or 0, iv.vuelta.escalas or 0),
                "enlace": iv.ida.enlace, "enlace_vuelta": iv.vuelta.enlace, "fuente": "Google Flights",
            })
    return res


def comparar_con_bilbao(g: "Google", ofertas: list[dict], cfg: dict) -> None:
    """Para cada oferta que no sale de BIO, mira en Google Flights el mismo viaje desde Bilbao (BIO):
    otras aerolíneas (Vueling, Volotea, Iberia…) y sin tener que ir a otro aeropuerto."""
    for o in ofertas:
        if o["origen"] == "BIO" or g.desactivado or g.agotado:
            continue
        try:
            iv = g.ida_vuelta("BIO", [o["destino"]], o["salida_ida"][:10], o["salida_vuelta"][:10])
        except Exception as e:
            log(f"  ! comparación BIO {o['destino']}: {e}")
            iv = None
        if not iv:
            continue
        d = iv.a_dict()
        acc_bio = 2 * acceso_bilbao(cfg, "BIO")["eur"]  # bus al aeropuerto de Bilbao, ida y vuelta
        d["coste_real_pp"] = round(d["total"] + acc_bio, 2)
        d["mejor_que_oferta"] = d["coste_real_pp"] < o.get("coste_real_pp", 1e9)
        o["alternativa_bio"] = d


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


def detectar_bajadas_ryanair(previo: list[dict], ofertas: list[dict], pct: float) -> list[dict]:
    """Fines de semana de Ryanair que hoy están ≥ pct % más baratos que su mínimo de días anteriores."""
    minimos: dict[tuple, float] = {}
    hoy_iso = hoy().isoformat()
    for r in previo:
        if r.get("campana") not in ("sorpresa", "enero") or r.get("fecha_consulta", "") >= hoy_iso:
            continue
        try:
            k = (r["origen"], r["destino"], r["fecha"])
            p = float(r["precio"])
        except (KeyError, ValueError, TypeError):
            continue
        minimos[k] = min(p, minimos.get(k, p))
    mejor_hoy: dict[tuple, dict] = {}
    for o in ofertas:
        k = (o["origen"], o["destino"], o["salida_ida"][:10])
        if k not in mejor_hoy or o["precio_ida_vuelta"] < mejor_hoy[k]["precio_ida_vuelta"]:
            mejor_hoy[k] = o
    bajadas = []
    for k, o in mejor_hoy.items():
        antes = minimos.get(k)
        p = o["precio_ida_vuelta"]
        if antes and antes > 0 and p < antes * (1 - pct / 100):
            bajadas.append({"origen": k[0], "destino": k[1], "fecha": k[2], "precio_hoy": p,
                            "minimo_anterior": antes, "bajada_pct": round(100 * (antes - p) / antes, 1),
                            "salida": o["salida_ida"], "aerolineas": f"{o.get('aerolineas', 'Ryanair')} i/v (vie→dom)",
                            "enlace": o["enlace"]})
    return bajadas


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


def minutos_txt(m) -> str:
    try:
        m = int(m)
    except Exception:
        return "?"
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d}" if m % 60 else f"{m // 60} h"


def linea_aloj(o: dict, ref_centro: float | None) -> str:
    dist = ""
    if o.get("km_centro") is not None:
        dist = (f" · a {str(o['km_centro']).replace('.', ',')} km del centro "
                f"(~{minutos_txt(o['min_centro'])} {o['modo_centro']} [est.])")
    ahorro = ""
    if o.get("ahorro") is not None and ref_centro is not None:
        ahorro = f" · **ahorras {eur(o['ahorro'])}** frente al centro"
        if o.get("coste_transporte"):
            if o.get("ahorro_neto", 0) > 0:
                ahorro += (f" (≈{eur(o['ahorro_neto'])} netos tras ~{eur(o['coste_transporte'])} de transporte "
                           f"ida/vuelta al centro cada día [est.])")
            else:
                ahorro += (f" ⚠️ pero ir y volver al centro cada día (~{eur(o['coste_transporte'])} [est.]) "
                           f"se come el ahorro")
    return (f"**{eur(o['precio_total'])} total** ({eur(o['precio_noche'])}/noche) · {o['nombre']} · "
            f"★ {o['valoracion']:.2f} ({o['resenas']} reseñas){dist}{ahorro} · [Airbnb]({o['enlace']}) ✅ precio real")


def bloque_alojamiento(a: dict | None, sangria: str = "") -> list[str]:
    if not a:
        return []
    s = sangria
    ref_txt = "" if a.get("centro_real", True) else " (centro aproximado)"
    out = [f"{s}- 🏠 **Alojamiento 2 personas · {a['lugar']}{ref_txt}** "
           f"({a['noches']} noches, {fecha_corta(a['check_in'])} → {fecha_corta(a['check_out'])}):"]
    centro, baratas = a.get("centro") or [], a.get("baratas") or []
    if not centro and not baratas and a.get("opciones"):  # formato antiguo
        baratas = a["opciones"]
    ref = centro[0]["precio_total"] if centro else None
    if centro:
        out.append(f"{s}  - 🏙️ **En el centro:**")
        out += [f"{s}    - {linea_aloj(o, None)}" for o in centro]
    else:
        out.append(f"{s}  - 🏙️ En el centro: sin opciones con buena valoración hoy")
    if baratas:
        etiqueta = "Más barato, fuera del centro" if centro else "Opciones más baratas"
        out.append(f"{s}  - 💸 **{etiqueta}:**")
        for o in baratas:
            zona = {"aeropuerto": "junto al aeropuerto · ", "afueras": "afueras · "}.get(o.get("zona"), "")
            out.append(f"{s}    - {zona}{linea_aloj(o, ref)}")
    if not centro and not baratas:
        out.append(f"{s}  - Sin precio verificado: {a.get('nota') or 'sin resultados'}")
    out.append(f"{s}  - Comparar: [Booking con estas fechas]({a['booking']}) · "
               f"[más pisos en Airbnb]({a['airbnb_busqueda']})")
    return out


def aloj_mas_barato(a: dict | None) -> tuple[float | None, float | None]:
    """(precio más barato en el centro, precio más barato en total)."""
    if not a:
        return None, None
    c = [o["precio_total"] for o in a.get("centro") or []]
    t = [o["precio_total"] for o in (a.get("centro") or []) + (a.get("baratas") or [])] or \
        [o["precio_total"] for o in a.get("opciones") or []]
    return (min(c) if c else None), (min(t) if t else None)


def tarjeta_oferta(o: dict, num: str = "") -> list[str]:
    n = noches(o["salida_ida"], o["salida_vuelta"])
    acc = o.get("acceso") or {}
    ciudad = o.get("ciudad_centro") or o["ciudad"]
    out = ["", f"### {num}{bandera(o.get('pais_codigo', ''))} {ciudad} ({o['destino']}) — "
               f"{eur(o['precio_ida_vuelta'])} ida y vuelta por persona"]
    aer = o.get("aerolineas") or "Ryanair"
    esc = o.get("escalas") or 0
    out.append(f"- ✈️ Ida: {fecha_corta(o['salida_ida'])} · {o['origen']} {hora(o['salida_ida'])} → "
               f"{o['destino']} {hora(o['llegada_ida'])} ({aer}{', ' + str(esc) + ' escala' if esc else ''})")
    out.append(f"- ✈️ Vuelta: {fecha_corta(o['salida_vuelta'])} · {o['destino']} {hora(o['salida_vuelta'])} → "
               f"{o['origen']} {hora(o['llegada_vuelta'])}")
    if o.get("fuente") == "Google Flights":
        out.append(f"- 🗓️ {n} noches · [ver ida en Google Flights]({o['enlace']}) · "
                   f"[ver vuelta]({o.get('enlace_vuelta') or o['enlace']})")
    else:
        out.append(f"- 🗓️ {n} noches · [ver vuelo en Ryanair]({o['enlace']})")
    if acc:
        out.append(f"- 🚌 Llegar al aeropuerto ({o['origen']}) desde Bilbao: ~{minutos_txt(acc['min'])} y "
                   f"~{eur(acc['eur'])} por persona y trayecto · {acc['como']} [est.]")
    alt = o.get("alternativa_bio")
    if alt:
        veredicto = "✅ sale MEJOR desde Bilbao" if alt.get("mejor_que_oferta") else "sale peor que la oferta"
        out.append(f"- 🔁 Mismo viaje desde Bilbao (BIO) en Google Flights: **{eur(alt['total'])}** por persona "
                   f"({alt['aerolineas']}, ida {hora(alt['salida'])}, vuelta {hora(alt['vuelta']['salida'])}) → "
                   f"{veredicto} [ver]({alt['enlace']})")
    a = o.get("alojamiento")
    out += bloque_alojamiento(a)
    c_min, t_min = aloj_mas_barato(a)
    vuelos2 = 2 * o["precio_ida_vuelta"]
    acceso2 = 4 * float(acc.get("eur", 0))
    if t_min is not None:
        partes = []
        if c_min is not None:
            partes.append(f"durmiendo en el centro **{eur(vuelos2 + acceso2 + c_min)}** "
                          f"({eur((vuelos2 + acceso2 + c_min) / 2)}/persona)")
        if c_min is None or t_min < c_min:
            partes.append(f"con el alojamiento más barato **{eur(vuelos2 + acceso2 + t_min)}** "
                          f"({eur((vuelos2 + acceso2 + t_min) / 2)}/persona)")
        out.append(f"- 💶 **Total 2 personas** (vuelos {eur(vuelos2)} + bus al aeropuerto ~{eur(acceso2)} + "
                   f"alojamiento): " + " · ".join(partes))
    else:
        out.append(f"- 💶 Vuelos 2 personas: {eur(vuelos2)} + bus al aeropuerto ~{eur(acceso2)} (alojamiento sin precio)")
    return out


def total_pp(o: dict) -> float:
    c_min, t_min = aloj_mas_barato(o.get("alojamiento"))
    acc = (o.get("acceso") or {}).get("eur", 0)
    base = o["precio_ida_vuelta"] + 2 * acc
    return base + (t_min / 2 if t_min is not None else 0)


MESES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
         "octubre", "noviembre", "diciembre"]


def informe_md(res: dict) -> str:
    out = [f"# Vigilancia de vuelos — {res['fecha_consulta']}", ""]
    out.append(f"Google Flights: {res.get('peticiones_google', 0)} consultas "
               f"({res.get('exitos_google', 0)} con vuelos, {res.get('sin_vuelos_google', 0)} sin vuelos, "
               f"{res.get('errores_google', 0)} errores, {res.get('rutas_saltadas', 0)} rutas saltadas). "
               f"Fines de semana (Ryanair + Google desde BIO): {len(res.get('sorpresa', []))} tarifas próximas + "
               f"{len(res.get('enero', []))} desde enero. "
               f"Airbnb: {res.get('busquedas_airbnb', 0)} búsquedas.")
    out.append("Vuelos: precio **por persona**, tarifa básica (sin maleta de cabina grande); horas locales. "
               "Alojamiento: precio **total para 2 personas** con tasas de Airbnb, consultado hoy para esas fechas "
               "exactas; distancias y tiempos al centro son estimados. Sin coche: todo en tren/bus. "
               "Solo fines de semana (viernes → domingo) y destinos fuera de España.")
    out.append("")
    if res.get("avisos"):
        out.append("## ⚠️ Avisos de la ejecución")
        out += [f"- {a}" for a in res["avisos"]]
        out.append("")

    sorp = res.get("sorpresa_top") or []
    ene = res.get("enero_top") or []
    esq = res.get("esqui") or []

    # ---------- Resumen
    out.append("## 🏆 Lo mejor de hoy")
    if sorp:
        b = min(sorp, key=total_pp)
        out.append(f"- 🎁 Escapada próxima: {b.get('ciudad_centro') or b['ciudad']} · "
                   f"{fecha_corta(b['salida_ida'])} → {fecha_corta(b['salida_vuelta'])} · "
                   f"~{eur(total_pp(b))} por persona con vuelo, bus y alojamiento")
    if ene:
        b = min(ene, key=total_pp)
        out.append(f"- 📅 Desde enero: {b.get('ciudad_centro') or b['ciudad']} · "
                   f"{fecha_corta(b['salida_ida'])} → {fecha_corta(b['salida_vuelta'])} · "
                   f"~{eur(total_pp(b))} por persona con vuelo, bus y alojamiento")
    zonas_ok = [z for z in esq if z["opciones"]]
    if zonas_ok:
        z = min(zonas_ok, key=lambda z: min(o["total_vuelos"] / o["dias"] for o in z["opciones"]))
        op = min(z["opciones"], key=lambda o: o["total_vuelos"] / o["dias"])
        out.append(f"- ⛷️ Esquí: {z['zona']} · {op['dias']} días {fecha_corta(op['ida'])} → "
                   f"{fecha_corta(op['vuelta'])} · vuelos {eur(op['total_vuelos'])}")
    if not (sorp or ene or zonas_ok):
        out.append("- Nada destacable hoy.")
    out.append("")

    # ---------- Escapadas próximas
    umbral = res.get("umbral", 150)
    out.append(f"## 🎁 Escapadas próximas de fin de semana (viernes → domingo) desde Bilbao y alrededores "
               f"(Europa y norte de África; Ryanair + otras aerolíneas desde BIO; ida y vuelta < {eur(umbral)}; "
               f"ordenadas por coste real incluyendo llegar al aeropuerto)")
    if not sorp:
        out.append("- Sin ofertas bajo el umbral hoy (o Ryanair no respondió; ver avisos).")
    medallas = ["🥇 ", "🥈 ", "🥉 "]
    for k, o in enumerate(sorp):
        out += tarjeta_oferta(o, medallas[k] if k < 3 else "")
    out.append("")

    # ---------- Desde enero
    if res.get("enero_rango"):
        r = res["enero_rango"]
        out.append(f"## 📅 Fines de semana a partir de enero (viernes → domingo, {fecha_corta(r[0])} – "
                   f"{fecha_corta(r[1])}) — los mejores de cada mes")
        if not ene:
            out.append("- Sin ofertas bajo el umbral para esas fechas hoy (o Ryanair no respondió; ver avisos).")
        mes_actual = None
        for o in ene:
            mes = o["salida_ida"][:7]
            if mes != mes_actual:
                mes_actual = mes
                try:
                    out.append("")
                    out.append(f"### 🗓️ {MESES[int(mes[5:7])].capitalize()} {mes[:4]}")
                except Exception:
                    pass
            out += [x.replace("### ", "#### ", 1) if x.startswith("### ") else x for x in tarjeta_oferta(o)]
        out.append("")

    # ---------- Esquí
    sal = res.get("esqui_salidas") or []
    rango = f"salidas entre {fecha_corta(sal[0])} y {fecha_corta(sal[-1])}" if sal else "sin fechas en temporada"
    out.append(f"## ⛷️ Esquí — viajes de {res.get('esqui_dias', '3-6')} días ({rango})")
    out.append("Mejor opción de vuelos para cada duración. B = Bilbao, A = Stuttgart. "
               "Cuando A va en tren, el tren lo calcula Claude (DB/SBB/ÖBB).")
    esq_ord = sorted(esq, key=lambda z: min([o["total_vuelos"] / o["dias"] for o in z["opciones"]] or [1e9]))
    if not zonas_ok:
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
            out += bloque_alojamiento(z["alojamiento"])
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
                 "esqui_dias": f"{t['dias_min']}-{t['dias_max']}", "esqui": [], "esqui_salidas": [],
                 "sorpresa": [], "sorpresa_top": [], "enero": [], "enero_top": [], "enero_rango": None,
                 "bajadas": []}
    umbral = float(res["umbral"])
    origenes_b = cfg["viajeros"]["B"]["aeropuertos"]
    ventanas = ventanas_campanas(cfg)

    # 1) Ryanair (rápido): escapadas próximas y fines de semana desde enero
    for campana, clave in (("sorpresa", "sorpresa"), ("enero", "enero")):
        if campana not in ventanas:
            continue
        desde, hasta = ventanas[campana]
        if campana == "enero":
            res["enero_rango"] = [desde.isoformat(), hasta.isoformat()]
        try:
            log(f"Ryanair {campana}: {desde} → {hasta}")
            res[clave], fallos = ryanair_sorpresa(cfg, origenes_b, desde, hasta)
            if fallos:
                res["avisos"].append(f"Ryanair ({campana}) falló para: " + "; ".join(fallos))
        except Exception as e:
            traceback.print_exc()
            res["avisos"].append(f"Fallo en ofertas Ryanair ({campana}): {type(e).__name__}: {e}")
    if "sorpresa" in ventanas and not res["sorpresa"] and not res["enero"]:
        res["avisos"].append("Ryanair no devolvió tarifas de fin de semana (¿caído o cambio en su API?).")

    # 1b) Otras aerolíneas desde Bilbao (Google Flights), fines de semana rotando cada día
    try:
        extra = campana_google_findes(cfg, g, ventanas)
        res["sorpresa"] += extra.get("sorpresa", [])
        res["enero"] += extra.get("enero", [])
    except Exception as e:
        traceback.print_exc()
        res["avisos"].append(f"Fallo buscando fines de semana en Google Flights: {type(e).__name__}: {e}")
    try:
        res["sorpresa_top"] = seleccionar_sorpresa(
            res["sorpresa"], umbral, int(cfg["sorpresa"].get("max_ofertas_informe", 6)), cfg)
        e = cfg.get("desde_enero") or {}
        res["enero_top"] = seleccionar_sorpresa(res["enero"], umbral, 0, cfg,
                                                por_mes=int(e.get("mejores_por_mes", 2)))
    except Exception as e:
        traceback.print_exc()
        res["avisos"].append(f"Fallo seleccionando ofertas: {type(e).__name__}: {e}")

    # 2) Google Flights: ¿sale mejor el mismo viaje desde Bilbao (BIO) con otra aerolínea?
    try:
        if (cfg.get("desde_enero") or {}).get("comparar_con_bilbao", True):
            comparar_con_bilbao(g, res["sorpresa_top"] + res["enero_top"], cfg)
    except Exception as e:
        traceback.print_exc()
        res["avisos"].append(f"Fallo comparando con Bilbao: {type(e).__name__}: {e}")

    # 3) Esquí con Google Flights
    try:
        res["esqui"], res["esqui_salidas"] = campana_esqui(cfg, g)
    except Exception as e:
        traceback.print_exc()
        res["avisos"].append(f"Fallo en campaña esquí: {type(e).__name__}: {e}")

    # 4) Alojamiento real (centro, afueras y aeropuerto) para las mejores opciones
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
        res["bajadas"] += detectar_bajadas_ryanair(previo, res["sorpresa"] + res["enero"],
                                                   float(cfg["bajada_relevante_pct"]))
        res["bajadas"].sort(key=lambda b: -b["bajada_pct"])
    except Exception as e:
        res["avisos"].append(f"No se pudieron calcular bajadas: {e}")

    dia = hoy().isoformat()
    nuevas = [{"fecha_consulta": dia, "campana": "google", "viajero": "", "tramo": "ida", **asdict(t)}
              for tramos in g.cache.values() for t in tramos[:1]]
    nuevas += [{"fecha_consulta": dia, "campana": "sorpresa", "viajero": "B", "origen": o["origen"],
                "destino": o["destino"], "fecha": o["salida_ida"][:10], "tramo": "ida-vuelta",
                "precio": o["precio_ida_vuelta"], "aerolineas": o.get("aerolineas", "Ryanair"),
                "salida": o["salida_ida"],
                "llegada": o["llegada_ida"], "escalas": 0, "duracion_vuelo_min": "",
                "fuente": o.get("fuente", "Ryanair"), "enlace": o["enlace"]} for o in res["sorpresa"] + res["enero"]
               if o["precio_ida_vuelta"] < umbral * 1.5]  # solo lo relevante: el histórico no crece sin fin
    try:
        guardar_historico(previo, nuevas, int(cfg["historico_dias"]))
    except Exception as e:
        res["avisos"].append(f"No se pudo guardar el histórico: {e}")

    escribir_salidas(res)
    log(f"Hecho: Google {g.peticiones} consultas ({g.exitos} con vuelos, {g.sin_vuelos} sin vuelos, "
        f"{g.errores} errores, {g.saltadas} saltadas); Ryanair {len(res['sorpresa']) + len(res['enero'])} tarifas; "
        f"Airbnb {aloj.busquedas} búsquedas ({aloj.errores} errores); {len(res['bajadas'])} bajadas.")
    if g.exitos == 0 and not res["sorpresa"] and not res["enero"]:
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
                              "esqui": [], "sorpresa": [], "sorpresa_top": [], "enero": [],
                              "enero_top": [], "bajadas": []})
        except Exception:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
