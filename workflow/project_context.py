"""Project context management for AI sessions"""

import os
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
import subprocess
import yaml

from workflow.git import get_repo
from workflow.config import load_config


PROJECT_CONTEXT_FILE = Path.home() / ".wf" / "project_context.md"


def project_context_exists() -> bool:
    """Check if project context file already exists"""
    return PROJECT_CONTEXT_FILE.exists()


def load_project_context() -> str:
    """Load existing project context"""
    if project_context_exists():
        return PROJECT_CONTEXT_FILE.read_text()
    return ""


def save_project_context(content: str):
    """Save project context to file"""
    PROJECT_CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROJECT_CONTEXT_FILE.write_text(content)


def generate_project_context() -> str:
    """Generate comprehensive project context"""
    try:
        repo = get_repo()
        repo_path = repo.git_dir.replace("/.git", "")
    except:
        repo_path = os.getcwd()
    
    context_parts = []
    
    # Header
    context_parts.append(f"# Project Context")
    context_parts.append(f"Generated: {datetime.now().isoformat()}")
    context_parts.append(f"Repository: {repo_path}")
    context_parts.append("")
    
    # Project Structure
    context_parts.append("## Project Structure")
    context_parts.append(generate_project_structure(repo_path))
    context_parts.append("")
    
    # Key Files and Directories
    context_parts.append("## Key Files and Directories")
    context_parts.append(generate_key_files_info(repo_path))
    context_parts.append("")
    
    # Dependencies and Configuration
    context_parts.append("## Dependencies and Configuration")
    context_parts.append(generate_dependencies_info(repo_path))
    context_parts.append("")
    
    # Common Patterns and Logic
    context_parts.append("## Common Patterns and Logic")
    context_parts.append(generate_patterns_info(repo_path))
    context_parts.append("")
    
    # Build and Development Commands
    context_parts.append("## Build and Development Commands")
    context_parts.append(generate_commands_info(repo_path))
    context_parts.append("")
    
    # Testing
    context_parts.append("## Testing")
    context_parts.append(generate_testing_info(repo_path))
    context_parts.append("")
    
    # Workflow Configuration
    context_parts.append("## Workflow Configuration")
    context_parts.append(generate_workflow_info())
    context_parts.append("")
    
    return "\n".join(context_parts)


def generate_project_structure(repo_path: str) -> str:
    """Generate project structure overview"""
    try:
        result = subprocess.run(
            ["find", repo_path, "-type", "f", "-name", "*.py", "-o", "-name", "*.js", "-o", "-name", "*.ts", "-o", "-name", "*.jsx", "-o", "-name", "*.tsx", "-o", "-name", "*.go", "-o", "-name", "*.java", "-o", "-name", "*.rb", "-o", "-name", "*.php", "-o", "-name", "*.cpp", "-o", "-name", "*.c", "-o", "-name", "*.h", "-o", "-name", "*.rs", "-o", "-name", "*.swift", "-o", "-name", "*.kt", "-o", "-name", "*.scala", "-o", "-name", "*.cs", "-o", "-name", "*.dart", "-o", "-name", "*.lua", "-o", "-name", "*.r", "-o", "-name", "*.m", "-o", "-name", "*.sh", "-o", "-name", "Dockerfile", "-o", "-name", "Makefile", "-o", "-name", "*.yml", "-o", "-name", "*.yaml", "-o", "-name", "*.json", "-o", "-name", "*.toml", "-o", "-name", "*.cfg", "-o", "-name", "*.ini", "-o", "-name", "*.md"],
            capture_output=True, text=True, timeout=30
        )
        
        if result.returncode == 0:
            files = result.stdout.strip().split('\n')
            # Filter out common directories to ignore
            important_files = [f for f in files if not any(ignore in f for ignore in [
                '/node_modules/', '/.git/', '/venv/', '/env/', '/__pycache__/',
                '/target/', '/build/', '/dist/', '/.pytest_cache/', '/coverage/',
                '/.vscode/', '/.idea/', '/.DS_Store'
            ])]
            
            # Create directory structure
            structure = {}
            for file_path in important_files:
                rel_path = file_path.replace(repo_path + '/', '')
                parts = rel_path.split('/')
                current = structure
                for part in parts[:-1]:
                    if part not in current:
                        current[part] = {}
                    current = current[part]
                current[parts[-1]] = None
            
            return format_structure(structure, "")
        else:
            return "Unable to analyze project structure"
    except Exception as e:
        return f"Error analyzing project structure: {e}"


def format_structure(structure: Dict, indent: str) -> str:
    """Format structure dictionary as readable text"""
    lines = []
    for name, content in sorted(structure.items()):
        lines.append(f"{indent}{name}/" if content is None else f"{indent}{name}")
        if content is not None:
            lines.append(format_structure(content, indent + "  "))
    return "\n".join(lines)


def generate_key_files_info(repo_path: str) -> str:
    """Generate information about key files"""
    info = []
    
    key_files = {
        "README.md": "Project documentation",
        "package.json": "Node.js dependencies and scripts",
        "requirements.txt": "Python dependencies",
        "pyproject.toml": "Python project configuration",
        "Cargo.toml": "Rust project configuration",
        "pom.xml": "Maven Java project configuration",
        "build.gradle": "Gradle Java project configuration",
        "Gemfile": "Ruby dependencies",
        "composer.json": "PHP dependencies",
        "go.mod": "Go modules",
        "Dockerfile": "Docker container configuration",
        "docker-compose.yml": "Docker compose configuration",
        "Makefile": "Build automation",
        ".gitignore": "Git ignore patterns",
        ".env.example": "Environment variables template",
    }
    
    for filename, description in key_files.items():
        filepath = os.path.join(repo_path, filename)
        if os.path.exists(filepath):
            info.append(f"- **{filename}**: {description}")
    
    return "\n".join(info) if info else "No standard key files found"


def generate_dependencies_info(repo_path: str) -> str:
    """Generate dependencies and configuration information"""
    info = []
    
    # Python dependencies
    if os.path.exists(os.path.join(repo_path, "requirements.txt")):
        try:
            with open(os.path.join(repo_path, "requirements.txt")) as f:
                deps = [line.strip() for line in f if line.strip() and not line.startswith('#')]
                if deps:
                    info.append("### Python Dependencies")
                    for dep in deps[:10]:  # Limit to first 10
                        info.append(f"- {dep}")
                    if len(deps) > 10:
                        info.append(f"- ... and {len(deps) - 10} more")
                    info.append("")
        except:
            pass
    
    # Node.js dependencies
    if os.path.exists(os.path.join(repo_path, "package.json")):
        try:
            with open(os.path.join(repo_path, "package.json")) as f:
                package_data = json.load(f)
                deps = package_data.get("dependencies", {})
                dev_deps = package_data.get("devDependencies", {})
                
                if deps or dev_deps:
                    info.append("### Node.js Dependencies")
                    for dep, version in list(deps.items())[:10]:
                        info.append(f"- {dep}: {version}")
                    if len(deps) > 10:
                        info.append(f"- ... and {len(deps) - 10} more")
                    
                    if dev_deps:
                        info.append("\n### Development Dependencies")
                        for dep, version in list(dev_deps.items())[:5]:
                            info.append(f"- {dep}: {version}")
                        if len(dev_deps) > 5:
                            info.append(f"- ... and {len(dev_deps) - 5} more")
                    info.append("")
        except:
            pass
    
    return "\n".join(info) if info else "No dependency files found"


def generate_patterns_info(repo_path: str) -> str:
    """Generate information about common patterns and logic"""
    info = []
    
    # Analyze Python files for common patterns
    python_files = []
    for root, dirs, files in os.walk(repo_path):
        # Skip common ignore directories
        dirs[:] = [d for d in dirs if d not in ['.git', 'node_modules', '__pycache__', 'venv', 'env', 'target', 'build', 'dist']]
        
        for file in files:
            if file.endswith('.py'):
                python_files.append(os.path.join(root, file))
    
    if python_files:
        info.append("### Python Patterns")
        
        # Look for common patterns
        class_patterns = []
        function_patterns = []
        import_patterns = []
        
        for py_file in python_files[:20]:  # Limit analysis
            try:
                with open(py_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    
                    # Look for class definitions
                    import re
                    classes = re.findall(r'^class\s+(\w+)', content, re.MULTILINE)
                    class_patterns.extend(classes)
                    
                    # Look for function definitions
                    functions = re.findall(r'^def\s+(\w+)', content, re.MULTILINE)
                    function_patterns.extend(functions)
                    
                    # Look for common imports
                    imports = re.findall(r'^import\s+(\w+)|^from\s+(\w+)', content, re.MULTILINE)
                    for imp in imports:
                        if imp[0]:
                            import_patterns.append(imp[0])
                        elif imp[1]:
                            import_patterns.append(imp[1])
            except:
                continue
        
        if class_patterns:
            common_classes = list(set(class_patterns))[:10]
            info.append(f"Common classes: {', '.join(common_classes)}")
        
        if function_patterns:
            common_functions = list(set(function_patterns))[:10]
            info.append(f"Common functions: {', '.join(common_functions)}")
        
        if import_patterns:
            common_imports = list(set(import_patterns))[:10]
            info.append(f"Common imports: {', '.join(common_imports)}")
        
        info.append("")
    
    return "\n".join(info) if info else "No common patterns identified"


def generate_commands_info(repo_path: str) -> str:
    """Generate build and development commands information"""
    info = []
    
    # Check package.json scripts
    if os.path.exists(os.path.join(repo_path, "package.json")):
        try:
            with open(os.path.join(repo_path, "package.json")) as f:
                package_data = json.load(f)
                scripts = package_data.get("scripts", {})
                
                if scripts:
                    info.append("### npm scripts")
                    for script, command in scripts.items():
                        info.append(f"- `npm run {script}`: {command}")
                    info.append("")
        except:
            pass
    
    # Check Makefile
    if os.path.exists(os.path.join(repo_path, "Makefile")):
        info.append("### Makefile targets")
        try:
            result = subprocess.run(["make", "-pRrq", ":"], capture_output=True, text=True, cwd=repo_path, timeout=10)
            if result.returncode == 0:
                lines = result.stdout.split('\n')
                targets = []
                for line in lines:
                    if line and not line.startswith('#') and ':' in line and not line.startswith('.'):
                        target = line.split(':')[0].strip()
                        if target and target.isupper() or '.' in target:
                            targets.append(target)
                
                for target in targets[:10]:
                    info.append(f"- `make {target}`")
        except:
            info.append("- `make` (various targets available)")
        info.append("")
    
    # Check pyproject.toml for Python project commands
    if os.path.exists(os.path.join(repo_path, "pyproject.toml")):
        info.append("### Python project commands")
        info.append("- `python -m pip install -e .` (install in development mode)")
        info.append("- `python -m pytest` (run tests)")
        info.append("- `python -m build` (build package)")
        info.append("")
    
    return "\n".join(info) if info else "No build commands identified"


def generate_testing_info(repo_path: str) -> str:
    """Generate testing information"""
    info = []
    
    # Look for test directories and files
    test_indicators = []
    
    for root, dirs, files in os.walk(repo_path):
        # Skip common ignore directories
        dirs[:] = [d for d in dirs if d not in ['.git', 'node_modules', '__pycache__', 'venv', 'env', 'target', 'build', 'dist']]
        
        # Check for test directories
        if 'test' in dirs or 'tests' in dirs:
            test_indicators.append("Test directory found")
        
        # Check for test files
        test_files = [f for f in files if f.startswith('test_') or f.endswith('_test.py') or f.endswith('.test.js') or f.endswith('.spec.js')]
        if test_files:
            test_indicators.append(f"Test files found: {len(test_files)}")
    
    # Check for common testing frameworks
    if os.path.exists(os.path.join(repo_path, "pytest.ini")) or any("pytest" in f for f in os.listdir(repo_path) if f.endswith('.py')):
        test_indicators.append("pytest framework detected")
    
    if os.path.exists(os.path.join(repo_path, "jest.config.js")) or "jest" in os.listdir(repo_path):
        test_indicators.append("Jest framework detected")
    
    if any("unittest" in f for f in os.listdir(repo_path) if f.endswith('.py')):
        test_indicators.append("unittest framework detected")
    
    if test_indicators:
        info.extend(test_indicators)
    else:
        info.append("No testing framework detected")
    
    return "\n".join(info)


def generate_workflow_info() -> str:
    """Generate workflow configuration information"""
    try:
        cfg = load_config()
        info = []
        
        info.append(f"Backend: {cfg.get('task_backend', 'Unknown')}")
        
        if 'jira' in cfg:
            jira_cfg = cfg['jira']
            info.append(f"Jira URL: {jira_cfg.get('url', 'Unknown')}")
            info.append(f"Jira Project: {jira_cfg.get('project', 'Unknown')}")
        
        if 'repositories' in cfg and cfg['repositories']:
            info.append(f"Configured repositories: {len(cfg['repositories'])}")
            for repo_path, repo_config in cfg['repositories'].items():
                base_branch = repo_config.get('base_branch', 'default')
                info.append(f"  - {repo_path} (base: {base_branch})")
        
        info.append(f"Git enabled: {cfg.get('git_enabled', False)}")
        info.append(f"GitHub enabled: {cfg.get('github_enabled', False)}")
        
        return "\n".join(info)
    except:
        return "Unable to load workflow configuration"


def update_project_context_with_session(issue_key: str, session_content: str, learnings: str = ""):
    """Update project context with new learnings from AI session"""
    current_context = load_project_context()
    
    # Add session learnings section
    update_section = f"""
## Session Learnings - {issue_key}
**Date**: {datetime.now().isoformat()}

{learnings}

**Session Summary**:
{session_content[:500]}{"..." if len(session_content) > 500 else ""}

---
"""
    
    # Append to existing context
    updated_context = current_context + update_section
    save_project_context(updated_context)


def ensure_project_context() -> str:
    """Ensure project context exists, generate if needed"""
    if not project_context_exists():
        print("🔍 Generating project context (this may take a moment)...")
        context = generate_project_context()
        save_project_context(context)
        print("✅ Project context generated")
        return context
    else:
        print("📋 Using existing project context")
        return load_project_context()