from jira import JIRA
from prompt_toolkit import prompt
from prompt_toolkit.completion import FuzzyWordCompleter
import typer
import yaml
import time
from pathlib import Path
from typing import Optional
from workflow.backends.base import Issue, TaskBackend
from workflow.variables import VariableResolver
from workflow.config import get_jira_config

class JiraBackend(TaskBackend):
    def __init__(self, cfg):
        jira_cfg = get_jira_config()
        self.client = JIRA(
            server=jira_cfg["url"],
            basic_auth=(jira_cfg["email"], jira_cfg["token"]),
        )
        self._cfg = cfg
        self._assigned_issues_cache = None
        self._cache_time = None
        self._cache_ttl = 30  # Cache for 30 seconds
        self._fields_cache = None
        self._fields_cache_time = None
        self._fields_cache_ttl = 3600  # Cache fields for 1 hour
        self._active_sprint_cache = None
        self._active_sprint_cache_time = None
        self._active_sprint_cache_ttl = 300  # Cache active sprint for 5 minutes
        self._variable_resolver = VariableResolver(self._cfg)
    
    def _get_config(self):
        """Get configuration - helper method"""
        return self._cfg

    def get(self, key):
        try:
            i = self.client.issue(key)
            return Issue(i.key, i.fields.summary, i.fields.description or "", i.fields.updated)
        except Exception as e:
            # Provide a clear error message for debugging
            error_msg = str(e)
            if "does not exist" in error_msg:
                raise Exception(f"Issue '{key}' does not exist or you do not have permission to see it.")
            else:
                raise Exception(f"Failed to get issue '{key}': {error_msg}")
            return None

    def _is_valid_ticket_key(self, text):
        """Check if text follows standard ticket key format (PROJECT-123)"""
        import re
        # Match pattern: letters/numbers + dash + numbers (e.g., PROJ-123, ABC-456)
        pattern = r'^[A-Z][A-Z0-9]*-\d+$'
        return bool(re.match(pattern, text.strip().upper()))

    def get_or_create(self, q):
        """Get existing issue or create new one if not found or if input is free text"""
        # Check if input looks like a valid ticket key
        if not self._is_valid_ticket_key(q):
            # Input is free text, create a new ticket automatically
            typer.echo(f"🎯 Creating new ticket from text: '{q}'")
            return self._create_issue_from_text(q)
        
        # Input looks like a ticket key, try to get it
        try:
            return self.get(q)
        except Exception as e:
            # Check if this is a "does not exist" error vs other errors
            error_msg = str(e)
            if "does not exist" in error_msg or "not found" in error_msg.lower():
                # Issue doesn't exist, create it automatically with the suggested key
                typer.echo(f"❌ Ticket '{q}' not found. Creating new ticket...")
                return self._create_issue_from_text(q, use_as_key=True)
            else:
                # Other error (permission, network, etc.)
                typer.echo(f"❌ Error accessing issue '{q}': {error_msg}")
            
            return None
    
    def _create_interactive_issue(self, suggested_key):
        """Create a new issue interactively"""
        typer.echo(f"\n📝 Creating new Jira issue...")
        
        # Get issue details from user - use cleaned key as default title
        summary = typer.prompt("Issue title", default=suggested_key) or suggested_key
        description = typer.prompt("Issue description", default="") or ""
        
        # Issue type options
        issue_types = ["Task", "Bug", "Story", "Epic"]
        typer.echo("\nAvailable issue types:")
        for i, itype in enumerate(issue_types, 1):
            typer.echo(f"  {i}. {itype}")
        
        type_choice = typer.prompt("Select issue type", default="1") or "1"
        try:
            type_index = int(type_choice) - 1
            if 0 <= type_index < len(issue_types):
                issue_type = issue_types[type_index]
            else:
                issue_type = "Task"
        except ValueError:
            issue_type = "Task"
        
        # Project key
        project_key = typer.prompt("Project key", default=self._get_config().get("jira", {}).get("project", "DEV"))
        
        # Create the issue
        new_issue = self.create_issue(summary, description, issue_type, project_key)
        
        if new_issue:
            typer.echo(f"✅ Successfully created issue {new_issue.key}")
            # Move it to in progress
            self.move_to_in_progress(new_issue)
            return new_issue
        
        return None

    def _create_issue_from_text(self, text, use_as_key=False):
        """Create a new issue from free text automatically"""
        # Clean up the input text
        text = text.strip().strip('"\'')
        
        if use_as_key and self._is_valid_ticket_key(text):
            # Use the text as the ticket key (title will be derived from it)
            summary = text
            # Extract project key from the ticket key
            project_key = text.split('-')[0]
        else:
            # Use the text as the summary/title
            summary = text
            # Get default project from config - be more robust with config access
            config = self._get_config()
            jira_config = config.get("jira", {})
            project_key = jira_config.get("project", "DEV")
        
        # Use a default description for auto-created tickets
        description = f"Auto-created ticket from: '{text}'"
        
        # Default to "Task" type for auto-created tickets
        issue_type = "Task"
        
        typer.echo(f"📝 Creating ticket '{summary}' in project {project_key}...")
        
        # Create the issue
        new_issue = self.create_issue(summary, description, issue_type, project_key)
        
        if new_issue:
            typer.echo(f"✅ Successfully created ticket {new_issue.key}")
            return new_issue
        else:
            typer.echo(f"❌ Failed to create ticket '{summary}'")
            # Return None to indicate failure
            return None

    def _get_cached_assigned_tasks(self):
        """Get assigned tasks with caching"""
        current_time = time.time()

        # Return cached data if still valid
        if (self._assigned_issues_cache and self._cache_time and
            current_time - self._cache_time < self._cache_ttl):
            return self._assigned_issues_cache

        # Fetch fresh data
        issues = self.client.search_issues("assignee = currentUser() AND statuscategory != Done ORDER BY updated DESC")
        self._assigned_issues_cache = [Issue(i.key, i.fields.summary, i.fields.description or "", i.fields.updated) for i in issues]
        self._cache_time = current_time
        return self._assigned_issues_cache

    def get_assigned_tasks(self):
        """Get assigned tasks (used by tab completion)"""
        return self._get_cached_assigned_tasks()

    def list_tasks(self, project_name: Optional[str] = None, status: Optional[str] = None):
        """List tasks from Jira with optional filtering"""
        jql_parts = []
        
        if project_name:
            jql_parts.append(f"project = '{project_name}'")
        
        if status:
            status_mapping = {
                "to do": "To Do",
                "todo": "To Do",
                "in progress": "In Progress",
                "in review": "In Review",
                "done": "Done"
            }
            normalized_status = status_mapping.get(status.lower(), status)
            jql_parts.append(f"status = '{normalized_status}'")
        
        jql = " AND ".join(jql_parts) if jql_parts else "assignee = currentUser() ORDER BY updated DESC"
        
        issues = self.client.search_issues(jql, maxResults=100)
        
        class JiraTask:
            """Wrapper for Jira issue to provide task-like interface"""
            def __init__(self, issue):
                self.key = issue.key
                self.title = issue.fields.summary
                self.description = issue.fields.description or ""
                self.status = issue.fields.status.name
                self.issue_type = issue.fields.issuetype.name
                self.priority = issue.fields.priority.name if issue.fields.priority else "Medium"
                self.assignee = issue.fields.assignee.displayName if issue.fields.assignee else None
                self.reporter = issue.fields.reporter.displayName if issue.fields.reporter else None
                self.labels = issue.fields.labels or []
                self.components = [c.name for c in issue.fields.components] if issue.fields.components else []
                self.fix_versions = [v.name for v in issue.fields.fixVersions] if issue.fields.fixVersions else []
                self.due_date = str(issue.fields.duedate) if issue.fields.duedate else None
                self.created_date = str(issue.fields.created)[:10] if issue.fields.created else ""
                self.updated_date = str(issue.fields.updated)[:10] if issue.fields.updated else ""
                self.file_path = None  # Not applicable for Jira tasks
        
        return [JiraTask(i) for i in issues]

    def select_interactively(self, include_others=False):
        # Use cached assigned tasks to avoid extra API call
        cached_issues = self._get_cached_assigned_tasks()
        # Get full issue objects for status display
        issue_keys = [issue.key for issue in cached_issues]
        full_issues = self.client.search_issues(f"key in ({','.join(issue_keys)})")
        mapping = {f"{i.key} [{i.fields.status.name}] {i.fields.summary}": i for i in full_issues}
        choice = prompt("Task: ", completer=FuzzyWordCompleter(list(mapping.keys())))
        
        # Check if the choice matches an existing issue
        if choice in mapping:
            return self.get(mapping[choice].key)
        else:
            # User typed something that's not in the list, treat it as new ticket creation
            typer.echo(f"🎯 '{choice}' not found in existing tasks. Creating new ticket...")
            return self._create_issue_from_text(choice)

    def _get_fields(self):
        """Get all available fields from Jira with caching"""
        import time
        current_time = time.time()
        
        # Return cached fields if still valid
        if (self._fields_cache and self._fields_cache_time and 
            current_time - self._fields_cache_time < self._fields_cache_ttl):
            return self._fields_cache
        
        try:
            typer.echo("🔍 Discovering Jira fields...")
            fields = self.client.fields()
            
            # Create a dictionary mapping field names to their info
            fields_dict = {}
            for field in fields:
                fields_dict[field['name'].lower()] = {
                    'id': field['id'],
                    'name': field['name'],
                    'custom': field['custom'],
                    'schema': field.get('schema', {}),
                    'allowed_values': field.get('allowedValues', [])  # Store allowed values for option fields
                }
            
            # Cache results
            self._fields_cache = fields_dict
            self._fields_cache_time = current_time
            
            typer.echo(f"✅ Discovered {len(fields)} Jira fields")
            return fields_dict
            
        except Exception as e:
            typer.echo(f"❌ Failed to fetch Jira fields: {e}")
            return {}

    def find_field_by_name(self, field_name):
        """Find field by name (case-insensitive)"""
        fields = self._get_fields()
        field_key = field_name.lower()
        
        if field_key in fields:
            return fields[field_key]
        
        # Try partial matching if exact match not found
        matches = [k for k in fields.keys() if field_key in k]
        if len(matches) == 1:
            return fields[matches[0]]
        elif len(matches) > 1:
            typer.echo(f"⚠️  Multiple fields match '{field_name}': {', '.join(matches)}")
            return None
        else:
            typer.echo(f"❌ No field found matching '{field_name}'")
            return None

    def _process_field_value(self, value, field_type, field_schema, field_id=None):
        """Process field value based on Jira field type"""
        try:
            if field_type == 'string':
                return str(value)
            elif field_type == 'number':
                return float(value) if '.' in value else int(value)
            elif field_type == 'date':
                # For date fields, Jira expects ISO format
                from datetime import datetime
                return value  # Assume user provides correct format
            elif field_type == 'datetime':
                # For datetime fields
                from datetime import datetime
                return value  # Assume user provides correct format
            elif field_type == 'array':
                # For multi-select fields
                if isinstance(value, str):
                    # Split by comma if it's a string
                    return [v.strip() for v in value.split(',')]
                return value if isinstance(value, list) else [value]
            elif field_type == 'user':
                # For user fields
                if isinstance(value, str) and '@' in value:
                    return {'accountId': value}  # Handle email
                return {'name': str(value)}
            elif field_type == 'option':
                # For single select fields - validate and format properly
                return self._process_option_field(value, field_schema, field_id)
            elif field_type == 'issuetype':
                # For issue type fields
                return {'name': str(value)}
            elif field_type == 'priority':
                # For priority fields
                return {'name': str(value)}
            else:
                # Default handling for unknown types
                return value
                
        except Exception as e:
            typer.echo(f"⚠️  Could not process field value '{value}' for type '{field_type}': {e}")
            return value

    def _process_option_field(self, value, field_schema, field_id):
        """Process option field values with better error handling"""
        try:
            value_str = str(value)
            
            # Get available options for this field to find the correct format
            options = self.get_field_options(field_id)
            
            if options:
                # Find the option that matches our value
                matching_option = None
                for option in options:
                    option_value = option.get('value', '')
                    if option_value.lower() == value_str.lower():
                        matching_option = option
                        break
                
                if matching_option:
                    # If option has an ID field, use it
                    if 'id' in matching_option and matching_option['id']:
                        return {'id': matching_option['id']}
                    # Otherwise use the value format
                    return {'value': matching_option['value']}
            
            # If no matching option found, try the simple value format
            return {'value': value_str}
            
        except Exception as e:
            typer.echo(f"⚠️  Could not process option field value '{value}': {e}")
            return {'value': str(value)}

    def get_active_sprint(self, project_key=None):
        """Get the current active sprint for a project with caching"""
        current_time = time.time()
        
        # Return cached sprint if still valid
        if (self._active_sprint_cache and self._active_sprint_cache_time and 
            current_time - self._active_sprint_cache_time < self._active_sprint_cache_ttl):
            return self._active_sprint_cache
        
        try:
            # Get project key from config if not provided
            if not project_key:
                config = self._get_config()
                project_key = config.get("jira", {}).get("project", "DEV")
            
            # Check for configured sprint preferences
            config = self._get_config()
            sprint_preferences = config.get("sprint_preferences", {})
            
            # Search for active sprints using JIRA's Greenhopper API
            jira_url = self.client._options['server'].rstrip('/')
            
            all_active_sprints = []
            
            # If specific board ID is configured, only check that board
            if sprint_preferences.get("board_id"):
                board_id = sprint_preferences["board_id"]
                try:
                    # Try to get the board name for logging
                    board_name = f"Board {board_id}"
                    try:
                        board_info_url = f"{jira_url}/rest/agile/1.0/board/{board_id}"
                        board_response = self.client._session.get(board_info_url)
                        if board_response.status_code == 200:
                            board_data = board_response.json()
                            board_name = board_data.get("name", f"Board {board_id}")
                    except:
                        pass  # Continue with default name
                    
                    typer.echo(f"🎯 Checking configured board: {board_name} (ID: {board_id})")
                    
                    # Get active sprints for this specific board
                    sprints_url = f"{jira_url}/rest/agile/1.0/board/{board_id}/sprint"
                    params = {"state": "active"}
                    response = self.client._session.get(sprints_url, params=params)
                    
                    if response.status_code != 200:
                        typer.echo(f"❌ Could not access configured board '{board_name}' (ID: {board_id})")
                        typer.echo(f"💡 Check board ID or remove specific board ID to search all boards")
                        return None
                    
                    sprints_data = response.json()
                    if not sprints_data:
                        typer.echo(f"⚠️  No sprints found on configured board")
                        return None
                        
                    sprints = sprints_data.get("values", [])
                    if not sprints:
                        typer.echo(f"⚠️  No active sprints on configured board '{board_name}'")
                        return None
                    
                    # Add all active sprints with their board info
                    for sprint in sprints:
                        sprint["board_name"] = board_name
                        sprint["board_id"] = board_id
                        all_active_sprints.append(sprint)
                        
                except Exception as e:
                    typer.echo(f"❌ Error accessing configured board: {e}")
                    return None
            
            else:
                # Search all boards if no specific board is configured
                # Get all boards for the project first
                boards_url = f"{jira_url}/rest/agile/1.0/board"
                params = {"projectKeyOrId": project_key}
                response = self.client._session.get(boards_url, params=params)
                
                if response.status_code != 200:
                    typer.echo(f"⚠️  Could not fetch boards for project {project_key}")
                    return None
                
                response_data = response.json()
                if not response_data:
                    typer.echo(f"⚠️  Invalid response when fetching boards")
                    return None
                    
                boards = response_data.get("values", [])
                if not boards:
                    typer.echo(f"⚠️  No boards found for project {project_key}")
                    return None
                
                typer.echo(f"🔍 Searching {len(boards)} boards for active sprints...")
                
                # Collect all active sprints from all accessible boards
                for i, board in enumerate(boards):
                    board_id = board["id"]
                    board_name = board.get("name", f"Board {i+1}")
                    
                    try:
                        # Get active sprints for this board
                        sprints_url = f"{jira_url}/rest/agile/1.0/board/{board_id}/sprint"
                        params = {"state": "active"}
                        response = self.client._session.get(sprints_url, params=params)
                        
                        if response.status_code != 200:
                            typer.echo(f"⚠️  Board '{board_name}' does not support sprints")
                            continue
                        
                        sprints_data = response.json()
                        if not sprints_data:
                            continue
                            
                        sprints = sprints_data.get("values", [])
                        if not sprints:
                            continue
                        
                        # Add all active sprints with their board info
                        for sprint in sprints:
                            sprint["board_name"] = board_name
                            sprint["board_id"] = board_id
                            all_active_sprints.append(sprint)
                        
                    except Exception as board_error:
                        typer.echo(f"⚠️  Error accessing board '{board_name}': {board_error}")
                        continue
            
            if not all_active_sprints:
                typer.echo(f"⚠️  No accessible active sprints found via boards, trying JQL fallback...")
                active_sprint = self._get_active_sprint_via_jql(project_key)
                if not active_sprint:
                    typer.echo(f"⚠️  No active sprints found for project {project_key}")
                    return None
            else:
                # Try to find the best sprint based on preferences
                active_sprint = self._select_best_sprint(all_active_sprints, sprint_preferences)
            
            # Cache the result
            self._active_sprint_cache = active_sprint
            self._active_sprint_cache_time = current_time
            
            return active_sprint
            
        except Exception as sprint_error:
            typer.echo(f"⚠️  Could not add ticket to active sprint: {sprint_error}")
            typer.echo(f"💡 To disable auto-assignment: wf config auto-assign-sprint false")
            # Continue without sprint assignment - the ticket was still created

    def _add_issue_to_sprint(self, issue_key, sprint_id):
        """Add an issue to a sprint with multiple fallback methods"""
        try:
            # Method 1: Direct API call
            self.client.add_issues_to_sprint(sprint_id, [issue_key])
            return True
        except Exception as e1:
            try:
                # Method 2: Update issue field directly
                issue = self.client.issue(issue_key)
                issue.update(fields={'sprint': sprint_id})
                return True
            except Exception as e2:
                typer.echo(f"⚠️  Sprint assignment failed with both methods:")
                typer.echo(f"   Method 1 (API): {str(e1)[:100]}...")
                typer.echo(f"   Method 2 (Field update): {str(e2)[:100]}...")
                return False
            
            # Create and return Issue object
            return Issue(new_issue.key, new_issue.fields.summary, new_issue.fields.description or "", new_issue.fields.updated)
            
        except Exception as e:
            print(f"❌ Failed to create Jira issue: {e}")
            return None
    
    def _get_default_fields_for_project(self, project_key):
        """Get default field values that apply to a specific project"""
        from workflow.config import get_field_value_mapping
        
        cfg = self._get_config()
        custom_fields = cfg.get("custom_fields", {})
        fields_info = self._get_fields()
        default_fields = {}
        
        for field_name, field_config in custom_fields.items():
            default_value = field_config.get("default_value")
            if not default_value:
                continue
            
            field_id = field_config["field_id"]
            field_project_key = field_config.get("project_key", "")
            
            # Check if this field applies to the current project
            if field_project_key:
                if field_project_key.upper() != project_key.upper():
                    continue  # Skip project-specific fields for other projects
            # Global field (no project restriction) applies to all projects
            
            # Replace variables in default value using generic variable resolver
            processed_default = self._variable_resolver.resolve_variables(default_value, {
                "project_key": project_key
            })
            
            # Apply value mapping if configured
            mapped_value = get_field_value_mapping(field_name, processed_default)
            
            # Get field schema information for type handling - find by ID instead of name
            field_info = None
            for info in fields_info.values():
                if info['id'].lower() == field_id.lower():
                    field_info = info
                    break
            
            if field_info:
                field_schema = field_info.get('schema', {})
                field_type = field_schema.get('type', 'string')
                
                # Process mapped default value based on field type
                processed_value = self._process_field_value(mapped_value, field_type, field_schema, field_id)
                default_fields[field_id] = processed_value
                
                # Show if mapping was applied
                if mapped_value != processed_default:
                    typer.echo(f"🔄 Mapped '{processed_default}' → '{mapped_value}' for field '{field_name}'")
                elif processed_default != default_value:
                    typer.echo(f"🔄 Replaced variables in '{default_value}' → '{processed_default}' for field '{field_name}'")
            else:
                # Use raw value if we can't determine field type
                default_fields[field_id] = processed_default
        
        return default_fields

    def get_field_options(self, field_id):
        """Get available options for a select field"""
        try:
            # First get field metadata
            metadata = self.get_field_metadata(field_id)
            if metadata and 'allowedValues' in metadata:
                return metadata['allowedValues']
            
            # If no allowed values in metadata, try to fields cache
            fields = self._get_fields()
            field_info = fields.get(field_id.lower())
            if field_info and 'allowed_values' in field_info:
                return field_info['allowed_values']
            
            # Try to fetch options using the field ID specific method
            return self._get_field_options_for_id(field_id)
            
        except Exception as e:
            typer.echo(f"⚠️  Error fetching field options: {e}")
            return []

    def get_field_metadata(self, field_id):
        """Get comprehensive metadata for a field including options"""
        try:
            # Use the cached fields list instead of making a separate API call
            fields = self._get_fields()
            
            # Find the field by ID (case-insensitive)
            field_info = None
            for field_data in fields.values():
                if field_data['id'].lower() == field_id.lower():
                    field_info = dict(field_data)  # Make a copy to modify
                    break
            
            # If it's an option field and we need to get to actual allowed values
            if field_info and field_info.get('schema', {}).get('type') == 'option':
                try:
                    # Try to get the field configuration via Jira's field configuration API
                    # This is a more complex call, so we'll try to extract from cached data first
                    if not field_info.get('allowedValues'):
                        # For option fields without cached values, we might need to use a different approach
                        # For now, indicate that options exist but aren't cached
                        field_info['allowedValues'] = self._get_field_options_for_id(field_id)
                except Exception:
                    # If we can't get options, continue without them
                    pass
            
            return field_info
                
        except Exception as e:
            typer.echo(f"⚠️  Error fetching field metadata: {e}")
            return None

    def _get_field_options_for_id(self, field_id):
        """Try to get field options for a specific field ID"""
        try:
            # Try Jira's custom field options endpoint
            jira_url = self.client._options['server'].rstrip('/')
            options_url = f"{jira_url}/rest/api/2/customFieldOption/{field_id}"
            response = self.client._session.get(options_url)
            
            if response.status_code == 200:
                return response.json()
            
            # Try search endpoint as alternative
            search_url = f"{jira_url}/rest/api/2/customFieldOption/{field_id}/search"
            response = self.client._session.get(search_url)
            
            if response.status_code == 200:
                return response.json()
                
        except Exception as e:
            pass  # Continue to fallback method
        
        # Fallback: Get options by searching for issues that use this field
        try:
            config = self._get_config()
            project_key = config.get("jira", {}).get("project", "") if config else ""
            if project_key:
                jql = f'project = {project_key} AND {field_id} is not EMPTY'
                issues = self.client.search_issues(jql, maxResults=50)
                
                # Extract unique values from issues with their IDs
                unique_values = []
                seen_values = set()
                
                for issue in issues:
                    try:
                        field_value = getattr(issue.fields, field_id)
                        if field_value and hasattr(field_value, 'value') and hasattr(field_value, 'id'):
                            value = field_value.value
                            option_id = field_value.id
                            if value not in seen_values:
                                unique_values.append({'value': value, 'id': option_id})
                                seen_values.add(value)
                        elif field_value and isinstance(field_value, str):
                            if field_value not in seen_values:
                                unique_values.append({'value': field_value})
                                seen_values.add(field_value)
                    except:
                        continue
                
                return unique_values
                
        except Exception as e:
            typer.echo(f"⚠️  Could not fetch field options: {e}")
        
        return []

    def _select_best_sprint(self, sprints, preferences):
        """Select the best sprint from a list based on preferences"""
        if not sprints:
            return None
        
        if len(sprints) == 1:
            sprint = sprints[0]
            typer.echo(f"🏃 Found active sprint on board '{sprint.get('board_name')}': {sprint['name']} (ID: {sprint['id']})")
            return sprint
        
        # Preference order:
        # 1. Exact board name match
        # 2. Sprint name contains preferred keywords
        # 3. Board name contains preferred keywords
        # 4. Fall back to first sprint
        
        preferred_board = preferences.get("board_name", "")
        preferred_keywords = preferences.get("sprint_keywords", [])
        
        # Try exact board name match
        if preferred_board:
            for sprint in sprints:
                if sprint.get('board_name', '').lower() == preferred_board.lower():
                    typer.echo(f"🏃 Found preferred board sprint on '{sprint.get('board_name')}': {sprint['name']} (ID: {sprint['id']})")
                    return sprint
        
        # Try sprint name keywords
        if preferred_keywords:
            for keyword in preferred_keywords:
                for sprint in sprints:
                    if keyword.lower() in sprint['name'].lower():
                        typer.echo(f"🏃 Found sprint matching keyword '{keyword}' on board '{sprint.get('board_name')}': {sprint['name']} (ID: {sprint['id']})")
                        return sprint
        
        # Try board name keywords
        if preferred_keywords:
            for keyword in preferred_keywords:
                for sprint in sprints:
                    if keyword.lower() in sprint.get('board_name', '').lower():
                        typer.echo(f"🏃 Found sprint on board matching keyword '{keyword}': {sprint['name']} (ID: {sprint['id']})")
                        return sprint
        
        # Default: try to find "core" or team-related sprints
        for sprint in sprints:
            if 'core' in sprint['name'].lower() or 'core' in sprint.get('board_name', '').lower():
                typer.echo(f"🏃 Found core sprint on board '{sprint.get('board_name')}': {sprint['name']} (ID: {sprint['id']})")
                return sprint
        
        # Fall back to first sprint
        sprint = sprints[0]
        typer.echo(f"🏃 Using first available sprint on board '{sprint.get('board_name')}': {sprint['name']} (ID: {sprint['id']})")
        return sprint

    def _get_active_sprint_via_jql(self, project_key):
        """Fallback method to find active sprint using JQL search"""
        try:
            # Search for issues that are in active sprints
            jql = f'project = {project_key} AND sprint in openSprints() ORDER BY created DESC'
            issues = self.client.search_issues(jql, maxResults=1, fields='sprint')
            
            if not issues:
                return None
            
            # Get the sprint from the first issue
            issue = issues[0]
            if hasattr(issue.fields, 'sprint') and issue.fields.sprint:
                sprint_info = issue.fields.sprint
                # Parse the sprint object - it contains id, name, state in a string format
                if hasattr(sprint_info, 'id'):
                    sprint_data = {
                        'id': sprint_info.id,
                        'name': getattr(sprint_info, 'name', 'Unknown Sprint'),
                        'state': getattr(sprint_info, 'state', 'unknown')
                    }
                    typer.echo(f"🏃 Found active sprint via JQL: {sprint_data['name']} (ID: {sprint_data['id']})")
                    return sprint_data
            
            return None
            
        except Exception as e:
            typer.echo(f"⚠️  JQL fallback method failed: {e}")
            return None

    def create_issue(self, summary, description, issue_type="Task", project_key=None):
        """Create a new Jira issue and assign to current user and active sprint"""
        try:
            # Get project key from config
            if not project_key:
                project_key = self._get_config().get("jira", {}).get("project", "DEV")
            
            # Prepare issue fields including default values
            issue_fields = {
                'project': {'key': project_key.upper()},
                'summary': summary,
                'description': description,
                'issuetype': {'name': issue_type}
            }
            
            # Add default values for custom fields
            default_fields = self._get_default_fields_for_project(project_key.upper())
            if default_fields:
                typer.echo(f"🎯 Applying default field values: {', '.join([f'{k}={v}' for k, v in default_fields.items()])}")
                issue_fields.update(default_fields)
            
            # Create the issue with default fields
            new_issue = self.client.create_issue(fields=issue_fields)
            
            # Now try to assign it to the current user
            try:
                # Get current user and assign to issue
                current_user = self.client.current_user()
                self.client.assign_issue(new_issue.key, current_user)
                typer.echo(f"👤 Assigned ticket {new_issue.key} to current user")
            except Exception as assign_error:
                typer.echo(f"⚠️  Could not auto-assign ticket: {assign_error}")
                # Continue without assignment - the ticket was still created
            
            # Assign to current active sprint (if auto-assign is enabled)
            try:
                # Check if auto-assign to sprint is enabled in config
                auto_assign_sprint = self._cfg.get("auto_assign_sprint", True)
                if auto_assign_sprint:
                    active_sprint = self.get_active_sprint(project_key.upper())
                    if active_sprint:
                        success = self._add_issue_to_sprint(new_issue.key, active_sprint['id'])
                        if success:
                            typer.echo(f"🏃 Added ticket {new_issue.key} to active sprint: {active_sprint['name']}")
                        else:
                            typer.echo(f"⚠️  Could not add ticket {new_issue.key} to sprint (permission issue)")
                            typer.echo(f"💡 To disable auto-assignment: wf config auto-assign-sprint false")
                    else:
                        typer.echo(f"⚠️  Could not find active sprint to assign ticket {new_issue.key}")
            except Exception as sprint_error:
                typer.echo(f"⚠️  Could not add ticket to active sprint: {sprint_error}")
                typer.echo(f"💡 To disable auto-assignment: wf config auto-assign-sprint false")
                # Continue without sprint assignment - the ticket was still created
            
            # Create and return Issue object
            return Issue(new_issue.key, new_issue.fields.summary, new_issue.fields.description or "", new_issue.fields.updated)
            
        except Exception as e:
            print(f"❌ Failed to create Jira issue: {e}")
            return None

    def move_to_in_progress(self, issue, custom_fields=None):
        """Move issue to In Progress with optional custom fields"""
        # First do the transition
        self._transition(issue.key, "In Progress")
        
        # Apply custom fields if provided
        if custom_fields:
            self._apply_custom_fields(issue.key, custom_fields)
    
    def _apply_custom_fields(self, issue_key, custom_fields):
        """Apply custom fields to an issue with type handling"""
        try:
            cfg = self._get_config()
            configured_fields = cfg.get("custom_fields", {})
            fields_info = self._get_fields()
            
            for field_name, field_value in custom_fields.items():
                if field_name in configured_fields:
                    field_config = configured_fields[field_name]
                    field_id = field_config["field_id"]
                    project_key = field_config.get("project_key")
                    
                    # Check if this field applies to the current issue's project
                    if project_key:
                        issue_project = issue_key.split('-')[0]
                        if issue_project.upper() != project_key.upper():
                            typer.echo(f"⚠️  Skipping field '{field_name}' - not applicable to project {issue_project}")
                            continue
                    
                    # Get field schema information for type handling
                    field_info = fields_info.get(field_id.lower())
                    if field_info:
                        field_schema = field_info.get('schema', {})
                        field_type = field_schema.get('type', 'string')
                        
                        # Convert value based on field type
                        processed_value = self._process_field_value(field_value, field_type, field_schema, field_id)
                    else:
                        processed_value = field_value
                    
                    # Get the issue object and update it
                    issue = self.client.issue(issue_key)
                    update_data = {field_id: processed_value}
                    
                    # Apply the update
                    issue.update(fields=update_data)
                    typer.echo(f"✅ Set {field_name} = {field_value} for {issue_key}")
                else:
                    typer.echo(f"⚠️  Unknown custom field: {field_name}")
        
        except Exception as e:
            typer.echo(f"❌ Error applying custom fields: {e}")

    def _transition(self, key, name):
        # Common transition name mappings
        transition_mappings = {
            "done": ["done", "mark as done", "complete", "close", "closed", "resolve"],
            "in progress": ["in progress", "start progress", "start work"],
            "code review": ["code review", "in review", "for review", "peer review", "review"]
        }
        
        # Get target transitions to look for
        target_transitions = transition_mappings.get(name.lower(), [name.lower()])
        
        for t in self.client.transitions(key):
            transition_name = t["name"].lower()
            # Check for exact match or any of the mapped variations
            if transition_name == name.lower() or transition_name in target_transitions:
                self.client.transition_issue(key, t["id"])
                return
        
        # If no transition found, raise an informative error
        available_transitions = [t["name"] for t in self.client.transitions(key)]
        raise Exception(f"No transition found for '{name}'. Available transitions: {', '.join(available_transitions)}")

    def move_to_done(self, issue):
        """Move an issue to Done status"""
        self._transition(issue.key, "Done")

    def move_to_review(self, issue):
        """Move an issue to Review status"""
        self._transition(issue.key, "Code Review")

    def is_done(self, issue):
        """Check if an issue is in a done/completed state"""
        try:
            issue_obj = self.client.issue(issue.key)
            status_name = issue_obj.fields.status.name.lower()
            return status_name in ['done', 'resolved', 'closed', 'complete']
        except Exception as e:
            print(f"❌ Failed to check status for {issue.key}: {e}")
            return False

    def search_fields(self, query):
        """Search fields by name (case-insensitive partial match)"""
        fields = self._get_fields()
        matches = {}
        
        query_lower = query.lower()
        
        for field_name, field_info in fields.items():
            # Check if query matches field name (case-insensitive)
            if query_lower in field_name:
                matches[field_info['name']] = field_info
        
        return matches

    def get_comments(self, key: str):
        """Get all comments for an issue"""
        try:
            issue = self.client.issue(key)
            comments = self.client.comments(issue)
            result = []
            for i, comment in enumerate(comments, 1):
                result.append({
                    'id': comment.id,
                    'author': getattr(comment.author, 'displayName', str(comment.author)) if comment.author else 'unknown',
                    'text': comment.body,
                    'created': comment.created,
                    'updated': comment.updated
                })
            return result
        except Exception as e:
            print(f"❌ Failed to get comments for {key}: {e}")
            return []

    def add_comment(self, key: str, text: str, author: Optional[str] = None):
        """Add a comment to an issue"""
        try:
            issue = self.client.issue(key)
            comment = self.client.add_comment(issue, text)
            return {
                'id': comment.id,
                'author': getattr(comment.author, 'displayName', 'unknown') if comment.author else 'unknown',
                'text': comment.body,
                'created': comment.created,
                'updated': comment.updated
            }
        except Exception as e:
            print(f"❌ Failed to add comment to {key}: {e}")
            return None

    def update_comment(self, key: str, comment_id: int, text: str) -> bool:
        """Update a comment on an issue"""
        try:
            issue = self.client.issue(key)
            comment = self.client.comment(issue, str(comment_id))
            comment.update(body=text)
            return True
        except Exception as e:
            print(f"❌ Failed to update comment #{comment_id} on {key}: {e}")
            return False

    def delete_comment(self, key: str, comment_id: int) -> bool:
        """Delete a comment from an issue"""
        try:
            jira_url = self.client._options['server'].rstrip('/')
            delete_url = f"{jira_url}/rest/api/3/issue/{key}/comment/{comment_id}"
            response = self.client._session.delete(delete_url)
            if response.status_code in [200, 204]:
                return True
            else:
                print(f"❌ Failed to delete comment: {response.status_code}")
                return False
        except Exception as e:
            print(f"❌ Failed to delete comment #{comment_id} from {key}: {e}")
            return False