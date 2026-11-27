# WebMatrix (VerNetA)

Panel web de control para MediaMTX: recibe señales por SRT y RTMP, muestra preview, códec, calidad y audio,
conmuta entre una entrada **main** y una **backup** publicando un `program` estable, y reenvía ese `program` a CDNs
o a reproductores locales. La visión completa está en [`docs/Projecte_VerNetA_v0.2.md`](docs/Projecte_VerNetA_v0.2.md).

## Arranque

```bash
docker compose up -d --build
```

Panel: http://localhost:8080

## Uso rápido

1. **Nuevo canal**: se crea con una clave para main y otra para backup.
2. **Datos de entrada**: copia servidor y clave (o la URL completa) en tu encoder. Pulsa un campo para copiarlo.
3. Cuando llega señal aparece el **preview del programa**, los datos de la señal y los **vúmetros** de cada entrada.
4. **Salidas**: añade un *push* (RTMP, RTMPS o SRT) a un CDN, o un *pull* (SRT, RTMP) para reproductores locales.
   Cada salida tiene interruptor de encendido y botón de edición.

Sin encoder real: `./scripts/test-sources.sh localhost NOMBRE_DEL_CANAL` simula main (SRT) y backup (RTMP).

Publicar en un canal: SRT `srt://IP:8890?streamid=publish:CANAL/main:verneta:CLAVE`, RTMP `rtmp://IP:1935/CANAL/main?user=verneta&pass=CLAVE`.

## Conmutación main/backup

Cada canal publica `canal/program`. El conmutador funciona **sin reencodar**: cambia de fuente en un keyframe y reescribe
los tiempos para que `program` nunca se desconecte; así las salidas, el preview y las grabaciones futuras no pierden la conexión.

- **Auto:** pasa a backup si la activa no envía paquetes durante 2 s. Vuelve a main tras 10 s estable, con un mínimo de
  10 s entre cambios voluntarios y un máximo de 3 por minuto.
- **Forzar main / Forzar backup / Lock:** control manual desde la cabecera del canal.
- Medido en pruebas: un lector conectado a `program` no perdió la conexión al cortar main; el congelado de vídeo fue de
  unos 2 s (detección + espera al siguiente keyframe), con tiempos continuos.
- Limitación: main y backup deben tener los mismos parámetros de codec y resolución. La conmutación sin ningún
  congelado requeriría reencodar (modo A del documento de visión).

## Audio

Un vúmetro por entrada, medido en el servidor con ffmpeg (funciona con AAC). El estado «Correcto», «Muy bajo» o
«Saturando» solo aparece si el problema se mantiene unos segundos; un pico puntual no avisa. Tras 5 s de silencio la
entrada pasa a «degradada» y se registra un evento.

## Qué falta

Grabación y biblioteca de grabaciones, fps/GOP (ffprobe), usuarios y roles, HTTPS, alertas, conmutación con reencode,
dashboard global y consumo por salida. Ver las fases del documento de visión.

## Seguridad

Publicar exige la clave de la entrada y leer `program` por SRT/RTMP exige una salida pull activa. Ver el panel todavía no
pide usuario: úsalo en red local. No publiques los puertos 9997 ni 9998 de MediaMTX.

## Flujo de trabajo

`main` siempre estable. Cada cambio va en una rama `feature/...` y se integra con pull request. Las versiones se marcan
con etiquetas `vX.Y.Z` tras aprobar la PR.
