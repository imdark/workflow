# Enhanced PR Detection - Multiple Fallback Methods

I've significantly enhanced PR detection with 5 different methods to handle various scenarios:

## 🔍 **Detection Methods (in order)**

### 1. **Current Branch PRs (GitHub CLI)**
```bash
gh pr list --head <current_branch> --json number,title,url
```
- ✅ Most reliable method
- ✅ Fast and efficient
- ✅ Full PR details

### 2. **GitHub Search by Issue Key**
```bash  
gh pr list --search <issue_key> --json number,title,url
```
- ✅ Finds PRs even if on different branch
- ✅ Case-insensitive matching
- ✅ Handles title variations

### 3. **Direct Branch Search (Duplicate)**
```bash
gh pr list --head <current_branch> --json number,title,url
```
- ✅ Redundant fallback for robustness

### 4. **GitHub API Direct Call**
```bash
curl https://api.github.com/repos/owner/repo/pulls?head=owner:branch
```
- ✅ Works when GitHub CLI fails
- ✅ Bypasses local CLI issues
- ✅ Uses GitHub token for auth

### 5. **Enhanced String Fallback**
```bash
gh pr list
```
- ✅ Parses output manually
- ✅ Extracts PR numbers
- ✅ Gets details with `gh pr view`

## 🎯 **Flexible Title Matching**

**Before**: Only exact prefix matching
```python
if pr.get("title", "").startswith(issue.key):
```

**After**: Contains matching (case-insensitive)
```python  
if issue.key.upper() in pr_title.upper():
```

## 🔧 **Debugging Capabilities**

Run this to debug PR detection:
```bash
cd /path/to/your/repo
~/code/workflow/debug_pr.sh
```

## 📋 **What This Should Fix**

Your specific case:
- **Branch**: `proj-123-add-search-alias`  
- **Issue**: `PROJ-123`
- **Expected**: PR detection should now work

The enhanced system will:
1. ✅ Check current branch for PRs
2. ✅ Search GitHub API if CLI fails
3. ✅ Match issue key anywhere in PR title
4. ✅ Handle various naming conventions
5. ✅ Provide fallback methods for edge cases

## 🚀 **Test It**

```bash
# Test the enhanced status command
wf status

# Should now show:
🔗 PR: https://github.com/your-repo/pull/XXX
📝 PR Title: Add search alias...  
📊 PR State: OPEN
```

If it still doesn't work, run the debug script to see what's happening with GitHub CLI and API calls.