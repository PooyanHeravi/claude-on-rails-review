# Contributing to Claude on Rails Review

Thank you for your interest in contributing! This document provides guidelines and instructions for contributing to this project.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [How Can I Contribute?](#how-can-i-contribute)
- [Development Setup](#development-setup)
- [Coding Guidelines](#coding-guidelines)
- [Testing](#testing)
- [Pull Request Process](#pull-request-process)

## Code of Conduct

This project follows a simple code of conduct:

- **Be respectful** - Treat all contributors with respect
- **Be constructive** - Provide helpful feedback and suggestions
- **Be collaborative** - Work together to make the project better
- **Be inclusive** - Welcome contributors of all skill levels

## How Can I Contribute?

### Reporting Bugs

Before creating a bug report:

1. **Check existing issues** to avoid duplicates
2. **Test with latest version** to ensure bug still exists
3. **Gather debug info** (logs, config files, reproduction steps)

Create a bug report including:

- Clear, descriptive title
- Steps to reproduce the issue
- Expected vs actual behavior
- Debug logs (`.claude/hooks/stop-hook-debug.log`)
- Configuration (sanitized `.claude/settings.json`)
- Environment (OS, Python version, Claude Code version)

### Suggesting Enhancements

Enhancement suggestions are welcome! Include:

- Clear description of the enhancement
- Use case and motivation
- Example configuration or code (if applicable)
- Potential implementation approach (optional)

### Contributing Code

We welcome code contributions! Areas that need help:

- **New agent types** - Specialized review agents for specific domains
- **Language support** - Expand beyond Python (TypeScript, Shell, etc.)
- **Integration features** - CI/CD integration, IDE plugins
- **Performance improvements** - Faster parsing, caching, parallel processing
- **Documentation** - Examples, tutorials, guides
- **Testing** - Unit tests, integration tests, edge case coverage

## Development Setup

### Prerequisites

- Python 3.10 or higher
- Git
- Claude Code CLI

### Local Setup

1. **Fork and clone:**

```bash
git clone https://github.com/PooyanHeravi/claude-on-rails-review.git
cd claude-on-rails-review
```

2. **Create test project:**

```bash
mkdir test-project
cd test-project
mkdir -p .claude/hooks
cp ../stop-design-audit.py .claude/hooks/
```

3. **Configure Claude Code:**

Create `.claude/settings.json`:

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

**Optional: For file mode results**, create `.claude/settings.local.json`:

```json
{
  "permissions": {
    "allow": ["Write(.claude/hooks/review-results-*.json)"]
  }
}
```

4. **Test the hook:**

```bash
# Create a test transcript
mkdir -p /tmp/claude-test
cat > /tmp/claude-test/transcript.jsonl << 'EOF'
{"type":"tool_use","name":"Edit","input":{"file_path":"test.py","old_string":"x = 1","new_string":"x = 2"}}
EOF

# Run hook with test input
echo '{"transcript_path":"/tmp/claude-test/transcript.jsonl"}' | python .claude/hooks/stop-design-audit.py
```

## Coding Guidelines

### Python Style

Follow PEP 8 with these specifics:

```python
# Use type hints
def parse_transcript(path: str, offset: int = 0) -> dict:
    ...

# Docstrings for public functions
def classify_tier(diff: int, files: int) -> str:
    """Classify review tier based on changes.

    Args:
        diff: Character count difference
        files: Number of files modified

    Returns:
        Tier name: "skip", "quick", "standard", or "deep"
    """
    ...

# Constants in UPPER_CASE
MAX_AUTO_CONTINUES = 3
TIER_THRESHOLDS = {...}
RESULTS_MODE = "inline"  # or "file"

# Private functions start with _
def _calculate_diff(tool_input: dict) -> int:
    ...
```

### Code Organization

The script is organized into sections:

```python
# 1. Configuration - All user-customizable settings
# 2. Transcript Parsing - Reading and parsing transcript
# 3. Tier Classification - Determining review depth
# 4. State Management - Session state persistence
# 5. API Mode - Fallback API implementation
# 6. Main - Entry point and orchestration
```

Keep related functions together and add clear section headers.

### Configuration Design

New configuration options should:

1. Have sensible defaults
2. Be documented in docstrings
3. Be grouped logically
4. Include examples in comments

```python
# -----------------------------------------------------------------------------
# New Feature Settings
# -----------------------------------------------------------------------------
# Brief description of what this controls and why.
# Example: FEATURE_ENABLED = True
FEATURE_ENABLED = False  # Disabled by default
FEATURE_THRESHOLD = 100  # Clear units
```

### Error Handling

- **Never crash the hook** - Always exit cleanly (`sys.exit(0)`)
- **Log errors** - Use `log()` function for debugging
- **Fail safe** - On error, allow stop (don't block user)

```python
try:
    risky_operation()
except SpecificError as e:
    log(f"Error in risky_operation: {e}")
    sys.exit(0)  # Allow stop, don't block
```

## Testing

### Manual Testing

1. **Test all tiers:**

Create changes of different sizes and verify correct tier classification:

```python
# Skip: <500 chars, 1 file
# Quick: 500-5000 chars, ≤3 files
# Standard: 5000-20000 chars, ≤6 files
# Deep: ≥20000 chars or >6 files
```

2. **Test state persistence:**

- Stop hook multiple times in same session
- Verify counters increment correctly
- Test state reset on new session

3. **Test module boundaries:**

Create violations and verify detection:

```python
# Should be caught
from forbidden_module import something
```

4. **Test edge cases:**

- Empty transcript
- Malformed JSON
- Missing files
- Very large diffs

5. **Test results modes:**

Test both inline and file modes:

```python
# Test inline mode (default)
RESULTS_MODE = "inline"
# Claude outputs: <!--REVIEW_RESULTS_START-->...<!--REVIEW_RESULTS_END-->
# Hook parses transcript for markers

# Test file mode
RESULTS_MODE = "file"
# Claude writes to: .claude/hooks/review-results-{hash}.json
# Requires permission in settings.local.json
```

Verify:
- Inline mode: Results extracted from transcript correctly
- File mode: Results file created with correct format
- Mode switching: Both modes produce equivalent behavior

### Automated Testing

We currently don't have automated tests, but contributions welcome! Ideal test structure:

```
tests/
├── test_parsing.py      # Transcript parsing tests
├── test_tiers.py        # Tier classification tests
├── test_state.py        # State management tests
└── fixtures/            # Sample transcripts, configs
```

## Pull Request Process

### Before Submitting

1. **Test thoroughly** - Verify changes work as expected
2. **Update documentation** - README, CONFIGURATION, etc.
3. **Follow code style** - PEP 8, type hints, docstrings
4. **Add examples** - Show how to use new features
5. **Check for sensitive data** - No API keys, personal info

### PR Description Template

```markdown
## Description
Brief description of changes

## Motivation
Why is this change needed?

## Changes
- Change 1
- Change 2

## Testing
How was this tested?

## Screenshots (if applicable)
Show output/behavior

## Checklist
- [ ] Code follows style guidelines
- [ ] Documentation updated
- [ ] Tested manually
- [ ] No breaking changes (or documented)
```

### Review Process

1. **Maintainer review** - Code and documentation review
2. **Testing** - Verify changes work as described
3. **Discussion** - Address any questions or concerns
4. **Merge** - Once approved, PR will be merged

### After Merge

- Your contribution will be credited in release notes
- Consider helping review other PRs
- Share your experience with the community

## Documentation

### Updating Documentation

When adding features, update:

- **README.md** - If user-facing feature
- **CONFIGURATION.md** - If adding config options
- **TROUBLESHOOTING.md** - If addressing common issues
- **Inline comments** - For complex logic

### Documentation Style

- Use **clear, concise** language
- Include **examples** for all features
- Add **code blocks** with proper syntax highlighting
- Use **tables** for comparing options
- Add **links** to related sections

Example:

```markdown
### Feature Name

Brief description of what it does.

**Configuration:**

```python
FEATURE_SETTING = value  # Description
```

**Example:**

Shows how to use the feature.

**See also:** [Related Section](#related-section)
```

## Community

### Communication

- **GitHub Issues** - Bug reports, feature requests
- **Pull Requests** - Code contributions, documentation
- **Discussions** - Questions, ideas, feedback

### Recognition

Contributors are recognized in:

- Release notes
- Contributors section (coming soon)
- GitHub contributor graph

## Questions?

If you have questions about contributing:

1. Check existing documentation
2. Search closed issues for similar discussions
3. Open a new issue with the "question" label

Thank you for contributing to Claude on Rails Review! 🚂
