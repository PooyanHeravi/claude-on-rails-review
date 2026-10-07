# Configuration Examples

Real-world `hook-overrides.json` examples for different project types and workflows. All examples are valid for 3.0.0: they use one `reviewer` per tier, with the checklist, models and thresholds as the main controls.

Put overrides in `.claude/hooks/hook-overrides.json`, and a checklist file next to it in `.claude/hooks/`. Add `.claude/hooks/state/` to `.gitignore`.

## Table of Contents

- [Microservices Project](#microservices-project)
- [Frontend Application](#frontend-application)
- [Python Library](#python-library)
- [Rapid Prototyping](#rapid-prototyping)
- [Production System](#production-system)
- [Open Source Project](#open-source-project)
- [Monorepo](#monorepo)
- [Team Size](#team-size)
- [Tips](#tips)

## Microservices Project

### Overview
Multiple services with strict boundaries, gRPC communication, shared core library.

### Project Structure
```
project/
├── api/           # REST API gateway
├── services/      # gRPC microservices
├── core/          # Shared models
├── proto/         # Protocol buffers
└── frontend/      # Web UI
```

### Configuration

**.claude/settings.json:**
```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "python .claude/hooks/stop-design-audit.py",
        "timeout": 30
      }]
    }]
  }
}
```

**.claude/hooks/hook-overrides.json:**
```json
{
  "preset": "balanced",
  "review_checklist_file": "review-checklist.md",
  "tier_thresholds": {"skip": 300, "quick": 2000, "standard": 10000},
  "tier_file_limits": {"skip": 1, "quick": 2, "standard": 5},
  "+critical_patterns": ["/api/", "/services/", "/proto/", "_service.py", "/models/"],
  "max_auto_continues": 2,
  "max_fail_retries": 2
}
```

Changes touching two or more of the critical patterns (or two top-level directories) make the reviewer check callers and contracts across those boundaries.

**.claude/hooks/review-checklist.md** (replaces the built-in checklist, so it repeats the generic items):
```markdown
1. Silent failures - swallowed exceptions, defaults returned on failure.
2. Correctness - null access, race conditions, resource leaks, edge cases.
3. Proto compatibility - field numbering, enum values, no breaking changes without a version bump.
4. Service boundaries - services talk through gRPC interfaces, never import each other.
5. Every RPC validates its request and maps errors to status codes.
6. Bug fixes include a regression test.
```

**.claude/review-config.json:**
```json
{
  "module_boundaries": {
    "api": {
      "forbidden_imports": ["services", "frontend"],
      "communication": "Must use gRPC client to communicate with services"
    },
    "services": {
      "forbidden_imports": ["api", "frontend"],
      "communication": "Services communicate via gRPC only"
    },
    "frontend": {
      "forbidden_imports": ["api", "services", "core"],
      "communication": "Must use REST API only, no direct imports"
    },
    "core": {
      "forbidden_imports": ["api", "services", "frontend"],
      "communication": "Core is a shared library with no dependencies"
    }
  }
}
```

## Frontend Application

### Overview
React application with API calls and state management, no backend code.

### Configuration

**.claude/hooks/hook-overrides.json:**
```json
{
  "preset": "relaxed",
  "reviewer_model": "sonnet",
  "+critical_patterns": ["/api/", "/hooks/", "/context/", "provider.tsx"],
  "+excluded_paths": ["/__snapshots__/", "/storybook-static/"]
}
```

Path patterns are matched lower-case, hence `provider.tsx`. The built-in `reviewer` already adds React-specific focus (hook dependencies, state batching, memory leaks) for `.ts`/`.tsx`/`.js`/`.jsx` files under `/frontend/`.

To add a second opinion on the deep tier only:

```json
{
  "preset": "relaxed",
  "extra_agent_definitions": {
    "frontend_specialist": {
      "subagent_type": "general-purpose",
      "model": "sonnet",
      "checks": "React hook dependencies, state updates, memory leaks, XSS vulnerabilities",
      "context_checks": {
        "frontend": "Check useMemo/useCallback usage, event handler cleanup, ref management"
      }
    }
  },
  "agent_ids": {
    "deep": ["reviewer", "frontend_specialist"]
  }
}
```

## Python Library

### Overview
Reusable Python package where public API stability is critical.

### Configuration

**.claude/hooks/hook-overrides.json:**
```json
{
  "preset": "strict",
  "review_checklist_file": "review-checklist.md",
  "+critical_patterns": ["/__init__.py", "/core.py"],
  "+excluded_paths": ["/examples/", "/docs/"],
  "max_fail_retries": 1
}
```

**.claude/hooks/review-checklist.md:**
```markdown
1. Breaking changes to the public API (names, signatures, return types, exceptions).
2. Missing or inconsistent type hints and docstrings on public functions.
3. Silent failures - swallowed exceptions, defaults returned on failure.
4. Behaviour changes without a test; bug fixes without a regression test.
```

## Rapid Prototyping

### Overview
Early-stage project; move fast, light review.

### Configuration

**.claude/hooks/hook-overrides.json:**
```json
{
  "preset": "minimal"
}
```

`minimal` raises the thresholds, allows 10 auto-continues, auto-fixes every deep finding and runs the reviewer on `sonnet`. To keep the reviewer on `opus`:

```json
{
  "preset": "minimal",
  "reviewer_model": "opus"
}
```

## Production System

### Overview
Mission-critical system, maximum rigor.

### Configuration

**.claude/hooks/hook-overrides.json:**
```json
{
  "preset": "strict",
  "review_checklist_file": "review-checklist.md",
  "tier_thresholds": {"skip": 0, "quick": 500, "standard": 3000},
  "tier_file_limits": {"skip": 0, "quick": 1, "standard": 3},
  "max_auto_continues": 1,
  "max_fail_retries": 1,
  "deep_auto_fix": "none",
  "extra_agent_definitions": {
    "security_checker": {
      "subagent_type": "general-purpose",
      "model": "opus",
      "checks": "SQL injection, XSS, CSRF, auth bypass, secrets exposure, input validation",
      "context_checks": {
        "api_routes": "Check authentication, authorization, rate limiting",
        "database": "Check query parameterization, connection security"
      }
    },
    "performance_checker": {
      "subagent_type": "general-purpose",
      "model": "sonnet",
      "checks": "N+1 queries, inefficient algorithms, memory leaks, unbounded loops"
    }
  },
  "agent_ids": {
    "standard": ["reviewer", "security_checker"],
    "deep": ["reviewer", "security_checker", "performance_checker"]
  }
}
```

With `skip` at 0 there is no skip tier: every change is reviewed. `deep_auto_fix: "none"` makes deep failures report-only so a human decides. Extra agents run in parallel with the reviewer in the same round, and the round fails if any of them fails.

## Open Source Project

### Overview
Community-driven; consistent quality, helpful for contributors.

### Configuration

**.claude/hooks/hook-overrides.json:**
```json
{
  "preset": "balanced",
  "tier_thresholds": {"skip": 500, "quick": 3000, "standard": 15000},
  "+critical_patterns": ["/src/api/", "/src/core/", "/__init__.py"],
  "+excluded_paths": ["/examples/", "/docs/", "/tutorials/"],
  "review_checklist_file": "review-checklist.md"
}
```

**.claude/hooks/review-checklist.md:**
```markdown
1. Bugs and silent failures, with a suggested fix for each.
2. Missing tests for changed behaviour.
3. Compliance with CONTRIBUTING.md (style, naming, commit scope).
4. Public API compatibility.
```

## Monorepo

### Overview
Multiple projects in one repo, different standards per project.

### Project Structure
```
monorepo/
├── packages/     # libraries: ui-lib, api-client, utils
├── apps/         # web, mobile
└── services/     # api, workers
```

### Configuration

**.claude/hooks/hook-overrides.json:**
```json
{
  "preset": "balanced",
  "tier_thresholds": {"skip": 500, "quick": 3000, "standard": 15000},
  "+critical_patterns": [
    "/packages/ui-lib/src",
    "/packages/api-client/src",
    "/services/api/",
    "/services/workers/"
  ]
}
```

**.claude/review-config.json:**
```json
{
  "module_boundaries": {
    "packages": {
      "forbidden_imports": ["apps", "services"],
      "communication": "Packages are libraries - no app/service dependencies"
    },
    "apps": {
      "forbidden_imports": ["services"],
      "communication": "Apps use packages and call services via API"
    },
    "services": {
      "forbidden_imports": ["apps"],
      "communication": "Services can use packages but not app code"
    }
  }
}
```

## Team Size

**Solo developer:**
```json
{ "preset": "minimal" }
```

**Small team:**
```json
{ "preset": "balanced" }
```

**Large team, strict consistency:**
```json
{ "preset": "strict", "max_auto_continues": 1 }
```

## Tips

1. **Start conservative** - begin with `strict` or tight thresholds, relax with evidence from `.claude/hooks/state/stop-hook-metrics.jsonl`.
2. **Invest in the checklist** - one good project checklist beats extra agents.
3. **Extra agents cost a full agent run per round** - add one only for a concern the checklist cannot express.
4. **Team consensus** - agree on thresholds with the team and commit `hook-overrides.json`; keep `.claude/hooks/state/` out of git.
5. **Provenance** - every metrics record carries the hook `version` and a `config_fingerprint`, so you can tell which configuration produced a review.

## Getting Help

Can't find an example for your use case? Open an issue describing your project type, team size, pain points and desired behavior.
