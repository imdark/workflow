# Custom Fields with `wf start`

The `wf start` command now supports configurable custom fields, allowing you to set project-specific fields (like "POD") when assigning and starting work on Jira tickets.

## Setup

### 1. Configure Custom Fields

First, add your custom fields to the configuration:

```bash
# Add a POD field for the DATA project
wf config add-field POD customfield_10100 --project-key DATA

# Add a Team field that applies to all projects
wf config add-field TEAM customfield_10101
```

### 2. List Configured Fields

```bash
wf config list-fields
```

Output:
```
Configured custom fields:
  POD: customfield_10100 (project: DATA)
  TEAM: customfield_10101
```

## Usage

### Basic Usage

```bash
# Start work and set the POD field
wf start TASK-123 --POD=backend

# Start work and set multiple fields
wf start TASK-123 --POD=backend --TEAM=infrastructure
```

### With Other Options

```bash
# Custom fields work with all other start options
wf start TASK-123 --base=develop --POD=frontend
wf start TASK-123 --repo-path=/path/to/repo --TEAM=platform
```

## Field Configuration Options

When adding a custom field, you need:

- **name**: The field name used in command-line arguments (`--POD=value`)
- **field_id**: The actual Jira field ID (find this in Jira's field configuration)
- **project_key** (optional): Limit this field to a specific project

## Finding Field IDs in Jira

To find the field ID for a custom field in Jira:

1. Go to Jira Administration → Issues → Custom Fields
2. Find your field in the list
3. The field ID will be shown (e.g., `customfield_10100`)

Alternatively, you can find it by inspecting the field in your browser's developer tools or through Jira's REST API.

## Project-Specific Fields

When you specify a project key, the custom field will only be applied when working on tickets from that project:

```bash
# This field only applies to DATA-* tickets
wf config add-field POD customfield_10100 --project-key DATA

# This will apply the POD field
wf start DATA-123 --POD=backend

# This will skip the POD field (wrong project)
wf start PROJ-456 --POD=backend
# Output: ⚠️  Skipping field 'POD' - not applicable to project PROJ
```

## Error Handling

The system will gracefully handle:
- Unknown custom fields (warning message)
- Fields that don't apply to the current project (skipped with warning)
- Field update failures (warning, but workflow continues)

## Managing Custom Fields

### Remove a Field

```bash
wf config remove-field POD
```

### Update a Field

To update a field configuration, remove it first, then add it again:

```bash
wf config remove-field POD
wf config add-field POD customfield_10101 --project-key=NEWPROJ
```

## Example Workflow

```bash
# Setup
wf config add-field POD customfield_10100 --project-key DATA
wf config add-field SPRINT customfield_10101

# Daily usage
wf start DATA-123 --POD=backend --SPRINT=Sprint-42
# ✅ Now working on task DATA-123
# ✅ Set POD = backend for DATA-123
# ✅ Set SPRINT = Sprint-42 for DATA-123
# [continues with branch creation, terminal tabs, etc.]
```