# Vigilancia de viajes

Script diario (GitHub Actions, la noche anterior hacia las 00:17 / 23:17 en invierno, con respaldo a las 03:47) que consulta **Google Flights**, las **tarifas públicas de Ryanair** y **Airbnb** para:

- **Escapadas próximas**: fines de semana (viernes → domingo) de las próximas semanas desde Bilbao y alrededores, solo destinos fuera de España, ordenadas por coste real (vuelo + llegar al aeropuerto desde Bilbao + tiempo).
- **Fines de semana desde enero**: lo mismo de enero a junio, con los mejores destinos de cada mes (sin repetir siempre los mismos).
- **Comparación con Bilbao (BIO)**: si una oferta sale de Biarritz, Santander, Vitoria…, se mira en Google Flights el mismo viaje desde BIO con otras aerolíneas.
- **Esquí**: viajes de 3 a 6 días entre diciembre y marzo, con destinos a los que se llega **sin coche**.
- **Alojamiento**: precio real en Airbnb para 2 personas y fechas exactas, en tres zonas: **centro**, **afueras** y **junto al aeropuerto**, con distancia y tiempo estimado al centro y cuánto se ahorra frente al centro.

Los resultados se guardan en `data/`:

| Archivo | Contenido |
|---|---|
| `data/ultimo.md` | Resumen legible de hoy (lo lee la tarea diaria de Claude) |
| `data/ultimo.json` | Lo mismo, estructurado |
| `data/historico.csv` | Precios de los últimos 60 días → permite detectar bajadas |
| `data/sin_ruta.json` | Rutas sin vuelos (por día de la semana), para no repetir consultas inútiles |
| `data/centros.json` | Centros de ciudad geolocalizados (OpenStreetMap) para destinos nuevos |

## Montaje (5 minutos)

1. En GitHub, crea un repositorio nuevo **público** llamado `vigilancia-viajes` (público para que la tarea de Claude pueda leer `data/ultimo.md`; no contiene datos personales ni claves).
2. Sube todos estos archivos (incluida la carpeta oculta `.github`). Lo más fácil: *Add file → Upload files* y arrastrar el contenido del zip.
3. *Settings → Actions → General → Workflow permissions* → marca **Read and write permissions** → *Save*.
4. Pestaña **Actions** → *Vigilancia diaria de vuelos* → **Run workflow** para la primera prueba. Tarda ~40–60 min.
5. Comprueba que aparece `data/ultimo.md`.

## Ajustes

Todo se cambia en `config.yaml`: aeropuertos, zonas de esquí, meses de los fines de semana desde enero, coste de llegar a cada aeropuerto, radios de búsqueda de alojamiento, umbral de aviso, nº de consultas diarias.

## Limitaciones

- Precios por persona, sumando dos billetes de ida (en low-cost coincide con la ida y vuelta; en aerolíneas tradicionales la i/v a veces es más barata).
- Incluye solo la tarifa básica: revisa el suplemento de maleta de cabina.
- `fast-flights` lee Google Flights por su protocolo interno; si Google lo cambia o bloquea, el workflow falla y GitHub te avisa por email. Normalmente se arregla con `pip install -U fast-flights`.
- Skyscanner, Kayak y Booking no se consultan (bloquean scrapers); el informe trae enlaces de Booking con las fechas puestas.
- Tiempos y costes de transporte al centro y a los aeropuertos son estimaciones.
