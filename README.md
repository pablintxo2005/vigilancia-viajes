# Vigilancia de viajes

Script diario (GitHub Actions, sobre las 5:05 en verano / 4:05 en invierno) que consulta **Google Flights** y las **tarifas públicas de Ryanair** para:

- **Esquí**: viajes de 3 a 6 días entre diciembre y marzo. Cada día revisa una ventana distinta de 6 días de salida (rota por toda la temporada) y todas las duraciones posibles, desde Bilbao (y Stuttgart cuando tiene sentido volar). Destinos elegidos para llegar **sin coche** (tren/bus).
- **Puente de diciembre**: combinaciones 4–8, 4–9 y 5–9 de diciembre desde los aeropuertos de Bilbao, a 9 destinos fijos + los 5 más baratos que encuentre Ryanair ese día, con el ahorro de salir un día antes / volver un día después.
- **Ofertas sorpresa**: ida y vuelta de 2–4 días desde Bilbao/Santander/Biarritz… en las próximas 10 semanas (Ryanair).
- **Alojamiento**: precio real en Airbnb (pisos, apartamentos y hoteles) para 2 personas y las fechas exactas de las mejores opciones, con enlace a Booking para comparar.

Los resultados se guardan en `data/`:

| Archivo | Contenido |
|---|---|
| `data/ultimo.md` | Resumen legible de hoy (lo lee la tarea diaria de Claude) |
| `data/ultimo.json` | Lo mismo, estructurado |
| `data/historico.csv` | Precios de los últimos 60 días → permite detectar bajadas |
| `data/sin_ruta.json` | Rutas sin vuelos (por día de la semana), para no repetir consultas inútiles |

## Montaje (5 minutos)

1. En GitHub, crea un repositorio nuevo **público** llamado `vigilancia-viajes` (público para que la tarea de Claude pueda leer `data/ultimo.md`; no contiene datos personales ni claves).
2. Sube todos estos archivos (incluida la carpeta oculta `.github`). Lo más fácil: *Add file → Upload files* y arrastrar el contenido del zip.
3. *Settings → Actions → General → Workflow permissions* → marca **Read and write permissions** → *Save*.
4. Pestaña **Actions** → *Vigilancia diaria de vuelos* → **Run workflow** para la primera prueba. Tarda ~40–60 min.
5. Comprueba que aparece `data/ultimo.md`.

## Ajustes

Todo se cambia en `config.yaml`: aeropuertos, zonas de esquí, fechas del puente, umbral de aviso, nº de consultas diarias.

## Limitaciones

- Precios por persona, sumando dos billetes de ida (en low-cost coincide con la ida y vuelta; en aerolíneas tradicionales la i/v a veces es más barata).
- Incluye solo la tarifa básica: revisa el suplemento de maleta de cabina.
- `fast-flights` lee Google Flights por su protocolo interno; si Google lo cambia o bloquea, el workflow falla y GitHub te avisa por email. Normalmente se arregla con `pip install -U fast-flights`.
- Skyscanner, Kayak y Airbnb no se consultan aquí (bloquean scrapers); el alojamiento lo busca la tarea de Claude.
