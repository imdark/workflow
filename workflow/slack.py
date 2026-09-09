import json

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

def get_github_reviewers(pr_url=None, repo_path=None):
    """Get GitHub reviewers from PR or repository"""
    if not pr_url and not repo_path:
        return []
    
    reviewers = []
    try:
        # This would require GitHub API integration
        # For now, return empty list - implement based on your GitHub API access
        pass
    except Exception:
        pass
    
    return reviewers

def get_slack_user_tags(reviewers, cfg):
    """Convert GitHub reviewers to Slack user tags"""
    slack_config = cfg.get("slack", {})
    user_mapping = slack_config.get("user_mapping", {})
    
    slack_tags = []
    for reviewer in reviewers:
        # Try to find by email
        email = reviewer.get("email", "").lower()
        slack_username = user_mapping.get(email)
        
        if slack_username:
            slack_tags.append(f"<@{slack_username}>")
        else:
            # Try to extract username from other fields
            if "@" in reviewer.get("login", ""):
                # Try to convert GitHub username to email format
                github_login = reviewer.get("login", "")
                potential_email = f"{github_login}@users.noreply.github.com"
                slack_username = user_mapping.get(potential_email)
                if slack_username:
                    slack_tags.append(f"<@{slack_username}>")
    
    return slack_tags

def format_message(template, issue, pr=None, reviewers=None, cfg=None):
    """Format a message template with variables"""
    if not cfg:
        cfg = {}
    
    variables = {
        "task_key": issue.key if issue else "",
        "task_title": getattr(issue, 'summary', '') if issue else '',
        "task_description": getattr(issue, 'description', '') if issue else '',
        "pr_url": pr.get("url", "") if pr else "",
        "pr_title": pr.get("title", "") if pr else "",
    }
    
    # Add PR section
    pr_section = ""
    if pr and pr.get("url"):
        pr_section = f"\n🔗 PR: {pr['url']}"
    variables["pr_section"] = pr_section
    
    # Add reviewers section
    reviewers_section = ""
    if reviewers:
        slack_tags = get_slack_user_tags(reviewers, cfg)
        if slack_tags:
            reviewers_section = f"\n👥 Reviewers: {' '.join(slack_tags)}"
    variables["reviewers_section"] = reviewers_section
    
    # Format the template
    try:
        formatted_message = template.format(**variables)
        return formatted_message
    except KeyError as e:
        # Fallback to basic formatting if template has invalid variables
        return f"Task {variables.get('task_key', 'unknown')}: {variables.get('task_title', 'No title')}"

def post_pr(cfg, issue, pr):
    """Post a PR notification to Slack using template"""
    if not cfg.get("slack_webhook") or not pr:
        return False
    
    slack_config = cfg.get("slack", {})
    template = slack_config.get("message_templates", {}).get("pr_ready", 
        "🔗 PR ready for review: {task_key} - {pr_url}{reviewers_section}")
    
    # Get reviewers if possible
    reviewers = get_github_reviewers(pr.get("url"))
    
    message = format_message(template, issue, pr, reviewers, cfg)
    channel = slack_config.get("default_channel")
    
    return post_message(cfg, message, channel)

def post_message(cfg, message, channel=None):
    """Post a custom message to Slack"""
    if not cfg.get("slack_webhook"):
        return False
    
    if not REQUESTS_AVAILABLE:
        print(f"⚠️  requests module not available. Would post to Slack:")
        print(f"Channel: {channel or 'default'}")
        print(f"Message: {message}")
        return True  # Pretend success for development
    
    payload = {"text": message}
    if channel:
        payload["channel"] = channel
    
    try:
        response = requests.post(cfg["slack_webhook"], json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        return False

def post_task_start(cfg, issue):
    """Post a task start notification to Slack using template"""
    if not cfg.get("slack_webhook") or not issue:
        return False
    
    slack_config = cfg.get("slack", {})
    template = slack_config.get("message_templates", {}).get("task_start", 
        "🚀 Starting work on {task_key}: {task_title}")
    
    message = format_message(template, issue, None, None, cfg)
    channel = slack_config.get("default_channel")
    
    return post_message(cfg, message, channel)

def post_task_complete(cfg, issue, pr=None):
    """Post a task completion notification to Slack using template"""
    if not cfg.get("slack_webhook") or not issue:
        return False
    
    slack_config = cfg.get("slack", {})
    template = slack_config.get("message_templates", {}).get("task_complete", 
        "✅ Completed {task_key}: {task_title}{pr_section}{reviewers_section}")
    
    # Get reviewers if PR exists
    reviewers = []
    if pr:
        reviewers = get_github_reviewers(pr.get("url"))
    
    message = format_message(template, issue, pr, reviewers, cfg)
    channel = slack_config.get("default_channel")
    
    return post_message(cfg, message, channel)

def post_error(cfg, error_message, context=None):
    """Post an error notification to Slack"""
    if not cfg.get("slack_webhook"):
        return False
    
    message = f"❌ Error: {error_message}"
    if context:
        message += f"\nContext: {context}"
    
    slack_config = cfg.get("slack", {})
    channel = slack_config.get("default_channel")
    
    return post_message(cfg, message, channel)
