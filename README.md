# iPad Desk Monitor

Tablero de escritorio de **1280×720 (16:9)** pensado para quedarse prendido en un
iPad junto al escritorio. Lo sirve una máquina Linux de la casa (Python estándar,
sin dependencias) y el iPad solo abre una URL.

![Pantalla con el aviso del NWS](docs/demo-alert.png)

![Pantalla con el termostato](docs/demo-nest.png)

> Las capturas están hechas con `--mock` (datos de ejemplo). No traen datos
> reales de nadie.

## Qué trae

- **Reloj grande + fecha** del día, en la zona horaria configurada.
- **Avisos oficiales del NWS** (api.weather.gov) en una tarjeta que **rota cada 5
  minutos con el termostato Nest**, sincronizada al reloj. Sin alerta activa, la
  tarjeta se queda en el Nest y esconde los puntos de paginación.
- **Clima ahora**: temperatura en °C, icono, sensación térmica, descripción,
  humedad y viento (Open-Meteo, sin API key).
- **Próximas 4 horas** con icono, probabilidad de lluvia y temperatura.
- **Calendario vertical** a toda la altura de la columna, leyendo los **feeds
  iCal privados** de cada calendario (sin OAuth): días completos, barra y
  etiqueta con el color de su calendario, y un separador fino entre días.
- **Termostato Nest** por Google Device Access (SDM): ambiente, set point,
  humedad, modo y estado.
- **Fondos que rotan** cada 10 minutos con crossfade y un velo oscuro para que el
  texto se lea sobre cualquier imagen.
- Paleta oscura única, tipografía Inter, cero librerías de frontend.

## Arranque rápido

```bash
git clone https://github.com/ElChacoVeloz/ipad-monitor.git
cd ipad-monitor
cp config.example.json config.json     # ajusta coordenadas, TZ y puerto
python3 server.py --port 8772          # http://localhost:8772
```

Para verlo sin credenciales: `python3 server.py --mock --port 8772`.
Para depurar el JSON: `python3 server.py --once`.

## Cómo está hecho

- `server.py` — solo biblioteca estándar. Sirve `web/` y publica **un único**
  `GET /api/data` que junta clima, calendario, avisos, Nest y la lista de fondos
  con cache por fuente y en paralelo (`ThreadPoolExecutor`). Si una fuente falla,
  esa tarjeta muestra el motivo en una línea y el resto sigue funcionando.
- `web/` — `index.html`, `style.css`, `app.js` sin librerías. El escenario mide
  **1280 de ancho fijo** y el alto se adapta al de la pantalla (800–1020 px de
  diseño) para que en un iPad 4:3 no queden franjas muertas; la tipografía escala
  con ese alto en `calc(Npx * var(--fs))` y `ensureFit()` la baja si algo no
  cabe, antes que recortar.
- `ics_lib.py` — parser iCal con expansión de `RRULE`/`EXDATE`, `webcal://` →
  `https://` y normalización de cada ocurrencia.
- `office-calendars.py` — alta de calendarios por **dirección secreta iCal**,
  con validación del feed, rechazo de direcciones repetidas y paleta de colores.

### El calendario no aprieta: elige

Se piden los días que diga `display.calendarDays`, pero solo se pintan los que
caben **completos** y con aire. Si no cabe uno, se va entero y el encabezado lo
dice (`Oct 22 – Oct 25 · +3 days (26, 27, 28)`). Nunca se corta un evento de un
día que sí se muestra; solo si **un** día solo no cupiera se recortan sus
últimos eventos con un `+N` en su encabezado.

## Configuración

`config.json` (no se versiona; parte de `config.example.json`):

| Clave | Para qué |
|---|---|
| `location` | etiqueta, lat/lon, `timezone`, unidades (`celsius`) |
| `display.calendarDays` | ventana que se le pide al calendario (7 = semana) |
| `display.hourlyCount` / `dailyCount` | horas y días de pronóstico |
| `wallpapers.dir` / `rotateMinutes` | carpeta de fondos y rotación (10 min) |
| `calendar.configFile` | ruta del `calendars.json` (0600, con las direcciones secretas) |
| `nest` | `projectId`, `deviceId` y archivo del refresh token (SDM) |
| `alerts` | User-Agent que exige api.weather.gov |

## Calendarios (iCal privado, sin OAuth)

Cada calendario se lee de su **dirección secreta en formato iCal**
(`…/private-XXXXXXXX/basic.ics`), la misma que usa el plugin de calendario de
Omarchy. Ventajas: el tablero es de solo lectura por diseño, no hay OAuth y no
existe el `invalid_grant` semanal del cliente en modo *Testing*.

```bash
python3 office-calendars.py list       # direcciones enmascaradas
python3 office-calendars.py add        # pide nombre y dirección (getpass)
python3 office-calendars.py check      # baja cada feed y cuenta eventos
python3 office-calendars.py color --name "Team" --color "#38bdf8"
python3 office-calendars.py remove --name "Team"
```

La dirección buena es la que dice **“Dirección secreta en formato iCal”** del
calendario **seleccionado** en Google Calendar; la pública y la de la vista web
no sirven. `add` rechaza una dirección ya usada: dos entradas iguales dejan una
semana incompleta sin dar error. Después de un alta, reinicia el servicio (o
espera el TTL de 5 minutos).

## Nest (opcional)

Con `nest.enabled: true` y `mode: "sdm"`, el servidor refresca el token y lee
`enterprises/{projectId}/devices/{deviceId}`. Necesita un cliente OAuth de tipo
*Web application* registrado en Device Access y el scope `sdm.service`; el
`deviceId` sale de la API, no de la consola. Si falta, la tarjeta dice
“Nest offline” y el resto del tablero sigue igual.

## Despliegue (Linux, sin sudo)

```bash
mkdir -p ~/.config/systemd/user
cp deploy/ipad-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ipad-monitor
loginctl enable-linger "$USER"     # arranca al encender, sin iniciar sesión
```

## El iPad (una sola vez)

1. Safari → `http://<ip-del-servidor>:8772/`.
2. Compartir → **Añadir a pantalla de inicio** (abre a pantalla completa).
3. **Ajustes → Pantalla y brillo → Bloqueo automático → Nunca** y déjalo
   conectado a corriente.
4. **Ajustes → Accesibilidad → Acceso guiado**: triple clic al botón lateral y
   queda clavado en el tablero.

La página pide **Screen Wake Lock** mientras está visible (iOS 16.4+), se recarga
sola cuando cambia el código del servidor y se auto-recarga si lleva 5 minutos sin
poder hablar con él. Para revisar una tarjeta concreta sin esperar la rotación:
`?slot=alert` o `?slot=nest`; para fijar un fondo, `?wp=2`.

## Lo que **no** está en este repo

- `config.json` y `calendars.json` (coordenadas e **direcciones iCal secretas**).
- Los fondos de pantalla originales que usa el dueño: aquí van tres degradados
  de ejemplo en `wallpapers/opt/`.
- Capturas reales: las de `docs/` se generaron con `--mock`.

Para rearmarlo: `cp config.example.json config.json`, pon tus coordenadas y
corre `office-calendars.py add`.

## Licencia

MIT — ver [LICENSE](LICENSE). La tipografía **Inter** se incluye bajo SIL Open
Font License 1.1 (`web/fonts/OFL.txt`).
