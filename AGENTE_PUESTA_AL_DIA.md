# Puesta al día del agente de IA (NVIDIA CODE)

## 1) Qué es y cómo arranca

Tu proyecto implementa un agente CLI de programación llamado **NVIDIA CODE v2.0** con dos modos:

- **Modo normal** (un solo agente conversacional con herramientas).
- **Modo Heavy** (colaboración multi-agente/modelo para análisis más profundo).

Entrada principal:

```bash
python main.py
python main.py --heavy
```

Parámetros relevantes:

- `--model/-m` para seleccionar modelo.
- `--workdir/-w` para fijar directorio inicial.
- `--no-stream` para desactivar streaming.

---

## 2) Arquitectura de alto nivel

### Capa de ejecución

- `main.py` instancia `NVIDIACodeAgent` y delega el ciclo principal con `agent.run()`.
- En `core/agent.py` vive la orquestación principal: estado del agente, ejecución de herramientas, cache, manejo de errores y persistencia de chat.

### Capa Heavy Agent

`core/heavy_agent.py` amplía el enfoque con colaboración entre agentes/modelos:

- **Message Bus** entre agentes (`AgentMessageBus`).
- **Knowledge Graph** para hechos con confianza y detección de conflictos.
- **Métricas por agente** (tokens, tools, retries, peer reviews, debates).
- **Contexto compartido con cache LRU** para evitar trabajo repetido.

### Capa de modelos

- `models/registry.py` define `ModelInfo` y `AVAILABLE_MODELS`.
- Actualmente hay catálogo numerado (1..18) con metadatos:
  - `id` del proveedor,
  - soporte de tools,
  - modo de thinking/reasoning,
  - temperatura/top_p,
  - tier y descripción.

### Capa de herramientas

- `tools/__init__.py` registra herramientas base y opcionales con carga resiliente (`try/except` por bloque).
- Base: archivos, terminal, búsqueda, git, utilidades de proyecto.
- Opcionales: testing, seguridad, datos, web/api, media, ml, devops, etc.

### Configuración

- `config.py` centraliza API key, endpoint NVIDIA, límites de tokens/iteraciones y parámetros del modo Heavy.
- El modo Heavy trae lista de modelos primarios + sintetizador + thresholds de consenso/debate.

---

## 3) Flujo operativo resumido

1. CLI parsea args.
2. Se crea `NVIDIACodeAgent` con modelo/directorio/stream/heavy.
3. Se inicializa UI y estado.
4. Si heavy está activo, se delega al motor colaborativo para descomposición/debate/síntesis.
5. Se consumen herramientas según necesidad y se devuelve respuesta al usuario.

---

## 4) Estado actual observado (quick audit)

- Proyecto orientado a **agente local tipo IDE/terminal assistant**.
- Muy fuerte en **extensibilidad por herramientas**.
- Arquitectura ya separada por responsabilidades (core/models/tools/ui/commands).
- El modo Heavy está diseñado para robustez (revisión cruzada + consenso), no solo para velocidad.

---

## 5) Recomendaciones inmediatas (priorizadas)

1. **Alinear defaults de modelo** entre `config.py` y `models/registry.py` para evitar confusión operativa.
2. **Añadir pruebas automáticas mínimas** para:
   - registro de modelos,
   - carga de tools,
   - arranque en modo normal/heavy.
3. **Documentar contrato de tool** (inputs/outputs/errores) para acelerar nuevas integraciones.
4. **Añadir healthcheck de startup** (API key, endpoint, disponibilidad de tools críticas).
5. **Métricas exportables** (JSON/CSV) como reporte por sesión para tuning de prompts/modelos.

---

## 6) Comandos útiles para operar tu agente

```bash
# Normal
python main.py

# Heavy mode
python main.py --heavy

# Forzar modelo del registro
python main.py --model 3

# Directorio de trabajo específico
python main.py --workdir /ruta/proyecto

# Sin streaming
python main.py --no-stream
```

---

## 7) TL;DR

Tu agente está bien encaminado para un asistente técnico “serio”: tiene base sólida de tools, catálogo amplio de modelos y una ruta avanzada multi-agente con memoria estructurada. Lo siguiente para subir de nivel es **estabilizar pruebas, observabilidad y consistencia de configuración**.
