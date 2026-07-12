# SpringMap

Knowledge graph builder for Spring Boot projects — optimized for LLM coding assistants (GitHub Copilot, Claude, Cursor).

**The problem:** Copilot reads every `.java` file on each question, burning tokens and context window.  
**The solution:** SpringMap produces a `GRAPH.md` so complete — endpoint tables, call chains, DI map, entity schemas — that Copilot can answer most questions without opening any source file.

---

## What gets extracted

| Source | What SpringMap reads |
|--------|---------------------|
| `.java` files | Controllers, services, repos, entities, DI graph, method call chains, HTTP endpoints |
| `pom.xml` / `build.gradle` | Project name, Java version, Spring Boot version, dependencies |
| `application.yml` / `.properties` | Server port, datasource URL, JPA config, custom props |
| `openapi.yaml` / `swagger.yaml` | Virtual controller nodes for code-generated or gateway endpoints |
| `.proto` files | gRPC service definitions and message types |

---

## Setup

### 1. Install

```bash
# From source (one-time)
cd /path/to/springmap
pip install -e .

# Verify
springmap --help
```

### 2. Build the graph

```bash
cd /path/to/your/spring-boot-project
springmap build .
```

Output in `./springmap-out/`:
- `GRAPH_COMPACT.md` — attach **this** to Copilot when coding (token-optimized)
- `GRAPH.md` — full detail, for human reference/browsing
- `graph.html` — interactive D3.js visualization, open in a browser
- `graph.json` — queried by all CLI commands
- `manifest.json` — tracks file hashes for incremental updates

### 3. Configure Copilot to use the graph {#using-springmap-with-copilot}

Attach **`springmap-out/GRAPH_COMPACT.md`**, not `GRAPH.md`. It has the
same structural information (classes, endpoints, DI map, Kafka listeners)
in about half the tokens — no call chains, no entity field tables, no
Maven dependency listing. Copilot needs the index to avoid reading files;
it doesn't need the full documentation view.

Add to `.github/copilot-instructions.md` (or your team's custom instructions):

```
## Project Knowledge Graph

A compact structural index lives in springmap-out/GRAPH_COMPACT.md.

RULE: Before reading any .java source file, search GRAPH_COMPACT.md for:
  - The class name you need (Class Index section, grouped by layer)
  - The endpoint path you need to modify (REST Endpoints table)
  - The Kafka/RabbitMQ topic you need (Event Listeners table)
  - Which class injects which (Injects / Used by lines)

Only open a .java file when you need the actual method body — the
compact index has structure, not implementation.
```

**Does this actually save tokens?** Only if Copilot stops reading source
files because of it — attaching `GRAPH_COMPACT.md` on top of Copilot's
normal auto-file-scanning costs MORE tokens, not less. The instruction
above exists specifically to make Copilot substitute the graph for file
reads, not add to them. For a 60-class project, `GRAPH_COMPACT.md` is
roughly 5,000 tokens versus ~12,000 tokens for Copilot auto-reading 10
Java files per question — but that saving only materializes if the
instruction is followed. Check `springmap stats` periodically; if your
project grows past ~150 classes, `GRAPH_COMPACT.md` itself starts costing
more than targeted file reads, and querying via the CLI (`springmap show
X`, `springmap query "..."`) becomes the better approach for large
codebases.

### 4. Keep the graph current

```bash
# After any code change — re-parses only modified files
springmap update .
```

Add as a git pre-commit hook:

```bash
# .git/hooks/pre-commit
#!/bin/sh
springmap update .
git add springmap-out/GRAPH_COMPACT.md springmap-out/graph.json
```

---

## Command reference

```
springmap [--out <dir>] COMMAND [OPTIONS]
```

`--out` defaults to `./springmap-out`. All commands share this option.

### `build`

Full build from scratch.

```bash
springmap build .
springmap build /path/to/project
springmap build . --out /tmp/my-graph
springmap build . --quiet          # suppress progress bars
```

### `update`

Incremental — only re-parses files that changed since last `build`.

```bash
springmap update .
```

Falls back to full `build` if no existing graph is found.

### `query`

Search the graph without touching any source files.

```bash
springmap query "user authentication"
springmap query "type:service"
springmap query "uses:UserRepository"
springmap query "used-by:UserController"
springmap query "path:/api/users"
springmap query "method:POST"
springmap query "kind:listener"
springmap query "src:openapi"
springmap query "type:service user" --limit 10
springmap query "type:repository" --json    # raw JSON output
```

**Filter syntax** (combinable with keywords):

| Filter | Matches |
|--------|---------|
| `type:service` | Node type: controller, service, repository, entity, component, configuration, dto, util, grpc, openapi |
| `uses:ClassName` | Classes that inject `ClassName` |
| `used-by:ClassName` | Classes that `ClassName` depends on |
| `path:/api/v1` | Endpoints whose path contains `/api/v1` |
| `method:POST` | Verb match — works for REST (`GET`/`POST`/…), gRPC (`RPC`), or listeners (`KAFKA`/`RABBIT`/…) |
| `kind:rest` / `kind:grpc` / `kind:listener` | Restrict endpoint matches to one category |
| `pkg:com.example` | Package starts with prefix |
| `src:openapi` | Nodes sourced from OpenAPI YAML (not Java) |
| `src:proto` | Nodes sourced from .proto files |

### `show`

Full details for one class — metadata, endpoint table, all methods with call chains, entity fields.

```bash
springmap show UserService
springmap show UserController
springmap show User             # entity
```

Supports partial/case-insensitive name matching and suggests alternatives if not found.

### `path`

Shortest dependency path between two classes (BFS across the DI graph).

```bash
springmap path UserController UserRepository
springmap path OrderController EmailService
```

Output shows the full chain with relationship labels:

```
UserController → UserService → UserRepository
```

### `endpoints`

List endpoints. **Defaults to REST only** — gRPC and message-listener methods
are never mixed into the default view; opt into them explicitly with flags.

```bash
springmap endpoints                     # REST only (GET/POST/PUT/DELETE/PATCH)
springmap endpoints --method POST
springmap endpoints --filter "/api/v1"
springmap endpoints --grpc              # gRPC RPCs from .proto files, only
springmap endpoints --listeners         # @KafkaListener/@RabbitListener/@SqsListener/
                                         # @JmsListener/@EventListener/@Scheduled, only
springmap endpoints --all               # REST + gRPC + listeners in one table
```

`--method` accepts any verb relevant to the category you're viewing — e.g.
`--method KAFKA` together with `--listeners`, or `--method RPC` with `--grpc`.

### `stats`

Parse quality report and size metrics, with REST/gRPC/listener endpoint
counts broken out separately (never combined into one ambiguous total).

```bash
springmap stats
```

### `info`

Deep breakdown of `pom.xml` + `application.yml`. Cross-references your
datasource URL against your dependencies — flags it if the URL says
`postgresql` but no PostgreSQL driver exists in the POM. Every detected
Spring starter is annotated with graph-derived counts (e.g. "2 REST
endpoints", "1 @KafkaListener").

```bash
springmap info
springmap info .
```

Shows: Maven coordinates, Java/Spring Boot versions, server port, context
path, active profiles, datasource URL + driver + inferred DB type, JPA
settings, every dependency grouped by category (starters, database,
messaging, security, testing), and all custom `application.yml` properties
flattened to dot notation (`app.kafka.topic`, `app.retry.max-attempts`, …).

### `graph`

Generates an interactive D3.js force-directed graph of the entire project
as a single self-contained HTML file — opens in any browser, no server
needed.

```bash
springmap graph
springmap graph --open     # generate + open in default browser
```

Features: nodes color-coded and shaped by Spring layer (controller = blue
circle, service = green rect, repository = amber diamond, entity = purple
hexagon, gRPC = magenta, Kafka consumer = dashed orange), click any node
for a detail panel (file, endpoints, DI dependencies, listeners), filter
by layer, search by class name, toggle DI/event edges and labels, drag to
reposition, zoom/pan.

This is for human/team use — architecture reviews, onboarding, tracing
data flows visually. It is not meant to be attached to Copilot; use
`GRAPH_COMPACT.md` for that (see below).

### `clean`

Delete the `springmap-out/` directory.

```bash
springmap clean
springmap clean --yes    # skip confirmation
```

---

## Output files

### `GRAPH_COMPACT.md` — attach this to Copilot

The token-optimized index. ~55% smaller than `GRAPH.md` — only class names,
types, files, DI edges, REST endpoint table, event listener table, and
plain method names. No call chains, no entity field details, no Maven
section. This is the file to attach to Copilot Chat or reference in
`.github/copilot-instructions.md`. See [Using SpringMap with Copilot](#using-springmap-with-copilot) below.

### `graph.html`

Self-contained interactive D3.js visualization — open in any browser, no
server required. For humans, not for Copilot: architecture reviews, new
engineer onboarding, tracing data flows, spotting over-coupled classes.
Generated automatically by `build`/`update`, or on demand with `springmap graph`.

### `GRAPH.md`

Structured for LLM consumption. Sections:

1. Project overview (tech stack, counts, config)
2. **REST endpoint table** — every endpoint from Java controllers AND OpenAPI specs, with body type, return type
3. **gRPC Services** — RPC methods from `.proto` files (only present if any exist)
4. **Event Listeners & Scheduled Jobs** — Kafka/RabbitMQ/SQS/JMS/`@EventListener`/`@Scheduled` methods, kept separate from REST endpoints (only present if any exist)
5. **Controllers** — base paths, endpoint tables, non-endpoint methods
6. **Services** — DI dependencies, method signatures and call chains, `@Transactional` markers
7. **Repositories** — extends, custom query methods
8. **Entities** — table name, field list with types / columns / JPA relationships
9. **Other components** (DTOs, configs, utils, exceptions, consumers)
10. **Configuration** — server port, datasource, JPA settings, custom props
11. **Dependency map** — textual call chains (Controller → Service → Repository)
12. **Maven dependencies**

Use this as human-readable reference documentation. It's larger than
`GRAPH_COMPACT.md` on purpose — it's meant to be read, not attached to
every Copilot request.

### `graph.json`

Full serialized graph. Queried by all CLI commands. Never attach this to
Copilot directly — it's larger than `GRAPH.md` and JSON syntax overhead
wastes tokens versus the markdown exports. Schema:

```json
{
  "project_name": "my-service",
  "classes": {
    "UserService": {
      "name": "UserService",
      "node_type": "service",
      "file_path": "src/main/java/.../UserService.java",
      "dependencies": ["UserRepository", "EmailService"],
      "dependents": ["UserController"],
      "methods": [
        {
          "name": "createUser",
          "return_type": "UserDTO",
          "parameters": [{"type": "UserCreateDTO", "name": "dto"}],
          "calls": ["UserRepository.save()", "EmailService.sendWelcome()"],
          "is_transactional": true,
          "signature": "UserDTO createUser(UserCreateDTO dto)"
        }
      ]
    }
  }
}
```

---

## Architecture

```
springmap/
├── parser/
│   ├── java_parser.py      AST (javalang) + regex fallback
│   ├── pom_parser.py       Maven / Gradle metadata
│   ├── config_parser.py    application.yml / .properties
│   ├── openapi_parser.py   OpenAPI / Swagger YAML → virtual nodes
│   └── proto_parser.py     .proto gRPC services → virtual nodes
├── graph/
│   ├── models.py           ClassNode, MethodInfo, ProjectGraph, …
│   └── builder.py          Orchestrates parsers + post-parse passes
│       ├── Interface-driven discovery (endpoints on interface → class)
│       ├── Constructor injection detection
│       └── Dependency resolution (dependencies + dependents edges)
├── exporters/
│   ├── markdown_exporter.py  LLM-optimized GRAPH.md
│   └── json_exporter.py      graph.json + SpringMapEncoder
├── query/
│   └── engine.py           In-memory search, path-finding, stats
└── cli.py                  Click CLI with rich output
```

---

## Troubleshooting

**`javalang` fails on some files** — expected for Java 16+ features (records, sealed classes). SpringMap automatically falls back to regex extraction. Run `springmap stats` to see the regex fallback count.

**Copilot still reads source files** — check that `GRAPH.md` is attached to the Copilot context and that the custom instruction is set. The instruction must explicitly say "search GRAPH.md before opening any .java file."

**OpenAPI nodes not appearing** — SpringMap recursively scans the entire project (skipping `target/`, `build/`, `node_modules/`, `.git/`) for any `.yaml`/`.yml`/`.json` file containing a top-level `openapi` or `swagger` key, at any nesting depth. If a spec still isn't picked up, confirm it actually has `openapi:` or `swagger:` as a real top-level YAML/JSON key (not just mentioned in a comment), and check the file is under 5 MB.

**gRPC / Kafka / scheduled jobs missing from `springmap endpoints`** — these are intentionally excluded from the default view. Use `springmap endpoints --grpc`, `springmap endpoints --listeners`, or `springmap endpoints --all` to see them. `springmap stats` always shows REST/gRPC/listener counts broken out separately.

**Spring Boot version shows blank** — SpringMap checks, in order: a direct `spring-boot-starter-parent` parent POM, a `spring-boot.version` property, a `spring-boot-dependencies` BOM import in `<dependencyManagement>`, then any direct `org.springframework.boot` dependency with an explicit version. If your build uses a fully custom internal parent with no Spring Boot version pinned anywhere in the POM itself (inherited transitively from a grandparent), it won't be resolvable from `pom.xml` alone.