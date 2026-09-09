#!/usr/bin/env python3
import sys
import os
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)

from workflow.terminal import open_terminal_tabs

# Test with a single repository
repos = {
    REPO_ROOT: {
        'base_branch': 'development'
    }
}

print("Testing terminal tab creation...")
open_terminal_tabs(repos, 'TEST-123')
print("Test completed!")