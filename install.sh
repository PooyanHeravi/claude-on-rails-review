#!/bin/bash
# Installation script for Claude on Rails Review
# Usage:
#   Interactive:      bash install.sh
#   Non-interactive:  bash install.sh --non-interactive [--preset=balanced]

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# =============================================================================
# Parse flags
# =============================================================================
NON_INTERACTIVE=false
PRESET=""
for arg in "$@"; do
    case "$arg" in
        --non-interactive) NON_INTERACTIVE=true ;;
        --preset=*) PRESET="${arg#--preset=}" ;;
        --help|-h)
            echo "Usage: bash install.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --non-interactive  Skip all prompts (for automated install by Claude)"
            echo "  --preset=NAME      Set initial preset (strict|balanced|relaxed|minimal)"
            echo "  --help, -h         Show this help"
            exit 0
            ;;
    esac
done

echo "════════════════════════════════════════════════════"
echo "  Claude on Rails Review - Installation"
if $NON_INTERACTIVE; then
    echo "  (non-interactive mode)"
fi
echo "════════════════════════════════════════════════════"
echo ""

# =============================================================================
# Check if we're in a project directory
# =============================================================================
if [ ! -d ".git" ] && [ ! -d ".claude" ]; then
    if $NON_INTERACTIVE; then
        echo -e "${YELLOW}Warning: No .git or .claude directory found. Proceeding anyway.${NC}"
    else
        echo -e "${YELLOW}Warning: No .git or .claude directory found.${NC}"
        echo "Are you in the root of your project? (y/n)"
        read -r response
        if [[ ! "$response" =~ ^[Yy]$ ]]; then
            echo "Exiting. Please run this script from your project root."
            exit 1
        fi
    fi
fi

# =============================================================================
# Create .claude/hooks directory
# =============================================================================
echo "Creating .claude/hooks directory..."
mkdir -p .claude/hooks

# =============================================================================
# Copy the hook shim and package
# =============================================================================
# Determine source directory (where install.sh lives)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "$SCRIPT_DIR/stop-design-audit.py" ] && [ -d "$SCRIPT_DIR/stop_design_audit" ]; then
    echo "Copying stop-design-audit.py (shim)..."
    cp "$SCRIPT_DIR/stop-design-audit.py" .claude/hooks/
    chmod +x .claude/hooks/stop-design-audit.py
    echo "Copying stop_design_audit/ package..."
    rm -rf .claude/hooks/stop_design_audit
    cp -r "$SCRIPT_DIR/stop_design_audit" .claude/hooks/stop_design_audit
else
    echo -e "${RED}Error: stop-design-audit.py or stop_design_audit/ not found in $SCRIPT_DIR${NC}"
    exit 1
fi

# =============================================================================
# Configure settings.json
# =============================================================================
HOOK_CMD="python .claude/hooks/stop-design-audit.py"

if [ -f ".claude/settings.json" ]; then
    # Check if hook is already registered
    if grep -q "stop-design-audit.py" .claude/settings.json 2>/dev/null; then
        echo -e "${GREEN}✓${NC} Hook already registered in .claude/settings.json"
    elif $NON_INTERACTIVE; then
        # Try to merge using python
        echo "Merging hook into existing .claude/settings.json..."
        python3 -c "
import json, sys
try:
    with open('.claude/settings.json', 'r') as f:
        settings = json.load(f)
    hooks = settings.setdefault('hooks', {})
    stop_hooks = hooks.setdefault('stop', [])
    entry = {'command': '$HOOK_CMD', 'timeout': 30000}
    if not any('stop-design-audit.py' in h.get('command', '') for h in stop_hooks):
        stop_hooks.append(entry)
    with open('.claude/settings.json', 'w') as f:
        json.dump(settings, f, indent=2)
    print('Merged hook into settings.json')
except Exception as e:
    print(f'Could not auto-merge: {e}', file=sys.stderr)
    print('Add this to .claude/settings.json manually:')
    print(json.dumps({'hooks': {'stop': [entry]}}, indent=2))
    sys.exit(1)
" 2>&1 && echo -e "${GREEN}✓${NC} Updated .claude/settings.json" || true
    else
        echo -e "${YELLOW}Warning: .claude/settings.json already exists${NC}"
        echo "Do you want to add the stop hook to it? (y/n)"
        read -r response
        if [[ "$response" =~ ^[Yy]$ ]]; then
            echo "Please manually add this to your .claude/settings.json:"
            echo ""
            echo '  "hooks": {'
            echo '    "stop": [{'
            echo '      "command": "python .claude/hooks/stop-design-audit.py",'
            echo '      "timeout": 30000'
            echo '    }]'
            echo '  }'
            echo ""
        fi
    fi
else
    echo "Creating .claude/settings.json..."
    cat > .claude/settings.json << 'EOF'
{
  "hooks": {
    "stop": [{
      "command": "python .claude/hooks/stop-design-audit.py",
      "timeout": 30000
    }]
  }
}
EOF
fi

# =============================================================================
# Write hook-overrides.json with preset (if specified)
# =============================================================================
if [ -n "$PRESET" ]; then
    VALID_PRESETS="strict balanced relaxed minimal"
    if echo "$VALID_PRESETS" | grep -qw "$PRESET"; then
        echo "Writing hook-overrides.json with preset: $PRESET"
        # Only write if file doesn't exist (don't overwrite custom config)
        if [ ! -f ".claude/hooks/hook-overrides.json" ]; then
            cat > .claude/hooks/hook-overrides.json << EOF
{
    "preset": "$PRESET"
}
EOF
            echo -e "${GREEN}✓${NC} Created .claude/hooks/hook-overrides.json"
        else
            echo -e "${YELLOW}hook-overrides.json already exists, skipping preset write${NC}"
        fi
    else
        echo -e "${YELLOW}Warning: Unknown preset '$PRESET'. Valid: $VALID_PRESETS${NC}"
    fi
fi

# =============================================================================
# Module boundaries (interactive only)
# =============================================================================
if ! $NON_INTERACTIVE; then
    echo ""
    echo "Do you want to set up module boundary enforcement? (y/n)"
    read -r response
    if [[ "$response" =~ ^[Yy]$ ]]; then
        if [ -f "$SCRIPT_DIR/review-config.example.json" ]; then
            echo "Copying example config to .claude/review-config.json..."
            cp "$SCRIPT_DIR/review-config.example.json" .claude/review-config.json
            echo -e "${GREEN}✓${NC} Created .claude/review-config.json"
            echo "  Please edit this file to match your project structure"
        else
            echo -e "${YELLOW}review-config.example.json not found, skipping...${NC}"
        fi
    fi
fi

# =============================================================================
# Add to .gitignore
# =============================================================================
echo ""
echo "Adding hook state files to .gitignore..."
if [ -f ".gitignore" ]; then
    if ! grep -q "stop-hook-state" .gitignore; then
        cat >> .gitignore << 'EOF'

# Claude on Rails Review state files
.claude/hooks/stop-hook-state-*.json
.claude/hooks/review-results-*.json
.claude/hooks/coordinator-instructions-*.json
.claude/hooks/stop-hook-debug.log
.claude/hooks/stop-hook-metrics.jsonl
.claude/hooks/hook-overrides.json
EOF
        echo -e "${GREEN}✓${NC} Updated .gitignore"
    else
        echo "Already in .gitignore, skipping..."
    fi
else
    echo -e "${YELLOW}No .gitignore found, creating one...${NC}"
    cat > .gitignore << 'EOF'
# Claude on Rails Review state files
.claude/hooks/stop-hook-state-*.json
.claude/hooks/review-results-*.json
.claude/hooks/coordinator-instructions-*.json
.claude/hooks/stop-hook-debug.log
.claude/hooks/stop-hook-metrics.jsonl
.claude/hooks/hook-overrides.json
EOF
fi

# =============================================================================
# Check Python version
# =============================================================================
echo ""
echo "Checking Python version..."
if command -v python3 &> /dev/null; then
    PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
    echo "Found Python $PYTHON_VERSION"

    MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
    MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

    if [ "$MAJOR" -lt 3 ] || ([ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 10 ]); then
        echo -e "${YELLOW}Warning: Python 3.10+ is required${NC}"
        echo "You have Python $PYTHON_VERSION"
    else
        echo -e "${GREEN}✓${NC} Python version is compatible"
    fi
else
    echo -e "${RED}Error: python3 not found${NC}"
    echo "Please install Python 3.10 or higher"
fi

# =============================================================================
# Test the hook (interactive only)
# =============================================================================
if ! $NON_INTERACTIVE; then
    echo ""
    echo "Testing hook installation..."
    if python3 .claude/hooks/stop-design-audit.py 2>&1 | grep -q "No transcript_path"; then
        echo -e "${GREEN}✓${NC} Hook script is executable"
    else
        echo -e "${RED}✗${NC} Hook test failed"
        echo "Please check the installation manually"
    fi
fi

# =============================================================================
# Done
# =============================================================================
echo ""
echo "════════════════════════════════════════════════════"
echo -e "${GREEN}Installation complete!${NC}"
echo "════════════════════════════════════════════════════"
echo ""
if $NON_INTERACTIVE; then
    echo "Next steps:"
    echo "  1. Create/edit .claude/hooks/hook-overrides.json for project-specific config"
    echo "  2. See SETUP.md for customization guide"
else
    echo "Next steps:"
    echo "  1. Review .claude/hooks/hook-overrides.json (or create one — see SETUP.md)"
    echo "  2. Edit .claude/review-config.json (if created)"
    echo "  3. Start using Claude Code - the hook will run automatically"
    echo ""
    echo "Documentation:"
    echo "  SETUP.md             - Setup guide (for Claude or humans)"
    echo "  README.md            - Feature overview"
    echo "  CONFIGURATION.md     - Configuration guide"
    echo "  EXAMPLES.md          - Example configurations"
    echo "  TROUBLESHOOTING.md   - Common issues"
fi
echo ""
