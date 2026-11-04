"""Búsqueda puntual de alojamiento barato para un GRUPO (no envía emails ni toca la vigilancia diaria).

Uso (lo lanza el workflow "Alojamiento para grupo"):
    python grupo.py [--viernes 2026-11-06] [--minimo 6] [--maximo 9]

Busca en varias webs:
  - Airbnb (pisos, hostels, habitaciones, hoteles): precio real, muy fiable.
  - Booking.com (hoteles, hostels, apartamentos): se intenta; si Booking bloquea al robot, se avisa.
  - Albergues juveniles de la zona (Jugendherbergen / Swiss Youth Hostels): lista con enlaces.
En:
  - Noche del viernes: ciudad de Constanza (Konstanz), a ~20 min en coche del centro
    (con --zona-viernes bodensee: todo el lago de Constanza, como antes).
  - Noche del sábado: Zúrich, a ~20 min en coche del centro.
Para el número mínimo y máximo de personas, ordenado por precio por persona.
Resultado: data/grupo.md (legible) y data/grupo.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from urllib.parse import quote_plus

import buscador as b

# Puntos de búsqueda (lat, lon) y radio en km de cada caja; distancia máxima al punto de referencia.
ZONAS = {
    "bodensee": {
        "titulo": "Viernes · Lago de Constanza (Bodensee)",
        # Pueblos y ciudades alrededor del lago: la distancia se mide al más cercano
        "referencias": [
            ("Konstanz", 47.6603, 9.1758), ("Überlingen", 47.7676, 9.1590), ("Meersburg", 47.6943, 9.2710),
            ("Friedrichshafen", 47.6542, 9.4790), ("Lindau", 47.5460, 9.6840), ("Bregenz", 47.5031, 9.7471),
            ("Rorschach", 47.4780, 9.4900), ("Romanshorn", 47.5656, 9.3786), ("Kreuzlingen", 47.6450, 9.1750),
            ("Radolfzell", 47.7370, 8.9690),
        ],
        "radio_caja_km": 9,
        "max_km": 16,        # ~20 min en coche
        "booking": "Konstanz",
        "booking_ciudades": ["Konstanz", "Kreuzlingen", "Friedrichshafen", "Lindau", "Bregenz", "Überlingen",
                             "Meersburg", "Radolfzell", "Romanshorn"],
        "albergues": ["Jugendherberge Konstanz", "Jugendherberge Kreuzlingen", "Jugendherberge Friedrichshafen",
                      "Jugendherberge Lindau", "Jugendherberge Überlingen", "Jugendherberge Meersburg",
                      "Jugendherberge Radolfzell", "Hostel Bregenz", "Hostel Konstanz"],
    },
    "konstanz": {
        "titulo": "Viernes · Constanza (ciudad de Konstanz)",
        # La distancia se mide al CENTRO de Konstanz (casco antiguo / estación)
        "referencias": [("Konstanz centro", 47.6603, 9.1758)],
        # Cajas para cubrir Konstanz y lo que está a ~20 min en coche por carretera:
        # Kreuzlingen/Tägerwilen (Suiza), Allensbach/Reichenau, Litzelstetten/Dingelsdorf y la orilla suiza
        "cajas": [(47.6603, 9.1758), (47.6400, 9.1300), (47.7150, 9.0800), (47.7150, 9.1700),
                  (47.6200, 9.2300)],
        "radio_caja_km": 6,
        "max_km": 13,        # ~20 min en coche desde el centro
        # Lo que está cerca en línea recta pero AL OTRO LADO DEL LAGO (solo en ferry o rodeando el lago):
        # Meersburg / Hagnau / Uhldingen y Überlingen / Sipplingen  -> se descarta
        "excluir": [(47.665, 90, 9.200, 180), (47.755, 90, 9.130, 180), (47.775, 90, 9.050, 180)],
        "booking": "Konstanz",
        "booking_ciudades": ["Konstanz", "Kreuzlingen", "Allensbach", "Reichenau", "Tägerwilen"],
        "albergues": ["Jugendherberge Konstanz Otto-Moericke-Turm", "Jugendherberge Kreuzlingen",
                      "Hostel Konstanz", "Jugendherberge Radolfzell"],
    },
    "zurich": {
        "titulo": "Sábado · Zúrich",
        "referencias": [("Zúrich centro", 47.3769, 8.5417)],
        # cajas extra para cubrir las afueras (más baratas) a ~20 min en coche
        "cajas": [(47.3769, 8.5417), (47.4017, 8.4001), (47.4500, 8.5800), (47.3471, 8.7209),
                  (47.2953, 8.5649), (47.4300, 8.4700), (47.3600, 8.4300)],
        "radio_caja_km": 8,
        "max_km": 18,        # ~20 min en coche
        "booking": "Zürich",
        "booking_ciudades": ["Zürich", "Dietikon", "Kloten", "Uster", "Thalwil", "Schlieren", "Wallisellen"],
        "albergues": ["Youth Hostel Zürich", "Hostel Zürich", "Backpackers Zürich", "Jugendherberge Rapperswil-Jona"],
    },
}


def km_min_coche(km: float) -> int:
    """Minutos ESTIMADOS en coche (carreteras regionales/autovía)."""
    return int(round((4 + km * 1.0) / 5.0)) * 5


def buscar_zona(zona: dict, personas: int, ci: str, co: str, cfg: dict) -> tuple[list[dict], list[str]]:
    aloj = b.Alojamiento(cfg)
    aloj.huespedes = personas
    aloj.val_min = 4.0          # estudiantes: priorizamos precio (pero sin sitios mal valorados)
    aloj.res_min = 3
    aloj.max_busquedas = 60
    aloj.tiempo_max_s = 20 * 60
    cajas = zona.get("cajas") or [(la, lo) for _, la, lo in zona["referencias"]]
    todos: dict = {}
    avisos = []
    for la, lo in cajas:
        try:
            for o in aloj._buscar_caja(la, lo, zona["radio_caja_km"], ci, co):
                todos.setdefault(o["id"], o)
        except Exception as e:
            avisos.append(f"{personas} pers. caja ({la:.2f},{lo:.2f}): {type(e).__name__}: {str(e)[:100]}")
            if aloj.desactivado:
                break
    res = []
    for o in todos.values():
        if not (o.get("lat") and o.get("lon")):
            continue
        # zonas descartadas (lat_min, lat_max, lon_min, lon_max): p. ej. la otra orilla del lago
        if any(a1 <= o["lat"] <= a2 and l1 <= o["lon"] <= l2 for a1, a2, l1, l2 in zona.get("excluir", [])):
            continue
        dist, ref = min((b.distancia_km(o["lat"], o["lon"], la, lo), n) for n, la, lo in zona["referencias"])
        if dist > zona["max_km"]:
            continue
        res.append(dict(o, km=round(dist, 1), cerca_de=ref, min_coche=km_min_coche(dist),
                        por_persona=round(o["precio_total"] / personas, 1), personas=personas))
    res.sort(key=lambda o: (o["precio_total"], -o["valoracion"]))
    return res, avisos


def _cliente_web():
    """Cliente que imita un navegador real (primp); si no está, requests."""
    if getattr(b, "primp", None) is not None:
        for imp in ("chrome_145", "chrome_133", "chrome_131", None):
            try:
                kw = dict(impersonate=imp, impersonate_os="windows") if imp else {}
                return b.primp.Client(cookie_store=True, referer=True, timeout=30, **kw)
            except Exception:
                continue
    return None


def _num(txt: str) -> float | None:
    import re
    t = (txt or "").replace("\xa0", " ")
    m = re.findall(r"\d[\d.,']*", t)
    if not m:
        return None
    v = m[-1].replace("'", "")
    # 1.234,56 / 1,234.56 / 1.234 / 1,234
    if "," in v and "." in v:
        v = v.replace(".", "").replace(",", ".") if v.rfind(",") > v.rfind(".") else v.replace(",", "")
    elif "," in v:
        v = v.replace(",", "") if len(v.split(",")[-1]) == 3 else v.replace(",", ".")
    elif "." in v and len(v.split(".")[-1]) == 3:
        v = v.replace(".", "")
    try:
        return float(v)
    except ValueError:
        return None


def buscar_booking(zona: dict, personas: int, ci: str, co: str) -> tuple[list[dict], list[str]]:
    """Booking.com: lee la página de resultados (ordenada por precio). Si Booking bloquea, lo avisa."""
    try:
        from selectolax.lexbor import LexborHTMLParser as HTMLParser
    except Exception:
        try:
            from selectolax.parser import HTMLParser
        except Exception as e:
            return [], [f"Booking: falta selectolax ({e})"]
    cli = _cliente_web()
    avisos, res, vistos = [], [], set()
    bloqueos = 0
    for ciudad in zona.get("booking_ciudades") or [zona["booking"]]:
        url = (f"https://www.booking.com/searchresults.en-gb.html?ss={quote_plus(ciudad)}&checkin={ci}&checkout={co}"
               f"&group_adults={personas}&no_rooms=1&group_children=0&order=price&selected_currency=EUR")
        try:
            if cli is not None:
                r = b.con_timeout(lambda: cli.get(url, headers={"Accept-Language": "en-GB,en;q=0.9"}), 45)
            else:
                r = b.requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
                                   "Accept-Language": "en-GB,en;q=0.9"})
            html = r.text if hasattr(r, "text") else ""
            codigo = getattr(r, "status_code", 200)
        except Exception as e:
            avisos.append(f"Booking {ciudad}: {type(e).__name__}")
            continue
        arbol = HTMLParser(html or "")
        tarjetas = arbol.css('[data-testid="property-card"]')
        if codigo != 200 or not tarjetas:
            bloqueos += 1
            avisos.append(f"Booking {ciudad}: sin resultados legibles (HTTP {codigo}; posible bloqueo anti-robots)")
            if bloqueos >= 3 and not res:
                avisos.append("Booking bloquea al programa: usa el enlace de Booking del informe")
                break
            time.sleep(2)
            continue
        for t in tarjetas:
            try:
                tit = t.css_first('[data-testid="title"]')
                nombre = tit.text(strip=True) if tit else ""
                a = t.css_first('a[data-testid="title-link"]') or t.css_first("a")
                enlace = (a.attributes.get("href") or "").split("?")[0] if a else ""
                p = t.css_first('[data-testid="price-and-discounted-price"]')
                precio = _num(p.text(strip=True)) if p else None
                if not nombre or not precio or nombre in vistos:
                    continue
                vistos.add(nombre)
                rs = t.css_first('[data-testid="review-score"]')
                val = _num(rs.text(strip=True).split()[0]) if rs and rs.text(strip=True) else None
                dist = t.css_first('[data-testid="distance"]')
                tipo = t.css_first('[data-testid="recommended-units"]')
                res.append({"fuente": "Booking", "nombre": nombre[:70], "tipo": (tipo.text(strip=True)[:60] if tipo else ""),
                            "precio_total": round(precio, 0), "por_persona": round(precio / personas, 1),
                            "valoracion_txt": (f"{val:.1f}/10" if val else "—"),
                            "zona_txt": f"{ciudad}" + (f" · {dist.text(strip=True)[:40]}" if dist else ""),
                            "enlace": (enlace + f"?checkin={ci}&checkout={co}&group_adults={personas}&no_rooms=1")
                            if enlace.startswith("http") else url, "personas": personas})
            except Exception:
                continue
        time.sleep(2.5)
    res.sort(key=lambda o: o["precio_total"])
    return res, avisos


def booking(ciudad: str, ci: str, co: str, n: int) -> str:
    return (f"https://www.booking.com/searchresults.es.html?ss={quote_plus(ciudad)}&checkin={ci}&checkout={co}"
            f"&group_adults={n}&no_rooms=1&order=price")


def main() -> int:
    hoy = date.today()
    prox_vie = hoy + timedelta(days=(4 - hoy.weekday()) % 7)
    ap = argparse.ArgumentParser()
    ap.add_argument("--viernes", default=prox_vie.isoformat())
    ap.add_argument("--minimo", type=int, default=6)
    ap.add_argument("--maximo", type=int, default=9)
    ap.add_argument("--zona-viernes", default="konstanz", choices=["konstanz", "bodensee"],
                    help="konstanz = ciudad de Constanza (por defecto); bodensee = todo el lago")
    a = ap.parse_args()
    vie = date.fromisoformat(a.viernes)
    sab, dom = vie + timedelta(days=1), vie + timedelta(days=2)
    tamanos = sorted({max(1, a.minimo), max(1, a.maximo)})
    cfg, avisos_cfg = b.cargar_config()
    noches_plan = [(a.zona_viernes, vie.isoformat(), sab.isoformat()), ("zurich", sab.isoformat(), dom.isoformat())]

    salida = {"generado": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "zonas": []}
    md = [f"# Alojamiento para el grupo — {salida['generado']}", "",
          f"Precios reales de Airbnb (pisos, hostels, habitaciones y hoteles) para las fechas exactas, "
          f"para {' y '.join(str(t) for t in tamanos)} personas, ordenados por precio. "
          f"Tiempos en coche **estimados** por distancia. Valoración mínima 4,0.", ""]
    for clave, ci, co in noches_plan:
        z = ZONAS[clave]
        bloque = {"zona": clave, "titulo": z["titulo"], "check_in": ci, "check_out": co, "grupos": []}
        md += [f"## {z['titulo']} — {b.fecha_corta(ci)} → {b.fecha_corta(co)} (1 noche, ~20 min en coche como máximo)", ""]
        for n in tamanos:
            b.log(f"{z['titulo']}: {n} personas")
            lista, avisos = buscar_zona(z, n, ci, co, cfg)
            b.log(f"  Booking: {n} personas")
            lista_bk, avisos_bk = buscar_booking(z, n, ci, co)
            bloque["grupos"].append({"personas": n, "opciones": lista[:30], "booking": lista_bk[:30],
                                     "avisos": avisos + avisos_bk})
            md += [f"### 👥 {n} personas — {len(lista)} en Airbnb · {len(lista_bk)} en Booking", ""]
            # Lo más barato de todas las webs juntas
            comb = [dict(fuente="Airbnb", nombre=o["nombre"], precio_total=o["precio_total"], por_persona=o["por_persona"],
                         zona_txt=f"a {str(o['km']).replace('.', ',')} km de {o['cerca_de']} (~{o['min_coche']} min en coche)",
                         valoracion_txt=f"{o['valoracion']:.2f}★", enlace=o["enlace"]) for o in lista] + lista_bk
            comb.sort(key=lambda o: o["precio_total"])
            if comb:
                md += [f"#### 🏆 Lo más barato de todas las webs ({n} personas)", "",
                       "| # | Web | Precio total | Por persona | Alojamiento | Valoración | Dónde | Enlace |",
                       "|---|---|---|---|---|---|---|---|"]
                for i, o in enumerate(comb[:12], 1):
                    md.append(f"| {i} | {o['fuente']} | **{b.eur(o['precio_total'])}** | {b.eur(o['por_persona'])} | "
                              f"{o['nombre'][:50].replace('|', '/')} | {o['valoracion_txt']} | {o['zona_txt'].replace('|', '/')} | "
                              f"[ver]({o['enlace']}) |")
                md.append("")
            md += [f"#### Airbnb ({n} personas)", ""]
            if not lista:
                md.append(f"- Sin resultados en Airbnb ({'; '.join(avisos)[:200] or 'nada que cumpla'}).")
            else:
                md += ["| # | Precio total | Por persona | Alojamiento | ★ | Zona | En coche | Enlace |",
                       "|---|---|---|---|---|---|---|---|"]
                for i, o in enumerate(lista[:20], 1):
                    md.append(f"| {i} | **{b.eur(o['precio_total'])}** | {b.eur(o['por_persona'])} | "
                              f"{o['nombre'][:55].replace('|', '/')} ({o['tipo'][:30].replace('|', '/')}) | "
                              f"{o['valoracion']:.2f} ({o['resenas']}) | a {str(o['km']).replace('.', ',')} km de {o['cerca_de']} | "
                              f"~{o['min_coche']} min | [Airbnb]({o['enlace']}) |")
            md += ["", f"#### Booking ({n} personas)", ""]
            if lista_bk:
                md += ["| # | Precio total | Por persona | Alojamiento | Valoración | Dónde | Enlace |",
                       "|---|---|---|---|---|---|---|"]
                for i, o in enumerate(lista_bk[:15], 1):
                    md.append(f"| {i} | **{b.eur(o['precio_total'])}** | {b.eur(o['por_persona'])} | "
                              f"{o['nombre'][:50].replace('|', '/')} {('(' + o['tipo'][:30] + ')') if o['tipo'] else ''} | "
                              f"{o['valoracion_txt']} | {o['zona_txt'].replace('|', '/')} | [Booking]({o['enlace']}) |")
            else:
                md.append("- Booking no ha dejado leer sus resultados (bloqueo anti-robots): mira el enlace de abajo.")
            if avisos or avisos_bk:
                md.append(f"\n> Avisos: {'; '.join(avisos + avisos_bk)[:500]}")
            md += ["", f"Comparar en [Booking (ordenado por precio)]({booking(z['booking'], ci, co, n)}) · "
                       f"[Hostelworld](https://www.hostelworld.com/) (buscar {z['booking']}, {ci} → {co}, {n} personas)", ""]
        md += [f"### 🛏️ Albergues juveniles y hostels de la zona (suelen ser lo más barato para grupos; precio a consultar)", ""]
        for nombre in z.get("albergues") or []:
            md.append(f"- [{nombre}](https://www.google.com/search?q={quote_plus(nombre + ' ' + ci)})")
        md.append("")
        salida["zonas"].append(bloque)

    b.DATA.mkdir(parents=True, exist_ok=True)
    (b.DATA / "grupo.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (b.DATA / "grupo.json").write_text(json.dumps(salida, ensure_ascii=False, indent=1), encoding="utf-8")
    total = sum(len(g["opciones"]) + len(g.get("booking") or []) for zb in salida["zonas"] for g in zb["grupos"])
    b.log(f"Hecho: {total} alojamientos guardados en data/grupo.md")
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
