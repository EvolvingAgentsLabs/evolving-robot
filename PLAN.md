# evolving-robot — Plan de trabajo

> Agente robótico 2D que evoluciona sus propias skills de forma autónoma, usando
> **Gemma 4 (AI Studio, sin GPU)** como único cerebro, **odyssey** como columna de
> orquestación/evaluación, **skill-map** como gate semántico de auto-ediciones, y
> **agentvcs** como memoria genética (commit / rollback / freeze). Se desarrolla en
> paralelo a **agentvcs** (dogfooding) en el mismo workspace `~/evolvingagents/`.

Estado: DRAFT (plan). Fecha: 2026-07-04.

---

## 0. Qué cambió respecto del brainstorm (correcciones basadas en el código real)

El brainstorm es sólido en la visión, pero la investigación de los 4 repos obliga a
corregir varias premisas de implementación:

1. **No es un monorepo pnpm+Python.** `odyssey` y `agentvcs` son **Python**;
   `skillos_x_robot` es **TypeScript**; `skill-map` es un **CLI TS (Node ≥24)** que se
   consume como **subproceso** (agnóstico al lenguaje). Mezclar pnpm workspaces con
   Python es fricción innecesaria. **Decisión: columna en Python** (odyssey + agentvcs
   nativos), reusando de `skillos_x_robot` sólo el **visor HTML/canvas** (`sim2d.html`,
   que no necesita TS en runtime) y **portando** su lógica de HAL/backend/dream a Python.

2. **El ejemplo `multiagent-openvla-gemma` se simplifica dramáticamente.** Todo el
   andamiaje `RemotePlanner` + `planner_server` + venv separado existe SÓLO para resolver
   el conflicto de versiones de `transformers` entre Gemma local y OpenVLA. Con **Gemma
   por REST** (AI Studio) y **sin OpenVLA** (un robot 2D no necesita una VLA de 7-DoF),
   todo corre **in-process**. Es la mayor simplificación disponible.

3. **skill-map NO reconoce `patrol-route.skill.md`.** skill-map lee los archivos del
   *vendor* (Claude Code `SKILL.md`, `.claude/agents/*.md`, etc.). Para que `sm scan` las
   detecte nativamente, las skills se guardan en el layout Claude Code:
   `robot_brain/skills/.claude/skills/<name>/SKILL.md`. Las referencias entre skills
   pasan a ser frontmatter `skills: [otra-skill]` o `@otra-skill` en el cuerpo → skill-map
   las convierte en aristas y `core/reference-broken` las gatea.

4. **El "dream engine" hay que construirlo, no reusarlo tal cual.** El de
   `skillos_x_robot` consolida **memoria** (`memory/consolidated/**`), no reescribe skills.
   El patrón "leer estado → 1 llamada LLM → parsear bloques → escribir .md" se reusa, pero
   redirigido a **archivos de skill** y con el gate `sm check` + commit `agentvcs` encima.

5. **El scoring es gratis.** odyssey ya calcula `success_rate`, `performance_score`,
   `letter_grade`, `passed` vía `build_eval_summary()` y promedia a `overall_grade`. El
   controlador de evolución sólo lee ese dict (SQLite `~/.odyssey/missions.db` o el JSON de
   `StdoutEventPublisher`). No hay que escribir métricas.

6. **Límites reales de skill-map como gate:** hoy sólo gatea deterministamente. Gates duros
   (`severity: error`): `core/reference-broken`, `core/name-collision`,
   `core/schema-violation`. La capa probabilística (dup semántico) es un **stub** — no
   gatea nada todavía. Y **no hay detección de ciclos** más allá de self-loop (A→A); si el
   agente crea A→B→A hay que caminar `ScanResult.links` a mano.

---

## 1. Arquitectura reformulada

```
~/evolvingagents/
├── agentvcs/            # Python lib (se EDITA acá: track de dogfooding)
├── odyssey/             # Python framework (se importa; extensiones upstreamables)
├── skill-map/           # CLI TS (se invoca como subproceso `sm`)
├── skillos_x_robot/     # TS (fuente de assets a portar: sim2d.html, HAL, backend, dream)
└── evolving-robot/      # <-- NUEVO PROYECTO (Python, hermano de los demás)
    ├── PLAN.md
    ├── pyproject.toml          # deps: agentvcs (-e ../agentvcs), odyssey (-e ../odyssey),
    │                           #        httpx/requests, websockets, pyyaml
    ├── agent.json              # manifiesto agentvcs (goal, models, trace=odyssey, eval)
    ├── robot_brain/
    │   ├── gemma.py            # GemmaRestGenerator.generate(messages, image=None)->str
    │   ├── pilot.py            # Pilot2D: PilotRuntime para acciones 2D discretas
    │   ├── dream.py            # DreamEngine: traces -> reescribe SKILL.md -> gate -> commit
    │   └── skills/.claude/skills/
    │       ├── patrol-route/SKILL.md
    │       ├── checkpoint-inspection/SKILL.md
    │       └── staff-interaction/SKILL.md
    ├── sim2d/
    │   ├── viewer.html         # portado de skillos_x_robot/sim/sim2d.html
    │   └── server.py           # asyncio: SimulatorHAL + WS(9091) + HTTP(9092)
    ├── odyssey_ext/
    │   ├── gemma_rest.py       # TextGenerator REST -> envuelto en odyssey LLMPlanner
    │   └── sim2d_runner.py     # Runner odyssey (EVALUATION) que maneja el sim por WS
    ├── missions/
    │   └── patrol.mission.yaml # 1 training (stub) + 1 evaluation (sim2d), eval último
    ├── scripts/
    │   └── eval.sh             # corre la misión y devuelve exit-code por umbral (para agentvcs)
    └── evolve.py               # CONTROLADOR: run -> score -> dream -> gate -> commit/rollback
```

### Contratos concretos (firmas reales, de la investigación)

**Cerebro Gemma (REST, AI Studio, sin GPU).** Portar el `GeminiBackend` de
`skillos_x_robot/src/backend.ts` a Python. Endpoint AI Studio:
`POST https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent?key=$GEMINI_API_KEY`.
Dos caras del mismo cliente:

```python
# robot_brain/gemma.py
class GemmaRestGenerator:
    def __init__(self, model: str, api_key: str): ...
    def generate(self, messages: list[dict], image=None) -> str: ...   # satisface odyssey.TextGenerator
    def generate_with_tools(self, messages, tools, tool_choice=None) -> dict: ...  # function-calling p/ pilot
```

- `MODEL` es **config** (env `GEMMA_MODEL`). Riesgo abierto: confirmar el id exacto de
  Gemma-4 servido por AI Studio `generateContent`; fallback a `gemini-flash` si el free tier
  no lo expone. La *forma* de la API está confirmada.
- `generate(messages, image=None)->str` es exactamente lo que pide
  `odyssey.runners.agents.runtime.TextGenerator`; se envuelve en el `LLMPlanner` existente
  (`odyssey/src/odyssey/runners/agents/planner.py`) → satisface `PlannerRuntime` sin tocar core.

**Pilot 2D** (reemplaza a OpenVLA). odyssey `PilotRuntime.act(image, instruction)->ndarray`
no encaja para nav 2D; escribimos un pilot que mapea sub-instrucción + observación a una
acción discreta vía Gemma function-calling (o regla determinista para tests):

```python
# robot_brain/pilot.py
class Pilot2D:                         # satisface PilotRuntime (duck-typed)
    def act(self, observation: dict, instruction: str) -> dict:   # {"op":"move_forward","distance_cm":30} etc.
        ...
```

Primitivas del robot (portadas de `skillos_x_robot/src/hal.ts`): `move_forward(cm)`,
`rotate_left(deg)`, `rotate_right(deg)`, `stop()`, `get_position()`, `observe()` (landmarks
dentro de 3.5 m con `distance_m`/`bearing_deg`/`type`). NO hay visión real: `observe()`
devuelve JSON estructurado (igual que el original).

**Simulación 2D.** `sim2d/server.py` (asyncio) sostiene `SimulatorHAL` (misma trig que
`hal.ts`), difunde eventos (`pose`/`move`/`rotate`/`observe`/`speak`/`tool_call`/...) al
`viewer.html` por WebSocket (:9091), y expone un API de step al runner. El `viewer.html`
se sirve por HTTP (:9092) y se abre en el browser: `http://localhost:9092`.

**Runner odyssey (evaluación).** Modelar sobre `RobosuiteRunner`
(`odyssey/src/odyssey/runners/evals/robosuite.py`), reemplazando `robosuite.make/env.step`
por un cliente WebSocket al sim:

```python
# odyssey_ext/sim2d_runner.py
class Sim2DRunner(Runner):
    name = "sim2d"
    supported_kinds = {TaskKind.EVALUATION}
    supported_types = {"custom"}          # o un EvaluationType nuevo
    async def run(self, ctx: TaskContext) -> dict:
        # resolve_eval_checkpoint(ctx); _has_specialist(ctx) -> PlannedEvalRuntime(planner=Gemma, pilot=Pilot2D)
        # loop: obs = ws.reset(); por step: action = runtime.get_action(obs); ws.step(action)
        # return build_eval_summary(successes, episode_returns, ...)  # success_rate/performance_score/passed
```

Se reusan tal cual: `resolve_eval_checkpoint`, `_has_specialist`, `PlannedEvalRuntime`
(documentado como "simulator-agnostic"), `build_eval_summary`. Registro vía
`config: {runner: sim2d}` en la task o un shim en `cli/commands/run.py:_build_runners()`.

**Misión.** odyssey exige ≥1 training + **exactamente 1 evaluation, y última**. Un robot 2D
no hace finetune de VLA, así que la training task es un **stub formal** (`config: {runner:
cpu_mock}` — `CPUMockRunner` soporta todos los kinds/types y siempre "pasa"), etiquetada
como "warm-up / demostración". La sustancia está en la eval task (`sim2d`). *Mejora futura:*
que `Sim2DRunner` soporte también `TRAINING` y corra una patrulla de demostración que
alimente al dream engine.

**agentvcs (memoria genética, in-process).**

```python
from agentvcs import Repository, crystallize, diff_commits, recall
repo = Repository.init(Path("robot_brain"), manifest=agent_json)   # o Repository.open()
# tras dream + gate OK:
oid = repo.commit("evolve(patrol-route): +observe frequency after server-room miss")
# tras regresión en la próxima misión:
info = repo.rollback()          # -> {"restored_to","previous_head","goal","state"}; escribe rollbacks.jsonl
# freeze de un skill-set verificado (gate por eval de agent.json):
new_oid, artifact = crystallize(repo)
hits = recall(repo, "patrol server room")   # cache de recetas frozen por goal
```

- `agent.json`: `goal` = objetivo de patrulla; `models` = pin de Gemma; `mode` = `runtime`
  (captura budget/context/tools desde el uso de Gemma); `trace` = provider `odyssey` (nuevo,
  ver §Track agentvcs) o path al trace; `eval` = `scripts/eval.sh` (corre la misión y sale 0
  si `success_rate ≥ umbral`) para que `crystallize`/`ensure_passing` gateen el freeze.
- El commit versiona **code(tree=skills) + goal + models + trace** en un solo objeto.

### El bucle de vida (evolve.py)

```
1. odyssey run missions/patrol.mission.yaml            # patrulla en el sim2d, Gemma planea+pilotea
2. leer result_summary (success_rate, performance_score) de SQLite / stdout JSON
3. DreamEngine: traces -> Gemma reescribe UNA SKILL.md objetivo (la más asociada a fallos)
4. gate:  sm scan --changed  &&  sm check --json -n <skill_path>
          - si hay severity:error -> DESCARTAR el cambio (feed message+data de vuelta a Gemma)
5. si pasa el gate -> repo.commit("evolve(<skill>): <por qué>")
6. re-run misión (paso 1). Si success_rate cae < umbral -> repo.rollback(reason=<score+motivo>)
7. si sube y se estabiliza N rondas -> crystallize(repo)  (freeze del skill-set verificado)
```

---

## 2. Track paralelo: optimización de agentvcs (dogfooding)

Mejoras concretas que salen de construir el robot y se implementan en
`~/evolvingagents/agentvcs` (con tests, corriendo `pytest -q`):

1. **Nuevo trace provider `odyssey`** (`agentvcs/src/agentvcs/traces/odyssey.py` +
   entrada en `_PROVIDERS` + `test_odyssey_provider.py`). `pull(decl, workdir)->list[msg]`
   lee el `result_summary`/eventos de una corrida odyssey (SQLite o el JSONL de
   `StdoutEventPublisher`) y los normaliza a mensajes `{role,content,model,ts}`. Opcional
   `runtime(...)` para reconstruir el frame (budget desde `usages` de Gemma). Encaja en el
   patrón existente (qwen-code / vercel-eve / anthropic-managed).

2. **`rollback(reason=...)` explícito.** Hoy `rollbacks.jsonl` guarda `reason` = texto del
   goal restaurado. Mejora: aceptar un `reason` del llamador para registrar el contexto real
   ("success_rate 0.4 < 0.5 en server-room; revert skill v3; sm-error: reference-broken →
   checkpoint-inspection"). Es exactamente la fricción que anticipaba el brainstorm (agentvcs
   leyendo el JSON de error de skill-map para el mensaje de rollback).

3. **`--reconcile` con cerebro Gemma.** Cuando dos ramas de skills evolucionadas chocan,
   un reconciliador Gemma (subproceso que lee el bundle JSON por stdin y devuelve
   `{goal, trace, resolved_files?}`) resuelve el merge. Alternativa/《complemento》a nanoLoop
   como brain de `--reconcile`.

4. **Eval gate contra carga real.** Cablear `agent.json` eval = corrida odyssey valida el
   gate de `crystallize`/`ensure_passing` con un workload verdadero (no un test de juguete).

Cada fricción encontrada se documenta y se prueba en el robot inmediatamente (loop de
dogfooding cerrado).

---

## 3. Plan por fases (reordenado para vertical slice temprano y de-risk)

Principio: conseguir un **slice vertical corriendo** cuanto antes (Gemma → sim → score),
y meter primero las costuras riesgosas (polyglot, WS, protocolos odyssey).

### Fase 0 — Scaffolding y cerebro Gemma (de-risk del modelo)  ✅ HECHA
**Objetivo:** confirmar que Gemma-4 responde por REST desde AI Studio, sin GPU.
- [x] Crear `evolving-robot/` (pyproject; `-e ../agentvcs`, `-e ../odyssey` se suman en Fase 2/5).
- [x] Portar `GeminiBackend` (TS) → `robot_brain/gemma.py` (`generate` + `generate_full` con tools;
      provider `aistudio` primario + `openrouter` fallback).
- [x] Smoke: `scripts/smoke_gemma.py` (texto + function-calling). Skip limpio sin API key.
- **Aceptación (verificada 2026-07-04 contra el endpoint real):** `gemma-4-26b-a4b-it`
  responde por REST en AI Studio (texto OK) y hace **function-calling nativo**
  (`move_forward({distance_cm:30})`) → el `Pilot2D` de Fase 2 usa tool-calling nativo, sin
  fallback de parseo. **Caveat:** Gemma-4 hace "thinking" por defecto que se filtra al texto
  y factura tokens extra (`completion=17` vs `total=380`); en Fase 2 suprimir/strip thinking
  para el planner y vigilarlo con el runtime frame de agentvcs.

### Fase 1 — Simulación 2D en Python + visor (de-risk del transporte)  ✅ HECHA
**Objetivo:** robot manejable en el sim, visible en el browser.
- [x] Portar `SimulatorHAL` (trig de `hal.ts`) a `sim2d/server.py`; landmarks del arena.
- [x] WS(:9091) broadcast + HTTP(:9092) sirviendo `viewer.html` (nuevo, mismo vocab de eventos).
- [x] Comandos de control (reset/move/rotate/observe/stop/get_position/speak) + replies para el runner.
- **Aceptación (verificada):** `python -m sim2d.server` sirve el viewer (8 KB) y
  `scripts/drive_sim.py` completa la patrulla de 4 puntos; el robot llega a 0.4 m de cada
  checkpoint y `observe()` reporta los 6 landmarks con distancias correctas.

### Fase 2 — Odyssey end-to-end con Gemma (vertical slice)  ✅ HECHA
**Objetivo:** `odyssey run` completa una patrulla real y devuelve un score.
- [x] `odyssey_ext/gemma_rest.py`: `GemmaPlannerGenerator` envuelve el cerebro en `LLMPlanner`.
- [x] `robot_brain/pilot.py`: `Pilot2D.act(obs, instruction)` — modo `gemma` (function-calling
      nativo) + `scripted` (geometría sin API, cola de checkpoints stateful).
- [x] `odyssey_ext/sim2d_runner.py`: `Runner` de eval modelado sobre `RobosuiteRunner`; usa el
      orquestador multi-agente real de odyssey (`PlannedEvalRuntime`), maneja el sim por WS,
      devuelve `build_eval_summary`. Se registra para `(EVALUATION, "custom")` → auto-selección.
- [x] `missions/patrol.mission.yaml`: training stub (`cpu_mock`) + eval `custom` (último).
- [x] `scripts/run_mission.py`: arma el `MissionEngine` con `providers=None` (sin resolución de
      robot/dataset). Sin tocar el fuente de odyssey.
- **Aceptación (verificada 2026-07-04):** mission COMPLETED con pilot **scripted** (4/4,
  success_rate 1.0) y con **Gemma planner + Gemma pilot** (4/4, success_rate 1.0, grade A,
  overall_grade 1.0), manejando el sim2d en vivo por function-calling nativo.
- **Lección:** el cliente Gemma es httpx **síncrono**; llamarlo directo dentro del runner async
  bloquea el event loop y el WS cierra por keepalive-timeout (1011). Fix:
  `asyncio.to_thread(runtime.get_action/begin_episode, ...)`.

### Fase 3 — Skills + skill-map como gate  ✅ HECHA
**Objetivo:** skills en layout que skill-map entiende, y gate funcionando.
- [x] 3 skills en `robot_brain/skills/.claude/skills/<n>/SKILL.md` con refs cruzadas `@name`
      (patrol-route, checkpoint-inspection, staff-interaction). `sm scan` detecta 3 nodos +
      6 aristas `mentions` (conf 1), sin issues.
- [x] Disclosure progresiva: `robot_brain/skills.py` (parse frontmatter, tabla de metadata
      nivel-1, `get_skill` nivel-2). El tool `load_skill` en el pilot se cablea en Fase 4.
- [x] `sm init` en `robot_brain/skills/` (Node 24 instalado vía nvm; `@skill-map/cli` global).
- [x] Helper `robot_brain/skill_gate.py`: `gate_skill(skills_dir, node_path)` corre
      `sm scan --changed` + `sm check --json -n path`, parsea `Issue[]`, rechaza si hay
      `severity:error`. Resuelve `sm` sobre Node≥24 automáticamente (o `SM_CMD`).
- **Aceptación (verificada, `scripts/gate_demo.py`):** set limpio → ok; meter `@ghost-skill`
      → `ok=False` con `core/reference-broken` y `data.target=@ghost-skill`; tras revertir → ok.
- **Requisito:** `sm` necesita Node ≥ 24. Instalado `v24.18.0` vía nvm; el helper lo detecta solo.

### Fase 4 — Dream engine (auto-programación)  ✅ HECHA
**Objetivo:** el agente reescribe UNA skill a partir de sus fallos.
- [x] El pilot Gemma ahora **lee** la skill objetivo (`skill_context` inyectado en el prompt),
      así reescribirla cambia el comportamiento (no es decorativo). El `Sim2DRunner` carga la
      skill y **escribe un trace markdown** por misión (`traces/<ts>_<benchmark>.md`: objetivo,
      success_rate, checkpoints reached/missed, y los pasos con instrucción/acción/posición).
- [x] `robot_brain/dream.py` (`DreamEngine`): lee el último trace → prompt a Gemma → parsea el
      bloque `--- SKILL --- ... --- END SKILL ---` → `write_skill_body` (preserva frontmatter).
- [x] `apply_gated`: escribe candidato → `gate_skill` → si falla, feed del error de skill-map a
      Gemma y reintenta (máx K) → si sigue fallando, **revierte** y descarta.
- **Aceptación (verificada 2026-07-04, `scripts/dream_demo.py`):** rama DISCARD (sin key): un
      rewrite con `@ghost-skill` se reintenta con el feedback de skill-map y se revierte (archivo
      intacto). Rama KEEP (Gemma real): `status=kept, attempts=1, gate_ok=True`; Gemma reescribió
      `patrol-route` desde el trace a un loop de navegación más explícito, **preservando** los
      `@checkpoint-inspection`/`@staff-interaction` (por eso pasó el gate).
- **Lección:** las reescrituras de Gemma (thinking) tardan >60s → subir el timeout httpx
      (`GEMMA_TIMEOUT`, default 120s) o la reescritura corta por ReadTimeout.

### Fase 5 — agentvcs: commit / rollback / freeze autónomos  ✅ HECHA
**Objetivo:** control de versiones semántico manejado por el propio agente.
- [x] `robot_brain/evolve.py` (`EvolutionController`): `Repository.init/open` sobre
      `robot_brain/skills/` (manifest con goal/models). Ignores cruzados: `.agentvcsignore`
      (excluye `.skill-map`/`.skillmapignore`) y `.skillmapignore` (excluye `agent.json`/
      `AGENTS.md`/`.agentvcs`) para que ninguno contamine al otro.
- [x] `evolve_step`: baseline commit → dream (rewrite gateado) → commit evolved → re-score →
      si `new_score < baseline*keep_ratio` → `rollback(reason=...)` (restaura las skills y cita
      el score en el ledger); si no, keep. Verificado bueno → `crystallize` (freeze).
- **Dogfood #1 en agentvcs:** `Repository.rollback(reason=...)` (antes el reason era el goal
      del commit restaurado). Backward-compatible (default None → comportamiento idéntico), con
      2 tests nuevos. **185/185 tests de agentvcs pasan.**
- **Aceptación (verificada, `scripts/evolve_demo.py`):** en una copia temporal, (1) mala
      evolución → score 0.4<1.0 → **rollback** con reason `success_rate 0.40 < 1.00 baseline`,
      skills restauradas; (2) buena evolución → keep → **crystallized**. History:
      `baseline → evolve → crystallized`.
- **Nota:** el demo usa dream stubeado + scores inyectados (determinista, sin sim/API). El bucle
      LIVE (dream real + score de misión real) está cableado en `evolve_step` y documentado.
      Pendiente para Fase 6: `crystallize` con eval-gate real (`agent.json` eval = corrida odyssey).

### Fase 6 — Dogfooding agentvcs + wiring live  ✅ HECHA (núcleo)
- [x] **Dogfood #1:** `Repository.rollback(reason=...)` (Fase 5). 2 tests.
- [x] **Dogfood #2:** trace provider `odyssey` en agentvcs
      (`src/agentvcs/traces/odyssey.py` + registro): lee la `missions.db` nativa de odyssey
      (stdlib `sqlite3`+`json`, sin importar odyssey) y normaliza objetivo + `result_summary`
      por task + status a mensajes. `pull`/`describe`. **5 tests nuevos. 190/190 pasan.**
- [x] Cableado en el manifest del controlador (`trace: {provider: odyssey, db: ...}`) → los
      commits de agentvcs **capturan el trace real de la misión** (verificado: baseline commit
      con dimensión trace = objetivo + success_rate + overall_grade).
- [x] `scripts/evolve_live.py`: bucle LIVE end-to-end (levanta el sim, misión real → commit
      con trace de odyssey → dream → re-score → keep/rollback). Bug arreglado: el controller
      hacía `Repository.open` que subía al `.agentvcs` del workspace; ahora crea el repo en la
      carpeta de skills. Verificado el path sin-key (wiring + captura de trace).
- [~] Pendiente/opcional: correr el LIVE completo con Gemma (composición ya verificada por
      partes), `crystallize` con eval-gate real (`agent.json` eval = corrida odyssey),
      `--reconcile` con Gemma. Demo visual: Artifact compartible en vez de video.
- **Aceptación:** `pytest -q` de agentvcs = **190 passed**; trace de odyssey capturado en un
      commit real; bucle live cableado y verificado.

---

## 4. Riesgos y decisiones abiertas

- **Modelo Gemma-4 en AI Studio:** confirmar id exacto para `generateContent`; fallback a
  `gemini-flash`. (Fase 0 lo cierra.) La API es la misma; sólo cambia el string del modelo.
- **Latencia/costo del bucle:** cada misión son N pasos × 1 llamada Gemma. Mantener misiones
  cortas (4 checkpoints) y `num_episodes` bajo al inicio. `mode=runtime` de agentvcs vigila
  el budget.
- **skill-map sin detección de ciclos:** si las skills pueden crear A→B→A, caminar
  `ScanResult.links` a mano en `gate_skill`. Empezar sin ciclos.
- **Determinismo para tests:** modo stub del pilot (regla) y traces canned para poder testear
  el bucle sin pegarle a la API.
- **Constraint de misión odyssey (training obligatorio):** resuelto con `cpu_mock` stub;
  revisar si conviene un `TRAINING` real que junte demostraciones.
- **Node ≥24 para `sm`:** el gate depende de tener `sm`/`npx @skill-map/cli` disponible.

---

## 5. Primer entregable propuesto

Cerrar **Fase 0 + Fase 1** en un solo empujón: `evolving-robot/` scaffoldeado, `gemma.py`
respondiendo por REST, y el sim2d 2D moviéndose en el browser con acciones scripteadas. Es el
piso sobre el que todo lo demás se apoya y de-riskea las dos incógnitas mayores (modelo por
REST + transporte al visor).
