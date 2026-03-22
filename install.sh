#!/bin/bash
# Installation script for Claude on Rails Review

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "════════════════════════════════════════════════════"
echo "  Claude on Rails Review - Installation"
echo "════════════════════════════════════════════════════"
echo ""

# Check if we're in a project directory
if [ ! -d ".git" ] && [ ! -d ".claude" ]; then
    echo -e "${YELLOW}Warning: No .git or .claude directory found.${NC}"
    echo "Are you in the root of your project? (y/n)"
    read -r response
    if [[ ! "$response" =~ ^[Yy]$ ]]; then
        echo "Exiting. Please run this script from your project root."
        exit 1
    fi
fi

# Create .claude/hooks directory
echo "Creating .claude/hooks directory..."
mkdir -p .claude/hooks

# Copy the hook script
if [ -f "stop-design-audit.py" ]; then
    echo "Copying stop-design-audit.py..."
    cp stop-design-audit.py .claude/hooks/
    chmod +x .claude/hooks/stop-design-audit.py
else
    echo -e "${RED}Error: stop-design-audit.py not found in current directory${NC}"
    echo "Please run this script from the claude-on-rails-review directory"
    exit 1
fi

# Check if settings.json exists
if [ -f ".claude/settings.json" ]; then
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

# Ask about module boundaries
echo ""
echo "Do you want to set up module boundary enforcement? (y/n)"
read -r response
if [[ "$response" =~ ^[Yy]$ ]]; then
    if [ -f "review-config.example.json" ]; then
        echo "Copying example config to .claude/review-config.json..."
        cp review-config.example.json .claude/review-config.json
        echo -e "${GREEN}✓${NC} Created .claude/review-config.json"
        echo "  Please edit this file to match your project structure"
    else
        echo -e "${YELLOW}review-config.example.json not found, skipping...${NC}"
    fi
fi

# Add to .gitignore
echo ""
echo "Adding hook state files to .gitignore..."
if [ -f ".gitignore" ]; then
    if ! grep -q "stop-hook-state" .gitignore; then
        cat >> .gitignore << 'EOF'

# Claude on Rails Review state files
.claude/hooks/stop-hook-state-*.json
.claude/hooks/review-results-*.json
.claude/hooks/stop-hook-debug.log
.claude/hooks/stop-hook-metrics.jsonl
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
.claude/hooks/stop-hook-debug.log
.claude/hooks/stop-hook-metrics.jsonl
EOF
fi

# Check Python version
echo ""
echo "Checking Python version..."
if command -v python3 &> /dev/null; then
    PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
    echo "Found Python $PYTHON_VERSION"

    # Check if version is 3.10+
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

# Test the hook
echo ""
echo "Testing hook installation..."
if python3 .claude/hooks/stop-design-audit.py 2>&1 | grep -q "No transcript_path"; then
    echo -e "${GREEN}✓${NC} Hook script is executable"
else
    echo -e "${RED}✗${NC} Hook test failed"
    echo "Please check the installation manually"
fi

echo ""
echo "════════════════════════════════════════════════════"
echo -e "${GREEN}Installation complete!${NC}"
echo "════════════════════════════════════════════════════"
echo ""
echo "Next steps:"
echo "  1. Review .claude/hooks/stop-design-audit.py configuration"
echo "  2. Edit .claude/review-config.json (if created)"
echo "  3. Start using Claude Code - the hook will run automatically"
echo ""
echo "Documentation:"
echo "  README.md           - Feature overview"
echo "  CONFIGURATION.md    - Configuration guide"
echo "  EXAMPLES.md         - Example configurations"
echo "  TROUBLESHOOTING.md  - Common issues"
echo ""
echo "Need help? Check the documentation or open an issue on GitHub"
echo ""
