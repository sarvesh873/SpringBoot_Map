# SpringMap

Knowledge graph builder for Spring Boot projects — optimized for LLM coding assistants (GitHub Copilot, Claude, Cursor) and multi-agent pipelines.

**The problem:** Copilot reads every `.java` file on each question, burning tokens and context window.
**The solution:** SpringMap produces a token-optimized structural index — plus a full reference doc and an interactive visualization — so agents answer most questions without opening a single source file.

---

## What gets extracted

| Source | What SpringMap reads |
|--------|---------------------|
| `.java` files | Controllers, services, repos, entities, DI graph, method call chains, HTTP endpoints |
| `.proto` files | gRPC services, RPC methods, request/response message types (recursive discovery, any location) |
| `pom.xml` / `build.gradle` | Project name, Java version, Spring Boot version, categorized dependencies |
| `application.yml` / `.properties` | Server port, datasource URL, JPA config, custom props (including nested keys) |
| `openapi.yaml` / `swagger.yaml` | Virtual controller nodes for code-generated or gateway endpoints |
| Kafka / RabbitMQ / SQS / JMS annotations | `@KafkaListener`, `@RabbitListener`, `@SqsListener`, `@JmsListener`, `@EventListener`, `@Scheduled` — tracked separately from REST endpoints |

---

## Setup

### 1. Install

```bash
cd /path/to/springmap
pip install -e .
springmap --help
```

### 2. Build the graph

```bash
cd /path/to/your/spring-boot-project
springmap build .
```

Five files land in `./springmap-out/`:

| File | Typical size (60 classes) | Use it for |
|------|---------------------------|------------|
| `GRAPH_COMPACT.md` | ~5K tokens | **Attach this to Copilot.** Class index, REST endpoints, DI edges, event listeners — nothing else. |
| `GRAPH.md` | ~13K tokens | Full reference: call chains, entity fields, Maven deps. For humans, or as a fallback when the compact index doesn't resolve a question. |
| `graph.html` | ~15–25 KB | Interactive D3.js visualization — open in any browser, zero server. |
| `graph.json` | ~7–10K tokens | Machine format. Powers every CLI command. Never attach this to an agent. |
| `manifest.json` | small | File-hash cache so `update` only re-parses what changed. |

### 3. Configure Copilot / agents to use the graph

Attach `GRAPH_COMPACT.md` — not `GRAPH.md`, not `graph.json` — in `.github/copilot-instructions.md`:

```markdown
## Project Knowledge Graph

A compact structural index lives in springmap-out/GRAPH_COMPACT.md.

RULE: Before reading any .java source file, search GRAPH_COMPACT.md for:
  - The class name you need (Class Index, grouped by layer)
  - The endpoint path you need to modify (REST Endpoints table)
  - The Kafka/RabbitMQ topic you need (Event Listeners table)
  - Which class injects which (Injects / Used by lines)

Only open a .java file when you need the actual method body.
```

**Does this save tokens?** Only if the agent actually stops reading source files because of it — attaching `GRAPH_COMPACT.md` on top of normal auto-file-scanning costs more, not less. For a 60-class project, `GRAPH_COMPACT.md` runs ~5K tokens versus ~12K for auto-reading 10 Java files per question — but that saving only exists if the instruction is followed. Past ~150 classes, `GRAPH_COMPACT.md` itself gets big enough that targeted CLI queries (`springmap show X`, `springmap query "..."`) beat attaching any file at all. Run `springmap stats` periodically to check where your project sits.

### 4. Keep the graph current

```bash
springmap update .    # re-parses only changed files
```

Pre-commit hook:

```bash
#!/bin/sh
# .git/hooks/pre-commit
springmap update .
git add springmap-out/GRAPH_COMPACT.md springmap-out/graph.json springmap-out/graph.html
```

---

## Multi-agent pipeline

SpringMap is designed to sit underneath a research → implement → verify → test agent chain, each pinned to the model suited to its job:

```
@research (Haiku 4.5, read-only)
    │  1. springmap show/path/query  → targeted 100–400 token answers
    │  2. GRAPH_COMPACT.md            → broader survey when needed
    │  3. GRAPH.md                    → fallback for call chains / entity fields
    │  4. source files                → last resort only
    ▼  handoff note: files+lines, cascades, auth gate, customer-data flags
@alita (Sonnet 4.6, Spring Boot implementation)
    │  trusts the handoff note — doesn't re-query the graph unless it's incomplete
    ▼  handoff note: files changed, new endpoints/methods, "never cut" items touched
@verifier (Haiku 4.5, read-only checklist)
    │  runs `springmap update .` first to refresh the graph, then checks the diff
    ▼
@veronica (Sonnet 4.6, JUnit 5 / Mockito)
       springmap show <Class> → "Injects" line = exact mock list
       springmap endpoints    → cross-check test coverage against real endpoints
```

Each agent's `.github/agents/*.agent.md` and the shared `.github/copilot-instructions.md`
reference `GRAPH_COMPACT.md` as the default lookup and the CLI as the escape hatch for
single-class questions — see `PIPELINE.md` in your repo config for the full I/O diagram
and per-stage token cost table.

---

## Command reference

```
springmap [--out <dir>] COMMAND [OPTIONS]
```

`--out` defaults to `./springmap-out` and is shared by every command.

### `build`

Full parse from scratch.

```bash
springmap build .
springmap build /path/to/project
springmap build . --quiet          # -q, suppress progress output
```

### `update`

Incremental — only re-parses files changed since the last `build` (tracked via `manifest.json`). Falls back to a full build if no existing graph is found.

```bash
springmap update .
```

### `query`

Search the graph without touching source files.

```bash
springmap query "user auth"                  # keyword search
springmap query "type:service"
springmap query "uses:UserRepository"
springmap query "used-by:UserController"
springmap query "path:/api/users"
springmap query "method:POST path:/api"
springmap query "kind:listener"               # rest | grpc | listener
springmap query "src:openapi"                 # or src:proto
springmap query "type:service user" --limit 10
springmap query "type:repository" --json      # raw JSON output
```

| Filter | Matches |
|--------|---------|
| `type:X` | controller, service, repository, entity, component, configuration, dto, grpc, openapi, interface |
| `uses:ClassName` | classes that inject `ClassName` |
| `used-by:ClassName` | classes `ClassName` depends on |
| `path:/api/v1` | endpoint path contains substring |
| `method:GET` | HTTP verb match |
| `kind:rest` / `kind:grpc` / `kind:listener` | endpoint category |
| `pkg:com.example` | package prefix |
| `src:openapi` / `src:proto` | non-Java-sourced nodes |

### `show`

Full details for one class — metadata, DI edges, endpoint table, all methods with call chains, entity fields.

```bash
springmap show UserService
springmap show UserController
springmap show User             # entity — shows table name + fields
```

Supports partial/case-insensitive matching; suggests alternatives if not found.

### `path`

Shortest dependency path between two classes (BFS over the DI graph).

```bash
springmap path UserController UserRepository
```

### `endpoints`

Lists **REST endpoints by default** — gRPC and listener methods are never mixed in unless you ask.

```bash
springmap endpoints                  # REST only (default)
springmap endpoints .
springmap endpoints --method POST
springmap endpoints --filter /api/v1
springmap endpoints --grpc           # gRPC RPCs from .proto files only
springmap endpoints --listeners      # Kafka/RabbitMQ/SQS/JMS/@EventListener/@Scheduled only
springmap endpoints --all            # REST + gRPC + listeners together
```

`--method` accepts any verb relevant to the category shown — `GET`/`POST`/etc. for REST, `RPC` with `--grpc`, `KAFKA`/`RABBIT`/`SCHEDULED`/etc. with `--listeners`.

### `stats`

Parse quality report: class counts by layer, REST/gRPC/listener endpoint counts (tracked separately, never combined into one ambiguous total), AST-parsed vs. regex-fallback counts, output file sizes.

```bash
springmap stats
```

### `info`

Deep breakdown of `pom.xml` + `application.yml`. Cross-references your datasource URL against your actual dependencies — flags it if the URL says `postgresql` but no PostgreSQL driver exists in the POM. Every detected Spring starter is annotated with graph-derived counts.

```bash
springmap info
```

Shows: Maven coordinates, Java/Spring Boot version (resolved through parent POM, BOM import, or direct dependency — handles internal/corporate parent POMs), server port, context path, active profiles, datasource + inferred DB type + driver match, JPA settings, dependencies grouped by category (starters, database, messaging, security, testing), and custom `application.yml` properties flattened to dot notation.

### `graph`

Interactive D3.js force-directed visualization of the whole architecture — single self-contained HTML file, opens in any browser, no server.

```bash
springmap graph
springmap graph --open    # generate + open in default browser
```

Nodes are shaped and colored by Spring layer (controller = blue circle, service = green rect, repository = amber diamond, entity = purple hexagon, gRPC = magenta, Kafka consumer = dashed orange). Click any node for a detail panel (file, endpoints, DI dependencies, listeners); filter by layer, search by class name, toggle DI/event edges and labels, drag to reposition, zoom/pan.

This is for humans and architecture reviews — not for attaching to Copilot; use `GRAPH_COMPACT.md` for that.

### `clean`

```bash
springmap clean                 # delete ./springmap-out
springmap clean .               # same — project root is optional
springmap clean --yes           # -y, skip confirmation
```

---

## Output files in detail

### `GRAPH_COMPACT.md`

The token-optimized index — attach this to Copilot. Contains only: class names, types, files, DI edges (`Injects:` / `Used by:`), a REST endpoint table, an event listener table, and plain method name lists. No call chains, no entity field tables, no Maven section. Roughly 55% smaller than `GRAPH.md` for the same project.

### `GRAPH.md`

Full human-readable reference. Sections:

1. Project overview (tech stack, counts, config)
2. **REST endpoint table** — every endpoint from Java controllers AND OpenAPI specs
3. **gRPC Services** — RPC methods from `.proto` files (present only if any exist)
4. **Event Listeners & Scheduled Jobs** — Kafka/RabbitMQ/SQS/JMS/`@EventListener`/`@Scheduled`, kept separate from REST (present only if any exist)
5. **Controllers** — base paths, endpoint tables, non-endpoint methods
6. **Services** — DI dependencies, method signatures and call chains, `@Transactional` markers
7. **Repositories** — extends, custom query methods
8. **Entities** — table name, field list with types/columns/JPA relationships
9. **Other components** (DTOs, configs, utils, exceptions, consumers)
10. **Configuration** — server port, datasource, JPA settings, custom props
11. **Dependency map** — textual call chains (Controller → Service → Repository)
12. **Maven dependencies**

### `graph.html`

Self-contained D3.js v7 visualization, loaded from CDN. All graph data embedded inline as JSON — works fully offline after the first load. Never attach this to an agent; it's a browser artifact, not context.

### `graph.json`

Full serialized graph. Powers every CLI command. Schema:

```json
{
  "project_name": "user-service",
  "artifact_id": "user-service",
  "group_id": "com.example",
  "project_version": "2.3.1",
  "java_version": "17",
  "spring_boot_version": "3.2.5",
  "maven_dependencies": ["org.springframework.boot:spring-boot-starter-web", "..."],
  "classes": {
    "UserService": {
      "name": "UserService",
      "node_type": "service",
      "file_path": "src/main/java/.../UserService.java",
      "dependencies": ["UserRepository"],
      "dependents": ["UserController"],
      "methods": [
        {
          "name": "createUser",
          "return_type": "UserDTO",
          "http_method": null,
          "calls": ["UserRepository.save()"],
          "is_transactional": true,
          "signature": "UserDTO createUser(UserCreateDTO dto)"
        }
      ]
    },
    "UserGrpcService": { "node_type": "grpc", "methods": [ { "name": "getUser", "http_method": null } ] },
    "UserEventConsumer": { "node_type": "component", "methods": [ { "name": "onEvent", "http_method": "KAFKA", "http_path": "user-events" } ] }
  },
  "config": { "server_port": "8080", "datasource_url": "jdbc:postgresql://...", "jpa_ddl_auto": "validate" }
}
```

---

## Architecture

```
springmap/
├── parser/
│   ├── java_parser.py       AST (javalang) + regex fallback; HTTP mapping + listener
│   │                        annotation detection (@KafkaListener, @RabbitListener,
│   │                        @SqsListener, @JmsListener, @EventListener, @Scheduled)
│   ├── proto_parser.py      Recursive .proto discovery, gRPC services + message types
│   ├── openapi_parser.py    Recursive OpenAPI/Swagger YAML discovery → virtual nodes
│   ├── pom_parser.py        Maven/Gradle metadata; Spring Boot version resolved via
│   │                        parent POM → BOM import → direct dependency fallback chain
│   └── config_parser.py     application.yml/.properties, nested custom-property flattening
├── graph/
│   ├── models.py            ClassNode, MethodInfo, ProjectGraph, categorize_verb()
│   └── builder.py           Orchestrates all parsers + post-parse passes:
│                              • interface-driven endpoint discovery
│                              • constructor injection detection
│                              • dependency resolution (dependencies + dependents edges)
├── exporters/
│   ├── compact_exporter.py    GRAPH_COMPACT.md — token-optimized index for agents
│   ├── markdown_exporter.py   GRAPH.md — full human-readable reference
│   ├── html_graph_exporter.py graph.html — D3.js interactive visualization
│   └── json_exporter.py       graph.json + SpringMapEncoder
├── query/
│   └── engine.py             In-memory search, path-finding, project_info(), stats
└── cli.py                    Click CLI — 10 commands, rich terminal output
```

---

## Troubleshooting

**`javalang` fails on some files** — expected for Java 16+ features (records, sealed classes). SpringMap automatically falls back to regex extraction. Run `springmap stats` to see the regex-fallback count.

**Copilot still reads source files** — confirm `GRAPH_COMPACT.md` (not `GRAPH.md` or `graph.json`) is attached, and that your custom instruction explicitly says "search GRAPH_COMPACT.md before opening any .java file." Attaching the graph without that instruction adds tokens on top of normal auto-scanning instead of replacing it.

**OpenAPI nodes not appearing** — SpringMap recursively scans the whole project (skipping `target/`, `build/`, `node_modules/`, `.git/`) for any `.yaml`/`.yml`/`.json` file with a top-level `openapi` or `swagger` key, at any depth, under 5 MB.

**gRPC / Kafka / scheduled jobs missing from `springmap endpoints`** — intentional. They're excluded from the default REST-only view; use `--grpc`, `--listeners`, or `--all`. `.proto` files are discovered recursively from any location under the project root (verified against `src/main/proto/`, `src/main/resources/proto/`, and project-root placements).

**Spring Boot version shows blank** — SpringMap checks, in order: a direct `spring-boot-starter-parent` parent POM, a `spring-boot.version` property, a `spring-boot-dependencies` BOM import in `<dependencyManagement>`, then any direct `org.springframework.boot` dependency with an explicit version. A fully custom internal parent POM with the version pinned only in a grandparent won't resolve from `pom.xml` alone.

**Custom nested properties missing from `springmap info` / `GRAPH.md`** — properties are flattened recursively (`app.kafka.topic`, `app.retry.max-attempts`), up to 6 levels deep. Deeper nesting or list-of-object values aren't flattened.