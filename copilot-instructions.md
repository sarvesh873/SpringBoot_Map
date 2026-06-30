# Engineering defaults: build the least, not the most

**Step 0 — check the active mode.** Read `.github/copilot-mode.md`. If it doesn't exist or doesn't parse, treat the mode as `full`. The mode changes how the ladder below is enforced:

| Mode | Behavior |
|------|----------|
| `lite` | Build the requested solution. If a simpler stdlib/native/existing-dependency alternative exists, mention it in one line — don't block on it. |
| `full` (default) | Apply the ladder below. Take the lowest rung that resolves the task. If you skip a more robust approach, name it in a one-line comment. |
| `ultra` | Refuse anything above the lowest working rung without justification. Push back on the requirement itself if a smaller change covers the actual need — including "we might need this later," which is not justification. |
| `off` | Ignore the ladder below. Behave as a normal coding assistant. |

To switch modes, run `/lazy-mode` in chat, or edit `.github/copilot-mode.md` directly. The change takes effect on the next response and holds until changed again — this file and the mode file are both re-read on every turn, so there's no need to restart anything.

Before adding new code, work down this list and stop at the first step that resolves the task:

1. **Necessary now?** If the need is speculative ("might want this later"), skip it.
2. **Already exists in this codebase?** Check existing `@Service`, `@Repository`, mapper, and util classes before writing a new one. A new `XService`/`XUtil` that duplicates one a few packages over is the most common waste in a Spring codebase.
3. **Java standard library?** `java.time`, `java.util.*`, `Optional`, the Streams API — prefer these over a new dependency or hand-rolled utility.
4. **Does Spring/Spring Boot already solve this?** Check before writing custom code:
   - Validation → Bean Validation (`@Valid`, `@NotNull`, `@Size`, custom `@Constraint`) on the DTO — not manual if-checks in the controller.
   - Error responses → one `@RestControllerAdvice` / `@ExceptionHandler` — not a try-catch per endpoint.
   - Config → `@ConfigurationProperties` + `application.yml` — not a hand-parsed properties file.
   - Data access → a Spring Data JPA derived query or `@Query` method — not a new `JdbcTemplate` call or DAO class.
   - Entity/DTO mapping → MapStruct/ModelMapper if already a dependency — not a new manual mapper.
   - Cross-cutting concerns (logging, auth, retry) → an existing `@Aspect`, filter, or interceptor — not duplicated logic per service method.
5. **Already a dependency in `pom.xml`/`build.gradle`?** Use it. Don't add a library for what Lombok, Jackson, or an existing starter already covers.
6. **One line / one small method?** Do that instead of a new class, interface, or abstraction layer.
7. Only after 1–6: write the smallest amount of new code that correctly solves the task.

## Never cut, regardless of the above

- `@Valid` / `@Validated` at controller and service boundaries.
- Transaction boundaries (`@Transactional`) and rollback semantics.
- Exception handling that distinguishes client errors (4xx) from server errors (5xx).
- **Customer data handling**: no logging of PII/sensitive fields (check the logging config before adding a log line), no widening of a repository query or DTO's exposed fields beyond what the endpoint needs, never bypass an existing masking/encryption/redaction utility to save a line.
- Spring Security configuration — authentication, authorization, CORS/CSRF settings.
- Accessibility on any server-rendered (Thymeleaf/JSP) views.

## In practice

- Don't write a manual null-check cascade where `@Valid` plus the DTO's constraints already enforce it.
- Don't add a new exception class if one in the existing `exception` package already fits.
- Don't write a per-controller response wrapper — use the project's existing response envelope or `@RestControllerAdvice`.
- Don't add a new repository method that duplicates one that's a derived-query rename away.
- Don't introduce a new bean/interface for something a `@Component` two files over already does.

## When reviewing a diff (or via the /trim-the-fat prompt)

Flag as suggested deletions: duplicate service/repository logic, hand-rolled validation Bean Validation already covers, new DTOs/mappers overlapping existing ones, and dependencies added for something a Spring Boot starter already provides.