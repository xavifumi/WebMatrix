# Plataforma de Gestión de Señales y Distribución basada en MediaMTX

**Versión:** 0.2  
**Estado:** Documento de visión funcional (revisado; ver sección 23)  
**Tecnología base:** MediaMTX + FFmpeg + WebApp de gestión

---

# 1. Resumen Ejecutivo

El proyecto consiste en desarrollar una plataforma web para la gestión profesional de señales de vídeo en directo sobre MediaMTX.

La solución permitirá:

- Recibir señales de vídeo mediante SRT, RTMP, RTSP y otros protocolos IP.
- Gestionar una fuente principal y una fuente de respaldo.
- Realizar conmutación automática o manual.
- Grabar la señal de programa.
- Distribuir la señal a múltiples destinos simultáneamente.
- Supervisar el estado técnico en tiempo real.
- Gestionar grabaciones y exportaciones desde una interfaz web.

El objetivo es ofrecer una experiencia similar a Restreamer, pero orientada a entornos broadcast y producción audiovisual profesional.

---

# 2. Objetivos del Proyecto

## Objetivo principal

Construir una plataforma que permita a un operador gestionar eventos de streaming sin necesidad de interactuar directamente con MediaMTX, FFmpeg o configuraciones complejas.

## Beneficios

- Redundancia de señal.
- Operación simplificada.
- Distribución centralizada.
- Grabación integrada.
- Monitorización técnica.
- Escalabilidad.

---

# 3. Arquitectura General

```text
Entrada MAIN
      │
      ▼

┌───────────────┐
│   MediaMTX    │
│  ingest paths │
└───────┬───────┘
        │
        ▼

┌────────────────────┐
│ Motor de Failover  │
│  Main / Backup     │
└─────────┬──────────┘
          │
          ▼

┌────────────────────┐
│      PROGRAM       │
└───┬─────┬─────┬────┘
    │     │     │
    │     │     │
 Grab  CDN  SRT WebRTC
```

---

## Componentes

- **MediaMTX:** ingesta, conversión de protocolos, grabación, reenvío, API de control y métricas.
- **Conmutador (uno por canal):** proceso supervisado que lee `main` y `backup` y publica `program` (sección 7).
- **Backend:** orquesta MediaMTX mediante su API, decide la conmutación, muestrea métricas, contabiliza consumo y gestiona exportaciones.
- **Frontend:** SPA con preview WebRTC y control de todo el sistema.

**Principio clave:** todo lo que cuelga de `program` (grabación, CDNs, pulls, embeds) nunca ve desconexiones, porque `program` lo publica el conmutador y no el encoder.

---

# 4. Estructura de Canales

Cada canal dispondrá de:

```text
canal/main
canal/backup
canal/program
```

## Main

Fuente principal.

## Backup

Fuente de respaldo.

## Program

Salida estable utilizada por:

- Grabaciones.
- CDNs.
- YouTube.
- Facebook.
- WebRTC.
- HLS.
- SRT Pull.

---

# 5. Gestión de Entradas

## Protocolos soportados

### SRT

- Caller
- Listener

### RTMP / RTMPS

- Push

### RTSP

- Push
- Pull

### Fuentes IP

- Encoders hardware
- vMix
- OBS
- MediaMTX remoto

---

## Modos de conexión

- **Listener / Push (esperar):** el servidor espera a que el encoder publique (SRT listener, RTMP, RTSP push).
- **Caller / Pull (conectar):** el servidor se conecta a la fuente (SRT caller, RTSP o RTMP remoto, otro MediaMTX).
- SRT: latencia, passphrase y `streamid` configurables por entrada.
- La entrada backup es **opcional**. Si existe, debe estar conectada de forma permanente (hot standby).
- Los datos de conexión se consultan en un popup «Datos de entrada»; no ocupan espacio en la pantalla principal.

---

## Información mostrada

### Estado

- Online
- Offline
- Warning

### Métricas

- Bitrate
- Pérdida de paquetes (solo SRT; en RTMP/TCP se muestra «no aplica»)
- Retransmisiones y capacidad del enlace (SRT)
- RTT
- Codec
- Resolución
- FPS
- Audio

---

# 6. Sistema de Failover

## Objetivo

Garantizar continuidad de emisión ante pérdida de señal.

---

## Modos

### Automático

La aplicación decide.

### Main Forzado

Siempre utiliza Main.

### Backup Forzado

Siempre utiliza Backup.

### Lock Actual

Mantiene la fuente actual.

> En la interfaz: control segmentado **Auto / Forzar main / Forzar backup** y un interruptor **Lock**.

---

## Cambio automático a Backup

Cuando sucede cualquiera de los siguientes eventos:

- Sin paquetes durante 2 segundos.
- Bitrate inferior al 15%.
- Congelación de vídeo superior a 3 segundos.
- Desconexión de la fuente.

---

## Retorno a Main

Cuando:

- Main permanece estable durante 10 segundos.
- Existe vídeo.
- Existe audio.
- Bitrate correcto.

---

## Protección anti-flapping

- Mínimo 10 segundos entre cambios.
- Máximo 3 cambios por minuto.
- Generación automática de alerta.

---

## Evaluación de salud

Cada fuente se evalúa en ventana deslizante. Detectar congelación, silencio o bitrate bajo exige analizar el vídeo y el audio, no solo las estadísticas del protocolo. Ambas entradas se evalúan de forma continua, también la que está en espera.

---

# 7. Motor de Programa

La plataforma generará una única señal estable:

```text
MAIN
   \
    \

    PROGRAM
    / 
   /

BACKUP
```

Todos los servicios consumirán exclusivamente la señal PROGRAM.

## Implementación del conmutador (punto crítico)

MediaMTX no conmuta entre dos fuentes: cada path admite un único publicador y su función «always-available» solo muestra un segmento de «sin señal» cuando no hay publicador. El conmutador es, por tanto, un componente propio.

| Modo | Funcionamiento | CPU | Resultado |
|---|---|---|---|
| A. Re-encode | Decodifica la fuente activa y reencoda siempre con parámetros fijos (resolución, fps, GOP, audio); inserta slate si no hay fuente | Alta (GPU recomendable) | Sin cortes; +1-2 s de latencia y una generación de pérdida |
| B. Copy | Remux sin reencodar; cambia en un keyframe reescribiendo timestamps | Mínima | Correcto si main y backup tienen ajustes idénticos; si no, puede haber glitch |
| C. Híbrido | A o B configurable por canal | Variable | Recomendado |

Candidatos técnicos: GStreamer (`input-selector`) o FFmpeg con el filtro `streamselect` controlado en caliente. FFmpeg con `-c copy` por sí solo no permite conmutar entradas en vivo. La elección se decide en la Fase 0.

Requisitos: timestamps continuos, parámetros de codec constantes, slate configurable y ambas fuentes activas en todo momento.

---

# 8. Distribución de Señal

## RTMP Push

Destinos compatibles:

- YouTube Live
- Facebook Live
- Vimeo
- Akamai
- Mux
- Wowza
- RTMP personalizados

---

## SRT Push

Distribución profesional hacia:

- Broadcasters
- CDNs privadas
- Centros de producción

---

## SRT Pull

Recepción desde clientes externos.

---

## Protocolos Locales

- WebRTC
- HLS
- RTSP

---

# 9. Sistema de Grabación

## Modos

### Manual

Inicio y parada controladas por operador.

### Automático

Ligado al estado del canal.

---

## Información guardada

- Fecha inicio
- Fecha fin
- Duración
- Tamaño
- Canal
- Source activa
- Estado

---

## Gestión de espacio

- Cuota por canal.
- Retención configurable.
- Alertas de disco.

---

## Formato

Se graba en **fMP4** por defecto: es el formato que permite línea de tiempo, reproducción en el navegador y recorte con el servidor de reproducción de MediaMTX (MPEG-TS no lo permite). Se graba `program`; como opción, también cada entrada por separado.

---

# 10. Biblioteca de Grabaciones

## Funciones

### Reproducción

Player integrado.

### Descarga

Exportación completa.

### Recorte rápido

```bash
ffmpeg -c copy
```

Sin reencodificación.

---

### Recorte preciso

```bash
ffmpeg -c:v libx264
```

Reencodificación con precisión de fotograma.

---

### Línea de tiempo y reproducción

Línea de tiempo diaria por canal, con los huecos visibles. Reproducción en el navegador antes de descargar. Un clic salta a ese momento y arrastrar selecciona un rango.

### Editor de recorte

Marcadores de entrada y salida, campos de tiempo editables y estimación de tamaño. El modo rápido ajusta el corte al keyframe más cercano; el preciso corta al fotograma.

### Exportaciones

Cola de tareas con progreso y descarga al terminar. Opción de guardar el fragmento como nueva grabación.

---

## Historial

Listado de grabaciones filtrable por:

- Canal
- Fecha
- Duración
- Estado

---

# 11. Dashboard Operativo

## Estado Global

Mostrar:

- CPU.
- RAM.
- Disco.
- Red.

---

## Resumen

- Canales activos.
- Canales en backup.
- Grabaciones activas.
- Destinos conectados.
- Alertas.

---

## Eventos recientes

Ejemplo:

```text
09:10 Grabación iniciada
09:45 Salida RTMP conectada
10:02 Main perdida
10:02 Cambio automático a Backup
10:05 Main recuperada
10:15 Retorno automático a Main
```

---

# 12. Monitorización

## Métricas por Canal

### Entradas

- Bitrate actual.
- FPS.
- Audio.
- RTT.
- Jitter.

### Programa

- Fuente activa.
- Estado.
- Tiempo de emisión.

### Salidas

- Estado.
- Reconexiones.
- Throughput.
- Bytes enviados, tiempo activo, último error y log del proceso.

---

# 13. Seguridad

## Roles

### Administrador

Acceso completo.

### Operador

Operación diaria.

### Solo Lectura

Monitorización.

---

## Secretos

Protección mediante:

- AES-256.
- Variables de entorno.
- Tokens API.
- Auditoría.

---

# 14. Modelo de Datos

## Channel

```json
{
  "id": "",
  "name": "",
  "enabled": true
}
```

---

## Input

```json
{
  "role": "main",
  "protocol": "srt",
  "url": ""
}
```

---

## Output

```json
{
  "type": "rtmp",
  "url": ""
}
```

---

## Recording

```json
{
  "id": "",
  "started_at": "",
  "duration": 0
}
```

---

## Entidades adicionales

- **Input:** `mode` (listener|caller), `latency_ms`, `passphrase` (cifrada), `enabled`.
- **Output:** `direction` (push|pull), `enabled`, `status`, `last_error`.
- **Recording:** `channel_id`, `source`, `size_bytes`, `format`, `status`.
- **ExportJob:** `recording_id`, `start`, `end`, `mode` (fast|precise), `status`, `progress`.
- **UsageSample:** `channel_id`, `session_id`, `direction`, `bytes`, `ts` (consolidado por hora, día y mes).
- **Event / Alert:** `channel_id`, `type`, `message`, `ts`.
- **ShareToken:** `channel_id`, `player` (webrtc|hls), `expires_at`, `allowed_domains`.
- **User:** `role`, `2fa`.

---

# 15. API REST

## Canales

```http
GET    /api/channels
POST   /api/channels
PATCH  /api/channels/{id}
DELETE /api/channels/{id}
```

---

## Entradas

```http
GET    /api/inputs
POST   /api/inputs
PATCH  /api/inputs/{id}
```

---

## Salidas

```http
GET    /api/outputs
POST   /api/outputs
PATCH  /api/outputs/{id}
```

---

## Grabaciones

```http
GET    /api/recordings
POST   /api/recordings/start
POST   /api/recordings/stop
```

---

## Control de Conmutación

```http
POST /api/channels/{id}/switch/main
POST /api/channels/{id}/switch/backup
POST /api/channels/{id}/switch/auto
```

---

## Endpoints adicionales

```http
POST  /api/channels/{id}/switch/lock
PATCH /api/outputs/{id}/enabled
GET   /api/recordings/{id}/play
POST  /api/recordings/{id}/exports
GET   /api/exports/{id}
GET   /api/usage?channel=&from=&to=&group=session|month
POST  /api/channels/{id}/share
GET   /api/events
```

---

# 16. Tecnologías Propuestas

## Backend

- FastAPI
- PostgreSQL
- Redis (estado en tiempo real, pub/sub y cola de exportaciones)

## Streaming

- MediaMTX
- FFmpeg
- ffprobe
- GStreamer o FFmpeg `streamselect` (candidatos para el conmutador; decidir en Fase 0)

## Frontend

- React
- TypeScript
- Tailwind

## Monitorización

- Prometheus
- Grafana (opcional, para operaciones; las gráficas de la interfaz las sirve el backend)

## Despliegue

- Docker
- Docker Compose

---

# 17. Roadmap

## Fase 0 — Prueba de concepto

Duración estimada: 1-2 semanas.

### Objetivos

- MediaMTX con dos entradas simuladas (FFmpeg) y un `program` estable.
- Prototipos del conmutador en modo A y modo B.
- Confirmar qué datos devuelve la API de MediaMTX (codec, métricas SRT, grabaciones y reproducción).
- Probar el recorte con el servidor de reproducción frente a FFmpeg.

### Criterio de éxito

Al cortar main, `program` no muestra corte perceptible ni obliga al reproductor a reconectar. Se mide el CPU por canal y el comportamiento en VLC y en un CDN real.

Las estimaciones siguientes deben revisarse al terminar esta fase.

---

## Fase 1 — MVP

Duración estimada: 4-6 semanas.

### Incluye

- Gestión de canales.
- Main / Backup.
- Failover básico.
- Vista previa WebRTC.
- Grabación.
- RTMP Push.
- Eventos.
- Interruptor y edición de salidas.
- Popup de datos de entrada.

---

## Fase 2 — Producción

Duración estimada: 3-4 semanas.

### Incluye

- Detección de freeze.
- Timeline de grabaciones.
- Exportaciones.
- Roles.
- Alertas.
- Históricos.
- Consumo de datos por sesión y mes.
- Compartir y embed.
- Reproducción y editor de recorte en el navegador.

---

## Fase 3 — Enterprise

Duración estimada: 4-6 semanas.

### Incluye

- Multi-servidor.
- API pública.
- Webhooks.
- Programación de eventos.
- Integración Teams.
- Integración Telegram.
- Dashboard global.

---

# 18. Compartir y Embed

- Popup «Compartir» por canal: tipo de reproductor (WebRTC o HLS), autoplay, silencio inicial, estadísticas, enlace con caducidad y dominios permitidos.
- Página de embed propia (`/embed/{canal}?t=token`) con reproductor propio (hls.js o WHEP), logo y mensaje de «sin señal».
- Código generado: `<iframe src="https://tu-dominio/embed/evento?t=…" allow="autoplay; fullscreen">`.
- Token de solo lectura, con caducidad; nunca las credenciales de publicación. HTTPS mediante proxy inverso.
- Público numeroso: emitir a un CDN. Cada espectador servido desde el servidor consume su ancho de banda de subida.

---

# 19. Consumo de Datos

- Los contadores de MediaMTX se reinician con cada sesión o reinicio. El backend muestrea cada 1-5 s, guarda los deltas y consolida por hora, día y mes.
- Entrada y salida por canal y destino; sesión actual y acumulado mensual.
- Cuotas por canal y alertas al acercarse al límite.
- Exportación a CSV.
- En la interfaz: botón en la barra superior que abre una ventana flotante.

---

# 20. Interfaz

- **Dashboard global:** estado de canales, alertas, recursos.
- **Detalle de canal:** programa con preview, indicadores de main/backup (recibiendo y correcta), grabación, señal activa con métricas, salidas con interruptor y edición. Fila superior: título y estado, espectadores, «Datos de entrada», selector de conmutación con Lock, «Compartir» y menú ⋯.
- **Grabaciones:** lista filtrable, reproductor, línea de tiempo, editor de recorte y exportaciones.
- **Ajustes:** usuarios, roles, alertas y sistema.
- Barra global: consumo, alertas y usuario.

Los mockups de detalle de canal y grabaciones están en el artefacto de diseño.

---

# 21. Decisiones Abiertas

1. Hardware: GPU o CPU disponible para reencodar, que decide entre modo A y modo B.
2. Número de canales simultáneos previstos.
3. Audiencia: solo reproductores locales o público (CDN).
4. Alojamiento y red: puertos UDP abiertos, proxy inverso, dominio.
5. Autenticación: usuarios locales o SSO.

---

# 22. Estructura Inicial del Repositorio

```text
verneta/
├─ docker-compose.yml
├─ mediamtx/mediamtx.yml
├─ backend/        (api, servicios: mediamtx, switcher, usage, exports)
├─ switcher/       (prototipos de la Fase 0)
├─ frontend/       (pages: Dashboard, Channel, Recordings, Settings)
├─ scripts/        (simulación de encoders con FFmpeg)
└─ docs/           (este documento y los mockups)
```

---

# 23. Cambios respecto a v0.1

Se mantiene la estructura y se añade:

- Definición técnica del conmutador (sección 7) y Fase 0.
- Modo caller/pull y entrada backup opcional (hot standby).
- Consumo de datos, compartir y embed, línea de tiempo y editor de recorte.
- Entidades y endpoints adicionales.
- Pérdida de paquetes solo en SRT.

---

# 24. Visión del Producto

La plataforma debe convertirse en una solución de operación de streaming profesional construida sobre MediaMTX, permitiendo a operadores audiovisuales gestionar señales, redundancia, grabaciones y distribución desde una única interfaz, sin necesidad de conocimientos avanzados sobre protocolos de transporte o configuración de bajo nivel.

La filosofía del producto es:

**"Gestionar canales, no protocolos."**