# GitHub CLI Setup

## Why GitHub CLI?

The `wf` tool now uses GitHub CLI (`gh`) instead of direct API calls for better reliability and security.

## Required Setup

### 1. Install GitHub CLI

```bash
# macOS
brew install gh

# Linux
sudo apt install gh
# Or download from https://github.com/cli/cli/releases
```

### 2. Authenticate with GitHub CLI

```bash
# Method 1: Interactive (recommended)
gh auth login

# Method 2: With token
gh auth login --with-token
```

### 3. Required Permissions

For PR creation, the GitHub CLI token needs these repository permissions:

**Required:**
- ✅ **Contents** - Read and write repository contents
- ✅ **Pull requests** - Read and write pull requests  
- ✅ **Metadata** - Read repository metadata

**Optional but useful:**
- ✅ **Issues** - Read and write issues (for task linking)

### 4. Troubleshooting

Check authentication status:
```bash
gh auth status
```

Check current configuration:
```bash
gh config list
```

If you get permission errors, re-authenticate with correct scopes:
```bash
gh auth login --scopes repo,read:org
```

## Usage

Once authenticated, `wf start`, `wf switch`, and `wf done` will work seamlessly with GitHub CLI integration:

- `wf start TASK` - Creates branch, starts work, opens terminal tabs
- `wf switch TASK` - Switches context, opens terminal tabs  
- `wf done` - Commits changes and creates pull request via `gh pr create`

The GitHub CLI handles authentication, API rate limiting, and provides better error messages than custom implementations.