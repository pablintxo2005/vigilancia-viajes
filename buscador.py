"""Vigilancia diaria de vuelos: esquí, puente de diciembre y ofertas sorpresa.

Fuentes:
  - Google Flights (vía librería fast-flights, sin API key)
  - Tarifas públicas de Ryanair (ofertas sorpresa desde Bilbao)

Salidas (carpeta data/):
  - historico.csv : todas las consultas, día a día (permite detectar bajadas)
  - ultimo.json   : resultados de la ejecución de hoy (estructurado)
  - ultimo.md     : resumen legible (lo lee la tarea diaria de Claude)
"""

from __future__ import annotations

import csv
import json
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from fast_flights import FlightQuery, Passengers, create_query, get_flights

RAIZ = Path(__file__).parent
DATA = RAIZ / "data"
HISTORICO = DATA / "historico.csv"
CAMPOS_HIST = [
    "fecha_consulta", "campana", "viajero", "origen", "destino", "fecha",
    "tramo", "precio", "aerolineas", "salida", "llegada", "escalas",
    "duracion_vuelo_min", "fuente", "enlace",
]


# ---------------------------------------------------------------- utilidades
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


def hoy() -> date:
    return datetime.now(timezone.utc).date()


def fmt_dt(d) -> str:
    """SimpleDatetime de fast-flights -> 'YYYY-MM-DD HH:MM'."""
    y, m, dd = d.date
    hh, mm = d.time
    return f"{y:04d}-{m:02d}-{dd:02d} {hh:02d}:{mm:02d}"


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------ Google Flights
class Google:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.presupuesto = cfg["max_peticiones_google"]
        self.peticiones = 0
        self.errores = 0
        self.cache: dict[tuple, list[Tramo]] = {}

    def tramo(self, origen: str, destino: str, fecha: str) -> list[Tramo]:
        """Devuelve hasta 3 opciones (más baratas) de un vuelo de ida."""
        clave = (origen, destino, fecha)
        if clave in self.cache:
            return self.cache[clave]
        if origen == destino or self.peticiones >= self.presupuesto:
            return []
        self.peticiones += 1
        q = create_query(
            flights=[FlightQuery(date=fecha, from_airport=origen, to_airport=destino)],
            trip="one-way",
            passengers=Passengers(adults=1),
            language=self.cfg["idioma"],
            currency=self.cfg["moneda"],
            max_stops=self.cfg["max_escalas"],
            hide_separate_and_self_transfer=True,
        )
        resultado: list[Tramo] = []
        try:
            vuelos = get_flights(q)
            for f in vuelos:
                if not f.flights or not f.price:
                    continue
                resultado.append(Tramo(
                    origen=origen, destino=destino, fecha=fecha,
                    precio=float(f.price),
                    aerolineas=", ".join(f.airlines),
                    salida=fmt_dt(f.flights[0].departure),
                    llegada=fmt_dt(f.flights[-1].arrival),
                    escalas=len(f.flights) - 1,
                    duracion_vuelo_min=sum(s.duration for s in f.flights),
                    fuente="Google Flights",
                    enlace=q.url(),
                ))
        except Exception as e:  # sin vuelos, bloqueo o cambio de formato
            if "FlightsNotFound" not in type(e).__name__:
                self.errores += 1
                log(f"  ! Google {origen}->{destino} {fecha}: {type(e).__name__}: {e}"[:200])
        resultado.sort(key=lambda t: t.precio)
        resultado = resultado[:3]
        self.cache[clave] = resultado
        time.sleep(self.cfg["pausa_entre_peticiones_s"])
        return resultado

    def ida_vuelta(self, origen: str, destinos: list[str], f_ida: str, f_vuelta: str) -> IdaVuelta | None:
        """Mejor ida+vuelta desde `origen` a cualquiera de `destinos` (puede volar
        a un aeropuerto de la zona y volver desde otro)."""
        idas = [t for d in destinos for t in self.tramo(origen, d, f_ida)[:1]]
        vueltas = [t for d in destinos for t in self.tramo(d, origen, f_vuelta)[:1]]
        if not idas or not vueltas:
            return None
        return IdaVuelta(min(idas, key=lambda t: t.precio), min(vueltas, key=lambda t: t.precio))


# ------------------------------------------------------------------ Ryanair
def ryanair_sorpresa(cfg: dict, origenes: list[str]) -> list[dict]:
    s = cfg["sorpresa"]
    ofertas = []
    inicio = hoy() + timedelta(days=3)
    fin = hoy() + timedelta(weeks=s["semanas_vista"])
    url = "https://services-api.ryanair.com/farfnd/v4/roundTripFares"
    for o in origenes:
        params = {
            "departureAirportIataCode": o,
            "outboundDepartureDateFrom": inicio.isoformat(),
            "outboundDepartureDateTo": fin.isoformat(),
            "inboundDepartureDateFrom": inicio.isoformat(),
            "inboundDepartureDateTo": (fin + timedelta(days=s["duracion_max_dias"])).isoformat(),
            "durationFrom": s["duracion_min_dias"],
            "durationTo": s["duracion_max_dias"],
            "currency": cfg["moneda"],
            "market": "es-es",
            "language": "es",
        }
        try:
            r = requests.get(url, params=params, timeout=30,
                             headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            if r.status_code != 200:
                log(f"  ! Ryanair {o}: HTTP {r.status_code}")
                continue
            for fare in r.json().get("fares", []):
                ida, vuelta = fare.get("outbound", {}), fare.get("inbound", {})
                total = (fare.get("summary", {}).get("price") or {}).get("value")
                if total is None:
                    continue
                dest = ida.get("arrivalAirport", {})
                f_ida = (ida.get("departureDate") or "")[:10]
                f_vta = (vuelta.get("departureDate") or "")[:10]
                ofertas.append({
                    "origen": o,
                    "destino": dest.get("iataCode"),
                    "ciudad": (dest.get("city") or {}).get("name") or dest.get("name"),
                    "pais": dest.get("countryName"),
                    "salida_ida": (ida.get("departureDate") or "").replace("T", " ")[:16],
                    "llegada_ida": (ida.get("arrivalDate") or "").replace("T", " ")[:16],
                    "salida_vuelta": (vuelta.get("departureDate") or "").replace("T", " ")[:16],
                    "llegada_vuelta": (vuelta.get("arrivalDate") or "").replace("T", " ")[:16],
                    "precio_ida_vuelta": float(total),
                    "enlace": (
                        "https://www.ryanair.com/es/es/trip/flights/select?adults=1"
                        f"&dateOut={f_ida}&dateIn={f_vta}&originIata={o}"
                        f"&destinationIata={dest.get('iataCode')}&isReturn=true"
                    ),
                })
        except Exception as e:
            log(f"  ! Ryanair {o}: {type(e).__name__}: {e}"[:200])
    ofertas.sort(key=lambda x: x["precio_ida_vuelta"])
    return ofertas


# ---------------------------------------------------------------- campañas
def fines_de_semana(cfg: dict) -> list[tuple[str, str, str]]:
    """Lista de fines de semana largos: (etiqueta, viernes, lunes) dentro de temporada."""
    t = cfg["temporada_esqui"]
    d = max(date.fromisoformat(t["desde"]), hoy() + timedelta(days=2))
    fin = date.fromisoformat(t["hasta"])
    while d.weekday() != 4:  # viernes
        d += timedelta(days=1)
    lista = []
    while d <= fin:
        lista.append((f"finde {d:%d/%m}", d.isoformat(), (d + timedelta(days=3)).isoformat()))
        d += timedelta(days=7)
    return lista


def campana_esqui(cfg: dict, g: Google) -> list[dict]:
    findes = fines_de_semana(cfg)
    if not findes:
        return []
    n = cfg["temporada_esqui"]["fines_de_semana_por_dia"]
    inicio = (hoy().toordinal() * n) % len(findes)
    hoy_findes = [findes[(inicio + i) % len(findes)] for i in range(n)]
    origenes_A = cfg["viajeros"]["A"].get("principales_esqui", cfg["viajeros"]["A"]["aeropuertos"])
    origenes_B = cfg["viajeros"]["B"].get("principales_esqui", cfg["viajeros"]["B"]["aeropuertos"])
    resultados = []
    for etiqueta, viernes, lunes in hoy_findes:
        jueves = (date.fromisoformat(viernes) - timedelta(days=1)).isoformat()
        domingo = (date.fromisoformat(lunes) - timedelta(days=1)).isoformat()
        for ida, vuelta in [(jueves, domingo), (viernes, lunes)]:
            for z in cfg["destinos_esqui"]:
                log(f"Esquí {z['zona']} {ida}->{vuelta}")
                mejor_B = min(
                    (iv for o in origenes_B if (iv := g.ida_vuelta(o, z["aeropuertos"], ida, vuelta))),
                    key=lambda iv: iv.precio, default=None)
                mejor_A = None
                if z.get("vuela_A"):
                    mejor_A = min(
                        (iv for o in origenes_A if (iv := g.ida_vuelta(o, z["aeropuertos"], ida, vuelta))),
                        key=lambda iv: iv.precio, default=None)
                resultados.append({
                    "zona": z["zona"], "finde": etiqueta, "ida": ida, "vuelta": vuelta,
                    "B": asdict(mejor_B.ida) | {"vuelta": asdict(mejor_B.vuelta), "total": mejor_B.precio} if mejor_B else None,
                    "A": asdict(mejor_A.ida) | {"vuelta": asdict(mejor_A.vuelta), "total": mejor_A.precio} if mejor_A else None,
                    "A_por_tierra": not z.get("vuela_A"),
                })
    return resultados


def campana_puente(cfg: dict, g: Google) -> list[dict]:
    p = cfg["puente"]
    if hoy() > date.fromisoformat(p["hasta"]):
        return []
    resultados = []
    for destino in p["aeropuertos_destino"]:
        fila = {"destino": destino, "combinaciones": {}}
        for ida, vuelta in p["combinaciones"]:
            log(f"Puente {destino} {ida}->{vuelta}")
            mejor = min(
                (iv for o in cfg["viajeros"]["B"]["aeropuertos"]
                 if (iv := g.ida_vuelta(o, [destino], ida, vuelta))),
                key=lambda iv: iv.precio, default=None)
            fila["combinaciones"][f"{ida[8:10]}-{vuelta[8:10]}"] = (
                asdict(mejor.ida) | {"vuelta": asdict(mejor.vuelta), "total": mejor.precio}
                if mejor else None)
        resultados.append(fila)
    return resultados


# --------------------------------------------------------- histórico/bajadas
def leer_historico() -> list[dict]:
    if not HISTORICO.exists():
        return []
    with HISTORICO.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def guardar_historico(filas: list[dict]) -> None:
    nuevo = not HISTORICO.exists()
    with HISTORICO.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS_HIST)
        if nuevo:
            w.writeheader()
        w.writerows(filas)


def detectar_bajadas(previo: list[dict], cache: dict, pct: float) -> list[dict]:
    """Compara el mejor precio de hoy por (origen, destino, fecha) con el mínimo
    registrado en días anteriores."""
    minimos: dict[tuple, float] = {}
    for r in previo:
        try:
            k = (r["origen"], r["destino"], r["fecha"])
            p = float(r["precio"])
        except (KeyError, ValueError):
            continue
        minimos[k] = min(p, minimos.get(k, p))
    bajadas = []
    for (o, d, f), tramos in cache.items():
        if not tramos or (o, d, f) not in minimos:
            continue
        hoy_p, antes = tramos[0].precio, minimos[(o, d, f)]
        if hoy_p < antes * (1 - pct / 100):
            bajadas.append({"origen": o, "destino": d, "fecha": f, "precio_hoy": hoy_p,
                            "minimo_anterior": antes,
                            "bajada_pct": round(100 * (antes - hoy_p) / antes, 1),
                            "salida": tramos[0].salida, "enlace": tramos[0].enlace})
    return sorted(bajadas, key=lambda b: -b["bajada_pct"])


# ------------------------------------------------------------------ informe
def eur(x) -> str:
    return "—" if x is None else f"{x:,.0f} €".replace(",", ".")


def linea_iv(d: dict | None) -> str:
    if not d:
        return "sin datos"
    v = d["vuelta"]
    return (f"{d['origen']}→{d['destino']} {d['salida'][5:]} ({d['aerolineas']}, {d['escalas']} esc.) "
            f"/ vuelta {v['origen']}→{v['destino']} {v['salida'][5:]} ({v['aerolineas']}) "
            f"= **{eur(d['total'])}** [ida]({d['enlace']}) [vuelta]({v['enlace']})")


def informe_md(res: dict) -> str:
    out = [f"# Vigilancia de vuelos — {res['fecha_consulta']}",
           f"Consultas Google Flights: {res['peticiones_google']} (errores: {res['errores_google']}). "
           "Precios por persona en €, ida y vuelta sumando dos billetes de ida "
           "(solo equipaje de mano básico; revisar suplemento de maleta de cabina).", ""]

    out.append("## Bajadas de precio respecto a días anteriores")
    if res["bajadas"]:
        for b in res["bajadas"][:15]:
            out.append(f"- {b['origen']}→{b['destino']} {b['fecha']} {b['salida'][11:]}: "
                       f"{eur(b['precio_hoy'])} (antes mín. {eur(b['minimo_anterior'])}, -{b['bajada_pct']} %) "
                       f"[ver]({b['enlace']})")
    else:
        out.append("- Ninguna bajada relevante hoy (o aún no hay histórico).")
    out.append("")

    out.append("## Esquí — fines de semana revisados hoy")
    for r in sorted(res["esqui"], key=lambda r: (r["B"] or {}).get("total", 1e9))[:12]:
        a = "por tierra (calcular tren/coche)" if r["A_por_tierra"] else linea_iv(r["A"])
        out.append(f"- **{r['zona']}** · {r['ida']} → {r['vuelta']}\n"
                   f"  - B (Bilbao): {linea_iv(r['B'])}\n  - A (Stuttgart): {a}")
    out.append("")

    if res["puente"]:
        out.append("## Puente de diciembre (viajero B desde Bilbao)")
        out.append("| Destino | 4–8 dic | 4–9 dic | 5–9 dic |")
        out.append("|---|---|---|---|")
        for fila in res["puente"]:
            c = fila["combinaciones"]
            celdas = []
            for k in ["04-08", "04-09", "05-09"]:
                d = c.get(k)
                celdas.append(f"{eur(d['total'])} ({d['origen']}, {d['salida'][11:]}/{d['vuelta']['salida'][11:]})"
                              if d else "—")
            out.append(f"| {fila['destino']} | " + " | ".join(celdas) + " |")
        out.append("")

    out.append(f"## Ofertas sorpresa Ryanair desde Bilbao/alrededores (< {res['umbral']} € i/v)")
    baratas = [o for o in res["sorpresa"] if o["precio_ida_vuelta"] < res["umbral"]]
    if baratas:
        vistos = set()
        for o in baratas:
            clave = (o["origen"], o["destino"])
            if clave in vistos:
                continue
            vistos.add(clave)
            out.append(f"- {o['origen']}→{o['destino']} ({o['ciudad']}, {o['pais']}): "
                       f"**{eur(o['precio_ida_vuelta'])}** · ida {o['salida_ida']} · vuelta {o['salida_vuelta']} "
                       f"[ver]({o['enlace']})")
            if len(vistos) >= 15:
                break
    else:
        out.append("- Sin datos o sin ofertas bajo umbral.")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------- main
def main() -> int:
    cfg = yaml.safe_load((RAIZ / "config.yaml").read_text(encoding="utf-8"))
    DATA.mkdir(exist_ok=True)
    previo = leer_historico()
    g = Google(cfg)
    fecha_consulta = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    res = {"fecha_consulta": fecha_consulta, "umbral": cfg["umbral_aviso_ida_vuelta_eur"]}
    for nombre, fn in [("puente", campana_puente), ("esqui", campana_esqui)]:
        try:
            res[nombre] = fn(cfg, g)
        except Exception:
            traceback.print_exc()
            res[nombre] = []
    try:
        res["sorpresa"] = ryanair_sorpresa(cfg, cfg["viajeros"]["B"]["aeropuertos"])
    except Exception:
        traceback.print_exc()
        res["sorpresa"] = []

    res["peticiones_google"] = g.peticiones
    res["errores_google"] = g.errores
    res["bajadas"] = detectar_bajadas(previo, g.cache, cfg["bajada_relevante_pct"])

    filas = []
    for tramos in g.cache.values():
        for t in tramos[:1]:
            filas.append({"fecha_consulta": fecha_consulta[:10], "campana": "google",
                          "viajero": "", **asdict(t)})
    for o in res["sorpresa"]:
        filas.append({"fecha_consulta": fecha_consulta[:10], "campana": "sorpresa", "viajero": "B",
                      "origen": o["origen"], "destino": o["destino"], "fecha": o["salida_ida"][:10],
                      "tramo": "ida-vuelta", "precio": o["precio_ida_vuelta"], "aerolineas": "Ryanair",
                      "salida": o["salida_ida"], "llegada": o["llegada_ida"], "escalas": 0,
                      "duracion_vuelo_min": "", "fuente": "Ryanair", "enlace": o["enlace"]})
    for f in filas:
        f.setdefault("tramo", "ida")
    guardar_historico(filas)

    (DATA / "ultimo.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    (DATA / "ultimo.md").write_text(informe_md(res), encoding="utf-8")
    log(f"Hecho: {g.peticiones} consultas Google, {g.errores} errores, "
        f"{len(res['sorpresa'])} tarifas Ryanair, {len(res['bajadas'])} bajadas.")
    # Si Google bloqueó casi todo, marcamos fallo para que GitHub avise por email
    if g.peticiones > 20 and g.errores > 0.8 * g.peticiones:
        log("Demasiados errores en Google Flights: posible bloqueo o cambio de formato.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
