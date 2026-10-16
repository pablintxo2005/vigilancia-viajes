# Vigilancia de viajes

Script diario (GitHub Actions) que consulta **Google Flights** y las **tarifas públicas de Ryanair** para:

- **Esquí**: fines de semana largos (jue→dom y vie→lun) de diciembre a marzo, desde Bilbao (y Stuttgart cuando tiene sentido volar) a las principales zonas de esquí de Europa. Cada día revisa un fin de semana distinto (rotación).
- **Puente de diciembre**: combinaciones 4–8, 4–9 y 5–9 de diciembre desde los aeropuertos de Bilbao.
- **Ofertas sorpresa**: ida y vuelta de 2–4 días desde Bilbao/Santander/Biarritz… en las próximas 10 semanas (Ryanair).

Los resultados se guardan en `data/`:

| Archivo | Contenido |
|---|---|
| `data/ultimo.md` | Resumen legible de hoy (lo lee la tarea diaria de Claude) |
| `data/ultimo.json` | Lo mismo, estructurado |
| `data/historico.csv` | Todos los precios día a día → permite detectar bajadas |

## Montaje (5 minutos)

1. En GitHub, crea un repositorio nuevo **público** llamado `vigilancia-viajes` (público para que la tarea de Claude pueda leer `data/ultimo.md`; no contiene datos personales ni claves).
2. Sube todos estos archivos (incluida la carpeta oculta `.github`). Lo más fácil: *Add file → Upload files* y arrastrar el contenido del zip.
3. *Settings → Actions → General → Workflow permissions* → marca **Read and write permissions** → *Save*.
4. Pestaña **Actions** → *Vigilancia diaria de vuelos* → **Run workflow** para la primera prueba. Tarda ~15–20 min.
5. Comprueba que aparece `data/ultimo.md`.

## Ajustes

Todo se cambia en `config.yaml`: aeropuertos, zonas de esquí, fechas del puente, umbral de aviso, nº de consultas diarias.

## Limitaciones

- Precios por persona, sumando dos billetes de ida (en low-cost coincide con la ida y vuelta; en aerolíneas tradicionales la i/v a veces es más barata).
- Incluye solo la tarifa básica: revisa el suplemento de maleta de cabina.
- `fast-flights` lee Google Flights por su protocolo interno; si Google lo cambia o bloquea, el workflow falla y GitHub te avisa por email. Normalmente se arregla con `pip install -U fast-flights`.
- Skyscanner, Kayak y Airbnb no se consultan aquí (bloquean scrapers); el alojamiento lo busca la tarea de Claude.
