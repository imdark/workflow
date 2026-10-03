from git import Repo as GitRepo
import os
import subprocess
import json
from github import Github
from pathlib import Path
from typing import Optional, List


def add_claude_trust(path: str):
    """Add a directory to Claude's trusted projects"""
    import subprocess
    claude_json = Path.home() / ".claude.json"
    if not claude_json.exists():
        return
    
    try:
        # Read current config
        config = json.loads(claude_json.read_text())
        
        # Backup the config
        backup_path = claude_json.with_suffix('.json.bak')
        backup_path.write_text(json.dumps(config, indent=2))
        
        if "projects" not in config:
            config["projects"] = {}
        
        # Add the path as trusted
        config["projects"][path] = {"hasTrustDialogAccepted": True}
        
        # Write back
        claude_json.write_text(json.dumps(config, indent=2))
    except Exception:
        pass

def get_repo(path: Optional[str] = None):
    if path:
        return GitRepo(path, search_parent_directories=True)
    return GitRepo(search_parent_directories=True)

def clone_repository(url: str, target_path: str) -> str:
    """Clone a repository from URL to target path"""
    try:
        from rich.console import Console
        from rich.progress import Progress, SpinnerColumn, TextColumn
        
        console = Console(color_system=None)
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            task = progress.add_task(f"Cloning {url}...", total=None)
            
            repo = GitRepo.clone_from(url, target_path)
            
        console.print(f"✅ Successfully cloned repository to {target_path}")
        return target_path
        
    except Exception as e:
        raise RuntimeError(f"Failed to clone repository: {e}")

def is_github_url(url: str) -> bool:
    """Check if the provided URL is a GitHub URL"""
    return url.startswith(('https://github.com/', 'git@github.com:', 'github.com/'))

def extract_repo_name_from_url(url: str) -> str:
    """Extract repository name from GitHub URL"""
    # Remove .git extension if present
    url = url.replace('.git', '')
    
    # Extract the last part after /
    if '/' in url:
        return url.split('/')[-1]
    return url

def generate_clone_path(repo_name: str, base_code_dir: Optional[str] = None) -> str:
    """Generate a path for cloning repository"""
    from pathlib import Path
    
    if not base_code_dir:
        base_code_dir = str(Path.home() / "code")
    
    base_path = Path(base_code_dir)
    target_path = base_path / repo_name
    
    # If target path exists, add a number suffix
    counter = 1
    while target_path.exists():
        target_path = base_path / f"{repo_name}-{counter}"
        counter += 1
    
    return str(target_path)

def ensure_code_directory(base_code_dir: Optional[str] = None) -> str:
    """Ensure code directory exists or create it"""
    from pathlib import Path
    from rich.console import Console
    
    if not base_code_dir:
        base_code_dir = str(Path.home() / "code")
    
    base_path = Path(base_code_dir)
    
    if not base_path.exists():
        console = Console(color_system=None)
        console.print(f"📁 Creating code directory: {base_path}")
        base_path.mkdir(parents=True, exist_ok=True)
    elif not base_path.is_dir():
        raise RuntimeError(f"Path exists but is not a directory: {base_path}")
    
    return str(base_path)

def has_uncommitted_changes(repo_path: Optional[str] = None) -> bool:
    """Check if repository has uncommitted changes (staged or unstaged)"""
    repo = get_repo(repo_path)
    return repo.is_dirty(untracked_files=True) or len(repo.index.diff("HEAD")) > 0

def get_uncommitted_changes_summary(repo_path: Optional[str] = None) -> str:
    """Get a summary of uncommitted changes"""
    repo = get_repo(repo_path)
    changes = []
    
    # Staged changes
    if len(repo.index.diff("HEAD")) > 0:
        staged_count = len(repo.index.diff("HEAD"))
        changes.append(f"{staged_count} staged")
    
    # Unstaged changes
    if repo.is_dirty():
        # Count modified files
        modified_files = [item.a_path for item in repo.index.diff(None)]
        changes.append(f"{len(modified_files)} unstaged")
    
    # Untracked files
    untracked_files = repo.untracked_files
    if untracked_files:
        changes.append(f"{len(untracked_files)} untracked")
    
    return ", ".join(changes) if changes else "no changes"

def get_default_branch(repo_path: Optional[str] = None) -> str:
    """Get the best default branch for the repository with priority order"""
    available_branches = get_available_branches(repo_path)
    standard_branches = ["main", "master", "develop", "dev", "development"]
    
    for branch in standard_branches:
        if branch in available_branches:
            return branch
    
    # Fallback to first available branch
    return available_branches[0] if available_branches else "main"

def fuzzy_match_branches(available_branches: List[str], query: str, limit: int = 10) -> List[str]:
    """Simple fuzzy matching for branch names"""
    if not query:
        return available_branches[:limit]
    
    query = query.lower()
    matches = []
    
    for branch in available_branches:
        branch_lower = branch.lower()
        score = 0
        
        # Exact match gets highest score
        if branch_lower == query:
            score = 100
        # Starts with query gets high score
        elif branch_lower.startswith(query):
            score = 80
        # Contains query gets medium score
        elif query in branch_lower:
            score = 60
        # Partial character matching gets lower score
        else:
            # Simple character sequence matching
            query_chars = list(query)
            branch_chars = list(branch_lower)
            char_index = 0
            matched_chars = 0
            
            for char in branch_chars:
                if char_index < len(query_chars) and char == query_chars[char_index]:
                    matched_chars += 1
                    char_index += 1
            
            if matched_chars > 0:
                score = (matched_chars / len(query_chars)) * 40
        
        if score > 0:
            matches.append((branch, score))
    
    # Sort by score (descending) and then by name
    matches.sort(key=lambda x: (-x[1], x[0]))
    
    return [branch for branch, score in matches[:limit]]

def get_available_branches(repo_path: Optional[str] = None) -> List[str]:
    """Get list of available branches in the repository (local only for speed)"""
    repo = get_repo(repo_path)
    # Only get local branches for much better performance
    branches = set()
    
    # Local branches only
    for branch in repo.branches:
        branches.add(branch.name)
    
    return sorted(list(branches))

def branch_exists(branch_name: str, repo_path: Optional[str] = None) -> bool:
    """Check if a branch exists in the repository"""
    available_branches = get_available_branches(repo_path)
    return branch_name in available_branches

def has_origin_remote(repo_path: Optional[str] = None) -> bool:
    """Check if the repository has an 'origin' remote configured"""
    repo = get_repo(repo_path)
    try:
        return any(remote.name == 'origin' for remote in repo.remotes)
    except:
        return False

def select_base_branch(preferred_branch: str, repo_path: Optional[str] = None) -> str:
    """Select an appropriate base branch, falling back if preferred doesn't exist"""
    if branch_exists(preferred_branch, repo_path):
        return preferred_branch
    
    available_branches = get_available_branches(repo_path)
    if not available_branches:
        raise RuntimeError("No branches found in repository")
    
    from rich.console import Console
    from rich.prompt import Prompt
    
    console = Console(color_system=None)
    
    console.print(f"⚠️  Branch '{preferred_branch}' not found. Available branches:")
    
    # Show all branches initially
    for branch in available_branches:
        console.print(f"  • {branch}")
    
    while True:
        query = Prompt.ask(
            f"Type branch name or part of name to search (or 'cancel' to abort)",
            default=""
        ).strip()
        
        if query.lower() == 'cancel':
            raise RuntimeError("Branch selection cancelled")
        
        if not query:
            # If no query, show standard branches first
            standard_branches = ["main", "master", "develop", "dev", "development"]
            standard_found = [b for b in standard_branches if b in available_branches]
            if standard_found:
                console.print(f"Suggested standard branches: {', '.join(standard_found)}")
                for branch in standard_found:
                    console.print(f"  • {branch}")
                query = Prompt.ask(f"Select one of these or search again", default=standard_found[0]).strip()
                if query in standard_found:
                    console.print(f"✅ Using '{query}' as base branch")
                    return query
            continue
        
        # Get fuzzy matched branches
        matched_branches = fuzzy_match_branches(available_branches, query, limit=5)
        
        if not matched_branches:
            console.print(f"❌ No branches found matching '{query}'")
            console.print("Try a different search term or type 'cancel' to abort")
            continue
        
        if len(matched_branches) == 1:
            # Single match, use it
            selected_branch = matched_branches[0]
            console.print(f"✅ Using '{selected_branch}' as base branch")
            return selected_branch
        else:
            # Multiple matches, show them
            console.print(f"Found {len(matched_branches)} branches matching '{query}':")
            for i, branch in enumerate(matched_branches, 1):
                console.print(f"  {i}. {branch}")
            
            choice = Prompt.ask(
                f"Select branch (1-{len(matched_branches)}) or type more to refine search",
                choices=[str(i) for i in range(1, len(matched_branches) + 1)] + ["more"]
            )
            
            if choice == "more":
                # Continue loop with same query but show more results
                more_matches = fuzzy_match_branches(available_branches, query, limit=15)
                if len(more_matches) > len(matched_branches):
                    console.print(f"All matches for '{query}':")
                    for i, branch in enumerate(more_matches, 1):
                        console.print(f"  {i}. {branch}")
                    
                    choice = Prompt.ask(
                        f"Select branch (1-{len(more_matches)})",
                        choices=[str(i) for i in range(1, len(more_matches) + 1)]
                    )
                    
                    selected_index = int(choice) - 1
                    selected_branch = more_matches[selected_index]
                    console.print(f"✅ Using '{selected_branch}' as base branch")
                    return selected_branch
            else:
                selected_index = int(choice) - 1
                selected_branch = matched_branches[selected_index]
                console.print(f"✅ Using '{selected_branch}' as base branch")
                return selected_branch

def create_branch(issue, base, repo_path: Optional[str] = None, skip_uncommitted_check: bool = False):
    import sys
    from rich.console import Console
    from rich.prompt import Prompt
    
    repo = get_repo(repo_path)
    console = Console(color_system=None)
    
    # Generate initial branch name
    from workflow.session import task_branch_name
    base_name = task_branch_name(issue)
    
    # Check for uncommitted changes before switching branches
    if not skip_uncommitted_check and has_uncommitted_changes(repo_path):
        current_branch = repo.active_branch.name
        changes_summary = get_uncommitted_changes_summary(repo_path)
        
        console.print(f"⚠️  You have {changes_summary} changes on branch '{current_branch}'")
        console.print("📝 Switching branches will lose these changes if not committed.")
        
        proceed = Prompt.ask(
            "Do you want to continue anyway, shelve changes, or cancel?",
            choices=["y", "s", "n"],
            default="n"
        )
        
        if proceed.lower() == "s":
            # Shelve the changes
            if not shelve_changes(repo_path):
                raise RuntimeError("Failed to shelve changes, branch creation cancelled")
            console.print(f"🔄 Switching branches (changes shelved)")
        elif proceed.lower() != "y":
            raise RuntimeError("Branch creation cancelled due to uncommitted changes")
    
    # Select appropriate base branch
    selected_base = select_base_branch(base, repo_path)
    
    # Checkout the base branch
    if not checkout_branch(selected_base, repo_path):
        raise RuntimeError(f"Failed to checkout base branch '{selected_base}'")
    
    # Try to create branch with loop for existing names
    name = base_name
    suffix = 1
    
    while True:
        try:
            repo.git.checkout("-b", name)
            console.print(f"✅ Created and switched to branch '{name}'")
            return name
        except Exception as e:
            if "already exists" in str(e):
                console.print(f"⚠️  Branch '{name}' already exists")
                
                # Suggest alternative name
                if suffix == 1:
                    # First time suggesting with suffix
                    suggested_name = f"{base_name}-{suffix}"
                else:
                    # Increment suffix for subsequent attempts
                    suggested_name = f"{base_name}-{suffix}"
                
                console.print(f"💡 Suggested alternative: {suggested_name}")
                
                # Ask user for input
                user_input = Prompt.ask(
                    "Enter a different branch name (or press Enter to use suggestion, 'cancel' to abort)",
                    default=suggested_name
                ).strip()
                
                if user_input.lower() == 'cancel':
                    raise RuntimeError("Branch creation cancelled by user")
                
                if not user_input:
                    # Use the suggested name
                    name = suggested_name
                    suffix += 1
                else:
                    # Use user's input
                    name = user_input
                    # Reset suffix counter since user provided custom name
                    if name.startswith(base_name):
                        try:
                            # Try to extract suffix if user entered something like "name-2"
                            existing_suffix = int(name.split('-')[-1])
                            suffix = existing_suffix + 1
                        except (ValueError, IndexError):
                            suffix = 1
                    else:
                        suffix = 1
            else:
                # Some other error occurred
                raise e

def set_commit_prefix(key, repo_path: Optional[str] = None):
    repo = get_repo(repo_path)
    repo.config_writer().set_value("commit", "template", f"[{key}] ").release()

def commit_all(issue, repo_path: Optional[str] = None):
    repo = get_repo(repo_path)
    repo.git.add(all=True)
    repo.git.commit("-m", f"[{issue.key}] {issue.title}")

def push_branch(repo_path: Optional[str] = None):
    repo = get_repo(repo_path)
    repo.git.push("--set-upstream", "origin", repo.active_branch.name)

def has_diff_with_base(repo_path: Optional[str] = None, base_branch: Optional[str] = None):
    """Check if current branch has differences with base branch that are pushed to remote"""
    if not repo_path:
        return False
        
    repo = get_repo(repo_path)
    
    # Get current branch
    current_branch = repo.active_branch.name
    
    # First check if branch exists on remote
    try:
        repo.git.fetch("origin", current_branch)
        remote_branch = f"origin/{current_branch}"
        # Check if commits exist on remote that aren't on base
        diff_commits = list(repo.iter_commits(f'{base_branch}..{remote_branch}'))
        return len(diff_commits) > 0
    except Exception:
        # Branch doesn't exist on remote yet
        return False

def create_pr(issue, reviewers: Optional[str] = None, repo_path: Optional[str] = None, base_branch=None):
    """Create a pull request using GitHub CLI"""
    import subprocess
    import json
    from rich.console import Console
    from pathlib import Path
    
    console = Console(color_system=None)
    
    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True
    )
    current_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    
    if not current_branch:
        console.print("❌ Could not determine current branch")
        return None
    
    git_repo_path = repo_path or "."
    
    repo = get_repo(repo_path)
    
    # Use default branch if none specified
    if not base_branch:
        base_branch = get_default_branch(repo_path)
    
    # Validate that the base branch exists
    if not branch_exists(base_branch, repo_path):
        console.print(f"⚠️  Base branch '{base_branch}' not found for PR creation")
        
        # Select an alternative base branch
        selected_base = select_base_branch(base_branch, repo_path)
        console.print(f"📝 Creating PR against '{selected_base}' instead of '{base_branch}'")
        base_branch = selected_base
    
    # Build GitHub CLI command
    pr_title = f"{issue.key} {issue.title}"
    pr_body = issue.description or ""
    
    cmd = [
        "gh", "pr", "create",
        "--title", pr_title,
        "--body", pr_body,
        "--base", base_branch,
        "--head", current_branch
    ]
    
# Add reviewers if provided
    if reviewers:
        reviewer_list = [r.strip() for r in reviewers.split(",")]
        cmd.extend(["--reviewer", *reviewer_list])
    
    console.print(f"🚀 Checking for existing pull requests for {issue.key}...")
    
    # Check if PR already exists for this issue with a more robust approach
    repo_path_for_cmd = repo_path or "."
    existing_prs_cmd = ["gh", "pr", "list", "--head", current_branch, "--repo", str(Path(repo_path_for_cmd).absolute()), "--json", "number,title,url"]
    existing_result = subprocess.run(existing_prs_cmd, capture_output=True, text=True, cwd=repo_path)
    
    if existing_result.returncode == 0:
        try:
            existing_prs = json.loads(existing_result.stdout.strip())
            expected_title = f"{issue.key} {issue.title}"
            
            for pr in existing_prs:
                # Check if PR title starts with the issue key (most reliable method)
                if pr.get("title", "").startswith(issue.key):
                    console.print(f"⚠️  Pull request for {issue.key} already exists. Skipping creation.")
                    console.print(f"🔗 URL: {pr['url']}")
                    return {"url": "existing", "existing": pr["url"], "pr_data": pr}
        except (json.JSONDecodeError, KeyError):
            # Fallback to the original string-based method if JSON parsing fails
            existing_output = existing_result.stdout.strip()
            if issue.key.lower() in existing_output.lower():
                console.print(f"⚠️  Pull request for {issue.key} already exists. Skipping creation.")
                return {"url": "existing", "existing": existing_output}
    
    console.print(f"🚀 Creating new pull request for {issue.key}...")
    
    try:
        # Run GitHub CLI command
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=repo_path)
        
        if result.returncode == 0:
            # Extract PR URL from output
            output = result.stdout.strip()
            if output:
                console.print(f"✅ Pull request created: {output}")
                return {"url": output}
            else:
                console.print("✅ Pull request created successfully")
                return {"url": ""}
        else:
            # Check if the error indicates an existing PR
            stderr_output = result.stderr
            if "already exists" in stderr_output.lower() and "pull request" in stderr_output.lower():
                # Try to extract the PR URL from the error message
                lines = stderr_output.split('\n')
                pr_url = None
                for line in lines:
                    if line.startswith('https://github.com/'):
                        pr_url = line.strip()
                        break
                
                pr_matches_issue = False
                if pr_url:
                    # Extract PR number and verify it matches the issue key
                    pr_number = None
                    if "/pull/" in pr_url:
                        try:
                            pr_number = pr_url.split("/pull/")[-1].split("/")[0]
                            pr_number = int(pr_number)
                        except (ValueError, IndexError):
                            pass
                    
                    if pr_number:
                        # Get PR details to verify it matches the issue
                        pr_view_cmd = ["gh", "pr", "view", str(pr_number), "--json", "title"]
                        pr_view_result = subprocess.run(pr_view_cmd, capture_output=True, text=True, cwd=repo_path)
                        if pr_view_result.returncode == 0:
                            try:
                                pr_data = json.loads(pr_view_result.stdout.strip())
                                pr_title = pr_data.get("title", "")
                                if issue.key.upper() in pr_title.upper():
                                    pr_matches_issue = True
                                    console.print(f"⚠️  Pull request for {issue.key} already exists (detected from error).")
                                    console.print(f"🔗 Existing PR: {pr_url}")
                                    return {"url": "existing", "existing": pr_url, "pr_data": pr_data}
                            except json.JSONDecodeError:
                                pass
                
                if not pr_matches_issue:
                    console.print(f"⚠️  A PR already exists on this branch but it's for a different issue.")
                    console.print(f"⚠️  Please ensure you're on the correct branch for {issue.key} and try again.")
                    return None
            else:
                console.print(f"❌ Failed to create pull request: {stderr_output}")
                return None
            
    except Exception as e:
        # Check if the exception message indicates an existing PR
        error_msg = str(e)
        if "already exists" in error_msg.lower() and "pull request" in error_msg.lower():
            console.print(f"⚠️  Pull request already exists (detected from exception).")
            return {"url": "existing", "existing": "Existing PR found"}
        else:
            console.print(f"❌ Error creating pull request: {e}")
            return None

def check_existing_prs_for_issue(issue, repo_paths: list) -> dict:
    """Check if PRs already exist for a given issue across multiple repositories"""
    import subprocess
    import json
    from pathlib import Path
    from rich.console import Console
    
    console = Console(color_system=None)
    existing_prs = {}
    
    for repo_path in repo_paths:
        git_repo_path = repo_path or "."
        
        current_branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=git_repo_path
        )
        if current_branch_result.returncode != 0:
            continue
        current_branch = current_branch_result.stdout.strip()
        
        # Search for PRs by issue key
        search_cmd = ["gh", "pr", "list", "--search", f"{issue.key}", "--json", "number,title,url,headRefName"]
        search_result = subprocess.run(search_cmd, capture_output=True, text=True, cwd=git_repo_path)
        
        found_pr = None
        if search_result.returncode == 0:
            try:
                prs = json.loads(search_result.stdout.strip())
                for pr in prs:
                    pr_title = pr.get("title", "")
                    if issue.key.upper() in pr_title.upper():
                        found_pr = pr
                        break
            except json.JSONDecodeError:
                pass
        
        if found_pr:
            existing_prs[repo_path] = found_pr
    
    return existing_prs

def checkout_branch(branch_name: str, repo_path: Optional[str] = None) -> bool:
    """Checkout a branch with automatic unshelving if needed"""
    from rich.console import Console
    from workflow.state import has_shelved_changes, get_shelved_branch
    
    repo = get_repo(repo_path)
    console = Console(color_system=None)
    
    try:
        # Check if we're switching to a branch with shelved changes
        if has_shelved_changes(repo_path or repo.working_dir):
            shelved_branch = get_shelved_branch(repo_path or repo.working_dir)
            if branch_name == shelved_branch:
                console.print(f"🔄 Switching back to branch '{branch_name}' with shelved changes")
                # Checkout the branch first, then unshelve
                repo.git.checkout(branch_name)
                unshelve_changes(repo_path)
                return True
        
        # Normal checkout
        repo.git.checkout(branch_name)
        return True
    except Exception as e:
        console.print(f"❌ Failed to checkout branch '{branch_name}': {e}")
        return False

def shelve_changes(repo_path: Optional[str] = None) -> bool:
    """Shelve current changes using git stash"""
    repo = get_repo(repo_path)
    try:
        # Create a stash with a descriptive name
        current_branch = repo.active_branch.name
        stash_name = f"wf-shelved-{current_branch}"
        repo.git.stash("push", "-m", stash_name)
        
        # Record in state
        from workflow.state import shelve_changes as record_shelve
        record_shelve(repo_path or repo.working_dir, current_branch)
        
        from rich.console import Console
        console = Console(color_system=None)
        console.print(f"✅ Changes shelved from branch '{current_branch}'")
        return True
    except Exception as e:
        from rich.console import Console
        console = Console(color_system=None)
        console.print(f"❌ Failed to shelve changes: {e}")
        return False

def unshelve_changes(repo_path: Optional[str] = None) -> bool:
    """Unshelve changes if they exist for this repository"""
    from workflow.state import has_shelved_changes, clear_shelved_changes as clear_state_shelved
    from rich.console import Console
    
    repo = get_repo(repo_path)
    console = Console(color_system=None)
    
    if not has_shelved_changes(repo_path or repo.working_dir):
        return False
    
    try:
        # Try to pop the stash
        repo.git.stash("pop")
        
        # Clear the state record
        clear_state_shelved(repo_path or repo.working_dir)
        
        console.print("✅ Shelved changes restored")
        return True
    except Exception as e:
        console.print(f"❌ Failed to unshelve changes: {e}")
        return False


def get_worktree_path(repo_path: str, branch_name: str) -> str:
    """Generate a worktree path for a branch in a neighboring directory.
    
    Args:
        repo_path: Path to the main repository
        branch_name: Name of the branch
        
    Returns:
        Path string for the new worktree directory
    """
    from pathlib import Path
    
    repo_path = Path(repo_path).resolve()
    parent_dir = repo_path.parent
    repo_name = repo_path.name
    
    # Create a sanitized branch name for the directory
    # Remove special characters and limit length
    safe_branch = "".join(c if c.isalnum() or c in "-_" else "_" for c in branch_name)
    safe_branch = safe_branch[:50]  # Limit length
    
    worktree_name = f"{repo_name}-{safe_branch}"
    worktree_path = parent_dir / worktree_name
    
    # If path exists, add a number suffix
    counter = 1
    original_path = worktree_path
    while worktree_path.exists():
        worktree_path = Path(f"{original_path}-{counter}")
        counter += 1
    
    return str(worktree_path)


def create_worktree(repo_path: str, branch_name: str, base_branch: str = None) -> str:
    """Create a git worktree for a new branch.
    
    Args:
        repo_path: Path to the main repository
        branch_name: Name of the branch to create
        base_branch: Base branch to create from (defaults to current branch)
        
    Returns:
        Path to the created worktree directory
    """
    from rich.console import Console
    from pathlib import Path
    
    repo = get_repo(repo_path)
    console = Console(color_system=None)
    
    # First check if branch already has a worktree
    try:
        worktrees = repo.git.worktree("list", "--porcelain")
        current_wt = None
        for line in worktrees.split("\n"):
            if line.startswith("worktree "):
                current_wt = line[9:].strip()
            elif line.startswith("branch "):
                branch_ref = line[7:].strip().replace("refs/heads/", "")
                if branch_ref == branch_name and current_wt:
                    # Found existing worktree for this branch
                    console.print(f"🌳 Worktree already exists for branch '{branch_name}' at: {current_wt}")
                    return current_wt
                    current_wt = None
    except:
        pass
    
    # Determine worktree path
    worktree_path = get_worktree_path(repo_path, branch_name)
    
    # Check if worktree path already exists on disk
    if Path(worktree_path).exists():
        console.print(f"🌳 Worktree already exists at: {worktree_path}")
        add_claude_trust(worktree_path)
        return worktree_path
    
    # Determine base branch
    if not base_branch:
        base_branch = repo.active_branch.name
    
    console.print(f"🌳 Creating worktree for branch '{branch_name}'...")
    console.print(f"   Base: {base_branch}")
    console.print(f"   Location: {worktree_path}")
    
    try:
        # Create the worktree with a new branch
        repo.git.worktree("add", "-b", branch_name, worktree_path, base_branch)
        console.print(f"✅ Worktree created successfully")
        add_claude_trust(worktree_path)
        return worktree_path
    except Exception as e:
        # If branch already exists, try to create worktree from existing branch
        if "already exists" in str(e):
            try:
                repo.git.worktree("add", worktree_path, branch_name)
                console.print(f"✅ Worktree created from existing branch")
                add_claude_trust(worktree_path)
                return worktree_path
            except Exception as e2:
                raise RuntimeError(f"Failed to create worktree: {e2}")
        else:
            raise RuntimeError(f"Failed to create worktree: {e}")


def list_worktrees(repo_path: str) -> list:
    """List all worktrees for a repository.
    
    Args:
        repo_path: Path to the repository
        
    Returns:
        List of dicts with worktree info
    """
    repo = get_repo(repo_path)
    
    try:
        output = repo.git.worktree("list", "--porcelain")
        worktrees = []
        current_worktree = {}
        
        for line in output.strip().split("\n"):
            if line.startswith("worktree "):
                if current_worktree:
                    worktrees.append(current_worktree)
                current_worktree = {"path": line[9:], "is_main": False, "branch": None}
            elif line == "bare":
                current_worktree["is_bare"] = True
            elif line.startswith("HEAD "):
                current_worktree["head"] = line[5:]
            elif line.startswith("branch "):
                branch_ref = line[7:]
                current_worktree["branch"] = branch_ref.replace("refs/heads/", "")
            elif line == "detached":
                current_worktree["detached"] = True
        
        if current_worktree:
            worktrees.append(current_worktree)
        
        # Mark the main worktree
        main_path = str(Path(repo_path).resolve())
        for wt in worktrees:
            if wt["path"] == main_path:
                wt["is_main"] = True
        
        return worktrees
    except Exception as e:
        return []


def remove_worktree(repo_path: str, worktree_path: str, force: bool = False) -> bool:
    """Remove a git worktree.
    
    Args:
        repo_path: Path to the main repository
        worktree_path: Path to the worktree to remove
        force: Force removal even if there are uncommitted changes
        
    Returns:
        True if successful, False otherwise
    """
    from rich.console import Console
    
    repo = get_repo(repo_path)
    console = Console(color_system=None)
    
    try:
        cmd = ["worktree", "remove"]
        if force:
            cmd.append("--force")
        cmd.append(worktree_path)
        
        repo.git.execute(["git"] + cmd)
        console.print(f"✅ Worktree removed: {worktree_path}")
        return True
    except Exception as e:
        console.print(f"❌ Failed to remove worktree: {e}")
        return False
