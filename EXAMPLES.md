# Configuration Examples

Real-world configuration examples for different project types and workflows.

## Table of Contents

- [Microservices Project](#microservices-project)
- [Frontend Application](#frontend-application)
- [Python Library](#python-library)
- [Rapid Prototyping](#rapid-prototyping)
- [Production System](#production-system)
- [Open Source Project](#open-source-project)
- [Monorepo](#monorepo)

## Microservices Project

### Overview
Multiple services with strict boundaries, gRPC communication, shared core library.

### Project Structure
```
project/
├── api/           # REST API gateway
├── services/      # gRPC microservices
│   ├── auth/
│   ├── users/
│   └── orders/
├── core/          # Shared models
├── proto/         # Protocol buffers
└── frontend/      # Web UI
```

### Configuration

**.claude/settings.json:**
```json
{
  "hooks": {
    "stop": [{
      "command": "python .claude/hooks/stop-design-audit.py",
      "timeout": 30000
    }]
  }
}
```

**stop-design-audit.py configuration:**
```python
# Results mode - inline (default) or file
RESULTS_MODE = "inline"

# Strict thresholds for service boundaries
TIER_THRESHOLDS = {
    "skip": 300,
    "quick": 2000,
    "standard": 10000,
}

TIER_FILE_LIMITS = {
    "skip": 1,
    "quick": 2,
    "standard": 5,
}

# Critical patterns - always deep review
CRITICAL_PATTERNS = [
    "/api/",
    "/services/",
    "/proto/",
    "_service.py",
    "/models/",
]

# Conservative auto-continue
MAX_AUTO_CONTINUES = 2
MAX_FAIL_RETRIES = 2
```

**.claude/review-config.json:**
```json
{
  "module_boundaries": {
    "api": {
      "allowed_imports": ["core"],
      "forbidden_imports": ["services", "frontend"],
      "communication": "Must use gRPC client to communicate with services"
    },
    "services": {
      "allowed_imports": ["core"],
      "forbidden_imports": ["api", "frontend"],
      "communication": "Services communicate via gRPC only"
    },
    "frontend": {
      "allowed_imports": [],
      "forbidden_imports": ["api", "services", "core"],
      "communication": "Must use REST API only, no direct imports"
    },
    "core": {
      "allowed_imports": [],
      "forbidden_imports": ["api", "services", "frontend"],
      "communication": "Core is a shared library with no dependencies"
    }
  }
}
```

**Custom agent for proto files:**
```python
AGENT_DEFINITIONS["proto_checker"] = {
    "subagent_type": "general-purpose",
    "model": "sonnet",
    "checks": "protobuf field numbering, breaking changes, enum values",
    "context_checks": {
        "proto": "Check backward compatibility, field deprecation"
    }
}

AGENT_IDS["deep"] = [
    "explore_haiku",
    "general_haiku",
    "bug_hunter",
    "general_sonnet",
    "proto_checker"  # Added
]
```

## Frontend Application

### Overview
React application with API calls, state management, no backend code.

### Project Structure
```
frontend/
├── src/
│   ├── components/
│   ├── pages/
│   ├── hooks/
│   ├── utils/
│   └── api/
├── tests/
└── public/
```

### Configuration

**stop-design-audit.py configuration:**
```python
# Results mode - inline (default) or file
RESULTS_MODE = "inline"

# Relaxed thresholds for frontend iteration
TIER_THRESHOLDS = {
    "skip": 1000,
    "quick": 5000,
    "standard": 20000,
}

# More auto-continues for rapid UI development
MAX_AUTO_CONTINUES = 5

# Frontend-specific critical patterns
CRITICAL_PATTERNS = [
    "/api/",
    "/hooks/",
    "/context/",
    "Provider.tsx",
]

# Exclude UI snapshot tests
EXCLUDED_PATHS = [
    "/tests/fixtures/",
    "/__snapshots__/",
    "/storybook-static/",
    "/coverage/",
]

# Frontend-focused agents
AGENT_IDS = {
    "quick": ["explore_haiku"],
    "standard": ["explore_haiku", "frontend_specialist"],
    "deep": ["explore_haiku", "frontend_specialist", "bug_hunter"],
}
```

**Custom frontend agent:**
```python
AGENT_DEFINITIONS["frontend_specialist"] = {
    "subagent_type": "general-purpose",
    "model": "haiku",
    "checks": "React hooks dependencies, state updates, memory leaks, XSS vulnerabilities",
    "context_checks": {
        "frontend": "Check useMemo/useCallback usage, event handler cleanup, ref management"
    }
}
```

## Python Library

### Overview
Reusable Python package, public API stability is critical.

### Project Structure
```
mylib/
├── mylib/
│   ├── __init__.py
│   ├── core.py
│   └── utils.py
├── tests/
├── docs/
└── examples/
```

### Configuration

**stop-design-audit.py configuration:**
```python
# Results mode - inline (default) or file
RESULTS_MODE = "inline"

# Strict for public API changes
TIER_THRESHOLDS = {
    "skip": 200,      # Very small changes
    "quick": 1000,
    "standard": 5000,
}

# Critical: any change to __init__.py (public API)
CRITICAL_PATTERNS = [
    "/__init__.py",
    "/core.py",
]

# Strict retry limits
MAX_AUTO_CONTINUES = 1
MAX_FAIL_RETRIES = 1

# Exclude examples and docs from review
EXCLUDED_PATHS = [
    "/examples/",
    "/docs/",
    "/tests/fixtures/",
]

# Focus on API stability and backward compatibility
AGENT_DEFINITIONS["api_checker"] = {
    "subagent_type": "general-purpose",
    "model": "sonnet",
    "checks": "breaking changes to public API, missing docstrings, type hint consistency",
    "context_checks": {}
}

AGENT_IDS = {
    "quick": ["explore_haiku", "api_checker"],
    "standard": ["explore_haiku", "api_checker", "bug_hunter"],
    "deep": ["explore_haiku", "api_checker", "bug_hunter", "general_sonnet"],
}
```

## Rapid Prototyping

### Overview
Early-stage project, move fast, less strict review.

### Configuration

**stop-design-audit.py configuration:**
```python
# Results mode - inline (default) or file
RESULTS_MODE = "inline"

# Very lenient thresholds
TIER_THRESHOLDS = {
    "skip": 3000,      # Skip most changes
    "quick": 10000,
    "standard": 50000,
}

TIER_FILE_LIMITS = {
    "skip": 5,
    "quick": 10,
    "standard": 20,
}

# Many auto-continues
MAX_AUTO_CONTINUES = 10

# Only use fast agents
AGENT_IDS = {
    "quick": ["explore_haiku"],
    "standard": ["explore_haiku"],
    "deep": ["explore_haiku", "general_haiku"],
}

# Minimal critical patterns
CRITICAL_PATTERNS = []

# Only catch serious bugs
AGENT_DEFINITIONS["explore_haiku"]["checks"] = "critical bugs, security issues only"
```

## Production System

### Overview
Mission-critical system, maximum rigor, comprehensive review.

### Configuration

**stop-design-audit.py configuration:**
```python
# Results mode - inline (default) or file
RESULTS_MODE = "inline"

# No skip tier - always review
TIER_THRESHOLDS = {
    "skip": 0,         # Everything reviewed
    "quick": 500,
    "standard": 3000,
}

TIER_FILE_LIMITS = {
    "skip": 0,
    "quick": 1,
    "standard": 3,
}

# Very conservative
MAX_AUTO_CONTINUES = 1
MAX_FAIL_RETRIES = 1

# Everything is critical
CRITICAL_PATTERNS = [
    "/",  # Match all files
]

# Maximum agents at all tiers
AGENT_IDS = {
    "quick": ["explore_haiku", "general_haiku"],
    "standard": ["explore_haiku", "general_haiku", "bug_hunter", "security_checker"],
    "deep": [
        "explore_haiku",
        "general_haiku",
        "bug_hunter",
        "general_sonnet",
        "security_checker",
        "performance_checker"
    ],
}

# Add security-focused agent
AGENT_DEFINITIONS["security_checker"] = {
    "subagent_type": "general-purpose",
    "model": "sonnet",
    "checks": "SQL injection, XSS, CSRF, auth bypass, secrets exposure, input validation",
    "context_checks": {
        "api_routes": "Check authentication, authorization, rate limiting",
        "database": "Check query parameterization, connection security"
    }
}

# Add performance agent
AGENT_DEFINITIONS["performance_checker"] = {
    "subagent_type": "general-purpose",
    "model": "sonnet",
    "checks": "N+1 queries, inefficient algorithms, memory leaks, unbounded loops",
    "context_checks": {
        "database": "Check index usage, query efficiency"
    }
}
```

## Open Source Project

### Overview
Community-driven, need consistent quality, helpful for contributors.

### Configuration

**stop-design-audit.py configuration:**
```python
# Results mode - inline (default) or file
RESULTS_MODE = "inline"

# Moderate thresholds
TIER_THRESHOLDS = {
    "skip": 500,
    "quick": 3000,
    "standard": 15000,
}

# Standard auto-continue
MAX_AUTO_CONTINUES = 3

# Critical patterns focus on public API
CRITICAL_PATTERNS = [
    "/src/api/",
    "/src/core/",
    "/__init__.py",
]

# Exclude contributor-friendly areas from strict review
EXCLUDED_PATHS = [
    "/examples/",
    "/docs/",
    "/tutorials/",
    "/tests/fixtures/",
]

# Friendly agent messaging
AGENT_DEFINITIONS["explore_haiku"]["checks"] = (
    "bugs, style issues (with suggestions for fixes), "
    "missing tests, unclear variable names"
)

# Context-aware help
AGENT_DEFINITIONS["contributor_helper"] = {
    "subagent_type": "general-purpose",
    "model": "haiku",
    "checks": "contribution guidelines compliance, code style, test coverage",
    "context_checks": {}
}

AGENT_IDS["quick"] = ["explore_haiku", "contributor_helper"]
```

## Monorepo

### Overview
Multiple projects in one repo, different standards per project.

### Project Structure
```
monorepo/
├── packages/
│   ├── ui-lib/       # UI component library
│   ├── api-client/   # API client
│   └── utils/        # Shared utilities
├── apps/
│   ├── web/          # Web application
│   └── mobile/       # Mobile app
└── services/
    ├── api/          # Backend API
    └── workers/      # Background workers
```

### Configuration

**stop-design-audit.py configuration:**
```python
# Results mode - inline (default) or file
RESULTS_MODE = "inline"

# Moderate defaults
TIER_THRESHOLDS = {
    "skip": 500,
    "quick": 3000,
    "standard": 15000,
}

# Project-specific critical patterns
CRITICAL_PATTERNS = [
    # Libraries (strict)
    "/packages/ui-lib/src",
    "/packages/api-client/src",
    "/packages/utils/src",

    # Backend (strict)
    "/services/api/",
    "/services/workers/",

    # Less strict for apps
    # (not listed)
]

# Context detection for monorepo
def detect_project_context(files: list[str]) -> str:
    """Detect which project the changes belong to."""
    for f in files:
        if "/packages/" in f:
            return "library"
        elif "/services/" in f:
            return "backend"
        elif "/apps/" in f:
            return "frontend"
    return "mixed"
```

**.claude/review-config.json:**
```json
{
  "module_boundaries": {
    "packages": {
      "allowed_imports": [],
      "forbidden_imports": ["apps", "services"],
      "communication": "Packages are libraries - no app/service dependencies"
    },
    "apps": {
      "allowed_imports": ["packages"],
      "forbidden_imports": ["services"],
      "communication": "Apps use packages and call services via API"
    },
    "services": {
      "allowed_imports": ["packages"],
      "forbidden_imports": ["apps"],
      "communication": "Services can use packages but not app code"
    }
  }
}
```

## Environment-Specific Configuration

### Development
```python
# Fast iteration
TIER_THRESHOLDS = {"skip": 1000, "quick": 5000, "standard": 20000}
MAX_AUTO_CONTINUES = 5
```

### Staging
```python
# Balanced
TIER_THRESHOLDS = {"skip": 500, "quick": 3000, "standard": 15000}
MAX_AUTO_CONTINUES = 3
```

### Production
```python
# Strict
TIER_THRESHOLDS = {"skip": 200, "quick": 1000, "standard": 5000}
MAX_AUTO_CONTINUES = 1
```

## Team Size Considerations

### Solo Developer
```python
# More lenient, trust yourself
MAX_AUTO_CONTINUES = 10
TIER_THRESHOLDS = {"skip": 2000, "quick": 10000, "standard": 50000}
```

### Small Team (2-5)
```python
# Moderate review
MAX_AUTO_CONTINUES = 3
TIER_THRESHOLDS = {"skip": 500, "quick": 3000, "standard": 15000}
```

### Large Team (10+)
```python
# Strict consistency
MAX_AUTO_CONTINUES = 1
TIER_THRESHOLDS = {"skip": 200, "quick": 1000, "standard": 5000}
```

## Integration Examples

### With Pre-commit Hooks

```bash
# .git/hooks/pre-commit
#!/bin/bash

# Run review on staged changes
git diff --cached --name-only | python .claude/hooks/check-files.py

# Helper script: check-files.py
# (Custom script that uses stop-design-audit.py logic)
```

### With CI/CD

```yaml
# .github/workflows/review.yml
name: AI Code Review

on: [pull_request]

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Run Claude Review
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          # Generate transcript from PR diff
          # Run stop-design-audit.py
          # Post results as PR comment
```

## Results Mode Configuration

All examples above use `RESULTS_MODE = "inline"` (the default). This section explains both modes.

### Inline Mode (Default)

```python
RESULTS_MODE = "inline"
```

- **No extra permissions needed**
- Claude embeds results in response with markers:
  ```
  <!--REVIEW_RESULTS_START-->
  {"round_id": "abc12345", "agents": {...}}
  <!--REVIEW_RESULTS_END-->
  ```
- Hook parses transcript to extract results
- Recommended for most users

### File Mode

```python
RESULTS_MODE = "file"
```

- **Requires permission** in `.claude/settings.local.json`:
  ```json
  {
    "permissions": {
      "allow": ["Write(.claude/hooks/review-results-*.json)"]
    }
  }
  ```
- Claude writes results to `.claude/hooks/review-results-{hash}.json`
- Useful for external tooling integration
- Cleaner file-based results

### When to Use File Mode

Consider file mode if:
- You have external tools that need to read review results
- You prefer persistent result files for auditing
- Your CI/CD pipeline needs to process results
- You're debugging and want to inspect raw result files

For most interactive use, inline mode works well and requires no extra setup.

## Tips

1. **Start conservative** - Begin strict, relax as needed
2. **Measure metrics** - Use `stop-hook-metrics.jsonl` to tune
3. **Team consensus** - Agree on thresholds with team
4. **Iterate** - Adjust based on real usage patterns
5. **Document** - Explain why you chose certain values

## Getting Help

Can't find an example for your use case? Open an issue describing:
- Your project type/structure
- Team size and workflow
- Pain points with current settings
- Desired behavior
