from typing import Dict, Any, List, Optional
import logging
import json
import subprocess
from .base import Action, ActionType


class GitHubCommentLookupAction(Action):
    """Action for looking up GitHub PR comments"""
    
    def __init__(self, action_id: str):
        super().__init__(action_id, ActionType.GITHUB_COMMENT_LOOKUP)
    
    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Look up GitHub PR comments"""
        try:
            owner = context.get('owner')
            repo = context.get('repo')
            pr_number = context.get('pr_number')
            
            if not owner or not repo or not pr_number:
                return {'success': False, 'error': 'owner, repo, and pr_number are required'}
            
            comments = self._lookup_comments(owner, repo, pr_number)
            
            return {
                'success': True,
                'comments': comments,
                'count': len(comments)
            }
        except Exception as e:
            self.logger.error(f"Failed to lookup GitHub comments: {e}")
            return {'success': False, 'error': str(e)}
    
    def _lookup_comments(self, owner: str, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        """Lookup comments via GitHub CLI"""
        try:
            result = subprocess.run(
                ['gh', 'pr', 'view', str(pr_number), '--repo', f'{owner}/{repo}', '--json', 'comments'],
                capture_output=True,
                text=True,
                check=True
            )
            
            data = json.loads(result.stdout)
            return data.get('comments', [])
        except subprocess.CalledProcessError as e:
            self.logger.error(f"GitHub CLI error: {e.stderr}")
            return []
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse JSON: {e}")
            return []
    
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate GitHub comment lookup configuration"""
        return all(k in config for k in ['owner', 'repo', 'pr_number'])


def lookup_github_comments(owner: str, repo: str, pr_number: int) -> List[Dict[str, Any]]:
    """Convenience function to lookup GitHub PR comments"""
    action = GitHubCommentLookupAction("github_comment_lookup")
    result = action.execute({'owner': owner, 'repo': repo, 'pr_number': pr_number})
    return result.get('comments', []) if result.get('success') else []


def main():
    """CLI interface for GitHub comment lookup"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Lookup GitHub PR comments')
    parser.add_argument('owner', help='Repository owner')
    parser.add_argument('repo', help='Repository name')
    parser.add_argument('pr_number', type=int, help='PR number')
    
    args = parser.parse_args()
    
    comments = lookup_github_comments(args.owner, args.repo, args.pr_number)
    
    print(f"Found {len(comments)} comments:")
    for comment in comments:
        author = comment.get('author', {}).get('login', 'unknown')
        body = comment.get('body', '')[:100]
        print(f"  - {author}: {body}...")


if __name__ == '__main__':
    main()
